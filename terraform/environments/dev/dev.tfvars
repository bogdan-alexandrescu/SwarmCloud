# Development environment.
#
#   terraform -chdir=terraform/infra init \
#     -backend-config="bucket=swarm-tfstate-saga-agents-staging" \
#     -backend-config="prefix=infra/dev"
#   terraform -chdir=terraform/infra plan -var-file=../environments/dev/dev.tfvars

project_id  = "saga-agents-staging"
region      = "us-central1"
environment = "dev"

# Dev may be torn down and rebuilt. prod may not -- see prod.tfvars.
deletion_protection  = false
bucket_force_destroy = false

extra_labels = {
  "swarm-owner" = "platform"
}

# --- network ---------------------------------------------------------------
# Deliberately disjoint from anything agents-staging-vpc might use.
subnet_cidr         = "10.40.0.0/20"
pods_cidr           = "10.44.0.0/14"
services_cidr       = "10.48.0.0/20"
gke_master_cidr     = "172.16.32.0/28"
nat_static_ip_count = 1
restrict_egress     = false

# --- GKE Autopilot (browser / GPU / >32 GiB only) --------------------------
enable_gke_autopilot        = true
gke_release_channel         = "RAPID"
gke_enable_private_endpoint = false

# gke_master_authorized_cidrs USED TO BE deliberately empty here and passed at
# apply time as the operator's own /32. That is no longer what this does.
#
# The old note gave two reasons not to commit a value: this repository is
# PUBLIC, so an operator's home address in git is a personal detail published
# permanently; and the value rots the moment anyone changes network. The first
# reason does not apply to 0.0.0.0/0 -- it discloses nothing about anyone. The
# second is exactly what went wrong: the cluster sat holding a stale operator
# /32 for an address that person no longer had, so the allowlist was denying the
# one person it existed to admit while protecting nothing.
#
# Opened on 2026-09-18 by operator decision. This is a NETWORK control only: the
# GKE API server still requires a Google identity and still enforces RBAC, so
# this makes the control plane reachable and discoverable from the internet, not
# unauthenticated. The full argument, including the private-endpoint-plus-IAP
# alternative that keeps both properties, is next to the removed validation in
# terraform/modules/gke_autopilot/variables.tf.
#
# PROD MUST NOT COPY THIS. A prod cluster keeps gke_enable_private_endpoint =
# true and reaches the control plane from inside the VPC.
gke_allow_open_master_authorized_network = true
gke_master_authorized_cidrs = [
  {
    cidr_block   = "0.0.0.0/0"
    display_name = "open-dev-cluster"
  }
]

# --- data ------------------------------------------------------------------
firestore_database      = "swarm"
artifact_retention_days = 14

# The disposable database scripts/bench-contention.sh runs the real admission
# against (S32). Dev only: it holds nothing but bench documents, and the
# harness refuses every other name. Prod leaves it unset and gets none.
firestore_bench_database = "swarm-bench"

# --- images ----------------------------------------------------------------
# No image is named here. Every image is deployed by digest through
# `image_refs`, which scripts/lib/image-refs.sh writes from the promotion
# manifest at plan and deploy time; a tfvars value would be a digest nobody
# updates.
immutable_image_tags = false

