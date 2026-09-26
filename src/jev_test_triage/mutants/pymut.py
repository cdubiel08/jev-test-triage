"""A small AST mutation tester for Python.

It mutates one module at a time, runs a test command for each mutant, and writes one
JSON line per mutant. It exists because some mutation tools rename modules while they
run, which breaks projects that import through a package path such as `src.*`.

Operators: arithmetic swap, comparison swap, and/or swap, drop `not`, constant tweaks
(bool flip, int + 1, float * 2, string -> "XX...XX"), and `return x` -> `return None`.
Log calls, f-strings, docstrings, raise messages and type annotations are not mutated.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

from .model import Mutant, splice

SWAP_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
            ast.FloorDiv: ast.Mult, ast.Mod: ast.Mult}
SWAP_CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
            ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
            ast.Is: ast.IsNot, ast.IsNot: ast.Is}
LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
WINDOW = 8  # lines of context either side of a module-level mutant


def parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}


def is_annotation(node: ast.AST, par: dict) -> bool:
    """True inside a type annotation or a typing.Literal[...] subscript."""
    cur = node
    while cur in par:
        p = par[cur]
        if isinstance(p, ast.arg) and cur is p.annotation:
            return True
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and cur is p.returns:
            return True
        if isinstance(p, ast.AnnAssign) and cur is p.annotation:
            return True
        if isinstance(p, ast.Subscript):
            v = p.value
            name = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else ""
            if name == "Literal":
                return True
        cur = p
    return False


def is_cosmetic(node: ast.AST, par: dict) -> bool:
    """True inside a log call, an f-string, a docstring, or a raise statement's message."""
    cur = node
    while cur in par:
        p = par[cur]
        if isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute) and p.func.attr in LOG_ATTRS:
            return True
        if isinstance(p, ast.JoinedStr):
            return True
        if isinstance(p, ast.Expr) and isinstance(cur, ast.Constant):
            return True
        if isinstance(p, ast.Raise):
            return True
        cur = p
    return False


