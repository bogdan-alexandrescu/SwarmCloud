"""What is INSIDE a checkpoint -- listing, one file, the whole archive.

Owner decision 2026-09-24 on redesign-v2 S3 / item A3: a checkpoint gets all
three routes. These tests drive the real routes through the real service over
the real in-memory object reader, with archives built by `tarfile` exactly as
`CheckpointManager._write_archive` builds them -- and one built BY that
manager, so the thing listed is the thing the worker writes.

WHAT THESE TESTS ARE FOR, in the order they matter:

  1. THE TENANT BOUNDARY (invariant 9). Another tenant's task is a 404 on all
     three routes, and -- the stronger claim -- NOT ONE object read or listing
     happens on the way to that 404. `RecordingReader` is what makes the
     second half assertable.
  2. TRAVERSAL IS IMPOSSIBLE. Every path-shaped hostile input -- `..`,
     absolute, empty segments, a backslash, a NUL, percent-encoded to get past
     the HTTP client's own normalisation -- is refused before any object is
     read, and a member the ARCHIVE names `../x` is listed as unsafe and cannot
     be opened.
  3. ABSENCE AND FAILURE ARE NOT AN EMPTY CHECKPOINT. `files: null` for an
     absent archive, a 503 with no `files` for an unreadable one, `corrupt`
     for a short one -- and `files: []` only for a real empty workspace.
  4. BOUNDED AND NEVER SILENT. Entry cap, scan budget and inflate budget each
     cut the listing and each SAY so; no read is ever larger than one window,
     which is what "never loads a large tarball into memory" means here.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import random
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.checkpoint_content import (
    CheckpointContent,
    content_type_for,
    member_path,
    requested_path,
)
from swarm_api.errors import ValidationFailed
from swarm_api.objects import InMemoryObjectReader, ObjectSlice, ObjectUnreadable
from swarm_api.routes.checkpoints import checkpoint_content

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
BUCKET = f"swarm-artifacts-{PROJECT}"

#: A credential shape the redaction table recognises (`github_token`), so a
#: served window can be shown to have been scrubbed. Not the Anthropic shape:
#: the credential sweep elsewhere flags that prefix anywhere in a response.
GH_TOKEN = "ghp_" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMm"


# --------------------------------------------------------------------------
# A reader that remembers what it was asked for
# --------------------------------------------------------------------------

class RecordingReader(InMemoryObjectReader):
    """The shipped in-memory reader, plus a record of every call.

    `fail_after_first` names keys whose SECOND and later windows fail, which is
    the only way to show what a download does when the store dies after the
    status line has already been sent.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reads: list[tuple[str, int, int]] = []
        self.lists: list[str] = []
        self.fail_after_first: set[str] = set()

    def list_objects(self, prefix, *, limit):
        self.lists.append(prefix)
        return super().list_objects(prefix, limit=limit)

    def read_range(self, key, *, offset, length) -> ObjectSlice:
        self.reads.append((key, offset, length))
        if key in self.fail_after_first and offset > 0:
            raise ObjectUnreadable(key, "injected failure after the first window")
        return super().read_range(key, offset=offset, length=length)

    def forget(self) -> None:
        self.reads.clear()
        self.lists.clear()


@pytest.fixture
def objects() -> RecordingReader:
    # Overrides conftest's `objects`, so `api_context` and `client` are built
    # around THIS reader with no other change to the wiring.
    return RecordingReader(bucket=BUCKET)


@pytest.fixture
def budgets(client, api_context):
    """Swap the service's budgets through FastAPI's own injection point."""

    def _set(**limits):
        client.app.dependency_overrides[checkpoint_content] = lambda: CheckpointContent(
            api_context.inspection, **limits
        )

    yield _set
    client.app.dependency_overrides.pop(checkpoint_content, None)


# --------------------------------------------------------------------------
# Archives, in the shapes the worker writes
# --------------------------------------------------------------------------

