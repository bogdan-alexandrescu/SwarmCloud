"""#172: the checkpoint and log reads serve each object's own time.

GCS reports an `updated` time for every object it lists. The inspector's
checkpoint and log tables have an Age column (AG-23 on #82), and before #172
the checkpoint listing dropped that time on the floor: an object was
`{name, key, bytes}`, so the panel could only print "not measured" beside a
manifest and an archive the bucket had dated exactly. These tests pin the time
onto every place the inspector reads an object from:

  * each object of `GET /v1/tasks/{id}/checkpoints`;
  * the archive of `GET /v1/tasks/{id}/checkpoints/{c}/files`;
  * each stream of `GET /v1/tasks/{id}/logs`, final and live (served since
    #184; pinned here beside the other two because the inspector reads all
    three the same way).

The rule is the one `_ages` already states for a log: NULL when the store
reported no time, never a guess -- not the checkpoint's `created_at`, not the
attempt's end, not the read time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_task, seed_tenant
from .test_checkpoint_content import put_checkpoint, seed, tar_gz
from .test_checkpoint_read_path import _attempt as ckpt_attempt
from .test_checkpoint_read_path import a_task_with_one_checkpoint, ckpt_prefix, write_checkpoint
from .test_log_agent_streams import _attempt as log_attempt
from .test_log_agent_streams import final_key, live_key, stream_of

ARCHIVE_AT = datetime(2026, 9, 20, 11, 0, 3, tzinfo=timezone.utc)
MANIFEST_AT = datetime(2026, 9, 20, 11, 0, 5, tzinfo=timezone.utc)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _listing(client, user="alice"):
    return client.get("/v1/tasks/task_a/checkpoints", headers=auth_header(user))


# --------------------------------------------------------------------------
# The checkpoint listing
# --------------------------------------------------------------------------

def test_each_checkpoint_object_carries_the_time_the_bucket_reported(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    ckpt_attempt(db, "att_1", "eng", "task_a", minutes_ago=30)
    prefix = ckpt_prefix("eng", "task_a", "att_1", "ckpt-00001")
    write_checkpoint(objects)
    # Re-stamp both objects with distinct times, as GCS would after the
    # archive-then-manifest upload order.
    objects.updated_at[f"{prefix}/archive.tar.gz"] = ARCHIVE_AT
    objects.updated_at[f"{prefix}/manifest.json"] = MANIFEST_AT

    response = _listing(client)
    assert response.status_code == 200, response.text
    row = response.json()["checkpoints"][0]
    times = {o["name"]: o["object_updated_at"] for o in row["objects"]}
    assert set(times) == {"archive.tar.gz", "manifest.json"}
    assert _parse(times["archive.tar.gz"]) == ARCHIVE_AT
    assert _parse(times["manifest.json"]) == MANIFEST_AT


def test_a_checkpoint_object_with_no_reported_time_is_null_not_a_guess(client, db, objects):
    """The in-memory store records no time unless one is given -- the shape of
    a store that did not report one. The checkpoint's own `created_at` is
    right there in the manifest, and it must NOT stand in for the object's."""
    a_task_with_one_checkpoint(db, objects)

    row = _listing(client).json()["checkpoints"][0]
    assert row["created_at"] == "2026-09-20T11:00:00Z"
    assert len(row["objects"]) == 2
    for obj in row["objects"]:
        assert "object_updated_at" in obj, obj
        assert obj["object_updated_at"] is None, obj


def test_an_uncommitted_checkpoint_still_dates_the_archive_it_left(client, db, objects):
    """No manifest means no `created_at`; the archive's own time is then the
    only time there is, and it is served."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    ckpt_attempt(db, "att_1", "eng", "task_a", minutes_ago=30)
    prefix = write_checkpoint(objects, manifest=False)
    objects.updated_at[f"{prefix}/archive.tar.gz"] = ARCHIVE_AT

    row = _listing(client).json()["checkpoints"][0]
    assert row["manifest"] == "absent"
    assert row["created_at"] is None
    (only,) = row["objects"]
    assert _parse(only["object_updated_at"]) == ARCHIVE_AT


# --------------------------------------------------------------------------
# The checkpoint's file listing
# --------------------------------------------------------------------------

def _files(client, user="alice"):
    return client.get(
        "/v1/tasks/task_a/checkpoints/ckpt-00001/files", headers=auth_header(user)
    )


def test_the_file_listing_dates_its_archive(client, db, objects):
    seed(db)
    base = put_checkpoint(objects, archive=tar_gz([("a.txt", "file", "a\n")]), file_count=1)
    objects.updated_at[f"{base}/archive.tar.gz"] = ARCHIVE_AT

    response = _files(client)
    assert response.status_code == 200, response.text
    archive = response.json()["archive"]
    assert _parse(archive["object_updated_at"]) == ARCHIVE_AT


def test_the_file_listing_s_archive_time_is_null_when_unreported_or_absent(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("a.txt", "file", "a\n")]), file_count=1)
    assert _files(client).json()["archive"]["object_updated_at"] is None

    # No archive object at all: no time either, and still the key, not a gap.
    put_checkpoint(objects, checkpoint="ckpt-00002", archive=None)
    body = client.get(
        "/v1/tasks/task_a/checkpoints/ckpt-00002/files", headers=auth_header("alice")
    ).json()
    assert body["status"] == "absent"
    assert body["archive"]["object_updated_at"] is None


# --------------------------------------------------------------------------
# The log read
# --------------------------------------------------------------------------

def _logs(client, query=""):
    return client.get(f"/v1/tasks/task_a/logs{query}", headers=auth_header("alice"))


def test_a_final_stream_is_dated_by_its_object_not_by_its_attempt_s_end(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    log_attempt(db, "att_1", minutes_ago=90, completed=True)
    uploaded = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=3)
    objects.put(final_key("stdout"), "runner out\n", updated=uploaded)

    body = _logs(client).json()
    out = stream_of(body, "stdout")
    assert (out["status"], out["source"]) == ("ok", "final")
    assert _parse(out["object_updated_at"]) == uploaded
    assert out["object_updated_at"] != body["attempt"]["completed_at"]
    assert out["age_seconds"] == round((_parse(body["read_at"]) - uploaded).total_seconds(), 3)


def test_a_live_tail_is_dated_by_its_object(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    log_attempt(db, "att_1", minutes_ago=2)
    published = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=11)
    objects.put(live_key("stdout"), "#swarm-tail offset=0 size=4\nabc\n", updated=published)

    out = stream_of(_logs(client).json(), "stdout")
    assert (out["status"], out["source"]) == ("ok", "live")
    assert _parse(out["object_updated_at"]) == published
    assert out["age_seconds"] >= 11


def test_a_stream_that_was_not_read_has_no_time(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    log_attempt(db, "att_1", minutes_ago=2)

    for out in _logs(client).json()["streams"]:
        assert out["status"] == "absent", out
        assert out["object_updated_at"] is None
        assert out["age_seconds"] is None
