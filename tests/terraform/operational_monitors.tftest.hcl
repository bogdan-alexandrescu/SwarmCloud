# The four monitors the monitoring module lacked (alerting item of the
# scanner-gates/alerting epic): a front-door uptime check, a reconciler
# pass-duration alert, a NAT-drop alert and a browser-secret-grant alert.
#
# THE EMITTERS, each read from the code rather than assumed:
#
#   * reconciler pass duration -- reconciler/repair.py logs
#     "reconciliation pass complete" once per pass through
#     reconciler.logs.StructuredLogger, with the report's `duration_seconds`
#     (float seconds, PassReport.as_dict) at the TOP LEVEL of jsonPayload.
#     tests/unit/control_plane/test_reconciler_gke_namespaced.py holds the
#     emitter to the metric's filter and extractor.
#   * NAT drops -- Cloud NAT's own `router.googleapis.com/nat/
#     dropped_sent_packets_count`, written for the gateway terraform/modules/
#     network creates (`<name_prefix>-nat`). No log-based metric, so no wait.
#   * browser-secret grant -- swarm_api.browser_slots sets the secret's IAM
#     policy (set_iam_policy); Secret Manager writes the Admin Activity audit
#     entry SetIamPolicy, which cannot be turned off. The app logs no line of
#     its own on a grant.
#   * uptime -- /healthz on swarm-api, routed through the front door's IAP
#     backend (terraform/modules/frontend, the API path rule).
#
# example.com hostnames only: RFC 2606 reserves them, so no test names a
# real host.

mock_provider "google" {}

# The check's id is server-assigned, so unknown at plan; the policy's filter
# names it. A fixed id lets the filter be read.
override_resource {
  target          = google_monitoring_uptime_check_config.front_door
  override_during = plan
  values = {
    uptime_check_id = "swarm-dev-front-door-healthz-test"
  }
}

variables {
  project_id  = "example-project"
  environment = "dev"
  labels      = { "managed-by" = "swarm-terraform" }

  service_names                = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
  wake_subscription            = "swarm-scheduler-wake-sub"
  dead_letter_subscription     = "swarm-scheduler-wake-dlq-sub"
  safety_tick_job              = "swarm-scheduler-tick"
  front_door_hostname          = "swarm.example.com"
  extra_notification_channels  = ["projects/example-project/notificationChannels/1111111111"]
  urgent_notification_channels = ["projects/example-project/notificationChannels/2222222222"]
}

