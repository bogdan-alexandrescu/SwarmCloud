"""A browser session copied into `work/` never leaves the pod (LB-L4b, part of #1030).

docs/design/live-browser.md section 5.5: the live browser's profile lives on
a sidecar-only memory volume, never under `work/`, so the checkpoint never
archives it. But the agent drives that browser over CDP, and an agent can call
Playwright's `storageState()` and write the JSON into its workspace, or copy a
Chromium `Cookies` SQLite database there. The checkpoint and the outputs would
then carry the signed-in session of the person who took control out of the
pod, into the tenant's bucket and a reader's download.

The design's answer, and what each test holds: the checkpoint filter and the
output upload drop a file with the SHAPE of a Playwright storage state (a JSON
object whose `cookies` entries carry `name`, `value` and `domain`) or of a
Chromium `Cookies` database, and record `browser_state_refused` with its path.
It is a shape check, not a proof, and the design says so; the controls below
are what keeps it from refusing an ordinary file that merely mentions cookies.

Every cookie value is built at run time (`secrets.token_hex`): nothing here
may look like a credential to the worker's own publish scan.

The event name and the reason are spelled out, not imported: they are what an
operator searches the log for and what a reader finds in the result summary.

MUTATIONS: make `artifact_manifest.browser_state_kind` return None (every
refusal test goes red, the controls stay green); drop the check from
`CheckpointManager._add_entry` (the archive tests go red); drop it from
`Worker._upload_copy` (the artifacts-folder test goes red) or from
`_upload_workdir_outputs` (the working-folder test goes red); leave the
refused inode in `tar.inodes` (the hard-link test fails its restore).
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Any

from agent_worker import artifact_manifest as manifest
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger

from worker_seeds import TENANT
from test_standalone_outputs import (  # noqa: F401 - `agent_cli` is a fixture
    _names,
    _run,
    _seed,
    _succeeded,
    _summary,
    agent_cli,
)

EVENT = "browser_state_refused"

#: Chromium's `Cookies` schema, as a recent Chromium writes it.
CHROMIUM_COOKIES_SCHEMA = (
    "CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);"
    "CREATE TABLE cookies(creation_utc INTEGER NOT NULL,host_key TEXT NOT NULL,"
    "top_frame_site_key TEXT NOT NULL,name TEXT NOT NULL,value TEXT NOT NULL,"
    "encrypted_value BLOB NOT NULL,path TEXT NOT NULL,expires_utc INTEGER NOT NULL,"
    "is_secure INTEGER NOT NULL,is_httponly INTEGER NOT NULL,last_access_utc INTEGER NOT NULL,"
    "has_expires INTEGER NOT NULL,is_persistent INTEGER NOT NULL,priority INTEGER NOT NULL,"
    "samesite INTEGER NOT NULL,source_scheme INTEGER NOT NULL,source_port INTEGER NOT NULL,"
    "last_update_utc INTEGER NOT NULL);"
    "CREATE UNIQUE INDEX cookies_unique_index ON cookies(host_key, top_frame_site_key, "
    "name, path, source_scheme, source_port);"
)


def _cookie_value() -> str:
    return secrets.token_hex(16)


def storage_state(value: str) -> bytes:
    """What `context.storageState({path})` writes, with one session cookie."""
    return json.dumps(
        {
            "cookies": [
                {
                    "name": "user_session",
                    "value": value,
                    "domain": "github.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        },
        indent=2,
    ).encode("utf-8")


def cookies_db(path: Path, value: str) -> bytes:
    """A Chromium `Cookies` database holding one cookie, built here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(CHROMIUM_COOKIES_SCHEMA)
        connection.execute(
            "INSERT INTO cookies VALUES (1,'.github.com','','user_session',?,x'',"
            "'/',0,1,1,0,0,0,1,1,2,443,0)",
            (value,),
        )
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def plain_db(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript("CREATE TABLE results(id INTEGER, score REAL);")
        connection.execute("INSERT INTO results VALUES (1, 0.5)")
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def _kind(name: str, data: bytes) -> str | None:
    return manifest.browser_state_kind(name, io.BytesIO(data))


def _logger(log_stream: Any):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="claude-code-browser", stream=log_stream,
    )


