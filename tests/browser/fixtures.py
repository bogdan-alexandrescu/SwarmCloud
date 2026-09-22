#!/usr/bin/env python3
"""Generate the control plane's response bodies THROUGH THE REAL SERIALISERS.

WHY THIS IS PYTHON AND NOT A .json FILE CHECKED IN
--------------------------------------------------
The defect this whole suite exists for has one shape: both ends built, the seam
between them never executed. A hand-written JSON fixture reproduces that blind
spot exactly -- it is a second, independent restatement of the API's response
shape, so the browser suite would keep passing after the API stopped sending
what the fixture says it sends.

So every payload below is built by calling `swarm_api.codec` and
`swarm_api.headroom` on Firestore-shaped documents, the same functions the live
route calls. `attempt_to_api(attempt_from_dict(doc))` is the exact expression
that dropped all five spend fields for weeks; running it here means the browser
assertion "a measured cost renders a number, not an em dash" is a live guard on
that decoder, not a guard on a JSON file somebody typed.

Nothing here reads Firestore, reaches the network, or needs a credential. The
codec functions are pure dict -> dataclass -> dict.

THE ONE THING THAT IS HAND-BUILT, and why. `/v1/accounts` is proxied from the
quota broker, so swarm-api has no serialiser for it to borrow -- routes/accounts.py
passes the broker's payload straight through. That block is marked below and is
the only one whose shape could drift without this file noticing.

Output: one JSON object on stdout, keyed by "<METHOD> <path-template>". The Node
control plane (control-plane.mjs) looks up, it does not construct -- building an
envelope in JavaScript would be the same restatement one language further away.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps" / "common"))
sys.path.insert(0, str(REPO / "apps" / "swarm-api"))

from swarm_api.codec import (  # noqa: E402
    attempt_from_dict,
    attempt_to_api,
    lease_from_dict,
    lease_to_api,
    pool_from_dict,
    pool_to_api,
    quota_from_dict,
    quota_to_api,
    task_from_dict,
    task_to_api,
    tenant_from_dict,
    tenant_to_api,
    workflow_from_dict,
    workflow_to_api,
)
from swarm_api.codec import event_from_dict  # noqa: E402
from swarm_api.headroom import analyse_profile, blocked_reason_groups  # noqa: E402
# The two catalogue routes are CALLED, not restated. Both are pure functions of
# the frozen catalogue with no store read, so a fixture that copied their output
# would be the drift `check-contract-parity.sh` exists to catch, one language
# further away where that script cannot see it.
from swarm_api.routes.admin import _heartbeat_grace_seconds  # noqa: E402
from swarm_api.routes.platform import resource_classes as route_resource_classes  # noqa: E402
from swarm_api.routes.platform import runtimes as route_runtimes  # noqa: E402
from swarm_api.routes.tasks import _event_to_api  # noqa: E402
from swarm_common.config import Settings  # noqa: E402
from swarm_common.models import pool_names_for  # noqa: E402
from swarm_common.profiles import (  # noqa: E402
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    resolve_backend,
)

TENANT = "u-bogdan"

# RELATIVE TO THE REAL CLOCK, not a frozen literal.
#
# `lease_to_api` computes `expired` and `dispatch_overdue` against utcnow(), and
# every screen renders ages ("started 38m ago"). A pinned timestamp would make
# the fixtures drift into the past until every lease read as expired and every
# age as weeks old -- so the suite would start reporting a platform on fire
# purely because a day had passed. Everything below is therefore expressed as an
# offset from this instant, and the run is reproducible in shape rather than in
# literal bytes.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def iso(value: Any) -> Any:
    """Match what FastAPI puts on the wire for a datetime."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [iso(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Firestore-shaped documents. These are the INPUT side of the real decoders.
# ---------------------------------------------------------------------------

