## Operator and admin affordances

This section specifies the screens an operator and an admin use: the ones that answer
"is the platform healthy", "is what is running what we asked for", "where did the
capacity go", "who changed this", and "which subscription account is about to die".

It is written against the code, not against the docs. Every field name, route and
collection below was opened and read. Where the verified-findings brief
(`operator-gaps.md`) was wrong, I say so and give the file and line, because a spec
that inherits a wrong claim produces a screen that renders a permanent zero.

**Scope warning.** This revision specifies **O1 only**. O2 (capacity ledger), O3
(config vs reality), O4 (deploy truth), O5 (account board) and O6 (incident feed) are
referenced by the ground rules and by O1's reserved slots but are **not written yet**.
§0.6 records what each still owes, so nothing the brief asked for is lost by being
unmentioned.

---

### 0. Ground rules

#### 0.1 The front door, and what it means for every number below

The decision is taken: an external Application Load Balancer with IAP in front of the
existing `swarm-api`. Consequences that constrain every screen in this section:

* **Every value must be reachable from a `swarm-api` route.** Where there is no route,
  that is a prerequisite, not a design note. Reading Firestore directly from a UI
  backend is off the table — it would bypass `runner_profile` resolution (invariant 10)
  and the tenant filter in `service.capacity()`
  (`/Users/bogdan/claudespace/agent-swarm-infra/apps/swarm-api/swarm_api/service.py:290-332`).
  Note that `swarm-api` is not the only control-plane service: the quota broker runs its
  own FastAPI app with its own routes (e.g. `POST /v1/quota/sweep`,
  `apps/quota-broker/quota_broker/main.py:500`) behind its own audience. A broker route
  is **not** a route this UI can call; anything from the broker has to be proxied by
  `swarm-api` or it does not exist for these screens.
* **Admin gating already exists and needs nothing new.** `admin_auth`
  (`apps/swarm-api/swarm_api/deps.py:190`) → `require_admin`
  (`apps/swarm-api/swarm_api/auth.py:248-251`) → `AuthContext.is_admin`, which is set by
  membership of `ADMIN_GROUPS` (`auth.py:225-226`, settings at `settings.py:122`). Every
  screen in this section is `Depends(admin_auth)`. No second auth mechanism, no shared
  bearer token.
* **Cross-tenant read safety is not yet good enough to lean on.** `_assert_principal_matches`
  (`store.py:255`) runs only inside `ensure_tenant` (`store.py:190`, called at `:230`).
  The admin screens here are admin-group-gated and platform-wide by design, so they are
  not the exposure — but nothing in this section should be reused to build a
  *tenant-facing* screen until the `u-` prefix collision
  (`docs/contract-change-requests.md` §1) is closed.

#### 0.2 The rule every screen obeys: a number never renders without its read status

This platform's defining bug is an error rendered as an empty success. It is still live
in two places I read today:

* `scripts/status.sh:57-64` — `gcloud_json()` takes its exit status from the *end* of a
  pipeline (`"$@" --format=json 2>/dev/null | redact`), so `redact` succeeding makes an
  expired gcloud session render as `[]`. Every Cloud Run listing on the operator's
  status screen collapses to "no services" when it means "could not look".
* `scripts/status.sh:138` — `gke="$(jq -nc --argjson pods "${pods:-[]}" '{reachable:true, pods:$pods}')"`.
  `reachable: true` is hardcoded. A `Forbidden` on the cluster-wide `get pods -A` renders
  as a positive assertion that GKE is reachable and empty.

The Firestore half *is* fixed — `fs_request` (`scripts/lib/common.sh:588-636`) checks
the HTTP status, opts into 404 only via `FS_ALLOW_404`, and `fs_list_docs` (`:664-669`)
runs `fs_list` first and checks it before piping to `jq`, with a comment naming this
exact trap. `fs_patch` (`:671-680`) now goes through `fs_request`, so the
`pause-swarm.sh` 403-as-success failure is closed on the shell side.

**The UI must not reintroduce it.** Every panel's data arrives in this envelope, and the
envelope is not optional:

```jsonc
{
  "value": <T> | null,
  "status": "ok" | "empty" | "unreadable",
  "source": "swarm-api:GET /v1/admin/pools",
  "observed_at": "2026-09-19T11:04:02Z",
  "age_seconds": 7,
  "truncated": false,                        // see 0.3
  "error": { "code": "PERMISSION_DENIED", "message": "…", "retryable": false } | null
}
```

