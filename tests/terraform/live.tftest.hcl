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
      principal = "eng@example.com"
      providers = ["anthropic", "openai"]
    }
  }

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "swarm-live"               = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
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
  # one process), running as the KSA the bucket grant names, on 8080, labelled
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
      && anytrue([for c in d.spec.template.spec.containers : anytrue([for p in try(c.ports, []) : p.containerPort == 8080])])
      if d.kind == "StatefulSet"
    ])
    error_message = "swarm-live is a one-replica StatefulSet, as ns/swarm-system/sa/swarm-live, listening on 8080 under the label the swarm-system policy admits"
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
      && [for p in d.spec.ports : p.port] == [8080]
      && d.spec.selector["app.kubernetes.io/name"] == "swarm-live"
      if d.kind == "Service" && try(d.spec.type, "ClusterIP") == "LoadBalancer"
    ])
    error_message = "swarm-api's path to the relay is an internal load balancer on the reserved address, port 8080"
  }
}

# ---------------------------------------------------------------------------
# The relay's wire as deployed (LB-L8e; owner decisions 2026-10-11 (b), (c)):
# plain ws on 8080, no TLS anywhere, the key by Secret Manager reference, and
# its own NetworkPolicy admitting only swarm-api's egress range and the
# labelled tenant gates.
# ---------------------------------------------------------------------------

run "the_relay_serves_plain_ws_on_8080_behind_its_own_ingress_policy" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # A range no default and no environment uses, so a policy that admits it
  # can only have read it from this variable.
  variables {
    enable_live_browser = true
    subnet_cidr         = "10.77.16.0/20"
  }

  # Every port the manifest names is 8080: both Services, their targets and
  # the container. 8443, the port L6 and L8 were built on, appears nowhere.
  assert {
    condition = alltrue(flatten([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : concat(
        d.kind == "Service" ? [for p in d.spec.ports : p.port == 8080 && p.targetPort == 8080] : [],
        d.kind == "StatefulSet" ? flatten([for c in d.spec.template.spec.containers : [for p in c.ports : p.containerPort == 8080]]) : [],
      )
      ])) && length([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : d if d.kind == "Service"
    ]) == 2 && !strcontains(output.swarm_live_manifest, "8443") && output.live_relay.port == 8080
    error_message = "the relay's Services and container are on 8080 only, and nothing names 8443"
  }

  # No TLS: the one volume is the memory emptyDir segments are encoded in; no
  # secret, projected or CSI volume, no certificate or key mount, no TLS
  # setting and no key file.
  assert {
    condition = alltrue(flatten([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : [
        [for v in d.spec.template.spec.volumes : v.name == "tmp" && try(v.emptyDir.medium, "") == "Memory" && length(keys(v)) == 2],
        flatten([for c in d.spec.template.spec.containers : [for m in c.volumeMounts : m.mountPath == "/tmp"]]),
        flatten([for c in d.spec.template.spec.containers : [for e in c.env : !strcontains(e.name, "TLS") && !strcontains(e.name, "CERT") && e.name != "SWARM_LIVE_KEY_FILE"]]),
      ] if d.kind == "StatefulSet"
    ])) && !strcontains(lower(output.swarm_live_manifest), "tls.crt") && !strcontains(output.swarm_live_manifest, "8443")
    error_message = "the relay mounts no certificate and no key: its one volume is the memory emptyDir at /tmp"
  }

  # The environment the relay reads (swarm_live.main, swarm_common.config):
  # exactly these three, the key by version reference.
  assert {
    condition = alltrue([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] :
      alltrue([for c in d.spec.template.spec.containers : {
        for e in c.env : e.name => e.value
        } == {
        PROJECT_ID            = "saga-agents-staging"
        ARTIFACT_BUCKET       = output.live_relay.artifact_bucket
        SWARM_LIVE_KEY_SECRET = join("/", ["projects", "saga-agents-staging", "secrets", output.live_relay.key_secret, "versions", "latest"])
      }])
      if d.kind == "StatefulSet"
    ])
    error_message = "the relay is given PROJECT_ID, ARTIFACT_BUCKET and SWARM_LIVE_KEY_SECRET (a version reference), and nothing else"
  }

  # Invariant 7 per container, and every resource the relay asks for named.
  assert {
    condition = alltrue(flatten([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : [
        for c in d.spec.template.spec.containers :
        c.resources.requests == c.resources.limits
        && toset(keys(c.resources.limits)) == toset(["cpu", "memory", "ephemeral-storage"])
      ] if d.kind == "StatefulSet"
    ]))
    error_message = "the relay's requests equal its limits for cpu, memory and ephemeral-storage (invariant 7)"
  }

  # Invariant 6: no Spot selector, toleration or class, in any spelling.
  assert {
    condition = !strcontains(lower(output.swarm_live_manifest), "spot") && alltrue([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] :
      try(d.spec.template.spec.nodeSelector, null) == null && try(d.spec.template.spec.tolerations, null) == null
      if d.kind == "StatefulSet"
    ])
    error_message = "the relay asks for no Spot capacity (invariant 6)"
  }

  # The NetworkPolicy's sources: ONE peer carrying both selectors (a tenant
  # namespace AND a pod the dispatcher labelled), and swarm-api's range read
  # from var.subnet_cidr; on 8080, nothing else. It selects the relay pod and
  # sits before the Services and the StatefulSet, so the relay is never
  # selected by no policy.
  assert {
    condition = length([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : d
      if d.kind == "NetworkPolicy"
      ]) == 1 && alltrue([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] :
      d.metadata.name == "swarm-live-ingress"
      && d.metadata.annotations["swarm.saga.xyz/swarm-api-egress-cidr"] == "10.77.16.0/20"
      && d.spec.podSelector == { matchLabels = { "app.kubernetes.io/name" = "swarm-live" } }
      && d.spec.policyTypes == ["Ingress"]
      && length(d.spec.ingress) == 2
      && alltrue([for r in d.spec.ingress : r.ports == [{ protocol = "TCP", port = 8080 }] && length(r.from) == 1])
      && d.spec.ingress[0].from[0] == {
        namespaceSelector = {
          matchLabels      = { "app.kubernetes.io/part-of" = "swarm" }
          matchExpressions = [{ key = "swarm-tenant", operator = "Exists" }]
        }
        podSelector = { matchLabels = { "swarm-live" = "enabled" } }
      }
      && d.spec.ingress[1].from[0] == { ipBlock = { cidr = "10.77.16.0/20" } }
      if d.kind == "NetworkPolicy"
    ]) && output.live_relay.swarm_api_egress_cidr == "10.77.16.0/20"
    error_message = "swarm-live-ingress admits only labelled tenant gates and swarm-api's subnet range (var.subnet_cidr), on TCP 8080"
  }

  assert {
    condition = index([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : d.kind
      ], "NetworkPolicy") < index([
      for d in [for s in split("\n---\n", output.swarm_live_manifest) : yamldecode(s)] : d.kind
    ], "StatefulSet")
    error_message = "the relay's NetworkPolicy is applied before its StatefulSet"
  }

  # On, the relay is a deployed image: its digest is in the image_refs output
  # scripts/plan.sh pins a non-deploy plan to.
  assert {
    condition     = output.image_refs["swarm-live"] == "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:7777777777777777777777777777777777777777777777777777777777777777"
    error_message = "with the relay on, image_refs carries swarm-live's digest"
  }
}

