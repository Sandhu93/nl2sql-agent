"""
Visualization agent — Phase 9.

Generates a Vega-Lite v5 chart spec when the user's question explicitly asks
for a chart, graph, or plot.  Triggered by wants_visualization() and runs in
parallel with rephrase_answer + generate_insights via asyncio.gather.

Only activated when the question contains a visualization keyword (e.g. "bar
chart", "plot", "visualize") — silent no-op otherwise.

Failures are silent: any exception returns None so the main answer is not
blocked.

TODO: Replace the LLM-based spec generation with an MCP chart-server tool
      call for deterministic, schema-validated Vega-Lite output:

          from mcp_chart_server import generate_spec
          chart_spec = await generate_spec(
              data_rows=parse_sql_result(result),
              chart_type=detect_chart_type(question),
              x_field=...,
              y_field=...,
          )

      The MCP server receives the data + intent, returns a guaranteed-valid
      Vega-Lite spec without relying on the LLM for JSON structure.
      See: https://vega.github.io/vega-lite/
"""

import json
import logging
import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Viz intent detection — lightweight regex before committing to an LLM call
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
# Vega-Lite spec generation
# ---------------------------------------------------------------------------

_VIZ_PROMPT = PromptTemplate.from_template(
    "You are a data visualization expert. Generate a Vega-Lite v5 JSON spec "
    "to visualize the SQL result below.\n\n"
    "Question: {question}\n"
    "SQL Result (raw string, rows as Python tuples): {result}\n\n"
    "Rules:\n"
    "1. Parse the result string into a 'values' array of JSON objects for the Vega-Lite data field\n"
    "2. Infer column names from the question context (e.g. 'player', 'total_runs')\n"
    "3. Best chart type: 'bar' for rankings/counts, 'line' for time series, 'point' for scatter\n"
    "4. Use descriptive axis labels derived from the question\n"
    "5. Include at most 20 data points (first 20 if there are more)\n"
    "6. For bar charts with long labels, use 'y' for the category axis and 'x' for the value\n"
    "7. The spec MUST contain: $schema, width (600), height (350), data.values (array), mark, encoding\n"
    "8. Output ONLY the raw JSON spec — no markdown fences, no explanation\n\n"
    'JSON spec (starting with "{{"):'
)


async def generate_chart_spec(question: str, result: str, llm) -> dict | None:
    """
    Generate a Vega-Lite v5 spec for the given SQL result.

    Args:
        question: The standalone natural-language question.
        result:   Raw SQL result string from QuerySQLDataBaseTool.
        llm:      The LLM instance (primary + fallbacks) from agent.py.

    Returns:
        Vega-Lite spec dict, or None if generation fails or spec is invalid.

    TODO: Replace body with MCP chart-server call (see module docstring).
    """
    try:
        raw: str = await (_VIZ_PROMPT | llm | StrOutputParser()).ainvoke({
            "question": question,
            "result": result[:3000],
        })
        raw = raw.strip()

        # Strip markdown fences if model added them despite instructions
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        spec = json.loads(raw)

        # Sanity check — must have data values + encoding or we can't render it
        data_ok = "data" in spec and isinstance(spec["data"].get("values"), list)
        enc_ok = "encoding" in spec
        if not data_ok or not enc_ok:
            logger.warning("Generated Vega-Lite spec is missing required fields; discarding")
            return None

        logger.info("Chart spec generated | mark=%s | data_points=%d",
                    spec.get("mark"), len(spec["data"]["values"]))
        return spec

    except Exception as exc:
        logger.warning("Chart spec generation failed (non-blocking): %s", exc)
        return None
