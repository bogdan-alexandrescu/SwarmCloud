variable "project_id" {
  description = "SHARED project. Nothing in this root may touch another team's resources."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "location" {
  description = "State bucket location. Regional, matching the workloads."
  type        = string
  default     = "US-CENTRAL1"
}

variable "name_prefix" {
  type    = string
  default = "swarm"

  validation {
    condition     = startswith(var.name_prefix, "swarm")
    error_message = "name_prefix must start with 'swarm'."
  }
}

variable "state_bucket_name" {
  description = "Defaults to <prefix>-tfstate-<project_id>, which is globally unique because project ids are."
  type        = string
  default     = ""
}

variable "state_noncurrent_versions_to_keep" {
  description = "How many previous state files survive. State corruption is usually noticed several applies later."
  type        = number
  default     = 20

  validation {
    condition     = var.state_noncurrent_versions_to_keep >= 5
    error_message = "keep at least 5 previous state versions; fewer than that and a corruption found a day later is unrecoverable."
  }
}

variable "state_noncurrent_retention_days" {
  type    = number
  default = 365
}

variable "manage_project_services" {
  type    = bool
  default = true
}

variable "prerequisite_services" {
  description = <<-EOT
    The APIs terraform itself needs before terraform/infra can plan anything.
    The full 18 are enabled by terraform/infra; these are the ones without which
    that root cannot even authenticate or read the project.
  EOT
  type        = list(string)
  default = [
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ]
}

# ---------------------------------------------------------------------------
# GitHub Actions Workload Identity Federation (optional)
# ---------------------------------------------------------------------------

variable "enable_github_wif" {
  description = "Create a Workload Identity pool so GitHub Actions can deploy WITHOUT a downloadable service account key."
  type        = bool
  default     = false
}

variable "github_repository" {
  description = "owner/repo allowed to assume the deployer SA. Required when enable_github_wif is true."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be in owner/repo form."
  }
}

variable "github_allowed_refs" {
  description = <<-EOT
    Git refs permitted to deploy. Default is the default branch only.

    This is the difference between "our CI can deploy" and "anyone who can open
    a pull request against this repo can deploy": a workflow triggered from a
    fork runs with the repository's own OIDC subject unless the ref is pinned.

    Fully qualified refs only, and no wildcards. GitHub's `assertion.ref` is a
    full ref name, so the provider condition compares it for equality -- a
    pattern like `refs/heads/*` would simply never match anything and the pool
    would be unusable rather than wide. The validations below refuse both that
    and an empty list, which used to render the condition as
    `assertion.repository == "..." && ()`: malformed CEL that fails at apply.
  EOT
  type        = list(string)
  default     = ["refs/heads/main"]

  validation {
    condition     = length(var.github_allowed_refs) > 0
    error_message = "at least one ref must be allowed: an empty list renders a malformed attribute condition, and the ref pin is the whole boundary in front of the deployer's roles."
  }

  validation {
    condition = alltrue([
      for r in var.github_allowed_refs : can(regex("^refs/(heads|tags)/[A-Za-z0-9._/-]+$", r))
    ])
    error_message = "each ref must be a fully qualified refs/heads/<name> or refs/tags/<name>."
  }

  validation {
    condition = alltrue([
      for r in var.github_allowed_refs : !strcontains(r, "*")
    ])
    error_message = "wildcards are not supported: assertion.ref is compared for equality, so a pattern matches nothing at all."
  }

  validation {
    condition = alltrue([
      for r in var.github_allowed_refs : !startswith(r, "refs/pull/")
    ])
    error_message = "refs/pull/* is a pull request ref, including one from a fork; allowing it hands a deploy token to anyone who can open a PR."
  }
}

