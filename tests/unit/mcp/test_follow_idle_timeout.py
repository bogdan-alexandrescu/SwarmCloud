"""A held `swarm_follow` ends inside Claude Code's MCP idle timeout, and a row survives one that does not.

MEASURED, 2026-10-04, workflow wf_a627d6b525e24823aa83 (lane U12): its review
and fix rows ended UNKNOWN with `MCP server timeout: swarm_follow call exceeded
idle timeout while waiting for parent tasks` while their tasks ran on. A row's
parent hold is ONE call of up to 1800 s, and the bridge sent nothing during it.

WHAT CLAUDE CODE DOES (read from the 2.1.283 binary, `bin/claude.exe`): every
MCP tool call gets a watchdog that ticks every 30 s and aborts the call when
nothing -- no response and no progress notification -- arrived for the idle
timeout. That is `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, else a per-server
`timeout`, else 1,800,000 ms for a stdio server (the plugin's) and 300,000 ms
for the others. Its `onprogress` handler sets the idle clock to now, and the
bundled MCP SDK sends `_meta.progressToken` (the request id) on every call made
with an `onprogress`. A notification with any other token is dropped as
unknown, so the bridge echoes the token it was given.

So the bridge sends `notifications/progress` every `server.KEEPALIVE_SECONDS`
for as long as a call that carried a token runs, and a progress hold that
carried none is capped at `compact.SILENT_MAX_WAIT_SECONDS`, well inside the
stdio default, and the row re-issues it.

Every test here replays the watchdog's rule over what the bridge actually
wrote, with a short fake timeout, and each has a control that the rule cuts.
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from swarm_mcp import compact, server

from test_follow_hold import Timeline

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_STEP_MD = _PLUGIN / "agents" / "step.md"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"

#: The version the previous change shipped; this one must be past it.
_PREVIOUS_RELEASED_VERSION = (0, 5, 12)

#: Claude Code's error, as lane U12 met it. A row reads it as an ordinary call
#: error, not as the step's end.
_HOST_TIMEOUT = (
    "MCP server timeout: swarm_follow call exceeded idle timeout while waiting for parent "
    "tasks. Increase CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT or per-server timeout in MCP settings."
)


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


# --------------------------------------------------------------------------
# The host: what was written, when, and Claude Code's idle rule over it
# --------------------------------------------------------------------------


class _Wire:
    """`sys.stdout` for `server.serve`: every line, stamped as it is written."""

    def __init__(self) -> None:
        self.lines: list[tuple[float, dict]] = []
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self.lines.append((time.monotonic(), json.loads(line)))
        return len(text)

    def flush(self) -> None:
        return None


def _serve_one_call(monkeypatch, world, arguments: dict, *, token=None) -> tuple[float, _Wire]:
    """Serve ONE `swarm_follow` call the way Claude Code sends it. Returns the
    time the request went in and what came out."""
    monkeypatch.setattr(server, "SwarmClient", lambda **_: world)
    params: dict = {"name": "swarm_follow", "arguments": arguments}
    if token is not None:
        params["_meta"] = {"progressToken": token}
    request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}) + "\n"
    wire = _Wire()
    monkeypatch.setattr(sys, "stdout", wire)
    began = time.monotonic()
    server.serve(stdin=io.StringIO(request))
    return began, wire


def _cut_at(began: float, wire: _Wire, *, token, idle: float) -> float | None:
    """Claude Code's watchdog, replayed: the idle clock starts at the request
    and is reset by a progress notification carrying THIS call's token; the
    call is aborted the moment it exceeds `idle`. Returns when it would have
    been cut, or None when the response arrived first."""
    last = began
    for at, message in wire.lines:
        if at - last > idle:
            return last + idle
        if message.get("id") == 7:
            return None
        if (message.get("method") == "notifications/progress"
                and token is not None
                and message.get("params", {}).get("progressToken") == token):
            last = at
    raise AssertionError("the call was never answered")


class _RealTimeTimeline(Timeline):
    """`Timeline` on the real clock, for a hold served end to end: its phases
    are in real seconds from construction."""

    def __init__(self, phases, steps) -> None:
        self._t0 = time.monotonic()
        super().__init__(phases, steps)

    @property
    def now(self) -> float:
        return time.monotonic() - self._t0

    @now.setter
    def now(self, _value: float) -> None:
        return None


@pytest.fixture
def fast(monkeypatch):
    """Seconds where the bridge has minutes: the hold re-reads every 50 ms and
    the keepalive goes every 100 ms."""
    monkeypatch.setattr(compact, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(compact, "PENDING_POLL_CAP_SECONDS", 0.05)
    monkeypatch.setattr(server, "KEEPALIVE_SECONDS", 0.1)


#: The fake idle timeout: shorter than the hold below, five keepalives long.
_FAKE_IDLE = 0.5


def _parent_runs_past_the_idle_timeout() -> _RealTimeTimeline:
    return _RealTimeTimeline(
        {
            "task_parent": [(0, "RUNNING", None), (1.5, "SUCCEEDED", None)],
            "task_child": [(0, "PARKED", "DEPENDENCY_INCOMPLETE"), (1.55, "READY", None)],
        },
        {"task_parent": "implement", "task_child": "review"},
    )


_PARENT_CALL = {
    "task_ids": ["task_child"], "step_id": "review", "format": "progress",
    "wait_seconds": 1800, "parents": ["task_parent"],
}


# --------------------------------------------------------------------------
# A parent wait longer than the idle timeout completes
# --------------------------------------------------------------------------


def test_a_parent_wait_longer_than_the_idle_timeout_completes_without_the_row_ending(monkeypatch, fast):
    """The parent runs 1.5 s, three times the fake 0.5 s idle timeout. The
    child's one parent call is answered with the parent finished, and the
    watchdog never fires on the way."""
    world = _parent_runs_past_the_idle_timeout()
    began, wire = _serve_one_call(monkeypatch, world, dict(_PARENT_CALL), token=7)

    assert _cut_at(began, wire, token=7, idle=_FAKE_IDLE) is None
    (answer,) = [m for _, m in wire.lines if m.get("id") == 7]
    reply = json.loads(answer["result"]["content"][0]["text"])
    assert reply["parents"] == {"task_parent": "SUCCEEDED"}
    assert world.now >= 1.5


def test_control_the_same_wait_with_no_keepalive_is_cut_by_the_idle_rule(monkeypatch, fast):
    """The measurement could come out the other way: the same 1.5 s hold, from
    a host that sent no progress token, is silent for longer than the idle
    timeout -- what Claude Code cut on 2026-10-04."""
    world = _parent_runs_past_the_idle_timeout()
    began, wire = _serve_one_call(monkeypatch, world, dict(_PARENT_CALL))

    assert _cut_at(began, wire, token=None, idle=_FAKE_IDLE) is not None
    assert not [m for _, m in wire.lines if m.get("method") == "notifications/progress"]


# --------------------------------------------------------------------------
# The keepalive: at the interval, for the whole hold, with the host's token
# --------------------------------------------------------------------------


def test_progress_goes_out_at_the_interval_for_the_whole_hold_and_never_after_the_answer(monkeypatch, fast):
    world = _parent_runs_past_the_idle_timeout()
    began, wire = _serve_one_call(monkeypatch, world, dict(_PARENT_CALL), token=7)

    stamps = [began]
    answered = None
    values = []
    for at, message in wire.lines:
        if message.get("id") == 7:
            answered = at
            continue
        assert answered is None, "a progress notification followed the answer"
        assert message["method"] == "notifications/progress"
        assert message["params"]["progressToken"] == 7  # echoed, number for number
        assert "id" not in message  # a notification
        values.append(message["params"]["progress"])
        stamps.append(at)
    assert answered is not None
    stamps.append(answered)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # Every gap is one interval, give or take a scheduler tick -- never one
    # anywhere near the idle timeout.
    assert max(gaps) < server.KEEPALIVE_SECONDS * 2.5, gaps
    assert len(values) >= 10, values
    assert values == sorted(set(values)), "progress must increase on every notification"


def test_a_string_progress_token_is_echoed_as_a_string(monkeypatch, fast):
    world = _parent_runs_past_the_idle_timeout()
    _began, wire = _serve_one_call(monkeypatch, world, dict(_PARENT_CALL), token="tok-7")
    tokens = {m["params"]["progressToken"] for _, m in wire.lines if m.get("method") == "notifications/progress"}
    assert tokens == {"tok-7"}


def test_the_keepalive_interval_is_well_under_every_default_idle_timeout():
    """Claude Code's defaults: 1800 s for a stdio server, 300 s for the rest,
    checked every 30 s. One a minute is a fifth of the shortest."""
    assert server.KEEPALIVE_SECONDS <= 300 / 4
    assert server.KEEPALIVE_SECONDS >= 10, "a keepalive every few seconds is noise on the wire"


# --------------------------------------------------------------------------
# No token: the hold is capped, well inside the idle timeout
# --------------------------------------------------------------------------


def test_a_progress_hold_without_a_keepalive_never_passes_the_safe_limit():
    world = Timeline({"task_a": [(0, "RUNNING", None), (99_999, "SUCCEEDED", None)]}, {"task_a": "a"})
    first = json.loads(server._call(world, "swarm_follow", {"task_ids": ["task_a"], "format": "progress"}))
    world.sleep(0)
    calls = {"task_ids": ["task_a"], "format": "progress", "wait_seconds": 1800, "since": first["since"]}

    original = compact.watch_progress

    def timed(*args, **kwargs):
        return original(*args, sleep=world.sleep, clock=world.clock, now=world.wall,
                        **{k: v for k, v in kwargs.items() if k not in ("sleep", "clock", "now")})

    import unittest.mock as mock

    with mock.patch.object(compact, "watch_progress", timed):
        start = world.now
        server._call(world, "swarm_follow", dict(calls))
        silent = world.now - start
        start = world.now
        server._call(world, "swarm_follow", dict(calls), keepalive=True)
        kept_alive = world.now - start

    assert silent == compact.SILENT_MAX_WAIT_SECONDS
    assert kept_alive == compact.MAX_WAIT_SECONDS == 1800
    # Well inside the 1800 s stdio default the watchdog checks every 30 s.
    assert compact.SILENT_MAX_WAIT_SECONDS <= 1800 * 2 / 3


def test_the_served_call_passes_the_cap_only_when_the_host_sent_no_token(monkeypatch):
    seen = []

    def record(client, task_ids, **kwargs):
        seen.append(kwargs["max_wait"])
        return {"progress": [], "changed": False, "transitions": [], "stop": True, "since": "", "tasks": []}

    monkeypatch.setattr(compact, "watch_progress", record)
    world = Timeline({"task_a": [(0, "RUNNING", None)]}, {"task_a": "a"})
    args = {"task_ids": ["task_a"], "format": "progress", "wait_seconds": 1800}
    _serve_one_call(monkeypatch, world, dict(args), token=3)
    _serve_one_call(monkeypatch, world, dict(args))
    assert seen == [compact.MAX_WAIT_SECONDS, compact.SILENT_MAX_WAIT_SECONDS]


def test_the_cap_is_stated_once_beside_the_idle_timeout_it_sits_under():
    source = Path(compact.__file__).read_text()
    at = source.index("SILENT_MAX_WAIT_SECONDS = ")
    comment = source[max(0, at - 2500):at]
    assert "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT" in comment
    assert "1,800,000" in comment or "1800000" in comment


# --------------------------------------------------------------------------
# A row: a cut call is repeated with the same since/parents
# --------------------------------------------------------------------------


class _HostThatCuts:
    """Claude Code holding a row's calls, simulated time: a call whose hold
    ran longer than `idle` with no keepalive answers the host's timeout error
    instead of the bridge's reply, `cuts` times; the reply is lost."""

    def __init__(self, world: Timeline, *, idle: float, cuts: int) -> None:
        self.world = world
        self.idle = idle
        self.cuts = cuts
        self.calls: list[dict] = []

    def follow(self, arguments: dict) -> dict:
        self.calls.append(dict(arguments))
        start = self.world.now
        reply = compact.watch_progress(
            self.world, arguments["task_ids"], since=arguments.get("since"),
            wait_seconds=arguments["wait_seconds"], step_id=arguments["step_id"],
            parents=arguments.get("parents"), max_wait=compact.MAX_WAIT_SECONDS,
            sleep=self.world.sleep, clock=self.world.clock, now=self.world.wall,
        )
        if self.cuts and self.world.now - start >= self.idle:
            self.cuts -= 1
            raise RuntimeError(_HOST_TIMEOUT)
        return reply


