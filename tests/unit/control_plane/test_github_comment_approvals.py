"""GitHub-comment approvals: `/swarmcloud approve <digest>` (docs/schedules.md §4.9, SD9, lane S12).

What these tests hold, against the real app and the in-memory Firestore and
GitHub (`forge_fakes.GitHubWrites`), with S5's fixtures (test_approvals.py):

  * A comment from a verified, current member with the current digest
    approves, once, through the console's own approval path, and the
    schedule's audit records `via: github_comment`, the comment id, the login
    and the member it resolved to.
  * Each of these approves nothing: a stale digest (answered once, with the
    current command), an edited comment, a bot (no answer), an unmapped
    login, a revoked connection, a login whose GitHub user id is not the
    connection's, a login whose member has left the tenant, the last editor
    on a merge-tier item, the account the platform's own comments are
    written with, and a comment written through an app.
  * A comment on a one-person tenant's own hold is answered: approve in the
    console. Two comments, or a comment racing the console's approve, make
    one transition.
  * The plan comment and the merge request show the exact command with the
    current digest, and the poll never reads the platform's own comments as
    a command. The schedule tick's advance step calls the poll.

No credentials, no network, no emulator.
"""

from __future__ import annotations


import pytest

from swarm_api import approvals, forgeapp, ghapprove, schedaudit, schedulefire
from swarm_api.issuecomments import APPROVE_COMMAND, render_plan_comment
from swarm_api.issueruns import IssueRuns
from swarm_common.identity import tenant_id_for_user

from .conftest import seed_tenant
from .test_approvals import (  # noqa: F401 -- S5's fixtures, used by name
    OWNER,
    _bootstrap_plan,
    _checking,
    _decide,
    _grants,
    _inbox,
    _merge_workflows,
    _planned,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    switch_on,
    writes,
)
from .test_issue_run_ci import PR, SHA_A, _read
from .test_issue_runs import FULL_PLAN, _docs
from .test_schedule_tick import seed_schedule

SCHEDULE = "sch_000000000001"
GH_ID = {"alice@example.com": 1001, "dave@example.com": 1004, "carol@example.com": 1003, OWNER: 1000}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _connect(db, email: str, login: str, *, tenant_id: str = "eng", state: str = forgeapp.ACTIVE,
             user_id: int | None = -1) -> None:
    """The onboarding connection forgeapp._store writes, with the fields §4.9 reads."""
    conn_id = forgeapp.connection_id_for(tenant_id, email)
    db.docs[f"{forgeapp.CONNECTIONS}/{conn_id}"] = {
        "connection_id": conn_id, "tenant_id": tenant_id, "user": email, "forge": "github",
        "forge_login": login, "forge_user_id": GH_ID.get(email) if user_id == -1 else user_id,
        "state": state,
    }


def _comment(writes, number: int, body: str, *, login: str, user_id: int | None = None,
             kind: str = "User", edited: bool = False, via_app: bool = False) -> int:
    cid = writes.add_comment(number, body, login=login)
    stamp = "2026-10-03T13:00:00Z"
    writes.comments[cid].update({
        "user": {"login": login, "id": user_id, "type": kind},
        "created_at": stamp,
        "updated_at": "2026-10-03T13:05:00Z" if edited else stamp,
        "performed_via_github_app": {"slug": "some-app"} if via_app else None,
    })
    return cid


def _command(digest: str) -> str:
    return f"{APPROVE_COMMAND} {digest}"


def _poll(api_context) -> dict:
    return ghapprove.poll(api_context)


def _state(db, run_id: str) -> str:
    return db.docs[f"issue_runs/{run_id}"]["state"]


def _replies(writes, number: int) -> list[str]:
    return [c["body"] for c in writes.on_issue(number) if c["body"].startswith("<!-- swarmcloud-approval:")]


def _scheduled(db, run: dict) -> None:
    db.docs[f"issue_runs/{run['id']}"].setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}


def _audit(db) -> list[dict]:
    rows = [v for k, v in db.docs.items() if k.startswith("schedule_audit/") and v.get("schedule_id") == SCHEDULE]
    return sorted(rows, key=lambda r: r["at"])


@pytest.fixture
def eng(db):
    seed_tenant(db, "eng")
    seed_schedule(db)
    _connect(db, "alice@example.com", "alice-gh")
    _connect(db, "dave@example.com", "Dave-GH")
    _connect(db, OWNER, "root-gh")


# --------------------------------------------------------------------------
# the command, as shown and as parsed
# --------------------------------------------------------------------------