variable "deployer_roles" {
  description = <<-EOT
    Predefined roles granted to the GitHub deployer service account. Wide by
    necessity -- it manages the whole platform -- but explicitly enumerated
    rather than roles/owner, so what CI can do is reviewable in a diff.

    Secret Manager is deliberately absent. roles/secretmanager.admin includes
    secretmanager.versions.access, which would let anything running on an
    allowed ref print every tenant's Anthropic and OpenAI key in cleartext --
    while the humans who rotate those keys get only secretVersionAdder and
    cannot read one back (modules/secret_manager/main.tf). Terraform needs to
    CREATE secrets and set their IAM policy, not to read their payloads, so it
    gets the custom swarmSecretProvisioner role in wif.tf instead. The
    validation below stops the predefined role being put back by hand.

    Every role here is granted project-wide until it is also named in
    deployer_scoped_roles. The six marked SCOPABLE below have a conditioned
    grant waiting in deployer_conditions.tf; the nine marked UNSCOPABLE belong to
    services IAM cannot name resources in, and that file records why for each.

    roles/iam.roleAdmin is NOT here, and a validation below refuses it (#79,
    owner decision 2026-09-25): its iam.roles.update, which no condition can
    scope, lets CI widen a custom role it holds and so bypass every condition
    on its other grants. The custom roles it existed for are defined in
    platform_roles.tf, which the owner applies.
  EOT
  type        = list(string)
  default = [
    # UNSCOPABLE: Artifact Registry is not in IAM's resource-attribute list.
    "roles/artifactregistry.admin",
    # MEASURED, NOT ASSUMED. Without this the first main-ref CI build failed at
    # `gcloud builds submit` with PERMISSION_DENIED, naming
    # swarm-tf-deployer@ as the caller -- so WIF had worked and the identity
    # simply could not create a build. `build-images.sh` submits one Cloud Build
    # per image; this is the role that grants `cloudbuild.builds.create`.
    #
    # It is about BUILDS, not identities: it confers no ability to act as any
    # service account, which is why it can be project-level here while
    # `iam.serviceAccountUser` deliberately cannot (see wif.tf).
    #
    # UNSCOPABLE: builds are named by server-generated UUID; service unlisted.
    "roles/cloudbuild.builds.editor",
    # UNSCOPABLE: Cloud Scheduler is not in IAM's resource-attribute list.
    "roles/cloudscheduler.admin",
    # SCOPABLE (partly): load-balancer types and VMs; networks stay wide.
    "roles/compute.networkAdmin",
    # SCOPABLE (partly): firewall rules and VMs; SSL certs stay wide.
    "roles/compute.securityAdmin",
    # SCOPABLE: cluster-level calls; not the Kubernetes API inside a cluster.
    "roles/container.admin",
    # SCOPABLE: per Firestore database.
    "roles/datastore.owner",
    # roles/iam.roleAdmin WAS HERE until 2026-09-25 (#79). Unscopable, and the
    # one role that let CI rewrite the permissions behind its other grants.
    # terraform/infra defines no custom role any more (platform_roles.tf), so CI
    # needs no iam.roles.* permission at all.
    #
    # UNSCOPABLE: IAM resources provide no resource name.
    "roles/iam.serviceAccountAdmin",
    # UNSCOPABLE: IAM resources provide no resource name.
    "roles/iam.workloadIdentityPoolAdmin",
    # SCOPABLE (partly): log buckets and views; sinks and exclusions stay wide.
    "roles/logging.configWriter",
    # UNSCOPABLE: Cloud Monitoring is not in IAM's resource-attribute list.
    "roles/monitoring.editor",
    # UNSCOPABLE: Pub/Sub is not in IAM's resource-attribute list.
    "roles/pubsub.admin",
    # SCOPABLE: by the roles a policy change modifies, not by resource name.
    "roles/resourcemanager.projectIamAdmin",
    # UNSCOPABLE: Cloud Run is not in IAM's resource-attribute list.
    "roles/run.admin",
    # UNSCOPABLE: one set of enabled services per project; nothing to divide.
    "roles/serviceusage.serviceUsageAdmin",
    # storage.admin is NOT in this list any more; it is granted in wif.tf with an
    # IAM CONDITION instead, because it is the one role on this list whose blast
    # radius includes three buckets belonging to another team. See
    # `deployer_storage_condition` there for the exact expression and for the
    # bucket that nearly got cut off by it.
  ]

  validation {
    condition     = !contains(var.deployer_roles, "roles/owner") && !contains(var.deployer_roles, "roles/editor")
    error_message = "roles/owner and roles/editor are never granted to CI; enumerate the roles instead so the grant is reviewable."
  }

  # OWNER DECISION 2026-09-25 (#79). Said by name, although the reviewed-roles
  # check below would also refuse it, so that the refusal explains itself.
  validation {
    condition     = !contains(var.deployer_roles, "roles/iam.roleAdmin")
    error_message = "roles/iam.roleAdmin is never granted to CI (#79): iam.roles.update cannot be conditioned, and with it CI can add resourcemanager.projects.setIamPolicy to a custom role it already holds and grant itself anything, so no condition on its other grants is ever evaluated. Custom roles are defined in terraform/bootstrap/platform_roles.tf, which the owner applies."
  }

  # Every role here confers secretmanager.versions.access, directly or by
  # inheritance. A CI identity that can read a provider key back can exfiltrate
  # every tenant's key in one workflow run, which is a strictly larger authority
  # than anything else on this list and is not needed to manage the platform.
  validation {
    condition = length([
      for r in var.deployer_roles : r
      if contains([
        "roles/secretmanager.admin",
        "roles/secretmanager.secretAccessor",
        "roles/secretmanager.secretVersionManager",
      ], r)
    ]) == 0
    error_message = "no role granting secretmanager.versions.access may be given to CI: terraform creates secrets and sets their IAM policy, it never reads a payload. The custom swarmSecretProvisioner role covers what it does need."
  }

  # OWNER DECISION 2026-09-24: CI reads the swarm-verify job's logs and no
  # others, through one view granted with a condition in verify_logs.tf. Two
  # refusals keep this variable from granting a second log read, and neither
  # alone is enough.
  #
  # 1. EVERY PREDEFINED ROLE MEASURED TO READ LOG ENTRIES is refused by name:
  #    local.log_reading_roles, read from log-reading-roles.json -- 50 of the
  #    2,397 predefined roles on 2026-09-24. It is not only the logging roles:
  #    roles/iam.securityReviewer reads logEntries AND privateLogEntries, and
  #    seven roles/firebase.* roles read logEntries or views. The first version
  #    of this refusal named five logging roles and let all of those through.
  #    roles/logging.viewAccessor is in the file because granted HERE it has no
  #    condition and reads every view in the project.
  #
  # 2. EVERY ROLE NOBODY HAS REVIEWED is refused: local.deployer_roles_reviewed
  #    in verify_logs.tf. The measured list is only as current as its date;
  #    this is what stops a role Google changes afterwards.
  #
  # Neither says anything about what the deployer can reach through the roles
  # it does hold -- verify_logs.tf, "WHAT THIS DOES NOT BOUND".
  validation {
    condition     = length(setintersection(toset(var.deployer_roles), toset(local.log_reading_roles))) == 0
    error_message = "no role measured to read log entries project-wide (terraform/bootstrap/log-reading-roles.json) may be given to CI: it reads the swarm-verify job's logs through one view, granted with a condition in verify_logs.tf, and nothing else (owner decision 2026-09-24)."
  }

  validation {
    condition     = alltrue([for r in var.deployer_roles : contains(local.deployer_roles_reviewed, r)])
    error_message = "every role in deployer_roles must be in local.deployer_roles_reviewed (terraform/bootstrap/verify_logs.tf). Adding one there is the review: `gcloud iam roles describe <role>` must show none of logging.logEntries.list, logging.privateLogEntries.list or logging.views.access, because CI may read no log but swarm-verify's (owner decision 2026-09-24)."
  }
}

