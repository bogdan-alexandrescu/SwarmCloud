"""A workspace's quality gates: whether its claude-code lanes run jev-belay and jev-enforce (JEV-LANES).

BGD, 2026-10-31: 25% of lane PRs were red on their first CI run (#642), and a
review's NOT_YET round costs a fix step and a re-review. With
`quality_gates.jev` on, the claude-code runner registers two Claude Code
hooks, both checked by TypeSafe's Jev with the workspace's TypeSafe key:
jev-belay (a Stop hook that blocks an unverified "done") and jev-enforce
(the final reply and every edit checked against the repository's CLAUDE.md
rule files). docs/jev-data-flow.md says what each sends.

WHERE IT LIVES. `workspace_settings/{tenant_id}`, field `quality_gates`, a
map holding `jev` (a bool), beside the workspace's other settings, written
with `merge=True` so it never touches theirs. Every change writes one
`admin_audit` entry in the same transaction; a write of the value already
stored writes nothing.

OFF BY DEFAULT, AND WHO MAY CHANGE IT. Like `code_intelligence.jev` (PR
1330), the switch decides that a lane's text leaves the platform for
TypeSafe, so only the owner of a personal workspace, the workspace's
`admin`, or a platform admin may change it; anyone else in the workspace is
refused 403 `role_admin_required`, and a caller outside it is answered 404,
the same as a workspace that does not exist (invariant 9). Nothing here takes
or returns a credential, an image, a command or a resource spec (invariant
10): the key stays where `provider_keys` put it, and the worker reads it.

NOT YET MAC'D, AND WHY THAT IS BOUNDED. The settings MAC covers only the
blocks the FROZEN `swarm_common.specsign.SETTINGS_MAC_FIELDS` lists, and
`quality_gates` is not one of them: adding it is a contract change, requested
in this lane's PR rather than made. Until then the reader acts only on
`jev is True` and reads anything else as off. A forged "on" can only send a
workspace's own lane text to TypeSafe under that workspace's OWN key, a key
its admin set for TypeSafe; a workspace with no key set is unaffected, and
nothing is sent anywhere else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from google.cloud import firestore
from pydantic import StrictBool

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from .admins import AUDIT_COLLECTION
from .judgment import SETTINGS_COLLECTION
from .auth import AuthContext
from .errors import RoleAdminRequired
from .qasettings import authorize_read, can_edit
from .roles import ADMIN, RoleStore
from .schemas import StrictModel

COLLECTION = SETTINGS_COLLECTION
FIELD = "quality_gates"
JEV = "jev"
SET_AT = "set_at"
SET_BY = "set_by"
AUDIT_ACTION = "workspace_quality_gates_set"
#: Off until the workspace's owner or admin turns it on (BGD, 2026-10-31).
DEFAULT_JEV = False


class QualityGatesPut(StrictModel):
    #: StrictBool: "true" and 1 are refused, not read as on.
    jev: StrictBool


def stored_jev(doc: Mapping[str, Any] | None) -> bool:
    """On only when the stored map says `jev: true`; anything else is off.
    See the module's MAC note."""
    block = (doc or {}).get(FIELD) if isinstance(doc, Mapping) else None
    return isinstance(block, Mapping) and block.get(JEV) is True


def authorize_write(auth: AuthContext, tenant: Tenant | None, tenant_id: str,
                    roles: RoleStore | None) -> Tenant:
    """The workspace, when the caller may change its quality gates: its
    owner, the group's admin (read fresh) or a platform admin. 404 for one
    they are not in, 403 for a member who is not its admin."""
    tenant = authorize_read(auth, tenant, tenant_id)
    if not can_edit(auth, tenant, roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its quality gates. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})
    return tenant


class QualityGatesStore:
    """`workspace_settings/{tenant}.quality_gates`, read and audited write."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def document(self, tenant_id: str) -> dict[str, Any]:
        snap = self._ref(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def put(self, tenant_id: str, jev: bool, *, by: str) -> None:
        """Store `jev` and its audit in one transaction; the same value again
        writes nothing."""
        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            previous = stored_jev(snap.to_dict() if snap.exists else None)
            if previous == jev:
                return
            txn.set(ref, {"tenant_id": tenant_id,
                          FIELD: {JEV: jev, SET_AT: now, SET_BY: by}}, merge=True)
            txn.set(audit, {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "setting": f"{FIELD}.{JEV}",
                           "from": previous, "to": jev},
            })

        _write(transaction)


def view(tenant_id: str, doc: Mapping[str, Any], *, editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the switch in force, its default, who set it
    and whether the caller may change it."""
    block = doc.get(FIELD) if isinstance(doc.get(FIELD), Mapping) else {}
    set_at = block.get(SET_AT)
    set_by = block.get(SET_BY)
    return {
        "tenant_id": tenant_id,
        FIELD: {JEV: stored_jev(doc)},
        "defaults": {FIELD: {JEV: DEFAULT_JEV}},
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else None,
        "set_by": set_by if isinstance(set_by, str) else None,
        "can_edit": editable,
    }


__all__ = [
    "AUDIT_ACTION",
    "COLLECTION",
    "DEFAULT_JEV",
    "FIELD",
    "JEV",
    "QualityGatesPut",
    "QualityGatesStore",
    "authorize_read",
    "authorize_write",
    "can_edit",
    "stored_jev",
    "view",
]
