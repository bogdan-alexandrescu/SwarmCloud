"""A SwarmCloud step behaves like a local Claude Code lane (#226).

Owner decisions of 2026-09-26, after comparing a dispatched workflow with the
same workflow run locally:

1. **The agent starts in the checkout.** With a repository attached, the CLI's
   working directory is `work/repo`, so Claude Code loads the repository's own
   `CLAUDE.md` by itself, as it does in a local lane, and a prompt no longer has
   to say "read repo/CLAUDE.md first". HOME stays the attempt's own `work/`,
   never the repository. `$SWARM_ARTIFACTS_DIR` stays reachable by its absolute
   path, named in the prompt line the worker adds, and so does every file a
   step's `input_from` staged, which lands in `work/` and is no longer in the
   agent's working directory. A task with no repository is unchanged.
2. **The code profile runs a pinned model.** The `claude-code` job's `MODEL`
   comes from Terraform, the worker reads it into `config.model`, and the
   runner passes `--model` from there. Never from the task's `input`: a model
   chosen by a caller is an execution parameter, which invariant 10 forbids,
   so `input.model` is refused at the API and dropped by the worker.

What these tests hold, each against the real runner or the real worker:

  * the CLI starts in the checkout when there is one, HOME is `work/`, and the
    repository's `CLAUDE.md` is readable from where it starts;
  * the CLI starts in `work/` when there is none (the control);
  * a checkout that should be there and is not fails the runner, rather than
    starting the agent somewhere its `CLAUDE.md` is not;
  * staged inputs are named in the prompt by absolute path when the agent
    starts in the checkout, and a task with no repository gets the prompt it
    got before;
  * `./artifacts` in the checkout is the artifacts directory, hidden from git so
    it never reaches the agent's diff, and never archived by a checkpoint;
  * `--model` is the Job's `MODEL`, and a caller's `input.model` reaches
    neither argv nor `input.json`.

The stand-in agent is a real executable started through `CLAUDE_CODE_BIN` (or
the `FAKE_BIN` of a test spec). It writes RELATIVE to the directory the runner
started it in, because that is what a real agent does.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from agent_worker.objectstore import LocalObjectStore
from agent_worker.runners.base import RunnerContext, RunnerFailure
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: The model the owner chose for the claude-code profile (#226). Spelled out,
#: not imported: the value lives in Terraform, which a Python test cannot read,
#: and the worker only ever sees it as the Job's MODEL.
PINNED_MODEL = "claude-opus-5-5"

#: What a caller might send in `input.model`. It must reach nothing.
CALLER_MODEL = "caller-chosen-model"

#: A line only the origin repository's CLAUDE.md carries.
CLAUDE_MD_MARKER = "origin CLAUDE.md marker-7f3a19"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def _deliverables_line(artifacts: Path) -> str:
    return (
        f"Files written to {artifacts} ($SWARM_ARTIFACTS_DIR) "
        "are uploaded and shown in Artifacts."
    )


# ---------------------------------------------------------------------------
# the runner, in process
# ---------------------------------------------------------------------------

#: A stand-in for `claude`: records where it was started and what it was given.
#: The child's environment is built, not inherited, so the record goes where
#: `SWARM_WORK_DIR` names -- which is `work/` whether or not the agent starts
#: in the checkout.
RECORDING_CLI = r"""#!/usr/bin/env python3
import json, os, sys
record = {
    "argv": sys.argv[1:-1],
    "prompt": sys.argv[-1],
    "cwd": os.getcwd(),
    "home": os.environ.get("HOME"),
    "claude_md": open("CLAUDE.md").read() if os.path.exists("CLAUDE.md") else None,
}
with open(os.path.join(os.environ["SWARM_WORK_DIR"], "record.json"), "w") as fh:
    json.dump(record, fh)
