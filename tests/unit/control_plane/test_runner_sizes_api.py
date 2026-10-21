"""Admin → Runner sizes: the API (SIZES-API, part of #1689).

docs/design/runner-sizes.md §4.3 (the two Firestore shapes), §4.5 (the
routes) and §6's SIZES-API entry, whose red-first list this file is. Owner
decisions of 2026-10-20 that it holds: any platform admin may change sizes,
with a reason, every version audited and restorable (Q4); `disk_gib` is shown,
never edited (Q5); a memory cut below the class's 7-day peak needs a typed
confirmation (Q6).

Every version is a whole table, written once and never updated; the pointer
`control/runner_sizes` names the current one. A stale `base_version` is a 409
and writes nothing; a size outside the frozen bounds (`check_size`) is a 422
naming every refusal at once.

Offline: FakeFirestore, StaticTokenVerifier. No credentials, no emulator.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_common.profiles import RESOURCE_CLASSES, default_sizes

from .conftest import auth_header, seed_pool, seed_task

ROUTE = "/v1/admin/runner-sizes"
POINTER = "control/runner_sizes"
VERSIONS = "runner_size_versions/"
GIB = 1024 ** 3
NOW = datetime.now(timezone.utc)


def _put(client, body: dict[str, Any], who: str = "root"):
    payload = {"reason": "long runs pinned at the limit", **body}
    return client.put(ROUTE, headers=auth_header(who), json=payload)


def _versions(db) -> dict[str, dict[str, Any]]:
    return {path: copy.deepcopy(doc) for path, doc in db.docs.items() if path.startswith(VERSIONS)}


def _fields(response) -> set[tuple[str, str]]:
    return {(r["class"], r["field"]) for r in response.json()["detail"]["refusals"]}


def seed_attempt(
    db,
    *,
    resource_class: str = "standard",
    runner_profile: str = "claude-code",
    days_ago: float = 1.0,
    minutes: float = 30.0,
    peak_gib: float | None = 3.0,
    mean_cpu: float | None = 2.0,
    cpu_limit: float | None = 8.0,
    size_version: int | None = None,
    backend: str = "GKE_AUTOPILOT",
    oom_near_miss: bool = False,
) -> tuple[str, str]:
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    attempt_id = f"att_{uuid.uuid4().hex[:12]}"
    started = NOW - timedelta(days=days_ago)
    seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED",
              runner_profile=runner_profile, resource_class=resource_class, created_at=started)
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": "eng",
        "generation": 1,
        "lease_id": f"lease_{attempt_id}",
        "backend": backend,
        "created_at": started,
        "started_at": started,
        "completed_at": started + timedelta(minutes=minutes),
        "peak_rss_bytes": None if peak_gib is None else int(peak_gib * GIB),
        "mean_cpu_cores": mean_cpu,
        "cpu_limit_cores": cpu_limit,
        "oom_near_miss": oom_near_miss,
        "size_version": size_version,
    }
    return task_id, attempt_id


def seed_beat(db, task_id: str, attempt_id: str, points: list[tuple[float, float]]) -> None:
    """One HEARTBEAT carrying LIVE-UTIL points, each (cpu cores, throttled share)."""
    at = NOW - timedelta(hours=1)
    series = [{"t": float(i * 5), "cpu": cpu, "mem": 2 * GIB, "thr": thr}
              for i, (cpu, thr) in enumerate(points)]
    db.docs[f"tasks/{task_id}/events/{uuid.uuid4().hex}"] = {
        "event_id": uuid.uuid4().hex,
        "task_id": task_id,
        "tenant_id": "eng",
        "type": "heartbeat",
        "at": at,
        "attempt_id": attempt_id,
        "generation": 1,
        "detail": {"series": series, "series_clock": float(len(points) * 5)},
    }


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def test_with_no_version_written_the_route_serves_the_code_defaults(client):
    body = client.get(ROUTE, headers=auth_header("root")).json()
    # MUTATION: serving a pointer-less table as version 1 (or None) turns this red.
    assert body["current_version"] == 0
    for name, size in default_sizes().items():
        served = body["classes"][name]
        # MUTATION: reading `cpu_limit` from `rc.cpu` (the request) turns this red.
        assert (served["cpu_request"], served["cpu_limit"], served["memory_gib"],
                served["disk_gib"], served["units"]) == (
            size.cpu_request, size.cpu_limit, size.memory_gib, size.disk_gib, size.units)
        assert body["defaults"][name] == served
    # MUTATION: dropping the browser class's live split from the served shape turns this red.
    assert body["classes"]["browser"]["live_split"]["display"] == {
        "cpu_request": 2, "cpu_limit": 3, "memory_gib": 5}
    # MUTATION: restating the bound instead of reading CPU_BURST_MAX_RATIO turns this red if it moves.
    assert body["bounds"]["cpu_burst_max_ratio"] == 2
    # MUTATION: listing a Cloud Run profile as following a resize turns this red (owner Q3).
    assert "mock" in body["followers"]["standard"]["fixed"]
    assert "claude-code" in body["followers"]["standard"]["follows"]
    # MUTATION: listing the control-plane `await-input` profile anywhere turns this red.
    assert all("await-input" not in group["follows"] + group["fixed"]
               for group in body["followers"].values())


def test_a_malformed_stored_version_reads_as_the_code_defaults(client, db):
    db.docs[POINTER] = {"current_version": 1, "updated_by": "someone@example.com"}
    bad = {name: {"cpu_request": s.cpu_request, "cpu_limit": s.cpu_limit,
                  "memory_gib": s.memory_gib, "disk_gib": s.disk_gib}
           for name, s in default_sizes().items()}
    bad["standard"]["cpu_limit"] = 40   # 10 x the request: outside the frozen bound
    db.docs[f"{VERSIONS}000001"] = {"version": 1, "classes": bad}

    body = client.get(ROUTE, headers=auth_header("root")).json()
    # MUTATION: serving the stored table without re-running check_size turns this red.
    assert body["classes"]["standard"]["cpu_limit"] == default_sizes()["standard"].cpu_limit
    # MUTATION: hiding that the stored version was refused turns this red.
    assert body["malformed"] is True
    # The pointer's number is still served, so an admin can write over it.
    assert body["current_version"] == 1

    db.docs[f"{VERSIONS}000001"] = {"version": 1, "classes": "not a table"}
    body = client.get(ROUTE, headers=auth_header("root")).json()
    # MUTATION: letting a non-mapping `classes` raise (a 500) instead of defaulting turns this red.
    assert body["classes"]["standard"]["cpu_limit"] == default_sizes()["standard"].cpu_limit


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_a_put_creates_version_n_plus_one_and_never_updates_version_n(client, db):
    first = _put(client, {"base_version": 0, "classes": {"standard": {"cpu_limit": 6}}})
    assert first.status_code == 200, first.text
    # MUTATION: numbering the first version 0 (rewriting the defaults) turns this red.
    assert first.json()["current_version"] == 1
    v1 = copy.deepcopy(db.docs[f"{VERSIONS}000001"])
    assert v1["previous_version"] == 0
    assert v1["created_by"] == "root@example.com"
    assert v1["reason"] == "long runs pinned at the limit"
    # MUTATION: writing a patch instead of the whole table turns this red.
    assert set(v1["classes"]) == set(RESOURCE_CLASSES)
    assert v1["classes"]["standard"]["cpu_limit"] == 6
    # MUTATION: dropping the derived units from the stored version turns this red.
    assert v1["classes"]["standard"]["units"] == 1
    assert v1["changed"] == [{"class": "standard", "field": "cpu_limit", "from": 8, "to": 6}]

    second = _put(client, {"base_version": 1, "classes": {"standard": {"cpu_limit": 7}}})
    assert second.status_code == 200, second.text
    # MUTATION: updating the current version document in place turns this red.
    assert db.docs[f"{VERSIONS}000001"] == v1
    v2 = db.docs[f"{VERSIONS}000002"]
    assert (v2["version"], v2["previous_version"]) == (2, 1)
    # A class or field the body does not name keeps its value.
    assert v2["classes"]["browser"] == v1["classes"]["browser"]
    assert v2["classes"]["standard"]["memory_gib"] == 8
    # MUTATION: leaving the pointer at the old version turns this red.
    assert db.docs[POINTER]["current_version"] == 2
    assert db.docs[POINTER]["updated_by"] == "root@example.com"
    assert client.get(ROUTE, headers=auth_header("root")).json()["classes"]["standard"]["cpu_limit"] == 7


def test_a_stale_base_version_is_409_and_writes_nothing(client, db):
    assert _put(client, {"base_version": 0, "classes": {"standard": {"cpu_limit": 6}}}).status_code == 200
    before = _versions(db)
    pointer = copy.deepcopy(db.docs[POINTER])

    stale = _put(client, {"base_version": 0, "classes": {"standard": {"cpu_limit": 7}}})
    # MUTATION: skipping the base_version comparison (last writer wins) turns this red.
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_base_version"
    assert stale.json()["detail"]["current_version"] == 1
    # MUTATION: writing the version before checking the pointer turns this red.
    assert _versions(db) == before
    assert db.docs[POINTER] == pointer


def test_an_out_of_bounds_put_is_422_naming_every_refusal(client, db):
    response = _put(client, {"base_version": 0,
                             "classes": {"standard": {"cpu_limit": 12, "memory_gib": 40}}})
    # MUTATION: storing a size without the frozen check_size turns this red.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "size_out_of_bounds"
    fields = _fields(response)
    # MUTATION: returning only the first refusal turns this red.
    assert {("standard", "cpu_limit"), ("standard", "memory_gib")} <= fields
    refusal = next(r for r in response.json()["detail"]["refusals"] if r["field"] == "cpu_limit")
    assert refusal["value"] == 12 and "2 x cpu_request" in refusal["bound"]
    # MUTATION: writing the refused table anyway turns this red.
    assert _versions(db) == {} and POINTER not in db.docs


@pytest.mark.parametrize("field,value", [("units", 2), ("disk_gib", 3), ("image", "x")])
def test_units_disk_and_unknown_fields_in_the_body_are_refused(client, db, field, value):
    response = _put(client, {"base_version": 0, "classes": {"standard": {field: value}}})
    # MUTATION: accepting `units` from the body (it is derived), `disk_gib` (owner Q5:
    # shown, not edited) or a field that is not a size turns this red.
    assert response.status_code == 422, response.text
    assert ("standard", field) in _fields(response)
    assert _versions(db) == {}


def test_an_unknown_class_is_refused(client, db):
    response = _put(client, {"base_version": 0, "classes": {"huge": {"cpu_request": 8}}})
    # MUTATION: letting an admin create a class turns this red (an admin resizes, never creates).
    assert response.status_code == 422
    assert ("huge", "resource_class") in _fields(response)


def test_a_reason_is_required(client, db):
    response = client.put(ROUTE, headers=auth_header("root"),
                          json={"base_version": 0, "classes": {"standard": {"cpu_limit": 6}}})
    # MUTATION: making `reason` optional turns this red (owner Q4: a reason is required).
    assert response.status_code == 422
    blank = _put(client, {"base_version": 0, "reason": "", "classes": {"standard": {"cpu_limit": 6}}})
    assert blank.status_code == 422
    assert _versions(db) == {}


def test_restore_writes_a_new_version_with_the_old_classes(client, db):
    assert _put(client, {"base_version": 0, "classes": {"standard": {"cpu_limit": 6}}}).status_code == 200
    assert _put(client, {"base_version": 1, "classes": {"standard": {"cpu_limit": 7}}}).status_code == 200
    v1 = copy.deepcopy(db.docs[f"{VERSIONS}000001"])
    v2 = copy.deepcopy(db.docs[f"{VERSIONS}000002"])

    restored = client.post(f"{ROUTE}/restore", headers=auth_header("root"),
                           json={"base_version": 2, "version": 1, "reason": "undo"})
    assert restored.status_code == 200, restored.text
    # MUTATION: rewinding the pointer to 1 instead of writing version 3 turns this red.
    assert db.docs[POINTER]["current_version"] == 3
    v3 = db.docs[f"{VERSIONS}000003"]
    assert v3["restored_from"] == 1 and v3["previous_version"] == 2
    # MUTATION: restoring from the wrong version turns this red.
    assert v3["classes"] == v1["classes"]
    assert v3["changed"] == [{"class": "standard", "field": "cpu_limit", "from": 7, "to": 6}]
    assert db.docs[f"{VERSIONS}000001"] == v1 and db.docs[f"{VERSIONS}000002"] == v2

    to_defaults = client.post(f"{ROUTE}/restore", headers=auth_header("root"),
                              json={"base_version": 3, "version": 0, "reason": "back to code"})
    assert to_defaults.status_code == 200, to_defaults.text
    # MUTATION: refusing version 0, the code defaults, as a restore target turns this red.
    assert db.docs[f"{VERSIONS}000004"]["classes"]["standard"]["cpu_limit"] == 8

    stale = client.post(f"{ROUTE}/restore", headers=auth_header("root"),
                        json={"base_version": 3, "version": 1, "reason": "again"})
    # MUTATION: skipping the base_version check on restore turns this red.
    assert stale.status_code == 409
    missing = client.post(f"{ROUTE}/restore", headers=auth_header("root"),
                          json={"base_version": 4, "version": 9, "reason": "nope"})
    # MUTATION: restoring a version that does not exist as the defaults turns this red.
    assert missing.status_code == 404
    assert f"{VERSIONS}000005" not in db.docs


def test_versions_lists_the_audit_newest_first(client, db):
    for base, limit in ((0, 6), (1, 7), (2, 5)):
        assert _put(client, {"base_version": base, "classes": {"standard": {"cpu_limit": limit}}}).status_code == 200
    body = client.get(f"{ROUTE}/versions", headers=auth_header("root")).json()
    # MUTATION: ordering oldest first turns this red.
    assert [v["version"] for v in body["versions"]] == [3, 2, 1]
    assert body["versions"][0]["created_by"] == "root@example.com"
    assert body["versions"][0]["changed"] == [{"class": "standard", "field": "cpu_limit", "from": 7, "to": 5}]
    older = client.get(f"{ROUTE}/versions?before=3&limit=1", headers=auth_header("root")).json()
    # MUTATION: ignoring `before` or `limit` turns this red.
    assert [v["version"] for v in older["versions"]] == [2]


# --------------------------------------------------------------------------
# A memory cut below what the class used (owner Q6)
# --------------------------------------------------------------------------


def test_a_memory_cut_below_the_recent_peak_needs_the_confirmation(client, db):
    seed_attempt(db, peak_gib=6.5, days_ago=2)
    seed_attempt(db, peak_gib=7.5, days_ago=9)   # outside the 7-day window

    cut = _put(client, {"base_version": 0, "classes": {"standard": {"memory_gib": 6}}})
    # MUTATION: no peak check (owner Q6 C) turns this red.
    assert cut.status_code == 422, cut.text
    assert cut.json()["code"] == "below_recent_peak"
    detail = cut.json()["detail"]["classes"]["standard"]
    assert detail["memory_gib"] == 6
    # MUTATION: reading attempts older than 7 days (7.5) turns this red.
    assert detail["memory_peak_gib_max"] == pytest.approx(6.5)
    assert _versions(db) == {}

    # MUTATION: counting the 9-day-old 7.5 GiB peak refuses this cut and turns it red.
    above = _put(client, {"base_version": 0, "classes": {"standard": {"memory_gib": 7}}})
    assert above.status_code == 200, above.text

    confirmed = _put(client, {"base_version": 1, "classes": {"standard": {"memory_gib": 6}},
                              "confirm_below_recent_peak": True})
    # MUTATION: refusing the cut outright even when confirmed (owner Q6 B) turns this red.
    assert confirmed.status_code == 200, confirmed.text


def test_a_cloud_run_attempts_peak_does_not_gate_a_resize(client, db):
    # `mock` runs on Cloud Run at the code default whatever the version says
    # (owner Q3), so its peak says nothing about the size being cut.
    seed_attempt(db, runner_profile="mock", backend="CLOUD_RUN_JOB", peak_gib=7.0)
    # MUTATION: counting Cloud Run attempts in the class's recent use turns this red.
    assert _put(client, {"base_version": 0, "classes": {"standard": {"memory_gib": 6}}}).status_code == 200


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def _preview(client, classes: dict[str, Any]):
    return client.post(f"{ROUTE}:preview", headers=auth_header("root"), json={"classes": classes})


def test_preview_prices_units_and_pools_and_writes_nothing(client, db):
    seed_pool(db, "global", hard_limit=40)
    seed_pool(db, "resource:browser", hard_limit=10)
    response = _preview(client, {
        "standard": {"memory_gib": 16},
        "browser": {"memory_gib": 16, "live_split": {"worker": {"memory_gib": 11}}},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True and body["refusals"] == []
    standard = body["classes"]["standard"]
    # MUTATION: reading units from the code table instead of units_for turns this red.
    assert (standard["units_before"], standard["units_after"]) == (1, 2)
    # MUTATION: pricing the limit instead of the request turns this red ($0.0445/vCPU-h, $0.0049225/GiB-h).
    assert standard["cost_hour_before"] == pytest.approx(4 * 0.0445 + 8 * 0.0049225, abs=1e-4)
    assert standard["cost_hour_after"] == pytest.approx(4 * 0.0445 + 16 * 0.0049225, abs=1e-4)
    pools = {p["pool"]: p for p in body["pools"]}
    # MUTATION: dividing the pool's hard_limit by the old units after the change turns this red.
    assert pools["global"]["admits_before"]["standard"] == 40
    assert pools["global"]["admits_after"]["standard"] == 20
    # MUTATION: counting another class against a `resource:<class>` pool turns this red.
    assert set(pools["resource:browser"]["admits_after"]) == {"browser"}
    assert pools["resource:browser"]["admits_after"]["browser"] == 5
    # MUTATION: writing on preview turns this red.
    assert _versions(db) == {} and POINTER not in db.docs


def test_preview_of_an_out_of_bounds_size_reports_every_refusal(client, db):
    body = _preview(client, {"standard": {"cpu_limit": 12, "cpu_request": 0.3}}).json()
    # MUTATION: a preview that 422s instead of answering `valid: false` turns this red.
    assert body["valid"] is False
    fields = {(r["class"], r["field"]) for r in body["refusals"]}
    assert {("standard", "cpu_limit"), ("standard", "cpu_request")} <= fields
    # MUTATION: deriving units from a refused size turns this red.
    assert body["classes"]["standard"]["units_after"] is None


def test_preview_reports_the_shares_from_the_live_util_points(client, db):
    # A 60-minute run with four points, at a measured limit of 8 and the
    # default request of 4: idle (0.2), above the request (4.5, 8.0, 7.9),
    # at the limit (>= 95% of 8: 8.0, 7.9), throttled 0, 0, 0.5, 0.3.
    task_a, att_a = seed_attempt(db, minutes=60, mean_cpu=3.0, peak_gib=5.0)
    seed_beat(db, task_a, att_a, [(0.2, 0.0), (4.5, 0.0), (8.0, 0.5), (7.9, 0.3)])
    # A 10-minute run admitted at size version 1, with no LIVE-UTIL points.
    seed_attempt(db, minutes=10, mean_cpu=2.0, peak_gib=6.0, size_version=1, oom_near_miss=True)

    use = _preview(client, {"standard": {"cpu_limit": 7}}).json()["recent_use"]["standard"]
    # MUTATION: dropping attempts with no points from the count turns this red.
    assert use["attempts"] == 2
    assert use["memory_peak_gib_max"] == pytest.approx(6.0)
    assert use["cpu_mean_cores_median"] == pytest.approx(2.5)
    assert use["oom_near_miss"] == 1
    # MUTATION: computing a share over attempts instead of points turns these red.
    assert use["share_idle"] == pytest.approx(0.25)
    assert use["share_above_request"] == pytest.approx(0.75)
    assert use["share_at_limit"] == pytest.approx(0.5)
    # MUTATION: counting the 10-minute run as long (>= 40 min) changes nothing here,
    # but reading long runs from every attempt with no points would null it: red.
    assert use["share_at_limit_long"] == pytest.approx(0.5)
    assert use["throttled_share"] == pytest.approx(0.2)
    by_version = {row["size_version"]: row for row in use["by_size_version"]}
    # MUTATION: not splitting by the version each attempt was admitted at turns this red.
    assert set(by_version) == {0, 1}
    assert by_version[0]["attempts"] == 1 and by_version[1]["attempts"] == 1
    assert by_version[0]["wall_minutes_median"] == pytest.approx(60.0)
    assert by_version[1]["wall_minutes_median"] == pytest.approx(10.0)


def test_a_share_with_no_live_util_records_is_null_never_zero(client, db):
    seed_attempt(db, minutes=50, mean_cpu=1.5, peak_gib=4.0)   # no heartbeat points
    use = _preview(client, {"standard": {"cpu_limit": 7}}).json()["recent_use"]["standard"]
    assert use["attempts"] == 1
    # MUTATION: serving 0.0 where nothing was measured turns these red.
    for share in ("share_idle", "share_above_request", "share_at_limit",
                  "share_at_limit_long", "throttled_share"):
        assert use[share] is None, share
    assert use["memory_peak_gib_max"] == pytest.approx(4.0)


def test_a_class_with_no_attempts_reads_null(client, db):
    use = _preview(client, {"large": {"cpu_limit": 12}}).json()["recent_use"]["large"]
    # MUTATION: a 0 GiB peak for a class nobody ran turns this red.
    assert use["attempts"] == 0
    assert use["memory_peak_gib_max"] is None and use["cpu_mean_cores_median"] is None


# --------------------------------------------------------------------------
# Who may
# --------------------------------------------------------------------------

ROUTES = [
    ("GET", ROUTE, None),
    ("GET", f"{ROUTE}/versions", None),
    ("PUT", ROUTE, {"base_version": 0, "reason": "r", "classes": {"standard": {"cpu_limit": 6}}}),
    ("POST", f"{ROUTE}:preview", {"classes": {"standard": {"cpu_limit": 6}}}),
    ("POST", f"{ROUTE}/restore", {"base_version": 0, "version": 0, "reason": "r"}),
]


@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_a_non_admin_gets_403_on_every_route(client, db, method, path, body):
    response = client.request(method, path, headers=auth_header("alice"), json=body)
    # MUTATION: declaring `current_auth` instead of `admin_auth` on any route turns this red.
    assert response.status_code == 403, response.text
    assert _versions(db) == {} and POINTER not in db.docs


def test_a_write_counts_as_an_admin_action(client, api_context):
    counter = api_context.metrics.admin_actions.labels(action="runner_sizes")
    before = counter._value.get()
    assert _put(client, {"base_version": 0, "classes": {"standard": {"cpu_limit": 6}}}).status_code == 200
    # MUTATION: not incrementing admin_actions{action="runner_sizes"} turns this red.
    assert counter._value.get() == before + 1
