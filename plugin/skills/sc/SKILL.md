---
name: sc
description: Show and interpret SwarmCloud cluster state — the subscription account pool and its 5-hour/7-day quota windows, pool ceilings and which pool binds each runner profile, agents running and queued, and what is wrong right now. Use when asked "what is the swarm doing", "how much quota is left", "why is my task queued", "is anything broken", "which account is nearly full", or before dispatching a long batch.
allowed-tools:
  - Bash(uv run sc:*)
  - Bash(sc:*)
  - Bash(uv run swarm doctor:*)
  - Bash(uv run swarm profiles:*)
---

# sc — SwarmCloud cluster state

`sc` is read-only. It never writes, never refreshes a credential and never
cancels anything, so it is always safe to run.

## Which view

| Question | Command |
|---|---|
| what is the swarm doing? | `uv run sc` |
| how much quota is left? which account? | `uv run sc accounts` |
| why is my task queued? what is running? | `uv run sc agents` |
| what is the real ceiling? | `uv run sc capacity` |
| what did this agent produce? | `uv run sc task <id>` |
| is anything broken? | `uv run sc trouble` |
| what may I actually run? | `uv run swarm profiles` |

Add `--json` for the numbers, `--width N` to force a column count, `--ascii`
for a terminal without the bar glyphs.

`swarm profiles` is the odd one out and is listed here because it is read-only
and because it is the question people ask next. It reads the frozen catalogue
rather than the cluster, so it makes **no network call** and still answers when
nothing else does — which is exactly when someone is guessing at a profile name
because the API is unreachable. It shows no image and no command, deliberately:
a caller names a profile and the execution details follow from the name.

## Reading the marks — this is the part that matters

Three marks, and they are the same three `cs status` uses:

| Mark | Means | How to report it |
|---|---|---|
| `12%` | measured, recent enough to trust | quote it |
| `~12%` | the **last** measurement, too old to trust | say "projected", or give the reading's age. Never quote it as current |
| `—` | **not measured** | say "unknown". Never say zero, never say "fine" |

**Never turn an em dash into a number.** "This account has used none of its
quota" and "nobody has asked this account how much quota it has used" are
different claims, and collapsing them turns a dead poller into a healthy-looking
pool. If the operator needs the real figure and it is stale, say so and stop —
do not estimate it.

A section that reports it could not be read is a **finding**, not an absence.
Report it as such: "the account pool could not be read — utilisation is
unknown", not silence.

## Reading capacity

`BINDS ON` is the pool that will actually refuse the next task of that profile.
Admission is all-or-nothing across every pool a profile requires, so the ceiling
a profile feels is the **tightest** of them — not the global one, which is the
one people quote. `ROOM` is how many more tasks of that profile fit before the
binding pool says no.

A pool absent from the list is unlimited by construction and shows `∞`. A pool
whose `enabled` flag did not arrive shows `? not reported` rather than "open" —
a paused pool rendered as open is the one error this column exists to prevent.

`ROOM` is `—` whenever **any** required pool could not be graded — one that did
not report its ceiling, or did not report whether it is paused. Admission is
all-or-nothing, so one unknown pool makes the real room unknown, and it could
be 0. Do not fill that dash in from the other pools' numbers: they are the
pools that did not object.

## Reading accounts

Columns are `cs status`'s: `ACCOUNT | 5H | 7D | CLEARS | STATE`. `CLEARS` is
when the **binding** window — the fullest one — rolls over.

States: `available`, `paused`, `draining` (takes no new agents), `reauth needed`
(the credential is dead; only the quota-broker can fix it).

Every account in this pool is a **Claude subscription**. There is no other kind
and no choice to make about one, so there is nothing to ask the operator here.

Never ask for, echo, or store an account's credential. `sc` cannot show one:
the API shape carries no key material, not even a token's length, and `--json`
goes through the same allow-list the screen does. Rotating a credential
**revokes the previous one**, so quota-broker is the platform's single writer
for them — not `sc`, not swarm-api, and not this session.

## Exit codes

They are the machine-readable half of this surface, and every view uses them
the same way:

| Code | Means | Report it as |
|---|---|---|
| 0 | everything printed was read, nothing is down | healthy |
| 1 | something this view is about could not be read | "I could not read X" — an alert about the path to the cluster |
| 3 | it was all read, and something is **down** | "the cluster is broken, here is what" |

1 and 3 are different claims. A cluster nobody can reach is not a cluster that
is down, and reporting either as the other sends someone to the wrong place.

## When sc cannot connect

`sc` exits 1 and prints nothing to stdout, on purpose: a screen of dashes would
look like a reading. Run `uv run swarm doctor` — it reports which of the four
auth tiers this machine is on and what that tier can reach, which is almost
always the real answer.

### "I cannot reach it" is not "it is down" — say the first one

This is the single most likely wrong report from this plugin, so it is worth
being exact. On a **team** deployment the API is behind IAP at a load balancer,
and `swarm doctor` prints which door it used and what that door takes:

```
front door  https://swarm.saga.xyz  (IAP; takes an OAuth ACCESS token)
api         UNREACHABLE
            GET /v1/tenants/me -> 401: ... Error code 900
```

Read the refusal, because the three of them mean three different things:

| What comes back | What it means |
|---|---|
| **401, IAP error code 900** | IAP did not accept the token at all. A *user* credential cannot pass here: this deployment sets no `oauth2_client_id`, so IAP uses a Google-managed OAuth client and there is no audience a laptop can mint against. Set `SWARM_IMPERSONATE_SA` |
| **403 that NAMES the caller** | IAP **authenticated** you and the principal is not on the list. One `roles/iap.httpsResourceAccessor` grant away — `frontend_iap_members` in Track C's tfvars |
| **an HTML 404** | the wrong ADDRESS, not a missing route. Cloud Run's `*.run.app` hostname refuses everyone outside the VPC and renders the refusal as 404 |

None of those is the cluster being down. **Say "I cannot reach the API from
here, and this is why", never "SwarmCloud is broken"** — and if someone needs
the numbers now, the console at the front-door host is signed in to the same
API in a browser, where the session cookie is a credential this bridge does not
have and must not go looking for.

Exit code 1 is exactly this case, and exit code 3 is the other one. That is the
whole reason they are different numbers.

If accounts specifically are unreadable while everything else works, this
deployment's swarm-api has no `/v1/accounts` route: the pool is **unknown from
here**, not empty, and the cluster is otherwise fine — `sc` and `sc trouble`
say so as a note and still exit 0. There is no environment variable that makes
`sc` try a second host, and asking for one would be asking it to send a bearer
token minted for swarm-api somewhere else. An operator on the VPC who wants to
read quota-broker directly points `SWARM_API_URL` at it, so the token is minted
for the host that receives it.
