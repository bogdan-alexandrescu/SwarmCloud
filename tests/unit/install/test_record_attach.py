"""`swarm install` connects the install record on every run (lane
DAYONE-RECORD-ATTACH, part of #2688).

N1: the record is attached once `infra.pass-1` is done (a resumed run) and
right after its apply (a first run), in terraform/infra's `swarm` database,
never `(default)`. N20: the record publishes the first team's group. N10: the
ready screen's marketplace is the deployment's channel's.

No credentials, no emulator: the Firestore documents are a fake, and the
Firestore client the record would build is a fake module.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from swarm_install import cli
from swarm_install import record as install_record
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.record import InstallRecord, console_document, engine_document
from swarm_install.state import STATE_OBJECT, InstallState, StepRecord
from swarm_install.steps import Observation, Plan
from swarm_install.verbs import install

from .test_install_asks import _terminal, world  # noqa: F401 - `world` is a fixture
from .test_record_writers import FakeDocuments

PROJECT = "acme-swarm-1"
ENV = install.DEFAULT_ENV


@dataclass
class HandOffStep:
    """A stage-13 step that, like `handoff.console`, needs the console's documents."""

    id: str = "handoff.fake-console"
    stage: int = 13
    satisfies: tuple[str, ...] = ("other.fake-handoff",)
    seen: list[Any] = field(default_factory=list)

    def check(self, ctx) -> Observation:
        return Observation(MET if self.seen and self.seen[-1] else UNMET, "fake")

    def plan(self, ctx) -> Plan:
        return Plan("wait for the console", ())

    def apply(self, ctx, plan) -> None:
        self.seen.append(ctx.read_console("github_app"))

    def verify(self, ctx) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return "the hand-off reads the console"


@dataclass
class PreflightStep:
    id: str = "preflight"
    stage: int = 0
    satisfies: tuple[str, ...] = ("other.fake-preflight",)

    def check(self, ctx) -> Observation:
        return Observation(MET, "fake")

    def plan(self, ctx) -> Plan:
        return Plan("nothing", ())

    def apply(self, ctx, plan) -> None:
        raise AssertionError("preflight is met")

    def verify(self, ctx) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return "preflight"


def _seed(store, *done: str) -> None:
    state = InstallState(project=PROJECT, environment=ENV, hostname="swarm.acme.example",
                         region="europe-west1", release="2026.11.3")
    for i, step_id in enumerate(done):
        state.steps[step_id] = StepRecord(state="done", at=100.0 + i, by="admin@example.test")
    store.objects[STATE_OBJECT] = (state.to_bytes(), 1)


@pytest.fixture
def documents(monkeypatch) -> tuple[FakeDocuments, list[tuple[str, str]]]:
    """The record `open_record` builds, on fake documents; and who asked for it."""
    docs = FakeDocuments()
    opened: list[tuple[str, str]] = []

    def open_record(project: str, env: str) -> InstallRecord:
        opened.append((project, env))
        return InstallRecord(env=env, documents=docs)

    monkeypatch.setattr(install_record, "open_record", open_record, raising=False)
    return docs, opened


# -- N1: the record is attached on every run once pass 1 is done --------------------


def test_a_resumed_run_attaches_the_record_and_the_hand_off_reads_the_console(
    world, documents, monkeypatch  # noqa: F811
):
    store, found = world
    docs, opened = documents
    docs.docs[console_document(ENV, "github_app")] = ({"id": 7, "slug": "swarm-app"}, 1)
    docs._tick = 1
    _seed(store, "preflight", "infra.pass-1")
    handoff = HandOffStep()
    found[:] = [PreflightStep(), handoff]
    _terminal(monkeypatch, "", tty=False)

    code = cli.main(["install", "--project", PROJECT])

    assert code == ExitCode.OK
    assert opened == [(PROJECT, ENV)]
    assert handoff.seen == [{"id": 7, "slug": "swarm-app"}], "the hand-off sees the console's App"
    written = docs.docs[engine_document(ENV)][0]
    assert written["hostname"] == "swarm.acme.example", "every save mirrors to the record"


