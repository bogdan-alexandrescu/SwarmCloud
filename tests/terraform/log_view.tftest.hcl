# The swarm-workers log view, the bucket and sink that feed it, and swarm-api's
# read of it (lane AU-5 LOG-VIEW, part of #1933; redesign lane LOG-BUCKET,
# part of #1960; docs/design/agent-autonomy.md §3.5, IAM row L-1):
# terraform/infra/log_bucket.tf and terraform/infra/log_view.tf.
#
# OWNER DECISION 2026-10-22 (Q4): "A log view filtered to SwarmCloud resources,
# viewAccessor on the view only". saga-agents-staging is SHARED: a project-wide
# log read would hand swarm-api the other team's agents-staging cluster logs.
# Cloud Logging refused that filter on a view of _Default (release run
# 39742374084: "Filter exceeds the view filter nesting depth limit of 1"), so
# BGD decided, 2026-10-22 (#1960): a dedicated log bucket fed by a sink, with
# the view on that bucket. A sink accepts the full query language.
#
# What is held here -- the PROPERTY, not the spelling:
#
#   * the bucket swarm-worker-logs exists in the project's region with 30-day
#     retention and says managed-by=swarm-terraform (no labels; description);
#   * the sink's filter is EXACTLY the disjunction of the branches
#     log_bucket.tf lists, each branch a plain conjunction naming one resource
#     type -- so nothing can be OR'ed or negated in around them -- and it never
#     names agents-staging;
#   * the filter is EVALUATED, not only compared: every entry in
#     log_admitted and log_refused is run through the planned branches, with
#     `=` and `=~` emulated (both RE2, as Cloud Logging's are). It admits
#     k8s_container in the swarm cluster's swarm-tenant-* namespaces and the
#     worker Cloud Run jobs swarm-job-*, and nothing else: not the other
#     team's cluster, not kube-system or swarm-system, not the platform's
#     services, not a name that only CONTAINS a prefix;
#   * nothing grants roles/logging.bucketWriter -- not the sink's writer, not
#     a platform account: a sink writing a log bucket in its own project needs
#     no grant (BGD, 2026-10-22, #1960);
#   * the view swarm-workers lives on the new bucket, never _Default;
#   * terraform/infra holds NO log-view IAM resource at all: row L-1,
#     swarm-api's viewAccessor on the view, is terraform/bootstrap's
#     (log_view_read.tf; log_view_read.tftest.hcl holds it). CI's deployer
#     cannot set IAM on a log view (release run 39888771458, 2026-10-23:
#     "Error 403: The caller does not have permission"), and BGD decided on
#     2026-10-23 (#1960) that it never gains the power to;
#   * no account this root grants project roles to holds any predefined role
#     measured to read log entries project-wide
#     (terraform/bootstrap/log-reading-roles.json, read, not restated).
#
# WHAT A MOCK PROVIDER CANNOT SHOW. That Cloud Logging accepts the sink's
# filter and the view's, that it authorizes the sink to write its
# same-project bucket with no grant, and that the view-level binding lets
# swarm-api's entries.list through. log_bucket.tf and log_view.tf, "WHAT IS NOT
# VERIFIED", say what would show each.

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

  # Log entries' monitored resources the planned sink filter is evaluated
  # against (below). Read by this file's assertions only, as
  # graph_service_iam.tftest.hcl reads graph_admitted. Labels are the ones
  # Cloud Logging puts on each type.
  log_admitted = [
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenant-eng", container_name = "agent" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenant-u-someone", container_name = "agent" } },
    { type = "cloud_run_job", labels = { job_name = "swarm-job-eng-claude-code" } },
    { type = "cloud_run_job", labels = { job_name = "swarm-job-u-someone-generic" } },
  ]
  log_refused = [
    # The other team's cluster, even in a namespace spelled like ours.
    { type = "k8s_container", labels = { cluster_name = "agents-staging", namespace_name = "swarm-tenant-eng", container_name = "agent" } },
    { type = "k8s_container", labels = { cluster_name = "agents-staging", namespace_name = "default", container_name = "api" } },
    # Our cluster, but not a tenant namespace: the platform's own swarm-system
    # (the egress proxy) is not a worker.
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-system", container_name = "swarm-egress" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "kube-system", container_name = "kube-dns" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "default", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "gke-managed-system", container_name = "x" } },
    # Near-misses of the namespace clause: unanchored, a sibling, a suffix.
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "not-swarm-tenant-eng", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenantx", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-system-2", container_name = "x" } },
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-other", container_name = "x" } },
    # A container with no namespace label at all.
    { type = "k8s_container", labels = { cluster_name = "swarm-autopilot", container_name = "x" } },
    # Cloud Run jobs that are not workers, not ours, or only contain the prefix.
    { type = "cloud_run_job", labels = { job_name = "swarm-verify" } },
    { type = "cloud_run_job", labels = { job_name = "swarm-workspace-apply" } },
    { type = "cloud_run_job", labels = { job_name = "swarm-jobs-x" } },
    { type = "cloud_run_job", labels = { job_name = "agents-staging-migrate" } },
    { type = "cloud_run_job", labels = { job_name = "pre-swarm-job-eng-claude-code" } },
    # The platform's services and the other team's: no service is a worker.
    { type = "cloud_run_revision", labels = { service_name = "swarm-api" } },
    { type = "cloud_run_revision", labels = { service_name = "swarm-scheduler" } },
    { type = "cloud_run_revision", labels = { service_name = "agents-staging-api" } },
    # A revision carrying a job's label (a type/label mix-up must not admit it).
    { type = "cloud_run_revision", labels = { job_name = "swarm-job-eng-claude-code", service_name = "agents-staging-api" } },
    # Every other resource type in the project.
    { type = "k8s_node", labels = { cluster_name = "swarm-autopilot", node_name = "n1" } },
    { type = "k8s_cluster", labels = { cluster_name = "swarm-autopilot" } },
    { type = "k8s_pod", labels = { cluster_name = "swarm-autopilot", namespace_name = "swarm-tenant-eng" } },
    { type = "gce_instance", labels = { instance_id = "1" } },
    { type = "cloud_function", labels = { function_name = "swarm-x" } },
    { type = "audited_resource", labels = { service = "logging.googleapis.com" } },
    { type = "global", labels = {} },
  ]

  # One clause, as log_bucket.tf writes them: a field, `=` or `=~`, a quoted
  # value. Group 1 the field, 2 the label name (null for resource.type), 3 the
  # operator, 4 the value.
  clause_re = "^(resource\\.type|resource\\.labels\\.([a-z_]+))(=~|=)\"([^\"]*)\"$"

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "swarm-egress"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-egress@sha256:8888888888888888888888888888888888888888888888888888888888888888"
    "swarm-typesafe-fwd"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-typesafe-fwd@sha256:9999999999999999999999999999999999999999999999999999999999999999"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

