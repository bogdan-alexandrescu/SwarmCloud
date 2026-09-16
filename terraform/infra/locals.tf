# ---------------------------------------------------------------------------
# The execution catalogue, mirrored from the frozen Python contract.
#
# swarm_common.profiles is authoritative. Terraform cannot import it, so these
# maps restate it -- and tests/terraform/catalogue.tftest.hcl asserts the
# restatement still matches. If the two ever drift, the symptom is a Job
# resource sized for a profile the worker is not running, which is exactly the
# class of bug nobody finds by reading.
# ---------------------------------------------------------------------------

locals {
  labels = merge(
    {
      # destroy.sh ABORTS if a plan would delete anything without this label.
      "managed-by" = "swarm-terraform"
      "swarm-env"  = var.environment
    },
    var.extra_labels,
  )

  # --- swarm_common.profiles.RESOURCE_CLASSES ------------------------------
  #
  # `disk_gib` is a slice OF `memory_gib`, not capacity on top of it. These once
  # read 20/40/100 GiB of disk-backed ephemeral storage, which the Terraform
  # google provider cannot express: empty_dir.medium accepts only "MEMORY". The
  # workspace is therefore a tmpfs and comes out of the container's memory.
  #
  # Keep this table byte-identical to swarm_common.profiles.RESOURCE_CLASSES;
  # tests/terraform/catalogue.tftest.hcl reads the Python at test time and fails
  # on drift, which is how this mismatch was caught.
  resource_classes = {
    standard = { cpu = 4, memory_gib = 8, disk_gib = 4, units = 1 }
    browser  = { cpu = 8, memory_gib = 16, disk_gib = 8, units = 2 }
    large    = { cpu = 8, memory_gib = 32, disk_gib = 16, units = 4 }
  }

  # --- swarm_common.profiles.RUNNER_PROFILES -------------------------------
  #
  # `secret_env` maps an environment variable NAME to a provider id. Neither is
  # a credential: the value is the string "anthropic" or "openai", and the real
  # key lives only in Secret Manager, added out of band. No secret material
  # exists anywhere in this repository.
  # checkov:skip=CKV_SECRET_6:env-var name to provider id, not a credential -- see above.
  runner_profiles = {
    "mock" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = null
      secret_env      = {}
      timeout_seconds = 600
    }
    "generic" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = null
      secret_env      = {}
      timeout_seconds = 3600
    }
    "claude-code" = {
      image          = "agent-runtime-base"
      resource_class = "standard"
      backend        = "CLOUD_RUN_JOB"
      provider       = "anthropic"
      # checkov:skip=CKV_SECRET_6:an env-var name mapped to a provider id, not a credential. The value is literally the string "anthropic"; the key lives only in Secret Manager.
      secret_env      = { ANTHROPIC_API_KEY = "anthropic" }
      timeout_seconds = 7200
    }
    "codex" = {
      image           = "agent-runtime-base"
      resource_class  = "standard"
      backend         = "CLOUD_RUN_JOB"
      provider        = "openai"
      secret_env      = { OPENAI_API_KEY = "openai" }
      timeout_seconds = 7200
    }
    "browser" = {
      image          = "agent-runtime-browser"
      resource_class = "browser"
      # Chromium needs a large /dev/shm, which GKE lets us control directly.
      backend         = "GKE_AUTOPILOT"
      provider        = "anthropic"
      secret_env      = { ANTHROPIC_API_KEY = "anthropic" }
      timeout_seconds = 5400
    }
  }

  backends = ["CLOUD_RUN_JOB", "GKE_AUTOPILOT"]

  providers_in_catalogue = distinct(compact([
    for name, p in local.runner_profiles : p.provider
  ]))

  # --- naming --------------------------------------------------------------
  control_plane_services = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]

  # Derived here rather than read back from the scheduler module: the API needs
  # the topic name in its environment, and the scheduler module needs the API's
  # service account, so one of the two dependencies has to be a plain string.
  wake_topic = "${var.name_prefix}-scheduler-wake"

  image_base = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}"

  tenant_ids = keys(var.tenants)

  # Resolved once, used by both the tenant slot pool and the tenant document, so
  # the ceiling Firestore records and the ceiling admission enforces cannot
  # disagree. `max_active` is nullable on purpose (see variables.tf): without
  # that, this coalesce has nothing to fall through to and
  # `pool_limits.default_tenant` is dead configuration that both tfvars files
  # set as though it worked.
  tenant_max_active = {
    for t, cfg in var.tenants :
    t => coalesce(cfg.max_active, var.pool_limits.default_tenant)
  }

  # Per-tenant secret administrators, falling back to the platform list. The
  # fallback may not name any tenant's own principal -- var.secret_admin_members
  # validates that -- because it is applied to every tenant that declares none.
  tenant_secret_admins = {
    for t, cfg in var.tenants :
    t => length(cfg.secret_admins) > 0 ? cfg.secret_admins : var.secret_admin_members
  }
}

# ---------------------------------------------------------------------------
# Slot pools.
#
# One document per pool name swarm_common.models.pool_names_for() can produce.
# Admission is all-or-nothing across the whole list for a task (invariant 2), so
# a pool that is missing is not "unlimited by intent" -- it is a ceiling nobody
# chose. Every name the catalogue can generate is materialised here.
# ---------------------------------------------------------------------------

