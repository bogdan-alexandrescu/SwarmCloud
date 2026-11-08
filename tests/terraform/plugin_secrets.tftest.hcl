# swarm-api reads managed plugins' secrets, `swarm-tenant-<tenant>-plg-<16
# hex>`, and their versions, so it can release a plugin's secret to an attempt
# whose signed plugin set names it; no tenant worker reads one
# (docs/design/managed-plugins.md §6.2; part of #2688).
# terraform/bootstrap/plugin_secrets.tf makes the grant. These runs hold:
#
#   * swarm-api alone holds roles/secretmanager.secretAccessor through it,
#     under one condition: a name starting `projects/<NUMBER>/secrets/
#     swarm-tenant-` with a non-empty tenant before `-plg-`, by extract() and
#     never matches() or a list -- the text terraform/infra/plugins.tf's
#     version-add grant carries;
#   * the condition, EVALUATED, admits every workspace's -plg- secrets and
#     their versions and refuses the tenant's forge token and slots, its
#     provider keys, its -wsec- secrets, the platform's secrets, the other
#     team's, another project's and the project-ID spelling -- so a dropped
#     condition, or one widened, fails here;
#   * the file names no worker account, declares no secret or version, and
#     the condition says managed-by=swarm-terraform;
#   * nothing exists until enable_plugin_secrets is set.
#
# The hex of a plugin secret is built at run time (sha256 of a fixed string),
# never written out: nothing here may look like a credential.
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

  infra_tenants_tfvars = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
}

