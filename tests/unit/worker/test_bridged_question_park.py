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
