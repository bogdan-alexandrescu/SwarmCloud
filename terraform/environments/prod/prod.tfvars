# Production environment.
#
#   terraform -chdir=terraform/infra init \
#     -backend-config="bucket=swarm-tfstate-saga-agents-staging" \
#     -backend-config="prefix=infra/prod"
#   terraform -chdir=terraform/infra plan -var-file=../environments/prod/prod.tfvars
#
# deletion_protection is true and MUST stay true. The variable validation in
# terraform/infra/variables.tf refuses a prod plan with it turned off unless
# allow_unprotected_prod is also set, which is the deliberate override -- there
# is no way to disable protection by accident, only on purpose.

project_id  = "saga-agents-staging"
region      = "us-central1"
environment = "prod"

deletion_protection    = true
allow_unprotected_prod = false
bucket_force_destroy   = false

extra_labels = {
  "swarm-owner" = "platform"
  "swarm-tier"  = "production"
}

# --- network ---------------------------------------------------------------
subnet_cidr         = "10.60.0.0/20"
pods_cidr           = "10.64.0.0/14"
services_cidr       = "10.68.0.0/20"
gke_master_cidr     = "172.16.48.0/28"
nat_static_ip_count = 2

# Left off until the full egress set is measured in dev. Turning it on without
# that measurement breaks git clones over non-standard transports, which fails
# a task mid-run rather than at admission.
restrict_egress = false

# --- GKE Autopilot ---------------------------------------------------------
enable_gke_autopilot = true

# STABLE: production takes GKE upgrades last, after they have been through RAPID
# and REGULAR elsewhere.
gke_release_channel = "STABLE"

# Left false until a bastion or an IAP tunnel exists inside swarm-vpc; turning
# it on first would lock terraform itself out of the control plane.
gke_enable_private_endpoint = false

gke_master_authorized_cidrs = []

# --- data ------------------------------------------------------------------
firestore_database      = "swarm"
artifact_retention_days = 180

# --- images ----------------------------------------------------------------
image_tag = "bootstrap"

# A tag can never be repointed in prod: the image an attempt ran is still the
# image that tag names, which is what makes a post-mortem possible.
immutable_image_tags = true

# --- capacity --------------------------------------------------------------
pool_limits = {
  global          = 100
  default_tenant  = 20
  provider_tenant = 10

  resource_classes = {
    standard = 100
    browser  = 20
    large    = 8
  }

  runner_profiles = {
    mock        = 10
    generic     = 40
    claude-code = 60
    codex       = 40
    browser     = 20
  }

  backends = {
    CLOUD_RUN_JOB = 100
    GKE_AUTOPILOT = 20
  }

  providers = {
    anthropic = 60
    openai    = 40
  }
}

service_max_instances = {
  "swarm-api"          = 30
  "swarm-scheduler"    = 3
  "swarm-quota-broker" = 3
  "swarm-reconciler"   = 2
}

settings_env = {
  PERFORMANCE_PROFILE = "balanced"
}

# --- tenancy ---------------------------------------------------------------
allowed_domains = ["saga.xyz"]

tenants = {
  eng = {
    kind           = "group"
    principal      = "eng@saga.xyz"
    display_name   = "Engineering"
    providers      = ["anthropic", "openai"]
    max_active     = 40
    capacity_units = 80
  }

  research = {
    kind           = "group"
    principal      = "research@saga.xyz"
    display_name   = "Research"
    providers      = ["anthropic"]
    max_active     = 20
    capacity_units = 40
  }

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
  "group:research@saga.xyz",
]

secret_admin_members = [
  "group:swarm-admins@saga.xyz",
]

# --- observability ---------------------------------------------------------
create_alerts = true

alert_emails = [
  "swarm-alerts@saga.xyz",
]