# --- capacity --------------------------------------------------------------
# DOUBLED by owner decision 2026-10-02 ("double the capacity settings across all
# tenants and across all runtimes"), to raise parallel throughput. The live
# values were doubled at ~05:20Z through scripts/pool-limit.sh (pools) and
# PUT /v1/admin/tenants/{id}/limits (tenants); the numbers below are those live
# values, so `scripts/pool-limit.sh --check` reports no drift and a new
# environment is born with them. tests/unit/scripts/test_dev_tfvars_capacity.py
# holds every derived pool and tenant limit to them.
#
# A ceiling above what a provider can really serve is not a cost: admission
# simply holds the excess QUEUED or PARKED, which hold no capacity and create no
# infrastructure demand (invariant 1), and AIMD still backs off below the
# ceiling on the first 429. What a runaway loop can spend is still bounded by
# `global` and each tenant's own pool.
#
# CHANGING THESE DOES NOT CHANGE A RUNNING ENVIRONMENT. Verified on
# 2026-09-19: raising every number here and applying moved the terraform OUTPUT
# and nothing else -- the live pools stayed at 20/10/5.
#
# terraform/modules/firestore/bootstrap.tf carries `ignore_changes = [fields]`
# on the pool documents, deliberately and for a good reason: `active` is mutated
# by the admission transaction on every lease, so an apply that rewrote these
# documents would reset live concurrency counters to zero and instantly
# oversubscribe every pool. Terraform creates them once and then stops having an
# opinion.
#
# So these values are the ceilings a NEW environment is born with. To change a
# running one, use the admin API (PUT /v1/admin/limits/...), which is the only
# path that writes hard_limit without touching `active`. Keep the two in step by
# hand -- nothing checks that they agree, which is worth knowing before reading
# the numbers below as a description of production.
#
# These are CEILINGS an operator sets, not targets. AIMD explores BELOW them:
# `SlotPool.effective_limit` is min(hard_limit, adaptive_target,
# quota_derived_limit), so adaptive logic may only ever lower the number, never
# raise it. A ceiling set too low is therefore invisible -- the platform simply
# never discovers it could do more, and reports PROVIDER_CONCURRENCY_LIMIT as
# though the provider had refused.
#
# Measured 2026-09-18: a 12-agent fan-out admitted 5 at a time and held the rest
# on provider_tenant, with three healthy accounts and no provider anywhere near
# its own limits. All twelve finished -- QUEUED costs nothing (invariant 1), so
# the held work drained in later waves rather than being refused. The ceiling
# cost WALL-CLOCK, not work: one fan-out ran as three serial waves.
#
# Worth stating plainly, because the earlier reading of this was that seven
# tasks were denied, and that would be a different and more urgent problem than
# the one actually measured.
#
# provider_tenant is sized for the ACCOUNT POOL rather than for one
# subscription. It was 15 (three accounts at roughly five concurrent agents
# each) and is 40 since the 2026-10-02 doubling, which matches every live
# provider:<p>:tenant:<t> pool terraform creates. AIMD still backs off
# multiplicatively on the first 429, so this is room to search in, not a
# promise that forty will work.
#
# Not expressible here: the live `provider:mock-provider` (100) and
# `provider:mock-provider:tenant:eng` (100). mock-provider is what the mock
# runner reports at runtime and no catalogue profile names it, so terraform
# creates neither pool and `--check` does not compare them.
pool_limits = {
  global          = 100
  default_tenant  = 40
  provider_tenant = 40

  resource_classes = {
    standard = 80
    browser  = 20
    large    = 40
  }

  runner_profiles = {
    mock        = 40
    generic     = 20
    claude-code = 80
    codex       = 20
    browser     = 20
  }

  # GKE_AUTOPILOT 40 -> 100: owner, 2026-10-07, accepting contract request 53
  # (claude-code moves to GKE Autopilot). At 40 the backend pool would bind
  # before claude-code's own ceiling (runner:claude-code 80), shared with
  # browser at 2 units a task, and halve claude-code's concurrency on the
  # move. 100 is claude-code's 80 plus browser headroom. It fits the quota:
  # us-central1 regional CPUS read 2,990 of 3,000 free on 2026-10-07, and
  # 100 x 4 vCPU = 400.
  #
  # NO COMPUTE CLASS MAY PIN T2D. T2D_CPUS quota in us-central1 is 128 vCPUs,
  # 32 standard pods; the pods carry no nodeSelector, so Autopilot's default
  # class picks from every family, and a T2D pin (or a ComputeClass listing
  # T2D alone) would cap this pool at 32 whatever it says here -- and every
  # probe pinned to one family hit "never scheduled" or "GCE out of
  # resources" (request 53, "What the probe saw go wrong").
  backends = {
    CLOUD_RUN_JOB = 100
    GKE_AUTOPILOT = 100
  }

  providers = {
    # Above provider_tenant x tenants, or the shared pool binds before the
    # per-tenant one and the per-tenant ceiling stops meaning anything. The
    # rule at variables.tf:337 enforces this; it is not a guideline.
    #
    #   anthropic  eng + smoke             = 2 x 40 = 80    (set: 120)
    #   openai     eng                     = 1 x 40 = 40    (set: 40)
    #
    # anthropic stays at 120 after u-bogdan left this map (W9, 2026-10-09),
    # above the floor of 80, because u-bogdan still runs on the shared
    # anthropic pool -- as a workspace now -- and W9 is a move that must
    # change nothing live. Lowering it is a separate decision.
    #
    # anthropic was 100 until smoke declared it for release acceptance
    # (#628): 100 is below the new floor and the plan would refuse it. The
    # floor is what forces 120, not a measurement; smoke's own pool is
    # min(max_active, capacity_units) = 20, so acceptance can add at most 20.
    anthropic = 120

    # Exactly the floor provider_tenant = 40 implies, and the live value since
    # the 2026-10-02 doubling. It is NOT a measurement: the account-pool reasoning
    # above is about three Anthropic subscriptions, and there is no equivalent
    # OpenAI pool behind this number. provider_tenant applies to every provider
    # uniformly, so lifting it for one lifts the floor for all of them.
    #
    # If that uniformity turns out to be wrong, the fix is a per-provider
    # tenant ceiling, not a smaller number here -- a number below this floor
    # does not fail at runtime, it fails the plan.
    openai = 40
  }
}

