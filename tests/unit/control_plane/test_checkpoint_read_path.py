"""`GET /v1/tasks/{id}/checkpoints` -- a seam that did not exist.

The worker has written checkpoints into the artifact bucket since the first
attempt ever ran, and nothing served them. "Which checkpoint would a retry
resume from, and is it still there" was answerable only by someone with a
gcloud session and the key layout memorised.

These tests drive the real route through the real service against the real
in-memory object reader. Four properties are pinned, and every one of them is a
bug this repository has shipped in a different component within the last week:

  * a failed LISTING is a failed listing, never `{"checkpoints": []}` with a 200;
  * a checkpoint that was never committed is ABSENT and provably unresumable,
    which is a different answer from one whose manifest could not be read;
  * `resumable` is null rather than false when nothing could be established;
  * no parameter -- task id, attempt id, page token -- reaches another tenant's
    prefix.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from swarm_api.inspect import parse_checkpoint_key, pointer_to_prefix

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Seeding, in the exact shapes the worker writes
# --------------------------------------------------------------------------

def _attempt(db, attempt_id, tenant, task_id, *, generation=1, minutes_ago=0, exit_code=None):
    created = NOW - timedelta(minutes=minutes_ago)
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": f"swarm-job-{tenant}-mock-{attempt_id}",
        "created_at": created,
        "started_at": created + timedelta(seconds=10),
        "completed_at": created + timedelta(minutes=2) if exit_code is not None else None,
        "exit_code": exit_code,
        "error": None,
        "peak_rss_bytes": 1234,
        "oom_near_miss": False,
        "checkpoints": [],
    })


def ckpt_prefix(tenant, task, attempt, checkpoint):
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}/checkpoints/{checkpoint}"


def write_checkpoint(
    objects,
    *,
    tenant="eng",
    task="task_a",
    attempt="att_1",
    checkpoint="ckpt-00001",
    seq=1,
    generation=1,
    archive=b"tar-bytes-here",
    created_at="2026-09-20T11:00:00Z",
    manifest=True,
    manifest_overrides=None,
    label="periodic",
):
    """Archive then manifest, in that order, exactly as `CheckpointManager.create`.

    The manifest is the commit marker and is uploaded LAST, so `manifest=False`
    reproduces the real shape of an interrupted checkpoint rather than an
    invented one.
    """
    prefix = ckpt_prefix(tenant, task, attempt, checkpoint)
    objects.put(f"{prefix}/archive.tar.gz", archive)
    if not manifest:
        return prefix
    body = {
        "checkpoint_id": checkpoint,
        "seq": seq,
        "task_id": task,
        "attempt_id": attempt,
        "tenant_id": tenant,
        "generation": generation,
        "created_at": created_at,
        "archive_key": f"{prefix}/archive.tar.gz",
        "manifest_key": f"{prefix}/manifest.json",
        "archive_bytes": len(archive),
        "archive_sha256": "5d6d401ff79341d7fb5048c8cdea403bda9255481b8c1885ace58a2b9b1759e2",
        "file_count": 7,
        "uri": f"gs://{objects.bucket}/{prefix}/",
        "label": label,
    }
    body.update(manifest_overrides or {})
    objects.put(f"{prefix}/manifest.json", json.dumps(body, indent=2))
    return prefix


def a_task_with_one_checkpoint(db, objects, *, tenant="eng", task="task_a"):
    seed_tenant(db, tenant)
    seed_task(db, task_id=task, tenant_id=tenant, state="RUNNING")
    _attempt(db, "att_1", tenant, task, minutes_ago=30)
    return write_checkpoint(objects, tenant=tenant, task=task, attempt="att_1")


def get(client, task="task_a", user="alice", query=""):
    return client.get(f"/v1/tasks/{task}/checkpoints{query}", headers=auth_header(user))


# --------------------------------------------------------------------------
# What it serves
# --------------------------------------------------------------------------

def test_the_manifest_the_worker_writes_is_what_the_route_returns(client, db, objects):
    """The regression. Before this route, every field below was unreachable."""
    prefix = a_task_with_one_checkpoint(db, objects)

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["task_id"] == "task_a"
    assert body["tenant_id"] == "eng"
    assert body["listed"] is True
    assert body["truncated"] is False
    assert len(body["checkpoints"]) == 1

    row = body["checkpoints"][0]
    assert row["checkpoint_id"] == "ckpt-00001"
    assert row["attempt_id"] == "att_1"
    assert row["prefix"] == prefix
    assert row["manifest"] == "present"
    assert row["created_at"] == "2026-09-20T11:00:00Z"
    assert row["seq"] == 1
    assert row["generation"] == 1
    assert row["label"] == "periodic"
    assert row["archive_bytes"] == len(b"tar-bytes-here")
    assert row["file_count"] == 7
    assert row["archive_sha256"].startswith("5d6d401f")


def test_it_says_what_a_checkpoint_contains_without_downloading_it(client, db, objects):
    """A UI has to show the size and shape of a checkpoint before anyone asks
    for it. `file_count` and `archive_bytes` come out of the manifest and the
    object sizes come out of the listing, so a 2 GiB archive costs this route
    the same as a 600-byte one."""
    a_task_with_one_checkpoint(db, objects)

    row = get(client).json()["checkpoints"][0]
    names = {o["name"]: o["bytes"] for o in row["objects"]}
    assert names == {"archive.tar.gz": 14, "manifest.json": names["manifest.json"]}
    assert row["stored_bytes"] == sum(names.values())
    assert row["file_count"] == 7, "the file count inside the archive, from the manifest"


def test_checkpoints_from_every_attempt_are_listed_newest_attempt_first(client, db, objects):
    """A resume scans the whole task prefix, so the checkpoint that matters
    after a crash was written by the attempt that DIED. A view of the current
    attempt only would omit the one thing anyone is looking for."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    _attempt(db, "att_old", "eng", "task_a", minutes_ago=90, exit_code=1)
    _attempt(db, "att_new", "eng", "task_a", minutes_ago=10)
    write_checkpoint(objects, attempt="att_old", checkpoint="ckpt-00001")
    write_checkpoint(objects, attempt="att_old", checkpoint="ckpt-00002", seq=2)
    write_checkpoint(objects, attempt="att_new", checkpoint="ckpt-00001")

    rows = get(client).json()["checkpoints"]
    assert [(r["attempt_id"], r["checkpoint_id"]) for r in rows] == [
        ("att_new", "ckpt-00001"),
        ("att_old", "ckpt-00002"),
        ("att_old", "ckpt-00001"),
    ]
    assert all(r["attempt_known"] for r in rows)


