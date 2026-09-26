"""Experimental question sets, state builders and compositions for the two tasks.

Each variant maps an item to (state, questions) and composes answers into a score in
[0, 1] where higher means "more worth acting on". `features()` exposes every raw
judgment so learned compositions can use them.
"""

from __future__ import annotations

import re

from typesafe_sdk import Choice, Noul, NoulCriteria, Score

# --------------------------------------------------------------------------- task A: v0

A0_QUESTIONS = {
    "affects_behavior": Noul(
        instructions=(
            "In `code`, the expression `change.original` is replaced by `change.mutated`. "
            "Does this replacement change a value that the code returns, stores, sends to "
            "another system, or uses to decide which branch runs?"
        ),
        criteria=NoulCriteria(
            true="The replacement changes a returned, stored, or sent value, or changes which branch runs.",
            false=(
                "The replacement only changes text written to a log or a human-readable message, "
                "or both versions always produce the same result."
            ),
        ),
    ),
    "tunable_value": Noul(
        instructions=(
            "Is `change.original` a tuning value such as a timeout, retry count, delay, "
            "backoff interval, batch size, or page limit, where `change.mutated` would still "
            "be an acceptable setting?"
        ),
    ),
    "impact": Score(
        instructions=(
            "If `change.mutated` replaced `change.original` in production, how much would "
            "users or downstream systems of `code` be harmed?"
        ),
        criteria=[
            "No harm: nobody could notice the difference.",
            "Minor: slightly different timing, retries, or log output, with the same final result.",
            "Moderate: a wrong result, but only in a rare edge case or for malformed input.",
            "Severe: a wrong result, wrong permission decision, or lost or corrupted data in normal use.",
        ],
    ),
    "area": Choice(
        instructions="Which kind of logic does the changed expression `change.original` belong to?",
        criteria={
            "access_control": "Decides whether a user, role, or plan may do something.",
            "data_transformation": "Parses, maps, validates, or reshapes data.",
            "control_flow": "Decides which branch, loop, or early return runs.",
            "error_handling_retry": "Handles failures, retries, or fallbacks.",
            "telemetry": "Builds log, analytics, or metric output.",
            "configuration": "Defines a constant, default, or setting.",
        },
    ),
}


def a0_state(r: dict) -> dict:
    m = r["mutant"]
    return {"file": m["file"], "code": m["context"] or m["original"],
            "change": {"line": m["line"], "original": m["original"], "mutated": m["replacement"]}}


def a0_score(a: dict) -> float:
    return a["affects_behavior"]["noul"] * (1 - a["tunable_value"]["noul"]) * a["impact"]["score"] / 3


# --------------------------------------------------------------------------- task A: state v1

ASSIGN = re.compile(r"^\s*(?:export\s+)?(?:const\s+|let\s+|var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)")


def usages(file_source: str, line: int, limit: int = 12) -> list[str]:
    """Lines elsewhere in the file that use the name assigned on `line`."""
    lines = file_source.splitlines()
    if not (0 < line <= len(lines)):
        return []
    m = ASSIGN.match(lines[line - 1])
    if not m:
        return []
    name = m.group(1)
    pat = re.compile(rf"\b{re.escape(name)}\b")
    return [f"{i + 1}: {t.strip()}" for i, t in enumerate(lines) if i + 1 != line and pat.search(t)][:limit]


def a1_state(r: dict) -> dict:
    """Before/after lines instead of a replace instruction; usages for top-level values."""
    m = r["mutant"]
    st: dict = {
        "file": m["file"],
        "code": m["context"] or m["line_before"],
        "edit": {"line_number": m["line"], "before": m["line_before"].strip(), "after": m["line_after"].strip()},
    }
    if m.get("qualname", "") in ("", "<module>"):
        u = usages(r.get("file_source", ""), m["line"])
        if u:
            st["uses_of_changed_name"] = u
    return st


# --------------------------------------------------------------------------- task A: questions v1

def _crit(true: str, false: str) -> NoulCriteria:
    return NoulCriteria(true=true, false=false)


