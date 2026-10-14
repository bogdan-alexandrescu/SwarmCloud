"""Which backend a workspace's Jev judgments go to (JUDGE-SELECT).

Owner decisions 2026-10-14. Jev (jev-browser, on the live browser) asks typed
questions about page state. Its answers can come from three backends, chosen
per workspace:

  * `internal-jev` -- THE DEFAULT. Saga's own decision API on the company
    tailnet (`/jev`), reached through the ONE Tailscale egress Service the
    operator runs in swarm-system (JUDGE-NET). Page text stays inside Saga,
    and no TypeSafe key is needed or sent.
  * `internal-laya` -- the same API's `/laya` path.
  * `typesafe` -- api.typesafe.ai, jev-browser's own default. Page text goes
    to TypeSafe, and a TypeSafe key must resolve (`provider_keys`).

WHERE THE CHOICE LIVES. `workspace_settings/{tenant_id}`, field
`judgment_backend`, beside nothing else yet: no per-workspace settings store
existed on main when this was written (the brief named TRAIN-WS and the
merge settings; neither stores per-workspace documents here), and the frozen
`Tenant` has no field for it. A document of its own rather than a field
written into `tenants/` keeps every reader of the frozen type untouched.
Absent means the default. Every change writes an `admin_audit` entry in the
same transaction.

WHO MAY CHANGE IT. The owner of a personal workspace (`kind: user`, its
principal the caller's verified email) or a platform admin. A group has no
per-tenant admin role to ask instead, exactly as for a tenant TypeSafe key.

AVAILABILITY never changes the choice and never falls back. An internal
backend is available when the platform renders INTERNAL_DECISION_API_URL
(the tailnet egress is enabled); TypeSafe when a TypeSafe key resolves for
the caller in that workspace. A chosen backend that is unavailable means Jev
is unavailable -- the dispatcher and the jev wrapper refuse rather than send
page text to TypeSafe in place of an internal backend.

`scheduler.judgment` restates the backend names, the collection and the
service address for the dispatcher (the scheduler does not import swarm-api);
tests/unit/control_plane/test_dispatch_judgment.py holds the two equal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from google.cloud import firestore

from swarm_common.admission import _snapshot

from .admins import AUDIT_COLLECTION
from .errors import ValidationFailed

INTERNAL_JEV = "internal-jev"
INTERNAL_LAYA = "internal-laya"
TYPESAFE_BACKEND = "typesafe"
#: In the order the console offers them.
BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA, TYPESAFE_BACKEND)
#: Owner decision 2026-10-14: page text stays inside Saga unless a workspace
#: chooses otherwise.
DEFAULT_BACKEND = INTERNAL_JEV
INTERNAL_BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA)

#: What the Submit picker and the browser strip show.
LABELS = {
    INTERNAL_JEV: "Jev (Internal)",
    INTERNAL_LAYA: "Jev (Internal Laya)",
    TYPESAFE_BACKEND: "Jev (TypeSafe)",
}

#: One document per workspace, keyed by tenant id.
SETTINGS_COLLECTION = "workspace_settings"
SETTING_FIELD = "judgment_backend"
AUDIT_JUDGMENT_SET = "judgment_backend_set"

#: The Tailscale egress Service JUDGE-NET runs in swarm-system, as a pod sees
#: it. The only value INTERNAL_DECISION_API_URL may hold besides "" (off):
#: the jev wrapper accepts exactly this host, so a second spelling here would
#: be one the wrapper refuses.
INTERNAL_DECISION_SERVICE = "http://decision-api.swarm-system.svc.cluster.local"

NO_EGRESS = ("the tailnet egress to the decision API is not enabled on this platform "
             "(INTERNAL_DECISION_API_URL is unset)")
NO_KEY = ("no TypeSafe key resolves for you in this workspace: set your own key, "
          "or ask an admin to set a tenant key")


def check_backend(value: str) -> str:
    name = (value or "").strip().lower()
    if name not in BACKENDS:
        raise ValidationFailed(f"unknown judgment backend {value!r}",
                               detail={"judgment_backends": list(BACKENDS)})
    return name


def stored_backend(doc: Mapping[str, Any] | None) -> str:
    """The stored choice, or the default for nothing or anything unknown."""
    value = (doc or {}).get(SETTING_FIELD)
    return value if isinstance(value, str) and value in BACKENDS else DEFAULT_BACKEND


def availability(*, internal_enabled: bool,
                 typesafe_key: bool) -> dict[str, tuple[bool, str | None]]:
    """Each backend -> (available, why not). Pure, so the matrix is a test."""
    internal = (True, None) if internal_enabled else (False, NO_EGRESS)
    return {
        INTERNAL_JEV: internal,
        INTERNAL_LAYA: internal,
        TYPESAFE_BACKEND: (True, None) if typesafe_key else (False, NO_KEY),
    }


def jev_status(backend: str, *, internal_enabled: bool, typesafe_key: bool,
               set_at: Any = None, set_by: str | None = None) -> dict[str, Any]:
    """The `jev` object: the chosen backend, its label, whether it can run
    and why not, the default, who chose it, and every option.

    `backend`, `label`, `available` and `reason` are the keys the shared
    `browser.jev` object (BSTATUS-API) carries for the picker and the strip.
    """
    table = availability(internal_enabled=internal_enabled, typesafe_key=typesafe_key)
    available, reason = table[backend]
    return {
        "backend": backend,
        "label": LABELS[backend],
        "available": available,
        "reason": reason,
        "default": DEFAULT_BACKEND,
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else set_at,
        "set_by": set_by,
        "options": [
            {"backend": b, "label": LABELS[b], "available": table[b][0], "reason": table[b][1]}
            for b in BACKENDS
        ],
    }


class JudgmentSettings:
    """`workspace_settings/{tenant}.judgment_backend`, read and audited write."""

    def __init__(self, db: Any, *, now: Any) -> None:
        self._db = db
        self._now = now

    def read(self, tenant_id: str) -> dict[str, Any]:
        snap = self._db.collection(SETTINGS_COLLECTION).document(tenant_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
        return {
            "judgment_backend": stored_backend(doc),
            "set_at": doc.get("judgment_backend_set_at"),
            "set_by": doc.get("judgment_backend_set_by"),
        }

    def set(self, tenant_id: str, backend: str, *, by: str) -> dict[str, Any]:
        """Store `backend` with its `admin_audit` entry, in one transaction."""
        backend = check_backend(backend)
        ref = self._db.collection(SETTINGS_COLLECTION).document(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            previous = stored_backend(snap.to_dict() if snap.exists else None)
            txn.set(ref, {
                "tenant_id": tenant_id,
                SETTING_FIELD: backend,
                "judgment_backend_set_at": now,
                "judgment_backend_set_by": by,
            }, merge=True)
            txn.set(audit, {
                "action": AUDIT_JUDGMENT_SET,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "judgment_backend": backend,
                           "previous": previous},
            })

        _write(transaction)
        return {"judgment_backend": backend, "set_at": now, "set_by": by}
