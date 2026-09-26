"""Compare variants: AUPRC, AUROC, precision@20%, Spearman, with paired cluster bootstrap.

Usage:
  uv run python eval/analyze.py <task> <results_dir> <data_dir> <label_dir> --split dev \
      --variants a0,a1,a1+lr,code+lr --ref a0

Variant names:
  <v>          scores saved by experiment.py
  <v>+lr       logistic regression on <v>'s features (plus code features), out-of-fold by
               group on the evaluated split; with --fit dev on the lockbox, fit on dev
  code+lr      logistic regression on code-only features (no Jev)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import GroupKFold

sys.path.insert(0, str(Path(__file__).parent))
from common import load, read_jsonl

from jev_test_triage.assertions import features as assertion_features

MUTATOR_KINDS = ["arith", "compare", "boolop", "not", "bool_const", "number", "string", "return", "other"]


def mutator_kind(m: str) -> str:
    m = m.lower()
    if re.search(r"(add|sub|mult|div|mod|arithmetic)", m):
        return "arith"
    if re.search(r"(eq|lt|gt|in->|is->|equality|conditionalexpression)", m):
        return "compare"
    if re.search(r"(and|or|logical)", m):
        return "boolop"
    if "not" in m or "unary" in m:
        return "not"
    if re.search(r"(true|false|boolean)", m):
        return "bool_const"
    if re.search(r"(^-?\d|->\d|numeric)", m):
        return "number"
    if re.search(r"(str|string|regex)", m):
        return "string"
    if "return" in m or "block" in m or "arrow" in m:
        return "return"
    return "other"


def code_features(task: str, r: dict) -> dict[str, float]:
    if task == "survivors":
        m = r["mutant"]
        k = mutator_kind(m["mutator"])
        f = {f"kind_{x}": float(k == x) for x in MUTATOR_KINDS}
        f["module_level"] = float(m.get("qualname", "x") in ("", "<module>") and m["lang"] == "python")
        f["no_coverage"] = float(m["status"] == "no_coverage")
        f["is_ts"] = float(m["lang"] != "python")
        line = m["line_before"]
        f["log_line"] = float(bool(re.search(r"\b(log|logger|console|logging)\.\w+\(", line)))
        f["raise_line"] = float(bool(re.search(r"\b(raise|throw)\b", line)))
        f["const_assign"] = float(bool(re.match(r"^\s*(export\s+)?(const\s+)?[A-Z][A-Z0-9_]+\s*[:=]", line)))
        return f
    f = assertion_features(r["code"], r["lang"])
    f["n_assert"] = min(f["n_assert"], 20) / 20
    f["n_exact"] = min(f["n_exact"], 20) / 20
    f["n_loose"] = min(f["n_loose"], 10) / 10
    f["n_mock"] = min(f["n_mock"], 10) / 10
    f["n_lines"] = min(f["n_lines"], 80) / 80
    f["is_ts"] = float(r["lang"] != "python")
    return f


def metrics(y: np.ndarray, s: np.ndarray, level: np.ndarray) -> dict[str, float]:
    k = max(1, round(0.2 * len(y)))
    top = np.argsort(-s, kind="stable")[:k]
    return {
        "auprc": average_precision_score(y, s),
        "auroc": roc_auc_score(y, s),
        "p@20%": float(y[top].mean()),
        "spearman": float(spearmanr(s, level).statistic),
    }


def lr_scores(X_fit, y_fit, g_fit, X_eval=None) -> np.ndarray:
    """Out-of-fold (X_eval None) or fit-then-predict logistic regression."""
    def model():
        return LogisticRegression(C=0.5, max_iter=2000, class_weight="balanced")
    if X_eval is not None:
        return model().fit(X_fit, y_fit).predict_proba(X_eval)[:, 1]
    out = np.zeros(len(y_fit))
    for tr, te in GroupKFold(n_splits=5).split(X_fit, y_fit, g_fit):
        out[te] = model().fit(X_fit[tr], y_fit[tr]).predict_proba(X_fit[te])[:, 1]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["survivors", "tests"])
    ap.add_argument("results", type=Path, nargs="+")
    ap.add_argument("--data", type=Path, action="append", required=True)
    ap.add_argument("--labels", type=Path, action="append", required=True)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--variants", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--fit", default=None, help="fit learned variants on this split (e.g. dev)")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--by-dataset", action="store_true")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    rows = [r for d, l in zip(a.data, a.labels) for r in load(a.task, d, l)]
    by_uid = {r["uid"]: r for r in rows}

    def saved(v: str, split: str) -> dict[str, dict]:
        recs = {}
        for rd in a.results:
            p = rd / f"{a.task}-{v}-{split}.jsonl"
            if p.exists() or Path(str(p) + ".gz").exists():
                for x in read_jsonl(p):
                    recs[x["uid"]] = x
        return recs

    ev = rows if a.split == "all" else [r for r in rows if r["split"] == a.split]
    uids = [r["uid"] for r in ev]
    y = np.array([by_uid[u]["y"] for u in uids])
    level = np.array([by_uid[u]["level"] for u in uids])
    groups = np.array([by_uid[u]["group"] for u in uids])

    def matrix(v: str, split: str, us: list[str]) -> np.ndarray:
        feats = []
        jev = saved(v, split) if v != "code" else {}
        for u in us:
            f = dict(code_features(a.task, by_uid[u]))
            if v != "code":
                f |= {f"j_{k}": x for k, x in jev[u]["features"].items()}
                f["prefiltered"] = float(jev[u].get("prefiltered") is not None)
            feats.append(f)
        keys = sorted({k for f in feats for k in f})
        return np.array([[f.get(k, 0.0) for k in keys] for f in feats]), keys

    scores: dict[str, np.ndarray] = {}
    for v in a.variants.split(","):
        if v.endswith("+lr"):
            base = v[:-3]
            X, keys = matrix(base, a.split, uids)
            if a.fit:
                fit_rows = [r for r in rows if r["split"] == a.fit]
                fu = [r["uid"] for r in fit_rows]
                Xf, kf = matrix(base, a.fit, fu)
                X = np.array([[dict(zip(keys, row)).get(k, 0.0) for k in kf] for row in X])
                scores[v] = lr_scores(Xf, np.array([r["y"] for r in fit_rows]), None, X)
            else:
                scores[v] = lr_scores(X, y, groups)
        else:
            s = saved(v, a.split)
            missing = [u for u in uids if u not in s]
            if missing:
                sys.exit(f"{v}: {len(missing)} items missing from results")
            scores[v] = np.array([s[u]["score"] for u in uids])

    print(f"{a.task} split={a.split} n={len(y)} positives={int(y.sum())} "
          f"({y.mean():.0%}) label agreement={np.mean([by_uid[u]['agree'] for u in uids]):.0%}")
    ug = np.unique(groups)
    gidx = {g: np.where(groups == g)[0] for g in ug}
    rng = np.random.default_rng(0)
    boots = [np.concatenate([gidx[g] for g in rng.choice(ug, len(ug))]) for _ in range(a.boot)]
    report = {}
    ref = scores[a.ref]
    for v, s in scores.items():
        m = metrics(y, s, level)
        line = f"  {v:14} " + "  ".join(f"{k} {x:.3f}" for k, x in m.items())
        if v != a.ref:
            d = []
            for idx in boots:
                yy = y[idx]
                if yy.min() == yy.max():
                    continue
                d.append(average_precision_score(yy, s[idx]) - average_precision_score(yy, ref[idx]))
            d = np.array(d)
            lo, hi = np.percentile(d, [2.5, 97.5])
            p = 2 * min((d <= 0).mean(), (d >= 0).mean())
            line += f"  | dAUPRC vs {a.ref} {d.mean():+.3f} [{lo:+.3f},{hi:+.3f}] p={p:.3f}"
            m |= {"d_auprc": float(d.mean()), "ci": [float(lo), float(hi)], "p": float(p)}
        print(line)
        report[v] = m
        if a.by_dataset:
            ds = np.array([by_uid[u]["dataset"] for u in uids])
            for name in sorted(set(ds)):
                ix = ds == name
                if y[ix].min() != y[ix].max():
                    mm = metrics(y[ix], s[ix], level[ix])
                    print(f"      {name:14} n={ix.sum():3} pos={int(y[ix].sum()):3} "
                          + "  ".join(f"{k} {x:.3f}" for k, x in mm.items()))
    if a.json:
        a.json.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
