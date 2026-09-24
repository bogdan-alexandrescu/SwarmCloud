# The in-VPC gate is 2 of 3, and the third one needs a write

**2026-09-22. A recommendation for Track C, and the reasoning that produced it.
Nothing in `terraform/` was changed by this note.**

`make verify-remote` runs the verification suites as a Cloud Run job inside the
VPC, because swarm-api's ingress refuses a laptop
(`docs/audits/2026-09-20/verification-targets-cannot-run.md`). Two of the three
targets pass:

    smoke-test        10/10
    concurrency-test   5/5   150 tasks, pool-over-limit samples 0, peak global 2 of 50
    race-test         fails at step 1

        [1] Narrow runner:mock to 1 slot(s)
         fail Firestore PATCH returned HTTP 403

Nothing is broken. `scripts/race-test.sh` narrows `pools/runner:mock` to one
slot so that twelve tasks have something to contend for, and narrowing a pool is
a Firestore **write**. The verify identity cannot write Firestore.

## What the verify identity actually holds

Read from the live project on 2026-09-22, not from the `.tf`:

    $ gcloud projects get-iam-policy saga-agents-staging --format=json
      roles/datastore.viewer   serviceAccount:swarm-verify@saga-agents-staging…
      roles/run.viewer         serviceAccount:swarm-verify@saga-agents-staging…

plus two resource-scoped grants that do not appear in the project policy and are
declared in `terraform/infra/verify.tf`: `roles/run.invoker` on the `swarm-api`
service, and `roles/storage.objectViewer` on the artifact bucket. No secret
access, by design — the suites use the `mock` runner profile, which has
`provider=None`, so this identity never touches a subscription credential.

It is also `ALLOWED_USERS` on swarm-api (`terraform/infra/locals.tf:387`) and
owns tenant `u-sw-c90291` (`terraform/environments/dev/dev.tfvars:266`). It is
**not** in `admin_users`, which today is `["bogdan@saga.xyz"]` alone.

## Option (a): give the verify identity a Firestore write role

### (a1) `roles/datastore.user`

    $ gcloud iam roles describe roles/datastore.user
      datastore.entities.create   datastore.entities.delete
      datastore.entities.get      datastore.entities.list
      datastore.entities.update   …

Firestore IAM **cannot scope below the database**. This repository already knows
that and has it in two places: `terraform/modules/tenancy`'s
`scope_firestore_to_database` defaults to false, and
`tests/integration/test_register_tenant_grants.py` exists because a conditioned
Firestore binding does not narrow data access, it removes it. So this grant is
create/update/**delete**/list over *every document in the `swarm` database*:
`tasks`, `leases`, `pools`, `attempts`, `tenants`, `quota`, `control`,
`accounts`.

Two of those matter more than the rest.

* **`pools/*.active`.** `swarm_common.models.SlotPool` says `active` is mutated
  only inside the admission and release transactions. The header of
  `scripts/race-test.sh` records what happened the one time this suite wrote it
  through a plain REST PATCH: an admission committing between the read and the
  write was clobbered, the count dropped below its true value, and capacity was
  inflated **on a live deployment** — and the "narrowed pool is never over its
  limit" assertion would then have passed on a platform that was genuinely
  oversubscribed. The only thing preventing a recurrence today is that the field
  is not in the mask, which is a comment. Under (a1) it is only ever a comment.
* **`tenants/*`.** Those documents carry the per-tenant GSA, GCS prefix and
  secret names. Writing one is CONTRACT invariant 9, from a test identity.

The mitigation people reach for here — "the suite would never do that" — is
exactly the claim the incident above disproved.

One thing (a1) does **not** cost: reach into another team's data. The shared
project holds one Firestore database, `projects/saga-agents-staging/databases/swarm`,
and it is ours (`gcloud firestore databases list`, 2026-09-22). But a
project-level binding follows any database created later, and the other team
creating one is not an event this repository would notice.

### (a2) the custom role this repository already maintains

`swarmTenantWorkerFirestore` exists and is narrower:

    $ gcloud iam roles describe swarmTenantWorkerFirestore --project saga-agents-staging
      datastore.databases.get
      datastore.entities.create
      datastore.entities.get
      datastore.entities.update

