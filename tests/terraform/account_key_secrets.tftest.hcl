# The quota broker destroys a deleted API key's secret at once, and can
# delete no other secret (docs/design/llm-providers.md §4.4 and §8.3; owner
# decision Q3, 9 November 2026; lane LP-T1).
# terraform/bootstrap/account_key_secrets.tf makes the role and the grant.
# These runs hold:
#
#   * swarmAccountKeyDeleter carries secretmanager.secrets.delete and nothing
#     else;
#   * swarm-quota-broker alone holds it, through one grant with one condition:
#     a name starting `projects/<NUMBER>/secrets/swarm-account-` with a
#     non-empty tenant before `--key-`, by extract() and never matches() or a
#     list;
#   * the condition, EVALUATED, admits every tenant's key secret and refuses a
#     plan account's secret and its -refresh half, the pre-design Voice it
#     key's `--<label>` secret, every swarm-tenant-* secret, the other team's
#     agents-* and peer-app-*, another project's and the project-ID spelling;
#   * terraform/modules/iam, which CI applies, grants the role to nobody.
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
  project_id = "example-project"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]

  # modules/iam's.
  artifact_bucket  = "swarm-artifacts-example-project"
  labels           = { "managed-by" = "swarm-terraform" }
  gke_cluster_name = "swarm-autopilot"
  gke_location     = "us-central1"
}

run "the_deleter_role_carries_secrets_delete_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = google_project_iam_custom_role.account_key_deleter.permissions == toset(["secretmanager.secrets.delete"])
    error_message = "swarmAccountKeyDeleter must carry secretmanager.secrets.delete and nothing else: no payload read, no version, no IAM"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.account_key_deleter.role_id == "swarmAccountKeyDeleter",
      google_project_iam_custom_role.account_key_deleter.project == "example-project",
      google_project_iam_custom_role.account_key_deleter.stage == "GA",
    ])
    error_message = "the role is swarmAccountKeyDeleter, GA, in this project"
  }

  assert {
    condition     = startswith(google_project_iam_custom_role.account_key_deleter.description, "managed-by=swarm-terraform;")
    error_message = "a custom role has no labels; it says managed-by=swarm-terraform in its description"
  }
}

