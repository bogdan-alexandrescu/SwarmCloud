# What an agent took in and produced: the read path behind the Artifacts tab

The task drawer has three panes: Details, Attempts and Artifacts. The
Artifacts pane shows a task's inputs, its outputs and its logs, and updates
every 5 seconds while the task runs (#184, owner decisions of 2026-09-25).
This document covers the backend half: what the worker writes, what the API
serves, and the reasons for both. Each section records a constraint that a
later change could silently break.

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

## The task's input is masked too, with a count

The owner decided on 2026-09-25 (#184) that the Artifacts pane's Inputs and
the drawer's Details show "a read-time-redacted copy of the task's input,
with 'masked N', like every other output". Both used to draw `task.input`
straight off `GET /v1/tasks/{id}`. The Artifacts pane said so
(`as submitted · not masked`) and Details said nothing. A token pasted into a
prompt was drawn in clear on the two screens that mask every other byte they
show.

`GET /v1/tasks/{id}/input` (`swarm_api.task_input`) serves three texts, each
with its own `redaction_count`:

* `prompt`: `input.prompt` when it is a string. It is masked as one decoded
  string, the way `/answer` and `/transcript` mask the strings they decode
  out of JSON, so a private key with no END is masked to the end of the
  string.
* `rest`: the input without that prompt, as its JSON text.
* `full`: the whole input, as its JSON text.

The redactor is `redaction.redact`, the one every other route uses. It is
not a second one. The JSON is masked as text, not value by value, because the
key/value rule is what catches `"api_token": "<a value with no recognisable
prefix>"`, and that rule needs the key beside its value. `prompt_key`
(`string`, `missing`, `other`) says what shape the input has, so a screen
never goes back to the raw document to find out.

`GET /v1/tasks/{id}` still serves the input as submitted. The caller is the
tenant that submitted it, and the CLI and MCP clients read it. Whether that
route should mask too is a separate decision, recorded on the PR, and this
change does not make it.

The UI reads the copy once per task, because an input never changes. A copy
that could not be read, or an API that does not serve one, is drawn as that.
The raw input is never drawn in its place.

## CPU in Details: peak and mean of the limit, as typed attempt fields

The worker already sampled CPU (`agent_worker.metrics`), but nothing
displayed the figures. The mean existed only after a runner stopped, and the
limit was recorded nowhere.

#188 put the figures on HEARTBEAT events, because the frozen `Attempt` had no
CPU fields. `GET /v1/tasks/{id}/attempts?include=usage` read them back with
one descending events read per request. That was the interim path.

**Contract request #15 was accepted on #184 (2026-09-25), and it replaces the
interim path.** `Attempt` carries four fields: `cpu_seconds`,
`peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores`. Each is the
attempt's, meaning every runner it started, combined.

* **The worker writes them on its own attempt document**
  (`control.record_cpu_usage`). It writes them with each periodic reading
  while a runner runs, which is every fifth heartbeat, the same cadence as
  the HEARTBEAT event. It writes them again when each runner is reaped. A key
  that was not measured is left out, not written as null, because the write
  is a merge and a null would erase an earlier figure. Nothing is written
  when nothing was measured, not even the limit.
* **The limit** is cgroup v2 `cpu.max` when that says anything. Otherwise it
  is the catalogue cpu of the class the container was *sized* with, which is
  the task's own class and not the profile's. Which of the two supplied it is
  no longer recorded (see below).
* **The HEARTBEAT event is back to what it carried before #188:**
  `elapsed_seconds`, `peak_rss_bytes`, `checkpoints`, the cumulative
  `cpu_seconds` and `cpu_source`. The reconciler's stuck-browser judgement
  (`reconciler.progress`) turns two consecutive `cpu_seconds` into a rate,
  and it is the reason those two stay. The `final` HEARTBEAT that #188 emitted
  when a runner was reaped is gone.
* **The API serves the four fields on every attempt row** (`attempt_to_api`),
  with no opt-in, because serving them costs no read the route was not already
  making. `include=usage` is no longer read. `swarm_api.attempt_usage`, the
  events reader, is removed. `attempt_is_over`, which `/answer` also uses,
  moved to `swarm_api.attempt_state`.
* **The attempt document is not fenced** for these writes, like the memory
  peaks beside them. It is the attempt's own document, and the fence guards
  the task, its lease and its event stream, none of which this touches. The
  tenant-mismatch exit writes nothing.

### What the typed fields cannot say

The four accepted fields carry no time and no limit source. So:

* **A live reading has no age.** Details calls a running attempt's figures a
  `live reading` and says `age not recorded`. The #187 review moved the CPU
  age onto the server's clock, and there is no server time to age now. An age
  taken from the browser's clock would be a guess drawn as a measurement.
* **The limit's source is not known.** Details names the class when the class
  read on the page has the same cpu as the limit. Otherwise it says
  `reported limit`. It no longer says `cgroup limit`.

Contract request #26 asks for both, and the owner decides.

### Attempts from before the typed fields

Attempts that ran between #188's deploy to dev (about 23:08 UTC on
2026-09-25) and this change's deploy carry their CPU only on HEARTBEAT
events. A read-only look at dev's Firestore at 23:16 UTC that day visited the
40 newest tasks. It found one such task, `task_49dde08b768b49748588` (mock,
the #188 deploy's smoke), with two interim readings, one of them `final`.
Every attempt that runs before this change deploys adds another.

Their typed fields are null, and "never measured" would be false of them. So
Details keeps one small legacy reader, `interimReading`, in the UI. For an
attempt whose typed fields carry nothing, it takes the newest HEARTBEAT on the
drawer's own event page that carries the interim keys, and labels it
`heartbeat event`. It adds no server read, because the page is already held.
Because it is oldest-first and capped, a long attempt's final reading can be
past it, and then the row says `never measured` with a mark that names the
cause. It reads nothing for any attempt the worker wrote typed fields for. It
can go once no attempt from that window is still opened.

Keeping the server-side reader for those attempts was the alternative. It was
not chosen, because the owner's decision was that the typed fields replace
the path. That reader cost an events query per drawer read, for a window of a
few hours on dev.
