# The sweep runs every two minutes (docs/design/voice-it-own-speech.md §4.5
# stop 1 and §6 row I4; lane L5 of its build plan, part of #2688).
#
# POST /v1/internal/voice/speech/sweep (swarm_api/routes/voice_speech.py,
# lane L4) ends a session whose lease is due and stops a speech machine that
# runs with no live holder. It is the stop that holds when every other one
# failed: a tab that vanished without a close, a swarm-api instance that died
# mid-session, a relay whose idle timer never fired. Without this job nothing
# calls the route, and such a machine bills its L4 until a person notices.
#
# THE IDENTITY IS THE TICK ACCOUNT (owner's recorded answer: the route admits
# the tick account and the doctor's, SWARM_SPEECH_SWEEP_CALLERS in locals.tf).
# The tick is the platform's existing Cloud Scheduler identity and the release
# deployer already acts as it (deployer.tf "swarm-tick"), so this adds no
# account and no actAs. It held run.invoker on the scheduler, the broker and
# the reconciler but NOT on swarm-api, and Cloud Run refuses a caller without
# it before the route's own check runs, so `speech_sweep_tick_invokes_api`
# below is the one new grant: run.invoker on swarm-api alone, while the
# engine is on. The doctor's run.invoker on swarm-api (doctor.tf) exists only
# while the doctor runs, so it could not carry a job that must run whenever
# the machine exists.
#
# WHAT THE GRANT OPENS. Only the edge. In the application the tick is on no
# caller list but SWARM_SPEECH_SWEEP_CALLERS: it is not in ALLOWED_USERS nor a
# registered service workspace, and its gserviceaccount.com domain is in no
# ALLOWED_DOMAINS, so swarm_api.auth refuses it on every other route.
#
# THE TOKEN. An OIDC token for swarm-api's push audience, the constant
# SWARM_SPEECH_SWEEP_AUDIENCE names and swarm-api's custom_audiences accepts
# (main.tf), so neither side needs the service URL to agree on it.
#
# OFF with the engine: both resources exist only while voice_speech_on, the
# switch voice_speech.tf's machine sits behind. With it off the route answers
# {"enabled": false} anyway, but there is no machine to sweep and no reason to
# give the tick a grant on swarm-api.

resource "google_cloud_scheduler_job" "voice_speech_sweep" {
  count = local.voice_speech_on ? 1 : 0

  project     = var.project_id
  region      = var.region
  name        = "${var.name_prefix}-voice-speech-sweep"
  description = "managed-by=swarm-terraform; ends due Voice it speech sessions and stops an unheld speech machine every two minutes"
  # Every two minutes (§4.5): a machine left running with nobody on it costs
  # at most about two minutes of L4 before this stops it.
  schedule  = "*/2 * * * *"
  time_zone = "Etc/UTC"

  # A sweep is one Firestore lease read, one instances.get (bounded at 30
  # seconds, ComputeClient.TIMEOUT_SECONDS) and at most one instances.stop,
  # whose operation swarm-api waits on for up to ComputeClient.
  # OPERATION_SECONDS (300); a GPU machine's stop takes a minute or more. 360
  # seconds covers all three and the lease writes, so a sweep that is busy
  # stopping a machine is not recorded as a failure and retried mid-stop
  # (swarm-api's own request_timeout is 660s). It runs past the next tick only
  # while it stops a machine, and that tick finds the machine STOPPING and
  # leaves it alone.
  attempt_deadline = "360s"

  # One retry, ten seconds on, for a cold start or a transient 5xx. The sweep
  # is fenced by the lease's generation and skips a machine already stopped
  # or STOPPING, so a retry that overlaps the next tick does no harm; more
  # retries would only duplicate the next scheduled run.
  retry_config {
    retry_count          = 1
    min_backoff_duration = "10s"
    max_backoff_duration = "10s"
    max_doublings        = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(module.cloud_run_api.service_urls["swarm-api"], "/")}/v1/internal/voice/speech/sweep"
    body        = base64encode(jsonencode({ source = "cloud-scheduler" }))

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = module.iam.tick_service_account
      audience              = local.push_audiences["swarm-api"]
    }
  }

  depends_on = [
    module.project_services,
    google_cloud_run_v2_service_iam_member.speech_sweep_tick_invokes_api,
  ]
}

# Row I4: run.invoker on the swarm-api service only, for the tick account,
# while the engine is on. See "THE IDENTITY" and "WHAT THE GRANT OPENS" above.
resource "google_cloud_run_v2_service_iam_member" "speech_sweep_tick_invokes_api" {
  count = local.voice_speech_on ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = [for k, _ in module.cloud_run_api.service_ids : k if k == "swarm-api"][0]
  role     = "roles/run.invoker"
  member   = module.iam.tick_member
}