run "the_broker_alone_deletes_key_secrets_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.account_key_secrets
    override_during = plan
    values = {
      number = "123456789012"
    }
  }

  # A mock account's member is a random string; the broker's is given its own,
  # so the member below matches only if it is the broker's.
  override_resource {
    target          = google_service_account.platform["swarm-quota-broker"]
    override_during = plan
    values = {
      email  = "swarm-quota-broker@example-project.iam.gserviceaccount.com"
      member = "serviceAccount:swarm-quota-broker@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = alltrue([
      google_project_iam_member.account_key_deleter.project == "example-project",
      google_project_iam_member.account_key_deleter.member == "serviceAccount:swarm-quota-broker@example-project.iam.gserviceaccount.com",
      google_project_iam_member.account_key_deleter.role == "projects/example-project/roles/swarmAccountKeyDeleter",
    ])
    error_message = "swarm-quota-broker, alone, holds swarmAccountKeyDeleter, granted through the role resource"
  }

  # Dropped, the grant would delete every secret in the shared project, the
  # other team's among them.
  assert {
    condition     = length(google_project_iam_member.account_key_deleter.condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, secrets.delete reaches every secret in example-project"
  }

  assert {
    condition     = strcontains(google_project_iam_member.account_key_deleter.condition[0].description, "managed-by=swarm-terraform")
    error_message = "an IAM member has no labels; its condition's description says managed-by=swarm-terraform"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  assert {
    condition     = google_project_iam_member.account_key_deleter.condition[0].expression == "resource.name.startsWith(\"projects/123456789012/secrets/swarm-account-\") && resource.name.extract(\"/secrets/swarm-account-{t}--key-\") != \"\""
    error_message = "the broker's key-secret delete condition changed; it must be a name starting projects/<NUMBER>/secrets/swarm-account- with a non-empty tenant before --key-, by extract(), and nothing else"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", " || ", "example-project", "swarm-tenant-"] :
      !strcontains(google_project_iam_member.account_key_deleter.condition[0].expression, f)
    ])
    error_message = "the condition uses no matches(), no hasOnly() list, no second arm, no swarm-tenant- arm, and the project number rather than its ID"
  }

  assert {
    condition     = output.account_key_deleter_grant == google_project_iam_member.account_key_deleter.condition[0].expression
    error_message = "the output the owner reads a plan against and lints must be the condition the grant carries"
  }

  # EVALUATED, as mcp_credentials.tftest.hcl evaluates its own: the expression
  # is parsed into its startsWith() literal and its extract() template, and
  # each name is judged by them -- startsWith() as startswith(),
  # extract("pre{t}suf") as the text between the FIRST pre and the first suf
  # after it, "" when either is missing. Because the prefix and the template
  # are READ from the expression, widening either changes the verdicts below.
  #
  # Not listed either way: a plan label beginning `key-` or holding `--key-`,
  # the residual the broker refuses at registration (the header).
  assert {
    condition = alltrue([
      for c in [{
        parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.account_key_deleter.condition[0].expression)
        admitted = [
          "projects/123456789012/secrets/swarm-account-a--key-main",
          "projects/123456789012/secrets/swarm-account-eng--key-team-openai",
          "projects/123456789012/secrets/swarm-account-u-alice--key-personal",
          "projects/123456789012/secrets/swarm-account-someone-not-in-terraform--key-x",
        ]
        refused = [
          "projects/123456789012/secrets/swarm-account-a--work",
          "projects/123456789012/secrets/swarm-account-a--work-refresh",
          "projects/123456789012/secrets/swarm-account-a--voice-it",
          "projects/123456789012/secrets/swarm-account-a-key-main",
          "projects/123456789012/secrets/swarm-account---key-main",
          "projects/123456789012/secrets/swarm-account-",
          "projects/123456789012/secrets/swarm-tenant-a--key-main",
          "projects/123456789012/secrets/swarm-tenant-a-anthropic",
          "projects/123456789012/secrets/swarm-tenant-a-openai",
          "projects/123456789012/secrets/swarm-tenant-a-git",
          "projects/123456789012/secrets/swarm-github-app-private-key",
          "projects/123456789012/secrets/agents-a--key-main",
          "projects/123456789012/secrets/peer-app-openai",
          "projects/210987654321/secrets/swarm-account-a--key-main",
          "projects/${"123456789012"}0/secrets/swarm-account-a--key-main",
          "projects/example-project/secrets/swarm-account-a--key-main",
        ]
        }] : alltrue([
        for n in concat(c.admitted, c.refused) : (
          startswith(n, c.parts[0]) &&
          length(split(c.parts[1], n)) > 1 &&
          length(split(c.parts[2], join(c.parts[1], slice(split(c.parts[1], n), 1, length(split(c.parts[1], n)))))) > 1 &&
          split(c.parts[2], join(c.parts[1], slice(split(c.parts[1], n), 1, length(split(c.parts[1], n)))))[0] != ""
        ) == contains(c.admitted, n)
      ])
    ])
    error_message = "the broker's key-secret delete condition must admit every tenant's swarm-account-<t>--key-<label> secret and refuse a plan's secret and its -refresh half, the pre-design Voice it key, an empty tenant, every swarm-tenant-* secret, the platform's swarm-github-app-*, the other team's agents-* and peer-app-*, another project's and the project-ID spelling"
  }

  # The control for the emulation above: the same parse finds a tenant in an
  # admitted name, and none in a plan's, so the verdicts compare something
  # rather than two empty lists.
  assert {
    condition = alltrue([
      for c in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.account_key_deleter.condition[0].expression)] :
      c[0] == "projects/123456789012/secrets/swarm-account-" && c[1] == "/secrets/swarm-account-" && c[2] == "--key-" &&
      split(c[2], split(c[1], "projects/123456789012/secrets/swarm-account-u-alice--key-x")[1])[0] == "u-alice" &&
      length(split(c[2], split(c[1], "projects/123456789012/secrets/swarm-account-u-alice--work")[1])) == 1
    ])
    error_message = "the condition's parts are the project-number prefix, the /secrets/swarm-account- template prefix and the --key- mark; the parse recovers u-alice from a key's secret and finds no mark in a plan's"
  }
}

# CI applies terraform/modules/iam. A project grant of this role made there
# would need the role on deployer_grantable_project_roles, whose hasOnly()
# bounds neither the member nor the condition: CI could grant itself delete
# over every secret in the shared project.
run "ci_grants_the_deleter_role_to_nobody" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  assert {
    condition = alltrue([
      for account, roles in output.granted_roles : alltrue([
        for role in roles : !strcontains(role, "swarmAccountKeyDeleter")
      ])
    ])
    error_message = "terraform/modules/iam grants swarmAccountKeyDeleter; the grant is terraform/bootstrap/account_key_secrets.tf's alone, so CI never modifies that role's members"
  }

  # Read as text, every root and module file CI applies, so a grant added in a
  # new resource or another file is found too; a comment may name the role.
  # The control is the module's own broker secret grant, so the scan read the
  # module it was meant to, and the file count, so it read files at all.
  assert {
    condition = alltrue([
      google_project_iam_member.broker_version_adder.role == "roles/secretmanager.secretVersionAdder",
      contains(fileset("../../terraform/modules/iam", "*.tf"), "custom_roles.tf"),
      length(fileset("../../terraform/infra", "*.tf")) > 10,
      alltrue([
        for f in concat(
          [for p in fileset("../../terraform/modules/iam", "*.tf") : "../../terraform/modules/iam/${p}"],
          [for p in fileset("../../terraform/infra", "*.tf") : "../../terraform/infra/${p}"],
          ) : !anytrue([
            for line in split("\n", file(f)) : strcontains(line, "swarmAccountKeyDeleter") && !startswith(trimspace(line), "#")
        ])
      ]),
    ])
    error_message = "a file CI applies (terraform/modules/iam or terraform/infra) names swarmAccountKeyDeleter outside a comment, or the scan read no files; the role is defined and granted by terraform/bootstrap/account_key_secrets.tf alone"
  }
}
