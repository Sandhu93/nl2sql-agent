"""
Insight generation agent — Phase 8.

Runs in parallel with rephrase_answer (via asyncio.gather in agent.py) after
every successful SQL execution.  Produces two artefacts:

  key_takeaway    — one sentence highlighting the most interesting finding
  follow_up_chips — 2-3 short follow-up questions the user might ask next

Failures are silent: any exception returns empty defaults so the main
answer is never blocked.

TODO: If latency becomes a concern, gate this behind a config flag
      (e.g. ENABLE_INSIGHTS=true in config.py / .env).
"""

import json
import logging

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

logger = logging.getLogger(__name__)

_INSIGHTS_PROMPT = PromptTemplate.from_template(
    "You are an IPL cricket data analyst.\n\n"
    "Question: {question}\n"
    "SQL Result: {result}\n\n"
    "Return a JSON object with exactly these two keys:\n"
    '  "key_takeaway": one concise sentence (max 25 words) highlighting the most interesting finding\n'
    '  "follow_up_chips": a list of exactly 3 short follow-up questions (max 10 words each) '
    "the user might want to ask next, answerable from the IPL database\n\n"
    "Rules:\n"
    "- Output ONLY the raw JSON object — no markdown fences, no explanation\n"
    "- follow_up_chips must be specific to the data shown and answerable by this cricket database\n"
    "- Never suggest questions that require external data outside this IPL dataset\n\n"
    "JSON:"
)


async def generate_insights(question: str, result: str, llm) -> dict:
    """
    Generate a key takeaway and follow-up question chips from a SQL result.

    Args:
        question: The standalone natural-language question.
        result:   Raw SQL result string from QuerySQLDataBaseTool.
        llm:      The LLM instance (primary + fallbacks) from agent.py.

    Returns:
        dict with keys:
          "key_takeaway"    — str  (empty string on failure)
          "follow_up_chips" — list[str]  (empty list on failure)
    """
    try:
        raw: str = await (_INSIGHTS_PROMPT | llm | StrOutputParser()).ainvoke({
            "question": question,
            "result": result[:2000],  # truncate very large result sets
        })
        raw = raw.strip()

        # Strip markdown fences if the model added them despite instructions
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        return {
            "key_takeaway": str(data.get("key_takeaway", "")),
            "follow_up_chips": [str(q) for q in data.get("follow_up_chips", [])[:3]],
        }
    except Exception as exc:
        logger.warning("Insight generation failed (non-blocking): %s", exc)
        return {"key_takeaway": "", "follow_up_chips": []}
