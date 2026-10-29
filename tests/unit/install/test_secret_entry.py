"""Human-held values through a hidden prompt (DAYONE-ENGINE-2; design §2.5,
plan §6.3, review finding 11).

The Desktop client's secret is typed at a hidden prompt on /dev/tty and goes
straight to Secret Manager's addVersion: stdout, stderr, the event stream and
the install state hold none of it. With no terminal there is no prompt at all:
nothing falls back to stdin, the step emits `step_paused` with reason
`needs_terminal` and the `! swarm install --resume` line, and the run exits 4.
"""

from __future__ import annotations

import getpass
import io
import json
import secrets
import sys
from pathlib import Path

import pytest

from swarm_install import secret_entry
from swarm_install.exitcodes import ExitCode
from swarm_install.secret_entry import NEEDS_TERMINAL, RESUME_LINE, NoTerminal
from swarm_install.steps import edge

from .test_preflight import PROJECT, Answers, FakeGcp, Rig

CLIENT_ID = "123456789012-abc123def456.apps.googleusercontent.com"


class FakeTerminal:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.written: list[str] = []
        self.handed: list[bytearray] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.written.append(text)

    def read_line(self) -> str:
        return CLIENT_ID

    def read_hidden(self) -> bytearray:
        typed = bytearray(self.value)
        self.handed.append(typed)
        return typed

    def close(self) -> None:
        self.closed = True


def _value() -> bytes:
    return ("GOCSPX-" + secrets.token_hex(12)).encode()  # built at run time: no literal credential


def _run(tmp_path: Path, gcp: FakeGcp, open_terminal) -> Rig:
    rig = Rig(tmp_path)
    step = edge.DesktopClientStep(lambda: gcp, Answers(edge_desktop_client=[CLIENT_ID]), open_terminal)
    rig.result = rig.engine([step]).run()
    return rig


def test_hidden_prompt_value_goes_straight_to_add_secret_version(tmp_path: Path, capsys) -> None:
    value = _value()
    terminal = FakeTerminal(value)
    gcp = FakeGcp()
    rig = _run(tmp_path, gcp, lambda: terminal)
    assert rig.result.exit_code == ExitCode.OK, rig.result.message

    assert gcp.secrets[edge.DESKTOP_SECRET] == [value]  # the one place it went
    assert ("create_secret", (PROJECT, edge.DESKTOP_SECRET, (("managed-by", "swarm-install"),))) in gcp.mutations
    captured = capsys.readouterr()
    text = value.decode()
    assert text not in captured.out and text not in captured.err
    assert text not in rig.out.getvalue()
    assert text not in json.dumps(rig.events.record)
    assert text not in "".join(terminal.written)
    stored = rig.store.load()
    assert text.encode() not in stored.to_bytes()
    assert text.encode() not in (tmp_path / "state.json").read_bytes()
    assert stored.outputs["desktop_client_secret_version"] == 1
    assert stored.outputs["desktop_client_id"] == CLIENT_ID
    assert all(b == 0 for b in terminal.handed[0])  # the typed buffer is wiped
    assert terminal.closed


def test_json_events_never_carry_a_value(tmp_path: Path) -> None:
    value = _value()
    gcp = FakeGcp()
    rig = _run(tmp_path, gcp, lambda: FakeTerminal(value))
    lines = [json.loads(line) for line in rig.out.getvalue().splitlines()]
    assert lines and {e["event"] for e in lines} >= {"step_plan", "step_apply", "step_done", "run_end"}
    for event in lines:
        assert value.decode() not in json.dumps(event)

    failing = FakeGcp()

    def refuse(project, secret_id, data):
        raise RuntimeError("Secret Manager is unavailable")

    failing.add_secret_version = refuse
    rig = _run(tmp_path / "failed", failing, lambda: FakeTerminal(value))
    assert rig.result.exit_code == ExitCode.FAIL
    assert value.decode() not in rig.out.getvalue()


def test_refuses_without_tty(tmp_path: Path, monkeypatch) -> None:
    value = _value()
    piped = io.StringIO(value.decode() + "\n")
    monkeypatch.setattr(sys, "stdin", piped)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: pytest.fail("getpass falls back to stdin"))

    def no_tty():
        raise NoTerminal("no controlling terminal")

    gcp = FakeGcp()
    rig = _run(tmp_path, gcp, no_tty)
    assert rig.result.exit_code == ExitCode.PAUSED
    paused = rig.of_kind("step_paused", "edge.desktop-client")
    assert paused and paused[0]["reason"] == NEEDS_TERMINAL
    assert paused[0]["command"] == RESUME_LINE == "! swarm install --resume"
    assert RESUME_LINE in rig.result.message
    assert gcp.called("add_secret_version") == []
    assert piped.tell() == 0  # nothing read the pipe

    def no_dev_tty(*args, **kwargs):
        raise OSError(6, "No such device or address")

    monkeypatch.setattr(secret_entry.os, "open", no_dev_tty)
    with pytest.raises(NoTerminal):
        secret_entry.DevTty.open()
