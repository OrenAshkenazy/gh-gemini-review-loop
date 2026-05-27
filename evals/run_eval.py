"""Run the Layer B (finding-quality) eval over all fixtures.

Compares the OpenAI judge's labels against the human ground-truth labels in
``evals/fixtures/pr-*.label.json``. Prints per-severity metrics, the confusion
matrix, and an agreement rate. Optionally writes a JSON report for the CI job.

CLI:
    python3 -m evals.run_eval                       # default: 1 sample/finding, all fixtures
    python3 -m evals.run_eval --samples 3           # 3 samples per finding for variance
    python3 -m evals.run_eval --fixture pr-8        # restrict to one fixture
    python3 -m evals.run_eval --report out.json     # write a structured report

Requires OPENAI_API_KEY in the environment unless ``--judge fake`` is passed
(for self-testing).
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import pathlib
import sys
from typing import Any

from evals.judge import VALID_LABELS, JudgeClient, JudgeError, JudgeResult


FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"


@dataclasses.dataclass
class EvaluatedFinding:
    pr: int
    comment_id: str
    severity: str | None
    path: str | None
    line: int | str | None
    human_label: str
    judge_labels: list[str]  # one per sample
    judge_confidences: list[float]
    judge_reasons: list[str]
    body_excerpt: str

    @property
    def majority_judge_label(self) -> str:
        counts = collections.Counter(self.judge_labels)
        return counts.most_common(1)[0][0]

    @property
    def agrees_with_human(self) -> bool:
        return self.majority_judge_label == self.human_label

    @property
    def has_variance(self) -> bool:
        return len(set(self.judge_labels)) > 1


def load_fixture_pair(pr_id: str) -> tuple[dict, dict]:
    """Load (findings, labels) for a fixture, keyed on filename stem like 'pr-6'."""
    findings_path = FIXTURES_DIR / f"{pr_id}.json"
    labels_path = FIXTURES_DIR / f"{pr_id}.label.json"
    if not findings_path.exists():
        raise FileNotFoundError(f"Missing findings fixture: {findings_path}")
    if not labels_path.exists():
        raise FileNotFoundError(
            f"Missing human-label sidecar: {labels_path}. "
            "Every fixture needs a `.label.json` for the eval to compute agreement."
        )
    # Explicit utf-8 encoding so platforms with non-UTF8 locale defaults (some
    # Windows + older CI runners) don't decode-error on Unicode in finding bodies.
    return (
        json.loads(findings_path.read_text(encoding="utf-8")),
        json.loads(labels_path.read_text(encoding="utf-8")),
    )


def discover_fixtures() -> list[str]:
    """Return sorted fixture stems (pr-6, pr-7, ...) with both data + labels present."""
    out: list[str] = []
    for findings_path in sorted(FIXTURES_DIR.glob("pr-*.json")):
        if findings_path.name.endswith(".label.json"):
            continue
        stem = findings_path.stem
        if (FIXTURES_DIR / f"{stem}.label.json").exists():
            out.append(stem)
    return out


def evaluate_fixture(
    stem: str,
    client: JudgeClient,
    *,
    samples: int,
) -> list[EvaluatedFinding]:
    findings_doc, labels_doc = load_fixture_pair(stem)
    pr = findings_doc["pr"]
    label_by_id = {lbl["comment_id"]: lbl for lbl in labels_doc["human_labels"]}

    out: list[EvaluatedFinding] = []
    for finding in findings_doc["findings"]:
        cid = finding["comment_id"]
        human = label_by_id.get(cid)
        if human is None:
            print(
                f"warning: PR #{pr} finding {cid} has no human label; skipping.",
                file=sys.stderr,
            )
            continue

        judge_results: list[JudgeResult] = []
        for _ in range(samples):
            judge_results.append(client.judge(finding))

        out.append(
            EvaluatedFinding(
                pr=pr,
                comment_id=cid,
                severity=finding.get("severity"),
                path=finding.get("path"),
                line=finding.get("line"),
                human_label=human["label"],
                judge_labels=[r.label for r in judge_results],
                judge_confidences=[r.confidence for r in judge_results],
                judge_reasons=[r.reason for r in judge_results],
                body_excerpt=(finding.get("body") or "")[:120].replace("\n", " "),
            )
        )
    return out


def confusion_matrix(rows: list[EvaluatedFinding]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {
        h: {j: 0 for j in VALID_LABELS} for h in VALID_LABELS
    }
    for r in rows:
        matrix[r.human_label][r.majority_judge_label] += 1
    return matrix


def metrics(rows: list[EvaluatedFinding]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        # Return a fully-populated shape so render_summary() and downstream
        # JSON consumers don't trip on missing keys when a fixture is skipped
        # or has zero labeled findings. agreement_rate of 1.0 on an empty set
        # is a deliberate identity choice (vacuously true).
        return {
            "n": 0,
            "agreement_rate": 1.0,
            "agreements": 0,
            "variance_count": 0,
            "by_severity": {},
            "confusion": {h: {j: 0 for j in VALID_LABELS} for h in VALID_LABELS},
            "by_human_label": {},
            "by_judge_label": {},
        }
    agreements = sum(1 for r in rows if r.agrees_with_human)
    by_severity: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"n": 0, "agree": 0}
    )
    for r in rows:
        sev = r.severity or "unknown"
        by_severity[sev]["n"] += 1
        if r.agrees_with_human:
            by_severity[sev]["agree"] += 1
    return {
        "n": n,
        "agreement_rate": round(agreements / n, 3),
        "agreements": agreements,
        "variance_count": sum(1 for r in rows if r.has_variance),
        "by_severity": {
            sev: {
                "n": stats["n"],
                "agreement_rate": round(stats["agree"] / stats["n"], 3) if stats["n"] else 0,
            }
            for sev, stats in by_severity.items()
        },
        "confusion": confusion_matrix(rows),
        "by_human_label": dict(collections.Counter(r.human_label for r in rows)),
        "by_judge_label": dict(collections.Counter(r.majority_judge_label for r in rows)),
    }


def render_summary(rows: list[EvaluatedFinding], m: dict[str, Any]) -> str:
    lines = [
        f"# Layer B finding-quality eval — {m['n']} findings",
        "",
        f"Judge↔human agreement: **{m['agreement_rate']:.1%}** ({m['agreements']}/{m['n']})",
        f"Variance (judge labels differed across samples): {m['variance_count']}",
        "",
        "## By severity",
        "",
        "| severity | n | agreement |",
        "|---|---|---|",
    ]
    # metrics() maps None severities to "unknown", so we only iterate over the
    # four known severity keys plus "unknown". Including None here would be dead
    # code — the lookup against m["by_severity"] always uses the "unknown" key.
    known_sevs = ("critical", "high", "medium", "low", "unknown")
    for sev in known_sevs:
        stats = m["by_severity"].get(sev)
        if stats:
            lines.append(f"| {sev} | {stats['n']} | {stats['agreement_rate']:.1%} |")
    for sev in sorted(m["by_severity"].keys()):
        if sev not in known_sevs:
            stats = m["by_severity"][sev]
            lines.append(f"| {sev} | {stats['n']} | {stats['agreement_rate']:.1%} |")
    lines.extend(["", "## Distribution", ""])
    lines.append(f"- Human labels:  {m['by_human_label']}")
    lines.append(f"- Judge labels:  {m['by_judge_label']}")
    lines.append("")
    lines.append("## Confusion matrix (rows = human, columns = judge majority)")
    lines.append("")
    header = "| human \\ judge | " + " | ".join(VALID_LABELS) + " |"
    sep = "|---" * (len(VALID_LABELS) + 1) + "|"
    lines.extend([header, sep])
    for h in VALID_LABELS:
        row = m["confusion"][h]
        lines.append(f"| **{h}** | " + " | ".join(str(row[j]) for j in VALID_LABELS) + " |")
    lines.extend(["", "## Disagreements (judge majority differs from human)", ""])
    disagreements = [r for r in rows if not r.agrees_with_human]
    if not disagreements:
        lines.append("_None._")
    else:
        for r in disagreements:
            lines.append(
                f"- PR #{r.pr} {r.path}:{r.line} [{r.severity}]: "
                f"human=`{r.human_label}` vs judge=`{r.majority_judge_label}` "
                f"({r.judge_labels})"
            )
            lines.append(f"  - Finding excerpt: `{r.body_excerpt}`")
            lines.append(f"  - Judge reason (sample 1): {r.judge_reasons[0]}")
    return "\n".join(lines) + "\n"


def _fake_judge_factory():
    """Deterministic fake for self-test: labels everything as 'useful' with 0.8 confidence."""
    def fake(_messages: list[dict]) -> dict:
        return {
            "content": json.dumps(
                {"label": "useful", "confidence": 0.8, "reason": "fake judge always says useful"}
            )
        }
    return fake


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="Run only one fixture (e.g. pr-8). Default: all.")
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Judge calls per finding for variance measurement. Default: 1.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help=(
            "Sampling temperature for the judge. Default 0.0 (deterministic, "
            "single-sample reproducible). Bump above zero (e.g. 0.3-0.7) when "
            "using --samples N > 1 — at temp=0 every sample returns the same "
            "label so variance is always zero."
        ),
    )
    parser.add_argument(
        "--judge",
        choices=["openai", "fake"],
        default="openai",
        help="`fake` short-circuits the API for self-test of the runner.",
    )
    parser.add_argument(
        "--report",
        help="Write the full JSON eval report to this path (in addition to the stdout summary).",
    )
    parser.add_argument(
        "--exit-nonzero-on-disagreement",
        action="store_true",
        help="Exit 1 if judge↔human agreement drops below 80%%. Useful for CI gates.",
    )
    args = parser.parse_args(argv)

    # Validate --samples up-front: 0 or negative would produce empty
    # judge_labels lists, which makes EvaluatedFinding.majority_judge_label
    # IndexError on .most_common(1)[0]. Fail fast with a clear message.
    if args.samples < 1:
        print(
            f"error: --samples must be >= 1 (got {args.samples}).",
            file=sys.stderr,
        )
        return 1

    if args.judge == "fake":
        client = JudgeClient(call_fn=_fake_judge_factory(), temperature=args.temperature)
    else:
        client = JudgeClient(temperature=args.temperature)
    if args.samples > 1 and args.temperature == 0.0:
        print(
            "warning: --samples > 1 with --temperature 0.0 produces identical samples "
            "(variance will be 0). Consider --temperature 0.3-0.7 for a real variance signal.",
            file=sys.stderr,
        )

    stems = [args.fixture] if args.fixture else discover_fixtures()
    if not stems:
        print("error: no fixtures found in evals/fixtures/", file=sys.stderr)
        return 1

    rows: list[EvaluatedFinding] = []
    for stem in stems:
        try:
            rows.extend(evaluate_fixture(stem, client, samples=args.samples))
        except FileNotFoundError as exc:
            # Friendly handling when --fixture points at a nonexistent stem.
            # Without this the user gets a raw traceback.
            available = ", ".join(discover_fixtures()) or "(none)"
            print(
                f"error: {exc}\nAvailable fixtures: {available}",
                file=sys.stderr,
            )
            return 1
        except JudgeError as exc:
            # API / network / auth / parsing failures from the judge surface
            # here. Exit cleanly with the message instead of dumping a raw
            # traceback that obscures the cause.
            print(f"error: judge failed on fixture {stem}: {exc}", file=sys.stderr)
            return 2

    m = metrics(rows)
    summary = render_summary(rows, m)
    print(summary)

    if args.report:
        report = {
            "metrics": m,
            "rows": [dataclasses.asdict(r) for r in rows],
            "summary_md": summary,
        }
        report_path = pathlib.Path(args.report)
        # Create parent dirs so users can pass nested paths like /tmp/eval/report.json.
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {args.report}", file=sys.stderr)

    if args.exit_nonzero_on_disagreement and m.get("agreement_rate", 1.0) < 0.80:
        print(
            f"error: agreement rate {m['agreement_rate']:.1%} is below the 80% gate.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