def _manager(store: Any, logger: Any) -> CheckpointManager:
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=logger,
    )


def _events(log_stream: io.StringIO) -> list[dict[str, Any]]:
    records = []
    for line in log_stream.getvalue().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == EVENT:
            records.append(record)
    return records


def _archived(store: Any, record: Any) -> tuple[list[str], bytes]:
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = archive.getnames()
        content = b"".join(
            archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
            if member.isreg()
        )
    return names, content


# ---------------------------------------------------------------------------
# the shape check
# ---------------------------------------------------------------------------


def test_a_playwright_storage_state_has_the_shape(tmp_path):
    assert _kind("auth.json", storage_state(_cookie_value())) is not None
    # Pretty or compact, with a BOM, under any name.
    compact = json.dumps(json.loads(storage_state(_cookie_value()))).encode()
    assert _kind("x.txt", b"\xef\xbb\xbf  \n" + compact) is not None
    # An empty jar beside localStorage is still Playwright's storage state.
    origins = json.dumps(
        {"cookies": [], "origins": [{"origin": "https://example.test", "localStorage": []}]}
    ).encode()
    assert _kind("state.json", origins) is not None


def test_a_bare_cookie_list_has_the_shape():
    """`context.cookies()` dumped as it is: the same jar without the wrapper."""
    jar = json.dumps([{"name": "sid", "value": _cookie_value(), "domain": ".example.test"}])
    assert _kind("cookies.json", jar.encode()) is not None


def test_a_chromium_cookies_database_has_the_shape_under_any_name(tmp_path):
    value = _cookie_value()
    data = cookies_db(tmp_path / "Default" / "Cookies", value)
    assert _kind("Cookies", data) is not None
    assert _kind("backup.sqlite", data) is not None


def test_ordinary_files_do_not_have_the_shape(tmp_path):
    """The controls: each mentions cookies or is a database, and is work."""
    assert _kind("recipe.json", json.dumps({"cookies": "chocolate chip"}).encode()) is None
    assert _kind("a.json", json.dumps({"cookies": [{"name": "oat", "grams": 30}]}).encode()) is None
    assert _kind("package.json", json.dumps({"name": "x", "version": "1.0.0"}).encode()) is None
    assert _kind("dns.json", json.dumps([{"name": "a", "domain": "b"}]).encode()) is None
    assert _kind("notes.md", b"# Cookies\n\nname, value and domain are the cookie's fields.\n") is None
    assert _kind("results.db", plain_db(tmp_path / "results.db")) is None
    assert _kind("empty", b"") is None
    assert _kind("broken.json", b'{"cookies": [{"name": "a", "value": "b", "domain": ') is None


# ---------------------------------------------------------------------------
# the checkpoint
# ---------------------------------------------------------------------------


def test_the_checkpoint_leaves_both_out_and_records_browser_state_refused(
    store, tmp_path, log_stream, monkeypatch
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    state_value, db_value = _cookie_value(), _cookie_value()
    (ws.work / "auth").mkdir()
    (ws.work / "auth" / "state.json").write_bytes(storage_state(state_value))
    cookies_db(ws.work / "profile" / "Default" / "Cookies", db_value)
    (ws.work / "notes.md").write_text("signed in; see the report\n")
    (ws.work / "recipe.json").write_text(json.dumps({"cookies": "chocolate chip"}))
    # Settled, as `test_checkpoint_incremental._settled` does it: a file written
    # inside the racy window is taken again next time, whatever it holds.
    from agent_worker import checkpoint as checkpoint_mod

    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 50_000_000)
    time.sleep(0.1)

    manager = _manager(store, _logger(log_stream))
    record = manager.create(ws)

    names, content = _archived(store, record)
    assert "auth/state.json" not in names and "profile/Default/Cookies" not in names, names
    assert "notes.md" in names and "recipe.json" in names, names
    assert state_value.encode() not in content and db_value.encode() not in content
    assert record.file_count == 2, names

    events = _events(log_stream)
    assert len(events) == 1, events
    assert sorted(events[0]["paths"]) == ["auth/state.json", "profile/Default/Cookies"]
    assert events[0]["refused"] == 2

    # Refused files are not "changed" forever: the next periodic checkpoint
    # of the same tree writes nothing, rather than re-reading and re-logging.
    assert manager.create_if_changed(ws) is None
    assert manager.last_unchanged


