# Install SwarmCloud

This page takes a company from an empty Google Cloud project to a SwarmCloud
deployment its people use the same day. It is the whole procedure: one
terminal, the console in a browser, and three steps.

```bash
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
swarm install
```

Then finish in the console's **Admin › Setup**. `swarm install` opens it for
you.

The installer asks you for three kinds of thing, and nothing else:

- **an approval**: a typed confirmation, a cost, or an IAM grant;
- **a credential or a sign-in**;
- **an optional choice**, where Enter keeps the proposed default.

Some steps happen on a Google or GitHub page that has no API, such as the OAuth
brand, the Desktop OAuth client, the GitHub App or a DNS record at your
registrar. For those the installer opens the exact page, says what to do there,
and watches for the change. You never press Enter to say you are done. You
never run another script, copy a command out of a document, or give Claude or
any other agent access to your project.

Releases come from SwarmCloud's release home: project `swarmcloud-prod`, the
bucket `gs://swarmcloud` (served as `https://storage.googleapis.com/swarmcloud`)
and the Artifact Registry repository
`us-docker.pkg.dev/swarmcloud-prod/swarmcloud`. Once the images are copied
into your project (stage 4), nothing in your deployment reads the release home
again.

## Before you start

You need:

- **A Google Workspace or Cloud Identity organisation**, and to be one of its
  admins. The installer creates the deployment's groups through the Groups API
  and assigns swarm-api the **Groups Reader** admin role. If you cannot assign
  admin roles, a super admin must take that one step. A personal gmail.com
  account cannot install SwarmCloud.
- **Rights in Google Cloud.** You need to create a project under an
  organisation or folder (`resourcemanager.projects.create`), and to link a
  billing account to it (`billing.resourceAssociations.create` on that
  account). Stage 0 checks both and names the role for any you lack. It never
  grants a right to you.
- **An open billing account.**
- **A GitHub organisation** that the deployment will serve, where you can
  create and install a GitHub App.
- **A hostname you control**, for example `swarm.example.com`, and access to
  set an `A` record for it at your DNS provider.
- **Claude subscriptions** for the agents to run on. Each subscription's owner
  signs in once, in the console.
- **A terminal on macOS or Linux** with `curl` and `sha256sum` or `shasum`.
  Everything else is fetched and checked by the installer, including uv,
  Python, Terraform, kubectl, cosign, gcrane, the GKE auth plugin and, if you
  agree, the Google Cloud SDK.

### What it costs

Before stage 1 creates anything, it shows the fixed monthly cost at Google's
list price. This is what the deployment costs while nobody uses it:

| Line | USD a month |
|---|---|
| swarm-api's two always-on instances, 1 vCPU and 1 GiB each | 105 |
| the GKE cluster's management fee, $0.10 an hour | 73 |
| the HTTPS load balancer's forwarding rule, $0.025 an hour | 18 |
| **floor, before any agent runs** | **about 196** |

Agent runs are billed on top, by the minute they run
([cost-control.md](cost-control.md)). Stage 1 also sets a monthly budget with
alerts at 50, 90 and 100 %. The proposed budget is the one stage 0's sizing
proposal gave, or 1,000 USD. You can type any other whole-dollar amount.

## Step 1: install the command line

```bash
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
```

This installs the release that `channels/stable` names. To install a release
candidate instead:

```bash
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | SWARM_CHANNEL=rc sh
```

Use `SWARMCLOUD_VERSION=<version>` instead to pin one release. Any channel
other than `stable` or `rc` is refused.

The script checks each download against the release's `SHA256SUMS`. If uv is
missing it installs it into `~/.local/bin`, from a pinned and checksummed
binary. It then installs two commands as one uv tool: `swarm`, which includes
the installer's verbs, and `sc`. Finally it puts them on your `PATH` and adds
one line to your shell's rc file, marked `# swarmcloud: swarm on PATH`. Open a
new terminal, or run the command the script prints, if `swarm` is not found
yet.

## Step 2: `swarm install`

```bash
swarm install
```

The installer asks:

- `Google Cloud project to create or use:` give a new project id, or one that
  already exists;
- `Domain for the console (a hostname you control):`
- `Region [us-central1]:`

It then shows a summary (project, hostname, region, release and the console's
URL) and asks `Continue? [Y/n]`. You can give the same answers as flags
instead: `--project`, `--domain` and `--region`. `--version` pins a release,
and `--env` names the environment (default `prod`).

