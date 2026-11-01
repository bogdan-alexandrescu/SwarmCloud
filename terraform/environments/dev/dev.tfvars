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
subnet_cidr     = "10.40.0.0/20"
pods_cidr       = "10.44.0.0/14"
services_cidr   = "10.48.0.0/20"
gke_master_cidr = "172.16.32.0/28"
# 1 -> 2: BGD, 2026-10-26, before the raised pools (BROWSER-POOL-100: 180 GKE
# pods platform-wide, a tenant up to 100) fill the one address's 64,512 ports;
# 655 packets were already dropped for lack of a port in the 7 days to
# 2026-10-21 (#1471). EVERY EGRESS ALLOW-LIST MUST ADD THE NEW ADDRESS: it is
# served as MCP_EGRESS_ADDRESSES and is what model providers allow-list
# (modules/network nat_addresses).
nat_static_ip_count = 2
# 128 -> 512 and 8192 -> 16384: #1334 c24. swarm-nat logged 320 drops in 10
# days, 216 of them in the 3 days to 2026-10-16, from 6 GKE nodes and none
# from Cloud Run. They came in bursts to DigitalOcean and Vultr hosts on port
# 44445 and to 151.101.0.223:443 (Fastly). Every drop is OUT_OF_RESOURCES.
# ENDPOINT_INDEPENDENCE_CONFLICT needs endpoint-independent mapping, which is
# off (modules/network main.tf). Port 44445 is the browser-egress vendor:
# brd.superproxy.io resolves to those same DigitalOcean and Vultr addresses
# (read 2026-10-27). swarm-egress has one replica, so one node, and it opens a
# new upstream connection for every browser CONNECT. It is the platform's
# forwarder, not an agent reaching unexpected hosts.
#
# A burst on that node starts at 128 ports per vendor address and has to wait
# for allocation to double. 512 is two doublings closer, and 16384 lifts the
# cap that busy node can reach. The floor is reserved on every VM: 2 addresses
# x 64,512 ports / 512 = 252 VMs before a new one gets no port at all. That
# covers today's GKE nodes plus Cloud Run, which runs only the mock and
# fallback Jobs. Check it again before nat_static_ip_count is lowered or
# Cloud Run takes real pools.
nat_min_ports_per_vm = 512
nat_max_ports_per_vm = 16384
restrict_egress      = false

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

