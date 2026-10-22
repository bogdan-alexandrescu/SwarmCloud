"""The `failure-review` schedule type (docs/schedules.md §3.16, docs/design/failure-review.md;
lane FR-3 TYPE, part of #1933).

WHAT IS HELD HERE -- the issue's "how would you know", and the owner's decisions
---------------------------------------------------------------------------------
* A SEEDED MOCK FAILURE FILES ONE ISSUE AND STARTS ONE RUN. A firing over a
  window holding a mock task that failed with a fixed error hands it to one
  investigator; the next firing takes the investigator's `rca.json` in, files
  one issue in the bug form, in the platform's own repository, and starts one
  fix run on it.
* A SECOND FIRING OVER THE SAME WINDOW FILES NOTHING: no issue, no comment, no run.
* AN IAM FIX IS FILED `needs-owner` WITH NO RUN, whether the investigator
  named the hold or FR-1's stops find it in the fix it proposed.
* THE REVIEWER'S OWN FAILURE IS NOT IN THE NEXT DIGEST (`self`), its review
  files nothing, and the window it lost is read again.
* CREATION WITHOUT THE AUDITED SWITCH KEEPS `merge: approve` (Q8); the fix
  run then waits for a person's merge approval.
* THE BUDGET DEFAULTS ARE THE OWNER'S TRIPWIRES (Q9): 200, 1000, 1.
* A cause in a tenant's own code is a finding in that tenant, never an issue
  here; nothing to review still writes its summary; dry run writes nothing.

No cloud and no emulator: the in-memory Firestore, and GitHub as a fake
transport under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import pytest

from swarm_api import failuredigest, schedulefire, schedules, scheduletypes
from swarm_api.errors import Forbidden, ValidationFailed
from swarm_api.scheduletypes import BudgetCaps
from swarm_api.schedtypes import failure_review

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    NOW,
    SCHEDULE_ID,
    ctx,
    github,
    issue_runs,
    make_schedule,
    register,
    tasks,
    writes,
)
from .test_schedtypes_observer import _artifacts

SWARM_OWNER, SWARM_REPO = "bogdan-alexandrescu", "SwarmCloud"
SWARM = f"{SWARM_OWNER}/{SWARM_REPO}"
ENTRY = scheduletypes.get("failure-review")
SEEDED_ERROR = "mock runner failed: seeded failure 7"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def platform_repo(db) -> str:
    return register(db, owner=SWARM_OWNER, repo=SWARM_REPO, platform=True)


def review_schedule(db, **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_="failure-review", scope_mode="platform", cron="0 */6 * * *",
                         max_concurrent=1, **kwargs)


def fire(ctx, schedule: dict[str, Any], at: datetime, *, room: int = 1) -> schedulefire.Firing:
    slot = at.replace(second=0, microsecond=0)
    return schedulefire.Firing(
        ctx=ctx, schedule=schedule, firing={"firing_id": schedulefire.firing_id(schedule["schedule_id"], slot),
                                            "slot": slot},
        owner=schedulefire.schedule_owner_auth(ctx, schedule), repo_ids=[], room=room, now=at,
        record=[].append,
    )


def seed_failure(db, task_id: str = "task_mock1", *, tenant: str = "research",
                 ended: datetime = NOW - timedelta(hours=1), **extra: Any) -> None:
    """A mock task that failed with a fixed error (the issue's own example)."""
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": tenant, "state": "FAILED", "runner_profile": "mock",
        "resource_class": "standard", "submitted_by": "dev@example.com",
        "created_at": ended - timedelta(minutes=10), "updated_at": ended, "completed_at": ended,
        "end_cause": "runner_error", "last_error": SEEDED_ERROR, "input": {"fail": True},
        "metadata": {}, **extra,
    }


def investigators(db) -> list[dict[str, Any]]:
    return sorted((t for t in tasks(db) if (t.get("metadata") or {}).get("failure_review")),
                  key=lambda t: t["metadata"]["failure_review"])


