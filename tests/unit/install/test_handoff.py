"""The hand-off to the console and READY (DAYONE-ENGINE-3; plan §5.5; D26;
B05, B09b; R8), driven through the real engine loop on fakes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_install.lease import HEARTBEAT_SECONDS
from swarm_install.state import StateStore, StepRecord
from swarm_install.steps import bootstrap, handoff
from swarm_install.steps.bootstrap import Deps, applied_key, generate, inputs_digest
from swarm_install.steps.github_switches import HookStep, Pass3Step, Pass4Step
from swarm_install.steps.preflight import remember
from swarm_install import tfvars

from .test_engine_loop import FakeStep, Rig
from .test_terraform_runner import FakeCtx

HOST = "swarm.example.com"
URL = f"https://{HOST}/admin/setup"


class World:
    """The console and the project over time: what lands, and when."""

    def __init__(self, rig: Rig, **at: float) -> None:
        self.rig = rig
        self.start = rig.clock.now()
        self.at = {"answers": 120, "app": 600, "self_check": 900, "finished": 1500}
        self.at.update(at)
        self.log: list[str] = []
        self.status_calls = 0
        self.writes = 0

    def landed(self, what: str) -> bool:
        return self.rig.clock.now() - self.start >= self.at[what]

    # -- the console's documents, through the install record --
    def read_console(self, name: str):
        if name == handoff.FINISHED:
            return {"at": 1.0} if self.landed("finished") else None
        if name == "github_app" and self.landed("app"):
            return {"id": "42", "slug": "swarmcloud", "secrets_written_at": 1.0,
                    "self_check": {"ok": self.landed("self_check")}}
        return None

    def write(self, *args, **kwargs) -> None:
        self.writes += 1

    # -- the project: Secret Manager metadata, the console's URL --
    def secret(self, project, name):
        return {"name": name} if self._has(name) else None

    def enabled_version(self, project, name):
        return 1 if self._has(name) else None

    def _has(self, name: str) -> bool:
        return name in tfvars.APP_SECRETS and self.landed("app")

    def status(self, url: str) -> int:
        self.status_calls += 1
        self.log.append("status")
        if not self.landed("answers"):
            raise OSError("connection refused")
        return 302


def seed(rig: Rig, tmp_path: Path) -> None:
    store = StateStore(project="p-test", environment="dev", local=tmp_path / "s.json", bucket=rig.bucket)

    def change(state) -> None:
        state.hostname, state.region, state.release = HOST, "europe-west1", "2026.10.1"
        state.outputs.update({"alert_email": "alerts@example.com", "hostname": HOST})

    store.update(change)


def rig_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, more=(), **at: float):
    rig = Rig(tmp_path)
    seed(rig, tmp_path)
    world = World(rig, **at)
    deps = Deps(gcp=lambda: world)

    def apply_pass(ctx, deps_, pass_name, **kwargs):
        world.log.append(f"apply {pass_name}")
        remember(ctx, outputs={applied_key(pass_name): inputs_digest(generate(ctx, pass_name, deps_.gcp()))})
        return {}

    monkeypatch.setattr(bootstrap, "apply_pass", apply_pass)

    class Api:
        def activate_hook(self):
            world.log.append("POST hook")
            return {"active": True}

    said: list[str] = []
    wait = handoff.HandoffStep(deps, status=world.status, say=lambda line: (said.append(line),
                                                                             world.log.append("say")))
    wait.triggered = (Pass3Step(deps), HookStep(deps, api=lambda ctx: Api()), Pass4Step(deps))
    engine = rig.engine([wait, *more])
    engine.record = world
    return rig, world, engine, said


def test_one_url_printed_once_console_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, world, engine, said = rig_for(tmp_path, monkeypatch)
    result = engine.run()
    assert result.exit_code == 0, result
    assert len(said) == 1, "the URL is printed once"
    assert re.findall(r"https://\S+", said[0]) == [URL]
    first_say = world.log.index("say")
    assert world.log[:first_say] == ["status"] * world.status_calls, "printed only once the console answers"
    assert world.status_calls > 1, "it waited for the console to answer"
    state = rig.bucket.state()
    assert state["hand_off_at"] == pytest.approx(world.start + 120, abs=handoff.CONSOLE_ANSWERS.interval_s)


def test_cli_lease_heartbeat_while_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The console answers late (a slow certificate), then the people take their time.
    rig, world, engine, said = rig_for(tmp_path, monkeypatch, answers=10 * HEARTBEAT_SECONDS,
                                       app=11 * HEARTBEAT_SECONDS, self_check=12 * HEARTBEAT_SECONDS,
                                       finished=20 * HEARTBEAT_SECONDS)
    result = engine.run()
    assert result.exit_code == 0, result
    beats = [e["at"] for e in rig.events.of_kind("lease")]
    assert beats[-1] - world.start >= 19 * HEARTBEAT_SECONDS
    gap = max(b - a for a, b in zip(beats, beats[1:]))
    assert gap <= HEARTBEAT_SECONDS + handoff.CONSOLE_ANSWERS.interval_s, (
        "the lease is renewed on every poll of both waits, never left to expire")
    assert world.writes > 0, "each heartbeat is mirrored to the install record"


def test_app_landing_triggers_pass3_then_hook_then_pass4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, world, engine, said = rig_for(tmp_path, monkeypatch)
    result = engine.run()
    assert result.exit_code == 0, result
    actions = [line for line in world.log if line not in ("status", "say")]
    assert actions == ["apply infra-3", "POST hook", "apply infra-4"]
    assert [e["step"] for e in rig.events.of_kind("step_triggered")] == [
        "github.pass-3", "github.hook", "github.pass-4"]
    steps = {k: v["state"] for k, v in rig.bucket.state()["steps"].items()}
    assert steps == {"github.pass-3": "done", "github.hook": "done", "github.pass-4": "done",
                     "handoff.console": "done"}


def test_hook_waits_for_the_self_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, world, engine, said = rig_for(tmp_path, monkeypatch, self_check=10**6, finished=10**6)
    result = engine.run()
    assert result.exit_code == 4, "past the bound the install pauses; swarm install resumes it"
    assert [line for line in world.log if line not in ("status", "say")] == ["apply infra-3"]


def test_console_finish_triggers_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    verify = FakeStep("verify.smoke", 14, log=log)
    rig, world, engine, said = rig_for(tmp_path, monkeypatch, more=(verify,))
    applied_at: list[float] = []
    verify._on_apply = lambda: applied_at.append(rig.clock.now())
    assert engine.run().exit_code == 0
    assert applied_at and applied_at[0] - world.start >= 1500, "verify runs once the console says finished"

    log.clear()
    rig, world, engine, said = rig_for(tmp_path / "unfinished", monkeypatch, more=(verify,), finished=10**6)
    verify.met = False
    result = engine.run()
    assert result.exit_code == 4
    assert "apply verify.smoke" not in log, "verify never runs while the console is unfinished (B09b)"


def test_ready_only_after_verify_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []
    ready = handoff.ReadyStep(Deps(gcp=lambda: None), verify_ids=lambda: ["verify.a", "verify.b"],
                              say=said.append)
    ctx = FakeCtx()
    ctx.state.steps["verify.a"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    assert ready.check(ctx).status == "unmet"
    with pytest.raises(RuntimeError, match="verify.b"):
        ready.apply(ctx, ready.plan(ctx))
    assert ctx.state.ready_at is None and said == []

    ctx.state.steps["verify.b"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    ready.apply(ctx, ready.plan(ctx))
    assert ctx.state.ready_at == ctx.clock.now()
    assert said == [f"\nREADY: SwarmCloud answers at https://{HOST}\n"]
    assert ready.check(ctx).status == "met"

    # Through the engine: a verify step that fails stops the run before READY.
    log: list[str] = []
    failing = FakeStep("verify.a", 14, log=log, is_met=lambda: False)
    engine_ready = handoff.ReadyStep(Deps(gcp=lambda: None), verify_ids=lambda: ["verify.a"], say=said.append)
    rig, world, engine, _ = rig_for(tmp_path / "engine", monkeypatch, more=(failing, engine_ready))
    result = engine.run()
    assert result.exit_code != 0 and result.step == "verify.a"
    assert rig.bucket.state()["ready_at"] is None
