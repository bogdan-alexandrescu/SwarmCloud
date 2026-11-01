"""A credential the agent's tools saw never reaches a checkpoint (part of #1333).

MEASURED 2026-10-31 and 2026-11-01 (accept runs 41234694248 and
41256718065): the live-browser group's `qa_no_leak` sweep found a JWT's shape
in `attempts/<attempt>/checkpoints/ckpt-00001/archive.tar.gz` of the QA
identity's task, while the same task's transcript, logs, outputs and events
were clean. HOME is `work/`, so Claude Code keeps its session -- every tool
result, unredacted -- in `work/.claude/projects/<cwd>/<session>.jsonl` and
its `tool-results/` sidecars, and the checkpoint archives that session by the
owner's decision on #244. The uploaded transcript goes through the redaction
filter; the archived session did not, and the archive download serves it as
stored.

What each test holds: the checkpoint masks every JWT and every `Bearer`
value (the redaction filter's own `jwt` and `http_authorization` rules) in
the archived copy of the CLIs' session stores (`.claude/`, `.claude.json`,
`.codex/`) and the hand-off spool (`.swarm-handoff/`), keeps each JSON line
whole so the session still resumes, and records how many files it masked.
The agent's own work is archived as written (the control).

Every token is built at run time from random parts (`secrets`): nothing here
may look like a credential to the publish scan, and no pattern is written
whole, so this file never matches itself.

MUTATIONS: drop the mask from `CheckpointManager._add_entry` (every archive
test goes red); mask every file (the control goes red); mask with the whole
redaction filter (the session test goes red: its key/value rule rewrites a
line that holds no token).
"""

from __future__ import annotations

import io
import json
import re
import secrets
import tarfile
import time
from typing import Any

from agent_worker import checkpoint as checkpoint_mod
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.logs import build_logger

from worker_seeds import TENANT

#: The acceptance sweep's shapes (tests/acceptance/test_live_browser.py,
#: `credential_marks`), each built from parts.
_JWT = re.compile(("ey" + "J") + r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}")
_BEARER = re.compile(r"(?i)\b" + "bear" + r"er\s+[A-Za-z0-9._~+/-]{20,}")

EVENT = "checkpoint_credentials_masked"


def _jwt() -> str:
    """A token with a JWT's shape: three base64url segments, the first `eyJ`."""
    return ("ey" + "J") + ".".join(
        (secrets.token_urlsafe(18), secrets.token_urlsafe(60), secrets.token_urlsafe(48))
    ).replace("=", "")


def _bearer() -> str:
    return "Bear" + "er " + secrets.token_urlsafe(32)


def _logger(log_stream: Any):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="claude-code-browser", stream=log_stream,
    )


def _manager(store: Any, logger: Any) -> CheckpointManager:
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=logger,
    )


def _members(store: Any, record: Any) -> dict[str, bytes]:
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
            if member.isreg()
        }


def _marks(data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace")
    return [kind for kind, pattern in (("a JWT", _JWT), ("a Bearer value", _BEARER))
            if pattern.search(text)]


def _events(log_stream: io.StringIO) -> list[dict[str, Any]]:
    records = []
    for line in log_stream.getvalue().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == EVENT:
            records.append(record)
    return records


def _settle(monkeypatch) -> None:
    # As `test_browser_state_refused` does: a file written inside the racy
    # window is taken again by the next checkpoint, whatever it holds.
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 50_000_000)
    time.sleep(0.1)