def end(db, task: dict[str, Any], state: str = "SUCCEEDED", at: datetime | None = None, **extra: Any) -> None:
    doc = db.docs[f"tasks/{task['id']}"]
    doc.update(state=state, completed_at=at or NOW + timedelta(hours=1), **extra)


def cause(refs: Sequence[str] = ("f-001",), *, hold: str = "none", where: str = "platform",
          fix: str = "Return the seeded error only when the input asks for it; a red-first test "
                     "in tests/unit/worker proves it.",
          files: Sequence[str] = ("apps/agent-worker/agent_worker/runners/mock.py",)) -> dict[str, Any]:
    return {
        "fingerprint_of": {"component": "worker", "site": "apps/agent-worker/agent_worker/runners/mock.py::run",
                           "class": "mock_seeded_failure", "signature": "mock runner failed: seeded failure <n>"},
        "known_fingerprint": None, "failures": list(refs),
        "cause": "The mock runner raises its seeded failure on every input, asked for or not.",
        "evidence": [{"command": "rg -n 'seeded failure' apps/agent-worker", "result": "runners/mock.py:40 raises it"}],
        "fix": fix, "files": list(files), "hold": hold, "where": where,
    }


def rca(firing_id: str, *causes: dict[str, Any], unexplained: Sequence[dict[str, Any]] = ()) -> str:
    return json.dumps({"firing_id": firing_id, "causes": list(causes), "unexplained": list(unexplained)})


def reviews(db) -> dict[str, dict[str, Any]]:
    return {d["firing_id"]: d for p, d in db.docs.items() if p.startswith(f"{failure_review.REVIEWS}/")}


def records(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("failure_causes/")]


def filed(writes) -> list[dict[str, Any]]:
    return [i for i in writes.issues.values() if "title" in i]


def first_firing(db, ctx) -> tuple[dict[str, Any], dict[str, Any], schedulefire.Firing]:
    """Firing 1 over a window holding the seeded failure: its investigator, ended."""
    platform_repo(db)
    seed_failure(db)
    schedule = review_schedule(db)
    firing = fire(ctx, schedule, NOW)
    failure_review.create(firing)
    (investigator,) = investigators(db)
    return schedule, investigator, firing


# ---------------------------------------------------------------------------
# The type: catalogue, scope, gate and budget
# ---------------------------------------------------------------------------


def test_the_type_is_built_platform_scoped_and_the_owners_to_create() -> None:
    available, reason = scheduletypes.availability(ENTRY)
    # MUTATION: delete schedtypes/failure_review.py and the type is unavailable.
    assert available and reason == ""
    # MUTATION: creators {"platform": "admin"} lets an admin read every tenant's failures.
    assert ENTRY.creators == {"platform": "owner"}
    assert ENTRY.min_interval == timedelta(hours=6) and ENTRY.profile == failure_review.PROFILE == "claude-code"
    assert ENTRY.pushes(ENTRY.params_model()) is True


def test_the_budget_defaults_are_the_owners_tripwires() -> None:
    # MUTATION: BUDGET_CAPS(15, 120, 8) (issue-sweep's figures) fails here.
    assert scheduletypes.budget_caps(ENTRY) == BudgetCaps(per_run_usd=200, per_day_usd=1000, max_concurrent=1)
    assert schedules.resolve_budget(ENTRY, None) == {"per_run_usd": 200.0, "per_day_usd": 1000.0,
                                                     "max_concurrent": 1}


def _create(**over: Any) -> dict[str, Any]:
    raw = {"name": "failure review", "type": "failure-review", "scope": {"mode": "platform"},
           "cron": "0 */6 * * *", "timezone": "UTC", **over}
    return schedules.build_schedule(schedules.parse_create(raw), tenant_id="eng", actor=ALICE, now=NOW,
                                    registered_repo_ids=[], is_owner=True)