Rendering rules, in order of how much damage breaking them does:

1. **`status: "unreadable"` never renders a number.** Not zero, not a dash that looks like
   zero, not a greyed-out last value. The tile renders a diagonal-hatch fill, the literal
   word **Unreadable**, and the error code. Last-known values may appear *only* with an
   explicit "last known, 4m ago" label and a strike, never in the primary numeral slot.
2. **"Unreadable" is visually louder than "empty".** The whole failure class is that zero
   looks calm. Unreadable gets the alert colour and the hatch; empty gets normal weight
   and the word "none".
3. **Any aggregate with an unreadable input renders `—`, never a partial sum.** "Total
   active across pools" with 3 of 19 pools unreadable is not a number. It is `—` with
   "3 of 19 pools unreadable" beneath it. This is the sum-with-a-hole bug and it is how a
   half-read platform looks fully healthy.
4. **Page-level banner.** A screen with ≥1 unreadable region shows a sticky top banner:
   `3 of 11 panels could not be read` → tap expands the list of sources and error codes.
   This banner is the single most important element in this section. It is what turns
   "the dashboard looked fine" into "the dashboard said it could not see".
5. **Stale is a third visual state on `ok`.** Each panel declares a freshness budget; past
   it, the timestamp goes amber and reads "as of 4m ago", and the numerals go to 60%
   opacity. A stale number is still a number; an unreadable one is not.
6. **A default substituted for an absent field is not a read.** §0.4 trap 1b lists the
   four places the API's own read models manufacture a value from a missing field. Where
   a panel's column depends on one of them, the route must report field presence and the
   UI renders `not set`, never the default. A number the code invented is the same lie as
   a zero the code could not read.

#### 0.3 Truncation is the same bug wearing a different hat — and it is live today

Verified in `apps/swarm-api/swarm_api/store.py`:

| reader | cap | paginates? | reports truncation? |
|---|---|---|---|
| `list_pools` (`:605`) | 500 | no | **no** |
| `list_tenants` (`:186`) | 200 | no | **no** |
| `list_quota` (`:663`) | 500 | no | **no** |
| `list_events` (`:490`) | 200 | no | **no** |
| `list_artifacts` (`:501`) | 200 | no | **no** |
| `list_tasks` (`:381-413`) | caller's | **yes** — `limit + 1` + cursor | yes |

`list_tasks` shows the house already knows the pattern. The admin read models did not get
it. Pool count grows as `1 + T + 3 + 5 + 2 + P + Σ_t |providers(t)|` — one global, one per
tenant, three resource classes (`profiles.py:78-80`), five runner profiles
(`profiles.py:119-163`), two backends (`terraform/infra/locals.tf:100`), one per provider,
and one per (tenant, provider) pair (`locals.tf:148-205`). Dev today is ~19; at 200 tenants
and 3 providers it is ~814 — and `GET /v1/admin/pools` would return the first 500,
unordered (`.limit(500)` with no `order_by`; the route's `sorted()` at
`routes/admin.py:302` sorts the 500 it happened to get, which is worse, because the result
looks deliberately ordered), and the UI would render a partial platform as the whole
platform.

**Every list panel must compare `len(items)` to the cap it asked for and, when equal,
render `showing 500 of ≥500 — truncated` instead of a total.** The `truncated` flag in
the envelope carries this.

**Prerequisite, not a design note:** the client cannot do that comparison today.
`GET /v1/admin/pools` (`routes/admin.py:296-302`) returns `{"pools": [...]}` with no cap,
no count and no cursor, so a UI can only detect truncation by hard-coding the server's
`500`. A hard-coded cap that drifts from the server's is a silent return of this exact
bug. The route must echo the cap it applied (and should accept one), or `truncated` is a
guess.

#### 0.4 Traps found today — do not build on these

These are in addition to `operator-gaps.md`'s "did not survive verification" list. Each
was opened and read.