variable "deployer_secret_permissions" {
  description = <<-EOT
    The custom secret-provisioning role CI actually gets. Everything needed to
    create a tenant's secret, set who may read it, and expire an old version --
    and nothing that can read a payload back.

    `secretmanager.versions.access` is the permission being withheld, and a
    validation refuses it explicitly so it cannot be reintroduced by appending
    to this list.
  EOT
  type        = list(string)
  default = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.get",
    "secretmanager.secrets.list",
    "secretmanager.secrets.update",
    "secretmanager.secrets.delete",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
    # Version metadata and lifecycle. `add` is what a rotation needs; none of
    # these returns the payload.
    "secretmanager.versions.add",
    "secretmanager.versions.get",
    "secretmanager.versions.list",
    "secretmanager.versions.enable",
    "secretmanager.versions.disable",
    "secretmanager.versions.destroy",
    "secretmanager.locations.get",
    "secretmanager.locations.list",
  ]

  validation {
    condition     = !contains(var.deployer_secret_permissions, "secretmanager.versions.access")
    error_message = "secretmanager.versions.access is the one permission this role exists to withhold: it is what turns a deploy identity into a reader of every tenant's provider key."
  }

  validation {
    condition = alltrue([
      for p in var.deployer_secret_permissions : startswith(p, "secretmanager.")
    ])
    error_message = "this role covers Secret Manager only; anything else belongs in deployer_roles where it is visible as a named role."
  }
}

