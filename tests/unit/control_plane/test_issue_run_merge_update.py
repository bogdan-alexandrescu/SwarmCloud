"""An `auto_merge` run accepts the base merges its own merge step made (part of #900).

Measured 2026-10-10: every sweeper issue run that opened a pull request
(5/5: #970, #981, #984, #985, #989) ended FAILED with "head ... was not
pushed by any of this run's tasks", #989 while GitHub said it merged. The
repository's ruleset requires a branch to be up to date, so the merge step
updates a behind branch through GitHub's `update-branch`, and the head that
leaves -- "Merge branch 'main' into swarm/task_..." -- is a commit no task
pushed. The worker's first-parent walk (`agent_worker.merge._updates_onto`)
accepts up to MERGE_MAX_BRANCH_UPDATES of them; `issueci._merge` accepted
none. What these tests hold:

  1. A head that is 1..MERGE_MAX_BRANCH_UPDATES of GitHub's base merges on
     top of a task-pushed head is merged, continuing the task that pushed it.
  2. One past the cap, a commit that is not a merge, a merge GitHub did not
     commit and sign, and a merge whose second parent is not on the base are
     refused, each saying which.
  3. The claimed merge's own update is the SAME merge: no second merge
     workflow is submitted while it runs.
  4. issueci's cap and committer are the worker's, read from its source.

GitHub is the CI-loop tests' fake transport, taught commits and comparisons.
No credentials, no network, no emulator.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from swarm_api import issueci, issueruns

from . import forge_fakes
from .test_issue_run_auto_merge import _checking, _merge_task, _merge_workflows
from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    PR,
    SHA_A,
    _read,
    _step_task,
    api_context,
    clock,
    forge_tokens,
)

ROOT = Path(__file__).resolve().parents[3]
BASE = "main"


class CommitWrites(forge_fakes.GitHubWrites):
    """`GitHubWrites` plus `commits/{sha}` and `compare/{base}...{sha}`."""

    def __init__(self) -> None:
        super().__init__()
        self.commits: dict[str, dict] = {}
        self.on_base: set[str] = set()

    def commit(self, sha: str, parents: list[str], *, by_github: bool = True) -> None:
        self.commits[sha] = {
            "sha": sha,
            "parents": [{"sha": p} for p in parents],
            "commit": {
                "committer": {"email": issueci.GITHUB_COMMITTER_EMAIL if by_github
                              else "someone@example.com"},
                "verification": {"verified": by_github},
            },
        }

    def __call__(self, method, url, headers, body, timeout):
        rest = urlparse(url).path.strip("/").split("/")[3:]
        if method == "GET" and len(rest) == 2 and rest[0] == "commits":
            if rest[1] not in self.commits:
                return 404, b"{}"
            self.calls.append((method, url, dict(headers), body))
            return 200, json.dumps(self.commits[rest[1]]).encode()
        if method == "GET" and len(rest) == 2 and rest[0] == "compare":
            base, _, sha = rest[1].partition("...")
            assert base == BASE
            self.calls.append((method, url, dict(headers), body))
            return 200, json.dumps(
                {"status": "behind" if sha in self.on_base else "diverged"}
            ).encode()
        return super().__call__(method, url, headers, body, timeout)


@pytest.fixture
def writes():
    fake = CommitWrites()
    fake.require("unit")
    return fake


def _sha(n: int) -> str:
    return f"{n:04x}".ljust(40, "e")


def _updates(writes: CommitWrites, count: int, *, onto: str = SHA_A) -> str:
    """`count` of GitHub's base merges stacked on `onto`; the top one."""
    head = onto
    for n in range(1, count + 1):
        base_tip = _sha(100 + n)
        writes.on_base.add(base_tip)
        merge = _sha(n)
        writes.commit(merge, [head, base_tip])
        head = merge
    return head


def _green_at(writes: CommitWrites, head: str) -> None:
    """The pull request's head is `head` (into BASE), and CI is green there."""
    writes.pulls[PR]["head"]["sha"] = head
    assert writes.pulls[PR]["base"]["ref"] == BASE
    writes.check(head, "unit", "success")


