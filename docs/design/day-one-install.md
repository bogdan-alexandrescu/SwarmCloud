# SwarmCloud Day One: the install design (P0)

**For a company installing SwarmCloud, the page is [docs/install.md](../install.md)**, written from the code and the manifest; this document is the design behind it.

**Status:** P0 design, for BGD's approval (GATE 0b). Part of #2688.
**Written:** 2026-10-29, against `main` at `17e2ec1`.
**Inputs:** the Day One brief that BGD approved on 2026-10-28 (GATE 0), with its decisions D1 to D24 kept as they are; the dependency inventory in [`day-one/manifest.draft.yaml`](day-one/manifest.draft.yaml); this repository's code, Terraform, runbooks and history; Google's and GitHub's documentation, cited by URL.

This document says **how** Day One is built. The brief says **what** and **why**, and this document does not reopen any of its decisions. Section numbers in the form "brief §N" refer to the brief.

**What was verified and what was not.** Every `path:line` here and in the manifest was checked to exist in the tree at `17e2ec1`. The claims were read at those lines by the research passes, and the claims this design rests on (sections 4 and 5) were read again while writing it. Nothing was applied, planned or probed against a Google Cloud project. Every `check` in the manifest is a proposed read-only probe; none has been run. Statements about Google's or GitHub's behaviour come from their documentation, cited by URL. Where a fact can only be settled by trying it, this document says **to be proven in P3/P4** and names the test.

---

## 1. What P0 found, in short

The manifest holds **433 dependencies**: 362 core and 71 add-on (43 `addon:ci`, 12 `addon:typesafe`, 9 `addon:bright-data-egress`, 4 `addon:tailscale`, 3 `addon:brave-search`). Counts by kind and by provider are in section 3.2. The live browser, code intelligence (the graph service and its indexer) and the in-VPC `swarm-verify` gate are core (D4, brief §5 stage 14); only the CI machinery of D10 is `addon:ci`.

The research summary in brief §3 holds. The code reading added the following points. Each one changes what the build must do.

1. **The bootstrap/infra cycle is mostly an artefact of one `depends_on`.** `module "frontend"` takes the Cloud Run service *names* as plain strings (`terraform/infra/main.tf:692-693`), and nothing in the module needs the services to exist: it has only an address, two serverless NEGs, two backend services, URL maps, a certificate, proxies and forwarding rules (`terraform/modules/frontend/main.tf:99-354`). The cycle that forces `frontend_iap_audiences` to be copied out of an output and back into tfvars (`terraform/infra/variables.tf:569-586`) comes from `depends_on = [module.project_services, module.cloud_run]` (`terraform/infra/main.tf:698`). Section 5.2 removes it.
2. **Two of the three copied values can be computed.** `quota_broker_url` and `swarm_api_url` are Cloud Run URLs (`terraform/infra/variables.tf:588-600`; `terraform/infra/child_tasks.tf:123`). Cloud Run's deterministic URL, `https://<service>-<project number>.<region>.run.app`, can be built from `data.google_project.this.number`, which infra already reads (`terraform/infra/main.tf:305-307`). Section 5.2 has the details.
3. **Bootstrap grants to accounts that only infra creates have one clean fix:** create every platform service account in bootstrap. The ids already come from one shared module, `terraform/modules/service_account_ids`, used by both roots. The repository already made the same move once for custom roles (`docs/runbooks/custom-roles-to-bootstrap.md`). Section 5.3 has the details.
4. **Every environment file names the shared project.** `terraform/environments/dev/dev.tfvars:8` and `terraform/environments/prod/prod.tfvars:13` both set `project_id` to the live project. The committed `terraform/bootstrap/terraform.tfvars` is the live configuration: `adopt_from_infra_states = ["infra/dev"]` at line 16, and a `workspace_apply_image` digest in the live registry at line 192. A release must never ship these files (section 6.2).
5. **The Desktop OAuth client secret lives in a password manager, not Secret Manager.** `docs/runbooks/iap-desktop-client.md:85` and `:235` say so, and the plugin passes it as `oauth_client_secret` user config (`plugin/.claude-plugin/plugin.json:23`, `:88`). Section 9.3 answers the brief's open question 1.
6. **Groups can be read without domain-wide delegation, and the repository tried the wrong layer.** `apps/swarm-api/swarm_api/groups.py:99-138` records that the GCP IAM role `roles/cloudidentity.groupsReader` "changed nothing". Google's supported path is a **Workspace admin role** assigned to the service account. Section 8 has the details.
7. **The tenants freeze passes with no Terraform tenants, but bootstrap refuses that.** The `var.tenants` validation (`terraform/infra/variables.tf:463-468`) admits `{}`. The bootstrap precondition requires at least one tenant in the infra tfvars (`terraform/bootstrap/deployer_service_accounts.tf:100-103`). A fresh install wants zero Terraform tenants, because workspaces are made by `swarm-workspace-apply`. So only the precondition has to change.
8. **Three version pins disagree.** `scripts/prerequisites.sh:57` pins terraform `1.16.2`. Both roots accept `>= 1.9.0` (`terraform/bootstrap/versions.tf:17`, `terraform/infra/versions.tf:2`). The engine enforces the prerequisites pin (D20).
9. **The swarm-system workloads are GKE objects, not Cloud Run.** Terraform creates their secrets and grants, and `kubernetes/apply.sh` applies them. The manifest records `swarm-live`, `swarm-graph`, `swarm-egress`, `swarm-typesafe-fwd` and `tailscale-egress` as `workload.*` (kind `namespace-object`).
10. **Jobs and Scheduler entries multiply per tenant.** The per-tenant Cloud Run jobs are a tenant × profile matrix (`terraform/infra/locals.tf:593-673`). The brief's "33 Scheduler jobs" are 13 job templates in `terraform/modules/scheduler/jobs.tf` (lines 3 to 787), fanned out per tenant (`docs/DEPLOY_STATE.md:264-275`). The manifest lists templates, and the engine expands them per tenant (section 3.4).

---

## 2. The engine

### 2.1 Language and home

- **Python 3.11 with `uv`**, as CLAUDE.md requires for every Python app.
- **New package `apps/swarm-install`**, import name `swarm_install`. It is not placed inside `apps/swarm-mcp`. Three reasons:
  - The engine pulls in Google API clients (Resource Manager, Service Usage, IAM, Secret Manager, Storage, Compute, Cloud DNS, Firestore) that a developer's plugin bridge should not install.
  - Its tests carry their own fixtures.
  - Its territory is cleanly separate for P3 lanes.
- **The CLI face is the existing `swarm` command** (`apps/swarm-mcp/pyproject.toml:19`, `swarm = "swarm_mcp.cli:main"`).
  - New subcommands: `install`, `uninstall`, `upgrade`, `status`, `team add`, `addon enable|disable`.
  - Each imports `swarm_install` lazily. When the package is missing, the command prints the one install line.
- **`swarm doctor` already exists** as a client-side doctor (`apps/swarm-mcp/swarm_mcp/cli.py:1428`). It gains a second tier, `platform`: when the caller's gcloud identity can read the install state of the deployment it resolves, it also runs every manifest check. It never writes in either tier.
- **Exit codes.** They follow `sc` (`apps/swarm-mcp/swarm_mcp/sc.py:124-129`: 0 ok, 1 fail, 2 usage, 3 trouble) and add two:

| Code | Meaning |
|---|---|
| 0 | every step in scope is `met` and verified |
| 1 | a step failed; the output names it and how to continue |
| 2 | usage error |
| 3 | `doctor`/`status`: something is not met, OR something could not be checked (never "fine" when a check did not run) |
| 4 | paused on a human step (`provider` is `human-*`, `org-admin` or `company-external`); `swarm install` resumes it |
| 5 | refused: a typed confirmation was missing, a downgrade was not allowed, or a deny-listed resource was named |

### 2.2 The step protocol

Two levels, because one action often satisfies many dependencies. A single bootstrap apply, for example, creates 30 custom roles.

- **Dependency** (a manifest entry). Each has a `check` probe and a `verify` probe. Each is `met`, `unmet`, `unknown` (the probe could not run, for example on a 403) or `waiting`.
- **Step** (Python code). Each step declares which manifest ids it `satisfies`, and implements:

```python
class Step(Protocol):
    id: str                      # e.g. "bootstrap.pass1", "edge.certificate"
    stage: int                   # brief §5 numbering
    satisfies: tuple[str, ...]   # manifest ids
    def check(self, ctx) -> Observation: ...   # read-only; aggregates its entries' probes
    def plan(self, ctx) -> Plan: ...           # what apply WOULD do; printed by --dry-run
    def apply(self, ctx, plan: Plan) -> None:  # idempotent; never runs if check is met
        ...
    def verify(self, ctx) -> Observation: ...  # proves it works (e.g. a 2xx), not only exists
    def explain(self) -> str: ...              # from the manifest's explain/guards text
```

**The loop for each stage, in order:**

1. Run every check for the stage's entries.
2. If all are `met`, verify them and move on.
3. Otherwise, take the steps whose `satisfies` cover an unmet entry. If a step's `depends_on` is not met, stop with its explanation.
4. `plan`, print the plan, `apply`, re-`check`, then `verify`.
5. Record the result in the state store **after every step**, never only at the end of a stage.

**Kinds of step:**

- **Engine steps.** Terraform applies, image copies, secret generation and Kubernetes policy applies.
- **Human steps.** For example the OAuth brand, the Desktop client, the domain-wide delegation grant, the GitHub App installation and a Claude sign-in. A human step's `apply` prints the exact instructions and a deep link, then **polls its own `check`**. The engine never asks the person to type "done". The check decides.
- **Waits.** These include certificate provisioning, DNS resolution and IAM propagation. They are polled with a visible countdown, a stated reason and an upper bound taken from history:
  - IAM: 45 s to 10 min (H19);
  - certificate: up to 60 min in `PROVISIONING` (`terraform/modules/frontend/outputs.tf:25`).

  Past the bound the engine says what to look at and exits 4. It never loops for ever.

**Checks are typed probes in code, not shell strings.** The draft's `check` field is a command a human can run. In P3 each entry gains a `probe:` (for example `{type: service_enabled, name: run.googleapis.com}`). The engine runs probes through the Google client libraries, so it needs no shell or gcloud parsing. `swarm doctor --explain <id>` prints the human command for any probe.

### 2.3 The state store and resume

- **Primary store:** `gs://swarm-tfstate-<project>/swarm-install/state.json`, in the bucket that holds Terraform state (manifest `other.install-state`).
  - Every write uses `ifGenerationMatch`, so two admins cannot overwrite each other.
  - A lease object, `swarm-install/lock`, is created with `ifGenerationMatch=0` and holds the holder's email and an expiry 30 minutes ahead. It is renewed on every step.
  - Another admin can take over an expired lease only after a typed confirmation.
