"""What leaves the pod is what was read, with no link followed, at the moment it left.

Wave 2026-09-27, lane 2, from the #227 epic's boxes:

* the artifacts upload, the redaction pass before it and the live tail each
  checked a path for a link and then opened it BY PATH, so a link put in the
  path between the check and the open was followed. The agent and the worker
  share a uid, and an agent process that outlived its runner (it runs in its
  own session) can make that swap at any time;
* the `rglob` walk over `$SWARM_ARTIFACTS_DIR` recursed once per folder, so a
  tree about 1,000 folders deep raised `RecursionError` and took the whole
  attempt's upload with it;
* the byte-cap and upload-error entries in `artifacts_skipped` added the raw
  name, where every other name the summary shows is scrubbed and then cut;
* a file redacted and then dropped by the byte cap was still reported in
  `redaction_skipped`, and logged, as "uploaded as-is".

Every swap below is made at a fixed point -- after the worker decided which
files to upload, or while it uploads one -- so each test is deterministic. A
link to a FOLDER is the case a leaf check never saw: `path.is_symlink()` is
False for `sub/report.txt` when `sub` is the link.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from agent_worker.expected_outputs import skipped_names
from agent_worker import artifact_manifest as manifest_mod
from agent_worker import workspace as workspace_mod
from agent_worker.standalone_outputs import SHOWN_NAME_CHARS

from worker_seeds import TENANT, build_worker, seed_attempt, seed_tenant
from fakes import FakeSecretClient

KEY = "sk-ant-supersecret-value-0123456789"
OUTSIDE = "the worker's own file, which the agent must never have uploaded\n"


def _worker(db, store, tmp_path, *, log=None, **overrides):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, config, _ = build_worker(
        db,
        store,
        tmp_path,
        log or io.StringIO(),
        runner_profile="claude-code",
        secret_client=FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": KEY}),
        **overrides,
    )
    worker.ws = ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._build_child_env()  # registers the key for redaction
    return worker, config, ws


def _outside(tmp_path: Path) -> Path:
    """A folder outside the workspace holding what a link would lead to."""
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "report.txt").write_text(OUTSIDE + f"and a key: {KEY}\n")
    return folder


def _swap_after_the_plan(monkeypatch, swap) -> None:
    """Run `swap` once the worker has decided which artifacts to upload."""
    real = manifest_mod.plan

    def plan(*args, **kwargs):
        decided = real(*args, **kwargs)
        swap()
        return decided

    monkeypatch.setattr(manifest_mod, "plan", plan)


def _every_object(store) -> dict[str, bytes]:
    return {key: store.download_bytes(key) for key in store.list_keys("")}


# ---------------------------------------------------------------------------
# (a) the artifacts upload and the redaction pass
# ---------------------------------------------------------------------------


def test_a_folder_swapped_for_a_link_after_the_plan_is_neither_uploaded_nor_rewritten(
    db, store, tmp_path, monkeypatch
):
    """`sub/report.txt` was a regular file when the worker walked the folder;
    `sub` is a link to a folder outside the workspace by the time it is read.
    Nothing behind the link reaches the bucket, and the redaction pass does not
    rewrite the file behind it either -- rewriting through a link is writing to
    wherever the agent pointed it."""
    worker, config, ws = _worker(db, store, tmp_path)
    outside = _outside(tmp_path)
    before = (outside / "report.txt").read_bytes()
    (ws.artifacts / "sub").mkdir()
    (ws.artifacts / "sub" / "report.txt").write_text("the agent's report\n")
    (ws.artifacts / "kept.txt").write_text("an ordinary artifact\n")

    def swap() -> None:
        (ws.artifacts / "sub" / "report.txt").unlink()
        (ws.artifacts / "sub").rmdir()
        (ws.artifacts / "sub").symlink_to(outside, target_is_directory=True)

    _swap_after_the_plan(monkeypatch, swap)
    summary = worker._upload_outputs()

    for key, body in _every_object(store).items():
        assert OUTSIDE.encode() not in body, f"{key} carries a file from behind a link"
    assert not store.exists(f"{config.artifact_prefix}/sub/report.txt")
    assert (outside / "report.txt").read_bytes() == before, (
        "the redaction pass rewrote a file outside the workspace through a link"
    )
    assert "sub/report.txt" in skipped_names(summary.get("artifacts_skipped")), summary
    # The positive control: the upload itself still works.
    assert store.download_bytes(f"{config.artifact_prefix}/kept.txt") == b"an ordinary artifact\n"


def test_a_file_swapped_for_a_link_after_the_plan_is_not_uploaded(
    db, store, tmp_path, monkeypatch
):
    worker, config, ws = _worker(db, store, tmp_path)
    outside = _outside(tmp_path)
    (ws.artifacts / "report.txt").write_text("the agent's report\n")

    def swap() -> None:
        (ws.artifacts / "report.txt").unlink()
        (ws.artifacts / "report.txt").symlink_to(outside / "report.txt")

    _swap_after_the_plan(monkeypatch, swap)
    summary = worker._upload_outputs()

    for key, body in _every_object(store).items():
        assert OUTSIDE.encode() not in body, f"{key} carries a file from behind a link"
    assert "report.txt" in skipped_names(summary.get("artifacts_skipped")), summary


def test_what_is_uploaded_is_what_was_read_even_if_a_link_appears_mid_upload(
    db, store, tmp_path, monkeypatch
):
    """The swap made while the upload call runs, the latest point there is. A
    worker that hands the store a PATH in the artifacts folder has the store
    open it after the swap; one that hands it the copy it read has not."""
    worker, config, ws = _worker(db, store, tmp_path)
    outside = _outside(tmp_path)
    (ws.artifacts / "report.txt").write_text("the agent's report\n")
    real_upload = store.upload_file
    swapped: list[str] = []

    def upload_file(key, source, content_type=None):
        if key.endswith("/report.txt") and not swapped:
            (ws.artifacts / "report.txt").unlink()
            (ws.artifacts / "report.txt").symlink_to(outside / "report.txt")
            swapped.append(key)
        return real_upload(key, source, content_type=content_type)

    monkeypatch.setattr(store, "upload_file", upload_file)
    worker._upload_outputs()

    assert swapped, "the upload never reached report.txt, so nothing was tested"
    body = store.download_bytes(f"{config.artifact_prefix}/report.txt")
    assert body == b"the agent's report\n", body


def test_an_agent_capture_swapped_for_a_link_mid_upload_is_not_followed_into_logs(
    db, store, tmp_path, monkeypatch
):
    """The agent CLI's captures are copied to `logs/` on every exit. The copy
    went up by path after a leaf check, so a link made between the two was
    followed."""
    from agent_worker.runners.streams import agent_stream_files

    worker, config, ws = _worker(db, store, tmp_path)
    outside = _outside(tmp_path)
    files = agent_stream_files("claude-code")
    assert files is not None
    capture = ws.artifacts / files.stderr
    capture.write_text("the agent's own stderr\n")
    real_upload = store.upload_file
    swapped: list[str] = []

    def upload_file(key, source, content_type=None):
        if key.endswith("/agent_stderr.log") and not swapped:
            capture.unlink()
            capture.symlink_to(outside / "report.txt")
            swapped.append(key)
        return real_upload(key, source, content_type=content_type)

    monkeypatch.setattr(store, "upload_file", upload_file)
    worker._upload_outputs()

    assert swapped, "the logs upload never reached the agent's stderr, so nothing was tested"
    body = store.download_bytes(f"{config.log_prefix}/agent_stderr.log")
    assert OUTSIDE.encode() not in body, body
    assert body == b"the agent's own stderr\n", body


def test_the_uploaded_artifact_is_redacted_and_the_local_worker_files_still_are(
    db, store, tmp_path
):
    """The positive control for the copy-then-redact upload: a key in an
    artifact never reaches the bucket, and the worker's own files -- the
    runner's stdout and `result.json`, which a later checkpoint archives -- are
    still rewritten where they lie."""
    worker, config, ws = _worker(db, store, tmp_path)
    (ws.artifacts / "notes").mkdir()
    (ws.artifacts / "notes" / "run.md").write_text(f"used {KEY}\n")
    ws.stdout_path.write_text(f"resolved ANTHROPIC_API_KEY={KEY}\n")
    ws.result_path.write_text(json.dumps({"status": "failed", "summary": f"used {KEY}"}))

    worker._upload_outputs()

    body = store.download_bytes(f"{config.artifact_prefix}/notes/run.md").decode()
    assert KEY not in body and "***REDACTED***" in body, body
    assert KEY not in store.download_bytes(f"{config.log_prefix}/stdout.log").decode()
    assert KEY not in ws.stdout_path.read_text()
    assert KEY not in ws.result_path.read_text()


def test_a_link_at_the_result_file_is_not_rewritten_through(db, store, tmp_path):
    """The in-place pass over the worker's own files reads without following a
    link, and puts the rewritten copy back without following one either."""
    worker, config, ws = _worker(db, store, tmp_path)
    outside = _outside(tmp_path)
    before = (outside / "report.txt").read_bytes()
    ws.result_path.unlink(missing_ok=True)
    ws.result_path.symlink_to(outside / "report.txt")

    worker._upload_outputs()

    assert (outside / "report.txt").read_bytes() == before
    assert ws.result_path.is_symlink(), "the link itself is the agent's; it is left alone"


# ---------------------------------------------------------------------------
# (a) the live tail
# ---------------------------------------------------------------------------


def test_the_live_tail_does_not_follow_a_folder_link_to_its_stream(db, store, tmp_path):
    """`logs/stdout.log` is not a link; `logs` is. The leaf check passed and
    the open followed the folder."""
    worker, config, ws = _worker(db, store, tmp_path)
    outside = tmp_path / "outside-logs"
    outside.mkdir()
    (outside / "stdout.log").write_text(OUTSIDE)
    moved = tmp_path / "moved-logs"
    ws.logs.rename(moved)
    ws.logs.symlink_to(outside, target_is_directory=True)

    worker._publish_tail("stdout", ws.stdout_path)

    assert not store.exists(f"{config.log_prefix}/live/stdout.tail.log"), (
        "the tail published a file from behind a folder link"
    )


def test_the_live_tail_still_publishes_a_regular_stream(db, store, tmp_path):
    worker, config, ws = _worker(db, store, tmp_path)
    ws.stdout_path.write_text(f"step 1 of 3, key {KEY}\n")

    worker._publish_tail("stdout", ws.stdout_path)

    body = store.download_bytes(f"{config.log_prefix}/live/stdout.tail.log").decode()
    assert "step 1 of 3" in body and KEY not in body, body


# ---------------------------------------------------------------------------
# (b) a deep tree
# ---------------------------------------------------------------------------

DEPTH = 1200


def _deep(root: Path) -> Path:
    """One `mkdir` per level: `os.makedirs` recurses per missing parent too."""
    deepest = root
    for _ in range(DEPTH):
        deepest = deepest / "d"
        os.mkdir(deepest)
    (deepest / "bottom.txt").write_text("at the bottom\n")
    return deepest


def _remove_deep(root: Path) -> None:
    """Without recursion (`shutil.rmtree` recurses per folder), and whatever
    part of the tree `_deep` got to make.

    The tree is under pytest's `tmp_path`, never a live workspace -- but in a
    SwarmCloud run `tmp_path` is itself under the attempt's TMPDIR, so a tree
    left behind here was one the worker's own teardown had to remove (#737).
    A run killed mid-test still leaves it; `workspace.destroy` no longer
    recurses, so that costs disk, not the attempt's exit code."""
    workspace_mod._remove_tree(root / "d")


