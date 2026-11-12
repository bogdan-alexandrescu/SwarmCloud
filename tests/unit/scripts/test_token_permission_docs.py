"""The docs' token permission tables ask for what forgeperms.REQUIRED asks
(the owner, 2026-11-05: a workspace's GitHub token needs Actions: Read and write).

The merge step re-runs the failed jobs of a run whose failure was a flake
(`GitHubWriter.rerun_failed_jobs`, `POST actions/runs/<run>/rerun-failed-jobs`),
and GitHub refuses that with 403 on a token holding Actions: Read only, so the
merge fails with `checks_failed`. Four docs tell a person what to grant, and
the console's permission cards read `forgeperms.REQUIRED`; these tests hold
them together:

  * every permission statement in those docs' tables and permission lists
    says `Actions: Read and write`;
  * every level they name for a permission forgeperms knows matches its
    requirement there (`REQUIRED`, `WORKFLOW_FILES_REQUIREMENT`,
    `ACTIONS_SECRETS_REQUIREMENTS`), imported, never restated here;
  * multi-tenancy.md's table names every permission of `REQUIRED`.

A "permission statement" is a table row, a list bullet, or the paragraph of
docs/issue-runs.md that lists the permissions. A mention followed by "only"
("a token with `Actions: Read` only gets 403") describes the token that is
not enough, and is not a statement of what to grant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCS = {
    "multi-tenancy": REPO / "docs" / "multi-tenancy.md",
    "issue-runs": REPO / "docs" / "issue-runs.md",
    "merge-strategy-live-proof": REPO / "docs" / "merge-strategy-live-proof.md",
    "github-app-runbook": REPO / "docs" / "runbooks" / "github-app.md",
}

#: GitHub's settings-page names for its permissions, as the docs write them,
#: to the API names forgeperms uses. GitHub's vocabulary, not the requirements.
GITHUB_NAME = {
    "Contents": "contents",
    "Pull requests": "pull_requests",
    "Issues": "issues",
    "Checks": "checks",
    "Commit statuses": "statuses",
    "Actions": "actions",
    "Metadata": "metadata",
    "Workflows": "workflows",
    "Secrets": "secrets",
    "Environments": "environments",
    "Administration": "administration",
}
#: GitHub's settings-page access words to the API's levels.
LEVEL = {"Read and write": "write", "Read-only": "read", "Read": "read", "No access": None}

MENTION_RE = re.compile(
    r"(?P<name>" + "|".join(sorted(map(re.escape, GITHUB_NAME), key=len, reverse=True)) + r")"
    r"`?\**`?: \**(?P<level>Read and write|Read-only|Read|No access)\b(?![-\w])`?\**(?P<after>\s*only\b)?")

#: The issue-runs.md paragraph that lists what an issue run's token needs.
ISSUE_RUNS_PARAGRAPH = "The permissions it needs, on every repository a tenant submits issues from"
#: The per-strategy minimum table's row for `collect`, which pushes nothing:
#: its `Contents: Read` is the least a private clone needs, below REQUIRED.
COLLECT_ROW = "| `collect` |"

_RANK = {None: 0, "read": 1, "write": 2, "admin": 3}


def _statements(text: str) -> list[str]:
    """The doc's table rows, list bullets and the issue-runs permission paragraph."""
    lines = text.splitlines()
    out = [line for line in lines
           if line.lstrip().startswith(("|", "* ", "- ")) and not line.startswith(COLLECT_ROW)]
    start = text.find(ISSUE_RUNS_PARAGRAPH)
    if start >= 0:
        end = text.find("\n\n", start)
        out.append(" ".join(text[start:end if end >= 0 else None].split()))
    return out


def _mentions(doc: str) -> list[tuple[str, str | None, str]]:
    """(api name, level, the statement) for each permission a statement names."""
    found = []
    for statement in _statements(DOCS[doc].read_text()):
        for m in MENTION_RE.finditer(statement):
            if m.group("after"):
                continue
            found.append((GITHUB_NAME[m.group("name")], LEVEL[m.group("level")], statement))
    return found


@pytest.fixture
def forgeperms():
    # Imported here, not at module top: swarm_api pulls in the service stack,
    # and every collection of tests/unit/scripts would pay for it
    # (test_collection_stays_cheap).
    from swarm_api import forgeperms
    return forgeperms


def _known(forgeperms) -> dict[str, str]:
    reqs = (*forgeperms.REQUIRED, forgeperms.WORKFLOW_FILES_REQUIREMENT,
            *forgeperms.ACTIONS_SECRETS_REQUIREMENTS)
    return {r.permission: r.access for r in reqs}


def test_the_issue_runs_paragraph_is_still_where_this_reads_it():
    # MUTATION: reword the paragraph's opening and issue-runs.md would be read
    # for nothing; this keeps the anchor honest.
    assert ISSUE_RUNS_PARAGRAPH in DOCS["issue-runs"].read_text()


@pytest.mark.parametrize("doc", sorted(DOCS))
def test_every_token_permission_statement_asks_actions_read_and_write(doc):
    actions = [(level, s) for name, level, s in _mentions(doc) if name == "actions"]
    assert actions, f"{doc} no longer names an Actions permission in a table or permission list"
    wrong = [s for level, s in actions if level != "write"]
    assert not wrong, f"{doc} asks for less than Actions: Read and write in: {wrong}"


@pytest.mark.parametrize("doc", sorted(DOCS))
def test_every_level_a_doc_names_matches_forgeperms(doc, forgeperms):
    known = _known(forgeperms)
    wrong = [f"{name}: doc {level}, forgeperms {known[name]} -- {s[:120]}"
             for name, level, s in _mentions(doc)
             if name in known and _RANK[level] != _RANK[known[name]]]
    assert not wrong, f"{doc} disagrees with forgeperms: {wrong}"


def test_actions_requirement_is_read_and_write(forgeperms):
    assert ("actions", "write") in {(r.permission, r.access) for r in forgeperms.REQUIRED}


def test_the_multi_tenancy_table_names_every_required_permission(forgeperms):
    named = {name for name, _, _ in _mentions("multi-tenancy")}
    assert {r.permission for r in forgeperms.REQUIRED} <= named