variable "deployer_scoped_roles" {
  description = <<-EOT
    Roles whose project-wide grant is REPLACED by the conditioned grant in
    deployer_conditions.tf. Naming a role here removes it from deployer_roles'
    unconditioned for_each and creates its conditioned block, in one plan.
    "swarmSecretProvisioner" names the custom role in wif.tf.

    ADD ONE PER RELEASE, and apply between releases. The next release's plan
    refreshes every managed resource, so a condition that is wrong fails there,
    immediately and loudly, on our own resource. Reverting is removing the
    entry and applying again.

    Empty by default so that merging the conditions changes nothing live.
  EOT
  type        = set(string)
  default     = []

  # Only a role with a conditioned block may be named. Anything else would be
  # subtracted from deployer_roles with nothing created in its place -- a role
  # silently revoked. The allowed names are the keys of the conditions
  # themselves, not a second list of them.
  validation {
    condition     = alltrue([for r in var.deployer_scoped_roles : contains(keys(local.deployer_conditions), r)])
    error_message = "only a role with a conditioned grant in deployer_conditions.tf can be scoped; the others are unscopable, and deployer_conditions.tf records why for each."
  }

  # And only a role the deployer actually holds. Scoping a role that is not in
  # deployer_roles would GRANT it, conditioned, rather than narrow it.
  validation {
    condition     = alltrue([for r in var.deployer_scoped_roles : r == "swarmSecretProvisioner" || contains(var.deployer_roles, r)])
    error_message = "a scoped role must also be in deployer_roles; scoping is a narrowing of an existing grant, never a new one."
  }
}

variable "extra_labels" {
  type    = map(string)
  default = {}

  validation {
    condition     = !contains(keys(var.extra_labels), "managed-by")
    error_message = "managed-by is fixed by this configuration."
  }
}

variable "deployer_storage_bucket_prefixes" {
  description = <<-EOT
    Bucket name prefixes the CI deployer may administer. Everything this platform
    creates is `swarm-`-prefixed, so one entry covers all of it -- and a prefix
    match also admits every OBJECT in those buckets, because an object's resource
    name is `projects/_/buckets/<b>/objects/<o>`.
  EOT
  type        = list(string)
  default     = ["swarm-"]

  validation {
    condition     = length(var.deployer_storage_bucket_prefixes) > 0
    error_message = "at least one prefix is required, or the deployer can administer no bucket at all and every terraform state write fails."
  }

  validation {
    # A one- or two-character prefix is not a scope. "s" would admit
    # saga-agents-files-staging, which is another team's.
    condition     = alltrue([for p in var.deployer_storage_bucket_prefixes : length(p) >= 4])
    error_message = "a prefix shorter than 4 characters is too broad to be a boundary in a shared project; `s` would admit saga-agents-files-staging."
  }
}

