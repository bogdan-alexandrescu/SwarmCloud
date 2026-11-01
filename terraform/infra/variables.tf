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
  description = "Protects the Autopilot cluster and the Cloud Run services from deletion. The Firestore database has its own switch, firestore_delete_protection."
  type        = bool
  default     = true

  validation {
    condition     = var.environment != "prod" || var.deletion_protection || var.allow_unprotected_prod
    error_message = "prod requires deletion_protection = true. Set allow_unprotected_prod = true to override deliberately."
  }
}

variable "firestore_delete_protection" {
  description = <<-EOT
    Delete protection on the platform Firestore database. Separate from
    deletion_protection, which dev sets false so the cluster and the Cloud Run
    services can be torn down: the database must not follow it.
  EOT
  type        = bool
  # true (#1818): point-in-time versions are deleted with the database, so an
  # unprotected `swarm` is one `gcloud firestore databases delete` or console
  # click from total loss of the control plane. Turning it off is a deliberate
  # tfvars edit, never a side effect of the shared switch.
  default = true
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

# The module's own defaults (128 and 8192). An environment raises them in its
# tfvars, with the measurement that justified it (dev: #1334 c24).
variable "nat_min_ports_per_vm" {
  type    = number
  default = 128
}

variable "nat_max_ports_per_vm" {
  type    = number
  default = 8192
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

variable "gke_enable_fqdn_network_policy" {
  description = <<-EOT
    Admit hostname-based egress policy (FQDNNetworkPolicy) on the cluster.
    Default false, today's value; turning it off once on replaces the cluster.
    The full note is on the gke_autopilot module's enable_fqdn_network_policy.
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

# The admission-contention bench database (S32). Empty creates none, which is
# the default because prod has no business holding a database that a bench
# overwrites; dev.tfvars turns it on. See terraform/modules/firestore/bench.tf.
variable "firestore_bench_database" {
  type    = string
  default = ""
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

# The registry's cleanup (modules/artifact_registry, #1886).
variable "cleanup_dry_run" {
  description = "True makes Artifact Registry evaluate the swarm-images cleanup policies and log what they would delete, deleting nothing."
  type        = bool
  # True, not the module's false (owner 2026-10-21): no environment deletes an
  # image until its tfvars says so, which is the owner's call after reading the
  # would-delete list (docs/runbooks/registry-cleanup.md).
  default = true
}

variable "untagged_ttl" {
  description = "How long an untagged swarm-images version survives before delete-untagged removes it. Go duration, e.g. 604800s."
  type        = string
  # 7 days, the module's default, unchanged by #1886.
  default = "604800s"
}

variable "keep_recent_versions" {
  description = "How many of the newest versions of every swarm-images package keep-recent keeps, whatever their tags."
  type        = number
  # 20, the module's default, unchanged by #1886.
  default = 20
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
    # Service accounts that resolve to THIS tenant besides its principal, by
    # an exact match on the email the caller's verified token carries
    # (contract request 30). Bare emails of user-managed accounts in this
    # project; never a human, never an admin, never under two tenants.
    service_accounts = optional(list(string), [])
  }))
  default = {}

  validation {
    condition     = alltrue([for t, v in var.tenants : v.max_active == null || v.max_active > 0])
    error_message = "a tenant's max_active must be positive; omit it to take pool_limits.default_tenant."
  }

  # THE RETIRED #295 APP KEYS (#453 box 118; owner decision MS0-Q4,
  # 2026-10-06). git-merge and git-review were GitHub App keys read only by
  # the per-tenant merge and post-verdict accounts, which are gone. Listed
  # here, either would now get a secret whose reader is the tenant's worker
  # account -- an App key any agent of the tenant could mint a token for --
  # so it is refused at plan. swarm-api, the quota broker and
  # scripts/register-tenant.sh refuse the same two.
  validation {
    condition = alltrue([
      for t, v in var.tenants : !contains(v.providers, "git-merge") && !contains(v.providers, "git-review")
    ])
    error_message = "a tenant may not register git-merge or git-review: the #295 App keys are retired, and nothing but the tenant's worker would read one."
  }

  # THE REMOVED -git-wf SLOT (#2029, WF-APP-TOKEN; agent-autonomy.md §3.3).
  # git_wf_tenant carried this refusal and went with the slot. Listed here,
  # git-wf would get a secret whose reader is the tenant's worker account --
  # a workflow-scoped token any agent of the tenant could push with -- so it
  # is refused at plan, in every environment.
  validation {
    condition     = alltrue([for t, v in var.tenants : !contains(v.providers, "git-wf")])
    error_message = "a tenant may not list git-wf as a provider: the -git-wf slot no longer exists, and a workflow-file push uses the token the SwarmCloud GitHub App mints for one repository whose registration has workflow_files on."
  }

  # The same expression as swarm_common.identity.SERVICE_ACCOUNT_EMAIL, held
  # equal by scripts/lib/check-contract-parity.sh (section 14), plus the pin to
  # THIS project that the frozen module has no project id to apply.
  validation {
    condition = alltrue(flatten([
      for t, v in var.tenants : [
        for sa in v.service_accounts :
        can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", sa))
        && endswith(sa, "@${var.project_id}.iam.gserviceaccount.com")
      ]
    ]))
    error_message = "tenants.*.service_accounts takes bare, lower-case emails of user-managed service accounts in this project (<id>@<project>.iam.gserviceaccount.com): never a person, a group, a domain, a pattern, a serviceAccount: member or a Google-managed account."
  }

  validation {
    condition     = length(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])) == length(distinct(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])))
    error_message = "a service account may be listed under ONE tenant: it decides the account's secrets, GCS prefix and namespace, and two listings would make that depend on rendering order."
  }

  validation {
    condition = length(setintersection(
      toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
      toset(concat(
        [for u in var.admin_users : lower(u)],
        [for u in var.admin_pool_users : lower(u)]
      ))
    )) == 0
    error_message = "a tenant's service account may not also be in admin_users or admin_pool_users: a listing gives exactly one tenant's rights, and an admin entry would add every tenant's. (secret_admin_members is checked by that variable's own validation: reading it from here would be a cycle, since it already reads var.tenants.)"
  }

  # Per-tenant secret admins, read from var.tenants itself, so no cycle: a
  # listed account may not be ANY tenant's `secret_admins` entry. Any, not only
  # its own tenant's -- secretVersionAdder on another tenant's key is the
  # cross-tenant reach a listing exists to deny, and on its own tenant's key it
  # would let the bot replace the provider key its own tasks run on.
  validation {
    condition = length(setintersection(
      toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
      toset(flatten([
        for t, v in var.tenants : [for m in v.secret_admins : lower(replace(m, "serviceAccount:", ""))]
      ]))
    )) == 0
    error_message = "a tenant's service account may not also be in any tenant's secret_admins: a listing gives read and submit rights inside one tenant, never the ability to replace a provider key."
  }

  validation {
    condition = length(setintersection(
      toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
      toset([for t, v in var.tenants : lower(v.principal)])
    )) == 0
    error_message = "a service account that is a tenant's principal already has a tenant; listing it under another would give it two."
  }

  # A PERSON'S WORKSPACE IS NOT A TERRAFORM TENANT (docs/workspaces.md §3.2,
  # rule 3; owner decision WD3, 2026-10-08). Personal workspaces are made by
  # the workspace job and live outside Terraform state, so a plan can never
  # change or destroy one; a person in this map would be in state, and in a
  # public tfvars file besides (§2.6). So a `kind = "user"` tenant must be a
  # SERVICE account's -- swarm-verify's u-sw-c90291 and, since 2026-10-18
  # (#1568), oracle-api's u-or-9a0792.
  validation {
    condition = alltrue([
      for t, v in var.tenants :
      v.kind != "user" || endswith(lower(v.principal), ".iam.gserviceaccount.com")
    ])
    error_message = "a `kind = \"user\"` tenant whose principal is a person is a personal workspace, and personal workspaces are made by the workspace job, never by Terraform (docs/workspaces.md §3.2). Ask for it in the console's setup checklist; only a service account's tenant belongs here."
  }

  # VAR.TENANTS IS FROZEN to the tenants each environment has today
  # (docs/design/workspace-kinds.md §4.7, Q8 (a), answered by the owner
  # 2026-10-19). A NEW workspace of any kind -- personal, group or service --
  # is made by the in-cloud job swarm-workspace-apply from an admin's request
  # in Admin › Tenants (§4.11), and lives outside Terraform state, so no plan
  # can change or destroy it. These tenants stay here because they carry
  # what the job never makes: authoritative per-provider secret bindings and
  # Terraform-made Cloud Run and scheduler jobs. Moving one out is §4.7's
  # option (b), a plan that destroys nothing, one tenant per pull request.
  # The lists below are a deliberate second copy of dev.tfvars' and
  # prod.tfvars' keys: that pull request removes the tenant from its tfvars
  # file AND from its list here, or the freeze would still admit the id the
  # job now owns. Nothing may add to either.
  #
  # Per environment, because the environments differ: dev holds §4.7's four;
  # prod.tfvars holds eng, research and smoke. An environment with no tfvars
  # file (staging) has no Terraform tenants, so it may declare none.
  # tests/terraform/tenants_frozen.tftest.hcl holds this.
  validation {
    condition = length(setsubtract(keys(var.tenants), lookup({
      dev  = ["eng", "smoke", "u-sw-c90291", "u-or-9a0792"]
      prod = ["eng", "research", "smoke"]
    }, var.environment, []))) == 0
    error_message = "var.tenants is frozen to the tenants this environment already has (dev: eng, smoke, u-sw-c90291, u-or-9a0792; prod: eng, research, smoke). A new workspace, personal, group or service, is made by the in-cloud job swarm-workspace-apply from Admin › Tenants › New workspace, never by Terraform (docs/design/workspace-kinds.md §4.7, Q8 (a))."
  }
}

