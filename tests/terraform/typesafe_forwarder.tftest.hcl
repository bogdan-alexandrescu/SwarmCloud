# The TypeSafe forwarder reads the version of `swarm-tenant-<t>-typesafe` an
# attempt ticket names, with its own Workload Identity principal, so the key
# never enters an agent pod (docs/design/workspace-secrets.md §3.5; lane
# SEC-INJECT-A, part of #1347). terraform/bootstrap/typesafe_forwarder.tf makes
# the grant; the owner applies it. These runs hold:
#
#   * swarmTypesafeForwarderAccessor carries secretmanager.versions.access and
#     nothing else;
#   * the forwarder's KSA principal alone holds it, through one grant with one
#     condition: a name starting `projects/<NUMBER>/secrets/swarm-tenant-` with
#     a non-empty tenant before the `-typesafe/versions/` that ends the secret
#     id, by extract() and never matches() or a list;
#   * the condition, EVALUATED, admits the versions of swarm-tenant-a-typesafe,
#     of swarm-tenant-u-x-typesafe and of every other tenant's and person's
#     TypeSafe key, and refuses -git, -wsec-, other provider keys, a longer id
#     ending past -typesafe, an empty tenant, swarm-github-app-*, the other
#     team's agents-* and promptlab-*, another project and the project ID --
#     so a dropped or widened condition fails here;
#   * no secret or version is declared, the file names no worker, and every
#     resource says managed-by=swarm-terraform;
#   * nothing exists until enable_typesafe_forwarder_read is set.
#
# The bare secret name (no `/versions/`) is refused on purpose: versions.access
# is evaluated on the version (forge_user_slots.tf, "WHY BOTH NAME FORMS"),
# and the role holds no permission evaluated on a secret.
#
# Hex is built at run time (sha256 of a fixed string), never written out.
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

run "the_accessor_role_carries_versions_access_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.typesafe_forwarder
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_typesafe_forwarder_read = true
  }

  assert {
    condition     = google_project_iam_custom_role.typesafe_forwarder_accessor[0].permissions == toset(["secretmanager.versions.access"])
    error_message = "swarmTypesafeForwarderAccessor reads a version's value and nothing else: no versions.get or list, no IAM, no write"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.typesafe_forwarder_accessor[0].role_id == "swarmTypesafeForwarderAccessor",
      google_project_iam_custom_role.typesafe_forwarder_accessor[0].project == "saga-agents-staging",
      google_project_iam_custom_role.typesafe_forwarder_accessor[0].stage == "GA",
    ])
    error_message = "the role id is the one terraform/modules/custom_role_ids spells, defined in this project"
  }
}

