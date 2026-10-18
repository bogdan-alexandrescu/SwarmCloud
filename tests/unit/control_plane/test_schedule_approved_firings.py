"""An approved `run: approve` firing runs (docs/schedules.md §1.4, the S5 remainder).

S5 left one gap: approving a firing records `run_approval` on it in the
decision's transaction (`approvals.firing_move`), and the tick then had no
path from `awaiting_approval` to work, so an approved firing waited for ever.

WHAT IS HELD HERE
-----------------
* An approved firing creates its work, EXACTLY ONCE: a later tick creates
  nothing more, and a creation that failed after submitting is adopted.
* It goes ahead only when `run_approval.digest` is the firing's
  `params_digest` -- and the digest the firing would run with now. A mismatch
  stays held and is audited, once.
* An unapproved firing stays held.
* Another tenant's approval cannot release a firing.
* More than a page of older unapproved firings, platform-wide, does not
  starve an approved one: the tick filters on `run_approval` before paging.

Each negative case runs beside an approved CONTROL firing in the same tick,
which must be created: the measurement could have come out the other way,
so "nothing happened" is not a tick that never looked.

No cloud and no emulator: the in-memory Firestore and the real approvals path.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from swarm_api import approvals, schedaudit, schedulefire

from .test_schedule_tick import (  # noqa: F401 - `ctx` and `tick_env` are fixtures
    ALICE,
    NOW,
    FakeExecutor,
    ctx,
    firings,
    seed_schedule,
    tasks,
    tick_env,
    ticker,
)

SUBJECT = "sch_000000000001"
CONTROL = "sch_000000000002"


def _held(db, ctx, *schedule_ids: str) -> dict[str, dict[str, Any]]:
    """Each schedule's slot, held at its run gate, with its `run` record made."""
    for sid in schedule_ids:
        seed_schedule(db, schedule_id=sid, gate={"run": "approve"})
    executor = FakeExecutor()
    report = ticker(ctx, executor).tick()
    assert report.held == len(schedule_ids) and executor.calls == 0 and tasks(db) == []
    approvals.sync_run_approvals(db, NOW, "eng")
    held = {f["schedule_id"]: f for f in firings(db).values()}
    assert {f["state"] for f in held.values()} == {"awaiting_approval"}
    assert all(f["approval_id"] for f in held.values())
    return held


def _approve(db, firing: dict[str, Any]) -> None:
    """The inbox's approve, exactly as the route makes it."""
    approvals.decide(
        db, NOW, "eng", firing["approval_id"], approve=True, by=ALICE,
        digest=firing["params_digest"],
        move=approvals.firing_move(db, NOW, approve=True, by=ALICE),
    )
    assert db.docs[f"schedule_firings/{firing['firing_id']}"]["run_approval"]["by"] == ALICE


def _firing(db, firing: dict[str, Any]) -> dict[str, Any]:
    return db.docs[f"schedule_firings/{firing['firing_id']}"]


def _created(db, firing: dict[str, Any]) -> None:
    doc = _firing(db, firing)
    assert doc["state"] == "created", doc["history"]
    assert [w["kind"] for w in doc["work"]] == ["task"]


def _still_held(db, firing: dict[str, Any]) -> None:
    doc = _firing(db, firing)
    assert doc["state"] == "awaiting_approval", doc["history"]
    assert doc["work"] == [] and not doc.get("finisher")


def _audit(db, action: str = "run_approval_refused") -> list[dict[str, Any]]:
    return [v for k, v in db.docs.items()
            if k.startswith(f"{schedaudit.COLLECTION}/") and v.get("action") == action]


def _marked(db, firing: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in tasks(db)
            if ((t.get("metadata") or {}).get("schedule") or {}).get("firing_id") == firing["firing_id"]]


LATER = NOW + timedelta(minutes=3)


def test_an_approved_firing_creates_its_work_exactly_once(db, ctx) -> None:
    held = _held(db, ctx, SUBJECT)
    _approve(db, held[SUBJECT])
    executor = FakeExecutor()
    report = ticker(ctx, executor, at=LATER).tick()
    _created(db, held[SUBJECT])
    assert executor.calls == 1 and len(tasks(db)) == 1, report.to_api()
    assert report.fired == 1
    # A retried tick, and one after it: nothing more is created.
    ticker(ctx, executor, at=LATER).tick()
    ticker(ctx, executor, at=LATER + timedelta(minutes=1)).tick()
    assert executor.calls == 1 and len(tasks(db)) == 1
    doc = _firing(db, held[SUBJECT])
    assert [w["id"] for w in doc["work"]] == [tasks(db)[0]["id"]]
    assert [h["state"] for h in doc["history"]] == ["claimed", "awaiting_approval", "created"]


def test_an_approved_creation_that_failed_after_submitting_is_adopted(db, ctx) -> None:
    held = _held(db, ctx, SUBJECT)
    _approve(db, held[SUBJECT])
    executor = FakeExecutor(fail_after_submit=True)
    report = ticker(ctx, executor, at=LATER).tick()
    assert report.errors == 1
    _still_held(db, held[SUBJECT])
    assert len(_marked(db, held[SUBJECT])) == 1
    ticker(ctx, executor, at=LATER + timedelta(minutes=1)).tick()
    _created(db, held[SUBJECT])
    assert executor.calls == 1, "the approved work was created twice"
    assert len(tasks(db)) == 1


