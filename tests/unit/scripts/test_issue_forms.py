"""The `hotfix` label, which hotfix.yml gates on, is declared (restored from the deleted issue-forms test).

hotfix.yml proceeds only on a merged pull request carrying `hotfix`; a label
the repository does not declare is one nobody can apply. Only .github/ is read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
LABELS = REPO / ".github" / "labels.yml"


def _load(path: Path) -> Any:
    assert path.is_file(), f"{path.relative_to(REPO)} is not present"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path.relative_to(REPO)} is empty"
    return yaml.safe_load(text)


def _declared_labels() -> list[dict[str, Any]]:
    data = _load(LABELS)
    assert isinstance(data, list) and data, ".github/labels.yml must be a non-empty list"
    return data


def _labels_workflows_gate_on() -> set[str]:
    """Every label a workflow passes as `--label <name>`."""
    found: set[str] = set()
    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        found |= set(re.findall(r"--label\s+[\"']?([\w.-]+)", path.read_text()))
    return found


def test_the_hotfix_label_is_declared_and_gated_on() -> None:
    """hotfix.yml proceeds only on a merged pull request carrying `hotfix`; a
    label the repository does not declare is one nobody can apply."""
    declared = {e.get("name") for e in _declared_labels() if isinstance(e, dict)}
    assert "hotfix" in _labels_workflows_gate_on(), "no workflow gates on --label hotfix any more"
    assert "hotfix" in declared
