# The live browser's infrastructure (docs/design/live-browser.md §3.3, §7,
# §8; lane LB-L8, part of #1030): terraform/infra/live.tf, the 7-day rule in
# terraform/modules/storage/main.tf, and kubernetes/swarm-system/swarm-live.yaml
# as live.tf renders it.
#
# What is held here, from the build plan's row:
#
#   * the 7-day SUFFIX rule exists. Recordings are deleted 7 days after they
#     are written, by `age` and whatever their customTime (LB-4), and the rule
#     reaches nothing else in the bucket -- not a task artifact, not a
#     checkpoint, not the repository index;
#   * the relay's grant is CREATE-ONLY and CONDITIONED on `/live/`: one
#     roles/storage.objectCreator binding on the artifact bucket, whose
#     condition admits only a tenant's `.../attempts/<a>/live/` key ending in
#     one of the two recording suffixes. No read, no delete, no Firestore;
#   * every resource that can carry a label carries managed-by=swarm-terraform
#     (an IAM binding cannot; destroy-guard.jq treats those as policy edges);
#   * NO SECRET VERSION. swarm-live-key is a container and its readers;
#     scripts/create-secrets.sh --live-key adds the value nobody sees.
#
# The infra root's setup is child_tasks_infra.tftest.hcl's: the step-spec key
# versions are mocked so the root plans at all, and the project number is
# overridden so the relay's Workload Identity principal is known at plan.

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

# The relay's internal load balancer address is computed by GCE; a fixed one
# lets the rendered manifest be read at plan.
override_resource {
  target          = google_compute_address.live_relay
  override_during = plan
  values = {
    address = "10.10.0.42"
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
      principal = "eng@saga.xyz"
      providers = ["anthropic", "openai"]
    }
  }

  image_refs = {
    "swarm-api"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"      = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"            = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "agent-runtime-base"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  }

  # Read as text by the assertion that lists live.tf's resources.
  tf_root = "../../terraform/infra"

  # §7's two suffixes, stated once for this file. The storage module's rule
  # and live.tf's grant condition are each held to this list, and so to each
  # other.
  live_suffixes = [".live.webm", ".live.jsonl"]

  # Keys as the relay writes them (§7), and keys it must never reach.
  live_segment = "tenants/eng/tasks/task_1/attempts/att_1/live/rec-000001.live.webm"
  live_actions = "tenants/u-ana/tasks/task_2/attempts/att_3/live/actions-000002.live.jsonl"
  not_live = [
    "tenants/eng/tasks/task_1/attempts/att_1/artifacts/summary.md",
    "tenants/eng/tasks/task_1/attempts/att_1/checkpoints/ck_1/archive.tar.gz",
    "tenants/eng/verdicts/wf_1/task_3/review.json",
    "tenants/eng/repos/repo_0123456789abcdef/index/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/repo-index.json",
  ]
}

# ---------------------------------------------------------------------------
# The 7-day rule (terraform/modules/storage/main.tf)
# ---------------------------------------------------------------------------