run "the_forwarder_alone_reads_typesafe_key_versions_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.typesafe_forwarder
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_typesafe_forwarder_read = true
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.typesafe_forwarder_accessor) == 1,
      google_project_iam_member.typesafe_forwarder_accessor[0].project == "saga-agents-staging",
      google_project_iam_member.typesafe_forwarder_accessor[0].member == "principal://iam.googleapis.com/projects/209012342332/locations/global/workloadIdentityPools/saga-agents-staging.svc.id.goog/subject/ns/swarm-system/sa/swarm-typesafe-fwd",
      google_project_iam_member.typesafe_forwarder_accessor[0].role == "projects/saga-agents-staging/roles/swarmTypesafeForwarderAccessor",
    ])
    error_message = "the forwarder's KSA principal in swarm-system, alone, holds swarmTypesafeForwarderAccessor, granted through the role resource"
  }

  # Dropped, the grant would read every secret in the shared project, the
  # other team's among them.
  assert {
    condition     = length(google_project_iam_member.typesafe_forwarder_accessor[0].condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, versions.access reads every secret in saga-agents-staging"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  assert {
    condition     = google_project_iam_member.typesafe_forwarder_accessor[0].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-typesafe/versions/\") != \"\""
    error_message = "the forwarder's condition changed; it must be a name starting projects/<NUMBER>/secrets/swarm-tenant- with a non-empty tenant before -typesafe/versions/, by extract(), and nothing else"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", " || ", "saga-agents-staging"] :
      !strcontains(google_project_iam_member.typesafe_forwarder_accessor[0].condition[0].expression, f)
    ])
    error_message = "the condition uses no matches(), no hasOnly() list, no second arm, and the project number rather than its ID"
  }

  assert {
    condition = (
      output.typesafe_forwarder_grants.read == google_project_iam_member.typesafe_forwarder_accessor[0].condition[0].expression
      && output.typesafe_forwarder_grants.member == google_project_iam_member.typesafe_forwarder_accessor[0].member
    )
    error_message = "the output the owner reads a plan against and lints must be the condition and member the grant carries"
  }

  # EVALUATED, as workspace_secrets.tftest.hcl does: the expression is parsed
  # into its startsWith() literal and its extract() template, and each name is
  # judged by them -- startsWith() as startswith(), extract("pre{t}suf") as the
  # text between the FIRST pre and the first suf after it, "" when either is
  # missing. Because the prefix and the template are READ from the expression,
  # widening either one changes the verdicts below and fails here.
  assert {
    condition = alltrue([
      for h in [substr(sha256("sec-inject typesafe forwarder"), 0, 16)] : alltrue([
        for c in [{
          parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.typesafe_forwarder_accessor[0].condition[0].expression)
          admitted = [
            "projects/209012342332/secrets/swarm-tenant-a-typesafe/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe/versions/latest",
            "projects/209012342332/secrets/swarm-tenant-u-x-typesafe/versions/2",
            "projects/209012342332/secrets/swarm-tenant-u-x-typesafe/versions/latest",
            "projects/209012342332/secrets/swarm-tenant-eng-typesafe/versions/3",
            "projects/209012342332/secrets/swarm-tenant-u-someone-not-in-terraform-typesafe/versions/12",
          ]
          refused = [
            "projects/209012342332/secrets/swarm-tenant-a-typesafe",
            "projects/209012342332/secrets/swarm-tenant-a-git",
            "projects/209012342332/secrets/swarm-tenant-a-git/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-git-u-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-git-r-${h}/versions/2",
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-u-x-wsec-${h}/versions/latest",
            "projects/209012342332/secrets/swarm-tenant-a-anthropic/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-anthropic-refresh/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe2/versions/1",
            "projects/209012342332/secrets/swarm-tenant--typesafe/versions/1",
            "projects/209012342332/secrets/swarm-tenant-typesafe/versions/1",
            "projects/209012342332/secrets/swarm-github-app-private-key/versions/1",
            "projects/209012342332/secrets/swarm-github-app-typesafe/versions/1",
            "projects/209012342332/secrets/swarm-account-a-typesafe/versions/1",
            "projects/209012342332/secrets/swarm-typesafe-fwd-key/versions/1",
            "projects/209012342332/secrets/agents-staging-typesafe/versions/1",
            "projects/209012342332/secrets/agents-a-typesafe/versions/1",
            "projects/209012342332/secrets/promptlab-typesafe/versions/1",
            "projects/111111111111/secrets/swarm-tenant-a-typesafe/versions/1",
            "projects/2090123423320/secrets/swarm-tenant-a-typesafe/versions/1",
            "projects/saga-agents-staging/secrets/swarm-tenant-a-typesafe/versions/1",
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
    error_message = "the forwarder's condition must admit the versions of every tenant's and person's swarm-tenant-<t>-typesafe key, people made outside Terraform included, and refuse the tenant's -git, -wsec- and other provider secrets, an id running past -typesafe, an empty tenant, swarm-github-app-*, swarm-account-*, the forwarder's own MAC key, the other team's agents-* and promptlab-*, another project's and the project-ID spelling"
  }

  # The control for the emulation above: the same parse finds the tenant in an
  # admitted name and nothing in a refused one, so the verdicts compare
  # something rather than two empty lists.
  assert {
    condition = alltrue([
      for c in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.typesafe_forwarder_accessor[0].condition[0].expression)] :
      c[0] == "projects/209012342332/secrets/swarm-tenant-" && c[1] == "/secrets/swarm-tenant-" && c[2] == "-typesafe/versions/" &&
      split(c[2], split(c[1], "projects/209012342332/secrets/swarm-tenant-u-x-typesafe/versions/1")[1])[0] == "u-x" &&
      length(split(c[2], split(c[1], "projects/209012342332/secrets/swarm-tenant-a-git/versions/1")[1])) == 1
    ])
    error_message = "the condition's parts are the project-number prefix, the /secrets/swarm-tenant- template prefix and the -typesafe/versions/ marker; the parse recovers u-x from a person's key and finds no marker in a -git version"
  }
}

run "no_secret_no_worker_and_every_resource_labelled" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.typesafe_forwarder
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_typesafe_forwarder_read = true
  }

  # A secret or a version declared here would put a value, or its slot, in a
  # state file several people read.
  assert {
    condition = var.enable_typesafe_forwarder_read && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version|_iam_[a-z]+)?\"",
      file("../../terraform/bootstrap/typesafe_forwarder.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/typesafe_forwarder.tf"))) == 0
    error_message = "terraform/bootstrap/typesafe_forwarder.tf must declare no secret, no secret version and no per-secret IAM"
  }

  # Under `inject` the key never enters a pod: this grant is the forwarder's,
  # and names no tenant worker.
  assert {
    condition = var.enable_typesafe_forwarder_read && alltrue([
      for f in ["worker_ids", "swarm-agent-worker", "service_account_ids", "serviceAccount:"] :
      !strcontains(file("../../terraform/bootstrap/typesafe_forwarder.tf"), f)
    ])
    error_message = "terraform/bootstrap/typesafe_forwarder.tf grants no service account at all: the forwarder's KSA principal is its only member"
  }

  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.typesafe_forwarder_accessor[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_member.typesafe_forwarder_accessor[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "the role and the grant's condition say managed-by=swarm-terraform in their descriptions"
  }
}

run "the_forwarder_read_is_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      !var.enable_typesafe_forwarder_read,
      length(google_project_iam_custom_role.typesafe_forwarder_accessor) == 0,
      length(google_project_iam_member.typesafe_forwarder_accessor) == 0,
      length(data.google_project.typesafe_forwarder) == 0,
      output.typesafe_forwarder_grants.read == "",
      output.typesafe_forwarder_grants.member == "",
    ])
    error_message = "enable_typesafe_forwarder_read defaults to false and, false, defines and grants nothing"
  }
}
