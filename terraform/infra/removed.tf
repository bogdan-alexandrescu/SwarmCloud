# u-bogdan LEAVES THIS ROOT -- FORGOTTEN, NOT DESTROYED (docs/workspaces.md
# §3.3; lane W9 of #847, 2026-10-09).
#
# Owner decision WD3, 2026-10-08: a person's workspace is made by the
# workspace job (scripts/register-tenant.sh --workspace) and lives outside
# Terraform state, so no plan can change or destroy one. u-bogdan, the owner's
# personal tenant, predates that and was a key of var.tenants in
# terraform/environments/dev/dev.tfvars. W9 takes it out of that map. Without
# the blocks below, every resource the map made for it -- its worker account,
# its provider secret and the authoritative bindings on it, its tenant and pool
# documents, its four Cloud Run jobs, its scheduler jobs and its grants -- has
# left the configuration and is PLANNED FOR DESTROY. Deleting the account
# alone would strand every one of its tasks; deleting the secret would take
# its subscription credential with it.
#
# `destroy = false` makes the release drop each object from this root's state
# and touch nothing live. NONE OF THESE BLOCKS MAY EVER BE CHANGED TO DESTROY:
# from the apply that forgets them, these objects are the workspace record's
# (`workspaces/u-bogdan`, `migrated = true`), and the only teardown of a
# personal workspace is the owner-approved run of §7.
#
# EACH OBJECT IS ONE INSTANCE OF A for_each, and a `removed` block cannot name
# an instance -- Terraform 1.16 refuses `from = x["key"]` ("Resource instance
# keys not allowed"), and a block naming the resource would forget every
# tenant's. So each instance is first MOVED to an address of its own that
# nothing declares, and that address is removed: the same two steps
# custom_roles_moved_to_bootstrap.tf takes for the broker's grant. A `moved`
# whose `from` is not in state is a no-op, so a state that never held one of
# these (a rebuilt environment) plans nothing for it.
#
# The addresses are exactly what terraform/infra planned for u-bogdan under
# dev.tfvars before this change, read from a mock-provider plan of it
# (2026-10-09): 33 instances across artifact_registry, cloud_run,
# cloud_run_jobs, firestore, scheduler, secret_manager and tenancy.
# tests/terraform/u_bogdan_removed.tftest.hcl replans that map and fails if
# one of its u-bogdan instances is missing here, or if a block destroys.
#
# THE ONE IN-PLACE CHANGE THIS DOES NOT COVER. modules/storage lists each
# var.tenants key's tasks/ and verdicts/ prefixes in the artifact bucket's
# Nearline lifecycle rule (`aged_prefixes`). Without u-bogdan in the map the
# rule loses its two prefixes, so the release plans one in-place update of
# module.storage.google_storage_bucket.artifacts. Nothing is deleted and the
# Delete rule is unchanged; u-bogdan's task objects stop moving to Nearline,
# which is the consequence §3.2 already accepts for every personal workspace.
#
# WHAT A FORGOTTEN JOB NO LONGER GETS. u-bogdan's four Cloud Run jobs keep the
# label managed-by=swarm-terraform, and the dispatcher refreshes the image only
# of jobs labelled managed-by=swarm-scheduler
# (apps/scheduler/scheduler/dispatch.py, `_refresh_image`). So they stay at the
# image of the last release that applied them until the owner relabels them or
# deletes them for the dispatcher to recreate (docs/workspaces.md §3.3, the
# operator steps).
#
# Terraform >= 1.7 for `removed`; CI and the owner run 1.16.2.

moved {
  from = module.artifact_registry.google_artifact_registry_repository_iam_member.pullers["tenant-u-bogdan"]
  to   = module.artifact_registry.google_artifact_registry_repository_iam_member.pullers_w9_tenant_u_bogdan
}

removed {
  from = module.artifact_registry.google_artifact_registry_repository_iam_member.pullers_w9_tenant_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.cloud_run.google_cloud_run_v2_service_iam_member.invokers["swarm-quota-broker:worker-u-bogdan"]
  to   = module.cloud_run.google_cloud_run_v2_service_iam_member.invokers_w9_swarm_quota_broker_worker_u_bogdan
}

removed {
  from = module.cloud_run.google_cloud_run_v2_service_iam_member.invokers_w9_swarm_quota_broker_worker_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this["swarm-job-u-bogdan-claude-code"]
  to   = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_claude_code
}

