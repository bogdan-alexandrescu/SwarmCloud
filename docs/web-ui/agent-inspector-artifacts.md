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
| Prompt | `GET /v1/tasks/{id}` → `task.input` | `input.prompt` verbatim and wrapped; otherwise the whole input as JSON. `no prompt` only when the key is missing. Served from Firestore **as submitted, not masked** (open question on #184). |
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
The Details pane's own file list, which reads the manifest off the task, showed
all 60. So the pane drew a partial read as if it were the whole list.

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

## CPU in Details

`GET /v1/tasks/{id}/attempts?include=usage` adds each attempt's newest CPU
reading, found by the server in one descending events read. The drawer's own
events page is oldest-first and capped, so on a long run it misses exactly the
final reading. Details draws two rows, `cpu peak` and `cpu mean`, in the
memory and workspace style. Each is set against the limit the worker sent with
the reading, and the label follows the worker's `cpu_limit_source`:

| cpu_limit_source | ceiling label |
|---|---|
| `cgroup` (the container's own `cpu.max`) | `cgroup limit` |
| `resource_class` (`cpu.max` said nothing; the catalogue cpu of the class the container was sized with) | the class's name when the class read here has that cpu, else `resource class limit` |
| no limit in the reading | the task's class read here, by name (`requests == limits`) |

The first version labelled every limit in the reading `cgroup limit`. The
worker sends `resource_class` whenever `cpu.max` is `max` or cannot be read, and
nobody has yet read what Cloud Run's `cpu.max` holds. A catalogue figure would
then have been captioned as a limit the kernel enforces. The `by` column also
says where the reading came from:

| usage.status | by |
|---|---|
| final | `at exit` |
| live | `latest heartbeat Ns ago` |
| last_reading | `last heartbeat Ns ago` |
| never_ran | `never ran` |
| absent, or every figure null | `never measured` |
| beyond_window | `off the event window` |
| unread | `read failed` |
| no `usage` served, or `usage: null` | `not served` |

With no reading there is one `cpu` row, not two identical hatched ones. An
absent figure is never drawn as zero, and a figure over the limit takes the
track's over-ceiling hatch. Two rows rather than one bar with a mean tick is an
open question on #184. So are the typed Attempt fields that would replace the
heartbeat interim (contract request #15).

**The age is the server's.** `Ns ago` is `usage.age_seconds`, the gap between
the reading and the server's `read_at`, moved on by the time since the browser
received the read (the rule the Artifacts pane's stream ages use). The first
version aged `measured_at` on the browser's clock, which is a clock the server
never checked. An age the server did not send reads `age not served` and is
never filled in from the browser.

### At phone width

The sheet's phone block hides `.ctl-util-track` and `.ctl-util-by` at 560px and
under. The first version kept the CPU rows' provenance, ceiling source,
cpu-seconds and kind of absence only in that `by` column. At 390px a live reading
140 s old read `cpu peak 1.62 vCPU / 2 vCPU`, which is exactly how a final
figure reads, and every kind of absence read `— / 2 vCPU`. The memory row had
already solved this with a strip outside the row (`.att-rss-note`). The CPU
rows now have one too (`.att-cpu-note`), with the same words drawn from the same
function as the `by` column:

* the mark and its words (`live heartbeat 20s ago`, `last heartbeat …`,
  `never ran`, `never measured`, `off the event window`, `read failed`,
  `not served`) show at every width, as the memory strip's do;
* the cpu-seconds and the ceiling's source show in the strip only at 560px and
  under, because above that the `by` column beside each bar already says them;
* a final reading has no mark, so its whole strip (`at exit · 402.3 cpu-s ·
  cgroup limit`) shows only at 560px and under.

Both strips now name their row (`cpu`, `memory`), because two lines of
`heartbeat 3m ago` under five bars would not say which bar each is about.

`details.cpu.test.tsx` asks the shipped sheet's cascade (`cssgate.ts`
`cascade`) what a 390px and a 1440px viewport display. jsdom applies no
stylesheet, so a test that reads `.ctl-util-by`'s text passes whether or not a
phone can see it. That is how the first version passed.

## What this pane does not show, said plainly

* **Only the final attempt's files are listed.** The manifest lives in
  `result_summary`, which is written once and holds only the last attempt's
  upload. The logs, transcript and answer are the latest attempt's. There is
  no attempt picker yet (open question on #184).
* **The live transcript is a bounded tail**, the newest 256 KiB, whole lines.
  The complete transcript appears when the attempt ends.
* **The runner log is in two places**: the Details panel, now titled
  `Runner log (platform)`, and `runner (platform)` under Logs here. Which one
  stays is the owner's call (open question on #184).
