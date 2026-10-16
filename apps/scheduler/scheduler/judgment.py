"""Where a live-browser pod's Jev judgments go (JUDGE-SELECT).

Owner decisions 2026-10-14. A workspace chooses `internal-jev` (the default),
`internal-laya` or `typesafe` (swarm-api `swarm_api.judgment`, which owns the
setting, its audit and the console's view of it). The dispatcher reads the
choice at dispatch -- `workspace_settings/{tenant}.judgment_backend` -- and
turns it into what the pod carries:

  * SWARM_JEV_BACKEND, always, on a live-browser pod: the choice itself, so
    the jev wrapper can refuse an internal backend it has no URL for instead
    of falling to jev-browser's TypeSafe default;
  * JEV_API_URL, for an internal backend with the tailnet egress on
    (`internal_decision_api_url`): the decision API's `/jev/v1/decide` or
    `/laya/v1/decide` BY ITS TAILNET NAME, over https;
  * the pod label `swarm-judge: internal`, with that URL and only then: what
    JUDGE-NET's NetworkPolicy keys the decision API's egress on, so a pod
    that does not judge internally cannot reach it;
  * with the label and only with it, a `hostAliases` entry pinning the
    tailnet name to the egress Service's fixed clusterIP. The cluster's DNS
    does not know the tailnet name; the Service passes raw TLS through to the
    tailnet, so the pod verifies the API's real certificate, issued for that
    name (owner decision 2026-10-15, #1380). The in-cluster name it replaced
    had no port 80 for http and the wrong certificate for https.

TypeSafe gets none of the three: jev-browser's own default URL
and whatever TypeSafe key the runner provides (JEV-RUNNER). Every value is
the platform's -- the scheduler's settings and a document only a workspace
owner or an admin writes through swarm-api -- never a caller's (invariant 10).

A read that fails, or a stored value this code does not know, is the
DEFAULT, internal-jev: it is the choice that sends nothing outside Saga.

The names below restate swarm-api's; tests/unit/control_plane/
test_dispatch_judgment.py holds them equal.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

INTERNAL_JEV = "internal-jev"
INTERNAL_LAYA = "internal-laya"
TYPESAFE_BACKEND = "typesafe"
BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA, TYPESAFE_BACKEND)
DEFAULT_BACKEND = INTERNAL_JEV

SETTINGS_COLLECTION = "workspace_settings"
SETTING_FIELD = "judgment_backend"

#: Saga's decision API's tailnet name: what its certificate is issued for, and
#: what the egress forwarder dials (terraform/infra/tailscale.tf
#: `tailscale_egress_target`, whose default a test holds equal to this).
DECISION_API_TAILNET_NAME = "decision-api-staging.taile0bda9.ts.net"
#: The decision API's origin as a judging pod calls it: the tailnet name over
#: https, resolved by the pod's `hostAliases` entry to the egress Service.
#: The only value INTERNAL_DECISION_API_URL may hold besides "" (off).
INTERNAL_DECISION_ORIGIN = f"https://{DECISION_API_TAILNET_NAME}"
#: The egress Service `decision-api`'s clusterIP. FIXED, because a judging
#: pod pins the tailnet name to it in /etc/hosts: a Service recreated with a
#: new address would leave every such pod dialling nothing. Stated once, as
#: terraform/infra/tailscale.tf `local.decision_api_cluster_ip`, which renders
#: it into the manifest; tests/unit/control_plane/test_dispatch_judgment.py
#: holds this copy equal to it.
DECISION_API_CLUSTER_IP = "10.48.6.120"
#: The decision API's path per internal backend.
INTERNAL_PATHS = {INTERNAL_JEV: "jev", INTERNAL_LAYA: "laya"}

JEV_BACKEND_ENV = "SWARM_JEV_BACKEND"
JEV_API_URL_ENV = "JEV_API_URL"
#: What JUDGE-NET's NetworkPolicy selects to open the decision API.
JUDGE_POD_LABEL = ("swarm-judge", "internal")


def check_internal_decision_api_url(value: str) -> None:
    """"" (the egress is off) or the decision API's tailnet origin, nothing
    else: the jev wrapper accepts that host alone, and the pod's hostAliases
    entry resolves that name alone, so any other value would light the
    internal backends up in the console and fail in every pod."""
    if value and value.rstrip("/") != INTERNAL_DECISION_ORIGIN:
        raise ValueError(
            f"INTERNAL_DECISION_API_URL must be empty or {INTERNAL_DECISION_ORIGIN!r} "
            f"(the decision API by its tailnet name), not {value!r}"
        )


def jev_dispatch(backend: str, internal_url: str
                 ) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    """(env, pod labels, pod hostAliases) for a live-browser pod whose
    workspace chose `backend`. The URL, the label and the hostAliases entry
    come together or not at all."""
    if backend not in BACKENDS:
        backend = DEFAULT_BACKEND
    env = {JEV_BACKEND_ENV: backend}
    labels: dict[str, str] = {}
    host_aliases: list[dict[str, Any]] = []
    base = (internal_url or "").strip().rstrip("/")
    if backend in INTERNAL_PATHS and base:
        env[JEV_API_URL_ENV] = f"{base}/{INTERNAL_PATHS[backend]}/v1/decide"
        labels[JUDGE_POD_LABEL[0]] = JUDGE_POD_LABEL[1]
        host_aliases.append({"ip": DECISION_API_CLUSTER_IP,
                             "hostnames": [DECISION_API_TAILNET_NAME]})
    return env, labels, host_aliases


def read_backend(db: Any, tenant_id: str) -> str:
    """The tenant's stored choice; the default when none, unknown or unreadable."""
    try:
        snap = db.collection(SETTINGS_COLLECTION).document(tenant_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
    except Exception as exc:  # noqa: BLE001 - a dispatch never fails on this read
        log.warning("judgment backend unreadable for tenant=%s, using %s: %s",
                    tenant_id, DEFAULT_BACKEND, type(exc).__name__)
        return DEFAULT_BACKEND
    value = doc.get(SETTING_FIELD)
    return value if isinstance(value, str) and value in BACKENDS else DEFAULT_BACKEND
