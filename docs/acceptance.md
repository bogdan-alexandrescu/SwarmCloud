# The acceptance suite

`scripts/acceptance/run.sh` dispatches **real** tasks against a deployed
environment and asserts what they **produced**. The other suites prove a task
reached `SUCCEEDED`. That is not enough, and the proof is on record: every
browser task this platform reported as a success before 2026-09-29
screenshotted `about:blank`. The runner took the picture, the artifact
uploaded, the task succeeded, and nothing ever looked at the picture. So here a
terminal state is a precondition and never the verdict; every check reads an
output: a PNG's pixels, a patch's lines, a staged file's bytes, a pull
request's title, a refusal's code. (Owner decision, 2026-09-29.)

## Running it

```bash
scripts/acceptance/run.sh --list                  # every group and check; touches nothing
scripts/acceptance/run.sh                         # every group
scripts/acceptance/run.sh --only mock             # one group
scripts/acceptance/run.sh --only generic,browser  # several

# inside the VPC, with the credentials smoke uses (what the release does):
./scripts/verify-remote.sh acceptance/mock
./scripts/verify-remote.sh acceptance/generic
./scripts/verify-remote.sh acceptance/claude-code
./scripts/verify-remote.sh acceptance/workflow
./scripts/verify-remote.sh acceptance/browser
./scripts/verify-remote.sh acceptance/live-browser
./scripts/verify-remote.sh acceptance/git-tools
```

`verify-remote.sh` runs `scripts/<target>.sh` in the `swarm-verify` job and
passes no arguments, so each group has a wrapper, `scripts/acceptance/<group>.sh`.
That also gives each group its own execution and its own 30-minute timeout
(`terraform/infra/verify.tf`); the seven together would not fit in one.

