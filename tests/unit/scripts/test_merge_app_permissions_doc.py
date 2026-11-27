"""The merge App's token mints are read from every workflow and composite action (restored control).

Only .github/ is read: the runbook half of the deleted test file is not restored.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
GITHUB = REPO / ".github"

ACTION = "actions/create-github-app-token"
APP_ID = "vars.MERGE_APP_ID"


def _files() -> list[Path]:
    workflows = sorted((GITHUB / "workflows").glob("*.y*ml"))
    actions = sorted((GITHUB / "actions").glob("**/action.y*ml"))
    return workflows + actions


def _steps(doc: object):
    """Every step mapping in a workflow (jobs.*.steps) or a composite action (runs.steps)."""
    if not isinstance(doc, dict):
        return
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict):
            yield from job.get("steps") or []
    runs = doc.get("runs")
    if isinstance(runs, dict):
        yield from runs.get("steps") or []


def _requested() -> dict[str, set[tuple[str, str]]]:
    """permission name -> {(file:step, level)} over every merge App token mint."""
    found: dict[str, set[tuple[str, str]]] = {}
    for path in _files():
        doc = yaml.safe_load(path.read_text())
        for step in _steps(doc):
            if not isinstance(step, dict) or not str(step.get("uses", "")).startswith(ACTION + "@"):
                continue
            inputs = step.get("with") or {}
            if APP_ID not in str(inputs.get("app-id", "")):
                continue
            where = f"{path.relative_to(REPO)}:{step.get('id') or step.get('name')}"
            for key, level in inputs.items():
                if key.startswith("permission-"):
                    found.setdefault(key[len("permission-"):], set()).add((where, str(level)))
    return found


def test_the_workflows_mint_the_merge_app_token_somewhere() -> None:
    """A control: a parser that finds no mint would pass every test below."""
    requested = _requested()
    sites = {where for uses in requested.values() for where, _ in uses}
    assert len(sites) >= 2, f"found only {sorted(sites)} merge App token mints under .github/"
    assert {"contents", "pull-requests"} <= set(requested), sorted(requested)