service_max_instances = {
  "swarm-api"          = 5
  "swarm-scheduler"    = 2
  "swarm-quota-broker" = 2
  "swarm-reconciler"   = 1
  # Static files behind a CDN-less load balancer; one instance serves the whole
  # team and scales to zero between visits.
  "swarm-ui" = 2
}

settings_env = {
  PERFORMANCE_PROFILE = "economy"
}

# --- tenancy ---------------------------------------------------------------
allowed_domains = ["saga.xyz"]

tenants = {
  # A Google group. swarm_common.identity turns eng@saga.xyz into `eng`.
  eng = {
    kind      = "group"
    principal = "eng@saga.xyz"
    # TEMPORARILY OFF. The group EXISTS -- groups/01gf8i8328uclsx -- and this
    # is not the phantom-group problem that `smoke` had. swarm-api simply
    # cannot READ it.
    #
    # Cloud Identity's Groups API does not use GCP IAM at all. It authorizes
    # through Admin, Non-admin or Namespace modes, none of which a
    # *.gserviceaccount.com identity satisfies: the service account is not a
    # principal in saga.xyz, so every mode falls through to
    # "Error(2028): Permission denied for resource eng@saga.xyz (or it may not
    # exist)". Verified 2026-09-20: roles/cloudidentity.groupsReader is an
    # ALPHA role with NO includedPermissions, and zero cloudidentity
    # permissions are testable at the organization -- granting it changed
    # nothing, as it cannot.
    #
    # The documented fix is a WORKSPACE change, not a GCP one: a Group Reader
    # admin role assigned to the service account through the Admin SDK, or
    # domain-wide delegation. Both were blocked today -- the Admin SDK needs
    # an OAuth scope Google refuses to issue to the gcloud client in this
    # domain.
    #
    # Until one of those lands, a failed lookup is fatal by design, so leaving
    # this true makes EVERY authenticated request 503 for EVERY user. Turning
    # it off costs the group-to-tenant mapping: callers resolve to their
    # personal `u-<user>` tenant instead of `eng`. Set it back to true in the
    # same commit as the Workspace change.
    # BACK ON as of 2026-09-20: domain-wide delegation was authorised in the
    # Admin console for swarm-api's OAuth client id, scoped to
    # cloud-identity.groups.readonly, and swarm_api.groups now acts as
    # groups_impersonate_user when it reads membership. Before that the
    # service account could not read this group at all -- not for want of an
    # IAM role, but because the Groups API does not use GCP IAM and a service
    # account is not a Workspace principal.
    #
    # If this starts 503ing every authenticated request again, the delegation
    # grant is the first thing to check: a failed lookup is fatal by design,
    # because a higher-priority group being unknown could file a caller's work
    # under the wrong tenant.
    directory_group = true
    display_name    = "Engineering"
    providers       = ["anthropic", "openai"]
    # Live values, set via the admin API on 2026-10-07 (owner): max_active was
    # already 45 but capacity_units 40 capped the pool at 40. Terraform creates
    # the tenant document once and ignores later changes, so these record the
    # live state rather than enforce it.
    max_active     = 45
    capacity_units = 45
    # The CI fixer (.github/workflows/ci-fix.yml) acts for this tenant, which
    # owns the swarm pull requests it fixes. Listed here, NOT added to
    # eng@saga.xyz: that group holds project-wide admin roles on this shared
    # project, and a listed service account resolves by exact email + unique
    # id instead (contract request 30). Owner decision, 2026-10-05.
    service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
  }

  # The mock runner needs no provider key, so this tenant can smoke-test the
  # whole path before anybody registers a credential.
  #
  # RELEASE ACCEPTANCE RUNS HERE (owner decision 2026-10-05, #628). The suite
  # (scripts/acceptance/config.sh) sends `X-Swarm-Tenant: smoke` as
  # swarm-verify, against the private sandbox repository. Until then it ran in
  # eng, as swarm-verify's default tenant, and was 62% of eng's tasks.
  #
  # The header only SELECTS among the caller's confirmed memberships of
  # registered directory groups (swarm_api.auth `_select_tenant`), so:
  #
  #   * the principal is smoke@saga.xyz, which swarm_common.identity slugs to
  #     `smoke`. It was swarm-smoke@saga.xyz, which slugs to `swarm-smoke`:
  #     no caller could ever have reached this tenant through the API.
  #   * directory_group is true, so the group is in TENANT_GROUPS. It sorts
  #     after `eng`, so swarm-verify's DEFAULT tenant stays eng and smoke-test,
  #     e2e and race-test are unchanged; only acceptance selects smoke.
  #   * NOT a service_accounts listing of swarm-verify: a listed account is
  #     continuation-scoped (CONTINUATION_ROUTES) and could submit no ordinary
  #     task, which would stop every suite it runs.
  #
  # THE GROUP MUST EXIST, WITH swarm-verify IN IT, BEFORE THIS IS APPLIED.
  # swarm-smoke@ never existed (verified 2026-09-19: "There is no such a
  # group"), and a registered group whose lookup fails 503s every caller who
  # is in no group above it -- by design, because an unanswered lookup could
  # file work under the wrong tenant. Before 2026-10-05 nothing signed in as
  # this tenant (the smoke suite dispatched through Firestore in-process), so
  # the group was never needed; acceptance needs it. Owner steps, in order:
  # docs/ci.md, "Release acceptance runs in the smoke tenant".
  #
  # anthropic, because terraform makes a tenant's claude-code Cloud Run Job
  # only for a declared provider, and the claude-code and integrate checks --
  # the ones that open the sandbox's pull requests -- run on it. The key is
  # not here: scripts/create-secrets.sh --tenant smoke --provider anthropic,
  # or an account lent to smoke.
  smoke = {
    kind            = "group"
    principal       = "smoke@saga.xyz"
    directory_group = true
    display_name    = "Smoke tests"
    providers       = ["anthropic"]
    # The live values, set by the owner through the admin API on 2026-10-07
    # (~23:4xZ): smoke's ceiling is 20. It was 8 -- capacity_units 8 held the
    # pool, min(max_active, capacity_units), below max_active 20 -- and a
    # console raise to 20 left it there until the tenant ceiling route began
    # setting both fields (the ceiling is one number, owner decision same day).
    #
    # These RECORD the live state; they do not enforce it. Terraform creates a
    # tenant document once and ignores later changes to it
    # (terraform/modules/firestore/bootstrap.tf, `ignore_changes = [fields]`),
    # so an apply neither sets 20 nor reverts it. What they decide is the
    # ceiling a rebuilt environment is born with.
    max_active     = 20
    capacity_units = 20
  }

  # The in-VPC verification job's own tenant.
  #
  # THE KEY IS COMPUTED, NOT CHOSEN. swarm_common.identity._slug appends a
  # digest of the full principal whenever the slug is lossy or too long for a
  # service account name, and
  # `swarm-verify@saga-agents-staging.iam.gserviceaccount.com` is both. So the
  # id is `u-sw-c90291` and not the `u-swarm-verify` that reads naturally --
  # verified by calling tenant_id_for_user, not by reading the regex. If the
  # service account or the project is ever renamed, this key changes with it
  # and must be recomputed the same way.
  #
  # It needs to exist at all because scripts/smoke-test.sh asks
  # GET /v1/tenants/me and then uses whatever tenant comes back: without a
  # tenant document there are no per-tenant Cloud Run Jobs, and an admitted
  # task holds a lease with nowhere to run.
  #
  # No providers, deliberately. The suites use the `mock` runner profile, which
  # needs no provider key, so this identity never touches a subscription
  # credential -- which is why swarm-verify holds no secret access.
  u-sw-c90291 = {
    kind            = "user"
    principal       = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    display_name    = "Verification gate"
    directory_group = false
    providers       = []
    max_active      = 4
    capacity_units  = 8
  }

  # u-bogdan IS NO LONGER HERE (lane W9 of #847, 2026-10-09). It was the
  # owner's personal fallback tenant -- `u-<local part>`, which a caller in no
  # registered group resolves to -- declared here so a laptop smoke run had
  # jobs to dispatch to. Owner decision WD3, 2026-10-08: a person's workspace is
  # made by the workspace job (scripts/register-tenant.sh --workspace) and
  # lives outside Terraform state, and terraform/infra/variables.tf refuses a
  # person in this map. Its account, secret, documents, jobs and grants are
  # FORGOTTEN, not destroyed, by terraform/infra/removed.tf and
  # terraform/bootstrap/removed.tf, and belong to the record
  # `workspaces/u-bogdan` (`migrated = true`; docs/workspaces.md §3.3). Do not
  # add it back: a person here would be planned again, in a public file.
}

