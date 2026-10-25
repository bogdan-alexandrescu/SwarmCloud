"""A platform-scope schedule's approvals are a platform admin's to decide (PLATFORM-SCHED-APPROVALS, part of #2157).

PR 2272 made every change to a schedule whose STORED `scope.mode` is
`platform` need a platform admin, `:run` included. Approving such a
schedule's gated run has the effect of `:run`, so BGD decided on 2026-10-24
("Platform admins only"): `POST /v1/approvals/{id}:approve` and `:reject`
refuse anyone else, with the same 403 `platform_admin_required` and the same
log line, when the item's stored schedule reference names a platform-scope
schedule. The refused item stays undecided and its subject unmoved.

Who is who, in eng (`test_approvals`'s app and in-memory Firestore):

* `alice` -- a member, the migration default;
* `dave` -- the workspace's `admin` by grant; NOT a platform admin;
* `erin` -- a platform admin who is not the platform owner;
* `root` -- PLATFORM_OWNER, a platform admin.

Items of a workspace-scope schedule are decided by a member exactly as before.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import logging
import secrets

import pytest

from swarm_api import forge, forgewrite, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings
from .test_approvals import (  # noqa: F401  (fixtures)
    FULL_PLAN,
    _audit,
    _checking,
    _decide,
    _docs,
    _firing,
    _grants,
    _inbox,
    _merge_workflows,
    _planned,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .test_issue_run_ci import PR, SHA_A, _read
from .test_roles_store import grant
from .test_schedule_tick import seed_schedule

SCHEDULE = "sch_000000000001"
DAVE = "dave@example.com"
#: The people a platform-scope schedule's items refuse, and the ones they do not.
REFUSED = ("alice", "dave")
ADMINS = ("erin", "root")


@pytest.fixture
def api_context(db, tokens, group_map, objects, writes, clock, pr_files):
    """`test_approvals`'s app, with a settings key so a role grant can be signed."""
    tokens = dict(tokens)
    return build_context(
        settings=api_settings(platform_owner="root@example.com", settings_key=secrets.token_hex(32)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=forge_fakes.GitHub(issues=[forge_fakes.issue(42)],
                                                         files={PR: pr_files})),
        forge_writer=forgewrite.GitHubWriter(send=writes, locate=writes.locate, fetch=writes.fetch),
    )


@pytest.fixture(autouse=True)
def workspace_admin(api_context):
    """`dave` is eng's workspace admin: the person most likely to be mistaken for a platform admin."""
    grant(api_context.roles, DAVE, roles.ADMIN)


def _schedule(db, mode: str, **gate) -> dict:
    """A gated schedule of eng, last edited by `carol` (a member of no group
    here), so the second-person rule refuses nobody these tests ask."""
    doc = seed_schedule(db, schedule_id=SCHEDULE, owner="carol@example.com", type_="failure-review",
                        repo_ids=(), gate={"run": "approve", **gate})
    doc["scope"] = {"mode": "platform"} if mode == "platform" else {"mode": "all"}
    return doc


def _run_item(client, db, mode: str, n: int = 1) -> dict:
    _schedule(db, mode)
    firing = _firing(db, f"{SCHEDULE}:{n}")
    (item,) = [i for i in _inbox(client, user="root") if i["kind"] == "run" and i["state"] == "pending"]
    assert item["subject"]["firing_id"] == firing["firing_id"]
    return item


def _merge_item(client, db, objects, writes, clock, mode: str) -> tuple[dict, dict]:
    """A failure-review-style run of the schedule, green, waiting on its merge approval."""
    _schedule(db, mode)
    running = _checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored["merge_approval"] = "required"
    stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE, "firing_id": f"{SCHEDULE}:9"}
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (item,) = _inbox(client, user="root", kind="merge")
    assert item["subject"]["schedule_id"] == SCHEDULE
    return item, running


def _plan_item(client, db, objects, mode: str) -> dict:
    _schedule(db, mode)
    run = _planned(client, db, objects)
    db.docs[f"issue_runs/{run['id']}"].setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}
    return run


def _record(db, item: dict) -> dict:
    return db.docs[f"approvals/{item['approval_id']}"]


