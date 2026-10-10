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

On 2026-10-07, #845 deployed `403 REPOSITORY_NOT_GRANTED` before #840. #840
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
anything. It applies to a person's task only: a service submission keeps the
tenant token either way. It isn't one of `SWITCHES`. Its off position
doesn't let the request through unchanged; the task runs with the tenant
token instead (`SubmissionService._resolve_forge`). So it keeps its own
setting and sits in the established list.
