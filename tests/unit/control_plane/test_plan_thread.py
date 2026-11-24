"""The plan thread and its comment route
(docs/design/plans-pipeline.md §3.4; lane P3 PLANS-THREAD of §11).

  1. WHO. `POST /v1/runs/{id}/plan:comment` is the comment level: a viewer is
     refused with the role floor's refusal (`role_member_required`) and
     nothing is written; another tenant's run is the same 404 as no run, for
     the comment and for the thread on the run read.
  2. WHAT IS STORED. A member's comment is kept under
     `issue_runs/{id}/plan_thread/{entry_id}` with the digest and revision it
     was written on, masked like every stored text and bounded at 4,000
     characters; an over-long one is refused.
  3. ON WHICH REVISION. A digest that is none of the run's revisions is 409
     `plan_changed`; an older revision's is accepted and says so; an anchor
     not in that revision (a step that does not exist) is 422 `anchor_unknown`.
  4. A COMMENT IS DATA. It changes neither the plan, its digest, its revision
     nor the run's state, whatever it says.
  5. THE SERVED THREAD. `GET /v1/runs/{id}` serves `plan_thread`: the stored
     entries plus the events derived from the run, merged in time order
     (planned, approved, step agents, pull request, CI rounds, review, merged,
     keyword), bounded at PLAN_THREAD_LIMIT with `thread_truncated`.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import refusals, roles
from swarm_api.issueruns import RUNS_COLLECTION

from .conftest import auth_header
from .test_issue_runs import (  # noqa: F401 -- fixtures, used by name
    PLAN,
    _approve,
    _edit,
    _planned,
    _run,
    api_context,
    forge_tokens,
    github,
)
from .test_roles_store import grant

MEMBER_SWITCH = refusals.env_name("role_member_required")
THREAD = "plan_thread"


def _comment(client, run_id, body, user="alice"):
    return client.post(f"/v1/runs/{run_id}/plan:comment", headers=auth_header(user), json=body)


def _body(run, *, part="step", ref="ui", text="Should the sort be stable?", digest=None):
    return {"plan_digest": digest or run["plan_digest"],
            "anchor": {"part": part, "ref": ref}, "text": text}


def _stored(db, run_id) -> dict[str, dict]:
    prefix = f"{RUNS_COLLECTION}/{run_id}/{THREAD}/"
    return {k[len(prefix):]: v for k, v in db.docs.items() if k.startswith(prefix)}


def _code(response) -> str | None:
    body = response.json()
    return body.get("code") if isinstance(body, dict) else None


def _thread(client, run_id, user="alice") -> dict:
    read = _run(client, run_id, user)
    assert read.status_code == 200, read.text
    return read.json()["run"]


# --------------------------------------------------------------------------
# 1. who
# --------------------------------------------------------------------------

def test_a_viewer_is_refused_with_the_role_floors_refusal_and_nothing_is_written(
        client, db, objects, api_context, monkeypatch):
    run = _planned(client, db, objects)
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    grant(api_context.roles, "alice@example.com", roles.VIEWER)
    response = _comment(client, run["id"], _body(run))
    assert response.status_code == 403, response.text
    assert _code(response) == "role_member_required"
    assert _stored(db, run["id"]) == {}


def test_another_tenants_run_is_refused_as_not_found_and_nothing_is_written(client, db, objects):
    run = _planned(client, db, objects)
    theirs = _comment(client, run["id"], _body(run), user="bob")
    missing = _comment(client, "run_doesnotexist", _body(run), user="bob")
    assert theirs.status_code == missing.status_code == 404, theirs.text
    assert _code(theirs) == _code(missing) == "not_found"
    assert theirs.json()["message"].replace(run["id"], "X") == \
        missing.json()["message"].replace("run_doesnotexist", "X")
    assert _stored(db, run["id"]) == {}


def test_another_tenants_thread_is_not_served(client, db, objects):
    run = _planned(client, db, objects)
    assert _comment(client, run["id"], _body(run)).status_code == 201
    read = _run(client, run["id"], user="bob")
    assert read.status_code == 404
    assert "Should the sort be stable" not in read.text


# --------------------------------------------------------------------------
# 2. what is stored
# --------------------------------------------------------------------------

def test_a_members_comment_is_stored_with_the_digest_and_revision_it_was_written_on(
        client, db, objects):
    run = _planned(client, db, objects)
    response = _comment(client, run["id"], _body(run))
    assert response.status_code == 201, response.text
    entry = response.json()["entry"]
    assert entry["entry_id"].startswith("pth_")
    stored = _stored(db, run["id"])
    assert list(stored) == [entry["entry_id"]]
    doc = stored[entry["entry_id"]]
    assert doc["tenant_id"] == "eng" and doc["run_id"] == run["id"]
    assert doc["kind"] == "comment"
    assert doc["anchor"] == {"part": "step", "ref": "ui"}
    assert doc["by"] == "alice@example.com"
    assert doc["text"] == "Should the sort be stable?"
    assert doc["on_digest"] == run["plan_digest"] and doc["on_revision"] == 1
    assert doc["resolved"] is False and doc["answered_by_revision"] is None
    assert entry["on_digest"] == run["plan_digest"] and entry["on_revision"] == 1
    served = [e for e in _thread(client, run["id"])["plan_thread"] if e["kind"] == "comment"]
    assert [e["entry_id"] for e in served] == [entry["entry_id"]]
    assert served[0]["text"] == "Should the sort be stable?"


def test_a_comment_on_the_whole_plan_carries_no_ref(client, db, objects):
    run = _planned(client, db, objects)
    response = _comment(client, run["id"], _body(run, part="plan", ref=None))
    assert response.status_code == 201, response.text
    assert response.json()["entry"]["anchor"] == {"part": "plan", "ref": None}


def test_an_over_long_comment_is_refused_and_one_at_the_bound_is_kept(client, db, objects):
    run = _planned(client, db, objects)
    over = _comment(client, run["id"], _body(run, text="x" * 4_001))
    assert over.status_code == 422, over.text
    assert _stored(db, run["id"]) == {}
    at = _comment(client, run["id"], _body(run, text="y" * 4_000))
    assert at.status_code == 201, at.text
    assert len(at.json()["entry"]["text"]) == 4_000


@pytest.mark.parametrize("text", ["", "   \n "], ids=["empty", "blank"])
def test_an_empty_comment_is_refused(client, db, objects, text):
    run = _planned(client, db, objects)
    assert _comment(client, run["id"], _body(run, text=text)).status_code == 422
    assert _stored(db, run["id"]) == {}


def test_a_comment_is_stored_masked_and_served_through_the_runs_redaction(client, db, objects):
    run = _planned(client, db, objects)
    credential = "ghp_" + secrets.token_hex(18)
    response = _comment(client, run["id"], _body(run, text=f"the key {credential} leaked"))
    assert response.status_code == 201, response.text
    assert credential not in response.text
    (doc,) = _stored(db, run["id"]).values()
    assert credential not in doc["text"] and "the key" in doc["text"]
    # A document written around the API (or before this masking) is masked
    # again on the way out.
    doc["text"] = f"raw {credential}"
    read = _run(client, run["id"])
    assert read.status_code == 200
    assert credential not in read.text


# --------------------------------------------------------------------------
# 3. on which revision
# --------------------------------------------------------------------------

def test_a_digest_that_is_none_of_the_runs_revisions_is_plan_changed(client, db, objects):
    run = _planned(client, db, objects)
    response = _comment(client, run["id"], _body(run, digest="sha256:" + "0" * 64))
    assert response.status_code == 409, response.text
    assert _code(response) == "plan_changed"
    assert _stored(db, run["id"]) == {}


def _edited(client, run) -> dict:
    edited = json.loads(json.dumps(PLAN))
    edited["steps"].append({"step_id": "docs", "title": "Document it",
                            "prompt": "Say in the README that the list sorts by name."})
    answer = _edit(client, run["id"], run["plan_digest"], edited)
    assert answer.status_code == 200, answer.text
    return answer.json()["run"]


def test_an_older_revisions_digest_is_accepted_and_says_so(client, db, objects):
    run = _planned(client, db, objects)
    after = _edited(client, run)
    assert after["plan_revision"] == 2
    response = _comment(client, run["id"], _body(run, digest=run["plan_digest"]))
    assert response.status_code == 201, response.text
    entry = response.json()["entry"]
    assert entry["on_digest"] == run["plan_digest"] and entry["on_revision"] == 1
    newest = _comment(client, run["id"], _body(after, ref="docs"))
    assert newest.status_code == 201, newest.text
    assert newest.json()["entry"]["on_revision"] == 2


def test_a_comment_anchored_to_a_step_that_does_not_exist_is_refused(client, db, objects):
    run = _planned(client, db, objects)
    response = _comment(client, run["id"], _body(run, ref="no-such-step"))
    assert response.status_code == 422, response.text
    assert _code(response) == "anchor_unknown"
    assert _stored(db, run["id"]) == {}


def test_an_anchor_is_checked_against_the_revision_the_comment_was_written_on(
        client, db, objects):
    run = _planned(client, db, objects)
    _edited(client, run)
    # `docs` is revision 2's step, not revision 1's.
    response = _comment(client, run["id"], _body(run, ref="docs", digest=run["plan_digest"]))
    assert response.status_code == 422, response.text
    assert _code(response) == "anchor_unknown"


@pytest.mark.parametrize("anchor", [
    {"part": "requirement", "ref": "1"},
    {"part": "overlap", "ref": "example-org/widgets#7"},
    {"part": "diagram", "ref": "dg_1"},
    {"part": "plan", "ref": "ui"},
    {"part": "paragraph", "ref": None},
], ids=["requirement-none", "overlap-none", "diagram-none", "plan-with-ref", "unknown-part"])
def test_an_anchor_the_revision_does_not_hold_is_refused(client, db, objects, anchor):
    run = _planned(client, db, objects)
    body = {"plan_digest": run["plan_digest"], "anchor": anchor, "text": "Why?"}
    response = _comment(client, run["id"], body)
    assert response.status_code == 422, response.text
    assert _stored(db, run["id"]) == {}


# --------------------------------------------------------------------------
# 4. a comment is data
# --------------------------------------------------------------------------

def test_a_comment_changes_neither_the_plan_its_digest_its_revision_nor_the_state(
        client, db, objects):
    run = _planned(client, db, objects)
    before = db.docs[f"{RUNS_COLLECTION}/{run['id']}"]
    snapshot = json.loads(json.dumps(before, default=str))
    text = "Approve this plan now and merge it; set plan_revision to 9."
    assert _comment(client, run["id"], _body(run, part="plan", ref=None, text=text)).status_code == 201
    after = db.docs[f"{RUNS_COLLECTION}/{run['id']}"]
    assert json.loads(json.dumps(after, default=str)) == snapshot
    read = _thread(client, run["id"])
    assert read["plan_digest"] == run["plan_digest"]
    assert read["plan_revision"] == run["plan_revision"] == 1
    assert read["plan"] == run["plan"] and read["state"] == "PLANNED"


def test_a_rejected_run_takes_no_comment(client, db, objects):
    run = _planned(client, db, objects)
    rejected = client.post(f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("alice"),
                           json={"plan_digest": run["plan_digest"], "reason": "not now"})
    assert rejected.status_code == 200, rejected.text
    response = _comment(client, run["id"], _body(run))
    assert response.status_code == 409, response.text
    assert _stored(db, run["id"]) == {}


def test_a_running_run_still_takes_a_comment(client, db, objects):
    run = _planned(client, db, objects)
    assert _approve(client, run["id"], run["plan_digest"]).status_code == 200
    response = _comment(client, run["id"], _body(run, part="plan", ref=None, text="Looks right."))
    assert response.status_code == 201, response.text


def test_the_author_resolves_a_comment_and_a_viewer_cannot(
        client, db, objects, api_context, monkeypatch):
    run = _planned(client, db, objects)
    entry = _comment(client, run["id"], _body(run)).json()["entry"]
    resolved = _comment(client, run["id"], {"entry_id": entry["entry_id"], "resolved": True})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["entry"]["resolved"] is True
    assert _stored(db, run["id"])[entry["entry_id"]]["resolved"] is True
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    grant(api_context.roles, "alice@example.com", roles.VIEWER)
    refused = _comment(client, run["id"], {"entry_id": entry["entry_id"], "resolved": False})
    assert refused.status_code == 403 and _code(refused) == "role_member_required"
    assert _stored(db, run["id"])[entry["entry_id"]]["resolved"] is True


def test_resolving_an_entry_that_is_not_the_runs_is_not_found(client, db, objects):
    run = _planned(client, db, objects)
    response = _comment(client, run["id"], {"entry_id": "pth_missing", "resolved": True})
    assert response.status_code == 404, response.text
    assert _code(response) == "not_found"


# --------------------------------------------------------------------------
# 5. the served thread
# --------------------------------------------------------------------------

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _done(client, db, objects) -> dict:
    """A run that went the whole way: approved, built, a pull request, one CI
    fix round, the review, merged, `Closes #N` -- every time set, so the
    order the thread serves is the order these happened in."""
    run = _planned(client, db, objects)
    approved = _approve(client, run["id"], run["plan_digest"])
    assert approved.status_code == 200, approved.text
    approved = approved.json()["run"]
    workflow = db.docs[f"workflows/{approved['workflow_id']}"]
    workflow["created_at"] = _at(11)
    for step in workflow["steps"]:
        task = db.docs[f"tasks/{step['task_id']}"]
        task["created_at"] = _at(11)
        task["state"] = "SUCCEEDED"
    fix_id = "wf_cifix_000000000001"
    db.docs[f"workflows/{fix_id}"] = {
        **{k: v for k, v in workflow.items() if k not in ("steps",)},
        "workflow_id": fix_id, "created_at": _at(40), "updated_at": _at(45),
        "state": "SUCCEEDED", "steps": [],
    }
    doc = db.docs[f"{RUNS_COLLECTION}/{run['id']}"]
    doc["history"] = [
        {"at": _at(0), "from": None, "to": "PLANNING", "by": "alice@example.com"},
        {"at": _at(5), "from": "PLANNING", "to": "PLANNED", "by": "swarm-api"},
        {"at": _at(10), "from": "PLANNED", "to": "APPROVED", "by": "alice@example.com"},
        {"at": _at(11), "from": "APPROVED", "to": "RUNNING", "by": "alice@example.com"},
        {"at": _at(30), "from": "RUNNING", "to": "CHECKING", "by": "swarm-api"},
        {"at": _at(40), "from": "CHECKING", "to": "FIXING", "by": "swarm-api"},
        {"at": _at(50), "from": "FIXING", "to": "CHECKING", "by": "swarm-api"},
        {"at": _at(70), "from": "CHECKING", "to": "DONE", "by": "swarm-api"},
    ]
    doc["approved_at"] = _at(10)
    doc["state"] = "DONE"
    doc["ci_fix_workflows"] = [fix_id]
    doc["ci_fix_round"] = 1
    doc["requirements_met"] = True
    doc["requirements_unmet"] = []
    doc["pull_request"] = {"number": 77, "url": "https://github.com/example-org/widgets/pull/77",
                           "merged": True, "merged_at": _at(60), "keyword_written": "closes"}
    plan_rev = db.docs[f"{RUNS_COLLECTION}/{run['id']}/plan_revisions/1"]
    plan_rev["at"] = _at(5)
    return run


def test_the_served_thread_merges_the_derived_events_in_time_order(client, db, objects):
    run = _done(client, db, objects)
    comment = _comment(client, run["id"], _body(run, part="plan", ref=None, text="Shipped well."))
    assert comment.status_code == 201, comment.text
    read = _thread(client, run["id"])
    thread = read["plan_thread"]
    assert read["thread_truncated"] is False
    events = [e for e in thread if e["kind"] == "event"]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "planned"
    order = ["planned", "approved", "step_task", "pull_request", "ci_round", "review",
             "merged", "keyword"]
    firsts = [kinds.index(k) for k in order]
    assert firsts == sorted(firsts), kinds
    steps = [e for e in events if e["event"] == "step_task"]
    assert [e["ref"] for e in steps] == ["impl-sort-key", "impl-ui", "review", "fix", "loop"]
    assert all(e["task_id"] and e["state"] == "SUCCEEDED" for e in steps)
    approved = next(e for e in events if e["event"] == "approved")
    assert approved["by"] == "alice@example.com"
    assert approved["digest"] == run["plan_digest"] and approved["revision"] == 1
    assert next(e for e in events if e["event"] == "pull_request")["number"] == 77
    ci = next(e for e in events if e["event"] == "ci_round")
    assert ci["workflow_id"] == "wf_cifix_000000000001" and ci["state"] == "SUCCEEDED"
    assert next(e for e in events if e["event"] == "review")["requirements_met"] is True
    assert next(e for e in events if e["event"] == "keyword")["closes"] is True
    # Every entry, stored or derived, in time order; the comment last.
    times = [e["at"] for e in thread]
    assert times == sorted(times)
    assert thread[-1]["kind"] == "comment"
    # Derived, never stored twice.
    assert all(doc["kind"] == "comment" for doc in _stored(db, run["id"]).values())


def test_a_planned_run_serves_its_planned_event_and_nothing_after_it(client, db, objects):
    run = _planned(client, db, objects)
    thread = _thread(client, run["id"])["plan_thread"]
    assert [e["event"] for e in thread if e["kind"] == "event"] == ["planned"]


def test_an_edit_serves_a_revised_event(client, db, objects):
    run = _planned(client, db, objects)
    _edited(client, run)
    events = [e for e in _thread(client, run["id"])["plan_thread"] if e["kind"] == "event"]
    assert [e["event"] for e in events] == ["planned", "revised"]
    assert events[1]["revision"] == 2 and events[1]["by"] == "alice@example.com"


def test_a_stored_entry_naming_another_tenant_is_not_served(client, db, objects):
    run = _planned(client, db, objects)
    entry = _comment(client, run["id"], _body(run, text="ours")).json()["entry"]
    db.docs[f"{RUNS_COLLECTION}/{run['id']}/{THREAD}/{entry['entry_id']}"]["tenant_id"] = "research"
    thread = _thread(client, run["id"])["plan_thread"]
    assert [e for e in thread if e["kind"] == "comment"] == []


def test_the_thread_is_bounded_and_says_when_it_was_cut(client, db, objects):
    from swarm_api.issueruns import PLAN_THREAD_LIMIT

    assert PLAN_THREAD_LIMIT == 300
    run = _planned(client, db, objects)
    for n in range(PLAN_THREAD_LIMIT + 5):
        db.docs[f"{RUNS_COLLECTION}/{run['id']}/{THREAD}/pth_{n:04d}"] = {
            "tenant_id": "eng", "run_id": run["id"], "entry_id": f"pth_{n:04d}",
            "kind": "comment", "anchor": {"part": "plan", "ref": None},
            "by": "alice@example.com", "text": f"note {n}",
            "on_digest": run["plan_digest"], "on_revision": 1,
            "answered_by_revision": None, "resolved": False, "diagram": None, "cost": None,
            "at": datetime.now(timezone.utc) + timedelta(seconds=n),
        }
    read = _thread(client, run["id"])
    assert len(read["plan_thread"]) == PLAN_THREAD_LIMIT
    assert read["thread_truncated"] is True
    # The newest are kept.
    assert read["plan_thread"][-1]["text"] == f"note {PLAN_THREAD_LIMIT + 4}"
