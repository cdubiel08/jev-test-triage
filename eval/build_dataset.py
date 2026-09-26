"""Build an eval dataset (survivors, a killed-mutant sample, and test functions) from a spec.

Usage: uv run python eval/build_dataset.py spec.json out_dir/

Spec format:
{
  "seed": 7,
  "killed_per_dataset": 25,
  "mutants": [
    {"dataset": "tenacity", "kind": "pymut", "path": "runs/x.jsonl", "root": "repos/tenacity"},
    {"dataset": "ufo", "kind": "stryker", "path": "repos/ufo/reports/mutation.json", "root": "repos/ufo"},
    {"dataset": "priv", "kind": "legacy", "path": "old.jsonl", "file_source": "snapshot_of_module.py"}
  ],
  "tests": [
    {"dataset": "tenacity", "root": "repos/tenacity", "files": ["tests/test_tenacity.py"], "max_per_file": 40},
    {"dataset": "priv", "records": "weak_tests.json"}
  ]
}
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

from jev_test_triage.mutants import stryker
from jev_test_triage.mutants.model import Mutant
from jev_test_triage.testfns import extract

ALL = {"survived", "no_coverage", "killed"}


def legacy(path: Path, file_source: str) -> list[Mutant]:
    """Records from the first-generation mutator (no columns, no line text)."""
    lines = file_source.splitlines()
    out = []
    for raw in path.read_text().splitlines():
        r = json.loads(raw)
        before = lines[r["line"] - 1] if 0 < r["line"] <= len(lines) else ""
        first = r["original"].split("\n")[0]
        after = before.replace(first, r["mutated"].split("\n")[0], 1) if first in before else before
        ctx, start = r["function_source"], 0
        if ctx and ctx in file_source:
            start = file_source[: file_source.index(ctx)].count("\n") + 1
        if not ctx:
            lo = max(0, r["line"] - 9)
            ctx, start = "\n".join(lines[lo : r["line"] + 8]), lo + 1
        out.append(Mutant(
            id=f"{r['module']}:{r['line']}:{r['id'].rsplit('-', 1)[-1]}", lang="python",
            file=r["module"], line=r["line"], mutator=r["mutation"], original=r["original"],
            replacement=r["mutated"], status=r["status"], line_before=before, line_after=after,
            qualname=r.get("qualname", ""), context=ctx, context_start=start,
        ))
    return out


def committed(root: Path, rel: str) -> str:
    """The committed file (mutation tools edit the working tree in place), else the file on disk."""
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else (root / rel).read_text()


def main() -> None:
    spec_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    spec = json.loads(spec_path.read_text())
    base = spec_path.parent
    rng = random.Random(spec.get("seed", 7))
    out_dir.mkdir(parents=True, exist_ok=True)
    survivors, killed, tests = [], [], []

    for src in spec["mutants"]:
        p = base / src["path"]
        if src["kind"] == "stryker":
            muts = stryker.load(p, ALL)
            data = json.loads(p.read_text())
            sources = {k: v["source"] for k, v in data["files"].items()}
        elif src["kind"] == "pymut":
            muts = [Mutant.from_dict(json.loads(line)) for line in p.read_text().splitlines()]
            root = base / src["root"]
            sources = {f: committed(root, f) for f in {m.file for m in muts}}
        else:
            fs = (base / src["file_source"]).read_text()
            muts = legacy(p, fs)
            sources = {m.file: fs for m in muts}
        for m in muts:
            rec = {"uid": f"{src['dataset']}/{m.id}", "dataset": src["dataset"],
                   "mutant": m.to_dict(), "file_source": sources.get(m.file, "")}
            (killed if m.status == "killed" else survivors).append(rec)

    # A small sample of killed mutants: their behavior change is proven by a failing test.
    by_ds: dict[str, list] = {}
    for r in killed:
        by_ds.setdefault(r["dataset"], []).append(r)
    k = spec.get("killed_per_dataset", 25)
    killed = [r for rs in by_ds.values() for r in rng.sample(rs, min(k, len(rs)))]

    for src in spec.get("tests", []):
        if "records" in src:
            for r in json.loads((base / src["records"]).read_text()):
                tests.append({"uid": f"{src['dataset']}/{r['file']}::{r['name']}", "dataset": src["dataset"],
                              "lang": r["lang"], "file": r["file"], "line": r["line"],
                              "name": r["name"], "code": r["code"]})
            continue
        root = base / src["root"]
        for rel in src["files"]:
            fns = extract(root / rel, rel)
            if len(fns) > src.get("max_per_file", 10**9):
                fns = sorted(rng.sample(fns, src["max_per_file"]), key=lambda t: t.line)
            for t in fns:
                tests.append({"uid": f"{src['dataset']}/{rel}::{t.name}", "dataset": src["dataset"], **t.to_dict()})

    seen = set()
    tests = [t for t in tests if not (t["uid"] in seen or seen.add(t["uid"]))]
    for name, rows in (("survivors", survivors), ("killed", killed), ("tests", tests)):
        with (out_dir / f"{name}.jsonl").open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"{name}: {len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
