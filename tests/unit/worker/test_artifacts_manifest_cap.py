"""The artifacts manifest is capped at 500 files (#228).

Found by reading on 2026-09-26: `Worker._upload_outputs` put one manifest entry
into `result_summary.artifacts` for EVERY file under `$SWARM_ARTIFACTS_DIR`
that fitted `max_artifact_bytes`, with no count cap. `result_summary` is a
field of the task's Firestore document, which Firestore limits to 1 MiB, so an
agent that left about 6,000 small files there would have taken the document
past it: `finish` refused, `_safe_finish` refused again, and the task lost a
result that may well have succeeded. Since #225 every claude-code and codex
prompt names the folder, so more agents write there.

The owner's decision, recorded on #228 the same day, and what each test holds:

* At most 500 files are uploaded from the folder per attempt. The rest are not
  uploaded; they are COUNTED in `result_summary`, and every one of their names
  goes to the worker's log, 100 names per WARNING line -- the shape #225 gave
  the working-folder cap.
* The order is deterministic, and a name a later step's `expected_outputs`
  declares comes first, so a declared output is never the one dropped.
* The document cannot pass 1 MiB even at the cap: a manifest name is bounded,
  as #225 bounded the names it lists.

Every test here runs the production worker and the production claude-code
runner, with a stand-in agent started through `CLAUDE_CODE_BIN` the way
`test_standalone_outputs.py` does. This one writes numbered SERIES of files
into `$SWARM_ARTIFACTS_DIR`: a plan naming 1,200 files of up to 700 bytes each
would be one argument past Linux's 128 KiB limit on a single argv string.

The keys and the wording are spelled out rather than imported: each is a field
of a document that outlives the process that wrote it, or a line an operator
searches for.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from swarm_common.config import Settings

from conftest import BUCKET, TENANT, build_worker, seed_attempt, seed_tenant
from fakes import FakeSecretClient
from test_standalone_outputs import (
    PROFILE,
    RUNNER_OWN_ARTIFACTS,
    _logged,
    _names,
    _run,
    _seed,
    _succeeded,
    _summary,
)

#: The owner's number, 2026-09-26 on #228.
CAP = 500

#: `result_summary` keys: how many files the folder held past the cap, and the cap.
OVER_CAP_KEY = "artifacts_over_cap"
CAP_KEY = "artifacts_cap_files"

#: The owner's words for a file that did not fit, as #225 used them.
OVER_CAP = "over cap"

#: The longest manifest name, in bytes of UTF-8, and the reason a longer one
#: is given when it is not uploaded.
MAX_NAME_BYTES = 256
NAME_TOO_LONG = "name is longer than the manifest's 256 bytes"

#: The worker's WARNING line naming the files in the folder it did not upload.
NOT_UPLOADED_LINE = "files in $SWARM_ARTIFACTS_DIR were not uploaded"

#: Firestore's limit on one document.
FIRESTORE_DOCUMENT_BYTES = 1024 * 1024


#: A stand-in for `claude --print` that writes `[template, count]` series into
#: the artifacts folder: `template.format(index)` for each index below count.
#: Its plan is the JSON value the prompt starts with; the platform's lines
#: follow it, so only the leading value is decoded.
SERIES_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
try:
    plan, _end = json.JSONDecoder().raw_decode(prompt)
except ValueError:
    plan = {}
artifacts = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"])
for template, count in plan.get("series") or []:
    for index in range(int(count)):
        path = artifacts / template.format(index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}))
"""


