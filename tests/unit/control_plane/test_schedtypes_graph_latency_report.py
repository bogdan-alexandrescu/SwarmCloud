"""The `graph-latency-report` schedule type and the graph delivery report
(#1840, docs/design/graph-answer-cache.md §1.2-§1.3; #4030).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT: in the catalogue, every 6 hours, an API type whose file
  exists and declares its caps, so the tick's real loader imports it.
* THE RATIO DECIDES, NOT THE MILLISECONDS: the newest measured version's p95
  over its own run's reference at or over `ratio_threshold` opens ONE issue
  (marker, title as a fact, the window's table, the slowest answers, label);
  the same version again writes nothing; a new version over comments once;
  back under comments "under again" once; it is never closed.
* WHERE THE MEASUREMENT IS READ: promotion's `answer_latency` on the version,
  else the indexer's `latency.json` beside the manifest, and only under the
  schedule's own tenant's prefix. A version with neither is unmeasured,
  never a zero; a silence of 3 days after a measurement is reported once.
* SEVEN DAYS OF DELIVERY (#4030): fixture attempts' `graph_ready` marks are
  counted by delivery and the fallbacks by reason, per repository, under the
  tenant only, one mark per attempt, outside the window not counted; the
  served p50/p95 is held against 300 ms; the read route serves the same for
  the caller's own repository and 404s another tenant's.
* DRY RUN: the same reads, and no write.

No cloud and no emulator: the in-memory Firestore and bucket, and GitHub as a
fake transport under the real `forgewrite.GitHubWriter`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, graphreports, schedulefire, scheduletypes
from swarm_api.repositories import repo_id_for
from swarm_api.main import create_app
from swarm_api.schedtypes import graph_latency_report

from . import forge_fakes
from .conftest import auth_header, seed_task, seed_tenant
from .test_schedtypes_issue_sweep import NOW, OWNER, REPO, firing_for, make_context, make_schedule, register, tasks
from .test_schedtypes_release_health import ActionsGitHub

TYPE = "graph-latency-report"


def sha(n: int) -> str:
    return f"{n:040x}"


@pytest.fixture
def github() -> ActionsGitHub:
    return ActionsGitHub()


@pytest.fixture
def ctx(db, tokens, group_map, objects, github, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    context = make_context(db, tokens, group_map, objects, forge_tokens=forge_fakes.AnyTenantTokens())
    context.forge_writer = forgewrite.GitHubWriter(send=github.send, locate=github.locate, fetch=github.fetch)
    return context


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def latency_doc(p95: float, reference: float, *, slowest: str = "tests/conftest.py#auth_header") -> dict:
    run = {"p50_ms": p95 / 3, "p95_ms": p95, "max_ms": p95 * 1.5, "reference_ms": reference,
           "slowest": [{"tool": "context", "symbol": slowest, "ms": p95 * 1.4}]}
    # Three runs; the median by p95 is the middle one.
    return {"schema": graphreports.LATENCY_SCHEMA, "tool_version": "2", "questions": 48,
            "runs": [{**run, "p95_ms": p95 * 2}, run, {**run, "p95_ms": p95 / 2}],
            "symbols": 58930, "raw_bytes": 1, "cpu_limit": 2.0}


def version(db, objects, repo_id: str, n: int, *, hours_ago: float, p95: float | None = None,
            reference: float = 20.0, tenant_id: str = "eng", recorded: bool = False) -> str:
    """A promoted version; with `p95`, its latency.json in the bucket (or, with
    `recorded`, promotion's copy on the version document)."""
    commit = sha(n)
    doc: dict[str, Any] = {"commit_sha": commit, "recorded_at": NOW - timedelta(hours=hours_ago)}
    if p95 is not None and recorded:
        doc["answer_latency"] = latency_doc(p95, reference)
    elif p95 is not None:
        objects.put(graphreports.latency_key(tenant_id, repo_id, commit),
                    json.dumps(latency_doc(p95, reference)))
    db.docs[f"repositories/{repo_id}/index_versions/{commit}"] = doc
    return commit


def report(db, *repo_ids: str, **params: Any) -> dict[str, Any]:
    return make_schedule(db, type_=TYPE, repo_ids=repo_ids, params=params, cron="0 */6 * * *")


def fire(ctx, db, repo_id: str, **params: Any) -> list[dict[str, Any]]:
    return graph_latency_report.create(firing_for(ctx, report(db, repo_id, **params)))


# ---------------------------------------------------------------------------
# The type
# ---------------------------------------------------------------------------


def test_the_type_is_in_the_catalogue_every_six_hours_and_available() -> None:
    entry = scheduletypes.get(TYPE)
    assert entry is not None and entry.executor == "api" and entry.profile is None
    assert entry.min_interval == timedelta(hours=6)
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is graph_latency_report
    assert scheduletypes.budget_caps(entry) is graph_latency_report.BUDGET_CAPS
    params = entry.params_model.model_validate({})
    assert (params.ratio_threshold, params.window, params.issue_label) == (12.0, 7, "bug")
    with pytest.raises(ValueError):
        entry.params_model.model_validate({"threshold_ms": 300})  # no millisecond threshold (Q3)


# ---------------------------------------------------------------------------
# Deciding and writing (#1840)
# ---------------------------------------------------------------------------


def test_a_version_over_the_ratio_opens_one_issue_and_the_same_version_writes_nothing_again(
        db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=30, p95=100.0)
    version(db, objects, repo_id, 2, hours_ago=6, p95=252.0)  # 12.6x a 20 ms reference

    (item,) = fire(ctx, db, repo_id)

    (number,) = github.issues
    issue = github.issues[number]
    assert issue["title"] == (f"swarm-graph's cold answer p95 on {OWNER}/{REPO} reached 12.6x the "
                              f"reference workload (252 ms) at {sha(2)[:12]}")
    assert issue["body"].startswith(graphreports.issue_marker(repo_id))
    assert graphreports.over_marker(sha(2)) in issue["body"]
    assert "tests/conftest.py#auth_header" in issue["body"] and "| 5.0x |" in issue["body"]
    assert [label["name"] for label in issue["labels"]] == ["bug"]
    assert item["kind"] == "api_action" and item["outcome"] == "succeeded"
    assert item["detail"]["action"] == graphreports.OPEN_ISSUE and tasks(db) == []

    before = list(github.writes())
    (again,) = fire(ctx, db, repo_id)
    assert github.writes() == before and again["detail"]["action"] == graphreports.NOTHING


def test_the_milliseconds_never_decide_only_the_ratio(db, ctx, github, objects, repo_id) -> None:
    # 400 ms is over the 300 ms target, but on a slow node whose reference is 50 ms: 8x.
    version(db, objects, repo_id, 1, hours_ago=2, p95=400.0, reference=50.0)
    (item,) = fire(ctx, db, repo_id)
    assert github.issues == {} and item["detail"]["action"] == graphreports.NOTHING
    # 130 ms is well under the target, at 13x a 10 ms reference: over.
    version(db, objects, repo_id, 2, hours_ago=1, p95=130.0, reference=10.0)
    (item,) = fire(ctx, db, repo_id)
    assert len(github.issues) == 1 and item["detail"]["action"] == graphreports.OPEN_ISSUE


def test_a_new_version_over_comments_once_and_under_again_comments_once_never_closing(
        db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=30, p95=260.0)
    fire(ctx, db, repo_id)
    (number,) = github.issues

    version(db, objects, repo_id, 2, hours_ago=20, p95=280.0)
    fire(ctx, db, repo_id)
    fire(ctx, db, repo_id)
    (over,) = github.comments[number]
    assert over["body"].startswith(graphreports.over_marker(sha(2)))

    version(db, objects, repo_id, 3, hours_ago=10, p95=120.0)
    fire(ctx, db, repo_id)
    fire(ctx, db, repo_id)
    assert len(github.comments[number]) == 2
    assert github.comments[number][1]["body"].startswith(graphreports.under_marker(sha(3)))
    assert "stays open until a person closes it" in github.comments[number][1]["body"]

    # Under, and under again at a newer version: nothing more.
    version(db, objects, repo_id, 4, hours_ago=5, p95=110.0)
    fire(ctx, db, repo_id)
    assert len(github.comments[number]) == 2
    assert github.issues[number]["state"] == "open"
    assert all(method != "PATCH" for method, _path in github.writes())


def test_promotions_recorded_answer_latency_is_read_before_the_bucket(db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=2, p95=300.0, recorded=True)
    (item,) = fire(ctx, db, repo_id)
    (measured,) = item["detail"]["readings"]["measured"]
    assert measured["source"] == "index_version" and measured["ratio"] == 15.0
    assert item["detail"]["action"] == graphreports.OPEN_ISSUE


def test_an_unmeasured_version_is_never_a_zero_and_no_measurement_writes_nothing(
        db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=2)
    objects.put(graphreports.latency_key("eng", repo_id, sha(9)), "not json")
    version(db, objects, repo_id, 9, hours_ago=1)
    (item,) = fire(ctx, db, repo_id)
    readings = item["detail"]["readings"]
    assert readings["measured"] == []
    assert {u["commit_sha"]: u["cause"] for u in readings["unmeasured"]} == {
        sha(1): "not_measured", sha(9): "malformed"}
    assert item["detail"]["action"] == graphreports.NOTHING and github.writes() == []


def test_a_silence_of_three_days_after_a_measurement_is_reported_once(db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=24 * 4, p95=100.0)
    version(db, objects, repo_id, 2, hours_ago=2)  # promoted since, never measured
    (item,) = fire(ctx, db, repo_id)
    (number,) = github.issues
    assert item["detail"]["action"] == graphreports.OPEN_UNMEASURED
    assert "has not been measured since" in github.issues[number]["title"]
    assert github.issues[number]["body"].startswith(graphreports.issue_marker(repo_id))
    fire(ctx, db, repo_id)
    assert len(github.issues) == 1 and github.comments.get(number, []) == []


def test_an_idle_repository_with_an_old_measurement_and_nothing_promoted_since_writes_nothing(
        db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=24 * 10, p95=100.0)
    (item,) = fire(ctx, db, repo_id)
    assert item["detail"]["action"] == graphreports.NOTHING and github.writes() == []


def test_another_tenants_measurement_is_never_read(db, ctx, github, objects, repo_id) -> None:
    commit = version(db, objects, repo_id, 1, hours_ago=2)
    # The same repo_id's file under another tenant's prefix: not this tenant's.
    objects.put(graphreports.latency_key("research", repo_id, commit), json.dumps(latency_doc(500.0, 20.0)))
    (item,) = fire(ctx, db, repo_id)
    assert item["detail"]["readings"]["measured"] == [] and github.issues == {}


def test_a_dry_run_reads_and_writes_nothing(db, ctx, github, objects, repo_id) -> None:
    version(db, objects, repo_id, 1, hours_ago=2, p95=300.0)
    recorded: list = []
    out = graph_latency_report.dry_run(firing_for(ctx, report(db, repo_id), recorded=recorded))
    (row,) = out["repositories"]
    assert row["action"] == graphreports.OPEN_ISSUE
    assert github.writes() == [] and recorded == []


def test_an_unregistered_repository_fails_its_own_action(db, ctx, github) -> None:
    other = repo_id_for("research", OWNER, REPO)
    register(db, tenant_id="research")
    (item,) = fire(ctx, db, other)
    assert item["outcome"] == "failed"
    assert item["detail"]["code"] == graph_latency_report.REPOSITORY_NOT_REGISTERED


# ---------------------------------------------------------------------------
# Seven days of graph delivery (#4030)
# ---------------------------------------------------------------------------

STAGED_FALLBACK = ("staged the graph of 0123456789ab (current); the graph service was not "
                   "used: the probe failed (timeout)")


def task(db, task_id: str, *, tenant_id: str = "eng", repository: str = f"{OWNER}/{REPO}") -> None:
    seed_task(db, task_id=task_id, tenant_id=tenant_id, state="SUCCEEDED",
              created_at=NOW - timedelta(days=1))
    db.docs[f"tasks/{task_id}"]["repository_url"] = f"https://github.com/{repository}"


def attempt(db, attempt_id: str, task_id: str, *, tenant_id: str = "eng", hours_ago: float = 5) -> datetime:
    created = NOW - timedelta(hours=hours_ago)
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant_id, "generation": 1,
        "lease_id": f"lease_{attempt_id}", "backend": "GKE", "execution_name": None,
        "created_at": created, "started_at": created, "completed_at": created + timedelta(minutes=5),
        "exit_code": 0, "error": None, "peak_rss_bytes": None, "oom_near_miss": False, "checkpoints": [],
    }
    return created