Questions with a default show it in brackets, and Enter takes it.

Most of the install's time is spent waiting on Google and on you: DNS, the
certificate, the cluster and the console steps. When the installer waits, it
says what it is waiting for, how long that usually takes and where to look. You
can leave it running.

### Stage 0: preflight

Stage 0 only reads and asks. It creates nothing in Google Cloud.

- **Tools.** The installer fetches and checks its pinned tools. If the Google
  Cloud SDK is missing it asks
  `Install the Google Cloud SDK into ~/.swarm/google-cloud-sdk? [Y/n]`.
- **Sign-in.** If gcloud has no signed-in account, or there are no
  application-default credentials, it runs `gcloud auth login` or
  `gcloud auth application-default login`. Each opens Google's sign-in page in
  your browser. Sign in as the Workspace admin who is installing. That account
  becomes the install's owner, and the first platform admin.
- **Your answers.** It asks only for what it does not already know:
  - the email address that receives SwarmCloud's alerts (required);
  - the hostname, if you did not give it;
  - the GitHub organisation login the deployment serves (`github.com/<login>`);
  - the billing account to link: a numbered list of your open accounts, or
    typed as `XXXXXX-XXXXXX-XXXXXX`. This is skipped if the project is already
    billed.
- **Region, confirmed by typing it.** The Firestore location can never change,
  so the installer asks you to type the region again. Anything else records
  nothing.
- **Where the project goes.** If the project does not exist yet, it asks
  `Project <id> does not exist yet. Create it under:`. Pick from the
  organisations and folders where you can create projects. It then checks your
  rights, and names any role you are missing.
- **Your organisation's domain.** It reads the domain, for example
  `example.com`, from the organisation. It asks only if it cannot read it, and
  offers your own account's domain as the default.
- **This machine's address.** It reads your public IPv4 address, so that the
  install can reach the cluster's control plane. It asks only if two lookups
  disagree. The address is removed again at the end of the install.
- **One person or a team.** The installer asks whether the deployment is for
  one person or for a team, and for a team, how many people. From your answer
  it proposes:
  - capacity numbers: how many agents may run at once across the deployment,
    per workspace and per runner profile;
  - runtime ceilings: the maximum instances of swarm-api, the scheduler, the
    quota broker and the reconciler;
  - how many Claude subscription accounts to add;
  - the monthly budget.

  Accept the proposal, or change any one number. A one-person deployment
  admits only you, has no team group, and uses your personal workspace as the
  first workspace.
- **Organisation policies and quotas.** The installer reads the policies the
  project inherits: resource locations, domain-restricted sharing, CMEK, VM
  external IPs and default service account grants. A policy that blocks the
  install is named, with what an admin must change. For an existing project it
  also checks quotas (Cloud KMS, Cloud Run jobs, 24 regional CPUs, one GKE
  cluster, one static IP).
- **The release.** It reads the release from `gs://swarmcloud` and shows each
  CVE the release accepts, with its expiry. An expired acceptance stops the
  install.

### Stage 1: foundations

- **The approval of cost.** Stage 1 asks
  `Monthly budget for <project>, in US dollars (Enter keeps it)`. It then
  shows the budget, the cost estimate above, and what it will do (create the
  project, link billing, or both). It ends with
  `Type the project id (<project>) to approve`.
  Anything else is refused, and nothing is created or linked. An existing
  project that is already billed is left as it is.
- It creates the project, labelled `managed-by=swarm-install`, and links the
  billing account.
- It enables the Google APIs the deployment uses.
- It creates the budget `swarm-<project>`, with alerts at 50, 90 and 100 %.
- **An IAM approval.** The Compute Engine default service account starts with
  `roles/editor` on the project. The installer asks you to type `remove-editor`
  to remove it.
- It creates the state bucket `gs://swarm-tfstate-<project>` (versioned,
  uniform access, public access prevented) and moves the install's state
  there.

### Stage 2: the front door

- **The OAuth brand.** The installer opens
  `https://console.cloud.google.com/auth/branding?project=<project>`. Set the
  app name to `SwarmCloud`, set the support email to a group you own, and save.
  Then, on the Audience page, choose **Internal**. The installer watches for an
  Internal brand and carries on by itself.
- It creates the IAP service agent and reserves the console's global IP
  address.
