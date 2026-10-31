# Install SwarmCloud

This page takes you from nothing to a SwarmCloud deployment that you, or your
team, use the same day. It is the whole procedure. You need one terminal and
the console in a browser, and you do three steps:

```bash
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
swarm install
```

The third step is the console's **Admin › Setup**, which `swarm install`
opens for you.

The installer asks you for three kinds of thing, and nothing else:

- **an approval**: a typed confirmation, a cost, or an IAM or admin-role grant;
- **a credential or a sign-in**;
- **an optional choice**, where Enter keeps the proposed default.

Some steps have no Google, GitHub or Claude API, so they happen on a web page
instead. The installer opens the exact page, says what to do there, and watches
for the change. You never press Enter to say you are done. You never run
another script, copy a command out of a document, or give Claude or any other
agent access to your project. [Steps outside the terminal and the
console](#steps-outside-the-terminal-and-the-console) lists every such page.

Releases come from SwarmCloud's release home:

- the project `swarmcloud-prod`;
- the bucket `gs://swarmcloud`, served as
  `https://storage.googleapis.com/swarmcloud`;
- the Artifact Registry repository
  `us-docker.pkg.dev/swarmcloud-prod/swarmcloud`.

Stage 4 copies the images into your project. After that, nothing in your
deployment reads the release home again.

## Before you start

You need:

- **A Google organisation**, on Google Workspace or the free Cloud Identity,
  on a domain you own. If the account you sign in with has none, for example a
  gmail.com account, stage 0 opens the Cloud Identity Free sign-up and waits
  for the organisation to appear. That usually takes 15 to 60 minutes, because
  Google verifies the domain by a DNS record. You then sign in again as the
  new organisation's admin. Stage 8 creates the deployment's groups and
  assigns swarm-api the **Groups Reader** admin role, so you must be an admin
  who can do both. If you cannot, a super admin can take that one step on the
  page the installer opens.
- **Rights in Google Cloud.** Stage 0 checks three rights and names the role
  for any you lack. It never grants a right to you.
  - To create a project you need `resourcemanager.projects.create` on the
    organisation or folder (Project Creator).
  - To link billing you need `billing.resourceAssociations.create` on the
    billing account (`roles/billing.user`).
  - For an existing project you need `iap.webServices.updateSettings`.
- **A billing account.** If you have none, stage 0 opens the page that
  creates one and waits for it.
- **A GitHub organisation** that the deployment will serve, where you can
  create and install a GitHub App.
- **A hostname you control**, for example `swarm.example.com`. If its zone is
  in the project's Cloud DNS, the installer sets the record itself after you
  approve. Otherwise you set one `A` record at your DNS provider.
- **Claude subscriptions** for the agents to run on. Each subscription's owner
  signs in once, in the console.
- **A terminal on macOS or Linux** with `curl`, `tar`, and either `sha256sum`
  or `shasum`.
  - `install.sh` installs uv if it is missing. uv needs Python 3.11 or newer.
  - Stage 0 fetches pinned, checksummed copies of Terraform, kubectl, jq,
    cosign, gcrane and the GKE auth plugin.
  - If the Google Cloud SDK is missing, stage 0 offers to install it.

### What it costs

Before stage 1 creates a project or links billing, it shows the fixed monthly
cost at Google's list price. This is what the deployment costs while nobody
uses it:

| Line | USD a month |
|---|---|
| swarm-api's two always-on instances, 1 vCPU / 1 GiB each | 105 |
| the GKE cluster's management fee, $0.10 an hour | 73 |
| the HTTPS load balancer's forwarding rule, $0.025 an hour | 18 |
| **floor, before any agent runs** | **about 196** |

Agent runs are billed on top, by the minute they run
([cost-control.md](cost-control.md)).

Stage 1 also sets a monthly budget, with alerts at 50, 90 and 100 %. Stage 0's
sizing proposes the amount:

- one person: 500 USD;
- a team: 300 USD plus the expected agents' run time, rounded up to the next
  hundred.

You can type any other whole number of dollars above 0.

## Step 1: install the command line

```bash
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
```

The script installs the release that `channels/stable` names in
`gs://swarmcloud`. To install the current release candidate instead, which
`channels/rc` names:

```bash
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | SWARM_CHANNEL=rc sh
```

To pin one release, set `SWARMCLOUD_VERSION=<version>` (for example
`2026.10.1`). The script refuses any channel other than `stable` or `rc`, and
any version that does not start `YYYY.MM.` and a digit, such as `2026.10.1`
or a release candidate's `2026.10.1-rc.1`.

What the script does:

1. It checks each wheel it downloads against the release's `SHA256SUMS`. A
   mismatch installs nothing. The installer itself later checks the signature
   on `SHA256SUMS`, using the release key built into it.
2. If uv is missing, it installs uv into `~/.local/bin` from a pinned,
   checksummed download.
3. It installs one uv tool, which provides the `swarm` and `sc` commands.
4. If the tool's directory is not on your `PATH`, it adds one line to your
   shell's rc file, ending `# swarmcloud: swarm on PATH`.

It prints `swarmcloud: installed SwarmCloud <version>: swarm and sc`, and
ends with the command to run next:

```
Next: swarm install
```

If this terminal cannot find `swarm` yet, that line gives its full path
instead, for example `~/.local/bin/swarm install`.

## Step 2: `swarm install`

```bash
swarm install
```

The installer asks:

```
? Google Cloud project to create or use:
? Domain for the console (a hostname you control):
? Region [us-central1]:
```

The project can be a new project id or one that already exists. The domain and
region are asked only if the install has not recorded them already. Then it
shows a summary and asks to go on:

```
SwarmCloud will be installed into Google Cloud project <project> (prod):
  Project:   <project> (created if it does not exist)
  Hostname:  <hostname>
  Region:    <region>
  Release:   <release>
  Console:   https://<hostname>/ when the install is done
Continue? [Y/n]
```

Any answer other than Enter, `y` or `yes` prints
`Stopped; nothing was changed.`

You can give the same answers as flags instead:

- `--project`, `--domain` and `--region`;
- `--version`, to pin a release;
- `--env`, to name the environment: `dev`, `staging` or `prod` (default
  `prod`).

Questions with a default show it in brackets, and Enter takes it.

Most of the install's time is spent waiting on Google and on you. While the
installer waits, it prints one line on each poll:

```
  waiting on <step>: <what it waits for> -- <N> s left of the bound (usually <how long>)
```

You can leave it running. If a wait runs past its bound, the installer prints
where to look and pauses (exit 4). Run `swarm install` again to pick up where
it stopped.

### Stage 0: preflight

Stage 0 reads and asks. It creates nothing in Google Cloud. Its steps run in
this order.

1. **Tools.** The installer fetches and checks its pinned tools. A tool with
   the wrong version already on your `PATH` is ignored. If the Google Cloud
   SDK is missing, it asks
   `Install the Google Cloud SDK into ~/.swarm/google-cloud-sdk? [Y/n]`.
   - If you answer no, it opens the SDK's install page and stops. Run
     `swarm install` again once the SDK is installed.

2. **Sign-in.** If gcloud has no signed-in account, the installer runs
   `gcloud auth login`. If there are no application-default credentials with
   the scopes the install needs, it runs
   `gcloud auth application-default login`.
   - The scopes are Cloud Platform plus the Directory scopes
     `admin.directory.customer.readonly` and
     `admin.directory.rolemanagement`.
   - Each sign-in opens Google's sign-in page in your browser. Sign in as the
     Workspace or Cloud Identity admin who is installing. That account becomes
     the install's owner and the first platform admin.
   - If Google says the app is blocked, press Ctrl-C, or wait 5 minutes. The
     installer then signs in with Cloud Platform alone, and stage 8 asks for
     the Directory scopes later, through the deployment's own client.

3. **The organisation.** If your account can see no organisation, the
   installer opens `https://workspace.google.com/signup/gcpidentity/welcome`.
   Sign up for Cloud Identity Free with your domain, and verify the domain
   with the DNS record that page shows.
   - If you have no billing account yet, the installer also opens
     `https://console.cloud.google.com/billing/create`. Google creates the
     organisation when the domain's account first creates a billing account or
     a project.
   - From a gmail.com account, you then sign in as the new organisation's
     admin in the browser window that opens next.
   - The installer waits for *a Google organisation appearing*: usually 15 to
     60 minutes, for up to 3 hours.

4. **Your answers.** It asks only for what it does not already know:
   - `Email address that receives SwarmCloud's alerts (required: alerts that reach nobody were lesson 41):`
   - `Hostname the deployment will answer on (its A record is set in stage 2):`
     This is asked only if you did not give the hostname already.
   - `GitHub organisation login the deployment serves (github.com/<login>):`
   - `Billing account to link the project to:` This is a numbered list of
     your open accounts. It is skipped if the project already has billing.
     - If you have no billing account, the installer opens
       `https://console.cloud.google.com/billing/create` and waits for one to
       appear: usually 5 to 15 minutes, for up to an hour.

5. **Region, confirmed by typing it.** The installer asks
   `Region for every regional resource [us-central1]:` if you have not given
   one. Then, because the Firestore location can never change, it asks:
   `The Firestore location can never change. Type the region again to confirm (<region>):`
   Anything else records nothing.

6. **Where the project goes, and your rights.** If the project does not exist
   yet, it asks `Project <id> does not exist yet. Create it under:`. Pick a
   number from the organisations and folders where you can create projects.
   It then checks your rights and names any role you are missing.

7. **Your organisation's domain.** The installer reads the domain, for
   example `example.com`, from the organisation. It asks
   `Your Google Workspace or Cloud Identity domain (IAP admits every account in it) [<your domain>]:`
   only if it cannot read the domain. A consumer domain such as gmail.com is
   refused.

8. **This machine's address.** It reads your public IPv4 address from two echo
   services, so that the install can reach the cluster's control plane. It
   asks for the address only if the two disagree or one fails. Stage 14
   removes the address again.

9. **Organisation policies.** It reads the policies the project inherits. A
   resource-locations policy that excludes your region, or a CMEK policy that
   covers a service the deployment uses, stops the install. The message names
   what an admin must change.

10. **Quotas.** For an existing project, it checks the quotas of the services
    already enabled:
    - Cloud KMS requests (at least 600);
    - Cloud Run jobs;
    - CPUs (at least 24, or what your sizing needs);
    - one GKE cluster;
    - one static IP.

    A new project has the default quotas. Its CPU quota is checked again in
    stage 1.

    When a quota is too low, the installer opens the Quotas page, filtered to
    that quota, so that you can request an increase there. On a Free Trial
    billing account it opens the billing page instead, so that you can
    activate the full account. It then waits for the quota: a few minutes, or
    up to 2 business days for a reviewed request, for up to 4 hours.

11. **One person or a team.** It asks:

    ```
    Who is this SwarmCloud for? [1] just me  [2] a team [1]:
    ```

    For a team, it also asks `How many people will use it?:`. It then shows a
    numbered table, `Proposed sizing for one person:` or
    `Proposed sizing for a team of <n>:`, with 19 rows:
    - how many agents may run at once: across the deployment, per workspace,
      per resource class, per runner profile and per backend;
    - the maximum instances of swarm-api, the scheduler, the quota broker and
      the reconciler;
    - `Claude subscription accounts to add`;
    - `monthly budget, USD`.

    The table is capped at your CPU quota. Then it asks:
    `Press Enter to accept it, or type a row number (1-19) to change that one number:`.
    Change as many rows as you like, one at a time, and press Enter to accept.

    A one-person deployment differs from a team's:
    - IAP does not admit your whole domain;
    - there is no team group;
    - your personal workspace is the first workspace.

12. **The release.** The installer reads the release from
    `https://storage.googleapis.com/swarmcloud`. It shows each CVE the release
    accepts, with the date the acceptance expires. An expired acceptance stops
    the install.

### Stage 1: foundations

1. **The approval of cost.** This step runs only if the project must be
   created or linked to billing. It first asks:

   ```
   Monthly budget for <project>, in US dollars (Enter keeps it) [<amount>]:
   ```

   Then it shows what it will do: the project, its parent or billing account,
   the budget, and the cost estimate above. It ends with:

   ```
   Type the project id (<project>) to approve
   ```

   Anything else is refused, and nothing is created or linked. An existing
   project that already has billing is left as it is. For that project, the
   budget question comes at the budget step below instead.
2. **The project.** The installer creates the project, labelled
   `managed-by=swarm-install`, and links the billing account. This usually
   takes under a minute. Three things can stop it:
   - **The Terms of Service.** If Google has not had the Terms of Service
     accepted for your account, the installer opens
     `https://console.cloud.google.com/`. Accept them there. It creates the
     project by itself once Google sees the acceptance: 1 to 5 minutes, for
     up to 30.
   - **A taken project id.** Project ids are global, and a project deleted in
     the last 30 days keeps its id. If the id is taken, the installer asks
     `Project id <project> is taken: ... Type another project id to create (6-30 characters: lowercase letters, digits and hyphens, starting with a letter); every answer given so far is kept`.
   - **A missing right.** If you cannot create the project, the installer
     opens the parent's IAM page. If you cannot link billing, it opens the
     project's billing page. Each page says what to grant or link. The
     installer carries on once it sees the change: 1 to 10 minutes, for up to
     30.
3. **The APIs.** The installer enables the Google APIs the deployment uses:
   1 to 3 minutes, for up to 10.
4. **The budget.** The installer creates the budget `swarm-<project>`, with
   alerts at 50, 90 and 100 %. If you lack the right to create budgets, it
   opens the billing account's Budgets page with the exact values to enter.
5. **An IAM approval.** If the Compute Engine default service account holds
   `roles/editor` on the project, the installer asks
   `Type remove-editor to remove roles/editor from serviceAccount:<number>-compute@developer.gserviceaccount.com`.
6. **The state bucket.** The installer creates `gs://swarm-tfstate-<project>`
   in your region: versioned, with uniform access and public access prevented.
   It moves the install's state there.
7. **Sizing, against the new project's quota.** If the new project's CPU quota
   cannot hold the sizing you accepted, the installer shows the table again,
   capped to the quota, for you to accept.

### Stage 2: the front door

1. **The OAuth brand and the Desktop client.** Google has no API for either.
   - The installer opens `https://console.cloud.google.com/auth/branding?project=<project>`.
     Set the App name to `SwarmCloud` and the support email to your own address
     (or a group you manage; the install's own groups are created only at
     stage 8), then save. The brand can never be deleted.
   - It then opens `https://console.cloud.google.com/auth/audience?project=<project>`.
     Choose **Internal**.
   - Then, on Clients › Create client
     (`https://console.cloud.google.com/auth/clients/create?project=<project>`),
     create a client with the Application type **Desktop app**, named
     `SwarmCloud sc`.
   - Keep the dialog that shows the new client open. Give its id at the
     prompt `The Desktop client's id (ends .apps.googleusercontent.com)`.
   - Then give its secret at the hidden prompt
     `The Desktop client's secret (input hidden):`. Google shows the secret
     only now.

   The secret is stored in Secret Manager, as
   `swarm-iap-desktop-client-secret`. It is also stored in this machine's
   `swarm` credential store, so verify never asks for it. If you resume from
   another machine, that machine asks for the secret once more. A later run
   that finds the id recorded but no secret opens the client's own page,
   where you **Add secret**.
2. **Google-side setup.** The installer creates IAP's service agent and
   reserves the console's global IP address. Nothing is asked.
3. **The DNS record.** The record is:

   ```
   <hostname>.  300  IN  A  <address>
   ```

   - **If the hostname's zone is in the project's Cloud DNS**, the installer
     shows the record and asks
     `Type the hostname (<hostname>) to approve, or manual to set it at your DNS provider yourself`.
     If you type the hostname, the installer writes the record itself.
   - **Otherwise, or if you typed `manual`**, you set the record at your DNS
     provider. The installer prints `Set this A record at your DNS provider:`
     and the record. It then opens a lookup of your hostname
     (`https://toolbox.googleapps.com/apps/dig/#A/<hostname>`) and goes on by
     itself.

   In both cases, it waits until Google Public DNS answers with the reserved
   address and nothing else. This usually takes a few minutes after the record
   is saved, and the installer waits for up to an hour.
4. **The certificate.** Google's managed certificate is attached to the load
   balancer in stage 5. Its wait to become active is in stage 8.

### Stage 3: roles, service accounts and keys

The installer downloads the release's Terraform module and checks it:

1. The signature on the release's `SHA256SUMS` must verify with the release
   key built into the installer.
2. The module must match its line in `SHA256SUMS`.

It then applies the first bootstrap pass as you. This pass creates:

- the custom roles;
- the deployer service account `swarm-tf-deployer`;
- the KMS keys;
- the platform service accounts.

It also lets you act as the deployer. It waits, for up to 10 minutes, for that
grant to take effect.

Every infrastructure pass after this runs as the deployer. The bootstrap
passes run as you.

Any Terraform pass that would destroy something asks first:

```
<pass> would DESTROY <n>: <addresses>. Type the project id (<project>) to approve, anything else refuses:
```

`SWARM_ASSUME_YES` never answers this question. With no terminal to type on,
the pass is refused.

### Stage 4: images

The installer creates your project's Artifact Registry repository,
`swarm-images`. For each image in the release's signed list, it:

1. verifies the image's signature;
2. copies the image by digest from
   `us-docker.pkg.dev/swarmcloud-prod/swarmcloud` to
   `<region>-docker.pkg.dev/<project>/swarm-images`.

Your deployment then runs only from its own copies.

### Stages 5 to 7: the platform

Nothing is asked in these stages, unless a pass would destroy something.

- **Stage 5** applies the infrastructure: Cloud Run services and jobs, Cloud
  Scheduler, Pub/Sub, Firestore, the network, the GKE Autopilot cluster, the
  load balancer and its certificate, and the secret containers. It adds this
  machine's address to the cluster control plane's authorised networks, as
  `swarm-install`. No public range is admitted.
- **Stage 6** generates the platform's keys and stores them in Secret
  Manager. They are never shown. The graph-ticket key's container is made
  later, so stage 8 stores that key.
- **Stage 7** applies the second infrastructure pass and the second bootstrap
  pass. Together they:
  - turn on the keys and the console's GitHub App setup;
  - turn on the code-intelligence service;
  - require the alert channel;
  - admit at IAP the Desktop client that the plugin signs in with.

IAP admits:

- for a team: everyone in your organisation's domain, you, the deployment's
  QA service account, and the deployment's own Desktop client;
- for one person: you, the QA service account and the Desktop client, but not
  the rest of your domain.

swarm-api accepts accounts from your organisation's domain. Who belongs to
which workspace still comes from Google groups.

### Stage 8: the console's address, and identity

1. **The console answering.** The installer waits for the certificate to be
   active and for the console to answer behind IAP: up to 10 minutes, for up
   to an hour.
   - Most of that time is Google provisioning the certificate after DNS is
     right.
   - If it gives up, the installer opens the project's SSL certificates page,
     where the certificate's status says whether DNS or provisioning is
     behind.

2. **Directory sign-in, if needed.** If stage 0's sign-in went on without the
   Directory scopes, the first call to the Directory opens Google's consent
   page for the deployment's Desktop client.

3. **The groups.** The installer asks:

   ```
   Create swarm-admins@<domain> and swarm-team@<domain> through the Groups API, you as owner? (yes/no) [yes]:
   ```

   It names only the groups that do not exist yet, and you become each
   group's owner and only member. A one-person deployment has only
   `swarm-admins@<domain>`. If you answer no, or Google refuses, the
   installer opens `https://admin.google.com/ac/groups` and names the groups
   to create there. It carries on once they exist: usually 1 to 5 minutes
   after they are saved, for up to 45.

4. **An admin-role approval.** swarm-api needs to read group membership, and
   nothing else. The installer asks:

   ```
   Assign the Groups Reader admin role to swarm-api@<project>.iam.gserviceaccount.com? It lets swarm-api read the membership of every group in your organisation, and nothing else. Type ASSIGN to approve, or delegation for the fallback:
   ```

   If Google refuses the assignment, the installer opens
   `https://admin.google.com/ac/roles` and names the path: Groups Reader ›
   Admins › Assign service accounts › swarm-api's account.

   Type `delegation` only if your organisation uses domain-wide delegation
   instead of admin roles. The installer then asks for the mailbox swarm-api
   acts as, and opens `https://admin.google.com/ac/owl/domainwidedelegation`
   with the client id and scope to add.

   Either way, the grant takes 1 to 10 minutes to reach the Groups API, and
   the installer waits for up to 45.

5. **The proof.** The installer proves that swarm-api can read the group,
   through swarm-api itself. Only then does it register the admins' group.
6. **The re-apply.** It re-applies the infrastructure with the admins' group.
   It also checks the QA service account and swarm-api's allowed domain.

### Stage 10: the cluster

Nothing is asked in this stage.

The installer writes a kubeconfig of its own,
`~/.config/swarmcloud/install/<project>.kubeconfig`. Your `~/.kube/config`
and `$KUBECONFIG` are never read or changed.

It then:

1. fetches the release's Kubernetes bundle and checks it against the signed
   `SHA256SUMS`;
2. applies the cluster's admission policies;
3. creates the `swarm-system` namespace and its network policy;
4. applies the live-browser and code-intelligence workloads;
5. waits up to 5 minutes for each workload to roll out.

Each cluster call first waits for the control plane to admit this machine's
address: about a minute, for up to 10.

### Stage 13: the hand-off to the console

The installer first sets up the alerts. It checks that the notification
channel sends to your alert email, and waits for the log metrics to appear in
Cloud Monitoring: 1 to 5 minutes, for up to 20. Then it creates the alert
policies.

It checks again that the console answers behind IAP. Then it opens
`https://<hostname>/admin/setup` and prints:

```
Finish setup in the console's Admin › Setup (opening it now). Keep this terminal open: it turns the GitHub App on and runs verify when the console is done.
  https://<hostname>/admin/setup
```

The terminal then waits:

```
  waiting on handoff.console: you finishing Admin › Setup in the console -- <N> s left of the bound (usually 20 to 60 min)
```

The bound is 6 hours. Leave the terminal running and go to step 3. If you
close it, or it pauses, Admin › Setup shows `swarm install is not running`.
The steps on that page stay usable, and `swarm install` picks up the wait
when you run it again.

While you work in the console, the installer reacts to what you finish. This
is stage 9:

- **When the GitHub App's three secrets are in Secret Manager**, it applies
  infrastructure pass 3. That pass gives swarm-api the App's id and turns the
  App on.
- **When the App's self-check passes**, it applies infrastructure pass 4. That
  pass creates the webhook receiver. It then turns on the App's webhook.

The hand-off is done only when the console is finished and these three
switches are on.

## Step 3: Admin › Setup in the console

Sign in with your Workspace or Cloud Identity account. The page is titled
**Setup**. A strip across the top shows six steps and which one is next, for
example `Step 2 of 6 · next`. The strip does not count Add-ons, which are
optional, so the checklist below has seven rows. The page reads its state again every 5 seconds.
Nothing on it is marked done by hand: each row is done when the page sees it
done.

The left column holds:

- the **Send to your team** card, locked until verify passes;
- the **Capacity** card, with stage 0's sizing, including
  `Accounts to add: <n> Claude subscription accounts`;
- the **First run** checklist, with the rows below.

The right column holds **The installer** card, which shows what
`swarm install` is doing, and the **Health** card, which links to
Admin › Setup › Health, the hourly doctor's checks once setup is done.

1. **Project, infrastructure, identity, edge and secrets**, by
   `swarm install`. This row is already done when the page opens:
   `swarm install did the cloud part: stages 0-8, 10 and 13, ...`.
   Nothing to click.

2. **GitHub App.**
   1. Press **Create the GitHub App**. GitHub opens on your organisation, with
      the App's permissions filled in from a manifest. Create the App there.
   2. GitHub sends you back to the console's **GitHub App created** page. Its
      private key, client secret and webhook secret go from GitHub's answer
      straight into your Secret Manager. None is shown on the page or
      anywhere else.
   3. Press **Install the App** (later, **Install on <org>**). On GitHub,
      choose the repositories the App may reach. The page checks every 10
      seconds until GitHub shows the installation. **Check again** checks at
      once.
   4. The self-check runs by itself once `swarm install` has applied
      infrastructure pass 3. Until then the row says
      `Installed on <org>; waiting for swarm install’s infra pass 3, which gives swarm-api the App’s id`.
      The check proves that the App's key signs and that GitHub answers as the
      App.

   The row ends `<app> · installed on <org> · self-check passed`. If the
   check fails, the row says what to fix and offers **Retry**.

3. **First team**, for a team deployment.
   - **Workspace group** is filled in with `swarm-team@<domain>`, the group
     stage 8 created with you as its owner. Its members are the team.
   - **Workspace name** and **First workspace admin** are optional.
   - Press **Create the team**. It makes the same call `swarm team add` makes,
     and opens the group's members page in Google Admin.
   - The team gets a team-level grant over the repositories the App's
     installation covers. The row shows the workspace's namespace, quota,
     keys, worker identity, repository grant and git slot as each is ready.
   - To invite people, use **Invite**. The console opens the group's members
     page in Google Admin, where you add them. Each one shows as
     `waiting for <email> to join` until the Groups API sees them.
   - When the App's self-check has passed, swarm-api runs a small **smoke
     agent** on the first granted repository. It runs as swarm-api's own
     service account, with the App installation's token, and opens no pull
     request. If it fails, the row offers **Retry the smoke**.

   The row is done when the team exists and its smoke has succeeded.

   **First workspace**, for a one-person deployment. There is no form and no
   invites: `One-person install: your personal workspace is ready.` The
   workspace's grant and its smoke agent show on this row instead.

4. **Claude accounts.** In a team deployment this row comes after the first
   team, because each pool account's owner must be in the team's group. Each
   subscription's owner signs in once, into the first workspace's pool.
   - Give an **Account label**, for example `ravi`, and **Its owner**. For one
     person, the owner is you.
   - For your own subscription, press **Sign in my Claude account**. Your
     sign-in opens in a new tab: sign in to Claude there.
   - For someone else's, press **Make a sign-in link**. The page shows a
     one-time link, once: send it to them. It works only for them, once,
     within 24 hours. On that link they open it, start the sign-in, sign in
     on Claude's page, and paste the code Claude shows.

   The row counts `Pool: <signed in> of <n> signed in`. The **Capacity** card
   shows how many accounts stage 0 proposed.

5. **Add-ons**, optional. Each add-on is off by default, and SwarmCloud runs
   without them. To turn one on, use `swarm addon enable <name>` in the
   terminal ([add-ons](#add-ons)).

6. **Verify, run by swarm install.** Once every console step above is done,
   the row says `Every console step is done: hand over, and swarm install
   checks ...`. Press **Hand over to swarm install**. There is no separate
   button to start the checks:
   1. swarm-api starts the console's two runs on a repository of the grant: a
      single agent and a two-step workflow, each opening a pull request. They
      run as swarm-api's own service account in the first team's tenant (for
      one person, your personal workspace's), on its own GitHub slot, never
      on a person's GitHub connection. They wait for swarm install's infra
      pass 3 to give swarm-api the App's id; until then the page's next read
      starts them.
   2. Their ids go into the install's records, and swarm install copies them
      from there, so nobody pastes a run id. The row lists both runs, each
      linked, with its state and the repository.
   3. A run that fails or is cancelled is started again by swarm-api on the
      page's next read, up to twice for each run; the row says how many fresh
      starts are left. Only a run that failed after both shows **Run the
      console checks again**, disabled, and says to read that run for why.
   4. The terminal then runs stage 14, and this row shows its progress, for
      example `<check> · 3 of 11 · 4 min elapsed · about 12 min left`, then
      the deadline. If a check fails, the row names it; fix what swarm install
      printed and run `swarm install` again: it re-runs verify from the
      failed check.

7. **Send to your team.** This row unlocks when verify passes. Until then it
   says:
   `Send to your team is unlocked when verify passes: the line below works only on a deployment verify has proven.`
   Once unlocked, it gives one line for a teammate's terminal, with a **Copy**
   button:

   ```
   claude plugin marketplace add https://storage.googleapis.com/swarmcloud/marketplace.json && claude plugin install sc@swarmcloud --config deployment_url=https://<hostname>
   ```

   On a release candidate the marketplace is
   `https://storage.googleapis.com/swarmcloud/channels/rc.marketplace.json`.
   The row also shows the console's URL and
   `Then: sc login, then /sc:submit in Claude Code`.

### Stage 14: verify

Back in the terminal, stage 14 checks the deployment. Each check's progress
shows on the console's verify row.

1. **The Scheduler's targets.** Every job is enabled, its last attempt was
   answered, and its account may invoke its target. This is usually at once,
   or up to 10 minutes while a new grant propagates.
2. **The images.** Every service serves an image whose digest is in the
   release.
3. **The reconciler.** It can reach Jobs in every workspace namespace.
4. **Firestore.** The migrations are current.
5. **Seven runs** on this deployment. This takes 15 to 25 minutes, for up to
   an hour.
   - From the plugin, as you: a single agent and a workflow, each of which
     opens a pull request; an agent on GKE; a live-browser agent; and a
     code-intelligence read. The code-intelligence read is checked against
     the graph's own record of the commit it indexed.
   - From the console: the two runs you started in Admin › Setup.

   The console's runs must clone and push with the GitHub App installation's
   token. The plugin's runs are your own submissions, so they use the App
   installation token or your own GitHub App connection. A personal access
   token is refused. A failed run is started again, up to twice.

The plugin's runs sign in through the context that stage 2 stored, so verify
never asks for the Desktop client secret. If that context is not signed in
yet, it opens the browser once for you to sign in
(`swarm --context <context> login`).

To pick the repository the runs use, verify reads the repositories the
GitHub App installation covers in your organisation, as you. If your own
GitHub account is not connected to the deployment yet, it runs
`swarm sc setup --context <context>`, which opens GitHub in the browser for
you to connect GitHub as yourself through the App. It waits up to 10 minutes
for that, then reads the repositories again.

Then a last infrastructure pass removes this machine's address from the
cluster's control plane. Nothing else changes.

### Stage 15: READY

The install ends with:

```
READY: SwarmCloud answers at https://<hostname>
```

Admin › Setup then says `Setup is complete`. After that,
`swarm install --status` prints:

```
Ready.
  Console:    https://<hostname>/
  Teammates:  claude plugin marketplace add <marketplace>  (deployment URL: https://<hostname>)
  Health:     swarm doctor
```

## Steps outside the terminal and the console

Each of these pages is opened for you. Google, GitHub and Claude have no API
for what you do there, or the step needs your own sign-in. The installer or
the console watches for the result.

| Where | When | Why it is a page |
|---|---|---|
| Google sign-in | stage 0, and stage 8 if the Directory scopes were deferred | your own sign-in |
| The deployment's own sign-in (`swarm --context <context> login`) | stage 14, only if that context is not signed in yet | your own sign-in |
| GitHub: connect your own GitHub account through the App (`swarm sc setup`) | stage 14, only if your account is not connected yet | GitHub authorizes a person's connection only in the browser |
| Cloud Identity Free sign-up, and creating a billing account | stage 0, only without an organisation or a billing account | sign-ups and payment methods have no API |
| The Quotas page, or the billing page on a Free Trial | stage 0 or 1, only when a quota is too low | quota increases and account upgrades are requested by a person |
| The Cloud console, to accept the Terms of Service | stage 1, only if Google asks | the Terms are accepted by a person |
| The OAuth brand, its Audience, and the Desktop client | stage 2 | Google has no API for an Internal brand or a Desktop client, and shows the secret only on that page |
| Your DNS provider | stage 2, when the zone is not in the project's Cloud DNS (or you typed `manual`) | your registrar is outside Google Cloud |
| Google Admin: groups, admin roles or domain-wide delegation | stage 8, only if Google refuses the API call or you choose to | a Workspace admin's own action |
| GitHub: create and install the App | Admin › Setup | GitHub creates an App from a manifest only in the browser, and the installation is your choice of repositories |
| Google Admin: the team group's members | Admin › Setup, inviting people | membership is changed by an admin, and swarm-api only reads it |
| Claude's sign-in | Admin › Setup, Claude accounts | each subscription's owner signs in |

## When it stops

**Run `swarm install` again.** It picks up where it stopped. Every step checks
what is already there before it changes anything. Done steps are skipped, and
anything that has drifted is repaired by the step that made it.

The state lives in `gs://swarm-tfstate-<project>/swarm-install/state.json`.
Before stage 1 creates the bucket, it lives in
`~/.config/swarmcloud/install/<project>.json`. Once the bucket exists, you can
resume from another machine.

Only one person runs the install at a time, under a 30-minute lease that is
renewed while the install runs. If the person who started it is gone and their
lease has expired, `swarm install` asks
`<email>'s lease expired. Type their email to take over the install:`.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | done | nothing |
| 1 | a step failed | read the message, which names what failed and where to look, then run `swarm install` again |
| 2 | usage error | fix the flags |
| 3 | a check could not run | usually a sign-in or a right; the message says which |
| 4 | paused: a page or a wait ran past its bound, for example a DNS record not set yet | fix what it names, then run `swarm install` again |
| 5 | refused | a typed confirmation did not match, a flag disagrees with what the install recorded (`--version`, `--domain`, `--region` or `--env`), or someone else holds the lease. Nothing was changed |

**`swarm doctor`** checks the deployment. It never changes anything.

```bash
swarm doctor --project <project>
```

It checks the live deployment against every manifest entry that carries a
check. Each check prints one line: `met`, `unmet`, `unknown` or `skipped`,
with its id and what it saw. A check that is not met also says what it is for
and how to fix it.

`unknown` means the check could not run, and it counts the same as `unmet`.
When Google refuses a check, the doctor asks Google's Policy Troubleshooter
why, and names the missing right or says to wait for a grant to propagate.

`swarm doctor` exits 0 only when every check is met. The `swarm-doctor` job
runs the same checks every hour and posts them to **Admin › Setup › Health**.

**`swarm install --repair <check>`** re-runs the one step that makes a check
true. The check is the id `swarm doctor` printed.

```bash
swarm install --project <project> --repair <check>
```

It says which step and stage it will re-run, and asks you to type the check's
id to go on. `SWARM_ASSUME_YES` does not answer this. The step then asks for
its own approvals. A step done on a Google or GitHub page opens that page
again and waits for the change.

Other ways to see where an install stands:

- `swarm install --status` shows each stage, when it finished and who ran it.
  It writes nothing, and `--json` prints it as one JSON document.
- `swarm install --dry-run` checks every step and prints the plan of each one
  that is not met. It writes nothing.
- `swarm install --stage <n>` runs one stage again. You can repeat the flag.
  The stages before it are still checked.

## Afterwards

### Add-ons

Optional capabilities are turned on one at a time:

```bash
swarm addon status  --project <project>
swarm addon enable  <name> --project <project>
swarm addon disable <name> --project <project>
```

The add-ons are `bright-data-egress`, `tailscale`, `typesafe`, `brave-search`
and `ci`.

- An add-on whose key the platform generates gets that key without asking.
- An add-on that needs your credential gives you a one-time link in the
  console to enter it. With `--in-terminal`, it asks at a hidden prompt
  instead.

`disable` keeps the add-on's secret containers. `--delete-secrets` deletes
them too, after you type the project id.

### More teams

Add a team from Admin › Setup's **Add a team**, or from the terminal:

```bash
swarm team add <group-email> --project <project>
```

It finishes only when the new team's smoke run succeeds.

### Upgrade

```bash
swarm upgrade --to <version> --project <project>
```

The upgrade drains dispatch first, then applies the new release in a safe
order. If the new images fail to roll out, it rolls back. A change to IAM asks
you to type the project id. If an upgrade stops, run the same command again to
resume. To go to an older release, add `--allow-downgrade`.

### Uninstall

```bash
swarm uninstall --project <project>
```

The command lists what it will delete and asks you to type the project id. It
then deletes, in order:

1. the Scheduler jobs, after draining them;
2. the workspace namespaces and the cluster policies;
3. the infrastructure;
4. the copied images;
5. the install's secrets and IAM bindings;
6. the bootstrap pass, which destroys the KMS key versions;
7. the state buckets.

Some things Google keeps, or does not let anyone delete:

- the OAuth brand and the Desktop client;
- the KMS key ring and the custom role ids;
- the Firestore database id;
- the GitHub App;
- the DNS record;
- the Workspace groups and the Groups Reader assignment.

The uninstall writes this list to
`~/.config/swarmcloud/install/<project>.remains.json`, so that you can remove
these things by hand.
