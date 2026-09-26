"""`./artifacts` in the agent's working directory IS the uploaded directory (#149).

Measured on 2026-09-25, third run `wf_06a3a949d2c242c3b0e9`. Every prompt told
the agent to put its file in `$SWARM_ARTIFACTS_DIR` and to run
`echo $SWARM_ARTIFACTS_DIR` to find it. scan-02 (`task_d5b148621c2641939826`,
SUCCEEDED) echoed the right answer, `/workspace/att_x/artifacts`, and then wrote
`/workspace/att_x/work/artifacts/scan-02.md`: it put `work/` in front of the path
it had just read. Its checkpoint held the file inside the working directory,
`result_summary.artifacts` held only the runner's three logs, and merge-1 failed
at staging. The agent's final message said it had written the file to
`$SWARM_ARTIFACTS_DIR`. It believed it had.

The artifacts directory is the working directory's SIBLING, so `./artifacts` is
the natural wrong guess. The owner's decision on #149: the worker makes
`work/artifacts` a symlink to the real artifacts directory before the agent
starts, so that guess lands in the directory that is uploaded.

What these tests hold, each against a real worker or a real checkpoint:

  * the link exists when the agent starts, and points at `$SWARM_ARTIFACTS_DIR`;
  * a file the agent writes through `./artifacts/<name>`, relative to its own
    working directory, is in the upload manifest once, and is not reported
    missing;
  * the checkpoint neither archives the link nor follows it. Archiving the link
    would break every resume, because `checkpoint._safe_members` refuses a link
    that leaves `work/`. Following it would archive the artifacts a second time;
  * a `work/artifacts` that already exists, here restored from a checkpoint
    taken before the link existed, is left alone and the skip is logged.

The stand-in agent is a real executable started through `CLAUDE_CODE_BIN`, the
way `test_agent_seam_end_to_end.py` starts one. It writes RELATIVE to the
directory the runner started it in, which is the whole point: that is what the
real agent did.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

PROFILE = "claude-code"

#: The runner writes these into `artifacts/` itself on every claude-code attempt.
RUNNER_OWN_ARTIFACTS = {
    "claude-code.stdout.log",
    "claude-code.stderr.log",
    "claude-transcript.json",
}

#: What the stand-in writes, and where, relative to its working directory: the
#: exact shape of scan-02's Write call.
SCAN_NAME = "scan-02.md"
SCAN_TEXT = "scan two: marker-5c21e0\n"

#: `result_summary` key naming what the attempt did not produce.
MISSING_KEY = "expected_outputs_missing"

#: A stand-in for `claude --print --output-format json`.
#:
#: The prompt is the last argv element. It starts with a JSON plan, and may be
#: followed by the instructions the claude-code runner appends when later steps
#: expect files from this one, so only the leading JSON value is decoded.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

try:
    plan, _end = json.JSONDecoder().raw_decode(sys.argv[-1])
except (ValueError, IndexError):
    plan = {}

link = pathlib.Path("artifacts")
out = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "done",
    "cwd_is_work_dir": os.path.samefile(os.getcwd(), os.environ["SWARM_WORK_DIR"]),
    "link_is_symlink": link.is_symlink(),
    "link_target": os.readlink(link) if link.is_symlink() else None,
    "artifacts_dir": os.environ.get("SWARM_ARTIFACTS_DIR"),
}

# Relative to the current directory, which the runner set to the working
# directory. Never through $SWARM_ARTIFACTS_DIR: this is the agent that guessed.
for name, text in sorted((plan.get("write_relative") or {}).items()):
    path = pathlib.Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

print(json.dumps(out))
"""


