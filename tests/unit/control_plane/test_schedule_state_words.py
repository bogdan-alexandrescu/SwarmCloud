"""What Automate's landing and one-schedule sheet read (proposal A, picked
2026-10-26; AUTO-API-SCHEDULES, part of #2417).

WHAT IS HELD HERE
-----------------
* `state_word`: each of the six words from the stored fields, by one rule
  (`schedules.state_word`), served on every listed schedule and on the one
  schedule, from the same waiting count, so the list and the sheet agree.
* `summary` on `GET /v1/schedules`: the listed schedules by word, every word
  present, and the waiting approvals as the Approvals tab's own inbox counts
  them, with the oldest one's expiry. An inbox that cannot be read is null
  with why, never 0.
* `work[].pull_request {number, state}` on a firing's issue runs that opened
  one, read from the run record; absent when the run opened none or is not
  the caller's.
* `largest_run_usd` on a firing: the costliest run, read as `/spans` reads an
  item's cost; null with `largest_run_unread` when a run did not report.

No cloud and no emulator: the in-memory Firestore and the real app.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swarm_api import approvals, schedulefire, schedules
from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.routes import schedules as schedule_routes
from swarm_api.validation import IssueRef

from .conftest import auth_header, seed_task
from .test_schedule_routes import NOW, built_types, client, create, ctx, executor  # noqa: F401
from .test_schedule_spans import _attempt, _firing

__all__ = ["built_types", "client", "ctx", "executor"]

ISSUECI = Path(schedule_routes.__file__).resolve().parents[1] / "issueci.py"


# ---------------------------------------------------------------------------
# The rule, alone
# ---------------------------------------------------------------------------


def _doc(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"state": "enabled", "pause": None, "consecutive_failures": 0,
                            "last_firing": {"firing_id": "f", "outcome": "succeeded"}}
    base.update(fields)
    return base


TRIP = {"by": "tick", "at": NOW, "reason": "Paused: a run cost more than its per-run budget",
        "code": "RUN_OVER_BUDGET"}


@pytest.mark.parametrize(("doc", "waiting", "word"), [
    (_doc(), 0, "active"),
    (_doc(state="paused", pause={"by": "alice", "code": None}), 0, "paused"),
    (_doc(state="auto_paused", pause=TRIP), 0, "paused_by_tripwire"),
    (_doc(), 2, "waiting_on_approval"),
    (_doc(consecutive_failures=2), 0, "failing"),
    (_doc(last_firing=None), 0, "never_fired"),
], ids=["active", "paused", "paused_by_tripwire", "waiting_on_approval", "failing", "never_fired"])
def test_each_of_the_six_words_comes_from_the_stored_fields(doc, waiting, word) -> None:
    assert schedules.state_word(doc, waiting=waiting) == word


def test_the_rule_names_exactly_the_six_words() -> None:
    assert schedules.STATE_WORDS == (
        "active", "paused", "paused_by_tripwire", "waiting_on_approval", "failing", "never_fired")


@pytest.mark.parametrize("code", ["CONSECUTIVE_FAILURES", "OWNER_NOT_MEMBER", "OWNER_ROLE_NARROWED"])
def test_another_auto_pause_is_paused_not_the_tripwire(code) -> None:
    assert schedules.state_word(_doc(state="auto_paused", pause={"code": code}), waiting=0) == "paused"


def test_a_schedule_that_cannot_fire_reads_paused_before_anything_it_waits_on() -> None:
    # Paused with an approval waiting, failures counted and never fired: an
    # approved firing does not run until it resumes, so paused is the word.
    doc = _doc(state="paused", pause={"code": None}, consecutive_failures=3, last_firing=None)
    assert schedules.state_word(doc, waiting=1) == "paused"
    # An admin's `disabled` sorts with the paused ones; `state` says whose.
    assert schedules.state_word(_doc(state="disabled", pause={"code": None}), waiting=0) == "paused"
    # The tripwire's pause stays told apart even with an approval waiting.
    assert schedules.state_word(_doc(state="auto_paused", pause=TRIP), waiting=1) == "paused_by_tripwire"


def test_waiting_reads_before_failing_and_failing_before_never_fired() -> None:
    assert schedules.state_word(_doc(consecutive_failures=1), waiting=1) == "waiting_on_approval"
    assert schedules.state_word(_doc(consecutive_failures=1, last_firing=None), waiting=0) == "failing"


def test_the_tripwire_code_is_the_ticks() -> None:
    assert schedules.TRIPWIRE_CODE == schedulefire.RUN_OVER_BUDGET


def test_the_summary_counts_every_word_and_an_unread_inbox_is_null_not_zero() -> None:
    later = NOW + timedelta(hours=72)
    out = schedules.summarize(["active", "failing", "active"], [{"expires_at": later}, {"expires_at": NOW}])
    assert out == {"by_state": {"active": 2, "paused": 0, "paused_by_tripwire": 0, "waiting_on_approval": 0,
                                "failing": 1, "never_fired": 0},
                   "pending_approvals": 2, "oldest_expires_at": later, "unread": None}
    assert schedules.summarize([], [])["oldest_expires_at"] is None
    unread = schedules.summarize(["paused"], None)
    assert unread["pending_approvals"] is None and unread["oldest_expires_at"] is None
    assert unread["unread"] == "the approvals inbox could not be read"
    assert unread["by_state"]["paused"] == 1


# ---------------------------------------------------------------------------
# The list, the summary and the sheet
# ---------------------------------------------------------------------------


def _set(db, sid: str, **fields: Any) -> None:
    db.docs[f"schedules/{sid}"].update(fields)


def _approval(db, item_id: str, *, sid: str | None, hours_ago: int, kind: str = "run",
              subject: dict[str, Any] | None = None, digest: Any = "d", tenant: str = "eng") -> None:
    requested = NOW - timedelta(hours=hours_ago)
    db.docs[f"{approvals.COLLECTION}/{item_id}"] = {
        "approval_id": item_id, "tenant_id": tenant, "kind": kind,
        "subject": subject if subject is not None else {"schedule_id": sid, "firing_id": f"{sid}:x"},
        "digest": digest, "summary": "s", "approvers": "members", "state": "pending",
        "requested_at": requested, "expires_at": requested + timedelta(hours=72),
        "decided_by": None, "decided_at": None, "reason": None,
    }


def _six(client, db) -> dict[str, str]:
    """One schedule in each of the six words, by name."""
    sids = {word: create(client, name=f"s {word}")["schedule_id"] for word in schedules.STATE_WORDS}
    last = {"firing_id": "f", "slot": NOW, "outcome": "succeeded", "state": "done", "work_ref": None,
            "ended_at": NOW}
    for word, sid in sids.items():
        _set(db, sid, last_firing=dict(last))
    _set(db, sids["paused"], state="paused", pause={"by": "alice", "at": NOW, "reason": "r", "code": None})
    _set(db, sids["paused_by_tripwire"], state="auto_paused", pause=TRIP)
    _set(db, sids["failing"], consecutive_failures=2)
    _set(db, sids["never_fired"], last_firing=None)
    _approval(db, "apr_one", sid=sids["waiting_on_approval"], hours_ago=5)
    _approval(db, "apr_two", sid=sids["waiting_on_approval"], hours_ago=2)
    return sids


def test_the_list_serves_each_word_and_the_summary_counts_them(client, db) -> None:
    sids = _six(client, db)
    # A plan of a run made by hand waits too, naming no schedule, and is the oldest.
    _approval(db, "apr_hand", sid=None, hours_ago=9, subject={"run_id": "run_x"})
    response = client.get("/v1/schedules", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    words = {row["schedule_id"]: row["state_word"] for row in body["schedules"]}
    assert words == {sid: word for word, sid in sids.items()}
    waiting = next(r for r in body["schedules"] if r["schedule_id"] == sids["waiting_on_approval"])
    assert waiting["pending_approvals"] == 2
    assert body["summary"] == {
        "by_state": dict.fromkeys(schedules.STATE_WORDS, 1),
        "pending_approvals": 3,
        "oldest_expires_at": (NOW - timedelta(hours=9) + timedelta(hours=72)).isoformat(),
        "unread": None,
    }


def test_the_summary_is_the_approvals_tabs_count(client, db) -> None:
    _six(client, db)
    _approval(db, "apr_theirs", sid=None, hours_ago=1, tenant="research", subject={"run_id": "run_y"})
    tab = client.get("/v1/approvals", headers=auth_header("alice")).json()["approvals"]
    summary = client.get("/v1/schedules", headers=auth_header("alice")).json()["summary"]
    assert summary["pending_approvals"] == len(tab) == 2
    assert summary["oldest_expires_at"] == tab[0]["expires_at"]


def test_the_sheet_reads_the_word_the_list_reads(client, db) -> None:
    sids = _six(client, db)
    listed = {r["schedule_id"]: r for r in client.get("/v1/schedules", headers=auth_header("alice")).json()["schedules"]}
    for word, sid in sids.items():
        one = client.get(f"/v1/schedules/{sid}", headers=auth_header("alice")).json()["schedule"]
        assert one["state_word"] == listed[sid]["state_word"] == word
        assert one["pending_approvals"] == listed[sid]["pending_approvals"]


def test_a_merge_waiting_on_a_scheduled_run_is_the_schedules_wait(client, db) -> None:
    sid = create(client)["schedule_id"]
    _set(db, sid, last_firing={"firing_id": "f", "outcome": "succeeded"})
    _run(db, "run_m", 41, state=RunState.CHECKING, pr={"number": 41, "head_sha": "a" * 40, "merged": False})
    _approval(db, "apr_merge", sid=None, hours_ago=1, kind="merge",
              subject={"run_id": "run_m", "pr": 41, "schedule_id": sid},
              digest={"head_sha": "a" * 40, "verdict": None})
    row = client.get("/v1/schedules", headers=auth_header("alice")).json()["schedules"][0]
    assert row["pending_approvals"] == 1
    assert row["state_word"] == "waiting_on_approval"


def test_an_inbox_that_cannot_be_read_still_lists_and_says_why(client, db, monkeypatch) -> None:
    sids = _six(client, db)

    def broken(ctx, tenant_id, *, kind=None):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(approvals, "inbox", broken)
    body = client.get("/v1/schedules", headers=auth_header("alice")).json()
    assert len(body["schedules"]) == 6
    assert body["summary"]["pending_approvals"] is None
    assert body["summary"]["unread"] == "the approvals inbox could not be read"
    words = {r["schedule_id"]: r["state_word"] for r in body["schedules"]}
    # No firing waits to run in this seed, so nothing reads waiting.
    assert words[sids["waiting_on_approval"]] == "active"


def test_another_tenants_approvals_name_none_of_yours(client, db) -> None:
    sid = create(client)["schedule_id"]
    _set(db, sid, last_firing={"firing_id": "f"})
    _approval(db, "apr_theirs", sid=sid, hours_ago=1, tenant="research")
    body = client.get("/v1/schedules", headers=auth_header("alice")).json()
    assert body["schedules"][0]["state_word"] == "active"
    assert body["summary"]["pending_approvals"] == 0
    assert body["summary"]["oldest_expires_at"] is None


# ---------------------------------------------------------------------------
# A firing's pull requests
# ---------------------------------------------------------------------------


def _run(db, run_id: str, number: int, *, state: RunState = RunState.DONE, planner: str = "task_plan",
         pr: dict[str, Any] | None = None, error: str | None = None, tenant: str = "eng") -> None:
    run = IssueRun(
        id=run_id, tenant_id=tenant, created_by="alice", created_at=NOW, updated_at=NOW, state=state,
        issue=IssueRef(owner="example-org", repo="example-api", number=number), plan_approval="auto",
        auto_merge=False, fix_rounds=2, planner_task_id=planner, pull_request=pr, error=error,
    )
    IssueRuns(db, now=lambda: NOW).create(run)


def test_each_issue_run_that_opened_a_pull_request_serves_it_with_its_state(client, db) -> None:
    sid = create(client)["schedule_id"]
    head = "b" * 40
    _run(db, "run_merged", 1, pr={"number": 101, "head_sha": head, "merged": True})
    _run(db, "run_open", 2, state=RunState.CHECKING, pr={"number": 102, "head_sha": head, "merged": False})
    _run(db, "run_waits", 3, state=RunState.CHECKING, pr={"number": 103, "head_sha": head, "merged": False})
    _approval(db, "apr_m", sid=None, hours_ago=1, kind="merge", subject={"run_id": "run_waits", "pr": 103},
              digest={"head_sha": head, "verdict": None})
    # A merge asked at an older head waits for nothing now.
    _run(db, "run_moved", 4, state=RunState.CHECKING, pr={"number": 104, "head_sha": head, "merged": False})
    _approval(db, "apr_old", sid=None, hours_ago=1, kind="merge", subject={"run_id": "run_moved", "pr": 104},
              digest={"head_sha": "c" * 40, "verdict": None})
    _run(db, "run_closed", 5, state=RunState.FAILED, pr={"number": 105, "head_sha": head, "merged": False},
         error="pull request #105 was closed without being merged")
    # Failed with its fix rounds spent: the pull request is still open.
    _run(db, "run_spent", 6, state=RunState.FAILED, pr={"number": 106, "head_sha": head, "merged": False},
         error="CI stayed red after 2 fix rounds")
    _run(db, "run_none", 7, state=RunState.FAILED)
    _run(db, "run_theirs", 8, pr={"number": 108, "head_sha": head, "merged": True}, tenant="research")
    ids = ["run_merged", "run_open", "run_waits", "run_moved", "run_closed", "run_spent", "run_none",
           "run_theirs", "run_missing"]
    fid = _firing(db, sid, 1, [{"kind": "issue_run", "id": i} for i in ids] + [{"kind": "task", "id": "t"}])

    def served(path: str) -> dict[str, Any]:
        response = client.get(path, headers=auth_header("alice"))
        assert response.status_code == 200, response.text
        return response.json()

    for firing in (served(f"/v1/schedules/{sid}")["firings"][0],
                   served(f"/v1/schedules/{sid}/firings")["firings"][0],
                   served(f"/v1/schedules/{sid}/firings/{fid}")["firing"]):
        prs = {item["id"]: item.get("pull_request") for item in firing["work"]}
        assert prs == {
            "run_merged": {"number": 101, "state": "merged"},
            "run_open": {"number": 102, "state": "open"},
            "run_waits": {"number": 103, "state": "waiting_on_approval"},
            "run_moved": {"number": 104, "state": "open"},
            "run_closed": {"number": 105, "state": "closed"},
            "run_spent": {"number": 106, "state": "open"},
            "run_none": None, "run_theirs": None, "run_missing": None, "t": None,
        }
        # Absent, not null: the work opened none, or it is not the caller's.
        assert all("pull_request" not in item for item in firing["work"]
                   if item["id"] in ("run_none", "run_theirs", "run_missing", "t"))


def test_the_closed_words_are_the_ones_the_ci_loop_stores() -> None:
    assert schedule_routes._PR_CLOSED_WORDS in ISSUECI.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A firing's largest run
# ---------------------------------------------------------------------------


def _task(db, task_id: str, *costs: float | None, state: str = "SUCCEEDED") -> None:
    seed_task(db, task_id=task_id, tenant_id="eng", state=state, created_at=NOW)
    for g, cost in enumerate(costs, start=1):
        _attempt(db, task_id, g, NOW + timedelta(minutes=10 * g), 5, cost=cost)


def _largest(client, sid: str) -> dict[str, tuple[Any, Any]]:
    firings = client.get(f"/v1/schedules/{sid}", headers=auth_header("alice")).json()["firings"]
    return {f["firing_id"]: (f["largest_run_usd"], f["largest_run_unread"]) for f in firings}


def test_the_largest_run_is_the_costliest_and_agrees_with_spans(client, db) -> None:
    sid = create(client)["schedule_id"]
    _task(db, "task_plan", 1.25, 0.5)
    _run(db, "run_a", 1, planner="task_plan")
    _task(db, "task_b", 2.0)
    fid = _firing(db, sid, 1, [{"kind": "issue_run", "id": "run_a"}, {"kind": "task", "id": "task_b"},
                               {"kind": "api_action", "id": "label:ready"}])
    assert _largest(client, sid)[fid] == (2.0, None)
    spans = client.get(f"/v1/schedules/{sid}/spans", params=[("firing_id", fid)],
                       headers=auth_header("alice")).json()["firings"][0]["items"]
    assert max(i["cost"]["reported_usd"] for i in spans if i["cost"]["reported_usd"] is not None) == 2.0
    one = client.get(f"/v1/schedules/{sid}/firings/{fid}", headers=auth_header("alice")).json()["firing"]
    assert one["largest_run_usd"] == 2.0


def test_a_run_that_did_not_report_makes_the_largest_unknown_never_zero(client, db) -> None:
    sid = create(client)["schedule_id"]
    _task(db, "task_half", 2.0, None)
    _task(db, "task_small", 0.4)
    partial = _firing(db, sid, 1, [{"kind": "task", "id": "task_half"}, {"kind": "task", "id": "task_small"}])
    _task(db, "task_none", None)
    unreported = _firing(db, sid, 2, [{"kind": "task", "id": "task_none"}])
    _task(db, "task_live", state="RUNNING")
    running = _firing(db, sid, 3, [{"kind": "task", "id": "task_live"}])
    actions = _firing(db, sid, 4, [{"kind": "api_action", "id": "label:ready"}])
    skipped = _firing(db, sid, 5, [])
    got = _largest(client, sid)
    assert got[partial] == (None, schedule_routes._LARGEST_PARTIAL)
    assert got[unreported] == (None, schedule_routes._COST_NONE_RECORDED)
    assert got[running] == (None, schedule_routes._COST_NO_ATTEMPT)
    assert got[actions] == (None, schedule_routes._LARGEST_NO_RUN)
    assert got[skipped] == (None, schedule_routes._LARGEST_NO_RUN)


def test_a_firing_past_the_costed_ones_reads_its_own_document(client, db) -> None:
    sid = create(client)["schedule_id"]
    newest = schedule_routes.SPAN_FIRINGS
    fids = []
    for n in range(1, newest + 3):
        _task(db, f"task_{n}", float(n))
        fids.append(_firing(db, sid, n, [{"kind": "task", "id": f"task_{n}"}]))
    # Past the window: the tick's `cost` of a one-task firing is that run's.
    db.docs[f"schedule_firings/{fids[-2]}"].update(
        {"cost": {"reported_usd": 11.0, "unreported_attempts": 0}})
    db.docs[f"schedule_firings/{fids[-1]}"].update(
        {"cost": {"reported_usd": 12.0, "unreported_attempts": 1}})
    got = _largest(client, sid)
    assert [got[f] for f in fids[:newest]] == [(float(n), None) for n in range(1, newest + 1)]
    assert got[fids[-2]] == (11.0, None)
    assert got[fids[-1]] == (None, schedule_routes._LARGEST_OLDER)
    # Opened alone, it is costed from its tasks.
    one = client.get(f"/v1/schedules/{sid}/firings/{fids[-1]}", headers=auth_header("alice")).json()["firing"]
    assert one["largest_run_usd"] == float(newest + 2)


def test_a_firing_over_the_task_cap_says_so(client, db, monkeypatch) -> None:
    monkeypatch.setattr(schedule_routes, "SPAN_TASKS", 1)
    sid = create(client)["schedule_id"]
    _task(db, "task_x", 1.0)
    _task(db, "task_y", 2.0)
    first = _firing(db, sid, 1, [{"kind": "task", "id": "task_x"}])
    second = _firing(db, sid, 2, [{"kind": "task", "id": "task_y"}])
    got = _largest(client, sid)
    assert got[first] == (1.0, None)
    assert got[second] == (None, schedule_routes._UNREAD_CAP)
