"""End-user-side LLM-as-judge for Gemini Code Assist findings.

This module ships INSIDE the installed plugin (it is what the user invokes
via ``--judge-mode``). It is intentionally separate from ``evals/judge.py``
at the repo root — that one is the maintainer's calibration judge and uses
a simpler 4-label schema. This one uses the richer end-user schema and runs
on the user's own PRs.

Verdict schema (richer than the maintainer one because end-users need
actionable advice, not just calibration labels):

- ``valid_actionable``   — Real finding the user should act on.
- ``false_positive``     — Incorrect; misread the diff or wrong context.
- ``needs_human``        — Requires a product/design/security judgment.
- ``explanation_only``   — Asks for an explanation, not a code change.
- ``duplicate``          — Substantively duplicates another finding on this PR.
- ``already_addressed``  — Resolved elsewhere; the line just hasn't been auto-resolved.

Plus:
- ``severity_override``  — One of critical/high/medium/low/none. Judge's
  opinion on the right severity, which can differ from Gemini's label.
- ``recommended_action`` — fix / reply / ignore / escalate.

Privacy / cost / safety invariants
- This module makes ZERO calls to OpenAI on its own. It only does work
  when the caller passes a judge mode and a phase that match. The caller
  (fetch_gemini_threads.py) reads the user's saved preference file and
  decides; this module obeys.
- The judge NEVER mutates the GitHub state. It cannot resolve, comment,
  push, or call any GraphQL/REST write. It only reads finding text and
  emits structured verdicts.
- When the OpenAI key or SDK is missing, this module returns a structured
  ``skipped`` result with a reason — not a raw exception. The caller
  surfaces the reason and continues the loop unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import typing as t
from pathlib import Path


VALID_VERDICTS = (
    "valid_actionable",
    "false_positive",
    "needs_human",
    "explanation_only",
    "duplicate",
    "already_addressed",
)

VALID_SEVERITY_OVERRIDES = ("critical", "high", "medium", "low", "none")

VALID_RECOMMENDED_ACTIONS = ("fix", "reply", "ignore", "escalate")

VALID_JUDGE_MODES = ("off", "on_cycle", "on_complete", "once")

VALID_JUDGE_PHASES = ("cycle", "complete")

DEFAULT_MODEL = os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o-mini")
DEFAULT_REQUEST_TIMEOUT = 30.0

PREFS_SCHEMA_VERSION = 1

SYSTEM_PROMPT = """\
You are an expert code-review judge. The user is deciding whether to act on \
a Gemini Code Assist finding on their pull request. Be strict: do not assume \
Gemini is correct just because it sounds confident, and do not assume it is \
wrong just because the finding is minor.

Classify the finding as exactly ONE verdict:
- "valid_actionable": Real issue the user should act on (bug, security, \
correctness, meaningful style/clarity improvement, missing test, doc/code \
mismatch, defensive null-safety on a plausible path).
- "false_positive": Incorrect — based on misreading the diff, looking at \
the wrong context, or claiming a problem that doesn't exist.
- "needs_human": Real but requires a product / design / security judgment \
the user must make. Don't auto-fix.
- "explanation_only": Asks for an explanation or context rather than a \
code change. Reply, don't edit.
- "duplicate": Substantively duplicates another finding on the same PR \
(e.g., re-flag at a slightly different line after a fix elsewhere).
- "already_addressed": The concern is resolved elsewhere in the diff; \
the line just hasn't been auto-resolved on GitHub.

Also output:
- "severity_override": Your opinion on the right severity (critical / high \
/ medium / low / none). Can match or differ from Gemini's label. "none" \
means below the threshold worth surfacing.
- "recommended_action": fix / reply / ignore / escalate.

