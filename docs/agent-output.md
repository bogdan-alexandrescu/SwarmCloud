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
   Files written to /workspace/<attempt>/artifacts ($SWARM_ARTIFACTS_DIR) are uploaded and shown in Artifacts.
   ```

   **It is information, in the owner's words, not an order.** The first build
   said "Write deliverables to ...", from a paraphrase of the decision. That
   order reached every repository task too, and decision 3 says a repository
   task's deliverable is its diff or pull request: an agent asked to write
   `docs/design.md`, and told last thing to write deliverables to the
   artifacts folder, can put the document there and leave the pull request
   empty (#225 review). A test holds a repository task's prompt to this one
   line and no order.

   It is built once, by `expected_outputs.deliverables_line`, and appended by
   `with_instructions`, which `cliagent.run_cli_agent` calls for both runners.
   When later steps expect files, their list follows the line and says "write
   each one there" -- the one order the text gives, because a dependant cannot
   run without those files (#149) -- so the directory is named once. The path
   is absolute beside the variable's name, because an agent has already
   reported the variable as unset and written nothing. A prompt is no longer
   passed through byte for byte when nothing is expected; that was the old
   rule, and the tests that held it now hold the line instead. The line must
   never carry a word the rate-limit or credential heuristics look for: a CLI
   that echoes its prompt would turn it into evidence.
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
  `{"name", "bytes", "reason": "over cap"}`, drawn in Artifacts > Outputs
  under "not uploaded from the working folder" with each file's reason, and
  in the worker's WARNING lines.
* **Every name is somewhere.** `not_uploaded` holds the first 50 entries and
  `not_uploaded_count` the whole number, because the summary is a Firestore
  document with a 1 MiB limit. The worker's log names every file it did not
  upload, 100 to a WARNING line (a line stays well under Cloud Logging's
  256 KiB entry), and the tab says how many only the log names. The first
  build logged the first 50 too, so past 50 a name was nowhere.
* **Not in `artifacts_skipped`.** Every reader of that list reads a name in it
  as a file of the artifacts folder: the tab's note on that folder's file list
  ("dropped at the size cap" until #228, "skipped" since), a dependant's
  "exceeded its artifact size cap". A working-folder file
  is dropped by another cap, or for a reason that is no cap at all, so the
  first build's copy there mislabelled a file the 50-file cap dropped.
* **Listed and not uploaded, with the reason** (#225 review):
  * *A name that is not UTF-8* (`name is not valid UTF-8`). `os.walk` decodes
    such a name into a str holding lone surrogates. GCS names objects in
    UTF-8, a protobuf string field -- so a Firestore write -- cannot carry a
    lone surrogate, and the worker logs UTF-8 to stdout. One such name in the
    summary made `finish` raise, and the next attempt restored the same file
    from the checkpoint and failed the same way, until the task had spent
    every attempt. The name is listed as the bytes it was, `caf\xe9.txt`,
    spelled the way `ls -b` and the API's checkpoint listing spell it. That
    is a display and never a key. The artifacts folder had the same hole: a
    file there with such a name is not uploaded, and is named the same way in
    `artifacts_skipped` and the log.
  * *A name too long to be an object's* (GCS allows 1,024 bytes of UTF-8 for
    the whole key, the attempt's prefix included). Listed with its name cut to
    256 characters, so 50 of them cannot take the summary near 1 MiB --
    scrubbed of any registered secret BEFORE the cut, not after, so a key
    that happens to cross that character cannot survive in the fragment kept
    (#232 review).
  * *A core dump*: `core` or `core.<pid>`, at any depth. A program the agent
    built and ran crashes with the working folder as its cwd, and its core
    holds its memory, environment block included, which inherited the CLI's
    credential. Listed, so a reader learns that something crashed.
  * *A file holding a registered secret the worker could not redact*, or one
    it could not scan. See the last rule below.
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
* **A file the rewrite cannot clean stays in the pod.** For
  `$SWARM_ARTIFACTS_DIR` the trade is to upload a binary file as-is, even when
  its raw bytes hold a registered value, and report it in
  `redaction_skipped`: the agent chose to deliver that file, and corrupting it
  to take a key out is worse. The working-folder upload is a net under files
  nobody chose to deliver, so that trade does not carry over. A file whose raw
  bytes hold a registered value after the rewrite declined it (`holds a
  registered secret that could not be redacted`), or that could not be
  scanned at all, is not uploaded, and is listed with the reason. A binary
  file with no registered value in it -- a PNG the agent drew -- is uploaded
  as before. Binary files are served raw with `X-Swarm-Redaction:
  not-applied`, so read-time redaction would not have covered this one.

What this does not do: it does not tell the agent about the net. The line says
what happens to a file written to `$SWARM_ARTIFACTS_DIR`. A file caught in the
working folder is a net under the agent that wrote somewhere else, not a
second place to write.

## At most 500 files from `$SWARM_ARTIFACTS_DIR`

Found by reading on 2026-09-26 (#228), not measured: every file in the
artifacts folder that fitted `max_artifact_bytes` became one entry in
`result_summary.artifacts`, with no count cap. `result_summary` is a field of
the task's Firestore document, which Firestore limits to 1 MiB. An entry is
about 160 bytes at ordinary name lengths, so an agent that left about 6,000
small files there would have taken the document past the limit: `finish`
refused, `_safe_finish` refused again, and the task lost a result that may
have succeeded. The fake Firestore the unit tests use does not enforce the
limit, which is why nothing caught it. Since #225 every CLI prompt names the
folder, so more agents write there.

The owner decided on 2026-09-26, recorded on #228:

* **At most 500 files are uploaded per attempt** (`artifact_manifest.MAX_FILES`,
  `WorkerConfig.max_artifact_files`). The rest are not uploaded. The summary
  **counts** them, `artifacts_over_cap`, beside `artifacts_cap_files`, the cap
  they were over, and lists none of them. Both keys are absent when every file
  fitted, so a reader never draws a zero as something dropped.
* **Every name is in the worker log**, 100 to a WARNING line headed "files in
  $SWARM_ARTIFACTS_DIR were not uploaded", each as `<name>: not uploaded: over
  cap`. It is the same helper #225's working-folder cap logs through
  (`Worker._log_not_uploaded`), so the two caps cannot drift apart.
* **Artifacts > Outputs** shows "not uploaded from $SWARM_ARTIFACTS_DIR" with
  the count, and #225's count line: "N over the 500-file cap, named in the
  worker log". The cap is read from the summary, not restated in the UI.

The rules this needed, and why:

* **Which 500.** The names a later step's `expected_outputs` declares come
  first, so a declared output is never the one dropped. There are at most 49
  of them for one task: each dependant's `input_from` maps one upstream step
  to one filename, and a workflow has at most 50 steps including this one.
  Then the files the platform writes into the folder: the agent CLI's stream
  captures and transcript, which `agent_streams` and the tab's answer and
  transcript read, and the harvest's patch. Then everything else, shallowest
  first and then by path, the rule #225 chose for the working folder, so a
  top-level report is not pushed out by a generated tree beside it. The byte
  cap is applied in the same order.
* **The manifest is still listed by path.** The order decides which files
  are taken. It does not change the order a reader sees them in, or which of
  them the listing route's first page holds.
* **A name longer than 256 bytes is not uploaded**
  (`artifact_manifest.MAX_NAME_BYTES`) -- UNLESS a later step declared it
  (#232 review, `artifact_manifest.MAX_DECLARED_NAMES`). A declared name is
  only put first, not excused from upload: holding it to this bound too made
  a declared output over 256 bytes fail every attempt as "written but not
  uploaded" -- worse than before #228, because `_publish_withheld` had by then
  already published, so the retry's push was refused as a non-fast-forward. A
  declared name is held only to GCS's own object-name limit, 1,024 bytes,
  same as the pre-#228 code and same as any other upload that is too long for
  GCS: it fails at upload time and is counted in `artifacts_skipped` like any
  other upload error, not turned away here.
* **The 256-byte bound exists because the count cap alone does not bound the
  document.** Every entry carries its name twice, as `name` and at the end of
  its `uri`, and GCS allows 1,024 bytes of object name, so 500 entries at that
  length are about 1 MB on their own. At 256 bytes, an entry is at most
  702 bytes; 500 of them would be 343 KiB, but up to 49 are exempt (above) and
  held only to the GCS length instead, so the manifest's worst case is 49
  entries at about 2,238 bytes (107 KiB) plus 451 at 702 bytes (309 KiB):
  about 416 KiB. The worst case of everything else the document can hold is
  the input (256 KiB -- see the caveat below), #225's working-folder block
  (about 155 KiB), the two skipped lists (about 67 KiB) and the runner
  envelope (about 12 KiB). That is about 906 KiB in all, leaving about
  118 KiB. At 512 bytes the manifest alone would pass that, and the sum would
  pass 1 MiB. 256 bytes is a nested path: Linux allows 255 bytes for one file
  name. A name over the bound (not declared) is logged with the reason `name
  is longer than the manifest's 256 bytes`, and listed in `artifacts_skipped`,
  both cut to 256 characters as #225 cuts a name it lists -- scrubbed of any
  registered secret BEFORE the cut, not after, so a key that happens to cross
  that character cannot survive in the fragment that is kept (#232 review). A
  path can be 4,096 bytes, and 100 of those would pass Cloud Logging's 256 KiB
  entry.
  * **The caveat this budget does not cover (#232 review, tracked on the wave
    epic, not fixed here):** "the input costs at most 256 KiB" assumes the
    submitted bytes are roughly what Firestore ends up storing. Firestore
    counts a number as a flat 8 bytes whatever its digit count, so an input
    dense with small integers can cost more once decoded than the same count
    of submitted bytes as text. This is the API's input-size check to
    tighten, not this cap.
* **Only what leaves the pod is redacted.** The pass that scrubs registered
  secrets now runs over the files that will be uploaded, not the whole
  folder. A file past the cap is not rewritten, and it is not reported in
  `redaction_skipped` as "uploaded as-is", because it was not uploaded. The
  agent CLI's captures are scrubbed either way, because a copy of each always
  goes to `logs/`.

`tests/unit/worker/test_artifacts_manifest_cap.py` holds each rule. The size
bound is measured with Firestore's own size rules, against a store that
renders the longest URI a deployment makes.

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

The redactor is `redaction.redact`, the one every other route uses, with its
one set of rules. It is not a second one. It is applied by
`redaction.JsonMasker`, which reads the input's **structure**:

1. Every string, the prompt included, and every key, is masked as one decoded
   string, the way `/answer` and `/transcript` mask the strings they decode.
2. A value under a key that **names a credential** is masked whole, as one
   leaf, counted once. The keyword may sit anywhere in the key, in any case,
   with `-` or `_` inside the two-word names: `api_token`,
   `AWS_SECRET_ACCESS_KEY`, `private_key`, `password_hash`, `secrets`,
   `x-api-key`. That covers a string, a list or an object holding one (a token
   split into lines, `{"value": "..."}`), and a number when the keyword ends the
   key (a PIN under `password`). It never covers `null`, `""`, `[]`, `{}` or a
   boolean, which cannot be credentials and are drawn as sent and not counted.
   A number under `input_tokens`, `max_tokens` or `credential_revoked_times`
   (the mock runner takes that one) is a count, not a credential.
3. A private key stored as a **list of lines** is masked from the element with
   its BEGIN marker through the one with its END, one count, because no body
   line holds a marker.
4. A literal that any of those masked (a value under a credential's name, or a
   value the key/value rule found beside `NAME=` in a string or a key) is
   masked **wherever else the input holds it**, the prompt included. At most
   256 literals are carried, so an input built to hold thousands cannot buy
   that many passes over itself.

The served text is `json.dumps(indent=2)` of the masked value, so it is always
the input's JSON.

**Why not a rule over the JSON text.** The route first served one `redact()`
over the JSON text, and the PR #210 review found the hole. JSON text writes a
quote inside a string as `\"`, and the key/value rule reads a quote as a
quote. A prompt holding `{"api_key": "<bare>"}` or `PASSWORD="<bare>"` came out
masked in `prompt` and in clear in `full`, under a count of 0. The fix-up
masked every string and then ran the rules over the JSON text with the strings
stood in. The re-review found that the rule's value class, the first run of
characters after the key, was a list's `[` or an object's `{` under
`password`: the strings inside were served under `masked 1`, the text stopped
being JSON, and `null`, `true`, `[]` and `{}` were each counted as a
credential. A key holding `=` pushed the mask onto the `:` after it. Each of
those is a question about the document's structure, so the structure answers
it now.

**The blocks agree.** One masker is built over the whole input and shared by
`prompt`, `rest` and `full`. Before, a password named in the rest was
`"********"` in the rest block and in clear in the prompt block directly above
it. Each string is masked once, so `full`'s count is always `prompt`'s plus
`rest`'s.

**The key/value rule is anchored.** Its name prefix was re-scanned from every
position of a long run of name characters, so 40,000 characters took about a
minute, and this route feeds it up to 256 KiB of a caller's input, holding the
GIL of an instance all tenants share. The rule now starts a match only where a
run of name characters starts, which masks exactly what it masked before. A
256 KiB run takes about 25 ms. The same change lets the rule take `x-api-key:`,
which the module's docstring had claimed and the rule did not catch; the
shell filter in `scripts/lib/common.sh` still does not take the hyphen.

`prompt_key` (`string`, `missing`, `other`) says what shape the input has, so
a screen never goes back to the raw document to find out. Under a string
prompt, both panes draw `rest` below it. They draw `full` only when there is no
prompt string, so the prompt is never on screen twice.

`GET /v1/tasks/{id}` still serves the input as submitted. The caller is the
tenant that submitted it, and the CLI and MCP clients read it. Whether that
route should mask too is a separate decision, recorded on the PR, and this
change does not make it.

The UI reads the copy once per task, because an input never changes. A copy
that could not be read, or an API that does not serve one, is drawn as that.
The raw input is never drawn in its place. A failed copy is asked for again
with the next poll of the pane that draws it, and the read is Details' own:
it is not part of the drawer's `loadAgentRun`, so a slow copy blanks only the
input blocks. Details' metadata table is not masked; it draws
`task.metadata` as submitted, and whether it should be masked is a decision
for the owner, recorded on PR #210.

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
  the HEARTBEAT event. It writes them again when each runner is reaped, and
  on every exit beside the attempt's spend (`_upload_outputs`, `_cleanup`),
  which covers a runner still alive at a crash or killed at cleanup without
  being reaped. A failed memory-peak write no longer skips the CPU write after
  it: the memory peak is telemetry too, and its failure is logged. A key
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

**Attempts from before #188 were measured too.** Every HEARTBEAT before #188
carried `cpu_seconds` and `cpu_source`, and no cores. #188's reader drew those
cpu-seconds on main. The PR #210 review found that the legacy reader asked for
`peak_cpu_cores` only, so those attempts read `never measured`, with a sentence
saying no heartbeat on the page carried a figure while the page held one.

Their typed fields are null, and "never measured" would be false of both kinds
of attempt. So Details keeps one small legacy reader, `interimReading`, in the
UI. For an attempt whose typed fields carry nothing, it takes the newest
HEARTBEAT of that attempt on the drawer's own event page that carries a
**figure**, and labels it `heartbeat event`. A key with no value does not
count. When that event has no cores, the peak and mean rows draw em dashes, the
mean row keeps the cpu-seconds, and the strip says `cpu-seconds only`. The
reader adds no server read, because the page is already held. The page is the
task's first 200 events, oldest first, so on a long attempt the newest reading
on it can be an early one. The mark says so, and when the page is full (200
events) and the reading is not the one taken at exit, the strip says
`page full; newer readings may exist`. When no heartbeat on the page carries a
figure the row says `never measured` without claiming anything about events
past the page. When the event read FAILED, the row says `events not read`
with the not-read mark, because nothing can be said about a page nobody read.
The reader does nothing for an attempt the worker wrote typed fields for.

Neither kind of attempt ages out, so the reader stays as long as those attempts
can be opened. The owner can decide to drop it; the cost is that every attempt
from before this change reads `never measured`.

Keeping the server-side reader for those attempts was the alternative. It was
not chosen, because the owner's decision was that the typed fields replace
the path. That reader cost an events query per drawer read. The UI reader
costs nothing, because it reads the page the drawer already holds, and the
price is that it sees only the task's first 200 events.
