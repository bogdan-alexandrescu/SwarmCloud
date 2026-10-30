# Registering the SwarmCloud GitHub App

**Who:** the deployment's owner, once per deployment (and again only to rotate
a secret). **Takes:** one merged pull request and its release, one bootstrap
apply, ten minutes at github.com, two `create-secrets.sh` runs, and a second
small pull request with the App's public settings. **Why:** owner decision D1
on [#780](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/780)
(2026-10-07): SwarmCloud acts on GitHub **as the user**, through a GitHub App's
user access tokens, so each user chooses at GitHub which orgs and repositories
it may reach. The design is [docs/onboarding.md](../onboarding.md) §1 and §3.4;
this page is lane OB2 of its §5.

GitHub has no API for creating an App that this platform should hold, so the
registration is by hand. Everything around it is code:

| what | where | who applies it |
|---|---|---|
| the App's public settings (id, client id, slug) | `terraform/environments/dev/dev.tfvars`: `github_app_id`, `github_app_client_id`, `github_app_slug` | the release |
| two empty secret slots, `swarm-github-app-client-secret` and `swarm-github-app-private-key`, swarm-api their one reader | `terraform/modules/secret_manager` (`github_app`, `github_app_accessor`), switched on by `enable_github_app` | the release, held at `dev-iam` |
| the user slots' IAM: `swarmForgeSlotCreator` and `swarmForgeSlotVersionManager` for swarm-api, and per tenant swarm-api's version-add, version disable/enable and read (base slots and `-refresh` twins). No worker reads a user slot: it obtains one through swarm-api's broker (#1041) | `terraform/bootstrap/forge_user_slots.tf`, switched on by `enable_forge_user_slots` | the owner, by bootstrap apply |
| the refresh sweep, `swarm-forge-refresh`, every 15 minutes | `terraform/modules/scheduler/jobs.tf` (`forge_refresh`), switched on by `enable_forge_refresh` | the release, once lane OB3 ships |
| the `forge_grants` and `forge_orgs` indexes on (`tenant_id`, `user_hash`) | `terraform/modules/firestore/indexes.tf` | the release |
| the values of the client secret and the private key | Secret Manager only, by `scripts/create-secrets.sh --github-app <slot> --stdin` | the owner, from a terminal |
| the push receiver's create-only Firestore role `swarmGithubHooksDedup` (`datastore.entities.create`) and the deployer's right to grant it | `terraform/bootstrap/github_hooks_role.tf` and `deployer_conditions.tf`, switched on by `enable_github_hooks_dedup_role` ([Step 9](#step-9-the-push-webhook), step 2) | the owner, `scripts/bootstrap.sh` |
| the push receiver `swarm-github-hooks`, its webhook secret's container and reader, its binding to that role and the delivery TTL | `terraform/infra/github_hooks.tf`, switched on by `enable_github_hooks` ([Step 9](#step-9-the-push-webhook), step 3) | the release, held at `dev-iam` |
| the webhook secret's value | Secret Manager only, by `scripts/create-secrets.sh --github-app-webhook-secret --stdin` | the owner, from a terminal |

## The rule this page exists to keep

**The client secret and the private key are never written to this repository,
a tfvars file, a Job's environment, a log line, an issue, a pull request or a
chat.** The repository is public: a value committed once is a value published,
and rewriting history does not unpublish it (CLAUDE.md, "Writing Terraform").
They reach Secret Manager by stdin and nothing else; `create-secrets.sh
--github-app` refuses `--from-file`, a tenant, a provider, and a slot
Terraform has not made. The **client id** is different: it is in every
authorise URL a browser follows, so it is public by design and lives in
tfvars.

If a secret is ever pasted anywhere else, treat it as leaked: generate a new
one at GitHub, store it (Rotating, below), and delete the old one at GitHub.

## Step 1: the slots exist (a release, held at dev-iam)

`terraform/environments/dev/dev.tfvars` sets `enable_github_app = true` (the
pull request that added this page did). Its release's plan creates, and is
held at the `dev-iam` environment for the owner because it adds IAM:

* `module.secret_manager.google_secret_manager_secret.github_app["client-secret"]`
* `module.secret_manager.google_secret_manager_secret.github_app["private-key"]`
* `module.secret_manager.google_secret_manager_secret_iam_binding.github_app_accessor["client-secret"]`
  -- `roles/secretmanager.secretAccessor`, members exactly
  `serviceAccount:swarm-api@saga-agents-staging.iam.gserviceaccount.com`
* `module.secret_manager.google_secret_manager_secret_iam_binding.github_app_accessor["private-key"]`
  -- the same role and the same one member
* `module.firestore.google_firestore_index.this["forge-grants-tenant-user"]`
* `module.firestore.google_firestore_index.this["forge-orgs-tenant-user"]`

Read the held plan for exactly these and nothing else of this change. Both
secrets carry `managed-by=swarm-terraform` and `component=github-app`, so
`make destroy` can remove them; neither gets a version from Terraform.

## Step 2: the user slots' grants (an owner bootstrap apply)

`terraform/bootstrap/terraform.tfvars` sets `enable_forge_user_slots = true`.
From `main`, `make bootstrap` (`scripts/bootstrap.sh`) plans, and the plan
must show exactly, with `<n>` the project number and `<t>` each tenant in
`dev.tfvars`:

* `google_project_iam_custom_role.forge_slot_creator[0]` -- **swarmForgeSlotCreator**,
  permissions exactly `secretmanager.secrets.create`;
* `google_project_iam_member.forge_slot_creator[0]` -- swarm-api, that role,
  **no condition** (creation is checked on the project, which carries no
  secret name, so no condition could narrow it; it creates an empty secret
  and can do nothing else to it);
* `google_project_iam_member.forge_slot_version_adder["<t>"]` -- swarm-api,
  `roles/secretmanager.secretVersionAdder`, condition
  `resource.name.startsWith("projects/<n>/secrets/swarm-tenant-<t>-git-u-")`;
* `google_project_iam_custom_role.forge_slot_version_manager[0]` --
  **swarmForgeSlotVersionManager**, permissions exactly
  `secretmanager.versions.disable` and `secretmanager.versions.enable` (no
  destroy, no access): "Disconnect GitHub" disables the user's versions so
  no usable token is left, and a reconnect enables them (OB3);
* `google_project_iam_member.forge_slot_version_manager["<t>"]` -- swarm-api,
  that role, with exactly the version adder's condition,
  `resource.name.startsWith("projects/<n>/secrets/swarm-tenant-<t>-git-u-")`;
* `google_project_iam_member.forge_refresh_reader["<t>"]` -- swarm-api,
  `roles/secretmanager.secretAccessor`, on that prefix: the `-refresh` twins
  **and** the base slots (owner decision 2026-10-08). swarm-api reuses the
  person's current access token instead of refreshing on every call -- each
  refresh makes GitHub end the access token a running agent holds, which then
  fails 401 -- and reading it adds no power, since swarm-api already reads
  the refresh token that mints access tokens. Changing the condition replaces
  the binding; the resource carries `create_before_destroy`, so the new
  binding exists before the old one goes. Until this is applied swarm-api
  falls back to refreshing per call, as before.

And the tenant's worker: `forge_slot_reader["<t>"]`, `secretAccessor` on
that prefix's BASE slots, never a `-refresh` twin (decision D7/U1). The
owner's 2026-10-11 decision that secrets are user-scoped (#1041) supersedes
it: swarm-api's broker, `POST /v1/attempts/forge-credential`
([design/user-scoped-secrets.md](../design/user-scoped-secrets.md)), is built
behind the `forge_broker` switch (FORGE_BROKER), off by default and off in
dev. **The binding stays in this root until the broker has been on in dev
and a live run passed**; while it is off, it is how every user-slot agent
clones. A plan of this root shows it unchanged.

**Removing it is a later, separate owner change, never part of the change
that ships or turns on the broker.** Before writing that change, check each
of these for the environment, and stop at the first that fails:

1. **The nonce measurement has been made** (design §5, "The precondition
   this rests on"; [child-tasks.md §9](../design/child-tasks.md#9-phase-2)
   item 2): a tenant service account cannot read a Cloud Run execution's
   overrides or a GKE pod spec in its namespace. Made in dev on 2026-10-11
   (recorded in `dev.tfvars`); not made in prod. Not made means the broker
   is not a guarantee and is not switched on: keep the binding, and stop
   here.
2. **`swarm-child-key` has a version** (`scripts/create-secrets.sh --child-key`).
3. **`enable_child_tasks = true` and `swarm_api_url`** are set in
   `terraform/environments/<env>/<env>.tfvars` and applied by a release
   (`terraform/infra/child_tasks.tf` gives the order; set in dev since
   2026-10-11, in prod not at all).
4. **`forge_broker = true`** is set in that tfvars and applied by a release
   carrying the worker's broker client
   (`apps/agent-worker/agent_worker/forgebroker.py`) -- read the running
   worker image's commit off the release, and `FORGE_BROKER=true` off
   swarm-api's and the scheduler's environment.
5. **A live run passed with it on**: a user-slot agent and a user-slot child
   of one clone and push through the broker, and another member's agent is
   refused that slot.

With the broker off, the worker reads a person's slot with its own account,
as before #1041. Destroy the binding before the broker is on, and that read
is `PERMISSION_DENIED`: every agent that resolved to a person's slot fails
closed until the broker is on. Turning `forge_broker` off or rolling the
worker back after the removal has the same effect; re-adding the binding is
the undo.

These are bootstrap's, not the release's, on purpose: a project-level
`secretAccessor` grant made by CI would have to be on the deployer's
grantable list, and that list limits which roles CI grants, never to whom --
CI could then grant itself read of every secret in `saga-agents-staging`, the
other team's included. `terraform/bootstrap/forge_user_slots.tf` has the full
reasoning. A tenant added later gets its four per-tenant
grants from the same bootstrap apply that grants the deployer on its worker
account ([docs/ci.md](../ci.md), "A new account exists before the release that
adds it").

## Step 3: register the App at GitHub

Signed in as the owner, open **Settings → Developer settings → GitHub Apps →
New GitHub App** on the account that should own it (an org you own keeps it
from depending on one person's account).
Fill in exactly:

| field | value | why |
|---|---|---|
| GitHub App name | `SwarmCloud` (if taken, `SwarmCloud Saga`) | what users and org admins see when they authorise and install |
| Homepage URL | `https://swarm.saga.xyz` | the console |
| Callback URL | `https://swarm.saga.xyz/onboarding/github/callback` | the console's callback page posts `{state, code}` to swarm-api's exchange with the user's own sign-in (onboarding.md §3.2). Terraform derives the same value from `frontend_hostname` (`terraform output github_app`); they must agree |
| Expire user authorization tokens | **ticked** | user access tokens live 8 hours and are refreshed by the sweep; a token that never expires is the OAuth App this design rejected (D1) |
| Request user authorization (OAuth) during installation | **ticked** | installing and authorising are one trip, so a new user ends on the callback page with a code |
| Enable Device Flow | unticked | the plugin reuses the browser sign-in or a pasted one-time code (onboarding.md §3.2), never device flow |
| Setup URL | empty (GitHub greys it out once the box above is ticked) | the callback URL serves both |
| Redirect on update | unticked | changing the repository selection at GitHub needs no new code; the Access page reads installations when it loads |
| Webhook → Active | **unticked** at registration, off -- until [Step 9](#step-9-the-push-webhook) | nothing receives GitHub events until the receiver `swarm-github-hooks` is deployed and holds the webhook secret; Step 9 turns it on, for `push` only |
| Where can this GitHub App be installed? | **Any account** | each user installs it on their own account and on each org they enable; "Only on this account" would refuse every other org |

**Repository permissions**, per owner decision D8 (everything not listed:
No access):

* Contents: Read and write -- clone and push
* Pull requests: Read and write -- open and update the agent's pull request
* Issues: Read and write -- the issue-run comments (#454)
* Checks: Read-only -- the merge step's CI wait
* Metadata: Read-only -- mandatory, GitHub sets it
* Actions: Read and write -- the merge step re-runs the failed jobs of a
  red CI run at the same commit (`mergewake.py`,
  `GitHubWriter.rerun_failed_jobs`, else `GitHubWriter.rerun_job` for one
  job; both `forgewrite.ACTIONS_WRITE`, no new grant, #2841); held since
  before 2026-10-23
* Workflows: Read and write -- added to the App on 2026-10-23 (#2029); each
  installation's owner accepts it. A repository uses it only when its
  `workflow_files` switch is on. A push that changes `.github/workflows/` or
  `.github/actions/` needs it: swarm-api mints a token for that ONE
  repository with `contents: write` and `workflows: write`, per push, never
  stored. An installation that has not accepted it reads as "Workflows: not
  granted" on a switched-on repository, and its pushes fall back to
  `workflow-lines.patch`. Why off by default: a workflow on a pushed branch
  runs with that repository's secrets before any merge hold.
* Secrets: Read and write, and Environments: Read and write -- added for
  writing GitHub Actions secrets (SSC-ACTIONS-API, #2158; BGD's Q3 of
  secrets-scopes.md §11: the existing App, no second one). Steps and the
  reasoning: [Writing GitHub Actions secrets](#writing-github-actions-secrets).
  **Not Administration**: SwarmCloud creates, edits and deletes no
  environment until user-scoped-secrets step 5 (Q4).

**Organization permissions** and **Account permissions**: none. **Subscribe
to events**: none at registration; Step 9 subscribes `push` and nothing else.

Create the App. GitHub shows its settings page.

## Step 4: store the client secret and the private key

From a checkout of `main`, with `gcloud` signed in as the owner:

1. On the App's settings page, **Generate a new client secret**. GitHub shows
   it once. Run the command below and paste it at the hidden prompt:

   ```bash
   scripts/create-secrets.sh --github-app client-secret --stdin
   ```

2. Under **Private keys**, **Generate a private key**. The browser downloads a
   `.pem` file. Redirect it into the script, then delete the file:

   ```bash
   scripts/create-secrets.sh --github-app private-key --stdin < ~/Downloads/<slug>.<date>.private-key.pem
   rm ~/Downloads/<slug>.<date>.private-key.pem
   ```

The script refuses a slot Terraform has not created (run step 1 first), a
private key that is not a PEM block, a PEM block offered as a client secret,
and any value as an argument or a file path. It prints the version number and
never the value.

Check without reading anything back:

```bash
gcloud secrets versions list swarm-github-app-client-secret --project saga-agents-staging --filter=state=ENABLED --format='value(name)'
gcloud secrets get-iam-policy swarm-github-app-client-secret --project saga-agents-staging --format='value(bindings.members)'
```

The first prints one version; the second prints swarm-api alone. The same two
for `swarm-github-app-private-key`. Never run `gcloud secrets versions access`
on either: there is no question it answers that the two above do not.

## Step 5: the public settings into tfvars

From the App's settings page, copy the **App ID** (a number), the **Client
ID** (`Iv1.` or `Iv23` followed by letters and digits) and the slug (the last
part of its public page, `github.com/apps/<slug>`) into
`terraform/environments/dev/dev.tfvars`:

```hcl
github_app_id        = "<App ID>"
github_app_client_id = "<Client ID>"
github_app_slug      = "<slug>"
```

Open a pull request with those three lines. The three are set together or not
at all (a `check` in `terraform/infra/github_app.tf` says so), and the client
id's validation refuses anything that is not a client id -- including a
client secret pasted into the wrong place. `terraform output github_app` after
the release shows the settings, the callback URL and the two slot ids, and no
value.

## Step 6: what the next lanes wire

Nothing reads the App until lane OB3 (onboarding.md §5): its swarm-api code
reads the client id from its environment and the client secret by Secret
Manager reference. That wiring lands with OB3 and **after** step 4, because a
Cloud Run revision that references a secret with no version fails to start
(`terraform/infra/child_tasks.tf` records the same order).

## Prove the key

Nothing in the user flow signs with the private key, so a key pasted into the
wrong slot, truncated, or deleted at GitHub would otherwise go unnoticed until
the first code that needs it. Once swarm-api's environment carries
`GITHUB_APP_ID` and `GITHUB_APP_SLUG` and the key has a version (step 4), ask
it, as a platform admin (`scripts/api.sh` keeps the ID token out of argv,
[security.md](../security.md)):

```bash
scripts/api.sh GET /admin/forge/app
```

swarm-api reads `swarm-github-app-private-key`'s latest version, signs an App
JWT with it (RS256, `iss` the App id, nine minutes), and calls GitHub's
`GET /app`. The answer is exactly `{ok, app_id, slug, reason}`:

| answer | means | do |
|---|---|---|
| `ok: true` | GitHub accepted the key and named the App configured here | nothing |
| `private_key_missing` | the slot has no enabled version, or swarm-api may not read it | step 4, then step 1's IAM check |
| `private_key_malformed` | the version is not an RSA PEM | store the downloaded `.pem` again (step 4) |
| `key_rejected` | GitHub answered 401: the key is not one of this App's, or was deleted there | generate a new key at GitHub and store it |
| `app_id_mismatch`, `app_slug_mismatch` | the key belongs to another App, or tfvars name another one | compare `app_id`/`slug` in the answer with step 5's values |
| `app_id_not_configured`, `app_slug_not_configured` | swarm-api's environment lacks the setting | step 5's release |
| `github_unreachable`, `github_refused` | GitHub did not answer, or answered something else | run it again; it reads nothing else |

The answer never carries GitHub's body, the JWT or any of the key, and
swarm-api logs only `ok` and `reason`. Run it again after every rotation.

## Step 7: switch the refresh sweep on

Once OB3's release serves `POST /v1/admin/forge/refresh` and admits the
rollup-sweeper account to it, set in `dev.tfvars`:

```hcl
enable_forge_refresh = true
```

The release then creates `module.scheduler.google_cloud_scheduler_job.forge_refresh[0]`,
**swarm-forge-refresh**: every 15 minutes, POST with an OIDC token minted for
`swarm-rollup-sweeper`, audience swarm-api's URL, no retry (a refresh token
works once; the next tick is the retry), description
`managed-by=swarm-terraform; refreshes GitHub user access tokens before they
expire`. It adds no IAM member: the account's one grant, `run.invoker` on
swarm-api, already exists. Before OB3 the job would answer 404 every tick,
which is why it is off.

## Step 8: prove the conditions, once the first user slot exists

The mock-provider tests hold the conditions' text, not what IAM answers
(`forge_user_slots.tf`, "NOT VERIFIED LIVE"). After the first user connects
(OB3), ask the Policy Troubleshooter, which evaluates conditions and reads no
value, with `<n>` the project number and `<slot>` that user's slot:

```bash
gcloud policy-intelligence troubleshoot-policy iam \
  "//secretmanager.googleapis.com/projects/<n>/secrets/<slot>/versions/1" \
  --principal-email=swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com \
  --permission=secretmanager.versions.access
```

Expected, while `forge_slot_reader` stands (the broker off, step 2): for
`swarm-agent-worker-eng@`, access **granted** for `<slot>` and **denied** for
`<slot>-refresh` (the worker never reads a refresh token); for `swarm-api@`,
**granted** for both. A worker granted `<slot>-refresh` means the base-only
condition does not match the resource-name form IAM evaluates for a version:
set `enable_forge_user_slots = false` and apply bootstrap before anything
else. swarm-api denied means its condition does not match: the same.

After the later removal of `forge_slot_reader` (step 2's checklist, with the
broker on): `swarm-agent-worker-eng@` **denied** for both (#1041: no worker
reads a user slot), `swarm-api@` still granted for both. A worker granted is a
binding the removal missed: find it with `gcloud projects get-iam-policy` and
remove it. Then submit an agent as that user in a group tenant: it clones and
pushes through the broker, and swarm-api logs
`forge credential released decision=released` with the secret's name and no
value.

## Step 9: the push webhook

**Why:** owner answer Q3 on
[#1384](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/1384)
(2026-10-15): a push to a registered repository's default branch makes its
index poll happen now, instead of at the next five-minute tick
([docs/design/graph-service.md](../design/graph-service.md) §6.2). The
five-minute poll stays: GitHub does not retry a failed delivery, and the poll
is the fallback for every one that is lost.

**What the door is.** swarm-api sits behind IAP, which GitHub cannot pass, so
the receiver is its own Cloud Run service, `swarm-github-hooks`, with ingress
`all` and `allUsers` holding `run.invoker` on that service alone -- the "new
unauthenticated door" this page used to refuse, kept as narrow as a door can
be. It accepts only `POST /github/events`, a body of at most 1 MiB, and a
valid `X-Hub-Signature-256` (HMAC-SHA256 of the raw body under the webhook
secret, compared in constant time); anything unsigned or badly signed is a 401
that never reaches Firestore or swarm-api. A `ping` is a 204, every other
event a 204 that is dropped. It keeps only a non-deleting push to
`refs/heads/<default branch>`, records `github_deliveries/<X-GitHub-Delivery>`
with `create()` (a redelivery is a 200 and nothing more; the document expires
after 7 days), and forwards the repository's `full_name` -- never the
payload's commit -- to swarm-api's `POST /v1/admin/repositories/push`, which
re-reads the head with each workspace's own token.

**THE IDENTITY, AND ITS BLAST RADIUS.** Owner decision 2026-10-16, recorded on
#1384: the receiver **runs as the rollup sweeper account**
(`swarm-rollup-sweeper`, `module.scheduler.rollup_sweeper_email`) and calls
swarm-api as it; `POST /v1/admin/repositories/push` was added to that account's
route allow-list (`ROLLUP_SWEEPER_ROUTES` in
`apps/swarm-api/swarm_api/auth.py`). A dedicated identity was recommended and
declined. So a **compromised receiver -- a public-facing service -- holds every
route the sweeper holds, for every tenant**: the workflow rollup, the run
advance, the repository poll and push, the merge wake, the GitHub user-token
refresh sweep, the workspace dispatch sweep, the `task_finished` push, the
stranded-PR sweep and the workspace-secrets rotation sweep. For the dedup's
`create()` it also holds the custom role `swarmGithubHooksDedup`, which grants
`datastore.entities.create` and nothing else (HOOKS-ROLE, PR 1423; owner
decision 2026-10-16, which replaced `roles/datastore.user`). Firestore
evaluates no IAM condition on documents, so the role's single permission is
what narrows it: it may create a document in any collection, and it can read,
query, update or delete none. None of those routes accepts an image, a command or a
secret value, and none makes the caller an admin or a member; that is the
whole of the mitigation. If the receiver is ever suspected, set
`enable_github_hooks = false` and release: the service, its public invoker,
the secret's reader and the Firestore role go with it, and the five-minute
poll carries on alone.

In this order:

1. **The image.** Every release builds and promotes `swarm-github-hooks`
   (`scripts/build-images.sh`, `scripts/push-images.sh`), so `image_refs`
   carries its digest from the first release after GS-HOOKS merged.

2. **The dedup role (the bootstrap, which the owner applies).** Set
   `enable_github_hooks_dedup_role = true` in
   `terraform/bootstrap/terraform.tfvars`, plan, read the rows and apply with
   `scripts/bootstrap.sh`. The plan adds `swarmGithubHooksDedup`
   (`datastore.entities.create` only; `terraform/bootstrap/github_hooks_role.tf`)
   and adds it to the deployer's grantable project roles
   (`terraform/bootstrap/deployer_conditions.tf`). Do this first: step 3's
   binding names the role, and the deployer may grant only what that list holds.

3. **The receiver and its IAM (a release, held at dev-iam).** Set in
   `dev.tfvars`:

   ```hcl
   enable_github_hooks = true
   ```

   The plan adds the service, `allUsers` → `roles/run.invoker` on it, the
   webhook secret's container (no version) with the sweeper account as its one
   accessor, the sweeper account's binding to `swarmGithubHooksDedup`, and the TTL policy
   on `github_deliveries.expires_at`. The IAM rows wait at the `dev-iam` gate
   for the owner. The service starts without the secret's value: it reads it
   at run time, so until step 4 every signed delivery is a 503, not a revision
   that fails to start. `terraform -chdir=terraform/infra output github_hooks`
   then names the secret (`webhook_secret`: `swarm-github-app-webhook-secret`,
   declared in `terraform/infra/github_hooks.tf`) and the URL (`webhook_url`).

4. **Store the webhook secret.** Choose it on the App's settings page, in the
   **Webhook secret** field (a long random string; GitHub never shows it again
   once saved), and paste the same value at the hidden prompt:

   ```bash
   scripts/create-secrets.sh --github-app-webhook-secret --stdin
   ```

   It refuses an argument, a file, an empty value and a container Terraform
   has not made (step 2 first), keeps the value exactly as typed except the
   paste's trailing newline, and prints the version number, never the value.
   Check without reading anything back, with `<secret>` the output's
   `webhook_secret`:

   ```bash
   gcloud secrets versions list <secret> --project saga-agents-staging --filter=state=ENABLED --format='value(name)'
   gcloud secrets get-iam-policy <secret> --project saga-agents-staging --format='value(bindings.members)'
   ```

   The first prints one version; the second prints the rollup sweeper account
   alone.

5. **Tick the webhook at GitHub.** On the App's settings page:

   | field | value |
   |---|---|
   | Webhook → Active | **ticked** |
   | Webhook URL | the output's `webhook_url`: the service's URL followed by `/github/events` |
   | Webhook secret | the value stored in step 3 |
   | SSL verification | enabled |

   Then **Permissions & events → Subscribe to events**: tick **Push** and
   nothing else (it needs the Contents permission the App already holds).
   Save. GitHub sends a `ping`, which the receiver answers 204: the App's
   **Advanced → Recent Deliveries** shows it green. A red ping is a 401 (the
   two secrets differ: store it again) or a 503 (the receiver has not read the
   secret yet: it re-reads at most once a minute; wait one and redeliver).

6. **A registration opts in.** A push is read only for a repository
   registration whose `on_change` is `webhook` ("poll and push");
   `poll` stays poll only, and `off` neither. Set it with the registration's
   `PATCH /v1/repositories/<repo_id>` and the body
   `{"index": {"on_change": "webhook"}}` (`repositories.IndexSettings`); a
   script uses that same call. The console offers it too (HOOKS-UI, PR 1410):
   the register form's **Change trigger** offers **Webhook · on push to the
   default branch** at registration, and a repository's settings change it in
   place with that same PATCH, carrying `index.on_change` alone. Both show the
   note that the App's webhook must be ticked (this step).
   `min_change_interval_minutes` (default 120 since #2524, was 30) applies to pushes as well, so a
   busy default branch does not queue a run per merge.

**Rotating the webhook secret.** GitHub holds one webhook secret at a time, so
there is a short gap: change it at GitHub, store the same value with step 3's
command, and the receiver reads it on the first delivery that fails to verify
(at most one re-read a minute). A delivery lost in between is caught by the
next five-minute poll. Then disable the old version by hand
(`gcloud secrets versions disable <N> --secret <secret>`).

## Writing GitHub Actions secrets

swarm-api writes a repository's or an environment's GitHub Actions secret
for a workspace manager whom GitHub calls admin of that repository
(`apps/swarm-api/swarm_api/actions_secrets.py`,
`routes/actions_secrets.py`; design: `docs/design/secrets-scopes.md` §3,
with BGD's answers in its §11). It mints from **this** App, per call, a
token for ONE repository and the ONE permission that call needs
(`forge_installation.ACTIONS_PERMISSIONS`), seals the value to GitHub's
public key with PyNaCl, PUTs it, and keeps only the name, who and when.
Until the App holds the permissions below and an installation has accepted
them, every write to that installation's repositories is refused, 409 with
`detail.reason` `secrets_not_granted` (or `environments_not_granted`) and
`detail.review_url`, the installation's page to accept them, and nothing is
written.

What BGD does on GitHub, once:

1. **Settings → Developer settings → GitHub Apps → SwarmCloud → Permissions
   & events → Repository permissions.**
2. Set **Secrets** to **Read and write**. A repository secret's public key,
   its PUT and its DELETE need it (GitHub's "Permissions required for GitHub
   Apps": Secrets).
3. Set **Environments** to **Read and write**. An environment secret's public
   key needs read; its PUT and DELETE need **write** (the same page lists
   them under Environments, not Secrets).
4. Leave **Administration** at **No access**, and every other permission as
   [Step 3](#step-3-register-the-app-at-github) lists it. Actions: Read is
   already held; it is what reads an environment and its deployment branch
   rules for **Check rule**.
5. **Save changes.** GitHub asks whether to notify installations: yes. The
   App's settings now request the two permissions; no installation has
   them yet.

Then, on **each installation** whose repositories should take a secret
(`GET /v1/forge/permissions` lists them, each with its `review_url` and
`pending_update: true` until accepted):

6. Open the installation's `review_url` (an org's is
   `https://github.com/organizations/<org>/settings/installations/<id>/permissions/update`;
   a personal account's is under `https://github.com/settings/installations/<id>`).
   Only the org's owner (or the account itself) can accept.
7. Review the request for **Secrets: Read and write** and **Environments:
   Read and write**, and **Accept new permissions**.
8. Prove it: `GET /v1/repositories/<repo_id>/actions/secrets` lists the
   repository's secret names (GitHub serves no value) instead of refusing
   with `secrets_not_granted`.

**Environments are created by a person, on GitHub (Q4).** SwarmCloud never
creates, edits or deletes one: a write into an environment that does not
exist is refused with GitHub's page to create it
(`https://github.com/<owner>/<repo>/settings/environments/new`). Create it
there with **Deployment branches and tags → Selected branches and tags** and
one rule, the default branch (`main`). `GET
/v1/repositories/<repo_id>/actions/environments/<name>/rule` then answers
`main_only: true`. A write into an environment whose rule is anything else
(no rule, protected branches, or more than that one branch) is refused unless
the request types the environment's name in `confirm_environment`, because a
secret there reaches every workflow those branches run.

**The risk BGD accepted with Q3.** A repository admin's App user token sits
in a `-git-u-` slot tenant workers can read until user-scoped-secrets step 5,
and with Secrets: write on the App such a token can overwrite that
repository's Actions secrets. Step 5 closes that; Administration waits for it.

## Rotating

GitHub keeps two client secrets (and several private keys) valid at once, so a
rotation never has a gap:

1. Generate the new one at GitHub and store it with the same step 4 command:
   a new version, which swarm-api reads as `latest`.
2. Once swarm-api has served an exchange or a refresh with it, delete the old
   one at GitHub.
3. Disable the old version by hand:
   `gcloud secrets versions disable <N> --secret swarm-github-app-client-secret --project saga-agents-staging`.

`create-secrets.sh --github-app` refuses `--disable-previous`: disabling
before step 2 would refuse every exchange in between.

## User slots outside Terraform

swarm-api creates each user's slot at onboarding (D3), labelled
`managed-by=swarm-api` and `swarm-tenant=<t>`, so no Terraform plan lists or
removes them. To see a tenant's:

```bash
gcloud secrets list --project saga-agents-staging --filter='labels.managed-by=swarm-api AND labels.swarm-tenant=<t>' --format='value(name)'
```

Offboarding a tenant must delete them by label, since no Terraform plan will
(onboarding.md §3.4 item 3). `scripts/offboard-tenant.sh` finds a tenant's
secrets by `labels.tenant`, so the slots OB3 creates must also carry
`tenant=<t>` for it to reach them; until OB3 ships there are no user slots to
reach. [tenant-offboarding.md](tenant-offboarding.md) gains its step with the
lane that first creates a slot.