# ---------------------------------------------------------------------------
# Uptime: the API's health route, through the front door.
# ---------------------------------------------------------------------------
run "the_uptime_check_reads_healthz_through_the_front_door" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      length(google_monitoring_uptime_check_config.front_door) == 1
      && google_monitoring_uptime_check_config.front_door[0].project == "example-project"
      && google_monitoring_uptime_check_config.front_door[0].monitored_resource[0].type == "uptime_url"
      && google_monitoring_uptime_check_config.front_door[0].monitored_resource[0].labels["host"] == "swarm.example.com"
      && google_monitoring_uptime_check_config.front_door[0].monitored_resource[0].labels["project_id"] == "example-project"
    )
    error_message = "the uptime check targets the front door hostname the module is given, in the swarm project"
  }

  assert {
    condition = (
      google_monitoring_uptime_check_config.front_door[0].http_check[0].path == "/healthz"
      && google_monitoring_uptime_check_config.front_door[0].http_check[0].port == 443
      && google_monitoring_uptime_check_config.front_door[0].http_check[0].use_ssl == true
      && google_monitoring_uptime_check_config.front_door[0].http_check[0].validate_ssl == true
    )
    error_message = "the check is HTTPS on the API's /healthz route and fails on a bad certificate"
  }

  # IAP answers every unauthenticated request with a 302 to sign-in, whatever
  # the backend is doing; an uptime check follows no redirect. 302 is the
  # only healthy answer: a 2xx would mean IAP is OFF on the API's backend.
  # MUTATION: accept 2xx (or drop accepted_response_status_codes) and this fails.
  assert {
    condition = (
      length(google_monitoring_uptime_check_config.front_door[0].http_check[0].accepted_response_status_codes) == 1
      && google_monitoring_uptime_check_config.front_door[0].http_check[0].accepted_response_status_codes[0].status_value == 302
    )
    error_message = "behind IAP the only healthy answer is IAP's 302; a 2xx means the gate is off"
  }

  assert {
    condition = (
      google_monitoring_uptime_check_config.front_door[0].period == "60s"
      && google_monitoring_uptime_check_config.front_door[0].timeout == "10s"
      && google_monitoring_uptime_check_config.front_door[0].user_labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the uptime check runs every 60s with a 10s timeout and carries managed-by=swarm-terraform"
  }

  assert {
    condition = (
      length(google_monitoring_alert_policy.front_door_down) == 1
      && google_monitoring_alert_policy.front_door_down[0].display_name == "swarm-dev-front-door-down"
      && google_monitoring_alert_policy.front_door_down[0].severity == "ERROR"
      && google_monitoring_alert_policy.front_door_down[0].user_labels["managed-by"] == "swarm-terraform"
      && strcontains(google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].filter, "metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\"")
      && strcontains(google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].filter, "resource.type = \"uptime_url\"")
      && strcontains(google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].filter, "metric.labels.check_id = \"swarm-dev-front-door-healthz-test\"")
    )
    error_message = "the front-door policy reads its own check's check_passed series, at ERROR, labelled managed-by"
  }

  # More than one checker region failing for five minutes: one region's
  # network blip is not the front door going down.
  assert {
    condition = (
      google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].threshold_value == 1
      && google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].duration == "300s"
      && google_monitoring_alert_policy.front_door_down[0].conditions[0].condition_threshold[0].aggregations[0].cross_series_reducer == "REDUCE_COUNT_FALSE"
    )
    error_message = "the front door pages when more than one region fails for 300s"
  }

  assert {
    condition     = contains(output.alert_policy_names, "swarm-dev-front-door-down")
    error_message = "the policy is listed with the module's others"
  }
}

run "no_front_door_no_uptime_check" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    front_door_hostname = ""
  }

  assert {
    condition     = length(google_monitoring_uptime_check_config.front_door) == 0 && length(google_monitoring_alert_policy.front_door_down) == 0
    error_message = "with no front door (enable_frontend off) there is no host to check and nothing is created"
  }
}

run "the_front_door_hostname_is_a_bare_host" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    front_door_hostname = "https://swarm.example.com/healthz"
  }

  expect_failures = [var.front_door_hostname]
}

# ---------------------------------------------------------------------------
# Reconciler pass duration.
# ---------------------------------------------------------------------------
run "the_pass_duration_metric_reads_the_reconcilers_pass_line" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for f in [
        "jsonPayload.message=\"reconciliation pass complete\"",
        "jsonPayload.component=\"reconciler\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=(\"swarm-reconciler\")",
      ] : strcontains(google_logging_metric.reconciler_pass_duration.filter, f)
    ])
    error_message = "the metric must match the reconciler's `reconciliation pass complete` line, from swarm-reconciler only"
  }

  assert {
    condition = (
      google_logging_metric.reconciler_pass_duration.value_extractor == "EXTRACT(jsonPayload.duration_seconds)"
      && google_logging_metric.reconciler_pass_duration.metric_descriptor[0].metric_kind == "DELTA"
      && google_logging_metric.reconciler_pass_duration.metric_descriptor[0].value_type == "DISTRIBUTION"
      && google_logging_metric.reconciler_pass_duration.metric_descriptor[0].unit == "s"
    )
    error_message = "pass duration is a DELTA DISTRIBUTION in seconds, extracted from the line's top-level duration_seconds"
  }

  # The threshold must be a bucket bound, or the percentile interpolates
  # across it and the alert fires on passes that never reached it.
  assert {
    condition     = contains(google_logging_metric.reconciler_pass_duration.bucket_options[0].explicit_buckets[0].bounds, 60)
    error_message = "the default threshold (60s) is a bucket bound"
  }

  assert {
    condition     = contains(output.log_metric_names, google_logging_metric.reconciler_pass_duration.name) && output.log_metric_filters[google_logging_metric.reconciler_pass_duration.name] == google_logging_metric.reconciler_pass_duration.filter
    error_message = "the metric is listed with the module's other log metrics"
  }
}

