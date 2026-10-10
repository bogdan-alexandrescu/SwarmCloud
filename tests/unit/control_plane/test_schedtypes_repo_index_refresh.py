"""The `repo-index-refresh` schedule type (docs/schedules.md §3.3, §8.3, lane S6).

WHAT IS HELD HERE
-----------------
* It queues through the index's own queueing (`RepoIndex.request_run`): one
  `indexer` task per repository, by profile NAME, submitted as the schedule's
  owner, waiting for admission like any task, and carrying the firing's
  `metadata.schedule` so a retried firing adopts it.
* The in-flight rule holds: a repository with a run in flight -- a person's,
  the change trigger's or an earlier firing's -- gets no second run; its head
  is recorded as pending.
* `only_if_behind`, `kind`, a paused registration, and the firing's room.
* A dry run reads Firestore only: no head is read and nothing is claimed or
  submitted.
"""

from __future__ import annotations

import pytest

from swarm_api import schedulefire, scheduletypes
from swarm_api.schedtypes import repo_index_refresh

from .repo_fakes import TenantTokens
from .repo_index_fakes import IndexGitHub, sha
from .test_schedtypes_issue_sweep import (
    ALICE,
    SCHEDULE_ID,
    firing_for,
    firings,
    make_context,
    make_schedule,
    register,
    tasks,
)
from .conftest import seed_tenant

ONE, TWO = sha("one"), sha("two")


@pytest.fixture
def transport():
    return IndexGitHub(heads={"main": ONE})


@pytest.fixture
def ctx(db, tokens, group_map, objects, transport, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=transport)


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def refresh(db, *repo_ids: str, **kwargs):
    return make_schedule(db, type_="repo-index-refresh", repo_ids=repo_ids, cron="0 * * * *", **kwargs)


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("repo-index-refresh")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is repo_index_refresh


def test_it_queues_one_indexer_task_marked_and_as_the_owner(db, ctx, repo_id) -> None:
    firing = firing_for(ctx, refresh(db, repo_id))

    work = repo_index_refresh.create(firing)

    (task,) = tasks(db)
    assert work == [{"kind": "task", "id": task["id"], "repo_id": repo_id}]
    # By NAME, the index's own prompt, the head it read (invariant 10).
    assert task["runner_profile"] == "indexer" and set(task["input"]) == {"prompt"}
    assert task["repository_ref"] == ONE and task["metadata"]["repo_index"] == repo_id
    # The firing's mark, so a finisher after a crash adopts it (§2.2).
    assert task["metadata"]["schedule"] == firing.mark
    assert task["tenant_id"] == "eng" and task["submitted_by"] == ALICE
    # Waiting for admission: no lease, no demand (invariant 1).
    assert task["state"] in ("QUEUED", "READY") and task.get("current_lease_id") is None
    run = db.docs[f"repo_index_runs/{task['id']}"]
    assert run["requested_by"] == ALICE and run["kind"] == "full"
    assert db.docs[f"repositories/{repo_id}"]["index"]["in_flight_task_id"] == task["id"]


def test_a_run_in_flight_is_not_duplicated(db, ctx, repo_id, transport) -> None:
    schedule = refresh(db, repo_id)
    repo_index_refresh.create(firing_for(ctx, schedule))
    transport.heads["main"] = TWO

    again = repo_index_refresh.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert again == [] and len(tasks(db)) == 1
    assert db.docs[f"repositories/{repo_id}"]["index"]["pending_sha"] == TWO


def test_only_if_behind_skips_a_current_index_and_queues_a_behind_one(db, ctx, transport) -> None:
    current = register(db, index={"current_sha": ONE})
    behind = register(db, repo="gadgets", index={"current_sha": TWO})
    schedule = refresh(db, current, behind, params={"only_if_behind": True})

    work = repo_index_refresh.create(firing_for(ctx, schedule))

    assert [w["repo_id"] for w in work] == [behind]


def test_a_paused_registration_is_not_queued(db, ctx) -> None:
    paused = register(db, index={"paused": True})

    assert repo_index_refresh.create(firing_for(ctx, refresh(db, paused))) == []
    assert tasks(db) == []


def test_kind_is_what_is_asked_for(db, ctx, repo_id) -> None:
    repo_index_refresh.create(firing_for(ctx, refresh(db, repo_id, params={"kind": "incremental"})))

    (task,) = tasks(db)
    run = db.docs[f"repo_index_runs/{task['id']}"]
    # No promoted index to build on, so §3.4 runs it full and says why.
    assert run["requested_kind"] == "incremental" and run["kind"] == "full" and run["kind_reason"]


def test_the_firing_never_queues_past_its_room(db, ctx) -> None:
    one, two = register(db), register(db, repo="gadgets")

    work = repo_index_refresh.create(firing_for(ctx, refresh(db, one, two), room=1))

    assert [w["repo_id"] for w in work] == [one] and len(tasks(db)) == 1


def test_a_head_that_cannot_be_read_refuses_the_firing_when_nothing_was_queued(db, ctx, transport, repo_id) -> None:
    transport.heads.clear()

    with pytest.raises(repo_index_refresh.IndexRefreshFailed) as caught:
        repo_index_refresh.create(firing_for(ctx, refresh(db, repo_id)))

    # A 4xx, so the tick ends the firing `refused` rather than retrying a
    # deleted branch every minute; the repository's own code is named.
    assert caught.value.status_code < 500 and caught.value.code == "index_refresh_failed"
    assert caught.value.detail == {"repositories": {repo_id: "failed: head_unreadable"}}
    assert tasks(db) == []


def test_one_unreadable_repository_does_not_stop_the_others(db, ctx, transport) -> None:
    gone = register(db, repo="gadgets", default_branch="gone")
    here = register(db)

    work = repo_index_refresh.create(firing_for(ctx, refresh(db, gone, here)))

    assert [w["repo_id"] for w in work] == [here]


def test_a_tick_records_the_task_as_the_firings_work(db, ctx, repo_id) -> None:
    refresh(db, repo_id)

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    (task,) = tasks(db)
    assert [w["id"] for w in firing["work"]] == [task["id"]], firing


def test_a_dry_run_reads_firestore_only_and_creates_nothing(db, ctx, transport) -> None:
    idle = register(db)
    busy = register(db, repo="gadgets", index={"in_flight_task_id": "task_x", "current_sha": ONE})
    calls = len(transport.calls)

    output = repo_index_refresh.dry_run(firing_for(ctx, refresh(db, idle, busy)))

    assert [row["would"] for row in output["repositories"]] == ["queue", "in_flight"]
    assert tasks(db) == [] and len(transport.calls) == calls
    assert "in_flight_task_id" not in (db.docs[f"repositories/{idle}"].get("index") or {})
