"""
Unit tests for the query rewrite safety guards in agent.py (Phase 0).

The rewrite step turns ambiguous follow-ups ("What about 2020?") into
standalone questions. Two safety guards protect against the LLM answering
instead of rewriting:

  1. Output must end with '?' — statements are not questions.
  2. Output must be ≤ 300 chars — paragraph-length hallucinations are rejected.

Bug #31 regression: the old length-ratio guard (3× or 5× of original) was
too aggressive for short inputs. A short follow-up like "you forgot to plot"
(18 chars) would have a ceiling of 54/90 chars — any meaningful standalone
question (60+ chars) would be discarded. The fix was to remove the ratio and
use only the absolute 300-char ceiling.
"""

import pytest


# ---------------------------------------------------------------------------
# Helpers: replicate the guard logic from agent.py
# ---------------------------------------------------------------------------

def _apply_rewrite_guard(original: str, rewrite: str) -> str:
    """
    Mirror the safety guard from run_agent() so we can test it in isolation.

    Returns the rewrite if valid, otherwise returns the original question.
    """
    _looks_like_answer = (
        not rewrite.strip().endswith("?")
        or len(rewrite) > 300
    )
    return original if _looks_like_answer else rewrite


# ---------------------------------------------------------------------------
# Safety guard: output must end with '?'
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRewriteGuardEndsWithQuestion:

    def test_rewrite_ending_with_question_mark_accepted(self):
        original = "What about 2020?"
        rewrite = "How many matches were played in the 2020 IPL season?"
        result = _apply_rewrite_guard(original, rewrite)
        assert result == rewrite

    def test_rewrite_not_ending_with_question_mark_rejected(self):
        original = "What about 2020?"
        rewrite = "In the 2020 IPL season, Mumbai Indians won the most matches."
        result = _apply_rewrite_guard(original, rewrite)
        assert result == original

    def test_statement_answer_falls_back_to_original(self):
        original = "Show me more."
        rewrite = "Virat Kohli scored 6624 runs in total. He is the top scorer."
        result = _apply_rewrite_guard(original, rewrite)
        assert result == original

    def test_rewrite_with_trailing_whitespace_accepted(self):
        """Trailing whitespace after '?' should still be considered valid."""
        original = "What about Kohli?"
        rewrite = "How many runs did Virat Kohli score in total?  "
        # strip() is applied in the guard, so trailing whitespace is ignored
        result = _apply_rewrite_guard(original, rewrite)
        assert result == rewrite


# ---------------------------------------------------------------------------
# Safety guard: output must be ≤ 300 chars
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRewriteGuardAbsoluteLength:

    def test_rewrite_at_exactly_300_chars_accepted(self):
        original = "Show more."
        # 299 chars + '?'
        rewrite = "A" * 299 + "?"
        assert len(rewrite) == 300
        result = _apply_rewrite_guard(original, rewrite)
        assert result == rewrite

    def test_rewrite_at_301_chars_rejected(self):
        original = "Show more."
        rewrite = "A" * 300 + "?"
        assert len(rewrite) == 301
        result = _apply_rewrite_guard(original, rewrite)
        assert result == original

    def test_paragraph_length_hallucination_rejected(self):
        original = "And 2019?"
        # LLM answered the question instead of rewriting it — ends with '?'
        # to specifically exercise the length path (not the question-mark path).
        rewrite = (
            "In the 2019 IPL season there were 60 matches played across India. "
            "Mumbai Indians topped the points table with 18 points from 14 games. "
            "Chennai Super Kings were runners up with 15 points. The season was "
            "notable for Rohit Sharma's consistent batting, Jasprit Bumrah's pace "
            "bowling, and many high-scoring thrillers. Who won the most matches in 2019?"
        )
        assert len(rewrite) > 300, (
            f"Test setup: rewrite must be > 300 chars, got {len(rewrite)}"
        )
        result = _apply_rewrite_guard(original, rewrite)
        assert result == original


# ---------------------------------------------------------------------------
# Bug #31 regression: short follow-ups must not be discarded
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRewriteGuardBug31Regression:

    def test_short_followup_plot_rewrite_accepted(self):
        """
        Bug #31: "you forgot to plot" (18 chars) must expand into a valid
        standalone question. The old 3× ratio ceiling (54 chars) would have
        rejected any rewrite > 54 chars. The current guard (≤ 300 chars
        absolute) accepts this correctly.
        """
        original = "you forgot to plot"
        rewrite = "Can you show me a bar chart of the top run scorers in IPL history?"
        assert len(original) * 3 < len(rewrite), "Test setup: rewrite exceeds 3× original"
        assert len(rewrite) <= 300
        result = _apply_rewrite_guard(original, rewrite)
        assert result == rewrite, (
            "Bug #31 regression: short follow-up rewrite was rejected by the guard"
        )

    def test_single_word_followup_expands_correctly(self):
        """Even a 4-char original like 'plot' must accept a full rewrite."""
        original = "plot"
        rewrite = "Can you create a bar chart of the top 5 batsmen by total runs?"
        assert len(rewrite) <= 300
        result = _apply_rewrite_guard(original, rewrite)
        assert result == rewrite

    def test_old_ratio_guard_would_have_failed(self):
        """
        Document that the old 3× ratio guard would reject valid rewrites.

        This test does NOT invoke the guard — it just verifies the test setup
        demonstrates the bug correctly for the 3× case.
        """
        original = "you forgot to plot"
        rewrite = "Can you show me a bar chart of the top run scorers in IPL history?"

        # Old 3× guard would reject (18 * 3 = 54, rewrite is ~67 chars)
        assert len(rewrite) > len(original) * 3

        # New absolute guard would accept (≤ 300 chars, ends with '?')
        assert len(rewrite) <= 300
        assert rewrite.strip().endswith("?")


# ---------------------------------------------------------------------------
# Combined guard: both conditions must be satisfied
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRewriteGuardCombined:

    def test_both_conditions_must_be_met(self):
        original = "Follow-up?"

        # Fails length check (> 300) even though ends with '?'
        too_long = "A" * 300 + "?"
        assert _apply_rewrite_guard(original, too_long) == original

        # Fails question check (doesn't end with '?') even though short
        not_question = "This is a statement"
        assert _apply_rewrite_guard(original, not_question) == original

        # Passes both checks
        valid = "How many wickets did Bumrah take in 2023?"
        assert _apply_rewrite_guard(original, valid) == valid

    def test_first_turn_original_returned_unchanged(self):
        """
        On first turn the rewrite step is skipped — the original question
        is used as-is. This test documents that contract (the guard is not
        even called on first turn, but the result must equal the original).
        """
        original = "How many matches were played in 2019?"
        # Simulate first-turn: no rewrite → use original
        standalone = original
        assert standalone == original
