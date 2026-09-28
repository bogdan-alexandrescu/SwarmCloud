# Custom roles.
#
# The predefined alternatives are all too wide for this platform:
#
#   roles/run.developer also grants run.services.* -- the scheduler could
#   redeploy the API it is called by.
#   roles/run.admin adds setIamPolicy, so the dispatcher could grant itself
#   anything on any Cloud Run resource.
#   roles/container.developer covers every workload type in every namespace,
#   including Secrets.
#
# Each role below is the exact permission set one component needs and nothing
# beyond it. Note what is absent from every one of them: setIamPolicy, and any
# delete permission outside the reconciler's role.

locals {
  role_suffix = var.custom_role_suffix == "" ? "" : "_${var.custom_role_suffix}"

  # Role ids live here, in configuration, rather than being read back off the
  # resources. They end up inside for_each KEYS in bindings.tf, and a key that
  # depends on a resource attribute is unknown for any role that does not exist
  # yet. `terraform plan` happens to resolve it from configuration anyway, which
  # is why that worked -- but `terraform import` does not, and the whole root
  # module becomes un-importable the moment a new custom role is added. Adding
  # one should not break importing an unrelated Firestore document.
  custom_role_ids = {
    job_dispatcher = "swarmJobDispatcher${local.role_suffix}"
    job_reaper     = "swarmJobReaper${local.role_suffix}"
    gke_dispatcher = "swarmGkeDispatcher${local.role_suffix}"
    gke_reaper     = "swarmGkeReaper${local.role_suffix}"
    secret_lister  = "swarmSecretLister${local.role_suffix}"
  }
}

resource "google_project_iam_custom_role" "job_dispatcher" {
  project = var.project_id
  role_id = local.custom_role_ids.job_dispatcher
  title   = "Swarm Job Dispatcher"

  description = "Create and run Cloud Run Job resources. No delete, no IAM, no services."
  stage       = "GA"

  permissions = [
    # Per-tenant-per-profile Job resources: Cloud Run pins the service account
    # on the Job, not the execution, so the dispatcher must be able to create
    # them (CONTRACT.md, platform decisions).
    "run.jobs.create",
    "run.jobs.get",
    "run.jobs.list",
    "run.jobs.update",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.executions.get",
    "run.executions.list",
    "run.tasks.get",
    "run.tasks.list",
    "run.operations.get",
    "run.operations.list",
  ]
}

resource "google_project_iam_custom_role" "job_reaper" {
  project = var.project_id
  role_id = local.custom_role_ids.job_reaper
  title   = "Swarm Job Reaper"

  description = "Cancel and delete executions, garbage-collect unused Job resources. Reconciler only."
  stage       = "GA"

  permissions = [
    "run.jobs.get",
    "run.jobs.list",
    "run.jobs.delete",
    "run.executions.get",
    "run.executions.list",
    "run.executions.cancel",
    "run.executions.delete",
    "run.tasks.get",
    "run.tasks.list",
    "run.operations.get",
    "run.operations.list",
  ]
}

resource "google_project_iam_custom_role" "gke_dispatcher" {
  count = var.gke_enabled ? 1 : 0

  project = var.project_id
  role_id = local.custom_role_ids.gke_dispatcher
  title   = "Swarm GKE Dispatcher"

  description = "Create and observe Jobs on the swarm Autopilot cluster. No Secret access, no cluster mutation."
  stage       = "GA"

  permissions = [
    # clusters.get is what `gcloud container clusters get-credentials` needs;
    # it returns the endpoint and CA, not any workload data.
    "container.clusters.get",
    "container.namespaces.get",
    "container.namespaces.list",
    "container.jobs.create",
    "container.jobs.get",
    "container.jobs.list",
    "container.jobs.update",
    "container.pods.get",
    "container.pods.list",
    "container.pods.getLogs",
    "container.events.get",
    "container.events.list",
  ]
}

resource "google_project_iam_custom_role" "gke_reaper" {
  count = var.gke_enabled ? 1 : 0

  project = var.project_id
  role_id = local.custom_role_ids.gke_reaper
  title   = "Swarm GKE Reaper"

  description = "Delete finished Jobs and stuck Pods on the swarm Autopilot cluster. Reconciler only."
  stage       = "GA"

  permissions = [
    "container.clusters.get",
    "container.namespaces.get",
    "container.jobs.get",
    "container.jobs.list",
    "container.jobs.delete",
    "container.pods.get",
    "container.pods.list",
    "container.pods.delete",
    "container.events.list",
  ]
}

