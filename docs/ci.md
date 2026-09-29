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
   runner is pinned to `ubuntu-24.04` with a pinned tool version per job; the
   laptop is whatever it has drifted into since the last `brew upgrade`.
2. **A gate that runs in one place can be believed by everyone.** A mutation
   proven in CI is proven for every future reader of the branch. A mutation
   proven on a laptop is proven once, to one person, and the proof is a sentence
   in a report.
3. **A local full gate blocks the machine that is supposed to be writing.**
   `make test` is the whole offline suite; running it serialises against every
   other lane working in the same checkout.

## The six workflows, and what each one is responsible for

| workflow | runs on | jobs |
|---|---|---|
| `application.yml` | push to `main`; pull requests touching `apps/`, `images/`, `kubernetes/`, `scripts/`, `tests/`, `docs/`, `Makefile`, `pyproject.toml`, `uv.lock`, `README.md`, `CLAUDE.md`, `CONTRACT.md`, `release.yml`, `iam-refusal-probe.yml`, `ci-fix.yml` or the workflow itself | `shellcheck` · `release workflow wiring (actionlint)` (also lints `iam-refusal-probe.yml` and `ci-fix.yml`) · `format / unit tests` · `swarm-ui typecheck / component tests` · `integration tests (emulator)` · `kubernetes manifests` · `build images` (**push to `main` only** — the one build of each commit) |
| `terraform.yml` | push to `main`; pull requests touching `terraform/`, `tests/terraform/`, the plan guard, the destroy guard, the unlabelable-type list or the workflow itself | `fmt / validate / tflint` · `terraform test` · `checkov` · `plan` (**not** on a pull request) · `plan (not run on a pull request)` |
| `security.yml` | every pull request; push to `main`; Mondays 06:00 UTC | `trivy (repo)` · `secret scan` · `checkov (terraform + kubernetes)` · `platform policy assertions` · `trivy (published images)` (schedule / dispatch only) |
| `release.yml` | push to `main` touching `apps/`, `images/`, `terraform/`, `kubernetes/`, `scripts/` or the workflow; or manual dispatch with an environment | `verify` · `images and scan` (reuses `application.yml`'s build of the commit; moves nothing) · `approval` (the one job naming a GitHub environment — prod waits here) · `promote` · `terraform apply` · `deploy and smoke` — the last three only after `approval` succeeded, and on prod only in the attempt it succeeded in · `prod approval is from an earlier attempt` (runs only on a partial re-run of prod, and fails it) |
| `ci-fix.yml` | `application` **completing red on a `swarm/<task-id>` branch** of this repository (`workflow_run`, so only as the file is on `main`) ([below](#the-ci-fixer)) | `fix a red SwarmCloud pull request` |
| `auto-merge.yml` | `pull_request_target` when a label is added; acts only on `ready` ([below](#a-ready-pull-request-is-merged-by-github-not-by-a-session)) | `queue for auto-merge` (refuses a `[swarm] task_` title, an unprotected base branch or a missing merge App, with a comment; otherwise enables native squash auto-merge under the PR's title) |
| `iam-refusal-probe.yml` | **manual dispatch on `main` only**, by the owner, once ([below](#the-deployers-refusal-is-proven-once-by-a-probe-the-owner-dispatches)) — never on a push, a pull request or a schedule | `deployer is refused an unlisted role` |

Three details in that table are easy to misread and each has bitten someone:

* **`application.yml` fires on a docs-only pull request** — `docs/**` is in its
  path filter — and its `shellcheck` job is where documentation links are
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
* **Nothing about an image actually building.** Images build only on `main`,
  once per commit, in `application.yml`'s `build images` job — see below.
* **Nothing a browser would see.** The UI job is a typecheck, Vitest in jsdom,
  `node:test`, and the production build (`npm run build`); jsdom has no layout
  engine, so overlap, overflow, wrapping and contrast are invisible to it. Every
  one of those defects this repository has found was found in a real browser
  and none of them turned a check red. The build proves the bundle compiles on
  the image's Node, not that the `swarm-ui` image builds. That runs on `main`,
  in `build images`.
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
   this commit and this environment.
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
5. The apply pins those digests and the deploy verifies them, unchanged.

**What the release does when it cannot reuse.** Each ends in a red job whose
last line (and a run annotation) names the reason and links the job:

| `application.yml`'s build of the commit | push to `main` | dispatched by hand |
|---|---|---|
| succeeded, recorded for this environment | reused | reused |
| still queued or running | waited for (45 min cap) | waited for |
| **failed** | fails: "re-run that job if it was a flake" | fails the same way — rebuilding would repeat the failure behind a second Cloud Build |
| cancelled (before it started, or part-way), skipped, never ran, or recorded for another environment | fails: "re-run CI's run, then this release" — a dispatch would release main's head, not this commit | **builds it here**, through the same script and tag |
| GitHub API unreadable | fails — an unreadable API is never read as "never built" | fails |

"Recorded for another environment" is every **prod** release:
`application.yml` builds for dev, and swarm-ui bakes its environment into the
bundle when it is compiled (`VITE_SWARM_ENV`), so a dev build is not a prod
build. A prod release is always dispatched, so it builds — and the build is
`build-images.sh`, the same path, not a second copy of it.

**On `main`, every commit's `application.yml` run has a concurrency group of
its own, and is never cancelled.** Its group used to be the ref, and a newer
push cancelled the in-progress run. Now that a run on `main` is the only build
of its commit, nothing may stop it before it builds — and turning
`cancel-in-progress` off was not enough on its own. GitHub keeps **one pending
run per group** and cancels the older pending run when a newer one queues,
whatever `cancel-in-progress` says. Measured on 2026-09-24: release run
36035365877, pending in `release-dev`, was cancelled at 17:41:28 — two seconds
after 36036058352 queued — and lists zero jobs.

With one group for all of `main` that cancelled builds releases were waiting
for. `application.yml` runs on **every** push to `main`; `release.yml` has a
path filter. So: commit A building, B queued behind it, and a push C that
touched only docs, tests or the README. C's run cancelled B's; C started no
release to take the place of B's; B's release, still pending, then found its
commit never built and went red, and B's change did not reach dev until the
next push that starts a release. (The group is now
`application-refs/heads/main@<sha>` on `main` and the ref everywhere else, so
a pull request's newer push still supersedes its older run.)

**What that costs.** Runs on `main` are no longer serialised. A burst of merges
builds every commit, in parallel; each run is bounded to four Cloud Builds at
once (`BUILD_PARALLELISM` in `build-images.sh`), in a project whose build
quota is shared. An estimate from measured push times, not measured builds:
on 2026-09-24 `main` took 18 pushes between 15:56:14 and 17:41:26 — one every
six minutes — and a run that finished took 9–10 minutes, the build its last
eight. In the densest stretch (five pushes between 16:42 and 16:56) three runs
would have been building at once: twelve concurrent Cloud Builds. Each commit
is still built exactly once. Cancelling part-way never saved a build anyway:
stopping `gcloud builds submit` does not stop the build it submitted.

A release of a commit is still replaced while *it* is pending, by the next
push that starts a release — which is harmless, because that release ships a
later commit that contains this one.

**Kept, deliberately:** scan before promote; all-or-nothing promotion; every
deployed image pinned by digest. (The `dev`/`prod` environments used to sit on
the apply and the deploy; they now sit on the one `approval` job in front of
the promotion — see the next section.)

**What a pull request cannot prove about this.** `release.yml` never runs on a
pull request, and `build images` is skipped there, so PR CI never exercises
the real hand-off. It checks the scripts against a fake `gh` and `gcloud`
(`tests/unit/scripts/test_ci_built_images.py`,
`test_push_images_promotes_recorded_digests.py`), reads both workflows to hold
the wiring in place (`test_release_reuses_ci_images.py`), and lints both with
actionlint. It cannot show that the job receives `actions: read`, that
GitHub's API returns what the fake returns, that `gh run download` fetches the
artifact across runs, or that a release actually gets faster. The first push
to `main` after this lands is the first real proof — read its release run.

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
| `push-images.sh --scan-only` — every digest confirmed and trivy-scanned; nothing moves | `deploy and smoke` — digest verification, the in-VPC smoke suite, the GKE proof |
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
So `approval` is the only job in `release.yml` that names an environment;
`promote`, `terraform apply` and `deploy and smoke` name none. They run only
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
* `deploy and smoke`'s GKE proof dispatches a browser task into the
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
* On prod, `promote`, `terraform apply` and `deploy and smoke` each run only
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
says whether prod changed. Its `deploy and smoke` summary lists the digests
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
| 2 | `roles/iam.roleAdmin` (unscopable) carries `iam.roles.update`. CI can add `logging.logEntries.list` to a custom role it holds: `swarmSecretProvisioner` (its scoped type guard admits every non-secret resource), `swarmDeployerProjectBuckets` (always unconditioned in `wif.tf`), or one of the custom roles the scoped `projectIamAdmin` still lets it grant itself. | **not by a condition, which cannot scope `roleAdmin`; closed by removing it** (#79, owner decision 2026-09-25). `roleAdmin` is off `deployer_roles` and a validation refuses it, and every custom role is defined in [`terraform/bootstrap/platform_roles.tf`](../terraform/bootstrap/platform_roles.tf), which the owner applies, so CI can change no role's permissions. **Closed once applied:** open until the owner's bootstrap apply destroys the live binding ([runbook](runbooks/custom-roles-to-bootstrap.md), step 2). |
| 3 | `roles/iam.serviceAccountAdmin` (unscopable) carries `iam.serviceAccounts.setIamPolicy`. CI can grant itself `serviceAccountTokenCreator` on an account that reads logs (the compute account above, or `209012342332@cloudbuild`, which holds `roles/cloudbuild.builds.builder`) and act as it. | **no** |
| 4 | `roles/logging.configWriter` keeps sinks and exclusions project-wide even when scoped. A sink can route every log to a `swarm-` bucket (`storage.admin`) or a Pub/Sub topic (`pubsub.admin`) that CI reads. | **no** |
| 5 | `roles/logging.configWriter` unconditioned holds `logging.views.update`: CI can rewrite this view's filter, or make another view, and read the result through the grant. | yes, once `roles/logging.configWriter` is in `deployer_scoped_roles` |
| 6 | `roles/resourcemanager.projectIamAdmin` unconditioned lets CI grant itself `roles/logging.viewer`, or any other role that reads logs. Scoped, it may modify only the roles terraform/infra grants: 14 since 2026-09-25, when `swarmSecretLister` left the list (#69) because its project-wide `secrets.setIamPolicy` reaches the other team's secrets and `terraform/bootstrap` now makes the broker's grant of it. None of the 14 reads a log entry: the nine predefined ones are absent from [`log-reading-roles.json`](../terraform/bootstrap/log-reading-roles.json), and the five custom ones carry no `logging.` permission ([`platform_roles.tf`](../terraform/bootstrap/platform_roles.tf)). | yes, once `roles/resourcemanager.projectIamAdmin` is in `deployer_scoped_roles` **and** route 2 is closed. Before route 2 closes, `roleAdmin` could widen one of the grantable custom roles, or add `resourcemanager.projects.setIamPolicy` to a role CI holds unconditioned, and the condition would never be evaluated. Route 2 closes with the owner's bootstrap apply in [the runbook](runbooks/custom-roles-to-bootstrap.md). |

What bounds routes 1, 3 and 4 today is the ref pin, not IAM: only a workflow
on `refs/heads/main` can mint the deployer's token, so each route has to be
merged to `main` first. The same held for 2, 5 and 6 until each is applied.
Closing 1 means building as an account without `roles/editor`; 3 means taking
`roles/iam.serviceAccountAdmin` off the deployer or replacing it with
resource-level grants on `swarm-*` accounts; 4 means moving sink management
out of CI. Each changes what CI can do, none is made here, and which to make is
the owner's decision. Route 2 was closed by the owner's decision on #79: no
resource-level grant exists for `roleAdmin` to be narrowed to (a role carries
no allow policy of its own; the project's testable permissions include no
`iam.roles.setIamPolicy`, read 2026-09-25), so it came off the deployer and the
custom roles moved to the root the owner applies.

## The deployer's refusal is proven once, by a probe the owner dispatches

Once PR #73's targeted apply lands, the deployer's `projectIamAdmin` carries the
`modifiedGrantsByRole` condition in
[`deployer_conditions.tf`](../terraform/bootstrap/deployer_conditions.tf) --
as of #275, chunked into two bindings rather than one: `hasOnly()` refuses a
list over 10 elements, and the fifteen grantable roles no longer fit in a
single call. Every chunk still authorises only a `setIamPolicy` whose modified
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

**#275's chunking is not yet reflected in the probe script (#276).**
[`iam-refusal-probe.sh`](../scripts/iam-refusal-probe.sh)'s preflight step
still asserts *exactly one* conditioned `projectIamAdmin` binding
(`bindings_for` on `SCOPED_ROLE`) and `die`s otherwise; after #275's apply it
will find two and stop before asking IAM anything. Filed as #276 rather than
fixed alongside #275, because the probe is `scripts/` (Track D) and #275's
brief was terraform/tests/docs only.

**What it cannot prove:**

* **That the `roleAdmin` route is closed (#79, PR #150).** That route changes a
  custom role CI already holds so that it carries `setIamPolicy`. The write is
  then authorised by that role, and the condition is never evaluated. The probe
  asks the condition a question that route never asks. If the route has already
  been used, preflight finds the permission on the role and stops. It does not
  show that the route is shut.
* **Who a listed role goes to.** `hasOnly` limits which roles change, not whose
  grant changes. CI can still grant any of the fifteen, unconditioned, to
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

1. **Only the pull request's current head.** A red run for a commit the branch
   has already moved past is ignored.
2. **The log is redacted, then capped.** `gh run view --log-failed` goes
   through `redact` (`scripts/lib/common.sh`) before anything else reads it,
   then each failed job keeps an equal share of its tail, 16 KiB in all
   (`LOG_EXCERPT_MAX_BYTES`). `redact` is a pattern list and not a boundary
   (its own header says so); what makes a leak survivable is that the excerpt
   is stored as a task's input, which the API serves masked.
3. **One step, through the API, by name.** It submits a one-step workflow:
   runner profile `claude-code`, the excerpt in `input.prompt`, strategy
   `direct-pr`, and `continues_task` naming the red branch's task. No image,
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

### Why a step may push to another task's branch

`continues_task` is a TASK ID, never a branch name: the worker derives the
branch from it with the prefix it pushed under, as the integrator derives its
contributors'. The API refuses it unless the task is in the caller's own tenant
(another tenant's reads exactly like a missing one), was itself `direct-pr` in
the same repository, and the workflow is `direct-pr` with one step and no
`repository_ref`. A continuation of a continuation continues the original
branch. The rules are in
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
  path `SWARM_IMPERSONATE_SA` gives an operator's laptop.
* **The deployer (`GCP_DEPLOY_SA`) needs `roles/iam.serviceAccountTokenCreator`
  on it**, because the workflow impersonates it from the deployer's Workload
  Identity token.
* **Its tenant must be the tenant that owns the swarm pull requests.** The API
  refuses to continue another tenant's task, by design. So the account has to
  be admitted (`allowed_users`) and a member of that tenant's Google group, or
  its personal tenant `u-<name>` will own nothing it can fix. The fix step runs
  on that tenant's `claude-code` credential and pushes with its
  `swarm-tenant-<tenant>-git` token, like every other `direct-pr` step.
* **`SWARM_API_HOST`**, optional: empty resolves the front door from
  `terraform/environments/<env>/<env>.tfvars`.

`workflow_run` only fires for a workflow file on the default branch, so the
fixer does nothing before it has merged.

## A ready pull request is merged by GitHub, not by a session

Owner decision, 2026-09-28 (#262): **adding the `ready` label enables GitHub's
native auto-merge**, and GitHub performs the squash merge once the base
branch's required checks pass at the pull request's head. It used to take a
merge watcher running in an operator's session, so a green, ready pull request
waited for somebody's laptop. [`auto-merge.yml`](../.github/workflows/auto-merge.yml)
is the whole mechanism; it waits for nothing and polls nothing.

It refuses, with a comment on the pull request saying which and why:

* **a title starting `[swarm] task_`** — the worker's placeholder. The squash
  subject is the pull request's title plus `(#N)`, set explicitly, because a
  one-commit squash otherwise takes the commit's own message: that is how #238
  put `swarm: work from task_...` on main as a headline. Retitle it as a
  fact-style headline, then remove and re-add `ready`;
* **a base branch with no required status checks** — auto-merge would have
  nothing to wait for and would merge at once, red or not;
* **no merge App configured** — see the next paragraph;
* **a check that already ran on the head commit and is failing or still
  running** — even one that is not required. Branch protection below requires
  only the checks that run on every pull request; a path-filtered workflow
  (`application.yml`, `terraform.yml`) is not required, but when a pull
  request's changes do trigger it, this still holds the merge on its result.

If the pull request is already green when the label lands, GitHub will not
*enable* auto-merge on it (its merge state is already `CLEAN`), so the
workflow merges it directly with the same token, method and subject. Branch
protection still decides; the App has no bypass.

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

### What the owner applies, once

These are repository settings. A workflow cannot apply them and no lane
should; they are here so that applying them is copying three commands.

**1. The merge App.** Create a GitHub App (Settings → Developer settings →
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
gh secret set MERGE_APP_PRIVATE_KEY --repo bogdan-alexandrescu/SwarmCloud < merge-app.private-key.pem
rm merge-app.private-key.pem
```

The key lives only in that Actions secret: never in this repository, a tfvars
file or a log. (It is not a tenant's forge token, which lives in Secret
Manager as `swarm-tenant-<tenant>-git`; this key merges this repository's own
pull requests and is read only by `auto-merge.yml`.)

**2. Auto-merge allowed on the repository:**

```bash
gh api --method PATCH repos/bogdan-alexandrescu/SwarmCloud -F allow_auto_merge=true
```

**3. Branch protection on `main`**, requiring only the checks that run on
**every** pull request — `security.yml`'s four jobs. `application.yml` and
`terraform.yml` both have a `pull_request` path filter
([table above](#the-six-workflows-and-what-each-one-is-responsible-for)), so
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

## Where the suites are documented

[`testing.md`](testing.md) is still the reference for **what each suite covers
and what it does not**, which is the load-bearing half: every suite in this
repository that ever lied did so by being read as covering something adjacent to
what it actually asserted. Read it for that. Read
[the defect class it exists to catch](testing.md#the-defect-class-this-exists-to-catch)
before deciding a check is redundant.

The commands it prints are how a suite is invoked *by the workflow*. They are
not an instruction to run it here.
