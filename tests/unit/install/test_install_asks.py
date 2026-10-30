"""`swarm install` asks for the project, domain and region it was not given
(lane DAYONE-INSTALL-ASKS, part of #2688): a new company types `swarm install`
with no flags and the installer guides the rest.

No credentials and no emulator and no real terminal: stdin is a `StringIO`
that says it is a TTY, stdout's `isatty` is faked, the steps are fakes and the
state bucket is in memory.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field

import pytest

from swarm_install import cli
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.state import STATE_OBJECT, InstallState
from swarm_install.steps import Observation, Plan
from swarm_install.verbs import install


@dataclass
class FakeStep:
    id: str
    stage: int
    satisfies: tuple[str, ...]
    met: bool = False
    applied: list[str] = field(default_factory=list)

    def check(self, ctx) -> Observation:
        return Observation(MET if self.met else UNMET, "fake")

    def plan(self, ctx) -> Plan:
        return Plan(f"create {self.id}", (f"gcloud create {self.id}",))

    def apply(self, ctx, plan) -> None:
        self.applied.append(plan.summary)
        self.met = True

    def verify(self, ctx) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return f"{self.id} is a fake step"


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}

    def read(self, name):
        return self.objects.get(name)

    def write(self, name, data, *, if_generation_match):
        current = self.objects.get(name, (b"", 0))[1]
        self.objects[name] = (data, current + 1)
        return current + 1

    def delete(self, name, *, if_generation_match):
        self.objects.pop(name, None)


class FakeStdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(install, "holder_identity", lambda: "admin@example.test")
    store = MemoryStore()
    monkeypatch.setattr(install, "open_bucket", lambda project: store)
    found = [
        FakeStep("preflight", 0, ("other.fake-preflight",), met=True),
        FakeStep("state-bucket", 1, ("other.fake-bucket",)),
    ]
    monkeypatch.setattr(install, "discover_steps", lambda: list(found))
    monkeypatch.setattr(install, "load_manifest", lambda path: None)
    return store, found


def _terminal(monkeypatch, typed: str, tty: bool = True) -> None:
    monkeypatch.setattr(sys, "stdin", FakeStdin(typed, tty))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)


def _state(tmp_home, project: str) -> InstallState:
    return InstallState.from_bytes((tmp_home / ".config/swarmcloud/install" / f"{project}.json").read_bytes())


def test_missing_project_with_a_tty_is_asked_and_validated(world, tmp_path, monkeypatch, capsys):
    _, found = world
    _terminal(monkeypatch, "Bad_Project\nacme-swarm-1\nswarm.acme.example\n\n\n")

    code = cli.main(["install"])

    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert out.count("? Google Cloud project to create or use:") == 2
    assert "'Bad_Project' is not a Google Cloud project id" in out
    assert "? Domain for the console (a hostname you control):" in out
    assert "? Region [us-central1]:" in out
    assert "Continue? [Y/n]" in out
    # It ran exactly as if the flags had been given.
    assert found[1].applied == ["create state-bucket"]
    state = _state(tmp_path, "acme-swarm-1")
    assert state.hostname == "swarm.acme.example"
    assert state.region == "us-central1"
    assert state.environment == install.DEFAULT_ENV


def test_summary_names_all_four_values_and_the_console_url(world, monkeypatch, capsys):
    _terminal(monkeypatch, "acme-swarm-1\nswarm.acme.example\neurope-west1\n\n")

    assert cli.main(["install", "--version", "2026.10.1"]) == ExitCode.OK

    out = capsys.readouterr().out
    summary = out[out.index("? Region"):out.index("Continue? [Y/n]")]
    for value in ("acme-swarm-1", "swarm.acme.example", "europe-west1", "2026.10.1",
                  "https://swarm.acme.example/"):
        assert value in summary


def test_answering_no_runs_nothing(world, tmp_path, monkeypatch, capsys):
    _, found = world
    _terminal(monkeypatch, "acme-swarm-1\nswarm.acme.example\n\nn\n")

    assert cli.main(["install"]) == ExitCode.OK

    assert found[1].applied == []
    assert not (tmp_path / ".config/swarmcloud/install/acme-swarm-1.json").exists()
    assert "nothing was changed" in capsys.readouterr().out.lower()


def test_no_tty_and_no_project_exits_2_naming_the_flag(world, monkeypatch, capsys):
    _, found = world
    _terminal(monkeypatch, "", tty=False)

    assert cli.main(["install"]) == ExitCode.USAGE

    captured = capsys.readouterr()
    assert "--project" in captured.err
    assert "?" not in captured.out
    assert found[1].applied == []


def test_no_tty_with_only_project_runs_as_before(world, monkeypatch, capsys):
    # A script that gives --project alone is unchanged: no prompt, no exit 2;
    # stage 0 owns the hostname and region then.
    _, found = world
    _terminal(monkeypatch, "", tty=False)

    assert cli.main(["install", "--project", "acme-swarm-1"]) == ExitCode.OK

    out = capsys.readouterr().out
    assert "? " not in out and "Continue?" not in out
    assert found[1].applied == ["create state-bucket"]


def test_flags_given_means_no_prompt(world, monkeypatch, capsys):
    _, found = world
    _terminal(monkeypatch, "", tty=True)  # anything read from here would be an EOF

    code = cli.main(["install", "--project", "acme-swarm-1", "--domain", "swarm.acme.example",
                     "--region", "us-east1"])

    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert "Continue?" not in out and "? Google Cloud project" not in out
    assert found[1].applied == ["create state-bucket"]


def test_a_resumed_install_is_not_asked_what_it_records(world, monkeypatch, capsys):
    store, found = world
    recorded = InstallState(project="acme-swarm-1", environment="prod",
                            hostname="swarm.acme.example", region="europe-west1")
    store.objects[STATE_OBJECT] = (recorded.to_bytes(), 1)
    _terminal(monkeypatch, "")  # a TTY, but anything read from it would be an EOF

    assert cli.main(["install", "--project", "acme-swarm-1"]) == ExitCode.OK

    assert "? " not in capsys.readouterr().out
    assert found[1].applied == ["create state-bucket"]


@pytest.mark.parametrize("mode", ["--dry-run", "--status"])
def test_dry_run_and_status_ask_only_for_the_project(world, monkeypatch, capsys, mode):
    _terminal(monkeypatch, "acme-swarm-1\n")

    code = cli.main(["install", mode])

    out = capsys.readouterr().out
    assert code != ExitCode.USAGE
    assert "? Google Cloud project to create or use:" in out
    assert "? Domain" not in out and "Continue?" not in out
    # It went on with the project typed: the dry run checked the steps, the
    # status named the project.
    assert ("check state-bucket" if mode == "--dry-run" else "acme-swarm-1") in out


def test_a_bad_domain_is_asked_again(world, monkeypatch, capsys):
    _terminal(monkeypatch, "acme-swarm-1\nnot a host\nswarm.acme.example\n\n\n")

    assert cli.main(["install"]) == ExitCode.OK

    out = capsys.readouterr().out
    assert out.count("? Domain for the console (a hostname you control):") == 2
    assert "'not a host' is not a hostname" in out


def test_a_bad_region_is_asked_again(world, monkeypatch, capsys):
    _terminal(monkeypatch, "acme-swarm-1\nswarm.acme.example\nmars\neurope-west1\n\n")

    assert cli.main(["install"]) == ExitCode.OK

    out = capsys.readouterr().out
    assert out.count("? Region [us-central1]:") == 2
    assert "'mars' is not a Google Cloud region" in out
