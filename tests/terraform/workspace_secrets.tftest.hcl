# swarm-api reads workspace secrets, `swarm-tenant-<tenant>-wsec-<16 hex>`,
# and their versions, so it can release an attempt's granted ones to that
# attempt's worker; no tenant worker reads one (docs/design/workspace-secrets.md
# §3.3, option B; lane SEC-IAM, part of #1347). terraform/bootstrap/
# workspace_secrets.tf makes the grant. These runs hold:
#
#   * swarmWorkspaceSecretAccessor carries secretmanager.versions.access and
#     nothing else;
#   * swarm-api alone holds it, through one grant with one condition: a name
#     starting `projects/<NUMBER>/secrets/swarm-tenant-` with a non-empty
#     tenant before `-wsec-`, by extract() and never matches() or a list;
#   * the condition, EVALUATED, admits every workspace's -wsec- secrets and
#     their versions and refuses the tenant's forge token, its provider keys
#     (-typesafe among them), its forge slots, the platform's secrets, the
#     other team's, another project's and the project-ID spelling -- so a
#     dropped condition, or one widened, fails here;
#   * no bootstrap grant to a tenant worker admits a -wsec- name, and this
#     file names no worker;
#   * no secret or version is declared, and every resource says
#     managed-by=swarm-terraform;
#   * nothing exists until enable_workspace_secrets is set.
#
# The hex of a workspace secret is built at run time (sha256 of a fixed
# string), never written out: nothing here may look like a credential.
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

run "the_accessor_role_carries_versions_access_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.workspace_secrets
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_workspace_secrets = true
  }

  assert {
    condition     = google_project_iam_custom_role.workspace_secret_accessor[0].permissions == toset(["secretmanager.versions.access"])
    error_message = "swarmWorkspaceSecretAccessor reads a version's value and nothing else: no versions.get or versions.list (the release accesses the record's current_version by name), no IAM, no write"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.workspace_secret_accessor[0].role_id == "swarmWorkspaceSecretAccessor",
      google_project_iam_custom_role.workspace_secret_accessor[0].project == "saga-agents-staging",
      google_project_iam_custom_role.workspace_secret_accessor[0].stage == "GA",
    ])
    error_message = "the role id is the one terraform/modules/custom_role_ids spells, defined in this project"
  }
}