# OFF BY DEFAULT. The switch (swarm_workers_log_view_enabled) is on in dev's
# tfvars only; a root without it plans no bucket, no sink, no view and no
# view name for swarm-api to read. Prod stays off until the
# owner turns it on there.
run "the_swarm_workers_log_path_is_off_by_default" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # MUTATION: default = true on var.swarm_workers_log_view_enabled (or dropping the view's count) turns this red.
  assert {
    condition     = length(google_logging_log_view.swarm_workers) == 0
    error_message = "with swarm_workers_log_view_enabled at its default (false) no google_logging_log_view is planned"
  }

  # MUTATION: dropping the count from the bucket or the sink in log_bucket.tf turns this red.
  assert {
    condition = (
      length(google_logging_project_bucket_config.swarm_worker_logs) == 0
      && length(google_logging_project_sink.swarm_worker_logs) == 0
    )
    error_message = "with the switch off, no worker-log bucket or sink is planned"
  }

  # MUTATION: value = local.swarm_workers_log_view unconditionally turns this red.
  assert {
    condition     = output.swarm_workers_log_view == null
    error_message = "with the view off, the swarm_workers_log_view output is null, so nothing is pointed at a view that does not exist"
  }
}

run "the_worker_log_bucket_keeps_30_days" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    swarm_workers_log_view_enabled = true
  }

  # A user-defined bucket in the project's region (us-central1 here), kept 30
  # days -- owner decision 2026-10-22 (#1960).
  # MUTATION: retention_days = 3650 (or location = "global", or bucket_id = "swarm-logs") turns this red.
  assert {
    condition = (
      length(google_logging_project_bucket_config.swarm_worker_logs) == 1
      && google_logging_project_bucket_config.swarm_worker_logs[0].bucket_id == "swarm-worker-logs"
      && google_logging_project_bucket_config.swarm_worker_logs[0].project == "saga-agents-staging"
      && google_logging_project_bucket_config.swarm_worker_logs[0].location == "us-central1"
      && google_logging_project_bucket_config.swarm_worker_logs[0].retention_days == 30
    )
    error_message = "exactly one bucket, swarm-worker-logs, in this project's region (us-central1), with retention_days = 30"
  }

  # Unlabelable (scripts/lib/unlabelable-types.json), so the description says
  # who manages it and the id carries the platform prefix the guards read.
  # MUTATION: dropping "managed-by=swarm-terraform; " from the bucket's or the sink's description turns this red.
  assert {
    condition = (
      startswith(google_logging_project_bucket_config.swarm_worker_logs[0].description, "managed-by=swarm-terraform")
      && startswith(google_logging_project_sink.swarm_worker_logs[0].description, "managed-by=swarm-terraform")
      && startswith(google_logging_project_sink.swarm_worker_logs[0].name, "swarm-")
    )
    error_message = "the bucket and the sink carry managed-by=swarm-terraform in their descriptions (they have no labels), and the sink's name begins swarm-"
  }
}

