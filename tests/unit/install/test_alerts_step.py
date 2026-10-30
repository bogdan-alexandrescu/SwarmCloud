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
