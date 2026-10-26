"""The merge App runbook grants every permission the workflows mint its token with.

MEASURED 2026-10-23 to 2026-10-26 (#2220; F12, F14, F15 of #2379):
runner-fallback.yml's `judge` mints the merge App's installation token with
`permission-variables: write`, and failed on every scheduled run with "The
permissions requested are not granted to this installation". The App had been
set up from docs/runbooks/merge-app.md, whose permission table listed only
Contents, Pull requests and Workflows. A token mint can ask only for what the
installation holds, so a permission a workflow requests and the runbook does
not list is a red job the day the App is (re)created from the runbook.

So these tests read every `actions/create-github-app-token` step that mints
with `vars.MERGE_APP_ID`, in every workflow and composite action, and hold the
runbook's "Repository permissions" table to them:

  * every `permission-<name>` input is a row, at a level that covers it, and
    the row names the input it answers;
  * the table lists nothing no step requests, so a permission dropped from the
    workflows does not stay granted to an App holding the `workflows` scope.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
RUNBOOK = REPO / "docs" / "runbooks" / "merge-app.md"
GITHUB = REPO / ".github"

ACTION = "actions/create-github-app-token"
APP_ID = "vars.MERGE_APP_ID"

# The runbook's level for each level an input may ask for: GitHub's settings
# page offers "Read-only" and "Read and write", and write covers read.
COVERS = {
    "read": {"Read-only", "Read and write"},
    "write": {"Read and write"},
}


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


def _table() -> dict[str, tuple[str, str]]:
    """permission (as an input suffix) -> (level, why) from the runbook's permission table."""
    text = RUNBOOK.read_text()
    start = text.index("**Repository permissions**")
    rows: dict[str, tuple[str, str]] = {}
    for line in text[start:].splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            if rows:
                break
            continue
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or cells[0] == "Permission" or set(cells[0]) <= set("-: "):
            continue
        rows[cells[0].lower().replace(" ", "-")] = (cells[1], cells[2])
    return rows


def test_the_workflows_mint_the_merge_app_token_somewhere() -> None:
    """A control: a parser that finds no mint would pass every test below."""
    requested = _requested()
    sites = {where for uses in requested.values() for where, _ in uses}
    assert len(sites) >= 2, f"found only {sorted(sites)} merge App token mints under .github/"
    assert {"contents", "pull-requests"} <= set(requested), sorted(requested)


def test_the_runbook_table_parses() -> None:
    rows = _table()
    assert {"contents", "pull-requests", "workflows"} <= set(rows), sorted(rows)


@pytest.mark.parametrize("permission", sorted(_requested()))
def test_every_requested_permission_is_a_runbook_row(permission: str) -> None:
    rows = _table()
    uses = sorted(_requested()[permission])
    assert permission in rows, (
        f"{', '.join(w for w, _ in uses)} mint the merge App's token with "
        f"permission-{permission}, and docs/runbooks/merge-app.md's permission "
        "table has no row for it: an App set up from the runbook refuses that mint "
        '("The permissions requested are not granted to this installation", #2220)'
    )
    level, why = rows[permission]
    for where, asked in uses:
        assert asked in COVERS, f"{where} asks permission-{permission}: {asked}, not read or write"
        assert level in COVERS[asked], (
            f"{where} asks permission-{permission}: {asked}; the runbook grants {level!r}"
        )
    assert f"permission-{permission}" in why, (
        f"the runbook's {permission} row does not name the input it answers (permission-{permission})"
    )


def test_the_runbook_grants_nothing_no_mint_requests() -> None:
    extra = sorted(set(_table()) - set(_requested()))
    assert not extra, (
        f"docs/runbooks/merge-app.md grants {extra}, which no merge App token mint "
        "under .github/ requests: drop the row, or say which step needs it"
    )
