# A workflow step that waits for a tenant member's answer: the bucket side
# (docs/design/human-steps.md §4.3; lane HW-IAM, part of #1436).
#
# swarm-api writes a member's answer -- the form as the step's artifact and up
# to ten attached files beside it -- under an attempt id of its own,
# `input-<12 hex>`, so dependants stage it with input_from like any worker's
# artifact (Q10, Q12):
#
#   tenants/<t>/tasks/<task>/attempts/input-<nonce>/artifacts/<artifact>
#   tenants/<t>/tasks/<task>/attempts/input-<nonce>/artifacts/<stem>.files/<file>
#
# This file holds swarm-api's ONE write grant for that, and nothing else. The
# expiry sweep's index is the firestore module's (`await_input_expiry`).
#
# CREATE-ONLY. roles/storage.objectCreator is create and nothing else: no get,
# no list, no delete. A create over an existing object needs delete, so it
# cannot overwrite either -- live.tf's `live_relay_records` records the same
# reasoning. The writer (apps/swarm-api/swarm_api/inputwriter.py, HW-API-ANSWER)
# creates with if_generation_match=0 on top of that. swarm-api's read stays
# `api_reader` in modules/iam/bindings.tf, unchanged, so objects.py remains a
# reader with no write method to call by accident.
#
# BUCKET-SCOPED. A member of the artifact bucket's policy, never the project's:
# this shared project holds another team's buckets.
#
# SHIPS DARK. The grant exists before anything uses it: the `await-input`
# profile is unavailable (Request C) until the owner applies Request D, and
# until HW-API-ANSWER merges no code holds a write call. It changes IAM, so
# the dev release that carries it waits at `dev-iam` for the owner (docs/ci.md,
# "A dev release that changes IAM waits for the owner").

locals {
  # The grant's condition. Cloud Storage IAM conditions have no matches()
  # (Google refused one with a 400, release run 38114832799; modules/iam
  # bindings.tf `history_purge_condition` records it), so the shape is pinned
  # with startsWith and extract() comparisons, seven clauses ANDed:
  #
  #   1. an object in this bucket under tenants/ -- never another bucket, a
  #      longer bucket name, the bucket itself or a key outside tenants/;
  #   2. T, the text after /objects/tenants/ up to the next "/", is not empty;
  #   3. T is also the text before the FIRST "/tasks/" after it, so the name
  #      is tenants/T/tasks/... with T one segment (not tenants/a/b/tasks/,
  #      tenants/a/repos/r/tasks/ or tenants/a/verdicts/...);
  #   4. K, the segment after that first "/tasks/", is not empty;
  #   5. K is also the text before the first "/attempts/input-" after it, so
  #      the name is tenants/T/tasks/K/attempts/input-... -- a worker attempt
  #      (attempts/att_x/) has no "/attempts/input-" where this one needs it,
  #      and an input path written INSIDE a worker attempt's artifacts puts
  #      "K/attempts/att_x/artifacts" before it, which is not K;
  #   6. N, the nonce after the first "/attempts/input-", is not empty;
  #   7. N is also the text before the first "/artifacts/" after it, so the
  #      next segment is artifacts/: never checkpoints/, live/ or logs/, nor a
  #      path that reaches "/artifacts/" further down.
  #
  # extract("pre{x}suf") returns the text between the FIRST pre and the first
  # suf after it, "" when either is missing, and {x} may span "/" (Google's
  # attribute reference). Nothing here relies on it stopping at a slash: each
  # placeholder is one segment only because it is compared with a template
  # whose suffix is "/". The first "/tasks/" is the one after T because the
  # bucket name and T hold no "/" and clause 3 puts one there; likewise the
  # first "/attempts/input-" is the one after K.
  #
  # What it does not pin: the nonce's twelve hex characters, and a non-empty
  # artifact name. Neither is expressible without matches(), and neither
  # widens the reach: every admitted key is still under one task's own
  # input attempt's artifacts/. HW-API-ANSWER's inputwriter refuses any other
  # shape before it calls GCS. A tenant whose id is literally "tasks" is
  # refused (clause 4 then reads the "/tasks/" of tenants/tasks/); that errs
  # closed, and tenant ids are names an operator registers.
  #
  # tests/terraform/await_input.tftest.hcl evaluates this expression against
  # admitted and refused names, clause by clause.
  await_input_object_condition = join(" && ", [
    "resource.name.startsWith(\"projects/_/buckets/${module.storage.artifact_bucket_name}/objects/tenants/\")",
    "resource.name.extract(\"/objects/tenants/{t}/\") != \"\"",
    "resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/tasks/\")",
    "resource.name.extract(\"/tasks/{k}/\") != \"\"",
    "resource.name.extract(\"/tasks/{k}/\") == resource.name.extract(\"/tasks/{k}/attempts/input-\")",
    "resource.name.extract(\"/attempts/input-{n}/\") != \"\"",
    "resource.name.extract(\"/attempts/input-{n}/\") == resource.name.extract(\"/attempts/input-{n}/artifacts/\")",
  ])
}

# An IAM member takes no labels; destroy-guard.jq treats it as a policy edge,
# and its condition's description carries managed-by=swarm-terraform.
resource "google_storage_bucket_iam_member" "api_input_writer" {
  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectCreator"
  member = module.iam.service_account_members["swarm-api"]

  condition {
    title       = "await-input-answers-only"
    description = "managed-by=swarm-terraform; swarm-api creates a member's answer under tenants/<t>/tasks/<task>/attempts/input-<nonce>/artifacts/ only (docs/design/human-steps.md section 4.3)."
    expression  = local.await_input_object_condition
  }
}
