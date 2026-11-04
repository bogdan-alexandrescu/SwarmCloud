"""A plugin step parks on its agent's question and resumes the same session (#2688).

docs/design/agent-plugins.md, "The question bridge", and the owner's
decision of 3 November 2026: an unattended plugin step whose agent asks
through `AskUserQuestion` what it cannot answer itself parks HUMAN_REQUIRED
on a `question_wait` the worker builds from the hook's record, and the next
attempt resumes THAT session with the answer as its next message.

What each test holds:

* the question parks the step with no lease, checkpointed, its record
  carrying the questions, the session, the tool use, the attempt and its
  generation; the hook's pending file is gone and nothing is published;
* a checkpoint that fails refuses the park and fails the attempt;
* the next attempt restores the checkpoint, checks the session is the
  parked one, and `--resume`s it with the answer on stdin;
* a restored workspace without the parked session fails the attempt
  without starting the agent;
* a stale generation exits without starting the agent and leaves the
  answer as recorded;
* the resumed session's one message is the answer, and a resume whose
  answer did not reach the runner is refused;
* with the switch off nothing parks on a question.

No credentials, no network, no emulator: the production worker and the
production claude-code runner around a stand-in CLI that behaves as the
pinned CLI was measured to in the spike (test_question_bridge.py).
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker import plugins as plugins_mod
from agent_worker import questions as questions_mod
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.runners import cliagent, question_bridge as qb
from agent_worker.runners.base import RunnerFailure
from swarm_common.states import ParkReason, TaskState
from test_standalone_outputs import _run, _seed  # noqa: F401
from worker_seeds import TENANT, record_as_earlier_attempt

QUESTION = {
    "question": "Ship the migration in this release?",
    "header": "Migration",
    "multiSelect": False,
    "options": [
        {"label": "now", "description": "Readers move at once.", "preview": "ALTER TABLE t ..."},
        {"label": "later", "description": "One more release on the old shape."},
    ],
}

#: The stand-in CLI. A fresh start asks QUESTION through the settings' hook
#: and stops on it; `--resume` records its args and stdin in SEEN and ends.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, uuid
from pathlib import Path

SEEN = Path(__SEEN__)
QUESTION = json.loads(__QUESTION__)
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
def out(event):
    print(json.dumps(event), flush=True)

prompt = sys.stdin.read()
home = Path(os.environ["HOME"])
resume = opt("--resume")
if resume:
    found = list((home / ".claude" / "projects").glob(f"*/{resume}.jsonl"))
    SEEN.write_text(json.dumps({"args": args, "stdin": prompt, "found": bool(found)}))
    out({"type": "system", "subtype": "init", "session_id": resume})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": resume,
         "terminal_reason": "completed", "result": "done"})
    sys.exit(0)

sid = str(uuid.uuid4())
slug = os.getcwd().replace("/", "-").replace(".", "-")
transcript = home / ".claude" / "projects" / slug / f"{sid}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.write_text(json.dumps({"type": "user", "sessionId": sid, "content": "go"}) + "\n")
tools = ["Bash"] + (["AskUserQuestion"] if opt("--permission-prompt-tool") == "stdio" else [])
out({"type": "system", "subtype": "init", "session_id": sid, "tools": tools})
if "AskUserQuestion" not in tools:
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "terminal_reason": "completed", "result": "done"})
    sys.exit(0)
tool_use = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": "AskUserQuestion",
            "input": {"questions": [QUESTION]}}
out({"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}})
settings = json.loads(Path(opt("--settings")).read_text())
payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": os.getcwd(),
           "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
           "tool_input": tool_use["input"], "tool_use_id": tool_use["id"]}
stop = False
for entry in settings["hooks"]["PreToolUse"]:
    if entry.get("matcher") not in ("AskUserQuestion", "*"):
        continue
    for hook in entry["hooks"]:
        run = subprocess.run(hook["command"], shell=True, input=json.dumps(payload),
                             capture_output=True, text=True)
        answer = json.loads(run.stdout) if run.stdout.strip() else {}
        stop = stop or answer.get("continue") is False
with transcript.open("a") as f:
    f.write(json.dumps({"type": "assistant", "sessionId": sid, "content": [tool_use]}) + "\n")
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
     "stop_reason": "tool_use", "terminal_reason": "hook_stopped" if stop else "completed",
     "result": ""})
'''


@pytest.fixture
def seen(tmp_path: Path) -> Path:
    return tmp_path / "seen.json"


@pytest.fixture
def bridge_cli(tmp_path: Path, seen: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_CLI.replace("__SEEN__", repr(str(seen)))
                      .replace("__QUESTION__", repr(json.dumps(QUESTION))))
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    for name in ("PLAYWRIGHT_BROWSERS_PATH", "SWARM_CHILDREN"):
        monkeypatch.delenv(name, raising=False)
    return binary


@pytest.fixture
def plugin_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A step that started a plugin, as `plugins.start` leaves one: GSTACK_HOME under `work/`."""

    def start(task: Any, *, work: Path, **_: Any) -> plugins_mod.Started:
        home = Path(work) / plugins_mod.HOME_DIR_NAME
        home.mkdir(parents=True, exist_ok=True)
        return plugins_mod.Started(
            plugins=(plugins_mod.Plugin(name="gstack", version="1", mode="unattended", skills=()),),
            env={"GSTACK_HOME": str(home)}, guidance="")

    monkeypatch.setattr(lifecycle.plugins_mod, "start", start)


