"""A checkpoint does not carry the task's input, and a resume does not need it to.

THE PR #229 REVIEW. The worker writes the whole task input to `work/input.json`
(`AgentLifecycle._prepare`) and every checkpoint archived `work/`. The API then
served it back out of every checkpoint -- the whole archive byte for byte, and
the file view through the rules over its text only -- while every task route
serves the input masked: a list of tokens, a value only the metadata named as
a secret, and a password in the prompt all came out of the file view in the
clear, under `masked 1`.

Leaving it out loses nothing: `_prepare` writes `input.json` from the task
document at every attempt, AFTER the restore (STEP 4), so a resumed attempt
reads the one it wrote. `test_checkpoint.py`'s resume test runs that path
through the whole worker; this file holds the archive's contents.

MUTATIONS: drop `ws.input_path` from `create`'s skip set (the archive and the
file count go red); skip it by NAME anywhere in the tree (the nested
`notes/input.json` control goes red).
"""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.logs import build_logger

from worker_seeds import TENANT

SECRET = "zork-grue-lantern-brass-4471"


def _logger(log_stream):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=log_stream,
    )


def _manager(store, logger, attempt_id="att_1", generation=1):
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id,
        generation=generation, logger=logger,
    )


def test_the_archive_holds_the_work_and_not_the_input(store, tmp_path, log_stream):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    ws.input_path.write_text(json.dumps({"prompt": f"deploy with {SECRET}"}, indent=2))
    (ws.work / "state.json").write_text('{"completed_steps": 1}')
    # A file the AGENT named input.json, deeper in its own tree, is its work.
    (ws.work / "notes").mkdir()
    (ws.work / "notes" / "input.json").write_text('{"mine": true}')

    record = _manager(store, _logger(log_stream)).create(ws)

    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = archive.getnames()
    assert "input.json" not in names, f"the task's input was archived: {names}"
    assert "state.json" in names and "notes/input.json" in names, names
    assert SECRET.encode() not in gzip.decompress(data), "the input's bytes are in the archive"
    assert record.file_count == 2, "state.json and the agent's own notes/input.json"


def test_a_resume_from_that_archive_starts_with_no_input_for_the_worker_to_write(
    store, tmp_path, log_stream
):
    """`restore` refuses a non-empty `work/`, and `_prepare` writes input.json
    after it: what matters is that the restore leaves the name free."""
    logger = _logger(log_stream)
    original = workspace_mod.create(tmp_path / "ws", "att_1")
    original.input_path.write_text('{"prompt": "first attempt"}')
    (original.work / "state.json").write_text('{"completed_steps": 1}')
    _manager(store, logger).create(original)

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, logger, attempt_id="att_2", generation=2)
    found = manager.find_latest()
    assert found is not None
    assert manager.restore(found, resumed) == 1
    assert (resumed.work / "state.json").read_text() == '{"completed_steps": 1}'
    assert not resumed.input_path.exists(), "a restored input.json would be the previous attempt's"


def test_a_hard_link_to_the_input_or_from_outside_is_archived_as_a_copy_knowingly(
    store, tmp_path, log_stream
):
    """PINNED, not a defect fixed (#227). A hard link from outside `work/`,
    or to `input.json` under another name, is archived as a regular file of
    its content under its name here: what the agent copying the file in would
    produce, which it may do with any file its uid reads. Accepted, and
    recorded in `_add_entry`'s docstring. A change that refuses or drops such
    a link should change this test."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    ws.input_path.write_text('{"prompt": "linked under a new name"}')
    outside = tmp_path / "outside.txt"
    outside.write_text("from outside work\n")
    os.link(outside, ws.work / "from-outside.txt")
    os.link(ws.input_path, ws.work / "copy.json")

    record = _manager(store, _logger(log_stream)).create(ws)

    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = {m.name: m for m in archive.getmembers()}
        bodies = {
            name: archive.extractfile(m).read() for name, m in members.items() if m.isreg()
        }
    assert "input.json" not in members
    assert members["from-outside.txt"].isreg(), "archived as the file, not a link"
    assert bodies["from-outside.txt"] == b"from outside work\n"
    assert members["copy.json"].isreg()
    assert bodies["copy.json"] == b'{"prompt": "linked under a new name"}'