def _row_per_step_md(host: _HostThatCuts, task_id: str, step_id: str, parents: list[str]) -> dict:
    """`plugin/agents/step.md` sections 1-2 as a loop: a call error -- the
    host's timeout included -- repeats the SAME call (same `since`, same
    `parents`); five in a row end the row UNKNOWN; 56 calls end it `running`."""
    arguments: dict = {"task_ids": [task_id], "step_id": step_id, "format": "progress",
                       "wait_seconds": compact.MAX_WAIT_SECONDS}
    if parents:
        arguments["parents"] = list(parents)
    errors = 0
    calls = 0
    while True:
        calls += 1
        if calls > 56:
            return {"state": "running"}
        try:
            reply = host.follow(arguments)
        except RuntimeError as exc:
            errors += 1
            if errors >= 5:
                return {"state": "UNKNOWN", "last_error": str(exc)}
            continue
        errors = 0
        if reply["stop"]:
            return {"state": reply["tasks"][0]["outcome"]["state"], "calls": calls}
        arguments = {"task_ids": [task_id], "step_id": step_id, "format": "progress",
                     "wait_seconds": compact.MAX_WAIT_SECONDS, "since": reply["since"]}
        pending = {p: s for p, s in (reply.get("parents") or {}).items()
                   if s not in ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")}
        if pending:
            arguments["parents"] = sorted(pending)


def test_a_timeout_mid_wait_is_retried_with_the_same_since_and_parents_and_the_row_ends_with_the_real_state():
    """The parent runs five hours; the host cuts the first two holds. The row
    repeats each cut call unchanged and ends SUCCEEDED, not UNKNOWN."""
    world = Timeline(
        {
            "task_parent": [(0, "RUNNING", None), (5 * 3600, "SUCCEEDED", None)],
            "task_child": [(0, "PARKED", "DEPENDENCY_INCOMPLETE"), (5 * 3600 + 5, "READY", None),
                           (5 * 3600 + 60, "RUNNING", None), (5 * 3600 + 900, "SUCCEEDED", None)],
        },
        {"task_parent": "implement", "task_child": "review"},
    )
    host = _HostThatCuts(world, idle=1800, cuts=2)
    got = _row_per_step_md(host, "task_child", "review", ["task_parent"])

    assert got["state"] == "SUCCEEDED", got
    first, second, third = host.calls[:3]
    assert first == second == third, "a cut call must be repeated exactly"
    assert third["parents"] == ["task_parent"] and "since" not in third


def test_a_cut_call_that_carried_since_is_repeated_with_that_since():
    world = Timeline({"task_a": [(0, "RUNNING", None), (4000, "SUCCEEDED", None)]}, {"task_a": "a"})
    host = _HostThatCuts(world, idle=1800, cuts=1)
    host_follow = host.follow
    cut_after_first = {"n": 0}

    def follow(arguments):
        cut_after_first["n"] += 1
        if cut_after_first["n"] == 1:
            host.calls.append(dict(arguments))
            return compact.watch_progress(world, arguments["task_ids"], step_id="a",
                                          sleep=world.sleep, clock=world.clock, now=world.wall)
        return host_follow(arguments)

    host.follow = follow
    got = _row_per_step_md(host, "task_a", "a", [])
    assert got["state"] == "SUCCEEDED", got
    cut, repeated = host.calls[1], host.calls[2]
    assert cut == repeated and cut["since"], (cut, repeated)


def test_step_md_treats_the_hosts_timeout_like_any_other_call_error():
    flat = _flat(_STEP_MD)
    lowered = flat.lower()
    assert "mcp" in lowered and "timeout" in lowered and "idle timeout" in lowered
    at = lowered.index("idle timeout")
    rule = lowered[max(0, at - 600):at + 600]
    assert "same `since`" in rule and "same `parents`" in rule, rule
    assert "not the end of the row" in rule or "not a reason to answer" in rule, rule
    # Section 1's parent call is covered too, not only section 2's.
    assert "section 1" in rule, rule
    # Within the five-errors rule.
    assert "five errors in a row" in lowered


# --------------------------------------------------------------------------
# The turn budget covers the stated hours
# --------------------------------------------------------------------------


def _stated_hours(flat: str) -> tuple[int, int]:
    found = re.search(r"56 calls cover (\d+) hours.*?56 of them cover (\d+) hours", flat)
    assert found, "step.md must state the hours its 56-call budget covers, both ways"
    return int(found.group(1)), int(found.group(2))


def test_the_turn_budget_covers_the_hours_step_md_states():
    flat = _flat(_STEP_MD)
    with_keepalive, without = _stated_hours(flat)
    assert 56 * compact.MAX_WAIT_SECONDS / 3600 >= with_keepalive
    assert 56 * compact.SILENT_MAX_WAIT_SECONDS / 3600 >= without
    assert without >= 12, "a step waiting hours on its parents must not run out of turns"
    assert "maxTurns: 60" in Path(_STEP_MD).read_text()
    # Every call counts: the parent call and every repeat after an error.
    assert "every `swarm_follow` call" in flat.lower()


def test_a_row_waits_the_stated_hours_on_its_parents_within_the_budget():
    """Capped holds, a parent that runs the stated hours less one -- the hour
    is the three calls step.md says the step's own state changes take once
    its parents are done: the row still ends with the task's state, inside
    56 calls."""
    flat = _flat(_STEP_MD)
    _, without = _stated_hours(flat)
    assert "takes one call of those 56" in flat
    finish = (without - 1) * 3600
    world = Timeline(
        {
            "task_parent": [(0, "RUNNING", None), (finish, "SUCCEEDED", None)],
            "task_child": [(0, "PARKED", "DEPENDENCY_INCOMPLETE"), (finish + 5, "READY", None),
                           (finish + 60, "RUNNING", None), (finish + 600, "SUCCEEDED", None)],
        },
        {"task_parent": "implement", "task_child": "review"},
    )

    class Capped(_HostThatCuts):
        def follow(self, arguments):
            self.calls.append(dict(arguments))
            return compact.watch_progress(
                self.world, arguments["task_ids"], since=arguments.get("since"),
                wait_seconds=arguments["wait_seconds"], step_id=arguments["step_id"],
                parents=arguments.get("parents"), max_wait=compact.SILENT_MAX_WAIT_SECONDS,
                sleep=self.world.sleep, clock=self.world.clock, now=self.world.wall,
            )

    host = Capped(world, idle=1800, cuts=0)
    got = _row_per_step_md(host, "task_child", "review", ["task_parent"])
    assert got["state"] == "SUCCEEDED", got
    assert got["calls"] <= 56


def test_a_re_issued_hold_is_not_a_poll():
    """One call per hold: a capped parent wait costs one call every
    `SILENT_MAX_WAIT_SECONDS`, three an hour at most."""
    assert 3600 / compact.SILENT_MAX_WAIT_SECONDS <= 3
    flat = _flat(_STEP_MD).lower()
    assert "not a poll" in flat


# --------------------------------------------------------------------------
# Version and pin, together
# --------------------------------------------------------------------------


def test_the_plugin_version_and_its_bridge_pin_were_bumped_together():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#subdirectory=apps/swarm-mcp" in arg for arg in pinned), pinned