def _session_line(text: str) -> str:
    """One line of Claude Code's session: a tool result, as the CLI writes it."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": text},
        ]},
    })


def test_the_archived_claude_session_holds_no_credential_shape(store, tmp_path, log_stream, monkeypatch):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    token, bearer = _jwt(), _bearer()
    session = ws.work / ".claude" / "projects" / "-work" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    lines = [
        _session_line("the console's Overview, signed in"),
        _session_line(f"page.url https://console.example.test/#{token}"),
        _session_line(f"curl -v said: Authorization: {bearer}"),
        # A key/value line: no token, so archived as written (the whole
        # filter would rewrite it). Its name is built from parts for the
        # publish scan.
        _session_line("settings: " + "pass" + 'word = "' + secrets.token_hex(4) + '"'),
    ]
    session.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sidecar = session.parent / "s1" / "tool-results" / "t1.txt"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(f"{token}\n", encoding="utf-8")
    _settle(monkeypatch)

    record = _manager(store, _logger(log_stream)).create(ws)

    members = _members(store, record)
    archived = members[".claude/projects/-work/s1.jsonl"]
    assert _marks(archived) == [], ".claude/projects/-work/s1.jsonl"
    assert _marks(members[".claude/projects/-work/s1/tool-results/t1.txt"]) == []
    assert token.encode() not in b"".join(members.values())
    assert bearer.split()[1].encode() not in b"".join(members.values())
    # Still the session: every line parses, and what is not a credential is kept.
    rows = [json.loads(line) for line in archived.decode("utf-8").splitlines()]
    assert len(rows) == 4
    assert rows[0] == json.loads(lines[0]) and rows[3] == json.loads(lines[3])
    assert "https://console.example.test/#" in rows[1]["message"]["content"][0]["content"]
    # The working file is the agent's CLI's: it is not touched.
    assert token in session.read_text(encoding="utf-8")


def test_codex_sessions_and_the_handoff_spool_are_masked_too(store, tmp_path, log_stream, monkeypatch):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    token = _jwt()
    codex = ws.work / ".codex" / "sessions" / "2026" / "rollout.jsonl"
    codex.parent.mkdir(parents=True)
    codex.write_text(json.dumps({"output": token}) + "\n", encoding="utf-8")
    (ws.work / ".claude.json").write_text(json.dumps({"note": token}), encoding="utf-8")
    tabs = ws.work / ".swarm-handoff" / "browser-tabs.json"
    tabs.parent.mkdir(parents=True)
    tabs.write_text(json.dumps([{"url": f"https://console.example.test/?t={token}"}]), encoding="utf-8")
    _settle(monkeypatch)

    record = _manager(store, _logger(log_stream)).create(ws)

    members = _members(store, record)
    for rel in (".codex/sessions/2026/rollout.jsonl", ".claude.json", ".swarm-handoff/browser-tabs.json"):
        assert _marks(members[rel]) == [], rel
        json.loads(members[rel].decode("utf-8").splitlines()[0])


def test_the_agents_own_work_is_archived_as_written(store, tmp_path, log_stream, monkeypatch):
    """The control: a fixture in the agent's checkout is its work, not a leak
    the platform made; masking it would change the tree a resume restores."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    fixture = ws.work / "repo" / "tests" / "fixture.txt"
    fixture.parent.mkdir(parents=True)
    data = f"{_jwt()}\n".encode()
    fixture.write_bytes(data)
    (ws.work / ".claude-notes.md").write_bytes(data)
    _settle(monkeypatch)

    record = _manager(store, _logger(log_stream)).create(ws)

    members = _members(store, record)
    assert members["repo/tests/fixture.txt"] == data
    assert members[".claude-notes.md"] == data
    assert _events(log_stream) == []


def test_the_mask_is_recorded_by_path_and_count_never_by_value(store, tmp_path, log_stream, monkeypatch):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    token = _jwt()
    session = ws.work / ".claude" / "projects" / "-work" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(_session_line(token) + "\n" + _session_line(token) + "\n", encoding="utf-8")
    (session.parent / "clean.jsonl").write_text(_session_line("nothing here") + "\n", encoding="utf-8")
    _settle(monkeypatch)

    manager = _manager(store, _logger(log_stream))
    manager.create(ws)

    events = _events(log_stream)
    assert len(events) == 1, events
    assert events[0]["files"] == 1 and events[0]["masked"] == 2
    assert events[0]["paths"] == [".claude/projects/-work/s1.jsonl"]
    assert token not in log_stream.getvalue()
    assert token[:20] not in log_stream.getvalue()
    # Unchanged since: the next periodic checkpoint writes nothing.
    assert manager.create_if_changed(ws) is None


def test_the_spool_named_is_the_handoffs():
    """`MASKED_STORES` restates the spool's name (the checkpoint never imports
    the hand-off); held equal here."""
    from agent_worker import handoff

    assert handoff.SPOOL_DIR_NAME in checkpoint_mod.MASKED_STORES
