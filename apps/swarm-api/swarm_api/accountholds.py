"""Who holds an account, as each viewer may see it (#379 parts 2 and 3).

The quota broker serves every live hold on an account, and every recorded span,
with the tenant, task and attempt each worker named -- to a PLATFORM caller
only. This module is where that becomes something a human may be shown, and it
is the only place: the routes in `routes/accounts.py` call it and serve what it
returns, and nothing else in this service reads a hold.

WHAT EACH VIEWER GETS
---------------------
The viewer is decided from the caller's RESOLVED tenant and the account's
owner, never from the request:

  own holds        every viewer, on its own holds: the task (as a link), the
                   attempt number and since when.
  owner            on the OTHER tenants' holds of an account it lent: how many
                   per tenant -- "2 agents · research" -- and no task or
                   attempt id. The owner decided to lend; it may know to whom.
  borrower         on holds that are not its own: a COUNT and nothing else, no
                   tenant name. Borrowing an account does not entitle a tenant
                   to learn who else does. In the HISTORY the same: its own
                   spans in full, and every other tenant's collapsed into
                   `others`, with no time and no outcome (owner decision
                   2026-10-01). The owner of a lent account keeps per-span
                   detail on the borrowers: times, outcome and tenant.
  platform         an admin, behind `require_admin`: everything.

WHY A TASK ID IS VERIFIED BEFORE IT IS SHOWN
--------------------------------------------
The task and attempt on a hold are what the WORKER sent. The broker stamps
them as sent, and the worker's tenant comes from its identity token -- but the
task id is a string in a request body. A hold whose task the hold's own tenant
does not own is therefore served as `verified: false` and WITHOUT the id, to
everyone but the platform: it may be another tenant's real task id, and
printing it would hand that id to a tenant that does not own it. The check is
`Store.tasks_by_id(hold_tenant, ...)`, which answers "missing" and "another
tenant's" identically, so it is not an oracle either.

A hold with no task at all -- an older worker image, or a hold from before the
stamp existed -- is `recorded: false`, and the console says "task not
recorded". That is different from "unverified", and the two are kept apart.

WHAT IS NEVER SERVED
--------------------
The assignment id (it authorises a release), the secret name, and anything of
the credential. Every response here is built field by field from an
allow-list, so a field the broker adds tomorrow is not served by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal

Viewer = Literal["owner", "borrower", "platform"]

#: The three ways a recorded hold ends, as the broker writes them. Anything
#: else is served as no end at all rather than passed through.
_ENDS = ("released", "unusable", "expired")


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def viewer_of(account: dict[str, Any], tenant_id: str) -> Viewer:
    """`owner` when the caller's tenant owns the account, otherwise `borrower`.

    Only ever called on an account the broker listed for this tenant, which is
    the owned and lent-to set -- so "not the owner" means "a borrower".
    """
    return "owner" if account.get("owner_tenant") == tenant_id else "borrower"


@dataclass
class TaskCheck:
    """Whether a hold's tenant owns the task it names, and which attempt it is.

    Cached per request: a page of spans names the same task many times, and
    each answer is a Firestore read.
    """

    tasks_by_id: Callable[[str, Iterable[str]], dict[str, Any]]
    list_attempts: Callable[..., list[Any]]
    _owned: dict[tuple[str, str], bool] = field(default_factory=dict)
    _attempts: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def prime(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Check many (tenant, task) pairs in one batched read per tenant."""
        wanted: dict[str, set[str]] = {}
        for tenant, task in pairs:
            if tenant and task and (tenant, task) not in self._owned:
                wanted.setdefault(tenant, set()).add(task)
        for tenant, tasks in wanted.items():
            found = self.tasks_by_id(tenant, sorted(tasks))
            for task in tasks:
                self._owned[(tenant, task)] = task in found

    def owns(self, tenant: str, task: str) -> bool:
        if not tenant or not task:
            return False
        if (tenant, task) not in self._owned:
            self.prime([(tenant, task)])
        return self._owned[(tenant, task)]

    def attempt_number(self, tenant: str, task: str, attempt_id: str | None) -> int | None:
        """1 for the task's first attempt. None when the attempt is not one of its own."""
        if not attempt_id or not self.owns(tenant, task):
            return None
        key = (tenant, task)
        if key not in self._attempts:
            rows = self.list_attempts(tenant, task)
            # Newest first from the store; the number counts from the oldest.
            ordered = sorted(rows, key=lambda a: a.created_at)
            self._attempts[key] = [a.attempt_id for a in ordered]
        ids = self._attempts[key]
        return ids.index(attempt_id) + 1 if attempt_id in ids else None