A1_QUESTIONS = {
    # effect kind, as independent yes/no questions
    "changes_result": Noul(
        instructions=("Compare `edit.before` with `edit.after` inside `code`. For some realistic input, "
                      "would the edited code return a different value, store or send different data, "
                      "or raise a different error?"),
        criteria=_crit("Some realistic input gives a different returned, stored, or sent value, or a different error.",
                       "Every realistic input gives the same returned, stored, and sent values and the same errors."),
    ),
    "changes_branch": Noul(
        instructions=("Compare `edit.before` with `edit.after` inside `code`. For some realistic input, "
                      "would a different branch, loop iteration, or early return run?"),
    ),
    "same_for_all_inputs": Noul(
        instructions=("Do `edit.before` and `edit.after` behave identically for every possible input, "
                      "for example because the value is never used, is overwritten, or the condition "
                      "cannot differ?"),
        criteria=_crit("No input can make the two versions behave differently.",
                       "At least one input makes the two versions behave differently."),
    ),
    "message_text_only": Noul(
        instructions=("Is the only thing that differs between `edit.before` and `edit.after` text meant for "
                      "people to read, such as a log line, an exception message, or a UI message?"),
    ),
    "telemetry_only": Noul(
        instructions=("Does the edited value only feed analytics, metrics, or tracing data, with no effect "
                      "on what the code returns or does for its caller?"),
    ),
    "tuning_knob": Noul(
        instructions=("Is the edited value a tuning setting, such as a timeout, retry count, delay, backoff, "
                      "batch size, page size, or cache lifetime, where the value in `edit.after` would "
                      "also be a reasonable setting?"),
    ),
    "type_or_name_only": Noul(
        instructions=("Does the edit only change a type annotation, a type variable name, or another "
                      "name that is never compared or looked up at runtime?"),
    ),
    # consequences
    "ordinary_input": Noul(
        instructions=("Would inputs that `code` is designed to handle in normal use expose the difference "
                      "between `edit.before` and `edit.after`, rather than only unusual, malformed, or "
                      "extreme inputs?"),
    ),
    "security_or_money": Noul(
        instructions=("Does the edited line help decide access, permissions, authentication, signatures, "
                      "money, or whether data is saved, deleted, or corrupted?"),
    ),
    "impact": Score(
        instructions=("If `edit.after` replaced `edit.before` in production, how much would users or "
                      "downstream systems of `code` be harmed?"),
        criteria=[
            {"what": "No harm", "examples": ["The two versions behave the same", "Only a log line changes"]},
            {"what": "Minor", "examples": ["Retries a little more or less", "A telemetry field is renamed"]},
            {"what": "Moderate", "examples": ["A wrong result for malformed input", "A rare edge case breaks"]},
            {"what": "Severe", "examples": ["A wrong result in normal use", "A wrong permission decision",
                                        "Data is lost or corrupted"]},
        ],
    ),
    "area": A0_QUESTIONS["area"],
}


def a1_features(a: dict) -> dict[str, float]:
    f = {k: v["noul"] for k, v in a.items() if "noul" in v}
    f["impact"] = a["impact"]["score"] / 3
    f["impact_conf"] = a["impact"]["confidence"]
    for k, p in a["area"]["probabilities"].items():
        f[f"area_{k}"] = p
    return f


def a1_score(a: dict) -> float:
    """Hand composition: behavior evidence, minus the benign explanations, times harm."""
    f = a1_features(a)
    behavior = max(f["changes_result"], f["changes_branch"]) * (1 - f["same_for_all_inputs"])
    benign = max(f["message_text_only"], f["telemetry_only"], f["tuning_knob"], f["type_or_name_only"])
    harm = 0.5 * f["impact"] + 0.25 * f["ordinary_input"] + 0.25 * f["security_or_money"]
    return behavior * (1 - benign) * harm



# --------------------------------------------------------------------------- task B: v0

B0_QUESTIONS = {
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
        criteria=_crit(
            "The name promises something (a value, an error, an order, a guarantee) that no assertion verifies.",
            "Every behavior named in `test_name` is checked by at least one assertion."),
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
}


def b0_state(r: dict) -> dict:
    return {"test_name": r["name"], "test_code": r["code"][:20000]}


def b0_score(a: dict) -> float:
    s = 1 - a["strength"]["score"] / 3
    return max(a["loose_only"]["noul"], a["restates_impl"]["noul"], a["name_mismatch"]["noul"], s,
               a["mock_only"]["noul"] * s)



# --------------------------------------------------------------------------- task A: v2

