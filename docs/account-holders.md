# Who holds an account, and who held it

An account in the pool is a Claude subscription several agents run on at
once. The pool always counted them (`assigned`); since #379 parts 2 and 3
(design approved 2026-09-30) it can also say **which** agents, to the people
allowed to know, and what held the account over the last 90 days.

This page records why it is built the way it is, so the next change does not
quietly undo a constraint.

## The record is written in the hold's own transaction

Each assignment is a **hold** on the account document (`holds`, projected to
`assigned`). A hold now also carries the `task_id` and `attempt_id` the worker
named and `assigned_at`. All three are optional: a worker image older than the
change sends neither id, and every hold written before the change has none of
the three. `holds_from_firestore` reads both shapes, and a malformed stamp
reads as absent rather than dropping the hold, because the stamp describes
the hold and the count is what `choose()` relies on.

Each hold also has a record in `account_holds/{assignment_id}`:

| field | |
|---|---|
| `account_id`, `tenant_id`, `task_id`, `attempt_id`, `assigned_at` | copied from the hold |
| `released_at`, `end` | null while open; `end` is `released`, `unusable` or `expired` |
| `hold_expires_at` | the hold's own deadline |
| `expires_at` | the TTL field: `released_at + 90 days`, or `hold_expires_at + 90 days` while open |

The record is written **in the same Firestore transaction** that changes
`holds`: `acquire_hold` opens it, `release_hold` closes it, and `prune_holds`
closes every expired hold it drops. Acquire and release also drop expired
holds as they pass, and they close those records too. A record written after
the counter commits could disagree with the counter, and the History tab would
then show a span the counter never had, or miss one it did. Closing a record
writes the whole record again from the hold rather than merging onto it. That
way a hold from before the change still gets a complete record, and the
transaction never reads the record, so the account document is still the only
thing it reads.

An expired hold's `released_at` is the hold's deadline, not the moment the
sweep noticed it, so the span ends when the counter stopped counting it.

An open record still has an `expires_at`. If its hold is never closed (for
example, the account is removed while an agent is on it), the record still
ages out instead of keeping a task id forever.

## Terraform

`terraform/modules/firestore/indexes.tf` declares:

* `account-holds-account-assigned`: `(account_id ASC, assigned_at DESC)`.
  The history query is an equality on one field plus a range and order on
  another. Firestore serves that only from a composite index; without one the
  query fails outright.
* `google_firestore_field.account_holds_ttl`: a TTL on `expires_at`, with its
  single-field index exempted. It does **not** depend on `var.event_ttl_field`.
  That variable switches off the audit-trail TTLs, and switching it off must
  not make records that name tasks permanent.

`tests/terraform/firestore.tftest.hcl` asserts both.

## The broker serves everything, to the platform only

`GET /v1/accounts/{id}/holds` (live holds) and
`GET /v1/accounts/{id}/holds/history?from=&to=&cursor=&limit=` (records with
`assigned_at` in `[from, to)`, newest first) refuse any caller that is not a
platform service account. They name every tenant's tasks, and swarm-api is
their only caller. Expired holds are filtered with the same `is_expired` the
counter uses, so a killed worker is not served as a current holder in the
minutes before the sweep prunes it.

A history cursor is `<instant>|<n>`: the last `assigned_at` served and how
many rows at that instant were served. A cursor meaning "strictly before the
last instant" would drop every other span taken in the same instant.

The cursor and the window are bounded, because `n` sizes the Firestore read
(`limit + n + 1`): `n` above the page maximum (500), a non-ASCII-digit or
non-numeric `n`, and a `from`..`to` span longer than the 90-day retention are
each a 422, never a 500 and never a scan of the account's whole log. Paging on
(`assigned_at`, document id) would need no count, but the document id is the
assignment id, which authorises a release, and a cursor is handed to the
caller.

Neither route serves the assignment id, because it authorises a release.
Neither serves the secret name, either.

## swarm-api does all the tenant filtering