def test_the_plan_comment_shows_the_exact_command_with_the_current_digest(client, db, objects, writes, eng):
    run = _planned(client, db, objects)
    (plan_comment,) = [c for c in writes.on_issue(42) if "plan:" in c["body"].splitlines()[0]
                       or "SwarmCloud plan" in c["body"]]
    assert f"`{_command(run['plan_digest'])}`" in plan_comment["body"]
    # Our own comment opens with the marker, so the poll never reads it as a command.
    assert ghapprove.parse_command(plan_comment["body"]) is None
    stored = IssueRuns(db).get("eng", run["id"])
    assert _command(run["plan_digest"]) in render_plan_comment(stored)


def test_only_a_first_line_that_is_exactly_the_command_is_one():
    digest = "f" * 64
    assert ghapprove.parse_command(_command(digest)) == digest
    assert ghapprove.parse_command(f"\n  {_command(digest)}  \nthanks!") == digest
    assert ghapprove.parse_command(f"/SwarmCloud Approve {digest}") == digest
    assert ghapprove.parse_command(f"> {_command(digest)}") is None
    assert ghapprove.parse_command(f"please {_command(digest)}") is None
    assert ghapprove.parse_command(f"lgtm\n{_command(digest)}") is None
    assert ghapprove.parse_command(f"{_command(digest)} extra") is None
    assert ghapprove.parse_command(f"{ghapprove.approval_marker('x')}\n{_command(digest)}") is None


# --------------------------------------------------------------------------
# a verified member approves a plan
# --------------------------------------------------------------------------

def test_a_verified_members_comment_approves_the_plan_once_and_the_audit_says_how(
    client, db, objects, writes, api_context, eng,
):
    run = _planned(client, db, objects)
    _scheduled(db, run)
    cid = _comment(writes, 42, _command(run["plan_digest"]), login="dave-gh", user_id=1004)
    second = _comment(writes, 42, _command(run["plan_digest"]), login="alice-gh", user_id=1001)

    counts = _poll(api_context)

    assert counts["approved"] == 1, counts
    assert _state(db, run["id"]) == "RUNNING"
    assert db.docs[f"issue_runs/{run['id']}"]["approved_by"] == "dave@example.com"
    assert len(_docs(db, "workflows")) == 1
    (entry,) = [e for e in _audit(db) if e["action"] == schedaudit.APPROVED]
    assert entry["by"] == "dave@example.com"
    assert entry["detail"]["via"] == "github_comment"
    assert entry["detail"]["comment_id"] == cid
    assert entry["detail"]["login"] == "dave-gh"
    assert entry["detail"]["member"] == "dave@example.com"
    assert entry["detail"]["approval_id"] == f"run:{run['id']}"

    # The run left the inbox: the next poll reads nothing more and the
    # second command never makes a second transition or a reply.
    assert _poll(api_context)["approved"] == 0
    assert len(_docs(db, "workflows")) == 1
    assert _replies(writes, 42) == []
    assert second > cid


def test_a_comment_racing_the_consoles_approve_makes_one_transition(client, db, objects, writes, api_context, eng):
    run = _planned(client, db, objects)
    _comment(writes, 42, _command(run["plan_digest"]), login="dave-gh", user_id=1004)
    items = ghapprove.candidates(api_context)
    assert [i.approval_id for i in items] == [f"run:{run['id']}"]

    assert _decide(client, f"run:{run['id']}", "approve", digest=run["plan_digest"]).status_code == 200
    # The poll acts on the item it read before the console decided it.
    counts = ghapprove.poll_thread(api_context, items[0], "unused-in-the-fake")
    assert counts["approved"] == 0
    assert len(_docs(db, "workflows")) == 1
    assert db.docs[f"issue_runs/{run['id']}"]["approved_by"] == "alice@example.com"
    assert _replies(writes, 42) == []


# --------------------------------------------------------------------------
# what approves nothing
# --------------------------------------------------------------------------

def test_a_stale_digest_approves_nothing_and_is_answered_once_with_the_current_command(
    client, db, objects, writes, api_context, eng,
):
    run = _planned(client, db, objects)
    _comment(writes, 42, _command("0" * 64), login="dave-gh", user_id=1004)

    _poll(api_context)
    _poll(api_context)

    assert _state(db, run["id"]) == "PLANNED"
    (reply,) = _replies(writes, 42)
    assert f"`{_command(run['plan_digest'])}`" in reply
    assert "approved nothing" in reply


