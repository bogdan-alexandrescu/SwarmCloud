"""The installer's verbs reach `swarm_install` through the existing `swarm`
command, lazily (lane DAYONE-CLI-1, part of #2688; design §2.1, plan R6).

A teammate's `swarm` is the bridge's wheel alone: it has no `swarm_install`
and must never pull in the Google clients the engine needs. So `swarm install`
imports the engine only when it runs, and without it prints the one install
line and exits 2. `swarm status` stays the teammates' one-line-per-task
command; the install's status is `swarm install --status` (review finding 13).
"""

from __future__ import annotations

import importlib.machinery
import subprocess
import sys
import types

import pytest

from swarm_mcp import cli

INSTALL_LINE = "curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh"


def _hide_swarm_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import swarm_install` fail, as on a teammate's machine."""
    for name in [m for m in sys.modules if m == "swarm_install" or m.startswith("swarm_install.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "swarm_install", None)


@pytest.mark.parametrize("verb", [v for v in cli.INSTALL_VERBS if v not in cli.CLIENT_TIER_VERBS])
def test_missing_package_prints_one_install_line(verb, monkeypatch, capsys):
    _hide_swarm_install(monkeypatch)

    code = cli.main([verb, "--project", "p"])

    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    lines = err.splitlines()
    assert len(lines) == 1, lines
    assert INSTALL_LINE in lines[0]
    assert f"swarm {verb}" in lines[0]


def test_verbs_delegate_lazily(monkeypatch):
    # Importing the bridge's CLI loads neither the engine nor a Google client.
    probe = (
        "import sys, swarm_mcp.cli\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('google', 'grpc', 'swarm_install'))\n"
        "print(','.join(bad))\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""

    # Running a verb hands the whole line, verbatim, to `swarm_install.cli.main`.
    seen: list[list[str]] = []
    fake_cli = types.ModuleType("swarm_install.cli")
    fake_cli.main = lambda argv: seen.append(list(argv)) or 4  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("swarm_install")
    fake_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_pkg.cli = fake_cli  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "swarm_install", fake_pkg)
    monkeypatch.setitem(sys.modules, "swarm_install.cli", fake_cli)

    argv = ["install", "--project", "p", "--env", "prod", "--dry-run", "--json-events"]
    assert cli.main(argv) == 4
    assert seen == [argv]


def test_install_verbs_listed_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for verb in cli.INSTALL_VERBS:
        assert verb in out


class _FakeClient:
    def __init__(self, **_kwargs) -> None:
        self.tasks = {
            "task_a": {"state": "RUNNING", "runner_profile": "claude-code"},
            "task_b": {"state": "QUEUED", "runner_profile": "codex", "step_id": "s1"},
        }

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def task(self, task_id: str) -> dict:
        return self.tasks[task_id]


def test_existing_swarm_status_unchanged(monkeypatch, capsys):
    _hide_swarm_install(monkeypatch)
    monkeypatch.setattr(cli, "SwarmClient", _FakeClient)
    monkeypatch.setattr(cli, "chosen_tenant", lambda given: given)

    code = cli.main(["status", "task_a", "task_b"])

    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("task_a  RUNNING  claude-code")
    assert lines[1].startswith("task_b  QUEUED  codex  step s1")
    assert "install" not in out
    # `status` is not an installer verb: the install's status is `swarm install --status`.
    assert "status" not in cli.INSTALL_VERBS


def test_doctor_without_the_installer_is_the_client_tier(monkeypatch, capsys):
    # DAYONE-DOCTOR-1: `doctor` is an installer verb, but a teammate's `swarm`
    # (no `swarm_install`) still answers `swarm doctor` with its client tier
    # and never with the install line.
    _hide_swarm_install(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(cli, "cmd_doctor", lambda client, args: ran.append("client") or 0)

    assert "doctor" in cli.INSTALL_VERBS
    assert cli.main(["doctor"]) == 0
    assert ran == ["client"]
    assert INSTALL_LINE not in capsys.readouterr().err


def test_doctor_with_the_installer_goes_to_it(monkeypatch):
    seen: list[list[str]] = []
    fake_cli = types.ModuleType("swarm_install.cli")
    fake_cli.main = lambda argv: seen.append(list(argv)) or 3  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("swarm_install")
    fake_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_pkg.__spec__ = importlib.machinery.ModuleSpec("swarm_install", None)  # type: ignore[attr-defined]
    fake_pkg.cli = fake_cli  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "swarm_install", fake_pkg)
    monkeypatch.setitem(sys.modules, "swarm_install.cli", fake_cli)

    assert cli.main(["doctor", "--project", "p"]) == 3
    assert seen == [["doctor", "--project", "p"]]
