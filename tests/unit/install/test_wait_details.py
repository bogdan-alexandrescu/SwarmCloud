"""Every wait says what it waits on and why its probe is not met yet, and no
give-up text hands the person a placeholder or a command (audit 5b M1, M2;
DAYONE-FLOW-WAITS).

M1: the engine passed only the Observation's status into `wait_until`, so a
probe's `detail` (a certificate's FAILED_NOT_VISIBLE, "Google refused ...",
which verification run cloned with a personal token) and a probe's exception
text were dropped, and a definite failure polled to the bound. M2: the
`look_at` texts named `<project>`, `<hostname>` and `<task id>` for the person
to fill in, and commands to run (`gcloud services list`, `swarm doctor`).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_install import engine as engine_module
from swarm_install import waits
from swarm_install.events import Events, _human_line
from swarm_install.exitcodes import MET, UNMET, WAITING, ExitCode
from swarm_install.steps import Observation, Plan
from swarm_install.steps import alerts, cluster, edge, foundations, github_switches, handoff, identity, preflight
from swarm_install.steps import verify
from swarm_install.waits import WaitSpec, wait_until

from .test_engine_loop import Rig
from .test_state_store import FakeClock

SPEC = WaitSpec(reason="the managed certificate", expected="up to 60 min", expected_s=600, bound_s=1800,
                look_at="the certificate's page", interval_s=30)


class WaitingStep:
    """A wait step whose check answers `answers` in turn (the last one again)."""

    def __init__(self, answers: list[Any], *, wait: WaitSpec = SPEC) -> None:
        self.id = "edge.certificate"
        self.stage = 2
        self.kind = "wait"
        self.wait = wait
        self.satisfies = ("certificate.managed-ssl",)
        self.answers = answers
        self.polls = 0

    def check(self, ctx: Any) -> Observation:
        answer = self.answers[min(self.polls, len(self.answers) - 1)]
        self.polls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer

    def plan(self, ctx: Any) -> Plan:
        return Plan("wait for the certificate")

    def apply(self, ctx: Any, plan: Plan) -> None:
        return None

    def verify(self, ctx: Any) -> Observation:
        return Observation(MET)

    def explain(self) -> str:
        return "The certificate."


# -- M1: the countdown carries why the probe is not met -----------------------


def test_the_engines_countdown_carries_the_checks_detail_when_it_changes(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    first = Observation(WAITING, "swarm-ui-cert is PROVISIONING: FAILED_NOT_VISIBLE for swarm.example.com")
    second = Observation(WAITING, "swarm-ui-cert is PROVISIONING: the domain is ACTIVE")
    # The engine checks once before apply; the wait polls from the second answer.
    step = WaitingStep([first, first, first, second, second, Observation(MET)])
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    countdown = rig.events.of_kind("wait_countdown")
    assert len(countdown) == 4
    said = [event.get("detail") for event in countdown]
    assert said == [first.detail, None, second.detail, None], "the detail is sent whenever it changes"
    assert "FAILED_NOT_VISIBLE" in _human_line(countdown[0]), "the terminal line prints it"


def test_a_probe_that_raises_shows_its_exception_on_the_countdown(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    refused = PermissionError("Google refused compute.sslCertificates.get on swarm-ui-cert")
    step = WaitingStep([Observation(UNMET, "not yet"), refused, Observation(MET)])
    assert rig.engine([step]).run().exit_code == ExitCode.OK
    countdown = rig.events.of_kind("wait_countdown")
    assert countdown[0]["state"] == "unknown"
    assert "Google refused compute.sslCertificates.get" in countdown[0]["detail"]
    assert "PermissionError" in _human_line(countdown[0])


def test_a_wait_that_gives_up_names_its_last_detail(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    step = WaitingStep([Observation(WAITING, "dns_problem: swarm.example.com resolves to nothing")])
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.PAUSED, result.message
    assert "dns_problem: swarm.example.com resolves to nothing" in result.message
    gave_up = rig.events.of_kind("wait_gave_up")[0]
    assert gave_up["detail"] == "dns_problem: swarm.example.com resolves to nothing"
    assert "resolves to nothing" in _human_line(gave_up)


def test_a_definite_failure_ends_the_wait_at_once_with_its_detail(tmp_path: Path) -> None:
    """A check that says its failure is definite (`waits.FAILED`) ends the
    wait on that poll: exit 1 naming what failed, not an hour's polling."""
    failed = getattr(waits, "FAILED", "wait_failed")
    rig = Rig(tmp_path)
    why = "plugin-single (t-1) did not clone and push with a GitHub App installation token (lesson 43)"
    step = WaitingStep([Observation(WAITING, "0 of 7 runs have passed"),
                        Observation(UNMET, why, {failed: True})])
    started = rig.clock.now()
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.FAIL and result.outcome == "failed", result.message
    assert why in result.message
    assert rig.clock.now() - started < SPEC.bound_s, "it did not poll to the bound"
    assert step.polls == 2
    assert rig.events.of_kind("wait_failed")[0]["detail"] == why
    assert rig.steps_state() == {"edge.certificate": "failed"}


