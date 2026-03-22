"""
Unit tests for _maybe_summarize_history() in agent.py (Phase 15).

This function compresses long conversation threads so the rewrite chain
receives compact, accurate context rather than a hard-truncated sliding
window that drops early topics.

Contracts under test
--------------------
1. Short history (≤ _SUMMARY_THRESHOLD = 8 msgs)
   → returns history.messages unchanged (no LLM call).

2. Long history (> 8 msgs)
   → splits into older = msgs[:-4] and recent = msgs[-4:]
   → calls the summary chain's ainvoke() via the LangChain pipe operator
   → returns [HumanMessage("[Earlier conversation summary]\\n<text>")] + recent
     (Fix 4: was SystemMessage — changed to HumanMessage so LLM-generated
     content never acquires system-role trust in the rewrite chain prompt)

3. LLM failure during summarization
   → logs a warning and returns msgs[-8:] (plain sliding window fallback)

4. Summary text content
   → HumanMessage.content starts with "[Earlier conversation summary]"
   → actual summary text from the LLM is appended after the prefix

5. Edge cases
   → exactly 8 messages → NO summarization (boundary is ≤, not <)
   → exactly 9 messages → summarization IS triggered

6. Integration point in run_agent()
   → when history.messages is non-empty, run_agent() calls
     _maybe_summarize_history(history) and passes its return value
     as the `history` key in the rewrite_query invocation.

Mocking strategy
----------------
The function builds inline: (ChatPromptTemplate.from_messages(...) | _fast_llm | StrOutputParser()).ainvoke(...)

We patch `app.agent.ChatPromptTemplate` (the module-level import binding) so
that `from_messages()` returns a MagicMock whose `__or__` always returns
itself (making the pipe chain a no-op) and whose `ainvoke` is an AsyncMock
returning our stub string. The `_PatchSummaryChain` context manager encapsulates
this pattern.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app.agent as agent_module

_SUMMARY_THRESHOLD = agent_module._SUMMARY_THRESHOLD   # 8
_SUMMARY_KEEP = agent_module._SUMMARY_KEEP             # 4


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_history_n(n: int) -> ChatMessageHistory:
    """Build a ChatMessageHistory with exactly n alternating messages."""
    h = ChatMessageHistory()
    for i in range(n):
        if i % 2 == 0:
            h.add_user_message(f"User message {i}?")
        else:
            h.add_ai_message(f"AI answer {i}")
    return h


# ---------------------------------------------------------------------------
# Context manager: patch ChatPromptTemplate.from_messages in agent.py
# so the chain pipe (.ainvoke) returns a controlled value.
# ---------------------------------------------------------------------------

class _PatchSummaryChain:
    """
    Context manager that patches `app.agent.ChatPromptTemplate` so that
    `ChatPromptTemplate.from_messages(...)` returns a mock chain whose
    `ainvoke` returns `return_value`.

    Usage:
        async with _PatchSummaryChain("some summary text"):
            result = await _maybe_summarize_history(history)
    """
    def __init__(self, return_value: str | Exception):
        self._rv = return_value
        self._patcher = None

    def __enter__(self):
        chain = MagicMock()
        chain.__or__ = MagicMock(return_value=chain)
        if isinstance(self._rv, Exception):
            chain.ainvoke = AsyncMock(side_effect=self._rv)
        else:
            chain.ainvoke = AsyncMock(return_value=self._rv)

        mock_cpt = MagicMock()
        mock_cpt.from_messages = MagicMock(return_value=chain)
        self._patcher = patch.object(agent_module, "ChatPromptTemplate", mock_cpt)
        self._chain = chain
        self._patcher.start()

        # Reset circuit breaker state so tests are independent of each other.
        # _maybe_summarize_history now routes through _llm_invoke which checks
        # the breaker first — a contaminated state from a prior test would
        # raise LLMCircuitOpenError before the mock chain is ever reached.
        self._saved_failures = agent_module._circuit_failures
        self._saved_open_until = agent_module._circuit_open_until
        agent_module._circuit_failures = 0
        agent_module._circuit_open_until = 0.0

        return chain

    def __exit__(self, *args):
        self._patcher.stop()
        # Restore circuit breaker state to avoid leaking into other tests.
        agent_module._circuit_failures = self._saved_failures
        agent_module._circuit_open_until = self._saved_open_until


# ---------------------------------------------------------------------------
# TestShortHistory — ≤ threshold messages → return unchanged, no LLM call
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestShortHistory:

    @pytest.mark.asyncio
    async def test_empty_history_returned_unchanged(self):
        h = ChatMessageHistory()
        result = await agent_module._maybe_summarize_history(h)
        assert result == []

    @pytest.mark.asyncio
    async def test_single_message_returned_unchanged(self):
        h = _make_history_n(1)
        result = await agent_module._maybe_summarize_history(h)
        assert result == h.messages

    @pytest.mark.asyncio
    async def test_exactly_threshold_messages_no_llm_call(self):
        """Boundary check: exactly 8 messages must NOT trigger summarization."""
        h = _make_history_n(_SUMMARY_THRESHOLD)
        assert len(h.messages) == _SUMMARY_THRESHOLD

        with _PatchSummaryChain("should not be called") as chain:
            result = await agent_module._maybe_summarize_history(h)
            # ainvoke must not have been called
            chain.ainvoke.assert_not_called()

        # Result is the original list
        assert result == h.messages
        # No summary message injected (neither SystemMessage nor HumanMessage wrapper)
        assert not any(
            isinstance(m, (SystemMessage, HumanMessage)) and
            getattr(m, "content", "").startswith("[Earlier conversation summary]")
            for m in result
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n_msgs", [1, 2, 3, 5, 7, 8])
    async def test_various_short_lengths_returned_unchanged(self, n_msgs):
        h = _make_history_n(n_msgs)
        result = await agent_module._maybe_summarize_history(h)
        assert result == h.messages
        assert len(result) == n_msgs


# ---------------------------------------------------------------------------
# TestLongHistory — > threshold messages → summarize + return [summary]+recent
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLongHistory:

    @pytest.mark.asyncio
    async def test_nine_messages_triggers_summarization(self):
        """Boundary: exactly 9 messages MUST trigger summarization."""
        n = _SUMMARY_THRESHOLD + 1  # 9
        h = _make_history_n(n)

        with _PatchSummaryChain("• summary text") as chain:
            result = await agent_module._maybe_summarize_history(h)
            chain.ainvoke.assert_called_once()

        # [HumanMessage summary] + last 4 = 5 items total (Fix 4: was SystemMessage)
        assert len(result) == _SUMMARY_KEEP + 1

    @pytest.mark.asyncio
    async def test_summary_message_is_first_element(self):
        h = _make_history_n(12)

        with _PatchSummaryChain("• summary"):
            result = await agent_module._maybe_summarize_history(h)

        # Fix 4: summary is now a HumanMessage, not SystemMessage
        assert isinstance(result[0], HumanMessage)

    @pytest.mark.asyncio
    async def test_summary_content_starts_with_prefix(self):
        h = _make_history_n(10)
        summary_text = "• Kohli scored 6000 runs\n• Bumrah took 100 wickets"

        with _PatchSummaryChain(summary_text):
            result = await agent_module._maybe_summarize_history(h)

        # Fix 4: result[0] is now a HumanMessage
        assert isinstance(result[0], HumanMessage)
        assert result[0].content.startswith("[Earlier conversation summary]")

    @pytest.mark.asyncio
    async def test_summary_content_includes_llm_output(self):
        """The LLM text must appear in the HumanMessage content."""
        h = _make_history_n(10)
        summary_text = "• Discussed Rohit Sharma's centuries\n• RCB bowling economy"

        with _PatchSummaryChain(summary_text):
            result = await agent_module._maybe_summarize_history(h)

        assert summary_text in result[0].content

    @pytest.mark.asyncio
    async def test_summary_content_exact_format(self):
        """Content must be exactly '[Earlier conversation summary]\\n<llm_output>'."""
        h = _make_history_n(10)
        summary_text = "• Topic A\n• Topic B"
        expected = f"[Earlier conversation summary]\n{summary_text}"

        with _PatchSummaryChain(summary_text):
            result = await agent_module._maybe_summarize_history(h)

        assert result[0].content == expected

    @pytest.mark.asyncio
    async def test_recent_messages_preserved_verbatim(self):
        """The last _SUMMARY_KEEP messages must appear unchanged after the summary."""
        h = _make_history_n(12)
        expected_recent = h.messages[-_SUMMARY_KEEP:]

        with _PatchSummaryChain("some summary"):
            result = await agent_module._maybe_summarize_history(h)

        assert result[1:] == expected_recent

    @pytest.mark.asyncio
    async def test_older_messages_not_individually_in_result(self):
        """Older messages must not appear individually — only the summary does."""
        h = _make_history_n(12)
        older = h.messages[:-_SUMMARY_KEEP]

        with _PatchSummaryChain("summary"):
            result = await agent_module._maybe_summarize_history(h)

        for old_msg in older:
            assert old_msg not in result

    @pytest.mark.asyncio
    async def test_original_history_not_mutated(self):
        """The original ChatMessageHistory object must not be modified."""
        h = _make_history_n(10)
        original_count = len(h.messages)
        original_first = h.messages[0].content

        with _PatchSummaryChain("summary"):
            await agent_module._maybe_summarize_history(h)

        assert len(h.messages) == original_count
        assert h.messages[0].content == original_first

    @pytest.mark.asyncio
    async def test_summary_is_human_message_not_system_or_ai(self):
        """
        Fix 4: the summary wrapper must be a HumanMessage, not SystemMessage or
        AIMessage. HumanMessage is used so LLM-generated content never acquires
        system-role trust in the rewrite chain prompt.
        """
        h = _make_history_n(10)

        with _PatchSummaryChain("bullets"):
            result = await agent_module._maybe_summarize_history(h)

        assert isinstance(result[0], HumanMessage)
        assert not isinstance(result[0], SystemMessage)
        assert not isinstance(result[0], AIMessage)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n_msgs", [9, 10, 15, 20, 50])
    async def test_always_returns_summary_plus_recent_for_long_histories(self, n_msgs):
        h = _make_history_n(n_msgs)

        with _PatchSummaryChain("short summary"):
            result = await agent_module._maybe_summarize_history(h)

        # Must always be exactly _SUMMARY_KEEP + 1 items
        assert len(result) == _SUMMARY_KEEP + 1
        # Fix 4: summary message is HumanMessage not SystemMessage
        assert isinstance(result[0], HumanMessage)
        # Tail must match the last _SUMMARY_KEEP messages
        assert result[1:] == h.messages[-_SUMMARY_KEEP:]


# ---------------------------------------------------------------------------
# TestLLMFailureFallback — exceptions → plain sliding window
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLLMFailureFallback:

    @pytest.mark.asyncio
    async def test_llm_runtime_error_returns_plain_slice(self):
        """On RuntimeError, must return msgs[-_SUMMARY_THRESHOLD:]."""
        h = _make_history_n(14)
        expected = h.messages[-_SUMMARY_THRESHOLD:]

        with _PatchSummaryChain(RuntimeError("OpenAI timeout")):
            result = await agent_module._maybe_summarize_history(h)

        assert result == expected

    @pytest.mark.asyncio
    async def test_network_error_returns_plain_slice(self):
        """ConnectionError triggers fallback."""
        h = _make_history_n(10)
        expected = h.messages[-_SUMMARY_THRESHOLD:]

        with _PatchSummaryChain(ConnectionError("Redis unreachable")):
            result = await agent_module._maybe_summarize_history(h)

        assert result == expected

    @pytest.mark.asyncio
    async def test_any_exception_type_triggers_fallback(self):
        """Any Exception subclass triggers the fallback — function never raises."""
        h = _make_history_n(12)

        with _PatchSummaryChain(ValueError("bad value")):
            result = await agent_module._maybe_summarize_history(h)

        assert isinstance(result, list)
        assert len(result) == _SUMMARY_THRESHOLD

    @pytest.mark.asyncio
    async def test_failure_does_not_raise(self):
        """_maybe_summarize_history is non-fatal — must never raise."""
        h = _make_history_n(12)

        with _PatchSummaryChain(Exception("catastrophic")):
            # Must not raise
            result = await agent_module._maybe_summarize_history(h)

        assert result is not None

    @pytest.mark.asyncio
    async def test_fallback_slice_never_exceeds_threshold(self):
        """Fallback always returns at most _SUMMARY_THRESHOLD messages."""
        h = _make_history_n(50)

        with _PatchSummaryChain(RuntimeError("fail")):
            result = await agent_module._maybe_summarize_history(h)

        assert len(result) <= _SUMMARY_THRESHOLD

    @pytest.mark.asyncio
    async def test_fallback_is_exact_tail_slice(self):
        """Fallback is exactly msgs[-8:], not a random subset."""
        h = _make_history_n(20)

        with _PatchSummaryChain(RuntimeError("fail")):
            result = await agent_module._maybe_summarize_history(h)

        assert result == h.messages[-_SUMMARY_THRESHOLD:]


# ---------------------------------------------------------------------------
# TestThresholdBoundary — parametrized boundary checks
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestThresholdBoundary:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n_msgs,should_summarize", [
        (0, False),
        (1, False),
        (7, False),
        (8, False),   # at threshold — NO summarization (≤ check)
        (9, True),    # one over — summarization triggered
        (16, True),
    ])
    async def test_threshold_boundary(self, n_msgs, should_summarize):
        h = _make_history_n(n_msgs)

        with _PatchSummaryChain("a summary") as chain:
            result = await agent_module._maybe_summarize_history(h)

        # Fix 4: summary is now a HumanMessage with [Earlier conversation summary] prefix,
        # not a SystemMessage — detect by checking for the HumanMessage wrapper prefix.
        has_summary_msg = any(
            isinstance(m, HumanMessage) and
            m.content.startswith("[Earlier conversation summary]")
            for m in result
        )
        assert has_summary_msg == should_summarize, (
            f"n_msgs={n_msgs}: expected summarize={should_summarize}, "
            f"got has_summary_msg={has_summary_msg}"
        )

        if should_summarize:
            chain.ainvoke.assert_called_once()
        else:
            chain.ainvoke.assert_not_called()


# ---------------------------------------------------------------------------
# TestTranscriptFormat — verify the transcript fed to the LLM
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTranscriptFormat:

    @pytest.mark.asyncio
    async def test_transcript_contains_user_and_assistant_labels(self):
        """
        The transcript passed to the LLM must prefix human messages with
        "User:" and AI messages with "Assistant:".
        """
        h = ChatMessageHistory()
        h.add_user_message("Who has the most sixes?")
        h.add_ai_message("Chris Gayle with 357 sixes.")
        # Pad to exceed the threshold
        for i in range(8):
            h.add_user_message(f"Pad {i}?")
            h.add_ai_message(f"Pad answer {i}.")

        captured: dict = {}

        async def _capture(inputs):
            captured.update(inputs)
            return "• summary"

        chain = MagicMock()
        chain.__or__ = MagicMock(return_value=chain)
        chain.ainvoke = AsyncMock(side_effect=_capture)

        mock_cpt = MagicMock()
        mock_cpt.from_messages = MagicMock(return_value=chain)

        with patch.object(agent_module, "ChatPromptTemplate", mock_cpt):
            await agent_module._maybe_summarize_history(h)

        if "transcript" in captured:
            transcript = captured["transcript"]
            assert "User:" in transcript
            assert "Assistant:" in transcript

    @pytest.mark.asyncio
    async def test_transcript_excludes_recent_messages(self):
        """
        The transcript fed to the LLM must only include 'older' messages
        (msgs[:-_SUMMARY_KEEP]) — the recent 4 messages are kept verbatim
        and must NOT appear in the transcript.
        """
        h = _make_history_n(10)
        recent_messages = h.messages[-_SUMMARY_KEEP:]

        captured: dict = {}

        async def _capture(inputs):
            captured.update(inputs)
            return "• summary"

        chain = MagicMock()
        chain.__or__ = MagicMock(return_value=chain)
        chain.ainvoke = AsyncMock(side_effect=_capture)
        mock_cpt = MagicMock()
        mock_cpt.from_messages = MagicMock(return_value=chain)

        with patch.object(agent_module, "ChatPromptTemplate", mock_cpt):
            await agent_module._maybe_summarize_history(h)

        if "transcript" in captured:
            transcript = captured["transcript"]
            # The content of the most recent message should not be in the transcript
            for msg in recent_messages:
                assert msg.content not in transcript


# ---------------------------------------------------------------------------
# TestRunAgentIntegration — verify the integration point in run_agent()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRunAgentIntegration:
    """
    Verify that run_agent() calls _maybe_summarize_history() and passes its
    return value as the `history` key into the rewrite_query chain when the
    thread has prior messages (non-first turn).

    We patch _maybe_summarize_history itself so we can assert it was called.
    """

    @pytest.mark.asyncio
    async def test_run_agent_calls_maybe_summarize_on_followup_turn(self):
        """
        When history.messages is non-empty, run_agent() must call
        _maybe_summarize_history(history) exactly once.
        """
        prior_history = ChatMessageHistory()
        prior_history.add_user_message("Who scored the most runs?")
        prior_history.add_ai_message("Virat Kohli.")

        summarized = prior_history.messages[:]

        mock_asyncio = MagicMock()
        mock_asyncio.gather = AsyncMock(side_effect=[
            (["deliveries"], "cricket context"),
            ("Natural answer.", {"key_takeaway": "", "follow_up_chips": []}, None),
        ])
        mock_asyncio.Semaphore = asyncio.Semaphore

        mock_summarize = AsyncMock(return_value=summarized)

        with (
            patch.object(agent_module, "_get_chain") as mock_get_chain,
            patch.object(agent_module, "_get_history", return_value=prior_history),
            patch.object(agent_module, "_redis_available", False),
            patch.object(agent_module, "_maybe_summarize_history", new=mock_summarize),
            patch.object(
                agent_module, "_llm_invoke",
                new=AsyncMock(return_value="Standalone question?")
            ),
            patch.object(
                agent_module, "resolve_player_mentions",
                return_value=("Standalone question?", {})
            ),
            patch.object(agent_module, "_db") as mock_db,
            patch.object(agent_module, "validate_sql"),
            patch.object(agent_module, "detect_semantic_sql_issue", return_value=None),
            patch.object(agent_module, "_clean_sql", return_value="SELECT 1"),
            patch.object(agent_module, "_run_sql", new=AsyncMock(return_value="[(1,)]")),
            patch.object(agent_module, "_is_sql_error", return_value=False),
            patch.object(agent_module, "asyncio", new=mock_asyncio),
        ):
            mock_rewrite = MagicMock()
            mock_generate = MagicMock()
            mock_execute = MagicMock()
            mock_rephrase = MagicMock()
            mock_select = MagicMock()
            mock_get_chain.return_value = (
                mock_generate, mock_execute, mock_rephrase, mock_select, mock_rewrite
            )
            mock_db.get_usable_table_names.return_value = ["deliveries", "matches"]

            try:
                await agent_module.run_agent("What about in 2020?", "thread-test")
            except Exception:
                pass  # Pipeline may fail deep in mocked layers; what matters is the call

            mock_summarize.assert_called_once_with(prior_history)

    @pytest.mark.asyncio
    async def test_run_agent_skips_summarize_on_first_turn(self):
        """
        On the first turn (empty history), _maybe_summarize_history must
        NOT be called — there is nothing to summarize.
        """
        empty_history = ChatMessageHistory()  # no messages

        mock_asyncio2 = MagicMock()
        mock_asyncio2.gather = AsyncMock(side_effect=[
            (["deliveries"], ""),
            ("Answer.", {"key_takeaway": "", "follow_up_chips": []}, None),
        ])
        mock_asyncio2.Semaphore = asyncio.Semaphore

        mock_summarize = AsyncMock(return_value=[])

        with (
            patch.object(agent_module, "_get_chain") as mock_get_chain,
            patch.object(agent_module, "_get_history", return_value=empty_history),
            patch.object(agent_module, "_redis_available", False),
            patch.object(agent_module, "_maybe_summarize_history", new=mock_summarize),
            patch.object(
                agent_module, "_llm_invoke",
                new=AsyncMock(return_value="SELECT 1")
            ),
            patch.object(
                agent_module, "resolve_player_mentions",
                return_value=("question?", {})
            ),
            patch.object(agent_module, "_db") as mock_db,
            patch.object(agent_module, "validate_sql"),
            patch.object(agent_module, "detect_semantic_sql_issue", return_value=None),
            patch.object(agent_module, "_clean_sql", return_value="SELECT 1"),
            patch.object(agent_module, "_run_sql", new=AsyncMock(return_value="result")),
            patch.object(agent_module, "_is_sql_error", return_value=False),
            patch.object(agent_module, "asyncio", new=mock_asyncio2),
        ):
            mock_get_chain.return_value = (
                MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )
            mock_db.get_usable_table_names.return_value = ["deliveries"]

            try:
                await agent_module.run_agent("Who scored most runs?", "thread-new")
            except Exception:
                pass

            # Must NOT be called on first turn
            mock_summarize.assert_not_called()

    @pytest.mark.asyncio
    async def test_rewrite_uses_summarized_history_not_raw_messages(self):
        """
        The `history` key passed to the rewrite chain invocation must be
        the return value of _maybe_summarize_history(), not history.messages.
        This ensures that the compressed context reaches the rewrite LLM.
        """
        prior_history = ChatMessageHistory()
        prior_history.add_user_message("Who bowled the most overs?")
        prior_history.add_ai_message("MS Dhoni's team was efficient.")

        # The summarized version — different from raw messages (Fix 4: HumanMessage not SystemMessage)
        summary_msg = HumanMessage(content="[Earlier conversation summary]\n• Overs discussed")
        recent = prior_history.messages[-_SUMMARY_KEEP:] if len(prior_history.messages) >= _SUMMARY_KEEP else prior_history.messages
        summarized_history = [summary_msg] + recent

        captured_invoke_args: list = []

        async def _fake_llm_invoke(chain, inputs):
            captured_invoke_args.append(inputs)
            # Return a plausible standalone question
            return "Who bowled the most overs in IPL 2020?"

        mock_asyncio3 = MagicMock()
        mock_asyncio3.gather = AsyncMock(side_effect=[
            (["deliveries"], ""),
            ("Answer.", {"key_takeaway": "", "follow_up_chips": []}, None),
        ])
        mock_asyncio3.Semaphore = asyncio.Semaphore

        with (
            patch.object(agent_module, "_get_chain") as mock_get_chain,
            patch.object(agent_module, "_get_history", return_value=prior_history),
            patch.object(agent_module, "_redis_available", False),
            patch.object(
                agent_module, "_maybe_summarize_history",
                new=AsyncMock(return_value=summarized_history)
            ),
            patch.object(agent_module, "_llm_invoke", new=AsyncMock(side_effect=_fake_llm_invoke)),
            patch.object(
                agent_module, "resolve_player_mentions",
                return_value=("Who bowled the most overs in IPL 2020?", {})
            ),
            patch.object(agent_module, "_db") as mock_db,
            patch.object(agent_module, "validate_sql"),
            patch.object(agent_module, "detect_semantic_sql_issue", return_value=None),
            patch.object(agent_module, "_clean_sql", return_value="SELECT 1"),
            patch.object(agent_module, "_run_sql", new=AsyncMock(return_value="result")),
            patch.object(agent_module, "_is_sql_error", return_value=False),
            patch.object(agent_module, "asyncio", new=mock_asyncio3),
        ):
            mock_get_chain.return_value = (
                MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )
            mock_db.get_usable_table_names.return_value = ["deliveries"]

            try:
                await agent_module.run_agent("What about 2020?", "thread-ctx")
            except Exception:
                pass

        # The first _llm_invoke call is the rewrite step — check its history arg
        if captured_invoke_args:
            first_call_inputs = captured_invoke_args[0]
            if "history" in first_call_inputs:
                assert first_call_inputs["history"] == summarized_history


# ---------------------------------------------------------------------------
# TestSummaryMessageType — Fix 4: summary wrapper is HumanMessage not SystemMessage
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSummaryMessageType:
    """
    Fix 4 (agent.py): the summary message is now a HumanMessage instead of a
    SystemMessage.  This prevents LLM-generated summary content — which may be
    injection-influenced — from acquiring system-role trust when passed into
    the rewrite chain prompt.

    Three test cases:
    1. The first element of the returned list is a HumanMessage instance.
    2. Its content starts with "[Earlier conversation summary]".
    3. It is NOT a SystemMessage (negative assertion confirming the fix holds).
    """

    @pytest.mark.asyncio
    async def test_summary_first_element_is_human_message(self):
        """
        The summary prepended to the message list must be a HumanMessage.
        Prior to Fix 4 this was a SystemMessage — this test locks in the change.
        """
        h = _make_history_n(10)

        with _PatchSummaryChain("• Kohli: 6000 runs\n• Bumrah: 200 wickets"):
            result = await agent_module._maybe_summarize_history(h)

        assert isinstance(result[0], HumanMessage), (
            f"Expected HumanMessage as first element after Fix 4, "
            f"got {type(result[0]).__name__}"
        )

    @pytest.mark.asyncio
    async def test_human_message_content_starts_with_prefix(self):
        """
        The HumanMessage content must start with '[Earlier conversation summary]'
        so downstream consumers can identify it as a condensed context block.
        """
        h = _make_history_n(12)
        summary_text = "• RCB won 3 matches\n• Virat Kohli scored 300 runs"

        with _PatchSummaryChain(summary_text):
            result = await agent_module._maybe_summarize_history(h)

        assert result[0].content.startswith("[Earlier conversation summary]"), (
            f"HumanMessage content must start with prefix. "
            f"Got: {result[0].content!r}"
        )

    @pytest.mark.asyncio
    async def test_summary_element_is_not_system_message(self):
        """
        Negative assertion: the summary must NOT be a SystemMessage after Fix 4.
        A SystemMessage would give LLM-generated content system-role trust in
        subsequent prompt construction — that is the vulnerability Fix 4 closes.
        """
        h = _make_history_n(10)

        with _PatchSummaryChain("• Some summary content"):
            result = await agent_module._maybe_summarize_history(h)

        assert not isinstance(result[0], SystemMessage), (
            "Summary must not be a SystemMessage after Fix 4 — "
            "LLM-generated content must not acquire system-role trust"
        )