# allUsers lets the caller's ID token REACH the app, which is the only component
# that can identify a tenant. Cloud Run's edge IAM would consume that token (see
# terraform/infra/variables.tf). The service is not internet-reachable: ingress
# stays internal-and-cloud-load-balancing, and the app still requires a valid
# Google ID token from saga.xyz.
api_invokers = [
  "allUsers",
]

# A dedicated platform-admin group, NOT a tenant group. This list is applied to
# every tenant that names no `secret_admins` of its own, so putting a tenant's
# own group here would let any member of that tenant replace another tenant's
# provider key with one pointing at infrastructure they control.
# terraform/infra/variables.tf refuses that shape.
# NOTE: group:swarm-admins@saga.xyz does not exist in this Workspace, and
# creating it needs Workspace-admin rights. For dev the platform admin is a
# named user, which satisfies the same constraint: it is not a tenant group, so
# no tenant member can replace another tenant's provider key. Create the group
# and switch this back before prod, where one named human is a single point of
# failure.
# Must NOT be any tenant's own principal: this list administers EVERY tenant's
# secrets, so naming a tenant member there would let that tenant replace another
# tenant's provider key with one pointing at infrastructure they control.
# terraform/infra/variables.tf enforces that, and caught exactly this when
# bogdan@saga.xyz was both the secret admin and the principal of the u-bogdan
# fallback tenant. admin@ is a platform account that owns no tenant.
secret_admin_members = [
  "user:admin@saga.xyz",
]

