# The schedule tick and what it rests on (docs/schedules.md §2.1, §2.10,
# §4.9; lane S4).
#
#   1. ONE job, google_cloud_scheduler_job.schedule_tick: every minute, no
#      retry, POSTing swarm-api's tick route with no tenant, as lane S13's
#      account and never the rollup sweeper or the platform tick. Removing the
#      job fails every assertion in the first run: each one reads it.
#   2. The three composite indexes the row names, the tick's own first.
#   3. The TTL policies on schedule_firings.expire_at and
#      schedule_ticks.expire_at.
#   4. The schedule_auto_paused and schedule_needs_owner log metrics, keyed on
#      swarm-api's own lines, and the one alert over both.
#
# The account itself and its one grant are schedule_tick_identity.tftest.hcl's.

mock_provider "google" {}

# ---------------------------------------------------------------------------
# 1. The job
# ---------------------------------------------------------------------------

run "the_schedule_tick_is_one_job_every_minute_as_its_own_account" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    project_id              = "saga-agents-staging"
    wake_topic_name         = "swarm-scheduler-wake"
    scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
    reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
    quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
    tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
    labels                  = { "managed-by" = "swarm-terraform" }
    rollup_tenant_ids       = ["eng", "research"]
    api_endpoint            = "https://swarm-api-abcdef-uc.a.run.app/"
  }

  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.name == "swarm-schedule-tick"
    error_message = "the schedule tick is one job named swarm-schedule-tick (docs/schedules.md §2.1)"
  }

  # §2.1: one tick, not one per tenant. A per-tenant tick (the issue_run_advance
  # pattern) misses personal `u-` tenants, which var.tenants never lists.
  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.http_target[0].uri == "https://swarm-api-abcdef-uc.a.run.app/v1/admin/schedules/tick"
    error_message = "the job must call the route routes/schedule_tick.py serves, with no tenant_id: the tick reads every tenant's due schedules"
  }

  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.http_target[0].http_method == "POST"
    error_message = "the tick route is a POST"
  }

  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.schedule == "* * * * *"
    error_message = "the schedule tick runs every minute: a slot is a Unix minute and every deferral in §2.10 is to the next minute"
  }

  # §2.1: the next minute's tick is the retry.
  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.retry_config[0].retry_count == 0
    error_message = "the schedule tick's retry_count is 0: the next tick is its retry, and a retry only adds a second caller to the claim race"
  }

  # The route stops starting work at 240 s; the deadline leaves it room.
  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.attempt_deadline == "300s"
    error_message = "the tick's attempt deadline is 300s, above the route's 240s budget (schedulefire.TICK_BUDGET_SECONDS)"
  }

  # SD10: its own account. swarm-api admits that address, and only it, to the
  # tick route; the sweeper's and the platform tick's tokens are refused there.
  assert {
    condition = (
      google_cloud_scheduler_job.schedule_tick.http_target[0].oidc_token[0].service_account_email == "swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"
      && google_cloud_scheduler_job.schedule_tick.http_target[0].oidc_token[0].service_account_email == output.schedule_tick_email
      && google_cloud_scheduler_job.schedule_tick.http_target[0].oidc_token[0].service_account_email != output.rollup_sweeper_email
      && google_cloud_scheduler_job.schedule_tick.http_target[0].oidc_token[0].service_account_email != var.tick_service_account
    )
    error_message = "the schedule tick presents swarm-schedule-tick (SD10), never the rollup sweeper or the platform tick"
  }

  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.http_target[0].oidc_token[0].audience == "https://swarm-api-abcdef-uc.a.run.app"
    error_message = "the token is minted for the API's own URL, which is what swarm-api verifies"
  }

  # A Cloud Scheduler job has no labels; the destroy guard reads the marker
  # from its description.
  assert {
    condition     = startswith(google_cloud_scheduler_job.schedule_tick.description, "managed-by=swarm-terraform;")
    error_message = "the schedule tick carries managed-by=swarm-terraform in its description"
  }

  # §2.11: var.paused is one of the two kill switches.
  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.paused == false
    error_message = "the schedule tick runs unless var.paused says otherwise"
  }
}

run "the_schedule_tick_obeys_the_pause_switch" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  variables {
    project_id              = "saga-agents-staging"
    wake_topic_name         = "swarm-scheduler-wake"
    scheduler_push_endpoint = "https://swarm-scheduler-abcdef-uc.a.run.app"
    reconciler_endpoint     = "https://swarm-reconciler-abcdef-uc.a.run.app"
    quota_broker_endpoint   = "https://swarm-quota-broker-abcdef-uc.a.run.app"
    tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
    labels                  = { "managed-by" = "swarm-terraform" }
    api_endpoint            = "https://swarm-api-abcdef-uc.a.run.app"
    paused                  = true
  }

  assert {
    condition     = google_cloud_scheduler_job.schedule_tick.paused == true
    error_message = "Cloud Scheduler `paused` (var.paused) must stop the tick being called at all (docs/schedules.md §2.11)"
  }
}

