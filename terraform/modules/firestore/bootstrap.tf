# Provisioning-time control-plane documents.
#
# swarm_common.admission.evaluate_capacity treats a MISSING pool document as
# unlimited: "the global pool and the tenant pool are always created at
# provisioning time, so a missing pool here is a narrow named one that was never
# capped." That comment is a promise this file keeps. Without it the very first
# scheduler pass on a fresh environment would find no `global` document and
# admit without any ceiling at all.
#
# `active` is mutated by the admission transaction on every lease, so every
# field is under ignore_changes. Terraform creates these documents once and then
# stops having an opinion about their contents; the alternative is an apply that
# resets live concurrency counters to zero and instantly oversubscribes every
# pool.

locals {
  bootstrap_enabled = var.bootstrap_documents ? 1 : 0

  pool_documents = var.bootstrap_documents ? var.pools : {}

  tenant_documents = var.bootstrap_documents ? var.tenant_documents : {}
}

resource "google_firestore_document" "pool" {
  for_each = local.pool_documents

  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "pools"
  document_id = each.key

  fields = jsonencode({
    name       = { stringValue = each.key }
    hard_limit = { integerValue = tostring(each.value.hard_limit) }
    active     = { integerValue = "0" }
    enabled    = { booleanValue = each.value.enabled }
    managed_by = { stringValue = "swarm-terraform" }
  })

  lifecycle {
    ignore_changes = [fields]
  }
}

resource "google_firestore_document" "tenant" {
  for_each = local.tenant_documents

  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "tenants"
  document_id = each.key

  fields = jsonencode({
    tenant_id       = { stringValue = each.key }
    kind            = { stringValue = each.value.kind }
    principal       = { stringValue = each.value.principal }
    display_name    = { stringValue = each.value.display_name }
    max_active      = { integerValue = tostring(each.value.max_active) }
    capacity_units  = { integerValue = tostring(each.value.capacity_units) }
    enabled         = { booleanValue = true }
    service_account = { stringValue = each.value.service_account }
    gcs_prefix      = { stringValue = each.value.gcs_prefix }
    namespace       = { stringValue = each.value.namespace }
    credentials = {
      arrayValue = {
        values = [for p in each.value.credentials : { stringValue = p }]
      }
    }
    managed_by = { stringValue = "swarm-terraform" }
  })

  lifecycle {
    # An admin raising a tenant's limits, or the API recording a newly
    # registered provider key, must not be reverted by the next apply.
    ignore_changes = [fields]
  }
}