def a2_prefilter(r: dict) -> str | None:
    """Deterministic low-value cases, decided in code without a model call."""
    m = r["mutant"]
    line = m["line_before"]
    if m["lang"] == "python":
        if re.search(r"\bLiteral\[", line) and m["original"][:1] in "'\"":
            return "type annotation (Literal)"
        if re.search(r"\b(TypeVar|NewType|ParamSpec|TypeVarTuple)\(", line) and m["original"][:1] in "'\"":
            return "type variable name"
        if m.get("qualname", "") in ("", "<module>") and _in_dunder_all(r.get("file_source", ""), m["line"]):
            return "__all__ entry"
    return None


def _in_dunder_all(src: str, line: int) -> bool:
    lines = src.splitlines()
    for i in range(line - 1, max(-1, line - 200), -1):
        t = lines[i]
        if re.match(r"^__all__\s*[:=]", t):
            return True
        if i < line - 1 and re.match(r"^\S", t):
            return False
    return False


REMOVED = {"", ";", "{}", "()"}


def describe_edit(m: dict) -> str:
    """A plain-language description of the mutation, written by code."""
    orig, repl, mut = m["original"].strip(), m["replacement"].strip(), m["mutator"]
    o = orig.split("\n")[0][:160]
    if repl in REMOVED:
        return f"Removes `{o}` so it no longer runs."
    if mut == "OptionalChaining" or ("?." in orig and "?." not in repl):
        return f"Removes the optional chaining in `{o}`, so a missing value now throws instead of giving undefined."
    if repl in ("true", "True") or repl.startswith("true") and mut == "ConditionalExpression":
        return f"Replaces the condition `{o}` with `true`, so it always passes."
    if repl in ("false", "False") and mut in ("ConditionalExpression", "EqualityOperator", "LogicalOperator"):
        return f"Replaces the condition `{o}` with `false`, so it never passes."
    if mut == "return->None":
        return "Makes the function return None here instead of its computed value."
    if mut == "drop-not":
        return f"Removes a negation: `{o}` becomes `{repl[:160]}`."
    return f"Changes `{o}` to `{repl.split(chr(10))[0][:160]}`."


def a2_state(r: dict) -> dict:
    st = a1_state(r)
    st["edit"] = {"description": describe_edit(r["mutant"]), **st["edit"]}
    if r["mutant"]["replacement"].strip() in REMOVED:
        st["edit"]["after"] = "(the statement is removed)"
    return st


A2_QUESTIONS = dict(A1_QUESTIONS)
A2_QUESTIONS.pop("message_text_only")
A2_QUESTIONS["prose_only"] = Noul(
    instructions=("Is the edited value free-form prose whose exact wording no program checks, such as a log "
                  "message, an exception or UI message, a docstring, or instructions in a prompt for a "
                  "language model?"),
    criteria=_crit(
        {"what": "Only the wording of human- or model-read text changes",
         "examples": ["A log message", "An error message", "A prompt sent to an LLM"]},
        {"what": "The value is compared, parsed, looked up, or used as a key, name, or identifier",
         "examples": ["A dictionary key", "An HTTP header name", "A cookie prefix", "An enum value"]}),
)
A2_QUESTIONS["telemetry_only"] = Noul(
    instructions=("Does the edit only change which analytics, metrics, or tracing data is recorded, without "
                  "changing consent or privacy handling, stored user data, or what the caller gets back?"),
)
for k in ("changes_result", "changes_branch", "same_for_all_inputs", "ordinary_input"):
    q = A1_QUESTIONS[k]
    A2_QUESTIONS[k] = type(q)(**{**q.model_dump(exclude_none=True, exclude={"type"}),
                                 "instructions": q.instructions.replace(
                                     "Compare `edit.before` with `edit.after` inside `code`.",
                                     "`edit.description` says how `code` was changed.")})


def a2_features(a: dict) -> dict[str, float]:
    return a1_features(a)


def a2_score(a: dict) -> float:
    f = a2_features(a)
    behavior = max(f["changes_result"], f["changes_branch"]) * (1 - f["same_for_all_inputs"])
    benign = max(f["prose_only"], f["telemetry_only"], f["tuning_knob"], f["type_or_name_only"])
    harm = 0.5 * f["impact"] + 0.25 * f["ordinary_input"] + 0.25 * f["security_or_money"]
    return behavior * (1 - benign) * harm


