# The quota broker reads each workspace's own provider key, and no other
# secret, so its key import (POST /v1/accounts/import-workspace-keys) can copy
# the key into the pool (docs/design/llm-providers.md §5.3, Q7; owner answer
# of 10 November 2026, "Grant a conditioned read", in terraform/bootstrap).
# terraform/bootstrap/broker_workspace_keys.tf makes the grant. These runs
# hold:
#
#   * swarm-quota-broker alone holds it: roles/secretmanager.secretAccessor,
#     one project grant, one condition;
#   * the condition is startsWith() and extract() only, on the project NUMBER,
#     never matches(), hasOnly() or the project ID;
#   * the condition, EVALUATED, admits versions of swarm-tenant-<t>-anthropic
#     and swarm-tenant-<t>-openai for two workspaces, and refuses another
#     provider's key, a forge token and its user slot, an MCP credential, a
#     saved browser login, an empty tenant, a refresh secret, a pool key,
#     lookalike prefixes, the bare secret, another project and the project-ID
#     spelling;
#   * terraform/modules/iam and terraform/infra, which CI applies, carry no
#     such read.
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

run "the_broker_alone_reads_workspace_provider_keys" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.broker_workspace_keys
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
      google_project_iam_member.broker_workspace_key_reader.project == "example-project",
      google_project_iam_member.broker_workspace_key_reader.member == "serviceAccount:swarm-quota-broker@example-project.iam.gserviceaccount.com",
      google_project_iam_member.broker_workspace_key_reader.role == "roles/secretmanager.secretAccessor",
    ])
    error_message = "swarm-quota-broker, alone, holds roles/secretmanager.secretAccessor on example-project for the key import"
  }

  # Dropped, the grant would read every secret in the shared project, the
  # other team's among them.
  assert {
    condition     = length(google_project_iam_member.broker_workspace_key_reader.condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, secretAccessor reads every secret in example-project"
  }

  assert {
    condition     = strcontains(google_project_iam_member.broker_workspace_key_reader.condition[0].description, "managed-by=swarm-terraform")
    error_message = "an IAM member has no labels; its condition's description says managed-by=swarm-terraform"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  assert {
    condition     = google_project_iam_member.broker_workspace_key_reader.condition[0].expression == "resource.name.startsWith(\"projects/123456789012/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-\") != \"\" && (resource.name.extract(\"/secrets/swarm-tenant-{t}-anthropic/versions/\") != \"\" || resource.name.extract(\"/secrets/swarm-tenant-{t}-openai/versions/\") != \"\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-git-\") == \"\" && resource.name.extract(\"/secrets/swarm-tenant-{t}-browser-\") == \"\""
    error_message = "the broker's workspace-key read condition changed; it must be a version of swarm-tenant-<t>-anthropic or -openai on the project number, a non-empty tenant, no -git- or -browser- part, by startsWith() and extract() alone"
  }

  assert {
    condition = alltrue([
      for f in ["matches(", "hasOnly(", "example-project", "-refresh", "-mcpc-", "-typesafe"] :
      !strcontains(google_project_iam_member.broker_workspace_key_reader.condition[0].expression, f)
    ])
    error_message = "the condition uses no matches(), no hasOnly() list, the project number rather than its ID, and names no refresh, MCP or TypeSafe secret"
  }

  assert {
    condition     = output.broker_workspace_key_read == google_project_iam_member.broker_workspace_key_reader.condition[0].expression
    error_message = "the output the owner reads a plan against and lints must be the condition the grant carries"
  }

  # EVALUATED, as account_key_secrets.tftest.hcl evaluates its own: the
  # expression is parsed into its startsWith() literal and its five extract()
  # templates, and each name is judged by them -- startsWith() as startswith(),
  # extract("pre{t}suf") as the text between the FIRST pre and the first suf
  # after it, "" when either is missing. Because the prefix and the templates
  # are READ from the expression, widening any of them changes the verdicts.
  #
  # Names are in the VERSION form: secretAccessor carries versions.access,
  # which IAM evaluates on `projects/<n>/secrets/<id>/versions/<v>`.
  assert {
    condition = alltrue([
      for c in [{
        parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\" && \\(resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\" \\|\\| resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) == \"\" && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) == \"\"$", google_project_iam_member.broker_workspace_key_reader.condition[0].expression)
        admitted = [
          # Two workspaces, both providers; a team and a person.
          "projects/123456789012/secrets/swarm-tenant-eng-anthropic/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-openai/versions/3",
          "projects/123456789012/secrets/swarm-tenant-u-alice-anthropic/versions/latest",
          "projects/123456789012/secrets/swarm-tenant-u-alice-openai/versions/12",
        ]
        refused = [
          # Another provider's key.
          "projects/123456789012/secrets/swarm-tenant-eng-typesafe/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-gemini/versions/1",
          # The forge token, and a user and a repository slot whose free text
          # ends like a provider.
          "projects/123456789012/secrets/swarm-tenant-eng-git/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-git-u-openai/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-git-r-anthropic/versions/1",
          # An MCP run credential and a workspace secret.
          "projects/123456789012/secrets/swarm-tenant-eng-mcpc-0123456789abcdef/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-wsec-0123456789abcdef/versions/1",
          # A saved browser login named after a provider.
          "projects/123456789012/secrets/swarm-tenant-u-alice-browser-login-openai/versions/1",
          "projects/123456789012/secrets/swarm-tenant-u-alice-browser-anthropic/versions/1",
          # An empty tenant.
          "projects/123456789012/secrets/swarm-tenant--anthropic/versions/1",
          "projects/123456789012/secrets/swarm-tenant--openai/versions/1",
          "projects/123456789012/secrets/swarm-tenant--git-u-openai/versions/1",
          # Refresh secrets: the workspace's and a pool plan's.
          "projects/123456789012/secrets/swarm-tenant-eng-anthropic-refresh/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-openai-refresh/versions/1",
          "projects/123456789012/secrets/swarm-account-eng--work-refresh/versions/1",
          # A pool key and the platform's App key.
          "projects/123456789012/secrets/swarm-account-eng--key-openai/versions/1",
          "projects/123456789012/secrets/swarm-github-app-private-key/versions/1",
          # Lookalike prefixes and suffixes.
          "projects/123456789012/secrets/swarm-tenants-eng-anthropic/versions/1",
          "projects/123456789012/secrets/xswarm-tenant-eng-anthropic/versions/1",
          "projects/123456789012/secrets/agents-swarm-tenant-eng-openai/versions/1",
          "projects/123456789012/secrets/peer-app-openai/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-anthropicx/versions/1",
          "projects/123456789012/secrets/swarm-tenant-eng-openai-key/versions/1",
          # The bare secret: secretAccessor holds no permission evaluated on it.
          "projects/123456789012/secrets/swarm-tenant-eng-anthropic",
          # Another project, a longer number, and the project-ID spelling.
          "projects/210987654321/secrets/swarm-tenant-eng-anthropic/versions/1",
          "projects/${"123456789012"}0/secrets/swarm-tenant-eng-anthropic/versions/1",
          "projects/example-project/secrets/swarm-tenant-eng-openai/versions/1",
        ]
        }] : alltrue([
        for n in concat(c.admitted, c.refused) : alltrue([
          for e in [[
            for t in [[c.parts[1], c.parts[2]], [c.parts[3], c.parts[4]], [c.parts[5], c.parts[6]], [c.parts[7], c.parts[8]], [c.parts[9], c.parts[10]]] :
            length(split(t[1], join(t[0], slice(split(t[0], n), 1, length(split(t[0], n)))))) > 1 ? split(t[1], join(t[0], slice(split(t[0], n), 1, length(split(t[0], n)))))[0] : ""
          ]] :
          (startswith(n, c.parts[0]) && e[0] != "" && (e[1] != "" || e[2] != "") && e[3] == "" && e[4] == "") == contains(c.admitted, n)
        ])
      ])
    ])
    error_message = "the broker's workspace-key read must admit versions of swarm-tenant-<t>-anthropic and -openai for every workspace, and refuse another provider's key, a forge token or slot, an MCP credential, a workspace secret, a browser login, an empty tenant, a refresh secret, a pool key, lookalike prefixes, the bare secret, another project and the project-ID spelling"
  }

  # The control for the emulation above: the parse yields the expected parts,
  # and recovers u-alice from an admitted name, so the verdicts compare
  # something rather than two empty lists.
  assert {
    condition = alltrue([
      for c in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\" && \\(resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\" \\|\\| resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) == \"\" && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) == \"\"$", google_project_iam_member.broker_workspace_key_reader.condition[0].expression)] :
      c[0] == "projects/123456789012/secrets/swarm-tenant-" &&
      c[1] == "/secrets/swarm-tenant-" && c[2] == "-" &&
      c[4] == "-anthropic/versions/" && c[6] == "-openai/versions/" &&
      c[8] == "-git-" && c[10] == "-browser-" &&
      split(c[4], split(c[3], "projects/123456789012/secrets/swarm-tenant-u-alice-anthropic/versions/1")[1])[0] == "u-alice" &&
      length(split(c[6], split(c[5], "projects/123456789012/secrets/swarm-tenant-u-alice-openai-refresh/versions/1")[1])) == 1
    ])
    error_message = "the condition's parts are the project-number prefix, the tenant segment, the -anthropic/versions/ and -openai/versions/ suffixes and the -git- and -browser- exclusions; the parse recovers u-alice from a key's version and finds no suffix in a refresh secret's"
  }
}

