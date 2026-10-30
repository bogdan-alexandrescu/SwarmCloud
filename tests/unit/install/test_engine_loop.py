"""The engine's step loop (docs/design/day-one-install.md §2.2-§2.4; DAYONE-ENGINE-1).

On fakes, with no credentials: resume re-checks done steps and never re-runs
them; a done step whose check now fails is drift, reported and repaired; the
state is written after every step, so a crash between two steps resumes at the
second; a stage refuses while an entry of an earlier stage it depends on is
unmet (lesson 8); a wait re-checks its triggered steps on every poll and runs
the ones whose trigger has landed (plan §5.5, review finding 9).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

from swarm_install.engine import Engine
from swarm_install.events import Events
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.lease import Lease
from swarm_install.manifest import Entry, Manifest
from swarm_install.state import STATE_OBJECT, StateStore
from swarm_install.steps import Observation, Plan
from swarm_install.waits import WaitSpec

from .test_state_store import FakeBucket, FakeClock


class Crash(BaseException):
    """The laptop closing mid-step: nothing in the engine catches it."""


class FakeStep:
    def __init__(
        self,
        id: str,
        stage: int,
        satisfies: tuple[str, ...] = (),
        *,
        met: bool = False,
        log: list[str],
        kind: str = "engine",
        wait: WaitSpec | None = None,
        triggered: tuple = (),
        trigger: Callable[[object], bool] | None = None,
        is_met: Callable[[], bool] | None = None,
        on_apply: Callable[[], None] | None = None,
    ) -> None:
        self.id = id
        self.stage = stage
        self.satisfies = satisfies or (f"other.{id}",)
        self.met = met
        self.log = log
        self.kind = kind
        self.wait = wait
        self.triggered = triggered
        self._trigger = trigger
        self._is_met = is_met
        self._on_apply = on_apply
        if trigger is not None:
            self.trigger = lambda ctx: trigger(ctx)

    def check(self, ctx) -> Observation:
        self.log.append(f"check {self.id}")
        met = self._is_met() if self._is_met is not None else self.met
        return Observation(MET if met else UNMET)

    def plan(self, ctx) -> Plan:
        return Plan(f"make {self.id}")

    def apply(self, ctx, plan: Plan) -> None:
        self.log.append(f"apply {self.id}")
        if self._on_apply is not None:
            self._on_apply()
        if self.kind == "engine":
            self.met = True

    def verify(self, ctx) -> Observation:
        self.log.append(f"verify {self.id}")
        return Observation(MET)

    def explain(self) -> str:
        return f"{self.id} explained."


class Rig:
    """A bucket, a clock and a way to build an engine over them, again and again."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.bucket = FakeBucket()
        self.clock = FakeClock()
        self.events: Events | None = None

    def engine(self, steps, *, manifest: Manifest | None = None, dry_run: bool = False) -> Engine:
        self.events = Events(self.clock.now, json_lines=True, out=io.StringIO())
        return Engine(
            steps,
            store=StateStore(
                project="p-test", environment="dev", local=self.tmp_path / "s.json", bucket=self.bucket
            ),
            lease=Lease(holder="admin@example.com", store=self.bucket, clock=self.clock.now),
            events=self.events,
            clock=self.clock,
            holder="admin@example.com",
            manifest=manifest,
            dry_run=dry_run,
        )

    def steps_state(self) -> dict[str, str]:
        return {k: v["state"] for k, v in self.bucket.state()["steps"].items()}


