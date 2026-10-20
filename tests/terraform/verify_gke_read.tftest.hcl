# swarm-verify reads GKE Jobs on the swarm cluster and on no other cluster
# (lane VERIFY-GKE-READ, part of #1754; owner decision 2026-10-20).
#
# e2e-test.sh [10] compares a GKE-backed capacity holder with its Job. That is
# two reads: container.clusters.get for the endpoint and CA, then jobs.get in
# the tenant's namespace. The second is Kubernetes RBAC
# (kubernetes/rbac/verify-rbac.yaml); this file holds the IAM half:
#
#   1. swarm-verify holds roles/container.clusterViewer, conditioned to the
#      swarm cluster's own resource name -- GKE has no per-cluster IAM resource,
#      so the condition IS the scope -- and the expression never names the
#      shared project's agents-staging cluster;
#   2. with GKE off there is no cluster, so there is no grant;
#   3. every project-level grant to swarm-verify in terraform/ is one of the
#      four it holds, and the only container.* role among them is that one,
#      carrying its condition. A text scan, with the controls
#      schedule_tick_identity.tftest.hcl uses: the scan finds a grant that
#      exists, and reports an unconditioned container role in a file built to
#      hold one.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}

# ---------------------------------------------------------------------------
# 1. The grant: its role, its member, and the cluster its condition names
# ---------------------------------------------------------------------------

run "verify_views_the_swarm_cluster_and_no_other" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    deployer_service_account = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic"] }
    }
  }

  # MUTATION: grant roles/container.viewer (which reads every Kubernetes object
  # in every cluster, Secrets' metadata and Pods' env included) and this fails.
  assert {
    condition     = google_project_iam_member.verify_views_swarm_cluster[0].role == "roles/container.clusterViewer"
    error_message = "swarm-verify's GKE grant must be roles/container.clusterViewer: container.clusters.get for the endpoint and CA, and no Kubernetes object read. Jobs are read through the namespaced RBAC in kubernetes/rbac/verify-rbac.yaml"
  }

  # MUTATION: name another account (the scheduler's) and the gate still 403s.
  assert {
    condition     = google_project_iam_member.verify_views_swarm_cluster[0].member == "serviceAccount:swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the cluster read must be granted to swarm-verify, the gate's identity, and to nobody else"
  }

  assert {
    condition     = google_project_iam_member.verify_views_swarm_cluster[0].project == "saga-agents-staging"
    error_message = "the cluster read is granted in the platform's project"
  }

  # MUTATION: drop the condition block and the grant covers every cluster in
  # this shared project, the other team's agents-staging included.
  assert {
    condition     = length(google_project_iam_member.verify_views_swarm_cluster[0].condition) == 1
    error_message = "swarm-verify's clusterViewer must carry a condition: unconditioned, a project-level grant reaches every cluster in saga-agents-staging, agents-staging included"
  }

  # MUTATION: a bare startsWith(".../clusters/swarm") would also admit any
  # cluster whose name begins with it; the expression names the cluster
  # exactly, or a path under it.
  assert {
    condition = google_project_iam_member.verify_views_swarm_cluster[0].condition[0].expression == join(" || ", [
      "resource.name == \"projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot\"",
      "resource.name.startsWith(\"projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot/\")",
    ])
    error_message = "swarm-verify's clusterViewer condition must name projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot exactly (or a path under it), and nothing else"
  }

  # MUTATION: point the condition at the shared cluster and this fails.
  assert {
    condition     = length(regexall("agents-staging\"|clusters/agents-staging", google_project_iam_member.verify_views_swarm_cluster[0].condition[0].expression)) == 0
    error_message = "swarm-verify's cluster grant names agents-staging, the other team's cluster on the shared deny-list (scripts/lib/common.sh)"
  }

  # The marker make destroy reads: an IAM member takes no labels, so it is at
  # the start of the condition's description, as the dispatchers' grants carry it.
  assert {
    condition     = startswith(google_project_iam_member.verify_views_swarm_cluster[0].condition[0].description, "managed-by=swarm-terraform;")
    error_message = "the condition's description must start with managed-by=swarm-terraform; (an IAM member takes no labels)"
  }
}

# ---------------------------------------------------------------------------
# 2. No cluster, no grant
# ---------------------------------------------------------------------------

run "no_gke_no_cluster_read" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_gke_autopilot     = false
    deployer_service_account = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
    tenants = {
      eng = { kind = "group", principal = "eng@example.com", providers = ["anthropic"] }
    }
  }

  # MUTATION: drop the count and a GKE-less environment grants a read on a
  # cluster that is not there -- under a condition naming nothing of ours.
  assert {
    condition     = length(google_project_iam_member.verify_views_swarm_cluster) == 0
    error_message = "with enable_gke_autopilot off there is no swarm cluster, so swarm-verify must hold no container role"
  }
}