def qualname(node: ast.AST, par: dict) -> str:
    names, cur = [], node
    while cur in par:
        cur = par[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(cur.name)
    return ".".join(reversed(names)) or "<module>"


def enclosing_func(node: ast.AST, par: dict):
    cur = node
    while cur in par:
        cur = par[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


def candidates(tree: ast.AST, par: dict) -> Iterator[tuple[ast.AST, str, object]]:
    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or is_annotation(node, par):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in SWAP_BIN:
            new = SWAP_BIN[type(node.op)]
            yield node, f"{type(node.op).__name__}->{new.__name__}", lambda n, new=new: setattr(n, "op", new())
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP_CMP:
            new = SWAP_CMP[type(node.ops[0])]
            yield node, f"{type(node.ops[0]).__name__}->{new.__name__}", lambda n, new=new: setattr(n, "ops", [new()])
        elif isinstance(node, ast.BoolOp):
            new = ast.Or if isinstance(node.op, ast.And) else ast.And
            yield node, f"{type(node.op).__name__}->{new.__name__}", lambda n, new=new: setattr(n, "op", new())
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield node, "drop-not", "DROP_NOT"
        elif isinstance(node, ast.Constant) and not is_cosmetic(node, par):
            v = node.value
            if isinstance(v, bool):
                yield node, f"{v}->{not v}", lambda n: setattr(n, "value", not n.value)
            elif isinstance(v, int):
                yield node, f"{v}->{v + 1}", lambda n: setattr(n, "value", n.value + 1)
            elif isinstance(v, float):
                yield node, f"{v}->{v * 2 or 1.0}", lambda n: setattr(n, "value", n.value * 2 or 1.0)
            elif isinstance(v, str) and v and not isinstance(par.get(node), ast.Dict):
                yield node, "str->XX", lambda n: setattr(n, "value", "XX" + n.value + "XX")
        elif isinstance(node, ast.Return) and node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            yield node, "return->None", lambda n: setattr(n, "value", ast.Constant(None))


def parse_lines(spec: str | None) -> set[int] | None:
    """'10-20,31' -> {10..20, 31}; None means every line."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def failing_tests(junit: Path) -> list[str]:
    """pytest node ids ('path::Class::name', params stripped) that failed or errored."""
    out = set()
    for case in ET.parse(junit).getroot().iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        cls, name = case.get("classname", ""), case.get("name", "").split("[")[0]
        parts = cls.split(".")
        # classname is 'tests.test_x' or 'tests.test_x.TestClass'
        i = next((k for k in range(len(parts), 0, -1) if parts[k - 1].startswith("test")), len(parts))
        mod = "/".join(parts[:i]) + ".py"
        out.add("::".join([mod, *parts[i:], name]))
    return sorted(out)


def mutate_module(repo: Path, module: str, cmd: list[str], timeout: int = 120,
                  lines: set[int] | None = None, log=print, per_test: bool = False) -> list[Mutant]:
    mod_p = repo / module
    original = mod_p.read_text()
    src_lines = original.splitlines()
    tree = ast.parse(original)
    par = parents(tree)
    nodes = list(ast.walk(tree))
    index = {id(n): i for i, n in enumerate(nodes)}
    cands = [c for c in candidates(tree, par) if lines is None or c[0].lineno in lines]
    log(f"{module}: {len(cands)} mutants")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cache = mod_p.parent / "__pycache__"

    junit = Path(tempfile.mkdtemp()) / "junit.xml"
    last_failures: list[str] = []

    def run() -> int:
        for pyc in cache.glob(f"{mod_p.stem}.*.pyc"):
            pyc.unlink()
        extra = [f"--junitxml={junit}"] if per_test else []
        junit.unlink(missing_ok=True)
        rc = subprocess.run(cmd + extra, cwd=repo, capture_output=True, text=True,
                            timeout=timeout, env=env, check=False).returncode
        last_failures[:] = failing_tests(junit) if per_test and junit.exists() else []
        return rc

    results: list[Mutant] = []
    try:
        mod_p.write_text(ast.unparse(tree))
        if run() != 0:
            raise RuntimeError(f"tests fail on the unmutated (re-printed) {module}; fix them first")
        for k, (node, desc, fn) in enumerate(cands):
            mtree = copy.deepcopy(tree)
            mnode = list(ast.walk(mtree))[index[id(node)]]
            orig_src = ast.get_source_segment(original, node) or ast.unparse(node)
            if fn == "DROP_NOT":
                p = parents(mtree)[mnode]
                for fld, val in ast.iter_fields(p):
                    if val is mnode:
                        setattr(p, fld, mnode.operand)
                    elif isinstance(val, list):
                        setattr(p, fld, [mnode.operand if x is mnode else x for x in val])
                new_src = ast.unparse(mnode.operand)
            else:
                fn(mnode)
                new_src = ast.unparse(mnode)
            try:
                code = ast.unparse(ast.fix_missing_locations(mtree))
            except Exception as e:  # noqa: BLE001 - an unprintable mutant is skipped
                log(f"skip {k}: {e}")
                continue
            mod_p.write_text(code)
            try:
                status = "killed" if run() != 0 else "survived"
            except subprocess.TimeoutExpired:
                status = "timeout"
            func = enclosing_func(node, par)
            if func is not None:
                ctx, start = ast.get_source_segment(original, func) or "", func.lineno
            else:
                lo = max(0, node.lineno - 1 - WINDOW)
                ctx, start = "\n".join(src_lines[lo:node.lineno + WINDOW]), lo + 1
            before = src_lines[node.lineno - 1]
            after = (splice(before, node.col_offset, node.end_col_offset, new_src)
                     if node.end_lineno == node.lineno else before)
            results.append(Mutant(
                id=f"{module}:{node.lineno}:{k}", lang="python", file=module, line=node.lineno,
                mutator=desc, original=orig_src[:400], replacement=new_src[:400], status=status,
                line_before=before, line_after=after, qualname=qualname(node, par),
                context=ctx[:8000], context_start=start, killed_by=list(last_failures),
            ))
            log(f"[{k + 1}/{len(cands)}] {status:8} L{node.lineno} {desc}")
    finally:
        mod_p.write_text(original)
    return results


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="jtt mutate-py", description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--module", required=True, help="module path relative to --repo")
    ap.add_argument("--cmd", required=True, help='test command, e.g. "pytest -x -q tests/test_x.py"')
    ap.add_argument("--out", required=True, type=Path, help="JSONL output")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--lines", help="only mutate these lines, e.g. 10-20,31")
    ap.add_argument("--per-test", action="store_true",
                    help="record which tests fail for each mutant (the command must not stop at the first failure)")
    a = ap.parse_args(argv)
    res = mutate_module(a.repo, a.module, shlex.split(a.cmd), a.timeout, parse_lines(a.lines),
                        log=lambda m: print(m, file=sys.stderr, flush=True), per_test=a.per_test)
    with a.out.open("w") as f:
        for m in res:
            f.write(json.dumps(m.to_dict()) + "\n")
    s = sum(m.status == "survived" for m in res)
    print(f"done {a.module}: {len(res)} mutants, {s} survived", file=sys.stderr)


if __name__ == "__main__":
    main()
