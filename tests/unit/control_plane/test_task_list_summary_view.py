"""#168 (API half): `GET /v1/tasks?view=summary` drops what a list never shows.

Every row of the list carried the task's masked `input`, `metadata` and
`result_summary`, so a 200-row poll moved megabytes the Agents list and the
Overview never draw. `view=summary` leaves the three out -- the keys ABSENT,
with their masking counts, not null -- skips the masking for them, and says
`"view": "summary"`. No `view`, or `view=full`, is the response as it was; an
unknown view is a 422 naming the allowed values. `GET /v1/tasks/{id}` is
unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.codec import SUMMARY_DROPPED_KEYS
from swarm_api.task_input import TaskMasking

from .conftest import auth_header, seed_task, seed_tenant

DROPPED = ("input", "metadata", "result_summary")


def _seed(db) -> None:
    seed_tenant(db, "eng")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(3):
        doc = seed_task(db, task_id=f"task_{i}", tenant_id="eng", state="SUCCEEDED",
                        created_at=base + timedelta(minutes=i))
        doc["input"] = {"prompt": "write the report " * 50}
        doc["metadata"] = {"ticket": f"T-{i}"}
        doc["result_summary"] = {"summary": "done", "files": ["a.py"]}


def _list(client, **params):
    return client.get("/v1/tasks", headers=auth_header("alice"), params=params)


def test_summary_rows_carry_none_of_the_three_keys(client, db):
    _seed(db)
    response = _list(client, view="summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["view"] == "summary"
    assert len(body["tasks"]) == 3
    for row in body["tasks"]:
        for key in DROPPED + tuple(f"{k}_redaction_count" for k in DROPPED):
            assert key not in row, f"{key} is in a summary row"
        # Everything else is still there.
        assert row["state"] == "SUCCEEDED"
        assert "last_error" in row and "last_error_redaction_count" in row
        assert "repository_url" in row


def test_full_and_absent_are_the_response_as_it_was(client, db):
    _seed(db)
    absent = _list(client)
    full = _list(client, view="full")
    assert absent.status_code == 200 and full.status_code == 200
    assert absent.content == full.content
    body = absent.json()
    assert "view" not in body
    for row in body["tasks"]:
        for key in DROPPED:
            assert key in row, f"{key} is missing from a full row"
        assert row["input"]["prompt"].startswith("write the report")
        assert row["metadata"]["ticket"].startswith("T-")


def test_an_unknown_view_is_a_422_naming_the_allowed_values(client, db):
    _seed(db)
    response = _list(client, view="compact")
    assert response.status_code == 422, response.text
    assert "compact" in response.text
    assert "summary" in response.text and "full" in response.text


def test_the_masking_for_dropped_fields_is_never_run(client, db, monkeypatch):
    _seed(db)
    calls: list[str] = []
    for name in ("input_value", "metadata_value", "leaves"):
        original = getattr(TaskMasking, name)

        def spy(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(TaskMasking, name, spy)

    assert _list(client, view="summary").status_code == 200
    assert calls == [], f"view=summary still ran {calls}"

    # The control: the full view does run them, so the spy can see a call.
    assert _list(client).status_code == 200
    assert {"input_value", "metadata_value", "leaves"} <= set(calls)


def test_the_single_task_read_is_unchanged(client, db):
    _seed(db)
    response = client.get("/v1/tasks/task_0", headers=auth_header("alice"),
                          params={"view": "summary"})
    assert response.status_code == 200, response.text
    for key in DROPPED:
        assert key in response.json()["task"]


@pytest.mark.parametrize("key", SUMMARY_DROPPED_KEYS)
def test_the_dropped_set_is_the_three_fields_and_their_counts(key):
    assert key.removesuffix("_redaction_count") in DROPPED
