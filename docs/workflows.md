# Workflows

Two different things share this name, and both are documented here:

* **Part 1 — task workflows**: multi-step agent DAGs submitted to the API.
* **Part 2 — development workflows**: the local loop, and the CI/CD pipeline in
  `.github/workflows/`.

---

# Part 1 — Task workflows (DAGs)

A workflow is a set of steps with dependencies. Each step becomes an ordinary
task, so everything true of a task is true of a step: it is admitted through the
same transaction, counts against the same pools, checkpoints the same way, and
costs nothing while it waits.

```bash
./scripts/api.sh POST /workflows '{
  "priority": 10,
  "on_step_failure": "fail_workflow",
  "steps": [
    {"step_id": "analyse",
     "runner_profile": "claude-code",
     "input": {"prompt": "Summarise the failing tests in this repo"}},

    {"step_id": "fix",
     "runner_profile": "claude-code",
     "depends_on": ["analyse"],
     "input_from": {"analyse": "summary.md"},
     "input": {"prompt": "Fix the failures described in summary.md"}},

    {"step_id": "verify",
     "runner_profile": "generic",
     "depends_on": ["fix"],
     "input_from": {"fix": "patch.diff"},
     "input": {"command": "pytest"}}
  ]
}'
```

## Step fields

| Field | Notes |
|---|---|
| `step_id` | unique within the workflow; `^[A-Za-z0-9][A-Za-z0-9_\-.]*$` |
| `runner_profile` | a **name** from the frozen catalogue — never an image or command |
| `input` | the step's own payload, bounded by `max_input_bytes`: its `prompt`, plus only the keys its profile declares (below). |
| `depends_on` | upstream `step_id`s, up to 50. |
| `input_from` | `{upstream_step: artifact_filename}` staged into this step's workspace. |
| `resource_class` | optional named class, no larger than the profile's own. |
| `timeout_seconds` | optional; may only shorten the profile's own default. |
| `when` | `{"step": upstream, "verdict_in": ["NOT_YET"]}`: run this step's agent only on those verdicts; see [review before publishing](#review-before-publishing-implement-review-fix-if-needed) |
| `builds_on` | an upstream `step_id` whose pushed branch this step's checkout starts from, instead of `repository_ref` |
| `allow_empty_diff` | `true`: an empty diff is this step's result, not its failure; see [an empty diff can succeed](#an-empty-diff-can-succeed-allow_empty_diff) |

`max_workflow_steps` (default 50) bounds the whole thing.

## What `input` may carry

A runner reads its task's `input`, and an input means something only to the
runner that reads it: `input.model` was passed as `--model` by the CLI runners
(until #226, when the model became the Job's `MODEL`, set in Terraform; see
[agent-output.md](agent-output.md)), and `input.quota_exhausted` makes the mock
simulate a rate limit. So what a
caller may send is declared per profile in the frozen catalogue,
`RunnerProfile.inputs` (contract request 25, accepted by the owner on #142 on
2026-09-25), and the API holds every caller to it: `POST /v1/tasks`, the batch,
and each step here.

| the profile | what `input` may carry |
|---|---|
| `mock` | `prompt`, and its declared test knobs, in the table below |
| `browser` | `prompt`, and what the browser runner reads: a `url`, `actions` in eight fixed shapes, timeouts, the viewport, in the table below |
| `generic` | `prompt`, and a **required** `command` from the runner's own catalogue, plus the paths, target, directory and limits in the table below |
| `claude-code`, `codex` | `prompt`, and `issue`, in the tables below |

What each declaring profile takes, with each key's kind and bounds. These
tables are generated from the catalogue, not written: a bound stated anywhere
else in this file would be a copy nothing compares, so none is.