def _refused(response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "platform_admin_required"
    assert "platform admin" in response.json()["message"]


# ---------------------------------------------------------------------------
# A platform-scope schedule's items
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED)
def test_a_member_and_a_workspace_admin_cannot_decide_a_platform_scope_run_approval(client, db, user):
    item = _run_item(client, db, "platform")
    firing = db.docs[f"schedule_firings/{SCHEDULE}:1"]
    _refused(_decide(client, item["approval_id"], "approve", user=user, digest=item["digest"]))
    _refused(_decide(client, item["approval_id"], "reject", user=user, reason="not this week"))
    assert _record(db, item)["state"] == "pending" and _record(db, item).get("decided_by") is None
    assert firing["state"] == "awaiting_approval" and "run_approval" not in firing
    assert _audit(db, SCHEDULE) == []


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admin_approves_and_rejects_a_platform_scope_run_approval(client, db, user):
    item = _run_item(client, db, "platform")
    approved = _decide(client, item["approval_id"], "approve", user=user, digest=item["digest"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["state"] == "approved"
    assert db.docs[f"schedule_firings/{SCHEDULE}:1"]["run_approval"]["by"] == f"{user}@example.com"

    other = _run_item(client, db, "platform", n=2)
    rejected = _decide(client, other["approval_id"], "reject", user=user, reason="not this week")
    assert rejected.status_code == 200, rejected.text
    assert db.docs[f"schedule_firings/{SCHEDULE}:2"]["state"] == "rejected"


@pytest.mark.parametrize("user", REFUSED)
def test_a_member_and_a_workspace_admin_cannot_decide_a_platform_scope_merge_approval(
        client, db, objects, writes, clock, user):
    item, running = _merge_item(client, db, objects, writes, clock, "platform")
    _refused(_decide(client, item["approval_id"], "approve", user=user, digest=item["digest"]))
    _refused(_decide(client, item["approval_id"], "reject", user=user, reason="not this one"))
    assert _record(db, item)["state"] == "pending"
    assert not db.docs[f"issue_runs/{running['id']}"].get("merge_approved")
    assert _merge_workflows(db) == []


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admin_approves_a_platform_scope_merge_approval(client, db, objects, writes, clock, user):
    item, running = _merge_item(client, db, objects, writes, clock, "platform")
    approved = _decide(client, item["approval_id"], "approve", user=user, digest=item["digest"])
    assert approved.status_code == 200, approved.text
    assert db.docs[f"issue_runs/{running['id']}"]["merge_approved"]["by"] == f"{user}@example.com"
    assert len(_merge_workflows(db)) == 1


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admin_rejects_a_platform_scope_merge_approval(client, db, objects, writes, clock, user):
    item, _ = _merge_item(client, db, objects, writes, clock, "platform")
    rejected = _decide(client, item["approval_id"], "reject", user=user, reason="not this one")
    assert rejected.status_code == 200, rejected.text
    assert _record(db, item)["state"] == "rejected"
    assert _merge_workflows(db) == []


@pytest.mark.parametrize("user", REFUSED)
def test_a_member_and_a_workspace_admin_cannot_decide_a_platform_scope_plan_from_the_inbox(
        client, db, objects, user):
    run = _plan_item(client, db, objects, "platform")
    _refused(_decide(client, f"run:{run['id']}", "approve", user=user, digest=run["plan_digest"]))
    _refused(_decide(client, f"run:{run['id']}", "reject", user=user, reason="out of scope"))
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"
    assert not _docs(db, "workflows")


def test_a_platform_admin_decides_a_platform_scope_plan_from_the_inbox(client, db, objects):
    run = _plan_item(client, db, objects, "platform")
    approved = _decide(client, f"run:{run['id']}", "approve", user="erin", digest=run["plan_digest"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"


def test_the_refusal_is_logged_with_who_asked_and_what(client, db, caplog):
    item = _run_item(client, db, "platform")
    with caplog.at_level(logging.WARNING, logger="swarm_api.routes.schedules"):
        _refused(_decide(client, item["approval_id"], "approve", user="dave", digest=item["digest"]))
        _refused(_decide(client, item["approval_id"], "reject", user="dave", reason="no"))
    lines = [r.getMessage() for r in caplog.records if "platform_admin_required" in r.getMessage()]
    assert len(lines) == 2, [r.getMessage() for r in caplog.records]
    for line, action in zip(lines, ("approve", "reject")):
        assert f"schedule={SCHEDULE}" in line and f"email={DAVE}" in line
        assert "tenant=eng" in line and f"action={action}" in line


# ---------------------------------------------------------------------------
# A workspace-scope schedule's items: exactly as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED)
def test_a_workspace_scope_run_approval_is_decided_by_a_member_as_before(client, db, user):
    item = _run_item(client, db, "workspace")
    approved = _decide(client, item["approval_id"], "approve", user=user, digest=item["digest"])
    assert approved.status_code == 200, approved.text
    other = _run_item(client, db, "workspace", n=2)
    rejected = _decide(client, other["approval_id"], "reject", user=user, reason="not this week")
    assert rejected.status_code == 200, rejected.text


def test_a_workspace_scope_merge_approval_is_decided_by_a_member_as_before(client, db, objects, writes, clock):
    item, _ = _merge_item(client, db, objects, writes, clock, "workspace")
    approved = _decide(client, item["approval_id"], "approve", user="alice", digest=item["digest"])
    assert approved.status_code == 200, approved.text
    assert len(_merge_workflows(db)) == 1


@pytest.mark.parametrize("verb,body", [("approve", None), ("reject", {"reason": "out of scope"})])
def test_a_workspace_scope_plan_is_decided_by_a_member_from_the_inbox_as_before(client, db, objects, verb, body):
    run = _plan_item(client, db, objects, "workspace")
    response = _decide(client, f"run:{run['id']}", verb, user="alice", **(body or {"digest": run["plan_digest"]}))
    assert response.status_code == 200, response.text