removed {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_claude_code

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this["swarm-job-u-bogdan-generic"]
  to   = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_generic
}

removed {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_generic

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this["swarm-job-u-bogdan-indexer"]
  to   = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_indexer
}

removed {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_indexer

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this["swarm-job-u-bogdan-mock"]
  to   = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_mock
}

removed {
  from = module.cloud_run_jobs.google_cloud_run_v2_job.this_w9_swarm_job_u_bogdan_mock

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.firestore.google_firestore_document.pool["provider:anthropic:tenant:u-bogdan"]
  to   = module.firestore.google_firestore_document.pool_w9_provider_anthropic_tenant_u_bogdan
}

removed {
  from = module.firestore.google_firestore_document.pool_w9_provider_anthropic_tenant_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.firestore.google_firestore_document.pool["tenant:u-bogdan"]
  to   = module.firestore.google_firestore_document.pool_w9_tenant_u_bogdan
}

removed {
  from = module.firestore.google_firestore_document.pool_w9_tenant_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.firestore.google_firestore_document.tenant["u-bogdan"]
  to   = module.firestore.google_firestore_document.tenant_w9_u_bogdan
}

removed {
  from = module.firestore.google_firestore_document.tenant_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.scheduler.google_cloud_scheduler_job.issue_run_advance["u-bogdan"]
  to   = module.scheduler.google_cloud_scheduler_job.issue_run_advance_w9_u_bogdan
}

removed {
  from = module.scheduler.google_cloud_scheduler_job.issue_run_advance_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.scheduler.google_cloud_scheduler_job.issue_sweep["u-bogdan"]
  to   = module.scheduler.google_cloud_scheduler_job.issue_sweep_w9_u_bogdan
}

removed {
  from = module.scheduler.google_cloud_scheduler_job.issue_sweep_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.scheduler.google_cloud_scheduler_job.merge_wake["u-bogdan"]
  to   = module.scheduler.google_cloud_scheduler_job.merge_wake_w9_u_bogdan
}

removed {
  from = module.scheduler.google_cloud_scheduler_job.merge_wake_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.scheduler.google_cloud_scheduler_job.repo_index_poll["u-bogdan"]
  to   = module.scheduler.google_cloud_scheduler_job.repo_index_poll_w9_u_bogdan
}

removed {
  from = module.scheduler.google_cloud_scheduler_job.repo_index_poll_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.scheduler.google_cloud_scheduler_job.workflow_rollup["u-bogdan"]
  to   = module.scheduler.google_cloud_scheduler_job.workflow_rollup_w9_u_bogdan
}

removed {
  from = module.scheduler.google_cloud_scheduler_job.workflow_rollup_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.scheduler.google_pubsub_topic_iam_member.worker_publishers["worker:u-bogdan"]
  to   = module.scheduler.google_pubsub_topic_iam_member.worker_publishers_w9_worker_u_bogdan
}

removed {
  from = module.scheduler.google_pubsub_topic_iam_member.worker_publishers_w9_worker_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.secret_manager.google_secret_manager_secret.refresh["swarm-tenant-u-bogdan-anthropic-refresh"]
  to   = module.secret_manager.google_secret_manager_secret.refresh_w9_swarm_tenant_u_bogdan_anthropic_refresh
}

removed {
  from = module.secret_manager.google_secret_manager_secret.refresh_w9_swarm_tenant_u_bogdan_anthropic_refresh

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.secret_manager.google_secret_manager_secret.this["swarm-tenant-u-bogdan-anthropic"]
  to   = module.secret_manager.google_secret_manager_secret.this_w9_swarm_tenant_u_bogdan_anthropic
}

removed {
  from = module.secret_manager.google_secret_manager_secret.this_w9_swarm_tenant_u_bogdan_anthropic

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.accessor["swarm-tenant-u-bogdan-anthropic"]
  to   = module.secret_manager.google_secret_manager_secret_iam_binding.accessor_w9_swarm_tenant_u_bogdan_anthropic
}

removed {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.accessor_w9_swarm_tenant_u_bogdan_anthropic

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.refresh_accessor["swarm-tenant-u-bogdan-anthropic-refresh"]
  to   = module.secret_manager.google_secret_manager_secret_iam_binding.refresh_accessor_w9_swarm_tenant_u_bogdan_anthropic_refresh
}