TASK_DOCS: list[dict[str, Any]] = [
    {
        # RUNNING, measured spend. The row that proves the decoder round-trips.
        "id": "task_27a0faef396c43f58878",
        "tenant_id": TENANT,
        "created_at": ago(minutes=41),
        "updated_at": ago(minutes=2),
        "state": "RUNNING",
        "runner_profile": "claude-code",
        "resource_class": "standard",
        "input": {"prompt": "Audit the admission path for the all-or-nothing invariant."},
        "submitted_by": "bogdan@saga.xyz",
        "provider": "anthropic",
        "model": "claude-opus-4",
        "attempt_count": 1,
        "current_lease_id": "lease_3f21aa9c",
        "current_generation": 1,
        "started_at": ago(minutes=38),
        "workflow_id": "wf_0d97f873656a4f0b9c11",
        "step_id": "research",
        "latest_checkpoint": "gs://swarm-artifacts-dev/u-bogdan/task_27a0faef396c43f58878/ckpt-4",
    },
    {
        # PARKED on a provider wait. Invariant 4's visible face.
        "id": "task_53ddef4c3662423a8e3d",
        "tenant_id": TENANT,
        "created_at": ago(hours=3, minutes=12),
        "updated_at": ago(minutes=19),
        "state": "PARKED",
        "runner_profile": "claude-code",
        "resource_class": "standard",
        "input": {"prompt": "Draft the migration note."},
        "submitted_by": "bogdan@saga.xyz",
        "provider": "anthropic",
        "attempt_count": 2,
        "park_reason": "PROVIDER_QUOTA_EXHAUSTED",
        "next_eligible_at": NOW + timedelta(minutes=48),
        "workflow_id": "wf_0d97f873656a4f0b9c11",
        "step_id": "draft",
        "depends_on": ["research"],
    },
    {
        # SUCCEEDED with a result summary carrying artifacts. The headline
        # feature's evidence: artifacts written by the agent, not by the runner.
        "id": "task_796d05f116c147e9854d",
        "tenant_id": TENANT,
        "created_at": ago(hours=5),
        "updated_at": ago(hours=4, minutes=21),
        "state": "SUCCEEDED",
        "runner_profile": "claude-code",
        "resource_class": "standard",
        "input": {"prompt": "Summarise yesterday's incident."},
        "submitted_by": "bogdan@saga.xyz",
        "provider": "anthropic",
        "attempt_count": 1,
        "started_at": ago(hours=4, minutes=52),
        "completed_at": ago(hours=4, minutes=21),
        "result_summary": {
            "artifacts": ["incident-summary.md", "timeline.json"],
            "exit_code": 0,
        },
    },
    {
        # FAILED, so a non-zero count exists for the checks pane to report.
        "id": "task_7c2a070c011042a5865a",
        "tenant_id": TENANT,
        "created_at": ago(hours=7),
        "updated_at": ago(hours=6, minutes=40),
        "state": "FAILED",
        "runner_profile": "codex",
        "resource_class": "standard",
        "input": {"prompt": "Port the reconciler detection tests."},
        "submitted_by": "bogdan@saga.xyz",
        "provider": "openai",
        "attempt_count": 3,
        "started_at": ago(hours=6, minutes=58),
        "completed_at": ago(hours=6, minutes=40),
        "last_error": "the agent exited 1 without writing a checkpoint",
    },
    {
        # QUEUED. Invariant 1: costs nothing, creates no demand.
        "id": "task_b208fc8542724268b5f4",
        "tenant_id": TENANT,
        "created_at": ago(minutes=6),
        "updated_at": ago(minutes=6),
        "state": "QUEUED",
        "runner_profile": "browser",
        "resource_class": "browser",
        "input": {"prompt": "Capture the pricing page at three widths."},
        "submitted_by": "bogdan@saga.xyz",
        "provider": "anthropic",
    },
    {
        # A mock run: costs nothing ON PURPOSE. This is the row that separates
        # "measured zero" from "not measured" on screen.
        "id": "task_b5dc2568713a40158851",
        "tenant_id": TENANT,
        "created_at": ago(hours=1, minutes=5),
        "updated_at": ago(hours=1),
        "state": "SUCCEEDED",
        "runner_profile": "mock",
        "resource_class": "standard",
        "input": {"prompt": "smoke"},
        "submitted_by": "bogdan@saga.xyz",
        "attempt_count": 1,
        "started_at": ago(hours=1, minutes=4),
        "completed_at": ago(hours=1),
        "result_summary": {"artifacts": [], "exit_code": 0},
    },
]