1. **`pool.updated_at` is manufactured at read time.** `pool_from_dict`
   (`apps/swarm-api/swarm_api/codec.py:211`) does
   `updated_at=as_datetime(data.get("updated_at")) or datetime.now(timezone.utc)`.
   Terraform's bootstrap document (`terraform/modules/firestore/bootstrap.tf:30-36`)
   writes `name`, `hard_limit`, `active`, `enabled`, `managed_by` — **and no `updated_at`**.
   So every pool nobody has ever changed through the API reports `updated_at` = the instant
   of the read. A "last changed" column would show "just now", forever, for exactly the
   pools that have never changed. **Do not render `updated_at` as "last changed" from
   `pool_to_api`.** The route must report whether the stored field was present; absent
   means "never changed since provisioning", which is a different and more useful fact.
1b. **`pool_from_dict` invents three more values the same way** (`codec.py:203-212`), and
   all three are columns O1 draws: `hard_limit=int(data.get("hard_limit", 0))` — a document
   missing the field reads as a **hard cap of 0**, which renders identically to a
   deliberate throttle to zero; `active=int(data.get("active", 0))`; and
   `enabled=bool(data.get("enabled", True))` — a document missing `enabled` reads as
   **open**. A fourth sits in `store.get_control()` (`store.py:707-714`): when the
   `control/dispatch` document does not exist it returns `{"dispatch_paused": False, …}`,
   so **a missing control document renders as a green DISPATCHING pill** — on a fresh
   environment, or against a wrong `FIRESTORE_DATABASE`. Each of these is "absent" being
   rendered as a confident value, which is rule 6. The routes must carry field/document
   presence alongside the value.
2. **`SlotPool` has no `managed_by`.** `operator-gaps.md` lists it in the pool shape. The
   model (`apps/common/swarm_common/models.py:44-79`) does not have it; `pool_from_dict`
   drops it; `pool_to_api` (`codec.py:215-226`) never emits it. Terraform *does* write it
   into the document (`bootstrap.tf:35`), and `store.upsert_pool`'s create branch
   (`store.py:639-649`) uses `ref.set({...})` without it — so a pool created by an admin
   PUT genuinely has no marker, and a terraform-created one has a marker nothing reads.
   Getting it onto the screen does **not** need a contract change: `store.py` is unfrozen,
   so a `list_pools_raw()` returning the snapshot dict beside the typed pool is the fix.
