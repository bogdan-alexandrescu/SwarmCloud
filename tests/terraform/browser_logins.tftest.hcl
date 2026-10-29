# Signed-in browser sessions are kept for a person's own workspace only
# (docs/design/live-browser.md LB-7 and §5.5.1; lane L8b of #1030).
#
# A session -- a cookie jar plus origin storage -- is stored as a Secret
# Manager secret `swarm-tenant-u-<user>-browser-wf-<workflow>` (a workflow's
# carry, 24-hour TTL) or `swarm-tenant-u-<user>-browser-login-<name>` (a named
# login). swarm-api creates each one and grants that tenant's worker read and
# version-add on that one secret (owner decision 2026-10-11, lane L8c);
# terraform/bootstrap/browser_logins.tf makes swarm-api's grants for it. These
# runs hold:
#
#   * NO worker -- a group tenant's, a service tenant's or a personal one's --
#     holds any project-level grant for a `-browser-` secret, in this root or
#     in anything CI applies, and no worker holds secrets.create;
#   * swarm-api provisions those secrets under a condition whose name arm is
#     the revoker's own, and whose only other arm is the project create is
#     evaluated on;
#   * swarm-api may destroy those secrets (delete, destroy versions) and can
#     never read one;
#   * no secret or secret version is declared, so no session is in any state;
#   * every resource says managed-by=swarm-terraform;
#   * nothing exists until enable_browser_logins is set.
#
# The conditions are EVALUATED as well as compared: the prefix is read back out
# of each planned expression and applied to secret names the way IAM applies
# startsWith() and extract(). None of this proves what IAM does live; see the
# file's "NOT VERIFIED LIVE".

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
}

run "no_worker_holds_a_browser_grant_even_with_personal_tenants_in_tfvars" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.browser_logins
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_browser_logins = true
    infra_tenants_tfvars  = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
  }

  # The fixture's two personal tenants are parsed -- so the grants below are
  # judged with personal tenants present, the case L8b granted workers in.
  assert {
    condition     = toset(local.infra_tenant_ids) == toset(["eng", "u-grp", "svc-bot", "u-alice", "u-verify"])
    error_message = "the fixture's five tenants were not all parsed; the no-worker-grant assertions would be judged on a partial list"
  }

  # Every grant this file makes is swarm-api's, and nobody else's.
  assert {
    condition = alltrue([
      for m in concat(
        google_project_iam_member.browser_session_provisioner,
        google_project_iam_member.browser_login_revoker,
      ) : m.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    ]) && length(google_project_iam_member.browser_session_provisioner) == 1 && length(google_project_iam_member.browser_login_revoker) == 1
    error_message = "swarm-api alone holds the browser-session grants: it provisions each secret and grants its worker on that secret (owner decision 2026-10-11, L8c)"
  }

  # No worker appears in this file at all: not by the module's ids, not by the
  # account's spelling. L8b's reader, writer and unconditioned creator are gone.
  assert {
    condition = var.enable_browser_logins && length(regexall(
      "worker_ids|swarm-agent-worker|browser_session_(reader|writer|creator)|swarmBrowserSessionCreator",
      join("\n", [for l in split("\n", file("../../terraform/bootstrap/browser_logins.tf")) : l if !startswith(trimspace(l), "#")]),
    )) == 0
    error_message = "terraform/bootstrap/browser_logins.tf names a worker or one of L8b's worker grants outside a comment; a worker holds no project-level browser-session grant and never secrets.create"
  }

  # Nor anywhere else: no .tf file in the bootstrap root, terraform/infra or
  # the modules defines the retired worker grants or role (comments, which
  # record why they went, aside).
  assert {
    condition = var.enable_browser_logins && length(flatten([
      for f in concat(
        [for p in fileset("../../terraform/infra", "**/*.tf") : "../../terraform/infra/${p}"],
        [for p in fileset("../../terraform/modules", "**/*.tf") : "../../terraform/modules/${p}"],
        [for p in fileset("../../terraform/bootstrap", "**/*.tf") : "../../terraform/bootstrap/${p}"],
        ) : regexall(
        "swarmBrowserSessionCreator|\"browser_session_(reader|writer|creator)\"",
        join("\n", [for l in split("\n", file(f)) : l if !startswith(trimspace(l), "#")]),
      )
    ])) == 0
    error_message = "a retired worker browser-session grant or swarmBrowserSessionCreator is still defined somewhere in terraform/"
  }
}

