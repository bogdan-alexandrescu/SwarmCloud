# The bootstrap IAM rows K1 to K4 that let swarm-workspace-apply make group
# and service workspaces, behind enable_workspace_kinds, off by default
# (docs/design/workspace-kinds.md §4.5, lane WK-IAM of #847). They live in
# terraform/bootstrap/workspace_deployer.tf (K1, K2), forge_user_slots.tf (K3)
# and variables.tf (K4, the switch). These runs hold:
#
#   * with enable_workspace_kinds = false, nothing of K1 to K3 is planned,
#     even with the deployer and the user slots on;
#   * K1, swarmFrontDoorAdmitter, holds exactly iap.webServices.getIamPolicy
#     and iap.webServices.setIamPolicy, and the deployer alone holds it, under
#     `resource.name == "projects/<NUMBER>/iap_web/compute/services/<generated
#     id of the API backend>"`: equality, never startsWith(); the API backend,
#     never the console's -ui-backend; the hasOnly() bound on the role it may
#     grant only once the owner's O1 shows IAP evaluates it;
#   * K2, the deployer's second secret-binder grant, EVALUATED, admits a
#     tenant's -git team slot and its -git-refresh twin and refuses its
#     provider key, its workspace secrets and its members' -git-u- slots;
#   * K3, swarm-api's three member-slot grants over any tenant, EVALUATED,
#     admits swarm-tenant-<t>-git-u-<hash> and its versions and refuses the
#     tenant's own -git slot;
#   * K1 needs both the switch and swarmFrontDoorAdmitter on
#     workspace_deployer_roles, whose default does not name it; the switch
#     alone makes K2 and K3 (the O1-fails shape);
#   * K4: swarmFrontDoorAdmitter is a reviewed deployer role; a role outside
#     the list, roles/iap.admin among them, is refused; the switch is refused
#     without the deployer or the user slots, and K1 without the API backend
#     in frontend_iap_backends.
#
#   * FRONT-DOOR-READ (part of #847, owner decision 2026-10-20):
#     swarmFrontDoorReader holds exactly iap.webServices.getIamPolicy and is
#     bound, unconditioned, on the API backend's OWN IAP policy for the
#     deployer alone -- never at the project, never on the console's
#     -ui-ui-backend. It is made only with the switch on and K1 off (K1's role
#     already carries the read), and refused without the API backend in
#     frontend_iap_backends.
#
# EVALUATED means the startsWith()/endsWith()/extract() emulation
# workspace_secrets.tftest.hcl uses: each expression is parsed into its
# literals and every name below is judged by them, so a widened prefix or a
# dropped arm changes the verdicts, not only the string.
#
# The slot hashes are built at run time (sha256 of a fixed string), never
# written out: nothing here may look like a credential.
#
# None of this proves what IAP or IAM do live. O1 (§4.5) is the owner's check
# that IAP accepts the permission names, matches the number-form name, and
# evaluates modifiedGrantsByRole.
#
# ORDER: the runs that only assert come first, the expect_failures runs last,
# because a run that ERRORS skips every run after it in this file.

mock_provider "google" {
  # The identities' empty policies are rendered by this data source; left to
  # the mock it is a random string the policy resources cannot parse.
  mock_data "google_iam_policy" {
    defaults = {
      policy_data = "{}"
    }
  }
}

# terraform/bootstrap/state_bucket.tf imports the state bucket (DAYONE-BOOT-1):
# a mock provider cannot import, so the bucket's provider values are the
# override's and its configuration is still planned and asserted.
override_resource {
  target = google_storage_bucket.state
}

variables {
  project_id           = "saga-agents-staging"
  frontend_iap_members = ["domain:example.com"]

  # Built from the parts so no line holds a 64-hex literal on its own.
  image_digest = "sha256:${join("", [for i in range(8) : "0123abcd"])}"

  # The default role list (docs/workspaces.md §2.3) and K1's role, which the
  # owner adds only once O1 has passed.
  roles_with_k1 = [
    "swarmWorkspaceAccountAdmin",
    "swarmWorkspaceProjectReader",
    "roles/resourcemanager.projectIamAdmin",
    "swarmWorkspaceBucketIam",
    "roles/storage.objectCreator",
    "swarmWorkspaceSecretBinder",
    "swarmForgeSlotCreator",
    "swarmWorkspaceFirestore",
    "roles/container.clusterViewer",
    "roles/logging.logWriter",
    "swarmFrontDoorAdmitter",
  ]
}

