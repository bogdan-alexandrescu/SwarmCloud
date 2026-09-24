# Deploy state

**Measured 2026-09-24 between 08:05 and 08:45 UTC, read-only, against
`saga-agents-staging`.** This replaces the record dated 2026-09-19. Every figure
below came from a `gcloud` describe or list, a Firestore REST `GET`, or a `gh`
read, and the commands are at the end. **Nothing was written** to any live
system to produce it.

Deliberately NOT read through `status.sh` or `smoke-test.sh`. Both are fixed
now (see [the sweep](audits/2026-09-18/13-swallowed-stderr-sweep.md)), but a
record of live state should not depend on the tools whose correctness it is
sometimes used to check.

## In four sentences

1. **What is running is a laptop deploy of `7c52762`, not main.** Every
   control-plane service and every worker job runs image tag `7c5276212251`
   ("Merge dense lane", 2026-09-23 09:10 PDT), rolled out at 2026-09-23 19:46 UTC.
2. **Main's release pipeline has never completed.** 44 of its 45 runs failed
   and one was cancelled. The latest built and promoted `b0fff1b` and then
   failed at `terraform plan`, denied the IAM policy of two IAP backend
   services; `deploy and smoke` was skipped.
3. **The front door works.** The DNS record the 2026-09-19 record was waiting on
   resolves, the certificate is `ACTIVE`, and `https://swarm.saga.xyz/` answers
   with IAP's Google sign-in redirect.
4. **Browser capacity is full, and nothing is using it.** Five `browser` leases
   expired at about 04:00 UTC and were never released; they hold `runner:browser`
   and `resource:browser` at 10 of 10, so no browser task can be admitted.

---

## What is running

`gcloud run services list` / `revisions list`, region `us-central1`:

| service | serving revision | image tag | revision created (UTC) |
|---|---|---|---|
| swarm-api | swarm-api-00050-zrc | `7c5276212251` | 2026-09-23 19:46:02 |
| swarm-scheduler | swarm-scheduler-00043-g5t | `7c5276212251` | 2026-09-23 19:46:02 |
| swarm-quota-broker | swarm-quota-broker-00049-f9n | `7c5276212251` | 2026-09-23 19:46:02 |
| swarm-reconciler | swarm-reconciler-00045-zh4 | `7c5276212251` | 2026-09-23 19:46:02 |
| swarm-ui | swarm-ui-00027-wwp | `7c5276212251` | 2026-09-23 19:46:02 |

All five take 100% of traffic on that revision, and all five report `Ready`.
Ingress is `internal-and-cloud-load-balancing` on every one. swarm-api's
revision carries `serving.knative.dev/creator: bogdan@saga.xyz` -- an operator
deploy, not the release workflow's service account.

`gcloud run jobs list`: eleven per-tenant worker jobs (`swarm-job-{eng,smoke,
u-bogdan,u-sw-c90291}-*`) on `agent-runtime-base:7c5276212251`, and
`swarm-verify` on `swarm-verify:7c5276212251`. All labelled
`managed-by=swarm-terraform`.

**`7c52762` is older than main.** It is a commit on
`fix/silent-failures-env-parity-and-audits` from before that branch's later
merges, so nothing merged to main since -- including every fix in the
[GKE dispatch incident](incidents/2026-09-24-gke-dispatch.md), whose §5 says the
same thing about the scheduler -- is running.

### A label that lies

The four control-plane services carry a service-level label
`swarm-image-tag=5c99c79`. That is not what they run. The label is written only
by `scripts/lib/deploy.sh`'s direct `gcloud run services update` fallback, which
last ran at `5c99c79` (2026-09-20); terraform has deployed since and does not
manage that label. **Read the image, not the label.**

## What main would deploy, and why it has not

