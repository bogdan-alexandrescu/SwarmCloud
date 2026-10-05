"""The reconciler's stalled workflows, as one tenant may read them (#616).

The reconciler's workflow stall check runs on its existing pass and persists
what it found on the pass document: `stalled_workflows` (one row per finding,
worst first) and `workflow_check` (what it examined, and `read_error` when it
could not read the workflows at all). See `reconciler.detect.
detect_stalled_workflows`. This module serves the LATEST pass's rows to the
workflow list route, which is the read the console's Overview "Needs a look"
and `sc trouble` take their Workflows findings from.

THREE RULES, each with the failure it prevents:

  * ONLY THE CALLER'S TENANT. A pass covers every tenant, so the rows are
    filtered by `tenant_id` before anything is served -- another tenant's
    workflow id is not served, not even counted (invariant 9).
  * AN UNKNOWN IS NOT A ZERO. No pass recorded, a pass from a reconciler
    older than the check, a check that could not read the workflows, or a
    read of the passes that failed here: each serves `count: null` and says
    which in `check_error`. A `count: 0` over any of them would be the
    reassuring silence this check exists to end.
  * THE ORDER IS THE RECONCILER'S. It sorts worst first once
    (`detect._STALL_ORDER`); this keeps it, so no surface restates it.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: The reconciler's pass collection (`reconciler.store.ControlStore.record_pass`).
PASSES = "reconciler_passes"

#: Rows served at most. A pass keeps every row, but a tenant with more than
#: this many stalled workflows has a platform problem one more row will not
#: explain; `truncated` says the list was cut.
MAX_ROWS = 50


def _descending() -> Any:
    from google.cloud import firestore

    return firestore.Query.DESCENDING


def stalled_workflows(db: Any, tenant_id: str) -> dict[str, Any]:
    """The caller's rows from the newest pass, and how far to trust them.

    Never raises: the workflow list must answer even when this read cannot,
    and the failure is served as `check_error` rather than swallowed.
    """
    try:
        docs = list(
            db.collection(PASSES)
            .order_by("started_at", direction=_descending())
            .limit(1)
            .stream()
        )
    except Exception as exc:
        log.warning("could not read the reconciler's latest pass: %s", exc)
        return _unknown(f"the reconciler's latest pass could not be read: {exc}"[:500])
    if not docs:
        return _unknown("no reconciliation pass has been recorded, so no workflow has been checked")
    latest = docs[0].to_dict() or {}
    pass_at = latest.get("started_at")
    check = latest.get("workflow_check")
    if not isinstance(check, dict):
        return _unknown(
            "the latest reconciliation pass did not run the workflow stall check "
            "(a reconciler older than #616)",
            pass_at=pass_at,
        )
    if check.get("read_error"):
        return _unknown(
            f"the reconciler's workflow stall check could not read the workflows: "
            f"{check['read_error']}",
            pass_at=pass_at,
        )
    rows = [
        dict(entry)
        for entry in (latest.get("stalled_workflows") or [])
        if isinstance(entry, dict) and entry.get("tenant_id") == tenant_id
    ]
    return {
        "count": len(rows),
        "workflows": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
        # The pass examined a bounded number of workflows platform-wide; when
        # it stopped at that bound, a zero here is a zero over what it read.
        "scan_truncated": bool(check.get("truncated")),
        "pass_at": pass_at,
        "check_error": None,
    }


def _unknown(why: str, *, pass_at: Any = None) -> dict[str, Any]:
    return {
        "count": None,
        "workflows": [],
        "truncated": False,
        "scan_truncated": False,
        "pass_at": pass_at,
        "check_error": why,
    }