def _work(
    entry: dict[str, Any], *, tenant: str, check: TaskCheck, show_unverified: bool
) -> dict[str, Any]:
    """The task, attempt and verification fields for a hold whose work may be shown."""
    task = _text(entry.get("task_id"))
    verified = bool(task) and check.owns(tenant, task)
    out: dict[str, Any] = {"recorded": bool(task), "verified": verified}
    if task and (verified or show_unverified):
        out["task_id"] = task
        out["attempt"] = check.attempt_number(tenant, task, _text(entry.get("attempt_id")))
    return out


def holders_view(
    payload: dict[str, Any],
    *,
    viewer: Viewer,
    tenant_id: str | None,
    check: TaskCheck,
    now: datetime,
) -> dict[str, Any]:
    """`GET /v1/accounts/{id}/holders` for one viewer.

    EXPIRED HOLDS ARE NEVER SERVED. The broker filters them already; this
    filters again on the served deadline, so a broker clock or a cached answer
    cannot put a killed worker back on the account.
    """
    live = []
    for raw in payload.get("holds") or []:
        if not isinstance(raw, dict):
            continue
        expires = _instant(raw.get("expires_at"))
        if expires is None or now >= expires:
            continue
        live.append(raw)

    if viewer == "platform":
        check.prime((str(h.get("tenant_id") or ""), _text(h.get("task_id")) or "") for h in live)
    else:
        check.prime((tenant_id or "", _text(h.get("task_id")) or "")
                    for h in live if h.get("tenant_id") == tenant_id)

    holders: list[dict[str, Any]] = []
    by_tenant: dict[str, int] = {}
    others = 0
    for h in live:
        tenant = str(h.get("tenant_id") or "")
        since = _instant(h.get("assigned_at"))
        if viewer == "platform" or tenant == tenant_id:
            entry: dict[str, Any] = {"since": since.isoformat() if since else None}
            if viewer == "platform":
                entry["tenant"] = tenant
            entry.update(_work(h, tenant=tenant, check=check,
                               show_unverified=viewer == "platform"))
            holders.append(entry)
            continue
        others += 1
        if viewer == "owner":
            by_tenant[tenant] = by_tenant.get(tenant, 0) + 1

    holders.sort(key=lambda e: e["since"] or "")
    out: dict[str, Any] = {
        "account_id": str(payload.get("account_id") or ""),
        "viewer": viewer,
        "total": len(live),
        "holders": holders,
        "others": others,
    }
    if viewer == "owner":
        out["by_tenant"] = [{"tenant": t, "n": n} for t, n in sorted(by_tenant.items())]
    return out


def history_view(
    payload: dict[str, Any],
    *,
    viewer: Viewer,
    tenant_id: str | None,
    check: TaskCheck,
    now: datetime,
    continued: bool = False,
) -> dict[str, Any]:
    """`GET /v1/accounts/{id}/history` for one viewer: spans, newest first.

    A borrower's `others` is served only when the page IS the whole hour-aligned
    window: no cursor in (`continued` False), none out, and the scan not
    limited. On any other page the count is bounded by a row's own instant (the
    page was cut at the borrower's own row) rather than by the hour grid, and
    differencing two such counts reads when another tenant held the account
    more finely than an hour. Omitting it leaks less than serving it.

    An OPEN record whose hold has passed its deadline is served as ended
    `expired` at that deadline -- the counter stopped including it then, and
    the sweep that will close the record is minutes away. Serving it as still
    running would be the one place a killed worker is shown as alive.
    """
    rows = [r for r in payload.get("spans") or [] if isinstance(r, dict)]
    if viewer == "platform":
        check.prime((str(r.get("tenant_id") or ""), _text(r.get("task_id")) or "") for r in rows)
    else:
        check.prime((tenant_id or "", _text(r.get("task_id")) or "")
                    for r in rows if r.get("tenant_id") == tenant_id)

    spans: list[dict[str, Any]] = []
    others = 0
    for r in rows:
        tenant = str(r.get("tenant_id") or "")
        if viewer == "borrower" and tenant != tenant_id:
            # Counted, and nothing else of it is read: not its times, not its
            # outcome, not its tenant.
            others += 1
            continue
        since = _instant(r.get("assigned_at"))
        until = _instant(r.get("released_at"))
        end = r.get("end") if r.get("end") in _ENDS else None
        if end is None:
            lapses = _instant(r.get("hold_expires_at"))
            if lapses is not None and now >= lapses:
                until, end = lapses, "expired"
        mine = tenant == tenant_id
        span: dict[str, Any] = {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "end": end,
            "mine": mine,
        }
        if viewer == "platform":
            span["tenant"] = tenant
            span.update(_work(r, tenant=tenant, check=check, show_unverified=True))
        elif mine:
            span.update(_work(r, tenant=tenant, check=check, show_unverified=False))
        elif viewer == "owner":
            span["tenant"] = tenant
        spans.append(span)

    cursor = payload.get("next_cursor")
    out: dict[str, Any] = {
        "account_id": str(payload.get("account_id") or ""),
        "viewer": viewer,
        "from": _text(payload.get("from")),
        "to": _text(payload.get("to")),
        "spans": spans,
        "next_cursor": cursor if isinstance(cursor, str) and cursor else None,
    }
    if viewer == "borrower":
        if payload.get("scan_limited"):
            out["scan_limited"] = True
        elif not continued and out["next_cursor"] is None:
            out["others"] = others
    return out


