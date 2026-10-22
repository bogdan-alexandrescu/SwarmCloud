# The swarm-workers log view and swarm-api's read of it (lane AU-5 LOG-VIEW,
# part of #1933; docs/design/agent-autonomy.md §3.5, IAM row L-1):
# terraform/infra/log_view.tf.
#
# OWNER DECISION 2026-10-22 (Q4): "A log view filtered to SwarmCloud resources,
# viewAccessor on the view only". saga-agents-staging is SHARED: a project-wide
# log read would hand swarm-api the other team's agents-staging cluster logs.
#
# What is held here -- the PROPERTY, not the spelling:
#
#   * the view is on the project's _Default bucket, id swarm-workers, and says
#     managed-by=swarm-terraform (it has no labels, so its description does);
#   * the filter is EXACTLY the disjunction of the branches log_view.tf lists,
#     each branch a plain conjunction naming one resource type -- so nothing
#     can be OR'ed or negated in around them;
#   * the filter is EVALUATED, not only compared: every entry in
#     log_admitted and log_refused is run through the planned branches, with
#     `=` and `=~` emulated (both RE2, as Cloud Logging's are). It admits
#     k8s_container in the swarm cluster's swarm-tenant-* and swarm-system
#     namespaces, and cloud_run_job / cloud_run_revision named swarm-*, and
#     nothing else: not the other team's cluster, not kube-system, not a name
#     that only CONTAINS swarm-;
#   * swarm-api's grant is a google_logging_log_view_iam_member -- a binding
#     on the view's own policy -- with role roles/logging.viewAccessor;
#   * no account this root grants project roles to holds any predefined role
#     measured to read log entries project-wide
#     (terraform/bootstrap/log-reading-roles.json, read, not restated).
#
# WHAT A MOCK PROVIDER CANNOT SHOW. That Cloud Logging accepts `=~` and OR in a
# view filter, and that the view-level binding lets swarm-api's entries.list
# through. log_view.tf, "WHAT IS NOT VERIFIED", says what would show each.

mock_provider "google" {}

override_data {
  target = data.google_kms_crypto_key_versions.step_spec
  values = {
    versions = [
      {
        id               = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        name             = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/cryptoKeyVersions/1"
        crypto_key       = "projects/saga-agents-staging/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
        version          = 1
        state            = "ENABLED"
        protection_level = "SOFTWARE"
        algorithm        = "EC_SIGN_P256_SHA256"
        public_key       = []
      },
    ]
  }
}

override_data {
  target = module.spec_signing_key.data.google_kms_crypto_key_version.enabled["1"]
  values = {
    version    = 1
    state      = "ENABLED"
    public_key = [{ algorithm = "EC_SIGN_P256_SHA256", pem = "PEM-ONE" }]
  }
}

override_data {
  target          = data.google_project.this
  override_during = plan
  values = {
    number = "209012342332"
  }
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"

  spec_signature_mode      = "legacy"
  spec_legacy_cutover      = "2026-10-01T12:00:00Z"
  spec_signing_key_version = 1

  tenants = {
    eng = {
      kind      = "group"
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
  }

  # Log entries' monitored resources the planned filter is evaluated against
  # (below). Read by this file's assertions only, as graph_service_iam.tftest.hcl
  # reads graph_admitted. Labels are the ones Cloud Logging puts on each type.
  log_admitted = [
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenant-eng", container_name = "agent" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenant-u-someone", container_name = "agent" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-system", container_name = "swarm-egress" } },
    { type = "cloud_run_job", labels = { job_name = "swarm-job-eng-claude-code" } },
    { type = "cloud_run_job", labels = { job_name = "swarm-verify" } },
    { type = "cloud_run_revision", labels = { service_name = "swarm-api" } },
    { type = "cloud_run_revision", labels = { service_name = "swarm-scheduler" } },
  ]
  log_refused = [
    # The other team's cluster, even in a namespace spelled like ours.
    { type = "k8s_container", labels = { cluster_name = "agents-staging", namespace_name = "swarm-tenant-eng", container_name = "agent" } },
    { type = "k8s_container", labels = { cluster_name = "agents-staging", namespace_name = "default", container_name = "api" } },
    # Our cluster, but not a SwarmCloud namespace.
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "kube-system", container_name = "kube-dns" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "default", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "gke-managed-system", container_name = "x" } },
    # Near-misses of each namespace clause: unanchored, a sibling, a suffix.
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "not-swarm-tenant-eng", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenantx", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-system-2", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-other", container_name = "x" } },
    # A container with no namespace label at all.
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", container_name = "x" } },
    # Cloud Run not ours, and names that only contain swarm-.
    { type = "cloud_run_job", labels = { job_name = "agents-staging-migrate" } },
    { type = "cloud_run_job", labels = { job_name = "pre-swarm-job" } },
    { type = "cloud_run_revision", labels = { service_name = "agents-staging-api" } },
    { type = "cloud_run_revision", labels = { service_name = "xswarm-api" } },
    # A revision carrying a job's label (a type/label mix-up must not admit it).
    { type = "cloud_run_revision", labels = { job_name = "swarm-verify", service_name = "agents-staging-api" } },
    # Every other resource type in the project.
    { type = "k8s_node", labels = { cluster_name = "swarm-autopilot", node_name = "n1" } },
    { type = "k8s_cluster", labels = { cluster_name = "swarm-autopilot" } },
    { type = "k8s_pod", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenant-eng" } },
    { type = "gce_instance", labels = { instance_id = "1" } },
    { type = "cloud_function", labels = { function_name = "swarm-x" } },
    { type = "audited_resource", labels = { service = "logging.googleapis.com" } },
    { type = "global", labels = {} },
  ]

  # One clause, as log_view.tf writes them: a field, `=` or `=~`, a quoted
  # value. Group 1 the field, 2 the label name (null for resource.type), 3 the
  # operator, 4 the value.
  clause_re = "^(resource\\.type|resource\\.labels\\.([a-z_]+))(=~|=)\"([^\"]*)\"$"

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"            = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-egress"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "swarm-typesafe-fwd"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-typesafe-fwd@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }
}

