"""Human-held values through a hidden prompt (DAYONE-ENGINE-2; design §2.5,
plan §6.3, review finding 11).

The Desktop client's secret is typed at a hidden prompt on /dev/tty and goes
straight to Secret Manager's addVersion, and the same typed value to the swarm
credential store of the new deployment's context (on stdin, never in argv),
so verify never needs it: stdout, stderr, the event stream and the install
state hold none of it. With no terminal there is no prompt at all:
nothing falls back to stdin, the step emits `step_paused` with reason
`needs_terminal` and the `! swarm install --resume` line, and the run exits 4.
"""

from __future__ import annotations

import getpass
import io
import json
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from swarm_install import secret_entry
from swarm_install.exitcodes import ExitCode
from swarm_install.secret_entry import NEEDS_TERMINAL, RESUME_LINE, NoTerminal
from swarm_install.steps import edge

from .test_preflight import PROJECT, Answers, FakeGcp, Rig

CLIENT_ID = "123456789012-abc123def456.apps.googleusercontent.com"
HOST = "swarm.acme.example"


class FakeContexts:
    """`swarm context list --json` and `swarm context add`, as the CLI prints
    and keeps them: the credential store is `stored`, by context."""

    def __init__(self, contexts: dict[str, dict[str, Any]] | None = None) -> None:
        self.contexts = dict(contexts or {})
        self.stored: dict[str, bytes] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: Sequence[str], stdin: str | None, env: dict[str, str] | None) -> tuple[int, str, str]:
        self.calls.append((list(argv), stdin))
        assert env is not None and not {"SWARM_URL", "SWARM_CONTEXT", "SWARM_TENANT"} & set(env)
        args = list(argv[1:])
        if args == ["context", "list", "--json"]:
            rows = [{"name": n, **c} for n, c in sorted(self.contexts.items())]
            return 0, json.dumps({"path": "/home/person/.config/swarm/config.json", "contexts": rows}), ""
        assert args[:2] == ["context", "add"] and "--use" not in args, argv
        assert "--client-secret-stdin" in args and stdin
        name = args[2]
        url = next(a.split("=", 1)[1] for a in args if a.startswith("--url="))
        client = next(a.split("=", 1)[1] for a in args if a.startswith("--client-id="))
        self.contexts[name] = {"url": url, "oauth_client_id": client, "signed_in": False}
        self.stored[name] = stdin.rstrip("\n").encode()
        return 0, f"added context {name} -> {url}\n", ""


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


def _run(tmp_path: Path, gcp: FakeGcp, open_terminal, contexts: FakeContexts | None = None) -> Rig:
    rig = Rig(tmp_path)
    rig.store.update(lambda s: setattr(s, "hostname", HOST))
    rig.contexts = contexts if contexts is not None else FakeContexts()
    step = edge.DesktopClientStep(lambda: gcp, Answers(edge_desktop_client=[CLIENT_ID]), open_terminal,
                                  rig.contexts)
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


def test_the_same_typed_value_goes_to_the_swarm_credential_store(tmp_path: Path) -> None:
    """Owner pick store-at-stage-6: the value typed once goes to Secret Manager
    and, on stdin, to the swarm credential store of a context for the new
    deployment, recorded as verify_context, so verify never needs the secret.
    A context of the hostname's name pointing elsewhere is left alone."""
    value = _value()
    terminal = FakeTerminal(value)
    elsewhere = {"url": "https://elsewhere.example", "oauth_client_id": None, "signed_in": False}
    contexts = FakeContexts({HOST: elsewhere})
    gcp = FakeGcp()
    rig = _run(tmp_path, gcp, lambda: terminal, contexts)
    assert rig.result.exit_code == ExitCode.OK, rig.result.message

    name = f"{HOST}-install"
    assert gcp.secrets[edge.DESKTOP_SECRET] == [value] and contexts.stored == {name: value}
    assert contexts.contexts[HOST] == elsewhere
    assert contexts.contexts[name] == {"url": f"https://{HOST}", "oauth_client_id": CLIENT_ID, "signed_in": False}
    assert not any(value.decode() in a for argv, _ in contexts.calls for a in argv)
    stored = rig.store.load()
    assert stored.outputs["verify_context"] == name
    assert value not in stored.to_bytes() and value.decode() not in rig.out.getvalue()
    assert all(b == 0 for b in terminal.handed[0])

    # Run again: met, nobody asked, nothing written.
    adds = len(contexts.calls)
    step = edge.DesktopClientStep(lambda: gcp, Answers(), lambda: pytest.fail("asked again"), contexts)
    assert rig.engine([step]).run().exit_code == ExitCode.OK
    assert not [argv for argv, _ in contexts.calls[adds:] if argv[1:3] == ["context", "add"]]
    assert len(gcp.secrets[edge.DESKTOP_SECRET]) == 1


def test_a_machine_without_the_context_is_asked_again_never_secret_manager(tmp_path: Path) -> None:
    """Secret Manager holds a version, but this machine's credential store has
    no context for the new deployment (another laptop): the engine never reads
    the value back (test_switches), so the step is unmet and the secret is
    typed again, to the credential store only."""
    first = _run(tmp_path, FakeGcp(), lambda: FakeTerminal(_value()))
    assert first.result.exit_code == ExitCode.OK, first.result.message
    gcp = FakeGcp()
    gcp.secrets[edge.DESKTOP_SECRET] = [b"kept"]
    value = _value()
    other = FakeContexts()
    step = edge.DesktopClientStep(lambda: gcp, Answers(), lambda: FakeTerminal(value), other)
    checks = len(first.of_kind("step_check", step.id))
    result = first.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert other.stored == {HOST: value}
    assert gcp.called("add_secret_version") == [] and gcp.secrets[edge.DESKTOP_SECRET] == [b"kept"]
    assert first.store.load().outputs["verify_context"] == HOST
    unmet = first.of_kind("step_check", step.id)[checks]
    assert unmet["state"] == "unmet" and "credential store" in json.dumps(unmet)


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
