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
| Files | `GET /v1/tasks/{id}/artifacts` | The viewer is chosen by the server's `kind`, not by a name rule in the client. Images are drawn from the raw route; binary files are described. Every row has `download` (the raw route) and `copy gsutil`. Before the last attempt ends the list is `uploaded when the attempt ends`, never "none". |
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

## The honesty rules, as this pane applies them

`absent`, `unreadable`, `not_applicable` and a measured-empty `''` are four
different marks. A window that is not the whole object says so. A read that
failed is drawn with the error it came back with. A route this deployment does
not serve yet (an API older than #184) reads `not served by this API`, never
"none". JSON is pretty-printed only when the window is the whole file. A part
of a JSON document does not parse, so it is shown as served and marked
`partial, not pretty-printed`.

## CPU in Details

`GET /v1/tasks/{id}/attempts?include=usage` adds each attempt's newest CPU
reading, found by the server in one descending events read. The drawer's own
events page is oldest-first and capped, so on a long run it misses exactly the
final reading. Details draws two rows, `cpu peak` and `cpu mean`, in the
memory and workspace style. Each is set against the container's cgroup limit
when the worker could read it, and otherwise against the class's cpu
(`requests == limits`). The `by` column says which, plus where the reading came
from:

| usage.status | by |
|---|---|
| final | `at exit` |
| live | `latest heartbeat Ns ago` |
| last_reading | `last heartbeat Ns ago` |
| never_ran | `never ran` |
| absent, or every figure null | `never measured` |
| beyond_window | `off the event window` |
| unread | `read failed` |
| no `usage` served | `not served` |

With no reading there is one `cpu` row, not two identical hatched ones. An
absent figure is never drawn as zero, and a figure over the limit takes the
track's over-ceiling hatch. Two rows rather than one bar with a mean tick is an
open question on #184. So are the typed Attempt fields that would replace the
heartbeat interim (contract request #15).

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
