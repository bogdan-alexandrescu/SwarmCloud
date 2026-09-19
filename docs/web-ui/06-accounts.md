## Claude subscription account management

### 0. The honest starting position

This is the section the owner asked for by name ("comparable to claudeswitch: quota per session, per week, refresh TTL") and it is the area where the gap between what looks available and what is actually written is widest.

What is real today, verified in the code:

- A Firestore collection `accounts`, document id `<owner_tenant>:<label>` (`apps/quota-broker/quota_broker/accountstore.py:40`, `apps/quota-broker/quota_broker/accounts.py:291-293`).
- Seven fields on it that a live code path actually writes: `account_id`, `owner_tenant`, `label`, `provider`, `lend_to[]`, `state`, `reason` — written by `AccountStore.register()` (`accountstore.py:83-134`) and `AccountStore.set_state()` (`accountstore.py:136-139`), both reached only from `scripts/account.sh`.
- A refresh sweep that visits every account every 5 minutes (`apps/quota-broker/quota_broker/main.py:500-551`, `terraform/modules/scheduler/variables.tf:122-125`).
- Two secrets per account, `swarm-account-<tenant>-<label>` and `…-refresh`, with a deliberate IAM split (`scripts/account.sh:102-141`).

What is **not** real, and would render as a confident lie if a screen bound to it:

| Field | Writer | What a naive UI would show |
|---|---|---|
| `windows{five_hour,seven_day}` | none — `record_reading()` (`accountstore.py:151-184`) has zero callers | empty gauges |
| `observed_at` | none | "just now" or blank |
| `assigned` | none — no increment/decrement exists anywhere | permanent `0` agents |
| `Account.headroom()` | derived from the above; returns a hardcoded `1.0` when `windows` is empty (`accounts.py:182-186`) | **every account solid green at 100%, forever** |
| `Account.next_reset()` | returns `None` for every account | every reset column `—` |
| `AccountState.REAUTH_REQUIRED` | never written by anything (`main.py` never calls `set_state`) | a dead account displayed as `AVAILABLE` with no reason |

`scripts/account.sh list` (lines 333-342) already renders exactly this table off exactly this unwritten data, which is why it prints `100%` and `-` for every account today. **A web UI that copies that table copies a fabrication.** The single most important rule in this section is therefore mechanical, not aesthetic:

> **The accounts API must not serve `headroom` or `next_reset` at all until a reading pipeline exists.** Not as `null`, not as `1.0` — the field must be absent from the response body, so that no frontend can bind to it by accident and no reviewer can mistake the optimistic default for a measurement. The API serves `observed_at: null` and `windows: {}` and the UI renders an explicit *not measured* state.

The same rule has a second half that is easy to miss: **a state that nothing writes must not be counted, either.** A chip reading `0 need reauth` is a measurement claim, and until P3 lands it is a false one — no account can ever enter `REAUTH_REQUIRED`, so the zero means "never checked", not "all healthy". Every count, filter and chip in this section is gated on its writer existing, not just every gauge.

There is also **no HTTP surface for accounts at all**. The broker exposes `/healthz`, `/readyz`, `/metrics`, `/v1/quota*` and `/v1/quota/sweep` (`main.py:376-551`); swarm-api's routes are health, tasks, workflows, tenants, platform, admin. Every screen below is therefore blocked on at least the read API. I have split the blockers into two tiers and kept them separate throughout, because they are days apart in cost:

- **Plumbing** (P1, P2, P3, P6, P7, P8, P9, P10) — exposing or persisting data the platform already computes. The store methods exist and are the same ones `account.sh` uses.
- **Capability** (P4, P5, P11) — the account pool's §2.6 design from `docs/BUILD_PROMPT_V2.md:185-334`, specified and never built, plus the browser onboarding path. Readings, assignment and credential intake are new systems, not new endpoints.

---

### 1. Prerequisites, in dependency order

**P1 — an accounts API.** `list` / `get` / `set state` / `set lend_to` / `remove`. The store methods already exist (`accountstore.py:50-187`); this is routes, schemas and a codec.

Route shape — recommend **two path segments, not the colon id**:

```
GET    /v1/admin/accounts
GET    /v1/admin/accounts/{owner_tenant}/{label}
POST   /v1/admin/accounts/{owner_tenant}/{label}/state
PUT    /v1/admin/accounts/{owner_tenant}/{label}/lend
DELETE /v1/admin/accounts/{owner_tenant}/{label}
```

