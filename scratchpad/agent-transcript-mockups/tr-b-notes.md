# Transcript, direction B: Trace (`?tr=b`)

**The idea.** Show the transcript the way a trace viewer shows a request. On the left is a compact timeline with one row for **every** event: a kind icon (message, tool, edit, test, browser, error, plus thinking, start, result, system and *not recognised*), a short label, a count of folded progress events, the duration where the record holds one, and a status (✓, ✕, or ● for no result yet). On the right is the picked event **in full**: the whole command and the whole output with nothing cut, an edit drawn as a diff with an *Open in Changes ›* link, the screenshot for a browser step, and a failure opened at its stack. While someone reads, the agent list folds to a 40px strip (☰ brings it back; *⇤ Full width* or `[` folds it again). Filter chips (All / Messages / Tools / Edits / Errors, each with its count). Search marks matching rows with a yellow left tick, dims the rest, and highlights matches in the detail; Enter steps through them. `j`/`k` (or ↑/↓) step through events, `e` and *Jump to error · N* go to the next error, `/` goes to search. *Keep open* pins one event in a second scroller under the picked one, so a diff and the failure it caused can be read together. A live run adds new events to the timeline and keeps the newest picked until the reader picks another; then *↓ N newer · Follow* takes them back.

Built in `apps/swarm-ui/src/traceB/` (every file starts `// MOCK-HOOK FILE`), with marked hooks in `api.ts` (fixture), `AgentLogs.tsx` (the Transcript view only; stdout, stderr and Runner are unchanged) and `work/Inspector.tsx` (the fold).

## What the record holds, and which fields carry time

The worker stores the CLI's `--output-format stream-json` stdout as `agent_stdout`. The server (`swarm_api/transcript.py` `map_event`) turns each record into steps. With `include_raw=true` each step also carries the masked record, and this view asks for that.

- **No record carries a wall-clock timestamp.** `system/init`, `assistant`, `user`, `result`, `rate_limit_event` and `tool_progress` have no time of day. So the trace shows **no clock times and no per-message durations**.
- `tool_progress.elapsed_time_seconds` (keyed by `tool_use_id`) is the only time recorded per step. The CLI emits it every few seconds while a tool runs, so the last one is a **lower bound**. It is drawn as `≥12s`, and its tooltip says so. A tool with no progress record shows no duration.
- `result.duration_ms` (and `duration_api_ms`, not shown), `num_turns` and `total_cost_usd` describe the whole run. They are drawn on the result row and in the summary line.
- `assistant.message.usage` (`input_tokens`, `cache_*`, `output_tokens`) is per API message. A message or call shows its `output_tokens` from it. Blocks of one message repeat the same usage, so it is a count for the message, not the block.
- An edit's diff comes from `user.tool_use_result.structuredPatch`, which carries line numbers. Without it the diff comes from the call's `old_string`/`new_string`, with no line numbers. A screenshot is **not** in the transcript: the tool result only counts its image (`images: 1`). The view shows the artifact the result names (`…/artifacts/screenshots/page-….png`). In the mock-up that artifact is drawn as a stand-in.
- The working directory is `system/init.cwd`. It is dropped from every command (`cd <cwd> && `) and path, and shown once, as the tooltip on `⌂ repo`.
- The fixtures (four invented runs, MOCK-HOOK) are written as raw stream-json records and mapped the way `map_event` maps them.

## + What it gets right