# CI applies terraform/modules/iam and terraform/infra. A project
# secretAccessor grant made there would need the role on
# deployer_grantable_project_roles, whose hasOnly() bounds neither the member
# nor the condition: CI could grant itself read on every secret in the shared
# project. So no file CI applies carries this read.
run "ci_carries_no_workspace_key_read" {
  command = plan

  module {
    source = "../../terraform/modules/iam"
  }

  # Read as text, every root and module file CI applies, so a grant added in a
  # new resource or another file is found too; a comment may name it. The
  # control is the module's own broker secret grant, so the scan read the
  # module it was meant to, and the file count, so it read files at all.
  assert {
    condition = alltrue([
      google_project_iam_member.broker_version_adder.role == "roles/secretmanager.secretVersionAdder",
      length(fileset("../../terraform/infra", "*.tf")) > 10,
      alltrue([
        for f in concat(
          [for p in fileset("../../terraform/modules/iam", "*.tf") : "../../terraform/modules/iam/${p}"],
          [for p in fileset("../../terraform/infra", "*.tf") : "../../terraform/infra/${p}"],
          ) : !anytrue([
            for line in split("\n", file(f)) : (strcontains(line, "-anthropic/versions/") || strcontains(line, "-openai/versions/")) && !startswith(trimspace(line), "#")
        ])
      ]),
    ])
    error_message = "a file CI applies (terraform/modules/iam or terraform/infra) carries a read of swarm-tenant-<t>-anthropic or -openai versions, or the scan read no files; the read is terraform/bootstrap/broker_workspace_keys.tf's alone"
  }
}