run "swarm_api_provisions_browser_secrets_under_the_condition" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.browser_logins
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_browser_logins = true
    infra_tenants_tfvars  = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
  }

  assert {
    condition = google_project_iam_custom_role.browser_session_provisioner[0].permissions == toset([
      "secretmanager.secrets.create",
      "secretmanager.secrets.get",
      "secretmanager.secrets.getIamPolicy",
      "secretmanager.secrets.setIamPolicy",
    ])
    error_message = "swarmBrowserSessionProvisioner creates a browser secret, reads its metadata and its IAM policy, and sets that policy -- nothing else: no versions.access (swarm-api never returns a value), no versions.add (it never plants a jar), no delete (the revoker's)"
  }

  assert {
    condition = alltrue([
      google_project_iam_custom_role.browser_session_provisioner[0].role_id == "swarmBrowserSessionProvisioner",
      google_project_iam_member.browser_session_provisioner[0].role == "projects/saga-agents-staging/roles/swarmBrowserSessionProvisioner",
    ])
    error_message = "the provisioner's role id is the one terraform/modules/custom_role_ids spells, granted through the resource"
  }

  # The role carries the condition, and exactly one.
  assert {
    condition     = length(google_project_iam_member.browser_session_provisioner[0].condition) == 1
    error_message = "swarm-api's provision grant carries exactly one condition; unconditioned, setIamPolicy would reach every secret in the shared project"
  }

  # The exact string: the project arm, OR the revoker's own name test. The
  # revoker run below evaluates that name test against every name; being the
  # same bytes, it narrows get, getIamPolicy and setIamPolicy the same way.
  assert {
    condition     = google_project_iam_member.browser_session_provisioner[0].condition[0].expression == "resource.type == \"cloudresourcemanager.googleapis.com/Project\" || (${google_project_iam_member.browser_login_revoker[0].condition[0].expression})"
    error_message = "swarm-api's provision condition changed; it must be the project (where secrets.create is evaluated) or the revoker's personal -browser- name test, and nothing else"
  }

  assert {
    condition     = google_project_iam_member.browser_session_provisioner[0].condition[0].expression == "resource.type == \"cloudresourcemanager.googleapis.com/Project\" || (resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-u-\") && resource.name.extract(\"/secrets/swarm-tenant-u-{tenant}-browser-\") != \"\")"
    error_message = "swarm-api's provision condition must name personal tenants' -browser- secrets by the project NUMBER and extract(), never matches()"
  }

  # EVALUATED. For a SECRET (the type every check but create is made on) the
  # project arm is false, so the grant holds exactly where the name arm does;
  # emulated as in the revoker run.
  assert {
    condition = alltrue([
      for c in [{
        expr = google_project_iam_member.browser_session_provisioner[0].condition[0].expression
        admitted = [
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser-wf-wf_1",
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser-login-github-qa",
          "projects/209012342332/secrets/swarm-tenant-u-someone-not-in-terraform-browser-login-x",
        ]
        refused = [
          "projects/209012342332/secrets/swarm-tenant-eng-browser-login-x",
          "projects/209012342332/secrets/swarm-tenant-u-alice-anthropic",
          "projects/209012342332/secrets/swarm-tenant-u-alice-git-u-0123456789abcdef",
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser",
          "projects/209012342332/secrets/swarm-tenant-u--browser-login-x",
          "projects/209012342332/secrets/swarm-github-app-private-key",
          "projects/209012342332/secrets/agents-staging-browser-login-x",
          "projects/111111111111/secrets/swarm-tenant-u-alice-browser-login-x",
        ]
        }] : alltrue([
        for n in concat(c.admitted, c.refused) : (
          startswith(n, regex("^resource\\.type == \"cloudresourcemanager\\.googleapis\\.com/Project\" \\|\\| \\(resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"/secrets/swarm-tenant-u-\\{tenant\\}-browser-\"\\) != \"\"\\)$", c.expr)[0]) &&
          length(split("/secrets/swarm-tenant-u-", n)) > 1 &&
          length(split("-browser-", join("/secrets/swarm-tenant-u-", slice(split("/secrets/swarm-tenant-u-", n), 1, length(split("/secrets/swarm-tenant-u-", n)))))) > 1 &&
          split("-browser-", join("/secrets/swarm-tenant-u-", slice(split("/secrets/swarm-tenant-u-", n), 1, length(split("/secrets/swarm-tenant-u-", n)))))[0] != ""
        ) == contains(c.admitted, n)
      ])
    ])
    error_message = "swarm-api's provision condition must admit, on a secret, every personal tenant's -browser- secrets and refuse a group tenant's, a personal tenant's provider and forge secrets, the platform's and the other team's"
  }
}

