# swarm-api refusals: a new one ships switched off

There's no separate swarm-api error catalogue. Each refusal is an `ApiError`
subclass with an HTTP status and a stable `code`
([`errors.py`](../apps/swarm-api/swarm_api/errors.py)). This page covers the
one rule about adding a new refusal.

## The rule

**A refusal added to swarm-api ships report-only.** While its switch is off,
the request goes through. The refusal it would have made is logged as a
warning with its code, its status and the sentence the caller would have
seen:

    refusal report-only code=<code> status=<status> switch=REFUSAL_<CODE>=off message=<sentence>

A later release turns the refusal on, after that log shows only the refusals
that were intended. Owner decision 2026-10-08 (observer proposal I).

Refusals that existed on main on 2026-10-08 are **established** and always
enforced. None of them was switched off.

## Why

On 2026-10-07, the REPOSITORY_NOT_GRANTED-refusal change deployed `403 REPOSITORY_NOT_GRANTED` before the GitHub-App-id-and-slug change. The GitHub-App-id-and-slug change
delivered the GitHub App id and slug, which a person needs before they can
connect GitHub and grant repositories at all. The refusal itself was correct,
but nobody could satisfy it, so every person's submission was refused for
about two hours.

A refusal is only safe once a caller has some way to avoid it, and that way is
often a separate change that deploys separately. When the switch is off,
deployment order doesn't matter: the worst case is a log line, not a lockout.

## Adding a refusal

All of it lives in [`swarm_api/refusals.py`](../apps/swarm-api/swarm_api/refusals.py).

1. **Give it a code of its own**: an `ApiError` subclass with its own `code`,
   or a new `onboarding.COPY` failure code. The code is the switch's key. A
   new refusal raised as plain `forbidden` can't be switched or told apart in
   the log.
2. **Register a `Switch`** for that code in `SWITCHES`. Its `why` says what a
   caller needs before it can be turned on. `default` stays `False`.
3. **Raise it through `refusals.refuse(error)`**, not `raise error`. When the
   switch is off, `refuse` returns, so the code after the call must be the
   request being let through.

`tests/unit/control_plane/test_new_refusals_ship_off.py` makes the rule stick.
It lists every 4xx code swarm-api can raise: each `ApiError` subclass's
`code`, each `x.code = "..."` override and each `onboarding.COPY` failure
code. It fails on any code that is neither in its frozen established list nor
in `SWITCHES`. Don't add a new code to the established list. That list is the
state of main on 2026-10-08.

## Turning a refusal on

Set `REFUSAL_<CODE>=on` in swarm-api's environment. `<CODE>` is the code
upper-cased, with every run of other characters replaced by `_`. In a
deployment, that variable comes from terraform:

```hcl
# terraform/environments/<env>/<env>.tfvars
api_refusals = {
  some_new_code = true
}
```

`var.api_refusals` ([`variables.tf`](../terraform/infra/variables.tf)) is
rendered as `local.api_refusal_env` and merged into swarm-api's
`local.service_env` ([`locals.tf`](../terraform/infra/locals.tf)). Setting an
entry to `false` (`=off`) turns the refusal back off, which takes a terraform
apply, not a code change.

**A misspelt switch stops the revision from starting.** At process start,
`ApiSettings.from_env` refuses any `REFUSAL_` variable that names no switch,
and any value other than `on` or `off`. Otherwise a typo would leave a refusal
that everyone believes is on. `scripts/lib/check-env-parity.sh` can't make
this check, because the variable names come from a terraform map rather than
from literal `KEY =` lines.

Once a refusal has been on in every environment, a release may flip its
`default` to `True` and drop the tfvars entry.

## The one refusal that predates this

