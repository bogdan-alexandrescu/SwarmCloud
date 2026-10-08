# Onboarding: connect GitHub as yourself, enable orgs, choose repositories

**Status: PROPOSED 2026-10-07, design and mock-ups only (functionality wave
15, lane C4T, for #780).** Nothing described here is built. The owner asked
on 2026-10-07 for "an onboarding experience that runs through the console AND
through the sc plugin, and hand-holds the user through the whole setup":
setting up git access, enabling one or more GitHub orgs, and choosing the
repositories SwarmCloud may read and write, **acting as the user**. Token
custody is part of this design, not a later pass. The screens are drawn in
[web-ui/mockups/onboarding.html](web-ui/mockups/onboarding.html), two or
three variants each.

This is the third document about forge credentials, and it builds on the two
before it rather than replacing them: [git-tokens.md](git-tokens.md) (the
registry of token slots, the permission probe, resolution order R2) and
[repo-index.md](repo-index.md) (the registered repository). The merge step's
credential is in [merge-step.md](merge-step.md); frozen-contract requests are
filed in [contract-change-requests.md](contract-change-requests.md), and this
document only drafts one (§3.3). The checklist's other half, the person's own
workspace (tenant, identity, namespace) that a locked-down provisioner creates
during onboarding, before which no task or workflow starts, is designed in
[workspaces.md](workspaces.md) (#847).

What it settles, one line each:

* **Mechanism: a SwarmCloud GitHub App with user access tokens (B)**, a
  fine-grained PAT per org (A) as the fallback for an org whose admin will not
  install the App, and today's tenant token kept, unchanged, for what it does
  today (§1).
* **One resumable state machine, seven steps, served by one route and read by
  both the console and the plugin**; each step is "done" only when its probe
  says so, and each failure has a code and word-for-word recovery copy (§2).
* **The worker gets the submitting user's credential by name**, never a value
  in its environment; the clean way needs one frozen field, drafted in §3.3,
  and a way without it is written out too.
* **A build plan whose first phase is useful on its own**: the Register picker
  pages through every repository and accepts a typed `owner/repo` (§5).

The names below are placeholders. `example-user` is a person's own GitHub
account and `example-org` an organisation that enforces SAML SSO (in #780 these
are the owner's account and `sagaxyz`); the tenant is `eng`, the console
`swarm.example.com`.

---

## 0. Today, re-verified 2026-10-07 against `main`

Re-read for this design, not copied from the gap analysis:

* **One token does everything.** Registration reads with the tenant token
  (`apps/swarm-api/swarm_api/repositories.py::read_repository` calls
  `tokens.token_for`), the readable list does too
  (`apps/swarm-api/swarm_api/repositories.py::readable`, which answers
  `"token_scope": "tenant"` every time), and the worker clones and pushes with
  `swarm-tenant-<tenant>-git` only
  (`apps/agent-worker/agent_worker/secrets.py::resolve_git_token`, called from
  `apps/agent-worker/agent_worker/lifecycle.py::Worker._git_token`).
* **The API pages; the console does not.** `readable` serves `next_page` and
  `capped` and takes `page` 1 to `MAX_READABLE_PAGES`; the console asks for
  page 1 only, with no `page` parameter
  (`apps/swarm-ui/src/api.ts` (`export async function loadReadableRepositories(): Promise<Result<ReadableList>> {`)),
  and `normReadable` never reads `next_page`
  (`apps/swarm-ui/src/RepositoriesData.ts` (`export function normReadable(v: unknown): ReadableList {`)).
  The Register page has no typed `owner/repo` field, though
  `POST /v1/repositories` accepts one.
* **Token slots exist as records; nothing but the impact query resolves
  them.** `apps/swarm-api/swarm_api/gittokens.py::resolve_r2` is pure and
  correct; the three scopes are named by
  `apps/swarm-api/swarm_api/gittokens.py::provider_suffix` (`git`,
  `git-r-<hex>`, `git-u-<hex>`), and the kinds the registry knows are
  `apps/swarm-api/swarm_api/gittokens.py::TOKEN_KINDS` (no user access token
  from an App among them).
* **The worker's environment carries no credential** and no owner
  (`apps/scheduler/scheduler/dispatch.py::worker_env`). That is right and this
  design keeps it.
* **The browser hand-off already exists, for Claude accounts.** `sc account
  add` asks the API to begin a sign-in, opens the browser, and finishes with an
  exchange the API performs
  (`apps/swarm-mcp/swarm_mcp/sc.py::cmd_account_add`,
  `apps/swarm-api/swarm_api/routes/accounts.py::begin_sign_in`,
  `apps/swarm-api/swarm_api/routes/accounts.py::finish_sign_in`). So does the
  short-lived-token refresher pattern: a `-refresh` twin secret, and a writer
  that publishes the short-lived half into the base secret as a new version
  (`terraform/modules/secret_manager/main.tf` (`resource "google_secret_manager_secret" "refresh" {`)).
  Onboarding reuses both shapes instead of inventing a third.

Not measured, by brief: whether the `eng` token can see `example-org` at all.
The probe records no SSO header and no org list, so nothing stored can say.

---

## 1. The access mechanism

### 1.1 The three options

**(A) The user's personal access token, pasted through a stdin path.** Either
a classic PAT with `repo` (and `read:org`), SSO-authorised per org under
GitHub › Settings › Developer settings › Tokens (classic) › Configure SSO, or
a fine-grained PAT, which has exactly one resource owner, so one per org and
one for the user's own account. The value goes from the user's terminal to
Secret Manager through `scripts/create-secrets.sh --stdin`, as today; a
self-service user without Secret Manager rights needs a new
`uv run sc setup token --owner <org>` path that reads stdin and sends the value
once to swarm-api, which writes it as a secret version. That is a new route
that accepts a token value, which PICKS.md (`docs/web-ui/mockups/PICKS.md`,
Git tokens B) kept closed, so it is the owner's to open (decision D5).

**(B) A SwarmCloud GitHub App, with user access tokens.** SwarmCloud registers
one GitHub App. Each org (and the user's own account) installs it and chooses
"all repositories" or "only select repositories"; that installation is the
org-side allow-list. The user authorises the App once in the browser; GitHub
returns a **user access token** (8 hours) and a **refresh token** (6 months,
replaced on every refresh). A request made with the user access token acts as
the user, limited to the intersection of what the App was granted, what the
installation covers and what the user can do. The tokens go straight from
GitHub to swarm-api's server-side exchange and from there to Secret Manager;
no person ever handles a value.

**(C) A GitHub OAuth App.** The user authorises an OAuth App with the `repo`
scope. The token acts as the user on every repository the user can reach, in
every org that has approved the OAuth App, and does not expire.

### 1.2 Compared honestly

| criterion | (A) PAT per org | (B) GitHub App, user access token | (C) OAuth App |
|---|---|---|---|
| Acts as the user | Yes: pushes and PRs are the user's, from a token the user minted | Yes: pushes, PRs and comments are the user's; GitHub may also show the App the request came through | Yes: the user's, through the OAuth App |
| Per-org selection | Classic: one token reaches every org that is SSO-authorised. Fine-grained: one token per org, so selection is which tokens exist | One installation per org; enabling an org is installing there | None: every org that approved the OAuth App, at once |
| Per-repo selection | Fine-grained only, chosen in GitHub's token page; classic is all-or-nothing | The installation's selected repositories, plus SwarmCloud's own grant list inside them | None at GitHub; SwarmCloud's grant list only |
| SAML SSO orgs | Classic: each token must be SSO-authorised per org, by hand, or the org's resources are withheld. Fine-grained: needs a linked SAML identity, no per-token step | No per-token step. The user needs a linked SAML identity for the org, and GitHub withholds the org until the user has an active SSO session; a 403 carries `X-GitHub-SSO` with the URL to open | Like the App: an active SSO session at authorisation; the org must also approve the OAuth App |
| Org-admin approval | The org may forbid classic tokens, or require approval of each fine-grained token (pending until approved) | An org owner installs the App, or approves a member's install request; a later permission increase needs re-approval | Under OAuth app access restrictions an org owner approves the App once, for every member |
| Lifetime and rotation | Classic: optional expiry, rotated by hand. Fine-grained: expiry required (org may cap it), rotated by hand: the user mints a new one and stores it again | 8-hour access token refreshed automatically by SwarmCloud; 6-month refresh token renewed on each refresh; nobody rotates anything | No expiry and no rotation: the token lives until revoked |
| Revocation | The user deletes the token at GitHub; SwarmCloud can only disable its own copy | SwarmCloud revokes the token through the App's token API; the user can revoke the authorisation; an org owner can uninstall, which ends the org's access for everyone | SwarmCloud revokes through the OAuth App's token API; the user or the org can revoke |
| Where the secret lives | Secret Manager, `swarm-tenant-<t>-git-u-<hex>` per org token; enters by stdin | Secret Manager: access token in `swarm-tenant-<t>-git-u-<hex>`, refresh token in its `-refresh` twin; the App's client secret in a platform secret | Secret Manager, one non-expiring token per user |
| Invariant 9 | Holds: tenant-named secrets, read by the tenant's worker account | Holds: tenant-named secrets per user; the App's client secret is platform-wide but grants nothing without a user's refresh token | Holds per secret, but the token's reach is every repository of the user, which is the widest blast radius here |
| Several users per tenant | Each user mints and stores tokens per org; heavy, and most users will not | Each user authorises once; installations are shared by every user of the org | Each user authorises once |
| Effort | Small: slots exist; needs the stdin route and per-owner resolution | Largest: App registration, exchange, refresher, access API, worker credential | Medium: like B without installations, so without per-org or per-repo control |

### 1.3 Recommendation

**Recommended: (B), with (A) as the fallback and today's tenant token kept.**
B is the only option where the org itself chooses which repositories
SwarmCloud may touch (the installation), where acting as the user needs no
long-lived secret anyone handles (an 8-hour user access token, refreshed by
SwarmCloud from a refresh token that never leaves Secret Manager and swarm-api),
where SSO needs no per-token chore, and where removing an org has a GitHub-side
answer (uninstall) as well as SwarmCloud's own. A fine-grained PAT (A) stays as
the fallback for an org whose admin will not install the App: it is the same
user slot, a different `kind`, and the user stores it by stdin. C is rejected:
it has neither per-org nor per-repo control at GitHub, and a non-expiring token
that reaches every repository of the user is the opposite of what #780 asks.

Custody, for every option, and not softened:

* A token is **never in the repository** (it is public: a value committed once
  is published, and rewriting history does not unpublish it), never in a
  tfvars file, never in a log line, an event, an API response or the
  **Job environment**. It lives in Secret Manager only and the worker reads it
  at runtime, as `swarm-tenant-<tenant>-git` is read today.
* Every forge reply that becomes evidence goes through `redact` with the token
  as a literal, as the probe already does
  (`apps/swarm-api/swarm_api/gittokens.py::redaction_literal`).
* A PAT reaches Secret Manager only by `scripts/create-secrets.sh --stdin` or,
  if D5 opens it, the one stdin route of §3.2; there is no console paste box.

### 1.4 What an org admin must do

* **(A)** Allow the token type: either leave classic PATs permitted (Settings ›
  Personal access tokens), or permit fine-grained PATs and approve each pending
  request. The user SSO-authorises a classic token per org themself.
* **(B)** Install the SwarmCloud App on the org once, choosing all or selected
  repositories, or approve a member's install request from the org's settings;
  approve a permission increase if the App ever asks for more. Add or remove
  repositories from the installation later. Nothing per user.
* **(C)** Approve the OAuth App under the org's third-party application access
  policy, once; there is nothing per repository to choose.

---

## 2. The onboarding flow

### 2.1 One resumable state machine

The flow is **resumable** and the same on both surfaces: the console's
checklist and the plugin's `/sc:setup` both read `GET /v1/onboarding` and act
through the same routes, so a user who connects GitHub in the console and
chooses repositories from the plugin sees one checklist. The state is per
(tenant, user). A step's state is **derived from evidence** each time it is
read (a connection record, an enabled org, a grant, a probe result), never a
flag a client sets, so a step that was done and stopped being true (a revoked
authorisation, an uninstalled org) shows as such without anyone editing it.

The steps, in order: `signed_in`, `github_connected`, `app_installed`,
`orgs_enabled`, `repos_chosen`, `access_verified`, `ready`. `app_installed`
was added on 2026-10-08 (#780) after the owner connected GitHub, authorised
the App, never installed it, and found an Access page with no orgs and no
explanation: authorising the App (Connect) and installing it are two acts at
GitHub, and only an installation lets SwarmCloud list an org. Each is in one of `todo`,
`in_progress`, `done`, `failed` (with a code from §2.3) or `stale` (done once,
its evidence older than its re-verification interval). The "next" step is the
first that is not `done`; the plugin resumes there and the console opens there.

### 2.2 Steps, what "done" means, and each step's probe

| step | done when | verification probe | failure states |
|---|---|---|---|
| `signed_in` | the caller has a verified Google identity and a resolved tenant | the platform's own tenant resolution (per registered group, with `x-goog-user-project`); no forge call | none here: an unresolved tenant is the sign-in's error |
| `github_connected` | a connection record for this user is `active`, with `forge_login` read from GitHub | `GET /user` with the new token: the login, the account id, and for a PAT the `X-OAuth-Scopes` and `github-authentication-token-expiration` headers; one call | `AUTHORISATION_DENIED`, `AUTHORISATION_EXPIRED`, `REFRESH_FAILED`, `CLASSIC_PAT_BLOCKED` |
| `app_installed` | the SwarmCloud GitHub App is installed on at least one owner the person reaches; done without a check for a connection that is a token rather than the App | an owner the person enabled was installed when enabled, so it answers with no forge call; otherwise `GET /user/installations` (one refresh of the person's token, the read Work › Access makes) | `FORGE_UNREACHABLE`; with no installation the step is `todo`, offering the App's install page |
| `orgs_enabled` | at least one owner (the user's account or an org) is enabled and its installation is reachable | `GET /user/installations` paged at 100 per page until a short page, plus `GET /user/orgs` to show orgs with no installation; each owner gets `installed`, `requested` or `not_installed` | `ORG_APPROVAL_PENDING`, `SSO_NOT_AUTHORISED`, `CLASSIC_PAT_BLOCKED`, `FINE_GRAINED_PAT_PENDING` |
| `repos_chosen` | at least one repository is granted, each `read` or `write`, each registered | `GET /user/installations/{installation_id}/repositories` paged (100 per page, `page` until a short page, served to the chooser one page at a time with a server-side search); a typed `owner/repo` is read once with `GET /repos/{owner}/{repo}` | `REPO_NOT_INSTALLED`, `REPO_ARCHIVED`, `SSO_NOT_AUTHORISED` |
| `access_verified` | every granted repository passed the checks its mode needs | clone: the smart-HTTP `upload-pack` advertisement (a GET of `info/refs?service=git-upload-pack`); push (write grants): the `receive-pack` advertisement plus `permissions.push` on `GET /repos/{owner}/{repo}`; pull request (write grants): the installation's `pull_requests: write` permission and the same push bit; all are reads, as the probe's are today (`apps/swarm-api/swarm_api/forge.py::git_basic_headers`). An optional write test creates and deletes one branch (decision D6) | `PERMISSION_MISSING`, `SSO_NOT_AUTHORISED`, `REPO_NOT_INSTALLED`, `FORGE_UNREACHABLE` |
| `ready` | every step above is `done` | none of its own; it offers the first task in a granted repository | none |

Each probe runs in swarm-api, not in a worker, so it costs no lease and no
capacity. A read that did not come back (a 5xx, a 429, a network error) is not
an answer: the step stays as it was and says `FORGE_UNREACHABLE`, exactly the
probe's rule today (§5.3 of git-tokens.md).

### 2.3 Failure states and their recovery copy

The copy is served by the API with the code, so the console and the plugin
print the same words. `{owner}`, `{repo}` and `{url}` are filled in; `{url}` is
a GitHub page, never a URL that carries a token.

| code | detected by | recovery copy, word for word |
|---|---|---|
| `SSO_NOT_AUTHORISED` | a 403 carrying `X-GitHub-SSO: required; url=…` on an org's resource | “{owner} uses SAML single sign-on and GitHub has not linked your session to it yet. Open {url}, sign in with {owner}'s identity provider, then press Re-check. Nothing in SwarmCloud has to change.” |
| `CLASSIC_PAT_BLOCKED` | a 403 whose message says the org forbids access by a personal access token (classic) | “{owner} does not accept classic personal access tokens. Connect with the SwarmCloud GitHub App instead (recommended), or create a fine-grained token whose resource owner is {owner} and store it with `uv run sc setup token --owner {owner}`.” |
| `ORG_APPROVAL_PENDING` | the user asked to install the App and is not an org owner; `GET /user/installations` does not list {owner} yet | “You asked {owner}'s owners to install SwarmCloud. Nothing can be read in {owner} until one of them approves it in {owner}'s settings › GitHub Apps. You can carry on with your other orgs; this step re-checks on its own every 15 minutes, or press Re-check.” |
| `FINE_GRAINED_PAT_PENDING` | a fine-grained token whose `GET /repos/{owner}/{repo}` answers 404 while {owner} lists a pending request | “Your fine-grained token for {owner} is waiting for an org owner's approval. Until it is approved GitHub shows it only public repositories. Ask an owner of {owner} to approve it under Settings › Personal access tokens › Pending requests.” |
| `REPO_NOT_INSTALLED` | a typed or granted repository that the installation does not cover | “SwarmCloud is installed on {owner} but not for {repo}. Add {repo} to the installation at {url} (an org owner may have to), then press Re-check. SwarmCloud never reaches a repository the installation leaves out.” |
| `AUTHORISATION_DENIED` | GitHub's callback came back with `error=access_denied` | “GitHub says the authorisation was cancelled, so SwarmCloud has no access. Press Connect GitHub to start again; nothing was stored.” |
| `AUTHORISATION_EXPIRED` | the callback's `state` is unknown, used, or older than 10 minutes | “That sign-in link has expired or was already used. Press Connect GitHub for a fresh one; links last 10 minutes and work once.” |
| `REFRESH_FAILED` | the refresher's refresh was refused (revoked, expired after 6 months, or the App's authorisation removed) | “SwarmCloud's access as {login} has ended at GitHub, so tasks you submit will wait instead of running. Press Reconnect to authorise again; your orgs and repositories are kept.” |
| `PERMISSION_MISSING` | a write grant whose push or pull-request check came back `missing` | “You chose write for {repo}, but GitHub does not let {login} push there through SwarmCloud. Ask for write access to {repo}, or change the grant to read.” |
| `REPO_ARCHIVED` | `archived: true` on the repository read | “{repo} is archived on GitHub, so nothing can be pushed to it. Unarchive it, or grant it read only.” |
| `FORGE_UNREACHABLE` | a 5xx, 429 or network error on the probe | “GitHub did not answer this check, so its result is unknown, not failed. It is retried on the next pass; press Re-check to try now.” |

### 2.4 Adding or removing an org or a repository later

The steady-state **Access** page (and `uv run sc access`) is the same state
machine with every step done:

* **Add an org**: from the reachable owners (§2.2's `orgs_enabled` probe), the
  user picks one with no installation; SwarmCloud sends them to the App's
  install page for that org (or the "request" page when they are not an org
  owner, which is `ORG_APPROVAL_PENDING`). It is enabled when the installation
  is visible. Adding repositories to an existing installation is done at
  GitHub; the chooser shows them on the next read.
* **Remove an org**: SwarmCloud deletes the user's `forge_orgs` record and every
  grant under that owner, in one write, so a task naming one of its
  repositories is **refused** at submission from that moment, and a worker
  already running is refused at its next push or pull request, where it
  re-reads the grant (§3.3 step 4). The token already inside that running
  attempt is not narrowed by the deletion: it stays valid at GitHub for the
  installations that remain until it expires (at most 8 hours). What GitHub still
  allows is said plainly: the user's token is not per org, so the page offers
  the installation's settings link (an org owner uninstalls) and, if it is
  the user's last org, "Disconnect GitHub", which revokes the token.
* **Add a repository**: the chooser, or a typed `owner/repo` (read once to
  check it). The grant is `read` or `write`; adding registers the repository
  for the tenant if it is not yet registered.
* **Remove a repository**: the grant is deleted; the tenant's registration is
  kept while another user still holds a grant on it, and deleted with the
  last one. Tasks for it by this user are refused from then on.

### 2.5 Several users in one tenant

Each user connects their own GitHub account: a second user in `eng` goes
through `github_connected` themself and gets their own connection and user
slot. Installations are an org's, so the second user's `orgs_enabled` step is
usually already satisfied for `example-org`: they see it as installed and only
enable it. Grants are per user: a repository is in the tenant's list once, and
each user holds their own `read` or `write` on it. A task acts as its
`submitted_by`, the verified email signed into its spec, never as whoever
connected first. The tenant view for an admin shows every member's connection
state and grants, never a value.

---

## 3. Data model, API, the worker's credential, Terraform

### 3.1 Firestore documents

All in the `swarm` database, every document carries `tenant_id`, and every
read is filtered on it (invariant 9).

* `onboarding/{tenant_id}__{user_hash}`: `tenant_id`, `user` (email, lower
  case), `user_hash` (16 hex of sha256 of the email, as
  `provider_suffix` hashes it), `steps` (map of step to
  `{state, code, checked_at, evidence}`), `next_step`, `dismissed_at` (the
  user hid the checklist; nothing stops), `updated_at`. A cache of the
  derivation in §2.1, rewritten on every read that changes it.
* `forge_connections/{connection_id}`: `connection_id` (`conn_` + 16 hex of
  tenant + user + forge), `tenant_id`, `user`, `forge` (`github`), `method`
  (`app_user`, `fine_grained_pat`, `classic_pat`), `forge_login`,
  `forge_user_id`, `token_id` (the `git_tokens` record of the user slot),
  `access_expires_at`, `refresh_expires_at`, `refreshed_at`, `refresh_lease`
  (`{holder, until}`, so one refresher at a time uses a refresh token that
  works once), `state` (`active`, `refresh_failed`, `revoked`), `created_at`,
  `revoked_at`, `revoked_by`. No field holds a value.
* `git_tokens/{token_id}`: the existing record
  (`apps/swarm-api/swarm_api/gittokens.py::GitTokenRecord`), scope `user`,
  with a new kind `app_user` beside `TOKEN_KINDS`; its probe and permission
  matrix work unchanged. One record per user slot; a fallback PAT for one org
  is a second user slot with `repo_ids` narrowed to that org's grants.
* `forge_orgs/{tenant_id}__{user_hash}__{owner}`: `owner` (lower case),
  `owner_type` (`User`, `Organization`), `installation_id`,
  `repository_selection` (`all`, `selected`), `install_state` (`installed`,
  `requested`, `not_installed`), `sso` (`ok`, `required`, `unknown`),
  `enabled_at`, `enabled_by`, `checked_at`.
* `forge_grants/{tenant_id}__{user_hash}__{repo_id}`: `repo_id`,
  `repository` (`owner/repo`), `owner`, `mode` (`read`, `write`),
  `granted_at`, `granted_by`, `checks` (`clone`, `push`, `pull_request`, each
  `ok`, `missing` or `unknown` with `checked_at`).
* `forge_authorizations/{state_hash}`: a pending browser authorisation:
  `tenant_id`, `user`, `surface` (`console`, `plugin`), `expires_at` (10
  minutes), `used_at`. The `state` itself is never stored, only its hash; the
  authorisation code is never stored at all.
* `repositories/{repo_id}`: unchanged, registered on the first grant.

### 3.2 API routes, all additive

Every route below is additive: nothing existing changes shape.

| route | what it does |
|---|---|
| `GET /v1/onboarding` | the caller's steps, next step, recovery copy |
| `POST /v1/onboarding/dismiss` | hide the checklist (steps keep their state) |
| `POST /v1/onboarding/github/authorize` | {surface} -> {authorize_url, expires_in_seconds} |
| `POST /v1/onboarding/github/exchange` | {state, code} -> the connection, never a token |
| `POST /v1/onboarding/github/token` | D5 only: a PAT on the request body, once, stored as a version |
| `DELETE /v1/onboarding/github` | disconnect: revoke the token at GitHub, delete grants |
| `GET /v1/access` | the caller's connection, orgs and grants (the Access page) |
| `GET /v1/access/orgs` | reachable owners and each one's install_state, sso |
| `POST /v1/access/orgs` | {owner}: enable an installed owner |
| `DELETE /v1/access/orgs/{owner}` | disable, and delete its grants |
| `GET /v1/access/orgs/{owner}/repositories` | ?page=N&q=text: one page, each with registered, granted, mode |
| `PUT /v1/access/grants/{repo_id}` | {repository, mode}: grant read or write |
| `DELETE /v1/access/grants/{repo_id}` | revoke the grant |
| `POST /v1/access/grants/{repo_id}/verify` | {checks?}: run clone, push, pull_request now |
| `GET /v1/access/members` | admin: every member's connection state and grants |

`GET /v1/repositories/readable` and `POST /v1/repositories` stay; phase 0
makes the console use `readable`'s paging, and phase 3 makes both resolve the
caller's own slot (R2's user leg) instead of the tenant token.

**No route returns a token**, a refresh token, the App's client secret, the
`code` or the `state` after it is issued. The exchange happens server-side:
GitHub redirects the browser to the console's `/onboarding/github/callback`
page, which posts `{state, code}` to the exchange with the user's own ID token,
so the binding of an authorisation to a person is the platform's ordinary
sign-in. On the plugin path the callback page does the same in the browser
the user signed in with; if that browser is not signed in to the console it
shows a one-time code to paste into the terminal, the shape `sc account add`
already uses. The code is useless without the client secret, which only
swarm-api reads. Every route takes the tenant from `tenant_scope`, never the
body, and another tenant's id is the same 404 as a missing one.

### 3.3 How the worker gets the per-task credential

Today `Worker._git_token` reads `swarm-tenant-<tenant>-git` through
`resolve_git_token`. The change is which suffix it reads, decided by
swarm-api at submission and carried by name:

1. **At submission** swarm-api resolves the task's repository and its
   `submitted_by` against the grants: no grant is a refusal
   (`REPOSITORY_NOT_GRANTED`, 403, with "choose it under Access"); a grant is
   the user's slot suffix `git-u-<hex>` and its mode. Resolution order
   becomes user grant, then repository token, then tenant token only where the
   tenant allows it (decision D4).
2. **The scheduler** checks the connection's `state` at admission, as
   `CREDENTIAL_MISSING` is decided today: `credentials.py::credential_for`,
   asked by `loop.Scheduler._admit_one` before any pool is reserved, also
   answers "no" for a `refresh_failed` connection, so the task parks
   `CREDENTIAL_MISSING` without a lease or a worker and a dead token costs no
   capacity. The existing credential sweep (`loop.Scheduler._promote_credentials`)
   asks the same question and returns the park to `READY` once the connection
   is `active` again. Nothing here is checked in `dispatch.py`, which runs
   after the lease is reserved.
3. **The worker** reads `tenant.secret_name(task.forge_credential or "git")`
   at runtime (the latest version, which the refresher keeps at least two
   hours from expiry), registers it with the redaction filter as today, and
   reads it again before each push or pull request, since an attempt can
   outlive an 8-hour token. That re-read refreshes the token, not the grant;
   step 4 re-reads the grant. A `read` grant makes the worker refuse to push:
   it never configures a push credential for that task.
4. **Before cloning, and again before each push or pull request**, the
   worker re-reads the grant document by `repo_id` (tenant-scoped). A grant
   deleted since submission ends the attempt before the agent starts if it is
   found before cloning, without touching the lease beyond releasing it; found
   before a push or pull request, the worker checkpoints, refuses that forge
   write, and ends the attempt. What this cannot stop is said plainly: the
   token already in the running attempt's workspace is a user token, which
   reaches every installation the user has until it expires (at most 8
   hours), so an agent that uses it directly, outside the worker's push, is
   bounded by GitHub's installation list, not by the grant.

The suffix must come from somewhere the worker can trust. Two ways:

* **With request E (recommended; ACCEPTED by the owner 2026-10-07, filed
  and applied as
  [contract request 54](contract-change-requests.md#54-modelspy--specsignpy-a-task-does-not-say-which-forge-credential-it-uses-or-whether-it-may-write))**:
  a `forge_credential` and a `forge_access` field on `Task`, written by
  swarm-api only, covered by the spec signature through a new projection in
  `canonical_step_spec`
  (`apps/common/swarm_common/specsign.py::canonical_step_spec`, format 3), so a
  doctored document fails verification. This is the path being built: the
  frozen half is applied, and lanes OB7 (swarm-api writes the fields at
  submission) and OB5 (the worker reads them) wire it.
* **Without it**: the worker derives the suffix from the signed `submitted_by`
  (which `canonical_step_spec` already covers) and reads the grant for the
  mode. This needs no frozen change, but it restates the user-hash rule in
  the worker (a parity check, like `scripts/lib/check-contract-parity.sh`
  does for the shell), and it cannot express "this task uses the tenant
  token", so D4's fallback would be impossible.

#### Contract request E (accepted by the owner 2026-10-07, filed as request 54)

This was written as a draft for the owner. **The owner accepted it on
2026-10-07**; it is filed and applied as
[request 54 in contract-change-requests.md](contract-change-requests.md#54-modelspy--specsignpy-a-task-does-not-say-which-forge-credential-it-uses-or-whether-it-may-write),
which is now the record, and is no longer a draft that is not filed. It
supersedes request (E) as git-tokens.md §8 sketched it, by adding the mode and
the signature. The summary below is kept as the design's statement of it.

* **What is true today:** `Task` (`apps/common/swarm_common/models.py::Task`)
  says nothing about which forge secret a task uses; the worker reads
  `Tenant.secret_name("git")`
  (`apps/common/swarm_common/models.py::Tenant.secret_name`), and
  `canonical_step_spec` signs `submitted_by` and `metadata` but no credential.
* **The requested change:** two optional fields on `Task`:
  `forge_credential: str | None` (a provider suffix matching
  `git(-[ru]-[0-9a-f]{16})?`) and `forge_access: str | None` (`read` or
  `write`), both set only by swarm-api at submission from the resolution in
  step 1 above and never accepted from a caller; and a new `SPEC_FORMAT` in
  `specsign.py` (format 3) whose projection includes both. Absent means `git`
  and `write`, today's behaviour. As applied, formats 1 and 2 refuse a
  document that sets either field, so one cannot be added to a task signed
  without it.
* **What it would break if accepted:** nothing existing: documents without
  the fields decode as today, and the older spec format still verifies for
  documents signed under it.
* **If it is declined:** the "without it" path above: user slots derived from
  `submitted_by`, no tenant-token fallback for a user without a grant, and the
  user-hash rule held equal in two places by a parity test.
* **Invariants:** 9, because the field names a suffix that
  `Tenant.secret_name` places under the task's own tenant, so it can never
  name another tenant's secret; 10, because no caller sets it; 5, because the
  worker verifies the signature before reading anything the field names.

### 3.4 Terraform and IAM

Every item goes through the **dev-iam** owner review, and every new resource
carries `managed-by=swarm-terraform` (the Cloud Scheduler job, which has no
labels, says it in its `description`, as
`terraform/modules/scheduler/jobs.tf` (`description = "managed-by=swarm-terraform; one-minute safety tick for the scheduler drain loop"`)
does).

1. **The GitHub App itself** is registered by hand (GitHub has no API for it
   that this platform should hold); its non-secret settings (app id, client
   id, slug, callback URL `https://<console>/onboarding/github/callback`)
   become tfvars, and its permissions are: contents read and write, pull
   requests read and write, issues read and write, checks read, metadata read,
   workflows write only if D8 says so. "Expire user authorization tokens" on.
2. **A platform secret for the App's client secret**, declared by Terraform
   with no version, value added by `scripts/create-secrets.sh --stdin`;
   `secretAccessor` for swarm-api only.
3. **User slots are created at onboarding, not by an apply**, because
   self-service is the point (decision D3). swarm-api gets a custom role with
   `secretmanager.secrets.create` (Secret Manager evaluates creation against
   the project, so a name condition cannot narrow it; the role carries no
   read) and `secretVersionAdder` on user slots, by IAM condition. `resource.name` for
   a secret is the full form `projects/<number>/secrets/<name>`, so the
   condition is written as a prefix on that form, one per tenant:
   `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-<t>-git-u-")`. Runtime-created secrets are
   labelled `managed-by=swarm-api` and `swarm-tenant=<tenant>`; teardown
   (`docs/runbooks/tenant-offboarding.md`) gains a step that deletes them by
   label, since no Terraform plan will.
4. **The tenant's worker account reads its own users' slots**:
   `secretAccessor` at the project, conditioned on
   `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-<t>-git-u-")
   && !resource.name.endsWith("-refresh")` (the full resource name, not the
   bare secret name), one conditional
   binding per tenant, from the tenancy module. This replaces the per-secret
   authoritative binding for these slots only, and the review should weigh
   exactly that.
5. **The refresher** (swarm-api in phase 2, decision D2) gets
   `secretAccessor` and `secretVersionAdder` on the `-refresh` twins and
   `secretVersionAdder` on the base slots, never accessor on a base slot, the
   same split the subscription refresher has today. For "Disconnect GitHub"
   (owner decision 2026-10-07, OB3), swarm-api also holds a custom role,
   `swarmForgeSlotVersionManager`, with exactly
   `secretmanager.versions.disable` and `secretmanager.versions.enable` (no
   destroy, no access), on the same per-tenant prefix condition as its
   `secretVersionAdder`: a disconnect disables the user's versions so no
   usable token is left, and a reconnect enables them.
6. **A Cloud Scheduler job, `swarm-forge-refresh`, every 15 minutes**, calling
   swarm-api's refresh sweep with an OIDC token; description
   `managed-by=swarm-terraform; refreshes GitHub user access tokens before they
   expire`.
7. **Firestore composite indexes** for `forge_grants` and `forge_orgs` on
   (`tenant_id`, `user_hash`), in the firestore module.
8. **The custom role** is bootstrapped as the others are
   (`docs/runbooks/custom-roles-to-bootstrap.md`).

### 3.5 Migration from today's single tenant token

`swarm-tenant-<tenant>-git` stays **unchanged**: same secret, same readers,
same record (`ensure_tenant_default`), same use by the merge step and by
registration until phase 3. Onboarding adds a user slot beside it; nothing is
moved. Once a user has connected, their tasks resolve their grant first; a
task by a user who has not connected keeps using the tenant token while the
tenant's policy allows it (D4), and the console says which credential a task
used. The tenant token is retired by the owner, not by a lane: when the
resolution events show it unused for a fortnight, the Access page offers
"retire the tenant token", which disables its version with
`create-secrets.sh --disable-previous` semantics and keeps the record as
`revoked`. For `eng` specifically: the owner connects as `example-user`,
installs the App on their own account (selecting SwarmCloud's repository) and
on `example-org` (an org owner's action), grants both, and the existing
classic PAT can then be revoked at GitHub.

### 3.6 Invariants, each with how it holds

**1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Every onboarding
probe, listing and verification runs inside swarm-api on a request, never as
a task, so none of it holds a lease or a slot; a task whose connection has
failed parks `CREDENTIAL_MISSING` at admission, as `CREDENTIAL_MISSING` is
today (`credentials.py::credential_for`), rather than starting a worker to
find out, and a parked task costs nothing.

**2. All-or-nothing reservation in one transaction.** Admission is untouched:
the credential is resolved at submission, before admission sees the task, and
the scheduler's connection check happens at admission, as `CREDENTIAL_MISSING`
is today (`credentials.py::credential_for`, asked by `loop.Scheduler._admit_one`
before the transaction), so no pool is reserved and then released because of
a credential, and nothing here
splits the single transaction that reserves every pool.

**3. Concurrency counts from LEASED.** Nothing in this design changes when a
lease is taken or counted; the refresher and the probes are not tasks and are
not counted, and a task waiting on a re-authorisation is PARKED, which was
never counted, so the count from LEASED stays the only count.

**4. Workers never sleep through a long provider wait.** A worker that finds
its user token expired or refused does not wait for the user to re-authorise:
it checkpoints, parks the task `CREDENTIAL_MISSING`, releases and exits, and
the existing credential sweep (`loop.Scheduler._promote_credentials`, asking
`credential_for`) returns it to `READY` when the connection is `active` again,
exactly as a missing provider key is handled today.

**5. Every attempt carries a fencing generation.** A stale worker must still
exit before running the agent and before reading any credential, and this
design fixes the order lane OB5 builds: the generation check comes first, then the signature check over the spec (which, with
request E, covers `forge_credential`), then the credential read, so a stale or
doctored attempt never holds the user's token.

**6. Spot is disabled.** No new compute is introduced: the probes run in
swarm-api, the refresher is a swarm-api route on a Cloud Scheduler tick, and
the worker runs on the same Jobs and profiles as today, none of which use
Spot, so nothing here can reintroduce a preemptible node or pod.

**7. requests == limits.** No profile, resource class or Job shape changes;
the worker reads one more secret at runtime, which costs no memory worth
sizing, and the swarm-api service's resources are untouched, so no new
container has a request different from its limit.

**8. Mandatory periodic checkpointing.** Unchanged and still required: a
worker refused by a revoked grant or an expired token mid-attempt checkpoints
before it parks, so a re-authorisation costs minutes rather than the attempt;
an 8-hour token is precisely a reason an attempt can be interrupted, not a
reason to checkpoint less.

**9. Per-tenant isolation.** Every user slot and refresh twin is named through
`Tenant.secret_name` under the task's own tenant, read only by that tenant's
worker account (by the per-tenant name condition) and swarm-api; every
document carries `tenant_id` and is read filtered by it; a user who belongs to
two tenants connects separately in each, so no token crosses tenants. Within a
tenant the worker account can read every member's slot, which is git-tokens.md
option U1, and decision D7 offers the stronger U3.

**10. Callers pick a runner_profile by name.** No caller supplies a
credential, a secret name, a suffix or a mode on a task: swarm-api resolves
them from the signed `submitted_by` and the stored grants, and a body that
names one is refused by the existing forbid-unknown-fields models
(`apps/swarm-api/swarm_api/schemas.py::StrictModel`), so a caller still picks
only a profile by name.

---

## 4. The sc plugin flow

### 4.1 Commands

* **`/sc:setup`** (a new `plugin/commands/` entry, granted
  `Bash(uv run sc setup:*)` and `Bash(uv run sc access:*)` by name): runs the
  state machine from wherever it stands, through the bridge tools below, and
  prints the same checklist the console draws.
* **`uv run sc setup`**: the same wizard in a terminal without Claude Code;
  `uv run sc setup status` prints the checklist and exits 0 when `ready`, 1
  when a step could not be read, 3 when a step has failed.
* **`uv run sc setup token --owner <org>`**: the PAT fallback, value read from
  stdin without echo, only if D5 opens the route.
* **`uv run sc access`**: the steady state; `uv run sc access add-org <owner>`,
  `remove-org <owner>`, `grant <owner/repo> --read|--write`,
  `revoke <owner/repo>`, `verify [<owner/repo>]`, `disconnect`.

`/sc:setup` never runs `disconnect` or `remove-org` itself: like `login`,
those are the user's to type (`/sc` already refuses to run `login` for the
same reason).

### 4.2 Bridge tools (swarm-mcp)

| tool | route | returns |
|---|---|---|
| `swarm_setup_status` | `GET /v1/onboarding` | steps, next step, each failure's code and copy |
| `swarm_setup_connect` | `POST /v1/onboarding/github/authorize` | the URL to open and its expiry; it opens the browser when it can |
| `swarm_setup_orgs` | `GET /v1/access/orgs`, `POST /v1/access/orgs` | reachable owners with `install_state` and `sso`; enables one |
| `swarm_setup_repos` | `GET /v1/access/orgs/{owner}/repositories` | one page, with `next_page`, filtered by `q` |
| `swarm_setup_grant` | `PUT /v1/access/grants/{repo_id}` | the grant |
| `swarm_setup_verify` | `POST /v1/access/grants/{repo_id}/verify` | clone, push, pull_request per repository |
| `swarm_access` | `GET /v1/access` | the Access page as text |

None takes or returns a token, a code or a state; `swarm_setup_connect`
returns the authorize URL, whose `state` parameter GitHub requires and which
is printed nowhere else (as `cmd_account_add` does).

### 4.3 A first-time user, end to end

`example-user`, a member of `eng`, signed in with `uv run sc login` already;
`example-org` enforces SAML SSO and the user is not one of its owners.

```text
> /sc:setup

  swarmcloud · swarm_setup_status
  SwarmCloud setup for example-user@example.com in tenant eng
    [x] signed_in          Google identity verified, tenant eng
    [ ] github_connected   next
    [ ] orgs_enabled
    [ ] repos_chosen
    [ ] access_verified
    [ ] ready

  Next: connect GitHub. SwarmCloud will act as you, through the SwarmCloud
  GitHub App. Your token goes from GitHub to Secret Manager; you never see it.

  swarmcloud · swarm_setup_connect
  Opened your browser to authorise SwarmCloud on GitHub.
  If it did not open, visit:
    https://github.com/login/oauth/authorize?client_id=<app>&state=<shown once>
  The link expires in 10 minutes.

  (the user approves in the browser; the console's callback page says
   "Connected as example-user. You can return to your terminal.")

  swarmcloud · swarm_setup_status
    [x] github_connected   connected as example-user (GitHub App, user token,
                           refreshed automatically)

  swarmcloud · swarm_setup_orgs
  Owners you can reach:
    example-user   your account   not installed
    example-org    organisation   not installed (you are a member, not an owner)
  Install SwarmCloud on which? [example-user, example-org]: both
  Opened the install page for example-user: choose "Only select repositories".
  Opened the request page for example-org: an owner of example-org must approve.

  swarmcloud · swarm_setup_status
    [~] orgs_enabled       example-user installed (2 repositories)
                           example-org  ORG_APPROVAL_PENDING
    You asked example-org's owners to install SwarmCloud. Nothing can be read
    in example-org until one of them approves it in example-org's settings ›
    GitHub Apps. You can carry on with your other orgs; this step re-checks on
    its own every 15 minutes, or press Re-check.

  Carry on with example-user now? [Y/n]: y

  swarmcloud · swarm_setup_repos  owner=example-user page=1
    example-user/example-api     private   push
    example-user/example-site    public    push
  Grant which, read or write? example-user/example-api write

  swarmcloud · swarm_setup_grant   example-user/example-api  write
  swarmcloud · swarm_setup_verify  example-user/example-api
    clone          ok   upload-pack advertised
    push           ok   receive-pack advertised, permissions.push true
    pull request   ok   installation grants pull_requests: write

  Setup paused at orgs_enabled for example-org. Run /sc:setup again to resume.

  --- the next day ---

> /sc:setup

  swarmcloud · swarm_setup_status
  Resuming at orgs_enabled.
    [~] orgs_enabled       example-org installed (selected: 14 repositories)
  swarmcloud · swarm_setup_orgs   enable example-org

  swarmcloud · swarm_setup_repos  owner=example-org q=service page=1
    example-org/example-service      private   push
    example-org/example-service-ui   private   push
  Grant which, read or write? example-org/example-service write

  swarmcloud · swarm_setup_verify  example-org/example-service
    clone          SSO_NOT_AUTHORISED
    example-org uses SAML single sign-on and GitHub has not linked your session
    to it yet. Open https://github.com/orgs/example-org/sso, sign in with
    example-org's identity provider, then press Re-check. Nothing in
    SwarmCloud has to change.

  (the user signs in with example-org's identity provider in the browser)

  Re-check? [Y/n]: y
  swarmcloud · swarm_setup_verify  example-org/example-service
    clone          ok
    push           ok
    pull request   ok

  swarmcloud · swarm_setup_status
    [x] signed_in  [x] github_connected  [x] orgs_enabled
    [x] repos_chosen  [x] access_verified  [x] ready
  SwarmCloud can clone, push and open pull requests as example-user in
  example-user/example-api and example-org/example-service. Anything else is
  refused. Change this any time with `uv run sc access` or Work › Access.
```

The transcript is the console's flow step for step: the same step ids
(`app_installed`, added later, is `[x]` once an installation exists), the same
codes, the same copy. A task submitted afterwards for
`example-org/example-other`, which was not granted, is refused at submission
with `REPOSITORY_NOT_GRANTED` and "choose it under Access".

---

## 5. Build plan

Lanes, smallest useful first. Within one phase no file is in two lanes; a lane
depends only on lanes of earlier phases. New files are named without their
root, since they do not exist yet.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| OB0 | 0 | the Register picker pages with `next_page` until `capped`, filters by owner, and accepts a typed owner/repo that posts to `POST /v1/repositories` | `apps/swarm-ui/src/RepositoriesRegister.tsx`, `apps/swarm-ui/src/RepositoriesData.ts`, `apps/swarm-ui/src/api.ts` | — |
| OB0b | 0 | the probe records the `X-GitHub-SSO` header and the orgs a token reaches; the no-access refusal names SSO and the classic-token policy | `apps/swarm-api/swarm_api/gittokens.py`, `apps/swarm-api/swarm_api/repositories.py` | — |
| OB1 | 1 | the onboarding document and `GET /v1/onboarding`, derived from today's records (tenant token, registrations, probe) | new `swarm_api/onboarding.py`, new `swarm_api/routes/onboarding.py`, `apps/swarm-api/swarm_api/main.py` | OB0b |
| OB2 | 1 | the App's registration runbook and Terraform: client-secret slot, user-slot IAM, refresher grants, the scheduler job, indexes, the custom role | `terraform/modules/secret_manager/`, `terraform/modules/tenancy/`, `terraform/modules/scheduler/jobs.tf`, `terraform/modules/firestore/`, new `runbooks/github-app.md` | — |
| OB3 | 2 | authorise, exchange, refresh sweep, disconnect; the `app_user` kind; the connection document | new `swarm_api/forgeapp.py`, new `swarm_api/routes/forgeapp.py`, `apps/swarm-api/swarm_api/gittokens.py`, `apps/swarm-api/swarm_api/main.py` | OB1, OB2 |
| OB4 | 3 | the access API (orgs, repositories with paging and search, grants, verify) and register and readable resolving the caller's slot | new `swarm_api/access.py`, new `swarm_api/routes/access.py`, `apps/swarm-api/swarm_api/repositories.py`, `apps/swarm-api/swarm_api/main.py` | OB3 |
| OB5 | 3 | the worker reads the task's credential, re-reads it before a push, refuses a push on a read grant, re-checks the grant before cloning and before each push or pull request | `apps/agent-worker/agent_worker/lifecycle.py`, `apps/agent-worker/agent_worker/secrets.py` | OB3 |
| OB6 | 3 | admission parks a task whose connection has failed as `CREDENTIAL_MISSING`, and the credential sweep returns it to `READY` when the connection is active | `apps/scheduler/scheduler/credentials.py`, `apps/scheduler/scheduler/loop.py` | OB3 |
| OB7 | 4 | submission resolves the grant and refuses an ungranted repository | `apps/swarm-api/swarm_api/validation.py`, `apps/swarm-api/swarm_api/routes/tasks.py` | OB4, OB5 |
| OB8 | 4 | the console onboarding checklist and the Access page, from the owner's picks | new `src/Onboarding.tsx`, new `src/Access.tsx`, `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/api.ts`, `.github/ISSUE_TEMPLATE/` | OB4 |
| OB9 | 4 | `sc setup`, `sc access`, the bridge tools, `/sc:setup` | `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/server.py`, `plugin/README.md`, new `commands/setup.md` | OB4 |
| OB10 | 5 | migration: tenant-token fallback policy, "retire the tenant token", the docs | `docs/git-tokens.md`, `docs/repo-index.md`, `docs/onboarding.md` | OB7, OB8, OB9 |

OB2 and OB3 get the one review (credentials, tenant isolation, IAM); OB2 is
also the dev-iam review. OB5 needs the frozen request of §3.3 accepted, or
builds the "without it" path; OB8 adds a tab, so it updates the issue forms in
the same lane (`tests/unit/scripts/test_issue_forms.py` holds them to the nav).

---

## 6. Owner decisions

### D1. Which mechanism does SwarmCloud use to act as the user?

* **(a) The GitHub App with user access tokens (B)**, a fine-grained PAT (A)
  as the fallback, the tenant token kept. Per-org and per-repo control at
  GitHub, nothing long-lived handled by a person; the largest build.
* **(b) PATs only (A)**: SSO-authorise today's classic token for each org, or a
  fine-grained token per org. Hours of work after phase 0; every rotation and
  every org is a manual chore for every user.
* **(c) An OAuth App (C)**: no per-org or per-repo control, a token that never
  expires.

**Recommendation:** (a). It is the only option that satisfies "choose the
repositories SwarmCloud may access" at GitHub as well as in SwarmCloud.

### D2. Who refreshes user access tokens?

* **(a) swarm-api**, on a Cloud Scheduler sweep: no new service; swarm-api then
  reads refresh tokens and the client secret.
* **(b) A dedicated forge broker** with its own service account, like the
  quota broker: swarm-api never reads a refresh token; one more service.
* **(c) The quota broker's existing refresher**, extended to GitHub: reuses
  the subscription pattern; mixes two unrelated credentials in one identity.

**Recommendation:** (a) for phase 2, behind an interface that (b) can take
over, because the exchange already needs the client secret in swarm-api.

### D3. How are user slots created?

* **(a) swarm-api creates them at onboarding** (S2-like, §3.4 item 3):
  self-service; a project-level create grant without read.
* **(b) Terraform declares each user's slot** (S1): reviewable; every new user
  waits for an apply, so onboarding is not operator-free.

**Recommendation:** (a): #780's first acceptance line is "without an
operator", and the grant it needs carries no read.

### D4. What does a task do when its submitter has not connected GitHub?

* **(a) Refuse at submission** with "connect GitHub under Setup".
* **(b) Use the tenant token**, as today, and say so on the task.
* **(c) Tenant token for tenant automation only** (indexing, scheduled runs);
  refuse for a person's task.

**Recommendation:** (c) once a tenant has finished migrating, (b) until then,
set per tenant, so nothing that works today stops on the day this ships.

### D5. May a personal access token reach the API, for the fallback?

* **(a) No**: a PAT enters only by `scripts/create-secrets.sh --stdin`, run by
  someone with Secret Manager rights (today's rule, PICKS.md Git tokens B).
* **(b) Yes, from the CLI only**: `uv run sc setup token --owner <org>` reads
  stdin and posts it once to `POST /v1/onboarding/github/token`; no console
  box.

**Recommendation:** (b), because (a) leaves the fallback needing an operator,
which is what #780 removes; the route stores and never returns.

### D6. May verification write to a repository?

* **(a) Reads only**: advertisements and permission bits; nothing is created.
* **(b) An opt-in write test**: create and delete one branch named
  `swarmcloud/onboarding-check-<nonce>`.
* **(c) Also open and close a draft pull request**.

**Recommendation:** (a) by default with (b) offered per repository: a real
push is the only proof of push, and it should be the user's choice.

### D7. How strongly are users isolated inside one tenant?

* **(a) U1**: the tenant's worker account reads every member's slot; the
  resolver uses only the submitter's.
* **(b) U3**: a reader identity per user, used by a credential step.

**Recommendation:** (a) now, with 8-hour tokens limiting what a misused read
is worth; (b) if a tenant mixes people who should not act as each other.

### D8. What may SwarmCloud do as the user beyond clone, push and pull request?

* **(a) Contents, pull requests, issues, checks read**: today's capabilities.
* **(b) Also workflows write**: pushing a change under `.github/workflows/`
  needs it, and it is the permission an org admin is most likely to refuse.

**Recommendation:** (a), with (b) as a separate App permission request when a
tenant asks for it.

### D9. Where is a read-only grant enforced?

* **(a) By SwarmCloud**: submission and the worker refuse a push on a read
  grant; GitHub would allow it if the user can write.
* **(b) Also by GitHub**: a second, read-only App for read grants.

**Recommendation:** (a): it is enforced in the two places that run the work,
and two Apps double what every org admin must approve.

### D10. Which screens are built?

* **(a) The recommended variants** on the mock-up page.
* **(b) The owner's picks**, recorded in PICKS.md.

**Recommendation:** (b); the page recommends one variant per screen to make
the pick quick, and recommends nothing it cannot justify.
