"""Tests for the eval runner — mockable judge, no network calls."""

from __future__ import annotations

import json

import pytest

from evals.judge import JudgeClient, JudgeError, build_user_prompt
from evals.run_eval import (
    EvaluatedFinding,
    confusion_matrix,
    discover_fixtures,
    evaluate_fixture,
    metrics,
    render_summary,
)


# ---------------------------------------------------------------------------
# Fake-judge factories for hermetic tests
# ---------------------------------------------------------------------------


def fake_constant(label: str, confidence: float = 0.9, reason: str = "fake"):
    """Judge that always returns the same label."""
    def call(_messages):
        return {"content": json.dumps({"label": label, "confidence": confidence, "reason": reason})}
    return call


def fake_sequence(labels: list[str]):
    """Judge that returns labels[0], labels[1], ... in order (wraps when exhausted)."""
    idx = {"i": 0}

    def call(_messages):
        lbl = labels[idx["i"] % len(labels)]
        idx["i"] += 1
        return {"content": json.dumps({"label": lbl, "confidence": 0.7, "reason": f"sample {idx['i']}"})}

    return call


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    def test_includes_file_severity_and_body(self):
        prompt = build_user_prompt(
            {"path": "x.py", "line": 42, "severity": "high", "body": "Fix me", "diff_hunk": ""}
        )
        assert "x.py:42" in prompt
        assert "Gemini severity: high" in prompt
        assert "Fix me" in prompt

    def test_omits_diff_block_when_empty(self):
        prompt = build_user_prompt(
            {"path": "x.py", "line": 1, "severity": "low", "body": "hi", "diff_hunk": ""}
        )
        assert "diff hunk" not in prompt

    def test_includes_diff_block_when_present(self):
        prompt = build_user_prompt(
            {"path": "x.py", "line": 1, "severity": "low", "body": "hi", "diff_hunk": "+foo"}
        )
        assert "diff hunk" in prompt
        assert "+foo" in prompt


# ---------------------------------------------------------------------------
# JudgeClient
# ---------------------------------------------------------------------------


class TestJudgeClient:
    def test_parses_valid_response(self):
        client = JudgeClient(call_fn=fake_constant("useful", 0.95, "looks legit"))
        result = client.judge({"body": "anything"})
        assert result.label == "useful"
        assert result.confidence == 0.95
        assert result.reason == "looks legit"

    def test_temperature_default_is_zero(self):
        client = JudgeClient(call_fn=fake_constant("useful"))
        assert client.temperature == 0.0

    def test_temperature_override(self):
        client = JudgeClient(call_fn=fake_constant("useful"), temperature=0.7)
        assert client.temperature == 0.7

    def test_raises_on_invalid_label(self):
        client = JudgeClient(
            call_fn=lambda _m: {"content": json.dumps({"label": "maybe", "confidence": 0.5})}
        )
        with pytest.raises(JudgeError):
            client.judge({"body": "x"})

    def test_raises_on_invalid_json(self):
        client = JudgeClient(call_fn=lambda _m: {"content": "not json at all"})
        with pytest.raises(JudgeError):
            client.judge({"body": "x"})

    def test_clamps_confidence_to_unit_range(self):
        client = JudgeClient(
            call_fn=lambda _m: {"content": json.dumps({"label": "useful", "confidence": 5.0})}
        )
        result = client.judge({"body": "x"})
        assert result.confidence == 1.0

    def test_handles_negative_confidence(self):
        client = JudgeClient(
            call_fn=lambda _m: {"content": json.dumps({"label": "useful", "confidence": -1.0})}
        )
        result = client.judge({"body": "x"})
        assert result.confidence == 0.0

    def test_raises_when_payload_is_not_dict(self):
        # Valid JSON but not an object: the model returned a bare string/list/null.
        client = JudgeClient(call_fn=lambda _m: {"content": json.dumps(["useful"])})
        with pytest.raises(JudgeError, match="not a JSON object"):
            client.judge({"body": "x"})

    def test_raises_when_payload_is_null(self):
        client = JudgeClient(call_fn=lambda _m: {"content": "null"})
        with pytest.raises(JudgeError, match="not a JSON object"):
            client.judge({"body": "x"})


# ---------------------------------------------------------------------------
# Runner: metrics / confusion / agreement
# ---------------------------------------------------------------------------


def make_row(human: str, judge_labels: list[str], sev: str = "medium") -> EvaluatedFinding:
    return EvaluatedFinding(
        pr=1,
        comment_id=f"c-{human}-{','.join(judge_labels)}",
        severity=sev,
        path="x.py",
        line=1,
        human_label=human,
        judge_labels=judge_labels,
        judge_confidences=[0.9] * len(judge_labels),
        judge_reasons=["r"] * len(judge_labels),
        body_excerpt="x",
    )


