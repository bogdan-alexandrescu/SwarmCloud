# Direct VPC egress for the Cloud Run Jobs swarm-scheduler creates (#1893).
#
# Terraform's worker Jobs leave through the swarm network
# (modules/cloud_run_jobs `vpc_access`: ALL_TRAFFIC on module.network's network
# and subnetwork, tagged module.network.worker_network_tag). The Jobs the
# scheduler creates for a tenant this root does not list -- every self-service
# `u-<email>` tenant -- had none. A worker switched to state-through-swarm-api
# (`state_via_api_tenants`) then called swarm-api from the public internet,
# and swarm-api's `internal-and-cloud-load-balancing` ingress answered 404
# before the app saw the request: isolation cutover a1 was rolled back for it
# on 2026-10-21 (PR 1892). The same Jobs' provider calls bypassed the swarm
# Cloud NAT, and the worker-ingress deny rule could not name them.
#
# On, the scheduler builds terraform's block from TENANT_JOB_NETWORK,
# TENANT_JOB_SUBNETWORK and TENANT_JOB_NETWORK_TAGS (locals.tf), rendered from
# the SAME module.network outputs module "cloud_run_jobs" is given (main.tf),
# never as literals, and rebuilds the Jobs it made before
# (scheduler.dispatch `_refresh_image`, labelled managed-by=swarm-scheduler
# only).
#
# THE GRANT IS TIED TO THE SWITCH, not a second variable: the switch on without
# it is the one state that is always wrong, and the env value below references
# the grant, so in the one apply that turns it on the scheduler's new revision
# waits for the grant to exist. Being IAM, that apply is held at dev-iam for
# the owner (docs/ci.md).

# DEFAULTS FALSE, and dev.tfvars is the owner's to change: on, the release
# plans an IAM row (the subnet grant below) that must be reviewed and applied
# at dev-iam before the scheduler creates or rewrites a Job on the subnet.
# Off, every scheduler-made Job is built byte-for-byte as before this existed.
variable "tenant_job_vpc_egress" {
  description = <<-EOT
    Give the Cloud Run Jobs swarm-scheduler creates (self-service u-<email>
    tenants, non-default classes) terraform's Direct VPC egress: ALL_TRAFFIC on
    the swarm network and subnetwork, tagged swarm-worker (#1893). Rendered as
    TENANT_JOB_VPC_EGRESS with the three names from module.network; the
    scheduler also rebuilds the Jobs it already made. Creates the scheduler's
    roles/compute.networkUser grant on the swarm subnetwork alone. Must be true
    before isolation cutover a1 (docs/runbooks/isolation-cutover.md).
  EOT
  type        = bool
  default     = false
  nullable    = false
}

# WHY AT THE SUBNETWORK, never the project. A project-level networkUser would
# let swarm-scheduler place a Job on ANY subnet in saga-agents-staging, a
# shared project holding the other team's agents-staging-vpc (CLAUDE.md rule 2,
# SHARED_DENY_LIST in scripts/lib/common.sh). Here it can attach only
# module.network's subnetwork.
#
# WHY THIS PRINCIPAL, and what the documentation does and does not say. Cloud
# Run's Direct VPC egress page
# (https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc, "Set up
# IAM permissions", read 2026-10-21) names the permissions the CLOUD RUN
# SERVICE AGENT needs -- compute.networks.get, compute.subnetworks.get,
# compute.subnetworks.use "on the project or the specific subnet",
# compute.addresses.*, compute.regionOperations.get -- and says the default
# roles/run.serviceAgent already holds them. This network is in the Job's own
# project, so the service agent needs nothing from this root. The page does
# NOT say what the principal that creates or updates the Job needs; the grant
# below is made because swarmJobDispatcher holds only run.* permissions and
# the subnet is the one thing it would newly name. It is held as an open
# question on #1893 until a release with the switch on shows whether the
# create needs it.
#
# WHY roles/compute.networkUser: the smallest PREDEFINED role holding both
# compute.subnetworks.use and compute.subnetworks.get. A custom role would be
# narrower, and custom roles are created in terraform/bootstrap (see
# custom_roles_moved_to_bootstrap.tf), which this change does not touch.
resource "google_compute_subnetwork_iam_member" "scheduler_tenant_job_subnet" {
  count = var.tenant_job_vpc_egress ? 1 : 0

  project    = var.project_id
  region     = var.region
  subnetwork = module.network.subnetwork_id
  role       = "roles/compute.networkUser"
  member     = "serviceAccount:${module.iam.scheduler_service_account}"
}