2b. **A pool created by an admin PUT with no limit gets `hard_limit = 1_000_000.**
   `UNLIMITED_HARD_LIMIT` (`store.py:96`) is the default in `upsert_pool`'s create branch
   (`store.py:630-631`). A binding-constraint column that reads `capped by
   hard_limit=1000000` is a lie dressed as a cap; the UI renders `uncapped` for that
   sentinel and says so.
3. **Attempts carry no `runner_profile` and no `resource_class`.**
   `ControlPlane.record_attempt_start`
   (`apps/agent-worker/agent_worker/control.py:425-457`) writes exactly sixteen keys and
   neither is among them. The runbook's "peak RSS by profile" therefore needs a join from
   every attempt to its task — 200 extra document reads for a 200-attempt panel.
4. **The event TTL is configured on a field nothing writes.**
   `terraform/modules/firestore/indexes.tf:249-264` sets a TTL policy on
   `var.event_ttl_field`, default `"expires_at"`
   (`terraform/modules/firestore/variables.tf:67-71`). No event writer sets it:
   `TaskEvent` (`models.py:219-228`) has no such field, `event_to_firestore`
   (`codec.py:139-142`) is `asdict(event)`, and the worker's explicit dict
   (`control.py:363-375`) does not include it. **Task events never expire**, despite the
   comment at `indexes.tf:249-250` saying that without a TTL the subcollection grows
   without bound. Any event-derived feed sits on an unbounded collection.
5. **The account sweep's result is keyed by label, not by account.**
   `CredentialRefresher.refresh_secret` (`credentials.py:211-220`) returns
   `self._refresh(secret_base, tenant_id=label or secret_base, provider="account")`, and
   the error path (`:256`) returns `RefreshOutcome(label, "account", False, "error")`. So
   the `accounts` block of the broker's `POST /v1/quota/sweep` identifies each row by
   **label alone** — two tenants that both use the label `main` are indistinguishable in it.
6. **Account ambiguity is at the secret, not the document.** `operator-gaps.md` says
   `swarm-account-u-bogdan-devops-main` is ambiguous between two valid registrations. True
   for the **Secret Manager name** — `secret_name()` (`accounts.py:97-102`) concatenates
   with a dash that both parts may contain. Not true for the Firestore key:
   `account_id_for()` (`accounts.py:291-293`) joins with a **colon**, so `u-bogdan:devops-main`
   and `u-bogdan-devops:main` are distinct documents that collide only on their secret.
   The UI must key rows on `account_id` and *display* the resolved secret name as a column,
   so the collision is visible rather than inherited.
6b. **`Account.state` is not safe to render, and this is worse than the brief says.**
   `AccountStore.set_state` (`apps/quota-broker/quota_broker/accountstore.py:136`) is
   defined and **called from nowhere in the quota broker** — grep for `set_state` across
   `apps/quota-broker/quota_broker/` returns only its own definition.
   `ReauthRequired` is caught at `credentials.py:174` and used only for the response body.
   So `state` stays `AVAILABLE` for a revoked account forever, and
   `accounts.py:356`'s filter (`[a for a in accounts if a.state is not
   AccountState.REAUTH_REQUIRED]`) is a guard that can never fire. **An account board
   built today paints green over exactly the condition that needs a human.** The
   write-back ships before the screen; this is a prerequisite, not a caveat.
7. **`operator-gaps.md` understates what is already indexed.** It says a usage-over-a-month
   chart "needs a composite index on `(tenant_id, created_at)`". That index exists:
   `attempts-tenant-created` at `terraform/modules/firestore/indexes.tf:171-178`. So do
   `attempts-task-created` (`:162-169`), `leases-released-expires` (`:143-150`),
   `leases-state-dispatch-deadline` (`:133-140`), `leases-tenant-created` (`:152-159`) and
   the `COLLECTION_GROUP` `events-tenant-at` (`:181-188`). **Several screens the brief
   calls blocked on indexing are blocked only on a route.** No new composite index is
   required by anything in this section; in particular the admin `/stats` tenant-scoped
   count (`state == X AND tenant_id == Y`) is served by the `(tenant_id, state, …)` prefix
   of `tasks-tenant-state-created` (`indexes.tf:85-93`).
8. **An admin's `GET /v1/stats` is twenty-four aggregation queries.**
   `count_tasks_by_state` (`store.py:524-533`) issues one COUNT per `TaskState` — twelve
   (`states.py:18-31`) — and `service.stats()` calls it twice when `ctx.is_admin`: once
   tenant-scoped (`service.py:276`) and once platform-wide (`service.py:287`). This is the
   one panel that must not poll fast.
9. **The runbooks name an endpoint that does not exist.** `docs/troubleshooting.md:146` and
   `docs/scaling.md:151` both say `./scripts/api.sh GET /stats | jq '.resource_usage'`.
   `service.stats()` (`service.py:271-288`) returns `tenant_id`, `tasks_by_state`,
   `dispatch_paused`, `limits`, `generated_at`, and `platform_tasks_by_state` for admins.
   There is no `resource_usage` field and never was. This is in the OOM section, which is
   where someone lands at 3am. Fix is a two-line doc correction in Track D's files —
   reported, not made.

#### 0.5 Owner's decisions (not mine to settle)

**D1 — Where GCP-sourced numbers come from.** Screens O4 (Deploy Truth), the env-block row
of O3, and the alert-policy row of O3 need Cloud Run Admin API and Cloud Monitoring reads,
which `swarm-api` cannot do today. **Until this is answered, no screen draws a Cloud Run,
GKE or Monitoring chip** — including O1's source strip, which in this revision lists only
the sources O1 actually reads.
* *(Recommended)* **A snapshot collector.** A small Cloud Run Job on the reconciler's
  Cloud Scheduler tick reads `run.googleapis.com` and `monitoring.googleapis.com`, writes
  a `platform_snapshots/{service}` document, and `swarm-api` serves those. Keeps the API's
  IAM narrow, gives the deploy screen the history it needs anyway (O4 wants "when it last
  changed"), and an admin request never blocks for seconds on a GCP list call. Cost: one
  new component.
* **Inline in `swarm-api`.** Grant the API's service account `run.viewer` and
  `monitoring.viewer`, call GCP from the admin route. No new component; widens the blast
  radius of the one service every tenant can already reach, and each page load waits on
  GCP.
* **Neither.** O4 and two rows of O3 do not get built. Honest, and leaves the
  `deploy.sh` silent-skip class invisible — which is the failure that produced "a
  half-deployed control plane where the API is new and the scheduler is old, with an exit
  code of 0".

**D2 — Does the UI perform control actions, or only observe?** Whatever is chosen, one
rule is **not** optional and is not part of the choice: **a mutation renders the result of
a fresh read, never the response of the write.** `pause-swarm.sh`'s recorded failure was a
403 discarded into an `ok paused <pool>` line; `store.set_dispatch_paused`
(`store.py:715-724`) returns the payload it *intended* to write, and `store.upsert_pool`'s
update branch (`store.py:660-661`) does re-read while the create branch (`:639-650`) does
not. Every confirmation in this UI shows the re-read value and its `observed_at`, or it
shows "write acknowledged, not yet confirmed".
* *(Recommended)* **Read-only, plus the four mutations that already have routes**: pause
  and resume dispatch (`POST /v1/admin/dispatch/{pause,resume}`, `routes/admin.py:78-101`),
  change a ceiling (`PUT /v1/admin/limits/…`, `:107-168`), drain a provider or resource
  class (`POST /v1/admin/{providers,resources}/…/drain`, `:219-265`). Each behind a typed
  confirmation — type the pool name to change its ceiling — mirroring the repo's own rule
  that anything destructive requires a typed confirmation and ignores `SWARM_ASSUME_YES`.
* **Fully read-only in v1.** A pause button behind IAP is a platform-wide stop reachable
  from a phone on a train. Safer; means an incident still runs through the scripts, which
  is where the attribution holes are.
* **Everything the API exposes.** Includes tenant limit changes and provider enable/disable.

**D3 — How the control-action trail gets the entries scripts make.** `pool-limit.sh` and
`pause-swarm.sh` write Firestore directly, because the API is unreachable from a
workstation (both say so in their headers: `scripts/pool-limit.sh:29-33`,
`scripts/swarm.py:1-13`). Once the ALB and IAP exist, it is reachable.
* *(Recommended)* **Repoint the scripts at the ALB.** One writer, one code path, no
  second restatement of the audit shape. Touches Track D and Track C.
* **Dual-write.** Scripts append to the `audit` collection themselves. Faster; a second
  restatement of a contract, which CLAUDE.md is explicit about where those end up.
* **Accept the hole.** The trail is missing exactly the entries made by someone working
  around the unreachable API — which is the population most worth having.

**D4 — Refresh transport.** *(Recommended)* plain polling per panel at the budgets given
below, with a visible "as of" and a manual refresh; Server-Sent Events only for the
incident feed (O6), where latency matters and the payload is small. Alternative: SSE
everywhere, which means a long-lived connection per viewer through IAP and the ALB and a
new failure mode — a dead stream that looks like a quiet platform, i.e. the same bug
again.

#### 0.6 What the remaining screens still owe (so it is not lost)

Written here because O2–O6 are not specified below and each carries a precondition that
must not be rediscovered at build time.

* **O2 — Capacity ledger.** Needs `GET /v1/admin/leases`; no route in
  `apps/swarm-api/swarm_api/routes/` reads the `leases` collection at all. The predicates
  are already on the model (`models.is_expired`, `models.dispatch_overdue`,
  `models.py:138-145`). This is the screen that makes `pool.active` checkable.
* **O3 — Config vs reality.** Three rows, and only one of them is D1's problem. The pool
  row (live `pools` vs `terraform output -json pool_limits`) already exists as
  `scripts/pool-limit.sh --check` / `make pool-check`, which **nothing invokes** — not
  CI, not `make test`, not a doc. The tenant row (`max_active`, `capacity_units`) drifts
  the same way, via the same `ignore_changes = [fields]`
  (`terraform/modules/firestore/bootstrap.tf:71-75`), and is checked by **nothing at all**.
  Per the brief's refutation, live ceilings are already on `GET /v1/admin/pools`; only the
  *configured* column needs plumbing.
* **O4 — Deploy truth.** Blocked on D1.
* **O5 — Account board.** Blocked on trap 6b (the `REAUTH_REQUIRED` write-back) and on a
  `swarm-api` route, since `accounts` is broker-side. Third column, from the brief and
  confirmed at `scripts/account.sh:119-121`: `_report` **warns** on a failed IAM grant and
  registers the account anyway, so "can the broker actually read this account's refresh
  secret" is a column, not an assumption.
* **O6 — Incident feed.** Sits on `tasks/{id}/events`, which trap 4 shows never expires.
  Bound the query before the collection is unbounded in production, not after.

---

### O1 — Control Room

The landing screen. One question: *is the platform healthy right now, and if not, what is
the first thing to look at.*

#### What the user sees

**Row 1 — the state banner** (full width, sticky):
* Environment chip: `dev` / `prod`, the Firestore database name, the region. **Two of
  these four values are not on any route today — see "What it cannot show yet".**
* **Dispatch pill**: `DISPATCHING` (green), `DISPATCH PAUSED` (red), or — the third state
  trap 1b forces — `UNKNOWN — no control document` (hatched). When paused it expands
  inline to `paused by alice@saga.xyz · 14:02 · "provider incident #412"`.
* "as of HH:MM:SS" with the freshness dot, and a manual refresh control.

**Row 2 — four counters**, each a single large numeral with its label:
* **Holding capacity** — `active` on the `global` pool. One number, because `global` is
  the pool every lease takes (`models.pool_names_for`, `models.py:82-107`).
* **Waiting** — `tasks_by_state.READY + tasks_by_state.QUEUED`.
* **Parked** — `tasks_by_state.PARKED`.
* **Dead-lettered** — `tasks_by_state.DEAD_LETTERED`, in alert colour when > 0. Labelled
  **"currently dead-lettered"**, never "all time": `count_tasks_by_state`
  (`store.py:524-533`) counts tasks *in* that state now, and `scripts/purge-data.sh:42`
  purges the whole `tasks` collection, so an all-time framing would be wrong the first
  time anyone runs a purge.

**Row 3 — pools that need looking at.** Not all 19. A pool appears here when
`available == 0`, or `active > effective_limit`, or `enabled == false`, or its read is
unreadable. Each row: `name · active/effective_limit · a bar · the binding constraint`.

Three groups, visually distinct, in this order:

1. **Oversubscribed** — `active > effective_limit`. This is its own group and it is the
   loudest thing on the screen, because it is the **one symptom of the capacity-leak
   class that O1 can show without a leases route**: a pool holding more than its own
   ceiling allows. Folding it into "full" (both have `available == 0`) hides it.
2. **Full** — `available == 0` and not oversubscribed.
3. **Drained — no new admissions** — `enabled == false`, with `active` beside it as
   `N lease(s) still holding capacity`. That count is `pool.active` and nothing stronger;
   it is the same number `scripts/pause-swarm.sh:89-90` prints in that sentence. It is
   labelled `active (pool counter, unverified — see O2)` because `active` is the counter a
   leaked lease inflates, so a drained pool that never reaches zero is either work still
   finishing or the leak itself, and O1 cannot tell which.

The binding constraint is computed and is the most useful column on the screen:
`effective_limit` is `max(0, min(candidates))` over the non-`None` members of
`{hard_limit, adaptive_target, quota_derived_limit}` (`models.py:65-72`), so the row says
**which** of the three is binding — `capped by quota_derived_limit=0` reads very
differently from `capped by hard_limit=20`. Three rules the client must not get wrong:
`adaptive_target` and `quota_derived_limit` arrive as JSON `null` when unset and are
**skipped**, never coerced to 0; `hard_limit == 1_000_000` renders as `uncapped` (trap 2b);
and if no candidate equals `effective_limit`, the column says `constraint unclear` rather
than picking one.

Below the list: `14 other pools have headroom` as a tappable disclosure.

**Row 4 — the source strip.** One chip per source **this screen actually reads**:
`pools`, `dispatch`, `stats` — three `swarm-api` routes, all Firestore-backed. Green =
read OK with its age; amber = stale; hatched red = unreadable, with the error code. This
is the row that makes 0.2 real. It is never collapsed, never behind a tap, on any
viewport. It does **not** carry Cloud Run, GKE or Monitoring chips: O1 reads none of them,
`swarm-api` cannot read two of them at all today, and a permanently grey chip for a source
nothing queried is the same lie in the other direction. Those chips arrive with D1.

#### Where every value comes from

| element | source |
|---|---|
| dispatch pill, actor, reason | `GET /v1/admin/dispatch` → `routes/admin.py:70-75` → `store.get_control()` (`store.py:707-714`) → Firestore doc `control/dispatch`, fields `dispatch_paused`, `updated_at`, `updated_by`, `reason`. **`updated_by` is `auth.email` of whoever called the route** (`routes/admin.py:83`, `:94`), so a pause made by `pause-swarm.sh` writing Firestore directly carries no actor — see D3 |
| dispatch "unknown" state | requires the route to report whether the document exists; `get_control` currently substitutes `dispatch_paused: False` (trap 1b) |
| pool rows | `GET /v1/admin/pools` → `routes/admin.py:296-302` → `store.list_pools()` → collection `pools`; serialised by `pool_to_api` (`codec.py:215-226`): `name, hard_limit, adaptive_target, quota_derived_limit, effective_limit, active, available, enabled, updated_at` |
| binding constraint | computed client-side from those fields, per the three rules above |
| oversubscribed flag | computed client-side: `active > effective_limit`, both from `pool_to_api` |
| holding capacity | `active` of the pool named `global` |
| task counters | `GET /v1/stats` → `routes/platform.py:13-19` → `service.stats()` (`service.py:271-288`) → `platform_tasks_by_state` (admin) or `tasks_by_state` |
| database name, project | `GET /readyz` (`routes/health.py:42-53`) — unauthenticated, returns `database` and `project` |
| environment (`dev`/`prod`), region | **nowhere.** See below |
| source strip | the `status`/`error`/`age_seconds` of each envelope (§0.2) |

#### What it cannot show yet

The previous draft of this section said "Nothing". That was wrong in four places, and
each is a prerequisite rather than a design note:

* **`environment` and `region` are not on any route.** They exist on
  `swarm_common.config.Settings` (`config.py:35-36`, read from `REGION` and `ENVIRONMENT`
  at `:107-108`) and are never serialised by anything. `/readyz`
  (`routes/health.py:42-53`) returns `database` and `project` only. Until a route emits
  them, the environment chip renders `dev`/`prod` and the region as **`not reported`** —
  it does not infer the environment from the database name, because inferring "prod" and
  being wrong is how someone pauses the wrong platform.
* **The dispatch pill cannot distinguish "not paused" from "no control document"**
  (trap 1b) until `GET /v1/admin/dispatch` reports document existence. Until then the pill
  shows `DISPATCHING (unconfirmed)` rather than plain green.
* **Truncation of the pool list is not detectable from the response** (§0.3): the route
  returns neither the cap nor a count, so `truncated` cannot be computed without
  hard-coding `500`. Until the route echoes its cap, the pool section carries a standing
  footnote `showing up to the server's page limit` rather than a `truncated` flag it
  cannot honestly set.
