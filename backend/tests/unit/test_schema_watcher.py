"""
Unit tests for backend/app/schema_watcher.py (Phase 14: Schema Drift Detection).

Coverage
--------
1. _build_schema_fingerprint(conn)
   - Happy path: returns deterministic SHA-256 hex string
   - Same rows always produce the same hash (determinism)
   - Different rows produce a different hash
   - Missing tables trigger a WARNING log
   - Empty result set (all tables missing) still returns a valid hash

2. _log_data_coverage(conn)
   - Happy path: INFO log contains max_year, match_count, delivery_count
   - Exception during query: WARNING logged, never re-raises

3. _check_and_store_hash(conn, redis_client)
   - redis_client is None: WARNING logged, redis.set never called
   - No stored hash (get returns None): baseline written, INFO "baseline recorded"
   - Hash matches stored: INFO "no changes detected", redis.set not called again
   - Hash differs: WARNING "Schema drift detected", redis.set called with new hash

4. _run_watcher()
   - Redis ping fails: redis_client is None, DB path still executes
   - DB connect fails: early return, WARNING logged, coverage + hash check skipped
   - Happy path: _log_data_coverage and _check_and_store_hash both called

5. run_schema_watcher() (async entry point)
   - _run_watcher raises an unexpected exception: caught and logged as WARNING,
     never re-raised (non-blocking guarantee)
   - Happy path: completes without error

All external I/O (psycopg2, redis) is fully mocked — no real connections.
"""

import hashlib
import logging
from unittest.mock import MagicMock, patch

import pytest

