# LLM providers: plan accounts and API keys as two pools, a plan-then-key-then-Voice-it fallback, and Capacity › LLM Providers

The owner's request, 9 November 2026 (kept in his words):

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

This document is the design. It builds nothing. Every choice that is the
owner's (cost, security, product) is a question in §9, with options and a
recommendation, and is not decided here.

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
| `may_serve(tenant)` is ownership or an explicit loan, and requires the `workspace` pool (`accounts.py:835-845`) | Lending a key to workspaces is the same `lend_to` field; "share with Voice it" is the `voice-it` pool membership. |
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
`swarm-account-<tenant>--<label>`, the same naming as a plan
(`accounts.py:333-370`), with no `-refresh` half.

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

One typed confirmation (the key's label), then, in order:

1. **Holds close in one transaction**: `remove_account_and_close_holds`
   (`main.py:1629-1703`), unchanged: every live hold closed as
   `account_removed`, the document deleted. No hold is left half-closed.
2. **Leases in flight**: an attempt already running on the key keeps the value
   it read into its process; the broker cannot take it back. At its next turn
   boundary its `hold-status` answers `move` (the hold is gone), and it swaps
   to the next tier or checkpoints and parks. So a deleted key stops being used
   within one turn, not instantly. A Voice it Live session on the key is ended
   at its next renew (every 240 s, `accountlease.py` in swarm-voice) and
   reopened on the next tier, or ends with the reason.
3. **The secret**: its versions are **destroyed** once the last hold that was
   open at deletion has closed or expired (at most the hold TTL), and then the
   secret is deleted. The broker records the deletion in the account's
   `account_holds` log and the audit log, never the value.
4. **The provider**: deleting here does not revoke the key at Anthropic or
   OpenAI; a stolen copy keeps working. The confirmation says so behind its
   (i), and the owner revokes it at the provider. Whether the broker should
   revoke it itself (which needs an organisation admin key) is Q4.

This departs from today's `"secret": "retained"` for plan accounts on purpose:
a plan's secret holds a refresh token that can be re-used after an accidental
removal; a key's secret holds a paid credential the owner asked to delete. Plan
removal is unchanged. Whether delete destroys at once or after a grace period
is Q3.

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
| Model classes | `model_classes` | the same | `serves_class` |
| Per-workspace cap | none | `per_workspace_max: {tenant: n}`, new on both kinds (Q6) | live holds whose tenant is that workspace, inside the same `acquire_hold` transaction |
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
Whether to read the real figure with an admin key is Q5.

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
`per_workspace_max`; it is not limited (`limited_until` passed); it is not
exhausted (`exhausted_at` unset); and its month's metered spend is under
`monthly_spend_cap_usd` if one is set. It has **no quota left** when it is
limited, exhausted or over its spend cap.

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
  capped at one output token; Q5 covers its cost) succeeds.

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
| Voice it planner | Anthropic plans in the Voice it pool | Anthropic keys shared with Voice it | none: refused (Q2) |

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

**Back up the chain.** Whether an attempt on a key moves back to a plan as
soon as one is available (cheaper, one more restart) or stays on the key to
the end of the attempt (simpler, pays per token) is Q1. Either way a **new**
attempt always starts at tier 1.

**The per-tenant secret.** Today's `no_accounts_registered` → tenant secret
path (`accounts.py:284-286`) and the broker-unreachable degrade
(`accountlease.py:46-58`) stay as they are: a control-plane outage still runs
on the tenant secret. Whether existing tenant secrets are imported as keys in
the pool is Q7.

**Voice it.** swarm-voice's `POST /v1/voice/lease` (`capability: live` or
`text`) and the planner's `POST /v1/accounts/assign` (`holder: voice-planner`)
walk their rows of the table the same way. Tier 3, the **dedicated Voice it
key**, is read only when tiers 1 and 2 have no available account, and is
never offered to an agent step: it is an `openai-api` account in the
`voice-it` pool alone, never lent, marked `voice_dedicated` (one per
deployment), which is exactly what today's Voice it key is. A Live session
renews every 240 s; a renew that finds its key limited or exhausted is
refused, and swarm-voice reopens Live on the next tier at the next turn
boundary instead of ending the session.

### 5.4 What the person sees

