"""The pull request a workflow's issue run opened, with its last CI reading
(PR #3901, the owner's pick "Serve checks with the workflow's pull request").

Home's workflow row draws the pull request's number with a checks dot. The
console never calls GitHub, and neither does this: the reading is the one the
issue run's CI loop already stored on the run (`issueci`, `pull_request.checks`
and `check_counts`, beside the `head_sha` it was read at). A workflow no issue
run started has no stored reading, and serves no `pull_request` block: Home
then draws the number alone, from the step results, as before.

ONE BOUNDED READ PER CALL. The runs are found by `workflow_id` with `in`
filters of at most `IN_CHUNK` ids, an equality-family filter the automatic
single-field index serves (no composite index). Tenant is checked again here:
a run of another tenant naming the same id is never served (invariant 9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

from .issueruns import RUNS_COLLECTION

#: Firestore's cap on an `in` filter's values.
IN_CHUNK = 30
#: `forgechecks`' states, as `issueci` stores them.
CHECK_STATES = frozenset({"green", "pending", "red", "none"})
#: The buckets `forgechecks.CiReading.counts` keeps (`issueruns.CHECK_COUNT_KEYS`).
COUNT_KEYS = ("passed", "failed", "pending", "skipped")


def _iso(moment: Any) -> str | None:
    if isinstance(moment, datetime):
        return moment.isoformat()
    return moment if isinstance(moment, str) and moment else None


def _counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, int] = {}
    for key in COUNT_KEYS:
        n = value.get(key)
        out[key] = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
    return out


def served_pull(pull: Any) -> dict[str, Any] | None:
    """The block served for a run's stored `pull_request`; None when it has
    no number. `checks` is null when no reading was stored -- never a state
    the CI loop did not read."""
    if not isinstance(pull, Mapping):
        return None
    number = pull.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    url = pull.get("url")
    checks = pull.get("checks")
    head = pull.get("head_sha")
    return {
        "number": number,
        "url": url if isinstance(url, str) and url.startswith(("https://", "http://")) else None,
        "head_sha": head if isinstance(head, str) and head else None,
        "checks": checks if checks in CHECK_STATES else None,
        "check_counts": _counts(pull.get("check_counts")),
        "checked_at": _iso(pull.get("checked_at")),
        "merged": pull.get("merged") if isinstance(pull.get("merged"), bool) else None,
    }


def _updated(doc: Mapping[str, Any]) -> str:
    return _iso(doc.get("updated_at")) or ""


def pull_requests(db: Any, tenant_id: str, workflow_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """`{workflow_id: served_pull(...)}` for each named workflow an issue run of
    `tenant_id` started and opened a pull request for; the newest run when
    more than one names it. Reads only."""
    wanted = list(dict.fromkeys(w for w in workflow_ids if isinstance(w, str) and w))
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for i in range(0, len(wanted), IN_CHUNK):
        chunk = wanted[i:i + IN_CHUNK]
        query = db.collection(RUNS_COLLECTION).where(filter=FieldFilter("workflow_id", "in", chunk))
        for snap in query.stream():
            doc = snap.to_dict() or {}
            workflow = doc.get("workflow_id")
            if doc.get("tenant_id") != tenant_id or workflow not in chunk:
                continue
            served = served_pull(doc.get("pull_request"))
            if served is None:
                continue
            at = _updated(doc)
            if workflow not in found or at > found[workflow][0]:
                found[workflow] = (at, served)
    return {workflow: served for workflow, (_, served) in found.items()}
