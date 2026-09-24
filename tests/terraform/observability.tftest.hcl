# Metrics, alerts and the image registry.
#
# What is deliberately NOT alerted matters as much as what is: a large PARKED or
# QUEUED backlog costs nothing (CONTRACT.md invariant 1), so paging on it would
# train the team to ignore the pager on exactly the days the platform is working
# as designed.

mock_provider "google" {}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  labels      = { "managed-by" = "swarm-terraform" }
}

run "the_metrics_read_the_frozen_event_vocabulary" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
    wake_subscription        = "swarm-scheduler-wake-sub"
    dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
    safety_tick_job          = "swarm-scheduler-tick"
    alert_emails             = ["platform@saga.xyz"]
  }

  # Every key here is an EventType VALUE that agent_worker.control.emit() writes
  # today. That is the property worth asserting: a log-based metric whose filter
  # matches nothing fails nowhere -- not at apply, not in an alert, not on a
  # dashboard. It just stays at zero, which is indistinguishable from a healthy
  # platform. Scheduler-side events are absent on purpose: the scheduler's lines
  # are JSON (every service calls swarm_common.logging_setup.configure_logging)
  # but carry no `event_type` field -- they are printf-style messages -- so
  # there is nothing for a filter to match on yet.
  assert {
    condition = alltrue([
      for e in ["starting", "lease_released", "generation_fenced", "quota_exhausted", "dead_lettered", "checkpoint_completed", "parked"] :
      contains(keys(google_logging_metric.events), e)
    ])
    error_message = "every event the platform alerts or charts on needs a log-based metric keyed by the EventType value the worker emits"
  }

  # The literal line ControlPlane.emit() writes: message "event" plus the
  # EventType value in `event_type`. Matching on a field no component sets was
  # the whole defect.
  assert {
    condition = alltrue([
      for e, m in google_logging_metric.events :
      strcontains(m.filter, "jsonPayload.message=\"event\"") && strcontains(m.filter, "jsonPayload.event_type=\"${e}\"")
    ])
    error_message = "metrics must match the message and event_type fields the worker's structured logger actually emits"
  }

  # StructuredLogger nests the attempt's bound identity under `labels` and keeps
  # per-call fields at the top level, and Cloud Logging gives `labels` no
  # special treatment inside a JSON payload. An extractor pointed at the wrong
  # depth yields a metric that counts right and groups by nothing.
  assert {
    condition = alltrue([
      for e, m in google_logging_metric.events :
      m.label_extractors["tenant_id"] == "EXTRACT(jsonPayload.labels.tenant_id)"
    ])
    error_message = "tenant_id is a bound logger label, so it lives at jsonPayload.labels.tenant_id"
  }

  assert {
    condition     = google_logging_metric.events["quota_exhausted"].label_extractors["provider"] == "EXTRACT(jsonPayload.detail.provider)"
    error_message = "agent_worker.quota puts the provider in the event detail, not in the logger's bound labels"
  }

  # Parking is not an error -- it is the platform declining to burn compute on a
  # wait -- so it carries the ParkReason, which is what lets "you are out of
  # quota" be told apart from "you never registered a key".
  assert {
    condition     = google_logging_metric.events["parked"].label_extractors["park_reason"] == "EXTRACT(jsonPayload.detail.reason)"
    error_message = "a parked count with no reason cannot tell a quota park from a missing credential"
  }

  # No metric may match on a field nothing writes. swarm_event was exactly that.
  assert {
    condition = !anytrue([
      for name, f in output.log_metric_filters : strcontains(f, "swarm_event")
    ])
    error_message = "no filter may read jsonPayload.swarm_event: no component in this repository emits that field, so the metric would be permanently zero"
  }

  assert {
    condition     = google_logging_metric.peak_rss.metric_descriptor[0].value_type == "DISTRIBUTION"
    error_message = "peak RSS is what corrects the resource-class sizing from production rather than from arithmetic"
  }

  # LoggingMetricsExporter spreads its labels at the TOP level rather than into
  # the logger's bound `labels` object, so this one is deliberately unnested.
  assert {
    condition = (
      strcontains(google_logging_metric.peak_rss.filter, "jsonPayload.message=\"attempt resource usage\"") &&
      google_logging_metric.peak_rss.label_extractors["runner_profile"] == "EXTRACT(jsonPayload.runner_profile)"
    )
    error_message = "the peak-RSS metric must match the line agent_worker.metrics.LoggingMetricsExporter writes"
  }

  # requests == limits leaves no headroom, so the attempt that finished at 97%
  # of its limit is the one that gets OOM-killed next week.
  assert {
    condition     = strcontains(google_logging_metric.oom_near_miss.filter, "jsonPayload.oom_near_miss=true")
    error_message = "a near miss has to be countable before it becomes a kill"
  }
}

