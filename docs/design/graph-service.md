# One shared graph service for every agent runtime, instead of a snapshot per pod

**Status: built on `main` except staging's removal (recorded 2026-10-27,
part of #1384).** Written 2026-10-16 as a design (lane GS-DESIGN). Of §11's
lanes, GS-MEASURE, GS-CORE, GS-TICKET, GS-INDEXDOC, GS-PUSHAPI,
GS-SETTING, GS-SVC, GS-HOOKS, GS-SHIM, GS-PACK, GS-INFRA and GS-WORKER have
landed. GS-INDEXDOC departs from §11: the repo-index document is a third
manifest key, `document_shards`, not a layer of `index_shards`, because
swarm-api refuses a manifest whose `index_shards` names a layer it does not
know (`repo_graph_shards.py`'s header). In dev the service serves and
answers (`graph_service = { serve = true, enabled = true }`,
`terraform/environments/dev/dev.tfvars`, 2026-10-16), and only a workspace
whose `code_intelligence.graph_delivery` is `service` uses it; every other
workspace still stages. What remains:

* **Widening** to every workspace (§8.2 step 5), once the `graph_ready`
  marks show a fallback rate under 1% over 7 days. The owner decides it.
* **GS-RETIRE (not built):** staging is deleted for GKE attempts only after
  every attempt has run on `service` for 14 days (§8.2 step 6), so it waits
  on the widening.

The owner's decisions of 2026-10-15 (~17:15 PDT, recorded in #1384's body)
are taken as given and are marked **(owner)**. Every other choice below was
the design lane's, is marked **(proposed)**, and is listed in §12; #1384's
comments record the owner's answers.

It extends [knowledge-graph.md](knowledge-graph.md) §5 and
[repo-index.md](../repo-index.md) §2.3, §3.3-§3.4 and §5. It replaces one
row of knowledge-graph.md §5.1's table, "a per-step snapshot plus a local MCP
server", and nothing else: the prompt-composed sections (planner, review,
fix) and the swarm-api read routes are unchanged.

Every code citation is `path::symbol`, read at `4f54cef` on 2026-10-16.
Symbols move less than lines do, so search for the symbol.

---

## 0. The answer in one screen

* **A read-only Deployment `swarm-graph` in `swarm-system`** serves the eight
  existing tools plus `call_graph` and `index` over MCP streamable HTTP. It
  imports graphmcp's `snapshot` and `tools` modules from the worker package.
  It does not fork them: §2 names the one seam each module gains.
* **Every request carries an attempt ticket** that swarm-api mints per
  attempt under its own purpose and MAC key. The ticket pins the tenant, the
  repository, the generation, the index commit and the **manifest digest**.
  The service derives every GCS path and every cache key from the ticket,
  never from the request. Its only IAM is object read on
  `tenants/*/repos/*/graph/**` (§3).
* **A stdio shim in the pod**, `python -m agent_worker.graphmcp.shim`, is
  registered as `swarm-graph` in every runtime's MCP config. It forwards each
  call and then marks rows `stale` from **one `git status` per answer**,
  against a private git index built from the step's base commit (§4).
* **Caches never need invalidating**, because a commit's index is immutable
  and every key holds the manifest digest (§5). There are three layers:
  * a per-replica LRU of decoded shards, warmed on first sight of a promoted digest (§5.4, owner Q5);
  * a per-replica cache of query results, keyed by (tenant, digest, tool,
    canonical args);
  * GCS as the store of record.
* **Sync** (§6):
  * GitHub App push events reach a new public receiver that verifies the
    signature and asks swarm-api to run the existing per-registration poll
    for that repository at once;
  * the existing 5-minute poll stays as the fallback;
  * the existing scheduled full re-index stays.
* **The context pack is built from the service** (§7). The stored setting
  still decides whether it is built (invariant 10).
* **Migration** (§8):
  * A per-workspace stored setting, `code_intelligence.graph_delivery`,
    switches between `staged` and `service`.
  * An attempt that cannot reach the service falls back to staging, so
    turning the service on never opens a gap.
  * Staging is deleted only after the service has carried every attempt for
    14 days.
* **Today's staging cost is not recorded anywhere** (§9). No timer, no
  event and no metric records it. This lane measured the bytes: **783
  sequential GCS GETs and about 9.4 MB per staged start**, at `4f54cef`.
  The seconds are still unmeasured, and the first lane adds that
  measurement before anything is switched.
* **No frozen-contract change is needed** (§11).

---

## 1. What exists today, and what this replaces

| piece | today | after |
|---|---|---|
| pick the index for a step | `apps/agent-worker/agent_worker/graphstage.py::_stage`: decides by `graphstage.py::decide`, lists `graphstage.py::_candidates` (the promoted `current_sha` first, then up to 21 kept versions, newest first), and proves ancestry with `graphstage.py::prove_ancestry` | **unchanged**. This choice now **pins** the index instead of copying it (§4.1) |
| deliver it | `graphstage.py::write_snapshot` downloads the manifest and **every** blob it names into `work/.swarm-context/snapshot/`, capped at `graphstage.py::MAX_SNAPSHOT_BYTES` (64 MiB) | nothing is downloaded. The worker mints a ticket (§3) |
| serve it | one `python -m agent_worker.graphmcp` per agent (`graphstage.py::server_argv`), which decodes shards into up to `apps/agent-worker/agent_worker/graphmcp/snapshot.py::CACHE_RAW_BYTES` (48 MiB raw) of the pod's memory | the `swarm-graph` Deployment. The pod runs a shim that holds no shard |
| staleness | `apps/agent-worker/agent_worker/graphmcp/freshness.py::Freshness.observe`: three git calls per answer (`update-index --refresh`, `diff --name-only`, `ls-files --others`) | one `git status` per answer, in the shim (§4.3) |
| context pack | `apps/agent-worker/agent_worker/contextpack/build.py::build_pack` opens the staged `Snapshot` | the same code over a `Snapshot` whose blobs come from the service (§7) |
| sync | `apps/swarm-api/swarm_api/repoindex.py::RepoIndex.poll` every 5 minutes (`terraform/modules/scheduler/jobs.tf` job `repo_index_poll`). `repositories.py::ON_CHANGE_VALUES` is `("poll", "off")`, so `webhook` is refused | push events trigger the same per-registration decision at once. The poll stays (§6) |

Owner decision 1 states what changes: "Nothing is copied into a pod, so this
adds no per-agent storage or start-up time." §5.3 of knowledge-graph.md said
"It makes no network call and holds no credential". It now reads: **one
network hop to one in-cluster service, gated by a ticket that names one
tenant, one repository and one manifest digest.**

---

## 2. The service

### 2.1 Shape

| property | value | why |
|---|---|---|
| kind | `Deployment` `swarm-graph`, namespace `swarm-system` | (owner) Read-only and stateless. Every byte it serves can be refetched from GCS, so it needs no ledger and no StatefulSet. `kubernetes/swarm-system/swarm-typesafe-fwd.yaml` is the template |
| replicas | 2, `maxUnavailable: 0`, `maxSurge: 1`, PodDisruptionBudget `minAvailable: 1` | (proposed) One replica is a single point of failure for every agent's tools. Two replicas halve each one's cache hit rate, which §5 accepts because a miss costs one GCS read |
| resources | cpu `1`, memory `3Gi`, ephemeral-storage `1Gi`, **requests == limits** | Invariant 7. The memory sizing is in §5.1 |
| pod security | PSS `restricted`, uid 10001, read-only root, no capabilities, seccomp `RuntimeDefault`, `automountServiceAccountToken: false` | the same posture as every swarm-system manifest |
| identity | KSA `swarm-graph`, a **direct Workload Identity principal**, no GSA | the swarm-live, swarm-egress and swarm-typesafe-fwd pattern (`terraform/infra/typesafe_fwd.tf`). There is nothing for the CI deployer to create, and no bootstrap `serviceAccountAdmin` grant |
| Service | headless (`clusterIP: None`), TCP 8080 | so the tenant-side egress policy's pod selector is what admits a connection, as `swarm-typesafe-fwd.yaml` does. Peers find each other through it for warming (§5.4) |
| ingress | NetworkPolicy `swarm-graph-ingress`: from namespaces labelled `app.kubernetes.io/part-of: swarm` **and** `swarm-tenant` Exists, on TCP 8080 | every swarm-system pod sits behind `kubernetes/network-policies/swarm-system-live.yaml` (`swarm-system-default-deny-ingress`) |
| tenant egress | new `kubernetes/network-policies/allow-graph-service.yaml`, listed in `kubernetes/render.py::TENANT_FILES`: every pod in the tenant namespace may reach `swarm-system`/`app.kubernetes.io/name: swarm-graph` on 8080 | the shape of `allow-egress-proxy.yaml`. It has no pod-label selector, because any agent pod may hold a graph ticket, so the dispatcher needs no new label |
| kill switch | ConfigMap `swarm-graph-config` `{"enabled": bool}`, mounted as a directory and re-read; a `config-sha256` annotation rolls the pods | the forwarder's pattern. Off means every call gets 503 `disabled`, and the worker then falls back (§8) |