# --- step-spec signing (contract request 34, #342) --------------------------
# LEGACY FIRST (owner decision 2026-09-29 on #353): unsigned tasks created
# before the cutover are admitted and logged -- a WARNING and a RUNNING event
# with phase verify_spec -- because every task parked when the verifying
# worker ships is unsigned. Inert until #353 is released: today's worker and
# swarm-api read none of these.
#
# THE CUTOVER PLAN (docs/runbooks/spec-signing-rollout.md):
#   1. done: legacy, with the cutover at the end of the legacy window,
#      2026-10-20T00:00:00Z, so the release that started signing (#353) could
#      not fail a single parked task.
#   2. done: #353 released.
#   3. THIS VALUE: the creation time of swarm-api-00119-lcs, the first
#      revision whose environment carries SPEC_SIGNING_KEY_VERSION, made by
#      #353's own release (5cf0c9b, terraform apply 21:23:55-21:25:34Z) and
#      serving 100% of traffic as latest from then on. Read 2026-10-01 with
#      `gcloud run revisions list --service swarm-api --format=json` (the
#      SPEC_ env names per revision) and its metadata.creationTimestamp,
#      2026-09-30T21:24:36.572669Z, truncated to the second (stricter by under
#      a second). A task Firestore created after this moment with no signature
#      is refused, not admitted. Every
#      unsigned non-terminal task read on 2026-10-01 was created on 2026-09-25,
#      long before it, so this change refuses none of them.
#   4. once no non-terminal unsigned task is left, a PR sets
#      spec_signature_mode = "enforce" -- before 2026-10-20, after which the
#      worker enforces regardless.
# Step 4 (owner decision 2026-10-01): dev admits signed specs only. The 14
# unsigned smoke tasks parked since 2026-09-25 in u-sw-c90291 -- swarm-verify's
# old personal tenant, which no principal resolves to since swarm-verify joined
# the eng group -- cannot be cancelled through the API and are left PARKED;
# under enforce, any dispatch of one is refused as spec_signature_invalid.
spec_signature_mode = "enforce"
# Version 1 is the one Cloud KMS creates with the key (terraform/bootstrap).
spec_signing_key_version = 1