<!-- runner-inputs:mock generated from RUNNER_PROFILES["mock"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the mock runner does with it |
|---|---|---|
| `sleep_seconds` | number 0..3600 | how long the run sleeps, in total |
| `cpu_burn_seconds` | number 0..3600 | how long it burns CPU, in total |
| `steps` | integer 1..1000 | how many progress files, and checkpoints, it writes |
| `fail` | boolean | fail on purpose, after the steps |
| `fail_message` | string | the error a failure reports |
| `exit_code` | integer 1..255 except 77, 78, 143 | the exit code a failure uses |
| `artifact_text` | string | what the output artifact holds |
| `artifact_name` | filename | the output artifact's file name |
| `quota_exhausted` | boolean | park the first attempt on a simulated provider rate limit; the next one runs |
| `retry_after_seconds` | integer 1..3600 | the retry-after that simulated rate limit reports |
| `artifact_before_park` | boolean | write the output artifact before the simulated rate limit parks the first attempt |
<!-- /runner-inputs:mock -->

<!-- runner-inputs:browser generated from RUNNER_PROFILES["browser"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the browser runner does with it |
|---|---|---|
| `url` | url | opened first, before any action |
| `actions` | list of 0..200, each object, `type` one of goto \| click \| fill \| press \| wait_for \| wait \| screenshot \| extract | run in order, after `url` |
| `actions` `goto` | `url` (required) url, `wait_until` string, one of load \| domcontentloaded \| networkidle \| commit | `url`: the page to open; `wait_until`: when the load counts as done; default load |
| `actions` `click` | `selector` (required) string 0..4096 | `selector`: the element to click |
| `actions` `fill` | `selector` (required) string 0..4096, `text` string 0..4096 | `selector`: the field to fill; `text`: what to type into it; default empty |
| `actions` `press` | `selector` (required) string 0..4096, `key` string 0..4096 | `selector`: the element to press a key in; `key`: the key; default Enter |
| `actions` `wait_for` | `selector` (required) string 0..4096, `timeout_ms` integer 1..300000 | `selector`: the element to wait for; `timeout_ms`: how long to wait; default the task's timeout_ms |
| `actions` `wait` | `seconds` number 0..60 | `seconds`: how long to pause; default 1 |
| `actions` `screenshot` | `name` filename, `full_page` boolean | `name`: the artifact's file name; default by position; `full_page`: the whole page, not the viewport; default true |
| `actions` `extract` | `selector` string 0..4096, `name` filename | `selector`: the element whose text is kept; default body; `name`: the artifact's file name; default by position |
| `timeout_ms` | integer 1..300000 | how long any one action may take; default 30000 |
| `launch_timeout_ms` | integer 1..180000 | how long Chromium may take to start; default 60000 |
| `viewport_width` | integer 320..3840 | pixels; default 1280 |
| `viewport_height` | integer 240..2160 | pixels; default 900 |
| `user_agent` | header 0..512 | the User-Agent sent; default Chromium's |
| `extract_text` | boolean | keep the final page's text as page.txt; default true |
| `screenshot` | boolean | keep a final full-page screenshot; default true |
<!-- /runner-inputs:browser -->

<!-- runner-inputs:generic generated from RUNNER_PROFILES["generic"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the generic runner does with it |
|---|---|---|
| `command` | string, one of make \| npm-build \| npm-ci \| npm-test \| pytest \| uv-sync, required | the platform catalogue entry to run; the platform owns its argv |
| `paths` | list of 0..32, each argument | pytest only: what to run; default everything |
| `target` | argument | make only: the target; default all |
| `working_directory` | argument | a directory inside the workspace to run in; default the workspace |
| `timeout_seconds` | number 1..3600 | lowers the command's wall clock |
| `grace_seconds` | number 1..20 | lowers the wait between SIGTERM and SIGKILL |
| `max_stdout_bytes` | integer 1..33554432 | lowers the stdout kept |
| `max_stderr_bytes` | integer 1..8388608 | lowers the stderr kept |
<!-- /runner-inputs:generic -->

What `claude-code` and `codex` declare (contract request 28, #265):

<!-- runner-inputs:claude-code generated from RUNNER_PROFILES["claude-code"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the claude-code runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:claude-code -->

<!-- runner-inputs:codex generated from RUNNER_PROFILES["codex"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the codex runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:codex -->

`claude-code-review` is `claude-code` under its own Job and service account,
for the merge chain's review step (contract request 36, #295). It is
disabled until #295 is enabled, and declares what `claude-code` does:

<!-- runner-inputs:claude-code-review generated from RUNNER_PROFILES["claude-code-review"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the claude-code-review runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:claude-code-review -->

`indexer` is `claude-code` on `agent-runtime-indexer`, the image that carries
the repository index's toolchain (contract request 48, #625). swarm-api runs
index runs on it; it declares what `claude-code` does:

<!-- runner-inputs:indexer generated from RUNNER_PROFILES["indexer"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the indexer runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:indexer -->

`issue` points a step at a GitHub issue, so its prompt need not restate one.
It names an issue in the task's own `repository_url` (a workflow's, for a
step), and a submission that sends it without a repository is refused with
422 `invalid_input`: there would be nothing to fetch it from. The worker
fetches the issue's title, body and comments read-only, with the tenant's
forge credential, after the clone and the credentials and before the agent
starts. It writes them to `issue.md` in the work directory, beside the
checkout, where no diff and no upload reaches it, and names that file in the
prompt by absolute path. The credential stays in the worker's memory, as it
does for the clone (#219), and is never sent to a host other than the
repository's own. The issue's text is scrubbed of every secret the attempt
holds, and it is data for the agent: nothing in it chooses anything the
platform does (invariant 10). A fetch that fails -- no such issue, a pull
request's number, a repository the credential cannot read, a forge that
cannot be reached -- fails the attempt with `INPUTS_UNAVAILABLE` and the
agent never starts. A step that asks for an issue cannot also stage an
`input_from` file named `issue.md`.

**Every step's spec is signed at submission, and verified before it runs**
(contract request 34, #342). A tenant's agents can write any task document
of their tenant -- Firestore has no document-level IAM -- so one step could
rewrite a parked later step's prompt, its `input_from`, its dispatch role or
its repository. swarm-api signs each step's canonical spec
(`swarm_common.specsign`) with a Cloud KMS key only its own account may use,
after `input_from` and `expected_outputs` are written; the worker verifies it
immediately after it fetches the task, before the checkpoint restore, the
clone, the staged inputs, the credentials or the issue. A mismatch ends the
task FAILED with `SPEC_SIGNATURE_INVALID`, never retried. What the signature
does NOT cover is every field a later writer changes (`state`,
`attempt_count`, `result_summary`, `latest_checkpoint` and the rest) and the
restored workspace; contract request 34's threat model states those
residuals.

Every declared number has a floor and a ceiling, and an integer's bounds lie
inside the signed 64-bit range; the catalogue refuses a declaration without
them. Python reads a JSON integer at any length and Firestore stores 64 bits,
so an unbounded number passed every check and failed at the task write with a
500. The API also refuses, with 422, an integer anywhere in an input or a
task's metadata that Firestore could not store, which is the only number check
a task's metadata gets.

A key the profile does not declare, or a declared one outside its bounds, is
refused with 422 `invalid_input`. The detail names the key (`key`, and every
refused key in `keys`), the bound for a declared key (`expected`), what the
profile does declare (`declared`) and, on a workflow, the step (`step_id`).
Nothing is created: one refused step refuses the workflow, and one refused task
refuses its batch. `NaN` and `Infinity` are refused as numbers; Python reads
them from JSON, and every comparison with `NaN` is false, so a bound alone
would let them through. `swarm_profiles` and `swarm profiles` list each
profile's inputs.

`browser` and `generic` declare every key their runners read (contract
request 32, #218, accepted by the owner on 2026-09-29). Until then both were
bounded by size alone, so a `generic` task with no `command`, an action with
no `selector`, or a browser `goto` to a private address was admitted, leased
and started, and failed inside the pod. Each is a 422 at the door now.

* A browser `url`, and every `goto` action's, must be http or https, carry no
  `user:password@`, and name a public host: the metadata server, private and
  cluster addresses, `.internal`, `.local`, `.localhost` and `.svc` names, a
  single-label host and any host that is not plain ASCII letters, digits, dots
  and hyphens are refused (`url_refusal` in the frozen catalogue). That check
  is **not** the SSRF control -- it sees the URL typed, never a redirect, a
  page's subresources or what a name resolves to in the pod. The worker's
  NetworkPolicy is the control.
* A browser task still needs `url` or at least one action. Neither is required
  on its own, so a task with neither is refused by the runner, not the API.
* A `generic` task's four limits may only lower the platform's own: each
  declared ceiling is what the worker exports by default.

`{"quota_exhausted": true}` parks a mock step ONCE: the task's first attempt,
and no other. The attempt is counted by the task's own `attempt_count`, which
admission increments in the lease's own transaction and the worker hands to
the runner, so the bound holds even when the park's checkpoint fails to
upload; the attempt after the park runs to the end, from that checkpoint when
there is one. It parked on every attempt until 2026-09-25, and a park does not
spend an attempt, so such a task never ended. Its first bound was a count in
the mock's own working directory, which only the checkpoint carried forward,
so a park whose upload failed was followed by another (the review of #213).

`{"quota_exhausted": true, "artifact_before_park": true}` moves the mock's
artifact (`artifact_name`, `artifact_text`) to BEFORE that park: the parking
attempt writes it and then parks, and the attempt after the park writes
nothing under that name. Without `quota_exhausted` the flag changes nothing.
It exists to prove #166's carry live (contract request 56, accepted by the
owner 2026-10-08): a file an agent wrote before a quota park reaches the
attempt that succeeds BY REFERENCE, and before this input the mock parked
before writing anything, so no live run could show it. Because the finishing
attempt never rewrites the file, the one the task ends with can only have
arrived by the carry. The recipe is a two-step workflow:

```json
{
  "steps": [
    {"step_id": "a",
     "runner_profile": "mock",
     "input": {"prompt": "write notes.md, then meet a rate limit",
               "quota_exhausted": true, "retry_after_seconds": 60,
               "artifact_before_park": true,
               "artifact_name": "notes.md", "artifact_text": "written before the park\n"}},

    {"step_id": "b",
     "runner_profile": "mock",
     "depends_on": ["a"],
     "input_from": {"a": "notes.md"},
     "input": {"prompt": "stage notes.md"}}
  ]
}
```

Expect A to park once and finish on its second attempt, with `notes.md` in
its `result_summary.artifacts` carrying `carried_from` = A's FIRST attempt
and a `uri` under that attempt's prefix (the parked attempt's uploads,
recorded by `ControlPlane._record_parked_uploads` and read back through
`ControlPlane.parked_uploads`), and no `notes.md` object under the second
attempt's prefix. B then stages `notes.md` from that first attempt's object:
its `result_summary.staged_inputs` names `notes.md` with a `uri` under A's
`attempts/<first attempt>/`. `tests/unit/worker/test_parked_uploads_carry.py`
holds the same run offline.

## Artifacts pass by reference

**An artifact a later step can stage by its name is a file the upstream step
wrote into `$SWARM_ARTIFACTS_DIR`, and nothing else is.** That directory sits
outside the agent's working directory and outside the repository checkout, so a
file written anywhere else is never staged by a later step, however exactly its
name matches. (Since #184's owner decision of 2026-09-26, a claude-code or codex
task with NO repository also uploads what its agent created in its working
folder, but as `workdir/<path>`, so a working-folder `scan-01.md` is
`workdir/scan-01.md` and still does not satisfy a step that stages
`scan-01.md`; see [agent-output.md](agent-output.md).) Until #149 this failed late and cost money:
the upstream step SUCCEEDED, and only the dependant failed, at staging, with
`upstream task … did not produce an artifact named 'scan-01.md'`. By then every
upstream step had spent its compute and its provider quota. That is what
happened to workflow `wf_73946ff4a32a4f99b3a4` on 2026-09-25. Its eight scan
steps were prompted "write it to scan-01.md", wrote into their working
directories and succeeded. All four merges then failed, and seventeen steps were
cancelled.

**So the platform tells the upstream agent, catches its likeliest wrong guess,
and fails the upstream attempt when neither worked.** Three layers, because the
first alone was measured not to be enough.

1. **Instructions.** At submission the API inverts every `input_from` edge. It
   records on each upstream step's task the filenames its dependants will
   stage, as `metadata.expected_outputs`, in the same write that creates the
   task. The worker passes that list to the runner. Only the `claude-code` and
   `codex` runners act on it: every prompt they give an agent already ends
   with one line naming the **absolute** path of `$SWARM_ARTIFACTS_DIR` (#184,
   2026-09-26), and they add the names after it, with each file's full path
   and the statement that files written anywhere else, the repository
   included, do not reach later steps. The `generic`, `mock` and `browser` runners receive the
   list in `input.json` and change nothing. A name the platform writes itself
   is never in the instructions: not the worker's `swarm-work.patch`, which is
   written after the agent exits, and not the runner's own
   `<runner>.stdout.log`, `<runner>.stderr.log` or transcript, which it writes
   while the agent runs.
2. **`./artifacts` is the artifacts directory.** On the third run,
   `wf_06a3a949d2c242c3b0e9` (2026-09-25), every prompt named
   `$SWARM_ARTIFACTS_DIR` and told the agent to `echo` it. scan-02 echoed
   `/workspace/att_…/artifacts`, which is right, then wrote
   `/workspace/att_…/work/artifacts/scan-02.md`, and reported that it had
   written the file to `$SWARM_ARTIFACTS_DIR`. The artifacts directory is the
   working directory's sibling, so `./artifacts` is the natural wrong guess. So
   before every agent starts, workflow step or not, the worker makes
   `work/artifacts` a symlink to the artifacts directory, and a file written to
   `./artifacts/<name>` lands in the directory that is uploaded. The link is
   never checkpointed: it points outside `work/`, which a restore refuses, and
   it names this attempt's directory, so a resumed attempt makes its own. If
   `work/artifacts` already exists (a declared input staged as `artifacts/…`,
   or a directory a restored checkpoint brought back), it is left alone and
   one WARNING says so. Files written under it are then not uploaded, exactly
   as before. A step with a repository starts its agent in the checkout
   (#226, 2026-09-26), so the worker makes the same link at
   `work/repo/artifacts` as well, hidden from git with the clone's
   `.git/info/exclude` so it never reaches the agent's diff; see
   [agent-output.md](agent-output.md) for when it is not made.
3. **A missing expected output fails the attempt** (owner decision on #149,
   2026-09-25), **retryably only when a retry can help** (#165, 2026-09-28).
   If an attempt's runner finishes cleanly and one of its expected outputs was
   not uploaded, the attempt FAILS with the missing names as its cause, in
   `last_error`, in `result_summary.expected_outputs_missing` (and, name by
   name, `expected_outputs_missing_causes`) and in one log line. The worker
   records WHY each file it did not upload was skipped: each
   `result_summary.artifacts_skipped` entry is `{name, cause}` (a summary
   from before #165 holds bare names, and every reader accepts both; the
   API's artifact listing serves the same `{name, cause}` entries, a bare
   name as `cause: null`), and the cause decides:
   * **not retried** -- `cap` (the artifact byte or file cap), `refused` (a
     link, not a regular file, or a name no object can carry), and for
     `swarm-work.patch` `empty_diff`, `patch_omitted` (over the patch cap) or
     `no_base`. The next attempt would write the same file into the same cap,
     against the same repository, so the task ends FAILED at once, with
     `end_cause: outputs_missing`, whatever attempts are left;
   * **retried** -- a file never written (`not_written`), or one whose upload
     raised (`upload_error`). The task goes back to READY, with a `retrying`
     event, and runs again as a new attempt while it has attempts left, and
     ends FAILED once `max_attempts` is spent.

   Either way a FAILED parent cancels its dependants as any failed parent
   does, and a requested cancel still ends it CANCELLED. **A dependant
   therefore never starts on a parent that did not write what it promised.**
   A runner that fails, times out or is stopped keeps its own cause, and the
   missing names are recorded next to it. A dependant that tries to stage a
   skipped file is told the cause the parent recorded, not an assumed cap.

None of this makes an agent write the file; the third layer makes it cost at
most `max_attempts` upstream attempts instead of the rest of the workflow. A
prompt that names the file and the directory as well does no harm.

**A retry starts with an empty artifacts directory.** It resumes `work/` from
the checkpoint the failed attempt took as it ended, and only `work/` is
checkpointed. So the retry must write every expected output again, not only the
one that was missing. The agent is given the same instructions, which list every
name.

**Nothing is published until the upload manifest has passed the check**
(#165, owner decision 2026-09-28). The worker harvests the repository and
uploads the artifacts first; the push and the pull request are made only
after the missing-output check has found every expected output in the
manifest. An attempt that is then failed or retried -- the last attempt
included -- has pushed nothing and opened no pull request; its
`result_summary.git.publish_reason` says why. Before this the publish ran
first, from a listing of the directory, so a file present and then not
uploaded (the cap, an upload error) left a branch and a pull request behind an
attempt the check then failed, and the last attempt published whatever it
had. A step with `carrier: branches` still pushes its committed work at each
checkpoint taken after its runner stopped (the carrier, not the publish; see
[design/dispatch-and-integration.md](design/dispatch-and-integration.md)
4.3), so its branch can hold the work of an attempt that then failed.

**`metadata.expected_outputs` belongs to the service.** A caller cannot set it:
`POST /v1/tasks`, `POST /v1/tasks/batch`, or a workflow whose own `metadata`
carries it, is refused with 422 `invalid_dispatch` and
`detail.reserved_metadata_keys: ["expected_outputs"]`, and nothing is created.
This is the reservation `metadata.dispatch` already has. Accepted, the key
would reach the agent as "later steps of this workflow need these files", in
the platform's voice, on a task no step stages from. For the same reason the
worker drops a caller's own `input.expected_outputs` before the runner sees it,
and logs that it did.

What this deliberately does not do:

* **It does not copy a file out of the working directory.** The worker does not
  go looking for a same-named file in the working directory and upload it. That
  was option (c) on #149, and the owner rejected it. The link is not a copy: a
  file written through it is written into the artifacts directory in the first
  place. A file left anywhere else, in the repository or elsewhere in the
  working directory, is never staged by a later step under its own name. A
  task with no repository does upload what its agent created there (#184),
  for a reader of the Artifacts tab, under `workdir/<path>`, which is not the
  name a dependant stages.
* **It does not carry the artifacts directory across a retry.** Only `work/`
  is checkpointed, and a resumed attempt starts with an empty artifacts
  directory. A PARK is different (#166, owner decision 2026-09-28): a parked
  attempt uploads what its agent wrote, and the attempt that finishes lists
  those uploads it did not replace in its own `result_summary.artifacts`, BY
  REFERENCE -- `{name, bytes, uri: <the parked attempt's object>,
  carried_from: <its attempt id>}`, nothing downloaded again. They count as
  present for the check, a dependant stages them from the parked attempt's
  object, and a name the finishing attempt uploaded itself wins. An attempt
  that failed retryably carries nothing forward.
* **It does not tell a retry which file it left out.** The retried agent gets
  the same prompt and the same list, which names every expected output.

`input_from` stages an upstream step's artifact into the downstream step's
workspace. The file travels through **GCS**, never inline through Firestore —
Firestore has a 1 MiB document limit, and an agent's output is routinely larger.
The tenant's GCS prefix applies, so a workflow cannot stage another tenant's
artifact even by naming it.

It lands in `work/<filename>`. A step with no repository starts its agent in
`work/`, so "read scan-01.md" in its prompt finds the file. A step with a
repository starts its agent in the checkout, `work/repo` (owner decision of
2026-09-26, #226, so Claude Code loads the repository's own `CLAUDE.md`), where
that relative name would not resolve, so the prompt line the worker adds names
every staged file by its absolute path. Staging into the checkout instead would
put the file in the agent's diff.

The filename is both the artifact's name in the upstream step and the path it
lands at downstream. So within one step each parent must stage a **distinct**
relative filename, with no empty, `.` or `..` segment, and the API refuses any
other shape at submission (HTTP 422 `invalid_dag`, naming the step, the parents
and the file) rather than letting the worker refuse it after every parent has run.

## An agent asks the owner: `questions.json`

**A remote agent that meets a decision that is the owner's asks, in a file,
instead of guessing.** Owner decision, 2026-10-05. Until then the agent had one
place for such a question: its answer. It wrote the question into prose and
shipped `part of #N` (I310, W532), and a workflow row cuts that answer to 500
characters, so the question could be cut off with the rest. Now every
claude-code and codex prompt says, in one sentence after the line naming
`$SWARM_ARTIFACTS_DIR`, that the agent may write `questions.json` there
(`expected_outputs.questions_line`). Both runners get it, because both are
told about the folder in the same place.

The file is a JSON list:

```json
[
  {
    "question": "Should the retry budget be per task or per workflow?",
    "options": [
      {"label": "per task", "description": "Each step keeps its own three attempts."},
      {"label": "per workflow", "description": "One budget shared by every step."}
    ],
    "recommended": "per task",
    "context": "CONTRACT.md fixes three attempts per task; a shared budget changes it."
  }
]
```

**The worker checks the shape and the size, and the task never depends on
it** (`agent_worker/questions.py`). The file is at most 64 KiB and holds at most 20
questions. Each question has exactly these four keys, and `recommended` and
`context` may be null or left out. `options` holds 1 to 10 `{label,
description}` objects with distinct labels, and `recommended` is one of those
labels or null. Every text has a length bound. The file is uploaded like any
other artifact, and it comes first in the 500-file cap, so the cap never drops
it. The attempt's `result_summary` then says one of three things:

| the file | `result_summary` |
|---|---|
| valid, and uploaded | `questions: N` |
| any other shape, too large, not JSON, a link, or not uploaded | `questions: 0`, `questions_rejected: <why>` |
| absent | `questions: 0` |

A rejected file is still an ordinary artifact, so the operator can read what
the agent wrote, but it is not counted, so nothing shows it as questions. It is
a WARNING line and a summary field, not a failed attempt: the task finishes
exactly as it would have without the file. The reason names a position and a
key (``question 2: `recommended` is not one of its options' labels``) and never
quotes the file.

**The bridge shows them to whoever reads the result.** When `questions` is
above 0, `swarm_result` and the follow outcome read the file back through the
artifacts route. That route looks it up by name in the task's own manifest and
redacts it, and the bridge returns it as `questions`, a list. A task that asked
nothing has `questions: []` and costs no extra read. A counted file that cannot
be read has `[]` with `questions_unavailable_because`, so "asked nothing" and
"asked something nobody could read" never look the same. The final progress
line of a `format: "progress"` row says `? N question(s) for the owner`, and a
`format: "lines"` row gets that as a line of its own. The `sc:step` result
carries `questions` with `""` for a missing text, keeping its no-null rule, and
`plugin/agents/step.md` tells the row to put them in its result as given.

**Data for the operator, never a channel to the platform.** Nothing executes,
answers or acts on the file. No state, retry, dispatch, merge or verdict reads
it, and no part of the platform takes an instruction from it. The agent asks,
and a person decides. If the answer changes the work, that is a new task,
dispatched by a person.

## Dependencies and state

A step with unmet dependencies is `PARKED(DEPENDENCY_INCOMPLETE)`. Parked costs
nothing (invariant 1), so a hundred-step workflow with a two-hour first step
occupies exactly one slot, not a hundred.

The scheduler's dependency sweep promotes steps whose upstreams have succeeded,
bounded by `dependency_sweep_size` per run so one enormous workflow cannot
monopolise a pass.

**Cycles are rejected at submission**, with the exact cycle named. A cyclic DAG
would otherwise sit in `DEPENDENCY_INCOMPLETE` forever — holding no capacity, but
never completing and never erroring, which is the worst kind of failure because
nothing alerts on it.

## The base pin: a downstream step starts where its parents started

**Why it exists.** On 2026-10-02 workflow `wf_b9b337e107494c10a416`
(implement, review, fix under `integrate`) lost a whole implementation. The
implement step cloned `main` at `b2c1094` and staged a 246 KB patch. The fix
step started later and cloned the tip `main` had moved to (`1402814`). The
patch did not apply, so the agent changed nothing, and the forge answered "No
commits between". The step and the workflow both read SUCCEEDED. Every step
was handed `repository_ref` as a branch name, and each worker cloned whatever
that branch pointed at when the worker started.

**What the worker does now.** A workflow step that has upstream dependencies
and does not start from an upstream branch clones the commit its parents
cloned, not the branch tip. Not starting from an upstream branch means no
`builds_on`, no `continues`, no parent branch carried under
`carrier: branches`, and no `single-pr` `pr_role`. The worker reads each
parent's `result_summary.git.base`, through the same tenant-checked read
staging uses.

| the parents' recorded bases | the clone | `result_summary.git.base_pin` |
|---|---|---|
| every parent that recorded one names the same commit | that commit | `{"pinned": true, "sha": ..., "from": [parent task ids]}` |
| they name different commits | the branch tip, as before | `{"pinned": false, "reason": "parents_disagree"}` |
| none recorded one | the branch tip, as before | `{"pinned": false, "reason": "no_upstream_base"}` |
| the commit cannot be fetched (a force-push removed it) | the branch tip, as before | `{"pinned": false, "reason": "fetch_failed"}` |

Each fallback also logs a line naming the parents and their bases. A root
step and a task outside a workflow read nothing and record no `base_pin`. A
step that starts from an upstream branch already has its upstream's work and
records none either. Neither does an attempt resumed from a checkpoint,
because its clone is not made again. The commit is fetched by sha, which
GitHub serves. If a server refuses a fetch by sha, the worker fetches the
branch's full history and checks the commit out of it.

**What it does not change.** The pin changes only the commit a step starts
from. The integrator still opens its pull request against `repository_ref`,
and it still merges its contributors' branches exactly as before. Under
`integrate`, those branches are what reach the pull request. The pin is what
keeps a staged *patch* applicable to the tree the fix step starts from. Any
step whose prompt says "apply the implementer's patch" depends on it.
`builds_on` is still the recommended way for a review or fix step to
receive an implementation (see the next section). It starts the step from
the implementer's branch, so there is no patch to apply in the first place.

**A pull-request step that published nothing is FAILED.** The second guard
from the same incident. A step whose job is to open a pull request is a
`direct-pr` step, the `integrate` integrator, or a `single-pr` author. If
such a step ends with no commit beyond its base, it ends FAILED. It ends
FAILED too if the forge refuses its pull request, for example "No commits
between". Its `last_error` begins `published_nothing:` and quotes the
refusal, masked. It is not retried, because another attempt would clone the
same base, run the same prompt and meet the same forge. The workflow then
reads FAILED through the ordinary rollup, and `on_step_failure` acts on it
as on any failed step. A step that opens no pull request still SUCCEEDS when
it changes nothing, because for a contributor, a review or a `collect` step
that is a correct result. The guard also exempts a step whose verdict gate
kept its agent from running, and a step that declared `allow_empty_diff`, or
wrote `verification.md`, and changed nothing ([below](#an-empty-diff-can-succeed-allow_empty_diff)). A forge that cannot be reached at all is not a
refusal, and that step reads as it did before. Until contract request 46 is
decided, the end cause is `outputs_missing`, the closest existing one.

**A pull-request step whose push failed is FAILED (#872).** Measured
2026-10-08 on `task_6a0c9afdbaea449eb53d`: the agent left 18 files
uncommitted and exited 0, the worker committed them, and the push failed with
`push failed with exit 1: error: failed to push some refs`. The step and its
workflow read SUCCEEDED, no branch or pull request existed, and the work was
found by chance in `swarm-work.patch` six hours later. Now, when git fails the
publish of a step whose job is to open a pull request (the same three kinds as
above) -- the push, or the commit, fold or authorship check before it -- the
publish records `result_summary.git.publish_failed: true` and the step ends
FAILED. Its `last_error` begins `publish_failed:`, quotes git's error, and
names the uploaded `swarm-work.patch` uri, so the work can be recovered with
`swarm_apply`; with no patch uploaded it says why and that the work is in the
attempt's final checkpoint. It is not retried: a refused push meets the same
refusal on the same branch. The end cause is `outputs_missing`, as for
`published_nothing:`, because the frozen contract has no publish-failed cause
and no new `TaskState` was added for it. A step that publishes nothing by
design -- a review, an `integrate` contributor, a `collect` step, a step with
no repository -- is unaffected.

## An empty diff can succeed: `allow_empty_diff`

**Why it exists.** A step asked to "fix it if it is broken" may find nothing
broken. Until 2026-10-05 such a step failed: a later step staged its
`swarm-work.patch`, the harvest wrote none because the diff was empty, and the
attempt ended FAILED for the missing output (`empty_diff`, not retried). A
pull-request step ended FAILED `published_nothing:`. Both are right for a step
that was meant to change something, and wrong for one whose correct answer was
"nothing to change". The owner's decision (lane review P2) is that the step
says which it is.

```json
{"step_id": "implement", "runner_profile": "claude-code",
 "allow_empty_diff": true,
 "input": {"prompt": "Fix the flaky test if it is still flaky; run it either way."}}
```

| the step's diff | without the flag | with `allow_empty_diff: true` |
|---|---|---|
| empty | FAILED, `empty_diff` (or `published_nothing:` for a pull-request step) | SUCCEEDED, `result_summary.no_change: true` |
| not empty | unchanged | unchanged |
| over the patch cap (`patch_omitted`), no clone base | FAILED | FAILED: the flag covers an empty diff and nothing else |

* **Its other artifacts are kept.** A `no_change` step's uploads (the test
  report, the check it ran) are uploaded as from any clean attempt, and a later
  step can stage them. Only `swarm-work.patch` and, for a pull-request step,
  `pr-title.txt` stop being owed; every other expected output still is, and a
  missing one still fails the step.
* **"Empty" is the harvest's measurement**: a base to diff against, no commit
  beyond it, no uncommitted path, and no patch written because the diff was
  empty. An integrator still owed its contributors' merge is never `no_change`:
  its deliverable is their work.
* **The flag is stored where the worker reads it**, `metadata.dispatch.
  allow_empty_diff`, inside the block the spec signature covers, and served in
  the task's `dispatch`. It must be a JSON boolean: `"yes"` is refused with 422.

**Or the agent says why: `verification.md` (Proposal J, 2026-10-08).** A
step's author cannot always know in advance that the work may already be done:
an issue run whose issue was fixed on main by another lane is the common case.
So an agent that changes nothing AND writes `$SWARM_ARTIFACTS_DIR/
verification.md` gets the same result the flag gives -- SUCCEEDED,
`result_summary.no_change: true` -- whatever the step's `allow_empty_diff`
says, and the file's text (masked, cut at 4,000 bytes) is kept as
`result_summary.no_change_reason`. The file is uploaded whole like any other
artifact. The evidence is the price: with no change and no `verification.md`
(or a blank one), the `empty_diff` or `published_nothing:` failure above is
unchanged. "No change" is the same harvest measurement as for the flag, and an
integrator still owed its contributors' merge is still never `no_change`.

### The steps that needed the change are SKIPPED

A step that needs the change a `no_change` step did not make ends SUCCEEDED
with `result_summary.skipped: {"reason": "nothing to change", "upstream":
[task ids]}`. It starts no agent, clones nothing (the branch it would start
from was never pushed), stages nothing and publishes nothing. A step needs an
upstream's change when it:

* stages that upstream's `swarm-work.patch` through `input_from`;
* starts from that upstream's branch: `builds_on`, a `single-pr` reader or
  amender's author, a merge step's pull request;
* integrates it, and every step it integrates changed nothing or was skipped.
  An integrator with at least one contributor that changed something runs, and
  merges only those; the others are listed under `git.integrated.no_change`,
  not as `missing`. A contributor that SUCCEEDED having published nothing (a
  read-only review) is left out the same way, under
  `git.integrated.read_only`; see the review shape below.

**A skip is transitive**: a step that stages anything from a SKIPPED step is
skipped too, because a skipped step wrote nothing. A step that stages only the
verification files of a `no_change` step still runs: those files exist. In the
review shape below, an implementer with nothing to change skips the review
(it stages the patch and builds on the implementer) and the fix (it builds on
the implementer), and the workflow SUCCEEDS with no pull request.

**Why SKIPPED is a SUCCEEDED task and not a state of its own.** The frozen
`TaskState` has no SKIPPED, and the frozen transitions forbid PARKED ->
SUCCEEDED, so the scheduler cannot end a parked step as skipped. The skip is
made the way the verdict gate's no-agent ending is (#264): the dependency
sweep promotes the step, admission leases it, and the worker reads its
upstreams' `result_summary` through the tenant-checked upstream read, before
any checkpoint restore, clone, staging or credential, and ends it. The step
holds a lease for that read and that write, and counts against the pools like
any other (invariants 1 and 3); no agent, provider quota or clone is spent.
Not CANCELLED: a cancel is a stop, and under `fail_workflow` a cancelled
dependant reads as the cascade of a fault there was not. A real `SKIPPED`
state, which would let the scheduler skip a step without leasing it, is a
frozen-contract change and has not been made.

**The workflow derivation counts a skipped step as a success.** It is a
SUCCEEDED task, so it ranks as one ([workflow state](#workflow-state)), and the
rollup names it in `rollup.skipped_steps`, so a reader can tell it from a step
that ran. `rollup.counts` keeps counting it under `SUCCEEDED`.

## Review before publishing: implement, review, fix-if-needed

**A step's patch can be reviewed inside the workflow, and fixed, before
anything opens a pull request** (#264). Until this existed every SwarmCloud PR
was reviewed by an agent on the operator's machine after it had opened. On
2026-09-27/28 two of seven came back NOT YET (#247 fixed the wrong layer, #256
left an ssh-user bypass), and each then needed a local fixer.

```bash
./scripts/api.sh POST /workflows '{
  "strategy": "integrate",
  "repository_url": "https://github.com/acme/widgets.git",
  "steps": [
    {"step_id": "implement",
     "runner_profile": "claude-code",
     "input": {"prompt": "Implement the change described in the issue."}},

    {"step_id": "review",
     "runner_profile": "claude-code",
     "depends_on": ["implement"],
     "builds_on": "implement",
     "input_from": {"implement": "swarm-work.patch"},
     "input": {"prompt": "Review swarm-work.patch against the repository. Do not edit files. Write verdict.json: {\"verdict\": \"MERGE\" or \"NOT_YET\", \"findings\": [\"one blocker per entry\"]}."}},

    {"step_id": "fix",
     "runner_profile": "claude-code",
     "depends_on": ["review"],
     "builds_on": "implement",
     "input_from": {"review": "verdict.json"},
     "when": {"step": "review", "verdict_in": ["NOT_YET"]},
     "input": {"prompt": "Fix every finding in verdict.json. Change nothing else."}}
  ]
}'
```

What each step does, and why each field is there:

| step | what it gets | what it does | what it publishes |
|---|---|---|---|
| `implement` | the default branch | the change | pushes `swarm/<task>`, opens no PR (an `integrate` contributor) |
| `review` | the implementer's branch (`builds_on`), and its patch (`input_from`) | writes `verdict.json` to `$SWARM_ARTIFACTS_DIR` | nothing it is merged for; see below |
| `fix` | the implementer's branch (`builds_on`), and the verdict (`input_from`) | on NOT_YET its agent fixes the findings; on MERGE **no agent runs** | the ONE pull request, carrying the verdict |

* **The patch is a named artifact already.** `swarm-work.patch` is the diff
  the worker harvests from every attempt with a repository and uploads under
  that name, so `input_from` stages it like any file. The implementer is not
  told to write it: it is the platform's (see "Artifacts pass by reference").
  An implementer that changes nothing writes no patch, so it fails for the
  missing output (`empty_diff`), not retried, unless it says changing nothing
  is a correct result with `allow_empty_diff: true`: then it SUCCEEDS with
  `no_change`, and the review and the fix, which need its change, are SKIPPED
  (see [an empty diff can succeed](#an-empty-diff-can-succeed-allow_empty_diff)).
* **The verdict is a file the review writes**, `{"verdict": "MERGE" |
  "NOT_YET", "findings": [...]}`. A finding is a string, or an object whose
  `summary`, `title` or `message` is one. The verdict is read whatever its case
  and surrounding spaces. The file name is yours: the gate reads whatever the
  gated step stages from `when.step`. Any other key is ignored by the gate,
  which is what lets an issue run's review (#454) put
  `"requirements": [{"index", "met", "note"}]` in the same file: swarm-api
  reads that list when the pull request opens, and the pull request says
  `Closes #N` only when every planned requirement is answered `met: true`
  (`swarm_api/issueci.py`, `evaluate_requirements`;
  [issue-runs.md](issue-runs.md#closes-n-only-when-the-review-confirmed-every-requirement)
  says why).
* **`when` gates the AGENT, not the step.** The gated step runs whatever the
  verdict, because it is the step that publishes: if it were skipped on
  MERGE, the reviewed work would never reach a pull request. When the verdict
  is not in `verdict_in`, the worker stages the verdict, checks its fencing
  generation as it would before an agent, starts no agent, and ends the
  attempt through the same path a clean agent exit takes: final checkpoint,
  uploads, publish, lease release. The task is SUCCEEDED, and
  `result_summary.verdict_gate` says `agent_ran: false`. The step holds a
  lease for that short publish; it is real work, and it counts like any
  other (invariants 1 and 3).
* **The gate is read by the worker, not the scheduler.** The scheduler reads
  no artifact, so it would need a GCS read on its drain path to learn the
  verdict, and skipping the step there would skip the pull request.
* **`builds_on` is how the fix sees the implementer's code.** Without it every
  step clones the default branch, and the fix agent would edit code that does
  not contain the change it is fixing. The worker clones
  `swarm/<implement task id>`, derived from the task id with its own branch
  prefix and never read as a ref name, exactly as the integrator's
  contributor branches are. The branch exists because the implementer pushed
  it as a contributor, so `builds_on` needs a strategy that pushes every step:
  `integrate` or `direct-pr`. A base whose branch was never pushed (a
  read-only token, a failed push) fails the clone, naming the task and the
  branch. The review builds on the implementer too, so it reviews the change
  in its tree rather than the patch alone.
* **Only the final step opens a pull request.** Under `integrate` the
  contributors push branches and open nothing; the fix is the integrator,
  merges the implementer's branch (already in its history, so the merge is a
  no-op, not a conflict) and opens the one PR. Its body carries the verdict,
  whether the fix ran, and the findings, inside a fenced block with every
  run of three backticks broken, because they are an agent's untrusted text
  on a page anyone with read access sees.
* **The review's own branch is not merged.** swarm-api leaves the step a
  gated integrator reads its verdict from out of that integrator's
  `integrates`. A review edits nothing, so it pushes no branch, and the
  integrator would otherwise report it "not found" on the PR as an incomplete
  integration. If it did edit the repository, those edits are unreviewed
  work, which is what the gate keeps out.
* **A contributor that published nothing by design is not "missing"**
  (#760). A review with no `when` gate reading it -- the implement -> review
  -> fix chain in `scripts/acceptance/groups/workflow.sh` -- stays in the
  integrator's `integrates`. If it ends SUCCEEDED with
  `result_summary.git.published: false`, `commit_count: 0` and nothing left
  uncommitted (it wrote only `verdict.json`), the integrator does not fetch
  it and the pull request does not list it under "NOT included". The worker
  decides this (`Worker._integrates_with_changes`), not swarm-api, because
  only the worker reads the contributor's finished result: the `integrates`
  list is fixed when the workflow is submitted, before any step has run. The
  pull request names such a step on its own neutral line,
  `read-only, nothing to merge: ...`, under the merged list, and the run
  result lists it under `git.integrated.read_only`. `- missing:` keeps its
  meaning, a step that should have pushed and did not: a FAILED contributor,
  one whose result cannot be read, and one that says it published but whose
  branch is not on the remote are still merged, and still listed missing
  when the branch is absent. Release acceptance fails on any `- missing:`
  line, so the distinction is what lets that check stay strict.

### What a MERGE verdict publishes

On MERGE the fix step starts **no agent** (owner decision, 2026-10-05: a fix
agent after a MERGE was paid for and could only add unreviewed change). The
step ends SUCCEEDED with `result_summary.skipped_agent: "review verdict
MERGE"` beside `verdict_gate.agent_ran: false`. NOT_YET is unchanged: the fix
agent runs.

**With one contributor, the control plane now opens the pull request itself**
(since contract request 52 was accepted by the owner on 2026-10-09): swarm-api
creates the branch and opens the pull request with no worker, no lease and no
clone, and the step records `published_by: "control_plane"`. That saves about
116 s and one Cloud Run execution per MERGE workflow, the cost measured on
2026-10-06 of starting a container only to push a branch that already exists
(below). With several contributors, or whenever swarm-api declines, the
integrator path runs as before: a worker merges the contributors' branches and
opens the one pull request.

What titles that pull request, since no agent wrote the `pr-title.txt` an
integrator owes:

1. **the implementer's own `pr-title.txt` and `pr-body.md`**, the uploaded
   artifacts of the `builds_on` step. They are read through the same
   tenant-checked upstream read staging uses, located in the implementer's
   successful attempt's manifest (a key outside this tenant's prefix for
   that task is refused), at most 256 KiB each;
2. for whichever is absent, or for a title the publish would refuse (two
   lines, attribution, a task id): text made from the **workflow's label**,
   the submission's `metadata.unit` (what `swarm_workflow_launch` sends a
   spec's `label` as), else its `metadata.title`. swarm-api copies it into
   the gated step's `dispatch.pr_label`, because the worker reads no
   metadata key the spec signature does not cover.

Both are written into the fix attempt's artifacts folder and read by the
publish exactly as an agent's text is: scrubbed of every registered secret,
refused on attribution or a task id, every mention neutralised.
`result_summary.pull_request_text_from` says where each came from
(`implementer`, `label`, or null). With neither -- no implementer text and no
label -- nothing is generated, and the missing title fails the attempt as it
always has; a step given an `issue` input is titled from the issue instead.

#### When swarm-api opens it without a worker (#748, contract request 52)

Starting a worker only to open that pull request cost 116 s from the review's
end, and one execution and one lease, on the three MERGE workflows measured on
2026-10-06 (57 s of it container start, 35 s egress wait and clone). With one
contributor, the integrator is cloned from the implementer's pushed branch,
merges that same branch (nothing to merge) and pushes it unchanged. The
implementer's own worker already scanned every byte of it before its push.
So swarm-api can open the pull request itself
(`apps/swarm-api/swarm_api/verdictpublish.py::on_task_finished`):

1. The scheduler's wake topic has a second push subscription,
   `api_task_finished`, which carries only `task_finished` to
   `POST /v1/admin/tasks/finished` as the rollup sweeper. Meanwhile the
   scheduler leaves `integrate`'s gated integrator PARKED, holding nothing,
   for up to `CONTROL_PUBLISH_HOLD_SECONDS` (60 s in the root) after its last
   parent ended (`apps/scheduler/scheduler/loop.py::Scheduler._held_for_control_publish`).
2. swarm-api claims the step in `metadata.control_publish`, in a transaction
   on its PARKED state, and checks every condition below. If they all hold, it
   creates `swarm/<fix task>` at the commit the implementer recorded it pushed
   (refused if the remote branch has moved since). It then opens the pull
   request, with the implementer's `pr-title.txt` and `pr-body.md` and the
   token the step's own worker would read, read from Secret Manager at that
   moment: the secret the step's `forge_credential` names (`git`,
   `git-r-<hex>` or `git-u-<hex>`, through `gittokens.secret_name_for`), the
   tenant's `-git` when it names none (contract request 54). It reads none
   of those fields until the step's spec signature verifies, with the
   workers' own keys (`SPEC_VERIFY_KEYS`), and for a `git-u-<hex>` slot it
   reads that person's grant again, as the worker does before each push
   (docs/onboarding.md §3.3 step 4). The token is
   never written to the repository, a Job environment or a log. Finally it ends the step
   SUCCEEDED, in the same `result_summary.git` shape a worker writes, plus
   `published_by: "control_plane"` and `verdict_gate.agent_ran: false`. The
   step's `task_finished` wake then releases its own dependants (the merge
   step).
3. In every other case it **declines**. It records why in
   `metadata.control_publish.code` and rings the parent's wake, and the
   scheduler promotes the step to its worker at once, exactly as before.

Declined, and so the worker's: several contributors, `continues`, an `issue`
input, a verdict that runs the agent (NOT_YET), a verdict file that does not
read as a verdict (the worker then refuses it by name), a finding marked minor
(the worker files those on the wave epic), an implementer that changed nothing
or pushed nothing, a missing title, or one the worker would refuse (two lines,
a control character, attribution, a task id, or the `[swarm] task_`
placeholder anywhere in it). Also declined, with code `credential`: anything
in the title or body that swarm-api's redaction masks or that holds the
token, a spec whose signature does not verify (unsigned, an untrusted key
version, an unknown format, or a document rewritten since it was signed), a
step whose `forge_access` is set and is not `write` (absent means write, as
for the worker), a user slot whose grant was removed or is no longer `write`,
a malformed `forge_credential`, and an unreadable token.

**Why the signature is checked first.** A tenant's agents can write any task
document of their tenant. `forge_credential`, `forge_access`,
`repository_url` and the dispatch block are all inside the signed spec
(`swarm_common.specsign`, format 3) for exactly that reason, and the worker
reads them only from the verified document. Read unverified, clearing
`forge_access: read` would buy a push with the tenant's `-git` token, and
rewriting `forge_credential` to another member's `git-u-<hex>` a push as that
person. The check uses the id the document was read by, never its `id`
field. Any refusal from GitHub is declined too. The body keeps the worker's
rules: attribution lines removed (#735), mentions neutralised, 60 KiB at most.

**The refusal scans are the worker's.** #748 asks for "the same refusal scans
the worker runs (credentials, title placeholder)". swarm-api does not import
the worker and nothing moved into `swarm_common`, so both are restated in
`verdictpublish.py` and held equal by
`tests/unit/control_plane/test_verdict_publish.py`:

* **The title.** `title_refusal` refuses what the worker's
  `Worker._agent_title` refuses, with the same reasons, and everything the
  merge step's `agent_worker.merge.title_is_placeholder` refuses. A pull
  request so titled would sit unmergeable, so it is never opened. It also
  refuses any task id, where the worker refuses only its own: the title was
  written by another task. One table of titles goes through all three
  functions (`test_the_restated_worker_rules_are_the_workers`).
* **The credentials.** The worker's credential half is its final-tree leak
  scan (`final_tree_leak`) plus scrubbing its pull request text. Here the
  tree needs no scan. The branch tip is the implementer's, scanned by the
  implementer's own worker before its push, and a tip that is not that
  recorded commit is declined (`implementer_branch_moved`). Only the title
  and body are new. Both come through the API's redacted artifact read,
  which declines rather than publish masked text, and both are checked for
  the step's token once it is read. Where the worker would scrub a
  registered value out of a title and publish the rest, swarm-api declines
  and the worker runs: the stricter of the two, never the looser.

**What a control-plane publish records.** The step ends SUCCEEDED, without a
lease, a pool count or an execution. `result_summary` carries the worker's
`git` shape plus `published_by: "control_plane"`, and
`verdict_gate.agent_ran: false`. A step published by its worker carries no
`published_by`, so the two paths can be told apart afterwards.

If swarm-api never decides (a lost push, or swarm-api down), the hold ends and
the step goes to its worker. A claim older than 300 s is ignored too. A worker
that then finds the pull request already open adopts it.

**On since contract request 52 was applied.** The path shipped switched off,
because the frozen state machine had no PARKED -> SUCCEEDED edge
(`swarm_common.states._ALLOWED`) and a step that never had a lease cannot
honestly pass through RUNNING. The owner accepted the request on 2026-10-09,
and the edge is now in `_ALLOWED`. Both swarm-api (`contract_allows`) and the
scheduler's hold still read `can_transition(PARKED, SUCCEEDED)`, so the
switch stays the frozen contract and not a setting. The hold also needs
`CONTROL_PUBLISH_HOLD_SECONDS` above 0 (60 s in the root; 0, the code
default, holds nothing and every MERGE workflow goes to its worker). Taking a
lease instead, so the step could pass through RUNNING, was rejected: it books
capacity for work that needs none, and puts a second admission writer beside
the scheduler (invariant 2).

### What is refused at submission

Every refusal is a 422 before anything is created, naming the step.

| declaration | answer | why |
|---|---|---|
| `when.step` that this step stages no file from | `invalid_dag` | the verdict is read from that file; add `input_from` (and `depends_on`) |
| `when.verdict_in` empty, repeated, or outside `MERGE`, `NOT_YET` | `invalid_dag`, `detail.accepted_verdicts` | a review following the convention could never write it |
| `when` under `direct-pr` | `invalid_dispatch` | every step opens its own PR there, the implementer's included, before the review |
| `when` under `integrate` on a step that is not the integrator | `invalid_dispatch`, `detail.integrator_step_id` | the integrator's PR is where the verdict is shown |
| a gated step that another step's `input_from` stages from | `invalid_dag`, `detail.staged_by` | when its agent does not run it writes nothing, so that step would fail |
| `builds_on` naming a step that is not upstream, or itself | `invalid_dag` | its branch may not exist yet |
| `builds_on` under `collect` | `invalid_dispatch` | `collect` pushes no branch to start from |

`when` under `collect` is accepted: nothing publishes, and the gate only
decides whether an agent runs.

### What the worker refuses

A gate or a verdict file that cannot be read fails the attempt with
`INPUTS_UNAVAILABLE`, naming the upstream task and the file, and the agent is
never started. That covers a file that is not JSON, is not an object, has no
`verdict` or one outside the two, or is over 256 KiB, and a gate whose task
this step stages nothing from. **An unreadable review is not a MERGE**: read
as one, it would publish unreviewed work; read as NOT_YET, it would run a
fixer against findings that are not there. The review step has already
SUCCEEDED by then, so under `fail_workflow` the workflow ends with nothing
published, which is the safe outcome, and the review has to be run again.

### Where it is recorded

The two fields travel in `task.metadata.dispatch`, which is already reserved,
keyed by upstream task id as `integrates` is: `builds_on: <task id>` and
`verdict_gate: {"task_id", "verdict_in"}`, and on the gated step the label a
MERGE titles its pull request with, `pr_label`. `GET /v1/tasks/{id}` returns them in
`dispatch` on the steps that have them. The workflow document's steps do NOT
carry them: `WorkflowStep` is frozen, so typing them there is contract request
29 in [contract-change-requests.md](contract-change-requests.md).

### What this does not do

* **It does not review the fix.** One review, one conditional fix. A second
  review of the fixed branch is another review step and another gated step,
  but under `integrate` only the integrator may be gated, so that chain needs
  a gate on a non-integrator whose verdict the PR also carries, which is not
  built.
* **It does not check the verdict where it is written.** A malformed verdict
  is found by the gated step, after the review has SUCCEEDED, not by the
  review's own end-of-attempt check, which would retry it.

### Minor findings are filed on the tenant's wave epic

CLAUDE.md's rule is that minor findings go to a wave epic, one comment per
finding. Until #638 that depended on an operator copying them out of
`verdict.json` by hand, and the 2026-10-05 history analysis counted 530 minors
across 122 reviews that never left the file. Now the gated step files them.

* **What a minor is.** A finding that is an object with `"severity":
  "minor"` (any case) and a `summary`, `title`, `message`, `problem` or
  `what`. It may also name `file`, a call site (`call_site`, or `where`) and
  how it was found (`evidence`; failing that, a `fix` trails the comment as
  "suggested fix: ..."). Both of these are read:
  `{"severity": "minor", "summary": "...", "file": "apps/x/a.py", "call_site":
  "run()", "evidence": "..."}` and the shape the review briefs prescribe,
  `{"severity": "minor", "file": "apps/x/a.py", "where": "run()", "problem":
  "...", "fix": "..."}`. The second is the shape of the 530 minors #638
  counted; reading only the first filed none of them. A string finding, or a `blocker` or `major`, is
  the fix step's and is **never** filed: blockers and majors are what the fix
  agent fixes, and a NOT_YET with only minors would otherwise be filed and
  fixed twice. Every finding, minor or not, still reaches the pull request
  body and the fix agent exactly as before.
* **Where.** The tenant's `findings_epic`, an issue number in the repository
  the run works on, set by an admin with `PUT
  /v1/admin/tenants/{tenant}/findings-epic {"findings_epic": 638}` (`null`
  stops it; `GET` reads it). It is a field of the tenant document beside the
  frozen `Tenant` fields, not one of them. swarm-api copies it at submission
  into the GATED step's `dispatch.findings_epic`, inside the block the spec
  signature covers, so no agent of the tenant can point the worker at another
  issue, and a change reaches workflows submitted after it.
* **Who posts.** The gated step's WORKER, after the agent ended (or, on
  MERGE, where no agent ran) and after its publish, with the tenant's own
  `swarm-tenant-<tenant>-git` token: never the agent, which never sees the
  token, and never another tenant's. The token goes only to github.com
  (#307); it is registered with the log redaction and appears in no log line,
  event or result. Each comment is one line in the epic shape, the agent's
  text scrubbed of registered secrets and with every `@`-mention broken:

      - [ ] **<the defect>** · `<file>` `<call site>` · found by review `<task>` of workflow `<wf>`; <evidence>

* **Once.** Every comment already on the epic is read first (at most 30 pages
  of 100), and a finding whose (text, file, call site) is already there, filed
  by an earlier run or by a person in the same shape, ticked or not, is not
  posted again. An epic that cannot be read whole files nothing: a partial
  read taken as whole would file duplicates.
* **The result says what happened.** `result_summary.findings_epic` on the
  gated step: `epic`, `repository`, `minors` (how many the verdict marked),
  `filed` (`text`, `file`, `call_site`, `comment_id` of each posted),
  `already_filed`, and `not_filed` with the reason when anything was not
  posted. With no epic configured, `epic` is null, nothing is posted and
  `not_filed` says so.
* **It never fails the step.** The step's pull request is already open when
  this runs. A forge that refuses or is down is recorded in `not_filed`; the
  comment POST is not retried (one whose answer was lost may have landed), and
  the next run's dedup posts what this one did not.
* **What it does not cover.** A review with no gated step after it (a review
  whose verdict nothing reads), and the `single-pr` chain's `review.json`,
  whose `summary` is free text with no per-finding severity: neither files
  anything.

## `metadata.input_from` belongs to the service, not the caller

The worker stages a task's inputs from `metadata.input_from`, a map of
`{upstream TASK id: filename}`. **Only workflow expansion writes that key.** It
rewrites each step's `input_from`, keyed by step id, to the ids of the upstream
tasks it has just created. That rewrite happens after the declaration has passed
the checks above, including the rule that every `input_from` source is also a
`depends_on`.

So the API refuses the key from callers, the same way it refuses
`metadata.dispatch` (owner decision on #151, 2026-09-25):

| request | answer |
|---|---|
| `POST /v1/tasks` or `POST /v1/tasks/batch` with `metadata.input_from` | 422 `invalid_dispatch`, `detail.reserved_metadata_keys: ["input_from"]`, nothing created |
| `POST /v1/workflows` whose own `metadata` has `input_from` | the same 422, nothing created |
| `POST /v1/workflows` whose steps declare `input_from` | accepted; each declaring step's task gets `metadata.input_from` |

The key is refused whatever its value, `{}` and `null` included. It is
reserved, not validated. A value on a plain task arrived with no dependency
edge, so nothing guaranteed the upstream task had run. A bad declaration was
refused only by the worker, after the task had been admitted and had held
capacity. The workflow's own `metadata` is copied onto every step's task, so a
value there reached every root step verbatim and was silently replaced on any
step that declared its own. To stage an artifact, declare `input_from` on the
workflow step that needs it.

The refusal runs before the DAG checks, so a workflow whose own `metadata`
carries `input_from` answers `invalid_dispatch` even when its steps would also
have been refused `invalid_dag`. There is no valid workflow-level value for the
filename rules above to check: they apply to a step's `input_from`, which is
the only declaration a caller can make.

A batch is refused whole: one task carrying the key means none of the batch is
created.

**Three metadata keys are reserved, and one refusal names all of them.**
`dispatch`, `input_from` and `expected_outputs` (above) are the keys the
service writes into `task.metadata`. They are one tuple,
`validation.RESERVED_METADATA_KEYS`, checked by one function. A caller who sends
more than one gets a single 422 whose `detail.reserved_metadata_keys` lists each
of them, in that order, and whose message says why each is reserved and what to
send instead. `expected_outputs` was first reserved by a check of its own (#153),
so as not to collide with the `input_from` change. That check heard about one
key per round trip, and it was folded into the tuple once both had merged.

## Failure behaviour

| `on_step_failure` | Effect when a step is FAILED or DEAD_LETTERED |
|---|---|
| `fail_workflow` (default) | every step of the workflow that has not started (SUBMITTED, QUEUED, READY, PARKED) is CANCELLED, dependent on the failure or not |
| `continue` | only the transitive dependents of the failed step are CANCELLED; independent branches keep starting |

The scheduler honours the field from the release that carries PR #42. Before
that nothing read it, and both rows behaved like `continue`. The rules that
are not obvious:

* **Workflows submitted before that release are covered too, unless a cutoff
  is set.** Every one of them stores `fail_workflow`, because that was the API
  default, and each was submitted while this page said the setting was not
  honoured. With `ON_STEP_FAILURE_ENFORCED_SINCE` unset (the default), the
  first drain after the deploy cancels the not-started steps of any in-flight
  workflow that already has a FAILED or DEAD_LETTERED step. A cancelled step
  cannot return to READY, so its checkpoint becomes reclaimable and its partial
  work is gone. Set `ON_STEP_FAILURE_ENFORCED_SINCE` on the scheduler (ISO-8601
  with a UTC offset, for example `2026-09-25T14:00:00Z`) and only workflows
  created at or after that instant get the rule. Older ones keep the
  dependency rule they were submitted under. A value without an offset refuses
  to start. To see what the next drain would cancel, run the read-only audit
  before deploying. It exits 3 if a page came back full:

  ```bash
  PROJECT_ID=saga-agents-staging FIRESTORE_DATABASE=swarm \
    uv run --project . python -m scheduler.on_step_failure_audit
  ```
* **It happens on the scheduler's next drain, not at the instant of failure.**
  That is a Pub/Sub push or the one-minute safety tick. A failure the drain
  itself writes, when a step exhausts its attempts on a failed dispatch, stops
  its siblings in that same drain. A failure a worker or the reconciler writes
  while a drain is running is seen by the next drain, because the verdict is
  read once per drain. So a sibling can still START after the failure: one
  that drain admits in the meantime. The window is the rest of that drain,
  which `MAX_RUN_SECONDS` bounds (45 s by default). No read closes it
  completely, because a failure can land between reading the verdict and
  taking the lease. A step that starts in that window holds capacity, so it
  runs to completion like any other running step.
* **Every cancel names the failure.** The step's CANCELLED event carries
  `workflow_id`, `on_step_failure` and `failed_steps` (step id, task id,
  state), and its `last_error` names the failed step. The metric is
  `swarm_scheduler_cancelled_total{reason="workflow_failed"}`, and the drain
  summary counts the cancels in `cancelled` and the workflows in
  `failed_workflows_swept`.
* **CANCELLED is not a failure, under either setting.** A step stopped by hand
  takes its own dependents (`last_error` "an upstream workflow step did not
  succeed") and nothing else. Otherwise pressing stop on one agent would end
  the whole run under the default.
* **A step that has not started is found through the scheduler's existing
  touch points,** not by scanning failures: the dependency sweep, the
  credential sweep, the prewarm sweep, and admission, which every step passes
  through before it can start. A step parked for a missing key is cancelled
  where it is parked, by the credential sweep. So is one parked for provider
  quota, cooldown or outage, by the prewarm sweep, while prewarm is enabled
  (the default; with it off, those reasons join the list below). A
  workflow whose only remaining steps are PARKED on a reason the scheduler never
  reads (MANUAL_PAUSE, BUDGET_EXHAUSTED, SCHEDULED_RETRY) is swept when one of
  them is promoted. Until then it holds no capacity, and its derived state reads
  PARKED rather than FAILED.
* **Each cancel is re-checked inside a transaction**
  (`SchedulerStore.cancel_if_not_started`). A step that a concurrent drain
  leased after it was read is left to run.

Cancel explicitly if you need a workflow stopped, running steps included:

```bash
./scripts/api.sh POST "/workflows/$WORKFLOW_ID/cancel"
```

Steps already holding capacity (LEASED, DISPATCHED, STARTING, RUNNING) are
**not** killed, flagged or written to when a sibling fails, under either
setting. They hold leases and partial work. Killing them wastes what
checkpointing exists to preserve, and releasing their capacity from the
scheduler would decrement pools a live container still occupies (invariant 1).
They run to completion. This is also why the derived workflow state below
reports RUNNING rather than FAILED while a sibling is still live: the workflow
is not over and a container is still costing money. Under `fail_workflow` it
reads FAILED once they finish.

A step that was running when its sibling failed, and that later returns to a
not-started state (a quota park, or a reconciler reclaim back to READY), is
PARKED or READY when the scheduler next meets it. Under `fail_workflow` it is
then cancelled like any other step that has not started.

## Workflow state

`workflow.state` is **derived from the step tasks on every read**, and the stored
copy is written back when the two disagree. Both halves matter and they are
different halves:

* **derived**, so the value a reader sees cannot go stale. Until 2026-09-22
  nothing ever advanced the stored field — `create_workflow` set it once and
  `cancel_workflow` only touched `cancel_requested` — so every workflow read
  `QUEUED` for its whole life. `wf_bcdc9180e4fb4a209f31` read QUEUED while its
  three steps were SUCCEEDED, FAILED and CANCELLED.
* **written**, so it is queryable. A purely derived field cannot answer "list my
  failed workflows" without loading every workflow's tasks.

The rule, in `apps/swarm-api/swarm_api/rollup.py`, in the order it is applied:

| condition | derived state |
|---|---|
| any step's task could not be read | `UNKNOWN` |
| any step holds capacity (`LEASED`/`DISPATCHED`/`STARTING`/`RUNNING`) | `RUNNING` |
| every step terminal | the worst present: `DEAD_LETTERED` > `FAILED` > `CANCELLED` > `SUCCEEDED` |
| otherwise | the most advanced pending: `READY` > `PARKED` > `QUEUED` |

A step the worker SKIPPED for "nothing to change" (`result_summary.skipped`,
[above](#the-steps-that-needed-the-change-are-skipped)) is a SUCCEEDED task and
ranks as one; `rollup.skipped_steps` names it. A `skipped` marker on a task
that did not SUCCEED is not a skip.

`UNKNOWN` is not a `TaskState` and is never written to Firestore. It is what a
read says when it could not establish an answer, and it exists so that a
derivation over a partial read cannot come back as "SUCCEEDED because the
failures did not load".

`cancel_requested` stays a separate boolean and is **not** folded into the state.
Cancelling is a request: a step holding a lease keeps it until the worker or the
reconciler releases it, so between the request and the release the workflow
really is still running.

### The drift check

The stored copy and the derived one are two records of one fact, which is the
defect shape this platform has had elsewhere (a pool counter versus a lease sum).
Every workflow read therefore carries a `drift` block:

```json
{"stored": "QUEUED", "derived": "FAILED", "agrees": false, "repaired": true}
```

`agrees` is three-valued. `null` means the two were **not compared**, because a
step could not be read — the same caution the capacity-holders screen carries
about a delta computed over a truncated page. A disagreement stays reported as a
disagreement after the write-back has fixed it, and moves
`swarm_api_workflow_state_drift_total`, because a repair that leaves no trace is
a silent resolution.

Workflows nobody reads are converged by an explicit sweep:

```bash
./scripts/api.sh POST "/admin/workflows/rollup?tenant_id=$TENANT"
```

A Cloud Scheduler job calls that route for every registered tenant (the keys
of `var.tenants`), every 15 minutes: `google_cloud_scheduler_job.workflow_rollup`
in `terraform/modules/scheduler/jobs.tf`, named `swarm-workflow-rollup-<tenant>`.
It presents an OIDC token for its own account, `swarm-rollup-sweeper`, which
holds no project role. Its one grant is `roles/run.invoker` on swarm-api
(`rollup_sweeper_invokes_api` in `terraform/infra/main.tf`), because in prod
`api_invokers` names only the tenant groups and Cloud Run's edge would refuse
the job before the application saw it. swarm-api admits that one address (`ROLLUP_SWEEPER_USERS`,
set by `terraform/infra/locals.tf`) to `POST /v1/admin/workflows/rollup`, to
the issue-run tick `POST /v1/admin/runs/advance` (#454, below), and to nothing
else, admin or not (`swarm_api.auth.ROLLUP_SWEEPER_ROUTES`, held by
`tests/unit/control_plane/test_rollup_sweeper_is_narrow.py`), which also
lists the repository poll, the merge wake, the forge refresh and, since #748, the
`task_finished` push that opens a MERGE verdict's pull request without a
worker (`POST /v1/admin/tasks/finished`, "When swarm-api opens it without a
worker" above). It is not an admin
because admin is one boolean that opens every `/v1/admin` route, including the
one that disables a tenant.

The same account calls `POST /v1/admin/runs/advance?tenant_id=<t>` for the same
tenants every minute (`google_cloud_scheduler_job.issue_run_advance`, named
`swarm-issue-run-advance-<tenant>`). It moves the tenant's issue runs as far as
their planner and workflow say, as a read of the run would, so a run nobody is
watching -- and every `plan_approval: auto` run -- still advances and is
written back to its issue. A PLANNED run waiting for a person is not read and
nothing is created for it (invariant 1). An `auto` approval is submitted in the
run's own tenant as the member who created the run (`routes/runs.py`
`run_owner_auth`), never as the sweeper, which holds no tenant. An issue run
compiles its approved plan into one `integrate` workflow of this shape --
implementers, a review, a gated fix -- and continues that workflow's integrator
for each CI fix round; why, and how to read a stuck run, is in
[issue-runs.md](issue-runs.md).

So the stored copy of a workflow nobody reads lags its steps by at most one
schedule interval plus a sweep, and a reader never sees the lag at all. Two
limits: a personal `u-` tenant the API creates at runtime is not in `var.tenants`
and is not swept (its workflows still converge on every read), and a sweep
examines one page of a tenant's live workflows, reporting `truncated: true` when
there are more. Request #7 in
[contract-change-requests.md](contract-change-requests.md) is the alternative
that would have put this write on the scheduler's own tick.

## Inspecting

```bash
./scripts/api.sh GET "/workflows/$WORKFLOW_ID" | jq '{
  state: .workflow.state,
  stored: .workflow.stored_state,
  drift: .workflow.drift,
  steps: [.tasks[] | {step_id, state, park_reason, blocked_by}]
}'
```

A step stuck at `DEPENDENCY_INCOMPLETE` whose upstream shows `SUCCEEDED` means
the sweep has not run yet — wake the scheduler, or wait for the 1-minute tick.

## Ending in a merge: the `merge` step

**Built 2026-10-04 (contract request 47, owner decisions recorded on #295).**
A workflow that opens a pull request can end by merging it. The `merge`
profile runs no agent: the worker squash-merges the pull request with the
tenant's existing `-git` token, in the workflow's own repository, and then
closes the issues the pull request closes. The design and every refusal are
in [merge-step.md](merge-step.md) ("Revised 2026-10-04 (owner)").

**Revised 2026-10-06 (owner), design only -- lane MS0, part of #352.** The
step becomes the last of implement → review → fix → merge on any repository
a workflow runs on, and replaces `auto-merge.yml` for SwarmCloud's own pull
requests once it has merged about ten cleanly. While the pull request's
checks run it will park as `CI_PENDING`, holding nothing, instead of failing
attempts. A per-tenant tick in swarm-api re-reads the checks with the
tenant's `-git` token and marks it for wake. If the base requires an
up-to-date branch and the branch is behind, the step updates it and parks
again. Every refusal ends `MERGE_REFUSED` with its code. The lifecycle, the
token's permissions, the invariants, the retirement and the lanes MS1-MS7
that build it are in
[merge-step.md](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs).
Until MS2 lands, the step waits as described below
(`MERGE_STEP_MAX_ATTEMPTS`, READY between attempts).

The review shape above, ending in a merge:

```bash
./scripts/api.sh POST /workflows '{
  "strategy": "integrate",
  "repository_url": "https://github.com/acme/widgets.git",
  "steps": [
    {"step_id": "implement", "runner_profile": "claude-code",
     "input": {"prompt": "Implement the change described in the issue."}},

    {"step_id": "review", "runner_profile": "claude-code",
     "depends_on": ["implement"], "builds_on": "implement",
     "input_from": {"implement": "swarm-work.patch"},
     "input": {"prompt": "Review swarm-work.patch. Do not edit files. Write verdict.json: {\"verdict\": \"MERGE\" or \"NOT_YET\", \"findings\": [...]}."}},

    {"step_id": "fix", "runner_profile": "claude-code",
     "depends_on": ["review"], "builds_on": "implement",
     "input_from": {"review": "verdict.json"},
     "when": {"step": "review", "verdict_in": ["NOT_YET"]},
     "input": {"prompt": "Fix every finding in verdict.json. Change nothing else."}},

    {"step_id": "merge", "runner_profile": "merge",
     "depends_on": ["fix", "review"],
     "input_from": {"review": "verdict.json"}}
  ]
}'
```

You do not have to write the last step. **It is opt-in, and chosen in one of
two places:**

| `metadata.merge` on the workflow | the spec states a merge step | result |
|---|---|---|
| absent | no | appended when the platform's `merge_by_default` is on (default **off**) and the repository is on github.com |
| absent | yes | the stated step is honoured |
| `"on"` | no | appended; refused (422) if the workflow opens no single pull request |
| `"on"` | yes | the stated step is honoured; nothing is doubled |
| `"off"` | no | none |
| `"off"` | yes | refused (422): the two disagree |

`merge_by_default` is the platform setting an admin reads and sets with
`GET`/`PUT /v1/admin/settings` (`{"merge_by_default": true}`), stored in
Firestore at `control/settings`. It applies to workflows submitted after it
changes; a workflow keeps the steps it was signed with. An issue run's
`auto_merge` is the same choice: absent, it takes the default, and the run
records what it resolved. The run does NOT merge inside its compiled
workflow (which, like every CI fix round, says `metadata.merge: "off"`): it
merges from its CI loop, with one merge-only continuation submitted once CI
is green and its `Closes #N` block is written, and only when its review said
`MERGE` ([merge-step.md](merge-step.md#revised-2026-10-04-owner)).

**A merge-only continuation** is a `direct-pr` workflow whose
`continues_task` names a task and whose only step runs the `merge` profile: it
merges that task's pull request at the head that task pushed, with every
check below except the verdict (it has no review). A tenant member's only.

**A merge of a pull request no workflow opened** (#352, owner decision
2026-10-07, MS0 question 2) is the same one-`merge`-step `direct-pr`
workflow with `merge_pr: {number, head_sha}` in place of `continues_task`:

```json
{"strategy": "direct-pr",
 "merge_pr": {"number": 41, "head_sha": "<the full 40-character head sha>"},
 "steps": [{"step_id": "merge", "runner_profile": "merge"}]}
```

From the bridge it is `swarm merge 41 --sha <head>` (or `owner/repo#41`, or
the pull request's URL), or `swarm_workflow` with `merge_pr`. Which
repository: the one `repository_url` names, which must be one the caller's
tenant registered (`/v1/repositories`), or -- when it names none -- the
tenant's ONLY registration. Several registrations and none named is refused
rather than guessed, because pull request numbers repeat across
repositories, and an unregistered repository is refused even when the
token could reach it, because the registration is the tenant's statement
that its `-git` token is meant to act there. At submission swarm-api reads
the pull request once with the tenant's `-git` token and refuses, writing
nothing: a `head_sha` that is not its head now (the answer names the current
head), a pull request that is closed or already merged, one not in that
repository or from a fork, one on another base than the registered default
branch, any step besides the one merge step, a `continues_task`,
`repository_ref` or `merge_fix_rounds` beside it, and any strategy but
`direct-pr`. A continuation-scoped account is refused before any read. The
merge step's signed `merge_target` is then `{number, head_sha, base}` instead
of a task id, and the worker merges through the gate every merge step uses:
only at that head (or GitHub's own update of it onto the base), only with
every required check green there. The console's Submit forms do not offer it
yet; that is a follow-up.

What swarm-api appends, before it signs anything:

* **`depends_on` the step that opens the pull request and the review.**
  Under `integrate` the pull request is the integrator's -- the agent steps'
  one sink -- and the review is the step the integrator's `when` reads. Under
  `direct-pr` there is ONE pull request only when there is one agent step, so
  a merge is appended (or accepted) only then; several `direct-pr` steps
  open several pull requests, and `"on"` there is refused rather than
  merging one of them. `collect` opens none.
* **`input_from` the review's verdict file**, the same file the gated step
  stages, so the merge reads the verdict the gate read. The merge refuses
  anything but `MERGE`.
* **10 attempts** (`MERGE_STEP_MAX_ATTEMPTS`). A required check still running
  fails the attempt retryably and the step waits READY for 5 minutes, holding
  nothing (invariants 1 and 4), then reads every fact again.
* **Its signed dispatch block names its target by task id**
  (`merge_target: {pull_request, review, verdict_file}`), so the worker never
  follows a pointer the signed spec does not name. A `merge_pr` workflow's
  names the pull request instead (`merge_target: {number, head_sha}`),
  because no task opened it.
* **`merge_target.base`, the default branch the tenant registered the
  repository with** (`/v1/repositories`, [repo-index.md](repo-index.md) §1),
  read once at submission from the tenant's OWN registration, and absent when
  it registered none. It is there so the worker can refuse a pull request on
  any other base (`base_not_default`, lane MS3) without reading the registry,
  inside the block the spec signature covers. Another tenant's registration
  of the same repository never names it. Built 2026-10-06 (lane MS1); until
  MS3 the worker carries it and does not act on it.

**Lane MS1 (2026-10-06): the step's knobs.** Two more things are settled at
submission, from the workflow's own `metadata`:

* **`metadata.merge_fix_rounds`**, a whole number from 0 to 5, absent
  meaning 0: how many CI-fix rounds a red required check may hand the pull
  request to before the step refuses `checks_failed`
  ([merge-step.md](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs)
  §1, "The CI-fix loop"). Anything else -- a negative, more than 5, a string,
  a bool, a float -- is a 422 naming the bounds. So is the key on a workflow
  that ends with no merge step, whatever its value: rounds nothing would
  spend are a request that would silently not happen, as `metadata.merge`
  `"on"` there is refused. It is stored as written; lane MS7 spends it, and
  until then it is accepted and changes nothing.
* **The workflow's label is its pull request's fallback title, merge step
  or not.** `metadata.unit`, else `metadata.title`, reaches the gated step as
  the dispatch block's `pr_label`, which the worker uses only as title and
  body text when no agent wrote `pr-title.txt`; it never becomes a GitHub
  label. So a label that reads `ready` is kept as written beside a merge step
  too. Lane MS1 had dropped it there, recording
  `metadata.merge_label_dropped`; the owner reverted that on 2026-10-06
  (#352) because it was aimed at the wrong thing: what races the merge step
  is the GitHub `ready` label `auto-merge.yml` merges on, which an operator's
  watcher or brief adds, not this title. `metadata.merge_label_dropped` is
  no longer reserved or written.

What the merge step checks, in order, and refuses with a plain reason
(`result_summary.merge.refusal`): the verdict is `MERGE`; the pull request is
open, not a draft, on the opening step's own branch and from no fork; its
head is exactly the head that step pushed (`head_moved` otherwise); every
REQUIRED check of the base branch -- rulesets and classic protection -- is
success or skipped at that head, or, on a branch that requires none, every
reported check is green and at least one exists; GitHub says it merges
cleanly; the token can write. Then it squash-merges with the pull request's
title and `sha` pinned, comments `Merged by SwarmCloud task <id>` on the pull
request, and closes every issue in GitHub's `closingIssuesReferences` that is
still open, commenting `Closed by #<pr>, merged by SwarmCloud task <id>`. A
`part of #N` pull request closes nothing. The merge call is never resent: a
merge whose answer was lost ends `merge_unanswered`.

**Refused at submission, never at merge time:** a repository on a host no
merger serves. The merge acts on github.com only (`GitHubMerger`, the one
`ForgeMerger`), the one host the tenant's token is ever sent to. A merge step
the caller asked for -- stated, or `"on"` -- on another host is a 422 that
says to set `metadata.merge` to `"off"`. The platform default is NOT applied
there: a workflow on another host opens no pull request (the worker harvests
a patch), so with `metadata.merge` absent it is accepted with no merge step
appended, and an admin turning `merge_by_default` on breaks no tenant who
never asked for a merge.

## When not to use a workflow

If steps do not exchange artifacts and do not depend on each other, submit a
**batch** (`POST /v1/tasks/batch`, up to `max_batch_size`). Independent tasks
interleave across tenants under round-robin; a workflow adds dependency
bookkeeping you are not using.

## PROPOSED: a chain that merges its own pull request

**Superseded 2026-10-04 for the merge itself** by the `merge` step above
(contract request 47): merging moved onto the tenant's `-git` token, at the
end of any `integrate` or one-step `direct-pr` workflow. The `single-pr`
chain below stays proposed and disabled, because its `post-verdict` and
`claude-code-review` profiles do; what follows is its design as written.

**Proposed on 2026-09-29 for #295 and not built. Revised four times,
2026-09-29,** against a security review's two blockers and five majors, then
a re-review that found the first B1 revision insufficient, then a third
round that found B1 still open against a different attack and decided three
more owner questions, then a fourth, joint review with CR 34 (#344, signed
step specs) that found `post-verdict`'s own read of `review.json` was still
routed through a tenant-writable pointer, corrected the real GCS bucket
layout, and found the cross-workflow forgery claim was not actually closed;
[merge-step.md](merge-step.md)'s own revision note lists what changed each
time. The API refuses this spec today. `single-pr`, `pr_role`, the `merge`
profile, and the `post-verdict` and `claude-code-review` profiles below, do
not exist yet. The `merge` profile needs contract request 33 in
[contract-change-requests.md](contract-change-requests.md); `post-verdict`
and `claude-code-review` each need their own, not-yet-filed request
([merge-step.md](merge-step.md) §1.3, §4.3, §10). `post-verdict` is a
worker-action profile structured exactly like `merge`: its own Job, its own
service account, and **no agent ever runs on it** — that is why it exists
rather than holding the review App's key on a dedicated profile for the
`review` step itself, which a re-review found insufficient (a dedicated
profile only changes which Job an agent runs on, and the review agent, which
reads attacker-controlled diffs, would still share a container with the App
key regardless of profile). `claude-code-review`, by contrast, **is** a
dedicated profile for the `review` step — safe for a narrower reason: it
holds no App key, only a GCS write grant scoped to one prefix, which is not
a portable secret an agent could exfiltrate and reuse. **This design also
depends on S0 issue #342 (signed step specs), which is not a
`profiles.py`/`models.py` request and is not built either** — without it, an
earlier step's agent can rewrite a later step's own `prompt` before that
step starts, which nothing else in this design touches (see below). The
design, and the reason for each rule below, is
[merge-step.md](merge-step.md).

The owner's chain is **implement → review → post-verdict → fix → proof →
merge**. It produces **one** pull request and ends with the **worker**
squash-merging it. The merge runs under a service account no agent ever runs
as, with a credential no agent can read — and so does `post-verdict`, one
step earlier, for the review credential.

```bash
./scripts/api.sh POST /workflows '{
  "on_step_failure": "fail_workflow",
  "repository_url": "https://github.com/bogdan-alexandrescu/SwarmCloud",
  "repository_ref": "main",
  "strategy": "single-pr",
  "steps": [
    {"step_id": "implement",
     "runner_profile": "claude-code",
     "pr_role": "author",
     "input": {"prompt": "Implement issue #295. Write a fact-style pull request title to pr-title.txt."}},

    {"step_id": "review",
     "runner_profile": "claude-code-review",
     "pr_role": "reader",
     "depends_on": ["implement"],
     "input": {"prompt": "Review the checked-out head against issue #295. Write review.json: {\"verdict\": \"MERGE\" or \"CHANGES\", \"sha\": the checked-out HEAD, \"title\": the pull request title you reviewed, \"summary\": your findings}."}},

    {"step_id": "post-verdict",
     "runner_profile": "post-verdict",
     "depends_on": ["review"],
     "input": {}},

    {"step_id": "fix",
     "runner_profile": "claude-code",
     "pr_role": "amender",
     "depends_on": ["post-verdict"],
     "input_from": {"review": "review.json"},
     "input": {"prompt": "If review.json says MERGE, change nothing and exit. Otherwise fix every finding in it."}},

    {"step_id": "proof",
     "runner_profile": "claude-code",
     "pr_role": "reader",
     "depends_on": ["fix"],
     "input": {"prompt": "Prove the change works at the checked-out head. Write proof.json: {\"outcome\": \"PROVED\" or \"NOT_PROVED\", \"sha\": the checked-out HEAD, \"evidence\": what you ran and saw}."}},

    {"step_id": "merge",
     "runner_profile": "merge",
     "depends_on": ["post-verdict", "proof"],
     "input_from": {"proof": "proof.json"},
     "input": {}}
  ]
}'
```

`fix` depends on `post-verdict`, not on `review` directly, even though `fix`'s
own `input_from` still reads `review.json` from the `review` task (staged
artifacts are named by the task that wrote them, not the task that gates
starting). That substitution is the point: `fix`'s agent cannot start, and so
cannot race to overwrite `review.json`, until `post-verdict` has already read
it and posted an immutable GitHub review ([merge-step.md](merge-step.md)
§4.3). `merge` depends on `post-verdict` the same ordering-only way, and on
`proof` for its `input_from`; it does **not** depend on `review` directly any
more, since `post-verdict` is now the thing that stands between them.
`validate_dag` allows a `depends_on` entry with no matching `input_from`
source for exactly this reason.

**`post-verdict` declares no `input_from` at all — corrected, joint review
with CR 34, 2026-09-29.** An earlier draft of this spec gave it
`"input_from": {"review": "review.json"}`, the ordinary staging mechanism.
That mechanism resolves an artifact's location through the upstream task's
`result_summary`, a Firestore field the tenant identity writes — exactly the
kind of attacker-writable pointer this whole design exists to route around
for the one read the verdict anchor depends on. `post-verdict` instead
computes the object's path itself — `gs://<bucket>/tenants/<tenant>/verdicts/<workflow_id>/<review task id>/review.json`
— from `workflow_id` and the review task id named in its **own signed spec**
(#342), and reads that exact path directly, never through the generic
resolver ([merge-step.md](merge-step.md) §4.1, §4.3, §6a). The
`"depends_on": ["review"]` edge above still orders it correctly; it is just
not also the channel `post-verdict` uses to find the file.

What each step does:

| step | clones | publishes | writes |
|---|---|---|---|
| implement (`author`) | `main` | pushes `swarm/<its task id>` and opens the pull request | the code, and `pr-title.txt` |
| review (`reader`, profile `claude-code-review`) | the implement branch | no git push, nothing to GitHub at all. Writes `review.json` under its own identity, `swarm-<tenant>-review`, to a prefix the tenant's ordinary worker account cannot write | `review.json` |
| post-verdict (no agent, profile `post-verdict`) | nothing | submits a GitHub PR review (`APPROVE` or `REQUEST_CHANGES`, `commit_id` pinned to `review.json.sha`), from a credential only this step's own service account can read — the review agent that wrote `review.json` cannot read it. Reads `review.json` from the review-only-writable prefix, not the general staged-artifact location | nothing |
| fix (`amender`) | the implement branch | fast-forward pushes to the **same** branch, only if it changed something | the fix |
| proof (`reader`) | the implement branch | no git push, nothing to GitHub at all | `proof.json` |
| merge (profile `merge`) | nothing, and it runs no agent | `PUT .../pulls/{n}/merge`, squash, `sha` pinned, to `api.github.com` only, no redirect followed | the squash commit, and a comment naming the task that merged it |

The merge happens only if **all** of these hold. Otherwise the step ends
`MERGE_REFUSED` and `result_summary.merge.refusal` says which one failed:

* `post-verdict`'s review is `APPROVED` at the pinned head — not merely
  `review.json.verdict == "MERGE"`, which is now a descriptive claim, not the
  trust anchor (owner decision B1, 2026-09-29, corrected the same day: this
  replaces both the original scheduler attestation and a first, insufficient
  B1 revision that would have run the review agent and the App key in the
  same container);
* `proof.json.outcome` is exactly `PROVED` — a plain staged-artifact check,
  not an App-anchored one. **A proof that executes the pull request's own
  code anchors only that the code ran and produced that outcome — never that
  the change is safe** (owner decision, 2026-09-29; [merge-step.md](merge-step.md)
  §4.3, §11 Q6);
* the review and the proof ran on the same head, the fix pushed nothing after
  the review, and that head is still the pull request's live head. A fix
  that changed anything therefore ends this workflow's merge with
  `review_not_at_head`, because nobody reviewed the fix;
* every required check on `main` is completed `success` or `skipped` at that
  head, pinned to GitHub Actions — and any required check with **no** pinned
  `app_id` is refused rather than accepted through a legacy commit status
  (owner decision M5) — and no other check that ran there is failing or still
  running. **Pending is not green, and the step does not wait for it**;
* GitHub reports the pull request cleanly mergeable. The branch is never
  updated from `main`, and its `base.ref` is re-checked immediately before and
  again immediately after the merge call, so a retargeted base is caught
  rather than silently merged;
* the title is the one the review saw, and not the `[swarm] task_` placeholder;
* the pull request does not touch a protected path — `.github/**`,
  `scripts/**`, `terraform/**`, `kubernetes/**`, `images/**`, `Makefile`,
  `pyproject.toml`, `uv.lock`, or any `**/conftest.py` (broadened from
  `.github/workflows/` alone, owner decision M4: merging to `main` is code
  execution as the deployer, since `release.yml` grants `id-token: write` at
  workflow level and its verify job runs the merged tests). Such a pull
  request lands only through a human's `ready`.

`owner`/`repo` and the forge host come from a record no tenant identity can
write, never from the task document (owner decision B2). **Decided,
round-3 re-review: that record is Terraform-rendered environment on the
`post-verdict` and `merge` Jobs, not Secret Manager and not Firestore**
([merge-step.md](merge-step.md) §2.1b) — the open question this section
used to carry is resolved.

A person's `ready` label on the same pull request still works — and so, in
principle, does an agent's: the owner has kept `-git`'s PAT for agent pushes
(owner decision M3), so `ready` is not exclusively a human action any more,
though the label stays bound to the exact head it was set on. `auto-merge.yml`
stays authoritative for labelled pull requests, and whichever merge lands
first, the other finds it merged at the same pinned head.

**None of the above closes B1 by itself.** Every mechanism in this section —
`post-verdict`'s isolation, the review-only-writable prefix, the pinned
host — protects an artifact's integrity once it exists. Signed step specs
(#342) protect something none of them touch: the `prompt` a step is handed
*before* it runs. Without #342, an `implement` agent can rewrite `review`'s
still-`PARKED` task document, and the review agent would honestly follow
corrupted instructions to a genuine, unforced `MERGE` verdict about a change
it was never actually asked to judge. `post-verdict` and `merge` must verify
the signed spec of every upstream step they depend on, not only stage its
output ([merge-step.md](merge-step.md) §0 consequence 4, §7 T14). **The
merge step must not be enabled for any tenant until #342 ships** — a
precondition independent of, and in addition to, `main-protection` (M1). By
contrast, moving `release.yml`'s `id-token: write` to job level is
recommended but, per the owner's round-3 decision, is **not** a
precondition ([merge-step.md](merge-step.md) §7 R4, §11 open question (b)).
**Separately, and NOT closed by anything above (named residual R8, joint
review with CR 34, 2026-09-29):** the review-only-writable prefix stops the
ordinary tenant account from writing there, but `swarm-<tenant>-review` is
one identity shared by every `single-pr` workflow of the tenant, and its
write grant covers every workflow's verdict path at once. A review agent of
one workflow, compromised by prompt injection, can still write a fabricated
verdict at a *different* workflow's path if it is told (or can derive) that
workflow's identifiers. Write-once (`ifGenerationMatch=0`) makes the losing
side of that race fail loudly instead of being silently overwritten; it does
not decide who wins ([merge-step.md](merge-step.md) §4.3, §7 T3/R8).

---

# Part 2 — Development workflows

## Local loop (primary): the Firestore emulator

```bash
make dev                      # emulator + seeded pools + the API on :8000
make dev TARGET=scheduler     # the drain loop instead
make dev TARGET=quota-broker
make dev TARGET=emulator      # emulator only; point your own process at it
```

`scripts/lib/dev.sh` starts `gcloud emulators firestore`, waits for it, seeds the
slot pools the scheduler needs, and runs the service with
`FIRESTORE_EMULATOR_HOST` set. No cloud credentials are used and nothing touches
`saga-agents-staging`.

The emulator is used rather than a mock because the admission path depends on
**real transaction semantics** — the last-slot race resolves the way it does
because Firestore aborts and retries a transaction whose read set changed. A mock
that returns canned documents cannot exercise that.

Requirements: `gcloud components install cloud-firestore-emulator`, and a JRE
(the emulator is a Java program). On a machine without one, `dev.sh` says so and
points at the container path below.

## Local loop (secondary): docker compose

```bash
docker compose up api            # emulator + seeded pools + the API
docker compose up scheduler
docker compose run --rm tests    # unit tests, no cloud credentials at all
docker compose down -v
```

This is the **optional** path for operators whose Docker works. It is not
primary because the Docker daemon on the reference workstation is broken, and
because this Mac is arm64 while every target is amd64.

Nothing in `docker-compose.yml` builds a deployable artifact: it mounts the
source into a stock Python image. Images are always built by Cloud Build.

Every port binds to `127.0.0.1`. Authentication is **not** disabled — the API
has no switch for that, and refuses `REQUIRE_AUTH=false` rather than ignoring
it — but the emulator behind it is an unauthenticated database, so the stack
must never be exposed beyond loopback.

## Tests

```bash
make test                # unit tests + the destroy-guard self-test. No cloud.
uv run pytest tests/unit -q
uv run pytest tests/unit/control_plane -q -k admission
```

`tests/unit` needs no emulator and no credentials: the admission logic is pure
(`evaluate_capacity`) and the rest uses in-memory fakes. That is deliberate — the
concurrency invariant is the thing most worth testing exhaustively, so testing it
must be fast.

`make test` also runs `destroy.sh --self-test`, which verifies the teardown guard
still catches deny-listed and unlabelled resources. That guard is the only thing
standing between `make destroy` and another team's production cluster, so it is
checked on every test run rather than only before a teardown.

Cloud-backed suites are separate because they cost money and need a deployment:

```bash
make smoke concurrency-test race-test quota-test failure-test load-test
```

## Making a change

```bash
git checkout -b feature/thing
# edit
git commit && git push -u origin feature/thing
gh pr create
gh run list --branch feature/thing   # read the run
gh run view <id> --log-failed        # read the failure
```

**The lint, the tests, the build and the deploy are all CI's**, not this
machine's: `application.yml` and `terraform.yml` gate the pull request, and
`release.yml` builds, applies and deploys on the way to an environment. See
[where the gates run](ci.md). The `make` targets above this section are how
those workflows invoke each suite and how an operator drives a deployment they
already have credentials for — they are not steps in authoring a change.

**Never edit `apps/common/swarm_common/`.** It is frozen by `CONTRACT.md`. If a
change is genuinely needed there, say so in the PR description rather than making
it — every other component was written against those types, and a quiet change
breaks them all at once.

## CI/CD

| Workflow | Trigger | Does |
|---|---|---|
| `terraform.yml` | PR touching `terraform/**` | fmt, validate, tflint, checkov, `plan` posted to the PR |
| `application.yml` | PR touching `apps/**`, `scripts/**`, `tests/**` | shellcheck, unit tests, integration tests, build |
| `security.yml` | every PR + weekly schedule | trivy (repo + images), checkov, secret scan |
| `release.yml` | push to `main`, or manual | build, push immutable SHA tags, plan/apply with environment approval, deploy, smoke |

All four authenticate to GCP with **Workload Identity Federation**. There are no
downloadable service account keys anywhere in this repository or its secrets —
see `terraform/bootstrap/wif.tf`, where the attribute condition pins both the
repository and the allowed refs. Without the ref pin, a workflow triggered from a
fork could mint a deploy token.

Production releases require **manual approval** through a GitHub Environment,
before `:prod` is promoted and before the apply
([ci.md](ci.md#a-prod-release-waits-for-approval-before-anything-prod-facing)).
`plan` runs on the PR so the diff is reviewable before anyone approves an apply
into a project that hosts another team's production.

## Release flow

```
PR  -> checks -> review -> merge to main
        |
        v
    release.yml: build (or reuse CI's) -> immutable :<sha> tags -> trivy
        |
        v
    dev: promote digest -> apply + deploy + smoke          (no approval)
        |
        v
    prod (dispatched): trivy -> APPROVAL -> promote digest -> apply -> deploy -> smoke
```

Nothing is ever deployed by a mutable tag. `build/deployed-images-<env>.json`
records the digest of every promotion, which is also what makes a rollback a
lookup rather than an archaeology exercise.
