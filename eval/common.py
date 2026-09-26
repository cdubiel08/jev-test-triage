"""Load datasets with merged reference labels, groups, and dev/lockbox splits."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

DEV_FRACTION = 0.6


def read_jsonl(path: Path) -> list[dict]:
    """Read path, or path + '.gz' when only the compressed copy exists."""
    if not path.exists() and Path(str(path) + ".gz").exists():
        path = Path(str(path) + ".gz")
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    return [json.loads(line) for line in text.splitlines() if line]


def _labels(label_dir: Path, tag: str) -> dict[str, dict]:
    out = {}
    for f in (label_dir / tag).glob("*.json"):
        for lab in json.loads(f.read_text())["out"]["labels"]:
            out[lab["id"]] = lab
    return out


def group_of(task: str, r: dict) -> str:
    if task == "survivors":
        m = r["mutant"]
        fn = m.get("qualname") or f"L{m.get('context_start', 0)}"
        return f"{r['dataset']}|{m['file']}|{fn}"
    name = r["name"]
    parent = name.rsplit(" > ", 1)[0] if " > " in name else name.rsplit("::", 1)[0] if "::" in name else ""
    return f"{r['dataset']}|{r['file']}|{parent}"


def split_of(group: str) -> str:
    h = int(hashlib.sha1(group.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "dev" if h < DEV_FRACTION else "lockbox"


def load(task: str, data_dir: Path, label_dir: Path, models=("opus", "sonnet")) -> list[dict]:
    """Rows with y (binary target), level (0-3 reference level), agree, group, split."""
    field = "worth" if task == "survivors" else "weakness"
    rows = read_jsonl(data_dir / f"{task}.jsonl")
    la, lb = (_labels(label_dir, f"{task}-{m}") for m in models)
    adj = _labels(label_dir, f"{task}-adj")
    out = []
    for r in rows:
        u = r["uid"]
        if u not in la or u not in lb:
            continue
        a, b = la[u][field], lb[u][field]
        agree = (a >= 2) == (b >= 2)
        if agree:
            level = (a + b) / 2
        elif u in adj:
            level = adj[u][field]
        else:
            continue  # disputed and not yet adjudicated
        g = group_of(task, r)
        out.append({**r, "y": int(level >= 2), "level": level, "agree": agree,
                    "ref_a": la[u], "ref_b": lb[u], "group": g, "split": split_of(g)})
    return out
