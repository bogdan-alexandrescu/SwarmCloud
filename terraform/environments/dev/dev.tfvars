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

# gke_master_authorized_cidrs is DELIBERATELY EMPTY HERE and set at apply time
# instead:
#
#   make infra TF_ARGS="-var gke_master_authorized_cidrs=[{cidr_block=\"$(curl -s ifconfig.me)/32\",display_name=\"operator\"}]"
#
# Two reasons it is not committed. This repository is PUBLIC, so an operator's
# home address in git is a personal detail published permanently; and the value
# rots the moment anyone changes network, at which point the committed answer is
# worse than no answer because it looks authoritative.
#
# An empty list means the control plane is reachable only from inside the VPC,
# which is the correct default and the one prod should keep. Reaching it from a
# laptop is a dev convenience, and it should read like one.

# --- data ------------------------------------------------------------------
firestore_database      = "swarm"
artifact_retention_days = 14

# --- images ----------------------------------------------------------------
image_tag            = "bootstrap"
immutable_image_tags = false

# --- capacity --------------------------------------------------------------
# Small on purpose. Dev exists to prove the control plane behaves, not to run
# the fleet, and a runaway loop here spends real money.
pool_limits = {
  global          = 20
  default_tenant  = 10
  provider_tenant = 5

  resource_classes = {
    standard = 20
    browser  = 4
    large    = 2
  }

  runner_profiles = {
    mock        = 20
    generic     = 10
    claude-code = 10
    codex       = 10
    browser     = 4
  }

  backends = {
    CLOUD_RUN_JOB = 20
    GKE_AUTOPILOT = 4
  }

  providers = {
    anthropic = 10
    openai    = 10
  }
}

service_max_instances = {
  "swarm-api"          = 5
  "swarm-scheduler"    = 2
  "swarm-quota-broker" = 2
  "swarm-reconciler"   = 1
}

settings_env = {
  PERFORMANCE_PROFILE = "economy"
}

# --- tenancy ---------------------------------------------------------------
allowed_domains = ["saga.xyz"]

tenants = {
  # A Google group. swarm_common.identity turns eng@saga.xyz into `eng`.
  eng = {
    kind           = "group"
    principal      = "eng@saga.xyz"
    display_name   = "Engineering"
    providers      = ["anthropic", "openai"]
    max_active     = 10
    capacity_units = 20
  }

  # The mock runner needs no provider key, so this tenant can smoke-test the
  # whole path before anybody registers a credential.
  smoke = {
    kind           = "group"
    principal      = "swarm-smoke@saga.xyz"
    display_name   = "Smoke tests"
    providers      = []
    max_active     = 2
    capacity_units = 4
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
