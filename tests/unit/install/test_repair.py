"""`swarm install --repair <check>` (lane DAYONE-DOCTOR-1, part of #2688; plan
§1.5): re-run the one step that satisfies a manifest entry, after a typed
confirmation that ignores SWARM_ASSUME_YES.

No credentials and no emulator: the steps are fakes, the state bucket is in
memory and `HOME` is a temporary directory.
"""

from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass, field

import pytest

from swarm_install import cli
from swarm_install import manifest as m
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.state import PreconditionFailed
from swarm_install.steps import Observation, Plan
from swarm_install.verbs import install

PROJECT = "proj-repair"


@dataclass
class FakeStep:
    id: str
    stage: int
    satisfies: tuple[str, ...]
    met: bool = True
    applied: list[str] = field(default_factory=list)
    checked: int = 0

    def check(self, ctx) -> Observation:
        self.checked += 1
        return Observation(MET if self.met else UNMET, "fake")

    def plan(self, ctx) -> Plan:
        return Plan(f"repair {self.id}")

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
        if current != if_generation_match:
            raise PreconditionFailed(name)
        self.objects[name] = (data, current + 1)
        return current + 1

    def delete(self, name, *, if_generation_match):
        self.objects.pop(name, None)


def _entry(entry_id: str, step: str, stage: int) -> m.Entry:
    return m.Entry(id=entry_id, kind="other", scope="core", stage=stage, provider="engine", step=step)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(install, "holder_identity", lambda: "admin@example.test")
    store = MemoryStore()
    monkeypatch.setattr(install, "open_bucket", lambda project: store)
    steps = [
        FakeStep("foundations.apis", 1, ("api.run",)),
        FakeStep("bootstrap.pass-2", 7, ("iap-setting.frontend-programmatic-clients",), met=False),
        FakeStep("handoff.ready", 9, ("other.ready",), met=False),
    ]
    manifest = m.Manifest(entries=[
        _entry("api.run", "foundations.apis", 1),
        _entry("iap-setting.frontend-programmatic-clients", "bootstrap.pass-2", 7),
        _entry("other.ready", "handoff.ready", 9),
    ], ignored={}, env={}, sources={})
    monkeypatch.setattr(install, "discover_steps", lambda: list(steps))
    monkeypatch.setattr(install, "load_manifest", lambda path: manifest)
    return steps


def _typing(monkeypatch, answer: str | None) -> list[str]:
    asked: list[str] = []

    def typed(prompt: str) -> str:
        asked.append(prompt)
        return "" if answer is None else answer

    monkeypatch.setattr(install, "_typed", typed)
    return asked


def test_repair_needs_typed_confirmation_ignoring_assume_yes(world, monkeypatch, capsys):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    asked = _typing(monkeypatch, None)  # nothing typed: no terminal, or Enter

    code = cli.main(["install", "--project", PROJECT, "--repair", "iap-setting.frontend-programmatic-clients"])

    assert code == ExitCode.REFUSED
    assert asked and "Type iap-setting.frontend-programmatic-clients" in asked[0]
    assert world[1].applied == []
    assert "refused" in capsys.readouterr().out

    asked = _typing(monkeypatch, "yes")  # a yes is not the typed id
    assert cli.main(["install", "--project", PROJECT, "--repair",
                     "iap-setting.frontend-programmatic-clients"]) == ExitCode.REFUSED
    assert world[1].applied == []


def test_repair_runs_the_step_that_satisfies_the_check(world, monkeypatch):
    _typing(monkeypatch, "iap-setting.frontend-programmatic-clients")

    code = cli.main(["install", "--project", PROJECT, "--repair", "iap-setting.frontend-programmatic-clients"])

    assert code == ExitCode.OK
    apis, pass2, ready = world
    assert pass2.applied == ["repair bootstrap.pass-2"]
    assert apis.applied == [] and apis.checked >= 1  # earlier stages: checked, never applied
    assert ready.applied == [] and ready.checked == 0  # later stages: untouched


def test_repair_of_an_unknown_or_stepless_entry_is_a_usage_error(world, monkeypatch, capsys):
    _typing(monkeypatch, "x")
    assert cli.main(["install", "--project", PROJECT, "--repair", "api.nope"]) == ExitCode.USAGE
    assert "not a manifest entry" in capsys.readouterr().err


def test_repair_and_dry_run_are_exclusive(world):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["install", "--project", PROJECT, "--repair", "api.run", "--dry-run"])
    assert stopped.value.code == ExitCode.USAGE


def test_typed_confirmation_reads_the_terminal_never_the_environment(monkeypatch):
    # The real `_typed`: with no terminal nothing is typed, SWARM_ASSUME_YES or not.
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("input() without a terminal"))
    assert install._typed("Type x: ") == ""
