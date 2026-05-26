"""LLM-as-judge for Gemini Code Assist finding quality.

Calls an OpenAI chat model (default: gpt-4o-mini, override via OPENAI_JUDGE_MODEL)
to label each Gemini finding as useful / false-positive / borderline / dup.

Cross-vendor judging is intentional: judging Gemini's findings with a non-Google
model reduces self-eval bias. Calibrate by comparing the judge's labels against
the human-labeled fixtures in ``evals/fixtures/pr-*.label.json``.

Usage from Python:

    from evals.judge import JudgeClient, JudgeResult

    client = JudgeClient()  # reads OPENAI_API_KEY from env
    result = client.judge(finding={...})
    print(result.label, result.confidence, result.reason)

The HTTP call is mockable via the ``call_fn`` constructor arg, so the runner's
metric computation can be tested without burning API tokens.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import typing as t


VALID_LABELS = ("useful", "false-positive", "borderline", "dup")

DEFAULT_MODEL = os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = """\
You are evaluating a Gemini Code Assist code-review comment to assess whether it \
represents a real, useful finding the PR author should act on. Be a strict judge: \
do not assume Gemini is correct just because it's confident.

Rate the finding as exactly ONE of:
- "useful": A real issue worth addressing (bug, security, correctness, meaningful \
style/clarity improvement, missing test, doc/code mismatch, defensive null-safety \
on a plausible path).
- "false-positive": Incorrect — based on misreading the diff, looking at the wrong \
context, or claiming a problem that doesn't exist in the actual codebase.
- "borderline": Ambiguous, very low-impact nit, or style-only where reasonable \
maintainers might disagree.
- "dup": Substantively duplicates another finding on the same PR (e.g., re-flag \
at a slightly different line after a fix already addressed it elsewhere).

Respond with strict JSON only, no preamble:
{"label": "<one of the four>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}\
"""


def build_user_prompt(finding: dict) -> str:
    """Render a finding into the structured user prompt."""
    severity = finding.get("severity") or "unknown"
    path = finding.get("path") or "(unknown path)"
    line = finding.get("line") or "?"
    body = (finding.get("body") or "").strip()
    diff_hunk = (finding.get("diff_hunk") or "").strip()

    parts = [
        f"File: {path}:{line}",
        f"Gemini severity: {severity}",
        "",
        "Gemini's finding:",
        body,
    ]
    if diff_hunk:
        parts.append("")
        parts.append("Surrounding diff hunk:")
        parts.append("```")
        parts.append(diff_hunk)
        parts.append("```")
    return "\n".join(parts)


@dataclasses.dataclass(frozen=True)
class JudgeResult:
    label: str
    confidence: float
    reason: str
    raw_response: str | None = None


class JudgeError(RuntimeError):
    pass


class JudgeClient:
    """Thin wrapper around OpenAI chat completions with JSON mode.

    ``call_fn`` is the underlying API callable; in tests, replace it with a fake
    that returns a canned response without hitting the network.

    ``temperature`` controls determinism. Default 0.0 — same input always yields
    the same label, so single-sample runs are reproducible. To use the
    ``--samples N > 1`` variance signal in run_eval.py, bump this above zero
    (typical range: 0.3–0.7); identical samples mean zero variance regardless of
    how many you take.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        call_fn: t.Callable[..., dict] | None = None,
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._call_fn = call_fn
        self.temperature = temperature

    def _openai_call(self, messages: list[dict]) -> dict:
        """Default OpenAI Python SDK call. Imported lazily so tests don't need it installed."""
        if not self.api_key:
            raise JudgeError(
                "OPENAI_API_KEY is not set. Export it or pass api_key=... to JudgeClient."
            )
        try:
            from openai import OpenAI  # noqa: PLC0415
        except ImportError as exc:
            raise JudgeError(
                "openai SDK not installed. `pip install openai` or use a mock call_fn."
            ) from exc
        client = OpenAI(api_key=self.api_key)
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=self.temperature,
        )
        # Defensive: content filtering or upstream API anomalies can return
        # an empty choices list, which would IndexError on [0].
        if not resp.choices:
            raise JudgeError("OpenAI response returned no choices.")
        return {
            "content": resp.choices[0].message.content,
            "model": resp.model,
        }

    def judge(self, finding: dict) -> JudgeResult:
        """Call the judge once and return a structured result.

        Raises JudgeError if the response can't be parsed into the expected schema.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(finding)},
        ]
        call = self._call_fn or self._openai_call
        raw = call(messages)
        content = raw.get("content") if isinstance(raw, dict) else None
        if not content:
            raise JudgeError(f"Empty response from judge: {raw!r}")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"Judge response was not valid JSON: {content!r}") from exc

        # Defensive: json.loads accepts non-object JSON (lists, strings, null).
        # All downstream code assumes a dict, so reject anything else early.
        if not isinstance(payload, dict):
            raise JudgeError(f"Judge response was not a JSON object: {content!r}")

        label = payload.get("label")
        if label not in VALID_LABELS:
            raise JudgeError(
                f"Judge returned an invalid label {label!r}; expected one of {VALID_LABELS}."
            )

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise JudgeError(f"Judge confidence is not a number: {payload!r}") from exc
        confidence = max(0.0, min(1.0, confidence))

        # Coerce to str defensively: a misbehaving model could return a non-string
        # for `reason` (int, bool, list); strip() would then AttributeError.
        reason = str(payload.get("reason") or "").strip()
        return JudgeResult(label=label, confidence=confidence, reason=reason, raw_response=content)


if __name__ == "__main__":  # pragma: no cover
    # Quick smoke-test from CLI: cat finding.json | python3 -m evals.judge
    finding = json.load(sys.stdin)
    result = JudgeClient().judge(finding)
    print(json.dumps(dataclasses.asdict(result), indent=2))
