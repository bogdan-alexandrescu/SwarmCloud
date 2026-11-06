# swarm-api reads the swarm-workers log view, granted by the root the OWNER
# applies (lane LOG-VIEW-BOOT, part of #1960; docs/design/agent-autonomy.md
# §3.5, IAM row L-1).
#
# Release run 39888771458 (2026-10-23, head 2aa01cbe) failed applying row L-1
# from terraform/infra at `terraform apply, IAM (dev-iam)`:
#
#   Error 403: The caller does not have permission
#
# CI's deployer holds roles/logging.configWriter, which cannot set IAM on a log
# view. BGD decided, 2026-10-23: move L-1 into terraform/bootstrap, as
# verify_cluster_read.tf did for swarm-verify's clusterViewer, so CI never
# gains the power to grant on log views. What is held here:
#
#   1. terraform/bootstrap plans exactly one google_logging_log_view_iam_member:
#      roles/logging.viewAccessor, swarm-api's account, the view swarm-workers
#      in the bucket swarm-worker-logs, in the project's region; and none when
#      swarm_workers_log_view_exists is false;
#   2. CI gains nothing: deployer_grantable_project_roles, the list of roles
#      CI's scoped projectIamAdmin may modify, carries no logging role but
#      logWriter and no role that reads log entries;
#   3. across terraform/infra, terraform/modules and terraform/bootstrap the one
#      log-view IAM resource is this one, in bootstrap/log_view_read.tf -- a
#      text scan, with a control that shows it can find one.
#
# terraform/infra's side (no log-view IAM resource there at all) is held in
# log_view.tftest.hcl.
#
# WHAT A MOCK PROVIDER CANNOT SHOW. That the owner's identity may set the
# view's IAM policy (logging.views.setIamPolicy), and that the binding lets
# swarm-api's entries.list through. The first logs_query after the owner's
# bootstrap apply shows both.

mock_provider "google" {
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
}

# ---------------------------------------------------------------------------
# 1. The grant, in terraform/bootstrap
# ---------------------------------------------------------------------------

run "swarm_api_reads_the_swarm_workers_view_from_bootstrap" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # On by default: dev's terraform/infra creates the view
  # (swarm_workers_log_view_enabled = true in dev.tfvars).
  # MUTATION: default = false on var.swarm_workers_log_view_exists (or count = 0 on the grant) turns this red.
  assert {
    condition     = length(google_logging_log_view_iam_member.swarm_api_reads_swarm_workers) == 1
    error_message = "with swarm_workers_log_view_exists at its default (true) bootstrap plans row L-1's binding, once"
  }

  # MUTATION: role = "roles/logging.viewer" (or privateLogViewer) turns this red.
  assert {
    condition     = google_logging_log_view_iam_member.swarm_api_reads_swarm_workers[0].role == "roles/logging.viewAccessor"
    error_message = "swarm-api's role on the view is roles/logging.viewAccessor; logging.viewer reads every log in this SHARED project"
  }

  # MUTATION: member = "serviceAccount:swarm-scheduler@..." turns this red.
  assert {
    condition     = google_logging_log_view_iam_member.swarm_api_reads_swarm_workers[0].member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the view's reader is swarm-api's account and nobody else (row L-1)"
  }

  # The view terraform/infra creates (log_view.tf, log_bucket.tf), field for
  # field: the bucket as its bare id, the location the infra root puts it in
  # (the project's region), the project as the parent.
  # MUTATION: name = "_AllLogs" (the bucket's every-log view) turns this red.
  assert {
    condition     = google_logging_log_view_iam_member.swarm_api_reads_swarm_workers[0].name == "swarm-workers"
    error_message = "swarm-api's binding must be on the swarm-workers view, never _AllLogs or another view"
  }

  # MUTATION: bucket = "_Default" (the bucket the other team's cluster writes to) turns this red.
  assert {
    condition     = google_logging_log_view_iam_member.swarm_api_reads_swarm_workers[0].bucket == "swarm-worker-logs"
    error_message = "swarm-api's binding must be on a view of the swarm-worker-logs bucket, by its bare id, never _Default"
  }

  # MUTATION: location = var.location ("US-CENTRAL1", the state bucket's) or "global" turns this red.
  assert {
    condition = (
      google_logging_log_view_iam_member.swarm_api_reads_swarm_workers[0].location == "us-central1"
      && google_logging_log_view_iam_member.swarm_api_reads_swarm_workers[0].parent == "projects/saga-agents-staging"
    )
    error_message = "the binding's location is the project's region (us-central1) and its parent projects/saga-agents-staging, as terraform/infra creates the bucket"
  }
}

