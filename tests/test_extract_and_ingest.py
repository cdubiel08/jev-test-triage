import json
from pathlib import Path

from jev_test_triage.diffscope import lines_spec
from jev_test_triage.mutants import stryker
from jev_test_triage.mutants.model import Mutant
from jev_test_triage.mutants.pymut import parse_lines
from jev_test_triage.survivors import build_state, describe_edit, prefilter
from jev_test_triage.testfns import extract


def test_python_tests_with_classes(tmp_path: Path):
    p = tmp_path / "test_m.py"
    p.write_text("class TestA:\n    def test_one(self):\n        assert 1\n\ndef test_two():\n    assert 2\n\ndef helper():\n    pass\n")
    names = [t.name for t in extract(p, "test_m.py")]
    assert names == ["TestA::test_one", "test_two"]


def test_js_tests_with_describe(tmp_path: Path):
    p = tmp_path / "a.test.ts"
    p.write_text("describe('outer', () => {\n  it('does a', () => {\n    expect(f('}')).toBe(1)\n  })\n})\ntest('top', () => {})\n")
    tests = extract(p, "a.test.ts")
    assert [t.name for t in tests] == ["outer > does a", "top"]
    assert tests[0].code.endswith("})")


def test_stryker_load(tmp_path: Path):
    report = {"schemaVersion": "1", "thresholds": {"high": 80, "low": 60}, "files": {"src/a.ts": {
        "language": "typescript",
        "source": "export function f(x) {\n  if (x > 1) {\n    return 1\n  }\n  return 0\n}\n",
        "mutants": [
            {"id": "1", "mutatorName": "EqualityOperator", "replacement": "x >= 1", "status": "Survived",
             "location": {"start": {"line": 2, "column": 7}, "end": {"line": 2, "column": 12}}},
            {"id": "2", "mutatorName": "BlockStatement", "replacement": "{}", "status": "Killed",
             "location": {"start": {"line": 2, "column": 13}, "end": {"line": 4, "column": 4}}},
        ]}}}
    p = tmp_path / "mutation.json"
    p.write_text(json.dumps(report))
    [m] = stryker.load(p)
    assert (m.original, m.line_after.strip(), m.status) == ("x > 1", "if (x >= 1) {", "survived")
    assert m.context.startswith("export function f(x)")


def test_prefilter_and_describe():
    m = Mutant(id="1", lang="python", file="m.py", line=3, mutator="str->XX", original='"a"',
               replacement="'XXaXX'", status="survived", line_before='Kind = Literal["a", "b"]', qualname="<module>")
    assert prefilter(m) == "type annotation (Literal)"
    r = Mutant(id="2", lang="typescript", file="a.ts", line=4, mutator="BlockStatement",
               original="flush()", replacement="", status="survived", line_before="flush()")
    assert describe_edit(r) == "Removes `flush()` so it no longer runs."
    assert build_state(r)["edit"]["after"] == "(the statement is removed)"


def test_line_specs_round_trip():
    s = {1, 2, 3, 7, 9, 10}
    assert lines_spec(s) == "1-3,7,9-10"
    assert parse_lines(lines_spec(s)) == s
