# Where the gates run — CI is the gate, this machine is not

**This repository is authored locally and gated remotely.** Tests run in GitHub
Actions. Builds run in GitHub Actions. Deployments run in GitHub Actions. On a
workstation you write code, commit it, push it and open a pull request; then you
read the run.

That is a decision, not a description of tooling, and it is worth writing down
because every doc in this directory used to say the opposite. The owner has now
said it four times, the fourth being: *"why are you running tests in here!???
Make it so that tests run in CI, builds run in CI, deployments run in CI. Here
we only author code, create PRs."* `CLAUDE.md`'s
[Before you say you are finished](../CLAUDE.md#before-you-say-you-are-finished)
is the binding statement; this file is the map of what the remote side actually
covers, so that "wait for CI" is a specific thing to wait for rather than a
shrug.

## Why, and not just what

Three reasons, in the order they cost the most:

1. **A local exit code proves something about one laptop.** This machine has
   `kubectl` 1.22 and 1.25 ahead of the working client on `$PATH` — an old
   client silently drops manifest fields — and a Homebrew checkov 3.3.10 that
   raises on import and shadows the 3.3.17 the checks need. `common.sh` carries
   `kubectl_bin` and `prefer_local_bin` precisely because of those two. The
   runner is a known image (the `bogdan-ci` VM image, or GitHub-hosted
   `ubuntu-24.04`; see [Where jobs run](#where-jobs-run)) with a pinned tool
   version per job; the laptop is whatever it has drifted into since the last `brew upgrade`.
2. **A gate that runs in one place can be believed by everyone.** A mutation
   proven in CI is proven for every future reader of the branch. A mutation
   proven on a laptop is proven once, to one person, and the proof is a sentence
   in a report.
3. **A local full gate blocks the machine that is supposed to be writing.**
   `make test` is the whole offline suite; running it serialises against every
   other lane working in the same checkout.

## The workflows, and what each one is responsible for

| workflow | runs on | jobs |
|---|---|---|
| `application.yml` | push to `main`; **every** pull request (no path filter since 2026-10-07: its `ci-gate` job is the required check). Its `what this change reaches` job skips every other job unless the change touches `apps/`, `images/`, `kubernetes/`, `scripts/`, `tests/`, `docs/`, `Makefile`, `pyproject.toml`, `uv.lock`, `README.md`, `CLAUDE.md`, `CONTRACT.md`, `release.yml`, `accept.yml`, `iam-refusal-probe.yml`, `ci-fix.yml`, `auto-merge.yml`, `.dockerignore`, `.gcloudignore` or the workflow itself ([below](#the-merge-pipelines-wall-time)) | `what this change reaches` · `shellcheck` · `release workflow wiring (actionlint)` (also lints `accept.yml`, `iam-refusal-probe.yml`, `ci-fix.yml` and `auto-merge.yml`) · `ruff check` ([below](#ruff-lint-is-the-house-python-linter-and-formatting-is-not-adopted)) · `format / unit tests` (stands for `lock / contract / format / bridge install` and the four `unit tests (k/4)` shards) · `swarm-ui typecheck / component tests` (stands for `swarm-ui typecheck / build` and the four `swarm-ui component tests (k/4)` shards) · `integration tests (emulator)` · `kubernetes manifests` · `build images` (**push to `main` only** — the one build of each commit) · `build changed images without pushing` (**pull requests only** — [below](#images-are-built-on-a-pull-request-without-pushing)) · `record the tested tree` (every run but main's: what it passed, keyed by its tree, which main's push run reuses — [below](#main-reuses-the-tested-tree)) · `ci-gate` — `needs` every job above but the two name-keepers `format / unit tests` and `swarm-ui typecheck / component tests` (whose own `needs` it holds; [below](#where-pull-request-ci-time-goes-2026-10-09)) and passes only when each passed or was skipped by its own `if:`, then waits for this commit's `terraform.yml` run ([below](#the-ruleset-on-main-and-ci-gate)) |
| `terraform.yml` | push to `main`; pull requests touching `terraform/`, `tests/terraform/`, the plan guard, the destroy guard, the unlabelable-type list or the workflow itself | `fmt / validate / tflint` · `terraform test` · `checkov` · `plan` (**not** on a pull request) · `plan (not run on a pull request)` |
| `security.yml` | every pull request; push to `main`; Mondays 06:00 UTC | `trivy (repo)` · `secret scan` · `checkov (terraform + kubernetes)` · `platform policy assertions` · `trivy (published images)` (schedule / dispatch only) |
| `release.yml` | **every** push to `main` (no path filter since 2026-10-26: only the newest commit is released, and a release whose commit `main` has moved past ends green, superseded — [below](#only-the-newest-commit-on-main-is-built-and-released)); or manual dispatch with an environment | `verify` (**dispatch only** — on a push CI proved the same SHA, [below](#the-release-timeline)) · `images and scan` (reuses `application.yml`'s build of the commit; moves nothing) · `approval` (the one job naming `dev` or `prod` — prod waits here) · `promote` · `terraform apply` · `terraform apply, IAM (dev-iam)` (dev only, and only when the plan changes IAM — the owner approves it [below](#a-dev-release-that-changes-iam-waits-for-the-owner)) · `deploy` (verifies the digests; **on prod** also warms, smokes and proves GKE) — the apply and deploy jobs only after `approval` succeeded, and on prod only in the attempt it succeeded in · `prod approval is from an earlier attempt` (runs only on a partial re-run of prod, and fails it) |
| `accept.yml` | `release` **completing** on `main` (`workflow_run`), or manual dispatch on `main` (scope `all`); accepting jobs in `accept-dev-<job>` groups, cancel-in-progress ([below](#the-release-timeline)). Also **every three hours** (`schedule`) and on a dispatch with scope `live-browser`, for the live-browser group alone ([below](#live-browser-runs-on-a-schedule)) | `what dev runs` (the SHA in `releases/dev/applied.json`) · `warm, smoke and GKE proof (dev)` · `put the deployed commit's fixtures on the sandbox` · `acceptance (<group>)` × 6, in two waves · `acceptance (tenant-isolation)` (the cross-tenant Firestore probe, beside wave 2; report-only until the repository variable `ISO_PROBE_REQUIRED` is `true`, [below](#the-cross-tenant-firestore-probe-is-report-only-until-the-cutover)) · `read back and sweep the sandbox` · `report what was accepted` (a red run opens or updates one `bug` issue). On the schedule: `acceptance (live-browser, scheduled)` · `report the scheduled live-browser group` (its own `bug` issue) — dev only, as its own identity, never the deployer |
| `hotfix.yml` | push to `main` touching `apps/`, `images/`, `terraform/`, `kubernetes/`, `scripts/`, `.trivyignore.yaml` or a release workflow or action (the paths `release.yml` filtered on until 2026-10-26); or manual dispatch on `main`. Acts only for a pull request merged with the `hotfix` label ([below](#hotfix-releases)) | `is this a hotfix` (reads the label; no Google identity) · `images and scan (dev, hotfix)` · `promote (dev, hotfix)` · `terraform apply (dev, hotfix)` (fails on a plan that changes IAM) · `deploy and smoke (dev, hotfix)` — dev only, concurrency group `hotfix-dev`, release.yml's own stages from `.github/actions/release-*` |
| `ci-fix.yml` | `application` **completing red on a `swarm/<task-id>` branch** of this repository (`workflow_run`, so only as the file is on `main`) ([below](#the-ci-fixer)) | `fix a red SwarmCloud pull request` |
| `auto-merge.yml` | `pull_request_target` when a label is added (acts only on `ready`) and when a pull request is merged; `workflow_run: completed` of every pull-request workflow, and the `workflow_dispatch` that sends ([below](#a-ready-pull-request-is-merged-by-github-not-by-a-session)) | `queue for auto-merge` (refuses a `[swarm] task_` title, an unprotected base branch, a failing check or a missing merge App, with a comment; waits while a check still runs; otherwise enables native squash auto-merge under the PR's title); `re-evaluate ready pull requests when a run finishes` dispatches it again for each `ready` pull request at a finished run's head ([below](#a-ready-label-that-lands-while-checks-run-is-re-evaluated), #697); on `closed`, `close the merged pull request's issues` closes the open issues a merge's closing keywords name ([below](#a-merge-closes-the-issues-its-keywords-name), #621). **To be retired** once the workflow `merge` step is proven (owner, 2026-10-04) |
| `iam-refusal-probe.yml` | **manual dispatch on `main` only**, by the owner, once ([below](#the-deployers-refusal-is-proven-once-by-a-probe-the-owner-dispatches)) — never on a push, a pull request or a schedule | `deployer is refused an unlisted role` |
| `publish-release.yml` | **manual dispatch on `main` only**, with a release name and a main commit whose `release` run succeeded (BGD 2026-10-30, "Move publishing into CI now"; [runbooks/release.md](runbooks/release.md)) — never on a push, a pull request or a schedule. `publish` waits in the GitHub environment `release` (a required reviewer) and authenticates as the deployer, which is why the file is on `wif.tf`'s `deployer_workflows`; in `swarmcloud-prod` that identity holds only the three roles `scripts/release-home.sh --ci-publisher` grants. It publishes; it never promotes | `check the version and the commit` · `publish <version> to the release home` |

Three details in that table are easy to misread and each has bitten someone:

* **`application.yml`'s jobs run on a docs-only pull request** — `docs/**` is
  in its `changes` job's list — and its `shellcheck` job is where documentation links are
  resolved (`scripts/lib/check-doc-links.sh`). A broken relative link or a
  broken heading anchor fails that job. Anchors are checked, not just files,
  because GitHub silently lands a reader at the top of the page when an anchor
  misses, so a wrong link looks like it worked.
* **`terraform.yml` does *not* fire on a docs-only pull request**, because its
  path filter names no documentation. A terraform doc edit that needs the
  terraform checks needs a terraform file in the same pull request, or a manual
  dispatch.
* **`security.yml` has no path filter at all**, so it runs on every pull
  request. It is the one workflow that cannot be avoided by touching the wrong
  directory, which is deliberate for a security gate.

## Where jobs run

Owner decision, 2026-10-14: *"make sure we use CI runners for all jobs moving
forward if capacity allows"*. GitHub-hosted `ubuntu-24.04` had saturated (76
jobs queued at 13:05Z) while the self-hosted pool had room. That pool is
ephemeral GCE VMs with the `bogdan-ci` label (e2-standard-8, one VM per job,
image built by sagaxyz/ai-studio `deploy/ci-runners/vm/provision-image.sh`).

**Every job runs on the pool except one class: jobs that hold a Google cloud
identity through workload identity** (`id-token: write` with
`google-github-actions/auth`). Those are the release, hotfix and acceptance
jobs that deploy or touch the cloud, `build images`, `terraform plan`, the
IAM refusal probe, the CI fixer and the scheduled image scan. They stay
GitHub-hosted, the rule PR 1184 set for release.yml.

Every other job spells `runs-on` as one guarded expression:

```
${{ (vars.SELF_HOSTED_CI == 'off' || ((github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/main') || ((github.event_name == 'pull_request' || github.event_name == 'pull_request_target') && github.event.pull_request.head.repo.full_name != github.repository)) && 'ubuntu-24.04' || fromJSON('["self-hosted","linux","x64","bogdan-ci"]') }}
```

* **Main's own runs go first, on GitHub-hosted runners** (owner decision
  2026-10-20). A push to `main` or a dispatch on `main` (main's CI run, every
  release and hotfix) resolves to `ubuntu-24.04`, so it never queues behind
  lane pull requests on the pool. On 2026-10-20 two releases failed after
  waiting 45 minutes for an image build whose CI run sat queued behind ~20
  lane PRs on 8 VMs. The repository is public, so hosted minutes are free.
  Pull requests, lane-branch pushes (`swarm/**`) and merge-queue entries stay
  on the pool, as the 2026-10-14 decision asks.
* **A fork's pull request never reaches the VMs** (owner decision 2026-10-12).
  The repository is public, and a returning contributor's fork PR runs
  without approval.
**The short form, for three workflows no fork can start.** accept.yml,
auto-merge.yml and merge-train.yml (schedule, `workflow_run` of a `train/`
branch, dispatch; no `pull_request`) spell `runs-on` without the fork clause:

```
${{ vars.SELF_HOSTED_CI == 'off' && 'ubuntu-24.04' || fromJSON('["self-hosted","linux","x64","bogdan-ci"]') }}
```

The long form's text cannot sit in those files: test_auto_merge_workflow.py
forbids `pull_request.head.repo` in auto-merge.yml (the workflow must never
touch a pull request's code, so it must not even read the fork's repository),
and test_acceptance_target.py forbids `github.repository` anywhere in
accept.yml (it must not name the public repository). The clause is dead weight
there anyway, because of the triggers:

* accept.yml starts from `workflow_run` (a completed release on main) and
  `workflow_dispatch` only, never from a pull request.
* auto-merge.yml runs on `pull_request_target` (the base branch's own workflow
  file, with no checkout of the pull request), `workflow_run`, `check_suite`
  and dispatch. It has no `pull_request` trigger, the only event that runs a
  fork's copy of a workflow file.

Every other workflow can be started by a fork's `pull_request`, so it keeps the
long form. The short form is accepted only for a file listed in
`SHORT_FORM_WORKFLOWS` in the test, and only while that file's `on:` has no
`pull_request`: adding one makes the test fail, which is the reminder to put
the fork clause back.

* **The capacity escape hatch.** Set the repo variable `SELF_HOSTED_CI` to
  `off` (`gh variable set SELF_HOSTED_CI --body off`) and every guarded job
  goes back to `ubuntu-24.04`. Use it when the pool is down or out of quota.
  Its default is `on`, and an unset variable also means the pool.

**Secrets on the VMs: the residual risk, accepted.** Two kinds of job hold a
GitHub-writing secret (environment `repo-secrets`) and run on the pool all the same:

* auto-merge.yml `enable` and `disable`, and merge-train.yml `tick`, hold the
  merge App's private key (`secrets.MERGE_APP_PRIVATE_KEY`, with
  `vars.MERGE_APP_ID`);
* accept.yml `sandbox` and `sandbox-results` hold `SWARM_SANDBOX_GITHUB_TOKEN`.

While such a job runs, its secret is in the VM's memory and on its disk.
Anyone with IAP SSH (the VMs' one ingress rule) or disk-snapshot rights in
saga-ci-runners could read it there. The VM is single-use and is deleted
afterwards, but that window is real. The owner accepted it on 2026-10-14. No
other secret may go on a guarded job.

`tests/unit/scripts/test_fork_prs_off_self_hosted.py` holds all of this:

* every job is either on the guarded expression or listed as hosted, with a
  reason the test checks against the job;
* no guarded job has `id-token: write`;
* no guarded job holds a secret beyond its `GUARDED_SECRETS` entry;
* the expression is evaluated for push, same-repository and fork pull
  requests, with the hatch on and off;
* the short form is accepted only for the `SHORT_FORM_WORKFLOWS` files, whose
  triggers it reads from the workflow itself.

A new job that silently uses a hosted image fails CI, and so does one that
puts a credential on the pool.

What the VM image differs in: `/usr/local/bin` is not writable by the
runner user, so a job installs a tool into `$RUNNER_TEMP` (security.yml does
this for trivy, which is installed by an explicit pinned step rather than
aquasecurity/trivy-action: that action restores its binary from a cache at
`/home/runner/.local/bin`, which the VM does not have, so the scan found no
`trivy`; and the shell job installs kubectl with azure/setup-kubectl, since
the VM has none). It has `gh`, `unzip`, docker with buildx, `jq` and
`shellcheck`, passwordless sudo, and a writable tool cache. Anything else
comes from a setup action.

### Sub-minute checks share a job

**Owner approval, 2026-10-21 (#1799, FIX 2 of the ci-runners capacity
review).** In the 24 h from 2026-10-20 06:00 to 2026-10-21 06:00 UTC, 3,313 of
7,341 self-hosted VM jobs (45%) did under 60 s of work, 18.4 h in all. Each
one paid a VM create and boot (about 52 s created-to-started at best) and took
a slot under the pool's cap, so the boot cost more than the check did. The most
frequent were `what this change reaches`, `platform policy assertions`,
`secret scan`, the two name-keepers, `ruff check`, `release workflow wiring
(actionlint)`, `ci-gate`, `kubernetes manifests` and `trivy (repo)`.

So the sub-minute checks that share a setup now run as steps of one job per
workflow:

| workflow | folded job | the checks it runs as steps |
|---|---|---|
| `application.yml` | `quick-checks` | `release workflow wiring (actionlint)` · `ruff check` · `kubernetes manifests` · `lock / contract / format / bridge install` |
| `security.yml` | `checks` | `secret scan` · `platform policy assertions` |
| `terraform.yml` | `offline` | `fmt / validate / tflint` · `terraform test` |

Three jobs that run no check of their own move to `ubuntu-24.04` without
folding: `format / unit tests` and `swarm-ui typecheck / component tests`
read `needs` and nothing else, and `plan (not run on a pull request)` writes a
fixed summary.

**Self-hosted VMs a same-repository pull request run starts**, counting each
matrix once (the four unit and four vitest shards are untouched and add the
same before and after):

| workflow | before | after |
|---|---:|---:|
| `application.yml` | 15 | 10 |
| `security.yml` | 4 | 3 |
| `terraform.yml` | 4 | 2 |
| **all three** | **23** | **15** |

That is 8 fewer VMs a run when the change reaches terraform.yml, and 6 fewer
when it does not (terraform.yml is path-filtered).

**Every check still fails the run on its own.** Each folded step is named
`<the old job's name> · <what it does>`, so a reader looking for `ruff check`
finds `ruff check · ruff check`. Every one runs `!cancelled()`, so one check
failing never hides another's answer, and a failed step fails the folded job.
Its last step, `verdict` (`always()`), reads `toJSON(steps)` and gives each
check one result by step id: `success` only when every shared `setup-*` step
succeeded, none of the check's own `<old id>-*` steps failed or was cancelled,
and at least one of them ran; `skipped` when none ran; `failure` otherwise.

**The name-keeper rule.** Every old job id stays, as a name-keeper: a job on
GitHub-hosted `ubuntu-24.04` (no VM) that needs the folded job, runs
`always()`, and passes only when the folded job's verdict for its check is
exactly `success`. An empty verdict (the folded job died, was cancelled or
never ran) fails it. A name-keeper never passes when its folded step failed,
and a skip would read as a pass, which is why it is `always()` and never
inherits the default `success()`. The ids and names stay because something
reads each one:

* `secret scan` and `platform policy assertions` are required by ruleset
  `main-protection` (24160219), as is `trivy (repo)`, which stays a job of its
  own (below). Its five contexts were read from
  this repository (`REQUIRED_CONTEXTS` in
  [`test_merge_queue_workflows.py`](../tests/unit/scripts/test_merge_queue_workflows.py),
  the ruleset read of 2026-10-01), not live;
* `workflows`, `ruff`, `manifests` and `python-checks` are what
  `scripts/lib/ci_reuse.py` reuses, records and gates by (CI-TREE-REUSE,
  #1798). Each keeps its old `if:` after `always() &&`, and the folded step
  carries the same condition, so a check a run of the identical tree already
  passed is skipped in both places. `build` needs `manifests`, and ci-gate
  needs all four and `quick-checks`;
* `validate` is what main's `plan` needs, and ci-gate fails on any failed job
  of a terraform.yml run.

**What did not fold, and why.** `trivy (repo)` is sub-minute too, but it
uploads SARIF, so it is the one security.yml job that holds
`security-events: write`. Folded into `checks`, that permission would reach the
`secret scan` and `platform policy assertions` steps, which run repository
scripts a lane's pull request can edit (`scripts/destroy.sh --self-test`).
Owner decision 2026-10-21 on PR 1813: one VM a run buys least privilege, and
`checks` holds `contents: read` only. `what this change reaches` decides what every
other job runs, so nothing can start until it ends; checks folded into it
would hold the shards back. `ci-gate` and `record the tested tree` check the
repository out and run scripts (ci-gate also waits for terraform.yml), which
is more than reading `needs`, so they stay on the pool. Both `checkov` jobs
were not among the sub-minute ones and share none of the folded setup.
`shellcheck`, the shards, `swarm-ui typecheck / build`, the integration tests
and the image build are the heavy jobs this change leaves alone, and a fork's
pull request still runs everything on GitHub-hosted runners.
[`test_ci_fold.py`](../tests/unit/scripts/test_ci_fold.py) holds the counts
above, every check name, each name-keeper's runner and verdict, and runs the
verdict step against faked step outcomes.

### CI falls back to GitHub-hosted runners on its own

Measured 2026-10-17 (part of #1483): runner-dispatcher registers the pool's
runners as the owner's GitHub user. When that user's quota ran out
(`generate-jitconfig` 403 from 02:00 UTC) its sweep read
`per_target: {swarmcloud: 0}`, and every pull request's checks stayed queued
for hours until someone set the escape hatch by hand. The hatch is now set by
`.github/workflows/runner-fallback.yml`, every 10 minutes and on dispatch:

* **Off.** A job of another workflow that asked for `bogdan-ci` has been
  queued more than 10 minutes, and no `bogdan-ci` job is running or started
  in that time: the judge sets `SELF_HOSTED_CI=off`, then cancels every run
  still queued on the pool and re-runs its cancelled jobs, so they land on
  `ubuntu-24.04`. **A run with any job in progress is never cancelled**
  (cancelling a run cancels all of it, and a release, accept or hotfix run
  can have a hosted deploy running while a later job waits for the pool): it
  is a "held: run N has a job in progress" summary line and is reconsidered
  next tick. One run that cannot be cancelled or re-run (finished meanwhile,
  needs fork approval, too old; `rerun-failed-jobs` answering 403, 409 or
  422 is "nothing to re-run") is a summary line naming the run and the
  status, and the loop goes on. No new run is started after 10 minutes, so
  the job ends inside its 15-minute timeout; the summary says how many were
  left for the next tick. A run is re-run once: one whose latest attempt
  `github-actions[bot]` started is not touched again. A job queued on a
  hosted label never counts, because hosted saturation is why the pool
  exists.
* **On again.** While the hatch is off nothing asks the pool for a runner,
  and the dispatcher starts one VM per queued job, so an empty runner list
  says nothing. So the workflow's `probe` job, the one job that targets
  `bogdan-ci` directly, runs only while the hatch is off. When every probe
  since the flip has been picked up for at least 15 minutes, two probes or
  more, the judge sets the variable back to `on`. It writes `on`, the
  default, rather than deleting it, because the variable's `updated_at` is
  the hysteresis clock. A probe still queued after 10 minutes breaks the
  streak, and the judge cancels its run.
* **Hysteresis.** The variable never changes more than once per 30 minutes.
  A flip that is due but held is logged too.
* **The thresholds** are `QUEUED_MINUTES`, `RECOVER_MINUTES` and
  `HYSTERESIS_MINUTES` in the workflow's `env`.
  `tests/unit/scripts/test_runner_fallback_workflow.py` pins them and runs
  the judge's own decision function against fake GitHub answers.

Every flip, held flip and re-run is a line in the run's job summary.

**Calls per tick.** The reads are ordered by cost. The judge reads the
variable and the queued runs first (2 calls, plus one per extra 100 runs, up
to 5 pages). With the hatch on and no queued run older than 10 minutes, which
is the steady state, the tick ends there: **2 calls**, no job listed. The
price of that is that a pool job queued inside a run that is already in
progress is only seen once some queued run is older than 10 minutes. In an
outage (hatch off, or a queued run older than 10 minutes) it also lists the
in-progress runs (1 call) and the jobs of every active run (1 call each),
and while off the probe runs of the last 2 hours and their jobs (1 + up to
30 calls); each re-run is then about 3 calls plus up to 24 status polls. All
of it is the workflow's GITHUB_TOKEN, not a person's quota.

**The judge never runs on the pool it judges.** It is pinned to
`ubuntu-24.04` and does not follow the hatch, because a judge queued behind
the outage could never report it. Hosted minutes are free on a public
repository.

**Two tokens.** The GITHUB_TOKEN carries `actions: write` and nothing else:
it reads runs and jobs, cancels and re-runs. It has no `variables` scope, so
the variable is read and written with the merge App's installation token,
minted with `permission-actions-variables: write` only (GitHub's API key for
repository variables is `actions_variables`; its settings page calls it
*Variables*). That token draws on the App's
quota, not a person's. **Owner step, once:** give the merge App the
repository permission *Variables: Read and write*
([runbook](runbooks/merge-app.md)). Until then the token mint fails and the
run is red. A red run is how a missing permission shows itself, and the hatch
can still be set by hand.

**A hand-set `off` does not stick.** The judge reverts it once the probes
show 15 minutes of service. To pin the hatch, set the repository variable
`RUNNER_FALLBACK` to `manual`: the judge still reads and writes its summary,
saying what it *would* do, but never flips `SELF_HOSTED_CI`, never cancels a
run and never re-runs one. Unset, or any other value, is automatic. The
reason it exists: an operator holding CI on hosted runners on purpose, for
example while the dispatcher is being replaced, must not be flipped back
because the old pool happens to answer a probe. Delete the variable (or set
it to `auto`) to hand the hatch back to the judge. Disabling the workflow
(`gh workflow disable runner-fallback.yml`) also stops it, but loses the
summary.

## Why terraform does not plan on a pull request

This is the single most likely thing to be "fixed" by someone who should not.

`terraform plan` needs a real cloud token. GitHub's OIDC claim for a
`pull_request` run is `refs/pull/<n>/merge`. The workload identity pool is
pinned to `refs/heads/main` — in `terraform/bootstrap/wif.tf`, and the pin is
stated in two independent places on purpose. So no token is minted on a pull
request and no plan is possible there.

**The pin is the security boundary, not an accident of configuration.** The
deployer service account holds `roles/resourcemanager.projectIamAdmin` on a
*shared* project, plus `roles/secretmanager.admin`, `roles/datastore.owner` and
`roles/storage.admin` — enough to read every tenant's provider key, read and
write the whole swarm Firestore database, and self-escalate to owner. Anyone can
open a pull request. A pull request that could mint that token is a pull request
that could grant itself anything.

`github_allowed_refs` **must never include `refs/pull/*`.** That sentence is
what `application.yml`'s `build` job spends twenty lines forbidding, because
the job authenticates as the deployer and then runs `scripts/build-images.sh`
from its own checkout, and that script executes any checked-in
`images/<t>/cloudbuild.yaml` or `apps/<t>/cloudbuild.yaml` as a Cloud Build
config — so whoever wrote the checkout chooses what runs. On a pull request the
attribute condition rejects the ref and no token is minted, and that pin is the
whole of the control: the `build-pr` environment the job names was measured on
2026-09-24 with **no protection rules**, so it adds no human in the loop. The
job is therefore skipped on a pull request rather than left permanently red,
because a permanently red check is standing pressure to widen the pin to "fix
CI".

Because a skipped job is invisible in the checks list, `terraform.yml` ships a
`plan (not run on a pull request)` job that is deliberately **not** a no-op: it
writes into the run summary what was checked offline (`fmt`, `validate`,
`tflint`, `checkov`, and the `terraform test` suite against a mock provider),
what was not (what the plan would do to the live project), and where the real
plan happens. Silence in this repository has meant "did not run" far more often
than "nothing to report", so a bound check says what it did not cover.

### Where a plan is read, and why it is never a pull-request comment

The `plan` job writes the plan into **the run summary** and uploads the text
plan as the `tfplan-text-<env>` **artifact**. That is all. There is no comment
on a pull request, because there is no pull-request plan to post.

Until 2026-09-24 the job carried a "comment on the PR" step gated
`github.event_name == 'pull_request'`, inside a job gated
`github.event_name != 'pull_request'`. Each condition is right on its own; together
they mean the step never ran on any event, and a skipped step is green, so it
read as a working feature. It was also the only reason the workflow granted
`pull-requests: write`, so every run carried a write token nothing used. The step
and the permission are both gone.

[`tests/unit/scripts/test_workflow_step_reachability.py`](../tests/unit/scripts/test_workflow_step_reachability.py)
now computes, for every workflow, the events each job and each step can run on,
and fails on a step its own job can never reach. It models only
`github.event_name` comparisons and treats anything else as "any event", so it
can miss a dead step but cannot invent one. If a plan ever needs to reach a pull
request, the answer is not a step in this job. This job cannot run on a pull
request, and the reason is the security boundary described above.

## What a pull request therefore does not tell you

Stated plainly, because a green pull request is the thing most likely to be
over-read:

* **Nothing about the live project.** No plan, no apply, no deployed state.
* **Nothing about an image being pushed or deployed**, and — until the owner
  creates the pull-request build identity — **nothing about an image building
  either**. Once it exists, a pull request that changes an image's inputs builds
  that image, self-tests included, without pushing it
  ([below](#images-are-built-on-a-pull-request-without-pushing)); the build a
  release ships is still the one `build images` makes on `main`.
* **Nothing a browser would see.** The UI job is a typecheck, Vitest in jsdom,
  `node:test`, and the production build (`npm run build`); jsdom has no layout
  engine, so overlap, overflow, wrapping and contrast are invisible to it. Every
  one of those defects this repository has found was found in a real browser
  and none of them turned a check red. The build proves the bundle compiles on
  the image's Node; the `swarm-ui` image's own build is
  `build changed images without pushing` on a pull request that changes it, and
  `build images` on `main`.
* **Nothing about the seams against a real deployment.** Smoke, concurrency,
  race, failure and e2e run through `scripts/verify-remote.sh` inside the VPC,
  because `swarm-api` ingress refuses a laptop. They are not pull-request
  checks.

`terraform test` runs against a *mock* provider, so it proves the configuration
says what was meant — never that GCP would accept it.

## Images are built once per commit, and the release reuses them

**Owner decision, 2026-09-24.** `application.yml`'s `build images` job builds
every image once per commit on `main` and records what it built;
`release.yml` waits for that job for the same commit and promotes exactly
those digests. The release submits no Cloud Build of its own on a push.

**Why.** Every push to `main` used to build all eight images twice —
`application.yml`'s job (tagged `pr-<run>-<sha>`) and the release's own build
(tagged `<sha>`) — competing for Cloud Build in `saga-agents-staging`, whose
build quota is shared with another team, while the release waited behind the
duplicate. Measured on commit `94ee043`: `application.yml` built it
16:33:09–16:41:51 (run 36027980122), and release 36027980448 built the same
commit again 16:39:24–16:48:17 before promoting anything. The two builds also
produced two digests per image for one commit, which is how `:dev` once ended
up holding five images from one build and three from the other (the exact-tag
matching in `build-images.sh` and `push-images.sh` is the scar).

**How it fits together.**

1. `application.yml`'s `build images` runs `scripts/build-images.sh` with no
   `--tag`: the tag is the commit's (`git_sha`), derived in one place. It
   uploads `build/images-dev.json` — each image's digest, the tag, the full
   commit and the environment — as the artifact `images-dev`, kept 30 days.
2. `release.yml`'s `images and scan` job runs
   `scripts/build-images.sh --reuse-ci only` on a push. That calls
   `scripts/lib/ci-built-images.sh`, which finds `application.yml`'s run for
   the same commit on `main`, **waits** while the build is queued or running
   (up to 45 minutes), downloads the record, and refuses it unless it names
   this commit and this environment. On a push the job first asks whether
   `main` has moved past the commit, and asks again after the wait; if it
   has, the release ends green, superseded, and promotes nothing
   ([below](#only-the-newest-commit-on-main-is-built-and-released)).
3. The same job runs `scripts/push-images.sh --manifest
   build/images-dev.json --scan-only`: every recorded digest confirmed in
   Artifact Registry and trivy-scanned, one refusal failing the lot, and **no
   channel tag moved**. It uploads the record it scanned.
4. After the `approval` job — see
   [the next section](#a-prod-release-waits-for-approval-before-anything-prod-facing)
   — the `promote` job fetches that record and runs
   `push-images.sh --manifest build/images-dev.json --scan`, which scans again
   and only then moves the channel tags — all or nothing, with the put-back
   and the `MIXED` report exactly as before. The tag `:<sha>` is not read at
   all, so a later build of the same commit cannot change what is promoted.
   Once every channel tag has moved it marks each promoted digest
   `promoted-<channel>-<sha>`, a tag that never moves and that `deploy.sh`
   reads to tell an earlier release's digest from one nobody released. It
   starts with `promoted-`, not with the channel, because Artifact Registry's
   cleanup policies match tags by prefix: a rule keeping `:dev` forever must
   not also keep every mark forever (#1886). Digests promoted before that
   carry the old `<channel>-promoted-<sha>`; `deploy.sh` reads both until the
   operator has run `scripts/registry-remark.sh --apply` once, which adds the
   new form beside each old one and removes nothing.
5. The apply pins those digests and the deploy verifies them, unchanged.

**Every promote records its runner images' sizes** (#625). Right after the
channel moves, `release-promote` runs `scripts/image-sizes.sh --manifest
build/deployed-images-<env>.json` and appends its table to the job summary:
for each runner image the promotion wrote (`agent-runtime-*`), the promoted
digest, its compressed size (the linux/amd64 layer sum a node pulls) and its
delta from the newest earlier release of that image in Artifact Registry; a
first release reads "no earlier release", never `+0.0`. It only lists and GETs
the registry, which the deployer already reads in the promote step itself, so
it needs no new role. It **cannot fail or hold the release**: the step is
`continue-on-error` and bounded by `timeout 300`, and an image it cannot
measure is a row saying so and a failed step, nothing more. It exists because
#625's start-time question needed every release's size, and until this step
no release recorded its own, so the 2026-10-05 bisect had to read each one
back out of the registry.
`tests/unit/scripts/test_release_size_record.py` holds the step's place and
flags; `test_image_sizes.py` holds the table.

**What the release does when it cannot reuse.** Each ends in a red job whose
last line (and a run annotation) names the reason and links the job:

| `application.yml`'s build of the commit | push to `main` | dispatched by hand |
|---|---|---|
| succeeded, recorded for this environment | reused | reused |
| still queued or running | waited for (45 min cap) | waited for |
| **failed** | fails: "re-run that job if it was a flake" | fails the same way — rebuilding would repeat the failure behind a second Cloud Build |
| cancelled by a newer push to `main` before it built (the run `cancelled`, its build job cancelled, skipped or never started) | **superseded**: green, nothing promoted — the newer commit's release ships this change | — (a dispatch ships main's head) |
| cancelled otherwise, skipped, never ran, or recorded for another environment | fails: "re-run CI's run, then this release" — a dispatch would release main's head, not this commit | **builds it here**, through the same script and tag |
| GitHub API unreadable | fails — an unreadable API is never read as "never built" | fails |

"Recorded for another environment" is every **prod** release:
`application.yml` builds for dev, and swarm-ui bakes its environment into the
bundle when it is compiled (`VITE_SWARM_ENV`), so a dev build is not a prod
build. A prod release is always dispatched, so it builds — and the build is
`build-images.sh`, the same path, not a second copy of it.

### Only the newest commit on `main` is built and released

**Owner decision, BGD, 2026-10-26 (#2438): "Release only the newest".** On
`main`, every `application.yml` run is in one concurrency group,
`application-refs/heads/main`, with `cancel-in-progress: true`: a newer push
cancels an older run that has not finished, queued or running. `release.yml`
runs on every push to `main` and ships its commit only while that commit is
still `main`'s newest. Pull requests and lane branches are unchanged: their
group is the ref, and a newer push cancels the older run.

**Why: the 2026-10-26 stall.** Nothing deployed from 2026-10-25 23:32Z
(release 40245218405, the last success) to past 08:00Z the next day. Until
then each commit on `main` had a group of its own and its run was never
cancelled (the 2026-09-24 rule below), so every merge added a full run of
about 25 jobs to the GitHub-hosted queue that main's push jobs use (PR 1720).
Read at 05:24Z: 71 workflow runs queued, 27 of them `application` push runs
on `main`, the oldest queued since 01:40Z. The release waited 2700 s for its
own commit's `build images` job, which had not reached a runner, and failed.
Of the last 30 `release.yml` runs on `main` at 05:24Z, 25 were cancelled while
pending, 3 had failed and 2 were pending; the 4 failures since 00:23Z
(40249148453, 40254486207, 40255378252, 40263856035) all read `timed out
after 2700s waiting for application.yml's 'build images' job`. Between 27 and
78 `application` runs on `main` were queued during the stall, and 34 merges
were waiting to deploy. Merges every few minutes meant no release ever
completed.

**Why cancelling is safe now.** `main` is linear, so a newer commit contains
every older one, and a cancelled commit's change ships inside the newer
commit's build. That build is incremental from the newest *recorded* build of
an ancestor (`ci-built-images.sh --previous` passes over a run that recorded
nothing), and an image is diffed from the commit its reused digest was built
from, not from the commit before, so an image a cancelled commit changed is
rebuilt ([below](#main-builds-only-the-images-a-commit-changed)). Every
intermediate commit was already tested on its pull request, or on its
identical tree ([Main reuses the tested tree](#main-reuses-the-tested-tree)).

**How a release ends when it is not the newest.** `images and scan` asks
GitHub to compare its commit with `main`; `ahead` means `main` holds the
commit and more. It asks twice:

* **before the wait**, so a release that starts behind `main` never waits
  for a build at all;
* **after it**, because the newer push cancels this commit's `application`
  run and the wait then ends "never built". That failure is read as
  superseded only when `main` has moved on **and** `application`'s push run
  for the commit concluded `cancelled` with its `build images` job cancelled,
  skipped or never started. A build that failed, a scan that refused, or a
  `main` that has not moved stays red. So does an unreadable API: "could not
  tell" is never "superseded".

A superseded release ends **green**, says so in its summary and a notice, and
outputs `superseded=true`; `approval` — and so promote, both applies and the
deploy — and the plugin tag do not run. A dispatch is never superseded: it
ships `main`'s head by construction.

**A pending release is replaced; a running one is never cancelled.**
`release-dev` stays `cancel-in-progress: false`. GitHub keeps one pending run
per group and cancels the older pending run when a newer one queues, so only
the newest push waits. The run in progress is left to finish: once past
`approval` it may be half-way through a promote, an apply or a deploy, and
cancelling it there would leave the release lock to go stale and dev between
two releases. Before `approval` it does not need cancelling: it ends itself,
superseded, within one poll (30 s) of the newer push cancelling its build.

**Why the release lost its path filter.** It filtered on `apps/`, `images/`,
`terraform/`, `kubernetes/`, `scripts/`, `.trivyignore.yaml` and its own
files. With `main`'s runs cancelling each other, a docs- or tests-only push
cancels the previous commit's build; under the filter it started no release,
and the previous commit's release ended superseded with nothing to ship its
change — exactly the 2026-09-24 stranding below. Now that push starts the
release that ships it. A release of a tree that changed nothing deployable
promotes the same digests (CI re-tags every unchanged image under the new
commit's tag), plans no change, and its deploy verifies the same digests;
acceptance then runs on the new SHA, as after any release. `hotfix.yml` keeps the filter: it acts
only for a labelled pull request anyway.

**What it costs.** A Cloud Build a cancelled run submitted carries on —
stopping `gcloud builds submit` does not stop it (`build-images.sh`,
`on_interrupt`) — at most `BUILD_PARALLELISM` of them per cancelled run. A
`hotfix`-labelled commit is still released by its own lane, which waits for
the same build: if a newer push cancels that build, the hotfix run fails and
the newer commit's normal release ships the fix.

**What a pull request cannot prove about this.**
`tests/unit/scripts/test_release_newest_only.py` holds the groups, the
trigger and the two conditions, and runs both decision steps against a fake
`gh`. It cannot show that GitHub's compare API says `ahead` for a commit
`main` has moved past, or how a run cancelled by concurrency lists its jobs;
the first burst of merges after this lands is the first real proof.

### Before 2026-10-26: one group per commit on `main`

*Superseded by the section above; kept because it records why the release
lost its path filter.* From 2026-09-24 every commit's `application.yml` run
on `main` had a concurrency group of its own and was never cancelled. A run on
`main` was the only build of its commit, so nothing could stop it before it
built — and turning `cancel-in-progress` off was not enough on its own.
GitHub keeps **one pending run per group** and cancels the older pending run
when a newer one queues, whatever `cancel-in-progress` says. Measured on
2026-09-24: release run 36035365877, pending in `release-dev`, was cancelled
at 17:41:28 — two seconds after 36036058352 queued — and lists zero jobs.

With one group for all of `main` that cancelled builds releases were waiting
for. `application.yml` ran on **every** push to `main`; `release.yml` had a
path filter. So: commit A building, B queued behind it, and a push C that
touched only docs, tests or the README. C's run cancelled B's; C started no
release to take the place of B's; B's release, still pending, then found its
commit never built and went red, and B's change did not reach dev until the
next push that started a release.

The price was that runs on `main` were not serialised: a burst of merges
built every commit, in parallel. On 2026-09-24 `main` took 18 pushes between
15:56:14 and 17:41:26 and three runs would have been building at once. On
2026-10-26, with main's jobs on GitHub-hosted runners, it was the stall
above.

**Kept, deliberately:** scan before promote; all-or-nothing promotion; every
deployed image pinned by digest. (The `dev`/`prod` environments used to sit on
the apply and the deploy; they now sit on the one `approval` job in front of
the promotion — see the next section.)

**What a pull request cannot prove about this.** `release.yml` never runs on a
pull request, and `build images` is skipped there (a pull request's own build
pushes nothing and records no manifest), so PR CI never exercises
the real hand-off. It checks the scripts against a fake `gh` and `gcloud`
(`tests/unit/scripts/test_ci_built_images.py`,
`test_push_images_promotes_recorded_digests.py`), reads both workflows to hold
the wiring in place (`test_release_reuses_ci_images.py`), and lints both with
actionlint. It cannot show that the job receives `actions: read`, that
GitHub's API returns what the fake returns, that `gh run download` fetches the
artifact across runs, or that a release actually gets faster. The first push
to `main` after this lands is the first real proof — read its release run.

## Main builds only the images a commit changed

Owner decision 2026-10-08 (observer proposal H). `build images` on main used to
rebuild all 9 images on every push. Measured that day, the job took **9.0-10.4
min**: `agent-runtime-base` alone 5m04, then `agent-runtime-browser` 3m21 on
top of it, and each service image 2m09-2m42. A `uv.lock` change, the most
common kind of dependency bump, reached 7 of the 9. Most pushes change one
service, or docs and tests that no image reads, and still paid for all of it.
That is Cloud Build quota in a project shared with another team, and minutes
on the critical path of every release.

**What happens now.** The job runs two scripts in one step:

1. `scripts/lib/ci-built-images.sh --previous` finds the newest `build images`
   job on main that succeeded for an **ancestor** of this commit and kept its
   `images-dev` record. It reads at most `CI_PREVIOUS_LOOK` (20) runs. It
   never waits for a build that is still running and never picks a later
   merge whose run happened to finish first. It never picks this commit
   either, so re-running a commit's build rebuilds it.
2. `scripts/build-images.sh --incremental <that record>` rebuilds image X only
   if `--affected-by` reaches it from `git diff <the commit X's previous
   digest was built from> HEAD`. That is the same input mapping and the same
   `build_after` closure a pull request's `build-check` uses, so a base change
   still rebuilds the browser and the indexer. Every other image keeps its
   previous digest, and this commit's tag is added to it with `gcloud
   artifacts docker tags add`. The tags are added **before** any build is
   submitted, because the browser's and the indexer's recipes pull
   `agent-runtime-base:<tag>`. If a tag cannot be added, that image is rebuilt
   instead, together with everything built FROM it.

**Why tag rather than record the old tag.** Every digest in the manifest then
carries this commit's tag. `push-images.sh`, `image-refs.sh`, `deploy.sh` and
`release.yml` read a manifest that is indistinguishable from a full build's,
and none of them changed. The proof is unchanged too: the manifest still names
all 9 digests, both trivy scans still run on all 9, and `deploy.sh
--verify-only` and acceptance run against the full set. Each manifest entry
adds `reused`, `built_from` (the commit whose build made the digest) and
`built_at`. Through a chain of reuses, the next build diffs from `built_from`
and ages from `built_at`, never from the record's own commit. Otherwise a
change made two builds ago could hide behind a build that reused its image.
The job's summary lists every image as rebuilt or reused.

**The release checks the record before it promotes it** (lane REL-SKIP, part
of #1194, owner-approved 2026-10-12). Release run 38273482069 on `f816e09b`
built nothing and failed, and nothing on the release side checked whether the
digests it was offered matched the commit. So `build-images.sh --reuse-ci` now
runs the same plan over CI's record of this commit. Each reused digest is
diffed from its own `built_from` to this commit, over that image's inputs, so
a stale one is caught whatever HEAD~1 or the last good release was. A digest
this commit changed is built in the release, under `only` too. Only those
images are built, plus the ones built FROM them; every other digest is kept.
The release never compares with HEAD~1 or with the last release. A release
that failed, was cancelled or was superseded therefore cannot make a later one
skip an image. A record whose every digest was built from this commit needs no
check. A docs-only push builds nothing. The release checks out one commit, so
the check first fetches the history it diffs against. `release-images` sets up
uv so that a digest kept across a `uv.lock` change is judged by its own
`uv export`, as it was in CI
(`tests/unit/scripts/test_release_rebuilds_stale_images.py`).

**A reused image reports the commit that built it, not the release.** The
`GIT_SHA` and `BUILD_TIME` build args are baked in when a digest is built, and
re-tagging does not change a digest. So on a reused image, swarm-api's
`/v1/version` (`git_sha`, `build_time`, from the `ENV` in
`images/swarm-api/Dockerfile`) and the indexer's
`org.opencontainers.image.revision` label name its `built_from` commit, which
can be several merges behind the release that deploys it. That is the truth
about the bytes: nothing in the image changed since `built_from`. The release
that deploys it is the image's tag and the manifest's `commit`, and Cloud Run's
`revision` (also on `/v1/version`) names the deploy. Read `git_sha` as "the
code in this container is that commit's", not "this is the release".

Every other value an image bakes in is recorded too: each manifest entry's
`build_args` lists them (`VITE_SWARM_ENV=dev` for `swarm-ui`, none for the
rest). An image whose baked values differ from its previous digest's is
rebuilt whatever the diff says, because a value fed from outside the tree (a
GitHub variable, say) changes no file and would otherwise ride a reused digest
until the 7-day limit. A record from before `build_args` existed rebuilds
`swarm-ui` once.

**`uv.lock` is narrowed to each image.** When `uv.lock` is the only input of a
Python image that changed, the script runs that image's own `uv export ...
--prune ...` line, read from its Dockerfile (held by
`test_service_image_install.py`), at both commits. The image is rebuilt only if
the two exports differ. Without `uv`, or if an export cannot be read, the
image is rebuilt.

**When everything is rebuilt anyway**, with the reason in the log, the
summary and the manifest's `incremental.full_build`:

* **build logic changed** since a reused digest was built: `build-images.sh`,
  `scripts/lib/common.sh`, `ci-built-images.sh` or `application.yml`. These
  decide how every image is built, so no diff of inputs can vouch for them;
* **there is no ancestor record**: the first build, a record past its 30-day
  retention, a shallow checkout (the job checks out with `fetch-depth: 0` for
  this), or a GitHub API that could not be read. A full build is the safe
  answer to "I could not tell", only a slower one, which is also why
  `--previous` retries a failed read only once, 5 s later
  (`CI_PREVIOUS_TRIES`=2, `CI_PREVIOUS_POLL`=5), not the release lookup's
  5 x 30 s: two minutes of retries on every build to save one ~10-minute
  build was the wrong trade;
* **a reused digest is more than 7 days old** (`BUILD_REUSE_MAX_AGE_DAYS`), so
  patches to `python:3.11-slim`, node and nginx arrive within a week even in a
  corner of the tree nobody touches;
* the **`full_build` input** is set on a `workflow_dispatch` of
  application.yml;
* the **environment is prod**. A prod release builds its own images
  (`--reuse-ci or-build`, which is never incremental). `swarm-ui` bakes its
  environment in, and prod is always built whole;
* the **working tree is dirty**, so git cannot say what changed.

**An image whose Dockerfile upgrades OS packages is rebuilt by every main
build and every release.** This was the owner's decision on 2026-10-14 ("force a fresh apt
layer", on every release). It costs about 7 extra minutes per release.
`APT_REFRESH` names the build: the commit plus the GitHub run id and attempt,
or the commit and the time outside Actions. `BUILD_APT_REFRESH` overrides it.
The manifest records it per image as `apt_refresh`. A digest of such an image
is reused only when its `apt_refresh` is this build's, which no other run's
is. So `plan_incremental` rebuilds every one of them, and the release's reuse
check does too, even from a record CI built whole for the same commit. An
image built FROM one is rebuilt with it. One that upgrades nothing
(swarm-verify) is still reused. Which images upgrade is read from their
Dockerfiles (`upgrades_os_packages`), not listed.
A digest CI built from the commit being released is that commit's own
build, not a reuse: the 7-day age limit and the unrecorded-build-args rule are
not asked of it, and it is not re-tagged (it already carries the tag), so a
release rebuilds only the OS-upgrading images and those built FROM them.

The cause it removes: releases 38535875750 and 38540997006 refused
workspace-apply over curl/libcurl4t64 `8.14.1-2+deb13u5` (CVE-2026-8286, HIGH,
fixed in `+deb13u7`). Its Dockerfile runs `apt-get upgrade`, but its log read
`workspace-apply reuse no input changed since 346f2725df43`. The digest was
built on 2026-10-13, before Debian shipped the fix, and nothing in the tree
changes when Debian does.

The build passes `--build-arg APT_REFRESH` (workspace-apply's recipe as
`_APT_REFRESH`). Every apt or apk upgrade `RUN` names it, so no layer cache
serves an older upgrade either. workspace-apply's apt `RUN` also fails its own
build when a simulated `apt-get dist-upgrade` still has something to install,
which catches a package `apt-get upgrade` kept back. The scan stays strict:
nothing is ignored or pinned. `test_build_images_apt_refresh.py` holds all of
this.

**What this does not prove.** The tests (`test_build_images_incremental.py`,
`test_ci_built_images.py`) run the real scripts against a fake `gcloud` that
remembers tags and a fake `gh`. They cannot show that Artifact Registry moves
a tag exactly as the fake does. `push-images.sh` relies on the same command to
move a channel tag. Read the first incremental `build images` run on main and
its summary.

## Images are built on a pull request, without pushing

**Owner decision, 2026-10-05 (#650).** Until then images were built only on
`main`. #641 changed `images/agent-runtime-base/Dockerfile` and the repo-index
LSP package; its pull request was green, and the first build of the image ran
after merge, where `swarm-repo-index --lsp-self-test` failed (Cloud Build
650b9ebb, main 539b5b58 and f1c4b075). Main was red and releases blocked until a
fix landed. An image's build-time self-tests are checks like any other, and a
check that first runs after merge is not a pull-request check.

### Why there is no pull-request identity

**Owner decision, 2026-10-05: pull requests get no Google identity.** A pull
request's checkout chooses what runs — the workflow file, `build-images.sh`,
every `cloudbuild.yaml` — so whatever an identity minted for a pull request
can do, anyone who can push a branch can do. An identity that could submit
Cloud Builds would let code on any branch run builds in
this repository's GCP project, which is a **shared project**: its Cloud Build quota,
its logs and the other team's resources in it are not this repository's to expose.
The deployer stays pinned to `refs/heads/main` (`terraform/bootstrap/wif.tf`,
and `build images` in `application.yml` says why that pin must never widen),
and no second, narrower identity is created either. #673 first shipped this
job waiting for one; that path is gone.

So a pull request builds on the GitHub-hosted runner, with `docker buildx`,
holding nothing: no Google authentication, no `id-token`, no registry login, no
secret.

### What `build changed images without pushing` does

`application.yml`'s `build changed images without pushing` job (`build-check`),
on every pull request that reaches an image. **Since 2026-10-09 one that
reaches none skips the job by its own `if:`**: `what this change reaches` runs
the same `build-images.sh --affected-by` over the same diff (its `does the
change reach an image` step) and says `images=false`, so the job takes no
runner at all. It says `true` whenever it cannot tell (not a pull request, a
diff it cannot read) and fails, failing every gate, if the script does.
Otherwise:

1. **Skips a fork's pull request in seconds**, before checking anything out,
   as the owner's brief for this job (2026-10-05) requires. A maintainer who
   wants a fork's images built pushes its branch to this repository.
2. **Maps the change to images.** `scripts/build-images.sh --affected-by`
   reads `git diff base...head` against each image's inputs: its recipe's
   directory (`images/<image>/**`), every `COPY`/`ADD` source its Dockerfile
   names, and `.dockerignore` and `.gcloudignore`, which filter every build's
   context. An image built `FROM` a reached one (`build_after`) is reached too,
   so a base change builds the browser and indexer images. The `COPY` sources
   are **read from the Dockerfiles** (`dockerfile_copy_sources`,
   `scripts/lib/common.sh`), never listed by hand: a `COPY` added to a
   Dockerfile is an input the day it lands. A `COPY` form the reader cannot
   map — JSON form, a heredoc, a variable, a URL — fails the job rather than
   being skipped, because a skipped source is a file whose change would not
   build the image. `build-images.sh --inputs` prints the whole map. A `COPY`'d
   file outside `images/` counts: `Makefile`, `scripts/**` and
   `tests/acceptance/fixtures/**` reach `swarm-verify`; `apps/common/**`
   reaches every Python service and the worker images. No image input changed:
   the job ends here, green, and its summary says so.
3. **Frees runner disk, for the agent-runtime family only** (since
   2026-10-09: the deletion took a median 92 s, up to 261 s, three times the
   median build, and a service image or `swarm-ui` does not need it). `agent-runtime-base` is ~900 MB compressed and
   several GB unpacked, and BuildKit holds its builder stage, its runtime and
   its exported layout at once before the browser image (Chromium) and the
   indexer (a Go toolchain and two compiled language servers) are built on it.
   The job deletes toolchains it never uses (Android, .NET, GHC, CodeQL,
   Swift) and the runner's cached images, and writes the free space on `/`
   before and after to its summary. **How much was needed is not measured
   yet**: the first pull request that builds the base records it there, and
   that number belongs here.
4. **Builds them with `docker buildx` and pushes nothing.**
   `build-images.sh --build-only --local <images>` writes exactly the configs
   `--build-only` would submit to Cloud Build — `generate_config`'s for a
   Dockerfile, a checked-in `cloudbuild.yaml` with its `images:` block removed,
   `generate_chain_config`'s base-then-derived pair — and `refuse_if_pushes`
   reads each one, as before. Then, instead of submitting, it replays them on
   a local `docker-container` buildx builder, one at a time:
   * a `docker build` step becomes `docker buildx build` with the step's own
     arguments (main's Dockerfile, `--platform linux/amd64`, `GIT_SHA`,
     `VITE_SWARM_ENV` for `swarm-ui`), plus `--no-cache` — a cached `RUN` is a
     self-test that did not run — and an output that is never a registry:
     `type=cacheonly`, or `type=oci` into `build/build-only/local/` for an
     image something is built `FROM`;
   * **order as on main**: `agent-runtime-browser` and `agent-runtime-indexer`
     are built after `agent-runtime-base`, from the base this run built — its
     digest passed as `BASE_IMAGE=swarm-build-only/agent-runtime-base@sha256:…`
     and resolved by `--build-context …=oci-layout://…`, so `FROM` never pulls
     and the indexer's refusal of an undigested base still holds. The base is
     built once per run even when both derived images need it, and a base that
     failed builds nothing from it;
   * any other step — `swarm-verify`'s context assembly — runs in its own
     image with the checkout at `/workspace`, as the runner's user, with no
     docker socket;
   * a docker step that is not a build (push, pull, login, tag), and a config
     that asks for a secret, are refused.

   No `gcloud` call is made, no manifest is written, and `--local` is only a
   mode of `--build-only`.
5. **Runs main's self-tests, and says which.** The self-tests are `RUN`
   steps of the Dockerfiles main's Cloud Build builds — the base's
   agent-worker import and toolbox checks, `swarm-repo-index --self-test`,
   `--lsp-self-test` and `swarm-repo-graph --self-test` in the indexer, the
   Chromium launch in the browser — so building the same Dockerfile to its
   last stage, uncached, is running them; a failing one fails the build and
   the job, naming the image. The script **reads** each Dockerfile's `RUN`
   steps into `build/build-only/local-report.md`, which the job appends to its
   summary; the workflow names none of them.

The job concludes `success` with its build steps skipped when no image input
changed and on a fork's pull request. It runs on every pull request, so a
failed build fails `application.yml`'s run and `ci-gate` with it. `main`'s
`build images` is unchanged, and only pull requests that reach an image pay
the minutes. A newer push to the branch cancels the run, and with it the
build, which lives on the runner.

**One difference from main, by construction.** Cloud Build receives the
checkout filtered by `.gcloudignore`, then `docker build` filters it again by
`.dockerignore`; the runner builds the checkout filtered by `.dockerignore`
only. A Dockerfile that `COPY`s a file `.gcloudignore` excludes and
`.dockerignore` does not would pass here and fail on main.

**What a pull request proves about this, and what it cannot.**
`tests/unit/scripts/test_build_images_local.py` runs the real script against a
fake `docker` and a `gcloud` that fails if called: every docker call is a
`buildx build` or a socket-less `run`, no push, no login, no registry output;
the base built first and once, the derived images `FROM` its digest through an
`oci-layout` context; every build of the Dockerfile main's recipe names, to its
last stage, uncached; a planted `RUN` showing up in the report; a recipe asking
for a secret refused. It reads the workflow job (buildx, no `secrets.`, no
`id-token`, no login, no `vars.GCP_*`, no self-test restated), runs its fork
and plan steps (a fork skips before the checkout; a docs change builds nothing;
an image change builds), and drives `ci-gate` with this job failed.
`test_build_images_pr_check.py` holds the path → image map and every input
path firing `application.yml` on a pull request. They cannot show that BuildKit
resolves the base through the `oci-layout://` context on a real runner, that
the disk suffices, or how long it takes. The first pull request touching an
image after this lands is that proof — read its `build-check` summary.

## A prod release waits for approval before anything prod-facing

**Owner decision, 2026-09-24.** A prod release waits for the owner's approval
before anything prod-facing happens, and `:prod` image promotion sits behind
that same approval. Scanning is read-only and may come before it. dev stays
un-gated.

**Why.** Until this change the approval sat on `terraform apply (prod)`, and
the `images and promote` job ahead of it had already moved `:prod` to the new
digests. A prod release that was rejected — or simply left waiting — had
repointed `:prod` at images prod was not running. `:prod` is not only for
humans: a `skip_build` redeploy of prod resolves `:prod` and deploys whatever
it finds, so the next routine redeploy would have shipped the unapproved
images under an approval given for "redeploy what is there".

**What runs before the approval, and what after.**

| before the approval — reads, and builds images nobody deploys | after it, and only if it succeeded |
|---|---|
| `verify` — unit tests, shellcheck, destroy-guard self-test | `promote` — scans again, then moves `:prod`, all or nothing |
| `images and scan` — reuses CI's build, or for **every prod release** builds one here (Cloud Build, `<sha>` tags in Artifact Registry, **no channel tag**) | `terraform apply` — plan, shared-project guard, apply |
| `push-images.sh --scan-only` — every digest confirmed and trivy-scanned; nothing moves | `deploy` — digest verification; on prod also the in-VPC smoke suite and the GKE proof |
| the digests written to the run summary, for the reviewer to read while the run waits | |

A prod release **builds before the approval**: `application.yml` builds for
dev, and swarm-ui bakes its environment in when it is compiled, so a prod
release is always built by the release itself (see the table in the previous
section). That is a consequence of scanning first — there is nothing to scan
until it is built — and it costs a Cloud Build in the shared project even for
a release that is then rejected. It changes nothing prod runs.

**One approval, not one per job.** GitHub holds *every* job that names a
protected environment until a reviewer approves *that* job (GitHub's
documented behaviour; not observed here, because prod has never been
released). The apply and the deploy both named `prod`, so a prod release
would have asked twice — the second time
after the apply had already changed prod, which made it a question with no
decision left in it. Adding a third for the promotion would have put
promotion behind a *different* approval from the apply, not "that same" one.
So `approval` is the only job in `release.yml` that names `prod` (or `dev`);
`promote`, `terraform apply` and `deploy` name none. The one other
environment is `dev-iam`, named by a dev-only job ([below](#a-dev-release-that-changes-iam-waits-for-the-owner)). They run only
if it **succeeded**, and on prod only in the attempt it succeeded in (see
"A prod approval clears one attempt" below).

That last clause is written out in each job's `if:` and it is load-bearing.
`terraform apply` needs a status function, because a `skip_build` redeploy
skips `promote` and the apply must still run; a status function on its own
would run the apply straight past a rejected approval. A reviewer who rejects
the run fails the `approval` job; a timeout or a cancel cancels it; either way
nothing after it starts.

**A cancel is a "no" too, after the approval as well as before it.** The
status function is `!cancelled()`, never `always()`, on every job and step
that promotes, applies or deploys. GitHub still *starts* an `always()` job
after the run is cancelled, and still runs an `always()` step in a job that
is being cancelled. The first version of this change used `always()`, and
two holes followed from it:

* On a `skip_build` prod redeploy, `promote` is skipped the moment `approval`
  succeeds, so the apply is the very next job. A reviewer who approved and
  cancelled a second later still got a prod apply.
* `deploy`'s GKE proof (prod; dev's is accept.yml's) dispatches a browser agent into the
  environment. It was `always() && steps.verify.outcome == 'success'`, so a
  cancel that landed on the smoke test still dispatched it.

`!cancelled()` runs past a skipped job or a failed step exactly as `always()`
does, and it stops on a cancel. Only the read-only `status` and `report`
steps keep `always()`. A cancel can still land in the middle of a job. That
is GitHub's behaviour, not something this file can change: a job that is
already running when the cancel lands stops wherever it has got to. A cancel
during `promote` can stop `push-images.sh` between two tag moves, before its
put-back runs, and leave `:prod` mixed (see "ALL OR NOTHING" in that
script). A cancel during `terraform apply` leaves whatever Terraform had
finished.

**A prod approval clears one attempt of the run, not the run.** GitHub asks
for an approval only for a job that names a protected environment. A
*partial* re-run — "Re-run failed jobs", or "Re-run this job" — starts the
chosen jobs and every job that depends on them again, and keeps the result
of every other job (GitHub's REST reference: "Re-run all of the failed jobs
and their dependent jobs"; "Re-run a job and its dependent jobs"). Re-running a failed `terraform apply (prod)` therefore leaves
`approval` out, and `needs.approval.result` is still the `success` of the
attempt the reviewer approved. With nothing else in the way, the apply would
start at once, with nobody asked. GitHub allows a re-run for 30 days, to
anyone with write access. The apply "plans immediately before applying", so
it would plan and apply that day's state of the shared project, and on a
`skip_build` redeploy it would resolve `:prod` again. Nobody would have
approved that plan. On `main` before this change, the apply and the deploy
named `prod` themselves, so every re-run of them asked again. Moving the
approval to one job made this possible.

So the approval is tied to the **attempt** it cleared:

* `approval` outputs `attempt: ${{ github.run_attempt }}`. GitHub renders it
  at the end of the job, so no step can write the wrong number. A partial
  re-run should carry that output over from the attempt that approved. That
  has not been observed here. If GitHub drops it instead, the output is
  empty: the check below then fails closed, and `stale-approval` still
  reports it.
* On prod, `promote`, `terraform apply` and `deploy` each run only
  if `needs.approval.outputs.attempt == github.run_attempt`. Each carries the
  condition itself, because "Re-run this job" on a deploy whose smoke test
  failed keeps the apply's old `success` too.
* A partial re-run of prod therefore skips all three. A run whose re-run jobs
  are all skipped would end **green** having changed nothing, so the
  `stale-approval` job ("prod approval is from an earlier attempt") needs
  all of them. It is re-run with any of them, and it fails the run with
  the error `Nothing was released -- re-run ALL jobs`.
* **"Re-run all jobs"** is how to retry a prod release. It runs `approval`
  again, and `approval` waits for its reviewer and records the new attempt.
* dev has no reviewer, so dev re-runs are not tied. "Re-run failed jobs" on a
  dev apply that hit the state lock applies again, as it always did (owner:
  keep dev un-gated).

**The cost, and the alternative.** "Re-run all jobs" runs everything again.
For prod that includes `images and scan`. CI never builds for prod, so this
is **a second Cloud Build of all eight images** in the shared project, plus
the verify job and a second scan, before the reviewer is even asked. The
other way to close the hole is to name `prod` on the apply (and the deploy)
again. A re-run of those jobs would then wait for its own approval, and
nothing would be rebuilt. But every prod release would ask the reviewer two
or three times, which "one approval" was chosen to avoid. `promote` would
still need the attempt check or an environment of its own. **This is the
owner's choice and is not settled.** This change ships the attempt check,
which keeps one approval per release, pending that decision.

**GitHub's Deployments list shows the approval, not the deploy.** GitHub
records a deployment against a job that names an environment, and here that
is only `approval`. So the repository's Deployments page lists prod as
deployed at the commit once the approval passes, and it keeps saying so if
the promotion or the apply then fails. The release run's own result is what
says whether prod changed. Its `deploy` summary lists the digests
the apply pinned, and that job's verify step fails unless every service runs
them.

**What that changes about the other jobs.** Measured 2026-09-24 with
`gh api`:

* No environment holds a secret or a variable. `GCP_DEPLOY_SA`,
  `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER` and `TF_STATE_BUCKET`
  are repository variables, so the apply and the deploy lose nothing by not
  naming an environment. **If one is ever added to `prod`, only the
  `approval` job can read it** — a job that needs it must name the
  environment, and GitHub will then ask again for that job.
* The workload identity pool does not pin the token's `sub`
  (`terraform/bootstrap/wif.tf` says why), so the apply and the deploy
  minting `repo:…:ref:refs/heads/main` instead of `repo:…:environment:prod`
  is admitted exactly as before. `assertion.ref` is `refs/heads/main` either
  way.

**dev.** A dev release runs the same jobs. `approval (dev)` names `dev`, which
has no protection rule, so it passes straight on. What that costs every dev
release, estimated from release 36059484794 (not yet measured on this
workflow): one more job start for `approval`, one for `promote` (checkout,
authentication, trivy), and the second scan — that run's scan and promotion of
all eight images took 75 seconds, 21:29:50–21:31:05.

**Why scan twice.** The first scan lets a reviewer approve a set that has
already passed, instead of one the promotion refuses a minute after the
approval. The second is the promotion's own gate, kept because the approval
may come hours later against a vulnerability database that has learned
something since.

**What the repository setting must hold** — and this workflow cannot check.
GitHub creates an environment a workflow names if it does not exist, with no
protection at all, so the approval is exactly as real as the `prod`
environment's settings. Measured 2026-09-24:

| environment | protection |
|---|---|
| `prod` | required reviewer `bogdan-alexandrescu` (self-review allowed); deployment branch policy: branch `main` only; `can_admins_bypass: true` |
| `dev` | none |

`can_admins_bypass: true` means a repository admin can deploy to prod
without an approval by choosing to bypass the rule. That is GitHub's default,
and it was left as measured.

**What a pull request proves about this, and what it cannot.**
`tests/unit/scripts/test_release_prod_gate.py` reads `release.yml` and
schedules it: for a prod dispatch, with and without `skip_build`, it walks
the job graph, evaluates every job-level `if:` against how the jobs it needs
ended, makes the job that names `prod` be rejected, cancelled or never
reached, and asserts that no job that promotes, applies or deploys can start
in any of those runs — and that, approved, every one of them still can. Two
more tests cancel the run. They try every way the jobs, or the earlier steps,
could have ended, and assert that no such job starts and no such step runs.
It also asserts that exactly one job names `prod`, and that no dev release
names it. The re-run tests carry an approved first attempt into a second
one. They try every set of jobs one partial re-run can restart without
`approval`, from every distinct approved first attempt. In each, no job that
promotes, applies or deploys may start, and a job that exists to fail must
start. "Re-run all jobs" must still reach every such job, and a dev re-run
must still promote, apply and deploy. The model's re-run rules come from
GitHub's documentation and have not been observed on this workflow. `test_push_images_scan_only.py` holds `--scan-only` to moving
nothing, which is what exempts the pre-approval scan.

The test finds prod-facing steps by reading each `run:` as text, and it errs
toward counting a step as one. A step whose *prose* says "terraform apply"
counts as an apply. That happened: the pre-approval summary did it, and the
test failed the pre-approval job for applying. So reword the prose; do not
loosen the match.

It cannot show that the `prod` environment still has its reviewer, that
GitHub's scheduler agrees with the model (the model reads a job's `success()`
over its direct needs only, the more permissive reading, so "cannot start" in
the model means cannot start on GitHub), that GitHub starts `always()` work
after a cancel (that is its documented behaviour, not observed here), or that
a real prod release waits. The first prod dispatch after this lands is that
proof.

## A dev release that changes IAM waits for the owner

**Owner decision, 2026-09-28 (#268).** Gate only IAM plans. A dev release
whose Terraform plan touches IAM stops for the owner. Every other dev release
applies un-gated, as it did before. Prod is unchanged: its one approval is
`approval (prod)`, which already covers every plan.

**Why IAM, and only IAM.** A dev apply runs in `saga-agents-staging`, which is
shared with another team. Most dev plans move image digests or change a
service's environment, and gating those would put the owner in front of every
merge. An IAM change decides who can do what in that shared project, and that
is the kind of change the owner wants to read before it lands. Ungating dev
(2026-09-24) was a decision about routine releases, not about grants.

**What this gate stops, and what it does not.** `dev-iam` catches an operator
mistake — a plan that changes who can do what in a shared project, applied
without anyone reading it — the same way `approval (prod)` does for prod. It
is not a boundary against an attacker who can merge to `main`: as measured
above for `approval (prod)`, `GCP_DEPLOY_SA` and `GCP_WIF_PROVIDER` are
repository variables, not environment ones, and the workload identity pool
does not pin the minted token's `sub` to the environment a job named
(`terraform/bootstrap/wif.tf`). `infrastructure-iam` authenticates with
exactly the same service account, through exactly the same provider, as
`terraform apply (dev)` and `terraform apply (prod)` — naming `dev-iam` asks a
human to look at the plan, but grants the job asking no identity that a plan
merged straight past the review could not already reach. Whether this gate
should also be a real identity boundary — for example, an attribute condition
on the WIF binding scoped to the environment — is the owner's to decide.

**What counts as an IAM change.** A resource change whose type is one of

* `google_project_iam_custom_role`, `google_organization_iam_custom_role`
* `google_*_iam_member`, `google_*_iam_binding`, `google_*_iam_policy`,
  `google_*_iam_member_remove`, `google_*_iam_audit_config` (any resource
  family the provider names one of these on, not only `project`)
* `google_iam_workload_identity_pool`, `google_iam_workload_identity_pool_provider`
* `google_iam_deny_policy`, `google_iam_principal_access_boundary_policy`
* `google_service_account_key`
* `google_service_account` -- but only `delete` or `forget`; its own `create`
  and `update` grant nothing (what it can do comes from the `_iam_member` /
  `_iam_binding` / `_iam_policy` resources already gated above), so only its
  removal is an access change
* `google_storage_bucket_acl`, `google_storage_bucket_access_control`
* `google_bigquery_dataset_access`

and whose actions include `create`, `update`, `delete` or `forget` (every
family above except `google_service_account`, which is `delete` or `forget`
only). A replace is `delete` + `create`, so it counts. `no-op` and `read` do
not. The rule is [`scripts/lib/iam-plan.jq`](../scripts/lib/iam-plan.jq),
stated there once, and it is reached only through
`scripts/lib/plan-guard.sh --classify-iam`.

**The second widening, 2026-09-28 (#274).** The `google_iam_workload_identity_pool`
family, the deny and principal-access-boundary policies, a service account's key
or deletion, the pre-IAM-conditions ACL mechanisms on a bucket or a BigQuery
dataset, an organization-level custom role, an audit config and
`_iam_member_remove` all decide who can do what, or what they can do it as, in
`saga-agents-staging` exactly like the families #268 already gated -- so they
gate the same way, through the same rule.

**`forget` is the case that would be missed.** A `removed` block with
`destroy = false` plans the action `forget`. Nothing live is deleted; the
resource just leaves this root's state. That is how
`terraform/infra/custom_roles_moved_to_bootstrap.tf` hands eight custom roles to
the owner's bootstrap root. A filter that knew only create, update and delete
would have waved it through. Who administers a role is an IAM decision.

**How a dev release runs now.**

1. `terraform apply (dev)` plans and saves `plan.tfplan`, as before, then runs
   the shared-project guard.
2. It classifies `terraform show -json plan.tfplan`. The answer is `true` or
   `false`. A plan the classifier cannot read fails the job, so nothing is
   applied. It never answers `false` for a plan it could not read.
3. The IAM rows go to the job's summary, one table row per change: address,
   type, actions, the resource the grant is on, and the role and member. The
   reviewer reads them on the run's page while the next job waits.
4. **`false`:** the same job applies `plan.tfplan`, exactly as before.
   **`true`:** the job does not apply. It copies `plan.tfplan` to
   `gs://<state bucket>/plans/dev/<run id>-<attempt>/plan.tfplan`, records its
   sha256 as a job output, and ends.
5. `terraform apply, IAM (dev-iam)` names the `dev-iam` environment, so GitHub
   holds it until the owner approves. It then fetches the held plan, refuses it
   if its sha256 differs from the one recorded, and applies **that file**. It
   never plans, because a new plan would be one nobody reviewed. After a
   successful apply it deletes the held plan.
6. `deploy` runs after whichever apply ran. If the owner rejects
   `dev-iam`, nothing deploys, and the run ends failed.

**Why the state bucket and not a workflow artifact.** This repository is
public, and a saved plan holds every planned value in plaintext. The state
bucket already holds the same values in the state. It enforces public access
prevention, and the deployer is `objectAdmin` on it (`terraform/bootstrap`,
`deployer_state`), so this needs no new grant.

**Staleness.** Terraform refuses to apply a saved plan once the state it was
planned against has changed ("Saved plan is stale"). This is Terraform's
documented behaviour; it has not been observed on this workflow. A dev-iam
approval given hours later therefore either applies exactly what was
reviewed, or fails without applying anything. It never applies something
else. To retry after a stale refusal, re-run `terraform apply (dev)`. That
re-plans, re-classifies, holds the new plan and asks again. Re-running the
dev-iam job on its own asks again and re-applies the same held plan.

**What waiting costs.** The run holds the `release-dev` concurrency group
while `dev-iam` waits. So the next dev release queues behind it, and GitHub
keeps only the newest pending run in a group. A rejected or abandoned IAM
release therefore delays routine dev releases until it ends. Reject or cancel
it, rather than leaving it waiting ([measured](#a-held-dev-iam-plan-cancels-every-release-behind-it)).

**The environment is a repository setting the owner creates.** GitHub
creates an environment a workflow names if it does not exist, with no
protection at all. Until the commands below have run, `dev-iam` does not wait
for anyone. `release.yml` cannot check this. To create it with the owner as
required reviewer, restricted to `main` like `prod`:

```bash
owner_id="$(gh api users/bogdan-alexandrescu --jq .id)"
printf '{"reviewers":[{"type":"User","id":%s}],"prevent_self_review":false,"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' "${owner_id}" \
  | gh api --method PUT repos/bogdan-alexandrescu/SwarmCloud/environments/dev-iam --input -
gh api --method POST repos/bogdan-alexandrescu/SwarmCloud/environments/dev-iam/deployment-branch-policies \
  -f name=main -f type=branch
```

and to read it back:

```bash
gh api repos/bogdan-alexandrescu/SwarmCloud/environments/dev-iam \
  --jq '{reviewers: [.protection_rules[] | select(.type == "required_reviewers") | .reviewers[].reviewer.login], branches: .deployment_branch_policy, can_admins_bypass}'
```

`prevent_self_review` is `false` for the same reason as on `prod`: the owner
is the only reviewer, and a release started by their own merge must still be
approvable. `can_admins_bypass` is left at GitHub's default, `true`, as it
was measured on `prod`.

**What a pull request proves about this, and what it cannot.**
`tests/unit/scripts/test_plan_guard_iam_classification.py` runs
`--classify-iam` on the fixtures in `tests/unit/scripts/fixtures/plans/`. A
plan that forgets a custom role must be classified IAM, and a plan that only
moves image digests must not. It also runs every owner-named family with every
changing action, and inputs that must be refused rather than answered. The
self-test (`plan-guard.sh --self-test`) carries the same cases.
`tests/unit/scripts/test_release_dev_iam_gate.py` schedules `release.yml` with
the model from `test_release_prod_gate.py`, trying every value the
classification can write. It checks four things:

* nothing applies or deploys after an IAM plan unless `dev-iam` is approved;
* a routine plan flows through `dev` untouched;
* an empty answer applies nowhere;
* the dev-iam job never starts on prod, and applies the checksummed saved
  plan without ever planning.

It cannot show that `dev-iam` has its reviewer (run the read-back above), nor
that GitHub and Terraform behave as documented. The first dev release with an
IAM change after this lands is that proof.

### A held dev-iam plan cancels every release behind it

**Measured 2026-10-11.** Release run 38115700349 (`3f6dbd26`, started 05:43Z)
stopped at `terraform apply, IAM (dev-iam)`. Its held plan carried a Cloud
Storage IAM condition written with `matches()`, which those conditions do not
support, so IAM would have refused it on approval (#1057 replaced it with
`extract()`). While it waited it held `release-dev`. 20 consecutive releases,
06:02Z to 08:40Z, ended `cancelled`, and nothing merged in that window reached
dev. Rejecting the held deployment at 09:0xZ let the next release, 38126514258, run.

**Why every release, not just its own.** `release.yml` sets
`cancel-in-progress: false`, so a running release is never displaced (two
applies would contend for the state lock). But GitHub keeps only **one**
pending run per concurrency group: each newer release cancels the pending one
before it. A waiting dev-iam job therefore turns every merge behind it into a
`cancelled` run. A string of `cancelled` dev releases, none of them red, is the
signature. Find the holder with:

```bash
gh run list --workflow release.yml --status waiting
```

**Reject instead of approving when** either holds:

* **main has moved past it.** The held plan was made from an older commit.
  Approving applies that old plan, or fails as stale, and the release behind it
  would plan from current main anyway.
* **its plan would fail.** Approval does not check the plan; IAM checks it at
  apply. Lint each IAM condition in the held plan's commit first with
  `iamPolicies:lintPolicy` (the summary's rows name role and member, not the
  condition), as `terraform/modules/iam/bindings.tf` records: it
  answered `ConditionCompileCheck`, severity `ERROR`, for the `matches()` form,
  and `{}` for the `extract()` one.

**Rejecting applies nothing.** The dev-iam job is the only one that applies a
held plan, and a rejection ends it before it starts, so `deploy` never runs and
the run ends failed. The pending release then takes the group and replans from
current main. If that plan still changes IAM, it holds again, now with a plan
worth reading.

## The release timeline

Owner decision 2026-10-08: cuts A, B and C of the release timing report. The
release used to hold dev's lock through everything that only *checks* a
deployment. Now it holds the lock only while it changes dev.

### What was measured

Over the 22 successful `release.yml` runs before 2026-10-08, the
`release-dev` concurrency lock was held **100 min p50**:

| stage | p50 |
|---|---|
| `verify` (the unit suite, shellcheck, the destroy-guard self-test) | 26.4 min |
| `deploy and smoke` | 23.6 min: warm 11.4, smoke 9-14, GKE proof 3.5 |
| `acceptance (dev)`, five groups one after another | 45.4 min: mock 8-12, generic 6-8, claude-code 4-7, workflow ~13, browser 2-3 |

New digests were live **43 min** after the run started. The other **71 min**
held the lock, and every later push queued behind it. `release-dev` is
`cancel-in-progress: false`, so each new push replaced the pending run.

### What changed

* **A. Acceptance has its own workflow.** On dev, `release.yml` ends at
  `deploy`, which verifies that every service, job and worker image runs the
  pinned digests and then applies the tenant namespaces. Warming, the smoke
  test, the GKE proof and the acceptance suite moved to
  `.github/workflows/accept.yml`. It starts when a release completes
  (`workflow_run`, on `main`), or by dispatch. Its deciding job
  (`what dev runs`) has no concurrency, so a completion that accepts nothing
  cannot cancel a real acceptance; only the accepting jobs carry
  `accept-dev-<job>` groups with **`cancel-in-progress: true`**, because only
  the newest deployed commit is worth accepting. A hotfix re-applying the
  same SHA is accepted. A GitHub cancel does not stop a Cloud Run execution,
  so a `stop an earlier acceptance` job cancels the running `swarm-verify`
  executions before the new run's first wave.
* **B. `verify` runs only on a dispatch.** On a push, `application.yml` runs
  the unit suite, shellcheck, the destroy-guard self-test and the manifests
  on the same SHA, and its `build images` job `needs: [python, shell,
  manifests]`. The release takes that job's image record with `--reuse-ci
  only` and refuses to build its own. So the record it ships cannot exist
  unless those checks passed on that commit. A dispatch has no such proof: a
  prod release, a dev dispatch that may build its own images (`or-build`), or
  a `skip_build` redeploy. So `verify` still runs on every dispatch, prod
  included. Each job that needs `verify` states one rule: `verify` succeeded,
  or a push skipped it. `test_release_prod_gate.py` holds that a dispatch
  whose `verify` did not succeed starts nothing that promotes, applies or
  deploys. On a push, the plugin tag waits for `images and scan`, which found
  CI's record.
* **C. The groups run in parallel**, as matrix jobs with `fail-fast:
  false`, one swarm-verify execution each. There were five when this was
  measured; six run now (git-tools joined, and live-browser left for its own
  schedule), in the two waves below.

### Six at once does not fit, so it is two waves

The suite submits for the `smoke` tenant. Its ceiling is **20** concurrent
agents: `max_active = capacity_units = 20` in `dev.tfvars`, `tenants.smoke`.
Each group's peak of agents in flight, read from its group file:

| group | peak | why |
|---|---|---|
| mock | ~13 | twelve agents and a two-step workflow, submitted before it waits |
| generic | ~8 | one agent per command row |
| workflow | ~4 | four workflow roots |
| claude-code | ~3 | three agents |
| browser | ~3 | three agents |
| git-tools | ~5 | its lane and a two-step build workflow, then the build pair beside the rebase lane and the reader |
| live-browser | 1 | one agent; on a schedule since 2026-10-14, not in a wave ([below](#live-browser-runs-on-a-schedule)) |

Without live-browser that is about 36, over the ceiling. An agent over the ceiling queues
rather than fails (invariant 1: `QUEUED` costs nothing). But every check has a
settle deadline, and time spent in the queue comes out of it. So the groups
run in **two waves**:

* **wave 1**: `mock`, `workflow` and `browser`, a peak of about 20;
* **wave 2**: `generic`, `claude-code` and `git-tools`, about 16. It starts
  when wave 1 ends, whatever wave 1 concluded.

`test_each_wave_fits_the_smoke_tenant_s_ceiling` reads the ceiling from
`dev.tfvars`. It fails if a wave's peaks exceed it, and also if all six
groups would fit in one wave.

### live-browser runs on a schedule

**Owner decision, 2026-10-14 (lane SPEED-C2): take the live-browser group off
the red path until it is stable.** Read that day: `accept.yml` had run 159
times; 43 runs completed and **21 of them failed (49%)**. The `acceptance
(live-browser)` job failed **18 of 34** runs; every other group failed 0-3
times. Each red run opens or updates the one acceptance `bug` issue, so most
of that issue's history was one group's noise, and a real regression in
another group read as more of the same.

So live-browser is in neither wave. `accept.yml`'s `live-browser` job runs it
**every three hours** (`schedule`, at :41) and on a dispatch with scope
`live-browser`. It reads what dev runs from `releases/dev/applied.json`, checks
that commit out and runs `verify-remote.sh acceptance/live-browser`, as the
waves would. `live-browser-report` files a red result on **its own** `bug`
issue: `scripts/accept-report.sh` with `ACCEPT_KIND=live-browser` finds that
issue by its own marker, never the post-release one. The scheduled run is not
an accepting job:

* it has no `target`, `quiesce`, smoke or sandbox sync, and cancels nothing;
* the post-release `report` does not need it, so it cannot turn that run red;
* its concurrency group is `accept-live-browser`, not an `accept-dev-` one.

A post-release run's `quiesce` cancels every other in-progress `accept.yml` run
and every running `swarm-verify` execution. That includes a scheduled
live-browser run in flight. A cancelled scheduled run files nothing and does
not count toward the streak. Its peak of one agent fits beside either wave if
the two overlap.

**Coming back.** `scripts/accept-live-browser-streak.sh` reads the completed
scheduled runs of `accept.yml` on `main`, newest first. It counts successes
until the first run that did not succeed. A cancelled or skipped run neither
counts nor breaks the streak. A dispatch is a probe and is not counted.
It prints `streak=`, `need=` and `promote=` and exits 0 at **five in a row**
(`--need` changes the number) or 3 below it. When it says `promote=true`, the
operator moves the group back:

1. put `live-browser` back in `acceptance-2`'s matrix in `accept.yml`;
2. remove the `schedule` trigger, the `scope` input, and the `live-browser`
   and `live-browser-report` jobs, and take `SCHEDULED_GROUPS` out of
   `tests/unit/scripts/test_release_acceptance_job.py`;
3. close the live-browser issue with that pull request.

The streak only says the group passed five times in a row. The causes below
are why it failed, and a later lane fixes them.

**Why it failed (14 failed live-browser jobs read, 2026-10-12 to 10-14).** Five
distinct causes, some in the same run:

| cause | runs | where |
|---|---|---|
| `returned`: the agent was still **`PARKED`** at the 900 s deadline, after the hand-off, the take-over and the typing had all passed | 5 (every run from 2026-10-14 00:24 on) | began with HANDOFF-POLICY (PR 1264), which parks a hold after one unwatched minute: `apps/swarm-api/swarm_api/handoff.py` (`HUMAN_UNWATCHED_SECONDS`, `opening_deadline`), the worker's park in `apps/agent-worker/agent_worker/lifecycle.py` (`park_human_required`). The scripted viewer (`tests/acceptance/test_live_browser.py`) holds control through the wait, so either it does not count as watching, or a returned agent is not woken from the park |
| `recording`: "2 object(s) under live/ are not recording segments" | 8 | **harness bug, fixed here.** The stray check matched any `/live/` in the object name, so it counted the worker's log tails `logs/live/agent_stdout.tail.log` and `agent_stderr.tail.log` (`agent_worker/runners/streams.py`). It now judges only `<attempt>/live/` (`stray_live_objects`) |
| `returned`: the agent succeeded, but its answer and `session-code.txt` do not hold the code the typed password makes | 4, plus 1 whose reason was lost (below) | the agent's second read of `#code` after `swarm-handoff wait` exits 0, through the live browser as BROWSER-ENFORCE (PR 1248) reshaped it: `images/agent-runtime-browser/swarm_browser.py`, `swarm-handoff` |
| `handoff`: the agent ended `SUCCEEDED` without asking for help | 3 (2026-10-12 to 10-13 03:18, none since) | the agent skipped `swarm-handoff request`; the prompt is `LIVE_BROWSER_PROMPT` in `scripts/acceptance/groups/live-browser.sh` |
| `handoff`: still `RUNNING` at the deadline | 1 | one run, 2026-10-12 14:52 |

One more harness bug blanked the reasons in all of these. 13 of the 14 logs
print `sed: unmatched '#'` once per check. `redact` (`scripts/lib/common.sh`)
is a `sed -E` program with `#` delimiters, and busybox sed cannot compile it.
The `swarm-verify` image is Alpine, so every reason piped through `redact`
came out empty. It failed closed: nothing leaked, but the reason was lost. The
image now installs GNU `sed` (`images/swarm-verify/Dockerfile`).

### The timeline now (expected, not yet measured)

| | before (p50, measured) | after (estimated from the same p50s) |
|---|---|---|
| `release-dev` lock, push | 100 min | about 100 − 26.4 (`verify`) − ~23 (warm, smoke, GKE proof) − 45.4 (acceptance) ≈ **5-10 min** past the wait for CI's image record |
| new digests live, from the push | 43 min | CI's `build images` + scan, promote, apply. `verify`'s 26.4 min is no longer in front of the apply |
| acceptance, after the deploy | 45.4 min, under the lock | warm 11.4 + smoke 9-14 + GKE proof 3.5 + wave 1 (~13) + wave 2 (~8) ≈ **45-50 min, outside the lock** |

These numbers are subtraction, not a measurement. The first week of runs is
the measurement. Re-measure the same way: the lock is the run's
`created_at` → `updated_at`, and the stages are the jobs' own times.

### It accepts exactly what dev runs

`what dev runs` reads the SHA from `releases/dev/applied.json`, through
`scripts/lib/release-order.sh deployed`. That is the record every successful
dev apply writes, from either lane. Every later job checks out **that
commit**, and the run's summary names it. A completed release is accepted
only when `applied.json` names that very run:

* a release that a newer commit superseded applied nothing, so it accepts
  nothing;
* a prod release records nothing for dev, so it accepts nothing either.

A dispatch accepts whatever dev runs now, e.g. after a hotfix. A trigger that
can accept nothing (a failed release, or a prod release, recognised by the
`release prod <ref>` run-name that `release.yml` now sets on a dispatch) is
put in a concurrency group of its own. It therefore cannot cancel a real
acceptance.

### The cross-tenant Firestore probe is report-only until the cutover

**Owner decision, 2026-10-20 (lane ISO-ACCEPT-WIRE, part of #1457): run the
probe on every acceptance run now, so the isolation cutover has its proof.**
Every tenant worker's account holds `swarmTenantWorkerFirestore` across the
whole `swarm` database, so an agent in one tenant's pod can read and write
another tenant's documents by id (#1457). Design B moves the worker's state
onto attempt-signed swarm-api routes and then removes that role
([tenant-settings-integrity.md](design/tenant-settings-integrity.md) §6).
[`test_cross_tenant_firestore.py`](../tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py)
is what says the role is gone. From inside a smoke worker, with that worker's
own credential, it asks Firestore to update `workspace_settings/<B>`, get
`tenants/<B>` and create under `leases/`. It passes only when all three are
answered 403 `PERMISSION_DENIED`. It writes nothing either way: each write
carries a precondition that cannot hold.

**It is red until the cutover, by design.** So `accept.yml`'s `acceptance
(tenant-isolation)` job has two modes, chosen by the repository variable
**`ISO_PROBE_REQUIRED`**:

| `ISO_PROBE_REQUIRED` | a red probe | the job | the acceptance issue |
|---|---|---|---|
| unset, or anything but `true` | **report-only**: its step is marked failed and a warning names each check's verdict | concludes green | not opened or updated: `report what was accepted` sees a success, and the job is not in the run's failing jobs |
| `true` | fails the job | concludes red | opened or updated, naming `acceptance (tenant-isolation)` |

Report-only is not a skip. The job runs and shows its result on every run;
it cannot file it. Only `report-only` is decided by the variable. The probe
and the group judge the same way in both modes. GitHub compares the value
without regard to case, so `TRUE` counts as `true`.

**How it runs.** The job starts beside wave 2, after wave 1, because the
probe is one agent and wave 1's peak is already 20 of smoke's 20. It runs
`verify-remote.sh acceptance/tenant-isolation`. The group submits one
`generic` pytest agent in `smoke` against the sandbox's copy of
`tests/acceptance/fixtures/tenant-isolation/`. Its `working_directory` is that
directory and `input.paths` names the probe, which is how the probe opts in:
the generic runner passes the agent's input to no environment variable. The
other tenant is a tenant id that does not exist, made up at run time. The role
is granted on the database, and no IAM condition can name a document, so IAM
answers the same for any id. The group passes only on exit 0 and three
`ISO-ACCEPT-RESULT` lines that say `PASS`. The probe writes each line to
stdout and to stderr: the generic runner keeps stdout's tail on exit 0 and
only stderr's tail (in the agent's `last_error`) on any other exit. A probe that could not measure (no metadata server, or an account
that is not a tenant worker) reports each check as `not measured`, which is
red, never a pass.

**What is on `main`, read 2026-10-20.** The probe's result lines (above) and
this section are. The `tenant-isolation` acceptance group under
`scripts/acceptance/` and the `accept.yml` job are not yet. ISO-ACCEPT-WIRE's
pull request carries both as patches, the group's first: a lane cannot push
workflow files, and the group lies outside that lane's territory. Until both
land, no acceptance run measures the probe.

**When it becomes required.** The owner sets `ISO_PROBE_REQUIRED=true` after
the last tenant is cut over, following `docs/runbooks/isolation-cutover.md`
(lane ISO-IAM-WIRE writes it). Before that, a green probe in report-only mode
is the evidence the cutover worked. A red probe after the variable is set is
a tenant-isolation regression: S0 under the issue rules in `CLAUDE.md`, though
the acceptance issue it lands on does not say so by itself.

### A red acceptance is an issue, not a blocked deploy

`report what was accepted` runs `scripts/accept-report.sh`. Every red run
opens **one** issue labelled `bug`, or retitles and comments on the one
already open. The issue names the SHA and every failing group, taken from
the run's job list (`acceptance (mock)`, …). It is written in the bug form's
sections. A green run changes nothing: the issue is closed by the change that
fixed it. A red run against a deployment that is already gone (the hotfix
lane applied a newer commit mid-run) is reported in the summary and not
filed. If `what dev runs` itself fails (an identity not yet bootstrapped, an
unreadable record), that is filed too.

The trade the owner made: a red acceptance no longer stops the next commit
reaching dev. It is reported, and the next push deploys regardless.

### Prod is unchanged

Acceptance never ran on prod. It spends subscription quota and opens real
pull requests on the sandbox. So there was no acceptance-before-prod gate to
keep, and none was added. A prod release still waits at `approval (prod)`.
Its `deploy` still warms, smokes and proves GKE inside the release, on steps
conditioned on `github.event.inputs.environment == 'prod'`. `dev-iam` still
holds a dev plan that changes IAM. `test_release_prod_gate.py` and
`test_release_dev_iam_gate.py` hold both gates.

### Its identity is not the deployer

`accept.yml` federates as **`vars.GCP_ACCEPT_SA`**, the account
`terraform/bootstrap/acceptance.tf` creates and binds to `accept.yml` on
`refs/heads/main` alone. It is **not** on `wif.tf`'s `deployer_workflows`, and
`test_workflow_id_token_scope.py` keeps it off. What the account can do:

* execute `swarm-verify` and the `swarm-job-*` worker jobs, read their
  executions and cancel a running one (the custom role
  `swarmAcceptanceRunner`, granted at project level **with no condition**:
  see below). The suites run *inside* the VPC as swarm-verify, under
  swarm-verify's own identity;
* list jobs and read operations, read-only;
* read the swarm-verify log view;
* read `releases/` in the state bucket, and no state object.

It cannot plan, apply, promote, write a release record, take the release lock
or touch IAM. **The owner applies it** (`make bootstrap`), then sets the
repository variable `GCP_ACCEPT_SA` to the `github_accept_service_account`
output. Until then `what dev runs` fails at authentication, and the report
files that as a red acceptance.

**The runner grant is unconditioned. Owner decision 2026-10-09 (#965).** It
was conditioned to the job names (`resource.name.endsWith("/jobs/swarm-verify")`,
`extract("/jobs/{job}").startsWith("swarm-job-")`), and that condition never
matched any job: Cloud Run does not expose `resource.name` to IAM Conditions.
Google's "Resource attributes for IAM Conditions" page lists no
`run.googleapis.com` resource ("Other services and resource types do not
recognize resource attributes"), and the IAM Policy Troubleshooter, measured
read-only on 2026-10-09, evaluates the `endsWith` clause as false even with the
job's name supplied. So `swarm-accept` was denied `run.jobs.get` on
swarm-verify and `warm, smoke and GKE proof (dev)` was red on every run.

The trade-off the owner accepted: in the **shared** project
`saga-agents-staging`, the grant reaches **every Cloud Run job, present and
future** — the other team's, and swarm's own merge job `swarm-job-eng-merge` —
not only swarm-verify and the worker jobs. What it allows on any of them is
the role and no more: get a job, start an execution (with overridden
arguments), read executions and agents, cancel a running execution. No create,
update, delete or IAM permission, so it cannot change what a job runs or who
may run it; `tests/terraform/bootstrap.tftest.hcl` holds both the absent
condition and those permissions. What bounds the account is that only
`accept.yml` on `refs/heads/main` can become it. Do not put a job-name
condition back: it denies every job.

The log-view and `releases/` grants keep their conditions: Cloud Logging and
Cloud Storage do evaluate `resource.name`. What only a live run proves is that
those two evaluate as documented; a condition that matches nothing shows as a
`Permission ... denied` in the step that reads the transcript or
`releases/dev/applied.json`.

## Hotfix releases

Owner decision 2026-10-08 (observer proposal H). A pull request labelled
**`hotfix`** ships to dev on its own lane, `hotfix.yml`, minutes after it
merges, instead of waiting behind the normal release.

### Why a second lane

Measured 2026-10-08 over 22 successful `release.yml` runs:

| | p50 | p90 |
|---|---|---|
| new digests live, from the run's creation | 43 min | 97 min |
| `release-dev` concurrency lock held | 100 min (verify 26, deploy + smoke 24, acceptance 45) | |
| queue wait before the run starts | 10 min | 56 min |

78 of the last 100 release runs were cancelled while pending, superseded by a
newer push: `release-dev` is `cancel-in-progress: false`, so it holds one
running release and one pending, and each new push replaces the pending one. A
hotfix (#857) waited about 1 h 45 min to reach dev.

### When to use the label

Put `hotfix` on the pull request **before it merges**, for a fix dev needs now:
a broken dispatch path, a crash loop, a release that left dev unusable. Not for
routine work -- the normal release still runs for every push, and it is the one
that runs acceptance. The label is read from the pull request the push to
`main` merged (`GET /repos/{repo}/commits/{sha}/pulls`,
`scripts/lib/hotfix-gate.sh`): only a pull request **merged into `main`** with
the label counts. A label added after the merge does nothing until the lane is
dispatched by hand on `main` (`gh workflow run hotfix.yml --ref main`), which
reads the label of the commit at `main`'s head.

### Why it is a workflow of its own

* GitHub evaluates `concurrency:` **before any job**, so nothing a run learns
  -- a label -- can move it out of `release-dev`. `hotfix.yml` is in
  the `hotfix-dev` group is on its work jobs only. The workflow runs on every
  push that would start a release, and its first job, `is this a hotfix`,
  decides; every other job runs only on its answer. A commit without the
  label ends there, green, in seconds.
* **The gate is deliberately outside the group.** GitHub keeps one running
  plus one pending run per group and cancels the older pending one when a
  newer arrives. With the group on the workflow, an ordinary push's gate-only
  run would displace a queued hotfix, which would then silently never run.
  A job the gate skips never enters the group. The ordering between hotfixes
  is held by `scripts/lib/release-order.sh`, not by the group.
* A re-run of an old release, or a dispatch whose SHA is older than
  `applied.json`, ends green as "superseded" and changes nothing on dev too.
  A deliberate dev rollback therefore needs the documented path, not a re-run.
* It runs on `main` only, never on a pull request: workload identity admits
  only `refs/heads/main` (the comment on `application.yml`'s `build images`
  auth step), and a pull request's ref is refused by the pool's condition.
* It authenticates as the deployer **as `hotfix.yml`**, which is therefore on
  the deployer's trust pin (`terraform/bootstrap/wif.tf`,
  `deployer_workflows`). That pin is IAM in the bootstrap root, which only the
  owner applies; until that apply the lane's first authenticating job fails
  with `Permission 'iam.serviceAccounts.getAccessToken' denied`, having
  changed nothing.

### What it runs, and that it is the same thing the release runs

`release.yml` and `hotfix.yml` run each stage through the same composite
actions, so the two cannot drift:

| stage | `.github/actions/` | what |
|---|---|---|
| images | `release-images` | the record `application.yml` built for this SHA (`--reuse-ci only`, never a build of its own), every digest scanned with `--scan-only` |
| promote | `release-promote` | scan again and move `:dev` to exactly those digests, all or nothing |
| apply | `release-apply` | pin by digest, plan, shared-project guard (`plan-guard.sh`), IAM classification, apply |
| verify | `release-verify` | `deploy.sh --verify-only`: every service, job and worker image runs the pinned digests |
| namespaces | `release-namespaces` | tenant namespaces and their step-spec keys, when `vars.APPLY_TENANT_NAMESPACES` is on |

then the smoke test inside the VPC. **Composite actions, not a reusable
workflow** (the first design asked for `release-steps.yml` with
`workflow_call`): the deployer trusts workflow *files* by `job_workflow_ref`,
and for a reusable workflow that names the called file. The called file would
have to be on the trust pin and callable at once, so any workflow on `main`
granting `id-token` could call it and mint the deployer -- exactly what
`test_no_deployer_pinned_workflow_is_callable` (#457) forbids. A composite
action runs inside the caller's job, so the token still names `release.yml` or
`hotfix.yml`. Every gate -- `approval`, `dev-iam`, each job's `if:` -- stays in
the workflow files, and the release tests flatten each composite action into
the job that runs it (`_workflow` in
`tests/unit/scripts/test_release_reuses_ci_images.py`), so
`test_release_prod_gate.py` still reads every prod-facing step.

### What it skips, and why that is safe

* **`verify`**. `application.yml`'s `build images` needs `[python, shell,
  manifests]`, so CI's image record for this SHA cannot exist unless its unit
  tests, shellcheck and manifests passed -- and the images stage refuses to
  build anything itself.
* **Warming the worker jobs, acceptance, the GKE proof and the plugin tag.**
  The same push still starts the normal release, which tags the plugin. When
  that release completes, `accept.yml` warms, smokes, proves GKE and accepts
  what dev then runs ([above](#the-release-timeline)). Acceptance is
  **delayed, not dropped**: a defect the hotfix put on dev is filed as the
  acceptance issue, as it would be for any commit.
* **Any change to IAM.** A plan that changes IAM **fails** the hotfix lane
  before anything is applied, with "the change must go through release.yml
  and the dev-iam gate". The hotfix never applies IAM, so the owner's
  `dev-iam` gate is untouched; the normal release holds that plan for it as
  always. (The hotfix's promote has already moved `:dev` by then; the normal
  release promotes the same digests again.)
* **prod.** Every hotfix job is pinned to `ENVIRONMENT: dev` and names no
  environment.

### The two lanes never move dev backwards

The lanes are in different concurrency groups, so GitHub no longer serialises
them. `scripts/lib/release-order.sh` does, through the state bucket:

* **One lock**, `gs://$TF_STATE_BUCKET/releases/dev/apply.lock`, created with
  `--if-generation-match=0` (create only if absent, atomically). Each promote,
  each apply (from before its plan to after its apply) and the dev-iam apply
  takes it; a lane waits up to 30 minutes for it; it is released by
  generation in an `always()` step, and a lock older than an hour -- a runner
  that died holding it -- is broken.
* **Two records**, `releases/dev/promoted.json` and `applied.json`: the SHA
  the last successful promote and apply shipped.
* **The rule**: under the lock, a lane proceeds only if every recorded SHA is
  an ancestor of (or equal to) its own commit. Otherwise a newer commit is
  already out, and the lane is **superseded**: it moves nothing, applies
  nothing, and ends **green** -- the newer commit contains this one. The
  check runs before promote too, so `:dev` never moves backwards, and again
  at the start of the deploy; a verification or smoke test that fails
  *because* a newer commit reached dev meanwhile also ends green
  (`release-order.sh unless-superseded`). `accept.yml` accepts a completed
  release only when `applied.json` names that run, so a superseded release
  is never accepted.
* A hotfix's apply waits up to 900 s for Terraform's own state lock
  (`-lock-timeout=900s`), where a release waits 180 s.
* **Not on prod.** A prod release is a person's choice of commit behind
  `approval (prod)` and may be a deliberate rollback; ordering it would refuse
  exactly that. The comparison needs `main`'s history, so every job that
  orders checks out with `fetch-depth: 0`; a shallow checkout is refused, not
  guessed.

In the usual case the hotfix lands first and the normal release of the same
commit follows it -- same SHA, so it proceeds, re-applies the same digests, and
its completion starts `accept.yml`. The normal release of an *older* commit that reaches its
promote or apply after the hotfix stops there, superseded.

### A build a newer push cancelled ends the hotfix green

Since #2438 (2026-10-26) `application.yml` runs all of `main` in one
concurrency group with `cancel-in-progress`, so a push to `main` cancels the
older commit's run before its `build images` job finishes. The hotfix waits
for exactly that job (`--reuse-ci only`), so its images stage then fails
"never built". The `after` step on `images` (release.yml's step of the same
name, failure path only) reads that one case as **superseded**: `main` has
moved past the commit **and** `application.yml`'s push run for it concluded
`cancelled` without `build images` succeeding or failing. The run ends green,
its summary names `main`'s newest commit -- whose build includes the change
and whose release ships it -- and promote, apply and deploy do not run. Any
other failure, or an API that cannot be read, stays red.

**There is no `newest` check**, unlike `release.yml`. A hotfix whose images
were built and scanned still reaches dev before the newer commit's normal
release, which waits for its own CI build and then for `release-dev` (an
estimated 5-10 min past that wait now that `verify` and acceptance left the
lock, "The timeline now" above; 100 min p50 before). The gap is smaller than it
was, but it is still the hotfix's whole reason to exist, and superseding it
whenever `main` had moved on would drop nearly every hotfix on a busy `main`
for nothing: `release-order.sh` already ends the lane green if a newer commit
reached dev first. The `after` step also reads `main`'s head, a call
release.yml's step does not make, so the summary can name the commit that
ships the change. Only a build that never happened
leaves the lane nothing to ship.

### What the tests hold, and what they cannot

`tests/unit/scripts/test_hotfix_release.py`: the gate proceeds only for a
merged, labelled pull request into `main` and nothing that promotes, applies
or deploys starts otherwise; an IAM plan fails the hotfix and is still held
for dev-iam by the release; `release-order.sh` refuses a non-ancestor, its
lock is exclusive and released by generation; release.yml keeps its prod gate
and its dev-iam gate; both lanes run the same stages and pin the same tools.
`tests/unit/scripts/test_hotfix_superseded.py` runs the `after` step against a
fake `gh`: a build a newer push cancelled ends green and promotes nothing,
every other failure stays red.
They cannot prove that GitHub runs the workflows as read, that GCS's
generation precondition is atomic (it is documented to be), or that the
deployer admits `hotfix.yml` before the owner applies the bootstrap root. The
first labelled merge after that apply is the proof.

## The plugin's bridge tag is made by the release

[`plugin/.claude-plugin/plugin.json`](../plugin/.claude-plugin/plugin.json)
names its version twice. One is `version`, which decides which skills a user
has. The other is the `@sc-v<version>` ref its MCP server installs the swarm-mcp
bridge from.
[`tests/unit/mcp/test_plugin_bridge_install.py`](../tests/unit/mcp/test_plugin_bridge_install.py)
holds the two equal. But the tag itself used to be made by hand, so it could
be missing. sc-v0.5.6 was: main pinned it before anyone had tagged it, every
fresh install of 0.5.6 failed to fetch its bridge, and no check said so (#272).

**Who tags, and when.** `release.yml`'s `plugin-tag` job creates the annotated
tag `sc-v<version>` on the commit being released (`github.sha`). It reads the
version from `plugin.json` with `jq` at run time, so the workflow never names
a version. It refuses anything that is not `X.Y.Z`.

* **After `verify`.** A tag is what users' installs resolve, so a commit whose
  unit tests failed is not one to hand them.
* **On main only.** A `workflow_dispatch` can name another branch, and a tag
  made from it would pin users to a commit main never had. The condition sits
  on each step, not on the job. The release-gate model in
  `test_release_prod_gate.py` cannot evaluate a job-level `github.ref`, and a
  condition it cannot evaluate is one it cannot hold. Off main, the job starts,
  runs nothing and ends green.
* **Only when origin does not have the tag** (`git ls-remote --exit-code`).
  If the remote cannot be read, the job fails. That is never treated as
  "absent".

**It never moves an existing tag.** Users' installs already resolved it, and uv
caches what it fetched. A moved tag would give two users of the same version
two different bridges, and neither of them could tell. So nothing in the job
forces. When the tag exists, the job prints a `::notice`, also naming the
commit the tag points at if that is not this one, and leaves it where it is.
If two releases tag at once (a dev push and a prod dispatch), the second push
is rejected. The job then reads the remote again, and counts the tag being
there as success.

**`contents: write` is granted on that job, not on the workflow.** The workflow
default stays `contents: read`. The jobs that build, plan and deploy run a lot
of code, and none of it needs to write to the repository. `plugin-tag` runs
only checkout, `jq` and `git`. (`acceptance` held the same grant for its own
sweep until #628 moved its pull requests to the private sandbox; it is
`contents: read` now, and the sweep uses the sandbox's own token.)

**A release a newer commit superseded tags nothing** (2026-10-26): it may
never have found CI's record, which is the tag's proof on a push. The newer
commit's release tags the same version on the commit it ships, whose
`plugin.json` says the same version. (Until 2026-10-26 a bump that touched only
`plugin/` started no release at all, because `release.yml` had a path filter.)

**The check.** On a push to main, `application.yml`'s `shell` job asks whether
origin has `sc-v<version>`.

* **Passes** when the tag exists.
* **Fails** when the tag is missing and a completed `release.yml` run on main
  whose `verify` job passed already descends from the commit that last changed
  the version line. `plugin-tag` runs exactly when `verify` passed, so that
  release should have tagged it and did not. The error names the tag, the bump
  commit, that run and its `plugin-tag` job's conclusion.
  **The run's own conclusion is not read.** A `plugin-tag` that failed (a
  rejected push, a ruleset on `sc-v*`), or any later build or deploy job that
  failed, makes the whole run `failure`. Counting only successful runs would
  warn forever in exactly the case this check exists for.
* **Only warns** when no release on main has passed `verify` since the bump
  (the plugin-only case above, a release still in flight, or one whose
  `verify` failed and so never tried to tag). The next release tags it.
* **Fails** if `gh run list`, `gh run view` or `git ls-remote` fails. A check that could not
  be made is not "no releases".

It prints how many release runs it examined. A release head missing from the
clone counts as not descended, and the check says so. It is gated to main
because a pull request that bumps the version is expected to have no tag yet.
It deepens the shallow checkout only on the path where the tag is missing. For
`gh`, the `shell` job holds `actions: read` along with the `contents: read`
that job-level permissions would otherwise drop.

[`tests/unit/scripts/test_plugin_tag.py`](../tests/unit/scripts/test_plugin_tag.py)
runs both steps' `run:` text, read out of the workflow files, against stub
`git` and `gh`. It covers the tag being created and pushed, the tag never being
moved, nothing ever being forced, a bad version being refused, and each of the
check's outcomes. It cannot show that GITHUB_TOKEN may push an `sc-v*` tag in
this repository. The first release after this lands is that proof.

## The UI job's Node is read from the image, not pinned

The `ui` job does not name a Node version. Its first step reads the major from
`ARG NODE_IMAGE` in [`images/swarm-ui/Dockerfile`](../images/swarm-ui/Dockerfile)
and hands it to `setup-node`. The step fails unless it finds exactly one such line.

The step after `setup-node` checks that the Node on `PATH` has that major, and
fails if it does not or if the major arrived empty. Without that check, an empty
value would read as success. `setup-node@v5` treats an empty `node-version` as
"no version given". It installs nothing and prints no warning, so the typecheck,
the tests and the build would all run, and pass, on whatever Node the runner
image ships. An empty value is what you get if the output name the first step
writes and the name `setup-node` reads ever stop matching.

It used to say `node-version: "20"`, while the image said `node:20` separately.
Two statements of one number is the drift
[`mirrored-values.md`](mirrored-values.md) exists to record. By September 2026
both copies named a line that reached end of life in April 2026, after
[`versions.md`](versions.md) had moved the platform to Node 24 LTS. The image now
pins `node:24-bookworm-slim` at the same digest `agent-runtime-base` pins, and a
bump there moves CI with it.
[`tests/unit/scripts/test_ui_node_line.py`](../tests/unit/scripts/test_ui_node_line.py)
fails if the job gets a literal back, if the two Node images disagree on the
major, or if the job stops running the production build. It also *runs* both
steps. The first runs against a Dockerfile moved to a major that appears nowhere
else, and must write that major under the name `setup-node` reads. The second
runs against a stand-in `node`, and must fail on a different major and on an
empty one.

Which Node line is *current* is not something a test can know. That is a fact
about a date, and it is decided in [`versions.md`](versions.md).

## What the gate's own identity, swarm-verify, may read

The in-VPC gate's suites run as `swarm-verify`
([`terraform/infra/verify.tf`](../terraform/infra/verify.tf)). Every grant is
one a suite actually exercises, all of them are reads, and every write goes
through `swarm-api`:

| grant | scope | why |
|---|---|---|
| `roles/run.invoker` | the `swarm-api` service | every suite submits and reads through the API |
| `roles/datastore.viewer` | project | the suites read agent, lease and pool documents directly (`fs_get`, `fs_count`) |
| `roles/run.viewer` | project | invariant 1 is asserted by counting Cloud Run executions |
| `roles/storage.objectViewer` | the artifact bucket | smoke proves artifacts land under the tenant's own prefix (invariant 9) |
| `roles/container.clusterViewer`, **conditioned** | project, to `projects/<p>/locations/<region>/clusters/swarm-autopilot` exactly | e2e [10] reads the cluster's endpoint and CA. Granted by **terraform/bootstrap**, which the owner applies ([`verify_cluster_read.tf`](../terraform/bootstrap/verify_cluster_read.tf)) |
| Role `swarm-verify-reader`: `get`, `list` on `jobs.batch` | the `eng` and `smoke` tenant namespaces only ([`kubernetes/rbac/verify-rbac.yaml`](../kubernetes/rbac/verify-rbac.yaml)) | e2e [10] reads a GKE-backed holder's Job |

**The last two are owner decisions of 2026-10-20 and 2026-10-21 (#1754,
VERIFY-GKE-READ and VERIFY-GKE-READ-2).** e2e [10], "no task is holding
capacity against an execution that is gone", compares every agent holding
capacity with the backend its attempt names. Before the two grants it could
read Cloud Run only, and the 2026-10-20 baseline reported ten eng agents
running on GKE as holders "not checked: no GKE read". The gate could not see a
dead GKE Job holding a slot, which is the thing [10] exists to see.

They are narrow on purpose, because this project is shared and holds the other
team's `agents-staging` cluster:

* **clusterViewer, not container.viewer.** clusterViewer is
  `container.clusters.get` and `.list`, which read the cluster's endpoint and
  CA and no Kubernetes object. container.viewer would read objects in every
  cluster: Pods and their env, ConfigMaps, Secrets' metadata.
* **Conditioned to our cluster.** GKE has no per-cluster IAM resource, so a
  project binding with a condition on the cluster's resource name is the only
  scope there is, as for the dispatchers' grants in `modules/iam`. The
  expression names the cluster exactly, or a path under it. A bare
  `startsWith` would also admit any cluster whose name began with ours.
  `tests/terraform/verify_gke_read.tftest.hcl` holds the role, the member, the
  expression, and that no other project-level container role reaches
  swarm-verify.
* **Granted by the owner, never by CI.** The grant is in
  `terraform/bootstrap`, applied by hand with `make bootstrap`, not in
  `terraform/infra`, which the release's deployer applies. The deployer's
  scoped `projectIamAdmin` may modify only the roles on
  `deployer_grantable_project_roles`
  ([`deployer_conditions.tf`](../terraform/bootstrap/deployer_conditions.tf)),
  and `hasOnly()` limits **which roles** a change touches, never the member or
  the condition. Had the grant been CI's, clusterViewer would have to be on
  that list, and then any CI run could grant it unconditioned, to itself or
  anyone, and reach `agents-staging`'s endpoint and CA. So the role is not on
  the list, and `tests/terraform/deployer_iam.tftest.hcl` fails if any
  `roles/container.*` role joins it. `verify_gke_read.tftest.hcl` also fails
  if `terraform/infra` (or a module it calls) grants swarm-verify any
  container role at all. The owner applies the one new binding with
  `make bootstrap` before the release that first runs the GKE read. Until
  then [10] fails on the refused cluster read (below), which is what it should
  do.
* **The Job read is Kubernetes RBAC, in our cluster only.** `get` and `list`
  on `jobs.batch`: no pods, no logs, no secrets, no ConfigMaps, no `watch`, no
  write. RBAC cannot narrow a read to fields, so a Job read returns the Job's
  pod template too, env included. That env holds task metadata, the public
  step-spec keys and a KMS key name, and never a credential: the worker
  templates carry no `secretKeyRef`, and provider keys and forge tokens are
  read from Secret Manager at run time. swarm-verify can already read the task
  documents behind that metadata through `datastore.viewer`. The Role and its
  binding exist only inside swarm-autopilot, so they cannot reach
  `agents-staging`. The binding names swarm-verify by email **and by numeric
  uniqueId**, because the gate authenticates with an OAuth access token and
  GKE presents a service account on that path by uniqueId
  (`dispatcher-rbac.yaml` has the 2026-09-24 measurement).
* **In eng and smoke only.** `render.py` renders the read in the `eng` and
  `smoke` tenants' own namespaces and nowhere else
  (`render.verify_read_wanted`): no other tenant, no personal
  `swarm-tenant-u-*` namespace, no group workspace, even when asked. The
  workspace deployer's scope is not widened for it:
  `policies/workspace-provisioner-scope.yaml` admits from the deployer only
  the Roles and subjects its render already held. A GKE holder in any
  namespace without the read is reported "not checked", never failed.
* **`kubernetes/apply.sh` resolves the uniqueId itself.** For an eng or smoke
  render it looks swarm-verify's uniqueId up with `uid_of`, exactly as it does
  the scheduler's and the reconciler's, and passes `--verify-uid` to the
  render. A missing account is fatal, as it is for the scheduler. Elsewhere it
  does not look the account up at all, so the workspace deployer, which runs
  the same script, makes no new call. `--verify-uid` on `apply.sh`'s own
  command line is refused, in full or abbreviated. A typed value would win
  over the resolved one. So binding the read is:

  ```bash
  kubernetes/apply.sh --tenant eng --confirm
  kubernetes/apply.sh --tenant smoke --confirm
  ```

  `render.py` still renders the read only when it is given `--verify-uid`. An
  email-only binding would authorise nobody on the gate's path, and this
  keeps the read out of `register-tenant.sh`'s A9 check, which re-renders a
  namespace and expects every object to exist.

**A refused cluster read now FAILS [10]**, where before it was a skip. The read
is granted, so a 403 on it means the grant is not applied or has been removed.
A gate that skipped it would pass with the grant gone, and that silence is what
[10] exists to end. An unreachable cluster, or a refused Job read (a namespace
without the RBAC), is still "not checked".

## The release reads one job's log, and the owner applies the grant

The in-VPC gate (`scripts/verify-remote.sh`, run by `release.yml` as
`swarm-tf-deployer`) used to report only *that* a target failed. Release
36038727721 printed `smoke-test FAILED (exit 1)` and nothing else; the two
failing cases were in the job's own stdout, in Cloud Logging, and it took a
second person to find them. The script now prints that transcript on a failed
target. The deployer could not read it: none of its 18 project roles carries a
permission that reads a log entry (each checked with `gcloud iam roles
describe` on 2026-09-24).

**Owner decision, 2026-09-24: the deployer may read the `swarm-verify` job's
logs and no others.** This project is shared, its logs include the other
team's, and a transcript is where a secret turns up, so `roles/logging.viewer`
was never an option. What implements it, in
[`terraform/bootstrap/verify_logs.tf`](../terraform/bootstrap/verify_logs.tf):

| piece | value |
|---|---|
| log view | `swarm-verify` on the `_Default` bucket (location `global`), filter `resource.type="cloud_run_job" AND resource.labels.job_name="swarm-verify"` |
| grant | `roles/logging.viewAccessor` to the deployer, condition `resource.name == "projects/saga-agents-staging/locations/global/buckets/_Default/views/swarm-verify"` |
| read | `gcloud logging read … --bucket _Default --location global --view swarm-verify`, which sends that view to `entries.list` as the resource read |
| refusal | `var.deployer_roles` refuses every predefined role measured to read log entries ([`log-reading-roles.json`](../terraform/bootstrap/log-reading-roles.json): 50 of 2,397 on 2026-09-24, `roles/iam.securityReviewer` and seven `roles/firebase.*` roles among them), and every role not on the reviewed list in `verify_logs.tf` |

It replaces PR #50's design, which copied the job's lines through a sink into a
`swarm-verify-logs` bucket. That was never applied: on 2026-09-24 the project
had only `_Default` and `_Required` buckets and sinks, and the owner's bootstrap
state held no logging resource. The view keeps no second copy of a transcript,
shows every execution still in `_Default`'s 30 days from the moment it exists,
and is a resource IAM can name, so the `configWriter` condition, once applied,
keeps CI from editing it. IAM has no name for a sink, so no condition can stop
CI rewriting one.

It lives in the bootstrap root because grants to the deployer come from the
root the owner applies, never from the root the deployer applies to itself.
**CI does not apply it and a merge changes nothing live.**

### The apply step, for the owner

```bash
make bootstrap    # scripts/bootstrap.sh: init, plan to build/bootstrap.tfplan, typed "apply"
```

Before typing `apply`, the plan must show these two creates for this change,
and no logging destroy:

```text
+ google_logging_log_view.verify
+ google_project_iam_member.deployer_reads_verify_logs[0]
```

**`make bootstrap` applies everything pending in the root, not only this.** On
2026-09-24 the owner's local bootstrap state still held `roles/storage.admin`
as an unconditioned grant, so the same plan also carries the storage.admin
scoping in `wif.tf`. The comment there says that change is applied only
*between* releases. To apply only the log view and its grant:

```bash
scripts/bootstrap.sh \
  --target google_logging_log_view.verify \
  --target 'google_project_iam_member.deployer_reads_verify_logs[0]'
```

That is the same script `make bootstrap` runs, with the plan limited to the two
addresses: the same pinned terraform, the same `init`, the same typed `apply`,
and it says before the plan and at the prompt that the plan is partial. The
second address is quoted because zsh reads `[0]` as a glob. Do not paste a bare
`terraform -chdir=...` instead: on the owner's workstation bare `terraform` is
Homebrew's 1.3.6, which this root refuses (`required_version >= 1.9.0`); the
scripts resolve the pinned 1.16.2 in `~/.local/bin` through `tf` in
`scripts/lib/common.sh`. `tests/integration/test_bootstrap_target.py` holds the
flag to reaching the plan and to an apply of that saved plan.

### What is proven, and what only a failed release will prove

`tests/terraform/verify_logs.tftest.hcl` holds the configuration to the
decision: the filter names only `swarm-verify` and cannot widen, the grant names
exactly the view the root creates, no predefined role this root grants the
deployer is a measured log reader, and `var.deployer_roles` refuses both a
measured reader and a role nobody reviewed. That is about what this root
*grants*; the next section is about what the deployer can *reach*.
`tests/integration/test_verify_remote_prints_the_job_log.py` reads the grant and
the view's filter out of the terraform and runs the script as an identity that
may read that one view.

Neither proves what Google will do, and this repository has already shipped
two IAM conditions written from documentation that matched nothing: the IAP one
and the exact-bucket `storage.admin` one, both in `wif.tf`. Three things are
documented and not yet observed:

* **the condition's form.** It is copied from the Logging guide's "Control
  access to a log view" and from IAM's resource-attribute table, which agree;
* **that `viewAccessor` is enough.** `entries.list` documents
  `logging.views.access` on the view as sufficient;
* **that the view filter may test a label.** The Logging guide calls this a
  *flexible* filter and its release notes date it 2026-04-02. The REST reference
  and the pinned provider's schema still describe the older rule (`SOURCE()`,
  `resource.type`, `LOG_ID()` only). If the service holds to that, the *apply*
  fails on the view, at the owner's terminal, before any grant exists.

**The condition is proven only when a failed release prints the log.** The
first failed gate after the apply shows either the transcript or the refusal
with gcloud's own error. The gate's verdict is the same either way.

### What the view does not bound: the deployer can reach every log

**"swarm-verify only" is what this grant gives. It is not what the deployer can
read**, and no condition written in
[`deployer_conditions.tf`](../terraform/bootstrap/deployer_conditions.tf)
makes it so, even with every scopable role scoped. Measured on 2026-09-24 with
read-only gcloud (`projects get-iam-policy`, `iam roles describe`,
`iam service-accounts get-iam-policy`); none of these routes has been
exercised, and this is what was found, not proof that there is nothing else.

| # | route to every log in the project | closed by a condition? |
|---|---|---|
| 1 | The deployer holds `roles/iam.serviceAccountUser` on `209012342332-compute@developer` (`wif.tf`, what `gcloud builds submit` runs as). That account holds **`roles/editor`**, which carries `logging.logEntries.list`. A build step or a Cloud Run job running `gcloud logging read` as it reads everything. Needs nothing CI does not already hold. | **no** |
| 2 | `roles/iam.roleAdmin` (unscopable) carries `iam.roles.update`. CI can add `logging.logEntries.list` to a custom role it holds: `swarmSecretProvisioner` (its scoped type guard admits every non-secret resource), `swarmDeployerProjectBuckets` (always unconditioned in `wif.tf`), or one of the custom roles the scoped `projectIamAdmin` still lets it grant itself. | **not by a condition, which cannot scope `roleAdmin`; closed by removing it** (#79, owner decision 2026-09-25). `roleAdmin` is off `deployer_roles` and a validation refuses it, and every custom role is defined in [`terraform/bootstrap/platform_roles.tf`](../terraform/bootstrap/platform_roles.tf), which the owner applies, so CI can change no role's permissions. **Closed and applied** 2026-09-28 23:21Z by the owner's bootstrap apply, which destroyed the live binding; read 2026-09-29: the deployer holds no `roles/iam.roleAdmin` ([runbook](runbooks/custom-roles-to-bootstrap.md#what-actually-happened-dates)). |
| 3 | `roles/iam.serviceAccountAdmin` (unscopable) carries `iam.serviceAccounts.setIamPolicy`. CI can grant itself `serviceAccountTokenCreator` on an account that reads logs (the compute account above, or `209012342332@cloudbuild`, which holds `roles/cloudbuild.builds.builder`) and act as it. | **not by a condition, which cannot scope IAM resources; closed by granting the role per account** (#334, owner decision 2026-09-29): only on the accounts `terraform/infra` manages, none of which reads logs ([below](#the-deployers-service-account-grants)). **Closed once applied.** |
| 4 | `roles/logging.configWriter` keeps sinks and exclusions project-wide even when scoped. A sink can route every log to a `swarm-` bucket (`storage.admin`) or a Pub/Sub topic (`pubsub.admin`) that CI reads. | **no** |
| 5 | `roles/logging.configWriter` unconditioned holds `logging.views.update`: CI can rewrite this view's filter, or make another view, and read the result through the grant. | yes, once `roles/logging.configWriter` is in `deployer_scoped_roles` |
| 6 | `roles/resourcemanager.projectIamAdmin` unconditioned lets CI grant itself `roles/logging.viewer`, or any other role that reads logs. Scoped, it may modify only the roles terraform/infra grants: 15 since #1430 turned `swarmGithubHooksDedup` on (14 from 2026-09-25, when `swarmSecretLister` left the list (#69) because its project-wide `secrets.setIamPolicy` reaches the other team's secrets and `terraform/bootstrap` now makes the broker's grant of it). None of the 15 reads a log entry: the nine predefined ones are absent from [`log-reading-roles.json`](../terraform/bootstrap/log-reading-roles.json), the five custom ones carry no `logging.` permission ([`platform_roles.tf`](../terraform/bootstrap/platform_roles.tf)), and `swarmGithubHooksDedup` holds `datastore.entities.create` alone ([`github_hooks_role.tf`](../terraform/bootstrap/github_hooks_role.tf)). | yes, once `roles/resourcemanager.projectIamAdmin` is in `deployer_scoped_roles` **and** route 2 is closed. Before route 2 closes, `roleAdmin` could widen one of the grantable custom roles, or add `resourcemanager.projects.setIamPolicy` to a role CI holds unconditioned, and the condition would never be evaluated. Route 2 closed with the owner's bootstrap apply of 2026-09-28 in [the runbook](runbooks/custom-roles-to-bootstrap.md#what-actually-happened-dates). |

What bounds routes 1 and 4 today is the ref pin, not IAM: only a workflow
on `refs/heads/main` can mint the deployer's token, so each route has to be
merged to `main` first. The same held for 3, 5 and 6 until each is applied,
and for 2 until the owner's bootstrap apply of 2026-09-28 closed it.
Closing 1 means building as an account without `roles/editor`; 4 means moving
sink management out of CI. Each changes what CI can do, none is made here,
and which to make is the owner's decision. Route 3 was closed by the owner's
decision on #334: `roles/iam.serviceAccountAdmin` is granted on each
`terraform/infra` account instead of the project. Route 2 was closed by the owner's decision on #79: no
resource-level grant exists for `roleAdmin` to be narrowed to (a role carries
no allow policy of its own; the project's testable permissions include no
`iam.roles.setIamPolicy`, read 2026-09-25), so it came off the deployer and the
custom roles moved to the root the owner applies.

## The deployer's project-level roles

`swarm-tf-deployer` holds these predefined roles project-wide
(`deployer_roles` in
[`terraform/bootstrap/variables.tf`](../terraform/bootstrap/variables.tf)).
A role marked scopable has a conditioned grant waiting in
[`deployer_conditions.tf`](../terraform/bootstrap/deployer_conditions.tf) and
switches to it when named in `deployer_scoped_roles`; the reasons each
unscopable one stays wide are recorded there, with what sat in its reach.

| role | scope |
|---|---|
| `roles/artifactregistry.admin` | unscopable: Artifact Registry is absent from IAM's resource-attribute list |
| `roles/cloudbuild.builds.editor` | unscopable: builds are named by server-generated UUID |
| `roles/cloudscheduler.admin` | unscopable |
| `roles/compute.networkAdmin` | scopable in part |
| `roles/compute.securityAdmin` | scopable in part |
| `roles/container.admin` | scopable |
| `roles/datastore.owner` | scopable |
| `roles/iam.serviceAccountCreator` | unscopable: creating is checked on the project; create, get and list only |
| `roles/logging.configWriter` | scopable in part |
| `roles/monitoring.editor` | unscopable |
| `roles/pubsub.admin` | unscopable |
| `roles/resourcemanager.projectIamAdmin` | scopable, by the roles a change modifies |
| `roles/run.admin` | unscopable |
| `roles/serviceusage.serviceUsageAdmin` | unscopable: one set of services per project |

**Not held, and refused by a validation:** `roles/owner`, `roles/editor`,
`roles/iam.roleAdmin` (#79, 2026-09-25) and, since 2026-09-29,
**`roles/iam.workloadIdentityPoolAdmin`** (owner decision, from the security
review of contract request 30, #314). `terraform/infra` manages no workload
identity pool or provider: the only pool it names is GKE's
`<project>.svc.id.goog`, and only as a string inside the member of a
service-account binding. So CI never used the role, and holding it let CI add
a provider to `swarm-github` that mints tokens for `swarm-ci-fix`, or for the
deployer itself, from outside the WIF ref pin, or to the other team's
`github-actions` pool. Pools and providers are made in
[`wif.tf`](../terraform/bootstrap/wif.tf), which the owner applies.

**`roles/iam.serviceAccountAdmin` is also off the project** since 2026-09-29
(owner decision on #334), and a validation refuses it in `deployer_roles`. See
the next section.

## The deployer's service-account grants

Project-wide, `roles/iam.serviceAccountAdmin` let CI set the IAM policy of
every service account in `saga-agents-staging` — the other team's eleven
(`promptlab-runner` among them), `swarm-ci-fix`, and `swarm-tf-deployer`
itself — and so grant itself `serviceAccountTokenCreator` on any of them and
act as it. That was route 3 above, and the way out of the WIF pin the review of
contract request 30 (#314) found.

**A condition could not narrow it.** IAM does not evaluate a resource name for
its own resources: *"the condition `resource.name.endsWith == devResource`
never grants access to any IAM resource because IAM resources don't provide
the resource name"*
([conditions attribute reference](https://docs.cloud.google.com/iam/docs/conditions-attribute-reference),
read 2026-09-28), and `iam.googleapis.com` is absent from the resource-service
table in
[conditions-resource-attributes](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes).
A `resource.name` condition would have revoked the role, not narrowed it.

**So it is granted per account.** The role can be granted on a single service
account ([role reference](https://docs.cloud.google.com/iam/docs/roles-permissions/iam):
"Lowest-level resources where you can grant this role: Service Account").
[`deployer_service_accounts.tf`](../terraform/bootstrap/deployer_service_accounts.tf)
grants it on each account `terraform/infra` manages and on nothing else, and
the deployer holds `roles/iam.serviceAccountCreator` on the project instead —
create, get and list, because creating is checked on the project, where the
new account does not exist yet.

| accounts the deployer administers | where the list comes from |
|---|---|
| `swarm-api`, `swarm-scheduler`, `swarm-quota-broker`, `swarm-reconciler`, `swarm-tick`, `swarm-verify` | [`terraform/modules/service_account_ids`](../terraform/modules/service_account_ids/main.tf), which `modules/iam`, `terraform/infra/verify.tf` and bootstrap all read |
| `swarm-agent-worker-<tenant>`, one per tenant | the prefix from the same module; the tenant keys from the `tenants` block of [`dev.tfvars`](../terraform/environments/dev/dev.tfvars), read by bootstrap from the file (`infra_tenants_tfvars`) |
| never `swarm-ci-fix` or `swarm-tf-deployer` | subtracted, and a precondition fails the plan if infra ever manages either |

Nothing restates the list, so an account infra starts managing is an account
bootstrap grants on at its next apply. The deployer's grant of `actAs` to itself
([`terraform/infra/deployer.tf`](../terraform/infra/deployer.tf)) still works:
every account in it is on the list. What admin on a listed account still
allows — acting as `swarm-api`, say — is authority CI already had by deploying
as it. Route 3 is closed once the owner applies this, because neither account
it named (the compute account, `209012342332@cloudbuild`) is on the list, and
no listed account reads logs.

### A new account exists before the release that adds it

The release sets a new account's IAM policy in the same apply that creates it:
`modules/tenancy`'s `act_as` and `workload_identity`, and `deployer.tf`'s
`actAs`, are `setIamPolicy` calls on the account. With the role per account,
those need bootstrap's grant on it, and bootstrap cannot grant on an account
that does not exist yet. So for a **new tenant**:

1. `scripts/register-tenant.sh --group <group>` creates `swarm-agent-worker-<tenant>`
   and checks the grant (step 2b). If the account **already existed**, step 2b
   refuses to go on when it carries any IAM binding the platform does not make,
   any conditioned binding, or a user-managed key — see "Adoption is limited to
   the tenant worker" below.
2. Add the tenant to the `tenants` block of `terraform/environments/dev/dev.tfvars`
   on the pull request's branch, and push it.
3. The owner applies bootstrap **from an up-to-date `main` checkout, never from
   the branch.** The branch contributes one file, as data, copied out with
   `git show`; every line of bootstrap code that runs is `main`'s:

   ```bash
   git fetch origin && git switch main && git pull --ff-only
   git show origin/<branch>:terraform/environments/dev/dev.tfvars > /tmp/pr-dev.tfvars
   grep -n '<<' /tmp/pr-dev.tfvars          # expect nothing inside the tenants block
   terraform -chdir=terraform/bootstrap init -reconfigure \
     -backend-config="bucket=swarm-tfstate-saga-agents-staging"
   terraform -chdir=terraform/bootstrap plan -var infra_tenants_tfvars=/tmp/pr-dev.tfvars
   ```

   `terraform init` is needed before any bootstrap plan or targeted apply: this
   root reads `modules/service_account_ids` and `modules/custom_role_ids`, and a
   checkout that has not initialised them since they were added fails with
   "Module not installed". The `-backend-config` is the state bucket: since #827
   bootstrap's state lives at `gs://<bucket>/bootstrap`, and a checkout with no
   state refuses to plan through `scripts/bootstrap.sh` but not through a bare
   `terraform plan` — check `terraform -chdir=terraform/bootstrap state list` is
   not empty before reading this plan
   ([operations](operations.md#the-bootstrap-layers-state)).

   **Check the untargeted plan before applying anything**, and stop if any of
   these does not hold:

   * the `deployer_admin_accounts` output gains exactly the worker ids of the
     tenants the pull request means to add, and loses none;
   * the resource changes are exactly those tenants'
     `google_service_account_iam_member.deployer_admin["swarm-agent-worker-<tenant>"]`,
     and the summary reads **"N to add, 0 to change, 0 to destroy"**, N being
     the number of new tenants;
   * no heredoc is inside the file's tenants block. The plan also refuses one
     (a precondition on `deployer_admin`), because a heredoc's body is free
     text and a line in it shaped like `  name = {` would parse as a tenant.

   Then apply only those grants, with the same `-var`:

   ```bash
   terraform -chdir=terraform/bootstrap apply -var infra_tenants_tfvars=/tmp/pr-dev.tfvars \
     -target='google_service_account_iam_member.deployer_admin["swarm-agent-worker-<tenant>"]'
   ```
4. Merge. The release adopts the existing account (`create_ignore_already_exists`)
   and sets its IAM.

Removing a tenant is the reverse: the release destroys the account first, then
bootstrap's next apply from `main` drops its grant.

#### Adoption is limited to the tenant worker

`create_ignore_already_exists` makes a create that meets a 409 take the
existing account into state instead of failing. On an account somebody else
made first, that is squatting: their IAM policy and their keys come with it.
So only `modules/tenancy`'s worker sets it — the one account that is created
before the release, by `register-tenant.sh`, which inspects an account it did
not create and refuses one carrying anything the platform does not grant
(tests/integration/test_register_tenant_squat.py). The platform accounts,
`swarm-tick` and `swarm-verify` are in state already and do not adopt: a 409 on
one of them fails the release. A **new platform account** is therefore created
by the release itself, which then fails with a 403 on that account's
`setIamPolicy`; the owner applies bootstrap's grant on it from `main` (its id
comes from `modules/service_account_ids`, already on `main` after the merge) and
re-runs the failed release job.

## The deployer's refusal is proven once, by a probe the owner dispatches

Once PR #73's targeted apply lands, the deployer's `projectIamAdmin` carries the
`modifiedGrantsByRole` condition in
[`deployer_conditions.tf`](../terraform/bootstrap/deployer_conditions.tf) --
as of #275, chunked into two bindings rather than one: `hasOnly()` refuses a
list over 10 elements, and the 15 grantable roles (14 until #1430 turned
`swarmGithubHooksDedup` on, and 15 until #150 took `swarmSecretLister` off) do not fit in a single call. Every chunk still authorises only a `setIamPolicy` whose modified
roles stay inside it, which is what each Terraform-issued call already does.

**The live project also still holds the deployer's UNCONDITIONED
`projectIamAdmin`, restored by hand and never re-entered into bootstrap
state.** Owner decision, 2026-09-28: remove it by hand, and only after the
chunks above are live, in this exact order:

1. `DEPLOYER=$(terraform -chdir=terraform/bootstrap output -raw github_deployer_service_account)`
2. `scripts/bootstrap.sh --target 'google_project_iam_member.deployer_project_iam_admin'`,
   with the plan reading exactly "2 to add, 0 to change, 0 to destroy" --
   abort on anything else.
3. `gcloud projects get-iam-policy saga-agents-staging --flatten=bindings --filter="bindings.role=roles/resourcemanager.projectIamAdmin AND bindings.members:serviceAccount:${DEPLOYER}" --format='value(bindings.condition.title)'`,
   expecting the two chunk titles plus one empty line (the unconditioned
   grant, which carries no condition title).
4. `gcloud projects remove-iam-policy-binding saga-agents-staging --member="serviceAccount:${DEPLOYER}" --role=roles/resourcemanager.projectIamAdmin --condition=None --format=none`.
5. Re-run step 3, expecting exactly the two chunk titles.
6. Prove the admitted side with a `terraform/infra` plan or a release apply.

**Why this order and not one apply that imports and destroys the old
grant.** CI never lacks `projectIamAdmin` for any interval this way, unlike
#275's own one-minute gap between destroying the old single condition and
creating the chunked ones. Importing the unconditioned grant into bootstrap
state and targeting both it and the chunks in one apply would reintroduce
that same race -- a parallel destroy and create of the same role, on the same
principal, with no ordering between them.

Releases then prove the **admitted** side. Every plan reads the project policy,
and a release that adds a tenant writes it. Nothing in the pipeline asks for a
role **off** the list. **Owner decision, 2026-09-25 (#68):** a deliberate
probe, run once by the owner after step 3 of the order on #80.

[`iam-refusal-probe.yml`](../.github/workflows/iam-refusal-probe.yml) has no
trigger but `workflow_dispatch`, takes no inputs, and refuses any ref but
`main`. The workload identity provider would refuse one anyway: it mints the
deployer's token for `refs/heads/main` only. As the deployer, the probe asks for
`roles/browser` for itself. It **passes only if IAM refuses with
PERMISSION_DENIED on the policy write**. If the grant lands, the probe removes
exactly that binding, reads the policy back, and fails saying #68 must be
reopened. The owner's steps, and what each outcome means, are in
[the runbook](runbooks/iam-refusal-probe.md).

**What a pass proves.** One role the list does not name was refused under the
condition, for a direct grant by the deployer to itself, at the time of the run.
Preflight makes that the condition's refusal and nobody else's. The scoped
bindings are the only `projectIamAdmin` grants the deployer holds, and none of
its other roles carries `resourcemanager.projects.setIamPolicy`. That check is
limited to the roles preflight could read, which is all of them before step 4
(#150). `hasOnly` treats every unlisted role alike, and a role absent from
`deployer_grantable_project_roles` is absent from every chunk, so one refusal
still speaks for all of them. It is still one role, measured once.

**It covers a self-grant of `swarmSecretLister` (#69).** #69's acceptance is
that, with `projectIamAdmin` scoped, the deployer cannot bind itself
`swarmSecretLister`, whose project-wide `secretmanager.secrets.setIamPolicy`
reaches the other team's secrets. The refusal of `roles/browser` stands for that
exactly when no live chunk lists `swarmSecretLister`, so preflight checks it --
by role id, `.../roles/swarmSecretLister` under any project prefix, in either
quote style -- stops with nothing attempted if a chunk does, and a pass says in
the run summary that the refusal also covers `swarmSecretLister`. The probe
never asks for `swarmSecretLister` itself: if the condition were broken, the
deployer would hold `secrets.setIamPolicy` over the other team's secrets until
the revert.

**Preflight checks every chunk, not one binding (#276).**
[`iam-refusal-probe.sh`](../scripts/iam-refusal-probe.sh)'s preflight accepts
one or more conditioned `projectIamAdmin` bindings on the deployer -- one per
chunk -- and refuses unless each one's expression is exactly
`api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly([...])`
and its list names neither `roles/browser` nor `swarmSecretLister`. A grant is admitted if *any*
binding admits it, so one chunk with a broader condition (a `|| true`, a
`hasAny`) or the probe role in its list would make a grant prove nothing; the
whole expression is matched, and the list is parsed, for that reason. Before
#276 it asserted exactly one binding and would have stopped at two.

**What it cannot prove:**

* **That the `roleAdmin` route is closed (#79, PR #150).** That route changes a
  custom role CI already holds so that it carries `setIamPolicy`. The write is
  then authorised by that role, and the condition is never evaluated. The probe
  asks the condition a question that route never asks. If the route has already
  been used, preflight finds the permission on the role and stops. It does not
  show that the route is shut: what shuts it is the binding's absence, since
  the owner's bootstrap apply of 2026-09-28 destroyed it.
* **Who a listed role goes to.** `hasOnly` limits which roles change, not whose
  grant changes. CI can still grant any of the 15, unconditioned, to
  anyone.
* **Routes through other identities.** Routes 1 and 3 in
  [the table above](#what-the-view-does-not-bound-the-deployer-can-reach-every-log)
  act as another account, and the probe signs in as the deployer only. Group
  memberships and inherited policy are not visible to it.
* **Anything after the run.** A later bootstrap apply can change the condition.
  After any change to it, run the probe again.

**How it knows it was refused.** gcloud does not print the status word
PERMISSION_DENIED. For an HTTP 403, which Google maps to PERMISSION_DENIED and
nothing else, it prints `does not have permission to access projects instance
[<project>:setIamPolicy]`. The probe accepts that sentence, and rejects a 403
that names a disabled API, billing or a service perimeter. Everything else fails
the run: an auth failure, a network error, a 403 on the policy read, a new
wording. The sentence comes from the Cloud SDK source (SDK 483.0.0) and has not
been seen on the runner. A wording change therefore costs a re-run, never a
false pass. [`classify`](../scripts/iam-refusal-probe.sh) has the full rule.
[`test_iam_refusal_probe.py`](../tests/unit/scripts/test_iam_refusal_probe.py)
runs the script against a fake `gcloud`, and holds each case.

## The CI fixer

A SwarmCloud pull request that goes red gets a fix attempt with no operator
(#263). Before this, #248 and #249 each waited for the operator to start a
fixer by hand and push the fix-up from a laptop.

`ci-fix.yml` runs when `application` completes with `failure` on a
`swarm/<task-id>` branch of this repository, and runs
[`scripts/ci-fix.sh`](../scripts/ci-fix.sh) `run`:

1. **Only the pull request's current head, at submit and again at start.** A
   red run for a commit the branch has already moved past is ignored. The
   check at submit is not enough, because a fix waits for capacity before it
   starts and the red head can be superseded while it waits. PR #2224's two
   fixes started 1.5-2 h after submission, when the owner had already fixed
   and merged the branch: one agent found nothing to change and failed
   `published_nothing`, the other reset onto the red sha and failed with
   HEAD moved (#2276). So swarm-api signs the red head into the dispatch
   block (`ci_fix_head`, `swarm_api.validation.CI_FIX_HEAD_FIELD`; the
   worker acts on no unsigned metadata), and the worker checks it again on
   the attempt's fresh clone, before any runner starts
   ([`agent_worker/cifixstart.py`](../apps/agent-worker/agent_worker/cifixstart.py),
   `lifecycle.py::Worker._ci_fix_superseded`). A branch that moved past the
   red commit, a merged or closed pull request, or a head the forge reports
   moved ends the attempt SUCCEEDED as `no_change`, with a `no_change_reason`
   starting `superseded:` (`lifecycle.py::Worker._finish_superseded_ci_fix`).
   No agent runs and nothing is pushed. A forge that cannot be read leaves the
   clone alone to decide. An attempt restored from a checkpoint is not
   checked, because its agent already ran.
2. **Every failed job is read, cut where it failed, redacted, then capped.**
   The script lists the run's jobs through the Actions API and reads each
   failed job's own log (`actions/jobs/<id>/logs`), not `gh run view
   --log-failed`: on run 39703499595 (PR 1905) that returned 106 lines, all
   from `ci-gate`, and none from the three failed jobs whose names contain
   `/`. Everything goes through `redact` (`scripts/lib/common.sh`) before
   anything else reads it. Each job's log is cut where its failed step ended
   (the first `##[error]Process completed with exit code`, else the line
   before `Post job cleanup`), so the runner's git cleanup never takes the
   room: on PR 1905 the plain 3,000-byte tail was half cleanup, and the
   failing test sat just above it. Of what is left, the lines that name a
   failure are kept in order -- a vitest `FAIL <file> > <test>` line with the
   block under it (`FAILURE_BLOCK_LINES`), pytest's `FAILED`/`ERROR` node ids,
   `E` lines and short summary, every `##[error]` -- and a job with none of
   those keeps its plain tail. A gate over `needs` (`ci-gate`), whose log only
   restates other jobs, comes last. The whole is 16 KiB at most
   (`LOG_EXCERPT_MAX_BYTES`). The merge step's own fix rounds build their
   excerpt by the same rules in Python (`swarm_api/issueci.py`,
   `_check_section`; CIFIX-EXCERPT, part of #1914), and go further: a red
   gate's workflow run is read and its failed leaf jobs are excerpted ahead
   of it, each section names the events it failed on, and a job red on push
   but green on pull_request at one sha is called out as a branch behind a
   fix on main: merge main (#2937). `redact` is a pattern list
   and not a boundary (its own header says so); what makes a leak survivable
   is that the excerpt is stored as an agent's input, which the API serves
   masked.
3. **One step, through the API, by name.** It submits a one-step workflow:
   runner profile `claude-code`, the excerpt in `input.prompt`, strategy
   `direct-pr`, and `continues_task` naming the red branch's agent. No image,
   command, backend or resource field exists to send (invariant 10).
4. **The fix lands on the red branch.** The worker clones `swarm/<task-id>`
   rather than main, and pushes the fix onto it rather than onto a branch of
   its own ([`agent_worker/continuation.py`](../apps/agent-worker/agent_worker/continuation.py)).
   The push is never forced, so a branch someone moved in the meantime is
   reported, not overwritten. The push re-runs CI.
5. **Every attempt is a comment on the pull request**, and after
   `MAX_FIX_ATTEMPTS` (two; the script says why) one more comment says it
   stopped. A submission the API refuses is a comment and a red fixer run, and
   does not count as an attempt.
6. **How the fix step ended is a comment too.** When the fix agent reaches a
   terminal state, swarm-api posts one comment on the pull request: the fix
   task's id, its console link, its state, and one paragraph -- the commit it
   pushed, or why nothing was pushed, and the agent's own account cut to a
   paragraph. It says when the per-pull-request cap is reached. No log is
   quoted (the console has it), and everything the agent or an error wrote is
   redacted, with the tenant's token as a known literal, and neutralised
   (mentions and closing keywords broken) before it is posted
   ([`swarm_api/cifix.py`](../apps/swarm-api/swarm_api/cifix.py)).

**It stands down while a lane's merge step is live, and the API decides
that** (CI-FIXER-ONE, CI-FIXER-GATE, part of #1822). A lane whose workflow has
a merge step runs its own CI-fix rounds on its pull request
(`mergewake._on_red`). A standalone fix submitted beside them is racing that
round for the same branch. In 72 h to 2026-10-21 this fixer ran 91 times and
failed 34, and 22 of those failures were a push rejected because the lane's
round had moved the branch first. So when the continued agent's workflow has a
merge step in any non-terminal state (QUEUED or PARKED on its CI wait counts),
the API's ci-fix submit path (`cifix.stamp`) answers 409 `ci_fix_lane_owns_pr`
naming that merge step. It starts no agent and takes no claim. The script logs
the skip, comments nothing, and passes. The check has to be the API's.
`SWARM_CI_FIX_SA` is continuation-scoped and is served only what it submitted
(`deps.submission_scope`), so CI-FIXER-ONE's read from the script got a 404
for every real lane. The API reads with its own access, and the fixer gains
no read. An agent in no workflow, a workflow with no merge step, and a merge
step that has finished are fixed as before.

**An issue run owns its pull request the same way** (#2216). An issue run's
compiled workflow has no merge step, so the merge-step read never fired for
its pull request, yet while it is CHECKING or FIXING it reads CI and submits
its own CI-fix rounds from `issueci`. Measured 2026-10-23: on #2145 the
standalone fixer ran 30 minutes and its push was rejected non-fast-forward
because the run's round had pushed first; on #2147 it started 64 s after the
run's round and published nothing while that round fixed the failure. So
`cifix.stamp` also answers 409 `ci_fix_lane_owns_pr` while an issue run of the
same tenant that is not terminal (any of PLANNING through FIXING: an APPROVED
or RUNNING run reaches CHECKING next) has the continued agent as its
`pr_task_id`, or the continued agent's workflow among its `ci_fix_workflows`.
The detail names `issue_run_id` and `issue_run_state`, and the script's skip
line names the run. The reads are two tenant-scoped equality reads on
`issue_runs` that the automatic single-field indexes serve, so there is no
composite index to build. The run's own rounds carry `metadata.issue_run`,
not `metadata.ci_fix`, so they never reach this gate. A pull request whose
issue run is DONE, FAILED or otherwise terminal is fixed as before. The
refusal ships **on**
(`REFUSAL_CI_FIX_LANE_OWNS_PR=off` turns it off). One gap remains: the
script's own attempt cap is checked before it submits, so a pull request
already at the cap gets the stop comment even while its lane's merge step is
live.

**A plain continuation of the lane waits for its merge step too** (#2685).
The merge-step read above held only a request with `metadata.ci_fix`; a plain
`continues_task` workflow (no fix mark of any kind) went straight through.
On 2026-10-28 the plain lane task_7ff30c32 (wf_85f651ab, `CIFIX-2617`) was
admitted onto PR #2617 while that lane's merge step task_943ec5f2 was live.
The merge step then submitted its ci-fix:1 round task_99b8e1e0 beside it, the
plain lane pushed 5bc405a first, the round's push was rejected
non-fast-forward, and the merge step refused `head_moved` at a head none of
its rounds had pushed. Why: a pull request has one writer at a time. A lane
with a merge step already has one -- the merge step submits its own CI,
conflict and pick rounds whenever CI goes red or the branch conflicts -- so
any other agent pushing to the branch races that round, and whichever
pushes second loses its whole run. So `cifix.stamp` answers such a request
409 `ci_fix_lane_owns_pr` while the continued lane's merge step is in any
non-terminal state, before anything is written. The detail names the merge
step (`task_id`), its `workflow_id`, the lane's `root_task_id`, the
`pull_request` when the branch's agent recorded one, and
`plain_continuation: true`; it carries no `metadata: ci_fix`, because the
request had none. Resubmit once the merge step ends or is cancelled. A
merge-only request, a request carrying a writer's own mark (`ci_fix`, the
merge step's `merge_fix_round` and `merge_conflict_round`, pr-shepherd's
`pr_shepherd`, an issue run's `issue_run`), an agent in no workflow, a
workflow with no merge step and a merge step that has finished are admitted
as before. A `publish_to_pr` workflow is not read here: its branch's agent is
known only after `stamp`. The same switch turns it off
(`REFUSAL_CI_FIX_LANE_OWNS_PR=off`).

**A CI fix waits for another live CI fix of its pull request** (#2763). The
claim (`ci_fix_in_flight`) keys a failure by head and run, so a red run at
another head, or another red run at the same head, counted as another failure
and got a second fix beside the first. On PR #2730 this fixer's attempt 1
(task_f11210cf) was RUNNING on bb31584 when attempt 2 (task_af11c3ba) was
admitted at 62a6fbac. Attempt 2 pushed 9e193de0 first, and attempt 1's publish
conflicted. Why: a pull request has one writer at a time, and two CI fixes on
one branch race exactly as a fix and a lane's round do. So `cifix.stamp`
answers 409 `ci_fix_lane_owns_pr` while another CI fix of the same pull
request is live -- a non-terminal agent continuing the same branch agent, or a
claim still inside its two-minute grace whose agent is not written yet -- at
any head and from any run, before the cap and the claim, so nothing is
written. The detail names the live fix's `task_id`, `workflow_id` and
`head_sha`, with `ci_fix: true`. When that fix pushes, CI runs again at its
head, and a red run there gets its own fix. It is this refusal, not
`ci_fix_in_flight`, because this one ships on and the script reads it as a
skip, while `ci_fix_in_flight` is report-only and refuses nothing in
production. A re-delivery of the very failure the live fix is on is still
answered `ci_fix_in_flight` by the claim, as before. A finished fix, a fix of
another pull request and another tenant's fix are passed over. The same
switch turns it off (`REFUSAL_CI_FIX_LANE_OWNS_PR=off`).

**A request that pushes to a pull request waits while a fix holds it**
(#2535). The gates above read only a request that carries `metadata.ci_fix`.
Anything else went straight through. On 2026-10-26 an `implement` step of
wf_3bde5fa9 was admitted onto PR #2473 with `publish_to_pr`, three seconds
after this fixer (task_b0abb81d) started on the same red head 377c3010. Both
agents fixed the same test, the fixer pushed first, and the step's publish
was refused `head_moved` after its agent had done the whole fix. So
`cifix.stamp` now answers 409 `pr_fix_in_flight` to a request that pushes and
is not itself a fix, while a live CI fix (an agent, or a claim still inside
its two-minute grace) or a live conflict fix (pr-shepherd's, or a merge
step's conflict round) holds that pull request. Two kinds of request meet it:

* a `continues_task` continuation, against the pull request its branch's
  agent recorded (`result_summary.git.pull_request.number`);
* a `publish_to_pr` workflow, against that number in its repository. The
  API reads that pull request's branch from GitHub only after `stamp`, so
  this read goes by number, and the repository is checked on every row.

The answer names the live agent, its workflow, its kind and the pull request,
and nothing is written. Resubmit once that fix ends: its push moves the head,
and CI runs again there. An issue run's own CI-fix round is such a
continuation. The second step of #2535 makes it read this 409 as "wait";
until that lands, the refused round fails its run. A merge-only request, a
request that is itself a fix round (each has its own gate), and a branch agent
with no recorded pull request are admitted as before. The refusal ships
**on** (`REFUSAL_PR_FIX_IN_FLIGHT=off` turns it off). Shipped off, it would
refuse nothing in production until terraform's `api_refusals` turned it on.

### The post-back, and the cap the API holds

The script comments when it SUBMITS; it ends before the step does, and
nothing in GitHub Actions should wait an agent's hour for it. So the outcome
is posted by swarm-api, on the per-tenant `merge_wake` Cloud Scheduler tick
(`POST /v1/admin/merges/wake?tenant_id=<t>`, every minute, as the rollup
sweeper), which already reads that tenant's `-git` token for the merge step's
CI reads. The tick's answer carries `ci_fix` (posted, waiting, failed) and
`ci_fix_failures` (task id and code) beside the merge report.

* **Stamped by the API, never by the caller.** The script sends
  `metadata.ci_fix` = `{pull_request, run_id, head_sha, attempt,
  max_attempts}` on the workflow. The API accepts it only beside
  `continues_task`, refuses any other key in it (the post-back's record
  above all), and stamps `ci_fix.postback = "pending"` on the fix agent.
* **Only to the pull request of the continued branch.** The tick reads the
  pull request with the tenant's token and posts only if its head branch is
  `swarm/<the continued task>`; anything else is given up as
  `pull_request_mismatch`.
* **Once.** The comment opens with `<!-- swarm-ci-fix:outcome:<task> -->`;
  a retry whose record was lost finds it and does not post again. It is never
  the script's attempt marker, so it never counts against the cap. A failed
  write is retried every 5 minutes for an hour (`RETRY_SECONDS`,
  `MAX_POSTBACK_FAILURES`), then given up with its code.
* **The cap holds at the API too.** The script counts its own attempt
  comments, which anyone with write access can delete. The API counts the
  tenant's fix workflows for that pull request of that branch and refuses
  the one past `max_attempts` (422 `invalid_dispatch`), and refuses a
  `max_attempts` above 3 (`MAX_ATTEMPTS_CEILING`).
* **The token needs `Issues: Read and write`** on the repository -- the same
  permission an issue run's comments need ([multi-tenancy.md](multi-tenancy.md)).
  Without it the fix still runs and pushes; the tick reports
  `writeback_forbidden` for the agent and retries until it gives up.

### Why a step may push to another agent's branch

`continues_task` is a TASK ID, never a branch name: the worker derives the
branch from it with the prefix it pushed under, as the integrator derives its
contributors'. The API refuses it unless the agent is in the caller's own tenant
(another tenant's reads exactly like a missing one), was itself `direct-pr` in
the same repository, and the workflow is `direct-pr` with one step and no
`repository_ref`. A continuation of a continuation continues the original
branch.

Since #454's CI loop a tenant MEMBER may also continue an `integrate`
workflow's **integrator**: it pushes `swarm/<its own task id>` through the same
`publish_branch` a `direct-pr` agent does, and opens the workflow's one pull
request from it, so an issue run's fix round lands on the run's pull request.
A contributor is still refused (its branch has no pull request).

**What the CI fixer may continue** (owner decision 2026-10-06). Its
continuation-scoped account reaches exactly the agents that opened a pull
request from their own branch, in its own tenant:

* a `direct-pr` agent -- the reach contract request 30 reviewed;
* an `integrate` workflow's **integrator**. Every SwarmCloud lane pull
  request is opened by one (the workflow's final fix step), so a fixer
  limited to `direct-pr` agents could fix none of them: its first real run,
  on PR #740, was refused `422 invalid_dispatch`. The fix pushes to the same
  `swarm/<integrator task>` branch, so that pull request updates, and the
  per-pull-request cap and the post-back above work unchanged, both keyed on
  the integrator as the continued agent. The integrator is recognised by the
  `role` the API recorded in its dispatch block when the workflow was
  submitted -- a reserved key no caller can write -- never by anything the
  fixer sends.

Still refused to it, each as `422 invalid_dispatch`: any other step of a
workflow (a contributor's branch has no pull request of its own, so a fix
there reaches nobody), any agent of another tenant (read exactly like a
missing one), a chain of continuations whose root is neither of the two, and
a merge-only continuation (a merge is a member's power, not a push). Widening
the reach to integrators lets a stolen fixer token push to every lane pull
request of its tenant as well as every `direct-pr` one; it still cannot
merge, start new work, read what it did not submit, or reach another tenant.
The rules are in
[`swarm_api/continuation.py`](../apps/swarm-api/swarm_api/continuation.py); the
block it writes is recorded under request #6 in
[`contract-change-requests.md`](contract-change-requests.md).

### What the owner configures, once

Until this is done the fixer comments once on each red swarm pull request that
it is not configured, and submits nothing.

* **`SWARM_CI_FIX_SA`**, a repository variable: the service account the
  fixer submits as. The swarm API is reachable from a GitHub runner only
  through the IAP front door, with an access token for a principal granted
  `roles/iap.httpsResourceAccessor` (`frontend_iap_members` in
  `terraform/bootstrap/terraform.tfvars`, applied by the owner) -- the same
  path `SWARM_IMPERSONATE_SA` gives an operator's laptop. IAP membership lives
  only there: `terraform/bootstrap/wif.tf` binds each member on the
  platform's own backends (`frontend_accessors`), in the root only the owner
  applies, so adding the fixer is an owner apply and never a release. The
  deployer (`GCP_DEPLOY_SA`) holds no IAP role at all -- no `roles/iap.*`
  project role, and it is not in `frontend_iap_members` (read 2026-10-07) --
  and the release never reads an IAP policy: each of the three ways of
  granting it IAP admin tried on 2026-09-24 either reached the other team's
  backends or was refused (wif.tf, "WHO MAY PASS IAP"). `tests/terraform/bootstrap.tftest.hcl`
  (`iap_membership_is_bootstrap_owned_and_the_deployer_has_no_iap_role`)
  requires every accessor binding to be `roles/iap.httpsResourceAccessor` and
  no deployer role to start `roles/iap.`.
* **`ci_fix_service_account` in `terraform/bootstrap/terraform.tfvars`**, the
  same email, applied by the owner. It binds that account
  (`roles/iam.workloadIdentityUser`) to exactly one principal,
  `principalSet://.../attribute.job_workflow_ref/<owner>/<repo>/.github/workflows/ci-fix.yml@refs/heads/main`
  ([`ci_fix.tf`](../terraform/bootstrap/ci_fix.tf)), and the workflow
  authenticates as it directly. The deployer (`GCP_DEPLOY_SA`) plays no part
  and holds no role on the fixer's account: an earlier version hopped from the
  deployer's token, which let every workflow that can become the deployer
  become the fixer too, and gave a job that only comments on pull requests a
  token holding `projectIamAdmin` on a shared project (owner decision,
  2026-09-28). The principal is the workflow FILE, not the repository:
  `attribute.job_workflow_ref` is set by GitHub from the file the job runs,
  so `release.yml` on main presents a different value and is refused.
  `tests/terraform/bootstrap.tftest.hcl` compares the member whole and
  refuses the repository-wide forms. The plan also refuses an account that is
  the deployer, or that is not in `frontend_iap_members`.
* **Its tenant must be the tenant that owns the swarm pull requests.** The API
  refuses to continue another tenant's agent, by design. So the account is
  LISTED on that tenant, in `tenants.<tenant>.service_accounts` in
  `terraform/environments/<env>/<env>.tfvars`, which resolves it by exact
  email and unique id (contract request 30). It is NOT added to the tenant's
  Google group: `eng@saga.xyz` holds project-wide admin roles on this shared
  project, and membership would hand those to a workflow. Without the listing
  its personal tenant `u-<name>` owns nothing it can fix (owner decision,
  2026-10-05; `swarm-ci-fix` is listed on `eng`). The fix step runs
  on that tenant's `claude-code` credential and pushes with its
  `swarm-tenant-<tenant>-git` token, like every other `direct-pr` step.
* **`SWARM_API_HOST`**, optional: empty resolves the front door from
  `terraform/environments/<env>/<env>.tfvars`.

`workflow_run` only fires for a workflow file on the default branch, so the
fixer does nothing before it has merged.

### Turning it on, in this order

Status, 2026-10-06: `ci_fix_wif` is applied (terraform/bootstrap, 08:40Z),
`swarm-ci-fix` is in `frontend_iap_members` and listed on `eng`, and the
repository variable `SWARM_CI_FIX_SA` is **unset**, so every red swarm pull
request gets the "not configured" comment and no fix. Each step below
depends on the one before it; doing the last one first gives a fixer that
authenticates and is refused.

1. **The account exists** (it is not created by Terraform; `ci_fix.tf` says
   why):

   ```bash
   gcloud iam service-accounts describe \
     swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com \
     --project saga-agents-staging --format='value(email,uniqueId)'
   ```

2. **Bootstrap applied** with `ci_fix_service_account` set and the same
   account in `frontend_iap_members` (`terraform/bootstrap/terraform.tfvars`;
   the plan refuses one without the other):

   ```bash
   terraform -chdir=terraform/bootstrap plan -out=/tmp/bootstrap.plan
   terraform -chdir=terraform/bootstrap apply /tmp/bootstrap.plan
   ```

   This makes `google_service_account_iam_member.ci_fix_wif`
   (`roles/iam.workloadIdentityUser` on the account, for the principalSet
   `attribute.job_workflow_ref/<repo>/.github/workflows/ci-fix.yml@refs/heads/main`)
   and the account's `roles/iap.httpsResourceAccessor` on the front door's
   backend services (`wif.tf`, `frontend_accessors`). **No token-creator
   grant exists or is needed**: the workflow federates AS the account, and
   `access_token` in `scripts/lib/common.sh` does not impersonate the account
   it is already signed in as.

3. **A release has rendered the tenant listing** into swarm-api
   (`tenants.eng.service_accounts` in
   `terraform/environments/dev/dev.tfvars`, `TENANT_SERVICE_ACCOUNTS` on the
   service). Without it the account resolves to its own `u-` tenant, which
   owns nothing it can continue, and every submission answers 422.

4. **eng's `-git` token can comment**: `Issues: Read and write` on the
   repository, for the post-back. It already needs it for issue runs.

5. **Then, and only then, the repository variable:**

   ```bash
   gh variable set SWARM_CI_FIX_SA \
     --repo bogdan-alexandrescu/SwarmCloud \
     --body swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com
   ```

   `GCP_WIF_PROVIDER` (already set for the release) names the provider;
   `SWARM_API_HOST` is optional.

### Seeing it work

* On the next red `swarm/<task>` pull request, the `ci-fix` run is green and
  the pull request has a comment `CI fixer: attempt 1 of 2.` naming the
  workflow and the fix agent. `gh run list --workflow ci-fix.yml --limit 5`
  lists the runs; a run that is SKIPPED was not a red `application` run on a
  `swarm/` branch of this repository (the job's `if:`).
* The fix agent is in the console under its id, in `eng`, and its branch is the
  pull request's. Its push re-runs `application` on the pull request.
* Within a minute or two of the agent ending, a second comment,
  `CI fixer: attempt 1 of 2 ended <STATE>.`, with the console link and the
  outcome. If it does not come, the `merge_wake` tick's answer names the agent
  and the code in `ci_fix_failures` (`writeback_forbidden`: the token lacks
  `Issues: write`; `pull_request_mismatch`: the pull request is not on the
  continued branch).
* A third red run on the same pull request gets `CI fixer: stopped after 2
  attempts.` and no submission; a submission forced past the cap answers 422.

### Admitting the fixer through `swarm-eng-bots@saga.xyz` (owner decision 2026-10-06): NOT EFFECTIVE YET

The owner asked, on 2026-10-06, that `swarm-ci-fix` reach tenant `eng`
through a SwarmCloud-only group, `swarm-eng-bots@saga.xyz`, rather than
`eng@saga.xyz` (which holds project-wide admin roles on this shared
project). Creating the group and its membership is an operator step in Cloud
Identity, and is exactly:

```bash
gcloud identity groups create swarm-eng-bots@saga.xyz \
  --organization=saga.xyz \
  --display-name="SwarmCloud eng bots" \
  --description="SwarmCloud only: service accounts admitted to tenant eng. Holds no IAM role on any project."
gcloud identity groups memberships add \
  --group-email=swarm-eng-bots@saga.xyz \
  --member-email=swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com
```

**Running it changes nothing about which tenant the fixer reaches, and
nothing in Terraform can register the group as `eng`'s.** A tenant's id is
derived from its group's address by the frozen
`swarm_common.identity.tenant_id_for_group`, so a registered
`swarm-eng-bots@saga.xyz` is a NEW tenant, `swarm-eng-bots`, which owns none
of `eng`'s pull requests -- every fix would be refused as "not a task in your
tenant". A group member is also a full member of its tenant, able to submit
any work, where the listing above makes the account continuation-scoped (it
may only continue a `direct-pr` agent or an integrator; contract request 30
and the 2026-10-06 decision above). The listing
already keeps the account out of `eng@saga.xyz`. Mapping a second group onto
`eng` is a frozen-contract change, and whether to make it is the owner's
question, not this document's: until it is answered, the listing is the
mechanism and the group, if created, is unused.

## A ready pull request is merged by GitHub, not by a session

**To be retired, owner decision 2026-10-04 (#295, contract request 47):**
merging moves off this GitHub-side App into a workflow `merge` step, which
squash-merges with the tenant's existing `-git` token, in whatever repository
the workflow runs on, and closes the issues the pull request closes -- the
App path merged #564 and #562 and left #72 and #560 open although GitHub
listed them as closing references (#569). See
[workflows.md](workflows.md#ending-in-a-merge-the-merge-step) and
[merge-step.md](merge-step.md) ("Revised 2026-10-04 (owner)").
`auto-merge.yml` and the `swarmcloud-merge` App stay in place until the merge
step is proven on real pull requests; retiring them (deleting the workflow,
the App and its `MERGE_APP_*` settings, and the parity cases in
`tests/unit/scripts/test_auto_merge_workflow.py`) is its own change, made
after that, not before.

**Status, measured 2026-10-01: the merge App is not yet configured**, so
`auto-merge.yml` refuses every `ready` label ("The merge App is not
configured") and `ready` pull requests merge only while an operator runs the
merge watcher. Everything below describes the mechanism once the owner has
followed [the merge App runbook](runbooks/merge-app.md). Remove this note when
`vars.MERGE_APP_ID` and `secrets.MERGE_APP_PRIVATE_KEY` exist.

Owner decision, 2026-09-28 (#262): **adding the `ready` label enables GitHub's
native auto-merge**, and GitHub performs the squash merge once the base
branch's required checks pass at the pull request's head. It used to take a
merge watcher running in an operator's session, so a green, ready pull request
waited for somebody's laptop. [`auto-merge.yml`](../.github/workflows/auto-merge.yml)
is the whole mechanism; it waits for nothing and polls nothing. A label that
lands while checks still run is looked at again when they finish
([below](#a-ready-label-that-lands-while-checks-run-is-re-evaluated)).

It refuses, with a comment on the pull request saying which and why:

* **a title starting `[swarm] task_`** — the worker's placeholder. The squash
  subject is the pull request's title plus `(#N)`, set explicitly, because a
  one-commit squash otherwise takes the commit's own message: that is how #238
  put `swarm: work from task_...` on main as a headline. Retitle it as a
  fact-style headline, then remove and re-add `ready`;
* **a base branch with no required status checks** — auto-merge would have
  nothing to wait for and would merge at once, red or not. The gate counts the
  branch's **effective rules** (`GET repos/{repo}/rules/branches/{branch}`,
  which includes every ruleset that applies, here
  [`main-protection`](#the-ruleset-on-main-and-ci-gate)) together with any
  classic branch protection's checks, each name once. Until 2026-10-01 it read
  only classic protection (`branches/main` → `.protection`), which this
  repository does not use, so it counted zero and refused every `ready` label
  (#412, #403). Classic protection still counts, so moving back to it works;
* **no merge App configured** — see the next paragraph;
* **a check that already ran on the head commit and is failing** — even one
  that is not required, and even beside checks still running. The ruleset
  requires only the checks that run on every pull request, plus `ci-gate`; a
  path-filtered workflow (`application.yml`, `terraform.yml`) is not required,
  but when a pull request's changes do trigger it, this still holds the merge
  on its result;
* **a closing keyword that names an open pull request** (since 2026-10-08,
  owner decision). The text of PR 857 named the open, unmerged PR 840 with a
  closing keyword, and two seconds after the App merged 857, GitHub itself
  closed 840. Before the gate, the enable job checks out the **default
  branch's** `scripts/` (sparse, no credentials, never the pull request's
  head) and runs
  [`scripts/check-closing-references.sh`](../scripts/check-closing-references.sh).
  GraphQL `closingIssuesReferences` is an IssueConnection and never lists a
  pull request (PR 817 puts "fixes" before PR 777 and lists nothing). So the
  script also reads the closing keywords in the title, the body and **every
  commit message**, because this repository's squash message is the commit
  messages. For each number in this repository it asks REST
  `repos/<R>/issues/<n>` whether it is a pull request and whether it is open.
  An open one refuses on the label and on a re-evaluation alike: `ready` is
  removed and the comment says to reword the reference ("PR 840") or write
  `part of #840`. An issue passes. So does a closed or merged pull request:
  the keyword closes, never reopens, and cannot change a merged one. If the
  check cannot tell (an error, a failed read other than a 404, more than one
  page of references or commits), the job fails and nothing is armed;
* **something the body says it depends on that has not landed** (since
  2026-10-08, owner decision, observer proposal I part 2) — see
  [Saying what a pull request depends on](#saying-what-a-pull-request-depends-on).

A check **still running** is not a refusal (since 2026-10-06, #697): if it
is a *required* one the pull request is queued and waits, as
[the section after next](#a-ready-label-that-lands-while-checks-run-is-re-evaluated)
says; if it is not required it does not hold the merge at all (since
2026-10-07, #815).

If the pull request is already green when the label lands, GitHub will not
*enable* auto-merge on it (its merge state is already `CLEAN`), so the
workflow merges it directly with the same token, method and subject. The
ruleset still decides; the App has no bypass.

### Saying what a pull request depends on

Lanes run in parallel, and one lane's pull request is often written on top of
another's that has not merged yet. Before this, nothing kept them in order:
`ready` merged whichever went green first, and the later one landed on a main
without the change it needed. Now a pull request says so in its **body**, on a
line of its own or anywhere in a sentence:

```text
depends on PR 840
depends on PR #840
depends on #840
depends on owner/repo#840
depends on https://github.com/owner/repo/pull/840
```

Any case (`Depends on PR 840`), any whitespace between the words. A bare
number is this repository's; `owner/repo#N` and a pull request or issue link
(`/pull/N`, `/issues/N`) are looked up in the repository they name. A number
GitHub does not find there (a private repository reads the same) refuses the
merge. Nothing else is read as a dependency: `see #840`, `after #840` and
`part of #840` are not, and neither is the phrase in the title or in a commit
message. The pull request's own number is ignored.

The phrase is read as prose. A `depends on` inside a fenced code block (to its
closing fence, or the end of the body if none closes it), an inline code span
or a `>` quoted line is ignored, so a pull request that explains this rule or
quotes another's body is not made to depend on anything.

The enable job runs the default branch's
[`scripts/check-pr-dependencies.sh`](../scripts/check-pr-dependencies.sh)
beside the closing-references check, from the same sparse checkout. For each
number it asks REST `repos/<R>/issues/<n>`:

| what `<n>` is | passes when | refuses when |
|---|---|---|
| a pull request | it is **merged** | open; or closed without merging, since it will never land and the line must go |
| an issue | it is **closed** | open |
| nothing (404) | never | always: a dependency nobody can read has not landed |

A refusal works like the gate's other refusals: on the label, a comment naming
each dependency and why, `ready` removed (the comment opens "Not queued for
auto-merge, and `ready` removed."), and a failed run; on a re-evaluation, the
same, with auto-merge disarmed too. **Nothing re-evaluates when the
dependency merges** (its merge is a push to main, not a run at this pull
request's head), so add `ready` again once it has. Editing the **body** of an
open `ready` pull request does re-evaluate: the `requeue-edited` job
dispatches a re-evaluation (only with the merge App configured), so adding or
removing the line is looked at then, not at the next CI finish. A read that
fails for any reason other than a 404 fails the job, and nothing is armed:
fail closed.

Only `auto-merge.yml` reads the phrase. The SwarmCloud worker's merge step
(`apps/agent-worker/agent_worker/merge.py`), which merges with the tenant's
own token, does not.

### A ready label that lands while checks run is re-evaluated

**Measured 2026-10-06 (observer P13):** #697 got `ready` while its CI was
running. The gate refused it ("failing or still running") and, evaluating
only on the label event, never looked again, so the pull request sat
unmerged after it went green. Every pull request SwarmCloud opens with
`pr_label: ready`, and every one an operator labels at creation, hit that.
Owner decision, 2026-10-06: a pull request labelled early is merged once its
checks are green at head.

So a *required* check still running at the head **waits** instead of
refusing. The label run comments once ("Queued for auto-merge, waiting for
required checks still running on the head commit: …") and arms nothing.
Then:

* the **`requeue` job** (`re-evaluate ready pull requests when a run
  finishes`) runs on `workflow_run: completed` of every workflow that runs on
  `pull_request` — `application`, `ci-gate`, `security`, `terraform`; the test
  reads that list out of the workflow files, so a new one cannot be left
  out. Since #815 it also runs on `check_suite: completed`, the backstop for
  a suite another App created (code scanning's, for one). It lists the open `ready` pull requests whose head is the finished
  run's head sha (not `workflow_run.pull_requests`, which GitHub leaves empty
  for a fork) and sends a `workflow_dispatch` of `auto-merge.yml` on main for
  each, with `pr: <number>`;
* the **dispatched run is the `enable` job again**: it reads the pull request
  back from the API (title and head as they are now), does nothing unless it
  is still open and labelled `ready`, and runs the same gate. A required
  check still running: it waits, silently. Every required check green and no
  check that ran failed: it merges with the App token, exactly as a label on
  a green pull request does. The last run to finish on the head is the one
  that merges it.

**Only a required check still running holds the merge (#815, 2026-10-07).**
#811 got `ready` while code scanning's `Trivy` was still running. Trivy is
not one of the checks ruleset 24160219 requires, and no application or
security run reports it, so the gate waited on it, nothing looked again when
it finished, and the merge queue stalled for five hours until an operator
dispatched the re-evaluation by hand. Now:

* the wait reads the same required names gate item 3 reads (the branch's
  effective rules plus classic protection), and only one of those still
  running waits. A non-required check still running does not hold the merge;
* a check that already **failed** still refuses, required or not — #815
  narrows the wait, not gate item 5;
* GitHub reports a pull request whose required checks are green but a
  non-required one is running (or red) as `UNSTABLE`, and will not *enable*
  auto-merge on it, as on `CLEAN`. So `UNSTABLE` and `HAS_HOOKS` are merged
  directly, or enqueued on a merge-queue base, exactly like `CLEAN`; the
  ruleset still decides. A non-required check that fails after the gate read
  the checks is merged past in that window — as native auto-merge would
  have done — which #815 accepts;
* `check_suite: completed` dispatches the same re-evaluation for each open
  `ready` pull request at the suite's head. GitHub runs no workflow on
  `check_suite` for a suite GitHub Actions created, so it never fires for
  `application` or `security` — `workflow_run` stays the way in for those —
  only for another App's suite. It is a backstop: the narrowed wait above
  does not depend on it.

Why not something simpler. `workflow_run` cannot merge by itself: a merge
with its GITHUB_TOKEN starts no build and no release (next section), so it
dispatches, and the dispatched run mints the App token after the gate.
`check_suite: completed` alone would not do: GitHub does not run a workflow
on `check_suite` for a suite GitHub Actions created, so it is only the
backstop for other Apps' suites (above). And arming native auto-merge at
once, on a running *required* check, is not done either: the gate re-reads
the checks when it finishes, so a failed check — required or not — is
refused by gate item 5 instead of merged past. A `ready` label on
a pull request with no check run at all yet (opened seconds ago) passes the
gate and arms native auto-merge, as before; the re-evaluation then finds it
armed (or already merged) and leaves it.

**A refusal on a re-evaluation** — the title, the base, or a check that
failed — turns auto-merge off, **removes `ready`** and comments once, so the
next finishing run does not say it again; fix it and add `ready` again. It
does not fail the run, which sits on main's head commit. The two refusals
only the owner can fix (no required checks, no merge App) keep `ready`, which
is what the operator's merge watcher reads, and say nothing more than the
label run did; without `vars.MERGE_APP_ID` the `requeue` job does not run at
all. A refusal on the label itself is unchanged: comment, red run, label left
on.

Re-evaluations queue in their own concurrency group
(`auto-merge-requeue-<number>`): a queued run cancels the one queued before
it in its group, so a burst of finishing runs must not cancel a queued
`synchronize` run of the `disable` job. An operator can send the same
dispatch by hand (Actions → auto-merge → *Run workflow*, `pr`) to make it
look again; it is a request to look, not a `ready`.

### `ready-on-green` is marked `ready` by CI, not by a session

Owner decision, 2026-10-17: on that day an operator's laptop ran ~20 watcher
loops (`gh pr view` every 60-90 s per pull request) only to add `ready` once
a pull request went green, spending the owner's personal GitHub quota. A pull
request labelled `ready-on-green` now gets that from the `ready-on-green` job
in `auto-merge.yml`. It runs on the events `requeue` runs on (a pull
request's `workflow_run` finishing, any `check_suite`), for the
`ready-on-green` pull requests at that head, and every 15 minutes for all of
them as the backstop. Green is judged at the head GitHub reports for each
pull request with main's `scripts/merge-train.sh judge`: every required
check finished well and nothing that ran failed. The head is read again just
before labelling, so a push since is not promoted. Green adds `ready` and
removes `ready-on-green` with the merge App's token, never the GITHUB_TOKEN:
GitHub raises no `labeled` event for a label the GITHUB_TOKEN adds, so the
`enable` job, and with it auto-merge, would never start. A red head is left
alone, with one comment per head naming the failing checks; a `[swarm]
task_` title is skipped, as the gate would refuse it; without the merge App
the job does not run.

### Why the merge uses a GitHub App token, not the GITHUB_TOKEN

**A merge performed with the workflow's GITHUB_TOKEN starts no workflow.**
GitHub drops every event that token causes except `workflow_dispatch` and
`repository_dispatch`. The push to `main` would then run neither
`application.yml` — whose `build images` job is the
[one build of the commit](#images-are-built-once-per-commit-and-the-release-reuses-them)
— nor `release.yml`, which waits for that build. Nothing would go red: main
would silently stop being built and released.

A `workflow_run` trigger or a second `push` workflow does not fix that. Both
hang off an event the GITHUB_TOKEN merge never raised, and this workflow's own
run finishes when auto-merge is *enabled*, typically long before GitHub
merges. So auto-merge is enabled with a **GitHub App installation token**:
GitHub attributes the eventual merge to whoever enabled auto-merge, and a push
made by an App starts workflows the way a person's does. `release.yml` and
`application.yml` are unchanged and fire on that push exactly as they do for a
merge made by hand. The workflow never falls back to the GITHUB_TOKEN; without
the App it refuses.

The workflow's own GITHUB_TOKEN holds `contents: read` (to read the base
branch's protection), `checks: read` (to read the head commit's check runs)
and `pull-requests: write` (to comment on a refusal). The App token is minted
after the gate passes, scoped to this repository and to `contents: write` +
`pull-requests: write` + `workflows: write` — the last one so that a pull
request touching `.github/workflows` auto-merges too (owner decision,
2026-09-28). Requesting it on the token mint grants nothing by itself; nothing
is granted until the owner creates the App with that permission (below). The
job never checks out or runs the pull request's code, and the title reaches
bash only through `env:`.

**A leaked App private key could rewrite CI and release workflows** — the
`workflows` permission is exactly the permission to change what
`.github/workflows/*.yml` do on this repository's next push. That is why the
key lives only in `secrets.MERGE_APP_PRIVATE_KEY` (never in a file, a log or a
tfvars) and the App is installed on this repository only, not on the
organization or another repository.
[`test_auto_merge_workflow.py`](../tests/unit/scripts/test_auto_merge_workflow.py)
holds all of that and runs the gate against a fake `gh`.

### A merge closes the issues its keywords name

**GitHub did not close the issues named by the pull requests the App
merged.** Of 65 merged pull requests with a closing keyword, owner merges
closed 77 of 77 referenced issues; the `swarmcloud-merge` App's merges left 28
of 33 open (history analysis, 2026-10-05; #487 → #124/#125/#127/#128, #481 →
#89–#93, #541 → #503, #490 → #307), although GraphQL `closingIssuesReferences`
listed every one of them (#621). Owner decision, 2026-10-05: close them
explicitly after the merge, as the workflow `merge` step does (#581), with no
change to the App's permissions.

So `auto-merge.yml` also runs on `closed`, and its `close-issues` job runs
[`scripts/close-merged-issues.sh`](../scripts/close-merged-issues.sh) on every
pull request **merged into the default branch**:

* it reads the pull request's `closingIssuesReferences` and closes each one
  that is **open and in this repository**, with the comment "Closed by #N,
  merged into main ..." naming the merging pull request;
* it closes nothing else. It never reads the pull request's text, so a
  `part of #N` — which GitHub does not list as a closing reference — is never
  closed. Write `part of #N` for a partial fix, and a closing keyword only when
  it is unconditionally true (CLAUDE.md, "Issues");
* it **never closes a pull request**. Owner decision, 2026-10-08: #857's
  text said `fix #840`, and #840, an open pull request, was closed two
  seconds after #857 merged. When a closing keyword names a pull request, the
  script leaves that pull request open, whatever its state. It also posts one
  comment on the merged pull request: "#N is a pull request named by a closing
  keyword in this pull request's text; it was left open ...".
  `closingIssuesReferences` is an `IssueConnection`, so GitHub types every
  node as `Issue`, and a `... on PullRequest` fragment there is a GraphQL
  validation error. For that reason the script reads the REST issue endpoint
  before every close: its `pull_request` key is the authoritative answer. If
  that check fails, nothing is closed for that reference and the job fails.
  This guard covers only the script's own closes. In the #857 merge, the
  job's log reads "#857 has no closing references", and #840's `closed`
  event came at 04:16:21, nine seconds before the job ran. Its actor is
  `swarmcloud-merge[bot]`, the merging App, so GitHub's own keyword handling
  closed #840. Nothing in this repository can stop that. The only defence is
  never to put a closing keyword before a pull request's number;
* an issue that is already closed is left alone, with no second comment, and
  one in another repository is recorded in the run summary, not touched;
* an unreadable answer or an unmerged pull request fails the job and closes
  nothing; a refused close fails it after the others have been tried, naming
  the issue; more than 100 references (one page) fails it after the page.

It runs on every merge, not only the App's, because the event does not
reliably say who merged and an owner's merge costs it one read: GitHub has
closed those issues already, and the script skips a closed issue. A merge into
any other branch does nothing, because GitHub's keywords act only on the
default branch.

**Least privilege:** the job's GITHUB_TOKEN holds `issues: write` (comment and
close), `pull-requests: read` (the references) and `contents: read` (a sparse
checkout of the default branch's `scripts/`, without persisted credentials);
no other job in the workflow holds `issues`, and the job mints no App token.
It checks out the default branch — the code the merge just made, the same main
the release builds — never the pull request's head. A close made with the
GITHUB_TOKEN starts no workflow, which is right here: nothing should.
[`test_close_merged_issues.py`](../tests/unit/scripts/test_close_merged_issues.py)
runs the script against a fake `gh` (`Closes` vs `part of` vs already closed,
another repository, a referenced pull request, an unmerged or unreadable pull
request), and
[`test_auto_merge_workflow.py`](../tests/unit/scripts/test_auto_merge_workflow.py)
holds the job's trigger, permissions and checkout.

The issues App merges left open before this existed are not closed by it: it
acts on the merge event only, so they are checked against main and closed by
hand (#621).

### What the owner applies, once

These are repository settings. A workflow cannot apply them and no lane
should; they are here so that applying them is copying three commands.

**1. The merge App** (step by step: [runbook](runbooks/merge-app.md)). Create a GitHub App (Settings → Developer settings →
GitHub Apps) with no webhook and exactly three repository permissions,
**Contents: Read and write**, **Pull requests: Read and write** and
**Workflows: Read and write**; install it on this repository only.

**A leaked App private key could rewrite CI and release workflows.** The
`workflows` permission is the permission to change what
`.github/workflows/*.yml` do on the next push, so this key is more sensitive
than the other two: it lives only in the `secrets.MERGE_APP_PRIVATE_KEY`
Actions secret, never in this repository, a tfvars file, a Job environment or
a log, and the App is installed on this repository only — never the
organization, never another repository.

Then:

```bash
gh variable set MERGE_APP_ID --repo bogdan-alexandrescu/SwarmCloud --body '<the App ID>'
gh secret set MERGE_APP_PRIVATE_KEY --env repo-secrets --repo bogdan-alexandrescu/SwarmCloud < merge-app.private-key.pem
rm merge-app.private-key.pem
```

The key lives only in that Actions secret: never in this repository, a tfvars
file or a log. (It is not a tenant's forge token, which lives in Secret
Manager as `swarm-tenant-<tenant>-git`; this key merges this repository's own
pull requests and is read only by `auto-merge.yml`, `merge-train.yml` and
`runner-fallback.yml`.)

**Both of this repository's GitHub-writing secrets live in the environment
`repo-secrets`, not at repository level (BGD's decision, 2026-10-23,
REPO-SECRETS-ENV, part of #2029).** `MERGE_APP_PRIVATE_KEY` and
`SWARM_SANDBOX_GITHUB_TOKEN` were repository secrets, and a repository secret
is readable by a workflow on ANY pushed branch: the branch's own workflow file
runs on the push, before any review, merge hold or `STOP_WORKFLOWS`, so once
agents can push workflow files a branch could echo the key that merges pull
requests. `repo-secrets` admits deployment branch `main` only, with no
reviewers and no wait timer, and every job that reads either secret declares
it (`environment: {name: repo-secrets, deployment: false}`):
auto-merge.yml `enable`, `disable` and `ready-on-green`, merge-train.yml
`tick`, runner-fallback.yml `judge`, and accept.yml `sandbox` and
`sandbox-results`. Their triggers all run on main's ref --
`pull_request_target` (main's ref whatever the base, since GitHub's
2025-12-08 change), `workflow_run`, `check_suite`, `schedule`, and a
`workflow_dispatch` from main -- so nothing they did stops; a dispatch from
another branch is refused, which is the point. `deployment: false` reads the
environment's secrets without writing a deployment record (GitHub,
[using environments without deployments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)).
[`test_repo_secrets_environment.py`](../tests/unit/scripts/test_repo_secrets_environment.py)
scans every workflow file and fails on a job that reads either secret without
the environment. Google Cloud needed no such move: its workload identity
binding already pins the ref to main. The switch-over runs in this order:
the owner enters both values in `repo-secrets` first; then the change that
adds the `environment:` lines merges; then the repository-level copies are
deleted (`gh secret delete MERGE_APP_PRIVATE_KEY` and
`gh secret delete SWARM_SANDBOX_GITHUB_TOKEN`). Deleting first would leave
every merge refused ("The merge App is not configured") and every acceptance
sync failing until the merge.

**2. Auto-merge allowed on the repository:**

```bash
gh api --method PATCH repos/bogdan-alexandrescu/SwarmCloud -F allow_auto_merge=true
```

(Superseded on 2026-09-29 by the repository ruleset `main-protection`, which
requires the same four checks plus `ci-gate`; see
[the ruleset on main](#the-ruleset-on-main-and-ci-gate). **The ruleset is what
supplies `main`'s required checks; classic branch protection is not applied**
(`GET .../branches/main/protection` is 404, read 2026-10-01). The command
below is kept as the record of why each value is what it is, and
`auto-merge.yml` would still count its checks if it were applied.)

**3. Branch protection on `main`**, requiring only the checks that run on
**every** pull request — `security.yml`'s four jobs. `application.yml` (until
2026-10-07) and `terraform.yml` both have a `pull_request` path filter
([table above](#the-workflows-and-what-each-one-is-responsible-for)), so
none of their jobs — including `shellcheck` and
`release workflow wiring (actionlint)` — report on a pull request that does
not touch their paths. GitHub treats a required check whose workflow was
skipped by a path filter as **pending, forever**, not as passed, so a required
check list may hold only checks that report on every pull request:

```bash
gh api --method PUT \
  repos/bogdan-alexandrescu/SwarmCloud/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "checks": [
      {"context": "trivy (repo)", "app_id": 15368},
      {"context": "secret scan", "app_id": 15368},
      {"context": "checkov (terraform + kubernetes)", "app_id": 15368},
      {"context": "platform policy assertions", "app_id": 15368}
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON
```

Why each value:

* **The four checks are exactly `security.yml`'s jobs that run on every pull
  request.** Its fifth job, `trivy (published images)`, runs only on a
  schedule or `workflow_dispatch`, never on a pull request, so it is not
  listed for the same reason `build images` and `plan` are not: a required
  check that is never reported holds the pull request forever.
* **`shellcheck` and `release workflow wiring (actionlint)` are deliberately
  left out**, even though they are useful signal. `application.yml`'s
  `pull_request` path filter means they do not report on a pull request
  outside its paths, and GitHub has no way to require a check "when its
  workflow ran." `terraform.yml`'s jobs are excluded for the same reason.
  `auto-merge.yml`'s gate (above) is what still holds the merge on these when
  they DO run: it reads the head commit's check runs directly and refuses to
  queue while any of them is failing or still in progress, required or not.
* **`app_id: 15368`** is GitHub Actions. Pinning it means a check of the same
  name posted by any other App or token cannot satisfy the rule.
* **`strict: false`**: auto-merge never updates a branch, so "must be up to
  date with main" would hold every pull request that fell behind main until a
  person clicked *Update branch* — the session dependency this removes.
* **`enforce_admins: false`** keeps the owner's manual merge as the emergency
  path. The App is not an admin and cannot bypass.
* **`required_pull_request_reviews: null`, `restrictions: null`**: the PUT
  rejects a body without them; `ready` is the review decision here.

`test_auto_merge_workflow.py` holds this command's check names to the jobs
that exist and to the ones `security.yml` runs on every pull request, so
renaming a job, adding a path filter to `security.yml`, or widening the
required list past what always runs fails CI.

### SwarmCloud merges its own pull requests

What main does as of 2026-10-10 (#295):

* **A workflow spec may set `merge: "on_merge_verdict"`** in its `metadata`
  (`MERGE_ON_VERDICT` in
  [`validation.py`](../apps/swarm-api/swarm_api/validation.py), #998). That
  appends a re-review on the integrator's pushed head and a `merge` step,
  which merges the integrator's pull request only when that review says
  MERGE and CI is green.
* **Merge steps on one repository take a single merge slot** and land in
  submission order, so concurrent merges no longer refuse
  `behind_too_often` behind each other
  ([`mergeslot.py`](../apps/agent-worker/agent_worker/mergeslot.py), #1002).
* **A repository's `merge_policy` sets the default** for a workflow whose
  `metadata.merge` says nothing (`MERGE_POLICIES` in
  [`validation.py`](../apps/swarm-api/swarm_api/validation.py), #998;
  [workflows.md](workflows.md#a-repositorys-merge_policy)).

## The ruleset on main, and ci-gate

**Owner decision, 2026-09-29.** `main` is protected by a repository ruleset,
**`main-protection` (id `24160219`)**, not by the classic branch protection
in step 3 above. It forbids deleting `main` and force-pushing to it, requires
a pull request (no approving review: `ready` is the review decision), and
requires five status checks: `security.yml`'s `secret scan`, `trivy (repo)`,
`checkov (terraform + kubernetes)` and `platform policy assertions`, and
`ci-gate` (added by the owner step below; read live on 2026-10-01 with
`gh api repos/bogdan-alexandrescu/SwarmCloud/rules/branches/main`). These are
the checks `auto-merge.yml` waits on: its gate reads the same effective-rules
endpoint.

### Why only security is required directly

`security.yml` has no path filter, so its four jobs report on every pull
request. `terraform.yml` carries a `pull_request` path filter, and
`application.yml` did until 2026-10-07 ([table above](#the-workflows-and-what-each-one-is-responsible-for)),
and **GitHub holds a required check whose workflow was filtered out as
pending, forever** — not skipped, not passed. Requiring `format / unit tests`
directly would freeze every pull request that touches only `terraform/`;
requiring `terraform test` would freeze every docs-only one. So before
`ci-gate`, nothing *required* held a pull request on its unit tests, its
integration tests or its terraform tests; only `auto-merge.yml`'s gate
(which reads the head's check runs when `ready` lands) did.

### What ci-gate does

**Since 2026-10-07 `ci-gate` is the last job of
[`application.yml`](../.github/workflows/application.yml)**, not a workflow of
its own. Until then `ci-gate.yml` ran one job that started with the pull
request and polled the Actions API every 60 s until `application.yml` and
`terraform.yml` had finished — a runner held for the whole ~12 minutes of CI on
every pull request, at a time when twelve pull requests opened together queued
their jobs 400-600 s for runners ([below](#the-merge-pipelines-wall-time)).
Now GitHub starts it when the last job it `needs` has finished, and it holds a
runner for seconds. Its name and its meaning are unchanged: the required
context is the job's name, exactly `ci-gate`, and it passes only when
everything `application.yml` and `terraform.yml` ran for the commit passed.

`application.yml` therefore runs on **every** pull request — a workflow
filtered out by `on.pull_request.paths` reports nothing, and the ruleset would
hold the pull request forever — and its first job, `what this change reaches`,
decides which of its other jobs a change can affect. The rest is skipped by its
own `if:`.

* **`application.yml`'s jobs: judged through `needs`.** `ci-gate` needs every
  other job of the workflow (a test fails when one is left out) except the two
  that only keep an old check name, `format / unit tests` and `swarm-ui
  typecheck / component tests`: each runs nothing but the judgement below over
  jobs the gate already needs (a test holds both halves), and waiting for them
  was one more runner queue on the critical path
  ([measured](#where-pull-request-ci-time-goes-2026-10-09)). It passes only
  when each concluded `success` or `skipped`, and `what this change reaches`
  itself succeeded. A job skipped because something it needs failed is
  `skipped` too, but what failed is in the same list and fails the gate by
  name; and a skip under a gate job that did not succeed means nothing, so
  that fails it too. `if: always()` makes it report when something it needs
  failed — a skipped `ci-gate` would read as passed.
* **`terraform.yml`: waited for, by
  [`scripts/ci-gate.sh wait`](../scripts/ci-gate.sh)**, told by
  `CI_GATE_WORKFLOWS` to gate `terraform.yml` alone (waiting for
  `application.yml` from inside it would wait for itself). The script's rules
  are as they were:
  * **Did not run because the paths did not match: pass.** Which workflows a
    change triggers is computed from the `pull_request.paths` lists **read out
    of the workflow files themselves**, against `git diff base...head` (the
    three-dot diff GitHub's filter uses). There is no second copy of those
    lists to drift.
  * **Should have run, and its run does not exist yet: wait.** "No run yet"
    looks exactly like "not triggered" in the API. An expected run that has
    not appeared after 10 minutes fails the gate by name.
  * **Ran and failed, was cancelled, timed out, awaits approval or failed to
    start: fail**, naming the job and its URL.
  * **A `skipped` job passes only inside a run that concluded `success`** — a
    skip its own `if:` chose, like `plan` on a pull request. A run that failed
    to start (`startup_failure`), or whose jobs were skipped because something
    they need failed, does not conclude `success`, and fails the gate whatever
    its jobs say.
  * **A run the path model did not predict is still judged** when it fails.
    The script can also keep looking for a while before it passes
    (`CI_GATE_SETTLE`), which mattered when the gate started on the event,
    often before the other runs existed. The job sets it to zero: it starts
    only after `what this change reaches` — a job of a run the same event
    created — has finished, and the runs of one event are created together.
    A 60 s window would add 60 s to every pull request for nothing.
  * **An unreadable API is never a pass**, and a run still going after 90
    minutes fails it with what was pending. It looks every 20 s; terraform.yml
    takes ~80 s and has usually finished long before.

The script reads runs from the Actions API (`actions: read`), filtered by head
sha and event, rather than matching check-run names: a check run is named by
its job's `name:`, which is not unique across workflows and cannot say whether
its workflow started at all. The job's token holds `actions: read` and
`contents: read` and nothing else; every value reaches the shell through
`env:`. [`test_ci_gate.py`](../tests/unit/scripts/test_ci_gate.py) runs the
real script against a fake `gh` in the configuration the job gives it, runs
the job's own `needs` judgement over passing, skipped, failed and cancelled
results, and holds the path lists the script reads to what PyYAML reads.

**If `application.yml` fails to start at all**, no `ci-gate` check exists and
the ruleset holds the pull request: closed, not open.

**Re-running a failed job re-runs `ci-gate` with it** when you choose *Re-run
failed jobs*: the gate failed too, and depends on the job. (Under
`ci-gate.yml` it did not, and had to be re-run by hand.)

**What it does not stop.** `pull_request` runs the pull request's own copy of
`application.yml` and `scripts/ci-gate.sh`, so a pull request that edits either
can make its own gate pass. Review of those two files is the guard.

`auto-merge.yml` still lists `ci-gate` among the workflows whose completion
re-evaluates a `ready` pull request. No workflow has that name now, so the
entry never fires and costs nothing; `application`'s completion is the gate's.
It is left as it is while another decision on that file is pending.

### Owner step: require ci-gate, once it is on main

**Not before it is on `main`**: until then no pull request reports a
`ci-gate` check, and requiring it would hold every one of them forever. Once
the pull request adding it has merged and `ci-gate` has reported green on a
pull request, the owner (or the orchestrator, with the owner's go-ahead) adds
it to the ruleset. The PUT replaces the ruleset whole, so the body restates
every rule it has today (read on 2026-09-29) and adds only `ci-gate`. No check
is pinned to an `integration_id` (owner decision, 2026-09-29), the four
security checks included, as today:

```bash
gh api -X PUT repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219 \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
  },
  "bypass_actors": [
    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
  ],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "required_reviewers": [],
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "require_extra_approval_for_unattributed_changes": true,
        "allowed_merge_methods": ["merge", "squash", "rebase"]
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          {"context": "secret scan"},
          {"context": "trivy (repo)"},
          {"context": "checkov (terraform + kubernetes)"},
          {"context": "platform policy assertions"},
          {"context": "ci-gate"}
        ]
      }
    }
  ]
}
JSON
```

Then read it back and check the list:
`gh api repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219 --jq '.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks'`.
Before sending, compare the body with a fresh read of the ruleset: a rule
added since 2026-09-29 that is not in this body would be removed by the PUT.
[`test_ci_gate.py`](../tests/unit/scripts/test_ci_gate.py) holds this body to
`security.yml`'s always-run jobs plus `ci-gate`, none pinned.

## The merge pipeline's wall time

**Owner request, 2026-10-07.** With `strict_required_status_checks_policy` on,
a pull request merges only when it is up to date with `main` and green at that
head, so exactly one pull request merges per CI cycle, and every merged pull
request runs full CI twice (once at its own head, once after the update). On
the night of 2026-10-07 (#754-#794) that measured:

| measure | value |
|---|---|
| merges | 24 in 5 h 47 m — 4.15 an hour |
| CI at a pull request's final head | median **707 s**, p90 796 s |
| share of the time between merges spent in CI | **82 %** (16,990 of 20,818 s) |
| long-pole jobs, uncontended run 37579951405 | `swarm-ui typecheck / component tests` 660 s · `format / unit tests` 646 s · `build changed images without pushing` 260 s · `integration tests (emulator)` 163 s |
| `ci-gate` | median 735 s, every second of it holding a runner while it polled |
| twelve pull requests opened together | jobs queued 400-600 s for a runner |

CI is the throughput limit, and inside CI two serial jobs set the time. So:

**1. The Python unit suite runs in four shards.** `unit tests (k/4)` each run
a quarter of the files under `tests/unit`, picked by the `pick this shard's
test files` step: every file, longest first, onto the lightest shard (LPT),
weighted by [`tests/unit/scripts/unit_test_durations.json`](../tests/unit/scripts/unit_test_durations.json)
(a file it has no weight for counts at the median). The split is
deterministic, so each shard computes the same one and takes its own part, and
every file runs exactly once. **Weights, not file counts**: one file
(`test_offboard_tenant.py`) is 12 % of the suite, and splitting by hash or
round-robin measured 1.20-1.59 times the even share on the slowest shard,
against 1.00 with the weights. Measured 2026-10-07 in a SwarmCloud container:
2,943 s of test time over 550 files, ~736 s a shard. At the rate the old job
ran (~2,943 s in ~520 s of `pytest -n auto` on a 4-vCPU runner) that is ~130 s
of tests plus ~60 s of setup a shard, about three minutes; more shards buy
less each and cost a runner apiece. The rest of the old job — the lock check,
the frozen-contract check, the format pass, the bridge install, the
destroy-guard self-test — is `lock / contract / format / bridge install`,
beside the shards rather than in front of them.

**2. The vitest suite runs in four shards** — `swarm-ui component tests
(k/4)`, vitest's own `--shard=k/4`, through the new `test:components` script
(`vitest run` alone, so the flag reaches vitest and not the last of the three
runners `npm test` chains). The typecheck, the two node:test runners
(`test:node`) and the production build are one job beside them, `swarm-ui
typecheck / build`. Measured 2026-10-07: shard 4/4 ran 60 of 246 files in
108 s on 5 cores.

**3. The check names are kept.** `format / unit tests` and `swarm-ui typecheck
/ component tests` are now jobs that `needs:` their shards, run `if: always()`
and pass only when each passed or was skipped by its own `if:` (and `what this
change reaches` succeeded). Neither is required by the ruleset — only the four
security checks and `ci-gate` are — but `auto-merge.yml`'s comments, the CI
fixer's log reads and this document name them, and `build images` still needs
`format / unit tests`.

**4. A change runs what it can reach** (`what this change reaches`, which
grew out of the swarm-ui gate of #605):

| the pull request changes | Python unit shards | `lock / contract / format / bridge install` | swarm-ui jobs | everything else in `application.yml` |
|---|---|---|---|---|
| only `docs/**` | only the test files that read `docs/` (~36 of 551) | skipped | skipped | runs |
| only `apps/swarm-ui/**` | only the test files that read `apps/swarm-ui/` (~51) | skipped | run | runs |
| only those two | the union (~81) | skipped | run | runs |
| anything else in the list (the old `pull_request.paths`) | all | runs | run if the change reaches swarm-ui (as #605 decided) | runs |
| nothing in the list (e.g. only `terraform/infra/`) | skipped | skipped | skipped | skipped — only `ci-gate`, which waits for `terraform.yml` |
| a push to `main`, a queue entry, a dispatch | all | runs | run | runs |

**A docs-only change still runs the Python tests that read `docs/`.**
`test_docs_describe_what_was_built`, `test_docs_spec_amendments` and ~30 others
hold the documentation to the code; skipping them would let a docs-only pull
request break `main`. "Reads" is found by the pick step, not listed: a module
whose source builds a path through the directory (`REPO / "docs"`,
`"apps/swarm-ui/src"`), every module that imports such a module (a helper, or
a test module used as one, as `test_issue_forms.py` imports
`test_nav_headings_agree.py`), and everything under a `conftest.py` that reads
it. A mention in prose or a dict key named `"docs"` is not a read: matching
those pulled in 496 of 551 files. A test that builds the path some other way
would be missed on the pull request and caught by `main`'s push run, which runs
everything — the one gap this leaves, stated so nobody relies on it.

A shard left with no file runs no `pytest` — a bare `pytest` runs its
`testpaths`, the whole suite.

**5. `ci-gate` holds no runner while it waits** — it is the last job of
`application.yml` ([above](#what-ci-gate-does)).

**Expected, not yet measured on a real run.** The critical path of a pull
request that touches Python was `format / unit tests` (~673 s) or the UI job
(~660 s); it becomes `what this change reaches` (~20 s) → the slowest of a
unit shard (~190-220 s), a vitest shard or `swarm-ui typecheck / build`
(~180 s), `build changed images without pushing` (260 s when an image input
changed, seconds otherwise) and `integration tests (emulator)` (163 s) → the
aggregating job and `ci-gate` (~20-40 s with runner pickup). That is **about
4-5 minutes** where the median was 707 s, before any runner queueing; a
docs-only pull request is about 2-3 minutes. The price is runners at once: a
Python-and-UI pull request starts up to 18 jobs instead of 10, though for
fewer runner-minutes in total (the old `ci-gate` alone held ~12), so a burst of
pull requests still queues — it queues for minutes of work, not for a poller.
The first runs after this lands are the measurement; correct these numbers
from them.

### Refreshing the shard weights

The weights go stale as tests are added and change — the split stays correct
(every file still runs exactly once, an unknown one at the median weight) but
drifts out of balance. Every shard on `main` uploads what it measured, per
file, as the artifact `unit-durations-<k>` (30 days).

**Stale weights warn; they do not fail (#2364).** `test_unit_shards.py` used to
fail when fewer than 80 % of the test files had a weight. The file was
refreshed only by hand, so the test files that landed between refreshes pushed
coverage under the floor (1069 of 1337 weighted) and turned main and every
pull request red — over a staleness that only unbalances the split. Now
`scripts/lib/unit_shards.py count` compares the weighted share of the files the
run selects with `WEIGHT_COVERAGE_WARN` (0.8) and, below it, writes a
`::warning` and a step-summary line and carries on.
`test_stale_weights_warn_and_still_split_within_budget` holds that a quarter
unweighted still exits 0 with every shard within budget. The test still fails
hard on a malformed file: a key that is not a `tests/unit/**.py` path, or a
weight that is not a positive number. It also holds the split to within 10 % of
an even share.

**To refresh**, one command, then review the diff and open a pull request with
the file:

```bash
scripts/refresh-unit-weights.sh            # or --run <id> for a given run
```

It finds the newest successful `push` run of `application.yml` on `main` that
has `unit-durations-*` artifacts. A push run that skipped `python-unit`,
because a run of the identical tree had already passed, uploads none, so it
walks back through the last 20 (`--limit`). It downloads them and hands them
to `scripts/lib/merge_unit_weights.py`. That replaces the old
`jq -S -s add`, which dropped every file the run had not measured and kept
every deleted one:

* a measured weight replaces the stored one;
* a file that no longer exists is dropped;
* a file that exists but was not measured keeps its stored weight;
* a measured path that is not an existing `tests/unit/**.py` is ignored.

The output is sorted, indent-1 JSON with a trailing newline, the shards' own
artifact format, so a refresh with nothing new changes no byte (the first
refresh after this rewrites the indentation from jq's two spaces). A download
with no `unit-durations.json`, or a malformed one, exits 1 and writes nothing:
an unread download is never "nothing to refresh". Only the weights' ratios
matter, so CI's seconds replacing a container's is fine.

Like `scripts/refresh-ui-weights.sh`, the script changes no workflow. A
scheduled workflow that runs it and opens the pull request with the merge
App's token lives under `.github/workflows/`, which only swarm-api can push,
so it is proposed separately rather than here.

### Where pull-request CI time goes (2026-10-09)

**Why this was measured.** Merging is the bottleneck: with strict up-to-date
merging every merge re-runs CI on the next pull request, and the operator
measured 15-25 minutes a run on 2026-10-09. The numbers above are 2026-10-07's
*expected* ones; these are what the runs did.

**Method.** The public REST API, unauthenticated, read on 2026-10-09 at
~03:00 UTC: `GET /repos/{repo}/actions/workflows/application.yml/runs?event=pull_request&status=completed&per_page=30`,
then `GET /actions/runs/{id}/jobs?filter=latest&per_page=100` for each.
**30 runs and 540 job records read.** 12 of the 30 were `cancelled` —
superseded by a newer push to the same branch — and are left out of the
timings; 18 remain (16 `success`, 1 `failure`, plus 1 that reached nothing and
ran only `changes` and `ci-gate`, 162 s). Queue is `created_at → started_at`
(waiting for a runner); run is `started_at → completed_at`. Step times come
from the same records. Job logs need a token, so step names, not log lines,
are as deep as this goes.

**Every job, over the 17 runs that reached the workflow** (seconds; `n` is
how many job records ran, so the shards count four a run):

| job | n | queue median | queue max | run median | run p90 | run max |
|---|---:|---:|---:|---:|---:|---:|
| `unit tests (k/4)` | 68 | 231 | 546 | 212 | 366 | 417 |
| `build changed images without pushing` | 17 | 192 | 539 | 151 | 356 | 502 |
| `integration tests (emulator)` | 17 | 182 | 396 | 149 | 157 | 161 |
| `shellcheck` | 17 | 201 | 510 | 134 | 153 | 170 |
| `swarm-ui component tests (k/4)` | 12 | 393 | 570 | 118 | 438 | 565 |
| `swarm-ui typecheck / build` | 3 | 330 | 436 | 55 | 55 | 59 |
| `lock / contract / format / bridge install` | 16 | 184 | 445 | 23 | 24 | 33 |
| `kubernetes manifests` | 17 | 211 | 364 | 12 | 14 | 14 |
| `release workflow wiring (actionlint)` | 17 | 225 | 408 | 6 | 7 | 7 |
| `what this change reaches` | 18 | 73 | 375 | 8 | 8 | 8 |
| `format / unit tests` (name-keeper) | 18 | 40 | 300 | 3 | 4 | 5 |
| `swarm-ui typecheck / component tests` (name-keeper) | 18 | 270 | 468 | 3 | 4 | 4 |
| `ci-gate` | 18 | 34 | 106 | 8 | 10 | 112 |

**Inside the long jobs** (step medians): a unit shard is 196 s of `pytest` in
212 s, so setup is ~15 s (uv and npm are already cached); the integration job
is 109 s of tests and 19 s installing the emulator; `shellcheck` is 96 s of
shellcheck; the image build spends a median **92 s (max 261 s) freeing runner
disk** against 30 s building.

**The shards are not balanced, inside or across** (test-step seconds):

| shard | unit median | unit max | vitest median | vitest max |
|---|---:|---:|---:|---:|
| 1/4 | 190 | 234 | **469** | **555** |
| 2/4 | 145 | 184 | 91 | 93 |
| 3/4 | **347** | **403** | 95 | 101 |
| 4/4 | 235 | 264 | 125 | 127 |

**The critical path, run by run** — the last job before `ci-gate`, and the
runner waits on the chain `changes → that job → its name-keeper → ci-gate`:

| last to finish | runs | wall median (range) | queued on the path, median |
|---|---:|---:|---:|
| `unit tests (3/4)` | 8 | 983 s (569-1552) | 394 s |
| another unit shard | 4 | 678 s (525-812) | 429 s |
| `swarm-ui component tests (1/4)` | 3 | 1517 s (1495-1567) | 785 s |
| `build changed images without pushing` | 2 | 889 s (843-935) | 308 s |

Over all 17: wall median **935 s**, and a median **440 s of it — 47 % — is
the critical path waiting for runners**. So, in order:

1. **Runners.** A run starts 14 jobs, 19 with the UI, and pull requests arrive
   together, so every hop on the path waits: 73 s for `changes`, ~230 s for
   a shard, 40 s for the name-keeper, 34 s for `ci-gate`, at the median. More
   shards would add runners to that queue and are **not** the answer here.
2. **Shard 3/4 of the unit suite** — last in 8 of 17 runs, its tests ~2.4
   times shard 2's and ~1.5 times the four shards' mean. The weights balance the shards' *summed* test time to the second
   (863 s each); what is unbalanced is xdist's schedule inside the shard. Its
   default `load` hands each worker a block up front, and shard 3 holds the
   six sleep-bound tests of `test_startup_is_loud.py` (30-67 s each, waiting
   out real DNS back-offs), which land together. Measured in a SwarmCloud
   container, shard 3's files on 4 workers: **404 s under `load`, 287 s under
   `--dist worksteal`**, against a floor of 276 s (summed test time / 4).
3. **vitest shard 1/4** — last in every run that ran the UI, at ~4-5 times the
   other shards. Not a split problem: `spacing.test.tsx` is one test that
   walks every route in both themes — 1,089 s of 1,670 s for shard 1's 72
   files in a full local run (288 files, 12 workers, so slower than CI's
   3) — and vitest runs a file in one worker, so no assignment of files gets
   below it.
4. **The name-keeper hop** — `format / unit tests` waited a median 40 s, up to
   300 s, for a runner to report three seconds of judgement that `ci-gate`
   then repeats.

**What changed, from this table:**

* the unit shards run `pytest -n auto --dist worksteal` (2.);
* `ci-gate` no longer `needs` the two name-keepers (4.). They still run and
  report; each needs only jobs the gate needs and runs the gate's own
  judgement step word for word, which `test_ci_gate.py` holds;
* `build changed images without pushing` is skipped by its own `if:` when
  `what this change reaches` finds no image input changed, so it takes no
  runner (1.), and frees disk only for an `agent-runtime-*` image (the 92 s);
* **not changed**: the shard counts (1. says why), the uv and npm caches (on
  already, and setup is ~15 s), the vitest split (3.: the file is the floor;
  splitting `spacing.test.tsx` by route is the fix, and it is the swarm-ui
  track's file, so it is a question to the owner), and every required check,
  the strict policy and the merge rules.

**Expected, by replaying the 17 runs** with every measured queue wait kept
as it was (so it under-counts: shorter jobs also shorten other runs' queues):
the change cuts a median **130 s** a run (mean 143 s, up to 439 s), a median
run going from **935 s to ~835 s**. By class:

| pull request | measured median | expected | what bounds it after |
|---|---:|---:|---|
| docs-only | (none in the sample) | ~7.5 min (`changes` 81 s + integration's 182 s queue and 149 s run + `ci-gate` 42 s, at the medians) | `integration tests (emulator)` and `shellcheck` with their queues; the docs subset of the unit suite, and no image job |
| UI-only | 1495 s (1 run) | ~1430 s | `spacing.test.tsx` in vitest shard 1 |
| API / full, no UI | 828 s (14 runs) | ~790 s | the slowest unit shard (~250 s) or the image build, plus the runner queues |
| full with UI | 1542 s (2 runs) | ~1315 s | `spacing.test.tsx` |
| reaches nothing | 162 s (1 run) | unchanged | `ci-gate` |

The first runs after this lands are the measurement: re-read them with the
same two calls and correct this table.

## CI starts when the implement step pushes

**Why (LANE CI-EARLY, part of #1694, owner decision 2026-10-20).** A read of
117 workflows found the merge step spending 40 % of a workflow waiting for CI
— 20.5 h in the sample — because nothing was tested until the fix step opened
the pull request. Under the integrate strategy the implement step is a
`contributor`: it pushes `swarm/<task id>` and opens no pull request
(`agent_worker/lifecycle.py`, the contributor's early return); the
`integrator` (the fix step) pushes its own branch and opens the pull request
after review. Measured CI then took a median 14.5 min (p90 20.9).

**What runs.** `application.yml`, `security.yml` and `terraform.yml` — every
workflow behind a required check, `terraform.yml` because `ci-gate` waits for
it — also run on a push to `swarm/**`. A lane push runs the whole of each, as
`main` does: the `changes` job treats any event but `pull_request` as "run
everything". It never builds or pushes an image (`build images` is held to
`main` and dispatch by its `if:`), never plans (`plan` likewise;
`plan-not-run` says so in the run), and never releases (`release.yml` is
`main`-only). `trivy (repo)` compares a lane push with `main`'s tip, the base
its pull request will have; with no base every finding `main` already carries
would fail it. The CI fixer acts only on a pull request's run, so one red
head does not start two fixers.

**Why push triggers and not an early draft pull request.** The owner offered
both; the merge step's rule — every required check green at the head — is
exact only under push triggers:

* `agent_worker/merge.py` reads every check run at the head commit, from any
  event, and holds a required check green only when **all** its runs are
  (`required_check_state`: any run in progress is pending, any red one is
  red). A push run at the head is one more run that must be green. Nothing is
  relaxed, and the pull request's own run still happens on every head.
* A draft opened at implement would be a *different* pull request from the
  one merged: the integrator pushes `swarm/<its own task id>`, merging the
  contributors' branches. Keeping one pull request would mean changing which
  branch the integrator publishes to, in the worker and in swarm-api.
* The merge step refuses a draft (`merge.py`, refusal `draft`), so the fix
  step would have to mark it ready, and a red draft would start `ci-fix.yml`
  on a branch the fix step is about to write.

**What it costs.** A lane branch's runs, cancelled by the next push to the same
branch (the concurrency group is the ref everywhere but `main`). The
integrator's head is pushed *and* opened as a pull request, so it carries two
runs, and the merge step waits for both: the pull request's selected run and
the push's full one. Whether the push run should narrow to what the pull
request would select, or the pull request's run reuse a lane push of the
identical tree (as `main` does, below), was put to the owner in the lane's
pull request.

### shellcheck runs in parallel, and on a pull request over what it reaches

`shellcheck` was the slowest check on 19 of 65 heads (291 s median, p90 320 s):
one serial `shellcheck -x` over every script. `scripts/lib/shellcheck-ci.sh`
hands each file to its own `shellcheck -x` over the runner's cores
(`xargs -P`) — shellcheck analyses each file it is given as a separate root,
so the findings are the serial run's. On `main`, a lane push and a queue entry
it checks exactly the files the serial command named. On a pull request it
checks the changed scripts, what they source, and **every script that sources
a changed file**, through any chain: under `-x` a library is analysed in its
callers' context, so removing a variable from `common.sh` shows up as SC2154
in its callers, never in `common.sh`. The edges are the `# shellcheck source=`
directives, because they are what `-x` follows. A change to the runner itself,
a `.shellcheckrc` or `application.yml` checks everything. Over every file on a
6-core container it took 85 s of wall for 312 s of CPU.

### The unit shards are sized by measured weight

The four fixed shards were sized on 2026-10-07 for ~2,950 s of measured test
time. By 2026-10-20 `unit_test_durations.json` summed to ~7,000 s, so a full
run's shard carried ~1,750 s — a 455-501 s shard p90 (max 829) against a
152-188 s median, the median being pull requests' small selections. The split
was already by weight; the count was the problem. `scripts/lib/unit_shards.py
count`, in the `changes` job, now sizes the matrix from the weight of the files
the run will test: about 600 s a shard, at most eight. A full run gets eight,
~875 s each (at most ~1.5x the target); a selection worth one target gets one
runner instead of four. `pick` splits with the same code, so the matrix and
the split never disagree on what runs.

## Flaky tests and the quarantine

**Owner decision, 2026-10-14: "Fix the known flakes and quarantine new
ones".** A flake costs a merge a rerun, and with strict up-to-date merging
every merge behind it waits too. A flake is also invisible in a pull
request's final state: the rerun went green, and only the run's earlier
attempt remembers the red job.

### What was measured (2026-10-14)

`scripts/flaky-report.sh --limit 1000` over `application.yml`, the 1,000 runs
from 2026-10-10 to 2026-10-14: 8 runs failed at a commit and then passed at
the same commit. By job and cause:

| job | times | test or step | cause | done |
|---|---:|---|---|---|
| `unit tests (2/4)` | 2 | `test_live_api.py::test_the_viewer_socket_extends_the_hold_while_it_is_open` (PRs 1267 and 1268) | the test left the socket's `with` as soon as it read the close, but the app still writes its `viewer_left` audit after closing; leaving the `with` makes the TestClient cancel the app's agent mid-teardown, which surfaced as `CancelledError` from the portal | fixed: the test waits for the `viewer_left` row inside the session, as do the two other socket tests with the same shape |
| `unit tests (2/4)` | 1 | `test_forge_token_isolation.py::test_no_agent_process_is_alive_when_the_publish_token_is_used` | `_pid_alive` called only a `Z` process dead: a read while init reaped the killed daemon saw `X (dead)`, or failed with ESRCH and fell back to `kill(pid, 0)`, which succeeds on a zombie -- so a process the reaper had already seen dead read as alive a moment later | fixed: every state after death reads dead |
| `release workflow wiring (actionlint)` | 2 | the actionlint download | rhysd's download script curls the release tarball with no `-f` and no retry; GitHub's release download answered an error page (92 bytes; 1,812 bytes after 19 s) and `tar` read it as the archive (`gzip: stdin: not in gzip format`) | fixed: the tarball is fetched directly with `-f` and retries, checked against its pinned sha256, and cached |
| `build images` | 1 | `agent-runtime-base` | a Cloud Build docker step exited 22 (curl's HTTP-error status) | not fixed: a network fetch in the image build |
| `build changed images without pushing` | 1 | `agent-runtime-indexer` | `proxy.golang.org` reset the stream (`INTERNAL_ERROR`) mid-download | not fixed: a network fetch in the image build |
| `integration tests (emulator)` | 1 | the emulator install | `gcloud components install cloud-firestore-emulator` failed to download | not fixed: a network fetch in the job's setup |

The last three are downloads, not tests: the remedy for those is a pin, a
mirror or a retry in the job that fetches, not the quarantine list.

### The quarantine

A test that flakes and whose cause is not known yet is **quarantined**, not
skipped, while its issue is worked:

* **`tests/flaky.txt`** is the list, checked in. One test per line: its pytest
  node id from the repository root, its issue (`#N`), and the date it was
  added. A bare id quarantines every parameter of a test; an id with `[...]`
  only that case. The operator edits it; **CI never does**, so nothing leaves
  the gate without a person and an issue behind it.
* **`tests/unit/conftest.py`** marks every listed test `flaky` at collection
  (the marker is registered in `pyproject.toml`), through
  `tests/unit/flaky_quarantine.py`. A malformed list stops collection with its
  line number rather than quarantine the wrong thing.
* **The unit job** runs the gating suite with `-m "not flaky"`, then runs the
  shard's quarantined tests in `quarantined unit tests (report only, never fail
  the gate)`. That step's failure is a warning annotation and a line in the
  step summary; it never fails the job. A quarantined test therefore still
  runs on every pull request, so the day it starts failing every time --
  a real defect, not a flake -- shows up there.
* **`tests/unit/scripts/test_flaky_quarantine.py`** holds the parser, the
  marking, the job's two steps, and that every listed test exists: a renamed
  test would otherwise leave its entry quarantining nothing while the real
  flake gates again.

The list covers `tests/unit` only. The UI suite and the integration suite have
no quarantine; a flake there is fixed at its cause.

### Finding candidates

```bash
scripts/flaky-report.sh                 # the last 300 application.yml runs
scripts/flaky-report.sh --limit 1000    # further back
scripts/flaky-report.sh --no-logs       # jobs only, no log downloads
```

It reads every earlier attempt of a run that was rerun green (`gh run list`
shows only a run's latest attempt), and every failed run at a commit where
another run passed, and prints each failed job with the failing pytest or
vitest ids its log names. It is read-only. Then:

1. fix the cause if it can be found now -- that is the default;
2. otherwise file an issue, add the line to `tests/flaky.txt`, and link the
   issue to the line; the fix removes the line.

## Main reuses the tested tree

**Owner decision 2026-10-14 (the delivery speed plan, B1).** Measured over the
last 150 pull requests: `application.yml`'s pull-request run took a median
7.6 min, main's push run 12.8 min, and merge to deployed 26.7 min -- the
release waits for the push run's `build images` (6.5 min), which waited
behind about 5 min of unit shards that re-tested **the exact tree the pull
request had already tested**. With strict up-to-date merging, a squash merge
commits the tree of the pull request's merge commit, and that merge commit is
what the pull request's run checked out.

So every run but main's own -- a pull request, a merge-train branch, a queue
entry -- ends with `record the tested tree`, which uploads the artifact
`tested-tree-<tree hash>`: the tree, the commit, the event, the `changes`
job's python mode and every test job that concluded `success`
([`scripts/lib/ci_reuse.py`](../scripts/lib/ci_reuse.py) `record`). It is in
`ci-gate`'s `needs`, so the record exists before the pull request can merge.

On a push to main, the `changes` job's `reuse` step looks the commit's tree up
(`ci_reuse.py find`) and writes `reused`, the JSON list of jobs this run
skips; each test job's `if:` ends in
`!contains(fromJSON(needs.changes.outputs.reused || '[]'), '<job>')`. A job
skipped by its own `if:` reads as passed to `format / unit tests`, to `build
images` and to `ci-gate`, so `build images` starts as soon as what was not
reused has passed -- with nothing reused, exactly as before. The summary says
`reused from run N` and lists the jobs.

**Exact or nothing.** A job is reused only when all of these hold, and any
doubt -- no record, an API that cannot be read, a record that disagrees --
reuses nothing:

* a record of **this exact tree hash**, from a run of
  `.github/workflows/application.yml` in this repository (never a fork's),
  whose conclusion is `success`;
* the run is tied to this commit: a `pull_request` run **at the head of the
  pull request this commit merged**, as the API names it; or a `push` (a
  merge-train branch) or `merge_group` run whose own commit has this tree, as
  git computes it in the job;
* the job concluded `success` in the record **and** in the API's list of that
  run's jobs, every shard of a matrix job 1..N with none missing; a job the
  run skipped (a pull request that did not reach the UI) runs again;
* for the unit shards, the run ran **the whole suite** (`python=all`). A pull
  request's selected subset ([below](#a-pull-request-runs-the-unit-tests-its-change-reaches))
  is never main's full run. So main reuses the unit shards from a merge-train
  run, a queue entry, or a pull request whose selection fell back to the
  whole suite; it reuses the other jobs from any up-to-date pull request.

### A pull request reuses its lane push of the same tree

**Owner approval 2026-10-21 (CI-TREE-REUSE, #1798).** Since LANE CI-EARLY a
lane's `swarm/**` push runs this workflow and `terraform.yml`, and its pull
request then ran them again on the same tree. In the 24 h from 2026-10-20
06:00 UTC, 166 (workflow, head) pairs ran both ways: 51.5 VM-hours, 18% of all
VM hours on the self-hosted runners.

So the `reuse` step also runs on a `pull_request` from this repository
(`ci_reuse.py find --event pull_request`; never a fork's). It looks up the
tree of the commit the run checked out, `refs/pull/N/merge`, and reuses a
record only from `decide_pull_request`:

* a **`push` run of a `swarm/**` branch** of `application.yml` in this
  repository, `completed` with conclusion `success`. A cancelled, failed or
  unfinished run is never reused -- including the lane's push run that is
  still running when the pull request opens, which then simply runs again;
* whose **own commit has this tree**, as git computes it in the job;
* the same per-job rules as main's: `success` in the record and in the API,
  every shard, and the unit shards only from a run of the whole suite (a lane
  push always runs it).

**Why trees and not shas.** A pull request tests its merge commit, not its
head. While the lane is up to date with main, that merge commit's tree is the
lane head's tree, so the push run tested exactly what the pull request would
merge. Once main moves, the merge tree differs, nothing is reused, and the pull
request tests what it would actually merge -- which a sha comparison (the
head is unchanged) would have skipped.

**Never reused:** another pull request's run (only a lane push), a run of
another branch, workflow or repository, a job the push run skipped, and the
`build changed images without pushing` check, which a push run never runs.

**ci-gate passes on its own verdict.** After its first judgement (every job
passed or was skipped by its own `if:`), the step `a job skipped by reuse
passes only when the reuse step listed it` runs `ci_reuse.py gate`: a reusable
job this change reaches (its `if:` before the reuse clause, `REACH`) that was
skipped passes only when `reuse` listed it and named the run it passed in
(`reused_from`). The job summary then reads `ci-gate passes on N job(s) reused
from <run URL>`, so a reader sees where the green came from.

**The merge step reads it as green.** `agent_worker/merge.py`
`required_check_state` holds every run of a required check at the head:
the lane push's `ci-gate` and the pull request's are both `success`, and a
skipped heavy job is in `REQUIRED_GREEN`. `test_the_merge_step_reads_a_reused_green_as_green`
holds it; `merge.py` is unchanged.

**Main keeps its rules.** Main's lookup still runs `decide()` and takes the
newest record that reuses anything. A pull request that reused everything
recorded no job as passed (a skipped job is not `success`), so main passes
over that record to the lane push's, whose commit has main's tree.

**Not done for `terraform.yml`, and why.** Its run takes ~80 s (ci-gate's own
measure): fmt/validate/tflint, `terraform test` and checkov in parallel. Carrying the
record there would add a lookup job before them and a record job after them,
each a runner start of its own, for about the time they would save -- and
`terraform.yml` has no `changes` job or gate of its own to hang them on;
ci-gate.sh reads its run's jobs directly.

**Not done, and why: pull-request images tagged by tree.** The plan's better
form had a pull request's final-head run push its images tagged by tree hash,
and the release scan and promote those. A pull request has no Google identity
(owner decision 2026-10-05, [below](#why-there-is-no-pull-request-identity)), so
it cannot push anything, and giving it one is the change that section exists
to refuse. Main still builds -- only the images its commit changed
([above](#main-builds-only-the-images-a-commit-changed)) -- and the release
still promotes exactly what main built, including the per-release OS-package
refresh of PR 1279.

## A pull request runs the unit tests its change reaches

**Owner decision 2026-10-14 (the delivery speed plan, B2).** On a pull request
whose change reaches beyond `docs/` and `apps/swarm-ui/` (`python=all`), the
`changes` job's `select` step narrows the unit shards to `python=selected`:
the files [`scripts/lib/select_unit_tests.py`](../scripts/lib/select_unit_tests.py)
says the change reaches, which are

* the repository-wide guard set, **always, whatever changed**: the
  `GUARD_TESTS` array in `scripts/changed-guards.sh`, which the selector reads
  from that file rather than keeping a copy ([below](#a-miss-costs-a-red-main-so-the-whole-tree-tests-always-run));
* everything `scripts/changed-guards.sh --list` selects for the same diff --
  that guard set and every test whose text names a changed path, the same
  selection a SwarmCloud agent runs before it finishes;
* every test that imports a changed module through any chain of imports under
  `apps/`, `scripts/` and `tests/unit` (relative imports resolved; a changed
  `__init__.py` reaches every importer of its package);
* every test naming a script that sources a changed script, transitively
  (a change to `scripts/lib/common.sh` reaches the tests of every script that
  sources it), and a generic basename (`Dockerfile`, `__init__.py`) only
  beside one of its directories' names;
* every test naming a doc that cites a changed path (`doc_closure`): a
  `docs/**/*.md` that names a changed non-doc path by its **full**
  repo-relative path counts as changed, one level deep (#2299). PR #2267 moved
  `PushTestCell` out of `Access.tsx` while `docs/onboarding.md` still cited it
  there; the tests that resolve that citation name only `onboarding.md`, so no
  rule picked them and main's push went red. Full path only, because a
  basename (`App.tsx`, `main.py`) is shared by many files; never from a changed
  doc, because 55 docs cite `CLAUDE.md` and following them would make a
  docs-only change select hundreds of files;
* when the change also touches `docs/` or `apps/swarm-ui/`, the pick step's
  own area readers, as a docs-only change picks them.

**It falls back to the whole suite**, and says why in the job summary, when a
selection is not one it can vouch for: a workflow, composite action,
`conftest.py`, `pyproject.toml`, `uv.lock`, `tests/flaky.txt` or the
quarantine module changed (each changes how every test runs); the frozen
contract changed (every component imports it); more than 40 paths changed
(of the last 150 merged pull requests, measured 2026-10-14: median 7 files,
p95 23, one over 40); or the
selection is already half the suite, when the rest costs little and a whole
run is one main can reuse ([above](#main-reuses-the-tested-tree)).

**A miss costs a red main, never an untested release.** Main's push run, a
merge-train run and a queue entry pass the diff's `python=all` straight
through and run every file, and main reuses unit shards only from a run of the
whole suite. `lock / contract / format / bridge install` still runs on a
selected pull request.

### A miss costs a red main, so the whole-tree tests always run

**CI-GUARDS-ALWAYS (part of #1822, 2026-10-21).** Main's own CI was red on 78
of 231 merged commits (34%) in the 72 h to 2026-10-21, and 14 of 32 failed
releases were "CI never built" a red main's commit. The two longest
test-caused streaks were tests no changed path names, because they read the
whole tree: `test_collection_stays_cheap` imports every module in
`tests/unit/scripts` (19 commits red, 2026-10-20 03:11-06:54Z, until PR 1705),
and `test_awaiting_input_readers` walks `apps/` and `scripts/` for every
reader of `HUMAN_REQUIRED` (6 commits, 2026-10-19 06:47-07:30Z). A diff that
adds a module or a reader breaks them without naming them, so selection by
path never ran them.

So every pull request whose unit shards are narrowed (`python=selected`)
runs `GUARD_TESTS` from
[`scripts/changed-guards.sh`](../scripts/changed-guards.sh) whatever paths it
changed. A change confined to `docs/` and `apps/swarm-ui/` (`python=subset`)
is never narrowed by the selector, so it still runs only its area readers,
and a lane's docs-only `changed-guards.sh` run still narrows to
`DOC_GUARD_TESTS`. The list is kept **once**, in that script:
`select_unit_tests.py`'s `guard_tests()` reads the array from it, a list it
cannot read or a guard that no longer exists runs the whole suite, and
`tests/unit/scripts/test_select_unit_tests.py` holds the two equal.

**What is in it is read from the array, not from here.** The set is #642's
docs guards, the UI route seam and `test_specsign_covers`, then the
whole-tree tests: files that read every file of a directory -- `apps/`,
`tests/`, `docs/`, `scripts/`, `terraform/`, `kubernetes/`, `.github/` or
`images/` -- and assert over each, so a new file there breaks them without
naming them. `GUARD_TESTS` lists them one path a line, with a comment naming
the lane that added each group and why. This page named them test by test
until CI-GUARDS-MORE added twenty in one go and the list here fell behind
(CI-GUARDS-DOCS, part of #1822); it names none now, so it cannot drift.

**The bound.** Every pull request and every lane pays for the set, so a file
is in it only if its recorded weight in `unit_test_durations.json` is at most
10 CI-seconds; the walkers CI-GUARDS-MORE and CI-GUARDS-DOCS added are each under 5 (a
test in `test_select_unit_tests.py` holds that per group, and a file with no
weight recorded is measured once and left out if it is over 5 s). A heavier
whole-tree file is left to main's push run, which runs every file:
`test_state_colour_discriminability` (34.7 s), `test_docs_cite_worker_symbols`
(26.0 s), `test_unit_shards` (25.5 s), `test_ci_gate` (28.2 s),
`test_merge_queue_workflows` (26.4 s), the `test_build_images_*` files, and
`test_shellcheck_ci` (no weight recorded; it takes minutes). Plugin and
single-package `apps/` walkers are not in it: whether to add them is not
decided. Add a whole-tree test to `GUARD_TESTS` when you write one.

## A pull request scans the images it builds

**Owner decision 2026-10-14 (the delivery speed plan, C3).** Two release
blockers were first seen after a merge: PR 1276 merged without its tool-mirror
object uploaded, so main's Cloud Build failed at its `tool-mirror` step; and a
Debian curl CVE stopped every release at the promotion scan for hours.

**The scan.** `build changed images without pushing` installs trivy at
`release.yml`'s pin (a test holds the two equal) and passes
`BUILD_ONLY_SCAN=scripts/scan-image-layout.sh` to `build-images.sh
--build-only --local`. Each image the replay builds is exported to an OCI
layout and scanned as soon as it is built, with the release's own policy --
fixable HIGH and CRITICAL findings, `.trivyignore.yaml` honoured -- read from
[`scripts/lib/image-scan.sh`](../scripts/lib/image-scan.sh), which
`push-images.sh --scan` reads too, so the two scans cannot drift. A finding
fails the build and names the image; a leaf's layout is removed after its
scan.

**The tool mirror.** `refresh-build-mirror.sh --check` reads the bucket, which
a pull request cannot. The job instead runs the offline check that holds
`images/agent-runtime-base/tool-mirror.sha256` equal to the Dockerfiles' pins,
and names every row the pull request **adds** -- a warning annotation and a
line in its summary -- as an object the operator uploads with
`scripts/refresh-build-mirror.sh --tools-only` before the merge
([runbook](runbooks/build-mirror.md)). It is a warning, not a failure: the
job cannot see whether the upload happened.

**Main fills the bucket itself (owner decision 2026-10-14, on PR 1298).**
Main's `build images` job, which holds the deployer's identity, runs
`scripts/refresh-build-mirror.sh --tools-only` before it builds. That uploads
every manifest object the bucket lacks, each downloaded from its upstream URL
and checked against its pinned sha256 first. A mismatch is refused and fails
the step, naming the object. An object already there is left alone, nothing
is ever deleted, and no image is pulled through (`--tools-only`). The step
never runs on a pull request or a queue entry, where the job itself is
skipped. So a merged manifest row no longer fails main's build; the PR
warning above says the object will be uploaded from upstream at the merge.

**The scan blocks (owner decision 2026-10-14).** A fixable HIGH or CRITICAL
finding fails the pull request's image job, the same rule as the release's
promotion scan, even when the CVE is new upstream and unrelated to the change.

**Download retries.** The three fetches PR 1281 measured failing runs now
retry, bounded: every `curl -fsSL` in a Dockerfile carries `--retry 5
--retry-all-errors --retry-connrefused` (the `build images` exit 22 was an
HTTP error, which curl retries only with `--retry-all-errors`); the indexer's
`build_server` -- `go mod download` and `go get` against proxy.golang.org --
runs up to three times, 15 and 30 s apart; and the Firestore emulator install
in `integration tests (emulator)` tries four times, 10, 20 and 40 s apart.

## Merging through the merge queue

**Owner decision, 2026-10-08 (observer proposal G).** Since 2026-10-07, main
has been held by `strict_required_status_checks_policy: true` (below): a pull
request merges only when it is up to date with `main` and green at that head.
That holds main green, but it serialises merging. Each `ready` pull request
waits its turn for an "Update branch" (the merge step's MS3, or the operator's
merge watcher), then for a full CI run at the new head, and the next one
starts only after it lands. **On 2026-10-07 pull requests waited p50 75 min
and p90 8.5 h from open to merge**, mostly in that queue of update-branch and
re-run CI rather than in review or in their own first CI run. A merge queue
keeps the guarantee and drops the serial wait. GitHub tests up to five queued
pull requests together on one temporary branch, merges every one that is green
in that group, and needs no update-branch at all.

What changed for this, and what did not:

* **Every workflow producing a required check runs on `merge_group`** with
  the same jobs (`application.yml` for `ci-gate`, `security.yml` for the other
  four, and `terraform.yml`, which `ci-gate` waits for). The jobs that must
  not run there (`build images`, `plan`) are excluded with an `if:` that says
  why. [What is ready for it](#what-is-ready-for-it) has the detail.
* **`auto-merge.yml` enqueues instead of merging** once the ruleset carries
  the `merge_queue` rule: `gh pr merge --auto` with no method arms auto-merge,
  and on a queued base that is what puts the pull request in the queue. Every
  refusal it has today still applies before that: a `[swarm] task_` title, a
  closing keyword naming an open pull request, a failing check, a missing
  merge App, and since 2026-10-08
  [an unlanded dependency](#saying-what-a-pull-request-depends-on).
* **No update-branch runs while the queue is on.** `auto-merge.yml` never
  updated a branch: it arms auto-merge, and in strict mode GitHub left an
  out-of-date pull request waiting for someone else to update it. The two
  things that do update a branch go quiet by themselves. The worker's merge
  step updates only when GitHub reports the pull request `behind` or refuses
  a merge as out of date, which is what the strict policy causes. The rule
  below turns strict off, and a merge that GitHub answers with "must be made
  through the merge queue" goes to `enqueuePullRequest`, never to an update.
  The operator's merge watcher has nothing to update once nothing requires an
  up-to-date branch. (Not yet observed: whether GitHub still reports `behind`
  with strict off. If it does, the step makes one needless update before the
  merge's 405 enqueues it. That costs a CI run but never merges anything
  untested.) With the queue off, both behave exactly as they do today.
  **Turning the queue on is the ruleset change below and nothing else.**

> **Not available on this repository yet (read 2026-10-07, and again 2026-10-08).** GitHub offers
> merge queues only to repositories owned by an organization; `bogdan-alexandrescu/SwarmCloud` is
> owned by a user account (`gh api repos/bogdan-alexandrescu/SwarmCloud --jq .owner.type` prints
> `User`, read again 2026-10-08). On 2026-10-07 a `merge_queue` rule was refused with
> `Invalid rule 'merge_queue'` (HTTP 422), and the ruleset was left unchanged. Expect the operator
> step below to be refused the same way until the repository is owned by an organization. That is
> the owner's decision, not something a pull request can change.
>
> **What protects main meanwhile** (owner decision 2026-10-07): ruleset 24160219's
> `required_status_checks` sets `strict_required_status_checks_policy: true` (read 2026-10-08), so a
> pull request must be up to date with main, and green at that head, before it merges. Two pull
> requests that are green on their own bases but red together (#726 and #727, 2026-10-06: main red
> 19:12-19:58Z) can no longer both merge: the second is behind once the first lands, and must update
> and pass CI again. The merge step already updates a behind branch (MS3) and re-reads its checks at
> the new head (MS2's wake). The `merge_group` triggers and both enqueue paths are inert while no
> queue exists: GitHub raises no `merge_group` event, and nothing enqueues unless the base requires
> a queue.

**Owner decision, 2026-10-06 (observer P26).** `main` was red from 19:12 to
19:58Z because #726 and #727 were each green on their own base and merged
nine seconds apart. Neither had run against the other: `main-protection`
(`24160219`) has `strict_required_status_checks_policy: false`, so a pull
request is not required to be up to date with `main` before it merges, and
two changes that each pass alone can fail together. Turning `strict` on would
serialise every merge behind a manual "update branch" and a full CI run each.
The owner chose a **merge queue** instead: GitHub builds a temporary branch
(`gh-readonly-queue/main/pr-<n>-<sha>`) holding `main` plus every queued pull
request ahead of it, runs the required checks there, and merges only what is
green together.

The ruleset change is the operator's, **after** the pull request carrying this
section has merged. Until then nothing here changes how anything merges.

### What is ready for it

* **Every required check reports on a queue entry.** A queue waits for the
  ruleset's required contexts on the entry's ref, through the `merge_group`
  event. `security.yml` (four of them) and `application.yml` (`ci-gate`, its
  last job since 2026-10-07) trigger on `merge_group: types:
  [checks_requested]`, with no filter, and so does `terraform.yml`, which
  `ci-gate` waits for there: `merge_group` does not take a path filter, and a
  filtered-out run would be one `ci-gate` waits ten minutes for and then fails
  the entry on.
  `scripts/ci-gate.sh` accepts `--event merge_group`; on it every gated
  workflow is expected, whatever changed. The entry's commit is `github.sha`
  and its base `merge_group.base_sha`.
* **Nothing that mints a cloud identity runs there.** `build images` and
  `plan` are gated off `merge_group` as they are off `pull_request`: the
  workload identity pool admits `refs/heads/main` only, and an entry's ref is
  not main. Left to run, they would fail auth, fail their run, and fail
  `ci-gate` on every entry. `plan (not run on a pull request)` says so on the
  entry, as on a pull request. `build changed images without pushing` and the
  frozen-contract check read pull-request fields and stay pull-request only
  (skipped is a pass inside a successful run).
* **`release.yml` and `auto-merge.yml` never run on `merge_group`.** A
  release is for a commit on `main`, which an entry is not yet; the queue's
  merge is a push to `main` like any other, and that push starts
  `application.yml` and `release.yml` as today (GitHub documents that a
  queue's merge raises `push`; not yet observed on this repository).
* **`auto-merge.yml` enqueues.** Its gate reads whether `main`'s effective
  rules carry a `merge_queue` rule (the same `rules/branches/main` read as its
  required-checks item) and says `queue`. On a queued base the merge step runs
  `gh pr merge --auto --match-head-commit` with no method and no subject --
  the queue's `merge_method` decides how it lands -- and its already-green
  fallback is GraphQL `enqueuePullRequest`, pinned to the head, never a direct
  merge, which would be the queue bypassed. A pull request already in the
  queue is left there. Without the rule the step is exactly what it was.
* **The worker's merge step enqueues.** In any repository whose base
  requires a queue, GitHub answers the REST merge `405` "Changes must be made
  through the merge queue". The step then enqueues through
  `enqueuePullRequest` with the tenant's `-git` token, behind the same fencing
  recheck, records the entry in `result_summary.merge.merge_queued`, and parks
  `CI_PENDING` with code `merge_queued`, holding no lease and no pool count
  (invariants 1, 3 and 4). The wake tick reads the queue for that park, not
  the checks. The next attempt finds the pull request merged (success, the
  record comment and the issues closed), still queued (parked again), or
  taken out unmerged (`merge_dequeued`, with GitHub's reason; it is never
  enqueued again).

`tests/unit/scripts/test_merge_queue_workflows.py` holds the workflow side to
the ruleset's five required contexts, hard-coded there with the ruleset id;
`test_auto_merge_workflow.py`, `tests/unit/worker/test_merge_action.py` and
`tests/unit/control_plane/test_merge_wake.py` hold the rest.

**What changes on `main` once it is on.** `merge_method` `MERGE` puts a merge
commit on `main` for each pull request, titled by GitHub ("Merge pull request
#N from ...") with the pull request's title in its body, where the squash put
the title as the headline. A `[swarm] task_` title is still refused before it
gets there. Merge commits must be allowed on the repository
(`gh api repos/bogdan-alexandrescu/SwarmCloud --jq .allow_merge_commit` is
`true`), and the ruleset's `allowed_merge_methods` already lists `merge`.

### Operator step: add the merge queue rule

Apply it only once the pull request carrying this section has merged and a
pull request has shown `ci-gate` and the four security checks green after it.
The PUT replaces the ruleset whole, so the body restates every rule the
[ci-gate step](#owner-step-require-ci-gate-once-it-is-on-main) sets and adds
only `merge_queue`. Compare it with a fresh read first
(`gh api repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219`): a rule
added since that this body leaves out would be deleted by the PUT.

```bash
gh api -X PUT repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219 \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
  },
  "bypass_actors": [
    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
  ],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "required_reviewers": [],
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "require_extra_approval_for_unattributed_changes": true,
        "allowed_merge_methods": ["merge", "squash", "rebase"]
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          {"context": "secret scan"},
          {"context": "trivy (repo)"},
          {"context": "checkov (terraform + kubernetes)"},
          {"context": "platform policy assertions"},
          {"context": "ci-gate"}
        ]
      }
    },
    {
      "type": "merge_queue",
      "parameters": {
        "merge_method": "MERGE",
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 5,
        "max_entries_to_merge": 5,
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0,
        "check_response_timeout_minutes": 60
      }
    }
  ]
}
JSON
```

Why these values: `ALLGREEN` merges an entry only when its checks and those
of every entry ahead of it pass, which is the guarantee the incident lacked.
In the settings page's words, that is **Require merge queue**, merge method
**Merge commit**, **Only merge non-failing pull requests** (`ALLGREEN`),
build concurrency 5, group size 1-5, no wait, and a 60-minute **status check
timeout**. `max_entries_to_merge: 5` is the group size the owner chose: up to
five pull requests land on one green run instead of one CI cycle each, which
is where the 2026-10-07 wait went. The API requires it; the 2026-10-07 body
lacked it. `max_entries_to_build: 5` bounds the groups built at once (each is
a full `application.yml` run, so a cost in runner time). `min_entries_to_merge:
1` with a `0`-minute wait merges a lone entry at once instead of holding it
for company. `check_response_timeout_minutes: 60` is under `ci-gate`'s own
90-minute wait, so a hung check drops the entry rather than stalling the queue
behind it. `strict` goes from `true` (2026-10-07) back to `false`: with
strict on, every pull request would still need an update-branch and its own
CI run before it could even enter the queue. That is the wait this removes,
and the queue's group run is what tests the combination now.

Then check it took: `gh api repos/bogdan-alexandrescu/SwarmCloud/rules/branches/main --jq '.[] | select(.type == "merge_queue") | .parameters'`.
The first queued pull request shows a `merge_group` run of `security`,
`ci-gate`, `application` and `terraform`; if any of them is missing, roll back
(below) before more pull requests pile into the queue.

### Rolling back

Roll back by sending the same PUT **without the `merge_queue` rule and with
`strict_required_status_checks_policy` back to `true`**. That is the ruleset
as it was on 2026-10-07, and the protection main had before the queue. (The
body in the [ci-gate step](#owner-step-require-ci-gate-once-it-is-on-main)
predates that and says `false`. Sending it as it stands would leave main with
neither the queue nor strict.) Everything this
section added stays harmless with the queue off: GitHub never raises
`merge_group`, `auto-merge.yml`'s gate says `queue=false` and squashes as
before, and the merge step never sees the merge-queue `405`. A pull request
still in the queue at the rollback is taken out by GitHub; label it `ready`
again.

## The release warms each Cloud Run worker job once on its new digest

On dev, `accept.yml`'s `warm, smoke and GKE proof (dev)` runs
`scripts/warm-jobs.sh` first, before its smoke test and before any
acceptance group ([the release timeline](#the-release-timeline)). It runs
against the digests that the release that just completed verified. On prod,
the release's `deploy` still runs it, right after it has verified that every
job runs the promoted digest and before the smoke test. The script executes
each per-tenant worker job once with `--args=--self-test`.

**Why.** The measurement of 2026-10-07 on #363 (874 claude-code attempts from
09-30 to 10-07, 497 of them with Cloud Run execution conditions): the first
execution of each Cloud Run job after a new image digest spends **30-59 s
importing the image** (ContainerReady "Imported container image in Xs"), and
every later start pays 1-3 s. That is about 3 % of starts. Without a warm run,
each job's first tenant agent after a release pays the import on top of Cloud
Run's own 70-200 s provisioning. The owner decided the same day that one
post-deploy run per job absorbs it. This is a different thing from the warm
capacity the owner declined on 2026-09-30 (invariant 1): nothing is left
running. Each execution exits as soon as Cloud Run starts it.

**What one execution does.** `--args` replaces the job's arguments, and the
image's ENTRYPOINT stays `python -m agent_worker`. On `--self-test` the worker
prints one line and exits 0 before it reads its configuration
(`agent_worker.__main__.self_test`). It takes no lease, builds no Firestore
client and touches no agent state. The execution carries no `TASK_ID` or
`ATTEMPT_ID`, so the reconciler leaves it alone. It logs it once
(`CloudRunBackend.list_executions`) and has no authority over it.

**Which jobs.** The script warms only jobs that are terraform-managed
(`managed-by=swarm-terraform`), carry a `swarm-tenant` label, and are named
`swarm-job-*`. That leaves out `swarm-verify` and any job something else
created. A name on the shared deny-list (`scripts/lib/common.sh`) is never
executed. If it was listed, the script warns and skips it. If it was named on
the command line, the script refuses it and exits non-zero after warming the
rest. `WARM_PARALLELISM` executions run at once (default 4), because each one
occupies a whole worker shape in the region while it starts.

**It never fails the release.** If a warm execution fails, or the listing
fails, the script prints a warning and exits 0. The cost is one slower first
agent. The step also sets `continue-on-error`. In the prod release it runs
only when the verify step succeeded, because warming the previous digest would
warm nothing, and only under `!cancelled()`, because it executes jobs in the
environment. That puts it under the prod gate's checks
(`test_release_prod_gate.py` counts it as a deploy). In `accept.yml` it runs
only after a release completed successfully, and so after that release's
verify.
`tests/unit/scripts/test_warm_jobs.py` and
`tests/unit/worker/test_worker_self_test.py` hold all of this.

**Nothing is warmed when the runner digests did not change** (observer
proposal H, 2026-10-08). Since main reuses the digests of unchanged images
(above), most releases ship the three runner images unchanged. A warm run on a
digest every job has already started pays no import. It only occupies a worker
shape. With `--previous FILE` (or `WARM_PREVIOUS_MANIFEST`), the script
compares the three runner digests in this release's deployed manifest with
those of the release before. If all three are equal, it warms nothing and says
so. `release.yml` does not pass that file yet: its deploy job has no `actions:
read` to fetch the earlier artifact, and another lane is restructuring that
workflow. Until it does, the script skips, by name, each listed job that is
pinned by digest and already has a successful execution on that same image.
That is the import already paid, by the last warm run or by a tenant agent. A
job new in this release (a new tenant or profile) has no such execution and is
warmed even when no digest changed, which comparing manifests alone would
miss.

**That skip's premise was measured on 2026-10-27 (box 94 of #888), and the
skip is kept as it is.** After a same-digest idle gap of 1 to 13.7 h, every
timed import on a worker job read 1.0-2.5 s, never the 30-59 s of a fresh
digest; a gap of a day or more has never occurred on a runner image, so no age
bound was added. The numbers, the method and when to read them again are in
[ci-warm-jobs-measurement.md](ci-warm-jobs-measurement.md).

## Release acceptance runs in the smoke tenant, against a private sandbox

`accept.yml` runs `scripts/acceptance/` after every dev deploy
([acceptance.md](acceptance.md) says what each check asserts; [the release
timeline](#the-release-timeline) says why it is not a release job any more). Until
2026-10-05 its agents were submitted as swarm-verify, which resolves to `eng`,
and cloned and opened pull requests on this platform's own public repository.
The history analysis of that day (#628) counted 58 fixture pull requests
("Fix add()...") opened and closed there since 09-30, each one running full CI,
and acceptance as 62% of eng's agents, so eng's success rates and failure
classes could not be read; the `smoke` tenant had none. The owner decided the
same day that acceptance runs in `smoke` against a private sandbox repository,
and that nothing in acceptance can name the public one.

**Where the target is stated.** Once, in
[`scripts/acceptance/config.sh`](../scripts/acceptance/config.sh): the tenant
(`smoke`), the repository (`bogdan-alexandrescu/swarmcloud-sandbox`, private,
default branch `main`), the ref, and the sandbox's fixture issue. The suite,
the sweep and the sync all read it; none of them reads
`GITHUB_REPOSITORY` for a target, and `config.sh` refuses a target that is the
repository a CI run is for or the one the checkout was cloned from.
[`test_acceptance_target.py`](../tests/unit/scripts/test_acceptance_target.py)
holds the configuration to smoke and the sandbox, and fails if any file under
`scripts/acceptance/`, or `accept.yml`, names the public repository.

**How the suite gets into `smoke`.** It sends `X-Swarm-Tenant: smoke` on every
API call (`SWARM_API_TENANT`, which `common.sh`'s `api_request` turns into the
header). The header *selects* among the caller's confirmed memberships of
registered directory groups and never grants, so swarm-verify must be a member
of smoke's group. Its default tenant stays `eng`: `smoke` sorts after `eng` in
`TENANT_GROUPS`, so smoke-test, e2e and race-test are unchanged. A
`tenants.smoke.service_accounts` listing would have been simpler to write and
would have stopped every suite: a listed account is continuation-scoped
(`swarm_api.auth.CONTINUATION_ROUTES`) and may submit nothing but a
continuation.

**One exception: the two await-input checks run in swarm-verify's own service
tenant** (owner decision 2026-10-19, HW-ACCEPT-ID). A service account answers
for no person in a group tenant, so their answer would be 403
`person_required` in `smoke`; as its own service tenant's principal it is
taken (docs/design/human-steps.md §4.2.2). `groups/workflow.sh` derives that
id at run time (`u-sw-c90291` for swarm-verify) from the email
`GET /tenants/me` verified, with `common.sh`'s `personal_tenant_id`, and sends
it as `X-Swarm-Tenant` for those two workflows only; cleanup cancels their
agents in that tenant.

**What the suite refuses.** Before any group runs, `run.sh` asks GitHub
anonymously for the repository and refuses a 200 (public) or an unanswered
read, and asks `GET /tenants/me` with the header and refuses any tenant but
`smoke`, including a 403 `tenant_not_member`. It never falls back to the
caller's default tenant: that fallback is how acceptance came to fill eng.

**What `accept.yml` holds.** `contents: read` everywhere. `id-token: write`
only on the jobs that start executions, as its own identity
([above](#its-identity-is-not-the-deployer)). `issues: write` only on the
report. The secret `SWARM_SANDBOX_GITHUB_TOKEN` (environment `repo-secrets`) goes to three steps
and never to the suite. The first is `scripts/acceptance/sandbox-sync.sh`, in
a job of its own before any group starts. It writes the deployed commit's
`tests/acceptance/fixtures/` to the sandbox's `main` (only when they differ,
and no other path) and opens or checks the fixture issue;
`scripts/acceptance/github-verify.sh` after it, which reads the suite's pull
requests back (below); and `scripts/acceptance/github-cleanup.sh` last, which
closes the pull requests and branches the suite opened there. The job's
`GITHUB_TOKEN` reaches only this repository, which is why it is no longer
write-scoped.

**Where the GitHub read-backs moved.** The swarm-verify job holds no GitHub
credential, by design, and the sandbox is private, so the suite cannot read a
pull request back and SKIPs those assertions, naming the step that makes them.
Dropping them would have let a release go green with the pull-request half of
the direct-pr and integrate checks unexercised, so they run on the GitHub
runner instead, which does hold the sandbox token:

* `github-verify.sh` runs after the suite (even a failed one) and before the
  sweep closes anything. It judges every open `swarm/task_` pull request of the
  sandbox opened since acceptance started (`SWARM_ACCEPTANCE_SINCE`, from the
  sandbox job's `started` step), and fails the job when one has an empty title or a title
  carrying a task id, a body without its task id, a change outside
  `tests/acceptance/fixtures/`, a diff that does not remove `return a - b` from
  `calc.py`, a direct-pr change to anything but `calc.py`, or an integrate body
  that lists no merged branch or leaves one out. It only reads; the sweep still
  runs when it fails. Finding no pull request at all is printed with a workflow
  warning, not a failure: a claude-code group without a provider credential
  SKIPs without opening one.
* The `collect` check's patch is applied in the suite itself, to this build's
  own `calc.py`: the swarm-verify image now carries
  `tests/acceptance/fixtures/`, and `sandbox-sync.sh` put the same commit's
  copy on the sandbox's `main` first, so the two are the same bytes.

What still runs only in the suite: the pull request was opened, its title is
the agent's (`pull_request_text.title`), the artifacts exist and the patch
replaces the bug line. The one comparison the runner cannot make is the PR
title against the agent's `pr-title.txt` artifact, which lives behind the API
inside the VPC; it checks the title is a non-empty fact rather than a task id
instead. An operator run with `SWARM_ACCEPTANCE_GITHUB_TOKEN` set measures that
in the suite too.

### What the owner applies, once, in this order

1. **Create the Workspace group `smoke@saga.xyz`** and add
   `swarm-verify@saga-agents-staging.iam.gserviceaccount.com` as a member.
   It must exist before step 3 is applied: `dev.tfvars` registers it as a
   directory group, and a registered group whose lookup fails 503s every
   caller in no group above it.
2. **Re-point the tenant document.** `tenants/smoke` in Firestore still says
   `principal: swarm-smoke@saga.xyz`, and Terraform will not change it (the
   tenant documents are under `ignore_changes`). swarm-api refuses a tenant
   whose stored principal differs from the selecting group with a 409, so set
   that one field to `smoke@saga.xyz`, for example:

   ```bash
   # The access token reaches curl as a config file, never in argv.
   curl -sS -X PATCH -K <(printf 'oauth2-bearer = "%s"\n' "$(gcloud auth print-access-token)") \
     -H "x-goog-user-project: saga-agents-staging" -H "Content-Type: application/json" \
     "https://firestore.googleapis.com/v1/projects/saga-agents-staging/databases/swarm/documents/tenants/smoke?updateMask.fieldPaths=principal" \
     -d '{"fields": {"principal": {"stringValue": "smoke@saga.xyz"}}}'
   ```

   `smoke` has never run an agent, so no work changes hands.
3. **Merge the `dev.tfvars` change** (the release applies it): `tenants.smoke`
   gets `principal = "smoke@saga.xyz"`, `directory_group = true` and
   `providers = ["anthropic"]`, and `pool_limits.providers.anthropic` goes from
   100 to 120, because three tenants now declare anthropic and the plan refuses
   a provider pool below 3 x `provider_tenant`. The live pool documents are
   under `ignore_changes`, so raise the live one too:
   `scripts/pool-limit.sh --pool provider:anthropic --limit 120`.
4. **Give `smoke` its credentials**, with the scripts that keep them out of the
   repository and out of Terraform state:
   * the forge token, slot **`swarm-tenant-smoke-git`**:
     `scripts/register-tenant.sh --tenant smoke --add-provider git`, then
     `scripts/create-secrets.sh --tenant smoke --provider git --stdin`. A
     fine-grained token scoped to the sandbox alone, with Contents, Pull
     requests and Issues read and write (the worker clones, pushes and opens
     the pull request with it; the `issue` input reads the fixture issue);
   * the Anthropic credential, slot `swarm-tenant-smoke-anthropic`:
     `scripts/register-tenant.sh --tenant smoke --add-provider anthropic`,
     then `scripts/create-secrets.sh --tenant smoke --provider anthropic
     --stdin` (or `--subscription`), or lend `smoke` an account
     (`PUT /v1/accounts/<id>/lending`). Without one, every claude-code check
     SKIPs on `CREDENTIAL_MISSING`.
5. **Add the secret `SWARM_SANDBOX_GITHUB_TOKEN` to the environment
   `repo-secrets`** (Settings -> Environments -> repo-secrets; not a
   repository secret, [why](#what-the-owner-applies-once)): a fine-grained token scoped to the
   sandbox alone, with Contents, Pull requests and Issues read and write. It
   may be the same token as step 4's. Without it the sync step fails and the
   suite does not start.

Until steps 1-3 are done the acceptance job fails at `acc_require_tenant`
with the sentence that names this section, and nothing is submitted: a red
acceptance job, not a quiet return to eng.

## The personal-workspace job is not CI, and its log is not public

A person's workspace is made by **the Cloud Run job `swarm-workspace-apply`**
in the project, not by a workflow in `.github/workflows/` and not by Cloud
Build ([multi-tenancy §7](multi-tenancy.md#7-a-persons-personal-workspace)).
It is recorded here because it is the one deployment-shaped thing that no
workflow runs, and the first place someone looks for its log is this page.

**Why not CI.** Its log names people, and this repository's Actions logs are
public. Its identity, `swarm-workspace-deployer`, holds account-IAM power IAM
cannot narrow in the shared project, so it must be reachable from as few
places as possible: a GitHub workflow holding a way to start it would put
that power one compromised workflow away (WD2, re-decided 2026-10-10 from a
Cloud Build trigger to this job).

**How it is started.** An admin's approval (or Retry, or the 10-minute
`swarm-workspace-sweep`) makes swarm-api publish the opaque workspace id to the
Pub/Sub topic `swarm-workspace-apply`. An Eventarc trigger hands the message
to the Workflows definition `swarm-workspace-apply`, running as
`swarm-workspace-dispatch`, which validates the id and the mode and runs the
job with those two arguments and nothing else. All of it is
`terraform/bootstrap/workspace_deployer.tf` and `workspace_dispatch.tf`, the
owner's apply, never the release's.

**What CI does for it.** The release builds and scans `images/workspace-apply`
with every other image (`scripts/build-images.sh` `ALL_TARGETS`); the `shell`
job runs the call guard's self-test (`workspace-guard.sh self-test`), and the
`python` job the walk of every call the job makes
(`tests/unit/scripts/test_workspace_deployer_walk.py`). **The job does not run
what the release just built.** It runs the digest in bootstrap's
`workspace_apply_image`, and only the owner's bootstrap apply moves it
(workspaces.md §2.4 R1), so a merge to `main` never changes what runs as the
deployer. A fix to the job's code therefore reaches it in two steps: the
release builds the image, then the owner re-pins the digest, as PR 1219 did
for PR 1217's image on 2026-10-13.

**Where its logs are.** Cloud Logging, `resource.type="cloud_run_job"` and
`resource.labels.job_name="swarm-workspace-apply"`, routed by the sink
`swarm-workspace-apply` to the restricted log bucket of the same name and
excluded from `_Default` (workspaces.md §2.6). Readable by the project's owners
and logging admins and the members of bootstrap's `workspace_log_readers`; the
person's row in Admin → People links to the execution. Each step's progress
is also on the record (`steps.A1` … `steps.A9`), which People shows without
any log access. The first execution to reach A9 ran 2026-10-13 05:03 to
05:05Z, for `w-7f9285`.

## ruff lint is the house Python linter, and formatting is not adopted

ruff is the house linter (owner decision 2026-10-14, "Adopt ruff"), configured
once in the root `pyproject.toml` under `[tool.ruff]` and pinned exactly in
its dev group, so a lane, the pre-publish gate and CI lint with the same
binary: `uv run ruff check .` for the repository, or `uv run ruff check
<files>` for a change. The rule set is deliberately small -- pyflakes and the
pycodestyle errors that are always bugs (`E4`, `E7`, `E9`, `F`), selected
explicitly because ruff's own default set has grown past that -- so it reports
defects (an unused import, an undefined name, an f-string with no
placeholder), never taste; widening it is a decision of its own, with its
churn measured first. `apps/common/swarm_common/` is frozen, so its unused
imports are ignored there rather than fixed. **`ruff format` is NOT adopted**:
a repository-wide reformat would conflict with every open lane, so
`[tool.ruff.format]` excludes every file and `force-exclude` keeps that true
when files are named explicitly, which is how the pre-publish gate
(`apps/agent-worker/agent_worker/prepublish.py`) runs `ruff format --check`
on a lane's changed Python -- without it every lane touching Python would be
told to reformat what it touched. `tests/unit/scripts/test_ruff_config.py`
holds the pin, the lock, the rule set and the format exclusion. The CI job
that runs it is `ruff check` in `application.yml` (SPEED-B, 2026-10-14):
`uv run --frozen --only-dev ruff check --output-format github .`, which
installs the dev group alone -- the pinned ruff with it -- so it takes
seconds, and annotates each finding on its line. It is in `ci-gate`'s
`needs`, so it is required the way every job there is.

## The finishing sequence

```bash
git commit && git push          # then:
gh pr create                    # or push to an existing PR branch
gh run list --branch <branch>   # read the run
gh run view <id> --log-failed   # read the failure
```

Report the run's conclusion. Never report a local exit code, because there
should not be one.

**Proving a test catches its defect is still required.** Prove it by committing
an assertion strong enough that the mutation would fail it, and let CI
demonstrate that — not by running the mutation here.

### A SwarmCloud agent runs `scripts/changed-guards.sh` before it finishes

The laptop rule above does not bind an agent in a SwarmCloud container
(CLAUDE.md, "A SwarmCloud agent is not this machine"): it runs the unit tests
for its area, then this one command (as `--shard` parts when its selection
is heavy, below):

```bash
scripts/changed-guards.sh            # diff = origin/main...HEAD + work tree
scripts/changed-guards.sh --list     # print the selection, run nothing
scripts/changed-guards.sh --base <rev> | --files <list>|-
scripts/changed-guards.sh --shard k/n  # run the k-th of n parts of the selection
```

**Why.** On 2026-10-05, 33 of 131 lane PRs (25%) were red on their first CI
run, and the reds were mostly repo-wide guards that a lane's narrowed
`pytest tests/unit/<area>` never collects: `test_docs_describe_what_was_built`,
`test_docs_spec_amendments`, the route seam
`test_every_v1_path_the_ui_calls_is_served_by_this_api`, `test_specsign_covers`
and the contract-parity script (#591, #611, #614; issue #642). Those tests read
source and docs **as text**, so an import graph does not find them; their text
naming the changed file does.

**What it selects.** For a non-empty diff: the fixed guard set (`GUARD_TESTS`,
which pull-request CI also runs on every pull request,
[above](#a-miss-costs-a-red-main-so-the-whole-tree-tests-always-run); always,
but only its doc guards for a docs-only diff, below), every
changed `tests/unit/**/test_*.py`, and every `tests/unit/**/test_*.py` whose
text names a changed path by repo-relative path or basename. A generic basename
(`__init__.py`, `package.json`, …) selects by full path only, or it would pick
half the suite. A `docs/**/*.md` that cites a changed non-doc path by its full
path adds its own path and basename to what a test may name (#2299, the
selector's `doc_closure` above): a test that resolves a doc's citations names
the doc, not the file it cites. It runs them in **one** `pytest -q -n auto -p no:warnings` call
(collecting `tests/unit/scripts` whole costs ~135 s, so files, not
directories), then `scripts/lib/check-contract-parity.sh`, with 540 s and 60 s
deadlines so one call stays under the ten minutes a lane allows a command. It
prints the last lines of the output and keeps the full log only on failure. An
empty diff runs nothing and exits 0; a base that does not resolve exits 2
rather than reading as an empty diff.

**A docs-only change selects only the doc guards** (GUARDS-DEDUP, part of
#1694). Docs-only lanes spent 44-50 min in guards on 2026-10-19/20. When every
changed path is under `docs/` or ends in `.md`, the script selects
`DOC_GUARD_TESTS` -- `test_docs_describe_what_was_built`,
`test_docs_spec_amendments` and `test_docs_agent_vocabulary` -- and the tests
whose text names a changed doc, and runs no contract-parity check. The UI route
seam, `test_specsign_covers` and the other whole-tree guards read source, not
docs, and the parity check skips `.md` files outright, so none of them can go
red on a doc change. A doc
beside any other path keeps the whole guard set.

**The worker does not run it again on the same tree** (GUARDS-DEDUP). The
worker's pre-publish gate (`agent_worker/prepublish.py`) re-ran the script on
code the agent had just passed in 73 of 74 lanes (4.9 h, 2026-10-19/20). Now
every run that reads its diff from git (not `--files`, not `--list`) records
how it ended (`passed`, `failed` or `timed_out`), the tree it ran on and its
base commit in `<git dir>/swarm-changed-guards/` (`all.json`, or
`shard-k-of-n.json` per part). The tree is the one the publish commits: HEAD
plus every uncommitted and untracked file, written from a scratch index. The
gate works out that tree and the base commit **itself** and skips its own run
only when a record says `passed` on both, for the whole selection or for every
part of one `--shard` split. It never takes the agent's word for it: a record
with another tree, another base or any other status means the script runs.
The skip and its reason go in the pull request's advisory section. The record
lives in the git directory, so it is never committed.

**A timed-out run is not re-run as-is.** Five lanes hit the 600 s limit in both
of the gate's rounds, with a fix turn in between that could not fix a timeout.
A guard run that ran out of time (the script's `ran past` line, or the gate's
kill at its check limit) is reported as a timeout and kept out of the fix
prompt. The next round runs it as the parts the script advised (`--shard 1/n`
... `--shard n/n`, at least two), each its own command under the limit. If
those time out too, it is published under "Pre-publish checks failed" as
`changed-guards timed out`.

**A heavy selection runs in parts, each under the 540 s limit** (#1334, PR
1415). The 540 s is not raised for a selection that needs longer: lane
GS-HOOKS's 79 files (2,603 tests) all passed in 595 s on 6 cores, and past
600 s an agent's own command limit kills the run with no `ran past` line and
no faulthandler frames to name what held it. So the script weighs its
selection in CI-seconds from `tests/unit/scripts/unit_test_durations.json` (a
file with no weight counts at the median) and, past `SHARD_HINT_WEIGHT` (540
CI-seconds, about 400 s in a 6-core container, measured 2026-10-16 at ~0.73 s
a CI-second), it does not make one call:

* **Without `--shard`** it warns, then runs the selection as
  ⌈weight / 540⌉ parts (at least two), **one after another**, each its own
  pytest call under the 540 s limit, so a hang still ends and a passing
  selection completes however many files it names. The whole run is then up
  to parts × 540 s: longer than ten minutes, by design. It exits 1 if any part
  failed.
* **`--shard k/n`** runs the k-th of n parts alone. The parts are cut the way
  CI cuts its unit shards (longest file first onto the lightest part), and the
  cut is deterministic, so the n commands together run every selected file
  once; the contract-parity check runs in part 1 only. An agent whose command
  limit is ten minutes runs the parts this way, as n separate commands: the
  warning prints the exact `--shard 1/n ... --shard n/n` lines to run.

**When it runs past 540 s it names the test** (box 102 of #888: lanes SPEC-GKE
and BRIDGE-BACKEND hit the cap on 2026-10-08 and nothing said which test held
it). The pytest call carries `--durations=10`, so a run that ends by itself
lists its ten slowest tests. That report never prints on an overrun: measured
2026-10-09 under xdist, `timeout`'s SIGTERM leaves no report and SIGINT leaves
an xdist teardown traceback. So the call also sets pytest's
`faulthandler_timeout=60`, which dumps the stack of any test still running
after a minute, and on exit 124 the script prints the dump's frames in a
`test_*.py` file (file, line, function). The fixed guard set alone runs in
about 50 s (2026-10-09), so one test past a minute is the suspect; if none
was, the line says so and the log's durations are where to look. Its first
reading (2026-10-09, the 30-file selection of the change that added it: 1,038
tests in 166-181 s, under the cap): the slowest was
`test_ui_changes_gate.py::test_the_shared_reading_of_the_list_agrees_with_the_step`
at 80 s, then `test_release_acceptance_job.py::test_the_acceptance_scripts_are_shellcheck_clean`
at 41 s and four `test_build_images_incremental.py` tests at 22-25 s each.
That first one no longer exists as one test (2026-10-11): it looped over a
sample under each APP_PATHS pattern and a near miss of each, building a
scratch repository and running the `changes` step for every sample serially,
so `-n auto` could not spread it. It is now parametrised over those samples,
one case each, which xdist spreads across workers; the property and the
sample set are unchanged, and a separate test fails if the sample list is
empty or is not two per pattern (an empty parametrisation would only skip).

**What it does not do.** It does not replace the area run (a test that imports
a changed module without naming its file is not selected), it runs no vitest
but the stylesheet guards on a swarm-ui `.css` change (see the related-tests
section below), no `terraform test` and nothing needing credentials, and a
green run proves nothing CI has not. `tests/unit/scripts/test_changed_guards.py` holds its
selection against fixture diffs.

### While it works, a SwarmCloud agent runs only the related tests

```bash
scripts/related-tests.sh             # print the related test files and the runs that test them
scripts/related-tests.sh --run       # run those, one after another, in the foreground
scripts/related-tests.sh --base <rev> | --files <list>|-
```

**Why** (RELATED-TESTS, part of #1694, owner decision 2026-10-20). Agents ran
whole suites and lost hours. Measured 2026-10-19/20: 36 vitest runs with no
file filter at about 22 min each on 4 cores (2.05 h), 8 pytest runs over whole
`tests/unit` directories (0.73 h), and both out-of-memory kills came from
`for i in 1 2 3 4; do npx vitest run --shard=$i/4 & done`. The rule an agent
is given (CLAUDE.md, and the claude-code runner's `RELATED_TESTS_NOTE` when the
checkout carries this script): related tests through this script; never vitest
without file arguments; never pytest over a whole directory or with `-n auto`
across the suite; never parallel shards or background batches polled with
sleep; `scripts/changed-guards.sh` once at the end.

**What is related** (`scripts/lib/related_tests.py`, standard library only).
Python: `select_unit_tests.py`'s rules, imported rather than restated -- the
change's own tests, every test that imports a changed module through any chain
of imports, every test whose text names a changed path or a script that names
one -- but **without** its fall-back to the whole suite: where CI would run
everything, this keeps the files it can name and leaves the rest to CI.
swarm-ui: the change's own test files and every vitest file that reaches a
changed module through relative imports (`import ... from`, `export ... from`,
`import '...'`, `import('...')`, `vi.mock('...')`), transitively -- the graph
`vitest related` follows, computed from the source so it needs no
`node_modules` and can be cut into runs. A change to what every component test
runs under (`vitest.config.ts`, `src/__tests__/setup.ts`, `package.json`, ...)
is related to no file by import: it is named in a warning and CI runs the suite.

**A stylesheet change also selects the stylesheet guards** (RELATED-CSS-GUARDS,
part of #1914, owner decision 2026-10-22). Any `.css` under `apps/swarm-ui/`
adds `stylesheet.gate.test.ts`, `sections.stylesheet.test.ts`,
`letterspacing.test.ts` and `typescale.test.ts` to the vitest selection, each
once however many sheets changed. They scan the text of **every** sheet --
they read it from disk with `import.meta.glob('../styles/*.css')`, directly or
through `sheets.ts` -- so no import connects them to the sheet a change edits
and the import graph never selects them. Measured 2026-10-22 on PR 1905: it
declared `.rn-filters > .c-seg > button` twice in one media block of
`styles/intake.css`; CI's gate and sections guards refused it, after the
lane's implement, fix and re-review agents had all passed this script and
`changed-guards.sh` without running either. The list is one tuple,
`STYLESHEET_GUARDS` in `scripts/lib/related_tests.py`; `changed-guards.sh`
reads it (`--stylesheet-guards`) and runs those guards in one vitest call from
`apps/swarm-ui/node_modules/.bin/vitest`, under the same 540 s limit, and
fails when vitest is not installed (`npm ci` there first) rather than reading
an unrun guard as a pass. Only no-render scans of every sheet belong on the
list: the ~170 screen tests that glob the sheets to compute the cascade
(through `marks.ts`, `spacingsweep.tsx`, `phone.chrome.test.tsx`) are behaviour
tests, and CI runs them.

**A changed sheet also selects the tests that read it by name** (NAMED-SHEETS,
part of #1914, owner decision 2026-10-22: "Yes, a name-keyed rule"). Some tests
read ONE sheet from disk -- `readFileSync(resolve(__dirname,
'../styles/repograph.css'))`, `join(__dirname, '..', 'styles', 'theater.css')`,
a helper's `css('theater.css')` -- and no import reaches them either; PR 1921
found seven (`browser.badges`, `repograph.vqa-l08`, `repositories.c6j`,
`repositories.graph`, `theater.followup`, `theater.v2`, `theater.take`). So a
`.css` change under `apps/swarm-ui/` also selects every vitest file holding a
quoted string that is the sheet: its file name alone, or a path ending in its
path relative to `src` (`styles/theater.css`). A bare word is not a name: a test
titled "the theater", or one that says `'styles/'`, is not selected for
`theater.css` or `styles.css`. Each file is selected once however many of its
sheets changed, and every file is read once per run. This is a related-test
rule, not a guard: `changed-guards.sh` does not run these files.

**A UI change also runs the node runners** (#2455). Any change under
`apps/swarm-ui/` adds one more run, after the vitest runs:
`(cd apps/swarm-ui && npm run test:node)`, once however many files changed, as
long as the package's `package.json` declares `test:node`. That script runs
`tests/run.mjs` and `test/*.test.mjs` under `node --test`, not vitest, and they
read the tree from disk -- `no-company-name.test.mjs` scans every source file
for the company's name -- so no import reaches them and the import graph never
selected them. CI's ui-build runs them. Four lanes (PRs #2429, #2430, #2433,
#2437) published UI code naming the company after passing this script, and went
red in ui-build. The node run names no files because the runner picks its own;
it is a whole package script by design, and a cheap one.

**It never prints an unfiltered vitest or pytest command.** Every pytest and
vitest line names files (the node run above is a package script). A selection
too big for one run comes back as a few runs: pytest cut by
`unit_test_durations.json` weight into parts of at most ~540 CI-seconds
(changed-guards.sh's `SHARD_HINT_WEIGHT`), vitest cut the same way by
`tests/unit/scripts/ui_test_durations.json` into runs of at most ~540
CI-seconds (`VITEST_RUN_WEIGHT`) and 50 files (`VITEST_FILES_PER_RUN`). `--run`
runs them in turn, each under 540 s, and stops at the first red; a run that
times out is not re-run as-is. `tests/unit/scripts/test_related_tests.py` holds
the selection for a UI-only, a Python-only and a mixed change, that no file run
is unfiltered, and that a UI change prints and runs `npm run test:node` once,
last.

**Vitest runs are weighted by recorded duration** (WEIGHTED-VITEST, part of
#2362). They used to be cut by count alone, 50 files a run on the assumption of
~2.9 s a file. The nine `spacing.*.test.tsx` slices take 252-485 s each in an
agent's container, and every one is reached by any change under `App.tsx`'s
import graph (`spacingsweep.tsx` imports `{App, SECTIONS}` from `'../App'`), so
they landed together in one run that could not finish inside the 540 s limit:
31 timeouts in the first 7 hours of 2026-10-25, 40+ by the next morning, each
followed by the step re-running them one by one until its own deadline. Now
`related_tests.py`'s `vitest_runs()` mirrors `pytest_runs()`: heaviest file
first onto the lightest run it fits, a new run whenever a file would take every
run over 540 s or 50 files, and a file heavier than 540 s alone. A file
`ui_test_durations.json` does not record weighs `VITEST_DEFAULT_SECONDS` = 2.9 s
(the whole suite, 463 files, in ~22 min on 4 cores); only files far over that
are recorded, so the file stays a few lines. Seeded 2026-10-26 from the agent
logs #2362 cites: `spacing.accounts` 485 s and `spacing.capacity` 259 s were
measured (the highest of each one's readings); the other six slices are an
estimate of 300 s, the low end of the 300-485 s one lane measured for them, which
also keeps any two slices over 540 s together.

To refresh the weights, run `scripts/refresh-ui-weights.sh` and commit the
diff. It finds main's newest successful `application.yml` run whose
`swarm-ui component tests` jobs all ran, reads their logs with `gh run view
--job <id> --log`, and has `scripts/lib/ui_weights.py` parse vitest's
per-file lines (`✓ src/__tests__/x.test.tsx (1 test) 259018ms`). It keeps only
files over 20 s. By default it never lowers a recorded figure, because the
seeded figures are agent-container times, which are what the 540 s limit is
measured against. A CI shard that ran a slice faster would otherwise put two
slices back into one run; `--replace` writes the measured figures alone. It
changes no workflow on purpose: a workflow file can only be pushed by swarm-api,
and the first lane that tried this by editing `application.yml` could not
publish.

**A UI change selects only the spacing slices whose screens it reaches**
(SPACING-NARROW, part of #2362). Weighting keeps the slices apart, but each
still costs 252-437 s, and by import every change under `App.tsx` reached all
of them: `spacingsweep.tsx` imports `{App, SECTIONS}` from `'../App'`. A slice
renders `App` at its own `SPACING_GROUPS` routes only, so `related_tests.py`
(`spacing_plan()`, `spacing_dropped()`) reads those routes from
`spacingsweep.tsx` and, from `App.tsx`'s `SectionBody`, the components each
`case '<route>':` renders, resolved through `App.tsx`'s imports to modules. A
`spacing.<group>.test.tsx` is then kept when the change reaches a module one of
its routes renders, or reaches the slice by a path that avoids every
`App.tsx` -> screen import: the shell (`Spine.tsx`, a hook App calls),
`App.tsx` itself, `spacingsweep.tsx` or `spaceprobe.ts`, which render on every
route. An import counts as a screen's only when every name it brings in is
used in `SectionBody`'s cases and nowhere else in `App.tsx`; a module App also
takes a card or a hook from (`Schedules`, `Workflows`, `Activity`) stays a
shell path. Screens import from one another -- Overview's cards come from
several -- so one screen's change usually keeps two or three slices of eight,
not one. A changed `src/styles.css` or `src/styles/*.css` keeps every slice,
because the sweep reads every sheet into every route's cascade by `?raw` import
and `import.meta.glob`, which no import edge follows. `spacing.test.tsx` is kept
whenever a slice is. Anything that does not parse -- no `SPACING_GROUPS`, no
`SectionBody`, a route with no case, or a component `App.tsx` neither imports
nor defines -- keeps every slice: a reshaped `App.tsx` costs time, never a
missed slice. `spacingsweep.tsx` and `App.tsx` are read, never edited for this;
`test_every_real_spacing_route_resolves_to_a_screen_module` holds the parse
against this checkout, so a renamed screen fails a test instead of quietly
turning the narrowing off.

## Where the suites are documented

[`testing.md`](testing.md) is still the reference for **what each suite covers
and what it does not**, which is the load-bearing half: every suite in this
repository that ever lied did so by being read as covering something adjacent to
what it actually asserted. Read it for that. Read
[the defect class it exists to catch](testing.md#the-defect-class-this-exists-to-catch)
before deciding a check is redundant.

The commands it prints are how a suite is invoked *by the workflow*. They are
not an instruction to run it here.