ATTEMPT_DOCS: dict[str, list[dict[str, Any]]] = {
    "task_27a0faef396c43f58878": [
        {
            "attempt_id": "att_5b1c9e07a1f34d2b",
            "task_id": "task_27a0faef396c43f58878",
            "tenant_id": TENANT,
            "generation": 1,
            "lease_id": "lease_3f21aa9c",
            "backend": "cloud_run_job",
            "created_at": ago(minutes=39),
            "started_at": ago(minutes=38),
            "execution_name": "swarm-claude-code-2p7xk",
            "peak_rss_bytes": 6_442_450_944,
            "peak_disk_bytes": 1_073_741_824,
            "oom_near_miss": True,
            "checkpoints": ["ckpt-1", "ckpt-2", "ckpt-3", "ckpt-4"],
            # MEASURED. If attempt_from_dict ever drops these again the browser
            # suite goes red on "a measured figure renders a number".
            "input_tokens": 182_430,
            "output_tokens": 24_118,
            "cache_read_input_tokens": 1_204_991,
            "cache_creation_input_tokens": 88_040,
            "cost_usd": 4.2719,
        }
    ],
    "task_b5dc2568713a40158851": [
        {
            # MEASURED ZERO. A mock run really did cost $0.00 and 0 tokens, and
            # that must render as 0 -- never as the em dash that means unknown.
            "attempt_id": "att_0a77c3d918e64f55",
            "task_id": "task_b5dc2568713a40158851",
            "tenant_id": TENANT,
            "generation": 1,
            "lease_id": "lease_91cc0d42",
            "backend": "cloud_run_job",
            "created_at": ago(hours=1, minutes=5),
            "started_at": ago(hours=1, minutes=4),
            "completed_at": ago(hours=1),
            "exit_code": 0,
            "peak_rss_bytes": 201_326_592,
            "checkpoints": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost_usd": 0.0,
        }
    ],
    "task_7c2a070c011042a5865a": [
        {
            # NOT MEASURED. The five spend fields are absent from the document,
            # so the API serves null and the UI owes an em dash.
            "attempt_id": "att_c41b8ee2705a4f19",
            "task_id": "task_7c2a070c011042a5865a",
            "tenant_id": TENANT,
            "generation": 3,
            "lease_id": "lease_77ab13de",
            "backend": "cloud_run_job",
            "created_at": ago(hours=7),
            "started_at": ago(hours=6, minutes=58),
            "completed_at": ago(hours=6, minutes=40),
            "exit_code": 1,
            "error": "the agent exited 1 without writing a checkpoint",
            "peak_rss_bytes": 3_221_225_472,
            "checkpoints": [],
        }
    ],
}

EVENT_DOCS: dict[str, list[dict[str, Any]]] = {
    "task_27a0faef396c43f58878": [
        {"type": "submitted", "at": ago(minutes=41)},
        {"type": "lease_acquired", "at": ago(minutes=40)},
        {"type": "dispatched", "at": ago(minutes=39)},
        {"type": "running", "at": ago(minutes=38)},
        {"type": "checkpoint_completed", "at": ago(minutes=12)},
    ],
    "task_53ddef4c3662423a8e3d": [
        {"type": "submitted", "at": ago(hours=3, minutes=12)},
        {"type": "parked", "at": ago(minutes=19)},
    ],
}

