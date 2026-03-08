"""
Cricket domain knowledge retrieval via ChromaDB.

Loads backend/app/cricket_rules.md, splits it into chunks at ## section
boundaries, embeds each chunk with OpenAI embeddings, and stores them in
an in-memory ChromaDB collection named "cricket_rules".

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

The singleton vector store is built once on the first request and reused
for all subsequent calls. No disk persistence is needed for this small
document (~26 sections).

TODO: If startup latency becomes an issue, switch to a persistent Chroma
      collection on disk so embeddings are not recomputed on every cold start.
"""

import logging
import re
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

# Lazy singleton — built on first call, reused for all subsequent requests.
_vectorstore: Chroma | None = None


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
    Build the cricket-rules vector store once and return the singleton.

    Reads cricket_rules.md, chunks by ## section, embeds with OpenAI,
    and stores in an in-memory ChromaDB collection "cricket_rules".
    Subsequent calls return the cached store immediately.
    """
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    raw_text = _RULES_PATH.read_text(encoding="utf-8")
    chunks = _chunk_by_h2(raw_text)

    _vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=OpenAIEmbeddings(api_key=settings.openai_api_key),
        collection_name="cricket_rules",
    )
    logger.info(
        "Cricket rules vector store built | file=%s | chunks=%d",
        _RULES_PATH.name,
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
