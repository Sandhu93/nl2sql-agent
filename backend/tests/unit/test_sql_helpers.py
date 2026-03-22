"""
Unit tests for sql_helpers.py.

All functions tested here are pure (no DB, no LLM) — no mocks needed.
Covers: _clean_sql, _is_sql_error, validate_sql, detect_semantic_sql_issue.
"""

import pytest

from app.sql_helpers import (
    _clean_sql,
    _is_sql_error,
    validate_sql,
    detect_semantic_sql_issue,
)


# ---------------------------------------------------------------------------
# _clean_sql — extracts pure SQL from LLM output
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCleanSql:

    # --- Markdown fence stripping ---

    def test_strips_sql_code_fence(self):
        raw = "```sql\nSELECT * FROM matches\n```"
        assert _clean_sql(raw) == "SELECT * FROM matches"

    def test_strips_plain_code_fence(self):
        raw = "```\nSELECT * FROM matches\n```"
        assert _clean_sql(raw) == "SELECT * FROM matches"

    def test_strips_sql_fence_case_insensitive(self):
        raw = "```SQL\nSELECT 1\n```"
        assert _clean_sql(raw) == "SELECT 1"

    # --- Prefix stripping ---

    def test_strips_sqlquery_prefix(self):
        raw = "SQLQuery: SELECT * FROM matches"
        assert _clean_sql(raw).startswith("SELECT")

    def test_strips_sql_colon_prefix(self):
        raw = "SQL: SELECT count(*) FROM deliveries"
        assert _clean_sql(raw).startswith("SELECT")

    def test_strips_sql_query_space_prefix(self):
        raw = "SQL Query: SELECT 1"
        assert _clean_sql(raw).startswith("SELECT")

    def test_prefix_stripping_case_insensitive(self):
        raw = "sqlquery: SELECT 1"
        assert _clean_sql(raw).startswith("SELECT")

    # --- Prose before SQL ---

    def test_strips_prose_before_sql(self):
        raw = "Here is the SQL query for your question:\nSELECT * FROM matches"
        result = _clean_sql(raw)
        assert result.startswith("SELECT")
        assert "Here is" not in result

    # --- Already clean SQL ---

    def test_already_clean_sql_unchanged(self):
        sql = "SELECT batsman, SUM(batsman_runs) FROM deliveries GROUP BY batsman"
        assert _clean_sql(sql) == sql

    def test_cte_query_passes_through(self):
        sql = "WITH cte AS (SELECT 1) SELECT * FROM cte"
        assert _clean_sql(sql) == sql

    # --- Multiple code blocks ---

    def test_multiple_code_blocks_joined(self):
        raw = (
            "```sql\nSELECT 1\n```\n"
            "Some explanation.\n"
            "```sql\nSELECT 2\n```"
        )
        result = _clean_sql(raw)
        assert "SELECT 1" in result
        assert "SELECT 2" in result

    def test_empty_string_returns_empty(self):
        assert _clean_sql("") == ""

    # --- Whitespace handling ---

    def test_leading_trailing_whitespace_stripped(self):
        raw = "  ```sql\n  SELECT 1  \n```  "
        result = _clean_sql(raw)
        assert result == "SELECT 1"


# ---------------------------------------------------------------------------
# _is_sql_error — detects QuerySQLDataBaseTool error strings
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsSqlError:

    @pytest.mark.parametrize("error_string", [
        "Error: column 'foo' does not exist",
        "Error: relation 'bar' does not exist",
        "Error: syntax error at or near SELECT",
        "Error: operator does not exist",
    ])
    def test_error_prefix_returns_true(self, error_string):
        assert _is_sql_error(error_string) is True

    @pytest.mark.parametrize("non_error", [
        "[('V Kohli', 6624), ('S Dhawan', 5784)]",
        "[(1169,)]",
        "No results found",
        "",
        "SELECT * FROM matches",
        "  Error with leading space",  # only "Error:" at start counts
    ])
    def test_non_error_returns_false(self, non_error):
        assert _is_sql_error(non_error) is False

    def test_error_with_leading_whitespace(self):
        """Leading whitespace is stripped before the check."""
        assert _is_sql_error("   Error: something failed") is True


