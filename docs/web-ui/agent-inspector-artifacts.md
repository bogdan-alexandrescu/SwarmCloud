# The agent inspector's Artifacts pane, and CPU in Details (#184)

What the task drawer shows about what an agent took in and produced, where each
figure comes from, and the constraints behind the choices. The owner's
decisions of 2026-09-25 are on issue #184; this file records how the UI keeps
them and why, so they are not quietly undone.

## Why it exists

On `task_73b5f4d9ca3641fbb914` (claude-code, SUCCEEDED) the agent's
7,124-character answer was inside the artifact `claude-code.stdout.log`, and
the drawer listed that file by name beside `copy gsutil`. The panel headed
"Output, as the agent wrote it" showed the **runner process's** streams: an
empty stdout and one `child started` JSON line on stderr. The answer was never
shown, nothing rendered an image, and Details had no CPU figure although the
worker measures CPU all along.

## The three panes

The drawer's segmented control reads **Details** (renamed from "Detail"),
**Attempts** and **Artifacts**. The route id of the first stays `detail`: it is
an address, and renaming it would move every saved link for no reader's
benefit. The new pane's address is `#work/task/<id>/artifacts`.

## Artifacts: Inputs, Outputs, Logs

| Section | Source | Notes |
|---|---|---|
| Prompt | `GET /v1/tasks/{id}/input` | The prompt as the API **masked** it, wrapped, and the rest of the input as JSON below it; otherwise the whole input as JSON. `masked N` beside the eyebrow counts what the block draws. `no prompt` only when the key is missing. Never `task.input`, and never the raw input in place of a copy that could not be read. See [The input, masked](#the-input-masked). |
| Repository | `task.repository_url`, `repository_ref`, `result_summary.git.base` | Nothing when no repository was asked for. The cloned commit is `reported at finish` while the task runs. |
| Staged files | `metadata.input_from` joined with `result_summary.staged_inputs` (`dag.ts` `taskInputsOf`) and, for a workflow step, `GET /v1/workflows/{id}` | Each file links to the upstream run's own Artifacts pane, labelled with its step. The file is read from the **upstream** task's artifact routes, because the staged copy is never uploaded separately. An upstream object removed since reads `removed from the upstream run`, and the size shown stays the staged size. |
| Answer | `GET /v1/tasks/{id}/answer` | First in Outputs, rendered with the existing Markdown renderer (React elements, never HTML). `not yet` while running, `no answer recorded` after an end with none. A runner-summary fallback is marked, because that summary is capped at 2,000 characters. |
| Files | `GET /v1/tasks/{id}/artifacts?limit=200`, then `result_summary.artifacts` for anything past that page | The viewer is chosen by the server's `kind`, not by a name rule in the client. Images are drawn from the raw route; binary files are described. Every row has `download` (the raw route) and `copy gsutil`. Before the last attempt ends the list is `uploaded when the attempt ends`, never "none". See [One page of the listing](#one-page-of-the-listing-and-the-rest-of-the-manifest). |
| Logs | `GET /v1/tasks/{id}/transcript`, `GET /v1/tasks/{id}/logs?stream=…` | `transcript` (steps), `stdout` and `stderr` (the agent CLI's own), and `runner (platform)`: the runner process's streams, which the Details panel used to present as the agent's output. |

### Every byte through the API

Reads and downloads go through the API, never a signed GCS URL, so read-time
credential redaction and the tenant check apply to every byte. Images pass
through as stored. An image, `open full` and `download` are anchors or `<img>`
pointing at `/v1/tasks/{id}/artifacts/raw`, which the browser fetches with the
session cookie every read already carries. They are **not** `fetch.ts`
`read()` calls: that function treats any non-JSON 2xx as an expired session,
which is correct for the JSON routes. An image that fails to decode (an
expired sign-in arrives as a page) reads `image could not be loaded`, never a
blank box.

Nothing in the client builds an object key, a prefix or a `gs://` URI to send.
A file is named by the name its manifest spells, on the task that owns it.

### One page of the listing, and the rest of the manifest

The listing route goes through `paged_limit`, like every list route. A request
with no `limit` gets `default_page_size` (50), and any other limit is clamped to
`max_page_size` (200). `store.list_artifacts` returns `manifest.artifacts[:limit]`
with `complete: true` and nothing saying it cut the list, and the route takes no
page token.

The pane's first version sent no `limit`. A browser run that took 60 screenshots
drew 50 rows and a chip reading 50, with no mark. The last ten screenshots, which
were the final page states, could not be viewed or downloaded from the pane.
The Details pane's own file list, which read the manifest off the task, showed
all 60. So the pane drew a partial read as if it were the whole list. (Details'
list has since been removed, as a duplicate of this one: see
[Details, after the follow-up](#details-after-the-follow-up).)

It now asks for `limit=200` (`ARTIFACT_PAGE_LIMIT`). Any file past that page is
listed from the task's own `result_summary.artifacts`, which is the same record
the route serves. The note beside the count reads `N of M listed`, with the
`partial` mark. A row past the page reads `from the task's manifest · kind not
served`, because the server named no kind for it and the client does not keep a
second copy of the server's name table. Such a row opens in the text viewer by
its name. It also has `open full`, which sends the raw route the file's name so
the server can sniff the bytes and serve an image as an image. Every row still
has `download` and `copy gsutil`.

The pane counts against the manifest, not against 200. A deployment that lowers
`MAX_PAGE_SIZE` therefore gets the same mark. When a summary carries no manifest
to count against and the route returned a full page, the note reads
`first N listed`.

### Live

While the task is not terminal and the pane is open, it re-reads every 5 s
(`ARTIFACTS_POLL_MS`), which is the worker's `live_log_interval_seconds`. A
faster poll re-reads the same object, and a slower one shows a tail a whole
interval behind. One poll is the task, the transcript and one log read (the
agent's stderr and the runner's two streams). The answer, the listing and the
workflow are read only when something can have changed. That is about 0.6
requests a second against the 20-per-second per-principal budget. The agent's
raw stdout is read only while its view is open.

Every live read shows the age the **server** measured for the object
(`age_seconds`, at its own `read_at`), so a browser's clock skew never enters
it. The age moves on the shared 5 s clock between reads. A live tail is
republished every interval even when nothing changed, so that age is the
publisher's liveness, not new output.

Polling stops once the task is terminal and its finish is in: the answer is
settled, and the transcript and logs are final objects. Otherwise it stops
30 s after `completed_at`, the Details pane's `DRAWER_SETTLE_MS`.

### The transcript

The server parses and redacts the agent's stdout into steps. The pane draws
one row per step. A tool call and its result are **one** row, joined by
`tool.id` = `tool_result.tool_use_id`, with the input and the result
collapsed. A failed result is drawn in the error tone **and** carries the word
`error`. A sub-agent's steps sit under the tool call that spawned them. The
window is described before the first row:

* a live tail that starts part-way reads `earlier steps are not in this window`;
* a final transcript longer than one window reads `later steps are in the next window`, with `next window`;
* unparseable lines are counted (`skipped`), never silently dropped;
* a run made with `--output-format json` recorded only its final result. It is
  labelled `this run recorded only its final result; turn-by-turn steps need
  stream-json`, never drawn as a short transcript.

`show records` re-reads with `include_raw=true` for each step's redacted source
record.

**Fixed after the post-deploy QA** (epic #222, 2026-09-26):

* The facts row (source, format, masked, the object's location, `show
  records`) is drawn only over a **published** stream. It was drawn whatever
  the stream's status, so a task that had published nothing read `source live
  [not measured] · masked 0` above `nothing published yet`, and a browser task
  `source — · masked 0`.
* A thinking step whose text is `""` is drawn as `thinking` and a real-zero
  mark, like a redacted one, not as an expandable that opens onto nothing.
* A rate-limit reading is worded (`allowed · 5-hour window · resets Sep 26,
  06:20 local`), with its reset in this browser's local time, never the
  server's flattened keys and an epoch. A key the screen does not know is its
  name in words, and a value it cannot word is printed as sent.
* A Markdown numbered list keeps its source numbers. A loose list (items
  parted by blank lines) comes out as several `<ol>`s, and each began at 1, so
  `1. 2. 3.` read `1. 1. 1.`; each list now starts at its first item's number.
* At 390 the Logs toolbar's note (`masking at read time`) ran 74 px past the
  drawer: the kit's `.ctl-card-note { flex: none }` kept its one-line width
  after the phone override let it wrap. The override now lets it shrink
  (`flex-shrink: 1; min-width: 0`).

**A null id joins nothing.** The server sends `tool.id` and
`tool_result.tool_use_id` as null when the event carried no string there. Two
nulls are two unknowns, not a call and its result, so each is drawn on its own
(`no result in this window`, `its call is not in this window`).

**A capture cut at its cap, and a line longer than the window** (#188's review
fix-up, read here since the parity pass):

* When the agent's output passed the worker's capture cap, the capture keeps the
  run's start and end and drops the middle, with a notice line where. The
  server sends `capture_truncated: true` and puts the reason in `stream.detail`.
  The window reads `capture · cut at its cap` with the `partial` mark, and it
  draws the server's sentence above the steps. The notice itself is a `system ·
  capture_truncated` step, and its own words (how many bytes were dropped) are
  drawn with the `partial` mark.
* A line longer than the window is one step with `meta.oversize` and no text,
  and its reason is in `stream.detail`. It reads `line longer than this window`,
  never a bare `other`.
* The answer carries `capture_truncated` too. The capture keeps the run's end,
  where the result is, so a cut capture can still answer whole. The answer is
  drawn, and its note reads `capture cut` with the `partial` mark. An ended run
  with no answer and a cut capture shows the server's sentence, because then
  "no result event" is about what was kept.

Any `stream.detail` on an `ok` transcript read is drawn above the steps. Those
sentences are how the server says a window is less than it looks.

## The honesty rules, as this pane applies them

`absent`, `unreadable`, `not_applicable` and a measured-empty `''` are four
different marks. A window that is not the whole object says so. A read that
failed is drawn with the error it came back with. A route this deployment does
not serve yet (an API older than #184) reads `not served by this API`, never
"none". JSON is pretty-printed only when the window is the whole file. A part
of a JSON document does not parse, so it is shown as served and marked
`partial, not pretty-printed`.

Bytes that are not UTF-8 cannot travel in JSON. `/artifacts/content` and
`/logs` show each one as U+FFFD and count them in `invalid_utf8_bytes`, with a
sentence in `detail`. The file viewer draws the count as `not utf-8 N` with the
`partial` mark, and draws nothing at zero. The log rows already draw the
server's `detail`. The raw route serves those bytes exactly as stored, so a
file's `download` is the one to use.

## The input, masked

The owner decided on 2026-09-25 that Inputs and Details show "a
read-time-redacted copy of the task's input, with 'masked N', like every other
output". Both panes used to draw `task.input` straight off the task document.
This pane said `as submitted · not masked`, and Details said nothing.

Both now read `GET /v1/tasks/{id}/input`. It serves the prompt, the rest of the
input and the whole input, each as the API's one redactor (`redaction.redact`)
leaves it, each with its own count (`TaskInputCopy`). Every string and key is
masked, a value under a credential's name is masked whole, and what one block
masks every block masks (`redaction.JsonMasker`, see `docs/agent-output.md`),
so a secret quoted inside the prompt, or named in the rest of the input, is
masked in every block that holds it. The UI does no masking of its own, and it
never goes back to `task.input`:

* **Inputs** draws the prompt and the rest. Its `masked N` is the sum of those
  two blocks' counts.
* **Details** draws the prompt and, under it, the rest of the input, each with
  its own `masked N`. It says `nothing else submitted` when the prompt is the
  whole input. It draws the full input only when there is no prompt string.
  It used to draw the full input under the prompt, which put the prompt on
  screen twice, and the full input was the block where the PR #210 review
  found a quoted secret in clear.
* The count is drawn as the artifact viewer's is: no mark, plain ink at zero,
  and `--warn` above zero.
* A copy that is loading, failed, or not served by an older API is drawn as
  that (`reading`, `input not read · <the server's words>`,
  `not served by this API`). The raw input is not drawn in its place.
* `empty prompt` means the empty string, in both panes. A prompt of
  whitespace is drawn as sent.
* **Details' metadata table is masked too** (the owner's "mask it
  everywhere", 2026-09-26). It drew `task.metadata` as submitted, between two
  masked blocks. It now draws only the copy's `metadata` block, which the same
  masker as the input made, with its own `masked N`. The platform's own keys
  (`dispatch`, `input_from`, `expected_outputs`, refused at submission from
  every caller) are served as stored, and their rows say `platform · as
  stored`. A copy that failed, or an API that serves no `metadata` block, is
  drawn as that; `task.metadata` is never drawn in its place.
  `GET /v1/tasks/{id}` serves the same masked input and metadata, so no screen
  and no client reads a raw one (see `docs/agent-output.md`).

The input never changes after submission, so a copy that was read is kept
per task (`loadTaskInputOnce`) and shared between the two panes. Each pane
asks through it on its own poll, the drawer's 10 s and this pane's 5 s: a copy
already read is answered from memory without a request, and one that failed
is asked for again, so one failure does not last until the drawer is
reopened. One request per task is in flight at a time, whoever asks. The copy
is not part of either pane's main read: Details used to read it inside
`loadAgentRun`'s `Promise.all`, so the whole drawer waited on it, and it now
reads it itself, as this pane does, so a slow copy blanks only the input
blocks.

## CPU in Details

**The figures are typed attempt fields.** Contract request #15 was accepted
on #184 (2026-09-25). Every attempt row carries `cpu_seconds`,
`peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores`, and the worker
writes them on the attempt with each periodic reading and when each runner is
reaped. They replaced #188's interim path, `attempts?include=usage` and its
`usage` block read off HEARTBEAT events, which Details no longer asks for or
reads.

Details draws two rows, `cpu peak` and `cpu mean`, in the memory and
workspace style, as the owner decided ("as built"). Each row is set against
the limit the worker wrote.

| limit | ceiling label |
|---|---|
| source `cgroup` (request #26) | `cgroup limit`: the kernel's `cpu.max`, whatever the class says |
| source `resource_class` | the class's name (`standard limit`) when the class read here has that cpu, else `class limit` |
| no source recorded, the class read here has that cpu | the class's name (`standard limit`) |
| no source recorded, any other figure | `reported limit`: an attempt from before request #26, which does not say whether the cgroup or the catalogue supplied it |
| none written | the task's class read here, by name (`requests == limits`) |

The `by` column says what the figures are, from what is known about the
attempt's end (`attemptEnd`, the same evidence the memory row reads):

| attempt | by |
|---|---|
| finish recorded | `at exit` |
| running | `live reading` (strip: `live reading · 20s ago`, the API's age; `live reading · age not recorded` for an attempt from before request #26) |
| ended with no recorded finish (kill, reclaim) | `last written` (strip: `last written · 3m ago` when the reading is dated) |
| ran before the typed fields, with a reading on this page | `heartbeat event` (strip: `heartbeat event · cpu-seconds only` when the event has no cores, and `· page full; newer readings may exist` on a full page when the reading is not the one at exit) |
| ran before the typed fields, and the event read failed | `events not read` |
| never started | `never ran` |
| running, nothing written yet | `not yet written` |
| ended, nothing written | `never measured` |
| the row has none of the four keys (an older API) | `not served` |

With nothing measured there is one `cpu` row, not two identical hatched ones.
An absent figure is never drawn as zero, and a figure over the limit takes the
track's over-ceiling hatch.

**A live reading's age is the API's** (contract request #26, accepted on
#184, 2026-09-26). The worker stamps `cpu_measured_at` on every write, and the
attempt routes serve `cpu_reading_age_seconds` against their own `read_at`, as
#187 aged the interim reading on the server's clock. Details draws that age
and never computes one from `cpu_measured_at` on the browser's clock, which
would reintroduce what the #187 review removed. An attempt from before #26
records no time, and its strip still says `age not recorded`.

**The legacy reader.** Two kinds of attempt carry their CPU only on HEARTBEAT
events. Attempts from #188's window have peak, mean and cpu-seconds there.
Attempts from before #188 have the cpu-seconds only, because every heartbeat
before #188 carried `cpu_seconds` and no cores. For an attempt whose typed
fields are all null, Details takes the newest heartbeat of that attempt on the
drawer's own event page that carries a figure (`interimReading`) and labels it
`heartbeat event`. A heartbeat with the key and no value is not a reading.
With cpu-seconds only, the peak and mean rows draw em dashes, never zeros; the
mean row keeps `N cpu-s`, and the strip says `cpu-seconds only`. No read is
added for this. The page is the task's first 200 events, so a long run's
newest reading on it can be an early one, and the mark says so; on a full page
the strip says `page full; newer readings may exist`. When the event read
failed there is no page to look at, and the row says `events not read`, not
`never measured`. The reader does nothing for an attempt with typed fields.

The first version of this reader asked for `peak_cpu_cores` only, and the PR
#210 review found that every attempt from before #188 then read
`never measured` while the page held its cpu-seconds.

### At phone width

The sheet's phone block hides `.ctl-util-track` and `.ctl-util-by` at 560px and
under. The CPU rows' provenance, ceiling source, cpu-seconds and kind of
absence therefore also sit in a strip outside the rows (`.att-cpu-note`),
beside the memory row's (`.att-rss-note`). The words come from the same
function as the `by` column:

* the mark and its words (`live reading · 20s ago`, `last written`,
  `heartbeat event`, `never ran`, `events not read`, `not yet written`, `never measured`,
  `not served`) show at every width, as the memory strip's do;
* the cpu-seconds and the ceiling's source show in the strip only at 560px and
  under, because above that the `by` column beside each bar already says them;
* figures at exit have no mark, so the whole strip (`at exit · 402.3 cpu-s ·
  reported limit`) shows only at 560px and under.

Both strips name their row (`cpu`, `memory`).

`details.cpu.test.tsx` asks the shipped sheet's cascade (`cssgate.ts`
`cascade`) what a 390px and a 1440px viewport display. jsdom applies no
stylesheet, so a test that reads `.ctl-util-by`'s text passes whether or not a
phone can see it.

## Details, after the follow-up

Two things Details drew are gone, by the owner's decisions of 2026-09-25:

* **The runner (platform) log.** It was a panel titled `Runner log (platform)`,
  over the runner process's two streams, and it read `/logs` on every 10 s poll
  of the drawer. It lives in Artifacts › Logs only now, behind
  `runner (platform)`, drawn with the same `Stream` rows.
* **Details' own artifact list.** A table with a viewer and a `copy gsutil`
  per file duplicated Artifacts › Outputs. Details' Output keeps the git
  outcome, which still names the patch file.

And by the owner's decisions of 2026-09-26:

* **The log object locations.** Output listed `result_summary.logs` (the
  runner's and the agent's stdout and stderr URIs, each with `copy gsutil`).
  "The runner log's object locations move to Artifacts › Logs, beside the logs
  they point to. Details holds no log rows." Each stream row in Logs carries
  its object's location and `copy gsutil`, and the transcript view carries the
  location of the agent stdout object it is parsed from (`object`, whole in its
  title and in the copy).
* **The card foot's index** listed `CPU is never sampled` directly under the
  two CPU rows (#222). The entry is `CPU is peak and mean cores of the limit`
  now, and says what the rows are.

## What this pane does not show, said plainly

* **Only the final attempt's files are listed.** The manifest lives in
  `result_summary`, which is written once and holds only the last attempt's
  upload. The logs, transcript and answer are the latest attempt's. There is
  no attempt picker yet (open question on #184).
* **The live transcript is a bounded tail**, the newest 256 KiB, whole lines.
  The complete transcript appears when the attempt ends.
* **The runner log is here only**, under `runner (platform)`. The Details
  panel that also drew it was removed by the owner's decision of 2026-09-25.