def test_a_run_before_pass_1_opens_no_record(world, documents, monkeypatch):  # noqa: F811
    """Before pass 1 there is no Firestore: nothing is opened, and pass 1's own
    apply attaches it (test_pass_1_attaches_the_record_after_its_apply)."""
    store, found = world
    _docs, opened = documents
    _seed(store, "preflight")
    found[:] = [PreflightStep()]
    _terminal(monkeypatch, "", tty=False)

    assert cli.main(["install", "--project", PROJECT]) == ExitCode.OK
    assert opened == []


def _fake_firestore(monkeypatch) -> list[dict[str, Any]]:
    """`google.cloud.firestore.Client`, faked: the arguments each client was built with."""
    built: list[dict[str, Any]] = []

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

    fake = types.ModuleType("google.cloud.firestore")
    fake.Client = Client  # type: ignore[attr-defined]
    import google.cloud

    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake)
    monkeypatch.setattr(google.cloud, "firestore", fake, raising=False)
    return built


def test_the_record_is_in_the_swarm_database(monkeypatch):
    """terraform/infra's database is `swarm`, and its variable forbids `(default)`."""
    built = _fake_firestore(monkeypatch)

    opened = install_record.open_record(PROJECT, ENV)
    install_record.FirestoreDocuments(PROJECT)

    assert opened.env == ENV
    assert built == [{"project": PROJECT, "database": "swarm"}] * 2


def test_upgrade_checks_read_the_record_in_the_swarm_database(monkeypatch):
    from swarm_install.verbs import upgrade

    built = _fake_firestore(monkeypatch)
    verb = upgrade.LiveWorld.__new__(upgrade.LiveWorld)
    verb.engine = types.SimpleNamespace(record=None)
    verb.project, verb.environment = PROJECT, ENV
    verb.steps = {}
    monkeypatch.setattr(upgrade, "CHECKS", ())
    monkeypatch.setattr(upgrade.LiveWorld, "ctx", lambda self: None)

    assert verb.checks() == []
    assert built == [{"project": PROJECT, "database": "swarm"}]
    assert verb.engine.record.env == ENV


# -- N20: the record publishes the first team's group --------------------------------


def _state(**outputs: Any) -> InstallState:
    return InstallState(project=PROJECT, environment=ENV, outputs=outputs)


def test_the_record_publishes_the_first_teams_group():
    record = InstallRecord(env=ENV, documents=FakeDocuments())
    groups = {"admin": "swarm-admins@acme.example", "team": "swarm-team@acme.example"}

    data = record.document_for(_state(workspace_groups=groups), {})

    assert data["workspace_groups"] == {"team": "swarm-team@acme.example"}


def test_a_one_person_install_or_one_before_stage_8_publishes_no_team():
    record = InstallRecord(env=ENV, documents=FakeDocuments())
    assert record.document_for(_state(), {})["workspace_groups"] is None
    one_person = _state(workspace_groups={"admin": "swarm-admins@acme.example"})
    assert record.document_for(one_person, {})["workspace_groups"] is None


# -- N10: the ready screen names the deployment's channel's marketplace ---------------


@pytest.mark.parametrize(
    ("release", "url"),
    [
        ("2026.11.3", "https://storage.googleapis.com/swarmcloud/marketplace.json"),
        ("2026.11.3-rc.2", "https://storage.googleapis.com/swarmcloud/channels/rc.marketplace.json"),
    ],
)
def test_the_ready_screen_names_the_channels_marketplace(release, url, world, monkeypatch, capsys):  # noqa: F811
    store, found = world
    state = InstallState(project=PROJECT, environment=ENV, hostname="swarm.acme.example",
                         region="europe-west1", release=release, ready_at=200.0)
    state.steps["preflight"] = StepRecord(state="done", at=100.0, by="admin@example.test")
    store.objects[STATE_OBJECT] = (state.to_bytes(), 1)
    found[:] = [PreflightStep()]

    assert cli.main(["install", "--status", "--project", PROJECT]) == ExitCode.OK

    out = capsys.readouterr().out
    assert f"claude plugin marketplace add {url}" in out


def test_with_no_release_recorded_the_channel_is_the_installers(monkeypatch):
    monkeypatch.setenv("SWARM_CHANNEL", "rc")
    assert install.plugin_marketplace(None).endswith("/channels/rc.marketplace.json")
    monkeypatch.delenv("SWARM_CHANNEL")
    assert install.plugin_marketplace(None) == install.PLUGIN_MARKETPLACE
