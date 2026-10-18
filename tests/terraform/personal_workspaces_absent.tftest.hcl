# Personal workspaces are not in Terraform (docs/workspaces.md §3.2; owner
# decision WD3, 2026-10-08; lane W4 of #847).
#
# A person's workspace is made by the workspace job and lives outside Terraform
# state, so no plan can change or destroy one. That holds while:
#
#   1. nothing in terraform/ iterates over people -- no variable of
#      workspaces or people, no Firestore data source, no workspace id;
#   2. nothing in terraform/ is authoritative over the bucket's or the
#      project's policy, which personal workers are members of (an
#      `_iam_binding` or `_iam_policy` there would remove them at the next
#      release); and every authoritative SECRET binding is on a secret named for
#      a key of var.tenants, never a person's;
#   3. a person cannot enter var.tenants: terraform/infra refuses a
#      `kind = "user"` tenant whose principal is not a service account, with
#      no exception since lane W9 moved u-bogdan out (§3.3);
#
# and the one Terraform layer that touches personal workers at all -- the
# bootstrap's workspace job -- names no person: its grants are to the deployer,
# to swarm-api over prefixes, and to swarm-workspace-dispatch, which runs the
# job. (Its google_project_iam_audit_config owns Cloud Run's audit config and
# no binding, so no personal worker's membership rides on it.)
#
# The fixture ids are personal-shaped on purpose: `u-fixture-person` is what
# identity.tenant_id_for_user mints for a person, `w-000000` is a workspace id.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  # terraform/infra refuses to plan an image with no digest; these only have to
  # pass its validations (infra_guards.tftest.hcl has the same).
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

  frontend_iap_members = ["domain:example.com"]
}

run "nothing_in_terraform_iterates_over_people_or_owns_their_policies" {
  command = plan

  module {
    source = "./personal_workspaces_inventory"
  }

  assert {
    condition     = output.files_read > 50 && output.tfvars_read > 0
    error_message = "the scan read too few files to mean anything; check the candidate paths in personal_workspaces_inventory/main.tf"
  }

  assert {
    condition     = length(output.people_inputs) == 0
    error_message = "terraform/ declares a variable of workspaces or people, or reads Firestore: ${join(", ", output.people_inputs)}. The list of people lives in Firestore and is read by the workspace job only (docs/workspaces.md §3.2, rule 1)"
  }

  assert {
    condition     = length(output.workspace_ids) == 0
    error_message = "a workspace id is written in terraform/: ${join(", ", output.workspace_ids)}. Personal workspaces are never in Terraform, not even by their opaque id"
  }

  assert {
    condition     = length(output.authoritative) == 0
    error_message = "terraform/ declares an authoritative bucket or project IAM resource: ${join(", ", output.authoritative)}. Personal workers are members of those policies, and an authoritative resource removes them at the next release (docs/workspaces.md §3.2, rule 2)"
  }
}

run "a_service_account_s_tenant_still_can" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      "u-sw-c90291" = {
        kind            = "user"
        principal       = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
        directory_group = false
        providers       = []
        max_active      = 4
      }
      eng = {
        kind      = "group"
        principal = "eng@saga.xyz"
        providers = ["anthropic"]
      }
    }
  }

  assert {
    condition     = toset(keys(output.tenant_namespaces)) == toset(["u-sw-c90291", "eng"])
    error_message = "a service account's user tenant and a group tenant are Terraform's, and still plan"
  }
}

