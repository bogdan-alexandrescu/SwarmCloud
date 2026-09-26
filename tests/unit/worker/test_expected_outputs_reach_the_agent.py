"""The upstream agent is told where its dependants' files must go (#149).

Measured on 2026-09-25, workflow `wf_73946ff4a32a4f99b3a4`: eight claude-code
scans were prompted "write it to scan-01.md", SUCCEEDED, and uploaded only
`claude-code.stdout.log`, `claude-code.stderr.log` and `claude-transcript.json`.
Only files in `$SWARM_ARTIFACTS_DIR` are uploaded (`runners/base.py`), that
directory is outside the working directory and the repository, and nothing had
told the agent so. All four merges then failed at staging.

The owner chose option (b) on #149. The API records the names on the upstream
task as `metadata.expected_outputs`
(tests/unit/control_plane/test_workflow_expected_outputs.py). These tests hold
the worker's half:

* the worker hands the names to the runner in `input.json`, and ONLY those
  names: a caller's own `input.expected_outputs` is dropped, because the
  instructions speak for the platform and a caller could otherwise put words
  in its mouth;
* a CLI runner appends them, with the ABSOLUTE artifacts path, to the prompt it
  passes the agent. With none, the prompt gets only the one line every CLI
  prompt now ends with, naming `$SWARM_ARTIFACTS_DIR` (#184, owner decision of
  2026-09-26; `test_standalone_outputs.py` holds that line for both runners);
* a name the platform writes itself -- the worker's `swarm-work.patch`, the
  runner's own stdout and stderr logs and transcript -- is never in those
  instructions. Telling an agent to write a file the platform then overwrites,
  or is writing to at the same moment, is worse than telling it nothing;
* an attempt whose runner finished cleanly but did not upload one of them
  FAILS, RETRYABLY, with the missing names as its cause (owner decision on
  #149, 2026-09-25, replacing the report-only behaviour this PR first
  shipped). The task goes back to READY while it has attempts left and to
  FAILED once `max_attempts` is spent, so a dependant never starts on a parent
  that did not write what it promised. An attempt that would be retried
  publishes nothing, as a parked one does not: the work is not finished;
* an attempt whose runner failed on its own keeps its own cause, and the
  missing names are still recorded.

`test_work_artifacts_link.py` holds the other half of #149: the `./artifacts`
link, for the agent that reads the right path and writes somewhere else anyway.

The keys are spelled out rather than imported: each is a field of a document
that outlives the process that wrote it (`input.json`, `result_summary`), so a
rename is a compatibility change a test should notice.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from agent_worker.runners.base import RunnerContext
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

#: `task.metadata` key the API writes and the worker reads.
METADATA_KEY = "expected_outputs"
#: `input.json` key the worker writes and a runner reads.
RUNNER_INPUT_KEY = "expected_outputs"
#: `result_summary` key naming what the attempt did not produce.
MISSING_KEY = "expected_outputs_missing"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed(db: Any, expected: Any, *, artifact_name: str = "notes.md") -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "write the notes",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": artifact_name,
            "artifact_text": "the notes\n",
        },
    )
    db.doc("tasks/task_1")["metadata"] = {METADATA_KEY: expected}


def _runner_input(store: LocalObjectStore) -> dict[str, Any]:
    """`input.json` as the runner saw it, from the final checkpoint of `work/`.

    The workspace is destroyed when the attempt ends; the checkpoint is the
    archive of `work/` taken while the runner was running.
    """
    keys = [
        key
        for key in store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/")
        if key.endswith("/archive.tar.gz")
    ]
    assert keys, "the attempt wrote no checkpoint"
    with tarfile.open(
        fileobj=io.BytesIO(store.download_bytes(sorted(keys)[-1])), mode="r:gz"
    ) as archive:
        member = archive.extractfile("input.json")
        assert member is not None
        return json.loads(member.read())


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


def _warnings_naming(log_stream: io.StringIO, name: str) -> list[dict[str, Any]]:
    return [
        r
        for r in _records(log_stream)
        if r.get("severity") == "WARNING" and name in str(r.get("message", ""))
    ]


def _ctx(tmp_path: Path, payload: dict[str, Any]) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


def _argv_recording_cli(tmp_path: Path) -> Path:
    """A stand-in for `claude`: records the argv it was started with, for real.

    The child's environment is built, not inherited, so the recording goes into
    its working directory, which `SWARM_WORK_DIR` names.
    """
    binary = tmp_path / "fake-claude"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.path.join(os.environ['SWARM_WORK_DIR'], 'argv.json'), 'w') as fh:\n"
        "    json.dump(sys.argv, fh)\n"
        "print(json.dumps({'result': 'ok'}))\n"
    )
    binary.chmod(0o755)
    return binary


def _prompt_the_agent_received(tmp_path: Path, monkeypatch, payload: dict[str, Any]) -> tuple[str, Path]:
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(_argv_recording_cli(tmp_path)))
    spec = CliAgentSpec(
        name="fake",
        provider="anthropic",
        binary_env="FAKE_BIN",
        binary_default="fake-claude",
        args_env="FAKE_ARGS",
        args_default=("--print",),
        key_env="FAKE_KEY",
        model_flag=None,
    )
    ctx = _ctx(tmp_path, payload)
    run_cli_agent(ctx, spec)
    argv = json.loads((ctx.work_dir / "argv.json").read_text())
    # The prompt is the single trailing argument; nothing else a caller supplies
    # reaches argv.
    return argv[-1], ctx.artifacts_dir


# ---------------------------------------------------------------------------
# the runner: what the agent is told
# ---------------------------------------------------------------------------


def test_the_agent_is_told_the_names_and_the_absolute_artifacts_path(tmp_path, monkeypatch):
    prompt, artifacts = _prompt_the_agent_received(
        tmp_path,
        monkeypatch,
        {"prompt": "Scan the repo. Write it to scan-01.md", RUNNER_INPUT_KEY: ["scan-01.md", "notes.md"]},
    )

    assert artifacts.is_absolute()
    # The caller's own instructions come first and are kept whole.
    assert prompt.startswith("Scan the repo. Write it to scan-01.md")
    # Every name, and the directory by its absolute path -- not only by the
    # variable's name, which an agent once reported as unset and wrote nothing
    # (wf_bcdc9180e4fb4a209f31, recorded in runners/cliagent.py).
    assert "scan-01.md" in prompt[len("Scan the repo. Write it to scan-01.md"):]
    assert "notes.md" in prompt
    assert str(artifacts) in prompt
    assert f"{artifacts}/scan-01.md" in prompt
    assert f"{artifacts}/notes.md" in prompt
    assert "$SWARM_ARTIFACTS_DIR" in prompt
    assert "outside the repository" in prompt


def _deliverables_line(artifacts: Path) -> str:
    """The one line every CLI prompt ends with (#184, owner decision of 2026-09-26).

    In the owner's words, information and not an order (#225 review): a
    repository task gets this line too, and its deliverable is its diff.
    """
    return (
        f"Files written to {artifacts} ($SWARM_ARTIFACTS_DIR) "
        "are uploaded and shown in Artifacts."
    )


def test_a_prompt_with_nothing_expected_gets_only_the_deliverables_line(tmp_path, monkeypatch):
    """Until 2026-09-26 this prompt was passed unchanged. The owner's decision on
    #184 is that every claude-code and codex prompt names $SWARM_ARTIFACTS_DIR,
    so the one line is appended and nothing else."""
    for payload in (
        {"prompt": "do the thing"},
        {"prompt": "do the thing", RUNNER_INPUT_KEY: []},
    ):
        prompt, artifacts = _prompt_the_agent_received(tmp_path, monkeypatch, dict(payload))
        assert prompt == f"do the thing\n\n{_deliverables_line(artifacts)}", payload


def test_the_runners_own_files_are_never_in_the_instructions(tmp_path, monkeypatch):
    """A dependant may stage the upstream runner's log or transcript. The
    runner writes those itself, the log WHILE the agent runs, so an agent told
    to write one would be writing into a file the runner holds open."""
    # `_prompt_the_agent_received` runs a spec named "fake" with the default
    # transcript name, so these are exactly the runner's three files.
    own = ["fake.stdout.log", "fake.stderr.log", "transcript.json"]
    prompt, _ = _prompt_the_agent_received(
        tmp_path, monkeypatch, {"prompt": "scan", RUNNER_INPUT_KEY: [*own, "scan-01.md"]}
    )

    assert "scan-01.md" in prompt
    for name in own:
        assert name not in prompt, f"the agent was told to write the runner's own {name}"


def test_a_prompt_whose_every_expected_name_is_the_runners_own_gets_only_the_line(
    tmp_path, monkeypatch
):
    prompt, artifacts = _prompt_the_agent_received(
        tmp_path, monkeypatch, {"prompt": "scan", RUNNER_INPUT_KEY: ["fake.stdout.log"]}
    )
    assert prompt == f"scan\n\n{_deliverables_line(artifacts)}"


# ---------------------------------------------------------------------------
# the worker: the names reach the runner, and a missing one fails the attempt
# ---------------------------------------------------------------------------


def test_the_worker_hands_the_names_to_the_runner(db, store, worker_factory):
    _seed(db, ["notes.md", "data.json"])
    worker, _config, _exporter = worker_factory()

    # The mock runner writes notes.md only, so data.json is missing and the
    # attempt fails retryably (tested below). What is asserted here is what the
    # runner was handed, which the final checkpoint holds either way.
    assert worker.run() == ExitCode.FAILED
    assert _runner_input(store).get(RUNNER_INPUT_KEY) == ["data.json", "notes.md"]


def test_a_task_that_expects_nothing_hands_the_runner_nothing(db, store, worker_factory):
    seed_attempt(db, task_input={"prompt": "ordinary", "steps": 1, "sleep_seconds": 0.01})
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert RUNNER_INPUT_KEY not in _runner_input(store)
    assert MISSING_KEY not in db.doc("tasks/task_1")["result_summary"]


def test_a_callers_own_input_expected_outputs_never_reaches_the_runner(
    db, store, worker_factory, log_stream
):
    """Only names the API recorded on the task reach the agent.

    The appended block says "later steps of this workflow need these files",
    in the platform's voice. A caller who could set it through `input` would
    be writing that claim for the platform, on a task nothing stages from.
    """
    seed_attempt(
        db,
        task_input={
            "prompt": "ordinary",
            "steps": 1,
            "sleep_seconds": 0.01,
            RUNNER_INPUT_KEY: ["caller.md"],
        },
    )
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert RUNNER_INPUT_KEY not in _runner_input(store)
    dropped = _warnings_naming(log_stream, "input.expected_outputs")
    assert len(dropped) == 1, "the caller's value was dropped without a word"


def test_the_platforms_names_replace_a_callers_own(db, store, worker_factory):
    _seed(db, ["notes.md"])
    db.doc("tasks/task_1")["input"][RUNNER_INPUT_KEY] = ["caller.md"]
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert _runner_input(store).get(RUNNER_INPUT_KEY) == ["notes.md"]


def test_the_workers_own_patch_is_never_handed_to_the_runner(db, store, worker_factory):
    """`swarm-work.patch` is the platform's record of the agent's repository
    changes, written after the agent exits whenever the diff is non-empty. An
    agent told to write it would have its file replaced, or, with an empty
    diff, uploaded under a name every reader takes for the platform's own."""
    _seed(db, ["notes.md", "swarm-work.patch"])
    worker, _config, _exporter = worker_factory()

    # No repository, so the harvest writes no patch and the attempt fails
    # retryably for it: left out of the instructions, the patch is still owed
    # to the dependant that stages it.
    assert worker.run() == ExitCode.FAILED
    assert _runner_input(store).get(RUNNER_INPUT_KEY) == ["notes.md"]
    assert db.doc("tasks/task_1")["result_summary"].get(MISSING_KEY) == ["swarm-work.patch"]


def _retrying(db: Any) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == "retrying"]


