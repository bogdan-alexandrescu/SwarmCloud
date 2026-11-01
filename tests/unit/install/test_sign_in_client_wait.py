"""The Desktop sign-in client comes from the console (issue #2964).

`edge.brand` and `edge.desktop-client` no longer prompt: Admin › Setup's
Sign-in client step takes the client's id and secret, and the installer
waits for it after the hand-off. Once the console saved it, the installer
reads the secret with the person's own credentials into the new deployment's
credential store and runs the bootstrap and infra passes that put the id in
IAP's programmatic-clients list and the discovery document. A client the old
terminal prompt recorded is met as before.

On a fake Google Cloud with no credentials.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from swarm_install.exitcodes import ExitCode
from swarm_install.steps import bootstrap, discover, edge, handoff, kind_of
from swarm_install.steps.bootstrap import Deps, applied_key, generate, inputs_digest
from swarm_install.steps.preflight import remember

from .test_edge_probes import FakeEdge
from .test_preflight import NUMBER, FakeGcp, Rig
from .test_secret_entry import FakeContexts

HOST = "swarm.example.com"
IP = "203.0.113.7"


def _client() -> str:
    return f"{secrets.randbelow(10**12)}-{secrets.token_hex(8)}.apps.googleusercontent.com"


def _value() -> bytes:
    return ("GOCSPX-" + secrets.token_hex(12)).encode()  # built at run time: no literal credential


class NeverAsk:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, ctx: Any, step_id: str, question: str, default: str | None = None) -> str:
        self.asked.append(step_id)
        raise AssertionError(f"{step_id} asked {question!r}: the sign-in client is never asked in the terminal")


class ConsoleGcp(FakeGcp):
    """Secret Manager with a payload the person's own credentials can read."""

    def __init__(self) -> None:
        super().__init__()
        self.accessed: list[tuple[str, int]] = []

    def access_secret_version(self, project: str, secret_id: str, version: int) -> bytes:
        self.accessed.append((secret_id, version))
        return self.secrets[secret_id][version - 1]


class Console:
    """The install record's console documents."""

    def __init__(self, **docs: dict[str, Any]) -> None:
        self.docs = dict(docs)

    def read_console(self, name: str) -> dict[str, Any] | None:
        return self.docs.get(name)

    def write(self, *args: Any, **kwargs: Any) -> None:
        pass


def _rig(tmp_path: Path, **outputs: Any) -> Rig:
    rig = Rig(tmp_path)
    rig.store.update(lambda s: (setattr(s, "hostname", HOST),
                                s.outputs.update({"project_number": NUMBER, "global_ip": IP, **outputs})))
    return rig


def _context(engine: Any) -> Any:
    """The context `Engine.run` builds, for driving one step's methods."""
    from swarm_install.engine import Context

    engine.ctx = Context(project=engine.store.project, environment=engine.store.environment,
                         holder=engine.holder, state=engine.store.load(), events=engine.events,
                         clock=engine.clock, dry_run=False, engine=engine)
    return engine.ctx


def _ordered() -> list[Any]:
    found = discover()
    return sorted(found, key=lambda step: (step.stage, found.index(step)))


def test_after_stage_1_the_client_is_a_wait_after_the_hand_off_not_a_prompt() -> None:
    """Red first: stage 2 holds no step that asks for the client; the two
    client steps are waits placed after `handoff.console` in the order."""
    ordered = _ordered()
    ids = [step.id for step in ordered]
    by_id = {step.id: step for step in ordered}
    for step_id in ("edge.brand", "edge.desktop-client"):
        assert kind_of(by_id[step_id]) == "wait", step_id
        assert by_id[step_id].stage == handoff.HandoffStep.stage, step_id
        assert ids.index(step_id) > ids.index("handoff.console"), step_id
    assert ids.index("edge.desktop-client") < ids.index("handoff.ready")
    after_stage_1 = [step for step in ordered if step.stage == 2]
    assert "edge.client-secret-container" in [step.id for step in after_stage_1]
    assert not {"edge.brand", "edge.desktop-client"} & {step.id for step in after_stage_1}