* **`managed_by` per pool** (trap 2) until `store.list_pools_raw()` exists. Until then a
  pool created by an admin PUT and one created by terraform look identical.

And, by design rather than by gap:

* O1 deliberately **omits** the lease-vs-`active` delta, which is the most valuable number
  an operator could have. That lives on O2 and needs a route. O1 reserves the slot and
  shows `Ledger check: unavailable — GET /v1/admin/leases not built` rather than nothing,
  so the absence is visible rather than assumed healthy.
* `updated_at` is **not** rendered as "last changed" (trap 1).

#### Refresh and cost

* `GET /v1/admin/pools`: one collection query, ~19 documents in dev. Trivial.
* `GET /v1/admin/dispatch`: one document read.
* `GET /v1/stats` as admin: **24 aggregation queries** (trap 8). Aggregations bill one read
  per 1000 index entries scanned, minimum one.
* **Budget: pools and dispatch at 10s; the task counters at 60s, on their own timer.** Do
  not put them on one poll. At 10s a single viewer would issue 8,640 admin `/stats` calls a
  day = 207,360 aggregation queries.
* **At 100x:** pools at 200 tenants is ~814 documents and `list_pools` truncates at 500
  with no marker (§0.3) — **this breaks first, and silently.** Fix before scale, not after.
  The counters degrade next: 12 aggregations over a million tasks is ~1000 index entries
  per state per scan. The counters should move to a periodically-materialised
  `platform_snapshots/task_counts` document rather than 24 live aggregations per viewer.

