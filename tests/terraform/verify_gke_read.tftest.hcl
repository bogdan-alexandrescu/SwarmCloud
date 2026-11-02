# swarm-verify reads GKE Jobs on the swarm cluster and on no other cluster
# (lanes VERIFY-GKE-READ and VERIFY-GKE-READ-2, part of #1754; owner decisions
# 2026-10-20 and 2026-10-21).
#
# e2e-test.sh [10] compares a GKE-backed capacity holder with its Job. That is
# two reads: container.clusters.get for the endpoint and CA, then jobs.get in
# the tenant's namespace. The second is Kubernetes RBAC
# (kubernetes/rbac/verify-rbac.yaml); this file holds the IAM half:
#
#   1. swarm-verify holds roles/container.clusterViewer, conditioned to the
#      swarm-autopilot cluster's own resource name -- GKE has no per-cluster
#      IAM resource, so the condition IS the scope -- and the expression never
#      names the shared project's agents-staging cluster;
#   2. the grant is made by terraform/bootstrap, which the OWNER applies, and
#      never by terraform/infra, which CI applies: CI's scoped projectIamAdmin
#      does not carry clusterViewer (deployer_iam.tftest.hcl holds that), and
#      terraform/infra grants swarm-verify no container role at all;
#   3. every project-level grant to swarm-verify in terraform/ is one of the
#      three it holds, and the only container.* role among them is that one,
#      carrying its condition. A text scan, with the controls
#      schedule_tick_identity.tftest.hcl uses: the scan finds a grant that
#      exists, and reports an unconditioned container role in a file built to
#      hold one.

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
# 1. The grant, in terraform/bootstrap: its role, its member, and the cluster
#    its condition names
# ---------------------------------------------------------------------------

run "verify_views_the_swarm_cluster_and_no_other" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  # MUTATION: grant roles/container.viewer (which reads every Kubernetes object
  # in every cluster, Secrets' metadata and Pods' env included) and this fails.
  assert {
    condition     = google_project_iam_member.verify_views_swarm_cluster.role == "roles/container.clusterViewer"
    error_message = "swarm-verify's GKE grant must be roles/container.clusterViewer: container.clusters.get for the endpoint and CA, and no Kubernetes object read. Jobs are read through the namespaced RBAC in kubernetes/rbac/verify-rbac.yaml"
  }

  # MUTATION: name another account (the scheduler's) and the gate still 403s.
  assert {
    condition     = google_project_iam_member.verify_views_swarm_cluster.member == "serviceAccount:swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the cluster read must be granted to swarm-verify, the gate's identity, and to nobody else"
  }

  # MUTATION: grant it in another project and the gate still 403s.
  assert {
    condition     = google_project_iam_member.verify_views_swarm_cluster.project == "saga-agents-staging"
    error_message = "the cluster read is granted in the platform's project"
  }

  # MUTATION: drop the condition block and the grant covers every cluster in
  # this shared project, the other team's agents-staging included.
  assert {
    condition     = length(google_project_iam_member.verify_views_swarm_cluster.condition) == 1
    error_message = "swarm-verify's clusterViewer must carry a condition: unconditioned, a project-level grant reaches every cluster in saga-agents-staging, agents-staging included"
  }

  # MUTATION: a bare startsWith(".../clusters/swarm-autopilot") -- the form
  # the workspace deployer's grant uses -- would also admit any cluster whose
  # name begins with it (swarm-autopilot2); the expression names the cluster
  # exactly, or a path under it.
  assert {
    condition = google_project_iam_member.verify_views_swarm_cluster.condition[0].expression == join(" || ", [
      "resource.name == \"projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot\"",
      "resource.name.startsWith(\"projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot/\")",
    ])
    error_message = "swarm-verify's clusterViewer condition must name projects/saga-agents-staging/locations/us-central1/clusters/swarm-autopilot exactly (or a path under it), and nothing else"
  }

  # MUTATION: point the condition at the shared cluster and this fails.
  assert {
    condition     = length(regexall("agents-staging\"|clusters/agents-staging", google_project_iam_member.verify_views_swarm_cluster.condition[0].expression)) == 0
    error_message = "swarm-verify's cluster grant names agents-staging, the other team's cluster on the shared deny-list (scripts/lib/common.sh)"
  }

  # The marker make destroy reads: an IAM member takes no labels, so it is at
  # the start of the condition's description, as the dispatchers' grants carry it.
  # MUTATION: drop the prefix from the description and this fails.
  assert {
    condition     = startswith(google_project_iam_member.verify_views_swarm_cluster.condition[0].description, "managed-by=swarm-terraform;")
    error_message = "the condition's description must start with managed-by=swarm-terraform; (an IAM member takes no labels)"
  }

  # IAM conditions have an undocumented 256-character description limit, found
  # only at apply (deployer_iam.tftest.hcl).
  # MUTATION: lengthen the description past 256 characters and this fails.
  assert {
    condition     = length(google_project_iam_member.verify_views_swarm_cluster.condition[0].description) <= 256
    error_message = "the condition's description is over IAM's 256-character limit; the owner's bootstrap apply would fail on it"
  }
}

