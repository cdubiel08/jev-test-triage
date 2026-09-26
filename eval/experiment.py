"""Run a variant over a labeled dataset and save per-item features and scores.

Usage: uv run python eval/experiment.py <task> <variant> <data_dir> <label_dir> <out_dir>
       [--split dev|lockbox|all] [--budget 15]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import variants as V
from common import load, read_jsonl

from jev_test_triage.jev import Jev

LEDGER = Path.home() / ".cache/jev-test-triage/ledger.json"
CACHE = Path.home() / ".cache/jev-test-triage/responses"


def run(task: str, variant: str, rows: list[dict], jev: Jev) -> list[dict]:
    spec = V.REGISTRY[variant]
    todo = [r for r in rows if spec.get("prefilter") is None or spec["prefilter"](r) is None]
    reqs = [(spec["state"](r), spec["questions"](r) if callable(spec["questions"]) else spec["questions"]) for r in todo]
    res = jev.ask_many(reqs) if reqs else []
    by_uid = {r["uid"]: x for r, x in zip(todo, res)}
    out = []
    for r in rows:
        x = by_uid.get(r["uid"])
        rec = {"uid": r["uid"], "dataset": r["dataset"], "group": r["group"], "split": r["split"],
               "y": r["y"], "level": r["level"], "agree": r["agree"]}
        if x is None:
            reason = spec["prefilter"](r)
            rec |= {"prefiltered": reason, "score": 0.0, "features": {}, "tokens": 0}
        else:
            a = x["answers"]
            feats = spec["features"](a, r) if "features" in spec else {}
            rec |= {"prefiltered": None, "score": spec["score"](a, r), "features": feats,
                    "answers": a, "tokens": x["input_tokens"]}
        out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["survivors", "tests", "killed"])
    ap.add_argument("variant")
    ap.add_argument("data", type=Path)
    ap.add_argument("labels", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--split", default="dev", choices=["dev", "lockbox", "all"])
    ap.add_argument("--budget", type=float, default=15.0)
    ap.add_argument("--prefetch", action="store_true", help="fill the cache for every item; no labels needed")
    a = ap.parse_args()
    if a.prefetch:
        name = "killed" if a.task == "killed" else a.task
        rows = [r | {"y": -1, "level": -1, "agree": True, "group": "", "split": "all"}
                for r in read_jsonl(a.data / f"{name}.jsonl")]
        task = "survivors" if a.task == "killed" else a.task
        a.split = "all"
    elif a.task == "killed":
        rows = [r | {"y": 1, "level": 3, "agree": True, "group": "k", "split": "all"}
                for r in read_jsonl(a.data / "killed.jsonl")]
        task = "survivors"
    else:
        rows = load(a.task, a.data, a.labels)
        task = a.task
        if a.split != "all":
            rows = [r for r in rows if r["split"] == a.split]
    jev = Jev(cache_dir=CACHE, ledger=LEDGER, budget_usd=a.budget)
    try:
        out = run(task, a.variant, rows, jev)
    finally:
        jev.close()
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / f"{a.task}-{a.variant}-{a.split}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in out))
    tok = sum(r["tokens"] for r in out)
    print(f"{a.task}/{a.variant}/{a.split}: {len(out)} items, {jev.calls} calls, {jev.hits} cached, "
          f"{tok} tokens, ledger ${jev.ledger.usd:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
