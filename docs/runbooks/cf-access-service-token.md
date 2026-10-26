# Let browser agents reach a site behind Cloudflare Access with a service token

CF-ACCESS-TOKEN, part of #2442. Decided by BGD on 2026-10-26 (08:5xZ), at the
AI Studio session's request. For the **workspace admin** who owns a site
behind Cloudflare Access and wants `claude-code-browser` steps (visual QA,
rendered review) to open it. The first site is the Galatia platform admin:
staging `staging-platformadmin-agents.pages.dev`, production
`platform.galatia.saga.xyz`.

Design: [docs/design/workspace-secrets.md](../design/workspace-secrets.md),
"The cf-access slot". Code: `apps/agent-worker/agent_worker/livegate/cfaccess.py`
(the gate), `apps/agent-worker/agent_worker/cf_access.py` (the worker),
`apps/swarm-api/swarm_api/workspace_secrets.py` (`HOST_SCOPED_SLOTS`, `slot_hosts`).

## Why it exists

Cloudflare Access puts a sign-in, usually with a second factor, in front of
every request. A person passes it once in their browser. An agent cannot: it
has no phone, no security key and no inbox, and SwarmCloud never hands it a
person's sign-in. Cloudflare's own answer for machines is a **service
token**: a client id and a client secret, sent as two request headers,
`CF-Access-Client-Id` and `CF-Access-Client-Secret`, which an Access policy
admits through a **Service Auth** rule instead of a sign-in.

## What a service token is, and what it opens

* It is a pair of values Cloudflare generates: a **Client ID** (ending
  `.access`) and a **Client Secret**. Cloudflare shows the secret **once**,
  when the token is made.
* It opens exactly what the Access application's policy lets it open: the
  applications whose policies carry a Service Auth rule naming it, and
  nothing else in your account. It is not a Cloudflare API token and cannot
  change any setting.
* **It is not read-only by itself.** Access decides who gets in, not what they
  may do once inside. The token gets every method the site serves to an
  admitted request. Read-only is the site's own rule, or the QA target's
  read-only guard when the step runs against a QA target
  ([visual-qa-any-app.md](../design/visual-qa-any-app.md) §3). For the platform
  admin, BGD approved read-only access: give the token to a policy on an
  application that serves read-only views, or rely on the visual-QA steps
  that use it being read-only.
* It expires on the date you choose when you make it (one year by default).
  Cloudflare can email before it does.

## What SwarmCloud does with it

* The token is a **workspace secret** of kind **OAuth client** (`client_id`,
  `client_secret`), granted to **`profile:claude-code-browser`** in its
  **`cf-access`** slot, with the **hosts** it may be sent to set on that
  grant: exact host names, at most 16, no wildcard. swarm-api signs the list
  with the grant (attestation form "v3"), so an agent that writes Firestore
  cannot add a host.
* When a `claude-code-browser` step of that workspace starts, the worker
  receives the token from swarm-api, **never writes it to a file**, and hands
  it to the live gate before the agent exists.
* The gate adds the two headers **only** to https requests whose host is on
  the list, from a page whose top frame is on the list. Every other request,
  a redirect included, gets neither; a request that somehow carries a value
  to another host has it removed.
* The agent never reads the values: they are not in its environment, prompt,
  files or tool results. Every message the browser sends the agent is
  scrubbed of them, and the worker's redaction filter knows them.