variable "enable_safety_tick_alert" {
  description = "Create the safety-tick-stopped alert. A kill switch: the metric it watches is a logs-based one the monitoring module creates in the same apply; see the module variable of the same name for why it no longer waits on a Cloud Scheduler metric."
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

variable "acceptance_qa_tenant" {
  description = <<-EOT
    The tenant the release acceptance's QA identity case submits in
    (SWARM_ACCEPTANCE_QA_TENANT on the swarm-verify job;
    scripts/acceptance/groups/live-browser.sh, run_live_browser_qa). It must
    be a tenant a platform admin has granted the QA identity: an ungranted one
    is refused qa_identity_not_granted at submission and the case FAILs.

    Empty by default, and empty means the case SKIPs every check: the grant is
    made in the console, not by Terraform, so an environment says it here only
    once that grant exists (dev first, BGD 2026-10-19 Q8).
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.acceptance_qa_tenant == "" || can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.acceptance_qa_tenant))
    error_message = "acceptance_qa_tenant is a tenant id (lower case letters, digits and hyphens), or empty to skip the QA identity case."
  }
}

variable "acceptance_console_url" {
  description = <<-EOT
    The console the release acceptance's QA identity case signs in to
    (SWARM_ACCEPTANCE_CONSOLE_URL on the swarm-verify job). The case judges
    "signed in" by this URL's host, so it is the environment's own console,
    https://<frontend_hostname>; tests/unit/scripts/
    test_verify_job_qa_identity_settings.py holds dev's equal to that.

    Empty by default: with no URL the case SKIPs every check.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.acceptance_console_url == "" || can(regex("^https://[a-z0-9.-]+/?$", var.acceptance_console_url))
    error_message = "acceptance_console_url is https://<host> (the environment's console), or empty to skip the QA identity case."
  }
}

