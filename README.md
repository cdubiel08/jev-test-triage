# jev-test-triage

Mutation testing finds code changes that no test notices. Most of those "surviving
mutants" are noise: equivalent rewrites, log and prompt wording, tuning constants, type
annotations. `jtt` ranks them by whether a test that kills them is worth writing, using
[TypeSafe](https://docs.typesafe.ai) Jev judgments composed in ordinary code. The ranked
list drops into pre-commit hooks and GitHub Actions, and can be handed to Claude Code or
Codex to write the tests.

```
changed code ──► mutate (Python: built-in; JS/TS: Stryker) ──► survivors
                                                                  │
      code prefilters ─► state built by code ─► 12 narrow Jev questions (one request)
                                                                  │
      composite score in code ─► act / review / ignore ─► summary · SARIF · agent prompt
```

On 771 labeled survivors from ten codebases, the final ranking beat the first version on
held-out data (AUPRC 0.809 vs 0.735, AUROC 0.848 vs 0.756), and 79–87% of the survivors
it marks "act" were real test gaps. The path there included a failed pre-registered
test on fresh repositories and one correction; [Results](#results) reports both. The
companion weak-test detector did not beat a plain assertion count against objective
per-test mutation data, so it ships as a deterministic lint with Jev as an opt-in
advisory.

## Install

```bash
uv tool install git+https://github.com/cdubiel08/jev-test-triage
export TYPESAFE_API_KEY=...   # https://docs.typesafe.ai
```

## Use

```bash
# Python: mutate a module against its tests, then triage
jtt mutate-py --module src/pkg/pricing.py --cmd "pytest -x -q tests/test_pricing.py" --out mutants.jsonl
jtt triage mutants.jsonl

# JavaScript/TypeScript: any mutation-testing-report-schema JSON (Stryker's json reporter)
npx stryker run --reporters json
jtt triage reports/mutation/mutation.json

# Only the lines a branch changed (what CI runs), configured by .jtt.toml
jtt ci --base origin/main

# Lint tests (deterministic; add --jev for Jev's advisory judgments)
jtt weak-tests tests/test_pricing.py
```

Each run writes `.jtt/summary.md`, `findings.json`, `results.sarif`, and `agent-prompt.md`
(instructions for a coding agent to write a killing test per finding and prove it fails
on the mutant). [`examples/demo`](examples/demo) is a small module with deliberately weak
tests to try it on.

### `.jtt.toml`

```toml
cache = ".jtt/cache"
budget_usd = 0.50                        # hard cap per run

[[python.targets]]
modules = "src/*"                        # fnmatch against changed files
requires = "tests/unit/{subdir}/test_{stem}.py"   # skip modules without a test file
cmd = "uv run pytest -x -q tests/unit/{subdir}/test_{stem}.py"
# placeholders: {module} {stem} {parent} {subdir} (parent minus its first segment)

stryker_reports = ["reports/mutation/mutation.json"]

[routing]                                # optional; defaults were fitted on the dev split
act = 0.805
ignore = 0.654
ignore_dunder_all = false                # true skips `__all__` entries (fine for apps, not libraries)
```

## Drop into CI

### pre-commit

```yaml
- repo: https://github.com/cdubiel08/jev-test-triage
  rev: v0.1.1
  hooks:
    - id: jtt-weak-tests   # lint staged tests; free, no key
    - id: jtt-mutants      # mutate staged Python lines + triage; pre-push stage by default
```

Without `TYPESAFE_API_KEY` the triage hook prints a notice and passes; local hooks never
block on a missing key.

### GitHub Actions

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
# ...set up your project so its tests run...
- id: jtt
  uses: cdubiel08/jev-test-triage@v0.1.1
  with:
    typesafe-api-key: ${{ secrets.TYPESAFE_API_KEY }}
    fail-on: never          # or act / review
```

To triage reports you produce yourself (for example a Stryker step), pass
`reports: path/to/mutation.json` instead of letting the action mutate changed Python
lines. Outputs: `act`, `review`, `weak`, `has_findings`, `prompt_file`, `sarif_file`. The step
summary shows the ranked table. In CI a missing key fails the step (`--require-key`), so a
run cannot go green having judged nothing.

To have an agent write the tests, see the two complete workflows:

- [`examples/github/jtt-claude-code.yml`](examples/github/jtt-claude-code.yml):
  `anthropics/claude-code-action@v1` writes and pushes tests when a maintainer adds the
  `write-tests` label.
- [`examples/github/jtt-codex.yml`](examples/github/jtt-codex.yml): the same with
  `openai/codex-action@v1`.

Both run the agent only for same-repository branches, never forks, and only on a label
a maintainer applies, because the agent reads code from the pull request.

## How it works

The design follows TypeSafe's
[building guide](https://docs.typesafe.ai/concepts/how-to-build-with-system-one): code owns
the workflow; the model answers narrow, typed questions; code composes the answers.

1. **Code first.** Deterministic filters drop mutants that cannot matter
   (`typing.Literal` strings, `TypeVar` names) without a model call.
   Code also classifies the edit (operator, number, string, boolean, return, removal).
2. **State built by code.** The enclosing function, the edited line before and after, a
   plain-language description of the edit (Stryker's "replace with `;`" becomes "Removes
   `flush()` so it no longer runs"), and, for top-level constants, the lines that use them.
3. **Speculative fan-out.** One request asks 12 narrow questions: does the result or the
   branch change, is the edit equivalent, is it only prose, telemetry, a tuning knob, a
   type or name, or the same truthiness; is the difference reachable with ordinary
   input; does the line decide access or money; how much harm (a Score); what kind of logic
   (a Choice).
4. **Composite score in code.** `behavior − benign + harm`, where "benign" uses only the
   explanations that can apply to this kind of edit (a tuning value cannot explain an
   operator swap). Every raw answer is kept in `findings.json`.
5. **Confidence routing.** Score bands send findings to *act* (write a test), *review*
   (escalate to an agent or a person), or *ignore*.

## Results

### Data and labels

| Set | Codebases | Survivors | Used for |
| --- | --- | --- | --- |
| Private | one production monorepo (3 Python + 3 TypeScript modules) | 211 | dev + lockbox |
| OSS | tenacity, itsdangerous, humanize, packaging (Python); ufo, casl (TypeScript) | 383 | dev + lockbox |
| Confirmation | cachetools, marshmallow (Python); cookie-es (TypeScript) | 177 | one final test |

Reference labels come from two reasoning models labeling blind to Jev (Claude Opus and
Claude Sonnet, rubric in [`eval/label.py`](eval/label.py)); disagreements on the binary
target "a test is worth writing" (worth ≥ 2 of 3) went to an Opus adjudicator. Agreement
was κ = 0.86 on the private set, 0.62 on OSS, and 0.73 on the confirmation set. The split is by function
(60% dev / 40% lockbox), so mutants of one function never straddle it. As an objective
check, none of the 290 *killed* mutants (whose behavior change is proven) was dropped by
the prefilters.

### Iterations (pooled dev, n = 418)

| Variant | Change | AUPRC | AUROC |
| --- | --- | --- | --- |
| a0 | first version: 4 questions, replace-this-with-that state, multiplicative formula | 0.726 | 0.719 |
| a1 | before/after lines, usages of changed names, 11 atomic questions, structured criteria | 0.778 | 0.743 |
| a2 | code prefilters (incl. `__all__`), code-written edit description, reworded prose/telemetry questions | 0.836 | 0.847 |
| a2add | additive instead of multiplicative composition | 0.843 | 0.851 |
| a3 | benign explanations gated by edit kind; `same_truthiness` question | 0.859 | 0.871 |
| **final** | a3 without the `__all__` prefilter (see below) | 0.821 | 0.822 |

a3 vs a2add: +0.016 AUPRC, 95% CI [−0.010, +0.043], p = 0.21, so iteration stopped
there and a3 was registered as the version to test. Two patterns from the docs did not
help here: a logistic regression over all answers (0.807, below the hand composition at
this sample size) and gating "act" on the harm Score's confidence (no gain over score
bands). Code-only features (mutator kind, line shape) reached 0.58 and did not transfer
between repositories.

### Held-out results

| Split | n | a0 | a3 (registered) | final | final − a0 (95% CI) |
| --- | --- | --- | --- | --- | --- |
| Lockbox | 176 | 0.743 | 0.849 | 0.867 | +0.125 [+0.035, +0.237], p = 0.002 |
| Confirmation (3 new repos) | 177 | 0.724 | **0.693** | 0.743 | +0.018 [−0.088, +0.113], p = 0.71 |
| Pooled held-out | 353 | 0.735 | 0.778 | 0.809 | +0.075 [+0.004, +0.146], p = 0.04 |

AUPRC; positives are 45–49% of each split. Bootstrap CIs resample function clusters.

What happened, in order:

1. The registered version (a3) improved on the lockbox (+0.111, p = 0.11: right direction,
   underpowered at 53 clusters) and **did not replicate on the confirmation set**
   (−0.024, p = 0.79). Its "act" precision there fell from 85% to 67%.
2. Diagnosis on the confirmation set: most of the loss came from one prefilter. It
   skipped `__all__` entries because the private application's labels rated them low
   value; in a library (cachetools) `__all__` is public API and the labelers rated
   breaking it worth a test. The rule had encoded one repository's policy.
3. That prefilter became opt-in (`ignore_dunder_all`). The final version is a3 without it.

The final version's numbers are exploratory: the change was motivated by the
confirmation set, and it is a second look at the lockbox. Its AUROC gain on pooled
held-out data is +0.092 [+0.033, +0.163], and its Spearman correlation with the graded
label rises from 0.35 to 0.58. A clean confirmation needs another fresh set.

Routing thresholds refitted on dev (act ≥ 0.805, ignore < 0.654): "act" precision 85%
dev, 87% lockbox, 79% confirmation; "ignore" is 90–93% correct; about 45% of survivors
land in "review", the band to hand to an agent rather than act on automatically.

### Weak tests: a negative result

The first version also flagged "weak" tests with Jev. Two things undercut it:

- The reference labelers agreed poorly on which tests are weak (κ = 0.38 on the private
  tests, 0.57 on OSS; on "the name promises something unchecked" κ = 0.20), and only 2–4%
  of tests were labeled weak at all. The mechanical flags they agreed on (loose-only
  κ = 0.91, mock-only κ = 0.95) are ones code computes exactly.
- Against an objective target (per-test mutation kill matrices on 278 OSS tests: the
  share of executed mutants a test fails to catch, ranked within each repo), Jev's
  weakness composite reached Spearman 0.13–0.16 and its strength Score 0.23 [0.06, 0.39],
  while plain assertion count reached 0.31 [0.16, 0.46]. Jev added +0.02–0.04 on top of
  the count, not significant. Only 4 of 278 tests caught none of the mutants they ran.

So `jtt weak-tests` is a deterministic lint by default, and surviving-mutant triage is
the test-quality signal this project recommends: a survivor is behavior that runs under
the tests and that no assertion checks.

### Cost

Jev charges $0.042 per million input tokens. A survivor costs about 1,400 tokens
(~$0.00006); the whole study (about 7,000 requests across all variants) cost $0.38 in
Jev spend.
Reference labeling used headless Claude Code and is not part of running `jtt`.

## Reproduce

[`eval/README.md`](eval/README.md) covers building datasets from mutation runs, labeling,
running variants, and the analyses. The OSS and confirmation benchmarks (sources,
labels, and every Jev answer) are in [`eval/benchmark`](eval/benchmark), so the metrics
above can be recomputed without API keys.

## Limitations

- Labels are model judgments, not ground truth; agreement on OSS code was only
  substantial (κ = 0.62–0.73). "Worth a test" also depends on the repository (library vs
  application), which a global ranker cannot know.
- The final version's held-out gain is exploratory (see above); the pre-registered
  version did not beat the first version on fresh repositories.
- The Python mutator is deliberately small (operator, constant, and return mutations).
- Jev's answers can change between model versions; the model is pinned to `jev-1.13.0`
  (`JTT_MODEL` overrides it) and thresholds should be refitted after an upgrade.

## License

MIT