The name `swarm-graph` is already the MCP server's name in the agent's
config (`apps/agent-worker/agent_worker/graphmcp/server.py::SERVER_NAME`).
**It stays the MCP name.** The shim registers under it, so the agent sees the
same server and the same tool names it sees today. The Kubernetes objects
share the name, which is a different namespace of names and causes no
collision.

### 2.2 Protocol

MCP **streamable HTTP**, stateless mode, one endpoint:

    POST /mcp            the graph ticket as the bearer credential (Authorization header)
                         Content-Type: application/json
                         body: one JSON-RPC 2.0 request (no batches)
    → 200 application/json, one JSON-RPC response
    GET  /healthz        → 200 when the key ring is loaded and the switch is on

* **No `Mcp-Session-Id` and no server-sent-event stream.** Every answer is a
  single response. A session would need replica affinity, and nothing here
  needs state between calls: the ticket carries the pin.
* The methods are the four that `apps/agent-worker/agent_worker/graphmcp/server.py::GraphServer.handle`
  answers today: `initialize`, `ping`, `tools/list` and `tools/call`.
* **The service reuses `GraphServer.handle` itself.** The service builds a
  `GraphServer` per request around the pinned snapshot and an empty
  observation (§4.4), then calls `handle(message)`. JSON-RPC parsing, error
  codes and the tool dispatch are therefore one implementation, not two.
* A JSON-RPC error is an HTTP 200 carrying the error. The HTTP status speaks
  only for the ticket and the service:
  * 401: no ticket, or a ticket that does not verify;
  * 409: the manifest does not match the ticket's digest;
  * 404: the index is missing;
  * 503: the service is disabled or the store is unreachable.

  §10 says what the shim tells the agent in each case.
* There is no separate `GET /v1/blobs/{sha256}` route. §7 explains why the
  context pack does not need one.

### 2.3 The tools: eight existing, two new

The eight in `apps/agent-worker/agent_worker/graphmcp/tools.py::TOOLS` are
`search`, `context`, `impact`, `tests_for`, `neighbours`, `territory`,
`similar_changes` and `rules_for`. They are served unchanged: the same
schemas, the same 4 KiB `tools.py::ANSWER_CAP`, and the same `next_cursor`
paging. Two tools are added **(proposed)**, for the owner's "the call graph or
index for X" and "paged whole-index reads".

| tool | arguments | answers | cost |
|---|---|---|---|
| `call_graph` | `symbol` (id, name or file path), `direction` ∈ `callers`/`callees`/`both` (default `both`), `depth` 1-3 (default 2), `cursor` | edges `{from, to, kind, depth, confidence}` breadth-first, bounded by `tools.py::IMPACT_MAX_NODES`. The head carries `nodes`, `edges` and `truncated`. Zero callers is `UNKNOWN` (`tools.py::UNKNOWN_CALLERS`), as `impact` already says | expensive. It is the main case the result cache exists for (§5.2) |
| `index` | `layer` ∈ `snapshot.py::LAYERS` + `INDEX_LAYERS` + `CONTEXT_LAYERS` + `document`; `key` (a module, or a section of `document`); `path` (alternative to `key`: the module that holds a path); `cursor` | without `key` or `path`: the layer's keys, each with its row count. With one: that shard's rows, paged at 4 KiB in the stored order. This is the **whole index, page by page**, and "the index for X" is `index(layer, path=X)` | one shard read per page |

* `index`'s `document` layer is repo-index.md §2.1's document: `modules`,
  `entry_points`, `routes`, `test_layout`, `test_map`, `territory`,
  `commands`, `hot_spots` and `notes`, one section per key.
  * Today that document is stored outside `graph/`, at
    `tenants/<t>/repos/<id>/index/<sha>/repo-index.json`.
  * The owner limited the service's IAM to `graph/**` (§3.4). So lane
    GS-INDEXDOC (§11) has the indexer write the document **as one more
    `index_shards` layer in the graph manifest**, keyed by section.
  * Its bytes are then covered by the manifest digest the ticket pins. That
    gives one pin and one IAM grant, and the service never reads `index/`.
  * Question Q7 asks the owner to choose between this and a second grant on
    `index/**`.
* **The schema budget.** knowledge-graph.md §7.7's ceiling is 2k tokens of
  schema plus instructions, held by
  `tests/unit/worker/test_graphmcp_budgets.py`. The two schemas are written
  to fit, and the test stays as it is. If they do not fit, the descriptions
  are shortened. The ceiling is not raised.

### 2.4 How it reuses graphmcp rather than forking it

The service imports `agent_worker.graphmcp.snapshot`,
`agent_worker.graphmcp.tools` and `agent_worker.graphmcp.server` from the
`swarm-agent-worker` wheel. It holds no copy, the way
`apps/swarm-egress/Dockerfile` installs `swarm-common` and `swarm-redaction`
from the local wheelhouse. Three seams are added, each with tests in lane
GS-CORE:

1. **`Snapshot` gets a blob source.** Today
   `apps/agent-worker/agent_worker/graphmcp/snapshot.py::Snapshot._decode`
   reads `root/blobs/<sha256>.jsonl.gz`.
   * The change is a `BlobSource` protocol:
     `decoded(hexdigest) -> _Shard`, which returns a verified and decoded
     shard.
   * `Snapshot.__init__` keeps its signature and builds a `DirectorySource`
     over `root`, with today's per-snapshot LRU. Every existing caller and
     test is unchanged.
   * A new `Snapshot.from_manifest(raw, *, manifest_digest, source)` takes
     manifest bytes the caller already holds and verifies them against the
     digest before parsing, exactly as `__init__` does today.
   * The service's source is a tenant-scoped view of the process-wide shard
     LRU (§5.1).
   * The sha256-against-name check and the `SnapshotError` refusal stay in
     `_decode`'s successor, **on both sources**. A blob is never decoded
     unchecked, whichever source supplies it.
2. **Each `tools.py` handler splits "compute" from "page".**
   * Today each `tool_*` both computes `(head, rows)` and calls
     `tools.py::page`.
   * After the split, `compute(name, snap, args) -> Computed(head, rows,
     expand)` is pure over the snapshot and the arguments, **with no
     observation**. `call` pages it.
   * The service caches `Computed`, not answer text (§5.2). A second page of
     an `impact` answer is then a slice of the cached rows, not a second
     traversal.
   * The two places that read the observation inside a handler move into
     `page`:
     * `tool_impact` marks the head stale;
     * `tool_context` and `tool_neighbours` `_mark` their heads.

     `page` marks the head by its `symbol` or `file` key, as it marks rows.
   * Every existing answer is byte-identical afterwards, and a test holds
     this over the fixture snapshot (`tests/unit/worker/graphmcp_fixtures.py`)
     for every tool.
3. **`tools.remark(text, obs) -> text`.** Given a service answer computed
   with the empty observation, `remark` returns the answer the local server
   would have given under `obs`. It is the shim's whole job (§4.4), so it
   lives beside `_mark` and `page` and shares them.

What the service adds is only HTTP, the ticket, the GCS source, the caches
and warming. It adds no graph logic.

---

## 3. The ticket

### 3.1 Minting