resource "google_project_iam_custom_role" "secret_lister" {
  project = var.project_id
  role_id = local.custom_role_ids.secret_lister
  title   = "Swarm Secret Lister and Account Provisioner"

  description = "List secret metadata project-wide, and create the two secrets an account pool entry needs. Cannot read any payload."
  stage       = "GA"

  # The quota broker must discover which tenants hold a subscription
  # credential, and discovery is a list. roles/secretmanager.viewer would do it,
  # but it also grants versions.list and versions.get across every secret in the
  # project, including secrets belonging to teams that have nothing to do with
  # this platform. This is the single permission discovery actually needs.
  #
  # Absent, and deliberately: secretmanager.versions.access. Payload access
  # stays per-secret, granted by the secret_manager module on the specific
  # refresh secrets, so a bug here cannot widen into reading tenant keys.
  permissions = concat([
    "secretmanager.secrets.list",

    # PROVISIONING an account's secrets, added when account management moved
    # into the Settings page and stopped being a shell script.
    #
    # A secret for a pool account cannot be created ahead of time: its name
    # contains a LABEL the operator chooses at registration, so terraform
    # cannot declare it and only the component handling the registration can
    # make it. That component is this one, because it is already the single
    # writer for subscription credentials -- see quota_broker.credentials for
    # why a second writer bricks a rotating credential.
    #
    # `setIamPolicy` is here for the same reason `create` is. A secret created
    # without an accessor binding is one the tenant's pod cannot read, and the
    # failure surfaces much later as an unexplained auth error inside a job.
    # Creating it and binding it are one operation or the secret is useless.
    "secretmanager.secrets.create",
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",

    # RETENTION, added 2026-09-22 on the owner's explicit decision.
    #
    # Without these the broker cannot expire what it supersedes, and it never
    # could: swarm-tenant-u-bogdan-anthropic reached 1,816 versions, ALL
    # ENABLED, ZERO destroyed, because nothing in this platform had ever
    # expired one. Only `latest` is ever read, so 1,815 of those were dead
    # credentials that stayed retrievable by anything holding accessor. A
    # credential that rotates but leaves its predecessor enabled has not
    # rotated -- the same point create-secrets.sh makes beside
    # `--disable-previous`.
    #
    # `list` is needed before `destroy`: retention recomputes the retained set
    # from a live listing on every publish rather than recording state.
    #
    # THE SCOPE IS PROJECT-WIDE AND THAT IS A DELIBERATE, INFORMED CHOICE, not
    # an oversight. saga-agents-staging is SHARED, so this permits the broker
    # to destroy a version of any secret in it, including another team's. The
    # owner chose this over a per-secret binding on 2026-09-22 having been shown
    # that trade-off explicitly.
    #
    # WHAT ACTUALLY STOPS IT is therefore no longer IAM but
    # `quota_broker.secretstore.owned_by_this_platform`, which matches
    # \Aswarm-(?:tenant|account)-[A-Za-z0-9_-]+\Z -- anchored with \A/\Z
    # rather than ^/$ so a trailing newline cannot smuggle a second name past
    # it, and admitting no `/` so a name cannot re-point the resource path at
    # another secret or project. It was attacked with thirteen hostile inputs
    # on 2026-09-22 -- newline injection, traversal, full resource paths,
    # lookalike prefixes -- and refused all of them.
    #
    # If that guard is ever weakened, this grant becomes the hole. Do not widen
    # one without re-reading the other.
    "secretmanager.versions.list",
    "secretmanager.versions.destroy",
    ],
    # versions.add, while it is still project-wide. See broker_version_adder
    # below for where it is going and why it cannot go there in one step.
    var.secret_lister_project_wide_versions_add ? ["secretmanager.versions.add"] : [],
  )

  # Create the scoped grant BEFORE this role loses the project-wide permission,
  # in any apply that does both. Ordering the API calls cannot order IAM's
  # propagation, which is why the two are also separate releases (below); this
  # only stops terraform making the window wider than it has to be.
  depends_on = [google_project_iam_member.broker_version_adder]
}

