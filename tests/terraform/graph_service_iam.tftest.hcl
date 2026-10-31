# The shared graph service, the platform side (docs/design/graph-service.md
# §2.1, §3.2, §3.4; lane GS-INFRA, part of #1384): terraform/infra/graph_service.tf
# and kubernetes/swarm-system/swarm-graph.yaml as it renders it.
#
# What is held here:
#
#   * OFF BY DEFAULT nothing exists: no key secret, no reader, no bucket grant,
#     no reference on swarm-api, no manifest. Merging the lane changes nothing
#     in an environment until the owner sets enable_graph_service;
#   * enable_graph_service creates swarm-graph-ticket-key with NO VERSION,
#     read by swarm-api and the service's KSA principal and nobody else, and
#     ONE conditioned roles/storage.objectViewer member on the artifact
#     bucket, held by that principal. Still nothing references the key (a
#     reference to a versionless secret would stop swarm-api starting);
#   * the graph-read condition is EVALUATED, not only compared: every name in
#     graph_admitted and graph_refused is run through the planned expression
#     with extract() emulated as history_purge_iam.tftest.hcl documents. It
#     admits tenants/<one segment>/repos/<one segment>/graph/** of the
#     artifact bucket and nothing else -- not the repository index, not a
#     task, not a nested tenant or repository, not another bucket;
#   * graph_service.serve puts the key on swarm-api by reference and renders
#     the manifest with the image, the key's version reference, the bucket and
#     the kill switch, which ships OFF;
#   * served without a digest renders nothing and fails its check; served or
#     switched on without the layer below it is refused by its check;
#   * every labelable resource carries managed-by=swarm-terraform; the IAM
#     member says it in its condition's description;
#   * served, the stats port (docs/design/graph-answer-cache.md §5.3; lane
#     CACHE-2-NETWORK): ONE reserved INTERNAL address, `graph_stats`, on the
#     subnet swarm-api egresses from, labelled managed-by=swarm-terraform;
#     rendered as the swarm-graph-stats load balancer's address with
#     swarm-api's range in the ingress policy, and swarm-api's
#     SWARM_GRAPH_STATS_URL is http://<that address>:8081. Off, neither the
#     address nor the URL exists, which swarm-api reads as stats not_read.
#
# The infra root's setup is typesafe_fwd.tftest.hcl's, with a swarm-graph
# digest added to image_refs.

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

# The stats load balancer's address is computed by GCE; a fixed one lets the
# rendered manifest and swarm-api's URL be read at plan (live.tftest.hcl does
# the same for the relay's).
override_resource {
  target          = google_compute_address.graph_stats
  override_during = plan
  values = {
    address = "10.10.0.81"
  }
}

variables {
  project_id  = "saga-agents-staging"
  environment = "dev"
  # Read by the dev run's assertions only (live.tftest.hcl does the same).
  tf_root = "../../terraform/infra"

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

  # Object names the graph-read condition is evaluated against (below). Read
  # by this file's assertions only, as tf_root is. Design §3.4's lists, plus a
  # near-miss of each clause.
  graph_admitted = [
    "tenants/a/repos/repo_0123456789abcdef/graph/blobs/x",
    "tenants/a/repos/repo_0123456789abcdef/graph/0123456789abcdef0123456789abcdef01234567/manifest.json",
    "tenants/u-someone/repos/repo_fedcba9876543210/graph/blobs/0f.jsonl.gz",
  ]
  graph_refused = [
    "tenants/a/repos/r/index/0123/repo-index.json",
    "tenants/a/tasks/task_1/result.json",
    "tenants/a/b/repos/r/graph/x",
    "tenants//repos/r/graph/x",
    "tenants/a/repos//graph/x",
    "tenants/a/repos/r/x/graph/y",
    "tenants/a/repos/r/graphs/x",
    "tenants/a/repos/r/graph",
    "tenants/a/tasks/t/repos/r/graph/x",
    "tenants/a/checkpoints/repos/r/graph/x",
    "tenants/a/repos/r",
    "tenants/a",
    "repos/r/graph/x",
    "graph/blobs/x",
  ]

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
    "swarm-graph"              = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-graph@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}