# ---------------------------------------------------------------------------
# 2 and 3. The indexes and the TTL policies
# ---------------------------------------------------------------------------

run "the_tick_query_the_firings_and_the_inbox_have_their_indexes" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    project_id = "saga-agents-staging"
  }

  # §2.1 and §0 Check 4: `schedules where state == enabled and next_run_at <=
  # now order by next_run_at`. Without it the tick answers FAILED_PRECONDITION
  # and nothing fires.
  assert {
    condition = (
      google_firestore_index.this["schedules-state-next-run"].collection == "schedules"
      && google_firestore_index.this["schedules-state-next-run"].query_scope == "COLLECTION"
      && length(google_firestore_index.this["schedules-state-next-run"].fields) == 2
      && google_firestore_index.this["schedules-state-next-run"].fields[0].field_path == "state"
      && google_firestore_index.this["schedules-state-next-run"].fields[0].order == "ASCENDING"
      && google_firestore_index.this["schedules-state-next-run"].fields[1].field_path == "next_run_at"
      && google_firestore_index.this["schedules-state-next-run"].fields[1].order == "ASCENDING"
    )
    error_message = "the tick's query needs schedules (state ASC, next_run_at ASC), COLLECTION scope, and no tenant_id: it reads every tenant"
  }

  assert {
    condition = (
      google_firestore_index.this["schedule-firings-schedule-slot"].collection == "schedule_firings"
      && google_firestore_index.this["schedule-firings-schedule-slot"].query_scope == "COLLECTION"
      && length(google_firestore_index.this["schedule-firings-schedule-slot"].fields) == 2
      && google_firestore_index.this["schedule-firings-schedule-slot"].fields[0].field_path == "schedule_id"
      && google_firestore_index.this["schedule-firings-schedule-slot"].fields[1].field_path == "slot"
      && google_firestore_index.this["schedule-firings-schedule-slot"].fields[1].order == "DESCENDING"
    )
    error_message = "a schedule's firings, newest slot first, need schedule_firings (schedule_id, slot DESC)"
  }

  assert {
    condition = (
      google_firestore_index.this["approvals-tenant-state-requested"].collection == "approvals"
      && google_firestore_index.this["approvals-tenant-state-requested"].query_scope == "COLLECTION"
      && length(google_firestore_index.this["approvals-tenant-state-requested"].fields) == 3
      && google_firestore_index.this["approvals-tenant-state-requested"].fields[0].field_path == "tenant_id"
      && google_firestore_index.this["approvals-tenant-state-requested"].fields[1].field_path == "state"
      && google_firestore_index.this["approvals-tenant-state-requested"].fields[2].field_path == "requested_at"
    )
    error_message = "the approvals inbox needs approvals (tenant_id, state, requested_at), tenant_id leading (invariant 9)"
  }

  # §1.2 and §2.10: firings 90 days, tick reports 7, by TTL on expire_at --
  # the field swarm_api.schedulefire writes.
  assert {
    condition = (
      toset(keys(google_firestore_field.schedule_records_ttl)) == toset(["schedule_firings", "schedule_ticks"])
      && alltrue([
        for c, f in google_firestore_field.schedule_records_ttl :
        f.collection == c && f.field == "expire_at" && length(f.ttl_config) == 1
      ])
    )
    error_message = "schedule_firings.expire_at and schedule_ticks.expire_at each need a TTL policy, or both collections grow without bound"
  }

  # Not the audit-trail switch: turning off var.event_ttl_field must not make
  # firings keep for ever.
  assert {
    condition     = alltrue([for c, f in google_firestore_field.schedule_records_ttl : f.field != var.event_ttl_field])
    error_message = "the schedule TTLs name schedulefire's own field, not var.event_ttl_field"
  }
}

run "the_schedule_ttls_survive_the_audit_ttl_switch" {
  command = plan

  module {
    source = "../../terraform/modules/firestore"
  }

  variables {
    project_id      = "saga-agents-staging"
    event_ttl_field = ""
  }

  assert {
    condition     = length(google_firestore_field.schedule_records_ttl) == 2 && length(google_firestore_field.events_ttl) == 0
    error_message = "switching off the audit-trail TTLs must leave the schedule TTLs in place"
  }
}

# ---------------------------------------------------------------------------
# 4. The metrics and the alert
# ---------------------------------------------------------------------------

