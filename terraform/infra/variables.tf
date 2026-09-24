# ---------------------------------------------------------------------------
# Project and environment
# ---------------------------------------------------------------------------

variable "project_id" {
  description = "SHARED project. It holds a live GKE cluster agents-staging, VPC agents-staging-vpc and 12 service accounts owned by other teams."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "name_prefix" {
  description = "Prefix on every resource. `make destroy` is label-scoped, and this is the second line of defence behind the label."
  type        = string
  default     = "swarm"

  validation {
    condition     = startswith(var.name_prefix, "swarm")
    error_message = "name_prefix must start with 'swarm'."
  }
}

variable "extra_labels" {
  description = "Merged on top of the mandatory managed-by label. Cannot override it."
  type        = map(string)
  default     = {}

  validation {
    condition     = !contains(keys(var.extra_labels), "managed-by")
    error_message = "managed-by is set by this configuration and may not be overridden; destroy.sh refuses to delete anything lacking managed-by=swarm-terraform."
  }
}

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

variable "deletion_protection" {
  description = "Protects the Autopilot cluster, the Firestore database and the Cloud Run services from deletion."
  type        = bool
  default     = true

  validation {
    condition     = var.environment != "prod" || var.deletion_protection || var.allow_unprotected_prod
    error_message = "prod requires deletion_protection = true. Set allow_unprotected_prod = true to override deliberately."
  }
}

variable "allow_unprotected_prod" {
  description = "The explicit override for running prod without deletion protection. Nothing sets this implicitly."
  type        = bool
  default     = false
}

variable "bucket_force_destroy" {
  description = "True lets terraform destroy delete artifacts and checkpoints. Refused in prod."
  type        = bool
  default     = false

  validation {
    condition     = var.environment != "prod" || var.bucket_force_destroy == false
    error_message = "bucket_force_destroy must be false in prod: it would let a destroy take every checkpoint with it."
  }
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

variable "subnet_cidr" {
  type    = string
  default = "10.40.0.0/20"
}

variable "pods_cidr" {
  type    = string
  default = "10.44.0.0/14"
}

variable "services_cidr" {
  type    = string
  default = "10.48.0.0/20"
}

variable "gke_master_cidr" {
  type    = string
  default = "172.16.32.0/28"
}

variable "nat_static_ip_count" {
  type    = number
  default = 2
}

variable "restrict_egress" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------------------
# GKE Autopilot (browser / GPU / >32 GiB only)
# ---------------------------------------------------------------------------

variable "enable_gke_autopilot" {
  type    = bool
  default = true
}

variable "gke_release_channel" {
  type    = string
  default = "REGULAR"
}

variable "gke_enable_private_endpoint" {
  type    = bool
  default = false
}

variable "gke_allow_open_master_authorized_network" {
  description = <<-EOT
    Permits 0.0.0.0/0 in gke_master_authorized_cidrs for this environment.

    Default false, which is what keeps prod's control plane closed: prod runs
    gke_enable_private_endpoint = false and relies on an empty allowlist, so the
    refusal of 0.0.0.0/0 is its backstop rather than a formality.
  EOT
  type        = bool
  default     = false
}

variable "gke_master_authorized_cidrs" {
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

variable "firestore_database" {
  type    = string
  default = "swarm"

  validation {
    condition     = var.firestore_database != "(default)"
    error_message = "the (default) database must stay free for other teams in this shared project."
  }
}

variable "bootstrap_firestore_documents" {
  type    = bool
  default = true
}

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

# EVERY IMAGE BY DIGEST, AND NO TAG ANYWHERE.
#
# This replaces `image_tag`, which stamped one tag on every service and job.
# A tag is resolved to a digest when a revision is created (Cloud Run) or when
# an image is pulled (the worker jobs, GKE), so a tag rebuilt to new content
# planned NO change and the old digest kept serving, healthy, through every
# check -- measured on swarm-ui on 2026-09-19, written up in
# docs/audits/2026-09-20/tag-vs-digest.md. A digest changes when the content
# does, so terraform sees the change and the stale revision cannot happen.
#
# Written by scripts/lib/image-refs.sh from the promotion manifest
# (build/deployed-images-<env>.json, which push-images.sh writes only for
# digests that passed the trivy scan), never by hand and never from tfvars.
#
# DEFAULT {} AND STILL REQUIRED. An image with no entry here fails the plan --
# see the precondition on google_cloud_run_v2_job.verify -- rather than falling
# back to anything, because a fallback is how a tag gets deployed without
# anybody choosing it. The default exists only so that a `-target`ed plan of
# the registry, on a fresh project with nothing built yet, can run at all.
variable "image_refs" {
  description = "Image name -> `<region>-docker.pkg.dev/<project>/<repository>/<name>@sha256:<64 hex>`, for every image this root deploys. Written by scripts/lib/image-refs.sh from the promotion manifest."
  type        = map(string)
  default     = {}

  validation {
    # No tag, not even beside a digest: the runtime ignores a tag when a digest
    # is present, so it is only ever a label that can disagree with what runs.
    condition = alltrue([
      for name, ref in var.image_refs : can(regex("^[^@:[:space:]]+@sha256:[0-9a-f]{64}$", ref))
    ])
    error_message = "every image_refs value must be `<registry path>@sha256:<64 hex>` with no tag. A tag is resolved at deploy or pull time, which is the mutability this variable exists to remove."
  }

  validation {
    # The digest of swarm-scheduler under the name swarm-api would plan cleanly
    # and deploy the wrong service's code.
    condition = alltrue([
      for name, ref in var.image_refs : endswith(split("@", ref)[0], "/${name}")
    ])
    error_message = "an image_refs entry names a different image than its key. Each key must be the image's own name, the last path segment before the @."
  }

  validation {
    # Pinned is not the same as ours: only this registry holds images the
    # pipeline built, scanned and promoted, and only it is readable by the
    # pull roles terraform grants.
    condition = alltrue([
      for name, ref in var.image_refs : startswith(ref, "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}/")
    ])
    error_message = "an image_refs entry is not in this environment's Artifact Registry repository. Only images this pipeline built and promoted may be deployed."
  }
}

variable "artifact_registry_repository" {
  description = "Must match swarm_common.config.Settings.artifact_registry."
  type        = string
  default     = "swarm-images"
}

variable "immutable_image_tags" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

variable "tenants" {
  description = <<-EOT
    Tenants provisioned up front. The API creates personal fallback tenants at
    runtime; those are not managed here.

    `max_active` is deliberately NOT defaulted here. A default would make
    `pool_limits.default_tenant` unreachable -- coalesce() never sees a null --
    and an operator who added a tenant without stating a ceiling would silently
    get the type's default instead of the environment's. Left null, the tenant
    pool falls through to pool_limits.default_tenant, which is what both tfvars
    files are written as though it does.
  EOT
  type = map(object({
    kind      = string
    principal = string
    # Whether `principal` is a group that actually EXISTS in the directory and
    # that swarm-api can resolve membership for.
    #
    # Separate from `kind` because they answer different questions. `kind`
    # decides how the tenant id is derived and how its namespace and service
    # account are named; this decides whether the group is put in TENANT_GROUPS
    # for swarm-api to check at sign-in.
    #
    # It exists because those came apart on 2026-09-19. `smoke` was declared
    # kind = "group" with principal swarm-smoke@saga.xyz, and no such group had
    # ever been created. A FAILED GROUP LOOKUP IS FATAL BY DESIGN -- a
    # higher-priority group being unknown could file a caller's work under the
    # wrong tenant -- so that one phantom group made every authenticated request
    # to the API answer 503, for every user, including ones in groups that do
    # exist.
    directory_group = optional(bool, true)
    display_name    = optional(string, "")
    providers       = optional(list(string), [])
    max_active      = optional(number)
    capacity_units  = optional(number, 40)
    ksa_name        = optional(string, "swarm-agent-worker")
    # Identities allowed to add a version to THIS tenant's provider-key secrets.
    # Empty falls back to var.secret_admin_members, which must be a platform
    # admin group rather than any tenant's own group -- see the validation there.
    secret_admins = optional(list(string), [])
  }))
  default = {}

  validation {
    condition     = alltrue([for t, v in var.tenants : v.max_active == null || v.max_active > 0])
    error_message = "a tenant's max_active must be positive; omit it to take pool_limits.default_tenant."
  }
}

variable "enable_safety_tick_alert" {
  description = "Create the safety-tick-stopped alert. Requires the Cloud Scheduler metric to already exist in the project; see the module variable of the same name."
  type        = bool
  default     = true
}

variable "enable_frontend" {
  description = <<-EOT
    Build the external load balancer and IAP in front of swarm-api.

    A flag rather than `frontend_hostname != ""`, because a count that depends
    on a value unknown at plan time cannot be planned -- the same reason
    enable_quota_refresh exists.
  EOT
  type        = bool
  default     = false
}

variable "frontend_hostname" {
  description = "The name the managed certificate is issued for. DNS lives at an external registrar, so the A record is added by hand from the module's ip_address output."
  type        = string
  default     = ""
}

variable "frontend_iap_audiences" {
  description = <<-EOT
    Audiences swarm-api accepts on an IAP assertion, one per backend service:

        /projects/<PROJECT NUMBER>/global/backendServices/<BACKEND SERVICE ID>

    Declared rather than derived because deriving it from the frontend module is
    a terraform cycle -- that module consumes the Cloud Run services this value
    configures. Read it from `terraform output frontend_iap_audiences` once the
    load balancer exists.

    EMPTY MEANS THE IAP PATH IS OFF, not "accept any audience". An unpinned
    audience makes google-auth skip the `aud` check, and an IAP assertion is
    issued to anyone who can reach any IAP-protected resource anywhere.
  EOT
  type        = list(string)
  default     = []
}

variable "quota_broker_url" {
  description = <<-EOT
    Base URL of the swarm-quota-broker Cloud Run service, for the SCHEDULER's
    environment. The scheduler never calls the broker; it passes this through
    to every worker it dispatches (scheduler.dispatch.worker_env), and a worker
    with no QUOTA_BROKER_URL uses no account pool at all.

    Declared rather than derived, for the same reason frontend_iap_audiences
    is: the scheduler's environment is an INPUT to the Cloud Run module and the
    broker's URL is an OUTPUT of it, so referencing it is a cycle terraform
    refuses to plan. Worker JOBS are unaffected -- locals.tf derives the same
    value for them, because that module reads the Cloud Run outputs rather than
    feeding them.

    Read it from `terraform output quota_broker_url` after the first apply and
    put it in <env>.tfvars. The `quota_broker_url_is_wired` check fails while it
    is empty or stale.

    EMPTY MEANS "NO POOL ON THE GKE PATH", not "guess". A guessed URL is the
    worst outcome available here: a 404 is a configuration refusal, and a
    worker that is refused PARKS rather than running on the wrong credential,
    so a wrong guess would park every browser and GPU task in the fleet.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.quota_broker_url == "" || startswith(var.quota_broker_url, "https://")
    error_message = "quota_broker_url must be an https:// base URL, or empty."
  }
}

variable "groups_impersonate_user" {
  description = <<-EOT
    The Workspace user swarm-api acts AS when it reads Cloud Identity groups.

    Required because the Groups API does not authorize through GCP IAM. It
    authorizes through admin, non-admin or namespace modes, and a
    *.gserviceaccount.com identity is a principal in none of them -- every
    lookup returns "Error(2028): Permission denied", which reads like a
    missing role and is not one. Verified 2026-09-20:
    roles/cloudidentity.groupsReader is an ALPHA role with no included
    permissions, and granting it at the organization changed nothing.

    Domain-wide delegation is the documented route. It is authorised in the
    Google Admin console against this service account's OAuth client id
    (116078917197392888097) and scoped to
    https://www.googleapis.com/auth/cloud-identity.groups.readonly -- read
    group membership, and nothing else.

    Empty disables delegation.
  EOT
  type        = string
  default     = ""
}

variable "admin_users" {
  description = <<-EOT
    Individual email addresses granted admin, as an ESCAPE HATCH.

    Admin is otherwise decided by Cloud Identity group membership, and
    swarm-api cannot read groups: the Groups API does not authorize through
    GCP IAM, and a *.gserviceaccount.com identity is not a Workspace
    principal, so every membership lookup returns
    "Error(2028): Permission denied". With no resolvable groups the admin set
    is empty and NOBODY is an admin, which 403s every operator screen on the
    platform.

    Strictly worse than a group operationally -- changing this needs a deploy
    -- and not a weakening of authentication: the email is the one from the
    verified IAP assertion, which is the same source a group lookup would
    have started from.

    Empty this in the same change that grants swarm-api a Workspace Group
    Reader role. See docs/audits/2026-09-20/session-handover.md.

    NOT ONLY OPERATORS. Dev also lists the verification gate's service
    account (owner decision, 2026-09-24), so scripts/race-test.sh can narrow
    runner:mock through PUT /v1/admin/limits/runner/mock rather than a raw
    Firestore write -- see docs/audits/2026-09-22/race-test-needs-a-write.md.
    That entry must survive the emptying above: a service account is not a
    Workspace principal and can never be put in an admin group.

    EVERY ENTRY IS A FULL PLATFORM ADMIN, whatever it was added for. Admin is
    one boolean: it can pause dispatch, set any ceiling, drain or disable a
    provider for every tenant, write any tenant's document (max_active,
    capacity_units and `enabled`, so it can disable a tenant), rewrite any
    tenant's workflow state, and read every tenant's leases and records. The
    full list, and what admin cannot reach, is beside the entry in dev.tfvars.

    Bare emails, never IAM members: swarm-api compares each entry with the
    email in the verified token, so `serviceAccount:x@y` matches nobody.
  EOT
  type        = list(string)
  default     = []
}

variable "admin_groups" {
  description = <<-EOT
    Google groups whose members may act across tenants in swarm-api -- reading
    another tenant's tasks, and the platform routes that are not tenant-scoped.

    Separate from `tenants` on purpose. A tenant group is a group that OWNS
    work; an admin group is one that may look at everyone's. Deriving one from
    the other would make every tenant an administrator the moment it was
    registered, which is the opposite of invariant 9.

    Empty is the correct default and the current dev setting: with no admin
    group, no caller is an administrator, and the only cross-tenant identity is
    the platform tick account, which authenticates as a service account and
    never through this path.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for g in var.admin_groups : can(regex("^[^@]+@[^@]+$", g))])
    error_message = "every admin group must be a group email address."
  }
}

