## Cluster state, health and capacity

This section covers the screens that answer four operator questions: *is the platform
running*, *how much room is left*, *what is holding that room right now*, and *does the
control plane's belief match reality on the backends*. It deliberately does **not**
cover per-agent logs, per-user accounting or the account pool — those are other
sections.

**Nothing in this section can be opened from a browser today, and that is not a
detail of any one screen.** All four Cloud Run services are pinned to
`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`, and the module *validates against*
`INGRESS_TRAFFIC_ALL` (`terraform/modules/cloud_run/variables.tf:30-44`, refusal at
`:39-42`); swarm-api's invoker list is explicit members only
(`terraform/infra/main.tf:265`); every caller must present a Google ID token from an
allowed hosted domain; and there is no CORS handling anywhere in `apps/swarm-api`.
The owner's own build prompt already says so — *"This makes the internal load balancer
and IAP a PREREQUISITE, not a nicety"* (`docs/BUILD_PROMPT_V2.md:382-387`), listed as
build item 7 at `:552`. That work is **P0**: every "Yes" in the inventory below means
*"swarm-api already returns this to an authorised caller"*, never *"a browser can fetch
it"*.

Three further things the owner asked for in this area cannot be drawn from data that
exists today, and I have written them as blocked screens with exact prerequisites rather
than as screens: a **node visual** (§7), a **capacity/utilisation trend line** (§8), and
a **green/amber/red cluster health score** (§3.2 explains why the honest replacement is
a checklist, not a score).

---

### 1. Data inventory — every value on every screen in this section

Everything below was read from the repository, not inferred. Where a value does not
exist, §9 names the prerequisite. The right-hand column answers **"does swarm-api serve
this today?"** — reaching swarm-api from a browser at all is **P0** above, and gates
every row including the ones marked Yes.

| Value | Source | Served by swarm-api today? (browser access = P0) |
|---|---|---|
| `dispatch_paused` | Firestore `control/dispatch`, read at `apps/swarm-api/swarm_api/store.py:707-714` | Yes — `GET /v1/stats` → `.dispatch_paused` (any caller); `GET /v1/admin/dispatch` adds `.updated_by`, `.reason`, `.updated_at` (admin only, `routes/admin.py:70-75`) |
| Pool `name / hard_limit / adaptive_target / quota_derived_limit / effective_limit / active / available / enabled / updated_at` | Firestore `pools/{name}`; model `apps/common/swarm_common/models.py:44-79`; serialised `apps/swarm-api/swarm_api/codec.py:215-226` | Yes — `GET /v1/capacity` (tenant-filtered, `service.py:290-332`), `GET /v1/admin/pools` (unfiltered, `routes/admin.py:296-302`) |
| Runner-profile catalogue: `resource_class, backend, provider, units, pools[]` | `apps/common/swarm_common/profiles.py:118-176`, `RESOURCE_CLASSES` at `:77-81`, `pool_names_for` at `models.py:82-107` | Yes — embedded in `GET /v1/capacity`. **Its `pools[]` is computed for the caller's own tenant, admin or not** — Trap D |
| Task counts by state (12 states) | `store.count_tasks_by_state` at `store.py:524-533` | Yes — `GET /v1/stats` → `.tasks_by_state`, **scoped to the caller's tenant**; admin also gets `.platform_tasks_by_state` (`service.py:287`) |
| Which states cost capacity | `apps/common/swarm_common/states.py:35-42` (`CONCURRENCY_STATES`), `:56-63` (`PENDING_STATES`) | Constant, compiled into the UI |
| Live leases (`lease_id, task_id, attempt_id, tenant_id, generation, pools[], units, state, created_at, dispatch_deadline, expires_at, heartbeat_at, released_at, release_reason`) | Firestore `leases`; model `models.py:114-146`; index `leases-released-expires` at `terraform/modules/firestore/indexes.tf:144-151` | **No.** `LEASES = "leases"` is declared at `store.py:80` and **never used anywhere else in swarm-api**. There is no `lease_to_api` in `codec.py` and no route. → **P1** |
| Provider/quota health per `(provider, tenant)` | Firestore `quota/{provider}:{tenant_id}`; model `models.py:268-300` | Own tenant only — `GET /v1/providers` (`service.py:334-350`, the sole caller of `list_quota`, at `service.py:336`). `store.list_quota(None)` exists at `store.py:663-668` but **nothing calls it with `None`**. → **P2** |
| Resource-class sizing (cpu, memory, units) | `profiles.py:77-81` — `standard` 4cpu/8GiB/1u, `browser` 8cpu/16GiB/2u, `large` 8cpu/32GiB/4u | Constant, compiled into the UI |
| Cloud Run execution / GKE Job inventory | `apps/reconciler/reconciler/backends.py:263-289` and `:560-608`; views at `reconciler/model.py:171-202` | **No.** The reconciler's only routes are `/healthz`, `/readyz`, `POST /reconcile` (`reconciler/service.py:109-136`). swarm-api's SA has **no** `run.*` and **no** `container.*` (`terraform/modules/iam/bindings.tf:73-107`, swarm-api entry at `:79-85`). → **P5** |
| GKE nodes | nothing reads them; no `container.nodes.*` permission exists in either custom role (`terraform/modules/iam/custom_roles.tf:95-110`, `:123-133`) | **No, and partly impossible.** → §7, **P6** |
| Reconciler last pass | `GET /readyz` on swarm-reconciler, `reconciler/service.py:114-121`; report held in a per-instance dict at `:100` | **No** — internal ingress, and `min_instance_count = 0` (`terraform/modules/cloud_run/main.tf:40`) means a cold instance answers `last_pass_at: null`. → **P7** |
| Alert policies / incidents, log-based metrics | `terraform/modules/monitoring/alerts.tf:45,89,143,193,239,282,328`; `metrics.tf:103-133` | **No** — swarm-api holds only `logging.logWriter`, `monitoring.metricWriter`, `cloudtrace.agent`, `serviceusage.serviceUsageConsumer` (`bindings.tf:49-53, 79-85`). It has **no `roles/monitoring.viewer`** (the quota broker and reconciler do; swarm-api does not). → **P8** |

