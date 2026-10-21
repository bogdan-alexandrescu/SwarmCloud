"""Both attempt routes serve the size the scheduler rendered the attempt at (RS-4; part of #1689).

Contract request 111 (RS-4) has the scheduler pin `size_version`, the
`sized_*` numbers and a live pod's `sized_live_split` on the attempt, and
`attempt_from_dict` reads them back. Details (AgentDetail.tsx `sizedCpuWords`)
reads an attempt sized 4 / 8 as "4 cores guaranteed, bursts to 8" from those
fields -- so an API that decodes them and does not serve them makes that line
unreachable in every deployed environment, while a UI test that puts the
fields in its fixture row still passes (the SIZES-DETAIL review).

What is pinned: both routes serve the six fields with the stored values; a
live pod's split is served as its two named containers; an attempt from before
RS-4 serves nulls, never the class default dressed as a record.

MUTATIONS: drop any of the six from `attempt_to_api`; serve
`sized_cpu_request` under `sized_cpu_limit`; fill a missing record from the
class catalogue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import auth_header, seed_task, seed_tenant

SIZED = ("size_version", "sized_cpu_request", "sized_cpu_limit", "sized_memory_gib",
         "sized_disk_gib", "sized_live_split")


def a_task(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")


def an_attempt(db, attempt_id, **fields):
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "GKE_AUTOPILOT",
        "created_at": started, "started_at": started + timedelta(seconds=10),
        "completed_at": None, "exit_code": None, "checkpoints": [],
        **fields,
    }


ROUTES = ("/v1/tasks/task_a/attempts", "/v1/attempts")


@pytest.mark.parametrize("route", ROUTES)
def test_an_attempt_sized_4_over_8_is_served_as_4_over_8(client, db, route):
    a_task(db)
    an_attempt(db, "att_1", size_version=2, sized_cpu_request=4.0, sized_cpu_limit=8.0,
               sized_memory_gib=16, sized_disk_gib=40, sized_live_split=None)

    response = client.get(route, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    (row,) = response.json()["attempts"]
    # MUTATION: drop `size_version` from attempt_to_api -> KeyError here.
    assert row["size_version"] == 2
    # MUTATION: drop `sized_cpu_request` / `sized_cpu_limit` from attempt_to_api,
    # or serve one under the other's key -> red here.
    assert row["sized_cpu_request"] == 4.0
    assert row["sized_cpu_limit"] == 8.0
    # MUTATION: drop `sized_memory_gib` / `sized_disk_gib` from attempt_to_api -> red here.
    assert row["sized_memory_gib"] == 16
    assert row["sized_disk_gib"] == 40
    # MUTATION: drop `sized_live_split` from attempt_to_api -> KeyError here.
    assert row["sized_live_split"] is None


@pytest.mark.parametrize("route", ROUTES)
def test_a_live_pods_split_is_served_as_its_two_named_containers(client, db, route):
    a_task(db)
    split = {
        "worker": {"cpu_request": 2.0, "cpu_limit": 4.0, "memory_gib": 6.0},
        "display": {"cpu_request": 2.0, "cpu_limit": 3.0, "memory_gib": 2.0},
    }
    an_attempt(db, "att_live", size_version=1, sized_cpu_request=4.0, sized_cpu_limit=7.0,
               sized_memory_gib=8, sized_disk_gib=40, sized_live_split=split)

    (row,) = client.get(route, headers=auth_header("alice")).json()["attempts"]
    # MUTATION: drop `sized_live_split` from attempt_to_api -> KeyError here.
    assert row["sized_live_split"] == split


@pytest.mark.parametrize("route", ROUTES)
def test_an_attempt_from_before_rs4_serves_nulls_not_a_guess(client, db, route):
    a_task(db)
    an_attempt(db, "att_old")

    (row,) = client.get(route, headers=auth_header("alice")).json()["attempts"]
    # MUTATION: fill a missing record from the class catalogue -> red here;
    # drop any of the six from attempt_to_api -> KeyError here.
    assert {name: row[name] for name in SIZED} == {name: None for name in SIZED}