- **The DNS record.** The installer prints the record to set at your DNS
  provider:

  ```
  <hostname>.  300  IN  A  <address>
  ```

  It opens a DNS lookup of your hostname
  (`https://toolbox.googleapps.com/apps/dig/#A/<hostname>`) and continues once
  the hostname resolves to that address alone. This usually takes minutes, and
  it waits up to an hour.
- **The certificate.** Once DNS is right, it waits for Google's managed
  certificate to become active.
- **The Desktop OAuth client.** The installer opens
  `https://console.cloud.google.com/auth/clients/create?project=<project>`.
  Create a client with the application type **Desktop app**, named
  `SwarmCloud sc`. Then paste its id at the prompt
  (`The Desktop client's id (ends .apps.googleusercontent.com)`), and its
  secret at the hidden prompt that follows. The secret is stored in Secret
  Manager and in this machine's SwarmCloud credential store, so nothing later
  asks for it again.

### Stage 3: roles, service accounts and keys

The installer downloads the release's Terraform module and checks its
signature against the release key built into the installer. It then applies
the first bootstrap pass as you: the custom roles, the deployer service account
`swarm-tf-deployer`, the KMS keys and the platform service accounts. Every
later pass runs as the deployer, not as you.

If any Terraform pass would destroy something, it lists what, and asks you to
type the project id to approve. `SWARM_ASSUME_YES` never answers that question.

### Stage 4: images

The installer creates your project's Artifact Registry repository. It verifies
each release image's signature, then copies the image by digest from
`us-docker.pkg.dev/swarmcloud-prod/swarmcloud` into your project. Your
deployment then runs only from its own copies.

### Stages 5 to 7: the platform

Nothing is asked in these stages.

- **Stage 5** applies the infrastructure: Cloud Run services and jobs, Cloud
  Scheduler, Pub/Sub, Firestore, the network, the GKE Autopilot cluster and the
  secret containers.
- **Stage 6** generates the platform's four signing keys and stores them in
  Secret Manager. They are never shown.
- **Stage 7** applies the second infrastructure and bootstrap passes. These
  turn on the alert channel and admit the Desktop client, which the plugin
  signs in with, at IAP.

IAP admits:

- for a team: everyone in your organisation's domain, you, and the
  deployment's own Desktop client;
- for one person: only you.

Who belongs to which workspace still comes from Google groups.

### Stage 8: identity

- **The groups.** The installer asks
  `Create swarm-admins@<domain> and swarm-team@<domain> through the Groups API, you as owner? (yes/no) [yes]`.
  A one-person deployment has only `swarm-admins@<domain>`, with you in it.
  If you answer no, or Google refuses, the installer opens
  `https://admin.google.com/ac/groups` and names the groups to create there. It
  carries on once they exist.
- **An IAM approval.** swarm-api needs to read group membership, and nothing
  else. The installer asks you to type `ASSIGN` to give its service account the
  Groups Reader admin role. If Google refuses, it opens
  `https://admin.google.com/ac/roles` and names the path: Groups Reader ›
  Admins › Assign service accounts. Type `delegation` instead only if your
  organisation uses domain-wide delegation rather than admin roles. In that
  case the installer also asks for the mailbox swarm-api acts as.
- The installer then proves that swarm-api can read a group, and re-applies
  the infrastructure with the groups and the admitted domain.

### Stage 10: the cluster

Nothing is asked in this stage. The installer writes a kubeconfig of its own
(`~/.config/swarmcloud/install/<project>.kubeconfig`); your own `~/.kube/config`
is never touched. It applies the cluster's admission policies, network policy
and the workloads that run in the cluster, and waits for them to roll out.

### Stage 13: the console

The installer sets up the alert policies and waits for the console to answer
behind IAP. It then opens `https://<hostname>/admin/setup` with:

> Finish setup in the console's Admin › Setup (opening it now). Keep this
> terminal open: it turns the GitHub App on and runs verify when the console
> is done.

Leave the terminal running and go to step 3. While you work in the console,
the installer reacts to each step you finish. When the GitHub App's secrets
appear it turns the App on (stage 9). When the App's self-check passes, it
turns on its webhooks.

## Step 3: Admin › Setup in the console

Sign in with your Workspace account. The page shows each step and which one is
next, and it updates by itself.

