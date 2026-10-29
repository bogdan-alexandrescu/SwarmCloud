# A group tenant's worker reads one listed member's personal TypeSafe key
# (JEV-RUNNER, owner decision 2026-10-14; terraform/bootstrap/
# typesafe_user_keys.tf). These runs hold:
#
#   * nothing is granted while typesafe_user_key_readers is empty;
#   * one row is one binding: secretAccessor, to that group's worker, on that
#     one person's -typesafe secret and its versions, by project NUMBER;
#   * the condition names no other secret of the person's tenant;
#   * a personal id that is not u-<user>, or a u- group, is refused.
#
# None of this proves what IAM does live; see the file's "NOT VERIFIED LIVE".

mock_provider "google" {
  source = "./mocks/google"
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
  infra_tenants_tfvars = "../../tests/terraform/browser_logins_fixture/tenants.tfvars"
}

run "no_rows_no_grant" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  assert {
    condition     = length(google_project_iam_member.typesafe_user_key_reader) == 0
    error_message = "with no rows, no group may read anyone's TypeSafe key"
  }
}

run "one_row_one_secret" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  override_data {
    target          = data.google_project.typesafe_user_keys
    override_during = plan
    values = {
      number = "209012342332"
    }
  }

  variables {
    typesafe_user_key_readers = { eng = ["u-bogdan"] }
  }

  assert {
    condition     = toset(keys(google_project_iam_member.typesafe_user_key_reader)) == toset(["eng/u-bogdan"])
    error_message = "one row is one binding"
  }

  assert {
    condition     = google_project_iam_member.typesafe_user_key_reader["eng/u-bogdan"].role == "roles/secretmanager.secretAccessor"
    error_message = "the grant is read, and nothing else"
  }

  assert {
    condition     = google_project_iam_member.typesafe_user_key_reader["eng/u-bogdan"].condition[0].expression == "resource.name == \"projects/209012342332/secrets/swarm-tenant-u-bogdan-typesafe\" || resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-u-bogdan-typesafe/versions/\")"
    error_message = "the condition is the one secret and its versions, by project number"
  }

  assert {
    condition     = startswith(google_project_iam_member.typesafe_user_key_reader["eng/u-bogdan"].condition[0].description, "managed-by=swarm-terraform;")
    error_message = "every binding says managed-by=swarm-terraform"
  }
}

run "a_personal_id_must_be_u_dash" {
  command = plan

  module {
    source = "../../terraform/bootstrap"
  }

  variables {
    typesafe_user_key_readers = { eng = ["bogdan"] }
  }

  expect_failures = [var.typesafe_user_key_readers]
}