variable "acceptance_sandbox_repository_url" {
  description = <<-EOT
    The sandbox repository the release acceptance pushes to and opens pull
    requests on (SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL on the swarm-verify
    job; scripts/acceptance/config.sh). Without it config.sh falls back to the
    live deployment's sandbox, so another company's deployment sets its own
    here and its acceptance never touches the live one's.

    Null by default, and null renders no env at all, so a deployment that does
    not set it plans no change (DAYONE-VERIFY-SANDBOX-ENV, part of #2688).
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.acceptance_sandbox_repository_url == null || can(regex("^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+$", var.acceptance_sandbox_repository_url))
    error_message = "acceptance_sandbox_repository_url is an https:// clone URL (https://github.com/<org>/<repo>.git), or null to keep the acceptance's default."
  }
}

variable "pr_console_links" {
  description = <<-EOT
    Whether a worker appends the workflow's and the agent's console links to
    the body of a pull request it opens (rendered as SWARM_PR_CONSOLE_LINKS
    onto the scheduler, which passes it to every worker through
    scheduler.dispatch.worker_env). Needs a console: with no frontend there is
    no link to append, whatever this says.

    OFF BY DEFAULT: off until the owner has seen it on a real PR. A PR body is
    read by people outside this platform, and a link they cannot open (the
    console is behind IAP) is noise in someone else's repository.
  EOT
  type        = bool
  default     = false
}

variable "internal_decision_api_enabled" {
  description = <<-EOT
    Whether the cluster's Tailscale egress to Saga's decision API is on
    (JUDGE-NET: the Tailscale Kubernetes operator in swarm-system with ONE
    egress Service to decision-api-staging). On, swarm-api and the scheduler
    get INTERNAL_DECISION_API_URL, the egress Service's in-cluster address,
    and a workspace's "Internal Jev" / "Internal Laya" judgment backend is
    available (JUDGE-SELECT).

    OFF BY DEFAULT: the egress needs the operator's OAuth client, which the
    owner stores as swarm-tailscale-oauth in Secret Manager. Off, an internal
    choice leaves Jev unavailable rather than sending page text to TypeSafe.
  EOT
  type        = bool
  default     = false
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

variable "platform_owner" {
  description = <<-EOT
    The ONE person whose admin rights no other admin can remove (PLATFORM_OWNER,
    docs/workspaces.md §6.5). swarm-api refuses to start if it is not one
    person's address or if it also appears in admin_pool_users. Empty means no
    owner is protected; admins and the last-admin rule still work.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.platform_owner == "" || (can(regex("^[^@,\\s]+@[^@,\\s]+$", var.platform_owner)) && !endswith(var.platform_owner, "gserviceaccount.com"))
    error_message = "platform_owner is one person's email address, not a list and not a service account."
  }
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

    EVERY ENTRY IS A FULL PLATFORM ADMIN, whatever it was added for. Admin is
    one boolean: it can pause dispatch, set any ceiling, drain or disable a
    provider for every tenant, write any tenant's document (max_active,
    capacity_units and `enabled`, so it can disable a tenant), rewrite any
    tenant's workflow state, and read every tenant's leases and records.

    SO IT IS FOR OPERATORS, NOT FOR THE VERIFICATION GATE. swarm-verify was
    put here on 2026-09-24 so race-test could narrow runner:mock, and the
    owner reversed that the same day because of the reach above. An identity
    that needs one admin route belongs in `admin_pool_users`, not here. See
    docs/audits/2026-09-22/race-test-needs-a-write.md.

    Bare emails, never IAM members: swarm-api compares each entry with the
    email in the verified token, so `serviceAccount:x@y` matches nobody.
  EOT
  type        = list(string)
  default     = []
}

