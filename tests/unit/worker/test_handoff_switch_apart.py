"""`swarm-handoff device` and `theme` are answered while the worker's loop is busy (PHONE-SWITCH).

MEASURED 2026-11-03 (QA target peer-app-prod, five production legs, capture
steps on GKE Autopilot): every phone-390 route of every leg failed with
"swarm-handoff device phone-390 exited 5: the worker did not answer the
device switch" or "swarm-handoff theme light exited 5: the worker did not
answer the theme change". The worker answered the spool only on its poll,
which runs on the lifecycle's loop behind the control poll, the child
requests, the tickets and the periodic checkpoint -- an upload of the work
tree, by then holding the desktop sweep's PNGs. The command waits 30 s.

Now a thread of the worker's own answers device and theme requests every
SWITCH_POLL_SECONDS, whatever the loop is doing; the poll still answers them
too, first, and one lock keeps the two from answering one file twice. The
real `swarm-handoff` command and the worker's real `HandoffPath`, over a
scripted gate client; `tick` is never called, as a loop held by a checkpoint
never calls it. No cloud credential.
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_handoff import (  # noqa: F401 -- `trace` is a fixture
    CTL,
    FakeGateClient,
    FakeLiveRoutes,
    _command_module,
    _Log,
    trace,
)

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.hardening import PROTECTED, MemoryProtection


def _path(tmp_path: Path, db, trace, gate, **over: Any):
    routes = FakeLiveRoutes(db, trace)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL}, **over,
    )
    path.setup()
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    return path, work


def _switch(cmd, root: Path, kind: str, value: str) -> int:
    """The command as the capture runs it, with a 5 s wait in place of 30 s."""
    if kind == "device":
        args = argparse.Namespace(preset=value, max_seconds=5.0, poll=0.05)
        return cmd.cmd_device(args, root)
    args = argparse.Namespace(theme=value, max_seconds=5.0, poll=0.05)
    return cmd.cmd_theme(args, root)


def test_a_device_and_a_theme_are_answered_while_the_loop_never_polls(tmp_path, db, trace, capsys):
    gate = FakeGateClient(trace)
    path, work = _path(tmp_path, db, trace, gate, answer_switches_apart=True)
    root = work / handoff_mod.SPOOL_DIR_NAME
    cmd = _command_module()
    try:
        started = time.monotonic()
        # MUTATION: answer the spool on `tick` alone and both wait out their 5 s, exit 5 `no_answer`.
        assert _switch(cmd, root, "device", "phone-390") == cmd.EXIT_RETURNED, capsys.readouterr().out
        assert _switch(cmd, root, "theme", "light") == cmd.EXIT_RETURNED, capsys.readouterr().out
        assert time.monotonic() - started < 5.0
    finally:
        path.close()
    assert ("device", {"preset": "phone-390"}) in gate.ops
    assert ("theme", {"theme": "light"}) in gate.ops
    assert not list((root / "devices").glob("*.json")) and not list((root / "themes").glob("*.json"))


def test_the_thread_stops_with_the_attempt(tmp_path, db, trace):
    gate = FakeGateClient(trace)
    path, work = _path(tmp_path, db, trace, gate, answer_switches_apart=True)
    assert any(t.name == "handoff-switches" for t in threading.enumerate())
    path.close()
    assert not any(t.name == "handoff-switches" and t.is_alive() for t in threading.enumerate())


def test_a_scripted_gate_answers_on_the_poll_alone_unless_asked(tmp_path, db, trace):
    """The default with an injected client: no thread, so the tests that
    drive `tick` step by step see every answer made by their own poll."""
    path, _work = _path(tmp_path, db, trace, FakeGateClient(trace))
    try:
        assert not any(t.name == "handoff-switches" for t in threading.enumerate())
    finally:
        path.close()


def test_the_gate_channel_takes_one_call_at_a_time():
    """The thread and the poll share L3's client, one connection answered line by line."""
    inside = threading.Semaphore(1)
    overlapped: list[str] = []

    class OneLine:
        def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
            if not inside.acquire(blocking=False):
                overlapped.append(op)
                return {}
            try:
                time.sleep(0.05)
                return {}
            finally:
                inside.release()

    gate = handoff_mod.Gate(OneLine())
    gate.registered = True
    threads = [threading.Thread(target=gate.call, args=(op, {})) for op in ("status", "device", "theme")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # MUTATION: drop `Gate._lock` and two calls write to the one connection at once.
    assert overlapped == []
