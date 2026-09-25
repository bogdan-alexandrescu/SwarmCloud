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
  passes the agent, and passes the prompt unchanged when there are none;
* a name the platform writes itself -- the worker's `swarm-work.patch`, the
  runner's own stdout and stderr logs and transcript -- is never in those
  instructions. Telling an agent to write a file the platform then overwrites,
  or is writing to at the same moment, is worse than telling it nothing;
* an attempt that ends without one of them says so, in one log line and in its
  result summary, and is NOT failed for it -- whether it should be is an open
  owner decision.

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


def test_the_prompt_is_passed_unchanged_when_nothing_is_expected(tmp_path, monkeypatch):
    for payload in (
        {"prompt": "do the thing"},
        {"prompt": "do the thing", RUNNER_INPUT_KEY: []},
    ):
        prompt, _ = _prompt_the_agent_received(tmp_path, monkeypatch, dict(payload))
        assert prompt == "do the thing", payload


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


def test_the_prompt_is_unchanged_when_every_expected_name_is_the_runners_own(
    tmp_path, monkeypatch
):
    prompt, _ = _prompt_the_agent_received(
        tmp_path, monkeypatch, {"prompt": "scan", RUNNER_INPUT_KEY: ["fake.stdout.log"]}
    )
    assert prompt == "scan"


# ---------------------------------------------------------------------------
# the worker: the names reach the runner, and a missing one is reported
# ---------------------------------------------------------------------------


def test_the_worker_hands_the_names_to_the_runner(db, store, worker_factory):
    _seed(db, ["notes.md", "data.json"])
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
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
    """`swarm-work.patch` is written by the worker's git harvest after the agent
    exits, over whatever is there. An agent told to write it would be doing
    work the platform throws away."""
    _seed(db, ["notes.md", "swarm-work.patch"])
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert _runner_input(store).get(RUNNER_INPUT_KEY) == ["notes.md"]


def test_a_missing_expected_output_is_named_in_one_line_and_does_not_fail_the_attempt(
    db, worker_factory, log_stream
):
    _seed(db, ["notes.md", "scan-01.md"], artifact_name="notes.md")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    # NOT failed: whether it should be is the owner's decision, not this one.
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["result_summary"].get(MISSING_KEY) == ["scan-01.md"]

    lines = _warnings_naming(log_stream, "scan-01.md")
    assert len(lines) == 1, lines
    # The file that WAS written is not reported as missing.
    assert "notes.md" not in lines[0]["message"]


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
    """The declaration is advice to the agent. An entry that cannot be a file
    in the artifacts directory is left out of that advice with a warning naming
    it; it does not cost the upstream step its run."""
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

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
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