variable "admin_pool_users" {
  description = <<-EOT
    Email addresses allowed ONE part of the admin surface: the routes in
    swarm-api's POOL_ADMIN_ROUTES allow-list (apps/swarm-api/swarm_api/auth.py),
    which today is PUT /v1/admin/limits/runner/{runner_profile} and nothing
    else. Every other /v1/admin route stays admin-only, and so does any admin
    route added later until it is deliberately allow-listed. Membership does
    not make the caller an admin anywhere else.

    Exists for the verification gate. scripts/race-test.sh narrows runner:mock
    to one slot, and restores it, through that route rather than a Firestore
    write role (owner decision, 2026-09-24). The first form of the decision
    put the gate in `admin_users`, which would also have let it disable any
    tenant; this list replaced that the same day. See
    docs/audits/2026-09-22/race-test-needs-a-write.md.

    What an entry CAN still do, stated rather than minimised: set the ceiling
    of ANY runner profile's pool, not only mock's -- the route takes the
    profile as a path parameter and the allow-list is by route. It cannot
    write `active`, create a pool outside the frozen catalogue, or reach a
    tenant, a provider switch or dispatch.

    Unaffected by emptying `admin_users` for a Group Reader role: there is no
    group form of this capability. Membership of an ADMIN_GROUPS group would
    make an identity a FULL admin, so the gate must not be added to any admin
    group -- a Google group can hold a service account as a member, where the
    group allows external members.

    Taken from the environment's tfvars as is; locals.tf appends nothing, so
    an identity holds this only where an environment names it.

    Bare emails, never IAM members: swarm-api compares each entry with the
    email in the verified token, so `serviceAccount:x@y` would plan, apply
    and match nobody. The validation below refuses that shape at plan time.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for u in var.admin_pool_users : can(regex("^[^@:]+@[^@:]+$", u))])
    error_message = "admin_pool_users takes bare email addresses, not IAM members: swarm-api compares each entry with the email in the verified token, so a serviceAccount: prefix would match nobody."
  }
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

  # Contract request 30: nor a service account a tenant LISTS. This half of the
  # "a listed account is never an admin" rule lives here, not on var.tenants
  # beside the admin_users/admin_pool_users half, because this variable's
  # validation already reads var.tenants and the reverse read is a cycle.
  validation {
    condition = length(setintersection(
      toset([for m in var.secret_admin_members : lower(replace(m, "serviceAccount:", ""))]),
      toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]]))
    )) == 0
    error_message = "secret_admin_members may not name a service account a tenant lists in tenants.*.service_accounts: a listing gives exactly one tenant's rights, and secretVersionAdder on every tenant's provider key would add every tenant's."
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
  description = <<-EOT
    Hosted domains permitted to authenticate.

    NO DEFAULT DOMAIN (null): a default is what a new install gets without
    being asked, and the only one this could hold is the live deployment's own
    domain (DAYONE-TF-1). Unset admits NO hosted domain -- only swarm-verify
    and var.allowed_users, swarm-api's solo deployment -- so leaving it out
    fails closed. An explicit [] is still refused: whoever writes it believes
    it opens sign-in to everyone.
  EOT
  type        = list(string)
  default     = null

  validation {
    condition     = var.allowed_domains == null || try(length(var.allowed_domains) > 0, false)
    error_message = "at least one allowed domain is required; refusing open auth. Leave allowed_domains unset to admit no hosted domain at all."
  }
}

variable "allowed_users" {
  description = <<-EOT
    Addresses admitted past the domain check in addition to swarm-verify's own
    account, which locals.tf always adds (swarm-api's ALLOWED_USERS). For a
    service account that is no Workspace principal and so can never match
    allowed_domains -- in the live deployment, the oracle-api client
    application (owner decision 2026-10-18, #1568).

    Empty by default: a new install admits no one by address until its owner
    names them. An input rather than a literal in locals.tf, because the
    address names one project and a literal would carry it into every install
    (DAYONE-TF-1).

    This widens WHICH verified identities are admitted, never whether an
    identity was verified: swarm-api consults it only after the token has been
    checked, and the address it compares comes from that token.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for u in var.allowed_users : can(regex("^[^@,[:space:]]+@[^@,[:space:]]+$", u))])
    error_message = "allowed_users are bare email addresses (no `user:` or `serviceAccount:` prefix, no comma): each is joined into swarm-api's comma-separated ALLOWED_USERS."
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
  description = "Ids of console-made channels (Email) every alert policy notifies. Ids only: the repository is public (docs/operations.md, \"Alert notification channels\")."
  type        = list(string)
  default     = []
}

variable "urgent_notification_channels" {
  description = "Ids of console-made channels (Slack) the CRITICAL and ERROR policies notify on top of extra_notification_channels; WARNING never does (#1817, option (b)). Ids only, never an address or token."
  type        = list(string)
  default     = []
}