# --- observability ---------------------------------------------------------
create_alerts = true
alert_emails  = []

# --- the web UI front door --------------------------------------------------
# An external ALB with IAP in front of swarm-api. Nothing about the existing
# services changes: their ingress setting is already exactly what an external
# load balancer requires, and it was the absence of the load balancer -- not the
# ingress setting -- that made swarm-api unreachable from a browser.
enable_frontend   = true
frontend_hostname = "swarm.saga.xyz"

# --- the SwarmCloud GitHub App (#780, docs/runbooks/github-app.md) -----------
# enable_github_app declares the App's two empty secret slots, which swarm-api
# alone reads (github_app.tf). The three settings below are the App's PUBLIC
# ones, copied from its settings page after the owner registers it (runbook
# step 3): registered 2026-10-07 on bogdan-alexandrescu as "SwarmCloud Saga"
# ("SwarmCloud" is reserved for the @swarmcloud account), installable on any
# account. The client SECRET and the private key are
# never written here -- the repository is public -- they go to Secret Manager
# by `scripts/create-secrets.sh --github-app <slot> --stdin` (runbook step 4).
enable_github_app    = true
github_app_id        = "5229127"
github_app_client_id = "Iv23lipzgYrbQvuJdmZg"
github_app_slug      = "swarmcloud-saga"

# The 15-minute user-token refresh sweep, swarm-forge-refresh. ON since
# 2026-10-07: swarm-api serves POST /v1/admin/forge/refresh from OB3's release
# (e983d06e, run 37672097489; revision swarm-api-00176). Runbook step 7.
enable_forge_refresh = true