def test_creation_without_the_audited_switch_keeps_merge_approve() -> None:
    created = _create()
    # MUTATION: a default gate of merge "auto" would merge with no one asked.
    assert created["gate"]["merge"] == "approve" and created["scope"]["mode"] == "platform"
    with pytest.raises(ValidationFailed) as caught:
        _create(gate={"merge": "auto"})
    # MUTATION: dropping the use_merge_switch check lets a create set auto.
    assert caught.value.code == "use_merge_switch"
    # The switch reaches it: the floor allows `auto`, only through the switch.
    assert schedules.resolve_gate(ENTRY, schedules.GateIn(merge="auto"), current=created["gate"],
                                  via_merge_switch=True)["merge"] == "auto"


def test_an_admin_who_is_not_the_owner_cannot_create_it_and_no_other_scope_exists() -> None:
    raw = {"name": "failure review", "type": "failure-review", "scope": {"mode": "platform"},
           "cron": "0 */6 * * *", "timezone": "UTC"}
    with pytest.raises(Forbidden) as caught:
        schedules.build_schedule(schedules.parse_create(raw), tenant_id="eng", actor=ALICE, now=NOW,
                                 registered_repo_ids=[], is_admin=True)
    assert caught.value.code == "owner_required"
    with pytest.raises(ValidationFailed) as caught:
        schedules.build_schedule(schedules.parse_create({**raw, "scope": {"mode": "all"}}), tenant_id="eng",
                                 actor=ALICE, now=NOW, registered_repo_ids=[], is_owner=True)
    assert caught.value.code == "scope_not_allowed"


def test_a_shorter_cron_than_six_hours_is_refused() -> None:
    with pytest.raises(ValidationFailed):
        _create(cron="0 * * * *")


# ---------------------------------------------------------------------------
# The investigator
# ---------------------------------------------------------------------------


def test_a_firing_hands_the_digest_to_one_investigator_on_the_platform_repository(db, ctx) -> None:
    schedule, investigator, firing = first_firing(db, ctx)

    assert investigator["runner_profile"] == "claude-code"
    assert investigator["repository_url"] == f"https://github.com/{SWARM}"
    # `collect`: it reports and changes nothing (stored as the absence of a dispatch strategy).
    assert ((investigator["metadata"].get("dispatch") or {}).get("strategy") or "collect") \
        == failure_review.STRATEGY == "collect"
    mark = investigator["metadata"]["schedule"]
    assert mark["type"] == "failure-review" and mark["firing_id"] == firing.firing["firing_id"]
    text = investigator["input"]["prompt"]
    # The digest as DATA, between two lines carrying one nonce (§3).
    lines = [line for line in text.splitlines() if line.startswith("=== FAILURE DIGEST ")]
    assert len(lines) == 2 and lines[0] == lines[1]
    # MUTATION: no redaction-free last_error, or the wrong window, and the failure is not there.
    assert SEEDED_ERROR in text and "rca.json" in text
    review = reviews(db)[firing.firing["firing_id"]]
    assert review["state"] == failure_review.INVESTIGATING
    assert review["refs"] == {"f-001": {"kind": "agent", "tenant_id": "research", "id": "task_mock1"}}
    # Another tenant's prompt never crosses (owner decision Q1): `input` is not in the text.
    assert '"fail": true' not in text


def test_a_schedule_whose_tenant_has_no_platform_registration_is_refused(db, ctx, monkeypatch) -> None:
    register(db)  # an ordinary registration, not `platform: true`
    schedule = review_schedule(db)
    monkeypatch.setenv("REFUSAL_FAILURE_REVIEW_NO_PLATFORM_REPOSITORY", "on")
    with pytest.raises(ValidationFailed) as caught:
        failure_review.create(fire(ctx, schedule, NOW))
    assert caught.value.code == "failure_review_no_platform_repository"
    assert investigators(db) == []

    # A new refusal ships off (refusals.py): through, with nothing reviewed or submitted.
    monkeypatch.setenv("REFUSAL_FAILURE_REVIEW_NO_PLATFORM_REPOSITORY", "off")
    assert failure_review.create(fire(ctx, schedule, NOW)) == []
    assert investigators(db) == [] and reviews(db) == {}


