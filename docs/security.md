# Security

This platform runs **AI coding agents on repositories it did not write**, with
provider credentials, on infrastructure shared with other teams. The threat model
is not "someone attacks the API". It is "the code we are paid to run is hostile,
and the agent running it is helpful and credulous".

Every section below names a concrete attack, what stops it, and — where relevant
— what is *not* stopped.

---

## Authentication

Google ID token, verified per request, `hd` claim restricted to `saga.xyz`.

**There is no shared platform bearer token, anywhere.** This is the design, not
an omission. A shared secret carries no identity, so it cannot answer "whose
tenant is this task?", "which secrets may this caller reach?" or "whose budget
does this spend?". Multi-tenancy has to start at authentication or it is
decorative. A shared token also cannot be revoked for one person, does not
expire, and leaves no audit trail of where it was copied.

Tokens never appear in logs, exception messages or error responses. The only
loggable form is a non-reversible fingerprint (`tok:` + 12 hex chars of SHA-256)
for correlating requests.

Admin routes are gated on admin: membership in an `ADMIN_GROUPS` group, or an
address on `ADMIN_USERS`, the by-email escape hatch for a deployment whose
service account cannot read groups (`ApiSettings.admin_users`). Both are kept
separate from `TENANT_GROUPS` so an admin still has a normal tenant for their
own work. Either makes the caller a **full** admin: every `/v1/admin/*` route,
including disabling any tenant (`PUT /v1/admin/tenants/{id}/limits`) and
rewriting any tenant's workflow state (`POST /v1/admin/workflows/rollup`).
With `PLATFORM_ADMIN_REACH` on, a full admin may also select every registered
group workspace as its `admin`, member or not, and never anyone else's
personal workspace; every write there is audited before it runs
([Workspace roles and a platform admin's reach](#workspace-roles-and-a-platform-admins-reach)).
It is on in dev and off in prod: `var.platform_admin_reach` in
`terraform/environments/<env>/<env>.tfvars` renders it into swarm-api's
environment (lane ROLES-ON-DEV, owner decision 2026-10-19).

There is one narrower way onto the admin surface, and it is not admin.
A caller on **`ADMIN_POOL_USERS`** may call the routes in
`swarm_api.auth.POOL_ADMIN_ROUTES` and nothing else: today only
`PUT /v1/admin/limits/runner/{runner_profile}`, for any runner profile. Every
other admin route answers it 403, and `is_admin` stays false for it, so no
operator screen and no cross-tenant field on `/v1/stats` or `/v1/capacity`
opens. It exists for the verification gate (`swarm-verify`), which narrows
`runner:mock` for race-test and puts it back.

It is an **allow-list** of (method, route template), not a deny-list of the
dangerous routes, because a deny-list is silently wrong the day an admin route
is added: the new route would be open to the gate until somebody thought to
deny it. With the allow-list a new admin route is admin-only until it is
deliberately added, in a diff a reviewer sees. Two things would undo it
without touching that list: putting the gate on `ADMIN_USERS`, or in an
`ADMIN_GROUPS` group (a Google group can hold a service account as a member).
Either makes it a full admin. The decision, dated 2026-09-24, is recorded in
[`docs/audits/2026-09-22/race-test-needs-a-write.md`](audits/2026-09-22/race-test-needs-a-write.md).

### Handling an ID token on the operator side

The ID token is the *only* input from which tenant identity is derived, so
possession of one for the next hour is full impersonation of that person's
tenant: their provider keys, their GCS prefix, their budget. Two consequences
that are easy to get wrong, and that this repository used to get wrong in its own
documentation.

**Never in argv.** `curl -H "Authorization: Bearer $(gcloud auth
print-identity-token)"` puts the token in curl's command line, which is
world-readable through `/proc/<pid>/cmdline` on Linux, and writes it into shell
history. That is the same objection that justifies `create-secrets.sh` never
taking a key as an argument, so the docs may not teach the opposite. Use
`scripts/api.sh`, which builds the header with a shell builtin and hands it to
`curl -K -` on stdin:

```bash
./scripts/api.sh GET  /tasks/tsk_123 | jq .blocked_by
./scripts/api.sh POST /tasks '{"runner_profile":"mock","input":{}}'
```

`api_request` in `scripts/lib/common.sh` does the same, so every ops script is
covered.

**Mind the audience.** A bare `gcloud auth print-identity-token` mints a token
whose audience is gcloud's *public* OAuth client. Any Google principal with
gcloud can mint one, and one captured anywhere else is replayable against this
API — leaving only the `hd` claim and Cloud Run's `run.invoker` binding as
controls. That is a real weakening of "there is no shared platform bearer token":
it is not shared, but it is broadly scoped and freely mintable.

Set `API_AUDIENCE` to the API's own URL and mint through impersonation
(`SWARM_IMPERSONATE_SA`) for anything beyond a local poke; CI already supplies
`SWARM_ID_TOKEN` minted by WIF for an exact audience. `.env.example` documents
the trade-off next to the setting.

---

## Public endpoint exposure

Cloud Run services are **not** `allUsers`-invocable. IAM `roles/run.invoker` is
granted to named groups (`api_invokers` in the tfvars), so an unauthenticated
request is rejected by Google before any of our code runs. Application-level
token verification is the second layer, not the first.

Internal surfaces are narrower still:

* the scheduler accepts Pub/Sub **push** with an OIDC token, and additionally
  checks *which* service account called;
* the reconciler — a service whose job is to terminate other people's work —
  checks the caller's verified email against `RECONCILER_ALLOWED_INVOKERS`;
* the quota broker requires a platform identity for its sweep endpoint.

The VPC is default-deny ingress (priority 65534) with narrow allows for internal
east-west traffic, Google health-check ranges, and the private GKE control plane
reaching admission webhooks.

---

## Malicious repository content

An agent clones a repository and reads it. The repository may be hostile.

* Clones are **shallow and single-branch** — the working tree, not the history.
  Single-branch is the transfer only: the clone's `origin` is then set to track
  every branch (`gitops.TRACK_EVERY_BRANCH`), because with the clone's
  one-branch refspec `git fetch origin main` never moved an existing
  `origin/main`, and an agent merged a stale main (#453).
* The URL comes from an authenticated caller and is still not trusted: the scheme
  must be `https` or `ssh`. `file://` and `ext::` are refused, because
  `ext::sh -c ...` is a git URL that executes a command.
* A URL beginning with `-` is refused, because git would parse it as an option —
  `--upload-pack=...` is the classic remote-code-execution shape.
* Refs are validated against a conservative pattern or a commit SHA.
* Credentials never appear in argv; a token goes into a 0600 credential file
  inside the workspace, because argv is world-readable through `/proc`.

What is **not** prevented: the repository's content being malicious in ways an
agent might act on. That is the next section.

---

## Prompt injection

A repository can contain text addressed to the agent — in a README, a comment, a
test fixture — telling it to exfiltrate its key or push to another remote. There
is no reliable filter for this, so the platform does not pretend to have one.
It limits the blast radius instead:

* the agent holds exactly one provider key, belonging to its own tenant;
* the container has no credentials for anything else — no broad service account
  token, no push credentials it was not given;
* egress can be restricted to a known port set (`restrict_egress`, off by
  default until the full set is measured in dev, because turning it on blind
  breaks git over non-standard transports *mid-run* rather than at admission);
* everything the agent does is attributed to an agent, an attempt and a tenant, so
  the question "what did it touch?" has an answer.

Treat anything an agent produces as caller-influenced data. The worker already
does: it never evaluates runner output, only records it.

---

## Arbitrary command execution

The worker starts the runner as a child process with strict rules
(`agent_worker/procman.py`):

* **Never a shell.** `argv` is a list and `shell=False`, always. Nothing from an
  agent input is ever concatenated into a command line, so there is no quoting
  bug that can become command injection.
* **Own session.** `start_new_session=True`, signals delivered with `killpg`, so
  a runner that forks helpers — `claude`, `npm` and Playwright all do — cannot
  leave orphans holding CPU.
* **SIGTERM, then SIGKILL** after a grace period. A process that ignores SIGTERM
  is exactly the process that would otherwise run to the platform timeout.
* **Capped output, never a blocked pipe.** Reader threads keep draining after the
  cap and discard the excess; stopping the read would fill the 64 KB pipe buffer
  and deadlock the child — a hang indistinguishable from a slow agent.
* **No zombies.** Every exit path waits on the process and joins the readers,
  and `tini` is PID 1 in the image.

---

## Compromised runner profiles

`CONTRACT.md` invariant 10: **API callers pick a `runner_profile` by NAME.**
Never an image, a command, a resource spec or a backend parameter.

`swarm_api/validation.py` enforces it twice: the request schemas forbid unknown
fields (so a smuggled `image` is a 422, not a silently ignored key), and a
greppable `FORBIDDEN_CALLER_FIELDS` list produces a precise error rather than a
generic one.

This is what stops an authenticated caller from turning the swarm into arbitrary
compute. Adding a profile is a code change to a frozen module, reviewed in a
diff, not an API call.

---

## Credential theft

Covered in depth in [multi-tenancy.md](multi-tenancy.md#secrets). In short:

1. the secret name comes from the frozen `Tenant.secret_name()`, never from
   caller input;
2. the secret's own IAM policy names exactly one accessor — the tenant's service
   account — so a cross-tenant read is denied by Google, not by our code;
3. **no route a person can call reads a payload.** Exactly two routes serve
   credential material, both worker-only, attempt-key-signed,
   generation-fenced and once per attempt, each by an owner's decision:
   `POST /v1/attempts/{attempt_id}/secrets:release` (workspace secrets, Q2 of
   #1347) and `POST /v1/attempts/{attempt_id}/mcp:attach` (a registered MCP
   server's credential, Q9 of #1433, [below](#registered-mcp-servers-the-callers-credential-is-never-in-the-agents-reach)).
   `tests/unit/control_plane/test_no_route_serves_credentials.py` fails on a
   third. (Amended 2026-10-18: this line used to say there was no read path
   at all, which stopped being true when the release shipped.)
4. keys reach the agent through the child's environment only — never argv, never
   the workspace, never an artifact.

`create-secrets.sh` never takes a key as an argument, never echoes it, and writes
it through a 0600 file in a private temp directory removed on every exit path.
Secret material is deliberately outside Terraform, because a Terraform-managed
secret version puts the plaintext in a state file that several people can read.

---

## Secret leakage in logs

Two controls that hold, and one filter that helps:

**The worker's logger redaction holds.** Every secret value is registered with
the logger the moment it is read, and registered values are replaced with
`***REDACTED***` in both the message and the structured fields. This is exact
matching on a known value, not pattern matching, so it does not depend on what
the key looks like.

**The API having no read path holds.** No function returns payload bytes, so
there is nothing for a route to leak.

**`redact()` in `scripts/lib/common.sh` is best-effort and is not a boundary.**
It is a list of `sed` patterns, so it masks the shapes it knows — `sk-…`,
`ya29.…`, JWTs, `AIza…`, `ghp_`/`gho_`/`github_pat_`, `xox*`, `AKIA`/`ASIA`, PEM
headers, `Bearer <token>` bodies and `key|password|secret|token|credential|
authorization` assignments, including one written as JSON text with escaped
quotes at any depth (`PASSWORD=\"…\"`, `\"api_key\": \"…\"` since #221, and
`\\\"…\\\"`, a command that quoted its own quotes, since the PR #229 review;
the same rule as the API's) — and nothing else. Being sed over text, it cannot
decode a JSON line the way `/logs` does (`redaction.redact_lines`), so a list's
later elements under a credential's name, and a value opened by an escaped
quote past its first backslash, still reach the terminal. An opaque, high-entropy key from
a provider whose prefix is not on that list (Azure, Bedrock, a self-hosted
gateway) reaches the terminal in cleartext, and `create-secrets.sh` already has a
generic provider branch, so those providers are contemplated rather than
hypothetical. Treat it as the last of the three layers, never the first.

Where it is actually applied: `make logs` pipes all Cloud Logging output through
it; `scripts/status.sh` pipes every `gcloud` and `kubectl` subprocess through it
before parsing; `configure-kubectl.sh` and `push-images.sh` pipe the commands
whose output could carry one. It is *not* applied by every script in `scripts/`,
and this document used to claim it was.

`scripts/status.sh` prints no environment variables, no secret payloads and no
tokens by construction — it reads control-plane documents and resource listings
only. It is the command an operator runs while sharing a screen, so its
`--tenant` flag scopes the snapshot it collects, not merely what it prints.

---

## Registered MCP servers: the caller's credential is never in the agent's reach

An agent may name MCP servers its workspace registered, and a caller may send a
per-run credential for one ([design](design/registered-mcp-servers.md), part
of #1433). The whole feature is dark until `SWARM_MCP_SERVERS` is `on`: off, every
registration write, submission naming a server and attach is refused
`mcp_unavailable`. What holds once it is on:

* **A step names a server, never addresses one** (invariant 10). The URL,
  header, allowed tools and limits come from a registration only a workspace
  manager writes; a registration holds no credential, and swarm-api never
  calls a registered URL, so a tenant-typed URL is never an SSRF from a
  platform identity.
* **The per-run credential at rest** is one Secret Manager secret per
  submission and server, `swarm-tenant-<tenant>-mcpc-<16 hex>`, with Secret
  Manager's own TTL as the backstop and a 10-minute sweep that destroys it
  once its agent ends. Only swarm-api may read `-mcpc-`; the tenant's worker
  account holds no grant on it, so an agent that mints the pod's token reads
  nothing. The Firestore row that names the secret holds no value and is
  HMAC-attested, so a row an agent forges points at nothing.
* **The worker gets it once**, from
  `POST /v1/attempts/{attempt_id}/mcp:attach`, signed with the attempt key
  under its own purpose, `swarm-mcp-attach/v1`
  (`apps/swarm-api/swarm_api/routes/attempts_mcp.py::ATTACH_PURPOSE`), so a
  release, child, egress or live signature never verifies there. The agent
  shares the worker's uid and ID token but not its non-dumpable heap, so it
  can neither ask nor choose what is attached; a stale generation gets 409
  and nothing.
* **The agent talks to a loopback relay**
  (`apps/agent-worker/agent_worker/mcp_relay.py::Relay`), never the server.
  The CLI's config, which checkpoints carry, holds the relay's URL and a
  per-attempt local token that nothing outside the pod can reach and nothing
  after the attempt accepts; the credential stays in the worker's heap and
  is added only to the request the relay sends to the registered URL. The
  relay refuses a non-loopback peer, a wrong local token, a tool not allowed,
  oversize and over-rate calls, a non-public or non-443 address, and a
  redirect; it scrubs registered values from answers; and it logs the tool
  and status, never arguments, results or headers.
* **What the agent may not send out**: any value registered with the logger
  (provider keys, released secrets, any server's credential), refused by
  value; and any string that matches a `swarm_redaction` credential rule,
  refused by shape, except `key_value_assignment` and `http_authorization`,
  which fire on ordinary prose and are exempt from this one check by the
  owner's decision of 2026-10-18 (`SHAPE_EXEMPT`; they still mask logs). A
  repository's own `.mcp.json` is loaded too (claude-code runs without
  `--strict-mcp-config`), but no server it declares can reach the credential:
  only the relay holds it.
* **After the attempt** the relay closes and drops every reference; a Python
  string cannot be zeroed, so the bytes go when the worker process exits,
  which it does at the end of every attempt.

What it does **not** protect against: a malicious or compromised registered
server can answer with text that steers the agent, which is prompt injection
by another door (see [Prompt injection](#prompt-injection)), and anything the
agent sends to an allowed tool is the tenant's work going to the tenant's own
server, by its manager's choice. A private endpoint is out of reach by design:
the relay refuses private addresses and the pod policy drops them anyway.

---

## Workspace roles and a platform admin's reach

Each workspace's people are `viewer`, `member` or `admin`
([design](design/workspace-roles.md), part of #1518). Two properties carry
the security weight. The refusals `role_member_required` and
`role_admin_required` are switches that ship off (Q10), and
`PLATFORM_ADMIN_REACH` ships off. Since ROLES-ON-DEV (owner decision
2026-10-19) dev runs with `role_member_required` and the reach on, both from
`dev.tfvars`; `role_admin_required` stays off in dev until every group
workspace that manages secrets has a named admin, and prod has all three off.
What holds once they are on:

* **A role never admits anyone.** Membership is still Google's, checked per
  registered group with `checkTransitiveMembership` and
  `x-goog-user-project`, because `searchTransitiveGroups` 403s in this
  project. A grant to someone outside the group is stored and inert until
  they join. A role only narrows what a member may do.
* **The one widening is a workspace `admin`**, who manages that group
  workspace's secrets, MCP servers, tenant provider keys and settings. It is
  live whatever the switches say, because it only lets someone through. It
  exists only where an admin named one: the default role can never be
  `admin`, so a group workspace has no workspace admin until a platform
  admin names its first.
* **A grant cannot be forged, replayed or found by a tenant's worker**,
  which can create and update Firestore documents today
  ([below](#a-tenant-workers-firestore-access-today-phase-0-and-design-b)).
  Each grant is HMAC-signed with the settings key under its own domain,
  `swarm-workspace-role-grant/v1`, over the tenant, the person, the role, who
  granted it and when. So a forged grant does not verify, a copied older one
  loses to the newer, and one moved to another tenant fails. Grants are
  append-only under Firestore's random ids, which no route returns, so there
  is no document to overwrite by name. A flood of unsigned grants pushes a
  person past the 60 the reader scans and gives `role_unavailable`: a denial
  of service, never a wider role, and it is counted in
  `swarm_api_role_grant_invalid_total`. That residual closes when ISO-IAM
  removes the worker's Firestore write.
* **A platform admin's reach stops at group workspaces.** With
  `PLATFORM_ADMIN_REACH` on, `X-Swarm-Tenant` may select a registered group
  workspace for a full admin who is not in it. Every `u-` id is dropped from
  the reach
  (`apps/swarm-api/swarm_api/auth.py::Authenticator._reachable_groups`), so
  another person's personal workspace, which holds their own secrets and
  GitHub token, is 403 `tenant_not_member` exactly as before. A pool admin,
  the schedule tick, the sweepers and a listed service account get no reach.
  This widens who may enter a tenant (invariant 9), which is why it is a
  switch.
* **Nothing a platform admin does in a workspace they are not a member of
  goes unrecorded.** `apps/swarm-api/swarm_api/deps.py::current_auth` writes
  an `admin_audit` row before the route runs: `platform_admin_acted` for
  every write, `platform_admin_viewed` once per admin, workspace and UTC hour
  for reads. Both fail closed: if the row cannot be written the request is
  503 `platform_admin_audit_unavailable` and the route never runs. A row
  records the route template and its path parameters, never a body, so a
  secret value or a prompt cannot land in the audit. A platform admin who is
  a member narrowed below what an action needs gets the same rows. Every
  member of the workspace sees them on its people page. One gap is known and
  is recorded in the design's §8: a write through a
  `/v1/workspaces/{tenant_id}/…` path naming a group workspace the workspace
  job made writes no row.
* **No agent can change a role.** The plugin's `swarm_roles` tool is
  read-only. `sc roles set|clear|default` reads its typed confirmation from
  `/dev/tty`, never from arguments or stdin, and ignores `SWARM_ASSUME_YES`,
  so an agent driving a person's own session cannot raise anyone.

## Cloud metadata abuse

An agent inside a container can reach `169.254.169.254` and ask for the
workload's token. Mitigation is to make that token worth as little as possible:

* the workload runs as the **tenant's** service account, whose only grants are
  that tenant's secrets, that tenant's GCS prefix, and the Firestore database;
* it has no ability to create infrastructure, and no access to another
  tenant's secrets or objects. **It does have access to another tenant's
  control-plane documents**: the Firestore grant is
  `swarmTenantWorkerFirestore` (get, create, update), bound project-wide with no
  condition, so it reaches every document in the `swarm` database whose id it
  can name. What that lets a token minted here do, today and after #1457's
  fixes, is in
  [A tenant worker's Firestore access](#a-tenant-workers-firestore-access-today-phase-0-and-design-b);
* on GKE, Workload Identity means the node's service account is not what the pod
  gets;
* CI's deployer service account is separate, keyless (WIF), and never present in
  a worker.

What is **not** prevented: an agent minting a token for its own tenant's
identity. That is the identity it is supposed to have. The boundary is what that
identity can do, and today that includes rewriting the documents the control
plane acts on for every tenant (#1457). Design B moves the boundary from the
token to the attempt: a state write needs a proof from a key the agent does
not hold.

---

## Worker-to-worker attacks

* GKE: one namespace per tenant with a **default-deny NetworkPolicy**, so a pod
  cannot reach another tenant's pods at all.
* Cloud Run: executions have no inbound surface — nothing is listening.
* Workspaces are per attempt, created empty, and destroyed at the end. A resumed
  worker wipes the tree before restoring, so it cannot inherit another attempt's
  leftovers.
* Checkpoint archives are checked whole before a byte is extracted, then
  extracted through Python's `tarfile.data_filter`
  (`agent_worker/checkpoint.py`, `_safe_members` and `_restore_filter`),
  because a checkpoint contains a tenant's working tree and an agent can put a
  symlink to `/etc` in it. Two outcomes, on purpose:
  * **A symlink that leaves `work/` is skipped**, never created, and the rest
    restores. uv leaves `bin/python -> /usr/local/bin/python3.11` in every
    build environment, and refusing the archive over it failed every resume of
    the agent (#286). The skip is logged once per restore: at most 20 links
    named, each name and target through the worker's secret scrub, then a
    count. A symlink that comes to leave only once a later one is made
    (`y -> z/..`, then `z -> .`) is removed after extraction and named the same
    way.
  * **An archive inconsistent with itself is refused whole**, and the resume
    falls back as it does on any refused checkpoint: a member path that is
    absolute, holds `..` or appears twice; a member whose path passes through
    a symlink member (`d -> .`, `c -> d/..`, then `c/x`); a hard link whose
    target is not an earlier regular-file member, including one to a skipped
    symlink and one out of the archive root (`../private/secret.txt`). This
    platform's archiver follows no link and writes each name once, so none of
    these comes from an agent's ordinary tree. The hard-link rule is enforced
    here rather than left to `tarfile`, whose fallback for a link it cannot
    make extracts the link's target member in its place (the CVE-2025-4330
    class), so the restore's safety does not depend on the Python patch level.
* **Which checkpoint is restored is not chosen from the bucket** (#347). Every
  agent of a tenant can write anywhere under `tenants/<tenant>/`, so another
  step of the same tenant could put a checkpoint under an agent's prefix, and
  `.claude/` — settings, hooks — travels in every checkpoint because HOME is
  `work/`. An agent's first attempt therefore restores nothing, and a retry
  restores only `task.latest_checkpoint` when the attempt document that wrote
  it lists its id and its archive digest (`checkpoint_sha256`), and the
  pointer lies in the agent's own prefix. That stops an agent holding only the
  bucket, whether it adds a checkpoint or rewrites the recorded one. It does
  not stop an agent that also writes Firestore, which has no document-level
  IAM: the complete fix waits for signed step specs (#342).
  [checkpointing.md](checkpointing.md#3-restore) lists every check.

---

## Agent Kubernetes API access

Agents have no reason to talk to Kubernetes and are given no way to.

* The tenant KSA has **no RBAC rules** — no role, no rolebinding. A `kubectl`
  inside a pod gets 403 for everything.
* The pod spec sets `automountServiceAccountToken: false` where the workload does
  not need it, so the token is not even on disk.
* The reconciler is the only component that creates or deletes Jobs, and it runs
  as the platform's own service account outside the tenant namespaces.
* `scripts/configure-kubectl.sh` writes an **isolated** kubeconfig
  (`build/kubeconfig-<env>.yaml`, mode 0600) instead of merging into
  `~/.kube/config`, and refuses to fetch credentials for any deny-listed cluster.
  On the reference workstation the *active* context was another team's live
  cluster, with a production cluster in the same file — merging would have been
  one `kubectl delete` away from a very bad afternoon.
* That script has a `--merge` flag, and it undoes the control above. It exists
  because some tooling cannot be told to use a different kubeconfig. It is named
  here rather than left out of the doc, because an operator reading only this
  page would not know the control is one flag away from being off; it requires a
  typed confirmation, which `SWARM_ASSUME_YES` does not skip.

---

## Supply-chain image compromise

* Every `FROM` in `images/*/Dockerfile` carries a **sha256 digest**, so a rebuild
  in six months produces the same toolchain rather than whatever the tag points
  at that day.
* `uv` and Node arrive as pinned images, not `curl | sh`.
* Agent CLIs are pinned to exact versions (`@anthropic-ai/claude-code`,
  `@openai/codex`) and bumped deliberately.
* Multi-stage: compilers and the wheel cache stay in the builder.
* Non-root uid 10001; the pod security posture refuses root, so an image that
  ignored this would fail to schedule rather than run privileged.
* Nothing talks to a container runtime — no Docker socket is ever mounted. An
  agent that could reach one would escape every boundary above it.
* `trivy image` runs in `push-images.sh` **before** a digest is allowed a channel
  tag, and failing the scan refuses promotion.
* **Everything deploys by digest** (`image@sha256:...`), and nothing can deploy
  a tag. `terraform/infra` takes `image_refs` — one digest per image, written by
  `scripts/lib/image-refs.sh` from the promotion manifest — and refuses to plan
  while any image it deploys has none; a tag, another image's digest, or an
  image from another registry is refused by the variable's validation. That
  covers every Cloud Run service, every worker job and the verification job, and
  the deployed digest is recorded in state. There is no `gcloud run services
  update` path any more: it moved four services and no job, and the next apply
  reverted it.
* **The worker path deploys by digest too**, which it used not to.
  `apps/scheduler/scheduler/dispatch.py` built `…/<image>:<worker_image_tag>`,
  resolved at pull time, and the GKE Job template pulls `IfNotPresent` — so a
  node holding a cached layer for an older push of that tag ran the older code.
  The scheduler now receives `WORKER_IMAGE_REFS` (image name → digest, from the
  same manifest), refuses to start if any value is not digest-pinned, and
  refuses to dispatch a profile whose image has no digest rather than fall back
  to a tag. A Cloud Run job the dispatcher created earlier — for a tenant
  terraform does not know — is moved to the current digest before it next runs;
  terraform's own jobs are never rewritten by the dispatcher.
  `scripts/lib/deploy.sh --verify-only` checks, after every release, that every
  service, every terraform-managed job and the scheduler's map name the promoted
  digests. One exception, and only a narrow one (owner decision 2026-10-11): a
  job the owner pins in `terraform/bootstrap` (`OWNER_PINNED_JOBS` in
  `scripts/lib/common.sh`; today `swarm-workspace-apply`) is not moved by a
  merge, by design, so after a release that rebuilds its image it lags. On a
  digest an earlier release promoted -- one carrying the
  `<channel>-promoted-<tag>` mark `scripts/push-images.sh` adds and never moves
  -- that lag is a warning, "awaiting owner re-pin", with the command, and the
  summary says "1 owner action pending". On a digest no release promoted it
  still fails: that is an image nobody released running as a privileged
  account. The checkov image-reference checks (CKV_K8S_14/15/43) are still
  skipped over the rendered manifests, because those are templates rendered with
  a placeholder image; the reference that actually dispatches is the
  dispatcher's, and it is a digest.
* CI builds in Cloud Build with `--platform linux/amd64`, never on a developer
  machine.

---

## Unbounded agent creation

An authenticated caller with a loop is the most likely incident.

| Guard | Default |
|---|---|
| Per-principal token bucket | `requests_per_second` 20, burst 40 |
| `max_batch_size` | 100 |
| `max_input_bytes` | 256 KiB |
| `max_workflow_steps` | 50 |
| `tenant:<id>` pool | 20 active for a new tenant |
| `global` pool | the platform-wide ceiling |

The rate limiter is per instance and in-process on purpose: a Firestore-backed
limiter would add a read and a write to every request to protect against a burst
that Cloud Run's concurrency setting already bounds, and would make the limiter
itself the hot spot. With N instances the real ceiling is N x rate, which is the
honest trade and is reported on `/v1/stats`.

The deeper protection is invariant 1: a caller can queue a million agents and pay
for none of them. Queue depth is a Firestore bill, not a compute bill.

---

## Runaway cost

See [cost-control.md](cost-control.md). The short version: nothing costs money
before `LEASED`, every pool has a ceiling, per-tenant budgets park work at
`BUDGET_EXHAUSTED`, and `make pause-swarm` stops admission platform-wide in one
command without killing anything already running.

---

## Stale execution races

The scenario: the reconciler decides a worker is dead, a replacement is admitted,
and the original was merely slow. Two agents, one repository, one credential, and
the second to finish silently overwrites the first.

Two independent brakes:

**Fencing generations.** Every attempt carries a generation. The worker validates
it **before the workspace exists, before a secret is read, before the runner
starts**. A stale worker emits `generation_fenced` and exits — and does **not**
touch the lease, because the live lease belongs to the attempt that replaced it.

**Repair order.** `reconciler/repair.py` does exactly this, in this order:

```
1. invalidate the generation   (the old worker now stops by itself)
2. terminate the execution     (and confirm the backend accepted it)
3. release the slot            (only if 2 succeeded)
4. repair the task state
```

Step 1 before step 2 because bumping a generation is a single Firestore write
that does not depend on the backend being reachable. Step 3 after step 2 because
releasing first hands the slot to the scheduler while the old agent is still
running. An unconfirmed termination therefore does not release: one stuck slot
until the next pass is cheap, silent data loss in a tenant's repository is not.

`make race-test` exercises all three races: last free slot, cancel-during-dispatch
and stale generation.

---

## Duplicate retries

* Retries are control-plane decisions: a new attempt, a new generation, a
  checkpoint restore. Cloud Run Jobs are created with `max_retries = 0` and GKE
  Jobs with `backoffLimit: 0`, so the backend never silently re-runs something
  the lease accounting does not know about.
* Lease release is idempotent per lease, because the worker, the reconciler and
  the cancellation path can all race to release the same one, and a double
  release decrements pools below their true `active` — inflating capacity
  silently.
* `attempt_count` is incremented inside the admission transaction, so
  `max_attempts` cannot be exceeded by a race.

---

## Cross-tenant escape

The worst outcome, and the one every other section feeds into. The full table of
what would have to fail is in
[multi-tenancy.md](multi-tenancy.md#4-cross-tenant-escape-what-would-have-to-go-wrong).
Every path but one requires **two** independent mechanisms to fail together: an
IAM boundary plus a naming boundary, or a namespace boundary plus a policy
boundary. **The exception is control-plane data in Firestore**, where one
mechanism stands, and it is not a tenant boundary: any tenant's worker can
rewrite another tenant's settings, the shared capacity pools and the dispatch
switch, and its own tenant record so that its next attempt runs as another
tenant (#1457;
[A tenant worker's Firestore access](#a-tenant-workers-firestore-access-today-phase-0-and-design-b)).
Until that is closed, "two mechanisms" is not true of this platform.

The single most dangerous future change is a non-admin route that accepts a
`tenant_id` parameter. Tenant identity must always be *derived* from the verified
token, never *accepted* from the request.

---

## Operating in a shared project

`saga-agents-staging` holds another team's live GKE cluster (`agents-staging`),
their VPC, six of their buckets, nineteen of their service accounts and two of
their Cloud Run services (`oracle-api`, `oracle-web`) — counted from
`SHARED_DENY_LIST` after the 2026-10-21 read of the live project, which found
twelve the 2026-09-15 list did not name. Protections:

* `SHARED_DENY_LIST` in `scripts/lib/common.sh` — one list, read by `destroy.sh`,
  `purge-data.sh` and `configure-kubectl.sh`;
* `make destroy` runs `terraform plan -destroy -json` and **aborts** unless every
  resource marked for deletion carries `managed-by=swarm-terraform` (or is of a
  type that physically cannot carry a label — and unknown unlabelable types are
  treated as offenders, i.e. it fails closed);
* it refuses in prod without `--allow-prod`, ignores `SWARM_ASSUME_YES`, requires
  a typed confirmation, and re-verifies afterwards that every shared resource is
  still there;
* the guard has a self-test (`scripts/destroy.sh --self-test`, also run by
  `make test`) so it is verifiable without a live plan;
* the CI plan guards are **the same implementation**, not a restatement of it.
  `scripts/lib/plan-guard.sh` runs `scripts/lib/destroy-guard.jq` against the
  shared deny-list and the shared unlabelable-type list, and both
  `.github/workflows/terraform.yml` and `release.yml` call it. They used to
  re-implement the rule — one in awk, one in jq — and the copies drifted in both
  directions: the awk exempted only `google_project_iam*`, so removing a tenant
  (which deletes a `google_storage_bucket_iam_member`) was blocked from
  production; `make destroy`'s list omitted `google_firestore_document`, so a
  teardown of any environment with bootstrapped pool documents always aborted.
  Both regressions are now fixtures in `plan-guard.sh --self-test`;
* on an apply, the guard also asserts the forward half: everything the plan
  *creates* must carry `managed-by=swarm-terraform`. `make destroy` refuses to
  delete anything that does not, so an unlabelled resource merging into this
  shared project is one nobody can ever clean up;
* `purge-data.sh` refuses to operate on the `(default)` Firestore database.

**`.env` is executed, not parsed.** `load_env` sources it under `set -a`, so
every line in it runs as shell in every script here — including `destroy.sh` and
`purge-data.sh`, which read `PROJECT_ID`, `GKE_CLUSTER` and `ENVIRONMENT` from it
*before* any deny-list check happens. It is therefore treated as code: `load_env`
refuses to run if the file is not owned by the caller, or if it is group- or
world-writable. `SWARM_ENV_FILE` points the same machinery at another path and
carries the same trust requirement.

---

## A display name is a label, never an identity

A person may choose a display name on Settings › Profile
([user-profile.md §4.4](design/user-profile.md#44-the-display-name-q5)), and
other people see it on Admin › People, approvals, agent pages and workflow
pages. A chosen name is the one thing on those pages that someone else picked.
So it is treated as untrusted text that can only ever *add* to an email, never
stand in for one.

* **The email is always beside it.** A name is served only as a sibling of an
  email field the response already serves: `submitted_by_name`,
  `decided_by_name`, `approver_names` (index for index with a listed
  `approvers`) and a People row's `display_name`
  (`swarm_api.people.add_names`). No response carries a name without its
  email, and no route looks anyone up by name. Every check that decides what a
  person may do (who may approve, whose agents a submitter filter returns) keeps
  using the verified email.
* **Nothing else from the preferences document is served to anyone else.** A
  People row carries the name and no default workspace, mute or reminder: those
  are the person's own, and no admin route reads them (Q8).
* **A failed lookup degrades to emails.** `display_names.DisplayNames.names_for`
  reads a page's names in one `get_all`, and when that read fails every name is
  `null` and the page is still served: a name is never a reason a page fails.
* **The rules stop a name from passing for something it is not**
  (`display_names.check`): 1-64 characters of letters, marks, spaces and
  `.'-`, so no digit and no `@` (a name cannot embed an email), no brackets (no
  `[admin]` tag), no format character (no bidi override or zero-width joiner,
  which render differently from their bytes); one script, so a Cyrillic `а` in
  a Latin name is refused; no reserved word (`admin`, `owner`, `system`,
  `bot`...); and not another person's name, nor a name that contains another
  person's name of two or more words, compared after folding case and accents.
* **The uniqueness check is platform-wide, not per workspace**, because
  listing a group's members is the Cloud Identity call this project cannot
  make (`searchTransitiveGroups` 403s here). A name unique across the platform
  is unique in every workspace.
* **A worker cannot squat a name.** The claim documents that make names unique
  are addressed by an HMAC under `swarm-child-key`, which only swarm-api and the
  scheduler hold, and a claim whose id is not the HMAC of its own name is
  ignored. Every change of name writes an `admin_audit` entry in the save's
  transaction, so admins see each person's name history in the audit they
  already read.

---

## Reporting and review

* `make security` runs checkov over Terraform and trivy over the repo.
* `.github/workflows/security.yml` runs both on every PR, plus a weekly scheduled
  scan so a vulnerability disclosed after merge is still found.
* CI authenticates to GCP with Workload Identity Federation. **No downloadable
  service account keys exist**, and `deployer_roles` is explicitly enumerated —
  `roles/owner` is rejected by a variable validation — so what CI can do is
  reviewable in a diff.

## Firestore database scoping does not exist (verified 2026-09-16)

Earlier revisions of this repository claimed that an IAM Condition pinned every
swarm identity to the `swarm` Firestore database, keeping it out of `(default)`
and any other database in this shared project. **That claim was false**, and it
was proven false on a live deployment.

Firestore does not evaluate IAM Conditions on the **data plane**. Conditions
govern administrative operations — creating a database, managing indexes,
backups — and nothing else. With the condition attached, all four control-plane
services were *denied* document access and reported:

    {"status":"not-ready","detail":"firestore unavailable: PermissionDenied"}

Firestore Security Rules are the documented alternative for data-plane
conditions, and they do not apply here either: **server SDKs using admin
credentials bypass Security Rules entirely**, and every component in this
platform is a server SDK.

**Current position, stated plainly.** The condition defaults off. Every swarm
identity — the four services and every per-tenant worker — can read and write
**any Firestore database in this project**. Today `swarm` is the only database,
so nothing else is exposed.

**If you create a second Firestore database in this project, these identities
can read and write it.** There is no IAM mechanism that prevents that.

What still holds:

* Tenant workers hold the narrowed `swarmTenantWorkerFirestore` role, which
  omits `datastore.entities.delete` and `datastore.entities.list`. A hostile
  worker can neither enumerate nor delete another tenant's documents; it can
  only reach documents whose ids it can name. Agent, attempt and lease ids are
  `<prefix>_<20 hex>`, but `tenants/<id>`, `pools/*`, `workspace_settings/<id>`
  and `control/dispatch` are names anyone can compute, and the role **updates**
  them: see the next section.
* Application-level tenant scoping is enforced on every read and write the
  API serves, and on every read and write the worker makes through its own
  code (`apps/agent-worker/agent_worker/control.py::ControlPlane._assert_tenant`).
  The worker's check is a convenience, not a boundary: the agent shares the
  worker's uid and can mint its token, and a direct Firestore call never passes
  through it.

**The real fix** is to move Firestore into a dedicated project, where
project-level IAM *is* the database boundary. That was not done here because it
requires project-creation and billing-link rights that were unavailable.

A dedicated project would keep the swarm identities out of other databases. It
would not keep one tenant's worker out of another tenant's documents in this
one; the next section is about that.

## A tenant worker's Firestore access: today, phase 0 and design B

Issue #1457, S0. Read from the source on 2026-10-17 at `766b9af`; not exercised
against the live database. The design that closes it, approved by the owner on
2026-10-17, is [docs/design/tenant-settings-integrity.md](design/tenant-settings-integrity.md),
whose §1.3 inventories all 30 documents. The per-row table, with the reader of
each forged value, is in
[multi-tenancy.md](multi-tenancy.md#the-firestore-row-in-full).

**Today.** Every tenant worker's service account holds the custom role
`swarmTenantWorkerFirestore`: `datastore.entities.get`, `create` and `update`
(`terraform/bootstrap/platform_roles.tf` (`worker_firestore = {`)). It is bound
project-wide, once per tenant, with no condition
(`terraform/modules/tenancy/main.tf`
(`resource "google_project_iam_member" "worker_firestore" {`)), and
`scripts/register-tenant.sh` grants the same role. No IAM condition can narrow
it to a tenant (the section above). The agent can mint that account's token
([Cloud metadata abuse](#cloud-metadata-abuse)), so the agent holds this access,
not only the worker. With it, a worker in tenant A can:

* **run as tenant B.** Rewrite `tenants/A.service_account` to B's worker
  account and A's next Cloud Run attempt runs as it: the dispatcher refuses
  only an empty value
  (`apps/scheduler/scheduler/dispatch.py::assert_tenant_identity`) and puts the
  stored one on the Job
  (`apps/scheduler/scheduler/dispatch.py::CloudRunJobDispatcher._build_job`).
  Rewrite `tenants/A.namespace` to `swarm-tenant-B` and A's next GKE pod runs
  in B's namespace as B's KSA, because a stored namespace is kept whenever it
  starts with the prefix
  (`apps/scheduler/scheduler/dispatch.py::verified_namespace`). B's secrets
  and B's GCS prefix follow the identity.
* **over-admit the platform.** Set `pools/global.active` to 0, and admission
  (`apps/common/swarm_common/admission.py::evaluate_capacity`) counts nothing in
  flight. Create a `leases/` document naming any pools and release it, and
  `apps/common/swarm_common/admission.py::release_lease_in_transaction`
  decrements them. The worker writes `active` and its lease legitimately on
  every release, so no signature could separate the forged write.
* **stop every tenant.** Set `control/dispatch.dispatch_paused`
  (`apps/scheduler/scheduler/store.py::SchedulerStore.dispatch_paused`), or
  disable `pools/global`.
* **change what the control plane does for B.** `workspace_settings/B` decides
  B's judgment backend (`apps/scheduler/scheduler/judgment.py::read_backend`,
  `apps/scheduler/scheduler/dispatch.py::read_judge_backend`,
  `apps/swarm-api/swarm_api/typesafe_ticket.py::TypesafeTicketService.mint`), its
  merge mode and its code-intelligence delivery.
* **read B's records.** `tenants/<B>`, `workspace_settings/<B>`, `quota/*:<B>`,
  `accounts/*`, and any agent whose id leaks.

**Phase 0, once its lanes merge (not built on 2026-10-17).** The dispatcher
and the reconciler derive each tenant's service account
(`apps/common/swarm_common/identity.py::worker_service_account_id`), namespace
and GCS prefix from the tenant id instead of reading them, which closes "run
as tenant B" for good. swarm-api MACs the `workspace_settings` fields the
control plane acts on with a key no worker holds, and every reader treats a
missing or bad MAC as unset, which for the judgment backend is `internal-jev`
and fails closed: that closes #1457's instance. **Phase 0 leaves the rest
open**: over-admission, pausing every tenant, B's pools, tenant record limits,
quota and accounts, and every read. A MAC cannot close the pool and lease rows
at all, because the worker writes them legitimately.

**Design B.** The worker loses direct Firestore access and reaches its state
only through worker-only swarm-api routes,
`/v1/attempts/{attempt_id}/state/...`. Each call needs the caller's ID token,
which proves the tenant, and an Ed25519 proof from the attempt key, which
proves the attempt; each transaction is fenced and permits only the
transitions a worker makes today. Reads move too, then the binding is removed.
That closes every row, reads included. The cutover is a per-tenant switch whose
default is today's behaviour: swarm-api runs at least two instances first, dev's
tenants are switched and `make smoke concurrency-test race-test` runs against
dev, then every tenant is switched, and only then does the tenancy module stop
binding the role (its switch defaults on until the last tenant is switched) and
`register-tenant.sh` stop granting it. An acceptance probe from a tenant-A pod
expects `PermissionDenied` on `workspace_settings/<B>`, `tenants/<B>` and a
`leases/` create, and is red until then.

**What remains after design B.** The agent can still mint its worker's token;
without the attempt key it gets `403`. A worker whose heap is not protected
runs anyway with its key flagged `unprotected` (owner decision, 2026-10-17), so
its agent could act as its own attempt, within its own tenant and never for
another. The four platform services keep database-wide access, and every swarm
identity can reach any second database created in this project (the section
above).
