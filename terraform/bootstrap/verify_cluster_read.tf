# ---------------------------------------------------------------------------
# swarm-verify's read of the swarm cluster -- and of no other cluster
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. scripts/e2e-test.sh [10] ("No task is holding capacity
# against an execution that is gone") compares a task holding capacity on GKE
# with its Job. That is two reads: container.clusters.get for the cluster's
# endpoint and CA, then jobs.get in the tenant's namespace. The second is
# Kubernetes RBAC (kubernetes/rbac/verify-rbac.yaml, `get` and `list` on
# jobs.batch in the eng and smoke namespaces, applied by kubernetes/apply.sh).
# This is the first. Without it the gate could not see a dead GKE Job holding
# a slot (#1754, owner decision 2026-10-20).
#
# WHY HERE AND NOT IN terraform/infra (owner decision 2026-10-21,
# VERIFY-GKE-READ-2). THE OWNER APPLIES THIS (`make bootstrap`); CI never does.
# terraform/infra is applied by CI's deployer, whose scoped projectIamAdmin may
# modify only the roles on deployer_grantable_project_roles
# (deployer_conditions.tf). hasOnly() limits WHICH roles a policy change
# touches, never to whom or under what condition, so granting clusterViewer
# from terraform/infra would need it on that list -- and then any CI run could
# grant clusterViewer to itself or anyone, unconditioned, reaching the other
# team's agents-staging cluster in this shared project. Granted from here, the
# role stays off CI's list: tests/terraform/deployer_iam.tftest.hcl holds that
# no container role is on it, and tests/terraform/verify_gke_read.tftest.hcl
# that terraform/infra grants swarm-verify none.
#
# clusterViewer, NOT container.viewer: clusterViewer is container.clusters.get
# and .list and no Kubernetes object at all, so it cannot read a Secret, a Pod
# or a log through IAM. What the gate reads inside the cluster is only what
# verify-rbac.yaml's Role binds.
#
# A CONDITIONED PROJECT BINDING BECAUSE THERE IS NO OTHER KIND. GKE has no
# per-cluster IAM resource, so the condition on the cluster's resource name is
# the scope. The expression names the cluster EXACTLY, or a path under it
# (`.../clusters/swarm-autopilot/...`). A bare startsWith on the name -- the
# form workspace_deployer.tf's cluster_viewer uses -- would also admit any
# cluster whose name began with ours (swarm-autopilot2). Neither form can
# match agents-staging, whose name does not start with ours.
#
# The account is this root's own (platform_service_accounts.tf, DAYONE-BOOT-2),
# spelled by modules/service_account_ids -- the one spelling both roots read --
# and made before this grant in the same apply (depends_on below). An IAM
# member takes no labels, so managed-by=swarm-terraform opens the condition's
# description, as the workspace deployer's grants carry it.

locals {
  # The swarm cluster, as terraform/infra names it ("${var.name_prefix}-autopilot",
  # main.tf) -- the same local the workspace deployer's grant reads.
  verify_swarm_cluster = local.workspace_cluster_name
}

resource "google_project_iam_member" "verify_views_swarm_cluster" {
  project = var.project_id
  role    = "roles/container.clusterViewer"
  member  = "serviceAccount:${module.service_account_ids.verify_id}@${var.project_id}.iam.gserviceaccount.com"

  condition {
    title       = "swarm-cluster-only"
    description = "managed-by=swarm-terraform; the gate reads the swarm Autopilot cluster's endpoint and CA (e2e-test.sh [10]); agents-staging belongs to another team. Owner-applied: CI never grants it."
    expression = join(" || ", [
      "resource.name == \"${local.verify_swarm_cluster}\"",
      "resource.name.startsWith(\"${local.verify_swarm_cluster}/\")",
    ])
  }

  depends_on = [google_service_account.verify]
}
