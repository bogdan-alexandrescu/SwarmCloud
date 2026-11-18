"""The `docs-drift` schedule type (docs/schedules.md §3.10, lane S10f).

WHAT IS HELD HERE
-----------------
* THE TYPE IS AVAILABLE because its file exists, and declares its own
  budget caps (the catalogue leaves them to the lane, SD4).
* THE TASK is one `claude-code` task per repository in scope, cloned at the
  registration's default branch, marked with the firing, up to the room.
  Its prompt names the three checks, the paths and `drift.md`.
* A REPORT (`fix: false`) is strategy `collect` and tells the agent to
  change nothing and write `verification.md` (no_change, a success).
* A FIX (`fix: true`) is strategy `direct-pr`, edits only `paths`, never
  `.github/workflows/`, and does not merge.
* PERMISSIONS ARE SAID PLAINLY: a fix whose token the last probe read
  without push refuses with `docs_drift_cannot_push`, naming the repository,
  when switched on; off, it is let through and logged. `unknown` never
  refuses. Another tenant's probe is never read.
* A DRY RUN creates nothing, and shows the rights a fix would need.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent_worker.runners import mock
from agent_worker.runners.base import RunnerContext
from swarm_api import gittokens, refusals, schedulefire, scheduletypes
from swarm_api.schedtypes import docs_drift

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    NOW,
    REPOSITORY,
    ctx,
    firing_for,
    github,
    make_schedule,
    register,
    tasks,
    writes,
)

SWITCH = refusals.env_name(docs_drift.DocsDriftCannotPush.code)


def drift(db, repo_ids, **kwargs):
    return make_schedule(db, type_="docs-drift", repo_ids=tuple(repo_ids), cron="0 3 * * *", **kwargs)


def drift_tasks(db):
    return [t for t in tasks(db) if t["metadata"].get("docs_drift")]


def strategy(task) -> str:
    return (task["metadata"].get("dispatch") or {}).get("strategy") or "collect"


def probe(db, repo_id: str, *, tenant_id: str = "eng", push: str = "ok", prs: str = "ok") -> None:
    """The tenant default's record and its last probe of `repo_id`."""
    record = gittokens.record_for_slot(tenant_id, gittokens.Scope.TENANT,
                                       registered_by=gittokens.SYSTEM, now=NOW)
    db.docs[f"{gittokens.COLLECTION}/{record.token_id}"] = record.to_firestore()
    cell = lambda state: {"state": state, "reason": f"the probe read {state}", "evidence": ""}  # noqa: E731
    db.docs[f"{gittokens.CHECKS_COLLECTION}/{record.token_id}_{repo_id}"] = {
        "token_id": record.token_id, "tenant_id": tenant_id, "repo_id": repo_id,
        "capabilities": {"push": cell(push), "open_pull_requests": cell(prs)},
    }


def test_the_type_is_available_because_its_file_exists_with_its_own_caps() -> None:
    entry = scheduletypes.get("docs-drift")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is docs_drift
    assert scheduletypes.budget_caps(entry) is docs_drift.BUDGET_CAPS


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_a_report_is_one_collect_task_per_repository_marked_and_read_only(db, ctx) -> None:
    first = register(db)
    second = register(db, repo="gadgets", default_branch="trunk")
    firing = firing_for(ctx, drift(db, [first, second]))

    work = docs_drift.create(firing)

    made = drift_tasks(db)
    assert len(made) == 2
    assert sorted(w["repo_id"] for w in work) == sorted([first, second])
    by_repo = {t["repository_url"]: t for t in made}
    gadgets = by_repo["https://github.com/example-org/gadgets"]
    assert gadgets["repository_ref"] == "trunk"
    for task in made:
        assert task["runner_profile"] == "claude-code" and strategy(task) == "collect"
        assert task["metadata"]["schedule"] == firing.mark and task["submitted_by"] == ALICE
        assert task["metadata"]["fix"] is False
        text = task["input"]["prompt"]
        assert "  docs/\n" in text and "drift.md" in text and "verification.md" in text
        assert "Do not change, create or delete any file" in text
        assert "pr-title.txt" not in text
        for check in ("LINE CITATIONS", "`path::symbol` CITATIONS", "STATED DEFAULTS"):
            assert check in text


def test_the_room_bounds_the_tasks_and_a_gone_registration_is_skipped(db, ctx) -> None:
    ids = [register(db, repo=f"r{n}") for n in range(3)]
    db.docs[f"repositories/{ids[0]}"]["archived"] = True

    docs_drift.create(firing_for(ctx, drift(db, ids), room=1))

    (task,) = drift_tasks(db)
    assert task["repository_url"].endswith("/r1")


def test_another_tenants_registration_is_not_checked(db, ctx) -> None:
    theirs = register(db, tenant_id="research")
    assert docs_drift.create(firing_for(ctx, drift(db, [theirs]))) == []
    assert drift_tasks(db) == []