def test_a_second_name_of_a_refused_file_is_refused_too_and_the_archive_restores(
    store, tmp_path, log_stream
):
    """`tarfile` remembers the inode of every file it was handed. Left
    remembered, a hard link to a refused file would become a link member
    pointing at a member that is not in the archive."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    value = _cookie_value()
    (ws.work / "a.json").write_bytes(storage_state(value))
    os.link(ws.work / "a.json", ws.work / "b.json")
    (ws.work / "keep.txt").write_text("work\n")

    logger = _logger(log_stream)
    record = _manager(store, logger).create(ws)
    names, content = _archived(store, record)
    assert names == ["keep.txt"], names
    assert value.encode() not in content

    fresh = workspace_mod.create(tmp_path / "ws2", "att_2")
    restorer = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_2",
        generation=2, logger=logger,
    )
    restorer.restore(record, fresh)
    assert (fresh.work / "keep.txt").read_text() == "work\n"
    assert not (fresh.work / "a.json").exists() and not (fresh.work / "b.json").exists()


def test_a_state_written_after_the_first_checkpoint_is_left_out_of_the_next(
    store, tmp_path, log_stream
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "notes.md").write_text("one\n")
    manager = _manager(store, _logger(log_stream))
    manager.create(ws)

    value = _cookie_value()
    (ws.work / "state.json").write_bytes(storage_state(value))
    record = manager.create_if_changed(ws)
    assert record is not None
    names, content = _archived(store, record)
    assert "state.json" not in names and value.encode() not in content
    assert [e["paths"] for e in _events(log_stream)] == [["state.json"]]


# ---------------------------------------------------------------------------
# the outputs
# ---------------------------------------------------------------------------


def test_the_outputs_leave_both_out_and_record_browser_state_refused(
    db, store, worker_factory, agent_cli, log_stream, tmp_path
):
    """A task with no repository: its working folder's new files are outputs,
    and so is everything in `$SWARM_ARTIFACTS_DIR`."""
    value = _cookie_value()
    db_bytes = cookies_db(tmp_path / "scratch" / "Cookies", _cookie_value())
    state = storage_state(value).decode()
    _seed(
        db,
        {
            "write": {"answer.md": "done\n", "auth/state.json": state},
            "write_binary": {"profile/Cookies": db_bytes.hex()},
            "write_artifact": {"report.md": "report\n", "session.json": state},
        },
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)

    names = _names(db)
    assert "workdir/answer.md" in names and "report.md" in names, names
    for refused in ("workdir/auth/state.json", "workdir/profile/Cookies", "session.json"):
        assert refused not in names, names
    listed = {e["name"]: e["reason"] for e in _summary(db)["workdir_outputs"]["not_uploaded"]}
    assert listed == {"workdir/auth/state.json": EVENT, "workdir/profile/Cookies": EVENT}, listed

    uploaded = b"".join(
        path.read_bytes() for path in (tmp_path / "gcs").rglob("*") if path.is_file()
    )
    assert value.encode() not in uploaded, "the session cookie left the pod"

    paths = sorted(path for event in _events(log_stream) for path in event.get("paths", [event.get("path")]))
    assert "artifacts/session.json" in paths, paths
    assert "workdir/auth/state.json" in paths and "workdir/profile/Cookies" in paths, paths