@pytest.mark.parametrize("count", range(1, issueci.MERGE_MAX_BRANCH_UPDATES + 1))
def test_a_head_of_base_merges_on_the_pushed_head_is_merged(
    client, db, objects, writes, clock, count,
):
    """MUTATION: restore `_pushing_task(ctx, tenant_id, run, head)` alone in
    `_merge` -- the run FAILs "not pushed by any of this run's tasks"."""
    running = _checking(client, db, objects, writes)
    head = _updates(writes, count)
    _green_at(writes, head)

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    (merge_wf,) = _merge_workflows(db)
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    assert _merge_task(db, merge_wf)["metadata"]["dispatch"]["merge_target"] == {
        "pull_request": integrator["id"], "pull_request_workflow": running["workflow_id"],
    }
    assert run["merge"]["head_sha"] == head


def test_one_base_merge_past_the_cap_is_refused(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes)
    _green_at(writes, _updates(writes, issueci.MERGE_MAX_BRANCH_UPDATES + 1))

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "not pushed by any of this run's tasks" in run["error"]
    assert (f"more than {issueci.MERGE_MAX_BRANCH_UPDATES} of GitHub's base merges"
            in run["error"])
    assert _merge_workflows(db) == []


def test_a_foreign_commit_that_is_not_a_merge_is_refused(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes)
    foreign = _sha(7)
    writes.commit(foreign, [SHA_A], by_github=False)
    _green_at(writes, foreign)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert f"{foreign[:12]} is not a merge (it has 1 parents)" in run["error"]
    assert _merge_workflows(db) == []


def test_a_foreign_commit_on_top_of_a_base_merge_is_refused(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes)
    foreign = _sha(7)
    writes.commit(foreign, [_updates(writes, 1)], by_github=False)
    _green_at(writes, foreign)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert f"{foreign[:12]} is not a merge" in run["error"]
    assert _merge_workflows(db) == []


def test_a_merge_whose_second_parent_is_not_on_the_base_is_refused(
    client, db, objects, writes, clock,
):
    running = _checking(client, db, objects, writes)
    merge, elsewhere = _sha(1), _sha(200)
    writes.commit(merge, [SHA_A, elsewhere])
    _green_at(writes, merge)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert f"{merge[:12]}'s second parent is not on {BASE}" in run["error"]
    assert _merge_workflows(db) == []


def test_a_merge_github_did_not_commit_and_sign_is_refused(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes)
    merge, base_tip = _sha(1), _sha(101)
    writes.on_base.add(base_tip)
    writes.commit(merge, [SHA_A, base_tip], by_github=False)
    _green_at(writes, merge)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert f"{merge[:12]} was not committed and signed by GitHub" in run["error"]
    assert _merge_workflows(db) == []


def test_the_claimed_merges_own_update_is_the_same_merge_not_a_second(
    client, db, objects, writes, clock,
):
    """MUTATION: drop the `through_base_merges` test of `claimed_head` in
    `_merge` -- a second merge workflow is submitted while the first runs."""
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")
    claimed = _read(client, clock, running["id"])
    assert claimed["merge"]["head_sha"] == SHA_A
    (merge_wf,) = _merge_workflows(db)

    # The merge step found the branch behind and updated it.
    head = _updates(writes, 1)
    _green_at(writes, head)
    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert [w["workflow_id"] for w in _merge_workflows(db)] == [merge_wf["workflow_id"]]
    assert run["merge"]["head_sha"] == SHA_A

    writes.pulls[PR]["merged"] = True
    writes.pulls[PR]["state"] = "closed"
    done = _read(client, clock, running["id"])
    assert done["state"] == "DONE" and done["green_sha"] == head


def _worker_value(name: str) -> str:
    """`name`'s literal in the worker's merge step, read as text: the API image
    does not carry the worker, so issueci cannot import it."""
    source = (ROOT / "apps/agent-worker/agent_worker/merge.py").read_text()
    found = re.findall(rf"^{name}\s*=\s*(.+?)\s*$", source, flags=re.MULTILINE)
    assert len(found) == 1, f"agent_worker/merge.py no longer assigns {name} once"
    return found[0]


def test_the_cap_is_the_workers():
    assert int(_worker_value("MERGE_MAX_BRANCH_UPDATES")) == issueci.MERGE_MAX_BRANCH_UPDATES


def test_githubs_committer_is_the_workers():
    assert ast.literal_eval(_worker_value("GITHUB_COMMITTER_EMAIL")) == \
        issueci.GITHUB_COMMITTER_EMAIL
