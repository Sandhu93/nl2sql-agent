"""
MCP Chart Server — Phase 9.5

Deterministic Vega-Lite v5 spec generation exposed as an MCP tool over
SSE/HTTP transport.

Why MCP instead of LLM-generated specs?
  - Guaranteed-valid JSON structure (no hallucinated field names or bad schema)
  - No LLM call for spec assembly — deterministic from structured inputs
  - The LLM in viz_agent.py only does the cheap intent-extraction step
    (chart type + column names), not the full spec build

Transport: SSE on port 8087
SSE endpoint: http://mcp_chart_server:8087/sse  (internal Docker DNS)
Health check: TCP socket on port 8087

Tool exposed:
  generate_chart(data_rows, chart_type, x_field, y_field, x_label, y_label, title)
    → Vega-Lite v5 spec as a JSON string
"""

import json
import logging
import os

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("chart-server")


@mcp.tool()
def generate_chart(
    data_rows: list[dict],
    chart_type: str,
    x_field: str,
    y_field: str,
    x_label: str = "",
    y_label: str = "",
    title: str = "",
) -> str:
    """
    Build a deterministic Vega-Lite v5 spec from structured inputs.

    Args:
        data_rows:  Parsed SQL result as list of dicts.
                    e.g. [{"batsman": "V Kohli", "total_runs": 6624}, ...]
        chart_type: "bar" | "line" | "point"
                    - bar:   horizontal bar chart (rankings, counts)
                    - line:  line chart (time series by year/season/over)
                    - point: scatter plot (two numeric axes)
        x_field:    Field name for the category / x axis (e.g. "batsman", "year")
        y_field:    Field name for the value / y axis (e.g. "total_runs", "wickets")
        x_label:    Human-readable label for the category axis (defaults to x_field)
        y_label:    Human-readable label for the value axis (defaults to y_field)
        title:      Short chart title (max 8 words)

    Returns:
        JSON string of a valid Vega-Lite v5 spec.
    """
    chart_type = chart_type.lower().strip()
    if chart_type not in ("bar", "line", "point"):
        logger.warning("Unknown chart_type %r — defaulting to bar", chart_type)
        chart_type = "bar"

    # Cap at 20 data points for readability
    values = data_rows[:20]

    tooltip = [
        {"field": x_field, "type": "nominal" if chart_type != "point" else "quantitative"},
        {"field": y_field, "type": "quantitative"},
    ]

    if chart_type == "bar":
        # Horizontal bar: category on y-axis, value on x-axis.
        # Horizontal layout is better for named entities (player names, teams).
        encoding = {
            "y": {
                "field": x_field,
                "type": "nominal",
                "sort": "-x",
                "axis": {"title": x_label or x_field},
            },
            "x": {
                "field": y_field,
                "type": "quantitative",
                "axis": {"title": y_label or y_field},
            },
            "tooltip": tooltip,
        }

    elif chart_type == "line":
        # Line chart: ordinal x-axis (year, season, over number).
        encoding = {
            "x": {
                "field": x_field,
                "type": "ordinal",
                "axis": {"title": x_label or x_field},
            },
            "y": {
                "field": y_field,
                "type": "quantitative",
                "axis": {"title": y_label or y_field},
            },
            "tooltip": tooltip,
        }

    else:  # point / scatter
        encoding = {
            "x": {
                "field": x_field,
                "type": "quantitative",
                "axis": {"title": x_label or x_field},
            },
            "y": {
                "field": y_field,
                "type": "quantitative",
                "axis": {"title": y_label or y_field},
            },
            "tooltip": [
                {"field": x_field, "type": "quantitative"},
                {"field": y_field, "type": "quantitative"},
            ],
        }

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": 600,
        "height": 350,
        "title": title,
        "data": {"values": values},
        "mark": {"type": chart_type, "tooltip": True},
        "encoding": encoding,
    }

    logger.info(
        "Chart spec built | chart_type=%s | rows=%d | x=%s | y=%s",
        chart_type, len(values), x_field, y_field,
    )
    return json.dumps(spec)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8087"))
    logger.info("Starting MCP chart server | transport=SSE | port=%d", port)
    # TODO: When MCP SDK supports adding custom routes to the SSE app,
    #       add a /health endpoint here for a proper HTTP health check.
    mcp.run(transport="sse", host="0.0.0.0", port=port)