`account_id` contains a colon (`accounts.py:291-293`). A colon is legal in a path segment per RFC 3986 but is percent-encoded inconsistently by proxies and by `fetch`, and IAP + an external ALB sits in front of this. Two segments avoid the question entirely, and both segments are already regex-validated server-side (`accounts.py:56` for the label, `account.sh:76` for the tenant). All routes sit behind `admin_auth` (`apps/swarm-api/swarm_api/deps.py:190-191`), which means Google identity plus `ADMIN_GROUPS` membership (`settings.py:70`, `auth.py:225-226`).

**P1a — where the Account model lives. This is an owner decision, presented in §8.**

**P2 — persist the refresh outcome on the account document.** The sweep already has `expires_at` in hand: `RefreshOutcome` carries it (`credentials.py:59-74`), `main.py:255-273` throws it away into `{examined, refreshed, reauth_required}`, and that summary is returned to Cloud Scheduler, which discards it. Write back, per account, on every sweep:

```
last_refresh_at        timestamp   (sweep wall clock)
token_expires_at       timestamp   (RefreshOutcome.expires_at)
last_refresh_reason    string      (one of: no_refresh_credential, store_unavailable,
                                    unreadable, still_valid, published, publish_failed,
                                    reauth_required, refresh_failed, refreshed, error)
last_refresh_account_id string     (see below)
```

Note the bug this must fix on the way: `sweep_accounts` is handed `(secret_base, label)` tuples (`main.py:537-546`) and `refresh_secret` sets `tenant_id=label` (`credentials.py:220`), so the `reauth_required` list contains **labels, which are not unique across tenants**. Two tenants can both have an account labelled `personal`. The sweep must carry `account_id` to write anything back correctly.