POOL_DOCS: dict[str, dict[str, Any]] = {
    "global": {"hard_limit": 64, "active": 11, "enabled": True, "updated_at": ago(seconds=30)},
    "resource:standard": {"hard_limit": 48, "active": 9, "enabled": True, "updated_at": ago(seconds=30)},
    "resource:browser": {"hard_limit": 8, "active": 2, "enabled": True, "updated_at": ago(seconds=30)},
    "resource:large": {"hard_limit": 4, "active": 0, "enabled": True, "updated_at": ago(seconds=30)},
    "runner:claude-code": {"hard_limit": 32, "active": 8, "enabled": True, "updated_at": ago(seconds=30)},
    "runner:codex": {"hard_limit": 16, "active": 1, "enabled": True, "updated_at": ago(seconds=30)},
    "runner:browser": {"hard_limit": 8, "active": 2, "enabled": True, "updated_at": ago(seconds=30)},
    "runner:generic": {"hard_limit": 8, "active": 0, "enabled": True, "updated_at": ago(seconds=30)},
    "runner:mock": {"hard_limit": 4, "active": 0, "enabled": True, "updated_at": ago(seconds=30)},
    "backend:cloud_run_job": {"hard_limit": 56, "active": 9, "enabled": True, "updated_at": ago(seconds=30)},
    "backend:gke_autopilot": {"hard_limit": 8, "active": 2, "enabled": True, "updated_at": ago(seconds=30)},
    # PAUSED. `enabled: false` is the one thing this column exists to show, and
    # `pool.enabled ?? true` / `false // true` both report it as open.
    "provider:openai": {"hard_limit": 12, "active": 1, "enabled": False, "updated_at": ago(minutes=9)},
    "provider:anthropic": {"hard_limit": 40, "active": 10, "enabled": True, "updated_at": ago(seconds=30)},
    f"provider:anthropic:tenant:{TENANT}": {
        "hard_limit": 20, "active": 10, "enabled": True, "updated_at": ago(seconds=30),
    },
    # The out-of-band drift README.md points at: active above the configured
    # ceiling. A fixture showing a tidy platform teaches the wrong thing.
    f"tenant:{TENANT}": {"hard_limit": 2, "active": 40, "enabled": True, "updated_at": ago(seconds=30)},
}

LEASE_DOCS: list[dict[str, Any]] = [
    {
        "lease_id": "lease_3f21aa9c",
        "task_id": "task_27a0faef396c43f58878",
        "attempt_id": "att_5b1c9e07a1f34d2b",
        "tenant_id": TENANT,
        "generation": 1,
        "pools": pool_names_for(
            tenant_id=TENANT,
            provider="anthropic",
            resource_class="standard",
            runner_profile="claude-code",
            backend="cloud_run_job",
        ),
        "units": 1,
        "state": "RUNNING",
        "created_at": ago(minutes=40),
        "dispatch_deadline": ago(minutes=35),
        "expires_at": NOW + timedelta(minutes=80),
        "heartbeat_at": ago(seconds=22),
    },
    {
        # Admitted and never started: dispatch_overdue. The reconciler's target.
        "lease_id": "lease_5ed0b7c4",
        "task_id": "task_b208fc8542724268b5f4",
        "attempt_id": "att_d2f0916b47ae4c80",
        "tenant_id": TENANT,
        "generation": 1,
        "pools": pool_names_for(
            tenant_id=TENANT,
            provider="anthropic",
            resource_class="browser",
            runner_profile="browser",
            backend="gke_autopilot",
        ),
        "units": 2,
        "state": "LEASED",
        "created_at": ago(minutes=14),
        "dispatch_deadline": ago(minutes=9),
        "expires_at": NOW + timedelta(minutes=45),
    },
]