run "recordings_are_deleted_seven_days_after_they_are_written" {
  command = plan

  module {
    source = "../../terraform/modules/storage"
  }

  variables {
    labels        = { "managed-by" = "swarm-terraform" }
    bucket_suffix = "saga-agents-staging"
    tenants       = ["eng"]
  }

  # Exactly one rule, a Delete keyed on `age` 7 and the two suffixes. `age`
  # and not customTime: a recording without customTime is still deleted
  # (LB-4 is 7 days whatever the object carries). with_state ANY: the bucket
  # is versioned, so a Delete on the live object only makes it noncurrent, and
  # a LIVE-only rule would leave every recording behind as a noncurrent
  # version for noncurrent_version_retention_days (30). ANY deletes that
  # version on the next pass, its age being its creation's.
  assert {
    condition = length([
      for r in google_storage_bucket.artifacts.lifecycle_rule : r
      if one(r.action).type == "Delete"
      && one(r.condition).age == 7
      && one(r.condition).with_state == "ANY"
      && toset(coalesce(one(r.condition).matches_suffix, [])) == toset(var.live_suffixes)
    ]) == 1
    error_message = "the artifact bucket has one Delete rule at age 7, with_state ANY, matching exactly the .live.webm and .live.jsonl suffixes (docs/design/live-browser.md §7, LB-4)"
  }

  # The rule is narrowed by suffix ALONE: a prefix would have to be a tenant's,
  # and a runtime personal tenant (`u-<slug>`) is never in the map.
  assert {
    condition = alltrue([
      for r in google_storage_bucket.artifacts.lifecycle_rule :
      length(coalesce(one(r.condition).matches_prefix, [])) == 0
      if length(coalesce(one(r.condition).matches_suffix, [])) > 0
    ])
    error_message = "the recording rule must not be narrowed by a tenant prefix: personal tenants are created at runtime and would keep their recordings forever"
  }

  # Both of the relay's keys match it, from a mapped and a personal tenant.
  assert {
    condition = alltrue([
      for k in [var.live_segment, var.live_actions] : anytrue([
        for r in google_storage_bucket.artifacts.lifecycle_rule :
        anytrue([for s in coalesce(one(r.condition).matches_suffix, []) : endswith(k, s)])
        if one(r.action).type == "Delete" && one(r.condition).age == 7
      ])
    ])
    error_message = "a recording segment or action log the relay writes is not matched by the 7-day rule"
  }

  # Nothing else does: not an artifact, a checkpoint, a verdict or the index.
  # An `age` Delete that reached a checkpoint would delete a PARKED task's only
  # way back (storage/main.tf, "Artifacts and logs expire on a clock;
  # checkpoints do not").
  assert {
    condition = alltrue(flatten([
      for r in google_storage_bucket.artifacts.lifecycle_rule : [
        for k in var.not_live : !anytrue([
          for s in coalesce(one(r.condition).matches_suffix, []) : endswith(k, s)
        ])
      ]
      if one(r.action).type == "Delete" && one(r.condition).age == 7
    ]))
    error_message = "the 7-day rule matches an object that is not a recording"
  }

  assert {
    condition     = google_storage_bucket.artifacts.labels["managed-by"] == "swarm-terraform"
    error_message = "the artifact bucket carries managed-by=swarm-terraform"
  }
}

# ---------------------------------------------------------------------------
# The relay's grants (terraform/infra/live.tf)
# ---------------------------------------------------------------------------

run "off_by_default_the_relay_holds_nothing" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # The container exists either way, so `create-secrets.sh --live-key` can add
  # a version before anything reads it, as swarm-child-key's does.
  assert {
    condition     = google_secret_manager_secret.live_key.secret_id == "swarm-live-key"
    error_message = "the HMAC key the relay and swarm-api share is swarm-live-key (docs/design/live-browser.md §3.3), the name create-secrets.sh --live-key adds versions to"
  }

  assert {
    condition     = google_secret_manager_secret.live_key.labels["managed-by"] == "swarm-terraform"
    error_message = "every resource carries managed-by=swarm-terraform, or make destroy refuses it"
  }

  assert {
    condition     = length(google_storage_bucket_iam_member.live_relay_records) == 0
    error_message = "with enable_live_browser off the relay holds no grant on the artifact bucket"
  }

  assert {
    condition     = length(google_compute_address.live_relay) == 0
    error_message = "with enable_live_browser off no address is reserved for the relay"
  }

  assert {
    condition     = output.swarm_live_manifest == null && output.live_relay.enabled == false
    error_message = "with enable_live_browser off there is no relay manifest to apply"
  }
}

