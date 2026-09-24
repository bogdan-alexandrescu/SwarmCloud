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

# --- images ----------------------------------------------------------------
image_tag            = "bootstrap"
immutable_image_tags = false

# --- capacity --------------------------------------------------------------
# Small on purpose. Dev exists to prove the control plane behaves, not to run
# the fleet, and a runaway loop here spends real money.
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
# provider_tenant is now sized for the ACCOUNT POOL rather than for one
# subscription -- three accounts at roughly five concurrent agents each. AIMD
# still backs off multiplicatively on the first 429, so this is room to search
# in, not a promise that fifteen will work.
pool_limits = {
  global          = 40
  default_tenant  = 20
  provider_tenant = 15

  resource_classes = {
    standard = 40
    browser  = 4
    large    = 2
  }

  runner_profiles = {
    mock        = 20
    generic     = 10
    claude-code = 20
    codex       = 10
    browser     = 4
  }

  backends = {
    CLOUD_RUN_JOB = 40
    GKE_AUTOPILOT = 4
  }

  providers = {
    # Above provider_tenant x tenants, or the shared pool binds before the
    # per-tenant one and the per-tenant ceiling stops meaning anything. The
    # rule at variables.tf:337 enforces this; it is not a guideline.
    #
    #   anthropic  eng + u-bogdan  = 2 x 15 = 30
    #   openai     eng             = 1 x 15 = 15
    anthropic = 30

    # Raised from 10 only because provider_tenant went to 15 and this is the
    # floor that implies. It is NOT a measurement: the account-pool reasoning
    # above is about three Anthropic subscriptions, and there is no equivalent
    # OpenAI pool behind this number. provider_tenant applies to every provider
    # uniformly, so lifting it for one lifts the floor for all of them.
    #
    # If that uniformity turns out to be wrong, the fix is a per-provider
    # tenant ceiling, not a smaller number here -- a number below this floor
    # does not fail at runtime, it fails the plan.
    openai = 15
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
    max_active      = 10
    capacity_units  = 20
  }

  # The mock runner needs no provider key, so this tenant can smoke-test the
  # whole path before anybody registers a credential.
  smoke = {
    kind      = "group"
    principal = "swarm-smoke@saga.xyz"
    # NO SUCH GROUP EXISTS. Verified 2026-09-19:
    #   gcloud identity groups describe swarm-smoke@saga.xyz
    #   -> "There is no such a group"
    # while eng@saga.xyz resolves to groups/01gf8i8328uclsx.
    #
    # Nothing signs in as this tenant -- the smoke suite dispatches through
    # Firestore in-process, the same way scripts/swarm.py does -- so the group
    # was never created and never needed. Left in TENANT_GROUPS it made EVERY
    # authenticated request 503, because a lookup that cannot be answered is
    # fatal by design.
    directory_group = false
    display_name    = "Smoke tests"
    providers       = []
    max_active      = 2
    capacity_units  = 4
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
    max_active      = 2
    capacity_units  = 4
  }

  # A personal fallback tenant. swarm_common.identity maps a caller who is in
  # none of the registered groups to `u-<local part>`, and that is what an
  # operator running the smoke test from their laptop actually resolves to:
  # group membership is resolved through Cloud Identity, and the swarm-api
  # service account has no permission to read groups, so every human currently
  # lands here rather than in `eng`. Without a tenant entry there are no
  # per-tenant Cloud Run Jobs to dispatch to, and an admitted task holds its
  # lease with nowhere to run.
  #
  # Declaring it keeps the dev environment self-testing. In prod, grant the API
  # service account group-read instead of enumerating humans here.
  u-bogdan = {
    kind         = "user"
    principal    = "bogdan@saga.xyz"
    display_name = "Bogdan (personal)"
    # Declaring a provider creates the tenant's OWN Secret Manager container and
    # its claude-code Cloud Run Job. The key material is not managed here:
    # scripts/create-secrets.sh adds versions, so no plaintext ever reaches the
    # Terraform state file, which several teams can read.
    providers      = ["anthropic"]
    max_active     = 2
    capacity_units = 4
  }
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

# The outer gate only. swarm-api stays the tenant boundary: it verifies the
# token, enforces allowed_domains above, and scopes every read to the caller's
# own tenant. This matches that domain rather than maintaining a second list,
# because a hand-maintained list of principals is the shape that rots -- as the
# GKE allowlist in this same file did.
frontend_iap_members = ["domain:saga.xyz"]

# The IAP OAuth brand is NOT created by terraform: google_iap_brand cannot be
# deleted, so terraform could create one and never remove it. It is a
# once-per-project manual step, documented in terraform/modules/frontend/main.tf.

# OFF because the metric it watches does not exist in this project. Verified
# 2026-09-19: the tick job is ENABLED and attempting every minute, and the
# project has zero cloudscheduler.googleapis.com metric descriptors. Creating
# the policy therefore fails the apply, and leaving it on made every
# `make deploy` exit 2 -- which teaches everyone to ignore the exit code.
#
# Turn it back on once the metric appears; the check is in the module variable's
# description.
enable_safety_tick_alert = false

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
admin_users = ["bogdan@saga.xyz"]

# Domain-wide delegation was authorised in the Admin console on 2026-09-20 for
# this service account's OAuth client id, scoped to
# cloud-identity.groups.readonly. swarm-api acts as this user when it reads
# group membership, because a service account is not a Workspace principal and
# the Groups API does not authorize through GCP IAM at all.
groups_impersonate_user = "bogdan@saga.xyz"
