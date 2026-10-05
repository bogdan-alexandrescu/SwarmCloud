# #295, the merge step's identities (docs/merge-step.md §1.3, §4.3, §10 item 4;
# contract requests 33, 35 and 36).
#
# Three per-tenant accounts, three Jobs, and the grants that make them worth
# having:
#
#   * `swarm-<tenant>-merge` is the ONLY reader of `-git-merge`, and
#     `swarm-<tenant>-post-verdict` the ONLY reader of `-git-review`. The
#     tenant's worker account -- whose token any of its agents can mint --
#     reads neither, and the quota broker's refresher touches neither;
#   * `swarm-<tenant>-review` runs the review agent and is the only identity
#     that can create under tenants/<tenant>/verdicts/. The worker account's
#     old objectUser grant on the whole prefix is replaced by a read grant and
#     a write grant that excludes verdicts/;
#   * NO TENANT IDENTITY HOLDS run.jobs.run, run.jobs.runWithOverrides OR
#     run.jobs.update on these Jobs (merge-step.md §1.3). A tenant that could
#     invoke or override its merge Job could run the merge account's code on
#     its own terms, which defeats the separate identity entirely. The last
#     runs hold it from both ends: which roles carry those permissions
#     (terraform/bootstrap), who holds those roles (modules/iam), and that no
#     tenant identity is granted any of them, or actAs on these accounts
#     (modules/tenancy).
#
# Nothing here enables the merge step: the profiles stay `available=False`
# until #342 is enforced and the owner creates the review and merge Apps, and
# no tfvars tenant registers git-merge or git-review.

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

  # eng registers both App providers; research only an agent key; u-alice
  # nothing. The App ids are GitHub's integer shapes, not credentials.
  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@saga.xyz"
      providers = ["anthropic", "git-review", "git-merge"]
      forge = {
        owner             = "saga-xyz"
        repo              = "agent-swarm-infra"
        review_app_id     = 1000001
        review_app_bot_id = 2000002
      }
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

  action_act_as_members = {
    deployer = "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
  }
}

# ---------------------------------------------------------------------------
# The accounts
# ---------------------------------------------------------------------------

run "the_three_accounts_exist_only_where_their_provider_does" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  assert {
    condition     = toset(keys(google_service_account.action)) == toset(["eng:merge", "eng:post-verdict", "eng:claude-code-review"])
    error_message = "a tenant registering git-merge and git-review gets a merge, a post-verdict and a review account, and a tenant registering neither gets none"
  }

  assert {
    condition = (
      google_service_account.action["eng:merge"].account_id == "swarm-eng-merge"
      && google_service_account.action["eng:post-verdict"].account_id == "swarm-eng-post-verdict"
      && google_service_account.action["eng:claude-code-review"].account_id == "swarm-eng-review"
    )
    error_message = "the accounts are swarm-<tenant>-merge, -post-verdict and -review (merge-step.md §1.3)"
  }

  assert {
    condition = alltrue([
      for k, sa in google_service_account.action : strcontains(sa.description, "managed-by=swarm-terraform")
    ])
    error_message = "every account carries the destroy guard's marker"
  }

  # Adopted like the worker: the registration creates it before the release,
  # because the deployer's actAs is a setIamPolicy bootstrap must grant first.
  assert {
    condition     = alltrue([for k, sa in google_service_account.action : sa.create_ignore_already_exists == true])
    error_message = "the #295 accounts must adopt the account the registration created before the release, or that release fails on a 409"
  }
}

run "the_action_accounts_are_in_infra_managed" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids       = ["eng", "research"]
    tenant_providers = { eng = ["anthropic", "git-review", "git-merge"], research = ["anthropic"] }
  }

  # terraform/bootstrap grants the deployer serviceAccountAdmin on exactly
  # infra_managed, so an account missing here is one whose actAs the release
  # cannot set. This holds the module's half only: bootstrap must also pass
  # tenant_providers, which it does not yet, before any tenant registers
  # git-merge or git-review.
  assert {
    condition = alltrue([
      for id in ["swarm-eng-merge", "swarm-eng-post-verdict", "swarm-eng-review"] : contains(output.infra_managed, id)
    ])
    error_message = "the #295 accounts must be in infra_managed, the list bootstrap grants the deployer on"
  }

  assert {
    condition     = length([for id in output.infra_managed : id if startswith(id, "swarm-research-")]) == 0
    error_message = "a tenant registering no App provider gets no #295 account"
  }

  # The review account's agent reads its profile's provider key; the mirror of
  # that profile in terraform/infra says which. Held here so the two cannot
  # drift: claude-code-review's provider is anthropic.
  assert {
    condition     = toset(output.action_accounts["claude-code-review"].also_reads) == toset(["anthropic"])
    error_message = "the review account must read the claude-code-review profile's provider key, anthropic"
  }
}

