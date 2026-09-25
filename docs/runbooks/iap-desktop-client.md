# One Desktop OAuth client, so developers sign in to IAP as themselves

**Who:** the deployment's owner, once per deployment. **Takes:** about fifteen
minutes, plus up to ten for IAM to propagate. **Changes:** one OAuth client in
the project, and one IAP setting on each of the platform's own two backend
services. Nothing on the other team's backends.

The worked values below are Saga's `dev` deployment (`saga-agents-staging`,
project number `209012342332`, `https://swarm.saga.xyz`, backends
`swarm-ui-backend` and `swarm-ui-ui-backend`). Another deployment substitutes
its own.

## Why this is needed

A gcloud user token is refused at the front door: measured on 2026-09-24,
IAP answers **401 with error code 900**. The deployment's IAP uses a
Google-managed OAuth client (`terraform/modules/frontend` sets no
`oauth2_client_id`, on purpose, so no client secret sits in Terraform state),
and Google's documentation says what that means:

> Google-managed OAuth clients cannot programmatically access IAP-protected
> applications. However, IAP-protected applications that use the
> Google-managed OAuth client can still be accessed programmatically using a
> separate OAuth client configured through the `programmatic_clients` setting
> or a service account JWT.
> — [Use custom OAuth clients with IAP](https://cloud.google.com/iap/docs/custom-oauth-configuration)

Until now the only way through from a laptop was impersonating a service
account, so every developer reached the API as `swarm-verify` rather than as
themselves. Google's documented path for a person at a command line is
[Authenticate from a desktop app](https://cloud.google.com/iap/docs/authentication-howto):
create a **Desktop app** OAuth client, add it to the application's allowlist
for programmatic access, and have each developer sign in with it. `sc login`
is that sign-in. One client serves every developer on the deployment; nobody
else creates anything.

The allowlist is set **per backend service**, not per project. That is the
line this runbook must not cross: nine of the ten IAP backends in
`saga-agents-staging` are another team's (their Keycloak and ArgoCD among them),
and a project-level IAP setting would change what they accept.

## Step 1 — look before touching anything (read-only)

```bash
gcloud iap oauth-brands list --project=saga-agents-staging \
  --format='table(name,applicationTitle,orgInternalOnly)'
```

Expected, as measured on 2026-09-25:

```text
NAME                                       APPLICATION_TITLE  ORG_INTERNAL_ONLY
projects/209012342332/brands/209012342332  AI Agents          True
```

**The consent screen already exists and is Internal. Do not edit it.** It is the
project's, shared with the other team, and "Internal" is the setting this needs:
only accounts in the organisation can sign in with a client under it. Developers
will see **"AI Agents"** as the application name when they sign in; that is this
brand's title, not a mistake. (A fresh project with no brand configures one in
the console: **Google Auth Platform → Branding**, then **Audience → Internal**.)

```bash
gcloud compute backend-services describe swarm-ui-backend --global \
  --project=saga-agents-staging --format='value(iap.enabled,iap.oauth2ClientId)'
```

Expected: `True` and nothing after it — IAP on, Google-managed client. If a
client id is printed, this deployment has its own IAP client and this runbook
does not apply; `SWARM_IAP_CLIENT_ID` with `SWARM_IMPERSONATE_SA` is that shape.

## Step 2 — create the Desktop OAuth client (console)

1. Open <https://console.cloud.google.com/auth/clients?project=saga-agents-staging>
   (**Google Auth Platform → Clients**; older consoles call it **APIs &
   Services → Credentials → Create credentials → OAuth client ID**).
2. **Create client**.
3. **Application type: Desktop app.** Not "Web application": the sign-in
   redirects to a listener on `127.0.0.1`, which only a Desktop client allows
   without registering redirect URIs.
4. **Name:** `SwarmCloud sc CLI`. Only you see it.
5. **Create.** The dialog shows two values. Copy both now:
   * **Client ID** — `209012342332-….apps.googleusercontent.com`. Not secret;
     it goes into Terraform in step 4 and into every developer's plugin.
   * **Client secret** — `GOCSPX-…`. Keep it in the team's password manager.
     Google documents that an installed app's secret is not treated as
     confidential, but it is still not something to paste into a public
     channel or a repository — and it never goes into Terraform.

Nothing is live yet: until step 4, IAP refuses tokens minted for this client.

## Step 3 — the permission to write IAP settings on our backends

Writing IAP settings needs `iap.webServices.updateSettings`, and every plan
reads them with `iap.webServices.getSettings`. Measured on 2026-09-25:

* both permissions are in `roles/iap.settingsAdmin` and **not** in
  `roles/iap.admin` (`gcloud iam roles describe` on each);
* `bogdan@saga.xyz` holds `roles/iap.admin` on the project, and
  `gcloud iap settings get --project=saga-agents-staging --resource-type=compute --service=swarm-ui-backend`
  answers `PERMISSION_DENIED ... iap.webServices.getSettings`.

So the applier needs `roles/iap.settingsAdmin`, and it must be granted **on the
two backend services only**. `roles/iap.admin` includes
`iap.webServices.setIamPolicy`, so the owner can grant it to themselves there:

```bash
for backend in swarm-ui-backend swarm-ui-ui-backend; do
  gcloud iap web add-iam-policy-binding \
    --project=saga-agents-staging \
    --resource-type=backend-services --service="${backend}" \
    --member=user:bogdan@saga.xyz \
    --role=roles/iap.settingsAdmin
done
```

Then check it took, which may need a few minutes:

```bash
gcloud iap settings get --project=saga-agents-staging \
  --resource-type=compute --service=swarm-ui-backend
```

It must print settings (possibly just a `name:` line), not `PERMISSION_DENIED`.

**Not verified:** that IAP honours `roles/iap.settingsAdmin` bound at the
backend-service level for these two permissions. The denial above names the
service-level resource, which suggests it will, but this repository has measured
the opposite once: `roles/iap.admin` bound on the backends still left the
deployer's `getIamPolicy` refused nine and a half minutes later
(`terraform/modules/frontend/main.tf`, "Who may pass IAP"). If `settings get` is
still denied after ten minutes, **stop**. The remaining option is
`roles/iap.settingsAdmin` on the **project**, granted by a project IAM admin —
which would let its holder change IAP settings on all ten backends, the other
team's included. That is a decision for the owner and the other team, not a
step in this runbook.

## Step 4 — allowlist the client (Terraform, bootstrap root)

Add the client ID from step 2 to `terraform/bootstrap/terraform.tfvars`:

```hcl
# Desktop OAuth client that `sc login` signs developers in with
# (terraform/bootstrap/iap_programmatic_clients.tf).
frontend_iap_programmatic_clients = [
  "209012342332-XXXXXXXX.apps.googleusercontent.com",
]
```

The variable refuses anything that is not a client ID — a pasted secret
(`GOCSPX-…`) or a URL fails the plan. Then apply only these two resources,
because `make bootstrap` applies everything pending in the root
([docs/ci.md](../ci.md) says why that matters):

```bash
scripts/bootstrap.sh \
  --target 'google_iap_settings.frontend_programmatic_clients["swarm-ui-backend"]' \
  --target 'google_iap_settings.frontend_programmatic_clients["swarm-ui-ui-backend"]'
```

Before typing `apply`, the plan must show exactly these two creates and nothing
else, each named at the **service** level:

```text
+ google_iap_settings.frontend_programmatic_clients["swarm-ui-backend"]
    name = "projects/209012342332/iap_web/compute/services/swarm-ui-backend"
+ google_iap_settings.frontend_programmatic_clients["swarm-ui-ui-backend"]
    name = "projects/209012342332/iap_web/compute/services/swarm-ui-ui-backend"
```

A `name` ending anywhere other than `/services/swarm-…` is the project-wide
change this runbook exists to avoid. Do not apply it.

## Step 5 — verify

```bash
gcloud iap settings get --project=saga-agents-staging \
  --resource-type=compute --service=swarm-ui-backend
```

Expected:

```yaml
accessSettings:
  oauthSettings:
    programmaticClients:
    - 209012342332-XXXXXXXX.apps.googleusercontent.com
```

Then sign in as a developer would, with the values from step 2 (the
[plugin setup guide](../plugin-setup.md) has the full flow):

```bash
printf '%s\n' 'GOCSPX-…' | uv run sc context add saga-dev \
  --url https://swarm.saga.xyz \
  --client-id 209012342332-XXXXXXXX.apps.googleusercontent.com \
  --client-secret-stdin
uv run sc login      # a browser opens; pick your saga.xyz account
uv run sc whoami     # principal is YOU, and your tenant
```

`sc login` ends by calling the API once, **with the ID token it just minted** —
not with whatever else this machine has — so its answer is about the
allowlist. (Until review caught it, that call went through the ordinary tier
detection, and with `SWARM_IMPERSONATE_SA` exported it tested the service
account instead and printed success whatever IAP thought of the Desktop
client.) If `sc login` prints a `warning … not as you` line, the check above is
still valid, but every other command on this machine will act as the identity
it names until you unset it.

If it prints `did not accept it: ... 401`, IAP is refusing the token: step 4 is
not applied, or has not propagated.
A **403 that names you** is the other gate — `roles/iap.httpsResourceAccessor`,
granted through `frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`
(`domain:saga.xyz` already covers every Saga account).

## Step 6 — tell developers

Three values, and where to put them: the plugin asks for all three at
`/plugin install sc@swarmcloud`. The plugin's MCP server fetches the bridge at
the tag `sc-v<version>`, so push that tag for the plugin version you hand out
before anyone installs it, or their server cannot start
([the plugin setup guide](../plugin-setup.md) says what they see).

| Prompt | Value |
|---|---|
| SwarmCloud deployment URL | `https://swarm.saga.xyz` |
| OAuth client ID (Desktop app) | the client ID from step 2 |
| OAuth client secret (Desktop app) | from the password manager |

## Undoing it

Set `frontend_iap_programmatic_clients = []` and apply the same two targets:
the provider clears the IAP settings on those two backends when the resources
are destroyed. Deleting the OAuth client in the console (step 2's page) also
invalidates every developer's refresh token at once, which is the fast way to
cut everyone off; `sc logout` does the same for one person.
