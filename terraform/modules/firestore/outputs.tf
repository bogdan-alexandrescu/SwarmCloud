output "database_name" {
  description = "Pass to the apps as FIRESTORE_DATABASE. Never `(default)`."
  value       = google_firestore_database.this.name
}

output "database_id" {
  value = google_firestore_database.this.id
}

output "database_resource_name" {
  description = "Full resource name, used to scope Firestore IAM bindings with a condition."
  value       = "projects/${var.project_id}/databases/${google_firestore_database.this.name}"
}

output "index_names" {
  value = sort(keys(local.indexes))
}

output "scheduler_index_fields" {
  description = "Exposed so tests can assert the drain-loop index is state + priority DESC + created_at ASC."
  value       = local.indexes["tasks-state-priority-created"].fields
}

output "pool_names" {
  value = sort(keys(local.pool_documents))
}

output "tenant_document_ids" {
  value = sort(keys(local.tenant_documents))
}