# D4's refusal is ON in dev (#780 OB10, the migration step). A PERSON's task on
# a GitHub repository they have not chosen under Access is refused with 403
# REPOSITORY_NOT_GRANTED ("choose it under Access") instead of running with the
# tenant token; a person with a grant runs as themselves, through their App
# slot or their fallback token for that owner. A service submission (repository
# indexing, schedules, the release's acceptance suite in the smoke tenant) is
# never refused: it keeps the tenant token and the task says so ("tenant token,
# service submission"). On here because dev is where people connect: the App
# above is registered and the refresh sweep runs. BEFORE THIS APPLIES, every
# person who submits in dev grants the repositories they work in (Work ›
# Access, or `uv run sc access grant owner/repo --write`), or their tasks are
# refused. docs/runbooks/onboarding-acceptance.md is the check that it works.
repository_grants_enforced = true

# WHO MAY PASS IAP is no longer set here. It moved to terraform/bootstrap
# (frontend_iap_members) on 2026-09-24, because managing it from this root made
# CI's deployer need IAP admin rights that could not be scoped to our backends.
# See terraform/modules/frontend/main.tf, "Who may pass IAP".

# The IAP OAuth brand is NOT created by terraform: google_iap_brand cannot be
# deleted, so terraform could create one and never remove it. It is a
# once-per-project manual step, documented in terraform/modules/frontend/main.tf.

# ON again, 2026-09-24. It was off because the metric it watched,
# cloudscheduler.googleapis.com/job/attempt_count, did not exist here -- and it
# never would have: Google publishes no Cloud Scheduler metric, so "wait for it
# to appear" had no end. The alert now watches a logs-based metric the
# monitoring module creates from Cloud Scheduler's own attempt logs, counting
# the tick's delivered attempts (read live that day: one per minute, every
# minute). The metric and the policy are created in the same apply, the metric
# first. See terraform/modules/monitoring/metrics.tf.
enable_safety_tick_alert = true

# The audiences IAP mints for this deployment's two backend services. Read from
# `terraform output frontend_iap_audiences` after the load balancer was created;
# see the variable's description for why this is declared rather than derived
# (deriving it is a terraform cycle).
#
# Without these, swarm-api has nothing to verify an IAP assertion against and
# answers 401 to every request from the web UI -- a signed-in user, a valid
# certificate, a healthy load balancer, and an API that cannot see any of it.
frontend_iap_audiences = [
  "/projects/209012342332/global/backendServices/817602226733443034",
  "/projects/209012342332/global/backendServices/6904312892305383900",
]

# The quota broker's URL, for the SCHEDULER's environment only -- it passes it
# through to every worker it dispatches, and a worker with no QUOTA_BROKER_URL
# uses no account pool at all. Read from `terraform output quota_broker_url`,
# and declared here for the same reason frontend_iap_audiences is: the
# scheduler's environment is an input to the Cloud Run module and this URL is
# an output of it, so referencing it is a cycle. Cloud Run JOBS do not need
# this -- locals.tf derives the same value for them.
#
# Unset, the pool is INERT and nothing says so at runtime: every agent runs on
# the one shared per-tenant subscription while the pool's own listing shows a
# healthy, idle set of accounts. The `quota_broker_url_is_wired` check is what
# makes that visible at plan time; this is what makes it unnecessary.
quota_broker_url = "https://swarm-quota-broker-tonstldhta-uc.a.run.app"


# Admin by email, because admin-by-group is unreachable here: swarm-api cannot
# read Cloud Identity groups, so the admin set is empty and every operator
# screen 403s for everyone. See the variable's description and
# docs/audits/2026-09-20/session-handover.md. Empty this the moment the
# Workspace Group Reader role lands.
#
# Every entry here is a FULL platform admin: it can pause dispatch, set any
# ceiling, drain or disable a provider for every tenant, disable any tenant
# (PUT /v1/admin/tenants/{id}/limits) and rewrite any tenant's workflow state.
# swarm-verify was added here on 2026-09-24 for race-test and the owner
# reversed that the same day for exactly that reach -- it is on
# admin_pool_users below instead. Do not put it back.
admin_users = ["bogdan@saga.xyz"]