class _Quiet:
    def info(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...


@pytest.fixture
def agent_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def _seed_agent(db: Any, plan: dict[str, Any], *, expected: list[str] | None = None) -> None:
    seed_attempt(db, runner_profile=PROFILE, task_input={"prompt": json.dumps(plan)})
    # Without the provider on the tenant, the task parks for a missing
    # credential before the agent starts, and every assertion below is vacuous.
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    if expected is not None:
        db.doc("tasks/task_1")["metadata"] = {"expected_outputs": list(expected)}


def _run(worker_factory: Any) -> int:
    worker, _config, _exporter = worker_factory(runner_profile=PROFILE)
    return worker.run()


def _summary(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")["result_summary"] or {}


def _manifest_names(db: Any) -> list[str]:
    return [entry["name"] for entry in _summary(db).get("artifacts", [])]


def _agent_saw(db: Any) -> dict[str, Any]:
    output = (_summary(db).get("runner") or {}).get("output") or {}
    assert isinstance(output, dict), f"the runner envelope was truncated: {output!r}"
    return output.get("structured_output") or {}


def _final_archive(store: LocalObjectStore, attempt_id: str = "att_1") -> tarfile.TarFile:
    """The last checkpoint of `work/` this attempt wrote.

    Taken in `_finalise`, after the agent exited, so the link is on disk when
    it is taken.
    """
    keys = sorted(
        key
        for key in store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/{attempt_id}/")
        if key.endswith("/archive.tar.gz")
    )
    assert keys, "the attempt wrote no checkpoint"
    return tarfile.open(fileobj=io.BytesIO(store.download_bytes(keys[-1])), mode="r:gz")


def _records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    out = []
    for line in log_stream.getvalue().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# the link, and a write through it
# ---------------------------------------------------------------------------


def test_the_agent_starts_with_work_artifacts_linked_to_the_artifacts_dir(
    db, worker_factory, agent_cli
):
    _seed_agent(db, {})

    assert _run(worker_factory) == ExitCode.OK

    saw = _agent_saw(db)
    # The agent ran where the runner says it runs, so `./artifacts` below is
    # `<work>/artifacts` and not some other directory's.
    assert saw["cwd_is_work_dir"] is True
    assert saw["link_is_symlink"] is True, (
        "the agent's working directory has no artifacts link, so its natural "
        "guess ./artifacts/<name> is a plain directory nobody uploads"
    )
    # The same absolute path the agent is told in $SWARM_ARTIFACTS_DIR, so
    # `readlink artifacts` and `echo $SWARM_ARTIFACTS_DIR` agree.
    assert saw["link_target"] == saw["artifacts_dir"]
    assert os.path.isabs(saw["link_target"])


def test_a_file_written_to_dot_artifacts_is_uploaded_once_and_not_reported_missing(
    db, store, worker_factory, agent_cli
):
    """scan-02's exact write, on a task a later step stages scan-02.md from."""
    _seed_agent(
        db,
        {"write_relative": {f"artifacts/{SCAN_NAME}": SCAN_TEXT}},
        expected=[SCAN_NAME],
    )

    assert _run(worker_factory) == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value

    names = _manifest_names(db)
    # Once, at the top of the manifest, under the name a dependant stages.
    # Not also as `artifacts/scan-02.md`, which is what an upload that walked
    # the link from inside `artifacts/` would add.
    assert names.count(SCAN_NAME) == 1, names
    assert set(names) == RUNNER_OWN_ARTIFACTS | {SCAN_NAME}, names
    key = f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/{SCAN_NAME}"
    assert store.download_bytes(key).decode("utf-8") == SCAN_TEXT
    # And the end-of-attempt check agrees the file arrived.
    assert MISSING_KEY not in _summary(db)


def test_the_checkpoint_holds_neither_the_link_nor_a_second_copy_of_the_artifacts(
    db, store, worker_factory, agent_cli
):
    _seed_agent(db, {"write_relative": {f"artifacts/{SCAN_NAME}": SCAN_TEXT}})

    assert _run(worker_factory) == ExitCode.OK

    with _final_archive(store) as archive:
        names = archive.getnames()
    assert "input.json" in names, f"not the archive of work/: {names}"
    under_artifacts = [n for n in names if n == "artifacts" or n.startswith("artifacts/")]
    assert under_artifacts == [], (
        "the checkpoint carries the artifacts link or what is behind it. The link "
        "points outside work/, so a resume refuses the whole archive; the files "
        f"behind it are uploaded already. Found: {under_artifacts}"
    )


def test_a_checkpoint_taken_with_the_link_in_place_restores_into_a_fresh_workspace(
    store, tmp_path, log_stream
):
    """The resume path, which the link must not break.

    `checkpoint._safe_members` refuses an archive holding a link that resolves
    outside `work/`. An archived artifacts link would do exactly that, so every
    attempt that resumes from a checkpoint would fail at restore.
    """
    logger = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile=PROFILE, stream=log_stream,
    )
    original = workspace_mod.create(tmp_path / "ws", "att_1")
    (original.work / "notes-in-progress.md").write_text("half done\n")
    (original.artifacts / SCAN_NAME).write_text(SCAN_TEXT)
    # The link as the worker makes it: absolute, to this attempt's artifacts.
    os.symlink(str(original.artifacts), original.work / "artifacts", target_is_directory=True)

    record = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=logger,
    ).create(original)
    assert record.file_count == 1, "only the working file; not the link, not the artifact"

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_2",
        generation=2, logger=logger,
    )
    found = manager.find_latest()
    assert found is not None
    assert manager.restore(found, resumed) == 1
    assert (resumed.work / "notes-in-progress.md").read_text() == "half done\n"
    # The resumed attempt makes its own link, to its own artifacts directory.
    assert not os.path.lexists(resumed.work / "artifacts")


