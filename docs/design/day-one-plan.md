# SwarmCloud Day One: the P3 build plan (P2)

**Status:** P2 plan, for BGD's approval (GATE 2), with the adversarial review's 21 findings fixed in place (section 13). Part of #2688. Merge is off.
**Written:** 2026-10-29, against `main` at `b3fe3e6`.
**Inputs:** the design [`day-one-install.md`](day-one-install.md) (approved at GATE 0b, with BGD's nine answers in its section 14), the inventory [`day-one/manifest.draft.yaml`](day-one/manifest.draft.yaml), the 48 lessons in [`day-one/lessons.md`](day-one/lessons.md), the brief's decisions D1 to D25, and the GATE 1 picks D26 (DX1 B), D27 (DX2 B) and D28 (DX3 B, the marketplace in the release home).

This plan says **who builds what, in which order, and how CI proves it**. It reopens no decision. Where the code, read while planning, disagrees with the design, section 0 says what the plan does instead and why; none of those changes a decision.

**What was verified and what was not.** Every file path in a territory was checked to exist at `b3fe3e6`, or is marked **(new)**. The code facts this plan rests on were read at the cited lines while writing it. Nothing was applied, planned or probed against a Google Cloud project, a GitHub org or Claude Code's plugin installer. Facts that only a live try can settle are marked **to be proven**, each with the lane or manual step that proves it.

---

## 0. Where the plan refines the design (read first)

Each row is a fact found in the code while planning. None changes a decision; each changes how a lane builds it.

| # | Design says | The code says | The plan does |
|---|---|---|---|
| R1 | §5.6: `SIGNING_KEYS` "must come from the job's environment". | `images/workspace-apply/entry.py` empties `os.environ` before anything runs (module docstring, "THE ENVIRONMENT, NONE OF IT"); only the fixed command survives an override. | The trusted key name goes into the job's **fixed command** as `--spec-signing-key=projects/<P>/locations/<R>/keyRings/<ring>/cryptoKeys/step-spec`, rendered by bootstrap beside the existing `--spec-verify-keys-sha256` (`terraform/bootstrap/workspace_deployer.tf:123`). Lane DAYONE-BOOT-1. |
| R2 | §2.3: the console mirror is `swarm_install/state`. | D26 (later, BGD) names it "Firestore `swarm-install/<env>` plus the state object". | D26 wins: the engine's mirror is document `swarm-install/<env>`. Section 5.4 splits it into one document per writer so nothing races. |
| R3 | §9.1: the engine runs the GitHub App manifest flow through a loopback listener. | D26 moves the App to the console (B06). | swarm-api runs the flow: `redirect_url` is `https://<H>/admin/setup/github-app/callback`, and swarm-api performs the code exchange and writes the three secrets server-side (section 5.3). The loopback flow is not built. |
| R4 | §7: `SHARED_DENY_LIST` "is read from `SWARM_SHARED_DENY_LIST`". | CLAUDE.md rule 2: the deny-list lives **once**, in `scripts/lib/common.sh`. An environment variable that an operator forgets would silently empty the live guard. | The list stays in `common.sh`, keyed by project: the shared project's list applies when `PROJECT_ID` is the shared project, and it is empty for any other project. The engine never sources `common.sh` (design §7) and reads its deny-list (empty) from the install state. Lane DAYONE-DESAGA-2. |
| R5 | §7: Kubernetes files hold `saga.xyz` literals to render per project. | Almost all are **label and annotation keys** (`swarm.saga.xyz/tenant`, `kubernetes/namespaces/tenant-namespace.yaml:62-64`; `WORKSPACE_LABEL = "swarm.saga.xyz/workspace"`, `kubernetes/render.py:935`). Renaming a key breaks every live object and admission policy that matches it. The deployment literals are `--project default="saga-agents-staging"` (`kubernetes/render.py:880`) and `apply.sh`'s defaults. | Keys stay (open question 2). Lane DAYONE-K8S-1 removes the project defaults. The no-live-literal test exempts the `swarm.saga.xyz/` key prefix by name. |
| R6 | §6.1: one wheel, "the engine + the swarm/sc CLI". | §2.1 keeps the Google clients out of the plugin bridge. A single wheel would pull them into every teammate's bridge. | Two wheels per release: `swarm_mcp-<V>` (bridge, `sc`, `swarm` without the engine) and `swarm_install-<V>` (engine). `install.sh` installs both; the plugin's bridge source is the first only. |
| R7 | §4 stage table: stage 13 sets `require_alert_channels` after stage 9-12. | D26: the CLI does stage 13 before the hand-off, and every infra apply after stage 7 is another wait. | The engine asks for the alert email in stage 0, writes the channel and `require_alert_channels = true` into infra pass 2 (stage 7). Stage 13 is a check: the channel exists, the flag is on, and a test notification is accepted. |
| R8 | §9.1 step 5: infra pass 2 turns on the App switches. | Under D26 the App does not exist until the console's GitHub step, after the hand-off. | The CLI, waiting on the install record, sees the App land and runs **infra pass 3** (App ids and `enable_github_app`, `enable_forge_refresh`), then marks the console's self-check runnable; after the self-check it turns on the webhook and `enable_github_hooks` (pass 4, one switch). Section 5.5. |
| R9 | `terraform/modules/monitoring/variables.tf:115` | defaults the excluded tenants to `["smoke", "u-sw-c90291"]`, the live verify tenant. The manifest has no entry for it. | Lane DAYONE-TF-1 removes the default; the engine passes the deployment's verify tenant id, computed by importing the frozen `swarm_common.identity` rule. Added to the manifest as `declared-value.monitoring-excluded-tenants`. |

---

## 1. Waves and lanes

**32 lanes in 6 waves.** A wave starts when every lane it depends on is merged (held PRs included). Within a wave no two lanes share a file; section 1.7 lists every file once with its lane.

**Conventions for every lane.**

- **Runner:** `claude-code` (a SwarmCloud agent in `agent-runtime-base`). UI lanes are `claude-code`, then a `claude-code-browser` rendered review.
- **Tests:** red first. The lane pushes the tests alone to the PR branch, CI shows them red, then the fix goes to `<branch>-fix` (CLAUDE.md "Lanes"). Unit tests run with no credentials and no emulator.
- **Held:** a PR flagged `held: IAM`, `held: Terraform` or `held: workflow file` waits for BGD with its diff shown, and with a `terraform plan` summary when it is Terraform (BGD runs the plan; CI never applies a held change). A workflow change is written as `$SWARM_ARTIFACTS_DIR/workflow-lines.patch` and quoted in the PR body.
- **Before finishing:** the lane's named tests, `scripts/related-tests.sh` as CLAUDE.md says, `scripts/changed-guards.sh` once, `scripts/publish-scan.sh`.
- **The manifest is split by lane (review finding 1).** `install/manifest.yaml` (MANIFEST-1) holds the draft's entries; every lane that adds, changes or removes a collectable item (a Terraform resource or data source, a `bool` variable, an environment variable a service reads, a Scheduler job, an image, a Kubernetes object) owns its own fragment `install/manifest.d/<lane>.yaml` (new), named in its territory, and adds the entries for what it creates in the same PR. The loader merges the fragments; a fragment may restate a base entry by id with `replace: true` (how TF-2 rewrites `iam-binding.github-hooks-allusers`); two fragments restating one id fail. So the completeness test is green after every merge, in either direction, and no two lanes share a manifest file.
- **The base manifest comes first (review-fix finding 3).** The fragment format, the loader and the completeness test exist only once MANIFEST-1 is merged, so every lane that writes a fragment depends on DAYONE-MANIFEST-1 (directly or through a lane that does) and is in wave 1 or later. In wave 0 only MANIFEST-1 writes the manifest, and the other eight wave-0 lanes add no collectable item: DESAGA-1 keeps the same 15 Dockerfiles and only empties `ARG` defaults; DESAGA-2 and K8S-1 change scripts and rendering, not the API list, `POLICY_FILES` or `TENANT_FILES`; DESAGA-3 keys the deny-list by the project id swarm-api already reads (`settings.py:896`), so it adds no environment variable; DESAGA-4 adds no `VITE_*` name; TF-1 adds only non-`bool` variables and removes a default; BOOT-1 adds an `import` block (an `ignored:` type) and one argument of an existing job; RELHOME-1 is a script outside every collector. `install/manifest.yaml` therefore carries **every entry whose reference already exists on `main` when MANIFEST-1 merges**: the draft's 433, every section 1.8 addition with no collectable reference, and the two additions whose code is already in the tree (`declared-value.monitoring-excluded-tenants`, `terraform/modules/monitoring/variables.tf:105-115`; `switch.tenant_job_vpc_egress`, `terraform/infra/tenant_job_vpc.tf:31`). A `+` entry whose code a later lane creates is added by that lane's fragment, and only then.
- **New Python package:** `apps/swarm-install` (Q5). Its deps (Google client libraries, `pydantic`, `pyyaml`) are added once, by DAYONE-MANIFEST-1, so no later lane touches `pyproject.toml` or `uv.lock`.
- **Step and verb registries are discovered**, never listed: `swarm_install.steps` and `swarm_install.verbs` are packages scanned with `pkgutil.iter_modules`, so each lane adds its own module and no two lanes edit a shared registry file.
- **Manifest ids** below are the draft's ids; `+` marks an entry this plan adds (section 1.8).

### 1.1 Wave 0: foundations, the de-Saga sweep and the held Terraform groundwork (9 lanes)

**DAYONE-MANIFEST-1** — The manifest gets its home, its typed model and the test that fails on an unlisted dependency.
- Territory: `install/manifest.yaml` (new), `apps/swarm-install/pyproject.toml` (new), `apps/swarm-install/swarm_install/__init__.py` (new), `apps/swarm-install/swarm_install/manifest.py` (new), `tests/unit/install/__init__.py` (new), `tests/unit/install/test_manifest_complete.py` (new), `tests/unit/install/test_manifest_counts.py` (new), `pyproject.toml`, `uv.lock`.
- depends_on: none. Manifest: every entry whose reference exists on `main` when it merges (review-fix finding 3): the draft's 433 entries, section 1.8's additions that have no collectable reference (`other.*`, `workspace-delegation.groups-reader-admin-role`), and the two additions already in the tree (`+declared-value.monitoring-excluded-tenants`, `+switch.tenant_job_vpc_egress`); each other `+` entry is added by the lane that creates its code, in that lane's fragment, after this lane. Adds the `probe:`, `step:`, `refs:`, `expand:` fields and the `ignored:` and `env:` tables (design §3.1, §3.3).
- Red first: `test_manifest_complete.py::test_every_reference_is_listed` is committed against an empty `install/manifest.yaml` and fails with every collector's unmatched reference (`assert not unmatched, ...`), as design §3.3 says; `test_manifest_counts.py::test_counts_match_header` fails until the counts table matches. Also `test_every_entry_ref_still_matches` (a stale entry fails) and `::test_fragments_merged_and_ids_unique` (two fragments restating one id fail; a fragment's new entry is collected).
- Flags: none. Runner: claude-code. Estimate: **$35**.
- CI green proves: every Terraform resource and data source, API, service environment variable, secret id, Scheduler job, image, Kubernetes object and switch in the tree is listed or ignored with a reason, and the counts in the header are true.

**DAYONE-DESAGA-1** — The 15 Dockerfiles build in any project.
- Territory: `apps/swarm-egress/Dockerfile`, `apps/swarm-github-hooks/Dockerfile`, `apps/swarm-graph/Dockerfile`, `apps/swarm-live/Dockerfile`, `apps/swarm-typesafe-fwd/Dockerfile`, `images/agent-runtime-base/Dockerfile`, `images/agent-runtime-browser/Dockerfile`, `images/agent-runtime-indexer/Dockerfile`, `images/swarm-api/Dockerfile`, `images/swarm-quota-broker/Dockerfile`, `images/swarm-reconciler/Dockerfile`, `images/swarm-scheduler/Dockerfile`, `images/swarm-ui/Dockerfile`, `images/swarm-verify/Dockerfile`, `images/workspace-apply/Dockerfile`, `scripts/lib/build-mirror.sh`, `scripts/build-images.sh`, `tests/unit/scripts/test_build_mirror.py`.
- depends_on: none. Manifest: `declared-value.dockerfile-mirror-hardcoding`, `registry.mirror-dockerhub`, `registry.mirror-ghcr`, `bucket.build-mirror`.
- Red first: `test_build_mirror.py::test_no_dockerfile_names_a_project` asserts no tracked Dockerfile contains `saga-agents-staging` or `swarm-mirror-` and every `ARG MIRROR_*` default is empty; the existing lines 169-172 change with it (design §7).
- Flags: none (no workflow file; `build-images.sh` passes the live mirror explicitly so the live build is unchanged). Estimate: **$15**.
- CI green proves: the Dockerfiles carry no project; the live build still resolves its mirror through `build-images.sh`.

**DAYONE-DESAGA-2** — Scripts stop defaulting to the live project; the deny-list is keyed by project (R4).
- Territory: `scripts/lib/common.sh`, `scripts/prerequisites.sh`, `scripts/lib/dev.sh`, `scripts/register-tenant.sh`, `.env.example`, `tests/unit/scripts/test_common_defaults.py` (new).
- depends_on: none. Manifest: `other.shared-project-literal-in-both-envs`, `declared-value.project_id`, `local-tool.terraform` (pin), `local-tool.kubectl`.
- Red first: `test_common_defaults.py::test_unset_project_is_an_error` (sourcing `common.sh` with `PROJECT_ID` unset exits non-zero, today it defaults); `::test_deny_list_empty_for_other_project` and `::test_deny_list_full_for_shared_project` (the second passes today and must keep passing: the guard never weakens).
- Flags: **held: IAM** (review finding 15: `common.sh` holds the deny-list, and `register-tenant.sh` runs inside `swarm-workspace-apply` as the deployer with project-wide account-IAM power). Estimate: **$15**.
- CI green proves: no script silently targets the live project; the live deny-list is unchanged for the shared project.

**DAYONE-DESAGA-3** — Service code stops carrying live literals at runtime.
- Territory: `apps/swarm-api/swarm_api/issueruns.py`, `apps/swarm-api/swarm_api/platform_secrets.py`, `apps/swarm-api/swarm_api/settings.py`, `apps/swarm-api/swarm_api/service.py`, `apps/swarm-api/swarm_api/qatargets.py`, `apps/swarm-api/swarm_api/judgment.py`, `apps/swarm-api/swarm_api/forge_installation.py`, `apps/quota-broker/quota_broker/credentials.py`, `apps/quota-broker/quota_broker/publishledger.py`, `apps/quota-broker/quota_broker/secretstore.py`, `apps/reconciler/reconciler/backends.py`, `apps/reconciler/reconciler/checkpoints.py`, `apps/reconciler/reconciler/detect.py`, `apps/scheduler/scheduler/judgment.py`, `apps/scheduler/scheduler/on_step_failure_audit.py`, `apps/swarm-live/swarm_live/recorder.py`, `tests/unit/scripts/test_no_live_literals.py` (new).
- depends_on: none. Manifest: lesson 5's rule; `other.allowed-users-literal` (its swarm-api half).
- Red first: `test_no_live_literals.py::test_no_runtime_literal` walks every `ast.Constant` string under `apps/*/` (tests, comments and docstrings excluded; `apps/common` excluded because it is frozen, section 4; `apps/swarm-mcp` and `apps/swarm-ui` excluded because DAYONE-PLUGIN-1 and DAYONE-DESAGA-4 own them) and fails on `saga-agents-staging`, `saga.xyz` (except the `swarm.saga.xyz/` key prefix, R5), `bogdan-alexandrescu`, `taile0bda9`. It lists the appendix-A lines that are runtime today.
- Flags: none. The issueruns deny-list (design §7, `issueruns.py:1264-1270`) reads the same project-keyed list as R4, keyed by the project id swarm-api already reads (`settings.py:896`), so the lane adds no environment variable and needs no manifest fragment (review-fix finding 3). Estimate: **$20**.
- CI green proves: no service carries a live default; a comment may still mention history.

**DAYONE-DESAGA-4** — The console stops naming the live repository as a default. `UI (rendered review)`.
- Territory (3 UI files): `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/IssueSubmit.tsx`, `apps/swarm-ui/src/styles/intake.css`; test `apps/swarm-ui/src/__tests__/noLiveRepository.test.ts` (new).
- depends_on: none. Manifest: lesson 5.
- Red first: `noLiveRepository.test.ts` reads every file under `src/` (tests excluded) and fails on `bogdan-alexandrescu` (`api.ts:555`, `:587`, `:4786`, `:5064`, `:5332`; `IssueSubmit.tsx:442`; `intake.css:311`).
- Runner: claude-code, then claude-code-browser review of the intake form. Estimate: **$8**.
- CI green proves: no UI default points a new company at another org's repository.