QUOTA_DOCS: list[dict[str, Any]] = [
    {
        "provider": "anthropic",
        "tenant_id": TENANT,
        "state": "AVAILABLE",
        "updated_at": ago(minutes=1),
        "configured_hard_max": 40,
        "adaptive_target": 34,
        "requests_remaining": 812,
        "tokens_remaining": 4_120_000,
        "reset_at": NOW + timedelta(hours=2, minutes=14),
        "success_count": 1841,
        "rate_limit_count": 3,
    },
    {
        # EXHAUSTED: effective_limit is 0 BY RULE, a fact rather than a gap.
        "provider": "openai",
        "tenant_id": TENANT,
        "state": "EXHAUSTED",
        "updated_at": ago(minutes=9),
        "configured_hard_max": 12,
        "requests_remaining": 0,
        "tokens_remaining": 0,
        "reset_at": NOW + timedelta(minutes=51),
        "last_429_at": ago(minutes=9),
        "retry_after_seconds": 3060,
        "success_count": 204,
        "rate_limit_count": 47,
    },
]

TENANT_DOCS: list[dict[str, Any]] = [
    {
        "tenant_id": TENANT,
        "kind": "user",
        "principal": "bogdan@saga.xyz",
        "created_at": ago(days=38),
        "display_name": "Bogdan Alexandrescu",
        "max_active": 20,
        "capacity_units": 40,
        "monthly_budget_usd": 400.0,
        "enabled": True,
        "credentials": ["anthropic"],
        "service_account": f"swarm-tenant-{TENANT}@saga-agents-dev.iam.gserviceaccount.com",
        "gcs_prefix": f"gs://swarm-artifacts-dev/{TENANT}/",
        "namespace": f"swarm-tenant-{TENANT}",
    },
    {
        "tenant_id": "eng",
        "kind": "group",
        "principal": "eng@saga.xyz",
        "created_at": ago(days=112),
        "display_name": "Engineering",
        "max_active": 40,
        "capacity_units": 80,
        "enabled": False,
        "credentials": ["anthropic", "openai"],
    },
]

WORKFLOW_DOCS: list[dict[str, Any]] = [
    {
        "workflow_id": "wf_0d97f873656a4f0b9c11",
        "tenant_id": TENANT,
        "created_at": ago(hours=3, minutes=20),
        "updated_at": ago(minutes=2),
        "state": "RUNNING",
        "submitted_by": "bogdan@saga.xyz",
        "steps": [
            {"step_id": "research", "runner_profile": "claude-code", "input": {"prompt": "research"},
             "task_id": "task_27a0faef396c43f58878"},
            {"step_id": "draft", "runner_profile": "claude-code", "input": {"prompt": "draft"},
             "depends_on": ["research"], "input_from": {"research": "findings.md"},
             "task_id": "task_53ddef4c3662423a8e3d"},
            {"step_id": "review", "runner_profile": "claude-code", "input": {"prompt": "review"},
             "depends_on": ["draft"], "input_from": {"draft": "draft.md"}},
        ],
    },
    {
        "workflow_id": "wf_c31a6b2049f84d7e8a02",
        "tenant_id": TENANT,
        "created_at": ago(minutes=7),
        "updated_at": ago(minutes=7),
        # Nothing in the repository ever advances a workflow state, so a
        # workflow that has been QUEUED since submission is the honest fixture.
        "state": "QUEUED",
        "submitted_by": "bogdan@saga.xyz",
        "steps": [
            {"step_id": "collect", "runner_profile": "browser", "input": {"prompt": "collect"}},
            {"step_id": "report", "runner_profile": "generic", "input": {"prompt": "report"},
             "depends_on": ["collect"]},
        ],
    },
]


# ---------------------------------------------------------------------------
# Envelopes, assembled the way the routes assemble them
# ---------------------------------------------------------------------------

def tasks_page() -> dict[str, Any]:
    # routes/tasks.py:73 list_tasks
    return {
        "tasks": [task_to_api(task_from_dict(d)) for d in TASK_DOCS],
        "next_page_token": None,
        "tenant_id": TENANT,
    }


