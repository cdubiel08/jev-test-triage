"""A mutant record shared by every mutation-tool adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Mutant:
    id: str
    lang: str  # "python" | "typescript" | "javascript"
    file: str  # path relative to the repo root
    line: int
    mutator: str  # e.g. "Eq->NotEq", "ConditionalExpression"
    original: str  # source text that was replaced
    replacement: str  # text that replaced it
    status: str  # "survived" | "killed" | "no_coverage" | "timeout" | "error"
    line_before: str = ""  # the full source line before mutation
    line_after: str = ""  # the same line with the replacement spliced in
    qualname: str = ""  # enclosing function, "<module>" for top-level code
    context: str = ""  # enclosing function source, or a window of lines
    context_start: int = 0  # 1-based line number of the first context line
    killed_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Mutant:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


def splice(line: str, start_col: int, end_col: int, replacement: str) -> str:
    """Replace line[start_col:end_col] (0-based) with the replacement's first line."""
    return line[:start_col] + replacement.split("\n")[0] + line[end_col:]