def test_stage_2_makes_the_secrets_container_and_asks_nothing(tmp_path: Path) -> None:
    gcp = ConsoleGcp()
    ask = NeverAsk()
    rig = _rig(tmp_path)
    steps = edge.build_steps(lambda: gcp, ask, edge=lambda: FakeEdge(), contexts=FakeContexts())
    container = next(step for step in steps if step.id == "edge.client-secret-container")
    result = rig.engine([container]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert gcp.secrets[edge.DESKTOP_SECRET] == [], "the container exists, with no version"
    assert ask.asked == []


def test_with_no_console_record_the_client_steps_wait_and_ask_nothing(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(edge, "open_page", lambda url, *, why, **_: opened.append(url))
    gcp = ConsoleGcp()
    ask = NeverAsk()
    rig = _rig(tmp_path)
    brand, client = handoff.client_steps(lambda: gcp, ask, edge=lambda: FakeEdge(), contexts=FakeContexts())
    engine = rig.engine([brand, client])
    engine.record = Console()
    result = engine.run()
    assert result.exit_code == ExitCode.PAUSED, result.message
    assert ask.asked == [] and opened == []
    assert rig.of_kind("wait_countdown", "edge.brand"), "the engine polls the console's record"


def test_the_consoles_saved_client_is_taken_without_a_prompt(tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    client_id, value = _client(), _value()
    gcp = ConsoleGcp()
    gcp.secrets[edge.DESKTOP_SECRET] = [value]
    contexts = FakeContexts()
    rig = _rig(tmp_path)
    brand, client = handoff.client_steps(lambda: gcp, NeverAsk(), edge=lambda: FakeEdge(), contexts=contexts)
    engine = rig.engine([brand, client])
    engine.record = Console(sign_in_client={"client_id": client_id, "saved_at": "2026-11-01T10:00:00Z",
                                            "saved_by": "root@example.com"})
    result = engine.run()
    assert result.exit_code == ExitCode.OK, result.message
    outputs = rig.store.load().outputs
    assert outputs["desktop_client_id"] == client_id
    assert outputs["desktop_client_secret_version"] == 1
    assert contexts.stored == {HOST: value}, "the secret went to the new deployment's credential store"
    assert gcp.accessed == [(edge.DESKTOP_SECRET, 1)]
    captured = capsys.readouterr()
    text = value.decode()
    assert text not in captured.out + captured.err
    assert text not in json.dumps(rig.events.record) and text not in json.dumps(outputs)


def test_a_client_id_the_terminal_recorded_before_is_met(tmp_path: Path) -> None:
    """Resume: a state from the old terminal prompt (id recorded, secret in
    Secret Manager and in this machine's credential store) is met as before,
    and nothing is read again."""
    client_id, value = _client(), _value()
    gcp = ConsoleGcp()
    gcp.secrets[edge.DESKTOP_SECRET] = [value]
    contexts = FakeContexts({HOST: {"url": f"https://{HOST}", "oauth_client_id": client_id, "signed_in": True}})
    rig = _rig(tmp_path, desktop_client_id=client_id, verify_context=HOST)
    brand, client = handoff.client_steps(lambda: gcp, NeverAsk(), edge=lambda: FakeEdge(), contexts=contexts)
    engine = rig.engine([brand, client])
    engine.record = Console()
    result = engine.run()
    assert result.exit_code == ExitCode.OK, result.message
    assert gcp.accessed == [] and contexts.stored == {}
    assert not rig.of_kind("wait_countdown", "edge.brand")


def test_a_second_console_save_replaces_the_recorded_client(tmp_path: Path) -> None:
    old, new, value = _client(), _client(), _value()
    gcp = ConsoleGcp()
    gcp.secrets[edge.DESKTOP_SECRET] = [_value(), value]
    contexts = FakeContexts({HOST: {"url": f"https://{HOST}", "oauth_client_id": old, "signed_in": True}})
    rig = _rig(tmp_path, desktop_client_id=old, verify_context=HOST)
    record = edge.SignInRecordStep(lambda: gcp, NeverAsk(), contexts=contexts)
    engine = rig.engine([])
    engine.record = Console(sign_in_client={"client_id": new, "saved_at": "2026-11-01T11:00:00Z",
                                            "saved_by": "x@example.com"})
    ctx = _context(engine)
    assert record.trigger(ctx)
    record.apply(ctx, record.plan(ctx))
    outputs = rig.store.load().outputs
    assert outputs["desktop_client_id"] == new and outputs["desktop_client_secret_version"] == 2
    assert contexts.stored == {HOST: value}
    assert record.check(ctx).status == "met"
    assert not record.trigger(ctx)


# -- the hand-off runs the passes once the console saved the client -----------


class World:
    """The project and the console for the hand-off's triggered sign-in steps."""

    def __init__(self, client_id: str, value: bytes) -> None:
        self.docs: dict[str, Any] = {}
        self.client_id, self.value = client_id, value
        self.log: list[str] = []

    def save(self) -> None:
        self.docs["sign_in_client"] = {"client_id": self.client_id, "saved_at": "2026-11-01T10:00:00Z",
                                       "saved_by": "root@example.com"}

    def read_console(self, name: str) -> dict[str, Any] | None:
        return self.docs.get(name)

    def write(self, *args: Any, **kwargs: Any) -> None:
        pass

    def secret(self, project: str, name: str) -> dict[str, Any] | None:
        return {"name": name} if name == edge.DESKTOP_SECRET else None

    def enabled_version(self, project: str, name: str) -> int | None:
        return 1 if name == edge.DESKTOP_SECRET and self.docs else None

    def access_secret_version(self, project: str, secret_id: str, version: int) -> bytes:
        self.log.append(f"read {secret_id}")
        return self.value


def test_the_hand_off_runs_bootstrap_and_infra_once_the_console_saved_the_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_handoff import seed
    from .test_engine_loop import Rig as LoopRig

    rig = LoopRig(tmp_path)
    seed(rig, tmp_path)
    client_id, value = _client(), _value()
    world = World(client_id, value)
    deps = Deps(gcp=lambda: world)

    def apply_pass(ctx, deps_, pass_name, **kwargs):
        world.log.append(f"apply {pass_name}")
        remember(ctx, outputs={applied_key(pass_name): inputs_digest(generate(ctx, pass_name, deps_.gcp()))})
        return {}

    monkeypatch.setattr(bootstrap, "apply_pass", apply_pass)
    contexts = FakeContexts()
    triggered = handoff.sign_in_triggered(deps, contexts=contexts)
    assert [step.id for step in triggered] == ["handoff.sign-in-client", "handoff.sign-in-bootstrap",
                                               "handoff.sign-in-infra"]
    engine = rig.engine([])
    engine.record = world
    ctx = _context(engine)
    ctx.engine.save(lambda s: (s.outputs.pop("desktop_client_id", None),
                               s.outputs.update({applied_key("bootstrap-2"): "old", applied_key("infra-2"): "old"})))
    assert not any(step.trigger(ctx) for step in triggered), "nothing runs before the console saved the client"

    world.save()
    for step in triggered:
        assert step.trigger(ctx), step.id
        step.apply(ctx, step.plan(ctx))
        assert step.check(ctx).status == "met", (step.id, step.check(ctx).detail)
    assert world.log == [f"read {edge.DESKTOP_SECRET}", "apply bootstrap-2", "apply infra-2"]
    outputs = ctx.outputs
    assert outputs["desktop_client_id"] == client_id
    assert outputs["iap_programmatic_client"] == client_id, "the engine record reports `applied` from it"
    assert contexts.stored, "the secret is in the new deployment's credential store"
    assert not any(step.trigger(ctx) for step in triggered)