def capacity() -> dict[str, Any]:
    # service.py:457 capacity -- the real analyser, not a re-derivation.
    pools = {name: pool_from_dict(name, doc) for name, doc in POOL_DOCS.items()}
    profiles: dict[str, Any] = {}
    for name, profile in RUNNER_PROFILES.items():
        backend = resolve_backend(profile).value
        required = pool_names_for(
            tenant_id=TENANT,
            provider=profile.provider,
            resource_class=profile.resource_class,
            runner_profile=name,
            backend=backend,
        )
        units = RESOURCE_CLASSES[profile.resource_class].units
        readable = {n: pools[n] for n in required if n in pools}
        profiles[name] = {
            "resource_class": profile.resource_class,
            "backend": backend,
            "provider": profile.provider,
            "units": units,
            "pools": required,
            "admission": analyse_profile(required=required, pools=readable, units=units, unread=()),
        }
    return {
        "tenant_id": TENANT,
        "pools": sorted((pool_to_api(p) for p in pools.values()), key=lambda p: p["name"]),
        "pools_complete": True,
        "runner_profiles": profiles,
        "blocked_reason_groups": blocked_reason_groups(),
        "generated_at": NOW,
    }


def runtimes() -> dict[str, Any]:
    """routes/platform.py:90, CALLED.

    Both catalogue routes read no store and no tenant, so the handler is a pure
    function and can be invoked directly. `auth` is declared to gate the route
    and never read -- the route's own comment says so -- so None is the honest
    argument. Resize a class in the frozen catalogue and this fixture moves with
    it in the same commit, which is the property a copied table cannot have.
    """
    return route_runtimes(auth=None)


def resource_classes() -> dict[str, Any]:
    return route_resource_classes(auth=None)


def stats() -> dict[str, Any]:
    # service.py:434 stats. The zeros here are MEASURED zeros: the count really
    # is nought, which is a different pixel from a count that could not be read.
    by_state: dict[str, int] = {}
    for doc in TASK_DOCS:
        by_state[doc["state"]] = by_state.get(doc["state"], 0) + 1
    by_state.setdefault("CANCELLED", 0)
    by_state.setdefault("READY", 0)
    return {
        "tenant_id": TENANT,
        "tasks_by_state": by_state,
        "platform_tasks_by_state": by_state,
        "dispatch_paused": False,
        "limits": {
            "max_batch_size": 100,
            "max_input_bytes": 262144,
            "max_workflow_steps": 50,
            "requests_per_second_per_instance": 20,
        },
        "generated_at": NOW,
    }


def providers() -> dict[str, Any]:
    # service.py:553 providers
    quota_by_provider = {d["provider"]: quota_from_dict(d) for d in QUOTA_DOCS}
    entries = []
    tenant = tenant_from_dict(TENANT_DOCS[0])
    for name in sorted({p.provider for p in RUNNER_PROFILES.values() if p.provider}):
        q = quota_by_provider.get(name)
        entries.append(
            {
                "provider": name,
                "credential_registered": name in tenant.credentials,
                "runner_profiles": sorted(p.name for p in RUNNER_PROFILES.values() if p.provider == name),
                "quota": quota_to_api(q) if q else None,
            }
        )
    return {"tenant_id": TENANT, "providers": entries, "generated_at": NOW}


