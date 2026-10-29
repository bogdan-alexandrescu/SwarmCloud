# Day One: what went wrong standing up the live deployment

These are the failures from standing up and running the live SwarmCloud deployment (project `saga-agents-staging`, 2026-09-15 onwards). Each one becomes an installer check: P2's plan maps every lesson to a manifest entry in `manifest.draft.yaml`, or says why none is needed.

Source of the list: the history research pass of 2026-10-28, which read git history, `docs/DEPLOY_STATE.md`, `docs/runbooks/`, `docs/ci.md`, `docs/operations.md`, `docs/deploying-without-an-organisation.md`, `docs/incidents/`, the open issues and the operator's notes.

Every entry has the same parts:
- **When:** the date it happened.
- **What broke:** the failure.
- **Cause:** why it happened.
- **Rule:** the rule or check that follows.
- **Source:** where it is recorded. Commits, docs and issues are given where the research pass recorded them.

## A. Project, billing and APIs

1. **The operator could create a project but not link billing.**
   - **When:** 2026-09-15.
   - **What broke:** The operator held `resourcemanager.projects.create`, but not `billing.resourceAssociations.create`. A dedicated Firestore project was never built because of this (`docs/security.md`).
   - **Rule:** preflight checks that the operator can both create a project and link billing.
2. **The Firestore location can never change.**
   - **What broke:** A database's location is fixed once the database exists.
   - **Rule:** the region is settled in preflight, before stage 1.
3. **Back-to-back IAM binding calls are rate-limited.**
   - **What broke:** Successive `gcloud projects add-iam-policy-binding` calls hit a write rate limit, which reads as a quota error.
   - **Rule:** retry the call; never report it as a denial.
4. **Three API lists disagree.**
   - **What broke:** `scripts/prerequisites.sh` (18 APIs, without `cloudkms` and `iap`), `terraform/infra/main.tf` and `terraform/bootstrap/variables.tf` each list different APIs. `containerscanning` is deliberately off. The Cloud KMS signing quota was never checked; if it runs out, every submission fails with a 503 (contract request 34).
   - **Rule:** one canonical API list, plus a KMS quota check.
5. **Defaults that name the live project.**
   - **What broke:** `saga-agents-staging` is the default in `scripts/lib/common.sh:158`, `.env.example` and the bootstrap tfvars, and appears in about 70 files. `SHARED_DENY_LIST` names the other team's resources.
   - **Rule:** no live-project default anywhere an install reads.
