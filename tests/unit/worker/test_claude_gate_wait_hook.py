"""A claude-code browser agent's next tool result says the gate asked a person (CAPTCHA-HANDOFF-2).

BGD, 2026-10-23 (part of #2058): when the gate sees a CAPTCHA on the agent's
page it raises the hand-off itself (CAPTCHA-HANDOFF, PR 2085), and the agent's
next tool result says a person has been asked to solve it and that it should
wait and not retry. claude-code puts text into a tool result only through a
hook, so the runner's headless settings register a PostToolUse hook
(`gate_wait_hook.py`) on an attempt with a hand-off spool. After each tool call
it reads `$SWARM_HANDOFF/.last` and `responses/<id>.json`; while that answer is
`origin: "gate"` and `outcome: "waiting"` it returns the answer's `message` as
`hookSpecificOutput.additionalContext`, and otherwise nothing. It fails open:
a missing or garbage spool prints nothing and exits 0.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker import handoff
from agent_worker.runners import claude_code

HOOK_NAME = "gate_wait_hook.py"
HOOK = Path(claude_code.__file__).resolve().parent / HOOK_NAME
MESSAGE = handoff.GATE_WAITING.format(host="www.google.com")
CALL = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "mcp__playwright__browser_click",
                   "tool_input": {}, "tool_response": "ok"})


def _spool(tmp_path: Path, request_id: str = "gate-0a1b2c", answer: object | None = None) -> Path:
    root = tmp_path / ".swarm-handoff"
    (root / "responses").mkdir(parents=True)
    (root / ".last").write_text(request_id, encoding="utf-8")
    if answer is not None:
        text = answer if isinstance(answer, str) else json.dumps({"request_id": request_id, **answer})
        (root / "responses" / f"{request_id}.json").write_text(text, encoding="utf-8")
    return root


def _run(*argv: str, payload: str = CALL) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(HOOK), *argv], input=payload,
                          capture_output=True, text=True, timeout=30)


def _gate_waiting(**extra: object) -> dict:
    return {"outcome": "waiting", "origin": "gate", "message": MESSAGE, "deadline": None, **extra}


def test_a_gate_raised_waiting_answer_becomes_the_tool_results_context(tmp_path):
    done = _run(str(_spool(tmp_path, answer=_gate_waiting())))
    assert done.returncode == 0, done.stderr  # MUTATION: the hook exits non-zero on success -- differs.
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"  # MUTATION: the event name is PreToolUse -- differs.
    assert out["additionalContext"] == MESSAGE  # MUTATION: the hook prints its own text, not the answer's message -- differs.


def test_an_agent_request_joined_to_the_gates_hand_off_is_told_too(tmp_path):
    root = _spool(tmp_path, request_id="a1b2c3", answer=_gate_waiting(joined="gate-0a1b2c"))
    done = _run(str(root))
    # MUTATION: the hook only answers a `gate-` request id -- the joined answer prints nothing, differs.
    assert json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] == MESSAGE


@pytest.mark.parametrize("answer", [
    {"outcome": "waiting", "deadline": None},  # the agent's own request: no origin
    {"outcome": "waiting", "origin": "agent", "message": MESSAGE},
    {"outcome": "returned", "origin": "gate", "message": MESSAGE, "exit": 0},
    {"outcome": "expired", "origin": "gate", "message": MESSAGE, "exit": 3},
    {"outcome": "refused", "origin": "gate", "message": MESSAGE, "exit": 5},
    {"outcome": "waiting", "origin": "gate"},  # no message to pass on
    {"outcome": "waiting", "origin": "gate", "message": ""},
    {"outcome": "waiting", "origin": "gate", "message": ["not", "text"]},
])
def test_any_other_answer_prints_nothing(tmp_path, answer):
    done = _run(str(_spool(tmp_path, answer=answer)))
    assert done.returncode == 0, done.stderr  # MUTATION: the hook raises on an answer it does not pass on -- differs.
    assert done.stdout == ""  # MUTATION: the hook ignores origin or outcome and prints the message -- differs.


def _garbage(tmp_path: Path) -> list[tuple[str, list[str]]]:
    cases: list[tuple[str, list[str]]] = [("no spool argument", [])]
    cases.append(("a spool that does not exist", [str(tmp_path / "nowhere")]))
    empty = tmp_path / "empty"
    empty.mkdir()
    cases.append(("a spool with no .last", [str(empty)]))
    cases.append(("a .last naming no answer", [str(_spool(tmp_path / "a"))]))
    cases.append(("an answer that is not JSON", [str(_spool(tmp_path / "b", answer="{not json"))]))
    cases.append(("an answer that is a list", [str(_spool(tmp_path / "c", answer="[1, 2]"))]))
    cases.append(("a .last that is not a request id",
                  [str(_spool(tmp_path / "d", request_id="../../etc/x", answer=None))]))
    # A .last that walks out of the spool, to an answer that would otherwise print.
    outside = tmp_path / "e"
    (outside / "responses").mkdir(parents=True)
    (outside / "responses" / "x.json").write_text(json.dumps(_gate_waiting()), encoding="utf-8")
    walk = _spool(tmp_path / "f")
    (walk / ".last").write_text("../../e/responses/x", encoding="utf-8")
    cases.append(("a .last that leaves the spool", [str(walk)]))
    # A symlinked answer: never followed out of the spool.
    linked = _spool(tmp_path / "g")
    (linked / "responses" / "gate-0a1b2c.json").symlink_to(outside / "responses" / "x.json")
    cases.append(("a symlinked answer", [str(linked)]))
    # A symlinked .last, likewise.
    pointer = tmp_path / "pointer"
    pointer.write_text("gate-0a1b2c", encoding="utf-8")
    via = _spool(tmp_path / "h", answer=_gate_waiting())
    (via / ".last").unlink()
    (via / ".last").symlink_to(pointer)
    cases.append(("a symlinked .last", [str(via)]))
    # An answer far larger than the worker ever writes: not read whole.
    big = _spool(tmp_path / "i", answer=json.dumps(_gate_waiting(pad="x" * (2 << 20))))
    cases.append(("an oversized answer", [str(big)]))
    return cases


def test_a_missing_or_garbage_spool_prints_nothing_and_exits_zero(tmp_path):
    cases = _garbage(tmp_path)
    assert len(cases) == 11  # MUTATION: a case dropped from the list -- the count differs.
    for label, argv in cases:
        done = _run(*argv)
        assert done.returncode == 0, (label, done.stderr)  # MUTATION: no try/except around the read -- a traceback, exit 1, differs.
        assert done.stdout == "", label  # MUTATION: the hook follows a symlink or a `..` id -- prints the outside answer, differs.


def test_garbage_on_stdin_prints_nothing_and_exits_zero(tmp_path):
    done = _run(str(_spool(tmp_path, answer={"outcome": "returned", "origin": "gate"})), payload="{not json")
    assert done.returncode == 0  # MUTATION: the hook parses stdin and raises on garbage -- exit 1, differs.
    assert done.stdout == ""  # MUTATION: the hook prints on a returned answer -- differs.


def _post_entries(settings: dict) -> list[dict]:
    return [entry for entry in settings.get("hooks", {}).get("PostToolUse", [])
            if any(HOOK_NAME in hook["command"] for hook in entry["hooks"])]


def test_a_live_browser_attempt_wires_the_hook_after_every_tool(tmp_path, monkeypatch):
    root = _spool(tmp_path, answer=_gate_waiting())
    monkeypatch.setenv(handoff.HANDOFF_ENV, str(root))
    settings = json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())
    entries = _post_entries(settings)
    assert len(entries) == 1, settings["hooks"]  # MUTATION: the hook is not wired on a browser attempt -- 0, differs.
    assert entries[0]["matcher"] == "*"  # MUTATION: matched to Bash only -- a browser_* call is not followed, differs.
    (hook,) = entries[0]["hooks"]
    assert hook["timeout"] <= 10  # MUTATION: the hook may hold a tool call for 30 s -- differs.
    argv = shlex.split(hook["command"])
    # MUTATION: the hook is the runner's copy under work/, which the agent can edit -- differs.
    assert argv[:3] == [sys.executable, "-I", str(HOOK)]
    assert argv[3:] == [str(root)]  # MUTATION: the spool is not named -- the hook reads nothing, differs.
    # And the wired command, run as the CLI runs it, passes the message on.
    done = subprocess.run(argv, input=CALL, capture_output=True, text=True, timeout=30)
    # MUTATION: the hook reads a path other than $SWARM_HANDOFF -- no message, differs.
    assert json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] == MESSAGE
    # The existing PreToolUse hooks are untouched.
    assert len(settings["hooks"]["PreToolUse"]) == 2  # MUTATION: the new hook replaces a PreToolUse entry -- differs.


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_attempt_without_the_live_browser_gets_no_hook(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(handoff.HANDOFF_ENV, raising=False)
    else:
        monkeypatch.setenv(handoff.HANDOFF_ENV, value)
    settings = json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())
    assert _post_entries(settings) == []  # MUTATION: the hook is wired for every claude-code task -- differs.
    assert "PostToolUse" not in settings["hooks"]  # MUTATION: an empty PostToolUse list is written -- differs.


def test_the_hook_is_standard_library_only():
    # `-I` drops the package's path: an import from the worker would fail
    # every call, silently (fail open), and the agent would never be told.
    source = HOOK.read_text(encoding="utf-8")
    imports = {line.split()[1].split(".")[0] for line in source.splitlines()
               if line.startswith(("import ", "from "))}
    # MUTATION: the hook imports agent_worker.handoff for GATE_ORIGIN -- differs.
    assert imports <= {"json", "os", "re", "stat", "sys", "__future__"}, imports
    assert os.access(HOOK, os.R_OK)