def tar_gz(entries: list[tuple]) -> bytes:
    """A tar.gz from `(name, kind, payload[, mode])` rows, in order.

    `kind` is file / dir / symlink / hardlink. For links `payload` is the
    target. `tarfile.open("w:gz")` is what `_write_archive` calls.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for row in entries:
            name, kind, payload = row[0], row[1], row[2]
            mode = row[3] if len(row) > 3 else (0o755 if kind == "dir" else 0o644)
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.mtime = int(NOW.timestamp())
            if kind == "file":
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
                tar.addfile(info)
            else:  # pragma: no cover - a test-authoring error
                raise AssertionError(kind)
    return buffer.getvalue()


WORKSPACE = [
    ("progress", "dir", None),
    ("progress/step-0001.md", "file", "# Step one\n\nread the repository\n"),
    ("state.json", "file", '{"completed_steps": 1}\n'),
    ("bin", "dir", None),
    ("bin/run.sh", "file", "#!/bin/sh\necho hello\n", 0o755),
    ("latest", "symlink", "state.json"),
]


def prefix(tenant="eng", task="task_a", attempt="att_1", checkpoint="ckpt-00001") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}/checkpoints/{checkpoint}"


def put_checkpoint(
    objects,
    *,
    tenant="eng",
    task="task_a",
    attempt="att_1",
    checkpoint="ckpt-00001",
    archive: bytes | None = None,
    manifest: bool = True,
    file_count: int | None = None,
    manifest_overrides: dict | None = None,
) -> str:
    """Archive then manifest, the order `CheckpointManager.create` uploads them."""
    base = prefix(tenant, task, attempt, checkpoint)
    if archive is not None:
        objects.put(f"{base}/archive.tar.gz", archive)
    if manifest:
        body = {
            "checkpoint_id": checkpoint,
            "seq": 1,
            "task_id": task,
            "attempt_id": attempt,
            "tenant_id": tenant,
            "generation": 1,
            "created_at": "2026-09-24T11:00:00Z",
            "archive_key": f"{base}/archive.tar.gz",
            "manifest_key": f"{base}/manifest.json",
            "archive_bytes": len(archive) if archive is not None else 0,
            "archive_sha256": hashlib.sha256(archive or b"").hexdigest(),
            "file_count": file_count,
            "uri": f"gs://{BUCKET}/{base}/",
            "label": "periodic",
        }
        body.update(manifest_overrides or {})
        objects.put(f"{base}/manifest.json", json.dumps(body, indent=2))
    return base


def seed(db, *, tenant="eng", task="task_a", attempts=("att_1",)):
    seed_tenant(db, tenant)
    seed_task(db, task_id=task, tenant_id=tenant, state="RUNNING")
    for n, attempt_id in enumerate(attempts):
        created = NOW - timedelta(minutes=30 - n)
        db.collection("attempts").document(attempt_id).set({
            "attempt_id": attempt_id,
            "task_id": task,
            "tenant_id": tenant,
            "generation": n + 1,
            "lease_id": f"lease_{attempt_id}",
            "backend": "CLOUD_RUN_JOB",
            "execution_name": f"swarm-job-{tenant}-mock-{attempt_id}",
            "created_at": created,
            "started_at": created,
            "completed_at": None,
            "exit_code": None,
            "error": None,
            "peak_rss_bytes": None,
            "oom_near_miss": False,
            "checkpoints": [],
        })


def a_workspace_checkpoint(db, objects, **kw) -> str:
    seed(db)
    return put_checkpoint(objects, archive=tar_gz(WORKSPACE), file_count=4, **kw)


def files(client, task="task_a", checkpoint="ckpt-00001", user="alice", **params):
    return client.get(
        f"/v1/tasks/{task}/checkpoints/{checkpoint}/files",
        params=params,
        headers=auth_header(user),
    )


def one_file(client, path, task="task_a", checkpoint="ckpt-00001", user="alice", **params):
    return client.get(
        f"/v1/tasks/{task}/checkpoints/{checkpoint}/files/{path}",
        params=params,
        headers=auth_header(user),
    )


def download(client, task="task_a", checkpoint="ckpt-00001", user="alice", **params):
    return client.get(
        f"/v1/tasks/{task}/checkpoints/{checkpoint}/content",
        params=params,
        headers=auth_header(user),
    )


def noise(n: int, seed_value: int = 7) -> bytes:
    """Incompressible bytes, deterministically -- so a compressed size is real."""
    return random.Random(seed_value).randbytes(n)


# --------------------------------------------------------------------------
# 1. The tenant boundary. MUTATE THESE FIRST.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["files", "file", "content"])
def test_another_tenants_checkpoint_is_a_404_and_no_object_is_touched(
    client, db, objects, route
):
    """THE ONE THAT MATTERS.

    `bob` is in research; the task, the checkpoint and the archive are eng's.
    The 404 is the same one a missing task gets, and the stronger half: the
    reader was never asked for anything. A route that listed eng's prefix and
    THEN refused would still be reading another tenant's bucket on a stranger's
    behalf.
    """
    a_workspace_checkpoint(db, objects)
    seed_tenant(db, "research")
    objects.forget()

    if route == "files":
        response = files(client, user="bob")
    elif route == "file":
        response = one_file(client, "state.json", user="bob")
    else:
        response = download(client, user="bob")

    assert response.status_code == 404, response.text
    assert "completed_steps" not in response.text
    assert "tenants/eng" not in response.text
    assert objects.reads == [], f"{route} read {objects.reads} for another tenant"
    assert objects.lists == [], f"{route} listed {objects.lists} for another tenant"


def test_every_key_read_sits_under_the_callers_own_task(client, db, objects):
    """Across a full listing, a file read and a download, the reader is asked
    for nothing outside `tenants/eng/tasks/task_a/`."""
    a_workspace_checkpoint(db, objects)
    put_checkpoint(objects, tenant="research", task="task_b", archive=tar_gz(WORKSPACE))
    objects.forget()

    assert files(client).status_code == 200
    assert one_file(client, "state.json").status_code == 200
    assert download(client).status_code == 200

    touched = [key for key, _, _ in objects.reads] + objects.lists
    assert touched, "nothing was read; this assertion would pass by silence"
    assert all(k.startswith("tenants/eng/tasks/task_a/") for k in touched), touched


# --------------------------------------------------------------------------
# 2. Traversal is impossible
# --------------------------------------------------------------------------

HOSTILE_PATHS = [
    # Percent-encoded, because httpx removes literal dot segments before the
    # request leaves the client and the test would then prove nothing.
    "..%2F..%2F..%2Fresearch%2Ftasks%2Ftask_b",
    "%2e%2e%2fstate.json",
    "progress%2F..%2F..%2Fstate.json",
    "%2Fetc%2Fpasswd",
    "progress%2F%2Fstep-0001.md",
    "progress%2F.%2Fstep-0001.md",
    ".%2Fstate.json",
    "progress%2F",
    "..%5C..%5Cstate.json",
    "state.json%00.png",
    "state%0A.json",
]


@pytest.mark.parametrize("hostile", HOSTILE_PATHS)
def test_a_traversing_path_is_refused_before_any_object_is_read(
    client, db, objects, hostile
):
    a_workspace_checkpoint(db, objects)
    put_checkpoint(objects, tenant="research", task="task_b", archive=tar_gz(WORKSPACE))
    objects.forget()

    response = one_file(client, hostile)

    assert response.status_code in (404, 422), (hostile, response.status_code, response.text)
    assert "completed_steps" not in response.text, hostile
    assert objects.reads == [], (hostile, objects.reads)
    assert objects.lists == [], (hostile, objects.lists)


@pytest.mark.parametrize(
    "path",
    ["../x", "a/../../b", "/etc/passwd", "a//b", "./a", "a/./b", "a/", "", "a\\b",
     "a\x00b", "a\nb", "..", "."],
)
def test_the_path_validator_refuses_every_shape_of_somewhere_else(path):
    with pytest.raises(ValidationFailed):
        requested_path(path)


def test_an_ordinary_nested_path_is_accepted_unchanged():
    assert requested_path("progress/step-0001.md") == "progress/step-0001.md"
    assert requested_path(".env") == ".env", "a dotfile is a name, not a segment"
    assert requested_path("a..b/c") == "a..b/c", "dots inside a name are not a segment"


def test_a_member_the_archive_names_outside_itself_is_listed_unsafe_and_never_served(
    client, db, objects
):
    """A hand-built archive, because the worker's writer cannot produce one:
    `../../escape.txt` is exactly what `checkpoint._safe_members` refuses on
    restore. It is LISTED -- hiding it would hide why this checkpoint cannot
    resume -- flagged, and no request can read it."""
    seed(db)
    put_checkpoint(
        objects,
        archive=tar_gz([
            ("../../escape.txt", "file", "escaped content"),
            ("/abs.txt", "file", "absolute content"),
            ("ok.txt", "file", "fine"),
        ]),
    )

    rows = {r["path"]: r for r in files(client).json()["files"]}
    assert rows["../../escape.txt"]["unsafe"] is True
    assert rows["/abs.txt"]["unsafe"] is True
    assert rows["ok.txt"]["unsafe"] is False

    for hostile in ("..%2F..%2Fescape.txt", "%2Fabs.txt"):
        response = one_file(client, hostile)
        assert response.status_code == 422, (hostile, response.text)
        assert "escaped content" not in response.text
        assert "absolute content" not in response.text


def not_utf8(raw: bytes) -> str:
    """A name as `Path.rglob` hands it to `tarfile` on Linux.

    A byte that is not UTF-8 arrives SURROGATE-ESCAPED (`b"\\xe9"` becomes
    `"\\udce9"`), `tarfile`'s PAX writer stores it as raw bytes under
    `hdrcharset=BINARY`, and its reader gives the same lone surrogate back.
    """
    return raw.decode("utf-8", "surrogateescape")


def test_a_member_name_that_is_not_utf8_is_listed_escaped_and_is_not_a_500(
    client, db, objects
):
    """One Latin-1 file name in a cloned repository, and every listing of every
    checkpoint of that task was an unhandled 500: a lone surrogate is a `str`
    no JSON encoder will write, so the response failed AFTER the route had
    returned -- not an ApiError, no reason, and a retry that never helps.

    The name is served as its bytes, escaped (`caf\\xe9.txt`), and flagged
    `undecodable`. It is NOT `unsafe`: that flag says a restore would refuse
    the archive, and a restore unpacks a Latin-1 name without complaint.
    """
    seed(db)
    put_checkpoint(objects, archive=tar_gz([
        ("ok.txt", "file", "fine"),
        (not_utf8(b"caf\xe9.txt"), "file", "latin-1 named content"),
        (not_utf8(b"d\xe9/inner.txt"), "file", "inside a latin-1 directory"),
        ("pointer", "symlink", not_utf8(b"t\xe9")),
    ]))

    response = files(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    rows = {r["path"]: r for r in body["files"]}
    assert set(rows) == {"ok.txt", "caf\\xe9.txt", "d\\xe9/inner.txt", "pointer"}, rows

    assert rows["caf\\xe9.txt"]["undecodable"] is True
    assert rows["caf\\xe9.txt"]["unsafe"] is False, "a restore unpacks it; it is not an escape"
    assert rows["d\\xe9/inner.txt"]["undecodable"] is True
    assert rows["pointer"]["link"] == "t\\xe9"
    assert rows["pointer"]["undecodable"] is True, "the TARGET is what was escaped"
    assert rows["ok.txt"]["undecodable"] is False

    # The escaped form is a display, never an address: it carries a backslash,
    # which `requested_path` refuses before any object is read.
    escaped = one_file(client, "caf%5Cxe9.txt")
    assert escaped.status_code == 422, escaped.text
    # Latin-1 percent-encoding decodes to U+FFFD, which names nothing here.
    latin = one_file(client, "caf%E9.txt")
    assert latin.status_code == 404, latin.text
    assert "latin-1 named content" not in escaped.text + latin.text

    # A link whose target is not UTF-8 is refused as a link -- a 422 naming
    # the escaped target -- and not a 500 from the error body itself.
    link = one_file(client, "pointer")
    assert link.status_code == 422, link.text
    assert link.json()["detail"]["link"] == "t\\xe9"


def test_a_hostile_checkpoint_or_attempt_id_reaches_no_object(client, db, objects):
    a_workspace_checkpoint(db, objects)
    put_checkpoint(objects, tenant="research", task="task_b", archive=tar_gz(WORKSPACE))
    objects.forget()

    for response in (
        files(client, checkpoint="%2e%2e"),
        files(client, checkpoint="ckpt-00001%2F..%2F..%2F..%2Fresearch"),
        files(client, attempt_id="../../../research"),
        files(client, attempt_id="att_1/../../x"),
        download(client, attempt_id=".."),
        one_file(client, "state.json", attempt_id="a/b"),
    ):
        assert response.status_code in (404, 422), response.text
        assert "task_b" not in response.text
    assert objects.reads == [] and objects.lists == [], (objects.reads, objects.lists)


def test_member_names_are_shown_without_a_leading_dot_slash():
    assert member_path("./a/b.txt") == ("a/b.txt", False)
    assert member_path("dir/") == ("dir", False)
    assert member_path("./") == (None, False), "the archive's own root is not a file"
    assert member_path("../x")[1] is True


# --------------------------------------------------------------------------
# 3. The listing -- and the answers it keeps apart
# --------------------------------------------------------------------------

def test_the_listing_is_every_member_with_path_size_mode_and_type(client, db, objects):
    """The regression. Before this route, no request could say what a
    checkpoint contained -- the manifest carries a count and no names."""
    base = a_workspace_checkpoint(db, objects)

    response = files(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "ok"
    assert body["truncated"] is False and body["truncated_reason"] is None
    assert body["checkpoint_id"] == "ckpt-00001"
    assert body["attempt_id"] == "att_1"
    assert body["archive"]["key"] == f"{base}/archive.tar.gz"
    assert body["archive"]["uri"] == f"gs://{BUCKET}/{base}/archive.tar.gz"

    rows = body["files"]
    assert [r["path"] for r in rows] == [
        "progress", "progress/step-0001.md", "state.json", "bin", "bin/run.sh", "latest",
    ], "archive order, not sorted -- the order a restore unpacks in"
    by_path = {r["path"]: r for r in rows}
    assert by_path["progress"]["type"] == "dir"
    assert by_path["state.json"] == {
        "path": "state.json", "size": 23, "mode": 0o644, "type": "file",
        "link": None, "unsafe": False, "undecodable": False,
    }
    assert by_path["bin/run.sh"]["mode"] == 0o755
    assert by_path["latest"]["type"] == "symlink"
    assert by_path["latest"]["link"] == "state.json"
    assert body["count"] == len(rows)


def test_the_listing_agrees_with_the_manifests_own_count(client, db, objects):
    """`_write_archive` counts every member that is not a directory. Four here:
    two files, one script, one symlink."""
    a_workspace_checkpoint(db, objects)
    body = files(client).json()
    assert body["manifest"]["status"] == "present"
    assert body["manifest"]["file_count"] == 4
    assert body["file_count_agrees"] is True
    assert body["manifest"]["archive_key_agrees"] is True


def test_a_listing_shorter_than_the_manifest_says_so(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz(WORKSPACE), file_count=40)
    assert files(client).json()["file_count_agrees"] is False


def test_the_archive_the_worker_writes_is_the_archive_this_route_lists(
    client, db, objects, tmp_path
):
    """Not a hand-built tarball: `CheckpointManager.create` over a real
    workspace, uploaded to the worker's own local store, copied key for key
    into the API's reader. If the worker's archive format ever moves, this is
    the test that notices."""
    from agent_worker import workspace as workspace_mod
    from agent_worker.checkpoint import CheckpointManager
    from agent_worker.objectstore import LocalObjectStore

    class Quiet:
        def info(self, *a, **k): ...
        def warning(self, *a, **k): ...
        def error(self, *a, **k): ...

    seed(db)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "progress").mkdir()
    (ws.work / "progress" / "step-0001.md").write_text("# Step one\n")
    (ws.work / "state.json").write_text('{"completed_steps": 1}')
    (ws.work / "latest").symlink_to("state.json")
    store = LocalObjectStore(tmp_path / "bucket", bucket=BUCKET)
    record = CheckpointManager(
        store=store, tenant_id="eng", task_id="task_a", attempt_id="att_1",
        generation=1, logger=Quiet(),
    ).create(ws)
    copied = 0
    for key in store.list_keys("tenants/"):
        objects.put(key, store.download_bytes(key))
        copied += 1
    assert copied == 2, "the archive and the manifest"

    body = files(client, checkpoint=record.checkpoint_id).json()
    assert body["status"] == "ok", body
    assert {r["path"] for r in body["files"]} == {
        "latest", "progress", "progress/step-0001.md", "state.json",
    }
    assert body["manifest"]["file_count"] == record.file_count == 3
    assert body["file_count_agrees"] is True

    content = one_file(client, "progress/step-0001.md", checkpoint=record.checkpoint_id).json()
    assert content["status"] == "ok"
    assert content["content"] == "# Step one\n"


def test_an_empty_workspace_is_a_real_empty_listing(client, db, objects):
    """`files: []` is allowed exactly here: the archive was read to its end
    and holds nothing. `status: ok` and `truncated: false` are what make the
    empty array an answer."""
    seed(db)
    put_checkpoint(objects, archive=tar_gz([]), file_count=0)

    body = files(client).json()
    assert body["status"] == "ok"
    assert body["files"] == []
    assert body["truncated"] is False
    assert body["file_count_agrees"] is True


def test_an_absent_archive_is_null_files_never_an_empty_list(client, db, objects):
    """The manifest is there and the archive is not -- reclaimed, or an upload
    that never landed. That is not a checkpoint with no files in it."""
    seed(db)
    put_checkpoint(objects, archive=None)

    response = files(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "absent"
    assert body["files"] is None, "null, not []: nothing was listed"
    assert body["archive"]["bytes"] is None
    assert "no archive" in body["detail"]


def test_a_checkpoint_that_does_not_exist_is_a_404(client, db, objects):
    a_workspace_checkpoint(db, objects)
    response = files(client, checkpoint="ckpt-00009")
    assert response.status_code == 404, response.text
    assert "ckpt-00009" in response.json()["message"]


def test_a_checkpoint_id_is_matched_as_a_whole_prefix_not_a_string_prefix(
    client, db, objects
):
    """`ckpt-00001` and `ckpt-000010` share a string prefix."""
    seed(db)
    put_checkpoint(objects, checkpoint="ckpt-000010", archive=tar_gz([("ten.txt", "file", "10")]))
    assert files(client, checkpoint="ckpt-00001").status_code == 404


def test_an_unreadable_archive_is_a_503_not_an_empty_listing(client, db, objects):
    """THE BUG THIS REPOSITORY KEEPS REMOVING. A read that failed is a 503 with
    a reason, and there is no `files` key at all to be mistaken for an answer."""
    base = a_workspace_checkpoint(db, objects)
    objects.fail_on(f"{base}/archive.tar.gz")

    response = files(client)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "upstream_unavailable"
    assert "files" not in body


def test_a_failed_prefix_listing_is_a_503(client, db, objects):
    a_workspace_checkpoint(db, objects)
    objects.fail_on("tenants/eng/tasks/task_a/attempts/")
    response = files(client, attempt_id="att_1")
    assert response.status_code == 503, response.text
    assert "files" not in response.json()


def test_an_unreadable_manifest_does_not_stop_the_listing_or_pretend_to_agree(
    client, db, objects
):
    base = a_workspace_checkpoint(db, objects)
    objects.fail_on(f"{base}/manifest.json")

    body = files(client).json()
    assert body["status"] == "ok"
    assert body["manifest"]["status"] == "unreadable"
    assert body["file_count_agrees"] is None, "null: there was nothing to compare with"
    assert len(body["files"]) == 6


def test_a_manifest_string_json_cannot_carry_is_escaped_not_a_500(client, db, objects):
    """The same lone surrogate by the other door. `json.dumps` writes one as
    the escape `\\udce9` and `json.loads` hands it straight back, so a manifest
    field is a second way for a string no response can encode to reach one."""
    seed(db)
    put_checkpoint(
        objects,
        archive=tar_gz(WORKSPACE),
        file_count=4,
        manifest_overrides={"label": "caf\udce9", "created_at": "\ud800"},
    )

    response = files(client)
    assert response.status_code == 200, response.text
    manifest = response.json()["manifest"]
    assert manifest["status"] == "present"
    assert manifest["label"] == "caf\\xe9"
    assert manifest["created_at"] == "\\ud800"


def test_a_manifest_digest_that_is_not_a_digest_stays_out_of_the_header(
    client, db, objects
):
    """A manifest is data a worker wrote into a bucket, and a response header
    is latin-1 with no control characters. A digest that is not a digest is
    left out of `X-Checkpoint-Sha256` rather than failing the download it
    would have labelled -- and never becomes a header of the worker's choosing.
    """
    seed(db)
    put_checkpoint(
        objects,
        archive=tar_gz(WORKSPACE),
        file_count=4,
        manifest_overrides={"archive_sha256": "☃ not a digest\r\nX-Injected: 1"},
    )

    fetched = download(client)
    assert fetched.status_code == 200, fetched.text
    assert "x-checkpoint-sha256" not in fetched.headers
    assert "x-injected" not in fetched.headers
    assert fetched.headers["x-checkpoint-manifest"] == "present"


def test_a_checkpoint_never_committed_is_still_listed_and_says_so(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz(WORKSPACE), manifest=False)
    body = files(client).json()
    assert body["status"] == "ok"
    assert body["manifest"]["status"] == "absent"
    assert "never committed" in body["manifest"]["detail"]
    assert body["file_count_agrees"] is None


def test_a_truncated_object_is_corrupt_not_a_shorter_listing(client, db, objects):
    """`tarfile` on its own stops quietly at a short gzip stream and reports
    the members before the cut as the whole archive. The gzip end marker is
    required, so a short object is named for what it is."""
    seed(db)
    whole = tar_gz([(f"f{n:03d}.bin", "file", noise(16 * 1024, n)) for n in range(12)])
    put_checkpoint(objects, archive=whole[: len(whole) // 2], file_count=12)

    body = files(client).json()
    assert body["status"] == "corrupt", body
    assert body["truncated"] is True
    assert body["truncated_reason"] == "corrupt"
    assert "truncated" in body["detail"] or "readable" in body["detail"]
    assert 0 < len(body["files"]) < 12, "the members before the cut, and only those"
    assert body["file_count_agrees"] is None


def test_an_object_that_is_not_a_tarball_at_all_is_corrupt(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=b"this is not a gzip stream at all")
    body = files(client).json()
    assert body["status"] == "corrupt"
    assert body["files"] == []
    assert body["truncated"] is True


def test_one_attempt_is_chosen_by_attempt_id(client, db, objects):
    """Checkpoint ids restart per attempt, so `ckpt-00001` exists once for
    every attempt that checkpointed. Guessing would serve another attempt's
    working tree under the right name."""
    seed(db, attempts=("att_1", "att_2"))
    put_checkpoint(objects, attempt="att_1", archive=tar_gz([("one.txt", "file", "1")]))
    put_checkpoint(objects, attempt="att_2", archive=tar_gz([("two.txt", "file", "2")]))

    ambiguous = files(client)
    assert ambiguous.status_code == 422, ambiguous.text
    assert ambiguous.json()["detail"]["attempt_ids"] == ["att_1", "att_2"]

    assert [r["path"] for r in files(client, attempt_id="att_2").json()["files"]] == ["two.txt"]
    assert [r["path"] for r in files(client, attempt_id="att_1").json()["files"]] == ["one.txt"]


def test_a_unique_checkpoint_needs_no_attempt_id(client, db, objects):
    a_workspace_checkpoint(db, objects)
    assert files(client).json()["attempt_id"] == "att_1"


# --------------------------------------------------------------------------
# 4. Bounded, and never silent about it
# --------------------------------------------------------------------------

def test_the_entry_cap_cuts_the_listing_and_says_so(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([(f"f{n}.txt", "file", str(n)) for n in range(10)]))

    body = files(client, limit=3).json()
    assert [r["path"] for r in body["files"]] == ["f0.txt", "f1.txt", "f2.txt"]
    assert body["truncated"] is True
    assert body["truncated_reason"] == "entry_cap"
    assert body["limits"]["entries"] == 3
    assert body["file_count_agrees"] is None, "a cut listing proves nothing about the count"


def test_a_large_archive_is_read_in_windows_and_the_cap_stops_the_read(
    client, db, objects, budgets
):
    """"Never load a large tarball into memory", made checkable: no single
    read is larger than one window, and a listing capped at two entries stops
    reading long before the end of a 5 MiB archive."""
    seed(db)
    archive = tar_gz([(f"f{n:02d}.bin", "file", noise(256 * 1024, n)) for n in range(20)])
    assert len(archive) > 5 * 1024 * 1024 - 1, "the archive must actually be large"
    base = put_checkpoint(objects, archive=archive)
    budgets(chunk_bytes=64 * 1024)
    objects.forget()

    body = files(client, limit=2).json()
    assert body["truncated_reason"] == "entry_cap"
    archive_reads = [(k, o, n) for k, o, n in objects.reads if k.endswith("archive.tar.gz")]
    assert archive_reads, "the archive was never read"
    assert max(n for _, _, n in archive_reads) <= 64 * 1024
    assert body["scanned_bytes"] < len(archive) // 4, body["scanned_bytes"]
    assert all(k.startswith(base) or k.startswith("tenants/eng/tasks/task_a/")
               for k, _, _ in objects.reads)


def test_the_scan_budget_cuts_the_listing_and_says_so(client, db, objects, budgets):
    seed(db)
    put_checkpoint(
        objects,
        archive=tar_gz([(f"f{n:02d}.bin", "file", noise(64 * 1024, n)) for n in range(20)]),
        file_count=20,
    )
    budgets(chunk_bytes=32 * 1024, max_scan_bytes=192 * 1024)

    body = files(client).json()
    assert body["status"] == "ok"
    assert body["truncated"] is True
    assert body["truncated_reason"] == "scan_budget"
    assert 0 < len(body["files"]) < 20
    assert body["scanned_bytes"] <= 192 * 1024
    assert body["file_count_agrees"] is None


def test_the_inflate_budget_stops_a_bomb(client, db, objects, budgets):
    """Zeros compress ~1000:1, so the compressed budget alone admits an object
    that inflates to hundreds of GiB. The inflate budget is what bounds CPU."""
    seed(db)
    bomb = tar_gz([("zeros.bin", "file", b"\x00" * (16 * 1024 * 1024)), ("after.txt", "file", "x")])
    assert len(bomb) < 256 * 1024, "the point is that it is SMALL on the wire"
    put_checkpoint(objects, archive=bomb)
    budgets(max_inflate_bytes=1024 * 1024)

    body = files(client).json()
    assert body["truncated"] is True
    assert body["truncated_reason"] == "inflate_budget"
    assert [r["path"] for r in body["files"]] == ["zeros.bin"]
    assert body["inflated_bytes"] <= 1024 * 1024 + 256 * 1024


# --------------------------------------------------------------------------
# 5. One file
# --------------------------------------------------------------------------

def test_one_file_is_served_as_the_artifact_route_serves_an_artifact(client, db, objects):
    """Field for field the artifact-content shape, so ArtifactViewer renders
    it with no second code path."""
    base = a_workspace_checkpoint(db, objects)

    response = one_file(client, "progress/step-0001.md")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["content"] == "# Step one\n\nread the repository\n"
    assert body["content_type"] == "text/markdown"
    assert body["member"] == {"type": "file", "mode": 0o644, "size": 32}
    assert body["total_bytes"] == 32
    assert body["truncated"] is False and body["next_offset"] is None
    assert body["path"] == "progress/step-0001.md"
    assert body["artifact"] == {
        "name": "progress/step-0001.md", "bytes": 32,
        "uri": f"gs://{BUCKET}/{base}/archive.tar.gz",
    }
    assert body["redaction"]["applied_at_read_time"] is True
    for field in ("task_id", "tenant_id", "attempt_id", "detail", "key", "uri", "offset",
                  "returned_bytes", "redacted", "redaction_count"):
        assert field in body, field


def test_a_file_is_redacted_at_read_time(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([
        ("notes.txt", "file", f"the agent printed {GH_TOKEN} into its notes\n"),
    ]))

    body = one_file(client, "notes.txt").json()
    assert body["status"] == "ok"
    assert GH_TOKEN not in body["content"]
    assert "Aa1Bb2Cc3" not in body["content"]
    assert body["redacted"] is True and body["redaction_count"] >= 1


def test_windows_page_the_whole_file_and_never_split_a_token(client, db, objects):
    """Windows are cut on whitespace by the artifact route's own `_align`, so
    concatenating every page reproduces the file and no credential is ever
    served in two halves that each escape the redaction rules."""
    seed(db)
    text = "".join(f"line {n:05d} of the transcript\n" for n in range(900))
    put_checkpoint(objects, archive=tar_gz([("log.txt", "file", text)]))

    pages, offset, rounds = [], 0, 0
    while True:
        body = one_file(client, "log.txt", offset=offset, limit_bytes=4096).json()
        assert body["status"] == "ok", body
        pages.append(body["content"])
        rounds += 1
        assert rounds < 50, "the offset is not advancing"
        if body["next_offset"] is None:
            assert body["truncated"] is False
            break
        assert body["truncated"] is True
        assert "window" in body["detail"]
        offset = body["next_offset"]
    assert rounds > 1, "the file was served in one window; paging was not exercised"
    assert "".join(pages) == text


def test_the_size_cap_is_the_artifact_routes_own(client, db, objects, api_context):
    """Consistent BY CONSTRUCTION: the caps are read off the one
    `InspectionService` the artifact-content route uses, so moving its ceiling
    moves this one."""
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("big.txt", "file", "word " * 20000)]))
    api_context.inspection._max_artifact_bytes = 6000
    api_context.inspection._default_artifact_bytes = 5000

    capped = one_file(client, "big.txt", limit_bytes=10**9).json()
    assert 0 < capped["returned_bytes"] <= 6000
    assert capped["truncated"] is True

    default = one_file(client, "big.txt").json()
    assert 0 < default["returned_bytes"] <= 5000


def test_a_binary_file_is_reported_binary_with_no_bytes(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("data.txt", "file", b"abc\x00def" * 10)]))
    body = one_file(client, "data.txt").json()
    assert body["status"] == "binary"
    assert body["content"] is None
    assert body["total_bytes"] == 70


def test_a_type_off_the_allowlist_is_refused_by_name(client, db, objects):
    """Text inside a `.png` is still not served: the allowlist decides by
    name, before a byte is read."""
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("image.png", "file", "plain words, png name")]))
    body = one_file(client, "image.png").json()
    assert body["status"] == "binary"
    assert body["content"] is None
    assert body["content_type"] is None
    assert "allowlist" in body["detail"]


def test_an_extensionless_file_is_a_text_candidate(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("Makefile", "file", "all:\n\techo hi\n")]))
    body = one_file(client, "Makefile").json()
    assert body["status"] == "ok"
    assert body["content_type"] == "text/plain"
    assert body["content"] == "all:\n\techo hi\n"


def test_the_allowlist_decisions():
    assert content_type_for("notes.md") == "text/markdown"
    assert content_type_for("src/app.TS") == "text/x-typescript"
    assert content_type_for(".env") == "text/plain"
    assert content_type_for("archive.tar.gz") is None
    assert content_type_for("photo.jpg") is None
    assert content_type_for("lib.so") is None


def test_an_empty_file_is_a_real_empty_string(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=tar_gz([("empty.txt", "file", "")]))
    body = one_file(client, "empty.txt").json()
    assert body["status"] == "ok"
    assert body["content"] == "", "'' is a file the agent left blank, never null"
    assert body["total_bytes"] == 0


@pytest.mark.parametrize("path,kind", [("progress", "dir"), ("latest", "symlink")])
def test_a_directory_or_a_link_is_not_a_file(client, db, objects, path, kind):
    a_workspace_checkpoint(db, objects)
    response = one_file(client, path)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["type"] == kind


def test_a_file_the_archive_does_not_hold_is_a_404(client, db, objects):
    a_workspace_checkpoint(db, objects)
    response = one_file(client, "not/there.txt")
    assert response.status_code == 404, response.text
    assert "not/there.txt" in response.json()["message"]


def test_a_file_in_an_absent_archive_is_absent_not_empty(client, db, objects):
    seed(db)
    put_checkpoint(objects, archive=None)
    body = one_file(client, "state.json").json()
    assert body["status"] == "absent"
    assert body["content"] is None


def test_a_file_in_an_unreadable_archive_is_unreadable_not_empty(client, db, objects):
    base = a_workspace_checkpoint(db, objects)
    objects.fail_on(f"{base}/archive.tar.gz")
    body = one_file(client, "state.json").json()
    assert body["status"] == "unreadable"
    assert body["content"] is None
    assert "injected store failure" in body["detail"]


def test_a_file_beyond_the_scan_budget_is_not_called_missing(client, db, objects, budgets):
    """It may well be there. 413, and the message says how to get it."""
    seed(db)
    put_checkpoint(
        objects,
        archive=tar_gz(
            [(f"f{n:02d}.bin", "file", noise(64 * 1024, n)) for n in range(20)]
            + [("last.txt", "file", "the end")]
        ),
    )
    budgets(chunk_bytes=32 * 1024, max_scan_bytes=128 * 1024)
    response = one_file(client, "last.txt")
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "checkpoint_scan_budget_exceeded"
    assert "download" in response.json()["message"]


# --------------------------------------------------------------------------
# 6. The whole archive
# --------------------------------------------------------------------------

def test_the_whole_archive_downloads_as_an_attachment_in_windows(
    client, db, objects, budgets
):
    seed(db)
    archive = tar_gz([(f"f{n}.bin", "file", noise(128 * 1024, n)) for n in range(8)])
    put_checkpoint(objects, archive=archive, file_count=8)
    budgets(chunk_bytes=64 * 1024)
    objects.forget()

    response = download(client)
    assert response.status_code == 200, response.text
    assert response.content == archive
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="task_a-att_1-ckpt-00001.tar.gz"'
    )
    # CHUNKED, NOT `Content-Length`. Cloud Run refuses an HTTP/1 response over
    # 32 MiB unless it is chunked or streamed, and uvicorn -- HTTP/1 only here
    # -- chunks exactly when the application declares no length. The size
    # travels in its own header, where no proxy acts on it.
    assert "content-length" not in response.headers
    assert response.headers["x-checkpoint-bytes"] == str(len(archive))
    assert response.headers["x-swarm-redaction"] == "not-applied"
    assert response.headers["x-checkpoint-manifest"] == "present"
    assert response.headers["x-checkpoint-sha256"] == hashlib.sha256(archive).hexdigest()
    assert response.headers["cache-control"] == "no-store"
    reads = [n for k, _, n in objects.reads if k.endswith("archive.tar.gz")]
    assert len(reads) >= len(archive) // (64 * 1024)
    assert max(reads) <= 64 * 1024, "the archive was never held whole"
    # And it is the archive: it opens, and it holds what the listing says.
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        assert len(tar.getnames()) == 8


def test_an_absent_archive_is_a_404_not_an_empty_download(client, db, objects):
    """A 200 with no body would be saved as a zero-byte .tar.gz that looks
    like a download."""
    seed(db)
    put_checkpoint(objects, archive=None)
    response = download(client)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["manifest"] == "present"


def test_an_unreadable_archive_is_a_503_before_any_byte(client, db, objects):
    base = a_workspace_checkpoint(db, objects)
    objects.fail_on(f"{base}/archive.tar.gz")
    response = download(client)
    assert response.status_code == 503, response.text
    assert "content-disposition" not in response.headers


def test_a_failure_mid_download_ends_short_of_its_declared_size(
    client, db, objects, budgets
):
    """After the status line is sent it cannot change. What lets a client see
    the stream was cut, rather than save a short file that looks complete, is
    `X-Checkpoint-Bytes` (and the digest, when the manifest has one) -- and,
    on the wire, a chunked body that never receives its terminating chunk,
    which a browser reports as a failed download. Not `Content-Length`: see
    the test above for why the response may not carry one."""
    seed(db)
    archive = tar_gz([(f"f{n}.bin", "file", noise(64 * 1024, n)) for n in range(4)])
    base = put_checkpoint(objects, archive=archive)
    budgets(chunk_bytes=32 * 1024)
    objects.fail_after_first.add(f"{base}/archive.tar.gz")

    response = download(client)
    assert "content-length" not in response.headers
    assert int(response.headers["x-checkpoint-bytes"]) == len(archive)
    assert len(response.content) < len(archive)
    assert response.content == archive[: len(response.content)]


def test_an_uncommitted_checkpoint_downloads_and_says_it_was_never_committed(
    client, db, objects
):
    seed(db)
    archive = tar_gz(WORKSPACE)
    put_checkpoint(objects, archive=archive, manifest=False)
    response = download(client)
    assert response.status_code == 200
    assert response.headers["x-checkpoint-manifest"] == "absent"
    assert "x-checkpoint-sha256" not in response.headers


# --------------------------------------------------------------------------
# 7. Read-only, and wired
# --------------------------------------------------------------------------

def test_nothing_on_these_routes_writes(client, db, objects):
    """A checkpoint is the entire value of a parked attempt; only the
    reconciler may remove one."""
    a_workspace_checkpoint(db, objects)
    before_objects = dict(objects.objects)
    before_task = dict(db.docs["tasks/task_a"])

    assert files(client).status_code == 200
    assert one_file(client, "state.json").status_code == 200
    assert download(client).status_code == 200

    assert objects.objects == before_objects
    assert db.docs["tasks/task_a"] == before_task


def test_the_three_routes_are_published(client):
    paths = client.app.openapi()["paths"]
    for path in (
        "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files",
        "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path}",
        "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content",
    ):
        assert path in paths, sorted(paths)
        assert "get" in paths[path]


def test_the_ui_calls_exactly_the_paths_this_router_serves(client):
    """The seam check `test_runtimes_screen.py` runs over api.ts, run over the
    component that calls these routes -- CheckpointBrowser.tsx keeps its own
    loaders, so that check would not see them."""
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "apps/swarm-ui/src/CheckpointBrowser.tsx"
    ).read_text()
    called = {
        re.sub(r"(\$\{[^}]*\}|\{[^}]*\})", "{}", raw.split("?")[0])
        for raw in re.findall(r"""['"`](/v1/[^'"`]*)['"`]""", source)
    }
    assert len(called) >= 3, f"the scan found {called}; this check would compare nothing"
    served = {re.sub(r"\{[^}]*\}", "{}", p) for p in client.app.openapi()["paths"]}
    missing = sorted(called - served)
    assert not missing, f"CheckpointBrowser calls paths the API does not serve: {missing}"


def test_the_archive_builder_here_matches_the_workers_format():
    """`tar_gz` above must produce what `_write_archive` produces -- gzip over
    a POSIX tar -- or every listing test is about the wrong format."""
    blob = tar_gz(WORKSPACE)
    assert blob[:2] == b"\x1f\x8b", "gzip magic"
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:") as tar:
        assert tar.getnames()[:2] == ["progress", "progress/step-0001.md"]