run "the_swarm_workers_view_reads_swarmcloud_resources_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # _Default is where GKE containers and Cloud Run already write; a view on
  # another bucket is a view of nothing. swarm-api's logs_query (§3.5) reads
  # this exact id.
  # MUTATION: bucket = ".../buckets/_Required" (or name = "swarm-logs") turns this red.
  assert {
    condition = (
      google_logging_log_view.swarm_workers.bucket == "projects/saga-agents-staging/locations/global/buckets/_Default"
      && google_logging_log_view.swarm_workers.name == "swarm-workers"
    )
    error_message = "the view must be swarm-workers on this project's _Default bucket (location global), where SwarmCloud's container and Cloud Run logs already land"
  }

  # Unlabelable (scripts/lib/unlabelable-types.json), so the description says
  # who manages it and the id carries the platform prefix the guards read.
  # MUTATION: dropping "managed-by=swarm-terraform; " from the description turns this red.
  assert {
    condition = (
      startswith(google_logging_log_view.swarm_workers.description, "managed-by=swarm-terraform")
      && startswith(google_logging_log_view.swarm_workers.name, "swarm-")
    )
    error_message = "the view carries managed-by=swarm-terraform in its description (it has no labels) and an id beginning swarm-"
  }

  # The filter is the branches and nothing else: no trailing OR, no NOT
  # wrapped around them, no branch the evaluation below never sees.
  # MUTATION: appending ` OR resource.type="k8s_container"` to the filter turns this red.
  assert {
    condition     = google_logging_log_view.swarm_workers.filter == join(" OR ", [for b in local.swarm_workers_log_branches : "(${b})"])
    error_message = "the view's filter must be exactly the OR of local.swarm_workers_log_branches, each parenthesised"
  }

  # Each branch only narrows: a plain AND of clauses this file can parse. OR,
  # NOT and Logging's `-` prefix are how a branch admits what it was written
  # to keep out; `:` (has) is a substring test, not a prefix.
  # MUTATION: a branch containing `NOT resource.labels.namespace_name="kube-system"` turns this red.
  assert {
    condition = alltrue(flatten([
      for b in local.swarm_workers_log_branches : [
        for c in split(" AND ", b) : can(regex(var.clause_re, trimspace(c)))
      ]
    ]))
    error_message = "every branch must be a plain AND of resource.type=\"...\" / resource.labels.<name>=\"...\" / =~\"...\" clauses: no OR, NOT, -, : or parentheses inside a branch"
  }

  # Exactly one resource type per branch, from the three §3.5 names.
  # MUTATION: adding a branch `resource.type="k8s_node"` turns this red.
  assert {
    condition = alltrue([
      for b in local.swarm_workers_log_branches :
      length([for c in split(" AND ", b) : c if startswith(trimspace(c), "resource.type=")]) == 1
      && length(setintersection(
        toset([for c in split(" AND ", b) : trimspace(c) if startswith(trimspace(c), "resource.type=")]),
        toset(["resource.type=\"k8s_container\"", "resource.type=\"cloud_run_job\"", "resource.type=\"cloud_run_revision\""]),
      )) == 1
    ])
    error_message = "each branch names exactly one resource.type, and it is k8s_container, cloud_run_job or cloud_run_revision"
  }

  # Every container branch is pinned to the swarm cluster, by the same name the
  # cluster module is given: the other team's agents-staging cluster writes
  # k8s_container entries to this same _Default bucket.
  # MUTATION: dropping the cluster_name clause from a k8s_container branch turns this red.
  assert {
    condition = alltrue([
      for b in local.swarm_workers_log_branches :
      contains([for c in split(" AND ", b) : trimspace(c)], "resource.labels.cluster_name=\"${module.gke_autopilot[0].cluster_name}\"")
      if strcontains(b, "resource.type=\"k8s_container\"")
    ]) && module.gke_autopilot[0].cluster_name == "swarm-autopilot"
    error_message = "every k8s_container branch must name the swarm cluster (swarm-autopilot) by cluster_name"
  }

  # THE PROPERTY. Every admitted entry matches some branch.
  # MUTATION: namespace clause `=~"^swarm-tenant-"` changed to `="swarm-tenant-"` turns this red.
  assert {
    condition = alltrue([
      for e in var.log_admitted : anytrue([
        for b in local.swarm_workers_log_branches : alltrue([
          for c in split(" AND ", b) : (
            regex(var.clause_re, trimspace(c))[2] == "="
            ? (regex(var.clause_re, trimspace(c))[0] == "resource.type" ? e.type : lookup(e.labels, regex(var.clause_re, trimspace(c))[1], "")) == regex(var.clause_re, trimspace(c))[3]
            : length(regexall(regex(var.clause_re, trimspace(c))[3], regex(var.clause_re, trimspace(c))[0] == "resource.type" ? e.type : lookup(e.labels, regex(var.clause_re, trimspace(c))[1], ""))) > 0
          )
        ])
      ])
    ])
    error_message = "the view must admit every SwarmCloud log entry in log_admitted: swarm-tenant-* and swarm-system containers in the swarm cluster, and swarm-* Cloud Run jobs and services"
  }

  # ... and no refused entry matches any branch.
  # MUTATION: the namespace regex unanchored (`=~"swarm-tenant-"`) or the cluster clause dropped turns this red.
  assert {
    condition = alltrue([
      for e in var.log_refused : !anytrue([
        for b in local.swarm_workers_log_branches : alltrue([
          for c in split(" AND ", b) : (
            regex(var.clause_re, trimspace(c))[2] == "="
            ? (regex(var.clause_re, trimspace(c))[0] == "resource.type" ? e.type : lookup(e.labels, regex(var.clause_re, trimspace(c))[1], "")) == regex(var.clause_re, trimspace(c))[3]
            : length(regexall(regex(var.clause_re, trimspace(c))[3], regex(var.clause_re, trimspace(c))[0] == "resource.type" ? e.type : lookup(e.labels, regex(var.clause_re, trimspace(c))[1], ""))) > 0
          )
        ])
      ])
    ])
    error_message = "the view admits an entry in log_refused: another team's cluster or service, a non-SwarmCloud namespace, a name that only contains swarm-, or another resource type"
  }
}