def _task(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")


def test_an_unanswerable_question_parks_the_step_with_its_session(
        db, tmp_path, worker_factory, bridge_cli, plugin_step, seen, monkeypatch):
    monkeypatch.setenv(qb.ENABLE_ENV, "1")
    _seed(db, {})

    # MUTATION: drop the bridge call in `_await_owner_or_release` -> the step SUCCEEDS.
    assert _run(worker_factory) == ExitCode.PARKED
    task = _task(db)
    assert task["state"] == TaskState.PARKED.value, task.get("last_error")
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    # Invariants 1 and 4: no lease while a person thinks; invariant 8: checkpointed.
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert task["latest_checkpoint"]
    (entry,) = task["blocked_by"]
    assert entry["park_phase"] == "question_wait"
    wait = entry[questions_mod.QUESTION_WAIT_KEY]
    assert [q["question"] for q in wait["questions"]] == [QUESTION["question"]]
    assert wait["questions"][0]["options"][0] == {"label": "now", "description": "Readers move at once."}
    assert "ALTER TABLE" in wait["questions"][0]["context"]
    assert wait["blocking"] == [0]
    bridge = wait[qb.BRIDGE_KEY]
    uuid.UUID(bridge["session_id"])
    assert bridge["tool_use_id"].startswith("toolu_")
    # Invariant 5: the record names the attempt and its fencing generation.
    assert wait["attempt_id"] == "att_1" and bridge["generation"] == 1
    assert "source_revision" in bridge
    # The hook's record is read once: from here the step's state is the record.
    assert not list(tmp_path.rglob(qb.PENDING_NAME))
    assert not seen.exists()


def test_a_failed_checkpoint_refuses_the_park(db, worker_factory, bridge_cli, plugin_step, monkeypatch):
    monkeypatch.setenv(qb.ENABLE_ENV, "1")
    real = lifecycle.Worker._checkpoint

    def failing(self: Any, label: str, **kw: Any) -> Any:
        return None if label == "question-wait" else real(self, label, **kw)

    monkeypatch.setattr(lifecycle.Worker, "_checkpoint", failing)
    _seed(db, {})

    # MUTATION: park without `require_checkpoint` -> PARKED with nothing to resume.
    assert _run(worker_factory) == ExitCode.FAILED
    task = _task(db)
    assert task["state"] != TaskState.PARKED.value
    assert "question_park_refused" in str(task.get("last_error"))


def test_without_the_switch_the_question_is_no_park(db, worker_factory, bridge_cli, plugin_step, monkeypatch):
    monkeypatch.delenv(qb.ENABLE_ENV, raising=False)
    _seed(db, {})

    assert _run(worker_factory) == ExitCode.OK
    assert _task(db)["state"] == TaskState.SUCCEEDED.value


def _parked_metadata(session_id: str, *, answered: bool = True) -> dict[str, Any]:
    pending = qb.parse_call({
        "session_id": session_id, "transcript_path": "", "cwd": "", "hook_event_name": "PreToolUse",
        "tool_name": qb.QUESTION_TOOL, "tool_input": {"questions": [QUESTION]}, "tool_use_id": "toolu_parked",
    })
    wait = qb.wait_record(pending, attempt_id="att_0", generation=1, source_revision=None,
                          asked_at="2026-11-03T10:00:00+00:00")
    answers = [{
        "question_index": 0, "question": QUESTION["question"], "choice": "later", "text": None,
        "by": "owner@example.com", "at": "2026-11-03T11:00:00+00:00", "history": [],
    }] if answered else []
    return {questions_mod.QUESTION_WAIT_KEY: wait, "owner_answers": answers}


def _checkpoint_with_session(db: Any, store: Any, tmp_path: Path, session_id: str) -> None:
    earlier = workspace_mod.create(tmp_path / "earlier-ws", "att_0")
    slug = earlier.work / ".claude" / "projects" / "-w-repo"
    slug.mkdir(parents=True)
    (slug / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": session_id, "content": "go"}) + "\n")

    class _Quiet:
        def info(self, *a: Any, **k: Any) -> None: ...
        def warning(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    record = CheckpointManager(store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_0",
                               generation=1, logger=_Quiet()).create(earlier)
    record_as_earlier_attempt(db, record)


def test_the_answered_question_resumes_the_parked_session_with_the_answer(
        db, store, tmp_path, worker_factory, bridge_cli, plugin_step, seen, monkeypatch):
    monkeypatch.setenv(qb.ENABLE_ENV, "1")
    sid = str(uuid.uuid4())
    _seed(db, {}, metadata=_parked_metadata(sid))
    _checkpoint_with_session(db, store, tmp_path, sid)

    assert _run(worker_factory) == ExitCode.OK, _task(db).get("last_error")
    # MUTATION: drop `_resume_parked_question` -> a fresh session starts and asks again.
    got = json.loads(seen.read_text())
    assert got["args"][got["args"].index("--resume") + 1] == sid and got["found"]
    assert "toolu_parked" in got["stdin"]
    assert f"{QUESTION['question']} -> later" in got["stdin"]


def test_a_restore_without_the_parked_session_never_starts_the_agent(
        db, store, tmp_path, worker_factory, bridge_cli, plugin_step, seen, monkeypatch):
    monkeypatch.setenv(qb.ENABLE_ENV, "1")
    _seed(db, {}, metadata=_parked_metadata(str(uuid.uuid4())))
    _checkpoint_with_session(db, store, tmp_path, str(uuid.uuid4()))

    # MUTATION: drop the `restored_session` check -> the CLI is asked to resume nothing.
    assert _run(worker_factory) != ExitCode.OK
    assert "question_resume_failed" in str(_task(db).get("last_error"))
    assert not seen.exists()


def test_a_stale_generation_never_runs_the_agent_nor_touches_the_answer(
        db, store, tmp_path, worker_factory, bridge_cli, plugin_step, seen, monkeypatch):
    monkeypatch.setenv(qb.ENABLE_ENV, "1")
    sid = str(uuid.uuid4())
    metadata = _parked_metadata(sid)
    _seed(db, {}, metadata=metadata)
    _checkpoint_with_session(db, store, tmp_path, sid)
    # Invariant 5: the task moved on to generation 2; this worker holds 1.
    _task(db)["current_generation"] = 2

    assert _run(worker_factory) != ExitCode.OK
    assert not seen.exists()
    assert _task(db)["metadata"]["owner_answers"] == metadata["owner_answers"]
    assert _task(db)["metadata"][questions_mod.QUESTION_WAIT_KEY] == metadata[questions_mod.QUESTION_WAIT_KEY]


def test_the_resumed_message_is_the_answer_and_never_nothing(tmp_path):
    answer = tmp_path / "answer.txt"
    answer.write_text("- Ship it? -> later\n")
    env = {cliagent.RESUME_REASON_ENV: cliagent.RESUME_ANSWERED, cliagent.QUESTION_ANSWER_FILE_ENV: str(answer)}
    assert cliagent.resumed_message(env) == "- Ship it? -> later"
    # MUTATION: fall back to `resume_prompt` without the file -> the session goes on as if nobody answered.
    for broken in ({**env, cliagent.QUESTION_ANSWER_FILE_ENV: str(tmp_path / "missing")},
                   {cliagent.RESUME_REASON_ENV: cliagent.RESUME_ANSWERED}):
        with pytest.raises(RunnerFailure, match="question_resume_failed"):
            cliagent.resumed_message(broken)
    assert cliagent.resumed_message({cliagent.RESUME_REASON_ENV: cliagent.RESUME_TIME_LIMIT}) == \
        cliagent.resume_prompt(cliagent.RESUME_TIME_LIMIT)


def test_after_a_hook_stopped_question_the_runner_takes_no_finish_turn(tmp_path, monkeypatch):
    """`cliagent`: a run that stopped on the question tool is not resumed to
    finish, however its answer reads: the turn would go on without the answer."""
    from test_claude_resume_to_finish import FAKE_CLI as FINISH_CLI, PENDING, _ctx, _install, _runs
    from agent_worker.runners import claude_code

    cli = FINISH_CLI.replace('"num_turns": step.get("turns", 3)}',
                             '"num_turns": step.get("turns", 3), **step.get("extra", {})}')
    assert cli != FINISH_CLI
    ctx = _ctx(tmp_path, {"prompt": "plan it"})
    asked = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_q", "name": qb.QUESTION_TOOL, "input": {"questions": [QUESTION]}}]}}
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": PENDING, "events": [asked], "extra": {"terminal_reason": qb.HOOK_STOPPED}}])
    (tmp_path / "fake-claude").write_text(cli)
    monkeypatch.delenv(qb.ENABLE_ENV, raising=False)

    out = claude_code.body(ctx)
    # MUTATION: drop `not parked_on_question()` from the finish pass -> two runs.
    assert len(_runs(ctx)) == 1
    assert out["resumed_to_finish"] is False


def _hook_stopped(**extra: Any) -> list[dict[str, Any]]:
    """The stream of a run the bridge's hook stopped on the question tool."""
    asked = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_q", "name": qb.QUESTION_TOOL, "input": {"questions": [QUESTION]}}]}}
    return [asked, {"type": "result", "subtype": "success", "is_error": False,
                    "terminal_reason": qb.HOOK_STOPPED, **extra}]


def _say_events(cli: str, marker: str) -> str:
    """A fake CLI of another test file that also prints its step's `events`
    before the result, and stamps `extra` on the result."""
    said = cli.replace(marker, 'for event in step.get("events", []):\n    say(event)\n' + marker)
    said = said.replace('"num_turns": step.get("turns", 3)}',
                        '"num_turns": step.get("turns", 3), **step.get("extra", {})}')
    assert said.count("step.get(\"events\"") == 1 and "**step.get(\"extra\"" in said
    return said


def test_after_a_hook_stopped_question_the_runner_takes_no_repair_turn(tmp_path, monkeypatch):
    """`cliagent`: a run that stopped on the question tool is not resumed to
    repair a missing output: the repair turn would go on without the answer."""
    from test_claude_repair_turns import FAKE_CLI as REPAIR_CLI, _ctx, _install, _runs
    from agent_worker.runners import claude_code

    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    asked, _ = _hook_stopped()
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "Which scope?", "events": [asked], "extra": {"terminal_reason": qb.HOOK_STOPPED}},
        {"answer": "Done.", "write": {"$ARTIFACTS/scan-01.md": "# findings\n"}},
    ])
    (tmp_path / "fake-claude").write_text(_say_events(REPAIR_CLI, 'say({"type": "result"'))
    monkeypatch.delenv(qb.ENABLE_ENV, raising=False)

    out = claude_code.body(ctx)
    # MUTATION: drop `not parked_on_question()` from the repair branch -> two runs.
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0
    assert ctx.report["question_parked"] is True


