# swarm-api reads and deletes per-run MCP credentials,
# `swarm-tenant-<tenant>-mcpc-<16 hex>`, and their versions, so the attach
# route can hand an attempt's relay the credential its caller supplied and the
# sweep can delete it when the task ends; no tenant worker reads one
# (docs/design/registered-mcp-servers.md §4.4; lane MCP-IAM, part of #1433).
# terraform/bootstrap/mcp_credentials.tf makes the grant. These runs hold:
#
#   * swarmMcpCredentialAccessor carries secretmanager.versions.access and
#     secretmanager.secrets.delete and nothing else;
#   * swarm-api alone holds it, through one grant with one condition: a name
#     starting `projects/<NUMBER>/secrets/swarm-tenant-` with a non-empty
#     tenant before `-mcpc-`, by extract() and never matches() or a list;
#   * the condition, EVALUATED, admits every tenant's -mcpc- secrets and their
#     versions and refuses a workspace secret (-wsec-), the tenant's forge
#     token (-git), its provider keys (-typesafe among them), the platform's
#     swarm-github-app-* secrets, the other team's agents-*, another
#     project's and the project-ID spelling -- so a dropped condition, or one
#     widened, fails here;
#   * no bootstrap grant to a tenant worker admits a -mcpc- name, and the file
#     names no worker;
#   * no secret or version is declared, and every resource says
#     managed-by=swarm-terraform;
#   * nothing exists until enable_mcp_credentials is set.
#
# The hex of a credential secret is built at run time (sha256 of a fixed
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

run "the_accessor_role_carries_access_and_delete_alone" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.mcp_credentials
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_mcp_credentials = true
  }

  assert {
    condition     = google_project_iam_custom_role.mcp_credential_accessor[0].permissions == toset(["secretmanager.versions.access", "secretmanager.secrets.delete"])
    error_message = "swarmMcpCredentialAccessor reads a version's value and deletes a credential secret, and nothing else: no versions.get or versions.list (the attach accesses the row's version by name), no IAM, no create (swarmProviderKeyWriter's)"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.mcp_credential_accessor[0].role_id == "swarmMcpCredentialAccessor",
      google_project_iam_custom_role.mcp_credential_accessor[0].project == "saga-agents-staging",
      google_project_iam_custom_role.mcp_credential_accessor[0].stage == "GA",
    ])
    error_message = "the role is swarmMcpCredentialAccessor, GA, defined in this project"
  }
}