run "the_alerts_page_on_what_a_human_would_act_on" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    service_names            = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
    wake_subscription        = "swarm-scheduler-wake-sub"
    dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
    safety_tick_job          = "swarm-scheduler-tick"
    alert_emails             = ["platform@saga.xyz"]
  }

  # The single most important policy: if the one-minute tick dies quietly, the
  # platform looks healthy right up until someone notices their tasks have been
  # READY for an hour.
  assert {
    condition     = google_monitoring_alert_policy.safety_tick_absent[0].severity == "CRITICAL"
    error_message = "a stopped safety tick is the highest-severity condition this platform has"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.safety_tick_absent[0].conditions[0].condition_absent[0].filter, "swarm-scheduler-tick")
    error_message = "the absence alert must watch the actual tick job"
  }

  # The project is shared: a prefix match would catch another team's services.
  assert {
    condition     = strcontains(google_monitoring_alert_policy.service_errors[0].conditions[0].condition_threshold[0].filter, "one_of(\"swarm-api\", \"swarm-scheduler\", \"swarm-quota-broker\", \"swarm-reconciler\")")
    error_message = "control-plane alerts must name the swarm services explicitly in a shared project"
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.service_errors[0].conditions[0].condition_threshold[0].filter, "resource.labels.location = \"us-central1\"")
    error_message = "alerts are pinned to the swarm's own region"
  }

  # Fencing should be flat zero: a sustained rate means two attempts for one
  # task are being dispatched.
  assert {
    condition     = google_monitoring_alert_policy.generation_fenced[0].conditions[0].condition_threshold[0].threshold_value == 0
    error_message = "any sustained fencing rate is worth a look"
  }

  assert {
    condition = length([
      for p in [
        google_monitoring_alert_policy.safety_tick_absent[0].display_name,
        google_monitoring_alert_policy.service_errors[0].display_name,
        google_monitoring_alert_policy.job_failures[0].display_name,
        google_monitoring_alert_policy.generation_fenced[0].display_name,
        google_monitoring_alert_policy.dead_letter_backlog[0].display_name,
        google_monitoring_alert_policy.wake_backlog[0].display_name,
        google_monitoring_alert_policy.tasks_dead_lettered[0].display_name,
      ] : p if startswith(p, "swarm-dev-")
    ]) == 7
    error_message = "every policy is named for its environment and carries the swarm prefix"
  }

  assert {
    condition     = google_monitoring_dashboard.control_plane.project == "saga-agents-staging"
    error_message = "the dashboard belongs to the swarm project"
  }

  # The reconciler held five dead leases for hours on 2026-09-24 because it
  # could not read GKE, said so in two log lines on every pass, and reported
  # `findings=0` -- and nothing watched those lines. This policy is what does.
  assert {
    condition = (
      length(google_monitoring_alert_policy.reconciler_blind) == 1 &&
      google_monitoring_alert_policy.reconciler_blind[0].user_labels["managed-by"] == "swarm-terraform" &&
      google_monitoring_alert_policy.reconciler_blind[0].display_name == "swarm-dev-reconciler-cannot-see-a-backend"
    )
    error_message = "the reconciler-blindness policy must exist, be named for its environment and carry managed-by=swarm-terraform"
  }

  # The filters match the reconciler's messages verbatim. The Python side of the
  # same pin (test_reconciler_gke_namespaced.py) checks the emitter spells them
  # identically; a filter that matches nothing is an alert that never fires.
  assert {
    condition = (
      strcontains(google_logging_metric.reconciler_backend_unavailable.filter, "jsonPayload.message=\"backend unavailable; skipping its findings\"") &&
      strcontains(google_logging_metric.reconciler_not_repairing.filter, "jsonPayload.message=\"not repairing: the backend that would hold this execution was unreadable\"") &&
      strcontains(google_logging_metric.reconciler_not_repairing.filter, "resource.labels.service_name=(\"swarm-reconciler\")")
    )
    error_message = "the reconciler log metrics must match the exact lines reconciler/repair.py writes, from the swarm-reconciler service only"
  }

  # Sustained, not a blip: three blind passes in fifteen minutes, grouped by
  # backend; and the same lease held back for thirty minutes, grouped by lease.
  assert {
    condition = (
      google_monitoring_alert_policy.reconciler_blind[0].conditions[0].condition_threshold[0].threshold_value == 2 &&
      google_monitoring_alert_policy.reconciler_blind[0].conditions[0].condition_threshold[0].aggregations[0].alignment_period == "900s" &&
      contains(google_monitoring_alert_policy.reconciler_blind[0].conditions[0].condition_threshold[0].aggregations[0].group_by_fields, "metric.labels.backend") &&
      google_monitoring_alert_policy.reconciler_blind[0].conditions[1].condition_threshold[0].duration == "1800s" &&
      contains(google_monitoring_alert_policy.reconciler_blind[0].conditions[1].condition_threshold[0].aggregations[0].group_by_fields, "metric.labels.lease_id")
    )
    error_message = "the reconciler policy fires on 3+ blind passes per backend and on one lease held back for more than 30 minutes"
  }
}

