"""Human-held values (DAYONE-ENGINE-2; design §2.5, plan §6.3, review
finding 11).

The Desktop client's secret is no longer typed in the terminal (issue
#2964): the console's Admin › Setup step Sign-in client adds it to Secret
Manager, and the installer reads it with the person's own credentials into
the swarm credential store of the new deployment's context (on stdin, never
in argv), so verify never needs it: stdout, stderr, the event stream and the
install state hold none of it. The hidden prompt other steps use still never
falls back to stdin.
"""

from __future__ import annotations

import argparse
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

from .test_preflight import Rig

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


def _value() -> bytes:
    return ("GOCSPX-" + secrets.token_hex(12)).encode()  # built at run time: no literal credential


def _run(tmp_path: Path, gcp: Any, contexts: FakeContexts | None = None, *, saved: bool = True,
         **outputs: Any) -> Rig:
    """The Desktop client step, after the console's Sign-in client step saved
    the client (issue #2964) unless `saved` is False."""
    from .test_sign_in_client_wait import Console, NeverAsk

    rig = Rig(tmp_path)
    rig.store.update(lambda s: (setattr(s, "hostname", HOST), s.outputs.update(outputs)))
    rig.contexts = contexts if contexts is not None else FakeContexts()
    step = edge.DesktopClientStep(lambda: gcp, NeverAsk(), rig.contexts)
    engine = rig.engine([step])
    engine.record = Console(**({"sign_in_client": {"client_id": CLIENT_ID, "saved_at": "2026-11-01T10:00:00Z",
                                                   "saved_by": "root@example.com"}} if saved else {}))
    rig.result = engine.run()
    return rig


def _gcp(value: bytes) -> Any:
    from .test_sign_in_client_wait import ConsoleGcp

    gcp = ConsoleGcp()
    gcp.secrets[edge.DESKTOP_SECRET] = [value]
    return gcp


def test_the_saved_value_goes_to_the_swarm_credential_store(tmp_path: Path, capsys) -> None:
    """Owner pick store-at-stage-6, kept with the console's Sign-in client
    step (issue #2964): the value the console saved is read with the person's
    own credentials and goes, on stdin, to the swarm credential store of a
    context for the new deployment, recorded as verify_context, so verify
    never needs the secret. A context of the hostname's name pointing
    elsewhere is left alone."""
    value = _value()
    elsewhere = {"url": "https://elsewhere.example", "oauth_client_id": None, "signed_in": False}
    contexts = FakeContexts({HOST: elsewhere})
    gcp = _gcp(value)
    rig = _run(tmp_path, gcp, contexts)
    assert rig.result.exit_code == ExitCode.OK, rig.result.message

    name = f"{HOST}-install"
    assert contexts.stored == {name: value} and gcp.called("add_secret_version") == []
    assert contexts.contexts[HOST] == elsewhere
    assert contexts.contexts[name] == {"url": f"https://{HOST}", "oauth_client_id": CLIENT_ID, "signed_in": False}
    assert not any(value.decode() in a for argv, _ in contexts.calls for a in argv)
    stored = rig.store.load()
    assert stored.outputs["verify_context"] == name
    assert stored.outputs["desktop_client_id"] == CLIENT_ID
    assert value not in stored.to_bytes() and value not in (tmp_path / "state.json").read_bytes()
    captured = capsys.readouterr()
    assert value.decode() not in captured.out + captured.err + rig.out.getvalue()

    # Run again: met, nothing read, nothing written.
    reads, adds = len(gcp.accessed), len(contexts.calls)
    again = _run(tmp_path / "again", gcp, contexts, desktop_client_id=CLIENT_ID,
                 desktop_client_saved_at="2026-11-01T10:00:00Z", verify_context=name)
    assert again.result.exit_code == ExitCode.OK, again.result.message
    assert not [argv for argv, _ in contexts.calls[adds:] if argv[1:3] == ["context", "add"]]
    assert len(gcp.accessed) == reads


def test_a_machine_without_the_context_reads_it_never_asks(tmp_path: Path) -> None:
    """A recorded client whose secret this machine's credential store lacks
    (another laptop): the step is unmet, then reads the version with the
    person's own credentials into the store. Nothing is typed, and nothing is
    added to Secret Manager."""
    value = _value()
    gcp = _gcp(value)
    other = FakeContexts()
    rig = _run(tmp_path, gcp, other, saved=False, desktop_client_id=CLIENT_ID)
    assert rig.result.exit_code == ExitCode.OK, rig.result.message
    assert other.stored == {HOST: value}
    assert gcp.called("add_secret_version") == [] and gcp.secrets[edge.DESKTOP_SECRET] == [value]
    assert rig.store.load().outputs["verify_context"] == HOST
    unmet = rig.of_kind("step_check", "edge.desktop-client")[0]
    assert unmet["state"] == "unmet" and "credential store" in json.dumps(unmet)


def test_json_events_never_carry_a_value(tmp_path: Path) -> None:
    value = _value()
    rig = _run(tmp_path, _gcp(value))
    lines = [json.loads(line) for line in rig.out.getvalue().splitlines()]
    assert lines and {e["event"] for e in lines} >= {"step_plan", "step_apply", "step_done", "run_end"}
    for event in lines:
        assert value.decode() not in json.dumps(event)

    failing = _gcp(value)

    def refuse(project, secret_id, version):
        raise RuntimeError("Secret Manager is unavailable")

    failing.access_secret_version = refuse
    rig = _run(tmp_path / "failed", failing)
    assert rig.result.exit_code == ExitCode.FAIL
    assert value.decode() not in rig.out.getvalue()


def test_the_client_step_never_reads_a_terminal(tmp_path: Path, monkeypatch) -> None:
    """With no terminal at all, the Desktop client step still never prompts:
    nothing reads stdin or getpass, and with no console record it waits."""
    piped = io.StringIO(_value().decode() + "\n")
    monkeypatch.setattr(sys, "stdin", piped)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: pytest.fail("getpass falls back to stdin"))
    monkeypatch.setattr(secret_entry.DevTty, "open", lambda: pytest.fail("the client step opened a terminal"))

    rig = _run(tmp_path, _gcp(_value()), saved=False)
    assert rig.result.exit_code == ExitCode.PAUSED
    assert not rig.of_kind("step_paused", "edge.desktop-client") or \
        rig.of_kind("step_paused", "edge.desktop-client")[0].get("reason") != NEEDS_TERMINAL
    assert piped.tell() == 0  # nothing read the pipe


def test_dev_tty_refuses_without_a_terminal(monkeypatch) -> None:
    """The hidden prompt other steps still use never falls back to stdin."""

    def no_dev_tty(*args, **kwargs):
        raise OSError(6, "No such device or address")

    monkeypatch.setattr(secret_entry.os, "open", no_dev_tty)
    with pytest.raises(NoTerminal):
        secret_entry.DevTty.open()
    assert RESUME_LINE == "! swarm install"


def test_no_message_names_a_flag_install_does_not_have() -> None:
    """#18: `swarm install` has no `--resume` (argparse exits 2 on it); it
    resumes by itself, so every line the engine hands the person says
    `swarm install`."""
    from swarm_install.verbs import install

    parser = argparse.ArgumentParser(prog="swarm install")
    install.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--resume"])
    root = Path(install.__file__).resolve().parents[1]
    named = [str(path.relative_to(root)) for path in sorted(root.rglob("*.py"))
             if "swarm install --resume" in path.read_text(encoding="utf-8")]
    assert named == []