run "the_sink_routes_swarmcloud_workers_and_nothing_else" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    swarm_workers_log_view_enabled = true
  }

  # MUTATION: destination = "logging.googleapis.com/projects/saga-agents-staging/locations/global/buckets/_Default" turns this red.
  assert {
    condition = (
      length(google_logging_project_sink.swarm_worker_logs) == 1
      && google_logging_project_sink.swarm_worker_logs[0].project == "saga-agents-staging"
      && google_logging_project_sink.swarm_worker_logs[0].destination == "logging.googleapis.com/projects/saga-agents-staging/locations/us-central1/buckets/swarm-worker-logs"
    )
    error_message = "exactly one sink, in this project, and its destination is the swarm-worker-logs bucket"
  }

  # The filter is the branches and nothing else: no trailing OR, no NOT
  # wrapped around them, no branch the evaluation below never sees.
  # MUTATION: appending ` OR resource.type="k8s_container"` to the filter turns this red.
  assert {
    condition     = google_logging_project_sink.swarm_worker_logs[0].filter == join(" OR ", [for b in local.swarm_workers_log_branches : "(${b})"])
    error_message = "the sink's filter must be exactly the OR of local.swarm_workers_log_branches, each parenthesised"
  }

  # The other team's cluster is never named -- not even to exclude it: an
  # exclusion is one edited character from an inclusion.
  # MUTATION: a branch `resource.labels.cluster_name="agents-staging"` (or `!=` it) turns this red.
  assert {
    condition     = !strcontains(google_logging_project_sink.swarm_worker_logs[0].filter, "agents-staging")
    error_message = "the sink's filter must never name agents-staging, the other team's cluster"
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

  # Exactly one resource type per branch, and only the two a worker writes:
  # its GKE container and its Cloud Run job's execution.
  # MUTATION: adding a branch `resource.type="cloud_run_revision"` turns this red.
  assert {
    condition = alltrue([
      for b in local.swarm_workers_log_branches :
      length([for c in split(" AND ", b) : c if startswith(trimspace(c), "resource.type=")]) == 1
      && length(setintersection(
        toset([for c in split(" AND ", b) : trimspace(c) if startswith(trimspace(c), "resource.type=")]),
        toset(["resource.type=\"k8s_container\"", "resource.type=\"cloud_run_job\""]),
      )) == 1
    ])
    error_message = "each branch names exactly one resource.type, and it is k8s_container or cloud_run_job"
  }

  # Every container branch is pinned to the swarm cluster, by the same name the
  # cluster module is given: the other team's agents-staging cluster writes
  # k8s_container entries in this project too.
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
    error_message = "the sink must admit every SwarmCloud worker log entry in log_admitted: swarm-tenant-* containers in the swarm cluster, and swarm-job-* Cloud Run jobs"
  }

  # ... and no refused entry matches any branch.
  # MUTATION: the namespace regex unanchored (`=~"swarm-tenant-"`), the job regex widened to `=~"^swarm-"`, or the cluster clause dropped turns this red.
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
    error_message = "the sink admits an entry in log_refused: another team's cluster or service, a non-tenant namespace, a platform job or service, a name that only contains a prefix, or another resource type"
  }
}