#### Phone layout (390pt viewport, 16px gutters → 358pt content)

* **Two sticky elements, one budget.** The §0.2 page banner and Row 1 cannot both pin on a
  390pt viewport. When ≥1 region is unreadable, **the failure banner takes the sticky slot
  and Row 1 scrolls**; the dispatch pill is mirrored into the banner as a chip so the one
  piece of Row 1 an operator needs during an incident is still pinned. When everything
  reads, Row 1 is sticky and there is no banner. The two never stack.
* Banner: two lines. Line 1 = environment chip + dispatch pill. Line 2 = "as of" +
  freshness dot. The pause reason wraps onto a third line when paused; when not paused it
  is absent.
* Counters: 2×2 grid, 171pt cards, numeral at 34pt, label at 12pt uppercase. The
  "currently dead-lettered" label wraps to two lines at 12pt rather than truncating —
  "dead-lettered" alone would restore the all-time reading the label exists to prevent.
* Pool groups: **card stacks, never a table**, one stack per group, each with its own
  header (`Oversubscribed 1` / `Full 3` / `Drained 2`) so the groups survive the loss of
  colour and column alignment. Each card is one pool: name on line 1 (truncated from the
  left, so `provider:anthropic:tenant:u-bogdan` shows the tenant), the bar full-width on
  line 2, `12/12 · capped by quota_derived_limit` on line 3. A drained card adds line 4:
  `2 lease(s) still holding (pool counter)`. An oversubscribed card's bar overflows its
  track in the alert colour rather than clamping at 100%.
