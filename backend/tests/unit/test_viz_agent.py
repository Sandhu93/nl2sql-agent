"""
Unit tests for viz_agent.py.

Covers:
  - wants_visualization(): regex intent detection
  - _parse_result_to_rows(): SQL result string → list of dicts
  - _extract_chart_intent(): invoke_fn routing and fallback on error

Bug #30 regression: _parse_result_to_rows() must handle Decimal('15.00')
constructor strings produced by psycopg2 for NUMERIC/DECIMAL columns.

No real LLM or MCP calls — all external dependencies are mocked.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.viz_agent import wants_visualization, _parse_result_to_rows, _extract_chart_intent


# ---------------------------------------------------------------------------
# wants_visualization — intent detection regex
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestWantsVisualization:

    @pytest.mark.parametrize("question", [
        "Show me a bar chart of top run scorers",
        "Plot the top 5 batsmen by runs",
        "Give me a graph of wins by team",
        "Visualize the economy rates",
        "Visualise wickets by bowler",
        "draw a chart of sixes by year",
        "display a chart of match results",
        "Can you show me a line chart?",
        "Create a pie chart of toss decisions",
        "Show a scatter plot of runs vs wickets",
        "Show a histogram of scores",
        "Show me a visualization of the data",
        "Give me a visualisation please",
    ])
    def test_viz_intent_detected(self, question):
        assert wants_visualization(question) is True, (
            f"Expected viz intent for: {question!r}"
        )

    @pytest.mark.parametrize("question", [
        "How many runs did Virat Kohli score in 2019?",
        "Who took the most wickets?",
        "Which team won the most matches?",
        "What is the highest score in IPL?",
        "Show me the top 5 run scorers",   # "show" without "chart"
        "Tell me about Rohit Sharma's performance",
        "How many sixes were hit in 2016?",
        "What was the economy rate for fast bowlers?",
    ])
    def test_no_viz_intent_for_data_questions(self, question):
        assert wants_visualization(question) is False, (
            f"False positive viz intent for: {question!r}"
        )

    def test_case_insensitive(self):
        assert wants_visualization("SHOW ME A BAR CHART") is True
        assert wants_visualization("bar Chart please") is True


# ---------------------------------------------------------------------------
# _parse_result_to_rows — SQL result string → list of dicts
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseResultToRows:

    # --- Bug #30 regression: Decimal constructor strings ---

    def test_bug_30_decimal_single_value(self):
        """
        Regression test for Bug #30.

        psycopg2 renders NUMERIC columns as Decimal('15.00') in result strings.
        ast.literal_eval cannot parse constructor calls and raised an exception,
        which was silently caught, returning [] and skipping all charts that
        involved computed decimals (batting average, economy, ROUND()).
        """
        result = "[('V Kohli', Decimal('52.50')), ('RG Sharma', Decimal('48.30'))]"
        rows = _parse_result_to_rows(result, "batsman", "avg_runs")

        assert len(rows) == 2, "Bug #30 regression: Decimal rows returned empty list"
        assert rows[0]["batsman"] == "V Kohli"
        assert abs(rows[0]["avg_runs"] - 52.50) < 0.01
        assert rows[1]["batsman"] == "RG Sharma"
        assert abs(rows[1]["avg_runs"] - 48.30) < 0.01

    def test_multiple_decimals_in_same_row(self):
        result = "[('2019', Decimal('7.45'), Decimal('8.12'))]"
        rows = _parse_result_to_rows(result, "year", "economy")
        assert len(rows) == 1
        assert rows[0]["year"] == "2019"
        assert abs(rows[0]["economy"] - 7.45) < 0.01

    def test_decimal_with_no_decimal_places(self):
        result = "[('MI', Decimal('120'))]"
        rows = _parse_result_to_rows(result, "team", "wins")
        assert len(rows) == 1
        assert rows[0]["team"] == "MI"

    # --- Normal integer tuples ---

    def test_integer_tuples(self):
        result = "[('V Kohli', 6624), ('S Dhawan', 5784), ('RG Sharma', 5611)]"
        rows = _parse_result_to_rows(result, "batsman", "runs")
        assert len(rows) == 3
        assert rows[0] == {"batsman": "V Kohli", "runs": 6624}
        assert rows[1] == {"batsman": "S Dhawan", "runs": 5784}

    def test_string_and_float(self):
        result = "[('Bumrah', 7.25), ('Shami', 7.98)]"
        rows = _parse_result_to_rows(result, "bowler", "economy")
        assert rows[0]["bowler"] == "Bumrah"
        assert rows[0]["economy"] == 7.25

    # --- Year (integer) as x-axis ---

    def test_integer_x_axis(self):
        result = "[(2019, 450), (2020, 380), (2021, 510)]"
        rows = _parse_result_to_rows(result, "year", "sixes")
        assert len(rows) == 3
        # Integer x values remain numeric (not stringified)
        assert rows[0]["year"] == 2019

    # --- Field name mapping ---

    def test_custom_field_names_used(self):
        result = "[('MI', 100)]"
        rows = _parse_result_to_rows(result, "team_name", "total_wins")
        assert "team_name" in rows[0]
        assert "total_wins" in rows[0]

    # --- Row count capped at 20 ---

    def test_capped_at_20_rows(self):
        # Generate 30 rows
        data = ", ".join(f"('Team{i}', {i})" for i in range(30))
        result = f"[{data}]"
        rows = _parse_result_to_rows(result, "team", "value")
        assert len(rows) == 20

    # --- Rows with fewer than 2 columns are skipped ---

    def test_single_column_rows_skipped(self):
        result = "[('V Kohli',), ('RG Sharma',)]"
        rows = _parse_result_to_rows(result, "batsman", "runs")
        assert len(rows) == 0

    # --- Malformed / unparseable input ---

    def test_malformed_result_returns_empty_list(self):
        result = "this is not valid python"
        rows = _parse_result_to_rows(result, "x", "y")
        assert rows == []

    def test_empty_string_returns_empty_list(self):
        rows = _parse_result_to_rows("", "x", "y")
        assert rows == []

    def test_empty_result_list(self):
        rows = _parse_result_to_rows("[]", "x", "y")
        assert rows == []

    # --- Mixed Decimal and non-Decimal rows ---

    def test_mixed_decimal_and_int_rows(self):
        result = "[('Kohli', Decimal('52.50')), ('Dhawan', 48)]"
        rows = _parse_result_to_rows(result, "batsman", "avg")
        assert len(rows) == 2
        assert abs(rows[0]["avg"] - 52.50) < 0.01
        assert rows[1]["avg"] == 48

    # --- Single-row result (not wrapped in a list) ---

    def test_single_tuple_not_in_list(self):
        result = "('V Kohli', 6624)"
        rows = _parse_result_to_rows(result, "batsman", "runs")
        # Single tuple gets wrapped in a list
        assert len(rows) == 1
        assert rows[0]["batsman"] == "V Kohli"


# ---------------------------------------------------------------------------
# _extract_chart_intent — invoke_fn routing
# ---------------------------------------------------------------------------

def _make_intent_chain(return_value=None, side_effect=None):
    """
    Build a mock that simulates the (_INTENT_PROMPT | llm | StrOutputParser()) chain.

    _extract_chart_intent builds: (_INTENT_PROMPT | llm | StrOutputParser()).ainvoke(...)
    We patch _INTENT_PROMPT so the pipe-chain collapses to a single mock whose
    ainvoke is controlled by us.
    """
    import json as _json

    final_chain = MagicMock()
    if side_effect is not None:
        final_chain.ainvoke = AsyncMock(side_effect=side_effect)
    else:
        final_chain.ainvoke = AsyncMock(return_value=return_value)

    intermediate = MagicMock()
    intermediate.__or__ = MagicMock(return_value=final_chain)

    mock_prompt = MagicMock()
    mock_prompt.__or__ = MagicMock(return_value=intermediate)

    return mock_prompt


@pytest.mark.unit
class TestExtractChartIntentInvokeFn:
    """
    Tests for the invoke_fn parameter in _extract_chart_intent().

    invoke_fn is the semaphore/circuit-breaker hook passed down from
    generate_chart_spec. When provided it intercepts the LLM call; when
    absent the chain's own .ainvoke is used. Failures always fall back to
    safe defaults — never propagate.
    """

    @pytest.mark.asyncio
    async def test_invoke_fn_is_called_when_provided(self):
        """invoke_fn must be called instead of chain.ainvoke when provided."""
        import json

        calls: list[tuple] = []
        mock_response = json.dumps({
            "chart_type": "bar",
            "x_field": "batsman",
            "y_field": "total_runs",
            "x_label": "Batsman",
            "y_label": "Total Runs",
            "title": "Top Run Scorers",
        })

        async def recording_invoke_fn(chain, inputs):
            calls.append((chain, inputs))
            return mock_response

        mock_prompt = _make_intent_chain(return_value=mock_response)

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt):
            intent = await _extract_chart_intent(
                question="Show a bar chart of top run scorers",
                result_preview="[('V Kohli', 6624)]",
                llm=MagicMock(),
                invoke_fn=recording_invoke_fn,
            )

        assert len(calls) == 1, "invoke_fn must be called exactly once"
        # Confirm the extracted intent reflects the mocked LLM response
        assert intent["chart_type"] == "bar"
        assert intent["x_field"] == "batsman"
        assert intent["y_field"] == "total_runs"

    @pytest.mark.asyncio
    async def test_chain_ainvoke_used_when_invoke_fn_is_none(self):
        """When invoke_fn=None, chain.ainvoke must handle the LLM call directly."""
        import json

        external_calls = 0

        async def should_not_be_called(chain, inputs):
            nonlocal external_calls
            external_calls += 1
            return "{}"

        mock_response = json.dumps({
            "chart_type": "line",
            "x_field": "year",
            "y_field": "wins",
            "x_label": "Year",
            "y_label": "Wins",
            "title": "Wins Over Time",
        })
        mock_prompt = _make_intent_chain(return_value=mock_response)

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt):
            intent = await _extract_chart_intent(
                question="Plot wins per year as a line chart",
                result_preview="[(2019, 10), (2020, 8)]",
                llm=MagicMock(),
                invoke_fn=None,
            )

        assert external_calls == 0, "invoke_fn must not be called when it is None"
        assert intent["chart_type"] == "line"

    @pytest.mark.asyncio
    async def test_invoke_fn_error_falls_back_to_safe_defaults(self):
        """
        If invoke_fn raises, _extract_chart_intent must catch it and return the
        safe default intent dict — never re-raise.
        """
        async def failing_invoke_fn(chain, inputs):
            raise RuntimeError("Circuit breaker open")

        mock_prompt = _make_intent_chain(return_value="{}")

        with patch("app.viz_agent._INTENT_PROMPT", mock_prompt):
            intent = await _extract_chart_intent(
                question="Show a chart of wickets",
                result_preview="[('JJ Bumrah', 145)]",
                llm=MagicMock(),
                invoke_fn=failing_invoke_fn,
            )

        # Safe defaults must be returned
        assert intent["chart_type"] == "bar"
        assert intent["x_field"] == "category"
        assert intent["y_field"] == "value"

    @pytest.mark.asyncio
    async def test_invoke_fn_passed_through_from_generate_chart_spec(self):
        """
        generate_chart_spec must forward its invoke_fn argument down to
        _extract_chart_intent — not swallow it.
        """
        import json
        from app.viz_agent import generate_chart_spec

        intent_invoke_calls: list = []

        async def tracking_invoke_fn(chain, inputs):
            intent_invoke_calls.append(inputs)
            return json.dumps({
                "chart_type": "bar",
                "x_field": "batsman",
                "y_field": "runs",
                "x_label": "Batsman",
                "y_label": "Runs",
                "title": "Top Scorers",
            })

        with patch("app.viz_agent._call_mcp_generate_chart", new_callable=AsyncMock) as mock_mcp, \
             patch("app.viz_agent._INTENT_PROMPT", _make_intent_chain(return_value="{}")):
            # MCP returns None to force the fallback path; we only care that
            # intent extraction received invoke_fn
            mock_mcp.return_value = None

            await generate_chart_spec(
                question="Show a bar chart of top scorers",
                result="[('V Kohli', 6624), ('S Dhawan', 5784)]",
                llm=MagicMock(),
                invoke_fn=tracking_invoke_fn,
            )

        # tracking_invoke_fn must have been called during intent extraction
        assert len(intent_invoke_calls) >= 1, (
            "invoke_fn must be forwarded to _extract_chart_intent"
        )
