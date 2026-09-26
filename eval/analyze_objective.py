"""Do weakness scores predict which tests miss the mutants they execute?

Target: miss rate = 1 - (mutants a test kills / mutants its execution covers), ranked
within each repository (repos differ in structure). Metric: Spearman between a score and
that within-repo rank, with a cluster bootstrap over test groups (file + class/describe).

Usage: uv run python eval/analyze_objective.py <tests_obj.jsonl> <results_dir> --variants b0,b1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, spearmanr

sys.path.insert(0, str(Path(__file__).parent))
from common import group_of, read_jsonl

from jev_test_triage.assertions import features


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tests", type=Path)
    ap.add_argument("results", type=Path)
    ap.add_argument("--variants", default="b0,b1")
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args()
    rows = read_jsonl(a.tests)
    rows = [r for r in rows if r["n_cov"] >= 5]
    miss = np.array([1 - r["n_kill"] / r["n_cov"] for r in rows])
    ds = np.array([r["dataset"] for r in rows])
    target = np.zeros(len(rows))
    for d in set(ds):
        ix = ds == d
        target[ix] = rankdata(miss[ix]) / ix.sum()
    groups = np.array([group_of("tests", r) for r in rows])

    scores: dict[str, np.ndarray] = {}
    for v in a.variants.split(","):
        rec = {x["uid"]: x for x in read_jsonl(a.results / f"tests-{v}-all.jsonl")}
        scores[v] = np.array([rec[r["uid"]]["score"] for r in rows])
        if v == "b1":
            for k in ("passes_if_wrong", "name_mismatch", "strength"):
                sgn = -1 if k == "strength" else 1
                scores[f"b1:{k}"] = sgn * np.array([rec[r["uid"]]["features"][k] for r in rows])
    feats = [features(r["code"], r["lang"]) for r in rows]
    scores["code:-n_assert"] = -np.array([f["n_assert"] for f in feats], dtype=float)
    scores["code:-frac_exact"] = -np.array([f["frac_exact"] for f in feats])
    scores["code:loose_or_mock"] = np.array([f["loose_only"] + f["mock_only"] + f["no_assertion"] for f in feats])

    ug = np.unique(groups)
    gi = {g: np.where(groups == g)[0] for g in ug}
    rng = np.random.default_rng(0)
    boots = [np.concatenate([gi[g] for g in rng.choice(ug, len(ug))]) for _ in range(a.boot)]
    print(f"n={len(rows)} tests (n_cov>=5), {len(ug)} groups, datasets={sorted(set(ds))}")
    for v, s in scores.items():
        rho = spearmanr(s, target).statistic
        bs = np.array([spearmanr(s[ix], target[ix]).statistic for ix in boots])
        lo, hi = np.nanpercentile(bs, [2.5, 97.5])
        per = "  ".join(f"{d}:{spearmanr(s[ds == d], target[ds == d]).statistic:+.2f}" for d in sorted(set(ds)))
        print(f"  {v:20} rho {rho:+.3f} [{lo:+.3f},{hi:+.3f}]   {per}")


if __name__ == "__main__":
    main()