def test_wait_until_takes_a_bare_status_as_before() -> None:
    clock = FakeClock()
    events = Events(clock.now, json_lines=True, out=io.StringIO())
    answers = iter([UNMET, MET])
    wait_until("w", SPEC, lambda: next(answers), clock=clock, events=events)
    assert "detail" not in events.of_kind("wait_countdown")[0]


# -- verify: a run that cloned with a personal token stops the wait ----------


def test_verify_end_to_end_stops_waiting_on_a_personal_token(tmp_path: Path) -> None:
    from .test_verify_step import _all_finished

    step, ctx = _all_finished({"console-single": "pat"})
    clock = FakeClock()
    events = Events(clock.now, json_lines=True, out=io.StringIO())
    with pytest.raises(waits.CheckFailed) as raised:
        wait_until(step.id, verify.RUNS_WAIT, lambda: step.check(ctx), clock=clock, events=events)
    assert "console-single (t-console-single)" in raised.value.detail and "lesson 43" in raised.value.detail
    assert clock.sleeps == [], "the first poll that saw it ended the wait"


# -- M2: no give-up text holds a placeholder or a command ---------------------

PROJECT = "acme-swarm-prod"
HOSTNAME = "swarm.example.com"
ADDRESS = "203.0.113.7"
RUN_IDS = {"plugin-single": "t-plugin-1", "plugin-workflow": "w-plugin-2", "console_task": "t-console-3"}


def _ctx() -> Any:
    outputs = {"hostname": HOSTNAME, "global_ip": ADDRESS, "verify_runs": dict(RUN_IDS)}
    return SimpleNamespace(project=PROJECT, outputs=outputs, state=SimpleNamespace(hostname=HOSTNAME))


def _look(spec: WaitSpec, ctx: Any) -> str:
    resolve = getattr(spec, "look_at_for", None)
    return resolve(ctx) if resolve is not None else str(spec.look_at)


MODULES = (waits, engine_module, foundations, edge, verify, identity, preflight, cluster, alerts, handoff,
           github_switches)
SPECS = [(f"{module.__name__.rsplit('.', 1)[-1]}.{name}", value) for module in MODULES
         for name, value in vars(module).items() if isinstance(value, WaitSpec)]

#: What a give-up text never hands the person: a value to type in, or a command.
FORBIDDEN = (re.compile(r"<[a-z][a-z _-]*>"), re.compile(r"\bgcloud "), re.compile(r"\bkubectl\b"),
             re.compile(r"\bswarm (doctor|status)\b"), re.compile(r"--repair\b"), re.compile(r"--explain\b"))


def test_every_wait_was_found() -> None:
    names = {name for name, _ in SPECS}
    assert {"waits.IAM_PROPAGATION", "foundations.PROJECT_READY", "foundations.API_ENABLEMENT",
            "foundations.API_PROPAGATION", "edge.DNS_WAIT", "verify.TARGETS_WAIT", "verify.RUNS_WAIT"} <= names
    assert len(SPECS) >= 20, len(SPECS)


@pytest.mark.parametrize(("name", "spec"), SPECS, ids=[name for name, _ in SPECS])
def test_no_give_up_text_holds_a_placeholder_or_a_command(name: str, spec: WaitSpec) -> None:
    for ctx in (_ctx(), None):
        said = _look(spec, ctx)
        assert said.strip(), name
        for pattern in FORBIDDEN:
            assert not pattern.search(said), f"{name}: {pattern.pattern!r} in {said!r}"


@pytest.mark.parametrize(("spec", "names"), [
    (foundations.PROJECT_READY, (f"project={PROJECT}",)),
    (foundations.API_ENABLEMENT, (f"project={PROJECT}",)),
    (foundations.API_PROPAGATION, (f"project={PROJECT}",)),
    (verify.TARGETS_WAIT, (f"cloudscheduler?project={PROJECT}",)),
    (edge.DNS_WAIT, (f"name={HOSTNAME}&type=A", ADDRESS)),
    (verify.RUNS_WAIT, (*RUN_IDS.values(), f"https://{HOSTNAME}/admin/setup", "console-workflow (not started yet)")),
    (waits.IAM_PROPAGATION, (f"project={PROJECT}",)),
], ids=["project-ready", "api-enablement", "api-propagation", "scheduler-targets", "dns", "verify-runs", "iam"])
def test_a_give_up_text_names_the_real_project_hostname_and_runs(spec: WaitSpec, names: tuple[str, ...]) -> None:
    said = _look(spec, _ctx())
    for wanted in names:
        assert wanted in said, f"{wanted!r} not in {said!r}"


def test_the_engines_give_up_fills_the_text_from_its_context(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = WaitSpec(reason="the DNS record", expected="minutes", expected_s=60, bound_s=120,
                    look_at=lambda ctx: f"the records of {ctx.project}", interval_s=60)
    result = rig.engine([WaitingStep([Observation(WAITING, "not yet")], wait=spec)]).run()
    assert result.exit_code == ExitCode.PAUSED
    assert "Look at: the records of p-test." in result.message
    assert rig.events.of_kind("wait_gave_up")[0]["look_at"] == "the records of p-test"