def test_after_a_hook_stopped_question_the_runner_takes_no_pre_publish_turn(tmp_path, monkeypatch):
    """`cliagent`: a run that stopped on the question tool runs no pre-publish
    check and takes no fix turn, though the tree it left fails a check."""
    import json as _json
    import secrets as pysecrets

    from test_prepublish_gate import FAKE_CLI as GATE_CLI, SESSION, _repo, _tool_calls, _tools
    from agent_worker import prepublish
    from agent_worker.gitmcp import server as shim
    from agent_worker.runners import claude_code
    from agent_worker.runners.base import RunnerContext

    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    repo, base = _repo(work)
    asked, _ = _hook_stopped()
    cli = GATE_CLI.replace(
        'print(json.dumps({"type": "result"',
        'for event in step.get("events", []):\n    print(json.dumps(event))\n'
        'print(json.dumps({**step.get("extra", {}), "type": "result"')
    assert cli != GATE_CLI
    (tmp_path / "fake-claude").write_text(cli)
    (tmp_path / "fake-claude").chmod(0o755)
    (work / "plan.json").write_text(_json.dumps({"session": SESSION, "runs": [
        {"write": {"scripts/deploy.sh": "#!/usr/bin/env bash\necho BAD\n"}, "events": [asked],
         "extra": {"terminal_reason": qb.HOOK_STOPPED}},
        {"write": {"scripts/deploy.sh": "#!/usr/bin/env bash\necho fixed\n"}},
    ]}))
    bin_dir = _tools(tmp_path)
    monkeypatch.setattr(cliagent, "BASE_PATH", f"{bin_dir}:{cliagent.BASE_PATH}")
    monkeypatch.setenv("TOOL_LOG", str(tmp_path / "tools.jsonl"))
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(tmp_path / "fake-claude"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "api03-" + pysecrets.token_hex(16))
    monkeypatch.setenv(prepublish.CLONE_BASE_ENV, base)
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL", qb.ENABLE_ENV,
                 prepublish.PREPUBLISH_BASE_ENV, shim.SPOOL_ENV,
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cliagent, "_PLAIN_PASSTHROUGH",
                        (*cliagent._PLAIN_PASSTHROUGH, "TOOL_LOG"))
    ctx = RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "do it"}, repo_dir=repo)

    out = claude_code.body(ctx)
    runs = (work / "runs.jsonl").read_text().splitlines()
    # MUTATION: drop `not parked_on_question()` from the pre-publish branch ->
    # the stand-in shellcheck runs, fails, and a fix turn resumes the session.
    assert len(runs) == 1
    assert _tool_calls(tmp_path) == []
    assert out.get("prepublish") is None
    assert ctx.report["question_parked"] is True


