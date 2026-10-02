"""U26: a missing control document reads as UNKNOWN, not as a resumed switch.

`Store.get_control` returns `dispatch_paused: False` when `control/dispatch`
does not exist -- correctly, because the scheduler dispatches in that case
(`scheduler/store.py` `dispatch_paused()`). But False alone cannot be drawn as
the specified `UNKNOWN - no control document` state, so both `GET /v1/stats`
and `GET /v1/admin/dispatch` add `dispatch_state` and `control_document`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api.store import CONTROL, CONTROL_DOC

from .conftest import auth_header, seed_tenant


def _control(db, *, paused: bool) -> None:
    db.docs[f"{CONTROL}/{CONTROL_DOC}"] = {
        "dispatch_paused": paused,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": "root@saga.xyz",
        "reason": "test",
    }


def _both(client) -> list[dict]:
    stats = client.get("/v1/stats", headers=auth_header("alice"))
    admin = client.get("/v1/admin/dispatch", headers=auth_header("root"))
    assert stats.status_code == 200, stats.text
    assert admin.status_code == 200, admin.text
    return [stats.json(), admin.json()]


def test_a_missing_document_is_unknown_and_missing(client, db):
    seed_tenant(db, "eng")
    assert f"{CONTROL}/{CONTROL_DOC}" not in db.docs
    for body in _both(client):
        assert body["dispatch_state"] == "unknown"
        assert body["control_document"] == "missing"
        # Unchanged: the scheduler dispatches with no document.
        assert body["dispatch_paused"] is False


@pytest.mark.parametrize(("paused", "state"), [(False, "running"), (True, "paused")])
def test_a_present_document_says_running_or_paused(client, db, paused, state):
    seed_tenant(db, "eng")
    _control(db, paused=paused)
    for body in _both(client):
        assert body["dispatch_state"] == state
        assert body["control_document"] == "present"
        assert body["dispatch_paused"] is paused


def test_pausing_through_the_route_reads_back_paused(client, db):
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/admin/dispatch/pause", headers=auth_header("root"), json={"reason": "drill"}
    )
    assert response.status_code == 200, response.text
    for body in _both(client):
        assert body["dispatch_state"] == "paused"
        assert body["control_document"] == "present"
