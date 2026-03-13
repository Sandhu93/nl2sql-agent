"""
Visualization agent — Phase 9.5 (MCP chart server).

Generates a Vega-Lite v5 chart spec when the user's question explicitly asks
for a chart, graph, or plot.

Architecture (Phase 9.5):
  Old: LLM builds the full Vega-Lite JSON (unreliable — hallucinated fields,
       malformed JSON, wrong schema version)
  New: LLM does ONE cheap step — extract chart intent (type + field names).
       The MCP chart server builds the spec deterministically from those
       structured inputs, returning a guaranteed-valid Vega-Lite v5 spec.

  viz_agent.py  ──intent──►  LLM (cheap: type + field names only)
                ──data+intent──►  MCP chart server (generate_chart tool)
                                        │
                                        ▼
                              deterministic Vega-Lite v5 spec

Failures are always silent — any exception returns None so the main
answer pipeline is never blocked.

TODO: Replace the LLM intent step with a rule-based extractor once we have
      enough examples to cover the common patterns deterministically.
"""

import ast
import json
import logging
import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Viz intent detection — lightweight regex, runs before any LLM call
# ---------------------------------------------------------------------------

_VIZ_INTENT_RE = re.compile(
    r"\b(chart|graph|plot|visuali[sz]e|visuali[sz]ation|"
    r"bar\s+chart|line\s+chart|pie\s+chart|scatter|histogram|"
    r"show\s+.*\s+chart|draw\s+.*\s+chart|display\s+.*\s+chart)\b",
    re.IGNORECASE,
)


def wants_visualization(question: str) -> bool:
    """Return True if the question explicitly asks for a chart or graph."""
    return bool(_VIZ_INTENT_RE.search(question))


# ---------------------------------------------------------------------------
# Step 1 — Intent extraction (small LLM call: type + field names only)
# ---------------------------------------------------------------------------

_INTENT_PROMPT = PromptTemplate.from_template(
    "You are a data visualization analyst. Analyze this question and SQL result "
    "to determine chart metadata.\n\n"
    "Question: {question}\n"
    "SQL Result (Python tuple format, first few rows): {result_preview}\n\n"
    "The SQL result is a list of tuples. Determine what each column represents:\n"
    "  - Column 0: usually the category (player name, team, year, over number)\n"
    "  - Column 1: usually the numeric value (runs, wickets, economy, count)\n\n"
    "Return a JSON object with exactly these keys:\n"
    '  "chart_type": "bar" | "line" | "point"\n'
    "    - bar:   rankings, counts, comparisons by named entity\n"
    "    - line:  time series (x-axis is year, season, or over number)\n"
    "    - point: scatter (both axes are numeric)\n"
    '  "x_field": short snake_case identifier for column 0 (e.g. "batsman", "year")\n'
    '  "y_field": short snake_case identifier for column 1 (e.g. "total_runs", "wickets")\n'
    '  "x_label": human-readable axis label for column 0\n'
    '  "y_label": human-readable axis label for column 1\n'
    '  "title":   concise chart title, max 8 words\n\n'
    "Output ONLY the raw JSON object — no markdown fences, no explanation.\n"
    "JSON:"
)


async def _extract_chart_intent(question: str, result_preview: str, llm) -> dict:
    """
    Use a small LLM call to determine chart type and column field names.

    Returns a dict with keys: chart_type, x_field, y_field, x_label, y_label, title.
    Falls back to safe defaults if the LLM call or JSON parse fails.
    """
    try:
        raw: str = await (_INTENT_PROMPT | llm | StrOutputParser()).ainvoke({
            "question": question,
            "result_preview": result_preview[:400],
        })
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        intent = json.loads(raw)
        intent.setdefault("chart_type", "bar")
        intent.setdefault("x_field", "category")
        intent.setdefault("y_field", "value")
        intent.setdefault("x_label", "")
        intent.setdefault("y_label", "")
        intent.setdefault("title", "")
        return intent

    except Exception as exc:
        logger.warning("Chart intent extraction failed — using defaults: %s", exc)
        return {
            "chart_type": "bar",
            "x_field": "category",
            "y_field": "value",
            "x_label": "",
            "y_label": "",
            "title": "",
        }


# ---------------------------------------------------------------------------
# Step 2 — Parse SQL result string → list of dicts for MCP tool
# ---------------------------------------------------------------------------