# ---------------------------------------------------------------------------
# validate_sql — blocks non-SELECT and dangerous SQL
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestValidateSql:

    # --- Allowed queries ---

    @pytest.mark.parametrize("allowed_sql", [
        "SELECT * FROM matches",
        "SELECT count(*) FROM deliveries",
        "select batsman, sum(batsman_runs) from deliveries group by batsman",  # lowercase
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
        "WITH\ncte AS (\n  SELECT 1\n)\nSELECT * FROM cte",  # multiline CTE
        "  SELECT 1  ",  # leading/trailing whitespace
    ])
    def test_allowed_sql_does_not_raise(self, allowed_sql):
        validate_sql(allowed_sql)  # must not raise

    # --- Non-SELECT must be blocked ---

    @pytest.mark.parametrize("non_select", [
        "INSERT INTO matches VALUES (1, 2)",
        "UPDATE matches SET winner = 'test'",
        "DELETE FROM deliveries",
        "DROP TABLE matches",
        "ALTER TABLE matches ADD COLUMN foo INT",
        "TRUNCATE deliveries",
        "CREATE TABLE foo (id INT)",
        "GRANT ALL ON matches TO user",
        "REVOKE ALL ON matches FROM user",
        "COPY matches TO '/tmp/out.csv'",
        "EXECUTE some_procedure()",
    ])
    def test_non_select_raises_value_error(self, non_select):
        with pytest.raises(ValueError, match="read-only"):
            validate_sql(non_select)

    # --- Forbidden keywords inside otherwise-valid SQL ---

    @pytest.mark.parametrize("dangerous_sql", [
        "SELECT * FROM matches; DROP TABLE matches",
        "SELECT * FROM matches WHERE 1=1; DELETE FROM deliveries",
        "SELECT * FROM pg_tables",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_stat_activity",
    ])
    def test_dangerous_keywords_raise(self, dangerous_sql):
        with pytest.raises(ValueError):
            validate_sql(dangerous_sql)

    # --- Case insensitivity ---

    def test_drop_lowercase_blocked(self):
        with pytest.raises(ValueError):
            validate_sql("SELECT 1; drop table matches")

    def test_delete_mixed_case_blocked(self):
        with pytest.raises(ValueError):
            validate_sql("SELECT 1; DeLeTe FROM deliveries")

    def test_pg_tables_lowercase_blocked(self):
        with pytest.raises(ValueError):
            validate_sql("SELECT * FROM pg_tables")

    # --- Error message is safe ---

    def test_error_message_is_user_safe(self):
        with pytest.raises(ValueError) as exc_info:
            validate_sql("DROP TABLE matches")
        assert "read-only" in str(exc_info.value)
        # Must not include actual SQL in error message
        assert "DROP TABLE" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# detect_semantic_sql_issue — grain-mismatch detection
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDetectSemanticSqlIssue:

    # --- Issues that must be detected ---

    def test_batsman_runs_eq_impossible_value(self):
        """batsman_runs = 119 is a grain mismatch (per-ball max is 6)."""
        sql = "SELECT batsman FROM deliveries WHERE batsman_runs = 119"
        result = detect_semantic_sql_issue(sql)
        assert result is not None
        assert "per-ball" in result or "batsman_runs" in result

    def test_batsman_runs_eq_50_is_impossible(self):
        sql = "SELECT batsman FROM deliveries WHERE batsman_runs = 50"
        result = detect_semantic_sql_issue(sql)
        assert result is not None

    def test_batsman_runs_eq_100_is_impossible(self):
        sql = "SELECT batsman FROM deliveries WHERE batsman_runs = 100"
        result = detect_semantic_sql_issue(sql)
        assert result is not None

    def test_batsman_runs_gt_7_is_impossible(self):
        """batsman_runs > 7: literal 7 > 6, so this is a grain mismatch."""
        sql = "SELECT batsman FROM deliveries WHERE batsman_runs > 7"
        result = detect_semantic_sql_issue(sql)
        assert result is not None

    def test_batsman_runs_gte_7_is_impossible(self):
        sql = "SELECT batsman FROM deliveries WHERE batsman_runs >= 7"
        result = detect_semantic_sql_issue(sql)
        assert result is not None

    def test_batsman_runs_lt_with_high_value(self):
        """batsman_runs < 50 — per-ball run is always < 50, so this is a grain mismatch."""
        sql = "SELECT batsman FROM deliveries WHERE batsman_runs <= 50"
        result = detect_semantic_sql_issue(sql)
        assert result is not None

    # --- Bug #20 regression: the exact real-world case ---

    def test_bug_20_regression_grain_mismatch(self):
        """
        Regression test for Bug #20: LLM used WHERE batsman_runs = 119
        instead of aggregating innings totals.
        """
        sql = (
            "SELECT batsman FROM deliveries "
            "WHERE batsman_runs = 119 AND dismissal_kind IS NULL"
        )
        result = detect_semantic_sql_issue(sql)
        assert result is not None, (
            "Bug #20 regression: batsman_runs = 119 grain mismatch not detected"
        )

    # --- Valid SQL — must return None ---

    @pytest.mark.parametrize("valid_sql", [
        "SELECT batsman FROM deliveries WHERE batsman_runs = 4",
        "SELECT batsman FROM deliveries WHERE batsman_runs = 6",
        "SELECT batsman FROM deliveries WHERE batsman_runs = 0",
        "SELECT batsman FROM deliveries WHERE batsman_runs >= 4",
        "SELECT batsman FROM deliveries WHERE batsman_runs > 0",
        # Aggregate query — SUM not filtered at ball level
        "SELECT batsman, SUM(batsman_runs) FROM deliveries GROUP BY batsman HAVING SUM(batsman_runs) > 50",
        # No batsman_runs filter at all
        "SELECT winner, COUNT(*) FROM matches GROUP BY winner ORDER BY 2 DESC",
    ])
    def test_valid_sql_returns_none(self, valid_sql):
        result = detect_semantic_sql_issue(valid_sql)
        assert result is None, f"False positive detected for: {valid_sql!r}"

    def test_case_insensitive_detection(self):
        """Detection must work regardless of SQL keyword casing."""
        sql = "SELECT batsman FROM deliveries WHERE BATSMAN_RUNS = 119"
        result = detect_semantic_sql_issue(sql)
        assert result is not None