run "a_tenant_key_at_the_limit_still_fits_the_post_verdict_account" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids       = ["abcdefghijk"]
    tenant_providers = { abcdefghijk = ["git-review"] }
  }

  assert {
    condition     = output.action_ids["abcdefghijk:post-verdict"] == "swarm-abcdefghijk-post-verdict" && length(output.action_ids["abcdefghijk:post-verdict"]) == 30
    error_message = "swarm-<11-character tenant>-post-verdict is 30 characters, IAM's limit"
  }
}

# ---------------------------------------------------------------------------
# The storage split (merge-step.md §4.3)
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
    error_message = "the worker's write grant must exclude tenants/<tenant>/verdicts/: a worker that can write there can forge the review a merge trusts"
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
    error_message = "every tenant's worker gets both halves, App providers or not"
  }
}

run "only_the_review_account_creates_a_verdict_and_it_can_only_create" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  assert {
    condition     = toset(keys(google_storage_bucket_iam_member.review_verdicts)) == toset(["eng:claude-code-review"])
    error_message = "the review account, and only it, gets the verdicts/ grant"
  }

  # Create only: no update, no delete (owner decision, round 5).
  assert {
    condition = (
      google_storage_bucket_iam_member.review_verdicts["eng:claude-code-review"].role == "roles/storage.objectCreator"
      && google_storage_bucket_iam_member.review_verdicts["eng:claude-code-review"].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/eng/verdicts/\")"
    )
    error_message = "the review account's verdict grant is objectCreator on tenants/<tenant>/verdicts/ and nothing wider"
  }

  # The review agent's own artifacts and checkpoints: the same pair as any
  # agent-running profile.
  assert {
    condition = (
      toset(keys(google_storage_bucket_iam_member.review_objects_write)) == toset(["eng:claude-code-review"])
      && google_storage_bucket_iam_member.review_objects_write["eng:claude-code-review"].condition[0].expression == google_storage_bucket_iam_member.worker_objects_write["eng"].condition[0].expression
    )
    error_message = "the review account writes its own artifacts under the same verdicts-excluding condition as the worker"
  }

  # merge and post-verdict run no agent and write nothing: read alone.
  assert {
    condition = (
      toset(keys(google_storage_bucket_iam_member.action_objects_read)) == toset(["eng:merge", "eng:post-verdict", "eng:claude-code-review"])
      && alltrue([for k, b in google_storage_bucket_iam_member.action_objects_read : b.role == "roles/storage.objectViewer"])
      && alltrue([for k, b in google_storage_bucket_iam_member.action_objects_read : b.condition[0].expression == google_storage_bucket_iam_member.worker_objects_read["eng"].condition[0].expression])
    )
    error_message = "every #295 account reads its tenant's prefix, and only with objectViewer"
  }
}

# ---------------------------------------------------------------------------
# The App keys (contract request 35, MAJOR 1, and its git-merge twin)
# ---------------------------------------------------------------------------

run "the_app_keys_are_read_by_their_action_account_alone" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # By name first: which account reads which secret.
  assert {
    condition     = output.secret_readers["eng"]["git-merge"] == ["eng:merge"]
    error_message = "the merge account must be the ONLY reader of -git-merge"
  }

  assert {
    condition     = output.secret_readers["eng"]["git-review"] == ["eng:post-verdict"]
    error_message = "the post-verdict account must be the ONLY reader of -git-review; the review agent's account must not hold the App key"
  }

  assert {
    condition     = output.secret_readers["eng"]["anthropic"] == ["worker", "eng:claude-code-review"]
    error_message = "the review agent reads the anthropic key beside the worker, as claude-code does"
  }

  assert {
    condition     = length(output.secret_readers["research"]) == 0 && length(output.secret_readers["u-alice"]) == 0
    error_message = "a tenant with no App provider keeps the worker as its secrets' only reader"
  }
}

