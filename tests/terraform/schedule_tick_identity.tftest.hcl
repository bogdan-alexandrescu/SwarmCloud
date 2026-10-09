# The schedule tick's identity, swarm-schedule-tick (docs/schedules.md §2.1,
# owner decision SD10, 2026-10-08; lane S13).
#
# The tick reads due schedules across every tenant, personal ones included, so
# it gets an account of its own rather than widening the rollup sweeper's. What
# keeps that account narrow is that it holds exactly one grant -- run.invoker
# on swarm-api, the pattern of rollup_sweeper_invokes_api -- and swarm-api
# admits it to one route (auth.SCHEDULE_TICK_ROUTES, lane S2). This file holds
# the Terraform half:
#
#   1. the account exists, under the id terraform/bootstrap grants the
#      deployer on (modules/service_account_ids), and carries the
#      managed-by=swarm-terraform marker;
#   2. it holds run.invoker on swarm-api, and the root renders its address
#      into swarm-api as SCHEDULE_TICK_USERS;
#   3. it holds NO OTHER GRANT anywhere in terraform/: every IAM resource in
#      terraform/infra, terraform/modules and terraform/bootstrap is read as
#      text, and the only one naming the account is schedule_tick_invokes_api;
#      and every live line that names the account at all is one this lane
#      wrote (so a grant made through a members map or a policy document,
#      which no IAM resource body would name, fails too).
#
# The scans in 3 run the same expression twice more as controls: over the
# rollup sweeper, which really does hold a grant, and over a synthetic file
# holding a second grant to the tick. Each must find what is there, so the
# check is shown to fail when its defect exists, not to pass on empty lists.
#
# A text scan with project_iam_inventory's trade: it matches the forms
# `terraform fmt` writes (`resource "<type>" "<name>" {` at the start of a line,
# `}` closing it), which CI checks over terraform/.

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
# 1. The account
# ---------------------------------------------------------------------------

# terraform/bootstrap/deployer_service_accounts.tf grants the release deployer
# serviceAccountAdmin on each id in infra_managed, and on nothing else. An id
# missing here is an account whose IAM the release can never set.
run "the_tick_account_is_on_the_list_bootstrap_grants_the_deployer_on" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids = ["eng"]
  }

  assert {
    condition     = output.schedule_tick_id == "swarm-schedule-tick"
    error_message = "the schedule tick's account id is swarm-schedule-tick (docs/schedules.md SD10)"
  }

  assert {
    condition     = contains(output.infra_managed, output.schedule_tick_id)
    error_message = "swarm-schedule-tick is not in infra_managed, so bootstrap never grants the deployer on it and the release cannot set its IAM"
  }

  # Its own account: not another name for the sweeper or the scheduler tick.
  assert {
    condition     = output.schedule_tick_id != output.rollup_sweeper_id && output.schedule_tick_id != output.tick_id
    error_message = "the schedule tick must have its own account, not the rollup sweeper's or swarm-tick's (SD10)"
  }
}

run "the_scheduler_module_creates_the_tick_account" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    wake_topic_name         = "swarm-scheduler-wake"
    scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
    reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
    quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
    tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
    labels                  = { "managed-by" = "swarm-terraform" }
  }

  assert {
    condition     = google_service_account.schedule_tick.account_id == "swarm-schedule-tick" && google_service_account.schedule_tick.project == "saga-agents-staging"
    error_message = "the scheduler module must create swarm-schedule-tick in the platform's project"
  }

  # A service account has no labels; the marker lives at the start of its
  # description, which is what make destroy's guard reads.
  assert {
    condition     = startswith(google_service_account.schedule_tick.description, "managed-by=swarm-terraform;")
    error_message = "swarm-schedule-tick must carry managed-by=swarm-terraform, or make destroy refuses to touch it"
  }

  assert {
    condition     = output.schedule_tick_email == "swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the root renders this address as SCHEDULE_TICK_USERS and grants it run.invoker; it must be the account the module creates"
  }

  assert {
    condition     = output.schedule_tick_email != output.rollup_sweeper_email
    error_message = "the schedule tick must not present the rollup sweeper's identity (SD10)"
  }
}

# ---------------------------------------------------------------------------
# 2. Its one grant, and its address in swarm-api's environment
# ---------------------------------------------------------------------------

