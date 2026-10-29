# swarm-api reads the METADATA of swarm-* secrets for Admin › Platform secrets,
# and never a value (docs/design/secrets-scopes.md §5.2, BGD's Q7 answer in
# §11; lane SSC-PLATFORM-STATUS, part of #2158).
# terraform/bootstrap/platform_secret_status.tf makes the grant. These runs
# hold:
#
#   * swarmSecretStatusViewer carries secretmanager.secrets.get and
#     secretmanager.versions.list and nothing else: no versions.access, no
#     versions.get, no secrets.list, no IAM, no write;
#   * swarm-api alone holds it, through one grant with one condition: a name
#     starting `projects/<NUMBER>/secrets/swarm-`;
#   * the number comes from data.google_project, never a literal: a second
#     project number flows straight into the condition, and the file spells
#     no number;
#   * the condition, EVALUATED, admits the platform's swarm-* secrets and the
#     broker's swarm-account-* ones, and refuses the other team's agents-* and
#     promptlab-* secrets, another project's, and the project-ID spelling --
#     so a dropped or widened condition fails here;
#   * no secret or version is declared, and every resource says
#     managed-by=swarm-terraform;
#   * nothing exists until enable_platform_secret_status is set.
#
# None of this proves what IAM does live; see the file's "NOT VERIFIED LIVE".

mock_provider "google" {
  source = "./mocks/google"
}

# terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1):
# a mock provider cannot import, so the bucket's provider values are the
# override's and its configuration is still planned and asserted.
override_resource {
  target = google_storage_bucket.state
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  infra_tenants_tfvars = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
}

run "the_status_role_holds_secrets_get_and_versions_list_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.platform_secret_status
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_platform_secret_status = true
  }

  assert {
    condition     = google_project_iam_custom_role.secret_status_viewer[0].permissions == toset(["secretmanager.secrets.get", "secretmanager.versions.list"])
    error_message = "swarmSecretStatusViewer holds exactly secretmanager.secrets.get and secretmanager.versions.list: no versions.access (a value), no versions.get, no secrets.list (enumerating the shared project), no IAM, no write"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.secret_status_viewer[0].role_id == "swarmSecretStatusViewer",
      google_project_iam_custom_role.secret_status_viewer[0].project == "saga-agents-staging",
      google_project_iam_custom_role.secret_status_viewer[0].stage == "GA",
    ])
    error_message = "the role is swarmSecretStatusViewer, defined in this project, GA"
  }
}

run "swarm_api_alone_reads_swarm_secret_metadata_by_project_number" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.platform_secret_status
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_platform_secret_status = true
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.secret_status_viewer) == 1,
      google_project_iam_member.secret_status_viewer[0].project == "saga-agents-staging",
      google_project_iam_member.secret_status_viewer[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.secret_status_viewer[0].role == "projects/saga-agents-staging/roles/swarmSecretStatusViewer",
    ])
    error_message = "swarm-api, alone, holds swarmSecretStatusViewer, granted through the role resource"
  }

  # Dropped, the grant would read the metadata of every secret in the shared
  # project, the other team's among them.
  assert {
    condition     = length(google_project_iam_member.secret_status_viewer[0].condition) == 1
    error_message = "the grant carries exactly one condition"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  assert {
    condition     = google_project_iam_member.secret_status_viewer[0].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-\")"
    error_message = "the condition is a name starting projects/<NUMBER>/secrets/swarm- and nothing else"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", " || ", "saga-agents-staging", "agents-", "promptlab"] :
      !strcontains(google_project_iam_member.secret_status_viewer[0].condition[0].expression, f)
    ])
    error_message = "the condition uses no matches(), no list, no second arm, names no other family, and spells the project number rather than its ID"
  }

  assert {
    condition     = output.platform_secret_status_grants.api_metadata == google_project_iam_member.secret_status_viewer[0].condition[0].expression
    error_message = "the output the owner reads a plan against and lints must be the condition the grant carries"
  }

  # EVALUATED. The startsWith() literal is READ from the expression and each
  # name judged by it, so widening the prefix (`swarm`, `projects/2090`) or
  # dropping the project changes these verdicts and fails here.
  assert {
    condition = alltrue([
      for c in [{
        prefix = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\)$", google_project_iam_member.secret_status_viewer[0].condition[0].expression)[0]
        admitted = [
          "projects/209012342332/secrets/swarm-github-app-private-key",
          "projects/209012342332/secrets/swarm-live-key",
          "projects/209012342332/secrets/swarm-tailscale-oauth",
          "projects/209012342332/secrets/swarm-account-u-alice--main",
          "projects/209012342332/secrets/swarm-account-u-alice--main-refresh",
        ]
        refused = [
          "projects/209012342332/secrets/agents-staging-anthropic",
          "projects/209012342332/secrets/agents-staging-db-password",
          "projects/209012342332/secrets/promptlab-openai",
          "projects/209012342332/secrets/swarmish-key",
          "projects/209012342332/secrets/swarm",
          "projects/111111111111/secrets/swarm-live-key",
          "projects/2090123423320/secrets/swarm-live-key",
          "projects/20901234233/secrets/swarm-live-key",
          "projects/saga-agents-staging/secrets/swarm-live-key",
        ]
      }] : alltrue([for n in concat(c.admitted, c.refused) : startswith(n, c.prefix) == contains(c.admitted, n)])
    ])
    error_message = "the condition must admit this project's swarm-* secrets (the platform's and the broker's accounts) and refuse the other team's agents-* and promptlab-*, a name that only begins `swarm` without the hyphen, another project's number, and the project-ID spelling"
  }

  # A custom role has no labels and an IAM member has none either: each says
  # managed-by=swarm-terraform in its description, as the destroy guard reads.
  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.secret_status_viewer[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_member.secret_status_viewer[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "the role and the grant's condition say managed-by=swarm-terraform in their descriptions"
  }

  # Nothing here declares a secret, a version or a per-secret binding.
  assert {
    condition = var.enable_platform_secret_status && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version|_iam_[a-z]+)?\"",
      file("../../terraform/bootstrap/platform_secret_status.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/platform_secret_status.tf"))) == 0
    error_message = "platform_secret_status.tf declares no secret, no version and no per-secret IAM"
  }
}

# The number is the data source's: another project's number flows straight
# into the condition, and the file spells no project number of its own.
run "the_project_number_comes_from_the_data_source_never_a_literal" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.platform_secret_status
    override_during = plan
    values = {
      number = "555555555555"
    }
  }

  variables {
    enable_platform_secret_status = true
  }

  assert {
    condition     = google_project_iam_member.secret_status_viewer[0].condition[0].expression == "resource.name.startsWith(\"projects/555555555555/secrets/swarm-\")"
    error_message = "the condition's project number is data.google_project's, whatever it is"
  }

  assert {
    condition     = var.enable_platform_secret_status && length(regexall("projects/[0-9]{6,}", file("../../terraform/bootstrap/platform_secret_status.tf"))) == 0
    error_message = "platform_secret_status.tf spells no project number: IAM names a secret by number, and a literal would be the wrong project's the day the root is reused"
  }
}

run "platform_secret_status_is_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      !var.enable_platform_secret_status,
      length(google_project_iam_custom_role.secret_status_viewer) == 0,
      length(google_project_iam_member.secret_status_viewer) == 0,
      length(data.google_project.platform_secret_status) == 0,
      output.platform_secret_status_grants.api_metadata == "",
    ])
    error_message = "enable_platform_secret_status defaults to false and, false, defines and grants nothing"
  }
}
