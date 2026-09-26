"""Reference labels from reasoning models, blind to any Jev output.

Two independent labelers (default: Claude Opus and Claude Sonnet via `claude -p`) label
every item; items whose binary label differs go to an adjudicator. Batches are grouped
by file so a file's source is sent once per batch. Results are cached per batch.

Usage:
  uv run python eval/label.py survivors data/survivors.jsonl labels/ [--models opus,sonnet]
  uv run python eval/label.py tests data/tests.jsonl labels/
  uv run python eval/label.py adjudicate-survivors data/survivors.jsonl labels/
  uv run python eval/label.py adjudicate-tests data/tests.jsonl labels/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SURVIVOR_RUBRIC = """\
You review mutation-testing results. Each item is a mutant that SURVIVED the test suite:
the code at `line_before` was changed to `line_after` (the exact edit is `original` ->
`replacement`) and every test still passed. Decide, for each item, whether a developer
should write a test that fails on the mutant. Judge the code, not the tests, which you
cannot see. Read the surrounding code and the full file when it is given.

Fields to return per item:
- equivalent: true if NO input or environment can make the mutated program behave
  differently in any observable way (return values, state, I/O, raised exceptions).
  Also true when the edit only touches type annotations, unreachable code, or a value
  that is always overwritten before use.
- effect: one of
  "none"          equivalent, nothing observable changes;
  "message_text"  only human-readable text changes (log lines, exception or UI messages);
  "telemetry"     only analytics, metrics or tracing payloads change;
  "tuning"        a timeout, retry count, delay, backoff, batch or page size, or similar
                  knob changes and the mutated value would also be a reasonable setting;
  "behavior"      a returned value, stored or sent data, raised error, or branch
                  decision changes.
- worth: how much a test that kills this mutant is worth
  0 = no test should be written (equivalent, or only log text changes);
  1 = low value: observable, but a test would only pin an incidental detail (message
      wording, a tuning constant, a telemetry field) or needs an unrealistic input;
  2 = worth a test: a realistic input produces a wrong result, state, or decision, but
      the consequence is moderate or the path is uncommon;
  3 = high priority: a wrong result or decision on a common path, or in security,
      permission, data-integrity, or money logic.
- note: at most 20 words explaining the decision.
"""

TEST_RUBRIC = """\
You review unit tests for weak assertions. For each test you get its full name
(including describe/class context) and its source. Judge how well the assertions pin down
the behavior that the test's name describes. You cannot see the code under test.

Fields to return per item:
- weakness: 0 = strong: specific expected values (or exact error types) pin the behavior
                the name describes; plausible regressions of that behavior would fail it;
            1 = adequate: the main behavior is pinned with specific values, but a secondary
                aspect the name mentions is unchecked or one check is loose;
            2 = weak: an important behavior the name promises is not checked; or all checks
                are loose (truthy, defined, type, non-empty, "was called") where specific
                values were feasible; or expected values are computed by the code under
                test or restate its formula; or only mock interactions are checked although
                the code produces an output that could be checked;
            3 = hollow: no meaningful assertion, an assertion that cannot fail, or only
                "does not raise" when the name promises more.
  A test named for "does not raise" or "is called" that checks exactly that is not weak.
  A parametrized test that checks exact values for each case is strong.
- loose_only: all assertions are loose checks (truthy/defined/type/length>0/called).
- mock_only: all assertions are about mock or spy calls.
- restates_impl: an expected value comes from calling the code under test or repeating
  its formula.
- name_mismatch: the name promises a behavior that no assertion checks.
- no_assertion: the test contains no assertion at all (explicit or via pytest.raises /
  expect(...).toThrow / a helper that asserts).
