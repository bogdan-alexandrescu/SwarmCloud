"""The `observer` schedule type (docs/schedules.md §3.4, lane S6).

WHAT IS HELD HERE
-----------------
* THE DIGEST is computed from documents swarm-api already holds, for the
  schedule's tenant only, over `window_hours`: cost per profile with its
  coverage, start latency (LEASED to RUNNING), the share of pull requests red
  on their first CI run, parks by reason, firings refused by code. A section
  the platform does not record says so; it is never a zero.
* THE TASK is one `claude-code` task with NO repository, the digest in its
  prompt as data between delimiter lines carrying the firing id, bounded to
  48 KiB, marked with the firing.
* OFFLINE WITH THE MOCK RUNNER, that task's prompt produces `report.md` from
  a fixture digest (§9, S6 acceptance).
* PROPOSALS of an earlier report land in the inbox once each.
* `file_issues: true` is refused with its own code, not silently ignored.
* THE PLATFORM VARIANT reads aggregates across tenants and lists no run ids.
* A DRY RUN creates nothing.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_worker.runners import mock
from agent_worker.runners.base import RunnerContext
from swarm_api import approvals, schedulefire, scheduletypes
from swarm_api.errors import NotFound
from swarm_api.schedtypes import observer

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    NOW,
    SCHEDULE_ID,
    ctx,
    firing_for,
    github,
    make_schedule,
    tasks,
    writes,
)


def watch(db, **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_="observer", scope_mode="all", cron="0 * * * *", **kwargs)


def seed_work(db, tenant_id: str = "eng", *, prefix: str = "") -> None:
    """Two tasks of two profiles, three attempts (one without a cost), a parked
    task, two runs with pull requests (one red first), two refused firings."""
    def task(task_id: str, profile: str, **extra: Any) -> None:
        db.docs[f"tasks/{prefix}{task_id}"] = {
            "id": f"{prefix}{task_id}", "tenant_id": tenant_id, "runner_profile": profile,
            "state": "SUCCEEDED", "created_at": NOW - timedelta(hours=2), "metadata": {}, **extra,
        }

    def attempt(attempt_id: str, task_id: str, *, wait_s: float, cost: float | None) -> None:
        leased = NOW - timedelta(hours=1)
        db.docs[f"attempts/{prefix}{attempt_id}"] = {
            "attempt_id": f"{prefix}{attempt_id}", "task_id": f"{prefix}{task_id}", "tenant_id": tenant_id,
            "created_at": leased, "started_at": leased + timedelta(seconds=wait_s),
            **({"cost_usd": cost} if cost is not None else {}),
        }

    task("t1", "claude-code", metadata={"issue_run": "run_a"})
    task("t2", "codex")
    task("t3", "claude-code", state="PARKED", park_reason="PROVIDER_QUOTA_EXHAUSTED")
    task("old", "claude-code", created_at=NOW - timedelta(days=5))
    attempt("a1", "t1", wait_s=10, cost=1.5)
    attempt("a2", "t1", wait_s=30, cost=2.0)
    attempt("a3", "t2", wait_s=20, cost=None)
    attempt("a4", "old", wait_s=999, cost=50.0)
    for run_id, rounds in (("run_a", 1), ("run_b", 0)):
        db.docs[f"issue_runs/{prefix}{run_id}"] = {
            "id": f"{prefix}{run_id}", "tenant_id": tenant_id, "created_at": NOW - timedelta(hours=3),
            "pull_request": {"number": 7, "url": "u"}, "ci_fix_round": rounds,
        }
    for n, code in ((1, "WORKSPACE_NOT_READY"), (2, "OVERLAP")):
        db.docs[f"schedule_firings/{prefix}f{n}"] = {
            "firing_id": f"{prefix}f{n}", "tenant_id": tenant_id, "fired_at": NOW - timedelta(hours=1),
            "state": "refused" if n == 1 else "skipped", "skip": {"code": code},
        }


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("observer")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is observer


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def test_the_digest_is_the_tenants_own_figures_with_their_coverage(db) -> None:
    seed_work(db)
    seed_work(db, "research", prefix="r_")  # another tenant's: never in eng's digest

    digest = observer.compute_digest(db, "eng", now=NOW, window_hours=24, focus=observer_focus())

    assert digest["scope"] == "tenant" and digest["tasks"] == 3
    cost = digest["cost"]
    assert cost["by_profile"] == {
        "claude-code": {"attempts": 2, "attempts_with_cost": 2, "cost_usd": 3.5},
        # NULL IS NOT ZERO: its one attempt reported nothing.
        "codex": {"attempts": 1, "attempts_with_cost": 0, "cost_usd": None},
    }
    assert cost["coverage"] == round(2 / 3, 3)
    assert cost["top_runs"] == [{"run": "run_a", "cost_usd": 3.5}]
    assert digest["latency"] == {"attempts_measured": 3, "attempts_unstarted": 0,
                                 "median_s": 20.0, "p95_s": 30.0}
    assert digest["ci"]["runs_with_pull_request"] == 2 and digest["ci"]["share"] == 0.5
    assert digest["parks"] == {"by_reason": {"PROVIDER_QUOTA_EXHAUSTED": 1}}
    assert digest["refusals"]["firings_by_code"] == {"OVERLAP": 1, "WORKSPACE_NOT_READY": 1}
    assert "not_measured" in digest["titles"] and "not_measured" in digest["idle_fixes"]
    assert "r_" not in json.dumps(digest, default=str)


def observer_focus() -> list[str]:
    return list(scheduletypes.OBSERVER_FOCUS)


def test_focus_limits_the_sections(db) -> None:
    seed_work(db)
    digest = observer.compute_digest(db, "eng", now=NOW, window_hours=24, focus=["ci"])
    assert "ci" in digest and not {"cost", "latency", "parks", "refusals", "titles"} & set(digest)


def test_no_attempts_is_no_latency_not_zero(db) -> None:
    digest = observer.compute_digest(db, "eng", now=NOW, window_hours=24, focus=["latency", "cost"])
    assert digest["latency"]["median_s"] is None and digest["cost"]["coverage"] is None


def test_the_platform_variant_reads_every_tenant_and_names_no_run(db) -> None:
    seed_work(db)
    seed_work(db, "research", prefix="r_")

    digest = observer.compute_digest(db, None, now=NOW, window_hours=24, focus=observer_focus())

    assert digest["scope"] == "platform" and digest["tasks"] == 6
    assert digest["cost"]["by_profile"]["claude-code"]["cost_usd"] == 7.0
    assert "top_runs" not in digest["cost"]


def test_the_digest_is_bounded_and_says_what_it_dropped() -> None:
    digest = {"window": {"hours": 24}, "truncated": [],
              "cost": {"by_profile": {}, "top_runs": [{"run": f"run_{n:06d}" + "x" * 200, "cost_usd": 1.0}
                                                      for n in range(1000)]}}

    text, cut = observer.bounded(digest)

    assert cut and len(text.encode()) <= observer.MAX_DIGEST_BYTES
    assert json.loads(text)["dropped"] == ["cost.top_runs"]


# ---------------------------------------------------------------------------
# The task, and the mock runner
# ---------------------------------------------------------------------------


def test_one_claude_code_task_with_no_repository_and_the_digest_as_data(db, ctx) -> None:
    seed_work(db)
    firing = firing_for(ctx, watch(db))

    work = observer.create(firing)

    report = [t for t in tasks(db) if t["metadata"].get("observer")]
    assert len(report) == 1
    (task,) = report
    assert work == [{"kind": "task", "id": task["id"], "repo_id": None}]
    assert task["runner_profile"] == "claude-code" and task["repository_url"] is None
    assert task["metadata"]["schedule"] == firing.mark and task["submitted_by"] == "alice@saga.xyz"
    assert task["state"] in ("QUEUED", "READY")
    prompt = task["input"]["prompt"]
    fid = firing.firing["firing_id"]
    begin, end = f"=== OBSERVER DIGEST {fid} ===", f"=== END OBSERVER DIGEST {fid} ==="
    assert prompt.count(begin) == 1 and prompt.count(end) == 1
    digest = json.loads(prompt.split(begin + "\n", 1)[1].split("\n" + end, 1)[0])
    assert digest["cost"]["by_profile"]["claude-code"]["cost_usd"] == 3.5
    assert "report.md" in prompt and "proposals.json" in prompt
    assert len(prompt.encode()) < 64 * 1024


def test_the_mock_runner_writes_report_md_from_a_fixture_digest(db, ctx, tmp_path: Path) -> None:
    """Offline: the observer's own task input, run by the mock runner, writes report.md."""
    seed_work(db)
    observer.create(firing_for(ctx, watch(db)))
    (task,) = [t for t in tasks(db) if t["metadata"].get("observer")]
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    run = RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={**task["input"], "steps": 1, "sleep_seconds": 0.0,
                 "artifact_name": observer.REPORT_FILE, "attempt_id": "att_1", "attempt_count": 1},
    )

    mock.body(run)

    report = (artifacts / observer.REPORT_FILE).read_text()
    assert "=== OBSERVER DIGEST" in report and "PROVIDER_QUOTA_EXHAUSTED" in report


