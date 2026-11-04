"""The work contract's evidence store: one document per proof record.

docs/design/agent-quality/jev-contract.md sections 5.4 and 9 (lane
CONTRACT-VERDICT-ROUTE, part of #2688). `workcontract.unmet` reads
`Evidence(requirement_id, contract_digest, commit, kind, passed)`; this is
where those records live, with what the console's approval screen shows
beside each (the verdict, the reason code, the cited item and its path, the
probability, the Jev call id), labelled by its `source`.

ONE WRITER. swarm-api writes every document here, inside the transaction
that checked the fence (`attemptstate.AttemptState.contract_verdict`) or the
re-ask's claim (`contractreask`); nothing a worker or an agent sends is
written but the closed values those checks let through. A document's id is
derived from what it binds (tenant, task, digest, commit, requirement,
kind), so a repeated write lands on the same document and evidence for
another revision or another commit is a different document that `unmet`
already ignores.

Pure but for `read_evidence`: no network, no clock.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

from . import contractjudge as cj
from . import workcontract as wc

COLLECTION = "contract_evidence"

#: Who produced a record: the worker's post-step through the state route, or
#: the control plane's re-ask from the published diff (the owner's answer 5).
SOURCE_WORKER = "worker"
SOURCE_REASK = "reask"
SOURCES = (SOURCE_WORKER, SOURCE_REASK)

#: Shown beside the verdict on the approval screen, never parsed.
LABEL = "Jev's judgment"


def evidence_id(tenant_id: str, task_id: str, digest: str, commit: str, requirement_id: str,
                kind: str = wc.VERIFICATION_JUDGMENT) -> str:
    """The document id for one record: a digest of what it binds."""
    raw = "\0".join((tenant_id, task_id, digest, commit, requirement_id, kind)).encode()
    return "ev_" + hashlib.sha256(raw).hexdigest()[:40]


def evidence_document(record: wc.Evidence, verdict: cj.Verdict, *, tenant_id: str,
                      task_id: str, contract: wc.WorkContract, attempt_id: str,
                      generation: int, call_id: str | None, source: str,
                      path: str | None, state_sha256: str | None,
                      at: datetime) -> dict[str, Any]:
    """The stored form of one `judgment` record and the verdict behind it."""
    if source not in SOURCES:
        raise ValueError("an evidence source is the worker or the re-ask")
    return {
        "tenant_id": tenant_id,
        "task_id": task_id,
        "contract_id": contract.contract_id,
        "revision": contract.revision,
        "requirement_id": record.requirement_id,
        "contract_digest": record.contract_digest,
        "commit": record.commit,
        "kind": record.kind,
        "passed": record.passed,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "evidence_item": verdict.evidence_item,
        "path": path,
        "p": verdict.p,
        "call_id": call_id,
        "source": source,
        "label": LABEL,
        "attempt_id": attempt_id,
        "generation": generation,
        "state_sha256": state_sha256,
        "at": at,
    }


def writes(db: Any, contract: wc.WorkContract, verdicts: Iterable[cj.Verdict], *,
           tenant_id: str, task_id: str, commit: str, attempt_id: str, generation: int,
           call_id: str | None, source: str, paths: Mapping[str, str],
           state_sha256: str | None, at: datetime) -> list[tuple[Any, dict[str, Any]]]:
    """(ref, document) for every record the verdicts write: passed for a
    `met`, failed for an `unmet`, nothing for a `cannot_judge`
    (`contractjudge.judgment_evidence`). `paths` maps a cited `i<n>` item to
    the changed path it names, for the approval screen."""
    by_id = {v.requirement_id: v for v in verdicts}
    out = []
    for record in cj.judgment_evidence(contract, by_id.values(), commit=commit):
        verdict = by_id[record.requirement_id]
        ref = db.collection(COLLECTION).document(evidence_id(
            tenant_id, task_id, record.contract_digest, commit, record.requirement_id))
        out.append((ref, evidence_document(
            record, verdict, tenant_id=tenant_id, task_id=task_id, contract=contract,
            attempt_id=attempt_id, generation=generation, call_id=call_id, source=source,
            path=paths.get(verdict.evidence_item or ""), state_sha256=state_sha256, at=at)))
    return out


def to_evidence(doc: Mapping[str, Any]) -> wc.Evidence | None:
    """A stored document as `workcontract.Evidence`, or None when it is not one."""
    fields = ("requirement_id", "contract_digest", "commit", "kind")
    if not all(isinstance(doc.get(f), str) for f in fields) or not isinstance(
            doc.get("passed"), bool):
        return None
    return wc.Evidence(requirement_id=doc["requirement_id"],
                       contract_digest=doc["contract_digest"], commit=doc["commit"],
                       kind=doc["kind"], passed=doc["passed"])


def read_evidence(db: Any, tenant_id: str, task_id: str) -> tuple[wc.Evidence, ...]:
    """Every record of one task, of its tenant only (invariant 9). `unmet`
    keeps those of the digest and the commit it is asked about."""
    query = (db.collection(COLLECTION)
             .where(filter=FieldFilter("task_id", "==", task_id))
             .where(filter=FieldFilter("tenant_id", "==", tenant_id)))
    out = []
    for snap in query.stream():
        record = to_evidence(snap.to_dict() or {})
        if record is not None:
            out.append(record)
    return tuple(out)