run "on_the_relay_may_only_create_recordings" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_live_browser = true
  }

  # ONE grant on the bucket, roles/storage.objectCreator: create, and the
  # multipart steps of a create, with no get, list or delete. Without delete
  # the relay cannot even overwrite a segment it wrote (§7: "no read, no
  # delete").
  assert {
    condition = (
      length(google_storage_bucket_iam_member.live_relay_records) == 1
      && google_storage_bucket_iam_member.live_relay_records[0].role == "roles/storage.objectCreator"
      && google_storage_bucket_iam_member.live_relay_records[0].bucket == "swarm-artifacts-saga-agents-staging"
    )
    error_message = "the relay's GCS grant is one roles/storage.objectCreator binding on the artifact bucket"
  }

  # Held by the relay's Kubernetes service account directly, through Workload
  # Identity Federation for GKE: no Google service account to create, impersonate
  # or grant on. The namespace and the KSA are part of the principal, so a
  # tenant pod (another namespace) cannot present it.
  assert {
    condition     = google_storage_bucket_iam_member.live_relay_records[0].member == "principal://iam.googleapis.com/projects/209012342332/locations/global/workloadIdentityPools/saga-agents-staging.svc.id.goog/subject/ns/swarm-system/sa/swarm-live"
    error_message = "the grant is held by ns/swarm-system/sa/swarm-live in GKE's workload identity pool, and by nothing else"
  }

  # The condition: a tenant's object, under an attempt's live/, with one of
  # the two suffixes. Each clause is required.
  assert {
    condition = alltrue([
      for clause in [
        "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/\")",
        "resource.name.extract(\"/attempts/{attempt}/live/\") != \"\"",
        "resource.name.endsWith(\".live.webm\")",
        "resource.name.endsWith(\".live.jsonl\")",
      ] : strcontains(google_storage_bucket_iam_member.live_relay_records[0].condition[0].expression, clause)
    ])
    error_message = "the relay's grant must be conditioned on a tenants/ key, an attempt's /live/ directory and the two recording suffixes"
  }

  # Clauses are ANDed: the only `||` is between the two suffixes, inside their
  # own parentheses. An `||` anywhere else would admit a key that meets one
  # clause and not the others.
  assert {
    condition = (
      length(regexall("\\|\\|", google_storage_bucket_iam_member.live_relay_records[0].condition[0].expression)) == 1
      && strcontains(google_storage_bucket_iam_member.live_relay_records[0].condition[0].expression, "(resource.name.endsWith(\".live.webm\") || resource.name.endsWith(\".live.jsonl\"))")
      && length(regexall("&&", google_storage_bucket_iam_member.live_relay_records[0].condition[0].expression)) == 2
    )
    error_message = "the condition is three clauses joined by &&, the suffixes alone joined by ||"
  }

  assert {
    condition     = toset(output.live_relay.recording_suffixes) == toset(var.live_suffixes)
    error_message = "the grant's suffixes are the 7-day rule's: a suffix one has and the other lacks is a recording kept forever, or one the relay cannot write"
  }

  # The key: swarm-api and the relay read it; nobody else.
  assert {
    condition = (
      google_secret_manager_secret_iam_binding.live_key_accessor.role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.live_key_accessor.secret_id == "swarm-live-key"
      && contains(google_secret_manager_secret_iam_binding.live_key_accessor.members, output.live_relay.principal)
    )
    error_message = "swarm-live-key is read through one authoritative secretAccessor binding that includes the relay"
  }

  assert {
    condition = (
      google_compute_address.live_relay[0].address_type == "INTERNAL"
      && google_compute_address.live_relay[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the relay's load-balancer address is internal and carries managed-by=swarm-terraform"
  }

  # live.tf declares exactly these resources and these alone. In particular
  # no google_secret_manager_secret_version (a managed version puts the key's
  # plaintext in state), no project-level grant, no Firestore role and no
  # Google service account.
  assert {
    condition = toset([
      for m in regexall("(?m)^resource \"([a-z0-9_]+)\" \"([a-z0-9_]+)\"", file("${var.tf_root}/live.tf")) : "${m[0]}.${m[1]}"
      ]) == toset([
      "google_secret_manager_secret.live_key",
      "google_secret_manager_secret_iam_binding.live_key_accessor",
      "google_storage_bucket_iam_member.live_relay_records",
      "google_compute_address.live_relay",
    ])
    error_message = "live.tf declares the key, its readers, the relay's one bucket grant and its address; anything else -- a secret version above all -- is a change to this design"
  }

  assert {
    condition = (
      !strcontains(file("${var.tf_root}/live.tf"), "google_secret_manager_secret_version")
      && !strcontains(file("${var.tf_root}/live.tf"), "secret_data")
      && google_secret_manager_secret.live_key.annotations["swarm-populated-by"] == "scripts/create-secrets.sh --live-key"
    )
    error_message = "no secret version in Terraform: scripts/create-secrets.sh --live-key adds the value"
  }
}

# ---------------------------------------------------------------------------
# The relay's manifest (kubernetes/swarm-system/swarm-live.yaml, rendered)
# ---------------------------------------------------------------------------

run "the_rendered_relay_is_one_pod_with_the_grants_identity" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_live_browser = true
  }

  assert {
    condition     = length(regexall("__[A-Z0-9_]+__", output.swarm_live_manifest)) == 0
    error_message = "every placeholder in swarm-live.yaml is rendered"
  }

  # Every object is managed-by=swarm-terraform and lives in swarm-system (the
  # namespace itself aside); no cluster-scoped grant.
  assert {
    condition = alltrue([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] :
      d.metadata.labels["managed-by"] == "swarm-terraform"
      && (d.kind == "Namespace" ? d.metadata.name : try(d.metadata.namespace, "")) == "swarm-system"
      && !contains(["ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"], d.kind)
    ])
    error_message = "every object in swarm-live.yaml carries managed-by=swarm-terraform, sits in swarm-system and grants no Kubernetes RBAC"
  }

  # A single-replica StatefulSet (§3.3: both ends of a session must meet in
  # one process), running as the KSA the bucket grant names, on 8443, labelled
  # as swarm-system-live.yaml's policy selects it.
  assert {
    condition = length([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : d
      if d.kind == "StatefulSet"
      ]) == 1 && alltrue([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] :
      d.metadata.name == "swarm-live"
      && d.spec.replicas == 1
      && d.spec.template.metadata.labels["app.kubernetes.io/name"] == "swarm-live"
      && d.spec.template.spec.serviceAccountName == "swarm-live"
      && endswith(output.live_relay.principal, "/ns/${d.metadata.namespace}/sa/${d.spec.template.spec.serviceAccountName}")
      && anytrue([for c in d.spec.template.spec.containers : anytrue([for p in try(c.ports, []) : p.containerPort == 8443])])
      if d.kind == "StatefulSet"
    ])
    error_message = "swarm-live is a one-replica StatefulSet, as ns/swarm-system/sa/swarm-live, listening on 8443 under the label the swarm-system policy admits"
  }

  # Invariant 7, per container, init containers included; and the image by
  # digest.
  assert {
    condition = alltrue(flatten([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : [
        for c in concat(d.spec.template.spec.containers, try(d.spec.template.spec.initContainers, [])) :
        c.resources.requests == c.resources.limits && length(keys(c.resources.limits)) >= 2
        && can(regex("@sha256:[0-9a-f]{64}$", c.image))
      ] if d.kind == "StatefulSet"
    ]))
    error_message = "every relay container has requests == limits (invariant 7) and an image by digest"
  }

  # Invariant 6: nothing asks for Spot.
  assert {
    condition     = !strcontains(output.swarm_live_manifest, "spot")
    error_message = "Spot is disabled platform-wide (invariant 6)"
  }

  # swarm-api reaches it through an internal passthrough load balancer on the
  # address live.tf reserved.
  assert {
    condition = output.live_relay.internal_address == "10.10.0.42" && length([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : d
      if d.kind == "Service" && try(d.spec.type, "ClusterIP") == "LoadBalancer"
      ]) == 1 && alltrue([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] :
      d.metadata.annotations["networking.gke.io/load-balancer-type"] == "Internal"
      && d.spec.loadBalancerIP == "10.10.0.42"
      && [for p in d.spec.ports : p.port] == [8443]
      && d.spec.selector["app.kubernetes.io/name"] == "swarm-live"
      if d.kind == "Service" && try(d.spec.type, "ClusterIP") == "LoadBalancer"
    ])
    error_message = "swarm-api's path to the relay is an internal load balancer on the reserved address, port 8443"
  }
}

run "the_relay_runs_only_on_gke" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_live_browser  = true
    enable_gke_autopilot = false
  }

  expect_failures = [check.live_browser_needs_gke]
}
