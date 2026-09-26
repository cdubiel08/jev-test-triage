"""Flag tests whose assertions do not pin down the behavior their names describe.

Evidence (README, "Weak tests"): against per-test mutation kill rates, the number of
assertions predicts how much executed code a test fails to check (Spearman 0.31) better
than Jev's semantic judgments (0.13-0.23), which add only ~0.03 on top. So the default is
a deterministic lint (no assertion, loose-only, mock-only, few assertions) and Jev is an
opt-in advisory layer. Surviving-mutant triage is the stronger test-quality signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typesafe_sdk import Noul, NoulCriteria, Score

from .assertions import assertion_lines
from .assertions import features as assertion_features
from .jev import Jev
from .testfns import TestFn


def build_state(t: TestFn) -> dict:
    return {"test_name": t.name, "test_code": t.code[:20000],
            "assertions": assertion_lines(t.code, t.lang) or ["(no assertion lines found)"]}


QUESTIONS = {
    "mock_only": Noul(instructions=(
        "Do all of the assertions in `test_code` check only whether mocks or spies were "
        "called (call counts or call arguments), with no assertion on a returned value, "
        "output, raised error, or resulting state?")),
    "restates_impl": Noul(instructions=(
        "Is an expected value in `test_code` produced by calling the same function under "
        "test, or by repeating its formula, instead of being written as a literal or "
        "known-correct value?")),
    "loose_only": Noul(instructions=(
        "Are all assertions in `test_code` loose checks, such as truthy, not None, "
        "toBeDefined, isinstance, a type check, or a length greater than zero, with no "
        "assertion of a specific expected value?")),
    "name_mismatch": Noul(
        instructions="Does `test_name` claim a behavior that the assertions in `test_code` do not actually check?",
        criteria=NoulCriteria(
            true="The name promises something (a value, an error, an order, a guarantee) that no assertion verifies.",
            false="Every behavior named in `test_name` is checked by at least one assertion."),
    ),
    "strength": Score(
        instructions="How precisely do the assertions in `test_code` pin down the behavior described by `test_name`?",
        criteria=[
            "Nothing meaningful is checked.",
            "Only presence, type, or that something was called.",
            "Some specific values are checked, but important outputs are left unchecked.",
            "The important outputs are checked against specific expected values.",
        ],
    ),
    "name_checked": Noul(
        instructions="Does at least one line in `assertions` directly check the behavior that `test_name` describes?",
    ),
    "passes_if_wrong": Noul(
        instructions=("Suppose the code under test returned a result of the right type and shape but with "
                      "the wrong content. Would every line in `assertions` still pass?"),
        criteria=NoulCriteria(
            true={"what": "The checks only look at presence, type, length, truthiness, or that a call happened",
                  "examples": ["expect(result).toBeDefined()", "assert isinstance(x, dict)", "assert len(items) > 0"]},
            false={"what": "At least one check compares the content to a specific expected value or exact error",
                   "examples": ["expect(result).toEqual({id: 1})", "assert total == 42", "pytest.raises(ValueError)"]}),
    ),
    "unchecked_output": Noul(
        instructions=("Does `test_code` call the code under test and then leave its main return value or "
                      "resulting state unchecked?"),
    ),
}


def features(answers: dict) -> dict[str, float]:
    f = {k: v["noul"] for k, v in answers.items() if "noul" in v}
    f["strength"] = answers["strength"]["score"] / 3
    f["strength_conf"] = answers["strength"]["confidence"]
    return f


def code_score(t: TestFn) -> tuple[float, list[str]]:
    """Deterministic lint: 1.0 no assertion, 0.9 loose checks only, 0.8 mock calls only,
    0.5 a single assertion, else 0. Helpers that assert internally are not seen."""
    c = assertion_features(t.code, t.lang)
    if c["no_assertion"] and not c["has_raises"]:
        return 1.0, ["no assertion found"]
    if c["loose_only"]:
        return 0.9, ["only loose checks"]
    if c["mock_only"]:
        return 0.8, ["only checks mock calls"]
    if c["n_assert"] <= 1 and not c["has_snapshot"]:
        return 0.5, ["single assertion"]
    return 0.0, []


def score(answers: dict, t: TestFn) -> float:
    """Code lint, raised by Jev's advisory judgments when they are confident."""
    base, _ = code_score(t)
    if not answers:
        return base
    f = features(answers)
    return max(base, 0.85 * f["name_mismatch"], 0.85 * (1 - f["strength"]))


FLAGS = {"passes_if_wrong": "would pass on a wrong result", "name_mismatch": "name promises an unchecked behavior",
         "restates_impl": "expected value restates the implementation", "mock_only": "only checks mock calls",
         "loose_only": "only loose checks"}


@dataclass
class WeakTest:
    test: TestFn
    score: float
    flags: list[str] = field(default_factory=list)
    answers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"test": self.test.to_dict(), "score": round(self.score, 4), "flags": self.flags,
                "answers": self.answers}


def score_tests(tests: list[TestFn], jev: Jev | None = None) -> list[WeakTest]:
    """Lint every test; with a Jev client, add the advisory judgments."""
    res = jev.ask_many([(build_state(t), QUESTIONS) for t in tests]) if jev else [{"answers": {}}] * len(tests)
    out = []
    for t, r in zip(tests, res):
        a = r["answers"]
        _, flags = code_score(t)
        if a:
            f = features(a)
            flags += [label for k, label in FLAGS.items()
                      if k in ("name_mismatch", "restates_impl", "passes_if_wrong") and f.get(k, 0) >= 0.5]
        out.append(WeakTest(t, score(a, t), flags, a))
    out.sort(key=lambda w: -w.score)
    return out
