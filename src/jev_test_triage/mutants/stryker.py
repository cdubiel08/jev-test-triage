"""Read mutation-testing-report-schema JSON (Stryker JS/.NET/Scala and compatible tools)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .model import Mutant, splice

STATUS = {"Survived": "survived", "NoCoverage": "no_coverage", "Killed": "killed",
          "Timeout": "timeout", "RuntimeError": "error", "CompileError": "error",
          "Ignored": "ignored", "Pending": "pending"}
LANG = {".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript"}

# A line that opens a function body: declarations, methods, arrows and callbacks.
FUNC_OPEN = re.compile(
    r"^\s*(export\s+)?(default\s+)?(async\s+)?function\b"
    r"|^\s*(public|private|protected|static|async|get|set|\s)*(?!(?:if|for|while|switch|catch|with|else|return)\b)"
    r"[A-Za-z_$][\w$]*\s*(<[^>]*>)?\s*\([^;]*\)\s*(:\s*[^=]+)?\{\s*$"
    r"|=>\s*\{\s*$"
    r"|^\s*(export\s+)?(const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(async\s*)?(\(|function)"
)
MAX_FUNC_LINES = 120
WINDOW_BEFORE, WINDOW_AFTER = 15, 12


def enclosing_block(lines: list[str], line: int) -> tuple[int, int]:
    """Best-effort 1-based [start, end] of the function that contains `line`."""
    for start in range(line, max(0, line - MAX_FUNC_LINES), -1):
        text = lines[start - 1]
        if not FUNC_OPEN.search(text):
            continue
        depth, seen = 0, False
        for end in range(start, min(len(lines), start + MAX_FUNC_LINES) + 1):
            for ch in lines[end - 1]:
                if ch == "{":
                    depth, seen = depth + 1, True
                elif ch == "}":
                    depth -= 1
            if seen and depth <= 0:
                if end >= line:
                    return start, end
                break
    lo = max(1, line - WINDOW_BEFORE)
    return lo, min(len(lines), line + WINDOW_AFTER)


def load(path: Path, statuses: set[str] | None = None) -> list[Mutant]:
    """Load mutants; `statuses` filters on the normalized status (default: survivors)."""
    statuses = statuses or {"survived", "no_coverage"}
    data = json.loads(Path(path).read_text())
    out: list[Mutant] = []
    for fname, f in data["files"].items():
        lines = f["source"].splitlines()
        lang = LANG.get(Path(fname).suffix, f.get("language", "javascript"))
        for m in f["mutants"]:
            status = STATUS.get(m["status"], m["status"].lower())
            if status not in statuses:
                continue
            s, e = m["location"]["start"], m["location"]["end"]
            if s["line"] == e["line"]:
                orig = lines[s["line"] - 1][s["column"] - 1 : e["column"] - 1]
            else:
                seg = lines[s["line"] - 1 : e["line"]]
                seg[0] = seg[0][s["column"] - 1 :]
                seg[-1] = seg[-1][: e["column"] - 1]
                orig = "\n".join(seg)
            repl = m.get("replacement") or ""
            before = lines[s["line"] - 1]
            end_col = e["column"] - 1 if s["line"] == e["line"] else len(before)
            after = splice(before, s["column"] - 1, end_col, repl)
            lo, hi = enclosing_block(lines, s["line"])
            out.append(Mutant(
                id=f"{fname}:{s['line']}:{m['id']}", lang=lang, file=fname, line=s["line"],
                mutator=m["mutatorName"], original=orig[:400], replacement=repl[:400],
                status=status, line_before=before, line_after=after,
                context="\n".join(lines[lo - 1 : hi])[:8000], context_start=lo,
                killed_by=list(m.get("killedBy") or []),
            ))
    return out