def test_resume_skips_done_steps_after_recheck(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []
    assert rig.engine([FakeStep("a", 1, log=log), FakeStep("b", 2, log=log)]).run().exit_code == 0
    assert rig.steps_state() == {"a": "done", "b": "done"}

    log.clear()
    resumed = rig.engine([FakeStep("a", 1, met=True, log=log), FakeStep("b", 2, met=True, log=log)])
    result = resumed.run()
    assert result.exit_code == ExitCode.OK
    assert log == ["check a", "check b"], "resume re-runs the checks and nothing else"
    assert [e["step"] for e in rig.events.of_kind("step_skip")] == ["a", "b"]


def test_done_step_whose_check_fails_is_drift(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []
    rig.engine([FakeStep("a", 1, log=log)]).run()

    log.clear()
    drifted = FakeStep("a", 1, met=False, log=log)
    result = rig.engine([drifted]).run()
    drift = rig.events.of_kind("step_drift")
    assert len(drift) == 1
    assert drift[0]["step"] == "a" and drift[0]["repaired_by"] == "a"
    assert drift[0]["state"] == UNMET
    assert log == ["check a", "apply a", "check a", "verify a"], "drift is repaired by its step"
    assert result.exit_code == 0
    assert rig.steps_state() == {"a": "done"}


def test_state_written_after_every_step(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []

    def crash() -> None:
        raise Crash()

    first = FakeStep("bootstrap.pass1", 3, log=log)
    second = FakeStep("bootstrap.pass2", 3, log=log, on_apply=crash)
    try:
        rig.engine([first, second]).run()
    except Crash:
        pass
    else:
        raise AssertionError("the crash must not be swallowed")
    assert rig.steps_state() == {"bootstrap.pass1": "done"}, (
        "the first step of the stage is recorded before the second runs"
    )

    log.clear()
    again = [FakeStep("bootstrap.pass1", 3, met=True, log=log), FakeStep("bootstrap.pass2", 3, log=log)]
    assert rig.engine(again).run().exit_code == 0
    assert "apply bootstrap.pass1" not in log
    assert "apply bootstrap.pass2" in log, "the resume starts at the second step"
    assert rig.steps_state() == {"bootstrap.pass1": "done", "bootstrap.pass2": "done"}


def _entry(id: str, stage: int, depends_on: list[str] = []) -> Entry:
    return Entry(id=id, kind=id.split(".")[0], scope="core", stage=stage, provider="engine",
                 depends_on=depends_on)


def test_stage_refuses_while_earlier_entry_unmet(tmp_path: Path) -> None:
    """Lesson 8: infra applied before bootstrap fails on `swarmSecretLister does
    not exist`. The engine refuses the later stage before it applies anything."""
    manifest = Manifest(
        entries=[
            _entry("role.custom.swarm-secret-lister", 3),
            _entry("switch.grant_broker_secret_lister", 7, ["role.custom.swarm-secret-lister"]),
        ],
        ignored={}, env={}, sources={},
    )
    rig = Rig(tmp_path)
    log: list[str] = []
    bootstrap = FakeStep("bootstrap.pass1", 3, ("role.custom.swarm-secret-lister",), log=log)
    infra = FakeStep("infra.pass2", 7, ("switch.grant_broker_secret_lister",), log=log)

    result = rig.engine([bootstrap, infra], manifest=manifest).run(stages=[7])

    assert result.exit_code == ExitCode.FAIL and result.outcome == "blocked"
    assert result.step == "infra.pass2"
    assert not [line for line in log if line.startswith("apply")], log
    refused = rig.events.of_kind("stage_refused")
    assert refused and refused[0]["needs"] == ["role.custom.swarm-secret-lister"]
    assert "bootstrap.pass1" in refused[0]["message"], "it names the step that satisfies the entry"

    log.clear()
    bootstrap.met = True
    assert rig.engine([bootstrap, infra], manifest=manifest).run(stages=[7]).exit_code == 0
    assert "apply infra.pass2" in log and "apply bootstrap.pass1" not in log


def _owned_entry(id: str, stage: int, step: str, depends_on: list[str] = []) -> Entry:
    return Entry(id=id, kind=id.split(".")[0], scope="core", stage=stage, provider="engine",
                 depends_on=depends_on, step=step)


def _pass(id: str, stage: int, *, log: list[str], met: bool = False) -> FakeStep:
    """A terraform pass: it lists nothing in `satisfies`; the manifest's
    `entry.step` names it as the owner of what it makes."""
    step = FakeStep(id, stage, log=log, met=met)
    step.satisfies = ()
    return step


def test_dependency_met_once_the_step_its_entry_names_is_done(tmp_path: Path) -> None:
    """Audit finding 1: `cluster.autopilot` is made by infra.pass-1, which
    declares `satisfies=()`. On a fresh install stage 10 must not refuse."""
    manifest = Manifest(
        entries=[
            _owned_entry("cluster.autopilot", 5, "infra.pass-1"),
            _entry("cluster-policy.cache-storage-class", 10, ["cluster.autopilot"]),
        ],
        ignored={}, env={}, sources={},
    )
    rig = Rig(tmp_path)
    log: list[str] = []
    infra = _pass("infra.pass-1", 5, log=log)
    policies = FakeStep("cluster.policies", 10, ("cluster-policy.cache-storage-class",), log=log)

    result = rig.engine([infra, policies], manifest=manifest).run()

    assert result.exit_code == 0, result
    assert [line for line in log if line.startswith("apply")] == ["apply infra.pass-1", "apply cluster.policies"]
    assert not rig.events.of_kind("stage_refused")

    # A scoped re-run counts it met from the earlier stage's check.
    log.clear()
    infra.met = True
    policies.met = False
    assert rig.engine([infra, policies], manifest=manifest).run(stages=[10]).exit_code == 0
    assert "apply cluster.policies" in log and "apply infra.pass-1" not in log


def test_dependency_refused_while_the_step_its_entry_names_is_not_done(tmp_path: Path) -> None:
    manifest = Manifest(
        entries=[
            _owned_entry("cluster.autopilot", 5, "infra.pass-1"),
            _entry("cluster.swarm-system-namespace", 10, ["cluster.autopilot"]),
        ],
        ignored={}, env={}, sources={},
    )
    rig = Rig(tmp_path)
    log: list[str] = []
    infra = _pass("infra.pass-1", 5, log=log)
    namespace = FakeStep("cluster.swarm-system", 10, ("cluster.swarm-system-namespace",), log=log)

    result = rig.engine([infra, namespace], manifest=manifest).run(stages=[10])

    assert result.outcome == "blocked" and result.step == "cluster.swarm-system"
    assert not [line for line in log if line.startswith("apply")], log
    refused = rig.events.of_kind("stage_refused")
    assert refused[0]["needs"] == ["cluster.autopilot"]
    assert "infra.pass-1, stage 5" in refused[0]["message"], "it names the step that makes the entry"


def test_fresh_install_reaches_every_stage_with_the_real_manifest(tmp_path: Path) -> None:
    """Audit finding 1, end to end: every real step (triggered ones included),
    as a fake that applies, over the real merged manifest on a fresh state.
    Before the fix, stage 8 refused: `identity.group-read needs sa.swarm-api
    (no step in this release satisfies it)`."""
    from swarm_install.manifest import load
    from swarm_install.steps import discover, triggered_of

    real, pending = [], list(discover())
    while pending:
        step = pending.pop(0)
        real.append(step)
        pending.extend(triggered_of(step))
    log: list[str] = []
    fakes = []
    for step in real:
        fake = FakeStep(step.id, step.stage, log=log)
        fake.satisfies = tuple(step.satisfies)
        fakes.append(fake)
    rig = Rig(tmp_path)

    result = rig.engine(fakes, manifest=load()).run()

    assert not rig.events.of_kind("stage_refused"), rig.events.of_kind("stage_refused")[0]["message"]
    assert result.exit_code == 0, result
    assert set(rig.steps_state().values()) == {"done"} and len(rig.steps_state()) == len(real)


def test_text_mode_prints_a_plans_actions_indented() -> None:
    """Audit finding 15: a human step's instructions live in its plan's
    actions; the terminal must show every one, under the plan line."""
    out = io.StringIO()
    events = Events(FakeClock().now, out=out)
    events.emit(
        "step_plan", step="edge.brand", stage=2, plan="create the OAuth brand",
        actions=["App name: SwarmCloud\nSupport email: you", "then open .../auth/audience and choose Internal"],
    )
    lines = out.getvalue().splitlines()
    assert lines[0] == "  plan edge.brand -- create the OAuth brand"
    assert lines[1:] == [
        "      App name: SwarmCloud",
        "      Support email: you",
        "      then open .../auth/audience and choose Internal",
    ]


def test_wait_runs_steps_whose_trigger_lands(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    start = rig.clock.now()
    log: list[str] = []
    world = {"app_landed_at": start + 30, "self_check_at": start + 60, "finished_at": start + 90}

    def landed(key: str) -> bool:
        return rig.clock.now() >= world[key]

    pass3 = FakeStep("handoff.infra-pass3", 9, log=log, trigger=lambda ctx: landed("app_landed_at"))
    pass4 = FakeStep(
        "handoff.infra-pass4", 9, log=log,
        trigger=lambda ctx: ctx.state.done("handoff.infra-pass3") and landed("self_check_at"),
    )
    handoff = FakeStep(
        "handoff.wait", 9, log=log, kind="wait",
        wait=WaitSpec(reason="the console's people steps", expected="about an hour",
                      expected_s=3600, bound_s=4 * 3600, look_at="Admin > Setup"),
        triggered=(pass3, pass4),
        is_met=lambda: landed("finished_at"),
    )

    result = rig.engine([handoff]).run()

    assert result.exit_code == 0, result
    applies = [line for line in log if line.startswith("apply")]
    assert applies == ["apply handoff.wait", "apply handoff.infra-pass3", "apply handoff.infra-pass4"]
    assert [e["step"] for e in rig.events.of_kind("step_triggered")] == [
        "handoff.infra-pass3", "handoff.infra-pass4",
    ]
    assert rig.steps_state() == {
        "handoff.infra-pass3": "done", "handoff.infra-pass4": "done", "handoff.wait": "done",
    }
    assert rig.clock.now() - start == 90


def test_dry_run_plans_and_writes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []
    result = rig.engine([FakeStep("a", 1, log=log), FakeStep("b", 2, met=True, log=log)],
                        dry_run=True).run()
    assert result.exit_code == 0
    assert log == ["check a", "check b"]
    assert [e["step"] for e in rig.events.of_kind("step_plan")] == ["a"]
    assert rig.bucket.writes == [] and STATE_OBJECT not in rig.bucket.objects


def test_unknown_check_stops_without_applying(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []

    class Forbidden(FakeStep):
        def check(self, ctx):
            raise PermissionError("403 on serviceusage.services.list")

    result = rig.engine([Forbidden("apis", 1, log=log)]).run()
    assert result.exit_code == ExitCode.TROUBLE != 0
    assert log == []


def test_same_stage_steps_run_in_dependency_order(tmp_path: Path) -> None:
    manifest = Manifest(
        entries=[_entry("bucket.state", 1), _entry("other.install-state", 1, ["bucket.state"])],
        ignored={}, env={}, sources={},
    )
    rig = Rig(tmp_path)
    log: list[str] = []
    upload = FakeStep("state.upload", 1, ("other.install-state",), log=log)
    bucket = FakeStep("foundations.bucket", 1, ("bucket.state",), log=log)
    assert rig.engine([upload, bucket], manifest=manifest).run().exit_code == 0
    applies = [line for line in log if line.startswith("apply")]
    assert applies == ["apply foundations.bucket", "apply state.upload"]


def test_human_step_polls_its_check_and_pauses_with_exit_4(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []
    brand = FakeStep(
        "edge.oauth-brand", 2, log=log, kind="human",
        wait=WaitSpec(reason="the OAuth brand", expected="5 min", expected_s=300,
                      bound_s=600, look_at="APIs & Services > OAuth consent screen"),
    )
    result = rig.engine([brand]).run()
    assert result.exit_code == ExitCode.PAUSED == 4 and result.outcome == "paused"
    assert log.count("apply edge.oauth-brand") == 1, "the instructions are printed once"
    assert log.count("check edge.oauth-brand") > 2, "then the step's own check is polled"
    assert rig.steps_state() == {"edge.oauth-brand": "paused"}
    assert rig.events.of_kind("wait_countdown"), "the person sees a countdown"


def test_json_events_carry_no_value(tmp_path: Path) -> None:
    import json
    import secrets

    import pytest
    from pydantic import SecretStr

    from swarm_install.events import EventValueError

    out = io.StringIO()
    events = Events(lambda: 1.0, json_lines=True, out=out)
    with pytest.raises(EventValueError):
        events.emit("step_done", step="secrets.hmac", value=SecretStr(secrets.token_hex(16)))
    with pytest.raises(EventValueError):
        events.emit("step_done", step="secrets.hmac", value=secrets.token_bytes(16))
    events.emit("step_done", step="secrets.hmac", state="met")
    line = json.loads(out.getvalue().splitlines()[-1])
    assert line == {"v": 1, "event": "step_done", "at": 1.0, "step": "secrets.hmac", "state": "met"}


def test_dry_run_runs_read_only_stage_0_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log: list[str] = []
    preflight = FakeStep("preflight.region", 0, log=log)
    preflight.read_only = True
    writer = FakeStep("preflight.writes", 0, log=log)
    later = FakeStep("foundations.apis", 1, log=log)
    later.read_only = True  # ignored outside stage 0
    assert rig.engine([preflight, writer, later], dry_run=True).run().exit_code == 0
    assert [line for line in log if line.startswith("apply")] == ["apply preflight.region"]
    assert rig.bucket.writes == []


# -- pages: opened once, then polled (DAYONE-OPEN-PAGES) ---------------------------


def test_open_page_honours_no_browser_and_no_display_and_never_raises() -> None:
    """open_page prints why and the URL, opens the browser only with a display
    and without SWARM_NO_BROWSER=1, and never raises."""
    from swarm_install.steps import has_display, open_page

    url = "https://console.cloud.google.com/auth/branding?project=p-test"
    said: list[str] = []
    opened: list[str] = []
    linux = {"DISPLAY": ":0"}

    open_page(url, why="create the brand", say=said.append, env=linux, platform="linux", browser=opened.append)
    assert opened == [url]
    assert len(said) == 1 and "create the brand" in said[0] and url in said[0]

    opened.clear()
    open_page(url, why="w", say=said.append, env={**linux, "SWARM_NO_BROWSER": "1"}, platform="linux",
              browser=opened.append)
    assert opened == [], "SWARM_NO_BROWSER=1 prints only"
    open_page(url, why="w", say=said.append, env={}, platform="linux", browser=opened.append)
    assert opened == [], "no display on Linux: a text browser would take over the terminal"
    open_page(url, why="w", say=said.append, env={"SSH_CONNECTION": "x"}, platform="darwin", browser=opened.append)
    assert opened == [], "a Mac reached over SSH has nobody in front of its screen"
    assert has_display({}, "darwin") and has_display({"WAYLAND_DISPLAY": "wayland-0"}, "linux")
    assert len(said) == 4 and all(url in line for line in said), "the URL is printed every time"

    def broken(_: object) -> None:
        raise OSError("no browser")

    open_page(url, why="w", say=broken, env=linux, platform="linux", browser=broken)  # never raises


class PagedStep(FakeStep):
    def __init__(self, *args, link: str | None, actions: tuple[str, ...] = (), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._actions = actions
        if link is not None:
            self.deep_link = lambda ctx: link

    def plan(self, ctx) -> Plan:
        return Plan(f"make {self.id}", self._actions)


def test_human_step_opens_its_page_once_then_polls(tmp_path: Path, monkeypatch) -> None:
    """A human step's page is opened once, after apply and before the first
    poll; the polls that follow never open it again."""
    from swarm_install import engine as engine_module

    rig = Rig(tmp_path)
    log: list[str] = []
    monkeypatch.setattr(engine_module, "open_page", lambda url, *, why: log.append(f"open {url}"))
    appears = rig.clock.now() + 45
    url = "https://console.cloud.google.com/auth/branding?project=p-test"
    brand = PagedStep(
        "edge.oauth-brand", 2, log=log, kind="human", link=url,
        is_met=lambda: rig.clock.now() >= appears,
        wait=WaitSpec(reason="the OAuth brand", expected="5 min", expected_s=300, bound_s=600,
                      look_at="Branding", interval_s=10),
    )
    result = rig.engine([brand]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert log.count(f"open {url}") == 1, "opened once, not on every poll"
    opened = log.index(f"open {url}")
    assert log[opened - 1] == "apply edge.oauth-brand"
    assert log[opened + 1:].count("check edge.oauth-brand") > 3, "then the check is polled"


def test_human_step_without_deep_link_opens_the_first_url_of_its_plan(tmp_path: Path, monkeypatch) -> None:
    """A human step that names no deep_link opens the first https URL its plan
    names; engine and wait steps are never opened by the engine."""
    from swarm_install import engine as engine_module

    rig = Rig(tmp_path)
    log: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(engine_module, "open_page", lambda url, *, why: opened.append(url))
    url = "https://admin.google.com/ac/roles"
    spec = WaitSpec(reason="r", expected="1 min", expected_s=60, bound_s=120, look_at="l", interval_s=10)
    steps = [
        PagedStep("h", 1, log=log, kind="human", link=None, wait=spec, is_met=lambda: "apply h" in log,
                  actions=("name a group", f"1 admin role: {url}: swarm-api", "https://example.com/second")),
        PagedStep("e", 2, log=log, link="https://example.com/engine"),
        PagedStep("w", 3, log=log, kind="wait", link="https://example.com/wait", wait=spec,
                  is_met=lambda: "apply w" in log),
    ]
    result = rig.engine(steps).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert opened == [url]