def mark(db, task_id: str, attempt_id: str, delivery: str, reason: str, *, at: datetime,
         event_id: str | None = None, tenant_id: str = "eng") -> None:
    event_id = event_id or f"ev_{attempt_id}"
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id, "task_id": task_id, "tenant_id": tenant_id, "type": "running",
        "at": at, "attempt_id": attempt_id, "lease_id": None, "generation": 1,
        "detail": {"cause": graphreports.READY_MARK,
                   "graph": {"delivery": delivery, "seconds": 1.2, "bytes": 10, "blobs": 1,
                             "reason": reason, "history_seconds": None, "phases": {}}},
    }


def seed_delivery(db) -> None:
    task(db, "t1")
    task(db, "t2", repository="example-org/gadgets")
    for n, (delivery, reason) in enumerate([
        ("service", "pinned the graph of 0123456789ab (current) for the graph service"),
        ("staged", STAGED_FALLBACK),
        ("staged", STAGED_FALLBACK),
        ("staged", "staged the graph of 0123456789ab (current)"),  # staged by the setting
        ("staged", "staged the graph of 0123456789ab (current); the graph service was not used: "
                   "pinning failed (TimeoutError)"),
    ]):
        created = attempt(db, f"a{n}", "t1")
        mark(db, "t1", f"a{n}", delivery, reason, at=created + timedelta(seconds=30))
    created = attempt(db, "b1", "t2")
    mark(db, "t2", "b1", "staged", STAGED_FALLBACK, at=created + timedelta(seconds=30))
    # A second mark for one attempt is not counted twice.
    mark(db, "t1", "a1", "staged", STAGED_FALLBACK, at=created + timedelta(seconds=40), event_id="ev_dup")
    # Outside the window, and an attempt that never reached the graph step.
    old = attempt(db, "old", "t1", hours_ago=24 * 8)
    mark(db, "t1", "old", "staged", STAGED_FALLBACK, at=old + timedelta(seconds=30))
    attempt(db, "nomark", "t1")
    # Another tenant's attempt and mark.
    task(db, "r1", tenant_id="research")
    created = attempt(db, "ra", "r1", tenant_id="research")
    mark(db, "r1", "ra", "staged", STAGED_FALLBACK, at=created, tenant_id="research")