#: A fake CLI whose first start asks the question tool on a turn past the
#: context ceiling and is stopped by the bridge's hook; any later start ends
#: plainly. Each start appends its argv and stdin to `$HOME/starts.jsonl`.
STOPPED_CLI = r'''#!PYTHON
import json, os, sys
home = os.environ["HOME"]
starts = os.path.join(home, "starts.jsonl")
n = sum(1 for _ in open(starts)) if os.path.exists(starts) else 0
with open(starts, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\n")
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "sess-asked-0a1b2c"
def say(event):
    print(json.dumps(event), flush=True)
say({"type": "system", "subtype": "init", "session_id": sid})
if n == 0:
    say({"type": "assistant", "session_id": sid, "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "toolu_q", "name": TOOL, "input": {"questions": []}}],
        "usage": {"input_tokens": 210000, "cache_read_input_tokens": 0, "output_tokens": 10}}})
    say({"type": "user", "session_id": sid, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_q", "content": "stopped by hook"}]}})
    say({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "result": "", "terminal_reason": STOPPED, "num_turns": 1})
else:
    say({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "result": "done", "num_turns": 1})
'''


def _stopped_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: Any) -> tuple[Any, Any]:
    """The runner around STOPPED_CLI, on a pool account and under the context
    ceiling, with `stop(kwargs)` applied after the first watched start: the
    stop a watcher made at the boundary the hook's turn ended on."""
    from test_context_ceiling import _spec, _starts
    from agent_worker.runners.base import RunnerContext

    cli = tmp_path / "fake-claude"
    cli.write_text(STOPPED_CLI.replace("#!PYTHON", "#!" + sys.executable)
                   .replace("TOOL", json.dumps(qb.QUESTION_TOOL))
                   .replace("STOPPED", json.dumps(qb.HOOK_STOPPED)))
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_BIN", str(cli))
    monkeypatch.setenv("FAKE_KEY", "k-" + uuid.uuid4().hex[:16])
    for name in (cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV, cliagent.SESSION_FILE_ENV,
                 cliagent.ACCOUNT_MOVE_ENV, "SWARM_CLONE_BASE", qb.ENABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(cliagent.ACCOUNT_STREAM_ENV, str(tmp_path / "stream.json"))
    watched = cliagent._run_watched
    calls: list[int] = []

    def stopping(*args: Any, **kwargs: Any) -> Any:
        done = watched(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            stop(kwargs)
        return done

    monkeypatch.setattr(cliagent, "_run_watched", stopping)
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    ctx = RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "do the work"})
    cliagent.run_cli_agent(ctx, _spec())
    return ctx, _starts(ctx)


