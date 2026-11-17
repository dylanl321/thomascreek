"""Lint and type check with a baseline: new findings fail, old ones are recorded until someone fixes them.

    python tools/lint.py            # check (also run by tests/test_lint.py, so premerge runs it)
    python tools/lint.py --update   # rewrite the baseline after fixing old findings

The baseline (lint-baseline.json) counts findings per file and rule, without line numbers, so moving code
does not trip it. A file may never gain findings of a rule; fixing some lowers its count on the next --update.
Rules and settings live in pyproject.toml ([tool.ruff], [tool.mypy]).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "lint-baseline.json"
MYPY_LINE = re.compile(r"^(?P<path>[^:]+):\d+(?::\d+)?: error: .*\[(?P<code>[a-z0-9-]+)\]$")


def ruff() -> Counter[tuple[str, str]]:
    out = subprocess.run([sys.executable, "-m", "ruff", "check", "--output-format", "json", "--no-fix", "hub", "tests"],
                         cwd=ROOT, capture_output=True, text=True)
    if out.returncode not in (0, 1):
        raise SystemExit(f"ruff did not run: {out.stderr.strip()[:400]}")
    found: Counter[tuple[str, str]] = Counter()
    for f in json.loads(out.stdout or "[]"):
        found[(Path(f["filename"]).resolve().relative_to(ROOT).as_posix(), str(f.get("code") or "syntax"))] += 1
    return found


def mypy() -> Counter[tuple[str, str]]:
    out = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary", "--no-pretty", "--show-error-codes"],
                         cwd=ROOT, capture_output=True, text=True)
    if out.returncode not in (0, 1):
        raise SystemExit(f"mypy did not run: {(out.stderr or out.stdout).strip()[:400]}")
    found: Counter[tuple[str, str]] = Counter()
    for line in out.stdout.splitlines():
        m = MYPY_LINE.match(line.strip())
        if m:
            found[(m["path"].replace("\\", "/"), m["code"])] += 1
    return found


def current() -> dict[str, dict[str, dict[str, int]]]:
    doc: dict[str, dict[str, dict[str, int]]] = {}
    for tool, found in (("ruff", ruff()), ("mypy", mypy())):
        files: dict[str, dict[str, int]] = {}
        for (path, code), n in sorted(found.items()):
            files.setdefault(path, {})[code] = n
        doc[tool] = files
    return doc


def new_findings(now: dict, base: dict) -> list[str]:
    out = []
    for tool, files in now.items():
        for path, codes in files.items():
            for code, n in codes.items():
                had = ((base.get(tool) or {}).get(path) or {}).get(code, 0)
                if n > had:
                    out.append(f"{tool} {path}: {code} {had} -> {n}")
    return out


def main(argv: list[str]) -> int:
    now = current()
    if "--update" in argv:
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        total = sum(n for files in now.values() for codes in files.values() for n in codes.values())
        print(f"baseline written: {total} findings recorded")
        return 0
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    new = new_findings(now, base)
    if new:
        print("New lint or type findings (fix them, or add `# noqa: CODE  reason` / `# type: ignore[code]`):")
        print("\n".join(f"  {line}" for line in new))
        print("Run `python -m ruff check hub tests` or `python -m mypy` to see the lines.")
        return 1
    print("lint ok: no new findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
