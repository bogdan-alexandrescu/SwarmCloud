"""A workspace's visual-QA settings: how many flows one live-browser step runs (QA-FLOWS-CAP, part of #2136).

BGD, 2026-10-26: a leg's flows run in BATCHES, several flows per live-browser
step (`qaspec.flow_batches`), the batch size chosen when a run starts
(`:run`'s `flows_per_step`) or, when the run names none, by the workspace's
default, `qa_flows_per_step`, held here. Neither set: 15
(`qaspec.FLOWS_PER_STEP_DEFAULT`). Bounds 1-30 (`qaspec.MIN_FLOWS_PER_STEP`,
`MAX_FLOWS_PER_STEP`, which say why).

WHERE IT LIVES. `workspace_settings/{tenant_id}`, field `qa_flows_per_step`,
beside the workspace's other settings (`judgment_backend`, `merge`,
`code_intelligence`), written with `merge=True` so it never touches theirs.
Every change writes one `admin_audit` entry in the same transaction; a write
of the value already stored writes nothing.

NOT YET MAC'D, AND WHY THAT IS BOUNDED. The settings MAC
(`judgment.SettingsMac`, docs/design/tenant-settings-integrity.md §3.4)
covers only the blocks the FROZEN `swarm_common.specsign.SETTINGS_MAC_FIELDS`
lists, and a `qa` block is not one of them: adding it is a contract change,
requested in this lane's PR rather than made. Until then a tenant worker that
writes this document directly can choose another workspace's batch size, so
the reader acts on a stored value only when it is an int in 1-30 and reads
anything else as unset (the default). The worst a forger achieves is a batch
size the workspace's own admin could have chosen; a run that names its own
`flows_per_step` is not affected at all.

WHO MAY CHANGE IT. The owner of a personal workspace, the workspace's
`admin`, or a platform admin; anyone else in the workspace is refused 403
`role_admin_required`, raised (a new route, never `refusals.refuse`'s switch),
and a caller outside it is answered 404, the same as a workspace that does not
exist (invariant 9). Nothing here takes or returns a credential, an image, a
command or a resource spec (invariant 10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from google.cloud import firestore
from pydantic import Field, StrictInt

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from . import qaspec
from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import NotFound, RoleAdminRequired
from .judgment import SETTINGS_COLLECTION
from .mergesettings import group_admin, is_member, is_owner
from .roles import ADMIN, RoleStore
from .schemas import StrictModel

COLLECTION = SETTINGS_COLLECTION
FIELD = "qa_flows_per_step"
SET_AT = "qa_flows_per_step_set_at"
SET_BY = "qa_flows_per_step_set_by"
AUDIT_ACTION = "qa_flows_per_step_set"
DEFAULT = qaspec.FLOWS_PER_STEP_DEFAULT

#: Where a run's batch size came from, as its `qa_target_runs` record says.
FROM_RUN = "run"
FROM_WORKSPACE = "workspace"
FROM_DEFAULT = "default"


class QaSettingsPut(StrictModel):
    #: StrictInt: "15" and true are refused, not read as 15 and 1.
    qa_flows_per_step: StrictInt = Field(ge=qaspec.MIN_FLOWS_PER_STEP, le=qaspec.MAX_FLOWS_PER_STEP)


def stored_flows_per_step(doc: Mapping[str, Any] | None) -> int | None:
    """The stored value when it is one this module could have written, else
    None: an int (never a bool) in 1-30. See the module's MAC note."""
    value = (doc or {}).get(FIELD)
    if isinstance(value, int) and not isinstance(value, bool) \
            and qaspec.MIN_FLOWS_PER_STEP <= value <= qaspec.MAX_FLOWS_PER_STEP:
        return value
    return None


def authorize_read(auth: AuthContext, tenant: Tenant | None, tenant_id: str) -> Tenant:
    """The workspace, when the caller is in it, owns it or is a platform
    admin; the established 404 otherwise, as for one that does not exist."""
    if tenant is None:
        raise NotFound(f"workspace {tenant_id!r} not found")
    if not (auth.is_admin or is_owner(auth, tenant) or is_member(auth, tenant_id)):
        raise NotFound(f"workspace {tenant_id!r} not found")
    return tenant


def can_edit(auth: AuthContext, tenant: Tenant, roles: RoleStore | None, *, fresh: bool = False) -> bool:
    """Owner, the group's `admin` (a role that cannot be read is "no") or a
    platform admin. `fresh` for the write, so a narrowing made a moment ago
    refuses it; the read's `can_edit` may use the cached role."""
    return bool(auth.is_admin) or is_owner(auth, tenant) or group_admin(roles, auth, tenant, fresh=fresh)


def authorize_write(auth: AuthContext, tenant: Tenant | None, tenant_id: str,
                    roles: RoleStore | None) -> Tenant:
    """The workspace, when the caller may change its QA settings; 404 for
    one they are not in, 403 for a member who is not its admin."""
    tenant = authorize_read(auth, tenant, tenant_id)
    if not can_edit(auth, tenant, roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its QA settings. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})
    return tenant


class QaSettingsStore:
    """`workspace_settings/{tenant}.qa_flows_per_step`, read and audited write."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def document(self, tenant_id: str) -> dict[str, Any]:
        snap = self._ref(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def flows_per_step(self, tenant_id: str) -> tuple[int, str]:
        """(the batch size a run that names none uses, where it came from)."""
        stored = stored_flows_per_step(self.document(tenant_id))
        return (stored, FROM_WORKSPACE) if stored is not None else (DEFAULT, FROM_DEFAULT)

    def put(self, tenant_id: str, value: int, *, by: str) -> None:
        """Store `value` and its audit in one transaction; the same value
        again writes nothing."""
        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            previous = stored_flows_per_step(snap.to_dict() if snap.exists else None)
            if previous == value:
                return
            txn.set(ref, {"tenant_id": tenant_id, FIELD: value, SET_AT: now, SET_BY: by}, merge=True)
            txn.set(audit, {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "from": previous if previous is not None else DEFAULT,
                           "to": value},
            })

        _write(transaction)


def view(tenant_id: str, doc: Mapping[str, Any], *, editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the value in force, its default and bounds
    (what the console's Workspace Settings draws), who set it and whether the
    caller may change it."""
    stored = stored_flows_per_step(doc)
    set_at = doc.get(SET_AT) if stored is not None else None
    set_by = doc.get(SET_BY) if stored is not None else None
    return {
        "tenant_id": tenant_id,
        FIELD: stored if stored is not None else DEFAULT,
        "defaults": {FIELD: DEFAULT},
        "bounds": {FIELD: {"min": qaspec.MIN_FLOWS_PER_STEP, "max": qaspec.MAX_FLOWS_PER_STEP}},
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else None,
        "set_by": set_by if isinstance(set_by, str) else None,
        "can_edit": editable,
    }


__all__ = [
    "AUDIT_ACTION",
    "COLLECTION",
    "DEFAULT",
    "FIELD",
    "FROM_DEFAULT",
    "FROM_RUN",
    "FROM_WORKSPACE",
    "QaSettingsPut",
    "QaSettingsStore",
    "authorize_read",
    "authorize_write",
    "can_edit",
    "stored_flows_per_step",
    "view",
]