variable "deployer_storage_buckets_exact" {
  description = <<-EOT
    Buckets admitted by EXACT name because they do not carry the platform prefix
    and cannot be renamed.

    `<project>_cloudbuild` is GCP's own source-staging bucket for
    `gcloud builds submit`. It is neither ours nor the other team's, it does not
    match `swarm-`, and omitting it breaks every image build -- which is how a
    plausible-looking `startsWith("swarm-")` condition would have taken down the
    release pipeline on the day it first worked.
  EOT
  type        = list(string)
  default     = ["saga-agents-staging_cloudbuild"]
}

variable "frontend_iap_backends" {
  description = <<-EOT
    The IAP-protected backend services whose accessor list this root manages
    (wif.tf, frontend_accessors). terraform/infra's frontend module creates them
    and turns IAP on; it no longer sets who may pass.

    The names follow modules/frontend/main.tf for name_prefix "swarm". A rename
    there makes this plan fail at the data source lookup.

    Set to [] for the first bootstrap apply on a fresh project, before
    terraform/infra has created the backends.
  EOT
  type        = list(string)
  default     = ["swarm-ui-backend", "swarm-ui-ui-backend"]

  # Nine of the ten backend services in saga-agents-staging belong to another
  # team, including their Keycloak and ArgoCD. The prefix check keeps any of them
  # out of this list.
  validation {
    condition     = alltrue([for b in var.frontend_iap_backends : startswith(b, "swarm")])
    error_message = "frontend_iap_backends may only name the platform's own backends (prefix 'swarm'); the others in this project belong to another team."
  }
}

variable "frontend_iap_members" {
  description = <<-EOT
    Who may pass IAP. The OUTER gate only -- swarm-api remains the tenant
    boundary, verifying the token, enforcing ALLOWED_DOMAINS and scoping every
    read to the caller's own tenant.

    `domain:saga.xyz` is the intended shape for people: it matches what the API
    already enforces, so there is no second list to drift. Service accounts are
    listed individually, each with the reason it needs the front door.

    Moved here from terraform/infra (where it was `frontend_iap_members` in
    environments/<env>/<env>.tfvars) on 2026-09-24; see wif.tf.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.frontend_iap_members) > 0
    error_message = "frontend_iap_members may not be empty: an IAP-protected backend with no members is unreachable by everyone, which reads as an outage rather than a configuration mistake."
  }

  validation {
    condition     = alltrue([for m in var.frontend_iap_members : can(regex("^(user|group|domain|serviceAccount):", m))])
    error_message = "every IAP member must be a fully qualified IAM member, e.g. domain:saga.xyz or group:eng@saga.xyz."
  }

  validation {
    condition     = !contains(var.frontend_iap_members, "allUsers") && !contains(var.frontend_iap_members, "allAuthenticatedUsers")
    error_message = "allUsers and allAuthenticatedUsers defeat IAP entirely: allAuthenticatedUsers means ANY Google account on the internet, not any account in your organisation."
  }
}

# ---------------------------------------------------------------------------
# The platform's custom roles and the broker's swarmSecretLister grant
# (platform_roles.tf; #79, #69)
# ---------------------------------------------------------------------------