Respond with strict JSON only, no preamble:
{"verdict": "<verdict>", "confidence": <0.0-1.0>, \
"severity_override": "<one of the five>", \
"recommended_action": "<one of fix/reply/ignore/escalate>", \
"reason": "<one sentence>"}\
"""


@dataclasses.dataclass(frozen=True)
class JudgeResult:
    verdict: str
    confidence: float
    severity_override: str
    recommended_action: str
    reason: str
    status: str = "ok"  # "ok" | "skipped" | "error"
    skip_reason: str | None = None
    raw_response: str | None = None


class JudgeError(RuntimeError):
    pass


def prefs_path() -> Path:
    """Return the path to the per-user preferences file.

    Overridable via ``GGRL_STATE_DIR`` to share a directory with the existing
    sticky-receipt state file. Defaults to ``~/.config/gh-gemini-review-loop/``.
    """
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / "preferences.json"


def load_preferences() -> dict[str, t.Any]:
    """Return saved preferences, or a safe default ('off') if missing/corrupt.

    Default is OFF for privacy: do not send anything to OpenAI unless the
    user has explicitly opted in.
    """
    path = prefs_path()
    if not path.exists():
        return _default_prefs()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_prefs()
    if not isinstance(data, dict):
        return _default_prefs()
    # Validate / coerce known fields. Unknown fields ignored. Unknown
    # schema_version: keep mode if valid, otherwise fall back to off.
    mode = data.get("judge_mode")
    if mode not in VALID_JUDGE_MODES:
        mode = "off"
    return {
        "schema_version": data.get("schema_version", PREFS_SCHEMA_VERSION),
        "judge_mode": mode,
        "judge_model": data.get("judge_model") or DEFAULT_MODEL,
        "judge_tip_shown": bool(data.get("judge_tip_shown", False)),
        "set_at": data.get("set_at") or "",
    }


def save_preferences(judge_mode: str, *, judge_model: str | None = None) -> dict[str, t.Any]:
    """Persist the user's judge preference. Returns the saved dict."""
    if judge_mode not in VALID_JUDGE_MODES:
        raise ValueError(f"judge_mode must be one of {VALID_JUDGE_MODES}; got {judge_mode!r}.")
    existing = load_preferences()
    prefs = {
        "schema_version": PREFS_SCHEMA_VERSION,
        "judge_mode": judge_mode,
        "judge_model": judge_model or DEFAULT_MODEL,
        "judge_tip_shown": existing.get("judge_tip_shown", False),
        "set_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    return prefs


def mark_tip_shown() -> None:
    """Persist judge_tip_shown=true without changing other prefs."""
    existing = load_preferences()
    existing["judge_tip_shown"] = True
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


def _default_prefs() -> dict[str, t.Any]:
    return {
        "schema_version": PREFS_SCHEMA_VERSION,
        "judge_mode": "off",
        "judge_model": DEFAULT_MODEL,
        "judge_tip_shown": False,
        "set_at": "",
    }


def should_judge_run(*, mode: str, phase: str | None) -> bool:
    """Decide whether the judge should execute for this invocation.

    The script is the single source of truth: this function captures the
    full logic so the agent doesn't have to replicate it.

    - ``mode == "off"``        → never.
    - ``mode == "once"``       → always run (caller's responsibility to gate
                                  on user intent for a one-shot run).
    - ``mode == "on_cycle"``   → run only when phase == "cycle".
    - ``mode == "on_complete"`` → run only when phase == "complete".
    """
    if mode == "off":
        return False
    if mode == "once":
        return True
    if mode == "on_cycle":
        return phase == "cycle"
    if mode == "on_complete":
        return phase == "complete"
    return False


def build_user_prompt(finding: dict) -> str:
    """Render a Gemini finding into the structured user prompt."""
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
        parts.extend(["", "Surrounding diff hunk:", "```", diff_hunk, "```"])
    return "\n".join(parts)


def skipped_result(reason: str) -> JudgeResult:
    """Build a structured skipped result so callers don't have to invent fields."""
    return JudgeResult(
        verdict="needs_human",
        confidence=0.0,
        severity_override="none",
        recommended_action="ignore",
        reason="",
        status="skipped",
        skip_reason=reason,
    )


class JudgeClient:
    """Calls OpenAI to label a single Gemini finding.

    ``call_fn`` is injected for tests so we never touch the network.
    Caller logic (in fetch_gemini_threads.py) MUST decide whether to invoke
    judge() based on saved preferences + phase; this class just performs the
    call when asked.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        call_fn: t.Callable[..., dict] | None = None,
        temperature: float = 0.0,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._call_fn = call_fn
        self.temperature = temperature
        self.request_timeout = request_timeout

    def is_ready(self) -> tuple[bool, str | None]:
        """Return (ready, skip_reason).

        Caller uses this to short-circuit gracefully: when not ready, emit a
        ``skipped_result(reason)`` instead of trying and crashing.
        """
        if self._call_fn is not None:
            return True, None
        if not self.api_key:
            return False, "OPENAI_API_KEY not set"
        try:
            # Older `openai` packages (< 1.0) don't expose the `OpenAI`
            # client class. Probe for it specifically so is_ready() returns
            # False (with a clear reason) instead of letting _openai_call
            # ImportError later on `from openai import OpenAI`.
            from openai import OpenAI  # noqa: F401, PLC0415
        except ImportError:
            return False, "openai SDK not installed or too old (need v1.0.0+: pip install -U openai)"
        return True, None

    def _openai_call(self, messages: list[dict]) -> dict:
        # Wrap import + client init + API call in a single try/except so
        # ANY failure (import error on stale SDK, auth failure during
        # client construction, transient network) raises JudgeError. The
        # runner catches that uniformly.
        try:
            from openai import OpenAI  # noqa: PLC0415
            client = OpenAI(api_key=self.api_key)
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=self.temperature,
                timeout=self.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — OpenAI SDK raises many concrete types
            raise JudgeError(f"OpenAI API call failed: {exc}") from exc
        if not resp.choices:
            raise JudgeError("OpenAI response returned no choices.")
        return {"content": resp.choices[0].message.content, "model": resp.model}

    def judge(self, finding: dict) -> JudgeResult:
        """Call the judge once. Returns a JudgeResult with status='ok' or status='skipped'.

        Raises JudgeError on parse / response problems so the caller can
        decide whether to skip THIS finding or abort.
        """
        ready, skip_reason = self.is_ready()
        if not ready:
            return skipped_result(skip_reason or "judge not ready")
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
        if not isinstance(payload, dict):
            raise JudgeError(f"Judge response was not a JSON object: {content!r}")

        verdict = payload.get("verdict")
        if verdict not in VALID_VERDICTS:
            raise JudgeError(
                f"Judge returned invalid verdict {verdict!r}; expected one of {VALID_VERDICTS}."
            )

        sev = payload.get("severity_override") or "none"
        if sev not in VALID_SEVERITY_OVERRIDES:
            sev = "none"

        action = payload.get("recommended_action") or "ignore"
        if action not in VALID_RECOMMENDED_ACTIONS:
            action = "ignore"

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise JudgeError(f"Judge confidence is not a number: {payload!r}") from exc
        confidence = max(0.0, min(1.0, confidence))

        reason_val = payload.get("reason")
        reason = str(reason_val).strip() if reason_val is not None else ""

        return JudgeResult(
            verdict=verdict,
            confidence=confidence,
            severity_override=sev,
            recommended_action=action,
            reason=reason,
            status="ok",
            raw_response=content,
        )