def test_a_missing_expected_output_fails_the_attempt_retryably_and_names_it(
    db, worker_factory, log_stream
):
    """The owner's decision on #149: the attempt FAILS, retryably, with the
    missing names as its cause. Attempt 1 of 3, so the task goes back to READY
    and gives its capacity back; the dependant does not start."""
    _seed(db, ["notes.md", "scan-01.md"], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task["state"]
    assert task["current_lease_id"] is None
    assert task["next_eligible_at"] is not None
    assert task.get("completed_at") is None, "a task that will run again is not complete"
    assert db.doc("leases/lease_1")["released_at"] is not None, "capacity was not returned"
    # The cause names the file that is missing, and only that one.
    assert "scan-01.md" in task["last_error"]
    assert "notes.md" not in task["last_error"]
    assert task["result_summary"].get(MISSING_KEY) == ["scan-01.md"]
    assert "scan-01.md" in db.doc("attempts/att_1")["error"]

    retrying = _retrying(db)
    assert len(retrying) == 1, db.event_types("task_1")
    detail = retrying[0]["detail"]
    assert detail["cause"] == "expected_outputs_missing"
    assert detail["missing"] == ["scan-01.md"]
    assert detail["to_state"] == TaskState.READY.value
    assert (detail["attempt_count"], detail["max_attempts"]) == (1, 3)
    assert "succeeded" not in db.event_types("task_1")

    lines = _warnings_naming(log_stream, "scan-01.md")
    assert len(lines) == 1, lines
    # The file that WAS written is not reported as missing.
    assert "notes.md" not in lines[0]["message"]


def test_the_last_attempt_without_an_expected_output_fails_the_task(db, worker_factory):
    """Bounded by `max_attempts`: attempt 3 of 3 ends FAILED, for good, with the
    same cause. The dependant is then cancelled as for any failed parent."""
    _seed(db, ["scan-01.md"], artifact_name="notes.md")
    db.doc("tasks/task_1")["attempt_count"] = 3
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["completed_at"] is not None
    assert task.get("next_eligible_at") is None, "a retry time on a task that will not run"
    assert "scan-01.md" in task["last_error"]
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert _retrying(db) == []
    assert "failed" in db.event_types("task_1")


def test_a_cancel_requested_before_the_attempt_ends_is_not_undone_by_a_retry(
    db, worker_factory, monkeypatch
):
    """The user's decision survives a retry, as it does in the reconciler's
    repair: a cancel that lands after the agent exits ends CANCELLED, not
    READY. Read inside the transaction that decides, not from the task the
    worker fetched when it started."""
    _seed(db, ["scan-01.md"], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory()
    upload = worker._upload_outputs

    def cancel_then_upload(**kwargs: Any) -> dict[str, Any]:
        db.doc("tasks/task_1")["cancel_requested"] = True
        return upload(**kwargs)

    monkeypatch.setattr(worker, "_upload_outputs", cancel_then_upload)

    worker.run()
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value, task["state"]
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_an_attempt_that_will_be_retried_publishes_nothing(db, worker_factory, monkeypatch):
    """Like a park: the work is not finished, and the next attempt publishes it.
    Published now, the retry would push again from the final checkpoint, taken
    before this attempt's auto-commit, and when there was one the push would be
    refused as a non-fast-forward."""
    _seed(db, ["scan-01.md"], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory()
    seen: list[dict[str, Any]] = []

    def harvest(**kwargs: Any) -> None:
        seen.append(kwargs)
        return None

    monkeypatch.setattr(worker, "_harvest_git", harvest)

    worker.run()
    assert [call["publish"] for call in seen] == [False], seen
    assert "expected output" in seen[0].get("withheld", ""), seen


@pytest.mark.parametrize(
    "attempt_count, written",
    [(1, "scan-01.md"), (3, "notes.md")],
    ids=["every-expected-output-written", "missing-on-the-last-attempt"],
)
def test_an_attempt_that_will_not_run_again_publishes(
    db, worker_factory, monkeypatch, attempt_count, written
):
    """The control for the test above: publishing is withheld only from an
    attempt that is going to run again. A clean attempt publishes, and so does
    the last one, like any other failed attempt."""
    _seed(db, ["scan-01.md"], artifact_name=written)
    db.doc("tasks/task_1")["attempt_count"] = attempt_count
    worker, _config, _exporter = worker_factory()
    seen: list[bool] = []

    def harvest(*, publish: bool, **_kwargs: Any) -> None:
        seen.append(publish)
        return None

    monkeypatch.setattr(worker, "_harvest_git", harvest)
    worker.run()
    assert seen == [True], seen


def test_a_runner_that_failed_on_its_own_keeps_its_own_cause(db, worker_factory):
    """The retry is for a runner that finished cleanly and left a file out. A
    runner that failed fails as it always has, terminal and with its own error;
    the missing names are still recorded next to it."""
    seed_attempt(
        db,
        task_input={
            "prompt": "x",
            "steps": 1,
            "sleep_seconds": 0.01,
            "fail": True,
            "fail_message": "the agent gave up",
        },
    )
    db.doc("tasks/task_1")["metadata"] = {METADATA_KEY: ["scan-01.md"]}
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert "the agent gave up" in task["last_error"]
    assert task["result_summary"].get(MISSING_KEY) == ["scan-01.md"]
    assert _retrying(db) == []


def test_nothing_is_reported_when_every_expected_output_was_written(db, worker_factory, log_stream):
    _seed(db, ["notes.md"], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert MISSING_KEY not in task["result_summary"]
    assert _warnings_naming(log_stream, "notes.md") == []


def test_an_unusable_entry_is_dropped_loudly_and_does_not_fail_the_attempt(
    db, store, worker_factory, log_stream
):
    """An entry that cannot be a file in the artifacts directory is left out of
    the instructions and of the end-of-attempt check, with a warning naming it.
    No dependant could stage it either, so it does not cost the upstream step
    its run. Only a usable name that is missing fails the attempt."""
    _seed(db, ["notes.md", "../escape.md", "/etc/passwd", 7, ""], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert _runner_input(store).get(RUNNER_INPUT_KEY) == ["notes.md"]
    assert _warnings_naming(log_stream, "../escape.md"), "the dropped entry was not named"


def test_a_file_written_but_not_uploaded_is_named_as_such(db, worker_factory, log_stream):
    """Over the artifact cap, the file exists and was still never uploaded. The
    dependant stages from the upload manifest, so for it the file is missing,
    and the line says which remedy applies: the cap, not the prompt."""
    _seed(db, ["notes.md"], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory(max_artifact_bytes=4)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    # Missing for the dependant, so failed retryably like a file never written.
    assert task["state"] == TaskState.READY.value
    assert task["result_summary"].get(MISSING_KEY) == ["notes.md"]
    lines = _warnings_naming(log_stream, "notes.md")
    missing_lines = [r for r in lines if "not uploaded" in r["message"]]
    assert len(missing_lines) == 1, lines
    assert "not written" not in missing_lines[0]["message"]


# ---------------------------------------------------------------------------
# the pure pieces
# ---------------------------------------------------------------------------


def test_a_relative_artifacts_directory_is_given_to_the_agent_as_absolute(tmp_path, monkeypatch):
    from agent_worker.expected_outputs import with_instructions

    monkeypatch.chdir(tmp_path)
    told = with_instructions("p", ["a.md"], Path("rel/artifacts"))
    assert f"{tmp_path}/rel/artifacts/a.md" in told
    assert " rel/artifacts" not in told


def test_the_declaration_is_read_defensively():
    from agent_worker.expected_outputs import declared_outputs, parse_names

    assert parse_names(None).names == ()
    assert parse_names(None).rejected == ()
    # A bare string is some other writer, not one name: the API writes a list.
    assert parse_names("notes.md").names == ()
    assert parse_names("notes.md").rejected == ("notes.md",)
    # Stripped, sorted, each once; nested names keep their directory.
    assert parse_names([" b.md", "a.md", "b.md", "reports/c.json"]).names == (
        "a.md",
        "b.md",
        "reports/c.json",
    )
    bad = ["a//b", "./a", "a/", "..", "x\ny", "x\\y", "x\x00y", None, 3]
    assert parse_names(bad).names == ()
    assert len(parse_names(bad).rejected) == len(bad)
    # Metadata that is not a dict cannot carry the key at all.
    assert declared_outputs(None).names == ()
    assert declared_outputs(["notes.md"]).names == ()