def test_a_digest_mismatch_stays_held_and_is_audited_once(db, ctx) -> None:
    held = _held(db, ctx, SUBJECT, CONTROL)
    for firing in held.values():
        _approve(db, firing)
    db.docs[f"schedule_firings/{held[SUBJECT]['firing_id']}"]["run_approval"]["digest"] = "sha256:" + "0" * 64
    executor = FakeExecutor()
    ticker(ctx, executor, at=LATER).tick()
    ticker(ctx, executor, at=LATER + timedelta(minutes=1)).tick()
    _created(db, held[CONTROL])
    _still_held(db, held[SUBJECT])
    assert _marked(db, held[SUBJECT]) == [] and executor.calls == 1
    (entry,) = _audit(db)
    assert entry["schedule_id"] == SUBJECT and entry["tenant_id"] == "eng"
    assert entry["detail"]["firing_id"] == held[SUBJECT]["firing_id"]
    assert entry["detail"]["code"] == "approval_changed"


def test_a_schedule_changed_since_the_approval_stays_held(db, ctx) -> None:
    """The approver saw one scope; the schedule now names another. The firing's
    stored digest still matches, the digest it would run with does not."""
    held = _held(db, ctx, SUBJECT, CONTROL)
    for firing in held.values():
        _approve(db, firing)
    db.docs["repositories/r2"] = {"repo_id": "r2", "tenant_id": "eng"}
    db.docs[f"schedules/{SUBJECT}"]["scope"] = {"mode": "repos", "repo_ids": ["r1", "r2"]}
    executor = FakeExecutor()
    ticker(ctx, executor, at=LATER).tick()
    ticker(ctx, executor, at=LATER + timedelta(minutes=1)).tick()
    _created(db, held[CONTROL])
    _still_held(db, held[SUBJECT])
    assert _marked(db, held[SUBJECT]) == []
    (entry,) = _audit(db)
    assert entry["detail"]["firing_id"] == held[SUBJECT]["firing_id"]


def test_an_unapproved_firing_stays_held(db, ctx) -> None:
    held = _held(db, ctx, SUBJECT, CONTROL)
    _approve(db, held[CONTROL])
    executor = FakeExecutor()
    ticker(ctx, executor, at=LATER).tick()
    _created(db, held[CONTROL])
    _still_held(db, held[SUBJECT])
    assert "run_approval" not in _firing(db, held[SUBJECT])
    assert _marked(db, held[SUBJECT]) == [] and executor.calls == 1
    assert _audit(db) == []


def test_another_tenants_approval_cannot_release_a_firing(db, ctx) -> None:
    """A `run_approval` naming a record the firing's tenant does not own --
    approved, for this very firing and digest, but in `research` -- releases
    nothing."""
    held = _held(db, ctx, SUBJECT, CONTROL)
    _approve(db, held[CONTROL])
    subject = held[SUBJECT]
    foreign = "apr_" + "f" * 24
    db.docs[f"{approvals.COLLECTION}/{foreign}"] = {
        "approval_id": foreign, "tenant_id": "research", "kind": approvals.RUN,
        "subject": {"schedule_id": SUBJECT, "firing_id": subject["firing_id"]},
        "digest": subject["params_digest"], "state": approvals.APPROVED,
        "decided_by": "mallory@saga.xyz", "decided_at": NOW,
    }
    _firing(db, subject)["approval_id"] = foreign
    _firing(db, subject)["run_approval"] = {
        "by": "mallory@saga.xyz", "at": NOW, "approval_id": foreign, "digest": subject["params_digest"],
    }
    executor = FakeExecutor()
    ticker(ctx, executor, at=LATER).tick()
    _created(db, held[CONTROL])
    _still_held(db, subject)
    assert _marked(db, subject) == [] and executor.calls == 1
    (entry,) = _audit(db)
    assert entry["tenant_id"] == "eng" and entry["detail"]["code"] == "approval_not_found"


def test_a_run_approval_without_its_approved_record_releases_nothing(db, ctx) -> None:
    """A field written on the firing alone, with the tenant's record still pending."""
    held = _held(db, ctx, SUBJECT, CONTROL)
    _approve(db, held[CONTROL])
    subject = held[SUBJECT]
    _firing(db, subject)["run_approval"] = {
        "by": ALICE, "at": NOW, "approval_id": subject["approval_id"], "digest": subject["params_digest"],
    }
    executor = FakeExecutor()
    ticker(ctx, executor, at=LATER).tick()
    _created(db, held[CONTROL])
    _still_held(db, subject)
    assert executor.calls == 1


def test_a_page_of_older_unapproved_firings_does_not_starve_an_approved_one(db, ctx) -> None:
    """The awaiting_approval scan is platform-wide and oldest first. More than
    ADVANCE_PAGE older firings without a `run_approval` sit ahead of the
    approved one; it is still created."""
    held = _held(db, ctx, SUBJECT, CONTROL)
    _approve(db, held[CONTROL])
    template = _firing(db, held[SUBJECT])
    for n in range(schedulefire.ADVANCE_PAGE + 5):
        fid = f"fir_old{n:017d}"
        db.docs[f"schedule_firings/{fid}"] = {
            **template, "firing_id": fid, "fired_at": template["fired_at"] - timedelta(days=1, minutes=n),
        }
    executor = FakeExecutor()
    ticker(ctx, executor, at=LATER).tick()
    _created(db, held[CONTROL])
    _still_held(db, held[SUBJECT])
    assert executor.calls == 1 and len(tasks(db)) == 1