# ---------------------------------------------------------------------------
# secretmanager.versions.add, SCOPED TO THE PLATFORM'S OWN SECRETS.
#
# The broker adds versions to two kinds of secret, and they reach it by two
# different routes:
#
#   swarm-tenant-<t>-<provider>[-refresh]   created by terraform; the broker is
#       bound secretVersionAdder ON EACH ONE by modules/secret_manager
#       (`version_adder`). Already per-secret.
#   swarm-account-<t>--<label>[-refresh]    created by the BROKER at
#       registration, with a name terraform cannot know in advance. These were
#       reachable only through `versions.add` in the project-wide role above --
#       and so was every one of the 63 secrets in this project that belong to
#       another team (live listing, 2026-09-24: 57 agents-*, 6 promptlab-*).
#
# A literal per-secret binding cannot cover the second kind without the broker
# granting itself adder on every secret it creates, which it could only do
# through the project-wide setIamPolicy -- the larger grant. So the scope is
# drawn where the platform already draws it: the names
# `quota_broker.secretstore.owned_by_this_platform` accepts,
# \Aswarm-(?:tenant|account)-..., as two prefixes IAM evaluates per secret. Of
# the 77 secrets in the project that admits exactly our 14 and refuses the 63.
#
# `projects/<NUMBER>/secrets/`: Secret Manager names secrets by project number
# in a condition, and the project ID "can't be substituted"
# (docs.cloud.google.com/iam/docs/conditions-resource-attributes).
# secretVersionAdder is {versions.add, secrets.rotate,
# resourcemanager.projects.get/list} (gcloud iam roles describe, 2026-09-24);
# both secretmanager permissions are checked on the secret, so no type guard is
# needed -- and none is used, so nothing depends on how IAM types a version.
#
# WHY THIS IS TWO RELEASES AND NOT ONE. Every refresh of an account ROTATES its
# refresh token (76 exchanges, 76 rotations, measured 2026-09-22 in
# quota_broker.credentials), and the new token is written with versions.add
# immediately after the exchange that consumed the old one. If that write is
# refused, the only valid refresh token is lost and the account needs a human.
# Granting the scoped permission and removing the project-wide one in the same
# apply leaves a window, as wide as IAM's propagation, in which the removal can
# land first -- at ~34 exchanges a day across seven accounts, a few minutes of
# window is a real chance of bricking one. So:
#
#   release 1  this grant exists; the role above still carries versions.add
#   release 2  secret_lister_project_wide_versions_add = false (the default
#              below, flipped), after release 1 has been live long enough to
#              propagate -- 7 minutes is IAM's documented worst case
#
# NOT VERIFIED LIVE: that IAM evaluates this condition as documented for
# versions.add. Release 1 proves nothing about it -- the project-wide grant is
# still there -- so the proof is the first account refresh after release 2,
# logged as "refresh token rotated and persisted".
# ---------------------------------------------------------------------------

variable "secret_lister_project_wide_versions_add" {
  description = <<-EOT
    Keep secretmanager.versions.add in the project-wide swarmSecretLister role.

    TRUE for exactly one release: the one that creates broker_version_adder, the
    scoped grant that replaces it. Flip it to false in the next release, once
    that grant has had time to propagate. Doing both in one apply risks refusing
    an account's rotated refresh token, which strands the account; see
    custom_roles.tf for the measurement.
  EOT
  type        = bool
  default     = true
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # The two name families quota_broker.secretstore.owned_by_this_platform
  # accepts. Literal "swarm-", like that regex: the broker's names do not follow
  # a configurable prefix, so neither does this.
  broker_secret_prefixes = [
    "projects/${data.google_project.this.number}/secrets/swarm-tenant-",
    "projects/${data.google_project.this.number}/secrets/swarm-account-",
  ]
}

resource "google_project_iam_member" "broker_version_adder" {
  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = local.sa_member["swarm-quota-broker"]

  condition {
    title       = "swarm tenant and account secrets only"
    description = "The broker may add versions to secrets this platform owns and to no other. 63 of the 77 secrets in this project belong to another team."
    expression  = join(" || ", [for p in local.broker_secret_prefixes : "resource.name.startsWith(\"${p}\")"])
  }
}