@pytest.fixture
def series_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude-series"
    binary.write_text(SERIES_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def _folder_names(db: Any, task_id: str = "task_1") -> list[str]:
    """The manifest's names that came from the artifacts folder, not the working folder."""
    return [name for name in _names(db, task_id) if not name.startswith("workdir/")]


def _logged_files(log_stream: Any) -> list[str]:
    return [line for record in _logged(log_stream, NOT_UPLOADED_LINE) for line in record["files"]]


def firestore_bytes(value: Any) -> int:
    """`value`'s size as Firestore counts it against a document's 1 MiB.

    https://firebase.google.com/docs/firestore/storage-size: a string is its
    UTF-8 bytes plus one; a boolean or null one byte; a number or a timestamp
    eight; an array the sum of its values; a map the sum of each key (a
    string) and its value. Spelled out here because the fake Firestore does
    not enforce the limit, which is how this defect reached main unnoticed.
    """
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return 8
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 1
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(firestore_bytes(item) for item in value)
    if isinstance(value, dict):
        return sum(firestore_bytes(str(key)) + firestore_bytes(item) for key, item in value.items())
    # A datetime, which the summary does not hold today.
    return 8


# ---------------------------------------------------------------------------
# the cap: 500 uploaded, the rest counted and named in the log
# ---------------------------------------------------------------------------


def test_the_cap_is_the_owners_500_files(worker_factory):
    _worker, config, _exporter = worker_factory(runner_profile=PROFILE)
    assert config.max_artifact_files == CAP


def test_600_files_upload_500_count_100_and_name_every_one_in_the_log(
    db, store, worker_factory, series_cli, log_stream
):
    """The folder holds 600 files: the runner's own three, and 597 the agent
    wrote. The runner's are taken first -- `result_summary.agent_streams` and
    the Artifacts tab's answer and transcript point at them -- and then the
    agent's, shallowest first and then by path, the working folder's rule."""
    written = 600 - len(RUNNER_OWN_ARTIFACTS)
    agent = {f"f{index:03d}.txt" for index in range(written)}
    _seed(db, {"series": [["f{:03d}.txt", written]]})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)

    names = _folder_names(db)
    assert len(names) == CAP, len(names)
    assert len(set(names)) == CAP
    assert RUNNER_OWN_ARTIFACTS <= set(names), sorted(RUNNER_OWN_ARTIFACTS - set(names))
    dropped = agent - set(names)
    assert dropped == {f"f{index:03d}.txt" for index in range(497, 597)}, sorted(dropped)

    summary = _summary(db)
    assert summary[OVER_CAP_KEY] == 100
    assert summary[CAP_KEY] == CAP
    # Not in `artifacts_skipped`: every reader of that list calls a name in
    # it dropped at the SIZE cap, and these were dropped at the FILE cap.
    assert not dropped & set(summary.get("artifacts_skipped") or []), summary.get("artifacts_skipped")
    # Nothing past the cap reached the bucket either.
    for name in ("f497.txt", "f596.txt"):
        assert not store.exists(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/{name}"), name

    # EVERY NAME, IN THE LOG, 100 to a WARNING line.
    records = _logged(log_stream, NOT_UPLOADED_LINE)
    assert [len(record["files"]) for record in records] == [100], [len(r["files"]) for r in records]
    assert records[0]["severity"] == "WARNING", records[0]
    assert sorted(_logged_files(log_stream)) == sorted(
        f"{name}: not uploaded: {OVER_CAP}" for name in dropped
    )


def test_past_the_cap_the_names_are_logged_100_to_a_warning_line(
    db, worker_factory, series_cli, log_stream
):
    """250 names past a cap of 10: three lines, of 100, 100 and 50, and
    between them every name once. A line stays well under Cloud Logging's
    256 KiB entry however many files the agent left."""
    written = 260 - len(RUNNER_OWN_ARTIFACTS)
    _seed(db, {"series": [["f{:03d}.txt", written]]})

    assert _run(worker_factory, max_artifact_files=10) == ExitCode.OK
    _succeeded(db)
    assert len(_folder_names(db)) == 10
    assert _summary(db)[OVER_CAP_KEY] == 250
    records = _logged(log_stream, NOT_UPLOADED_LINE)
    assert [len(record["files"]) for record in records] == [100, 100, 50]
    logged = _logged_files(log_stream)
    assert len(logged) == len(set(logged)) == 250


def test_a_folder_within_the_cap_is_uploaded_whole_and_says_nothing(
    db, worker_factory, series_cli, log_stream
):
    """The control: under the cap nothing changes, and the summary carries no
    over-cap count, so a reader does not draw a zero as something dropped."""
    _seed(db, {"series": [["report.md", 1], ["data/table-{}.csv", 3]]})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert {"report.md", "data/table-0.csv", "data/table-2.csv"} <= set(_folder_names(db))
    assert OVER_CAP_KEY not in _summary(db), _summary(db).get(OVER_CAP_KEY)
    assert _logged(log_stream, NOT_UPLOADED_LINE) == []


# ---------------------------------------------------------------------------
# the order: a declared output is never the one dropped
# ---------------------------------------------------------------------------


def test_an_expected_output_past_the_500th_file_is_still_uploaded(
    db, worker_factory, series_cli, log_stream
):
    """`zz/late-report.md` is the deepest name and the last by path, so in any
    order that ignored the declaration it would be the 604th of 604 files. A
    later step stages it, so it is taken first, and a file nobody declared is
    dropped in its place."""
    _seed(
        db,
        {"series": [["f{:03d}.txt", 600], ["zz/late-report.md", 1]]},
        metadata={"expected_outputs": ["zz/late-report.md"]},
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    names = _folder_names(db)
    assert "zz/late-report.md" in names, names[-5:]
    assert len(names) == CAP
    summary = _summary(db)
    assert "expected_outputs_missing" not in summary, summary.get("expected_outputs_missing")
    assert summary[OVER_CAP_KEY] == 604 - CAP
    assert not [line for line in _logged_files(log_stream) if line.startswith("zz/late-report.md")]


# ---------------------------------------------------------------------------
# the document stays bounded at the cap
# ---------------------------------------------------------------------------


class ProductionLengthUris(LocalObjectStore):
    """`uri()` as `GcsObjectStore` renders it, at the longest a deployment makes it.

    `gs://<bucket>/<key>` with a 63-character bucket (GCS's limit for a name
    without dots) and the tenant id at its 11-character limit
    (`swarm_common.identity`). The task and attempt ids are passed at their
    production length by the test. Every manifest entry repeats its name in
    its URI, so the URI's length is half of what the cap has to bound.
    """

    def uri(self, key: str) -> str:
        longest = key.replace(f"tenants/{TENANT}/", "tenants/" + "t" * 11 + "/", 1)
        return f"gs://{'b' * 63}/{longest}"


def test_the_summary_stays_bounded_at_the_cap_with_the_longest_names(
    db, tmp_path, log_stream, series_cli
):
    """600 files whose names are exactly the longest a manifest entry may
    carry, and 600 more whose names are longer. At the cap, with every entry at
    the longest name and URI, the summary stays inside what the task document
    has left once its input (up to `max_input_bytes`) and a reserve for the
    rest -- the working-folder block of #225, the runner envelope, the git
    summary, the metadata -- are counted.

    Before the cap every one of the 1,200 was listed: about 1.4 MB of summary."""
    task_id = "task_" + "0" * 20
    attempt_id = "att_" + "0" * 20
    at_bound = f"{'d' * 120}/{'e' * 120}/{{:04d}}{'x' * 10}"
    over_bound = f"{'p' * 200}/{'q' * 200}/{'r' * 200}/{{:04d}}{'y' * 93}"
    assert len(at_bound.format(0).encode("utf-8")) == MAX_NAME_BYTES
    assert len(over_bound.format(0).encode("utf-8")) == 700
    _seed(db, {"series": [[at_bound, 600], [over_bound, 600]]}, task_id=task_id, attempt_id=attempt_id)
    store = ProductionLengthUris(tmp_path / "gcs", bucket=BUCKET)
    worker, _config, _exporter = build_worker(
        db, store, tmp_path, log_stream, runner_profile=PROFILE, task_id=task_id, attempt_id=attempt_id
    )

    assert worker.run() == ExitCode.OK
    _succeeded(db, task_id)
    summary = _summary(db, task_id)
    names = _folder_names(db, task_id)
    assert len(names) == CAP
    assert max(len(name.encode("utf-8")) for name in names) == MAX_NAME_BYTES
    assert not [name for name in names if name.startswith("p" * 200)]

    budget = (
        FIRESTORE_DOCUMENT_BYTES
        - Settings(project_id="p").max_input_bytes
        # The reserve for everything else the document holds. The largest
        # part is #225's working-folder block, which only a task with no
        # repository has: 50 entries whose keys run to GCS's 1,024 bytes
        # (about 100 KiB), and 50 listed as not uploaded (about 55 KiB).
        - 256 * 1024
    )
    size = firestore_bytes(summary)
    assert size <= budget, f"result_summary is {size} bytes at the cap; the budget is {budget}"


def test_a_name_longer_than_the_manifest_bound_is_not_uploaded_and_is_named(
    db, worker_factory, series_cli, log_stream
):
    """Named whole in the log with its reason, and in `artifacts_skipped` with
    its name cut short as #225 cuts a name it lists, so 50 of them cannot take
    the summary near 1 MiB either."""
    long_name = f"{'a' * 200}/{'b' * 60}.txt"
    assert len(long_name.encode("utf-8")) > MAX_NAME_BYTES
    _seed(db, {"series": [["report.md", 1], [long_name, 1]]})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert "report.md" in _folder_names(db)
    assert long_name not in _names(db)
    assert f"{long_name}: not uploaded: {NAME_TOO_LONG}" in _logged_files(log_stream)
    skipped = _summary(db).get("artifacts_skipped") or []
    assert [name for name in skipped if name.startswith("a" * 200)], skipped
    assert all(len(name) <= MAX_NAME_BYTES + 3 for name in skipped), [len(n) for n in skipped]


# ---------------------------------------------------------------------------
# a file past the cap never leaves the pod, so it is not reported as if it had
# ---------------------------------------------------------------------------


def test_a_file_past_the_cap_is_not_reported_as_uploaded_unredacted(db, store, tmp_path):
    """`redaction_skipped` lists files that went to GCS without being
    rewritten. A binary file holding the key and lying past the cap never goes
    to GCS, so it is not listed there, and the log does not say it was
    "uploaded as-is"."""
    key = "sk-ant-supersecret-value-0123456789"
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    log = io.StringIO()
    worker, config, _ = build_worker(
        db,
        store,
        tmp_path,
        log,
        runner_profile="claude-code",
        secret_client=FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": key}),
        max_artifact_files=2,
    )
    worker.ws = ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._build_child_env()  # registers the key for redaction

    (ws.artifacts / "a.txt").write_text("a\n")
    (ws.artifacts / "b.txt").write_text("b\n")
    (ws.artifacts / "c.bin").write_bytes(b"\x7fELF\x00\xff\xfe" + key.encode("utf-8") + b"\x00\xff")

    summary = worker._upload_outputs()

    assert [entry["name"] for entry in summary["artifacts"]] == ["a.txt", "b.txt"]
    assert summary[OVER_CAP_KEY] == 1
    assert not store.exists(f"{config.artifact_prefix}/c.bin")
    unredacted = [entry.get("file") for entry in summary.get("redaction_skipped") or []]
    assert not [name for name in unredacted if name and name.endswith("c.bin")], unredacted
    assert "uploaded as-is" not in log.getvalue()
    # Named in the log as not uploaded, with the reason.
    lines = [line for record in _logged(log, NOT_UPLOADED_LINE) for line in record["files"]]
    assert lines == [f"c.bin: not uploaded: {OVER_CAP}"], lines
    # Left where the agent put it: not uploaded is not deleted.
    assert (ws.artifacts / "c.bin").exists()
