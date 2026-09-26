# Benchmark sources

Mutants, test functions and code excerpts in this directory come from these open-source
projects at the listed commits, under their licenses. Labels (`*/labels/`) are model
judgments produced by `eval/label.py`; `results/` holds every Jev answer and score.

| Set | Project | Commit | License | Mutated modules |
| --- | --- | --- | --- | --- |
| oss | [jd/tenacity](https://github.com/jd/tenacity) | 3e58094 | Apache-2.0 | `tenacity/wait.py`, `stop.py`, `retry.py` |
| oss | [pallets/itsdangerous](https://github.com/pallets/itsdangerous) | 672971d | BSD-3-Clause | `timed.py`, `signer.py`, `serializer.py` |
| oss | [python-humanize/humanize](https://github.com/python-humanize/humanize) | 392aef7 | MIT | `number.py`, `filesize.py` |
| oss | [pypa/packaging](https://github.com/pypa/packaging) | 7b898d9 | Apache-2.0 or BSD-2-Clause | `utils.py`, `markers.py` |
| oss | [unjs/ufo](https://github.com/unjs/ufo) | f06c800 | MIT | `src/utils.ts`, `parse.ts`, `query.ts` |
| oss | [stalniy/casl](https://github.com/stalniy/casl) | 4d09ed5 | MIT | `casl-ability/src/Rule.ts`, `RuleIndex.ts`, `utils.ts`, `AbilityBuilder.ts`, `ForbiddenError.ts` |
| confirm | [tkem/cachetools](https://github.com/tkem/cachetools) | 3c082c6 | MIT | `__init__.py`, `_cached.py`, `_cachedmethod.py`, `func.py` |
| confirm | [marshmallow-code/marshmallow](https://github.com/marshmallow-code/marshmallow) | 7f0792b | MIT | `validate.py` |
| confirm | [unjs/cookie-es](https://github.com/unjs/cookie-es) | f89ede8 | MIT | `src/cookie/*.ts`, `src/set-cookie/*.ts` |

Python mutants come from `jtt mutate-py`; TypeScript mutants from Stryker 10.
`pertest/tests_obj.jsonl.gz` holds per-test kill and coverage counts for the oss Python
projects and casl (Stryker jest runner with `disableBail`).
