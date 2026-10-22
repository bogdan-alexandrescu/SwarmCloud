# outside_personal_workers lets the workspace job bind swarm-api on a person's
# QA secret, and nothing wider (lane VQ3-DEPLOYER-QA, part of #1933;
# docs/design/visual-qa-any-app.md §2.1, §7 V-1/V-2).
#
# BGD, 2026-10-22: the deployer job creates a PERSONAL workspace's empty
# `swarm-tenant-u-<id>-qa` and grants swarm-api secretVersionAdder and
# secretAccessor on it (register-tenant.sh ws_a6, guard rule C6). Before this,
# outside_personal_workers fired on every deployer setIamPolicy whose member
# was not a personal worker, so every create would page. The exemption is
# exactly: member serviceAccount:swarm-api@<project>, role one of those two,
# on a resource name ending /secrets/swarm-tenant-u-<id>-qa. A worker -- or
# anyone else -- on that secret still fires, and so does swarm-api on any
# other secret.
#
# Cloud Logging's filter language is not evaluated here; its regexes are RE2,
# as Terraform's regex() is, so the three patterns are read back out of the
# rendered filter and run against names here. A rename or a widening of
# either side fails one of these.

mock_provider "google" {}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  labels      = { "managed-by" = "swarm-terraform" }

  service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
  wake_subscription        = "swarm-scheduler-wake-sub"
  dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
  safety_tick_job          = "swarm-scheduler-tick"
}