def test_a_tree_a_thousand_folders_deep_does_not_abort_the_upload(db, store, tmp_path):
    """`Path.rglob` recursed once per folder on Python 3.11 and raised
    RecursionError about 1,000 deep, out of `_upload_outputs`, so none of the
    attempt's artifacts, logs or summary went up. Now the folder too deep to
    hold any storable name is listed as skipped, and the rest is uploaded."""
    worker, config, ws = _worker(db, store, tmp_path)
    (ws.artifacts / "ok.txt").write_text("an ordinary artifact\n")
    try:
        _deep(ws.artifacts)
        summary = worker._upload_outputs()
    finally:
        _remove_deep(ws.artifacts)

    assert store.download_bytes(f"{config.artifact_prefix}/ok.txt") == b"an ordinary artifact\n"
    skipped = skipped_names(summary.get("artifacts_skipped"))
    deep = [name for name in skipped if name.startswith("d/d/d/")]
    assert deep, skipped
    assert all(len(name) <= SHOWN_NAME_CHARS + 3 for name in deep), [len(n) for n in deep]


def test_a_deep_tree_does_not_stop_the_expected_outputs_check_before_the_upload(
    db, store, tmp_path
):
    """`_finalise` asks `_publish_withheld` which declared outputs are missing
    BEFORE `_upload_outputs` runs, and it walked the folder with `rglob` too:
    the same RecursionError, one step earlier, with nothing uploaded."""
    worker, config, ws = _worker(db, store, tmp_path)
    worker._expected_outputs = ("notes.md",)
    (ws.artifacts / "notes.md").write_text("the declared output\n")
    try:
        _deep(ws.artifacts)
        withheld = worker._publish_withheld(True)
    finally:
        _remove_deep(ws.artifacts)

    assert withheld is None, withheld


