# #295, the merge step's identities, RETIRED (owner decision MS0-Q4,
# 2026-10-06; part of #352 box 4 and #295).
#
# The merge runs as the tenant's worker account on its `-git` token (contract
# request 47), and the review's verdict is a file the merge stages, not a
# GitHub review posted by an App (docs/merge-step.md, "Revised 2026-10-06").
# So the per-tenant `swarm-<tenant>-merge`, `-post-verdict` and `-review`
# accounts, their grants, the `-git-merge`/`-git-review` App keys' accessor
# overrides and the `forge` record that fed their Jobs are gone from
# modules/service_account_ids, modules/tenancy and terraform/infra. What this
# suite still holds:
#
#   * no tenant may register git-merge or git-review (#453 box 118), and the
#     platform derives no account beyond the worker for any provider;
#   * the frozen catalogue's post-verdict and claude-code-review entries get
#     no Job for any tenant, and merge's Job runs as the worker account;
#   * the worker's bucket grant is still the read/write pair, unchanged --
#     collapsing it back into one binding is a create;
#   * modules/secret_manager's App-key guards, which it still enforces for
#     any caller that passes an override;
#   * NO TENANT IDENTITY HOLDS run.jobs.run, run.jobs.runWithOverrides OR
#     run.jobs.update on any Job (merge-step.md §1.3).
#
# tests/unit/scripts/test_tenancy_retired_295_identities.py holds that the
# retired resources, locals and outputs are not declared at all, which a plan
# cannot show.

# source: the shared defaults every suite that plans terraform/infra needs
# (mocks/google/kms.tfmock.hcl -- the step-spec key's enabled version 1).
mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  artifact_bucket = "swarm-artifacts-saga-agents-staging"
  labels          = { "managed-by" = "swarm-terraform" }

  # eng holds an agent key and the forge token; research an agent key alone;
  # u-alice nothing.
  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@saga.xyz"
      providers = ["anthropic", "git"]
    }
    research = {
      kind      = "group"
      principal = "research@saga.xyz"
      providers = ["anthropic"]
    }
    "u-alice" = {
      kind      = "user"
      principal = "alice@saga.xyz"
    }
  }

  dispatcher_members = {
    scheduler  = "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"
    reconciler = "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
    deployer   = "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
  }
}

# ---------------------------------------------------------------------------
# The accounts: the worker's, and no other
# ---------------------------------------------------------------------------

run "infra_manages_no_account_beyond_the_platforms_and_the_workers" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids = ["eng", "research"]
  }

  # terraform/bootstrap grants the deployer serviceAccountAdmin on exactly
  # infra_managed. The retired accounts are not in it, so bootstrap's next
  # apply drops the deployer's grant on any it still holds.
  assert {
    condition = output.infra_managed == sort([
      "swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler",
      "swarm-tick", "swarm-verify", "swarm-rollup-sweeper", "swarm-schedule-tick",
      "swarm-agent-worker-eng", "swarm-agent-worker-research",
    ])
    error_message = "infra_managed is the platform accounts and one worker account per tenant, nothing else"
  }
}

run "every_tenant_secret_input_names_the_worker_alone" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  override_resource {
    target          = google_service_account.worker
    override_during = plan
    values = {
      email = "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = (
      toset(keys(output.secret_inputs)) == toset(["eng", "research"])
      && alltrue([
        for t, cfg in output.secret_inputs :
        cfg.accessor == "serviceAccount:swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
        && toset(keys(cfg)) == toset(["providers", "accessor"])
      ])
    )
    error_message = "a tenant's secrets are read by its worker account; no per-provider reader override is sent any more"
  }
}

# ---------------------------------------------------------------------------
# No tenant registers an App provider (#453 box 118)
# ---------------------------------------------------------------------------

run "a_tenant_registering_git_review_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = ["anthropic", "git-review"] }
    }
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  }

  expect_failures = [var.tenants]
}

run "a_tenant_registering_git_merge_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = ["git", "git-merge"] }
    }
    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  }

  expect_failures = [var.tenants]
}

# ---------------------------------------------------------------------------
# The Jobs (terraform/infra)
# ---------------------------------------------------------------------------

