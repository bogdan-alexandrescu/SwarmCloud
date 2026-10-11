"""The `epic-triage` schedule type (docs/schedules.md §3.5, lane S10a).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT: its file exists, it declares its BUDGET_CAPS, so the
  catalogue serves it available and the tick's loader imports it.
* A BOX is an unticked task-list item in a TRUSTED author's comment, outside a
  code fence; a box id names its comment and its text.
* ONE `claude-code` TASK PER EPIC with unticked boxes, in a clone of the
  default branch, the boxes as data between delimiter lines carrying the
  firing id, up to `max_epics`. An issue without the label, an epic with no
  unticked box and an epic whose ticks wait for approval get no task.
* TAKE IN: a proposed tick WITHOUT a PR or commit reference is refused, and
  so is one GitHub does not confirm (a PR not merged, a commit not on the
  default branch). The rest become ONE `plan` approval; nothing is written
  to GitHub before a person approves, and a task is taken in once.
* APPLY: once approved, swarm-api ticks the boxes and appends the evidence,
  once -- a box a person unticks later is not ticked again. A token GitHub
  refuses fails the intake action with a code naming `issues: write`, and the
  approval is retried. The epic is never closed.
* A firing whose every repository failed to list is refused with
  `epic_triage_unreadable` when that switch is on, and goes through when off.
* A DRY RUN creates nothing and writes nothing.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real clients.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import approvals, schedulefire, scheduletypes
from swarm_api.errors import NotFound
from swarm_api.schedtypes import epic_triage

from . import forge_fakes
from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    NOW,
    SCHEDULE_ID,
    ctx,
    firing_for,
    issue,
    make_schedule,
    register,
    tasks,
)

EPIC = 7
SHA_ON_MAIN = "a1b2c3d" + "0" * 33
SHA_ELSEWHERE = "f" * 40


class Writes(forge_fakes.GitHubWrites):
    """The shared fake, plus the compare route the commit check reads."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.on_main: set[str] = set()

    def __call__(self, method, url, headers, body, timeout):
        rest = urlparse(url).path.strip("/").split("/")[3:]
        if method == "GET" and rest[:1] == ["compare"]:
            self.calls.append((method, url, dict(headers), body))
            head = rest[1].split("...", 1)[1]
            status = "behind" if head in self.on_main else "diverged"
            return 200, json.dumps({"status": status}).encode()
        return super().__call__(method, url, headers, body, timeout)

    def comment(self, number: int, body: str, *, association: str = "MEMBER") -> int:
        cid = self.add_comment(number, body)
        self.comments[cid]["author_association"] = association
        return cid

    def merged(self, number: int, *, base: str = "main", merged: bool = True) -> None:
        self.open_pull(number, "0" * 40)
        self.pulls[number].update(merged=merged, state="closed", base={"ref": base})


@pytest.fixture
def github():
    return forge_fakes.GitHub(issues=[issue(EPIC, labels=("epic",)), issue(8)], pulls=[], files={})