run "swarm_api_alone_reads_mcpc_secrets_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.mcp_credentials
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_mcp_credentials = true
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.mcp_credential_accessor) == 1,
      google_project_iam_member.mcp_credential_accessor[0].project == "saga-agents-staging",
      google_project_iam_member.mcp_credential_accessor[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.mcp_credential_accessor[0].role == "projects/saga-agents-staging/roles/swarmMcpCredentialAccessor",
    ])
    error_message = "swarm-api, alone, holds swarmMcpCredentialAccessor, granted through the role resource"
  }

  # Dropped, the grant would read every secret in the shared project, the
  # other team's among them.
  assert {
    condition     = length(google_project_iam_member.mcp_credential_accessor[0].condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, versions.access reads every secret in saga-agents-staging"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  assert {
    condition     = google_project_iam_member.mcp_credential_accessor[0].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-mcpc-\") != \"\""
    error_message = "swarm-api's MCP credential condition changed; it must be a name starting projects/<NUMBER>/secrets/swarm-tenant- with a non-empty tenant before -mcpc-, by extract(), and nothing else"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", " || ", "saga-agents-staging", "wsec"] :
      !strcontains(google_project_iam_member.mcp_credential_accessor[0].condition[0].expression, f)
    ])
    error_message = "the condition uses no matches(), no hasOnly() list, no second arm, no -wsec- arm, and the project number rather than its ID"
  }

  assert {
    condition     = output.mcp_credential_grants.api_read == google_project_iam_member.mcp_credential_accessor[0].condition[0].expression
    error_message = "the output the owner reads a plan against and lints must be the condition the grant carries"
  }

  # EVALUATED, as workspace_secrets.tftest.hcl evaluates its own: the
  # expression is parsed into its startsWith() literal and its extract()
  # template, and each name is judged by them -- startsWith() as startswith(),
  # extract("pre{t}suf") as the text between the FIRST pre and the first suf
  # after it, "" when either is missing. Because the prefix and the template
  # are READ from the expression, widening either changes the verdicts below.
  assert {
    condition = alltrue([
      for h in [substr(sha256("mcp-iam per-run credential"), 0, 16)] : alltrue([
        for c in [{
          parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.mcp_credential_accessor[0].condition[0].expression)
          admitted = [
            "projects/209012342332/secrets/swarm-tenant-a-mcpc-${h}",
            "projects/209012342332/secrets/swarm-tenant-a-mcpc-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-mcpc-${h}/versions/latest",
            "projects/209012342332/secrets/swarm-tenant-eng-mcpc-${h}",
            "projects/209012342332/secrets/swarm-tenant-u-alice-mcpc-${h}/versions/3",
            "projects/209012342332/secrets/swarm-tenant-u-someone-not-in-terraform-mcpc-${h}",
          ]
          refused = [
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant-a-wsec-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-git",
            "projects/209012342332/secrets/swarm-tenant-a-git/versions/1",
            "projects/209012342332/secrets/swarm-tenant-a-git-u-${h}",
            "projects/209012342332/secrets/swarm-tenant-a-git-r-${h}/versions/2",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe",
            "projects/209012342332/secrets/swarm-tenant-a-typesafe/versions/3",
            "projects/209012342332/secrets/swarm-tenant-u-alice-typesafe",
            "projects/209012342332/secrets/swarm-tenant-a-anthropic",
            "projects/209012342332/secrets/swarm-tenant-a-mcpc",
            "projects/209012342332/secrets/swarm-tenant-mcpc-${h}",
            "projects/209012342332/secrets/swarm-tenant--mcpc-${h}",
            "projects/209012342332/secrets/swarm-tenant",
            "projects/209012342332/secrets/swarm-github-app-private-key",
            "projects/209012342332/secrets/swarm-github-app-webhook-mcpc-${h}",
            "projects/209012342332/secrets/swarm-account-a-mcpc-${h}",
            "projects/209012342332/secrets/agents-staging-anthropic",
            "projects/209012342332/secrets/agents-a-mcpc-${h}",
            "projects/209012342332/secrets/promptlab-openai",
            "projects/111111111111/secrets/swarm-tenant-a-mcpc-${h}",
            "projects/2090123423320/secrets/swarm-tenant-a-mcpc-${h}",
            "projects/saga-agents-staging/secrets/swarm-tenant-a-mcpc-${h}",
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
    error_message = "swarm-api's MCP credential condition must admit every tenant's swarm-tenant-<t>-mcpc-<hex> secret and its versions, personal tenants made outside Terraform included, and refuse a workspace secret (-wsec-), the tenant's -git and -typesafe secrets, its forge slots and provider keys, an empty tenant, the platform's swarm-github-app-* and swarm-account-* secrets, the other team's agents-* and promptlab-*, another project's and the project-ID spelling"
  }

  # The control for the emulation above: the same parse finds a tenant in an
  # admitted name, and finds none in a refused one, so the verdicts compare
  # something rather than two empty lists.
  assert {
    condition = alltrue([
      for c in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.mcp_credential_accessor[0].condition[0].expression)] :
      c[0] == "projects/209012342332/secrets/swarm-tenant-" && c[1] == "/secrets/swarm-tenant-" && c[2] == "-mcpc-" &&
      split(c[2], split(c[1], "projects/209012342332/secrets/swarm-tenant-u-alice-mcpc-x")[1])[0] == "u-alice" &&
      length(split(c[2], split(c[1], "projects/209012342332/secrets/swarm-tenant-u-alice-wsec-x")[1])) == 1
    ])
    error_message = "the condition's parts are the project-number prefix, the /secrets/swarm-tenant- template prefix and the -mcpc- marker; the parse recovers u-alice from a personal tenant's credential and finds no marker in a workspace secret"
  }
}

run "no_tenant_worker_reads_a_mcpc_secret" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.mcp_credentials
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
    enable_mcp_credentials    = true
    enable_forge_user_slots   = true
    typesafe_user_key_readers = { eng = ["u-alice"] }
  }

  # This file names no worker account: the grant is swarm-api's.
  assert {
    condition = var.enable_mcp_credentials && alltrue([
      for f in ["worker_ids", "swarm-agent-worker", "service_account_ids"] :
      !strcontains(file("../../terraform/bootstrap/mcp_credentials.tf"), f)
    ])
    error_message = "terraform/bootstrap/mcp_credentials.tf must grant no tenant worker anything: the tenant GSA reads no -mcpc- secret"
  }

  # The bootstrap grants that DO name a tenant worker for a Secret Manager
  # read. Each condition is a disjunction whose every arm carries a positive
  # name literal -- a startsWith() prefix or an == name -- so it can admit a
  # name only if one of those literals does. None may admit the tenant's own
  # -mcpc- secret or a version of it.
  assert {
    condition = alltrue([
      for b in concat(
        [for k, m in google_project_iam_member.forge_slot_reader : { tenant = k, member = m.member, expr = m.condition[0].expression }],
        [for k, m in google_project_iam_member.typesafe_user_key_reader : { tenant = split("/", k)[0], member = m.member, expr = m.condition[0].expression }],
        ) : alltrue([
          for n in [
            "projects/209012342332/secrets/swarm-tenant-${b.tenant}-mcpc-${substr(sha256("mcp-iam per-run credential"), 0, 16)}",
            "projects/209012342332/secrets/swarm-tenant-${b.tenant}-mcpc-${substr(sha256("mcp-iam per-run credential"), 0, 16)}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-u-alice-mcpc-${substr(sha256("mcp-iam per-run credential"), 0, 16)}",
            ] : alltrue(concat(
              [for p in regexall("startsWith\\(\"([^\"]+)\"\\)", b.expr) : !startswith(n, p[0])],
              [for e in regexall("== \"([^\"]+)\"", b.expr) : n != e[0]],
          ))
      ])
    ])
    error_message = "a bootstrap grant to a tenant worker admits a -mcpc- secret: only swarm-api reads a per-run MCP credential, and a worker grant would let any agent of that tenant mint its GSA token and read the credential the relay keeps from it"
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
        anytrue([for p in regexall("startsWith\\(\"([^\"]+)\"\\)", m.condition[0].expression) : startswith("projects/209012342332/secrets/swarm-tenant-${k}-git-u-${substr(sha256("mcp-iam per-run credential"), 0, 16)}", p[0])])
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
    target          = data.google_project.mcp_credentials
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_mcp_credentials = true
  }

  # A secret or a version declared here would put a value, or its slot, in a
  # state file several people read. swarm-api creates each secret at run time.
  assert {
    condition = var.enable_mcp_credentials && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version|_iam_[a-z]+)?\"",
      file("../../terraform/bootstrap/mcp_credentials.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/mcp_credentials.tf"))) == 0
    error_message = "terraform/bootstrap/mcp_credentials.tf must declare no secret, no secret version and no per-secret IAM: per-run credentials live in Secret Manager only, written at run time"
  }

  # A custom role has no labels and an IAM member has none either: each says
  # managed-by=swarm-terraform in its description, as the destroy guard reads.
  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.mcp_credential_accessor[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_member.mcp_credential_accessor[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "the role and the grant's condition say managed-by=swarm-terraform in their descriptions"
  }
}

run "mcp_credential_grants_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      !var.enable_mcp_credentials,
      length(google_project_iam_custom_role.mcp_credential_accessor) == 0,
      length(google_project_iam_member.mcp_credential_accessor) == 0,
      length(data.google_project.mcp_credentials) == 0,
      output.mcp_credential_grants.api_read == "",
    ])
    error_message = "enable_mcp_credentials defaults to false and, false, defines and grants nothing"
  }
}
