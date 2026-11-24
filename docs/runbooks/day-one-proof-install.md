# Day One proof install: from an empty project to a merged pull request, then the first release

This is the runbook for the proof the Day One epic asks for: a person, using
only the product, installs SwarmCloud into an empty Google Cloud project; a
teammate goes from the plugin to a merged pull request on it; every snag goes
in a [snag log](day-one-snag-log-template.md); and the owner signs off the
first public release. The owner approves this runbook, then follows it.

**How to read the citations.** Every step names the file and symbol, or the
docs section, it was checked against when this runbook was written
(2026-11-24, on the branch's single commit `263ca01`). `SI` means
`apps/swarm-install/swarm_install/`. A step that could not be checked says
**not checked** and why. Where the docs and the code disagree, this runbook
follows the code and says so; each such disagreement is listed in
[Known disagreements, fixed before the first proof install](#known-disagreements-fixed-before-the-first-proof-install),
with where it was fixed, because a person using only the product would have
met them.

**Placeholders.** Nothing here names a real company, person or deployment.

| Placeholder | Meaning |
|---|---|
| `<proof-project-1>`, `<proof-project-2>` | the two new project ids, 6 to 30 characters (`SI/steps/foundations.py`, `PROJECT_ID`) |
| `<host-1>`, `<host-2>` | the hostnames the two deployments answer on, for example `swarm1.example.com` |
| `<example.com>` | the organisation's Workspace or Cloud Identity domain |
| `<example-org>` | the GitHub organisation the deployment serves |
| `<V>-rc.<n>` | the release candidate; `<V>` the first final release, `YYYY.MM.N` |
| `<owner>`, `<teammate>` | the person installing, and a second person who is not the owner |

**Secrets are never written down.** Wherever a value is a secret (the Desktop
client secret, a Claude sign-in code), the person types it into the product's
own prompt or form, and the snag log records only that it was typed and where.

---

## 1. What this proves, and what counts as a pass

The epic's bar, in its own words, is that the guided install "has never been
proven end to end by a person using only the product". This runbook proves it
when all of the following are true:

1. **A person, using only the product, goes from an empty project to a working
   deployment.** "Only the product" means the install line, `swarm` and its
   prompts, the pages it opens, the console's Admin › Setup and the published
   [install page](../install.md). Not a checkout, not a script from this
   repository, not a command copied from a design document. The plan states
   the same rule for P4: "from the product only, D12; no checkout"
   (`docs/design/day-one-plan.md` §10). "Working" means `swarm install` ends
   with `READY: SwarmCloud answers at https://<host>` (`SI/steps/handoff.py`,
   `ReadyStep`) and `swarm doctor` exits 0 (`SI/verbs/doctor.py`;
   [install.md § When it stops](../install.md#when-it-stops)).
2. **A teammate goes from the plugin to a merged pull request on it.** The
   teammate is not the owner, starts from the one line the console's **Send to
   your team** step shows, and ends with a pull request their task opened,
   merged on GitHub ([section 5](#5-the-teammates-path-from-the-plugin-to-a-merged-pull-request)).
3. **The snag log is filled.** One row for every step of section 4, including
   the steps that went cleanly ("no snag" is a recorded result, not a blank).
   Each snag is filed as its own issue and is either fixed or accepted by the
   owner, by name, in the log (epic: "Each snag becomes its own issue").
4. **The owner signs off the first public release** with the checklist in
   [section 7](#7-the-release-sign-off-checklist), then promotes the stable
   channel by hand.

The epic orders the work as four things, and this runbook keeps that order:

| Epic item | Runbook |
|---|---|
| a green `swarm install --dry-run` against an empty project, from a published candidate | Phase A and B of section 4 |
| proof install 1: cold, product only, every snag logged, then `swarm uninstall` | Phase C, then [section 6](#6-rollback-uninstall-what-it-removes-what-it-leaves) |
| proof install 2: zero snags, the teammate path, a reinstall into project 1, an upgrade from the candidate to the first release | Phase D and [section 5](#5-the-teammates-path-from-the-plugin-to-a-merged-pull-request) |
| the first release shipped, the stable channel promoted by hand | Phase E and [section 7](#7-the-release-sign-off-checklist) |

A proof install **fails** if any step needs something outside the product (a
`gcloud` command typed by hand, a file edited, a person from the team that
built it unblocking it). That is a snag. Log it, then decide whether to go on
([section 8](#8-open-decisions-for-the-owner)).

---

## 2. Impact inventory

### 2.1 What the install creates in the new project, by kind

The source is the install manifest as the engine loads it: `install/manifest.yaml`
plus the fragments in `install/manifest.d/`, merged by `SI/manifest.py` (`load`,
`merge`). The `counts:` block at the top of `install/manifest.yaml` covers only
the base file, 447 entries. The merged manifest has 566 entries. Of those, 489
are `scope: core`; the rest belong to add-ons, which are off until someone runs
`swarm addon enable` ([install.md § Add-ons](../install.md#add-ons)). The engine
makes 448 of the core entries (`provider: engine`). These counts come from
`manifest.load()`, run read-only on 2026-11-24.

Not every entry is a cloud resource. `switch`, `declared-value`, `local-tool`
and `other` are facts the install checks or settings it sets. Core entries the
engine makes, by kind:

| Kind | Core entries | What it is in the new project |
|---|---|---|
| `iam-binding` | 71 | project-level IAM grants to the deployment's own service accounts |
| `other` | 54 | assorted checks and one-off objects (for example the IAP service agent) |
| `switch` | 38 | settings turned on in order, key before switch ([`day-one-plan.md` §9.1](../design/day-one-plan.md#91-install-stage-by-stage-key-before-switch-every-wait-bounded)) |
| `custom-role` | 30 | project custom roles, made by bootstrap pass 1 |
| `network` | 27 | VPC, subnets, NAT, router, firewall, the global address `swarm-ui-ip`, the load balancer pieces |
| `gcp-api` | 25 | enabled Google APIs (the stage 1 list is in `SI/steps/foundations.py`) |
| `image` | 24 | container images copied by digest into `<region>-docker.pkg.dev/<project>/swarm-images` |
| `service-account` | 21 | the deployer `swarm-tf-deployer` and the platform's service accounts |
| `secret-container` | 18 | Secret Manager secrets, empty until a value is stored |
| `declared-value` | 17 | values written into Terraform inputs |
| `namespace-object` | 15 | Kubernetes objects in `swarm-system` and the first team's namespace |
| `scheduler-job` | 15 | Cloud Scheduler jobs |
| `secret-value` | 12 | secret versions the engine generates (stage 6), never shown |
| `pubsub-topic` | 10 | Pub/Sub topics |
| `local-tool` | 9 | pinned tools on the installer's machine, not in the project |
| `monitoring` | 9 | log metrics, the notification channel, alert policies |
| `cluster-policy` | 9 | admission policies on the cluster |
| `bucket` | 7 | Cloud Storage buckets, including `swarm-tfstate-<project>` (`SI/state.py`, `state_bucket`) |
| `cloud-run-service` | 7 | Cloud Run services (swarm-api, the console, discovery and others) |
| `cloud-run-job` | 6 | Cloud Run jobs (including `swarm-doctor`) |
| `firestore` | 6 | the Firestore database and its indexes |
| `registry` | 5 | Artifact Registry repositories and policies |
| `iap-setting` | 3 | IAP settings on the backends |
| `kms-key` | 3 | the KMS key ring and keys |
| `certificate`, `cluster`, `dns`, `oauth-brand` | 1 each | the managed certificate, the GKE Autopilot cluster, the A record, the brand |
| `workspace-group`, `workspace-delegation`, `quota` | 1 each | checked or made outside the project (section 2.2) |

**What a yes to Voice it adds (the proof answers no).** The Voice it questions
at stage 0 (`infra.voice-it`, `infra.voice-speech`; `infra.voice-speech-zone`
at stage 1) add core entries to the manifest that the table above counts only
if they are on. A yes to the speech engine adds one L4 GPU machine from a
regional instance template (`terraform/infra/voice_speech.tf`,
`compute.voice-speech-machine`), its firewall and address, the
`swarm-voice` and `swarm-speech` service accounts and their images. It needs
one L4 of GPU quota in the region's zone (stage 1 checks it; section 2.2) and
its cost is not in section 2.5's floor. Not priced here: not checked.

The project itself is made by Python, not Terraform, labelled
`managed-by=swarm-install` (`SI/steps/preflight.py`, `CREATED_BY_INSTALL`;
`SI/steps/foundations.py`, `ProjectStep`). Every Terraform resource carries
`managed-by=swarm-terraform`.

### 2.2 What it needs or changes at organisation and billing level

| Level | What | Checked against |
|---|---|---|
| Organisation (read) | Five organisation policies are read, never changed: resource locations, domain-restricted sharing, CMEK, VM external IP access, automatic IAM grants to default service accounts. A resource-locations policy that excludes the region, or a CMEK policy covering a service the deployment uses, stops the install. | `SI/steps/preflight.py`, `ORG_POLICIES`, `OrgPoliciesStep`; [install.md § Stage 0](../install.md#stage-0-preflight) item 9 |
| Organisation (write) | A project created under the organisation or a folder you pick. | `SI/steps/foundations.py`, `ProjectStep`; [install.md § Stage 1](../install.md#stage-1-foundations) |
| Organisation (Workspace) | Groups `swarm-admins@<example.com>` and, for a team, `swarm-team@<example.com>`, with you as owner. The **Groups Reader** admin role is assigned to swarm-api's service account, or domain-wide delegation if you choose it. | `SI/steps/identity.py`; [install.md § Stage 8](../install.md#stage-8-the-consoles-address-and-identity) items 3 and 4 |
| Quotas (read, possibly a request) | KMS requests at least 600, Cloud Run jobs, regional CPUs at least 24, one GKE cluster and one static IP. A shortfall opens the Quotas page. | `SI/steps/preflight.py`, `QUOTAS`; [install.md § Stage 0](../install.md#stage-0-preflight) item 10 |
| GPU quota (only for a yes to Voice it's speech engine) | one NVIDIA L4 in a zone of the region. The proof answers no. | `SI/steps/infra.py`, `VoiceSpeechZoneStep` |
| Billing account | The project is linked to the billing account you pick. A budget `swarm-<project>` is created with alerts at 50, 90 and 100 %. | `SI/steps/foundations.py`, `BudgetStep`; `SI/uninstall.py`, `budget_name` |

Terraform makes nothing above project level. A grep of `terraform/` on
2026-11-24 found no `google_organization*`, `google_folder*` or `google_billing*`
resource.

### 2.3 What it touches outside the new project

| Where | What | Undone by `swarm uninstall`? |
|---|---|---|
| Billing account | the project link and the budget `swarm-<project>` | The budget is deleted. If the billing account refuses, it is listed under `left`. The link stays until the project is deleted; if you keep the project (C15) it stays too. (`SI/uninstall.py`, `_budget`) |
| Google Workspace or Cloud Identity | the groups, the Groups Reader assignment or the delegation | No. Listed as `workspace-groups` (`SI/remains.py`) |
| Your DNS provider, or a Cloud DNS zone | one `A` record for `<host>` | Deleted only if it sits in the project's own Cloud DNS zone; otherwise listed as `dns-record` (`SI/uninstall.py`, `_dns_record`) |
| GitHub (`<example-org>`) | a GitHub App and its installation | No. Only the owning account can delete it; listed as `github-app` (`SI/remains.py`) |
| Claude | each pool account's sign-in, from the console | not checked: no uninstall step names it |
| The release home (`https://storage.googleapis.com/swarmcloud`, `us-docker.pkg.dev/swarmcloud-prod/swarmcloud`) | read only: the CLI, the bundle and the images are fetched | nothing to undo ([install.md](../install.md), intro) |
| The installer's machine | the `swarm` and `sc` tool, uv if missing, the Google Cloud SDK if you accept its install, the machine-wide Application Default Credentials (stage 0 runs `gcloud auth application-default login --disable-quota-project` with Directory scopes, replacing what other tools use; `SI/steps/preflight.py`, `adc_login`), one `PATH` line in the shell rc file, `~/.swarm/`, `~/.config/swarmcloud/install/<project>.{json,kubeconfig,remains.json}`, the `swarm` credential store | No. Not removed by any verb (`install/install.sh`; `SI/state.py`, `LOCAL_DIR`) |

**How the engine keeps out of every other project.** It must touch nothing in
any existing project. Six mechanisms do the work; one gap is open.

1. **Every call names the new project.** Each step reads the project from the
   install state. A `--project`, `--env`, `--domain`, `--region` or `--version`
   that disagrees with the recorded install is refused with exit 5 and changes
   nothing (`SI/verbs/install.py`, `_refusal`;
   [install.md § When it stops](../install.md#when-it-stops)).
2. **The never-touch list.** Stage 1 writes `config/<env>/deny-list.json` into
   the new project's state bucket once, and never replaces it
   (`SI/steps/foundations.py`, `ConfigStep`, `deny_list_document`).
   - For a project the installer created, the list is explicitly empty: no
     entries, every floor zero.
   - For a project that already existed, the installer lists what the
     deployment does not own (`SI/steps/preflight.py`, `neighbours`). That is
     clusters, networks including `default`, subnets, buckets and service
     accounts that are neither `swarm-` prefixed nor labelled `managed-by`
     `swarm-terraform`, `swarm-install` or `swarm-bootstrap`. You type the
     project id to approve the list.
   - The shell reads the same object fail-closed (`scripts/lib/common.sh`,
     `deny_list_load`, `deny_list_require`).
   - The Python side refuses any deny-listed name: `SI/uninstall.py`
     (`denied`, `refuse_denied`), `SI/steps/cluster.py` (`WrongCluster`) and
     `SI/addons.py` (`_refuse_denied`).
3. **Its own kubeconfig.** The cluster steps write
   `~/.config/swarmcloud/install/<project>.kubeconfig` and never read
   `~/.kube/config` or `$KUBECONFIG`. Any other cluster is refused before
   `kubectl` runs (`SI/steps/cluster.py`, `WrongCluster`;
   [install.md § Stage 10](../install.md#stage-10-the-cluster)).
4. **Only the deployment's own resources are deleted.** The uninstall destroys
   only what is labelled `managed-by=swarm-terraform` or `swarm-install` (or a
   type that cannot carry labels). Anything else is dropped from state and
   listed under "Also not removed:" (`SI/uninstall.py`, `OWNED_BY`).
5. **The plan guard inside the engine.** `SI/terraform.py` (`review_plan`,
   `run_pass`) refuses an IAM resource replaced destroy-first, an IAM
   condition over Google's limits, and a Cloud Run service not at 100 % to
   its latest revision. It asks for the project id, typed, before any destroy,
   and `SWARM_ASSUME_YES` never answers.
6. **Nothing above project level.** No Terraform resource sits at organisation,
   folder or billing level (the grep above).

**Gap 1: stage 1 can change an existing project before the never-touch
approval.** `ProjectStep` treats an existing, billed project as met, with no
typed approval. When an id is taken it asks `Type another project id to
create`, and silently uses the id typed if that is a project you can see
(`SI/steps/foundations.py`, `ProjectStep._another`). Stage 1 then runs the
APIs, the budget, `DefaultComputeEditorStep` (removes `roles/editor` from that
project's default compute account once `remove-editor` is typed) and the state
bucket **before** `ConfigStep` shows the neighbours and asks for approval. A
typo, or the retry at "is taken", that lands on an existing project therefore
changes it. The runbook's guard is a person's check (C1, C3); the engine does
not stop it. It is in [decision O7](#8-open-decisions-for-the-owner).

**Gap 2.** The repository's shell plan guard checks for a plan that writes
outside the target project (`scripts/lib/plan-guard.sh`, the `wrong_project`
check). The installer does not run it: no file in `SI/` calls `plan-guard.sh`
(grep, 2026-11-24). The installer's in-engine review has no wrong-project
check. Between them, mechanisms 1, 2, 3 and 6 (and a person's care at C1 and C3, see gap 1) are what keep the install inside
the new project. The guard is not a code change for this runbook; it is
[decision O7](#8-open-decisions-for-the-owner).

**A shared project is refused outright.** `swarm uninstall` refuses the whole
uninstall (exit 5) when the install state carries any deny-list entry at all
(`SI/uninstall.py`, `Uninstall.refusal`). That has a consequence for this
proof:
[let the installer create the project](#8-open-decisions-for-the-owner) (O1).

### 2.4 What cannot be undone

| Item | Why | Source |
|---|---|---|
| The OAuth brand | has no delete method; goes only with the project | `SI/remains.py`; `SI/adopt.py` docstring |
| The KMS key ring and keys | never deleted; the uninstall destroys the key versions | `SI/remains.py`; `SI/uninstall.py`, `KEEP` |
| Custom role ids | a deleted role can be undeleted for 7 days, and its id is held for 37 days in total. **A reinstall into project 1 must start within 7 days of its uninstall, or wait until day 37.** Past day 7, bootstrap stops with "custom roles ... were deleted more than 7 days ago". | `SI/steps/bootstrap.py`, `ROLE_ID_HOLD_DAYS`, `ROLE_UNDELETE_DAYS`, `held_roles`; `SI/adopt.py`, `ROLE_UNDELETE_DAYS` |
| The Firestore location | the region can never change for the project; a deleted database's id comes free after Google's delay | [install.md § Stage 0](../install.md#stage-0-preflight) item 5; `SI/adopt.py`, `FIRESTORE_ID` |
| The project id | if you delete the project, Google shuts it down at once and deletes it after 30 days. The id is never reusable; [install.md § Stage 1](../install.md#stage-1-foundations) says so since K6 was fixed. | `SI/uninstall.py`, the project step's prompt |
| The GitHub App | only the owning account can delete it, and App names are global | `SI/remains.py` |
| The state bucket name `swarm-tfstate-<project>` | bucket names are global | `SI/state.py`, `state_bucket` |
| Workspace groups, the Groups Reader assignment, a delegation | organisation-admin objects | `SI/remains.py` |
| Domain verification for a new Cloud Identity organisation | done by a person at the registrar; no step undoes it | [install.md § Stage 0](../install.md#stage-0-preflight) item 3 |
| The first release's version number | a published version is never overwritten; a failed publish burns it | [release.md § If the run fails](release.md#if-the-run-fails) |

### 2.5 What it costs while it stands

[install.md § What it costs](../install.md#what-it-costs) gives the floor at
Google's list price, the same figures as `SI/steps/foundations.py`
(`MONTHLY_FLOOR_USD`):

| Line | USD a month |
|---|---|
| swarm-api's two always-on instances | 210 |
| the GKE cluster's management fee | 73 |
| the HTTPS load balancer's forwarding rule | 18 |
| **floor** | **about 301** |

- **Per day:** about 10 USD per standing deployment. That is 301 divided by 30;
  this runbook did the arithmetic, the repository gives no per-day figure.
- **Not priced:** NAT, Firestore, KMS, Secret Manager, logs and image storage
  are not in the floor (`SI/sizing.py`, comment above `BUDGET_FLOOR_USD`).
- **Agents:** priced at 0.22 USD an agent-hour (`SI/sizing.py`,
  `USD_PER_AGENT_HOUR`). Verify's seven runs of 15 to 25 minutes cost cents.
- **Voice it's speech engine** (a yes at stage 0; the proof answers no) adds one always-on L4 GPU machine, not in the floor and not priced here.
- **Claude subscriptions** are paid outside Google Cloud.
- **For the whole proof**, with two deployments standing two to three days
  each: about 40 to 60 USD of floor.
- **The proposed budget.** The installer proposes the budget from
  `SI/sizing.py` (`propose`, `BUDGET_FLOOR_USD = 400` plus run time), which
  install.md § What it costs states since K5 was fixed: 600 USD for one
  person. Log the figure the terminal shows. The budget only alerts; nothing in the code stops spend
  when it is reached (not checked against a live budget).

### 2.6 How long each stage takes

Expected time and bound are from the code's wait specs, quoted by
[install.md](../install.md) per stage. Where neither gives one, the table says
so.

| Stage | What | Expected / bound |
|---|---|---|
| CLI | `install.sh` | not stated; a download and a `uv tool install` |
| 0 | preflight; with no organisation, Cloud Identity sign-up | organisation 15 to 60 min, bound 3 h; billing account 5 to 15 min, bound 1 h; quota up to 2 business days, bound 4 h (`SI/steps/preflight.py`, `ORGANISATION_WAIT`, `BILLING_WAIT`) |
| 1 | project, APIs, budget, state bucket, never-touch list | project under a minute; Terms of Service 1 to 5 min, bound 30; APIs 1 to 3 min, bound 10 |
| 2 | client-secret container, IAP agent, global IP, DNS, certificate precondition | DNS a few minutes after the record is saved, bound 60 min (`SI/steps/edge.py`, `DnsStep`) |
| 3 | bootstrap pass 1 | impersonation grant up to 10 min |
| 4 to 7 | images, infra pass 1, platform keys, infra and bootstrap pass 2 | **not stated** anywhere; record the real times in the snag log |
| 8 | console answering, groups, admin role | certificate and console up to 10 min, bound 1 h; groups 1 to 5 min, bound 45; grant 1 to 10 min, bound 45 |
| 9 | GitHub App switches, during the console steps | (`SI/steps/github_switches.py`) |
| 10 | cluster | control plane about 1 min, bound 10; each rollout bound 5 min (`SI/steps/cluster.py`, `ROLLOUT_TIMEOUT`) |
| 13 | alerts, hand-off to Admin › Setup | alerts 1 to 5 min, bound 20; console steps usually 20 to 60 min, bound 6 h (`SI/steps/handoff.py`, `CONSOLE`) |
| 14 | verify | seven runs, 15 to 25 min, bound 1 h |
| 15 | READY | at once |

The repository gives no total. A reasonable plan:

- **Proof install 1:** half a day for one sitting if the organisation, billing
  and quotas are already in place. Add up to 3 hours for a new organisation.
- **Proof install 2:** the same again.
- **Reinstall, upgrade, publish and promote:** another half day.

---

## 3. Before starting

Check each item before Phase A. Write the result in the snag log's
"Before starting" table.

| You need | How to check it | Source |
|---|---|---|
| **A Google organisation** on Workspace or Cloud Identity, on a domain you own, and an admin account in it that can create groups and assign admin roles | Sign in at `admin.google.com` as that account. If you have none, stage 0 opens the Cloud Identity Free sign-up and waits; allow up to 3 hours. | [install.md § Before you start](../install.md#before-you-start); `SI/steps/preflight.py`, `OrganizationStep` |
| **Project Creator** (`resourcemanager.projects.create`) on the organisation or a folder | Stage 0 checks it and names the role if missing (`RightsStep`). To know beforehand, look at your roles on the organisation's IAM page. | install.md § Before you start |
| **Billing User** (`billing.resourceAssociations.create`) on an open billing account that is not a Free Trial | Stage 0 lists the open accounts it can see. A Free Trial is capped at 8 CPUs and stage 0 opens the upgrade page (`SI/steps/preflight.py`, `FREE_TRIAL_CPUS`). | install.md § Before you start |
| **A new project id, not an existing project** (see O1) | Pick two ids, `<proof-project-1>` and `<proof-project-2>`, that nobody has used. Let `swarm install` create each one. | `SI/steps/foundations.py`, `ConfigStep`; `SI/uninstall.py`, `Uninstall.refusal` |
| **Two hostnames you control**, `<host-1>` and `<host-2>` | You can add an `A` record for each at your DNS provider, or the zone is in the new project's Cloud DNS (it is not, for a new project). | install.md § Before you start; [§ Stage 2](../install.md#stage-2-the-front-door) item 3 |
| **A GitHub organisation** `<example-org>` where you can create and install a GitHub App, and at least one repository the App may reach, with a test you do not mind agents opening pull requests on | You are an owner of the organisation on GitHub. | install.md § Before you start; [§ Step 3](../install.md#step-3-admin--setup-in-the-console) row 2 |
| **Claude subscriptions**: at least one, the number stage 0's sizing proposes (`Claude subscription accounts to add`) | Each owner can sign in to Claude in a browser. | install.md § Before you start; [§ Stage 0](../install.md#stage-0-preflight) item 11 |
| **A teammate** who is not the owner, with an account in `<example.com>`, a GitHub account in `<example-org>`, Claude Code installed, and a macOS or Linux terminal | They can run `claude --version` and see v2.1.224 or later. The release plugin is an `archive` source that needs that version (`scripts/release/marketplace.py`, module docstring). | section 5 |
| **A terminal on macOS or Linux** with `curl`, `tar`, and `sha256sum` or `shasum` | `command -v curl tar sha256sum shasum` | install.md § Before you start; `install/install.sh` |
| **A browser on the same machine**, signed in to nothing you would mind the installer opening pages in | The installer opens pages unless `SWARM_NO_BROWSER` is set (`SI/steps/__init__.py`, `open_page`) | |
| **Rights in the release home** to publish and promote (owner or operator) | You can approve the `release` environment, and the once-only set-up in [release.md § Once: let CI publish](release.md#once-let-ci-publish) is done | release.md |
| **The snag log**, a copy of [the template](day-one-snag-log-template.md) | Open; header filled | this runbook |

---

## 4. The steps

Each step lists:

- **Who** does it.
- **Do:** the exact product action.
- **Worked when:** what appears when it worked.
- **Log:** what to record in the snag log.
- **If it fails:** resume, doctor or stop.
- **Checked against:** the file and symbol, or docs section, the step was
  checked against.

**The three answers to any failure in this phase.** They hold for every step
below unless the step says otherwise.

- **Resume.** Run the same `swarm install` line again. It skips what is done,
  re-checks the rest and repairs drift. There is no `--resume` flag.
  (`SI/engine.py` docstring; [install.md § When it stops](../install.md#when-it-stops))
- **Doctor.** `swarm doctor --project <project>` reads the deployment and
  prints one line per check. Then `swarm install --project <project> --repair <check>`
  re-runs the one step behind a failing check, after you type the check id.
- **Stop.** Exit 5 means refused, and nothing changed. Never retry with
  another project or another flag to get round it (`plugin/commands/install.md`).

| Exit | Meaning (`SI/exitcodes.py`, `ExitCode`) |
|---|---|
| 0 | every step met |
| 1 | a step failed; the message names it |
| 2 | usage |
| 3 | a check could not run, or (for `--status` and doctor) something is not met |
| 4 | paused: a human step, or a wait past its bound |
| 5 | refused |

### Phase A: the release candidate

**A1. Publish a release candidate.**

- **Who:** the owner. The epic says a person runs the publish for the first
  release.
- **Do:** follow [release.md § Each release](release.md#each-release) steps 1
  to 3 with a candidate version:
  `gh workflow run publish-release.yml -f version=<V>-rc.<n> -f commit=<sha>`,
  then approve the `release` environment.
- **Worked when:** the run is green. It wrote `channels/rc.install.sh`,
  `channels/rc.marketplace.json` and `channels/rc` (which holds exactly
  `<V>-rc.<n>`), and read them back. It never wrote the root `install.sh`.
- **Log:** the version, the commit, the run's URL.
- **If it fails:** before approval, dispatch again with a corrected input.
  During the publish, the version is burned; publish `-rc.<n+1>`
  (release.md § If the run fails).
- **Checked against:** release.md § Each release; `scripts/release/publish.sh`
  header.
- **Note:** this is the one step that runs from the repository's CI, not from
  the product. It is the publisher's act, not the installer's.

### Phase B: the dry run against an empty project

**B1. Install the command line from the candidate.**

- **Who:** the owner, on the machine that will run proof install 1.
- **Do:**
  `curl -fsSL https://storage.googleapis.com/swarmcloud/channels/rc.install.sh | SWARM_CHANNEL=rc sh`
- **Worked when:** it prints
  `swarmcloud: installed SwarmCloud <V>-rc.<n>: swarm and sc`, then
  `Next: swarm install`, or the full path to `swarm` if this terminal cannot
  find it yet.
- **Log:** the printed version; whether `swarm` was on `PATH` in a new
  terminal; whether uv was installed.
- **If it fails:** a SHA-256 mismatch installs nothing ("does not match
  SHA256SUMS ... nothing was installed"). Log it and stop: the candidate is
  bad.
- **Checked against:** [install.md § Step 1](../install.md#step-1-install-the-command-line);
  `install/install.sh`.
- **Why the candidate's script:** the root `install.sh` is written only by a
  final release, so it cannot install a candidate (release.md § Each release
  step 3).
- **Fixed (K4):** `docs/design/day-one-plan.md` §10 used to show the root
  `install.sh` for P4; it now shows `channels/rc.install.sh`.

**B2. Run the dry run.**

- **Who:** the owner.
- **Do:**
  `swarm install --project <proof-project-1> --env prod --version <V>-rc.<n> --domain <host-1> --dry-run`
  (or `/sc:install dry-run --project <proof-project-1>` from Claude Code with
  the plugin, which runs `swarm install --json-events --dry-run`).
- **Worked when:**
  - It exits **0**, or **4** only for the human steps it lists.
  - It prints, per stage, `Stage N` and a `plan <id> -- <summary>` line for
    each step not yet met.
  - It writes nothing to any project or to install state: no state, no cache, no lease. Stage 0 may still set up this machine (the Cloud SDK, sign-ins that write the machine's Application Default Credentials). The verb opens the store
    read-only (`SI/verbs/install.py`, `_RunStore.read_only`).
  - Stage 0's read-only preflight steps do run and may ask their questions.
    Answering them records nothing (`SI/engine.py`, the dry-run branch for a
    `read_only` stage 0 step).
- **Log:** the exit code; every `plan` line for stages 1 to 15 (attach the
  output, run with `--json-events` if you want it machine-readable); every
  question it asked; anything printed that you did not expect.
- **If it fails:** exit 1 or 3 names the step. Fix what it names (usually a
  right or a sign-in) and run the dry run again. Exit 5 means a flag disagrees
  with a recorded install: you named a project that already holds one.
- **Checked against:** the epic's "What you expected";
  [`day-one-plan.md` §10](../design/day-one-plan.md#10-the-proof-brief-10-what-each-wave-leaves-ready)
  ("P3's exit check"); [install.md § When it stops](../install.md#when-it-stops)
  (`--dry-run`); `plugin/commands/install.md`.
- **Not checked:**
  - The plan's "no write, checked by the project's audit log showing no
    mutating call". The product has no command that reads an audit log. If
    `<proof-project-1>` does not exist yet, the dry run cannot write to it at
    all, and the check is trivially met.
  - How fully the dry run plans stages 3 to 15 for a project with no Terraform
    backend. The design says it prints what the module would create instead of
    a Terraform plan (`docs/design/day-one-install.md` §2.4).
  - Which target to dry-run against is [decision O2](#8-open-decisions-for-the-owner).

### Phase C: proof install 1 (cold, product only, every snag logged)

Use only [install.md](../install.md), what the terminal prints and what the
console shows. Keep the snag log open. Write one row per step, with the time
each step started and ended.

**C1. Start the install.**

- **Who:** the owner.
- **Do:** `swarm install`, with no flags, as install.md § Step 2 shows. This
  is the cold path. An rc CLI with no `--version` installs the candidate it
  came as (`SI/steps/preflight.py`, `ReleaseStep` docstring).
- **Worked when:** it first prints what it needs, a Google organisation
  (`SI/verbs/install.py`, `_say_organisation`). Then it asks:
  - `? Google Cloud project to create or use:`
  - `? Domain for the console (a hostname you control):`
  - `? Region [<default>]:`

  Then it prints a summary ending `Continue? [Y/n]`.
- **What to type:** answer `<proof-project-1>` (a new id), `<host-1>`, and the
  region, then Enter. Answer with a brand-new id only, never an existing
  project's: the summary at C1 does not show whether the id exists. The check
  comes at the stage 1 approval (C3), which shows `parent ...` for a project
  to be created and `(exists)` for one that already is (section 2.3, gap 1).
- **Log:** the questions exactly as printed, and the summary. Note whether the
  release line shows the candidate.
- **If it fails:** any answer other than Enter, `y` or `yes` prints
  `Stopped; nothing was changed.`, exit 0. Run it again.
- **Checked against:** `SI/verbs/install.py` (`_ask_missing`, `_ask`);
  [install.md § Step 2](../install.md#step-2-swarm-install).

**C2. Stage 0: preflight.**

- **Who:** the owner; an organisation admin if a right is missing.
- **Do:** answer what it asks, in order (install.md § Stage 0, items 1 to 12):
  1. offer to install the Google Cloud SDK if it is missing;
  2. two Google sign-ins in the browser, as the organisation's admin;
  3. if no organisation is seen, the Cloud Identity Free sign-up;
  4. the alert email, the GitHub organisation login `<example-org>`, and the
     billing account (a numbered choice);
  5. the region, then the region typed again ("The Firestore location can
     never change");
  6. the parent organisation or folder for the new project;
  7. the domain, only if it cannot be read;
  8. this machine's address, only if the two echo services disagree;
  9. organisation policies;
  10. quotas;
  11. `Who is this SwarmCloud for? [1] just me  [2] a team [1]:`: **answer 2,
      a team** (O3), and a small number of people;
  12. the CVE acceptances of the release;
  13. two optional questions install.md does not list (`SI/steps/infra.py`,
      `VoiceItChoiceStep`, `VoiceSpeechChoiceStep`): `turn Voice it on`, and,
      only after a yes, Voice it's own speech engine. **Answer no to both
      (Enter, the default).** A yes adds an L4 speech machine and its pieces
      (section 2.1), needs GPU quota and costs more (section 2.5); the proof
      does not cover it.
- **Cannot be undone:** item 3, creating a Cloud Identity organisation and
  verifying its domain at the registrar; no step undoes it (section 2.4).
- **Worked when:** every stage 0 step prints `done` (or `check ... met`), and
  no `UNMET`.
- **Log:** each page it opened and why; how long the organisation, billing and
  quota waits took; the sizing table and every row you changed; the budget it
  proposed.
- **If it fails:**
  - **A missing right:** shows UNMET, names the role, and opens the IAM page.
    Get the grant; the installer goes on by itself.
  - **A wait past its bound:** exits 4. Run `swarm install` again.
  - **An expired CVE acceptance:** stops the install. Log it and stop: the
    candidate needs a re-publish.
- **Checked against:** [install.md § Stage 0](../install.md#stage-0-preflight);
  `SI/steps/preflight.py` (`OrganizationStep`, `InputsStep`, `RegionStep`,
  `RightsStep`, `OrgPoliciesStep`, `QUOTAS`, `ReleaseStep`).

**C3. Stage 1: foundations, and the approval of cost.**

- **Who:** the owner.
- **Do:**
  0. **First check the stage 1 summary and approval:** it must say the project
     will be created (`parent ...`), not `(exists)`. If it says `(exists)`, do
     not type the project id: type anything else and stop (section 2.3, gap 1).
     At an `is taken` prompt type only a brand-new id, never one that exists.
  1. Answer `Monthly budget for <proof-project-1>, in US dollars (Enter keeps it) [<amount>]:`.
  2. Read what it will create and the cost estimate, then type the project id
     at `Type the project id (<proof-project-1>) to approve`.
  3. If it asks `Type remove-editor to remove roles/editor from ...`, type
     `remove-editor` **only if the approval said the project was created**. For
     an existing project type anything else and stop.
- **Worked when:**
  - the project exists, labelled `managed-by=swarm-install`;
  - the APIs are enabled;
  - the budget `swarm-<proof-project-1>` exists;
  - `gs://swarm-tfstate-<proof-project-1>` exists;
  - `foundations.config` reports that the never-touch list "names 0
    resource(s)".
- **Log:** the cost figure shown; whether the Terms of Service page opened;
  the never-touch list's count. **It must be 0. Anything else is a snag that
  blocks the uninstall later:** stop and log it.
- **If it fails:**
  - A taken id asks for another; every answer given so far is kept.
  - A missing right opens the IAM or billing page; the installer goes on once
    it sees the change.
  - Exit 4: run `swarm install` again.
- **Checked against:** [install.md § Stage 1](../install.md#stage-1-foundations);
  `SI/steps/foundations.py` (`ProjectStep`, `BudgetStep`, `ConfigStep`,
  `MONTHLY_FLOOR_USD`).

**C4. Stage 2: the front door (the DNS record).**

- **Who:** the owner, at the DNS provider.
- **Do:** at the DNS provider, set the `A` record the terminal prints after
  `Set this A record at your DNS provider:`.
- **Worked when:** the wait line for `edge.dns` stops. The engine polls Google
  Public DNS until it answers with the reserved address and nothing else.
- **Log:** the time from saving the record to the wait ending.
- **If it fails:** past 60 minutes it exits 4. Check the record on the lookup
  page it opened, then run `swarm install` again.
- **Checked against:** `SI/steps/edge.py` (`DnsStep`, module docstring);
  [install.md § Stage 2](../install.md#stage-2-the-front-door) item 3.
- **Fixed (K1):** install.md § Stage 2 item 1 used to say the OAuth brand
  and the Desktop client are made here, in the terminal, with a hidden secret
  prompt. The code moved them to the console, and install.md now says so. `edge.brand` and
  `edge.desktop-client` are stage 13 waits, satisfied by Admin › Setup's
  **Sign-in client** step (`SI/steps/edge.py`, `BrandStep`, `DesktopClientStep`;
  [`day-one-plan.md` §5.6](../design/day-one-plan.md#56-the-sign-in-client-step-the-brand-and-the-desktop-client-move-to-the-console)).
  Expect no brand prompt in stage 2. If one appears, log what it said.

**C5. Stages 3 to 7: roles, images, the platform.**

- **Who:** the owner, waiting.
- **Do:** nothing, unless a pass would destroy something:
  `<pass> would DESTROY <n>: ... Type the project id (<proof-project-1>) to approve, anything else refuses:`.
  On a fresh project a destroy is unexpected. **Do not type the id.** Log it
  and let it refuse.
- **Worked when:** `done` for `bootstrap.pass-1`, `images.registry`,
  `images.copy`, `infra.pass-1`, `secrets.platform-keys`, `infra.pass-2` and
  `bootstrap.pass-2`.
- **Log:** each stage's start and end time (the docs give none); any destroy
  prompt.
- **If it fails:**
  - Exit 1 names the step. Run `swarm install` again.
  - If it fails twice in the same place, run
    `swarm doctor --project <proof-project-1>` and attach its output to the
    snag.
- **Checked against:** install.md § Stage 3, § Stage 4, § Stages 5 to 7;
  `SI/terraform.py` (`run_pass`, `review_plan`).

**C6. Stage 8: the console's address, and identity.**

- **Who:** the owner, as an organisation admin.
- **Do:**
  1. Answer
     `Create swarm-admins@<example.com> and swarm-team@<example.com> through the Groups API, you as owner? (yes/no) [yes]:`
     with Enter.
  2. At the admin-role question that ends
     `Type ASSIGN to approve, or delegation for the fallback:`, type `ASSIGN`.
  3. If stage 0 deferred the Directory scopes, no consent page opens: the
     Desktop client is made only in the console's **Sign-in client** step,
     which stage 13 waits for. The groups are created with stage 0's
     sign-in. After `ASSIGN`, `https://admin.google.com/ac/roles` opens:
     assign Groups Reader to swarm-api's account there
     (`SI/steps/identity.py`, `GroupsReaderRoleStep`).
- **Cannot be undone:** the Workspace groups and the org-wide Groups Reader
  assignment (or delegation) stay after `swarm uninstall` (`workspace-groups`;
  section 2.4).
- **Worked when:** the console answers behind IAP; the groups exist;
  swarm-api proves it can read the admins' group.
- **Log:**
  - whether the Groups API created the groups or the installer fell back to
    `admin.google.com`;
  - whether the admin-role assignment worked through the API. The plan
    names this as "to be proven in P4"
    ([`day-one-plan.md` §1.1](../design/day-one-plan.md#11-wave-0-foundations-the-debranding-sweep-and-the-held-terraform-groundwork-9-lanes));
  - how long the certificate took.
- **If it fails:**
  - **The certificate:** past an hour the installer opens the SSL certificates
    page. A status other than ACTIVE means DNS is not right yet.
  - **The grant:** if Google refuses it, follow the path the installer names
    at `https://admin.google.com/ac/roles`.
  - Exit 4: run `swarm install` again.
- **Checked against:**
  [install.md § Stage 8](../install.md#stage-8-the-consoles-address-and-identity);
  `SI/steps/identity.py`; `SI/steps/edge.py` (`ConsoleAnswersStep`).

**C7. Stage 10: the cluster.**

- **Who:** the owner, waiting.
- **Do:** nothing.
- **Worked when:** the admission policies, `swarm-system` and the workloads
  roll out.
- **Log:** how long it took.
- **If it fails:** a rollout past 5 minutes fails the step. Run
  `swarm install` again; if it fails again, run `swarm doctor`.
- **Checked against:** [install.md § Stage 10](../install.md#stage-10-the-cluster);
  `SI/steps/cluster.py`.

**C8. Stage 13: the alerts, and the hand-off to the console.**

- **Who:** the owner.
- **Do:** leave the terminal running. It opens `https://<host-1>/admin/setup`
  and prints:
  `Finish setup in the console's Admin › Setup (opening it now), then press its "Hand over to swarm install" button: that is what this terminal waits for, for up to 6 hours. ...`
- **Worked when:** the wait line
  `waiting on handoff.console: ... -- <N> s left of the bound` appears and
  stays.
- **Log:** the exact text printed.
- **If it fails:**
  - If the console does not answer behind IAP, it opens the certificates page
    and exits 4. Run `swarm install` again.
  - If you close the terminal, Admin › Setup shows `swarm install is not running`
    and stays usable. Run `swarm install` again later to pick up the wait.
- **Checked against:** `SI/steps/handoff.py` (`HandoffStep`, `HAND_OVER`,
  `CONSOLE`); [install.md § Stage 13](../install.md#stage-13-the-hand-off-to-the-console).
- **Fixed (K8):** install.md used to quote an older wording of this message
  that did not name the button; it now quotes the message as the code
  builds it.

**C9. Admin › Setup in the console.** Sign in with your organisation account.
The page shows a checklist of eight steps beside the selected step's panel,
re-read every 5 seconds, never marked by hand
(`apps/swarm-ui/src/consolefinish/ChecklistFinish.tsx`, header comment and
`finishLines`). Work the lines in this order:

| # | Line | Who | Do (labels as the console draws them) | Worked when |
|---|---|---|---|---|
| C9.1 | **Cloud** | nobody | nothing: `swarm install` did it | `swarm install did the cloud part: stages 0-8, 10 and 13, ...` |
| C9.2 | **Sign-in client** | owner | Open Google's **Branding** page: App name `SwarmCloud`, support email. Open **Audience**: choose **Internal**. Open **Desktop client**: create a client of type Desktop app named `SwarmCloud sc`, and keep Google's dialog open. Type the **Client id** and **Client secret** into the console's form yourself, then **Save**. Google shows the secret only in its dialog. | the line says the client is on IAP's programmatic-clients list. **Cannot be undone:** the OAuth brand has no delete method and goes only with the project (section 2.4). |
| C9.3 | **GitHub App** | owner, on GitHub | **Create the GitHub App** (GitHub opens with the manifest), then **Install on `<example-org>`** and choose the repositories. **Check again** checks at once. | `<app> · installed on <example-org> · self-check passed`. **Cannot be undone by us:** the App's name is global and only the owning account can delete it (section 2.4).  The self-check waits for infra pass 3, which the terminal applies by itself (stage 9). |
| C9.4 | **First team** | owner | **Workspace group** is filled with `swarm-team@<example.com>`. Press **Create the team**. Use **Invite** to add `<teammate>` to the group in Google Admin. | the workspace is ready; `<teammate>` no longer shows `waiting for <email> to join` |
| C9.5 | **Claude accounts** | owner and each subscription owner | Give an **Account label** and **Its owner**. Use **Sign in my Claude account** for your own subscription; use **Make a sign-in link** for someone else's (one-time, 24 hours, only for that owner). The owner pastes the code Claude shows into the product's page, never into a chat or a log. | the count of signed-in accounts reaches stage 0's proposal |
| C9.6 | **Smoke agent** | nobody | nothing; **Retry the smoke** if it failed | `succeeded`. It runs as swarm-api's own account and opens no pull request. |
| C9.7 | **Verify** | owner | Press **Hand over to swarm install**, offered only once the console's steps are done. The console starts its two runs, each opening a pull request on a granted repository. | the terminal takes over (C10); the line shows the checks' progress |
| C9.8 | **Send to your team** | owner | locked until verify passes (`Locked until verify passes: the line works only on a deployment verify has proven.`); then **Copy** the one line | `the plugin line is ready` |

- **Log for each line:** start and end time; who acted; any page that did not
  say what to do; every wait longer than its typical time.
- **If it fails:**
  - **GitHub App:** a failed self-check offers **Retry**.
  - **Smoke agent:** a failed smoke offers **Retry the smoke**.
  - **Verify's console runs:** each is restarted up to twice. After both
    retries, read the run for why and log it.
  - **A line that never completes:** run `swarm doctor --project <proof-project-1>`
    in another terminal (read-only) and attach the output.
- **Checked against:**
  - `apps/swarm-ui/src/consolefinish/ChecklistFinish.tsx` (`LineKey`,
    `finishLines`, the locked text);
  - `apps/swarm-ui/src/AdminSetupSteps.tsx` (`signInRow`, `githubRow`,
    `teamRow`, `accountsRow`, `SendToTeam`);
  - `apps/swarm-api/swarm_api/routes/setup.py`;
  - [install.md § Step 3](../install.md#step-3-admin--setup-in-the-console);
  - [`day-one-plan.md` §5](../design/day-one-plan.md#5-admin--setup-and-health-d26-d27).
- **Fixed (K1, K2):** install.md § Step 3 used to say the strip shows six
  steps and the checklist seven rows, with no Sign-in client row. It now
  describes the eight lines the console draws, Sign-in client included, plus
  the optional ninth, **Add a plugin**, which never gates.

**C10. Stage 14: verify.**

- **Who:** the owner, in the terminal.
- **Do:**
  - If the deployment's context is not signed in, a browser opens once
    (`swarm --context <context> login`); sign in.
  - If your own GitHub account is not connected, it runs
    `swarm sc setup --context <context>` and opens GitHub. Connect, within
    10 minutes.
- **Worked when:**
  - the scheduler targets, the images, the reconciler and Firestore all pass;
  - all seven runs pass: from the plugin, an agent and a workflow (each
    opening a pull request), an agent on GKE, a live-browser agent and a
    code-intelligence read; and the console's two runs;
  - the last infra pass removes this machine's address.
- **Log:** each check's time; the pull requests the runs opened (links only).
- **If it fails:** a failed check is named on the console's verify line. Fix
  what the terminal printed, then run `swarm install` again; it re-runs verify
  from the failed check.
- **Checked against:** [install.md § Stage 14](../install.md#stage-14-verify);
  `SI/steps/verify.py`.

**C11. Stage 15: READY.**

- **Who:** the owner.
- **Do:** read the end of the run, then run
  `swarm install --status --project <proof-project-1>`.
- **Worked when:**
  - the run ended with `READY: SwarmCloud answers at https://<host-1>`;
  - for a team install, a `Teammates:` line with the release's
    `marketplace.json` URL followed;
  - `--status` prints `Ready.`, the console URL, the teammates line and
    `Health:     swarm doctor`, and exits 0;
  - Admin › Setup says `Setup is complete`.
- **Log:** the total wall-clock time from C1.
- **If it fails:** `--status` exits 3 and prints
  `Not finished. Resume with: swarm install --project <proof-project-1> --env prod`.
  Do that.
- **Checked against:** `SI/steps/handoff.py` (`ReadyStep`, `teammates_line`);
  `SI/verbs/install.py`; [install.md § Stage 15](../install.md#stage-15-ready).

**C12. Doctor.**

- **Who:** the owner.
- **Do:** `swarm doctor --project <proof-project-1>`
- **Worked when:**
  - it exits 0;
  - the summary reads `N met, 0 unmet, 0 unknown, ...`;
  - the last line says whether the hourly doctor is on. It should say "the
    hourly doctor is on" (`SI/tfvars.py`, `SCHEDULED_DOCTOR_SHIPPED` is
    `True`); "off" is a snag.
  - **Admin › Setup › Health** shows the hourly findings after the next hour.
- **Log:** the summary line; every non-`met` line.
- **If it fails:** each unmet line prints its fix, a
  `swarm install --repair <check>`. Run it (you type the check id), then
  doctor again.
- **Checked against:** `SI/verbs/doctor.py`; `SI/doctor.py`
  (`hourly_doctor`); [install.md § When it stops](../install.md#when-it-stops).

**C13. A second team (the plan's P4 line).**

- **Who:** the owner.
- **Do:** either Admin › Setup's **Add a team**, or
  `swarm team add <second-group>@<example.com> --project <proof-project-1>`.
  The group must exist in Google Admin first.
- **Worked when:** the terminal ends
  `Team <team> is ready: namespace ..., and a green smoke agent (<task>).`, or
  the console shows the team ready. The team's GitHub credential is entered
  only through the one-time link it opens, never typed in the terminal.
- **Log:** which path you used; whether the console showed the equivalent CLI
  line (the epic says it should).
- **If it fails:**
  - Exit 4: run the same `swarm team add` line again.
  - Exit 1 prints `swarm team add: ...`; log it.
- **Checked against:** [install.md § More teams](../install.md#more-teams);
  `SI/verbs/team.py`; `SI/team.py`;
  [`day-one-plan.md` §10](../design/day-one-plan.md#10-the-proof-brief-10-what-each-wave-leaves-ready).

**C14. The teammate's path** on `<host-1>`: [section 5](#5-the-teammates-path-from-the-plugin-to-a-merged-pull-request).
In proof install 1 it is optional, but it finds snags early. In proof install
2 it is required.

**C15. Uninstall proof install 1, keeping the project.** Follow
[section 6](#6-rollback-uninstall-what-it-removes-what-it-leaves). At the last
question, which asks you to type the id again to delete the project, **type
anything else**: the reinstall in D4 needs `<proof-project-1>`. Note the date.
The reinstall must start within 7 days (section 2.4, custom role ids), so **do
D4 now, before C16**: the clock started at this uninstall. Carry on to D4
straight away, reinstalling the release recorded for proof 1 (decision O8).

**C16. Triage.** File each snag as its own issue
([`.github/ISSUE_TEMPLATE/`](../../.github/ISSUE_TEMPLATE/); one issue per snag,
as the epic asks). Mark each row in the log **fixed** (with the pull request)
or **accepted** (with the owner's initials and date). A fix ships in a new
candidate: repeat A1 and B with `-rc.<n+1>` before D1. Do C16 only after D4
has finished; D4 does not wait for fixes. If D4 would start later than day 7
from C15, the reinstall proof has not been met (see D4).

### Phase D: proof install 2, the reinstall, the upgrade

**D1. Proof install 2, from the latest candidate, with zero snags.**

- **Who:** the owner, and the teammate in D3.
- **Do:** repeat C1 to C13 into `<proof-project-2>` with `<host-2>`, from the
  candidate that carries every fix. Refresh the CLI first with B1, or
  `swarm upgrade` installs the right CLI when it is time.
- **Worked when:** every step goes through with no snag.
- **Log:** a fresh log (its own header); the same rows.
- **If it fails:** a snag here fails the zero-snag bar. Log it, then decide
  under O5 whether to fix it and run proof 2 again.
- **Checked against:** the epic's "What you expected"; `day-one-plan.md` §10
  (P5).

**D2. Doctor on proof 2.** As C12, against `<proof-project-2>`.

**D3. The teammate's path on `<host-2>`.**
[Section 5](#5-the-teammates-path-from-the-plugin-to-a-merged-pull-request),
in full, done by `<teammate>`.

**D4. Reinstall into the uninstalled project 1.** (Run it straight after C15,
ahead of D1 to D3; it is listed here with the second proof's phase.)

- **Who:** the owner.
- **Do:** `swarm install --project <proof-project-1> --domain <host-1>`. Do it
  straight after C15, before C16, and in any case within 7 days of C15; write
  the deadline date next to C15's date in the log.
- **Worked when:** the run adopts what survived instead of recreating it
  (`SI/adopt.py`, module docstring): the KMS ring, the undeleted custom roles,
  the brand and Desktop client (taken from the remains record's defaults), and
  the GitHub App by its slug. The run then reaches `READY` again.
- **Log:**
  - each adopted resource;
  - any step that asked again for something the remains record holds;
  - whether the old GitHub App was reused or a new one was asked for;
  - any stop on a Cloud Logging bucket id: the buckets Terraform destroyed
    (`workspace-apply`, `worker-logs`) stay pending deletion for 7 days and
    may collide with the reinstall (section 6, "Also left"; not checked);
  - the Firestore wait if the old database id was still being released
    (`SI/adopt.py`, `FIRESTORE_ID`, bound 15 min).
- **If it fails:**
  - A "custom roles ... were deleted more than 7 days ago" stop means the
    window passed. Log it. Wait for the date it prints (day 37) and run D4 then.
    A reinstall into a new project does **not** prove the epic's "reinstall
    into the first, uninstalled project": do not substitute it. Ask the owner
    (decision O8).
  - Any other failure: resume.
- **Checked against:** `SI/adopt.py`; `SI/steps/bootstrap.py`
  (`held_roles`); `SI/remains.py` docstring ("A reinstall never depends on
  it").
- **Not checked:**
  - **Which release the reinstall installs.** The uninstall keeps the local
    state file, which records proof 1's release, domain, region and
    environment (R2). A `--version`, `--domain`, `--region` or `--env` that
    disagrees with a recorded value is refused with exit 5 (`SI/verbs/install.py`,
    `_refusal`). With no `--version`, the recorded release is reused
    (`SI/steps/preflight.py`, `ReleaseStep._version`). So D4 probably
    reinstalls proof 1's candidate, not the latest one. Whether the uninstall
    clears the recorded release was not traced. If the reinstall refuses the
    newer candidate, log it and see [decision O8](#8-open-decisions-for-the-owner).
    Never move the state file by hand: that is outside the product.
  - **How a reinstall treats a GitHub App whose name is already taken** in
    `<example-org>`. The code reads the App's slug from the remains record,
    but this runbook did not trace the console's GitHub App step for a
    reinstall.

**D5. Publish the first final release (not yet promoted).**

- **Who:** the owner.
- **Do:** A1 with `version=<V>`, from the same commit as the last candidate or
  a later one.
- **Cannot be undone:** publishing `<V>` burns the version: it is never
  overwritten, and a failed publish burns it too (section 2.4).
- **Worked when:** the run is green, under `releases/<V>/`.
- **Log:** the commit, the run URL.
- **If it fails:** as A1.
- **Checked against:** release.md § Each release.
- **Not checked:** release.md says a final release's publish writes the root
  `install.sh`, and also that `--promote` moves it. This runbook did not
  settle which one changes the root script, so do not point anyone at the
  root `install.sh` between D5 and E2.

**D6. Upgrade proof 2 from the candidate to the first release.**

- **Who:** the owner.
- **Do:** `swarm upgrade --to <V> --project <proof-project-2>`.
  - If the installed CLI is not `<V>`, it asks you to type the project id to
    install `<V>`'s CLI, then runs under it.
  - It shows the plan. Approve it with
    `Upgrade <proof-project-2> from <V>-rc.<n> to <V> as planned above? Type the project id (<proof-project-2>) to approve, anything else refuses:`,
    and separately approve any IAM change.
- **Worked when:**
  - it exits 0;
  - `swarm install --status --project <proof-project-2>` shows release `<V>`;
  - `swarm doctor` exits 0;
  - the teammate's plugin, still on the candidate's pinned marketplace, still
    runs one agent (the plan's skew check, `day-one-plan.md` §7.3).
- **Log:** the drain time; whether it rolled back; the doctor summary.
- **If it fails:**
  - Run the same command again; it resumes.
  - Exit 5 means refused (a downgrade, or an IAM change not approved).
  - Exit 4 means a wait gave up: resume.
- **Checked against:** `SI/verbs/upgrade.py`; `SI/upgrade.py`;
  [install.md § Upgrade](../install.md#upgrade);
  [`day-one-plan.md` §7.3](../design/day-one-plan.md#73-the-manual-pre-release-install-and-upgrade-test-d22).

### Phase E: ship

**E1. Record the manual test.**

- **Who:** the owner, at a terminal with rights in the release home.
- **Do:** `scripts/release/publish.sh --record-test <V> --record tested.json`.
  `tested.json` summarises D1 to D6. Its shape is in the header of
  `scripts/release/publish.sh`.
- **Worked when:** it writes `releases/<V>/TESTED` once. A record that is not
  a pass is refused.
- **Log:** the record's steps.
- **If it fails:** the message names the field.
- **Checked against:** release.md § Each release step 4.

**E2. Promote the stable channel by hand.** This is the typed act.

- **Who:** the owner.
- **Do:** `scripts/release/publish.sh --promote <V>`, only after the
  checklist in section 7 is signed.
- **Cannot be undone:** promoting makes `<V>` the public stable release.
  `release.md` gives no un-promote. If the promoted release is bad, do not
  try to reverse it: stop, tell the owner, and publish a fixed version and
  promote that (not checked: whether `--promote <earlier version>` is
  accepted).
- **Worked when:** `marketplace.json`, `install.sh` and, last,
  `channels/stable` are moved and read back.
- **Log:** the time.
- **If it fails:** without a passing `TESTED` it is refused. Do E1.
- **Checked against:** release.md § Each release step 4.

**E3. Uninstall proof 2 and the reinstalled project 1**, or keep one as the
first live deployment. That is decision O6. Either way, follow
[section 6](#6-rollback-uninstall-what-it-removes-what-it-leaves) for each one
you remove, and type the id twice to delete the project when you no longer
need it.

---

## 5. The teammate's path: from the plugin to a merged pull request

The teammate is `<teammate>`, not the owner. They are in the team group (C9.4
Invite), and the owner has copied the **Send to your team** line (C9.8).
Every snag goes into the same snag log, under "Teammate".

**T1. Install the plugin, with one value.**

- **Who:** the teammate, in a terminal.
- **Do:** run the line the console gave, as copied:

  ```
  claude plugin marketplace add https://storage.googleapis.com/swarmcloud/releases/<version>/marketplace.json && claude plugin install sc@swarmcloud --config deployment_url=https://<host>
  ```

- **Worked when:** Claude Code lists the `sc` plugin as installed. In a new
  Claude Code session, `/mcp` shows the plugin's `swarmcloud` server
  connected.
- **Log:** the Claude Code version; any question Claude Code asked; whether
  the `--config` value was taken.
- **If it fails:** a skew message names the fix (an older plugin is updated
  with `claude plugin update sc@swarmcloud`). Log anything else and stop the
  teammate's path; the owner decides.
- **Checked against:**
  - `apps/swarm-ui/src/AdminSetupSteps.tsx` (`pluginLine`,
    `teammateMarketplace`);
  - [install.md § Step 3](../install.md#step-3-admin--setup-in-the-console)
    row 7;
  - `scripts/release/marketplace.py` (module docstring, `bridge_args`);
  - `plugin/.claude-plugin/plugin.json` (`userConfig.deployment_url`, the one
    required value);
  - `apps/swarm-mcp/swarm_mcp/discovery.py` (reads
    `https://<host>/.well-known/swarmcloud` for the rest).
- **Not checked:**
  - That this Claude Code version accepts `claude plugin install ... --config`.
    No Claude Code documentation is in the repository, and the plan lists the
    Claude Code installer's behaviour as to be proven
    ([`day-one-plan.md` §6.4](../design/day-one-plan.md#64-the-marketplace-and-the-plugin-bundle-in-the-release-home)).
  - Whether the teammate needs uv on their machine before the plugin's server
    starts. The plugin's server runs through `uv tool run`
    (`scripts/release/marketplace.py`, `bridge_args`).

**T2. Sign in.**

- **Who:** the teammate, in a terminal.
- **Do:** `sc login`. A browser opens; pick the work account in
  `<example.com>`. Then `sc whoami`.
- **Worked when:** `sc whoami` prints the context, the URL `https://<host>`,
  the teammate and their tenant. Plugin tools no longer answer
  `sign-in required for <context>: run sc login`.
- **Log:** whether `sc` was found on `PATH`.
- **If it fails:**
  - `sc: command not found` means the command line is not installed: install.md
    § Step 3 line 8 says so since K9 was fixed, so it is a snag only if the
    teammate could not find that. Log it, then install the
    command line pinned to the deployment's release:
    `curl -fsSL https://storage.googleapis.com/swarmcloud/channels/rc.install.sh | SWARMCLOUD_VERSION=<version> SWARM_CHANNEL=rc sh`
    for a candidate deployment
    ([install.md § Step 1](../install.md#step-1-install-the-command-line);
    `install/install.sh` installs `swarm` and `sc`). Then run `sc login`
    again.
  - An IAP refusal is a snag; log it.
- **Checked against:** `plugin/README.md` § Install (`sc login`, `sc whoami`);
  `plugin/commands/setup.md` (the closing paragraph: `sc login` is the
  person's to type).
- **Not checked:** whether `sc` is on a teammate's `PATH` after T1 alone. The
  plugin's own server runs from the release wheel through `uv tool run`, but
  every teammate-facing `sc login` line in the docs assumes `sc` is installed.
  The marketplace entry for the repository's own plugin says the `sc` skill
  needs `sc` and `swarm` "on PATH, where SwarmCloud's install puts them"
  (`.claude-plugin/marketplace.json`). This is
  [decision O3](#8-open-decisions-for-the-owner).

**T3. Per-person setup.**

- **Who:** the teammate, in Claude Code.
- **Do:** `/sc:setup`, then follow its checklist in order:
  1. **Where it stands.**
  2. **Workspace**: request it if asked; an admin approves it.
  3. **Claude account**: the console's Capacity › Accounts, or ask an admin
     to lend one.
  4. **Connect GitHub**: an authorize link, valid 10 minutes, once.
  5. **Owners**: enable `<example-org>`.
  6. **Repositories**: grant write on the target repository.
  7. **Verify**: an opt-in push test that creates and deletes a branch
     `swarmcloud/onboarding-check-<nonce>`.
  8. **Done.**
- **Worked when:** `sc setup status` exits 0, and the checklist's `complete`
  is true.
- **Log:** every item that did not go through on the first try.
- **If it fails:**
  - A token fallback (`sc setup token --owner <example-org>`) is the
    teammate's to type, never Claude's.
  - `WORKSPACE_NOT_READY` means the workspace request is waiting for an admin
    (the owner approves it).
- **Checked against:** `plugin/commands/setup.md`;
  [onboarding-acceptance.md § Check 2](onboarding-acceptance.md#check-2-every-step-shows-its-verified-state)
  (exit 0 ready, 3 not).

**T4. Give it a task that ends in a pull request.**

- **Who:** the teammate, in Claude Code.
- **Do:** open an issue in the target repository that asks for a small,
  safe change, then run `/sc run --issue <example-org>/<repo>#<n>`.
  - The plugin says it is about to plan the issue run, then returns the run
    id. A run whose plan needs approval stops at `PLANNED`.
  - `/sc plan show <run>` shows the plan and its digest. Approve it with
    `/sc plan approve <run>`, or in a terminal with `sc plan approve <run>`,
    which asks you to type `approve`.
- **Worked when:** the run opens a pull request on `<example-org>/<repo>`,
  authored as the teammate, and reads its CI. It spends up to three fix rounds
  by default.
- **Log:** the run id, the pull request link, its author, how long it took.
- **If it fails:** `/sc status <run>` and `/sc runs` show where it stands.
  `/sc:check` reports stuck or stale work. Log the run id; never paste a token
  or a log line that holds one.
- **Checked against:**
  - `plugin/skills/sc/reference/issue-runs.md`;
  - `plugin/commands/sc.md` (`run --issue`, `plan approve`, `--auto-merge`);
  - [onboarding-acceptance.md § Check 1](onboarding-acceptance.md#check-1-a-new-user-clones-pushes-and-opens-a-pull-request-as-themselves-in-two-orgs)
    (the pull request is authored by the submitter).
- **Fixed before the proof (K3):** the console's **Send to your team** line,
  the plan's §10 and install.md used to say `/sc:submit`, which does not
  exist. They now say `/sc run --issue owner/repo#N`
  (`apps/swarm-ui/src/consolefinish/ChecklistFinish.tsx`, `SendBody`;
  `apps/swarm-ui/src/AdminSetupSteps.tsx`, `SendToTeam`). A teammate who
  still sees `/sc:submit` is on a console older than the fix: log it.

**T5. Merge it.**

- **Who:** the teammate, or a reviewer with merge rights.
- **Do:** review the pull request on GitHub and merge it there once its checks
  are green (O4).
- **Worked when:** the pull request shows Merged.
- **Log:** the time from T1 to the merge. This is the epic's teammate result.
- **If it fails:** a red check is the run's to fix within its fix rounds. Past
  those, log it.
- **Checked against:** `apps/swarm-api/swarm_api/issueruns.py`
  (`_auto_merge_refusal`).
- **The alternative:** an issue run created with `--auto-merge` merges through
  the `merge` step instead. The `merge` profile has been enabled since
  contract request 47 (the same function).

---

## 6. Rollback: uninstall, what it removes, what it leaves

**R1. Run the uninstall.**

- **Who:** the owner, in a terminal.
- **Do:** `swarm uninstall --project <project>`.
  1. It prints `swarm uninstall removes the SwarmCloud install in <project>, in this order:`
     and the eight steps, each tagged `[as the deployer]`, `[as you]` or
     `[done]`.
  2. It asks
     `This deletes the deployment and its data. Type the project id (<project>) to approve, anything else refuses:`.
     Type the id.
  3. `SWARM_ASSUME_YES` is never read. With no terminal, it is refused.
- **What it removes, in order** (`SI/uninstall.py`, `STEPS`):

  | # | Step | Runs as | Removes |
  |---|---|---|---|
  | 1 | Scheduler | deployer | pauses the Scheduler jobs, then waits up to 30 min for running executions to end |
  | 2 | Kubernetes | deployer | the workspace namespaces, then the cluster policies |
  | 3 | infra | deployer | lifts deletion protection, empties the buckets, destroys the infrastructure |
  | 4 | images | deployer | the copied images |
  | 5 | secrets | deployer | the secrets labelled `managed-by=swarm-install` or `swarm-api`, and `swarm-tenant-*` |
  | 6 | IAM | you | the worker accounts and their bindings, then your grant on the deployer |
  | 7 | bootstrap | you | the Firestore database; the bootstrap pass; the KMS key versions |
  | 8 | state | you | the budget; the `A` record in the project's own zone; writes and prints the remains record; deletes the state and log buckets; then asks whether to delete the project |

- **The project question.** At the end, for a project the installer created,
  it asks:
  `swarm install created the project <project>. Type its id again to delete the project too (Google shuts it down now and deletes it after 30 days; its id is never reusable), anything else keeps it:`.
  - **After proof install 1, keep it:** type anything else (C15).
  - **After the last proof**, type the id if you are done with the project
    (O6).
- **Worked when:** it prints `What remains of the SwarmCloud install in <project>:`,
  each item with `why:` and `to remove it:`, then `Also not removed:` if
  anything was left, then `(written to ~/.config/swarmcloud/install/<project>.remains.json)`.
  It exits 0.
- **Log:** the full remains output (it holds no secret); the time each step
  took.
- **If it fails:**
  - Exit 1 names the step. Run `swarm uninstall` again; it continues from
    that step.
  - Exit 4 means the drain timed out, or you lack a permission steps 6 to 8
    need ("grant them (roles/owner holds every one)"). Fix it, then run again.
  - Exit 5 means not approved, or the state carries a deny-list. The second
    should not happen with an installer-made project: if it does, stop and
    log it.
- **Checked against:** `SI/verbs/uninstall.py`; `SI/uninstall.py` (`STEPS`,
  `Uninstall.prompt`, `Uninstall.refusal`, `OWNED_BY`, `OWNER_PERMISSIONS`);
  [install.md § Uninstall](../install.md#uninstall).
- **Fixed (K7):** install.md used to list seven deletions; it now lists the
  eight steps the code runs.

**What it leaves on purpose.** `SI/remains.py` (`KINDS`, `survivors`) leaves
the following, each with the page or step that removes it by hand:

- `oauth-brand`
- `kms-key-ring`
- `custom-roles`
- `firestore-database` (only if it could not be deleted)
- `desktop-client`
- `github-app`
- `dns-record` (only if it is at a registrar or the delete failed)
- `workspace-groups`

Also left:

- **Firestore daily backups** (`terraform/modules/firestore/backups.tf`,
  retention 7 days). Google's backups doc, quoted there, says deleting the
  database does not delete them. `SI/uninstall.py` and `SI/remains.py` never
  mention them. They hold the deployment's data and bill storage until they
  expire after 7 days;
- **Cloud Logging buckets** Terraform destroyed (`workspace-apply`, in
  `terraform/bootstrap/workspace_deployer.tf`; `worker-logs`, in
  `terraform/infra/log_bucket.tf`). They stay pending deletion for 7 days and
  their ids are held meanwhile, which may collide with a reinstall in D4. Not
  checked against a live project;
- **for a project you kept (C15):** the billing link, the enabled APIs and the
  Google-managed service agents;
- anything in Terraform state without a `managed-by` label;
- the enabled APIs and, if Google made one, a `default` network. No uninstall
  step names these (grep of `SI/uninstall.py`, 2026-11-24); not checked
  against a live project;
- the files on the installer's machine (section 2.3).

**R2. Confirm the uninstall finished.** Do both checks. They see only the remains record: nothing in the product proves the project is empty (decision O7).

1. Run `swarm uninstall --project <project>` a second time. It prints
   `swarm uninstall: <project> is already uninstalled; what remains is in <path>`
   and exits 0, without asking anything (`SI/uninstall.py`, `Uninstall.run`).
2. Every item in the remains record is one of the kinds listed above, and
   nothing else.

**Do not use `--status` or `doctor` as proof of "empty".** Once the state
bucket is about to go, the uninstall makes the local file
`~/.config/swarmcloud/install/<project>.json` the state, and keeps it
(`SI/uninstall.py`, the comment "From here the local file is the state").
`swarm install --status` and `swarm doctor` read the bucket, then that local
copy (`SI/verbs/doctor.py`, `read_state`). So after an uninstall they still
find a state. Their output is not checked for this case: log it.

**Not checked:** a product command that lists every resource left in the
project. There is none. The product's proof of "empty" is the two checks
above. Whether to add a check outside the product, and to add the
existing-project path (section 2.3, gap 1) to the engine's guarantees, is
[decision O7](#8-open-decisions-for-the-owner).

---

## 7. The release sign-off checklist

The owner ticks each line, with the evidence, in the snag log's sign-off
table. Promotion (E2) waits for every tick.

- [ ] The candidate was published by CI, and approved by a person (A1).
- [ ] The dry run exited 0, or 4 only for listed human steps, and wrote
      nothing (B2).
- [ ] Proof install 1 reached `READY` from the product alone, and
      `swarm doctor` exited 0 (C11, C12).
- [ ] Every snag of proof install 1 is filed as its own issue, and is either
      fixed (pull request linked) or accepted by the owner by name (C16).
- [ ] Proof install 1's uninstall printed what remains, and the remains are
      only the documented kinds (R1, R2).
- [ ] Proof install 2 reached `READY` with **zero** snags (D1, D2).
- [ ] A teammate, not the owner, went from the plugin line to a merged pull
      request on proof 2 (section 5, T1 to T5).
- [ ] The reinstall into project 1 reached `READY` (D4).
- [ ] The upgrade from the candidate to `<V>` exited 0; doctor exited 0
      afterwards; the older plugin still ran an agent (D6).
- [ ] The disagreements listed below (K1 to K9, fixed on 2026-11-24) are in
      the candidate under test.
- [ ] `releases/<V>/TESTED` was recorded as a pass (E1).
- [ ] The owner promoted the stable channel by typing `--promote <V>` (E2).
- [ ] What happens to the proof deployments is decided and done (O6), and
      every remains record is kept with the log.

### Known disagreements, fixed before the first proof install

The code and the docs disagreed on these points on 2026-11-24. The owner
decided the same day to fix them first (O5 (a)). Each is fixed; the code was
the truth in every case, so the docs, the plugin's setup command and two
console strings changed, and no behaviour of the installer did.
`tests/unit/install/test_install_doc_agrees.py` holds K1, K2 and K4 to K9 to
the code they were checked against; the console tests hold K3. A
disagreement found later is a snag like any other (C16).

| # | The docs said | The code does (checked against) | Fixed where | How to see it |
|---|---|---|---|---|
| K1 | the brand and the Desktop client are made in stage 2 in the terminal, with a hidden secret prompt | they are made in the console's **Sign-in client** step; the CLI waits in stage 13 (`SI/steps/edge.py`: `BrandStep`, `DesktopClientStep`, `ClientSecretContainerStep`) | install.md § Stage 2 (item 1 is now the secret's empty container), § Stage 13, § Step 3 line 2, and the row in § Steps outside the terminal and the console | § Stage 2 asks nothing; § Step 3 line 2 lists Branding, Audience, Create client and the **Client id** / **Client secret** form. `test_k1_*` |
| K2 | Admin › Setup has six steps and a seven-row checklist | eight lines, plus an optional ninth, Add a plugin (`ChecklistFinish.tsx`: header comment, `finishLines`) | install.md § Step 3: the intro and lines 1 to 9 (Cloud, Sign-in client, GitHub App, First team, Claude accounts, Smoke agent, Verify, Send to your team, Add a plugin) | § Step 3 says `2 of 8 done` and numbers nine lines, the ninth optional. `test_k2_*` |
| K3 | a teammate runs `/sc:submit` | no such command; the paths are `/sc run --issue`, `/sc plan approve`, or plain words (`plugin/commands/sc.md`; `swarm_mcp/sc.py`, `run --issue`) | the console's **Send to your team** line in `ChecklistFinish.tsx` (`SendBody`) and `AdminSetupSteps.tsx` (`SendToTeam`); install.md § Step 3 line 8; `day-one-plan.md` §10 | the console's line reads `Then each teammate runs sc login, then /sc run --issue owner/repo#N`, with the same word count, so the prose ratchet holds. `AdminSetup.test.tsx` and `AdminSetupSteps.test.tsx` (Send to your team), and `test_k3_*` for docs and plugin |
| K4 | P4 starts from the root `install.sh` with `--version <RC>` | a candidate is installed from `channels/rc.install.sh`; only a final release writes the root script (release.md § Each release, step 3) | `day-one-plan.md` §10, the P4 commands and a paragraph after them | the P4 block's first line is `curl -fsSL .../channels/rc.install.sh \| SWARM_CHANNEL=rc sh`. `test_k4_*` |
| K5 | the proposed budget is 500 USD for one person, 300 USD plus run time for a team | `BUDGET_FLOOR_USD = 400` plus the expected agents' run time, rounded up to the next hundred (`SI/sizing.py`, `propose`) | install.md § What it costs | it says 400 USD plus run time: 600 USD for one person, 700 USD for 2 members, 1100 USD for 10. `test_k5_*` reads the floor and the one-person figure from `sizing` |
| K6 | a deleted project's id is held 30 days | the id is never reusable; the project is deleted after 30 days (`SI/uninstall.py`, the project-deletion offer) | install.md § Stage 1, "A taken project id" | it says the id is never reusable. `test_k6_*` |
| K7 | the uninstall deletes in seven steps | eight steps (`SI/uninstall.py`, `STEPS`) | install.md § Uninstall | the list has eight items, the last the budget, the remains record and the state buckets. `test_k7_*` compares the list with `STEPS` |
| K8 | the stage 13 message does not name the hand-over button | the message names **Hand over to swarm install** and the 6 h bound (`SI/steps/handoff.py`: `HandoffStep`, `HAND_OVER`, `CONSOLE`) | install.md § Stage 13 | the printed block is the message as the code builds it. `test_k8_*` reads `HAND_OVER` and the bound from `handoff` |
| K9 | (nothing) | a teammate may need `sc` on `PATH` from `install.sh` before `sc login`: the release's plugin runs the bridge from a wheel URL, which `swarm_mcp/invocation.py` (`source_requirement`) cannot spell, so the `sign-in required` answer (`signin.py`, `sign_in_hint`) says a bare `sc login` | install.md § Step 3 line 8; `plugin/commands/setup.md` (plugin 0.5.91, with its CHANGELOG entry); T2 below | § Step 3 line 8 says `sc` must be on `PATH` and gives both install lines; `/sc:setup` says where `sc` comes from. `test_k9_*`. The product gap (a sign-in line that runs with the plugin alone) is open: questions.json Q1 |

---

## 8. Open decisions for the owner

The same decisions are in the step's `questions.json`. Each has options and a
recommendation, and the runbook above is written to the recommendation.

**O1. Which project the proof installs into.**

- **(a)** A new id that `swarm install` creates.
- **(b)** An empty project you create first.

**Recommendation: (a).** For a project the installer creates, the never-touch
list is written empty. For a project that existed, the installer lists what
it does not own, which includes a `default` network if one exists
(`SI/steps/preflight.py`, `neighbours`), and writes it to the list. Any entry
then makes `swarm uninstall` refuse the whole uninstall
(`SI/uninstall.py`, `Uninstall.refusal`). (b) could therefore leave proof 1
uninstallable. Whether enabling Compute creates a `default` network in your
organisation depends on its policy: not checked.

**O2. Where the dry run (B2) points.**

- **(a)** At `<proof-project-1>` before it exists: nothing can be written, and
  stage 0 is exercised against your real rights.
- **(b)** At a separate empty project you create, with its audit log read
  afterwards outside the product.

**Recommendation: (a).** It meets the plan's "no write" by construction. (b)
is the plan's literal wording, but needs a person to read an audit log, which
no product command does.

**O3. A team install, and the teammate's `sc`.**

- **(a)** Both proofs are team installs (stage 0 answer 2). If T2 finds no
  `sc`, the teammate runs the pinned install line, and that is logged as a
  snag to fix before the release.
- **(b)** The same, but a missing `sc` fails the teammate's proof outright.

**Recommendation: (a).** The teammates line and **Send to your team** exist
only for a team install (`SI/steps/handoff.py`, `teammates_line`). The
install line is part of the product, so the proof can go on, and the snag is
still fixed.

**O4. Who merges the teammate's pull request.**

- **(a)** The teammate or a reviewer merges it by hand on GitHub.
- **(b)** The run is created with `--auto-merge` and the `merge` step merges
  it.

**Recommendation: (a)** for this proof. It needs no extra feature, and it
shows a person accepting the agent's work. (b) is enabled
(`issueruns._auto_merge_refusal`), but it is a second thing to prove.

**O5. The docs disagreements K1 to K9.** Decided by the owner on
2026-11-24: (a), fix them first. Done; see section 7.

- **(a)** File and fix them in a new candidate before proof install 1.
- **(b)** Run proof install 1 as is, and log each one as a snag when met.

**Recommendation: (a).** They are already known. Fixing them first leaves
proof 1 to find what nobody knows yet, and proof 2's zero-snag bar is more
reachable.

**O6. What happens to the proof deployments afterwards.**

- **(a)** Uninstall both and delete both projects.
- **(b)** Keep the upgraded proof 2 as the first live deployment, and
  uninstall the reinstalled project 1.

**Recommendation: (b).** It keeps one deployment on `<V>` for the next
release's upgrade test (plan §7.3), at about 10 USD a day.

**O7. The gaps in "touches nothing else" and "empty again".**

- **(a)** Accept the engine's guarantees as written in section 2.3 (including
  gap 1, the existing-project path in stage 1, held off by the checks at C1
  and C3), and the two checks in R2.
- **(b)** Before the release, add the shell plan guard's wrong-project check
  to the engine's Terraform passes, make stage 1 ask for the typed approval
  before it changes an existing project, and add a product check that lists what an
  uninstalled project still holds.

**Recommendation: (a) for this proof, and file (b) as issues.** Nothing above
project level is in Terraform, and the target project is fixed by the install
state. (b) is a code change, outside this runbook's scope.

**O8. Which release the reinstall into project 1 (D4) proves.**

- **(a)** Reinstall whatever release the kept local state records. Then, if
  that is older than the latest candidate, `swarm upgrade` project 1 to the
  latest candidate before D5.
- **(b)** Require the reinstall to install the latest candidate directly. If
  the product refuses that, it is a snag to fix in the engine before the
  release.

**Recommendation: (a).** The epic asks that "a reinstall into the first,
uninstalled project works"; adoption is what it proves, whatever the release.
The extra upgrade is cheap, and (b) may need a code change before the proof
can finish.

---

## What the independent review changed

An independent review found four major gaps and four minor ones; each is fixed
here and checked against the code it cites.

- **Existing-project path (2.3, C1, C3, O7).** Stage 1 can change an existing
  project (APIs, budget, default compute editor role, state bucket) before the
  never-touch approval, if a typo or the "is taken" retry names it. Stated as
  gap 1; C1 and C3 now say to use a new id and to check for `(exists)` at the stage 1 approval and say when not to type the
  project id or `remove-editor`; added to O7.
- **Voice it (C2, 2.1, 2.2, 2.5).** Stage 0's two Voice it questions are listed,
  with the proof's answer (no) and what a yes adds.
- **Reinstall window (C15, C16, D4).** D4 now follows C15 directly, before
  triage; the "new project" fallback is dropped as not meeting the epic item;
  the wrong O4 reference is now O8.
- **Rollback (section 6).** Firestore backups, pending-deletion log buckets and
  what a kept project keeps are listed; R2 is renamed "Confirm the uninstall
  finished" and says nothing proves emptiness.
- **Irreversible steps** are marked at C2, C6, C9.2, C9.3, D5 and E2.
- **Minors:** the installer's-machine row (ADC, SDK), the dry-run "writes
  nothing" wording, the commit citation, and the billing link staying with a
  kept project.