run "no_job_for_a_retired_profile_and_merge_runs_as_the_worker" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"

    tenants = {
      eng   = { kind = "group", principal = "eng@saga.xyz", providers = ["anthropic", "git"] }
      smoke = { kind = "group", principal = "swarm-smoke@saga.xyz", providers = ["anthropic"] }
    }

    image_refs = {
      "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
      "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  }

  override_resource {
    target          = module.tenancy.google_service_account.worker
    override_during = plan
    values = {
      email = "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # The frozen catalogue still holds post-verdict and claude-code-review
  # (contract request 50 is the owner's call). eng holds anthropic, so a
  # claude-code-review Job would exist if the profile were not listed in
  # profiles_without_a_job -- and it would run as the worker account.
  assert {
    condition = !anytrue([
      for name in keys(local.jobs) : endswith(name, "-post-verdict") || endswith(name, "-claude-code-review")
    ])
    error_message = "a retired #295 profile got a Cloud Run Job"
  }

  # The control: eng's other anthropic Jobs exist, so the check above is not
  # passing on an empty matrix.
  assert {
    condition     = contains(keys(local.jobs), "swarm-job-eng-claude-code") && contains(keys(local.jobs), "swarm-job-eng-merge")
    error_message = "eng holds anthropic and git, so it has a claude-code and a merge Job"
  }

  assert {
    condition     = !contains(keys(local.jobs), "swarm-job-smoke-merge")
    error_message = "a tenant without the forge token gets no merge Job"
  }

  # Contract request 47: every Job, merge included, runs as the worker account.
  assert {
    condition = alltrue([
      for name, job in local.jobs :
      job.service_account_email == "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "every Job runs as the tenant's worker account"
  }

  # The merge acts on the workflow's own repository_url, so no Job carries a
  # forge record, and the merge mounts no credential: it reads -git at merge
  # time only.
  assert {
    condition = alltrue([
      for name, job in local.jobs :
      length([for k in keys(job.env) : k if startswith(k, "FORGE_") || startswith(k, "REVIEW_APP_")]) == 0
    ])
    error_message = "no Job carries a forge record"
  }

  assert {
    condition     = length(local.jobs["swarm-job-eng-merge"].secret_env) == 0
    error_message = "the merge's -git token is never mounted into its Job's environment"
  }
}

# ---------------------------------------------------------------------------
# The storage split (merge-step.md §4.3), kept as it is
# ---------------------------------------------------------------------------

run "no_worker_can_write_a_verdict" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # Read and list stay exactly as tenant-scoped as the old binding was: both
  # of its clauses, on objectViewer.
  assert {
    condition = (
      google_storage_bucket_iam_member.worker_objects_read["eng"].role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.worker_objects_read["eng"].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/\") || api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"tenants/eng/\")"
    )
    error_message = "the worker's read grant is objectViewer on its own prefix, with the objectListPrefix clause that keeps listing tenant-scoped"
  }

  # Write is the old grant minus verdicts/, and lists nothing on its own.
  assert {
    condition = (
      google_storage_bucket_iam_member.worker_objects_write["eng"].role == "roles/storage.objectUser"
      && google_storage_bucket_iam_member.worker_objects_write["eng"].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/\") && !resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/verdicts/\")"
    )
    error_message = "the worker's write grant is unchanged: its own prefix, less tenants/<tenant>/verdicts/"
  }

  assert {
    condition = alltrue([
      for k, b in google_storage_bucket_iam_member.worker_objects_write : !strcontains(b.condition[0].expression, "objectListPrefix")
    ])
    error_message = "the write grant carries no list clause of its own; listing is the read grant's"
  }

  # The two condition titles differ, so neither binding replaces the other in
  # the bucket policy.
  assert {
    condition     = google_storage_bucket_iam_member.worker_objects_read["eng"].condition[0].title != google_storage_bucket_iam_member.worker_objects_write["eng"].condition[0].title
    error_message = "the read and write bindings need distinct condition titles"
  }

  assert {
    condition     = length(google_storage_bucket_iam_member.worker_objects_read) == 3 && length(google_storage_bucket_iam_member.worker_objects_write) == 3
    error_message = "every tenant's worker gets both halves"
  }
}

# ---------------------------------------------------------------------------
# modules/secret_manager's App-key guards (contract request 35, MAJOR 1). No
# caller passes an override any more; the module still refuses a worker on an
# App key for any caller that does.
# ---------------------------------------------------------------------------

run "a_provider_override_is_the_complete_reader_list" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    enable_subscription_refresh = true
    refresher_member            = "serviceAccount:swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com"

    tenant_secrets = {
      eng = {
        providers = ["anthropic", "git-review", "git-merge"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
        accessor_overrides = {
          "git-merge"  = ["serviceAccount:swarm-eng-merge@saga-agents-staging.iam.gserviceaccount.com"]
          "git-review" = ["serviceAccount:swarm-eng-post-verdict@saga-agents-staging.iam.gserviceaccount.com"]
        }
      }
    }
  }

  assert {
    condition     = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-git-merge"].members == toset(["serviceAccount:swarm-eng-merge@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "the authoritative accessor binding on -git-merge must name the merge account alone"
  }

  assert {
    condition     = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-git-review"].members == toset(["serviceAccount:swarm-eng-post-verdict@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "the authoritative accessor binding on -git-review must name the post-verdict account alone"
  }

  # The control: a provider with no override still falls back to the worker.
  assert {
    condition     = google_secret_manager_secret_iam_binding.accessor["swarm-tenant-eng-anthropic"].members == toset(["serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"])
    error_message = "a provider with no override is read by the tenant's worker account"
  }

  # Neither App key gets a -refresh twin, and the refresher gets no write on
  # either; anthropic, the control, still does.
  assert {
    condition = (
      contains(keys(google_secret_manager_secret.refresh), "swarm-tenant-eng-anthropic-refresh")
      && !contains(keys(google_secret_manager_secret.refresh), "swarm-tenant-eng-git-merge-refresh")
      && !contains(keys(google_secret_manager_secret.refresh), "swarm-tenant-eng-git-review-refresh")
    )
    error_message = "git-merge and git-review must never get a -refresh twin; anthropic still must"
  }

  assert {
    condition = (
      !contains(keys(google_secret_manager_secret_iam_binding.version_adder), "swarm-tenant-eng-git-merge")
      && !contains(keys(google_secret_manager_secret_iam_binding.version_adder), "swarm-tenant-eng-git-review")
      && contains(google_secret_manager_secret_iam_binding.version_adder["swarm-tenant-eng-anthropic"].members, "serviceAccount:swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com")
    )
    error_message = "the refresher may write anthropic's secret, never an App key's"
  }
}

run "an_app_key_with_no_override_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers = ["git-merge"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
  }

  expect_failures = [var.tenant_secrets]
}

