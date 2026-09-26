"""Deterministic assertion features for a single test function.

Code finds and classifies assertions; the model is only asked what code cannot see.
Each assertion is classified as:
  exact  - compares against a specific value, exact error, snapshot, or structure;
  loose  - truthiness, presence, type, or non-emptiness only;
  mock   - checks that a mock or spy was called (with or without arguments).
"""

from __future__ import annotations

import ast
import re

PY_LOOSE_CALLS = {"assertTrue", "assertFalse", "assertIsNotNone", "assertIsNone", "assertIsInstance",
                  "assertNotIsInstance", "assert_", "failUnless"}
PY_EXACT_CALLS = {"assertEqual", "assertEquals", "assertNotEqual", "assertDictEqual", "assertListEqual",
                  "assertSequenceEqual", "assertSetEqual", "assertTupleEqual", "assertAlmostEqual",
                  "assertRaises", "assertRaisesRegex", "assertIn", "assertNotIn", "assertCountEqual",
                  "assertRegex", "assertGreater", "assertLess", "assertGreaterEqual", "assertLessEqual",
                  "assertIs", "assertIsNot", "assertWarns", "assertLogs"}
PY_MOCK_RE = re.compile(r"\.assert_(?:called|not_called|any_call|has_calls|awaited)\w*\(|\.called\b|\.call_count\b|\.call_args")


def _py_assert_kind(node: ast.Assert) -> str:
    t = node.test
    if isinstance(t, ast.Compare):
        op, right = t.ops[0], t.comparators[0]
        left_src = ast.unparse(t.left)
        if PY_MOCK_RE.search(left_src):
            return "mock"
        if isinstance(op, (ast.IsNot, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
            return "loose"
        if (isinstance(op, (ast.Gt, ast.GtE)) and isinstance(right, ast.Constant) and right.value in (0, 1)
                and isinstance(t.left, ast.Call) and getattr(t.left.func, "id", "") == "len"):
            return "loose"
        return "exact"
    if isinstance(t, ast.Call) and getattr(t.func, "id", "") in ("isinstance", "callable", "hasattr", "bool", "any", "all"):
        return "loose"
    src = ast.unparse(t)
    if PY_MOCK_RE.search(src):
        return "mock"
    return "loose"  # bare truthiness: `assert x`, `assert not x`


def python_features(code: str) -> dict:
    kinds: list[str] = []
    snapshot = raises = False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                kinds.append(_py_assert_kind(node))
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "raises" or name in ("assertRaises", "assertRaisesRegex"):
                    raises = True
                    kinds.append("exact")
                elif name in PY_EXACT_CALLS:
                    kinds.append("exact")
                elif name in PY_LOOSE_CALLS:
                    kinds.append("loose")
                elif name.startswith("assert_") and isinstance(fn, ast.Attribute):
                    kinds.append("mock")
                elif name in ("snapshot", "assert_match"):
                    snapshot = True
                    kinds.append("exact")
    return _summary(kinds, snapshot, raises, code)


TS_MATCHER = re.compile(r"\.(not\.)?(to[A-Z]\w*|rejects\.\w+|resolves\.\w+)\s*\(")
TS_LOOSE = {"toBeDefined", "toBeTruthy", "toBeFalsy", "toBeUndefined", "toBeNull", "toBeInstanceOf",
            "toBeTypeOf", "toBeNaN"}
TS_MOCK = {"toHaveBeenCalled", "toHaveBeenCalledTimes", "toHaveBeenCalledWith", "toHaveBeenLastCalledWith",
           "toHaveBeenNthCalledWith", "toBeCalled", "toBeCalledWith", "toBeCalledTimes",
           "toHaveReturned", "toHaveReturnedWith", "toHaveBeenCalledOnce"}
TS_SNAPSHOT = {"toMatchSnapshot", "toMatchInlineSnapshot", "toMatchFileSnapshot", "toThrowErrorMatchingSnapshot",
               "toThrowErrorMatchingInlineSnapshot"}


def js_features(code: str) -> dict:
    kinds: list[str] = []
    snapshot = raises = False
    for m in TS_MATCHER.finditer(code):
        negated, name = bool(m.group(1)), m.group(2).split(".")[-1]
        rest = code[m.end(): m.end() + 40]
        if name in TS_MOCK:
            kinds.append("mock")
        elif name in TS_SNAPSHOT:
            snapshot = True
            kinds.append("exact")
        elif name in TS_LOOSE or name in ("toBeGreaterThan", "toBeGreaterThanOrEqual") and re.match(r"\s*[01]\s*\)", rest):
            kinds.append("loose")
        elif name in ("toThrow", "toThrowError"):
            raises = True
            kinds.append("loose" if re.match(r"\s*\)", rest) else "exact")
        elif negated and name in ("toBe", "toEqual") and re.match(r"\s*(null|undefined)\s*\)", rest):
            kinds.append("loose")
        else:
            kinds.append("exact")
    # assert.* / chai-style assertions
    for m in re.finditer(r"\bassert(?:\.(\w+))?\s*\(", code):
        k = m.group(1) or "ok"
        kinds.append("loose" if k in ("ok", "exists", "isDefined", "isNotNull", "isTrue") else "exact")
    return _summary(kinds, snapshot, raises, code)


def _summary(kinds: list[str], snapshot: bool, raises: bool, code: str) -> dict:
    n = len(kinds)
    ne, nl, nm = kinds.count("exact"), kinds.count("loose"), kinds.count("mock")
    return {
        "n_assert": n, "n_exact": ne, "n_loose": nl, "n_mock": nm,
        "no_assertion": float(n == 0),
        "loose_only": float(n > 0 and ne == 0 and nm == 0),
        "mock_only": float(n > 0 and nm == n),
        "frac_exact": ne / n if n else 0.0,
        "has_snapshot": float(snapshot), "has_raises": float(raises),
        "n_lines": code.count("\n") + 1,
    }


def features(code: str, lang: str) -> dict:
    return python_features(code) if lang == "python" else js_features(code)


def assertion_lines(code: str, lang: str, limit: int = 40) -> list[str]:
    """Source lines that contain an assertion, for pointing questions at them."""
    pat = (re.compile(r"^\s*(assert\b|with\s+pytest\.raises|self\.assert|\w+(\.\w+)*\.assert_)")
           if lang == "python" else re.compile(r"\bexpect\s*\(|\bassert\b"))
    return [ln.strip() for ln in code.splitlines() if pat.search(ln)][:limit]