# ---------------------------------------------------------------------------
# a work/artifacts that is already there
# ---------------------------------------------------------------------------


def test_an_existing_work_artifacts_is_left_alone_and_the_skip_is_logged(
    db, store, tmp_path, worker_factory, agent_cli, log_stream
):
    """A checkpoint from before the link existed, whose agent made
    `work/artifacts` itself: scan-02's own checkpoint, resumed.

    The directory is the agent's work and is restored as it was. It is not
    replaced by a link, so nothing in it is lost, and the one line that says so
    is what explains, later, why a file written under it was not uploaded.
    """
    old = workspace_mod.create(tmp_path / "old-ws", "att_0")
    (old.work / "artifacts").mkdir()
    (old.work / "artifacts" / SCAN_NAME).write_text("restored\n")
    CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_0",
        generation=1, logger=_Quiet(),
    ).create(old)
    _seed_agent(db, {"write_relative": {"artifacts/after-resume.md": "new\n"}})

    assert _run(worker_factory) == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value

    saw = _agent_saw(db)
    assert saw["link_is_symlink"] is False, "the restored directory was replaced by a link"
    with _final_archive(store) as archive:
        directory = archive.getmember("artifacts")
        assert directory.isdir() and not directory.issym()
        restored = archive.extractfile(f"artifacts/{SCAN_NAME}")
        assert restored is not None and restored.read() == b"restored\n"
        assert "artifacts/after-resume.md" in archive.getnames()

    # What was already true before the link, and still is for this directory:
    # a file under it is not an artifact under its own name, so no later step
    # stages it. This task has no repository, so what its agent created in
    # the working folder is uploaded as `workdir/<path>` (#184, owner decision
    # of 2026-09-26; test_standalone_outputs.py) -- the restored file too,
    # because an earlier attempt's agent created it.
    names = _manifest_names(db)
    assert "after-resume.md" not in names
    assert "workdir/artifacts/after-resume.md" in names, names
    assert "workdir/artifacts/scan-02.md" in names, names

    skipped = [
        r
        for r in _records(log_stream)
        if r.get("severity") == "WARNING"
        and "work/artifacts" in str(r.get("message", ""))
        and "not linked" in str(r.get("message", ""))
    ]
    assert len(skipped) == 1, [r.get("message") for r in _records(log_stream)]