@pytest.mark.parametrize("case", ["edited", "unmapped", "revoked", "wrong_user_id", "own_login", "via_app"])
def test_each_of_these_approves_nothing_and_is_answered_once(client, db, objects, writes, api_context, eng, case):
    run = _planned(client, db, objects)
    digest = run["plan_digest"]
    login, user_id, extra = "dave-gh", 1004, {}
    if case == "edited":
        extra = {"edited": True}
    elif case == "unmapped":
        login, user_id = "mallory-gh", 666
    elif case == "revoked":
        _connect(db, "dave@example.com", "dave-gh", state=forgeapp.REVOKED)
    elif case == "wrong_user_id":
        user_id = 4242  # the login was renamed and reused: the id is not the member's
    elif case == "own_login":
        # The tenant's forge credential writes the platform's comments as this account.
        login = db.docs[f"issue_runs/{run['id']}"]["forge_login"]
        _connect(db, "dave@example.com", login, user_id=None)
    elif case == "via_app":
        extra = {"via_app": True}
    _comment(writes, 42, _command(digest), login=login, user_id=user_id, **extra)

    _poll(api_context)
    _poll(api_context)

    assert _state(db, run["id"]) == "PLANNED"
    assert not _docs(db, "workflows")
    assert len(_replies(writes, 42)) == 1


def test_a_bots_comment_approves_nothing_and_is_not_answered(client, db, objects, writes, api_context, eng):
    run = _planned(client, db, objects)
    _connect(db, "dave@example.com", "dave-gh[bot]")
    _comment(writes, 42, _command(run["plan_digest"]), login="dave-gh[bot]", user_id=1004, kind="Bot")
    _poll(api_context)
    assert _state(db, run["id"]) == "PLANNED"
    assert _replies(writes, 42) == []


def test_a_login_whose_member_has_left_the_tenant_approves_nothing(
    client, db, objects, writes, api_context, monkeypatch, eng,
):
    run = _planned(client, db, objects)
    # Removed from the tenant's group since connecting: the directory says so now.
    groups = api_context.authenticator._groups
    monkeypatch.setitem(groups._mapping, "dave@example.com", ())
    _comment(writes, 42, _command(run["plan_digest"]), login="dave-gh", user_id=1004)
    _poll(api_context)
    assert _state(db, run["id"]) == "PLANNED"
    (reply,) = _replies(writes, 42)
    assert "no longer a member" in reply


def test_a_connection_in_another_tenant_maps_to_nobody_here(client, db, objects, writes, api_context, eng):
    run = _planned(client, db, objects)
    _connect(db, "bob@example.com", "bob-gh", tenant_id="research", user_id=2002)
    _comment(writes, 42, _command(run["plan_digest"]), login="bob-gh", user_id=2002)
    _poll(api_context)
    assert _state(db, run["id"]) == "PLANNED"
    assert len(_replies(writes, 42)) == 1


def test_a_comment_cannot_step_round_a_hold_the_creator_comments_and_a_second_member_approves(
    client, db, objects, writes, api_context, eng, switch_on,
):
    run = _planned(client, db, objects, _bootstrap_plan())
    assert db.docs[f"issue_runs/{run['id']}"]["approval_hold"]["approvers"] == "second_member"
    _comment(writes, 42, _command(run["plan_digest"]), login="alice-gh", user_id=1001)
    _poll(api_context)
    assert _state(db, run["id"]) == "PLANNED"
    (reply,) = _replies(writes, 42)
    assert "second member" in reply

    _comment(writes, 42, _command(run["plan_digest"]), login="dave-gh", user_id=1004)
    _poll(api_context)
    assert _state(db, run["id"]) == "RUNNING"
    assert len(_replies(writes, 42)) == 1


def test_a_one_person_tenants_own_hold_is_answered_approve_in_the_console(
    client, db, objects, writes, api_context, switch_on,
):
    personal = tenant_id_for_user("carol@example.com")
    run = _planned(client, db, objects, _bootstrap_plan(), user="carol")
    assert run["tenant_id"] == personal
    _connect(db, "carol@example.com", "carol-gh", tenant_id=personal)
    _comment(writes, 42, _command(run["plan_digest"]), login="carol-gh", user_id=1003)
    _poll(api_context)
    assert _state(db, run["id"]) == "PLANNED"
    (reply,) = _replies(writes, 42)
    assert "console" in reply and "typed" in reply


# --------------------------------------------------------------------------
# merges: the request on the pull request, and a comment approving it
# --------------------------------------------------------------------------

def _merge_waiting(client, db, objects, writes, clock, *, scheduled: bool) -> tuple[dict, dict]:
    running = _checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored["merge_approval"] = "required"
    if scheduled:
        stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge")
    return running, item


