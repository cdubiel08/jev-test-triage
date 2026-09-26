"""Render findings as Markdown (PR comment / step summary), JSON, SARIF, or an agent prompt."""

from __future__ import annotations

import json

from .survivors import Finding
from .weak import WeakTest

ICON = {"act": "🔴", "review": "🟡", "ignore": "⚪"}


def _cell(s: str, n: int = 90) -> str:
    s = " ".join(s.split())
    s = s if len(s) <= n else s[: n - 1] + "…"
    return s.replace("|", "\\|").replace("`", "'")


def markdown(findings: list[Finding], weak: list[WeakTest], top: int = 20,
             weak_threshold: float = 0.8, spend_usd: float | None = None) -> str:
    out = []
    if findings:
        n = {b: sum(f.bucket == b for f in findings) for b in ("act", "review", "ignore")}
        out.append(f"### Surviving mutants: {n['act']} worth a test, {n['review']} to review, {n['ignore']} ignored\n")
        shown = [f for f in findings if f.bucket != "ignore"][:top]
        if shown:
            out.append("| | score | location | edit | why |\n|---|---|---|---|---|")
            for f in shown:
                m = f.mutant
                repl = m.replacement.strip() or "(removed)"
                out.append(f"| {ICON[f.bucket]} | {f.score:.2f} | `{m.file}:{m.line}` | "
                           f"`{_cell(m.original.strip(), 50)}` → `{_cell(repl, 50)}` | {_cell(f.reason, 70)} |")
        else:
            out.append("No surviving mutant looks worth a test.")
        out.append("")
    if weak:
        flagged = [w for w in weak if w.score >= weak_threshold]
        out.append(f"### Tests with weak assertions: {len(flagged)} of {len(weak)}\n")
        if flagged:
            out.append("| score | test | flags |\n|---|---|---|")
            for w in flagged[:top]:
                out.append(f"| {w.score:.2f} | `{w.test.file}:{w.test.line}` {_cell(w.test.name, 70)} | "
                           f"{_cell(', '.join(w.flags) or 'low assertion strength', 70)} |")
        out.append("")
    if spend_usd is not None:
        out.append(f"<sub>Judgments by TypeSafe Jev; spend this run ${spend_usd:.4f}. Scores rank; they are not proof.</sub>")
    return "\n".join(out)


def to_json(findings: list[Finding], weak: list[WeakTest]) -> str:
    return json.dumps({"survivors": [f.to_dict() for f in findings],
                       "weak_tests": [w.to_dict() for w in weak]}, indent=1)


def sarif(findings: list[Finding], weak: list[WeakTest], weak_threshold: float = 0.8) -> str:
    results = []
    for f in findings:
        if f.bucket == "ignore":
            continue
        m = f.mutant
        results.append({
            "ruleId": "surviving-mutant",
            "level": "warning" if f.bucket == "act" else "note",
            "message": {"text": f"No test fails when `{m.line_before.strip()}` becomes `{m.line_after.strip()}` "
                                f"(score {f.score:.2f}; {f.reason})."},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": m.file},
                                                "region": {"startLine": m.line}}}],
        })
    for w in weak:
        if w.score < weak_threshold:
            continue
        results.append({
            "ruleId": "weak-test",
            "level": "note",
            "message": {"text": f"Test '{w.test.name}' may not check what its name claims "
                                f"({', '.join(w.flags) or 'low assertion strength'}; score {w.score:.2f})."},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": w.test.file},
                                                "region": {"startLine": w.test.line}}}],
        })
    return json.dumps({
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{"tool": {"driver": {"name": "jev-test-triage", "informationUri":
                                      "https://github.com/cdubiel08/jev-test-triage",
                                      "rules": [{"id": "surviving-mutant"}, {"id": "weak-test"}]}},
                  "results": results}],
    }, indent=1)


AGENT_PROMPT = """\
You are improving this repository's tests. A mutation-testing run found code changes
("mutants") that no test detects, and a triage step ranked them. For each mutant below:

1. Read the code around the location. Decide whether the mutant is equivalent (no input
   can tell the two versions apart). If it is, say so in your summary and move on.
2. Otherwise write the smallest focused test, in the existing test file and style for
   that module, that PASSES on the current code and would FAIL if the line were changed
   as described. Assert on specific values, not truthiness.
3. Verify: run the new test; then temporarily apply the mutation, confirm the test fails,
   and revert the mutation. Never leave source code modified.

Do not change production code. Do not weaken or delete existing tests. Keep each test
independent. Finish with a short summary table: mutant, test added (or "equivalent"),
and whether you confirmed the test fails on the mutant.

{weak_section}
## Mutants ({n} items; highest priority first)
{items}
"""


def agent_prompt(findings: list[Finding], weak: list[WeakTest], top: int = 15,
                 weak_threshold: float = 0.8) -> str:
    items = []
    for i, f in enumerate([f for f in findings if f.bucket in ("act", "review")][:top], 1):
        m = f.mutant
        items.append(f"{i}. `{m.file}:{m.line}` ({f.bucket}, score {f.score:.2f}; {f.reason})\n"
                     f"   - current: `{m.line_before.strip()}`\n   - mutated: `{m.line_after.strip()}`")
    flagged = [w for w in weak if w.score >= weak_threshold][:top]
    ws = ""
    if flagged:
        ws = ("## Weak tests to strengthen\nThese tests may not check what their names claim. Strengthen "
              "their assertions so they pin the named behavior with specific expected values.\n" +
              "\n".join(f"- `{w.test.file}:{w.test.line}` {w.test.name} ({', '.join(w.flags) or 'low strength'})"
                        for w in flagged) + "\n")
    return AGENT_PROMPT.format(n=len(items), items="\n".join(items) or "(none)", weak_section=ws)