run "the_qa_secret_exemption_is_swarm_api_on_a_persons_qa_secret_only" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  # The two clauses, as rendered: the member-not-a-worker clause skips the QA
  # secret, and the QA secret's own clause fires on any member but swarm-api
  # or any role but V-1/V-2.
  assert {
    # MUTATION: drop the NOT ...-qa$ from the worker clause and a worker on the
    # QA secret is let through by it; drop the QA clause and nothing watches it.
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter,
      "(resource.type!=\"service_account\" AND protoPayload.methodName=~\"(?i)setiam(policy|permissions)$\" AND NOT protoPayload.resourceName=~\"/secrets/swarm-tenant-u-[a-z0-9]([a-z0-9-]*[a-z0-9])?-qa$\" AND protoPayload.serviceData.policyDelta.bindingDeltas.member!~\"^serviceAccount:swarm-agent-worker-u-[a-z0-9-]+@saga-agents-staging[.]iam[.]gserviceaccount[.]com$\")"),
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter,
      "(protoPayload.methodName=~\"(?i)setiam(policy|permissions)$\" AND protoPayload.resourceName=~\"/secrets/swarm-tenant-u-[a-z0-9]([a-z0-9-]*[a-z0-9])?-qa$\" AND (protoPayload.serviceData.policyDelta.bindingDeltas.member!~\"^serviceAccount:swarm-api@saga-agents-staging[.]iam[.]gserviceaccount[.]com$\" OR protoPayload.serviceData.policyDelta.bindingDeltas.role!~\"^roles/secretmanager[.](secretVersionAdder|secretAccessor)$\"))"),
      # Any removal still pages, the QA secret's included.
      strcontains(google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter,
      "(protoPayload.serviceData.policyDelta.bindingDeltas.action=\"REMOVE\")"),
    ])
    error_message = "outside_personal_workers must skip a person's QA secret in the worker clause and watch it in its own: any member but swarm-api, or any role but secretVersionAdder/secretAccessor, fires"
  }

  # swarm-api is named in exactly one place: the exemption is not repeated
  # somewhere wider.
  assert {
    # MUTATION: exempt swarm-api in the worker clause too (on every resource) and this fails.
    condition     = length(regexall("swarm-api@", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)) == 1
    error_message = "swarm-api may be exempted only on a person's QA secret"
  }

  # The resource pattern: a person's QA secret, and no other name.
  assert {
    # MUTATION: widen the pattern to /secrets/swarm-tenant-.*-qa$ and the group secret matches.
    condition = alltrue([
      for name in [
        "projects/209012342332/secrets/swarm-tenant-u-alice-qa",
        "projects/209012342332/secrets/swarm-tenant-u-or-9a0792-qa",
      ] : can(regex(regex("AND protoPayload[.]resourceName=~\"([^\"]+)\" AND [(]", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)[0], name))
    ])
    error_message = "the QA clause must match a personal tenant's swarm-tenant-u-<id>-qa"
  }

  assert {
    # MUTATION: drop the anchors or the u- and one of these matches.
    condition = alltrue([
      for name in [
        "projects/209012342332/secrets/swarm-tenant-eng-qa",
        "projects/209012342332/secrets/swarm-tenant-u-alice-git-u-0123456789abcdef",
        "projects/209012342332/secrets/swarm-tenant-u-alice-git-u-0123456789abcdef-refresh",
        "projects/209012342332/secrets/swarm-tenant-u-alice-qa-refresh",
        "projects/209012342332/secrets/swarm-tenant-u-alice-qa/versions/1",
        "projects/209012342332/secrets/not-swarm-tenant-u-alice-qa",
        "projects/209012342332/secrets/swarm-tenant-u--qa",
        "projects/209012342332/secrets/swarm-tenant-u-alice-anthropic",
        "projects/_/buckets/swarm-artifacts-saga-agents-staging",
        "projects/saga-agents-staging",
      ] : !can(regex(regex("AND protoPayload[.]resourceName=~\"([^\"]+)\" AND [(]", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)[0], name))
    ])
    error_message = "the QA clause must match no other secret (a group's QA secret, a forge slot or its twin, a provider key) and no other resource"
  }

  # The member: swarm-api of this project, exactly. Not matching it is what
  # fires (member!~), so every other member pages -- the control is the
  # worker of the same tenant, on the same secret.
  assert {
    condition     = can(regex(regex("bindingDeltas[.]member!~\"([^\"]+)\" OR ", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)[0], "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"))
    error_message = "swarm-api's own binding on a person's QA secret must not page (V-1, V-2)"
  }

  assert {
    # MUTATION: make the member pattern the personal-worker one, or .*, and the worker stops paging.
    condition = alltrue([
      for member in [
        "serviceAccount:swarm-agent-worker-u-alice@saga-agents-staging.iam.gserviceaccount.com",
        "serviceAccount:swarm-api@other-project.iam.gserviceaccount.com",
        "serviceAccount:swarm-api@saga-agents-stagingXiamXgserviceaccountXcom",
        "serviceAccount:swarm-api-2@saga-agents-staging.iam.gserviceaccount.com",
        "serviceAccount:not-swarm-api@saga-agents-staging.iam.gserviceaccount.com",
        "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com",
        "user:swarm-api@saga-agents-staging.iam.gserviceaccount.com",
        "deleted:serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com?uid=1",
        "allUsers",
      ] : !can(regex(regex("bindingDeltas[.]member!~\"([^\"]+)\" OR ", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)[0], member))
    ])
    error_message = "any member on a person's QA secret but swarm-api -- the tenant's own worker first -- must page"
  }

  # The role: V-1 or V-2, exactly.
  assert {
    # MUTATION: make the role pattern roles/secretmanager[.].* and admin stops paging.
    condition = alltrue(concat(
      [for role in ["roles/secretmanager.secretVersionAdder", "roles/secretmanager.secretAccessor"] :
        can(regex(regex("bindingDeltas[.]role!~\"([^\"]+)\"", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)[0], role))
      ],
      [for role in ["roles/secretmanager.admin", "roles/secretmanager.secretVersionManager", "roles/secretmanager.secretAccessorX", "roles/owner", "roles/iam.serviceAccountTokenCreator"] :
        !can(regex(regex("bindingDeltas[.]role!~\"([^\"]+)\"", google_monitoring_alert_policy.workspace["outside_personal_workers"].conditions[0].condition_matched_log[0].filter)[0], role))
      ],
    ))
    error_message = "swarm-api may hold secretVersionAdder and secretAccessor on a person's QA secret and nothing else; any other role pages"
  }
}
