# Registered MCP servers: a task's agent queries its caller's own MCP server

**Status: PROPOSED 2026-10-17 (lane CALLER-MCP-DESIGN, part of #1433);
approved by the owner on 2026-10-17 with the answers to §7 in PR 1466's
comments; BUILT, dark, by 2026-10-18.** §8 says what each build-plan lane
landed and where the build differs from this text. The feature ships dark:
every `mcp_servers` submission is refused `mcp_unavailable` until
`SWARM_MCP_SERVERS=on` (off by default). The body below is the design as
approved, amended in place only where the owner decided after approval
(§1.2 and §4.8, each dated); every choice in it that was this lane's is still
marked **(proposed)**.

It builds on [workspace-secrets.md](workspace-secrets.md) (#1347): long-lived
credentials come from that store, through its grants, and the attempt-signed,
generation-fenced release is the model for the one new route that serves
credential material here. It borrows the worker's loopback-shim shape from
the TypeSafe migration (`apps/agent-worker/agent_worker/typesafe_shim.py`)
and the per-tenant registration shape from registered repositories
(`apps/swarm-api/swarm_api/repositories.py`).

Every code citation is `path::symbol` or a path, read at `35f2a61` on
2026-10-17. Symbols move less than lines do, so search for the symbol.

---

## 0. The answer in one screen

* **A tenant registers an MCP server by NAME** (`kb`), with its HTTPS URL,
  how it authenticates, which profiles may use it and which of its tools an
  agent may call. Registration is an admin act and lives in Firestore
  `mcp_servers/`, per tenant, like a registered repository (§4.2).
* **A dispatch or a workflow step names servers by name**:
  `input.mcp_servers: ["kb"]`, a profile-declared input, so it is covered by
  the existing spec signature (format 3) with no new format. It never names a
  URL, a header, a transport or a command (invariant 10). A name that is not
  registered, disabled, or not allowed for the profile is refused **at
  submission**, before admission (§4.3).
* **A per-run credential travels beside the task, never in it**:
  `mcp_credentials: {"kb": {"value": …, "expires_in": 3600}}`, a top-level
  request field. swarm-api writes it to a per-submission Secret Manager secret
  (`swarm-tenant-<t>-mcpc-<16 hex>`, with a Secret Manager TTL) and stores only
  the secret's name; it is destroyed when the task ends (§4.4). A long-lived
  credential is a workspace secret granted to the consumer `mcp:kb` (§4.5).
* **The credential never enters the agent's reach.** Once per attempt the
  worker asks swarm-api, on an attempt-key-signed, generation-fenced request
  (`POST /v1/attempts/{attempt_id}/mcp:attach`), for each named server's URL,
  rules and credential. It keeps them in its own non-dumpable heap and serves
  a **loopback relay** per server. The CLI's MCP config names the relay
  (`http://127.0.0.1:<port>/mcp/kb`) and a per-attempt local token, never the
  credential (§4.6, §4.7).
* **The relay is the policy point**: it adds the auth header, forwards only
  MCP's own headers, calls only allowed tools, caps sizes, rates and time,
  refuses a request that carries a registered secret or a credential-shaped
  string, scrubs the credential from answers, follows no redirect, connects
  only to a public address on 443, and logs the tool name and status, never
  arguments or results (§4.8).
* **Egress needs no new rule** for a public HTTPS server: tenant pods already
  reach `0.0.0.0/0` on 443 minus private ranges, through the swarm NAT's fixed
  addresses, which the caller can allowlist. A private endpoint is out of
  reach by design (§4.9, Q6).
* **Four contract requests** (§5): the `mcp_servers` input on the CLI agent
  profiles; the `mcp` secret-consumer kind and its one slot; the server-name
  spelling; and a recorded "no new park reason, no new spec format".

---

## 1. The problem, as measured

### 1.1 What the issue asks

Issue #1433 (opened 2026-10-16) wants to run SwarmCloud workflows from an
application that keeps the knowledge its agents need (an indexed graph of
documents, conversations and decisions) behind its own MCP server. Each agent
must **query that server during its run and write results back through it**,
with **a credential valid for that one run only**. Its four acceptance
checks:

1. a claude-code task dispatched with a registered MCP server and a
   credential calls a tool on that server, and the server sees the
   credential;
2. the same dispatch naming an unregistered server is refused before
   admission;
3. the credential appears in no log route, artifact, checkpoint or event, and
   a test searches all of them for it;
4. after the attempt ends, the worker holds no copy of it.

### 1.2 What the code allows today (read 2026-10-17 at `35f2a61`)

* **A task's inputs are its prompt, one repository and its profile's declared
  inputs.** `apps/swarm-api/swarm_api/schemas.py::TaskCreate` has eleven
  fields (`runner_profile`, `input`, `priority`, `metadata`,
  `repository_url`, `repository_ref`, `timeout_seconds`, `max_attempts`,
  `model`, `strategy`, `carrier`) and `extra="forbid"`, so any other field is
  a 422. `claude-code` and `codex` declare exactly one input,
  `issue` (`apps/common/swarm_common/profiles.py::_CLI_AGENT_INPUTS`).
* **Every MCP server the worker hands an agent is a local stdio process it
  chose.** `apps/agent-worker/agent_worker/runners/claude_code.py::write_mcp_config`
  can write five servers (`playwright`, `jev`, `swarm-git`, `swarm-search`,
  `swarm-graph`), every one `"type": "stdio"`; none is remote, none carries a
  header. The codex runner passes two (`swarm-git`, `swarm-graph`) as `-c
  mcp_servers.<name>.*` overrides (`runners/codex.py::git_tools_args`,
  `graph_tools_args`), also stdio. **Zero remote servers, zero headers.**
* **Nothing carries a per-run credential to a worker.** The scheduler's
  `apps/scheduler/scheduler/dispatch.py::worker_env` carries "identifiers and
  endpoints" only, by design. The workspace-secrets release
  (`apps/swarm-api/swarm_api/routes/attempts_secrets.py`) serves only
  long-lived workspace secrets granted to a profile, tool or component, and
  writes them as files the agent can read (workspace-secrets.md §4.1).
* **What the caller does instead** (the issue's own words): it retrieves
  context itself and pastes it into each step's prompt, and results come back
  only as artifact files. The agent cannot ask a follow-up question of the
  knowledge it was given, and cannot write to it.

A side note, recorded because someone will think of it: a repository's own
`.mcp.json` might be loaded by the CLI, because the claude-code runner does
not pass `--strict-mcp-config` (it appears nowhere in the repository). That is
not a route for this feature: the file is in a public-or-shared checkout, so it
can never carry a credential, and when this was written nobody had verified
what the pinned CLI (`CLAUDE_CODE_VERSION=2.1.283`,
`images/agent-runtime-base/Dockerfile`) does with it under `--print`. The
design delegated the question to the build lane MCP-RUNNERS (§6), to be
decided with a test, because it bears on which servers an agent is actually
given.

**Decided by MCP-RUNNERS, with a test, as the design delegated (recorded
2026-10-18).** claude-code runs without `--strict-mcp-config`. A repository's
own `.mcp.json` stays available to the agent, as it is to a developer who
checks the repository out; an attached server wins a name the repository also
declares; and the caller's credential is never within reach of a
repository-declared server, because it is never in the CLI's config at all:
only the relay holds it, and the relay sends it only to the registered URL
(§4.8). Measured against the pinned 2.1.283 by
`tests/unit/worker/test_mcp_runner_config.py::test_the_pinned_claude_code_loads_the_repositorys_mcp_json_and_ours_wins_a_name`
(in the image, where the binary exists): a server named only in the checkout's
`.mcp.json` reaches the model, and a checkout server under the attached
server's name is never contacted. The runner's own account is in the
`apps/agent-worker/agent_worker/runners/claude_code.py` module docstring.

### 1.3 What "done" lets someone do that they cannot today

* Register `kb → https://kb.example.com/mcp` once for a workspace, with
  "agents may call `search` and `record_decision`, nothing else".
* Dispatch a claude-code or codex task, or a workflow whose steps say
  `mcp_servers: ["kb"]`, with a credential minted by their own application
  for that dispatch, valid for an hour.
* Have the agent call `kb.search` as often as it needs during the run and
  `kb.record_decision` to write its result back, while the credential is never
  in the agent's environment, files, transcript, artifacts, checkpoint or the
  platform's logs, and is destroyed when the task ends.

---

## 2. Constraints that bind the design

### 2.1 The CONTRACT.md invariants it touches, and how each is kept

1. **Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create infrastructure
   demand.** Nothing in this design runs before a lease: registration is a
   Firestore document, the stored per-run credential is a Secret Manager
   secret (storage, not capacity), and every check before the lease is a
   Firestore read. A task whose server was disabled parks
   `CREDENTIAL_MISSING` before it leases, at no cost (§4.10). The relay runs
   inside the worker process the attempt already has; there is no new pod
   per attempt and no warm pool.
2. **All-or-nothing reservation in one transaction.** Unchanged. An MCP server
   is not a capacity pool and is never reserved. Its rate limit is enforced
   per attempt by the relay, not by admission, so nothing is added to the
   reservation transaction and nothing can half-reserve.
3. **Concurrency counts from `LEASED`.** Unchanged; nothing here counts
   running work.
4. **Workers never sleep through a long provider wait.** A slow or down MCP
   server is a tool error to the agent, bounded by the registration's
   `call_timeout_seconds` (default 60, ceiling 300), never a wait the worker
   sits in and never a park. The agent decides what to do with the error, as
   it does with a failing web fetch.
5. **Fencing.** The attach route is fenced exactly as the secrets release is:
   STARTING or RUNNING at this lease and this generation, lease unreleased,
   re-checked in the transaction that writes its audit rows; a stale
   generation gets 409 and no URL or credential. The relay stops forwarding
   the moment the worker's control poll marks the attempt fenced, so a worker
   superseded mid-run cannot keep writing to the caller's server.
6. **Spot disabled.** Untouched.
7. **`requests == limits`.** Untouched: the relay is a thread pool inside the
   worker's existing container, inside its existing requests.
8. **Checkpointing is mandatory and periodic.** Untouched, and no credential
   reaches a checkpoint: the credential lives only in the worker's heap, and
   what is written under `work/` (which checkpoints carry) holds the relay's
   loopback URL and a per-attempt local token that is inert once the attempt
   ends (§4.7). MCP session state is not checkpointed; a resumed attempt opens
   a new session, so a registered server must tolerate a client that
   reconnects (§4.8).
9. **Per-tenant isolation.** A registration belongs to one tenant and every
   read compares the stored `tenant_id`, answering a mismatch with the same
   404 as an absent one (the rule `repositories.py` keeps). The attach route
   takes the tenant from the fenced task, never from the request. A per-run
   credential lives in a secret named under the task's tenant
   (`Tenant.secret_name`), readable by swarm-api only. The tenant's worker
   account holds **no** grant on it, so an agent that mints the pod's GSA
   token reads nothing.
10. **Callers pick a `runner_profile` by name.** A step names registered
    servers by name, and the URL, transport, header, allowed tools and limits
    all come from the registration, which an admin writes. The per-run
    credential is a value the caller's own server will check; it selects
    nothing about what runs, where, or with what resources. The `mcp_servers`
    input is declared in the frozen catalogue like `issue` and `browser_tool`,
    so swarm-api, the plugin and the console read one declaration.

### 2.2 The facts every design has to answer

These are the same facts workspace-secrets.md §3.1 records, restated because
each option below is judged against them:

* **The agent runs as the worker's uid, in the worker's pod.** It can read
  every file the worker writes (`private/` is "not a permission boundary",
  `apps/agent-worker/agent_worker/workspace.py`), `/proc/<pid>/environ` of
  any runner child, and it can mint the tenant GSA's tokens.
* **It cannot read the worker's memory or sign as the worker.** The worker
  makes itself non-dumpable before the agent exists
  (`apps/agent-worker/agent_worker/hardening.py::make_non_dumpable`), and the
  attempt key it registers then is the one thing in the pod the agent cannot
  use. The git tools, the live session, the egress and TypeSafe tickets and
  the secrets release all rest on it.
* **`work/` is HOME and is checkpointed**; `private/` is not. The claude-code
  MCP config is written to `work/.swarm-claude-code/mcp.json` today
  (`runners/claude_code.py::write_mcp_config`), mode 0600, holding no secret.
* **Firestore never holds a credential value**, and nothing that serves one
  is a GET (`tests/unit/control_plane/test_no_route_serves_credentials.py`
  names `secrets:release` as its one carve-out, owner decision Q2 of #1347).
* **Egress is open to public addresses on 443, 80 and 22** and closed to
  private ranges (`kubernetes/network-policies/allow-egress.yaml`, rule 4);
  there is no hostname allowlist and none is planned, because the browser
  profile must fetch whatever page a task names.

### 2.3 What it needs from `apps/common/swarm_common/`

Four requests, written out in §5: the `mcp_servers` input in a dict of its
own, `_MCP_SERVER_INPUTS`, spread into `claude-code`, `codex` and
`claude-code-browser` only (not into `_CLI_AGENT_INPUTS`, which `indexer` and
`claude-code-review` also take);
`mcp` as a fourth `SECRET_CONSUMER_KINDS` member with its one slot; one
spelling for a server name; and, recorded so no lane adds them, no new spec
format, no new park reason and no new event type.

---

## 3. Designs that could work

All three share §4.2's registration, §4.3's naming and §4.4's per-run
credential store; they differ in **where the credential goes once an attempt
starts** and who speaks to the caller's server.

### 3.1 Design A: the CLI talks to the server directly, credential in its config

The worker writes the remote server into the CLI's MCP config as an `http`
entry with the credential in `headers` (claude-code), or names it with
`bearer_token_env_var` and puts the credential in the CLI's environment
(codex).

* **Code:** smallest. One fetch at start (the attach route), a few lines in
  each runner. No relay.
* **What a compromised or merely curious agent gets:** the credential itself.
  It is in a file in `work/` or in the CLI's environment, which the agent's
  Bash tool inherits. It can be read, printed, committed or sent anywhere,
  and it stays valid until its own expiry, after the attempt ends.
* **What it breaks:** acceptance check 3 at once. `work/` is checkpointed, so
  the claude-code config would carry the credential into GCS unless it moved
  to `private/`; the environment route puts it in `/proc/<pid>/environ`.
  Check 4 ("the worker holds no copy") is true of the worker and false of
  everything the agent copied. Nothing can enforce an allowed-tools list, a
  size cap or a rate, because the CLI speaks to the server unmediated.
* **Latency and money:** none added. **Verdict:** rejected. It is the design
  the issue's acceptance checks were written to rule out.

### 3.2 Design B: a loopback relay inside the worker (recommended)

The worker holds the credential in its non-dumpable heap and serves a
loopback HTTP relay per server for the attempt. The CLI's config names the
relay and a local token. The relay adds the real credential on the way out.
This is `typesafe_shim.py`'s shape, generalised from one fixed upstream to a
registered one.

* **Code:** one worker module (the relay, ~600 lines on the TypeSafe shim's
  evidence), one attach client, two runner changes, one swarm-api route
  beside the secrets release, the registry and its console page.
* **What a compromised agent gets:** the **use** of the server for the rest of
  the attempt, within the registration's allowed tools, sizes and rate. Not
  the credential: it is in no file, no environment and no argv, and the
  worker's memory is not readable. When the attempt ends the relay dies with
  the worker process and the local token is worthless. That is the same
  boundary every other attempt-scoped capability here has ("the boundary is
  the attempt", workspace-secrets.md §0).
* **What it breaks:** nothing existing. It adds a hop the platform owns, so
  MCP features the relay does not pass (for example a server-initiated
  request on a separate stream the relay does not forward) are unavailable
  until it does; §4.8 lists what it passes.
* **Latency:** one swarm-api round trip and one Secret Manager read at
  attempt start (tens of milliseconds); one loopback hop per tool call
  (sub-millisecond). **Money:** none beyond Design A; the relay uses the
  worker's existing CPU.
* **Works on both backends:** the relay is in the worker, so it runs on GKE
  Autopilot and Cloud Run Jobs alike.

### 3.3 Design C: a platform forwarder in `swarm-system`

The `swarm-typesafe-fwd` shape (workspace-secrets.md §3.5): a Deployment in
`swarm-system` holds or reads the credential and forwards MCP traffic; the
worker gets a short-lived ticket, and the credential never enters an agent
pod at all.

* **What it adds over B:** a worker compromise (not just an agent compromise)
  still does not yield the credential. Today nothing in this platform
  separates the worker from the agent except `make_non_dumpable`, so the gain
  is real but narrow.
* **What it costs:**
  * **A generic forwarder with caller-chosen upstreams is an SSRF surface on a
    platform identity.** The TypeSafe forwarder is safe because its upstream
    is a constant. Here every registration is a new upstream, so the
    forwarder needs the same public-address, port and redirect checks the
    relay needs, but running as a platform service account in
    `swarm-system`, where a mistake reaches more than one tenant.
  * **Cloud Run workers cannot reach it.** The TypeSafe forwarder is
    reachable only from GKE pods (workspace-secrets.md §3.5, "What is left");
    `claude-code` and `codex` would lose the feature wherever they run on
    Cloud Run, or need a second, public ingress.
  * **A new image, a Deployment, a key ring, a ticket spec shared by two
    halves, a NetworkPolicy, a roll script, a release step** (the TypeSafe
    forwarder took four lanes and two owner-pushed workflow changes,
    PR 1414 and PR 1417, and is still off).
  * **Fencing and rate in memory need one replica**, as `swarm-typesafe-fwd`
    has, so every tenant's MCP traffic shares one pod's capacity and fate.
  * **Money:** one more always-on pod with `requests == limits` (invariant
    7), plus its share of the release pipeline. **Latency:** one more network
    hop per tool call, across the cluster.
* **Verdict:** not first. Recorded as an upgrade path the same way `inject`
  was recorded for workspace secrets: if a caller's credential ever justifies
  keeping it out of the worker too, Design B's relay is the seam where the
  forwarder's ticket would replace the credential, and nothing in the
  registration, the API or the runners changes (Q8).

### 3.4 Orthogonal: where the per-run credential comes from

Independent of A/B/C, three sources; the recommended design supports the
first two and leaves the third to the owner (Q3):

| source | how | strength | cost |
|---|---|---|---|
| **dispatch** (the issue's ask) | the caller sends a value with the dispatch; swarm-api stores it in a per-submission secret with a TTL; destroyed at task end | the caller controls lifetime and scope; nothing for their server to learn | a value at rest for the queue time, in Secret Manager, readable by swarm-api only |
| **workspace secret** | an admin stores a long-lived key in the workspace store and grants it to `mcp:<name>` | no per-dispatch plumbing for callers whose server takes one key | the key is long-lived; rotation is the store's (24 h overlap) |
| **platform-minted** | swarm-api mints a signed JWT per attempt (audience the registered URL, claims tenant/task/attempt/generation, minutes of life) and publishes a JWKS; the caller's server verifies it | nothing caller-supplied is ever at rest; the token cannot outlive the attempt; the server learns which attempt is calling | the caller's server must verify our JWT; a new signing key and a public JWKS route |

---

## 4. The recommended design: B, with dispatch and workspace-secret credentials

### 4.1 The flow

```
caller ──POST /v1/tasks {input.mcp_servers:["kb"], mcp_credentials:{kb:{value,expires_in}}}──▶ swarm-api
   swarm-api: registration exists, enabled, profile allowed  ──else 4xx, nothing stored
              credential → Secret Manager swarm-tenant-<t>-mcpc-<hex> (ttl) ; Firestore row holds its NAME
              task signed (input covered, format 3) ──▶ QUEUED
scheduler admission: registration still enabled? credential row live and unexpired?
              disabled → PARK CREDENTIAL_MISSING (no lease) ; expired → FAILED mcp_credential_expired
              else lease, attempt-key nonce issued (as for any task with grants)
worker (non-dumpable, attempt key registered) ──POST /v1/attempts/{id}/mcp:attach {generation}──▶ swarm-api
   swarm-api: signature, fence, once; servers from the SIGNED input; registration re-read;
              credential read from Secret Manager; one audit row per server ──▶ {servers:[{name,url,auth,rules,credential}]}
worker: registers every credential for redaction; starts relay 127.0.0.1:<port>/mcp/<name>
        writes the CLI config: url = relay, header = local token
agent ──MCP (streamable HTTP)──▶ relay ──policy checks, real auth header──▶ https://kb.example.com/mcp
attempt end: relay closed, references dropped, process exits
task terminal: sweep destroys swarm-tenant-<t>-mcpc-<hex> (Secret Manager TTL is the backstop)
```

### 4.2 The registration

One Firestore document per (tenant, server), in its own collection
`mcp_servers/{server_id}`, kept out of `store.py` and `codec.py` for the
reason `repositories.py` gives: the shape is not the frozen contract's.
`server_id` is `mcp_` + the first 16 hex of
`sha256(tenant_id + "\0" + name)`, so the id is stable and carries no name.

| field | meaning |
|---|---|
| `server_id`, `tenant_id` | identity; `tenant_id` never changes and every read compares it |
| `name` | the spelling a step uses: `[a-z][a-z0-9-]{0,30}`, unique per tenant. `swarm-*`, `playwright` and `jev` are reserved, because the worker already registers servers by those names (§5, request M3) |
| `url` | `https://` only; host a DNS name (no IP literal, no userinfo, no fragment, no query); port 443 or absent; path kept. Normalised (lower-cased host, no default port) before it is stored. 1-512 characters |
| `transport` | `streamable-http`, the one value (Q5) |
| `description` | 0-280 characters, shown in the agent's prompt preamble beside the name, so the agent knows what the server is for. The tenant's own text |
| `auth` | `{mode, header, scheme}`. `mode` is `none`, `dispatch` (a per-run credential the caller sends) or `workspace_secret` (the grant `mcp:<name>`, §4.5). `header` defaults to `Authorization`; any other must match `[A-Za-z0-9-]{1,64}` and not be one of `Host`, `Cookie`, `Content-Length`, `Content-Type`, `Accept`, `Transfer-Encoding`, `Connection`, `Mcp-Session-Id`, `MCP-Protocol-Version`, `Proxy-Authorization`. `scheme` is `Bearer` (default) or empty (the value is the header's whole value) |
| `allowed_profiles` | a subset of the profiles that declare `mcp_servers` (request M1); empty means all of them |
| `allowed_tools` | MCP tool names an agent may call, `[A-Za-z0-9_.-]{1,64}`, at most 64; empty means every tool the server lists (Q4) |
| `limits` | `{max_request_bytes, max_response_bytes, calls_per_minute, call_timeout_seconds}`; defaults 1 MiB, 4 MiB, 60, 60; ceilings 8 MiB, 32 MiB, 600, 300 |
| `enabled` | the kill switch. Disabling parks queued tasks that name it (§4.10); it is never deleted, so its history reads beside `admin_audit` |
| `revision` | incremented on every change; recorded on every attach row, so "which URL did attempt X call" has an answer |
| `created_at`, `created_by`, `updated_at`, `updated_by` | emails from the verified session, never from a body |

**No credential field, no secret name.** A `workspace_secret` server's key is
found through the grant (§4.5), a `dispatch` server's through the task's
credential row (§4.4).

**Who registers** is Q1; the recommendation is platform admins only, as for a
group workspace's secrets (owner decision Q4 of #1347), with a person
managing their own personal workspace.

**swarm-api never calls a registered URL.** Not at registration and not to
"verify": swarm-api runs as a platform identity with reach into every
tenant, and a URL a tenant typed is exactly what an SSRF needs. The first
call a server sees is from a worker of that tenant, through the relay's
address checks (§4.8). A registration's "test" is a dispatch.

Every write is audited in the same transaction, in `admin_audit`:
`mcp_server_registered`, `_updated`, `_disabled`, `_enabled`, with
`{tenant_id, server_id, name, url, auth.mode, revision}`.

### 4.3 How a step names a server

**In the task's `input`**, as the profile-declared input `mcp_servers` (request
M1): a list of one to four registered names, no duplicates.

```json
{"runner_profile": "claude-code",
 "input": {"prompt": "…", "mcp_servers": ["kb"]},
 "repository_url": "https://github.com/acme/app",
 "mcp_credentials": {"kb": {"value": "<the caller's credential>", "expires_in": 3600}}}
```

Why `input` and not a new `Task` field:

* `input` is already covered by the spec signature
  (`apps/common/swarm_common/specsign.py::canonical_step_spec`, format 3), so
  the worker can trust the list it reads from Firestore without a format 4.
* `profiles.check_inputs` already refuses a list that is too long, an
  undeclared key on a profile that does not take MCP servers, and a
  wrong-typed item, with the 422 shape callers already branch on, and the
  console and the plugin already render declared inputs.
* It reaches the runner through `input.json` as names only, which is
  harmless.

swarm-api then checks, in `service.py::_build_task` after
`validate_runner_input` and before anything is stored:

| refusal | status, code |
|---|---|
| a name not registered for the task's tenant, or registered and disabled | 422 `mcp_server_unregistered` (one code for both, so the answer does not say whether another tenant has the name) |
| registered, but the profile is not in `allowed_profiles` | 403 `mcp_server_profile_refused` |
| `auth.mode == dispatch` and no `mcp_credentials` entry for it | 422 `mcp_credential_missing` |
| an `mcp_credentials` key the input does not name, or one for a server whose mode is not `dispatch` | 422 `mcp_credential_unexpected` |
| a value not 8 B-8 KiB of printable ASCII on one line, or `expires_in` outside 60-86400 s | 422 `mcp_credential_shape` (header-safe by construction, as `typesafe_shim` holds a key to one printable line) |

Every refusal names the field and the rule, never the value. The
`RequestValidationError` handler in `apps/swarm-api/swarm_api/main.py::create_app`
already returns only each error's `loc`, `msg` and `type`, never its `input`;
the MCP-SUBMIT lane holds that with a test that a runtime-built credential is
absent from every 4xx body.

**Workflows.** `WorkflowStepCreate.input` carries `mcp_servers` per step.
`WorkflowCreate` gains one top-level `mcp_credentials` map, because the
credential is the caller's for the whole submission: each step that names a
`dispatch` server uses that one stored credential. A step that names a server
the map has no entry for is refused as above.

**Children.** A child task submitted by a running agent
([child-tasks.md](child-tasks.md)) may name only servers its parent named, and
uses the parent's stored credential row; a child cannot add a server or a
credential, because the agent writing the child's spec is exactly the party
the credential is kept from (Q7).

**Schedules and issue runs** name servers like any step; a scheduled run has no
caller online to mint a credential, so it can use `none` or
`workspace_secret` servers only and is refused `mcp_credential_missing` for a
`dispatch` server at creation time, not at each fire.

**The response** echoes `input.mcp_servers` and, for each stored credential,
`{"server": "kb", "expires_at": "…"}`. Never the value, its length, a prefix
or a hash.

### 4.4 The per-run credential at rest

* **Where.** One Secret Manager secret per (submission, server):
  `Tenant.secret_name("mcpc-<16 hex>")`, i.e.
  `swarm-tenant-<tenant>-mcpc-<16 hex>`, the hex random. `mcpc-` is a marker
  no existing suffix uses, so an IAM condition can find it, as `wsec-` does.
  It is created with Secret Manager's own `ttl` set to `expires_in` plus 5
  minutes, so a sweep that never runs still cannot leave it behind, and
  labelled `managed-by=swarm-api`, `swarm-kind=mcp-credential`.
* **The row.** `mcp_dispatch_credentials/{owner_id}~{server_id}`, where
  `owner_id` is the task id (or the workflow id for a workflow's map):
  `{owner_id, tenant_id, server_id, name, secret_id, version, expires_at,
  state: live|destroyed, created_at, created_by, destroyed_at,
  destroyed_reason}`. No value, length, prefix or hash.
* **Who reads it.** swarm-api only, under a conditioned
  `secretmanager.versions.access` grant on
  `extract("/secrets/swarm-tenant-{t}-mcpc-") != ""` (lane MCP-IAM; the owner
  applies bootstrap). Creating and destroying use swarm-api's existing
  `swarmProviderKeyWriter` reach over `swarm-tenant-*`; MCP-IAM confirms the
  role holds `secretmanager.secrets.create`, `versions.add` and
  `secrets.delete` and asks for any it lacks. The tenant worker account holds
  nothing on `-mcpc-`, and MCP-IAM's tftest proves it.
* **Destroyed when.** The task (every task of the workflow, for a workflow's
  map) is terminal, or `expires_at` passes, whichever is first:
  `POST /v1/admin/mcp-credentials/sweep`, run every 10 minutes by a Cloud
  Scheduler job as the rollup-sweeper account (the shape of the workspace
  secrets' rotation sweep), deletes the secret and marks the row
  `destroyed`. Not at the end of each attempt, because a retry is a new
  attempt of the same task and needs it (Q2). The worker's copy dies with the
  attempt regardless (§4.7).
* **Expiry in the queue.** A task whose credential expires before it is
  admitted fails `mcp_credential_expired` at admission, with no lease
  (§4.10). It cannot park: no one can supply a new value to a queued task.

### 4.5 Long-lived credentials: a workspace secret granted to `mcp:<name>`

A registration with `auth.mode: workspace_secret` takes its key from the
workspace store of #1347, with no new storage:

* the consumer catalogue gains a fourth kind, `mcp` (request M2), so a grant
  is `workspace_secret_grants/{secret_id}~mcp:kb`, with the `enabled` flag,
  attestation and audit rows the store already has;
* the consumer has one slot, `auth`, accepting `api_key` and `bearer_token`;
* `GET /v1/workspace/secrets/consumers` lists `mcp:<name>` for every enabled
  registration of the workspace, so the console's matrix shows it.

The attach route (§4.6), not `secrets:release`, reads such a grant: the
release writes files the agent can read (workspace-secrets.md §4.1), and an
MCP credential must never be a file. So the release must never hand out an
`mcp:` grant, and the attach must take them. **The held invariant is that
`workspace_secrets.attempt_consumers` never derives an `mcp:` consumer**
(request M2 says so): `release_plan` returns only the grants naming the
`consumers` it is passed, so as long as no `mcp:` name is ever among them it
cannot release one, and no filter inside `release_plan` is added (it would
guard a case that cannot arise and be the wrong thing to test). The MCP-API
lane's test is on `attempt_consumers`: for every profile, with and without
`mcp_servers` in its signed input, no derived consumer parses as kind
`mcp`; and, end to end, an `mcp:` grant is not in `secrets:release`'s answer. A key granted to `mcp:kb` and also to
`profile:claude-code` is released both ways, as the store's own rule says: the
boundary is the attempt.

### 4.6 The attach route

```
POST /v1/attempts/{attempt_id}/mcp:attach
body: {"generation": <int>}                 (nothing else; any other field is 422)
signed with the attempt key, purpose "swarm-mcp-attach/v1"
```

Worker-only, like `secrets:release`: no person can call it, and the request
carries no tenant, task, server name or consumer to choose. In order,
refusing at the first failure:

1. **The attempt.** The path's attempt id names one lease, which names the
   tenant and task; the tenant's worker identity, then the attempt key's
   signature over the raw body under `swarm-mcp-attach/v1` (its own purpose,
   so a release, egress, live or child signature never verifies). Every
   failure is the same 403.
2. **The fence** (invariant 5). STARTING or RUNNING at this lease and this
   generation, lease unreleased; else 409 and nothing.
3. **Once.** A second attach for the attempt is 410.
4. **The signed spec.** The server names are read from the task's verified
   `input.mcp_servers` (`verdictpublish.spec_refusal`, as the release does);
   an unverified task is attached nothing.
5. **The registrations**, re-read now: a server disabled since submission, or
   whose `allowed_profiles` no longer includes the task's profile, is
   `withheld` with that reason. The registration's current `revision` is
   used, so an admin's URL change takes effect at the next attempt, as a
   grant change does in the store.
6. **The credentials.** `dispatch`: the task's row (or its workflow's, or its
   parent's for a child), `state == live`, unexpired, then the Secret Manager
   version. `workspace_secret`: the enabled, attested grant
   `mcp:<name>` of the task's tenant, then its `current_version`. Missing or
   unreadable: `withheld` with a reason; a read swarm-api may not make (the
   IAM not applied) is 503 with a reason and no name.
7. **The rows.** One `mcp_attach_releases` row per server
   (`at, task_id, attempt_id, generation, tenant_id, server_id, name,
   revision, auth_mode, secret_id, version, submitted_by`, `expire_at` 90 days
   out for a Firestore TTL, as owner decision Q6 of #1347 set for release
   rows), in the transaction that re-checks the fence.

**Answer** `200`:

```json
{"servers": [{"name": "kb", "url": "https://kb.example.com/mcp", "revision": 3,
              "description": "…", "auth": {"header": "Authorization", "scheme": "Bearer",
              "credential": null},
              "allowed_tools": ["search", "record_decision"],
              "limits": {"max_request_bytes": 1048576, "max_response_bytes": 4194304,
                         "calls_per_minute": 60, "call_timeout_seconds": 60}}],
 "withheld": [{"name": "…", "reason": "disabled|profile_refused|credential_expired|credential_missing|unreadable"}]}
```

`credential` is the value as a string, or null for mode `none` (shown null
above so the example holds nothing credential-shaped).

It is the **second route that serves credential material**, beside
`secrets:release`, so `test_no_route_serves_credentials.py` names it as a
carve-out and `main.UNCAPTURED_ROUTES` lists it, and `POST /v1/tasks`,
`/v1/tasks/batch` and `/v1/workflows` join that list too, because their
bodies now carry credentials (Q9). The handler reads the raw body itself and
builds the answer from locals, as `attempts_secrets.py` does.

**Why a sibling route and not more of `secrets:release`:** the release is
"once per attempt, and only when the profile or its tools have slots"
(`apps/agent-worker/agent_worker/secretrelease.py::SecretRelease.release`), it
writes every value to a file, and it has just shipped with its own review.
Widening it would put MCP credentials on its file path and re-open a reviewed
security route. The sibling reuses its fence and signature helpers
(`childkey.verify_proof`, the lease lookup) by import, not by copy.

**The attempt key must exist.** The scheduler issues the registration nonce
only under three conditions (`dispatch.py::worker_env`, "THE ATTEMPT KEY");
MCP-SCHED adds "the task's signed input names an MCP server", as
SEC-RELEASE added `consumes_workspace_secrets`. A deployment with the child
key off registers no key, so a task naming a server there is refused at
submission with `mcp_unavailable` (503) rather than admitted to fail.

### 4.7 The worker: attach, relay, config, cleanup

**Order in `lifecycle.py`**, after the secrets release (STEP 6a) and before
the runner's environment is built, so every value is registered before the
agent exists:

1. `mcp_attach.attach(...)` calls the route once (§4.6). A 409 marks the
   attempt fenced and exits without running the agent, as
   `SecretReleaseFenced` does. Each credential is passed to
   `logger.register_secret` **before** it is validated, so even a refused
   item is redacted.
2. **A withheld server fails the attempt before the agent runs**
   (`mcp_server_withheld:<name>:<reason>`), non-retryable for
   `credential_expired`, retryable for `unreadable` (Q2). The caller named the
   server because the agent needs it; running without it would produce work
   built on knowledge the agent was told it had and did not.
3. `mcp_relay.Relay(servers, logger, fence).start()` binds `127.0.0.1` on an
   ephemeral port, one path per server (`/mcp/<name>`), and generates one
   local token per attempt (`secrets.token_urlsafe(32)`), registered for
   redaction too.
4. The runners are handed `[(name, relay_url, local_token, description)]`
   through the runner context (never the credential):
   * **claude-code**: `write_mcp_config` gains `relay_servers`, each written as
     `{"type": "http", "url": <relay url>, "headers": {…}}`, the one header
     being the authorization header with the local token, bearer scheme,
     under the name `kb`, in the one config file it
     already writes (one `--mcp-config` per attempt, its docstring's rule).
   * **codex**: `-c mcp_servers.kb.url=<relay url>` and
     `-c mcp_servers.kb.bearer_token_env_var="SWARM_MCP_RELAY_TOKEN"`, the local
     token in the CLI child's environment only. **Not verified:** that codex
     0.154.0 (`CODEX_VERSION`) accepts both keys, and that claude-code 2.1.283
     accepts an `http` entry with `headers` in `--mcp-config`. MCP-RUNNERS'
     first red test runs each pinned binary against a loopback fake and
     decides; if codex lacks the env-var key, the token goes in
     `http_headers` as an override (argv, which the agent can read anyway).
   * the prompt preamble gains one line per server: its name, its
     description, and "its tools are listed under the MCP server `kb`".
5. **The local token in `work/` is deliberate.** The claude-code config lives
   in `work/`, which checkpoints carry. What it holds is a loopback URL and a
   token that nothing outside this pod can reach and nothing after this
   attempt will accept, because the relay is gone; acceptance check 3 is about
   the caller's credential, which is never there. The next attempt rewrites
   the file (`_write_private` unlinks and recreates it). A test asserts that
   the stored checkpoint carries no credential, and that its local token
   differs from the next attempt's.
6. **Cleanup** joins the lifecycle's ordered steps (where `stop_typesafe_shim`
   and `remove_released_secrets` are), right after `terminate_runner`: the
   relay closes its listener and upstream connections and drops every
   reference to the credentials. **Stated honestly:** a Python `str` cannot be
   zeroed, so "no copy" after close means no reference, and the bytes are
   gone when the worker process exits, which it does at the end of every
   attempt. Acceptance check 4 is met by that exit, and the test for it
   asserts the relay holds no reference after close and that no file under
   `work/` or `private/` contains the value.
7. **Publish.** The value is a registered secret, so the publish-time scan
   (`lifecycle.Worker._leaks_in_added_text`) already refuses a diff that
   echoes it, by value, with `REGISTERED_SECRET_RULE`.

### 4.8 The relay: what the agent may and may not send

The relay forwards MCP streamable HTTP: `POST` (JSON-RPC request, answered
with JSON or an SSE stream), `GET` (the server-to-client SSE stream) and
`DELETE` (session end), on `/mcp/<name>` only. Every other method or path is
404. Checks, in order, before anything leaves the pod:

| check | refusal |
|---|---|
| the peer is loopback | 403 |
| exactly one authorization header, bearer scheme, whose value is the local token (constant-time compare) | 401 |
| the attempt is not fenced (the lifecycle's control poll sets the flag) | 409; the worker exits as for any fence |
| `Content-Length` present (no chunked request), at most `max_request_bytes` | 413 |
| the body parses as JSON-RPC 2.0 (a single message or a batch) | 400 |
| every `tools/call` names a tool in `allowed_tools` (when the list is set) | a JSON-RPC error `tool_not_allowed`, nothing forwarded |
| the body contains no value registered with the logger (provider keys, released secrets, ticket values, this or another server's credential) and matches no `swarm_redaction` credential rule outside `SHAPE_EXEMPT` (amended 2026-10-18, below) | a JSON-RPC error `credential_in_request`, nothing forwarded, logged with the rule name only |
| the per-attempt, per-server sliding-minute rate is under `calls_per_minute` | a JSON-RPC error `rate_limited` |

Then it forwards:

* **Headers out:** `Content-Type`, `Accept`, `Mcp-Session-Id`,
  `MCP-Protocol-Version`, `Last-Event-ID` from the agent; the registration's
  auth header, set by the relay; a fixed `User-Agent: swarmcloud-mcp-relay`
  and `X-Swarm-Task`/`X-Swarm-Attempt` ids (not secrets; they let the caller's
  server audit by attempt). Every other header the agent sent, any
  authorization or cookie included, is dropped. **No GSA token, ID token or
  platform credential is ever attached.**
* **Where:** the registration's URL exactly; a request path beyond
  `/mcp/<name>` is 404, so the agent cannot choose a path on the server. DNS is
  resolved by the relay; every address must be public (not loopback, RFC1918,
  link-local and the metadata server, CGNAT `100.64/10`, ULA, multicast, or
  the cluster's pod and service ranges); the connection goes to the checked
  address with SNI and certificate verification against the registered host
  (`ssl.create_default_context()`), so a DNS answer that changes between check
  and connect cannot redirect it. Port 443 only. **No redirect is followed**;
  a 3xx reaches the agent as an error.
* **Headers back:** `Content-Type`, `Mcp-Session-Id`, and the status. An SSE
  body is streamed event by event.
* **Answers:** each forwarded chunk is scrubbed of registered secrets (so a
  server that echoes the credential never shows it to the agent), capped at
  `max_response_bytes` per call, and bounded by `call_timeout_seconds`
  overall, checked between chunks as well as per read (the TypeSafe
  forwarder's rule). A server-to-client request arriving on the stream
  (sampling, elicitation) is passed through unchanged; the CLI decides
  whether it supports it.
* **Logs:** one `mcp_call` event per request: server name, JSON-RPC method,
  tool name for `tools/call`, status, request and response bytes, duration,
  refusal code. **Never arguments, results, headers or URLs with queries.**
  Each line goes through the logger's redaction anyway.

What the agent **may** send: any call to an allowed tool, with any arguments,
including repository content and its own conclusions. That is the feature, and
the server is the tenant's own, registered by its admin, so sending the
tenant's work to it is the tenant's decision (Q4 is about narrowing it).
What it **may not** send: a credential the platform knows (refused by value
and by shape), a call to a tool not on the list, more than the size and rate
caps, anything to any host but the registered one.

**Amended 2026-10-18, as the owner approved.** The `credential_in_request`
check does not apply two of the `swarm_redaction` rules: the key/value rule
(`key_value_assignment`) and the bare `Bearer|Basic <12+ chars>` rule
(`http_authorization`). Both fire on ordinary prose an agent legitimately
sends a knowledge server: the first on `token: 4000` or `password: field`,
the second on the word basic or bearer followed by any word of twelve
letters or more, which English about authentication is full of (the relay's
own test sends one such sentence and sees it forwarded). Masking a false
positive in a log costs a word where refusing one costs the agent its write.
The owner's note gave `token_budget: 4000` as its example; measured at main
on 2026-10-18 neither rule fires on that line, but they fire on the examples
above. What stays refused: every registered secret value, by value, whatever
its shape; and every token-shaped string by the remaining rules, including a
value after an `Authorization` header's scheme word
(`http_authorization_scheme`). The exemption is the relay's alone:
`swarm_redaction.RULES` is unchanged, so every reader that masks with it, the
API's log routes among them
(`apps/swarm-api/swarm_api/redaction.py::redact_lines`), still masks what the
two rules match, where a false positive costs one masked word. The list is
`apps/agent-worker/agent_worker/mcp_relay.py::SHAPE_EXEMPT`, and the relay's
check is `apps/agent-worker/agent_worker/mcp_relay.py::credential_rule`.

What the relay does **not** protect against, said once: a malicious or
compromised registered server can return text that tries to steer the agent
(prompt injection). That is the same exposure as a fetched web page or an
issue body, and the guards against its consequences are the existing ones:
the agent's git tools refuse pushes outside its branch, publish scans refuse
credentials, and the credential itself is not in reach.

### 4.9 Egress

* **GKE.** `allow-egress.yaml` rule 4 already admits TCP 443 to every public
  IPv4 address, so a public HTTPS server needs **no NetworkPolicy change**.
  Traffic leaves through `google_compute_router_nat.this`
  (`terraform/modules/network/main.tf`), `MANUAL_ONLY` addresses, so the
  caller can allowlist SwarmCloud's NAT IPs on their server. The console's
  registration page shows those addresses (read from a Terraform output
  rendered into swarm-api's settings; MCP-IAM adds the output).
* **Cloud Run Jobs.** `vpc_access { egress = "ALL_TRAFFIC" }`
  (`terraform/modules/cloud_run_jobs/main.tf`) sends worker traffic through
  the same NAT, so the same addresses apply. Where `restrict_egress` is on,
  443 is in `egress_allowed_ports` by default.
* **Private endpoints** (a VPC, on-premises, `*.internal`) are refused at
  registration (no IP literals) and at connect (private address), and the pod
  policy would drop them anyway. Reaching them needs Private Service Connect
  or a tenant-specific route, which is a separate design (Q6).
* **The swarm-egress proxy is not used.** It carries only the live browser's
  Chromium through a residential vendor (`docs/browser-egress.md` §2.1); an
  MCP call has no reason to look like a residential browser.

### 4.10 Admission

In the scheduler, beside the existing credential park
(`apps/scheduler/scheduler/secret_slots.py`, `credentials.py`), before any
lease:

* a named server whose registration is disabled or no longer allows the
  profile: **park `CREDENTIAL_MISSING`** with code `mcp_server_unavailable`
  and the server's name; the credential sweep returns it to `READY` when the
  registration is enabled again (invariant 1: parked costs nothing);
* a `dispatch` server whose credential row is `destroyed` or past
  `expires_at`: **`FAILED`** with `mcp_credential_expired`, no lease, no
  retry. Nobody can hand a queued task a new value, so parking would wait for
  ever.

The worker's attach (§4.6 step 5-6) repeats both checks, because state moves
between admission and attach; the attach is the authority, admission is the
cheap early answer.

### 4.11 Console, plugin and API surfaces

* **swarm-api** (`/v1/mcp-servers`): `POST` (register), `GET` (list, the
  tenant's), `GET /{name}`, `PATCH /{name}` (url, description, auth,
  allowed_profiles, allowed_tools, limits, enabled), no `DELETE` (disable
  instead; the history stays). Admin-only writes (Q1). Responses carry no
  credential, because the registration has none.
* **Console:** Settings › Workspace › MCP servers, a sibling of the planned
  Secrets page: a table of registrations; a form with the URL, auth mode,
  profile checkboxes and allowed tools; the NAT addresses to allowlist; "last
  used" from `mcp_attach_releases`. A `workspace_secret` server links to the
  Secrets page's `mcp:<name>` grant. The Submit forms render the
  `mcp_servers` input as a multi-select of registered, enabled servers, and a
  credential field per `dispatch` server that is never echoed back.
  Settings tabs are not in `SECTIONS`, so the issue forms' "Where" list
  (`test_issue_forms.py`) is unchanged.
* **swarm-mcp and the plugin:** `swarm_dispatch` and each `swarm_workflow` step
  gain `mcp_servers` through the existing `_INPUTS_SCHEMA` path (it is a
  declared input); both gain a top-level `mcp_credentials` object. A new
  read-only `swarm_mcp_servers` tool lists the tenant's registrations. **No
  tool registers a server or stores a long-lived credential**, as no tool may
  accept a secret value for the store (SEC-SC's rule); a per-run credential is
  passed through to the API and nowhere else, and the plugin's local logs
  redact the `mcp_credentials` object by key.

---

## 5. Frozen contract: change requests

`apps/common/swarm_common/` is frozen. These are requests, written in the
shape of [contract-change-requests.md](../contract-change-requests.md); the
operator files them there, numbered from the next free entry (85 to 88 on
2026-10-17), once the owner approves this design. Nothing here edits the
contract.

### Request M1. `profiles.py`: the CLI agent profiles declare the input `mcp_servers`

**Status:** proposed 2026-10-17 by lane CALLER-MCP-DESIGN, part of #1433.

#### Why

A step names its MCP servers by name. The list must be declared once, where
swarm-api's `check_inputs`, the plugin's local catalogue check and the
console's Submit form already read declared inputs (request 25), and it must
be inside `input` so the spec signature (format 3) covers it with no new
format.

#### The change

The input is **not** added to `_CLI_AGENT_INPUTS`. That dict is the `inputs=`
of four profiles (`claude-code`, `codex`, `claude-code-review`, `indexer`)
and is spread into `_CLAUDE_CODE_BROWSER_INPUTS`, so a key added there would
reach `indexer` and `claude-code-review` too, and with them the relay, since
an empty `allowed_profiles` means "every profile that declares
`mcp_servers`" (§4.2). Neither should have it: the indexer runs no prompt a
caller wrote, and a review step reads the diff it was given, not a caller's
knowledge base; if a review step should call one later, that is one more
spread and a request of its own. So the input gets its own dict, spread into exactly three
profiles:

```python
#: Contract request <85>: the registered MCP servers a step may call
#: (docs/design/registered-mcp-servers.md). Its own dict, NOT a key of
#: _CLI_AGENT_INPUTS, which indexer and claude-code-review also take.
_MCP_SERVER_INPUTS: dict[str, RunnerInput] = {
    "mcp_servers": RunnerInput(
        "list",
        minimum=1,
        maximum=4,
        items=RunnerInput("string", maximum=31),
        means=(
            "registered MCP servers, by name, the agent may call during its run; "
            "each must be registered and enabled for the task's workspace and allowed "
            "for this profile (docs/design/registered-mcp-servers.md)"
        ),
    ),
}

#: claude-code and codex: the CLI agent inputs plus mcp_servers.
_CLI_AGENT_MCP_INPUTS: dict[str, RunnerInput] = {
    **_CLI_AGENT_INPUTS,
    **_MCP_SERVER_INPUTS,
}

_CLAUDE_CODE_BROWSER_INPUTS: dict[str, RunnerInput] = {
    **_CLI_AGENT_INPUTS,
    **_MCP_SERVER_INPUTS,                             # added
    "browser_tool": RunnerInput(...),                 # unchanged
}
```

and `"claude-code"` and `"codex"` change `inputs=_CLI_AGENT_INPUTS` to
`inputs=_CLI_AGENT_MCP_INPUTS`. `claude-code-review` and `indexer` keep
`inputs=_CLI_AGENT_INPUTS` unchanged, and `_CLI_AGENT_INPUTS` itself is not
edited.

So `claude-code`, `codex` and `claude-code-browser` gain it; `indexer`,
`claude-code-review` and every profile without declared inputs (`mock`,
`generic`, `browser`, `merge`, `post-verdict`) do not. Membership is checked
by swarm-api against the registry, because the registry is per tenant and
changes at runtime, so the contract cannot list names as `choices`.

#### Rollback

Remove the key. Tasks already stored with it fail `check_inputs` on any
re-validation, so roll back only with the registry disabled.

#### What it would break

Nothing that exists: a task without the key is unchanged. The plugin's local
catalogue copy (`apps/swarm-mcp/swarm_mcp/profiles.py`) must learn the input
in the same release, or a local check refuses it (lane MCP-SC).

#### Invariants

**10** is why it exists: names, never a URL or a command. **5**: the list is
signed, so a worker never attaches a server a tampered task document added.
1-4 and 6-9 untouched.

### Request M2. `models.py` and `profiles.py`: `mcp` is a secret-consumer kind with one slot, `auth`

**Status:** proposed 2026-10-17 by lane CALLER-MCP-DESIGN, part of #1433.

#### Why

A long-lived MCP credential is a workspace secret granted to the server that
uses it (§4.5). Requests 80-82 made a consumer's spelling and slots contract,
so that swarm-api, the worker and the console read one declaration; an MCP
server is a fourth kind of consumer.

#### The change

```python
# models.py
SECRET_CONSUMER_KINDS: tuple[str, ...] = ("profile", "tool", "component", "mcp")
# consumer_name: `mcp:` names are checked against MCP_SERVER_NAME (request M3);
# membership in a tenant's registry is checked by its reader, as for `profile:`.

# profiles.py
MCP_SECRET_SLOTS: Mapping[str, SecretSlot] = MappingProxyType({
    "auth": SecretSlot(
        kinds=("api_key", "bearer_token"),
        means="the relay adds it as the registered server's auth header; never a file",
    ),
})
```

#### Rollback

Remove the member. Grants already written to `mcp:` consumers stop parsing
and are dropped by the release and the attach as unknown, which disables them
safely.

#### What it would break

`parse_consumer` gains a fourth accepted prefix; any reader that switched on
the three kinds exhaustively must handle `mcp` (the release's
`attempt_consumers` must exclude it, §4.5).

#### Invariants

**9**: a grant is still bounded by its own `tenant_id`. **10**: a consumer is
still a name. 1-8 untouched.

### Request M3. `models.py`: one spelling for an MCP server's name

**Status:** proposed 2026-10-17 by lane CALLER-MCP-DESIGN, part of #1433.

#### Why

The name is written by swarm-api (registration), read by the worker (it
becomes an MCP server key in the CLI's config and a path segment of the relay)
and by the plugin, and must never collide with a server the worker registers
itself.

#### The change

```python
MCP_SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
#: Names the worker registers itself; a registration may not take them.
RESERVED_MCP_SERVER_NAMES: tuple[str, ...] = ("playwright", "jev")
RESERVED_MCP_SERVER_PREFIX = "swarm-"
```

#### Rollback

Remove the constants; each reader would restate them, which is the drift the
contract exists to stop.

#### What it would break

Nothing; new names.

#### Invariants

**10**: a server is named, never addressed. 1-9 untouched.

### Request M4. No new spec format, park reason or event type

**Status:** proposed 2026-10-17 by lane CALLER-MCP-DESIGN, part of #1433.
Requests **no change**, recorded so no lane adds one.

* **Spec signing:** `input` is already in `canonical_step_spec`, so
  `mcp_servers` is signed by format 3. The credential is not in the task and
  must not be signed into it. `SPEC_FORMAT` stays 3.
* **Park reason:** a disabled registration parks `CREDENTIAL_MISSING`, with
  code `mcp_server_unavailable`, exactly as request 83 reused it for a missing
  secret grant.
* **Event types:** the relay logs `mcp_call` as a structured log line, not a
  task event; the attach's audit is its own collection. `EventType` is
  unchanged.

**Not requested:** `Tenant.secret_name` already spells `mcpc-<hex>`;
`Task` gains no field; `RunnerProfile.secrets` is unchanged.

---

## 6. Build plan

Each lane owns the files named and no file is in two lanes (CLAUDE.md,
"Issues"). Every lane pushes its tests first to the PR branch and its fix to
`<branch>-fix`, and runs `scripts/changed-guards.sh` before finishing. The
review CLAUDE.md requires for credentials, isolation, redaction and IAM
applies to MCP-IAM, MCP-API, MCP-SUBMIT and MCP-WORKER.

| order | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **MCP-CONTRACT** (owner applies) | `apps/common/swarm_common/profiles.py`, `apps/common/swarm_common/models.py`, `docs/contract-change-requests.md`, `tests/unit/common/test_mcp_contract.py` (new) | `check_inputs` accepts `mcp_servers: ["kb"]` on claude-code, codex, claude-code-browser and refuses it on `browser`, `merge`, `indexer` and `claude-code-review` (`indexer` and `claude-code-review` share `_CLI_AGENT_INPUTS`, which is why the input has a dict of its own); `check_inputs` still accepts `issue` on `indexer` and `claude-code-review` (their inputs are unchanged); refuses five names and a non-string; `consumer_name("mcp", "kb")` round-trips through `parse_consumer`; `MCP_SERVER_NAME` refuses `swarm-git`'s prefix use via the reserved list; `canonical_step_spec` of a task with `mcp_servers` differs from the same task without (it is signed) |
| 1 | **MCP-IAM** (owner applies bootstrap) | `terraform/bootstrap/mcp_credentials.tf` (new), `tests/terraform/mcp_credentials.tftest.hcl` (new), `terraform/modules/scheduler/jobs.tf` (the 10-minute sweep job), `tests/terraform/mcp_sweep.tftest.hcl` (new), the NAT-address output in `terraform/infra/outputs.tf` | the access condition admits `swarm-tenant-a-mcpc-<hex>` and its versions; refuses `-wsec-`, `-git`, `-typesafe`, `swarm-github-app-*`, `agents-*`, another project; no grant to a tenant worker admits `-mcpc-`; the sweep job calls only its route as the rollup sweeper; every resource carries `managed-by=swarm-terraform` |
| 1 | **MCP-API** | `apps/swarm-api/swarm_api/mcp_servers.py` (new), `mcp_credentials.py` (new), `routes/mcp_servers.py` (new, incl. the admin sweep), `routes/attempts_mcp.py` (new), `main.py` (routers, `UNCAPTURED_ROUTES`), `auth.py` (`ROLLUP_SWEEPER_ROUTES`), `workspace_secrets.py` (list `mcp:` consumers; `attempt_consumers` unchanged and held by a test never to derive one, §4.5), `tests/unit/control_plane/test_mcp_servers.py`, `test_mcp_attach.py` (new), `test_no_route_serves_credentials.py` (the second carve-out), `test_rollup_sweeper_is_narrow.py` | another tenant's server is 404; a non-admin write is 403; a `http://`, IP-literal, userinfo, non-443 or reserved-name registration is 422; every write has its audit row in the transaction; attach: unsigned or wrongly signed is 403, stale generation 409 with nothing, second call 410, a disabled server is withheld, an `mcp:` grant is attached and never released by `secrets:release`; a runtime-built credential is absent from every log line, Firestore document and non-attach response; the sweep destroys a terminal task's secret and marks the row |
| 2 | **MCP-SUBMIT** (after MCP-API) | `apps/swarm-api/swarm_api/schemas.py`, `service.py`, `validation.py`, `children.py` (`_build_child`: the child's `mcp_servers` must be a subset of the parent's, read through `_read_parent`, and the child's task points at the parent's credential rows, because a child has no `mcp_credentials` of its own), `schedules.py` (`build_schedule` and `apply_edit`: refuse a `dispatch` server at creation and on edit), `tests/unit/control_plane/test_mcp_submit.py` (new) | unregistered name 422 before anything is stored; profile not allowed 403; missing, unexpected and malformed credentials 422; a runtime-built credential is in no 4xx body, no task document, no workflow document, no response, and exactly one Secret Manager `add_version` call with a TTL; a workflow's map serves every step; a child cannot add a server, and a child naming its parent's server is attached with the parent's credential; a schedule naming a `dispatch` server is refused at creation and on edit (a firing goes through `SubmissionService.submit_tasks`/`submit_workflow`, so `schedulefire.py` needs no change: a fire that somehow names one is refused `mcp_credential_missing` by `_build_task` like any submission) |
| 2 | **MCP-SCHED** (after MCP-CONTRACT) | `apps/scheduler/scheduler/mcp_admission.py` (new), `credentials.py` (its sweep promotes the park), `dispatch.py` (`worker_env`'s attempt-key condition), `tests/unit/scheduler/test_mcp_admission.py` (new) | a disabled registration parks `CREDENTIAL_MISSING` before any lease and is promoted when re-enabled; an expired credential is FAILED with no lease; a task naming a server gets the attempt-key nonce; a task without one is unchanged |
| 3 | **MCP-WORKER** (after MCP-API) | `apps/agent-worker/agent_worker/mcp_attach.py` (new), `mcp_relay.py` (new), `lifecycle.py` (attach, relay start, fence flag, cleanup step), `tests/unit/worker/test_mcp_relay.py`, `test_mcp_attach.py` (new) | against a loopback fake server: the credential arrives as the registered header and nowhere else; a wrong local token is 401; a non-allowed tool is refused unforwarded; a body carrying a registered secret or a credential-shaped string is refused unforwarded; a 3xx is not followed; a private, loopback or metadata address is refused; size, rate and timeout caps hold; an echoed credential is scrubbed from the answer; after the fence flag nothing is forwarded; after close no reference remains and no file under the workspace holds the value; a 409 attach exits without the agent; a withheld server fails the attempt before the agent |
| 4 | **MCP-RUNNERS** (after MCP-WORKER) | `apps/agent-worker/agent_worker/runners/claude_code.py` (`write_mcp_config`, the preamble line), `runners/codex.py`, `tests/unit/worker/test_mcp_runner_config.py` (new) | the claude-code config holds the relay URL and local token and never the credential; codex's overrides likewise; the pinned binaries list the fake server's tools through the relay (in the image test, where the binaries exist); a checkpointed `work/` holds no credential; the repository-`.mcp.json` question of §1.2 answered by a test either way |
| 3 | **MCP-SC** (after MCP-SUBMIT) | `apps/swarm-mcp/swarm_mcp/server.py`, `sc.py`, `workflows.py`, `profiles.py`, `plugin/commands/sc.md` and `plugin/README.md`, `tests/unit/mcp/test_mcp_servers_tool.py` (new) | `swarm_dispatch` and a workflow step pass `mcp_servers` and `mcp_credentials` through; no tool schema accepts a registration or a store value; the plugin's log of a dispatch holds no credential |
| 3 | **MCP-UI** (after MCP-API) | `apps/swarm-ui/src/McpServers.tsx` (new), its route in `App.tsx`, `paths.ts`, `apps/swarm-ui/src/Submit.tsx` (an MCP server picker beside the `BrowserToolPicker`, offering the tenant's enabled registrations by name; the `SUGGESTED` table is not where it goes, since that holds free-form keys), vitest files | the credential field is never rendered back after submit; a non-admin sees the list read-only; the NAT addresses are shown; a disabled server is not offered on Submit |
| 5 | **MCP-ACCEPT** | `scripts/acceptance/groups/mcp.sh` (new), `tests/acceptance/test_caller_mcp.py` (new), a dev-only echo MCP server (where it runs is Q10) | the issue's four checks: the server sees the credential; an unregistered name is refused before admission; a search over every log route, artifact, checkpoint and event of the task finds no credential (control: the same search finds a planted canary); after the attempt the worker has exited and its workspace holds no copy |
| each | **MCP-DOCS** | this file ("what was built" per lane), `docs/security.md` | the docs guards |

Order, as a graph: MCP-CONTRACT → {MCP-IAM, MCP-API, MCP-SCHED} →
{MCP-SUBMIT, MCP-WORKER, MCP-UI} → {MCP-RUNNERS, MCP-SC} → MCP-ACCEPT. Nothing
is reachable by a caller until MCP-SUBMIT merges, and MCP-SUBMIT refuses every
`mcp_servers` with `mcp_unavailable` until a setting
(`SWARM_MCP_SERVERS=on`, off by default) is set, so the lanes can merge in
order with the feature dark, as every new refusal here ships.

---

## 7. Owner questions

Each is also in this lane's `questions.json`. The recommendation is first.

**Q1. Who may register an MCP server for a group workspace?**
(a) **platform admins only, as for the workspace's secrets (#1347 Q4)**
(recommended: a registration decides where every agent of the workspace may
send its work); (b) any member registers, admins enable; (c) any member. A
personal workspace is managed by its person in every option.

**Q2. When is a per-run credential destroyed?**
(a) **when the task ends, or at its `expires_in`, whichever is first; the
worker's copy dies with each attempt** (recommended: a retry is an attempt of
the same task and needs it); (b) at the end of the first attempt, so a task
that names a `dispatch` server gets no retry; (c) only at `expires_in`.

**Q3. Which credential sources in the first build?**
(a) **dispatch and workspace secret now; a platform-minted per-attempt JWT
later** (recommended: it is what the issue asks, and the JWT needs the caller
to verify our tokens); (b) all three now; (c) platform-minted only, no
caller-supplied value at rest at all.

**Q4. The default for a registration's allowed tools?**
(a) **empty means every tool the server lists; admins narrow it**
(recommended: the server is the tenant's own); (b) empty means none, every
tool must be listed; (c) no list at all.

**Q5. Which MCP transports?**
(a) **streamable HTTP only** (recommended: the current MCP transport, and the
only one both pinned CLIs are expected to speak; the old HTTP+SSE pair needs
two endpoints the relay would have to correlate); (b) streamable HTTP and
legacy SSE.

**Q6. Servers on private networks?**
(a) **public HTTPS only, through the fixed NAT addresses** (recommended: no
new egress rule, and the pod policy already forbids private ranges);
(b) a later design for Private Service Connect per tenant; (c) allow
RFC1918 targets on 443 from worker pods (widens every tenant pod's egress).

**Q7. May a child task use its parent's servers?**
(a) **only servers its parent named, with the parent's stored credential**
(recommended); (b) never: a child names none; (c) any registered server, as
its own submission (but a child has no caller to supply a `dispatch`
credential).

**Q8. Keep the credential out of the worker too (Design C, a platform
forwarder)?**
(a) **later, if a caller's credential ever justifies it; the relay is the
seam** (recommended); (b) never; (c) in the first build, which costs a new
service, a release step and Cloud Run workers' access to the feature.

**Q9. May swarm-api serve credential material on a second route?**
(a) **yes, only `mcp:attach`, signed, fenced, once per attempt, named in
`test_no_route_serves_credentials.py`** (recommended; it is the decision Q2
of #1347 made for `secrets:release`); (b) no: fold MCP into `secrets:release`
instead, re-opening that reviewed route.

**Q10. Where does the acceptance group's test MCP server run?**
(a) **a dev-only Cloud Run service that scales to zero, labelled
`managed-by=swarm-terraform`** (recommended: costs nothing idle, reachable
through public HTTPS as a caller's would be); (b) an external endpoint the
owner provides; (c) no live acceptance; unit and integration tests only.

---

## 8. What was built

Read on main at `3fbf123` on 2026-10-18, by lane MCP-DOCS. One entry per
build-plan lane of §6, plus the follow-up lanes the build needed. Everything
below ships dark: `apps/swarm-api/swarm_api/mcp_servers.py::FLAG` names
`SWARM_MCP_SERVERS`, off unless it is exactly `on`, and while it is off a
registration write, a submission naming a server or carrying a credential,
and an attach are each refused `mcp_unavailable`. No task can name a server,
so the scheduler, worker and runner halves below never run.

**MCP-CONTRACT (part of #1433; the owner accepted it on 2026-10-17).** Contract requests 88-91 in
[contract-change-requests.md](../contract-change-requests.md), the §5
requests M1-M4 as written: `_MCP_SERVER_INPUTS` in
`apps/common/swarm_common/profiles.py`, spread into `claude-code`, `codex`
and `claude-code-browser` only; `mcp` as the fourth
`SECRET_CONSUMER_KINDS` member with its one slot, `auth`; `MCP_SERVER_NAME`
with `RESERVED_MCP_SERVER_NAMES` and `RESERVED_MCP_SERVER_PREFIX`; and no new
spec format, park reason or event type. Held by
`tests/unit/common/test_mcp_contract.py`.

**MCP-IAM (part of #1433; the owner applies bootstrap).**
`terraform/bootstrap/mcp_credentials.tf` grants swarm-api the conditioned
read on `swarm-tenant-<t>-mcpc-` secrets and nothing to a tenant worker
(`tests/terraform/mcp_credentials.tftest.hcl`); the 10-minute sweep job is in
`terraform/modules/scheduler/jobs.tf`, as the rollup sweeper
(`tests/terraform/mcp_sweep.tftest.hcl`); the NAT addresses are the
`mcp_egress_addresses` output of `terraform/infra/outputs.tf`, rendered into
swarm-api's `MCP_EGRESS_ADDRESSES`.

**MCP-API (part of #1433), and MCP-API-B.** The registration and its
routes (`apps/swarm-api/swarm_api/mcp_servers.py`,
`apps/swarm-api/swarm_api/routes/mcp_servers.py`: `GET`, `POST`, `PATCH`, no
`DELETE`), the per-run credential at rest and its sweep
(`apps/swarm-api/swarm_api/mcp_credentials.py::McpCredentials`, behind
`POST /v1/admin/mcp-credentials/sweep`), and the attach
(`apps/swarm-api/swarm_api/routes/attempts_mcp.py`, purpose
`swarm-mcp-attach/v1`), named as the second carve-out in
`tests/unit/control_plane/test_no_route_serves_credentials.py`.
`apps/swarm-api/swarm_api/workspace_secrets.py::attempt_consumers` still
never derives an `mcp:` consumer, so `secrets:release` never hands one out.
MCP-API-B added the list's `egress_addresses` and each server's
`last_attached_at`. Where the build goes beyond §4.4: every
`mcp_dispatch_credentials` row carries an HMAC under `swarm-child-key` with
its own purpose (`RowKeys`), because a tenant's agent can write Firestore
documents and a forged row could otherwise point its task at a colleague's
per-run secret; a row that does not verify is no row, for the attach and the
sweep alike.

**MCP-SUBMIT (part of #1433).** `SubmissionService.check_mcp_servers`
(`apps/swarm-api/swarm_api/service.py::SubmissionService.check_mcp_servers`)
applies §4.3's table in its order, before anything is stored:
`mcp_server_unregistered`, `mcp_server_profile_refused`,
`mcp_credential_missing`, `mcp_credential_unexpected`,
`mcp_credential_shape`. A deployment with no `swarm-child-key` refuses
`mcp_unavailable`, as §4.6 says. A child may name only its parent's servers
and uses the parent's rows (`children.py`); a schedule naming a `dispatch`
server is refused at creation and on edit (`schedules.py`). Held by
`tests/unit/control_plane/test_mcp_submit.py`.

**MCP-SCHED (part of #1433), and MCP-FAILED-EDGE.**
`apps/scheduler/scheduler/mcp_admission.py` answers before the lease: a
disabled, absent or profile-refused registration parks `CREDENTIAL_MISSING`
with `mcp_server_unavailable`, and the credential sweep promotes it when the
registration is enabled again; `dispatch.worker_env` issues the attempt-key
nonce to a task that names a server. §4.10's `FAILED` for an expired or
destroyed credential needed edges the frozen state machine did not have:
contract request 98 added `QUEUED` -> `FAILED`, and request 99 added
`READY` -> `FAILED` and `PARKED` -> `FAILED`, the two states admission and
the sweep actually hold a task in. Admission writes them, with no lease and
no reservation, in
`apps/scheduler/scheduler/loop.py::Scheduler._fail_unrunnable`. The
`McpAnswer.terminal` docstring in `mcp_admission.py` still describes the
park that preceded request 99; the code does not.

**MCP-WORKER (part of #1433).** `apps/agent-worker/agent_worker/mcp_attach.py`
calls the attach once per attempt, after the secrets release and before the
runner's environment, registers every credential for redaction the moment
the answer is parsed, validates each server again, fails the attempt before
the agent on a withheld server (`mcp_server_withheld`, retryable only for
`unreadable`) and stands down on a 409. The relay
(`apps/agent-worker/agent_worker/mcp_relay.py::Relay`) applies §4.8's checks
as amended there; the runner is handed only the path of a 0600 file under
`private/` holding the relay's loopback URLs and the local token. The
lifecycle's cleanup closes it (`close_mcp_relay` in
`apps/agent-worker/agent_worker/lifecycle.py::Worker._cleanup`). Held by
`tests/unit/worker/test_mcp_relay.py` and
`tests/unit/worker/test_mcp_attach.py`.

**MCP-RUNNERS (part of #1433).** claude-code writes each server into its one
`--mcp-config` as an `http` entry at the relay with the local token
(`apps/agent-worker/agent_worker/runners/claude_code.py::write_mcp_config`)
and adds one prompt line per server
(`apps/agent-worker/agent_worker/runners/claude_code.py::mcp_servers_note`);
codex passes `-c mcp_servers.<name>.url` and `bearer_token_env_var`
(`apps/agent-worker/agent_worker/runners/codex.py::relay_tools_args`). §4.7's
"not verified" is answered: both pinned binaries list the fake server's tools
through the relay, in the image test. The `.mcp.json` question is answered in
§1.2. Held by `tests/unit/worker/test_mcp_runner_config.py`.

**MCP-SC (part of #1433).** `swarm_dispatch` and each `swarm_workflow` step
pass `inputs.mcp_servers` and a top-level `mcp_credentials` through; the
read-only `swarm_mcp_servers` tool lists the workspace's registrations; no
tool registers a server or takes a store value
(`apps/swarm-mcp/swarm_mcp/server.py`; held by
`tests/unit/mcp/test_mcp_servers_tool.py`).

**MCP-UI (part of #1433).** Settings › MCP servers
(`apps/swarm-ui/src/McpServers.tsx`, route `/settings/mcp-servers`): every
member reads the list, a manager registers, edits, enables and disables, the
NAT addresses and "last used" are shown. Submit offers the enabled
registrations through `McpServerPicker` and a credential field per
`dispatch` server that is cleared after submit and never rendered back.

**MCP-ACCEPT (part of #1433), and MCP-ACCEPT-B.** The acceptance group
`scripts/acceptance/groups/mcp.sh` and its judge
`tests/acceptance/test_caller_mcp.py` run the issue's four checks against
the dev-only echo server (`tests/acceptance/mcp_echo_server/server.py`,
deployed by `terraform/infra/mcp_test_server.tf`, Q10). MCP-ACCEPT-B made
nothing public: the echo server's one invoker is the acceptance identity,
whose own Google ID token is the run's per-run credential. An operator
registers the server once in the smoke tenant; no registration, a disabled
one, or the switch off is a skip naming the reason, never a pass.

**MCP-DOCS (2026-10-18, part of #1433).** This section; the two amendments
the owner decided after approval, §1.2 (claude-code runs without
`--strict-mcp-config`) and §4.8 (the relay's `credential_in_request` check
does not apply `key_value_assignment` or `http_authorization`); and the
threat model in [security.md](../security.md#registered-mcp-servers-the-callers-credential-is-never-in-the-agents-reach).
`tests/unit/scripts/test_docs_describe_what_was_built.py` reads the switch,
the exempt rules, the attach purpose, the state edges and the runner's
answer from the code, and fails when a lane of §6 has no entry here.
