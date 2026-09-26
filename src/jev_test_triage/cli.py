"""jtt: triage surviving mutants and weak tests with TypeSafe Jev.

  jtt triage REPORT...          rank survivors from Stryker mutation.json / pymut JSONL
  jtt weak-tests FILE...        score test functions (pre-commit friendly)
  jtt mutate-py ...             mutate one Python module (see `jtt mutate-py -h`)
  jtt ci                        mutate changed Python lines, triage, score changed tests
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path

from . import report
from .diffscope import changed_lines
from .jev import BudgetExceeded, Jev, JevFatal
from .mutants import pymut, stryker
from .mutants.model import Mutant
from .survivors import Finding, Routing, triage
from .testfns import extract
from .weak import WeakTest, score_tests

TEST_GLOBS = ["test_*.py", "*_test.py", "*.test.ts", "*.test.tsx", "*.spec.ts", "*.spec.tsx",
              "*.test.js", "*.spec.js", "*.test.mjs"]


def load_config(path: Path | None) -> dict:
    for p in [path] if path else [Path(".jtt.toml"), Path("pyproject.toml")]:
        if p and p.exists():
            d = tomllib.loads(p.read_text())
            return d.get("tool", {}).get("jtt", d) if p.name == "pyproject.toml" else d
    return {}


def make_jev(a, cfg: dict) -> Jev:
    cache = Path(a.cache or cfg.get("cache", ".jtt/cache"))
    budget = a.budget if a.budget is not None else cfg.get("budget_usd", 1.0)
    return Jev(cache_dir=cache, ledger=cache / "ledger.json" if a.cumulative_budget else None,
               budget_usd=budget, offline=a.offline)


def load_mutants(paths: list[Path], repo: Path) -> tuple[list[Mutant], dict[str, str]]:
    muts: list[Mutant] = []
    sources: dict[str, str] = {}
    for p in paths:
        if p.suffix == ".jsonl":
            for line in p.read_text().splitlines():
                m = Mutant.from_dict(json.loads(line))
                muts.append(m)
                f = repo / m.file
                if m.file not in sources and f.exists():
                    sources[m.file] = f.read_text()
        else:
            data = json.loads(p.read_text())
            muts += stryker.load(p)
            sources |= {k: v["source"] for k, v in data["files"].items()}
    return [m for m in muts if m.status in ("survived", "no_coverage")], sources


def write_outputs(out_dir: Path, findings: list[Finding], weak: list[WeakTest], a, spend: float) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = report.markdown(findings, weak, a.top, a.weak_threshold, spend)
    (out_dir / "summary.md").write_text(md)
    (out_dir / "findings.json").write_text(report.to_json(findings, weak))
    (out_dir / "results.sarif").write_text(report.sarif(findings, weak, a.weak_threshold))
    (out_dir / "agent-prompt.md").write_text(report.agent_prompt(findings, weak, a.top, a.weak_threshold))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        n_act = sum(f.bucket == "act" for f in findings)
        n_weak = sum(w.score >= a.weak_threshold for w in weak)
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"act={n_act}\nreview={sum(x.bucket == 'review' for x in findings)}\nweak={n_weak}\n"
                    f"has_findings={'true' if n_act or n_weak else 'false'}\n"
                    f"prompt_file={out_dir / 'agent-prompt.md'}\n")
    return md


def exit_code(findings: list[Finding], weak: list[WeakTest], a) -> int:
    if a.fail_on == "act" and any(f.bucket == "act" for f in findings):
        return 1
    if a.fail_on == "review" and any(f.bucket in ("act", "review") for f in findings):
        return 1
    if a.fail_on_weak and any(w.score >= a.weak_threshold for w in weak):
        return 1
    return 0


def tests_in(files: list[Path], changed: dict[str, set[int]] | None) -> list:
    out = []
    for p in files:
        if not p.exists() or not any(fnmatch.fnmatch(p.name, g) for g in TEST_GLOBS):
            continue
        for t in extract(p, str(p)):
            if changed is None or changed.get(str(p), set()) & set(range(t.line, t.end_line + 1)):
                out.append(t)
    return out


def cmd_triage(a, cfg) -> int:
    muts, sources = load_mutants(a.reports, Path(a.repo))
    jev = make_jev(a, cfg)
    routing = Routing(**cfg.get("routing", {}))
    try:
        findings = triage(muts, jev, sources, routing)
    finally:
        jev.close()
    print(write_outputs(Path(a.out_dir), findings, [], a, jev.ledger.usd))
    return exit_code(findings, [], a)


def cmd_weak(a, cfg) -> int:
    changed = changed_lines(None, staged=True) if a.changed_only else None
    tests = tests_in(a.files, changed)
    if not tests:
        return 0
    jev = make_jev(a, cfg) if a.jev else None
    try:
        weak = score_tests(tests, jev)
    finally:
        if jev:
            jev.close()
    print(write_outputs(Path(a.out_dir), [], weak, a, jev.ledger.usd if jev else None))
    return exit_code([], weak, a)


def cmd_ci(a, cfg) -> int:
    repo = Path(a.repo)
    changed = changed_lines(None if a.staged else a.base, repo, staged=a.staged)
    reports: list[Path] = []
    work = Path(a.out_dir) / "mutants"
    work.mkdir(parents=True, exist_ok=True)
    for target in cfg.get("python", {}).get("targets", []):
        for f, lines in changed.items():
            if not (fnmatch.fnmatch(f, target["modules"]) and f.endswith(".py") and lines):
                continue
            if any(fnmatch.fnmatch(Path(f).name, g) for g in TEST_GLOBS):
                continue
            fp = Path(f)
            fields = {"module": f, "stem": fp.stem, "parent": str(fp.parent),
                      "subdir": str(Path(*fp.parent.parts[1:])) if len(fp.parent.parts) > 1 else ""}
            if "requires" in target and not (repo / target["requires"].format(**fields)).exists():
                print(f"jtt: no tests for {f} ({target['requires'].format(**fields)}); skipped", file=sys.stderr)
                continue
            cmd = shlex.split(target["cmd"].format(**fields))
            res = pymut.mutate_module(repo, f, cmd, target.get("timeout", 120), lines,
                                      log=lambda m: print(m, file=sys.stderr))
            out = work / (f.replace("/", "__") + ".jsonl")
            out.write_text("".join(json.dumps(m.to_dict()) + "\n" for m in res))
            reports.append(out)
    for pattern in cfg.get("stryker_reports", []):
        reports += [p for p in repo.glob(pattern) if p.exists()]
    muts, sources = load_mutants(reports, repo) if reports else ([], {})
    tests = tests_in([repo / f for f in changed], {str(repo / f): v for f, v in changed.items()})
    print(f"jtt: {len(changed)} changed files, {len(reports)} mutation reports, {len(muts)} survivors, "
          f"{len(tests)} changed tests", file=sys.stderr)
    jev = make_jev(a, cfg)
    try:
        findings = triage(muts, jev, sources, Routing(**cfg.get("routing", {}))) if muts else []
        weak = score_tests(tests, jev if a.jev else None) if tests and not a.no_weak else []
    finally:
        jev.close()
    print(write_outputs(Path(a.out_dir), findings, weak, a, jev.ledger.usd))
    return exit_code(findings, weak, a)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["mutate-py"]:
        return pymut.main(argv[1:])
    ap = argparse.ArgumentParser(prog="jtt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", type=Path)
        p.add_argument("--repo", default=".")
        p.add_argument("--out-dir", default=".jtt")
        p.add_argument("--cache", help="response cache directory (default .jtt/cache)")
        p.add_argument("--budget", type=float, help="max Jev spend in USD for this run (default 1.0)")
        p.add_argument("--cumulative-budget", action="store_true", help="apply the budget across runs via a ledger")
        p.add_argument("--offline", action="store_true", help="replay cached answers only")
        p.add_argument("--top", type=int, default=20)
        p.add_argument("--weak-threshold", type=float, default=0.8)
        p.add_argument("--fail-on", choices=["never", "act", "review"], default="never")
        p.add_argument("--fail-on-weak", action="store_true")
        p.add_argument("--require-key", action="store_true",
                       help="exit 2 when TYPESAFE_API_KEY is missing (CI) instead of skipping (pre-commit)")

    p = sub.add_parser("triage", help="rank surviving mutants")
    p.add_argument("reports", nargs="+", type=Path)
    common(p)
    p = sub.add_parser("weak-tests", help="score test functions")
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument("--changed-only", action="store_true", help="only tests that overlap staged changes")
    p.add_argument("--jev", action="store_true", help="add Jev's advisory judgments (needs TYPESAFE_API_KEY)")
    common(p)
    p = sub.add_parser("ci", help="mutate changed lines, triage, and score changed tests")
    p.add_argument("--base", default=os.environ.get("JTT_BASE", "origin/main"))
    p.add_argument("--staged", action="store_true", help="use staged changes instead of --base (pre-commit)")
    p.add_argument("--jev", action="store_true", help="also ask Jev about changed tests (advisory)")
    p.add_argument("--no-weak", action="store_true")
    common(p)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    needs_key = a.cmd != "weak-tests" or a.jev
    if needs_key and not os.environ.get("TYPESAFE_API_KEY") and not a.offline:
        # Locally, never block a commit on a missing key. In CI (--require-key) a run that
        # judged nothing must not look green.
        print("jtt: TYPESAFE_API_KEY is not set; " + ("failing." if a.require_key else "skipping."), file=sys.stderr)
        sys.exit(2 if a.require_key else 0)
    try:
        rc = {"triage": cmd_triage, "weak-tests": cmd_weak, "ci": cmd_ci}[a.cmd](a, cfg)
    except JevFatal as e:
        print(f"jtt: {e}", file=sys.stderr)
        rc = 3
    except BudgetExceeded as e:
        print(f"jtt: {e}", file=sys.stderr)
        rc = 4
    sys.exit(rc)


if __name__ == "__main__":
    main()
