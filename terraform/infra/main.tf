# ---------------------------------------------------------------------------
# The swarm, composed.
#
# Dependency order is deliberate and acyclic:
#
#   services -> network -> registry/storage/firestore -> iam -> cloud run
#            -> scheduler (needs the service URLs) -> monitoring
#
#   gke -> tenancy (needs the Workload Identity pool) -> secrets -> jobs
#
# The one place a cycle would have appeared is the wake topic: the API needs its
# name in the environment, while the subscription needs the API's service
# account. It is broken by deriving the topic name in locals and passing it into
# the scheduler module, rather than reading it back out.
# ---------------------------------------------------------------------------

module "project_services" {
  source = "../modules/project_services"

  count = var.manage_project_services ? 1 : 0

  project_id = var.project_id

  services = [
    "artifactregistry.googleapis.com",
    "cloudidentity.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    # containerscanning.googleapis.com is NOT held here (owner decision
    # 2026-10-21): Google's per-image scanning cost ~$0.26 an image for
    # results nothing read, and trivy is every gate (see
    # terraform/modules/artifact_registry/main.tf). Removing it from this list
    # does not disable it -- disable_on_destroy is false in this shared
    # project -- it stops releases from re-enabling it. The API itself is
    # turned off once, by hand: `gcloud services disable
    # containerscanning.googleapis.com --project saga-agents-staging`.
    # The personal-workspace job's dispatch, option (ii) of
    # docs/workspaces.md §2.1 (#847, W4b): an Eventarc trigger on the
    # swarm-workspace-apply topic starts a workflow that runs the job. Enabled
    # here, by the release, before the owner's bootstrap apply creates them.
    "eventarc.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    # The front door. IAP is the outer gate in front of swarm-api; see
    # terraform/modules/frontend.
    "iap.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "workflows.googleapis.com",
  ]
}

module "network" {
  source = "../modules/network"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix

  subnet_cidr          = var.subnet_cidr
  pods_cidr            = var.pods_cidr
  services_cidr        = var.services_cidr
  gke_master_cidr      = var.gke_master_cidr
  nat_static_ip_count  = var.nat_static_ip_count
  nat_min_ports_per_vm = var.nat_min_ports_per_vm
  nat_max_ports_per_vm = var.nat_max_ports_per_vm
  restrict_egress      = var.restrict_egress

  labels = local.labels

  depends_on = [module.project_services]
}

module "artifact_registry" {
  source = "../modules/artifact_registry"

  project_id     = var.project_id
  region         = var.region
  repository_id  = var.artifact_registry_repository
  immutable_tags = var.immutable_image_tags

  # The cleanup (#1886): dry run until the owner has read the would-delete
  # list (docs/runbooks/registry-cleanup.md).
  cleanup_dry_run      = var.cleanup_dry_run
  untagged_ttl         = var.untagged_ttl
  keep_recent_versions = var.keep_recent_versions

  # Two grants, not one, because the two sides need different things.
  #
  # The control plane may enumerate the repository: the reconciler compares what
  # is published against what is referenced, and roles/artifactregistry.reader is
  # the role for that.
  #
  # A tenant worker only ever pulls the one image its profile names, and it runs
  # attacker-controlled code by design. Artifact Registry IAM cannot be narrowed
  # below the repository, so the narrowing that IS available is the shape of the
  # access: `pullers` gets a custom role with every *.list permission removed, so
  # a tenant cannot discover image names it was not given -- which is what keeps
  # this from becoming a cross-tenant read the day a customer-specific runner
  # image is published here.
  #
  # Both are keyed by component and tenant, because the emails themselves are not
  # known until apply.
  readers = { for c in local.control_plane_services : c => module.iam.service_account_members[c] }

  pullers = { for t, email in module.tenancy.worker_service_accounts : "tenant-${t}" => "serviceAccount:${email}" }

  labels = local.labels

  depends_on = [module.project_services]
}

module "storage" {
  source = "../modules/storage"

  project_id    = var.project_id
  location      = upper(var.region)
  name_prefix   = var.name_prefix
  bucket_suffix = var.project_id

