# The control plane. Four services, four identities, and an idle cost of zero:
# a backlog of ten thousand QUEUED tasks must produce no running containers
# (CONTRACT.md invariant 1).

mock_provider "google" {}

variables {
  project_id = "saga-agents-staging"
  network    = "projects/saga-agents-staging/global/networks/swarm-vpc"
  subnetwork = "projects/saga-agents-staging/regions/us-central1/subnetworks/swarm-subnet-us-central1"
  labels     = { "managed-by" = "swarm-terraform" }

  services = {
    "swarm-api" = {
      service_account_email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api:test"
      max_instances         = 20
      cpu                   = "1"
      memory                = "1Gi"
      invokers              = { eng = "group:eng@saga.xyz" }
    }
    "swarm-scheduler" = {
      service_account_email = "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
      image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler:test"
      max_instances         = 3
      concurrency           = 1
      invokers              = { tick = "serviceAccount:swarm-tick@saga-agents-staging.iam.gserviceaccount.com" }
    }
    "swarm-quota-broker" = {
      service_account_email = "swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com"
      image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker:test"
      max_instances         = 3
    }
    "swarm-reconciler" = {
      service_account_email = "swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
      image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler:test"
      max_instances         = 2
      concurrency           = 1
    }
  }
}

run "idle_costs_nothing_and_scale_is_bounded" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].scaling[0].min_instance_count == 0
    ])
    error_message = "min-instances must be 0 on every control-plane service: a queued backlog costs nothing"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].scaling[0].max_instance_count > 0
    ])
    error_message = "max-instances must be explicit: an unbounded control plane can outscale its own database"
  }

  assert {
    condition     = google_cloud_run_v2_service.this["swarm-scheduler"].template[0].max_instance_request_concurrency == 1
    error_message = "concurrent requests on one scheduler instance contend on the same Firestore documents and abort each other"
  }
}

run "the_control_plane_is_not_on_the_internet" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    ])
    error_message = "ingress must be internal-and-cloud-load-balancing"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      length(svc.template[0].vpc_access) == 1 && svc.template[0].vpc_access[0].network_interfaces[0].subnetwork == var.subnetwork
    ])
    error_message = "every service egresses through the swarm subnet via Direct VPC egress"
  }

  assert {
    condition = alltrue([
      for k, m in google_cloud_run_v2_service_iam_member.invokers :
      m.member != "allUsers" && m.member != "allAuthenticatedUsers"
    ])
    error_message = "no public invoker: callers are authenticated by Google ID token"
  }

  assert {
    condition = alltrue([
      for k, m in google_cloud_run_v2_service_iam_member.invokers :
      m.role == "roles/run.invoker"
    ])
    error_message = "invoker bindings grant run.invoker and nothing else"
  }
}

run "each_service_runs_as_its_own_identity_with_requests_equal_to_limits" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  assert {
    condition = length(distinct([
      for name, svc in google_cloud_run_v2_service.this : svc.template[0].service_account
    ])) == length(google_cloud_run_v2_service.this)
    error_message = "a shared service account would make 'only the reconciler may delete' unenforceable"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].execution_environment == "EXECUTION_ENVIRONMENT_GEN2"
    ])
    error_message = "second-generation execution environment is required"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].containers[0].resources[0].limits["cpu"] != "" && svc.template[0].containers[0].resources[0].limits["memory"] != ""
    ])
    error_message = "CPU and memory must both be explicit: an unstated limit is not a limit"
  }

  # An explicit limit is not the same as request == limit. These two fields are
  # the only ways Cloud Run breaks that equality, and an assertion about
  # invariant 7 that does not mention them is an assertion about nothing.
  #
  # startup_cpu_boost allocates MORE than the declared limit while an instance
  # starts, which is bursting in the plain sense of the word. cpu_idle throttles
  # CPU to near zero between requests, so the guaranteed allocation is the limit
  # only while a request happens to be in flight -- and the scheduler's drain
  # loop and the reconciler's sweep both run for minutes inside one request.
  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].containers[0].resources[0].startup_cpu_boost == false
    ])
    error_message = "startup_cpu_boost allocates CPU above the declared limit during startup: that is bursting, which invariant 7 forbids"
  }

  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].containers[0].resources[0].cpu_idle == false
    ])
    error_message = "cpu_idle = true means CPU is allocated only during a request, so the guaranteed request is not equal to the limit (invariant 7)"
  }

  assert {
    condition     = google_cloud_run_v2_service.this["swarm-api"].name == "swarm-api"
    error_message = "service names carry the swarm- prefix the destroy guard looks for"
  }
}

run "a_public_invoker_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  variables {
    services = {
      "swarm-api" = {
        service_account_email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
        image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api:test"
        max_instances         = 20
        invokers              = { everyone = "allUsers" }
      }
    }
  }

  expect_failures = [var.services]
}

run "internet_ingress_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  variables {
    ingress = "INGRESS_TRAFFIC_ALL"
  }

  expect_failures = [var.ingress]
}
