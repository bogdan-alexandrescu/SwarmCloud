"""A tenant's failure findings, read side (docs/design/failure-review.md §1;
lane FR-4 FINDINGS of docs/design/agent-autonomy.md §6.2, part of #1933).

    GET /v1/failure-findings                  the caller's tenant's findings, newest first
    GET /v1/failure-findings?task_id=<id>     those about one failed agent (its detail page)
    GET /v1/failure-findings/{finding_id}     one finding

WHAT A FINDING IS. The `failure-review` schedule (lane FR-3,
`schedtypes/failure_review.py` `Filing.report`) writes one
`failure_findings/{id}` document, carrying `tenant_id`, for each failed agent
whose cause it found in that tenant's OWN repository: a cause this platform
does not fix, so it is told to the tenant instead of filed on GitHub with the
platform tenant's credential. Nothing here writes one.

TENANT ISOLATION (invariant 9). The tenant is `tenant_scope`'s, never a query
or a body. A finding of another tenant, one that does not exist, and (for a
continuation-scoped caller) one about an agent they did not submit all answer
the SAME 404 in the same words: a different answer would confirm that the id
exists somewhere, which is an enumeration oracle (`Store.get_task` keeps the
same rule).

NO PLATFORM-TENANT TEXT REACHES A TENANT. The stored document also holds what
the platform tenant's review knows and the tenant must not read: the cause's
`fingerprint` (the key of a `failure_causes` record in the platform tenant),
the platform's `schedule_id` and `firing_id`, and the investigator's proposed
`fix`, written in the platform tenant from a digest of every tenant's
failures. `tenant_view` serves an ALLOW-LIST -- what failed, its own
platform-written evidence, and the cause sentence -- so a field FR-3 adds
later stays in the platform tenant until someone adds it here on purpose. The
cause sentence is the investigator's, so the digest's opaque references to
OTHER failures (`f-002`) and to other tenants (`t2`) are replaced
before it is served (`tenant_text`), and it is redacted again.

PLAIN TEXT, NOT HTML. `Filing.report` stores the cause and the fix as written
(`plain`: masked, mentions broken, closing keywords turned into `refs`, but not
HTML-escaped), and this route serves the cause untouched: the console shows it
through React, which escapes it once itself, so a stored `&gt;` read '-&gt;'
(FAILURE-STORE-PLAIN, part of #2391; the owner's "Store plain", 2026-10-26). A
document written before keeps its entities: nothing migrates or unescapes them.

NOTHING HERE CREATES DEMAND (invariants 1-3): it reads documents only. No
caller names an image, a command, a resource spec or a profile (10).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

from fastapi import APIRouter, Depends, Query
from google.cloud.firestore_v1.base_query import FieldFilter

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import NotFound
from ..redaction import redact_detail
from ..schedtypes.failure_review import FINDINGS

router = APIRouter(prefix="/v1/failure-findings", tags=["failure-findings"])

#: The most findings one read serves: a bell drop-down and a detail page, not
#: a history. A tenant's findings are one per cause and failed agent.
SCAN = 200
#: The cause sentence as `Filing.report` bounds it (`plain(..., 2000)`).
CAUSE_CHARS = 2000
#: The evidence fields a finding may carry: what the platform wrote about the
#: tenant's OWN failure (`failure_review._evidence_of`). Nothing else passes.
EVIDENCE_FIELDS = ("kind", "state", "profile", "end_cause", "last_error")
#: `last_error` as the digest bounds it (`failuredigest.ERROR_BYTES`, 500).
ERROR_CHARS = 500

#: The digest's opaque references (`failuredigest`: `f-001` per failure, `t1`
#: per other tenant). In a tenant's cause sentence they name failures and
#: tenants the reader cannot see. The platform's own label, `platform`, is
#: left: it is an ordinary word in a cause sentence, and names no tenant.
_FAILURE_REF = re.compile(r"\bf-\d{3,}\b")
_TENANT_LABEL = re.compile(r"\bt\d+\b")


def tenant_text(value: Any, limit: int = CAUSE_CHARS) -> str | None:
    """Investigator-written text as a tenant may read it: one line, the digest's
    references to other failures and tenants replaced, redacted, bounded."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    text = _FAILURE_REF.sub("a failure", text)
    text = _TENANT_LABEL.sub("a tenant", text)
    return redact_detail(text, limit=limit) or None


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def tenant_view(doc: Mapping[str, Any]) -> dict[str, Any]:
    """One finding as its tenant reads it: the allow-list, never the document."""
    raw = doc.get("evidence")
    evidence = raw if isinstance(raw, Mapping) else {}
    served = {k: evidence.get(k) for k in EVIDENCE_FIELDS if k in evidence}
    if "last_error" in served:
        served["last_error"] = redact_detail(" ".join(str(served["last_error"] or "").split()),
                                             limit=ERROR_CHARS) or None
    return {
        "finding_id": doc.get("finding_id"),
        "kind": doc.get("kind"),
        "task_id": doc.get("task_id"),
        "failure_id": doc.get("failure_id"),
        "state": doc.get("state"),
        "cause": tenant_text(doc.get("cause")),
        "evidence": served,
        "created_at": _iso(doc.get("created_at")),
    }


def _created(doc: Mapping[str, Any]) -> str:
    return str(_iso(doc.get("created_at")) or "")


def tenant_findings(db: Any, tenant_id: str, *, task_id: str | None = None,
                    limit: int = SCAN) -> list[dict[str, Any]]:
    """This tenant's stored findings, newest first: the documents, tenant-checked
    again after the query, for a reader that projects them with `tenant_view`."""
    query = db.collection(FINDINGS).where(filter=FieldFilter("tenant_id", "==", tenant_id))
    if task_id is not None:
        query = query.where(filter=FieldFilter("task_id", "==", task_id))
    found = []
    for snap in query.limit(limit).stream():
        doc = snap.to_dict() or {}
        if doc.get("tenant_id") != tenant_id:
            continue
        if task_id is not None and doc.get("task_id") != task_id:
            continue
        found.append(doc)
    return sorted(found, key=_created, reverse=True)


def _mine(ctx: AppContext, auth: AuthContext, tenant_id: str,
          docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A continuation-scoped caller (`AuthContext.member_scope`) reads only the
    findings about agents they submitted, as every task read narrows them."""
    if not auth.member_scope:
        return docs
    tasks = ctx.store.tasks_by_id(tenant_id, [str(d.get("task_id") or "") for d in docs])
    email = auth.email.strip().lower()
    return [d for d in docs
            if d.get("task_id") in tasks
            and (tasks[d["task_id"]].submitted_by or "").strip().lower() == email]


@router.get("")
def list_failure_findings(
    task_id: str | None = Query(default=None, min_length=1, max_length=200),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    """`{findings: [...]}`, newest first: each `{finding_id, kind, task_id,
    failure_id, state, cause, evidence, created_at}`."""
    docs = _mine(ctx, auth, tenant_id, tenant_findings(ctx.db, tenant_id, task_id=task_id))
    return {"findings": [tenant_view(d) for d in docs]}


@router.get("/{finding_id}")
def get_failure_finding(
    finding_id: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    missing = NotFound(f"failure finding {finding_id!r} not found")
    snap = ctx.db.collection(FINDINGS).document(finding_id).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    if not doc or doc.get("tenant_id") != tenant_id or not _mine(ctx, auth, tenant_id, [doc]):
        raise missing
    return tenant_view(doc)