variable "require_alert_channels" {
  description = "Fail the plan when alerts are created with no channel at all (#1817: 21 policies reached nobody). False until the change that commits the channel ids sets it true alongside them."
  type        = bool
  default     = false
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

variable "repository_grants_enforced" {
  description = <<-EOT
    swarm-api's REPOSITORY_GRANTS_ENFORCED (#780 D4, docs/onboarding.md):
    whether a PERSON's task on a GitHub repository they have not chosen under
    Access is refused (403 REPOSITORY_NOT_GRANTED). While off, such a task runs
    with the tenant token, `git`, as before #780. A service-account submission
    (repository indexing, schedules, the release's acceptance suite) keeps the
    tenant token either way and says so ("tenant token, service submission").
    OFF by default, as swarm_api.settings is: on, it refuses every task of every
    person who has not connected GitHub, so an environment turns it on only once
    its people can connect (a registered GitHub App, var.enable_github_app).
  EOT
  type        = bool
  default     = false
}

variable "platform_admin_reach" {
  description = <<-EOT
    swarm-api's PLATFORM_ADMIN_REACH (docs/design/workspace-roles.md §4.2,
    "Platform admins: admin in every group workspace, and in no one else's
    personal workspace"; owner answer Q5): whether a platform admin may select
    a registered group workspace they are not a member of, as its admin, each
    such request audited. Never another person's personal (u-) workspace, on
    or off. OFF by default, as swarm_api.settings is: it widens who may enter
    a tenant (invariant 9), so it goes on in dev first, is exercised by the
    acceptance probe, and only then in prod (§6, step 5).
  EOT
  type        = bool
  default     = false
}

variable "additional_repositories_enabled" {
  description = <<-EOT
    swarm-api's ADDITIONAL_REPOSITORIES_ENABLED (#1434,
    docs/design/multi-repository-steps.md §6 "Feature flag"): whether a
    submission may name `additional_repositories`, the registered repositories
    a step reads, at pinned commits, beside the one it writes. OFF by default,
    as swarm_api.settings is: off, a submission naming any is refused 422
    `additional_repositories_disabled` and nothing is stored, so a swarm-api
    deployed ahead of the workers that clone them writes no task those
    workers would fail. Turn it on only where workers carrying lane MR-WORKER
    are deployed. How many a step may name and how large they may be are not
    here: they are the platform settings max_additional_repositories (1-10,
    default 5) and additional_repositories_budget_gib (1-20, default 6), set
    at run time with PUT /v1/admin/settings.
  EOT
  type        = bool
  default     = false
}

variable "search_tool_enabled" {
  description = <<-EOT
    swarm-api's SEARCH_TOOL_ENABLED (docs/search-tool.md, #1030): whether
    POST /v1/attempts/{id}/search exists and live-browser agents are offered
    the `search` tool. OFF by default (owner decision 2026-10-13), as
    swarm_api.settings is: every query is billed to the platform, and the
    provider's key must be stored first with
    scripts/create-secrets.sh --search-api-key --stdin. Off, the route is a
    404 and no agent is told the tool exists.
  EOT
  type        = bool
  default     = false
}

variable "search_provider" {
  description = <<-EOT
    swarm-api's SEARCH_PROVIDER: which swarm_api.search.PROVIDERS entry
    answers a search. "brave" is the owner's choice (2026-10-13) and the only
    provider implemented. Validated here against the same names because
    swarm-api refuses to START on an unknown one, which a plan should catch
    before a revision does; add a name here in the change that adds it to
    PROVIDERS.
  EOT
  type        = string
  default     = "brave"

  validation {
    condition     = contains(["brave"], var.search_provider)
    error_message = "search_provider must name a swarm_api.search.PROVIDERS entry: brave."
  }
}

variable "mcp_servers_enabled" {
  description = <<-EOT
    swarm-api's SWARM_MCP_SERVERS (docs/design/registered-mcp-servers.md §6,
    #1433): whether a workspace may register MCP servers and a claude-code or
    codex step may name them in `input.mcp_servers`. Rendered "on" or "off":
    swarm_api.mcp_servers.mcp_servers_enabled() reads exactly "on" as on and
    anything else as off. OFF by default, as that function is: off, a
    registration write, an `mcp_servers` submission and an attempt's
    mcp:attach are each 503 `mcp_unavailable`, and nothing is stored. On, it
    also creates swarm-mcp-credentials-sweep (modules/scheduler/jobs.tf), the
    10-minute job that deletes a per-run credential once its task ends (owner
    decision Q2). Turn it on only where the owner has applied
    terraform/bootstrap's enable_mcp_credentials: without that grant swarm-api
    cannot read a stored credential at attach, so every attempt naming a
    `dispatch` server fails before its agent starts.
  EOT
  type        = bool
  default     = false
}

variable "workspace_apply_publish" {
  description = <<-EOT
    swarm-api's WORKSPACE_APPLY_PUBLISH: whether approving a personal
    workspace (and its dispatch sweep) publishes the workspace id to the
    swarm-workspace-apply topic, which starts the workspace job through
    Eventarc -> Workflows -> the Cloud Run job swarm-workspace-apply (WD2,
    re-decided 2026-10-10; docs/workspaces.md §2.1-2.2). OFF by default because
    the topic and what it starts exist only after the owner's bootstrap apply with
    enable_workspace_deployer = true (§10): publishing to a topic that does
    not exist records publish_failed on every sweep. Turn it on in the same
    release as that apply. Off, nothing is silent: the swarm-workspace-sweep
    job still runs, and every approved record is logged as workspace_stuck
    (reason publishing_off), which pages through the workspace-stuck alert.
  EOT
  type        = bool
  default     = false
}

variable "workspace_gate" {
  description = <<-EOT
    swarm-api's WORKSPACE_GATE (swarm_api.workspaces.gate_from_env, #847 WD8;
    docs/workspaces.md §5.5): whether a person's task or workflow submission
    into their own personal tenant is refused until their workspace is `ready`
    (403 WORKSPACE_NOT_READY) and has a Claude account (403 NO_CLAUDE_ACCOUNT),
    and whether swarm-api stops writing tenants/u-* on first sight. A group
    submission is never gated (WD7). OFF by default, as gate_from_env is: on
    refuses every person without a `ready` workspace record, so an environment
    turns it on only after one real approval has gone end to end through the
    swarm-workspace-apply job.
  EOT
  type        = bool
  default     = false
}