- **Contents:** release version, environment, region, hostname, every step's `{state, at, by, plan_digest, outputs}`, the non-secret outputs the engine needs later (the IAP audiences, the App id and slug, the Desktop client id), and a `schema_version`.
- **What is never stored:** a secret value. The state records only that a secret version exists and its version number.
- **Before the bucket exists** (stages 0 and 1), state is local only, in `~/.config/swarmcloud/install/<project>.json`. It is uploaded as soon as the bucket exists, and from then on the local copy is a cache.
- **Mirror for the console:** once Firestore exists (stage 5), the engine also writes the same document to `swarm_install/state`, so Admin › Setup reads what the CLI reads (brief §4.1). The GCS object stays authoritative, because it exists before Firestore and outlives a failed Firestore apply.
- **Resume:** `swarm install` with no arguments reads the state of the project gcloud points at, re-runs the checks of every step marked done (cheap and read-only), and continues at the first unmet one. A step marked done whose check now fails is reported as drift, with the step that will repair it.

### 2.4 Dry run

`swarm install --dry-run` runs stage 0 and every `check` in every stage, and prints each step's `plan`.

- For the Terraform steps, the plan is a real `terraform plan` against generated inputs, but **only when** the state bucket and the deployer service account already exist.
- On an empty project the Terraform plan cannot run, because the backend is missing. The dry run then prints the resources the module would create, read from the release's resource list.

The release candidate gate in brief §8 (P3: "the RC passes `swarm install --dry-run` against an empty project") is exactly this mode.

### 2.5 Secrets through the engine

**The values the engine generates** (the platform HMAC keys, which `scripts/create-secrets.sh` makes today) go straight from `secrets.token_bytes` into `SecretManagerServiceClient.add_secret_version`. They are never printed or written to disk.

**Human-held values** go in one of two ways, never through Claude:

- a hidden terminal prompt (`getpass`, the same contract as `create-secrets.sh --stdin`);
- a **one-time console link**: a single-use URL to an Admin page that writes the value server-side.

**GitHub App credentials** come from the manifest conversion (section 9.1) and are written directly by the engine.

**The `/sc:install` skill** drives the engine through `swarm install --json-events`. It sees only step ids, states and explanations, never a value. That matches the rule that no value goes in any tool schema (`docs/security.md:453-455`; `apps/swarm-mcp/swarm_mcp/secrets.py`).

---

## 3. The manifest

### 3.1 Schema

The draft's fields are exactly brief §6 A's list, and the header of [`day-one/manifest.draft.yaml`](day-one/manifest.draft.yaml) documents them:

`id`, `kind`, `scope`, `stage`, `provider`, `check`, `apply`, `verify`, `explain`, `guards`, `secret_entry`, `depends_on`, `evidence`, `notes`.

`provider` says who provides the dependency **in the Day One install**, not who runs it by hand today. Everything Terraform, the engine or `kubernetes/apply.sh --policies` creates is `engine` (D20; brief §5 stage 10), so no Terraform-made role, binding, switch, cluster policy or namespace object pauses the install. A `human-*`, `org-admin` or `company-external` provider is a step that pauses it with exit code 4 (section 2.1), and is kept for what only a person can do: install a local tool, create the project and link billing, type a value held outside the platform, sign in to a Claude account, or act in a console.

**P3 adds four fields:**

- `probe:`, the typed check (section 2.2);
- `step:`, the engine step that satisfies the entry;
- `refs:`, the machine-matchable references the completeness test uses (section 3.3): Terraform addresses, environment variable names, secret ids or id patterns, Scheduler job names, image names and Kubernetes object files;
- `expand:` `per_tenant` | `per_profile` | `per_account`, for template entries (section 3.4).

**Format and home.** YAML, at `install/manifest.yaml`, loaded by a `pydantic` model in `swarm_install.manifest`. The release pipeline copies it into the bundle (section 6), and the console reads the bundle's copy through swarm-api. P2 may move the file. The schema does not depend on where it lives.

### 3.2 What the draft holds

433 entries. A test should hold these counts (section 3.3), so a reader does not have to trust them.

| kind | count | kind | count |
|---|---|---|---|
| iam-binding | 52 | firestore | 6 |
| switch | 38 | org-policy | 5 |
| other | 35 | quota | 5 |
| custom-role | 30 | addon | 5 |
| gcp-api | 26 | github-app-setting | 4 |
| secret-container | 24 | iap-setting | 3 |
| image | 23 | kms-key | 3 |
| declared-value | 21 | registry | 3 |
| secret-value | 21 | cloud-run-job | 3 |
| network | 18 | workspace-group | 3 |
| namespace-object | 17 | certificate | 1 |
| local-tool | 15 | dns | 1 |
| service-account | 15 | oauth-brand | 1 |
| scheduler-job | 13 | oauth-client | 1 |
| cluster-policy | 9 | cluster | 1 |
| pubsub-topic | 8 | workspace-delegation | 1 |
| bucket | 7 | github-app-installation | 1 |
| cloud-run-service | 7 | claude-account | 1 |
| monitoring | 6 |  |  |

**By provider:** engine 381, human-in-terminal 21, org-admin 17, human-in-console 13, company-external 1.

**By stage:** 0: 26, 1: 39, 2: 12, 3: 42, 4: 30, 5: 58, 6: 42, 7: 106, 8: 9, 9: 19, 10: 16, 11: 18, 12: 1, 13: 4, 14: 4, 15: 7.

**The API list (brief §6 B1)**, merged from `scripts/prerequisites.sh:174-192`, `terraform/infra/main.tf:24-59` and `terraform/bootstrap/variables.tf:91-97`:

- **Core:** `artifactregistry`, `cloudidentity`, `cloudkms`, `cloudresourcemanager`, `cloudscheduler`, `cloudtrace`, `compute`, `container`, `eventarc`, `firestore`, `iam`, `iamcredentials`, `iap`, `logging`, `monitoring`, `pubsub`, `run`, `secretmanager`, `serviceusage`, `storage`, `sts`, `workflows`, plus the new `policytroubleshooter` (H48) and `billingbudgets`.
- **Conditional:** `dns`, only when the zone is in Cloud DNS.
- **Add-on only:** `cloudbuild`, under `addon:ci`. Core copies images and builds nothing (D21). Today `scripts/prerequisites.sh:176` hard-fails without it, and no Terraform root enables it.
- **Deliberately absent:** `containerscanning` (`terraform/infra/main.tf:32-39`).

### 3.3 The test that fails on an unlisted dependency

**File:** `tests/unit/install/test_manifest_complete.py`. It runs offline, with no credentials and no emulator, and reads only the tree. Each collector below builds a set of references from the code, and the test asserts two things:

- **(a)** every reference is matched by some entry's `refs:`, or is listed in the manifest's `ignored:` section with a reason;
- **(b)** every entry's `refs:` still match something. A stale entry fails too.

The collectors:

1. **Terraform resources.**
   - Regex `^\s*(resource|data)\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"` over `terraform/bootstrap/*.tf`, `terraform/infra/*.tf` and `terraform/modules/*/*.tf`.
   - Each match keys on `<root or module>/<type>.<name>`. A regex is used instead of an HCL parser, so the test needs no new dependency. Resource headers are always on one line in this tree; the test itself asserts that, so a multi-line header fails loudly.
   - `data` sources count, because a data lookup on something that does not exist yet is a first-apply trap (`frontend_iap_backends`, `terraform/bootstrap/variables.tf:503-525`).
   - Types that create nothing are listed in `ignored:` with a reason: `terraform_data`, `random_*`, `time_sleep`, `moved`, `removed`, `import`.
2. **APIs.** The string literals inside the `google_project_service` lists (`terraform/infra/main.tf:24-59`, `terraform/bootstrap/variables.tf:91-97`) and the `REQUIRED_APIS` array in `scripts/prerequisites.sh:174-192`. Each must be an `api.*` entry, and each `api.*` entry must be in at least one of those lists, or be marked `new: true`.
3. **Environment variables read by services.** A Python `ast` walk of `apps/*/` (excluding `tests`) for:
   - `os.environ[...]`, `os.environ.get(...)`, `os.getenv(...)`, and `environ.get(...)` / `_env(environ, ...)` where the first argument is a literal;
   - `pydantic` `BaseSettings` field names with their `env_prefix`.

   A regex over `apps/swarm-ui/src` finds `import.meta.env.VITE_*`. Each name must appear in the manifest's `env:` table, which maps the name to its setter (`terraform/infra/locals.tf:<line>`, a Kubernetes manifest, or `internal: <reason>`). The frozen `apps/common/swarm_common/config.py` is read, never edited, and its variables are in the table too (for example `ALLOWED_DOMAINS`, `apps/common/swarm_common/config.py:110`).
4. **Secret ids.**
   - Literals matching `swarm-[a-z0-9-]+` in `secret_id =` attributes under `terraform/`.
   - The `PLATFORM_SECRETS` catalogue, imported (`apps/swarm-api/swarm_api/platform_secrets.py:97-121`).
   - The `--<name>` cases in `scripts/create-secrets.sh`.
   - The per-tenant patterns from the naming helpers in the frozen contract and swarm-api (`swarm-tenant-<t>-*`, `swarm-account-<t>--<label>`), which become `refs: {secret_pattern: ...}`.
5. **Scheduler jobs.** `resource "google_cloud_scheduler_job"` in `terraform/modules/scheduler/jobs.tf`. There are 13 today, from line 3 (`safety_tick`) to line 787 (`mcp_credentials_sweep`).
6. **Images.** The keys of `terraform/infra/images.json`, the target list in `scripts/build-images.sh:169`, and every tracked `Dockerfile`: 15 today, all listed in appendix A.
7. **Kubernetes objects.** The `POLICY_FILES` and `TENANT_FILES` tuples, imported from `kubernetes/render.py` (`POLICY_FILES` at line 529), and the files under `kubernetes/swarm-system/`.
8. **Switches.** Every `variable "enable_*"` and every `bool` variable in both roots. Each is a `switch.*` entry or `ignored:` with a reason.

**Red first.** The first P3 lane commits the test with an empty manifest, so it is red with every reference listed. Then it commits the manifest. The P2 adversarial review runs the collectors against the draft, and every miss becomes an entry.

**Count tests.** One more test holds the counts table in section 3.2 against the manifest, so this document cannot go stale silently.

### 3.4 Templates and what this draft does not enumerate

**Template entries (P3: `expand:`).** The engine expands these at run time for the deployment's tenants and profiles:

- the per-tenant × profile Cloud Run job matrix (`cloud-run-job.tenant-profile-matrix`, `terraform/infra/locals.tf:593-673`);
- the per-tenant Scheduler jobs (`merge_wake`, `issue_run_advance`, `workflow_rollup`, `repo_index_poll`, `stranded_pr_sweep`, `terraform/modules/scheduler/jobs.tf:169-426`);
- the per-tenant secrets and namespace objects.

**Not enumerated one by one:**

