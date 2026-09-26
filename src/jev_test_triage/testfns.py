"""Extract individual test functions from Python and JS/TS test files."""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class TestFn:
    lang: str
    file: str
    line: int
    name: str  # full display name: "Class::test_x" or "describe > it"
    code: str
    end_line: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def python_tests(path: Path, rel: str) -> list[TestFn]:
    src = path.read_text()
    tree = ast.parse(src)
    out: list[TestFn] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef) and child.name.startswith("Test"):
                visit(child, f"{prefix}{child.name}::")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                out.append(TestFn("python", rel, child.lineno, prefix + child.name,
                                  ast.get_source_segment(src, child) or "", child.end_lineno or 0))

    visit(tree, "")
    return out


_BLOCK = re.compile(
    r"""^(?P<indent>[ \t]*)(?P<kind>describe|it|test)(?:\.(?:each|only|concurrent|skip)(?:\([^)]*\))?)?"""
    r"""\(\s*(?P<q>['"`])(?P<name>.*?)(?P=q)""",
    re.MULTILINE,
)


def _match_block(src: str, start: int) -> int:
    """Index of the '}' or ')' closing the call that starts at `start` (string-aware)."""
    depth, i, n = 0, src.find("(", start), len(src)
    quote = None
    while i < n:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def js_tests(path: Path, rel: str) -> list[TestFn]:
    src = path.read_text()
    lang = "typescript" if path.suffix in (".ts", ".tsx", ".mts") else "javascript"
    blocks = []
    for m in _BLOCK.finditer(src):
        end = _match_block(src, m.start("kind"))
        blocks.append((m.start(), end, m.group("kind"), m.group("name")))
    out: list[TestFn] = []
    for s, e, kind, name in blocks:
        if kind == "describe":
            continue
        parents = [b[3] for b in blocks if b[2] == "describe" and b[0] < s and b[1] >= e]
        full = " > ".join([*parents, name])
        line = src.count("\n", 0, s) + 1
        out.append(TestFn(lang, rel, line, full, src[s : e + 1].strip(), src.count("\n", 0, e) + 1))
    return out


def extract(path: Path, rel: str | None = None) -> list[TestFn]:
    rel = rel or str(path)
    if path.suffix == ".py":
        return python_tests(path, rel)
    return js_tests(path, rel)
