# LLM providers: plan accounts and API keys as two pools, a plan-then-key-then-Voice-it fallback, and Capacity › LLM Providers

The owner's request, 9 November 2026 (kept in the owner's words):

> we also need a way to delete and fully manage API keys separately from
> subscription accounts. We should:
> - be able to add a new openai subscription account by using Capacity->LLM
>   Providers-> Plans, API Keys (two sub pages)
> - in Plans we need to support adding both Anthropic Claude accounts and
>   OpenAI accounts
> - in API Keys, similarly we need to be able to add Anthropic and OpenAI api
>   keys and have similar controls on leasing, agent quotas, and a way to share
>   that with Voice it.
> - there should also be a fall back strategy where we first try to use a plan
>   account if available, then fall back to an API key pool, and lastly in voice
>   it settings have chat GPt api key that is dedicated to voice it that can be
>   used if the shared plans or keys are not available or have no quota left.

This document is the design. It builds nothing. Every choice that was the
owner's (cost, security, product) was a question in §9; the owner answered all
ten on 9 November 2026, and each is now marked **DECIDED** there with the
answer. Seven took the recommendation. Three changed the design, and the
sections they touch say so: **Q3** (a deleted key's secret is destroyed at
once, §4.4, §8.3), **Q7** (lending carries a cap the lender sets, for keys and
plans alike, §4.6), and **Q8** (an attempt records its credential tier and
account, through a contract request, §8.2).

## 1. Why, in one paragraph

Today the broker has one first-class kind, the **plan account** (a Claude
subscription, and since 1 November a ChatGPT plan), and one exception, the
**OpenAI developer API key**, which exists only so Voice it can open a GPT-Live
session and is locked into the Voice it pool (`apps/quota-broker/quota_broker/openai_api_key.py`,
header). An Anthropic API key has no home in the pool at all: it lives as the
per-tenant secret `swarm-tenant-<tenant>-anthropic`, which a worker reads only
when the broker says `no_accounts_registered` or cannot be reached
(`apps/agent-worker/agent_worker/accountlease.py:19-58`,
`apps/quota-broker/quota_broker/accounts.py:284-286`). So when every plan is
at its window the agent parks, even when a paid key with credit sits unused one
secret away; and a key can be neither listed, paused, drained, capped nor
deleted from the console. The request turns the key into a first-class kind
beside the plan, with the same controls, and puts the two in one ordered
fallback.

## 2. What exists, and what each fact forces

| Fact (file) | What it forces |
|---|---|
| `Account` carries `provider`, `state` (`AVAILABLE`, `PAUSED`, `DRAINING`, `REAUTH_REQUIRED`), `windows`, `holds`, `max_agents`, `model_classes`, `lend_to`, `pools`, `limited_until` (`accounts.py:306-326`, `545-729`) | A key is an `Account` too: one document shape, one hold mechanism, one set of state transitions. No parallel store. |
| `may_serve(tenant)` is ownership or an explicit loan, and requires the `workspace` pool (`accounts.py:835-845`) | Lending a key to workspaces is the same lending as a plan's (`lend_to`, which §4.6 turns into `lends`, each with a cap); "share with Voice it" is the `voice-it` pool membership. |
| A hold is granted in one Firestore transaction that re-checks eligibility and room on the account document (`main.py:1442-1538`); `is_full` counts **live** holds, never the advisory `assigned` (`accounts.py:772-783`) | The key tier reuses `acquire_hold` unchanged: one transaction per grant, counted from the grant. |
| `POST /v1/accounts/assign` filters by `provider == asked`, `may_serve`, and the step's model class resolved from its runner profile (`accounts.py:1533-1573`, `main.py:1310-1349`) | The fallback is a change inside the assign route: which providers it walks, in what order. The caller still names nothing but its agent (invariant 10). |
| A swap moves a hold in one broker transaction, "never two and never none" (`accountlease.py:35-44`, `main.py:5614`) | Moving an attempt from a plan to a key mid-run is a swap whose candidate list now includes the key tier. |
| `DELETE /v1/accounts/{id}` closes every hold as `account_removed` in one transaction and answers `"secret": "retained"`; `secretstore.py` has no delete method (`main.py:1629-1703`, `4738-4765`; `accountstore.py:917-929`) | Deleting a key is today's removal plus an explicit, typed act on the secret (§4.4). That act needs a permission the broker does not hold (§8.3). |
| The `openai-api` kind: provider name of its own so a codex step's `openai` lease can never pick it; no window; no `-refresh`; voice-it pool only, never lent (`openai_api_key.py`) | The provider-name split is kept and extended (`anthropic-api`). The "voice-it only" rule is what changes, and only for keys the owner marks shared. |
| GPT-Live: the ChatGPT plan's token answered **403** on `POST /v1/live/sessions`, measured 4 November 2026 (`openai_api_key.py` header, `docs/design/voice-it.md` §3.6) | A plan can never serve Live. Live's chain has no plan tier. |
| Voice it text conversation leases a **ChatGPT plan** (`capability: text`) and talks to the Codex backend; a key is refused there today (`apps/swarm-voice/swarm_voice/textchat.py:41-44`, `166-167`) | A key serving the conversation model needs a second wire in swarm-voice (the developer Responses API). |
| The Voice it planner is headless Claude Code on a leased **Claude plan**, its environment built with `CLAUDE_CODE_OAUTH_TOKEN` only (`apps/swarm-voice/swarm_voice/claudeturn.py:1-11`, `plannerlease.py:1-30`) | The planner can fall to an Anthropic key only if swarm-voice builds `ANTHROPIC_API_KEY` instead. |
| Runner profiles accept either credential: claude-code (and its sizes, review, indexer, browser and lightpanda variants) declare `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`; codex declares `OPENAI_API_KEY` or the two plan variables; `browser` declares `ANTHROPIC_API_KEY` alone (`apps/common/swarm_common/profiles.py`, `secrets_any_of=True`) | The frozen catalogue already allows both kinds. **No contract change is needed for the fallback.** |
| The worker hands an account's secret to `CLAUDE_CODE_OAUTH_TOKEN` or the plan pair alone, and refuses any other shape (`accountlease.py:693-730`) | The worker gains one shape per key kind: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, the only variable set. |
| The worker classifies `insufficient_quota`, `quota exceeded`, `resource_exhausted`, `usage limit reached` and a standing 429 as a rate limit (`apps/agent-worker/agent_worker/runners/cliagent.py:197-224`) | A key's "credit gone" is today treated like a window that will reset by itself. §5.2 splits the two. |
| Per-agent budget in tokens or dollars, `cost_usd` estimated by the worker (`docs/design/agent-budget.md` §2) | A key's spend limit is metered from the same estimate; no provider reports spend to a plain key (§4.6). |