# ---------------------------------------------------------------------------
# (c) scrub-then-cut for the byte-cap and upload-error entries
# ---------------------------------------------------------------------------


def _declared_long_name() -> str:
    """A name over the manifest's 256-byte bound, with the key across the cut.

    Declared names are exempt from the bound (#232 review), so this one is
    taken, and reaches the byte-cap and upload-error paths."""
    body = "a" * 230 + KEY + "z" * 36
    return body[:150] + "/" + body[150:] + ".txt"


def test_a_name_over_the_byte_cap_is_scrubbed_then_cut(db, store, tmp_path):
    name = _declared_long_name()
    worker, config, ws = _worker(db, store, tmp_path, max_artifact_bytes=4)
    worker._expected_outputs = (name,)
    (ws.artifacts / name).parent.mkdir(parents=True)
    (ws.artifacts / name).write_text("more than four bytes\n")

    summary = worker._upload_outputs()

    skipped = skipped_names(summary.get("artifacts_skipped"))
    assert skipped, summary
    for entry in skipped:
        assert len(entry) <= SHOWN_NAME_CHARS + 3, len(entry)
        assert KEY[:12] not in entry, entry
        assert "sk-ant" not in entry, entry


def test_a_name_whose_upload_failed_is_scrubbed_then_cut(db, store, tmp_path, monkeypatch):
    name = _declared_long_name()
    worker, config, ws = _worker(db, store, tmp_path)
    worker._expected_outputs = (name,)
    (ws.artifacts / name).parent.mkdir(parents=True)
    (ws.artifacts / name).write_text("the report\n")
    real_upload = store.upload_file

    def upload_file(key, source, content_type=None):
        if "/artifacts/" in f"/{key}" and key.endswith(".txt"):
            raise OSError("the bucket said no")
        return real_upload(key, source, content_type=content_type)

    monkeypatch.setattr(store, "upload_file", upload_file)
    summary = worker._upload_outputs()

    skipped = skipped_names(summary.get("artifacts_skipped"))
    assert skipped, summary
    for entry in skipped:
        assert len(entry) <= SHOWN_NAME_CHARS + 3, len(entry)
        assert "sk-ant" not in entry, entry