def test_file_issues_is_refused_with_its_own_code(db, ctx, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_OBSERVER_FILE_ISSUES_UNAVAILABLE", "on")
    with pytest.raises(observer.ObserverFileIssuesUnavailable) as caught:
        observer.create(firing_for(ctx, watch(db, params={"file_issues": True})))
    assert caught.value.code == "observer_file_issues_unavailable" and caught.value.status_code == 422
    assert tasks(db) == []


def test_a_dry_run_says_what_it_would_send_and_creates_nothing(db, ctx) -> None:
    seed_work(db)
    before = len(tasks(db))

    output = observer.dry_run(firing_for(ctx, watch(db)))

    assert output["profile"] == "claude-code" and output["repositories"] == []
    assert 0 < output["digest_bytes"] < output["prompt_bytes"] <= 64 * 1024
    assert "cost" in output["sections"] and len(tasks(db)) == before


# ---------------------------------------------------------------------------
# Proposals: an earlier report's, into the inbox once
# ---------------------------------------------------------------------------


def _earlier_report(db, task_id: str = "task_report1", *, age: timedelta = timedelta(hours=1)) -> None:
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": "eng", "runner_profile": "claude-code", "state": "SUCCEEDED",
        "created_at": NOW - age - timedelta(minutes=5), "completed_at": NOW - age,
        "metadata": {"observer": SCHEDULE_ID,
                     "schedule": {"schedule_id": SCHEDULE_ID, "firing_id": "f_prev", "type": "observer"}},
    }