1. **GitHub App.** Create the deployment's GitHub App from the page. GitHub
   opens with every setting filled in, and after you create the App GitHub
   sends you back to the console. Then install the App on your GitHub
   organisation, choosing the repositories it may reach; the page notices the
   installation. Finally, run the self-check. The App's keys go straight from
   GitHub to your Secret Manager: no person and no terminal ever sees them.
2. **First team** (a team deployment only). Give the Workspace group whose
   members are the team (`swarm-team@<domain>` by default), the workspace's
   name and its first admin. The team gets a team-level grant over the
   repositories the App's installation covers. To invite people, the console
   opens the group's members page in Google Admin. You add people there, and
   the console shows each one as their membership appears. A one-person
   deployment skips this step: your personal workspace is the first workspace.
3. **Claude accounts.** Each Claude subscription's owner signs in once, into
   the first workspace's pool, through a one-time link the page makes. They can
   also use `sc account add`. Stage 0 proposed how many accounts to add. When
   the first account is in, swarm-api runs a small smoke agent on a granted
   repository.
4. **Verify.** The console starts its verification runs, and the terminal runs
   its own (stage 14). Nobody pastes a run id.
5. **Send to your team.** This step unlocks once verification passes. It shows
   what your people need: the plugin's marketplace
   (`https://storage.googleapis.com/swarmcloud/marketplace.json`), the console's
   URL, and "sc login, then /sc:submit in Claude Code".

### Stage 14: verify, and READY

Back in the terminal, stage 14 checks:

- the Scheduler's targets;
- the service image digests;
- the reconciler's Jobs;
- the Firestore migrations.

It then signs you in once through the browser (`swarm --context <context>
login`). It runs agents from the plugin, and waits for the console's runs. Every
run must clone and push with the GitHub App installation's token, never a
person's. A last infrastructure pass then removes this machine's address from
the cluster's control plane.

The install ends with:

```
READY: SwarmCloud answers at https://<hostname>
```

After that, `swarm install --status` says `Ready.` and prints the console's
URL, the line your people use to add the plugin, and `swarm doctor`.

## When a stage stops

**Run `swarm install` again.** It resumes where it stopped. Every step checks
what is already there before it changes anything. Done steps are skipped, and
anything that has drifted is repaired by the step that made it. The state
lives in `gs://swarm-tfstate-<project>/swarm-install/state.json`, or before
stage 1 in `~/.config/swarmcloud/install/<project>.json`. You can therefore
resume from another machine, once the state bucket exists.

What the exit codes mean:

| Exit | Meaning | What to do |
|---|---|---|
| 0 | done | nothing |
| 1 | a step failed | read the step's message, which names what failed and where to look, then run `swarm install` again |
| 3 | a check could not run | usually a sign-in or a right; the message says which |
| 4 | paused | a wait ran past its limit, for example a DNS record not set yet. Fix what it names and run `swarm install` again |
| 5 | refused | a typed confirmation did not match, or a flag disagrees with what the install recorded (a different `--version`, `--domain` or `--region`). Nothing was changed |

Other ways to see where an install stands:

- `swarm install --status` shows each stage, when it finished and who ran it.
  It writes nothing. Add `--json` for one JSON document.
- `swarm install --dry-run` checks every step and prints the plan of each one
  that is not met. It writes nothing.
- `swarm install --stage <n>` runs one stage again. You can repeat the flag.
  The stages before it are still checked.
- `swarm doctor` checks this machine and then the deployment (below).
- `swarm install --repair <check>` re-runs the one step that repairs a check
  `swarm doctor` names (below).

### `swarm doctor`: check the deployment

```bash
swarm doctor --project <project>
```

`swarm doctor` never changes anything. It runs in two parts:

1. **This machine.** When the `sc` plugin's bridge is installed beside the
   installer, it first shows which deployment this machine reaches and how it
   is signed in. `--platform-only` skips this part, and `--json` leaves it
   out.
2. **The deployment.** It reads the install's state (from the state bucket,
   or else the local copy) and checks the live deployment against every entry
   in the release's manifest that carries a check. Without `--project` it uses
   the project gcloud is configured with, and `--env` names the environment
   (default `prod`). The checks look at:
   - the Google APIs the deployment needs, which must be enabled;
   - the Secret Manager secrets, which must exist (it never reads a value);
   - IAP on the front door, which must admit the deployment's Desktop client;
   - the Workspace groups, which swarm-api must be able to read;
   - the region's Cloud Run Jobs, against the limit of 1,000 Jobs a region
     may hold, and whether finished Jobs are being deleted;
   - the cluster's control plane, which must have authorised networks on,
     must not admit `0.0.0.0/0`, and must no longer admit the installer's
     address;
   - the cluster's access rules in every team namespace.

