"""
Unit tests for viz_agent.py — spec validation and fallback renderer.

Covers:
  - _validate_vega_lite_spec(): structural validation of Vega-Lite v5 specs
  - _build_fallback_spec(): deterministic in-process spec builder
  - generate_chart_spec() fallback path: MCP None / invalid spec / valid spec / empty rows

All tests are fully mocked — no network, no LLM, no MCP server calls.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.viz_agent import (
    _validate_vega_lite_spec,
    _build_fallback_spec,
    generate_chart_spec,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _minimal_valid_spec(mark="bar", data_values=None) -> dict:
    """Return the smallest spec that passes _validate_vega_lite_spec."""
    if data_values is None:
        data_values = [{"batsman": "V Kohli", "runs": 6624}]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": data_values},
        "mark": mark,
        "encoding": {
            "x": {"field": "runs", "type": "quantitative"},
            "y": {"field": "batsman", "type": "nominal"},
        },
    }


def _sample_data_rows(n=3) -> list[dict]:
    return [{"batsman": f"Player{i}", "runs": 1000 - i * 100} for i in range(n)]


def _sample_intent(chart_type="bar") -> dict:
    return {
        "chart_type": chart_type,
        "x_field": "batsman",
        "y_field": "runs",
        "x_label": "Batsman",
        "y_label": "Runs",
        "title": "Top Run Scorers",
    }


# ---------------------------------------------------------------------------
# _validate_vega_lite_spec
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestValidateVegaLiteSpec:
    """Structural validation of Vega-Lite v5 specs."""

    def test_valid_spec_returns_true(self):
        spec = _minimal_valid_spec()
        assert _validate_vega_lite_spec(spec) is True

    def test_missing_schema_returns_false(self):
        spec = _minimal_valid_spec()
        del spec["$schema"]
        assert _validate_vega_lite_spec(spec) is False

    def test_missing_data_returns_false(self):
        spec = _minimal_valid_spec()
        del spec["data"]
        assert _validate_vega_lite_spec(spec) is False

    def test_missing_mark_returns_false(self):
        spec = _minimal_valid_spec()
        del spec["mark"]
        assert _validate_vega_lite_spec(spec) is False

    def test_missing_encoding_returns_false(self):
        spec = _minimal_valid_spec()
        del spec["encoding"]
        assert _validate_vega_lite_spec(spec) is False

    def test_data_values_empty_list_returns_false(self):
        spec = _minimal_valid_spec(data_values=[])
        assert _validate_vega_lite_spec(spec) is False

    def test_data_values_missing_returns_false(self):
        """data dict present but no 'values' key at all."""
        spec = _minimal_valid_spec()
        spec["data"] = {}  # no 'values' key
        assert _validate_vega_lite_spec(spec) is False

    def test_data_values_not_a_list_returns_false(self):
        """data.values exists but is a string, not a list."""
        spec = _minimal_valid_spec()
        spec["data"]["values"] = "not-a-list"
        assert _validate_vega_lite_spec(spec) is False

    def test_mark_string_returns_true(self):
        """mark as a plain string (e.g. 'bar') is valid."""
        spec = _minimal_valid_spec(mark="bar")
        assert _validate_vega_lite_spec(spec) is True

    @pytest.mark.parametrize("mark_type", ["bar", "line", "point", "area", "circle"])
    def test_mark_string_variants_return_true(self, mark_type):
        spec = _minimal_valid_spec(mark=mark_type)
        assert _validate_vega_lite_spec(spec) is True

    def test_mark_dict_with_type_returns_true(self):
        """mark as a dict containing 'type' is valid."""
        spec = _minimal_valid_spec(mark={"type": "bar", "tooltip": True})
        assert _validate_vega_lite_spec(spec) is True

    def test_mark_dict_without_type_returns_false(self):
        """mark as a dict missing 'type' key is invalid."""
        spec = _minimal_valid_spec(mark={"tooltip": True})
        assert _validate_vega_lite_spec(spec) is False

    def test_mark_none_returns_false(self):
        spec = _minimal_valid_spec()
        spec["mark"] = None
        assert _validate_vega_lite_spec(spec) is False

    def test_mark_integer_returns_false(self):
        spec = _minimal_valid_spec()
        spec["mark"] = 42
        assert _validate_vega_lite_spec(spec) is False

    def test_spec_none_returns_false(self):
        assert _validate_vega_lite_spec(None) is False

    def test_spec_string_returns_false(self):
        assert _validate_vega_lite_spec("not-a-dict") is False

    def test_spec_list_returns_false(self):
        assert _validate_vega_lite_spec([]) is False

    def test_empty_encoding_dict_returns_false(self):
        """encoding must be a non-empty dict."""
        spec = _minimal_valid_spec()
        spec["encoding"] = {}
        assert _validate_vega_lite_spec(spec) is False

    def test_encoding_not_a_dict_returns_false(self):
        spec = _minimal_valid_spec()
        spec["encoding"] = "invalid"
        assert _validate_vega_lite_spec(spec) is False

    def test_multiple_data_rows_accepted(self):
        """Spec with many data rows must still pass validation."""
        rows = [{"player": f"P{i}", "score": i * 10} for i in range(15)]
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": rows},
            "mark": {"type": "bar", "tooltip": True},
            "encoding": {
                "y": {"field": "player", "type": "nominal"},
                "x": {"field": "score", "type": "quantitative"},
            },
        }
        assert _validate_vega_lite_spec(spec) is True


# ---------------------------------------------------------------------------
# _build_fallback_spec
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildFallbackSpec:
    """Deterministic in-process Vega-Lite spec builder."""

    def test_empty_data_rows_returns_none(self):
        result = _build_fallback_spec([], _sample_intent())
        assert result is None

    def test_bar_chart_encoding_structure(self):
        """bar: y=nominal (x_field), x=quantitative (y_field) — horizontal bars."""
        spec = _build_fallback_spec(_sample_data_rows(), _sample_intent("bar"))
        assert spec is not None
        enc = spec["encoding"]
        # Horizontal bar: category on y-axis (nominal), value on x-axis (quantitative)
        assert enc["y"]["field"] == "batsman"
        assert enc["y"]["type"] == "nominal"
        assert enc["x"]["field"] == "runs"
        assert enc["x"]["type"] == "quantitative"

    def test_line_chart_encoding_structure(self):
        """line: x=ordinal (x_field), y=quantitative (y_field)."""
        intent = _sample_intent("line")
        intent["x_field"] = "year"
        intent["y_field"] = "wins"
        data = [{"year": str(y), "wins": w} for y, w in [(2019, 10), (2020, 8), (2021, 12)]]
        spec = _build_fallback_spec(data, intent)
        assert spec is not None
        enc = spec["encoding"]
        assert enc["x"]["field"] == "year"
        assert enc["x"]["type"] == "ordinal"
        assert enc["y"]["field"] == "wins"
        assert enc["y"]["type"] == "quantitative"

    def test_point_chart_encoding_structure(self):
        """point/scatter: both x and y are quantitative."""
        intent = _sample_intent("point")
        intent["x_field"] = "economy"
        intent["y_field"] = "wickets"
        data = [{"economy": 7.2, "wickets": 20}, {"economy": 6.8, "wickets": 25}]
        spec = _build_fallback_spec(data, intent)
        assert spec is not None
        enc = spec["encoding"]
        assert enc["x"]["type"] == "quantitative"
        assert enc["y"]["type"] == "quantitative"

    def test_unknown_chart_type_defaults_to_bar(self):
        """Any unrecognised chart_type must fall back to bar."""
        intent = _sample_intent("donut")  # unsupported
        spec = _build_fallback_spec(_sample_data_rows(), intent)
        assert spec is not None
        # mark.type must have been normalised to "bar"
        assert spec["mark"]["type"] == "bar"
        # bar encoding: nominal y
        assert spec["encoding"]["y"]["type"] == "nominal"

    def test_result_passes_validate_vega_lite_spec(self):
        """Every spec built by _build_fallback_spec must pass validation."""
        for chart_type in ("bar", "line", "point"):
            intent = _sample_intent(chart_type)
            spec = _build_fallback_spec(_sample_data_rows(5), intent)
            assert spec is not None
            assert _validate_vega_lite_spec(spec), (
                f"Fallback spec for chart_type={chart_type!r} failed validation"
            )

    def test_data_rows_capped_at_20(self):
        """data.values in the returned spec must contain at most 20 entries."""
        large_data = [{"batsman": f"P{i}", "runs": i} for i in range(35)]
        spec = _build_fallback_spec(large_data, _sample_intent())
        assert spec is not None
        assert len(spec["data"]["values"]) == 20

    def test_schema_url_is_vega_lite_v5(self):
        spec = _build_fallback_spec(_sample_data_rows(), _sample_intent())
        assert spec["$schema"] == "https://vega.github.io/schema/vega-lite/v5.json"

    def test_title_is_set_from_intent(self):
        intent = _sample_intent()
        intent["title"] = "IPL 2023 Top Scorers"
        spec = _build_fallback_spec(_sample_data_rows(), intent)
        assert spec["title"] == "IPL 2023 Top Scorers"

    def test_x_label_falls_back_to_x_field_when_empty(self):
        """If x_label is empty string, axis title should use x_field as label."""
        intent = _sample_intent()
        intent["x_label"] = ""
        spec = _build_fallback_spec(_sample_data_rows(), intent)
        assert spec is not None
        # x-axis title for bar is the y-field axis (horizontal bar orientation)
        # The code: x_label = intent.get("x_label", "") or x_field
        # For bar: enc["y"]["axis"]["title"] == x_label (resolved from x_field)
        assert spec["encoding"]["y"]["axis"]["title"] == intent["x_field"]

    def test_y_label_falls_back_to_y_field_when_empty(self):
        """If y_label is empty string, axis title should use y_field as label."""
        intent = _sample_intent()
        intent["y_label"] = ""
        spec = _build_fallback_spec(_sample_data_rows(), intent)
        assert spec is not None
        # For bar: enc["x"]["axis"]["title"] == y_label (resolved from y_field)
        assert spec["encoding"]["x"]["axis"]["title"] == intent["y_field"]

    def test_mark_has_tooltip_true(self):
        """The mark object must include tooltip: True for interactive charts."""
        spec = _build_fallback_spec(_sample_data_rows(), _sample_intent())
        assert spec["mark"]["tooltip"] is True

    def test_width_and_height_set(self):
        spec = _build_fallback_spec(_sample_data_rows(), _sample_intent())
        assert "width" in spec
        assert "height" in spec
        assert isinstance(spec["width"], int)
        assert isinstance(spec["height"], int)


# ---------------------------------------------------------------------------
# generate_chart_spec — fallback path end-to-end
# ---------------------------------------------------------------------------

def _make_intent_chain_for_generate(intent_dict: dict):
    """
    Build the mock chain tree for _INTENT_PROMPT so generate_chart_spec
    can complete the intent-extraction step without a real LLM.
    """
    raw_json = json.dumps(intent_dict)
    final_chain = MagicMock()
    final_chain.ainvoke = AsyncMock(return_value=raw_json)

    intermediate = MagicMock()
    intermediate.__or__ = MagicMock(return_value=final_chain)

    mock_prompt = MagicMock()
    mock_prompt.__or__ = MagicMock(return_value=intermediate)
    return mock_prompt


@pytest.mark.unit
class TestGenerateChartSpecFallbackPath:
    """
    End-to-end tests for the MCP → validate → fallback decision logic inside
    generate_chart_spec().

    All LLM and MCP calls are mocked. The tests verify which code path wins
    based on what the MCP call returns.
    """

    _default_intent = {
        "chart_type": "bar",
        "x_field": "batsman",
        "y_field": "runs",
        "x_label": "Batsman",
        "y_label": "Runs",
        "title": "Top Scorers",
    }
    _result_str = "[('V Kohli', 6624), ('S Dhawan', 5784), ('RG Sharma', 5611)]"

    @pytest.mark.asyncio
    async def test_mcp_none_uses_fallback_spec(self):
        """
        When _call_mcp_generate_chart returns None (MCP unreachable),
        the in-process fallback renderer must produce the spec.
        """
        mock_prompt = _make_intent_chain_for_generate(self._default_intent)

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt), \
             patch("app.viz_agent._call_mcp_generate_chart", new_callable=AsyncMock) as mock_mcp:
            mock_mcp.return_value = None

            spec = await generate_chart_spec(
                question="Show a bar chart of top run scorers",
                result=self._result_str,
                llm=MagicMock(),
            )

        assert spec is not None
        assert _validate_vega_lite_spec(spec), "Fallback spec must be valid"
        # Confirm MCP was called (attempted) and failed gracefully
        mock_mcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_invalid_spec_uses_fallback(self):
        """
        When MCP returns a spec that fails _validate_vega_lite_spec, the
        fallback renderer must be used instead.
        """
        # Deliberately broken spec: missing encoding
        broken_spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": [{"batsman": "V Kohli", "runs": 6624}]},
            "mark": "bar",
            # encoding intentionally missing
        }
        mock_prompt = _make_intent_chain_for_generate(self._default_intent)

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt), \
             patch("app.viz_agent._call_mcp_generate_chart", new_callable=AsyncMock) as mock_mcp:
            mock_mcp.return_value = broken_spec

            spec = await generate_chart_spec(
                question="Draw a bar chart of top scorers",
                result=self._result_str,
                llm=MagicMock(),
            )

        assert spec is not None
        # Result must be the fallback spec, which is always valid
        assert _validate_vega_lite_spec(spec)
        # The broken MCP spec must NOT be the returned value
        assert "encoding" in spec

    @pytest.mark.asyncio
    async def test_mcp_valid_spec_returned_as_is(self):
        """
        When MCP returns a valid spec, generate_chart_spec must return it
        without modification. The fallback renderer must NOT be invoked.
        """
        mcp_spec = _minimal_valid_spec()
        mock_prompt = _make_intent_chain_for_generate(self._default_intent)

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt), \
             patch("app.viz_agent._call_mcp_generate_chart", new_callable=AsyncMock) as mock_mcp, \
             patch("app.viz_agent._build_fallback_spec") as mock_fallback:
            mock_mcp.return_value = mcp_spec

            spec = await generate_chart_spec(
                question="Give me a bar chart of top run scorers",
                result=self._result_str,
                llm=MagicMock(),
            )

        # Must return the MCP spec unchanged
        assert spec is mcp_spec
        # Fallback must NOT have been called
        mock_fallback.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_mcp_and_fallback_fail_returns_none(self):
        """
        When MCP returns None AND data_rows is empty (fallback also fails),
        generate_chart_spec must return None — never raise.
        """
        # Intent extraction succeeds but with unusual field names that produce
        # no parseable rows. Use an empty result string directly.
        mock_prompt = _make_intent_chain_for_generate(self._default_intent)

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt), \
             patch("app.viz_agent._call_mcp_generate_chart", new_callable=AsyncMock) as mock_mcp:
            mock_mcp.return_value = None

            spec = await generate_chart_spec(
                question="Show me a chart",
                result="[]",   # empty result → _parse_result_to_rows returns []
                llm=MagicMock(),
            )

        # Both paths failed — must return None, not raise
        assert spec is None

    @pytest.mark.asyncio
    async def test_mcp_valid_spec_fallback_not_called(self):
        """
        Complementary to test_mcp_valid_spec_returned_as_is — use a spy on
        _build_fallback_spec to confirm it is skipped when MCP succeeds.
        """
        mcp_spec = _minimal_valid_spec()
        mock_prompt = _make_intent_chain_for_generate(self._default_intent)

        fallback_was_called = False

        def spy_fallback(data_rows, intent):
            nonlocal fallback_was_called
            fallback_was_called = True
            return None

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt), \
             patch("app.viz_agent._call_mcp_generate_chart", new_callable=AsyncMock) as mock_mcp, \
             patch("app.viz_agent._build_fallback_spec", side_effect=spy_fallback):
            mock_mcp.return_value = mcp_spec

            await generate_chart_spec(
                question="Show a chart of top scorers",
                result=self._result_str,
                llm=MagicMock(),
            )

        assert not fallback_was_called, (
            "_build_fallback_spec must not be called when MCP returns a valid spec"
        )
