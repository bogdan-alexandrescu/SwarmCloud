# Voice it's two alerts: a voice session left live with no activity, and the
# speech machine left running with no session holding it (docs/operations.md,
# "Voice it alerts").
#
# THE SIGNAL. Both read one structured line swarm-api writes at the end of
# every speech sweep (voice_speech_sweep.tf, every two minutes), event
# `voice_sweep_state` (swarm_api/routes/voice_speech.py SWEEP_STATE_EVENT):
#
#   * `voice_live_idle_seconds`: the most seconds any session still `live`
#     after the sweep has gone without activity (sessions.py last_activity);
#   * `speech_machine_unheld`: the sweep found the machine not at rest with
#     nothing holding it (no session's lease, no warm lease inside its idle
#     time).
#
# Both stops already exist: the sweep ends a session idle LIVE_STALE_SECONDS
# (1800: the longest idle stop Settings offer, 15 minutes, plus 15) and stops
# an unheld machine at once. These alerts say a stop did not take, so a
# person looks before the bill does.
#
# HOW LONG IS MEASURED. Idle time is in the line, so the voice metric counts
# only lines at or past its threshold and one is enough. The machine's line
# is a yes or no, so its metric counts the yeses and the policy's `duration`
# is the threshold: the condition holds while every 600-second window has at
# least one (the sweep writes two to five per window), for that long.
#
# NEW METRICS, NEVER AN EXISTING ONE CHANGED (a label change forces a
# replace, archived issue 1822). The thresholds move a filter and a duration,
# both updated in place; no metric here has a label.
#
# OFF with the engine (local.voice_speech_on): the sweep is the only writer of
# the line and its scheduler job exists only then; a policy on a line nobody
# writes could never fire and would only look like cover. The channels are
# the monitoring module's, as every policy's: no channel, account or grant is
# made here.

variable "voice_session_idle_alert_seconds" {
  description = "Seconds a Voice it session may stay live with no activity before the voice-session-idle alert fires (docs/operations.md, \"Voice it alerts\")."
  type        = number
  # 2700 (45 minutes): the sweep ends a session idle 1800 seconds at its next
  # run, two minutes on, so a healthy platform never logs more than about
  # 1920. 2700 is that bound plus about six failed sweeps: past any race,
  # inside the hour, before a forgotten session runs up its afternoon.
  default = 2700

  validation {
    # Below 1800 + one sweep (120 s) the alert fires on sessions the sweep is
    # about to end by itself; a day is the most a person could want to wait.
    condition     = var.voice_session_idle_alert_seconds >= 1920 && var.voice_session_idle_alert_seconds <= 86400 && floor(var.voice_session_idle_alert_seconds) == var.voice_session_idle_alert_seconds
    error_message = "voice_session_idle_alert_seconds is whole seconds from 1920 (the sweep's own bound, 1800, plus one sweep) to 86400."
  }
}

variable "speech_machine_unheld_alert_seconds" {
  description = "Seconds the speech machine may run with no session holding it before the speech-machine-unheld alert fires (docs/operations.md, \"Voice it alerts\")."
  type        = number
  # 1200 (20 minutes): the sweep stops an unheld machine at its next run and a
  # GPU machine's stop takes a minute or two, so a healthy platform logs one
  # or two yeses in a row. 1200 is about ten sweeps that did not stop it: an
  # L4 billed for twenty minutes with nobody on it, not an afternoon.
  default = 1200

  validation {
    # Under 600 seconds one window of the condition (600 s) already outlasts
    # it; a Monitoring condition's duration is whole minutes, at most a day.
    condition     = var.speech_machine_unheld_alert_seconds >= 600 && var.speech_machine_unheld_alert_seconds <= 86400 && var.speech_machine_unheld_alert_seconds % 60 == 0
    error_message = "speech_machine_unheld_alert_seconds is whole minutes, in seconds, from 600 to 86400."
  }
}