variable "adopt_from_infra_states" {
  description = <<-EOT
    terraform/infra state prefixes, in the state bucket this root creates, that
    created the platform's eight custom roles and the quota broker's
    swarmSecretLister grant before they moved here -- "infra/dev" for
    saga-agents-staging, the prefix scripts/bootstrap.sh gives the dev root.

    Non-empty does two things (platform_roles.tf):
      * the `import` blocks adopt the nine live objects into this root's state;
      * every plan reads each named state's outputs, and refuses to manage the
        roles or the grant until that state's custom_roles_owner output reads
        "terraform/bootstrap" -- the output the release writes in the same
        apply that makes terraform/infra forget them. That is what stops this
        root adopting a role terraform/infra still manages.

    EMPTY on a fresh project, where nothing exists to adopt, and in tests.
    Leaving it set after the adoption keeps the second check on every plan;
    remove it if that infra state is ever destroyed, or the check fails.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for p in var.adopt_from_infra_states : can(regex("^infra/[a-z0-9-]+$", p))])
    error_message = "each entry is a terraform/infra state prefix, infra/<environment>, e.g. infra/dev."
  }
}

variable "grant_broker_secret_lister" {
  description = <<-EOT
    Grant swarm-quota-broker the swarmSecretLister role (platform_roles.tf).
    terraform/infra creates that account, so on a FRESH project the first
    bootstrap apply must run with this false -- a grant to an account that does
    not exist yet is refused -- and the next one, after terraform/infra's first
    apply, with it true. Everywhere else, true.
  EOT
  type        = bool
  default     = true
}

variable "broker_secret_lister_project_wide_versions_add" {
  description = <<-EOT
    Keep secretmanager.versions.add in the project-wide swarmSecretLister role.

    Moved here from terraform/modules/iam with the role (#79), with the same
    default. The scoped grant that replaces it, terraform/infra's
    broker_version_adder (modules/iam/custom_roles.tf), is already live, so
    flipping this to false is the second of the two steps that comment
    describes -- now an owner bootstrap apply rather than a release. Flip it
    only once that grant has had at least 7 minutes to propagate; removing the
    project-wide permission first risks refusing an account's rotated refresh
    token, which strands the account (measurement in that comment).
  EOT
  type        = bool
  default     = true
}

variable "image_puller_permissions" {
  description = <<-EOT
    The pull-only role's permission set (swarmImagePuller, platform_roles.tf):
    roles/artifactregistry.reader minus every enumeration permission, and minus
    everything that writes. Moved here from terraform/modules/artifact_registry's
    `puller_permissions` with the role (#79), default and validations unchanged;
    modules/artifact_registry's `pullers` says why enumeration is the line.

    Verified against `gcloud iam list-testable-permissions` for this project on
    2026-09-16 -- an invalid permission name fails at apply, not at plan, so
    this list is checked against the API rather than remembered.
  EOT
  type        = list(string)
  default = [
    # Resolve the repository and the region.
    "artifactregistry.repositories.get",
    "artifactregistry.locations.get",
    # Pull: the manifest, the version behind the tag, and the layers.
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.dockerimages.get",
    "artifactregistry.packages.get",
    "artifactregistry.tags.get",
    "artifactregistry.versions.get",
    "artifactregistry.files.get",
    "artifactregistry.files.download",
  ]

  validation {
    condition     = length(var.image_puller_permissions) > 0
    error_message = "the pull-only role needs at least the download permissions, or every worker fails to start on an image pull."
  }

  validation {
    condition = alltrue([
      for p in var.image_puller_permissions : startswith(p, "artifactregistry.")
    ])
    error_message = "this role covers Artifact Registry only; anything else belongs in a named role where it is visible."
  }

  # `.list`, `listEffectiveTags` and `listTagBindings` are all enumeration, and
  # enumeration is the single property this role exists to withhold.
  validation {
    condition = alltrue([
      for p in var.image_puller_permissions : !strcontains(lower(p), "list")
    ])
    error_message = "no enumeration permission may be added: the point of this role is that a tenant worker cannot discover an image name it was not given."
  }

  validation {
    condition = alltrue([
      for p in var.image_puller_permissions :
      !can(regex("(create|update|delete|upload|export|setIamPolicy|createOnPush)", p))
    ])
    error_message = "the pull role is read-only: no runtime identity may push, delete or export an image."
  }
}
