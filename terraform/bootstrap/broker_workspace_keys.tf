# ---------------------------------------------------------------------------
# LLM providers: the quota broker reads each workspace's own provider key, so
# its key import can copy it into the pool (docs/design/llm-providers.md §5.3,
# Q7; owner answer of 10 November 2026, "Grant a conditioned read", in this
# root; lane llm-broker-import-grant).
# ---------------------------------------------------------------------------
#
# `POST /v1/accounts/import-workspace-keys` (quota_broker main.py) lists the
# secrets `scripts/create-secrets.sh` labelled as a tenant credential, keeps
# those named exactly `swarm-tenant-<tenant>-<vendor>` for vendor anthropic or
# openai (secretstore.workspace_provider_keys), reads each one's latest version
# (secretstore.access) and registers it as that workspace's own pool key. The
# broker could read only its refresh secrets (per-secret bindings,
# modules/secret_manager), so in a live deployment the route skipped every
# workspace as `unreadable: PermissionDenied` and imported nothing.
#
#   google_project_iam_member.broker_workspace_key_reader
#       swarm-quota-broker holds roles/secretmanager.secretAccessor
#       (secretmanager.versions.access) on the project, under one condition.
#
# THE CONDITION, one expression and no list:
#
#   resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-") != ""
#     && (resource.name.extract("/secrets/swarm-tenant-{t}-anthropic/versions/") != ""
#         || resource.name.extract("/secrets/swarm-tenant-{t}-openai/versions/") != "")
#     && resource.name.extract("/secrets/swarm-tenant-{t}-git-") == ""
#     && resource.name.extract("/secrets/swarm-tenant-{t}-browser-") == ""
#
#   * The project NUMBER: IAM evaluates Secret Manager names with it, never the
#     project ID. versions.access is evaluated on the VERSION,
#     `projects/<n>/secrets/<id>/versions/<v>`.
#   * extract(), never matches(), which IAM refuses on some services
#     (terraform/modules/iam/bindings.tf). extract("pre{t}suf") is the text
#     between the first `pre` and the first `suf` after it, "" when either is
#     missing.
#   * `-anthropic/versions/` and `-openai/versions/` as the suffixes, as
#     typesafe_forwarder.tf pins `-typesafe/versions/`: the `/versions/` pins
#     the vendor to the END of the secret id, so the `-refresh` half
#     (`swarm-tenant-<t>-anthropic-refresh`) and any `-anthropic<x>` are
#     refused. The bare secret is refused too, which costs nothing: the role
#     holds no permission evaluated on a secret.
#   * The non-empty first segment: no tenant id is empty or starts with `-`,
#     so `swarm-tenant--<...>` is never a workspace's key; the clause also
#     keeps the two exclusions below from reading an empty tenant as "no
#     exclusion".
#   * The two exclusions close the families whose free text the PERSON
#     chooses and could end in a vendor: a forge user or repository slot
#     (`-git-u-<login>`, `-git-r-<repo>`; a GitHub login can be `openai`) and a
#     saved browser login (`-browser-login-<name>`, `-browser-wf-<id>`). The
#     cost: a tenant whose OWN id contains `-git-` or `-browser-` cannot be
#     imported; the route lists it as unreadable and its key stays where it
#     is, fail closed.
#   * No hasOnly() and no tenant list: the tenant is found in the name, so the
#     condition covers personal tenants the workspace job creates outside
#     Terraform too (docs/workspaces.md §3.2).
#
# WHAT IT REFUSES. The forge token (`-git`) and its slots, an MCP run
# credential (`-mcpc-<16 hex>`), a workspace secret (`-wsec-<hex>`), a plugin
# secret (`-plg-<hex>`: hex never spells a vendor), another provider's key
# (`-typesafe`, ...), every `-refresh` secret, the pool's own
# `swarm-account-*` secrets, the platform's swarm-github-app-* and the other
# team's agents-* and peer-app-* secrets, which do not start with the prefix.
# tests/terraform/broker_workspace_keys.tftest.hcl evaluates each.
#
# WHAT IT STILL REACHES (the residual, for the security review). Any secret
# whose id is `swarm-tenant-<x>-anthropic` or `-openai` with no `-git-` or
# `-browser-` in <x>: today only `Tenant.secret_name(vendor)` mints that
# shape. The broker's code reads only names it derived from the secret's
# `tenant` and `provider` labels and that equal them exactly, never a name a
# caller gives.
#
# TENANT ISOLATION. The broker reads a workspace's provider key only to copy
# it, as that same workspace's own account (`<tenant>:workspace-<vendor>`,
# lent to nobody), into the pool whose keys it already holds and writes. No
# tenant worker is named here and no tenant gains a read of another's key; the
# workspace's own secret and its worker's per-secret binding are unchanged.
#
# WHY HERE AND NOT IN terraform/infra (the owner's answer). A project
# secretAccessor grant made by the release would need the role on
# deployer_grantable_project_roles (deployer_conditions.tf), whose hasOnly()
# limits which roles CI grants, never to whom or under what condition: CI
# could grant itself read on every secret in the shared project, the other
# team's among them. So the owner applies this, and the release carries no
# part of it. tests/terraform/broker_workspace_keys.tftest.hcl holds that no
# file CI applies carries the read.
#
# ORDER: none against a release. The import route is already deployed and
# answers `unreadable` until this is applied, never a partial import.
#
# THE OWNER'S TARGETED PLAN shows exactly one object:
#
#   terraform -chdir=terraform/bootstrap plan \
#     -target=google_project_iam_member.broker_workspace_key_reader
#
# data.google_project.broker_workspace_keys is read, never changed, and the
# broker's account (platform_service_accounts.tf) already exists.
#
# NOT VERIFIED LIVE: that IAM evaluates versions.access with resource.name in
# the version form, and extract() as the mock-provider tests emulate it. Lint
# the condition with iamPolicies:lintPolicy before applying, as
# docs/provider-keys.md §6 did; the first import that lists a workspace as
# imported rather than unreadable is the live proof.
#
# THE OTHER TEAM'S RESOURCES. The name arm starts
# `projects/<n>/secrets/swarm-tenant-`; their secrets are agents-* and
# peer-app-*, so it can match none of them. The grant is to swarm-quota-broker
# alone and touches no cluster, network, bucket or account of theirs.