#### 1.1 Five traps in the data that a screen will fall into if nobody says so

**Trap A — `pool.active` counts weighted units, not agents.** Admission increments every
pool in the lease's list by `units` (`admission.py:204-208`), where
`units = RESOURCE_CLASSES[rc].units` ∈ {1, 2, 4}. So `global.active = 8` may be two
`large` agents or eight `standard` ones. **No label in this section may read "agents"
over a pool number.** The word is "units". The agent count comes from a different
place (task counts, or the lease list), and the two are shown side by side with
different words, never on the same axis.

**Trap B — `lease.state` only ever holds `LEASED` or `DISPATCHED`.** Admission writes
`LEASED` (`admission.py:196`), `scheduler/store.py:235-237` writes `DISPATCHED`, and
**nothing writes `STARTING` or `RUNNING` to the lease document** — the worker advances
the *task* through those states (`agent_worker/control.py:399-421`) and touches the
lease only to read it (`control.py:216`) and to write `heartbeat_at` / `expires_at`
(`control.py:509-516`). A "state" column on the lease table would therefore show
`DISPATCHED` for an agent that has been running for an hour. The column is labelled
**"dispatch"** with values `awaiting dispatch` / `dispatched`, and liveness is expressed
by the *heartbeat age* next to it. (`Lease.dispatch_overdue` at `models.py:144-146` is
still correct, because it only tests `state is LEASED`.)

**Trap C — `pool.updated_at` is not a freshness signal.** Terraform bootstraps pool
documents with `name, hard_limit, active, enabled, managed_by` and **no `updated_at`**
(`terraform/modules/firestore/bootstrap.tf:30-36`), and `pool_from_dict` fills the gap
with `datetime.now(timezone.utc)` (`codec.py:203-212`). A pool that has never held a
lease therefore reports "updated 0 seconds ago" on every single request. Separately, the
quota broker rewrites `updated_at` on every `provider:*` pool it touches each refresh
pass (`quota_broker/service.py:352-358`, schedule `*/5 * * * *` at
`terraform/modules/scheduler/variables.tf:122-125`), whether or not anything changed.
**No screen renders `pool.updated_at` as "last changed".** Freshness is the client's own
fetch timestamp. **P4** fixes the field properly.

**Trap D — the runner-profile `pools[]` in `/v1/capacity` is the caller's own tenant's
list, including for an admin.** `service.capacity()` calls
`pool_names_for(tenant_id=ctx.tenant_id, …)` unconditionally (`service.py:314-321`), so
an admin's headroom rows are "headroom for a task **this admin's tenant** submits", not a
platform figure. An admin reading those rows as the platform's capacity is the most
plausible misreading on the whole screen. Every headroom row therefore carries the
tenant it was computed for, in words: `headroom for tenant eng`. A platform-wide
per-profile headroom does not exist today and is **not** faked by swapping in another
tenant's pools; it needs **P3** (§3.1), which computes it server-side and can compute it
per tenant.