run "swarm_api_reads_the_view_and_only_the_view" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # Row L-1: a binding on the VIEW's own IAM policy. The resource type is the
  # scope; a google_project_iam_member here would be the project.
  # MUTATION: role = "roles/logging.viewer" (or privateLogViewer) turns this red.
  assert {
    condition     = google_logging_log_view_iam_member.swarm_api_reads_swarm_workers.role == "roles/logging.viewAccessor"
    error_message = "swarm-api's role on the view is roles/logging.viewAccessor; logging.viewer reads every log in this SHARED project"
  }

  # The account's email is computed; the mock is told swarm-api's, so the
  # member is known at plan. Any other account's stays unknown, which fails
  # the run as surely as a wrong string would.
  override_resource {
    target          = module.iam.google_service_account.platform["swarm-api"]
    override_during = plan
    values = {
      email = "swarm-api@saga-agents-staging.iam.gserviceaccount.com"
      name  = "projects/saga-agents-staging/serviceAccounts/swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    }
  }

  # MUTATION: member = module.iam.service_account_members["swarm-scheduler"] turns this red.
  assert {
    condition     = google_logging_log_view_iam_member.swarm_api_reads_swarm_workers.member == "serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com"
    error_message = "the view's reader is swarm-api's account and nobody else (row L-1)"
  }

  # The binding names the view this root creates, field for field.
  # MUTATION: name = "_AllLogs" (the bucket's every-log view) turns this red.
  assert {
    condition = (
      google_logging_log_view_iam_member.swarm_api_reads_swarm_workers.parent == "projects/saga-agents-staging"
      && google_logging_log_view_iam_member.swarm_api_reads_swarm_workers.location == "global"
      && google_logging_log_view_iam_member.swarm_api_reads_swarm_workers.bucket == google_logging_log_view.swarm_workers.bucket
      && google_logging_log_view_iam_member.swarm_api_reads_swarm_workers.name == google_logging_log_view.swarm_workers.name
    )
    error_message = "swarm-api's binding must be on exactly the swarm-workers view this root creates, never _AllLogs or another view"
  }

  # Never on the project: no account this root grants project roles holds a
  # predefined role measured to read log entries project-wide. The measured
  # file is read, not restated (it includes viewAccessor, which granted on the
  # project unconditioned reads every view).
  # MUTATION: adding "roles/logging.viewAccessor" to swarm-api's plain roles in modules/iam turns this red.
  assert {
    condition = length(setintersection(
      toset(flatten(values(module.iam.granted_roles))),
      toset(keys(jsondecode(file("../../terraform/bootstrap/log-reading-roles.json")).roles)),
    )) == 0 && length(module.iam.granted_roles["swarm-api"]) > 0
    error_message = "an account holds a project-wide log-reading role (terraform/bootstrap/log-reading-roles.json); swarm-api's one log read is the view binding in log_view.tf"
  }
}