run "alerts_can_be_deferred_until_the_apps_emit_anything" {
  command = plan

  module {
    source = "../../terraform/modules/monitoring"
  }

  variables {
    service_names            = ["swarm-api"]
    wake_subscription        = "swarm-scheduler-wake-sub"
    dead_letter_subscription = "swarm-scheduler-wake-dlq-sub"
    safety_tick_job          = "swarm-scheduler-tick"
    create_alerts            = false
  }

  assert {
    condition     = length(google_monitoring_alert_policy.safety_tick_absent) == 0
    error_message = "a first apply before any image exists should not page anybody"
  }

  assert {
    condition     = length(google_monitoring_alert_policy.reconciler_blind) == 0
    error_message = "the reconciler-blindness policy follows create_alerts like every other policy"
  }

  assert {
    condition     = length(output.log_metric_names) > 0
    error_message = "the metrics and the dashboard still exist when the policies do not"
  }
}

run "the_registry_is_regional_and_pullable_only_by_swarm_identities" {
  command = plan

  module {
    source = "../../terraform/modules/artifact_registry"
  }

  variables {
    readers = {
      "swarm-api"        = "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      "swarm-reconciler" = "serviceAccount:swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com"
    }

    pullers = {
      "tenant-eng"      = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
      "tenant-research" = "serviceAccount:swarm-agent-worker-research@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_artifact_registry_repository.this.location == "us-central1"
    error_message = "pulling a multi-gigabyte agent image across regions is paid for on every cold start"
  }

  assert {
    condition     = google_artifact_registry_repository.this.repository_id == "swarm-images"
    error_message = "the repository id must match swarm_common.config.Settings.artifact_registry"
  }

  assert {
    condition     = output.image_base == "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images"
    error_message = "the image base is what the dispatcher builds every runner image reference from"
  }

  assert {
    condition = length([
      for p in google_artifact_registry_repository.this.cleanup_policies : p if p.id == "keep-recent"
    ]) == 1
    error_message = "a KEEP policy must exist, or a cleanup could remove every rollback target"
  }

  assert {
    condition = alltrue([
      for k, m in google_artifact_registry_repository_iam_member.readers :
      m.role == "roles/artifactregistry.reader" && m.member != "allUsers"
    ])
    error_message = "images are pulled by named identities only"
  }

  assert {
    condition     = length(google_artifact_registry_repository_iam_member.writers) == 0
    error_message = "no runtime identity may push an image; writers are CI only and are not set here"
  }

  # Artifact Registry IAM stops at the repository, so a tenant worker's grant
  # cannot be narrowed to the images its profiles name. What CAN be narrowed is
  # the shape: a role with no *.list permission means an image is only reachable
  # under a name the caller already holds, so a customer- or tenant-specific
  # runner image published into this repository is not discoverable by another
  # tenant's worker. Without this the grant is a standing cross-tenant read.
  assert {
    condition = alltrue([
      for k, m in google_artifact_registry_repository_iam_member.pullers :
      m.role == "projects/saga-agents-staging/roles/swarmImagePuller"
    ])
    error_message = "tenant workers must hold the pull-only custom role, not roles/artifactregistry.reader"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.image_puller[0].permissions :
      !strcontains(lower(p), "list")
    ])
    error_message = "the pull role grants no enumeration: one tenant's worker must not be able to list every image name in the shared repository"
  }

  assert {
    condition     = contains(google_project_iam_custom_role.image_puller[0].permissions, "artifactregistry.repositories.downloadArtifacts") && contains(google_project_iam_custom_role.image_puller[0].permissions, "artifactregistry.repositories.get")
    error_message = "the pull role must still be able to pull, or every worker fails at image pull instead of running"
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.image_puller[0].permissions :
      !can(regex("(create|update|delete|upload|export)", p))
    ])
    error_message = "a worker identity that can push or delete an image can replace the runtime every other tenant runs"
  }

  # The control plane and the tenants are deliberately on different grants.
  assert {
    condition = !anytrue([
      for k, m in google_artifact_registry_repository_iam_member.readers :
      strcontains(m.member, "swarm-agent-worker-")
    ])
    error_message = "a tenant worker in `readers` would take roles/artifactregistry.reader and with it the whole enumeration surface"
  }
}

run "a_puller_role_that_can_enumerate_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/artifact_registry"
  }

  variables {
    pullers = {
      "tenant-eng" = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
    }

    puller_permissions = [
      "artifactregistry.repositories.get",
      "artifactregistry.repositories.downloadArtifacts",
      "artifactregistry.packages.list",
    ]
  }

  expect_failures = [var.puller_permissions]
}

run "a_puller_role_that_can_push_is_refused" {
  command = plan

  module {
    source = "../../terraform/modules/artifact_registry"
  }

  variables {
    pullers = {
      "tenant-eng" = "serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"
    }

    puller_permissions = [
      "artifactregistry.repositories.get",
      "artifactregistry.repositories.downloadArtifacts",
      "artifactregistry.repositories.uploadArtifacts",
    ]
  }

  expect_failures = [var.puller_permissions]
}
