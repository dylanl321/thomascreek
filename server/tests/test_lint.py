"""New code passes the linter and the type checker; old findings are in lint-baseline.json (tools/lint.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _lint():
    spec = importlib.util.spec_from_file_location("hub_lint", ROOT / "tools" / "lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_new_lint_or_type_findings():
    if importlib.util.find_spec("ruff") is None or importlib.util.find_spec("mypy") is None:
        pytest.fail('ruff and mypy are missing: pip install -e ".[test]"')
    lint = _lint()
    new = lint.new_findings(lint.current(), json.loads(lint.BASELINE.read_text()))
    assert not new, "New lint or type findings:\n" + "\n".join(new)


def test_the_ratchet_only_flags_growth():
    lint = _lint()
    base = {"ruff": {"hub/a.py": {"F401": 2}}, "mypy": {}}
    assert lint.new_findings({"ruff": {"hub/a.py": {"F401": 2}}, "mypy": {}}, base) == []
    assert lint.new_findings({"ruff": {"hub/a.py": {"F401": 1}}, "mypy": {}}, base) == []
    assert lint.new_findings({"ruff": {"hub/a.py": {"F401": 3}}, "mypy": {"hub/b.py": {"arg-type": 1}}}, base) == [
        "ruff hub/a.py: F401 2 -> 3", "mypy hub/b.py: arg-type 0 -> 1"]