run "off_by_default_nothing_exists" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  assert {
    condition = (
      length(google_secret_manager_secret.graph_ticket_key) == 0
      && length(google_secret_manager_secret_iam_binding.graph_ticket_key_accessor) == 0
      && length(google_storage_bucket_iam_member.graph_service_read) == 0
    )
    error_message = "with enable_graph_service off (the default) no key secret, no reader and no bucket grant exist"
  }

  assert {
    condition     = !contains(keys(local.api_secret_env), "SWARM_GRAPH_TICKET_KEY")
    error_message = "with the graph service off swarm-api carries no swarm-graph-ticket-key reference"
  }

  assert {
    condition = (
      output.swarm_graph_manifest == null
      && output.graph_service.created == false
      && output.graph_service.serving == false
      && output.graph_service.key_secret == ""
    )
    error_message = "with the graph service off there is no manifest to apply"
  }

  assert {
    condition     = local.graph_service_config == { enabled = false }
    error_message = "the graph service's kill switch ships OFF, and is exactly {enabled}"
  }

  assert {
    condition = (
      length(google_compute_address.graph_stats) == 0
      && local.service_env["swarm-api"].SWARM_GRAPH_STATS_URL == ""
    )
    error_message = "with the graph service off no stats address is reserved and swarm-api's SWARM_GRAPH_STATS_URL is empty (read as stats not_read)"
  }
}

run "created_the_key_has_two_readers_and_the_service_reads_graphs_only" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_graph_service = true
  }

  assert {
    condition = (
      google_secret_manager_secret.graph_ticket_key[0].secret_id == "swarm-graph-ticket-key"
      && google_secret_manager_secret.graph_ticket_key[0].labels["managed-by"] == "swarm-terraform"
    )
    error_message = "the ticket key is swarm-graph-ticket-key, labelled managed-by=swarm-terraform"
  }

  assert {
    # swarm-api's member comes from the iam module and is unknown at plan, as
    # in typesafe_fwd.tftest.hcl; the role, the secret and the principal it
    # adds are what plan can show.
    condition = (
      google_secret_manager_secret_iam_binding.graph_ticket_key_accessor[0].role == "roles/secretmanager.secretAccessor"
      && google_secret_manager_secret_iam_binding.graph_ticket_key_accessor[0].secret_id == "swarm-graph-ticket-key"
      && output.graph_service.principal == "principal://iam.googleapis.com/projects/209012342332/locations/global/workloadIdentityPools/saga-agents-staging.svc.id.goog/subject/ns/swarm-system/sa/swarm-graph"
    )
    error_message = "swarm-graph-ticket-key is read by swarm-api (mints) and the service's KSA principal (verifies), nobody else"
  }

  # ONE grant on the bucket: read by exact name, held by the KSA principal
  # directly. No list (a list is evaluated against the bucket, which the
  # condition refuses), no write, no delete, no Firestore.
  assert {
    condition = (
      length(google_storage_bucket_iam_member.graph_service_read) == 1
      && google_storage_bucket_iam_member.graph_service_read[0].role == "roles/storage.objectViewer"
      && google_storage_bucket_iam_member.graph_service_read[0].bucket == "swarm-artifacts-saga-agents-staging"
      && google_storage_bucket_iam_member.graph_service_read[0].member == output.graph_service.principal
    )
    error_message = "the service's GCS grant is one conditioned roles/storage.objectViewer member on the artifact bucket, held by ns/swarm-system/sa/swarm-graph"
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.graph_service_read[0].condition[0].title == "swarm-graph-read-graph-only"
      && startswith(google_storage_bucket_iam_member.graph_service_read[0].condition[0].description, "managed-by=swarm-terraform;")
    )
    error_message = "an IAM member has no labels; its condition's description says managed-by=swarm-terraform"
  }

  # The exact string, so any edit is a deliberate one this file has to follow.
  assert {
    condition     = google_storage_bucket_iam_member.graph_service_read[0].condition[0].expression == "resource.name.startsWith(\"projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/\") && resource.name.extract(\"/objects/tenants/{t}/\") != \"\" && resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/repos/\") && resource.name.extract(\"/repos/{r}/\") != \"\" && resource.name.extract(\"/repos/{r}/\") == resource.name.extract(\"/repos/{r}/graph/\")"
    error_message = "the graph-read condition changed; it must admit tenants/<t>/repos/<r>/graph/** of the artifact bucket and nothing else, and use no matches() (Cloud Storage IAM conditions refuse it)"
  }

  # EVALUATED. The five clauses are read back out of the planned expression
  # (a regex that refuses any other shape, an `||` anywhere, or matches()),
  # each extract() emulated per Google's attribute reference -- the text
  # between the FIRST occurrence of the prefix and the first suffix after it,
  # "" when either is missing -- and applied to every name. The result must
  # equal what a one-segment-tenant, one-segment-repository, graph/ regex says.
  assert {
    condition = [
      for x in [regex("^resource\\.name\\.startsWith\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"]+)\"\\) != \"\" && resource\\.name\\.extract\\(\"([^\"]+)\"\\) == resource\\.name\\.extract\\(\"([^\"]+)\"\\) && resource\\.name\\.extract\\(\"([^\"]+)\"\\) != \"\" && resource\\.name\\.extract\\(\"([^\"]+)\"\\) == resource\\.name\\.extract\\(\"([^\"]+)\"\\)$", google_storage_bucket_iam_member.graph_service_read[0].condition[0].expression)] : {
        for name in concat(
          [for path in concat(var.graph_admitted, var.graph_refused) : "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/${path}"],
          [for path in var.graph_admitted : "projects/_/buckets/other-bucket/objects/${path}"],
          [for path in var.graph_admitted : "projects/_/buckets/swarm-artifacts-saga-agents-staging-x/objects/${path}"],
          ) : name => (
          x[1] == x[2] && x[4] == x[5] && startswith(name, x[0]) && [
            # e = [T, T', R, R'], each template emulated as documented.
            for e in [[
              for tpl in [x[1], x[3], x[4], x[6]] : [
                for ps in [regex("^(.*)\\{[a-z]+\\}(.*)$", tpl)] : [
                  for rest in [length(split(ps[0], name)) < 2 ? "" : join(ps[0], slice(split(ps[0], name), 1, length(split(ps[0], name))))] :
                  length(split(ps[1], rest)) < 2 ? "" : split(ps[1], rest)[0]
                ][0]
              ][0]
            ]] : e[0] != "" && e[0] == e[1] && e[2] != "" && e[2] == e[3]
          ][0]
        )
      }
      ][0] == {
      for name in concat(
        [for path in concat(var.graph_admitted, var.graph_refused) : "projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/${path}"],
        [for path in var.graph_admitted : "projects/_/buckets/other-bucket/objects/${path}"],
        [for path in var.graph_admitted : "projects/_/buckets/swarm-artifacts-saga-agents-staging-x/objects/${path}"],
      ) : name => can(regex("^projects/_/buckets/swarm-artifacts-saga-agents-staging/objects/tenants/[^/]+/repos/[^/]+/graph/", name))
    }
    error_message = "the graph-read condition, evaluated with extract() as documented, must admit exactly tenants/<t>/repos/<r>/graph/** (one tenant and one repository segment) of the artifact bucket and refuse the index, tasks, nested and empty segments and every other bucket"
  }

  # The control: the regex the evaluation is compared with admits the three
  # admitted names and refuses every refused one, so the comparison above
  # could come out either way.
  assert {
    condition = (
      alltrue([for p in var.graph_admitted : can(regex("^tenants/[^/]+/repos/[^/]+/graph/", p))])
      && !anytrue([for p in var.graph_refused : can(regex("^tenants/[^/]+/repos/[^/]+/graph/", p))])
    )
    error_message = "the test's own lists disagree with its oracle"
  }

  assert {
    condition = (
      !contains(keys(local.api_secret_env), "SWARM_GRAPH_TICKET_KEY")
      && output.swarm_graph_manifest == null
    )
    error_message = "created but not served: the key has no version yet, so swarm-api must not reference it and nothing is rendered"
  }

  # No version, ever, from terraform: scripts/create-secrets.sh owns the value.
  assert {
    condition     = length(regexall("google_secret_manager_secret_version|secret_data", file("${var.tf_root}/graph_service.tf"))) == 0
    error_message = "terraform/infra/graph_service.tf declares no secret version and no secret data: the key's value is generated by scripts/create-secrets.sh --graph-ticket-key"
  }
}