# ---------------------------------------------------------------------------
# 3. No other container role, anywhere in terraform/
# ---------------------------------------------------------------------------

run "verify_holds_no_other_project_level_container_role" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids = []

    tf_root   = "../../terraform"
    verify_re = "google_service_account\\.verify\\.email|swarm-verify@"
    grant_re  = "(?ms)^resource \"(google_project_iam_member|google_project_iam_binding|google_project_iam_policy)\" \"([^\"]+)\" \\{\\n(.*?)^\\}"
  }

  # The control on the read itself: the scan found the files.
  assert {
    condition = (
      length(fileset(var.tf_root, "infra/**/*.tf")) > 10
      && length(fileset(var.tf_root, "modules/**/*.tf")) > 10
      && length(fileset(var.tf_root, "bootstrap/**/*.tf")) > 0
    )
    error_message = "the scan read no terraform/infra, terraform/modules or terraform/bootstrap files; every assertion below would pass on nothing"
  }

  # THE CHECK. Every project-level grant naming swarm-verify.
  # MUTATION: add a google_project_iam_member granting swarm-verify
  # roles/container.viewer and this list is one longer.
  assert {
    condition = sort(flatten([
      for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf"), fileset(var.tf_root, "bootstrap/**/*.tf")) : [
        for m in regexall(var.grant_re, file("${var.tf_root}/${f}")) : "${f} ${m[0]}.${m[1]}"
        if length(regexall(var.verify_re, m[2])) > 0
      ]
      ])) == tolist([
      "infra/verify.tf google_project_iam_member.verify_reads_firestore",
      "infra/verify.tf google_project_iam_member.verify_reads_run",
      "infra/verify.tf google_project_iam_member.verify_views_swarm_cluster",
    ])
    error_message = "swarm-verify holds a project-level grant besides datastore.viewer, run.viewer and the conditioned clusterViewer, or one of those is gone. A project-level role reaches the other team's resources in this shared project"
  }

  # THE CHECK on the role: of those grants, the only one with a container role
  # is verify_views_swarm_cluster, and its body carries a condition.
  # MUTATION: delete the condition block from verify_views_swarm_cluster and
  # this list is empty.
  assert {
    condition = flatten([
      for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf"), fileset(var.tf_root, "bootstrap/**/*.tf")) : [
        for m in regexall(var.grant_re, file("${var.tf_root}/${f}")) : "${m[1]} ${length(regexall("(?m)^\\s*condition \\{", m[2])) > 0 ? "conditioned" : "unconditioned"}"
        if length(regexall(var.verify_re, m[2])) > 0 && length(regexall("roles/container\\.", m[2])) > 0
      ]
    ]) == tolist(["verify_views_swarm_cluster conditioned"])
    error_message = "swarm-verify's only container role must be verify_views_swarm_cluster's clusterViewer, conditioned to the swarm cluster"
  }

  # THE CONTROL: the scan reports an unconditioned container role to
  # swarm-verify in a file built to hold one, so the check above could fail.
  assert {
    condition = [
      for m in regexall(var.grant_re, join("\n", [
        "resource \"google_project_iam_member\" \"verify_views_swarm_cluster\" {",
        "  role   = \"roles/container.clusterViewer\"",
        "  member = \"serviceAccount:$${google_service_account.verify.email}\"",
        "  condition {",
        "    expression = \"x\"",
        "  }",
        "}",
        "",
        "resource \"google_project_iam_member\" \"verify_reads_everything\" {",
        "  role   = \"roles/container.viewer\"",
        "  member = \"serviceAccount:$${google_service_account.verify.email}\"",
        "}",
        "",
        "resource \"google_project_iam_member\" \"someone_else\" {",
        "  role   = \"roles/container.viewer\"",
        "  member = \"serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com\"",
        "}",
        "",
      ])) : "${m[1]} ${length(regexall("(?m)^\\s*condition \\{", m[2])) > 0 ? "conditioned" : "unconditioned"}"
      if length(regexall(var.verify_re, m[2])) > 0 && length(regexall("roles/container\\.", m[2])) > 0
      ] == [
      "verify_views_swarm_cluster conditioned",
      "verify_reads_everything unconditioned",
    ]
    error_message = "the scan does not report an unconditioned container role to swarm-verify in a file built to hold one; the check above could not fail"
  }
}
