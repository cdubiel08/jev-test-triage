"""Objective test-effectiveness labels from per-test mutation results.

For each test function: how many mutants its execution covers (n_cov) and how many of
those make it fail (n_kill). A test that runs mutated code yet fails for none of it is
"pseudo-tested": its assertions do not constrain the code it exercises.

Usage: uv run python eval/build_pertest.py spec.json out/tests_obj.jsonl

Spec:
{"repos": [
  {"dataset": "humanize", "kind": "pymut", "root": "humanize", "coverage": "pertest/humanize.cov.json",
   "mutants": ["pertest/humanize__number.jsonl"], "tests": ["tests/test_number.py"]},
  {"dataset": "casl", "kind": "stryker", "root": "casl/packages/casl-ability",
   "report": "casl/packages/casl-ability/reports/mutation.pertest.json", "tests": ["spec/ability.spec.ts"]}
]}
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from jev_test_triage.testfns import extract

COUNTED = {"killed", "survived", "Killed", "Survived"}


def norm_py(node_id: str) -> str:
    return re.sub(r"\[.*$", "", node_id.split("|")[0])


def python_repo(spec: dict, base: Path) -> list[dict]:
    root = base / spec["root"]
    cov = json.loads((base / spec["coverage"]).read_text())["files"]
    muts = [json.loads(line) for p in spec["mutants"] for line in (base / p).read_text().splitlines()]
    muts = [m for m in muts if m["status"] in COUNTED]
    # covered lines per (test id, module)
    lines_by_test: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for fname, f in cov.items():
        for ln, ctxs in f.get("contexts", {}).items():
            for c in ctxs:
                if c:
                    lines_by_test[norm_py(c)][fname].add(int(ln))
    out = []
    for rel in spec["tests"]:
        for t in extract(root / rel, rel):
            tid = f"{rel}::{t.name}"
            covered = lines_by_test.get(tid, {})
            cov_m = [m for m in muts if m["line"] in covered.get(m["file"], set())]
            kill_m = [m for m in muts if tid in m.get("killed_by", [])]
            kill_ids = {m["id"] for m in kill_m}
            out.append({**t.to_dict(), "dataset": spec["dataset"],
                        "n_cov": len({m["id"] for m in cov_m} | kill_ids), "n_kill": len(kill_ids)})
    return out


def stryker_repo(spec: dict, base: Path) -> list[dict]:
    root = base / spec["root"]
    rep = json.loads((base / spec["report"]).read_text())
    names = {t["id"]: t["name"] for f in rep.get("testFiles", {}).values() for t in f["tests"]}
    cov, kill = defaultdict(set), defaultdict(set)
    for fname, f in rep["files"].items():
        for m in f["mutants"]:
            if m["status"] not in COUNTED:
                continue
            key = f"{fname}:{m['id']}"
            for tid in m.get("coveredBy") or []:
                cov[names.get(tid, tid)].add(key)
            for tid in m.get("killedBy") or []:
                kill[names.get(tid, tid)].add(key)
    out = []
    for rel in spec["tests"]:
        for t in extract(root / rel, rel):
            n = t.name.replace(" > ", " ")
            out.append({**t.to_dict(), "dataset": spec["dataset"], "n_cov": len(cov.get(n, ())),
                        "n_kill": len(kill.get(n, ())), "matched": n in cov or n in kill})
    return out


def main() -> None:
    spec_path, out = Path(sys.argv[1]), Path(sys.argv[2])
    spec = json.loads(spec_path.read_text())
    rows = []
    for r in spec["repos"]:
        rows += python_repo(r, spec_path.parent) if r["kind"] == "pymut" else stryker_repo(r, spec_path.parent)
    seen = set()
    with out.open("w") as f:
        for r in rows:
            uid = f"{r['dataset']}/{r['file']}::{r['name']}"
            if uid in seen:
                continue
            seen.add(uid)
            r["uid"] = uid
            r["kill_ratio"] = r["n_kill"] / r["n_cov"] if r["n_cov"] else None
            f.write(json.dumps(r) + "\n")
    print(f"{len(seen)} tests", file=sys.stderr)


if __name__ == "__main__":
    main()