variable "secret_admin_members" {
  description = <<-EOT
    Fallback identities allowed to ADD a provider-key version for a tenant that
    declares no `secret_admins` of its own. They cannot read a key back.

    This list is applied to EVERY such tenant's secrets, so it must be a
    dedicated platform-admin group. A tenant's own group here would hand every
    member of that tenant secretVersionAdder on every other tenant's provider
    key -- and while secretVersionAdder cannot read the key in place, it can
    REPLACE it with one that points at an attacker-controlled proxy, after which
    the victim tenant's prompts, source and output all flow through it. The
    validation below refuses that shape outright.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = length([
      for m in var.secret_admin_members : m
      if contains([for t, v in var.tenants : lower("group:${v.principal}")], lower(m))
      || contains([for t, v in var.tenants : lower("user:${v.principal}")], lower(m))
    ]) == 0
    error_message = "secret_admin_members is applied to every tenant's secrets, so it may not name a tenant's own principal: that grants one tenant the ability to replace another tenant's provider key. Use a dedicated platform-admin group, or set per-tenant `secret_admins`."
  }
}

variable "api_invokers" {
  description = <<-EOT
    Identities allowed past Cloud Run's edge to reach swarm-api.

    This is NOT what authenticates a caller. swarm_api.auth is: it verifies the
    Google ID token against Google's keys, requires email_verified, enforces the
    saga.xyz hosted domain, and resolves Cloud Identity group membership to pick
    the tenant. This variable only decides who the platform lets through to be
    authenticated.

    Setting it to ["allUsers"] is the SUPPORTED configuration, because Cloud Run
    cannot do both jobs at once. When the edge enforces IAM it CONSUMES the
    caller's Authorization header and the container receives a different,
    non-JWT credential -- measured on one correlated request on 2026-09-16:
    the client sent tok:7f518e564d27 and the application saw tok:c130e289a085,
    which failed with MalformedError. So edge IAM does not add a second layer
    here; it removes the only one that can identify a tenant.

    Reachability is controlled by ingress, which the cloud_run module pins to
    internal-and-cloud-load-balancing and refuses to set to INGRESS_TRAFFIC_ALL.
    That, not this binding, is what satisfies "no required service publicly
    exposed by default".
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.api_invokers, "allAuthenticatedUsers")
    error_message = "allAuthenticatedUsers means every Google account and adds nothing: the application performs the real authentication. Use allUsers with internal ingress, or name specific groups."
  }
}

# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

variable "pool_limits" {
  description = <<-EOT
    Hard ceilings for the slot pools. These are the `hard_limit` of each pool
    document; the adaptive target and any quota-derived cap can only lower the
    effective limit, never raise it above this.
  EOT
  type = object({
    global           = optional(number, 100)
    default_tenant   = optional(number, 20)
    provider_tenant  = optional(number, 10)
    resource_classes = optional(map(number), {})
    runner_profiles  = optional(map(number), {})
    backends         = optional(map(number), {})
    providers        = optional(map(number), {})
  })
  default = {}

  validation {
    condition     = var.pool_limits.global > 0
    error_message = "the global pool must have a positive, finite ceiling."
  }

  # swarm_common.models.pool_names_for() makes a task acquire BOTH
  # `provider:<p>` and `provider:<p>:tenant:<t>`, and admission is all-or-nothing
  # across the list. So whenever the provider-wide ceiling is lower than the sum
  # of the per-tenant ceilings underneath it, it becomes the binding constraint
  # and tenants start competing for the same slots -- one tenant parking long
  # runs directly reduces what another can admit, even though that other tenant
  # pays for and holds its own key. The per-tenant pools exist precisely so that
  # cannot happen.
  #
  # Requiring the provider pool to be at least the sum keeps it as what it is
  # meant to be: a platform-wide safety net that can never be what denies a
  # tenant its own declared capacity. The `global` pool is where a real
  # cross-tenant ceiling belongs, because it is the one every task shares.
  validation {
    condition = alltrue([
      for p in distinct(flatten([for t, cfg in var.tenants : cfg.providers])) :
      lookup(var.pool_limits.providers, p, var.pool_limits.global) >=
      length([for t, cfg in var.tenants : t if contains(cfg.providers, p)]) * var.pool_limits.provider_tenant
    ])
    error_message = "a provider's platform-wide pool must be at least (tenants holding that provider) x provider_tenant, or the shared ceiling re-couples tenants that were deliberately given separate per-tenant provider pools."
  }
}