def test_after_a_hook_stopped_question_the_runner_takes_no_wrap_up_turn(tmp_path, monkeypatch):
    """`cliagent`: a run stopped to wrap up at the boundary its question's
    turn ended on is not resumed to wrap up: the pass would go on without
    the answer."""
    def wrap_up(kwargs: dict[str, Any]) -> None:
        kwargs["watcher"].stopped_to_wrap_up = True

    ctx, starts = _stopped_run(tmp_path, monkeypatch, wrap_up)
    # MUTATION: drop `not parked_on_question()` from the wrap-up branch -> a
    # second start, `--resume`d with WRAP_UP_PROMPT.
    assert len(starts) == 1, starts
    assert ctx.report.get("resumed_to_wrap_up") is not True
    assert ctx.report["question_parked"] is True


def test_after_a_hook_stopped_question_the_ceiling_hands_off_to_no_fresh_session(
    tmp_path, monkeypatch
):
    """`cliagent`: a run stopped at the context ceiling on the boundary its
    question's turn ended on starts no fresh session from a handoff."""
    from agent_worker.runners import ceiling

    def over(kwargs: dict[str, Any]) -> None:
        kwargs["ceiling"].stopped_for = ceiling.WHY_CONTEXT

    ctx, starts = _stopped_run(tmp_path, monkeypatch, over)
    # MUTATION: drop `not parked_on_question()` before `carry_on` -> a second
    # start whose stdin is the CONTEXT CEILING HANDOFF.
    assert len(starts) == 1, starts
    assert ctx.report["question_parked"] is True


