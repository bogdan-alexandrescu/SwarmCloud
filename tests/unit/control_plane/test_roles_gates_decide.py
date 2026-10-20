"""The role floor on every decision: approve, reject, the merge switch and a
`/swarmcloud approve` comment (docs/design/workspace-roles.md §4.1, §4.8;
lane ROLES-GATES-DECIDE, part of #1518).

Who is who, in eng (`eng@example.com`):

* `alice`, `dave` -- members, no grant (so `member`, the migration default);
* `vic` -- in eng's group, narrowed to `viewer` by a signed grant;
* `ops` -- a platform admin in no tenant group: reaches eng only through
  `platform_admin_reach` (`tenant_via == "platform_admin"`);
* `root` -- PLATFORM_OWNER, a platform admin and a member of eng.

What these hold, against the real app and the in-memory Firestore, with S5's
fixtures (test_approvals.py):

  * with `role_member_required`'s switch on, a `viewer` is 403 on `:approve`,
    `:reject` (a record and a projected plan) and `:merge-mode`, and nothing
    moves: the item stays pending, the run PLANNED, the gate unchanged;
  * with the switch off the same decision goes through and the log names the
    refusal it would have made (report-only, §4.9);
  * a `member` decides; a non-member platform admin decides through the
    reach, and a gate's `approvers` list that does not name them still
    refuses them; a list naming a `viewer` still refuses the viewer -- the
    role is the floor, the list the narrower rule on top;
  * the decision reads the role FRESH: a cache primed with `member` on this
    instance, a grant to `viewer` written by another, then approve: refused;
  * a `/swarmcloud approve` comment from a `viewer` approves nothing and its
    reply names the code -- and names no admin's address, because a GitHub
    thread is not the workspace's roster.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import logging
import secrets

import pytest

from swarm_api import approvals, forge, forgewrite, ghapprove, refusals, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.roles import RoleStore
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, api_settings, auth_header, seed_tenant
from .test_approvals import (  # noqa: F401 -- S5's fixtures, used by name
    OWNER,
    SWEEPER,
    _decide,
    _grants,
    _planned,
    _sweep,
    _switch,
    built_types,
    client,
    clock,
    pr_files,
    writes,
)
from .test_github_comment_approvals import _command, _comment, _connect, _replies
from .test_issue_run_ci import PR
from .test_issue_runs import _docs
from .test_roles_store import grant
from .test_schedule_tick import seed_schedule

MEMBER_SWITCH = refusals.env_name("role_member_required")
VIC = "vic@example.com"
OPS = "ops@example.com"


@pytest.fixture
def group_map():
    return {
        "alice@example.com": (ENG_GROUP,),
        "dave@example.com": (ENG_GROUP,),
        VIC: (ENG_GROUP,),
        OPS: (ADMIN_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "carol@example.com": (),
        OWNER: (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def settings():
    return api_settings(platform_owner=OWNER, rollup_sweeper_users=(SWEEPER,),
                        platform_admin_reach=True, settings_key=secrets.token_hex(32))


@pytest.fixture
def api_context(db, tokens, group_map, objects, writes, clock, pr_files, settings):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=settings,
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


@pytest.fixture
def vic_is_viewer(api_context):
    seed_tenant(api_context.db, "eng")
    grant(api_context.roles, VIC, roles.VIEWER)


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")


@pytest.fixture
def switch_off(monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "off")


def _proposal(api_context, *, key: str = "p1", approvers: object = "members") -> dict:
    """A pending `proposal` record: no subject to move, so only the decision shows."""
    return approvals.request(
        api_context.db, api_context.now(), tenant_id="eng", kind=approvals.PROPOSAL, key=key,
        subject={"proposal": key}, digest=f"digest-{key}", summary="file the flake as an issue",
        approvers=approvers, ttl=72,
    )


def _state(db, item: dict) -> str:
    return db.docs[f"approvals/{item['approval_id']}"]["state"]


def _as(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(user)
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def _decide_as(client, item_id: str, verb: str, user: str, tenant: str | None = None, **body):
    return client.post(f"/v1/approvals/{item_id}:{verb}", headers=_as(user, tenant), json=body)


def _role_refused(response) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "role_member_required"
    assert body["detail"]["role"] == roles.VIEWER and body["detail"]["required"] == roles.MEMBER


# --------------------------------------------------------------------------
# a viewer decides nothing
# --------------------------------------------------------------------------

def test_a_viewer_is_refused_approve_and_reject_on_a_record_and_it_stays_pending(
    client, api_context, db, vic_is_viewer, switch_on,
):
    item = _proposal(api_context)
    _role_refused(_decide_as(client, item["approval_id"], "approve", "vic", digest=item["digest"]))
    assert _state(db, item) == approvals.PENDING
    _role_refused(_decide_as(client, item["approval_id"], "reject", "vic", reason="not now"))
    assert _state(db, item) == approvals.PENDING
    assert db.docs[f"approvals/{item['approval_id']}"]["decided_by"] is None


def test_a_viewer_is_refused_a_projected_plans_approve_and_reject_and_it_stays_planned(
    client, api_context, db, objects, vic_is_viewer, switch_on,
):
    run = _planned(client, db, objects)
    item_id = f"run:{run['id']}"
    _role_refused(_decide_as(client, item_id, "approve", "vic", digest=run["plan_digest"]))
    _role_refused(_decide_as(client, item_id, "reject", "vic", reason="not now"))
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"
    assert not _docs(db, "workflows")


def test_a_viewer_is_refused_the_merge_switch_and_the_gate_is_unchanged(
    client, api_context, db, vic_is_viewer, switch_on,
):
    sweep = _sweep(client, db)
    _role_refused(_switch(client, sweep, "auto", user="vic"))
    _role_refused(_switch(client, sweep, "approve", user="vic"))
    stored = db.docs[f"schedules/{sweep['schedule_id']}"]
    assert stored["gate"]["merge"] == "approve" and stored["revision"] == sweep["revision"]


def test_with_the_switch_off_a_viewer_decides_and_the_refusal_is_logged(
    client, api_context, db, vic_is_viewer, switch_off, caplog,
):
    item = _proposal(api_context)
    with caplog.at_level(logging.WARNING):
        response = _decide_as(client, item["approval_id"], "approve", "vic", digest=item["digest"])
    assert response.status_code == 200, response.text
    assert _state(db, item) == approvals.APPROVED
    assert "refusal report-only code=role_member_required" in caplog.text


# --------------------------------------------------------------------------
# who does decide
# --------------------------------------------------------------------------

def test_a_member_approves(client, api_context, db, vic_is_viewer, switch_on):
    item = _proposal(api_context)
    response = _decide_as(client, item["approval_id"], "approve", "alice", digest=item["digest"])
    assert response.status_code == 200, response.text
    assert _state(db, item) == approvals.APPROVED
    assert db.docs[f"approvals/{item['approval_id']}"]["decided_by"] == "alice@example.com"


def test_a_member_approves_a_projected_plan(client, api_context, db, objects, vic_is_viewer, switch_on):
    run = _planned(client, db, objects)
    response = _decide_as(client, f"run:{run['id']}", "approve", "dave", digest=run["plan_digest"])
    assert response.status_code == 200, response.text
    assert response.json()["run"]["state"] == "RUNNING"


def test_a_non_member_platform_admin_approves_through_the_reach(
    client, api_context, db, vic_is_viewer, switch_on,
):
    item = _proposal(api_context)
    response = _decide_as(client, item["approval_id"], "approve", "ops", "eng", digest=item["digest"])
    assert response.status_code == 200, response.text
    assert db.docs[f"approvals/{item['approval_id']}"]["decided_by"] == OPS
    acted = [r for r in db.dump("admin_audit/").values()
             if r["action"] == roles.AUDIT_PLATFORM_ADMIN_ACTED and r["by"] == OPS]
    assert acted and acted[-1]["detail"]["route"] == "/v1/approvals/{item_id}:approve"


def test_an_approvers_list_that_does_not_name_the_platform_admin_still_refuses_them(
    client, api_context, db, vic_is_viewer, switch_on,
):
    item = _proposal(api_context, approvers=["alice@example.com"])
    response = _decide_as(client, item["approval_id"], "approve", "ops", "eng", digest=item["digest"])
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "approver_not_allowed"
    assert _state(db, item) == approvals.PENDING


def test_an_approvers_list_naming_a_viewer_still_refuses_them(
    client, api_context, db, vic_is_viewer, switch_on,
):
    item = _proposal(api_context, approvers=[VIC, "alice@example.com"])
    _role_refused(_decide_as(client, item["approval_id"], "approve", "vic", digest=item["digest"]))
    assert _state(db, item) == approvals.PENDING
    # The list still admits the member it names.
    ok = _decide_as(client, item["approval_id"], "approve", "alice", digest=item["digest"])
    assert ok.status_code == 200, ok.text


def test_the_decision_reads_the_role_fresh(client, api_context, db, settings, switch_on):
    seed_tenant(db, "eng")
    item = _proposal(api_context)
    # This instance's cache says member: no grant yet, so the default.
    assert api_context.roles.person_role("eng", VIC).role == roles.MEMBER
    # Another instance narrows vic a moment later; this one's cache is not told.
    grant(RoleStore(db, settings, metrics=ApiMetrics(), now=api_context.now), VIC, roles.VIEWER)
    assert api_context.roles.person_role("eng", VIC).role == roles.MEMBER   # still cached
    _role_refused(_decide_as(client, item["approval_id"], "approve", "vic", digest=item["digest"]))
    assert _state(db, item) == approvals.PENDING


# --------------------------------------------------------------------------
# a `/swarmcloud approve` comment
# --------------------------------------------------------------------------

@pytest.fixture
def eng_threads(db):
    seed_tenant(db, "eng")
    seed_schedule(db)
    _connect(db, "alice@example.com", "alice-gh")
    _connect(db, VIC, "vic-gh", user_id=1009)


def test_a_viewers_comment_approves_nothing_and_the_reply_names_the_code(
    client, api_context, db, objects, writes, eng_threads, vic_is_viewer, switch_on,
):
    grant(api_context.roles, "dave@example.com", roles.ADMIN)
    run = _planned(client, db, objects)
    _comment(writes, 42, _command(run["plan_digest"]), login="vic-gh", user_id=1009)

    counts = ghapprove.poll(api_context)

    assert counts["approved"] == 0, counts
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"
    assert not _docs(db, "workflows")
    (reply,) = _replies(writes, 42)
    assert "role_member_required" in reply
    # A GitHub thread is not the roster: no admin's address in the reply.
    assert "dave@example.com" not in reply and "@example.com" not in reply


def test_with_the_switch_off_a_viewers_comment_approves_and_is_logged(
    client, api_context, db, objects, writes, eng_threads, vic_is_viewer, switch_off, caplog,
):
    run = _planned(client, db, objects)
    _comment(writes, 42, _command(run["plan_digest"]), login="vic-gh", user_id=1009)
    with caplog.at_level(logging.WARNING):
        counts = ghapprove.poll(api_context)
    assert counts["approved"] == 1, counts
    assert db.docs[f"issue_runs/{run['id']}"]["approved_by"] == VIC
    assert "refusal report-only code=role_member_required" in caplog.text


def test_a_members_comment_still_approves_with_the_switch_on(
    client, api_context, db, objects, writes, eng_threads, vic_is_viewer, switch_on,
):
    run = _planned(client, db, objects)
    _comment(writes, 42, _command(run["plan_digest"]), login="alice-gh", user_id=1001)
    assert ghapprove.poll(api_context)["approved"] == 1
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "RUNNING"
