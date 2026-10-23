# The runner git tools: the bucket side of a large git_fetch (#1815, lane
# GIT-FETCH-BUNDLES; docs/runner-git-tools.md §1.3).
#
# swarm-api answers a git_fetch whose bundle is over gittools.INLINE_BUNDLE_BYTES
# (24 MiB) by putting the bundle in the artifact bucket and naming it, and the
# worker reads it back under its own tenant prefix:
#
#   tenants/<t>/attempts/<a>/git/<call>.fetch.bundle      (gittools.FETCH_OBJECT)
#
# Until this grant swarm-api could not create that object: GCS answered 403,
# the route answered a bare 500, and the worker retried it (134 of 415 fetches
# in 48 h, #1815). This file holds swarm-api's ONE write grant for it, and
# nothing else. The owner chose it (option A, 2026-10-21) over streaming the
# bundle in the response body.
#
# CREATE-ONLY. roles/storage.objectCreator is create and nothing else: no get,
# no list, no delete. A create over an existing object needs delete, so it
# cannot overwrite either; GcsBundles.upload creates with if_generation_match=0
# on top of that, and a refused create is the named refusal
# `fetch_bundle_store_failed`, never a 500. swarm-api's read stays `api_reader`
# in modules/iam/bindings.tf, unchanged.
#
# BUCKET-SCOPED. A member of the artifact bucket's policy, never the project's:
# this shared project holds another team's buckets.
#
# THE TENANT IS FIXED BY THE CODE, NOT BY THIS CONDITION. One swarm-api serves
# every tenant, so no condition can tie the tenant segment to the caller. The
# key is built from the PROVEN attempt (its tenant's own worker identity and
# attempt key), never from the request body, and the worker refuses a
# bundle_object outside tenants/<own>/attempts/ (agent_worker.gittools
# `_received_bundle`). The condition pins the SHAPE, so this grant cannot
# reach a task's artifacts, a verdict, a checkpoint, the repository index or a
# worker's push bundle.
#
# IT CHANGES IAM, so the dev release that carries it waits at `dev-iam` for the
# owner (docs/ci.md, "A dev release that changes IAM waits for the owner").

locals {
  # The grant's condition, six clauses ANDed. Cloud Storage IAM conditions have
  # no matches() (Google refused one with a 400, release run 38114832799;
  # modules/iam bindings.tf `history_purge_condition` records it), and a
  # startsWith per tenant would miss the personal tenants created at runtime
  # that var.tenants does not list. So the shape is pinned with startsWith,
  # extract() comparisons and endsWith:
  #
  #   1. an object in this bucket under tenants/ -- never another bucket, a
  #      longer bucket name, the bucket itself or a key outside tenants/;
  #   2. T, the text after /objects/tenants/ up to the next "/", is not empty;
  #   3. T is also the text before the FIRST "/attempts/" after it, so the name
  #      is tenants/T/attempts/... with T one segment (not tenants/a/b/attempts/,
  #      tenants/a/tasks/t/attempts/ or tenants/a/repos/r/attempts/);
  #   4. A, the segment after that first "/attempts/", is not empty;
  #   5. A is also the text before the first "/git/" after it, so the name is
  #      tenants/T/attempts/A/git/... -- never attempts/A/live/git/ or a git/
  #      written further down inside another prefix;
  #   6. the name ends .fetch.bundle: never a worker's push bundle (.bundle),
  #      which swarm-api only reads.
  #
  # extract("pre{x}suf") returns the text between the FIRST pre and the first
  # suf after it, "" when either is missing, and {x} may span "/" (Google's
  # attribute reference). Each placeholder is one segment only because it is
  # compared with a template whose suffix is "/". The first "/attempts/" is the
  # one after T because the bucket name and T hold no "/" and clause 3 puts one
  # there.
  #
  # What it does not pin: that <call> is one segment. Without matches() it is
  # not expressible, and it does not widen the reach: every admitted key is
  # still under one attempt's own git/. swarm-api's request_id pattern keeps
  # "/" out of it before GCS is called. A tenant whose id is literally
  # "attempts" is refused (clause 3 then reads tenants/attempts/'s own
  # "/attempts/"); that errs closed, and tenant ids are names an operator
  # registers.
  #
  # Google's iamPolicies:lintPolicy returned {} for this expression on
  # 2026-10-21 (and ConditionCompileCheck ERROR for a matches() control).
  # tests/terraform/git_tools_iam.tftest.hcl holds the text verbatim and
  # evaluates it against admitted and refused names, clause by clause.
  git_fetch_bundle_condition = join(" && ", [
    "resource.name.startsWith(\"projects/_/buckets/${module.storage.artifact_bucket_name}/objects/tenants/\")",
    "resource.name.extract(\"/objects/tenants/{t}/\") != \"\"",
    "resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/attempts/\")",
    "resource.name.extract(\"/attempts/{a}/\") != \"\"",
    "resource.name.extract(\"/attempts/{a}/\") == resource.name.extract(\"/attempts/{a}/git/\")",
    "resource.name.endsWith(\".fetch.bundle\")",
  ])
}

# An IAM member takes no labels; destroy-guard.jq treats it as a policy edge,
# and its condition's description carries managed-by=swarm-terraform.
resource "google_storage_bucket_iam_member" "api_fetch_bundle_writer" {
  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectCreator"
  member = module.iam.service_account_members["swarm-api"]

  condition {
    title       = "git-fetch-bundles-only"
    description = "managed-by=swarm-terraform; swarm-api creates a large git_fetch bundle under tenants/<t>/attempts/<a>/git/<call>.fetch.bundle only (#1815)."
    expression  = local.git_fetch_bundle_condition
  }
}

# --- no -git-wf slot (#2029, WF-APP-TOKEN) -----------------------------------
#
# The workflow-scoped `swarm-tenant-<tenant>-git-wf` slot and swarm-api's
# accessor on it (IAM row L-2) were removed by BGD's decision of 2026-10-22.
# swarm-api now pushes a change under .github/workflows/ or .github/actions/
# with a token the SwarmCloud GitHub App mints at push time, for ONE
# repository, from the workspace's own installation, and only for a
# registration whose `workflow_files` switch is on
# (docs/design/agent-autonomy.md §3.3). Nothing is stored, so there is no
# secret and no IAM row for it. tests/terraform/git_wf_slot.tftest.hcl holds
# that no `*-git-wf` secret is declared and swarm-api holds no accessor on one.
#
# The removal changes IAM, so the dev release that carries it waits at
# `dev-iam` for the owner (docs/ci.md, "A dev release that changes IAM waits
# for the owner").