run "swarm_api_alone_reads_wsec_secrets_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.workspace_secrets
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_workspace_secrets = true
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.workspace_secret_accessor) == 1,
      google_project_iam_member.workspace_secret_accessor[0].project == "saga-agents-staging",
      google_project_iam_member.workspace_secret_accessor[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.workspace_secret_accessor[0].role == "projects/saga-agents-staging/roles/swarmWorkspaceSecretAccessor",
    ])
    error_message = "swarm-api, alone, holds swarmWorkspaceSecretAccessor, granted through the role resource"
  }

  # Dropped, the grant would read every secret in the shared project, the
  # other team's among them.
  assert {
    condition     = length(google_project_iam_member.workspace_secret_accessor[0].condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, versions.access reads every secret in saga-agents-staging"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  # The project NUMBER, extract() and no matches(), no hasOnly(), no list.
  assert {
    condition     = google_project_iam_member.workspace_secret_accessor[0].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-wsec-\") != \"\""
    error_message = "swarm-api's workspace-secret condition changed; it must be a name starting projects/<NUMBER>/secrets/swarm-tenant- with a non-empty tenant before -wsec-, by extract(), and nothing else"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", " || ", "saga-agents-staging"] :
      !strcontains(google_project_iam_member.workspace_secret_accessor[0].condition[0].expression, f)
    ])
    error_message = "the condition uses no matches() (refused on some services), no hasOnly() list, no second arm, and the project number rather than its ID"
  }

  assert {
    condition     = output.workspace_secret_grants.api_read == google_project_iam_member.workspace_secret_accessor[0].condition[0].expression
    error_message = "the output the owner reads a plan against and lints must be the condition the grant carries"
  }

  # EVALUATED. The expression is parsed into its startsWith() literal and its
  # extract() template, and each name is judged by them: startsWith() as
  # startswith(), and extract("pre{t}suf") as the text between the FIRST pre
  # and the first suf after it, "" when either is missing (Google's attribute
  # reference), which split() on the literal pre and suf reproduces. Because
  # the prefix and the template are READ from the expression, widening either
  # one -- `swarm-` for `swarm-tenant-`, `-` for `-wsec-`, a shorter project
  # -- changes the verdicts below and fails here, not only the string above.
  assert {
    condition = alltrue([
      for h in [substr(sha256("sec-iam workspace secret"), 0, 16)] : alltrue([
        for c in [{
          parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.workspace_secret_accessor[0].condition[0].expression)
          admitted = [
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}/versions/latest",
            "projects/209012342332/secrets/swarm-tenant-eng-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant-u-alice-wsec-${h}/versions/12",
            "projects/209012342332/secrets/swarm-tenant-u-someone-not-in-terraform-wsec-${h}",
          ]
          refused = [
            "projects/209012342332/secrets/swarm-tenant-a-git",
            "projects/209012342332/secrets/swarm-tenant-a-git/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-git-u-${h}",
            "projects/209012342332/secrets/swarm-tenant-a-git-r-${h}/versions/2",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe/versions/3",
            "projects/209012342332/secrets/swarm-tenant-u-alice-typesafe",
            "projects/209012342332/secrets/swarm-tenant-a-anthropic",
            "projects/209012342332/secrets/swarm-tenant-a-anthropic-refresh",
            "projects/209012342332/secrets/swarm-tenant-u-alice-browser-login-github-qa",
            "projects/209012342332/secrets/swarm-tenant-a-wsec",
            "projects/209012342332/secrets/swarm-tenant-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant--wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant",
            "projects/209012342332/secrets/swarm-github-app-private-key",
            "projects/209012342332/secrets/swarm-github-app-webhook-wsec-${h}",
            "projects/209012342332/secrets/swarm-account-a-wsec-${h}",
            "projects/209012342332/secrets/agents-staging-anthropic",
            "projects/209012342332/secrets/agents-a-wsec-${h}",
            "projects/209012342332/secrets/promptlab-openai",
            "projects/111111111111/secrets/swarm-tenant-a-wsec-${h}",
            "projects/2090123423320/secrets/swarm-tenant-a-wsec-${h}",
            "projects/saga-agents-staging/secrets/swarm-tenant-a-wsec-${h}",
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
    error_message = "swarm-api's workspace-secret condition must admit every workspace's swarm-tenant-<t>-wsec-<hex> secret and its versions, workspaces made outside Terraform included, and refuse the tenant's -git and -typesafe secrets, its forge slots and provider keys, an empty tenant, the platform's swarm-github-app-* and swarm-account-* secrets, the other team's agents-* and promptlab-*, another project's and the project-ID spelling"
  }

  # The control for the emulation above: the same parse finds a tenant in an
  # admitted name, and finds none in a refused one, so the verdicts compare
  # something rather than two empty lists.
  assert {
    condition = alltrue([
      for c in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.workspace_secret_accessor[0].condition[0].expression)] :
      c[0] == "projects/209012342332/secrets/swarm-tenant-" && c[1] == "/secrets/swarm-tenant-" && c[2] == "-wsec-" &&
      split(c[2], split(c[1], "projects/209012342332/secrets/swarm-tenant-u-alice-wsec-x")[1])[0] == "u-alice"
    ])
    error_message = "the condition's parts are the project-number prefix, the /secrets/swarm-tenant- template prefix and the -wsec- marker, and the parse recovers u-alice from a personal tenant's secret"
  }
}

run "no_tenant_worker_reads_a_wsec_secret" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.workspace_secrets
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target          = data.google_project.forge_user_slots
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target          = data.google_project.typesafe_user_keys
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  # Every switch that grants a tenant worker a Secret Manager read in this
  # root, on, so the check below sees each worker binding there is.
  variables {
    enable_workspace_secrets  = true
    enable_forge_user_slots   = true
    typesafe_user_key_readers = { eng = ["u-alice"] }
  }

  # This file names no worker account: the grant is swarm-api's.
  assert {
    condition = var.enable_workspace_secrets && alltrue([
      for f in ["worker_ids", "swarm-agent-worker", "service_account_ids"] :
      !strcontains(file("../../terraform/bootstrap/workspace_secrets.tf"), f)
    ])
    error_message = "terraform/bootstrap/workspace_secrets.tf must grant no tenant worker anything: under option B the tenant GSA reads no -wsec- secret"
  }

  # The bootstrap grants that DO name a tenant worker for a Secret Manager
  # read. Each condition is a disjunction whose every arm carries a positive
  # name literal -- a startsWith() prefix or an == name -- so it can admit a
  # name only if one of those literals does. None may admit the tenant's own
  # -wsec- secret or a version of it.
  assert {
    condition = alltrue([
      for b in concat(
        [for k, m in google_project_iam_member.forge_slot_reader : { tenant = k, member = m.member, expr = m.condition[0].expression }],
        [for k, m in google_project_iam_member.typesafe_user_key_reader : { tenant = split("/", k)[0], member = m.member, expr = m.condition[0].expression }],
        ) : alltrue([
          for n in [
            "projects/209012342332/secrets/swarm-tenant-${b.tenant}-wsec-${substr(sha256("sec-iam workspace secret"), 0, 16)}",
            "projects/209012342332/secrets/swarm-tenant-${b.tenant}-wsec-${substr(sha256("sec-iam workspace secret"), 0, 16)}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-u-alice-wsec-${substr(sha256("sec-iam workspace secret"), 0, 16)}",
            ] : alltrue(concat(
              [for p in regexall("startsWith\\(\"([^\"]+)\"\\)", b.expr) : !startswith(n, p[0])],
              [for e in regexall("== \"([^\"]+)\"", b.expr) : n != e[0]],
          ))
      ])
    ])
    error_message = "a bootstrap grant to a tenant worker admits a -wsec- secret: under option B only swarm-api reads a workspace secret, and a worker grant would let any agent of that tenant mint its GSA token and read every one"
  }

  # The control: the bindings exist, each is a worker's, each condition has a
  # literal the check reads, and the same check DOES flag the secret each one
  # is for -- so the assertion above could have come out the other way.
  assert {
    condition = alltrue([
      length(google_project_iam_member.forge_slot_reader) > 0,
      length(google_project_iam_member.typesafe_user_key_reader) == 1,
      alltrue([for k, m in google_project_iam_member.forge_slot_reader : startswith(m.member, "serviceAccount:swarm-agent-worker-${k}@")]),
      alltrue([for k, m in google_project_iam_member.typesafe_user_key_reader : startswith(m.member, "serviceAccount:swarm-agent-worker-eng@")]),
      alltrue([
        for k, m in google_project_iam_member.forge_slot_reader :
        anytrue([for p in regexall("startsWith\\(\"([^\"]+)\"\\)", m.condition[0].expression) : startswith("projects/209012342332/secrets/swarm-tenant-${k}-git-u-${substr(sha256("sec-iam workspace secret"), 0, 16)}", p[0])])
      ]),
      alltrue([
        for k, m in google_project_iam_member.typesafe_user_key_reader :
        anytrue([for e in regexall("== \"([^\"]+)\"", m.condition[0].expression) : e[0] == "projects/209012342332/secrets/swarm-tenant-u-alice-typesafe"])
      ]),
    ])
    error_message = "the worker-grant check read no binding, or a binding's condition has no name literal, or the check fails to admit the forge slot or TypeSafe key the binding exists for: the assertion above would be vacuous"
  }
}

run "no_secret_in_state_and_every_resource_labelled" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.workspace_secrets
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_workspace_secrets = true
  }

  # A secret or a version declared here would put a value, or its slot, in a
  # state file several people read. swarm-api creates each secret at run time.
  assert {
    condition = var.enable_workspace_secrets && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version|_iam_[a-z]+)?\"",
      file("../../terraform/bootstrap/workspace_secrets.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/workspace_secrets.tf"))) == 0
    error_message = "terraform/bootstrap/workspace_secrets.tf must declare no secret, no secret version and no per-secret IAM: workspace secrets live in Secret Manager only, written at run time"
  }

  # A custom role has no labels and an IAM member has none either: each says
  # managed-by=swarm-terraform in its description, as the destroy guard reads.
  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.workspace_secret_accessor[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_member.workspace_secret_accessor[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "the role and the grant's condition say managed-by=swarm-terraform in their descriptions"
  }
}

run "workspace_secret_grants_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      !var.enable_workspace_secrets,
      length(google_project_iam_custom_role.workspace_secret_accessor) == 0,
      length(google_project_iam_member.workspace_secret_accessor) == 0,
      length(data.google_project.workspace_secrets) == 0,
      output.workspace_secret_grants.api_read == "",
    ])
    error_message = "enable_workspace_secrets defaults to false and, false, defines and grants nothing"
  }
}