variable "manage_project_services" {
  description = "All 18 APIs are already enabled on saga-agents-staging; this holds them enabled rather than turning anything on."
  type        = bool
  default     = true
}

variable "deployer_service_account" {
  description = <<-EOT
    Email of the CI identity that runs this root's applies (the release's
    swarm-tf-deployer, created by terraform/bootstrap and named to GitHub as the
    GCP_DEPLOY_SA repository variable). It is granted actAs on each service
    account this root attaches to a service or job -- deployer.tf explains why and
    why individually.

    Empty means no such grants: correct for an owner applying from a workstation,
    who needs none.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.deployer_service_account == "" || can(regex("^[a-z][a-z0-9-]+@[a-z0-9-]+\\.iam\\.gserviceaccount\\.com$", var.deployer_service_account))
    error_message = "deployer_service_account must be a service account email, or empty."
  }
}

variable "api_refusals" {
  description = <<-EOT
    Which switched swarm-api refusals are on, by code: { "<code>" = true }.
    Each entry renders REFUSAL_<CODE>=on (or =off) into swarm-api's
    environment (local.api_refusal_env).

    A refusal added to swarm-api ships OFF, report-only, until an entry here
    turns it on (owner decision 2026-10-08, observer proposal I; see
    docs/api-refusals.md). Empty means every switched refusal is at its
    default, which is off. A code that names no switch in
    swarm_api.refusals.SWITCHES is refused when swarm-api starts, so a typo
    here fails the revision instead of leaving a refusal believed on.
  EOT
  type        = map(bool)
  default     = {}

  validation {
    condition     = alltrue([for code in keys(var.api_refusals) : can(regex("^[A-Za-z][A-Za-z0-9_]*$", code))])
    error_message = "api_refusals keys are swarm-api error codes: letters, digits and underscores."
  }
}

variable "mirror_kms_key_name" {
  description = "Customer-managed key for the pull-through mirror repositories (build_mirror.tf); empty keeps Google-managed encryption, as modules/artifact_registry does for kms_key_name."
  type        = string
  default     = ""
}

variable "settings_mac_tenants" {
  description = <<-EOT
    The tenants whose workspace_settings are signed and verified
    (settings_mac.tf; docs/design/tenant-settings-integrity.md). ONE list,
    rendered into swarm-api and the scheduler alike as SETTINGS_MAC_TENANTS,
    so the signer and the verifiers never disagree about who is cut over.
    Empty by default: no tenant is switched before the worker wiring
    (ISO-WIRE) has merged (owner decision on #1457, 2026-10-17).
  EOT
  type        = list(string)
  default     = []
}

# DEFAULTS TRUE: every Terraform tenant's worker keeps swarmTenantWorkerFirestore
# on the database, today's grant exactly (same addresses, so no environment
# plans a change by this variable existing). It is passed to module "tenancy"
# (main.tf), whose switch it is. False is the LAST step of #1457's design B
# cutover (docs/runbooks/isolation-cutover.md step c), and is refused at plan
# while any Terraform tenant is missing from state_via_api_tenants: that
# tenant's worker would still be dispatched onto the direct path and fail its
# first read with PermissionDenied.
variable "worker_firestore_access" {
  description = <<-EOT
    Grant each Terraform tenant's worker the swarmTenantWorkerFirestore role on
    the swarm database (modules/tenancy `worker_firestore_access`). True is
    today's grant. False removes it for every tenant in var.tenants, and plans
    only once every one of them is in state_via_api_tenants
    (docs/runbooks/isolation-cutover.md).
  EOT
  type        = bool
  default     = true
  nullable    = false

  validation {
    condition     = var.worker_firestore_access || alltrue([for t in keys(var.tenants) : contains(var.state_via_api_tenants, t)])
    error_message = "worker_firestore_access = false needs every tenant in var.tenants listed in state_via_api_tenants first: an unlisted tenant's worker is still dispatched onto direct Firestore and fails its first read once the role is gone (docs/runbooks/isolation-cutover.md)."
  }
}

# DEFAULTS EMPTY: every Terraform tenant keeps its worker_firestore binding,
# today's grant exactly, so no environment plans a change by this variable
# existing; listing a tenant is the owner's cutover step, never a default.
# Passed to module "tenancy" (main.tf), which filters the listed tenants out
# of the binding's for_each without re-keying the others: the plan destroys
# module.tenancy.google_project_iam_member.worker_firestore["<tenant>"] and
# nothing else. It exists because the cutover goes one tenant at a time (owner
# decision Q7, confirmed 2026-10-20: u-bogdan, then eng, then all), and
# worker_firestore_access is one switch for every Terraform tenant. Distinct
# from worker_firestore_off_tenants below, which is swarm-api's list for
# job-made workspace records and creates or destroys no binding. Refused at
# plan, as the global switch is, for a tenant not yet in
# state_via_api_tenants, and for `*` or anything else that is not a tenant id.
variable "worker_firestore_off_tenants_iam" {
  description = <<-EOT
    Terraform tenant ids (keys of var.tenants) whose worker loses its
    swarmTenantWorkerFirestore binding (module.tenancy worker_firestore) while
    worker_firestore_access is still true: #1457 design B, one tenant at a
    time (docs/runbooks/isolation-cutover.md step b2). Each entry must already
    be in state_via_api_tenants. Default [] = every tenant keeps today's grant.
  EOT
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for t in var.worker_firestore_off_tenants_iam : can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", t))])
    error_message = "every worker_firestore_off_tenants_iam entry must be a tenant id (lowercase letters, digits and inner hyphens). \"*\" is not supported: list the tenants."
  }

  validation {
    condition     = alltrue([for t in var.worker_firestore_off_tenants_iam : contains(var.state_via_api_tenants, t)])
    error_message = "every worker_firestore_off_tenants_iam entry must already be in state_via_api_tenants: that tenant's worker is otherwise still dispatched onto direct Firestore and fails its first read once its binding is destroyed (docs/runbooks/isolation-cutover.md)."
  }
}

