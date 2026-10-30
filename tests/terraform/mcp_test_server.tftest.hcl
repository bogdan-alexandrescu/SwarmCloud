# The acceptance group's echo MCP server, dev only (lane MCP-ACCEPT, part of
# #1433; owner decision Q10): terraform/infra/mcp_test_server.tf.
#
# What is held here:
#
#   * OFF BY DEFAULT nothing exists: no service, no invoker, no account, no
#     actAs. No environment's tfvars sets the switch, so merging the lane
#     changes nothing until the owner turns it on in dev;
#   * "account" creates the account alone, so bootstrap can grant the deployer
#     admin on it before anything acts as it;
#   * the switch is refused outside dev (staging and prod never run a public
#     test endpoint);
#   * on, the service swarm-mcp-echo is ingress ALL, scales to zero, runs the
#     echo server from the swarm-verify image's promoted digest with
#     requests == limits (invariant 7), as its own account;
#   * NOTHING IS PUBLIC (owner decision 2026-10-18, MCP-ACCEPT-B): run.invoker
#     on THIS service is held by swarm-verify alone -- the identity the
#     acceptance suite runs as and mints its own ID token from -- and
#     mcp_test_server.tf names neither allUsers nor
#     allAuthenticatedUsers. Cloud Run's IAM check on that identity's ID token is
#     what admits a request; ingress stays ALL because the relay reaches the
#     service from the swarm NAT;
#   * that account holds no project role, and only the deployer may act as it;
#   * every labelable resource carries managed-by=swarm-terraform.
#
# The infra root's setup is github_hooks.tftest.hcl's.

mock_provider "google" {}

override_data {
  target = data.google_kms_crypto_key_versions.step_spec
  values = {
    versions = [
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 1
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
    ]
  }
}

override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  # Read by the dev run's assertions only (live.tftest.hcl does the same).
  tf_root = "../../terraform/infra"

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
  }

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"            = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-egress"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "swarm-typesafe-fwd"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-typesafe-fwd@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    "swarm-graph"           = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-graph@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    "swarm-github-hooks"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-github-hooks@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
  # The deployer that must act as the echo server's account.
  deployer_service_account = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
}

run "off_by_default_nothing_exists" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_service.mcp_test_server) == 0
      && length(google_cloud_run_v2_service_iam_member.mcp_test_server_invoker) == 0
      && length(google_service_account.mcp_test_server) == 0
      && length(google_service_account_iam_member.mcp_test_server_deployer_acts_as) == 0
    )
    error_message = "with mcp_test_server off (the default) no echo server, invoker, account or actAs exists, even in dev with a swarm-verify digest"
  }

  assert {
    condition = (
      output.mcp_test_server.created == false
      && output.mcp_test_server.url == ""
      && output.mcp_test_server.service_account == ""
    )
    error_message = "with the echo server off there is no URL to register"
  }

  # No tfvars file in the repository turns it on: merging creates nothing.
  assert {
    condition = alltrue([
      for f in fileset("${var.tf_root}/../environments", "**/*.tfvars") :
      !strcontains(file("${var.tf_root}/../environments/${f}"), "mcp_test_server")
    ])
    error_message = "no environment's tfvars sets mcp_test_server; the owner sets it in dev"
  }
}

run "outside_dev_the_switch_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    environment     = "prod"
    mcp_test_server = "on"
  }

  expect_failures = [var.mcp_test_server]
}

run "account_creates_the_account_alone" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    mcp_test_server = "account"
  }

  assert {
    condition = (
      length(google_service_account.mcp_test_server) == 1
      && google_service_account.mcp_test_server[0].account_id == "swarm-mcp-echo"
      && length(google_service_account_iam_member.mcp_test_server_deployer_acts_as) == 0
      && length(google_cloud_run_v2_service.mcp_test_server) == 0
      && length(google_cloud_run_v2_service_iam_member.mcp_test_server_invoker) == 0
      && output.mcp_test_server.created == false
      && output.mcp_test_server.service_account == "swarm-mcp-echo@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "step 1 creates swarm-mcp-echo and nothing that acts as it: bootstrap's admin grant must land before the deployer's actAs is asked for"
  }
}

