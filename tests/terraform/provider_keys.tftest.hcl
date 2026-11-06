# swarm-api writes and destroys tenant provider keys in Secret Manager, on
# secrets named swarm-tenant-* only (owner decision 2026-10-14, lane KEYS-IAM;
# docs/provider-keys.md §6). terraform/bootstrap/provider_keys.tf makes the
# grant. These runs hold:
#
#   * swarmProviderKeyWriter carries exactly the Secret Manager permissions
#     apps/swarm-api/swarm_api/credentials.py's calls need -- read out of that
#     file, so a call added there, or one removed, fails here until the role
#     follows;
#   * swarm-api alone holds it, through one grant with one condition: the
#     project (where secrets.create is evaluated) or a name starting
#     `projects/<NUMBER>/secrets/swarm-tenant-`;
#   * the name arm, EVALUATED, admits every tenant's provider key and its
#     versions and refuses the platform's secrets, the other team's, another
#     project's and the project-ID spelling;
#   * no secret or version is declared, and every resource says
#     managed-by=swarm-terraform;
#   * nothing exists until enable_provider_keys is set.
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

run "the_writer_role_carries_exactly_what_credentials_py_calls" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.provider_keys
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_provider_keys = true
  }

  assert {
    condition = google_project_iam_custom_role.provider_key_writer[0].permissions == toset([
      "secretmanager.secrets.create",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
      "secretmanager.versions.add",
      "secretmanager.versions.destroy",
      "secretmanager.versions.disable",
      "secretmanager.versions.list",
    ])
    error_message = "swarmProviderKeyWriter creates a tenant secret, reads and merges its IAM policy, adds a version, lists, disables and destroys versions -- nothing else: no versions.access (neither writer has a read path), no versions.enable (nothing re-enables a version), no secrets.get (neither reads metadata), no secrets.delete (a deleted key keeps its secret)"
  }

  # THE ROTATION SWEEP'S ONE PERMISSION (#1347, owner decision Q5): the
  # workspace secrets store disables a rotated-out version after its 24-hour
  # grace and never destroys it. Against the role as KEYS-IAM shipped it, the
  # sweep adds exactly versions.disable: not versions.enable, not
  # versions.access, not secrets.delete, and no second destroy-shaped
  # permission. The grant itself is held unchanged by the next run.
  assert {
    condition = setsubtract(google_project_iam_custom_role.provider_key_writer[0].permissions, toset([
      "secretmanager.secrets.create",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
      "secretmanager.versions.add",
      "secretmanager.versions.destroy",
      "secretmanager.versions.list",
    ])) == toset(["secretmanager.versions.disable"])
    error_message = "the rotation sweep adds secretmanager.versions.disable to swarmProviderKeyWriter and nothing else"
  }

  # DERIVED FROM THE CALLERS. Every Secret Manager client method, paired with
  # the permission it needs: the role holds the permission exactly when
  # credentials.py or workspace_secrets.py (SecretManagerWorkspaceSecrets,
  # the workspace secrets store's writer, which runs as the same swarm-api
  # account on the same swarm-tenant-* names) calls the method.
  # `client.<method>(` and `_secret_client().<method>(` are how those files
  # spell every call. Pairs rather than a map, which the worker's publish
  # scan reads as credential assignments.
  assert {
    condition = alltrue([
      for call in [
        ["create_secret", "secretmanager.secrets.create"],
        ["get_secret", "secretmanager.secrets.get"],
        ["update_secret", "secretmanager.secrets.update"],
        ["delete_secret", "secretmanager.secrets.delete"],
        ["list_secrets", "secretmanager.secrets.list"],
        ["get_iam_policy", "secretmanager.secrets.getIamPolicy"],
        ["set_iam_policy", "secretmanager.secrets.setIamPolicy"],
        ["add_secret_version", "secretmanager.versions.add"],
        ["get_secret_version", "secretmanager.versions.get"],
        ["list_secret_versions", "secretmanager.versions.list"],
        ["access_secret_version", "secretmanager.versions.access"],
        ["disable_secret_version", "secretmanager.versions.disable"],
        ["enable_secret_version", "secretmanager.versions.enable"],
        ["destroy_secret_version", "secretmanager.versions.destroy"],
        ] : contains(google_project_iam_custom_role.provider_key_writer[0].permissions, call[1]) == (
        length(regexall("(client|_secret_client\\(\\))\\.${call[0]}\\(", join("\n", [
          file("../../apps/swarm-api/swarm_api/credentials.py"),
          file("../../apps/swarm-api/swarm_api/workspace_secrets.py"),
        ]))) > 0
      )
    ])
    error_message = "swarmProviderKeyWriter and its callers (apps/swarm-api/swarm_api/credentials.py, workspace_secrets.py) disagree: the role must hold a Secret Manager permission exactly when one of them calls its method"
  }

  # The control for the second file: the rotation sweep's call is found, so
  # the scan above really reads workspace_secrets.py.
  assert {
    condition     = var.enable_provider_keys && length(regexall("_secret_client\\(\\)\\.disable_secret_version\\(", file("../../apps/swarm-api/swarm_api/workspace_secrets.py"))) == 1
    error_message = "the scan found no disable_secret_version call in workspace_secrets.py; check its path and its spelling of a call"
  }

  # The control for the scan above: a scan that matched nothing would make
  # every permission "not called" and hold only an empty role.
  assert {
    condition     = var.enable_provider_keys && length(regexall("client\\.[a-z_]+_secret(_version|_versions)?\\(", file("../../apps/swarm-api/swarm_api/credentials.py"))) >= 4
    error_message = "the scan of credentials.py found too few Secret Manager calls to mean anything; check its path and its spelling of a call"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.provider_key_writer[0].role_id == "swarmProviderKeyWriter",
      google_project_iam_custom_role.provider_key_writer[0].project == "saga-agents-staging",
      google_project_iam_custom_role.provider_key_writer[0].stage == "GA",
    ])
    error_message = "the role id is the one terraform/modules/custom_role_ids spells, defined in this project"
  }
}