6. **The default compute service account holds Editor.**
   - **What broke:** With Editor on the project it can act as every swarm account (#1020).
   - **Rule:** remove the grant on day one, and give Cloud Build its own least-privilege account.

## B. Bootstrap, Terraform state and order

7. **Bootstrap state lived on one laptop.**
   - **When:** 2026-10-07 (#827, eb7aa0344).
   - **What broke:** A run from a fresh worktree planned "9 to import, 91 to add".
   - **Rule:** state lives in the bucket from the first run; an empty state is refused while the deployer exists.
8. **Infra applied before bootstrap.**
   - **What broke:** The apply fails with `Role ... swarmSecretLister does not exist`.
   - **Rule:** bootstrap first. The first bootstrap runs with `adopt_from_infra_states = []` and `grant_broker_secret_lister = false` (`docs/runbooks/custom-roles-to-bootstrap.md`).
9. **The first infra plan covers only the services and the registry.**
   - **What broke:** No image digests exist yet on a fresh project.
   - **Rule:** freshness is decided by Terraform state, never by reading the registry.
10. **CI was left without its `projectIamAdmin` grant for about a minute.**
    - **When:** 2026-09-28 (#275, fixed in #277).
    - **What broke:** The old grant was destroyed first, and the new one was then refused because `hasOnly()` held more than 10 entries.
    - **Rule:** lint every IAM condition (`iamPolicies:lintPolicy`), and create the new grant before destroying the old one. Known limits:
      - `hasOnly()` takes at most 10 entries;
      - no `matches()` on Cloud Storage;
      - at most 12 logical operators;
      - role descriptions are at most 300 characters;
      - Cloud Run cannot evaluate `resource.name` on jobs.
11. **A `-var` value never reached tfvars.**
    - **When:** 2026-10-16 (PR 1428).
    - **What broke:** The next plan from main would have destroyed what the value created.
    - **Rule:** write tfvars first, never `-var`, and read every plan for destroys.
12. **Wrong tool versions.**
    - **What broke:** Homebrew terraform 1.3.6 refuses the state; 1.16.2 is required. kubectl 1.22 and 1.25 silently drop manifest fields. Bootstrap fails with "Module not installed" without `init`.
    - **Rule:** pinned tool versions are checked in preflight.
13. **A laptop plan would have deleted the GitHub receiver.**
    - **When:** 2026-10-21 (#1878).
    - **What broke:** The state's `image_refs` had no digest for that image.
    - **Rule:** `plan.sh` refuses such a plan; the engine pins every image by digest.
14. **Adding a metric label failed every release.**
    - **When:** 2026-10-22.
    - **What broke:** A label added to a `google_logging_metric` forces its replacement, and GCP will not delete a metric that an alert uses.
    - **Rule:** add the label live first.
15. **Pool ceilings in tfvars are only birth values.**
    - **What broke:** Pool documents carry `ignore_changes`, so later tfvars edits do nothing.
    - **Rule:** the live values change only through `PUT /v1/admin/limits`; `doctor` reads the live value.

## C. IAP, front door, OAuth and identity

16. **An IAP admin grant scoped by resource name matched nothing.**
    - **When:** 2026-09-24.
    - **What broke:** IAP names backends by project number and numeric id, not by name. A binding on the backend did not grant admin either.
    - **Rule:** IAP settings need project-level rights. Check inherited bindings (folder, org, group) before telling anyone they lack a right.
17. **Manual steps needed once per project.**
    - **What broke:** None of these is in Terraform:
      - the OAuth brand (Internal; it needs an org and can never be deleted);
      - the IAP service agent (`gcloud beta services identity create`); without it the load balancer says "The IAP service account is not provisioned";
      - the DNS A record; the certificate stays in PROVISIONING for up to an hour.
    - **Rule:** each is a guided, checked stage.
18. **gcloud tokens refused at IAP.**
    - **When:** 2026-09-24.
    - **What broke:** A gcloud token gets 401 with code 900.
    - **Rule:** a Desktop OAuth client, allowlisted per backend as a programmatic client.
19. **IAM propagation takes time.**
    - **What broke:** IAP IAM took about 45 s; after the custom-role move, wait 7 minutes or more.
    - **Rule:** waits are polled, with a bound.
20. **Group lookups failed.**
    - **When:** 2026-09-20.
    - **What broke:** Service accounts cannot read Cloud Identity groups through GCP IAM. Delegation's `with_subject` works only with key files.
    - **Rule:** fixed with a keyless `signJwt`. A `signJwt` 403 is a GCP grant; `unauthorized_client` is the Admin console. Day One prefers the Workspace Groups Reader admin role (design §8, BGD 2026-10-29).
21. **Auth and routing traps.**
    - **When:** 2026-09-16 to 09-24.
    - **What broke:**
      - Cloud Run adds its own `Authorization` header; read `X-Serverless-Authorization` instead.
      - Two silent 404s on push paths.
      - Scripts that called the internal-ingress `*.run.app` address got 404.
      - The release smoke test ran outside the VPC with `API_AUDIENCE` unset.
    - **Rule:** verification runs inside the VPC (`swarm-verify`).

## D. CI identity, trust and the release pipeline

22. **Main's release pipeline had never completed.**
    - **When:** until 2026-09-24.
    - **What broke:** 44 of 45 runs failed. What ran was a laptop deploy.
    - **Rule:** the first deploy runs as the deployer identity, never as an owner (D20).
23. **The first release past IAP lacked `actAs`.**
    - **When:** 2026-09-24 (#35).
    - **What broke:**
      - The deployer lacked `actAs` on every runtime account.
      - An alert failed on "Cannot find metric": a labelled counter has no descriptor until its first sample.
      - An alert filter needs a `resource.type`.
      - The KMS data source returned a public key only for `versions[0]`.
    - **Rule:** apply as the deployer. Alerts are created only after their metric exists.
24. **A trust-policy pin blocked the deploy it was meant to protect.**
    - **When:** 2026-09-24.
    - **What broke:** An `assertion.sub` pin could not admit jobs gated by an environment.
    - **Rule:** print the real OIDC claims before writing the condition; pin `job_workflow_ref` and verify it (`scripts/verify-deployer-trust.sh`).
25. **A prod release moved the `:prod` tag before approval.**
    - **When:** 2026-09-24 (#52).
    - **What broke:** GitHub creates a missing environment unprotected.
    - **Rule:** one approval before promotion; the environments are checked, not assumed (CI add-on).
26. **A held `dev-iam` plan blocked every later release.**
    - **What broke:** One wait lasted 16 hours. On 2026-10-11, 20 releases were cancelled behind a held plan that IAM would have refused anyway.
    - **Rule:** IAM changes wait for a typed approval in `upgrade`, and the wait says what it blocks.
27. **Runner and CVE stalls.**
    - **What broke:**
      - Image builds queued on hosted runners until release timeouts (#2438).
      - CI depends on the separate `saga-ci-runners` project.
      - The trivy HIGH-CVE gate stopped all releases four times in October.
    - **Rule:** the installer says what is accepted and until when, and never skips a scan.
28. **Deployed-version traps.**
    - **What broke:**
      - A substring `tag:` filter promoted another workflow's build.
      - The service label `swarm-image-tag` is wrong on four services.
      - Traffic pinned to a revision made a later deploy serve 0% (2026-10-21).
    - **Rule:** read digests, never labels; route with `--to-latest`.

## E. GKE and Kubernetes

29. **Every browser agent ever accepted had failed.**
    - **When:** 2026-09-23/24 (`docs/incidents/2026-09-24-gke-dispatch.md`).
    - **What broke:** Seven or eight stacked causes, all returning the same 403:
      - namespace prefix mismatch;
      - the `apply.sh` guard;
      - a missing RBAC file;
      - an IAM condition that cannot authorise a namespaced request;
      - an RBAC subject named by email instead of the numeric uniqueId;
      - the Job's command override;
      - `SWARM_ARTIFACTS_DIR` missing on a read-only filesystem.
    - **Rule:** check in this order: namespace, then Role and RoleBinding, then the subject, then IAM. Bind by both email and uniqueId.
30. **The reconciler never released dead GKE leases.**
    - **When:** 2026-09-24.
    - **What broke:** It listed Jobs at cluster scope, which it was never granted, so it saw nothing.
    - **Rule:** namespaced reads; `doctor` checks that the reconciler can see Jobs.
31. **A new tenant cannot run anything until its namespace is created by hand.**
    - **When:** 2026-10-19 (#1590).
    - **What broke:** `APPLY_TENANT_NAMESPACES` was unset. One tenant also lacked its `swarm-spec-verify-keys` ConfigMap.
    - **Rule:** `team add` and releases apply namespaces and keys.
32. **A hand-applied namespace got the wrong quota.**
    - **When:** 2026-10-23 (#2039).
    - **What broke:** It took `render.py`'s default instead of the tenant record's.
    - **Rule:** the quota is rendered from the tenant record.
33. **Admission policy refused Jobs.**
    - **When:** 2026-10-20 (#1820).
    - **What broke:** The scheduler shipped new sizes before the policy allowed them. 54 to 56 dispatches failed, and nobody was alerted.
    - **Rule:** policies are applied before the code that depends on them.
34. **The Job-count quota filled with finished Jobs.**
    - **When:** 2026-10-28 (#2661).
    - **What broke:** Finished Jobs were kept for 3,600 s.
    - **Rule:** finished-Job TTL of 10 minutes; refusals park instead of failing (contract request 123).
35. **Cloud Run Jobs the scheduler created had no VPC access.**
    - **When:** 2026-10-21.
    - **What broke:** They could not reach the internal API.
    - **Rule:** `tenant_job_vpc_egress` (PR 1897).
36. **Direct VPC egress can delay the first connection.**
    - **When:** 2026-09-25.
    - **What broke:** The delay can last a minute or more.
    - **Rule:** workers retry for 180 s; the dispatch deadline is 480 s.
37. **Cluster access.**
    - **When:** 2026-09-18.
    - **What broke:** The laptop's kube context was the other team's cluster. The control plane is open to `0.0.0.0/0`.
    - **Rule:** context guards; `doctor` reports the authorised networks.

## F. Tenants, secrets and scheduled ticks

38. **The quota sweep had answered 403 on every tick since it was created.**
    - **When:** 2026-09-17.
    - **What broke:** `PLATFORM_SERVICE_ACCOUNTS` was unset, while Scheduler reported every tick as successful.
    - **Rule:** every tick's target must answer 2xx after deploy.
39. **A secret with no version blocked deploys.**
    - **When:** 2026-10-05/06.
    - **What broke:** A Cloud Run revision that names such a secret never starts.
    - **Rule:** key first, then the switch.
40. **New-tenant order.**
    - **What broke:** The tenant's worker SA, then the owner's bootstrap `deployer_admin` grant, must come before the release (#334). Re-running `register-tenant --providers` replaces credentials.
    - **Rule:** use `--add-provider`.
41. **All 21 alert policies notified nobody.**
    - **What broke:** `alert_emails = []` (#1817).
    - **Rule:** a notification channel is required at install.
42. **Secrets are scoped to the user.**
    - **When:** 2026-10-11.
    - **What broke:** Members of a group tenant share one service account (#1041).
    - **Rule:** recorded in the design's secret model.

## G. GitHub Apps and quota

43. **The operator's GitHub quota ran out.**
    - **When:** 2026-10-17.
    - **What broke:** Background ticks used a personal access token.
    - **Rule:** background reads use App installation tokens.
44. **Agents could not push workflow files.**
    - **When:** 2026-10-08 to 10-23 (#2046).
    - **What broke:** The `-git-wf` slot never had a version.
    - **Rule:** a per-repo App token behind a per-repo `workflow_files` switch. Changing App permissions needs the owner's acceptance on each installation.
45. **The wrong token-permission key.**
    - **When:** 2026-10-27 (PR 2538).
    - **What broke:** `permission-variables` does not exist.
    - **Rule:** use `permission-actions-variables`.
46. **The GitHub App and the merge App are once-per-deployment.**
    - **What broke:** The callback is derived from `frontend_hostname` and must match. An App on a personal account cannot use the merge queue.
    - **Rule:** the manifest flow creates the App with the right callback and permissions, including Commit statuses: Read (design §9.1).

## H. Running the installer as an agent

47. **The auto-mode permission check refused IAM and deletion commands.**
    - **When:** 2026-10-22 and 2026-10-28.
    - **What broke:** A hand `gcloud` IAM removal, `get-credentials` and a finished-Job cleanup were refused, despite verbal pre-approval.
    - **Rule:** `/sc:install` hands the person the exact command, or asks for a narrow allow rule. It never works around a refusal.
48. **IAM conditions can only be tested by releasing.**
    - **What broke:** The Policy Troubleshooter API is disabled and the deployer cannot be impersonated, so the only real test of a condition was the release job.
    - **Rule:** enable both in a fresh project; `doctor` uses the Troubleshooter to tell a missing grant from slow propagation.
