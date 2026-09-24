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

## The four workflows, and what each one is responsible for

| workflow | runs on | jobs |
|---|---|---|
| `application.yml` | push to `main`; pull requests touching `apps/`, `images/`, `kubernetes/`, `scripts/`, `tests/`, `docs/`, `Makefile`, `pyproject.toml`, `uv.lock`, `README.md`, `CLAUDE.md`, `CONTRACT.md`, `release.yml` or the workflow itself | `shellcheck` · `release workflow wiring (actionlint)` · `format / unit tests` · `swarm-ui typecheck / component tests` · `integration tests (emulator)` · `kubernetes manifests` · `build images` (**push to `main` only** — the one build of each commit) |
| `terraform.yml` | push to `main`; pull requests touching `terraform/`, `tests/terraform/`, the plan guard, the destroy guard, the unlabelable-type list or the workflow itself | `fmt / validate / tflint` · `terraform test` · `checkov` · `plan` (**not** on a pull request) · `plan (not run on a pull request)` |
| `security.yml` | every pull request; push to `main`; Mondays 06:00 UTC | `trivy (repo)` · `secret scan` · `checkov (terraform + kubernetes)` · `platform policy assertions` · `trivy (published images)` (schedule / dispatch only) |
| `release.yml` | push to `main` touching `apps/`, `images/`, `terraform/`, `kubernetes/`, `scripts/` or the workflow; or manual dispatch with an environment | `verify` · `images and promote` (reuses `application.yml`'s build of the commit) · `terraform apply` · `deploy and smoke` — the last two behind a GitHub environment |

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
2. `release.yml`'s `images and promote` job runs
   `scripts/build-images.sh --reuse-ci only` on a push. That calls
   `scripts/lib/ci-built-images.sh`, which finds `application.yml`'s run for
   the same commit on `main`, **waits** while the build is queued or running
   (up to 45 minutes), downloads the record, and refuses it unless it names
   this commit and this environment.
3. `scripts/push-images.sh --manifest build/images-dev.json --scan` confirms
   every recorded digest is in Artifact Registry, trivy-scans every one, and
   only then moves the channel tags — all or nothing, with the put-back and the
   `MIXED` report exactly as before. The tag `:<sha>` is not read at all, so a
   later build of the same commit cannot change what is promoted.
4. The apply pins those digests and the deploy verifies them, unchanged.

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
deployed image pinned by digest; the `dev`/`prod` environments on the apply
and the deploy.

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
| 2 | `roles/iam.roleAdmin` (unscopable) carries `iam.roles.update`. CI can add `logging.logEntries.list` to a custom role it holds: `swarmSecretProvisioner` (its scoped type guard admits every non-secret resource), `swarmDeployerProjectBuckets` (always unconditioned in `wif.tf`, not yet applied), or one of terraform/infra's six custom roles, which the scoped `projectIamAdmin` still lets it grant itself. | **no** |
| 3 | `roles/iam.serviceAccountAdmin` (unscopable) carries `iam.serviceAccounts.setIamPolicy`. CI can grant itself `serviceAccountTokenCreator` on an account that reads logs (the compute account above, or `209012342332@cloudbuild`, which holds `roles/cloudbuild.builds.builder`) and act as it. | **no** |
| 4 | `roles/logging.configWriter` keeps sinks and exclusions project-wide even when scoped. A sink can route every log to a `swarm-` bucket (`storage.admin`) or a Pub/Sub topic (`pubsub.admin`) that CI reads. | **no** |
| 5 | `roles/logging.configWriter` unconditioned holds `logging.views.update`: CI can rewrite this view's filter, or make another view, and read the result through the grant. | yes, once `roles/logging.configWriter` is in `deployer_scoped_roles` |
| 6 | `roles/resourcemanager.projectIamAdmin` unconditioned lets CI grant itself `roles/logging.viewer`. | yes, once scoped (but not route 2) |

What bounds all six today is the ref pin, not IAM: only a workflow on
`refs/heads/main` can mint the deployer's token, so each route has to be merged
to `main` first. Closing 1 means building as an account without `roles/editor`;
2 and 3 mean taking `roles/iam.roleAdmin` and `roles/iam.serviceAccountAdmin`
off the deployer or replacing them with resource-level grants on `swarm-*`
roles and accounts; 4 means moving sink management out of CI. Each changes
what CI can do, none is made here, and which to make is the owner's decision.

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