run "swarm_api_alone_holds_it_on_swarm_tenant_secrets" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.provider_keys
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_provider_keys = true
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.provider_key_writer) == 1,
      google_project_iam_member.provider_key_writer[0].project == "saga-agents-staging",
      google_project_iam_member.provider_key_writer[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.provider_key_writer[0].role == "projects/saga-agents-staging/roles/swarmProviderKeyWriter",
    ])
    error_message = "swarm-api, alone, holds swarmProviderKeyWriter, granted through the role resource"
  }

  assert {
    condition     = length(google_project_iam_member.provider_key_writer[0].condition) == 1
    error_message = "the grant carries exactly one condition; unconditioned, setIamPolicy would reach every secret in the shared project, the other team's among them"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  # The project NUMBER: IAM evaluates Secret Manager names with it.
  assert {
    condition     = google_project_iam_member.provider_key_writer[0].condition[0].expression == "resource.type == \"cloudresourcemanager.googleapis.com/Project\" || resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\")"
    error_message = "swarm-api's provider-key condition changed; it must be the project (where secrets.create is evaluated) or a name starting projects/<NUMBER>/secrets/swarm-tenant-, and nothing else"
  }

  assert {
    condition     = output.provider_key_grants.api_write == google_project_iam_member.provider_key_writer[0].condition[0].expression
    error_message = "the output the owner reads a plan against must be the condition the grant carries"
  }

  # EVALUATED. On a secret or a version -- the type every check but create is
  # made on -- the project arm is false, so the grant holds exactly where
  # startsWith() does.
  assert {
    condition = alltrue([
      for c in [{
        expr = google_project_iam_member.provider_key_writer[0].condition[0].expression
        admitted = [
          "projects/209012342332/secrets/swarm-tenant-eng-anthropic",
          "projects/209012342332/secrets/swarm-tenant-eng-anthropic/versions/3",
          "projects/209012342332/secrets/swarm-tenant-u-alice-typesafe",
          "projects/209012342332/secrets/swarm-tenant-u-someone-not-in-terraform-typesafe/versions/1",
        ]
        refused = [
          "projects/209012342332/secrets/swarm-github-app-private-key",
          "projects/209012342332/secrets/swarm-tenant",
          "projects/209012342332/secrets/agents-staging-anthropic",
          "projects/209012342332/secrets/promptlab-openai",
          "projects/111111111111/secrets/swarm-tenant-eng-anthropic",
          "projects/saga-agents-staging/secrets/swarm-tenant-eng-anthropic",
        ]
        }] : alltrue([
        for n in concat(c.admitted, c.refused) :
        startswith(n, regex("^resource\\.type == \"cloudresourcemanager\\.googleapis\\.com/Project\" \\|\\| resource\\.name\\.startsWith\\(\"([^\"]+)\"\\)$", c.expr)[0]) == contains(c.admitted, n)
      ])
    ])
    error_message = "the name arm must admit every tenant's provider key and its versions, workspaces made outside Terraform included, and refuse the platform's secrets, the other team's, another project's, and the project-ID spelling"
  }
}

run "no_key_in_state_and_every_resource_labelled" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.provider_keys
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_provider_keys = true
  }

  # A secret or a version declared here would put a key, or its slot, in a
  # state file several people read. swarm-api creates each secret at run time.
  assert {
    condition = var.enable_provider_keys && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version|_iam_[a-z]+)?\"",
      file("../../terraform/bootstrap/provider_keys.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/provider_keys.tf"))) == 0
    error_message = "terraform/bootstrap/provider_keys.tf must declare no secret, no secret version and no per-secret IAM: keys live in Secret Manager only, written at run time"
  }

  # A custom role has no labels and an IAM member has none either: each says
  # managed-by=swarm-terraform in its description, as the destroy guard reads.
  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.provider_key_writer[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_member.provider_key_writer[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "the role and the grant's condition say managed-by=swarm-terraform in their descriptions"
  }
}

run "provider_key_grants_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition = alltrue([
      !var.enable_provider_keys,
      length(google_project_iam_custom_role.provider_key_writer) == 0,
      length(google_project_iam_member.provider_key_writer) == 0,
      length(data.google_project.provider_keys) == 0,
      output.provider_key_grants.api_write == "",
    ])
    error_message = "enable_provider_keys defaults to false and, false, defines and grants nothing"
  }
}