class TestMajorityAndAgreement:
    def test_majority_judge_label_picks_mode(self):
        row = make_row("useful", ["useful", "useful", "borderline"])
        assert row.majority_judge_label == "useful"

    def test_agrees_with_human(self):
        row = make_row("useful", ["useful"])
        assert row.agrees_with_human

    def test_disagrees_when_majority_differs(self):
        row = make_row("useful", ["false-positive", "false-positive", "useful"])
        assert not row.agrees_with_human

    def test_has_variance_flagged(self):
        row = make_row("useful", ["useful", "borderline", "useful"])
        assert row.has_variance is True

    def test_no_variance_when_all_same(self):
        row = make_row("useful", ["useful", "useful"])
        assert row.has_variance is False


class TestMetrics:
    def test_empty_input_returns_full_shape(self):
        # Must populate every key so render_summary() and JSON consumers work.
        m = metrics([])
        assert m["n"] == 0
        assert m["agreement_rate"] == 1.0
        assert m["agreements"] == 0
        assert m["variance_count"] == 0
        assert m["by_severity"] == {}
        assert m["by_human_label"] == {}
        assert m["by_judge_label"] == {}
        # Confusion matrix must be present and well-shaped even at n=0
        assert "useful" in m["confusion"]
        assert m["confusion"]["useful"]["useful"] == 0

    def test_empty_input_renders_summary_without_keyerror(self):
        # Regression: render_summary used to KeyError on empty rows.
        m = metrics([])
        out = render_summary([], m)
        assert "0 findings" in out
        assert "100.0%" in out  # vacuous agreement

    def test_perfect_agreement(self):
        rows = [
            make_row("useful", ["useful"]),
            make_row("false-positive", ["false-positive"]),
            make_row("useful", ["useful"]),
        ]
        m = metrics(rows)
        assert m["n"] == 3
        assert m["agreement_rate"] == 1.0
        assert m["agreements"] == 3
        assert m["variance_count"] == 0

    def test_half_agreement(self):
        rows = [
            make_row("useful", ["useful"]),
            make_row("useful", ["false-positive"]),
        ]
        m = metrics(rows)
        assert m["agreement_rate"] == 0.5

    def test_by_severity_breakdown(self):
        rows = [
            make_row("useful", ["useful"], sev="high"),
            make_row("useful", ["false-positive"], sev="medium"),
            make_row("useful", ["useful"], sev="medium"),
        ]
        m = metrics(rows)
        assert m["by_severity"]["high"] == {"n": 1, "agreement_rate": 1.0}
        assert m["by_severity"]["medium"] == {"n": 2, "agreement_rate": 0.5}


class TestConfusionMatrix:
    def test_pure_useful(self):
        rows = [make_row("useful", ["useful"]) for _ in range(3)]
        cm = confusion_matrix(rows)
        assert cm["useful"]["useful"] == 3
        assert cm["useful"]["false-positive"] == 0
        assert cm["false-positive"] == {"useful": 0, "false-positive": 0, "borderline": 0, "dup": 0}

    def test_mixed_diagonal(self):
        rows = [
            make_row("useful", ["useful"]),
            make_row("false-positive", ["useful"]),  # off-diagonal
            make_row("false-positive", ["false-positive"]),
        ]
        cm = confusion_matrix(rows)
        assert cm["false-positive"]["useful"] == 1
        assert cm["false-positive"]["false-positive"] == 1


# ---------------------------------------------------------------------------
# End-to-end with a fake judge against the real fixtures
# ---------------------------------------------------------------------------


class TestEvaluateFixture:
    def test_real_fixture_pr8_with_perfectly_calibrated_fake(self):
        # Force the fake judge to always return the human label for PR #8 (false-positive).
        client = JudgeClient(call_fn=fake_constant("false-positive"))
        rows = evaluate_fixture("pr-8", client, samples=1)
        assert len(rows) == 1
        assert rows[0].human_label == "false-positive"
        assert rows[0].agrees_with_human is True

    def test_real_fixture_pr8_with_uncalibrated_fake(self):
        # Fake always says "useful" → should disagree with the PR #8 false-positive.
        client = JudgeClient(call_fn=fake_constant("useful"))
        rows = evaluate_fixture("pr-8", client, samples=1)
        assert rows[0].agrees_with_human is False
        m = metrics(rows)
        assert m["agreement_rate"] == 0.0


class TestDiscoverFixtures:
    def test_finds_pr_fixtures(self):
        stems = discover_fixtures()
        assert "pr-6" in stems
        assert "pr-7" in stems
        assert "pr-8" in stems
        assert "pr-9" in stems
        # Should NOT pick up the label files as separate fixtures
        assert all(not s.endswith(".label") for s in stems)


class TestRenderSummary:
    def test_summary_includes_key_sections(self):
        rows = [make_row("useful", ["useful"]), make_row("false-positive", ["useful"])]
        m = metrics(rows)
        out = render_summary(rows, m)
        assert "agreement" in out.lower()
        assert "By severity" in out
        assert "Confusion matrix" in out
        # Disagreement block surfaces the PR #1 false-positive case
        assert "Disagreements" in out
