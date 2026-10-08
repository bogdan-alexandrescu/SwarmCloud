# ---------------------------------------------------------------------------
# The personal-workspace job's alerts (docs/workspaces.md §2.4, lane W4 of #847)
# ---------------------------------------------------------------------------
#
# swarm-workspace-deployer (terraform/bootstrap/workspace_deployer.tf) holds
# project-wide account-IAM power that IAM cannot narrow, and on the WD9
# fallback a projectIamAdmin whose hasOnly() bounds WHICH roles it grants but
# never to WHOM. The owner accepted that on 2026-10-08 with safeguards, and the
# last line of them is detective -- these three policies:
#
#   1. outside_personal_workers: the deployer changed an account not named
#      swarm-agent-worker-u-*, or edited any other IAM policy for a member not
#      named so, or removed a binding at all (§2.4 safeguard 3, R4). The guard
#      refuses every such call the script could make (rules C2-C6, C9), so a
#      hit is either a guard bug or the identity's token used outside the guard
#      (R2) -- both are the owner's to look at now.
#   2. foreign_build: a build ran as the deployer that did not come from the
#      swarm-workspace-apply trigger -- `gcloud builds submit` as it, or another
#      trigger made or edited to run as it. Either one runs code main never
#      reviewed with that power.
#   3. trigger_changed: anything made, changed, deleted or run by hand on the
#      trigger, the topic's IAM, or the deployer's own IAM, keys or state. The
#      owner's own bootstrap apply pages here too; that page is the alert
#      working, and it is the only expected one.
#
# EACH IS A LOG-MATCH POLICY over the Admin Activity audit log, which Cloud
# Logging writes to _Required for every project and which no sink or
# exclusion can drop -- so a stolen token cannot route its own trail away
# first. One matching entry is enough; the rate limit only keeps a burst to one
# notification per five minutes.
#
# THE NAMES ARE LITERALS, the ones terraform/bootstrap/workspace_deployer.tf
# creates; tests/terraform/workspace_deployer.tftest.hcl plans both and holds
# them equal. This module cannot read the bootstrap's state, and must not need
# it: an alert that waited for the trigger's id would not exist on the day the
# trigger is made.
#
# NOT VERIFIED LIVE -- the field paths are Google's documented audit-log shapes,
# not entries read from this project, because no such entry exists yet:
#
#   * resource.labels.email_id on IAM's service_account entries;
#   * protoPayload.serviceData.policyDelta.bindingDeltas (google.iam.v1.logging
#     .AuditData) on SetIamPolicy entries of Resource Manager, Cloud Storage
#     (storage.setIamPermissions) and Secret Manager;
#   * protoPayload.request.build.serviceAccount on CreateBuild and
#     protoPayload.request.trigger.serviceAccount on Create/UpdateBuildTrigger.
#
# A field path that does not exist matches nothing, so the failure mode is
# SILENCE, not noise. The control is the first workspace: its run writes
# account and bucket entries the first policy must NOT match (the person's own
# worker), and the owner's bootstrap apply writes a trigger entry the third
# MUST match. A first apply that does not page on the third policy means a path
# here is wrong.

