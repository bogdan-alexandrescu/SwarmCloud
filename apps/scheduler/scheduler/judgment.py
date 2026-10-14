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
    (`internal_decision_api_url`): the egress Service's `/jev/v1/decide` or
    `/laya/v1/decide`;
  * the pod label `swarm-judge: internal`, with that URL and only then: what
    JUDGE-NET's NetworkPolicy keys the decision API's egress on, so a pod
    that does not judge internally cannot reach it.

TypeSafe gets neither the URL nor the label: jev-browser's own default URL
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

#: The Tailscale egress Service in swarm-system (JUDGE-NET), as a pod sees it.
INTERNAL_DECISION_SERVICE = "http://decision-api.swarm-system.svc.cluster.local"
#: The decision API's path per internal backend.
INTERNAL_PATHS = {INTERNAL_JEV: "jev", INTERNAL_LAYA: "laya"}

JEV_BACKEND_ENV = "SWARM_JEV_BACKEND"
JEV_API_URL_ENV = "JEV_API_URL"
#: What JUDGE-NET's NetworkPolicy selects to open the decision API.
JUDGE_POD_LABEL = ("swarm-judge", "internal")


def check_internal_decision_api_url(value: str) -> None:
    """"" (the egress is off) or the egress Service's address, nothing else:
    the jev wrapper accepts that host alone, so any other value would light
    the internal backends up in the console and fail in every pod."""
    if value and value.rstrip("/") != INTERNAL_DECISION_SERVICE:
        raise ValueError(
            f"INTERNAL_DECISION_API_URL must be empty or {INTERNAL_DECISION_SERVICE!r} "
            f"(the Tailscale egress Service in swarm-system), not {value!r}"
        )


def jev_dispatch(backend: str, internal_url: str) -> tuple[dict[str, str], dict[str, str]]:
    """(env, pod labels) for a live-browser pod whose workspace chose `backend`."""
    if backend not in BACKENDS:
        backend = DEFAULT_BACKEND
    env = {JEV_BACKEND_ENV: backend}
    labels: dict[str, str] = {}
    base = (internal_url or "").strip().rstrip("/")
    if backend in INTERNAL_PATHS and base:
        env[JEV_API_URL_ENV] = f"{base}/{INTERNAL_PATHS[backend]}/v1/decide"
        labels[JUDGE_POD_LABEL[0]] = JUDGE_POD_LABEL[1]
    return env, labels


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