# swarm-images cleanup is LIVE (owner 2026-10-22, #1886: "Go live via a PR").
# It ran as a dry run from 2026-10-21, but its results were never logged:
# Artifact Registry's Data Access audit logs are off in this project, so the
# would-delete list was computed from the five policies over every version
# instead (2026-10-22 04:40Z): 4,130 of 13,568 versions, about 1.2 TB, none of
# the 11 images then deployed on Cloud Run. Deletion cannot be undone; a
# rollback to a build older than 14 days that was never promoted needs a
# rebuild. Back to true stops deleting at the next release.
cleanup_dry_run = false

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
# BROWSER-POOL-100 (BGD, 2026-10-26, part of #2136): the browser pool provides
# 100 units, and they run BESIDE claude-code's 80 standard, not instead of them.
# A claude-code-browser task acquires every pool of pool_names_for() in one
# transaction (invariant 2), so each pool it shares with claude-code had to hold
# both: 100 browser x 1 unit (1 since contract request 79, 2026-10-14) + 80
# standard x 1 unit = 180. The pools, and what each needed:
#
#   global                 shared with claude-code       100 -> 180
#   resource:browser       browser alone                  20 -> 100
#   runner:claude-code-browser   browser alone           (global) -> 100, stated
#   backend:GKE_AUTOPILOT  shared with claude-code       100 -> 180 (below)
#   provider:anthropic     shared with claude-code       120 -> 200 (below)
#   provider:<p>:tenant:<t>      every tenant's floor     40 -> 100 (owner, below)
#   tenant:<t>             NOT raised: each is one tenant's own ceiling (eng 45,
#     smoke 20, a workspace its record's), which still binds below 100.
#
# provider_tenant 40 -> 100: BGD, 2026-10-26, answering BROWSER-POOL-100's
# question "should any single tenant be able to hold the 100 browser units?"
# with "raise every tenant's floor". It lifts provider:<p>:tenant:<t> for every
# provider a tenant declares, so the platform-wide pools must follow
# (variables.tf's floor rule): anthropic 2 x 100 = 200, openai 1 x 100 = 100.
# A tenant's own tenant:<t> pool is NOT raised by this, so eng still runs at
# most 45 at once until its capacity is raised through the admin API.
#
# A browser pod is 4 vCPU requested (worker 2 + display 2; limits 4 + 3) and
# 8 GiB, request == limit (invariant 7), so 180 pods ask for 720 vCPU and
# 1,440 GiB. It fits the quota: us-central1 regional CPUS read 216 of 3,000 in
# use on 2026-10-21 (docs/scaling.md), and 216 + 720 = 936. Not re-read for this
# change: this lane's identity has no compute.regions.get. No compute class pins
# a family (no ComputeClass, nodeSelector or machine-family in kubernetes/,
# terraform/ or the scheduler's pod spec, grep 2026-10-26), so T2D's 128 vCPU
# quota (below) caps none of these pods.
#
# AS EVERYWHERE IN THIS BLOCK, NONE OF THIS CHANGES THE LIVE POOLS (the
# ignore_changes note above): each needs
# `scripts/pool-limit.sh --pool <pool> --limit <n>`.
pool_limits = {
  global          = 180
  default_tenant  = 40
  provider_tenant = 100

  resource_classes = {
    standard = 80
    browser  = 100
    large    = 40
  }

  runner_profiles = {
    mock        = 40
    generic     = 20
    claude-code = 80
    codex       = 20
    browser     = 20
    # Stated rather than left to fall back to `global`, which is 180 now and
    # would say nothing about browser: resource:browser's 100.
    claude-code-browser = 100
    # DOM-only browser agents on `standard` (contract request 127). Equal to
    # claude-code-browser's ceiling, so moving work here never lowers the
    # browser ceiling; resource:standard (80) still binds below it.
    claude-code-lightpanda = 100
  }

  # GKE_AUTOPILOT 40 -> 100: owner, 2026-10-07, accepting contract request 53
  # (claude-code moves to GKE Autopilot). At 40 the backend pool would bind
  # before claude-code's own ceiling (runner:claude-code 80), shared with
  # browser at 2 units a task (1 since contract request 79, 2026-10-14),
  # and halve claude-code's concurrency on the
  # move. 100 is claude-code's 80 plus browser headroom. It fits the quota:
  # us-central1 regional CPUS read 2,990 of 3,000 free on 2026-10-07, and
  # 100 x 4 vCPU = 400.
  #
  # GKE_AUTOPILOT 100 -> 180: BROWSER-POOL-100 (2026-10-26). claude-code's 80
  # standard plus 100 browser units, every one of them a GKE pod; at 100 this
  # pool would bind at 20 browser tasks beside a full claude-code. 180 x 4 vCPU
  # = 720 against the 3,000 regional CPUS (the arithmetic above pool_limits).
  #
  # NO COMPUTE CLASS MAY PIN T2D. T2D_CPUS quota in us-central1 is 128 vCPUs,
  # 32 standard pods; the pods carry no nodeSelector, so Autopilot's default
  # class picks from every family, and a T2D pin (or a ComputeClass listing
  # T2D alone) would cap this pool at 32 whatever it says here -- and every
  # probe pinned to one family hit "never scheduled" or "GCE out of
  # resources" (request 53, "What the probe saw go wrong").
  backends = {
    CLOUD_RUN_JOB = 100
    GKE_AUTOPILOT = 180
  }

  providers = {
    # Above provider_tenant x tenants, or the shared pool binds before the
    # per-tenant one and the per-tenant ceiling stops meaning anything. The
    # rule at variables.tf:337 enforces this; it is not a guideline.
    #
    #   anthropic  eng + smoke             = 2 x 100 = 200  (set: 200)
    #   openai     eng                     = 1 x 100 = 100  (set: 100)
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
    #
    # 120 -> 180: BROWSER-POOL-100 (2026-10-26). claude-code and
    # claude-code-browser are both anthropic, so 80 standard beside 100
    # browser is 180 anthropic leases; at 120 this pool would bind at 40
    # browser tasks. Still a ceiling AIMD searches below (the 429 back-off
    # above), not a promise.
    #
    # 180 -> 200: provider_tenant 100 (owner, 2026-10-26) sets the floor at
    # 2 x 100, and the plan refuses anything below it. `global` (180) still
    # binds first, so this admits no more than 180 at once.
    anthropic = 200

    # Exactly the floor provider_tenant = 40 implies, and the live value since
    # the 2026-10-02 doubling. It is NOT a measurement: the account-pool reasoning
    # above is about three Anthropic subscriptions, and there is no equivalent
    # OpenAI pool behind this number. provider_tenant applies to every provider
    # uniformly, so lifting it for one lifts the floor for all of them.
    #
    # If that uniformity turns out to be wrong, the fix is a per-provider
    # tenant ceiling, not a smaller number here -- a number below this floor
    # does not fail at runtime, it fails the plan.
    #
    # 40 -> 100: the floor provider_tenant 100 implies (owner, 2026-10-26). It
    # is a ceiling with no OpenAI account pool behind it; codex's own
    # runner:codex (20) still binds first.
    openai = 100
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
# Admitted past the domain check beside swarm-verify: the oracle-api client
# application (owner decision 2026-10-18, #1568), which submits as its own
# tenant u-or-9a0792 below. A literal in terraform/infra/locals.tf until
# DAYONE-TF-1 made it an input.
allowed_users = ["oracle-api@saga-agents-staging.iam.gserviceaccount.com"]

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
    #
    # 45 -> 100: BGD, 2026-10-26, so one tenant can hold the 100 browser units
    # (BROWSER-POOL-100's question; provider_tenant 100 alone left eng at 45).
    # Live once set through `PUT /v1/admin/tenants/eng/limits`; eng's namespace
    # ResourceQuota (kubernetes/apply.sh, default 100 pods) must rise with it, or
    # 100 browser pods at 4 vCPU fill it exactly.
    max_active     = 100
    capacity_units = 100
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

  # The oracle-api client application's own tenant (owner decision
  # 2026-10-18, #1568), modelled on u-sw-c90291 above.
  #
  # ITS OWN TENANT, NEVER A MEMBER OF eng@saga.xyz. The account is an
  # orchestrating client that submits workflows on its own behalf. In eng it
  # would run as eng's worker account with eng's provider keys, GCS prefix and
  # namespace (invariant 9), and its work would count against eng's ceiling.
  # Nor is it a service_accounts listing under eng: a listed account files its
  # work in that group's tenant. Here it reaches nothing of anybody else's:
  # it is in no registered group, so swarm-api resolves it to the personal id
  # below and no X-Swarm-Tenant header can select eng for it.
  #
  # THE KEY IS COMPUTED, as u-sw-c90291's is: tenant_id_for_user gives
  # `oracle-api@saga-agents-staging.iam.gserviceaccount.com` `u-or-9a0792`
  # (lossy and too long, so it carries a digest). Recomputed on main on
  # 2026-10-18 and held by tests/unit/scripts/test_oracle_api_tenant.py; a
  # rename of the account or the project changes it.
  #
  # Admitted at the front door by frontend_iap_members in
  # terraform/bootstrap/terraform.tfvars, and past swarm-api's domain check by
  # ALLOWED_USERS in terraform/infra/locals.tf.
  #
  # No providers and 4/8, swarm-verify's pattern. The owner lends it a pool
  # Claude account rather than registering a key it would hold itself, and a
  # lent account needs no declared provider (the pool's own waits apply). A
  # declared anthropic would also lift provider:anthropic's floor to 4 x
  # provider_tenant = 160, above pool_limits.providers.anthropic, and fail the
  # plan. Its git token is swarm-tenant-u-or-9a0792-git, stored with
  # scripts/create-secrets.sh --stdin and never in this repository. If its
  # limits need changing, PUT /v1/admin/limits/tenant/u-or-9a0792: like every
  # tenant document, these are only the values it is born with.
  u-or-9a0792 = {
    kind            = "user"
    principal       = "oracle-api@saga-agents-staging.iam.gserviceaccount.com"
    display_name    = "Oracle API"
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

# --- the release acceptance's QA identity case (QA-IDENTITY-WIRE, #1649) ----
# run_live_browser_qa in scripts/acceptance/groups/live-browser.sh SKIPs every
# check unless the swarm-verify job carries both. BGD decided on 2026-10-27 to
# grant `smoke`, the tenant the suite already submits in, the QA identity; the
# grant is made in the console by a platform admin, and until it is the case's
# submission is refused qa_identity_not_granted and its first check FAILs.
# The URL is this environment's own console (https://<frontend_hostname>),
# because the case judges "signed in" by its host.
acceptance_qa_tenant   = "smoke"
acceptance_console_url = "https://swarm.saga.xyz"

# --- the SwarmCloud GitHub App (#780, docs/runbooks/github-app.md) -----------
# enable_github_app declares the App's two empty secret slots, which swarm-api
# alone reads (github_app.tf). The four settings below are the App's PUBLIC
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
github_app_name      = "SwarmCloud Saga"

# The 15-minute user-token refresh sweep, swarm-forge-refresh. ON since
# 2026-10-07: swarm-api serves POST /v1/admin/forge/refresh from OB3's release
# (e983d06e, run 37672097489; revision swarm-api-00176). Runbook step 7.
enable_forge_refresh = true

# --- the live browser (docs/design/live-browser.md, #1030) ------------------
# ON in dev: owner decision 2026-10-11 ~15:00 PDT, once every live-browser lane
# had merged (LB-Lon). This is step 4 of terraform/infra/live.tf's rollout
# order; the operator does steps 1-3 BEFORE this applies -- `make apply` with
# it false, `scripts/create-secrets.sh --live-key`, and a swarm-live digest in
# image_refs -- and step 5 after: apply `terraform output -raw
# swarm_live_manifest` to swarm-autopilot. One switch turns on both halves: the
# relay's grants, address and swarm-api's SWARM_LIVE_URL/SWARM_LIVE_KEY, and the
# scheduler's LIVE_BROWSER_SIDECAR, which locals.tf derives from it so a
# claude-code-browser pod gets the display sidecar exactly when there is a
# relay to stream to. Needs enable_gke_autopilot (true above): the relay is a
# StatefulSet on swarm-autopilot and claude-code-browser runs on GKE.
# Saved browser logins stay OFF: that is terraform/bootstrap's
# enable_browser_logins, which this does not touch.
enable_live_browser = true

# --- browser egress (docs/browser-egress.md §9, #1030) -----------------------
# ON in dev: owner decision 2026-10-14 (lane EGRESS-BUILD). Residential egress
# through Bright Data, US exit, for the owner's personal tenant u-bogdan and
# the claude-code-browser profile ONLY; every other tenant and profile runs
# direct. The budget is $150/month: browser_egress.monthly_cap_usd keeps its
# default of 150, which at the default list price of $8/GB is the 18.75 GB
# swarm-egress refuses past. The hard stop is the Bright Data sub-user's own
# $150 traffic limit, set in the vendor dashboard, not here. Raising the cap
# or naming another tenant is a new owner decision, and
# tests/terraform/egress.tftest.hcl holds this block to exactly these values.
#
# enable_browser_egress deploys swarm-egress (step 4 of terraform/infra/egress.tf's
# order). Steps 1-3 were done 2026-10-14: the bootstrap apply, version 1 of
# swarm-egress-key and swarm-egress-credential, and -- from this change on --
# every release builds swarm-egress and pins its digest in image_refs
# (scripts/build-images.sh). Each release then rolls it to swarm-autopilot
# (scripts/roll-egress.sh, after the live relay; never agents-staging).
# Needs enable_gke_autopilot (true above) and enable_live_browser (true just
# above: kubernetes/apply.sh applies swarm-egress after the relay).
# Kill switch: browser_egress.enabled = false and apply (docs/browser-egress.md
# §9.3, "Stopping it").
enable_browser_egress = true
browser_egress = {
  enabled  = true
  tenants  = ["u-bogdan"]
  profiles = ["claude-code-browser"]
  country  = "us"
}

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

# A step's additional repositories (swarm-api ADDITIONAL_REPOSITORIES_ENABLED,
# docs/design/multi-repository-steps.md, #1434): a claude-code, codex,
# generic, browser or claude-code-browser step names other registered,
# readable-by-tasks repositories by `owner/repo`; swarm-api pins each to a
# commit and signs it, and the worker clones each read-only under
# work/repos/<name>, never harvested or checkpointed. ON in dev from the
# release that merges this line, a change held as a draft until the MR lanes
# (MR-BRIDGE, MR-UI, MR-DOCS) have merged (owner request, 2026-10-18). Each
# person still needs a grant per repository while repository_grants_enforced
# is on. Limits: platform settings max_additional_repositories (default 5)
# and additional_repositories_budget_gib (default 6), PUT /v1/admin/settings.
# Kill switch: false and release; every submission naming one is then 422
# additional_repositories_disabled, and a task already stored keeps what it
# was signed with.
additional_repositories_enabled = true

# One switched refusal is ON in dev (owner decision 2026-10-11, after PR 1027
# added it OFF as every new refusal ships). index_refresh_failed: a repo-index
# refresh firing where every repository failed is refused visibly instead of
# finishing quietly with nothing queued. observer_file_issues_unavailable was
# on here too until PR 1054 removed that switch; the key then crashed
# swarm-api's start (release 38126514258). A code that names no switch fails
# that start, and tests/unit/control_plane/test_tfvars_refusal_switches.py
# now fails CI on one first.
#
# role_member_required is ON in dev too (owner decision 2026-10-19, "Viewer
# gate + reach now", lane ROLES-ON-DEV, part of #1518): a workspace `viewer`
# is refused 403 role_member_required on every member route (submit, cancel,
# decide, answer, schedules, repositories, access, accounts, live control,
# browser logins). Everyone keeps `member` until an admin narrows them
# (docs/design/workspace-roles.md §4.7), so this refuses only people an admin
# has made viewers. role_admin_required stays OFF, deliberately: its own
# condition (workspace-roles.md §4.9) is that every group workspace that
# manages secrets has a named workspace admin, and eng and smoke do not yet.
# Turned on before that, the gate would leave a group workspace's secrets
# with nobody but a platform admin able to change them.
api_refusals = {
  index_refresh_failed = true
  role_member_required = true
}

# A platform admin's reach (swarm-api PLATFORM_ADMIN_REACH,
# docs/design/workspace-roles.md §4.2, owner answer Q5; WK-REACH, #847): a
# platform admin may select every READY registered group workspace they are
# not a member of, as its admin, and every such request is audited
# (platform_admin_acted). Never anyone else's personal (`u-`) workspace, nor a
# service workspace. ON in dev only (owner decision 2026-10-19, "Viewer gate
# + reach now", lane ROLES-ON-DEV); prod stays off until the acceptance probe
# (ROLES-ACCEPT) has exercised it here. Kill switch: false and release.
platform_admin_reach = true

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

# frontend_iap_audiences, quota_broker_url and swarm_api_url are no longer
# set here: terraform/infra computes them (DAYONE-TF-1; the IAP audiences from
# the frontend module, the two URLs from the project number). The audiences it
# computes are the two this file held, "/projects/209012342332/global/
# backendServices/817602226733443034" and ".../6904312892305383900".

# CHILD TASKS ON (owner decision 2026-10-11 ~22:35 PDT), in terraform/infra/child_tasks.tf's
# order: the swarm-child-key secret existed, `scripts/create-secrets.sh --child-key`
# added version 1 (22:33 PDT), then this switch. The live browser needs it: a
# live session is signed with the attempt key the worker registers only with a
# SWARM_CHILD_NONCE, so with this off no claude-code-browser attempt gets a
# live path ("no live path for this attempt", no_attempt_key/not_registered,
# task_97f89a31527a464e98f2).
# The measurement child_tasks.tf asks for first (design §9 item 2, deferred to
# #476) was taken 2026-10-11 ~22:30 PDT and PASSES: the tenant pod identity
# (KSA swarm-agent-worker in swarm-tenant-eng) cannot get/list/watch pods,
# pods/log, jobs, events, secrets or configmaps, and the pod mounts no k8s
# token; the tenant worker GSA holds no container.* or run.* role at project
# level and nothing on its Cloud Run jobs. So no other container of the tenant
# can read the nonce from a pod spec or an execution's overrides.
# Side effect, accepted: a running agent may submit depth-1 child tasks.
enable_child_tasks = true
# SCHEDULER-MADE JOBS ON THE SWARM VPC (#1893; owner decision 2026-10-21, part
# of re-doing isolation cutover a1). The Cloud Run Jobs swarm-scheduler creates
# for self-service u-<email> tenants had no VPC egress, so a worker switched to
# state-through-swarm-api reached swarm-api from the internet and its ingress
# answered 404 (the a1 rollback). On: those Jobs get ALL_TRAFFIC egress on the
# swarm subnet, tag swarm-worker, and the existing swarm-job-u-* are refreshed.
# Its one IAM row, roles/compute.networkUser for swarm-scheduler on the swarm
# subnet only (owner: keep it), holds at dev-iam for the owner.
tenant_job_vpc_egress = true
# ISOLATION CUTOVER STEP a1 (#1457, docs/runbooks/isolation-cutover.md), AGAIN.
# The first try (2026-10-21) was rolled back: the Jobs the scheduler creates for
# a tenant terraform does not know (every self-service u-<email> tenant,
# scheduler/dispatch.py) carried no vpc_access, so state/open left over the
# public internet and swarm-api's ingress (internal-and-cloud-load-balancing)
# answered 404. tenant_job_vpc_egress above fixed that (#1893): on 2026-10-22 at
# 01:47Z a mock dispatch rebuilt swarm-job-u-bogdan-mock with
# vpc-access-egress all-traffic on swarm-subnet-us-central1 and the swarm-worker
# tag, as the runbook's switch-on check requires. Its GKE workers already passed
# every state route on the first try (196 calls, all 200).
# ISOLATION CUTOVER STEP b1 (#1457, docs/runbooks/isolation-cutover.md b1).
# eng joins u-bogdan on the state routes. a2 closed first (2026-10-23): u-bogdan's
# worker lost its direct Firestore role, its verify read 34 of 34 objects, and the
# isolation probe from a u-bogdan pod passed all three checks. BGD chose on
# 2026-10-23 to prepare b1 and merge it. The release renders "u-bogdan,eng";
# rollback is removing "eng" here (a1's rollback). eng's Firestore role stays
# bound until b2, which is a separate plan at dev-iam.
state_via_api_tenants = ["u-bogdan", "eng"]
# ISOLATION CUTOVER STEP a2 (#1457, docs/runbooks/isolation-cutover.md):
# swarm-api records u-bogdan as a tenant whose worker has no direct Firestore
# access (WORKER_FIRESTORE_OFF_TENANTS), so the next admin action writes
# worker_firestore_access false and A9 then refuses the role it still holds.
# The role itself is removed by hand (runbook a2 step 4, owner-approved), never
# by the job (rule C9). Only after a1's three u-bogdan agents and verify-remote
# as eng were green.
worker_firestore_off_tenants = ["u-bogdan"]
# ISOLATION CUTOVER STEP b2 (#1457, docs/runbooks/isolation-cutover.md):
# eng's worker loses swarmTenantWorkerFirestore by plan. eng is a Terraform
# tenant, so a hand removal would come back as "1 to add" on the next release.
# BGD decided on 2026-10-23 to start b2 after b1's verify-remote as eng was
# green (PR 2040). The plan must show exactly one row, a destroy:
# module.tenancy.google_project_iam_member.worker_firestore["eng"]. It stops at
# dev-iam for the owner. Rollback: back to [] (one "1 to add" at dev-iam).
worker_firestore_off_tenants_iam = ["eng"]
# THE FORGE BROKER STAYS OFF HERE (#1041, terraform/infra/child_tasks.tf
# `forge_broker`), although child tasks are on: workers keep reading a
# person's git-u- slot with their own account through forge_slot_reader.
# Turning it on is the owner's step, and removing forge_slot_reader comes
# only after it has been on here and a live run passed.
forge_broker = false


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

# Personal-workspace publishing (swarm-api WORKSPACE_APPLY_PUBLISH, #847).
# ON in dev from 2026-10-11 (owner): the swarm-workspace-apply topic exists, and
# an approval now reaches the Cloud Run job swarm-workspace-apply through
# Eventarc and Workflows (WD2 re-decided 2026-10-10; W4b bootstrap applied
# 2026-10-11, 33 resources; W5 cluster policies applied the same day). While it
# was off every approved request waited, logged as workspace_stuck
# (publishing_off). If the job path breaks, turning this off again stops new
# dispatches; the sweep and the stuck alert keep reporting waiting records.
workspace_apply_publish = true

# The personal-workspace submission gate (swarm-api WORKSPACE_GATE, #847 WD8).
# ON in dev from 2026-10-13 (W10). WD8's precondition, one real approval end
# to end through the job, was met that morning: devops@saga.xyz requested
# w-7f9285 (u-devops) 2026-10-12 22:31Z, an admin approved it 2026-10-13
# 00:02Z, the Cloud Run job swarm-workspace-apply ran A1-A9 05:03-05:05Z and
# the workspace reached ready at 05:05Z, and eng:devops-main was then lent to
# it (docs/multi-tenancy.md section 7.5). docs/workspaces.md section 5.5 also
# asks, in the same step, for a task of that person's to have run and for
# u-bogdan and every other existing personal tenant with resources to hold a
# ready record: the owner confirms both before merging this, because on, a
# personal tenant without a ready record is refused. On, a PERSON's
# submission into their own tenant is refused until their workspace is ready
# and has a Claude account (403 WORKSPACE_NOT_READY / NO_CLAUDE_ACCOUNT), and
# swarm-api no longer writes tenants/u-* on first sight. Group submissions are never gated
# (WD7). If it refuses people it should not, set false: the next release
# turns it off, and no workspace record changes.
workspace_gate = true

# The live browser's search tool (swarm-api SEARCH_TOOL_ENABLED /
# SEARCH_PROVIDER, docs/search-tool.md, #1030). OFF in dev as of 2026-10-14:
# the owner chose Brave and "add it (off by default)" on 2026-10-13. It turns on
# once the Brave key is stored with
# `scripts/create-secrets.sh --search-api-key --stdin` -- on without the key,
# every search answers 503 search_unconfigured. search_provider stays at
# its default, "brave".
search_tool_enabled = false

# Registered MCP servers (swarm-api SWARM_MCP_SERVERS,
# docs/design/registered-mcp-servers.md, #1433): a workspace registers its own
# MCP server by name, and a claude-code or codex step names it in
# `input.mcp_servers`; the worker's relay holds the caller's credential so the
# agent never sees it. ON in dev from the release that merges this line, a
# change held as a draft until the MCP acceptance lane passes (owner request,
# 2026-10-18). It also creates swarm-mcp-credentials-sweep. It needs
# terraform/bootstrap's enable_mcp_credentials applied first (the owner's
# apply): without swarm-api's read of swarm-tenant-<t>-mcpc-* every attempt
# naming a `dispatch` server fails at attach. Kill switch: false and release;
# every registration write, submission naming a server and attach is then
# 503 mcp_unavailable, and a stored credential dies at its Secret Manager ttl.
mcp_servers_enabled = true

# Workspace-secret grant attestation (SEC-GRANT-ATTEST, PR 1377;
# terraform/infra/workspace_grant_key.tf,
# docs/runbooks/workspace-grant-attestation.md). Step 2, served in dev as of
# 2026-10-16 (owner chose "mint, serve, sign; required off" ~02:50 PDT):
# version 1 of swarm-workspace-grant-key was added with
# `scripts/create-secrets.sh --workspace-grant-key` before this was set, so
# swarm-api's reference resolves. From this revision on, every grant a manager
# ticks or unticks is signed. `required` stays false, the owner's "warn first,
# flip later": with it on, every grant written before this revision would be
# dropped at release until the attest route has signed it.
workspace_grant_attestation = {
  serve = true
}

# The decision-api tailnet egress (terraform/infra/tailscale.tf,
# docs/runbooks/tailscale-egress.md). ON in dev as of 2026-10-15 (~04:25 PDT):
# the owner stored the tag:swarmcloud OAuth client as swarm-tailscale-oauth
# version 2 and saved the tailnet policy grant (tag:swarmcloud ->
# decision-api-staging tcp:443). internal_decision_api_enabled follows once the
# egress is applied with kubernetes/apply.sh --swarm-system and reaches the API.
enable_tailscale_egress = true
# The decision API's MagicDNS name, the forwarder's one target. No default
# since DAYONE-TF-1: it names this deployment's tailnet.
tailscale_egress_target = "decision-api-staging.taile0bda9.ts.net"

# The GitHub push receiver swarm-github-hooks (GS-HOOKS, PR 1406;
# terraform/infra/github_hooks.tf; docs/runbooks/github-app.md Step 9 step 3).
# ON in dev as of 2026-10-16 (owner: "turn on the GitHub push receiver"). It
# runs as the rollup sweeper (owner decision on #1384, with its blast radius
# stated in the runbook), holds swarmGithubHooksDedup (create-only, applied in
# the bootstrap the same day, PR 1430), and is invoked by allUsers on this one
# service. The IAM rows wait at the release's dev-iam gate. Until the owner
# stores swarm-github-app-webhook-secret (step 4) every signed delivery is a
# 503; until the App's webhook is ticked (step 5) nothing is delivered.
enable_github_hooks = true

# The internal Jev and Laya judgment backends (JUDGE-SELECT), ON in dev as of
# 2026-10-15 ~17:20 PDT (2026-10-16 00:20Z). Verified before switching: the egress joined the
# tailnet (version 3 of swarm-tailscale-oauth, stored 2026-10-15 19:32Z), and
# after JEV-URL (PR 1382) a `swarm-judge: internal` pod with the scheduler's
# hostAliases entry got HTTP 400 for the empty probe from
# https://decision-api-staging.taile0bda9.ts.net/{jev,laya}/v1/decide, while
# an unlabelled pod timed out. Off renders no URL, and an internal choice
# leaves Jev unavailable instead of sending page text to TypeSafe.
internal_decision_api_enabled = true

# The TypeSafe forwarder (SEC-INJECT-A/B, #1347; terraform/infra/typesafe_fwd.tf,
# docs/design/workspace-secrets.md section 3.5). Step 1 of 3, ON in dev as of
# 2026-10-16 (owner decision ~03:45 PDT): creates swarm-typesafe-fwd-key with
# no version and its two readers, swarm-api and the swarm-typesafe-fwd KSA --
# the IAM row held at the release's dev-iam gate. The bootstrap half
# (enable_typesafe_forwarder_read: the forwarder's conditioned
# secretmanager.versions.access on swarm-tenant-<t>-typesafe versions) was
# applied the same morning. typesafe_forwarder.serve is a separate change made
# only after `scripts/create-secrets.sh --typesafe-fwd-key` has written a
# version: a Cloud Run reference to a secret with no version stops swarm-api
# starting. Nothing forwards until typesafe_forwarder.enabled names tenants,
# and no agent pod reaches the forwarder before SEC-MIGRATE-TYPESAFE.
enable_typesafe_forwarder = true

# Step 2 of 3, 2026-10-16 (owner: "mint the key, then a serve PR"). Version 1
# of swarm-typesafe-fwd-key was added at 19:00Z with
# `scripts/create-secrets.sh --typesafe-fwd-key` (generated, never shown), so
# swarm-api's reference resolves. `serve` puts SWARM_TYPESAFE_FWD_KEY on
# swarm-api and renders swarm_typesafe_fwd_manifest, which the release's
# "roll swarm-typesafe-fwd" step (PR 1417, scripts/roll-typesafe-fwd.sh)
# applies to swarm-system. `enabled` stays false and `tenants` empty: the
# forwarder forwards nothing until the owner names tenants (step 3), and pods
# reach it only once SWARM_TYPESAFE_INJECT is set and the owner has re-applied
# `kubernetes/apply.sh --policies` for allow-typesafe-fwd (PR 1409).
typesafe_forwarder = {
  serve   = true
  enabled = false
}

# The shared graph service (#1384, docs/design/graph-service.md;
# terraform/infra/graph_service.tf). Step 1 of 3, ON in dev as of 2026-10-16
# (owner decision ~02:20 PDT, recorded on #1384): creates the
# swarm-graph-ticket-key container (no version), its two readers (swarm-api
# and the swarm-graph KSA) and swarm-graph's read of
# tenants/<t>/repos/<r>/graph/** -- the IAM rows held at the release's dev-iam
# gate. graph_service.serve (step 3) is a separate change made only after
# `scripts/create-secrets.sh --graph-ticket-key` has written a version:
# serving references the key from swarm-api, and a reference to a secret with
# no version stops swarm-api starting.
enable_graph_service = true

# Step 3 of 3 (owner decision 2026-10-16, "run dark", recorded on #1384):
# serve, but do not answer. `serve` puts SWARM_GRAPH_TICKET_KEY on swarm-api
# by reference and renders the swarm-graph manifest the release's "roll
# swarm-graph" step applies (two pods, 1 CPU / 3 GiB each, requests == limits).
# It merges only after `scripts/create-secrets.sh --graph-ticket-key` has
# written the key's first version (version 1, 2026-10-16 ~12:03Z): a Cloud
# Run reference to a secret with no version stops swarm-api starting.
# `enabled` stays false, so swarm-api mints
# no graph ticket and no workspace uses the service until the owner turns it
# on and a workspace's code_intelligence.graph_delivery is `service`.
#
# ENABLED 2026-10-16 (owner: "enable a workspace"). The service answers and
# swarm-api mints graph tickets, but only a workspace whose
# code_intelligence.graph_delivery is `service` uses it. Every other workspace
# keeps `staged`, its default, and so does any attempt where the service is
# unreachable (the worker falls back to staging). The first workspace switched
# is u-bogdan, by PUT /v1/workspaces/u-bogdan/code-intelligence after this
# release. Kill switch: enabled = false and release; the pods keep serving
# nothing and every workspace stages again.
graph_service = {
  serve   = true
  enabled = true
}

# SwarmCloud's worker logs for swarm-api (agent-autonomy.md §3.5, IAM row
# L-1; BGD's decision 2026-10-22, #1960): the swarm-worker-logs bucket (30
# days, us-central1), a sink routing only swarm-tenant-* containers on the
# swarm cluster and swarm-job-* Cloud Run jobs into it, the sink writer's
# bucketWriter on that bucket, and the swarm-workers view on it with
# swarm-api's viewAccessor. Two IAM rows: the release holds at dev-iam. Prod
# stays off. Kill switch: false and release.
swarm_workers_log_view_enabled = true

# The acceptance suite's public fixture bucket (terraform/infra/acceptance_fixture.tf),
# an addon:ci piece that defaults off since DAYONE-TF-2 so an install makes no
# public bucket. This deployment runs the acceptance suite, so it stays on and
# the live plan shows no change.
enable_acceptance_fixture = true

# Settings signing, step one (docs/design/tenant-settings-integrity.md; #1457). The
# key's first version was generated 2026-11-01 by scripts/create-secrets.sh
# --settings-mac-key, so swarm-api and the scheduler may now reference it as
# SWARM_SETTINGS_KEY. No tenant is cut over by this: scripts/resign-workspace-settings.sh
# runs against the deployed key first, and only then does a second change add
# Engineering to settings_mac_tenants and turn settings_mac_required on. Owner decision
# 2026-11-01: sign settings so the Jev quality checks can run on request for the
# code intelligence experiment.
settings_mac_key_ready = true

# Settings signing, step two: Engineering is cut over and the scheduler refuses
# an unsigned or badly signed setting for it. scripts/resign-workspace-settings.sh
# --tenant eng --apply signed Engineering's two stored blocks on 2026-11-01 after
# its dry run's covered values were read against Engineering's admin_audit rows
# (judgment backend typesafe set 10-23, Jev on 10-23, graph delivery service
# 10-28, both context fields never set); a second dry run found nothing left to
# sign. Only Engineering: every other workspace stays unsigned and unverified
# until it is re-signed and listed here. Owner decision 2026-11-01 ("Sign
# settings; checks on request").
settings_mac_tenants  = ["eng"]
settings_mac_required = true
