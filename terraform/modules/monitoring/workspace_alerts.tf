# ---------------------------------------------------------------------------
# The personal-workspace job's alerts (docs/workspaces.md §2.4; lane W4 of
# #847, re-targeted at the Cloud Run job by W4b)
# ---------------------------------------------------------------------------
#
# swarm-workspace-deployer (terraform/bootstrap/workspace_deployer.tf) holds
# project-wide account-IAM power that IAM cannot narrow, and on the WD9
# fallback a projectIamAdmin whose hasOnly() bounds WHICH roles it grants but
# never to WHOM. The owner accepted that on 2026-10-08 with safeguards, and the
# last line of them is detective -- these four policies:
#
#   1. outside_personal_workers: the deployer changed an account not named
#      swarm-agent-worker-u-*, or edited any other IAM policy for a member not
#      named so, or removed a binding at all (§2.4 safeguard 3, R4). The guard
#      refuses every such call the script could make (rules C2-C6, C9), so a
#      hit is either a guard bug or the identity's token used outside the guard
#      (R2) -- both are the owner's to look at now.
#   2. foreign_runtime: something other than the swarm-workspace-apply job was
#      made or changed to run as the deployer -- a Cloud Run job or service, a
#      build or build trigger, a workflow or an Eventarc trigger. Each needs
#      actAs on the deployer, which nobody holds at the account level, so a hit
#      is a project-level actAs holder (W0b (3): the owners, and the default
#      compute account's Editor, #1020) running code nobody reviewed with that
#      power.
#   3. job_changed: anything made, changed or deleted on the job, its IAM
#      policy, the dispatch path (option (ii): the workflow, the Eventarc
#      trigger, the topic's IAM, the project-level workflows.invoker grant),
#      either identity's own IAM, keys or state, or Cloud Run's audit config.
#      The owner's own bootstrap apply pages here too; that page is the alert
#      working, and it is the only expected one.
#   4. foreign_run: a jobs.run of swarm-workspace-apply by any caller but
#      swarm-workspace-dispatch. swarm-scheduler, swarm-accept, the release
#      deployer and the owners hold run.jobs.runWithOverrides project-wide and
#      unscopably (§2.1); the job's entrypoint discards what an override can
#      set, and this sees that one was tried.
#
# 1-3 ARE LOG-MATCH POLICIES over the Admin Activity audit log, which Cloud
# Logging writes to _Required for every project and which no sink or
# exclusion can drop -- so a stolen token cannot route its own trail away
# first. 4 reads the DATA ACCESS log: a RunJob is a DATA_WRITE entry (W0b (2),
# 2026-10-10), off by default and written to _Default once enabled, which the
# bootstrap's google_project_iam_audit_config.run_data_write does; the
# bootstrap's sink and exclusion leave audit entries in _Default on purpose.
# job_changed pages if that audit config is changed. One matching entry is
# enough; the rate limit only keeps a burst to one notification per five
# minutes.
#
# THE NAMES ARE LITERALS, the ones terraform/bootstrap creates;
# tests/terraform/workspace_deployer.tftest.hcl plans both and holds them
# equal. This module cannot read the bootstrap's state, and must not need it.
#
# NOT VERIFIED LIVE -- the field paths are Google's documented audit-log shapes,
# not entries read from this project, because no such entry exists yet:
#
#   * resource.labels.email_id on IAM's service_account entries;
#   * protoPayload.serviceData.policyDelta.bindingDeltas and .auditConfigDeltas
#     (google.iam.v1.logging.AuditData) on SetIamPolicy entries of Resource
#     Manager, Cloud Storage (storage.setIamPermissions) and Secret Manager;
#   * protoPayload.resourceName ending in jobs/, workflows/ or triggers/<name>
#     on Cloud Run, Workflows and Eventarc entries, v1 and v2 alike;
#   * that the deployer's email appears in the entry of a create or update
#     that sets it as a resource's account (matched as text anywhere in it);
#   * that a Cloud Run create names the new job in resourceName. If it names
#     the parent instead, the owner's first bootstrap apply pages
#     foreign_runtime once, beside job_changed -- noise on a known day, never
#     silence;
#   * that a RunJob entry carries the caller in authenticationInfo, as every
#     Data Access entry does. Whether it carries the overrides is pending the
#     first execution (§2.4).
#
# A field path that does not exist matches nothing, so the failure mode is
# SILENCE, not noise. The control is the first workspace: its run writes
# account and bucket entries the first policy must NOT match (the person's own
# worker), the owner's bootstrap apply writes job entries the third MUST match,
# and a deliberate jobs.run by the owner, once, must page on the fourth.

