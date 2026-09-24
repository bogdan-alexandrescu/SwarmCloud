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
    "containerscanning.googleapis.com",
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
  ]
}

module "network" {
  source = "../modules/network"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix

  subnet_cidr         = var.subnet_cidr
  pods_cidr           = var.pods_cidr
  services_cidr       = var.services_cidr
  gke_master_cidr     = var.gke_master_cidr
  nat_static_ip_count = var.nat_static_ip_count
  restrict_egress     = var.restrict_egress

  labels = local.labels

  depends_on = [module.project_services]
}

module "artifact_registry" {
  source = "../modules/artifact_registry"

  project_id     = var.project_id
  region         = var.region
  repository_id  = var.artifact_registry_repository
  immutable_tags = var.immutable_image_tags

  custom_role_suffix = var.custom_role_suffix

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

  delete_protection = var.deletion_protection
  deletion_policy   = "ABANDON"

  bootstrap_documents = var.bootstrap_firestore_documents
  pools               = local.pools

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
  custom_role_suffix = var.custom_role_suffix

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
  custom_role_suffix = var.custom_role_suffix

  workload_identity_pool = var.enable_gke_autopilot ? module.gke_autopilot[0].workload_identity_pool : ""

  tenants = var.tenants

  # Only the dispatcher and the reconciler may name a tenant SA on a Job.
  dispatcher_members = {
    scheduler  = module.iam.service_account_members["swarm-scheduler"]
    reconciler = module.iam.service_account_members["swarm-reconciler"]
  }

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

  # One writer, and this is it. See the variable's own description, and
  # quota_broker.credentials, for why a second one corrupts a rotating
  # credential rather than merely duplicating work.
  enable_subscription_refresh = true
  refresher_member            = module.iam.service_account_members["swarm-quota-broker"]

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
      cpu                   = "1"
      memory                = "1Gi"
      # The ONLY control-plane service that calls another one. It proxies every
      # account-pool request to the quota broker, whose ingress is
      # internal-and-cloud-load-balancing; under PRIVATE_RANGES_ONLY the
      # broker's public hostname is not a private range, so the call leaves the
      # VPC, arrives as external traffic and is refused with an HTML 404. The
      # worker jobs already run ALL_TRAFFIC, which is why they could reach the
      # broker and this could not.
      vpc_egress  = "ALL_TRAFFIC"
      concurrency = 80
      env         = local.service_env["swarm-api"]
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
      invokers = merge(
        { for member in var.api_invokers : member => member },
        { iap = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com" },
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
      concurrency      = 1
      request_timeout  = "540s"
      env              = local.service_env["swarm-scheduler"]
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
      # Keyed by tenant id rather than by the member string, for the reason the
      # cloud_run module's own comment gives: a service account email is not
      # known until apply and cannot appear in a for_each key.
      invokers = merge(
        {
          tick      = module.iam.tick_member
          scheduler = module.iam.service_account_members["swarm-scheduler"]
          api       = module.iam.service_account_members["swarm-api"]
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

  # The API publishes a wake message on submission; the reconciler republishes
  # when it returns reclaimed work to READY.
  publisher_members = {
    api          = module.iam.service_account_members["swarm-api"]
    reconciler   = module.iam.service_account_members["swarm-reconciler"]
    quota_broker = module.iam.service_account_members["swarm-quota-broker"]
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

  labels = local.labels

  depends_on = [module.project_services, module.cloud_run]
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

  alert_emails                = var.alert_emails
  extra_notification_channels = var.extra_notification_channels
  create_alerts               = var.create_alerts

  labels = local.labels

  depends_on = [module.project_services]
}

# ---------------------------------------------------------------------------
# The account pool is wired, or terraform says so
# ---------------------------------------------------------------------------
#
# The failure this exists to prevent is completely silent at runtime. With
# QUOTA_BROKER_URL unset on the scheduler, `WorkerConfig.quota_broker_url` is
# None, `Worker.__init__` builds no AccountBroker, and `_lease_account` returns
# None on its first branch for every task -- so every agent runs on the one
# shared per-tenant subscription, which is the exact contention the pool was
# built to remove. Nothing fails. Nothing logs. The pool's own listing shows a
# healthy, idle set of accounts, because it is idle.
#
# A `check` and not a `precondition`, deliberately: on a fresh project the
# broker does not exist yet, so the value cannot be known on the first apply
# and a hard failure would make the configuration unbootstrappable. A check
# reports on every plan and apply without blocking either, which is the right
# shape for "apply once, read the output, put it in tfvars" -- the same
# two-step frontend_iap_audiences already uses.
#
# ONLY THE MISMATCH IS ASSERTED HERE, and the emptiness is not, which is not an
# oversight. `terraform test` treats a failed check as a failed run, and the
# suite in tests/terraform plans this root with no tfvars at all -- so an
# assertion that the variable is set would fail every one of those runs for a
# value they have no reason to supply. The "this deployment is silently inert"
# alarm therefore lives where it can tell the difference between "no pool" and
# "a pool nobody is using": the broker's own sweep warns when accounts are
# registered and nothing has ever been assigned from them.
check "quota_broker_url_is_wired" {
  assert {
    condition     = var.quota_broker_url == "" || var.quota_broker_url == module.cloud_run.service_urls["swarm-quota-broker"]
    error_message = "quota_broker_url does not match the deployed swarm-quota-broker URL. A stale value is worse than an empty one: a request to it is refused rather than unanswered, and a worker that is refused PARKS every task rather than falling back. Run `terraform output quota_broker_url` and update tfvars."
  }
}