# NO GRANT TO THE SINK'S WRITER (BGD, 2026-10-22, #1960). The sink writes a
# log bucket in its own project, which Google authorizes automatically ("Route
# logs to supported destinations", Set destination permissions), so the
# release adds no IAM row for it. Nothing anywhere grants
# roles/logging.bucketWriter:
#
#   * by text: no `role = "roles/logging.bucketWriter"` in any .tf file under
#     terraform/infra or terraform/modules, so a grant of any resource name
#     -- google_project_iam_member, a binding, a bucket-scoped member -- is
#     caught, not only the one this run once named;
#   * in the plan: none of the root's own google_project_iam_member grants,
#     and no module.iam account, carries it.
run "nothing_grants_bucket_writer_to_the_sink_or_anyone" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    swarm_workers_log_view_enabled = true
    # Relative to tests/terraform, where `terraform test` runs (make tf-test).
    granting_dirs = ["../../terraform/infra", "../../terraform/modules"]
  }

  # The control: the scan below read real files, so an empty match is a
  # finding and not a wrong path.
  assert {
    condition = (
      alltrue([for d in var.granting_dirs : length(fileset(d, "**/*.tf")) > 0])
      && length(regexall("(?m)^\\s*role\\s*=", file("${var.granting_dirs[0]}/verify.tf"))) > 0
    )
    error_message = "the scan read no file under terraform/infra or terraform/modules, or its role pattern matches nothing in verify.tf; the assertion below would pass on nothing"
  }

  # MUTATION: re-adding the writer grant turns this red.
  assert {
    condition = alltrue([
      for f in flatten([for d in var.granting_dirs : [for p in fileset(d, "**/*.tf") : "${d}/${p}"]]) : length(regexall("(?m)^\\s*role\\s*=\\s*\"roles/logging\\.bucketWriter\"", file(f))) == 0
    ])
    error_message = "a .tf file under terraform/infra or terraform/modules grants roles/logging.bucketWriter; the sink writes a bucket in its own project and needs no grant (BGD, 2026-10-22, #1960)"
  }

  # MUTATION: role = "roles/logging.bucketWriter" on google_project_iam_member.verify_reads_run turns this red.
  assert {
    condition = !contains(concat(
      [google_project_iam_member.verify_reads_firestore.role, google_project_iam_member.verify_reads_run.role],
      [for g in google_project_iam_member.github_hooks_firestore : g.role],
    ), "roles/logging.bucketWriter")
    error_message = "a google_project_iam_member in terraform/infra grants roles/logging.bucketWriter"
  }

  # Not a module.iam account: no platform account gains bucketWriter.
  # MUTATION: adding "roles/logging.bucketWriter" to any account's plain roles in modules/iam turns this red.
  assert {
    condition     = !contains(flatten(values(module.iam.granted_roles)), "roles/logging.bucketWriter")
    error_message = "no platform account holds roles/logging.bucketWriter"
  }
}