def accounts() -> dict[str, Any]:
    """THE ONE HAND-BUILT BLOCK.

    routes/accounts.py:157 passes the quota broker's payload through untouched,
    so swarm-api owns no serialiser for it and there is nothing here to borrow.
    The shape is types.ts:1707 `Account`. If the broker changes it, this is the
    block that will not notice -- recorded rather than hidden.
    """
    return {
        "tenant_id": TENANT,
        "accounts": [
            {
                "account_id": f"{TENANT}:primary",
                "owner_tenant": TENANT,
                "label": "primary",
                "provider": "anthropic",
                "state": "ACTIVE",
                "reason": "refreshed 4 minutes ago",
                "lend_to": ["eng"],
                "assigned": 2,
                "windows": {
                    "five_hour": {"used": 41, "limit": 100, "resets_at": iso(NOW + timedelta(hours=1, minutes=9))},
                    "seven_day": {"used": 312, "limit": 900, "resets_at": iso(NOW + timedelta(days=4))},
                },
                "observed_at": iso(ago(minutes=4)),
                "stale": False,
            },
            {
                # REAUTH_REQUIRED removes capacity exactly the way a lowered
                # ceiling does, and its reading is old enough not to be trusted.
                "account_id": f"{TENANT}:overflow",
                "owner_tenant": TENANT,
                "label": "overflow",
                "provider": "anthropic",
                "state": "REAUTH_REQUIRED",
                "reason": "the refresh token was rejected; sign in again to restore this account",
                "lend_to": [],
                "assigned": 0,
                "windows": {},
                "observed_at": None,
                "stale": True,
            },
        ],
    }


def admin_leases() -> dict[str, Any]:
    """routes/admin.py:387 list_leases, envelope and all.

    THE FIRST RUN OF THIS SUITE CAUGHT THE ENVELOPE HERE BEING WRONG. An
    earlier draft of this file served `{leases, tenant_id}` and nothing else;
    the route also sends `thresholds`, `evaluated_at`, `active_only` and
    `units_held`, and Overview.tsx reads `thresholds.heartbeat_grace_seconds`
    without a guard. The browser rendered the ErrorBoundary -- "Cannot read
    properties of undefined" -- on the very first navigation.

    That is the suite's own shape working on the suite: an envelope assembled
    by hand drifted from the route it claims to imitate, and running it in a
    browser is what said so. The three per-row fields and the grace are
    therefore computed the way the route computes them, from the same
    `_heartbeat_grace_seconds` the reconciler's config derives, never from a
    literal 90 typed here.
    """
    now = NOW
    core = Settings(project_id="swarm-ui-test")
    rows = []
    last_error_by_task = {d["id"]: d.get("last_error") for d in TASK_DOCS}
    for doc in LEASE_DOCS:
        lease = lease_from_dict(doc)
        row = lease_to_api(lease)
        row["last_error"] = last_error_by_task.get(lease.task_id)
        beat = lease.heartbeat_at if lease.heartbeat_at is not None else lease.created_at
        row["silent_seconds"] = max(0, int((now - beat).total_seconds()))
        row["heartbeat_ever"] = lease.heartbeat_at is not None
        rows.append(row)
    return {
        "leases": rows,
        "thresholds": {
            "heartbeat_grace_seconds": _heartbeat_grace_seconds(core),
            "lease_timeout_seconds": core.lease_timeout_seconds,
        },
        "evaluated_at": now,
        "active_only": True,
        "tenant_id": None,
        "units_held": sum(lease_from_dict(d).units for d in LEASE_DOCS),
    }


def me() -> dict[str, Any]:
    # routes/tenants.py:24 get_me
    return {
        "tenant": tenant_to_api(tenant_from_dict(TENANT_DOCS[0])),
        "principal": {
            "email": "bogdan@saga.xyz",
            "domain": "saga.xyz",
            "groups": ["eng@saga.xyz"],
            "is_admin": True,
            "is_admin_unresolved": False,
        },
    }


def workflows_page() -> dict[str, Any]:
    return {
        "workflows": [workflow_to_api(workflow_from_dict(d)) for d in WORKFLOW_DOCS],
        "next_page_token": None,
        "tenant_id": TENANT,
    }