def test_an_answered_resume_over_the_ceiling_still_resumes_the_parked_session(
    tmp_path, monkeypatch
):
    """`cliagent`: the context ceiling's fresh-session handoff never replaces a
    parked session's resume. The worker resumes the session that asked, over
    the ceiling or not, with the answer as its one message."""
    from test_context_ceiling import FAKE_CLI as CEILING_CLI, SMALL, _plan, _spec, _starts
    from agent_worker.runners import ceiling
    from agent_worker.runners.base import RunnerContext

    cli = tmp_path / "fake-claude"
    cli.write_text(CEILING_CLI.format(python=sys.executable))
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_BIN", str(cli))
    monkeypatch.setenv("FAKE_KEY", "k-" + uuid.uuid4().hex[:16])
    for name in ("SWARM_ACCOUNT_STREAM", "SWARM_ACCOUNT_MOVE", "SWARM_CLONE_BASE", qb.ENABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    work, artifacts, private = tmp_path / "work", tmp_path / "artifacts", tmp_path / "private"
    for path in (work, artifacts, private):
        path.mkdir()
    ctx = RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "do the work"})
    session = "sess-parked-" + uuid.uuid4().hex[:8]
    answer = "Use the staging scope."
    (private / "answer.txt").write_text(answer)
    monkeypatch.setenv(cliagent.SESSION_FILE_ENV, str(private / "session.json"))
    monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, session)
    monkeypatch.setenv(cliagent.RESUME_REASON_ENV, cliagent.RESUME_ANSWERED)
    monkeypatch.setenv(cliagent.QUESTION_ANSWER_FILE_ENV, str(private / "answer.txt"))
    # The parked session ended past the ceiling.
    (private / ceiling.STATE_NAME).write_text(json.dumps(
        {"session_id": session, "context_tokens": 452_706, "tool_calls": 40,
         "turns": 40, "fresh_sessions": 0}))
    _plan(ctx, SMALL)

    out = cliagent.run_cli_agent(ctx, _spec())
    (only,) = _starts(ctx)
    # MUTATION: drop `and not answered` from the resume's ceiling check -> a
    # fresh session from CONTEXT CEILING HANDOFF, the answer never said.
    assert only["argv"][only["argv"].index("--resume") + 1] == session
    assert only["stdin"].startswith(answer + "\n")
    assert "CONTEXT CEILING HANDOFF" not in only["stdin"]
    assert out["context_ceiling"]["stops"] == []