data "google_project" "broker_workspace_keys" {
  project_id = var.project_id
}

locals {
  # A version of a workspace's own anthropic or openai key (the header).
  # Literal "swarm-tenant-": Tenant.secret_name's spelling; the vendors are
  # secretstore.workspace_provider_keys' two.
  broker_workspace_key_prefix    = "projects/${data.google_project.broker_workspace_keys.number}/secrets/swarm-tenant-"
  broker_workspace_key_condition = "resource.name.startsWith(\"${local.broker_workspace_key_prefix}\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-\") != \"\" && (resource.name.extract(\"/secrets/swarm-tenant-{t}-anthropic/versions/\") != \"\" || resource.name.extract(\"/secrets/swarm-tenant-{t}-openai/versions/\") != \"\") && resource.name.extract(\"/secrets/swarm-tenant-{t}-git-\") == \"\" && resource.name.extract(\"/secrets/swarm-tenant-{t}-browser-\") == \"\""
}

resource "google_project_iam_member" "broker_workspace_key_reader" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = google_service_account.platform["swarm-quota-broker"].member

  condition {
    title       = "swarm workspace provider keys"
    description = "managed-by=swarm-terraform; the quota broker reads versions of swarm-tenant-<t>-anthropic and -openai, each workspace's own key, to copy it into the pool as that workspace's account (LLM providers, Q7). No forge, MCP, browser, refresh or other secret."
    expression  = local.broker_workspace_key_condition
  }
}

output "broker_workspace_key_read" {
  description = "The condition the quota broker's read of workspaces' own provider keys is granted under, for the owner to read a plan against and to lint (iamPolicies:lintPolicy)."
  value       = local.broker_workspace_key_condition
}
