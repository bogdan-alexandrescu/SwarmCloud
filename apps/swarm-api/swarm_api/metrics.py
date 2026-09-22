"""Prometheus metrics for the API.

A private registry rather than the global default one: the global registry picks
up process and GC collectors from any library that imports prometheus_client,
and two app instances in one test process would then collide on registration.
Everything the API exports is declared here.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST


class ApiMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.requests = Counter(
            "swarm_api_requests_total",
            "API requests by route template, method and status class.",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.request_latency = Histogram(
            "swarm_api_request_seconds",
            "API request latency by route template.",
            ["route"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self.tasks_submitted = Counter(
            "swarm_api_tasks_submitted_total",
            "Tasks accepted, by tenant and runner profile.",
            ["tenant", "runner_profile"],
            registry=self.registry,
        )
        self.tasks_rejected = Counter(
            "swarm_api_tasks_rejected_total",
            "Task submissions rejected by a guard, by reason.",
            ["reason"],
            registry=self.registry,
        )
        self.workflows_submitted = Counter(
            "swarm_api_workflows_submitted_total",
            "Workflows accepted, by tenant.",
            ["tenant"],
            registry=self.registry,
        )
        self.workflow_state_drift = Counter(
            "swarm_api_workflow_state_drift_total",
            "Workflow reads where the stored state and the state derived from "
            "the steps did not agree (disagree), or could not be compared "
            "because a step task was unreadable (unknown). Taken BEFORE the "
            "write-back repairs it, so a repaired disagreement still counts.",
            ["direction"],
            registry=self.registry,
        )
        self.auth_failures = Counter(
            "swarm_api_auth_failures_total",
            "Authentication and authorisation failures, by kind.",
            ["kind"],
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "swarm_api_rate_limited_total",
            "Requests rejected by the per-principal token bucket.",
            ["tenant"],
            registry=self.registry,
        )
        self.credentials_written = Counter(
            "swarm_api_tenant_credentials_written_total",
            "Per-tenant provider keys written to Secret Manager.",
            ["tenant", "provider"],
            registry=self.registry,
        )
        self.admin_actions = Counter(
            "swarm_api_admin_actions_total",
            "Admin control-plane mutations, by action.",
            ["action"],
            registry=self.registry,
        )
        self.dispatch_paused = Gauge(
            "swarm_api_dispatch_paused",
            "1 when global dispatch is paused by an admin.",
            registry=self.registry,
        )
        self.wake_failures = Counter(
            "swarm_api_scheduler_wake_failures_total",
            "Failed attempts to nudge the scheduler over Pub/Sub.",
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