# ---------------------------------------------------------------------------
# The issue's "how would you know"
# ---------------------------------------------------------------------------


def test_a_seeded_mock_failure_files_one_issue_and_starts_one_run(db, ctx, writes, monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(firing.firing["firing_id"], cause())})

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=6)))

    # MUTATION: skip the take-in and no issue is filed.
    (issue,) = filed(writes)
    assert issue["repository"] == SWARM and issue["labels"] == [{"name": "bug"}]
    assert issue["title"] == "The mock runner raises its seeded failure on every input, asked for or not."
    (record,) = records(db)
    assert issue["body"].startswith(f"<!-- failure-review fingerprint={record['fingerprint']} -->")
    # The bug form's sections, in its order (CLAUDE.md "Issues").
    sections = ["### What happened", "### Severity", "### Where you saw it", "### Environment",
                "### Steps to reproduce", "### What you expected instead", "### Identifiers",
                "### Screenshots, errors, logs", "### Anything else"]
    assert [issue["body"].index(s) for s in sections] == sorted(issue["body"].index(s) for s in sections)
    # Another tenant's failure is a count per opaque label, never its id.
    assert "task_mock1" not in issue["body"] and "research" not in issue["body"]
    # MUTATION: a FILE_HELD for a clean cause starts no run.
    (run,) = issue_runs(db)
    assert run["issue"]["number"] == issue["number"]
    assert f"{run['issue']['owner']}/{run['issue']['repo']}" == SWARM
    assert run["tenant_id"] == "eng" and run["created_by"] == f"schedule:{SCHEDULE_ID}"
    # Q8: no switch made, so the fix waits for a person's merge approval.
    assert run["merge_approval"] == "required"
    assert record["issue"] == issue["number"] and record["state"] == "fixing"
    assert [r["run_id"] for r in record["runs"]] == [run["id"]]
    review = reviews(db)[firing.firing["firing_id"]]
    assert review["state"] == failure_review.FILED
    assert review["summary"]["issues_filed"] == 1 and review["summary"]["runs_started"] == 1


def test_a_second_firing_over_the_same_window_files_nothing(db, ctx, writes, monkeypatch) -> None:
    schedule, first, firing1 = first_firing(db, ctx)
    end(db, first)
    second_at = NOW + timedelta(hours=6)
    files = {first["id"]: rca(firing1.firing["firing_id"], cause())}
    _artifacts(monkeypatch, ctx, files)
    failure_review.create(fire(ctx, schedule, second_at))
    # Firing 2's window overlaps firing 1's: it hands the same failure over again.
    second = [t for t in investigators(db) if t["id"] != first["id"]][0]
    firing2_id = second["metadata"]["schedule"]["firing_id"]
    assert reviews(db)[firing2_id]["refs"]["f-001"]["id"] == "task_mock1"
    end(db, second, at=second_at + timedelta(hours=1))
    files[second["id"]] = rca(firing2_id, cause())
    before = list(writes.writes())
    runs_before = len(issue_runs(db))

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=12)))

    # MUTATION: count the overlap's failure again and a comment (or a run) is made.
    assert writes.writes() == before and len(filed(writes)) == 1
    assert len(issue_runs(db)) == runs_before == 1
    assert reviews(db)[firing2_id]["summary"]["seen_again"] == 1