# Every authoritative secret binding is on a secret named for a key of the
# tenants it was given -- never on a personal tenant's secret it was not.
run "authoritative_secret_bindings_are_on_terraform_tenants_secrets_only" {
  command = plan

  module {
    source = "../../terraform/modules/secret_manager"
  }

  variables {
    labels = { "managed-by" = "swarm-terraform" }
    tenant_secrets = {
      eng = {
        providers = ["anthropic", "git"]
        accessor  = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      }
    }
  }

  assert {
    condition = alltrue(concat(
      [for k, b in google_secret_manager_secret_iam_binding.accessor : startswith(b.secret_id, "swarm-tenant-eng-")],
      [for k, b in google_secret_manager_secret_iam_binding.version_adder : startswith(b.secret_id, "swarm-tenant-eng-")],
      [for k, b in google_secret_manager_secret_iam_binding.refresh_accessor : startswith(b.secret_id, "swarm-tenant-eng-")],
    )) && length(google_secret_manager_secret_iam_binding.accessor) > 0
    error_message = "an authoritative per-secret binding is on a secret that is not named for one of the tenants given: it would own a personal workspace's slot and strip the job's grant on it"
  }

  assert {
    condition     = alltrue([for k, b in google_secret_manager_secret_iam_binding.github_app_accessor : !startswith(b.secret_id, "swarm-tenant-u-")])
    error_message = "the GitHub App's platform secrets are not a personal tenant's"
  }
}

# The bootstrap's workspace job is the one layer that touches personal workers,
# and it names none of them.
run "the_workspace_job_s_terraform_names_no_person" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  }

  override_data {
    target = data.google_iam_policy.workspace_deployer_nobody
    values = {
      policy_data = "{}"
    }
  }

  override_data {
    target = data.google_iam_policy.workspace_dispatch_nobody
    values = {
      policy_data = "{}"
    }
  }

  override_data {
    target = data.google_iam_policy.workspace_apply_job
    values = {
      policy_data = "{}"
    }
  }

  assert {
    condition = alltrue(concat(
      [for k, m in google_project_iam_member.workspace_deployer : length(regexall("swarm-agent-worker-u-|u-fixture-person|w-[0-9a-f]{6}", m.member)) == 0],
      [for k, m in google_project_iam_member.forge_personal_slots : m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"],
      [for k, m in google_project_iam_member.forge_personal_slots : length(regexall("u-fixture-person|swarm-tenant-u-[a-z0-9]", m.condition[0].expression)) == 0],
    ))
    error_message = "the workspace job's grants must name no personal worker, tenant or workspace: they are to swarm-workspace-deployer and swarm-api, over the swarm-tenant-u- prefix"
  }

  # The job is declared with a command and NO arguments: the workspace id and
  # the mode arrive with each execution, from the workflow, which reads them
  # from the message. Terraform holds no workspace.
  assert {
    condition = alltrue([
      google_pubsub_topic.workspace_apply[0].name == "swarm-workspace-apply",
      try(length(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].args), 0) == 0,
      try(length(google_cloud_run_v2_job.workspace_apply[0].template[0].template[0].containers[0].env), 0) == 0,
      strcontains(google_workflows_workflow.workspace_apply[0].source_contents, "map.get(message, \"workspace_id\")"),
      length(regexall("w-[0-9a-f]{6}", google_workflows_workflow.workspace_apply[0].source_contents)) == 0,
    ])
    error_message = "which workspace to make reaches the job only in the message, at run time: the job carries no argument or environment, and the workflow reads the id from the message and names none"
  }

  assert {
    condition = alltrue([
      for b in data.google_iam_policy.workspace_apply_job.binding : alltrue([
        for m in b.members : length(regexall("swarm-agent-worker-u-|u-fixture-person|w-[0-9a-f]{6}", m)) == 0
      ])
    ]) && length(regexall("swarm-agent-worker-u-|u-fixture-person", google_project_iam_member.workspace_dispatch_invoker[0].member)) == 0
    error_message = "the job's runner and the workflow invoker are swarm-workspace-dispatch, never a person's worker"
  }
}

# Last: it must FAIL, and a run that errors skips every run after it.
run "a_person_cannot_be_a_terraform_tenant" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    tenants = {
      "u-fixture-person" = {
        kind      = "user"
        principal = "fixture.person@saga.xyz"
      }
    }
  }

  expect_failures = [var.tenants]
}