locals {
  pool_global = {
    "global" = { hard_limit = var.pool_limits.global }
  }

  pool_tenants = {
    for t, cfg in var.tenants :
    "tenant:${t}" => { hard_limit = local.tenant_max_active[t] }
  }

  pool_resource_classes = {
    for name, rc in local.resource_classes :
    "resource:${name}" => {
      hard_limit = lookup(var.pool_limits.resource_classes, name, var.pool_limits.global)
    }
  }

  pool_runner_profiles = {
    for name, p in local.runner_profiles :
    "runner:${name}" => {
      hard_limit = lookup(var.pool_limits.runner_profiles, name, var.pool_limits.global)
    }
  }

  pool_backends = {
    for b in local.backends :
    "backend:${b}" => {
      hard_limit = lookup(var.pool_limits.backends, b, var.pool_limits.global)
    }
  }

  pool_providers = {
    for p in local.providers_in_catalogue :
    "provider:${p}" => {
      hard_limit = lookup(var.pool_limits.providers, p, var.pool_limits.global)
    }
  }

  # Provider quota is tracked per tenant because tenants bring their own keys:
  # one tenant's 429 must never throttle another's.
  pool_provider_tenants = {
    for pair in flatten([
      for t, cfg in var.tenants : [
        for p in cfg.providers : {
          name  = "provider:${p}:tenant:${t}"
          limit = var.pool_limits.provider_tenant
        }
      ]
    ]) : pair.name => { hard_limit = pair.limit }
  }

  pools = merge(
    local.pool_global,
    local.pool_tenants,
    local.pool_resource_classes,
    local.pool_runner_profiles,
    local.pool_backends,
    local.pool_providers,
    local.pool_provider_tenants,
  )
}

# ---------------------------------------------------------------------------
# Cloud Run Job resources, one per (tenant, Cloud-Run-backed profile).
#
# A profile that needs a provider is only materialised for tenants that hold a
# credential for it. Creating the rest would produce Job resources whose every
# execution fails at start on a missing secret, when the correct behaviour is
# for the task to PARK as CREDENTIAL_MISSING and cost nothing.
# ---------------------------------------------------------------------------

locals {
  job_matrix = flatten([
    for tenant_id, cfg in var.tenants : [
      for profile_name, profile in local.runner_profiles : {
        key             = "${var.name_prefix}-job-${tenant_id}-${profile_name}"
        tenant_id       = tenant_id
        runner_profile  = profile_name
        resource_class  = profile.resource_class
        image           = "${local.image_base}/${profile.image}:${var.image_tag}"
        timeout_seconds = profile.timeout_seconds
        secret_env = {
          for env_name, provider in profile.secret_env :
          env_name => "swarm-tenant-${tenant_id}-${provider}"
        }
      }
      if profile.backend == "CLOUD_RUN_JOB" && (
        profile.provider == null || contains(cfg.providers, profile.provider)
      )
    ]
  ])

  jobs = {
    for job in local.job_matrix : job.key => {
      tenant_id             = job.tenant_id
      runner_profile        = job.runner_profile
      resource_class        = job.resource_class
      service_account_email = module.tenancy.worker_service_accounts[job.tenant_id]
      image                 = job.image
      timeout_seconds       = job.timeout_seconds
      secret_env            = job.secret_env

      # Identity of the ATTEMPT -- TASK_ID, ATTEMPT_ID, LEASE_ID, GENERATION --
      # is supplied per execution by the dispatcher, never here. What the
      # environment carries is only which tenant and which profile, because
      # agent_worker.config reads the image and the command from the frozen
      # catalogue by NAME and refuses to take them from the environment at all
      # (invariant 10).
      env = merge(local.common_env, {
        RUNNER_PROFILE = job.runner_profile
        TENANT_ID      = job.tenant_id
        WORKSPACE_ROOT = "/workspace"
      })
    }
  }
}

# ---------------------------------------------------------------------------
# Process environment, mirroring swarm_common.config.Settings.from_env().
# ---------------------------------------------------------------------------

locals {
  common_env = merge({
    PROJECT_ID            = var.project_id
    REGION                = var.region
    ENVIRONMENT           = var.environment
    FIRESTORE_DATABASE    = var.firestore_database
    ARTIFACT_BUCKET       = module.storage.artifact_bucket_name
    ALLOWED_DOMAINS       = join(",", var.allowed_domains)
    GLOBAL_CAPACITY_UNITS = tostring(var.pool_limits.global * 2)
    MAX_ACTIVE_AGENTS     = tostring(var.pool_limits.global)
  }, var.settings_env)

  service_env = {
    "swarm-api" = merge(local.common_env, {
      WAKE_TOPIC = local.wake_topic
    })
    "swarm-scheduler" = merge(local.common_env, {
      WAKE_TOPIC        = local.wake_topic
      GKE_CLUSTER       = var.enable_gke_autopilot ? "${var.name_prefix}-autopilot" : ""
      GKE_LOCATION      = var.region
      # Required for a client running OUTSIDE the cluster. Without both, the GKE
      # backend falls back to load_incluster_config(), which on Cloud Run fails
      # with "Service host/port is not set" on every reconciliation pass.
      GKE_ENDPOINT     = var.enable_gke_autopilot ? try(module.gke_autopilot[0].endpoint, "") : ""
      GKE_CA_CERT_B64  = var.enable_gke_autopilot ? try(module.gke_autopilot[0].ca_certificate, "") : ""
      ARTIFACT_REGISTRY = var.artifact_registry_repository
      IMAGE_BASE        = local.image_base
    })
    "swarm-quota-broker" = local.common_env
    "swarm-reconciler" = merge(local.common_env, {
      GKE_CLUSTER  = var.enable_gke_autopilot ? "${var.name_prefix}-autopilot" : ""
      GKE_LOCATION = var.region
      GKE_ENDPOINT    = var.enable_gke_autopilot ? try(module.gke_autopilot[0].endpoint, "") : ""
      GKE_CA_CERT_B64 = var.enable_gke_autopilot ? try(module.gke_autopilot[0].ca_certificate, "") : ""
    })
  }
}