def build() -> dict[str, Any]:
    tasks_by_id = {d["id"]: task_to_api(task_from_dict(d)) for d in TASK_DOCS}

    attempts_by_task = {
        task_id: {
            "task_id": task_id,
            "attempts": [attempt_to_api(attempt_from_dict(a)) for a in docs],
        }
        for task_id, docs in ATTEMPT_DOCS.items()
    }
    for doc in TASK_DOCS:
        attempts_by_task.setdefault(doc["id"], {"task_id": doc["id"], "attempts": []})

    # Through the real decoder AND the real route serialiser. `_event_to_api`
    # is private to routes/tasks.py on purpose; importing it is deliberate,
    # because the alternative is an eight-key envelope typed out here that
    # would keep passing after the route's changed.
    events_by_task: dict[str, Any] = {}
    for doc in TASK_DOCS:
        raw = EVENT_DOCS.get(doc["id"], [])
        events_by_task[doc["id"]] = {
            "task_id": doc["id"],
            "events": [
                _event_to_api(
                    event_from_dict(
                        {
                            "event_id": f"evt_{doc['id'][-6:]}_{i}",
                            "task_id": doc["id"],
                            "tenant_id": TENANT,
                            "type": e["type"],
                            "at": e["at"],
                        }
                    )
                )
                for i, e in enumerate(raw)
            ],
        }

    routes: dict[str, Any] = {
        "GET /v1/capacity": capacity(),
        "GET /v1/stats": stats(),
        "GET /v1/providers": providers(),
        "GET /v1/resource-classes": resource_classes(),
        "GET /v1/runtimes": runtimes(),
        "GET /v1/tasks": tasks_page(),
        "GET /v1/workflows": workflows_page(),
        "GET /v1/tenants/me": me(),
        "GET /v1/accounts": accounts(),
        "GET /v1/admin/dispatch": {
            "dispatch_paused": False,
            "paused_by": None,
            "paused_at": None,
            "reason": None,
        },
        "GET /v1/admin/leases": admin_leases(),
        "GET /v1/admin/quota": {
            "quota": [
                quota_to_api(quota_from_dict(d))
                for d in sorted(QUOTA_DOCS, key=lambda q: (q["provider"], q["tenant_id"]))
            ],
            "tenant_id": None,
        },
        "GET /v1/admin/tenants": {"tenants": [tenant_to_api(tenant_from_dict(d)) for d in TENANT_DOCS]},
        "GET /v1/admin/pools": {
            "pools": sorted(
                (pool_to_api(pool_from_dict(n, d)) for n, d in POOL_DOCS.items()),
                key=lambda p: p["name"],
            )
        },
        "GET /v1/tasks/{task_id}": {tid: {"task": t} for tid, t in tasks_by_id.items()},
        "GET /v1/tasks/{task_id}/attempts": attempts_by_task,
        "GET /v1/tasks/{task_id}/events": events_by_task,
        "GET /v1/tasks/{task_id}/checkpoints": {
            tid: {"task_id": tid, "checkpoints": [], "next_page_token": None} for tid in tasks_by_id
        },
        "GET /v1/tasks/{task_id}/logs": {
            tid: {
                "task_id": tid,
                "attempt_id": None,
                "stream": "stdout",
                "source": "gcs",
                "offset": 0,
                "next_offset": 0,
                "eof": True,
                "text": "",
            }
            for tid in tasks_by_id
        },
        "GET /v1/workflows/{workflow_id}": {
            d["workflow_id"]: {
                "workflow": workflow_to_api(workflow_from_dict(d)),
                "dispatch": {"strategy": "sequential", "carrier": "gcs"},
                "tasks": [
                    tasks_by_id[s["task_id"]]
                    for s in d["steps"]
                    if s.get("task_id") in tasks_by_id
                ],
            }
            for d in WORKFLOW_DOCS
        },
    }

    return {
        "generated_at": iso(NOW),
        "tenant_id": TENANT,
        "ids": {
            "running_task": "task_27a0faef396c43f58878",
            "measured_zero_task": "task_b5dc2568713a40158851",
            "unmeasured_task": "task_7c2a070c011042a5865a",
            "workflow": "wf_0d97f873656a4f0b9c11",
        },
        "routes": iso(routes),
    }


if __name__ == "__main__":
    json.dump(build(), sys.stdout, indent=1, sort_keys=False)
    sys.stdout.write("\n")