- **+ Every event is one row and fits the screen** (tr-b-coding-rest): a 14-turn lane is 20 rows instead of a page of monospace one-liners. Progress records fold into a count pill on their call (`3`) and are not rows of their own. `thinking · text not recorded` stays a quiet row, because it is an event.
- **+ Nothing is cut off where it is read** (tr-b-coding-open, tr-b-failed): the detail pane shows the whole command and output. A failure opens at its first `>`/`E ` stack line, with the event's title kept stuck at the top of its scroller.
- **+ An edit is a diff** with old and new line numbers and *Open in Changes ›* (tr-b-coding-open). A file written outside the repository (the PR files) says it is not on the Changes tab.
- **+ Test runs read as tests**: a flask icon and `1 failed · 11 passed` in the row, taken from pytest's own summary line.
- **+ Errors are one keystroke away** (tr-b-failed): *Jump to error · 2* goes from rest to the first error, then to the next. The task's own `last_error` sits above the panes, labelled in its tooltip as from the task record, not the transcript.
- **+ The result is not drawn twice** (tr-b-coding-rest): the result row holds outcome, turns, duration and cost, and its answer is a link to "#19, the last message".
- **+ Honest about time**: `≥12s` (lower bound, from progress), `2m 4s` (from `duration_ms`, in the agent head's own format), and nothing else.
- **+ Unknown events survive** (`auth_status · as recorded`, tr-b-coding-rest row 15): the detail shows the record as written.
- **+ Live follows without fighting the reader** (tr-b-live).
- **+ The browser lane reads as a browser session** (tr-b-browser): the target is in each row (`navigate https://example.com`, `click Learn more link`), the screenshot sits beside the output, and console output is coloured by level.

## ! What it costs

- **! Two panes need width.** At 1280 with the agent list back in (tr-b-coding-rest-1280) the dock is about 330px, so the trace shows only the timeline. Tapping a row swaps it for the detail. The view is meant to be read full width, and the strip makes that one click away, but a reader who keeps the list open loses the side-by-side.
- **! A row is a label, not prose.** An agent's message is reduced to its first line in the timeline (only markdown markers are dropped: a leading `#`/`>`, backtick pairs, emphasis around words; an `_` or `#` inside a word or path stays, as recorded), and the full markdown is only in the detail pane. Direction A (a reading view) reads messages better. Here the conversation is the thing you click through.
- **! It needs `include_raw=true`.** The working directory, progress seconds, structured patches and token usage live only in the raw record. Each read is larger, as every step carries its record (masked by the same `JsonMasker`); not measured here.
- **! Screenshots are a second read.** The transcript holds no image bytes. The view reads the artifact the tool result names. If the artifact is missing, it says so in words (*not in this run's artifacts*).
- **! Durations are sparse.** Short tool calls never emit `tool_progress`, so most rows have no duration (tr-b-coding-rest). The column is empty by honesty, not by accident. A true per-step time needs the worker to stamp each stored line, which is a contract change, not built here.
- **! At 1280 the head folds; at 1440 it does not.** Beside the agent list (tr-b-coding-rest-1280) the trace takes a window's height and the column is scrolled once so the agent's tabs sit at its top: the head is one scroll back, and 19 of 20 rows show with every status, count and duration whole. The live fixture (tr-b-live) is dated from now, so its head reads minutes.
- **! The agent head still costs about 230px** above the trace on the Logs tab (all 1440 frames). The column scrolls it away, as it does today, but at rest it takes height from the timeline.
- **! Search covers this read**, the window the server served, not the whole stream when the read is windowed. A `window_starts_mid_stream` read shows *earlier events are not in this read* at the top of the timeline.
- **! Error means `is_error`**: a failure the CLI did not mark (a non-zero exit inside a passing call) is not counted. The tooltip on *Jump to error* says so.
- **! The agent head is drawn above the trace** (all 1440 frames): the dock's title and the agent head both say the agent's name. That is today's Inspector, not this direction; it is left as it is.
- **! A Bash call's own `description`** (*Run the tests related to the change*) is shown in the detail's sub-line, in italics, because the record holds it and it says why the command ran. It is the agent's words, not the console's; it could move behind the title's tooltip if it reads as prose.

Rows are 25px, so a 20-event lane fits a 1440x900 screen whole (tr-b-coding-rest). When the list scrolls, it lands on a row's edge, so the top row is never drawn half.