**Trap E — `/v1/stats` counts are tenant-scoped while the `global` pool is
platform-wide.** `stats()` calls `count_tasks_by_state(ctx.tenant_id)`
(`service.py:276`) and only adds `platform_tasks_by_state` for admins (`:287`), whereas
the `global` pool in `/v1/capacity` is visible to everyone and is a platform number
(`service.py:296-310` filters only other tenants' `tenant:*` and `*:tenant:*` pools).
Putting "4 agents" (this tenant) next to "7 units" (whole platform) in one tile is a
scope error dressed as a comparison. **Every panel in this section declares its scope in
the header — `This tenant · eng` or `Platform` — and a number may only sit beside
another number of the same scope.** The platform scope is admin-only; a non-admin's
screen has a Platform column only where the underlying pool genuinely is shared
(`global`, `resource:*`, `runner:*`, `backend:*`, `provider:*`), and it is labelled
"platform-wide" there.

And the one already paid for in this repo: **`enabled` is a real `false`, not an absent
field.** In JS, `pool.enabled ?? true` is correct; `pool.enabled || true` is the same
bug as jq's `.enabled // true` and reports a paused pool as open. Every comparison in
this section is written `pool.enabled === false`.

---

### 2. The honesty contract — a shared panel state machine

This platform's defining bug is an error rendered as an empty success. That is not
fixed by being careful on each screen; it is fixed once, in a shared component that
every panel in this section is built from. **No panel in this section renders a bare
number.**

`<DataPanel>` has exactly six states and each one is visually distinct without relying
on colour alone (icon + text, for accessibility and for a phone in sunlight):

1. **`loading`** — skeleton bars at the shape of the eventual content. **A numeric tile
   never shows `0` while a request is in flight.** The placeholder glyph is `—`.
2. **`ok`** — the data, plus a footer line: `as of 14:03:12 · GET /v1/capacity · 41 ms`.
3. **`ok-empty`** — the request succeeded and the collection is genuinely empty. Muted
   styling, a check glyph, and copy that names the query:
   `No live leases. GET /v1/admin/leases returned 200 with 0 rows at 14:03:12.`
   Never the error colour. This is the state that makes "nothing is running" legible.
4. **`error`** — non-2xx or transport failure. Red band with the HTTP status, the API's
   `code` field (the envelope is `{code, message, detail}`, `apps/swarm-api/swarm_api/errors.py:24-28`),
   the route, and a Retry control. **Numeric regions render `—`, never `0`.** A `401`
   or `403` from an expired IAP session (P0) lands here as an error with a re-auth
   action — never as an empty result.
5. **`stale`** — a previously successful payload is being displayed because the latest
   refresh failed. Amber band:
   `Showing data from 14:03:12. The last 3 refreshes failed (503 upstream_unavailable).`
   Every number in the panel is dimmed and gets a superscript marker. This state is the
   whole point of the contract: a poll that starts failing must not keep repainting the
   last good number as if it were current. **A dropped SSE stream (§3.5) is this state,
   not a silent freeze.**
6. **`partial`** — some of the panel's inputs arrived and some did not. The panel renders
   what it has and **enumerates what it could not check, by name**. §3.2 is the reason
   this state exists. A field the API returned as `null` because it was never measured —
   `quota: null` in `/v1/providers` (`service.py:347`), `last_pass_at: null` from the
   reconciler — is this state, not a clean one.

Plus one cross-cutting guard, **`truncated`**: `list_pools` caps at 500
(`store.py:605`), `list_tenants` at 200 (`store.py:186`), `list_quota` at 500
(`store.py:663`). None of these tells the caller it truncated, and two things follow
that a naive client gets wrong:

* **Counting rows does not detect it for a non-admin.** `service.capacity()` filters
  *after* `store.list_pools()` has already been capped (`service.py:295-311`), so a
  non-admin can receive 300 pools from a read that truncated at 500. The client-side
  heuristic `pools.length === 500` is therefore only valid on `GET /v1/admin/pools`.
  On `/v1/capacity` the client instead treats truncation as **undetectable** and says so
  once, quietly, in the data-source strip: `pool list completeness unverified (P10)`.
* **A truncated pool reads as extra headroom.** §3.1's rule "a missing pool is unlimited
  by construction" is correct for a pool that was never configured and catastrophic for
  one that was dropped by a limit: the screen would report room that admission will
  refuse. So the headroom computation treats *unknown completeness* as a reason to
  render `headroom: unknown`, never a larger number.

Arithmetic, so nobody re-derives it wrongly: the pool namespace is 13 fixed documents
(`global`, 3 `resource:*`, 5 `runner:*`, 2 `backend:*`, 2 `provider:*`) plus 3 per
tenant (`tenant:<id>` and 2 `provider:<p>:tenant:<id>`). 13 + 3T ≤ 500 holds through
**T = 162 (499 documents)**; truncation begins at **T = 163 (502)**. **P10** makes the
API say when it truncated, which is the only real fix.

---

### 3. Screen A — Operations Home

The one screen an operator opens first, and the only one that should ever be on a wall
display. It answers "is it running, and does anything need me". It is dense on purpose:
everything here is one HTTP round trip or two.

#### 3.1 What the user sees

**Row 0 — the pause banner.** Full-bleed, only rendered when
`stats.dispatch_paused === true`:

> **DISPATCH IS PAUSED PLATFORM-WIDE.** Nothing will be admitted at all.
> Paused by alice@saga.xyz at 13:41 · reason: "provider incident"
> `[ Resume dispatch ]`

Source: `GET /v1/stats` → `.dispatch_paused` for the flag (visible to every caller);
`GET /v1/admin/dispatch` → `.updated_by`, `.reason`, `.updated_at` for the attribution
line (admin only — a non-admin sees the banner without the attribution, not a missing
banner). The scheduler checks this before anything else (`scheduler/loop.py:135-143`,
`report.stop_reason = "dispatch_paused"`), so it is the first cause of "work is queued
and nothing is running" and belongs above everything. `[Resume dispatch]` calls
`POST /v1/admin/dispatch/resume` (`routes/admin.py:90-102`), which also wakes the
scheduler (`ctx.waker.wake("dispatch_resumed", …)`) — without that wake the queue waits
for the next safety tick.

Two shapes the control carries: the route takes a **JSON body**, `PauseRequest`
(`schemas.py:94-95`) = `{"reason": str|null}` with `max_length=512`, and `StrictModel`
forbids extra keys — so the button posts `{}` or `{"reason": "…"}` and nothing else, or
an operator gets a 422 mid-incident. And `store.get_control()` returns a synthesised
`{dispatch_paused: False, updated_at: None, updated_by: None, reason: None}` when the
document does not exist (`store.py:707-714`). That is correct — both mean "not paused" —
but it means `updated_by` and `reason` may be `null`, which render as nothing, never as
"paused by null" or `reason: "null"`.

**Row 1 — four tiles.** Each is a `<DataPanel>`, and each declares its scope in its
header (Trap E). The screen has one scope control at the top — `This tenant · eng` /
`Platform` — and `Platform` is present only for an admin, because only an admin receives
`platform_tasks_by_state`.

| Tile | Value | Computation | Scope |
|---|---|---|---|
| **Holding capacity** | `4 agents · 7 units` | agents = the four `CONCURRENCY_STATES` counts (`states.py:35-42`) summed from `stats.tasks_by_state` (tenant) or `stats.platform_tasks_by_state` (platform, admin only). units = `pools["tenant:<id>"].active` in tenant scope, `pools["global"].active` in platform scope — **the units number must come from the same scope as the agent count**. **Two numbers because they are two quantities** (Trap A). | both |
| **Global headroom** | `13 of 20 units free · platform-wide` + a bar | `pools["global"].available` and `.effective_limit` — both already computed server-side by `pool_to_api` (`codec.py:215-226`). The UI **does not recompute** `effective_limit`; that formula is frozen-contract logic (`models.py:65-72`) and this repository's rule is that every restatement of it has drifted. Always labelled platform-wide, on both roles' screens, because `global` is shared and is not the caller's own budget. | platform |
| **Backlog (costs nothing)** | `312 queued · 8 parked` | `stats.tasks_by_state.{SUBMITTED,QUEUED,READY}` summed, and `.PARKED` separately; `platform_tasks_by_state` in platform scope. Subtitle, always present: *"QUEUED, PARKED and READY create no infrastructure demand"* (CONTRACT.md invariant 1). This stops a 10,000-task backlog reading as an outage. | both |
| **Attention** | `2 of 6 checks are raising · 2 checks unavailable` | §3.2 | both |

**Row 2 — headroom by runner profile.** Five rows today (`mock`, `generic`,
`claude-code`, `codex`, `browser` — `profiles.py:118-176`). This is the honest answer to
"total cluster capacity", which does not otherwise exist: pools are a **conjunction** — a
task must clear `global` AND `tenant:` AND `resource:` AND `runner:` AND `backend:` AND
`provider:` AND `provider::tenant:` (`models.py:82-107`) — so capacity is a minimum
over a set, and it differs per profile. The row header names the tenant these numbers
are for (Trap D): *"headroom for tenant eng"*, on an admin's screen too.

```
claude-code    7 more   ▓▓▓▓▓▓▓░░░   limited by provider:anthropic:tenant:eng  (5 of 5 units used)
browser        0 more   ░░░░░░░░░░   PAUSED: resource:browser
mock          13 more   ▓▓▓░░░░░░░   limited by global                          (7 of 20 units used)
```

Computation, per profile `p` in `capacity.runner_profiles`:

```
units    = p.units
required = p.pools                       # pool_names_for() for the CALLER's tenant (Trap D)
slots    = []
for each name in required:
    pool = pools[name]
    if pool is undefined:
        if pool-list completeness is unverified:  headroom = UNKNOWN; break   # §2
        continue                         # genuinely unconfigured => unlimited (admission.py:100-104)
    if pool.enabled === false: headroom = 0; blocker = name (MANUAL_PAUSE); break
    slots.push(floor(pool.available / units))
headroom = slots.length ? min(slots) : UNLIMITED    # "no pool caps this profile", not 0
blocker  = the argmin pool name
```

This mirrors `evaluate_capacity` (`admission.py:87-114`, missing-pool skip at `:100-104`,
`MANUAL_PAUSE` at `:105-110`) but is not the same code, and a JavaScript copy would be a
fourth restatement of the frozen admission rule — exactly what
`scripts/lib/check-contract-parity.sh` exists to police for the shell and jq copies.
**Recommendation (P3): compute it in swarm-api instead**, inside `service.capacity()`,
adding `headroom: {slots, blocking_pool, blocking_reason, computed_for_tenant}` per
profile, with a unit test asserting `headroom.slots > 0 ⟺ evaluate_capacity(...) == []`
so the derived number is tied back to the frozen function. P3 is also the only honest
route to a *platform* per-profile headroom, since the server can compute it per tenant
and the client cannot. It is a small Track A change and it removes the restatement
permanently. The screen is buildable without it; it is just built on a copy, and
tenant-scoped only.

**Row 3 — the Attention list** (§3.2), then **Row 4 — the data-source strip** (§3.3).

#### 3.2 The Attention rollup, and why it is a checklist rather than a score

`scripts/status.sh:329-347` already computes a six-item operator rollup. It is the best
existing specification of what a health page should say, and the UI reproduces it —
**with one change that matters more than the rest of this screen**.

| # | Check | Source | Available today? |
|---|---|---|---|
| 1 | Dispatch is paused | `GET /v1/stats.dispatch_paused` | **Yes** |
| 2 | N tasks parked | `GET /v1/stats.tasks_by_state.PARKED` | **Yes**, tenant-scoped; platform-wide is admin-only (`platform_tasks_by_state`) |
| 3 | N pools paused | `GET /v1/capacity` → `pools.filter(p => p.enabled === false)` | **Yes** (a non-admin sees only the pools it is shown — §3.4) |
| 4 | N expired leases still holding capacity | `leases` where `released_at == null && expires_at < now` | **No — P1** |
| 5 | Providers throttled / exhausted | `quota` where `state ∈ {EXHAUSTED, COOLDOWN}` (`models.py:268-276`) | **Own tenant only** via `GET /v1/providers`; platform-wide needs **P2** |
| 6 | Pod restarts observed | k8s pod `restartCount` | **No — P5** |

`status.sh` prints **"nothing needs attention"** when its list is empty — and it builds
that list from helpers that return `[]` on failure (`gcloud_json`, `scripts/status.sh:57-63`),
so a dead `gcloud` and a clean platform produce the same line. A UI that copies that line
while three of six checks never ran is the exact bug this platform keeps shipping — an
absence of evidence rendered as evidence of health. So:

> **The Attention panel never renders "nothing needs attention" unless all six checks
> actually ran and returned clean.** Otherwise it renders
> `4 checks clean · 2 could not run` and lists the two by name with the reason:
> *"expired leases: no API exposes the lease collection (see P1)"*.

Two corollaries in the same spirit: check 5 reads `providers[].quota`, which is `null`
whenever no quota document exists for that `(provider, tenant)` (`service.py:347`) —
**`null` is "not measured", so that provider counts as *could not run*, not as clean**;
and a check that is available only for the caller's own tenant says so in its row rather
than implying the platform.

The same rule replaces the green/amber/red score the owner asked for. A score is a
single cell that has to be *some* colour, and with two of six inputs missing and the
reconciler answering `null` on a cold instance, that colour would be a guess. The
checklist says what is known and what is not, in the same amount of screen space.

Each raising item is a row with a one-line consequence and a link, matching the
runbook language already in `status.sh`:

```
⚠  provider:anthropic:tenant:eng is PAUSED
   No admission for claude-code on tenant eng. Resume → /capacity#provider-anthropic-tenant-eng
⚠  8 tasks parked
   Parked tasks cost nothing. 6 are PROVIDER_QUOTA_EXHAUSTED → /providers
○  expired leases — CANNOT CHECK
   No endpoint exposes the lease collection. Until then use scripts/status.sh.
```

#### 3.3 The data-source strip

A three-to-six-cell footer, one cell per upstream, each showing the route, the HTTP
status of the most recent attempt, the age of the newest successful payload, and the
current `<DataPanel>` state. This is the screen's own self-report, and it is what an
operator looks at when a number seems wrong:

```
/v1/stats  200 · 380ms · 12s ago      /v1/capacity  200 · 41ms · 4s ago
/v1/admin/dispatch  403 forbidden     /v1/admin/leases  not built (P1)
```

A `403` here is information, not a failure — a non-admin genuinely cannot read
`/v1/admin/*`, and the strip says so rather than letting the page look broken. A `401`
is different: it is an expired IAP session (P0) and carries a re-auth action.

#### 3.4 Tenant scoping and invariant 9

`service.capacity()` filters another tenant's `tenant:*` and `*:tenant:*` pools out for
non-admins (`service.py:296-310`), deliberately, because `active` is a usage signal
about that tenant. The response does not say how many pools were removed — and it must
not, because that count is the size of the tenant roster.

So a non-admin's Operations Home carries one fixed line under the pool areas:
*"Pools belonging to other tenants are not shown."* Qualitative, never a number. The
screen is otherwise identical for both roles — same widgets, same layout, with the
platform/tenant scope control present for both and disabled with a reason for a
non-admin — so nobody learns the platform shape from which widgets appear.

#### 3.5 Refresh and cost

The owner's §2.8 asks for a dashboard that *pushes updates over server-sent events*.
SSE is a property of the dashboard service (build item 8, `docs/BUILD_PROMPT_V2.md:553`)
which holds the service identity and fans one poll out to every viewer; **swarm-api has
no streaming endpoint today and is not being asked to grow one.** The intervals below
are what that service (or, before it exists, an authorised client) does against
swarm-api. The `<DataPanel>` contract is unchanged either way: a dropped SSE stream is
the `stale` state, with the same amber band and the same dimmed numbers as a failed poll.

| Call | Cost | Interval |
|---|---|---|
| `GET /v1/capacity` | one unindexed stream of `pools` — 13 + 3T docs (≈73 at 20 tenants), sub-100 ms | **10 s** visible, paused when the tab is hidden (Page Visibility API), **60 s** on a phone |
| `GET /v1/stats` | **12 serial Firestore COUNT aggregations** (24 for an admin, because `service.py:287` calls `count_tasks_by_state` a second time for the platform view) — `store.py:524-533` | **30 s** minimum, **60 s** on a phone. Never bind it to a live tick. |
| `GET /v1/admin/dispatch` | one document read | **30 s**, or piggyback on the `stats` tick |

**What breaks at 100× — and this one costs real money.** Firestore COUNT bills one read
unit per 1000 index entries *matched*. `tasks` has no TTL policy — the only TTL in
`terraform/modules/firestore/indexes.tf:249-262` is on `tasks/{id}/events`, and it is
conditional on `event_ttl_field` being set — so the task collection grows monotonically
forever. At 10 M lifetime tasks, one `/v1/stats` call scans ~10 M index entries across
its 12 queries ≈ 10,000 read units. At a 30 s refresh (2,880 calls/day) that is
**~29 M read units per day per open browser tab**, and double that for an admin's tab.
This is the single most expensive thing in the whole UI proposal and it is invisible at
today's volume.

Three ways out, in order of preference: (a) maintain a counters document updated in the
same transactions that already move task state; (b) cache the `/v1/stats` payload
server-side for 30 s and serve every viewer from it — one tab's cost regardless of how
many people watch; (c) at minimum, run the 12 queries concurrently so the latency stops
being 12 serial round trips. **(b) is the cheap correct answer for a dashboard and
should ship with the first version of this screen**, because a wall display is by
definition an always-open tab. Note that (b) must cache per scope — the tenant payload
and the platform payload are different documents, and serving one for the other is
Trap E with a cache in front of it.

The `pools` scan does **not** have this problem — it is bounded by tenant count, not by
history — until `list_pools`' 500 limit truncates it at 163 tenants (§2).

#### 3.6 Phone layout (390 pt)

- Pause banner: full-bleed, sticky at the top, **never collapses**. It is the one thing
  that must survive every viewport.
- Scope control (`This tenant` / `Platform`): a segmented control directly under the
  banner, because a number read at the wrong scope is worse on a small screen where the
  header label is the first thing to be truncated.
- Tiles: 2 × 2 grid, 8 pt gutter, tabular numerals. The two-number "Holding capacity"
  tile stacks `4 agents` over `7 units` rather than putting them on one line.
- Headroom: the five profile rows stay, because five rows is the whole list. The
  "headroom for tenant X" header stays too. The "limited by" pool name is the part that
  overflows — it truncates **in the middle** (`provider:anthropic:…:eng`) and the full
  name is in the tap sheet.
- Attention: full-width rows, always visible, never behind a tap. The
  `N could not run` line stays visible too.
- Data-source strip: collapsed to one line — `4 sources · 1 stale · 1 unavailable` —
  tapping expands the cells.
- The refresh control and the `as of` stamp sit at the **bottom**, in thumb reach, not
  under the notch.

#### 3.7 Empty, loading, broken

- **Loading**: all tiles `—`; the pause banner is not rendered at all until
  `/v1/stats` answers (rendering it optimistically as "not paused" is the failure mode).
- **Empty**: `0 agents holding capacity` is a legitimate reading and renders in the
  `ok-empty` style with `GET /v1/stats returned 200 at 14:03:12` under it. An idle
  platform and a broken query must never look alike, and today they do in `status.sh`,
  whose `gcloud_json` returns `[]` on any failure (`scripts/status.sh:57-63`).
- **Broken**: if `/v1/capacity` fails, the headroom rows and the global-headroom tile go
  to `error` and show `—`; the tiles fed by `/v1/stats` keep working. Panels fail
  independently — one dead upstream must not blank the page.
- **Stale**: after two consecutive failed refreshes of any source, that source's panels
  flip to `stale` and the data-source strip turns amber. After five, the page shows a
  single top-level band: `Live updates have stopped.`
- **Unauthorised**: a 401 blanks nothing — panels hold their last payload in `stale` and
  the page shows a re-auth action. Sessions expire behind IAP (P0) and an expiry that
  reads as "the platform is empty" is this section's whole thesis failing at the door.

---

### 4. Screen B — Capacity board (the pool table)

The full pool inventory. This is where an operator goes when the headroom rows say
"limited by X" and they need to know why.

#### 4.1 What the user sees

Rows grouped by pool family, in this order — `global`, `tenant:*`, `resource:*`,
`runner:*`, `backend:*`, `provider:*`, `provider:*:tenant:*` — matching
`pool_names_for` (`models.py:82-107`) so the grouping teaches the conjunction rather
than hiding it. A fixed header above the table states it once:
*"A task must clear **every** pool in its list at the same moment; capacity is the
minimum across them, never a sum."*

<!-- The text supplied for review was cut off at this point, mid-sentence, in §4.1.
     §§4.2–9 — including the definitions of P1–P10 that §§1–3 reference, the node
     visual (§7), the trend line (§8) and Screen B's phone layout — were not part of
     the reviewed text and are unchanged and unverified here. P0 (internal LB + IAP,
     docs/BUILD_PROMPT_V2.md:382-387, :552) must be added to §9 as the prerequisite
     that gates every screen in this section. -->
