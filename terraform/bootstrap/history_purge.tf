# ---------------------------------------------------------------------------
# swarmHistoryPurgeDeleter: the audited history purge may delete a purged
# run's own files, and nothing else (owner decision 2026-10-11).
# ---------------------------------------------------------------------------
#
# POST /v1/admin/history:purge (apps/swarm-api/swarm_api/purge.py) deletes a
# terminal workflow's or task's documents and the objects under
# tenants/<stored tenant>/tasks/<id>/. swarm-api held only
# roles/storage.objectViewer on the artifact bucket, so a purge of a run with
# files was refused by GCS and audited as history_purge_failed.
#
# THE ROLE IS DEFINED HERE AND GRANTED IN terraform/modules/iam. Every custom
# role lives in this root, which the owner applies: CI holds no iam.roles.*
# permission (#79, platform_roles.tf says why), and
# tests/terraform/custom_roles_out_of_ci.tftest.hcl fails if terraform/infra or
# a module defines one. The grant is a bucket IAM member like swarm-api's other
# artifact grants (modules/iam bindings.tf, `api_history_purge_deleter`), with
# a condition admitting tenants/<t>/tasks/ and tenants/<t>/checkpoints/ only.
# Both roots read the id from terraform/modules/custom_role_ids.
#
# ORDER: the owner applies this root BEFORE the release that grants the role,
# or that release's bucket setIamPolicy names a role that does not exist yet
# and fails. The release is an IAM change and waits at dev-iam for the owner
# anyway; apply bootstrap, then approve dev-iam.
#
# A NEW ROLE, NOT ONE OF platform_roles: those eight are held byte for byte to
# a fixture read from the live project before #79's move, and each is IMPORTED
# when adopt_from_infra_states is set. This one never existed in
# terraform/infra, so there is nothing to import and no fixture to match.

resource "google_project_iam_custom_role" "history_purge_deleter" {
  project     = var.project_id
  role_id     = module.custom_role_ids.ids["history_purge_deleter"]
  title       = "Swarm History Purge Deleter"
  description = "managed-by=swarm-terraform; swarm-api deletes a purged run's objects under tenants/<t>/tasks/ and tenants/<t>/checkpoints/ (POST /v1/admin/history:purge). Delete only: no read, list, create or IAM."
  stage       = "GA"

  # storage.objects.delete alone. Not roles/storage.objectAdmin or objectUser,
  # which would add create and update -- swarm-api produces no artifacts.
  #
  # storage.objects.get is NOT needed. GcsArtifactPurger.delete_keys builds
  # each blob from a key with bucket.blob(k), which makes no request, and
  # deletes it with an unconditional DELETE (no generation precondition), and
  # Cloud Storage authorizes that DELETE on storage.objects.delete alone. The
  # listing before it (list_blobs) is storage.objects.list, which swarm-api
  # already holds bucket-wide through roles/storage.objectViewer
  # (`api_reader`), as it does storage.objects.get; adding either here would
  # grant nothing new and widen the role's argument for no reason.
  permissions = ["storage.objects.delete"]
}
