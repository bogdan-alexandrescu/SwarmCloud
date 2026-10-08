# Git tokens: a registry with three scopes, and what each token can do

**Status: PROPOSED 2026-10-04, design and mock-ups only (functionality wave 4,
lane RI0b, drawn 2026-10-05).** Nothing described here is built. The owner
asked on 2026-10-04 for "a way to visualize the permissions the git token has
on a given repo or set of repos. We might need to manage different git tokens
per repo and per user or tenant." This is a sibling of
[repo-index.md](repo-index.md) rather than a section of it, because a forge
token is used by every task that clones or publishes, registered repository
or not, and because the rules that keep it secret deserve one place to be
read in full. The screens are drawn in
[web-ui/mockups/repositories.html](web-ui/mockups/repositories.html#tokens)
(the Git tokens page and the permission matrix, two and three variants).

What the design settles, in one line each:

* **Three scopes: tenant default, per repository, per user.** Each is a
  Secret Manager secret named through the frozen `Tenant.secret_name`, under
  the tenant's own name, read only by the tenant's own identities (§1, §2).
* **The resolution order is the owner's to pick**; two orders are written out
  with what each costs. When nothing fits, the task parks
  `CREDENTIAL_MISSING`, at no cost, as it does today (§3).
* **What a token can do is computed server-side and served as labels**:
  per token and repository, eight capabilities as `ok` / `missing` /
  `unknown`, each with its reason, plus "expires in N days" and "last
  verified". The token itself is never returned, logged or rendered (§5, §6).
* **Phase 1 needs no frozen-contract change**: the registry, the probe and the
  views work for the tenant default token as it is today. Using a repository
  or user token in a task needs one request (E), written in §8.

---

## 0. Today

One forge token per tenant. It lives in Secret Manager as
`swarm-tenant-<tenant>-git` — the name `Tenant.secret_name("git")` produces
(`apps/common/swarm_common/models.py::Tenant.secret_name`) — and is stored only with
`scripts/create-secrets.sh --stdin` (`scripts/create-secrets.sh`, whose header
says why the forge token is never typed on a command line). The worker reads
it at runtime and writes it to a credentials file the agent is never told
about, for the forge host the URL names and no other
(`apps/agent-worker/agent_worker/gitops.py`, its module docstring). swarm-api
reads the same secret through `SecretManagerForgeTokens`
(`apps/swarm-api/swarm_api/forge.py::SecretManagerForgeTokens`) for the issue preview and the
open-work read, as the one platform identity bound to it
(`var.forge_readers` on the accessor binding,
`terraform/modules/secret_manager/main.tf` (`resource "google_secret_manager_secret_iam_binding" "accessor"`)).

The owner rule of 2026-09-25 (CLAUDE.md, "Writing Terraform") binds every
section below: **a forge token is never written to the repository, a tfvars
file, a Job environment, a log, an API response or the UI.** Nothing in this
design relaxes it; §4.2 names the one place a design choice brushes against
it, so the owner decides it rather than discovers it.

**The merge step.** [merge-step.md §2.1](merge-step.md) designs the merge
credential as a GitHub App key in `swarm-tenant-<tenant>-git-merge`, read only
by `swarm-<tenant>-merge` (`terraform/modules/tenancy/main.tf`, the
`sole_accessor` comment), so that a merge is attributed to a bot identity no
agent can read. The lane brief for RI0b says the merge step (#295, lane M1a,
in flight) "uses that same token", the tenant's `-git`. Those two statements
disagree, and this lane cannot read #295 to settle which is current. The
registry below is written so either holds: `merge` is a capability every
token is probed for, and §3.3 says how the merge step picks under each.

---

## 1. The registry: three scopes

A **git token record** describes one secret. It never holds a value.

| scope | covers | who may register or rotate it | why it exists |
|---|---|---|---|
| **tenant default** | every repository the tenant works in that no narrower record covers | an admin of the tenant | today's `-git`, unchanged; a tenant with only this keeps working exactly as now |
| **per repository** | one registered repository (`repo_id`, [repo-index.md §1](repo-index.md)) | an admin of the tenant | a repository in another organisation, or one the owner wants reached by a narrower fine-grained PAT than the tenant's |
| **per user** | the repositories that user's own dispatches touch, optionally narrowed to a list of `repo_id`s | that user, for themself only; an admin may revoke it but never register one for someone else | so a pull request, a comment or a merge is attributed to the person who dispatched it, not to a shared account |

The record, in Firestore, kept by its own swarm-api module like
`repositories` (not through `store.py`, for the reason `issue_runs` gives):

    git_tokens/{token_id}
      tenant_id, scope (tenant|repository|user), repo_ids (repository: one;
      user: [] for all, or a list), user (the email; user scope only),
      provider_suffix, secret_name, forge (github), kind (fine_grained_pat |
      classic_pat | app_installation), forge_login, last4, expires_at,
      registered_by, registered_at, rotated_at, verified_at, state
      (active|expired|revoked|unverified)

`token_id` is `tok_` + 16 hex of sha256(`tenant_id` + scope + key), so
registering the same slot twice is idempotent. `last4` is the last four
characters, recorded once at registration or rotation from the value in
memory; it is the only part of a value that is ever stored, and it is stored
because the owner asked to see it. `forge_login` is the account the token
acts as (`GET /user` — or the App's slug for an App installation), which is
what "owner" means on the Git tokens page.

---

## 2. Secret Manager naming, and who can read each secret

Every name goes through the frozen `Tenant.secret_name(provider)`, so the
contract does not change for naming. `create-secrets.sh` accepts a provider of
lowercase letters, digits and hyphens only, which decides the suffixes:

| scope | provider suffix | secret |
|---|---|---|
| tenant default | `git` | `swarm-tenant-<tenant>-git` (today's, unchanged) |
| per repository | `git-r-<16 hex of repo_id>` | `swarm-tenant-<tenant>-git-r-<hex>` |
| per user | `git-u-<16 hex of sha256(lower-cased email)>` | `swarm-tenant-<tenant>-git-u-<hex>` |

The user's email is hashed, not spelled, because a secret's name appears in
audit logs, in `create-secrets.sh --list` and in the tenancy module's plan;
the record maps the hash back for the console.

**Readers (invariant 9: own GSA, own secrets, own GCS prefix, own
namespace).** Each secret's accessor binding is the tenant's own worker
service account and swarm-api (the probe, §5), exactly the two members
`-git` has today, set in the same authoritative per-secret binding — never a
project-level grant, which would reach every tenant's every key. A secret
named under tenant `eng` is never bound to `ops`'s account, so a repository
token or a user token is as isolated between tenants as `-git` is now.

**Within a tenant, a user token is readable by the tenant's worker account,
which every task of that tenant runs as.** The resolver (§3) decides that
Alice's token is used only for Alice's dispatches; IAM does not. The worker
keeps the credential out of the agent's sight (`gitops.py`: the agent is
never told the path and the file is gone before it starts), which is the same
protection `-git` has, and no stronger. Three options, for the owner:

* **(U1) Accept it.** A user token is a tenant secret attributed to a person;
  a tenant's members already share the tenant token. Cheapest.
* **(U2) User tokens live only in the user's personal tenant** (`u-<user>`,
  whose worker account is that user's alone). Strong isolation, but Alice's
  token then serves only dispatches she makes in her personal tenant, not in
  `eng`.
* **(U3) A per-user reader identity** (a service account per registered user,
  the only accessor of `-git-u-<hex>`, used by a credential step the way
  `swarm-<tenant>-merge` is used for `-git-merge`). Strongest; one more
  identity per user and a step that runs no agent.

**Creating the secret.** Today every tenant secret is declared by Terraform
(`terraform/modules/secret_manager/`) from the tenancy module's provider list,
with the value added out of band. Two ways to create the narrower ones:

* **(S1) Terraform declares each slot**: the tenancy tfvars gain a
  `git_tokens` list of `{scope, repo, user}` keys (no values, ever), each
  becoming a secret with the labels the module already writes and the same
  accessor binding. Reviewable, and the apply is what grants the reader.
  Registering a token needs a Terraform change first.
* **(S2) swarm-api creates the secret** on registration, under a custom role
  restricted by an IAM condition to names starting `swarm-tenant-` and
  containing `-git-r-` or `-git-u-`, and sets the accessor binding to that
  tenant's worker account. Self-service, but swarm-api gains the right to
  set IAM on tenant secrets, the most privileged grant in this design; it
  gets a security review if picked.

The recommendation is S1 for phase 1: the registry's value does not depend on
self-service, and S1 adds no platform privilege.

---

## 3. Resolution: which token a task uses

### 3.1 The order — two options, for the owner

When a task needs a token for a repository, swarm-api resolves it at
submission (§8 says why there, not in the worker), from the records of the
task's **own** tenant:

* **(R1) user > repo > tenant.** The dispatching user's token if they have
  one covering the repository, else the repository's, else the tenant
  default. Everything the task does on the forge — clone, push, open the
  pull request, comment — is the user. Simplest to explain; a user whose
  personal token cannot push to a repository the tenant token can is
  refused where R2 would have succeeded (the probe shows it in advance).
* **(R2) repo > tenant, with the user token for attribution only.** Clone and
  push use the repository token, else the tenant default; the user token, if
  present, is used for exactly the actions whose author is visible on the
  pull request: opening it, commenting, and (if the owner allows, §3.3)
  merging. Least privilege on the user's token — it needs only
  `pull_requests: write` — and a user without one still dispatches.

In both, the dispatching user is the task's `submitted_by`
(`apps/common/swarm_common/models.py::Task.submitted_by`), the verified email from the ID
token, never a field a caller sets (invariant 10: a caller picks a runner
profile and a repository by name, never a credential).

### 3.2 When nothing fits

If no record covers the repository, or the one that does is `expired` or
`revoked`, the task parks **`CREDENTIAL_MISSING`** — the existing state, at
no cost (invariant 1: a parked task creates no infrastructure demand) — with
an event detail naming the scopes tried and why each was passed over
(`user: none registered`, `repo: expired 2026-09-30`, `tenant: none`), by
name and never by value. Registering or rotating a token that covers it
moves the task back to `QUEUED`, as registering any missing credential does.
A token whose last probe says a capability the step needs is `missing`
(push, for a publishing step) parks the same way with that reason; one whose
probe says `unknown` is used, because refusing on "unknown" would refuse
every fine-grained PAT (§5.2).

### 3.3 The merge step and the push steps

* **Push steps** (every step that publishes a branch or opens a pull request)
  use the resolved token for its repository. The worker writes it into the
  credentials file exactly as it writes `-git` today.
* **The merge step** keeps the identity merge-step.md §2.1 gives it when
  `swarm-tenant-<tenant>-git-merge` is registered: the merge App, never a
  person's token, because a rule "only the merge App may merge to `main`"
  (merge-step.md Q3) cannot be written about a person's PAT. If the brief is
  right that M1a uses the tenant's `-git`, the merge step resolves like a
  push step under R2's repo > tenant order, never using a user token. An
  option the owner may take later: "attributed merges" — the user token
  under R1 — which gives up Q3's rule for that repository, and the console
  says so on the repository's Settings.

### 3.4 Rotation and expiry

* **Rotation** adds a new secret version (`create-secrets.sh --stdin
  --disable-previous`, or §4.2's register box if the owner allows it); the
  record's `last4` and `rotated_at` change and the probe runs at once. A
  worker reads `latest` when it starts, so a task in flight keeps the version
  it read, and the next attempt gets the new one.
* **Expiry** is read from the forge on every probe: GitHub answers a request
  made with an expiring PAT with the `github-authentication-token-expiration`
  header. The console shows "expires in N days", in the warning colour from
  14 days, and red from 3. An expired token's record becomes `expired`: the
  resolver skips it (§3.2) and the tenant's admins get a notification. A
  classic PAT with no expiry shows "no expiry" — itself a warning, since
  GitHub's guidance is that every PAT expires. An App installation token is
  minted per use and expires in an hour by design; its record has no expiry,
  the App key behind it has none either, and the page says that rather than
  a date.

---

## 4. Registering a token

### 4.1 By the command line (today's path, unchanged)

    scripts/create-secrets.sh --tenant eng --provider git-r-<hex> --stdin

The console's Register dialog then only creates the record and runs the
probe; it never sees the value. This is the path that keeps the owner rule of
2026-09-25 to the letter, and the Git tokens page's variant B draws it.

### 4.2 By the console's paste box (only if the owner allows it)

The owner asked for "register/rotate a token" in the console. A paste box
means the value crosses swarm-api once: the box is a `type="password"` field
with `autocomplete="off"`, sent once in the body of `PUT
/v1/git-tokens/{token_id}/value`, which swarm-api writes as a new secret
version (it holds `secretVersionAdder`, which cannot read a version back, the
pattern `terraform/modules/secret_manager/main.tf` already uses for humans
and CI), probes, and drops. The response carries `last4`, the probe result
and nothing else; the box is cleared and never re-displays the value. The
request body is excluded from every log and trace, and the value is
registered with the redaction filter before anything else is done with it.

That is a new path for a forge token — through an API, not `--stdin` — so it
is the owner's decision, not this lane's. The mock-ups draw both.

---

## 5. Permissions: what a token can actually do

### 5.1 The capabilities SwarmCloud needs

Per token × repository, computed by swarm-api from the forge, served as one
of `ok` / `missing` / `unknown` with a one-line reason:

| capability | what needs it | how it is read (non-mutating) |
|---|---|---|
| **clone / read** | every task | `GET /repos/{o}/{r}`: 200 is `ok`, 404 is `missing` ("not visible to this token") |
| **push branches** | publishing steps | the repository's `permissions.push`, and for a classic PAT the `repo` (or, public only, `public_repo`) scope; confirmed by asking for the push service advertisement (`info/refs?service=git-receive-pack`), which GitHub answers only for a credential that may push and which writes nothing |
| **open pull requests** | publishing steps | `pull_requests: write` in an App token's permissions; for a classic PAT, `repo` scope plus push; for a fine-grained PAT, `unknown` (§5.2) |
| **read checks** | the merge step, the selected-tests gate | `GET /repos/{o}/{r}/commits/{default_branch}/check-runs?per_page=1`: 200 `ok`, 403 `missing` |
| **merge** | the merge step | push, plus the branch rules (`GET /repos/{o}/{r}/rules/branches/{base}`) not restricting merges to other actors; `missing` when a rule names a bypass list the token's actor is not on |
| **close issues** | issue runs that close their issue | `issues: write` in an App token's permissions; classic `repo` with triage or above; fine-grained `unknown` |
| **read issues** | issue runs | `GET /repos/{o}/{r}/issues?per_page=1`: 200 `ok`; 410 `missing` with "issues are disabled on this repository" |
| **workflow_dispatch** | the selected-tests gate's CI mode ([repo-index.md §4.4](repo-index.md)) | `actions: write` in an App token's permissions; classic `repo` scope; fine-grained `unknown` unless `GET /repos/{o}/{r}/actions/workflows` is also 403, which makes it `missing` |

For a **classic PAT**, the `X-OAuth-Scopes` header on any response lists the
token's scopes; with the repository's `permissions` object (the actor's role:
`pull`, `triage`, `push`, `maintain`, `admin`) that decides most rows. For an
**App installation token**, the mint response's `permissions` object is
exactly what was granted, so every row is decidable. For a **fine-grained
PAT**, see §5.2. Every probe also records the expiry header (§3.4) and the
rate-limit headers, so the page can say a token is near its limit.

### 5.2 Honest unknowns

GitHub does not expose a fine-grained PAT's granted permissions through the
API. The repository's `permissions` object for such a token describes its
owner's role on the repository, not what the token was granted, so reading
it as the grant would show `ok` for a token that will be refused. Rows that
can be read by a harmless request (clone, read checks, read issues, push via
the receive-pack advertisement) are measured; the rest are `unknown` with
the reason "fine-grained grants are not readable; the role allows it", never
`ok`. A step that then fails on the forge records the 403, and the next probe
turns that row `missing` with the step as its evidence. The push probe's
behaviour is to be confirmed against a test repository by the lane that
builds it (GT2); until then a push row it decides carries "measured by
receive-pack advertisement" in its reason.

### 5.3 How often it is re-verified, and what it costs

A token is re-verified on registration and on rotation; then daily per token
× repository it covers, in the same scheduler pass as the repository poll
([repo-index.md §3.3](repo-index.md)); on demand from the console's "Verify
now"; and on the next pass after a worker reports a 401 or 403 from the forge
for it. A probe is at most eight GETs; forty repositories × three tokens is
under a thousand requests a day, against GitHub's 5,000 an hour per token.
"last verified" is the time of the last complete probe; a probe that could
not run (the forge unreachable) leaves the previous answer with "last
verified 3 days ago — the last attempt failed: …" rather than turning rows
`unknown`.

### 5.4 The probe never logs or returns the secret

The probe reads the secret into memory, registers it as a literal with the
redaction filter before the first request, sends it only to the forge host
the record names (the rule `forge.may_receive_forge_token` states in
`apps/agent-worker/agent_worker/forge.py`; it lives only in the worker
today, so GT2 adds or shares a swarm-api equivalent of that host rule rather
than assuming one exists), and returns booleans, labels and reasons. A
forge error message is passed through `redact` before it becomes a reason. No
route returns any part of a value except `last4`. In the console, no
`aria-label`, `title`, tooltip, `data-` attribute or copy button carries a
value: the only copy button on these pages copies the **secret's name**,
which is not a credential, and its label says so ("copy secret name").

---

## 6. The views

Drawn in [web-ui/mockups/repositories.html](web-ui/mockups/repositories.html):
the **Git tokens** page (section 12, variants A and B: by scope with owner,
forge, expiry, last verified and repositories covered; register and rotate),
the **permission matrix** (section 13, variants A: a tokens × repositories ×
capability grid; B: per-repository cards naming the token each action would
use and what it lacks; C: a "can SwarmCloud do X in repo Y?" checker), and
the **resolved token and its capability row** on a repository's Settings
(section 11).

---

## 7. API sketch

Tenant-scoped like `/v1/repositories`: another tenant's `token_id` is a 404
indistinguishable from a missing one.

| route | does |
|---|---|
| `GET /v1/git-tokens` | the tenant's records (no values), with their probe summaries |
| `POST /v1/git-tokens` | register a slot: `{"scope": "repository", "repo_id": "..."}`; the user scope takes no user field, it is always the caller |
| `PUT /v1/git-tokens/{token_id}/value` | only if §4.2 is allowed: the value, once; answers `last4` and the probe |
| `POST /v1/git-tokens/{token_id}:verify` | re-probe now |
| `DELETE /v1/git-tokens/{token_id}` | revoke: the record becomes `revoked` and the secret's versions are disabled; typed confirmation in the console |
| `GET /v1/git-tokens/permissions?repo_id=` | the matrix: per token × repository, the eight capabilities with state and reason, `expires_at`, `verified_at` |
| `GET /v1/repositories/{repo_id}/token?user=me` | which token resolves for this repository (under the picked order, §3.1) and its capability row |
| `POST /v1/git-tokens:check` | `{"repo_id": "...", "capability": "merge"}` → the answer, the token that would be used, and why |

Probe results are kept per pair in `git_token_checks/{token_id}_{repo_id}`
(capability → state and reason, `verified_at`, `expires_at`,
`rate_remaining`).

---

## 8. Frozen contract: requests, not changes

**Phase 1 needs none.** The registry, the probe and every view work on
swarm-api documents; the tenant default token is the existing `-git`.

**Request (E), for using a repository or user token in a task**, to be filed
in [contract-change-requests.md](contract-change-requests.md) when the owner
picks this design:

* *What is true today:* the worker reads `tenant.secret_name("git")`, and the
  admission check that parks `CREDENTIAL_MISSING` reads `Tenant.credentials`
  (`apps/common/swarm_common/models.py::Tenant.credentials`), which lists providers; nothing
  on a `Task` says which forge secret it uses.
* *The requested change:* an optional `forge_credential` on `Task`, set only
  by swarm-api at submission from the resolution of §3 (a provider suffix,
  `git`, `git-r-<hex>` or `git-u-<hex>`), never accepted from a caller
  (invariant 10); the worker reads `tenant.secret_name(task.forge_credential
  or "git")`, and admission treats the named suffix as the provider to check.
* *What it would break if accepted:* nothing existing; absent means `git`.
* *If it is declined:* the registry and the permission views still ship, for
  the tenant default only; per-repository and per-user tokens are visible in
  the design and not usable.

Resolving in the worker instead (it reads the registry at start) would need
no `Task` field, but admission would still park a tenant with only a
repository token as `CREDENTIAL_MISSING`, because the check that parks reads
`Tenant.credentials`. Resolving at submission is what keeps the park
truthful.

---

## 9. Build plan

| lane | builds | territory | needs |
|---|---|---|---|
| GT1 | the `git_tokens` module and routes, the tenant-default record created from the existing `-git`, unit tests | new `gittokens.py`, `routes/gittokens.py` in `apps/swarm-api/swarm_api/` | — |
| GT2 | the probe, its eight capability readers, the daily re-verification in the poll pass, redaction tests | `gittokens.py`, the poll in `repoindex.py` | GT1, RI4 |
| GT3 | the Git tokens page, the permission view from the owner's pick, the repository Settings row | `apps/swarm-ui/` | GT1, GT2 |
| GT4 | per-repository and per-user secrets under the picked S and U options | `terraform/modules/tenancy/`, `terraform/modules/secret_manager/` | owner's picks |
| GT5 | resolution at submission and the worker reading `forge_credential` | `apps/swarm-api/`, `apps/agent-worker/` | request E |

GT2 and GT4 get the one review (credentials, tenant isolation, redaction).

**For the owner:** R1 or R2 (§3.1); U1, U2 or U3 (§2); S1 or S2 (§2);
whether the console may accept a token value at all (§4.2); whether
"attributed merges" are ever wanted (§3.3); which variants to build.
