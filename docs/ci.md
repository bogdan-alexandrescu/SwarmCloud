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
| `application.yml` | push to `main`; pull requests touching `apps/`, `images/`, `kubernetes/`, `scripts/`, `tests/`, `docs/`, `Makefile`, `pyproject.toml`, `uv.lock`, `README.md`, `CLAUDE.md`, `CONTRACT.md` or the workflow itself | `shellcheck` · `format / unit tests` · `swarm-ui typecheck / component tests` · `integration tests (emulator)` · `kubernetes manifests` · `build images` |
| `terraform.yml` | push to `main`; pull requests touching `terraform/`, `tests/terraform/`, the plan guard, the destroy guard, the unlabelable-type list or the workflow itself | `fmt / validate / tflint` · `terraform test` · `checkov` · `plan` (**not** on a pull request) · `plan (not run on a pull request)` |
| `security.yml` | every pull request; push to `main`; Mondays 06:00 UTC | `trivy (repo)` · `secret scan` · `checkov (terraform + kubernetes)` · `platform policy assertions` · `trivy (published images)` (schedule / dispatch only) |
| `release.yml` | push to `main` touching `apps/`, `images/`, `terraform/`, `kubernetes/`, `scripts/` or the workflow; or manual dispatch with an environment | `verify` · `build and promote` · `terraform apply` · `deploy and smoke` — each behind a GitHub environment |

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
what `application.yml`'s `build` job spends twenty lines forbidding, and the
reason it spends twenty lines on it is that the job is *currently saved by the
pin by accident*: `build` authenticates as the deployer and then runs
`scripts/build-images.sh` from the pull request's own checkout, and that script
executes any checked-in `images/<t>/cloudbuild.yaml` or `apps/<t>/cloudbuild.yaml`
as a Cloud Build config — so the pull request author chooses what runs. The
attribute condition rejects the PR ref and the auth step fails. That is not a
control; it is standing pressure to widen the pin to "fix CI". The job is gated
on a GitHub environment with required reviewers instead, so a human reads the
diff before any credential is minted.

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
* **Nothing about an image actually building**, unless a reviewer approved the
  `build-pr` environment on that run.
* **Nothing a browser would see.** The UI job is a typecheck, Vitest in jsdom,
  `node:test`, and the production build (`npm run build`); jsdom has no layout
  engine, so overlap, overflow, wrapping and contrast are invisible to it. Every
  one of those defects this repository has found was found in a real browser
  and none of them turned a check red. The build proves the bundle compiles on
  the image's Node, not that the `swarm-ui` image builds. That runs on `main`.
* **Nothing about the seams against a real deployment.** Smoke, concurrency,
  race, failure and e2e run through `scripts/verify-remote.sh` inside the VPC,
  because `swarm-api` ingress refuses a laptop. They are not pull-request
  checks.

`terraform test` runs against a *mock* provider, so it proves the configuration
says what was meant — never that GCP would accept it.

## The UI job's Node is read from the image, not pinned

The `ui` job does not name a Node version. Its first step reads the major from
`ARG NODE_IMAGE` in [`images/swarm-ui/Dockerfile`](../images/swarm-ui/Dockerfile)
and hands it to `setup-node`. The step fails unless it finds exactly one such line.

The step after `setup-node` checks that the Node on `PATH` has that major, and
fails if it does not or if the major arrived empty. Without that check, an empty
value would read as success. `setup-node@v4` treats an empty `node-version` as
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