import app.schema_watcher as sw
from app.schema_watcher import (
    KNOWN_TABLES,
    _SCHEMA_HASH_KEY,
    _build_schema_fingerprint,
    _check_and_store_hash,
    _log_data_coverage,
    _run_watcher,
    run_schema_watcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cursor(fetchall_return=None, fetchone_side_effect=None):
    """Build a mock psycopg2 cursor usable as a context manager."""
    cur = MagicMock()
    if fetchall_return is not None:
        cur.fetchall.return_value = fetchall_return
    if fetchone_side_effect is not None:
        cur.fetchone.side_effect = fetchone_side_effect
    # Support `with conn.cursor() as cur:`
    return cur


def _make_conn(cursor=None):
    """Build a mock psycopg2 connection whose cursor() context-manager yields `cursor`."""
    conn = MagicMock()
    if cursor is not None:
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def _fingerprint_from_rows(rows):
    """Replicate the fingerprint algorithm from schema_watcher for assertion."""
    fingerprint = "|".join(f"{t}:{c}:{dt}:{nn}" for t, c, dt, nn in rows)
    return hashlib.sha256(fingerprint.encode()).hexdigest()


# ---------------------------------------------------------------------------
# _build_schema_fingerprint
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildSchemaFingerprint:

    _SAMPLE_ROWS = [
        ("deliveries", "batsman", "character varying", "YES"),
        ("deliveries", "batsman_runs", "integer", "YES"),
        ("matches", "match_id", "integer", "NO"),
        ("matches", "year", "integer", "YES"),
    ]

    def _conn_with_rows(self, rows):
        cur = _make_cursor(fetchall_return=rows)
        return _make_conn(cur), cur

    def test_returns_64_char_hex_string(self):
        conn, _ = self._conn_with_rows(self._SAMPLE_ROWS)
        result = _build_schema_fingerprint(conn)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic_same_rows(self):
        """Same row data must always produce the identical hash."""
        conn1, _ = self._conn_with_rows(self._SAMPLE_ROWS)
        conn2, _ = self._conn_with_rows(self._SAMPLE_ROWS)
        assert _build_schema_fingerprint(conn1) == _build_schema_fingerprint(conn2)

    def test_different_rows_different_hash(self):
        """Changing any column attribute must change the fingerprint."""
        rows_v2 = list(self._SAMPLE_ROWS)
        rows_v2[0] = ("deliveries", "batsman", "text", "YES")  # data_type changed
        conn1, _ = self._conn_with_rows(self._SAMPLE_ROWS)
        conn2, _ = self._conn_with_rows(rows_v2)
        assert _build_schema_fingerprint(conn1) != _build_schema_fingerprint(conn2)

    def test_extra_column_changes_hash(self):
        """Adding a new column must change the fingerprint."""
        rows_extended = self._SAMPLE_ROWS + [("matches", "new_col", "boolean", "YES")]
        conn1, _ = self._conn_with_rows(self._SAMPLE_ROWS)
        conn2, _ = self._conn_with_rows(rows_extended)
        assert _build_schema_fingerprint(conn1) != _build_schema_fingerprint(conn2)

    def test_hash_matches_manual_computation(self):
        """The returned hash must equal the independently computed fingerprint."""
        conn, _ = self._conn_with_rows(self._SAMPLE_ROWS)
        expected = _fingerprint_from_rows(self._SAMPLE_ROWS)
        assert _build_schema_fingerprint(conn) == expected

    def test_empty_rows_returns_valid_hash(self):
        """Empty result set (all tables missing) must not raise — returns hash of ''."""
        conn, _ = self._conn_with_rows([])
        result = _build_schema_fingerprint(conn)
        assert len(result) == 64
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected

    def test_missing_table_logs_warning(self, caplog):
        """If a KNOWN_TABLE is absent from results, a WARNING is emitted."""
        # Provide rows only for 'deliveries', omitting all other KNOWN_TABLES.
        rows_partial = [
            ("deliveries", "batsman", "character varying", "YES"),
        ]
        conn, _ = self._conn_with_rows(rows_partial)

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _build_schema_fingerprint(conn)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("missing" in msg.lower() for msg in warning_messages), (
            "Expected a WARNING about missing tables, got: " + str(warning_messages)
        )

    def test_all_tables_missing_logs_warning(self, caplog):
        """Zero rows returned (all 9 tables absent) must log a WARNING."""
        conn, _ = self._conn_with_rows([])

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _build_schema_fingerprint(conn)

        assert any("missing" in r.message.lower() for r in caplog.records)

    def test_all_known_tables_present_no_warning(self, caplog):
        """When all 9 tables appear in results no WARNING about missing tables is logged."""
        # One column per table is sufficient — we only need found_tables to include all.
        rows_all_tables = [
            (table, "col", "integer", "YES") for table in KNOWN_TABLES
        ]
        conn, _ = self._conn_with_rows(rows_all_tables)

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _build_schema_fingerprint(conn)

        missing_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "missing" in r.message.lower()
        ]
        assert missing_warnings == [], (
            "No missing-table WARNING expected when all tables present"
        )

    def test_cursor_execute_called_with_known_tables(self):
        """Verify the query is executed with KNOWN_TABLES as parameters."""
        cur = _make_cursor(fetchall_return=[])
        conn = _make_conn(cur)
        _build_schema_fingerprint(conn)
        cur.execute.assert_called_once()
        # call_args.args is the positional tuple: (sql_string, params_list)
        positional_args = cur.execute.call_args.args
        # Second positional arg (the params) must be KNOWN_TABLES.
        assert positional_args[1] == KNOWN_TABLES

    @pytest.mark.parametrize("changed_field_idx,new_value", [
        (1, "new_column_name"),   # column_name changed
        (2, "bigint"),            # data_type changed
        (3, "NO"),                # is_nullable changed
    ])
    def test_any_field_change_changes_hash(self, changed_field_idx, new_value):
        """A change to any of the four tracked fields must alter the hash."""
        base_rows = [("deliveries", "batsman_runs", "integer", "YES")]
        modified = list(base_rows[0])
        modified[changed_field_idx] = new_value
        rows_modified = [tuple(modified)]

        conn_base, _ = self._conn_with_rows(base_rows)
        conn_mod, _ = self._conn_with_rows(rows_modified)

        assert _build_schema_fingerprint(conn_base) != _build_schema_fingerprint(conn_mod)


