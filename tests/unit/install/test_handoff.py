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
from .test_terraform_runner import DESKTOP_CLIENT_ID, FakeCtx

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
        state.outputs.update({"alert_email": "alerts@example.com", "hostname": HOST, "github_org": "example-org",
                              "org_domain": "example.com", "installer_cidr": "203.0.113.7/32",
                              "desktop_client_id": DESKTOP_CLIENT_ID})

    store.update(change)


def rig_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, more=(), **at: float):
    rig = Rig(tmp_path)
    seed(rig, tmp_path)
    world = World(rig, **at)
    deps = Deps(gcp=lambda: world)

    def apply_pass(ctx, deps_, pass_name, **kwargs):
        world.log.append(f"apply {pass_name}")
        remember(ctx, outputs={applied_key(pass_name): inputs_digest(generate(ctx, pass_name, deps_.gcp()))})
        # Pass 4's apply reports the receiver serving (terraform/infra's github_hooks output).
        return {"github_hooks": {"serving": True}} if pass_name == "infra-4" else {}

    monkeypatch.setattr(bootstrap, "apply_pass", apply_pass)

    class Api:
        def activate_hook(self):
            world.log.append("POST hook")
            return {"active": True}

    said: list[str] = []
    wait = handoff.HandoffStep(deps, status=world.status, say=lambda line: (said.append(line),
                                                                             world.log.append("say")))
    wait.triggered = (Pass3Step(deps), Pass4Step(deps), HookStep(deps, api=lambda ctx: Api()))
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


def test_app_landing_triggers_pass3_then_pass4_then_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, world, engine, said = rig_for(tmp_path, monkeypatch)
    result = engine.run()
    assert result.exit_code == 0, result
    actions = [line for line in world.log if line not in ("status", "say")]
    # The webhook is pointed at the receiver only once pass 4 has made it (DAYONE-ENGINE-INPUTS).
    assert actions == ["apply infra-3", "apply infra-4", "POST hook"]
    assert [e["step"] for e in rig.events.of_kind("step_triggered")] == [
        "github.pass-3", "github.pass-4", "github.hook"]
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
    for switch in handoff.SWITCHES:
        ctx.state.steps[switch] = StepRecord(state="done", at=1.0, by="admin@example.com")
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


