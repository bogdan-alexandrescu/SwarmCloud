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
}

api_invokers = [
  "group:eng@saga.xyz",
]

secret_admin_members = [
  "group:eng@saga.xyz",
]

# --- observability ---------------------------------------------------------
create_alerts = true
alert_emails  = []