def test_seven_days_of_fallbacks_are_counted_by_reason_per_repository(db, ctx) -> None:
    seed_delivery(db)
    since, until = graphreports.window_until(NOW)
    out = graphreports.delivery_report(ctx.store, db, "eng", since, until)
    rows = {row["repository"]: row for row in out["repositories"]}
    widgets = rows[f"{OWNER}/{REPO}"]
    assert widgets["attempts"] == 5
    assert widgets["delivery"] == {"service": 1, "staged": 4, "none": 0}
    assert widgets["fallbacks"] == 3
    assert widgets["fallback_reasons"] == {"the probe failed (timeout)": 2,
                                           "pinning failed (TimeoutError)": 1}
    # Three of the four attempts that wanted the service fell back.
    assert widgets["fallback_rate"] == 0.75
    assert rows["example-org/gadgets"]["fallbacks"] == 1
    assert out["total"]["fallbacks"] == 4 and out["total"]["attempts"] == 6
    assert out["unmarked"] == 1 and out["truncated"] is False
    assert "research" not in json.dumps(out["repositories"])


def test_the_report_narrows_to_one_repository(db, ctx) -> None:
    seed_delivery(db)
    since, until = graphreports.window_until(NOW)
    out = graphreports.delivery_report(ctx.store, db, "eng", since, until,
                                       repository="Example-Org/Gadgets")
    assert [row["repository"] for row in out["repositories"]] == ["example-org/gadgets"]
    assert out["total"]["fallbacks"] == 1