  artifact_retention_days = var.artifact_retention_days
  force_destroy           = var.bucket_force_destroy
  tenants                 = local.tenant_ids

  labels = local.labels

  depends_on = [module.project_services]
}

module "firestore" {
  source = "../modules/firestore"

  project_id  = var.project_id
  location    = var.region
  database_id = var.firestore_database

  # Its own switch, not var.deletion_protection: dev turns that one off for
  # the GKE cluster and the Cloud Run services, and a deleted `swarm` takes its
  # point-in-time history with it -- total loss of the control plane (#1818).
  delete_protection = var.firestore_delete_protection
  deletion_policy   = "ABANDON"

  bootstrap_documents = var.bootstrap_firestore_documents
  pools               = local.pools

  bench_database_id = var.firestore_bench_database

  tenant_documents = {
    for t, cfg in var.tenants : t => {
      kind            = cfg.kind
      principal       = cfg.principal
      display_name    = cfg.display_name
      max_active      = local.tenant_max_active[t]
      capacity_units  = cfg.capacity_units
      credentials     = cfg.providers
      service_account = module.tenancy.worker_service_accounts[t]
      gcs_prefix      = module.tenancy.gcs_prefixes[t]
      namespace       = module.tenancy.namespaces[t]
    }
  }

  depends_on = [module.project_services]
}

module "gke_autopilot" {
  source = "../modules/gke_autopilot"

  count = var.enable_gke_autopilot ? 1 : 0

  project_id   = var.project_id
  region       = var.region
  cluster_name = "${var.name_prefix}-autopilot"

  network             = module.network.network_id
  subnetwork          = module.network.subnetwork_id
  pods_range_name     = module.network.pods_range_name
  services_range_name = module.network.services_range_name

  master_ipv4_cidr_block  = var.gke_master_cidr
  release_channel         = var.gke_release_channel
  enable_private_endpoint = var.gke_enable_private_endpoint
  master_authorized_cidrs = var.gke_master_authorized_cidrs
  # Opening the control plane is recorded in the environment's tfvars, so it
  # shows up in a diff rather than as an edit to the rule that guards prod.
  allow_open_master_authorized_network = var.gke_allow_open_master_authorized_network
  deletion_protection                  = var.deletion_protection

  labels = local.labels

  depends_on = [module.project_services]
}

module "iam" {
  source = "../modules/iam"

  project_id         = var.project_id
  firestore_database = var.firestore_database
  gke_enabled        = var.enable_gke_autopilot
  artifact_bucket    = module.storage.artifact_bucket_name

  # Named from configuration rather than read back from the cluster module: the
  # cluster is behind a `count`, and the IAM condition must be known at plan
  # time. The string is the same one gke_autopilot is given.
  gke_cluster_name = "${var.name_prefix}-autopilot"
  gke_location     = var.region

  labels = local.labels
}

module "tenancy" {
  source = "../modules/tenancy"

  project_id         = var.project_id
  firestore_database = var.firestore_database
  artifact_bucket    = module.storage.artifact_bucket_name

  workload_identity_pool = var.enable_gke_autopilot ? module.gke_autopilot[0].workload_identity_pool : ""

  tenants = var.tenants

  # #1457 design B's last step (variables.tf says why it defaults true, and
  # when false plans): true is today's swarmTenantWorkerFirestore binding per
  # tenant; false removes every one of them.
  worker_firestore_access = var.worker_firestore_access

  # The same last step, one Terraform tenant at a time (variables.tf says why it
  # defaults empty, and when an entry plans): a listed tenant's binding is
  # destroyed and every other tenant's keeps its key.
  worker_firestore_off_tenants_iam = var.worker_firestore_off_tenants_iam

  # Only the dispatcher and the reconciler may name a tenant SA on a Job.
  # The deployer is here because it DEPLOYS the tenant jobs that run as these
  # accounts, which needs actAs on each (see deployer.tf). Static key, so a new
  # tenant gets the grant in the same apply that creates its account -- which
  # works only once the account exists and terraform/bootstrap's per-account
  # serviceAccountAdmin grant on it is applied, since this binding is a
  # setIamPolicy on the account (#334; modules/tenancy/main.tf has the order).
  dispatcher_members = merge(
    {
      scheduler  = module.iam.service_account_members["swarm-scheduler"]
      reconciler = module.iam.service_account_members["swarm-reconciler"]
    },
    var.deployer_service_account == "" ? {} : { deployer = local.deployer_member },
  )

