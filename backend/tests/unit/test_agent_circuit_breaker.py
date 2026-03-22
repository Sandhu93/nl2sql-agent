"""
Unit tests for the Redis-backed circuit breaker in agent.py (Phase 11 / Phase 17).

Covers both execution paths:
  - In-process fallback (Redis unavailable) — module-level _circuit_failures / _circuit_open_until
  - Redis path (Redis available) — INCR / SET EX / EXISTS / DEL operations

Tests:
  _is_circuit_open()
  _circuit_record_success()
  _circuit_record_failure()
  _llm_invoke() raises LLMCircuitOpenError when circuit is open
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.agent as agent_module
from app.agent import (
    LLMCircuitOpenError,
    _CIRCUIT_FAILURES_KEY,
    _CIRCUIT_OPEN_KEY,
    _is_circuit_open,
    _circuit_record_success,
    _circuit_record_failure,
    _llm_invoke,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    """Restrict anyio async tests to asyncio backend (trio is not installed)."""
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_circuit():
    """
    Reset ALL circuit breaker state before and after each test.
    Forces Redis unavailable so in-process fallback tests are deterministic.
    """
    orig_redis_available = agent_module._redis_available
    orig_redis_client = agent_module._redis_client

    agent_module._redis_available = False
    agent_module._redis_client = None
    agent_module._circuit_failures = 0
    agent_module._circuit_open_until = 0.0

    yield

    agent_module._redis_available = orig_redis_available
    agent_module._redis_client = orig_redis_client
    agent_module._circuit_failures = 0
    agent_module._circuit_open_until = 0.0


@pytest.fixture()
def mock_redis():
    """Provide a mock Redis client with Redis available flag set."""
    client = MagicMock()
    agent_module._redis_available = True
    agent_module._redis_client = client
    yield client
    agent_module._redis_available = False
    agent_module._redis_client = None


# ---------------------------------------------------------------------------
# IN-PROCESS FALLBACK PATH
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCircuitInitialState:

    def test_circuit_closed_initially(self):
        assert _is_circuit_open() is False

    def test_failure_counter_zero_initially(self):
        assert agent_module._circuit_failures == 0


@pytest.mark.unit
class TestCircuitRecordSuccessFallback:

    def test_success_resets_failure_counter(self):
        agent_module._circuit_failures = 3
        _circuit_record_success()
        assert agent_module._circuit_failures == 0

    def test_success_from_zero_stays_zero(self):
        _circuit_record_success()
        assert agent_module._circuit_failures == 0

    def test_success_closes_open_circuit(self):
        """Success resets the counter AND closes an open circuit (half-open → closed)."""
        agent_module._circuit_open_until = time.time() + 60
        agent_module._circuit_failures = 5
        _circuit_record_success()
        assert agent_module._circuit_failures == 0
        assert agent_module._circuit_open_until == 0.0
        assert _is_circuit_open() is False


@pytest.mark.unit
class TestCircuitRecordFailureFallback:

    def test_failure_increments_counter(self):
        _circuit_record_failure()
        assert agent_module._circuit_failures == 1

    def test_multiple_failures_accumulate(self):
        for _ in range(3):
            _circuit_record_failure()
        assert agent_module._circuit_failures == 3

    def test_circuit_opens_at_threshold(self):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        for _ in range(threshold):
            _circuit_record_failure()
        assert _is_circuit_open() is True

    def test_circuit_opens_with_correct_cooldown(self):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        cooldown = agent_module.settings.llm_circuit_cooldown_seconds
        before = time.time()
        for _ in range(threshold):
            _circuit_record_failure()
        assert agent_module._circuit_open_until >= before + cooldown - 1
        assert agent_module._circuit_open_until <= before + cooldown + 2

    def test_circuit_stays_closed_below_threshold(self):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        for _ in range(threshold - 1):
            _circuit_record_failure()
        assert _is_circuit_open() is False


@pytest.mark.unit
class TestIsCircuitOpenFallback:

    def test_closed_when_open_until_in_past(self):
        agent_module._circuit_open_until = time.time() - 10
        assert _is_circuit_open() is False

    def test_open_when_open_until_in_future(self):
        agent_module._circuit_open_until = time.time() + 60
        assert _is_circuit_open() is True

    def test_closed_when_open_until_is_zero(self):
        agent_module._circuit_open_until = 0.0
        assert _is_circuit_open() is False


# ---------------------------------------------------------------------------
# REDIS PATH
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsCircuitOpenRedis:

    def test_closed_when_open_key_absent(self, mock_redis):
        mock_redis.exists.return_value = 0
        assert _is_circuit_open() is False
        mock_redis.exists.assert_called_once_with(_CIRCUIT_OPEN_KEY)

    def test_open_when_open_key_present(self, mock_redis):
        mock_redis.exists.return_value = 1
        assert _is_circuit_open() is True

    def test_falls_back_to_in_process_on_redis_error(self, mock_redis):
        mock_redis.exists.side_effect = Exception("connection lost")
        agent_module._circuit_open_until = time.time() + 60
        assert _is_circuit_open() is True  # in-process fallback


@pytest.mark.unit
class TestCircuitRecordSuccessRedis:

    def test_calls_getdel_and_delete(self, mock_redis):
        mock_redis.getdel.return_value = None
        _circuit_record_success()
        mock_redis.getdel.assert_called_once_with(_CIRCUIT_FAILURES_KEY)
        mock_redis.delete.assert_called_once_with(_CIRCUIT_OPEN_KEY)

    def test_logs_on_non_zero_previous_failures(self, mock_redis):
        mock_redis.getdel.return_value = b"3"
        _circuit_record_success()  # should not raise

    def test_falls_back_to_in_process_on_redis_error(self, mock_redis):
        mock_redis.getdel.side_effect = Exception("timeout")
        agent_module._circuit_failures = 3
        _circuit_record_success()
        assert agent_module._circuit_failures == 0
        assert agent_module._circuit_open_until == 0.0


@pytest.mark.unit
class TestCircuitRecordFailureRedis:

    def test_calls_incr(self, mock_redis):
        mock_redis.incr.return_value = 1
        _circuit_record_failure()
        mock_redis.incr.assert_called_once_with(_CIRCUIT_FAILURES_KEY)

    def test_does_not_set_open_key_below_threshold(self, mock_redis):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        mock_redis.incr.return_value = threshold - 1
        _circuit_record_failure()
        mock_redis.set.assert_not_called()

    def test_sets_open_key_at_threshold(self, mock_redis):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        cooldown = agent_module.settings.llm_circuit_cooldown_seconds
        mock_redis.incr.return_value = threshold
        _circuit_record_failure()
        mock_redis.set.assert_called_once_with(_CIRCUIT_OPEN_KEY, 1, ex=cooldown)

    def test_falls_back_to_in_process_on_redis_error(self, mock_redis):
        mock_redis.incr.side_effect = Exception("timeout")
        _circuit_record_failure()
        assert agent_module._circuit_failures == 1


# ---------------------------------------------------------------------------
# _llm_invoke — circuit breaker enforcement (both paths)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLlmInvokeCircuitBreaker:

    @pytest.mark.anyio
    async def test_raises_when_circuit_open_fallback(self):
        agent_module._circuit_open_until = time.time() + 60
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value="result")
        with pytest.raises(LLMCircuitOpenError):
            await _llm_invoke(mock_chain, {"question": "test"})
        mock_chain.ainvoke.assert_not_called()

    @pytest.mark.anyio
    async def test_raises_when_circuit_open_redis(self, mock_redis):
        mock_redis.exists.return_value = 1
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value="result")
        with pytest.raises(LLMCircuitOpenError):
            await _llm_invoke(mock_chain, {"question": "test"})
        mock_chain.ainvoke.assert_not_called()

    @pytest.mark.anyio
    async def test_succeeds_when_circuit_closed(self):
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value="SELECT 1")
        result = await _llm_invoke(mock_chain, {"question": "test"})
        assert result == "SELECT 1"

    @pytest.mark.anyio
    async def test_successful_invoke_resets_failure_counter(self):
        agent_module._circuit_failures = 2
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value="ok")
        await _llm_invoke(mock_chain, {})
        assert agent_module._circuit_failures == 0

    @pytest.mark.anyio
    async def test_failed_invoke_increments_failure_counter(self):
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=RuntimeError("OpenAI down"))
        with pytest.raises(RuntimeError):
            await _llm_invoke(mock_chain, {})
        assert agent_module._circuit_failures == 1

    @pytest.mark.anyio
    async def test_circuit_opens_after_threshold_failures(self):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        for _ in range(threshold):
            with pytest.raises(RuntimeError):
                await _llm_invoke(mock_chain, {})
        assert _is_circuit_open() is True

    @pytest.mark.anyio
    async def test_llm_circuit_open_error_not_double_counted(self):
        """LLMCircuitOpenError re-raised from chain must not increment failure counter."""
        agent_module._circuit_open_until = time.time() + 60
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=LLMCircuitOpenError("already open"))
        with pytest.raises(LLMCircuitOpenError):
            await _llm_invoke(mock_chain, {})
        assert agent_module._circuit_failures == 0
