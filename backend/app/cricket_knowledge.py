"""
Cricket domain knowledge retrieval via ChromaDB.

Loads backend/app/cricket_rules.md, splits it into chunks at ## section
boundaries, embeds each chunk with OpenAI embeddings, and stores them in
a persistent ChromaDB collection at settings.chroma_persist_dir/cricket_rules.

retrieve_cricket_rules(question, k=3) returns the k most relevant rule
sections as a single string, ready to be injected into the SQL-generation
system prompt as {cricket_context}.

Chunking strategy
-----------------
The file is split at every ## heading (top-level sections). Each chunk
contains the full section text including all ### subsections beneath it,
so retrieved context is always complete — e.g. "Bowling Rules" includes
the aggregation grain rule, every bowling metric formula, and the
"Never Do This" warning all in one chunk.

Disk persistence
----------------
The vector store is persisted to disk (settings.chroma_persist_dir/cricket_rules)
so embeddings are NOT recomputed on every cold start. A SHA-256 hash of
cricket_rules.md is written alongside the store as .content_hash. On startup:
  - hash matches  → load from disk (fast, no OpenAI call)
  - hash differs  → re-embed and overwrite (content changed)
  - dir missing   → embed from scratch

To force a full re-embed (e.g. after upgrading chromadb): delete the
cricket_rules subdirectory and restart.

TODO: Add file-lock around the write path to prevent two concurrent processes
      (multiple uvicorn workers) from corrupting the store simultaneously.
"""

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import List

from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Path to the rules file — same directory as this module.
_RULES_PATH = Path(__file__).parent / "cricket_rules.md"

# Name of the sidecar hash file written next to the ChromaDB SQLite files.
_HASH_FILE_NAME = ".content_hash"

# Lazy singleton — built on first call, reused for all subsequent requests.
_vectorstore: Chroma | None = None


def _content_hash() -> str:
    """SHA-256 of the cricket_rules.md source file."""
    return hashlib.sha256(_RULES_PATH.read_bytes()).hexdigest()


def _is_cache_valid(chroma_dir: Path) -> bool:
    """
    Return True only when the on-disk store exists AND its content hash
    matches the current cricket_rules.md.  Any mismatch triggers a
    full re-embed so the store never silently serves stale content.
    """
    hash_file = chroma_dir / _HASH_FILE_NAME
    if not hash_file.exists():
        return False
    # Confirm there is actual ChromaDB data alongside the hash file.
    has_data = any(f.name != _HASH_FILE_NAME for f in chroma_dir.iterdir())
    if not has_data:
        return False
    return hash_file.read_text().strip() == _content_hash()


def _chunk_by_h2(text: str) -> List[Document]:
    """
    Split a markdown document into chunks at every ## heading.

    Each chunk contains the complete ## section text (including all ###
    subsections beneath it), tagged with the section heading as metadata.
    Keeping subsections inside the parent chunk ensures that when e.g.
    "Bowling Rules" is retrieved, all bowling metric formulas and the
    GROUP BY rule come with it — not as disconnected fragments.

    Returns a list of LangChain Documents:
        page_content: full section text starting from "## <heading>"
        metadata:     {"heading": "<section title without ##>"}
    """
    # Split on lines that begin with exactly "## " (not ### or ####)
    parts = re.split(r"(?m)^(?=## )", text)
    docs = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # First line is the heading; rest is the body
        first_line = part.splitlines()[0]
        heading = first_line.lstrip("#").strip()
        docs.append(Document(page_content=part, metadata={"heading": heading}))
    return docs


def _get_vectorstore() -> Chroma:
    """
    Return the cricket-rules vector store, loading from disk when possible.

    Flow:
      1. Return in-process singleton immediately if already loaded.
      2. If on-disk store exists and its content hash matches → load from disk
         (no OpenAI call, fast cold start).
      3. Otherwise re-embed cricket_rules.md, persist to disk, write hash.

    The persist directory is settings.chroma_persist_dir/cricket_rules, which
    is mounted as a Docker named volume so it survives container restarts.
    """
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    chroma_dir = Path(settings.chroma_persist_dir) / "cricket_rules"
    chroma_dir.mkdir(parents=True, exist_ok=True)
    embeddings = OpenAIEmbeddings(api_key=settings.openai_api_key)

    if _is_cache_valid(chroma_dir):
        # Fast path: load from disk — no embeddings API call required.
        _vectorstore = Chroma(
            collection_name="cricket_rules",
            embedding_function=embeddings,
            persist_directory=str(chroma_dir),
        )
        logger.info(
            "Cricket rules vector store loaded from disk | dir=%s", chroma_dir
        )
    else:
        # Slow path: (re-)embed and persist.  Clear any stale data first.
        if chroma_dir.exists():
            shutil.rmtree(chroma_dir)
        chroma_dir.mkdir(parents=True, exist_ok=True)

        raw_text = _RULES_PATH.read_text(encoding="utf-8")
        chunks = _chunk_by_h2(raw_text)

        _vectorstore = Chroma.from_documents(
            documents=chunks,
            embedding=embeddings,
            collection_name="cricket_rules",
            persist_directory=str(chroma_dir),
        )
        # Write hash so next cold start skips re-embedding.
        (chroma_dir / _HASH_FILE_NAME).write_text(_content_hash())
        logger.info(
            "Cricket rules vector store built and persisted | dir=%s | chunks=%d",
            chroma_dir,
            len(chunks),
        )
    return _vectorstore


async def retrieve_cricket_rules(question: str, k: int = 3) -> str:
    """
    Return the k most relevant cricket rule sections for a given question.

    Uses cosine similarity between the question embedding and each stored
    section chunk to select the most relevant rules. The returned string
    is injected directly into the SQL-generation system prompt as
    {cricket_context}.

    Called in parallel with table selection in run_agent() so it adds
    no wall-clock latency beyond what table selection already takes.

    Args:
        question: The standalone NL question (post-rewrite).
        k:        Number of sections to retrieve. Default 3 gives
                  ~1000–1500 tokens of context, well within budget.

    Returns:
        A single string of the retrieved sections joined by "---"
        separators, ready for prompt injection. Returns a safe
        fallback string on any error so it never blocks the pipeline.
    """
    try:
        vs = _get_vectorstore()
        docs = await vs.asimilarity_search(question, k=k)
        logger.info(
            "Cricket rules retrieved | k=%d | sections=%s",
            len(docs),
            [d.metadata.get("heading") for d in docs],
        )
        return "\n\n---\n\n".join(d.page_content for d in docs)
    except Exception as exc:
        # Never let retrieval failure block SQL generation.
        logger.warning("Cricket rules retrieval failed: %s", exc)
        return ""