  labels = local.labels
}

module "secret_manager" {
  source = "../modules/secret_manager"

  project_id = var.project_id
  region     = var.region

  # admin_members is per tenant, not one global list. A single list applied to
  # every tenant means every one of those identities can REPLACE any other
  # tenant's provider key with one pointing at a proxy they control -- which
  # secretVersionAdder permits even though it cannot read the key in place.
  # var.secret_admin_members is the fallback for tenants that name nobody, and
  # it is validated to exclude any tenant's own principal.
  tenant_secrets = {
    for t, cfg in module.tenancy.secret_inputs : t => {
      providers     = cfg.providers
      accessor      = cfg.accessor
      admin_members = local.tenant_secret_admins[t]
    }
  }

  # swarm-api reads each tenant's -git secret for the issue preview (#454,
  # mock-up 1A), by a per-secret binding on that secret alone. This reaches
  # only a -git secret Terraform manages; a script-registered one (every
  # tenant's today) gets the same grant from scripts/register-tenant.sh.
  forge_readers = [module.iam.service_account_members["swarm-api"]]

  # One writer, and this is it. See the variable's own description, and
  # quota_broker.credentials, for why a second one corrupts a rotating
  # credential rather than merely duplicating work.
  enable_subscription_refresh = true
  refresher_member            = module.iam.service_account_members["swarm-quota-broker"]

  # The SwarmCloud GitHub App's client secret and private key (#780, OB2):
  # two empty platform slots swarm-api alone reads. github_app.tf says why
  # the user slots' grants are not made here.
  # The App's two secret containers exist under enable_console_app_setup alone
  # too, so the console's App step has somewhere to store the manifest
  # exchange's results before any App switch is on (DAYONE-TF-2).
  github_app_secrets_enabled = var.enable_github_app || var.enable_console_app_setup
  github_app_secret_readers  = [module.iam.service_account_members["swarm-api"]]

  labels = local.labels

  depends_on = [module.project_services]
}

# The IAP service agent's email is `service-<PROJECT NUMBER>@gcp-sa-iap...`, so
# the number has to come from somewhere. Read rather than hardcoded: a project
# number pasted into a repository is right until the day someone stands this up
# in a second project, at which point it is silently wrong.
data "google_project" "this" {
  project_id = var.project_id
}

module "cloud_run" {
  source = "../modules/cloud_run"

  project_id = var.project_id
  region     = var.region

  network    = module.network.network_id
  subnetwork = module.network.subnetwork_id

  deletion_protection = var.deletion_protection