# The protected owner (docs/workspaces.md §6.5, owner 2026-10-08): no other
# admin can remove this person's admin rights.
platform_owner = "bogdan@saga.xyz"

# The verification gate's ONE admin route, by owner decision on 2026-09-24.
#
# scripts/race-test.sh narrows runner:mock to one slot to force contention, and
# restores it, through PUT /v1/admin/limits/runner/mock. The alternative was a
# Firestore write role, which cannot be scoped below the database and would
# have left `pools/*.active` -- the counter a wider field mask once clobbered on
# this deployment -- one typo away. The route cannot write `active`, refuses a
# pool name outside the frozen catalogue, bounds the value, and records the
# verified caller on the pool (admin_changed_by).
#
# swarm-api lets this list call an ALLOW-LIST of admin routes
# (swarm_api.auth.POOL_ADMIN_ROUTES), which holds that one route. Every other
# /v1/admin route answers it 403 -- tenant limits, provider switches, drains,
# dispatch pause, the workflow rollup and every admin read -- and so does any
# admin route added later until it is deliberately allow-listed. It does not
# set is_admin, so no operator screen treats the gate as an admin. What it can
# still do: set the ceiling of ANY runner profile's pool, because the route
# takes the profile as a parameter. race-test itself refuses every profile but
# mock. Granted in dev only; there is no reason for the gate to hold it in
# prod.
#
# BARE EMAIL, not `serviceAccount:<email>`: swarm-api compares this list against
# the email in the verified token, so a prefixed entry would plan, apply, and
# match nobody (variables.tf now refuses that shape). The key it must agree
# with is ALLOWED_USERS, which locals.tf derives from
# google_service_account.verify.email.
admin_pool_users = [
  "swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
]

# Domain-wide delegation was authorised in the Admin console on 2026-09-20 for
# this service account's OAuth client id, scoped to
# cloud-identity.groups.readonly. swarm-api acts as this user when it reads
# group membership, because a service account is not a Workspace principal and
# the Groups API does not authorize through GCP IAM at all.
groups_impersonate_user = "bogdan@saga.xyz"

# The release's CI identity (terraform/bootstrap). It is granted actAs on each of
# our service accounts it deploys as -- terraform/infra/deployer.tf. The same
# email is the GitHub repository variable GCP_DEPLOY_SA; both are written by the
# bootstrap output github_deployer_service_account.
deployer_service_account = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"

# The in-platform issue sweeper (swarm-api SWEEP_ENABLED), on in dev from
# 2026-10-09. Owner decision 2026-10-08: switch it on once PR 898 is deployed
# (it was, 2026-10-09 02:28Z). This is the platform switch only; the tenant's
# own switch, its cap of 8 live runs and submit_as are set per tenant through
# PUT /v1/admin/tenants/eng/issue-sweep, not here (docs/issue-runs.md "Turning
# it on, for tenant eng"). Left off in prod.
enable_issue_sweep = true

# Personal-workspace publishing (swarm-api WORKSPACE_APPLY_PUBLISH, #847).
# ON in dev from 2026-10-11 (owner): the swarm-workspace-apply topic exists, and
# an approval now reaches the Cloud Run job swarm-workspace-apply through
# Eventarc and Workflows (WD2 re-decided 2026-10-10; W4b bootstrap applied
# 2026-10-11, 33 resources; W5 cluster policies applied the same day). While it
# was off every approved request waited, logged as workspace_stuck
# (publishing_off). If the job path breaks, turning this off again stops new
# dispatches; the sweep and the stuck alert keep reporting waiting records.
workspace_apply_publish = true