variable "service_max_instances" {
  description = "Explicit Cloud Run maximum per control-plane service."
  type        = map(number)
  default = {
    "swarm-api"          = 20
    "swarm-scheduler"    = 3
    "swarm-quota-broker" = 3
    "swarm-reconciler"   = 2
    "swarm-ui"           = 2
  }

  validation {
    condition     = alltrue([for k, v in var.service_max_instances : v > 0])
    error_message = "every service needs an explicit positive max-instances."
  }

  validation {
    condition = length(setsubtract(
      ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler", "swarm-ui"],
      keys(var.service_max_instances),
    )) == 0
    error_message = "service_max_instances must name every control-plane service. A tfvars file written before a service existed omits it, and the failure is an 'Invalid index' at plan time that names a line number rather than the missing key."
  }
}

variable "settings_env" {
  description = <<-EOT
    Overrides for swarm_common.config.Settings.from_env. Only what must be known
    at process start belongs here -- concurrency limits live in Firestore
    precisely so they can change without a redeploy.
  EOT
  type        = map(string)
  default     = {}
}

variable "allowed_domains" {
  description = "Hosted domains permitted to authenticate."
  type        = list(string)
  default     = ["saga.xyz"]

  validation {
    condition     = length(var.allowed_domains) > 0
    error_message = "at least one allowed domain is required; refusing open auth."
  }
}

# ---------------------------------------------------------------------------
# Storage and observability
# ---------------------------------------------------------------------------

variable "artifact_retention_days" {
  type    = number
  default = 90
}

variable "alert_emails" {
  type    = list(string)
  default = []
}

variable "extra_notification_channels" {
  type    = list(string)
  default = []
}

variable "create_alerts" {
  type    = bool
  default = true
}

variable "pubsub_kms_key_name" {
  description = "Optional CMEK for the wake and dead-letter topics. Empty uses Google-managed keys."
  type        = string
  default     = ""
}

variable "enable_quota_refresh" {
  description = "Create the Cloud Scheduler tick that recomputes provider quota state and adaptive pool targets."
  type        = bool
  default     = true
}

variable "manage_project_services" {
  description = "All 18 APIs are already enabled on saga-agents-staging; this holds them enabled rather than turning anything on."
  type        = bool
  default     = true
}

variable "custom_role_suffix" {
  description = "Disambiguates custom role ids when a previous one is still in its 7-day soft-delete window."
  type        = string
  default     = ""
}