run "swarm_api_alone_reads_plg_secrets_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.plugin_secrets
    override_during = plan
    values = {
      number = "123456789012"
    }
  }

  variables {
    enable_plugin_secrets = true
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.plugin_secret_reader) == 1,
      google_project_iam_member.plugin_secret_reader[0].project == "example-project",
      google_project_iam_member.plugin_secret_reader[0].member == "serviceAccount:swarm-api@example-project.iam.gserviceaccount.com",
      google_project_iam_member.plugin_secret_reader[0].role == "roles/secretmanager.secretAccessor",
    ])
    error_message = "swarm-api, alone, holds roles/secretmanager.secretAccessor for plugin secrets"
  }

  # Dropped, the grant would read every secret in the shared project, the
  # other team's among them.
  assert {
    condition     = length(google_project_iam_member.plugin_secret_reader[0].condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, secretAccessor reads every secret in example-project"
  }

  # The exact string, so any edit is a deliberate one this file must follow:
  # terraform/infra/plugins.tf's plugin_secret_condition, which
  # plugins_infra.tftest.hcl holds to the same text.
  assert {
    condition     = google_project_iam_member.plugin_secret_reader[0].condition[0].expression == "resource.name.startsWith(\"projects/123456789012/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-plg-\") != \"\""
    error_message = "swarm-api's plugin-secret condition changed; it must be a name starting projects/<NUMBER>/secrets/swarm-tenant- with a non-empty tenant before -plg-, by extract(), and nothing else"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", " || ", "example-project"] :
      !strcontains(google_project_iam_member.plugin_secret_reader[0].condition[0].expression, f)
    ])
    error_message = "the condition uses no matches() (refused on some services), no hasOnly() list, no second arm, and the project number rather than its ID"
  }

  # IAM's limits: a title of at most 100 characters, a description of at most
  # 256, at most 12 logical operators (docs/design/day-one/lessons.md, 10).
  assert {
    condition = alltrue([
      google_project_iam_member.plugin_secret_reader[0].condition[0].title == "swarm plugin secrets read",
      length(google_project_iam_member.plugin_secret_reader[0].condition[0].description) <= 256,
      length(regexall("&&|\\|\\|", google_project_iam_member.plugin_secret_reader[0].condition[0].expression)) <= 12,
    ])
    error_message = "the condition's title, description and operator count stay inside IAM's condition limits"
  }

  assert {
    condition     = output.plugin_secret_grants.api_read == google_project_iam_member.plugin_secret_reader[0].condition[0].expression
    error_message = "the output the owner reads a plan against and lints must be the condition the grant carries"
  }

  # EVALUATED, as workspace_secrets.tftest.hcl does for -wsec-: the expression
  # is parsed into its startsWith() literal and its extract() template, and
  # each name is judged by them -- startsWith() as startswith(), and
  # extract("pre{t}suf") as the text between the FIRST pre and the first suf
  # after it, "" when either is missing, which split() reproduces. Widening
  # either part changes the verdicts below and fails here.
  assert {
    condition = alltrue([
      for h in [substr(sha256("managed plugins secret"), 0, 16)] : alltrue([
        for c in [{
          parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.plugin_secret_reader[0].condition[0].expression)
          admitted = [
            "projects/123456789012/secrets/swarm-tenant-a-plg-${h}",
            "projects/123456789012/secrets/swarm-tenant-a-plg-${h}/versions/1",
            "projects/123456789012/secrets/swarm-tenant-a-plg-${h}/versions/latest",
            "projects/123456789012/secrets/swarm-tenant-eng-plg-${h}",
            "projects/123456789012/secrets/swarm-tenant-u-alice-plg-${h}/versions/12",
            "projects/123456789012/secrets/swarm-tenant-u-someone-not-in-terraform-plg-${h}",
          ]
          refused = [
            "projects/123456789012/secrets/swarm-tenant-a-git",
            "projects/123456789012/secrets/swarm-tenant-a-git/versions/1",
            "projects/123456789012/secrets/swarm-tenant-a-git-u-${h}",
            "projects/123456789012/secrets/swarm-tenant-a-git-r-${h}/versions/2",
            "projects/123456789012/secrets/swarm-tenant-a-typesafe",
            "projects/123456789012/secrets/swarm-tenant-a-anthropic",
            "projects/123456789012/secrets/swarm-tenant-a-anthropic-refresh",
            "projects/123456789012/secrets/swarm-tenant-a-wsec-${h}",
            "projects/123456789012/secrets/swarm-tenant-a-wsec-${h}/versions/1",
            "projects/123456789012/secrets/swarm-tenant-u-alice-browser-login-github-qa",
            "projects/123456789012/secrets/swarm-tenant-a-plg",
            "projects/123456789012/secrets/swarm-tenant-plg-${h}",
            "projects/123456789012/secrets/swarm-tenant--plg-${h}",
            "projects/123456789012/secrets/swarm-tenant",
            "projects/123456789012/secrets/swarm-github-app-private-key",
            "projects/123456789012/secrets/swarm-github-app-plg-${h}",
            "projects/123456789012/secrets/swarm-account-a-plg-${h}",
            "projects/123456789012/secrets/peer-cluster-anthropic",
            "projects/123456789012/secrets/agents-a-plg-${h}",
            "projects/123456789012/secrets/peer-app-openai",
            "projects/210987654321/secrets/swarm-tenant-a-plg-${h}",
            "projects/${"123456789012"}0/secrets/swarm-tenant-a-plg-${h}",
            "projects/example-project/secrets/swarm-tenant-a-plg-${h}",
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
    ])
    error_message = "swarm-api's plugin-secret condition must admit every workspace's swarm-tenant-<t>-plg-<hex> secret and its versions, workspaces made outside Terraform included, and refuse the tenant's -git, -typesafe and -wsec- secrets, its forge slots and provider keys, an empty tenant, the platform's swarm-github-app-* and swarm-account-* secrets, the other team's agents-* and peer-app-*, another project's and the project-ID spelling"
  }

  # The control for the emulation above: the same parse finds a tenant in an
  # admitted name, so the verdicts compare something.
  assert {
    condition = alltrue([
      for c in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.plugin_secret_reader[0].condition[0].expression)] :
      c[0] == "projects/123456789012/secrets/swarm-tenant-" && c[1] == "/secrets/swarm-tenant-" && c[2] == "-plg-" &&
      split(c[2], split(c[1], "projects/123456789012/secrets/swarm-tenant-u-alice-plg-x")[1])[0] == "u-alice"
    ])
    error_message = "the condition's parts are the project-number prefix, the /secrets/swarm-tenant- template prefix and the -plg- marker, and the parse recovers u-alice from a personal tenant's secret"
  }
}

run "no_worker_no_secret_in_state_and_labelled" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.plugin_secrets
    override_during = plan
    values = {
      number = "123456789012"
    }
  }

  variables {
    enable_plugin_secrets = true
  }

  # The tenant GSA holds no grant on a plugin secret (design §6.2): this file
  # names no worker account.
  assert {
    condition = var.enable_plugin_secrets && alltrue([
      for f in ["worker_ids", "swarm-agent-worker", "service_account_ids"] :
      !strcontains(file("../../terraform/bootstrap/plugin_secrets.tf"), f)
    ])
    error_message = "terraform/bootstrap/plugin_secrets.tf must grant no tenant worker anything: only swarm-api reads a plugin secret"
  }

  # A secret or a version declared here would put a value, or its slot, in a
  # state file several people read. swarm-api creates each secret at run time.
  assert {
    condition = var.enable_plugin_secrets && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version|_iam_[a-z]+)?\"",
      file("../../terraform/bootstrap/plugin_secrets.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/plugin_secrets.tf"))) == 0
    error_message = "terraform/bootstrap/plugin_secrets.tf must declare no secret, no secret version and no per-secret IAM"
  }

  # An IAM member has no labels: its condition says managed-by=swarm-terraform
  # in its description, as the destroy guard reads.
  assert {
    condition     = startswith(google_project_iam_member.plugin_secret_reader[0].condition[0].description, "managed-by=swarm-terraform;")
    error_message = "the grant's condition says managed-by=swarm-terraform in its description"
  }
}

run "plugin_secret_grants_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      !var.enable_plugin_secrets,
      length(google_project_iam_member.plugin_secret_reader) == 0,
      length(data.google_project.plugin_secrets) == 0,
      output.plugin_secret_grants.api_read == "",
    ])
    error_message = "enable_plugin_secrets defaults to false and, false, grants nothing"
  }
}