locals {
  # swarm-api's service, as the monitoring module names it (its api_services):
  # the line is swarm-api's alone, and the project is shared.
  voice_alert_api_services = coalescelist(
    [for s in module.cloud_run_api.service_names : s if endswith(s, "-api")],
    ["${var.name_prefix}-api"],
  )

  # Logging query syntax: `field=(a OR b)`.
  voice_sweep_state_source = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=(${join(" OR ", [for s in local.voice_alert_api_services : "\"${s}\""])})",
    "resource.labels.location=\"${var.region}\"",
    "jsonPayload.event=\"voice_sweep_state\"",
  ])

  voice_session_idle_doc = <<-DOC
    A Voice it session has been live for over ${var.voice_session_idle_alert_seconds} seconds with no activity: no turn, no Talk tab connected, nothing since it went live. The speech sweep should have ended it at 1800 seconds (cause `abandoned`) and did not.

    What to do (docs/operations.md, "Voice it alerts"): read swarm-api's log for `voice sweep: marker` and `abandoned voice session` warnings, which name the session and why it was not ended; check the `${var.name_prefix}-voice-speech-sweep` Cloud Scheduler job ran. The person can end the session from Talk; a person's next Start also ends it.
  DOC

  speech_machine_unheld_doc = <<-DOC
    The speech machine (an L4 GPU machine, billed while it runs) has been running for over ${var.speech_machine_unheld_alert_seconds} seconds with no Voice it session or warm start holding it. The speech sweep stops such a machine at its next run, and has not.

    What to do (docs/operations.md, "Voice it alerts"): in Compute Engine, stop the `${local.speech_machine_prefix}machine` instance in whichever zone it is (swarm-api starts it again for the next session). Then read swarm-api's `speech sweep` lines for the instance status and the stop's failure, and check the `${var.name_prefix}-voice-speech-sweep` Cloud Scheduler job ran.
  DOC
}

resource "google_logging_metric" "voice_session_idle" {
  count = local.voice_speech_on ? 1 : 0

  project     = var.project_id
  name        = "${var.name_prefix}/voice-session-idle"
  description = "managed-by=swarm-terraform; speech sweeps that found a Voice it session live with no activity for voice_session_idle_alert_seconds or more."

  # The threshold is in the filter: a sweep line counts only past it.
  filter = "${local.voice_sweep_state_source} AND jsonPayload.voice_live_idle_seconds>=${var.voice_session_idle_alert_seconds}"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "speech_machine_unheld" {
  count = local.voice_speech_on ? 1 : 0

  project     = var.project_id
  name        = "${var.name_prefix}/speech-machine-unheld"
  description = "managed-by=swarm-terraform; speech sweeps that found the speech machine not at rest with no session or warm start holding it."

  filter = "${local.voice_sweep_state_source} AND jsonPayload.speech_machine_unheld=true"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "voice_session_idle" {
  count = local.voice_speech_on && var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-voice-session-idle"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "a Voice it session is live with no activity past the threshold"

    condition_threshold {
      # Monitoring requires a resource.type restriction on a log-based
      # metric's condition; the line is swarm-api's, a Cloud Run service.
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.voice_session_idle[0].name}\"",
      ])
      # One line past the threshold is enough: the threshold is in the metric.
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = module.monitoring.notification_channels

  documentation {
    content   = trimspace(local.voice_session_idle_doc)
    mime_type = "text/markdown"
  }

  # The sweep re-logs the session every two minutes while it stays live, so
  # an incident left open an hour past the last line has been resolved.
  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = local.labels
}

resource "google_monitoring_alert_policy" "speech_machine_unheld" {
  count = local.voice_speech_on && var.create_alerts ? 1 : 0

  project      = var.project_id
  display_name = "swarm-${var.environment}-speech-machine-unheld"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "the speech machine runs with no session past the threshold"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.speech_machine_unheld[0].name}\"",
      ])
      # Every 600-second window holds at least one "unheld" line, for the
      # threshold's whole length (see HOW LONG IS MEASURED above).
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "${var.speech_machine_unheld_alert_seconds}s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = module.monitoring.notification_channels

  documentation {
    content   = trimspace(local.speech_machine_unheld_doc)
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "3600s"
  }

  user_labels = local.labels
}