## 3. The model: two kinds, two providers, four cells

A **plan account** is a subscription signed in by a person: a flat price, a
usage window the broker reads, a token the broker refreshes. An **API key** is
a developer key: billed per token or per minute, no window to read, no
refresh, a rate limit and a credit balance that only show themselves as
refusals.

| Kind × provider | `provider` on the `Account` | Exists today | Credential the worker gets |
|---|---|---|---|
| Anthropic plan (Claude subscription) | `anthropic` | yes | `CLAUDE_CODE_OAUTH_TOKEN` |
| OpenAI plan (ChatGPT plan, Codex sign-in) | `openai` | yes | `CODEX_PLAN_ACCESS_TOKEN`, `CODEX_PLAN_ACCOUNT_ID` |
| Anthropic API key | `anthropic-api` | **new** | `ANTHROPIC_API_KEY` |
| OpenAI API key | `openai-api` | yes, Voice it only | `OPENAI_API_KEY` (agents) or the bearer (Voice it) |

Why the key keeps its own `provider` name rather than a new `kind` field: the
`openai-api` reason holds for both vendors. Every filter in the broker already
keys on `provider`, so a key can never be handed to a caller that asked for a
plan by accident, and no stored document is rewritten. The **vendor** of a
provider (`anthropic` for `anthropic` and `anthropic-api`) is one table in the
broker (`VENDOR_OF`), and the console groups by it.

### 3.1 What each can serve, and what it cannot