# ---------------------------------------------------------------------------
# 2. Where it may be granted from: bootstrap holds the one grant, terraform/infra
#    and its modules hold no container role for swarm-verify at all
# ---------------------------------------------------------------------------

run "verify_holds_no_other_project_level_container_role" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids = []

    tf_root = "../../terraform"
    # local.verify_email / data.google_service_account.verify: how
    # terraform/infra names the account since DAYONE-BOOT-2 moved it to
    # terraform/bootstrap and infra reads it.
    verify_re = "google_service_account\\.verify\\.email|swarm-verify@|service_account_ids\\.verify_id|local\\.verify_email|data\\.google_service_account\\.verify\\b"
    grant_re  = "(?ms)^resource \"(google_project_iam_member|google_project_iam_binding|google_project_iam_policy)\" \"([^\"]+)\" \\{\\n(.*?)^\\}"
  }

  # The control on the read itself: the scan found the files.
  assert {
    condition = (
      length(fileset(var.tf_root, "infra/**/*.tf")) > 10
      && length(fileset(var.tf_root, "modules/**/*.tf")) > 10
      && length(fileset(var.tf_root, "bootstrap/**/*.tf")) > 10
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
      "bootstrap/verify_cluster_read.tf google_project_iam_member.verify_views_swarm_cluster",
      "infra/verify.tf google_project_iam_member.verify_reads_firestore",
      "infra/verify.tf google_project_iam_member.verify_reads_run",
    ])
    error_message = "swarm-verify holds a project-level grant besides datastore.viewer, run.viewer (terraform/infra) and the conditioned clusterViewer (terraform/bootstrap), or one of those is gone or has moved roots. A project-level role reaches the other team's resources in this shared project"
  }

  # THE CHECK on CI's root: terraform/infra and the modules it calls grant
  # swarm-verify no container role, conditioned or not. CI applies that root,
  # so a container grant there would need clusterViewer back on CI's allow-list.
  # MUTATION: move verify_views_swarm_cluster back into terraform/infra/verify.tf
  # and this list is one longer.
  assert {
    condition = length(flatten([
      for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf")) : [
        for m in regexall(var.grant_re, file("${var.tf_root}/${f}")) : m[1]
        if length(regexall(var.verify_re, m[2])) > 0 && length(regexall("roles/container\\.", m[2])) > 0
      ]
    ])) == 0
    error_message = "terraform/infra (or a module it calls) grants swarm-verify a container role. CI applies that root; the conditioned clusterViewer is terraform/bootstrap's, which the owner applies"
  }

  # THE CHECK on the role: of those grants, the only one with a container role
  # is bootstrap's verify_views_swarm_cluster, and its body carries a condition.
  # MUTATION: delete the condition block from verify_views_swarm_cluster and
  # this list reads "unconditioned".
  assert {
    condition = tolist(flatten([
      for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf"), fileset(var.tf_root, "bootstrap/**/*.tf")) : [
        for m in regexall(var.grant_re, file("${var.tf_root}/${f}")) : "${f} ${m[1]} ${length(regexall("(?m)^\\s*condition \\{", m[2])) > 0 ? "conditioned" : "unconditioned"}"
        if length(regexall(var.verify_re, m[2])) > 0 && length(regexall("roles/container\\.", m[2])) > 0
      ]
    ])) == tolist(["bootstrap/verify_cluster_read.tf verify_views_swarm_cluster conditioned"])
    error_message = "swarm-verify's only container role must be terraform/bootstrap's verify_views_swarm_cluster clusterViewer, conditioned to the swarm cluster"
  }

  # THE CONTROL: the scan reports an unconditioned container role to
  # swarm-verify in a file built to hold one, whichever way the account is
  # named, so the checks above could fail.
  assert {
    condition = tolist([
      for m in regexall(var.grant_re, join("\n", [
        "resource \"google_project_iam_member\" \"verify_views_swarm_cluster\" {",
        "  role   = \"roles/container.clusterViewer\"",
        "  member = \"serviceAccount:$${module.service_account_ids.verify_id}@$${var.project_id}.iam.gserviceaccount.com\"",
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
        "resource \"google_project_iam_member\" \"verify_reads_everything_by_local\" {",
        "  role   = \"roles/container.viewer\"",
        "  member = \"serviceAccount:$${local.verify_email}\"",
        "}",
        "",
        "resource \"google_project_iam_member\" \"someone_else\" {",
        "  role   = \"roles/container.viewer\"",
        "  member = \"serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com\"",
        "}",
        "",
      ])) : "${m[1]} ${length(regexall("(?m)^\\s*condition \\{", m[2])) > 0 ? "conditioned" : "unconditioned"}"
      if length(regexall(var.verify_re, m[2])) > 0 && length(regexall("roles/container\\.", m[2])) > 0
      ]) == tolist([
      "verify_views_swarm_cluster conditioned",
      "verify_reads_everything unconditioned",
      "verify_reads_everything_by_local unconditioned",
    ])
    error_message = "the scan does not report an unconditioned container role to swarm-verify in a file built to hold one; the checks above could not fail"
  }
}