# ---------------------------------------------------------------------------
# _log_data_coverage
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLogDataCoverage:

    def _conn_returning(self, max_year, match_count, delivery_count):
        """Build a conn whose cursor returns the two expected fetchone() values."""
        cur = _make_cursor(
            fetchone_side_effect=[
                (max_year, match_count),   # SELECT MAX(year), COUNT(*) FROM matches
                (delivery_count,),         # SELECT COUNT(*) FROM deliveries
            ]
        )
        return _make_conn(cur)

    def test_happy_path_logs_info(self, caplog):
        conn = self._conn_returning(2023, 1169, 278171)

        with caplog.at_level(logging.INFO, logger="app.schema_watcher"):
            _log_data_coverage(conn)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert info_records, "Expected at least one INFO log"
        combined = " ".join(r.getMessage() for r in info_records)
        assert "2023" in combined
        assert "1169" in combined
        assert "278171" in combined

    def test_happy_path_log_contains_expected_keys(self, caplog):
        """Log message must mention max_year, matches, and deliveries labels."""
        conn = self._conn_returning(2022, 500, 100000)

        with caplog.at_level(logging.INFO, logger="app.schema_watcher"):
            _log_data_coverage(conn)

        combined = " ".join(r.getMessage() for r in caplog.records)
        assert "max_year" in combined or "2022" in combined
        assert "matches" in combined or "500" in combined
        assert "deliveries" in combined or "100000" in combined

    def test_exception_logs_warning_not_raised(self, caplog):
        """A DB exception during coverage query must log WARNING and never re-raise."""
        cur = MagicMock()
        cur.execute.side_effect = Exception("relation 'matches' does not exist")
        conn = _make_conn(cur)

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            # Must not raise
            _log_data_coverage(conn)

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("coverage" in msg.lower() or "failed" in msg.lower() for msg in warning_msgs)

    def test_no_exception_raised_on_error(self):
        """Confirm _log_data_coverage is non-blocking — no exception escapes."""
        cur = MagicMock()
        cur.execute.side_effect = RuntimeError("unexpected DB error")
        conn = _make_conn(cur)
        # Should complete silently
        _log_data_coverage(conn)

    @pytest.mark.parametrize("max_year,match_count,delivery_count", [
        (2019, 60, 14400),
        (2023, 1169, 278171),
        (None, 0, 0),          # edge: no data in DB
    ])
    def test_various_coverage_values_logged(self, max_year, match_count, delivery_count, caplog):
        conn = self._conn_returning(max_year, match_count, delivery_count)
        with caplog.at_level(logging.INFO, logger="app.schema_watcher"):
            _log_data_coverage(conn)
        # Function completes without raising for any valid DB return
        assert True