run "nothing_is_planned_while_the_switch_is_off" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  assert {
    condition = alltrue([
      var.enable_workspace_kinds == false,
      length(google_project_iam_custom_role.front_door_admitter) == 0,
      length(google_project_iam_member.front_door_admitter) == 0,
      length(google_project_iam_member.workspace_team_slot_binder) == 0,
      length(google_project_iam_member.forge_member_slots_any_tenant) == 0,
      length(google_project_iam_custom_role.front_door_reader) == 0,
      length(google_iap_web_backend_service_iam_member.front_door_reader) == 0,
    ])
    error_message = "enable_workspace_kinds is off by default, and while it is off no K1, K2 or K3 role or grant, and no front-door reader, is planned"
  }

  assert {
    condition     = output.workspace_kinds_grants == null
    error_message = "the K-rows output is null while the switch is off"
  }
}

run "the_rows_are_k1_to_k3_and_each_is_bounded" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    enable_workspace_kinds    = true
    workspace_deployer_roles  = var.roles_with_k1
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  # Different ids for the two backends, so a condition that named the
  # console's backend would carry its id and fail below.
  override_data {
    target = data.google_compute_backend_service.frontend_iap["swarm-ui-backend"]
    values = {
      name         = "swarm-ui-backend"
      generated_id = 4410000000000000001
    }
  }

  override_data {
    target = data.google_compute_backend_service.frontend_iap["swarm-ui-ui-backend"]
    values = {
      name         = "swarm-ui-ui-backend"
      generated_id = 4420000000000000002
    }
  }

  # ---- K1 ---------------------------------------------------------------

  assert {
    condition = alltrue([
      google_project_iam_custom_role.front_door_admitter[0].role_id == "swarmFrontDoorAdmitter",
      google_project_iam_custom_role.front_door_admitter[0].project == "saga-agents-staging",
      google_project_iam_custom_role.front_door_admitter[0].stage == "GA",
      startswith(google_project_iam_custom_role.front_door_admitter[0].description, "managed-by=swarm-terraform;"),
      length(google_project_iam_custom_role.front_door_admitter[0].description) <= 300,
    ])
    error_message = "K1's role is swarmFrontDoorAdmitter, GA in this project, describing who manages it in at most GCP's 300 characters"
  }

  assert {
    condition = toset(google_project_iam_custom_role.front_door_admitter[0].permissions) == toset([
      "iap.webServices.getIamPolicy",
      "iap.webServices.setIamPolicy",
    ])
    error_message = "swarmFrontDoorAdmitter holds exactly iap.webServices.getIamPolicy and setIamPolicy: no iap.web.* or iap.webTypes.* (every backend), no tunnel, no admin"
  }

  assert {
    condition = alltrue([
      length(google_project_iam_member.front_door_admitter) == 1,
      google_project_iam_member.front_door_admitter[0].member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.front_door_admitter[0].role == "projects/saga-agents-staging/roles/swarmFrontDoorAdmitter",
      google_project_iam_member.front_door_admitter[0].project == "saga-agents-staging",
      length(google_project_iam_member.front_door_admitter[0].condition) == 1,
      startswith(google_project_iam_member.front_door_admitter[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "K1 is one grant, to swarm-workspace-deployer alone, of swarmFrontDoorAdmitter, with exactly one condition"
  }

  # K1's role carries the read, so the reader is not made beside it.
  assert {
    condition = alltrue([
      length(google_project_iam_custom_role.front_door_reader) == 0,
      length(google_iap_web_backend_service_iam_member.front_door_reader) == 0,
    ])
    error_message = "with swarmFrontDoorAdmitter granted, swarmFrontDoorReader and its binding are not planned: the admitter already includes the read"
  }

  # Equality on the API backend's number-form name. Before O1, no hasOnly().
  assert {
    condition     = google_project_iam_member.front_door_admitter[0].condition[0].expression == "resource.name == \"projects/209012342332/iap_web/compute/services/4410000000000000001\""
    error_message = "K1's condition is resource.name == projects/<NUMBER>/iap_web/compute/services/<generated id of swarm-ui-backend>, and nothing else until O1"
  }

  assert {
    condition = alltrue([
      for f in ["startsWith", "endsWith", "extract(", "matches(", " || ", "4420000000000000002", "swarm-ui-ui-backend", "saga-agents-staging", "iap_web/compute/services/\"", "*"] :
      !strcontains(google_project_iam_member.front_door_admitter[0].condition[0].expression, f)
    ])
    error_message = "K1's condition is no prefix, suffix, extract or match, has no second arm, never names the console's -ui-backend by id or name, uses the project NUMBER, and names one backend"
  }

  # No K-row binding names the console's backend in any form.
  assert {
    condition = alltrue(flatten([
      for m in concat(
        google_project_iam_member.front_door_admitter,
        google_project_iam_member.workspace_team_slot_binder,
        values(google_project_iam_member.forge_member_slots_any_tenant),
        ) : [
        for f in ["4420000000000000002", "swarm-ui-ui-backend"] : !strcontains(jsonencode([m.role, m.member, m.condition]), f)
      ]
    ]))
    error_message = "no K-row binding names the console's -ui-backend"
  }

  # ---- K2 ---------------------------------------------------------------

  assert {
    condition = alltrue([
      length(google_project_iam_member.workspace_team_slot_binder) == 1,
      google_project_iam_member.workspace_team_slot_binder[0].member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.workspace_team_slot_binder[0].role == "projects/saga-agents-staging/roles/swarmWorkspaceSecretBinder",
      length(google_project_iam_member.workspace_team_slot_binder[0].condition) == 1,
      google_project_iam_member.workspace_team_slot_binder[0].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\") && (resource.name.endsWith(\"-git\") || resource.name.endsWith(\"-git-refresh\"))",
      startswith(google_project_iam_member.workspace_team_slot_binder[0].condition[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "K2 is a second swarmWorkspaceSecretBinder grant to the deployer, bounded to swarm-tenant-*-git and -git-refresh by full name with the project NUMBER"
  }

  # The personal grant is unchanged beside it.
  assert {
    condition     = google_project_iam_member.workspace_deployer["swarmWorkspaceSecretBinder"].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-u-\")"
    error_message = "the first secret-binder grant stays personal tenants' secrets only"
  }

  # EVALUATED: startsWith() as startswith(), endsWith() as endswith().
  assert {
    condition = alltrue([
      for h in [substr(sha256("wk-iam team slot"), 0, 16)] : alltrue([
        for c in [{
          parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && \\(resource\\.name\\.endsWith\\(\"([^\"]+)\"\\) \\|\\| resource\\.name\\.endsWith\\(\"([^\"]+)\"\\)\\)$", google_project_iam_member.workspace_team_slot_binder[0].condition[0].expression)
          admitted = [
            "projects/209012342332/secrets/swarm-tenant-eng-git",
            "projects/209012342332/secrets/swarm-tenant-eng-git-refresh",
            "projects/209012342332/secrets/swarm-tenant-research-git",
            "projects/209012342332/secrets/swarm-tenant-research-git-refresh",
            "projects/209012342332/secrets/swarm-tenant-u-sw-c90291-git",
          ]
          refused = [
            "projects/209012342332/secrets/swarm-tenant-eng-anthropic",
            "projects/209012342332/secrets/swarm-tenant-eng-anthropic-refresh",
            "projects/209012342332/secrets/swarm-tenant-eng-wsec-x",
            "projects/209012342332/secrets/swarm-tenant-eng-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant-eng-git-u-${h}",
            "projects/209012342332/secrets/swarm-tenant-eng-git-u-${h}-refresh",
            "projects/209012342332/secrets/swarm-tenant-eng-git-r-${h}",
            "projects/209012342332/secrets/swarm-tenant-eng-typesafe",
            "projects/209012342332/secrets/swarm-tenant-eng-mcpc-${h}",
            "projects/209012342332/secrets/swarm-github-app-git",
            "projects/209012342332/secrets/agents-staging-git",
            "projects/209012342332/secrets/promptlab-git",
            "projects/111111111111/secrets/swarm-tenant-eng-git",
            "projects/2090123423320/secrets/swarm-tenant-eng-git",
            "projects/saga-agents-staging/secrets/swarm-tenant-eng-git",
          ]
          }] : alltrue([
          for n in concat(c.admitted, c.refused) :
          (startswith(n, c.parts[0]) && (endswith(n, c.parts[1]) || endswith(n, c.parts[2]))) == contains(c.admitted, n)
        ])
      ])
    ])
    error_message = "K2 must admit a tenant's -git team slot and its -git-refresh twin, and refuse its provider keys, workspace secrets, typesafe and MCP secrets, its members' -git-u- and repository -git-r- slots, the platform's own secrets, the other team's, another project's and the project-ID spelling"
  }

  # ---- K3 ---------------------------------------------------------------

  assert {
    condition = alltrue([
      toset(keys(google_project_iam_member.forge_member_slots_any_tenant)) == toset(["version_adder", "version_manager", "slot_reader"]),
      google_project_iam_member.forge_member_slots_any_tenant["version_adder"].role == "roles/secretmanager.secretVersionAdder",
      google_project_iam_member.forge_member_slots_any_tenant["version_manager"].role == "projects/saga-agents-staging/roles/swarmForgeSlotVersionManager",
      google_project_iam_member.forge_member_slots_any_tenant["slot_reader"].role == "roles/secretmanager.secretAccessor",
    ])
    error_message = "K3 is swarm-api's three forge-slot roles, the version adder, the version manager and the slot reader, one any-tenant binding each"
  }

  assert {
    condition = alltrue([
      for k, m in google_project_iam_member.forge_member_slots_any_tenant :
      m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      && length(m.condition) == 1
      && m.condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-git-u-\") != \"\""
      && startswith(m.condition[0].description, "managed-by=swarm-terraform;")
    ])
    error_message = "every K3 binding is swarm-api's, under swarm-tenant-<t>-git-u- by extract() with the project NUMBER, and nothing else"
  }

  # EVALUATED, as workspace_secrets.tftest.hcl evaluates extract(): the text
  # between the first pre and the first suf after it, "" when either is
  # missing.
  assert {
    condition = alltrue([
      for h in [substr(sha256("wk-iam member slot"), 0, 16)] : alltrue([
        for c in [{
          parts = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"{]+)\\{[a-z]+\\}([^\"]+)\"\\) != \"\"$", google_project_iam_member.forge_member_slots_any_tenant["slot_reader"].condition[0].expression)
          admitted = [
            "projects/209012342332/secrets/swarm-tenant-research-git-u-${h}",
            "projects/209012342332/secrets/swarm-tenant-research-git-u-${h}/versions/1",
            "projects/209012342332/secrets/swarm-tenant-research-git-u-${h}-refresh",
            "projects/209012342332/secrets/swarm-tenant-research-git-u-${h}-refresh/versions/latest",
            "projects/209012342332/secrets/swarm-tenant-eng-git-u-${h}",
            "projects/209012342332/secrets/swarm-tenant-u-alice-git-u-${h}",
          ]
          refused = [
            "projects/209012342332/secrets/swarm-tenant-research-git",
            "projects/209012342332/secrets/swarm-tenant-research-git/versions/1",
            "projects/209012342332/secrets/swarm-tenant-research-git-refresh",
            "projects/209012342332/secrets/swarm-tenant-research-git-r-${h}",
            "projects/209012342332/secrets/swarm-tenant-research-anthropic",
            "projects/209012342332/secrets/swarm-tenant-research-wsec-${h}",
            "projects/209012342332/secrets/swarm-tenant--git-u-${h}",
            "projects/209012342332/secrets/swarm-tenant-git-u-${h}",
            "projects/209012342332/secrets/swarm-github-app-git-u-${h}",
            "projects/209012342332/secrets/agents-research-git-u-${h}",
            "projects/111111111111/secrets/swarm-tenant-research-git-u-${h}",
            "projects/saga-agents-staging/secrets/swarm-tenant-research-git-u-${h}",
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
    error_message = "K3 must admit every tenant's members' -git-u- slots, twins and versions, and refuse the tenant's own -git slot and twin, its repository slots, provider keys and workspace secrets, an empty tenant, the platform's and the other team's secrets, another project's and the project-ID spelling"
  }

  # ---- what the owner reads the plan against -----------------------------

  assert {
    condition = alltrue([
      output.workspace_kinds_grants.front_door_admit == google_project_iam_member.front_door_admitter[0].condition[0].expression,
      output.workspace_kinds_grants.front_door_backend == "swarm-ui-backend",
      output.workspace_kinds_grants.team_slot_bind == google_project_iam_member.workspace_team_slot_binder[0].condition[0].expression,
      output.workspace_kinds_grants.member_slots == google_project_iam_member.forge_member_slots_any_tenant["slot_reader"].condition[0].expression,
    ])
    error_message = "the output the owner reads a plan against carries the conditions the grants carry"
  }
}

# After O1 proves IAP evaluates modifiedGrantsByRole, the owner turns the
# bound on: K1 may then add or remove members of the accessor role only.
run "o1s_bound_appends_hasonly_on_the_accessor_role" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer   = true
    enable_forge_user_slots     = true
    enable_workspace_kinds      = true
    front_door_admit_role_bound = true
    workspace_deployer_roles    = var.roles_with_k1
    workspace_apply_image       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_compute_backend_service.frontend_iap["swarm-ui-backend"]
    values = {
      name         = "swarm-ui-backend"
      generated_id = 4410000000000000001
    }
  }

  override_data {
    target = data.google_compute_backend_service.frontend_iap["swarm-ui-ui-backend"]
    values = {
      name         = "swarm-ui-ui-backend"
      generated_id = 4420000000000000002
    }
  }

  assert {
    condition     = google_project_iam_member.front_door_admitter[0].condition[0].expression == "resource.name == \"projects/209012342332/iap_web/compute/services/4410000000000000001\" && api.getAttribute(\"iam.googleapis.com/modifiedGrantsByRole\", []).hasOnly([\"roles/iap.httpsResourceAccessor\"])"
    error_message = "with O1's proof, K1 is the same equality AND hasOnly() over exactly roles/iap.httpsResourceAccessor"
  }
}

# K4: the new role is a reviewed one, so the role list may name it.
run "the_front_door_admitter_is_a_reviewed_role" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_deployer_roles = ["swarmWorkspaceProjectReader", "swarmWorkspaceSecretBinder", "swarmFrontDoorAdmitter"]
  }

  assert {
    condition     = contains(var.workspace_deployer_roles, "swarmFrontDoorAdmitter")
    error_message = "swarmFrontDoorAdmitter is on the reviewed list of swarm-workspace-deployer's roles"
  }
}

# The switch alone, with the default role list: K2 and K3, and no K1. This is
# the shape the owner applies if O1 shows the number-form name does not match,
# and S2 stays an owner step.
run "the_switch_alone_makes_k2_and_k3_and_not_k1" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    enable_workspace_kinds    = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  # Different names and ids for the two backends, so a reader bound on the
  # console's backend would carry its name and fail below.
  override_data {
    target = data.google_compute_backend_service.frontend_iap["swarm-ui-backend"]
    values = {
      name         = "swarm-ui-backend"
      generated_id = 4410000000000000001
    }
  }

  override_data {
    target = data.google_compute_backend_service.frontend_iap["swarm-ui-ui-backend"]
    values = {
      name         = "swarm-ui-ui-backend"
      generated_id = 4420000000000000002
    }
  }

  assert {
    condition = alltrue([
      !contains(var.workspace_deployer_roles, "swarmFrontDoorAdmitter"),
      contains(var.workspace_deployer_roles, "swarmWorkspaceProjectReader"),
      length(google_project_iam_custom_role.front_door_admitter) == 0,
      length(google_project_iam_member.front_door_admitter) == 0,
      length(google_project_iam_member.workspace_team_slot_binder) == 1,
      length(google_project_iam_member.forge_member_slots_any_tenant) == 3,
    ])
    error_message = "the default role list does not name swarmFrontDoorAdmitter, and keeps swarmWorkspaceProjectReader (K4, unchanged); with the switch alone K2 and K3 are made and K1 is not"
  }

  # ---- FRONT-DOOR-READ: the read alone, on the API backend alone ----------

  assert {
    condition = alltrue([
      length(google_project_iam_custom_role.front_door_reader) == 1,
      google_project_iam_custom_role.front_door_reader[0].role_id == "swarmFrontDoorReader",
      google_project_iam_custom_role.front_door_reader[0].project == "saga-agents-staging",
      google_project_iam_custom_role.front_door_reader[0].stage == "GA",
      startswith(google_project_iam_custom_role.front_door_reader[0].description, "managed-by=swarm-terraform;"),
      length(google_project_iam_custom_role.front_door_reader[0].description) <= 300,
    ])
    error_message = "the reader's role is swarmFrontDoorReader, GA in this project, describing who manages it in at most GCP's 300 characters"
  }

  assert {
    condition     = toset(google_project_iam_custom_role.front_door_reader[0].permissions) == toset(["iap.webServices.getIamPolicy"])
    error_message = "swarmFrontDoorReader holds exactly iap.webServices.getIamPolicy: no setIamPolicy, no iap.web.* or iap.webTypes.*, no settings, no tunnel"
  }

  assert {
    condition = alltrue([
      length(google_iap_web_backend_service_iam_member.front_door_reader) == 1,
      google_iap_web_backend_service_iam_member.front_door_reader[0].web_backend_service == "swarm-ui-backend",
      google_iap_web_backend_service_iam_member.front_door_reader[0].project == "saga-agents-staging",
      google_iap_web_backend_service_iam_member.front_door_reader[0].member == "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com",
      google_iap_web_backend_service_iam_member.front_door_reader[0].role == "projects/saga-agents-staging/roles/swarmFrontDoorReader",
      length(google_iap_web_backend_service_iam_member.front_door_reader[0].condition) == 0,
    ])
    error_message = "the reader is one binding of swarmFrontDoorReader, to swarm-workspace-deployer alone, on the API backend swarm-ui-backend's own IAP policy, with no condition"
  }

  # Never at the project, and nothing names the deployer on the console's backend.
  assert {
    condition = alltrue(concat(
      [for m in google_project_iam_member.workspace_deployer : !strcontains(m.role, "swarmFrontDoorReader")],
      [for b in google_iap_web_backend_service_iam_member.frontend_accessors : b.member != "serviceAccount:swarm-workspace-deployer@saga-agents-staging.iam.gserviceaccount.com"],
      [google_iap_web_backend_service_iam_member.front_door_reader[0].web_backend_service != "swarm-ui-ui-backend"],
    ))
    error_message = "swarmFrontDoorReader is never granted at the project, and swarm-workspace-deployer is named on no IAP backend but the API backend"
  }

  assert {
    condition     = output.workspace_kinds_grants.front_door_reader == "swarm-ui-backend"
    error_message = "the output the owner reads a plan against names the one backend the reader is bound on"
  }
}

# ---------------------------------------------------------------------------
# The refusals. Last: each must FAIL, and a run that errors skips the rest.
# ---------------------------------------------------------------------------

run "a_role_outside_the_list_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    workspace_deployer_roles = ["swarmFrontDoorAdmitter", "roles/iap.admin"]
  }

  expect_failures = [var.workspace_deployer_roles]
}

run "the_switch_without_the_deployer_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_kinds  = true
    enable_forge_user_slots = true
  }

  expect_failures = [var.enable_workspace_kinds]
}

run "the_switch_without_the_user_slots_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_kinds    = true
    enable_workspace_deployer = true
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
    # The slot creator off too, so its own precondition (workspace_deployer.tf)
    # is not the failure this run sees.
    workspace_deployer_roles = ["swarmWorkspaceProjectReader", "swarmWorkspaceSecretBinder", "swarmFrontDoorAdmitter"]
  }

  expect_failures = [var.enable_workspace_kinds]
}

# K1 needs the API backend's id; with frontend_iap_backends = [] (a fresh
# project's first apply) there is none, and the grant is refused at plan
# rather than made on a name that matches nothing.
run "k1_without_the_api_backend_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    enable_workspace_kinds    = true
    frontend_iap_backends     = []
    workspace_deployer_roles  = var.roles_with_k1
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  expect_failures = [google_project_iam_member.front_door_admitter]
}

# The reader needs the API backend; with frontend_iap_backends = [] there is
# none, and the binding is refused at plan rather than made on no backend.
run "the_front_door_reader_without_the_api_backend_is_refused" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    enable_workspace_deployer = true
    enable_forge_user_slots   = true
    enable_workspace_kinds    = true
    frontend_iap_backends     = []
    workspace_apply_image     = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/workspace-apply@${var.image_digest}"
  }

  override_data {
    target = data.google_project.workspace_deployer
    values = {
      number = "209012342332"
    }
  }

  override_data {
    target = data.google_project.forge_user_slots
    values = {
      number = "209012342332"
    }
  }

  expect_failures = [google_iap_web_backend_service_iam_member.front_door_reader]
}
