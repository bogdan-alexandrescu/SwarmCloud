# The tenant-Job VPC egress switch (terraform/infra/tenant_job_vpc.tf; #1893).
#
# The Cloud Run Jobs swarm-scheduler creates for a tenant this root does not
# list had no vpc_access, so a worker switched to state-through-swarm-api
# reached swarm-api from the internet and was answered 404 at its ingress (the
# 2026-10-21 isolation cutover a1 rollback). These runs hold the wiring: the
# switch ships off, the network, subnetwork and worker tag reach the scheduler
# from module.network -- the outputs modules/cloud_run_jobs is given, never a
# literal -- and the scheduler's subnet grant exists only when the switch is
# on, on the swarm subnetwork alone.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "the_switch_ships_off_with_no_subnet_grant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    # MUTATION: default tenant_job_vpc_egress to true and this fails: a merge
    # would move every scheduler-made Job before anyone approved the grant.
    condition     = local.service_env["swarm-scheduler"].TENANT_JOB_VPC_EGRESS == "false"
    error_message = "tenant_job_vpc_egress ships off: TENANT_JOB_VPC_EGRESS renders \"false\" on swarm-scheduler"
  }

  assert {
    # MUTATION: drop the count on the grant and an off release plans an IAM row.
    condition     = length(google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet) == 0
    error_message = "with the switch off there is no subnet grant for the scheduler"
  }
}

run "the_switch_on_renders_module_network_and_grants_the_swarm_subnet_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenant_job_vpc_egress = true
  }

  override_resource {
    target          = module.network.google_compute_network.this
    override_during = plan
    values = {
      id = "projects/saga-agents-staging/global/networks/swarm-vpc"
    }
  }

  override_resource {
    target          = module.network.google_compute_subnetwork.this
    override_during = plan
    values = {
      id = "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1"
    }
  }

  override_resource {
    target          = module.iam.google_service_account.platform["swarm-scheduler"]
    override_during = plan
    values = {
      email = "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    # MUTATION: render the switch from anything but the variable and this fails.
    condition     = local.service_env["swarm-scheduler"].TENANT_JOB_VPC_EGRESS == "true"
    error_message = "tenant_job_vpc_egress = true renders TENANT_JOB_VPC_EGRESS \"true\" on swarm-scheduler"
  }

  assert {
    # MUTATION: restate the network as a literal (or another output) and this fails.
    condition     = local.service_env["swarm-scheduler"].TENANT_JOB_NETWORK == module.network.network_id && module.network.network_id == "projects/saga-agents-staging/global/networks/swarm-vpc"
    error_message = "TENANT_JOB_NETWORK is module.network.network_id, the value modules/cloud_run_jobs is given"
  }

  assert {
    # MUTATION: restate the subnetwork as a literal (or another output) and this fails.
    condition     = local.service_env["swarm-scheduler"].TENANT_JOB_SUBNETWORK == module.network.subnetwork_id && module.network.subnetwork_id == "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1"
    error_message = "TENANT_JOB_SUBNETWORK is module.network.subnetwork_id, the value modules/cloud_run_jobs is given"
  }

  assert {
    # MUTATION: render any tag but module.network.worker_network_tag and the
    # worker-ingress deny rule cannot name the scheduler's Jobs.
    condition     = local.service_env["swarm-scheduler"].TENANT_JOB_NETWORK_TAGS == module.network.worker_network_tag && module.network.worker_network_tag == "swarm-worker"
    error_message = "TENANT_JOB_NETWORK_TAGS is module.network.worker_network_tag"
  }

  assert {
    # MUTATION: drop the grant and the scheduler's Job create is refused on the subnet.
    condition     = length(google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet) == 1
    error_message = "with the switch on the scheduler holds exactly one subnet grant"
  }

  assert {
    # MUTATION: name any subnetwork but module.network's and the grant reaches a
    # subnet this root does not own -- in a shared project, possibly the other team's.
    condition     = google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet[0].subnetwork == module.network.subnetwork_id && google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet[0].region == var.region && google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet[0].project == var.project_id
    error_message = "the scheduler's grant is on the swarm subnetwork alone"
  }

  assert {
    # MUTATION: grant it to any account but swarm-scheduler's and the
    # scheduler still cannot attach the subnet while another account can.
    condition     = google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet[0].member == "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the subnet grant is swarm-scheduler's service account's"
  }

  assert {
    # MUTATION: widen the role (compute.networkAdmin, editor) and this fails.
    condition     = google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet[0].role == "roles/compute.networkUser"
    error_message = "the subnet grant is roles/compute.networkUser, the smallest predefined role holding compute.subnetworks.use and compute.subnetworks.get"
  }

  assert {
    # MUTATION: render the four names onto another service and this fails.
    condition = alltrue([
      for svc in ["swarm-api", "swarm-quota-broker", "swarm-reconciler"] :
      !contains(keys(local.service_env[svc]), "TENANT_JOB_VPC_EGRESS")
    ])
    error_message = "the tenant-Job VPC switch is the scheduler's alone: it is the only component that creates tenant Jobs"
  }
}
