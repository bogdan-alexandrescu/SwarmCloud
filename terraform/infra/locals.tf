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
      # Both names map to the SAME tenant secret, and that is deliberate: a
      # tenant holds one credential per provider and it is either metered API
      # access or a Claude subscription token from `claude setup-token`. The
      # worker picks the right variable from the value's shape
      # (agent_worker.runners.cliagent._credential_env) -- `sk-ant-oat...` is a
      # subscription token, `sk-ant-api...` is a key -- and passes only that one
      # to the CLI. Projecting just one name would force a tenant who already
      # pays for a subscription to buy metered access as well.
      # checkov:skip=CKV_SECRET_6:env-var names mapped to a provider id, not credentials. The values are literally the string "anthropic"; the credential lives only in Secret Manager.
      secret_env = {
        ANTHROPIC_API_KEY       = "anthropic"
        CLAUDE_CODE_OAUTH_TOKEN = "anthropic"
      }
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
      # QUOTA_BROKER_URL is what makes the account pool exist at runtime.
      # Without it `WorkerConfig.quota_broker_url` is None, the worker builds
      # no AccountBroker, and every agent runs on the one shared per-tenant
      # secret -- the exact contention the pool was built to remove -- with
      # nothing anywhere saying so.
      #
      # DERIVED HERE, and it can be: this module reads module.cloud_run's
      # outputs and nothing in module.cloud_run reads local.jobs, so there is
      # no cycle. The same value cannot be derived for the SCHEDULER's own
      # environment, which lives inside module.cloud_run and would have to read
      # a URL that is an attribute of the resource it is part of -- see
      # var.quota_broker_url and the check block in main.tf.
      #
      # The dispatcher's per-execution overrides MERGE with this, and
      # `worker_env()` omits the name entirely when the scheduler has no URL,
      # so a Cloud Run Job keeps the value below either way.
      env = merge(local.common_env, {
        RUNNER_PROFILE = job.runner_profile
        TENANT_ID      = job.tenant_id
        WORKSPACE_ROOT = "/workspace"

        QUOTA_BROKER_URL = module.cloud_run.service_urls["swarm-quota-broker"]
        # The broker declares a custom audience, so a token minted for the
        # service URL alone is one its own `aud` check rejects.
        QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]
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

  # A Cloud Run service's OIDC audience is normally its own URL -- which cannot
  # be written into that same service's environment, because the URL is an
  # attribute of the resource the environment is part of. Terraform reports that
  # honestly as a cycle, and the way it was resolved was to not set the variable
  # at all, leaving both audience checks inert.
  #
  # A custom audience is the documented way out: a literal string known before
  # apply, accepted by Cloud Run IN ADDITION to the service URL, and minted by
  # the Pub/Sub and Cloud Scheduler OIDC tokens. Both sides then name the same
  # constant and nothing needs to know a URL.
  push_audiences = {
    for name in ["swarm-scheduler", "swarm-quota-broker"] :
    name => "https://${name}.${var.environment}.swarm.internal"
  }

  service_env = {
    "swarm-api" = merge(local.common_env, {
      # DISPATCH_TOPIC, not WAKE_TOPIC. Both apps read `DISPATCH_TOPIC`
      # (swarm_api.settings, scheduler.settings); terraform set `WAKE_TOPIC`,
      # which nothing has ever read. swarm_api.deps falls back to NullWaker()
      # when the value is empty, so every submission silently skipped the
      # publish and waited for the one-minute Cloud Scheduler safety tick.
      #
      # Nothing failed, which is why it survived: the queue still drained, just
      # up to 60s later than designed, and tail latency is not a thing anyone
      # alerts on. Renaming the env key is the whole fix -- the topic, the IAM
      # and the subscription were always correct.
      DISPATCH_TOPIC = local.wake_topic

      # Neither name appeared anywhere in terraform, so swarm_api.settings read
      # empty tuples, resolve_tenant() had no groups to check, and EVERY caller
      # fell through to the personal `u-<email>` tenant.
      #
      # That fallback is working, legitimate code -- which is exactly why this
      # hid. Every request still succeeded and still got *a* tenant; it was just
      # never the group one. A member of eng@saga.xyz spent their own personal
      # quota and wrote to their own GCS prefix while the shared tenant this
      # file provisions sat unused.
      #
      # Derived from var.tenants rather than restated: a tenant of kind "group"
      # IS a tenant group, and keeping a second list in sync by hand is how the
      # two would drift.
      # Behind the load balancer a browser sends NO Authorization header -- IAP
      # has already authenticated the person and forwards the result in
      # x-goog-iap-jwt-assertion. Without these pinned, swarm-api has nothing to
      # verify that against and answers 401 to every request from the web UI:
      # a signed-in user, a valid certificate, a healthy load balancer, and an
      # API that cannot see any of it.
      #
      # DECLARED, NOT DERIVED, and that is forced rather than chosen. Reading
      # module.frontend's output here is a terraform CYCLE: the frontend module
      # needs the Cloud Run services (for the serverless NEG), and this
      # environment feeds those same services. I wrote "no cycle" in an earlier
      # version of this comment and terraform disagreed, in detail.
      #
      # The audience contains a GCP-GENERATED backend service id, so unlike the
      # push audiences above it cannot be made a constant known before apply.
      # So it is an input: read the value from `terraform output
      # frontend_iap_audiences` after the load balancer exists and put it in
      # tfvars. The ids are stable for the life of the backend service.
      IAP_AUDIENCES = join(",", var.frontend_iap_audiences)

      # `directory_group` as well as `kind`: a tenant can be group-KINDED for
      # naming and isolation while its principal is not a resolvable directory
      # group. Listing one that does not exist 503s every request, not just that
      # tenant's -- see the variable's description.
      TENANT_GROUPS = join(",", sort([
        for t, v in var.tenants : v.principal if v.kind == "group" && v.directory_group
      ]))
      ADMIN_GROUPS            = join(",", sort(var.admin_groups))
      ADMIN_USERS             = join(",", sort(var.admin_users))
      GROUPS_IMPERSONATE_USER = var.groups_impersonate_user

      # The verification job's identity, admitted past the domain check.
      #
      # swarm-api admits a caller through ALLOWED_USERS or through the frozen
      # domain check against allowed_domains (saga.xyz). The swarm-verify job
      # authenticates as swarm-verify@<project>.iam.gserviceaccount.com, whose
      # domain is not saga.xyz and cannot be -- a service account is not a
      # Workspace principal. ALLOWED_USERS exists in settings.py and auth.py
      # for exactly this case and was set by no .tf and no .tfvars, so the
      # in-VPC gate's every request was answered 403 by design.
      #
      # This widens WHICH verified identities are admitted, never whether an
      # identity was verified: auth.py:331 consults this only after the token
      # has been checked, and the address it compares comes from that token.
      ALLOWED_USERS = google_service_account.verify.email

      # The same principals terraform already refuses to let own a DECLARED
      # tenant, handed to the runtime so it can refuse the ones terraform
      # cannot see.
      #
      # variables.tf validates secret_admin_members against var.tenants, and
      # that is the whole check -- but Store.ensure_tenant creates a
      # self-service tenant for any allowed-domain caller on first sight, and
      # var.tenants does not contain those. On 2026-09-21 admin@saga.xyz signed
      # in to the web UI and became the principal of tenant u-admin.
      #
      # The `user:`/`group:` prefix is stripped because swarm-api compares
      # against a bare principal (an email), which is what both
      # tenant_id_for_group and tenant_id_for_user are given.
      SECRET_ADMIN_PRINCIPALS = join(",", sort([
        for m in var.secret_admin_members : replace(replace(m, "user:", ""), "group:", "")
      ]))

      # swarm-api PROXIES every account-pool call to the broker and never
      # writes a subscription credential itself, so without this the Settings
      # page cannot list or register an account at all.
      #
      # DECLARED, NOT DERIVED, for exactly the reason the scheduler's copy is
      # (see var.quota_broker_url): this environment is an input to
      # module.cloud_run and the broker's URL is an output of it, so
      # referencing it here is a cycle terraform refuses to plan. The worker
      # JOBS can derive it because they live outside that module.
      #
      # Unlike the silent failure the scheduler's comment warns about, an unset
      # value here is LOUD: BrokerClient refuses to start and /v1/accounts
      # answers 503 naming this variable. That is deliberate -- an account pool
      # that half-exists is worse than one that is plainly absent -- and it is
      # what the deployed API returned before this line was added.
      QUOTA_BROKER_URL      = var.quota_broker_url
      QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]
    })
    "swarm-scheduler" = merge(local.common_env, {
      # See the swarm-api block: the reader has always been DISPATCH_TOPIC.
      DISPATCH_TOPIC = local.wake_topic

      # PushVerifier is the second auth layer in front of /v1/push: Cloud Run
      # IAM proves the caller is authorised, this proves WHICH caller it is.
      # Neither name was ever set, so the verifier constructed itself disabled
      # and the comment in scheduler/main.py asserted a control that was not
      # running. The audience matters independently: google-auth SKIPS the
      # `aud` claim entirely when none is passed, so without it a token the
      # tick account minted for ANY other service was accepted here.
      PUSH_SERVICE_ACCOUNT = module.iam.tick_service_account
      PUSH_AUDIENCE        = local.push_audiences["swarm-scheduler"]

      GKE_CLUSTER  = var.enable_gke_autopilot ? "${var.name_prefix}-autopilot" : ""
      GKE_LOCATION = var.region
      # Required for a client running OUTSIDE the cluster. Without both, the GKE
      # backend falls back to load_incluster_config(), which on Cloud Run fails
      # with "Service host/port is not set" on every reconciliation pass.
      GKE_ENDPOINT    = var.enable_gke_autopilot ? try(module.gke_autopilot[0].endpoint, "") : ""
      GKE_CA_CERT_B64 = var.enable_gke_autopilot ? try(module.gke_autopilot[0].ca_certificate, "") : ""
      # ARTIFACT_REGISTRY_HOST, not ARTIFACT_REGISTRY or IMAGE_BASE. The
      # scheduler reads only this one (scheduler/settings.py), and the other two
      # names were read by nothing at all.
      #
      # This one was WORKING, which is the interesting part. Unset, the
      # scheduler falls back to building the host itself out of
      # `swarm_common.config.Settings.artifact_registry` -- a dataclass default
      # of "swarm-images" that is never read from the environment -- and
      # var.artifact_registry_repository also defaults to "swarm-images", so the
      # two agreed and every dispatch happened to pull the right image.
      #
      # `artifact_registry_repository`'s own description says "Must match
      # swarm_common.config.Settings.artifact_registry", and nothing enforced
      # that. Changing the repository name in tfvars would have silently sent
      # every dispatch at a registry path that does not exist, and the symptom
      # would have been a 404 on the image, pointing at the build rather than at
      # the variable. Passing the value makes it flow instead of coincide.
      ARTIFACT_REGISTRY_HOST = local.image_base
      # The agent runtime images carry the same immutable git-SHA tag as the
      # control plane. Without this the dispatcher asked for ":latest", which
      # scripts/build-images.sh never pushes, so every dispatch failed with
      # `404 Image '...agent-runtime-base:latest' not found` and the task was
      # left holding a lease.
      WORKER_IMAGE_TAG = var.image_tag

      # Passed THROUGH to each worker by `scheduler.dispatch.worker_env`, which
      # is the single source of a worker's execution environment for both
      # backends. The GKE path has no terraform-managed Job to carry it, so
      # without this every browser/GPU worker runs with no pool.
      #
      # DECLARED, NOT DERIVED, and that is forced rather than chosen -- exactly
      # like IAP_AUDIENCES above. This environment is an input to
      # module.cloud_run, and the broker's URL is an output of it; referencing
      # it here is a cycle terraform refuses to plan. So: apply once, read
      # `terraform output quota_broker_url`, put it in tfvars. The check block
      # in main.tf fails loudly while this is empty or stale, because the
      # failure it prevents -- a pool that is configured everywhere except in
      # the one place that matters -- is completely silent at runtime.
      QUOTA_BROKER_URL      = var.quota_broker_url
      QUOTA_BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]
    })
    "swarm-quota-broker" = merge(local.common_env, {
      # WITHOUT THIS THE SWEEP HAS NEVER RUN. /v1/quota/sweep requires a
      # platform caller, the broker derives "platform" from this list, and the
      # list was empty -- so the Cloud Scheduler tick got 403 every five
      # minutes, silently, since the day it was created. Verified in the logs
      # before the fix: an unbroken run of 403s on /v1/quota/sweep.
      #
      # The visible cost was the AIMD quota state never being recomputed and
      # PARKED tenants never being un-parked. The refresher for subscription
      # credentials runs on the same tick, so it would have been dead on
      # arrival too, and its symptom -- credentials quietly stopping -- points
      # nowhere near the cause.
      #
      # The Cloud Scheduler tick, and swarm-api. NOT workers: they report quota
      # as their own tenant, and being on this list would let any one of them
      # set another tenant's hard max.
      #
      # swarm-api is here because it PROXIES the account-pool routes for a
      # browser, so it has to be able to act for whichever tenant the signed-in
      # caller resolved to -- it is not itself a tenant, and the broker's
      # WorkerIdentity derives a tenant from the caller's service account name,
      # which for swarm-api yields "caller is not a swarm worker service
      # account" and a 403 on every account request.
      #
      # This is safe only because swarm-api resolves the caller's tenant ITSELF,
      # through `ctx.submissions.tenant_for(auth)`, and sends that resolved
      # value as `owner_tenant` -- it never forwards a tenant the browser
      # supplied. swarm-api is the tenant boundary for these routes; the broker
      # is the single writer. If that ever stops being true, this line is the
      # one that turns it into a cross-tenant hole.
      PLATFORM_SERVICE_ACCOUNTS = join(",", [
        module.iam.tick_service_account,
        module.iam.service_account_emails["swarm-api"],
      ])

      # Provisioning an account's two secrets, which moved here when account
      # management became a Settings page rather than a shell script. A pool
      # account's secret name contains a LABEL chosen at registration time, so
      # terraform cannot declare it in advance and the component handling the
      # registration has to create it.
      BROKER_SERVICE_ACCOUNT = module.iam.service_account_emails["swarm-quota-broker"]

      # A TEMPLATE, not a pattern rebuilt in Python. modules/tenancy owns what a
      # tenant's service account is called -- `swarm-agent-worker-<tenant>`, with
      # an 11-character tenant limit because of the 30-character GCP cap -- and a
      # second copy of that rule inside the broker would be correct until the day
      # it was not. The symptom then is a pod that cannot read the credential it
      # was assigned, surfacing inside a job, nowhere near the cause.
      #
      # `{tenant}` is substituted by the broker. An unset value makes the
      # register route REFUSE rather than create a secret no pod can read.
      WORKER_SERVICE_ACCOUNT_TEMPLATE = "swarm-agent-worker-{tenant}@${var.project_id}.iam.gserviceaccount.com"

      # WorkerIdentity refuses to start without this when `hardened`, for the
      # same reason PUSH_AUDIENCE exists: no audience means google-auth does not
      # check `aud` at all. It was never set, and `hardened` is driven by
      # ENVIRONMENT -- which is "dev" for saga-agents-staging, a project that is
      # production-shaped in every way except that label. So the guard written
      # to make this impossible was itself disarmed in the only place it ran.
      BROKER_AUDIENCE = local.push_audiences["swarm-quota-broker"]
    })
    "swarm-reconciler" = merge(local.common_env, {
      # No GKE_CLUSTER/GKE_LOCATION here: the reconciler talks to the cluster
      # through the endpoint and CA bundle directly (reconciler/backends.py) and
      # has never read either name. The scheduler does read them; that is why
      # they are set there and not here.
      GKE_ENDPOINT    = var.enable_gke_autopilot ? try(module.gke_autopilot[0].endpoint, "") : ""
      GKE_CA_CERT_B64 = var.enable_gke_autopilot ? try(module.gke_autopilot[0].ca_certificate, "") : ""
    })
  }
}