def a2_additive(a: dict) -> float:
    """Additive variant: benign explanations subtract instead of multiplying."""
    f = a2_features(a)
    behavior = max(f["changes_result"], f["changes_branch"]) - f["same_for_all_inputs"]
    benign = max(f["prose_only"], f["telemetry_only"], f["tuning_knob"], f["type_or_name_only"])
    return (0.35 * behavior - 0.35 * benign + 0.2 * f["impact"] + 0.05 * f["ordinary_input"]
            + 0.05 * f["security_or_money"] + 0.7) / 1.4



# --------------------------------------------------------------------------- task B: v1

from jev_test_triage.assertions import assertion_lines
from jev_test_triage.assertions import features as assertion_features


def b1_state(r: dict) -> dict:
    """Name, code, and the assertion lines found by code, so questions can point at them."""
    return {"test_name": r["name"], "test_code": r["code"][:20000],
            "assertions": assertion_lines(r["code"], r["lang"]) or ["(no assertion lines found)"]}


B1_QUESTIONS = {
    **B0_QUESTIONS,
    "name_checked": Noul(
        instructions=("Does at least one line in `assertions` directly check the behavior that `test_name` "
                      "describes?"),
    ),
    "passes_if_wrong": Noul(
        instructions=("Suppose the code under test returned a result of the right type and shape but with "
                      "the wrong content. Would every line in `assertions` still pass?"),
        criteria=_crit(
            {"what": "The checks only look at presence, type, length, truthiness, or that a call happened",
             "examples": ["expect(result).toBeDefined()", "assert isinstance(x, dict)", "assert len(items) > 0"]},
            {"what": "At least one check compares the content to a specific expected value or exact error",
             "examples": ["expect(result).toEqual({id: 1})", "assert total == 42", "pytest.raises(ValueError)"]}),
    ),
    "unchecked_output": Noul(
        instructions=("Does `test_code` call the code under test and then leave its main return value or "
                      "resulting state unchecked?"),
    ),
}


def b1_features(a: dict, r: dict) -> dict[str, float]:
    f = {k: v["noul"] for k, v in a.items() if "noul" in v}
    f["strength"] = a["strength"]["score"] / 3
    f["strength_conf"] = a["strength"]["confidence"]
    return f


def b1_score(a: dict, r: dict) -> float:
    f = b1_features(a, r)
    c = assertion_features(r["code"], r["lang"])
    weak_strength = 1 - f["strength"]
    s = max(f["passes_if_wrong"], f["name_mismatch"], f["restates_impl"], weak_strength,
            (1 - f["name_checked"]) * weak_strength, f["mock_only"] * weak_strength)
    if c["no_assertion"] and not c["has_raises"]:
        s = max(s, 0.5 * (1 + f["loose_only"]))  # code found no assertion; helpers may still assert
    return s


def b1_mean(a: dict, r: dict) -> float:
    f = b1_features(a, r)
    return (f["passes_if_wrong"] + f["name_mismatch"] + (1 - f["name_checked"]) + (1 - f["strength"])
            + f["restates_impl"] + f["unchecked_output"]) / 6


# --------------------------------------------------------------------------- task A: v3

def mutation_kind(m: dict) -> str:
    """What kind of edit this is, from the mutator name (code knows this; the model need not guess)."""
    mut, repl = m["mutator"], m["replacement"].strip()
    if repl in REMOVED or mut in ("BlockStatement", "ArrowFunction"):
        return "removal"
    if mut == "return->None":
        return "return"
    if mut == "str->XX" or mut in ("StringLiteral", "Regex"):
        return "string"
    if mut == "BooleanLiteral" or re.match(r"^(True|False)->", mut):
        return "bool"
    if re.match(r"^-?[\d.]+(e-?\d+)?->", mut):
        return "number"
    return "operator"  # arithmetic, comparison, logical, conditional, method, optional chaining


A3_QUESTIONS = dict(A2_QUESTIONS)
A3_QUESTIONS["same_truthiness"] = Noul(
    instructions=("If the caller of `code` only checks whether the result is truthy or falsy, would it behave "
                  "the same with `edit.after` as with `edit.before`?"),
)