# Then as members. The emails are computed, so a plan leaves them unknown;
# these overrides give the worker one known value and every #295 account
# another, which is enough to show the worker is not on an App key.
run "the_worker_account_is_on_no_app_key" {
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

  override_resource {
    target          = google_service_account.action
    override_during = plan
    values = {
      email = "swarm-action-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-action-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = alltrue([
      for p in ["git-merge", "git-review"] :
      output.secret_inputs["eng"].accessor_overrides[p] == ["serviceAccount:swarm-action-mock@saga-agents-staging.iam.gserviceaccount.com"]
    ])
    error_message = "the tenant's worker account must never read an App key: any agent of the tenant can mint its token"
  }

  assert {
    condition     = output.secret_inputs["eng"].accessor == "serviceAccount:swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the control: the worker's member is a different string from the #295 accounts'"
  }

  assert {
    condition     = length(output.secret_inputs["research"].accessor_overrides) == 0
    error_message = "a tenant with no App provider sends no override"
  }
}

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
# The forge record
# ---------------------------------------------------------------------------

run "an_app_provider_without_a_forge_record_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = ["git-review"] }
    }
  }

  expect_failures = [var.tenants]
}

run "a_forge_host_with_a_scheme_or_path_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = ["git-review"]
        forge = {
          host          = "https://api.github.com.example.net/redirect"
          owner         = "saga-xyz"
          repo          = "agent-swarm-infra"
          review_app_id = 1000001
        }
      }
    }
  }

  expect_failures = [var.tenants]
}

run "merge_without_a_review_app_to_trust_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  variables {
    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = ["git-merge"]
        forge     = { owner = "saga-xyz", repo = "agent-swarm-infra" }
      }
    }
  }

  expect_failures = [var.tenants]
}

# ---------------------------------------------------------------------------
# The Jobs (terraform/infra)
# ---------------------------------------------------------------------------

run "each_action_job_runs_as_its_own_account_with_its_forge_record" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment = "dev"

    tenants = {
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = ["anthropic", "git", "git-review", "git-merge"]
        forge = {
          owner             = "saga-xyz"
          repo              = "agent-swarm-infra"
          review_app_id     = 1000001
          review_app_bot_id = 2000002
        }
      }
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
    }
  }

  # Known emails at plan, one for the worker and one for every #295 account,
  # so "runs as the action account and not the worker" is a comparison of two
  # different strings.
  override_resource {
    target          = module.tenancy.google_service_account.worker
    override_during = plan
    values = {
      email = "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = module.tenancy.google_service_account.action
    override_during = plan
    values = {
      email = "swarm-action-mock@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-action-mock@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = alltrue([
      for name in ["swarm-job-eng-merge", "swarm-job-eng-post-verdict", "swarm-job-eng-claude-code-review"] :
      contains(keys(local.jobs), name)
    ])
    error_message = "a tenant registering git and git-review gets the merge, post-verdict and review Jobs"
  }

  assert {
    condition = !anytrue([
      for name in keys(local.jobs) :
      startswith(name, "swarm-job-smoke-") && (endswith(name, "-merge") || endswith(name, "-post-verdict") || endswith(name, "-claude-code-review"))
    ])
    error_message = "a tenant registering neither git nor an App provider gets none of the three Jobs, even holding anthropic"
  }

  assert {
    condition = alltrue([
      for name in ["swarm-job-eng-post-verdict", "swarm-job-eng-claude-code-review"] :
      local.jobs[name].service_account_email == "swarm-action-mock@saga-agents-staging.iam.gserviceaccount.com"
    ])
    error_message = "the post-verdict and review Jobs must each run as their own account, never the tenant's worker account"
  }

  # Contract request 47 (owner, 2026-10-04): the merge reads the tenant's
  # existing -git token, so its Job runs as the worker account, the one that
  # already reads that secret, and not as the retired git-merge account.
  assert {
    condition     = local.jobs["swarm-job-eng-merge"].service_account_email == "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the merge Job runs as the tenant's worker account since contract request 47"
  }

  # The control: every other Job still runs as the worker.
  assert {
    condition = alltrue([
      for name, job in local.jobs :
      job.service_account_email == "swarm-agent-worker-mock@saga-agents-staging.iam.gserviceaccount.com"
      if !contains(["post-verdict", "claude-code-review"], job.runner_profile)
    ])
    error_message = "every other Job runs as the tenant's worker account"
  }

  # The merge acts on the workflow's own repository_url (contract request 47),
  # so its Job carries no forge record: any repository a workflow runs on.
  assert {
    condition = length([
      for k in keys(local.jobs["swarm-job-eng-merge"].env) : k
      if startswith(k, "FORGE_") || startswith(k, "REVIEW_APP_")
    ]) == 0
    error_message = "the merge Job carries no forge record; it reads its repository from the signed spec"
  }

  assert {
    condition = (
      local.jobs["swarm-job-eng-post-verdict"].env["FORGE_HOST"] == "api.github.com"
      && local.jobs["swarm-job-eng-post-verdict"].env["FORGE_OWNER"] == "saga-xyz"
      && local.jobs["swarm-job-eng-post-verdict"].env["FORGE_REPO"] == "agent-swarm-infra"
      && local.jobs["swarm-job-eng-post-verdict"].env["REVIEW_APP_ID"] == "1000001"
      && !contains(keys(local.jobs["swarm-job-eng-post-verdict"].env), "REVIEW_APP_BOT_ID")
    )
    error_message = "the post-verdict Job carries the host, owner, repo and review App id, and not the bot id it has no use for"
  }

  # An agent never sees the forge record: it is what a forged repository_url
  # is checked against, not something an agent should be handed.
  assert {
    condition = alltrue([
      for name in ["swarm-job-eng-claude-code-review", "swarm-job-eng-claude-code"] :
      length([for k in keys(local.jobs[name].env) : k if startswith(k, "FORGE_") || startswith(k, "REVIEW_APP_")]) == 0
    ])
    error_message = "no agent-running Job carries the forge record"
  }

  # The merge and post-verdict Jobs mount no secret: the worker reads its
  # credential at action time (the -git token, the App key).
  assert {
    condition     = length(local.jobs["swarm-job-eng-merge"].secret_env) == 0 && length(local.jobs["swarm-job-eng-post-verdict"].secret_env) == 0
    error_message = "no worker-action credential is ever mounted into a Job's environment"
  }

  assert {
    condition     = local.jobs["swarm-job-eng-claude-code-review"].env["MODEL"] == "claude-opus-5-5"
    error_message = "the review agent runs the same pinned model as claude-code"
  }
}