run "served_swarm_api_mints_and_the_manifest_renders" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_graph_service = true
    graph_service = {
      serve = true
    }
  }

  assert {
    condition     = local.api_secret_env["SWARM_GRAPH_TICKET_KEY"].secret == "swarm-graph-ticket-key" && local.api_secret_env["SWARM_GRAPH_TICKET_KEY"].version == "latest"
    error_message = "served, swarm-api reads swarm-graph-ticket-key by reference"
  }

  assert {
    condition = (
      output.swarm_graph_manifest != null
      && length(regexall("__[A-Z0-9_]+__", output.swarm_graph_manifest)) == 0
      && strcontains(output.swarm_graph_manifest, "swarm-graph@sha256:dddd")
      && strcontains(output.swarm_graph_manifest, "value: projects/saga-agents-staging/secrets/swarm-graph-ticket-key/versions/latest")
      && strcontains(output.swarm_graph_manifest, "value: swarm-artifacts-saga-agents-staging")
      && strcontains(output.swarm_graph_manifest, "{\"enabled\":false}")
      && strcontains(output.swarm_graph_manifest, sha256(jsonencode(local.graph_service_config)))
    )
    error_message = "the rendered manifest has no placeholder left, the image digest, the key by version reference, the bucket and the switch, OFF"
  }

  assert {
    condition     = output.graph_service.url == "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"
    error_message = "the URL an agent's shim dials is the headless Service's, on 8080, at /mcp"
  }

  # The stats port's address (graph-answer-cache.md §5.3): reserved, INTERNAL,
  # on swarm-api's egress subnet, labelled.
  assert {
    condition = (
      length(google_compute_address.graph_stats) == 1
      && google_compute_address.graph_stats[0].address_type == "INTERNAL"
      && google_compute_address.graph_stats[0].labels["managed-by"] == "swarm-terraform"
      && google_compute_address.graph_stats[0].labels["component"] == "graph-stats"
      && google_compute_address.graph_stats[0].region == "us-central1"
      && google_compute_address.graph_stats[0].name == "${var.name_prefix}-graph-stats"
    )
    error_message = "served, the stats load balancer's address is one reserved INTERNAL google_compute_address labelled managed-by=swarm-terraform"
  }

  assert {
    condition     = local.service_env["swarm-api"].SWARM_GRAPH_STATS_URL == "http://10.10.0.81:8081"
    error_message = "swarm-api's SWARM_GRAPH_STATS_URL is http://<the reserved stats address>:8081"
  }

  # The manifest puts the internal load balancer on that address, on 8081
  # only, and the ingress policy admits swarm-api's range on 8081 only.
  assert {
    condition = length([
      for d in [for s in split("\n---\n", output.swarm_graph_manifest) : yamldecode(s)] : d
      if d.kind == "Service" && try(d.spec.type, "ClusterIP") == "LoadBalancer"
      && try(d.metadata.name, "") == "swarm-graph-stats"
      && try(d.metadata.annotations["networking.gke.io/load-balancer-type"], "") == "Internal"
      && try(d.spec.loadBalancerIP, "") == "10.10.0.81"
      && try([for p in d.spec.ports : p.port], []) == [8081]
    ]) == 1
    error_message = "the rendered swarm-graph-stats Service is an internal load balancer on the reserved address, port 8081 only"
  }

  assert {
    condition = [
      for d in [for s in split("\n---\n", output.swarm_graph_manifest) : yamldecode(s)] : [
        for r in try(d.spec.ingress, []) : [for p in r.ports : p.port] if anytrue([for f in r.from : try(f.ipBlock.cidr, "") == var.subnet_cidr])
      ] if d.kind == "NetworkPolicy"
    ] == [[[8081]]]
    error_message = "swarm-graph-ingress admits swarm-api's egress range (var.subnet_cidr, as swarm-live-ingress) on 8081 only"
  }
}

