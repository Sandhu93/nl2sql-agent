"""
Unit tests for the circuit breaker in agent.py (Phase 11).

Tests the in-process state machine:
  - _is_circuit_open()
  - _circuit_record_success()
  - _circuit_record_failure()
  - _llm_invoke() raises LLMCircuitOpenError when circuit is open

The circuit breaker uses module-level globals (_circuit_failures,
_circuit_open_until). Each test resets these via the `reset_circuit` fixture
so tests are fully isolated from each other.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agent as agent_module
from app.agent import (
    LLMCircuitOpenError,
    _is_circuit_open,
    _circuit_record_success,
    _circuit_record_failure,
    _llm_invoke,
)


# ---------------------------------------------------------------------------
# Fixture: reset circuit breaker state between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_circuit():
    """Reset module-level circuit breaker state before every test."""
    agent_module._circuit_failures = 0
    agent_module._circuit_open_until = 0.0
    yield
    # Clean up after test too
    agent_module._circuit_failures = 0
    agent_module._circuit_open_until = 0.0


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCircuitInitialState:

    def test_circuit_closed_initially(self):
        assert _is_circuit_open() is False

    def test_failure_counter_zero_initially(self):
        assert agent_module._circuit_failures == 0


# ---------------------------------------------------------------------------
# _circuit_record_success
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCircuitRecordSuccess:

    def test_success_resets_failure_counter(self):
        agent_module._circuit_failures = 3
        _circuit_record_success()
        assert agent_module._circuit_failures == 0

    def test_success_from_zero_stays_zero(self):
        _circuit_record_success()
        assert agent_module._circuit_failures == 0

    def test_success_does_not_close_already_open_circuit(self):
        """Success resets the counter but doesn't retroactively close an open circuit."""
        agent_module._circuit_open_until = time.time() + 60
        _circuit_record_success()
        assert agent_module._circuit_failures == 0
        # The open_until timestamp is unchanged by success alone
        assert agent_module._circuit_open_until > time.time()


# ---------------------------------------------------------------------------
# _circuit_record_failure
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCircuitRecordFailure:

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

        # open_until should be approximately now + cooldown
        assert agent_module._circuit_open_until >= before + cooldown - 1
        assert agent_module._circuit_open_until <= before + cooldown + 2

    def test_circuit_stays_closed_below_threshold(self):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        for _ in range(threshold - 1):
            _circuit_record_failure()
        assert _is_circuit_open() is False


# ---------------------------------------------------------------------------
# _is_circuit_open
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsCircuitOpen:

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
# _llm_invoke — circuit breaker enforcement
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLlmInvokeCircuitBreaker:

    @pytest.mark.asyncio
    async def test_raises_when_circuit_open(self):
        """_llm_invoke must raise LLMCircuitOpenError immediately when circuit is open."""
        agent_module._circuit_open_until = time.time() + 60

        mock_chain = AsyncMock()
        mock_chain.ainvoke = AsyncMock(return_value="result")

        with pytest.raises(LLMCircuitOpenError):
            await _llm_invoke(mock_chain, {"question": "test"})

        # Chain must NOT have been called
        mock_chain.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_succeeds_when_circuit_closed(self):
        """_llm_invoke must call the chain and return its result when circuit is closed."""
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value="SELECT 1")

        result = await _llm_invoke(mock_chain, {"question": "test"})
        assert result == "SELECT 1"

    @pytest.mark.asyncio
    async def test_successful_invoke_resets_failure_counter(self):
        agent_module._circuit_failures = 2
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value="ok")

        await _llm_invoke(mock_chain, {})

        assert agent_module._circuit_failures == 0

    @pytest.mark.asyncio
    async def test_failed_invoke_increments_failure_counter(self):
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=RuntimeError("OpenAI down"))

        with pytest.raises(RuntimeError):
            await _llm_invoke(mock_chain, {})

        assert agent_module._circuit_failures == 1

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold_failures(self):
        threshold = agent_module.settings.llm_circuit_failure_threshold
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))

        for _ in range(threshold):
            with pytest.raises(RuntimeError):
                await _llm_invoke(mock_chain, {})

        assert _is_circuit_open() is True

    @pytest.mark.asyncio
    async def test_llm_circuit_open_error_not_double_counted(self):
        """LLMCircuitOpenError re-raised from chain must not increment failure counter."""
        agent_module._circuit_open_until = time.time() + 60

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=LLMCircuitOpenError("already open"))

        with pytest.raises(LLMCircuitOpenError):
            await _llm_invoke(mock_chain, {})

        # Counter must not be incremented — it was already at threshold
        assert agent_module._circuit_failures == 0