def test_one_attempt_can_be_asked_for_on_its_own(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_old", "eng", "task_a", minutes_ago=90)
    _attempt(db, "att_new", "eng", "task_a", minutes_ago=10)
    write_checkpoint(objects, attempt="att_old")
    write_checkpoint(objects, attempt="att_new")

    rows = get(client, query="?attempt_id=att_old").json()["checkpoints"]
    assert [r["attempt_id"] for r in rows] == ["att_old"]


def test_an_attempt_with_no_document_is_listed_last_and_says_so(client, db, objects):
    """`scripts/purge-data.sh` removes documents and objects in separate steps
    and either can fail alone. A checkpoint whose attempt document is gone must
    still be visible -- and must not claim the attempt never happened."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_known", "eng", "task_a", minutes_ago=10)
    write_checkpoint(objects, attempt="att_known")
    write_checkpoint(objects, attempt="att_orphaned")

    rows = get(client).json()["checkpoints"]
    assert [r["attempt_id"] for r in rows] == ["att_known", "att_orphaned"]
    assert rows[0]["attempt_known"] is True
    assert rows[1]["attempt_known"] is False
    assert rows[1]["attempt_created_at"] is None


# --------------------------------------------------------------------------
# Present, absent, unreadable -- the three answers
# --------------------------------------------------------------------------

def test_a_task_with_no_checkpoints_is_listed_and_empty(client, db, objects):
    """`listed: true` is what makes the empty array readable as an answer."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="QUEUED")

    body = get(client).json()
    assert body["checkpoints"] == []
    assert body["listed"] is True
    assert body["total_found"] == 0


def test_a_checkpoint_with_no_manifest_was_never_committed(client, db, objects):
    """The manifest is uploaded LAST, so its absence is a fact, not a failure:
    no restore will ever select this prefix. `resumable` is false, and the
    reason says why."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    write_checkpoint(objects, manifest=False)

    row = get(client).json()["checkpoints"][0]
    assert row["manifest"] == "absent"
    assert row["resumable"] is False
    assert "never committed" in row["resumable_detail"]
    assert row["archive_bytes"] is None, "nothing may be claimed from a manifest that is not there"


def test_an_unreadable_manifest_is_not_reported_as_an_uncommitted_one(client, db, objects):
    """THE DISTINCTION THIS ROUTE EXISTS TO KEEP.

    A 403 on the manifest object and a checkpoint that was never committed
    produce the same empty metadata. One means "this is not resumable"; the
    other means "I could not look". Collapsing them is how an operator decides
    a checkpoint is worthless and retries from scratch.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    prefix = write_checkpoint(objects)
    objects.fail_on(f"{prefix}/manifest.json")

    row = get(client).json()["checkpoints"][0]
    assert row["manifest"] == "unreadable"
    assert row["resumable"] is None, "null, not false: nothing was established"
    assert row["resumable_detail"] == "the manifest could not be read, so nothing is known"
    # The checkpoint itself is still reported -- the listing succeeded.
    assert row["checkpoint_id"] == "ckpt-00001"
    assert row["objects"], "the objects are known from the listing even when the manifest is not"


def test_a_manifest_that_is_not_json_is_unreadable_rather_than_a_500(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    prefix = write_checkpoint(objects)
    objects.put(f"{prefix}/manifest.json", "{not json at all")

    response = get(client)
    assert response.status_code == 200, response.text
    row = response.json()["checkpoints"][0]
    assert row["manifest"] == "unreadable"
    assert row["resumable"] is None


def test_a_failed_listing_is_a_failed_listing_not_an_empty_list(client, db, objects):
    """The bug this whole repository has been removing for two days.

    A 503 with a reason, never a 200 with `{"checkpoints": []}`. There is no
    partial answer to a listing that did not happen, and an empty array is
    indistinguishable from a task that has genuinely checkpointed nothing.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    write_checkpoint(objects)
    objects.fail_on("tenants/eng/tasks/task_a/attempts/")

    response = get(client)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "upstream_unavailable"
    assert "could not be listed" in body["message"]
    assert "checkpoints" not in body


def test_an_unconfigured_artifact_store_says_so_rather_than_answering_none(
    db, tokens, group_map
):
    """"No bucket is configured" and "the read failed" send an operator to two
    different places. One is a deployment variable, the other is IAM."""
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        objects=None,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")

    response = client.get("/v1/tasks/task_a/checkpoints", headers=auth_header("alice"))
    assert response.status_code == 503, response.text
    assert "ARTIFACT_BUCKET" in response.json()["message"]


# --------------------------------------------------------------------------
# Resumability -- the same rules `CheckpointManager` applies
# --------------------------------------------------------------------------

def test_a_committed_checkpoint_whose_archive_is_present_is_resumable(client, db, objects):
    a_task_with_one_checkpoint(db, objects)
    row = get(client).json()["checkpoints"][0]
    assert row["resumable"] is True


def test_a_manifest_naming_another_task_is_refused_exactly_as_the_worker_refuses_it(
    client, db, objects
):
    """`CheckpointManager._owns` compares BOTH identifiers and requires both
    keys to sit under this task's own prefix, because a manifest found in a
    bucket is not evidence of who wrote it. This route answers the same way, so
    a screen cannot offer a restore the worker would reject."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    write_checkpoint(objects, manifest_overrides={"task_id": "task_somewhere_else"})

    row = get(client).json()["checkpoints"][0]
    assert row["resumable"] is False
    assert "not this task" in row["resumable_detail"]


def test_a_manifest_pointing_its_archive_outside_the_task_is_not_resumable(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    write_checkpoint(
        objects,
        manifest_overrides={"archive_key": "tenants/research/tasks/t/attempts/a/x.tar.gz"},
    )

    row = get(client).json()["checkpoints"][0]
    assert row["resumable"] is False
    assert "outside this task's own prefix" in row["resumable_detail"]


def test_a_truncated_archive_is_not_resumable(client, db, objects):
    """The manifest declares a size. A restore verifies a sha256 it cannot
    check here without downloading, but a size that already disagrees is
    provable for free -- and it is the shape a half-finished upload leaves."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    write_checkpoint(objects, manifest_overrides={"archive_bytes": 99999})

    row = get(client).json()["checkpoints"][0]
    assert row["resumable"] is False
    assert "truncated or replaced" in row["resumable_detail"]


def test_a_manifest_whose_archive_is_gone_is_not_resumable(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    prefix = write_checkpoint(objects)
    del objects.objects[f"{prefix}/archive.tar.gz"]

    row = get(client).json()["checkpoints"][0]
    assert row["resumable"] is False
    assert "not in the bucket" in row["resumable_detail"]


# --------------------------------------------------------------------------
# `task.latest_checkpoint`
# --------------------------------------------------------------------------

def test_the_pointer_the_control_plane_holds_is_marked_on_the_row(client, db, objects):
    prefix = a_task_with_one_checkpoint(db, objects)
    db.docs["tasks/task_a"]["latest_checkpoint"] = f"gs://{objects.bucket}/{prefix}/"

    body = get(client).json()
    assert body["latest_checkpoint"]["status"] == "present"
    assert body["latest_checkpoint"]["checkpoint_id"] == "ckpt-00001"
    assert body["checkpoints"][0]["is_latest_pointer"] is True


def test_the_pointer_matches_a_whole_prefix_not_a_string_prefix(client, db, objects):
    """`ckpt-00001` and `ckpt-000010` share a string prefix. A substring test
    would mark the wrong checkpoint as the one a retry would restore."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    write_checkpoint(objects, checkpoint="ckpt-00001")
    write_checkpoint(objects, checkpoint="ckpt-000010", seq=10)
    db.docs["tasks/task_a"]["latest_checkpoint"] = (
        f"gs://{objects.bucket}/{ckpt_prefix('eng', 'task_a', 'att_1', 'ckpt-00001')}/"
    )

    marked = [r["checkpoint_id"] for r in get(client).json()["checkpoints"] if r["is_latest_pointer"]]
    assert marked == ["ckpt-00001"]


def test_a_pointer_outside_this_task_is_a_finding_not_a_current_checkpoint(client, db, objects):
    """A resuming worker resolves the pointer only inside this task's own
    prefix and otherwise ignores it. Rendering such a pointer as "the current
    checkpoint" would show a restore source that will never be used."""
    a_task_with_one_checkpoint(db, objects)
    db.docs["tasks/task_a"]["latest_checkpoint"] = (
        f"gs://{objects.bucket}/tenants/research/tasks/task_b/attempts/a/checkpoints/ckpt-00001/"
    )

    pointer = get(client).json()["latest_checkpoint"]
    assert pointer["status"] == "outside_this_task"
    assert pointer["checkpoint_id"] is None
    assert "would ignore it" in pointer["detail"]


def test_a_pointer_at_a_reclaimed_checkpoint_says_missing(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    db.docs["tasks/task_a"]["latest_checkpoint"] = (
        f"gs://{objects.bucket}/{ckpt_prefix('eng', 'task_a', 'att_1', 'ckpt-00009')}/"
    )

    pointer = get(client).json()["latest_checkpoint"]
    assert pointer["status"] == "missing"
    assert pointer["checkpoint_id"] == "ckpt-00009"


def test_no_pointer_is_unset_rather_than_missing(client, db, objects):
    a_task_with_one_checkpoint(db, objects)
    pointer = get(client).json()["latest_checkpoint"]
    assert pointer["status"] == "unset"


# --------------------------------------------------------------------------
# Tenant isolation -- invariant 9
# --------------------------------------------------------------------------

def test_another_tenants_task_is_a_404_and_leaks_nothing(client, db, objects):
    """Bob is in `research`. The task, the attempt and the checkpoint are all
    `eng`'s. The 404 is the same one a missing task gets, deliberately: a
    different status would confirm the id exists somewhere else."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    a_task_with_one_checkpoint(db, objects)

    response = client.get("/v1/tasks/task_a/checkpoints", headers=auth_header("bob"))
    assert response.status_code == 404, response.text
    assert "ckpt-00001" not in response.text
    assert "tenants/eng" not in response.text


def test_an_attempt_id_cannot_traverse_out_of_the_caller_s_prefix(client, db, objects):
    """The attempt segment is appended to a prefix that already pins tenant and
    task, so it can only narrow. It is validated anyway: GCS happens not to
    normalise `..`, and "the current storage backend happens not to" is not an
    access-control argument."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_task(db, task_id="task_a", tenant_id="eng")
    seed_task(db, task_id="task_b", tenant_id="research")
    write_checkpoint(objects, tenant="research", task="task_b", attempt="att_r")

    for hostile in ("../../../research", "..", "a/../../b", "", "att/../att"):
        response = get(client, query=f"?attempt_id={hostile}")
        assert response.status_code in (404, 422), (hostile, response.status_code)
        assert "task_b" not in response.text, hostile


def test_a_checkpoint_object_outside_the_scoped_prefix_is_never_reported(client, db, objects):
    """Defence in depth. The listing already pins tenant and task, so this can
    only happen if the store is wrong -- which is exactly when a boundary needs
    to hold. The key parser re-checks both identifiers and drops the key."""
    a_task_with_one_checkpoint(db, objects)
    foreign = ckpt_prefix("research", "task_b", "att_r", "ckpt-00001")

    assert parse_checkpoint_key(f"{foreign}/manifest.json", tenant_id="eng", task_id="task_a") is None
    body = get(client).json()
    assert all(r["prefix"].startswith("tenants/eng/tasks/task_a/") for r in body["checkpoints"])


def test_the_route_is_read_only(client, db, objects):
    """Neither the objects nor the task document may change. A checkpoint is
    the entire value of a parked attempt and only the reconciler may remove
    one."""
    a_task_with_one_checkpoint(db, objects)
    before_objects = dict(objects.objects)
    before_task = dict(db.docs["tasks/task_a"])

    assert get(client).status_code == 200
    assert objects.objects == before_objects
    assert db.docs["tasks/task_a"] == before_task


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------

def test_paging_returns_every_checkpoint_exactly_once(client, db, objects):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a", minutes_ago=50)
    _attempt(db, "att_2", "eng", "task_a", minutes_ago=10)
    for n in range(1, 6):
        write_checkpoint(objects, attempt="att_1", checkpoint=f"ckpt-{n:05d}", seq=n)
        write_checkpoint(objects, attempt="att_2", checkpoint=f"ckpt-{n:05d}", seq=n)

    seen, token, pages = [], None, 0
    while True:
        query = "?limit=3" + (f"&page_token={token}" if token else "")
        body = get(client, query=query).json()
        seen += [(r["attempt_id"], r["checkpoint_id"]) for r in body["checkpoints"]]
        token, pages = body["next_page_token"], pages + 1
        if token is None:
            break
        assert pages < 20, "the cursor is not advancing"

    assert len(seen) == 10
    assert len(set(seen)) == 10
    assert seen == sorted(seen, reverse=True), "newest attempt first, newest checkpoint first"


def test_a_page_token_this_endpoint_did_not_issue_is_refused(client, db, objects):
    """Restarting from page one on a malformed cursor gives a client an
    infinite list of the same rows and no way to learn why."""
    a_task_with_one_checkpoint(db, objects)
    response = get(client, query="?page_token=!!!not-base64!!!")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


def test_a_truncated_scan_is_never_silent(client, db, objects, api_context):
    """A screen showing the first N of M and a screen showing all of them are
    otherwise byte-identical."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _attempt(db, "att_1", "eng", "task_a")
    for n in range(1, 8):
        write_checkpoint(objects, checkpoint=f"ckpt-{n:05d}", seq=n)
    api_context.inspection._scan_limit = 4

    body = get(client).json()
    assert body["truncated"] is True


# --------------------------------------------------------------------------
# The layout is restated in swarm_api. It must not drift.
# --------------------------------------------------------------------------

def test_the_key_layout_agrees_with_the_one_the_worker_writes():
    """`swarm_api.inspect` restates the checkpoint layout because the swarm-api
    image installs `swarm-api` and `swarm-common` and nothing else, so
    `agent_worker` is not importable in the deployed service. Every rule
    restated twice in this repository has since drifted, so the two are
    compared directly rather than trusted.
    """
    from agent_worker.checkpoint import (
        ARCHIVE_NAME,
        MANIFEST_NAME,
        attempts_prefix as worker_attempts_prefix,
        checkpoint_prefix,
    )

    from swarm_api import inspect as api

    assert api.MANIFEST_NAME == MANIFEST_NAME
    assert api.ARCHIVE_NAME == ARCHIVE_NAME
    assert api.attempts_prefix(tenant_id="eng", task_id="t") == worker_attempts_prefix(
        tenant_id="eng", task_id="t"
    )

    prefix = checkpoint_prefix(
        tenant_id="eng", task_id="task_1", attempt_id="att_9", checkpoint_id="ckpt-00007"
    )
    ref = parse_checkpoint_key(f"{prefix}/{MANIFEST_NAME}", tenant_id="eng", task_id="task_1")
    assert ref is not None
    assert (ref.attempt_id, ref.checkpoint_id) == ("att_9", "ckpt-00007")
    assert ref.prefix == prefix
    assert ref.manifest_key == f"{prefix}/{MANIFEST_NAME}"
    assert ref.archive_key == f"{prefix}/{ARCHIVE_NAME}"


def test_the_log_prefix_agrees_with_the_one_the_worker_writes():
    """The same drift risk, for the log layout `_publish_live_logs` uses."""
    from agent_worker.config import WorkerConfig

    from swarm_api import inspect as api

    config = WorkerConfig(
        task_id="task_1",
        attempt_id="att_9",
        tenant_id="eng",
        lease_id="lease_1",
        generation=1,
        runner_profile="mock",
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        artifact_bucket="swarm-artifacts-saga-agents-staging",
    )
    base = api.attempt_prefix(tenant_id="eng", task_id="task_1", attempt_id="att_9")
    assert config.gcs_prefix == base
    assert config.log_prefix == f"{base}/{api.LOGS_SEGMENT}"
    assert config.checkpoint_prefix("ckpt-00001") == (
        f"{base}/{api.CHECKPOINTS_SEGMENT}/ckpt-00001"
    )


def test_the_pointer_normaliser_matches_the_reconcilers():
    """Three copies of this rule now exist -- the worker's `find_by_uri`, the
    reconciler's collector and this API. Two of them are compared here."""
    from reconciler.checkpoints import pointer_to_prefix as reconciler_version

    for pointer in (
        "gs://bucket/tenants/eng/tasks/t/attempts/a/checkpoints/ckpt-00001/",
        "gs://bucket/tenants/eng/tasks/t/attempts/a/checkpoints/ckpt-00001/manifest.json",
        "file:///tmp/x/tenants/eng/tasks/t/attempts/a/checkpoints/ckpt-00001",
        "nonsense",
        "",
        None,
    ):
        assert pointer_to_prefix(pointer) == reconciler_version(pointer), pointer