# DEFAULTS EMPTY: swarm-api writes worker_firestore_access true on every
# job-made workspace record, so each keeps its grant exactly as before this
# variable existed. Rendered onto swarm-api alone as WORKER_FIRESTORE_OFF_TENANTS
# (locals.tf), the job-made path's twin of worker_firestore_access above
# (docs/workspaces.md §2.7). A tenant is listed only after it is in
# state_via_api_tenants, and the plan refuses it otherwise. `*` is refused
# too: swarm-api would read it as every tenant, which the state switch cannot
# list, so every unswitched workspace would lose a role its worker still uses.
variable "worker_firestore_off_tenants" {
  description = <<-EOT
    Tenant ids whose job-made workspace worker gets NO Firestore role
    (swarm-api's WORKER_FIRESTORE_OFF_TENANTS, #1457 design B; owner decision
    Q7: u-bogdan, then eng, then all). swarm-api writes the record's
    worker_firestore_access from it at each approval, retry and ceiling
    change; the workspace job then withholds the role and its A9 check
    requires the worker to hold none. Removing an existing binding is the
    owner's, by hand (docs/runbooks/isolation-cutover.md). Each entry must
    already be in state_via_api_tenants. Default [] = nobody cut over.
  EOT
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for t in var.worker_firestore_off_tenants : can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", t))])
    error_message = "every worker_firestore_off_tenants entry must be a tenant id (lowercase letters, digits and inner hyphens). \"*\" is not supported: list the tenants."
  }

  validation {
    condition     = alltrue([for t in var.worker_firestore_off_tenants : contains(var.state_via_api_tenants, t)])
    error_message = "every worker_firestore_off_tenants entry must already be in state_via_api_tenants: a workspace worker still dispatched onto direct Firestore fails its first read once its role is withheld (docs/runbooks/isolation-cutover.md)."
  }
}

variable "settings_mac_required" {
  description = <<-EOT
    The scheduler's SETTINGS_MAC_REQUIRED: whether it refuses to act on an
    unsigned or badly signed workspace_settings value for a tenant in
    settings_mac_tenants. Off by default, because turning it on before every
    listed tenant's document has been re-signed would make the scheduler
    ignore settings that are genuine.
  EOT
  type        = bool
  default     = false
}

variable "settings_mac_key_ready" {
  description = <<-EOT
    Whether swarm-settings-key has a version, so swarm-api and the scheduler
    may reference it as SWARM_SETTINGS_KEY (and, in a rotation,
    SWARM_SETTINGS_KEY_PREVIOUS). False by default because a Cloud Run
    revision that references a secret with no version fails to start, and the
    secret has none until `scripts/create-secrets.sh --settings-mac-key` runs,
    which needs the container this root creates; the first apply would
    otherwise take both services down. Set it true in the apply after that
    script has run (owner decision on #1457, 2026-10-17).
  EOT
  type        = bool
  default     = false
}

variable "settings_mac_key_previous_version" {
  description = <<-EOT
    During a swarm-settings-key rotation only: the version NUMBER the new one
    replaced, read by both services as SWARM_SETTINGS_KEY_PREVIOUS so a value
    signed under the old key still verifies until it is re-signed. Set it in
    the apply that follows `create-secrets.sh --settings-mac-key`, clear it
    once re-signing is done, then disable the old version. null outside a
    rotation, and then no service carries the variable at all.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.settings_mac_key_previous_version == null || can(regex("^[0-9]+$", var.settings_mac_key_previous_version))
    error_message = "settings_mac_key_previous_version must be a version NUMBER, or null; never a value."
  }
}