A new route on swarm-api, the shape of
`apps/swarm-api/swarm_api/routes/live.py::mint_egress_ticket` and
`apps/swarm-api/swarm_api/routes/typesafe_ticket.py::mint_typesafe_ticket`:

    POST /v1/attempts/{attempt_id}/graph-ticket
      the worker's ID token as the bearer credential     (Authorization or X-Serverless-Authorization)
      X-Swarm-Attempt-Proof / X-Swarm-Attempt-Timestamp  (the attempt key's Ed25519 signature)
      body {tenant_id, task_id, attempt_id, lease_id, generation, index_sha}
    → 200 {ticket, expires_in, index_sha, manifest_digest}

* **The request proof.** The worker signs with
  `apps/agent-worker/agent_worker/handoff.py::sign_headers` under the purpose
  `swarm-graph-ticket-request/v1`. The signed message has the same shape as
  `apps/swarm-api/swarm_api/typesafe_ticket.py::typesafe_ticket_message`.
  swarm-api checks the ID token and the proof as
  `typesafe_ticket.py::TypesafeTicketService` does.
* **The fence (invariant 5).** The agent must be `RUNNING` with no cancel
  requested, and the lease must be live with the body's generation. If not,
  swarm-api answers 409 and mints nothing. The worker treats that 409 as
  `apps/agent-worker/agent_worker/egress_ticket.py::mint`'s caller treats
  its own: a fenced exit that does not touch the lease.
* **The repository comes from the agent, never the body.** swarm-api reads
  the agent's own `tenant_id` and `repository_url` from Firestore, resolves
  the registration with
  `apps/swarm-api/swarm_api/repositories.py::repo_id_for`, and checks that
  the registration's `tenant_id` is the agent's.
  * The body names only `index_sha`, the commit the worker's ancestry proof
    chose (§4.1). swarm-api accepts it only when
    `repositories/{repo_id}/index_versions/{index_sha}` exists **and** has a
    `graph_digest`.
  * The `manifest_digest` in the ticket is read from that document, which
    only swarm-api writes, in `repoindex.py::RepoIndex._promote_from`.
  * Any agent in the tenant can write objects under `tenants/<t>/`, so a
    digest taken from a request or from GCS could name a rewritten manifest.
    Taking it from Firestore closes that.
* **The stored setting decides (invariant 10).** If
  `apps/swarm-api/swarm_api/codeintel.py::effective_context` says `off` for
  this tenant and registration, swarm-api refuses with 403
  `context_disabled`. A caller cannot ask for graph access that the
  workspace's setting does not give.

### 3.2 Format, purpose and key

| | value |
|---|---|
| request purpose | `swarm-graph-ticket-request/v1` |
| MAC domain | `b"swarm-graph-ticket/v1\0"` |
| fields, exact set | `tenant_id, repo_id, task_id, attempt_id, generation, index_sha, manifest_digest, exp, nonce` |
| encoding | `<b64url(json, sort_keys, compact)>.<b64url(HMAC-SHA256(key, DOMAIN ‖ payload))>`, unpadded, the same as `typesafe_ticket.py::mint_typesafe_ticket` |
| lifetime | setting `graph_ticket_seconds`, env `GRAPH_TICKET_SECONDS`, default 600, clamped to 60-900 |
| key | Secret Manager `swarm-graph-ticket-key`, at least 32 bytes, generated by `scripts/create-secrets.sh`. swarm-api reads it as env `SWARM_GRAPH_TICKET_KEY` by secret reference. The service reads it over REST with its own identity and polls it for rotation, as `apps/swarm-egress/swarm_egress/main.py::read_key` does |
| rotation | the verifier holds a key ring of the current key and the previous one, keeping the previous key for 900 + 60 s, as `apps/swarm-egress/swarm_egress/server.py::KeyRing` does |

* **Its own key, not a shared one.** Every ticket kind here has its own
  Secret Manager key and its own domain: `swarm-egress-key`,
  `swarm-typesafe-fwd-key` and `swarm-live-key`. A graph-service compromise
  then cannot mint an egress ticket, and an egress compromise cannot read a
  graph.
* **The reference verifier is swarm-api's.** `verify_graph_ticket(keys,
  ticket, now)` in the new `apps/swarm-api/swarm_api/graph_ticket.py`
  mirrors `typesafe_ticket.py::verify_typesafe_ticket`:
  * the field set must match exactly;
  * `exp > now`, and `exp <= now + 900 + 1`;
  * the MAC is checked in constant time.

  The service's verifier is its own module. A parity test holds the
  constants and a cross-verification equal, as
  `apps/agent-worker/agent_worker/egress_ticket.py` and
  `apps/swarm-egress/swarm_egress/ticket.py` are held equal today.
* **The generation is fenced at minting, not at each read.** The service has
  no Firestore access, so it cannot read the live generation.
  * A stale worker cannot renew: its next mint is a 409. Its last ticket
    lives for at most `graph_ticket_seconds`.
  * Within that window, the ticket reads only the graph the attempt was
    already entitled to read.
  * This is the same trade the egress and TypeSafe verifiers make (neither
    checks the generation at verify time).

### 3.3 What the service does with it

1. It verifies the ticket, or answers 401. The agent never sees the reason
   text beyond `ticket_refused` (§10).
2. It checks `tenant_id` against the tenant id pattern and `repo_id` against
   `repo_[0-9a-f]{16}`. It checks `index_sha` is 40 hex characters and
   `manifest_digest` is `sha256:<64 hex>`. A ticket that fails these is
   refused **before** any path is built from it.
3. It builds the object names **only from the ticket**, through the same
   layout as `apps/swarm-api/swarm_api/repograph.py::manifest_key` and
   `repograph.py::blob_key`:

       tenants/<tenant_id>/repos/<repo_id>/graph/<index_sha>/manifest.json
       tenants/<tenant_id>/repos/<repo_id>/graph/blobs/<sha256>.jsonl.gz

   No tool takes a repository, tenant, commit or path argument that reaches
   the store. As today, `swarm-graph` "has no repository argument at all"
   (knowledge-graph.md §5.3).
4. It reads the manifest through the manifest cache keyed `(tenant_id,
   manifest_digest)`. It verifies the bytes against the digest, or answers
   409 `manifest_mismatch`. It then opens
   `Snapshot.from_manifest(raw, manifest_digest=..., source=TenantSource(tenant_id, repo_id))`.
5. Every cache read and write takes `tenant_id` as the first element of its
   key (§5). **Decision 3 (owner): nothing is shared across workspaces.**
   This holds even when two workspaces register the same public repository
   and their blobs have equal digests. Each workspace pays for its own copy.

### 3.4 IAM (a proposal; the owner applies IAM)

The service needs exactly three grants: object read on the graph prefix of
every tenant, read on its own ticket key, and nothing on Firestore.
swarm-api's existing grants already cover what it does here. It reads
Firestore and gets one new secret accessor row.

The graph-read condition uses the `extract()` boundary technique that
`terraform/modules/iam/bindings.tf` documents for
`api_history_purge_deleter`. Cloud Storage conditions have no `matches()`,
and a per-tenant list would miss tenants created at runtime. **T** is the
first segment after `tenants/`. It must be non-empty and equal the text
before the first `/repos/`, so the name is `tenants/T/repos/...` with T
exactly one segment. **R** is the text between the first `/repos/` and the
next `/`. It must be non-empty and equal the text before the first `/graph/`
after that `/repos/`, so the name is `tenants/T/repos/R/graph/...` with R
exactly one segment.

Proposed `terraform/infra/graph_service.tf`:

```hcl
locals {
  graph_service_namespace = "swarm-system" # namespace-prefix-exempt: the platform's own namespace, not a tenant's
  graph_service_ksa       = "swarm-graph"
  graph_service_on        = var.enable_graph_service && var.enable_gke_autopilot
  graph_service_image     = lookup(var.image_refs, "swarm-graph", "")

  graph_service_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${local.graph_service_namespace}/sa/${local.graph_service_ksa}"

  graph_ticket_key_version_name = local.graph_service_on ? "projects/${var.project_id}/secrets/${google_secret_manager_secret.graph_ticket_key[0].secret_id}/versions/latest" : ""

  # tenants/<one segment>/repos/<one segment>/graph/... and nothing else.
  graph_read_condition = {
    title       = "swarm-graph-read-graph-only"
    description = "managed-by=swarm-terraform; swarm-graph reads tenants/<t>/repos/<r>/graph/** only."
    expression = join(" && ", [
      "resource.name.startsWith(\"projects/_/buckets/${module.storage.artifact_bucket_name}/objects/tenants/\")",
      "resource.name.extract(\"/objects/tenants/{t}/\") != \"\"",
      "resource.name.extract(\"/objects/tenants/{t}/\") == resource.name.extract(\"/objects/tenants/{t}/repos/\")",
      "resource.name.extract(\"/repos/{r}/\") != \"\"",
      "resource.name.extract(\"/repos/{r}/\") == resource.name.extract(\"/repos/{r}/graph/\")",
    ])
  }

  graph_ticket_key_secret_env = local.graph_service_on ? {
    SWARM_GRAPH_TICKET_KEY = {
      secret  = google_secret_manager_secret.graph_ticket_key[0].secret_id
      version = "latest"
    }
  } : {}
}

resource "google_storage_bucket_iam_member" "graph_service_read" {
  count = local.graph_service_on ? 1 : 0

  bucket = module.storage.artifact_bucket_name
  role   = "roles/storage.objectViewer"
  member = local.graph_service_principal

  condition {
    title       = local.graph_read_condition.title
    description = local.graph_read_condition.description
    expression  = local.graph_read_condition.expression
  }
}

resource "google_secret_manager_secret" "graph_ticket_key" {
  count = local.graph_service_on ? 1 : 0

  project   = var.project_id
  secret_id = "swarm-graph-ticket-key"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  deletion_protection = var.deletion_protection

  labels = merge(local.labels, {
    "component" = "graph-ticket-key"
  })

  annotations = {
    "swarm-populated-by" = "generated, never shown (docs/design/graph-service.md section 3.2)"
  }
}