@pytest.fixture
def writes():
    return Writes()


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def triage(db, repo_id: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("cron", "0 */6 * * *")
    kwargs.setdefault("max_concurrent", 6)
    return make_schedule(db, type_="epic-triage", repo_ids=(repo_id,), **kwargs)


FINDINGS = (
    "- [ ] **The stale comment says 3** · `apps/x.py` `f()` · lane A\n"
    "- [x] **Already done** · `apps/y.py` `g()` · lane B\n"
    "```\n- [ ] not a box: inside a fence\n```\n"
    "- [ ] **The guard checks less than its name** · `apps/z.py` `h()` · lane C"
)


def boxes_of(writes: Writes, cid: int) -> list[epic_triage.Box]:
    return epic_triage.unticked_boxes(cid, writes.comments[cid]["body"])


def triage_tasks(db) -> list[dict[str, Any]]:
    """The triage tasks a firing submitted (an earlier one a test seeds has SUCCEEDED)."""
    return [t for t in tasks(db) if epic_triage.METADATA_KEY in t["metadata"] and t["state"] != "SUCCEEDED"]


def plan_approvals(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("approvals/") and d.get("type") == "epic-triage"]


def edits(writes: Writes) -> list[tuple[str, str]]:
    return writes.writes()


# ---------------------------------------------------------------------------
# The type, and boxes
# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists_and_declares_caps() -> None:
    entry = scheduletypes.get("epic-triage")
    assert scheduletypes.availability(entry) == (True, "")
    assert scheduletypes.budget_caps(entry) is epic_triage.BUDGET_CAPS
    assert schedulefire.load_executor(entry) is epic_triage


def test_a_box_is_an_unticked_item_outside_a_fence() -> None:
    boxes = epic_triage.unticked_boxes(41, FINDINGS)
    assert [b.text for b in boxes] == [
        "**The stale comment says 3** · `apps/x.py` `f()` · lane A",
        "**The guard checks less than its name** · `apps/z.py` `h()` · lane C",
    ]
    assert boxes[0].id == epic_triage.box_id(41, "**The stale comment says 3**  ·  `apps/x.py` `f()` · lane A")
    assert boxes[0].id != epic_triage.box_id(42, boxes[0].text)


def test_tick_line_ticks_only_the_named_unticked_box() -> None:
    (first, second) = epic_triage.unticked_boxes(41, FINDINGS)

    body, done = epic_triage.tick_line(FINDINGS, 41, second.id, " · evidence #9")

    assert done
    assert "- [x] **The guard checks less than its name** · `apps/z.py` `h()` · lane C · evidence #9" in body
    assert "- [ ] **The stale comment says 3**" in body and "- [ ] not a box" in body
    assert epic_triage.tick_line(body, 41, second.id, " · again") == (body, False)
    assert epic_triage.tick_line(FINDINGS, 99, first.id, "x") == (FINDINGS, False)


# ---------------------------------------------------------------------------
# The triage tasks
# ---------------------------------------------------------------------------


def test_one_claude_code_task_per_epic_with_its_boxes_as_data(db, ctx, writes, repo_id) -> None:
    cid = writes.comment(EPIC, FINDINGS)
    writes.comment(EPIC, "- [ ] **an outsider's box**", association="NONE")
    firing = firing_for(ctx, triage(db, repo_id))

    work = epic_triage.create(firing)

    (task,) = triage_tasks(db)
    assert work == [{"kind": "task", "id": task["id"], "repo_id": None}]
    assert task["runner_profile"] == "claude-code"
    assert task["repository_url"] == "https://github.com/saga-xyz/widgets"
    assert task["repository_ref"] == "main"
    assert task["metadata"]["schedule"] == firing.mark and task["submitted_by"] == ALICE
    assert task["metadata"][epic_triage.METADATA_KEY] == {
        "schedule_id": SCHEDULE_ID, "repo_id": repo_id, "number": EPIC}
    prompt = task["input"]["prompt"]
    fid = firing.firing["firing_id"]
    begin, end = f"=== EPIC BOXES {fid} ===", f"=== END EPIC BOXES {fid} ==="
    assert prompt.count(begin) == 1 and prompt.count(end) == 1
    data = json.loads(prompt.split(begin + "\n", 1)[1].split("\n" + end, 1)[0])
    assert data["epic"] == f"saga-xyz/widgets#{EPIC}" and data["default_branch"] == "main"
    assert [b["box"] for b in data["boxes"]] == [b.id for b in boxes_of(writes, cid)]
    assert "outsider" not in prompt and "ticks.json" in prompt
    # Reading only: nothing written to GitHub, and the issue without the label got nothing.
    assert edits(writes) == [] and len(triage_tasks(db)) == 1


def test_an_epic_with_no_unticked_box_gets_no_task(db, ctx, writes, repo_id) -> None:
    writes.comment(EPIC, "- [x] **done** · `a.py` `f()`")
    assert epic_triage.create(firing_for(ctx, triage(db, repo_id))) == []
    assert triage_tasks(db) == []


def test_max_epics_bounds_the_tasks(db, ctx, github, writes, repo_id) -> None:
    github.issues = [issue(n, labels=("Epic",)) for n in (1, 2, 3)]
    for n in (1, 2, 3):
        writes.comment(n, "- [ ] **open** · `a.py` `f()`")

    epic_triage.create(firing_for(ctx, triage(db, repo_id, params={"max_epics": 2})))

    assert sorted(t["metadata"][epic_triage.METADATA_KEY]["number"] for t in triage_tasks(db)) == [1, 2]


def test_a_dry_run_creates_nothing_and_writes_nothing(db, ctx, writes, repo_id) -> None:
    writes.comment(EPIC, FINDINGS)

    output = epic_triage.dry_run(firing_for(ctx, triage(db, repo_id)))

    assert output["would_triage"] == [{"epic": f"saga-xyz/widgets#{EPIC}", "boxes": 2}]
    assert output["profile"] == "claude-code" and 0 < output["prompt_bytes"][0] < 64 * 1024
    assert tasks(db) == [] and plan_approvals(db) == [] and edits(writes) == []


# ---------------------------------------------------------------------------
# Take in: proposals checked, one approval, nothing written
# ---------------------------------------------------------------------------


def earlier_task(db, repo_id: str, task_id: str = "task_triage1", *, number: int = EPIC) -> None:
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": "eng", "runner_profile": "claude-code", "state": "SUCCEEDED",
        "created_at": NOW - timedelta(hours=7), "completed_at": NOW - timedelta(hours=6),
        "metadata": {epic_triage.METADATA_KEY: {"schedule_id": SCHEDULE_ID, "repo_id": repo_id,
                                                "number": number},
                     "schedule": {"schedule_id": SCHEDULE_ID, "firing_id": "f_prev", "type": "epic-triage"}},
    }