print(json.dumps({"result": "ok"}))
"""


def _spec(model_flag: str | None = "--model"):
    from agent_worker.runners.cliagent import CliAgentSpec

    return CliAgentSpec(
        name="fake",
        provider="anthropic",
        binary_env="FAKE_BIN",
        binary_default="fake-claude",
        args_env="FAKE_ARGS",
        args_default=("--print",),
        key_env="FAKE_KEY",
        model_flag=model_flag,
    )


def _ctx(tmp_path: Path, payload: dict[str, Any], *, repo: bool) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    checkout = work / "repo"
    if repo:
        checkout.mkdir(exist_ok=True)
        (checkout / "CLAUDE.md").write_text(CLAUDE_MD_MARKER + "\n")
    ctx = RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )
    # Assigned rather than passed, so the no-repository controls construct the
    # context the same way on a worker that predates the field.
    if repo:
        ctx.repo_dir = checkout
    return ctx


@pytest.fixture
def recording_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text(RECORDING_CLI)
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(binary))
    monkeypatch.delenv("FAKE_ARGS", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    return binary


def _run(ctx: RunnerContext, *, model_flag: str | None = "--model") -> tuple[dict, dict]:
    from agent_worker.runners.cliagent import run_cli_agent

    out = run_cli_agent(ctx, _spec(model_flag))
    record = json.loads((ctx.work_dir / "record.json").read_text())
    return out, record


def test_with_a_repository_the_agent_starts_in_the_checkout_and_home_stays_work(
    tmp_path, recording_cli
):
    ctx = _ctx(tmp_path, {"prompt": "fix the bug"}, repo=True)

    _out, record = _run(ctx)

    assert os.path.samefile(record["cwd"], ctx.work_dir / "repo"), (
        "the agent did not start in the checkout, so Claude Code cannot load the "
        f"repository's own CLAUDE.md by itself; it started in {record['cwd']}"
    )
    assert record["claude_md"] is not None and CLAUDE_MD_MARKER in record["claude_md"]
    # HOME is the attempt's isolated directory, never the repository: the CLI
    # writes its own state under HOME, and none of it belongs in the diff.
    assert os.path.samefile(record["home"], ctx.work_dir)
    assert not os.path.samefile(record["home"], ctx.work_dir / "repo")


def test_without_a_repository_the_agent_starts_in_the_work_dir(tmp_path, recording_cli):
    """The control, and the owner's "no-repository tasks unchanged"."""
    ctx = _ctx(tmp_path, {"prompt": "answer the question"}, repo=False)

    _out, record = _run(ctx)

    assert os.path.samefile(record["cwd"], ctx.work_dir)
    assert os.path.samefile(record["home"], ctx.work_dir)
    assert record["claude_md"] is None


def test_a_checkout_that_is_missing_fails_the_runner_instead_of_starting_elsewhere(
    tmp_path, recording_cli
):
    """The worker said there is a checkout and there is not. Starting the agent
    in `work/` would run it without the repository's CLAUDE.md and without the
    code, and it would still report success."""
    ctx = _ctx(tmp_path, {"prompt": "fix the bug"}, repo=False)
    ctx.repo_dir = ctx.work_dir / "repo"

    with pytest.raises(RunnerFailure, match="checkout"):
        _run(ctx)
    assert not (ctx.work_dir / "record.json").exists(), "the agent was started anyway"