`GET /v1/accounts/{id}/holders` and `GET /v1/accounts/{id}/history` resolve
the caller's tenant through `tenant_for`, as every account route does, and
see the account only if the broker lists it for that tenant (owned or lent).
Any other caller gets a 404, the same answer an unknown id gets.
`swarm_api/accountholds.py` then shapes the response:

| viewer | own holds | other tenants' holds |
|---|---|---|
| any tenant | task link, attempt number, since when | — |
| owner of a lent account | (as above) | `N agents · <tenant>`; no task or attempt id |
| borrower | (as above) | a count only; no tenant name |

In the **history**, the same split holds span by span (owner decision
2026-10-01). A borrower is served only its own spans, in full, plus `others`:
how many other tenants' spans the page covered, with no time, outcome or
tenant. The owner of a lent account keeps each borrower's spans with times,
outcome and tenant name. A borrower's `next_cursor` is rebuilt from its own
last row on the page (swarm-api cuts the page there; the rows cut come back,
counted, on the next page), because the broker's cursor is the last row's
`assigned_at`, and that row can be another tenant's.

**A borrower's window is on a UTC hour grid.** swarm-api snaps a borrower's
`from` DOWN and `to` UP to whole UTC hours before asking the broker (an absent
`to` is the next whole hour, an absent `from` is seven days before `to`), and
the response echoes the snapped window. Owner and platform windows are not
snapped. Why: an exact `others` count per window let a borrower recover another
tenant's start time to the microsecond by halving windows until the count
flipped (re-review of #413, 2026-10-01). On the grid the finest question a
borrower can ask is "how many in this hour".

`others` is served only when the page is the whole snapped window: no cursor in,
none out, scan not limited. A page cut at the borrower's own row, or a
continuation page, counts rows bounded by an instant rather than by the grid,
and the difference of two such counts would reopen the same probe, so `others`
is omitted there. (The alternative, serving it over hour-aligned bounds on
continuation pages, would need the whole window walked; omitting leaks less.)

A borrower's cursor is accepted only if its instant is the `assigned_at` of one
of THIS tenant's own spans on the account, checked with one bounded broker
lookup; any other cursor is a 422 with no page read. Without that, `T|0` for any
T was a precision probe of its own. The scan for a first own row follows at most
`OWN_PAGE_SCAN_MAX` = 5 broker pages (about 500 Firestore reads per request);
past that the response says `scan_limited` and offers no cursor.
| admin (`?scope=platform`, `require_admin` first) | everything | everything |

**A task id is checked before anyone is shown it.** The task id on a hold is
whatever the worker sent. If the hold's own tenant does not own that task
(`Store.tasks_by_id(hold_tenant, …)`, which answers "missing" and "another
tenant's" the same way), the hold is served as `verified: false` **without**
the id to everyone but an admin. It might be another tenant's real task id.
The console shows it as *unverified*. A hold that named no task is
`recorded: false`, shown as *task not recorded*. These are two different
facts and are kept apart.

Nothing on this path logs a task id.

## The console

Each expanded account row has **Holding now (N)** and **History** tabs. Both
are closed by default and load only when clicked. N is the listing's own
`assigned`, so showing the count needs no extra read.

The History tab draws each span as a bar across the window. It prints the
current five-hour reading beside the bars, labelled as current. The broker
keeps one reading per window, not a time series, so there is no past
utilisation to draw the spans over. Drawing today's figure across the whole
window would show a measurement that was never taken.

## Rollout order

The broker's assign body is strict (`extra="forbid"`). A worker that sends
`task_id` to a broker older than this change gets a 422, treats it as
`BrokerUnavailable`, and falls back to the tenant secret. **Deploy the broker
before, or together with, the worker image.** `release.yml` applies both in
one terraform apply, but the two do not switch over at the same instant. In
the gap, a new worker runs on its tenant secret instead of the pool. The task
does not fail, and the gap ends as soon as the broker revision is serving.