* A workspace with no `cf-access` grant sends nothing, and the browser reaches
  the site signed out (Access's sign-in page).

## 1. Make the token (Cloudflare Zero Trust)

1. Open the [Cloudflare dashboard](https://dash.cloudflare.com), then **Zero
   Trust**.
2. Go to **Access › Service Auth › Service Tokens** and choose **Create
   Service Token**. (Newer dashboards file it under **Access controls ›
   Service credentials › Service Tokens**; it is the same page.)
3. Name it for what uses it, for example `swarmcloud-visual-qa`, and choose a
   duration. Choose **Generate token**.
4. Cloudflare shows the **Client ID** and the **Client Secret**. The secret is
   shown **once**. Keep the page open until step 3 below is done. Do not
   paste either value into a chat, an issue, a pull request, a prompt, a
   terminal history or this repository.

## 2. Let the token through the Access policy

For **each** Access application the agents must open (here: the staging and
the production platform admin):

1. **Access › Applications**, the application, **Edit**, then **Policies**.
2. Add a policy, or edit the one meant for automation:
   * **Action: Service Auth.** (Not *Allow*: an Allow policy with a service
     token still sends a person to the sign-in page.)
   * **Include: Service Token**, the token you made in step 1.
3. Save. Leave the people's policies as they are.

Check it from your own machine. The values are read from the terminal and
handed to curl on stdin (`-K -`), so they reach neither the command line, the
shell history nor `/proc/<pid>/cmdline`:

```bash
read -r -p 'Paste the Client ID and press Enter ' CF_ID
read -r -s -p 'Paste the Client Secret and press Enter ' CF_SECRET; echo
printf 'header = "%s: %s"\n' CF-Access-Client-Id "$CF_ID" CF-Access-Client-Secret "$CF_SECRET" \
  | curl -sS -K - -o /dev/null -w '%{http_code}\n' https://staging-platformadmin-agents.pages.dev/
unset CF_ID CF_SECRET
```

`200` (or the site's own redirect) means the policy admits the token; a `302`
to `*.cloudflareaccess.com` or a `403` means it does not.

## 3. Store it in the workspace, with its hosts

You need `admin` in the workspace whose agents will open the site (eng, for
the AI Studio lanes).

1. **Store the value.** Console › **Settings › Workspace › Secrets › Add**
   (or mint a one-time link to that form with `swarm_secret_set_link`):
   * name: `platform-admin` (lower case, digits, hyphens);
   * provider: **other**;
   * kind: **OAuth client**;
   * paste the **Client ID** into `client_id` and the **Client Secret** into
     `client_secret`. The form is the only place the value is typed.
2. **Grant it to the browser runner, with its hosts.** `claude-code-browser`
   declares the `cf-access` slot (contract request 117). The console has no
   host-list field yet, so set the grant from a terminal with
   `scripts/api.sh`, which calls swarm-api as you without putting your token
   in argv (the body carries no secret, only host names), as the workspace's
   admin:

   ```bash
   scripts/api.sh PUT /workspace/secrets/platform-admin/grants/profile:claude-code-browser \
     '{"enabled": true, "slot": "cf-access",
       "hosts": ["staging-platformadmin-agents.pages.dev", "platform.galatia.saga.xyz"]}'
   ```

   The answer lists the grant with its `hosts`, sorted. A host with a scheme,
   a port, a path, a wildcard or an IP address is refused, and so is an empty
   list. `example.com` does not cover `www.example.com`: list each host the
   site's pages and its API are served from.

   **Do not tick this secret's `profile:claude-code-browser` cell in the
   console's grant matrix.** The matrix has one cell per consumer, and for a
   consumer with two slots it writes the first that takes the kind, which
   for an OAuth client is `files`. A secret has one grant per consumer, so
   the tick replaces the `cf-access` grant with a `files` one, and the
   `files` slot writes the value to a file the agent reads. If it happens,
   send the `PUT` above again, then rotate the token (below): the agent of
   any attempt that started in between could read it.
3. Close the Cloudflare page. The value now lives only in Secret Manager.

The grant takes effect at the next attempt. A running attempt keeps what it
was handed.

## Check it worked

Run a `claude-code-browser` step in the workspace that opens a listed host.

* The page renders the site, not Access's sign-in.
* The worker's log says `Cloudflare Access token handed to the gate` with the
  secret's name and its hosts, and nothing else about it. The gate's `status`
  (`swarm-handoff status`) shows `cf_access: {state: held, hosts: [...],
  added: N}`.
* Neither value appears in the step's transcript, tool results, artifacts or
  logs. If one ever does, treat it as an S0 (secrets): rotate the token
  (below) and file the issue by **where** it appeared, never pasting it.

If the browser sees the sign-in page instead, the worker's log names why:
`no_gate` (the step's profile has no live browser), `unprotected`,
`malformed` (the secret is not an OAuth client with both fields), or
`gate_refused`; or the policy does not admit the token (step 2's check).

## Rotate, change the hosts, or remove

* **Rotate.** In Cloudflare, **Refresh** the token (or make a new one and add
  it to the Service Auth rule), then paste the new pair through the console's
  **Rotate** on `platform-admin`. The old version stays usable for 24 hours,
  so running attempts finish; then remove the old token from the policy.
* **Change the hosts.** Send the grant again with the new `hosts`. It is
  signed again; the next attempt uses the new list.
* **Remove.** Untick the grant (`"enabled": false`), or delete the secret
  with its typed confirmation. Then delete the service token in Cloudflare,
  which is what actually stops it opening the site.
