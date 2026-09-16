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

  # Every identity that pulls an image. Workers pull their runner image; the
  # control plane pulls its own.
  readers = concat(
    [for c in local.control_plane_services : "serviceAccount:${module.iam.service_account_emails[c]}"],
    [for t, email in module.tenancy.worker_service_accounts : "serviceAccount:${email}"],
  )

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
      max_active      = cfg.max_active
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

  network             = module.network.network_self_link
  subnetwork          = module.network.subnetwork_self_link
  pods_range_name     = module.network.pods_range_name
  services_range_name = module.network.services_range_name

  master_ipv4_cidr_block  = var.gke_master_cidr
  release_channel         = var.gke_release_channel
  enable_private_endpoint = var.gke_enable_private_endpoint
  master_authorized_cidrs = var.gke_master_authorized_cidrs
  deletion_protection     = var.deletion_protection

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
  dispatcher_members = [
    module.iam.service_account_members["swarm-scheduler"],
    module.iam.service_account_members["swarm-reconciler"],
  ]

  labels = local.labels
}

module "secret_manager" {
  source = "../modules/secret_manager"

  project_id = var.project_id
  region     = var.region

  tenant_secrets = {
    for t, cfg in module.tenancy.secret_inputs : t => {
      providers     = cfg.providers
      accessor      = cfg.accessor
      admin_members = var.secret_admin_members
    }
  }

  labels = local.labels

  depends_on = [module.project_services]
}

module "cloud_run" {
  source = "../modules/cloud_run"

  project_id = var.project_id
  region     = var.region

  network    = module.network.network_self_link
  subnetwork = module.network.subnetwork_self_link

  deletion_protection = var.deletion_protection

  services = {
    "swarm-api" = {
      service_account_email = module.iam.service_account_emails["swarm-api"]
      image                 = "${local.image_base}/swarm-api:${var.image_tag}"
      max_instances         = var.service_max_instances["swarm-api"]
      cpu                   = "1"
      memory                = "1Gi"
      concurrency           = 80
      env                   = local.service_env["swarm-api"]
      invokers              = var.api_invokers
    }
    "swarm-scheduler" = {
      service_account_email = module.iam.service_account_emails["swarm-scheduler"]
      image                 = "${local.image_base}/swarm-scheduler:${var.image_tag}"
      max_instances         = var.service_max_instances["swarm-scheduler"]
      cpu                   = "2"
      memory                = "2Gi"
      # The drain loop is a single serialised pass over admissible work.
      # Concurrent requests on one instance would have them contend on the same
      # Firestore documents and abort each other.
      concurrency     = 1
      request_timeout = "540s"
      env             = local.service_env["swarm-scheduler"]
      invokers        = [module.iam.tick_member]
    }
    "swarm-quota-broker" = {
      service_account_email = module.iam.service_account_emails["swarm-quota-broker"]
      image                 = "${local.image_base}/swarm-quota-broker:${var.image_tag}"
      max_instances         = var.service_max_instances["swarm-quota-broker"]
      cpu                   = "1"
      memory                = "512Mi"
      concurrency           = 40
      env                   = local.service_env["swarm-quota-broker"]
      invokers = [
        module.iam.tick_member,
        module.iam.service_account_members["swarm-scheduler"],
        module.iam.service_account_members["swarm-api"],
      ]
    }
    "swarm-reconciler" = {
      service_account_email = module.iam.service_account_emails["swarm-reconciler"]
      image                 = "${local.image_base}/swarm-reconciler:${var.image_tag}"
      max_instances         = var.service_max_instances["swarm-reconciler"]
      cpu                   = "1"
      memory                = "1Gi"
      concurrency           = 1
      request_timeout       = "900s"
      env                   = local.service_env["swarm-reconciler"]
      invokers              = [module.iam.tick_member]
    }
  }

  labels = local.labels

  depends_on = [module.project_services]
}

module "cloud_run_jobs" {
  source = "../modules/cloud_run_jobs"

  project_id = var.project_id
  region     = var.region

  network    = module.network.network_self_link
  subnetwork = module.network.subnetwork_self_link

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
  reconciler_endpoint     = module.cloud_run.service_urls["swarm-reconciler"]
  quota_broker_endpoint   = module.cloud_run.service_urls["swarm-quota-broker"]

  tick_service_account = module.iam.tick_service_account

  # The API publishes a wake message on submission; the reconciler republishes
  # when it returns reclaimed work to READY.
  publisher_members = [
    module.iam.service_account_members["swarm-api"],
    module.iam.service_account_members["swarm-reconciler"],
    module.iam.service_account_members["swarm-quota-broker"],
  ]

  labels = local.labels

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

  alert_emails                = var.alert_emails
  extra_notification_channels = var.extra_notification_channels
  create_alerts               = var.create_alerts

  labels = local.labels

  depends_on = [module.project_services]
}
