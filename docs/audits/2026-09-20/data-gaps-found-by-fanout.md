# Data gaps found while drafting the P1/P2 screens

Found 2026-09-20 by a parallel drafting run over three screens. The code that
run produced was discarded — three drafts, ~31,000 characters each, none of
which compiled — but its *findings about the data* were correct and are worth
keeping. They are the reason those screens cannot say certain things.

Recorded so the next person does not rediscover them, and so nobody
"fixes" a screen by adding a number the platform cannot support.

---

## 1. `list_leases` gives no truncation signal, and the drift check needs one

`Store.list_leases` takes the newest `limit` lease documents and only THEN
drops released ones client-side. So a page can come back short for two
completely different reasons — there genuinely are few unreleased leases, or
the window filled with released ones — and nothing in the response
distinguishes them.

This matters specifically for the accounting-drift check, which compares
`sum(lease.units)` against `pool.active`. `pool.active` counts every tenant
and every lease; the lease sum counts one page. A page that silently
truncated produces a delta that looks exactly like a leaked lease.

**Consequence:** the drift check must label its lease side as "over the rows
returned", and must not present a delta as evidence of a leak on its own.

**Fix, when someone wants it:** return the unfiltered count alongside the
rows, or filter `released_at == null` server-side with the composite index
that would require.

## 2. `GET /v1/admin/quota` returns no `generated_at`

Every other read on this platform carries one — `/v1/capacity`, `/v1/stats`
and `/v1/providers` all do. The quota route does not, so a panel built on it
cannot show the server's own idea of freshness and has to fall back to the
client's receipt time.

That is the weaker signal: it says when the bytes arrived, not when the
platform computed them.

## 3. A client-side `RESOURCE_SIZING` table would drift from the frozen catalogue

The in-flight vCPU/GiB figures need `cpu` and `memory_gib` per resource class.
Those live in `RESOURCE_CLASSES` (`apps/common/swarm_common/profiles.py`),
which is frozen contract data — and there is no API route that exposes them.

Copying them into TypeScript is exactly the restatement drift
`scripts/lib/check-contract-parity.sh` exists to catch for shell and jq. The
parity checker does not cover TypeScript, so the copy would drift silently.

**Consequence:** either the API exposes the catalogue, or the screen reports
units only and says so. Do not hand-copy the sizes.

## 4. Resource class cannot be inferred from `units`

Inverting `units` (1, 2, 4) to recover the class happens to work today
because the three weights are distinct. It is a guess that silently picks the
wrong class the first time two classes share a weight, and a wrong class
means a wrong vCPU figure.

The class is available honestly: `pool_names_for` writes a `resource:<class>`
pool onto every lease, and `lease.pools` is returned.

## 5. `/v1/stats` has no time predicate

`count_tasks_by_state` filters on state and tenant only. So the platform
counts are "how many documents are in each state right now", never "how many
in the last hour". Any chart over time built on this endpoint would be
inventing its x-axis.

## 6. A provider with runner profiles but no quota document is invisible

`/v1/admin/quota` lists quota DOCUMENTS, not providers. A provider that no
tenant has ever driven has no document and simply does not appear — which
reads as "no such provider" rather than "never used".

`/v1/providers` derives its list from `RUNNER_PROFILES` and so does not have
this problem, but it is tenant-scoped.

---

## On the method

The fan-out found these because three agents read the spec and the source
closely and were then asked, explicitly, what they could NOT build. That
question produced better output than the code did.

The code did not survive review: 35 violations across three drafts, including
symbols that do not exist anywhere in the repository, a `num()` bypass
defended by a false claim about what `num(0)` returns, and a tenant id
rendered through `text-transform: capitalize` so the displayed id differs
from the real one.

Worth repeating for the findings. Not worth repeating for the code, at least
not without a hard size limit and a requirement to compile against the real
tree.