| Consumer | Anthropic plan | Anthropic key | OpenAI plan | OpenAI key |
|---|---|---|---|---|
| claude-code family steps (every model class) | yes | yes | no | no |
| `browser` profile steps | **no**: it declares `ANTHROPIC_API_KEY` only | yes | no | no |
| codex steps | no | no | yes | yes |
| Voice it **Live** (GPT-Live) | no | no | **no: OpenAI answers 403** (measured 4 November 2026) | yes, the **only** kind |
| Voice it **conversation** model (text) | no | no | yes (today's path) | yes, after swarm-voice gains the developer Responses wire (lane LP-V1) |
| Voice it **planner** (Claude Code) | yes (today's path) | yes, after swarm-voice builds `ANTHROPIC_API_KEY` (lane LP-V1) | no | no |

Said plainly:

* **A ChatGPT plan cannot serve GPT-Live.** Not in the pool, not as a
  fallback, not with a different header. Live's chain starts at the key tier.
* **A Claude plan cannot serve the `browser` profile**: that profile declares
  only the metered key. Its chain starts at the key tier too; today it runs on
  the per-tenant secret.
* **No Anthropic credential serves a Voice it Live or conversation model, and no
  OpenAI credential serves the planner.** The vendors are not interchangeable;
  the fallback walks kinds within one vendor, never across vendors.
* A key serves the **model classes** it is marked for (`model_classes`, as a
  plan does). An Anthropic key's organisation tier bounds its rate limits, not
  its models; the field exists so an owner can keep an expensive class off a
  key.

## 4. API keys managed fully and separately

Every control below is the plan account's control, reached through the same
broker route where one exists, so the two pages behave alike and the broker
has one code path per action.

### 4.1 Add

Two ways in, both through Admin › Setup's one-time link machinery
(`apps/swarm-api/swarm_api/setup_links.py`, `routes/setup.py`), generalised
from today's `voice-api-key` kind to a new `api-key` kind carrying
`{provider: anthropic-api | openai-api, label, share_with_voice_it, lend_to}`:

* **The one-step form** on API Keys › Add, for the admin who holds the key: the
  console mints a link for its own user and redeems it in the same page, the
  key in the POST body. This is exactly `VoiceKeyLink` today
  (`apps/swarm-ui/src/Accounts.tsx:3180-3271`), with a provider choice.
* **A one-time link** for someone else (the person who owns the provider
  account): bound to their verified email, single use, 10 minutes, as
  `VOICE_KEY_TTL` (`setup_links.py:36-44`).

**A key never passes through Claude, a log or a URL.** The link token is in
the URL; the key is only ever in the redeem POST body. swarm-api forwards it
once to the broker's `POST /v1/accounts` and keeps no copy: not in the link's
record, a log line, an answer or a refusal (`setup.py:1761-1768` holds this
today). No agent, planner turn or chat surface ever offers to take a key; the
console's add field is a password input with autocomplete off. Nothing in this
path is reachable from an agent or a Voice it conversation.

**Validated before it is stored.** The broker checks the shape (as
`openai_api_key.parse_api_key` does, never quoting the value), then makes one
read-only call the key must be able to make (each vendor's list-models route).
A 401 refuses the add with "the provider refused this key"; a 429 adds it as
limited. The value is written to one Secret Manager secret,
`swarm-account-<tenant>--key-<label>`: the plan's naming family
(`accounts.py:333-370`, so `secretstore.owned_by_this_platform` and the
broker's existing version-adder condition already cover it), with no
`-refresh` half and a `key-` mark before the label. The mark is what lets IAM
tell a key's secret from a plan's (§8.3): the broker may delete the first and
never the second. A tenant id cannot contain `--` (`accounts.py` refuses
one), so the broker refuses a plan label that begins `key-` or contains
`--key-`, and then a name holds `--key-` exactly when it is a key's; stage
(a) of LP-B1 lists the existing accounts to show no plan's does before the
grant is made.

### 4.2 Label

The label is the secret's name and so is fixed at add, as for a plan. The
display name is editable (`PUT /v1/accounts/{id}/name`, existing).

### 4.3 Pause and drain

`PUT /v1/accounts/{id}/state`, existing (`main.py:4628-4662`). **Paused**:
holders keep it, nothing new is assigned. **Draining**: nothing new, and every
holder is told `move` at its next turn boundary, in the same transaction that
sets the state; the holder swaps to the next candidate in the chain (§5.3) or
checkpoints and parks (invariants 4 and 8).

### 4.4 Delete

**DECIDED (Q3, 9 November 2026): the secret is destroyed at once, on
confirmation.** The recommendation was to wait for the last open hold; the
owner chose at once, because a running attempt does not need the secret: it
keeps the value it already read. Waiting would only have kept a credential
the owner asked to delete readable for up to a hold TTL, by anyone who could
read it.

**The confirmation** is one dialog, typed, in the no-prose form of §6.5:

* Title: `Delete API key <display name>`.
* One line on the surface, because it is an instruction, not an explanation:
  `Revoke it at Anthropic too.` (or `at OpenAI`), the vendor's name linking to
  that vendor's key page.
* A field: `Type <label> to delete`, the button disabled until it matches.
* The button: `Delete key`.
* Behind its (i): "The stored key is destroyed now. Agents running on it keep
  the value they already read until their next turn, then move to the next
  tier or park. Deleting here does not revoke the key at the provider: a copy
  anywhere else keeps working until it is revoked there."

Then, in order, in the one `DELETE /v1/accounts/{id}` request:

1. **Holds close in one transaction**: `remove_account_and_close_holds`
   (`main.py:1629-1703`), with one addition: every live hold closed as
   `account_removed` and the document deleted, as today, **and** a
   `key_deletions/<account id>` record (secret name, who, when; never the
   value) written in the same transaction. No hold is left half-closed, and no
   key document disappears without a record that its secret is owed a
   deletion.
2. **The secret, at once**: after that transaction commits, the broker
   destroys every version of `swarm-account-<tenant>--key-<label>` and deletes
   the secret, then marks the record done. It records the deletion in the
   audit log, never the value. If either call fails (Secret Manager down, a
   refused permission) the answer is a 502 naming the step that failed, the
   console says the key is removed but its secret is not yet destroyed, and
   the broker's quota sweep retries every record not marked done until it is.
   The order is deliberate: destroying first and then failing the transaction
   would leave a live account whose secret is gone, which every worker would
   read as `account_unreadable`; closing first leaves at worst an orphan
   secret that the record names and the sweep removes.
3. **Leases in flight**: an attempt already running on the key keeps the value
   it read into its process; the broker cannot take it back, and does not need
   to. At its next turn boundary its `hold-status` answers `move` (the hold is
   gone), and it swaps to the next tier or checkpoints and parks. A worker
   that tries to read the secret again (a runner restart, a late start) gets
   NOT_FOUND, which is today's `account_unreadable`: it excludes the account
   and asks again (`_reject_account`). So a deleted key stops being used
   within one turn, not instantly. A Voice it Live session on the key is ended
   at its next renew (every 240 s, `accountlease.py` in swarm-voice) and
   reopened on the next tier, or ends with the reason.
4. **The provider**: deleting here does not revoke the key at Anthropic or
   OpenAI; a stolen copy keeps working. **DECIDED (Q4, 9 November 2026), as
   recommended:** the broker does not revoke at the provider (that would need
   an organisation admin key, a far wider credential than the one deleted);
   the confirmation tells the admin to, on its surface.

This departs from today's `"secret": "retained"` for plan accounts on purpose:
a plan's secret holds a refresh token that can be re-used after an accidental
removal; a key's secret holds a paid credential the owner asked to delete. Plan
removal is unchanged, and the IAM grant cannot reach a plan's secret (§8.3).

Two keys are stored outside the `--key-` name, and the delete says so:

* **An imported workspace key** (Q7, §5.3) is a copy: deleting it destroys the
  pool copy at once, and the workspace's own `swarm-tenant-<tenant>-<vendor>`
  secret, which `scripts/create-secrets.sh` owns, still holds the value for
  the outage path. The dialog's surface line then reads `Revoke it at
  Anthropic too. The workspace's own key secret still holds it.`
* **The dedicated Voice it key registered before this design** (Q9) keeps its
  `swarm-account-<tenant>--<label>` secret. Its delete in Voice it › Settings
  destroys every version at once (the broker's existing `versions.destroy`
  on secrets this platform owns) and leaves an empty secret with no version
  to read, named in the audit line. A dedicated key added after stage (a) is
  stored under the `--key-` name and deleted whole.

### 4.5 Rotate

**Replace key** on a key's row opens the same one-step form (or mints a link)
for the **same label**. The broker validates the new value, adds it as a new
version and expires the superseded one (`SecretManagerStore.add_version`,
`_expire_superseded`, `secretstore.py:389-458`). Holders keep the old value
until their next turn boundary or process start, so the old version is
destroyed after the hold TTL, not at once. Rotation does not touch holds,
caps, sharing or spend counters, and the key's row shows "replaced by
<person>, <date>" (the `key_added_by`/`key_added_at` fields that exist for the
Voice it key today, `openai_api_key.py`).

### 4.6 Leasing and agent quotas

| Control | Plan account today | API key (this design) | Measured from |
|---|---|---|---|
| Concurrency | `max_agents` (`PUT .../max-agents`) | the same field and route | live holds (`is_full`) |
| Who may use it | `lend_to` (`PUT .../lending`) | the same | `may_serve` |
| Lend caps | none: a lend is a name in `lend_to` | `lends: {tenant: {max_agents, monthly_spend_cap_usd}}`, new on **both** kinds; every lend carries a cap (Q7) | per borrowing workspace: its live holds, and for a key its month's metered spend, inside the `acquire_hold` transaction |
| Model classes | `model_classes` | the same | `serves_class` |
| Per-workspace cap | none | `per_workspace_max: {tenant: n}`, new on both kinds (Q6, decided) | live holds whose tenant is that workspace, inside the same `acquire_hold` transaction |
| Usage window | read from the provider (five-hour, seven-day) | none: "not measured", never 0 (`Account.measured`) | provider's usage route |
| Rate limit | `limited_until` from a 429 | the same, from the 429's `retry-after` | the worker's report |
| Credit gone | n/a | `exhausted_at` (new): set by a credit-gone refusal, cleared by a successful probe or **Resume** | §5.2 |
| Spend limit | n/a (flat price) | `monthly_spend_cap_usd` (new, optional) | the sum of the `cost_usd` estimates of holds closed on the key this calendar month (UTC) |
| Share with Voice it | `voice-it` in `pools` | the same, as a switch | `pools` |

**Spend where a provider reports it.** Neither vendor reports spend to a
plain key; both report it only to an organisation **admin** key (Anthropic's
Admin API cost report, OpenAI's organisation costs route). So the cap is
metered here from the worker's own `cost_usd` estimate (`agent_worker/spendprice.py`,
the figure the per-agent budget already uses), shown as an estimate with its
basis in the tooltip, and a key past its cap reads as "no quota left" (§5.1).
**DECIDED (Q5, 9 November 2026), as recommended:** the cap is metered from
that estimate, not read with an admin key, and an exhausted key is probed
hourly with a one-token completion (§5.2).

**DECIDED (Q6, 9 November 2026), as recommended:** per-workspace caps exist on
plan accounts and API keys alike, one field on both kinds, so the two pages
have the same controls.

**Lending, with a cap on every lend (Q7, decided 9 November 2026).** The
owner's note on Q7 asked that a key lent to another workspace be leased
exactly as a plan is, and that the lend itself be capped, tracked and
enforced. So lending is one mechanism for both kinds, and a lend is no longer
just a name:

* **The shape.** `lend_to` becomes `lends`, a map from the borrowing
  workspace to its cap: `max_agents` (agents at once, required, at least 1)
  and, on a key, `monthly_spend_cap_usd` (required on a key: a lend of a
  metered credential with no ceiling is exactly the open-ended cost the note
  was about; absent on a plan, whose price is flat). `may_serve` reads the
  map's keys, so who may use the account is unchanged in meaning. The owning
  workspace is not in `lends`; its own limit is `per_workspace_max` (Q6), and
  the account's `max_agents` bounds everyone together.
* **Who sets it.** The lender: the owning workspace's admin, or a platform
  admin, through `PUT /v1/accounts/{id}/lending`, which now takes the caps and
  refuses (422) a lend that carries none. Narrowing a cap below current use
  closes no hold: the borrowers over it run on, and the next grant is refused
  until they are under it.
* **Tracked.** Per borrowing workspace, on the account document: live holds
  (counted from the grant, as `is_full` counts, invariant 3) and, on a key,
  `lend_spend: {month: {tenant: usd}}`, the metered estimate of that
  workspace's holds. A closed hold adds its `cost_usd` in the transaction
  that closes it; a running holder's `hold-status` carries the attempt's
  running estimate, so a long attempt is counted before it ends.
* **Enforced inside the grant.** `acquire_hold` already reads the account
  document and checks room in one transaction; it now also reads the
  borrower's lend, and refuses the hold when the borrower is at its
  `max_agents` or its month's spend is at its cap. The check and the write are
  the same transaction, so two borrowers racing for the last slot of a lend
  cannot both win, and a refused grant writes nothing: the walk moves to the
  next candidate (§5.3). The scheduler's admission, every pool of
  `Lease.pools` in one transaction, is untouched (invariant 2): the lend is a
  limit on the credential, checked where the credential is granted, never a
  second reservation beside the capacity one. A borrower past its spend cap
  mid-attempt is answered `move` at its next turn boundary, like a key past
  its own cap.
* **Today's lends** (plans lent with no cap) are carried over by stage (a)
  of LP-B1 with `max_agents` equal to the account's own `max_agents`, which
  is exactly the limit they have today, so nothing changes until the lender
  narrows it; the console marks them `cap not set` until a lender saves one.
* **Shown on both pages.** Each lend is a line under its account, on Plans
  and on API Keys alike: the borrowing workspace, agents now over its cap,
  and on a key the month's estimated spend over its cap (§6.2, §6.3).

**The share-with-Voice-it switch** adds or removes `voice-it` from the key's
`pools`. It is a platform-admin action, as moving a plan between pools is
today (`PUT /v1/accounts/{id}/pools`, `main.py:4805`), because Voice it's
cost lands on whoever owns the key. A shared key serves Voice it
**after** the plans and **before** the dedicated Voice it key (§5). The
"voice-it only, never lent" rule of `openai_api_key.py` stays true for the
**dedicated** Voice it key and stops being true for keys added on API Keys.

## 5. The fallback order

### 5.1 "Available" and "no quota left", from what the broker measures

A **plan account is available** to a request when, read inside the grant's
transaction: its state is `AVAILABLE`; `may_serve` the tenant; it serves the
step's model class; it is not full (live holds below `max_agents`, and below
`MAX_AGENTS_BEFORE_READING` while it awaits its first reading); it is not
limited (`limited_until` passed); and no window is below the assign floor
(`blocking_windows` empty), on a reading fresh enough to trust. It has **no
quota left** when its binding window is under the floor or a 429 has set
`limited_until` that has not passed.

An **API key is available** when: its state is `AVAILABLE`; `may_serve` the
tenant; it serves the class; it is not full; the workspace is under its
`per_workspace_max`; a borrowing workspace is under its lend's `max_agents`
and its lend's spend cap (§4.6); it is not limited (`limited_until` passed);
it is not exhausted (`exhausted_at` unset); and its month's metered spend is
under `monthly_spend_cap_usd` if one is set. It has **no quota left** when it
is limited, exhausted or over its spend cap. A workspace at its lend's cap is
not "no quota left" for anyone else: the key stays available to its owner and
its other borrowers. A plan follows the same lend rule for a borrower: under
its lend's `max_agents`.

### 5.2 Rate-limited versus credit gone

The two answers want opposite responses, and the worker cannot tell them
apart today (`cliagent.py:197-224` puts `insufficient_quota` in the rate-limit
list):

* **Rate-limited** (Anthropic `rate_limit_error`, OpenAI `rate_limit_exceeded`,
  a standing 429 with `retry-after`): the key resets by itself. It is marked
  `limited_until = now + retry-after` (a minute when none is sent), exactly as
  a plan's 429 is (`accountstore.record_health`), and the attempt swaps.
* **Credit gone** (the owner's brief records both `credit_balance_exhausted`
  and `insufficient_quota` answered live with a 429 on 9 November 2026; the
  raw answers are not in this repository): the key does **not** reset by
  itself. It is marked `exhausted_at`, a health event of the existing
  `exhausted` kind, and is skipped until a platform admin presses **Resume**
  after topping up, or the broker's hourly probe (one list-models call, which
  a key with no credit still passes, so the probe is a small completion
  capped at one output token; its cost was accepted with Q5 on 9 November
  2026) succeeds.

So the worker's classifier gains a third outcome, `credit_exhausted`, matched
on those two codes before the rate-limit markers, and reported to the broker's
existing `exhausted` route with the account named. A credit-gone answer that
names no account (the per-tenant secret) stays the tenant-level AIMD
`EXHAUSTED` it is today (`aimd.py`).

### 5.3 The chain, and how a lease moves to the next tier

| Consumer | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| claude-code family | Anthropic plans | Anthropic keys lent to the workspace | none: park |
| `browser` | (no plan can serve it) | Anthropic keys lent to the workspace | none: park |
| codex | OpenAI plans | OpenAI keys lent to the workspace | none: park |
| Voice it Live | (no plan can serve it) | OpenAI keys shared with Voice it | the dedicated Voice it key |
| Voice it conversation | OpenAI plans in the Voice it pool | OpenAI keys shared with Voice it | the dedicated Voice it key |
| Voice it planner | Anthropic plans in the Voice it pool | Anthropic keys shared with Voice it | none: refused (Q2, decided) |

**At assign.** `POST /v1/accounts/assign` takes the same request it takes today
(the worker names its provider, the broker resolves the step's class); the
broker walks tier 1's candidates in today's order, then tier 2's, and grants
the first that `acquire_hold` accepts. The answer gains `tier`
(`plan` | `api_key` | `voice_dedicated`) and the account's `provider`, so the
worker knows which variable to set. When nothing in any tier is available the
answer is today's 200 with a `reason`, now the **soonest** of the tiers'
reasons and `next_reset_at` (a limited key's `limited_until` counts; an
exhausted key's does not). The worker parks exactly as it does today.

**Mid-run.** An attempt whose plan answers a 429 swaps today
(`POST /v1/accounts/swap`, one transaction). The swap's candidate list becomes
the same chain, minus `exclude`, so a plan at its window hands over to a key
at the next turn boundary: the worker checkpoints, the swap moves the hold,
and the runner restarts its CLI with the key's variable in place of the
token. A swap is still one transaction: the hold moves, it is never two and
never none.

**Back up the chain. DECIDED (Q1, 9 November 2026), as recommended:** an
attempt on a key moves back to a plan at its next turn boundary once one is
free. A key costs per token and a restart costs seconds, so the cheaper tier
wins whenever it can. At each turn boundary a key holder's `hold-status` asks
whether a tier-1 account would grant this attempt now (the same availability
test as §5.1, read in the status's transaction); if one would, it answers
`move` with the reason `plan_available`, the worker checkpoints, and the swap
asks for tier 1 only. The swap is still one transaction: if the plan filled
in between, the hold stays on the key and nothing moves, never two and never
none. A plan the attempt left on a 429 is not offered back until its
`limited_until` has passed, which availability already requires, so an
attempt does not bounce between a spent plan and a key. A **new** attempt
always starts at tier 1.

**The per-tenant secret.** Today's `no_accounts_registered` → tenant secret
path (`accounts.py:284-286`) and the broker-unreachable degrade
(`accountlease.py:46-58`) stay as they are: a control-plane outage still runs
on the tenant secret. **DECIDED (Q7, 9 November 2026), as recommended, with
the owner's note:** each existing `swarm-tenant-<tenant>-<vendor>` provider
key is imported as a pool key owned by that workspace, so it becomes visible,
capped and deletable. The import is a copy, made once by stage (a) of LP-B1:
the broker reads the tenant secret, validates it as §4.1 does, and registers
it as a `*-api` account of that workspace, lent to nobody, its value written
to its own `--key-` secret. The tenant secret stays where it is, owned by
`scripts/create-secrets.sh`, because it is still the outage path above. A key
the import cannot validate is skipped and listed in the stage's report, never
registered half-checked. Lending an imported key later follows §4.6: a cap
on every lend.

**Voice it.** swarm-voice's `POST /v1/voice/lease` (`capability: live` or
`text`) and the planner's `POST /v1/accounts/assign` (`holder: voice-planner`)
walk their rows of the table the same way. Tier 3, the **dedicated Voice it
key**, is read only when tiers 1 and 2 have no available account, and is
never offered to an agent step: it is an `openai-api` account in the
`voice-it` pool alone, never lent, marked `voice_dedicated` (one per
deployment), which is exactly what today's Voice it key is. A Live session
renews every 240 s; a renew that finds its key limited or exhausted is
refused, and swarm-voice reopens Live on the next tier at the next turn
boundary instead of ending the session. **DECIDED (Q9, 9 November 2026), as
recommended:** today's Voice it OpenAI key stays the dedicated Voice it key,
in Voice it › Settings; it is not moved to API Keys as a shared key.
**DECIDED (Q2, 9 November 2026), as recommended:** there is no dedicated
Anthropic key for the Voice it planner. The request names only a ChatGPT key
for Voice it, so the planner's chain has no tier 3: when the plans in the
Voice it pool and the Anthropic keys shared with Voice it are all spent, the
planner refuses with the reason, as it does today.

### 5.4 What the person sees

* **An agent step**: a chip on the attempt, `Plan` or `API key`, with the
  account's display name in its tooltip, read from the attempt's own record
  (`credential_tier` and `account_id`, Q8, §8.2); a tier change is one line on
  the attempt's timeline ("moved to API key", the reason in the tooltip),
  read from the broker's `account_holds` log, which keeps every move. A park
  shows the reason and the soonest reset, as today.
* **Voice it**: the session's header shows which tier is serving
  (`Plan`, `Shared key`, `Voice it key`) as a small mark with a tooltip; a
  tier change mid-session is one caption, never a modal. A refusal names
  the reason ("no OpenAI key available").
* **Capacity › LLM Providers**: each row's state, live holders and tier usage
  (how many attempts today ran on a key because the plans were spent), so the
  owner sees what the fallback is costing.

### 5.5 Invariants

* **1. Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create demand.** The
  credential is chosen by a worker of a `LEASED` attempt; a `QUEUED`,
  `PARKED` or `READY` agent holds no account and no key. Walking more tiers
  adds no pods and uses no pending pod as a backlog: an empty chain still
  parks.
* **2. All-or-nothing in one transaction.** The scheduler's admission (every
  pool of `Lease.pools`, one transaction) is untouched. The credential hold is
  one `acquire_hold` transaction on one account document whatever its tier,
  and a swap is one transaction that moves it. The walk itself holds nothing:
  a candidate that loses its room between the listing and the transaction is
  refused inside the transaction and the walk tries the next.
  The lend caps of §4.6 are read and enforced inside that same
  `acquire_hold` transaction: a borrower at its cap is refused there, and a
  refusal writes nothing.
* **3. Concurrency counts from `LEASED`.** `max_agents`, the new
  `per_workspace_max` and each lend's `max_agents` count live holds from the
  moment of the grant, which happens while the attempt is `LEASED`, never from
  `RUNNING`.
* **4 and 8.** A move between tiers is checkpoint, swap or park, at a turn
  boundary; nothing sleeps through a provider's reset.
* **5. Fencing.** Holds stay stamped with agent and attempt; a fenced attempt's
  assign, swap and release are refused as today.
* **9. Per-tenant isolation.** A key lent to a workspace is readable by that
  workspace's worker service account through the per-secret accessor grant the
  broker makes for a lent plan today (`secretstore._grant_accessors`); a key
  not lent to a workspace is not. The dedicated Voice it key is readable by
  the broker alone, as now.
* **10. Callers pick a profile by name.** No request carries a key, a tier or
  an account. The broker chooses.

## 6. The console: Capacity › LLM Providers

### 6.1 What moves, what stays

| Today | Becomes |
|---|---|
| Capacity › Accounts (`apps/swarm-ui/src/Accounts.tsx`), the plan board | Capacity › LLM Providers › **Plans** |
| The Voice it pool panel (`VoiceItPool`, `Accounts.tsx:3083-3155`) | stays on Plans as a filter (`Voice it`) and as the Voice it switch on each row |
| The one-step Voice it key field (`VoiceKeyLink`, `Accounts.tsx:3180-3271`) | **Voice it › Settings**, under "Dedicated OpenAI key", beside today's `openai_key` status (`routes/voice_settings.py`) |
| Account health, Holders, Provider quota, By runner profile, Jev usage | stay where they are; Account health gains a kind filter |
| `/capacity/accounts` | redirects to `/capacity/llm-providers/plans`, keeping `?account=` |

The sub-pages follow the existing second-level pattern (Admin › Workspaces'
`?tab=`, `App.tsx:1230-1238`): one `llm-providers` tab in `SECTIONS`, its two
sub-pages as `/capacity/llm-providers/plans` and `/capacity/llm-providers/api-keys`
(or `?tab=`; the mock-up lane picks).

### 6.2 Plans

* **List**: one row per plan, grouped by vendor (Anthropic, OpenAI); state,
  windows (five-hour and seven-day bars), live holders over `max_agents`,
  lent-to count, Voice it mark; under each row, one line per lend: the
  borrowing workspace and its agents now over the lend's cap (§4.6).
* **Add**: a vendor choice, then today's sign-in for that vendor: the Claude
  sign-in, or the Codex sign-in for a ChatGPT plan (`sc account add --provider
  openai` today, `openai_plan.py`); or a one-time `pool-account` link for the
  plan's owner, generalised with a vendor.
* **Controls**: pause, drain, remove (typed), max agents, per-workspace caps,
  lend (with its agents cap), model classes, share with Voice it, refresh,
  rename.
* **Empty state**: "No plans" with the add control beside it, and a `Mark`
  stating the read succeeded (today's `acct-empty`, `Accounts.tsx:807-848`).

### 6.3 API Keys

* **List**: one row per key, grouped by vendor; state (available, limited
  until, exhausted, paused, draining), live holders over `max_agents`, the
  month's metered spend over its cap, lent-to count, Voice it switch, added
  by and when; under each row, one line per lend: the borrowing workspace,
  its agents now over the lend's agents cap, and its month's estimated spend
  over the lend's spend cap (§4.6). The value is never shown, not even
  masked; there is nothing to reveal.
* **Add**: vendor, label, then the key (one-step form) or "send a link".
* **Controls**: pause, drain, resume (after credit is topped up), replace key,
  delete (typed, the confirmation of §4.4), max agents, per-workspace caps,
  spend cap, lend (with its agents and spend caps), model classes, share with
  Voice it.
* **Empty state**: "No API keys"; the tooltip says agents fall back to keys
  only after plans, so an empty page is valid.

### 6.4 Voice it › Settings

"Dedicated OpenAI key": set by, when, replace, delete (typed); and a read-only
line of the chain ("Plans → shared keys → this key") with each tier's
availability now.

### 6.5 No prose

Every explanation on these pages (what a tier is, why a plan cannot serve
Live, that spend is an estimate, that delete does not revoke at the provider)
is a tooltip behind an (i), never a sentence on the surface: the owner's
no-prose rule of 5 November 2026 (as cited at
`apps/swarm-ui/src/AccountHealthPage.tsx:251-252` and `Access.tsx:309-311`).

### 6.6 Mock-ups first

Under the owner's mock-up practice (two directions each, drawn in the real
console before any build; `docs/design/voice-it-own-speech.md` §9,
`docs/web-ui/mockups/PICKS.md`: build the picked variant only), these lanes
wait for a pick:

* **LP-M1** draws LLM Providers: the sub-page navigation, the Plans list and
  the API Keys list, in two directions (for example one table with a kind
  column and a vendor filter, against two vendor-grouped card lists).
* **LP-M2** draws the add flows and the delete and replace confirmations, in
  two directions (a side sheet against an in-row expansion).
* **LP-M3** draws the Voice it › Settings dedicated-key block and the tier
  mark in a Voice it session, in two directions.

## 7. Day One

A fresh install gets, from `apps/swarm-install`:

* **The question. DECIDED (Q10, 9 November 2026), as recommended:** the
  installer asks "Which will agents run on: plans, API keys, or both?" and
  records the answer in its state. A key answer offers the one-step key form
  as a console link at the end, never a prompt in the terminal (the
  installer must not see a key either).
* **The team gate** (`team.py:388-413`, `await_pool_account`) checks that
  choice: `plans` waits for a plan the team may use, `API keys` for a key lent
  to or owned by the team, `both` for one of each. So a deployment with keys
  only finishes, and an operator who meant both is not let through on one.
* **Voice it** (`tfvars.py:57-78`): when Voice it is enabled, the summary
  links to Voice it › Settings › Dedicated OpenAI key and says Live needs a
  key.
* **The IAM grant** of §8.3, through Terraform, so the delete path works on a
  fresh project without a hand step.
* **The final summary** prints the choice and each chain of §5.3 with what it
  has now (`claude-code: 0 plans, 1 key`; `Voice it Live: no key`), read from
  the broker's `GET /v1/accounts` and `GET /v1/voice/pool`, and flags a kind
  the operator chose that has nothing registered, so an operator sees a chain
  with no tier before the first step parks on it.
* **The smoke step**: the team's smoke agent (`mock` needs no credential;
  the first claude-code smoke after the gate) runs on whichever tier the
  chain grants, and the summary names the tier it ran on.

Why the installer never asks for the key itself: the terminal it runs in
keeps scrollback and may be recorded, and the operator's laptop is not where
credentials are entered (`scripts/create-secrets.sh --stdin` is the one
exception, for the tenant's forge token). The console's one-step form is the
same path every later key takes, so Day One tests it.

Nothing else is new for a fresh deployment: the collections, routes and
secrets are the broker's, no new Firestore index is needed (the broker lists a
tenant's accounts and filters in memory, as `accounts_serving` does today), and
a deployment with neither plans nor keys behaves as today: the per-tenant
secret, or a park.

## 8. The lane plan

### 8.1 Lanes

| Lane | Territory | Waits on |
|---|---|---|
| **LP-C1** contract: request 152 (§8.2), applied to `Attempt`, with its TypeScript restatement and parity check | `apps/common/swarm_common/models.py` (applied by the contract lane under the owner's acceptance, never by a feature lane), `apps/swarm-ui/src/types.ts`, `scripts/lib/check-contract-parity.sh`, its test | nothing: the owner accepted request 152 on 9 November 2026; it runs first |
| **LP-B1** broker, one lane in three sequenced stages, each a PR that waits on the one before: **(a)** the `anthropic-api` kind, `VENDOR_OF`, key validation, the `--key-` secret name and the plan-label refusal, `exhausted_at`, `per_workspace_max`, `lends` with their caps and the carry-over of today's lends, `monthly_spend_cap_usd`, `voice_dedicated`, the `api-key` registration path and caps routes, the import of the workspace provider keys (Q7); **(b)** the tiered walk in assign, swap and the Voice it lease, the `tier` field, the lend-cap check and `lend_spend` in `acquire_hold` and hold close, the move back to a plan (Q1), the soonest reason, `accounts_serving`; **(c)** delete with immediate secret destruction and the `key_deletions` sweep, rotate, resume, the hourly exhausted probe | `apps/quota-broker/quota_broker/accounts.py`, `main.py`, `accountstore.py`, `secretstore.py`, a new `api_key.py` (generalising `openai_api_key.py`), their tests | (a) nothing; (b) stage (a); (c) stage (b), LP-T1 applied |
| **LP-W1** worker: the key shapes in `credential_env_from_account`, the `credit_exhausted` outcome, the tier on the attempt's timeline, the cross-kind swap restart and the move back to a plan, the running spend on `hold-status`, and the attempt's `account_id`, `credential_source` and `credential_tier` written where `record_spend` writes spend | `apps/agent-worker/agent_worker/accountlease.py`, `runners/cliagent.py`, `lifecycle.py` (`_lease_account`, `_decline_pool`), `control.py`, their tests | LP-C1; LP-B1 stage (b) |
| **LP-A1** swarm-api: the `api-key` setup-link kind, the redeem path, the Voice it settings' dedicated-key status | `apps/swarm-api/swarm_api/setup_links.py`, `routes/setup.py`, `routes/voice_settings.py`, their tests | LP-B1 stage (a) |
| **LP-V1** swarm-voice: Live, text and planner leases walk the chain; the developer Responses wire for text on a key; `ANTHROPIC_API_KEY` for a planner turn on a key; reopening Live on the next tier | `apps/swarm-voice/swarm_voice/accountlease.py`, `plannerlease.py`, `textchat.py`, `claudeturn.py`, their tests | LP-B1 stage (b) |
| **LP-M1, M2, M3** mock-ups (§6.6) | `docs/web-ui/mockups/llm-providers-*.html` | nothing |
| **LP-U1** console: the `llm-providers` tab and routing, the redirect | `apps/swarm-ui/src/App.tsx`, a new `LlmProviders.tsx`, its test | LP-M1 pick |
| **LP-U2** console: Plans (the board moved out of `Accounts.tsx`) | `apps/swarm-ui/src/Accounts.tsx`, a new `LlmPlans.tsx`, its test | LP-U1; LP-M1 pick |
| **LP-U3** console: API Keys list, add, controls | a new `LlmApiKeys.tsx`, `apps/swarm-ui/src/api.ts` (the key calls, and the dedicated-key and chain fields of `VoiceSettingsView` for LP-U4), its test | LP-U1; LP-M1, LP-M2 picks; LP-B1 stage (c), LP-A1 |
| **LP-U4** console: Voice it › Settings' dedicated key, the session tier mark | `apps/swarm-ui/src/voice/Settings.tsx`, `apps/swarm-ui/src/voice/Talk.tsx`, its test | LP-M3 pick; LP-A1; LP-U3 (for its `api.ts` calls) |
| **LP-U5** console: the attempt's `Plan` / `API key` chip (§5.4), read from the attempt's record | `apps/swarm-ui/src/AgentDetail.tsx`, its test | LP-C1; LP-W1 |
| **LP-I1** installer: §7, the plans / keys / both question, its gate and its summary | `apps/swarm-install/swarm_install/team.py`, `tfvars.py`, their tests | LP-B1 stage (a) |
| **LP-T1** Terraform: §8.3, the delete-only role and its conditional binding, and the `(account_id, created_at)` attempts index request 152 needs | the bootstrap role file under `terraform/bootstrap/`, the broker's IAM file under `terraform/modules/iam/`, `terraform/modules/firestore/indexes.tf` | nothing; its bootstrap half is the owner's apply |

Each UI lane touches at most three files. LP-U2 and LP-U3 both depend on
`api.ts` only through LP-U3; LP-U2 reuses the existing plan calls, so the two
never edit the same file. LP-U4 edits no `api.ts`: the existing
`VoiceSettingsView.openai_key` status covers only the read, so the replace and
delete calls and the chain line it needs are added by LP-U3, and LP-U4 waits on
it. LP-C1 runs first and alone because a frozen-contract change lands before
anything writes or reads its fields: the worker (LP-W1) is its one writer and
the console (LP-U5) its reader, and both wait on it. The broker work is one
lane, not three, because its stages share
`main.py` and `accounts.py` (two issues that edit the same file are one lane);
its stages merge in order, so no two broker PRs are open on the same file at
once.

### 8.2 The frozen contract

**The fallback itself needs no change.** The catalogue already declares both
credentials for every profile the chain serves (`secrets_any_of=True`), the
broker's `Account` (with its new `lends`) is not in `apps/common/swarm_common/`,
and the scheduler's `SlotPool`, `Lease` and `QuotaState` are unchanged.

**The attempt's record does change. DECIDED (Q8, 9 November 2026): yes.** The
recommendation was "not now, the hold log answers it"; the owner chose to take
up open request **13** ("`Attempt` does not record which pool account it ran
on") so that an attempt records the credential tier and the account it ran
on. The reason it is worth a contract change: with a key tier, "which agents
ran on a paid key, and on whose" becomes a spend question, and spend is
already on `Attempt` (request 2). Answering it from the hold log means a join
through a broker collection the console does not read, for every attempt on
the page; answering it from the attempt is one field beside `cost_usd`.

It is filed as request **152** in
[`docs/contract-change-requests.md`](../contract-change-requests.md), accepted
by the owner on 9 November 2026, and is **not** applied by this design: nothing
under `apps/common/swarm_common/` is edited here. In short, `Attempt` gains
`account_id`, `credential_source` (request 13's two fields) and
`credential_tier` (`plan` | `api_key`), all optional and `None` meaning "not
recorded". Each names the account the attempt **kept last**; the moves between
tiers stay in the broker's `account_holds` log and the attempt's timeline,
because a list of every account an attempt touched is history, not a record.
`Task.current_account` (request 132) is a dict the worker already writes, so
it carries the tier without a contract change.

The lane order follows from it (§8.1): **LP-C1** applies the request first,
then its readers: LP-W1 writes the fields, LP-U5 shows them.

### 8.3 IAM, Terraform and workflows

* **IAM / Terraform: needed, because Q3 decided "at once".** The broker can
  add versions and destroy them (`_destroy_stale_versions`, on names
  `secretstore.owned_by_this_platform` accepts) but holds no
  `secretmanager.secrets.delete`. Deleting a key's secret needs exactly that
  one permission, and only on key secrets:
  * a custom role holding `secretmanager.secrets.delete` and nothing else,
    defined in `terraform/bootstrap/` (custom roles are the owner's bootstrap
    apply), on the pattern of `swarmMcpCredentialAccessor` in
    `terraform/bootstrap/mcp_credentials.tf`, which holds a delete scoped to
    one name family rather than a wider role;
  * bound to the broker's service account with a condition limited to the
    broker's **key** secret names:
    `resource.name.startsWith("projects/<number>/secrets/swarm-account-")`
    and `resource.name.extract("/secrets/swarm-account-{t}--key-") != ""`,
    the same `extract` form the MCP credential binding uses. So the broker
    may delete `swarm-account-<tenant>--key-<label>` and cannot delete a
    plan's secret, a `swarm-tenant-*` secret, or anything on the shared
    project's never-touch list, whose names match neither arm.
  * One Terraform change in lane LP-T1, labelled `managed-by=swarm-terraform`,
    touching nothing on the never-touch list, applied before stage (c) of
    LP-B1 ships. Not verified live until the first key delete logs its
    secret deletion; until then a refused delete is the 502 and sweep retry
    of §4.4, never a silent retain.
* **Per-secret accessor grants** for lent keys are made by the broker at
  runtime, as for lent plans; no Terraform change. A lend's caps change no
  grant: a borrower under or over its cap can read the secret either way,
  and the cap is enforced where the hold is granted (§4.6).
* **The attempts index** for request 152's "an account's attempts, newest
  first", `(account_id, created_at)`, in `terraform/modules/firestore/indexes.tf`
  (lane LP-T1).
* **Workflows**: none. The lanes' tests are picked up by the existing jobs.

## 9. Questions for the owner: all ten DECIDED, 9 November 2026

Each was in the lane's `questions.json` with options and a recommendation. The
owner answered all ten on 9 November 2026; the answers are recorded with the
lane, and each is written into the section it governs. The question
as it was asked stays under each answer, so a reader can see what was weighed.

* **Q1. Back up the chain mid-run? DECIDED 9 November 2026, as recommended.**
  Owner's answer: an agent on an API key moves back to a plan at the next turn
  boundary when one frees up. (§5.3.)
  Asked: move an attempt from a key back to a plan at a turn boundary when one
  is free (recommended: keys cost per token, a restart costs seconds), or keep
  it on the key to the end of the attempt.
* **Q2. A dedicated Anthropic key for the Voice it planner? DECIDED 9 November
  2026, as recommended.** Owner's answer: no dedicated Anthropic key for the
  Voice it planner. (§5.3.)
  Asked: the request names only a ChatGPT key for Voice it. Recommended: no;
  the planner refuses when plans and shared Anthropic keys are spent, as it
  does today.
* **Q3. Delete: when is the secret destroyed? DECIDED 9 November 2026,
  DIFFERS from the recommendation.** Owner's answer: a deleted API key's secret
  is destroyed at once on confirmation; a running attempt keeps the value it
  already read. The same permission change: the broker may delete its own key
  secrets, conditioned to its key secret names. (§4.1, §4.4, §8.3.)
  Asked: after the last open hold ends (recommended), at once, or never
  (retain, as for plans).
* **Q4. Revoke at the provider? DECIDED 9 November 2026, as recommended.**
  Owner's answer: the broker does not revoke at the provider; the confirmation
  tells the admin to. (§4.4.)
  Asked: recommended no; the broker would need an organisation admin key, a
  far wider credential than the one deleted.
* **Q5. Spend: metered estimate or the provider's figure? DECIDED 9 November
  2026, as recommended.** Owner's answer: the spend cap is metered from the
  estimate, with an hourly one-token probe of an exhausted key. (§4.6, §5.2.)
  Asked: metered from the worker's `cost_usd` (recommended), or the provider's
  figure, which needs an admin key per organisation; and whether the hourly
  probe of an exhausted key is acceptable.
* **Q6. Per-workspace caps on plans too? DECIDED 9 November 2026, as
  recommended.** Owner's answer: per-workspace caps on both plan accounts and
  API keys. (§4.6.)
  Asked: recommended yes, one field on both kinds, so the two pages have the
  same controls.
* **Q7. The per-tenant secrets. DECIDED 9 November 2026, as recommended, with
  a note that changed the design.** Owner's answer: existing per-workspace
  provider secrets are imported as pool keys, and the owner added, verbatim:
  "but apply the same lease for api keys as we do for subscribtion accounts.
  When an account or an API key is leased to another workspace, a cap for that
  lease should be set and tracked and enforced as well". So lending is one
  mechanism for both kinds and every lend carries a cap the lender sets, counted
  per borrowing workspace and enforced inside the grant's transaction, and both
  pages show each lend's cap and use. (§4.6, §5.1, §5.3, §5.5, §6.2, §6.3.)
  Asked: import each `swarm-tenant-<tenant>-<vendor>` key as a pool key owned
  by that workspace (recommended: it becomes visible, capped and deletable), or
  leave them as the outage-only path they are.
* **Q8. Contract request 13. DECIDED 9 November 2026, DIFFERS from the
  recommendation.** Owner's answer: yes, take up the open contract change
  request so an attempt records the credential tier and the account it ran on.
  Filed as request 152, accepted by the owner on 9 November 2026, applied by
  the contract lane LP-C1 before its readers. (§5.4, §8.1, §8.2.)
  Asked: take it up (the attempt records its tier and account), or not now,
  since the hold log answers it (recommended).
* **Q9. Today's Voice it key. DECIDED 9 November 2026, as recommended.**
  Owner's answer: today's Voice it OpenAI key stays the dedicated Voice it key,
  in Voice it settings. (§5.3, §6.4.)
  Asked: keep it as the dedicated Voice it key (recommended: it is what it was
  added for), or move it to API Keys as a shared key.
* **Q10. The installer's question. DECIDED 9 November 2026, as recommended.**
  Owner's answer: the installer asks "Which will agents run on: plans, API
  keys, or both?"; its gate and summary check that choice. (§7.)
  Asked: ask at install (recommended: the team gate and the summary then check
  what the operator meant to add), or ask nothing and let the gate accept
  either kind.