def test_a_take_in_that_stopped_half_way_files_nothing_twice(db, ctx, writes, monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(firing.firing["firing_id"], cause())})
    later = fire(ctx, schedule, NOW + timedelta(hours=6))
    registration = failure_review.platform_registration(ctx, "eng")
    failure_review.take_in_one(later, registration, dict(reviews(db)[firing.firing["firing_id"]]))
    (record,) = records(db)
    fp = record["fingerprint"]
    # As a crash would leave it: the issue and the run made on GitHub and in
    # the run store, neither recorded, and the cause's progress not done.
    db.docs[f"failure_causes/{fp}"].update(issue=None, runs=[])
    review = reviews(db)[firing.firing["firing_id"]]
    progress = dict(review["progress"][fp])
    for key in ("done", "issue", "run_id"):
        progress.pop(key, None)
    stale = dict(review, state=failure_review.FILING, progress={fp: progress})

    failure_review.take_in_one(later, registration, stale)

    # MUTATION: no marker search before create_issue and a second issue is filed.
    assert len(filed(writes)) == 1
    # MUTATION: no live-run adoption before start_run and a second run starts.
    (run,) = issue_runs(db)
    assert records(db)[0]["issue"] == filed(writes)[0]["number"]
    assert [r["run_id"] for r in records(db)[0]["runs"]] == [run["id"]]


def test_an_iam_fix_is_filed_needs_owner_and_gets_no_run(db, ctx, writes, monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(
        firing.firing["firing_id"], cause(hold="iam", files=("terraform/infra/iam_worker.tf",)))})

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=6)))

    (issue,) = filed(writes)
    # MUTATION: file a held cause with `bug` alone and nothing keeps the sweep off it.
    assert issue["labels"] == [{"name": "bug"}, {"name": "needs-owner"}]
    assert "Held for the owner" in issue["body"]
    # MUTATION: start a run for FILE_HELD and this list is not empty.
    assert issue_runs(db) == []
    (record,) = records(db)
    assert record["state"] == "held" and record["runs"] == []


def test_an_iam_change_in_the_proposed_fix_is_held_even_when_the_investigator_said_none(
        db, ctx, writes, monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    risky = cause(fix="Grant the worker google_project_iam_member roles/logging.viewer.", files=())
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(firing.firing["firing_id"], risky)})

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=6)))

    # MUTATION: trust the investigator's `hold: none` and a run is started for an IAM change.
    assert issue_runs(db) == []
    (issue,) = filed(writes)
    assert {"name": "needs-owner"} in issue["labels"]
    assert records(db)[0]["hold"] == "iam"


def held_later(db, ctx, writes, monkeypatch) -> tuple[dict[str, Any], int]:
    """A cause filed clean and given a fix run that ended without a merge, then
    found again by a later firing with a new failure in a hold category: the
    HOLD action (§5), on an issue that already exists. Returns the schedule and
    the issue's number; the third firing, which takes the HOLD in, is the
    caller's."""
    schedule, first, firing1 = first_firing(db, ctx)
    end(db, first)
    files = {first["id"]: rca(firing1.firing["firing_id"], cause())}
    _artifacts(monkeypatch, ctx, files)
    second_at = NOW + timedelta(hours=6)
    seed_failure(db, "task_mock2", ended=second_at - timedelta(minutes=30))
    failure_review.create(fire(ctx, schedule, second_at))
    (issue,) = filed(writes)
    (run,) = issue_runs(db)
    db.docs[f"issue_runs/{run['id']}"]["state"] = "FAILED"   # the fix run did not hold
    second = [t for t in investigators(db) if t["id"] != first["id"]][0]
    firing2_id = second["metadata"]["schedule"]["firing_id"]
    refs = reviews(db)[firing2_id]["refs"]
    assert sorted(where["id"] for where in refs.values()) == ["task_mock1", "task_mock2"]
    end(db, second, at=second_at + timedelta(hours=1))
    # Every ref is accounted for; task_mock1 is counted already, task_mock2 is new.
    files[second["id"]] = rca(firing2_id, cause(tuple(refs), hold="iam",
                                                files=("terraform/infra/iam_worker.tf",)))
    return schedule, int(issue["number"])