def artifacts(monkeypatch, ctx, files: dict[str, Any]) -> list[str]:
    asked: list[str] = []

    def read_artifact(tenant_id: str, task_id: str, *, submitted_by: Any, name: str, **_: Any) -> dict:
        asked.append(f"{tenant_id}/{task_id}/{name}")
        if task_id not in files:
            raise NotFound(f"task {task_id} lists no artifact {name}")
        return {"status": "ok", "content": json.dumps(files[task_id]), "truncated": False}

    monkeypatch.setattr(ctx.inspection, "read_artifact", read_artifact)
    return asked


def proposals_for(writes: Writes, cid: int) -> list[dict[str, Any]]:
    stale, guard = boxes_of(writes, cid)
    return [
        {"box": stale.id, "status": "done", "evidence": {"pull_request": 12},
         "file": "apps/x.py", "call_site": "f()", "note": "the comment now says 4"},
        {"box": guard.id, "status": "done", "note": "trust me"},  # no reference: refused
        {"box": guard.id, "status": "done", "evidence": {"commit": SHA_ELSEWHERE}},
        {"box": "c1-000000000000", "status": "done", "evidence": {"pull_request": 12}},
        {"box": guard.id, "status": "open", "note": "still checks less"},
    ]


def test_proposed_ticks_are_checked_and_become_one_plan_approval(db, ctx, writes, repo_id, monkeypatch) -> None:
    cid = writes.comment(EPIC, FINDINGS)
    writes.merged(12)
    earlier_task(db, repo_id)
    asked = artifacts(monkeypatch, ctx, {"task_triage1": {"ticks": proposals_for(writes, cid)}})
    recorded: list[dict[str, Any]] = []

    work = epic_triage.create(firing_for(ctx, triage(db, repo_id), recorded=recorded))

    (doc,) = plan_approvals(db)
    stale, guard = boxes_of(writes, cid)
    assert doc["kind"] == approvals.PLAN and doc["state"] == approvals.PENDING
    assert doc["subject"]["epic"] == f"saga-xyz/widgets#{EPIC}" and doc["subject"]["task_id"] == "task_triage1"
    assert [t["box"] for t in doc["ticks"]] == [stale.id]
    assert doc["ticks"][0]["evidence"] == {"pull_request": 12} and doc["ticks"][0]["comment_id"] == cid
    reasons = sorted(r["reason"] for r in doc["refused_ticks"])
    assert reasons == ["commit ffffffffffff is not on main", "no PR or commit reference",
                       "not an unticked box of this epic now"]
    assert doc["still_open"] == 1 and doc["digest"] == epic_triage.ticks_digest(doc["ticks"])
    assert asked == ["eng/task_triage1/ticks.json"]
    # Nothing written to GitHub before a person approves, and the epic waits.
    assert edits(writes) == [] and triage_tasks(db) == []
    (action,) = [w for w in work if w["kind"] == "api_action"]
    assert action == recorded[0] and action["outcome"] == "succeeded"
    assert action["detail"]["taken_in"] == ["task_triage1"]


