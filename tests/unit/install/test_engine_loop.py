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