run "auto_pause_and_needs_owner_are_counted_and_alerted" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    project_id               = "saga-agents-staging"
    environment              = "dev"
    labels                   = { "managed-by" = "swarm-terraform" }
    service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
    wake_subscription        = "swarm-scheduler-wake-sub"
    dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
    safety_tick_job          = "swarm-scheduler-tick"
  }

  # schedulefire.Firer.pause: log.warning("schedule %s %s: %s", id, state, code).
  assert {
    condition = alltrue([
      for f in [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=(\"swarm-api\")",
        "jsonPayload.logger=\"swarm_api.schedulefire\"",
        "jsonPayload.message=~\"^schedule \\\\S+ auto_paused: \"",
      ] : strcontains(google_logging_metric.schedule_auto_paused.filter, f)
    ])
    error_message = "schedule_auto_paused must match schedulefire's `schedule <id> auto_paused: <code>` line, from swarm-api alone"
  }

  # The shared project: another team's services must not count.
  assert {
    condition = (
      !strcontains(google_logging_metric.schedule_auto_paused.filter, "swarm-scheduler")
      && !strcontains(google_logging_metric.schedule_needs_owner.filter, "swarm-scheduler")
    )
    error_message = "the schedule metrics read swarm-api's lines only"
  }

  assert {
    condition     = google_logging_metric.schedule_auto_paused.label_extractors["code"] == "REGEXP_EXTRACT(jsonPayload.message, \"auto_paused: (\\\\S+)\")"
    error_message = "the auto-pause count is labelled by its code, read from the line"
  }

  assert {
    condition = (
      strcontains(google_logging_metric.schedule_needs_owner.filter, "jsonPayload.message=\"schedule hold needs the owner\"")
      && google_logging_metric.schedule_needs_owner.label_extractors["tenant_id"] == "EXTRACT(jsonPayload.tenant_id)"
    )
    error_message = "schedule_needs_owner matches `schedule hold needs the owner` and reads tenant_id from its extra= field"
  }

  assert {
    condition = alltrue([
      for m in [google_logging_metric.schedule_auto_paused, google_logging_metric.schedule_needs_owner] :
      m.metric_descriptor[0].metric_kind == "DELTA" && m.metric_descriptor[0].value_type == "INT64"
    ])
    error_message = "a count of events is a DELTA INT64 series"
  }

  assert {
    condition = (
      google_logging_metric.schedule_auto_paused.name == "swarm/schedule-auto-paused"
      && google_logging_metric.schedule_needs_owner.name == "swarm/schedule-needs-owner"
      && contains(output.log_metric_names, "swarm/schedule-auto-paused")
      && contains(output.log_metric_names, "swarm/schedule-needs-owner")
      && output.log_metric_filters["swarm/schedule-auto-paused"] == google_logging_metric.schedule_auto_paused.filter
    )
    error_message = "both metrics are listed with the module's other log metrics"
  }

  # §4.9: "the same alert", on the existing channel.
  assert {
    condition = (
      length(google_monitoring_alert_policy.schedule_needs_attention) == 1
      && google_monitoring_alert_policy.schedule_needs_attention[0].combiner == "OR"
      && length(google_monitoring_alert_policy.schedule_needs_attention[0].conditions) == 2
    )
    error_message = "one policy carries both conditions, either of which fires it"
  }

  assert {
    condition = (
      strcontains(google_monitoring_alert_policy.schedule_needs_attention[0].conditions[0].condition_threshold[0].filter, "logging.googleapis.com/user/swarm/schedule-auto-paused")
      && strcontains(google_monitoring_alert_policy.schedule_needs_attention[0].conditions[1].condition_threshold[0].filter, "logging.googleapis.com/user/swarm/schedule-needs-owner")
      && alltrue([
        for c in google_monitoring_alert_policy.schedule_needs_attention[0].conditions :
        strcontains(c.condition_threshold[0].filter, "resource.type = \"cloud_run_revision\"")
        && c.condition_threshold[0].threshold_value == 0
        && c.condition_threshold[0].comparison == "COMPARISON_GT"
      ])
    )
    error_message = "each condition fires on one event of its metric"
  }

  assert {
    condition     = google_monitoring_alert_policy.schedule_needs_attention[0].user_labels["managed-by"] == "swarm-terraform"
    error_message = "the policy carries managed-by=swarm-terraform"
  }

  assert {
    condition     = contains(output.alert_policy_names, "swarm-dev-schedule-needs-attention")
    error_message = "the policy is listed with the module's other alert policies"
  }
}

run "the_schedule_alert_follows_create_alerts" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    project_id               = "saga-agents-staging"
    environment              = "dev"
    labels                   = { "managed-by" = "swarm-terraform" }
    service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
    wake_subscription        = "swarm-scheduler-wake-sub"
    dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
    safety_tick_job          = "swarm-scheduler-tick"
    create_alerts            = false
  }

  assert {
    condition     = length(google_monitoring_alert_policy.schedule_needs_attention) == 0 && !contains(output.alert_policy_names, "swarm-dev-schedule-needs-attention")
    error_message = "create_alerts = false leaves the metrics and creates no schedule policy"
  }
}
