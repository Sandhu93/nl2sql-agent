"""
Insight generation agent — Phase 8.

Runs after successful SQL execution and returns:
  - key_takeaway: concise analyst-style insight (conditionally shown)
  - follow_up_chips: short next-question suggestions

Design goals:
  - Keep main answer non-blocking (failures are silent).
  - Avoid repetitive chips.
  - Prefer deterministic chips for common player/team/year flows.
"""

import ast
import json
import logging
import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

logger = logging.getLogger(__name__)

_INSIGHTS_PROMPT = PromptTemplate.from_template(
    "You are an IPL cricket data analyst.\n\n"
    "Question: {question}\n"
    "SQL Result: {result}\n\n"
    "Return a JSON object with exactly these two keys:\n"
    '  "key_takeaway": one concise sentence (max 25 words) with a NEW angle '
    "(comparison/context/caveat/trend), not a restatement\n"
    '  "follow_up_chips": a list of exactly 3 short follow-up questions '
    "(max 10 words each) answerable from the IPL database\n\n"
    "Rules:\n"
    "- Output ONLY the raw JSON object — no markdown fences, no explanation\n"
    "- follow_up_chips must be specific to the data shown and answerable by this cricket database\n"
    "- Do NOT restate the answer in key_takeaway; add one new angle\n"
    "- Never suggest questions that require external data outside this IPL dataset\n\n"
    "JSON:"
)


def _parse_result_rows(result: str) -> list:
    """Best-effort parse of QuerySQLDataBaseTool result strings."""
    try:
        parsed = ast.literal_eval(result)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, tuple):
            return [parsed]
    except Exception:
        pass
    return []


def _is_rich_output(question: str, rows: list) -> bool:
    """
    Rich outputs get key_takeaway.
    Single-value/simple outputs should primarily get follow-up chips.
    """
    q = question.lower()
    richness_terms = (
        "top", "rank", "highest", "lowest", "compare", "comparison",
        "trend", "over time", "by year", "by season", "distribution",
        "most", "best", "least", "fewest", "worst", "maximum", "minimum",
        "average", "total", "all time", "career",
    )
    if len(rows) >= 2:
        return True
    return any(term in q for term in richness_terms)


_SENTENCE_STARTERS = {
    "has", "had", "did", "was", "were", "who", "which", "what", "how",
    "when", "where", "why", "is", "are", "can", "could", "will", "would",
    "should", "do", "does", "the", "a", "an", "in", "on", "at", "for",
    "of", "to", "by", "with", "from", "tell", "show", "list", "give",
}


def _extract_player(question: str) -> str | None:
    # Use finditer on individual cap-words so we can check adjacent pairs
    # without the non-overlapping consumption problem of paired findall.
    cap_words = list(re.finditer(r"\b[A-Z][a-z]+\b", question))
    for i in range(len(cap_words) - 1):
        w1, w2 = cap_words[i].group(), cap_words[i + 1].group()
        # Only consider genuinely adjacent words (just whitespace between)
        between = question[cap_words[i].end(): cap_words[i + 1].start()]
        if between.strip():
            continue
        if w1.lower() in _SENTENCE_STARTERS:
            continue
        return f"{w1} {w2}"
    return None


def _extract_year(question: str) -> str | None:
    m = re.search(r"\b(20\d{2})\b", question)
    return m.group(1) if m else None


def _extract_team(question: str) -> str | None:
    teams = [
        "Chennai Super Kings", "Mumbai Indians", "Royal Challengers Bangalore",
        "Royal Challengers Bengaluru", "Kolkata Knight Riders", "Rajasthan Royals",
        "Sunrisers Hyderabad", "Punjab Kings", "Kings XI Punjab", "Delhi Capitals",
        "Delhi Daredevils", "Lucknow Super Giants", "Gujarat Titans",
    ]
    q_lower = question.lower()
    for team in teams:
        if team.lower() in q_lower:
            return team
    return None


def _template_chips(question: str) -> list[str]:
    """Deterministic chip seeds for common player/team/year flows."""
    player = _extract_player(question)
    team = _extract_team(question)
    year = _extract_year(question)

    chips: list[str] = []
    if player and team:
        chips.extend([
            f"How many matches for {team}?",
            f"What is {player}'s highest score for {team}?",
            f"How many runs has {player} scored for {team}?",
        ])
    elif player and year:
        chips.extend([
            f"How many runs did {player} score in {year}?",
            f"What was {player}'s strike rate in {year}?",
            f"Who were {player}'s top opponents in {year}?",
        ])
    elif team and year:
        chips.extend([
            f"Who scored most runs for {team} in {year}?",
            f"Who took most wickets for {team} in {year}?",
            f"How did {team} perform by venue in {year}?",
        ])
    elif player:
        chips.extend([
            f"What is {player}'s highest score?",
            f"How many matches has {player} played?",
            f"What is {player}'s batting average?",
        ])
    elif team:
        chips.extend([
            f"How did {team} perform by season?",
            f"Who are top run scorers for {team}?",
            f"Who are top wicket takers for {team}?",
        ])
    elif year:
        chips.extend([
            f"Who scored most runs in {year}?",
            f"Which team won most matches in {year}?",
            f"Who took most wickets in {year}?",
        ])
    return chips


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _is_too_similar(a: str, b: str) -> bool:
    """Lightweight overlap guard to avoid repeated/same-intent chips."""
    sa = set(_normalize_text(a).split())
    sb = set(_normalize_text(b).split())
    if not sa or not sb:
        return False
    overlap = len(sa & sb) / max(len(sa), len(sb))
    return overlap >= 0.75


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = _normalize_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


async def generate_insights(
    question: str,
    result: str,
    llm,
    recent_chips: list[str] | None = None,
    invoke_fn=None,
) -> dict:
    """
    Generate conditional insight + follow-up chips.

    `recent_chips` allows cross-turn dedupe to avoid repeating suggestions.
    `invoke_fn`: optional coroutine ``(chain, inputs) -> result`` used to route
        the LLM call through a semaphore and circuit breaker (e.g. agent._llm_invoke).
        When omitted the chain is called directly (backward-compatible).
    """
    recent_chips = recent_chips or []
    rows = _parse_result_rows(result)
    rich_output = _is_rich_output(question, rows)

    async def _invoke(chain, inputs: dict):
        if invoke_fn is not None:
            return await invoke_fn(chain, inputs)
        return await chain.ainvoke(inputs)

    try:
        raw: str = await _invoke(_INSIGHTS_PROMPT | llm | StrOutputParser(), {
            "question": question,
            "result": result[:2000],
        })
        raw = raw.strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        llm_key_takeaway = str(data.get("key_takeaway", "")).strip()
        llm_chips = [str(q).strip() for q in data.get("follow_up_chips", [])]
    except Exception as exc:
        logger.warning("Insight generation failed (non-blocking): %s", exc)
        llm_key_takeaway = ""
        llm_chips = []

    template_chips = _template_chips(question)
    merged_chips = _dedupe_preserve_order(template_chips + llm_chips)

    filtered_chips: list[str] = []
    for chip in merged_chips:
        # Result-aware constraints:
        # 1) avoid re-asking the same question
        # 2) avoid repeating recent-turn suggestions
        if _is_too_similar(chip, question):
            continue
        if any(_is_too_similar(chip, prev) for prev in recent_chips):
            continue
        filtered_chips.append(chip)

    return {
        # Conditional takeaway: suppress for simple/single-value outputs.
        "key_takeaway": llm_key_takeaway if rich_output else "",
        "follow_up_chips": filtered_chips[:3],
    }