`gh run list --workflow release.yml --limit 100`: 45 runs, **44 failed and 1
was cancelled; none succeeded.** The latest, **run 35972131246 on `b0fff1b`**
(#17):

| job | result |
|---|---|
| verify | success |
| build and promote (dev) | **success** -- the libexpat refusal #17 fixed is gone |
| terraform apply (dev) | **failure**, at `terraform plan` |
| deploy and smoke (dev) | skipped |

The plan fails reading the IAM policy of two IAP web backend services, for the
`roles/iap.httpsResourceAccessor` binding to `domain:saga.xyz`:

    Error when reading or editing Resource "iap webbackendservice
    \"projects/saga-agents-staging/iap_web/compute/services/swarm-ui-backend\""
    ... "reason": "IAM_PERMISSION_DENIED"

and the same for `swarm-ui-ui-backend`. The `terraform` workflow's `plan (dev)`
on the same commit (run 35972131328) fails identically.

**This is the question `e233537` left open, answered in the negative.** That
commit met this exact 403 and granted the deployer `roles/iap.admin` with a
condition, `resource.name.startsWith(".../iap_web/compute/services/swarm")`,
rather than unconditioned -- because the project's other nine IAP backends are
another team's, `keycloak` and `argocd` among them. It recorded as unknown
whether IAP evaluates `resource.name` conditions at all, and predicted that if it
does not, "the next apply fails with the same 403". The plan at 08:30 UTC, an
hour after that grant was applied, fails with the same denial. That is
consistent with the condition not being honoured, or not matching the name IAP
checks; **which of the two was not established here.** Either way the remedy is
an IAM decision on the deploy service account, and the unconditioned version of
it hands CI authority over another team's identity provider.

### The release promoted a build it did not make

Found while reading that run, and worth knowing before trusting any `:dev` tag.
The build step's digest table prints **two digests run together** for every
image:

    swarm-api   sha256:84485114...884297sha256:5f3afe39...627f59

`scripts/build-images.sh` reads each digest back with
`gcloud artifacts docker tags list --filter="tag:${TAG}"`, and a gcloud filter
`tag:X` matches any tag CONTAINING `X`. On a push to main the `application`
workflow has already built the same commit as `pr-<run>-b0fff1b8d43c`, so
`tag:b0fff1b8d43c` matches both, and `tr -d '[:space:]'` glues the two digests
into one string. The promote step then scanned and tagged the SECOND one:
`swarm-api:dev -> sha256:5f3afe39...`, which is `pr-35972131428-b0fff1b8d43c` --
the `application` workflow's build -- not `b0fff1b8d43c` (`sha256:84485114...`),
the image this release built. Checked in the registry for swarm-api:

    b0fff1b8d43c                  sha256:8448511469e9...
    pr-35972131428-b0fff1b8d43c   sha256:5f3afe399d88...
    dev                           sha256:5f3afe399d88...

Same commit, so probably the same code; but "the release promotes what it
built and scanned" is not true, and nothing failed. `build-images.sh` is not
this record's to change; it is reported with PR #25.

## The front door -- resolved

The 2026-09-19 record was "waiting on one DNS record". It no longer is:

| | measured |
|---|---|
| `dig +short swarm.saga.xyz A` | `8.232.87.184` |
| `swarm-ui-ip` (global address) | `8.232.87.184`, `IN_USE`, `managed-by=swarm-terraform` |
| `swarm-ui-cert` | `MANAGED`, `ACTIVE`, domain `swarm.saga.xyz` `ACTIVE` |
| `curl http://swarm.saga.xyz/` | `301` -> `https://swarm.saga.xyz:443/` |
| `curl https://swarm.saga.xyz/` | `302` -> `accounts.google.com/o/oauth2/v2/auth?...` (IAP) |
| `curl https://swarm.saga.xyz/readyz` | `302` (IAP, before the request reaches swarm-api) |

Path routing (`/v1/*`, `/healthz`, `/readyz`, `/metrics`, `/docs`,
`/openapi.json` to swarm-api, everything else to swarm-ui) was **not**
re-checked: every path answers IAP's redirect to an unauthenticated caller.

## The GKE cluster

`gcloud container clusters describe swarm-autopilot --region us-central1`:
`RUNNING`, Autopilot, control plane `1.36.4-gke.1082000`, created 2026-09-17,
labelled `managed-by=swarm-terraform`. Master authorized networks are enabled
with one entry, `0.0.0.0/0` ("open-dev-cluster"). Nothing was read from inside
the cluster -- no `kubectl`.

## Capacity: browser is full, and nothing is using it

Firestore database `swarm`, `pools` collection, by REST `GET`. Five pools show
`active: 10`:

    global  tenant:eng  provider:anthropic  provider:anthropic:tenant:eng
    backend:GKE_AUTOPILOT  runner:browser  resource:browser

`runner:browser` and `resource:browser` have a `hard_limit` of 10, so both are
**full**. The `leases` collection holds 330 documents; exactly five have no
`released_at`, and they account for all ten units (`units: 2` each):

| lease | task | created (UTC) | expires_at | heartbeat |
|---|---|---|---|---|
| lease_b8dc9372… | task_637eb5ea… | 03:40:06 | 03:42:06 | never |
| lease_c388c25e… | task_2a417cb2… | 03:55:07 | 03:57:07 | never |
| lease_d4d69212… | task_30074d78… | 03:55:07 | 03:57:07 | never |
| lease_e221353b… | task_64a451a6… | 03:55:08 | 03:57:08 | never |
| lease_66d39067… | task_719225c0… | 03:55:08 | 03:57:08 | never |

All five are `DISPATCHED` on runner profile `browser`, backend `GKE_AUTOPILOT`;
four of the tasks are steps of `wf_ebb3ab2d65664707a559`. Each lease's
`dispatch_deadline` passed at about 04:00. At the time of reading, **four and a
half hours** later, none had been reclaimed, although `swarm-reconciler-tick`
runs every five minutes and its last attempt (08:05:09) returned OK. The tasks'
own documents were last updated at about 07:40.

**Not diagnosed.** What is established is that the capacity is held by leases
nothing is heartbeating, past every deadline they carry, on a deployment that
predates the dispatch fixes. What is not established is why the reconciler has
not reclaimed them. Until something does, every browser task will wait
`READY` behind a pool that reads full.

## Pool ceilings: live against `dev.tfvars`

Still true, and still by design: `terraform/modules/firestore/bootstrap.tf`
puts `ignore_changes = [fields]` on the pool documents, because `active` is
mutated by every admission and an apply that rewrote the documents would zero
live concurrency counters. Terraform creates pools once; a running
environment's ceilings change through `PUT /v1/admin/limits/...`. So
`dev.tfvars` is what a NEW environment is born with, and the two are expected to
differ. They do, and in the other direction from 2026-09-19 -- the live
ceilings are now ABOVE the file's:

| pool | live `hard_limit` | `dev.tfvars` |
|---|---:|---:|
| global | 50 | 40 |
| provider:anthropic:tenant:eng | 20 | 15 (`provider_tenant`) |
| runner:claude-code | 40 | 20 |
| runner:browser | 10 | 4 |
| resource:browser | 10 | 4 |
| backend:CLOUD_RUN_JOB | 50 | 40 |
| backend:GKE_AUTOPILOT | 20 | 4 |

Every pool document is `enabled: true`. Nothing checks that the two agree, and
this table is not a claim that they should.

## Monitoring and the scheduler ticks

`gcloud scheduler jobs list`: `swarm-scheduler-tick` (every minute),
`swarm-reconciler-tick` and `swarm-quota-refresh` (every five), all `ENABLED`,
all with a last attempt at 08:05 UTC and status OK.

Alert policies (Monitoring REST): six, all enabled, all
`managed-by=swarm-terraform` -- `swarm-dev-scheduler-not-draining`,
`-tasks-dead-lettered`, `-control-plane-5xx`, `-generation-fencing`,
`-wake-messages-dead-lettered`, `-job-executions-failing`.

**The safety-tick alert is still not created**, and still cannot be:
`enable_safety_tick_alert = false` in `dev.tfvars`, and the project has **zero**
metric descriptors under `cloudscheduler.googleapis.com` -- the same query
returns 49 under `run.googleapis.com`, so it is an answer and not a failed read.
The 2026-09-19 expectation that the metric would appear "within ten minutes" of
the ticks running has not held in five days of ticks.

## In the project, not in terraform

**`swarm-authprobe`**, a Cloud Run service created 2026-09-20 14:46 UTC from
`cloud-run-source-deploy/swarm-authprobe`, labelled
`managed-by=scratch-delete-me`, ingress `all`. Its only `run.invoker` is the
`swarm-verify` service account, and an anonymous request is refused with `403`.
Its label says what it is for. **Not deleted**: this record writes nothing, and
deleting it is the owner's call.

## Carried over from 2026-09-19, NOT re-checked

* **The IAP brand is shared and is not ours.** `projects/209012342332/brands/
  209012342332`, "AI Agents", `orgInternalOnly: true`. Terraform neither creates
  nor modifies it: `google_iap_brand` cannot be deleted, so owning it would make
  the module impossible to destroy cleanly.
* **IAM granted to get the front door up:** `roles/compute.loadBalancerAdmin`
  and `roles/iap.admin` to bogdan@saga.xyz. IAP IAM took ~45 seconds to
  propagate, and the retry in between failed with the identical 403 -- which
  reads exactly like the grant not having worked.
* **`/readyz` answers 404 from a workstation for the `*.run.app` hostnames**,
  because ingress is `internal-and-cloud-load-balancing` and Google's edge
  refuses the request before it reaches the container. `deploy.sh` reports that
  as "not reachable from here" rather than as a failure.

## Outstanding, replacing the 2026-09-19 list

| 2026-09-19 said | now |
|---|---|
| the DNS A record | **done** -- resolves; certificate `ACTIVE` |
| `enable_safety_tick_alert = false` until the metric exists | **still true**; the metric still does not exist |
| `register-tenant.sh` keeps its three sweep findings | **fixed** in PR #25 (`a1239b5`) |
| `destroy.sh:340` reports the other team's cluster missing on an expired session | **fixed** in `f9eee2f` |

New, from this reading:

* the release pipeline cannot plan (IAP IAM read denied), so nothing on main
  reaches the platform;
* `build-images.sh`'s digest read-back matches tags by substring, so a release
  can promote a digest it did not build;
* five expired, unreleased browser leases hold browser capacity full;
* `swarm-authprobe` is a scratch service still deployed.

## How this was measured

    gcloud run services list / revisions list / revisions describe --region us-central1
    gcloud run jobs list --region us-central1
    gcloud run services get-iam-policy swarm-authprobe --region us-central1
    gcloud compute ssl-certificates list;  gcloud compute addresses list --global
    dig +short swarm.saga.xyz A;  curl -o /dev/null -w '%{http_code} %{redirect_url}'
    gcloud container clusters describe swarm-autopilot --region us-central1
    gcloud scheduler jobs list --location us-central1
    gcloud artifacts docker tags list .../swarm-images/swarm-api
    Monitoring REST: GET alertPolicies; GET metricDescriptors (filtered)
    Firestore REST (database swarm): GET pools; GET leases (all pages); GET tasks/<id>
    gh run list --workflow release.yml;  gh run view <id> --log-failed

`kubectl` was not used. No `gcloud ... update`, `create`, `delete`, `apply` or
Firestore write was issued.