run "a_pass_slower_than_its_tick_for_fifteen_minutes_warns" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      length(google_monitoring_alert_policy.reconciler_pass_slow) == 1
      && google_monitoring_alert_policy.reconciler_pass_slow[0].display_name == "swarm-dev-reconciler-pass-slow"
      && google_monitoring_alert_policy.reconciler_pass_slow[0].severity == "WARNING"
      && google_monitoring_alert_policy.reconciler_pass_slow[0].user_labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the pass-duration policy exists, at WARNING, labelled managed-by"
  }

  # MUTATION: change reconciler_pass_slow_seconds' default to 120 and this fails.
  assert {
    condition = (
      google_monitoring_alert_policy.reconciler_pass_slow[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.reconciler_pass_slow[0].conditions[0].condition_threshold[0].threshold_value == 60
      && google_monitoring_alert_policy.reconciler_pass_slow[0].conditions[0].condition_threshold[0].duration == "900s"
      && google_monitoring_alert_policy.reconciler_pass_slow[0].conditions[0].condition_threshold[0].aggregations[0].per_series_aligner == "ALIGN_PERCENTILE_99"
      && google_monitoring_alert_policy.reconciler_pass_slow[0].conditions[0].condition_threshold[0].aggregations[0].alignment_period == "300s"
    )
    error_message = "the policy fires when p99 pass duration stays above the one-minute tick (60s) for 900s"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.reconciler_pass_slow[0].conditions[0].condition_threshold[0].filter, "metric.type = \"logging.googleapis.com/user/swarm/reconciler-pass-duration\"")
    error_message = "the policy reads the pass-duration metric"
  }
}

# ---------------------------------------------------------------------------
# NAT drops.
# ---------------------------------------------------------------------------
run "nat_drops_sustained_for_fifteen_minutes_page" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      length(google_monitoring_alert_policy.nat_drops) == 1
      && google_monitoring_alert_policy.nat_drops[0].display_name == "swarm-dev-nat-dropping-packets"
      && google_monitoring_alert_policy.nat_drops[0].severity == "ERROR"
      && google_monitoring_alert_policy.nat_drops[0].user_labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the NAT-drop policy exists, at ERROR, labelled managed-by"
  }

  # The swarm's own gateway in its own region: the project is shared, and a
  # policy over every nat_gateway would page on another team's.
  # MUTATION: drop the gateway_name clause and this fails.
  assert {
    condition = alltrue([
      for f in [
        "metric.type = \"router.googleapis.com/nat/dropped_sent_packets_count\"",
        "resource.type = \"nat_gateway\"",
        "resource.labels.gateway_name = \"swarm-nat\"",
        "resource.labels.region = \"us-central1\"",
      ] : strcontains(google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].filter, f)
    ])
    error_message = "the NAT policy reads Cloud NAT's dropped-packet count for swarm-nat in the swarm's region only"
  }

  # A single 27 ms burst (docs/incidents/2026-10-09-egress-open-delay.md) is
  # dynamic port allocation doubling, not exhaustion; drops in every 5-minute
  # window for 15 minutes are.
  assert {
    condition = (
      google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].comparison == "COMPARISON_GT"
      && google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].threshold_value == 0
      && google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].duration == "900s"
      && google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].aggregations[0].alignment_period == "300s"
      && contains(google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].aggregations[0].group_by_fields, "metric.labels.reason")
    )
    error_message = "the NAT policy fires on drops in every 300s window for 900s, grouped by drop reason"
  }
}

run "a_nat_gateway_name_can_be_given" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    name_prefix      = "swarmb"
    nat_gateway_name = "swarmb-egress-nat"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.nat_drops[0].conditions[0].condition_threshold[0].filter, "resource.labels.gateway_name = \"swarmb-egress-nat\"")
    error_message = "nat_gateway_name overrides the <name_prefix>-nat default"
  }
}

