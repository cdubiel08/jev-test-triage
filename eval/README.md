# Evaluation

Everything behind the numbers in the top-level README. The public benchmark (six OSS
projects for dev/lockbox, three more for confirmation) is in `benchmark/`; the private
set used the same scripts on data that is not published.

## Pipeline

1. **Mutate.** Python: `jtt mutate-py` (add `--per-test` to record which tests fail per
   mutant). JS/TS: Stryker with the json reporter (`disableBail: true` and
   `coverageAnalysis: perTest` when you need per-test kills).
2. **Build a dataset.** `build_dataset.py spec.json out/` writes `survivors.jsonl`,
   `killed.jsonl` (a sample of killed mutants, used as an objective check), and
   `tests.jsonl`. It reads Python sources from git `HEAD`, because mutation tools edit
   the working tree in place.
3. **Label.** `label.py survivors out/survivors.jsonl labels/` runs two labelers
   (`claude -p` with Opus and Sonnet, no tools, a JSON schema, and a fixed rubric) blind
   to any Jev output, then `label.py adjudicate-survivors ...` sends binary disagreements
   to an Opus adjudicator. Batches are cached per file.
4. **Run a variant.** `experiment.py survivors <variant> out/ labels/ results/ --split dev`
   (or `--prefetch` to fill the Jev cache for every item without labels). Variants live in
   `variants.py`; `ship` is the packaged implementation, evaluated exactly as it runs.
   Every Jev call goes through a disk cache and a spend ledger with a hard budget.
5. **Analyze.** `analyze.py` (AUPRC, AUROC, precision at the top 20%, Spearman with the
   graded label, and a paired cluster bootstrap over function groups), `routing.py`
   (act/ignore thresholds fitted on one split and checked on another), and
   `analyze_objective.py` (weak-test scores against per-test kill rates from
   `build_pertest.py`).

## Protocol

- Split by function group (hash of dataset, file, function): 60% dev, 40% lockbox.
- Variants were designed and compared on dev only. The first design round used errors
  from the private dev split; the OSS dev split was first looked at after variant a2.
- Stopping rule: stop when the last change is not significantly better than the one
  before it (paired bootstrap on AUPRC).
- The lockbox was evaluated once, for a0, a2add, a3 and ship. The confirmation set (three
  repositories never looked at during design) was evaluated once, for a0 and ship.

## Reproduce from the published benchmark (no API keys)

```bash
B=eval/benchmark
uv sync --extra eval
uv run python eval/analyze.py survivors $B/results/oss --data $B/oss --labels $B/oss/labels \
    --split lockbox --variants a0,a2add,a3,ship --ref a0 --by-dataset
uv run python eval/analyze.py survivors $B/results/confirm --data $B/confirm --labels $B/confirm/labels \
    --split all --variants a0,ship --ref a0 --by-dataset
uv run python eval/analyze_objective.py $B/pertest/tests_obj.jsonl.gz $B/results/obj --variants b0,b1
```

The OSS-only numbers differ from the README's pooled numbers, which include the private
set. Source repositories, commits and licenses are listed in `benchmark/SOURCES.md`.
