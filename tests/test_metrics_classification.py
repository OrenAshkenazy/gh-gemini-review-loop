"""Unit tests for metrics.classify_finding_state and the new outcome.

The classifier is a pure, multi-signal state machine: it takes explicit
booleans (no hidden magic) and returns exactly one per-finding state. Each
documented precedence branch is exercised with the minimal distinguishing
signal combination, plus precedence checks where a higher rule must override
a lower one.
"""

import metrics


def _signals(**overrides):
    """Build a signals dict with every flag defaulting to False."""
    base = {
        "judge_needs_human": False,
        "carried_over": False,
        "fixed_locally": False,
        "file_changed": False,
        "gemini_confirmed": False,
        "cap_reached": False,
    }
    base.update(overrides)
    return base


class TestValidOutcomes:
    def test_fixed_pending_confirmation_is_valid_outcome(self):
        assert "fixed_pending_confirmation" in metrics.VALID_OUTCOMES

    def test_existing_outcomes_preserved(self):
        for outcome in ("clean", "capped", "human", "regression", "no_progress", "verification_failed"):
            assert outcome in metrics.VALID_OUTCOMES


class TestClassifyFindingState:
    def test_new_valid_actionable_when_no_signals(self):
        assert metrics.classify_finding_state(_signals()) == "new_valid_actionable"

    def test_confirmed_outdated_when_gemini_confirmed(self):
        assert (
            metrics.classify_finding_state(_signals(gemini_confirmed=True))
            == "confirmed_outdated"
        )

    def test_needs_human_when_judge_flags_it(self):
        assert (
            metrics.classify_finding_state(_signals(judge_needs_human=True))
            == "needs_human"
        )

    def test_fixed_pending_confirmation_when_fixed_and_changed_at_cap(self):
        assert (
            metrics.classify_finding_state(
                _signals(fixed_locally=True, file_changed=True, cap_reached=True)
            )
            == "fixed_pending_confirmation"
        )

    def test_fixed_pushed_awaiting_review_when_fixed_and_changed_not_capped(self):
        assert (
            metrics.classify_finding_state(
                _signals(fixed_locally=True, file_changed=True)
            )
            == "fixed_pushed_awaiting_review"
        )

    def test_fixed_locally_when_marked_but_no_file_change(self):
        assert (
            metrics.classify_finding_state(_signals(fixed_locally=True))
            == "fixed_locally"
        )

    def test_stale_already_fixed_when_carried_and_file_changed(self):
        assert (
            metrics.classify_finding_state(
                _signals(carried_over=True, file_changed=True)
            )
            == "stale_already_fixed"
        )

    def test_capped_unfixed_when_carried_at_cap(self):
        assert (
            metrics.classify_finding_state(_signals(carried_over=True, cap_reached=True))
            == "capped_unfixed"
        )

    def test_carried_over_when_persisting_no_other_signal(self):
        assert (
            metrics.classify_finding_state(_signals(carried_over=True))
            == "carried_over"
        )

    def test_capped_unfixed_when_only_cap_reached(self):
        assert (
            metrics.classify_finding_state(_signals(cap_reached=True))
            == "capped_unfixed"
        )


class TestClassifyPrecedence:
    def test_gemini_confirmed_overrides_needs_human(self):
        assert (
            metrics.classify_finding_state(
                _signals(gemini_confirmed=True, judge_needs_human=True)
            )
            == "confirmed_outdated"
        )

    def test_needs_human_overrides_fixed_signals(self):
        # A human-decision finding must surface even with fix signals at cap.
        assert (
            metrics.classify_finding_state(
                _signals(
                    judge_needs_human=True,
                    fixed_locally=True,
                    file_changed=True,
                    cap_reached=True,
                )
            )
            == "needs_human"
        )

    def test_fixed_pending_confirmation_overrides_carried_over(self):
        # A carried-over finding that is also locally fixed at cap is pending
        # confirmation, not capped_unfixed.
        assert (
            metrics.classify_finding_state(
                _signals(
                    carried_over=True,
                    fixed_locally=True,
                    file_changed=True,
                    cap_reached=True,
                )
            )
            == "fixed_pending_confirmation"
        )

    def test_missing_keys_default_to_false(self):
        # Partial signals dict must not raise; absent flags are False.
        assert metrics.classify_finding_state({"cap_reached": True}) == "capped_unfixed"