removed {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.refresh_accessor_w9_swarm_tenant_u_bogdan_anthropic_refresh

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.version_adder["swarm-tenant-u-bogdan-anthropic"]
  to   = module.secret_manager.google_secret_manager_secret_iam_binding.version_adder_w9_swarm_tenant_u_bogdan_anthropic
}

removed {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.version_adder_w9_swarm_tenant_u_bogdan_anthropic

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.version_adder["swarm-tenant-u-bogdan-anthropic-refresh"]
  to   = module.secret_manager.google_secret_manager_secret_iam_binding.version_adder_w9_swarm_tenant_u_bogdan_anthropic_refresh
}

removed {
  from = module.secret_manager.google_secret_manager_secret_iam_binding.version_adder_w9_swarm_tenant_u_bogdan_anthropic_refresh

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_project_iam_member.worker_firestore["u-bogdan"]
  to   = module.tenancy.google_project_iam_member.worker_firestore_w9_u_bogdan
}

removed {
  from = module.tenancy.google_project_iam_member.worker_firestore_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_project_iam_member.worker_telemetry["u-bogdan:roles/cloudtrace.agent"]
  to   = module.tenancy.google_project_iam_member.worker_telemetry_w9_u_bogdan_roles_cloudtrace_agent
}

removed {
  from = module.tenancy.google_project_iam_member.worker_telemetry_w9_u_bogdan_roles_cloudtrace_agent

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_project_iam_member.worker_telemetry["u-bogdan:roles/logging.logWriter"]
  to   = module.tenancy.google_project_iam_member.worker_telemetry_w9_u_bogdan_roles_logging_logwriter
}

removed {
  from = module.tenancy.google_project_iam_member.worker_telemetry_w9_u_bogdan_roles_logging_logwriter

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_project_iam_member.worker_telemetry["u-bogdan:roles/monitoring.metricWriter"]
  to   = module.tenancy.google_project_iam_member.worker_telemetry_w9_u_bogdan_roles_monitoring_metricwriter
}

removed {
  from = module.tenancy.google_project_iam_member.worker_telemetry_w9_u_bogdan_roles_monitoring_metricwriter

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_service_account.worker["u-bogdan"]
  to   = module.tenancy.google_service_account.worker_w9_u_bogdan
}

removed {
  from = module.tenancy.google_service_account.worker_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_service_account_iam_member.act_as["u-bogdan:deployer"]
  to   = module.tenancy.google_service_account_iam_member.act_as_w9_u_bogdan_deployer
}

removed {
  from = module.tenancy.google_service_account_iam_member.act_as_w9_u_bogdan_deployer

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_service_account_iam_member.act_as["u-bogdan:reconciler"]
  to   = module.tenancy.google_service_account_iam_member.act_as_w9_u_bogdan_reconciler
}

removed {
  from = module.tenancy.google_service_account_iam_member.act_as_w9_u_bogdan_reconciler

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_service_account_iam_member.act_as["u-bogdan:scheduler"]
  to   = module.tenancy.google_service_account_iam_member.act_as_w9_u_bogdan_scheduler
}

removed {
  from = module.tenancy.google_service_account_iam_member.act_as_w9_u_bogdan_scheduler

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_service_account_iam_member.workload_identity["u-bogdan"]
  to   = module.tenancy.google_service_account_iam_member.workload_identity_w9_u_bogdan
}

removed {
  from = module.tenancy.google_service_account_iam_member.workload_identity_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_storage_bucket_iam_member.worker_bucket_metadata["u-bogdan"]
  to   = module.tenancy.google_storage_bucket_iam_member.worker_bucket_metadata_w9_u_bogdan
}

removed {
  from = module.tenancy.google_storage_bucket_iam_member.worker_bucket_metadata_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_storage_bucket_iam_member.worker_objects_read["u-bogdan"]
  to   = module.tenancy.google_storage_bucket_iam_member.worker_objects_read_w9_u_bogdan
}

removed {
  from = module.tenancy.google_storage_bucket_iam_member.worker_objects_read_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}

moved {
  from = module.tenancy.google_storage_bucket_iam_member.worker_objects_write["u-bogdan"]
  to   = module.tenancy.google_storage_bucket_iam_member.worker_objects_write_w9_u_bogdan
}

removed {
  from = module.tenancy.google_storage_bucket_iam_member.worker_objects_write_w9_u_bogdan

  lifecycle {
    destroy = false
  }
}