BENIGN_BY_KIND = {
    "number": ("tuning_knob", "telemetry_only"),
    "string": ("prose_only", "type_or_name_only", "telemetry_only"),
    "removal": ("prose_only", "telemetry_only"),
    "return": ("same_truthiness", "telemetry_only"),
    "bool": ("same_truthiness", "telemetry_only"),
    "operator": ("telemetry_only",),
}


def a3_score(a: dict, r: dict) -> float:
    f = a1_features(a)
    kind = mutation_kind(r["mutant"])
    behavior = max(f["changes_result"], f["changes_branch"]) - f["same_for_all_inputs"]
    benign = max(f[k] for k in BENIGN_BY_KIND[kind])
    return (0.35 * behavior - 0.35 * benign + 0.2 * f["impact"] + 0.05 * f["ordinary_input"]
            + 0.05 * f["security_or_money"] + 0.7) / 1.4


def a3_features(a: dict, r: dict) -> dict[str, float]:
    f = a1_features(a)
    kind = mutation_kind(r["mutant"])
    f["benign_gated"] = max(f[k] for k in BENIGN_BY_KIND[kind])
    return f


# --------------------------------------------------------------------------- registry

REGISTRY: dict[str, dict] = {
    "a0": {"state": a0_state, "questions": A0_QUESTIONS, "score": lambda a, r: a0_score(a),
           "features": lambda a, r: {"affects_behavior": a["affects_behavior"]["noul"],
                                     "tunable_value": a["tunable_value"]["noul"],
                                     "impact": a["impact"]["score"] / 3,
                                     **{f"area_{k}": p for k, p in a["area"]["probabilities"].items()}}},
    "a1": {"state": a1_state, "questions": A1_QUESTIONS, "score": lambda a, r: a1_score(a),
           "features": lambda a, r: a1_features(a)},
    "b0": {"state": b0_state, "questions": B0_QUESTIONS, "score": lambda a, r: b0_score(a),
           "features": lambda a, r: {k: v["noul"] for k, v in a.items() if "noul" in v}
                                     | {"strength": a["strength"]["score"] / 3}},
}
REGISTRY["a2"] = {"state": a2_state, "questions": A2_QUESTIONS, "prefilter": a2_prefilter,
                  "score": lambda a, r: a2_score(a), "features": lambda a, r: a2_features(a)}
REGISTRY["a2add"] = {**REGISTRY["a2"], "score": lambda a, r: a2_additive(a)}
REGISTRY["b1"] = {"state": b1_state, "questions": B1_QUESTIONS, "score": b1_score, "features": b1_features}
REGISTRY["b1mean"] = {**REGISTRY["b1"], "score": b1_mean}
REGISTRY["a3"] = {"state": a2_state, "questions": A3_QUESTIONS, "prefilter": a2_prefilter,
                  "score": a3_score, "features": a3_features}
REGISTRY["a2gate"] = {**REGISTRY["a2"], "score": lambda a, r: a3_score({**a, "same_truthiness": {"noul": 0.0}}, r),
                      "features": lambda a, r: a2_features(a)}

# The shipped implementation, evaluated exactly as it runs.
from jev_test_triage import survivors as _ship
from jev_test_triage.mutants.model import Mutant as _Mutant

REGISTRY["ship_all"] = {  # ship as evaluated on lockbox/confirmation: skips __all__ entries
    "state": lambda r: _ship.build_state(_Mutant.from_dict(r["mutant"]), r.get("file_source", "")),
    "questions": _ship.QUESTIONS,
    "prefilter": lambda r: _ship.prefilter(_Mutant.from_dict(r["mutant"]), r.get("file_source", ""), True),
    "score": lambda a, r: _ship.score(a, _Mutant.from_dict(r["mutant"])),
    "features": lambda a, r: _ship.features(a),
}
REGISTRY["ship"] = {
    "state": lambda r: _ship.build_state(_Mutant.from_dict(r["mutant"]), r.get("file_source", "")),
    "questions": _ship.QUESTIONS,
    "prefilter": lambda r: _ship.prefilter(_Mutant.from_dict(r["mutant"]), r.get("file_source", "")),
    "score": lambda a, r: _ship.score(a, _Mutant.from_dict(r["mutant"])),
    "features": lambda a, r: _ship.features(a),
}
