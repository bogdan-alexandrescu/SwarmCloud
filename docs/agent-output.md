# What an agent took in and produced: the read path behind the Artifacts tab

The task drawer has three panes: Details, Attempts and Artifacts. The
Artifacts pane shows a task's inputs, its outputs and its logs, and updates
every 5 seconds while the task runs (#184, owner decisions of 2026-09-25).
This document covers the backend half: what the worker writes, what the API
serves, and the reasons for both, including where an agent is told to put its
deliverables and what a task with no repository uploads from its working
folder (owner decisions of 2026-09-26). Each section records a constraint that
a later change could silently break.

## Every byte goes through the API, never through a signed URL

The owner decided this. A signed GCS URL would skip the two checks that every
served byte must pass:

* **The tenant boundary (invariant 9).** Each route resolves the task through
  `tenant_scope` and then `Store.get_task`. Another tenant's task gets the same
  404 as a task that does not exist. Object keys are rebuilt from the task
  document's own tenant and id. They are never taken from the request or from
  the stored `uri`, which is compared and not followed.
* **Read-time redaction.** The worker scrubs only the registered literal
  values of credentials the platform resolved itself. It matches no patterns,
  and it does nothing at all when no secret was registered (every `mock`
  run). So text is redacted again on the way out, whatever happened when it
  was written (`swarm_api.redaction`).

`GET /v1/tasks/{id}/artifacts/raw` is the one route that serves bytes rather
than JSON. Its output depends on what the bytes are:

| what the bytes are | served as | redacted |
|---|---|---|
| an image, where the name is `.png/.jpg/.jpeg/.gif/.webp` AND the magic bytes agree | the image type, byte for byte | no (the owner's decision); `X-Swarm-Redaction: not-applied` says so |
| text (no NUL in the first 4096 bytes) | `text/plain; charset=utf-8`, **whatever the name says** | yes, window by window; `X-Swarm-Redaction: applied` |
| anything else | `application/octet-stream`, always as an attachment | no, and the response says so |

Text is always served as text/plain. An `.html` or `.svg` file written by an
agent is shown as source and never rendered in the API's origin. SVG is
therefore on the text list and not the image list, because it is XML that can
carry script. The `nosniff`, `CSP: default-src 'none'; sandbox` and
`no-store` headers are a second line of defence in case anything does render.

**Errors come before the first byte**, as the usual JSON envelope:

* 404: the manifest does not list the name.
* 410 `artifact_gone`: the manifest lists the name but the object is gone.
  Bucket retention (`artifact_retention_days`: dev 14, default 90, prod 180)
  reclaimed it, or the upload never completed.
* 503: the store could not be read.
* 422: `disposition` is something other than `inline` or `attachment`.

After the first byte the status cannot change. A later failure ends the
chunked body without its final chunk, and `X-Artifact-Bytes` lets a client
see that the body came up short. There is no `Content-Length`, for the reason
in `CheckpointContent.download`: Cloud Run refuses an unchunked HTTP/1
response over 32 MiB.

Text is redacted in windows. Each window is cut at the last whitespace (a
newline if there is one), and the remainder is carried into the next window,
so every token is redacted whole. The concatenated windows equal the
redaction of the whole object. The one exception is a run with no whitespace
that is longer than 4 MiB: it is cut at that length, and a credential could
be split there. The paged routes withhold such a run instead, but a download
cannot withhold part of a file without corrupting it.

**Text that is not UTF-8 downloads as itself** (#188 review). A window is
decoded with `surrogateescape` and encoded back the same way, so a byte that
is not UTF-8 passes through exactly as stored and only a credential-shaped run
changes. It used to be decoded with `errors="replace"`, so a Latin-1 CSV came
down with every such byte turned into U+FFFD, and nothing said so. A JSON
string cannot carry such a byte, so the paged routes (`/artifacts/content`,
`/logs`) still show it as U+FFFD, but they now count it: `invalid_utf8_bytes`
on every window read, with a sentence in `detail` when it is not 0, and null
when nothing was read.

## Private keys are masked as blocks, not lines

The house filter's private-key rule masks the rest of the BEGIN marker's line,
because `sed` works one line at a time. On a raw NDJSON line that is the whole
key: JSON writes the key's newlines as the two characters `\n`, so the key is
one line. Once the line is **decoded**, the newlines are real, and the same
rule masked the BEGIN line and served the base64 body in clear. `/transcript`
and `/answer`, which redact after `json.loads`, were therefore weaker than
`/logs` over the same bytes (#188 review). A key printed as plain text
(`cat id_rsa`) had the same hole on every route.

`swarm_api.redaction.mask_private_keys` masks a key as a block. It runs before
every other rule, so no other rule masks pieces of a key's body first:

* **BEGIN … END**, within 64 KiB: from the marker through the END line.
* **BEGIN with no END.** In a string decoded out of JSON (`redact(decoded=True)`),
  the key is masked to the end of the string, which is what the line rule masked
  of the raw line that held it. In text, the rest of the BEGIN line is masked,
  then every following line shaped like a key body, stopping at the first line
  that is not.
* **END with no BEGIN**, for text that starts inside a key: the key material
  above the marker is masked.
* **A page that begins inside a key.** A page in the middle of a long key holds
  neither marker. The paged routes therefore read up to 64 KiB before a page's
  start. When that look-back finds a BEGIN with no END after it
  (`open_key_start`), the page starts on a whole line and its leading body
  lines are masked (`redact(inside_key=True)`). The download route carries an
  open key whole into its next window instead, so the key is masked in one
  piece.

Lines that a tool numbered (`cat -n`, or an agent's file-read tool) still count
as key lines. Every scan is linear, not one backtracking pattern: a lazy
BEGIN-to-END regex is quadratic on text with many BEGIN markers and no END,
and agent output can take that shape.

## Where an agent's deliverables go, and a task with no repository

The post-deploy QA of #184 found a hole the Artifacts tab could not show
around. `task_0e5b1f8b7bc1448fafdf`, a claude-code task with no repository,
wrote `answer.md` and `primes.txt` in its working folder. Only
`$SWARM_ARTIFACTS_DIR` was ever uploaded, so the task SUCCEEDED and its
Outputs held the runner's logs and nothing the agent made. No prompt had told
it where a deliverable goes unless a later workflow step expected one (#149).

The owner decided three things on 2026-09-26 (recorded on #184):

1. **Every claude-code and codex prompt ends with one line** naming the
   directory:

   ```
   Write deliverables to /workspace/<attempt>/artifacts ($SWARM_ARTIFACTS_DIR); files there are uploaded and shown in Artifacts.
   ```

   It is built once, by `expected_outputs.deliverables_line`, and appended by
   `with_instructions`, which `cliagent.run_cli_agent` calls for both runners.
   When later steps expect files, their list follows the line and says "write
   each one there", so the directory is named once. The path is absolute
   beside the variable's name, because an agent has already reported the
   variable as unset and written nothing. A prompt is no longer passed through
   byte for byte when nothing is expected; that was the old rule, and the
   tests that held it now hold the line instead. The line must never carry a
   word the rate-limit or credential heuristics look for: a CLI that echoes
   its prompt would turn it into evidence.
2. **A task with no repository also uploads what its agent CREATED in its
   working folder**, when the attempt ends (`agent_worker.standalone_outputs`).
3. **A repository task is unchanged.** Its diff or pull request is the
   deliverable, plus the artifacts folder. `result_summary.workdir_outputs` is
   absent on such a task, and nothing is named `workdir/`.

What decision 2 does, and the reason for each rule:

* **Which tasks.** No repository URL (read from the same two places the clone
  reads it), and a runner whose child is a provider's coding-agent CLI:
  claude-code and codex, the runners the line reaches
  (`runners.streams.cli_agent_spec`). Not `mock`, which writes `progress/`
  and `mock_state.json` into its working folder on every smoke test. Not
  `generic`, which its own module calls "the platform's escape valve for work
  that is not an agent". Not `browser`, which writes its outputs into the
  artifacts folder itself.
* **Created, not existing.** The worker lists `work/` once per attempt, just
  before the first runner starts, and uploads what is there at the end and
  was not there then. An in-place restart does not take a second list, so a
  first run's files are not "existing" to the second. A staged input
  (`input_from`) and the control files (`input.json`, `result.json`,
  `quota.json`, `credential.json`) were there first, and a file the agent only
  edited was not created by it.
* **A resumed attempt.** A checkpoint restores `work/`, so an earlier
  attempt's files are on disk before this runner starts. They still count as
  created, unless they are a staged input: the platform puts nothing else in a
  standalone task's `work/`. The manifest a reader sees is the final
  attempt's, and without this a task that parked once would show only what
  its last attempt wrote.
* **Named `workdir/<path>`**, in the same manifest as every artifact
  (`result_summary.artifacts`). The Artifacts tab lists and serves them
  through the routes above, with the same tenant check and read-time
  redaction, and no route changed. The prefix keeps a file of the same name in
  `$SWARM_ARTIFACTS_DIR` in its place (if the artifacts folder holds
  `workdir/<path>` itself, that file wins and the working-folder copy is
  listed with the reason). It also keeps #149's rejected option (c) rejected:
  a dependant's `input_from` and the expected-outputs check both match by
  exact name, so `scan-01.md` left in the working folder is
  `workdir/scan-01.md` and does not satisfy a later step that stages
  `scan-01.md`.
* **Caps: 50 files and 25 MiB per attempt** (the owner's numbers;
  `WorkerConfig.max_workdir_output_files` and `max_workdir_output_bytes`), and
  never past what `max_artifact_bytes` leaves. Files are taken shallowest
  first, then by path, so a top-level answer is not pushed out by a generated
  tree. A file that does not fit is **listed, never dropped silently**: in
  `result_summary.workdir_outputs.not_uploaded` as
  `{"name", "bytes", "reason": "over cap"}`, in `artifacts_skipped` (which
  the Artifacts tab already shows as "N over cap" with the names), and in one
  WARNING line. `not_uploaded` is capped at 50 entries like
  `artifacts_skipped`; `not_uploaded_count` is the whole number.
* **Skipped without a listing:** every name that starts with a dot, folder or
  file. The owner named dot folders; dot files are skipped too, because the
  CLI runners set HOME to the working folder, and the CLIs write their own
  state there (`.claude.json`, `.claude/`, `.codex/`), some of it describing
  the account the agent ran as. Also `node_modules`, `__pycache__`, a folder
  whose name ends in "cache" or "caches" or that holds `CACHEDIR.TAG` (the
  Cache Directory Tagging convention), and a virtual environment: `.venv`, or
  any folder holding `pyvenv.cfg`.
* **Symlinks are never followed.** The scan does not descend into a linked
  folder or read a linked file, and it counts them
  (`workdir_outputs.symlinks_skipped`). The read then opens each path
  component with `O_NOFOLLOW`, relative to the folder above it, and the file
  itself with `O_NONBLOCK`. A path swapped for a link after the scan (an
  orphaned agent process can still be running), or a FIFO swapped in for a
  file, is refused rather than followed or waited on. A working folder that
  was itself replaced by a link is not walked at all.
* **Redacted before upload as well as at read time.** Each file is copied into
  the worker's own scratch folder (`private/`, which is in no child's
  environment), scrubbed of registered secrets exactly as an artifact is, and
  uploaded from there. The agent's file is left as it was.

What this does not do: it does not tell the agent about the net. The line says
where deliverables go. A file caught in the working folder is a net under the
agent that did not listen, not a second place to write.

## Two kinds of stdout, and the label that was wrong

There are two processes, and each has its own streams:

* The **runner** is the process the worker starts. The worker captures it to
  `logs/stdout.log` and `logs/stderr.log`. Its stdout is usually empty, and
  its stderr holds the runner's own JSON log lines, such as "child started"
  with the argv and the cwd.
* The **agent** is the CLI that the runner starts as *its* child. The runner
  captures it into `artifacts/`: `claude-code.stdout.log` and
  `claude-code.stderr.log` for claude-code, `codex.*` for codex, and
  `command.*` for the generic runner. For claude-code, the agent's stdout is
  the transcript.

The old panel labelled "Output, as the agent wrote it" showed the runner's
streams. The log route now names both kinds. `stdout` and `stderr` still mean
the runner's streams, and naming no stream still returns those two, so
RunFiles and `swarm tail` are unchanged. `agent_stdout` and `agent_stderr`
are the agent's streams. The `stream` parameter can be given more than once.

The file names are defined once, in `agent_worker.runners.streams`. The API
cannot import the worker, so it restates the names in
`swarm_api.agent_streams`. The two copies are compared for every catalogue
profile by `tests/unit/control_plane/test_agent_stream_parity.py`.

For each agent stream, the worker now writes three objects:

1. **Live:** `logs/live/agent_*.tail.log`, republished every 5 s while the
   agent runs.
2. **Final:** `logs/agent_*.log`, a copy beside the runner's logs, uploaded
   when the attempt ends. The copy is not subject to `max_artifact_bytes`.
3. **Artifact:** the original capture file stays in `artifacts/`, because a
   downstream `input_from` stages it from there.

`result_summary.agent_streams` records which manifest entry holds which
stream. It is **`null`** for a runner with no agent child (mock, browser).
The key is present and null, and that is what lets a reader answer "not
applicable" instead of "absent".

An attempt made before this change has no live or final agent objects. For
that attempt only, the log route falls back to the manifest entry named by
the runner's convention and serves it as `source: "artifact"`. The reference
task, `task_73b5f4d9ca3641fbb914`, is such an attempt. The fallback is used
only for the attempt the manifest describes. The manifest belongs to the
final attempt, and an earlier attempt must not be shown the final attempt's
output as its own.

## Nothing was live: `read1`, not `read`

`procman.StreamCapture.pump` read each pipe with `read(65536)`. The pipe is a
`BufferedReader`, and `BufferedReader.read(n)` blocks until *n* bytes arrive
or the pipe closes.

In a test, a child wrote and flushed a 392-byte line at t=0. The line reached
the capture file only when the child exited, 3.1 s later. Both captures use
this class: the worker's capture of the runner, and the runner's capture of
the agent. So no stream under 64 KiB existed on disk while an attempt ran,
and the live-tail publisher had nothing to publish. Two staging tenants had
118 attempts with final logs between them and **zero** `logs/live/` objects.

The fix is `read1(65536)`, which returns whatever one read of the pipe yields
as soon as there is any data. It is pinned by
`tests/unit/worker/test_live_capture.py`.

## claude-code prints stream-json

With `--output-format json`, the CLI prints ONE object when it finishes and
nothing before that. The reference task ran for 89.6 s and seven turns, and
none of those turns were recorded: its stdout and `claude-transcript.json`
hold the same final object, one of them re-indented.

The default is now `--print --output-format stream-json --verbose`.
`CLAUDE_CODE_ARGS` still overrides it; that value is set by the platform,
never by a caller. The CLI requires `--verbose` alongside stream-json in
print mode. With stream-json, stdout is NDJSON with one event per line:

* the init record
* every assistant block and tool call
* every tool result
* the `rate_limit_event` readings that the account pool wants
* the same `result` event, last

The switch has a consequence for rate-limit detection. Under stream-json, the
agent's stdout holds the whole conversation, and an agent that reads a file
about HTTP 429s prints every marker that `cliagent.detect_rate_limit` looks
for. A failed run would then have been parked as rate-limited instead of
failing. For a streamed run, the heuristic therefore reads everything except
two kinds of event:

* the agent's own `assistant` and `user` events, unless the CLI flags one as
  an API error;
* `rate_limit_event` readings whose status is `allowed`.

The result event, system events, non-JSON lines and stderr are still read.

**The transcript artifact is written whole, or not at all.** It used to be
cut at 4,000,000 characters, mid-token, into JSON that nothing could parse.
Past that size it is now omitted, and `agent_streams.transcript_skipped:
"too_large"` records why. The agent's stdout is the canonical transcript in
every case.

## A capped stream keeps its end

Under stream-json, claude-code's stdout is the whole conversation, including
every tool result. It is still captured under `max_stdout_bytes` (32 MiB). The
capture used to write the first 32 MiB and discard the rest. The `result`
event is always the last line, so a long session lost it (#188 review). With
it went four things:

* the answer, so `/answer` fell back to the runner's 2,000-character summary;
* the spend, so the attempt read "not reported";
* the evidence that the rate-limit decision after a successful exit reads;
* the fact of the cut itself, which nothing reported.

Now the runners capture their agent's streams with `keep_tail`
(`procman.StreamCapture`):

* **The start and the end are kept.** The first `limit - tail` bytes are
  written as they arrive, and those are what the live tail shows. The last
  `tail` bytes (1 MiB, and never more than half the cap) are held in memory.
  When the stream closes, they are written after a notice that starts
  `[swarm] output truncated` and counts the bytes dropped. The kept end
  starts on a whole line, so an NDJSON reader loses only the line the cut
  went through. The file stays within the cap plus the notice.
* **The live file says so while the stream runs.** When the head fills, a
  pending notice is written, so a live view that stops growing does not read
  as an agent that stopped printing. If the stream then closes within the
  cap, the pending notice is replaced by the rest of the stream, and nothing
  was dropped.
* **The cut is reported on every outcome.** The runner puts its capture report
  in `RunnerContext.report`, which every `write_result` carries, including a
  failed or parked run. The worker writes `stdout_truncated` and
  `stderr_truncated` into `result_summary.agent_streams`, with null meaning
  "not reported", never "not cut". The transcript artifact of a cut stream is
  omitted with `transcript_skipped: "capture_truncated"`. A document built
  from a stream with its middle gone would be valid JSON that says nothing
  about the gap.
* **The notice is never rate-limit evidence.** It counts bytes, and `429` is
  a marker that matches anywhere in the text. The line the cut went through
  is not evidence either.

git's captures keep the old behaviour, the head and a notice. The patch
harvest judges a patch by the size of its file, so a patch with its middle
removed must never look like one that fitted. There is one fix to the old
behaviour: a cap reached exactly at the end of one read used to drop the rest
of the stream without writing the notice.

## The live tail

The live tail has four properties:

* **It is a window, not the file.** GCS has no append operation, so
  republishing the whole stream would make the cost of watching a run grow
  with the run's length. Each tail is the last `live_log_tail_bytes`
  (256 KiB), scrubbed of registered secrets, and republished every
  `live_log_interval_seconds` (5 s) even when nothing has changed. The
  object's GCS `updated` time therefore shows that the publisher is alive,
  and "no new output" shows up as an unchanged `size` across two polls.
* **It starts on a whole line.** When the stream is longer than the window,
  the window moves forward past the first newline, so every NDJSON line in it
  parses. The exception is a window that lies entirely inside one long final
  line: that window keeps the partial line, because an empty tail would hide
  the newest output.
* **Its header carries the publish time:** `#swarm-tail offset=<raw byte>
  size=<raw size> at=<RFC 3339 UTC>`. Readers must accept a header without
  `at=`, because every tail published before this change lacks it.
* **It is read with one retry.** A read that straddles a republish is
  ordinary, not a fault. The GCS reader pins each download to the generation
  it looked up. The API retries a replaced live object once, and if it is
  replaced again the API reports the read as unreadable. It never serves a
  window spliced from two objects.

**Cost.** Each running agent does one object write per non-empty stream per
interval. At 5 s and 40 agents, that is up to 32 writes a second (about
2.8M class-A operations a day) when all four streams have output. Raise the
interval before raising concurrency.

**Extent.** The tail is bounded, so the live view of a long run shows only
its recent steps and says "earlier steps are not in this window". The
complete transcript appears when the attempt ends. Serving the complete
transcript live would require compose-appending to a GCS object. That is an
open question for the owner.

## The transcript and the answer

`GET /v1/tasks/{id}/transcript` parses the same object that
`/logs?stream=agent_stdout` would serve.

* **Redaction runs after decoding.** A credential written with a JSON escape
  (`ghp_…`) becomes a credential once decoded, and a regex over the raw
  line never sees it. The server therefore decodes first and then redacts
  every string field. Each field is capped at 16 KiB, and a capped field is
  named in `truncated_fields`.
* **Some content is never sent.** Image bytes inside a tool result are
  counted, not served, because base64 in a JSON string would bypass the raw
  route's image handling. The raw record is served only on request.
* **The format is detected, not trusted.** It is one of `claude-stream-json`,
  `claude-json` (a single result object from an attempt made before the
  switch), other `ndjson`, or `text`, which has no steps.
* **Windows are cut on newlines only.** A line longer than the window becomes
  one step with no text, and paging moves past it. An unparseable line is
  counted in `skipped_lines`; it is never dropped silently.
* **Decoded strings are masked at least as far as their raw line.** Every
  string is redacted with `decoded=True` (see the section on private keys).
* **A cut capture is never complete.** The capture's notice is a `system` step
  with `meta.subtype: "capture_truncated"`, not a skipped line.
  `capture_truncated` is `true` when the window holds the notice or the worker
  reported the cut. It is `false` when the worker reported no cut, or when the
  read covered the whole final object and found no notice. Otherwise it is
  `null`. When it is `true`, `complete` is false and `stream.detail` says why.
  `/answer` carries the same flag. The capture keeps the end of the stream, so
  a cut capture still answers with the whole result, and `detail` says the
  rest of the run was not all kept.

`GET /v1/tasks/{id}/answer` serves the LAST `result` event's `result`, found
in the final agent log, then the pre-change artifact, then the live tail.
It does not use `result_summary.runner.summary`, which `cliagent._summarise`
cuts at 2,000 characters. The reference task's answer is 7,124 characters.
The summary is used only when no result event exists, and it is then marked
`complete: false` when it is at the cap.

The answer route gives one of four answers:

* `ok`: the answer is here. An agent that reported an error is still `ok`,
  with `is_error` set.
* `not_yet`: the attempt is still running and has no result yet.
* `absent`: the attempt ended and left nothing.
* `unreadable`: a read failed, and nothing further down the chain is tried.

## CPU in Details: peak and mean of the limit

The worker already sampled CPU (`agent_worker.metrics`), but nothing
displayed the figures. The mean existed only after a runner stopped, and the
limit was recorded nowhere.

The frozen `Attempt` has no CPU fields, so until contract request #15
(amended for this) is decided, the figures ride on the HEARTBEAT events'
`detail`. They include peak cores, mean cores (now kept current while the
runner runs), CPU-seconds, and the CPU limit with its source. The limit is
read from cgroup v2 `cpu.max`. When that is unavailable, the worker uses the
catalogue cpu of the class the container was *sized* with, which is the
task's own class and not the profile's.

A reading with `final: true` is emitted when each runner is reaped. A fenced
attempt never emits one, because the event stream belongs to the generation
that owns the task.

`GET /v1/tasks/{id}/attempts?include=usage` is opt-in, because the Overview
calls that route once per task. It serves the newest reading per attempt
from one descending read of the task's events. Each reading has a status:
`final`, `live`, `last_reading`, `never_ran`, `absent`, `beyond_window`, or
`unread`. None means "not measured" and is never drawn as zero.