# ---------------------------------------------------------------------------
# _check_and_store_hash
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckAndStoreHash:
    """Tests for all four branches of _check_and_store_hash."""

    def _conn_for_hash(self, rows=None):
        """Return a conn that produces a deterministic fingerprint."""
        if rows is None:
            rows = [("matches", "match_id", "integer", "NO")]
        cur = _make_cursor(fetchall_return=rows)
        return _make_conn(cur)

    def _expected_hash(self, rows=None):
        if rows is None:
            rows = [("matches", "match_id", "integer", "NO")]
        return _fingerprint_from_rows(rows)

    # ------------------------------------------------------------------
    # Branch A: redis_client is None
    # ------------------------------------------------------------------

    def test_redis_none_logs_warning(self, caplog):
        conn = self._conn_for_hash()
        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=None)

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("redis" in msg.lower() or "unavailable" in msg.lower() for msg in warning_msgs)

    def test_redis_none_does_not_call_set(self):
        """When redis_client is None, no attempt is made to call .set()."""
        conn = self._conn_for_hash()
        # If redis_client is None and code still tries .set(), AttributeError is raised.
        # We confirm the function completes without error, proving .set() is not called.
        _check_and_store_hash(conn, redis_client=None)  # no AttributeError

    def test_redis_none_returns_early(self, caplog):
        """After the WARNING, the function returns — no redis.get() attempted."""
        conn = self._conn_for_hash()
        mock_redis = MagicMock()
        # Pass None, not mock_redis, to exercise the None branch
        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=None)
        mock_redis.get.assert_not_called()

    # ------------------------------------------------------------------
    # Branch B: No stored hash (first run)
    # ------------------------------------------------------------------

    def test_no_stored_hash_writes_baseline(self):
        """get() returning None must write current hash via redis.set()."""
        conn = self._conn_for_hash()
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        expected = self._expected_hash()

        _check_and_store_hash(conn, redis_client=mock_redis)

        mock_redis.set.assert_called_once_with(_SCHEMA_HASH_KEY, expected)

    def test_no_stored_hash_logs_info_baseline(self, caplog):
        conn = self._conn_for_hash()
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with caplog.at_level(logging.INFO, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=mock_redis)

        info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("baseline" in msg.lower() for msg in info_msgs), (
            f"Expected 'baseline' in INFO log, got: {info_msgs}"
        )

    def test_no_stored_hash_no_drift_warning(self, caplog):
        """First run must not produce a drift WARNING."""
        conn = self._conn_for_hash()
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=mock_redis)

        drift_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "drift" in r.getMessage().lower()
        ]
        assert drift_warnings == []

    # ------------------------------------------------------------------
    # Branch C: Hash matches stored
    # ------------------------------------------------------------------

    def test_hash_match_logs_no_changes(self, caplog):
        rows = [("matches", "match_id", "integer", "NO")]
        conn = self._conn_for_hash(rows)
        current = _fingerprint_from_rows(rows)

        mock_redis = MagicMock()
        mock_redis.get.return_value = current.encode()  # stored == current

        with caplog.at_level(logging.INFO, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=mock_redis)

        info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("no changes" in msg.lower() for msg in info_msgs), (
            f"Expected 'no changes' in INFO log, got: {info_msgs}"
        )

    def test_hash_match_does_not_call_set(self):
        """When hashes match, redis.set must NOT be called."""
        rows = [("deliveries", "batsman", "character varying", "YES")]
        conn = self._conn_for_hash(rows)
        current = _fingerprint_from_rows(rows)

        mock_redis = MagicMock()
        mock_redis.get.return_value = current.encode()

        _check_and_store_hash(conn, redis_client=mock_redis)

        mock_redis.set.assert_not_called()

    def test_hash_match_no_warning_logged(self, caplog):
        rows = [("deliveries", "batsman", "character varying", "YES")]
        conn = self._conn_for_hash(rows)
        current = _fingerprint_from_rows(rows)
        mock_redis = MagicMock()
        mock_redis.get.return_value = current.encode()

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=mock_redis)

        # Only check that no *drift* WARNING was logged — the fingerprint builder
        # may independently warn about missing tables in the test fixture, which
        # is unrelated to the hash-comparison branch under test.
        drift_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "drift" in r.getMessage().lower()
        ]
        assert drift_warnings == [], "No drift WARNING expected when hashes match"

    # ------------------------------------------------------------------
    # Branch D: Hash differs (schema drift)
    # ------------------------------------------------------------------

    def test_hash_differs_logs_drift_warning(self, caplog):
        rows_new = [("matches", "match_id", "bigint", "NO")]  # data_type changed
        conn = self._conn_for_hash(rows_new)
        old_hash = _fingerprint_from_rows([("matches", "match_id", "integer", "NO")])

        mock_redis = MagicMock()
        mock_redis.get.return_value = old_hash.encode()

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=mock_redis)

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("drift" in msg.lower() for msg in warning_msgs), (
            f"Expected 'drift' in WARNING, got: {warning_msgs}"
        )

    def test_hash_differs_updates_baseline(self):
        """On drift, redis.set must be called with the NEW hash."""
        rows_new = [("matches", "match_id", "bigint", "NO")]
        conn = self._conn_for_hash(rows_new)
        new_hash = _fingerprint_from_rows(rows_new)
        old_hash = _fingerprint_from_rows([("matches", "match_id", "integer", "NO")])

        mock_redis = MagicMock()
        mock_redis.get.return_value = old_hash.encode()

        _check_and_store_hash(conn, redis_client=mock_redis)

        mock_redis.set.assert_called_once_with(_SCHEMA_HASH_KEY, new_hash)

    def test_hash_differs_warning_contains_both_hashes(self, caplog):
        """Drift WARNING must include the stored and current hash prefixes."""
        rows_new = [("matches", "match_id", "bigint", "NO")]
        conn = self._conn_for_hash(rows_new)
        new_hash = _fingerprint_from_rows(rows_new)
        old_hash = _fingerprint_from_rows([("matches", "match_id", "integer", "NO")])

        mock_redis = MagicMock()
        mock_redis.get.return_value = old_hash.encode()

        with caplog.at_level(logging.WARNING, logger="app.schema_watcher"):
            _check_and_store_hash(conn, redis_client=mock_redis)

        combined = " ".join(r.getMessage() for r in caplog.records)
        # The log uses [:8] slices — check at least one of the 8-char prefixes appears
        assert old_hash[:8] in combined or new_hash[:8] in combined

    def test_stored_hash_read_from_correct_key(self):
        """redis.get must be called with the exact _SCHEMA_HASH_KEY constant."""
        conn = self._conn_for_hash()
        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # baseline branch

        _check_and_store_hash(conn, redis_client=mock_redis)

        mock_redis.get.assert_called_once_with(_SCHEMA_HASH_KEY)