Each check prints `PASS`, `FAIL` or `SKIP` with a one-line reason and the task
id, and the run exits non-zero when any check failed. `accept.yml` runs every
group but live-browser after a dev release completes, one matrix job per group
in two waves that fit the smoke tenant's ceiling, after its own warm and smoke.
A `FAIL` fails that group's job and opens (or updates) the one acceptance
issue; it no longer fails the release ([ci.md, "The release timeline"](ci.md#the-release-timeline)).
live-browser runs every three hours instead, off the red path, and files its
own issue, until it passes five scheduled runs in a row
([ci.md, "live-browser runs on a schedule"](ci.md#live-browser-runs-on-a-schedule)).

## What accept.yml's identity may run

`accept.yml` starts these executions as its own account, `swarm-accept`
(`terraform/bootstrap/acceptance.tf`; [ci.md, "Its identity is not the
deployer"](ci.md#its-identity-is-not-the-deployer)). Its custom role
`swarmAcceptanceRunner` — get a job, start an execution, read executions and
tasks, cancel a running execution; no create, update, delete or IAM — is
granted at **project level with no condition**. Owner decision 2026-10-09
(#965).

Why no condition: the grant used to be conditioned to `swarm-verify` and
`swarm-job-*` by job name, and that condition never matched anything. Cloud
Run does not expose `resource.name` to IAM Conditions (Google's "Resource
attributes for IAM Conditions" lists no `run.googleapis.com` resource, and the
IAM Policy Troubleshooter evaluated `resource.name.endsWith("/jobs/swarm-verify")`
as false with the name supplied), so the account was denied `run.jobs.get` on
swarm-verify and every acceptance run failed before its first check.

The trade-off, accepted by the owner: `saga-agents-staging` is a shared
project, and the unconditioned grant reaches **every Cloud Run job in it,
present and future**, including the other team's and swarm's own merge job
(`swarm-job-eng-merge`). It can start, read and cancel their executions; it
cannot change, create, delete or re-permission any of them. Do not restore a
job-name condition: Cloud Run would deny every job again.

## Where it runs: the smoke tenant and a private sandbox

Every task is submitted for the **`smoke` tenant** (`X-Swarm-Tenant: smoke`)
and clones the **private sandbox repository**
`bogdan-alexandrescu/swarmcloud-sandbox`, where the direct-pr and integrate
checks open their pull requests. Both are stated once, in
`scripts/acceptance/config.sh` (owner decision 2026-10-05, #628). Until then
acceptance ran in `eng` against this platform's public repository: 58 fixture
pull requests there in five days, each running full CI, and 62% of eng's tasks.

Before any group runs the suite refuses to start when the API does not resolve
it to `smoke`, or when an anonymous read of the repository succeeds (a public
repository) or cannot be had. Nothing in `scripts/acceptance/` names the
public repository, and `config.sh` refuses it as an override.
`tests/unit/scripts/test_acceptance_target.py` holds all three. Why `smoke` is
reached through the header and not a service-account listing, and what the
owner applies for it, is in
[ci.md](ci.md#release-acceptance-runs-in-the-smoke-tenant-against-a-private-sandbox).

The sandbox holds nothing of this repository unless something puts it there.
In the release, `scripts/acceptance/sandbox-sync.sh` runs before the suite and
writes this commit's `tests/acceptance/fixtures/` to the sandbox's `main` (only
when they differ; no other path), and opens or checks the fixture issue. A
local run against a branch you pushed to the sandbox yourself sets
`SWARM_ACCEPTANCE_REF`.

| variable | default | what it changes |
|---|---|---|
| `SWARM_ACCEPTANCE_TENANT` | `smoke` | the tenant every call selects with `X-Swarm-Tenant`; the caller must be a confirmed member of its group |
| `SWARM_ACCEPTANCE_REPOSITORY_URL` | the sandbox | the repository the tasks clone; refused when it is the repository the checkout or CI run is for, or publicly readable |
| `SWARM_ACCEPTANCE_REF` | `main` | the ref of the sandbox the tasks clone. A release run cannot set it (verify-remote passes no environment), and reads main, where `sandbox-sync.sh` just wrote the deployed commit's fixtures |
| `SWARM_ACCEPTANCE_TIMEOUT` | `900` | how long one task may take |
| `SWARM_ACCEPTANCE_ADMIT_WAIT` | `300` | how long a never-admitted task is waited on before it is a SKIP |
| `SWARM_ACCEPTANCE_ISSUE`, `_ISSUE_EXPECT` | `1`, `sandbox-probe.sh` | the sandbox issue the `issue` input fetches, and a string only its text contains |
| `SWARM_ACCEPTANCE_GITHUB_TOKEN` (or `GH_TOKEN`, `GITHUB_TOKEN`) | unset | a token that reads and writes the sandbox: lets each check read its pull request back and close it. Without one, those reads are SKIPs (the sandbox is private) |

## SKIP is not PASS

A check that cannot be measured says so and why: a profile input this
deployment does not declare yet, a pool an operator closed, a credential the
caller's tenant does not hold, a third-party page's bot wall. It is counted apart and listed by
name in the summary, as testlib's `t_skip` does for every suite. Nothing here
passes silently. Two skips are decided by the platform's own behaviour rather
than restated from its configuration:

* **Not admitted.** A task still waiting with no attempt after
  `SWARM_ACCEPTANCE_ADMIT_WAIT`, whose `blocked_by` names a pool that is
  *closed* -- `MANUAL_PAUSE`, or a limit of 0, as the scheduler itself records
  them (`swarm_common.admission`) -- or that is parked on `MANUAL_PAUSE` or
  `BUDGET_EXHAUSTED`, is held on purpose. It is cancelled and the check skips,
  naming the pool. A pool that is merely *full* (the suite's own tasks fill it)
  is waited on for the whole timeout, and a task that never runs then FAILS. A
  task parked on `CREDENTIAL_MISSING` skips at once: the tenant has no
  credential for the profile's provider.
* **Not deployed.** Whether a runner input is declared is asked of the door
  without creating anything. The probe carries `metadata.expected_outputs`, a
  key only the service may write, and swarm-api checks a runner's input
  *before* it checks reserved metadata (`SubmissionService._build_task`). A
  declared input that is refused answers `invalid_input`; an undeclared one is
  let through to the metadata check and answers `invalid_dispatch`. Either way
  nothing is created. Every door check in this suite carries the same key, so a
  door that fails to refuse is a `FAIL`, never a task.

## What each check asserts, and why

### mock

The mock has no provider, so this group needs nothing but a deployment. Its
tasks are submitted together and judged after, so the group costs about its
slowest task.

| check | asserts | why that and not less |
|---|---|---|
| success | `completed_steps/requested_steps` is `2/2` and the runner's summary says so; every lease its events name is released | `SUCCEEDED` without the runner's own record is what the about:blank tasks had |
| fail with fail_message | `FAILED`, `end_cause` `runner_error`, `last_error` carries the exact message, one attempt | the message is the only thing that tells a user *why*; a generic "runner exited 1" loses it |
| every exit_code class | codes 2, 76 and 255 each end `FAILED runner_error` with that code in `result_summary.exit_code` and the runner's message; 0, 77, 78, 143 and 256 are a 422 at the door | 76 is the worker's own timeout code and must still read as the runner's failure; 77/78/143 mean park, refused credential and SIGTERM to the worker, so accepting them would turn a failure on purpose into something else |
| short rate limit retried | a `retrying` event, a second attempt, `SUCCEEDED`, and the runner saw its saved state (`was_resumed`) | a 1 s retry-after is under the worker's in-place threshold (45 s), so it must be retried in place before the park |
| quota park | `PARKED` on `QUOTA_EXHAUSTED`; `next_eligible_at` at least 85 s after the `parked` event for a 90 s retry-after; no `retrying` event; while parked, `current_lease_id` is null and every lease is released; then `SUCCEEDED` | invariant 1: a parked task costs nothing. Invariant 4: a long wait is a park, not a sleep |
| checkpoint restored | the second attempt's `checkpoint_restored` names the last checkpoint written before it, the task's `latest_checkpoint` while parked names the same one, and the runner reports `was_resumed` | a resume that starts from nothing still succeeds for the mock; only the restored id shows the checkpoint was used |
| cancel while running | observed `RUNNING`, cancelled, ends `CANCELLED` with fewer than its 20 steps done, leases released | a cancel that lets the work finish is not a cancel |
| cancel while queued | a workflow step waiting on a sleeping parent: no lease while it waits, `CANCELLED` after the cancel, and its events never name a lease | deterministic "queued" without racing the scheduler; invariant 1 where it is observable |
| periodic checkpoints | 150 s of sleep at the mock's 30 s interval writes at least 3 checkpoints, and their ids increase in write order | invariant 8: checkpointing is periodic. The final checkpoint alone is one |
| artifact bytes | the downloaded artifact is byte-identical (`cmp`) to `artifact_text`, which carries an inner newline, a trailing newline and a non-ASCII character | those are exactly what a lossy path (a shell variable, a JSON round trip, a charset guess) changes. Downloaded with curl to a file, never through a shell variable |
| steps | `completed_steps` and `requested_steps` are both 7 | |
| cpu_burn | the runner reports 20 s, ran at least 20 s, and the attempt's peak RSS is under the class's memory with no OOM near miss | invariant 7: `requests == limits`, so an overshoot is a kill, not a burst |

### generic

Every command in `GENERIC_COMMANDS` (`apps/agent-worker/agent_worker/runners/generic.py`)
runs against `tests/acceptance/fixtures/generic` in a clone of the sandbox.
Each check asserts the command the runner *recorded*, its exit code, and the
output the fixture prints: pytest's summary line reads `3 passed` (and `1
passed` for the `paths` variant, which names one file), and `npm test`, `npm
run build` and `make acceptance` each print a marker. `npm ci` and `uv sync`
are asserted by exit code alone: they write progress to stderr, and its wording
changes between versions. The fixture has no dependencies, so neither needs a
registry.

Bad arguments -- a command outside the catalogue, no command, a path that is a
flag or escapes the workspace, a make target that is a flag, an escaping
working directory, an `argv`, a timeout above the platform's -- must each be a
422 `invalid_input` at the door. That door is contract request 32 (#218, PR
#345); until it is deployed the check skips, because a bad argument would
become a task the runner refuses rather than a refusal.

### claude-code

Prompts are one or two sentences: every check spends subscription quota. The
fixture is `tests/acceptance/fixtures/claude-code`: `calc.py`'s `add()`
subtracts, on purpose, and `test_calc.py` fails because of it. **Do not fix
that file.**

* **collect.** The harvested `swarm-work.patch` changes only `calc.py`, removes
  exactly `    return a - b` and adds a line in its place, and applies (with
  `git apply`, or `patch` where git is absent) to `calc.py` as it is at the
  cloned ref, after which `add()` reads `return a + b`. The original is fetched
  through GitHub's contents API, because the swarm-verify image carries
  `scripts/` and not `tests/`; the sandbox is private, so without a token that
  step is a SKIP.
* **direct-pr.** A pull request opens; its title on GitHub is the line in the
  task's `pr-title.txt` artifact, `pull_request_text.title` is `agent`, the
  title carries no task id (the owner's 2026-09-28 rule), the body carries the
  task id, the head is the branch the task pushed, and the diff changes only
  the fixture and removes the bug line. Everything after "the title is the
  agent's" is read back from the sandbox and needs a token; the release's run
  has none, so there it is a SKIP naming why. Then the pull request is closed
  and the branch deleted.
* **issue.** With `issue: 1`, the sandbox's fixture issue, the agent is asked
  only for the file name the issue is about, and the answer
  (`GET /tasks/{id}/answer`) must name `sandbox-probe.sh` -- which only that
  issue's text says. `sandbox-sync.sh` opens it on an empty sandbox and
  refuses a sandbox whose #1 says something else. Contract request 28 (#265);
  whether it is deployed is probed at the door.

### workflow

| check | asserts |
|---|---|
| input_from | step `a` writes a known file; step `b` stages it; the file in `b`'s own last checkpoint (its workspace as it saw it) is byte-identical to what `a` wrote |
| expected_outputs | `a`'s `metadata.expected_outputs` names the file `b` stages; a parent that writes the wrong file ends `FAILED outputs_missing`, `result_summary.expected_outputs_missing` names the file, it was retried to `max_attempts`, and its child ends `CANCELLED` having never held a lease |
| on_step_failure | a failing root's child and grandchild both end `CANCELLED workflow_sweep` with no lease ever, and the workflow's derived state is `FAILED` (the fail_workflow sweep in `apps/scheduler/scheduler/loop.py` `_sweep_failed_workflow` cancels every not-started step of the workflow ahead of the per-parent `failed_parent` cascade, dependent or not, and always records `end_cause=WORKFLOW_SWEEP`) |
| integrate chain | implement (fix the bug, stage its `change.diff`) -> review (apply the staged `change.diff`, judge it, write `verdict.json`, then reverse the patch) -> fix (read the staged `verdict.json`, write `verdict-seen.txt` and `pr-title.txt`), strategy `integrate`: all three succeed, review's `result_summary.staged_inputs` lists implement's `change.diff` (without it review would be judging repository_ref's unfixed fixture, not implement's work -- strategy `integrate` gives a non-integrator step no other way to see an upstream step's tree, docs/workflows.md), exactly one pull request exists and fix opened it, fix read the verdict review wrote (`MERGE`), the pull request's body lists the implement branch as merged, and its diff removes the bug line (those two are read back from the private sandbox, and are a SKIP in a run with no token for it, as the release's is). Then the pull request and every branch are cleaned up |
| carry | docs/workflows.md's park-and-carry recipe: `a` (mock, `quota_exhausted` + `artifact_before_park`) writes `notes.md` and parks, then succeeds on a later attempt that writes nothing; `a`'s `result_summary.artifacts` has one `notes.md`, whose `carried_from` is the attempt the `parked` event names and whose `uri` is that attempt's object, none under the finishing attempt's prefix; `b` succeeds, its `staged_inputs` names that object, and its checkpoint's `notes.md` is `a`'s bytes. It exists because #166 was reopened by a live dev measurement (a 403), and only a live measurement the other way closes it: the owner asked for this one on 2026-10-08 |

### browser

The pages are **third-party sites** (owner decision on #358, 2026-09-29).
Nothing this repository owns serves an `.html` file as HTML:
`raw.githubusercontent.com` and jsDelivr answer `text/plain` with
`X-Content-Type-Options: nosniff` (measured 2026-09-29), so Chromium would show a
fixture's source instead of rendering it.

| check | page | asserts |
|---|---|---|
| example.com renders | `https://example.com/` | the title is `Example Domain`, `final_url` is on example.com, and `page.txt` (extract_text) carries "This domain is for use in documentation examples" |
| example.com's pixels | same task | the full-page screenshot, decoded by `scripts/acceptance/pngcheck.py`, is not blank, and at least half of it is the page's background, `#eee` |
| reddit.com renders | `https://www.reddit.com/` | `final_url` is on reddit.com, the title is not empty, the full-page screenshot is not blank. No exact text: the page changes. A bot challenge or consent wall (by title, or by the text of a short page) is a SKIP naming what was seen, never a PASS |
| fill and click | `https://httpbin.org/forms/post` | `fill` the customer name, `click` submit, and httpbin's echoed response carries `"custname": "<the name>"`. httpbin unreachable (a `net::ERR`, a timeout, a 50x) is a SKIP |
| door refusals | none | `http://169.254.169.254/...`, `http://10.0.0.1/`, `http://kubernetes.default.svc/` and `file:///etc/passwd` are each a 422 `invalid_input`, as `url` and as a `goto` action's `url` |

**example.com's visible text no longer says "Example Domain"** (measured
2026-09-29): only its `<title>` does, and the body reads "This domain is for use
in documentation examples without needing permission." So the title carries
the "Example Domain" assertion and the text assertion uses the body sentence.
The page also asks not to be relied on for testing and monitoring; one request
per dev release is the whole use. Its background is `light-dark(#eee,#222)`:
`#eee` under the light scheme headless Chromium renders by default.

**httpbin.org** is the form page because it exists to echo requests: filling a
form there sends test data to a service built to receive it, and its response
proves the fill reached the field and the click submitted the form. It is not
always available, which is why its outage is a SKIP.

These need contract request 32 (#218, PR #345) deployed, and the group skips
until it is.

**`pngcheck.py` uses the standard library only** (`zlib` and the five PNG row
filters). The swarm-verify image carries `python3` for it (#358). Wherever the
suite runs without `python3`, the pixel checks SKIP saying so. It decodes at
most the top 4000 rows (`--max-rows`): a real site's full-page screenshot can
be tens of thousands of rows, which pure Python unfilters in minutes, and
whether a page rendered is decided at its top.

### live-browser

One `claude-code-browser` task, and a scripted person who signs in on its
browser (docs/design/live-browser.md, section 9, lane LB-L13). The shell half
is `scripts/acceptance/groups/live-browser.sh`: it submits the task and waits
on it with the suite's own settle, so a closed pool or a missing credential is
the same SKIP as everywhere else. The person is
`tests/acceptance/test_live_browser.py`, standard library only (the
swarm-verify image has `python3` and no pip): a websocket client, an RFB 3.8
viewer and a GCS reader, written out. It runs beside the settle wait and is
told the settled state through a file. It reports one verdict per check.

**The scripted person opens the live view before anything else.** swarm-api
counts someone as watching from the viewer socket's join, and a hold nobody
watches ends after `human_unwatched_seconds` (60 s); the worker then parks
the task on `HUMAN_REQUIRED`, which only a person's "I'm ready" promotes. The
other-tenant probes (six API calls and a socket that may wait 20 s for its
refusal) used to run first, and the scheduled runs of 2026-10-15..19 failed
`returned` with the task PARKED at the 900 s settle (#1333). They now run
while the viewer's socket is open and the page is on screen. When the settle
runs out anyway, the group reads the task once more and the verdict names
its state and `park_reason` (`PARKED:HUMAN_REQUIRED`, or a provider park
with the instant it is eligible again), never just "did not finish".

**The page is served inside the agent's own pod.** Nothing this repository
owns serves HTML as HTML on the internet (see browser, above), so
the agent runs `python3 -m http.server` on `127.0.0.1:8765` over
`tests/acceptance/fixtures/` and opens `login.html` in the platform's
Chromium through `$SWARM_CDP_ENDPOINT`. The sidecar's Chromium shares the
pod's network namespace, so it reaches loopback; nothing leaves the pod.

**The password is made at run time, inside the viewer process, and never
leaves it.** It is not in this shell, an argv, the environment, a log or the
results file. It is typed as RFB key events, lower-case letters and digits
only, each its own unshifted keysym. The page has no `name` on the field and
cancels the submission, so nothing is requested with it. What the page shows
after the sign-in is 12 hex digits of its SHA-256. The agent reports that
code, which proves the typing reached the page without the agent ever
holding the value.

| check | asserts | why that and not less |
|---|---|---|
| hand-off | `GET /v1/tasks/{id}/live/session` shows the session `available` with a `human_wait` (Q2: the field, not a state) | a hand-off the console cannot see is a hand-off nobody answers |
| another tenant | as a member of another tenant (the identity's other membership from `GET /v1/tenants/mine`, or `SWARM_ACCEPTANCE_OTHER_TENANT`), the task, `live/session`, `live/ticket`, `live/control` and `live/audit` are each a 404; a real viewer ticket whose `tenant_id` is rewritten to that tenant is closed `4404` on the socket | LB-2 and invariant 9. A 403 would confirm the task exists, and a ticket that could be pointed at a tenant the caller also belongs to would select it |
| frames painted | the viewer opens `wss://.../v1/live/{session}/stream` with the ticket as its first message (never in the URL), takes control, clicks the field and types. In every frame that shows the page, the field's box -- found from the cyan marker 80 CSS px above it -- is entirely the mask's `#222222`, and no pixel of the field's own magenta is on screen. At least one such frame arrived while the person typed | LB-3: masked before anything leaves the pod. The marker shows the page is on screen, so a frame the mask blanked whole (a stale report, section 5.3) is not counted as a pass |
| recording painted | segments `rec-<13 digits>.live.webm` exist under the task's `attempts/<a>/live/`, nothing else under `live/` but `actions-*.live.jsonl`; decoded with `ffmpeg` at 5 fps, every recorded frame that shows the page has the same box painted (VP8 is lossy: within 24 per channel, 95% of the box) | LB-4 records the masked stream; a recorder that took frames from anywhere else would show it here |
| returned | after Return control, the task `SUCCEEDED`, its answer and its `session-code.txt` output carry the code only the typed password makes, and `human_handoff_requested`, `human_control_taken`, `human_control_returned` and `human_handoff_resolved` are among its events | control came back and the agent carried on from the signed-in page, rather than the task ending some other way |
| no password | the password is in no object under `tenants/<t>/tasks/<task>/`, read from the bucket itself (not through the API, which redacts on the way out), gzip members decompressed: the transcript (`logs/agent_stdout.log`), every other log, every checkpoint archive, every output and the recording. Nor in the task document, its events, its live audit and session, or its answer. Each of transcript, logs, checkpoints and outputs must have at least one object read, and an object that could not be read FAILS the check | section 5.4: the agent's CDP is paused while a person types and the fields are cleared before it resumes. An unread category is not a clean one |

**SKIP, not PASS:** a task the platform holds (closed pool, no credential);
a deployment that dispatches the profile with no live path, recognised by the
agent's own report of `swarm-handoff`'s refusal code `no_live_path`
(`enable_live_browser` or `LIVE_BROWSER_SIDECAR` off); an identity that is a
member of no tenant but smoke; and the recording's frames where there is no
`ffmpeg`. Any other refusal from `swarm-handoff` (`gate_unavailable`,
`no_live_session`, ...) is a FAIL: the path was on and did not work.

**The swarm-verify image carries no `ffmpeg`, so in the release the recording
check SKIPs after confirming the segments exist, and says so.** The frames
the viewer judges are the stream the relay records: the relay's recorder is
one more RFB client of the same gate (`swarm_live/recorder.py`). Decoding the
VP8 itself needs a decoder this image does not have, and adding one is a
question for the owner (its CVE surface is the reason the image has no Cloud
SDK either). A local run with `ffmpeg` on `PATH` judges them.

It needs a second `swarm-verify` execution of its own (`scripts/acceptance/
live-browser.sh`). Its one task peaks the group at 1. It ran in wave 2 of
`accept.yml` until 2026-10-14, when it failed 18 of 34 post-release runs. It
now runs on `accept.yml`'s schedule, every three hours, and reports to an
issue of its own. It goes back into wave 2 when
`scripts/accept-live-browser-streak.sh` says it passed five in a row
([ci.md, "live-browser runs on a schedule"](ci.md#live-browser-runs-on-a-schedule)).

### git-tools

The runner git tools' acceptance (#1225; docs/runner-git-tools.md, section 7,
lane G7): five checks, one per row of that lane. The shell half is
`scripts/acceptance/groups/git-tools.sh`: it submits the tasks, waits on them
with the suite's own settle, and saves each task's state, document,
`GET /v1/tasks/{id}/git-calls` audit and two artifacts under one directory.
The judge is `tests/acceptance/test_runner_git_tools.py`, standard library
only, which also writes the prompts, so what a task is asked and what is read
back are written once. Its offline tests are
`tests/unit/scripts/test_runner_git_tools_acceptance.py`.

**Platform facts come from swarm-api's audit, not from the agent.** A fetch's
resolved sha, a push's old and new sha and `rewrite`, a `pr_checks` read's
pull request and head: all are read from the audit rows. Two things only the
agent sees are read from its `git-answers.jsonl`. The first is a refusal the
WORKER answers before any request leaves: a push to a branch the task may not
push never reaches swarm-api, so it is never audited. The second is the paths
a rebase listed as conflicted. Each is cross-checked against the audit: no
push of that task ever reached the forge, and the branch it aimed at did not
move.

**Planting the conflict without a token.** The suite holds no GitHub
credential, and only `sandbox-sync.sh` writes to the sandbox. So
`sandbox-sync.sh` points the branch `acceptance/git-tools-base` at the parent
of the newest commit on main that ADDED `tests/acceptance/fixtures/git-tools/
planted.txt`. It makes no commit: the base is a commit main already has. The
branch is outside `swarm/`, so no task can push it.

The lane task clones that base and adds its own `planted.txt`, so its pull
request conflicts with main. The conflict lives in the lane's own commit,
which the worker wrote. A sandbox commit would carry a foreign author, which
the push's authorship check refuses.

| check | asserts | why that and not less |
|---|---|---|
| rebase | a `direct-pr` continuation of the lane's pull request fetches its branch (so swarm-api records the tip to lease), `git_rebase_onto main` lists `planted.txt` as conflicted, the agent resolves it, the audited push replaced exactly the fetched tip with `rewrite: true` (Q1 = A), `pr_checks` read the lane's pull request at the pushed head, and the lane ended `SUCCEEDED`. Its `env_report.py` artifact, names only, shows no forge credential: no name `env \| grep -i token` would find except `CLAUDE_CODE_OAUTH_TOKEN` (owner, 2026-10-13), no `GH_TOKEN`, `GITHUB_TOKEN` or `GIT_*` credential variable, no `GIT_CONFIG_KEY_<n>` naming a credential helper, extra header or `insteadOf`, and no value shaped like a GitHub token or private key | #1225's first acceptance item, end to end. The report prints names, never values, because the environment holds the Claude login. The agent cannot know the forge slot's value, so a value is judged by its shape |
| read_private | the reader reads an allowlisted repository's file at a branch, then at the sha that read resolved to, both audited `ok`. When that repository is the sandbox, the content must be this build's `planted.txt`. A repository on no list, and on no forge, is refused `repo_not_allowlisted` | the allowlist is the gate (Q3 = A). The refused control shows the check could have come out the other way |
| push to main | the reader's push to `main` is answered `protected_branch` or `not_this_tasks_branch`, swarm-api accepted no push of the reader's at all, and main's tip, fetched through the tools before and after, is the same | a refusal the agent was told about is not enough. The branch must not have moved |
| push to another task's branch | the same, aimed at the lane's branch. The branch may move only by the rebase lane's own audited pushes, because the two run at the same time | another task's branch is exactly what `not_this_tasks_branch` exists for |
| a failed push fails | an `integrate` contributor pushes its branch mid-task, then commits off it (`git reset --hard HEAD~1`), so its final push cannot fast-forward and is refused, never forced. The step ends `FAILED` with `publish_failed:` (G1), not `SUCCEEDED`. If the mid-task push never landed, the check FAILs saying the failure was never set up. If the final push succeeded, it FAILs saying a new way to fail a push is needed | #1225's lost final pushes were contributors that read `SUCCEEDED` with no branch |

The reader is `collect` with `carrier: branches`, so it MAY push its own
branch. A strategy that pushes nothing is refused `strategy_pushes_nothing`
before the branch rule is ever asked.

**On GitHub.** The lane's pull request carries `acceptance-git-tools: rebase`
in its body, and `github-verify.sh` judges it as its own kind. Its head must
contain main, it must change exactly `planted.txt` and `lane.txt`, and
`planted.txt` at its head must be main's line followed by the lane's.

**SKIP, not PASS:** a task the platform held (closed pool, no credential),
and `read_private` when no repository in the smoke tenant is
`readable_by_tasks`. Only a platform admin can set that, with
`PATCH /v1/repositories/{id}`, so the suite never sets it itself.

It runs in wave 2 of `accept.yml` (`scripts/acceptance/git-tools.sh`), with
a peak of five tasks. The lane runs first, because both later tasks need its
branch. In the worst case its two settles take the job's whole 30 minutes;
the claude-code tasks it runs take a few minutes each.

## Pull requests on the sandbox

The direct-pr and integrate checks open real pull requests on the sandbox,
never on this repository. A check closes its own when its run holds a token
for the sandbox. The release's run does not: it runs in the swarm-verify job,
which holds no GitHub credential, on purpose, and so cannot read its pull
requests back either: those assertions SKIP in the suite. The release's
acceptance job makes them after the suite, on the GitHub runner, with
`scripts/acceptance/github-verify.sh` -- the title is a fact and not a task
id, the body carries the task id, the diff stays under
`tests/acceptance/fixtures/` and removes the bug line, an integrate body lists
its merged branches and leaves none out -- and fails the job on any defect
([ci.md](ci.md#release-acceptance-runs-in-the-smoke-tenant-against-a-private-sandbox)).
The `collect` patch is applied in the suite to the swarm-verify image's own
copy of `calc.py`, the same commit `sandbox-sync.sh` put on the sandbox. Then
the job runs `scripts/acceptance/github-cleanup.sh` with the sandbox's own
token (the repository secret `SWARM_SANDBOX_GITHUB_TOKEN`; the job's
`GITHUB_TOKEN` reaches only this repository). It closes only an open pull request from a
`swarm/task_` branch whose every changed file lies under
`tests/acceptance/fixtures/`, deletes its head and the contributor branches its
body lists as merged, and deletes any leftover `swarm/task_` branch whose diff
is non-empty and lies entirely under that directory. Anything that touches
another path is left alone, whoever opened it. The sweep does not know which
run opened what, so a local run in progress at the same time can have its pull
request closed under it.
