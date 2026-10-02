# The ids of the service accounts terraform/infra manages, spelled once for
# both roots.
#
# terraform/infra CREATES these accounts, and CI applies it. terraform/bootstrap
# grants the CI deployer roles/iam.serviceAccountAdmin ON EACH OF THEM, and the
# owner applies that. The two roots share no state, so the only thing that
# keeps "the accounts infra makes" and "the accounts bootstrap grants on" the
# same list is that both read it from here (docs/mirrored-values.md, answer
# (a): derived, nothing to compare) -- the pattern modules/custom_role_ids set
# for the custom roles.
#
# WHY PER ACCOUNT (owner decision 2026-09-29, #334, from the security review of
# contract request 30, #314). Held on the project, serviceAccountAdmin let CI
# set the IAM policy of every service account in saga-agents-staging -- the
# other team's promptlab-runner, swarm-ci-fix, and swarm-tf-deployer itself --
# and so grant itself tokenCreator on any of them. No IAM condition can narrow
# it: "IAM resources don't provide the resource name"
# (docs.cloud.google.com/iam/docs/conditions-attribute-reference).
#
# The tenants' worker accounts are "<tenant_worker_prefix><tenant>"; the tenant
# keys come from each root's own reading of the tenants variable (infra: the
# variable; bootstrap: the tenants block of terraform/environments/dev/dev.tfvars).
#
# Plain strings, no resource and no data source, so both roots know them at plan.

locals {
  # account id -> what modules/iam gives it. Adding an account here makes
  # infra create it AND bootstrap grant the deployer on it; the account must
  # exist before bootstrap's grant can be applied (docs/ci.md, "The deployer's
  # service-account grants").
  platform = {
    "swarm-api" = {
      display_name = "Swarm API"
      description  = "Authenticates callers, resolves tenants, writes tasks. Creates no infrastructure."
    }
    "swarm-scheduler" = {
      display_name = "Swarm Scheduler"
      description  = "Admits work and dispatches executions. Cannot delete infrastructure."
    }
    "swarm-quota-broker" = {
      display_name = "Swarm Quota Broker"
      description  = "Owns provider quota state. Control-plane data only."
    }
    "swarm-reconciler" = {
      display_name = "Swarm Reconciler"
      description  = "The only identity permitted to delete executions and garbage-collect Job resources."
    }
  }

  tick_id   = "swarm-tick"
  verify_id = "swarm-verify"
  # The identity the per-tenant workflow-rollup jobs present to swarm-api
  # (modules/scheduler jobs.tf, D17). Its own account rather than the tick's:
  # swarm-api grants this one address one route (auth.ROLLUP_SWEEPER_ROUTES),
  # and the tick reaches the scheduler and the reconciler.
  rollup_sweeper_id = "swarm-rollup-sweeper"

  # scripts/register-tenant.sh and kubernetes/render.py spell this too; they
  # are not Terraform and cannot read it.
  tenant_worker_prefix = "swarm-agent-worker-"

  worker_ids = { for t in var.tenant_ids : t => "${local.tenant_worker_prefix}${t}" }

  # --- #295: the three profiles that must not run as the worker account -----
  #
  # runner profile -> the suffix of its per-tenant account, and the provider
  # whose registration brings it into being (docs/merge-step.md §1.3, §10 item
  # 4; contract requests 33, 35, 36). `swarm-<tenant>-merge` is the only
  # accessor of `-git-merge`, `swarm-<tenant>-post-verdict` the only accessor of
  # `-git-review`, and `swarm-<tenant>-review` runs the review agent with the
  # one grant nothing else holds: create under tenants/<tenant>/verdicts/.
  #
  # The review account is keyed on git-review, not on its profile's own
  # provider (anthropic): a tenant without a review App has no verdict to
  # write, so it has no use for the account.
  #
  # Every id fits IAM's 30 characters for an 11-character tenant key, the
  # longest modules/tenancy admits: "swarm-" + 11 + "-post-verdict" is 30.
  #
  # `sole_accessor`: the account is the ONLY reader of its provider's secret,
  # which the tenant's worker account must never read. `also_reads`: the
  # secrets of the profile's own provider (the review agent's Anthropic key),
  # read beside the worker account. tests/terraform/merge_step_iam.tftest.hcl
  # holds `also_reads` to terraform/infra's catalogue mirror.
  action_accounts = {
    "merge"              = { suffix = "-merge", provider = "git-merge", sole_accessor = true, also_reads = [] }
    "post-verdict"       = { suffix = "-post-verdict", provider = "git-review", sole_accessor = true, also_reads = [] }
    "claude-code-review" = { suffix = "-review", provider = "git-review", sole_accessor = false, also_reads = ["anthropic"] }
  }

  action_account_prefix = "swarm-"

  # "<tenant>:<profile>" -> account id, for each tenant whose providers
  # include the profile's gating provider. Empty for a caller that passes no
  # providers, which is every caller that predates #295.
  action_ids = merge([
    for t, providers in var.tenant_providers : {
      for profile, a in local.action_accounts :
      "${t}:${profile}" => "${local.action_account_prefix}${t}${a.suffix}"
      if contains(providers, a.provider)
    }
  ]...)

  infra_managed = sort(concat(
    keys(local.platform),
    [local.tick_id, local.verify_id, local.rollup_sweeper_id],
    values(local.worker_ids),
    values(local.action_ids),
  ))
}
