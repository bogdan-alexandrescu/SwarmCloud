"""Stage 13, alerts: the policies only after their metrics' descriptors exist
(DAYONE-ENGINE-3; design §4; plan R7; #1817; lesson 23)."""

from __future__ import annotations

import pytest

from swarm_install import tfvars
from swarm_install.steps import alerts
from swarm_install.steps.bootstrap import Deps

from .test_state_store import FakeClock
from .test_terraform_runner import FakeCtx


class FakeMonitoring:
    def __init__(self, clock: FakeClock, appears_at: float) -> None:
        self.clock = clock
        self.appears_at = appears_at
        self.log: list[str] = []
        self.policies: list[dict] = []

    def metric_descriptors(self, project, prefix):
        assert prefix == alerts.LOG_METRIC_PREFIX
        self.log.append("descriptors")
        if self.clock.now() < self.appears_at:
            return []
        return [f"{prefix}_task_failures", f"{prefix}_quota_parks"]

    def alert_policies(self, project):
        return list(self.policies)

    def notification_channels(self, project):
        return [{"labels": {"email_address": "alerts@example.com"}}]


def test_alert_policies_after_metric_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    ctx = FakeCtx(clock=clock)
    gcp = FakeMonitoring(clock, appears_at=clock.now() + 90)

    def infra_now() -> dict:
        return tfvars.infra_inputs(ctx.state, pass_name="infra-2", installer="admin@example.com")

    assert infra_now()["create_alerts"] is False, "infra passes run with create_alerts off until the descriptors"

    def apply_pass(ctx_, deps, pass_name, **kwargs):
        gcp.log.append(f"apply {pass_name} create_alerts={infra_now()['create_alerts']}")
        gcp.policies = [{"displayName": "swarm task failures"}]
        tfvars.store(ctx.bucket, "infra", infra_now())
        return {}

    monkeypatch.setattr(alerts, "apply_pass", apply_pass)
    step = alerts.AlertsStep(Deps(gcp=lambda: gcp), expected_metrics=2)
    assert "the alert policies wait for their metrics' descriptors" in step.check(ctx).detail

    step.apply(ctx, step.plan(ctx))

    first_apply = gcp.log.index("apply infra-2 create_alerts=True")
    assert gcp.log[:first_apply] and set(gcp.log[:first_apply]) == {"descriptors"}
    assert len(gcp.log[:first_apply]) > 1, "the step waited for the descriptors"
    assert "apply infra-2 create_alerts=False" not in gcp.log
    assert ctx.events.of_kind("wait_countdown"), "the wait shows a countdown"
    assert ctx.outputs["metric_descriptors_ready"] is True
    assert step.check(ctx).status == "met"


def test_alerts_unmet_without_a_channel_or_the_flag() -> None:
    clock = FakeClock()
    ctx = FakeCtx(clock=clock)
    gcp = FakeMonitoring(clock, appears_at=0)
    gcp.notification_channels = lambda project: []  # type: ignore[method-assign]
    detail = alerts.AlertsStep(Deps(gcp=lambda: gcp)).check(ctx).detail
    assert "no notification channel sends to alerts@example.com" in detail
    assert "require_alert_channels is not true" in detail


@pytest.mark.parametrize(("passes", "expected"), [
    (("infra-2", "infra-3", "infra-4"), "infra-4"),
    (("infra-2", "infra-3", "infra-4", "infra-close"), "infra-close"),
])
def test_alerts_redo_applies_the_latest_pass_never_infra_2(monkeypatch: pytest.MonkeyPatch,
                                                            passes: tuple[str, ...], expected: str) -> None:
    """A redo after READY (a deleted policy, a changed alert email, --repair
    monitoring.alert-policies) never re-applies infra-2, whose inputs switch the
    GitHub App, the forge refresh and the hooks off and put the address back (N12)."""
    from swarm_install.steps.bootstrap import applied_key

    clock = FakeClock()
    ctx = FakeCtx(clock=clock)
    ctx.state.outputs["metric_descriptors_ready"] = True
    for pass_name in passes:
        ctx.state.outputs[applied_key(pass_name)] = "digest"
    applied: list[str] = []
    monkeypatch.setattr(alerts, "apply_pass", lambda ctx_, deps, pass_name, **kw: applied.append(pass_name))
    step = alerts.AlertsStep(Deps(gcp=lambda: FakeMonitoring(clock, appears_at=0)))
    plan = step.plan(ctx)
    step.apply(ctx, plan)
    assert applied == [expected]
    assert expected in plan.summary and "infra pass 2" not in plan.summary