def test_the_served_p95_is_held_against_300_ms_and_not_read_stays_not_read() -> None:
    over = graphreports.served_latency({"state": "read", "count": 900, "window_hours": 24,
                                        "p50_ms": 40.0, "p95_ms": 310.0, "exact": True})
    assert over["p95_over_target"] is True and over["target_ms"] == 300.0
    under = graphreports.served_latency({"state": "read", "p50_ms": 10.0, "p95_ms": 120.0})
    assert under["p95_over_target"] is False
    missing = graphreports.served_latency({"state": "not_read", "reason": "not_configured"})
    assert missing == {"state": "not_read", "reason": "not_configured", "target_ms": 300.0}


def test_a_firing_records_the_repositorys_delivery_beside_its_decision(db, ctx, objects, repo_id) -> None:
    seed_delivery(db)
    (item,) = fire(ctx, db, repo_id)
    delivery = item["detail"]["delivery"]
    assert delivery["total"]["fallbacks"] == 3
    assert [row["repository"] for row in delivery["repositories"]] == [f"{OWNER}/{REPO}"]
    assert item["detail"]["served_latency"]["state"] == "not_read"


def test_the_read_route_serves_the_callers_own_repository_and_404s_another_tenants(
        db, ctx, repo_id) -> None:
    seed_delivery(db)
    other = register(db, tenant_id="research", repo="hidden")
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = client.get(f"/v1/repositories/{repo_id}/graph-delivery", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repository"] == f"{OWNER}/{REPO}" and body["days"] == 7
    assert body["delivery"]["total"]["fallbacks"] == 3
    assert body["served_latency"]["state"] == "not_read"
    assert client.get(f"/v1/repositories/{other}/graph-delivery",
                      headers=auth_header("alice")).status_code == 404
    assert client.get(f"/v1/repositories/{repo_id}/graph-delivery?days=31",
                      headers=auth_header("alice")).status_code == 422


def test_a_firing_reads_the_tenants_attempts_once_for_all_its_repositories(
        db, ctx, objects, monkeypatch) -> None:
    seed_delivery(db)
    first = register(db)
    second = register(db, repo="gadgets")
    calls: list[str] = []
    real = ctx.store.page_attempts

    def counted(tenant_id: str, **kwargs: Any) -> Any:
        calls.append(tenant_id)
        return real(tenant_id, **kwargs)

    monkeypatch.setattr(ctx.store, "page_attempts", counted)
    work = graph_latency_report.create(firing_for(ctx, report(db, first, second)))
    assert calls == ["eng"]
    fallbacks = [item["detail"]["delivery"]["total"]["fallbacks"] for item in work]
    assert fallbacks == [3, 1]
