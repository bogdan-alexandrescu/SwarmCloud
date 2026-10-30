"""Stage 13, alerts: a channel that reaches someone, `require_alert_channels`
on, and the alert policies, made only after their metrics' descriptors exist
(design §4; plan R7; #1817; lesson 23).

Plan R7 moved the channel and the flag into infra pass 2 (the alert email is
asked in stage 0), so this stage checks them. The policies are the exception:
an alert on a log-based metric fails with "Cannot find metric" until the
metric's descriptor exists (lesson 23), so infra passes run with
`create_alerts = false` until this step has seen every platform log metric's
descriptor; then it turns `create_alerts` on and applies infra pass 2 again.
"""

from __future__ import annotations

from typing import Any

from .. import tfvars
from ..exitcodes import MET, UNMET
from ..waits import WaitSpec, wait_until
from . import Observation, Plan
from .bootstrap import DEPS, Deps, apply_pass
from .preflight import remember

#: The platform's log-based metrics (google_logging_metric, `swarm_*`).
LOG_METRIC_PREFIX = "logging.googleapis.com/user/swarm"

DESCRIPTORS = WaitSpec(
    reason="the log metrics' descriptors appearing in Cloud Monitoring",
    expected="1 to 5 min after infra pass 2",
    expected_s=5 * 60,
    bound_s=20 * 60,
    look_at="gcloud logging metrics list: each swarm_* metric's descriptor appears after it is created",
    interval_s=20,
)


class AlertsStep:
    id = "alerts.policies"
    stage = 13
    kind = "engine"
    satisfies = ("monitoring.notification-channel", "monitoring.alert-policies")

    def __init__(self, deps: Deps, *, expected_metrics: int | None = None) -> None:
        self.deps = deps
        self._expected = expected_metrics

    def explain(self) -> str:
        return ("Alerts that reach nobody were #1817: the install sets require_alert_channels with the email "
                "asked in stage 0. An alert on a log metric fails until the metric's descriptor exists "
                "(lesson 23), so the policies are applied after the descriptors are seen.")

    def problems(self, ctx: Any) -> list[str]:
        gcp = self.deps.gcp()
        found = []
        email = str(ctx.outputs.get("alert_email") or "")
        channels = gcp.notification_channels(ctx.project)
        if not any((c.get("labels") or {}).get("email_address") == email for c in channels):
            found.append(f"no notification channel sends to {email or 'the alert email (stage 0 asks it)'}")
        bucket = ctx.engine.store.bucket
        stored = tfvars.stored(bucket, "infra") if bucket is not None else None
        if not stored or stored.get("require_alert_channels") is not True:
            found.append("require_alert_channels is not true in the applied infra inputs")
        if not ctx.outputs.get("metric_descriptors_ready"):
            found.append("the alert policies wait for their metrics' descriptors")
        elif not gcp.alert_policies(ctx.project):
            found.append("no alert policy exists")
        return found

    def check(self, ctx: Any) -> Observation:
        found = self.problems(ctx)
        return Observation(UNMET, "; ".join(found)) if found else Observation(MET, "alerts reach " + str(
            ctx.outputs.get("alert_email")))

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def plan(self, ctx: Any) -> Plan:
        return Plan("wait for the log metrics' descriptors, then apply infra pass 2 with create_alerts on",
                    tuple(self.problems(ctx)))

    def descriptors_ready(self, ctx: Any) -> bool:
        found = self.deps.gcp().metric_descriptors(ctx.project, LOG_METRIC_PREFIX)
        if self._expected is not None:
            return len(found) >= self._expected
        return bool(found)

    def apply(self, ctx: Any, plan: Plan) -> None:
        if not ctx.outputs.get("metric_descriptors_ready"):
            wait_until(self.id, DESCRIPTORS, lambda: MET if self.descriptors_ready(ctx) else UNMET,
                       clock=ctx.clock, events=ctx.events)
            remember(ctx, outputs={"metric_descriptors_ready": True})
        apply_pass(ctx, self.deps, "infra-2")


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [AlertsStep(deps)]


STEPS = build_steps()