locals {
  workspace_deployer_email = "swarm-workspace-deployer@${var.project_id}.iam.gserviceaccount.com"
  workspace_dispatch_email = "swarm-workspace-dispatch@${var.project_id}.iam.gserviceaccount.com"
  workspace_apply_name     = "swarm-workspace-apply"

  # `[.]` rather than `\.`: inside a quoted Logging string a backslash is the
  # query language's escape before it is the regex's.
  workspace_project_re = replace(var.project_id, ".", "[.]")
  personal_worker_re   = "swarm-agent-worker-u-[a-z0-9-]+@${local.workspace_project_re}[.]iam[.]gserviceaccount[.]com"

  workspace_audit_log       = "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\""
  workspace_data_access_log = "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fdata_access\""

  # The job, the workflow and the Eventarc trigger by the tail of their
  # resource name: v1 Cloud Run spells a job namespaces/<p>/jobs/<name>, v2
  # projects/<p>/locations/<l>/jobs/<name>. Anchored, so a job named
  # swarm-workspace-apply-2 is NOT the job.
  workspace_job_re      = "/jobs/${local.workspace_apply_name}$"
  workspace_workflow_re = "/workflows/${local.workspace_apply_name}$"
  workspace_eventarc_re = "/triggers/${local.workspace_apply_name}$"

  # Cloud Build's own service agent, which runs what a trigger starts. Its
  # entries are a trigger working, not someone using it.
  cloudbuild_agent_re = "@gcp-sa-cloudbuild[.]iam[.]gserviceaccount[.]com$"

  workspace_alert_filters = {
    outside_personal_workers = join(" AND ", [
      local.workspace_audit_log,
      "protoPayload.authenticationInfo.principalEmail=\"${local.workspace_deployer_email}\"",
      join(" OR ", [
        # An account other than a personal worker: created, updated, or its
        # own policy written (the worker's own policy carries other members --
        # its Kubernetes service accounts, the scheduler, the reconciler -- so
        # for accounts it is the ACCOUNT that is checked).
        "(resource.type=\"service_account\" AND NOT resource.labels.email_id=~\"^${local.personal_worker_re}$\")",
        # Any other policy -- the bucket, the project, a secret -- given a
        # member who is not a personal worker. On a repeated field, `!~` is
        # true when ANY element fails to match.
        "(resource.type!=\"service_account\" AND protoPayload.methodName=~\"(?i)setiam(policy|permissions)$\" AND protoPayload.serviceData.policyDelta.bindingDeltas.member!~\"^serviceAccount:${local.personal_worker_re}$\")",
        # Any removal at all: the job only ever adds (rule C9).
        "(protoPayload.serviceData.policyDelta.bindingDeltas.action=\"REMOVE\")",
      ]),
    ])

    foreign_runtime = join(" AND ", [
      local.workspace_audit_log,
      "NOT protoPayload.authenticationInfo.principalEmail=~\"${local.cloudbuild_agent_re}\"",
      # The deployer named anywhere in the entry: a request setting it as the
      # account something runs as.
      "\"${local.workspace_deployer_email}\"",
      join(" OR ", [
        "(protoPayload.serviceName=\"run.googleapis.com\" AND protoPayload.methodName=~\"[.](Create|Update|Replace)(Job|Service)$\" AND NOT protoPayload.resourceName=~\"${local.workspace_job_re}\")",
        "(protoPayload.serviceName=\"cloudbuild.googleapis.com\" AND protoPayload.methodName=~\"[.](CreateBuild|CreateBuildTrigger|UpdateBuildTrigger)$\")",
        "(protoPayload.serviceName=\"workflows.googleapis.com\" AND protoPayload.methodName=~\"[.](Create|Update)Workflow$\")",
        "(protoPayload.serviceName=\"eventarc.googleapis.com\" AND protoPayload.methodName=~\"[.](Create|Update)Trigger$\")",
      ]),
    ])

    job_changed = join(" AND ", [
      local.workspace_audit_log,
      join(" OR ", [
        # The job: created, updated, replaced, deleted, its IAM set. A run is
        # not in this log (foreign_run reads it).
        "(protoPayload.serviceName=\"run.googleapis.com\" AND protoPayload.resourceName=~\"${local.workspace_job_re}\")",
        # Option (ii)'s dispatch path.
        "(protoPayload.serviceName=\"workflows.googleapis.com\" AND protoPayload.resourceName=~\"${local.workspace_workflow_re}\")",
        "(protoPayload.serviceName=\"eventarc.googleapis.com\" AND protoPayload.resourceName=~\"${local.workspace_eventarc_re}\")",
        "(resource.type=\"pubsub_topic\" AND resource.labels.topic_id=\"${local.workspace_apply_name}\" AND protoPayload.methodName=~\"(?i)setiampolicy$\")",
        "(protoPayload.methodName=~\"(?i)setiampolicy$\" AND protoPayload.serviceData.policyDelta.bindingDeltas.role=\"roles/workflows.invoker\")",
        # Both identities: their own policy, their keys, their state.
        "(resource.type=\"service_account\" AND resource.labels.email_id=\"${local.workspace_deployer_email}\")",
        "(resource.type=\"service_account\" AND resource.labels.email_id=\"${local.workspace_dispatch_email}\")",
        # Cloud Run's audit config: turning DATA_WRITE off blinds foreign_run.
        "(protoPayload.methodName=~\"(?i)setiampolicy$\" AND protoPayload.serviceData.policyDelta.auditConfigDeltas.service=\"run.googleapis.com\")",
      ]),
    ])

    foreign_run = join(" AND ", [
      local.workspace_data_access_log,
      "protoPayload.serviceName=\"run.googleapis.com\"",
      "protoPayload.methodName=~\"[.]RunJob$\"",
      "protoPayload.resourceName=~\"${local.workspace_job_re}\"",
      "NOT protoPayload.authenticationInfo.principalEmail=\"${local.workspace_dispatch_email}\"",
    ])
  }

  workspace_alerts = {
    outside_personal_workers = {
      display_name = "swarm-${var.environment}-workspace-deployer-outside-personal-workers"
      severity     = "CRITICAL"
      condition    = "swarm-workspace-deployer changed something that is not a personal worker"
      doc          = <<-DOC
        **swarm-workspace-deployer** changed an account that is not a personal
        worker (`swarm-agent-worker-u-*`), gave a role to a member that is not
        one, or removed a binding. The workspace job's call guard refuses every
        one of those (docs/workspaces.md §2.5, rules C2-C6 and C9), so either
        the guard let a call through, or the identity's token was used outside
        an execution (§2.4 R2).

        1. Open the matching entry: who called (`authenticationInfo`), from
           where (`requestMetadata`), on what (`resourceName`), and the
           `policyDelta`.
        2. Find the execution of the Cloud Run job `swarm-workspace-apply`
           running at that minute in the restricted log bucket
           `swarm-workspace-apply` and read what the guard printed. No
           execution at that minute means the token was used outside one.
        3. Undo the change, then, as the owner, take the dispatcher off the
           job's IAM policy (or delete the job) until the cause is understood.
           Never by widening the guard.
      DOC
    }

    foreign_runtime = {
      display_name = "swarm-${var.environment}-workspace-deployer-foreign-runtime"
      severity     = "CRITICAL"
      condition    = "something other than the workspace job was set to run as swarm-workspace-deployer"
      doc          = <<-DOC
        A Cloud Run job or service other than `swarm-workspace-apply`, a build,
        a build trigger, a workflow or an Eventarc trigger was created or
        changed to run as **swarm-workspace-deployer**. Only the one Cloud Run
        job, running the image the owner pinned, may use the identity
        (docs/workspaces.md §2.4, safeguard 1): this is code nobody reviewed
        running with project-wide account-IAM power.

        Delete or cancel what was made, and find who holds `actAs` on the
        identity: the entry's `authenticationInfo.principalEmail` does. Nobody
        holds it at the account level, so it is a project-level holder (W0b
        (3): the owners, and the default compute account's Editor, #1020).
        Then check the outside-personal-workers policy for anything it changed.
      DOC
    }

    job_changed = {
      display_name = "swarm-${var.environment}-workspace-job-or-dispatch-changed"
      severity     = "ERROR"
      condition    = "the workspace job, its dispatch, its identities or Cloud Run's audit config changed"
      doc          = <<-DOC
        The `swarm-workspace-apply` Cloud Run job, or its IAM policy, was
        created, changed or deleted; or its dispatch was (the workflow and the
        Eventarc trigger of the same name, the topic's IAM, a
        `roles/workflows.invoker` grant); or **swarm-workspace-deployer**'s or
        **swarm-workspace-dispatch**'s own IAM policy, keys or state changed;
        or Cloud Run's audit config did. All of them are made by the owner's
        bootstrap apply (terraform/bootstrap/workspace_deployer.tf and
        workspace_dispatch.tf) and by nothing else.

        If the owner just applied the bootstrap, this is that apply. Otherwise
        compare the change with those files and re-apply the bootstrap from
        `main`: the job's policy and both identities' policies are written
        authoritatively, so an apply removes anything granted on them by hand.
      DOC
    }

    foreign_run = {
      display_name = "swarm-${var.environment}-workspace-job-run-by-another-caller"
      severity     = "ERROR"
      condition    = "swarm-workspace-apply was run by a caller other than swarm-workspace-dispatch"
      doc          = <<-DOC
        An execution of the Cloud Run job `swarm-workspace-apply` was started
        by a caller other than **swarm-workspace-dispatch**, the only member of
        the job's IAM policy. The caller holds `run.jobs.run` project-wide
        (docs/workspaces.md §2.3: swarm-scheduler, swarm-accept, the release
        deployer, the owners, or the default compute account's Editor, #1020).
        The job's entrypoint discards every override but its two validated
        arguments and A1 refuses a record no admin approved, so the most it
        can do is apply an approved workspace; nothing calls the job that way
        on purpose.

        Read the entry's `authenticationInfo.principalEmail` and
        `requestMetadata`, then the execution's log in the restricted bucket
        `swarm-workspace-apply`. If the caller is not the owner testing, treat
        that caller's credentials as compromised.
      DOC
    }
  }
}

resource "google_monitoring_alert_policy" "workspace" {
  for_each = var.create_alerts ? local.workspace_alerts : {}

  project      = var.project_id
  display_name = each.value.display_name
  combiner     = "OR"
  severity     = each.value.severity

  conditions {
    display_name = each.value.condition

    condition_matched_log {
      filter = local.workspace_alert_filters[each.key]
    }
  }

  notification_channels = local.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = "${trimspace(each.value.doc)}${local.alert_docs_suffix}"
  }

  # A log-match policy must say how often it may notify; it opens one incident
  # per matching entry otherwise.
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  # Merged, not just inherited, as reconciler_blind does: the marker `make
  # destroy` keys on, even if a caller passes labels without it.
  user_labels = merge(var.labels, { "managed-by" = "swarm-terraform" })
}