# ---------------------------------------------------------------------------
# a file dropped by the byte cap never left, so it is not "uploaded as-is"
# ---------------------------------------------------------------------------


def test_a_file_dropped_by_the_byte_cap_is_not_reported_as_uploaded_unredacted(
    db, store, tmp_path
):
    """#227: taken by the file cap, examined by the redaction pass -- which
    cannot rewrite a binary file, found the key and logged it as uploaded
    as-is -- and then dropped by the BYTE cap. It never left the pod."""
    log = io.StringIO()
    worker, config, ws = _worker(db, store, tmp_path, log=log, max_artifact_bytes=64)
    (ws.artifacts / "a.txt").write_text("a\n")
    (ws.artifacts / "z.bin").write_bytes(
        b"\x7fELF\x00\xff\xfe" + KEY.encode("utf-8") + b"\x00\xff" * 64
    )

    summary = worker._upload_outputs()

    assert not store.exists(f"{config.artifact_prefix}/z.bin")
    assert "z.bin" in skipped_names(summary.get("artifacts_skipped")), summary
    unredacted = [entry.get("file") for entry in summary.get("redaction_skipped") or []]
    assert not [name for name in unredacted if name and name.endswith("z.bin")], unredacted
    assert "uploaded as-is" not in log.getvalue()