run "swarm_api_may_destroy_browser_secrets_and_never_read_one" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.browser_logins
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_browser_logins = true
    infra_tenants_tfvars  = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
  }

  assert {
    condition = google_project_iam_custom_role.browser_login_revoker[0].permissions == toset([
      "secretmanager.secrets.delete",
      "secretmanager.secrets.get",
      "secretmanager.versions.destroy",
      "secretmanager.versions.list",
    ])
    error_message = "swarmBrowserLoginRevoker deletes a browser secret, destroys and lists its versions, and reads its metadata -- nothing else: no versions.access (swarm-api never returns a value), no versions.add (it never plants a jar), no IAM"
  }

  assert {
    condition     = google_project_iam_custom_role.browser_login_revoker[0].role_id == "swarmBrowserLoginRevoker"
    error_message = "the role id is the one terraform/modules/custom_role_ids spells"
  }

  assert {
    condition = alltrue([
      google_project_iam_member.browser_login_revoker[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
      google_project_iam_member.browser_login_revoker[0].role == "projects/saga-agents-staging/roles/swarmBrowserLoginRevoker",
      length(google_project_iam_member.browser_login_revoker) == 1,
    ])
    error_message = "swarm-api, alone, holds the revoker role: it revokes a named login and deletes a workflow's carry at its terminal state"
  }

  # The exact string, so any edit is a deliberate one this file must follow.
  assert {
    condition     = google_project_iam_member.browser_login_revoker[0].condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-u-\") && resource.name.extract(\"/secrets/swarm-tenant-u-{tenant}-browser-\") != \"\""
    error_message = "swarm-api's revoke condition changed; it must admit every personal tenant's -browser- secrets, workspaces made outside Terraform included, and nothing else"
  }

  # EVALUATED. extract("pre{x}suf") is the text between the FIRST pre and the
  # first suf after it, "" when either is missing (Google's attribute
  # reference); split() on the literal pre and suf does exactly that.
  assert {
    condition = alltrue([
      for c in [{
        expr = google_project_iam_member.browser_login_revoker[0].condition[0].expression
        admitted = [
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser-wf-wf_1",
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser-login-github-qa",
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser-login-github-qa/versions/7",
          "projects/209012342332/secrets/swarm-tenant-u-someone-not-in-terraform-browser-login-x",
        ]
        refused = [
          "projects/209012342332/secrets/swarm-tenant-eng-browser-login-x",
          "projects/209012342332/secrets/swarm-tenant-u-alice-anthropic",
          "projects/209012342332/secrets/swarm-tenant-u-alice-git-u-0123456789abcdef",
          "projects/209012342332/secrets/swarm-tenant-u-alice-browser",
          "projects/209012342332/secrets/swarm-tenant-u--browser-login-x",
          "projects/209012342332/secrets/swarm-github-app-private-key",
          "projects/209012342332/secrets/agents-staging-browser-login-x",
          "projects/111111111111/secrets/swarm-tenant-u-alice-browser-login-x",
        ]
        }] : alltrue(concat(
        [
          for n in concat(c.admitted, c.refused) : (
            startswith(n, regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"/secrets/swarm-tenant-u-\\{tenant\\}-browser-\"\\) != \"\"$", c.expr)[0]) &&
            length(split("/secrets/swarm-tenant-u-", n)) > 1 &&
            length(split("-browser-", join("/secrets/swarm-tenant-u-", slice(split("/secrets/swarm-tenant-u-", n), 1, length(split("/secrets/swarm-tenant-u-", n)))))) > 1 &&
            split("-browser-", join("/secrets/swarm-tenant-u-", slice(split("/secrets/swarm-tenant-u-", n), 1, length(split("/secrets/swarm-tenant-u-", n)))))[0] != ""
          ) == contains(c.admitted, n)
        ],
      ))
    ])
    error_message = "swarm-api's revoke condition must admit every personal tenant's -browser- secrets and their versions, and refuse a group tenant's, a personal tenant's provider and forge secrets, the platform's and the other team's"
  }
}

