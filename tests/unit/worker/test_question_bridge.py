"""The question bridge spike (agent plugins plan; docs/design/agent-plugins-question-bridge.md).

A headless claude-code agent's question is captured by a PreToolUse hook on
`AskUserQuestion`, the turn ends, and the same session resumes in a new
process with the answer as its next message. The CLI here is a fake that
behaves as the pinned CLI (2.1.283) was measured to: it offers the question
tool only under `--permission-prompt-tool stdio`, runs the settings' PreToolUse
hooks with the documented payload, ends the turn on `"continue": false` with
`terminal_reason: "hook_stopped"`, keeps the session under
`$HOME/.claude/projects/<slug>/<session>.jsonl` and `--resume`s it from there.
No credentials, no network.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from agent_worker.runners import claude_code, question_bridge as qb
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.cliagent import cli_stream_files

QUESTIONS = [
    {
        "question": "Which database should the plan use?",
        "header": "Database",
        "multiSelect": False,
        "options": [
            {"label": "Postgres (Recommended)", "description": "Managed Cloud SQL.",
             "preview": "CREATE TABLE t (id serial);"},
            {"label": "Firestore", "description": "Document store.", "preview": "collection('t')"},
        ],
    },
    {
        "question": "Which regions?",
        "header": "Regions",
        "multiSelect": True,
        "options": [{"label": "europe-west1", "description": ""}, {"label": "us-central1", "description": ""}],
    },
]

#: The fake CLI. Its behaviour is the pinned CLI's as measured in the spike.
FAKE_CLI = r'''
import json, os, subprocess, sys, uuid
from pathlib import Path

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None

prompt = sys.stdin.read()
home = Path(os.environ["HOME"])
slug = os.getcwd().replace("/", "-").replace(".", "-")
def out(event):
    print(json.dumps(event), flush=True)

resume = opt("--resume")
if resume:
    found = list((home / ".claude" / "projects").glob(f"*/{resume}.jsonl"))
    if not found:
        sys.stderr.write(f"No conversation found with session ID: {resume}\n")
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": resume})
        sys.exit(1)
    history = [json.loads(l) for l in found[0].read_text().splitlines()]
    out({"type": "system", "subtype": "init", "session_id": resume})
    with found[0].open("a") as f:
        f.write(json.dumps({"role": "user", "content": prompt}) + "\n")
    out({"type": "result", "subtype": "success", "session_id": resume, "terminal_reason": "completed",
         "result": json.dumps({"history": len(history), "answer": prompt})})
    sys.exit(0)

sid = str(uuid.uuid4())
tools = ["Bash", "Read"]
disallowed = ",".join(args[i + 1] for i, a in enumerate(args) if a == "--disallowedTools").split(",")
if opt("--permission-prompt-tool") == "stdio":
    tools += [t for t in ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode") if t not in disallowed]
out({"type": "system", "subtype": "init", "session_id": sid, "tools": tools})
transcript = home / ".claude" / "projects" / slug / f"{sid}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.write_text(json.dumps({"role": "user", "content": prompt}) + "\n")
if "AskUserQuestion" not in tools:
    out({"type": "result", "subtype": "success", "session_id": sid, "terminal_reason": "completed",
         "result": "no question tool"})
    sys.exit(0)
tool_use = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": "AskUserQuestion",
            "input": json.loads(os.environ["FAKE_QUESTIONS"])}
out({"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}})
settings = json.loads(Path(opt("--settings")).read_text())
payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": os.getcwd(),
           "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
           "tool_input": tool_use["input"], "tool_use_id": tool_use["id"]}
stop = False
for entry in settings["hooks"]["PreToolUse"]:
    if entry["matcher"] not in ("AskUserQuestion", "*"):
        continue
    for hook in entry["hooks"]:
        run = subprocess.run(hook["command"], shell=True, input=json.dumps(payload),
                             capture_output=True, text=True)
        answer = json.loads(run.stdout) if run.stdout.strip() else {}
        stop = stop or answer.get("continue") is False
with transcript.open("a") as f:
    f.write(json.dumps({"role": "assistant", "content": [tool_use]}) + "\n")
if stop:
    out({"type": "result", "subtype": "success", "session_id": sid, "stop_reason": "tool_use",
         "terminal_reason": "hook_stopped", "result": ""})
else:
    out({"type": "result", "subtype": "success", "session_id": sid, "terminal_reason": "completed",
         "result": "Tool permission request failed: AbortError: Stream closed"})
'''

_CLEARED = (qb.ENABLE_ENV, claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
            claude_code.BROWSERS_PATH_ENV, claude_code.CDP_ENDPOINT_ENV, claude_code.BUDGET_NOTE_ENV,
            claude_code.MAX_TURNS_ENV)


def _body_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]) -> list[str]:
    """What claude_code's body hands the CLI with `env` set."""
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        return {"summary": "", "stdout_truncated": False, "metrics": {"duration_seconds": 1.0}}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True, exist_ok=True)
    ctx = RunnerContext(work_dir=work, artifacts_dir=work / "artifacts", input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "Plan the storage layer."}, repo_dir=work / "repo")
    claude_code.body(ctx)
    return seen["args"]


