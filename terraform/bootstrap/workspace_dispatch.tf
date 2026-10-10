# ---------------------------------------------------------------------------
# swarm-workspace-dispatch: how an approval starts the workspace job, under
# option (ii) of docs/workspaces.md §2.1 (owner, 2026-10-10; lane W4b of #847)
# ---------------------------------------------------------------------------
#
# swarm-api keeps exactly what it held under the Cloud Build shape:
# roles/pubsub.publisher on the one topic swarm-workspace-apply
# (workspace_deployer.tf). It publishes {"workspace_id", "mode", "request_id"}.
# From there:
#
#   topic --(Eventarc trigger, as swarm-workspace-dispatch)--> workflow
#   workflow --(as swarm-workspace-dispatch, jobs.run with two args)--> job
#
# The workflow (workflows/workspace-apply.yaml) checks the id against
# ^w-[0-9a-f]{6}$ and the mode against create|limits, and starts one execution
# with those two values as its arguments and nothing else. So a compromised
# swarm-api can choose which approved workspace runs, and cannot reach the
# job's environment, task count or timeout -- the reason (ii) was chosen over
# swarm-api calling jobs.run itself (§2.1's table).
#
# THE DISPATCH ACCOUNT holds two things and nothing else:
#
#   * roles/run.jobsExecutorWithOverrides ON THE JOB, in the job's
#     authoritative policy (workspace_deployer.tf), which names it alone;
#   * roles/workflows.invoker AT THE PROJECT, for the Eventarc trigger to start
#     the workflow. W0b (7) found on 2026-10-10 that Workflows accepts no
#     grant narrower than the project, no IAM Condition can narrow it
#     (workflows.googleapis.com is not in IAM's resource-attribute list), and
#     the provider has no per-workflow IAM resource
#     (hashicorp/terraform-provider-google#13125). So this account can start
#     ANY workflow in the shared project, the other team's included -- each of
#     which then runs as its own account, not this one. It is granted to the
#     Eventarc trigger's identity only, which has no key and no WIF binding,
#     and the job_changed alert pages on any change to its IAM.
#
# Its own IAM policy is written AUTHORITATIVELY EMPTY: nobody may act as it or
# mint its token at the account level. Creating the trigger and the workflow
# to run as it needs actAs on it, which only the owner's bootstrap apply
# exercises (as the project owner).
#
# NOT VERIFIED LIVE -- each fails closed (no execution, which the sweep's
# dispatched_unclaimed and the workspace-stuck alert see within 30 minutes):
#
#   * that the Pub/Sub service agent can mint this account's token for the
#     trigger's push without a token-creator grant. Google grants it by default
#     unless the agent was enabled on or before 8 April 2021 (W0b (7)); this
#     project's creation date is the operator's to read. If it was, the grant
#     belongs here and this account's policy stops being empty -- a decision
#     for the owner, not a default;
#   * that the googleapis.run.v2 connector with skip_polling needs no
#     permission beyond run.jobs.runWithOverrides on the job.
#
# The Workflows and Eventarc APIs are in terraform/infra's service list
# (terraform/infra/main.tf): the release enables them before the owner's
# bootstrap apply creates anything here.

locals {
  workspace_dispatch_account_id = "swarm-workspace-dispatch"
  workspace_dispatch_email      = "${local.workspace_dispatch_account_id}@${var.project_id}.iam.gserviceaccount.com"
  workspace_dispatch_member     = "serviceAccount:${local.workspace_dispatch_email}"

  # The workflow's source, read from the file the owner reviews
  # (.github/CODEOWNERS, §2.4 safeguard 2).
  workspace_dispatch_source = file("${path.module}/workflows/workspace-apply.yaml")
}

# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

# No labels: google_service_account has none (scripts/lib/unlabelable-types.json).
# No create_ignore_already_exists, for the deployer's reason: an account
# someone made first under this name is a squat to investigate.
resource "google_service_account" "workspace_dispatch" {
  count = local.workspace_deployer_on

  project      = var.project_id
  account_id   = local.workspace_dispatch_account_id
  display_name = "Swarm workspace dispatch"
  description  = "managed-by=swarm-terraform; starts the swarm-workspace-apply job with a checked workspace id and mode, as the swarm-workspace-apply Eventarc trigger and workflow (docs/workspaces.md §2.1, option (ii)). No key, no WIF."
}

# AUTHORITATIVE AND EMPTY: no actAs, token creator or workloadIdentityUser on
# this account for anyone at the account level. Its own data source, not the
# deployer's, so the two policies are held apart by the tests.
data "google_iam_policy" "workspace_dispatch_nobody" {}

resource "google_service_account_iam_policy" "workspace_dispatch" {
  count = local.workspace_deployer_on

  service_account_id = google_service_account.workspace_dispatch[0].name
  policy_data        = data.google_iam_policy.workspace_dispatch_nobody.policy_data
}

# PROJECT-WIDE, because Workflows accepts nothing narrower (W0b (7), above).
# To the dispatch account only, which the Eventarc trigger runs as.
resource "google_project_iam_member" "workspace_dispatch_invoker" {
  count = local.workspace_deployer_on

  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = local.workspace_dispatch_member

  depends_on = [google_service_account.workspace_dispatch]
}

# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------

resource "google_workflows_workflow" "workspace_apply" {
  count = local.workspace_deployer_on

  project         = var.project_id
  region          = var.region
  name            = local.workspace_apply_name
  description     = "managed-by=swarm-terraform; checks an approval's workspace id and mode and starts one execution of the swarm-workspace-apply job with them (docs/workspaces.md §2.1, option (ii))."
  service_account = google_service_account.workspace_dispatch[0].id
  source_contents = local.workspace_dispatch_source
  labels          = local.labels

  # No call arguments or results in any log (§2.6 item 4): the event is the
  # opaque id, and this keeps even that out of the workflow's own log.
  call_log_level = "LOG_NONE"

  # Switching enable_workspace_deployer off must remove it, as for the job.
  deletion_protection = false

  depends_on = [google_service_account_iam_policy.workspace_dispatch]
}

# ---------------------------------------------------------------------------
# The Eventarc trigger: the existing topic, to the workflow
# ---------------------------------------------------------------------------

resource "google_eventarc_trigger" "workspace_apply" {
  count = local.workspace_deployer_on

  project         = var.project_id
  location        = var.region
  name            = local.workspace_apply_name
  service_account = google_service_account.workspace_dispatch[0].email
  labels          = local.labels

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }

  # The topic swarm-api already publishes to. Eventarc makes its own push
  # subscription on it; the topic's publishers stay swarm-api alone.
  transport {
    pubsub {
      topic = google_pubsub_topic.workspace_apply[0].id
    }
  }

  destination {
    workflow = google_workflows_workflow.workspace_apply[0].id
  }

  depends_on = [
    google_project_iam_member.workspace_dispatch_invoker,
    google_service_account_iam_policy.workspace_dispatch,
  ]
}