`REPOSITORY_NOT_GRANTED` is behind `REPOSITORY_GRANTS_ENFORCED`, which is off
by default and turned on in the onboarding migration step
([onboarding.md](onboarding.md) §3.5). Like a `REFUSAL_` switch, it is set
from tfvars, not by hand: `var.repository_grants_enforced` in
`terraform/environments/<env>/<env>.tfvars`, rendered into swarm-api's
environment by `terraform/infra/locals.tf`. It is on in dev and off in prod,
which registers no GitHub App, so nobody there could connect and be granted
anything. It applies to a person's agent only: a service submission keeps the
tenant token either way. It isn't one of `SWITCHES`. Its off position
doesn't let the request through unchanged; the agent runs with the tenant
token instead (`SubmissionService._resolve_forge`). So it keeps its own
setting and sits in the established list.

## Join requests: refusals with no switch, and why

Join requests ([design/join-requests.md](design/join-requests.md) §4.9) added these codes, all in [`errors.py`](../apps/swarm-api/swarm_api/errors.py).
**None has a switch**, and the rule above does not apply to them: the rule
exists so a new refusal cannot lock a person out of something they could do
yesterday, and every one of these gates a route or a store that did not exist
before that design. Turned "off", each would let the request through, and
letting it through is the breach the refusal is there to stop (a
non-decider's approval, a forged request approved). They are raised with
`raise`, not `refusals.refuse`, and listed in
`tests/unit/control_plane/test_new_refusals_ship_off.py`'s
`NOT_SWITCHABLE_CODES` with that reason.

| code | status | when | why it answers this way |
|---|---|---|---|
| `join_link_unknown` | 404 | a join code that is unknown, rotated, switched off, too long, empty, or for a workspace whose group changed | one answer and one body for every code that does not work, given before anything about the caller is read, so a code reveals nothing |
| `join_already_member` | 409 | the person is in the workspace already, by the group or by a platform membership | nobody needs to decide it |
| `join_already_reaches` | 409 | a platform admin asks | their reach already makes them `admin` there |
| `join_not_offered` | 409 | a request, membership or join link for a personal (`u-`) or service workspace | a personal workspace has one person |
| `join_request_unknown` | 404 | no request has that `ref`, or the caller may not withdraw it | another person's request reads exactly like none |
| `join_request_invalid` | 409 | the request's MAC fails: something changed it after it was signed | approving it would approve someone the admin never saw; nothing is written, and `swarm_api_join_request_invalid_total` counts it |
| `join_request_decided` | 409 | a decision on a request no longer `requested` | two admins deciding at once: one wins |
| `join_workspace_changed` | 409 | approving a request whose workspace is no longer the group the request named | the person asked to join a group that is no longer this workspace's; they may ask again |
| `join_role_not_allowed` | 409 | an approval choosing `admin`, or anything but the default or `viewer` | an admin is named person by person, after they have joined |
| `join_decider_required` | 403 | a decision, removal or join-link change by someone who is neither that workspace's `admin` (read fresh) nor a platform admin | only they decide who is in a workspace |
| `join_requests_open_limit` | 429 | the person already holds the most open requests | a person cannot flood every workspace's admins |
| `join_requests_daily_limit` | 429 | the person asked the most times in the last 24 hours | the same, over time |
| `join_denied_recently` | 429 | asking a workspace that denied this person within the hold; `detail.opens_on` is the UTC day it opens | a denial is not answered by asking again at once |
| `join_workspace_queue_full` | 429 | the workspace already holds the most open requests | only someone holding a working code can reach it, so it reveals nothing |

Two more answers are 503s, outside the 4xx guard, and never a refusal of the
person:

* **`membership_unavailable`**: a person's platform memberships could not be
  read (Firestore failed, or the bounded read stopped short of the tenant
  asked about). It answers only an `X-Swarm-Tenant` naming a non-`u-` id while
  `PLATFORM_MEMBERSHIPS` is on, and never `tenant_not_member`, because the
  unread membership could have changed the answer; a group member's request
  without the header is never stopped by it ([multi-tenancy.md](multi-tenancy.md) §2).
* **`join_signing_unavailable`**: a join request, decision, link or membership
  write in an environment with no settings key. An unsigned record is skipped
  by every reader, so nothing is written rather than writing one that admits
  nobody.

A service account asking to join is refused as plain `forbidden` (403): a join
request is a person's own (`routes/join_requests._person`). That route was new
too, so it has no switch either.