def _fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake-claude.py"
    path.write_text(FAKE_CLI)
    return path


def _run_cli(cli: Path, home: Path, cwd: Path, args: list[str], prompt: str) -> tuple[int, list[dict[str, Any]]]:
    cwd.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "FAKE_QUESTIONS": json.dumps({"questions": QUESTIONS})}
    run = subprocess.run([sys.executable, str(cli), *args], input=prompt, capture_output=True, text=True,
                         cwd=str(cwd), env=env, timeout=60)
    return run.returncode, [json.loads(line) for line in run.stdout.splitlines() if line.strip()]


def _call(**extra: Any) -> dict[str, Any]:
    payload = {"session_id": str(uuid.uuid4()), "transcript_path": "/h/.claude/projects/-w/x.jsonl",
               "cwd": "/w", "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
               "tool_input": {"questions": QUESTIONS}, "tool_use_id": "toolu_abc"}
    payload.update(extra)
    return payload


def _hook(payload: Any, pending: Path) -> dict[str, Any]:
    run = subprocess.run([sys.executable, "-I", str(qb.HOOK), str(pending)],
                         input=payload if isinstance(payload, str) else json.dumps(payload),
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# --- the switch -------------------------------------------------------------


def test_the_switch_is_off_unless_exactly_one():
    assert not qb.enabled({})
    assert not qb.enabled({qb.ENABLE_ENV: "true"})
    assert not qb.enabled({qb.ENABLE_ENV: "0"})
    assert qb.enabled({qb.ENABLE_ENV: "1"})
    assert qb.cli_args({}) == []


def test_off_the_runner_adds_neither_the_flag_nor_the_hook(tmp_path, monkeypatch):
    args = _body_args(monkeypatch, tmp_path, {})
    assert "--permission-prompt-tool" not in args
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert all(e["matcher"] != qb.QUESTION_TOOL for e in settings["hooks"]["PreToolUse"])


def test_on_the_runner_adds_the_flag_the_withheld_tools_and_the_hook(tmp_path, monkeypatch):
    args = _body_args(monkeypatch, tmp_path, {qb.ENABLE_ENV: "1"})
    # MUTATION: drop `question_bridge.cli_args` from `body` -> this fails.
    assert args[args.index("--permission-prompt-tool") + 1] == "stdio"
    withheld = args[args.index("--disallowedTools") + 1].split(",")
    assert set(qb.WITHHELD_TOOLS) <= set(withheld)
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    entries = [e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == qb.QUESTION_TOOL]
    # MUTATION: drop the hook entry from `write_headless_settings` -> this fails.
    assert len(entries) == 1
    command = entries[0]["hooks"][0]["command"]
    assert str(qb.HOOK) in command and qb.PENDING_NAME in command and " -I " in command
    # The pending file sits beside the settings in work/, never in the checkout.
    settings_dir = Path(args[args.index("--settings") + 1]).parent
    assert command.endswith(str(settings_dir / qb.PENDING_NAME))
    assert settings_dir.name == claude_code.SETTINGS_DIR_NAME


# --- the hook ---------------------------------------------------------------


def test_the_hook_records_the_question_and_stops_the_turn(tmp_path):
    pending = tmp_path / "s" / qb.PENDING_NAME
    payload = _call()
    answer = _hook(payload, pending)
    assert answer["continue"] is False
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert answer["stopReason"] == qb.PARKED_REASON
    recorded = qb.read_pending(pending)
    assert recorded.session_id == payload["session_id"] and recorded.tool_use_id == "toolu_abc"
    first, second = recorded.questions
    assert first.recommended == "Postgres (Recommended)" and not first.multi_select
    assert first.options[0].preview == "CREATE TABLE t (id serial);"
    assert second.multi_select and second.recommended is None
    assert pending.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("payload", [
    "not json",
    _call(tool_name="Bash"),
    _call(session_id="../../etc"),
    _call(tool_input={"questions": []}),
    _call(tool_input={"questions": [{"question": "q", "options": [{"description": "no label"}]}]}),
])
def test_a_question_it_cannot_record_still_stops_the_turn_and_leaves_no_pending(tmp_path, payload):
    pending = tmp_path / qb.PENDING_NAME
    pending.write_text("stale from an earlier park")
    answer = _hook(payload, pending)
    # Strict: never let the call through to a host nobody answers.
    assert answer["continue"] is False
    assert answer["stopReason"] == qb.FAILED_REASON
    assert not pending.exists()


def test_the_hook_refuses_an_oversized_payload(tmp_path):
    pending = tmp_path / qb.PENDING_NAME
    out = io.StringIO()
    big = json.dumps(_call(tool_input={"questions": [{"question": "x" * (qb.MAX_PAYLOAD_BYTES + 1),
                                                      "options": [{"label": "a"}]}]}))
    assert qb.run_hook([str(pending)], io.StringIO(big), out) == 0
    assert json.loads(out.getvalue())["stopReason"] == qb.FAILED_REASON
    assert not pending.exists()


def test_the_hook_replaces_a_link_at_the_pending_name_without_following_it(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    pending = tmp_path / qb.PENDING_NAME
    pending.symlink_to(target)
    _hook(_call(), pending)
    assert target.read_text() == "keep"
    assert not pending.is_symlink() and qb.read_pending(pending) is not None


# --- classify ---------------------------------------------------------------


def _events(sid: str, tool: str = "AskUserQuestion", tool_id: str = "toolu_abc",
            terminal: str = "hook_stopped") -> list[dict[str, Any]]:
    return [
        {"type": "system", "subtype": "init", "session_id": sid},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": tool}]}},
        {"type": "result", "subtype": "success", "session_id": sid, "terminal_reason": terminal},
    ]


def test_classify_parks_only_when_stream_and_record_agree(tmp_path):
    pending = tmp_path / qb.PENDING_NAME
    payload = _call()
    _hook(payload, pending)
    sid = payload["session_id"]
    outcome, question = qb.classify(_events(sid), pending)
    assert outcome == qb.PARKED and question.tool_use_id == "toolu_abc"
    # A record of another call or another session is not this park.
    assert qb.classify(_events(sid, tool_id="toolu_other"), pending)[0] == qb.BRIDGE_FAILED
    assert qb.classify(_events(str(uuid.uuid4())), pending)[0] == qb.BRIDGE_FAILED
    # A record with no stop in the stream is not a park either.
    assert qb.classify(_events(sid, terminal="completed"), pending)[0] == qb.BRIDGE_FAILED


def test_classify_reads_an_unrecorded_question_as_a_failure_not_an_end(tmp_path):
    pending = tmp_path / qb.PENDING_NAME
    assert qb.classify(_events(str(uuid.uuid4())), pending) == (qb.BRIDGE_FAILED, None)
    pending.write_text("{not json")
    assert qb.classify(_events(str(uuid.uuid4())), pending) == (qb.BRIDGE_FAILED, None)


def test_classify_a_run_that_asked_nothing(tmp_path):
    sid = str(uuid.uuid4())
    events = _events(sid, tool="Bash", terminal="completed")
    assert qb.classify(events, tmp_path / qb.PENDING_NAME) == (qb.NOT_ASKED, None)


def _skill_call(name: Any, tool_id: str = "toolu_skill") -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": qb.SKILL_TOOL, "input": {"skill": name}}]}}


def test_the_skill_that_asked_is_the_last_skill_call_before_the_question():
    # The owner's pick of 4 November 2026: the last `Skill` call before the
    # question names the skill that asked (finding 3).
    sid = str(uuid.uuid4())
    events = _events(sid)
    stream = [events[0], _skill_call("gstack:plan-ceo-review", "toolu_1"),
              _skill_call("gstack:office-hours", "toolu_2"), *events[1:],
              _skill_call("gstack:ship", "toolu_3")]
    assert qb.asked_by_skill(stream, "toolu_abc") == "gstack:office-hours"


@pytest.mark.parametrize("stream", [
    [],
    _events(str(uuid.uuid4())),
    [_skill_call(""), *_events(str(uuid.uuid4()))],
    [_skill_call(None), *_events(str(uuid.uuid4()))],
    [_skill_call("x" * (qb.MAX_SKILL_CHARS + 1)), *_events(str(uuid.uuid4()))],
    # A Skill call with no question after it named nothing that asked.
    [_skill_call("gstack:office-hours"), *_events(str(uuid.uuid4()), tool_id="toolu_other")],
])
def test_a_question_no_skill_call_preceded_names_no_skill(stream):
    assert qb.asked_by_skill(stream, "toolu_abc") is None


def test_the_wait_record_carries_the_skill_that_asked_or_none():
    pending = qb.parse_call(_call())
    record = qb.wait_record(pending, attempt_id="att_1", generation=1, source_revision=None,
                            asked_at="2026-11-05T10:00:00+00:00", skill="gstack:office-hours")
    assert record[qb.BRIDGE_KEY]["skill"] == "gstack:office-hours"
    bare = qb.wait_record(pending, attempt_id="att_1", generation=1, source_revision=None,
                          asked_at="2026-11-05T10:00:00+00:00")
    assert bare[qb.BRIDGE_KEY]["skill"] is None


# --- answers ----------------------------------------------------------------


def _pending() -> qb.PendingQuestion:
    return qb.parse_call(_call())


def test_the_answer_message_names_every_answer():
    text = qb.answer_message(_pending(), {
        "Which database should the plan use?": "Postgres (Recommended)",
        "Which regions?": ["europe-west1", "asia-east1"],
    })
    assert "toolu_abc" in text
    assert "Which database should the plan use? -> Postgres (Recommended)" in text
    assert "europe-west1; (in their own words) asia-east1" in text


@pytest.mark.parametrize("answers", [
    {"Which database should the plan use?": "Firestore"},
    {"Which database should the plan use?": "Firestore", "Which regions?": [], },
    {"Which database should the plan use?": ["Firestore", "Postgres (Recommended)"], "Which regions?": "a"},
    {"Which database should the plan use?": "Firestore", "Which regions?": "a", "Unasked?": "x"},
    {"Which database should the plan use?": " ", "Which regions?": "a"},
])
def test_a_partial_or_malformed_answer_never_resumes(answers):
    with pytest.raises(ValueError):
        qb.answer_message(_pending(), answers)


def test_resume_args_refuse_what_is_not_a_session_id():
    sid = str(uuid.uuid4())
    assert qb.resume_args(sid) == ["--resume", sid]
    with pytest.raises(ValueError):
        qb.resume_args("--dangerously-skip-permissions")


# --- park and resume end to end, against the fake CLI -----------------------


def test_without_the_flag_the_fake_cli_offers_no_question_tool(tmp_path, monkeypatch):
    args = _body_args(monkeypatch, tmp_path, {})
    settings = Path(args[args.index("--settings") + 1])
    code, events = _run_cli(_fake_cli(tmp_path), tmp_path / "home", tmp_path / "ws",
                            ["--print", *args], "Plan.")
    assert code == 0 and "AskUserQuestion" not in events[0]["tools"]
    assert qb.classify(events, settings.parent / qb.PENDING_NAME)[0] == qb.NOT_ASKED


def test_a_question_parks_and_the_same_session_resumes_after_a_restore(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    args = _body_args(monkeypatch, tmp_path, {qb.ENABLE_ENV: "1"})
    settings_dir = Path(args[args.index("--settings") + 1]).parent
    pending_path = settings_dir / qb.PENDING_NAME
    home = tmp_path / "home"
    code, events = _run_cli(cli, home, tmp_path / "att-1" / "repo", ["--print", *args], "Plan.")
    assert code == 0
    assert "EnterPlanMode" not in events[0]["tools"] and "AskUserQuestion" in events[0]["tools"]
    outcome, pending = qb.classify(events, pending_path)
    assert outcome == qb.PARKED
    assert pending.questions[0].options[1].label == "Firestore"
    # The checkpoint: the session's files under HOME, restored into a fresh
    # HOME for a new attempt whose workspace path differs.
    files = qb.session_files(home, pending.session_id)
    assert [p.name for p in files] == [f"{pending.session_id}.jsonl"]
    restored = tmp_path / "home-2"
    for path in files:
        target = restored / path.relative_to(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    message = qb.answer_message(pending, {q.question: q.options[0].label for q in pending.questions})
    code, resumed = _run_cli(cli, restored, tmp_path / "att-2" / "repo",
                             ["--print", *qb.resume_args(pending.session_id)], message)
    assert code == 0
    result = resumed[-1]
    assert result["session_id"] == pending.session_id
    assert json.loads(result["result"]) == {"history": 2, "answer": message}


def test_resume_without_the_restored_session_fails_loudly(tmp_path):
    code, events = _run_cli(_fake_cli(tmp_path), tmp_path / "empty", tmp_path / "ws",
                            ["--print", *qb.resume_args(str(uuid.uuid4()))], "answer")
    assert code == 1 and events[-1]["is_error"] is True


def test_session_files_include_the_sessions_tool_results(tmp_path):
    sid = str(uuid.uuid4())
    slug = tmp_path / ".claude" / "projects" / "-w-repo"
    (slug / sid / "tool-results").mkdir(parents=True)
    (slug / f"{sid}.jsonl").write_text("{}\n")
    (slug / f"{uuid.uuid4()}.jsonl").write_text("{}\n")
    assert qb.session_files(tmp_path, sid) == [slug / f"{sid}.jsonl", slug / sid]


# --- what the runner makes of a question ---------------------------------------


def test_a_recorded_question_is_unanswered_not_a_success(tmp_path):
    pending = tmp_path / qb.PENDING_NAME
    _hook(_call(), pending)
    reason = qb.unanswered([], pending)
    assert reason.startswith("question_unanswered:") and "Which database" in reason


def test_a_question_the_stream_shows_and_nothing_recorded_is_a_bridge_failure(tmp_path):
    events = _events(str(uuid.uuid4()))
    assert qb.unanswered(events, tmp_path / qb.PENDING_NAME).startswith("question_bridge_failed:")
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    assert qb.unanswered([], bad).startswith("question_bridge_failed:")


def test_a_run_that_asked_nothing_is_not_held_back(tmp_path):
    events = _events(str(uuid.uuid4()), tool="Bash", terminal="completed")
    assert qb.unanswered(events, tmp_path / qb.PENDING_NAME) is None


def test_read_stream_skips_what_is_not_an_event(tmp_path):
    stream = tmp_path / "stdout.jsonl"
    stream.write_text('warning: something\n{"type": "result"}\n{broken\n[1]\n')
    assert qb.read_stream(stream) == [{"type": "result"}]
    assert qb.read_stream(tmp_path / "missing") == []


def _body_with_stream(monkeypatch, tmp_path, env, stream_events, *, hook_payload=None):
    """claude_code's body, its fake CLI run writing `stream_events` and, when
    given, running the real hook on `hook_payload` as the CLI would."""

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        args = list(extra_args)
        if hook_payload is not None:
            settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
            entry = next(e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == qb.QUESTION_TOOL)
            subprocess.run(entry["hooks"][0]["command"], shell=True, input=json.dumps(hook_payload),
                           capture_output=True, text=True, check=True)
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        (ctx.artifacts_dir / cli_stream_files(spec).stdout).write_text(
            "".join(json.dumps(e) + "\n" for e in stream_events))
        return {"summary": "", "stdout_truncated": False, "metrics": {"duration_seconds": 1.0}}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True, exist_ok=True)
    ctx = RunnerContext(work_dir=work, artifacts_dir=work / "artifacts", input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "Plan the storage layer."}, repo_dir=work / "repo")
    return claude_code.body(ctx)


def test_on_a_recorded_question_the_runner_ends_cleanly_for_the_worker_to_park(tmp_path, monkeypatch):
    payload = _call()
    events = _events(payload["session_id"])
    # MUTATION: drop the `question_parked` mark in `body` -> this fails.
    result = _body_with_stream(monkeypatch, tmp_path, {qb.ENABLE_ENV: "1"}, events, hook_payload=payload)
    assert result["question_parked"] is True
    # The record stays for the worker, which reads it once at the park.
    assert (tmp_path / "work" / claude_code.SETTINGS_DIR_NAME / qb.PENDING_NAME).is_file()


def test_on_a_question_the_record_does_not_hold_the_runner_fails_the_attempt(tmp_path, monkeypatch):
    events = _events(str(uuid.uuid4()))
    # MUTATION: treat BRIDGE_FAILED as a normal end in `body` -> this fails.
    with pytest.raises(claude_code.RunnerFailure, match="question_bridge_failed"):
        _body_with_stream(monkeypatch, tmp_path, {qb.ENABLE_ENV: "1"}, events)


def test_on_a_stale_pending_file_from_an_earlier_run_is_not_this_runs(tmp_path, monkeypatch):
    stale = tmp_path / "work" / claude_code.SETTINGS_DIR_NAME / qb.PENDING_NAME
    stale.parent.mkdir(parents=True)
    _hook(_call(), stale)
    events = _events(str(uuid.uuid4()), tool="Bash", terminal="completed")
    result = _body_with_stream(monkeypatch, tmp_path, {qb.ENABLE_ENV: "1"}, events)
    assert result["summary"] == ""
    assert not stale.exists()


def test_off_the_runner_does_not_look_for_questions(tmp_path, monkeypatch):
    events = _events(str(uuid.uuid4()))
    result = _body_with_stream(monkeypatch, tmp_path, {}, events)
    assert result["summary"] == ""