Each check prints one line: `met`, `unmet` or `unknown`, its id and what it
saw. A check that is not met also says, in words, what it is for and how to
fix it: usually `swarm install --repair <id>`, which for a step done on a
Google or GitHub page opens that page and waits for the change. A last line counts the results and says how many manifest
entries carry no check, because the install itself verified them when it ran.
`--json` prints all of this as one JSON document, with each check's repair
command.

**`unknown` is never fine.** It means the check could not run: a missing
right, a timeout, or something the install state does not record yet. It
counts the same as `unmet`. When Google refuses a check, the doctor asks
Google's Policy Troubleshooter why. If you hold the right, Google is still
propagating the grant (45 seconds to 10 minutes), and the line says to run
`swarm doctor` again. If you do not, the line names the right that is
missing.

`swarm doctor` exits:

| Exit | Meaning |
|---|---|
| 0 | every check is `met` |
| 1 | the doctor could not start: no install state for the project, or the manifest would not load |
| 2 | no project to check, or the install state is for another environment |
| 3 | at least one check is `unmet` or `unknown`, or the check of this machine failed |

### `swarm install --repair <check>`: repair one check

```bash
swarm install --project <project> --repair <check>
```

`<check>` is the id `swarm doctor` printed. The installer finds the install
step that makes that check true and says which step and stage it will re-run.
You type the check's id to go on. This confirmation ignores
`SWARM_ASSUME_YES`, and with no terminal to type on the repair is refused
(exit 5) and nothing changes.

The repair then takes the install's lease, as `swarm install` does. It checks
the steps before it, and the other steps of the same stage, without changing
them, and runs the one step. That step asks for its own approvals: a typed
confirmation, a cost or an IAM grant. When the step is done on a Google or
GitHub page, the installer opens the page, says what to do there and watches
for the change. If the step's own check already passes and the install
recorded the step done, the step is skipped: there is nothing for it to
repair. A check id
that is not in the manifest, or that no install step makes, exits 2. Otherwise
the exit codes are those of `swarm install`, in the table above.

### The hourly doctor

The `swarm-doctor` Cloud Run job runs the same checks every hour, read-only,
with its own service account. It posts what it finds to **Admin › Setup ›
Health** in the console. The install turns it on in its second
infrastructure pass. When the job cannot read the install's state, the checks
that need it say `unknown`. When it cannot post, the next hour's run tries
again.

The last line of `swarm doctor` says whether the hourly doctor is on. When it
is off (`enable_scheduled_doctor` is false in the deployment's Terraform), the
line says so, and that Admin › Setup › Health then shows no hourly findings.

Only one person runs the install at a time, under a lease. If the person who
started it is gone and their lease has expired, `swarm install` asks you to
type their email address to take it over.

## Afterwards

### Add-ons

Optional capabilities are turned on one at a time:

```bash
swarm addon status  --project <project>
swarm addon enable  <name> --project <project>
swarm addon disable <name> --project <project>
```

The add-ons are `bright-data-egress`, `tailscale`, `typesafe`, `brave-search`
and `ci`. An add-on that needs a secret gives you a one-time link in the
console to enter it, or a hidden prompt with `--in-terminal`. `disable` keeps
the add-on's secret containers. `--delete-secrets` deletes them too, after you
type the project id.

### More teams

Add a team from Admin › Setup, or from the terminal:

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

The command lists what it will delete, and asks you to type the project id. It
deletes, in order:

- the Scheduler jobs (after draining them);
- the tenant namespaces and the cluster policies;
- the infrastructure;
- the copied images;
- the install's secrets and IAM bindings;
- the bootstrap pass, which destroys the KMS key versions;
- the state buckets.

Google keeps some things, or does not let them be deleted:

- the OAuth brand and the Desktop client;
- the KMS key ring and the custom role ids;
- the Firestore database id;
- the GitHub App;
- the DNS record;
- the Workspace groups and the Groups Reader assignment.

The uninstall writes this list to
`~/.config/swarmcloud/install/<project>.remains.json`, so you can remove these
by hand.