# swarm-api mints, swarm-graph verifies; nobody else. Authoritative.
resource "google_secret_manager_secret_iam_binding" "graph_ticket_key_accessor" {
  count = local.graph_service_on ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.graph_ticket_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  members = [
    module.iam.service_account_members["swarm-api"],
    local.graph_service_principal,
  ]
}
```

and `terraform/infra/locals.tf`'s `api_secret_env` gains
`local.graph_ticket_key_secret_env` in its `merge(...)`.

Notes on these rows:

* **Object read, not list.** The service fetches by exact name and never
  lists. A list call is evaluated against the bucket, so this condition
  refuses it, and that is intended. Retention never needs the service to
  list: the indexer's sweep owns deletion (repo-index.md §2.3).
* **`roles/storage.objectViewer`, conditioned.** It is the same predefined
  role that `terraform/modules/tenancy/main.tf` grants each worker, there
  conditioned to one tenant's prefix.
* A `tests/terraform/graph_service_iam.tftest.hcl` evaluates the expression
  against admitted and refused names, in the style of
  `history_purge_iam.tftest.hcl`:
  * admitted: `tenants/a/repos/repo_0123456789abcdef/graph/blobs/x`;
  * refused: `tenants/a/repos/r/index/...`, `tenants/a/tasks/...`,
    `tenants/a/b/repos/r/graph/x`, `tenants//repos/r/graph/x`,
    `tenants/a/repos//graph/x`, `tenants/a/repos/r/x/graph/y`.
* **No further row.** Owner decision Q5 (2026-10-15): warming is on first
  sight (§5.4), so the service holds read on `graph/**` and nothing else. No
  Pub/Sub grant.

---

## 4. The local shim

### 4.1 What the worker does at start (pin, ticket, register)

`apps/agent-worker/agent_worker/lifecycle.py::Worker._graph_context_env` calls
`graphstage.prepare` once per attempt, as it does today. With
`graph_delivery: service` (§8), `_stage` runs exactly as it does now, up to
the point where it has a `version` whose ancestry is proven. It then:

1. **Pins instead of copying.** Where today it calls
   `graphstage.py::write_snapshot`, it calls a new `graphstage.pin(...)`.
   * `pin` writes `work/.swarm-context/pin.json`: `{index_sha,
     manifest_digest, base_sha, behind_by, basis, changed}`. This is
     `freshness.json`'s record (`graphmcp/snapshot.py::load_freshness_record`)
     plus the digest.
   * `changed` is the same `git diff --name-only index_sha..base_sha`
     staging computes.
   * The file holds no credential, so its home in the checkpointed `work/`
     is correct. A resumed attempt reads the same pin.
2. **Mints the first ticket** through a new
   `apps/agent-worker/agent_worker/graph_ticket.py`, which mirrors
   `egress_ticket.py::mint`. It signs with the attempt key
   (`children.py::AttemptKey`) and sends through `children.py::ChildApi`.
   * It writes the ticket to `private/graph-ticket`, mode 0600, by
     write-to-temporary-then-rename.
   * `private/` is never checkpointed and never uploaded
     (`apps/agent-worker/agent_worker/jevkey.py` records why).
   * The ticket is renewed when a third of its life is left, as
     `egress_ticket.py::BrowserEgress` renews. Retries back off from 5 s to
     a 60 s cap.
   * A 409 is a fenced exit.
3. **Probes the service once:** an `initialize` with the ticket, with a 2 s
   timeout. If the probe fails, the attempt falls back to `write_snapshot`
   (§8.2), and `stage.json` records `delivery: "staged"` and the reason.
4. **Returns the env.** `SWARM_CONTEXT` as today, `SWARM_GRAPH_PIN=<pin.json>`
   and `SWARM_GRAPH_TICKET_FILE=<private/graph-ticket>`. It does not set
   `SWARM_GRAPH_SNAPSHOT`.

### 4.2 Registration in every runtime's MCP config

The argv is fixed and names no host, bucket or sha. That is
knowledge-graph.md §5.4 unchanged:

    [sys.executable, "-I", "-m", "agent_worker.graphmcp.shim",
     "--pin", <pin.json>, "--ticket-file", <private/graph-ticket>, "--workdir", <checkout>]

The service URL is not an argument. It is a worker setting,
`SWARM_GRAPH_URL`, default `http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp`,
which the worker passes in the server entry's `env`. A caller has no way to
set it (invariant 10).

| runtime | today | change |
|---|---|---|
| **claude-code** | `apps/agent-worker/agent_worker/runners/claude_code.py::write_mcp_config` writes `mcpServers["swarm-graph"]` as `{"type":"stdio","command":..., "args": graphstage.server_argv(...)}` into `work/.swarm-claude-code/mcp.json`, passed as `--mcp-config` | the same key, with argv from a new `graphstage.shim_argv(pin, ticket_file, workdir)` when the stage record says `delivery: "service"` |
| **claude-code-browser** | the same runner (`agent_worker.runners.claude_code`), so the same function | nothing beyond claude-code's change. A test asserts the browser profile's config carries the shim |
| **codex** | `apps/agent-worker/agent_worker/runners/codex.py::graph_tools_args` emits `-c mcp_servers.swarm-graph.command=…` and `.args=…` | the same keys with the shim's argv, plus `-c mcp_servers.swarm-graph.env={"SWARM_GRAPH_URL": …}` |

The prompt line naming the tool, in
`apps/agent-worker/agent_worker/runners/cliagent.py::run_cli_agent`, does
not change. The agent is offered the same server with the same tools, plus
§2.3's two new ones.

### 4.3 One git call per answer, and the stale guarantee

knowledge-graph.md §5.2 requires that a row on a path changed since the
index, **or dirtied by the agent in this step**, is marked `stale` with the
reason. Today `freshness.py::Freshness.observe` makes three git calls per
answer. The shim makes **one**. It works like this:

* **Once, at shim start** (not per answer), it builds a **private index of
  the base commit**: `GIT_INDEX_FILE=<tmp>/index git read-tree <base_sha>`,
  then `git update-index -q --refresh`.
  * This replaces `Freshness._private_index`, which copies the agent's own
    index.
  * The agent's index and its lock are never touched, as today.
* **For each answer**, it runs one command:

      GIT_INDEX_FILE=<tmp>/index git -C <checkout> -c core.quotepath=off status \
          --porcelain=v2 -z --no-renames --untracked-files=normal --ignore-submodules=dirty

  Because the index **is the base tree**:
  * the "staged" column (HEAD against the index) is every path committed
    since the base;
  * the "unstaged" column (the index against the working tree) is every
    path changed in the tree against the base;
  * untracked entries are paths absent from the base and not ignored. A
    directory comes back with a trailing `/`, which is today's
    `Observation.dirty_dirs`.

  The union of the three is the dirty set against the base **commit**, the
  quantity `observe` computes today.
* **Over-marking.** A file committed and then reverted is listed and marked
  dirty. That errs in the safe direction: a row marked stale that is not
  stale is a hint, while a stale row unmarked is a wrong answer.
