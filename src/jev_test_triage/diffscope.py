"""Changed files and lines from git, so hooks and CI only look at what a change touched."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def changed_lines(base: str | None, cwd: Path = Path("."), staged: bool = False) -> dict[str, set[int]]:
    """{path: {added or modified line numbers}} against `base`, or the index when `staged`."""
    args = ["diff", "--unified=0", "--no-color", "--diff-filter=AM"]
    if staged:
        args.append("--cached")
    elif base:
        args.append(f"{base}...HEAD")
    out: dict[str, set[int]] = {}
    current = None
    for line in _git(args, cwd).splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            if current:
                out.setdefault(current, set())
        elif current and (m := HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out[current].update(range(start, start + count))
    return out


def lines_spec(lines: set[int]) -> str:
    """{3,4,5,9} -> '3-5,9' (the format `jtt mutate-py --lines` takes)."""
    parts, run = [], []
    for n in sorted(lines):
        if run and n != run[-1] + 1:
            parts.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = []
        run.append(n)
    if run:
        parts.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ",".join(parts)