def test_a_checkout_outside_the_work_dir_is_refused(tmp_path, recording_cli):
    ctx = _ctx(tmp_path, {"prompt": "fix the bug"}, repo=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ctx.repo_dir = elsewhere

    with pytest.raises(RunnerFailure, match="checkout"):
        _run(ctx)


def test_the_prompt_names_staged_inputs_by_absolute_path_when_the_agent_starts_in_the_checkout(
    tmp_path, recording_cli
):
    """A staged input lands in `work/`, which is no longer the agent's working
    directory once it starts in the checkout. "Read scan-01.md" would resolve
    against the repository, so the prompt line names where each one is."""
    staged = [
        {"task_id": "task_up1", "filename": "scan-01.md", "path": "scan-01.md", "bytes": 3},
        {"task_id": "task_up2", "filename": "d/notes.md", "path": "d/notes.md", "bytes": 3},
    ]
    ctx = _ctx(tmp_path, {"prompt": "merge the scans", "staged_inputs": staged}, repo=True)

    _out, record = _run(ctx)
    prompt = record["prompt"]

    assert prompt.startswith("merge the scans\n\n")
    assert _deliverables_line(ctx.artifacts_dir) in prompt
    assert f"{ctx.work_dir}/scan-01.md" in prompt
    assert f"{ctx.work_dir}/d/notes.md" in prompt
    assert "outside the repository" in prompt


def test_a_task_with_no_repository_gets_the_prompt_it_got_before(tmp_path, recording_cli):
    """The staged file is in its working directory, where "read scan-01.md"
    already finds it; the owner said a task with no repository is unchanged."""
    staged = [{"task_id": "task_up1", "filename": "scan-01.md", "path": "scan-01.md", "bytes": 3}]
    ctx = _ctx(tmp_path, {"prompt": "merge the scans", "staged_inputs": staged}, repo=False)

    _out, record = _run(ctx)

    assert record["prompt"] == f"merge the scans\n\n{_deliverables_line(ctx.artifacts_dir)}"


def test_an_unusable_staged_path_is_never_put_in_the_prompt(tmp_path, recording_cli):
    """`staged_inputs` is the worker's own record, but the prompt is text in the
    platform's voice: a path that leaves `work/` or carries a line break could
    rewrite the lines around it."""
    staged = [
        {"path": "../escape.md"},
        {"path": "/etc/passwd"},
        {"path": "two\nlines.md"},
        {"path": 7},
        "not-a-record",
        {"path": "fine.md"},
    ]
    ctx = _ctx(tmp_path, {"prompt": "p", "staged_inputs": staged}, repo=True)

    _out, record = _run(ctx)
    prompt = record["prompt"]

    assert f"{ctx.work_dir}/fine.md" in prompt
    for bad in ("escape.md", "/etc/passwd", "lines.md"):
        assert bad not in prompt, bad


def test_the_model_comes_from_the_job_and_never_from_the_task_input(
    tmp_path, recording_cli, monkeypatch
):
    """Invariant 10. `MODEL` is set on the Job by Terraform and handed to the
    runner by the worker; `input.model` is the caller's, and a caller does not
    choose what runs."""
    monkeypatch.setenv("MODEL", PINNED_MODEL)
    ctx = _ctx(tmp_path, {"prompt": "hi", "model": CALLER_MODEL}, repo=False)

    out, record = _run(ctx)

    argv = record["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == PINNED_MODEL
    assert CALLER_MODEL not in argv
    # What the runner reports it asked for, which lands in the task's
    # result_summary.runner.output.model.
    assert out["model"] == PINNED_MODEL


def test_a_callers_input_model_alone_selects_nothing(tmp_path, recording_cli):
    ctx = _ctx(tmp_path, {"prompt": "hi", "model": CALLER_MODEL}, repo=False)

    out, record = _run(ctx)

    assert "--model" not in record["argv"]
    assert CALLER_MODEL not in record["argv"]
    assert out["model"] is None


def test_a_job_model_with_unsupported_characters_fails_before_the_agent_starts(
    tmp_path, recording_cli, monkeypatch
):
    monkeypatch.setenv("MODEL", "x; rm -rf /")
    ctx = _ctx(tmp_path, {"prompt": "hi"}, repo=False)

    with pytest.raises(RunnerFailure, match="unsupported characters"):
        _run(ctx)
    assert not (ctx.work_dir / "record.json").exists()


def test_the_runner_context_reads_the_checkout_from_its_environment(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    base = {
        "SWARM_WORK_DIR": str(work),
        "SWARM_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
    }

    assert RunnerContext.from_env(base).repo_dir is None
    with_repo = RunnerContext.from_env({**base, "SWARM_REPO_DIR": str(work / "repo")})
    assert with_repo.repo_dir == work / "repo"


# ---------------------------------------------------------------------------
# the checkpoint and git: ./artifacts inside the checkout
# ---------------------------------------------------------------------------


class _Quiet:
    def info(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...


def test_a_checkout_artifacts_link_is_never_archived_and_the_archive_restores(
    store, tmp_path, log_stream
):
    """`checkpoint._safe_members` refuses an archive holding a link that leaves
    `work/`, so an archived `repo/artifacts` link would fail every resume."""
    logger = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="claude-code", stream=log_stream,
    )
    original = workspace_mod.create(tmp_path / "ws", "att_1")
    checkout = original.checkout()
    checkout.mkdir()
    (checkout / "main.py").write_text("print('hi')\n")
    (original.artifacts / "scan.md").write_text("scan\n")
    assert workspace_mod.link_artifacts(original, within=checkout) is True
    assert original.is_artifacts_link(checkout / "artifacts")

    record = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=logger,
    ).create(original)
    assert record.file_count == 1, "only repo/main.py; not the link, not the artifact"

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_2",
        generation=2, logger=logger,
    )
    found = manager.find_latest()
    assert found is not None
    assert manager.restore(found, resumed) == 1
    assert (resumed.checkout() / "main.py").read_text() == "print('hi')\n"
    assert not os.path.lexists(resumed.checkout() / "artifacts")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout


def _repo_with_a_commit(path: Path, *, gitignore: str | None = None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=main", ".")
    (path / "README.md").write_text("hello\n")
    if gitignore is not None:
        (path / ".gitignore").write_text(gitignore)
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "base")
    return path


@needs_git
def test_the_checkout_link_is_hidden_from_git(tmp_path):
    repo = _repo_with_a_commit(tmp_path / "repo")
    target = tmp_path / "artifacts"
    target.mkdir()
    os.symlink(str(target), repo / "artifacts", target_is_directory=True)
    private = tmp_path / "private"
    logs = tmp_path / "logs"
    private.mkdir()
    logs.mkdir()

    hidden = gitops.hide_from_git(
        repo=repo, name="artifacts", private_dir=private, logs_dir=logs,
        timeout_seconds=30, logger=_Quiet(),
    )

    assert hidden is True
    assert "artifacts" not in _git(repo, "status", "--porcelain")
    assert "/artifacts" in (repo / ".git" / "info" / "exclude").read_text().splitlines()
    # Idempotent: a resumed attempt restores the exclude file and hides again.
    assert gitops.hide_from_git(
        repo=repo, name="artifacts", private_dir=private, logs_dir=logs,
        timeout_seconds=30, logger=_Quiet(),
    ) is True
    assert (repo / ".git" / "info" / "exclude").read_text().splitlines().count("/artifacts") == 1


@needs_git
def test_a_repository_that_unignores_the_name_is_reported_not_hidden(tmp_path):
    """A `.gitignore` outranks `.git/info/exclude`. When the repository itself
    says `!/artifacts`, the link would be in the agent's diff, so the caller
    must be told it could not be hidden."""
    repo = _repo_with_a_commit(tmp_path / "repo", gitignore="!/artifacts\n")
    target = tmp_path / "artifacts"
    target.mkdir()
    os.symlink(str(target), repo / "artifacts", target_is_directory=True)
    (tmp_path / "private").mkdir()
    (tmp_path / "logs").mkdir()

    assert gitops.hide_from_git(
        repo=repo, name="artifacts", private_dir=tmp_path / "private",
        logs_dir=tmp_path / "logs", timeout_seconds=30, logger=_Quiet(),
    ) is False


# ---------------------------------------------------------------------------
# the worker, end to end
# ---------------------------------------------------------------------------

#: A stand-in for `claude --print --output-format stream-json`: reports where
#: it started and what it was given, and writes RELATIVE to that directory.
LANE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

try:
    plan, _end = json.JSONDecoder().raw_decode(sys.argv[-1])
except (ValueError, IndexError):
    plan = {}

out = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "done",
    "cwd": os.getcwd(),
    "home": os.environ.get("HOME"),
    "work_dir": os.environ.get("SWARM_WORK_DIR"),
    "flags": sys.argv[1:-1],
    "claude_md": open("CLAUDE.md").read() if os.path.exists("CLAUDE.md") else None,
    "prompt_tail": sys.argv[-1][-600:],
}
for name, text in sorted((plan.get("write_relative") or {}).items()):
    path = pathlib.Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
