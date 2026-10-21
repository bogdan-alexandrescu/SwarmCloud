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
      invokers              = { eng = "group:eng@example.com" }
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

# The one warm floor (owner decision Q6(a), #1457): swarm-api asks for two
# instances and gets them; every service that does not ask still scales to zero.
run "a_service_that_sets_min_instances_keeps_that_floor_and_no_other_does" {
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
        min_instances         = 2
      }
      "swarm-scheduler" = {
        service_account_email = "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
        image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler:test"
        max_instances         = 3
        concurrency           = 1
      }
    }
  }

  assert {
    condition     = google_cloud_run_v2_service.this["swarm-api"].template[0].scaling[0].min_instance_count == 2
    error_message = "swarm-api's min_instances must reach the revision: under design B every heartbeat goes through it"
  }

  assert {
    condition     = google_cloud_run_v2_service.this["swarm-scheduler"].template[0].scaling[0].min_instance_count == 0
    error_message = "a service that sets no min_instances must still scale to zero"
  }
}

run "a_floor_above_the_ceiling_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  variables {
    services = {
      "swarm-api" = {
        service_account_email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
        image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api:test"
        max_instances         = 1
        min_instances         = 2
      }
    }
  }

  expect_failures = [var.services]
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

run "a_public_invoker_is_accepted_only_while_ingress_is_restricted" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  # REVERSED on evidence, 2026-09-16. allUsers used to be refused outright.
  # When Cloud Run enforces IAM it CONSUMES the caller's Authorization header
  # and the container receives a different, non-JWT credential -- measured on a
  # correlated request: the client sent tok:7f518e564d27, the application saw
  # tok:c130e289a085 and failed with MalformedError. The same image and token
  # authenticate correctly when the edge is not gating, so edge IAM does not add
  # a layer here, it removes the only one that can identify a tenant.
  #
  # What authenticates a caller is swarm_api.auth: Google ID token signature,
  # email_verified, the example.com hosted domain, and Cloud Identity group
  # membership. What keeps the service off the internet is ingress, which this
  # module pins to internal and refuses to set to INGRESS_TRAFFIC_ALL.
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

  assert {
    condition     = var.ingress != "INGRESS_TRAFFIC_ALL"
    error_message = "allUsers is only safe because ingress keeps the service off the internet; this pairing is the whole guarantee."
  }

  assert {
    condition = alltrue([
      for k, v in var.services : !contains(values(v.invokers), "allAuthenticatedUsers")
    ])
    error_message = "allAuthenticatedUsers means every Google account and adds nothing over the app's own check."
  }
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

# A slow but healthy start is not killed (#1816, options (d) and (e) chosen by
# the owner on 2026-10-21). The startup probe polls every 5 s; at 12 failures a
# start is killed at about 96 s, and five healthy swarm-api starts measured at
# 61-77 s on 2026-10-21 left too little room. swarm-api asks for 36 (three
# times the window); every service that does not ask keeps 12.
run "a_service_that_sets_a_startup_failure_threshold_gets_it_and_no_other_does" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  variables {
    services = {
      "swarm-api" = {
        service_account_email     = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
        image                     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api:test"
        max_instances             = 20
        startup_failure_threshold = 36
      }
      "swarm-scheduler" = {
        service_account_email = "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
        image                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler:test"
        max_instances         = 3
        concurrency           = 1
      }
    }
  }

  # MUTATION: ignore each.value.startup_failure_threshold in the module (keep
  # the fixed 12) and this fails.
  assert {
    condition     = google_cloud_run_v2_service.this["swarm-api"].template[0].containers[0].startup_probe[0].failure_threshold == 36
    error_message = "swarm-api's startup_failure_threshold must reach the revision's startup probe: a healthy 61-77 s start is otherwise killed near 96 s"
  }

  # MUTATION: change the attribute's default from 12 to 36 and this fails.
  assert {
    condition     = google_cloud_run_v2_service.this["swarm-scheduler"].template[0].containers[0].startup_probe[0].failure_threshold == 12
    error_message = "a service that sets no startup_failure_threshold must keep the module's 12"
  }
}

# The default, over the four services of the file-level variables, none of
# which sets a threshold: every one keeps 12, polled every 5 s.
run "every_service_that_sets_no_threshold_keeps_twelve" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  # MUTATION: change the attribute's default from 12 to anything else, or the
  # probe's period from 5 s, and this fails.
  assert {
    condition = alltrue([
      for name, svc in google_cloud_run_v2_service.this :
      svc.template[0].containers[0].startup_probe[0].failure_threshold == 12
      && svc.template[0].containers[0].startup_probe[0].period_seconds == 5
    ])
    error_message = "every service that sets no startup_failure_threshold must keep 12 failures at a 5 s period"
  }
}

# The root sets 36 for swarm-api and for no other service. A test cannot
# address a child module's resources from terraform/infra, so this reads the
# root's text, the way u_bogdan_removed.tftest.hcl reads removed.tf.
run "the_infra_root_sets_36_for_swarm_api_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/modules/cloud_run"
  }

  # MUTATION: drop `startup_failure_threshold = 36` from swarm-api's block in
  # terraform/infra/main.tf, or set any other number, and this fails.
  assert {
    # The module plans swarm-api from the file-level variables: the reference
    # terraform test requires, and a control that this run planned at all.
    condition = contains(keys(google_cloud_run_v2_service.this), "swarm-api") && length(regexall(
      "(?s)\n    \"swarm-api\" = \\{\n.*?\n      startup_failure_threshold = 36\n.*?\n    \"swarm-scheduler\" = \\{",
      file("../../terraform/infra/main.tf"),
    )) == 1
    error_message = "terraform/infra/main.tf must set startup_failure_threshold = 36 in swarm-api's block of module \"cloud_run\""
  }

  # MUTATION: add startup_failure_threshold to any other service's block and
  # this fails.
  assert {
    condition     = contains(keys(google_cloud_run_v2_service.this), "swarm-api") && length(regexall("(?m)^\\s*startup_failure_threshold\\s*=", file("../../terraform/infra/main.tf"))) == 1
    error_message = "only swarm-api sets startup_failure_threshold in terraform/infra/main.tf; every other service keeps the module's 12"
  }
}