run "the_swarm_workers_view_lives_on_the_worker_log_bucket" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    swarm_workers_log_view_enabled = true
  }

  # The switch builds the view, once, and the output names it.
  # MUTATION: count = 0 on google_logging_log_view.swarm_workers (or the output null regardless) turns this red.
  assert {
    condition = (
      length(google_logging_log_view.swarm_workers) == 1
      && output.swarm_workers_log_view == "projects/saga-agents-staging/locations/us-central1/buckets/swarm-worker-logs/views/swarm-workers"
    )
    error_message = "with swarm_workers_log_view_enabled = true exactly one swarm-workers view is planned and the output names it on swarm-worker-logs"
  }

  # The view is on the new bucket, never _Default: a view on _Default cannot
  # keep the other team's logs out (release run 39742374084). Its id is the
  # one swarm-api's logs_query reads.
  # MUTATION: bucket = ".../buckets/_Default" (or name = "swarm-logs") turns this red.
  assert {
    condition = (
      google_logging_log_view.swarm_workers[0].bucket == "projects/saga-agents-staging/locations/us-central1/buckets/swarm-worker-logs"
      && !strcontains(google_logging_log_view.swarm_workers[0].bucket, "_Default")
      && google_logging_log_view.swarm_workers[0].name == "swarm-workers"
    )
    error_message = "the view must be swarm-workers on this project's swarm-worker-logs bucket (us-central1), never on _Default"
  }

  # The sink already chose SwarmCloud's entries, so the view needs only the
  # simplest filter a view accepts: one source check, no nesting.
  # MUTATION: filter = join(" OR ", ...) of the branches (what Cloud Logging refused) turns this red.
  assert {
    condition     = google_logging_log_view.swarm_workers[0].filter == "SOURCE(\"projects/saga-agents-staging\")"
    error_message = "the view's filter is SOURCE(\"projects/saga-agents-staging\") alone: the sink does the selecting"
  }

  # MUTATION: dropping "managed-by=swarm-terraform; " from the view's description turns this red.
  assert {
    condition = (
      startswith(google_logging_log_view.swarm_workers[0].description, "managed-by=swarm-terraform")
      && startswith(google_logging_log_view.swarm_workers[0].name, "swarm-")
    )
    error_message = "the view carries managed-by=swarm-terraform in its description (it has no labels) and an id beginning swarm-"
  }
}

# ROW L-1 IS NOT GRANTED HERE (lane LOG-VIEW-BOOT, BGD 2026-10-23, #1960).
# Release run 39888771458 (head 2aa01cbe) failed applying it at
# `terraform apply, IAM (dev-iam)`: "Error 403: The caller does not have
# permission". CI's deployer holds roles/logging.configWriter, which cannot set
# IAM on a log view. The grant moved to terraform/bootstrap/log_view_read.tf,
# which the owner applies, as verify_cluster_read.tf did for clusterViewer.
run "infra_grants_no_log_view_iam_and_no_project_log_read" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    swarm_workers_log_view_enabled = true
    # Relative to tests/terraform, where `terraform test` runs (make tf-test).
    granting_dirs = ["../../terraform/infra", "../../terraform/modules"]
    view_iam_re   = "(?m)^resource \"google_logging_log_view_iam_(member|binding|policy)\" "
  }

  # The control: the scan read real files, and its pattern finds a log-view
  # IAM resource in a text built to hold one, so an empty match is a finding
  # and not a wrong path or a wrong pattern.
  assert {
    condition = (
      alltrue([for d in var.granting_dirs : length(fileset(d, "**/*.tf")) > 0])
      && length(fileset(var.granting_dirs[0], "log_view.tf")) == 1
      && length(regexall(var.view_iam_re, "resource \"google_logging_log_view_iam_member\" \"x\" {\n}\n")) == 1
    )
    error_message = "the scan read no file under terraform/infra or terraform/modules (or no log_view.tf), or its pattern finds nothing in a text built to hold a log-view grant; the assertion below would pass on nothing"
  }

  # MUTATION: putting google_logging_log_view_iam_member.swarm_api_reads_swarm_workers back in log_view.tf turns this red.
  assert {
    condition = alltrue([
      for f in flatten([for d in var.granting_dirs : [for p in fileset(d, "**/*.tf") : "${d}/${p}"]]) : length(regexall(var.view_iam_re, file(f))) == 0
    ])
    error_message = "a .tf file under terraform/infra or terraform/modules declares a google_logging_log_view_iam_* resource; CI applies that root and cannot set IAM on a log view. Row L-1 is terraform/bootstrap/log_view_read.tf's (BGD, 2026-10-23, #1960)"
  }

  # The view itself stays here: the grant names it, by id.
  # MUTATION: count = 0 on google_logging_log_view.swarm_workers turns this red.
  assert {
    condition     = length(google_logging_log_view.swarm_workers) == 1 && google_logging_log_view.swarm_workers[0].name == "swarm-workers"
    error_message = "terraform/infra still creates the swarm-workers view that bootstrap's L-1 grant is made on"
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
    error_message = "an account holds a project-wide log-reading role (terraform/bootstrap/log-reading-roles.json); swarm-api's one log read is the view binding in terraform/bootstrap/log_view_read.tf"
  }
}