#: How many broker pages a borrower's read may walk to find one of its own rows.
#: Five pages of at most 100 rows each is about 500 Firestore reads per
#: borrower request at most (owner decision 2026-10-01); it was 20, which let
#: one borrower request cost about 2,000.
OWN_PAGE_SCAN_MAX = 5


def _cursor_skip(cursor: str | None) -> tuple[datetime | None, int]:
    """The (instant, skip) of a cursor this service minted, or (None, 0)."""
    if not cursor:
        return None, 0
    instant, _, count = cursor.rpartition("|")
    return (_instant(instant), int(count)) if count.isdigit() else (None, 0)


def own_page(
    payload: dict[str, Any],
    *,
    tenant_id: str | None,
    cursor: str | None,
    fetch: Callable[[str | None], dict[str, Any]],
) -> dict[str, Any]:
    """A borrower's page: it ends at a row of its own, so its cursor can.

    The broker's cursor is the instant of the LAST row on the page, and when
    that row is another tenant's the cursor is that tenant's `assigned_at` --
    a timestamp a borrower is not shown. So the page is cut after the
    borrower's last own row and the cursor is rebuilt from THAT row, which the
    borrower may know. The rows cut are served again, counted, on the next
    page. A page with no row of the borrower's at all is followed (at most
    `OWN_PAGE_SCAN_MAX` pages) until one has, because there is no cursor to hand
    back otherwise; past that bound the scan stops, says so, and offers none.
    """
    rows = [r for r in payload.get("spans") or [] if isinstance(r, dict)]
    broker_cursor = payload.get("next_cursor")
    more = isinstance(broker_cursor, str) and bool(broker_cursor)
    pages = 1

    def mine(r: dict[str, Any]) -> bool:
        return r.get("tenant_id") == tenant_id

    while more and not any(mine(r) for r in rows) and pages < OWN_PAGE_SCAN_MAX:
        page = fetch(broker_cursor)
        rows += [r for r in page.get("spans") or [] if isinstance(r, dict)]
        broker_cursor = page.get("next_cursor")
        more = isinstance(broker_cursor, str) and bool(broker_cursor)
        pages += 1

    served = dict(payload)
    if not more:
        served["spans"] = rows
        served["next_cursor"] = None
        return served
    last = max((i for i, r in enumerate(rows) if mine(r)), default=None)
    if last is None:
        served["spans"] = rows
        served["next_cursor"] = None
        served["scan_limited"] = True
        return served
    kept = rows[: last + 1]
    stamp = kept[-1].get("assigned_at")
    at = _instant(stamp)
    n = sum(1 for r in kept if _instant(r.get("assigned_at")) == at)
    from_at, from_skip = _cursor_skip(cursor)
    if from_at is not None and from_at == at:
        n += from_skip
    served["spans"] = kept
    served["next_cursor"] = f"{stamp}|{n}" if isinstance(stamp, str) and stamp else None
    return served


__all__ = ["TaskCheck", "Viewer", "history_view", "holders_view", "own_page", "viewer_of"]
