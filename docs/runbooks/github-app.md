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
| the user slots' IAM: `swarmForgeSlotCreator` and `swarmForgeSlotVersionManager` for swarm-api, and per tenant swarm-api's version-add, version disable/enable and `-refresh` read, the worker's base-slot read | `terraform/bootstrap/forge_user_slots.tf`, switched on by `enable_forge_user_slots` | the owner, by bootstrap apply |
| the refresh sweep, `swarm-forge-refresh`, every 15 minutes | `terraform/modules/scheduler/jobs.tf` (`forge_refresh`), switched on by `enable_forge_refresh` | the release, once lane OB3 ships |
| the `forge_grants` and `forge_orgs` indexes on (`tenant_id`, `user_hash`) | `terraform/modules/firestore/indexes.tf` | the release |
| the values of the client secret and the private key | Secret Manager only, by `scripts/create-secrets.sh --github-app <slot> --stdin` | the owner, from a terminal |

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
  `roles/secretmanager.secretAccessor`, on that prefix **and** a `-refresh`
  twin;
* `google_project_iam_member.forge_slot_reader["<t>"]` -- the tenant's
  worker, `swarm-agent-worker-<t>`, `roles/secretmanager.secretAccessor`, on
  that prefix and **not** a `-refresh` twin.

These are bootstrap's, not the release's, on purpose: a project-level
`secretAccessor` grant made by CI would have to be on the deployer's
grantable list, and that list limits which roles CI grants, never to whom --
CI could then grant itself read of every secret in `saga-agents-staging`, the
other team's included. `terraform/bootstrap/forge_user_slots.tf` has the full
reasoning, and why the `-refresh` test reads both the secret's and the
version's resource name. A tenant added later gets its four per-tenant
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
| Webhook → Active | **unticked**, off | the design needs no webhook: nothing here receives GitHub events, and an endpoint that accepts them would be a new unauthenticated door |
| Where can this GitHub App be installed? | **Any account** | each user installs it on their own account and on each org they enable; "Only on this account" would refuse every other org |

**Repository permissions**, per owner decision D8 (everything not listed:
No access):

* Contents: Read and write -- clone and push
* Pull requests: Read and write -- open and update the task's pull request
* Issues: Read and write -- the issue-run comments (#454)
* Checks: Read-only -- the merge step's CI wait
* Metadata: Read-only -- mandatory, GitHub sets it
* Workflows: No access -- a push that changes `.github/workflows/` needs it,
  and it is the permission org admins refuse most; D8 makes it a separate
  request when a tenant asks for it

**Organization permissions** and **Account permissions**: none. **Subscribe
to events**: none (there is no webhook).

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

Expected: access **granted** for `<slot>` and **denied** for `<slot>-refresh`;
for `swarm-api@` the opposite pair. Any other answer means a condition does
not match the resource-name form IAM evaluates: set
`enable_forge_user_slots = false` and apply bootstrap before anything else.

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