No `delete`, no `list`. Granting that to `swarm-verify` (which already has
`list` and `get` from `datastore.viewer`) is a strictly smaller step than (a1):
it can rewrite any document, but it cannot destroy one. It still leaves
`pools/*.active` writable, which is the specific field the 2026-09-19 incident
turned into an inflated ceiling.

It also breaks one path in race-test: the `POOL_EXISTED=0` branch of `restore`
deletes the pool it created. `runner:mock` exists (`hard_limit=20`), so that
branch is not taken in practice, and the script now reports the failure loudly
rather than claiming a removal that did not happen.

## Option (b): make the verify identity a platform admin

`ADMIN_USERS` is an email list consulted **after** the token is verified
(`apps/swarm-api/swarm_api/auth.py:342,421`), so it changes which verified
identities are admins, never whether the identity was verified. Adding
`swarm-verify@saga-agents-staging.iam.gserviceaccount.com` to `var.admin_users`
opens these nineteen routes and nothing else:

| | |
|---|---|
| **reads** | `GET /v1/admin/pools`, `/leases`, `/quota`, `/tenants`, `/metrics`, `/dispatch` |
| **limits** | `PUT /v1/admin/limits/{global,provider/*,provider/*/tenant/*,backend/*,resource/*,runner/*,tenant/*}`, `PUT /v1/admin/tenants/*/limits` |
| **switches** | `POST /v1/admin/dispatch/{pause,resume}`, `/providers/*/drain`, `/providers/*/enabled`, `/resources/*/drain` |

Two observations that decide this, both from reading the code rather than the
route list.

