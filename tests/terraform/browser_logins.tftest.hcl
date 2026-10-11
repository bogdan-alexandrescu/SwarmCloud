# Signed-in browser sessions are kept for a person's own workspace only
# (docs/design/live-browser.md LB-7 and §5.5.1; lane L8b of #1030).
#
# A session -- a cookie jar plus origin storage -- is stored as a Secret
# Manager secret `swarm-tenant-u-<user>-browser-wf-<workflow>` (a workflow's
# carry, 24-hour TTL) or `swarm-tenant-u-<user>-browser-login-<name>` (a named
# login). terraform/bootstrap/browser_logins.tf makes the IAM for them. These
# runs hold:
#
#   * only a PERSONAL tenant's worker -- `kind = "user"` with a `u-` id -- holds
#     a grant, and only on its own `swarm-tenant-<tenant>-browser-` prefix, in
#     the full `projects/<number>/secrets/` form IAM evaluates;
#   * no group tenant's worker, and no service tenant's whose id is not `u-`,
#     holds any `-browser-` grant, in this root or in anything CI applies;
#   * swarm-api may destroy those secrets (delete, destroy versions) and can
#     never read one;
#   * no secret or secret version is declared, so no session is in any state;
#   * every resource says managed-by=swarm-terraform;
#   * a tenant whose browser prefix nests inside another's is refused;
#   * nothing exists until enable_browser_logins is set.
#
# The conditions are EVALUATED as well as compared: the prefix is read back out
# of each planned expression and applied to secret names the way IAM applies
# startsWith() and extract(). None of this proves what IAM does live; see the
# file's "NOT VERIFIED LIVE".

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id = "saga-agents-staging"

  # Required by terraform/bootstrap; the value only has to pass validation.
  frontend_iap_members = ["domain:example.com"]
}