- **The 21 alert policies** are one entry, `monitoring.alert-policies` (`terraform/modules/monitoring/alerts.tf:156-173`).
- **Environment variables** are not yet one entry each. The research table is in the P0 artefacts, not in this PR. Collector 3 is the source of truth from P3 on.
- **Notable environment findings:**
  - `WORKER_KSA_NAME` has no Terraform setter (`apps/scheduler/scheduler/dispatch.py:2850`; `kubernetes/render.py:1424`).
  - `VITE_SWARM_ENV` is compiled in at build time (`apps/swarm-ui/src/Brand.tsx:34`; `apps/swarm-ui/src/vite-env.d.ts:19`), which a prod-shaped release image must stop doing (brief §9).
  - `IAP_AUDIENCES` and `TENANT_GROUPS` are declared, not derived (`terraform/infra/locals.tf:866-878`).

---

## 4. The stage graph

Each stage's dependencies are the manifest's `depends_on` for that stage's entries, and the engine walks the graph in stage order. Within a stage, independent entries run in parallel where the API allows. The arrows below are the cross-stage edges that decide the order. Stage numbers are brief §5's.

```
0 preflight ─▶ 1 foundations ─▶ 2 edge+identity prereqs ─▶ 3 bootstrap pass 1 ─▶ 4 images
                    │                     │ (cert waits on DNS, runs in background)      │
                    ▼                     ▼                                              ▼
              state bucket          OAuth brand ─▶ Desktop client ─────────────▶ 5 infra pass 1 (switches off)
                                                                                         │
                                         6 keys and secrets, then switches ◀─────────────┘
                                                   │
                         7 infra pass 2 + bootstrap pass 2 (IAP clients, grants, propagation wait)
                                                   │
          8 identity wiring ─▶ 9 GitHub App ─▶ 10 cluster policies ─▶ 11 first team ─▶ 12 Claude capacity
                                                                                         │
                                                         13 alerts ─▶ 14 verify ─▶ 15 hand-off
```

