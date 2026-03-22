"""
Unit tests for viz_agent.py.

Covers:
  - wants_visualization(): regex intent detection
  - _parse_result_to_rows(): SQL result string → list of dicts

Bug #30 regression: _parse_result_to_rows() must handle Decimal('15.00')
constructor strings produced by psycopg2 for NUMERIC/DECIMAL columns.

No LLM or MCP calls — those are tested at integration level.
"""

import pytest

from app.viz_agent import wants_visualization, _parse_result_to_rows


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