def test_descriptors_look_at_names_no_raw_gcloud() -> None:
    """A wait past its bound sends the person to a page, never raw gcloud (DAYONE-WAIT-LOOK-AT)."""
    assert "gcloud" not in alerts.DESCRIPTORS.look_at and "--explain" not in alerts.DESCRIPTORS.look_at


def test_descriptors_that_never_appear_open_the_log_metrics_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the descriptors' bound the log-based metrics page of this project is
    opened before the wait's WaitGaveUp pauses the run; nothing is applied."""
    from swarm_install.waits import WaitGaveUp

    clock = FakeClock()
    ctx = FakeCtx(clock=clock)
    gcp = FakeMonitoring(clock, appears_at=clock.now() + 10 * alerts.DESCRIPTORS.bound_s)
    opened: list[str] = []
    monkeypatch.setattr(alerts, "open_page", lambda url, *, why, **kw: opened.append(url))
    monkeypatch.setattr(alerts, "apply_pass", lambda *a, **k: pytest.fail("nothing is applied"))
    step = alerts.AlertsStep(Deps(gcp=lambda: gcp), expected_metrics=2)
    with pytest.raises(WaitGaveUp):
        step.apply(ctx, step.plan(ctx))
    assert opened == [f"https://console.cloud.google.com/logs/metrics?project={ctx.project}"]
    assert "metric_descriptors_ready" not in ctx.outputs


class SomeDescriptors:
    """Cloud Monitoring with `count` of the platform's log metric descriptors."""

    def __init__(self, count: int) -> None:
        self.count = count

    def metric_descriptors(self, project, prefix):
        return [f"{prefix}/{name}" for name in alerts.LOG_METRICS[:self.count]]


def test_the_registered_step_waits_for_every_log_metric_descriptor() -> None:
    """One descriptor is not every descriptor: the step the engine runs turns
    create_alerts on only once each log metric's descriptor exists, else a
    policy on a missing one fails with "Cannot find metric" (lesson 23)."""
    ctx = FakeCtx(clock=FakeClock())
    every = len(alerts.LOG_METRICS)
    for build in (lambda deps: alerts.build_steps(deps)[0], alerts.AlertsStep):
        assert not build(Deps(gcp=lambda: SomeDescriptors(1))).descriptors_ready(ctx)
        assert not build(Deps(gcp=lambda: SomeDescriptors(every - 1))).descriptors_ready(ctx)
        assert build(Deps(gcp=lambda: SomeDescriptors(every))).descriptors_ready(ctx)


def test_log_metrics_are_every_metric_the_monitoring_module_makes() -> None:
    """`LOG_METRICS` is held to terraform/modules/monitoring: a metric added
    there and not here would let the alert policies go on before it exists."""
    import re
    from pathlib import Path

    module = Path(__file__).resolve().parents[3] / "terraform" / "modules" / "monitoring"
    names: list[str] = []
    for tf in sorted(module.glob("*.tf")):
        text = tf.read_text()
        for match in re.finditer(r'^resource "google_logging_metric" "(\w+)" \{\n(.*?)^\}', text, re.S | re.M):
            body = match.group(2)
            assert not re.search(r"^\s*count\s*=", body, re.M), f"{match.group(1)} is conditional"
            if re.search(r"^\s*for_each\s*=\s*local\.event_metrics", body, re.M):
                block = re.search(r"^  event_metrics = \{\n(.*?)^  \}", text, re.S | re.M)
                assert block, "the event_metrics local"
                names += [key.replace("_", "-") for key in re.findall(r'^    "(\w+)" = \{', block.group(1), re.M)]
            else:
                names += re.findall(r'^\s*name\s*=\s*"\$\{var\.name_prefix\}/([^"]+)"', body, re.M)
    assert len(names) > 7
    assert sorted(names) == sorted(alerts.LOG_METRICS)