run "off_the_relay_is_not_a_deployed_image" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # A state applied before the release built swarm-live has no digest for it;
  # with the relay off that must still plan, so it is not a deployed image.
  # The `image_refs` output may still record a swarm-live digest it was given:
  # it records every gated image's digest (#1878), deployed or not.
  assert {
    condition     = !contains(local.deployed_images, "swarm-live")
    error_message = "with enable_live_browser off, swarm-live is not a deployed image"
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

  # The sidecar follows the relay, not the variable: without the cluster there
  # is no relay, so no browser pod is given a display to stream from.
  assert {
    condition     = local.service_env["swarm-scheduler"].LIVE_BROWSER_SIDECAR == "false"
    error_message = "without enable_gke_autopilot the scheduler renders no display sidecar (LIVE_BROWSER_SIDECAR=false)"
  }
}

# ---------------------------------------------------------------------------
# swarm-api's side of the wire (LB-L8d; owner decisions 2026-10-11 (b), (c))
# ---------------------------------------------------------------------------

run "off_swarm_api_has_no_relay_address_and_no_key_reference" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == ""
    error_message = "with enable_live_browser off swarm-api has no relay address, which swarm_api.settings reads as no relay"
  }

  # A reference to swarm-live-key before create-secrets.sh --live-key added a
  # version would stop swarm-api starting.
  assert {
    condition     = !contains(keys(local.api_secret_env), "SWARM_LIVE_KEY")
    error_message = "with enable_live_browser off swarm-api carries no swarm-live-key reference"
  }
}

