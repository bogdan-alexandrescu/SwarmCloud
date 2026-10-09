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
  # The identity the one schedule tick presents to swarm-api
  # (docs/schedules.md §2.1, owner decision SD10, 2026-10-08). Its own account
  # rather than the rollup sweeper's: the tick reads due schedules across every
  # tenant, a different reach from the sweeper's per-tenant routes, so swarm-api
  # admits this address to POST /v1/admin/schedules/tick alone
  # (auth.SCHEDULE_TICK_ROUTES) and the sweeper's route set is not widened.
  schedule_tick_id = "swarm-schedule-tick"

  # scripts/register-tenant.sh and kubernetes/render.py spell this too; they
  # are not Terraform and cannot read it.
  tenant_worker_prefix = "swarm-agent-worker-"

  worker_ids = { for t in var.tenant_ids : t => "${local.tenant_worker_prefix}${t}" }

  infra_managed = sort(concat(
    keys(local.platform),
    [local.tick_id, local.verify_id, local.rollup_sweeper_id, local.schedule_tick_id],
    values(local.worker_ids),
  ))
}
