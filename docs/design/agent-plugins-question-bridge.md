# The question bridge spike: a headless agent asks, parks and resumes the same session

Part of the agent plugins plan (`docs/design/agent-plugins.md`, branch `agent-plugins-plan`; part of #2688). The
owner's decisions of 3 November 2026: in **unattended** mode gstack answers the questions that carry a
recommendation itself, and any other question parks the step until a person answers, then the same step resumes; in
**planner** and **designer** modes every interview question parks the step, answered on the planning page or in
Voice it. This spike answers the plan's first risk: can headless Claude Code hand a question to a person and continue
the same session with the answer? The answer is **yes**, with one flag the runner does not pass today.

| State | What |
| --- | --- |
| Proposed | parking on a question, the planning page's interview panel, Voice it, the 24-hour expiry ([the plan](agent-plugins.md)) |
| Implemented | `apps/agent-worker/agent_worker/runners/question_bridge.py`: the hook, the record, the run's classification, the session's files, the answer message, the resume flags; and in `claude_code.py`, behind the switch, the hook, the flags and a check that fails an attempt whose agent asked |
| Behaviourally tested | `tests/unit/worker/test_question_bridge.py`, against a fake CLI with the measured behaviour; and the real pinned CLI against a fake model API (below) |
| Enabled in configuration | **no**: the switch `SWARM_QUESTION_BRIDGE` is off unless exactly `1`, and nothing in the platform sets it |
| Live-verified | **no**: no run with a real model yet. The one real check is at the end of this page |

## How it was measured

The pinned CLI is `@anthropic-ai/claude-code` **2.1.283** (`images/agent-runtime-base/Dockerfile`,
`CLAUDE_CODE_VERSION`), the same binary as the spike's container. The container has no model credentials, so the
real CLI was pointed at a local fake of the Messages API (`ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`, a key built
at run time). The fake records every request body (so the tools offered to the model are read off the wire, not off
the CLI's own report) and scripts the model: it answers the first main-loop request with an `AskUserQuestion` tool
call carrying two options with previews, and anything after with text echoing what it was sent. Everything below
the model is the real CLI: tool assembly, hooks, the turn loop, the session store and `--resume`. The harness and
every run's output are in the step's artifacts (`spike/`).

## Findings

**1. Plain print mode does not offer the question tool.** With `--print` (text or `--input-format stream-json`
input, with or without `--dangerously-skip-permissions`), the request's `tools` never include `AskUserQuestion`,
even with `--tools AskUserQuestion,Bash` (the CLI drops it and keeps `Bash`). The CLI lists it among its built-in
tools; print mode filters it out unless a permission prompt tool is named (finding 2). Why is the CLI's own
choice; only the behaviour was measured.

**2. `--permission-prompt-tool stdio` offers it.** With that flag the request carries `AskUserQuestion`, and
also `EnterPlanMode` and `ExitPlanMode`; nothing else changes. It works with the prompt as text on stdin (how the
worker starts the CLI, `cliagent`) and with `--dangerously-skip-permissions` (the runner's default). The flag
tells the CLI to send any decision a hook leaves open to its "host" over stdin and stdout. The bridge's hook always
decides, so that channel is never used; and when a decision does reach it with stdin closed, the CLI does not hang:
the call fails with `Tool permission request failed: AbortError: Stream closed` and the model carries on (measured).
The two plan-mode tools are withheld with `--disallowedTools EnterPlanMode,ExitPlanMode`: a headless agent that
entered plan mode would stop editing. Repeated `--disallowedTools` flags add up (measured with the lightpanda
profile's own denial before it), and the flag is variadic, so it goes right before another flag.

**3. A PreToolUse hook on `AskUserQuestion` fires and receives the whole question.** Its stdin:

```json
{"session_id": "<uuid>", "transcript_path": "$HOME/.claude/projects/<slug>/<uuid>.jsonl", "cwd": "...",
 "prompt_id": "...", "permission_mode": "bypassPermissions", "effort": {"level": "medium"},
 "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "toolu_...",
 "tool_input": {"questions": [{"question": "...", "header": "...", "multiSelect": false,
   "options": [{"label": "Postgres (Recommended)", "description": "...", "preview": "..."}, ...]}]}}
```

The recommendation is the option whose label ends in `(Recommended)`, the CLI's own convention. **The skill that
asked is not in the payload**: a build that needs it reads it off the session (the last `Skill` call before the
question), which the packaging spike should confirm for gstack's skills.

**4. The hook can refuse the call and end the turn cleanly, but only with `"continue": false`.**

| Hook answer | What the CLI does |
| --- | --- |
| `permissionDecision: "deny"` | the model gets an error tool result and **another turn** (a second request) |
| exit code 2 | the same as deny |
| `"continue": false` with `stopReason` (and the deny) | **no further request**; the result event is `subtype: "success"`, `stop_reason: "tool_use"`, `terminal_reason: "hook_stopped"`; exit 0 |
| `permissionDecision: "allow"` with `updatedInput.answers` (`{"<question>": "<label>"}`) | the call succeeds with `Your questions have been answered: "<question>"="<label>"...`: a hook can answer a question itself |

So the bridge stops with `continue: false`, and the turn ends after exactly one model request.

**5. `claude --print --resume <session>` continues the same session in a new process after a restore.** A new
process with a **fresh HOME** that holds only `~/.claude/projects/<slug>/<session>.jsonl` resumes the session under
the **same session id**. The model's next request carries the whole history: the question, the tool's error result
(`PreToolUse:AskUserQuestion hook error: <stopReason>`), a system line `... hook stopped continuation: <stopReason>`,
a synthetic assistant `No response requested.`, then the answer as the next user message. It works when the
working directory changed (each attempt runs in its own `/workspace/att_<id>/work/repo`): the CLI finds the session
under the old directory's slug or the new one. With the file missing it exits 1 with
`{"type": "result", "subtype": "error_during_execution", "is_error": true}` and
`No conversation found with session ID: <id>` on stderr.

**6. Where the CLI keeps its session under HOME.** HOME is the attempt's `work/` (`cliagent`).

| Path | What | Needed to resume |
| --- | --- | --- |
| `.claude/projects/<slug>/<session>.jsonl` | the transcript; `<slug>` is the working directory with every `/` and `.` made `-` | **yes**, the only file |
| `.claude/projects/<slug>/<session>/` | large tool results and subagent transcripts the transcript points at | when present |
| `.claude/sessions/<pid>.json` | the running process's registry entry | no |
| `.claude/session-env/<session>/`, `.claude/shell-snapshots/`, `.claude/backups/`, `.claude.json` | per-process state, settings backups, the trust flag | no (`.claude.json` is rewritten by `trust_workspace`) |

The worker's checkpoint already archives all of `work/.claude/` (`checkpoint.py`, the owner's decision on #244),
masking JWT and `Bearer` values in a way that keeps each JSON line valid "so a resumed session reads it".

**7. The question tool is available, so the fallback is not needed.** The brief's alternative, a SwarmCloud tool
the agent calls to ask and gstack's question instructions pointed at it, is only needed if a CLI release stops
offering `AskUserQuestion` under the flag. Then the same hook and record would sit behind an MCP tool on the
platform's own server (`--mcp-config`, as the git and state tools are), and gstack's skills would be told, in the
system note, to call it wherever they say `AskUserQuestion`. Not built: nothing to build it for.

## The prototype, behind its switch

`question_bridge.py` is standard library only, because it is also the hook: the settings run it as
`python -I <absolute path> <pending path>`, as `stdin_hook.py` is run, so an agent cannot edit the copy a later
attempt runs.

* **The hook** records the question to `work/.swarm-claude-code/pending-question.json` (beside the runner's
  settings, in HOME, never in the checkout), owner-only, written atomically, a link at the name replaced and never
  followed. It always answers `continue: false` with the deny. **Strict, never advisory:** an input it cannot
  record (not JSON, another tool, a session id that is not a UUID, no questions, an option with no label, more than
  256 KiB) still stops the turn, with a reason that says nobody will answer, and removes any earlier record. It
  never lets a question through to the host, where it would fail as "Stream closed" and the model would continue
  as if asked and unanswered.
* **`classify(events, pending)`** reads one run: `parked` only when the result says `hook_stopped`, the last tool
  call is the question tool, and the record is that call (`tool_use_id`) in that session. Any disagreement is
  `bridge_failed`, never a normal end: a question asked and not recorded is missing evidence, not a pass.
* **`session_files(home, session)`** lists what to checkpoint: the transcript and its sidecar directory in any slug.
* **`answer_message(pending, answers)`** builds the resumed session's next message: every question with its answer,
  an answer that is not an option's label marked as the person's own words. A missing, extra, empty or
  several-for-a-single-choice answer is refused: a partial answer never resumes the step.
* **`resume_args(session)`** is `--resume <session>`, the session id checked first.

`claude_code.py` changes only while `SWARM_QUESTION_BRIDGE=1`: the settings gain the hook (and lose an earlier
run's record), the CLI gains `--permission-prompt-tool stdio --disallowedTools EnterPlanMode,ExitPlanMode`, and after
the run an attempt whose agent asked fails with `question_unanswered: ...` (or `question_bridge_failed: ...`)
instead of reporting success. That last check is a safety net while nothing parks: a stopped turn is
`subtype: "success"` with an empty result, and `cliagent`'s repair and finish turns would then resume the session
**without** an answer.

### Measured end to end

With the real CLI and the committed hook (the fake model as above): the first run offers `AskUserQuestion` and not
`EnterPlanMode`, stops with `terminal_reason: "hook_stopped"`, and `classify` says `parked`; the session's one file
is copied into a fresh HOME; a new process in a different working directory resumes with
`--resume <session>` and `answer_message`, under the same session id, and the model's request ends with
`A person answered the question you asked with AskUserQuestion (tool use toolu_...). Continue the task with these
answers: - Which database should the plan use? -> Postgres (Recommended)`.

## What the trusted state package must provide for park and resume

The park and resume themselves belong to the program's trusted state package; the spike does not wire them into the
lifecycle. What they need, from these findings:

1. **The pending question, out of the agent's reach.** The hook's record is in `work/`, which the agent can write.
   At park, the worker reads it once (`classify`), checks it against the stream, and writes the question to the
   step's state: the question, options, previews, recommendation, the `session_id` and `tool_use_id`, the attempt
   and its fencing generation, and the **source revision** of the checkout it was asked about. From then on the
   step's state is the record, not the file.
2. **A checkpoint before the park** that includes `work/.claude/projects/<slug>/<session>.jsonl` and its sidecar
   directory (already in the archive) and `GSTACK_HOME` under the attempt. The park is refused if that checkpoint
   fails: a park nobody can resume is a lost step.
3. **The `AWAITING_INPUT` park state** (`swarm_common/states.py`): no lease, no slot, no container. The first answer
   is recorded with who answered and where; a second is refused.
4. **On resume, a new attempt at a new generation** restores the checkpoint, checks that the restored session file
   is the parked `session_id`, and runs `claude --print ... --permission-prompt-tool stdio --resume <session>` with
   `answer_message` on stdin. A stale generation exits without running the agent and without touching the answer.
5. **Changes in `cliagent` the build lane owns:** after a `hook_stopped` question, no repair, finish, pending-work
   or pre-publish turn and no publish; and the context ceiling's fresh-session handoff must not replace a parked
   session's `--resume` (a handoff would drop the question's context).
6. **Expiry and cost.** A question unanswered for 24 hours ends the step with the question left open. Each resume is
   a new attempt whose spend is counted like any other (failed, cancelled, parked and retried attempts included);
   after a long park the prompt cache has expired, so the resumed request pays to write the whole context again.

## Unknowns, recorded as unknown

* **A real model's behaviour**: whether Opus, given `AskUserQuestion` headless, calls it where gstack's skills ask
  for it, and accepts the resumed history (the shape is the CLI's own, so the API should; not run).
* Whether gstack's own `AskUserQuestion` hook (for `plan-tune`) runs beside this one, and which decision wins when
  they differ: not measured.
* Whether any tool other than the three named starts asking the host under the flag (an MCP server's elicitation,
  for one). None did in these runs.
* The skill that asked, for gstack's skills (finding 3).

## The one real check

It needs model credentials, which the spike's container lacks: a `CLAUDE_CODE_OAUTH_TOKEN` or an
`ANTHROPIC_API_KEY` in the environment (not a `~/.claude/.credentials.json`, since the check changes HOME), CLI
2.1.283, and this branch checked out at `$REPO`. Run in bash:

```bash
set -euo pipefail
T="$(mktemp -d)"; mkdir -p "$T/home1" "$T/home2" "$T/att-1/repo" "$T/att-2/repo"
PYTHONPATH="$REPO/apps/agent-worker" python3 -c '
import json, pathlib, sys
from agent_worker.runners import question_bridge as qb
print(json.dumps({"hooks": {"PreToolUse": [qb.hook_entry(pathlib.Path(sys.argv[1]))]}}))
' "$T/pending-question.json" > "$T/settings.json"
FLAGS=(--print --output-format stream-json --verbose --dangerously-skip-permissions --settings "$T/settings.json"
       --permission-prompt-tool stdio --disallowedTools EnterPlanMode,ExitPlanMode)
cd "$T/att-1/repo"
echo 'Use the AskUserQuestion tool now to ask me which database the plan should use: "Postgres (Recommended)" or "Firestore", with a one-line preview of each. Then say which one I chose.' \
  | HOME="$T/home1" claude "${FLAGS[@]}" > "$T/run1.jsonl"
tail -n 1 "$T/run1.jsonl" | jq '{subtype, stop_reason, terminal_reason}'   # expect success, tool_use, hook_stopped
jq . "$T/pending-question.json"                                         # expect the question with both options and previews
SID="$(jq -r .session_id "$T/pending-question.json")"
mkdir -p "$T/home2/.claude/projects"
cp -R "$T/home1/.claude/projects/." "$T/home2/.claude/projects/"
cd "$T/att-2/repo"
PYTHONPATH="$REPO/apps/agent-worker" python3 -c '
import json, pathlib, sys
from agent_worker.runners import question_bridge as qb
p = qb.read_pending(pathlib.Path(sys.argv[1]))
print(qb.answer_message(p, {q.question: "Firestore" for q in p.questions}))
' "$T/pending-question.json" | HOME="$T/home2" claude "${FLAGS[@]}" --resume "$SID" > "$T/run2.jsonl"
tail -n 1 "$T/run2.jsonl" | jq '{subtype, session_id, result}'   # expect success, the same $SID, a result naming Firestore
```

The script itself was run against the fake model API (adding `ANTHROPIC_BASE_URL` and a fake key) and passes there;
only the model is left to check. A pass is: run 1 ends `hook_stopped` with the question recorded, and run 2, in a new process, a fresh HOME and a
different directory, continues session `$SID` and names Firestore. Anything else is a fail, and its two `.jsonl`
files say where.