run "no_session_in_state_and_every_resource_labelled" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.browser_logins
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    enable_browser_logins = true
    infra_tenants_tfvars  = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
  }

  # A secret or a version declared here would put a session -- or at least
  # its slot -- in a state file several people read. swarm-api creates each
  # secret at run time; Terraform declares neither.
  assert {
    condition = var.enable_browser_logins && length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version)?\"",
      file("../../terraform/bootstrap/browser_logins.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/browser_logins.tf"))) == 0
    error_message = "terraform/bootstrap/browser_logins.tf must declare no secret and no secret version: sessions live in Secret Manager only, written at run time"
  }

  # A custom role has no labels; it says so in its description. An IAM member
  # has none either; a conditioned one says so in its condition's description.
  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.browser_session_provisioner[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_custom_role.browser_login_revoker[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "every custom role this file defines says managed-by=swarm-terraform in its description"
  }

  assert {
    condition = alltrue(flatten([
      for grants in [
        google_project_iam_member.browser_session_provisioner,
        google_project_iam_member.browser_login_revoker,
      ] : [for m in grants : startswith(m.condition[0].description, "managed-by=swarm-terraform;")]
    ]))
    error_message = "every conditioned grant this file makes says managed-by=swarm-terraform in its condition's description"
  }

  # Nothing CI applies grants on a browser session: terraform/infra and its
  # modules never spell the -browser- prefix, so no release can hand any
  # worker one. And in this root, browser_logins.tf alone does.
  assert {
    condition = var.enable_browser_logins && length(flatten([
      for f in concat(
        [for p in fileset("../../terraform/infra", "**/*.tf") : "../../terraform/infra/${p}"],
        [for p in fileset("../../terraform/modules", "**/*.tf") : "../../terraform/modules/${p}"],
        [for p in fileset("../../terraform/bootstrap", "**/*.tf") : "../../terraform/bootstrap/${p}" if p != "browser_logins.tf"],
      ) : regexall("-browser-", file(f))
    ])) == 0
    error_message = "a -browser- secret prefix is spelled outside terraform/bootstrap/browser_logins.tf; every grant on a browser session must be made there, where this suite holds it"
  }

  assert {
    condition     = var.enable_browser_logins && length(fileset("../../terraform/infra", "**/*.tf")) > 5 && length(fileset("../../terraform/bootstrap", "**/*.tf")) > 5
    error_message = "the scan above read too few files to mean anything; check its paths"
  }
}

run "browser_grants_are_absent_until_switched_on" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    infra_tenants_tfvars = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
  }

  assert {
    condition = alltrue([
      !var.enable_browser_logins,
      length(google_project_iam_custom_role.browser_session_provisioner) == 0,
      length(google_project_iam_custom_role.browser_login_revoker) == 0,
      length(google_project_iam_member.browser_session_provisioner) == 0,
      length(google_project_iam_member.browser_login_revoker) == 0,
      length(data.google_project.browser_logins) == 0,
    ])
    error_message = "enable_browser_logins defaults to false and, false, defines and grants nothing"
  }
}
