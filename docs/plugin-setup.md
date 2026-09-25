# Setting up the `sc` plugin against your deployment

The `sc` Claude Code plugin is a **client**, like `gh` or `kubectl`: you point
it at *your* SwarmCloud deployment, and it signs you in there as yourself. It
carries no deployment of its own. Until 2026-09-25 it did — it found
`https://swarm.saga.xyz` by reading this repository's Terraform variables, so
everyone who installed it was pointed at Saga's cluster — and this page
describes what replaced that.

## What you need from your operator

Three values, once per deployment:

| | What it is | Looks like |
|---|---|---|
| **Deployment URL** | the address you reach the platform at | `https://swarm.example.com` (a team deployment's load balancer) or `https://swarm-api-….run.app` (a solo one) |
| **OAuth client ID** | the deployment's one *Desktop app* OAuth client | `123456789-abc….apps.googleusercontent.com` |
| **OAuth client secret** | that client's secret | `GOCSPX-…` |

A solo deployment, reached through Cloud Run with no IAP, has no OAuth client;
leave the last two empty. The plugin then sends your own gcloud identity
(`gcloud auth print-identity-token`) to the `*.run.app` address, which is
what Cloud Run documents for a developer and accepts from an account holding
`run.routes.invoke`, i.e. `roles/run.invoker` on the service
([Test private services](https://docs.cloud.google.com/run/docs/authenticating/developers)).
That token has no audience of its own, so Google calls it a development path;
the bridge sends it to that configured `*.run.app` address and nowhere else.
A team deployment behind IAP needs all three values, and its operator creates
the client once for everybody —
[runbooks/iap-desktop-client.md](runbooks/iap-desktop-client.md) is that
one-time step for Saga's deployment, and the same shape for any other.

## Install

In a Claude Code session:

```text
/plugin marketplace add bogdan-alexandrescu/SwarmCloud
/plugin install sc@swarmcloud
```

The plugin's MCP server fetches the bridge from GitHub at the tag
`sc-v<version>`, where `<version>` is the plugin's own version
([plugin/README.md](../plugin/README.md) has the declaration and the release
step). **Until that tag is pushed for the version you installed, the server
cannot start** — `/mcp` shows `plugin:sc:swarmcloud` failed, uv says it cannot
find the ref, nothing you configured reaches a running bridge, and `sc login`
answers that no deployment is configured. Meanwhile, start Claude Code with
`SWARM_MCP_FROM=<checkout>/apps/swarm-mcp claude` to run a checkout's bridge
instead.

The install dialog asks for the three values above. Claude Code keeps the URL
and client ID in your user `settings.json` (under `pluginConfigs`) and the
**secret in your system's secure credential store**, because the manifest marks
it `sensitive` ([plugins reference](https://code.claude.com/docs/en/plugins-reference),
"User configuration"). Then:

```text
/reload-plugins
/mcp                # plugin:sc:swarmcloud should say connected
```

and sign in, in a terminal:

```bash
uv run sc login
```

A browser opens on Google's sign-in page; choose your work account. The page
then says "Signed in" and the terminal prints who you are and your tenant.
That last line comes from one call to the API made **with the sign-in just
created**, whatever else this machine has, so it is a real test that the
deployment admits you. If `SWARM_IMPERSONATE_SA`, `SWARM_ID_TOKEN` or a GCP
metadata server (Cloud Shell, Workstations) is present, `sc login` also warns
that every other command and tool on this machine still acts as that identity
and not as you — they outrank a sign-in, on purpose, for CI.
That is the last time you do this on this machine until you sign out: the
refresh token is kept, and each new ID token is minted from it silently.

`sc` is a console script of this repository's `swarm-mcp` package, so outside a
checkout spell it `uv run --directory <path to a checkout> sc login`, or run it
from the same package the plugin's server fetches:
`uv tool run --from 'swarm-mcp @ git+https://github.com/bogdan-alexandrescu/SwarmCloud@sc-v<version>#subdirectory=apps/swarm-mcp' sc login`.

To change any of the three values later: `/plugin configure sc@swarmcloud`.
The shell command `claude plugin install` never prompts; pass
`--config deployment_url=https://…` there instead.

## What `sc login` does, and what it stores

It runs Google's documented sign-in for a command-line tool
([Authenticate from a desktop app](https://cloud.google.com/iap/docs/authentication-howto)):
the browser redirects to a listener on `127.0.0.1`, the one-time code is
exchanged for tokens, and the **ID token** — whose audience is the Desktop
client — is what the plugin sends to the deployment. The deployment's IAP
admits it because that client is on its programmatic-client allowlist, and the
API then sees **you**, not a shared service account. Two protections beyond
Google's example: a random `state` checked on the callback, and PKCE, so a code
intercepted on the way to the listener is useless without the verifier.

| What | Where | Why there |
|---|---|---|
| deployment URL, client ID | the config file below | not secret; a terminal must find them |
| client secret | the credential store | per deployment, needed to refresh |
| your refresh token | the credential store | per person; `sc logout` revokes it |
| each ID token | this process's memory only | an hour-long bearer credential |

The **credential store** is the macOS Keychain (service `swarmcloud`), the
Secret Service on Linux (GNOME Keyring, KWallet), or — where neither exists —
a `0600` file next to the config file, which `sc whoami` says plainly. Force a
choice with `SWARM_CREDENTIAL_STORE=keychain|secret-service|file`.

The **config file** is `config.json` in `$XDG_CONFIG_HOME/swarmcloud` if that
is set, else `~/Library/Application Support/SwarmCloud` on macOS, else
`~/.config/swarmcloud`. It never holds a secret. `SWARM_CONFIG_DIR` moves it.

## More than one deployment

Each deployment is a **context**, and one is current — the `kubectl` model:

```bash
printf '%s\n' "$SECRET" | uv run sc context add staging \
  --url https://swarm.staging.example.com \
  --client-id 123-abc.apps.googleusercontent.com \
  --client-secret-stdin
uv run sc context list            # * marks the current one
uv run sc context use staging
uv run sc login                   # sign-in is per context
uv run sc --context prod accounts # one command against another context
uv run sc context remove staging  # also forgets its secret and your sign-in
```

The secret is only ever read from stdin, never taken as an argument: argv is
what `ps` and your shell history keep.

The plugin's configured deployment is **seeded** into this file as a context
(named after its host, e.g. `swarm.example.com`) the first time its MCP server
starts, so `sc login` in a plain terminal can find it. It becomes the current
context only if nothing is current: a background process moving your terminal
to another cluster is how a dispatch lands in the wrong place.

## Which deployment wins

Most explicit first:

1. `--context NAME` on the command line
2. `SWARM_URL` (with `SWARM_OAUTH_CLIENT_ID` / `SWARM_OAUTH_CLIENT_SECRET`),
   then the older `SWARM_API_URL`, `API_URL`, `SWARM_API_HOST`, `API_HOST`
3. `SWARM_CONTEXT=NAME`
4. developer mode, below
5. the plugin's configuration (inside Claude Code's MCP server)
6. the current context

`uv run sc whoami` prints which of these answered, and `uv run swarm doctor`
prints it next to the auth tier.

## In CI

Set the deployment in the environment and authenticate as a service account;
there is no browser to sign in with:

```bash
export SWARM_URL=https://swarm.example.com
export SWARM_IMPERSONATE_SA=ci-runner@my-project.iam.gserviceaccount.com
```

Impersonation outranks sign-in whenever `SWARM_IMPERSONATE_SA` is set, and that
service account needs `roles/iap.httpsResourceAccessor` on the deployment's IAP.

## Developer mode: reading this repository

Working **inside** this repository, the owner may want the old behaviour — the
deployment named by `terraform/environments/<env>/<env>.tfvars`. It is off
unless asked for:

```bash
export SWARM_MCP_CONFIG_FROM=repo   # and ENVIRONMENT=dev|prod as before
```

Nothing else reads Terraform files. The bridge's tests watch every file the
process opens and fail if a `.tfvars` file is opened outside this mode.

## When it does not work

| What you see | What it means | What to do |
|---|---|---|
| `sign-in required for <context>: run sc login` | this deployment takes a signed-in developer, and you are not one | run it; a browser opens |
| `no SwarmCloud deployment is configured` | nothing in any of the six places above | `sc context add`, or install the plugin |
| `sc login` succeeds, then `did not accept it: ... 401` | IAP does not have this Desktop client on its allowlist | the operator's step, [runbooks/iap-desktop-client.md](runbooks/iap-desktop-client.md) |
| 403 that names you | you are signed in and IAP does not list you as an accessor | the operator adds you (or your domain) to the IAP accessor list |
| `no OAuth client secret for <context>` | the secret was never given on this machine | `/plugin configure sc@swarmcloud`, or `sc context add … --client-secret-stdin` |
| `sc login` succeeds with `warning … not as you` | a service-account identity outranks your sign-in here | unset the variable it names, or run from outside GCP |
| `/mcp` shows `plugin:sc:swarmcloud` failed | the tag `sc-v<version>` for the installed version is not pushed yet, or `uv` is not on the `PATH` Claude Code started with | push the tag ([plugin/README.md](../plugin/README.md), "Releasing a version"), or start with `SWARM_MCP_FROM=<checkout>/apps/swarm-mcp` |

## Worked example: Saga's `dev` deployment

| Value | |
|---|---|
| Deployment URL | `https://swarm.saga.xyz` |
| OAuth client ID | created by the owner per [the runbook](runbooks/iap-desktop-client.md); ask for it |
| OAuth client secret | the team password manager |
| Who may pass IAP | `domain:saga.xyz` (`frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`) |

These are an example of values an operator hands out, not defaults: nothing in
the plugin or the bridge names them.