def _parse_result_to_rows(result: str, x_field: str, y_field: str) -> list[dict]:
    """
    Convert QuerySQLDataBaseTool result string into a list of dicts.

    The result is a Python repr of a list of tuples, e.g.:
      "[('V Kohli', 6624), ('S Dhawan', 5784)]"

    Only the first two columns are used (x and y). Values are converted to
    JSON-serializable types (Decimal → float, etc.).
    """
    try:
        rows = ast.literal_eval(result)
    except Exception:
        return []

    if not isinstance(rows, list):
        rows = [rows]

    data: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            row = (row,)
        if len(row) < 2:
            continue

        x_val = row[0]
        y_val = row[1]

        # Convert non-JSON-serializable types (e.g. Decimal from psycopg2)
        if hasattr(y_val, "__float__") and not isinstance(y_val, float):
            y_val = float(y_val)
        if hasattr(x_val, "__float__") and not isinstance(x_val, float):
            x_val = float(x_val)

        # Stringify x values for nominal axes (cleaner Vega-Lite labels)
        if not isinstance(x_val, (int, float)):
            x_val = str(x_val)

        data.append({x_field: x_val, y_field: y_val})

    return data[:20]


# ---------------------------------------------------------------------------
# Step 3 — MCP client call → deterministic Vega-Lite spec
# ---------------------------------------------------------------------------

async def _call_mcp_generate_chart(
    data_rows: list[dict],
    intent: dict,
    mcp_url: str,
) -> dict | None:
    """
    Call the generate_chart MCP tool on the chart server over SSE.

    Opens a fresh SSE connection per request (stateless, no persistent client).
    Returns the parsed Vega-Lite spec dict, or None on any failure.

    TODO: Consider a persistent connection pool if request volume is high.
    """
    try:
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        sse_url = f"{mcp_url}/sse"
        async with sse_client(url=sse_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "generate_chart",
                    arguments={
                        "data_rows": data_rows,
                        "chart_type": intent.get("chart_type", "bar"),
                        "x_field": intent.get("x_field", "category"),
                        "y_field": intent.get("y_field", "value"),
                        "x_label": intent.get("x_label", ""),
                        "y_label": intent.get("y_label", ""),
                        "title": intent.get("title", ""),
                    },
                )

                # Tool returns a JSON string wrapped in TextContent
                if not result.content:
                    logger.warning("MCP chart server returned empty content")
                    return None

                spec = json.loads(result.content[0].text)
                logger.info(
                    "MCP chart spec received | mark=%s | data_points=%d",
                    spec.get("mark"), len(spec.get("data", {}).get("values", [])),
                )
                return spec

    except Exception as exc:
        logger.warning("MCP chart server call failed (non-blocking): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point — called from agent.py
# ---------------------------------------------------------------------------

async def generate_chart_spec(question: str, result: str, llm) -> dict | None:
    """
    Generate a Vega-Lite v5 spec for the given SQL result via the MCP chart server.

    Pipeline:
      1. _extract_chart_intent()    — LLM extracts chart type + column field names
      2. _parse_result_to_rows()    — SQL result string → list of dicts
      3. _call_mcp_generate_chart() — MCP tool builds the deterministic spec

    Args:
        question: The standalone natural-language question.
        result:   Raw SQL result string from QuerySQLDataBaseTool.
        llm:      The LLM instance (primary + fallbacks) from agent.py.

    Returns:
        Vega-Lite spec dict, or None if any step fails or spec is invalid.
    """
    mcp_url = settings.mcp_chart_server_url

    # Step 1 — extract chart intent (cheap LLM call for type + field names only)
    intent = await _extract_chart_intent(question, result, llm)
    logger.info(
        "Chart intent | chart_type=%s | x=%s | y=%s",
        intent["chart_type"], intent["x_field"], intent["y_field"],
    )

    # Step 2 — parse SQL result into list of dicts
    data_rows = _parse_result_to_rows(result, intent["x_field"], intent["y_field"])
    if not data_rows:
        logger.warning("Chart skipped — no parseable rows in SQL result")
        return None

    # Step 3 — call the MCP chart server for the deterministic spec
    return await _call_mcp_generate_chart(data_rows, intent, mcp_url)