locals {
  workspace_deployer_email = "swarm-workspace-deployer@${var.project_id}.iam.gserviceaccount.com"
  workspace_apply_name     = "swarm-workspace-apply"

  # `[.]` rather than `\.`: inside a quoted Logging string a backslash is the
  # query language's escape before it is the regex's.
  workspace_project_re = replace(var.project_id, ".", "[.]")
  personal_worker_re   = "swarm-agent-worker-u-[a-z0-9-]+@${local.workspace_project_re}[.]iam[.]gserviceaccount[.]com"

  workspace_audit_log = "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\""

  # Cloud Build's own service agent, which runs what a trigger starts. Its
  # entries are the trigger working, not someone using it.
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

    foreign_build = join(" AND ", [
      local.workspace_audit_log,
      "protoPayload.serviceName=\"cloudbuild.googleapis.com\"",
      "NOT protoPayload.authenticationInfo.principalEmail=~\"${local.cloudbuild_agent_re}\"",
      join(" OR ", [
        "(protoPayload.methodName=\"google.devtools.cloudbuild.v1.CloudBuild.CreateBuild\" AND protoPayload.request.build.serviceAccount:\"${local.workspace_deployer_email}\")",
        "(protoPayload.methodName=~\"[.](Create|Update)BuildTrigger$\" AND protoPayload.request.trigger.serviceAccount:\"${local.workspace_deployer_email}\" AND NOT protoPayload.request.trigger.name=\"${local.workspace_apply_name}\")",
      ]),
    ])

    trigger_changed = join(" AND ", [
      local.workspace_audit_log,
      join(" OR ", [
        # Made, edited, deleted or run by hand. A Delete or Run request names
        # the trigger by id, which this module cannot know, so those are
        # matched by the name or the account appearing anywhere in the entry.
        "(protoPayload.serviceName=\"cloudbuild.googleapis.com\" AND protoPayload.methodName=~\"[.](Create|Update|Delete|Run)BuildTrigger$\" AND NOT protoPayload.authenticationInfo.principalEmail=~\"${local.cloudbuild_agent_re}\" AND (\"${local.workspace_apply_name}\" OR \"${local.workspace_deployer_email}\"))",
        "(resource.type=\"pubsub_topic\" AND resource.labels.topic_id=\"${local.workspace_apply_name}\" AND protoPayload.methodName=~\"(?i)setiampolicy$\")",
        # Its own policy, its keys, its state: no read is in this log.
        "(resource.type=\"service_account\" AND resource.labels.email_id=\"${local.workspace_deployer_email}\")",
      ]),
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
        a build (§2.4 R2).

        1. Open the matching entry: who called (`authenticationInfo`), from
           where (`requestMetadata`), on what (`resourceName`), and the
           `policyDelta`.
        2. Find the build running at that minute in the restricted log
           bucket `swarm-workspace-apply` and read what the guard printed. No
           build at that minute means the token was used outside one.
        3. Undo the change, then disable the trigger
           (`gcloud builds triggers update ... --disabled`, as the owner) until
           the cause is understood. Never by widening the guard.
      DOC
    }

    foreign_build = {
      display_name = "swarm-${var.environment}-workspace-deployer-foreign-build"
      severity     = "CRITICAL"
      condition    = "a build ran as swarm-workspace-deployer outside its trigger"
      doc          = <<-DOC
        A build was submitted to run as **swarm-workspace-deployer**, or a
        trigger other than `swarm-workspace-apply` was made or edited to run
        as it. Only that one trigger, building `main`, may use the identity
        (docs/workspaces.md §2.4, safeguard 1): this is code nobody reviewed
        running with project-wide account-IAM power.

        Cancel the build, delete the other trigger, and find who holds `actAs`
        on the identity: the entry's `authenticationInfo.principalEmail` does.
        Then check the outside-personal-workers policy for anything it changed.
      DOC
    }

    trigger_changed = {
      display_name = "swarm-${var.environment}-workspace-trigger-or-identity-changed"
      severity     = "ERROR"
      condition    = "the workspace trigger, its topic's IAM or its identity changed"
      doc          = <<-DOC
        The `swarm-workspace-apply` trigger was created, changed, deleted or
        run by hand; or the topic's IAM policy changed; or
        **swarm-workspace-deployer**'s own IAM policy, keys or state changed.
        All of them are made by the owner's bootstrap apply
        (terraform/bootstrap/workspace_deployer.tf) and by nothing else.

        If the owner just applied the bootstrap, this is that apply. Otherwise
        compare the change with that file and re-apply the bootstrap from
        `main`: the identity's policy is written authoritatively empty, so an
        apply removes anything granted on it by hand.
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