def test_the_mock_runner_writes_drift_md_from_the_tasks_own_input(db, ctx, tmp_path: Path) -> None:
    docs_drift.create(firing_for(ctx, drift(db, [register(db)])))
    (task,) = drift_tasks(db)
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    run = RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={**task["input"], "steps": 1, "sleep_seconds": 0.0,
                 "artifact_name": docs_drift.REPORT_FILE, "attempt_id": "att_1", "attempt_count": 1},
    )

    mock.body(run)

    assert "STATED DEFAULTS" in (artifacts / docs_drift.REPORT_FILE).read_text()


# ---------------------------------------------------------------------------
# The fix, and the rights it needs
# ---------------------------------------------------------------------------


def test_a_fix_opens_a_pull_request_on_the_paths_only_and_never_merges(db, ctx) -> None:
    repo_id = register(db)
    probe(db, repo_id)
    docs_drift.create(firing_for(ctx, drift(db, [repo_id], params={"fix": True,
                                                                  "paths": ["docs/", "README.md"]})))

    (task,) = drift_tasks(db)
    assert strategy(task) == "direct-pr" and task["metadata"]["fix"] is True
    text = task["input"]["prompt"]
    assert "  docs/\n  README.md\n" in text
    assert "Edit only files under the paths above" in text and ".github/workflows/" in text
    assert "pr-title.txt" in text and "do not merge it" in text
    assert "verification.md" in text


def test_a_fix_the_token_cannot_push_is_refused_naming_the_repository(db, ctx, monkeypatch) -> None:
    monkeypatch.setenv(SWITCH, "on")
    repo_id = register(db)
    probe(db, repo_id, push="missing")

    with pytest.raises(docs_drift.DocsDriftCannotPush) as caught:
        docs_drift.create(firing_for(ctx, drift(db, [repo_id], params={"fix": True})))

    error = caught.value
    assert error.code == "docs_drift_cannot_push" and error.status_code == 403
    assert REPOSITORY in error.message and "contents:write" in error.message
    assert list(error.detail["repositories"][REPOSITORY]) == ["push"]
    assert error.detail["repositories"][REPOSITORY]["push"]["token"] == "tenant"
    assert drift_tasks(db) == []


def test_the_refusal_ships_off_and_the_fix_goes_through_logged(db, ctx, monkeypatch, caplog) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    assert refusals.SWITCHES["docs_drift_cannot_push"].default is False
    repo_id = register(db)
    probe(db, repo_id, prs="missing")

    with caplog.at_level(logging.WARNING, logger="swarm_api.refusals"):
        docs_drift.create(firing_for(ctx, drift(db, [repo_id], params={"fix": True})))

    assert len(drift_tasks(db)) == 1
    assert "docs_drift_cannot_push" in caplog.text


@pytest.mark.parametrize("seed", ["none", "unknown", "other_tenant"])
def test_unmeasured_rights_do_not_refuse(db, ctx, monkeypatch, seed) -> None:
    monkeypatch.setenv(SWITCH, "on")
    repo_id = register(db)
    if seed == "unknown":
        probe(db, repo_id, push="unknown", prs="unknown")
    elif seed == "other_tenant":
        # research's probe of the same id says missing: never eng's evidence.
        probe(db, repo_id, tenant_id="research", push="missing")

    docs_drift.create(firing_for(ctx, drift(db, [repo_id], params={"fix": True})))

    assert len(drift_tasks(db)) == 1


def test_a_report_never_reads_or_needs_push(db, ctx, monkeypatch) -> None:
    monkeypatch.setenv(SWITCH, "on")
    repo_id = register(db)
    probe(db, repo_id, push="missing", prs="missing")

    docs_drift.create(firing_for(ctx, drift(db, [repo_id])))

    assert len(drift_tasks(db)) == 1


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_says_what_it_would_send_and_creates_nothing(db, ctx) -> None:
    good, bad = register(db), register(db, repo="gadgets")
    probe(db, good)
    probe(db, bad, push="missing")
    firing = firing_for(ctx, drift(db, [good, bad], params={"fix": True}))
    before = dict(db.docs)

    output = docs_drift.dry_run(firing)

    assert db.docs == before
    assert output["profile"] == "claude-code" and output["strategy"] == "direct-pr"
    assert output["needs"] == ["contents:read", "contents:write", "pull_requests:write"]
    assert output["merge"].startswith("never by the platform")
    rows = {row["repository"]: row for row in output["repositories"]}
    assert rows[REPOSITORY]["would"] == "queue" and rows[REPOSITORY]["branch"] == "main"
    assert 0 < rows[REPOSITORY]["prompt_bytes"]
    assert rows["example-org/gadgets"]["would"] == "refuse: docs_drift_cannot_push"
    assert rows["example-org/gadgets"]["rights"]["push"]["state"] == "missing"


def test_a_report_dry_run_lists_no_push_rights(db, ctx) -> None:
    output = docs_drift.dry_run(firing_for(ctx, drift(db, [register(db)]), room=0))
    assert output["strategy"] == "collect" and output["needs"] == ["contents:read"]
    assert output["merge"] is None and output["repositories"][0]["would"] == "room"