run "switched_on_the_manifest_carries_the_switch" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_graph_service = true
    graph_service = {
      serve   = true
      enabled = true
    }
  }

  assert {
    condition     = strcontains(output.swarm_graph_manifest, "{\"enabled\":true}") && local.graph_service_config.enabled == true
    error_message = "graph_service.enabled is the ConfigMap's enabled"
  }
}

run "the_switch_without_the_service_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    graph_service = {
      enabled = true
    }
  }

  expect_failures = [check.graph_service_switch_needs_the_service]
}

run "serving_without_the_key_container_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    graph_service = {
      serve = true
    }
  }

  expect_failures = [check.graph_service_serves_only_when_created]

  assert {
    condition     = !contains(keys(local.api_secret_env), "SWARM_GRAPH_TICKET_KEY") && output.swarm_graph_manifest == null
    error_message = "serve without enable_graph_service references no key and renders nothing"
  }
}

run "without_gke_nothing_is_created_and_the_check_says_why" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_graph_service = true
    enable_gke_autopilot = false
  }

  expect_failures = [check.graph_service_needs_gke]

  assert {
    condition = (
      length(google_secret_manager_secret.graph_ticket_key) == 0
      && length(google_storage_bucket_iam_member.graph_service_read) == 0
    )
    error_message = "the service's principal is the Autopilot cluster's; without the cluster nothing is created"
  }
}

# Served with the file's map minus swarm-graph: a run's variables replace the
# whole map.
run "served_without_a_digest_renders_no_manifest_and_fails_its_check" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_graph_service = true
    graph_service = {
      serve = true
    }
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

  expect_failures = [check.graph_service_has_an_image]

  assert {
    condition     = output.swarm_graph_manifest == null
    error_message = "with no swarm-graph digest there is no manifest to apply, never one naming an empty image"
  }
}
