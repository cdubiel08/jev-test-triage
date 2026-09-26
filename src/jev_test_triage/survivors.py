"""Rank surviving mutants by how much a test that kills them is worth.

Design (see docs/method.md):
  1. Code first: deterministic filters drop mutants that cannot matter (type-only
     strings, `__all__` entries) without a model call.
  2. State: the enclosing function, the edited line before and after, a plain-language
     description of the edit written by code, and uses of a changed top-level name.
  3. Questions: many narrow Nouls and one Score, asked together in one request
     (speculative fan-out); none of them is the final decision.
  4. Composition in code: behavior evidence, discounted by benign explanations,
     weighted by harm. Confidence routing then splits results into act/review/ignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from typesafe_sdk import Choice, Noul, NoulCriteria, Score

from .jev import Jev
from .mutants.model import Mutant

# ----------------------------------------------------------------------------- code first

REMOVED = {"", ";", "{}", "()"}
ASSIGN = re.compile(r"^\s*(?:export\s+)?(?:const\s+|let\s+|var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)")


def _in_dunder_all(src: str, line: int) -> bool:
    lines = src.splitlines()
    for i in range(line - 1, max(-1, line - 200), -1):
        t = lines[i]
        if re.match(r"^__all__\s*[:=]", t):
            return True
        if i < line - 1 and re.match(r"^\S", t):
            return False
    return False


def prefilter(m: Mutant, file_source: str = "", ignore_dunder_all: bool = False) -> str | None:
    """A reason when code alone can tell the mutant does not matter, else None.

    `__all__` entries are only skipped on request: in an application nobody star-imports,
    but in a library `__all__` is public API (the confirmation set showed exactly that).
    """
    line = m.line_before
    if m.lang == "python":
        if re.search(r"\bLiteral\[", line) and m.original[:1] in "'\"":
            return "type annotation (Literal)"
        if re.search(r"\b(TypeVar|NewType|ParamSpec|TypeVarTuple)\(", line) and m.original[:1] in "'\"":
            return "type variable name"
        if ignore_dunder_all and m.qualname in ("", "<module>") and _in_dunder_all(file_source, m.line):
            return "__all__ entry"
    return None


def describe_edit(m: Mutant) -> str:
    """A plain-language description of the mutation, written by code."""
    orig, repl, mut = m.original.strip(), m.replacement.strip(), m.mutator
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


def usages(file_source: str, line: int, limit: int = 12) -> list[str]:
    """Lines elsewhere in the file that use the name assigned on `line`."""
    lines = file_source.splitlines()
    if not (0 < line <= len(lines)):
        return []
    m = ASSIGN.match(lines[line - 1])
    if not m:
        return []
    pat = re.compile(rf"\b{re.escape(m.group(1))}\b")
    return [f"{i + 1}: {t.strip()}" for i, t in enumerate(lines) if i + 1 != line and pat.search(t)][:limit]


def build_state(m: Mutant, file_source: str = "") -> dict:
    st: dict = {
        "file": m.file,
        "code": m.context or m.line_before,
        "edit": {"description": describe_edit(m), "line_number": m.line,
                 "before": m.line_before.strip(), "after": m.line_after.strip()},
    }
    if m.replacement.strip() in REMOVED:
        st["edit"]["after"] = "(the statement is removed)"
    if m.qualname in ("", "<module>"):
        u = usages(file_source, m.line)
        if u:
            st["uses_of_changed_name"] = u
    return st


# ----------------------------------------------------------------------------- questions

def _crit(true, false) -> NoulCriteria:
    return NoulCriteria(true=true, false=false)


QUESTIONS = {
    "changes_result": Noul(
        instructions=("`edit.description` says how `code` was changed. For some realistic input, would the "
                      "edited code return a different value, store or send different data, or raise a "
                      "different error?"),
        criteria=_crit("Some realistic input gives a different returned, stored, or sent value, or a different error.",
                       "Every realistic input gives the same returned, stored, and sent values and the same errors."),
    ),
    "changes_branch": Noul(
        instructions=("`edit.description` says how `code` was changed. For some realistic input, would a "
                      "different branch, loop iteration, or early return run?"),
    ),
    "same_for_all_inputs": Noul(
        instructions=("Do `edit.before` and `edit.after` behave identically for every possible input, "
                      "for example because the value is never used, is overwritten, or the condition "
                      "cannot differ?"),
        criteria=_crit("No input can make the two versions behave differently.",
                       "At least one input makes the two versions behave differently."),
    ),
    "prose_only": Noul(
        instructions=("Is the edited value free-form prose whose exact wording no program checks, such as a log "
                      "message, an exception or UI message, a docstring, or instructions in a prompt for a "
                      "language model?"),
        criteria=_crit(
            {"what": "Only the wording of human- or model-read text changes",
             "examples": ["A log message", "An error message", "A prompt sent to an LLM"]},
            {"what": "The value is compared, parsed, looked up, or used as a key, name, or identifier",
             "examples": ["A dictionary key", "An HTTP header name", "A cookie prefix", "An enum value"]}),
    ),
    "telemetry_only": Noul(
        instructions=("Does the edit only change which analytics, metrics, or tracing data is recorded, without "
                      "changing consent or privacy handling, stored user data, or what the caller gets back?"),
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
    "same_truthiness": Noul(
        instructions=("If the caller of `code` only checks whether the result is truthy or falsy, would it behave "
                      "the same with `edit.after` as with `edit.before`?"),
    ),
    "area": Choice(
        instructions="Which kind of logic does the edited line `edit.before` belong to?",
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


# ----------------------------------------------------------------------------- composition

def mutation_kind(m: Mutant) -> str:
    """What kind of edit this is, from the mutator name (code knows this; the model need not guess)."""
    mut, repl = m.mutator, m.replacement.strip()
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


# Speculative fan-out: every benign explanation is asked, but code uses only the ones that
# can apply to this kind of edit (a tuning value cannot explain an operator swap).
BENIGN_BY_KIND = {
    "number": ("tuning_knob", "telemetry_only"),
    "string": ("prose_only", "type_or_name_only", "telemetry_only"),
    "removal": ("prose_only", "telemetry_only"),
    "return": ("same_truthiness", "telemetry_only"),
    "bool": ("same_truthiness", "telemetry_only"),
    "operator": ("telemetry_only",),
}


def features(answers: dict) -> dict[str, float]:
    f = {k: v["noul"] for k, v in answers.items() if "noul" in v}
    f["impact"] = answers["impact"]["score"] / 3
    f["impact_conf"] = answers["impact"]["confidence"]
    return f


def score(answers: dict, m: Mutant) -> float:
    """Additive composite in [0, 1]: behavior evidence minus the applicable benign explanation, plus harm."""
    f = features(answers)
    behavior = max(f["changes_result"], f["changes_branch"]) - f["same_for_all_inputs"]
    benign = max(f[k] for k in BENIGN_BY_KIND[mutation_kind(m)])
    return (0.35 * behavior - 0.35 * benign + 0.2 * f["impact"] + 0.05 * f["ordinary_input"]
            + 0.05 * f["security_or_money"] + 0.7) / 1.4


@dataclass
class Routing:
    """Score bands for confidence-gated routing (tuned on the dev split, see README)."""
    act: float = 0.805  # at or above: write a test (precision 85% dev, 87% lockbox, 79% new repos)
    ignore: float = 0.654  # below: leave it (90-93% of these are not worth a test)
    ignore_dunder_all: bool = False  # skip `__all__` entries (sensible for applications)
    min_impact_conf: float = 0.0  # an 'act' with less harm confidence goes to review


@dataclass
class Finding:
    mutant: Mutant
    score: float
    bucket: str  # "act" | "review" | "ignore"
    reason: str = ""
    answers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"mutant": self.mutant.to_dict(), "score": round(self.score, 4), "bucket": self.bucket,
                "reason": self.reason, "answers": self.answers}


def route(s: float, answers: dict, r: Routing) -> str:
    if s < r.ignore:
        return "ignore"
    if s >= r.act and (not answers or answers["impact"]["confidence"] >= r.min_impact_conf):
        return "act"
    return "review"


def reason_text(f: dict[str, float], kind: str) -> str:
    labels = {"prose_only": "only message or prompt wording changes",
              "telemetry_only": "only telemetry changes", "tuning_knob": "a tuning value",
              "type_or_name_only": "only a type or name changes",
              "same_truthiness": "same truthiness as before"}
    benign = {k: labels[k] for k in BENIGN_BY_KIND[kind]}
    top = max(benign, key=lambda k: f[k])
    if f[top] >= 0.5:
        return benign[top]
    if f["same_for_all_inputs"] >= 0.5:
        return "probably equivalent"
    parts = []
    if f["security_or_money"] >= 0.5:
        parts.append("access/money/data logic")
    if f["ordinary_input"] >= 0.5:
        parts.append("reachable with ordinary input")
    parts.append(f"harm {f['impact'] * 3:.1f}/3")
    return ", ".join(parts)


def triage(mutants: list[Mutant], jev: Jev, sources: dict[str, str] | None = None,
           routing: Routing | None = None) -> list[Finding]:
    """Findings for survivors, highest score first."""
    sources = sources or {}
    routing = routing or Routing()
    out: list[Finding] = []
    todo = []
    for m in mutants:
        why = prefilter(m, sources.get(m.file, ""), routing.ignore_dunder_all)
        if why:
            out.append(Finding(m, 0.0, "ignore", why))
        else:
            todo.append(m)
    results = jev.ask_many([(build_state(m, sources.get(m.file, "")), QUESTIONS) for m in todo])
    for m, res in zip(todo, results):
        a = res["answers"]
        s = score(a, m)
        out.append(Finding(m, s, route(s, a, routing), reason_text(features(a), mutation_kind(m)), a))
    out.sort(key=lambda f: -f.score)
    return out