**DAYONE-K8S-1** — Kubernetes rendering takes the project and the subjects from its caller, never a default.
- Territory: `kubernetes/render.py`, `kubernetes/apply.sh`, `tests/unit/scripts/test_k8s_render_project.py` (new).
- depends_on: none. Manifest: `cluster-policy.*` (9), `rbac.dispatcher-role`, `rbac.verify-role`, `rbac.worker-role`, `rbac.provisioner-clusterrole`, `namespace-object.tenant-resourcequota`, `cluster.swarm-system-namespace`.
- Red first: `test_k8s_render_project.py::test_project_is_required` (`render.py namespaces` and `rbac` with no `--project` exit 2; today they default, `render.py:880`); `::test_subjects_carry_unique_id` (every RoleBinding subject is rendered with both email and the numeric uniqueId passed in, lesson 29); `::test_quota_from_tenant_record` (a namespace's ResourceQuota is rendered from the tenant record's values passed in, never `render.py`'s default, #2039).
- `--project` is required only by the subcommands that render a project-bearing object (namespaces, RBAC, swarm-system); `render.py policies` keeps working with no `--project`, as `.github/workflows/security.yml:235` and `Makefile:343` call it, so neither file changes (review finding 14); `::test_policies_unchanged_without_project` runs exactly that call and asserts its output names no project.
- Flags: none. `apply.sh` refuses an unset project (design §7). Estimate: **$20**.
- CI green proves: no Kubernetes object can be rendered for the wrong project or with a default quota.

**DAYONE-TF-1** — Infra and bootstrap literals become inputs, both roots pin Terraform, and the declared-value cycle goes. `held: Terraform`.
- Territory: `terraform/infra/main.tf`, `terraform/infra/locals.tf`, `terraform/infra/variables.tf`, `terraform/infra/tailscale.tf`, `terraform/infra/child_tasks.tf`, `terraform/infra/versions.tf`, `terraform/infra/custom_roles_moved_to_bootstrap.tf`, `terraform/bootstrap/versions.tf`, `terraform/bootstrap/variables.tf`, `terraform/modules/monitoring/variables.tf`, `tests/unit/scripts/test_terraform_install_inputs.py` (new). No fragment: TF-1 adds no collectable item (its new variables are strings and lists, not `bool`), and removing a default leaves `declared-value.monitoring-excluded-tenants`'s reference in place; MANIFEST-1 carries that entry (review-fix finding 3).
- depends_on: none. Manifest: `other.allowed-users-literal`, `other.moved-block-project-literal`, `addon.tailscale-egress-target`, `declared-value.frontend_iap_audiences`, `declared-value.quota_broker_url`, `declared-value.frontend_iap_backends`, `declared-value.deployer_storage_buckets_exact`, `local-tool.terraform`, `+declared-value.monitoring-excluded-tenants`.
- Changes: `ALLOWED_USERS` from a variable (`locals.tf:1012-1015`); the tailnet host a variable with no default (`tailscale.tf:84`); `required_version = "~> 1.16.2"` in both roots (Q8); remove `module.cloud_run` from the frontend's `depends_on` (`main.tf:698`) and compute `IAP_AUDIENCES` from `module.frontend[0].iap_audiences` (`locals.tf:870`); compute `quota_broker_url` and `swarm_api_url` from the project number (design §5.2); `deployer_storage_buckets_exact` defaults to `[]`; `frontend_iap_backends` empty in pass 1; the monitoring excluded-tenants default removed (R9). The `moved` block in `custom_roles_moved_to_bootstrap.tf:115` is deleted **only if** BGD's live `terraform plan` shows it already applied (no pending move); otherwise it stays and the lane says so.
- Red first: `test_terraform_install_inputs.py` (text-level, offline): `::test_frontend_depends_only_on_services` (fails on `main.tf:698` today), `::test_iap_audiences_computed`, `::test_broker_url_computed`, `::test_required_version_pinned` (both roots), `::test_no_live_default` (no `default` in either root's variables names the live project, domain or tailnet).
- Flags: **held: Terraform**. BGD runs `scripts/plan.sh` for the live environments; the plan must show **zero destroys and zero replacements** (the `IAP_AUDIENCES` value must be identical to today's tfvars value). Estimate: **$30**.
- CI green proves: the module carries no live default and no hand-copied value; `terraform validate` and `fmt -check` pass (the terraform workflow). The NEG-before-service fact (design R1) is **to be proven** on the D22 test project in P3's pre-release run (section 10).

**DAYONE-BOOT-1** — Bootstrap becomes install-safe: the state bucket imports, an install may declare no Terraform tenants, workspace-apply trusts the project's own key. `held: Terraform`, `held: IAM`.
- Territory: `terraform/bootstrap/state_bucket.tf`, `terraform/bootstrap/deployer_service_accounts.tf`, `terraform/bootstrap/workspace_deployer.tf`, `images/workspace-apply/entry.py`, `tests/unit/scripts/test_workspace_apply_entry.py`, `tests/unit/scripts/test_bootstrap_install_safe.py` (new). No fragment: an `import` block is an `ignored:` type and the job gains one argument, so BOOT-1 adds no collectable item (review-fix finding 3).
- depends_on: none. Manifest: `bucket.state`, `other.bootstrap-sh-empty-state-guard`, `declared-value.infra_tenants_tfvars`, `declared-value.var-tenants-freeze`, `cloud-run-job.workspace-apply`, `other.spec-signing-keys`.
- Changes: an unconditional `import` block for `google_storage_bucket.state` (design §5.5); the tenants precondition (`deployer_service_accounts.tf:100-103`) conditional on `var.infra_tenants_tfvars != ""` (design §5.6); `entry.py`'s `SIGNING_KEYS` map is replaced by the `--spec-signing-key=` argument of the fixed command (R1), which `workspace_deployer.tf` renders from the module's own key, and which `entry.py` validates against `^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/keyRings/[A-Za-z0-9_-]+/cryptoKeys/step-spec$`.
- Red first: `test_workspace_apply_entry.py::test_signing_key_from_fixed_command` (a project other than the live one gets its key trusted; today `entry.py:183-190` trusts none) and `::test_signing_key_not_from_environment` (an env var carrying a key changes nothing); `test_bootstrap_install_safe.py::test_state_bucket_imported` and `::test_empty_tenants_admitted_for_installs`.
- Flags: **held: Terraform, held: IAM** (the job spec is IAM-adjacent: it decides whose signatures grant). The live plan must show the job's command gaining one argument and nothing else. Estimate: **$25**.
- CI green proves: a fresh project can create a team workspace (issue #2688's first finding), and bootstrap state can never be created twice.

**DAYONE-RELHOME-1** — A script stands up the release home's registry, bucket and signing key in project `swarmcloud-prod`, idempotently. `held: IAM`.
- Territory: `scripts/release-home.sh` (new), `tests/unit/scripts/test_release_home.py` (new).
- depends_on: none. Manifest: `+other.release-home`, `other.release-bundle`.
- What it does (BGD runs it; CLD does not create projects, D23): Artifact Registry repo `swarmcloud` (us, Docker), bucket `gs://swarmcloud` (uniform access, versioning), KMS key ring `swarmcloud-release` and an asymmetric P-256 key for cosign, each labelled `managed-by=swarm-release`; then, **only after** the Q4 org-policy exception exists (it checks the effective policy first), `allUsers` `roles/storage.objectViewer` on the bucket and `roles/artifactregistry.reader` on the repo. The public grant needs a typed confirmation of the project id that ignores `SWARM_ASSUME_YES`.
- Red first: `test_release_home.py::test_dry_run_prints_every_call` (with a fake `gcloud` on `PATH`), `::test_public_grant_refused_without_exception`, `::test_public_grant_needs_typed_confirmation`, `::test_refuses_shared_project` (the deny-list guard), `::test_shellcheck_clean`.
- Flags: **held: IAM** (public read on one project, Q4). Estimate: **$12**.
- CI green proves: the script is idempotent and guarded; BGD's run proves the exception.

### 1.2 Wave 1: the engine core, the service-account migration, the discovery app, a runtime-configured console, the group mode (5 lanes)

**DAYONE-DISCOVERY-1** — The discovery app serves `/.well-known/swarmcloud` and nothing else.
- Territory: `apps/swarm-api/swarm_api/discovery.py` (new), `install/manifest.d/discovery-1.yaml` (new; the `env:` rows for the variables `discovery.py` reads, which exist only once this lane's code does), `tests/unit/control_plane/test_discovery.py` (new).
- depends_on: DAYONE-MANIFEST-1 (its fragment needs the loader and the completeness test; moved from wave 0 by review-fix finding 3). Manifest: `other.discovery-endpoint`, `+cloud-run-service.swarm-discovery`.
- Built as section 6 says: a `create_app()` factory (uvicorn `--factory`), no Firestore client, its document built once at start from environment variables Terraform renders.
- Red first: `test_discovery.py::test_document_keys_are_exactly_the_public_set` (the response's keys equal section 6.2's list, and no value matches a secret pattern built at runtime); `::test_every_other_path_404s` (`/`, `/v1/...`, `/.well-known/other`); `::test_client_secret_only_when_switched` (absent unless `DISCOVERY_SERVE_CLIENT_SECRET=true`, and then read from Secret Manager, never from an env var); `::test_no_firestore_at_import`.
- Flags: none. Estimate: **$15**.
- CI green proves: the document's shape and that nothing private is served; serving through the load balancer is DAYONE-TF-2.

**DAYONE-IDENTITY-1** — swarm-api reads groups through the Workspace admin role (Q2) with delegation as the fallback, and one unreadable group fails only its own members.
- Territory: `apps/swarm-api/swarm_api/groups.py`, `apps/swarm-api/swarm_api/delegation.py`, `apps/swarm-api/swarm_api/settings.py` (↺: DESAGA-3 holds it in wave 0; `GROUPS_AUTH` is parsed in `ApiSettings.from_env`, `settings.py:905`), `install/manifest.d/identity-1.yaml` (new), `tests/unit/control_plane/test_groups_auth_mode.py` (new), `tests/unit/control_plane/test_groups_single_failure.py` (new).
- depends_on: DAYONE-DESAGA-3 (shares `settings.py` across waves; review finding 7), DAYONE-MANIFEST-1 (its fragment; review-fix finding 3). Manifest: `+declared-value.groups_auth`, `workspace-delegation.groups-dwd`, `workspace-group.tenant-groups`, `workspace-group.cloud-identity-presence`, `+workspace-delegation.groups-reader-admin-role`.
- Red first: `test_groups_auth_mode.py::test_admin_role_skips_delegation` (`GROUPS_AUTH=admin_role` calls `checkTransitiveMembership` with swarm-api's own credentials, never `delegation.delegate()`; fails today: there is no mode); `::test_unknown_mode_refused_at_start`. `test_groups_single_failure.py::test_one_unreadable_group_fails_only_its_members` (fails today: every request 503s, `docs/multi-tenancy.md:167-171`); `::test_unreadable_registry_still_503s` (kept).
- Flags: none (the env var is set by DAYONE-TF-2). Estimate: **$20**.
- CI green proves: the mode switch and the scoped failure, with fakes. The admin-role path against the real Groups API is **to be proven in P4** (design §8); delegation stays the fallback.

**DAYONE-ENGINE-1** — The engine: state store, lease, step loop, waits with a countdown, exit codes, `--json-events`, dry run.
- Territory: `apps/swarm-install/swarm_install/engine.py` (new), `apps/swarm-install/swarm_install/state.py` (new), `apps/swarm-install/swarm_install/lease.py` (new), `apps/swarm-install/swarm_install/waits.py` (new), `apps/swarm-install/swarm_install/events.py` (new), `apps/swarm-install/swarm_install/exitcodes.py` (new), `apps/swarm-install/swarm_install/record.py` (new), `apps/swarm-install/swarm_install/steps/__init__.py` (new), `apps/swarm-install/swarm_install/verbs/__init__.py` (new), `tests/unit/install/test_state_store.py` (new), `tests/unit/install/test_engine_loop.py` (new), `tests/unit/install/test_waits.py` (new), `tests/unit/install/test_exit_codes.py` (new), `tests/unit/install/test_record_writers.py` (new).
- depends_on: DAYONE-MANIFEST-1. Manifest: `other.install-state`, `+other.install-record-firestore`.
- Red first (fakes for GCS and Firestore, no credentials): `test_state_store.py::test_write_uses_if_generation_match` and `::test_conflicting_writer_loses` (two writers, one wins, the other re-reads); `::test_no_secret_value_is_stored` (a state holding a `SecretStr` refuses to serialise); `::test_local_cache_uploaded_once_bucket_exists`. `test_engine_loop.py::test_resume_skips_done_steps_after_recheck`, `::test_done_step_whose_check_fails_is_drift`, `::test_state_written_after_every_step` (a crash between two steps of a stage resumes at the second), `::test_stage_refuses_while_earlier_entry_unmet` (lesson 8), `::test_wait_runs_steps_whose_trigger_lands` (a wait step re-checks registered triggered steps on every poll and runs the ones whose trigger is met; how the hand-off runs passes 3 and 4, review finding 9). `test_waits.py::test_wait_gives_up_at_bound_with_exit_4` (fake clock), `::test_countdown_event_carries_expected_and_deadline`. `test_exit_codes.py::test_unknown_is_never_zero`. `test_record_writers.py::test_engine_writes_only_its_document` (section 5.4).
- Flags: none. Estimate: **$45**.
- CI green proves: resume, idempotence and bounded waits, on fakes.

**DAYONE-BOOT-2** — Every platform service account moves into bootstrap, for installs and for the live deployment (Q7), with zero destroys; the discovery and doctor accounts are created there. `held: Terraform`, `held: IAM`.
- Territory: `terraform/bootstrap/platform_service_accounts.tf` (new), `terraform/bootstrap/variables.tf`, `terraform/bootstrap/spec_signing.tf`, `terraform/bootstrap/verify_cluster_read.tf`, `terraform/bootstrap/outputs.tf`, `terraform/infra/removed.tf`, `terraform/infra/verify.tf`, `terraform/modules/iam/service_accounts.tf`, `terraform/modules/scheduler/main.tf`, `terraform/modules/scheduler/jobs.tf`, `terraform/modules/service_account_ids/main.tf`, `terraform/modules/service_account_ids/outputs.tf`, `terraform/modules/iam/outputs.tf`, `terraform/modules/iam/bindings.tf`, `terraform/infra/deployer.tf`, `terraform/infra/locals.tf` (↺: TF-1 wave 0, TF-2 wave 2), `install/manifest.d/boot-2.yaml` (new), `tests/unit/scripts/test_sa_migration_blocks.py` (new). The four added files reference the migrated accounts (`modules/iam/outputs.tf:49,53,58`, `bindings.tf:14,16,332`, `infra/locals.tf:1032`, `infra/deployer.tf:46`) and change from resource to data-source addresses (review finding 5).
- depends_on: DAYONE-TF-1 (shares `terraform/bootstrap/variables.tf` across waves), DAYONE-BOOT-1, DAYONE-MANIFEST-1 (its fragment; review-fix finding 3). Manifest: `sa.swarm-api`, `sa.swarm-quota-broker`, `sa.swarm-reconciler`, `sa.swarm-rollup-sweeper`, `sa.swarm-schedule-tick`, `sa.swarm-scheduler`, `sa.swarm-tick`, `sa.swarm-verify`, `switch.grant_broker_secret_lister`, `switch.swarm_workers_log_view_exists`, `iam-binding.swarm-api-spec-signer`, `iam-binding.verify-cluster-viewer`, `iam-binding.broker-secret-lister`, `+sa.swarm-discovery`, `+sa.swarm-doctor`.
- Changes: section 8. `removed { from = ...; lifecycle { destroy = false } }` in infra for `module.iam.google_service_account.platform`, `.tick`, `google_service_account.verify`, `module.scheduler.google_service_account.schedule_tick`, `.rollup_sweeper`; `google_service_account` resources in bootstrap with `import {}` blocks gated by `adopt_from_infra_states` exactly as `terraform/bootstrap/platform_roles.tf:425-437` does; infra reads the accounts as `data "google_service_account"`; the four "later" booleans go.
- Red first: `test_sa_migration_blocks.py::test_every_platform_sa_has_removed_destroy_false`, `::test_every_bootstrap_sa_has_gated_import`, `::test_infra_declares_no_platform_sa` (fails today on `terraform/modules/iam/service_accounts.tf:34`, `:48`, `terraform/infra/verify.tf:38`, `terraform/modules/scheduler/main.tf:236`, `terraform/modules/scheduler/jobs.tf:155`).
- Flags: **held: Terraform, held: IAM**. **Merge gate:** BGD's live plans (bootstrap, then infra dev and prod) show **0 to destroy** and every service account as "import" or "forget"; any destroy blocks the merge (Q7). Estimate: **$40**.
- CI green proves: the blocks are present and no root declares an account twice; the zero-destroy live plan is BGD's gate.

**DAYONE-UIENV-1** — The console reads its environment at runtime, so one image serves every deployment (brief §9). `UI (rendered review)`.
- Territory (3 UI files): `apps/swarm-ui/src/Brand.tsx`, `apps/swarm-ui/src/vite-env.d.ts`, `apps/swarm-ui/src/runtimeConfig.ts` (new); plus `images/swarm-ui/nginx.conf`, `images/swarm-ui/Dockerfile` (an entrypoint that writes `/config.json` from the container's environment); test `apps/swarm-ui/src/__tests__/runtimeConfig.test.ts` (new).
- depends_on: DAYONE-DESAGA-1 (shares `images/swarm-ui/Dockerfile` across waves). Manifest: `image.swarm-ui`.
- Red first: `runtimeConfig.test.ts` "Brand reads the environment from /config.json, not import.meta.env.VITE_SWARM_ENV" (fails today: `Brand.tsx:34`).
- Runner: claude-code, then claude-code-browser review of the brand strip in both environments. Estimate: **$15**.
- CI green proves: no environment is compiled into the console image.

### 1.3 Wave 2: the second Terraform pass, the engine's stages 0-2 and 8-15, the CLI verbs, the setup API, the plugin's one value (6 lanes)

**DAYONE-PLUGIN-1** — The plugin needs one value: the bridge reads discovery, checks version skew, and runs without a checkout.
- Territory: `apps/swarm-mcp/swarm_mcp/discovery.py` (new), `apps/swarm-mcp/swarm_mcp/config.py`, `apps/swarm-mcp/swarm_mcp/signin.py`, `apps/swarm-mcp/swarm_mcp/auth.py`, `apps/swarm-mcp/swarm_mcp/client.py`, `apps/swarm-mcp/swarm_mcp/launch.py`, `apps/swarm-mcp/swarm_mcp/workflows.py`, `plugin/.claude-plugin/plugin.json`, `plugin/commands/setup.md`, `plugin/commands/sc.md`, `plugin/skills/sc/SKILL.md`, `plugin/workflows/run.js`, `tests/unit/mcp/test_discovery_client.py` (new), `tests/unit/mcp/test_plugin_portable.py` (new), `tests/unit/mcp/test_version_skew.py` (new), `tests/unit/mcp/test_pkce_only_probe.py` (new).
- depends_on: DAYONE-DISCOVERY-1 (the document's schema). Manifest: `local-tool.sc-cli-config-file`, `other.plugin-mcp-bridge-fetch`, `oauth-client.desktop-sc`, `+other.plugin-marketplace`, `+other.plugin-version-skew`.
- Changes: section 6.4 and 6.5. `oauth_client_id` and `oauth_client_secret` become optional user config (a set value still wins, so the live deployment's three-value installs keep working); the bridge source becomes the release wheel URL (`https://storage.googleapis.com/swarmcloud/releases/<V>/swarm_mcp-<V>-py3-none-any.whl`, R6); `/sc:setup`'s `uv run …` hints become `swarm …` (`plugin/commands/setup.md:54`, `:133`); `sc login --pkce-only` is the one-request probe of Q1 (section 6.3).
- Red first: `test_discovery_client.py::test_one_value_fills_client_id_and_audience` (fails today: `config.py:96-98` reads three values); `::test_user_config_wins`; `::test_discovery_cached_and_refetched_on_401`. `test_version_skew.py::test_plugin_older_than_min_names_both_versions_and_fix`, `::test_deployment_older_than_supported_names_both_versions_and_fix`, `::test_skew_error_on_every_tool_call_never_silent`. `test_pkce_only_probe.py::test_probe_sends_no_secret`. `test_plugin_portable.py::test_plugin_has_no_checkout_hint_or_live_literal` (fails today: `plugin/skills/sc/SKILL.md` carries 44 `uv run` hints and the live front door at `:1061`; review finding 17).
- Flags: none. Estimate: **$35**.
- CI green proves: one value configures the plugin, and skew is always named; Claude Code's install of a plugin from a URL-hosted marketplace is **to be proven** by DAYONE-RELEASE-1's manual check (section 6.4).

**DAYONE-TF-2** — The front door serves discovery, the GitHub receiver needs no `allUsers`, swarm-api can store the App's secrets, and the hourly doctor exists. `held: Terraform`, `held: IAM`.
- Territory: `terraform/infra/discovery.tf` (new), `terraform/infra/doctor.tf` (new), `terraform/infra/github_hooks.tf`, `terraform/infra/locals.tf`, `terraform/infra/variables.tf`, `terraform/modules/frontend/main.tf`, `terraform/modules/frontend/variables.tf`, `terraform/modules/frontend/outputs.tf`, `terraform/modules/cloud_run/main.tf`, `terraform/modules/cloud_run/variables.tf`, `terraform/bootstrap/github_app_setup.tf` (new), `terraform/bootstrap/doctor_roles.tf` (new), `terraform/infra/main.tf` (↺: TF-1 wave 0; the `module "frontend"` block at `main.tf:683-699` gains the discovery backend's arguments, review finding 6), `terraform/modules/secret_manager/main.tf`, `terraform/infra/acceptance_fixture.tf`, `terraform/environments/dev/dev.tfvars`, `terraform/environments/prod/prod.tfvars`, `install/manifest.d/tf-2.yaml` (new), `tests/unit/scripts/test_terraform_day_one_edge.py` (new).
- depends_on: DAYONE-TF-1, DAYONE-BOOT-2 (the discovery and doctor accounts), DAYONE-DISCOVERY-1, DAYONE-IDENTITY-1. Manifest: `other.discovery-endpoint`, `+cloud-run-service.swarm-discovery`, `iam-binding.github-hooks-allusers` (corrected, section 1.8), `other.url-map`, `+iam-binding.swarm-api-github-app-secret-writer`, `+cloud-run-job.swarm-doctor`, `+sched.doctor-hourly`, `+iam-binding.doctor-read-only`, `+monitoring.doctor-findings-alert`, `+switch.enable_console_app_setup`, `+switch.enable_scheduled_doctor`, `+switch.enable_acceptance_fixture`, `+declared-value.groups_auth`, `+iam-binding.deployer-iap-api-accessor` (review-fix finding 2).
- Changes: `roles/iap.httpsResourceAccessor` for `swarm-tf-deployer` on the API backend's IAP only (never the UI backend), so the engine can call swarm-api's two engine routes (section 5.3); `swarm-discovery` (the swarm-api image, `command` = the discovery factory, ingress `internal-and-cloud-load-balancing`, `invoker_iam_disabled = true`, runs as `swarm-discovery` with no roles unless `discovery_serves_client_secret` is true, when it holds `secretAccessor` on `swarm-iap-desktop-client-secret` alone); a third backend without IAP and `path_rule { paths = ["/.well-known/swarmcloud"] }` beside `terraform/modules/frontend/main.tf:287-288`; `github_hooks_public` (`github_hooks.tf:314-320`) replaced by `invoker_iam_disabled = true` (Q3); `GROUPS_AUTH` rendered for swarm-api (default `delegation`, so the live deployment is unchanged until BGD flips it); under `enable_console_app_setup` (new bool, default false; the engine sets it in infra pass 2, review finding 2) the three `swarm-github-app-*` **containers** are created independently of `enable_github_app` and `enable_github_hooks` (today they exist only under those switches: `terraform/modules/secret_manager/main.tf:249-255`, `terraform/infra/github_hooks.tf:136-137`), with `secretmanager.versions.add` and `secretAccessor` for swarm-api on exactly those three, conditioned on their names; `acceptance_fixture.tf`'s bucket and its `allUsers` reader gated by `count = var.enable_acceptance_fixture ? 1 : 0` (new bool, default false, an `addon:ci` switch; `moved` blocks to index `[0]`; both live tfvars set it `true`, so the live plan shows no change; review finding 4); `swarm-doctor`'s email added to swarm-api's `PLATFORM_SERVICE_ACCOUNTS` (lesson 38's cause, review finding 12); the `swarm-doctor` Cloud Run job (the swarm-verify image, VPC egress as lesson 35), an hourly Scheduler job, the doctor's read-only roles (`roles/viewer`, `roles/iam.securityReviewer`, `roles/secretmanager.viewer`, `roles/container.clusterViewer`, `roles/policytroubleshooter.user`; it can invoke swarm-api to post findings and nothing else), and an alert on its finding count, the job and its schedule both gated by `enable_scheduled_doctor` (new bool, default false), so nothing runs hourly before DOCTOR-1's code ships; the engine turns it on (DOCTOR-1).
- Red first: `test_terraform_day_one_edge.py::test_no_allusers_binding_anywhere` (fails today on `github_hooks.tf:314`), `::test_discovery_path_rule_targets_non_iap_backend`, `::test_discovery_has_no_roles_by_default`, `::test_app_secret_writer_scoped_to_three_names`, `::test_app_secret_containers_exist_without_app_switches` (fails today), `::test_doctor_gated_off_by_default`, `::test_doctor_in_platform_service_accounts`, `::test_acceptance_fixture_off_by_default`, `::test_deployer_iap_accessor_on_api_backend_only` (review-fix finding 2), `::test_doctor_roles_are_read_only` (no role in the doctor's list holds a `.create`, `.update`, `.delete`, `.setIamPolicy` or `.add` permission, read from a checked-in list of each role's permissions).
- Flags: **held: Terraform, held: IAM**. BGD's live plan: create-only (no change to existing services except swarm-api's one new env var and swarm-github-hooks' invoker setting). The org policy `run.managed.requireInvokerIam` is **to be proven** absent or excepted (design §9.2); if it is enforced, the fallback is option (b), one path to swarm-api, and the lane stops and asks. Estimate: **$45**.
- CI green proves: the edge has no public binding and the doctor cannot write.

**DAYONE-ENGINE-2** — Stages 0, 1 and 2: preflight, project foundations, edge and identity prerequisites.
- Territory: `apps/swarm-install/swarm_install/steps/preflight.py` (new), `apps/swarm-install/swarm_install/steps/foundations.py` (new), `apps/swarm-install/swarm_install/steps/edge.py` (new), `apps/swarm-install/swarm_install/secret_entry.py` (new), `tests/unit/install/test_preflight.py` (new), `tests/unit/install/test_foundations.py` (new), `tests/unit/install/test_edge.py` (new), `tests/unit/install/test_secret_entry.py` (new).
- depends_on: DAYONE-ENGINE-1. Manifest: every stage 0, 1 and 2 entry (section 1.8 lists them by id in the coverage table, section 2.2).
- Red first: `test_preflight.py::test_missing_billing_link_right_stops_with_fix` (B01; no write happens: the fake records zero mutating calls), `::test_tool_versions_pinned` (terraform `1.16.2`, kubectl ≥ the pinned minor, lesson 12), `::test_region_confirmed_before_stage_1` (lesson 2), `::test_kms_signing_quota_read` (lesson 4), `::test_org_policies_read_effective`, `::test_alert_email_asked` (R7), `::test_inherited_bindings_checked_before_saying_missing` (lesson 16). `test_foundations.py::test_state_bucket_created_with_settings_then_state_uploaded` (lesson 7), `::test_default_compute_editor_removed` (lesson 6), `::test_one_api_list_from_manifest` (lesson 4), `::test_iam_write_rate_limit_retried_not_reported_as_denial` (lesson 3). `test_edge.py::test_brand_has_no_api_so_step_polls_its_check` (B02), `::test_dns_must_resolve_to_reserved_ip_before_cert_wait` (R6 of the design), `::test_cert_wait_bound_60_min`, `::test_iap_service_agent_created`. `test_secret_entry.py::test_hidden_prompt_value_goes_straight_to_add_secret_version` (the fake records the value only inside the Secret Manager call; stdout, stderr, the event stream and the state hold none of it), `::test_json_events_never_carry_a_value`, `::test_refuses_without_tty` (no `/dev/tty` means no prompt: `getpass`'s stdin fallback is disabled, the step emits a `needs_terminal` event naming the `! swarm install --resume` line and exits 4, so a value can never be piped in by a Claude session; review finding 11). `test_preflight.py::test_release_cve_acceptances_shown_with_expiry` (lesson 27).
- Flags: none (the engine applies at run time; this lane changes no Terraform). Estimate: **$30**.
- CI green proves: B01-B03 on fakes, and that the Desktop client secret only ever reaches Secret Manager.

**DAYONE-ENGINE-4** — Stages 8, 10 and 14.
- Territory: `apps/swarm-install/swarm_install/steps/identity.py` (new), `apps/swarm-install/swarm_install/steps/cluster.py` (new), `apps/swarm-install/swarm_install/steps/verify.py` (new), `apps/swarm-install/swarm_install/api_client.py` (new: the engine's calls to swarm-api, section 5.3; review-fix finding 2), `tests/unit/install/test_identity_step.py` (new), `tests/unit/install/test_cluster_step.py` (new), `tests/unit/install/test_verify_step.py` (new), `tests/unit/install/test_api_client.py` (new). The hand-off wait moved to ENGINE-3, which builds the passes it runs (review finding 9).
- depends_on: DAYONE-ENGINE-1, DAYONE-K8S-1, DAYONE-IDENTITY-1. Manifest: every stage 8, 10 and 14 entry; `+iam-binding.deployer-iap-api-accessor` (consumed), `+other.verify-row`, `+other.release-kubernetes-bundle` (stage 10 renders from the bundle's `kubernetes.tgz`, never a checkout).
- Red first: `test_identity_step.py::test_group_registered_only_after_probe_passes` (lesson 20; the 503 trap), `::test_unauthorized_client_names_admin_console_not_iam` (B04), `::test_admin_role_path_offered_first` (Q2), `::test_probe_runs_through_swarm_api_in_its_mode` (review finding 8). `test_api_client.py::test_id_token_minted_for_deployer_with_iap_audience` (the faked `generateIdToken` call names `swarm-tf-deployer`, the API backend's IAP audience and `includeEmail`), `::test_never_sends_a_gcloud_user_token` (lesson 18), `::test_only_hook_and_groups_probe_routes` (review-fix finding 2). `test_cluster_step.py::test_policies_before_tenant_objects` (lesson 33), `::test_subjects_from_unique_id_read_from_iam` (lesson 29), `::test_kubectl_resolved_through_prefer_local_bin`, `::test_context_is_the_install_cluster` (lesson 37). `test_verify_step.py::test_every_scheduler_target_must_answer_2xx` (lesson 38), `::test_digests_read_never_labels` (lesson 28), `::test_verify_progress_written_with_countdown` (the console's verify row, D26), `::test_four_end_to_end_runs_each_to_a_pr` (plugin and console, single agent and workflow), `::test_reconciler_can_list_jobs` (lesson 30), `::test_plugin_run_signs_in_with_desktop_client_not_gcloud_token` (lesson 18), `::test_runs_use_installation_tokens` (lesson 43: the run's audit records an App installation token, never a PAT), `::test_cluster_objects_from_release_bundle`.
- Flags: none. Estimate: **$25**.
- CI green proves: B04 on fakes, and the console's verify row gets its data.

**DAYONE-CLI-1** — `swarm install` (with `--status`) reaches the engine through the existing `swarm` command.
- Territory: `apps/swarm-mcp/swarm_mcp/cli.py`, `apps/swarm-install/swarm_install/cli.py` (new), `apps/swarm-install/swarm_install/verbs/install.py` (new), `tests/unit/mcp/test_swarm_install_verbs.py` (new), `tests/unit/install/test_cli.py` (new).
- depends_on: DAYONE-ENGINE-1. Manifest: `other.ready-screen`.
- Red first: `test_swarm_install_verbs.py::test_missing_package_prints_one_install_line` (exit 2, the line from `install.sh`); `::test_verbs_delegate_lazily` (importing `swarm_mcp.cli` imports no Google client); `::test_existing_swarm_status_unchanged` (`swarm status` stays the teammates' one-line-per-agent command, `apps/swarm-mcp/swarm_mcp/cli.py:1870`; the install's status is `swarm install --status`, review finding 13). `test_cli.py::test_verbs_discovered_from_package`, `::test_dry_run_writes_nothing`, `::test_json_events_schema`.
- Flags: none. Estimate: **$20**.
- CI green proves: one `swarm` command, the engine optional for teammates.

**DAYONE-SETUP-API-1** — swarm-api's routes behind Admin › Setup: the install record, the GitHub manifest flow and code exchange, pool-account sign-in links, team creation, add-ons and Health.
- Territory: `apps/swarm-api/swarm_api/routes/setup.py` (new), `apps/swarm-api/swarm_api/install_record.py` (new), `apps/swarm-api/swarm_api/setup_github.py` (new), `apps/swarm-api/swarm_api/setup_links.py` (new), `apps/swarm-api/swarm_api/main.py`, `apps/swarm-api/swarm_api/settings.py` (↺: IDENTITY-1 wave 1; the GitHub org, hostname and the doctor's account, review finding 7), `install/manifest.d/setup-api-1.yaml` (new), `tests/unit/control_plane/test_setup_routes.py` (new), `tests/unit/control_plane/test_setup_github_exchange.py` (new), `tests/unit/control_plane/test_setup_links.py` (new), `tests/unit/control_plane/test_install_record_writers.py` (new).
- depends_on: DAYONE-ENGINE-1 (the record's schema; `test_install_record_writers.py` imports `swarm_install.record` and asserts parity). Manifest: `github-app-setting.*` (core), `github-app-installation.per-owner`, `secret.swarm-github-app-*.value`, `claude-account.subscription`, `workspace-group.tenant-groups`, `+other.install-record-firestore`, `+other.verify-row`.
- Routes: section 5.3.
- Red first: `test_setup_routes.py::test_platform_admin_only` (a workspace admin gets 403 on every route), `::test_reads_engine_document_never_writes_it`. `test_setup_github_exchange.py::test_state_checked_before_exchange`, `::test_exchange_writes_three_secrets_and_returns_no_value` (the response body, the log records and the Firestore write hold none of `pem`, `client_secret`, `webhook_secret`; fakes build them with `secrets.token_hex`), `::test_manifest_requests_statuses_read` (lesson 46), `::test_hook_inactive_until_secret_has_version`, `::test_hook_activation_signed_server_side` (`POST /v1/admin/setup/github-app/hook` signs the App JWT inside swarm-api; the caller's request and the response carry no key material; review finding 10). `test_setup_links.py::test_link_single_use_and_expires_24h`, `::test_link_bound_to_named_account_owner`, `::test_link_kinds_pool_account_team_git_addon_secret` (one link route serves the three kinds; review finding 21), `::test_pool_link_refused_before_first_team` (409 with "create the first team first" while no team record exists), `::test_pool_link_owner_must_be_team_member` (an owner outside the first team's group is refused), `::test_pool_link_sign_in_lands_in_first_team` (the redeemed sign-in's `owner_tenant` is the first team, through the existing `/authorize` → `/exchange` routes, faked broker; review-fix finding 1). `test_setup_routes.py::test_groups_probe_answers_with_api_identity` (the doctor's registered-group re-probe goes through swarm-api, which holds the Groups Reader role; review finding 12). `test_install_record_writers.py::test_api_writes_only_console_documents`. `test_setup_routes.py::test_engine_identity_admitted_only_on_hook_and_probe` (the verified email `swarm-tf-deployer@<project>` gets through on the hook and groups-probe routes and 403 on every other route; review-fix finding 2).
- Flags: none (the IAM it needs is DAYONE-TF-2). Estimate: **$40**.
- CI green proves: the console's half writes only its own documents and never returns a secret.

### 1.4 Wave 3: Terraform composition, the team verb, the setup panels, `/sc:install`, the release scripts (5 lanes)

**DAYONE-RELEASE-1** — The release is assembled, signed and published to the release home, and the plugin marketplace with it. `held: workflow file`.
- Territory: `scripts/release/assemble.sh` (new), `scripts/release/publish.sh` (new), `scripts/release/marketplace.py` (new), `install/install.sh` (new), `install/compat.json` (new), `tests/unit/scripts/test_release_assemble.py` (new), `tests/unit/scripts/test_release_publish.py` (new), `tests/unit/scripts/test_marketplace.py` (new); workflow patch `$SWARM_ARTIFACTS_DIR/workflow-lines.patch` adding `.github/workflows/release-public.yml` (quoted in the PR body, pushed by the operator).
- depends_on: DAYONE-RELHOME-1, DAYONE-MANIFEST-1, DAYONE-DESAGA-1, DAYONE-UIENV-1, DAYONE-PLUGIN-1. Manifest: `other.release-bundle`, `image.copy-to-company-registry`, `+other.release-home`, `+other.plugin-marketplace`, `+other.release-kubernetes-bundle`.
- Section 7 has the pipeline.
- Red first: `test_release_assemble.py::test_module_has_no_tfvars_or_environments` (design §5.1), `::test_two_wheels` (R6), `::test_sha256sums_cover_every_file`, `::test_images_json_digests_only`, `::test_bundle_carries_kubernetes` (`kubernetes.tgz`: `render.py`, `policies/`, `swarm-system/`, `rbac/`, the tenant templates; stage 10, team add, upgrade and uninstall run from it with no checkout; review finding 3). `test_release_publish.py::test_publish_refuses_without_manual_test_record` (D22), `::test_channel_moved_only_by_promote`, `::test_cve_acceptances_have_expiry` (lesson 27). `test_marketplace.py::test_marketplace_names_plugin_sc_at_version`, `::test_plugin_bundle_carries_supported_deployment_range`.
- Flags: **held: workflow file**. Estimate: **$30**.
- CI green proves: the bundle's shape. The publish itself is a manual run (section 7.3).

**DAYONE-ENGINE-3** — Stages 3 to 7, the hand-off wait and the App's switches after the console, stage 13 and READY: Terraform driven by the engine as the deployer.
- Territory: `apps/swarm-install/swarm_install/terraform.py` (new), `apps/swarm-install/swarm_install/tfvars.py` (new), `apps/swarm-install/swarm_install/adopt.py` (new), `apps/swarm-install/swarm_install/steps/bootstrap.py` (new), `apps/swarm-install/swarm_install/steps/images.py` (new), `apps/swarm-install/swarm_install/steps/infra.py` (new), `apps/swarm-install/swarm_install/steps/secrets.py` (new), `apps/swarm-install/swarm_install/steps/github_switches.py` (new), `apps/swarm-install/swarm_install/steps/alerts.py` (new), `apps/swarm-install/swarm_install/steps/handoff.py` (new), `apps/swarm-install/swarm_install/tools.py` (new: pinned versions, SHA-256 and download of cosign and gcrane), `apps/swarm-install/swarm_install/release_key.pub` (new: the cosign public key of `swarmcloud-release/cosign`, which exists once BGD has run `scripts/release-home.sh`; a precondition of this lane's start), `tests/unit/install/test_tfvars.py` (new), `tests/unit/install/test_terraform_runner.py` (new), `tests/unit/install/test_images_step.py` (new), `tests/unit/install/test_switches.py` (new), `tests/unit/install/test_adopt.py` (new), `tests/unit/install/test_alerts_step.py` (new), `tests/unit/install/test_handoff.py` (new), `tests/unit/install/test_tools.py` (new).
- depends_on: DAYONE-ENGINE-1, DAYONE-ENGINE-4 (stage 14, which the hand-off triggers), DAYONE-TF-1, DAYONE-BOOT-1, DAYONE-BOOT-2, DAYONE-TF-2, DAYONE-SETUP-API-1 (the hook route), DAYONE-RELHOME-1 **run by BGD** (the public key). Manifest: every stage 3-7 entry, stage 9's switches, stage 13's and 15's entries, `local-tool.cosign`, `local-tool.gcrane`, `+other.release-signing-public-key`, `+other.engine-terraform-rules`, `+switch.enable_console_app_setup`, `+switch.tenant_job_vpc_egress`, `+declared-value.groups_auth`.
- Red first: `test_tfvars.py::test_generated_never_from_committed_tfvars` (lesson 5, design §5.1), `::test_no_dash_var_ever` (lesson 11), `::test_inputs_stored_in_state_bucket`, `::test_pass2_sets_console_app_setup` (review finding 2), `::test_groups_auth_admin_role_for_installs` (Q2; `delegation` only when the person chose the fallback at stage 8; review finding 8), `::test_tenant_job_vpc_egress_true` (lesson 35), `::test_traffic_latest_100` (lesson 28). `test_terraform_runner.py::test_every_apply_after_pass1_impersonates_deployer` (lessons 22, 23), `::test_plan_with_destroy_refused_without_typed_approval` (lesson 11), `::test_iam_replace_without_create_before_destroy_refused` (lesson 10), `::test_conditions_linted_before_apply` (lesson 10's limits), `::test_terraform_version_checked` (lesson 12), `::test_init_before_plan` (lesson 12), `::test_freshness_from_state_not_registry` (lesson 9), `::test_empty_state_refused_when_deployer_exists` (lesson 7), `::test_deployer_act_as_checked_before_apply` (lesson 23). `test_images_step.py::test_cosign_verified_before_copy`, `::test_copy_by_digest_only` (lesson 13), `::test_verified_with_pinned_release_key`. `test_tools.py::test_download_checked_against_pinned_sha256`. `test_alerts_step.py::test_alert_policies_after_metric_descriptor` (lesson 23). `test_switches.py::test_switch_true_only_with_enabled_version` (lesson 39), `::test_app_switches_after_console_app_lands` (R8), `::test_hooks_switch_after_self_check`, `::test_engine_never_reads_an_app_secret` (the webhook is activated through swarm-api's hook route; the engine holds no accessor on any `swarm-github-app-*` secret; review finding 10). `test_handoff.py::test_one_url_printed_once_console_answers` (B05), `::test_cli_lease_heartbeat_while_waiting`, `::test_app_landing_triggers_pass3_then_hook_then_pass4` (passes 3 and 4 are triggered steps of the wait, ENGINE-1's `test_wait_runs_steps_whose_trigger_lands`), `::test_console_finish_triggers_verify` (B09b), `::test_ready_only_after_verify_passes`. `test_adopt.py::test_kms_ring_imported_never_recreated`, `::test_custom_roles_undeleted_within_7_days`, `::test_brand_found_and_adopted`, `::test_desktop_client_adopted_or_asked`, `::test_github_app_reuse_or_new_name`, `::test_firestore_id_waited_with_countdown`, `::test_remains_rederived_by_probe_without_cache` (D24; review finding 18).
- Flags: none (it runs Terraform at install time against the company's project; it changes no `.tf` file). Estimate: **$55**.
- CI green proves: the four-apply sequence, key before switch, the hand-off (B05, B09b) and adoption on fakes; the real applies are P4.

**DAYONE-TEAM-1** — `swarm team add <group>` and the console's team creation drive one path: swarm-api's team route, then `swarm-workspace-apply`.
- Territory: `apps/swarm-install/swarm_install/verbs/team.py` (new), `apps/swarm-install/swarm_install/team.py` (new), `scripts/workspace-apply.sh`, `scripts/register-tenant.sh` (↺: DESAGA-2 wave 0; the namespace, ResourceQuota and `--providers` logic lives here, `register-tenant.sh:300-325`, `:1889`; review finding 15), `tests/unit/install/test_team_add.py` (new), `tests/unit/scripts/test_workspace_apply_namespace.py` (new).
- depends_on: DAYONE-SETUP-API-1, DAYONE-CLI-1, DAYONE-K8S-1, DAYONE-BOOT-1. Manifest: every stage 11 entry, `secret.swarm-tenant-git-u.value` (its `secret_entry` is the one-time link).
- Red first: `test_team_add.py::test_group_probe_before_registration` (lesson 20), `::test_team_git_value_only_through_link` (prints the one-time link; never prompts in the terminal for a forge token, CLAUDE.md), `::test_ends_with_smoke_green`, `::test_smoke_waits_for_a_pool_account` (the smoke agent starts only once a pool account is owned by or lent to the team; for the first team, stage 12's accounts; the wait says so; review-fix finding 1). `test_workspace_apply_namespace.py::test_namespace_quota_from_tenant_record` (lesson 32), `::test_verify_keys_configmap_present` (lesson 31), `::test_add_provider_never_replaces` (lesson 40).
- Flags: **held: IAM** (both scripts run inside `swarm-workspace-apply` as the deployer with account-IAM power). Estimate: **$30**.
- CI green proves: a team ends with namespace, quota, verify-keys, worker identity, slots and a green smoke, on fakes.

**DAYONE-SETUP-UI-1** — The Admin › Setup step panels, the GitHub App callback, the Health panel and the setup API client. `UI (rendered review)`. The starting point is P1-B's `variants.patch` (`AdminSetupMock.tsx`, `admin-setup-mock.css`; not in the tree: CLD attaches it to this lane's brief from task_201016d500db49fab184's artefacts), MOCK-HOOKs stripped.
- Territory (3 UI files): `apps/swarm-ui/src/AdminSetupSteps.tsx` (new; also exports `GitHubAppCallback`, the `/admin/setup/github-app/callback` page that posts `{code, state}` to the conversion route), `apps/swarm-ui/src/AdminSetupHealth.tsx` (new), `apps/swarm-ui/src/api.ts` (↺: DESAGA-4 wave 0; the setup routes' client, review finding 16); tests `apps/swarm-ui/src/__tests__/AdminSetupSteps.test.tsx` (new), `apps/swarm-ui/src/__tests__/AdminSetupHealth.test.tsx` (new).
- depends_on: DAYONE-SETUP-API-1 (the shapes). Manifest: `other.ready-screen`, `+other.verify-row`, `+other.doctor-findings`.
- Red first: `AdminSetupSteps.test.tsx` "the verify row shows a countdown from expected and deadline" (the P1 gap), "Send to your team stays locked until verify passes", "a failed step shows its fix and a retry", "the plugin line carries only the deployment URL", "the callback posts code and state once and shows secrets stored without a value"; `AdminSetupHealth.test.tsx` "a finding shows the repair command and the console path".
- Runner: claude-code, then claude-code-browser review at 1440 and 390 wide (B06-B09a, B11, B12b). Estimate: **$20**.
- CI green proves: every state of section 5.1 renders from fixtures.

**DAYONE-SC-INSTALL-1** — `/sc:install`, `/sc:doctor` and `/sc:team` drive the engine and never see a value.
- Territory: `plugin/commands/install.md` (new), `plugin/commands/doctor.md` (new), `plugin/commands/team.md` (new), `tests/unit/mcp/test_plugin_install_commands.py` (new).
- depends_on: DAYONE-CLI-1, DAYONE-ENGINE-2 (the `needs_terminal` event). Manifest: `+other.sc-install-refusal-rule`.
- Red first: `test_plugin_install_commands.py::test_commands_run_json_events_only`, `::test_no_value_in_any_prompt_or_tool_schema`, `::test_refusal_hands_exact_command` (lesson 47: on an auto-mode refusal the command text tells Claude to print the `!` line or ask for a narrow allow rule, and contains no workaround), `::test_secret_step_hands_bang_command` (on a `needs_terminal` event the command tells Claude to hand the person the `! swarm install --resume` line and never to ask for, read or pipe the value; review finding 11).
- Flags: none. Estimate: **$10**.
- CI green proves: the skill is a face over `--json-events`.

### 1.5 Wave 4: doctor, uninstall, upgrade, add-ons, the setup page (5 lanes)

**DAYONE-DOCTOR-1** — `swarm doctor`'s platform tier, the hourly scheduled doctor, and `swarm install --repair <check>`.
- Territory: `apps/swarm-install/swarm_install/doctor.py` (new), `apps/swarm-install/swarm_install/scheduled_doctor.py` (new), `apps/swarm-install/swarm_install/repair.py` (new), `apps/swarm-install/swarm_install/verbs/doctor.py` (new), `apps/swarm-install/swarm_install/verbs/install.py`, `apps/swarm-mcp/swarm_mcp/cli.py`, `images/swarm-verify/Dockerfile`, `apps/swarm-install/swarm_install/tfvars.py` (↺: ENGINE-3 wave 3; turns `enable_scheduled_doctor` on for a release that carries this lane), `install/manifest.d/doctor-1.yaml` (new), `tests/unit/install/test_doctor.py` (new), `tests/unit/install/test_scheduled_doctor.py` (new), `tests/unit/install/test_repair.py` (new).
- depends_on: DAYONE-ENGINE-2, DAYONE-ENGINE-3, DAYONE-ENGINE-4, DAYONE-CLI-1, DAYONE-TF-2, DAYONE-SETUP-API-1. Manifest: every entry's `check` (doctor re-runs them), `+cloud-run-job.swarm-doctor`, `+other.doctor-findings`.
- **The fix verb is `swarm install --repair <check>`, not `swarm doctor --fix`.** Why: `doctor` never writes (brief §4.2), and keeping the write out of the `doctor` command makes that structural (a test asserts `swarm_install.doctor` imports no apply path); a repair needs exactly what `install` already has, its lease, its state record, its plan and its typed confirmation. The finding prints `swarm install --repair iap-setting.frontend-programmatic-clients` where frame B12a drew `swarm doctor --fix …`.
- Red first: `test_doctor.py::test_doctor_imports_no_apply_path`, `::test_unchecked_is_exit_3_never_0`, `::test_propagation_told_apart_from_missing_grant` (lesson 19, lesson 48: the Troubleshooter), `::test_pool_limits_read_live` (lesson 15), `::test_job_count_and_ttl` (lesson 34), `::test_authorised_networks_reported` (lesson 37), `::test_registered_groups_reprobed_through_api` (through swarm-api's groups-probe route: the doctor holds no Groups role), `::test_reconciler_can_list_jobs` (lesson 30), `::test_desktop_client_in_programmatic_allowlist` (lesson 18, B12a). `test_scheduled_doctor.py::test_findings_posted_to_api_only`, `::test_switch_on_in_pass2_tfvars`. `test_repair.py::test_repair_needs_typed_confirmation_ignoring_assume_yes`, `::test_repair_runs_the_step_that_satisfies_the_check`.
- Flags: none. Estimate: **$30**.
- CI green proves: B12a and B12b on fakes.

**DAYONE-UNINSTALL-1** — `swarm uninstall` removes what the install made, in reverse, and records what it cannot.
- Territory: `apps/swarm-install/swarm_install/uninstall.py` (new), `apps/swarm-install/swarm_install/remains.py` (new), `apps/swarm-install/swarm_install/verbs/uninstall.py` (new), `tests/unit/install/test_uninstall.py` (new).
- depends_on: DAYONE-ENGINE-3. Manifest: every entry (reverse walk); `other.install-state` (the "what remains" record, design §10).
- Red first: `test_uninstall.py::test_typed_confirmation_ignores_assume_yes`, `::test_order_is_section_9_2`, `::test_unlabelled_resource_never_deleted`, `::test_deny_listed_name_refused`, `::test_remains_record_lists_the_eight` (design §10: the brand, the KMS ring, custom role ids, the Firestore id, the Desktop client, the GitHub App, the DNS record, Workspace groups and the admin role or delegation), `::test_resume_after_crash_mid_uninstall` (a second run continues at the next unremoved step), `::test_identity_of_each_step` (steps 1-5 as the deployer, 6-8 as the owner; the impersonation grant removed before bootstrap destroy), `::test_resume_after_deployer_deleted_runs_as_owner` (a crash inside step 7 resumes as the owner, with no impersonation attempt; review-fix finding 5).
- Flags: none. Estimate: **$30**.
- CI green proves: B14 on fakes; the real teardown is P4's end.

**DAYONE-UPGRADE-1** — `swarm upgrade --to V`: plan, IAM gate, ordered apply, doctor, rollback; numbered Firestore migrations.
- Territory: `apps/swarm-install/swarm_install/upgrade.py` (new), `apps/swarm-install/swarm_install/verbs/upgrade.py` (new), `apps/swarm-install/swarm_install/migrations/__init__.py` (new), `apps/swarm-install/swarm_install/migrations/m0001_workspace_record.py` (new), `apps/swarm-install/swarm_install/migrations/m0002_index_interval.py` (new), `apps/swarm-install/swarm_install/migrations/m0003_attempt_ends.py` (new), `tests/unit/install/test_upgrade.py` (new), `tests/unit/install/test_migrations.py` (new).
- depends_on: DAYONE-ENGINE-3. Manifest: `other.firestore-migrations`, `other.verify-digests`.
- Red first: `test_upgrade.py::test_downgrade_refused_exit_5`, `::test_iam_change_waits_for_typed_project_id` (lesson 26), `::test_apply_order_is_section_9_3` (lesson 33), `::test_app_permission_added_prints_review_url` (lesson 44), `::test_namespaces_applied_for_every_tenant` (lesson 31), `::test_metric_label_change_split_live_first` (lesson 14), `::test_rollback_reapplies_previous_digests`, `::test_resume_at_failed_step`. `test_migrations.py::test_each_migration_idempotent`, `::test_schema_version_advanced_once`.
- Flags: none. Estimate: **$35**.
- CI green proves: B13 on fakes.

**DAYONE-ADDON-1** — `swarm addon enable|disable <name>` for the five add-ons, each with its preflight, secret link, switch and check.
- Territory: `apps/swarm-install/swarm_install/addons.py` (new), `apps/swarm-install/swarm_install/verbs/addon.py` (new), `scripts/acceptance/config.sh`, `install/manifest.d/addon-1.yaml` (new), `tests/unit/install/test_addons.py` (new); workflow patch for the CI add-on's runner label (`$SWARM_ARTIFACTS_DIR/workflow-lines.patch`).
- depends_on: DAYONE-ENGINE-3. Manifest: every `addon:*` entry (71), `addon.*`.
- Red first: `test_addons.py::test_core_passes_with_every_addon_off`, `::test_disable_keeps_secret_containers`, `::test_secret_only_through_link_or_hidden_prompt`, `::test_ci_addon_makes_cloud_build_sa_not_default_sa` (lesson 6), `::test_ci_addon_turns_on_acceptance_fixture`, `::test_ci_addon_app_token_inputs_declared_by_action` (lesson 45; review-fix finding 4).
- Flags: **held: workflow file** (the runner-label patch). Estimate: **$25**.
- CI green proves: add-ons are opt-in and core never depends on one.

**DAYONE-SETUP-UI-2** — The Admin › Setup page itself: the strip, the lead, the checklist and the second-team flow, reading the setup API. `UI (rendered review)`.
- Territory (3 UI files): `apps/swarm-ui/src/AdminSetup.tsx` (new), `apps/swarm-ui/src/AdminSetupTeam.tsx` (new), `apps/swarm-ui/src/styles/admin-setup.css` (new); test `apps/swarm-ui/src/__tests__/AdminSetup.test.tsx` (new).
- depends_on: DAYONE-SETUP-UI-1, DAYONE-SETUP-API-1. Manifest: as SETUP-UI-1.
- Red first: `AdminSetup.test.tsx` "first run shows the CLI's stages done and the console's steps pending", "when no CLI holds the lease the page says run swarm install to continue" (R8), "the second team flow prints the swarm team add line beside it" (B15).
- Runner: claude-code, then claude-code-browser review. Estimate: **$20**.
- CI green proves: the page composes the panels against the real API client.

### 1.6 Wave 5: the route and the generated guide (2 lanes)

**DAYONE-SETUP-UI-3** — Admin › Setup and Health get their route and their nav entry, for platform admins only. `UI (rendered review)`.
- Territory (3 UI files): `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/paths.ts`, `apps/swarm-ui/src/Spine.tsx`; test `apps/swarm-ui/src/__tests__/adminSetupRoute.test.tsx` (new).
- depends_on: DAYONE-SETUP-UI-2. Red first: "/admin/setup renders Admin › Setup for a platform admin and not for a workspace admin", "/admin/setup/health renders the Health panel", "/admin/setup/github-app/callback renders GitHubAppCallback"; the existing route-table guards stay green.
- Runner: claude-code, then claude-code-browser review. Estimate: **$10**.

**DAYONE-DOCS-1** — The public install page is generated from the manifest, and every runbook step it replaces is marked superseded.
- Territory: `apps/swarm-install/swarm_install/guide.py` (new), `docs/install/README.md` (new, generated), `docs/runbooks/iap-desktop-client.md`, `docs/runbooks/github-app.md`, `docs/plugin-setup.md`, `tests/unit/install/test_guide_generated.py` (new).
- depends_on: every wave 0-4 lane (the manifest's `step:` fields final). Red first: `test_guide_generated.py::test_guide_matches_manifest` (regenerating gives the committed file), `::test_every_human_step_in_guide`.
- Estimate: **$12**.

### 1.7 Every file once, with its lane, by wave

A file appears in at most one lane per wave. Files in a later wave that an earlier wave also touched are marked ↺ (allowed: the waves are sequential).

| Wave | Lane | Files |
|---|---|---|
| 0 | MANIFEST-1 | `install/manifest.yaml`, `apps/swarm-install/pyproject.toml`, `apps/swarm-install/swarm_install/__init__.py`, `apps/swarm-install/swarm_install/manifest.py`, `tests/unit/install/__init__.py`, `tests/unit/install/test_manifest_complete.py`, `tests/unit/install/test_manifest_counts.py`, `pyproject.toml`, `uv.lock` |
| 0 | DESAGA-1 | the 15 Dockerfiles listed in 1.1, `scripts/lib/build-mirror.sh`, `scripts/build-images.sh`, `tests/unit/scripts/test_build_mirror.py` |
| 0 | DESAGA-2 | `scripts/lib/common.sh`, `scripts/prerequisites.sh`, `scripts/lib/dev.sh`, `scripts/register-tenant.sh`, `.env.example`, `tests/unit/scripts/test_common_defaults.py` |
| 0 | DESAGA-3 | the 16 service files listed in 1.1 (`apps/swarm-api/swarm_api/settings.py` among them), `tests/unit/scripts/test_no_live_literals.py` |
| 0 | DESAGA-4 | `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/IssueSubmit.tsx`, `apps/swarm-ui/src/styles/intake.css`, `apps/swarm-ui/src/__tests__/noLiveRepository.test.ts` |
| 0 | K8S-1 | `kubernetes/render.py`, `kubernetes/apply.sh`, `tests/unit/scripts/test_k8s_render_project.py` |
| 0 | TF-1 | `terraform/infra/{main,locals,variables,tailscale,child_tasks,versions,custom_roles_moved_to_bootstrap}.tf`, `terraform/bootstrap/{versions,variables}.tf`, `terraform/modules/monitoring/variables.tf`, `tests/unit/scripts/test_terraform_install_inputs.py` |
| 0 | BOOT-1 | `terraform/bootstrap/{state_bucket,deployer_service_accounts,workspace_deployer}.tf`, `images/workspace-apply/entry.py`, `tests/unit/scripts/test_workspace_apply_entry.py`, `tests/unit/scripts/test_bootstrap_install_safe.py` |
| 0 | RELHOME-1 | `scripts/release-home.sh`, `tests/unit/scripts/test_release_home.py` |
| 1 | DISCOVERY-1 | `apps/swarm-api/swarm_api/discovery.py`, `install/manifest.d/discovery-1.yaml`, `tests/unit/control_plane/test_discovery.py` |
| 1 | IDENTITY-1 | `apps/swarm-api/swarm_api/groups.py`, `apps/swarm-api/swarm_api/delegation.py`, `apps/swarm-api/swarm_api/settings.py` ↺, `install/manifest.d/identity-1.yaml`, `tests/unit/control_plane/test_groups_auth_mode.py`, `tests/unit/control_plane/test_groups_single_failure.py` |
| 1 | ENGINE-1 | `apps/swarm-install/swarm_install/{engine,state,lease,waits,events,exitcodes,record}.py`, `.../steps/__init__.py`, `.../verbs/__init__.py`, `tests/unit/install/test_{state_store,engine_loop,waits,exit_codes,record_writers}.py` |
| 1 | BOOT-2 | `terraform/bootstrap/{platform_service_accounts,spec_signing,verify_cluster_read,outputs}.tf`, `terraform/bootstrap/variables.tf` ↺, `terraform/infra/{removed,verify,deployer}.tf`, `terraform/infra/locals.tf` ↺, `terraform/modules/iam/{service_accounts,outputs,bindings}.tf`, `terraform/modules/scheduler/{main,jobs}.tf`, `terraform/modules/service_account_ids/{main,outputs}.tf`, `install/manifest.d/boot-2.yaml`, `tests/unit/scripts/test_sa_migration_blocks.py` |
| 1 | UIENV-1 | `apps/swarm-ui/src/{Brand.tsx,vite-env.d.ts,runtimeConfig.ts}`, `images/swarm-ui/nginx.conf`, `images/swarm-ui/Dockerfile` ↺, `apps/swarm-ui/src/__tests__/runtimeConfig.test.ts` |
| 2 | PLUGIN-1 | `apps/swarm-mcp/swarm_mcp/{discovery,config,signin,auth,client,launch,workflows}.py`, `plugin/.claude-plugin/plugin.json`, `plugin/commands/{setup,sc}.md`, `plugin/skills/sc/SKILL.md`, `plugin/workflows/run.js`, `tests/unit/mcp/test_{discovery_client,version_skew,pkce_only_probe,plugin_portable}.py` |
| 2 | TF-2 | `terraform/infra/{discovery,doctor}.tf`, `terraform/infra/{github_hooks,locals,variables,main}.tf` ↺, `terraform/infra/acceptance_fixture.tf`, `terraform/environments/dev/dev.tfvars`, `terraform/environments/prod/prod.tfvars`, `terraform/modules/frontend/{main,variables,outputs}.tf`, `terraform/modules/cloud_run/{main,variables}.tf`, `terraform/modules/secret_manager/main.tf`, `terraform/bootstrap/{github_app_setup,doctor_roles}.tf`, `install/manifest.d/tf-2.yaml`, `tests/unit/scripts/test_terraform_day_one_edge.py` |
| 2 | ENGINE-2 | `.../steps/{preflight,foundations,edge}.py`, `.../secret_entry.py`, `tests/unit/install/test_{preflight,foundations,edge,secret_entry}.py` |
| 2 | ENGINE-4 | `.../steps/{identity,cluster,verify}.py`, `.../api_client.py`, `tests/unit/install/test_{identity_step,cluster_step,verify_step,api_client}.py` |
| 2 | CLI-1 | `apps/swarm-mcp/swarm_mcp/cli.py`, `.../swarm_install/cli.py`, `.../verbs/install.py`, `tests/unit/mcp/test_swarm_install_verbs.py`, `tests/unit/install/test_cli.py` |
| 2 | SETUP-API-1 | `apps/swarm-api/swarm_api/routes/setup.py`, `apps/swarm-api/swarm_api/{install_record,setup_github,setup_links,main}.py`, `apps/swarm-api/swarm_api/settings.py` ↺, `install/manifest.d/setup-api-1.yaml`, `tests/unit/control_plane/test_{setup_routes,setup_github_exchange,setup_links,install_record_writers}.py` |
| 3 | RELEASE-1 | `scripts/release/{assemble.sh,publish.sh,marketplace.py}`, `install/install.sh`, `install/compat.json`, `tests/unit/scripts/test_{release_assemble,release_publish,marketplace}.py`, workflow patch (`release-public.yml`) |
| 3 | ENGINE-3 | `.../swarm_install/{terraform,tfvars,adopt,tools}.py`, `.../swarm_install/release_key.pub`, `.../steps/{bootstrap,images,infra,secrets,github_switches,alerts,handoff}.py`, `tests/unit/install/test_{tfvars,terraform_runner,images_step,switches,adopt,alerts_step,handoff,tools}.py` |
| 3 | TEAM-1 | `.../verbs/team.py`, `.../swarm_install/team.py`, `scripts/workspace-apply.sh`, `scripts/register-tenant.sh` ↺, `tests/unit/install/test_team_add.py`, `tests/unit/scripts/test_workspace_apply_namespace.py` |
| 3 | SETUP-UI-1 | `apps/swarm-ui/src/{AdminSetupSteps,AdminSetupHealth}.tsx`, `apps/swarm-ui/src/api.ts` ↺, their two tests |
| 3 | SC-INSTALL-1 | `plugin/commands/{install,doctor,team}.md`, `tests/unit/mcp/test_plugin_install_commands.py` |
| 4 | DOCTOR-1 | `.../swarm_install/{doctor,scheduled_doctor,repair}.py`, `.../swarm_install/tfvars.py` ↺, `.../verbs/doctor.py`, `.../verbs/install.py` ↺, `apps/swarm-mcp/swarm_mcp/cli.py` ↺, `images/swarm-verify/Dockerfile` ↺, `install/manifest.d/doctor-1.yaml`, `tests/unit/install/test_{doctor,scheduled_doctor,repair}.py` |
| 4 | UNINSTALL-1 | `.../swarm_install/{uninstall,remains}.py`, `.../verbs/uninstall.py`, `tests/unit/install/test_uninstall.py` |
| 4 | UPGRADE-1 | `.../swarm_install/upgrade.py`, `.../verbs/upgrade.py`, `.../migrations/{__init__,m0001_workspace_record,m0002_index_interval,m0003_attempt_ends}.py`, `tests/unit/install/test_{upgrade,migrations}.py` |
| 4 | ADDON-1 | `.../swarm_install/addons.py`, `.../verbs/addon.py`, `scripts/acceptance/config.sh`, `install/manifest.d/addon-1.yaml`, `tests/unit/install/test_addons.py`, workflow patch (runner label) |
| 4 | SETUP-UI-2 | `apps/swarm-ui/src/{AdminSetup,AdminSetupTeam}.tsx`, `apps/swarm-ui/src/styles/admin-setup.css`, `apps/swarm-ui/src/__tests__/AdminSetup.test.tsx` |
| 5 | SETUP-UI-3 | `apps/swarm-ui/src/{App.tsx,paths.ts,Spine.tsx}`, `apps/swarm-ui/src/__tests__/adminSetupRoute.test.tsx` |
| 5 | DOCS-1 | `apps/swarm-install/swarm_install/guide.py`, `docs/install/README.md`, `docs/runbooks/{iap-desktop-client,github-app}.md`, `docs/plugin-setup.md`, `tests/unit/install/test_guide_generated.py` |

`.../` is `apps/swarm-install/swarm_install/`. Brace groups expand to the full paths written out in each lane's territory above; the territory lines are authoritative.

**UI files per UI lane:** DESAGA-4 3, UIENV-1 3, SETUP-UI-1 3, SETUP-UI-2 3, SETUP-UI-3 3. None over 3.

**Within-wave check after the review:** `settings.py` is DESAGA-3's in wave 0 and moves with IDENTITY-1 to wave 1, then SETUP-API-1 in wave 2; `api.ts` is DESAGA-4's (0) then SETUP-UI-1's (3); `register-tenant.sh` DESAGA-2's (0) then TEAM-1's (3); `terraform/infra/locals.tf` TF-1 (0), BOOT-2 (1), TF-2 (2); `terraform/infra/main.tf` TF-1 (0), TF-2 (2); `tfvars.py` ENGINE-3 (3), DOCTOR-1 (4). After review-fix finding 3, DISCOVERY-1 is in wave 1, PLUGIN-1 in wave 2 and RELEASE-1 in wave 3; none of their files is in another lane of its new wave. Every manifest fragment has one lane, and no wave-0 lane but MANIFEST-1 writes the manifest. No file is in two lanes of one wave.

**Held PRs (9 lanes, 12 flags):** TF-1 (Terraform), BOOT-1 (Terraform, IAM), RELHOME-1 (IAM), DESAGA-2 (IAM), BOOT-2 (Terraform, IAM), TF-2 (Terraform, IAM), RELEASE-1 (workflow file), TEAM-1 (IAM), ADDON-1 (workflow file). **Contract requests:** none needed to build (section 4).

### 1.8 Manifest additions and corrections made by this plan

Added to `docs/design/day-one/manifest.draft.yaml` (each with evidence; the header counts are updated: 433 + 27 = **460**). The first 16 were found while planning; the next 10 by the adversarial review (section 13); the last one by the review of the fixed plan (section 13.1).

| id | kind | stage | Why it was missing |
|---|---|---|---|
| `declared-value.monitoring-excluded-tenants` | declared-value | 7 | `terraform/modules/monitoring/variables.tf:115` defaults to the live verify tenant (R9). |
| `cloud-run-service.swarm-discovery` | cloud-run-service | 7 | Design §9.2 (c) creates a service; only the endpoint was listed. |
| `sa.swarm-discovery` | service-account | 3 | Its runtime identity (BOOT-2). |
| `sa.swarm-doctor` | service-account | 3 | D27's scheduled doctor needs an identity. |
| `cloud-run-job.swarm-doctor` | cloud-run-job | 7 | D27. |
| `sched.doctor-hourly` | scheduler-job | 7 | D27. |
| `iam-binding.doctor-read-only` | iam-binding | 7 | D27: read-only roles, plus invoking swarm-api to post findings. |
| `monitoring.doctor-findings-alert` | monitoring | 13 | P1-B: the scheduled doctor emails the alert channel. |
| `iam-binding.swarm-api-github-app-secret-writer` | iam-binding | 7 | D26: swarm-api performs the manifest code exchange (R3). |
| `workspace-delegation.groups-reader-admin-role` | workspace-delegation | 8 | Q2's chosen path; the draft has only delegation. |
| `other.install-record-firestore` | other | 5 | D26's Firestore record, distinct from the GCS state object. |
| `other.verify-row` | other | 14 | D26: the console shows verify's progress. |
| `other.doctor-findings` | other | 15 | D27: Admin › Setup › Health. |
| `other.release-home` | other | 0 | D25/Q4: the release home and its public read. |
| `other.plugin-marketplace` | other | 15 | D28: `gs://swarmcloud/marketplace.json` and the plugin bundle. |
| `other.plugin-version-skew` | other | 15 | D28: the supported range and its message. |
| `switch.enable_console_app_setup` | switch | 7 | Review finding 2: the App's secret containers and swarm-api's writer grant must exist before the console's App step. |
| `switch.enable_scheduled_doctor` | switch | 7 | Review finding 12: the hourly doctor must not run before its code ships. |
| `switch.enable_acceptance_fixture` | switch (`addon:ci`) | 7 | Review finding 4: `acceptance_fixture.tf`'s unconditional `allUsers` bucket. |
| `declared-value.groups_auth` | declared-value | 7 | Review finding 8: installs run swarm-api in the admin-role group mode. |
| `switch.tenant_job_vpc_egress` | switch | 7 | Review finding 19: lesson 35's guard (`terraform/infra/tenant_job_vpc.tf:31`, default false) was unlisted. |
| `other.release-kubernetes-bundle` | other | 10 | Review finding 3: stage 10, team add, upgrade and uninstall need the Kubernetes trees with no checkout. |
| `other.release-signing-public-key` | other | 4 | Review finding 3: the key the engine verifies with. |
| `other.engine-terraform-rules` | other | 3 | Review finding 19: lessons 7, 10, 11 had no manifest entry. |
| `other.upgrade-iam-gate` | other | 7 | Review finding 19: lesson 26. |
| `other.sc-install-refusal-rule` | other | 0 | Review finding 19: lesson 47. |
| `iam-binding.deployer-iap-api-accessor` | iam-binding | 7 | Review-fix finding 2: the engine calls swarm-api behind IAP as the deployer (section 5.3). |

**Corrected:** `iam-binding.github-hooks-allusers` keeps its id (the completeness test matches the Terraform address until DAYONE-TF-2 removes it) and gains a `notes` line: Q3 replaces the `allUsers` binding with `invoker_iam_disabled = true`; after TF-2 its check is "no `allUsers` member and invoker IAM disabled".

Considered and **not** added: a separate entry for the team git slot's link (it is `secret.swarm-tenant-git-u.value`'s `secret_entry`) and for the uninstall's "what remains" record (design §10 keeps it in `other.install-state`).

---

## 2. Coverage

### 2.1 The brief's section 11 work list

Every item and sub-item, and the lane(s) that build it.

| Brief §11 item | Lane(s) |
|---|---|
| 1. De-Saga: Dockerfile mirror references, `test_build_mirror.py` | DESAGA-1 |
| 1. `ALLOWED_USERS` literal to a variable | TF-1 (Terraform), DESAGA-3 (swarm-api's reader) |
| 1. the `moved` block | TF-1 (deleted only once BGD's live plan shows it applied) |
| 1. `PROJECT_ID` and `ALLOWED_DOMAINS` defaults (env overrides for the frozen config) | DESAGA-2 (`PROJECT_ID`), ENGINE-3 (renders `ALLOWED_DOMAINS` from the stage 0 domain into every service), section 4 (the frozen defaults) |
| 1. `SHARED_DENY_LIST` per deployment | DESAGA-2 (R4), DESAGA-3 (issueruns) |
| 1. the Tailscale target to the add-on | TF-1 (variable, no default), ADDON-1 (asks for it) |
| 1. `frontend_iap_backends` | TF-1 (empty in pass 1), ENGINE-3 (derived from infra state for bootstrap pass 2) |
| 1. workspace-apply `SIGNING_KEYS` per project | BOOT-1 (R1) |
| 1. `var.tenants` unfrozen for installs | BOOT-1 (the precondition), ENGINE-3 (`tenants = {}`) |
| 1. the verify tenant id derived per project | TF-1 (R9), ENGINE-3 (computes it through `swarm_common.identity`) |
| 1. runner label and sandbox for the CI add-on | ADDON-1 (workflow patch, held) |
| 1. plugin bridge source to the release bundle | PLUGIN-1 |
| 2. a per-deployment bootstrap config generated by the engine | ENGINE-3 (`tfvars.py`) |
| 2. no grants to accounts that do not exist yet | BOOT-2 |
| 2. the state bucket created once (with an import) | ENGINE-2 (creates it), BOOT-1 (the `import`) |
| 2. Cloud Build's API and its own SA, if the release pipeline needs it | Not needed by core: the release builds in SwarmCloud's own CI (section 7), and core copies images (D21). ADDON-1 builds it for `addon:ci` (lesson 6). |
| 2. `deployer_storage_buckets_exact` derived from the project | TF-1 |
| 2. the tenants freeze lifted for installs | BOOT-1 |
| 2. project-derived service tenants computed per project | ENGINE-3 (imports the frozen rule, never restates it) |
| 2. adopt-or-rename for permanent names on reinstall | ENGINE-3 (`adopt.py`), UNINSTALL-1 (the record it adopts from) |
| 3. remove the declared-output cycle, or have the engine do the passes | TF-1 (the cycle goes), ENGINE-3 (the four applies) |
| 3. key-gated switches driven by the engine | ENGINE-3 (`test_switches.py`) |
| 3. bootstrap and infra composed by the engine | ENGINE-3 |
| 4. Engine and manifest: state store, step protocol, dry run, resume, countdown waits, exit codes, `explain()`, the generated guide | MANIFEST-1, ENGINE-1, CLI-1 (dry run, `--json-events`), DOCS-1 (guide) |
| 5. Identity: delegation guided and checked; a group-probe check; a safe failure instead of 503-everyone | IDENTITY-1, ENGINE-4 (stage 8), TF-2 (`GROUPS_AUTH`) |
| 6. GitHub App manifest flow and installation check | SETUP-API-1 (R3), TF-2 (the secret-writer grant), ENGINE-3 (the switches, R8), SETUP-UI-1 (the panel) |
| 7. Discovery endpoint; a plugin that needs one value; `/sc:setup` hints; `/sc` without a checkout | DISCOVERY-1, TF-2, PLUGIN-1 (incl. `plugin/skills/sc/SKILL.md` and `plugin/commands/sc.md`), RELEASE-1 (the marketplace) |
| 8. Team add: namespaces by the engine and by releases (#1590); quota from the tenant record (#2039); verify-keys; first admin; team `-git` slot through a link | TEAM-1, K8S-1, UPGRADE-1 (namespaces on every release), SETUP-API-1 (the team route), SETUP-UI-2 (second team) |
| 9. Cluster policies in the engine | ENGINE-4 (`steps/cluster.py`), K8S-1, RELEASE-1 (`kubernetes.tgz` in the bundle) |
| 10. Alerts: a channel required (#1817) | ENGINE-2 (asks), ENGINE-3 (renders, stage 13 check) |
| 11. Safer defaults: no default-SA Editor; no `allUsers` under DRS; `workspace_gate` and `repository_grants_enforced` on | ENGINE-2 (#1020), TF-2 (Q3, and the acceptance fixture's `allUsers` bucket gated to `addon:ci`), ENGINE-3 (the two switches) |
| 12. Doctor, uninstall, upgrade, `team add`, add-ons | DOCTOR-1, UNINSTALL-1, UPGRADE-1, TEAM-1, ADDON-1 |
| 13. The release pipeline and release home; prod-shaped images; signing; the install-and-upgrade test | RELHOME-1, RELEASE-1, UIENV-1, DESAGA-1; the test is the manual step of section 7.3 |
| 14. Console Admin › Setup and `/sc:install` | SETUP-API-1, SETUP-UI-1, SETUP-UI-2, SETUP-UI-3, SC-INSTALL-1 |
| 15. Docs: the install page generated from the manifest; superseded runbook steps | DOCS-1 |

### 2.2 The design's sections 2 to 12

| Design section | Lane(s) |
|---|---|
| 2.1 Language and home (`apps/swarm-install`, `swarm` verbs, doctor's platform tier, exit codes) | MANIFEST-1, ENGINE-1, CLI-1, DOCTOR-1 |
| 2.2 The step protocol (dependency and step, check/plan/apply/verify/explain, human steps, waits, typed probes) | ENGINE-1; probes per stage in ENGINE-2, ENGINE-3, ENGINE-4 |
| 2.3 The state store and resume (GCS, lease, local cache, Firestore mirror) | ENGINE-1, SETUP-API-1 (the console's documents, section 5.4) |
| 2.4 Dry run | CLI-1, ENGINE-3 (Terraform plan when the backend exists) |
| 2.5 Secrets through the engine | ENGINE-2 (`secret_entry.py`), ENGINE-3 (generated keys), SETUP-API-1 (App secrets, links), SC-INSTALL-1 |
| 3.1 Schema (`probe:`, `step:`, `refs:`, `expand:`) | MANIFEST-1 |
| 3.2 What the draft holds (the counts) | MANIFEST-1 (`test_manifest_counts.py`) |
| 3.3 The completeness test | MANIFEST-1 |
| 3.4 Templates (`expand:`) | MANIFEST-1 (field), ENGINE-3 and TEAM-1 (expansion per tenant) |
| 4 The stage graph | ENGINE-2 (0-2), ENGINE-3 (3-7, 9's switches, 13, the hand-off and 15), ENGINE-4 (8, 10, 14), SETUP-API-1 and TEAM-1 (9, 11, 12 under D26) |
| 4.1 History lessons as checks | section 3 of this plan |
| 5.1 Generated inputs | ENGINE-3, RELEASE-1 (no tfvars in `module.tgz`) |
| 5.2 The three declared values | TF-1, ENGINE-3 (`workspace_apply_image`) |
| 5.3 Bootstrap grants to accounts that do not exist yet | BOOT-2 |
| 5.4 Key-gated switches | ENGINE-3 |
| 5.5 The state bucket, created once | ENGINE-2, BOOT-1 |
| 5.6 The tenants freeze and service tenants | BOOT-1, ENGINE-3 |
| 5.7 Tenant secret containers: one writer for installs | TEAM-1 (every tenant value through swarm-api's writer behind a link) |
| 6.1 The release home | RELHOME-1 |
| 6.2 The release pipeline | RELEASE-1, UIENV-1, DESAGA-1 |
| 6.3 The company's side (`install.sh`, verify, copy) | RELEASE-1 (`install.sh`), ENGINE-3 (`steps/images.py`, `tools.py`, `release_key.pub`) |
| 7 De-hardcoding | DESAGA-1, DESAGA-2, DESAGA-3, DESAGA-4, K8S-1, TF-1, BOOT-1, PLUGIN-1 |
| 8 Identity and domain-wide delegation | IDENTITY-1, ENGINE-4, TF-2 |
| 9.1 The GitHub App Manifest flow | SETUP-API-1, TF-2, ENGINE-3, SETUP-UI-1 |
| 9.2 The discovery endpoint | DISCOVERY-1, TF-2, PLUGIN-1 |
| 9.3 The Desktop client secret | ENGINE-2 (hidden prompt into Secret Manager), PLUGIN-1 (the PKCE-only probe), DISCOVERY-1 and TF-2 (the switch) |
| 10 Uninstall and reinstall | UNINSTALL-1, ENGINE-3 (`adopt.py`: all eight surviving names) |
| 11 Upgrade | UPGRADE-1 |
| 12 Add-ons | ADDON-1 |

**No lane builds what no item asks for.** Every lane above traces to a §11 item or a design section; the D26-D28 additions (Admin › Setup, Health, the scheduled doctor, the release-home marketplace, version skew) trace to §11 items 7, 12 and 14 as the GATE 1 picks shaped them.

---

## 3. The lesson map (all 48)

Each lesson from [`day-one/lessons.md`](day-one/lessons.md), the manifest entry that carries it, and the test that fails if the guard goes. "Engine rule" means a rule in the named module with its own test.

| # | Lesson | Manifest entry | Guard (lane: test) |
|---|---|---|---|
| 1 | Project create without billing link | `switch.billing-link-right`, `switch.project-create-right` | ENGINE-2: `test_preflight.py::test_missing_billing_link_right_stops_with_fix` |
| 2 | Firestore location is permanent | `firestore.main`, `org-policy.resource-locations` | ENGINE-2: `::test_region_confirmed_before_stage_1` |
| 3 | IAM binding write rate limit | `iam-binding.default-compute-sa-editor-removed` (the first engine IAM write) | ENGINE-2: `test_foundations.py::test_iam_write_rate_limit_retried_not_reported_as_denial` |
| 4 | Three API lists; KMS quota | `api.*`, `quota.kms-signing-h4` | MANIFEST-1: collector 2 in `test_manifest_complete.py`; ENGINE-2: `::test_one_api_list_from_manifest`, `::test_kms_signing_quota_read` |
| 5 | Defaults name the live project | `other.shared-project-literal-in-both-envs`, `declared-value.project_id` | DESAGA-2: `test_common_defaults.py`; DESAGA-3: `test_no_live_literals.py`; TF-1: `::test_no_live_default`; ENGINE-3: `::test_generated_never_from_committed_tfvars` |
| 6 | Default compute SA holds Editor | `iam-binding.default-compute-sa-editor-removed` | ENGINE-2: `::test_default_compute_editor_removed`; ADDON-1: `::test_ci_addon_makes_cloud_build_sa_not_default_sa` |
| 7 | Bootstrap state on one laptop | `bucket.state`, `other.install-state`, `other.bootstrap-sh-empty-state-guard` | ENGINE-2: `::test_state_bucket_created_with_settings_then_state_uploaded`; BOOT-1: `::test_state_bucket_imported`; ENGINE-3: `test_terraform_runner.py::test_empty_state_refused_when_deployer_exists` (`+other.engine-terraform-rules`) |
| 8 | Infra applied before bootstrap | `role.custom.*`, `switch.grant_broker_secret_lister` | ENGINE-1: `test_engine_loop.py::test_stage_refuses_while_earlier_entry_unmet`; BOOT-2 removes the "later" booleans |
| 9 | First infra plan has no digests | `declared-value.image_refs`, `other.image-digest-refusal` | ENGINE-3: `::test_freshness_from_state_not_registry` |
| 10 | Destroy-first IAM swap; condition limits | `+other.engine-terraform-rules`; `iam-binding.*` | ENGINE-3: `::test_iam_replace_without_create_before_destroy_refused`, `::test_conditions_linted_before_apply` |
| 11 | A `-var` never reached tfvars | `+other.engine-terraform-rules` (design §5.1) | ENGINE-3: `::test_no_dash_var_ever`, `::test_plan_with_destroy_refused_without_typed_approval` |
| 12 | Wrong tool versions; init | `local-tool.terraform`, `local-tool.kubectl` | ENGINE-2: `::test_tool_versions_pinned`; ENGINE-3: `::test_terraform_version_checked`, `::test_init_before_plan`; TF-1: `::test_required_version_pinned` |
| 13 | A plan would delete the receiver (no digest) | `other.image-digest-refusal`, `image.swarm-github-hooks` | ENGINE-3: `test_images_step.py::test_copy_by_digest_only` and `images.json` must hold a digest for every image (`test_release_assemble.py::test_images_json_digests_only`) |
| 14 | A metric label forces replacement | `monitoring.log-metrics` | UPGRADE-1: `::test_metric_label_change_split_live_first` |
| 15 | Pool ceilings are birth values | `declared-value.*` pool ceilings (doctor reads live) | DOCTOR-1: `::test_pool_limits_read_live` |
| 16 | IAP admin scoped by name matched nothing | `iap-setting.backend-api`, `iap-setting.backend-ui` | ENGINE-2: `::test_inherited_bindings_checked_before_saying_missing` |
| 17 | Brand, IAP agent, DNS by hand | `oauth-brand.iap`, `other.iap-service-agent`, `dns.frontend-hostname`, `certificate.managed-ssl` | ENGINE-2: `test_edge.py` (four tests) |
| 18 | gcloud tokens 401 code 900 at IAP | `iap-setting.frontend-programmatic-clients`, `oauth-client.desktop-sc` | ENGINE-4: `test_verify_step.py::test_plugin_run_signs_in_with_desktop_client_not_gcloud_token`, `test_api_client.py::test_never_sends_a_gcloud_user_token` (the engine's own calls, section 5.3); DOCTOR-1: `test_doctor.py::test_desktop_client_in_programmatic_allowlist` (the check B12a shows) |
| 19 | IAM propagation takes time | stage 7 wait | ENGINE-1: `::test_wait_gives_up_at_bound_with_exit_4`; DOCTOR-1: `::test_propagation_told_apart_from_missing_grant` |
| 20 | Group lookups failed | `workspace-delegation.groups-dwd`, `+workspace-delegation.groups-reader-admin-role` | IDENTITY-1: both tests; ENGINE-4: `::test_unauthorized_client_names_admin_console_not_iam`, `::test_group_registered_only_after_probe_passes` |
| 21 | Auth and routing traps; verify outside VPC | `cloud-run-job.verify` | ENGINE-4: verify runs as the in-VPC `swarm-verify` job (`test_verify_step.py::test_four_end_to_end_runs_each_to_a_pr` asserts it is started as that job, not from the laptop) |
| 22 | The release pipeline never completed; laptop deploy | `iam-binding.installer-impersonates-deployer` | ENGINE-3: `::test_every_apply_after_pass1_impersonates_deployer` |
| 23 | No `actAs`; alerts before metrics | `iam-binding.deployer-acts-as`, `monitoring.alert-policies` | ENGINE-3: `test_terraform_runner.py::test_deployer_act_as_checked_before_apply`, `::test_every_apply_after_pass1_impersonates_deployer`; `test_alerts_step.py::test_alert_policies_after_metric_descriptor` |
| 24 | A trust-policy pin blocked the deploy | `other.wif-github-pool-and-provider` (`addon:ci`) | ADDON-1: the CI add-on's preflight runs `scripts/verify-deployer-trust.sh` against the printed claims; **no core check: core has no CI identity (D10)** |
| 25 | A prod tag moved before approval | `addon:ci` entries | **No core check: environments and promotion are the CI add-on's (D10).** ADDON-1's preflight checks the environments exist and are protected. |
| 26 | A held IAM plan blocked every release | `+other.upgrade-iam-gate` | UPGRADE-1: `::test_iam_change_waits_for_typed_project_id` (the wait names what it blocks) |
| 27 | Runner and CVE stalls | `other.release-bundle` | RELEASE-1: `::test_cve_acceptances_have_expiry`; ENGINE-2: `test_preflight.py::test_release_cve_acceptances_shown_with_expiry` |
| 28 | Deployed-version traps (labels lie, pinned traffic) | `other.verify-digests` | ENGINE-4: `::test_digests_read_never_labels`; ENGINE-3: `test_tfvars.py::test_traffic_latest_100` |
| 29 | Browser agents all failed (RBAC by email) | `rbac.*` | K8S-1: `::test_subjects_carry_unique_id`; ENGINE-4: `::test_subjects_from_unique_id_read_from_iam` |
| 30 | Reconciler listed Jobs at cluster scope | `rbac.dispatcher-role` | ENGINE-4: `test_verify_step.py::test_reconciler_can_list_jobs` (at install); DOCTOR-1: `test_doctor.py::test_reconciler_can_list_jobs` (the lesson's rule: doctor checks it on the live deployment) |
| 31 | New tenant without namespace / verify-keys | `namespace-object.tenant-namespace`, `namespace-object.verify-keys-configmap` | TEAM-1: `::test_verify_keys_configmap_present`; UPGRADE-1: `::test_namespaces_applied_for_every_tenant` |
| 32 | Wrong quota on a hand-applied namespace | `namespace-object.tenant-resourcequota` | K8S-1: `::test_quota_from_tenant_record`; TEAM-1: `::test_namespace_quota_from_tenant_record` |
| 33 | Admission policy refused Jobs | `cluster-policy.*` | ENGINE-4: `::test_policies_before_tenant_objects`; UPGRADE-1: `::test_apply_order_is_section_9_3` |
| 34 | Job-count quota filled | `quota.cloud-run-jobs` | DOCTOR-1: `::test_job_count_and_ttl` |
| 35 | Scheduler-created Jobs had no VPC | `cloud-run-job.tenant-profile-matrix`, `iam-binding.scheduler-tenant-job-subnet` | ENGINE-3: `test_tfvars.py::test_tenant_job_vpc_egress_true` (`+switch.tenant_job_vpc_egress`, default false in `terraform/infra/tenant_job_vpc.tf:31-43`, now set in pass 2); ENGINE-4: verify's console agent runs through a Cloud Run job and must reach the API; TF-2 gives the doctor job VPC egress |
| 36 | Direct VPC egress delays the first connection | `network.subnet` | **No installer check:** the workers' 180 s retry and 480 s dispatch deadline are code already in the release; ENGINE-4's verify bounds its own first call at 180 s so it does not report this as a failure. |
| 37 | Wrong kube context; open control plane | `cluster.autopilot` | ENGINE-4: `::test_context_is_the_install_cluster`; DOCTOR-1: `::test_authorised_networks_reported` |
| 38 | Quota sweep 403'd while Scheduler said success | `other.verify-scheduler-targets` | ENGINE-4: `::test_every_scheduler_target_must_answer_2xx` |
| 39 | A secret with no version blocked deploys | `switch.*` | ENGINE-3: `::test_switch_true_only_with_enabled_version` |
| 40 | New-tenant order; `--providers` replaces | `sa.tenant-worker`, `secret.swarm-tenant-provider.value` | TEAM-1: `::test_add_provider_never_replaces` |
| 41 | Alerts notified nobody | `monitoring.notification-channel` | ENGINE-2: `::test_alert_email_asked`; ENGINE-3: stage 13's check |
| 42 | Secrets are scoped to the user | `secret.swarm-tenant-*` | **No installer check:** it is a property of the secret model, enforced by swarm-api's existing writer; TEAM-1 routes every tenant value through that writer (design §5.7), so the install adds no second path. |
| 43 | GitHub quota ran out on a PAT | `github-app-setting.tenant-forge-app` | SETUP-API-1: the App is the only forge credential an install creates; ENGINE-4: `test_verify_step.py::test_runs_use_installation_tokens` |
| 44 | Agents could not push workflow files; permission changes need owner acceptance | `github-app-setting.tenant-forge-app` | UPGRADE-1: `::test_app_permission_added_prints_review_url` |
| 45 | The wrong token-permission key | `github-app-setting.merge-app` (`addon:ci`) | ADDON-1: `test_addons.py::test_ci_addon_app_token_inputs_declared_by_action` reads the CI add-on's workflow lines (`.github/workflows/runner-fallback.yml:120` today, and the add-on's own workflow patch) and fails on any `with:` key of an `actions/create-github-app-token` step that is not an input of the pinned action's `action.yml` (its input list is held in the test), so `permission-variables` fails; shown red on a fixture that carries it. **No core check: the merge App is the CI add-on's (D10)**; the tenant App's permission keys are a different list (lesson 46, SETUP-API-1) |
| 46 | App once per deployment; callback must match; Commit statuses | `github-app-setting.commit-statuses-read-gap` | SETUP-API-1: `::test_manifest_requests_statuses_read`, and the callback is derived from the hostname in the same test |
| 47 | Auto-mode refused IAM and deletion commands | `+other.sc-install-refusal-rule` | SC-INSTALL-1: `::test_refusal_hands_exact_command` |
| 48 | IAM conditions testable only by releasing | `api.policytroubleshooter` | ENGINE-2: the API is in the manifest's list (`::test_one_api_list_from_manifest`); DOCTOR-1: `::test_propagation_told_apart_from_missing_grant` |

**No core installer check (5 of 48), each with its reason above:** 24, 25 and 45 belong to the CI add-on (D10) and are checked by ADDON-1 for a company that enables it; 36 and 42 are properties of code the release already carries, and the installer only bounds its own calls around them.

---

## 4. Contract requests (design §14 Q6)

### 4.1 Every frozen literal appendix A lists

Read at `b3fe3e6` with `grep -n 'saga\|bogdan' apps/common/swarm_common/*.py`. Seven lines.

| File:line | Text | Runtime? | Overridable by environment? |
|---|---|---|---|
| `config.py:46` | `allowed_domains: tuple[str, ...] = ("saga.xyz",)` | yes: the dataclass default | Yes. `Settings.from_env` builds the field from `ALLOWED_DOMAINS` (`config.py:110`); swarm-api reads `settings.core.allowed_domains` (`apps/swarm-api/swarm_api/admins.py:483`, `auth.py:1171`). No service constructs `Settings()` without `from_env` (`grep -rn 'Settings()' apps` finds only the egress module's own class). |
| `config.py:110` | `os.environ.get("ALLOWED_DOMAINS", "saga.xyz")` | yes: the fallback when the variable is unset | Yes, by setting `ALLOWED_DOMAINS`. **But the fallback fails open to another company's domain** when a service is deployed without the variable. |
| `identity.py:8` | module docstring example `eng@saga.xyz` | no | n/a (documentation) |
| `identity.py:68` | comment `swarm-ci-fix@saga-agents-staging…` | no | n/a |
| `identity.py:70` | comment `eng@saga.xyz` | no | n/a |
| `identity.py:169` | docstring `` `eng@saga.xyz` -> `eng` `` | no | n/a |
| `models.py:164` | docstring: measured on the live project | no | n/a |
| `models.py:873` | comment `eng@saga.xyz \| alice@saga.xyz` | no | n/a; already the subject of open request **122** |

**Result:** by Q6's test, **no frozen literal is impossible to override by environment**, so no contract request is required to build Day One. The install guards the one runtime risk without a contract change: ENGINE-3 renders `ALLOWED_DOMAINS` into every service, and DOCTOR-1 checks each service's environment carries it (a missing value is a finding, never "fine").

Two requests are drafted anyway, because the fallback fails open and the comments name the company in a product companies receive (D1). **Filing them is BGD's call** (open question 3); neither blocks a lane.

### 4.2 Draft request A: `config.py`: `ALLOWED_DOMAINS` has no live default

```markdown
## 127. `config.py`: `ALLOWED_DOMAINS` has no default domain (DAYONE-P2, part of #2688)

**Status:** open, REQUESTED 2026-10-29 by lane DAYONE-P2 (the Day One plan,
`docs/design/day-one-plan.md` §4). Not applied: `apps/common/swarm_common/`
is frozen.

### What is true today

`apps/common/swarm_common/config.py:46` defaults `Settings.allowed_domains`
to `("saga.xyz",)`, and `Settings.from_env` (line 110) falls back to
`"saga.xyz"` when `ALLOWED_DOMAINS` is unset. On the live deployment that is
the right domain. On a Day One install in another company's project, a
service deployed without the variable trusts the live deployment's domain
instead of failing: the sign-in check (`apps/swarm-api/swarm_api/auth.py:1171`)
and the admin check (`admins.py:483`) would admit that domain's accounts if
IAP ever let one through.

### The change

    # config.py, class Settings
    allowed_domains: tuple[str, ...] = ()

    # config.py, Settings.from_env
    domains = tuple(
        d.strip() for d in os.environ.get("ALLOWED_DOMAINS", "").split(",") if d.strip()
    )

and swarm-api's startup refuses an empty `allowed_domains` with a message
naming the variable, so a missing value stops the revision instead of
trusting a domain.

### What it would break if accepted

Any process that relies on the fallback. The live deployment sets
`ALLOWED_DOMAINS` in Terraform for every service (`terraform/infra/locals.tf`);
the release must check that before applying, and tests that construct
`Settings()` with no domain need one passed.

### If it is declined

The Day One engine sets `ALLOWED_DOMAINS` on every service and
`swarm doctor` reports a service without it; the fallback stays as a latent
fail-open default in every company's deployment.

### Invariants

Invariant 9 (per-tenant isolation) is strengthened: no deployment trusts a
domain it was not given.
```

### 4.3 Draft request B: the frozen contract's comments use example identities

```markdown
## 128. `identity.py` / `models.py`: the live project and domain in comments become example values (DAYONE-P2, part of #2688)

**Status:** open, REQUESTED 2026-10-29 by lane DAYONE-P2 (the Day One plan,
`docs/design/day-one-plan.md` §4). Not applied: `apps/common/swarm_common/`
is frozen. It extends request 122 (one comment in `models.py`) to the other
five lines.

### What is true today

Comments and docstrings name the live deployment's domain and project:
`identity.py:8`, `:68`, `:70`, `:169`; `models.py:164`; and `models.py:873`
(request 122). Nothing reads them. The Day One release ships this package to
every company that installs SwarmCloud (D1, D5).

### The change

Comments only: `eng@saga.xyz` becomes `eng@example.com`;
`swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com` becomes
`swarm-ci-fix@example-project.iam.gserviceaccount.com`; the measurement in
`models.py:164` keeps its task id and date and says "the live deployment"
instead of the project id.

### What it would break if accepted

Nothing. No type, default, field or document shape moves, and
`scripts/lib/check-contract-parity.sh` reads no comment.

### If it is declined

The frozen contract keeps six lines that name the live deployment, and
DAYONE-DESAGA-3's no-live-literal test keeps `apps/common` excluded by name.

### Invariants

None is touched: comments decide nothing.
```

---

## 5. Admin › Setup and Health (D26, D27)

### 5.1 Pages and states

Two pages, both under Admin, both for **platform admins only** (the `admin_pool_users` and admin groups of design §8; a workspace admin gets the console's existing "not yours" page). Routes `/admin/setup`, `/admin/setup/health` and `/admin/setup/github-app/callback` (SETUP-UI-3; the callback page is `GitHubAppCallback` from SETUP-UI-1).

**Admin › Setup** draws the P1-B idiom: the `cfg-strip` strip, the `ws-lead` lead, the `ob-list` checklist, side cards.

| State | What it shows | Data |
|---|---|---|
| First run, CLI part done | The strip: "swarm install did the cloud part" with stages 0-8, 10, 13 ticked and their times; the lead names the next console step; the checklist's console steps pending. | engine document |
| Waiting for the CLI | If no CLI holds the install lease (its heartbeat is older than 2 minutes): "swarm install is not running: run `swarm install` in a terminal to continue", because stage 9's switches and verify need it (R8). The console steps stay usable. | engine document's `lease` |
| GitHub App, in progress | B06: "Create the GitHub App" (one button: the manifest form posts to github.com); after the redirect, "secrets stored" (never shown); then "Install on <org>" (link to `apps/<slug>/installations/new`), polled; then "self-check running", which waits on the CLI's pass 3. | console document `github_app` |
| First team | B08: group → workspace name, first admin, invites; then the progress of tenant record, namespace (quota, verify-keys), worker identity, team git slot (a one-time link for its owner); its smoke agent waits for the pool accounts (next row), because it needs one (review-fix finding 1). | console document `teams/<id>` plus the workspace record |
| Claude capacity | B07, **after the first team** (review-fix finding 1): one row per pool account, each owner a member of the first team's group (invited in the row above, role `member`): signed in, or a one-time sign-in link (single use, expires 24 h, shown once with a copy button), or `sc account add --label <name>`; the broker check after both; then the first team's smoke agent runs. Lea's row says "waiting for Lea Novak to open her link" and the link's expiry. | console document `accounts` |
| Add-ons | Each add-on with "off" and the `swarm addon enable <name>` line (enabling is CLI-only: it needs a Terraform switch). Its secret goes in through a one-time link the CLI prints (the `addon-secret` link kind) or the hidden prompt. | release manifest |
| A step failed | The step's row turns red with the error's one-line cause, the fix (from the manifest's `explain`) and "Retry". A retry re-runs that step's check first. | console or engine document |
| **Verify, run by swarm install** | A row of its own after the console steps: the current check (smoke, GKE proof, race, Scheduler targets, digests, live browser, code intelligence, four end-to-end runs), done/total, elapsed, a countdown to the expected end and the deadline, from the engine's `verify` field, refreshed every 5 s. "Send to your team" stays locked with "unlocked when verify passes". | engine document `verify` |
| Ready | B09a: every row done; "Send to your team" shows the one plugin line (the deployment URL is the only value) and the console URL; the installer card says swarm install saw this finish and its verify passed. At 390 wide (B11) the strip compacts and the plugin line is a scrollable code block. | both |
| Second team (day two) | B15: the same team panel as a standalone flow, with `swarm team add <group>` printed beside it. | console document `teams/<id>` |

**Admin › Setup › Health** (B12b): the latest scheduled doctor run (time, checks run, findings), each finding with what changed (the audit-log entry when the check knows it), the repair command `swarm install --repair <check>` and the console path where one exists, and the full check list with each check's last result. "Not checked" is shown as such, never as passing.

### 5.2 Who may see them

swarm-api answers every `/v1/admin/setup/*` route only for a platform admin (the existing admin check). The one-time links are the exception, and they are narrow: a link opens only its own form, for the person it was minted for (their email, checked against the IAP identity), once.

### 5.3 The API routes (SETUP-API-1)

| Route | Reads / writes | Notes |
|---|---|---|
| `GET /v1/admin/setup` | reads the engine document and every console document | the page's whole state |
| `POST /v1/admin/setup/github-app/manifest` | writes `github_app.state` (a random value) | returns the manifest JSON and the form target `https://github.com/organizations/<org>/settings/apps/new?state=…`; `redirect_url` = `https://<H>/admin/setup/github-app/callback`; `hook_attributes.active = false`; permissions from design §9.1 including `statuses: read` |
| `POST /v1/admin/setup/github-app/conversion` | writes three Secret Manager versions; writes `github_app.{id,slug,client_id}` | body `{code, state}`; checks `state`; calls `POST /app-manifests/<code>/conversions`; `pem`, `client_secret`, `webhook_secret` go straight to `add_secret_version` and are dropped; the response is `{id, slug}` only; nothing of the GitHub response is logged |
| `GET /v1/admin/setup/github-app/installation` | reads GitHub with an App JWT | polled by the page until the org appears; writes `github_app.installed_on`. Works from infra pass 2 on: swarm-api's read grant is under `enable_console_app_setup` (TF-2), not `enable_github_app` |
| `POST /v1/admin/setup/github-app/hook` | calls GitHub `PATCH /app/hook/config` with an App JWT signed **inside swarm-api** | called by the engine, as the deployer `swarm-tf-deployer` with a Google-signed ID token (below; review-fix finding 2), after the self-check; the request and the response carry no key material, so the private key never leaves swarm-api (review finding 10) |
| `POST /v1/admin/setup/links` | writes `<kind>.<label>.link` (a hash, never the token) | one route, three kinds: `pool-account`, `team-git`, `addon-secret` (review finding 21). For a pool account it mints a one-time sign-in link for a named owner; the link opens the existing `/v1/accounts/authorize` → `/exchange` flow (`apps/swarm-api/swarm_api/routes/accounts.py:424`, `:451`) for that owner only. That flow files the account into the **caller's** tenant and needs role `member` there (`_writer_tenant_id`, `accounts.py:173-185`, `owner_tenant` at `:441-447`), so the route refuses a pool-account link until the first team exists, and only for an owner who is a member of the first team's group; the link carries the first team as the selected workspace, so the owner's sign-in lands with `owner_tenant` = the first team (review-fix finding 1). `sc account add` takes the same path from the terminal |
| `POST /v1/admin/setup/teams` | writes the workspace record through the existing workspace path, and `teams/<id>` | the same call `swarm team add` makes; `swarm-workspace-apply` does the namespace, quota, verify-keys and identity (design §5.7, one writer) |
| `POST /v1/admin/setup/links` (kind `team-git`) | mints a one-time link to the team git slot's form | the value goes through swarm-api's credential writer (`credentials.py:150`), never the terminal (CLAUDE.md forge-token rule) |
| `PUT /v1/admin/setup/health` | writes `health/latest` | **only** the `swarm-doctor` service account may call it (checked by email from the verified token) |
| `GET /v1/admin/setup/health` | reads `health/latest` | |
| `POST /v1/admin/setup/health/groups-probe` | reads group membership with swarm-api's own Groups Reader identity | callable by `swarm-doctor`, the engine's deployer identity (stage 8's probe) and platform admins; answers met or unmet per registered group, never a member list (review finding 12) |

**How the engine reaches swarm-api (review-fix finding 2).** swarm-api's ingress is `internal-and-cloud-load-balancing`, so the laptop reaches it only through the load balancer, behind IAP; a gcloud user token is refused there (401, code 900, lesson 18), and the person's own identity is not necessarily a platform admin. The engine therefore calls swarm-api as the deployer service account it already impersonates (`sa.swarm-tf-deployer`, through `iam-binding.installer-impersonates-deployer`): `iamcredentials generateIdToken` for `swarm-tf-deployer` with `includeEmail = true` and the API backend's IAP audience (computed in infra pass 2, read from outputs). IAP admits it because TF-2 grants that account `roles/iap.httpsResourceAccessor` on the API backend only (`+iam-binding.deployer-iap-api-accessor`, stage 7). swarm-api admits the verified email `swarm-tf-deployer@<project>.iam.gserviceaccount.com` (derived from the project id it already reads) on exactly two routes, `POST /v1/admin/setup/github-app/hook` and `POST /v1/admin/setup/health/groups-probe`, and refuses it everywhere else. The client is ENGINE-4's `swarm_install/api_client.py`; that a service-account ID token with the backend's IAP client id as audience passes IAP is **to be proven** on the D22 test project in P3's pre-release run (section 10), and if it does not, the fallback is the person's own sign-in through the Desktop client (as `sc login` does), which then needs them to be a platform admin.

### 5.4 One install record, no races: who writes what

Firestore collection `swarm-install`, document `<env>` (D26), and its subcollections. **Every document has exactly one writer**; a reader never writes what it reads.

| Document | Writer | Fields |
|---|---|---|
| `swarm-install/<env>` | the engine (CLI), only | `schema_version`, `release`, `region`, `hostname`, `stages[0..15]` `{state, at, by}`, `lease {holder, heartbeat, expires}`, `verify {check, done, total, started, expected_end, deadline, result}`, `hand_off_at`, `ready_at` |
| `swarm-install/<env>/console/github_app` | swarm-api, only | `state_hash`, `id`, `slug`, `client_id`, `installed_on`, `secrets_written_at`, `self_check` |
| `swarm-install/<env>/console/accounts` | swarm-api, only | per label: `owner`, `link_hash`, `link_expires`, `signed_in_at` |
| `swarm-install/<env>/teams/<id>` (a collection under the env document since PR 2850: `swarm-install/<env>/console/teams/<id>` has five segments, and a Firestore document path needs an even count) | swarm-api, only | `group`, `workspace`, `first_admin`, `invites`, `progress`, `smoke` |
| `swarm-install/<env>/console/finished` | swarm-api, only | `at`, `by` (written when every required console step is done) |
| `swarm-install/<env>/health/latest` | swarm-api on behalf of `swarm-doctor`, only | `run_at`, `checks`, `findings` |
| `gs://swarm-tfstate-<P>/swarm-install/state.json` | the engine, only | the authoritative state (design §2.3); the engine copies the console's results into it when it reads them |

Within one writer, every write is a transaction with an `update_time` precondition (Firestore) or `ifGenerationMatch` (GCS), so two CLIs or two swarm-api replicas cannot lose an update; the install lease keeps two CLIs from running steps at once. ENGINE-1's `test_record_writers.py` and SETUP-API-1's `test_install_record_writers.py` hold the table.

### 5.5 The hand-off, end to end

1. The CLI finishes stage 13, checks `https://<H>/` answers behind IAP, writes `hand_off_at`, prints the one URL, and keeps its lease, heartbeating every 30 s.
2. The console's GitHub step lands (`github_app.secrets_written_at`). The CLI sees it (it polls the console documents every 10 s), runs infra pass 3 (App ids, `enable_github_app`, `enable_forge_refresh`), then writes `stages[9].state = switches_on`; the console then runs the `/admin/forge/app` self-check and writes `self_check`. The CLI then asks swarm-api to set the webhook active (`POST /v1/admin/setup/github-app/hook`, called as `swarm-tf-deployer` with an ID token for the API's IAP audience, section 5.3; swarm-api signs the App JWT, so the private key never reaches the laptop or a Claude session) and runs pass 4 (`enable_github_hooks`). The three secrets' containers and swarm-api's add/read grants already exist from infra pass 2 (`enable_console_app_setup`), so the conversion in step 2 has somewhere to write (key before switch). Passes 3 and 4 are triggered steps of the hand-off wait (ENGINE-1's `test_wait_runs_steps_whose_trigger_lands`; ENGINE-3's `steps/handoff.py`).
3. When `console/finished` exists, the CLI runs stage 14, writing `verify` as it goes (the console's verify row), then writes `ready_at` and prints READY (B09b).
4. If the CLI was closed, every console step still works up to the App's self-check; the page says how to continue (5.1, "Waiting for the CLI"). Re-running `swarm install` resumes at step 2 or 3.

---

### 5.6 The Sign-in client step: the brand and the Desktop client move to the console (issue #2964)

**Where it lives now.** Admin › Setup's first step, before the GitHub App, is **Sign-in client**. It links three Google Cloud console pages in order (Branding: app name SwarmCloud, support email the person's own address; Audience: Internal; Clients › Create client: Desktop app, name SwarmCloud sc) and takes the client's id and secret in a form. `POST /v1/admin/setup/sign-in-client` (platform admins only, audited like the GitHub App conversion) refuses an id that does not end `.apps.googleusercontent.com` (422 `client_id_invalid`), adds the secret as a new version of `swarm-iap-desktop-client-secret` and never returns, logs or stores it elsewhere, and records `{client_id, saved_at, saved_by}` in `swarm-install/<env>/console/sign_in_client`. `GET /v1/admin/setup` serves `sign_in_client` as `waiting`, `saved` or `applied`, with the three links built from the deployment's own project id.

In the installer, stage 2 only makes the secret's **empty container** (`edge.client-secret-container`), so infra pass 2 can grant swarm-api `secretVersionAdder` on that one secret (`terraform/infra/discovery.tf`, under `enable_console_app_setup`). `edge.brand` and `edge.desktop-client` are **waits in stage 13, after `handoff.console`**, and ask nothing in the terminal. The hand-off wait carries three triggered steps that run once the console saved a client this state has not taken in: `handoff.sign-in-client` reads the secret's latest version with the person's own credentials into the new deployment's context credential store (`verify_context`, as before, so verify never asks); `handoff.sign-in-bootstrap` re-runs bootstrap pass 2, which writes `frontend_iap_programmatic_clients` through the IAP settings API, and records the client as `iap_programmatic_client` in the engine document, which the route reads as `applied`; `handoff.sign-in-infra` re-runs the latest infra pass the install reached, so the discovery document names the client and no switch a later pass turned on goes off. Infra and bootstrap passes before then render the client as absent (`oauth_client_id` empty, programmatic clients `[]`). A state that recorded a client id from the old terminal prompt is met as before: the passes' own checks find the id already applied, and nothing is read again.

**Why.** The real install of 2026.10.1-rc.3 stopped at `edge.brand`, which sent the person from the terminal to three console pages and asked for the client's id and secret there. Google shut the IAP OAuth Admin API down on 2026-03-19, so no API creates the brand or the client; the owner chose (2026-11-01) to keep the client and make the console, where the person already is, the place they make it. The move is sound because the console does not need the brand: "By default, IAP uses a Google-managed OAuth client", which "can only be used to manage access for internal users that are within an organization" ([custom OAuth configuration](https://cloud.google.com/iap/docs/custom-oauth-configuration)), and `terraform/modules/frontend/main.tf` turns IAP on with no `oauth2_client_id`, so the organisation's people reach Admin › Setup in a browser before any brand exists. The Desktop client is for programmatic sign-in only (`sc`, verify): "IAP-protected applications that use the Google-managed OAuth client can still be accessed programmatically using a separate OAuth client configured through the programmatic_clients setting", which the IAP settings API (`iapSettings`, not shut down) writes ([sharing OAuth clients](https://cloud.google.com/iap/docs/sharing-oauth-clients)). The engine reads the secret's value only for this one secret, and only to hand it to the credential store (`tests/unit/install/test_switches.py` holds that).

## 6. The discovery service and the plugin's source (D28, Q1, Q3, Q4)

### 6.1 The route

`GET https://<H>/.well-known/swarmcloud`, served by the `swarm-discovery` Cloud Run service (DISCOVERY-1, TF-2): the swarm-api image with the discovery factory as its command, ingress `internal-and-cloud-load-balancing`, **invoker IAM check disabled** (no `allUsers` binding, as Q1/Q3), a third backend without IAP, one `path_rule`. Every other path on that service is 404. It never touches Firestore.

### 6.2 What it serves, and what it never serves

Public (`schema: 1`): `name`, `version` (the deployment's release), `console_url`, `api_url`, `oauth_client_id` (the Desktop client), `iap_audience` (the API backend's), `plugin.min_version` (the oldest plugin this deployment accepts), and, only if Q1's probe fails and the switch is on, `oauth_client_secret`.

Never: any other secret, a tenant, a group, an email, a project number beyond what `iap_audience` already embeds, the install state. `test_discovery.py::test_document_keys_are_exactly_the_public_set` fails on any extra key.

### 6.3 The PKCE-only attempt and its fallback (Q1)

1. **The attempt.** PLUGIN-1 adds `sc login --pkce-only`, which runs the installed-app flow with PKCE and **no** `client_secret`. BGD runs it once against the live deployment's existing Desktop client (wave 2, after PLUGIN-1 merges; it needs his sign-in, so it is a manual step, about two minutes). The result is recorded in the PR of TF-2.
2. **If Google accepts it:** `discovery_serves_client_secret = false` (the default). The plugin needs no secret; `swarm-desktop`'s secret stays in Secret Manager for nothing but the record.
3. **If Google refuses it:** the engine sets `discovery_serves_client_secret = true` in the generated tfvars; `swarm-discovery` gains `secretAccessor` on `swarm-iap-desktop-client-secret` only and serves the value (design §9.3: Google classifies it as non-confidential; Internal brand plus IAP remain the boundary). This is BGD's already-made Q1 decision, not a new one.

Either way the secret enters through ENGINE-2's hidden prompt at stage 2, straight into Secret Manager.

### 6.4 The marketplace and the plugin bundle in the release home

Per release `V`, RELEASE-1 publishes:

```
gs://swarmcloud/
  marketplace.json                     the marketplace (D28): one plugin "sc", version V, source releases/V/plugin/
  releases/V/plugin/                   the plugin directory: .claude-plugin/plugin.json, commands/, skills/, workflows/
  releases/V/marketplace.json          the same marketplace pinned to V (for a team that must stay on V)
  releases/V/swarm_mcp-V-py3-none-any.whl     the bridge the plugin runs (R6)
  releases/V/compat.json               {"plugin": "V", "deployments": {"min": "...", "max_exclusive": "..."}}
  releases/V/kubernetes.tgz            render.py and the Kubernetes trees stage 10, team add, upgrade and uninstall render from
```

`marketplace.json` at the root moves only with `channels/stable` (section 7.3). A teammate types the D28 line; `deployment_url` is the plugin's only required user config.

**To be proven (RELEASE-1, a manual check before the first RC):** that `claude plugin marketplace add <https URL to marketplace.json>` accepts a plugin whose `source` is a path relative to that URL. If Claude Code accepts only git or GitHub sources for URL-hosted marketplaces, the fallback is open question 1.

### 6.5 Version skew between plugin and deployment

The plugin can run ahead of the deployment it talks to (D28). The rule: **a skew is always named, with both versions and the fix; it is never a silent failure.**

- **Published with each release:** `compat.json`, the range of deployment versions the release's plugin supports (`min` inclusive, `max_exclusive`), and the deployment's own `plugin.min_version` in its discovery document. The range is set by RELEASE-1 from the API compatibility the release keeps: a deployment version is in range when every route and field the plugin calls exists in it (checked by the manual pre-release test, section 7.3, which runs the new plugin against the previous release).
- **At the plugin's start, and on any 404/410 from the API:** the bridge reads discovery (cached for an hour), compares.
- **Plugin too old** (`plugin < deployment.plugin.min_version`): every tool call answers `sc plugin <P> is older than this deployment (<D>) accepts (needs ≥ <min>). Update it: claude plugin update sc@swarmcloud`.
- **Deployment too old** (`D` outside the plugin's range): `this deployment runs SwarmCloud <D>; plugin <P> supports <min> to <max>. Ask your admin to run: swarm upgrade --to <P>. Or pin the plugin to your deployment: claude plugin marketplace add https://storage.googleapis.com/swarmcloud/releases/<D>/marketplace.json`.
- **Discovery unreachable:** `cannot read https://<H>/.well-known/swarmcloud (<status>)`, exit 3, never a guessed version.
- `sc doctor` prints both versions and the range. PLUGIN-1's `test_version_skew.py` holds the three messages.

---

## 7. The release (D5, D16, D21, D22, D25, Q4)

### 7.1 The release home (project `swarmcloud-prod`, D25)

RELHOME-1's script, run by BGD: Artifact Registry `us-docker.pkg.dev/swarmcloud-prod/swarmcloud` (Docker), `gs://swarmcloud`, KMS key `swarmcloud-release/cosign` (P-256). **The one org-policy exception (Q4):** `iam.allowedPolicyMemberDomains` overridden on project `swarmcloud-prod` alone, covering `allUsers` as `roles/storage.objectViewer` on the bucket and `roles/artifactregistry.reader` on the repository. It is an org-admin action (BGD); the script checks the effective policy and refuses the public grant until the exception exists.

### 7.2 The pipeline

1. **Tag** `v<YYYY.MM.N>` on `main` triggers `release-public.yml` (RELEASE-1's patch; **held: workflow file**).
2. **Build prod-shaped images** in SwarmCloud's own CI into its existing registry: the console with no `VITE_SWARM_ENV` (UIENV-1), Dockerfiles with no project (DESAGA-1).
3. **Scan** with the existing trivy gate; a HIGH or CRITICAL needs an acceptance with an expiry, which `RELEASE.md` lists (lesson 27).
4. **Assemble** (`scripts/release/assemble.sh`): `module.tgz` (`terraform/{bootstrap,infra,modules}`, no tfvars, no `environments/`), the two wheels, `manifest.yaml`, `images.json` (digests only), `kubernetes.tgz` (`kubernetes/render.py`, `policies/`, `swarm-system/`, `rbac/`, the tenant templates), the plugin directory, `compat.json`, `marketplace.json`, `RELEASE.md`, `SHA256SUMS`. Uploaded as a workflow artefact.
5. **Publish** (`scripts/release/publish.sh`), run by a person, not CI: copy each image by digest to the release home, sign each digest with cosign and the KMS key, sign `SHA256SUMS`, upload under `releases/<V>/`. CI holds no grant in project `swarmcloud-prod`, so D22's "no IAM change for CI" holds (open question 4 asks whether BGD wants CI to publish later).
6. **Promote** (`publish.sh --promote V`): moves `channels/stable` and the root `marketplace.json`, refused unless the manual test of 7.3 is recorded for `V`.

**Signing: cosign with a KMS key**, not Binary Authorization. cosign signatures live beside the images and are verified on the installer's machine with the public key pinned in the engine wheel (`swarm_install/release_key.pub`, written by ENGINE-3 after BGD has run `release-home.sh`; cosign and gcrane are fetched at pinned versions and checked against pinned SHA-256s by `swarm_install/tools.py`) (design §6.1); Binary Authorization would need a policy and attestors in every company's project, which core does not otherwise need.

### 7.3 The manual pre-release install-and-upgrade test (D22)

Before `--promote`, CLD runs, in the throwaway or kept test project BGD provides (CLD creates no project):

1. `publish.sh` the candidate to `releases/<V>-rc.<n>/`.
2. `swarm install --version <previous>` into the test project, then `swarm upgrade --to <V>-rc.<n>`; `swarm doctor` exits 0; the new plugin runs one agent against the previous release first (the skew check, 6.5).
3. `swarm uninstall`; the "what remains" record lists only the eight non-removables of design §10.
4. The record (versions, times, results) is written to `releases/<V>-rc.<n>/TESTED` by `publish.sh --record-test`, which `--promote` requires.

The first release has no previous version: its test is install, doctor, uninstall, reinstall (run 2b's shape, D24).

---

## 8. The live-deployment migration (Q7)

BOOT-2 moves the platform service accounts into bootstrap for the live deployment too.

**What changes in code.** Infra gets `removed { from = <address>; lifecycle { destroy = false } }` for each of: `module.iam.google_service_account.platform` (`terraform/modules/iam/service_accounts.tf:34`), `module.iam.google_service_account.tick` (`:48`), `google_service_account.verify` (`terraform/infra/verify.tf:38`), `module.scheduler.google_service_account.schedule_tick` (`terraform/modules/scheduler/main.tf:236`), `module.scheduler.google_service_account.rollup_sweeper` (`terraform/modules/scheduler/jobs.tf:155`). Bootstrap gets the same accounts as resources, with `import {}` blocks gated on `adopt_from_infra_states` as `terraform/bootstrap/platform_roles.tf:425-437` does for roles. Infra reads them as data sources. Per-tenant worker accounts (`terraform/modules/tenancy/main.tf:109`) stay where they are: installs have no Terraform tenants, and the live tenants' accounts are not platform accounts. `mcp_test_server` (`addon:ci`) stays in infra.

**The plan BGD reviews (the merge gate).** BGD runs, in order, and reads each for destroys:

1. `terraform plan` of bootstrap for the live project: N to import (the five resources, with every key of `module.iam`'s `for_each`), 0 to destroy, and the grants that used to need "later" booleans as in-place no-ops.
2. `terraform plan` of infra dev and prod: the same addresses as "will be removed from state, not destroyed", 0 to destroy, 0 to replace.

**Any destroy or replace blocks the merge** (Q7). BGD applies bootstrap first, then infra (the reverse order is also safe, because infra reads the accounts through data sources that find them either way; bootstrap first is chosen so that no moment exists where no root holds them).

**Order relative to the other lanes.** BOOT-2 is wave 1, after TF-1 and BOOT-1 are merged **and applied** live (so the live plan BOOT-2 shows is against current state). TF-2 (wave 2) creates the discovery and doctor accounts' grants and must follow BOOT-2's live apply. The live release pipeline keeps running between them: the migration changes no service account's email or unique id, so no grant, Workload Identity binding or RBAC subject (lesson 29) moves.

---

## 9. Orders

### 9.1 Install (stage by stage; key before switch; every wait bounded)

| Stage | Who | What, in order | Wait and its bound |
|---|---|---|---|
| 0 | CLI | tools and pins; gcloud signed in; rights (project create, billing link, org policy read, inherited bindings); Workspace or Cloud Identity and its domain; org policies; quotas incl. KMS signing and Cloud Run Jobs; hostname and DNS provider; GitHub org and admin; two Claude account emails; region (confirmed: permanent); alert email; cost estimate and budget; the release's signature and its accepted CVEs | none (reads only) |
| 1 | CLI | project and billing (or adopt), budget alert, APIs (one list), default compute SA Editor removed, state bucket (then local state uploaded into the GCS state object); the Firestore install record starts at stage 5, once Firestore exists | API enablement polled, 10 min |
| 2 | CLI, human | OAuth brand (guided, Internal), IAP service agent, global IP, DNS record (Cloud DNS or printed), DNS resolves to that IP, then the certificate wait starts in the background; Desktop client (guided), its secret by hidden prompt into Secret Manager (container created by the engine, labelled `managed-by=swarm-install`) | brand re-check every 10 s, 30 min; DNS 60 min; certificate 60 min, checked again before stage 7 |
| 3 | CLI | bootstrap pass 1 (roles, deployer, KMS ring and key or adopt, every platform service account), as the owner the one time, then the installer's impersonation grant | IAM propagation, 10 min |
| 4 | CLI | targeted infra apply of the registry, as the deployer; cosign verify; copy every digest | none beyond the copy |
| 5 | CLI | infra pass 1, every key-gated switch off; Firestore created in the stage 0 region; the install record `swarm-install/<env>` written from the GCS state | Terraform's own |
| 6 | CLI | generate platform keys into Secret Manager; **then** compute the switches from `ENABLED` versions | none |
| 7 | CLI | infra pass 2 (switches on, IAP audiences computed, alert channel, `require_alert_channels`, `workspace_gate`, `repository_grants_enforced`, discovery, `enable_console_app_setup` (the App's three secret containers and swarm-api's grants), `groups_auth = "admin_role"`, `tenant_job_vpc_egress`, `enable_scheduled_doctor` when the release carries it), bootstrap pass 2 (IAP programmatic clients, log view grant) | IAM propagation, 10 min; certificate ACTIVE |
| 8 | CLI, org admin | Groups Reader admin role on swarm-api's account (or delegation); probe group checked live; tenant groups named; admin group resolves | re-check every 30 s, 24 h bound, exit 4 between |
| 10 | CLI | cluster policies before anything that needs them; swarm-system workloads; RBAC subjects by uniqueId | rollout, 10 min |
| 13 | CLI | the channel exists, `require_alert_channels` is on, a test notification is accepted | 2 min |
| hand-off | CLI | `/` answers behind IAP; print ONE URL; keep the lease | |
| 9 | console, CLI | App by manifest (console) → secrets stored (containers from pass 2) → CLI pass 3 (App switches) → install on the org → self-check → webhook active (swarm-api's hook route) → CLI pass 4 (`enable_github_hooks`) | installation polled, 30 min; self-check 2 min |
| 11 | console | first team: record → `swarm-workspace-apply` (namespace, quota, verify-keys, identity, slots) → first admin → invites (the pool-account owners among them, role `member`) | workspace apply 30 min (its own deadline) |
| 12 | console | pool accounts signed in (each owner once, into the first team); the broker check; **then** the first team's smoke agent | a link expires in 24 h; no other bound (another person); smoke 15 min |
| add-ons | console | listed, off | |
| 14 | CLI | verify, in the VPC: smoke, GKE proof, race, Scheduler targets 2xx, digests, live browser, code intelligence, plugin agent and workflow, console agent and workflow | 45 min total, each check with its own bound; progress in the record |
| 15 | both | READY; the plugin line; `swarm doctor` | |

Stage 12 needs the first team: the existing sign-in files an account into the signed-in owner's own tenant and needs role `member` there (`apps/swarm-api/swarm_api/routes/accounts.py:173-185`, `:441-447`), so before stage 11 Ravi and Lea have no tenant for their sign-in to land in. The first team's smoke agent needs a pool account and stage 9's App (its repository). **The console orders its steps App → first team (record, `swarm-workspace-apply`, first admin, invites) → pool accounts, owned by the first team → the first team's smoke agent** (review-fix finding 1). Frames B07 and B08 draw the accounts before the team; their content stands and SETUP-UI-1/SETUP-UI-2 build them in this order.

### 9.2 Uninstall (reverse; typed confirmation that ignores `SWARM_ASSUME_YES`)

1. Scheduler jobs paused; running executions drained (countdown, 30 min bound).
2. Tenant namespaces and their objects; then cluster policies (`kubernetes/apply.sh` in reverse).
3. infra destroy as the deployer (only labelled resources; `make destroy`'s label check).
4. Images in the project's registry.
5. Secret versions and containers the engine created (`managed-by=swarm-install`), Desktop client secret included, as the deployer. **This is the last step that impersonates.**
6. **From here on, as the owner** (the identity bootstrap pass 1 used, stage 3; the engine first checks the person's own rights, as preflight does, and stops with exit 4 naming the missing permission): engine-made IAM bindings outside bootstrap, then the installer's impersonation grant on `swarm-tf-deployer`, removed **before** the account it names is deleted.
7. bootstrap destroy as the owner (the deployer, the platform service accounts, the KMS key versions; custom roles enter their 7-day reservation).
8. The "what remains" record written to the local cache and printed; then the state bucket. A reinstall does not depend on the cache: `adopt.py` re-derives every surviving name by probing (brand list, KMS ring, deleted custom roles, Firestore id), and asks only for what cannot be probed (the Desktop client id, the GitHub App's slug), with the cache's value as the default.

**What it cannot remove, with the exact step it prints:** the OAuth brand (no delete method); the KMS key ring and key (versions destroyed, ring stays); custom role ids (reserved 7 days); the GitHub App (the org deletes it: `https://github.com/organizations/<org>/settings/apps/<slug>/advanced`); the DNS record (at the registrar); Workspace groups; the Groups Reader admin role assignment or the delegation (admin.google.com paths); the Desktop OAuth client (a console step, kept for reuse); the Firestore database id (reusable after Google's delay). These are design §10's eight rows. Each is in the record so run 2b adopts it (D24). **Retention windows re-read (design §10 asked P2):** the plan does not settle them from documentation read offline; UNINSTALL-1's `explain()` text cites Google's pages for KMS deletion and custom-role retention, and run 2b (P5) is the live proof. The engine never relies on either window: it imports a ring if it exists and undeletes roles if undeletable, else creates them. Resume: each step is checked before it runs, and each step's identity is fixed by its number, never by what the last run used: steps 1-5 impersonate the deployer, steps 6-8 run as the owner. A crash at or after step 6 therefore never leaves a second `swarm uninstall` trying to impersonate an account that step 7 deleted, and a second run continues where the first stopped (review-fix finding 5).

### 9.3 Upgrade (drain, migrate, apply, verify, roll back)

1. Read the running version; refuse a downgrade (exit 5) unless `--allow-downgrade`.
2. Fetch and verify release `V`.
3. Plan everything and show it: both roots' plans, image digests, Firestore migrations, cluster-policy diff, new App permissions (with the review URL), new manifest entries.
4. IAM gate: any IAM create, change or delete waits for the typed project id; conditions linted first; a destroy-first IAM swap is refused.
5. **Drain:** admission paused (the platform's existing admission switch), running work allowed to checkpoint and park (invariant 4), bounded at 30 min with a countdown.
6. Apply: copy images → additive migrations → bootstrap → policies that admit more → infra (new code, `--to-latest` traffic) → policies that tighten → migrations that need the new code → namespaces for every tenant.
7. Verify: `swarm doctor` and the smoke; admission resumed.
8. **Roll back** on a failed image roll: re-apply the previous release's digests and generated tfvars; migrations that removed data never ran (they run only after a green doctor).

---

## 10. The proof (brief §10): what each wave leaves ready

| Wave | Leaves ready for P4/P5 |
|---|---|
| 0 | A manifest that a test holds complete, with every entry whose code is already on `main` (fragments per lane after it); images and scripts that build and run in any project; a bootstrap that can start from an empty project; a release home BGD can make public. |
| 1 | The engine core with resume and bounded waits; the live deployment on the one service-account layout; the discovery app; a console image with no baked environment. |
| 2 | Stages 0-2 and 8, 10, 14 runnable; `swarm install`; the setup API; a plugin configured by one value; the discovery path and the hourly doctor in Terraform (off until DOCTOR-1). |
| 3 | Stages 3-7, the hand-off, 9's switches, 13 and 15: a full `swarm install` from an empty project to READY; `swarm team add`; `/sc:install`; the setup panels; the release assembled with `kubernetes.tgz`. |
| 4 | `swarm doctor` (CLI and hourly), `uninstall`, `upgrade`, `addon`; the setup page. |
| 5 | Admin › Setup reachable; the generated install page. **Then:** RC1 published by `publish.sh`, and the P3 exit check. |

**P3's exit check (brief §8), run by CLD after wave 5:** `swarm install --project <empty project BGD provides> --env prod --version <RC1> --domain swarmcloud.saga.xyz --dry-run` exits 0 or 4 (4 only for the human steps it lists), printing every stage's plan and no write (checked by the project's audit log showing no mutating call).

**The commands BGD runs in P4** (from the product only, D12; no checkout):

```
curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
swarm install --project swarmcloud-dayone-1 --env prod --version <RC> --domain swarmcloud.saga.xyz
#   ... guided human steps; then the ONE URL: https://swarmcloud.saga.xyz/admin/setup
#   Admin › Setup: GitHub App, first team, Claude accounts, Send to your team
#   (the CLI prints READY after verify)
swarm doctor                     # exit 0
swarm team add <second group>    # or Admin › Setup, second team
swarm uninstall                  # typed confirmation; prints what remains
```

A teammate (not BGD) then runs the D28 line with `deployment_url=https://swarmcloud.saga.xyz`, `sc login`, and `/sc:submit` to a PR. P5 repeats into `swarmcloud-dayone-2`, then run 2b into `swarmcloud-dayone-1`, then `swarm upgrade --to <v1 candidate>`. CLD keeps the snag log (brief §10).

---

## 11. Budget

| Wave | Lanes | Estimate |
|---|---|---|
| 0 | MANIFEST-1 35, DESAGA-1 15, DESAGA-2 15, DESAGA-3 20, DESAGA-4 8, K8S-1 20, TF-1 30, BOOT-1 25, RELHOME-1 12 | $180 |
| 1 | DISCOVERY-1 15, IDENTITY-1 20, ENGINE-1 45, BOOT-2 40, UIENV-1 15 | $135 |
| 2 | PLUGIN-1 35, TF-2 45, ENGINE-2 30, ENGINE-4 25, CLI-1 20, SETUP-API-1 40 | $195 |
| 3 | RELEASE-1 30, ENGINE-3 55, TEAM-1 30, SETUP-UI-1 20, SC-INSTALL-1 10 | $145 |
| 4 | DOCTOR-1 30, UNINSTALL-1 30, UPGRADE-1 35, ADDON-1 25, SETUP-UI-2 20 | $140 |
| 5 | SETUP-UI-3 10, DOCS-1 12 | $22 |
| **Lanes** | 32 | **$817** |
| Reserve | fix rounds after red CI and held-PR revisions (~10 %) | $83 |
| **P3 total** | | **$900** |

**Within P3's ~$900**, with no cut; the review's fixes added $30 to the lanes, taken from the reserve. The estimates assume the measured cost of recent lanes (P1's frame lanes were $4.98 and $8.24; code lanes with tests run several times that). If wave 0 and 1 together pass $450, the next wave pauses and CLD asks BGD which of these to merge or defer, in this order: SETUP-UI-3 into SETUP-UI-2's PR (saves a lane's overhead), DOCS-1 deferred to P4 (the guide is generated, so it can follow), ADDON-1 split so only the CI add-on's held patch waits.

---

## 12. Open questions for BGD

Only what the decisions do not settle.

1. **If a URL-hosted marketplace cannot install a plugin from a relative path** (section 6.4, to be proven by RELEASE-1 before RC1), where does the plugin's source live?
   - (a) A public, read-only git repository holding only `plugin/`, under a GitHub org BGD names (D16: `swarmcloud` on GitHub is taken).
   - (b) A plugin source of type `url` pointing at a git bundle served from `gs://swarmcloud`, if Claude Code supports it.
   - (c) Keep the plugin in the existing marketplace (`bogdan-alexandrescu/SwarmCloud`) and publish only the bridge to the release home.

   **Recommend (a)**: it keeps D28's one line and needs nothing Claude Code may not support. Built: nothing until the check runs; the check is a two-minute manual step.
2. **The `swarm.saga.xyz/` Kubernetes label and annotation keys** (R5) stay in every company's cluster.
   - (a) Keep them in v1: they are identifiers, not deployment config, and renaming them is a live-cluster migration of every object and admission policy.
   - (b) Rename to a neutral prefix (for example `swarmcloud.dev/`) in v1, with a migration lane for the live cluster.

   **Recommend (a)**, with (b) as a post-v1 issue. The plan builds (a).
3. **File the two drafted contract requests** (section 4)?
   - (a) File both (127 `ALLOWED_DOMAINS` without a default; 128 comments to example values).
   - (b) File 127 only.
   - (c) File neither; the engine's environment and doctor's check guard the fallback.

   **Recommend (a)**. Built either way: nothing in P3 waits on them.
4. **Who publishes a release to project `swarmcloud-prod`.** D22 keeps IAM for CI unchanged, so `publish.sh` is run by a person (section 7.2).
   - (a) A person runs `publish.sh` for v1 (CLD or BGD, with rights in `swarmcloud-prod`), as planned.
   - (b) Add a release-publisher identity in `swarmcloud-prod` that the tag workflow assumes (an IAM change for CI, held).

   **Recommend (a)** for v1: it honours D22 and the manual test already puts a person in the loop.

---

## 13. The adversarial review: findings and what changed

The P2 review lane read this plan against the design, the manifest, the lessons and the code, with one job: break it. It reported 4 blockers, 15 majors and 2 minors. It also checked, and found consistent: the budget arithmetic, the lane count (32), every territory path exists or is marked (new), no file shared within a wave, no UI lane over 3 files. Every finding below is fixed in the sections named.

| # | Severity | Finding (evidence) | What changed |
|---|---|---|---|
| 1 | Blocker | Only MANIFEST-1 could edit the manifest, so the completeness test (design §3.3, both directions) would turn `main` red when a later lane added a collectable item or when MANIFEST-1 listed `+` entries whose Terraform did not exist yet. | Section 1 conventions: per-lane fragments `install/manifest.d/<lane>.yaml`, each `+` entry added by the lane that creates its code; `test_fragments_merged_and_ids_unique`; fragments in TF-1, BOOT-1, DISCOVERY-1, IDENTITY-1, BOOT-2, TF-2, SETUP-API-1, DOCTOR-1, ADDON-1. |
| 2 | Blocker | The console wrote the App's three secrets before their containers existed (`terraform/modules/secret_manager/main.tf:249-255`, `terraform/infra/github_hooks.tf:136-137` create them only under `enable_github_app` / `enable_github_hooks`), and `enable_console_app_setup` was set by no stage. | TF-2 creates the containers and swarm-api's add/read grants under `enable_console_app_setup`, independent of the App switches; `secret_manager/main.tf` added to TF-2; ENGINE-3 sets it in pass 2 (`test_pass2_sets_console_app_setup`); sections 5.3, 5.5, 9.1; manifest `switch.enable_console_app_setup`. |
| 3 | Blocker | The release bundle carried no Kubernetes trees, though stage 10, team add, upgrade and uninstall need them with no checkout; no lane built the pinned cosign/gcrane download or the pinned release key. | RELEASE-1 adds `kubernetes.tgz` (`test_bundle_carries_kubernetes`); ENGINE-3 adds `tools.py` and `release_key.pub` (after BGD runs `release-home.sh`); sections 6.4, 7.2; manifest `other.release-kubernetes-bundle`, `other.release-signing-public-key`. |
| 4 | Blocker | `terraform/infra/acceptance_fixture.tf:41,99-103` grants `allUsers` with no `count`: core would fail under Domain Restricted Sharing, contradict D9/D10, and TF-2's own `test_no_allusers_binding_anywhere` had no file to fix. | TF-2 gates it with `enable_acceptance_fixture` (default false, `addon:ci`; live tfvars set true, `moved` to `[0]`, zero-change live plan); ADDON-1 turns it on; manifest `switch.enable_acceptance_fixture`. |
| 5 | Major | BOOT-2 lacked the files that reference the migrated accounts (`modules/iam/outputs.tf:49,53,58`, `bindings.tf:14,16,332`, `infra/locals.tf:1032`, `infra/deployer.tf:46`). | Added to BOOT-2. |
| 6 | Major | TF-2 could not add the discovery backend without `terraform/infra/main.tf:683-699`. | Added to TF-2 (↺). |
| 7 | Major | `GROUPS_AUTH` needs `swarm_api/settings.py`, which DESAGA-3 held in the same wave; SETUP-API-1 needs it too. | IDENTITY-1 moved to wave 1 with `settings.py` ↺ (depends on DESAGA-3); SETUP-API-1 gets `settings.py` ↺. Waves 0/1 were then 10/5 lanes (9/5 after section 13.1). |
| 8 | Major | No lane switched installs to the admin-role group mode (Q2), so swarm-api would read groups through delegation that was never set up. | ENGINE-3 `test_groups_auth_admin_role_for_installs`; ENGINE-4 `test_probe_runs_through_swarm_api_in_its_mode`; stage 7 sets it; manifest `declared-value.groups_auth`. |
| 9 | Major | The hand-off wait (ENGINE-4, wave 2) had to run passes 3 and 4, which ENGINE-3 builds in wave 3. | `handoff.py` and `test_handoff.py` moved to ENGINE-3; ENGINE-1 adds `test_wait_runs_steps_whose_trigger_lands`. |
| 10 | Major (secret path) | Activating the webhook from the CLI needed an App JWT, so the private key would reach the laptop, possibly a Claude session. | New route `POST /v1/admin/setup/github-app/hook` (swarm-api signs); `test_hook_activation_signed_server_side`; ENGINE-3 `test_engine_never_reads_an_app_secret`; section 5.5. |
| 11 | Major (secret path) | `getpass` falls back to stdin with no TTY, so under `/sc:install` a value could be piped in by Claude. | ENGINE-2 `test_refuses_without_tty` (`needs_terminal` event, exit 4); SC-INSTALL-1 `test_secret_step_hands_bang_command`. |
| 12 | Major | The hourly doctor would run two waves before its code; `swarm-doctor` was not in `PLATFORM_SERVICE_ACCOUNTS` (lesson 38's exact cause) and could not re-probe groups. | TF-2 gates it with `enable_scheduled_doctor` (off), adds the account to `PLATFORM_SERVICE_ACCOUNTS`; DOCTOR-1 turns it on through `tfvars.py` ↺; group re-probe through swarm-api's new `groups-probe` route. |
| 13 | Major | `swarm status` already exists (`apps/swarm-mcp/swarm_mcp/cli.py:1870`). | The install's status is `swarm install --status`; `verbs/status.py` dropped; `test_existing_swarm_status_unchanged`. |
| 14 | Major | K8S-1's mandatory `--project` would break `.github/workflows/security.yml:235` and `Makefile:343`. | `--project` required only for project-bearing subcommands; `render.py policies` unchanged (`test_policies_unchanged_without_project`). No workflow change. |
| 15 | Major | The namespace, quota and `--providers` logic is in `scripts/register-tenant.sh`, not `workspace-apply.sh`; both run as the deployer with account-IAM power, unflagged. | `register-tenant.sh` ↺ added to TEAM-1; TEAM-1 and DESAGA-2 flagged `held: IAM` (9 held lanes, 12 flags). |
| 16 | Major | The setup panels (wave 3) called an API client added only in wave 4; the mock is not in the tree; no lane built the App callback page. | `api.ts` moved to SETUP-UI-1 and `admin-setup.css` to SETUP-UI-2; the mock comes from P1-B's artefacts via the brief; `GitHubAppCallback` in SETUP-UI-1, its route and `/admin/setup/health` tested in SETUP-UI-3. |
| 17 | Major | `plugin/skills/sc/SKILL.md` (44 `uv run` hints, the live front door at `:1061`) and `plugin/commands/sc.md` ship in the bundle, in no territory (D1). | Added to PLUGIN-1 with `test_plugin_has_no_checkout_hint_or_live_literal`. |
| 18 | Major | Reinstall adoption covered two of design §10's eight names; the record lived only in the local cache; the retention windows were not reported. | Eight names everywhere; `adopt.py` tests for the brand, Desktop client, App reuse or rename, Firestore wait, and re-derivation by probe; section 9.2 says how the windows are handled. |
| 19 | Major | Lessons 7, 8, 18, 23, 27, 28, 30, 35, 43 mapped to prose or to tests no lane wrote; 10, 11, 26, 47 had no manifest entry; lesson 35's guard (`tenant_job_vpc_egress`, default false) was set by no stage. | Named tests added to the owning lanes and section 3; manifest `other.engine-terraform-rules`, `other.upgrade-iam-gate`, `other.sc-install-refusal-rule`, `switch.tenant_job_vpc_egress`; stage 7 sets the egress switch. |
| 20 | Minor | Stage 1 wrote the Firestore install record before Firestore exists (stage 5). | Section 9.1: stage 1 writes the GCS state; the record starts at stage 5. |
| 21 | Minor | Add-on secrets were promised through a link no route minted. | One links route with kinds `pool-account`, `team-git`, `addon-secret` (section 5.3). |

### 13.1 The review of the fixed plan: findings and what changed

A second adversarial review read the plan above and reported 2 blockers and 3 majors ("review-fix finding N" in the text); 8 minors go to the wave epic.

| # | Severity | Finding (evidence) | What changed |
|---|---|---|---|
| 1 | Blocker (wrong order) | The console ordered App → accounts → team, but the existing sign-in files an account into the caller's tenant and needs role `member` there (`apps/swarm-api/swarm_api/routes/accounts.py:159-185`, `:441-447`), so before stage 11 the pool-account owners had no tenant; the team's smoke agent needs a pool account. | Order App → first team → pool accounts owned by the first team → the team's smoke agent (sections 5.1, 9.1); the pool-account link is refused before the first team and only for its members, and lands with `owner_tenant` = the first team (section 5.3; SETUP-API-1's three `test_pool_link_*` tests); TEAM-1 `test_smoke_waits_for_a_pool_account`. |
| 2 | Major (missing dependency) | No lane said how the laptop engine authenticates to swarm-api behind IAP: gcloud user tokens are refused (lesson 18) and the deployer is not a platform admin, so the hook call and stage 8's probe had no working caller. | The engine calls as `swarm-tf-deployer` with a Google-signed ID token for the API's IAP audience (section 5.3); TF-2 grants it `roles/iap.httpsResourceAccessor` on the API backend only; swarm-api admits it on the hook and groups-probe routes only; ENGINE-4 builds `api_client.py` with `test_api_client.py`; SETUP-API-1 `test_engine_identity_admitted_only_on_hook_and_probe`; manifest `iam-binding.deployer-iap-api-accessor`. |
| 3 | Major (wrong order / red main) | TF-1, BOOT-1 and DISCOVERY-1 wrote fragments in wave 0, where only MANIFEST-1 builds the loader; MANIFEST-1 left out additions whose code already exists (`switch.tenant_job_vpc_egress`), so its completeness test would be red against `main`. | MANIFEST-1 carries every entry whose reference exists on `main`, including `declared-value.monitoring-excluded-tenants` and `switch.tenant_job_vpc_egress`; TF-1 and BOOT-1 add no collectable item and lose their fragments; every fragment-writing lane depends on MANIFEST-1; DISCOVERY-1 moves to wave 1, so PLUGIN-1 moves to wave 2 and RELEASE-1 to wave 3 (sections 1, 1.7, 10, 11; waves are now 9/5/6/5/5/2, 32 lanes, the same $817). |
| 4 | Major (lesson guard) | Lesson 45 is the CI add-on's merge-App token input (`.github/workflows/runner-fallback.yml:120`), not the tenant App's manifest; SETUP-API-1's test never reads a workflow file. | Row 45 maps to `github-app-setting.merge-app` (`addon:ci`) and ADDON-1 `test_ci_addon_app_token_inputs_declared_by_action`; no core check (D10); the count is 5 of 48. |
| 5 | Major (cannot be retried) | Uninstall never said which identity runs bootstrap destroy, and removed the impersonation grant after deleting the account it names; a crash part-way left a second run impersonating a deleted account. | Section 9.2: steps 1-5 as the deployer, steps 6-8 as the owner; the impersonation grant goes before bootstrap destroy; each step's identity fixed by its number; UNINSTALL-1 `test_identity_of_each_step`, `test_resume_after_deployer_deleted_runs_as_owner`. |
