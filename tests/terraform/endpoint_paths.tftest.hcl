# The paths Cloud Scheduler and Pub/Sub POST to must exist in the applications.
#
# Two of them did not. `scheduler_push_path` was "/pubsub/wake" against an app
# serving "/pubsub/push", and `quota_broker_path` was "/refresh" against an app
# whose sweep endpoint is "/v1/quota/sweep". Both produced 404s that nothing
# alerts on: a push subscription retries a 404 quietly, so the scheduler was
# never woken and every submitted task stayed READY forever while every health
# check stayed green.
#
# These pin the defaults to the routes the applications actually serve, so a
# rename fails in CI rather than in production silence.

variables {
  project_id              = "saga-agents-staging"
  scheduler_push_endpoint = "https://swarm-scheduler.example.run.app"
  reconciler_endpoint     = "https://swarm-reconciler.example.run.app"
  quota_broker_endpoint   = "https://swarm-quota-broker.example.run.app"
  tick_service_account    = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
  labels                  = { managed-by = "swarm-terraform" }
}

run "configured_paths_match_the_routes_the_apps_serve" {
  command = plan

  module {
    source = "../../terraform/modules/scheduler"
  }

  assert {
    condition     = var.scheduler_push_path == "/pubsub/push"
    error_message = "scheduler/main.py serves @app.post(\"/pubsub/push\"); any other value 404s every wake and no task is ever admitted."
  }

  assert {
    condition     = var.reconciler_path == "/reconcile"
    error_message = "reconciler/service.py serves @app.post(\"/reconcile\")."
  }

  assert {
    condition     = var.quota_broker_path == "/v1/quota/sweep"
    error_message = "quota_broker/main.py serves @app.post(\"/v1/quota/sweep\"); \"/refresh\" has never existed."
  }
}