# ---------------------------------------------------------------------------
# Browser-secret grant.
# ---------------------------------------------------------------------------
run "a_browser_secret_grant_outside_swarm_apis_slot_pages" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = (
      length(google_monitoring_alert_policy.browser_secret_grant) == 1
      && google_monitoring_alert_policy.browser_secret_grant[0].display_name == "swarm-dev-session-secret-grant-outside-slot"
      && google_monitoring_alert_policy.browser_secret_grant[0].severity == "CRITICAL"
      && google_monitoring_alert_policy.browser_secret_grant[0].user_labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the browser-secret-grant policy exists, at CRITICAL (an isolation tripwire), labelled managed-by"
  }

  assert {
    condition = alltrue([
      for f in [
        "logName=\"projects/example-project/logs/cloudaudit.googleapis.com%2Factivity\"",
        "protoPayload.serviceName=\"secretmanager.googleapis.com\"",
        "protoPayload.methodName=~\"(?i)setiampolicy$\"",
        "protoPayload.resourceName=~\"/secrets/swarm-tenant-[a-z0-9-]+-browser-(wf|park|login)-[^/]+$\"",
        # Anyone but swarm-api, any member but a personal worker, any role but the slot's two.
        "NOT protoPayload.authenticationInfo.principalEmail=\"swarm-api@example-project.iam.gserviceaccount.com\"",
        "protoPayload.serviceData.policyDelta.bindingDeltas.member!~\"^serviceAccount:swarm-agent-worker-u-[a-z0-9-]+@example-project[.]iam[.]gserviceaccount[.]com$\"",
        "protoPayload.serviceData.policyDelta.bindingDeltas.role!~\"^roles/secretmanager[.](secretAccessor|secretVersionAdder)$\"",
      ] : strcontains(google_monitoring_alert_policy.browser_secret_grant[0].conditions[0].condition_matched_log[0].filter, f)
    ])
    error_message = "the policy matches a SetIamPolicy on a browser secret by anyone but swarm-api, or to any member or role the slot never grants"
  }

  assert {
    condition = (
      google_monitoring_alert_policy.browser_secret_grant[0].alert_strategy[0].notification_rate_limit[0].period == "300s"
      && google_monitoring_alert_policy.browser_secret_grant[0].alert_strategy[0].auto_close == "1800s"
    )
    error_message = "a log-match policy rate-limits its notifications (300s) and closes after 1800s"
  }
}

# ---------------------------------------------------------------------------
# All four follow create_alerts; channels follow severity.
# ---------------------------------------------------------------------------
run "the_four_follow_create_alerts" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    create_alerts = false
  }

  assert {
    condition = (
      length(google_monitoring_uptime_check_config.front_door) == 0
      && length(google_monitoring_alert_policy.front_door_down) == 0
      && length(google_monitoring_alert_policy.reconciler_pass_slow) == 0
      && length(google_monitoring_alert_policy.nat_drops) == 0
      && length(google_monitoring_alert_policy.browser_secret_grant) == 0
    )
    error_message = "with create_alerts off none of the four monitors is created"
  }
}

run "the_four_route_by_severity" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  assert {
    condition = alltrue([
      for p in [
        google_monitoring_alert_policy.front_door_down[0],
        google_monitoring_alert_policy.nat_drops[0],
        google_monitoring_alert_policy.browser_secret_grant[0],
        ] : toset(p.notification_channels) == toset([
          "projects/example-project/notificationChannels/1111111111",
          "projects/example-project/notificationChannels/2222222222",
      ])
    ])
    error_message = "the CRITICAL and ERROR monitors reach the ordinary and the urgent channels"
  }

  assert {
    condition     = toset(google_monitoring_alert_policy.reconciler_pass_slow[0].notification_channels) == toset(["projects/example-project/notificationChannels/1111111111"])
    error_message = "the WARNING pass-duration monitor reaches the ordinary channels only"
  }
}