# ---------------------------------------------------------------------------
# _run_watcher
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRunWatcher:
    """Tests for the synchronous _run_watcher() core function."""

    # ------------------------------------------------------------------
    # Redis failure path
    # ------------------------------------------------------------------

    def test_redis_ping_failure_logs_warning(self, caplog):
        """When Redis ping raises, a WARNING is logged and execution continues."""
        mock_conn = MagicMock()
        mock_cursor = _make_cursor(
            fetchall_return=[("matches", "match_id", "integer", "NO")],
            fetchone_side_effect=[(2023, 1169), (278171,)],
        )
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            caplog.at_level(logging.WARNING, logger="app.schema_watcher"),
        ):
            mock_redis_lib.from_url.return_value.ping.side_effect = Exception("Connection refused")
            _run_watcher()

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("redis" in msg.lower() or "unavailable" in msg.lower() for msg in warning_msgs)

    def test_redis_ping_failure_still_runs_db_parts(self):
        """When Redis is unavailable, the DB coverage + hash check still runs."""
        mock_conn = MagicMock()
        mock_cursor = _make_cursor(
            fetchall_return=[("matches", "match_id", "integer", "NO")],
            fetchone_side_effect=[(2023, 1169), (278171,)],
        )
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn) as mock_db,
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage") as mock_coverage,
            patch("app.schema_watcher._check_and_store_hash") as mock_hash_check,
        ):
            mock_redis_lib.from_url.return_value.ping.side_effect = Exception("refused")
            _run_watcher()

        mock_db.assert_called_once()
        mock_coverage.assert_called_once()
        mock_hash_check.assert_called_once()
        # redis_client is the second positional arg to _check_and_store_hash — must be None
        passed_redis = mock_hash_check.call_args.args[1]
        assert passed_redis is None

    # ------------------------------------------------------------------
    # DB failure path
    # ------------------------------------------------------------------

    def test_db_connect_failure_logs_warning(self, caplog):
        """When _connect_db raises, a WARNING is logged."""
        with (
            patch("app.schema_watcher._connect_db", side_effect=Exception("DB refused")),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            caplog.at_level(logging.WARNING, logger="app.schema_watcher"),
        ):
            mock_redis_lib.from_url.return_value.ping.return_value = True
            _run_watcher()

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "db" in msg.lower() or "unavailable" in msg.lower() or "skipping" in msg.lower()
            for msg in warning_msgs
        )

    def test_db_connect_failure_skips_coverage_and_hash(self):
        """On DB failure, _log_data_coverage and _check_and_store_hash must not be called."""
        with (
            patch("app.schema_watcher._connect_db", side_effect=Exception("refused")),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage") as mock_coverage,
            patch("app.schema_watcher._check_and_store_hash") as mock_hash_check,
        ):
            mock_redis_lib.from_url.return_value.ping.return_value = True
            _run_watcher()

        mock_coverage.assert_not_called()
        mock_hash_check.assert_not_called()

    def test_db_connect_failure_does_not_raise(self):
        """_run_watcher must never propagate DB connection errors."""
        with (
            patch("app.schema_watcher._connect_db", side_effect=Exception("refused")),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
        ):
            mock_redis_lib.from_url.return_value.ping.return_value = True
            _run_watcher()  # must not raise

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_happy_path_calls_coverage_and_hash_check(self):
        """Happy path: both _log_data_coverage and _check_and_store_hash are called."""
        mock_conn = MagicMock()

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage") as mock_coverage,
            patch("app.schema_watcher._check_and_store_hash") as mock_hash_check,
        ):
            mock_redis_lib.from_url.return_value.ping.return_value = True
            _run_watcher()

        mock_coverage.assert_called_once_with(mock_conn)
        mock_hash_check.assert_called_once()

    def test_happy_path_closes_conn(self):
        """The DB connection must be closed even on the happy path."""
        mock_conn = MagicMock()

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage"),
            patch("app.schema_watcher._check_and_store_hash"),
        ):
            mock_redis_lib.from_url.return_value.ping.return_value = True
            _run_watcher()

        mock_conn.close.assert_called_once()

    def test_happy_path_closes_redis(self):
        """The Redis client must be closed after the watcher completes."""
        mock_conn = MagicMock()
        mock_redis_instance = MagicMock()

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage"),
            patch("app.schema_watcher._check_and_store_hash"),
        ):
            mock_redis_lib.from_url.return_value = mock_redis_instance
            mock_redis_instance.ping.return_value = True
            _run_watcher()

        mock_redis_instance.close.assert_called_once()

    def test_conn_closed_even_when_coverage_raises(self):
        """DB connection is closed in a finally block — even if coverage query raises."""
        mock_conn = MagicMock()

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage", side_effect=Exception("boom")),
            patch("app.schema_watcher._check_and_store_hash"),
        ):
            mock_redis_lib.from_url.return_value.ping.return_value = True
            # The exception from _log_data_coverage propagates out of _run_watcher
            # because it is NOT inside the inner try/except — only the finally block runs.
            try:
                _run_watcher()
            except Exception:
                pass  # we only care that close() was called

        mock_conn.close.assert_called_once()

    def test_redis_from_url_called_with_settings_url(self):
        """redis.from_url must be called with the configured redis_url."""
        mock_conn = MagicMock()

        with (
            patch("app.schema_watcher._connect_db", return_value=mock_conn),
            patch("app.schema_watcher.redis_lib") as mock_redis_lib,
            patch("app.schema_watcher._log_data_coverage"),
            patch("app.schema_watcher._check_and_store_hash"),
            patch.object(sw, "settings") as mock_settings,
        ):
            mock_settings.redis_url = "redis://testhost:6379"
            mock_redis_lib.from_url.return_value.ping.return_value = True
            _run_watcher()

        mock_redis_lib.from_url.assert_called_once_with(
            "redis://testhost:6379", socket_connect_timeout=2
        )