* **An agent step**: a chip on the attempt, `Plan` or `API key`, with the
  account's display name in its tooltip; a tier change is one line on the
  attempt's timeline ("moved to API key", the reason in the tooltip). A park
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
* **3. Concurrency counts from `LEASED`.** `max_agents` and the new
  `per_workspace_max` count live holds from the moment of the grant, which
  happens while the attempt is `LEASED`, never from `RUNNING`.
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
  lent-to count, Voice it mark.
* **Add**: a vendor choice, then today's sign-in for that vendor: the Claude
  sign-in, or the Codex sign-in for a ChatGPT plan (`sc account add --provider
  openai` today, `openai_plan.py`); or a one-time `pool-account` link for the
  plan's owner, generalised with a vendor.
* **Controls**: pause, drain, remove (typed), max agents, per-workspace caps,
  lend, model classes, share with Voice it, refresh, rename.
* **Empty state**: "No plans" with the add control beside it, and a `Mark`
  stating the read succeeded (today's `acct-empty`, `Accounts.tsx:807-848`).

### 6.3 API Keys

* **List**: one row per key, grouped by vendor; state (available, limited
  until, exhausted, paused, draining), live holders over `max_agents`, the
  month's metered spend over its cap, lent-to count, Voice it switch, added
  by and when. The value is never shown, not even masked; there is nothing to
  reveal.
* **Add**: vendor, label, then the key (one-step form) or "send a link".
* **Controls**: pause, drain, resume (after credit is topped up), replace key,
  delete (typed), max agents, per-workspace caps, spend cap, lend, model
  classes, share with Voice it.
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

* **The question** "Which will agents run on: plans, API keys, or both?"; a
  key answer offers the one-step key form as a console link at the end, never
  a prompt in the terminal (the installer must not see a key either).
* **The team gate** (`team.py:388-413`, `await_pool_account`) accepts a key
  lent to the team as well as a plan, so a deployment with keys only finishes.
* **Voice it** (`tfvars.py:57-78`): when the answer is yes, the summary links to
  Voice it › Settings › Dedicated OpenAI key and says Live needs a key.
* **The IAM grant** of §8.3, through Terraform, so the delete path works on a
  fresh project without a hand step.
* **The final summary** prints each chain of §5.3 with what it has now
  (`claude-code: 0 plans, 1 key`; `Voice it Live: no key`), read from the
  broker's `GET /v1/accounts` and `GET /v1/voice/pool`, so an operator sees a
  chain with no tier before the first step parks on it.
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
| **LP-B1** broker: the `anthropic-api` kind, `VENDOR_OF`, key validation, `exhausted_at`, `per_workspace_max`, `monthly_spend_cap_usd`, `voice_dedicated`, the `api-key` registration path | `apps/quota-broker/quota_broker/accounts.py`, a new `api_key.py` (generalising `openai_api_key.py`), `main.py` registration and caps routes, their tests | Q6 |
| **LP-B2** broker: the tiered walk in assign, swap and the Voice it lease; the `tier` field; the soonest reason | `apps/quota-broker/quota_broker/main.py` assign, swap and voice routes, `accounts.py` `accounts_serving`, their tests | LP-B1; Q1 |
| **LP-B3** broker: delete with secret destruction, rotate, resume, the hourly exhausted probe | `accountstore.py`, `secretstore.py`, `main.py` delete route, their tests | LP-B1; Q3, Q4, Q5; the IAM change of §8.3 |
| **LP-W1** worker: the key shapes in `credential_env_from_account`, the `credit_exhausted` outcome, the tier on the attempt's timeline, the cross-kind swap restart | `apps/agent-worker/agent_worker/accountlease.py`, `runners/cliagent.py`, their tests | LP-B2 |
| **LP-A1** swarm-api: the `api-key` setup-link kind, the redeem path, the Voice it settings' dedicated-key status | `apps/swarm-api/swarm_api/setup_links.py`, `routes/setup.py`, `routes/voice_settings.py`, their tests | LP-B1 |
| **LP-V1** swarm-voice: Live, text and planner leases walk the chain; the developer Responses wire for text on a key; `ANTHROPIC_API_KEY` for a planner turn on a key; reopening Live on the next tier | `apps/swarm-voice/swarm_voice/accountlease.py`, `plannerlease.py`, `textchat.py`, `claudeturn.py`, their tests | LP-B2; Q2 |
| **LP-M1, M2, M3** mock-ups (§6.6) | `docs/web-ui/mockups/llm-providers-*.html` | nothing |
| **LP-U1** console: the `llm-providers` tab and routing, the redirect | `apps/swarm-ui/src/App.tsx`, a new `LlmProviders.tsx`, its test | LP-M1 pick |
| **LP-U2** console: Plans (the board moved out of `Accounts.tsx`) | `apps/swarm-ui/src/Accounts.tsx`, a new `LlmPlans.tsx`, its test | LP-U1; LP-M1 pick |
| **LP-U3** console: API Keys list, add, controls | a new `LlmApiKeys.tsx`, `apps/swarm-ui/src/api.ts` (the key calls only), its test | LP-U1; LP-M1, LP-M2 picks; LP-B1, LP-B3, LP-A1 |
| **LP-U4** console: Voice it › Settings' dedicated key, the session tier mark | `apps/swarm-ui/src/voice/Settings.tsx`, `apps/swarm-ui/src/voice/Talk.tsx`, its test | LP-M3 pick; LP-A1 |
| **LP-I1** installer: §7 | `apps/swarm-install/swarm_install/team.py`, `tfvars.py`, their tests | LP-B1 |
| **LP-T1** Terraform: §8.3 | the broker's IAM file under `terraform/infra/` | Q3 |

Each UI lane touches at most three files. LP-U2 and LP-U3 both depend on
`api.ts` only through LP-U3; LP-U2 reuses the existing plan calls, so the two
never edit the same file. Two broker lanes edit `main.py` in disjoint routes;
if the orchestrator prefers, LP-B2 waits on LP-B1's merge (the table says so).

### 8.2 The frozen contract

**No change is required.** The catalogue already declares both credentials for
every profile the chain serves (`secrets_any_of=True`), the broker's `Account`
is not in `apps/common/swarm_common/`, and the scheduler's `SlotPool`, `Lease`
and `QuotaState` are unchanged. One optional request touches this design:
open request **13** ("`Attempt` does not record which pool account it ran
on") would let the attempt itself carry the tier and account; the timeline
works without it from the broker's `account_holds` log. Whether to take it up
is Q8, and if yes it is an entry in `docs/contract-change-requests.md`, never
an edit under `apps/common/swarm_common/`.

### 8.3 IAM, Terraform and workflows

* **IAM / Terraform**: the broker can add and expire versions but has no
  `secretmanager.secrets.delete` (and destroys versions only through
  `_destroy_stale_versions`). Deleting a key's secret needs
  `secretmanager.secrets.delete` added to the broker's custom role, scoped by
  an IAM condition to secrets named `swarm-account-*`. One Terraform change in
  lane LP-T1, labelled `managed-by=swarm-terraform`, touching nothing on the
  never-touch list. Not needed if Q3 picks "retain".
* **Per-secret accessor grants** for lent keys are made by the broker at
  runtime, as for lent plans; no Terraform change.
* **Workflows**: none. The lanes' tests are picked up by the existing jobs.

## 9. Questions for the owner

Each is in the lane's `questions.json` with options and a recommendation.

* **Q1. Back up the chain mid-run?** Move an attempt from a key back to a plan
  at a turn boundary when one is free (recommended: keys cost per token, a
  restart costs seconds), or keep it on the key to the end of the attempt.
* **Q2. A dedicated Anthropic key for the Voice it planner?** The request names
  only a ChatGPT key for Voice it. Recommended: no; the planner refuses when
  plans and shared Anthropic keys are spent, as it does today.
* **Q3. Delete: when is the secret destroyed?** After the last open hold ends
  (recommended), at once, or never (retain, as for plans).
* **Q4. Revoke at the provider?** Recommended: no; the broker would need an
  organisation admin key, a far wider credential than the one deleted. The
  confirmation says to revoke it at the provider.
* **Q5. Spend: metered estimate or the provider's figure?** Recommended:
  metered from the worker's `cost_usd`; the provider's figure needs an admin
  key per organisation. Also whether the hourly probe of an exhausted key
  (a one-token completion) is acceptable.
* **Q6. Per-workspace caps on plans too?** Recommended: yes, one field on both
  kinds, so the two pages have the same controls.
* **Q7. The per-tenant secrets.** Import each `swarm-tenant-<tenant>-<vendor>`
  key as a pool key owned by that tenant (recommended: it becomes visible,
  capped and deletable), or leave them as the outage-only path they are.
* **Q8. Contract request 13.** Take it up so the attempt records its tier and
  account (recommended: not now; the hold log answers it).
* **Q9. Today's Voice it key.** Keep it as the dedicated Voice it key
  (recommended: it is what it was added for), or move it to API Keys as a
  shared key.