* Tap opens the **pool detail sheet**: every field of `pool_to_api` with its read status,
  the binding-constraint derivation shown as the three candidates with the winner marked,
  `updated_at` labelled per trap 1, and — once the routes exist — `managed_by` and the
  O2 ledger check. Nothing in the sheet is a number O1 does not already have.
* `14 other pools have headroom` stays collapsed on phone by default; expanded by default
  on desktop.
* The **Ledger check: unavailable** slot is not dropped on phone. It renders as a single
  full-width hatched strip under the pool stacks, one line: `Ledger check unavailable —
  GET /v1/admin/leases not built`. An absence that is invisible on the smaller screen is
  the assumption this whole section exists to prevent.
* The **truncation footnote** renders as a full-width line directly under the last pool
  stack, at body weight in the alert colour — never as a badge on the section header,
  where it would be the first thing dropped at 358pt.
* **The source strip stays visible on phone.** It compresses to three dots with a count
  (`● ● ⨯  1 unreadable`) and taps to a sheet. It does not get dropped — it is the one
  element whose absence reproduces the bug.
* Nothing on this screen scrolls horizontally. No table appears below 640pt.

#### Empty, loading and broken

* **Loading:** skeletons shaped like the real rows, with the source strip already showing
  `reading…` per chip. No spinner over the whole page — a whole-page spinner hides which
  source is slow.