def test_a_cause_held_later_is_labelled_needs_owner_and_still_commented(db, ctx, writes, monkeypatch) -> None:
    schedule, number = held_later(db, ctx, writes, monkeypatch)
    assert writes.labelled == []

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=12)))

    (record,) = records(db)
    assert record["state"] == "held"
    # MUTATION: drop the add_labels call from the HOLD action and the issue is never labelled.
    assert writes.labelled == [(number, ["needs-owner"])]
    assert {"name": "needs-owner"} in writes.issues[number]["labels"]
    # MUTATION: replace the comment with the label and the issue says nothing of why it is held.
    (comment,) = writes.on_issue(number)
    assert "Held for the owner" in comment["body"]
    # MUTATION: run a fix for HOLD and a second run is started.
    assert len(issue_runs(db)) == 1


def test_a_label_github_refuses_leaves_the_cause_held(db, ctx, writes, monkeypatch) -> None:
    schedule, number = held_later(db, ctx, writes, monkeypatch)
    writes.refuse[("POST", f"/repos/{SWARM}/issues/{number}/labels")] = 403

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=12)))

    # The label was asked for and refused.
    assert [w for w in writes.writes() if w[1].endswith(f"/issues/{number}/labels")]
    # MUTATION: let the label's refusal propagate and the review fails before the cause is held.
    (record,) = records(db)
    assert record["state"] == "held"
    assert "Held for the owner" in writes.on_issue(number)[0]["body"]
    (review,) = [r for r in reviews(db).values() if r["progress"].get(record["fingerprint"], {}).get("action")
                 == "hold"]
    # MUTATION: undo the hold on a label failure and this progress is not done.
    assert review["state"] == failure_review.FILED
    assert review["progress"][record["fingerprint"]]["done"] is True


def test_the_reviewers_own_failure_is_not_in_the_next_digest_and_files_nothing(db, ctx, writes,
                                                                                monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator, state="FAILED", last_error="runner exceeded its 7200s timeout and was killed")
    _artifacts(monkeypatch, ctx, {})

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=6)))

    first = reviews(db)[firing.firing["firing_id"]]
    # §9: nothing is half filed, and the review says why.
    assert first["state"] == failure_review.FAILED and "FAILED" in first["last_error"]
    assert filed(writes) == [] and issue_runs(db) == []
    second_id = [t for t in investigators(db) if t["id"] != investigator["id"]][0]["metadata"]["schedule"]["firing_id"]
    second = reviews(db)[second_id]
    # MUTATION: drop the `self` rule and the investigator is f-00n of the next digest.
    assert investigator["id"] not in {r["id"] for r in second["refs"].values()}
    assert investigator["id"] in second["skipped_ids"][failuredigest.SELF]
    assert second["skipped"][failuredigest.SELF] == 1
    # No review has succeeded, so the window is the plain 7 h back: nothing
    # moved the cursor past the failed review's hours.
    assert failure_review.last_succeeded_until(db, SCHEDULE_ID) is None
    assert second["since"] == NOW + timedelta(hours=6) - failuredigest.WINDOW


def test_an_invalid_rca_files_nothing_and_its_window_is_read_again(db, ctx, writes, monkeypatch) -> None:
    # An earlier review succeeded with its window ending 6 h before firing 1.
    db.docs[f"{failure_review.CURSORS}/{SCHEDULE_ID}"] = {
        "schedule_id": SCHEDULE_ID, "tenant_id": "eng", "until": NOW - timedelta(hours=6)}
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    # f-001 accounted for by no cause and not unexplained.
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(firing.firing["firing_id"])})
    later = fire(ctx, schedule, NOW + timedelta(hours=12))

    failure_review.create(later)

    first = reviews(db)[firing.firing["firing_id"]]
    # MUTATION: file from an unvalidated rca.json and the issue list is not empty.
    assert first["state"] == failure_review.FAILED and "not accounted for: f-001" in first["last_error"]
    assert filed(writes) == [] and issue_runs(db) == []
    # §2: the failed review's hours are read again -- the next window starts at
    # the last SUCCEEDED review's end less the overlap, so the seeded failure,
    # 13 h before this firing, is reviewed again.
    again = reviews(db)[later.firing["firing_id"]]
    # MUTATION: advance the cursor on a failed review and the window is the plain 7 h.
    assert again["since"] == NOW - timedelta(hours=7)
    assert again["refs"]["f-001"]["id"] == "task_mock1"