def test_ready_needs_the_webhook_pass_and_the_hook(tmp_path: Path) -> None:
    """Every verify step passed, but pass 4 and the hook have not run: not READY (#17)."""
    said: list[str] = []
    ready = handoff.ReadyStep(Deps(gcp=lambda: None), verify_ids=lambda: ["verify.a"], say=said.append)
    ctx = FakeCtx()
    for step in ("verify.a", "github.pass-3"):
        ctx.state.steps[step] = StepRecord(state="done", at=1.0, by="admin@example.com")
    assert ready.check(ctx).status == "unmet"
    with pytest.raises(RuntimeError, match="github.pass-4, github.hook"):
        ready.apply(ctx, ready.plan(ctx))
    assert ctx.state.ready_at is None and said == []

    ctx.state.steps["github.pass-4"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    with pytest.raises(RuntimeError, match="github.hook"):
        ready.apply(ctx, ready.plan(ctx))
    ctx.state.steps["github.hook"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    ready.apply(ctx, ready.plan(ctx))
    assert ready.check(ctx).status == "met"


def test_console_finished_before_swarm_install_ran_still_gets_pass4_and_hook(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The console finished while no CLI ran: this run turns the App's switches on before READY (#17)."""
    said: list[str] = []
    ready = handoff.ReadyStep(Deps(gcp=lambda: None), verify_ids=lambda: [], say=said.append)
    rig, world, engine, _ = rig_for(tmp_path, monkeypatch, more=(ready,), answers=0, app=0, self_check=0,
                                    finished=0)
    result = engine.run()
    assert result.exit_code == 0, result
    actions = [line for line in world.log if line not in ("status", "say")]
    assert actions == ["apply infra-3", "apply infra-4", "POST hook"]
    state = rig.bucket.state()
    steps = {k: v["state"] for k, v in state["steps"].items()}
    assert steps["github.pass-4"] == steps["github.hook"] == steps["handoff.console"] == "done"
    assert state["outputs"]["github_hook_active"] is True
    order = [e["step"] for e in rig.events.of_kind("step_done")]
    assert order.index("github.hook") < order.index("handoff.console") < order.index("handoff.ready")
    assert state["ready_at"] is not None


def test_handoff_is_not_met_while_the_switches_are_off() -> None:
    """A hand-off recorded done with the hooks still off (an older run) is repaired, not skipped (#17)."""
    ctx = FakeCtx(console={handoff.FINISHED: {"at": 1.0}})
    wait = handoff.HandoffStep(Deps(gcp=lambda: None), status=lambda url: 302)
    ctx.state.steps["handoff.console"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    ctx.state.steps["github.pass-3"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    assert wait.check(ctx).status != "met"
    assert "github.pass-4" in wait.check(ctx).detail and "github.hook" in wait.check(ctx).detail
    assert wait.verify(ctx).status != "met"
    for switch in handoff.SWITCHES:
        ctx.state.steps[switch] = StepRecord(state="done", at=1.0, by="admin@example.com")
    assert wait.check(ctx).status == "met" and wait.verify(ctx).status == "met"


def test_console_that_never_answers_pauses_then_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The console answering behind IAP is waited on inside the hand-off's apply;
    past that wait's bound the install pauses (exit 4) instead of crashing, and
    the next run resumes at the hand-off (DAYONE-ENGINE-WAIT-PAUSE)."""
    never = 10 * handoff.CONSOLE_ANSWERS.bound_s
    rig, world, engine, said = rig_for(tmp_path, monkeypatch, answers=never, app=never, self_check=never,
                                       finished=never)
    result = engine.run()
    assert result.exit_code == 4 and result.outcome == "paused", result
    assert result.step == "handoff.console"
    assert handoff.CONSOLE_ANSWERS.reason in result.message and "swarm install again" in result.message
    assert rig.steps_state()["handoff.console"] == "paused"
    assert not any(URL in line for line in said), "the console's URL is not printed while it does not answer"
    assert rig.bucket.state()["hand_off_at"] is None

    elapsed = rig.clock.now() - world.start
    world.at = {"answers": elapsed + 60, "app": elapsed + 600, "self_check": elapsed + 900,
                "finished": elapsed + 1500}
    resumed = rig.engine(engine.steps)
    resumed.record = world
    paused_said = len(said)
    result = resumed.run()
    assert result.exit_code == 0, result
    resumed_said = said[paused_said:]
    assert len(resumed_said) == 1 and re.findall(r"https://\S+", resumed_said[0]) == [URL]
    assert rig.steps_state()["handoff.console"] == "done"


# -- DAYONE-WAIT-LOOK-AT: a wait that gives up names a page or a command that exists ------------------


def _named_commands(text: str) -> list[list[str]]:
    """Every `swarm <verb> [--flag [value]]...` command a look_at line names, as
    argv without `swarm`; the prose after the verb ("again to resume") is not."""
    return [match.split() for match in re.findall(r"\bswarm ([a-z][\w-]*(?: --[\w-]+(?: [\w.-]+)?)*)", text)]


@pytest.mark.parametrize("spec", [handoff.CONSOLE, handoff.CONSOLE_ANSWERS], ids=["console", "console-answers"])
def test_handoff_look_at_names_no_raw_gcloud_and_only_commands_that_parse(spec) -> None:
    """The person past a wait's bound is sent to a page or a `swarm` command that
    exists: never raw gcloud, and never a flag the real parser refuses."""
    from swarm_install import cli, verbs

    assert "gcloud" not in spec.look_at and "--explain" not in spec.look_at, spec.look_at
    parser = cli.build_parser(verbs.discover(verbs.__name__))
    for argv in _named_commands(spec.look_at):
        parser.parse_args(argv)


def test_console_that_never_answers_opens_the_certificate_page(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the console-answers bound, the Google Cloud console's certificate
    page for this project is opened (and printed) before the run pauses."""
    never = 10 * handoff.CONSOLE_ANSWERS.bound_s
    rig, world, engine, said = rig_for(tmp_path, monkeypatch, answers=never, app=never, self_check=never,
                                       finished=never)
    result = engine.run()
    assert result.exit_code == 4 and result.outcome == "paused", result
    urls = [url for line in said for url in re.findall(r"https://\S+", line)]
    assert urls == [handoff.certificates_page("p-test")], said
    assert URL not in urls, "the console's own URL is not handed off while it does not answer"