run "an_app_key_read_by_the_worker_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    tenant_secrets = {
      eng = {
        providers = ["git-review"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
        accessor_overrides = {
          "git-review" = [
            "serviceAccount:swarm-eng-post-verdict@saga-agents-staging.iam.gserviceaccount.com",
            "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com",
          ]
        }
      }
    }
  }

  expect_failures = [var.tenant_secrets]
}

# ---------------------------------------------------------------------------
# No tenant identity holds run.jobs.run, run.jobs.runWithOverrides or
# run.jobs.update on any Job (merge-step.md §1.3)
# ---------------------------------------------------------------------------

# 1. Which roles carry those permissions. Only the dispatcher's: a new role, or
#    an old one gaining one of them, changes who must be checked below.
run "only_the_dispatcher_role_can_run_or_update_a_job" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = toset([
      for k, r in google_project_iam_custom_role.platform : k
      if length(setintersection(toset(r.permissions), toset(["run.jobs.run", "run.jobs.runWithOverrides", "run.jobs.update"]))) > 0
    ]) == toset(["job_dispatcher"])
    error_message = "a custom role other than swarmJobDispatcher carries run.jobs.run, runWithOverrides or update; this suite's check of who holds them no longer covers every holder"
  }
}

# 2. Who holds that role, and every predefined run.* role. Platform identities
#    only, and of them only the dispatcher.
run "only_the_scheduler_holds_a_role_that_runs_or_updates_a_job" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  variables {
    gke_enabled      = true
    gke_cluster_name = "swarm-dev-autopilot"
    gke_location     = "us-central1"
    artifact_bucket  = "swarm-artifacts-saga-agents-staging"
  }

  assert {
    condition = alltrue([
      for account, roles in output.granted_roles :
      account == "swarm-scheduler" || alltrue([
        for r in roles : r != "projects/saga-agents-staging/roles/swarmJobDispatcher" && !startswith(r, "roles/run.")
      ])
    ])
    error_message = "a platform identity other than swarm-scheduler holds swarmJobDispatcher or a predefined run.* role"
  }

  # The control: the scheduler does hold it, so the check above is reading the
  # grants and not an empty map.
  assert {
    condition     = contains(output.granted_roles["swarm-scheduler"], "projects/saga-agents-staging/roles/swarmJobDispatcher")
    error_message = "swarm-scheduler must hold swarmJobDispatcher; without it nothing dispatches"
  }
}

# 3. No tenant identity is granted any of it.
run "no_tenant_identity_can_run_update_or_act_as_a_job" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # Every project role a tenant identity holds: the narrowed Firestore role
  # and the three telemetry roles. None carries run.*.
  assert {
    condition = alltrue([
      for r in concat(
        [for k, b in google_project_iam_member.worker_firestore : b.role],
        [for k, b in google_project_iam_member.worker_telemetry : b.role],
      ) :
      contains([
        "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore",
        "roles/logging.logWriter",
        "roles/monitoring.metricWriter",
        "roles/cloudtrace.agent",
      ], r)
    ])
    error_message = "a tenant identity holds a project role beyond Firestore and telemetry; run.jobs.run, runWithOverrides or update could be among it"
  }

  # The control: the list is not empty.
  assert {
    condition     = length(google_project_iam_member.worker_firestore) == 3 && length(google_project_iam_member.worker_telemetry) == 9
    error_message = "each of the three tenants' workers must hold the Firestore role and the three telemetry roles, and this check must have read them"
  }

  # actAs on a worker account is the dispatchers' and the deployer's, and
  # names no tenant identity.
  assert {
    condition = alltrue([
      for k, b in google_service_account_iam_member.act_as : contains(values(var.dispatcher_members), b.member)
    ])
    error_message = "actAs on a worker account is the dispatchers' and the deployer's, nobody else's"
  }
}
