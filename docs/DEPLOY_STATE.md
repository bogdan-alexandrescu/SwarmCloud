# Deploy state

**Measured 2026-10-27 between 05:52 and 06:12 UTC (22:52-23:12 PDT
2026-10-26), read-only, against `saga-agents-staging`, region `us-central1`.**
This replaces the record dated 2026-09-24, which is kept below as
[the previous reading](#previous-reading-2026-09-24). The header clock was read
with `date -u` at 2026-10-27 06:11:48Z; origin/main at read time was
`17d6c5abd` (#2533). Every figure below came from a `gcloud` describe or list,
a read-only `kubectl` get, a Firestore or Monitoring REST read, or a `gh` read,
and the commands are at the end. **Nothing was created, updated, deleted,
deployed, cancelled or applied**, and no secret value was read. The operator
made the reading from their laptop (owner decision 2026-10-27, bundle H18: an
agent container is not given these reads); the field-reduced raw data is in the
operator's session scratchpad (`triage-1027/h18/`), not in this repository.

`kubectl` was used only with `--context
gke_saga-agents-staging_us-central1_swarm-autopilot`, through
`/opt/homebrew/bin/kubectl` v1.36.3 (an old client silently drops manifest
fields). The other team's `agents-staging` cluster, VPC and service accounts
were not read.

Deliberately NOT read through `status.sh` or `smoke-test.sh`: a record of live
state should not depend on the tools whose correctness it is sometimes used to
check (see [the sweep](audits/2026-09-18/13-swallowed-stderr-sweep.md)).

**A release deployed in the middle of this reading.** At 05:58:26Z every
service ran `bce4b227327c`. Release run 40454472082 (`17d6c5abd`) ran
`terraform apply (dev)` at 05:58:52-06:01:09 and `deploy (dev)` until
06:06:33. The tables below are the re-read from 06:02-06:08Z; where the
before-apply value matters it is given next to them.

## In four sentences

1. **What runs is main's head, `17d6c5abd` (#2533), deployed by the
   release workflow.** All six control-plane services, all thirteen
   terraform-managed worker jobs, `swarm-verify` and the four swarm images in `swarm-system` run
   digests tagged `17d6c5abd724` (`promoted-dev-17d6c5abd724`, `dev`). The two
   exceptions are `swarm-job-u-bogdan-mock` (`3d4e0872752b`, managed by the
   scheduler) and `swarm-workspace-apply` (`7c2e148dd0d2`).
2. **Main's release pipeline completes.** The last five release runs on main
   all succeeded; two of them (`177f6f2b6`, `e854f595f`) built images, then
   skipped promote and deploy. The newest acceptance run that ran its groups
   (`bce4b2273`) failed only `git-tools`; the acceptance run for `17d6c5abd`
   was still queued.
3. **The front door works, as before.** DNS, address, certificate and IAP
   redirect are unchanged from 2026-09-24.
4. **Capacity is consistent.** There are 21 open leases (22 units, all tenant
   `eng`, all `DISPATCHED` and heartbeating, none past `expires_at`), each
   pool's `active` equals the open lease units that name it, and no pool is
   full.

---

## What is running: Cloud Run services

`gcloud run services list` / `revisions describe`, region `us-central1`, read
06:02:21Z and 06:04:33Z:

| service | serving revision | image digest | registry tags on that digest | revision created (UTC) | traffic | Ready |
|---|---|---|---|---|---|---|
| swarm-api | swarm-api-00460-6sw | `1ebdb6fe623b` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` | 2026-10-27 05:59:51 | 100% | True |
| swarm-github-hooks | swarm-github-hooks-00095-86j | `cda9029b56d7` | same three | 2026-10-27 06:00:42 | 100% | True |
| swarm-quota-broker | swarm-quota-broker-00386-kkn | `7a256dd92506` | same three | 2026-10-27 05:59:51 | 100% | True |
| swarm-reconciler | swarm-reconciler-00385-6pr | `27d9620cb179` | same three | 2026-10-27 05:59:51 | 100% | True |
| swarm-scheduler | swarm-scheduler-00436-6df | `d613b9887d51` | same three | 2026-10-27 05:59:51 | 100% | True |
| swarm-ui | swarm-ui-00432-ms9 | `d7e19bf553a1` | same three | 2026-10-27 05:59:51 | 100% | True |

* Each revision's `serving.knative.dev/creator` is
  `swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com`, the
  release's deployer, not an operator. (On 2026-09-24 swarm-api's creator was
  an operator's own account.)
* Each service sends 100% of traffic to its latest revision; no service pins
  traffic.
* Ingress is `internal-and-cloud-load-balancing` on all of them except
  `swarm-github-hooks`, which is `all`.
* All six carry `managed-by=swarm-terraform`.
* At 05:58:26Z, before the apply, the serving revisions were
  swarm-api-00459-6jn, swarm-github-hooks-00094-fjd,
  swarm-quota-broker-00385-scl, swarm-reconciler-00384-n5t,
  swarm-scheduler-00435-rz6 and swarm-ui-00431-tgf. Each ran a digest tagged
  only `bce4b227327c` / `promoted-dev-bce4b227327c`, created 03:54:35Z
  (github-hooks 03:55:07Z).
* The project also holds services that are not SwarmCloud's: `oracle-api` and
  `oracle-web` (Product Oracle, images `product-oracle/{api,web}:2bc13abd`, no
  `managed-by` label).

### The label that lies is still there

swarm-api, swarm-quota-broker, swarm-reconciler and swarm-scheduler still carry
the service-level label `swarm-image-tag=5c99c79`. That is not what they run.
The label was written only by `scripts/lib/deploy.sh`'s direct `gcloud run
services update` fallback, which last ran at `5c99c79` (2026-09-20); terraform
has deployed every release since and does not manage that label. **Read the
image, not the label**: the commit below was mapped from digest to Artifact
Registry tag (`gcloud artifacts docker images list --include-tags
--filter=version=<digest>`), never from the label.

## What is running: Cloud Run jobs

`gcloud run jobs list`, read 06:02:21Z. Every image is pinned by digest:

| job | image | registry tags on that digest | managed-by | latest execution created (UTC) |
|---|---|---|---|---|
| swarm-job-eng-claude-code | `agent-runtime-base@918952ff8191` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` | swarm-terraform | 2026-10-27 04:03:03 |
| swarm-job-eng-codex | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:03:03 |
| swarm-job-eng-generic | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:03:03 |
| swarm-job-eng-indexer | `agent-runtime-indexer@ddb96b987690` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` | swarm-terraform | 2026-10-27 04:03:03 |
| swarm-job-eng-mock | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:13:55 |
| swarm-job-smoke-claude-code | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:04:46 |
| swarm-job-smoke-generic | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:04:46 |
| swarm-job-smoke-indexer | `agent-runtime-indexer@ddb96b987690` | same | swarm-terraform | 2026-10-27 04:04:46 |
| swarm-job-smoke-mock | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:24:43 |
| swarm-job-u-or-9a0792-generic | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:06:22 |
| swarm-job-u-or-9a0792-mock | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:06:22 |
| swarm-job-u-sw-c90291-generic | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:06:22 |
| swarm-job-u-sw-c90291-mock | `agent-runtime-base@918952ff8191` | same | swarm-terraform | 2026-10-27 04:26:22 |
| **swarm-job-u-bogdan-mock** | `agent-runtime-base@ee6fe197642d` | `3d4e0872752b`, `promoted-dev-3d4e0872752b` (#1915, 2026-10-22) | **swarm-scheduler** | 2026-10-22 04:55:54 |
| swarm-verify | `swarm-verify@50f431fc8ab5` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` | swarm-terraform | 2026-10-27 04:28:51 |
| swarm-workspace-apply | `workspace-apply@7a03edc768e6` | `7c2e148dd0d2`, `promoted-dev-7c2e148dd0d2`, `dev-promoted-7c2e148dd0d2` (#1640, 2026-10-19) | swarm-terraform | 2026-10-22 09:58:57 |

The latest executions predate the new digest: they are the `bce4b22` warm and
acceptance runs. `17d6c5abd`'s warm run belongs to accept run 40459142385,
which was still queued at 06:08Z.

## The GKE cluster and `swarm-system`

`gcloud container clusters describe swarm-autopilot --region us-central1`:

* `RUNNING`, Autopilot, control plane `1.36.4-gke.2326000` (2026-09-24:
  `1.36.4-gke.1082000`).
* Created 2026-09-17 21:46:37Z, labelled `managed-by=swarm-terraform`.
* Master authorized networks enabled, with one entry: `open-dev-cluster` =
  `0.0.0.0/0`.

`kubectl -n swarm-system get deploy,statefulset,daemonset`, re-read 06:08:31Z
(the release's deploy rolled these between 06:00 and 06:05). This is the first
reading of this record that looks inside the cluster; the 2026-09-24 one used no
`kubectl`.

| kind | name | ready | image | registry tags on that digest |
|---|---|---|---|---|
| Deployment | decision-api-egress | 1/1 | `ghcr.io/tailscale/tailscale:v1.104.1@87c55d6be56a` (2 containers) | (third-party) |
| Deployment | swarm-graph | 2/2 | `swarm-graph@17405c5bdb16` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` |
| Deployment | swarm-typesafe-fwd | 1/1 | `swarm-typesafe-fwd@738c3dc5da24` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` |
| StatefulSet | swarm-egress | 1/1 | `swarm-egress@9738dfd66ee9` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` |
| StatefulSet | swarm-live | 1/1 | `swarm-live@dcdb31a7e995` | `17d6c5abd724`, `dev`, `promoted-dev-17d6c5abd724` |

* There are no DaemonSets.
* Six pods, all `Running` and ready with 0 restarts, created
  06:00:36-06:05:12Z.
* At 05:58Z (before the deploy) graph, typesafe-fwd, egress and live ran
  `bce4b227327c` digests. typesafe-fwd was the last to move: still
  `6b8dbdf0d29a` at 06:04:33Z, `738c3dc5da24` by 06:08:16Z.

## Which commit the deployed services run

**`17d6c5abd724` = `17d6c5abd` = origin/main HEAD at read time** ("Fictional
example identities move from the company's domain to example.com, and the
frozen models.py comment is contract request 122 (part of #1647) (#2533)",
committed 2026-10-26 22:17 PDT). Until 05:59Z the deployed commit was
`bce4b2273` (#2523), four commits behind main.

## Main's release and acceptance runs

`gh run list --workflow release.yml --branch main --limit 5`, read 05:58Z and
again 06:08:16Z:

| run | commit | conclusion | jobs |
|---|---|---|---|
| 40454472082 | `17d6c5abd` | **success** (06:08Z; in progress at 05:58Z) | images and scan ✓ 05:23-05:54 · verify skipped · approval ✓ · plugin tag ✓ · promote ✓ 05:58:50 · terraform apply ✓ 06:01:09 · deploy ✓ 06:06:33 · IAM (dev-iam) skipped |
| 40450939409 | `e854f595f` | success | images and scan ✓; approval, promote, terraform apply, IAM, deploy all skipped |
| 40449385640 | `177f6f2b6` | success | images and scan ✓; approval, promote, terraform apply, IAM, deploy all skipped |
| 40449197675 | `aedb96572` | success (04:20-04:24Z) | not read by job |
| 40442880730 | `bce4b2273` | success | images and scan ✓ · approval ✓ · promote ✓ · terraform apply ✓ 03:55:46 · deploy ✓ 04:01:05 · IAM skipped |

`gh run list --workflow accept.yml --branch main --limit 5` (all
`workflow_run`):

| run | commit | conclusion | jobs |
|---|---|---|---|
| 40459142385 | `17d6c5abd` | **queued** (06:06:35Z) | -- |
| 40455047269 | `17d6c5abd` | success, 20 s | only `what dev runs` ran; warm/smoke/GKE proof and every acceptance group skipped |
| 40451053639 | `e854f595f` | success, 21 s | same: everything after `what dev runs` skipped |
| 40449572923 | `177f6f2b6` | success (04:24-04:25Z) | not read by job |
| 40447384962 | `bce4b2273` | **failure** | warm, smoke and GKE proof ✓ · browser ✓ · mock ✓ · workflow ✓ · claude-code ✓ · generic ✓ · tenant-isolation ✓ · **git-tools ✗** (04:28:31-04:35:07) · sweep ✓ · report ✓ |

The run before those, 40440511298 on `210f2611a`, was also a failure; it was
not read by job.

## The front door

| | measured 06:07:51Z |
|---|---|
| `dig +short swarm.saga.xyz A` | `8.232.87.184` |
| `swarm-ui-ip` (global address) | `8.232.87.184`, `IN_USE`, `managed-by=swarm-terraform` |
| `swarm-ui-cert` | `MANAGED`, `ACTIVE`, domain `swarm.saga.xyz` `ACTIVE` |
| `curl http://swarm.saga.xyz/` | `301` -> `https://swarm.saga.xyz:443/` |
| `curl https://swarm.saga.xyz/` | `302` -> `accounts.google.com/o/oauth2/v2/auth?...` (IAP) |
| `curl https://swarm.saga.xyz/readyz` | `302` (IAP, before the request reaches swarm-api) |

Path routing behind IAP was not re-checked: every path answers IAP's redirect
to an unauthenticated caller.

## Capacity

Firestore database `swarm`. Pools by REST `GET` (36 documents, all
`enabled: true`), open leases by a read-only `runQuery` (`released_at
IS_NULL`), both read 06:07:39-40Z:

| pool | active | sum of open lease units naming it |
|---|---:|---:|
| global | 22 | 22 |
| tenant:eng | 22 | 22 |
| provider:anthropic | 22 | 22 |
| provider:anthropic:tenant:eng | 22 | 22 |
| backend:GKE_AUTOPILOT | 22 | 22 |
| runner:claude-code | 20 | 20 |
| resource:standard | 20 | 20 |
| runner:claude-code-browser | 2 | 2 |
| resource:browser | 2 | 2 |
| every other pool (27) | 0 | 0 |

There are 21 open leases: 20 `claude-code` (1 unit each) and 1
`claude-code-browser` (2 units). All are tenant `eng`, `DISPATCHED`, created
05:07-06:07Z, with `expires_at` 06:09Z, which is after the read. 20 had a
heartbeat within 25 s of the read; one, created 06:07:17Z, had none yet. No
lease was past its `expires_at`, and no pool was full. The 2026-09-24
reading's five expired, never-heartbeated browser leases are gone.
`backend:CLOUD_RUN_JOB` read 0: in this reading the Cloud Run worker jobs ran
only warm self-tests (`--self-test` executions, read for #888 in the same
session), and every agent lease was on GKE.

## Pool ceilings: live against main's `dev.tfvars`

Still true, and still by design: `terraform/modules/firestore/bootstrap.tf`
puts `ignore_changes = [fields]` on the pool documents, because `active` is
mutated by every admission and an apply that rewrote the documents would zero
live concurrency counters. Terraform creates pools once; a running
environment's ceilings change through `PUT /v1/admin/limits/...`. So
`dev.tfvars` is what a NEW environment is born with, and the two may differ.
On 2026-09-24 seven pools did; now one does:

| pool | live `hard_limit` | `dev.tfvars` (`pool_limits`) |
|---|---:|---:|
| global | 180 | 180 |
| provider:anthropic | 200 | 200 |
| provider:openai | 100 | 100 |
| provider:anthropic:tenant:{eng,smoke,u-bogdan,u-or-9a0792} | 100 each | 100 (`provider_tenant`) |
| provider:openai:tenant:eng | 100 | 100 (`provider_tenant`) |
| runner:claude-code | 80 | 80 |
| runner:claude-code-browser | 100 | 100 |
| **runner:browser** | **40** | **20** |
| runner:mock / generic / codex | 40 / 20 / 20 | 40 / 20 / 20 |
| resource:standard / browser / large | 80 / 100 / 40 | 80 / 100 / 40 |
| backend:CLOUD_RUN_JOB / GKE_AUTOPILOT | 100 / 180 | 100 / 180 |
| tenant:eng / smoke | 100 / 20 | `max_active` 100 / 20 |
| tenant:u-sw-c90291 / u-or-9a0792 | 4 / 4 | `max_active` 4 / 4 (`capacity_units` 8 / 8) |
| tenant:u-bogdan / u-devops / u-qa-b260e6 | 100 / 30 / 4 | not in `tenants` |
| runner:await-input, claude-code-large, indexer (100); claude-code-review, merge, post-verdict (40); provider:git (100), git-review (40), mock-provider (100), mock-provider:tenant:eng (100) | as listed | not in `runner_profiles` / `providers` |

Only `runner:browser` differs from a value the file states. Nothing checks that
the two agree, and this table is not a claim that they should.

## Scheduler ticks

`gcloud scheduler jobs list --location us-central1`, read 06:01:08Z: 33 jobs,
all `ENABLED`, every last attempt `OK` (2026-09-24: three). They include:

* `swarm-scheduler-tick` (every minute), `swarm-reconciler-tick` (`*/1`, not
  the `*/5` of 2026-09-24) and `swarm-quota-refresh` (`*/5`);
* `swarm-schedule-tick`, `swarm-forge-refresh`, `swarm-workspace-sweep` and
  `swarm-mcp-credentials-sweep`;
* per-tenant `merge-wake`, `issue-run-advance`, `workflow-rollup`,
  `repo-index-poll` and `stranded-pr-sweep` jobs;
* `oracle-tick`, which is Product Oracle's.

**`swarm-issue-sweep-u-bogdan` is absent.** No job name contains
`issue-sweep`, and `gcloud scheduler jobs describe swarm-issue-sweep-u-bogdan`
answers `NOT_FOUND: Job not found`.

## Monitoring

* Monitoring REST `GET alertPolicies`: **21** policies (2026-09-24: six), all
  enabled, all `managed-by=swarm-terraform`.
* They include **`swarm-dev-safety-tick-stopped`**, so the safety-tick alert
  the 2026-09-24 reading said "is still not created" now exists. It watches the
  logs-based metric that reading's correction described.
* The other 20 are `control-plane-5xx`, `dispatch-failing-on-a-backend`,
  `generation-fencing`, `job-executions-failing`, `pr-stranded`,
  `reconciler-cannot-see-a-backend`, `schedule-needs-attention`,
  `scheduler-not-draining`, `spec-signature-invalid`, `tasks-dead-lettered`,
  `tenant-namespace-mismatch`, `wake-messages-dead-lettered`,
  `worker-action-ended`, `workspace-deployer-foreign-runtime`,
  `workspace-deployer-front-door-iam`,
  `workspace-deployer-outside-personal-workers`,
  `workspace-deployer-tenant-secret-iam`, `workspace-job-or-dispatch-changed`,
  `workspace-job-run-by-another-caller` and `workspace-stuck`.
* `GET metricDescriptors` filtered to `logging.googleapis.com/user/`: 21
  logs-based metrics. The scheduler one is
  `logging.googleapis.com/user/swarm/scheduler-tick-delivered`.

## In the project, not in terraform

* **`swarm-authprobe` is absent.** `gcloud run services describe
  swarm-authprobe` answers `Cannot find service [swarm-authprobe]`, and it is
  not in `services list`. The 2026-09-24 reading found it deployed, labelled
  `managed-by=scratch-delete-me`; who deleted it, and when, was not read.
* `swarm-job-u-bogdan-mock` is labelled `managed-by=swarm-scheduler` (not
  terraform) and still runs `3d4e0872752b` from 2026-10-22.
* `oracle-api`, `oracle-web` and `oracle-tick` are Product Oracle's.

## Not read

These are left out of the tables above:

* the cause of `git-tools`'s failure in accept run 40447384962 (its log was not
  opened);
* the jobs of release run 40449197675 and accept runs 40449572923 and
  40440511298;
* agent pods outside `swarm-system`;
* the full `leases` collection: a masked `GET leases` page-through was stopped
  at the operator's own 60-page cap (18,000 documents; the collection is
  larger) and discarded. The open-lease count above comes from the `runQuery`,
  which returns only unreleased leases;
* the IAP brand, and the IAM grants made to stand the front door up (see the
  previous reading).

## Outstanding, replacing the 2026-09-24 list

| 2026-09-24 said | 2026-10-27 |
|---|---|
| the release pipeline cannot plan (IAP IAM read denied), so nothing on main reaches the platform | **done**: the last five release runs on main succeeded and main's head is deployed |
| `build-images.sh`'s digest read-back matches tags by substring | not re-read here; every deployed digest maps to one commit tag plus `dev` / `promoted-dev-<commit>` |
| five expired, unreleased browser leases hold browser capacity full | **gone**: no open lease is past its `expires_at`; no pool is full |
| `swarm-authprobe` is a scratch service still deployed | **gone**: `Cannot find service` |
| the safety-tick alert is outstanding until a release applies it | **done**: `swarm-dev-safety-tick-stopped` exists |

New, from this reading:

* the `swarm-image-tag=5c99c79` label still lies on four services;
* `runner:browser` is 40 live against 20 in `dev.tfvars`;
* the newest acceptance run that ran its groups failed `git-tools`, cause not
  read;
* `swarm-job-u-bogdan-mock` (scheduler-managed) and `swarm-workspace-apply`
  run older images than the rest.

## Previous reading: 2026-09-24

Measured 2026-09-24 between 08:05 and 08:45 UTC, read-only; it replaced a
record dated 2026-09-19. Its four sentences, as it read then:

1. What was running was a laptop deploy of `7c52762`, not main: every
   control-plane service and every worker job ran image tag `7c5276212251`
   ("Merge dense lane", 2026-09-23 09:10 PDT), rolled out at 2026-09-23 19:46
   UTC by an operator, and swarm-api's revision named the operator's account as
   its creator. `7c52762` was a commit on
   `fix/silent-failures-env-parity-and-audits` from before that branch's later
   merges, so no fix merged to main since (including the
   [GKE dispatch incident](incidents/2026-09-24-gke-dispatch.md)'s) was
   running.
2. Main's release pipeline had never completed: 44 of 45 runs failed and one
   was cancelled. The latest, run 35972131246 on `b0fff1b`, built and promoted
   and then failed at `terraform plan`, denied the IAM policy of the two IAP
   backend services `swarm-ui-backend` and `swarm-ui-ui-backend`
   (`IAM_PERMISSION_DENIED`), even after `e233537` had granted the deployer
   `roles/iap.admin` conditioned on `resource.name` starting with
   `.../iap_web/compute/services/swarm` (the project's other nine IAP backends
   are another team's). Whether IAP ignored the condition or checked a
   different name was not established.
3. The front door worked: the DNS record the 2026-09-19 record waited on
   resolved, the certificate was `ACTIVE`, and IAP redirected to Google
   sign-in.
4. Browser capacity was full and nothing was using it: five `browser` leases
   (`units: 2` each, `DISPATCHED` on GKE, never heartbeated) had expired at
   about 04:00 UTC and were never released, holding `runner:browser` and
   `resource:browser` at 10 of 10. Why the reconciler had not reclaimed them was
   not diagnosed.

It also found that `scripts/build-images.sh` read each digest back with a
`tag:X` filter, which gcloud matches by substring, so the release promoted the
`application` workflow's `pr-<run>-<commit>` build instead of its own (same
commit; reported with PR #25); that the cluster ran control plane
`1.36.4-gke.1082000`; that six alert policies existed and the safety-tick alert
could not, because Google publishes no Cloud Scheduler metric (corrected that
day to a logs-based metric); and that `swarm-authprobe`, a scratch Cloud Run
service created 2026-09-20 from `cloud-run-source-deploy/swarm-authprobe`,
labelled `managed-by=scratch-delete-me`, ingress `all`, invokable only by the
`swarm-verify` service account, was still deployed.

Carried from 2026-09-19 and not re-checked on 2026-09-24 or since: the IAP
brand `projects/209012342332/brands/209012342332` ("AI Agents",
`orgInternalOnly: true`) is shared and not ours, and terraform neither creates
nor modifies it (`google_iap_brand` cannot be deleted); IAP IAM took about 45
seconds to propagate when the front door was stood up, and a retry inside that
window failed with the identical 403; and `/readyz` answers 404 from a
workstation for the `*.run.app` hostnames, because ingress is
`internal-and-cloud-load-balancing`.

## How this was measured

All read-only. `$B` is
`https://firestore.googleapis.com/v1/projects/saga-agents-staging/databases/swarm/documents`.
The bearer token came from `gcloud auth print-access-token` inside the curl
header and was never printed. Outputs were reduced with `jq` to the fields
shown above.

```bash
git fetch -q origin main; git log -1 origin/main; date -u
gcloud run services list --region us-central1 --project saga-agents-staging --format=json      # 05:58:26Z and 06:02:21Z
gcloud run revisions describe <revision> --region us-central1 --project saga-agents-staging --format=json   # creationTimestamp, creator, Ready
gcloud run services describe swarm-authprobe --region us-central1 --project saga-agents-staging
gcloud run jobs list --region us-central1 --project saga-agents-staging --format=json   # image, latestCreatedExecution, managed-by
gcloud artifacts docker images list us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/<image> --include-tags --filter="version=sha256:<digest>" --format='value(tags)'
gcloud container clusters describe swarm-autopilot --region us-central1 --project saga-agents-staging --format=json
/opt/homebrew/bin/kubectl --context gke_saga-agents-staging_us-central1_swarm-autopilot get deploy,statefulset,daemonset -n swarm-system -o json   # images, ready only
/opt/homebrew/bin/kubectl --context gke_saga-agents-staging_us-central1_swarm-autopilot get pods -n swarm-system -o json
gh run list --workflow release.yml --branch main --limit 5 --json ...;  gh run view <id> --json jobs
gh run list --workflow accept.yml --branch main --limit 5 --json ...;   gh run view <id> --json jobs
gcloud scheduler jobs list --location us-central1 --project saga-agents-staging --format=json   # name, schedule, state, lastAttemptTime, status only
gcloud scheduler jobs describe swarm-issue-sweep-u-bogdan --location us-central1 --project saga-agents-staging
curl -H "$AUTH_HEADER" -H "x-goog-user-project: saga-agents-staging" "$B/pools?pageSize=300"   # AUTH_HEADER: the gcloud access token, never printed
curl -X POST ... "${B}:runQuery" -d '{"structuredQuery":{"from":[{"collectionId":"leases"}],"where":{"unaryFilter":{"op":"IS_NULL","field":{"fieldPath":"released_at"}}},"select":{...}}}'
curl ... "https://monitoring.googleapis.com/v3/projects/saga-agents-staging/alertPolicies?pageSize=200"
curl -G ... "https://monitoring.googleapis.com/v3/projects/saga-agents-staging/metricDescriptors" --data-urlencode 'filter=metric.type = starts_with("logging.googleapis.com/user/")'
dig +short swarm.saga.xyz A
gcloud compute addresses list --global --filter='name~^swarm';  gcloud compute ssl-certificates list --filter='name~^swarm'
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' http://swarm.saga.xyz/ | https://swarm.saga.xyz/ | https://swarm.saga.xyz/readyz
git show origin/main:terraform/environments/dev/dev.tfvars   # pool_limits and tenants blocks
```

No `gcloud ... update`, `create`, `delete` or `apply`, no `kubectl` write and
no Firestore write was issued.