run "only_personal_workers_hold_a_grant_on_their_own_prefix" {
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

  # The parse found every tenant of the fixture, and exactly the two personal
  # ones are personal: not the group, not the `u-` group, not svc-bot.
  assert {
    condition     = toset(local.infra_tenant_ids) == toset(["eng", "u-grp", "svc-bot", "u-alice", "u-verify"])
    error_message = "the fixture's five tenants were not all parsed; the personal-tenant filter would be judged on a partial list"
  }

  assert {
    condition     = local.browser_login_tenants == toset(["u-alice", "u-verify"])
    error_message = "browser sessions belong to personal tenants only: kind = \"user\" AND an id starting u- (LB-7). Got ${jsonencode(local.browser_login_tenants)}"
  }

  # Each of the worker's three grants is made for exactly those tenants.
  assert {
    condition = alltrue([
      for grants in [
        google_project_iam_member.browser_session_creator,
        google_project_iam_member.browser_session_reader,
        google_project_iam_member.browser_session_writer,
      ] : toset(keys(grants)) == toset(["u-alice", "u-verify"])
    ])
    error_message = "the worker's create, read and write grants are made for every personal tenant and for no other"
  }

  # Each grant names that tenant's own worker, and nobody else's.
  assert {
    condition = alltrue(flatten([
      for grants in [
        google_project_iam_member.browser_session_creator,
        google_project_iam_member.browser_session_reader,
        google_project_iam_member.browser_session_writer,
      ] : [
        for t, m in grants : m.member == "serviceAccount:swarm-agent-worker-${t}@saga-agents-staging.iam.gserviceaccount.com"
      ]
    ]))
    error_message = "a personal tenant's browser grant must name swarm-agent-worker-<that tenant> and nobody else"
  }

  # No worker of a group tenant, or of a service tenant that is not personal,
  # appears as a member of any grant this file makes.
  assert {
    condition = alltrue(flatten([
      for grants in [
        values(google_project_iam_member.browser_session_creator),
        values(google_project_iam_member.browser_session_reader),
        values(google_project_iam_member.browser_session_writer),
        google_project_iam_member.browser_login_revoker,
      ] : [
        for m in grants : alltrue([
          for t in ["eng", "u-grp", "svc-bot"] : m.member != "serviceAccount:swarm-agent-worker-${t}@saga-agents-staging.iam.gserviceaccount.com"
        ])
      ]
    ]))
    error_message = "a group tenant's worker is shared by every member of the group, so it must hold no -browser- grant at all (LB-7)"
  }

  # Read is secretAccessor and write is secretVersionAdder, each on exactly
  # the tenant's own full-form prefix.
  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.browser_session_reader :
      m.role == "roles/secretmanager.secretAccessor" &&
      m.condition[0].expression == "resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-${t}-browser-\")"
    ])
    error_message = "a personal worker reads its own swarm-tenant-<t>-browser- secrets and no others, by the project NUMBER"
  }

  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.browser_session_writer :
      m.role == "roles/secretmanager.secretVersionAdder" &&
      m.condition[0].expression == google_project_iam_member.browser_session_reader[t].condition[0].expression
    ])
    error_message = "a personal worker adds versions (a carried jar, a refreshed login) to its own browser secrets only, on the reader's own condition"
  }

  # EVALUATED. The prefix is read out of each planned expression -- a regex
  # that refuses any other shape -- and every name below is put through it.
  assert {
    condition = alltrue(flatten([
      for t, m in google_project_iam_member.browser_session_reader : [
        for c in [{
          prefix = regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\)$", m.condition[0].expression)[0]
          own = [
            "projects/209012342332/secrets/swarm-tenant-${t}-browser-wf-wf_1",
            "projects/209012342332/secrets/swarm-tenant-${t}-browser-login-github-qa",
            "projects/209012342332/secrets/swarm-tenant-${t}-browser-login-github-qa/versions/3",
          ]
          others = concat(
            [
              for o in ["eng", "u-grp", "svc-bot", "u-alice", "u-verify"] :
              "projects/209012342332/secrets/swarm-tenant-${o}-browser-login-github-qa" if o != t
            ],
            [
              "projects/209012342332/secrets/swarm-tenant-${t}-anthropic",
              "projects/209012342332/secrets/swarm-tenant-${t}-git",
              "projects/209012342332/secrets/swarm-tenant-${t}-git-u-0123456789abcdef",
              "projects/209012342332/secrets/swarm-tenant-${t}-browser",
              "projects/209012342332/secrets/agents-staging-browser-login-x",
              "projects/111111111111/secrets/swarm-tenant-${t}-browser-login-github-qa",
            ],
          )
        }] : concat(
          [for n in c.own : startswith(n, c.prefix)],
          [for n in c.others : !startswith(n, c.prefix)],
        )
      ]
    ]))
    error_message = "a personal worker's browser condition must admit its own -browser- secrets and their versions, and refuse every other tenant's, its own provider and forge secrets, and the other team's"
  }

  # Creating a secret is checked against the project, which carries no secret
  # name, so the create grant is the one with no condition -- and its role
  # carries nothing but create.
  assert {
    condition     = google_project_iam_custom_role.browser_session_creator[0].permissions == toset(["secretmanager.secrets.create"])
    error_message = "swarmBrowserSessionCreator must carry secretmanager.secrets.create and nothing else; anything more on it is project-wide"
  }

  assert {
    condition = alltrue([
      for t, m in google_project_iam_member.browser_session_creator :
      m.role == "projects/saga-agents-staging/roles/swarmBrowserSessionCreator" && length(m.condition) == 0
    ])
    error_message = "the worker's create grant is swarmBrowserSessionCreator, by the id terraform/modules/custom_role_ids spells"
  }

  assert {
    condition = alltrue(flatten([
      for grants in [google_project_iam_member.browser_session_reader, google_project_iam_member.browser_session_writer] : [
        for t, m in grants : length(m.condition) == 1
      ]
    ]))
    error_message = "every read and write grant carries exactly one condition; unconditioned, it would reach every secret in the shared project"
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
  # its slot -- in a state file several people read. The worker creates each
  # secret at run time; Terraform declares neither.
  assert {
    condition = length(regexall(
      "(?m)^\\s*(resource|data)\\s+\"google_secret_manager_(regional_)?secret(_version)?\"",
      file("../../terraform/bootstrap/browser_logins.tf"),
    )) == 0 && length(regexall("secret_data", file("../../terraform/bootstrap/browser_logins.tf"))) == 0
    error_message = "terraform/bootstrap/browser_logins.tf must declare no secret and no secret version: sessions live in Secret Manager only, written at run time"
  }

  # A custom role has no labels; it says so in its description. An IAM member
  # has none either; a conditioned one says so in its condition's description.
  assert {
    condition = alltrue([
      startswith(google_project_iam_custom_role.browser_session_creator[0].description, "managed-by=swarm-terraform;"),
      startswith(google_project_iam_custom_role.browser_login_revoker[0].description, "managed-by=swarm-terraform;"),
    ])
    error_message = "every custom role this file defines says managed-by=swarm-terraform in its description"
  }

  assert {
    condition = alltrue(flatten([
      for grants in [
        values(google_project_iam_member.browser_session_reader),
        values(google_project_iam_member.browser_session_writer),
        google_project_iam_member.browser_login_revoker,
      ] : [for m in grants : startswith(m.condition[0].description, "managed-by=swarm-terraform;")]
    ]))
    error_message = "every conditioned grant this file makes says managed-by=swarm-terraform in its condition's description"
  }

  # Nothing CI applies grants on a browser session: terraform/infra and its
  # modules never spell the -browser- prefix, so no release can hand a group
  # tenant's worker one. And in this root, browser_logins.tf alone does.
  assert {
    condition = length(flatten([
      for f in concat(
        [for p in fileset("../../terraform/infra", "**/*.tf") : "../../terraform/infra/${p}"],
        [for p in fileset("../../terraform/modules", "**/*.tf") : "../../terraform/modules/${p}"],
        [for p in fileset("../../terraform/bootstrap", "**/*.tf") : "../../terraform/bootstrap/${p}" if p != "browser_logins.tf"],
      ) : regexall("-browser-", file(f))
    ])) == 0
    error_message = "a -browser- secret prefix is spelled outside terraform/bootstrap/browser_logins.tf; every grant on a browser session must be made there, where this suite holds it"
  }

  assert {
    condition     = length(fileset("../../terraform/infra", "**/*.tf")) > 5 && length(fileset("../../terraform/bootstrap", "**/*.tf")) > 5
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
      length(google_project_iam_custom_role.browser_session_creator) == 0,
      length(google_project_iam_custom_role.browser_login_revoker) == 0,
      length(google_project_iam_member.browser_session_creator) == 0,
      length(google_project_iam_member.browser_session_reader) == 0,
      length(google_project_iam_member.browser_session_writer) == 0,
      length(google_project_iam_member.browser_login_revoker) == 0,
      length(data.google_project.browser_logins) == 0,
    ])
    error_message = "enable_browser_logins defaults to false and, false, defines and grants nothing"
  }
}

run "a_tenant_whose_browser_prefix_nests_inside_anothers_is_refused" {
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
    infra_tenants_tfvars  = "../../tests/terraform/browser_logins_fixture/nested_tenants.tfvars"
  }

  expect_failures = [google_project_iam_member.browser_session_reader]
}