- note: at most 20 words.
"""

SURVIVOR_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "equivalent": {"type": "boolean"},
            "effect": {"type": "string", "enum": ["none", "message_text", "telemetry", "tuning", "behavior"]},
            "worth": {"type": "integer", "minimum": 0, "maximum": 3},
            "note": {"type": "string"},
        },
        "required": ["id", "equivalent", "effect", "worth", "note"],
    }}},
    "required": ["labels"],
}

TEST_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "weakness": {"type": "integer", "minimum": 0, "maximum": 3},
            "loose_only": {"type": "boolean"},
            "mock_only": {"type": "boolean"},
            "restates_impl": {"type": "boolean"},
            "name_mismatch": {"type": "boolean"},
            "no_assertion": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["id", "weakness", "loose_only", "mock_only", "restates_impl",
                     "name_mismatch", "no_assertion", "note"],
    }}},
    "required": ["labels"],
}

ADJ_SURVIVOR = SURVIVOR_RUBRIC + """
Two reviewers labeled each item below independently and disagreed on whether a test is
worth writing (worth >= 2). Their labels are included. Re-examine the code yourself and
return your own final label; do not average theirs.
"""
ADJ_TEST = TEST_RUBRIC + """
Two reviewers labeled each item below independently and disagreed on whether the test is
weak (weakness >= 2). Their labels are included. Re-examine the test yourself and return
your own final label; do not average theirs.
"""

MAX_FILE_CHARS = 24000


def claude(system: str, prompt: str, schema: dict, model: str, timeout: int = 900) -> dict:
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--tools", "",
           "--system-prompt", system, "--json-schema", json.dumps(schema)]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, check=False)
    d = json.loads(r.stdout)
    if d.get("is_error") or not d.get("structured_output"):
        raise RuntimeError(f"labeler error: {str(d.get('result'))[:300]}")
    return {"out": d["structured_output"], "cost": d.get("total_cost_usd", 0.0)}


def survivor_item(r: dict) -> dict:
    m = r["mutant"]
    return {"id": r["uid"], "file": m["file"], "language": m["lang"], "line": m["line"],
            "original": m["original"], "replacement": m["replacement"],
            "line_before": m["line_before"].strip(), "line_after": m["line_after"].strip(),
            "enclosing_code": m["context"]}


def test_item(r: dict) -> dict:
    return {"id": r["uid"], "file": r["file"], "language": r["lang"], "name": r["name"], "code": r["code"][:20000]}


def batches(rows: list[dict], key, size: int) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = []
    for g in groups.values():
        out += [g[i : i + size] for i in range(0, len(g), size)]
    return out


def run_batches(kind: str, rows: list[dict], out_dir: Path, model: str, workers: int,
                extra: dict[str, list] | None = None) -> None:
    adjudicate = extra is not None
    is_surv = kind == "survivors"
    size = 8 if is_surv else 10
    system = (ADJ_SURVIVOR if is_surv else ADJ_TEST) if adjudicate else (SURVIVOR_RUBRIC if is_surv else TEST_RUBRIC)
    schema = SURVIVOR_SCHEMA if is_surv else TEST_SCHEMA
    key = (lambda r: r["dataset"] + "|" + r["mutant"]["file"]) if is_surv else (lambda r: r["dataset"] + "|" + r["file"])
    tag = f"{kind}-{'adj' if adjudicate else model}"
    cache_dir = out_dir / tag
    cache_dir.mkdir(parents=True, exist_ok=True)

    def work(batch: list[dict]) -> tuple[int, float]:
        h = hashlib.sha1(json.dumps([r["uid"] for r in batch]).encode()).hexdigest()[:16]
        f = cache_dir / f"{h}.json"
        if f.exists():
            return 0, 0.0
        items = [survivor_item(r) if is_surv else test_item(r) for r in batch]
        if adjudicate:
            for it in items:
                it["reviewer_labels"] = extra[it["id"]]
        payload: dict = {"items": items}
        if is_surv:
            src = batch[0]["file_source"]
            if src and len(src) <= MAX_FILE_CHARS:
                payload = {"file": batch[0]["mutant"]["file"], "full_file_source": src, "items": items}
        prompt = ("Label every item. Return one label per item id.\n\n" + json.dumps(payload, indent=1))
        for attempt in range(3):
            try:
                res = claude(system, prompt, schema, "opus" if adjudicate else model)
                got = {x["id"] for x in res["out"]["labels"]}
                missing = [it["id"] for it in items if it["id"] not in got]
                if missing:
                    raise RuntimeError(f"missing ids: {missing[:3]}")
                f.write_text(json.dumps({"uids": [r["uid"] for r in batch], **res}))
                return 1, res["cost"]
            except Exception as e:  # noqa: BLE001 - retry, then report
                err = e
        print(f"FAILED batch {h}: {err}", file=sys.stderr)
        return 0, 0.0

    bs = batches(rows, key, size)
    done, cost = 0, 0.0
    with ThreadPoolExecutor(workers) as ex:
        for n, c in ex.map(work, bs):
            done += n
            cost += c
            print(f"{tag}: +{n} batch (${cost:.2f} list)", file=sys.stderr, flush=True)
    print(f"{tag}: {len(bs)} batches, {done} new, ${cost:.2f} list-price", file=sys.stderr)


def collect(out_dir: Path, tag: str) -> dict[str, dict]:
    labels = {}
    for f in (out_dir / tag).glob("*.json"):
        for lab in json.loads(f.read_text())["out"]["labels"]:
            labels[lab["id"]] = lab
    return labels


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["survivors", "tests", "adjudicate-survivors", "adjudicate-tests"])
    ap.add_argument("data", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--models", default="opus,sonnet")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    rows = [json.loads(line) for line in a.data.read_text().splitlines()]
    models = a.models.split(",")
    if a.mode in ("survivors", "tests"):
        for m in models:
            run_batches(a.mode, rows, a.out, m, a.workers)
        return
    kind = a.mode.split("-", 1)[1]
    field, cut = ("worth", 2) if kind == "survivors" else ("weakness", 2)
    la, lb = (collect(a.out, f"{kind}-{m}") for m in models)
    disputed = [r for r in rows if r["uid"] in la and r["uid"] in lb
                and (la[r["uid"]][field] >= cut) != (lb[r["uid"]][field] >= cut)]
    extra = {r["uid"]: [dict(la[r["uid"]], id=None), dict(lb[r["uid"]], id=None)] for r in disputed}
    print(f"{len(disputed)} disputed {kind}", file=sys.stderr)
    run_batches(kind, disputed, a.out, "opus", a.workers, extra=extra)


if __name__ == "__main__":
    main()