print(json.dumps(out))
"""


@pytest.fixture
def lane_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "lane-claude"
    binary.write_text(LANE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    return binary


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository whose tree carries its own CLAUDE.md."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("the repository as the agent finds it\n")
    (seed / "CLAUDE.md").write_text(f"# Working here\n\n{CLAUDE_MD_MARKER}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
        check=True, capture_output=True,
    )
    return bare


@pytest.fixture
def local_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Let the clone reach a `file://` remote inside tmp_path, and nothing else
    (the same wrapper test_strategy_end_to_end.py uses)."""
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"

    def validate(url: str) -> str:
        if url.startswith(allowed):
            return url
        return real(url)

    monkeypatch.setattr(gitops, "validate_repository_url", validate)


def _seed_lane(db: Any, plan: dict[str, Any], **extra_input: Any) -> None:
    seed_attempt(
        db, runner_profile="claude-code",
        task_input={"prompt": json.dumps(plan), **extra_input},
    )
    # Without the provider on the tenant the task parks for a missing
    # credential before the agent starts, and every assertion is vacuous.
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]


def _summary(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")["result_summary"] or {}


def _agent_saw(db: Any) -> dict[str, Any]:
    output = (_summary(db).get("runner") or {}).get("output") or {}
    assert isinstance(output, dict), f"the runner envelope was truncated: {output!r}"
    return output.get("structured_output") or {}


def _final_archive(store: LocalObjectStore) -> tarfile.TarFile:
    keys = sorted(
        key
        for key in store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/")
        if key.endswith("/archive.tar.gz")
    )
    assert keys, "the attempt wrote no checkpoint"
    return tarfile.open(fileobj=io.BytesIO(store.download_bytes(keys[-1])), mode="r:gz")


def _artifact(store: LocalObjectStore, name: str) -> str:
    return store.download_bytes(
        f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/{name}"
    ).decode("utf-8")


def _run_lane(worker_factory: Any, monkeypatch: pytest.MonkeyPatch, **config: Any) -> int:
    worker, _config, _exporter = worker_factory(runner_profile="claude-code", **config)
    # No tenant git credential: a `file://` remote needs none, and the fake
    # secret client would otherwise hand back a made-up token.
    monkeypatch.setattr(worker, "_git_token", lambda: None)
    return worker.run()


@needs_git
def test_a_repository_step_runs_in_its_checkout_like_a_local_lane(
    db, store, worker_factory, lane_agent, origin, local_urls, monkeypatch
):
    _seed_lane(
        db,
        {"write_relative": {"artifacts/scan-02.md": "scan two\n", "answer.md": "an answer\n"}},
        model=CALLER_MODEL,
    )

    code = _run_lane(
        worker_factory, monkeypatch, repository_url=f"file://{origin}", model=PINNED_MODEL
    )

    assert code == ExitCode.OK, _summary(db)
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    saw = _agent_saw(db)
    # Compared as real paths: the workspace is gone by now, and `getcwd` reports
    # the resolved path while the environment carries the one the worker built.
    work = os.path.realpath(saw["work_dir"])

    # 1. It started in the checkout, found the repository's CLAUDE.md there,
    #    and HOME is the attempt's own directory.
    assert os.path.realpath(saw["cwd"]) == os.path.join(work, "repo"), saw["cwd"]
    assert saw["claude_md"] is not None and CLAUDE_MD_MARKER in saw["claude_md"]
    assert os.path.realpath(saw["home"]) == work

    # 2. The Job's model, and not the caller's.
    flags = saw["flags"]
    assert flags[flags.index("--model") + 1] == PINNED_MODEL
    assert CALLER_MODEL not in json.dumps(saw)

    # 3. `./artifacts` written from the checkout is the artifacts directory: the
    #    file is uploaded under its own name ...
    names = [entry["name"] for entry in _summary(db).get("artifacts", [])]
    assert "scan-02.md" in names, names
    assert _artifact(store, "scan-02.md") == "scan two\n"
    # ... and never reaches the agent's diff, while the agent's real edit does.
    patch = _artifact(store, "swarm-work.patch")
    assert "answer.md" in patch
    assert "artifacts" not in patch, patch

    # 4. The checkpoint holds the checkout, the exclude entry that hides the
    #    link, and not the link.
    with _final_archive(store) as archive:
        members = archive.getnames()
        assert "repo/CLAUDE.md" in members
        assert "repo/artifacts" not in members
        exclude = archive.extractfile("repo/.git/info/exclude")
        assert exclude is not None
        assert "/artifacts" in exclude.read().decode("utf-8").splitlines()
        runner_input = archive.extractfile("input.json")
        assert runner_input is not None
        assert "model" not in json.loads(runner_input.read())


def test_a_callers_input_model_never_reaches_input_json(
    db, store, worker_factory, log_stream
):
    """The worker's half of the refusal, for a task written before the API
    refused the key or by any path that does not go through the API. The
    platform's own model is not put there either: it is the Job's `MODEL`,
    passed to the runner in its environment."""
    seed_attempt(
        db,
        task_input={"prompt": "ordinary", "steps": 1, "sleep_seconds": 0.01, "model": CALLER_MODEL},
    )
    worker, _config, _exporter = worker_factory(model=PINNED_MODEL)

    assert worker.run() == ExitCode.OK
    with _final_archive(store) as archive:
        member = archive.extractfile("input.json")
        assert member is not None
        runner_input = json.loads(member.read())
    assert "model" not in runner_input
    assert len(_warnings_naming(log_stream, "input.model")) == 1, (
        "the caller's input.model was dropped without a word"
    )


def test_a_callers_own_staged_inputs_never_reaches_input_json(
    db, store, worker_factory, log_stream
):
    """`staged_inputs` becomes a line in the prompt, in the platform's voice,
    naming files earlier steps gave this one. Only what the worker staged may
    fill it."""
    seed_attempt(
        db,
        task_input={
            "prompt": "ordinary", "steps": 1, "sleep_seconds": 0.01,
            "staged_inputs": [{"path": "caller.md"}],
        },
    )
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    with _final_archive(store) as archive:
        member = archive.extractfile("input.json")
        assert member is not None
        runner_input = json.loads(member.read())
    assert "staged_inputs" not in runner_input
    assert len(_warnings_naming(log_stream, "input.staged_inputs")) == 1