* **The private index updates its own stat cache.** `--no-optional-locks` is
  *not* passed, because the index and its lock are the shim's own. So the
  first answer pays the full re-hash (measured at 240 ms on this repository,
  `freshness.py`'s docstring) and later answers do not. This replaces the
  per-answer `update-index --refresh`.
* **Timeouts.** A git call over `freshness.py::GIT_TIMEOUT_SECONDS` (5 s),
  or one that fails, makes the block say `"dirty": "unknown"`, exactly as
  today. It never says zero.
* **Changed since the index.** This comes from `pin.json`'s `changed`, read
  once.
  * When the index commit is not in the clone, which happens on a shallow
    clone with the ancestry proven by the recorded `head_relation`,
    `changed` is absent.
  * In that case the block says `changed_since_index: "unknown"` with
    today's note. `freshness.py::Freshness.establish` already does exactly
    this, and the shim calls it.

This lands **in `freshness.py` itself** (lane GS-CORE), so the local server
used during migration gets the one-call path too. A test builds a repository
covering six cases and asserts the new dirty set equals the old three-call
set, or is a superset on the reverted-commit case only. The six cases are:
staged, committed, deleted, added-and-committed, renamed, and an untracked
directory.

### 4.4 The answer: service computes, shim marks

* The shim is `GraphServer` with a remote `tools/call`, the pattern of
  `apps/agent-worker/agent_worker/graphmcp/remote.py::RemoteGraphServer`.
* It answers `initialize`, `ping` and `tools/list` **locally**, from the same
  package's `server.py` and `tools.py`. The agent's tool list is therefore
  the shim's own version's, and the service down never empties it.
* For `tools/call`, the shim does four things:
  1. It reads the ticket file. A stat is cheap, and the file is re-read only
     when its mtime changes.
  2. It POSTs the JSON-RPC message with the ticket, with a 10 s timeout.
  3. It takes `obs = freshness.observe()`, the one git call.
  4. It returns `tools.remark(service_text, obs)`.
* `remark` replaces the service's freshness block with the shim's (`index_sha`
  agrees, and the shim adds `base_sha`, `behind_by`, `changed_since_index`
  and `dirty`). It marks the head and every row with `tools.py::_mark`.
* **If marking pushes the answer over 4 KiB, `remark` drops rows from the
  end and sets `next_cursor` to the first dropped row's offset.** A cursor is
  an offset into a fixed row order over an immutable snapshot
  (`tools.py`'s module docstring), so the next page starts exactly there.
* The pages the shim returns, concatenated, are therefore the same rows, in
  the same order, with the same marks, as the local server's pages. A
  property test holds that for every tool over the fixture snapshot, across
  random dirty sets.
* A page always advances, because `page`'s existing "one row over the cap"
  placeholder applies to a single remaining row.
* The service computes every answer under an **empty observation**:
  `Observation(block={"index_sha": …}, changed=frozenset(), dirty=frozenset())`.
  That is what makes its answers cacheable across agents (§5.2). Staleness
  belongs to one working tree, and only the shim knows it.

---

## 5. Caching

**A commit's index is immutable.**
* A manifest is named by its digest. Every blob is named by the sha256 of
  its stored bytes, and the reader checks it before decoding
  (`snapshot.py::Snapshot._decode`).
* A new commit is a new manifest and a new key, so nothing is ever
  invalidated.

Each cache below is per replica, in memory. **Every key starts with
`tenant_id`.**

### 5.1 Decoded-shard LRU

* **Key:** `(tenant_id, blob_sha256)`. Within a tenant, it is shared across
  commits and across that tenant's repositories, because a blob's name is
  its content (repo-index.md §2.5: "a blob path holding different bytes is a
  hard error"). It is never shared across tenants (owner decision 3).
* **Value:** the decoded `_Shard` with its lazily built `by(field)` maps.
* **Bound:** `SHARD_CACHE_RAW_BYTES = 768 MiB` of decompressed JSON, counted
  as `Snapshot` counts it (`_Shard.raw_bytes`). Eviction is least recently
  used.
* **Fairness:** while more than one tenant is resident, no tenant may hold
  more than half of the bound. A tenant over its share evicts its own least
  recently used shard first, so one large repository cannot starve every
  other workspace's agents.
* **Sizing, and what is not measured.**
  * This repository's whole graph is "about 70 MB of JSON" raw, and
    "decoded is several times that" (`snapshot.py`'s docstring, unmeasured
    multiple).
  * Lane GS-SVC measures the decoded-to-raw ratio on this repository's
    snapshot with `tracemalloc`. A test asserts that the shard bound times
    the measured ratio, plus the result cache and 512 MiB of headroom, fits
    in the 3 GiB limit. If it does not, the bound shrinks; the limit does not
    grow.
  * At a 2× ratio, 768 MiB raw holds about ten repositories the size of this
    one, hot.
* **Warming:** see §5.4.

### 5.2 Query-result cache

* **Key:** `(tenant_id, manifest_digest, tool, canonical_args, TOOLS_VERSION)`.
  * `canonical_args` is the validated argument object **without `cursor`**,
    with defaults filled, serialised `sort_keys`. For example, `impact`
    without `depth` is keyed as `depth: 2`, and a symbol name is keyed as
    given.
  * `TOOLS_VERSION` is a constant in `tools.py` that is bumped whenever a
    handler's output changes. A test fails if a handler's output changes on
    the fixture while the constant does not.
* **Value:** `Computed(head, rows)` serialised compactly (§2.4 seam 2). Every
  page of one question is served from one computation.
* **What is cached:** non-error results, including an `_Unresolved`
  (ambiguous-name) answer, which is equally deterministic. **A
  `SnapshotError` is never cached**: a missing or mismatching blob is a
  fault, not an answer.
* **Bound:** 256 MiB. Eviction is least recently used. A single entry over
  4 MiB is computed but not kept.
* **Where it pays:** `impact` and `call_graph` at depth 3, `tests_for` over
  50 inputs, and `search`. A planner and four implementers in one issue run
  ask the same `impact` of the same symbol at the same commit, and the
  second through fifth calls are a dictionary read.

**Why a hit is always correct.** An answer is a function of four inputs:

    answer = f(manifest bytes, blob bytes, tool, args, handler code)

* **Manifest bytes.** They are verified against `manifest_digest` before
  use, and `manifest_digest` is in the key. It came from a ticket that
  swarm-api minted from the promoted `index_versions` document (§3.1).
* **Blob bytes.** They are named in the manifest by their sha256 and checked
  against it before decoding. Equal manifest bytes therefore mean equal blob
  bytes.
* **Tool and args.** Both are in the key. `cursor` is excluded because it
  only slices `rows`.
* **Handler code.** `TOOLS_VERSION` is in the key, and the caches live in
  memory, so a new image starts empty anyway.
* **Determinism.** Every handler sorts its rows explicitly (for example
  `tools.py::tool_impact`), and reads nothing else: no clock, no environment
  and no git.
* **The observation is not an input.** The service always uses the empty
  one, and the shim applies the real one after the hit (§4.4).
* **`tenant_id` leads every key.** So a hit can only return an answer
  computed for the same workspace, which keeps invariant 9 as decision 3
  states it.

### 5.3 GCS, the store of record

The store is unchanged, and the service only reads it:
`tenants/<t>/repos/<repo_id>/graph/<sha>/manifest.json` and
`.../graph/blobs/<sha256>.jsonl.gz` (repo-index.md §2.3, §2.5).
* The issue says shards are "shared across commits within a tenant". **As
  built, they are shared across commits within one repository**: the
  `blobs/` directory is per `repo_id`. The shard LRU dedups across a
  tenant's repositories in memory anyway (§5.1), so nothing needs to move.
* Retention keeps 20 versions (repo-index.md §2.3, `KEPT_VERSIONS`). A pin
  older than that is §10's "index missing".

### 5.4 Warming: on first sight (owner decision Q5)

**Decided 2026-10-15 by the owner: warm on first sight, with no new IAM.**
The first ticket that names a manifest digest a replica has not seen starts a
background fetch. The replica fetches the manifest, verifies it against the
ticket's digest, then fetches and decodes the hot layers (`symbols`,
`callers`, `files`, `terms`), bounded by `graphstage.py::MAX_SNAPSHOT_BYTES`.
That first query is answered as soon as the shards it needs are in. Only the
first query after a promotion, per replica, pays a GCS read, which is about
one object GET per touched shard.

**Not built: Pub/Sub warming.** It was the doc's first recommendation: a
`swarm-graph-promotions` topic, a pull subscription and a peer fan-out over
`/internal/warm`. It needed a `pubsub.subscriber` grant beyond the owner's
"graph/** only", and the owner declined it. Revisit it only if the first-query
cost after a promotion measures as a problem; GS-SVC's metrics record it.

---

## 6. Sync: push, poll and the scheduled full index

### 6.1 What exists

* **The 5-minute poll exists and stays.**
  `terraform/modules/scheduler/jobs.tf` defines one
  `google_cloud_scheduler_job.repo_index_poll` per tenant, at
  `*/5 * * * *`. It calls
  `apps/swarm-api/swarm_api/routes/admin.py::poll_repositories`, then
  `repoindex.py::RepoIndex.poll`, then `RepoIndex._poll_one` and
  `RepoIndex._decide_one`.
  * The head is read with the tenant's own token, through
    `repoindex.py::read_head_if_changed` (ETag, a 304 is free).
  * `repoindex.py::poll_decision` queues a run when the head is past
    `current_sha`, subject to `min_change_interval_minutes`.
  * `repoindex.py::choose_kind` picks `incremental` when an ancestor graph
    exists, fewer than 300 files changed, no config file changed and the
    last full run is younger than `full_every_days`.
  * One run is in flight per registration. A newer head becomes
    `pending_sha`.
* **The scheduled full re-index stays:** `full_every_days`, default 7, and
  `interval_hours`, default 24.
* **No webhook receiver exists.**
  * `apps/swarm-api/swarm_api/mergewake.py`'s docstring: "The platform has
    no webhook receiver."
  * `repositories.py::ON_CHANGE_VALUES` refuses `webhook`.
  * `docs/runbooks/github-app.md` leaves the App's webhook unticked because
    "an endpoint that accepts them would be a new unauthenticated door".

### 6.2 The push path

**Endpoint (proposed).** swarm-api sits behind IAP, and GitHub cannot pass
IAP. So the receiver is a new, minimal public Cloud Run service,
`swarm-github-hooks`, with ingress `all` and `allUsers` as `run.invoker`.
That is the "new unauthenticated door" the runbook warned about, kept as
narrow as a door can be:

1. **It accepts only `POST /github/events`**, with a body of at most
   1 MiB, `X-GitHub-Event: push` (`ping` gets a 204; any other event gets a
   204 and is dropped), and a valid **`X-Hub-Signature-256`**.
   * The signature is HMAC-SHA256 of the raw body under the App's webhook
     secret, Secret Manager `swarm-github-app-webhook-secret`, compared in
     constant time.
   * An unsigned or badly signed request gets a 401 and never reaches
     Firestore.
2. **It dedups on `X-GitHub-Delivery`.** It creates the Firestore document
   `github_deliveries/{delivery_id}` with `create()`, which fails if the
   document exists. A redelivery or replay of the same id gets a 200 and does
   nothing more. The document carries `expires_at = now + 7 days` under a
   Firestore TTL policy.
3. **It keeps only pushes to a default branch.** The `ref` must be
   `refs/heads/<repository.default_branch>`, and the push must not be a
   deletion (`deleted: false`).
4. **It forwards the repository name and nothing else.** It calls swarm-api's
   new `POST /v1/admin/repositories/push` with `{forge: "github",
   full_name}` and its own ID token. The route admits only that identity, as
   `poll_repositories` admits only the scheduler's (`auth.is_rollup_sweeper`).

**Mapping a push to registrations.** swarm-api's route finds every
registration with `forge == "github"` and lower-cased `owner/repo` equal to
`full_name`, in **every** workspace that registered it. For each one, it runs
the existing `RepoIndex._poll_one(tenant_id, record, ...)`. That path:

* **re-reads the head with that workspace's own credential**. The `after`
  sha in the payload is never trusted or stored, so a valid signature over a
  stale or crafted payload can at most cause one extra head read;
* honours `paused`, `backoff`, the in-flight rule (`pending_sha`) and
  `min_change_interval_minutes`;
* queues an incremental run when `choose_kind` allows one and a full run
  otherwise.

The push is a **hint that makes the next poll happen now**. It is not a new
decision path, so the poll and the push can never disagree. Two workspaces
that registered the same public repository each get their own run with their
own credential, and their own index under their own prefix (decision 3).

* **Registration setting.** `ON_CHANGE_VALUES` gains `webhook`. It means
  "poll and push", and becomes the default for new registrations once the
  receiver is live. `poll` still means poll only, and `off` still means
  neither.
* **Interval.** `min_change_interval_minutes` (default 30, range 10-1440)
  applies to pushes as well. Otherwise a busy default branch would queue a
  run per merge. Q4 asks whether the default should drop to 10 now that
  pushes arrive promptly.
* **The App.** `Webhook → Active` is ticked, the URL is the receiver's, and
  only the `push` event is subscribed. This is a runbook change the owner
  makes in GitHub's settings, recorded in `docs/runbooks/github-app.md` by
  lane GS-HOOKS.
* **A missed webhook.** GitHub does not retry a failed delivery on its own.
  The 5-minute poll is the fallback (owner decision 2), unchanged, and it
  "enqueues one whenever the head is past the indexed commit".

### 6.3 Promotion and the service

The service learns of a promotion only when the first ticket names the new
digest (§5.4). A push never reaches it: the service serves only promoted,
pinned indexes.

---

## 7. The context pack at start

`apps/agent-worker/agent_worker/contextpack/__init__.py::prepare` builds
`CONTEXT.md` before the agent starts, through `contextpack/build.py::build_pack`.
That function opens `Snapshot(snapshot_dir, manifest_digest=...)` and walks it
directly: `contextpack/retrieve.py::graph`, `retrieve.py::_callers`,
`retrieve.py::_siblings`, and `contextpack/seeds.py::seeds_for` use `symbol`,
`callers`, `callees`, `tests`, `file`, `symbols_in`, `search` and raw
`shard(...).rows`.

**Decision (proposed).** The pack keeps its code and reads the service
through §2.4's blob-source seam.
* `build_pack` opens `Snapshot.from_manifest(...)` over a `ServiceSource`,
  which fetches **only the shards the seeds touch**, by calling the service's
  `index` tool page by page and decoding into the worker's memory.
* Nothing is written to disk. The shards are held under the same
  `CACHE_RAW_BYTES` bound and dropped when the pack is written.
* The ticket is the worker's, read directly from memory, since the worker
  minted it.

Rewriting the pack as tool calls was the alternative. It was not chosen
because the pack walks shards row by row, and each walk would become dozens
of round trips with a 4 KiB cap.

What the pack fetches is a small fraction of the graph: the seeds' modules,
their callers' modules one to three hops out, and the test modules. The pack
writes `pack.json`'s `shards_read` and `bytes_read`, so the claim is measured
on every attempt.

**The stored setting still decides (invariant 10).**
* `graphstage.py::decide` reads the registration's `context` override, then
  `code_intelligence.context`, then `context_ab` (`graphstage.py::assign_arm`).
  That decides whether there is a pin, and so whether there is a pack.
* The ticket route re-checks the same setting through `codeintel.py::effective_context`
  (§3.1).
* No caller input names the service, the URL, the commit or the digest.

A finding in passing, outside this lane's territory:
`contextpack/build.py::build_pack` reads `stage.get("arm")`. But
`graphstage.py::StageResult.record` nests `arm` inside `decision`, so
`pack.json`'s `arm` looks always `None`. This was read from the code, not
run. Lane GS-PACK fixes it, since it edits `build.py` anyway.

---

## 8. Migration

### 8.1 The flag

* **The stored setting is `code_intelligence.graph_delivery` ∈ `staged` |
  `service`**, per workspace, beside `context` and `context_ab` in
  `apps/swarm-api/swarm_api/codeintel.py`. The default is `staged`, it is
  editable by the workspace owner, and edits are audited like the others.
* Per workspace, so the owner's own workspace runs on the service first,
  then everyone else.
* `graphstage.prepare` reads it with the settings document it already reads.
* **The platform switch is the service's ConfigMap `enabled`** (§2.1).
  Off means every attempt falls back to staging, in seconds and with no
  deploy.

### 8.2 No gap

The order of rollout:

1. **Measure** (lane GS-MEASURE, §9). Staging is timed before anything
   changes.
2. **Deploy the service dark.** It runs in `swarm-system` with nothing
   pointing at it, and is checked by a smoke step that mints a ticket for a
   probe agent.
3. **Switch the owner's workspace** to `service`. For each attempt:
   * the worker pins and probes (§4.1, step 3);
   * **if the probe fails, the same attempt stages exactly as today** and
     records `delivery: "staged", reason: <why>` in `stage.json` and in the
     `graph_ready` start-up mark;
   * an attempt therefore never starts without the graph because the service
     was down at start.
4. **During the attempt.** If the service fails after the probe, the shim
   says so in the answer (§10). The worker does not hot-swap to a snapshot
   mid-attempt. That is a deliberate limit: the agent is told plainly, and
   the next attempt falls back.
5. **Widen** to every workspace once the fallback rate is below 1% over 7
   days, read from the `graph_ready` marks.
6. **Delete staging** (lane GS-RETIRE) once every attempt has run on
   `service` for 14 days. This removes `write_snapshot`, `server_argv` and
   the snapshot branch of `contextpack/__init__.py::context_dir`.
   * `graphmcp/__main__.py` stays: it is the offline server over a
     directory, which tests and a laptop still use.
   * **Exception:** steps on the `CLOUD_RUN_JOB` backend, today only
     `claude-code-review`, cannot reach a headless in-cluster Service. See
     Q2. Unless the owner chooses otherwise, those steps keep staging, and
     `graphstage` chooses by the attempt's backend, so the fallback code
     stays for them.

---

## 9. Start-up and storage: today's cost, and the expected saving

### 9.1 What can be measured today: nothing directly

* `graphstage.py` has no timer: no `monotonic`, and no seconds field
  anywhere.
* Its one log line, `log.info("graph context", staged=…, reason=…, arm=…)` in
  `graphstage.py::prepare`, carries neither bytes nor seconds.
* Bytes appear only in the pod-local `stage.json` (`facts.bytes`,
  `facts.blobs`), which is never uploaded.
* **`startup_progress` has no graph step.**
  `apps/swarm-api/swarm_api/startup.py::STARTUP_STEPS` lists pod and browser
  steps only.
* The worker's RUNNING start-up marks
  (`lifecycle.py::Worker._emit_startup_mark`) are `egress_ready`,
  `clone_timed` and `agent_started`. None isolates staging.
* `graphstage.prepare` runs inside `_build_child_env` under the
  `credentials` phase, so its time is folded into that phase.

This lane tried to read live attempts through the platform's API from inside
its pod. It was refused with an IAP 401. **No live timing is quoted here.**
The byte and request counts in §9.2 were measured offline, on this
repository.

### 9.2 What is known

| quantity | value | source |
|---|---|---|
| bytes downloaded per staged start | the whole graph: every blob the pin's manifest names. **Measured by this lane on 2026-10-16 at `4f54cef`: 782 blobs, 9,319,749 bytes gzipped, plus a 119,458-byte manifest (format 3).** The graph came from the test fixtures' extractor (`tests/unit/worker/graphmcp_fixtures.py::graph_of`, then `write_snapshot`) on this repository, which took 23.5 s. That extract has no LSP pass and no H or R layers, so a production index is this size or larger. knowledge-graph.md's "2.2 MB gzipped" was measured at `da1ca30` on 2026-10-08 and is out of date | this lane's measurement |
| upper bound | 64 MiB per start, refused above | `graphstage.py::MAX_SNAPSHOT_BYTES` |
| GCS requests per start | 1 manifest + 1 GET per blob, **sequential**, each hash-checked: **783 GETs** at the size above | `graphstage.py::write_snapshot` (one `store.download_bytes` per blob in a plain loop) |
| Firestore reads per start | registration, settings and up to 21 `index_versions` | `graphstage.py::_stage`, `graphstage.py::MAX_VERSIONS_READ` |
| git calls per start | up to one `merge-base --is-ancestor` per candidate, plus one `diff --name-only` | `graphstage.py::prove_ancestry` |
| pod memory for serving | up to 48 MiB raw shards decoded in the agent's pod, per server process | `graphmcp/snapshot.py::CACHE_RAW_BYTES` |
| git calls per answer | 3 | `graphmcp/freshness.py::Freshness.observe` |

### 9.3 The expected saving, stated honestly

* **Removed per start:** the manifest and blob downloads, and the writes to
  `work/.swarm-context/snapshot/`, which is checkpointed. Today a resumed
  attempt carries the snapshot in its checkpoint. Afterwards it carries a
  `pin.json` of a few KB.
* **Kept per start:** the Firestore reads and the ancestry git calls. The
  pin still needs them.
* **Added per start:** one ticket mint (one signed POST to swarm-api) and
  one `initialize` probe to the service.
* **Removed per pod:** up to 48 MiB raw of decoded shards and the per-agent
  decode CPU, which move to the shared, cached service.
* **Removed per answer:** two of three git calls.
* **Removed from disk per pod:** about 9.4 MB, which `work/` also carries
  into every checkpoint.
* **Net start-up time:** 783 sequential GCS GETs minus about two in-region
  round trips. **This is an estimate, not a measurement.** At an assumed 10-30
  ms per small in-region GET, 783 GETs is about 8-25 s per start. No
  production timing exists to confirm the per-GET figure, and GS-MEASURE's
  `graph_ready` mark replaces this estimate with a reading. If the reading
  comes in much lower, the saving is mainly memory and checkpoint size, not
  start-up time.

**Measurement first (lane GS-MEASURE, the first lane):**
* `graphstage.prepare` gains a timer around `_stage`.
* `lifecycle.py` emits a RUNNING start-up mark `graph_ready` with
  `{delivery, seconds, bytes, blobs, reason}`, beside `clone_timed`. The
  mark's `cause` is a string and is not in the frozen contract (§11).
* A week of readings on `staged` becomes the baseline. The same mark on
  `service` gives the after number, and the PR that flips the second
  workspace quotes both.

---

## 10. Failure modes: told plainly, never silently wrong

In every failure the agent receives an answer whose `freshness` block comes
first and whose `error` says what happened. It never receives an empty list
that reads as "no callers". `isError: true` is set, so the CLI shows it as a
failed tool call. The shim builds these answers with
`tools.py::error_answer` and the local observation.

| failure | where detected | what the agent is told | what else happens |
|---|---|---|---|
| **service down at start** (probe fails) | worker, §4.1 step 3 | nothing differs: the attempt stages and runs the local server | `stage.json` and `graph_ready` record `delivery: staged` and the reason |
| **service down mid-attempt** (connect refused, 5xx, timeout over 10 s) | shim | `"the graph service is unreachable (<cause>); no answer was computed. Retry later, or read the code directly."` | the shim retries once after 1 s before answering. Nothing is cached in the pod, so there is no stale fallback to serve wrongly |
| **service disabled** (switch off, 503 `disabled`) | shim | `"the graph service is switched off by the platform; no answer was computed."` | the next attempt falls back at its probe |
| **ticket refused** (401: expired, bad MAC, wrong fields) | shim | `"the graph service refused this attempt's ticket (<expired \| invalid>); no answer was computed."` | the shim re-reads the ticket file once and retries, which covers a renewal race. A persistent 401 means the worker's renewal failed. The worker logs that, and its next mint either succeeds or ends the attempt fenced (409) |
| **context disabled** (403 at mint) | worker | no `swarm-graph` server is registered, exactly as today when the setting is off | `SWARM_GRAPH_UNSTAGED=<reason>`, as today |
| **cache cold** | service | nothing: a cold cache is slower, never different. §5.2's proof holds for misses too | a per-answer `cache` field (`hit`/`miss`) in the service's own log and metrics, never in the answer |
| **index missing** (404: pinned manifest or a blob gone, e.g. swept past `KEPT_VERSIONS` during a long attempt) | service | `"the index of <sha12> is no longer stored (pruned or missing); no answer was computed."` | the service never answers from a partial graph. It never falls forward to another commit, because the pin is the ticket's |
| **manifest does not match the pin** (409: rewritten in GCS) | service | `"the stored index of <sha12> does not match its promoted digest; refused."` | logged as a security event with tenant and repository ids only. Nothing is cached |
| **no index for the base commit** | worker, at pin | the nearest indexed ancestor is pinned, `behind_by` is set, and every row on a path changed since then is marked `stale: "changed since the index"` (§4.3) | today's behaviour, unchanged |
| **no indexed ancestor at all** | worker | no `swarm-graph` server, `SWARM_GRAPH_UNSTAGED=<reason>`, and the pack says "No repository context was staged…" as today | — |
| **git fails or is slow in the shim** | shim | the answer is served with `"dirty": "unknown"` in the block | today's behaviour (`freshness.py::Observation`). It never reports zero |

---

## 11. Lanes

The rules are knowledge-graph.md §6's:
* no two lanes in one phase share a file;
* a phase starts when the one before it has merged;
* tests are pushed first and go red in CI, then the fix.

Territory is files. Each lane's brief names `scripts/changed-guards.sh` and
`scripts/publish-scan.sh` as its pre-finish commands.

| lane | phase | builds | territory (files edited or created) | tests |
|---|---|---|---|---|
| **GS-MEASURE** | 0 | the `graph_ready` mark: timer, bytes, blobs, delivery, reason | `apps/agent-worker/agent_worker/graphstage.py`, `apps/agent-worker/agent_worker/lifecycle.py` | `tests/unit/worker/test_graphstage.py` (timer and facts), new `tests/unit/worker/test_graph_ready_mark.py` |
| **GS-CORE** | 1 | §2.4's three seams (`BlobSource`, compute/page split, `remark`); the `call_graph` and `index` tools; the `document` layer name; the one-git-call `observe` (§4.3); `TOOLS_VERSION` | `apps/agent-worker/agent_worker/graphmcp/snapshot.py`, `.../graphmcp/tools.py`, `.../graphmcp/freshness.py`, `.../graphmcp/server.py` (instructions only) | `tests/unit/worker/test_graphmcp_budgets.py`, new `test_graphmcp_source.py`, new `test_graphmcp_remark.py` (page-concatenation property), new `test_graphmcp_freshness_one_call.py` (the six cases), new `test_graphmcp_call_graph_index.py`, new `test_graphmcp_tools_version.py` |
| **GS-TICKET** | 1 | §3.1-§3.2: the reference mint and verify; the route; the setting | new `apps/swarm-api/swarm_api/graph_ticket.py`, new `apps/swarm-api/swarm_api/routes/graph_ticket.py`, `apps/swarm-api/swarm_api/main.py` (include the router), `apps/swarm-api/swarm_api/settings.py` | new `tests/unit/control_plane/test_graph_ticket_api.py` (fence, repository from the agent, digest from Firestore, setting off is 403, field set, clamp) |
| **GS-INDEXDOC** | 1 | the repo-index document as the `document` `index_shards` layer in the manifest | `images/agent-runtime-indexer/repo-index/repo_graph_shards.py` | `tests/unit/worker/test_repo_graph_shards.py`, `tests/unit/worker/test_repo_graph_format.py` |
| **GS-PUSHAPI** | 1 | `POST /v1/admin/repositories/push`; `webhook` in `ON_CHANGE_VALUES` | `apps/swarm-api/swarm_api/repoindex.py`, `apps/swarm-api/swarm_api/routes/admin.py`, `apps/swarm-api/swarm_api/repositories.py` | new `tests/unit/control_plane/test_repository_index_push.py` (mapping across workspaces, `_poll_one` reused, payload sha never stored, identity check), `tests/unit/control_plane/test_repository_index_poll.py` |
| **GS-SETTING** | 1 | `code_intelligence.graph_delivery` | `apps/swarm-api/swarm_api/codeintel.py` | `tests/unit/control_plane/test_repository_context_override.py` or a new `test_code_intelligence_delivery.py` |
| **GS-SVC** | 2 | the `swarm-graph` service: HTTP, verifier, `TenantSource`, the caches, warming | new `apps/swarm-graph/` (`pyproject.toml`, `uv.lock`, `Dockerfile`, `swarm_graph/{main,server,ticket,store,cache,warm}.py`) | new `tests/unit/graphsvc/` (ticket parity with `graph_ticket.py`, tenant-leading keys, hit correctness, fairness, sizing assertion, every §10 status) |
| **GS-HOOKS** | 2 | the public receiver `swarm-github-hooks` | new `apps/swarm-github-hooks/` (same layout as GS-SVC), `docs/runbooks/github-app.md` | new `tests/unit/hooks/` (signature, size cap, event filter, delivery dedup, forwards only `full_name`) |
| **GS-SHIM** | 2 | `agent_worker.graphmcp.shim` (§4.3-§4.4) | new `apps/agent-worker/agent_worker/graphmcp/shim.py` | new `tests/unit/worker/test_graphmcp_shim.py` (local `tools/list`, remark, ticket re-read, every §10 message) |
| **GS-PACK** | 2 | `build_pack` over a `ServiceSource`; the `arm` read fixed; `shards_read`/`bytes_read` | `apps/agent-worker/agent_worker/contextpack/build.py`, `apps/agent-worker/agent_worker/contextpack/__init__.py` | `tests/unit/worker/test_contextpack.py` |
| **GS-INFRA** | 3 | §3.4's rows, the manifest, the tenant policy, the hooks service and secret, images, release roll | new `terraform/infra/graph_service.tf`, new `terraform/infra/github_hooks.tf`, `terraform/infra/variables.tf`, `terraform/infra/locals.tf`, `terraform/infra/outputs.tf`, new `kubernetes/swarm-system/swarm-graph.yaml`, new `kubernetes/network-policies/allow-graph-service.yaml`, `kubernetes/render.py`, `scripts/push-images.sh`, `scripts/create-secrets.sh`, `.github/workflows/release.yml`, `.github/workflows/hotfix.yml` | new `tests/terraform/graph_service_iam.tftest.hcl`, new `tests/unit/scripts/test_graph_service_manifest.py` (requests == limits, PSS, kill switch keys) |
| **GS-WORKER** | 3 | `graphstage.pin`, the probe and fallback, `shim_argv`; `graph_ticket.py` mint and renew; the runners' config; renewal in the lifecycle | `apps/agent-worker/agent_worker/graphstage.py`, new `apps/agent-worker/agent_worker/graph_ticket.py`, `apps/agent-worker/agent_worker/runners/claude_code.py`, `apps/agent-worker/agent_worker/runners/codex.py`, `apps/agent-worker/agent_worker/lifecycle.py`, `apps/agent-worker/agent_worker/config.py` | `tests/unit/worker/test_graphstage.py`, new `test_graph_ticket_worker.py` (renewal, 409 fenced exit, file mode 0600 outside `work/`), new `test_runner_graph_shim_config.py` (claude-code, claude-code-browser, codex) |
| **GS-RETIRE** | 4 | delete staging for GKE attempts, after §8.2 step 6's 14 days | `apps/agent-worker/agent_worker/graphstage.py`, `apps/agent-worker/agent_worker/contextpack/__init__.py` | `tests/unit/worker/test_graphstage.py`, `tests/unit/worker/test_contextpack.py` |

Why this order:
* **Phase 0** is alone because GS-WORKER and GS-RETIRE edit the same two
  worker files later.
* **Phase 1's six lanes are disjoint.** GS-PUSHAPI owns `repoindex.py`
  whole, so the promotion publish lives there too. GS-SETTING owns
  `codeintel.py`, which GS-TICKET only imports.
* **Phase 2 depends on phase 1.**
  * GS-SVC imports GS-CORE's seams, and its parity test reads GS-TICKET's
    verifier.
  * GS-SHIM imports `remark`.
  * GS-PACK imports `BlobSource`.
  * GS-HOOKS calls GS-PUSHAPI's route.

  Each phase-2 lane is a new directory or a file no other phase-2 lane
  touches.
* **Phase 3 needs the images and code of phase 2.** GS-INFRA alone owns
  `scripts/push-images.sh`, so both new images are added in one lane, after
  their Dockerfiles exist; an `ALL_TARGETS` entry without a recipe would turn
  CI red. GS-WORKER is the only phase-3 lane in `apps/agent-worker/`.
* **Track crossings, said once.** GS-INFRA and GS-HOOKS edit Track C and
  Track D files, and GS-PUSHAPI edits Track A's. CLAUDE.md says track
  ownership never defers work, and each lane's commit names the track it
  crosses into.

**After GS-INFRA merges,** the owner applies the IAM rows (§3.4), populates
`swarm-graph-ticket-key` and `swarm-github-app-webhook-secret` with
`scripts/create-secrets.sh`, and ticks the App's webhook. Every lane before
that is mergeable dark.

**Contract change requests: none.** Nothing here edits or needs
`apps/common/swarm_common/`.
* The ticket constants live in swarm-api, the worker and the service, as the
  egress and TypeSafe constants do.
* `graph_ready` is a start-up mark `cause`, a string the worker writes, and
  is not a member of `swarm_common`.
* No runner profile changes. `RUNNER_PROFILES` already names `claude-code`,
  `codex` and `claude-code-browser`, each with the runner that registers the
  server.
* `code_intelligence.graph_delivery` is a swarm-api setting, as `context` is.

If GS-SVC finds that it needs a field in a frozen type, it writes an entry
for [`docs/contract-change-requests.md`](../contract-change-requests.md) and
stops.

---

## 12. Decisions this document makes that the owner has not

The owner decides each of these. The recommended option is the one the
sections above are written against.

**Answered by the owner on 2026-10-15 (~18:00 PDT):** Q1 recommended (the
ticket file), Q2 recommended (keep staging for Cloud Run Job steps), Q3
recommended (the `swarm-github-hooks` receiver), Q5 **warm on first sight**.
Q4 and Q6 to Q11 follow the recommended option; the owner may revisit any of
them before its lane starts.

* **Q1, ticket custody.**
  * **Recommended:** the worker writes the ticket to `private/graph-ticket`
    and the shim reads it. The agent runs as the same uid and can read it,
    as `jevkey.py` records for the TypeSafe key. A leaked ticket grants only
    the read of this workspace's graph that the agent already has, for at
    most 15 minutes, and only from inside the cluster.
  * Alternative: the shim holds no ticket and relays through a worker spool,
    as `swarm-git` and `swarm-search` do. The worker answers spools on its
    control-poll cadence (`config.py`'s `control_poll_seconds`, 10 s), which
    is far over the 300 ms answer budget.
* **Q2, the `CLOUD_RUN_JOB` backend** (`claude-code-review`).
  * **Recommended:** keep staging for it.
  * Alternative: an internal load balancer and Direct VPC egress so Cloud
    Run Jobs reach the service.
* **Q3, the webhook receiver.**
  * **Recommended:** a separate public Cloud Run service `swarm-github-hooks`
    with an HMAC check and delivery dedup, and nothing else.
  * Alternative: an IAP-exempt path on swarm-api's load balancer.
  * Alternative: no receiver, with the poll at 1 minute instead.
* **Q4, the change interval for pushes.** Keep the default 30 minutes, or
  lower the default to 10.
* **Q5, warming. DECIDED (owner, 2026-10-15): warm on first sight, with no
  new IAM** (§5.4). Pub/Sub was the doc's first recommendation and is not
  built.
* **Q6, packaging.**
  * **Recommended:** the service installs the `swarm-agent-worker` wheel and
    imports `graphmcp` from it, which costs about the google-cloud client
    libraries of image size.
  * Alternative: move `snapshot.py`, `tools.py` and `freshness.py` into a
    small shared package that both import.
* **Q7, the repository index document.**
  * **Recommended:** the indexer writes it into the graph manifest as the
    `document` layer.
  * Alternative: a second IAM grant on `tenants/*/repos/*/index/**`.
* **Q8, sizing.** Two replicas at 1 vCPU and 3 GiB, a 768 MiB raw shard LRU
  and a 256 MiB result cache, with the shard bound shrunk if GS-SVC's
  measurement says so.
* **Q9, the migration thresholds.** Widen at under 1% fallback over 7 days;
  delete staging after 14 days.
* **Q10, the context pack.** Read the seeds' shards through the service into
  worker memory, instead of re-expressing the pack as tool calls.
* **Q11, the two new tools.** `call_graph` and `index`, by these names, kept
  inside the existing 2k-token schema budget.