def test_a_tick_on_an_unmerged_pull_request_is_refused(db, ctx, writes, repo_id, monkeypatch) -> None:
    cid = writes.comment(EPIC, FINDINGS)
    writes.merged(12, merged=False)
    writes.on_main.add(SHA_ON_MAIN[:10])
    stale, guard = boxes_of(writes, cid)
    earlier_task(db, repo_id)
    artifacts(monkeypatch, ctx, {"task_triage1": [
        {"box": stale.id, "status": "done", "evidence": {"pull_request": "#12"}},
        {"box": guard.id, "status": "done", "evidence": {"commit": SHA_ON_MAIN[:10]}},
    ]})

    epic_triage.create(firing_for(ctx, triage(db, repo_id)))

    (doc,) = plan_approvals(db)
    assert [t["evidence"] for t in doc["ticks"]] == [{"commit": SHA_ON_MAIN[:10]}]
    assert [r["reason"] for r in doc["refused_ticks"]] == ["#12 is not merged"]


def test_ticks_with_no_evidence_make_no_approval(db, ctx, writes, repo_id, monkeypatch) -> None:
    cid = writes.comment(EPIC, FINDINGS)
    earlier_task(db, repo_id)
    artifacts(monkeypatch, ctx, {"task_triage1": [
        {"box": b.id, "status": "done"} for b in boxes_of(writes, cid)]})

    work = epic_triage.create(firing_for(ctx, triage(db, repo_id)))

    assert plan_approvals(db) == []
    (action,) = [w for w in work if w["kind"] == "api_action"]
    (result,) = action["detail"]["intake_results"]
    assert result["outcome"] == "nothing_to_approve" and result["refused"] == 2


# ---------------------------------------------------------------------------
# Apply: after approval, swarm-api edits the comment, once
# ---------------------------------------------------------------------------


def propose_and_approve(db, ctx, writes, repo_id, monkeypatch) -> tuple[int, dict[str, Any]]:
    cid = writes.comment(EPIC, FINDINGS)
    writes.merged(12)
    earlier_task(db, repo_id)
    artifacts(monkeypatch, ctx, {"task_triage1": {"ticks": proposals_for(writes, cid)}})
    schedule = triage(db, repo_id)
    first: list[dict[str, Any]] = []
    epic_triage.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:first", recorded=first))
    db.docs[f"schedule_firings/{SCHEDULE_ID}:first"] = {
        "firing_id": f"{SCHEDULE_ID}:first", "schedule_id": SCHEDULE_ID, "tenant_id": "eng",
        "recorded": first}
    (doc,) = plan_approvals(db)
    approvals.decide(db, NOW, "eng", doc["approval_id"], approve=True, by=ALICE, digest=doc["digest"])
    return cid, schedule


