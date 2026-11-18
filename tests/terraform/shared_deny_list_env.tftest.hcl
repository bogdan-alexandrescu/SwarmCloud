# swarm-api's never-touch twin (docs/design/generic-repository.md section 2.4).
#
# The shared project's other-team resources live in the deployment's private
# tfvars, never in this repository. Terraform renders them on swarm-api as
# SWARM_SHARED_DENY_LIST (a JSON array) and SWARM_SHARED_DENY_LIST_SET = "1"
# only when the deployment wrote a list, `[]` included. With no list written
# (the variable left null) the marker is "0" and swarm-api refuses an
# infrastructure issue run: a tfvars file that forgot the entry must fail
# closed, never tell swarm-api the project shares nothing.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "example-project"
  environment = "dev"

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "no_list_written_renders_no_marker" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SHARED_DENY_LIST_SET == "0"
    error_message = "with no list written, SWARM_SHARED_DENY_LIST_SET is \"0\", so swarm-api fails closed instead of reading an empty list"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SHARED_DENY_LIST == ""
    error_message = "with no list written, SWARM_SHARED_DENY_LIST is empty, never \"[]\""
  }
}

run "an_empty_list_is_rendered_as_an_empty_array_and_set" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    shared_deny_list = []
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SHARED_DENY_LIST == "[]"
    error_message = "a written empty list reaches swarm-api as SWARM_SHARED_DENY_LIST = \"[]\", never [\"\"]"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SHARED_DENY_LIST_SET == "1"
    error_message = "a written list, empty or not, carries SWARM_SHARED_DENY_LIST_SET = \"1\""
  }
}

run "the_listed_resources_reach_swarm_api_as_json" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    shared_deny_list = ["peer-cluster", "peer-vpc", "peer-subnet", "peer-bucket", "peer-runner@example-shared-project.iam.gserviceaccount.com"]
  }

  assert {
    condition     = jsondecode(local.service_env["swarm-api"].SWARM_SHARED_DENY_LIST) == ["peer-cluster", "peer-vpc", "peer-subnet", "peer-bucket", "peer-runner@example-shared-project.iam.gserviceaccount.com"]
    error_message = "swarm-api's SWARM_SHARED_DENY_LIST is the tfvars list, in order, as a JSON array"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_SHARED_DENY_LIST_SET == "1"
    error_message = "SWARM_SHARED_DENY_LIST_SET = \"1\" travels with the list"
  }
}

run "an_empty_entry_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    shared_deny_list = ["peer-cluster", " "]
  }

  expect_failures = [var.shared_deny_list]
}