**P3 — write `REAUTH_REQUIRED`.** One `set_state` call in the sweep loop, keyed by `account_id` (needs P2's plumbing). Today the sweep detects a dead refresh token (`credentials.py:174-180`), logs it, counts it, and leaves the document at `AVAILABLE` — so `due_for_refresh()` (`accounts.py:338-356`) retries the dead token every five minutes forever, which is precisely what the module's own docstring says must not happen. This is audit finding 1 in `docs/audits/2026-09-18/06-quota-broker-accounts.md`. **Without P3 the one state the UI most needs to surface can never appear, and every count and filter built on it is a zero that means nothing.**

P3 must also ship with its exit, or it replaces a silent failure with a permanent one: `due_for_refresh()` **excludes** `REAUTH_REQUIRED`, so once the sweep writes it, the sweep will never touch that account again. The only writer back to `AVAILABLE` is `set_state` — i.e. `account.sh resume` or the UI's Resume action. See ACC-2 for the recovery wording, which the current draft of this spec got wrong.

**P4 — the reading pipeline.** This is the big one and it is four separate pieces:

1. The worker runs `--output-format json`, not `stream-json` (`apps/agent-worker/agent_worker/runners/claude_code.py:33-37`). `rate_limit_event` is a stream event (`docs/BUILD_PROMPT_V2.md:307-334`, event table at §2.7:337-359); it is **not in the output the worker asks for today**. Switching the format changes transcript handling and `_summarise`/`_looks_complete` (`cliagent.py:371-400`), which currently key off a single JSON object.
2. Nothing parses utilization. `cliagent.py:64-93` does substring matching on `429` / `rate limit` / `retry-after` and regexes a retry-after integer. There is no `utilization` parser anywhere in `apps/`.
3. Parse **before** truncating. `_tail()` keeps the last 8000 bytes (`cliagent.py:202-205`) and the transcript artifact is capped at 4 MB (`cliagent.py:328-329`). A `rate_limit_event` emitted early in a long run is already gone by the time anything downstream sees the output. (This is the same defect the accepted token/cost change request is about; the fix is one parse pass over the stream as it is produced.)
4. A broker ingest endpoint and an attribution identity. `record_reading` has no HTTP surface, and the worker has no account identity to report under — `kubernetes/render.py:279` defaults `ACCOUNT_LABEL` to the literal string `"unassigned"`.

Also required before the first multi-window reading arrives: `Account.with_reading` full-overwrites the `windows` dict (`accounts.py:225-237`), so a later reading carrying only `seven_day` silently erases a binding `five_hour`. Audit finding 4. Fix to a per-key merge while it is still unreachable.

**P5 — account-to-agent assignment.** `choose()` (`accounts.py:296-335`) has no production caller. Dispatch mounts the *tenant* secret `swarm-tenant-<tenant>-<provider>` (`apps/scheduler/scheduler/dispatch.py:285-296`), not an account secret. `Lease` (`apps/common/swarm_common/models.py:114-135`) has no account field. **This is a frozen-contract change**: recording the assignment on the lease means a field on `Lease`, and probably on `Attempt` too, and `apps/common/swarm_common/` may not be edited — it goes in `docs/contract-change-requests.md` as a request, alongside the accepted token/cost one.

**P6 — secret version metadata.** To show "access token published 4 minutes ago" without ever touching a payload. Verified against the live role definitions: `secretVersionAdder` = `{secrets.rotate, versions.add}` and `secretAccessor` = `{versions.access}`; neither grants `versions.list` or `versions.get`. The broker's custom role grants only `secretmanager.secrets.list` and deliberately excludes payload access (`terraform/modules/iam/custom_roles.tf:136-156`). A new custom role with `versions.list` + `versions.get`, bound **per account secret** by `account.sh`'s `grant_access`, is metadata-only. This is Track C work.

**P7 — safe mutation from a multi-user UI.** `set_state` is a blind `.update()` with no precondition and no actor (`accountstore.py:136-139`); two admins racing both win and neither is recorded. `remove()` is a blind delete with no check that the account is `DRAINING` or idle (`accountstore.py:141-149`). The only guardrail today is a typed confirmation in bash that deliberately ignores `SWARM_ASSUME_YES` (`account.sh:409-417`) — a browser inherits none of it. Needs: an `expected_state` precondition in a transaction, an actor field, and an account-action audit trail mirroring `admin.py`'s `by=auth.email` + `ctx.metrics.admin_actions.labels(action=…)` pattern (`routes/admin.py:84-87`). Also: `update()` raises `NotFound` on a missing document, which is why `cmd_state` pre-reads (`account.sh:367-368`) — the API must do the same or return 500 for a typo.

P7 also has to **define the legal transitions, server-side, for the first time.** There is no transition table in the code today: `account.sh` maps `pause`/`resume`/`drain` to three unconditional `set_state` calls (`account.sh:422-424`), so every transition is currently "legal". The UI must not be the place that invents the rules — that is a fourth restatement of contract logic in a codebase that already has `scripts/lib/check-contract-parity.sh` because of the first three. The API owns the set; the UI disables an action because the API told it the transition is not allowed, not because a constant in the frontend says so.

**P8 — `list()` must report what it skipped, and which.** `AccountStore.list()` swallows `KeyError`/`ValueError` per document with a `log.warning` and returns the rest (`accountstore.py:61-68`). A UI built on it shows four accounts when there are five and says nothing. Return **both** a count and the skipped document ids — `list()` already has `doc.id` in hand at the point it logs, and a count alone tells an operator that something is wrong without telling them which account to go look at. This is the section's instance of the platform's defining bug, inside the store itself.

**P9 — refuse colliding secret names at registration.** `secret_name()` concatenates with dashes (`accounts.py:97-102`); tenant ids and labels both permit internal dashes, so tenant `acme-prod` + label `x` and tenant `acme` + label `prod-x` derive the **same secret**, across a tenant boundary. Audit finding 2 calls it a direct invariant-9 violation requiring no attacker. A CLI makes it rare; **a form with two adjacent text boxes makes it routine**, so the add flow must not ship without the refusal.

The refusal lives on the server and nowhere else. The label regex, the tenant regex and the secret-name format are each already written twice — Python (`accounts.py:56`, `accounts.py:97-102`) and bash (`account.sh:66-78`, `account.sh:84-86`). A client-side copy in a web form is a third, and this is the exact drift `scripts/lib/check-contract-parity.sh` exists to catch. **The form validates by calling the API** — a debounced `POST …/validate` or an optimistic submit that renders the 422 — and never by re-implementing the pattern in JavaScript.

**P10 — swarm-api → broker service-to-service auth.** See §8; only needed if the owner picks the proxy option. Note that swarm-api verifies Google ID tokens but does not mint them (`auth.py:94-97` is verification only), and that adding its SA to `PLATFORM_SERVICE_ACCOUNTS` would be wrong — that list also authorizes `PUT /v1/quota/{provider}/{tenant}/hard-max` and `POST /v1/quota/sweep` (`main.py:483-551`), so it needs its own narrower allowlist.

**P11 — "add an account" from a browser, if it is ever to exist.** The current draft assumed this away; it is the largest single unlisted blocker in the section. Two independent obstacles:

1. **Policy.** `accountstore.py:91-96` and `account.sh:10-13` both state that the credential value must never pass through a component whose job is bookkeeping. A browser add makes the browser and swarm-api handle a live Claude refresh token, which is standing access to the account — `terraform/modules/secret_manager/main.tf:137-140` calls the refresh secret strictly *more* sensitive than an API key.
2. **IAM.** Creating the two secrets and setting their bindings needs `secretmanager.secrets.create` and `setIamPolicy`, which no platform service account holds: secret administration is granted to a named human (`terraform/environments/dev/dev.tfvars:244-246`, `user:admin@saga.xyz`).

**Recommended scope cut for v1: the browser never sees a credential.** Add is a *guided* flow that collects tenant, label, provider and lend-to, validates them against the API (P9), and then renders the exact `scripts/account.sh add` command to run in a terminal, with a copy button. Everything after onboarding — state, lending, removal, health — is manageable in the browser. If the owner instead wants a true browser add, that is a security decision plus new IAM, and it should be taken deliberately in §8 rather than arrived at by a designer drawing a form.

**Not verified:** the existing `POST /v1/tenants/me/credentials` path calls `create_secret` and `set_iam_policy`, and `terraform/modules/iam/bindings.tf:73-104` does not appear to grant the swarm-api SA either permission. Either that route is broken in deployment or there is an out-of-band grant. **Check which before citing it as the precedent for anything.**

---

### 2. ACC-1 — Account pool (the list)

The section's home screen. One dense table on web, cards on phone.

#### What the user sees

Header strip, count chips, always visible: **`5 accounts · 4 available · 1 paused · 0 draining`**. Counts are computed client-side from the same payload, never a second query.

**There is no "needs reauth" chip until P3 lands**, and no `0` in its place. Nothing writes `REAUTH_REQUIRED`, so a zero there is not a reassurance, it is an unmeasured field wearing the costume of one — the same failure as a green headroom bar. Post-P3 the chip appears and is red when non-zero. Pre-P3, the header carries one small dimmed note instead: *"reauth detection not wired (P3) — a dead account still shows AVAILABLE."*

Table columns, web, in this order:

| Column | Value | Source |
|---|---|---|
| Account | `label`, monospace | `accounts/{id}.label` |
| Owner | `owner_tenant` | `.owner_tenant` |
| Provider | `anthropic` | `.provider` (defaults `"anthropic"`, `accounts.py:135`) |
| State | chip: `AVAILABLE` green / `PAUSED` amber / `DRAINING (advisory)` blue / `REAUTH_REQUIRED` red | `.state` |
| Reason | free text, one line, expandable on click or tap | `.reason` |
| Lent to | tenant chips, or `—` when empty | `.lend_to[]` |
| Session (5h) | **not measured** chip | `.windows.five_hour` — empty until P4 |
| Week (7d) | **not measured** chip | `.windows.seven_day` — empty until P4 |
| Token | **unknown** chip | `token_expires_at` — does not exist until P2 |
| Agents | **not tracked** chip | `.assigned` — permanently 0 until P5 |

The four right-hand columns ship as explicit unavailability chips, not blanks and not zeros. Each chip is a tap/click target opening a short "why this is empty" popover naming the prerequisite — a `title` tooltip is not enough, because there is no hover on a phone and this is the text that stops someone trusting the column. That is uglier than a green bar and it is the only honest rendering: `headroom()` returns `1.0` for an unobserved account **by design** (`accounts.py:182-186`), so the pretty version is a lie the code will happily produce.

`Reason` gets the same treatment for the same reason: it is the field that tells an operator what to do, and a hover-only truncation hides it from half the audience.

Row affordances: whole row opens ACC-2. Kebab menu offers Pause / Resume / Drain / Edit lending / Remove. An action is disabled **because the API said the transition is not allowed from the current state** (P7), with the reason shown inline in the menu row — not in a hover tooltip, and not from a rules table living in the frontend.

Two actions carry a warning in their confirm step, not a tooltip:

- **Drain** — advisory only. Nothing moves agents off an account because nothing ever put them on one (P5). The dialog says so in words: *"This marks the account as draining. No agent will be moved — nothing assigns accounts yet."* Without that sentence an operator will drain an account, see the chip change, and believe it emptied.
- **Remove** — *"This forgets the account. The secret `swarm-account-<tenant>-<label>` is left in place, on purpose: a credential destroyed by a mistyped label cannot be recovered."* That is `accountstore.py:141-149` and `account.sh:399-404`, and it must be said on screen. **Deleting the secret is not an action this UI offers at all** — it stays a separate, explicit, typed act at a terminal.

Filters: segmented `All | Available | Paused | Draining`; a tenant typeahead; a free-text match over label + owner + reason. `Needs reauth` joins the segmented control when P3 lands — a filter that can only ever return zero rows teaches the operator that the pool is healthy. All client-side over the full payload — see cost below.

Sort default: `owner_tenant`, then `label` — the same ordering `account.sh:334` uses, so the CLI and the UI list accounts in the same order.

#### What this screen must never offer

- **A "refresh now" button that does its own token exchange.** The broker is the platform's single writer for these credentials because refresh tokens rotate and two writers invalidate each other (`credentials.py` module docstring; `oauth.py:8-32`). A UI that exchanges a token races the sweep and can kill a healthy account.
- Even "run the sweep now" is not free: `POST /v1/quota/sweep` requires a platform caller (`main.py:500-551`), and swarm-api is not one — see P10 for why widening `PLATFORM_SERVICE_ACCOUNTS` to get it would also hand swarm-api the hard-max write. If a manual sweep trigger is wanted, it is its own narrowly-scoped endpoint, not a reuse of that list.
- **Any path to a credential payload.** There is no read path anywhere in the platform and it is enforced structurally, not by convention (`swarm_api/credentials.py:23-25`). The UI shows secret *names* and, post-P6, version *metadata*. Never bytes, never a length.

#### Where every value comes from

`GET /v1/admin/accounts` → `AccountStore.list()` → unfiltered `.stream()` of collection `accounts`. Response envelope (P1 + P8):

```json
{
  "accounts": [
    {"account_id":"u-bogdan:personal","owner_tenant":"u-bogdan","label":"personal",
     "provider":"anthropic","state":"AVAILABLE","reason":"","lend_to":[],
     "observed_at":null,"windows":{},
     "secret":"swarm-account-u-bogdan-personal",
     "refresh_secret":"swarm-account-u-bogdan-personal-refresh"}
  ],
  "unreadable": 0,
  "unreadable_ids": [],
  "as_of": "2026-09-19T10:14:03Z"
}
```

`secret` and `refresh_secret` are **derived server-side** from `secret_name()` (`accounts.py:97-102`), never echoed from a client and never resolved to a payload. No field named `headroom`, `next_reset` or `assigned` appears until its writer does.

#### What it cannot show yet

Session and weekly utilization, reset times, token freshness, agents-per-account. P4, P2, P5 respectively. `REAUTH_REQUIRED` cannot appear at all until P3. And `DRAINING` is a label and nothing more: nothing moves agents off an account because nothing ever put them on one, which is why the chip reads `DRAINING (advisory)` and the action confirms in words.

#### Refresh and cost

One unfiltered collection stream per call. Today's cardinality is one document per subscription account — five real claudeswitch accounts (`docs/BUILD_PROMPT_V2.md:188`), realistically under fifty. A full scan per page load is free at that size. Cache the response 30 s server-side; poll at **60 s**, or not at all — nothing writes these documents except an operator running `account.sh`, so there is nothing to animate. Post-P2 the sweep writes every 5 minutes; post-P4 readings arrive per agent turn, and 20-30 s becomes the right interval.

At 100×: ~5000 documents streamed per uncached request. That is a real bill under a 5 s poll and a genuine latency problem. The fix is ordinary — a `limit`/`start_after` page over `__name__`, the existing `paged_limit` helper (`deps.py:194-199`), and client-side filters replaced by server-side ones. Flag now: **`AccountStore.for_tenant()` filters in Python after streaming everything (`accountstore.py:77-79`)**, so a per-tenant view costs a full scan per viewer. At 100× that needs two Firestore queries (`owner_tenant ==` and `lend_to array-contains`) merged, because Firestore cannot OR them in one.

Never: a Cloud Logging query on this path. The per-account refresh detail survives only in log records today (`oauth.py:94-100`), and a Logging API query per account behind a dashboard poll is seconds of latency, rate-limited, and metered. Persist the fields (P2) instead.

#### Phone layout (390pt)

The table becomes cards. Ten columns is not a phone screen.

```
┌─────────────────────────────────┐
│ personal              [AVAILABLE]│   label 17pt semibold, chip right
│ u-bogdan · anthropic             │   13pt secondary
│ 5h —  ·  7d —  ·  token —        │   "not measured" row, 12pt, dimmed
│ ⚠ paused by an operator          │   reason line, amber, only when set
└─────────────────────────────────┘
```

Always visible: label, state chip, owner. Collapsed behind the tap: lend_to, secret names (long, wrap badly, low frequency), provider when it is the default. The count chips become a single sticky summary line; the filter segmented control lives in a sheet behind a filter icon carrying a dot when active. Row actions are a bottom sheet, never a hover kebab — and a disabled action keeps its row in the sheet with the reason printed under the label, because there is nowhere for a tooltip to go. Minimum 44pt targets throughout.

The dimmed `5h — · 7d — · token —` row is itself a tap target opening the same "why this is empty" sheet the desktop popover shows. The truncated reason line expands on tap. Banners (below) render full-width above the cards and are dismissible only for the session, never permanently.

#### Empty, loading and broken

Five distinguishable states, never collapsed into one:

1. **Loading** — skeleton rows, count chips as shimmer. Never an empty table with a spinner outside it.
2. **Empty (HTTP 200, `accounts: []`)** — "No accounts registered." plus the guided add flow (P11) and the exact `scripts/account.sh add` command with a copy button. Explicitly worded as a fact about the pool.
3. **Failed (5xx, `upstream_unavailable`, or a network/timeout error)** — a red banner with the `code` from the error envelope (`errors.py:14-67`: `unauthenticated`, `forbidden`, `not_found`, `conflict`, `rate_limited`, `upstream_unavailable`, …), the HTTP status, the `as_of` of the last good payload, and a Retry button. **The table below it keeps the last good rows, greyed, stamped "as of 10:14 — not current."** It must never fall back to zero rows: "the query failed" and "no accounts" are opposite facts and this platform has shipped that confusion repeatedly.
4. **Refused (401 `unauthenticated` / 403 `forbidden`)** — a distinct state, not a red error with a Retry button. `admin_auth` requires `ADMIN_GROUPS` membership (`deps.py:190-191`, `auth.py:225-226`), and retrying a 403 will never succeed. The page says which group membership is missing and offers a sign-in (401) or a contact route (403), with no Retry.
5. **Degraded (HTTP 200, `unreadable > 0`)** — an amber banner: "Showing 4 of 5 accounts. 1 document could not be read." with the skipped document ids from `unreadable_ids`. This is the P8 case and it is invisible today by construction.

A sixth, subtler one: if `last_refresh_at` (P2) is older than 15 minutes on **every** account at once, that is not five sick accounts, it is a dead sweep. The header shows one banner — "No account has been refreshed since 09:31; the 5-minute sweep may not be running" — rather than fifty red chips. The sweep failing silently for 403 is exactly what happened to `/v1/quota/sweep` before (`terraform/infra/locals.tf:371-377`).

---

### 3. ACC-2 — Account detail

A right-hand drawer on web (480pt), a full sheet on phone. Opened from any row.

#### What the user sees

**Identity** — `account_id`, owner tenant, label, provider, `lend_to` as removable chips with an "Add tenant" control.

**Credential** — the two derived secret names, each with a copy button and a "never readable from here" note:

```
swarm-account-u-bogdan-personal            access token   worker reads · broker writes
swarm-account-u-bogdan-personal-refresh    refresh token  broker only — the worker is absent
```

The absence of the worker from the refresh secret is the isolation property (`account.sh:130-141`), so it is stated on screen, not left implicit. Neither secret is ever fetched; the UI has no path to a payload and swarm-api has no function that returns one (`swarm_api/credentials.py:23-25`).

**Refresh health** (P2) — `Last refresh`, `Outcome` chip, `Token expires`, `Refresh in`. Outcome chip mapping, from the ten `RefreshOutcome.reason` values (`credentials.py:100-209`):

| reason | chip | meaning shown |
|---|---|---|
| `refreshed`, `published` | green | token rotated / published |
| `still_valid` | green, muted | nothing to do |
| `no_refresh_credential` | amber | no refresh secret — this account cannot self-heal |
| `unreadable` | red | the stored credential is not a usable JSON credential |
| `reauth_required` | red | **a human must re-authenticate** |
| `refresh_failed`, `publish_failed`, `store_unavailable`, `error` | amber | transient; retries next sweep |

Those are three genuinely different situations and three different actions. Collapsing them into "error" throws away the whole point of the reason field.

**Token freshness** (P6) — "access token version published 4 min ago" from `secretmanager.versions.list` on the base secret, metadata only, one call, on the detail view only. Never on the list.

**Quota** (P4) — see ACC-5; renders inside this drawer.

**State** — the four states with their real meanings, and the legal transitions as buttons, with the legal set supplied by the API (P7). Pause = no new assignments, running work stays (`ASSIGNABLE_STATES` / `USABLE_STATES`, `accounts.py:86-90`). Drain = no new work and get off, *advisory today*. Resume = back to `AVAILABLE`.

`REAUTH_REQUIRED` is written by the sweep (P3), never by a person — there is no command or button that sets it. **Recovering from it takes two steps, and the panel must say both**, because `register()` deliberately preserves `state` on re-registration (`accountstore.py:106-124`, `state=current.state`): re-running `account.sh add` with a fresh credential does **not** clear it. The account stays `REAUTH_REQUIRED`, and because `due_for_refresh()` excludes that state (`accounts.py:338-356`), the sweep will never look at it again. The panel's wording:

> 1. Re-add the credential from a terminal: `scripts/account.sh add --tenant … --label … --from-claudeswitch …`
> 2. Then **Resume** this account — until you do, the 5-minute sweep skips it and the new credential is never published.

An earlier draft of this spec said re-adding the credential clears the state by itself. It does not, and shipping that sentence would have produced a runbook step that leaves the account permanently dead.

**History** — does not exist. See §7.

#### Where every value comes from

`GET /v1/admin/accounts/{tenant}/{label}` → `AccountStore.get(account_id)` — a **single document read**. Secret names derived. Version metadata from Secret Manager (P6). Nothing here is a query.

#### Refresh and cost

One document read plus, post-P6, one `versions.list` per open. Poll 30 s while open, stop when the drawer closes. At 100× nothing changes: it is a keyed read.

#### Phone layout

Full-height sheet, sections collapsible, **Credential** and **State** expanded by default, **Identity** and **Refresh health** collapsed under summary lines ("Last refresh 3 min ago · refreshed"). Secret names get their own row with a copy button and `text-overflow: ellipsis` from the middle, so `…-personal-refresh` stays legible — the distinguishing part is the suffix. Actions pinned to the bottom of the sheet above the safe area, each with its own confirm step; the Drain and Remove warnings above are full sentences in the confirm sheet, not tooltips.

#### Empty, loading and broken

1. **Loading** — the drawer opens immediately with the identity fields already known from the list row, and skeletons only for what needs the fetch (refresh health, token freshness, quota). Opening onto a blank panel loses the one thing the user already had.
2. **Gone (404 `not_found`)** — the document has been removed since the list was fetched. "This account is no longer registered", a "Back to pool" action, and the pool list refreshed behind the drawer. Not a blank drawer, and not a silent close.
3. **Refused (403 `forbidden`)** — same treatment as ACC-1 state 4: no Retry, name the missing membership.
4. **Failed (5xx / network)** — keep the fields fetched last, grey them, stamp "as of 10:14 — not current", and offer Retry. The drawer must never render a field as empty because the fetch failed: `no reason recorded` and `could not load` are different facts, and the second is the one that matters.
5. **Partial (P6 metadata call fails independently)** — the token-freshness line alone degrades to "version metadata unavailable (`<code>`)" while everything else renders normally. It is a separate call to a separate service; one Secret Manager permission error must not blank a drawer whose other half came from Firestore. And "unavailable" is distinct from "no version has ever been published", which is a real and different state the operator has to be able to see.
6. **Not measured vs. measurement failed** — for quota (P4), the drawer shows the same explicit *not measured* state as the list, naming the prerequisite. Once P4 exists, a reading that is absent because the pipeline is broken must not reuse that wording: "no reading yet" and "readings stopped arriving at 09:40" are different, and the second is an incident.

Polling while the drawer is open follows the same rule as the list: a failed poll never blanks a populated panel, it ages it visibly and keeps the last good `as_of` on screen.