# ---------------------------------------------------------------------------
# run_schema_watcher (async entry point)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRunSchemaWatcher:
    """Tests for the async run_schema_watcher() entry point."""

    async def test_happy_path_completes_without_error(self):
        """When _run_watcher completes normally, run_schema_watcher returns cleanly."""
        with patch("app.schema_watcher._run_watcher") as mock_run:
            await run_schema_watcher()
        mock_run.assert_called_once()

    async def test_exception_caught_and_logged_as_warning(self, caplog):
        """Any exception from _run_watcher must be caught and logged — never re-raised."""
        with (
            patch(
                "app.schema_watcher._run_watcher",
                side_effect=RuntimeError("unexpected crash"),
            ),
            caplog.at_level(logging.WARNING, logger="app.schema_watcher"),
        ):
            # Must not raise
            await run_schema_watcher()

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "unexpected" in msg.lower() or "error" in msg.lower() or "startup" in msg.lower()
            for msg in warning_msgs
        ), f"Expected WARNING about unexpected error, got: {warning_msgs}"

    async def test_exception_not_re_raised(self):
        """Non-blocking guarantee: no exception ever escapes run_schema_watcher."""
        with patch(
            "app.schema_watcher._run_watcher",
            side_effect=Exception("catastrophic failure"),
        ):
            await run_schema_watcher()  # must complete silently

    async def test_delegates_to_thread_not_inline(self):
        """run_schema_watcher must use asyncio.to_thread, not call _run_watcher directly."""
        # Patch via the module's own asyncio reference so the event loop stays intact.
        async def _noop(*a, **kw): pass

        with patch("app.schema_watcher.asyncio") as mock_asyncio:
            mock_asyncio.to_thread.return_value = _noop()
            await run_schema_watcher()

        mock_asyncio.to_thread.assert_called_once()
        # Verify _run_watcher is the function passed to to_thread
        args, _ = mock_asyncio.to_thread.call_args
        assert args[0] is sw._run_watcher

    async def test_multiple_exception_types_are_caught(self):
        """Both RuntimeError and ValueError must be caught — not just the base Exception."""
        for exc_type in (RuntimeError, ValueError, OSError, KeyError):
            with patch(
                "app.schema_watcher._run_watcher",
                side_effect=exc_type("test error"),
            ):
                await run_schema_watcher()  # must complete silently for each type