run "on_in_dev_the_echo_server_admits_only_the_acceptance_identity_scales_to_zero_and_runs_as_an_account_with_nothing" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    mcp_test_server = "on"
  }

  assert {
    condition = (
      length(google_cloud_run_v2_service.mcp_test_server) == 1
      && google_cloud_run_v2_service.mcp_test_server[0].name == "swarm-mcp-echo"
      && google_cloud_run_v2_service.mcp_test_server[0].ingress == "INGRESS_TRAFFIC_ALL"
      && google_cloud_run_v2_service.mcp_test_server[0].template[0].containers[0].image == "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    )
    error_message = "the echo server is the Cloud Run service swarm-mcp-echo, ingress all, on the swarm-verify image's promoted digest"
  }

  # The image carries the server at this path (images/swarm-verify/Dockerfile).
  assert {
    condition = (
      jsonencode(google_cloud_run_v2_service.mcp_test_server[0].template[0].containers[0].command) == jsonencode(["python3"])
      && jsonencode(google_cloud_run_v2_service.mcp_test_server[0].template[0].containers[0].args) == jsonencode(["/swarm/tests/acceptance/mcp_echo_server/server.py"])
      && strcontains(file("${var.tf_root}/../../images/swarm-verify/Dockerfile"), "COPY tests/acceptance/mcp_echo_server/ /swarm/tests/acceptance/mcp_echo_server/")
      && fileexists("${var.tf_root}/../../tests/acceptance/mcp_echo_server/server.py")
    )
    error_message = "the service runs the echo server the swarm-verify image copies in, with python3"
  }

  assert {
    condition = (
      google_cloud_run_v2_service.mcp_test_server[0].template[0].scaling[0].min_instance_count == 0
      && google_cloud_run_v2_service.mcp_test_server[0].template[0].scaling[0].max_instance_count == 1
      && google_cloud_run_v2_service.mcp_test_server[0].template[0].containers[0].resources[0].cpu_idle == false
      && google_cloud_run_v2_service.mcp_test_server[0].template[0].containers[0].resources[0].startup_cpu_boost == false
    )
    error_message = "an idle echo server runs nothing (invariant 1, Q10's scale to zero), and its CPU is its limit and nothing more (invariant 7)"
  }

  # NOTHING PUBLIC: swarm-verify alone invokes THIS service, and
  # the file names no public member at all (security.yml's "no public Cloud
  # Run services" allows one only for the GitHub push receiver).
  assert {
    condition = (
      length(google_cloud_run_v2_service_iam_member.mcp_test_server_invoker) == 1
      && google_cloud_run_v2_service_iam_member.mcp_test_server_invoker[0].member == "serviceAccount:swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_run_v2_service_iam_member.mcp_test_server_invoker[0].role == "roles/run.invoker"
      && google_cloud_run_v2_service_iam_member.mcp_test_server_invoker[0].name == "swarm-mcp-echo"
      && output.mcp_test_server.invoker == "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "run.invoker on swarm-mcp-echo is held by swarm-verify alone, and the output names it"
  }

  assert {
    condition     = length(regexall("allUsers|allAuthenticatedUsers", file("${var.tf_root}/mcp_test_server.tf"))) == 0
    error_message = "mcp_test_server.tf names no public member: the owner decided on 2026-10-18 that the echo server is not public"
  }

  # The invoker's address is spelled from the id verify.tf reads swarm-verify
  # by (terraform/bootstrap creates it since DAYONE-BOOT-2), never typed: the
  # account and the invoker cannot drift apart.
  assert {
    condition = (
      data.google_service_account.verify.account_id == "swarm-verify"
      && strcontains(file("${var.tf_root}/mcp_test_server.tf"), "module.service_account_ids.verify_id")
    )
    error_message = "the invoker must be spelled from the id swarm-verify is read by (verify.tf), not typed"
  }

  # Its own account, and never one that can read anything.
  assert {
    condition = (
      google_service_account.mcp_test_server[0].account_id == "swarm-mcp-echo"
      && google_cloud_run_v2_service.mcp_test_server[0].template[0].service_account == "swarm-mcp-echo@saga-agents-staging.iam.gserviceaccount.com"
      && output.mcp_test_server.service_account == "swarm-mcp-echo@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "the echo server runs as its own account swarm-mcp-echo, not swarm-verify's (which reads Firestore) nor the default compute account"
  }

  # The account holds no role: the only IAM this file declares is the
  # swarm-verify's invoker on the service and the deployer's actAs on
  # the account.
  assert {
    condition = (
      length(regexall("resource \"google_[a-z0-9_]*iam_[a-z_]*\"", file("${var.tf_root}/mcp_test_server.tf"))) == 2
      && google_service_account_iam_member.mcp_test_server_deployer_acts_as[0].role == "roles/iam.serviceAccountUser"
      && google_service_account_iam_member.mcp_test_server_deployer_acts_as[0].member == "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "mcp_test_server.tf grants the echo server's account nothing; the deployer alone may act as it"
  }

  assert {
    condition = (
      google_cloud_run_v2_service.mcp_test_server[0].labels["managed-by"] == "swarm-terraform"
      && google_cloud_run_v2_service.mcp_test_server[0].template[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "every labelable resource carries managed-by=swarm-terraform"
  }

  # No secret, ever: the server holds nothing.
  assert {
    condition     = length(regexall("google_secret_manager|secret_key_ref|env \\{", file("${var.tf_root}/mcp_test_server.tf"))) == 0
    error_message = "mcp_test_server.tf declares no secret and no environment: the echo server holds nothing"
  }

  assert {
    condition = (
      output.mcp_test_server.created == true
      && output.mcp_test_server.service == "swarm-mcp-echo"
    )
    error_message = "the output names the service to register"
  }
}
