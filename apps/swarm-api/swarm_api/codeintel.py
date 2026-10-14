"""A workspace's code-intelligence setting (CTXQ-SET).

docs/design/context-quality.md §4.7 (opt-in and staging) and §5.4 (what is
sent to Jev). Settings > Workspace > Code intelligence:

  * `jev`: whether Jev may ever see this workspace's code. OFF BY DEFAULT
    (owner decision, §4.7's table): nothing leaves the platform until the
    workspace's owner or a platform admin turns it on. A repository cannot
    opt itself in (`agent_worker.judge.settings`).
  * `context`: the workspace default for local graph context (the staged
    snapshot and CONTEXT.md), `on` or `off`. Unset is `inherit`: the
    platform default, `DEFAULT_CONTEXT`. Nothing it stages leaves the
    platform.
  * `context_ab`: §6.2's online A/B, `ab`, `on` or `off`. Unset is
    `inherit`: `DEFAULT_AB`, the experiment.

A repository's own `context` override (`inherit | on | off`) is kept on its
registration (`repositories.CONTEXT_FIELD`, set through `RepositoryPatch`)
and beats the workspace's `context`, as `effective_context` resolves it.

WHERE IT IS STORED. Owner decision 2026-10-14: every per-workspace setting
lives in ONE document, `workspace_settings/{tenant_id}`, one field per
feature -- JUDGE-SELECT's `judgment_backend` (`swarm_api.judgment`) and
TRAIN-WS's `merge` (`swarm_api.mergesettings`) are already there. This is
the map under `code_intelligence`, never a `tenant_settings` collection and
never a field of the frozen `swarm_common.models.Tenant`. A write changes
that one field and nothing else: `update` of the field when the document
exists (a field-level write, which is what a merge is for one top-level
field), `set` of a new document otherwise, both inside the transaction that
read it. Keys of the map this module does not own (CTXQ-J's
`jev_monthly_cap_tokens`) are carried over, never dropped.

The worker reads this document as it stands: `agent_worker.graphstage.decide`
the `context` and `context_ab` keys, `agent_worker.judge.settings` the `jev`
key. Their names and defaults are restated here because swarm-api does not
import the worker; tests/unit/control_plane/test_code_intelligence.py holds
the two equal.

WHO. Exactly as JUDGE-SELECT gates `judgment_backend`: the owner of a
personal workspace -- `kind: user`, its principal the caller's verified
email -- or a platform admin (`require_admin`, which answers 503 rather than
403 when the directory could not be asked). A member who is neither is 403
`code_intelligence_owner_required`. A caller who is not a member at all is
the established 404: another workspace's setting is neither served nor
confirmed to exist, and nothing a non-member sends is written (invariant 9).

AUDIT. Every change is ONE `admin_audit` entry -- action
`workspace_code_intelligence_set`, `by`, `at`, `detail.from`/`detail.to`
naming only what changed -- written in the same transaction as the change,
so the setting and its audit cannot disagree. A PUT that changes nothing
writes nothing.

INVARIANTS. A setting is a Firestore document: it holds no capacity and
creates no infrastructure demand (invariant 1). Nothing here accepts an
image, a command, a URL or a resource spec (invariant 10): the body is one
boolean and two words.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import StrictBool, model_validator

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from .admins import AUDIT_COLLECTION
from .auth import AuthContext, require_admin
from .errors import Forbidden, NotFound
from .judgment import SETTINGS_COLLECTION
from .mergesettings import is_member, is_owner
from .repositories import COLLECTION as REPOSITORIES
from .repositories import CONTEXT_INHERIT, context_of
from .schemas import StrictModel

#: The workspace's one settings document (owner decision 2026-10-14).
COLLECTION = SETTINGS_COLLECTION
FIELD = "code_intelligence"
JEV_FIELD = "jev"
#: `agent_worker.graphstage.CONTEXT_FIELD` and `AB_FIELD`.
CONTEXT_FIELD = "context"
AB_FIELD = "context_ab"

ON, OFF, AB, INHERIT = "on", "off", "ab", CONTEXT_INHERIT
CONTEXTS = (ON, OFF)
AB_MODES = (AB, ON, OFF)

#: Jev sees no code unless a workspace opts in (§4.7; §8 Q9: "Jev stays
#: per-workspace opt-in forever").
DEFAULT_JEV = False
#: `agent_worker.graphstage.DEFAULT_CONTEXT`: local context never leaves the
#: platform, so it is on unless a workspace or repository turns it off.
DEFAULT_CONTEXT = ON
#: `agent_worker.graphstage.DEFAULT_AB`: until §6.3's thresholds pass, a
#: workspace that set nothing is in the A/B.
DEFAULT_AB = AB

AUDIT_ACTION = "workspace_code_intelligence_set"


class OwnerRequired(Forbidden):
    """A member of the workspace who is neither its owner nor a platform admin."""

    code = "code_intelligence_owner_required"


@dataclass(frozen=True)
class CodeIntelligence:
    """The stored setting. `context` and `context_ab` None mean inherit."""

    jev: bool = DEFAULT_JEV
    context: str | None = None
    context_ab: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {JEV_FIELD: self.jev, CONTEXT_FIELD: self.context, AB_FIELD: self.context_ab}


def settings_of(block: Mapping[str, Any] | None) -> CodeIntelligence:
    """The setting a stored `code_intelligence` map says. Anything malformed
    is unset: `jev` is on only for a stored `True`, as the worker reads it."""
    block = block if isinstance(block, Mapping) else {}
    context = block.get(CONTEXT_FIELD)
    ab = block.get(AB_FIELD)
    return CodeIntelligence(
        jev=block.get(JEV_FIELD) is True,
        context=context if context in CONTEXTS else None,
        context_ab=ab if ab in AB_MODES else None,
    )


def effective_context(settings: CodeIntelligence, registration: Mapping[str, Any] | None) -> str:
    """`on` or `off` for one repository: its override, else the workspace's
    `context`, else the platform default -- `graphstage.decide`'s order,
    before its A/B arm."""
    return context_of(registration or {}) or settings.context or DEFAULT_CONTEXT


class CodeIntelligencePut(StrictModel):
    """PUT /v1/workspaces/{id}/code-intelligence. Every field optional; a
    field left out is unchanged, `inherit` clears, and a body that names
    nothing is refused."""

    jev: StrictBool | None = None
    context: Literal["on", "off", "inherit"] | None = None
    context_ab: Literal["ab", "on", "off", "inherit"] | None = None

    @model_validator(mode="after")
    def _names_something(self) -> "CodeIntelligencePut":
        if self.jev is None and self.context is None and self.context_ab is None:
            raise ValueError("name at least one of jev, context or context_ab")
        return self


# --------------------------------------------------------------------------
# who may read and change it
# --------------------------------------------------------------------------

def can_edit(auth: AuthContext, tenant: Tenant | None) -> bool:
    return bool(auth.is_admin) or (tenant is not None and is_owner(auth, tenant))


def authorize(auth: AuthContext, tenant: Tenant | None, tenant_id: str, *, write: bool) -> Tenant:
    """The workspace, when the caller may read it (and, `write`, change it).

    Not a member, not the owner, not an admin: the established 404, the same
    as a workspace that does not exist. A member who may not change it: 403
    (503 when the directory could not say whether they are an admin)."""
    if tenant is None:
        raise NotFound(f"workspace {tenant_id!r} not found")
    admin = bool(auth.is_admin)
    owner = is_owner(auth, tenant)
    if not (admin or owner or is_member(auth, tenant_id)):
        raise NotFound(f"workspace {tenant_id!r} not found")
    if write:
        require_owner_or_admin(auth, tenant)
    return tenant


def require_owner_or_admin(auth: AuthContext, tenant: Tenant | None) -> str:
    """The caller's email when they own `tenant` or are a platform admin.

    Also the gate on a repository's `context` override
    (routes/repositories.py): the workspace default and the override over it
    are the same person's decision."""
    if tenant is None or not is_owner(auth, tenant):
        try:
            require_admin(auth)
        except Forbidden:
            raise OwnerRequired(
                "Only the workspace's owner or a platform admin may change its code "
                "intelligence settings. Nothing was changed.",
                detail={"tenant_id": tenant.tenant_id if tenant is not None else None},
            ) from None
    return (auth.email or "").strip().lower()


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class CodeIntelligenceStore:
    """`workspace_settings/{tenant}.code_intelligence`, read and audited write."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def block(self, tenant_id: str) -> dict[str, Any] | None:
        """The stored `code_intelligence` map, or None."""
        snap = self._ref(tenant_id).get()
        value = (snap.to_dict() or {}).get(FIELD) if snap.exists else None
        return value if isinstance(value, dict) else None

    def get(self, tenant_id: str) -> CodeIntelligence:
        """The read CTXQ-S's and CTXQ-J's gates make, as swarm-api serves it."""
        return settings_of(self.block(tenant_id))

    def registrations(self, tenant_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """The tenant's registrations, by id; the filter again in the
        application, as `Repositories.list` does."""
        query = self._db.collection(REPOSITORIES).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)).limit(limit)
        rows = [snap.to_dict() or {} for snap in query.stream()]
        return sorted((r for r in rows if r.get("tenant_id") == tenant_id),
                      key=lambda r: str(r.get("repo_id")))

    def put(self, tenant_id: str, body: CodeIntelligencePut, *, by: str) -> CodeIntelligence:
        """Apply `body` and its audit in one transaction; returns the setting."""
        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        def _word(value: str | None, current: str | None) -> str | None:
            if value is None:
                return current
            return None if value == INHERIT else value

        @firestore.transactional
        def _apply(txn: Any) -> CodeIntelligence:
            snap = _snapshot(txn.get(ref))
            whole = (snap.to_dict() or {}) if snap.exists else None
            stored = (whole or {}).get(FIELD)
            stored = dict(stored) if isinstance(stored, dict) else {}
            before = settings_of(stored)
            after = CodeIntelligence(
                jev=body.jev if body.jev is not None else before.jev,
                context=_word(body.context, before.context),
                context_ab=_word(body.context_ab, before.context_ab),
            )
            old, new = before.as_dict(), after.as_dict()
            changed = [k for k in old if old[k] != new[k]]
            if not changed:
                return after
            block = {k: v for k, v in stored.items()
                     if k not in (JEV_FIELD, CONTEXT_FIELD, AB_FIELD)}
            block[JEV_FIELD] = after.jev
            if after.context is not None:
                block[CONTEXT_FIELD] = after.context
            if after.context_ab is not None:
                block[AB_FIELD] = after.context_ab
            block.update({"updated_at": now, "updated_by": by})
            if whole is None:
                txn.set(ref, {"tenant_id": tenant_id, FIELD: block})
            else:
                txn.update(ref, {FIELD: block})
            txn.set(audit, {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id,
                           "from": {k: old[k] for k in changed},
                           "to": {k: new[k] for k in changed}},
            })
            return after

        return _apply(transaction)


def view(tenant_id: str, settings: CodeIntelligence, block: Mapping[str, Any] | None,
         registrations: list[Mapping[str, Any]], *, editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the setting, its defaults, who may change it,
    and every registered repository's override and effective context."""
    block = block or {}
    updated = block.get("updated_at")
    return {
        "tenant_id": tenant_id,
        JEV_FIELD: settings.jev,
        CONTEXT_FIELD: settings.context or INHERIT,
        AB_FIELD: settings.context_ab or INHERIT,
        "defaults": {JEV_FIELD: DEFAULT_JEV, CONTEXT_FIELD: DEFAULT_CONTEXT,
                     AB_FIELD: DEFAULT_AB},
        "updated_at": updated.isoformat() if isinstance(updated, datetime) else None,
        "updated_by": block.get("updated_by") if isinstance(block.get("updated_by"), str)
        else None,
        "can_edit": editable,
        "repositories": [
            {
                "repo_id": r.get("repo_id"),
                "repository": f"{r.get('owner')}/{r.get('repo')}",
                "context": context_of(r) or INHERIT,
                "effective_context": effective_context(settings, r),
            }
            for r in registrations
        ],
    }