# Planned with prod's shape of api_invokers -- tenant groups only, no allUsers
# -- because that is where a missing grant is a 403 on every tick.
run "the_tick_can_invoke_swarm_api_and_swarm_api_knows_its_address" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    api_invokers             = ["group:eng@saga.xyz"]
    deployer_service_account = "swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
    tenants = {
      eng = { kind = "group", principal = "eng@saga.xyz", providers = ["anthropic"] }
    }
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.schedule_tick_invokes_api.name == "swarm-api"
    error_message = "the tick's invoker grant must be on swarm-api, the service its job calls, and on no other service"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.schedule_tick_invokes_api.role == "roles/run.invoker"
    error_message = "the tick needs run.invoker on swarm-api and nothing broader"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.schedule_tick_invokes_api.member == "serviceAccount:swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the invoker grant must name swarm-schedule-tick"
  }

  assert {
    condition     = local.service_env["swarm-api"]["SCHEDULE_TICK_USERS"] == "swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "swarm-api must be told the tick's address as SCHEDULE_TICK_USERS, or auth.SCHEDULE_TICK_ROUTES admits nobody"
  }

  # The sweeper's admission is not widened to the tick, nor the tick's to the
  # sweeper.
  assert {
    condition     = local.service_env["swarm-api"]["ROLLUP_SWEEPER_USERS"] == "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "ROLLUP_SWEEPER_USERS must still name the sweeper alone"
  }

  # Only swarm-api is told; no other service admits the tick.
  assert {
    condition     = length([for svc, env in local.service_env : svc if contains(keys(env), "SCHEDULE_TICK_USERS")]) == 1
    error_message = "SCHEDULE_TICK_USERS is rendered into a service other than swarm-api"
  }

  # The deployer may act as it -- a grant ON the account, to the deployer, which
  # creating a job that mints its OIDC token (lane S4) needs. Not a grant the
  # account holds.
  assert {
    condition = (
      google_service_account_iam_member.deployer_acts_as["swarm-schedule-tick"].service_account_id == "projects/saga-agents-staging/serviceAccounts/swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"
      && google_service_account_iam_member.deployer_acts_as["swarm-schedule-tick"].role == "roles/iam.serviceAccountUser"
      && google_service_account_iam_member.deployer_acts_as["swarm-schedule-tick"].member == "serviceAccount:swarm-tf-deployer@saga-agents-staging.iam.gserviceaccount.com"
    )
    error_message = "the deployer must hold actAs on swarm-schedule-tick, and only the deployer"
  }

  assert {
    condition     = !contains(var.api_invokers, "allUsers")
    error_message = "this run must plan without allUsers, or it proves nothing about prod"
  }
}

# ---------------------------------------------------------------------------
# 3. No other grant, anywhere in terraform/
# ---------------------------------------------------------------------------