  services = {
    "swarm-api" = {
      service_account_email = module.iam.service_account_emails["swarm-api"]
      image                 = local.image["swarm-api"]
      max_instances         = var.service_max_instances["swarm-api"]
      # TWO WARM INSTANCES, ALWAYS (owner decision Q6(a), 2026-10-17, #1457).
      # Under design B every worker's heartbeat, checkpoint, park and release
      # goes through swarm-api (docs/design/tenant-settings-integrity.md), so a
      # cold start or a single instance's restart is a gap in every running
      # attempt's lease renewal, and a gap past the 120 s lease timeout gets
      # live attempts fenced. Two, not one, so one instance's replacement never
      # leaves none; the 30 s / 120 s timings stay as they are. Fixed cost: about
      # $105 a month per environment at list price (1 vCPU, 1 GiB, CPU always
      # allocated, ~$53 an instance). A floor, not demand: no QUEUED task adds
      # to it (invariant 1). Every other service still scales to zero.
      min_instances = 2
      cpu           = "1"
      memory        = "1Gi"
      # 36 STARTUP-PROBE FAILURES, NOT THE MODULE'S 12 (owner decision (d),
      # 2026-10-21, #1816). The probe polls every 5 s, and with 12 failures a
      # start is killed at about 96 s; healthy but slow swarm-api starts were
      # measured at 61-77 s on 2026-10-21 and five starts were killed. 36 gives
      # three times the window. It costs nothing: a start that binds earlier is
      # ready the moment it binds, and min_instances stays 2. Every other
      # service keeps 12.
      startup_failure_threshold = 36
      # STARTUP CPU BOOST ON (BGD's decision, 2026-10-23, #1816 option (a)).
      # Most of a ~21 s start is Python startup on 1 vCPU, and starts that ran
      # past the probe left callers with Cloud Run's 429 on 2026-10-21. The
      # boost bursts past the 1 vCPU limit while an instance starts, so it is
      # BGD's recorded exception to invariant 7 for this one service; about $1
      # a month (35-80 starts a day, ~30 boosted vCPU-seconds each). Every
      # other service keeps the module's false, and cpu_idle stays false here.
      startup_cpu_boost = true
      # The ONLY control-plane service that calls another one. It proxies every
      # account-pool request to the quota broker, whose ingress is
      # internal-and-cloud-load-balancing; under PRIVATE_RANGES_ONLY the
      # broker's public hostname is not a private range, so the call leaves the
      # VPC, arrives as external traffic and is refused with an HTML 404. The
      # worker jobs already run ALL_TRAFFIC, which is why they could reach the
      # broker and this could not.
      vpc_egress  = "ALL_TRAFFIC"
      concurrency = 80
      # 660s, NOT THE MODULE'S 300s (#2576). git_push_branch, git_fetch and
      # git_rebase_onto run git against GitHub under
      # gitexec.CALL_DEADLINE_SECONDS = 600 (apps/swarm-api/swarm_api/gitexec.py);
      # at 300s Cloud Run cut a longer call with a 504 the worker read as
      # api_unavailable, and the publish failed. 600 plus 60 s for the bundle
      # and the answer. The worker waits the same 660 s for these tools:
      # agent_worker.gittools.GIT_CALL_TIMEOUT_SECONDS. Change all three
      # together; tests/terraform/cloud_run.tftest.hcl holds this one.
      request_timeout = "660s"
      env             = local.service_env["swarm-api"]
      # SWARM_CHILD_KEY (and, during a rotation, SWARM_CHILD_KEY_PREVIOUS) by
      # Secret Manager reference, only once enable_child_tasks is on
      # (child_tasks.tf); and SWARM_LIVE_KEY, the live relay's ticket key,
      # only once the relay is on (locals.tf `live_key_secret_env`, live.tf);
      # and SWARM_SETTINGS_KEY always (settings_mac.tf).
      secret_env = local.api_secret_env
      # The audience a worker mints its ID token for when it calls the child
      # routes (SWARM_API_AUDIENCE): a constant, because the worker cannot be
      # told this service's URL-shaped API_AUDIENCE without a cycle. Accepted
      # in addition to the URL; people's clients are unaffected.
      custom_audiences = [local.push_audiences["swarm-api"]]
      # THE IAP SERVICE AGENT, EXPLICITLY.
      #
      # IAP invokes Cloud Run AS THIS IDENTITY, and without it the load balancer
      # answers every request with "The IAP service account is not provisioned"
      # -- a 500-class failure that looks like the app being broken rather than
      # a missing grant.
      #
      # Not left to `allUsers`, which currently holds run.invoker and would
      # cover it by accident. That grant exists for a different reason (Cloud
      # Run's edge IAM consumes the Authorization header, destroying the only
      # credential that identifies a tenant), and the day it is tightened IAP
      # would break for a reason nobody would connect to it.
      #
      # The AGENT ITSELF is not created here: it is a Google-managed identity
      # and `google_project_service_identity` lives only in the google-beta
      # provider, which this repository does not carry for one resource. It is a
      # once-per-project command, documented in modules/frontend/main.tf beside
      # the OAuth brand:
      #
      #   gcloud beta services identity create \
      #     --service=iap.googleapis.com --project=<project>
      #
      # And each tenant's WORKER, once child tasks are on: the worker-only
      # child routes are the one thing a worker calls here (child_tasks.tf).
      invokers = merge(
        { for member in var.api_invokers : member => member },
        { iap = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com" },
        local.child_route_invokers,
      )
    }
    "swarm-scheduler" = {
      service_account_email = module.iam.service_account_emails["swarm-scheduler"]
      image                 = local.image["swarm-scheduler"]
      max_instances         = var.service_max_instances["swarm-scheduler"]
      cpu                   = "2"
      memory                = "2Gi"
      # The drain loop is a single serialised pass over admissible work.
      # Concurrent requests on one instance would have them contend on the same
      # Firestore documents and abort each other.
      concurrency     = 1
      request_timeout = "540s"
      env             = local.service_env["swarm-scheduler"]
      # SWARM_CHILD_KEY once child tasks are on, and SWARM_SETTINGS_KEY
      # (settings_mac.tf): locals.tf `scheduler_secret_env`.
      secret_env       = local.scheduler_secret_env
      custom_audiences = [local.push_audiences["swarm-scheduler"]]
      invokers         = { tick = module.iam.tick_member }
    }
    "swarm-quota-broker" = {
      service_account_email = module.iam.service_account_emails["swarm-quota-broker"]
      image                 = local.image["swarm-quota-broker"]
      max_instances         = var.service_max_instances["swarm-quota-broker"]
      cpu                   = "1"
      memory                = "512Mi"
      concurrency           = 40
      env                   = local.service_env["swarm-quota-broker"]
      custom_audiences      = [local.push_audiences["swarm-quota-broker"]]
      # THE TENANT WORKER SERVICE ACCOUNTS ARE ON THIS LIST, and without them
      # the account pool cannot work at all. `/v1/accounts/assign` and
      # `/v1/accounts/{id}/release` are called by WORKERS -- they are the only
      # two routes here that are -- and Cloud Run rejects a caller that holds
      # no `run.invoker` before the application ever runs. The broker's own
      # identity check (`worker_sa_pattern`, pinned to this project) is what
      # decides WHICH tenant a worker is; this grant is only what lets the
      # request arrive so that check can run.
      #
      # It is not a widening: every route above is authorised per account by
      # `_authorize`/`may_serve`, so a worker on this list can still only be
      # assigned an account its own tenant owns or was lent.
      #
      # The reconciler calls one platform-only route here,
      # /v1/holds/release-attempt, when it fences an attempt (#380).
      #
      # Keyed by tenant id rather than by the member string, for the reason the
      # cloud_run module's own comment gives: a service account email is not
      # known until apply and cannot appear in a for_each key.
      invokers = merge(
        {
          tick       = module.iam.tick_member
          scheduler  = module.iam.service_account_members["swarm-scheduler"]
          api        = module.iam.service_account_members["swarm-api"]
          reconciler = module.iam.service_account_members["swarm-reconciler"]
        },
        {
          for tenant_id, member in module.tenancy.worker_members :
          "worker-${tenant_id}" => member
        },
      )
    }
    "swarm-ui" = {
      service_account_email = module.iam.service_account_emails["swarm-api"]
      image                 = local.image["swarm-ui"]
      max_instances         = var.service_max_instances["swarm-ui"]
      cpu                   = "1"
      memory                = "512Mi"
      concurrency           = 80
      # Static files. No Firestore, no secrets, no tenant data -- it reuses
      # swarm-api's service account only because nginx never calls Google, and
      # a dedicated identity with no bindings would be ceremony. If this service
      # ever needs to call anything, give it its own first.
      env = {}
      # Reached only through the load balancer, which presents the IAP identity.
      # Same as swarm-api: IAP fronts both backends, so it must be able to
      # invoke both. See the note on the swarm-api service above.
      invokers = merge(
        { for member in var.api_invokers : member => member },
        { iap = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com" },
      )
    }
    "swarm-reconciler" = {
      service_account_email = module.iam.service_account_emails["swarm-reconciler"]
      image                 = local.image["swarm-reconciler"]
      max_instances         = var.service_max_instances["swarm-reconciler"]
      cpu                   = "1"
      memory                = "1Gi"
      concurrency           = 1
      request_timeout       = "900s"
      env                   = local.service_env["swarm-reconciler"]
      invokers              = { tick = module.iam.tick_member }
    }
  }

  labels = local.labels

  depends_on = [module.project_services]
}

module "cloud_run_jobs" {
  source = "../modules/cloud_run_jobs"

  project_id = var.project_id
  region     = var.region

  network    = module.network.network_id
  subnetwork = module.network.subnetwork_id

  # Tagging the worker instances is what puts them in scope of the deny rule
  # that stops one tenant's worker connecting to another's on the shared subnet.
  network_tags = [module.network.worker_network_tag]

  resource_classes = local.resource_classes
  jobs             = local.jobs

  artifact_bucket = module.storage.artifact_bucket_name

  # Jobs are recreated by the dispatcher on demand, so deletion protection here
  # would block the reconciler's garbage collection rather than protect data.
  deletion_protection = false

  labels = local.labels

  depends_on = [module.secret_manager, module.project_services]
}

module "scheduler" {
  source = "../modules/scheduler"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix

  wake_topic_name = local.wake_topic

  scheduler_push_endpoint = module.cloud_run.service_urls["swarm-scheduler"]

  # The endpoint is still the URL -- that is where the request goes. The
  # audience is the constant both sides name, so the receiver can verify it.
  scheduler_push_audience = local.push_audiences["swarm-scheduler"]
  quota_broker_audience   = local.push_audiences["swarm-quota-broker"]
  reconciler_endpoint     = module.cloud_run.service_urls["swarm-reconciler"]
  quota_broker_endpoint   = module.cloud_run.service_urls["swarm-quota-broker"]
  enable_quota_refresh    = var.enable_quota_refresh

  tick_service_account = module.iam.tick_service_account
  kms_key_name         = var.pubsub_kms_key_name

  # D17: one workflow-rollup job per registered tenant, calling swarm-api as
  # the module's rollup-sweeper account. The audience is the service URL, the
  # same value verify.tf gives its own direct caller of swarm-api. The same
  # tenants get the #454 issue-run tick (`issue_run_advance`), as the same
  # account, on var.issue_run_advance_schedule's default of every minute,
  # and the stranded-PR sweep (`stranded_pr_sweep`, part of #295), as the
  # same account, at :19 and :49, which only reads, stores and logs
  # `pr_stranded` (redrive false; the monitoring module alerts on it). The
  # issue sweeper's job was deleted on 2026-10-12: the tenant's `issue-sweep`
  # schedule replaced it (modules/scheduler/jobs.tf).
  api_endpoint      = module.cloud_run.service_urls["swarm-api"]
  rollup_tenant_ids = toset(keys(var.tenants))

  # D2 (#780, OB2): swarm-forge-refresh, the GitHub user-token refresh sweep,
  # as the same rollup-sweeper account. Off until swarm-api serves the route.
  enable_forge_refresh = var.enable_forge_refresh

  # #748: known at plan time, unlike api_endpoint, so it can gate a count.
  enable_task_finished_push = true

  # #847: swarm-workspace-sweep, the caller of swarm-api's personal-workspace
  # dispatch sweep and so the stuck-workspace detector. A literal, never a
  # tfvars variable: it runs even while var.workspace_apply_publish is off,
  # which is exactly when an approved workspace waits (2026-10-09).
  enable_workspace_sweep = true

  # #1347 (owner decision Q5): swarm-workspace-secrets-rotation-sweep, the
  # hourly caller that ends a rotated-out workspace secret's 24-hour grace.
  # A literal, like the workspace sweep: an environment without it keeps
  # every rotated-out value enabled for ever.
  enable_secrets_rotation_sweep = true

  # #1433 (owner decision Q2): swarm-mcp-credentials-sweep, every 10 minutes,
  # deletes a per-run MCP credential once its task has ended. It follows
  # var.mcp_servers_enabled rather than being a literal like the two sweeps
  # above: it is the feature's own job, and an environment that never turns
  # the feature on stores no credential for it to delete. Turning the
  # feature OFF after credentials were stored removes the job with it; each
  # stored credential's Secret Manager ttl (its expires_in plus 5 minutes, at
  # most 24 h 5 min) then removes it, as design §4.4 provides for a sweep
  # that never runs.
  enable_mcp_credentials_sweep = var.mcp_servers_enabled

  # The API publishes a wake message on submission; the reconciler republishes
  # when it returns reclaimed work to READY.
  publisher_members = {
    api          = module.iam.service_account_members["swarm-api"]
    reconciler   = module.iam.service_account_members["swarm-reconciler"]
    quota_broker = module.iam.service_account_members["swarm-quota-broker"]
  }

  # #636: a worker publishes `task_finished` when it ends a task, so that
  # task's dependants are released at once, not on the next safety tick. Every
  # identity a task runs as: the tenant's worker account. Keys are known at
  # plan; the emails are not.
  worker_publisher_members = { for t, m in module.tenancy.worker_members : "worker:${t}" => m }

  # #627: swarm-api publishes a cancelled task's attempt; the reconciler, the
  # identity that already holds the stop permissions, is pushed it.
  execution_cancel_topic_name = local.execution_cancel_topic
  execution_cancel_publisher_members = {
    api = module.iam.service_account_members["swarm-api"]
  }

  labels = local.labels

  # NOT `depends_on = [module.cloud_run]`, though the ordering it would buy is
  # real: a custom audience is only accepted once it is present on the receiving
  # service, so the service must be updated before the subscription mints tokens
  # naming it.
  #
  # That ordering already exists. `scheduler_push_endpoint` above reads the
  # service's `uri`, and terraform's graph is built from references rather than
  # from which values changed -- so the subscription node depends on the service
  # node whether or not the URL moves, and the service is applied first.
  #
  # Adding the explicit dependency anyway is not free, measured on this plan:
  # a module-level depends_on defers that module's DATA SOURCES to apply time,
  # so `data.google_project.this` became unknown, the DLQ IAM members' `member`
  # became unknown with it, and two IAM bindings went from "no change" to
  # "must be replaced". 1 add / 18 change / 0 destroy became 3 / 18 / 2.
  depends_on = [module.project_services]
}

# The workflow-rollup jobs (D17), and the issue-run tick (#454) that runs as
# the same account, call swarm-api directly, so Cloud Run's edge
# must let their identity through before the app's ROLLUP_SWEEPER_ROUTES check
# is ever reached. `api_invokers` is not that grant: dev sets it to allUsers,
# prod to the tenant groups, and the sweeper is in neither -- so without this
# every prod job is a 403 and stored Workflow.state stays stale. Modelled on
# verify.tf's verify_invokes_api, the only other direct caller, and granted in
# every environment for the same reason. run.invoker on this one service is
# the account's only grant; the route allow-list narrows it from there.
resource "google_cloud_run_v2_service_iam_member" "rollup_sweeper_invokes_api" {
  project  = var.project_id
  location = var.region
  # `service_names` is a sorted list; take the name from the map-shaped output,
  # as verify_invokes_api does.
  name   = [for k, _ in module.cloud_run.service_ids : k if k == "swarm-api"][0]
  role   = "roles/run.invoker"
  member = "serviceAccount:${module.scheduler.rollup_sweeper_email}"
}

# The schedule tick (docs/schedules.md §2.1, owner decision SD10) calls
# swarm-api directly too, as its own account, so the edge must let it through
# before the app's SCHEDULE_TICK_ROUTES check is reached -- the same reason,
# and the same grant, as rollup_sweeper_invokes_api above. run.invoker on this
# one service is the account's only grant (held by
# tests/terraform/schedule_tick_identity.tftest.hcl); the route allow-list
# narrows it to POST /v1/admin/schedules/tick from there.
resource "google_cloud_run_v2_service_iam_member" "schedule_tick_invokes_api" {
  project  = var.project_id
  location = var.region
  name     = [for k, _ in module.cloud_run.service_ids : k if k == "swarm-api"][0]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${module.scheduler.schedule_tick_email}"
}

# The external front door: an ALB with IAP in front of swarm-api.
#
# Gated by a flag rather than by the presence of a hostname, for the reason the
# scheduler module's header already records: a `count` that depends on a value
# unknown until apply cannot be planned at all.
module "frontend" {
  count  = var.enable_frontend ? 1 : 0
  source = "../modules/frontend"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix

  project_number  = data.google_project.this.number
  service_name    = "swarm-api"
  ui_service_name = "swarm-ui"
  hostname        = var.frontend_hostname

  # The unauthenticated /.well-known/swarmcloud backend (discovery.tf); "" when
  # swarm-discovery is not deployed, which makes no backend and no path rule.
  discovery_service_name = local.discovery_on ? local.discovery_name : ""

  labels = local.labels

  # NOT module.cloud_run. Nothing here reads a Cloud Run output: the services
  # are named by string, and a serverless NEG names its service rather than
  # needing it to exist. swarm-api's environment reads this module's
  # iap_audiences (locals.tf IAP_AUDIENCES), so waiting for module.cloud_run
  # was the cycle that made the audiences a value copied by hand out of
  # `terraform output` and back into tfvars (docs/design/day-one-install.md
  # §5.2).
  depends_on = [module.project_services]
}

module "monitoring" {
  source = "../modules/monitoring"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
  environment = var.environment

  service_names            = local.control_plane_services
  wake_subscription        = module.scheduler.wake_subscription
  dead_letter_subscription = "${module.scheduler.dead_letter_topic}-sub"
  safety_tick_job          = "${var.name_prefix}-scheduler-tick"
  enable_safety_tick_alert = var.enable_safety_tick_alert

  # The same string gke_autopilot is given, named from configuration because
  # that module is behind a `count` (as for workspace_deployer above).
  gke_cluster_name = "${var.name_prefix}-autopilot"

  alert_emails                 = var.alert_emails
  extra_notification_channels  = var.extra_notification_channels
  urgent_notification_channels = var.urgent_notification_channels
  require_alert_channels       = var.require_alert_channels
  create_alerts                = var.create_alerts

  # The declared tenants, whose forge slots the workspace job must never bind.
  tenant_ids = local.tenant_ids

  # Release acceptance's tenant and swarm-verify's own: the declared tenant
  # whose principal is the verify account. Read from var.tenants, which holds
  # the id swarm_common.identity derived, rather than restating that rule here
  # or naming one deployment's id (DAYONE-TF-1, plan R9). The account's address
  # is built from its id, not read off google_service_account.verify, so it is
  # known at plan on a fresh project too.
  fencing_alert_excluded_tenants = concat(["smoke"], sort([
    for t, v in var.tenants : t
    if lower(v.principal) == "${module.service_account_ids.verify_id}@${var.project_id}.iam.gserviceaccount.com"
  ]))

  labels = local.labels

  depends_on = [module.project_services]
}

# ---------------------------------------------------------------------------
# The account pool's and the child routes' addresses, computed
# ---------------------------------------------------------------------------
#
# The scheduler hands QUOTA_BROKER_URL and SWARM_API_URL to every worker it
# dispatches, and swarm-api proxies the account pool to the broker. Those
# environments are INPUTS to module.cloud_run, and a service's `uri` is an
# OUTPUT of it, so reading the uri there is a cycle terraform refuses to plan.
# Until DAYONE-TF-1 the way round was a hand copy: apply, read `terraform
# output`, paste into tfvars, apply again, with a `check` per value to catch a
# stale paste.
#
# Cloud Run also serves every service at a DETERMINISTIC URL,
# https://<service>-<project number>.<region>.run.app
# (https://cloud.google.com/run/docs/triggering/https-request), and the
# project number is read here already (data.google_project.this). So both are
# built, and the copy, the second pass and the two checks are gone
# (docs/design/day-one-install.md §5.2).
#
# Audiences do not move with this: every token minted for these services
# names a custom audience (local.push_audiences), never the URL.
#
# To be proven in P3 on the throwaway project of D22 (the design's own words):
# that the existing smoke reaches both services at this address, internal
# ingress included. The fallback is the design's: the engine reads
# `terraform output quota_broker_url` after pass 1 and writes it for pass 2.
locals {
  quota_broker_url = "https://swarm-quota-broker-${data.google_project.this.number}.${var.region}.run.app"
  swarm_api_url    = "https://swarm-api-${data.google_project.this.number}.${var.region}.run.app"
}