| Stage | Gate to enter (manifest ids) | What it satisfies (examples) | Why that order |
|---|---|---|---|
| 0 | none | `local-tool.*` (15), `org-policy.*` (5), `quota.*` (5), `switch.project-create-right`, `switch.billing-link-right`, `other.release-bundle` | Nothing writes until the person's rights, the org's policies and the release's signature are known (D14). |
| 1 | `switch.project-create-right`, `switch.billing-link-right` | `other.gcp-project`, `other.billing-link`, `monitoring.budget-alert`, `api.*` (26), `iam-binding.default-compute-sa-editor-removed`, `bucket.state`, `other.install-state` | APIs before any resource; the state bucket before any Terraform (H7). |
| 2 | `api.iap`, `api.compute` | `oauth-brand.iap`, `other.iap-service-agent`, `network.global-ip`, `dns.frontend-hostname`, `certificate.managed-ssl`, `oauth-client.desktop-sc` | The certificate waits up to an hour on DNS, so it starts as early as possible and is waited on in the background until stage 7. |
| 3 | `bucket.state`, `api.cloudkms`, `api.iam` | `role.custom.*` (30), `sa.swarm-tf-deployer`, every platform `sa.*` (section 5.3), `kms-key.step-spec-ring`, `kms-key.step-spec-key`, `iam-binding.installer-impersonates-deployer` | Infra needs bootstrap's roles and KMS key (`docs/runbooks/custom-roles-to-bootstrap.md`). |
| 4 | `registry.swarm` (created by a targeted infra apply), `local-tool.gcrane`, `local-tool.cosign` | `image.copy-to-company-registry`, `image.*` | The full infra apply refuses to run without digests (`terraform/infra/verify.tf:236-252`). |
| 5 | stage 3, stage 4 | `cloud-run-service.*`, `cloud-run-job.*`, `sched.*`, `pubsub.*`, `firestore.*`, `network.*`, `cluster.autopilot`, `secret.*.container` | Every key-gated switch is off, so no revision references a secret with no version (H39). |
| 6 | secret containers from stage 5 | `secret.*.value` (platform keys), then `switch.*` | Key before switch (brief §6 B7). |
| 7 | stage 6 | `declared-value.frontend_iap_audiences` (computed, section 5.2), `iap-setting.*`, the `frontend_iap_members` grants, `other.discovery-endpoint` | IAP members and programmatic clients can only name accounts and backends that exist. |
| 8 | `cloud-run-service.swarm-api`, `api.cloudidentity` | `workspace-delegation.groups-dwd` (or the admin-role path, section 8), `workspace-group.*` | The first group lookup must succeed before any group is registered, or every request 503s (`docs/multi-tenancy.md:167-171`). |
| 9 | `cloud-run-service.github-hooks`, `secret.swarm-github-app-*.container` | `github-app-setting.*`, `github-app-installation.per-owner`, `switch.enable_github_app`, `switch.enable_forge_refresh`, `switch.enable_github_hooks` | The webhook is turned on only after its secret has a version. |
| 10 | `cluster.autopilot` | `cluster-policy.*` (9), `workload.*` | Admission policies before the code that needs them (H33). The workspace deployer's scope policy comes before its RBAC (`kubernetes/render.py:519-528`). |
| 11 | stages 8 and 10 | `namespace-object.*` (17), `secret.swarm-tenant-provider.*`, `switch.workspace_gate`, `switch.repository_grants_enforced` | A namespace with the right quota and the verify-keys ConfigMap must exist before the first GKE dispatch (#1590, #2039). |
| 12 | `cloud-run-service.swarm-quota-broker`, stage 11 | `claude-account.subscription` (two accounts) | The pool is inert without the broker URL, which section 5.2 now computes. |
| 13 | `api.monitoring` | `monitoring.notification-channel`, `monitoring.alert-policies` | `require_alert_channels` defaults to false (`terraform/infra/variables.tf:958-961`); the install sets it to true (#1817). |
| 14 | all of the above | `other.verify-scheduler-targets` (H38), `other.verify-digests` (H28), `other.verify-end-to-end`, `other.firestore-migrations` | Done is proven (D3). |
| 15 | stage 14 | `other.ready-screen`, `addon.*` (offered, off) | |

**Uninstall** walks the same graph in reverse (section 10).

### 4.1 History lessons as checks (brief §7)

Brief §7 says the 48 lessons are listed, with sources, in the history research report. That report is in the session that wrote the brief, **not in this repository**, so P0 could not read all 48. The table maps every lesson the brief names (§3, §5 and §7) to the manifest entry or engine rule that makes it impossible. The other lessons are mapped in P2, when the report is attached to the plan (open question 9).

| Lesson | Manifest entry or engine rule | The check |
|---|---|---|
| Infra applied before bootstrap (missing custom roles) | stage gate 3 → 5; `role.custom.*` | Stage 5 refuses to start while any `role.custom.*` is unmet. |
| H2 Firestore location is permanent | `firestore.main`, `org-policy.resource-locations` | Region is chosen and confirmed in stage 0, before anything is created. |
| H4 Cloud KMS signing quota | `quota.kms-signing-h4` | Preflight reads the quota. |
| H7 bootstrap state must never be local | `bucket.state`, `other.install-state` | The engine writes no Terraform state anywhere but the bucket. |
| H10 destroy-before-create IAM swap | upgrade rule (section 11 step 5) | A plan that replaces an IAM resource without `create_before_destroy` is refused. |
| H11 a `-var` never written to tfvars | engine rule (section 5.1) | No `-var`; every input is in the generated files kept in the state bucket. |
| H19 IAM propagation, 45 s to 10 min | stage 7 wait | A countdown wait with a bound; `doctor` tells propagation apart from a missing grant. |
| H23 an owner apply hides a missing `actAs` | `iam-binding.installer-impersonates-deployer` | Every apply after bootstrap pass 1 runs as the deployer. |
| H28 a pinned revision served 0 %; the tag label lies | `other.verify-digests` | Traffic and digest compared with the release. |
| H29 RBAC subject named by email, not uniqueId | `rbac.dispatcher-role`, `rbac.verify-role`, `rbac.worker-role` | The engine renders subjects from the service accounts' `uniqueId`, read from IAM. |
| H33 admission policies after the code that needs them | stage 10 before 11; upgrade order (section 11) | Policies that admit more go before the code. |
| H38 Scheduler reported success while the target 403'd | `other.verify-scheduler-targets` | Each target is called and must answer 2xx. |
| H39 a revision referencing a secret with no version | `switch.*` computed from versions (section 5.4) | A switch is true only when its key has an `ENABLED` version. |
| H43 GitHub API quota | `github-app-setting.tenant-forge-app` | Background reads use App installation tokens. |
| H47 the auto-mode classifier refuses IAM or deletion commands | `/sc:install` rule (section 2.5) | The skill hands the person the exact command; it never works around a refusal. |
| H48 Policy Troubleshooter | `api.policytroubleshooter` | Enabled in stage 1; used by `doctor`. |
| Pool inert without the broker URL | `declared-value.quota_broker_url` (computed, section 5.2) | The scheduler's `QUOTA_BROKER_URL` is non-empty and answers. |
| Every request 503s on a failed group lookup | `workspace-delegation.groups-dwd`, `workspace-group.*` | A group is registered only after its probe succeeds (section 8). |
| 401 code 900 without the programmatic-client allowlist | `iap-setting.frontend-programmatic-clients` | `sc login` with the Desktop client reaches `/healthz`. |
| `gke_create_job_forbidden` without a namespace (#1590) | `namespace-object.tenant-namespace` | `team add` applies it and checks it exists. |
| Wrong ResourceQuota on a hand-applied namespace (#2039) | `namespace-object.tenant-resourcequota` | Quota rendered from the tenant record and compared after apply. |
| Missing `swarm-spec-verify-keys` ConfigMap | `namespace-object.verify-keys-configmap` | Present in every tenant namespace. |
| Alerts reach nobody (#1817) | `monitoring.notification-channel` | `require_alert_channels = true`, with a channel that exists. |
| Default compute SA holds Editor (#1020) | `iam-binding.default-compute-sa-editor-removed` | No Editor binding remains. |
| The Job-count quota fills with finished Jobs (#2661) | `quota.cloud-run-jobs` | `doctor` counts Jobs against the quota and checks the TTL. |
| The certificate waits on DNS | `dns.frontend-hostname` → `certificate.managed-ssl` | DNS must resolve to the reserved address before the certificate wait starts. |
| A same-day CVE blocks the first deploy | `other.release-bundle` | The installer shows the release's accepted CVEs and their expiry; it never skips a scan. |
| The App lacked Commit statuses: Read | `github-app-setting.commit-statuses-read-gap` | The manifest flow requests `statuses: read` (section 9.1). |

---

## 5. Collapsing the bootstrap and infra passes (brief §3 item 12, §11 items 2 and 3)

The target is four Terraform applies, every one driven by the engine, and **no value copied by hand**:

1. bootstrap pass 1;
2. a targeted infra apply of the registry only;
3. infra pass 1;
4. infra pass 2 (switches on), then bootstrap pass 2 (IAP programmatic clients).

Today's sequence has about six applies and three hand copies (brief §3 item 3).

### 5.1 Generated inputs, never the committed tfvars

The release's `module.tgz` contains `terraform/bootstrap`, `terraform/infra` and `terraform/modules`. It contains **no** `terraform.tfvars` and **no** `terraform/environments/`. The engine writes `bootstrap.auto.tfvars.json` and `infra.auto.tfvars.json` into its work directory from:

- the install state (project, region, environment, hostname, admins, App ids, Desktop client id);
- the release's image digests;
- the computed switch values (section 5.4).

This removes all of these at once:

- `adopt_from_infra_states = ["infra/dev"]` (`terraform/bootstrap/terraform.tfvars:16`);
- the live `workspace_apply_image` digest (`:192`);
- the live `frontend_iap_members` (`:149-155`);
- `ci_fix_service_account` (`:34`);
- the person-named `typesafe_user_key_readers` (`:224-226`).

The project id and region are written into the same generated files. The engine passes **no** `-var`: a value given only on the command line is a value the next apply does not have (H11). `scripts/bootstrap.sh:332-334`'s `-var` override is not used for installs.

### 5.2 The three declared values

**`frontend_iap_audiences`.** Remove the module-level `depends_on = [module.project_services, module.cloud_run]` at `terraform/infra/main.tf:698`, keeping only `module.project_services`. Then set `IAP_AUDIENCES = join(",", module.frontend[0].iap_audiences)` in `terraform/infra/locals.tf:870`.

Nothing in the frontend module reads a Cloud Run output: it takes `service_name = "swarm-api"` and `ui_service_name = "swarm-ui"` as strings (`terraform/infra/main.tf:692-693`; `terraform/modules/frontend/main.tf:116-117`, `:180-181`). Once that `depends_on` is gone, the graph runs backend service → audience → Cloud Run environment, with no cycle.

To be proven in P3: that a serverless NEG and an IAP-enabled backend service can be created before the Cloud Run service they name exists. The test is a plan and apply on the throwaway project of D22.

**Fallback** if Google refuses a NEG for a missing service: split the module into `frontend_backends` (NEGs and backend services, no dependencies) and `frontend_edge` (everything else). The audience still comes from the first part.

**`quota_broker_url` and `swarm_api_url`.** Compute them as `"https://${name}-${data.google_project.this.number}.${var.region}.run.app"`. Cloud Run documents this deterministic URL alongside the older hashed one; see https://cloud.google.com/run/docs/triggering/https-request.

Two things need care:

- Workers mint ID tokens with the URL as audience (`SWARM_API_AUDIENCE`, `terraform/infra/child_tasks.tf:14`). Cloud Run accepts either URL as an audience, but that is **to be proven in P3** by the existing smoke against the throwaway project.
- Fallback: the engine reads `terraform output quota_broker_url` (`terraform/infra/outputs.tf:197-200`) after infra pass 1 and writes it into the generated tfvars for pass 2. That keeps the passes but removes the hand copy.

**`workspace_apply_image`** (bootstrap). The engine sets it from the release's images.json digest, in the project's own registry (D21). It always exists before bootstrap pass 2, because stage 4 copies it.

### 5.3 Bootstrap grants to accounts that do not exist yet

These bootstrap grants name accounts that infra creates:

- swarm-api `cloudkms.signer` (`terraform/bootstrap/spec_signing.tf:136-144`);
- swarm-verify `container.clusterViewer` (`terraform/bootstrap/verify_cluster_read.tf:52-58`);
- the broker's secret lister (`terraform/bootstrap/variables.tf:591-601`, default `true`);
- the workers' log view (`swarm_workers_log_view_exists`, default `true`, `terraform/bootstrap/variables.tf:898-912`).

**Fix:** move every `google_service_account` for platform identities into bootstrap. The ids already come from `terraform/modules/service_account_ids`, which both roots use.

**Migration for the live deployment.** This is the custom-roles move again:

- `removed { from = ...; lifecycle { destroy = false } }` in infra;
- `import {}` blocks in bootstrap, gated by `adopt_from_infra_states` exactly as `terraform/bootstrap/platform_roles.tf:425-437` does for roles.

**Effect:** bootstrap pass 1 can grant on accounts that exist, and the four boolean "later" inputs disappear.

**The log view.** It is created by infra (`swarm_workers_log_view_exists`) and stays a bootstrap pass 2 grant. The engine sets that input to true only after infra pass 1.

### 5.4 Key-gated switches, driven by the engine

**Switches that gate on a secret version** (brief §6 B7):

- `enable_child_tasks` (`swarm-child-key`);
- `enable_live_browser` (`swarm-live-key`);
- `graph_service.serve` (`swarm-graph-ticket-key`);
- `settings_mac_key_ready` (`swarm-settings-key`; turning it on before the key has a version takes down swarm-api and swarm-scheduler, `terraform/infra/variables.tf:1292`);
- `workspace_grant_attestation.serve` (`swarm-workspace-grant-key`);
- `enable_github_app` / `enable_forge_refresh` / `enable_github_hooks` (the three App secrets).

The engine computes each one from a check: does the secret have an `ENABLED` version? It writes the result into the generated tfvars. Infra pass 1 runs with every one false. Infra pass 2 runs after stage 6 with each one true exactly when its key exists.

**Switches that are product defaults, not keys:**

- `workspace_gate` and `repository_grants_enforced` (both default `false`, `terraform/infra/variables.tf:1114`, `:994`) are **set true** for installs (brief §11 item 11);
- `require_alert_channels` is **set true** with a channel (stage 13).

### 5.5 The state bucket, created once

The bucket is a chicken-and-egg case. Terraform's backend needs it before `terraform init`, so it cannot be created only by Terraform.

**Today:** `scripts/bootstrap.sh:96-114` creates it with gcloud, and `terraform/bootstrap/state_bucket.tf:47` declares it again with no `import`.

**Fix:**

1. The engine creates it in stage 1 with the same settings as `scripts/bootstrap.sh:107-113`: uniform access, public-access prevention, versioning and labels.
2. `state_bucket.tf` gains an unconditional `import { to = google_storage_bucket.state, id = "${var.project_id}/${local.state_bucket_name}" }`. It is safe, because by the time Terraform runs the bucket always exists.

### 5.6 The tenants freeze and service tenants

- **Installs declare no Terraform tenants** (`tenants = {}`). The freeze admits that (`terraform/infra/variables.tf:463-468`). Workspaces are made by `swarm-workspace-apply` (stage 11).
- **The bootstrap precondition** that the infra tfvars hold at least one tenant (`terraform/bootstrap/deployer_service_accounts.tf:100-103`) becomes conditional on `var.infra_tenants_tfvars != ""`. The engine passes `""`.
- **`swarm-workspace-apply`** trusts signing keys only for the live project (`images/workspace-apply/entry.py:183-190`). `SIGNING_KEYS` must come from the job's environment, which bootstrap renders from the deployment's own KMS key. Otherwise a fresh project cannot create a workspace (brief §3 item 2).
- **The project-derived service tenants** (`u-sw-*`, `u-or-*`) are computed by the frozen contract from the project (`apps/common/swarm_common/identity.py:131-165`). The engine computes them the same way, by importing that code, never by restating it.

### 5.7 Tenant secret containers: three writers, one for installs (brief §6 B3)

The research brief named two writers of `swarm-tenant-<t>-<provider>`. The code has **three**:

- **Terraform**, for the tenants in `var.tenants`: `google_secret_manager_secret.this` and `.refresh` (`terraform/modules/secret_manager/main.tf:76`, `:172`; per-tenant map at `:16`).
- **`scripts/create-secrets.sh`**, which describes and then creates a container if it is missing (around `scripts/create-secrets.sh:928`).
- **swarm-api's credential writer**, `put_credential` (`apps/swarm-api/swarm_api/credentials.py:150`). It runs under an unconditioned project-level create grant (`terraform/bootstrap/provider_keys.tf:51-61`).

All three tolerate `AlreadyExists`. The 409 risk is a Terraform `create` racing one of the others: Terraform then fails on a container it does not hold in state.

**For installs, only one writer is used.**

- There are no Terraform tenants (section 5.6), so Terraform declares no per-tenant container.
- The engine never calls `create-secrets.sh` for a tenant.
- `swarm team add` routes every tenant value through swarm-api's writer, behind a one-time console link. `create-secrets.sh` stays as the live deployment's tool.

Whether the live deployment's Terraform tenants should move to the same single writer is P2's question. It is not needed for Day One.

---

## 6. Release home and distribution (D5, D16, D21, D22, D23)

### 6.1 The release home

**D25 (BGD, 2026-10-28):** the release home exists. It is GCP project `swarmcloud-prod` under the Saga org (its display name is `swarmcloud`; the id `swarmcloud` was taken, so the D23 fallback `swarmcloud-releases` was not needed either; PR 2769); billing is linked. Paths: Artifact Registry `us-docker.pkg.dev/swarmcloud-prod/swarmcloud`, bundles in `gs://swarmcloud`.

It holds three things.

**`gs://swarmcloud`**, free as of 2026-10-28 (D16). Layout:

```
gs://swarmcloud/
  releases/<YYYY.MM.N>/
    module.tgz            terraform/{bootstrap,infra,modules}, no tfvars, no environments/
    swarmcloud-<V>-py3-none-any.whl   the engine + the swarm/sc CLI
    manifest.yaml         the dependency manifest of section 3
    images.json           {image: "sha256:<digest>"} for every image
    SHA256SUMS
    SHA256SUMS.sig        cosign signature over SHA256SUMS (P2 confirms cosign vs Binary Authorization)
    RELEASE.md            changes, upgrade notes, migrations, accepted CVEs with expiry
  channels/stable         one line: the current version
  install.sh              a 20-line POSIX script: checks uv, then `uv tool install <wheel URL>`
```

**An Artifact Registry repository `swarmcloud`** in `swarmcloud-prod`, holding images by digest, signed with a KMS key in that project.

**A public verification key** at `releases/<V>/cosign.pub`, also pinned in the wheel, so a tampered bucket cannot also replace the key.

**Public read.** The bucket and the repository need `allUsers` read: `roles/storage.objectViewer` and `roles/artifactregistry.reader`.

- If the org enforces Domain Restricted Sharing (`iam.allowedPolicyMemberDomains`), that grant is refused unless the release project carries an exception. That is a decision for BGD (open question 4).
- Unverified: whether the org enforces it today. Check with `gcloud resource-manager org-policies describe iam.allowedPolicyMemberDomains --project=<release project> --effective`.

### 6.2 The release pipeline (brief §9; workflow files reviewed by BGD)

1. **Tag:** a `v<YYYY.MM.N>` tag on `main` triggers a new `release-public.yml`. It is a workflow file, so P3 writes it as a patch for BGD.
2. **Build prod-shaped images.**
   - `swarm-ui` stops baking `VITE_SWARM_ENV` (`apps/swarm-ui/src/Brand.tsx:34`) and reads its environment from a `/config.json` that its nginx serves from the container environment.
   - The Dockerfile `# syntax=` lines and `ARG MIRROR_*` defaults are made project-independent (appendix A).
3. **Scan** with the trivy gate as it is. A HIGH or CRITICAL finding needs an acceptance with an expiry, recorded in `RELEASE.md`.
4. **Copy and sign.** Copy the images to the release home by digest, then sign each digest.
5. **Assemble and upload** `module.tgz`, the wheel, `manifest.yaml`, `images.json`, `SHA256SUMS` and the signature, then upload under `releases/<V>/`. `channels/stable` is moved only by a separate, manual workflow run after the manual install-and-upgrade test (D22).

**The plugin** is published to the existing marketplace at the same version.

- Its MCP bridge source today is a git tag of this repository (`plugin/.claude-plugin/plugin.json:75`). It becomes the release wheel URL, so a company's teammates never need the repository.
- `/sc:setup`'s `uv run …` hints (`plugin/commands/setup.md:54`, `:133`) become `swarm …`.

### 6.3 The company's side

**Install:** `curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh`, then `swarm install --project P --env prod --version V --domain H`.

The engine then:

- downloads `releases/<V>/`, verifies `SHA256SUMS` against its pinned key, and unpacks the module into `~/.cache/swarmcloud/<V>/`;
- in stage 4, copies each image digest into `<region>-docker.pkg.dev/<P>/swarm/` with `gcrane cp`, after `cosign verify`. Google documents `gcrane` for copies between projects: https://cloud.google.com/artifact-registry/docs/docker/copy-images.

From then on, nothing in the deployment reads the release home (D21).

---

## 7. De-hardcoding

The sweep was `git grep -lE 'saga-agents-staging|saga\.xyz|bogdan-alexandrescu|swarm-mirror-dockerhub|swarm-mirror-ghcr|bogdan-ci|taile0bda9'` over `terraform scripts images apps plugin .claude-plugin kubernetes Makefile .env.example docker-compose.yml`, excluding tests and Markdown. It matches **138 files**, every one listed in appendix A with its literals and lines. `.github/` is not in the sweep: workflow files are BGD-reviewed, and the CI add-on owns them.

What each group becomes:

| Group | Files | Change |
|---|---|---|
| Dockerfile `# syntax=` lines and `ARG MIRROR_*` defaults | 15 Dockerfiles | BuildKit reads `# syntax=` before any build argument, so it cannot be a build-arg. Use `# syntax=docker/dockerfile:1.7@sha256:<same digest>` and let the builder resolve it through its own registry mirror, or drop the syntax line where no 1.7-only feature is used. `ARG MIRROR_*` defaults become empty, with the build passing the project's mirror. `tests/unit/scripts/test_build_mirror.py:169-172` changes with it. |
| Environment and bootstrap tfvars | `terraform/environments/{dev,prod}/*.tfvars`, `terraform/bootstrap/terraform.tfvars` | Stay as the live deployment's own configuration, and are excluded from `module.tgz` (section 5.1). |
| Terraform literals | `terraform/infra/custom_roles_moved_to_bootstrap.tf:115` (the project in a `moved` address), `terraform/infra/locals.tf:1012-1015` (`ALLOWED_USERS`), `terraform/infra/tailscale.tf:84` (the tailnet host), `terraform/bootstrap/variables.tf:488-501` (`deployer_storage_buckets_exact`), `:503-525` (`frontend_iap_backends`) | The `moved` block is deleted once the live state has applied it. It is a no-op for a new state, but it carries the live project id into every install. `ALLOWED_USERS` becomes a variable. The Tailscale target moves to the add-on's variables with no default. `deployer_storage_buckets_exact` defaults to `"${var.project_id}_cloudbuild"` under `addon:ci` and to `[]` in core. `frontend_iap_backends` is derived from the infra state's backends, empty in pass 1. |
| Script defaults | `scripts/lib/common.sh:158` (`PROJECT_ID`), `:260-290` (`SHARED_DENY_LIST`), `scripts/prerequisites.sh:156-157`, `scripts/register-tenant.sh:2286`, `scripts/lib/dev.sh:93` | `PROJECT_ID` has no default: an unset project is an error. `SHARED_DENY_LIST` is read from `SWARM_SHARED_DENY_LIST`, and the live deployment's `.env` keeps today's list. The engine never sources `common.sh`; it reads the deny-list from the install state, empty for a fresh project. |
| Frozen contract | `apps/common/swarm_common/config.py:46,110`, `identity.py`, `models.py` | **Not edited.** Overridden by environment (`ALLOWED_DOMAINS`), which the engine sets from the Workspace domain found in stage 0. If an override is impossible for a literal in `identity.py` or `models.py`, that is a **contract request** (section 14, question 6). |
| swarm-api's own deny-list | `apps/swarm-api/swarm_api/issueruns.py:1264-1270` | Read from the same environment variable as `common.sh`. |
| Plugin | `plugin/.claude-plugin/plugin.json:75`, `plugin/workflows/*` | Bridge source becomes the release wheel (section 6.2). |
| workspace-apply | `images/workspace-apply/entry.py:183-190` | `SIGNING_KEYS` from the job's environment (section 5.6). |
| Kubernetes | `kubernetes/rbac/*`, `kubernetes/swarm-system/*`, `kubernetes/network-policies/*`, `kubernetes/worker-templates/*`, `kubernetes/render.py`, `kubernetes/apply.sh` | Rendered with the project from the install state; `apply.sh` refuses an unset project instead of defaulting. |

---

## 8. Identity and domain-wide delegation

**How it works today:**

- swarm-api reads group membership with `checkTransitiveMembership`, one registered group at a time, with `x-goog-user-project` (`apps/swarm-api/swarm_api/groups.py:1-23`; `docs/multi-tenancy.md:118-152`).
- It does so through domain-wide delegation. A keyless `signJwt` impersonates the mailbox in `groups_impersonate_user` (`terraform/infra/variables.tf:620`, rendered at `terraform/infra/locals.tf:912`; `apps/swarm-api/swarm_api/delegation.py:257-306`).
- The delegation itself is granted by hand in the Admin console, and no runbook covers it. The manifest entry is `workspace-delegation.groups-dwd`.

**The alternative the repository did not try.** `apps/swarm-api/swarm_api/groups.py:99-138` records that the GCP IAM role `roles/cloudidentity.groupsReader` changed nothing. That is the wrong layer: the Groups API authorises through **Workspace admin roles**.

- Since 2020, a Workspace super admin can assign an admin role (any except Super Admin) directly to a service account's email. The service account then calls the Groups API as itself, with no delegation and no impersonated user. See https://workspaceupdates.googleblog.com/2020/08/use-service-accounts-google-groups-without-domain-wide-delegation.html and https://support.google.com/a/answer/9807615.
- The role swarm-api needs is the predefined **Groups Reader**.

**Design.** Stage 8 offers the admin-role path first and keeps the delegation path as a fallback. Both are `org-admin` human steps with a check.

- **Admin role:**
  - **Engine:** prints the Admin console path (Account › Admin roles › Groups Reader › Assign service accounts) with swarm-api's service account email.
  - **Check:** a live `checkTransitiveMembership` *as swarm-api*, run by the engine through `swarm-verify`'s path, against a **probe group** the engine asks the person to create (or a tenant group they name), with a member they name.
- **Delegation:**
  - **Engine:** prints swarm-api's OAuth client id (its numeric unique id) and the scope `https://www.googleapis.com/auth/cloud-identity.groups.readonly`, plus the Admin console path (Security › API controls › Domain-wide delegation).
  - **Person:** names a mailbox for `groups_impersonate_user`, ideally a dedicated Workspace user that is not a person (brief §12 Q2).
  - **Check:** the same probe.

**To be proven in P4:** that `checkTransitiveMembership` works with the admin-role path. The blog post covers the Groups API in general; whether it covers this method is not verified.

- If it works, `swarm_api` gains a mode switch: `GROUPS_AUTH=admin_role|delegation`.
- The code change is small: with `admin_role`, `delegation.delegate()` is skipped and swarm-api's own credentials are used.

**The 503-everyone trap** (`docs/multi-tenancy.md:167-171`):

- The engine never registers a group whose probe failed.
- `swarm doctor` re-probes every registered group.
- P3 item 5 (brief §11) keeps the 503 for an unreadable registry, but makes a single unreadable group fail only for that group's members. That is a behaviour change to swarm-api, not the contract.

**Admins.** The platform owner and the admin users or groups are written as `frontend_iap_members` and `admin_pool_users` from the install state. The live `swarm-admins` group was never created (`terraform/environments/dev/dev.tfvars:538`; `terraform/environments/prod/prod.tfvars:145`), so the engine creates or names a real group and checks that it resolves.

---

## 9. The GitHub App, the discovery endpoint and the Desktop client

### 9.1 The GitHub App Manifest flow (stage 9)

GitHub documents the flow at https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest.

1. **Start a listener.** The engine starts a loopback listener on `127.0.0.1:<random port>`, the same pattern as `sc login` (`apps/swarm-mcp/swarm_mcp/signin.py:233-276`). It opens the browser at a local page that auto-submits a form `POST https://github.com/organizations/<org>/settings/apps/new?state=<random>` with the field `manifest` set to a JSON document holding:
   - `name`: `SwarmCloud <deployment display name>`, made unique by the engine if GitHub refuses it;
   - `url`: `https://<H>`;
   - `hook_attributes`: `{url: <github-hooks receiver URL>, active: false}`. The webhook is turned on only after its secret has a version (stage 9's last step).
   - `redirect_url`: the loopback URL;
   - `callback_urls`: the console's GitHub sign-in callback on `https://<H>`;
   - `public: false`;
   - `default_permissions`, from `docs/runbooks/github-app.md:170-198`:
     - `contents: write`, `pull_requests: write`, `issues: write`, `checks: read`, `metadata: read`, `actions: write`, `workflows: write`;
     - `secrets: write` and `environments: write`, as that runbook lists them;
     - **`statuses: read`**, the Commit statuses permission the live App lacked (brief §5 stage 9). GitHub's key is `statuses`: https://docs.github.com/en/rest/overview/permissions-required-for-github-apps.
   - `default_events`: the events the runbook lists.
2. **Exchange the code.** GitHub redirects to the loopback with `code` and `state`. The engine checks `state`, then calls `POST https://api.github.com/app-manifests/<code>/conversions` (no authentication; the code expires after one hour). The response carries `id`, `slug`, `client_id`, `client_secret`, `webhook_secret` and `pem`.
3. **Store.** The engine writes `pem`, `client_secret` and `webhook_secret` straight into `swarm-github-app-private-key`, `swarm-github-app-client-secret` and `swarm-github-app-webhook-secret` with `add_secret_version`. It writes `id`, `client_id` and `slug` (not secret) into the install state, and from there into `github_app_id`, `github_app_client_id` and `github_app_slug` (`terraform/infra/github_app.tf:44-83`). The response body is never logged or written to disk.
4. **Install.** The engine opens `https://github.com/apps/<slug>/installations/new` and polls `GET /app/installations` with an App JWT until the org appears.
5. **Self-check.** Infra pass 2 turns on `enable_github_app` and `enable_forge_refresh`. The engine runs the `/admin/forge/app` self-check through swarm-api, then sets `hook_attributes.active` to true through `PATCH /app/hook/config` and turns on `enable_github_hooks`.

**One known trap** (`docs/onboarding.md:342-346`): adding a permission to an existing App does nothing until every org owner approves it. `swarm upgrade` must say so and print the review URL whenever a release adds an App permission.

### 9.2 The discovery endpoint (brief §6 B11)

**Path:** `GET https://<H>/.well-known/swarmcloud`, unauthenticated, `Content-Type: application/json`. Its fields, all non-secret:

```json
{
  "schema": 1,
  "name": "<deployment display name>",
  "version": "<YYYY.MM.N>",
  "console_url": "https://<H>/",
  "api_url": "https://<H>/v1",
  "oauth_client_id": "<Desktop client id>",
  "iap_audience": "<the API backend's IAP audience>",
  "plugin": {"min_version": "<V>"}
}
```

**Where it is served.** Every backend on the front door has IAP on (`terraform/modules/frontend/main.tf:148`, `:206`), so an unauthenticated path needs a third backend. The options:

- **(a)** A backend bucket. Rejected: it needs `allUsers` on the bucket, which Domain Restricted Sharing blocks (brief §12 Q3).
- **(b)** A non-IAP backend to swarm-api. Rejected: swarm-api would then need `allUsers` invoker, or the invoker check disabled, on the whole API service.
- **(c) Recommended:** a tiny Cloud Run service **`swarm-discovery`**:
  - its image is the swarm-api image with a different entrypoint, so the release adds no image;
  - ingress `internal-and-cloud-load-balancing`;
  - the **invoker IAM check disabled** (`invoker_iam_disabled = true` on `google_cloud_run_v2_service`; https://cloud.google.com/run/docs/securing/managing-access), so no `allUsers` binding is made and Domain Restricted Sharing is not involved;
  - it serves only that JSON, from environment variables Terraform renders;
  - the URL map gains `path_rule { paths = ["/.well-known/swarmcloud"] service = <discovery backend> }` beside the existing rule at `terraform/modules/frontend/main.tf:287-288`.

  To be proven in P3: that the org's policies allow the invoker check to be disabled (there is an org constraint for it). The fallback is (b) restricted to the one path.

**The plugin.** It needs only `deployment_url`. The bridge fetches the discovery document on first use and fills `oauth_client_id` (today `apps/swarm-mcp/swarm_mcp/config.py:96-98` reads all three from user config). A user-config value still wins, so existing installs keep working.

### 9.3 Can the plugin avoid the Desktop client secret? (brief §12 open question 1)

**What Google's documents say:**

- **IAP's "Authenticate from a desktop app"** walkthrough creates a Desktop client, allowlists it in `programmaticClients`, and sends **both** `client_id` and `client_secret` in the token exchange: https://cloud.google.com/iap/docs/authentication-howto. The Google-managed OAuth client cannot be used programmatically: https://cloud.google.com/iap/docs/custom-oauth-configuration (quoted in `apps/swarm-mcp/swarm_mcp/signin.py:7-11`).
- **Google's installed-app guide** says desktop apps "cannot keep secrets" and that the secret is not treated as confidential. It requires PKCE, which `sc login` already uses (`apps/swarm-mcp/swarm_mcp/signin.py:33-37`): https://developers.google.com/identity/protocols/oauth2/native-app. The runbook repeats the point (`docs/runbooks/iap-desktop-client.md:86`).
- **The IAP OAuth Admin API** (`gcloud iap oauth-brands`/`oauth-clients`) has been deprecated since 2025-01-22, so a Desktop client cannot be made by API: https://cloud.google.com/iap/docs/deprecations/migrate-oauth-client. Creating the client stays a guided console step (`docs/runbooks/iap-desktop-client.md:72-90`).

**Answer.** Google documents no secretless exchange for a Desktop client. Whether its token endpoint accepts a PKCE-only exchange for that client type is **not verified**: one request in P3 settles it. Even if it does not, the secret need not be withheld. Google classifies it as non-confidential, and the security boundary is elsewhere:

- the Internal brand, so only the organisation's accounts can complete consent;
- IAP's membership check;
- swarm-api's tenant resolution.

A person outside the org who holds the secret still cannot get an ID token IAP accepts.

**Recommendation** (open question 1; BGD decides, and this design builds neither branch until then):

1. In P3, run the PKCE-only exchange once. If Google accepts it, the plugin needs no secret, and the discovery document carries the client id only.
2. If Google refuses, the recommendation is that the discovery document also carries `oauth_client_secret`. That is consistent with Google's own classification and with the brief's goal that the plugin needs one value, but serving it unauthenticated is BGD's call, not part of the plan: the other options are in section 14 item 1.
3. Either way, the value lives in Secret Manager as `swarm-iap-desktop-client-secret` (a new id), entered by hidden prompt in stage 2, instead of a password manager.

The other branch the brief named, serving the secret "to signed-in members only", is circular: a member signs in *with* that client.

---

## 10. Uninstall and reinstall

**`swarm uninstall`:**

- needs a typed confirmation of the project id and ignores `SWARM_ASSUME_YES` (CLAUDE.md);
- walks the stage graph in reverse;
- deletes only what carries `managed-by=swarm-terraform` or `managed-by=swarm-install`, or what the install state recorded as created;
- refuses any name in the deny-list.

**Order:**

1. Turn off the Scheduler jobs, then wait for running executions to end (bounded, with a countdown).
2. Delete the tenant namespaces and the Kubernetes policies (`kubernetes/apply.sh` in reverse).
3. `terraform destroy` infra as the deployer. `make destroy` already aborts on an unlabelled resource.
4. Delete the images in the project's registry.
5. `terraform destroy` bootstrap.
6. Delete the secret versions and containers the engine created.
7. Remove the engine-made IAM bindings.
8. Delete the state bucket last, after writing the "what remains" record to the local cache and printing it.

**Names that outlive an uninstall, and what reinstall does:**

| Name | Why it survives | Reinstall (D24, run 2b) |
|---|---|---|
| OAuth brand | Cannot be deleted; there is no delete method: https://cloud.google.com/iap/docs/reference/rest/v1/projects.brands | Adopt: stage 2's check finds it and marks it met. |
| KMS key ring `swarm-<env>-specs` | Key rings and keys cannot be deleted; key versions can be destroyed. Google's page on deleting KMS resources (https://cloud.google.com/kms/docs/delete-kms-resources) must be **re-read in P2**: the research pass reported that newer documentation allows deleting an empty key ring, and the brief says rings are permanent. | Adopt: import the ring and key; create a new key **version**, never a new ring. |
| Custom role ids | A deleted custom role's id is reserved for 7 days, then can be undeleted or is purged (the reason `custom_role_suffix` exists). The exact window is to be re-read in P2. | If the uninstall is under 7 days old, **undelete** the roles (`gcloud iam roles undelete`) and adopt them; else re-create with the same ids. The suffix is not changed unless undelete fails. |
| Firestore database id | A deleted database's id can be reused after a delay (Google documents minutes, not days: https://cloud.google.com/firestore/docs/manage-databases). | Wait with a countdown; adopt nothing. |
| Desktop OAuth client | Deleting it is a console step; it can be kept. | Adopt it if its id is in the "what remains" record. |
| GitHub App | Only the org can delete it. | Offer reuse (a new key through the console) or a new App. |
| DNS record | At the company's registrar. | Check it still points at the new address; the address is new unless the reserved IP was kept. |
| Workspace groups, admin role or delegation | Org-admin objects. | Re-probed in stage 8. |

---

## 11. Upgrade

`swarm upgrade --to V`:

1. **Read and refuse.** Read the running version from the install state. A lower `V` is refused with exit 5 unless `--allow-downgrade` is given.
2. **Fetch and verify** release `V` (section 6.3).
3. **Plan everything, and show it before anything is applied:**
   - `terraform plan` for both roots against generated inputs;
   - the image digests that change;
   - the Firestore migrations between the two versions (`other.firestore-migrations`; today these are one-off scripts such as `scripts/workspace-migrate-record.sh`, `scripts/migrate-index-interval.sh` and `scripts/backfill-attempt-ends.sh`, which become numbered, idempotent migrations with a `schema_version` in the install state);
   - the cluster policy diff (`kubernetes/apply.sh --policies` dry-run);
   - new GitHub App permissions (section 9.1);
   - new manifest entries, each with its check.
4. **IAM gate.** Any plan that creates, changes or deletes an IAM binding, role or condition needs a typed approval. IAM conditions are linted first (`iamPolicies:lintPolicy`), against the limits brief §7 lists.
5. **Apply in this order:** copy images → Firestore migrations that only add fields → bootstrap → cluster policies that only admit more (new objects the new code needs, H33) → infra (the new code) → cluster policies that tighten → migrations that need the new code → `swarm doctor`.
   An IAM change the plan shows as destroy-then-create is refused unless the resource has `create_before_destroy`, because a destroy-first swap once left CI with no grant (H10).
6. **On failure,** stop. Record the failed step; the next `swarm upgrade` resumes at it. A failed image roll is rolled back by re-applying the previous release's digests.

Migrations run before the new images only when they add data; migrations that remove data run after `doctor` is green.

---

## 12. Add-ons

Each add-on is a `swarm addon enable|disable <name>`. Each has its own manifest entries (`scope: addon:<name>`), its own preflight, a hidden-prompt or one-time-link secret entry, a switch, and a check. Core passes stage 14 with every add-on off.

| Add-on | Entries | What enable does | Evidence |
|---|---|---|---|
| `bright-data-egress` | 9 | the egress credential and key, the egress switch, the `swarm-egress` workload | `apps/swarm-api/swarm_api/platform_secrets.py:107-109`; `terraform/infra/egress.tf` |
| `tailscale` | 4 | the OAuth secret, the tailnet target (no default; today `terraform/infra/tailscale.tf:84`), the `tailscale-egress` workload | `apps/swarm-api/swarm_api/platform_secrets.py:120-121` |
| `typesafe` | 12 | the forwarder key, `enable_typesafe_forwarder`, per-user key readers | `apps/swarm-api/swarm_api/platform_secrets.py:116-117`; `terraform/infra/typesafe_fwd.tf` |
| `brave-search` | 3 | `swarm-search-api-key` | `apps/swarm-api/swarm_api/platform_secrets.py:105` |
| `ci` | 43 | WIF pool and provider for GitHub Actions, the merge App, merge train, CI fixer (`swarm-ci-fix`, created outside Terraform, `terraform/bootstrap/ci_fix.tf:29-34`), sandbox repo, self-hosted runners, Cloud Build and its own service account, the post-deploy acceptance identity and fixtures, and the repository's own lint and scan tools (checkov, tflint, trivy, shellcheck, and docker for the local build loop) | `terraform/bootstrap/wif.tf:72`; `docs/runbooks/merge-app.md:54-68` |

Disabling an add-on turns its switch off, destroys its Terraform resources, and leaves its secret containers in place. Their deletion is a second, typed step.

---

## 13. Risks

| # | Risk | Likelihood | Effect | Mitigation |
|---|---|---|---|---|
| R1 | A serverless NEG cannot be created before its Cloud Run service exists (section 5.2). | Low | The audience cycle stays. | Split the frontend module, or keep an engine-driven second pass. Tested on the D22 project in P3. |
| R2 | The org enforces Domain Restricted Sharing on the release project, so the release home cannot be public. | Medium | Companies cannot download the release. | An org-policy exception on that one project (open question 4), or signed URLs from a small public endpoint. |
| R3 | The admin-role groups path does not cover `checkTransitiveMembership`. | Medium | A delegation tied to one mailbox stays. | Delegation remains the fallback; a dedicated non-person Workspace user (open question 2). |
| R4 | Moving platform service accounts into bootstrap disturbs the live deployment. | Medium | A destroy and re-create of SAs drops every grant. | The `removed { destroy = false }` + `import` pattern already used for custom roles; a plan that shows zero destroys is a merge gate. |
| R5 | The trivy gate blocks a release or an install on a same-day CVE (four times in October). | High | The release is late. | Acceptances with expiry in `RELEASE.md`; the installer says what is accepted and until when, and never skips a scan. |
| R6 | Certificate provisioning stalls on wrong DNS. | Medium | Stage 7 waits for ever. | Stage 2 checks DNS resolution against the reserved IP before waiting; the wait has a bound and says what to fix. |
| R7 | IAM propagation exceeds the bound (H19). | Medium | A spurious failure. | A 10-minute bound with retry; `doctor` tells propagation apart from a missing grant through the Policy Troubleshooter (H48). |
| R8 | A first install in a new org trips an org policy the preflight did not list. | Medium | A snag in P4. | Preflight reads the effective policy for every constraint the manifest names, and the snag log adds new ones. |
| R9 | The frozen contract holds a live literal that environment variables cannot override. | Low | A contract request is needed. | Open question 6; raised as a request, never an edit. |
| R10 | The Desktop client secret is served unauthenticated (section 9.3, if PKCE-only fails). | Certain if chosen | The value is public. | Google classifies it as non-confidential; the boundary is the Internal brand plus IAP. This is BGD's decision (open question 1). |
| R11 | The P0 inventory misses a dependency. | Medium | A hand step in P4. | The completeness test (section 3.3), the P2 adversarial review, and two proof installs. |

---

## 14. Open questions for BGD, and his answers

**BGD answered all nine on 2026-10-29 (GATE 0b review), each with the recommended option:**

| # | Question | Decision |
|---|---|---|
| 1 | The Desktop client secret | **(a)** PKCE-only if Google accepts it; otherwise the discovery document serves the secret. A teammate configures only the deployment URL. |
| 2 | Group reads | **(a)** The Workspace admin role Groups Reader on swarm-api's service account, proven in P4; delegation through a dedicated non-person user is the documented fallback. |
| 3 | The public GitHub receiver vs Domain Restricted Sharing | **(a)** Disable the invoker IAM check on `swarm-github-hooks` (no `allUsers` binding); the HMAC check stays the authentication. |
| 4 | The release home's public read | **(a)** One org-policy exception for `iam.allowedPolicyMemberDomains` on the release project alone, covering the registry reader and the bucket's object viewer. |
| 5 | The engine's home | **(a)** A new `apps/swarm-install` package behind the existing `swarm` CLI. |
| 6 | Frozen-contract literals | **(a)** P2 reads each one; any that environment variables cannot override becomes a contract request. |
| 7 | Moving platform service accounts into bootstrap | **(a)** The live deployment migrates too, in P3, plan-reviewed by BGD, with the `removed { destroy = false }` + `import` pattern. A plan with any destroy is a merge blocker. |
| 8 | Terraform pin | **Yes:** both roots move to `~> 1.16.2`. |
| 9 | The 48 history lessons | **(a)** The full list is attached as [`day-one/lessons.md`](day-one/lessons.md); P2's plan maps every lesson to a manifest entry, or says why none is needed. |

The questions as P0 put them follow, for the record.

1. **The Desktop client secret** (brief §12 Q1).
   - (a) PKCE-only if Google accepts it, else serve the secret in the discovery document.
   - (b) Never serve it; teammates get it from the admin out of band (today's model, three values).
   - (c) Build a non-IAP token-vending path to swarm-api for signed-in members.

   **Recommend (a)**, for the reasons in section 9.3.
2. **Group reads** (brief §12 Q2).
   - (a) The Workspace admin role "Groups Reader" on swarm-api's service account, with delegation as the fallback.
   - (b) Delegation impersonating a dedicated, non-person Workspace user.
   - (c) Delegation impersonating an admin's own mailbox (today).

   **Recommend (a)**, proven in P4, with (b) as the documented fallback.
3. **Domain Restricted Sharing vs the public GitHub receiver** (brief §12 Q3). The receiver needs `allUsers` invoker (`terraform/infra/github_hooks.tf:8`, `:83`).
   - (a) Disable the invoker IAM check on `swarm-github-hooks` (no `allUsers` binding; the HMAC check stays the authentication).
   - (b) Require an org-policy exception.
   - (c) Front it with a path on the load balancer.

   **Recommend (a)**, the same mechanism as the discovery service. If the org forbids disabling the invoker check, (b).
4. **The release home's public read.**
   - (a) An org-policy exception for `iam.allowedPolicyMemberDomains` on the release project alone.
   - (b) Put the release project outside the Saga org.
   - (c) Serve the bundle through signed URLs from a small public service.

   **Recommend (a)**: one project, one documented exception.
5. **The engine's home and the `swarm` name.**
   - (a) A new `apps/swarm-install` package behind the existing `swarm` CLI.
   - (b) Inside `apps/swarm-mcp`.
   - (c) A separate `swarmcloud` CLI.

   **Recommend (a)**: it keeps the plugin bridge small, and one command name for the company.
6. **Frozen-contract literals.** `apps/common/swarm_common/identity.py` and `models.py` hold live-deployment literals (appendix A). P0 did not establish whether every one is overridable by environment.
   - (a) P2 reads each one, and any that is not overridable becomes a contract request in `docs/contract-change-requests.md`.
   - (b) Accept them as documentation-only literals.

   **Recommend (a)**.
7. **Bootstrap service accounts migration for the live deployment** (section 5.3). It touches the live project's IAM state.
   - (a) Do it in P3, plan-reviewed by BGD, zero destroys.
   - (b) Keep the live deployment on the old layout and have only installs create SAs in bootstrap, through a variable.

   **Recommend (a)**: one layout, one code path.
8. **Terraform pin.** Raise `required_version` in both roots from `>= 1.9.0` to `~> 1.16.2`, to match `scripts/prerequisites.sh:57` and D20? **Recommend yes.** The engine refuses other versions either way.

9. **The 48 history lessons.** The report that lists them (brief §7) is not in the repository, so section 4.1 maps only the lessons the brief names.
   - (a) Attach the report, or its list, to the P2 plan, and the plan maps every lesson to a manifest entry.
   - (b) Accept the brief's named lessons as the set.

   **Recommend (a)**: the brief asks for all 48.

---

## 15. What this document did not verify

- No `terraform plan` or `apply`, and no probe, against any project. Every `check` is unrun.
- No live run of: a NEG created before its service; Cloud Run deterministic URLs as ID-token audiences; a PKCE-only exchange for a Desktop client; the admin-role groups path with `checkTransitiveMembership`; the invoker-check-disabled option under the org's policies; the KMS key ring and custom role id retention windows.
- The `apply` text of each manifest entry says what creates it today. The research passes read it at the cited lines; the merge did not re-read all 433 entries line by line.
- The per-service environment variable inventory was made by a research pass and is not in this PR; collector 3 replaces it in P3.

---

## Appendix A: every file with a live-deployment literal

The command in section 7 lists them. Columns: the file, the literals with their counts, and the first lines that hold them. 138 files.

| File | Literals | Lines |
|---|---|---|
| `.env.example` | saga-agents-staging×4 saga.xyz×5 | 29,51,61,113,164,174,… (8 lines) |
| `Makefile` | saga.xyz×3 | 381,382 |
| `apps/common/swarm_common/config.py` | saga.xyz×2 | 46,110 |
| `apps/common/swarm_common/identity.py` | saga-agents-staging×1 saga.xyz×3 | 8,68,70,169 |
| `apps/common/swarm_common/models.py` | saga-agents-staging×1 saga.xyz×2 | 164,857 |
| `apps/quota-broker/quota_broker/credentials.py` | saga-agents-staging×1 | 118 |
| `apps/quota-broker/quota_broker/publishledger.py` | saga-agents-staging×1 | 6 |
| `apps/quota-broker/quota_broker/secretstore.py` | saga-agents-staging×2 | 82,464 |
| `apps/reconciler/reconciler/backends.py` | saga-agents-staging×1 | 983 |
| `apps/reconciler/reconciler/checkpoints.py` | saga-agents-staging×3 | 57,312,409 |
| `apps/reconciler/reconciler/detect.py` | saga-agents-staging×1 | 809 |
| `apps/scheduler/scheduler/judgment.py` | taile0bda9×1 | 80 |
| `apps/scheduler/scheduler/on_step_failure_audit.py` | saga-agents-staging×1 | 11 |
| `apps/swarm-api/swarm_api/delegation.py` | saga.xyz×2 | 17,99 |
| `apps/swarm-api/swarm_api/forge_installation.py` | bogdan-alexandrescu×1 | 5 |
| `apps/swarm-api/swarm_api/groups.py` | saga-agents-staging×1 saga.xyz×1 | 4,6 |
| `apps/swarm-api/swarm_api/issueruns.py` | saga-agents-staging×18 | 1280,1281,1282,1284,1285,1286,… (18 lines) |
| `apps/swarm-api/swarm_api/judgment.py` | taile0bda9×1 | 115 |
| `apps/swarm-api/swarm_api/platform_secrets.py` | saga-agents-staging×1 | 17 |
| `apps/swarm-api/swarm_api/qatargets.py` | saga.xyz×1 | 219 |
| `apps/swarm-api/swarm_api/service.py` | saga.xyz×1 | 504 |
| `apps/swarm-api/swarm_api/settings.py` | saga.xyz×2 | 380,383 |
| `apps/swarm-egress/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,44,45 |
| `apps/swarm-github-hooks/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,52,53 |
| `apps/swarm-graph/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,53,54 |
| `apps/swarm-live/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,54,55 |
| `apps/swarm-live/swarm_live/recorder.py` | saga.xyz×1 | 161 |
| `apps/swarm-mcp/swarm_mcp/auth.py` | saga.xyz×1 | 173 |
| `apps/swarm-mcp/swarm_mcp/client.py` | saga.xyz×3 | 264,1510 |
| `apps/swarm-mcp/swarm_mcp/config.py` | saga.xyz×3 | 6,381 |
| `apps/swarm-mcp/swarm_mcp/launch.py` | saga.xyz×2 | 35,45 |
| `apps/swarm-mcp/swarm_mcp/signin.py` | saga.xyz×1 | 3 |
| `apps/swarm-mcp/swarm_mcp/workflows.py` | saga.xyz×1 | 893 |
| `apps/swarm-typesafe-fwd/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,48,49 |
| `apps/swarm-ui/src/IssueSubmit.tsx` | bogdan-alexandrescu×1 | 442 |
| `apps/swarm-ui/src/api.ts` | bogdan-alexandrescu×5 | 555,587,4786,5064,5332 |
| `apps/swarm-ui/src/styles/intake.css` | bogdan-alexandrescu×1 | 311 |
| `docker-compose.yml` | saga-agents-staging×1 saga.xyz×4 | 23,40,41,42,129 |
| `images/agent-runtime-base/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,61,62 |
| `images/agent-runtime-browser/Dockerfile` | saga-agents-staging×1 swarm-mirror-dockerhub×1 | 1 |
| `images/agent-runtime-browser/jev-browser/bin/jev-browser-mcp` | taile0bda9×1 | 74 |
| `images/agent-runtime-indexer/Dockerfile` | saga-agents-staging×2 swarm-mirror-dockerhub×2 | 1,49 |
| `images/swarm-api/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,44,45 |
| `images/swarm-quota-broker/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,44,45 |
| `images/swarm-reconciler/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,46,47 |
| `images/swarm-scheduler/Dockerfile` | saga-agents-staging×3 swarm-mirror-dockerhub×2 swarm-mirror-ghcr×1 | 1,46,47 |
| `images/swarm-ui/Dockerfile` | saga-agents-staging×2 swarm-mirror-dockerhub×2 | 1,62 |
| `images/swarm-verify/Dockerfile` | saga-agents-staging×2 swarm-mirror-dockerhub×2 | 1,58 |
| `images/workspace-apply/Dockerfile` | saga-agents-staging×2 swarm-mirror-dockerhub×1 swarm-mirror-ghcr×1 | 1,94 |
| `images/workspace-apply/entry.py` | saga-agents-staging×2 | 187,188 |
| `kubernetes/apply.sh` | saga-agents-staging×2 saga.xyz×4 | 9,147,360,368,1115,1124 |
| `kubernetes/namespaces/tenant-namespace.yaml` | saga.xyz×3 | 62,63,64 |
| `kubernetes/network-policies/allow-egress.yaml` | saga.xyz×5 | 105,106,107,108,109 |
| `kubernetes/network-policies/default-deny.yaml` | saga.xyz×1 | 59 |
| `kubernetes/network-policies/swarm-system-live.yaml` | saga.xyz×2 | 95,96 |
| `kubernetes/network_parity.py` | saga.xyz×7 | 21,69,74,75,76,77,… (7 lines) |
| `kubernetes/policies/workspace-provisioner-scope.yaml` | saga.xyz×11 | 65,157,158,199,252,253,… (11 lines) |
| `kubernetes/rbac/dispatcher-rbac.yaml` | saga.xyz×2 | 55,130 |
| `kubernetes/rbac/provisioner-rbac.yaml` | saga.xyz×1 | 71 |
| `kubernetes/rbac/verify-rbac.yaml` | saga.xyz×1 | 59 |
| `kubernetes/rbac/worker-rbac.yaml` | saga.xyz×1 | 54 |
| `kubernetes/render.py` | saga-agents-staging×2 saga.xyz×2 | 880,935,1304,1507 |
| `kubernetes/service-accounts/legacy-worker-serviceaccount.yaml` | saga.xyz×1 | 49 |
| `kubernetes/service-accounts/worker-serviceaccount.yaml` | saga.xyz×3 | 64,67,86 |
| `kubernetes/swarm-system/swarm-egress.yaml` | saga.xyz×1 | 159 |
| `kubernetes/swarm-system/swarm-graph.yaml` | saga.xyz×1 | 252 |
| `kubernetes/swarm-system/swarm-live.yaml` | saga.xyz×2 | 101,102 |
| `kubernetes/swarm-system/swarm-typesafe-fwd.yaml` | saga.xyz×1 | 172 |
| `kubernetes/swarm-system/tailscale-egress.yaml` | saga.xyz×1 | 169 |
| `kubernetes/worker-templates/worker-job-browser.yaml` | saga.xyz×3 | 54,55,56 |
| `kubernetes/worker-templates/worker-job-v2.yaml` | saga.xyz×4 | 62,63,64,69 |
| `kubernetes/worker-templates/worker-job.yaml` | saga.xyz×3 | 63,64,65 |
| `plugin/.claude-plugin/plugin.json` | bogdan-alexandrescu×1 | 75 |
| `plugin/workflows/run.js` | saga.xyz×2 | 1010,1114 |
| `scripts/acceptance/config.sh` | bogdan-alexandrescu×1 | 40 |
| `scripts/bootstrap.sh` | saga-agents-staging×1 | 5 |
| `scripts/build-images.sh` | saga-agents-staging×3 | 355,371,1765 |
| `scripts/destroy.sh` | saga-agents-staging×16 | 4,98,99,101,104,105,… (16 lines) |
| `scripts/dev/drive.py` | saga-agents-staging×2 | 24,28 |
| `scripts/lib/auth-guard.sh` | saga-agents-staging×2 saga.xyz×3 | 70,85,95 |
| `scripts/lib/build-mirror.sh` | swarm-mirror-dockerhub×1 swarm-mirror-ghcr×1 | 14,15 |
| `scripts/lib/check-contract-parity.sh` | saga.xyz×1 | 106 |
| `scripts/lib/common.sh` | bogdan-alexandrescu×1 saga-agents-staging×21 | 12,158,271,272,273,275,… (22 lines) |
| `scripts/lib/destroy-guard-proof-cases.json` | saga-agents-staging×1 | 36 |
| `scripts/lib/dev.sh` | saga-agents-staging×1 saga.xyz×1 | 13,93 |
| `scripts/lib/kubectl-guard.sh` | saga-agents-staging×2 | 61,69 |
| `scripts/lib/plan-guard.sh` | saga-agents-staging×13 | 4,395,399,403,404,407,… (11 lines) |
| `scripts/lib/quality_metrics.py` | bogdan-alexandrescu×2 | 35,81 |
| `scripts/lib/testlib.sh` | bogdan-alexandrescu×1 | 593 |
| `scripts/lib/workspace-calls.json` | saga-agents-staging×1 saga.xyz×2 | 9,152 |
| `scripts/lib/workspace-guard-cases.json` | saga-agents-staging×18 saga.xyz×3 | 5,40,56,67,79,94,… (21 lines) |
| `scripts/lib/workspace-guard.sh` | saga-agents-staging×1 saga.xyz×2 | 201,524,1494 |
| `scripts/prerequisites.sh` | saga-agents-staging×1 saga.xyz×3 | 156,157,212 |
| `scripts/quality-baseline.sh` | bogdan-alexandrescu×1 | 44 |
| `scripts/register-tenant.sh` | saga.xyz×4 | 90,276,302,2286 |
| `scripts/smoke-test.sh` | saga.xyz×1 | 149 |
| `scripts/swarm.py` | saga-agents-staging×2 | 37,41 |
| `scripts/verify-destroy-guard.sh` | saga-agents-staging×1 | 5 |
| `terraform/bootstrap/acceptance.tf` | saga-agents-staging×1 | 138 |
| `terraform/bootstrap/browser_logins.tf` | bogdan-alexandrescu×1 saga-agents-staging×1 | 79,163 |
| `terraform/bootstrap/deployer_conditions.tf` | saga-agents-staging×4 | 72,74,215,619 |
| `terraform/bootstrap/deployer_service_accounts.tf` | saga-agents-staging×1 | 6 |
| `terraform/bootstrap/forge_user_slots.tf` | saga-agents-staging×1 | 107 |
| `terraform/bootstrap/iap_programmatic_clients.tf` | saga-agents-staging×1 | 30 |
| `terraform/bootstrap/mcp_credentials.tf` | saga-agents-staging×1 | 96 |
| `terraform/bootstrap/platform_roles.tf` | saga-agents-staging×1 | 236 |
| `terraform/bootstrap/provider_keys.tf` | saga-agents-staging×1 | 97 |
| `terraform/bootstrap/spec_signing.tf` | saga-agents-staging×1 | 18 |
| `terraform/bootstrap/terraform.tfvars` | bogdan-alexandrescu×1 saga-agents-staging×10 saga.xyz×3 | 1,6,21,34,107,112,… (14 lines) |
| `terraform/bootstrap/terraform.tfvars.example` | saga-agents-staging×2 | 8,21 |
| `terraform/bootstrap/typesafe_forwarder.tf` | saga-agents-staging×1 | 74 |
| `terraform/bootstrap/variables.tf` | saga-agents-staging×5 saga.xyz×3 | 22,318,500,518,533,549,… (7 lines) |
| `terraform/bootstrap/wif.tf` | saga-agents-staging×5 | 267,285,286,442,455 |
| `terraform/bootstrap/workspace_secrets.tf` | saga-agents-staging×1 | 95 |
| `terraform/environments/dev/dev.tfvars` | bogdan-alexandrescu×1 saga-agents-staging×9 saga.xyz×20 taile0bda9×1 | 4,8,318,321,324,332,… (31 lines) |
| `terraform/environments/prod/prod.tfvars` | saga-agents-staging×2 saga.xyz×8 | 4,13,108,113,122,131,… (10 lines) |
| `terraform/infra/acceptance_fixture.tf` | saga.xyz×1 | 14 |
| `terraform/infra/backend.tf` | saga-agents-staging×1 | 6 |
| `terraform/infra/build_mirror.tf` | saga-agents-staging×1 | 32 |
| `terraform/infra/custom_roles_moved_to_bootstrap.tf` | saga-agents-staging×2 | 43,115 |
| `terraform/infra/deployer.tf` | saga-agents-staging×1 | 15 |
| `terraform/infra/locals.tf` | saga-agents-staging×2 saga.xyz×4 | 845,1014,1016,1033,1043,1336 |
| `terraform/infra/log_view.tf` | saga-agents-staging×1 | 23 |
| `terraform/infra/main.tf` | saga-agents-staging×1 | 39 |
| `terraform/infra/tailscale.tf` | taile0bda9×1 | 84 |
| `terraform/infra/tenant_job_vpc.tf` | saga-agents-staging×1 | 47 |
| `terraform/infra/variables.tf` | saga-agents-staging×1 saga.xyz×3 | 324,811,924,1118 |
| `terraform/modules/cloud_run/variables.tf` | saga.xyz×1 | 148 |
| `terraform/modules/firestore/variables.tf` | saga-agents-staging×1 | 12 |
| `terraform/modules/frontend/main.tf` | saga.xyz×1 | 80 |
| `terraform/modules/frontend/variables.tf` | saga.xyz×2 | 36,46 |
| `terraform/modules/gke_autopilot/main.tf` | saga-agents-staging×1 | 23 |
| `terraform/modules/gke_autopilot/variables.tf` | saga-agents-staging×1 | 173 |
| `terraform/modules/iam/bindings.tf` | saga-agents-staging×2 | 34,157 |
| `terraform/modules/project_services/main.tf` | saga-agents-staging×1 | 3 |
| `terraform/modules/project_services/variables.tf` | saga-agents-staging×1 | 23 |
| `terraform/modules/service_account_ids/main.tf` | saga-agents-staging×1 | 14 |
| `terraform/modules/tenancy/variables.tf` | saga.xyz×2 | 67,74 |
