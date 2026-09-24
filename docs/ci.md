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

## What a pull request therefore does not tell you

Stated plainly, because a green pull request is the thing most likely to be
over-read:

* **Nothing about the live project.** No plan, no apply, no deployed state.
* **Nothing about an image actually building.** Images build only on `main`,
  once per commit, in `application.yml`'s `build images` job — see below.
* **Nothing a browser would see.** The UI job is a typecheck plus Vitest in
  jsdom; jsdom has no layout engine, so overlap, overflow, wrapping and contrast
  are invisible to it. Every one of those defects this repository has found was
  found in a real browser and none of them turned a check red.
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
| cancelled, skipped, never ran, or recorded for another environment | fails: "dispatch release.yml for it" | **builds it here**, through the same script and tag |
| GitHub API unreadable | fails — an unreadable API is never read as "never built" | fails |

"Recorded for another environment" is every **prod** release:
`application.yml` builds for dev, and swarm-ui bakes its environment into the
bundle when it is compiled (`VITE_SWARM_ENV`), so a dev build is not a prod
build. A prod release is always dispatched, so it builds — and the build is
`build-images.sh`, the same path, not a second copy of it.

**`application.yml` is no longer cancelled on `main`.** Its concurrency group
used to cancel an in-progress run whenever a newer push arrived. Now that a run
on `main` is the only build of its commit, cancelling it would strand that
commit's release — and it never saved the Cloud Build anyway, because stopping
`gcloud builds submit` does not stop the build it submitted. A run still
*queued* is replaced by a newer push regardless; its release is queued in its
own group and is replaced the same way.

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