* **Empty — genuinely:** `no pool is constrained` in normal weight, with the total pool
  count beside it so "none constrained" is distinguishable from "none read". Zero tasks in
  every state renders `0` in each counter with `queried at HH:MM:SS` beneath. A drained
  group with no members is absent, not rendered as "0 drained".
* **Broken:** per §0.2. Specifically: if `GET /v1/admin/pools` fails, the pool section is
  hatched and says `pools unreadable: PERMISSION_DENIED`, and the **holding capacity**
  counter goes to `—` because its input is unreadable — it does not render `0`.
* **Partial:** if pools read but `/stats` fails, the pool section renders normally and only
  the counters hatch. The banner says `1 of 3 panels could not be read`.
* **Dispatch unreadable:** the pill is hatched and reads `DISPATCH STATE UNREADABLE` with
  the error code. It never falls back to green. A red `PAUSED` fallback is equally
  forbidden: guessing paused makes an operator stop investigating a platform that is
  dispatching.
* **Truncated:** when `len(pools)` equals the server's cap, the pool section keeps rendering
  the rows it has but **replaces every count derived from the whole list with `—`** — the
  `14 other pools have headroom` disclosure becomes `unknown — the pool list was
  truncated`, and the per-group headers show `≥1` rather than `1`. The rows themselves are
  still true; the totals are not, and rule 3 says a total with a hole is not a number.
  Because the response cannot currently prove truncation (§0.3), this state is reachable
  only once the route echoes its cap; until then the standing footnote stands in for it.
* **A field the API defaulted:** where a route reports a field absent (traps 1, 1b, 2b),
  the cell reads `not set` in the "empty" treatment, never the default value. This is
  rule 6, and it is the difference between "this pool is capped at 0" and "this pool's
  document has no `hard_limit`".