# Plans the smallest module only so the assertions have somewhere to run; what
# they read is every .tf file under terraform/infra, terraform/modules and
# terraform/bootstrap, as text, from tests/terraform (the directory
# `make tf-test` and CI run from).
#
# TICK names the account every way the code can: its id, its address, the
# module output and local carrying it, and the resource. An IAM resource is any
# google_*_iam_member, _iam_binding or _iam_policy -- project, service,
# bucket, secret, topic, service account alike.
run "the_tick_holds_no_grant_but_run_invoker_on_swarm_api" {
  command = plan

  module {
    source = "../../terraform/modules/service_account_ids"
  }

  variables {
    tenant_ids = []

    tf_root  = "../../terraform"
    tick_re  = "schedule_tick_email|schedule_tick_id|swarm-schedule-tick|google_service_account\\.schedule_tick\\b"
    grant_re = "(?ms)^resource \"(google_[a-z0-9_]*_iam_(?:member|binding|policy))\" \"([^\"]+)\" \\{\\n(.*?)^\\}"
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

  # THE CHECK. Every IAM resource in terraform/ whose body names the tick.
  assert {
    condition = sort(flatten([
      for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf"), fileset(var.tf_root, "bootstrap/**/*.tf")) : [
        for m in regexall(var.grant_re, file("${var.tf_root}/${f}")) : "${f} ${m[0]}.${m[1]}"
        if length(regexall(var.tick_re, m[2])) > 0
      ]
      ])) == tolist([
      "infra/main.tf google_cloud_run_v2_service_iam_member.schedule_tick_invokes_api",
    ])
    error_message = "swarm-schedule-tick holds an IAM grant besides run.invoker on swarm-api (schedule_tick_invokes_api), or that grant is gone. The tick is admitted to one route; a second grant widens it (docs/schedules.md SD10)"
  }

  # THE CHECK, through indirection. A grant made by putting the address into a
  # members map, a for_each or a policy document names the tick on a line no
  # IAM resource body holds. So every live (non-comment) line in terraform/
  # that names the tick must be one of these, whitespace collapsed. An OIDC
  # token line in modules/scheduler (lane S4's job) is a token the account
  # mints, not a grant it holds, so it is allowed in that module.
  assert {
    condition = length([
      for l in flatten([
        for f in setunion(fileset(var.tf_root, "infra/**/*.tf"), fileset(var.tf_root, "modules/**/*.tf"), fileset(var.tf_root, "bootstrap/**/*.tf")) : [
          for line in split("\n", file("${var.tf_root}/${f}")) : "${f}: ${replace(trimspace(line), "/\\s+/", " ")}"
          if length(regexall(var.tick_re, line)) > 0 && !startswith(trimspace(line), "#")
        ]
      ]) : l
      if !contains([
        "modules/service_account_ids/main.tf: schedule_tick_id = \"swarm-schedule-tick\"",
        "modules/service_account_ids/main.tf: [local.tick_id, local.verify_id, local.rollup_sweeper_id, local.schedule_tick_id],",
        "modules/service_account_ids/outputs.tf: output \"schedule_tick_id\" {",
        "modules/service_account_ids/outputs.tf: value = local.schedule_tick_id",
        "modules/scheduler/main.tf: account_id = module.service_account_ids.schedule_tick_id",
        "modules/scheduler/main.tf: schedule_tick_email = \"$${google_service_account.schedule_tick.account_id}@$${var.project_id}.iam.gserviceaccount.com\"",
        "modules/scheduler/outputs.tf: output \"schedule_tick_email\" {",
        "modules/scheduler/outputs.tf: value = local.schedule_tick_email",
        "infra/main.tf: member = \"serviceAccount:$${module.scheduler.schedule_tick_email}\"",
        "infra/locals.tf: SCHEDULE_TICK_USERS = module.scheduler.schedule_tick_email",
        "infra/deployer.tf: \"swarm-schedule-tick\" = module.scheduler.schedule_tick_email",
      ], l) && length(regexall("^modules/scheduler/[^:]+: service_account_email = local\\.schedule_tick_email$", l)) == 0
    ]) == 0
    error_message = "a line in terraform/ names swarm-schedule-tick outside the places lane S13 put it: the account's id, its creation, its output, its one invoker grant, SCHEDULE_TICK_USERS and the deployer's actAs. A new reference may be a grant through a map; read it, and if it is not one, add it here"
  }

  # THE CONTROL: the same scan, over an identity that really does hold a grant,
  # finds it. If the regex stopped matching fmt'd resources, the check above
  # would see no grant at all and still fail on the missing invoker -- this
  # shows the scan reports a grant it is given, not only the expected one.
  assert {
    condition = contains(flatten([
      for f in fileset(var.tf_root, "infra/**/*.tf") : [
        for m in regexall(var.grant_re, file("${var.tf_root}/${f}")) : "${f} ${m[0]}.${m[1]}"
        if length(regexall("rollup_sweeper_email", m[2])) > 0
      ]
    ]), "infra/main.tf google_cloud_run_v2_service_iam_member.rollup_sweeper_invokes_api")
    error_message = "the grant scan does not find rollup_sweeper_invokes_api, a grant that exists; it has stopped matching terraform fmt's resource blocks, and the check above proves nothing"
  }

  # THE CONTROL that the check can fail: a file with the invoker grant and a
  # second, project-level grant to the tick. The same expression reports both,
  # so a real second grant would make the list above longer than one and fail.
  assert {
    condition = sort([
      for m in regexall(var.grant_re, join("\n", [
        "resource \"google_cloud_run_v2_service_iam_member\" \"schedule_tick_invokes_api\" {",
        "  role   = \"roles/run.invoker\"",
        "  member = \"serviceAccount:$${module.scheduler.schedule_tick_email}\"",
        "}",
        "",
        "resource \"google_project_iam_member\" \"tick_reads_firestore\" {",
        "  role   = \"roles/datastore.viewer\"",
        "  member = \"serviceAccount:swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com\"",
        "}",
        "",
        "resource \"google_project_iam_member\" \"someone_else\" {",
        "  member = \"serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com\"",
        "}",
        "",
      ])) : "${m[0]}.${m[1]}" if length(regexall(var.tick_re, m[2])) > 0
      ]) == tolist([
      "google_cloud_run_v2_service_iam_member.schedule_tick_invokes_api",
      "google_project_iam_member.tick_reads_firestore",
    ])
    error_message = "the grant scan does not report a second grant to the tick in a file built to hold one; the check above could not fail"
  }
}