def test_a_succeeded_review_moves_the_next_window_to_its_end_less_the_overlap(db, ctx, monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(firing.firing["firing_id"], cause())})
    later = NOW + timedelta(hours=12)

    failure_review.create(fire(ctx, schedule, later))

    assert failure_review.last_succeeded_until(db, SCHEDULE_ID) == NOW
    newest = max(reviews(db).values(), key=lambda r: r["until"])
    # MUTATION: ignore the cursor and the window is the plain 7 h, losing hours.
    assert newest["since"] == NOW - failuredigest.OVERLAP


def test_a_cause_in_a_tenants_own_code_is_a_finding_there_and_never_an_issue(db, ctx, writes,
                                                                             monkeypatch) -> None:
    schedule, investigator, firing = first_firing(db, ctx)
    end(db, investigator)
    _artifacts(monkeypatch, ctx, {investigator["id"]: rca(firing.firing["firing_id"], cause(where="tenant_repo"))})

    failure_review.create(fire(ctx, schedule, NOW + timedelta(hours=6)))

    # MUTATION: file a tenant_repo cause with the platform's credential and this is not empty.
    assert filed(writes) == [] and issue_runs(db) == []
    (finding,) = [d for p, d in db.docs.items() if p.startswith(f"{failure_review.FINDINGS}/")]
    assert finding["tenant_id"] == "research" and finding["task_id"] == "task_mock1"
    assert finding["evidence"]["last_error"] == SEEDED_ERROR
    assert records(db)[0]["state"] == "reported"


def test_nothing_to_review_writes_the_summary_and_runs_no_agent(db, ctx, writes) -> None:
    platform_repo(db)
    schedule = review_schedule(db, params={"summary_issue": f"{SWARM}#7"})
    firing = fire(ctx, schedule, NOW)

    # MUTATION: submit an investigator for an empty digest and work is not [].
    assert failure_review.create(firing) == [] and investigators(db) == []
    review = reviews(db)[firing.firing["firing_id"]]
    assert review["state"] == failure_review.FILED
    (comment,) = writes.on_issue(7)
    assert comment["body"].startswith(failure_review.summary_marker(firing.firing["firing_id"]))
    assert "0 failures in the window" in comment["body"]


def test_a_summary_issue_outside_the_platform_repository_is_not_written(db, ctx, writes) -> None:
    platform_repo(db)
    schedule = review_schedule(db, params={"summary_issue": "someone-else/repo#7"})
    failure_review.create(fire(ctx, schedule, NOW))
    # MUTATION: post wherever summary_issue points and the other repository gets a comment.
    assert writes.writes() == []


def test_a_dry_run_says_what_it_would_review_and_writes_nothing(db, ctx, writes) -> None:
    platform_repo(db)
    seed_failure(db)
    schedule = review_schedule(db)
    before = dict(db.docs)

    out = failure_review.dry_run(fire(ctx, schedule, NOW))

    assert out["reviewed"] == 1 and out["would_investigate"] is True and out["repository"] == SWARM
    # MUTATION: write the review record in a dry run and the documents differ.
    assert db.docs == before and writes.writes() == []


def test_the_executor_module_declares_what_the_tick_loads() -> None:
    assert schedulefire.load_executor(ENTRY) is failure_review
    assert Path(failure_review.__file__).name == "failure_review.py"