def test_an_approved_tick_is_applied_with_its_evidence_once(db, ctx, writes, repo_id, monkeypatch) -> None:
    cid, schedule = propose_and_approve(db, ctx, writes, repo_id, monkeypatch)
    second: list[dict[str, Any]] = []

    epic_triage.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:second", recorded=second))

    body = writes.comments[cid]["body"]
    assert ("- [x] **The stale comment says 3** · `apps/x.py` `f()` · lane A · ticked with evidence #12 "
            "(apps/x.py · f()) · approved in SwarmCloud epic-triage") in body
    assert "- [ ] **The guard checks less than its name**" in body
    assert edits(writes) == [("PATCH", f"/repos/saga-xyz/widgets/issues/comments/{cid}")]
    (action,) = second
    assert action["outcome"] == "succeeded" and len(action["detail"]["applied"]) == 1
    assert action["detail"]["apply_results"][0]["ticked"] == 1

    # A person unticks it; the ledger keeps the next firing from ticking it again.
    db.docs[f"schedule_firings/{SCHEDULE_ID}:second"] = {
        "firing_id": f"{SCHEDULE_ID}:second", "schedule_id": SCHEDULE_ID, "tenant_id": "eng",
        "recorded": second}
    writes.comments[cid]["body"] = FINDINGS
    epic_triage.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:third"))
    assert writes.comments[cid]["body"] == FINDINGS
    # The epic itself is never closed.
    assert not any(path.endswith(f"/issues/{EPIC}") for _, path in edits(writes))


def test_a_token_without_issues_write_is_reported_and_retried(db, ctx, writes, repo_id, monkeypatch) -> None:
    cid, schedule = propose_and_approve(db, ctx, writes, repo_id, monkeypatch)
    writes.status["PATCH"] = 403
    second: list[dict[str, Any]] = []

    epic_triage.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:second", recorded=second))

    (action,) = second
    assert action["outcome"] == "failed" and action["detail"]["applied"] == []
    (failure,) = action["detail"]["apply_failures"]
    assert failure["code"] == "writeback_forbidden" and "issues: write" in failure["message"]
    assert writes.comments[cid]["body"] == FINDINGS

    # Not in the ledger, so the next firing applies it once the token can.
    db.docs[f"schedule_firings/{SCHEDULE_ID}:second"] = {
        "firing_id": f"{SCHEDULE_ID}:second", "schedule_id": SCHEDULE_ID, "tenant_id": "eng",
        "recorded": second}
    del writes.status["PATCH"]
    epic_triage.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:third"))
    assert "- [x] **The stale comment says 3**" in writes.comments[cid]["body"]


def test_an_epic_whose_ticks_wait_is_not_triaged_again(db, ctx, writes, repo_id, monkeypatch) -> None:
    cid = writes.comment(EPIC, FINDINGS)
    writes.merged(12)
    earlier_task(db, repo_id)
    artifacts(monkeypatch, ctx, {"task_triage1": {"ticks": proposals_for(writes, cid)}})
    schedule = triage(db, repo_id)

    epic_triage.create(firing_for(ctx, schedule))
    epic_triage.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert len(plan_approvals(db)) == 1 and triage_tasks(db) == []


# ---------------------------------------------------------------------------
# Unreadable: refused when switched on, through when off
# ---------------------------------------------------------------------------


def test_every_repository_unreadable_is_refused_when_switched_on(db, ctx, github, repo_id, monkeypatch) -> None:
    github.status["/issues"] = 403
    monkeypatch.setenv("REFUSAL_EPIC_TRIAGE_UNREADABLE", "on")
    with pytest.raises(epic_triage.EpicTriageUnreadable) as caught:
        epic_triage.create(firing_for(ctx, triage(db, repo_id)))
    assert caught.value.code == "epic_triage_unreadable" and caught.value.status_code == 409
    assert caught.value.detail["failures"][0]["repository"] == "saga-xyz/widgets"

    monkeypatch.setenv("REFUSAL_EPIC_TRIAGE_UNREADABLE", "off")
    assert epic_triage.create(firing_for(ctx, triage(db, repo_id))) == []