run "on_swarm_api_carries_the_relay_address_and_the_keys_reference_never_the_key" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_live_browser = true
  }

  # Plain ws to the internal load balancer's reserved address, on the port
  # both integration lanes fixed (decision (b)).
  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == "ws://${output.live_relay.internal_address}:8080"
    error_message = "swarm-api's SWARM_LIVE_URL is ws://<the relay's reserved internal address>:8080"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == "ws://10.10.0.42:8080"
    error_message = "swarm-api's SWARM_LIVE_URL renders the reserved address"
  }

  # The key by Secret Manager reference, through the secret_env merge, next to
  # the child key's: a secret name and a version, nothing else.
  assert {
    condition = (
      local.api_secret_env["SWARM_LIVE_KEY"].secret == google_secret_manager_secret.live_key.secret_id
      && local.api_secret_env["SWARM_LIVE_KEY"].version == "latest"
      && length(keys(local.api_secret_env["SWARM_LIVE_KEY"])) == 2
    )
    error_message = "swarm-api reads SWARM_LIVE_KEY by reference to swarm-live-key/latest"
  }

  # Never the key itself: no plain environment variable carries it, and no
  # Terraform-managed secret version exists for it.
  assert {
    condition = (
      !contains(keys(local.service_env["swarm-api"]), "SWARM_LIVE_KEY")
      && !contains(keys(local.service_env["swarm-api"]), "SWARM_LIVE_KEY_SECRET")
      && !strcontains(file("${var.tf_root}/live.tf"), "google_secret_manager_secret_version")
    )
    error_message = "swarm-live-key reaches swarm-api only as a Secret Manager reference, never as a plain value or a managed version"
  }
}

# ---------------------------------------------------------------------------
# The scheduler's side, and dev switched on (LB-Lon, owner decision
# 2026-10-11 ~15:00 PDT)
# ---------------------------------------------------------------------------

# Off, a claude-code-browser pod is the pod it always was: no display sidecar,
# no live gate (scheduler.settings.SchedulerSettings.live_browser_sidecar).
run "off_the_scheduler_renders_no_display_sidecar" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition     = local.service_env["swarm-scheduler"].LIVE_BROWSER_SIDECAR == "false"
    error_message = "with enable_live_browser off the scheduler reads LIVE_BROWSER_SIDECAR=false"
  }
}

# dev.tfvars as committed turns the live browser on, and the plan those values
# make carries both halves of the wire: the relay (its grants, address and
# manifest; swarm-api's SWARM_LIVE_URL) AND the scheduler's display sidecar.
# Before LB-Lon nothing set LIVE_BROWSER_SIDECAR, so a relay could stand with
# no browser pod ever streaming to it.
run "dev_turns_the_live_browser_on_end_to_end" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  # dev's two switches, as committed. Read as text: a terraform test cannot
  # load a tfvars file into a run, so the run below sets the same values and
  # these two assertions hold dev.tfvars to them.
  variables {
    enable_live_browser  = true
    enable_gke_autopilot = true
  }

  assert {
    condition     = length(regexall("(?m)^enable_live_browser\\s*=\\s*true\\b", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets enable_live_browser = true (LB-Lon)"
  }

  assert {
    condition     = length(regexall("(?m)^enable_gke_autopilot\\s*=\\s*true\\b", file("${var.tf_root}/../environments/dev/dev.tfvars"))) == 1
    error_message = "terraform/environments/dev/dev.tfvars sets enable_gke_autopilot = true, without which the live browser grants nothing"
  }

  assert {
    condition     = local.service_env["swarm-scheduler"].LIVE_BROWSER_SIDECAR == "true"
    error_message = "dev renders the scheduler with LIVE_BROWSER_SIDECAR=true, so a claude-code-browser pod carries the display sidecar and the live gate"
  }

  assert {
    condition     = output.live_relay.enabled && output.swarm_live_manifest != null
    error_message = "dev renders the relay enabled, with a manifest to apply"
  }

  assert {
    condition     = length(google_storage_bucket_iam_member.live_relay_records) == 1 && length(google_compute_address.live_relay) == 1
    error_message = "dev plans the relay's one bucket grant and its internal address"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_LIVE_URL == "ws://10.10.0.42:8080"
    error_message = "dev renders swarm-api with the relay's address"
  }
}