def _artifacts(monkeypatch, ctx, files: dict[str, str]) -> list[str]:
    asked: list[str] = []

    def read_artifact(tenant_id: str, task_id: str, *, submitted_by: Any, name: str, **_: Any) -> dict:
        asked.append(f"{tenant_id}/{task_id}/{name}")
        if task_id not in files:
            raise NotFound(f"task {task_id} lists no artifact {name}")
        return {"status": "ok", "content": files[task_id], "truncated": False}

    monkeypatch.setattr(ctx.inspection, "read_artifact", read_artifact)
    return asked


def _proposals(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("approvals/") and d.get("kind") == "proposal"]


def test_an_earlier_reports_proposals_reach_the_inbox_once(db, ctx, monkeypatch) -> None:
    _earlier_report(db)
    written = [
        {"title": "Planner tasks wait 30 s to start", "finding": "p95 start latency is 30 s",
         "evidence": "latency.p95_s = 30", "suggestion": "warm one slot", "extra": "ignored"},
        {"finding": "no title: refused"},
        {"title": "Half the pull requests are red first", "finding": "ci.share = 0.5"},
    ]
    asked = _artifacts(monkeypatch, ctx, {"task_report1": json.dumps(written)})
    schedule = watch(db)

    observer.create(firing_for(ctx, schedule))
    observer.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    rows = sorted(_proposals(db), key=lambda d: d["subject"]["position"])
    assert [r["summary"] for r in rows] == ["Planner tasks wait 30 s to start",
                                            "Half the pull requests are red first"]
    first = rows[0]
    assert first["tenant_id"] == "eng" and first["state"] == approvals.PENDING
    assert first["subject"] == {"schedule_id": SCHEDULE_ID, "firing_id": "f_prev",
                                "task_id": "task_report1", "position": 0}
    assert first["proposal"]["evidence"] == "latency.p95_s = 30" and "extra" not in first["proposal"]
    assert first["digest"] == observer.proposal_digest(observer.Proposal(**written[0]))
    assert asked and all(a == "eng/task_report1/proposals.json" for a in asked)


def test_a_report_with_no_proposals_file_or_too_old_brings_none(db, ctx, monkeypatch) -> None:
    _earlier_report(db, "task_none")
    _earlier_report(db, "task_old", age=timedelta(days=8))
    _artifacts(monkeypatch, ctx, {"task_old": json.dumps([{"title": "stale"}])})

    observer.create(firing_for(ctx, watch(db)))

    assert _proposals(db) == []


def test_another_schedules_report_is_not_read(db, ctx, monkeypatch) -> None:
    _earlier_report(db)
    db.docs["tasks/task_report1"]["metadata"]["schedule"]["schedule_id"] = "sch_000000000777"
    asked = _artifacts(monkeypatch, ctx, {"task_report1": json.dumps([{"title": "x"}])})

    observer.create(firing_for(ctx, watch(db)))

    assert asked == [] and _proposals(db) == []


def test_parse_proposals_takes_a_bounded_valid_list() -> None:
    many = [{"title": f"finding {n}"} for n in range(30)]
    assert len(observer.parse_proposals(json.dumps(many))) == observer.MAX_PROPOSALS
    assert observer.parse_proposals(json.dumps({"proposals": many[:2]}))[1].title == "finding 1"
    assert observer.parse_proposals("not json") == [] and observer.parse_proposals("{}") == []