def test_the_merge_request_shows_the_command_once_and_a_members_comment_merges(
    client, db, objects, writes, clock, api_context, eng,
):
    running, item = _merge_waiting(client, db, objects, writes, clock, scheduled=True)
    digest = approvals.merge_digest_text(item["digest"])
    requests = [c for c in writes.on_issue(PR) if c["body"].startswith(ghapprove.approval_marker(item["approval_id"]))]
    assert len(requests) == 1, "one merge request per record, however many visits"
    assert f"    {_command(digest)}" in requests[0]["body"]
    assert db.docs[f"approvals/{item['approval_id']}"]["request_comment_id"] == requests[0]["id"]

    # alice created the schedule, so she last edited its gate: a merge is not hers.
    _comment(writes, PR, _command(digest), login="alice-gh", user_id=1001)
    _poll(api_context)
    assert _merge_workflows(db) == []
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "pending"
    (reply,) = _replies(writes, PR)[1:]
    assert "last changed this schedule" in reply

    cid = _comment(writes, PR, _command(digest), login="dave-gh", user_id=1004)
    assert _poll(api_context)["approved"] == 1
    decided = db.docs[f"approvals/{item['approval_id']}"]
    assert decided["state"] == "approved" and decided["decided_by"] == "dave@example.com"
    assert db.docs[f"issue_runs/{running['id']}"]["merge_approved"]["head_sha"] == SHA_A
    (entry,) = [e for e in _audit(db) if e["action"] == schedaudit.APPROVED]
    assert entry["detail"]["via"] == "github_comment" and entry["detail"]["comment_id"] == cid
    assert entry["detail"]["member"] == "dave@example.com" and entry["detail"]["login"] == "dave-gh"

    _read(client, clock, running["id"])
    assert len(_merge_workflows(db)) == 1


def test_a_merge_comment_with_another_heads_digest_approves_nothing(
    client, db, objects, writes, clock, api_context, eng,
):
    running, item = _merge_waiting(client, db, objects, writes, clock, scheduled=False)
    _comment(writes, PR, _command("b" * 40 + ":MERGE"), login="dave-gh", user_id=1004)
    _poll(api_context)
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "pending"
    (reply,) = _replies(writes, PR)[1:]
    assert _command(approvals.merge_digest_text(item["digest"])) in reply
    assert "merge_approved" not in db.docs[f"issue_runs/{running['id']}"] or \
        db.docs[f"issue_runs/{running['id']}"]["merge_approved"] is None


# --------------------------------------------------------------------------
# the poll: bounded, cursored, and on the tick
# --------------------------------------------------------------------------

def test_the_poll_reads_only_threads_with_a_pending_approval_and_sends_its_cursor(
    client, db, objects, writes, api_context, eng,
):
    assert ghapprove.poll(api_context)["threads"] == 0
    assert [c for c in writes.calls if c[0] == "GET" and "/comments" in c[1]] == []
    run = _planned(client, db, objects)
    before = len(writes.calls)
    assert ghapprove.poll(api_context)["threads"] == 1
    (call,) = [c for c in writes.calls[before:] if c[0] == "GET" and "/comments" in c[1]]
    assert "issues/42/comments" in call[1] and "since=" in call[1]
    (cursor,) = [v for k, v in db.docs.items() if k.startswith(f"{ghapprove.CURSORS}/")]
    assert cursor["approval_id"] == f"run:{run['id']}" and cursor["tenant_id"] == "eng"


def test_the_pick_bounds_a_tick_and_one_tenants_share_of_it():
    items = [ghapprove.Item(approval_id=f"run:{t}{n:03d}", tenant_id=t, kind="plan", run=None,
                            thread=None, digest="d", requested_at=None)
             for t in ("a", "b") for n in range(30)]
    picked = ghapprove.pick(items, minute=0)
    assert len(picked) == 2 * ghapprove.PER_TENANT_PER_TICK
    assert {i.tenant_id for i in picked} == {"a", "b"}
    assert ghapprove.pick(items, minute=1) != picked
    assert ghapprove.pick([], minute=5) == []


def test_the_schedule_ticks_advance_step_polls_the_comments(api_context, monkeypatch):
    called = []
    monkeypatch.setattr(ghapprove, "poll", lambda ctx, out_of_time: called.append(ctx) or {})
    schedulefire.run_tick(api_context, environ={"SCHEDULES_ENABLED": "true"})
    assert called == [api_context]


def test_the_poll_never_raises(api_context, monkeypatch):
    def boom(ctx):
        raise RuntimeError("the inbox read failed")

    monkeypatch.setattr(ghapprove, "candidates", boom)
    assert ghapprove.poll(api_context)["errors"] == 1


def test_the_member_map_reads_only_active_connections_in_the_tenant(db):
    _connect(db, "dave@example.com", "Dave-GH")
    assert ghapprove.member_for(db, "eng", "dave-gh", 1004) == "dave@example.com"
    assert ghapprove.member_for(db, "eng", "dave-gh", 9) is None
    assert ghapprove.member_for(db, "research", "dave-gh", 1004) is None
    _connect(db, "carol@example.com", "nobody-id", user_id=None)
    assert ghapprove.member_for(db, "eng", "NOBODY-ID", 77) == "carol@example.com"
    _connect(db, "alice@example.com", "dave-gh", user_id=1004)
    assert ghapprove.member_for(db, "eng", "dave-gh", 1004) is None, "two members claiming one account"