run "no_grant_while_the_view_does_not_exist" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    swarm_workers_log_view_exists = false
  }

  # MUTATION: dropping the count from google_logging_log_view_iam_member.swarm_api_reads_swarm_workers turns this red.
  assert {
    condition     = length(google_logging_log_view_iam_member.swarm_api_reads_swarm_workers) == 0
    error_message = "with swarm_workers_log_view_exists = false bootstrap plans no binding on a view that is not there"
  }
}

# ---------------------------------------------------------------------------
# 2. CI gains nothing: no log-reading role on the list CI may grant
# ---------------------------------------------------------------------------

run "ci_may_grant_no_logging_role" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # The control: the list is the one deployer_conditions.tf builds, not empty.
  assert {
    condition     = length(local.deployer_grantable_project_roles) > 10
    error_message = "deployer_grantable_project_roles is empty or nearly so; the assertion below would pass on nothing"
  }

  # Granting L-1 from terraform/infra would have needed a log-reading role CI
  # could hand out. It is granted here instead, so CI's list keeps the one
  # logging role it had before -- logWriter, which the platform's and tenants'
  # accounts write their own logs with and which reads nothing -- and gains
  # no other.
  # MUTATION: adding "roles/logging.viewAccessor" (or "roles/logging.admin") to deployer_grantable_project_roles turns this red.
  assert {
    condition     = [for r in local.deployer_grantable_project_roles : r if startswith(r, "roles/logging.")] == ["roles/logging.logWriter"]
    error_message = "deployer_grantable_project_roles carries a logging role besides roles/logging.logWriter, so CI's scoped projectIamAdmin could grant it; row L-1 is the owner's bootstrap grant (BGD, 2026-10-23, #1960)"
  }

  # And none of them reads log entries: the measured file is read, not restated.
  # MUTATION: adding "roles/logging.privateLogViewer" to deployer_grantable_project_roles turns this red.
  assert {
    condition = length(setintersection(
      toset(local.deployer_grantable_project_roles),
      toset(keys(jsondecode(file("../../terraform/bootstrap/log-reading-roles.json")).roles)),
    )) == 0 && length(keys(jsondecode(file("../../terraform/bootstrap/log-reading-roles.json")).roles)) > 0
    error_message = "a role measured to read log entries (terraform/bootstrap/log-reading-roles.json) is on deployer_grantable_project_roles, or the file lists none"
  }
}

# ---------------------------------------------------------------------------
# 3. The one log-view IAM resource in terraform/ is bootstrap's
# ---------------------------------------------------------------------------

run "the_only_log_view_iam_resource_is_bootstraps" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids = []

    tf_root = "../../terraform"
    view_re = "(?m)^resource \"(google_logging_log_view_iam_(member|binding|policy))\" \"([^\"]+)\""
  }

  # The control on the read: the scan found the files.
  assert {
    condition = (
      length(fileset(var.tf_root, "infra/**/*.tf")) > 10
      && length(fileset(var.tf_root, "modules/**/*.tf")) > 10
      && length(fileset(var.tf_root, "bootstrap/**/*.tf")) > 10
    )
    error_message = "the scan read no terraform/infra, terraform/modules or terraform/bootstrap files; the assertion below would pass on nothing"
  }

  # The control on the pattern: it finds a log-view grant in a text built to
  # hold one of each kind.
  assert {
    condition = length(regexall(var.view_re, join("\n", [
      "resource \"google_logging_log_view_iam_member\" \"a\" {",
      "}",
      "resource \"google_logging_log_view_iam_binding\" \"b\" {",
      "}",
      "resource \"google_logging_log_view_iam_policy\" \"c\" {",
      "}",
      "",
    ]))) == 3
    error_message = "the scan's pattern does not find a log-view IAM resource in a text built to hold three; the check below could not fail"
  }

  # MUTATION: putting google_logging_log_view_iam_member.swarm_api_reads_swarm_workers back in terraform/infra/log_view.tf turns this red.
  assert {
    condition = sort(flatten([
      for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf"), fileset(var.tf_root, "bootstrap/**/*.tf")) : [
        for m in regexall(var.view_re, file("${var.tf_root}/${f}")) : "${f} ${m[0]}.${m[2]}"
      ]
      ])) == tolist([
      "bootstrap/log_view_read.tf google_logging_log_view_iam_member.swarm_api_reads_swarm_workers",
    ])
    error_message = "the one log-view IAM resource in terraform/ must be bootstrap/log_view_read.tf's swarm_api_reads_swarm_workers: CI applies terraform/infra and cannot set IAM on a log view (release run 39888771458, 403)"
  }
}
