"""Confidence-gated routing: pick act/ignore thresholds on one split, check them on another.

act    = score >= t_act (and optional impact confidence >= c)   -> write a test
ignore = score <  t_ignore                                      -> leave it
review = everything else                                        -> escalate to an agent/human

Usage: uv run python eval/routing.py <variant> --fit dev --check lockbox \
          --results R1 R2 --data D1 D2 --labels L1 L2 [--act-precision 0.85 --ignore-npv 0.9]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import load, read_jsonl


def buckets(s, conf, t_act, t_ign, c_min):
    act = (s >= t_act) & (conf >= c_min)
    ign = s < t_ign
    return act, ign & ~act, ~act & ~ign


def summarize(y, s, conf, t_act, t_ign, c_min) -> dict:
    act, ign, rev = buckets(s, conf, t_act, t_ign, c_min)
    return {
        "act_n": int(act.sum()), "act_precision": float(y[act].mean()) if act.any() else float("nan"),
        "ignore_n": int(ign.sum()), "ignore_npv": float(1 - y[ign].mean()) if ign.any() else float("nan"),
        "review_n": int(rev.sum()), "review_share": float(rev.mean()),
        "positives_caught_by_act": float(y[act].sum() / max(1, y.sum())),
        "positives_lost_to_ignore": float(y[ign].sum() / max(1, y.sum())),
    }


def fit(y, s, conf, act_precision, ignore_npv, use_conf):
    """Lowest act threshold meeting the precision target; highest ignore threshold meeting NPV."""
    best = None
    cands = np.unique(np.round(s, 3))
    for c_min in ([0.0, 0.2, 0.3, 0.4, 0.5] if use_conf else [0.0]):
        for t in cands:
            act = (s >= t) & (conf >= c_min)
            if act.sum() >= 5 and y[act].mean() >= act_precision:
                cover = y[act].sum()
                if best is None or cover > best[0]:
                    best = (cover, t, c_min)
                break
    t_act, c_min = (best[1], best[2]) if best else (1.01, 0.0)
    t_ign = 0.0
    for t in cands:
        ign = s < t
        if ign.sum() >= 5 and 1 - y[ign].mean() >= ignore_npv and t <= t_act:
            t_ign = t
    return float(t_act), float(t_ign), float(c_min)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("variant")
    ap.add_argument("--fit", default="dev")
    ap.add_argument("--check", default=None)
    ap.add_argument("--results", type=Path, nargs="+", required=True)
    ap.add_argument("--data", type=Path, nargs="+", required=True)
    ap.add_argument("--labels", type=Path, nargs="+", required=True)
    ap.add_argument("--act-precision", type=float, default=0.85)
    ap.add_argument("--ignore-npv", type=float, default=0.90)
    a = ap.parse_args()
    rows = {r["uid"]: r for d, l in zip(a.data, a.labels) for r in load("survivors", d, l)}

    def arrays(split):
        recs = {}
        for rd in a.results:
            p = rd / f"survivors-{a.variant}-{split}.jsonl"
            if p.exists() or Path(str(p) + ".gz").exists():
                recs |= {x["uid"]: x for x in read_jsonl(p)}
        us = [u for u in recs if u in rows and (split == "all" or rows[u]["split"] == split)]
        y = np.array([rows[u]["y"] for u in us])
        s = np.array([recs[u]["score"] for u in us])
        conf = np.array([recs[u].get("answers", {}).get("impact", {}).get("confidence", 1.0) for u in us])
        return y, s, conf

    y, s, conf = arrays(a.fit)
    for use_conf in (False, True):
        t_act, t_ign, c_min = fit(y, s, conf, a.act_precision, a.ignore_npv, use_conf)
        label = "score+impact-confidence" if use_conf else "score only"
        print(f"[{label}] thresholds from {a.fit}: act>={t_act:.3f} ignore<{t_ign:.3f} impact_conf>={c_min}")
        print(f"   {a.fit:8}", json.dumps({k: round(v, 3) for k, v in summarize(y, s, conf, t_act, t_ign, c_min).items()}))
        if a.check:
            yc, sc, cc = arrays(a.check)
            print(f"   {a.check:8}", json.dumps({k: round(v, 3) for k, v in summarize(yc, sc, cc, t_act, t_ign, c_min).items()}))


if __name__ == "__main__":
    main()