**The read half grants nothing new.** Every one of those GETs is served from
Firestore documents that `roles/datastore.viewer` already lets this identity
read directly — and the suites do read them directly, by design
(`scripts/lib/testlib.sh`: "OBSERVATIONS read Firestore directly… reading state
from the same API that is under test would let a broken API report success").
The only other widening from `is_admin` outside `/v1/admin` is two extra fields:
`platform_tasks_by_state` on `stats` and the unfiltered pool list on `capacity`
(`service.py:453,496`). Both are already readable from Firestore.

**The write half cannot express the bug that has actually happened here.**
`Store.upsert_pool` (`store.py:677-723`) writes `hard_limit`, `enabled`,
`adaptive_target`, `quota_derived_limit`, `updated_at` — and never `active`. It
also cannot invent a pool: `PUT /limits/runner/{rp}` validates `rp` against the
frozen `RUNNER_PROFILES` and 400s otherwise. There is no route that deletes a
lease, writes a tenant document, creates a tenant or reads a secret.

What it *does* grant that is genuinely new, and should not be glossed:

* `POST /v1/admin/dispatch/pause` stops dispatch **platform-wide**.
* `POST /v1/admin/providers/{p}/drain` sets `enabled=false` on the provider pool
  **and on every per-tenant provider pool** (`routes/admin.py:309-312`), i.e.
  every tenant's work on that provider, not just the caller's.
* `PUT /v1/admin/limits/global` can take the whole platform to zero.

All three are reversible in one call, all three are the operator actions the
admin surface exists for, and none of them can corrupt an invariant — they
change ceilings, they do not change counts.

### A correction to the premise

The suggestion that started this said the admin route "is validated and
audited". Validated, yes. **Audited, no.** Every limit route increments
`ctx.metrics.admin_actions.labels(action=…)` — a Prometheus counter carrying the
action name and *nothing else*: no caller, no pool, no value. `grep -rn audit
apps/swarm-api/` returns nothing. `upsert_pool` writes `updated_at` and no `by`.
The only place the admin surface records *who*, is
`set_dispatch_paused(…, by=auth.email, reason=…)` on the control document.

So the honest version is: **a bounded, validated surface with attribution on
one route out of nineteen, plus whatever the Cloud Run request log keeps.** That
is still a much better record than a Firestore PATCH, which leaves none at all —
but it is not an audit trail, and if this decision is being made partly on
"it is audited", it should be made on something else.

## Recommendation: (b), and add the attribution

Narrowing a pool is a *ceiling change*, and the platform already has exactly one
supported, validated, reversible way to change a ceiling. Option (a) does not
give the suite the ability to change a ceiling; it gives the suite the ability
to write **any document the platform owns**, and then relies on the suite only
writing the one it means to. That is the property that already failed here once,
in this very script, in the direction that made a broken platform look healthy.

The decisive asymmetry is not the size of the route list. It is that under (b)
the *shape* of the dangerous bug is unreachable: no admin route writes `active`,
no admin route touches a tenant document, and a typo in a pool name is a 400
rather than a new document. Under (a) — either variant — a one-character mistake
in a field mask is a silent write to live capacity accounting.

The honest cost of (b) is that a buggy suite could pause dispatch or drain a
provider for every tenant. That is loud, immediately visible on the operator
screen, and undone with one call. The cost of (a) is quiet and is discovered
later by a capacity number that does not add up.

`docs/DEPLOY_STATE.md` already reached the same conclusion for the ceilings
themselves, for a different reason: *"changing a running one goes through
`PUT /v1/admin/limits/…`, the only path that writes `hard_limit` without
touching `active`."*

If (b) is taken, two things should land with it, and the second is not optional:

1. **The Track C change.** In `terraform/environments/dev/dev.tfvars`:

       admin_users = [
         "bogdan@saga.xyz",
         "swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
       ]

   `terraform/infra/locals.tf:371` already joins and sorts `var.admin_users`
   into `ADMIN_USERS`, so nothing else changes. This is Track C's file and was
   **not** edited by this note. `terraform/infra/variables.tf:357` should say
   why a service account is in a list whose comment describes operators.

2. **Attribution on the limit routes**, so a ceiling change made by the gate is
   distinguishable from one made by a person. `set_dispatch_paused` already
   takes `by=auth.email`; `upsert_pool` does not. That is Track A's, and is
   recorded here as a request rather than a change.

`scripts/race-test.sh` is **not** switched to the admin API by this note. The
mechanism is the decision being asked for, and the script's guards are correct
either way; what changed in it is that a narrow it cannot perform now stops the
run instead of being scored.

## The restore path, checked rather than assumed

The lane asked whether race-test restores the ceiling it narrows on every exit
path. The log after the 403 read

    ==> restored runner:mock hard_limit to 20

and that line was **false twice over**. The narrowing PATCH had been refused, so
nothing needed restoring; and the restoring PATCH was refused by the same IAM,
so nothing was restored either. `fs_patch … >/dev/null 2>&1 || true` followed by
an unconditional `info` discarded the status and the error, and printed the
success message in every case — the exact defect class this repository keeps
producing, in the cleanup path of the test written to catch it.

It matters beyond the log. The case the line exists for is the one where the
narrow *succeeded* and the restore then failed: `runner:mock` is left pinned at
one slot on a live deployment, every `mock` task afterwards serialises behind a
single lease, and the only record says it was put back.

Three faults, all fixed in `scripts/race-test.sh` and covered by
`tests/integration/test_race_test_refuses_an_unnarrowed_pool.py`:

* the verdict now comes from **reading the pool back**, not from the write's
  status, and names `scripts/pool-limit.sh --pool … --limit …` when it did not
  take;
* `trap restore EXIT INT TERM` ran the handler and then **resumed the suite**. A
  signal during the sampling loop restored the ceiling, cancelled every task,
  and then carried on asserting "the narrowed pool is never over its limit"
  against a pool that was no longer narrowed — and could exit 0. INT and TERM
  now re-raise;
* re-entry: the signal handler calls `restore` and exits, which fires the EXIT
  trap and enters `restore` again, cancelling every task twice. It is now
  idempotent.

## The three suites nobody has run in-VPC

### failure-test — should work today, unverified

It writes only through the API (`submit_task`, `cancel_task`) and reads only
`fs_get` / `fs_count` / `fs_list_docs` / `pool_active`. Its whole IAM
requirement is `roles/run.invoker` on swarm-api and `roles/datastore.viewer`,
both of which `swarm-verify` holds. It uses the `mock` profile throughout, so it
needs no provider key. Nothing in it narrows or drains anything.

Not run. It is the cheapest of the three to try and the most likely to pass:
`make verify-remote failure-test`. Its default `--timeout 600` plus a 180s drain
wait sits inside the job's 1800s timeout.

### load-test — runnable, but it does not do what its flag says

Two things to settle before it is pointed at the live platform.

**The rate limiter is not there.** `scripts/load-test.sh:74` paces submissions
with

    perl -e "select(undef,undef,undef,${INTERVAL_US}/1000000)" 2>/dev/null || true

and `images/swarm-verify/Dockerfile` is `FROM alpine:3.20` with one
`apk add --no-cache bash curl jq coreutils ca-certificates`. **Nothing there
brings perl.** The `|| true` swallows the missing binary, so in-VPC every
submission fires back to back: `--rate 10` is not 10/s, it is as fast as bash
and curl manage. The comment beside the line says "perl is present on every
macOS and every GitHub runner", which was true when it was written and stopped
being true when the image was built without a Cloud SDK.

*Read from the Dockerfile, not measured inside the image* — `docker run … which
perl` was not executed, and that is the one-command confirmation before acting
on this.

The consequence is not just an inaccurate number. `assert_eq "0"
"${SUBMIT_FAILURES}"` would then fail on the API's own rate limiting, and the
run would read as a platform defect.

**The cost, stated before anyone runs it.** The defaults are `--count 100
--rate 10 --profile mock --timeout 900`: **100 real mock tasks** against the
live dev platform, each a real Cloud Run job execution, submitted by tenant
`u-sw-c90291` whose `max_active` is 2. It cancels what it created unless
`--keep`. Against a `global` pool of 20 and a tenant ceiling of 2 the backlog
drains serially, so the 900s timeout is the thing most likely to end it. Nothing
here is expensive in money; it is expensive in *contention* — it will be the
dominant load on dev for as long as it runs, which is why it should not be
started while someone is using dev for anything else.

Fix the pacing first. Two ways, and which one is right is a decision about the
load profile rather than a typo, so neither was applied here:

* add perl to `images/swarm-verify/Dockerfile` — Track B's file, and it makes
  the in-VPC run behave exactly like the laptop run;
* replace the line with `sleep "$(…)"` — the image installs GNU `coreutils`,
  whose `sleep` takes a fractional argument, so no new package is needed. That
  is `scripts/load-test.sh`, which is Track D's, but it changes what `--rate`
  means on macOS too (BSD `sleep` also accepts fractions, so in practice it
  does not, but that is worth confirming rather than assuming).

Then decide the count.

### quota-test — not runnable, and the admin API is the wrong lever for it

Three separate blocks, in the order it hits them:

1. `TENANT="$(api_get /tenants/me …)"` resolves to `u-sw-c90291`, whose tfvars
   entry is `providers = []` on purpose. So `quota/anthropic:u-sw-c90291` does
   not exist and the script dies at `no quota document at …; register the tenant
   and store a <provider> key first`.
2. It drives quota state with `fs_patch` on that document — the same Firestore
   write race-test needs, on a different collection.
3. Its default profile is `claude-code`, which has a provider, so an admitted
   task would need a credential this identity deliberately cannot read.

Making it runnable means giving the verification tenant a **real provider
subscription**. That is a larger widening than anything discussed above: it puts
a paid credential behind a test identity. It is worth saying plainly that this
suite is not one grant away.

And the admin API does not help here, in the one place it looked like it might:
`POST /v1/admin/providers/{p}/drain` disables the provider pool *and every
per-tenant provider pool*, so using it to simulate exhaustion would park every
tenant's work on that provider. The Firestore write it does today is scoped to
one tenant's quota document and is genuinely narrower.

`scripts/quota-test.sh:77` carries the identical unconditional `info "restored
${PROVIDER} to ${ORIGINAL_STATE}"` that race-test had. It was not changed here —
that is a second file and a second suite, and it deserves the same before/after
proof rather than a sympathetic edit.

## What was not verified

* **Nothing was run against the live platform.** No task was submitted, no pool
  was narrowed, no suite was executed in the VPC. Every claim about race-test's
  behaviour comes from the offline harness, and every claim about IAM comes from
  read-only `gcloud` calls named above.
* The recommendation has not been applied. `swarm-verify` is still not in
  `admin_users`, so race-test still fails at step 1 — with a better message and
  an honest cleanup, but it fails.
* `failure-test` in-VPC is an argument from reading its source, not a result.
* The `--parallel`/`--limit` argument checks and the "contention was actually
  observed" assertion are new and have never run against a real platform. If
  the second one is wrong, it is wrong in the direction of a false failure, not
  a false pass.