variable "swarm_workers_log_view_enabled" {
  description = "Whether to create SwarmCloud's worker-log path: the swarm-worker-logs bucket, the sink that fills it (no grant: a sink writing a bucket in its own project needs none), the swarm-workers view on that bucket and swarm-api's viewAccessor binding on it (log_bucket.tf, log_view.tf, agent-autonomy.md §3.5, row L-1)."
  type        = bool
  # Off by default: it adds an IAM row (L-1) the owner applies at dev-iam, and
  # a second, 30-day copy of every worker log (ingestion and storage cost), so
  # an environment takes it on by its own tfvars line. The sink's writer gets
  # no grant: BGD decided on 2026-10-22 (#1960) to drop it, since Google
  # authorizes a sink writing a log bucket in its own project. On in dev (BGD, 2026-10-22,
  # #1960); prod stays off until BGD turns it on there. The earlier design, a
  # view on _Default, failed (release run 39742374084: "Filter exceeds the view
  # filter nesting depth limit of 1"); this switch now builds the bucket-and-
  # sink design instead.
  default = false
}

# ---------------------------------------------------------------------------
# Day One: the edge, the console's App setup and the scheduled doctor
# (DAYONE-TF-2; docs/design/day-one-plan.md §1.2, design §9.2)
# ---------------------------------------------------------------------------

variable "discovery" {
  description = <<-EOT
    The non-secret discovery document swarm-discovery serves at
    https://<frontend_hostname>/.well-known/swarmcloud (discovery.tf, design
    §9.2): the deployment's display name, the release version, the IAP Desktop
    OAuth client id the plugin signs in with, and the oldest plugin version the
    deployment supports. The install engine writes it.

    EMPTY oauth_client_id (the default) creates no swarm-discovery service and
    no discovery backend: apps/swarm-api/swarm_api/discovery.py builds the
    document at start-up and refuses to start without each field, so a service
    without them would fail its first revision. The live deployment stays
    unchanged until the engine (or the owner) sets it.
  EOT
  type = object({
    name               = optional(string, "SwarmCloud")
    version            = optional(string, "")
    oauth_client_id    = optional(string, "")
    plugin_min_version = optional(string, "")
  })
  default = {}

  validation {
    condition     = var.discovery.oauth_client_id == "" || (var.discovery.version != "" && var.discovery.plugin_min_version != "" && var.discovery.name != "")
    error_message = "discovery.oauth_client_id is set, so discovery.version, discovery.plugin_min_version and discovery.name must be too: swarm-discovery refuses to start without any of them."
  }
}

variable "discovery_serves_client_secret" {
  description = <<-EOT
    Serve the IAP Desktop client's secret in the discovery document (design
    §9.3). Off (the default) swarm-discovery holds no role at all; on, it holds
    roles/secretmanager.secretAccessor on swarm-iap-desktop-client-secret and
    on no other secret (discovery.tf).
  EOT
  type        = bool
  default     = false
}

variable "enable_console_app_setup" {
  description = <<-EOT
    Create the GitHub App's three secret containers (swarm-github-app-client-
    secret, -private-key, -webhook-secret) independently of enable_github_app
    and enable_github_hooks, so the console's Admin > Setup > GitHub App step
    can store what the manifest exchange returns (plan R3, review finding 2).
    swarm-api's version-adder and accessor on exactly those three are
    terraform/bootstrap's (github_app_setup.tf). Off by default: the install
    engine turns it on in infra pass 2; the live deployment already has the
    containers through its App switches.
  EOT
  type        = bool
  default     = false
}

variable "enable_scheduled_doctor" {
  description = <<-EOT
    Run swarm-doctor, the read-only doctor, every hour (doctor.tf, D27), with
    its Scheduler job and the alert on its finding count. Off by default so
    nothing runs hourly before DAYONE-DOCTOR-1's code ships in the swarm-verify
    image; the engine turns it on (review finding 12).
  EOT
  type        = bool
  default     = false
}

variable "enable_acceptance_fixture" {
  description = <<-EOT
    The acceptance suite's public fixture bucket and its allUsers object
    reader (acceptance_fixture.tf), an addon:ci piece. Off by default: an
    install makes no public bucket (review finding 4; Domain Restricted
    Sharing refuses allUsers in most organisations). dev.tfvars and prod.tfvars
    set it true, so the live plan shows no change.
  EOT
  type        = bool
  default     = false
}

variable "groups_auth" {
  description = <<-EOT
    How swarm-api reads a caller's groups (GROUPS_AUTH, apps/swarm-api/swarm_api/
    groups.py; design §8, Q2): "delegation" (domain-wide delegation, the live
    deployment's mode) or "admin_role" (the Groups Reader admin role on
    swarm-api's own account, what installs use). The default keeps the live
    deployment unchanged until the owner flips it.
  EOT
  type        = string
  default     = "delegation"

  validation {
    condition     = contains(["delegation", "admin_role"], var.groups_auth)
    error_message = "groups_auth is delegation or admin_role; swarm-api refuses to start on any other value."
  }
}

variable "setup_github_org" {
  description = <<-EOT
    The GitHub organisation the console's Admin > Setup registers the App under
    (SETUP_GITHUB_ORG, apps/swarm-api/swarm_api/settings.py). Empty: not
    rendered, and the console's App step answers 409 setup_unconfigured. The
    install engine writes it from the stage 0 answers.
  EOT
  type        = string
  default     = ""
}