# ---------------------------------------------------------------------------
# No tenant identity holds run.jobs.run, run.jobs.runWithOverrides or
# run.jobs.update on these Jobs (merge-step.md §1.3)
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

# 3. No tenant identity -- worker, merge, post-verdict or review account -- is
#    granted any of it, and none can act as a #295 account.
run "no_tenant_identity_can_run_update_or_act_as_the_merge_review_or_post_verdict_job" {
  command = plan

  module {
    source = "../../terraform/modules/tenancy"
  }

  # Every project role a tenant identity holds, worker and #295 alike: the
  # narrowed Firestore role and the three telemetry roles. None carries run.*.
  assert {
    condition = alltrue([
      for r in concat(
        [for k, b in google_project_iam_member.worker_firestore : b.role],
        [for k, b in google_project_iam_member.worker_telemetry : b.role],
        [for k, b in google_project_iam_member.action_firestore : b.role],
        [for k, b in google_project_iam_member.action_telemetry : b.role],
      ) :
      contains([
        "projects/saga-agents-staging/roles/swarmTenantWorkerFirestore",
        "roles/logging.logWriter",
        "roles/monitoring.metricWriter",
        "roles/cloudtrace.agent",
      ], r)
    ])
    error_message = "a tenant identity holds a project role beyond Firestore and telemetry; run.jobs.run, runWithOverrides or update on the merge Job could be among it"
  }

  # The control: the #295 accounts are in that list at all.
  assert {
    condition     = length(google_project_iam_member.action_firestore) == 3 && length(google_project_iam_member.action_telemetry) == 9
    error_message = "the three #295 accounts must each hold the Firestore role and the three telemetry roles, and this check must have read them"
  }

  # actAs on a #295 account is the deployer's alone. Not the dispatcher, the
  # reconciler or any tenant identity: actAs plus run.jobs.update is a repoint
  # of the merge Job's image under the merge account.
  assert {
    condition = (
      length(google_service_account_iam_member.action_act_as) == 3
      && alltrue([
        for k, b in google_service_account_iam_member.action_act_as :
        b.member == "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com" && b.role == "roles/iam.serviceAccountUser"
      ])
    )
    error_message = "only the deployer may act as the merge, post-verdict and review accounts"
  }

  # The worker's own actAs list is unchanged, and names no tenant identity.
  assert {
    condition = alltrue([
      for k, b in google_service_account_iam_member.act_as : contains(values(var.dispatcher_members), b.member)
    ])
    error_message = "actAs on a worker account is the dispatchers' and the deployer's, nobody else's"
  }
}
