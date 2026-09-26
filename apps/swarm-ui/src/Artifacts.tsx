import { Fragment, useCallback, useRef, useState, type ReactNode } from 'react'
import { Chip, DRAWER_SETTLE_MS, Em, Mark, MaskedNote } from './AgentDetail'
import {
  ARTIFACT_PAGE_LIMIT,
  artifactRawUrl,
  loadAnswer,
  loadArtifactListing,
  loadTask,
  loadTaskInputOnce,
  loadTaskLogs,
  loadTranscript,
  loadWorkflow,
  type WorkflowRead,
} from './api'
import { ArtifactViewer, Markdown } from './ArtifactViewer'
import { DECLARED_WORDS, taskInputsOf, type TaskInputRow } from './dag'
import { errorHeading, num, type ApiError, type Result } from './fetch'
import { Absent } from './primitives'
import { attemptLine, servedAge, Stream, useRead } from './RunFiles'
import { Id, Screen, type ScreenReading } from './Shell'
import {
  ageSpan,
  bytesLabel,
  TERMINAL_STATES,
  type ArtifactEntry,
  type ArtifactRef,
  type ArtifactKindName,
  type ArtifactListing,
  type LogStreamName,
  type ResultSummary,
  type Task,
  type TaskAnswer,
  type TaskInputCopy,
  type TaskLogs,
  type TaskTranscript,
  type TranscriptStep,
} from './types'
import { AGE_TICK_MS, useNow } from './useNow'

/**
 * THE ARTIFACTS PANE (#184): WHAT AN AGENT TOOK IN AND WHAT IT PRODUCED.
 *
 * WHY IT EXISTS. On `task_73b5f4d9ca3641fbb914` the agent's 7,124-character
 * answer sat inside `claude-code.stdout.log`, and the drawer showed it as a
 * file name beside `copy gsutil`. The panel headed "Output, as the agent
 * wrote it" showed the RUNNER's streams -- an empty stdout and one `child
 * started` line. Nothing rendered an image. The owner's decisions of
 * 2026-09-25 are what this pane is built to:
 *
 *   Inputs   the prompt as the API masked it, with its count, the repository
 *            and ref and the commit
 *            actually cloned, and every file staged from an upstream step,
 *            each linking to the run that produced it;
 *   Outputs  the agent's final answer, rendered as Markdown, FIRST; then
 *            every file with an inline viewer chosen by the server's kind, a
 *            download and a `copy gsutil`;
 *   Logs     the agent's transcript as readable steps, its stdout and stderr,
 *            and the runner's own log, which is the platform's.
 *
 * LIVE. While the task has not finished, the pane re-reads every
 * `ARTIFACTS_POLL_MS`, which is the worker's own publish interval: a faster
 * poll re-reads the same object, a slower one shows a tail a whole interval
 * behind. Every live read says how old the object was when the SERVER read
 * it -- `published Ns ago` -- and that age moves on the shared clock between
 * reads. Polling stops once the task is terminal and its finish is in: the
 * answer is settled and the final objects are what was read, or
 * `DRAWER_SETTLE_MS` after `completed_at`, the rule the Details pane uses.
 *
 * THROUGH THE API, EVERY BYTE. Text is read through the content, log and
 * transcript routes, redacted at read time; an image, a download and `open
 * full` are the raw route, which the browser fetches with the session every
 * read already carries. No signed URL, no object key, no `gs://` uri is ever
 * sent: a file is named by the name its manifest spells, on the task that
 * owns it.
 *
 * NO HTML IS BUILT FROM AGENT BYTES. The answer and every text step go
 * through `Markdown`, which builds React elements; everything else is a
 * `<pre>`.
 *
 * THE HONESTY RULES, as this pane applies them. `absent`, `unreadable`,
 * `not_applicable` and a measured-empty `''` are four different marks. A
 * window that is not the whole object says so. A read that FAILED is a fifth
 * thing, drawn with the error it came back with, and a route this API does not
 * serve yet is a sixth -- `not served`, never "none".
 */
export function ArtifactsScreen({ taskId }: { taskId: string }) {
  // The previous poll's answers, so a read whose object cannot change any
  // more -- a complete listing, a settled answer, a final transcript -- is not
  // asked for again on every tick. Keyed on the task: a drawer that stays
  // mounted while another agent is opened must not carry this one's answers.
  const previous = useRef<ArtifactsView | null>(null)
  const load = useCallback(async (): Promise<Result<ArtifactsView>> => {
    const prev = previous.current !== null && previous.current.task.id === taskId ? previous.current : null
    const next = await loadView(taskId, prev)
    if (next.status === 'ok') previous.current = next.data
    return next
  }, [taskId])

  return (
    <Screen
      // KEYED ON THE TASK for the reason AgentDetailScreen's is: `Screen`
      // re-runs its load on its own nonce only, so a changed task id would
      // otherwise keep drawing the previous task's files under this id.
      key={taskId}
      title={taskId}
      load={load}
      pollMs={artifactsPoll}
      summary={(v) => (
        <>
          {v.task.runner_profile} · {v.task.state.toLowerCase()}
        </>
      )}
    >
      {(v, reading) => <Body v={v} reading={reading} />}
    </Screen>
  )
}

/**
 * HOW OFTEN THE PANE RE-READS WHILE ITS TASK RUNS: every 5 s, which is the
 * worker's `live_log_interval_seconds` -- the cadence the live tails are
 * republished at. One poll is the task, the transcript and one log read (the
 * answer, the listing and the workflow only when something can have changed),
 * about 0.6 requests a second against the 20 per second per-principal budget.
 */
export const ARTIFACTS_POLL_MS = 5_000

/** What one poll of the pane read. Each part keeps its own answer, never another's. */
interface ArtifactsView {
  task: Task
  /**
   * NULL: not read, because the task has no result summary yet. The listing
   * route serves the manifest out of that summary, so it would answer an
   * empty, incomplete list -- "uploaded when the attempt ends" is known
   * without asking.
   */
  listing: Result<ArtifactListing> | null
  answer: Result<TaskAnswer>
  transcript: Result<TaskTranscript>
  /** The agent's stderr and the runner's stdout and stderr, in one read. */
  logs: Result<TaskLogs>
  /** NULL: the task is not a workflow step, so there is no workflow to read. */
  workflow: Result<WorkflowRead> | null
}

/** The streams the poll reads in one `/logs` call. The agent's stdout is read only while its view is open. */
const POLLED_STREAMS: readonly LogStreamName[] = ['agent_stderr', 'stdout', 'stderr']

/** A 404 with no error envelope: FastAPI's own, for a path this deployment does not serve. */
function routeMissing(e: ApiError): boolean {
  return e.kind === 'not_found' && e.code === null
}

function ok<T>(r: Result<T> | null | undefined): r is { status: 'ok'; data: T; fetchedAt: number; serverAt?: string } {
  return r !== null && r !== undefined && r.status === 'ok'
}

/** A terminal task's answer that no later read can change. */
function answerSettled(v: ArtifactsView): boolean {
  return (
    TERMINAL_STATES.has(v.task.state) &&
    ok(v.answer) &&
    (v.answer.data.status === 'ok' || v.answer.data.status === 'absent')
  )
}

/** A terminal task's transcript read from an object that no longer changes. */
function transcriptSettled(v: ArtifactsView): boolean {
  if (!TERMINAL_STATES.has(v.task.state) || !ok(v.transcript)) return false
  const s = v.transcript.data.stream
  return s.status === 'not_applicable' || (s.status === 'ok' && (s.source === 'final' || s.source === 'artifact'))
}

/** A terminal task's logs read from objects that no longer change. */
function logsSettled(v: ArtifactsView): boolean {
  if (!TERMINAL_STATES.has(v.task.state) || !ok(v.logs)) return false
  if (v.logs.data.attempt.completed_at === null) return false
  return v.logs.data.streams.every((s) => s.source !== 'live')
}

/**
 * ONE POLL. The task decides whether there is a pane at all; every other read
 * degrades to its own failure inside the view, because a failed transcript
 * read must not blank the answer and must not be drawn as "no transcript".
 */
async function loadView(taskId: string, prev: ArtifactsView | null): Promise<Result<ArtifactsView>> {
  const task = await loadTask(taskId)
  if (task.status === 'error') return task
  if (task.status !== 'ok' && task.status !== 'stale') {
    return {
      status: 'error',
      error: { kind: 'unreachable', httpStatus: null, code: null, message: 'The task read did not complete.' },
    }
  }
  const t = task.data
  const terminal = TERMINAL_STATES.has(t.state)

  // THE LISTING: only once there is a summary to list, and not again once a
  // complete one is in hand -- the manifest is written once.
  const prevListing = prev?.listing ?? null
  const listing: Promise<Result<ArtifactListing> | null> =
    t.result_summary === null
      ? Promise.resolve(null)
      : ok(prevListing) && prevListing.data.complete
        ? Promise.resolve(prevListing)
        : loadArtifactListing(taskId)

  // THE ANSWER: read at open; while running, again only once the transcript
  // has shown a result event, or while the last read failed; after the end,
  // until it is settled.
  const prevAnswer = prev?.answer ?? null
  const answerIn = prevAnswer !== null && ok(prevAnswer) && prevAnswer.data.status === 'ok'
  const retry = prevAnswer !== null && prevAnswer.status === 'error' && !routeMissing(prevAnswer.error)
  const resultSeen =
    prev !== null && ok(prev.transcript) && (prev.transcript.data.steps ?? []).some((s) => s.kind === 'result')
  const askAnswer =
    prev === null || prevAnswer === null || retry || (terminal ? !answerSettled(prev) : resultSeen && !answerIn)
  const answer: Promise<Result<TaskAnswer>> =
    askAnswer || prevAnswer === null ? loadAnswer(taskId) : Promise.resolve(prevAnswer)

  const transcript: Promise<Result<TaskTranscript>> =
    prev !== null && transcriptSettled(prev) ? Promise.resolve(prev.transcript) : loadTranscript(taskId, { source: 'auto' })
  const logs: Promise<Result<TaskLogs>> =
    prev !== null && logsSettled(prev) ? Promise.resolve(prev.logs) : loadTaskLogs(taskId, { stream: POLLED_STREAMS, source: 'auto' })

  // THE WORKFLOW, once: it names the step behind each staged file and says
  // whether that run still lists it, and neither changes while this is open.
  const prevWorkflow = prev?.workflow ?? null
  const workflow: Promise<Result<WorkflowRead> | null> =
    t.workflow_id === null
      ? Promise.resolve(null)
      : ok(prevWorkflow)
        ? Promise.resolve(prevWorkflow)
        : loadWorkflow(t.workflow_id)

  const [l, a, tr, lg, w] = await Promise.all([listing, answer, transcript, logs, workflow])
  return {
    status: 'ok',
    data: { task: t, listing: l, answer: a, transcript: tr, logs: lg, workflow: w },
    fetchedAt: task.fetchedAt,
  }
}

/**
 * The pane's cadence: `ARTIFACTS_POLL_MS` until the task is terminal, then
 * until its finish is in -- the answer settled and every object final -- or
 * `DRAWER_SETTLE_MS` after `completed_at`, whichever is first. The age is
 * taken either way round, as the Details pane takes it: the server's clock
 * against this browser's.
 */
export function artifactsPoll(v: ArtifactsView | null, now: number = Date.now()): number | null {
  if (v === null || !TERMINAL_STATES.has(v.task.state)) return ARTIFACTS_POLL_MS
  if (answerSettled(v) && transcriptSettled(v) && logsSettled(v)) return null
  const done = v.task.completed_at === null ? Number.NaN : Date.parse(v.task.completed_at)
  return Number.isFinite(done) && Math.abs(now - done) <= DRAWER_SETTLE_MS ? ARTIFACTS_POLL_MS : null
}

function Body({ v, reading }: { v: ArtifactsView; reading: ScreenReading }) {
  // THE SHARED AGE CLOCK. Every served age on this pane ticks on it, as every
  // age in the frame does, so two ages side by side cannot disagree.
  const now = useNow(AGE_TICK_MS)
  return (
    <div className="run-stack arts">
      <Inputs v={v} reading={reading} />
      <Outputs v={v} />
      <Logs v={v} reading={reading} now={now} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

function Inputs({ v, reading }: { v: ArtifactsView; reading: ScreenReading }) {
  return (
    <section className="section arts-inputs">
      <h2>Inputs</h2>
      <Prompt taskId={v.task.id} readAt={reading.fetchedAt} />
      <Repository task={v.task} />
      <StagedFiles v={v} />
    </section>
  )
}

/**
 * THE INPUT, AS THE API MASKED IT (#184, owner decision of 2026-09-25): the
 * prompt wrapped, and the rest of the input as JSON below it, when the input
 * carries a prompt; the whole input as JSON when it does not. `no prompt` is
 * drawn only when the KEY is missing -- an empty string is a prompt somebody
 * sent, and an empty input is a task sent without one.
 *
 * NEVER THE TASK DOCUMENT. This read `task.input` and said `as submitted · not
 * masked`, which was true: every byte this pane read from the bucket was
 * redacted at read time and the input was not. The owner decided the input is
 * masked too, "with 'masked N', like every other output", so it is read from
 * `GET /v1/tasks/{id}/input` -- the same redactor, the same count -- and the
 * count beside the eyebrow is over what this block draws. A copy that could
 * not be read, or an API that does not serve one, is said as that; the raw
 * input is never drawn in its place.
 *
 * ITS OWN READ, NOT THE POLL'S. The input never changes, so it is read once
 * per task (and shared with the Details pane, `loadTaskInputOnce`), and a
 * slow or failed copy holds up nothing else on the pane.
 *
 * A FAILED COPY IS ASKED AGAIN WITH THE PANE'S POLL (PR #210 review). The key
 * was the constant `input`, so one failed read said `the masked input not
 * read` for as long as the pane stayed open. The key now follows the pane's
 * read (`readAt`): a copy already read is answered from memory without a
 * request, and one that failed is asked for again. A pane that has stopped
 * polling -- a settled finish -- stops asking with it.
 */
function Prompt({ taskId, readAt }: { taskId: string; readAt: number }) {
  const { state } = useRead<TaskInputCopy>(() => loadTaskInputOnce(taskId), taskId, `input:${readAt}`, null)
  const copy = state.status === 'ok' || state.status === 'stale' ? state.data : null
  // What this block draws, masked: the prompt and the rest, or the whole input.
  const masked =
    copy === null
      ? null
      : copy.prompt !== null
        ? copy.prompt.redaction_count + (copy.rest?.redaction_count ?? 0)
        : copy.full.redaction_count
  return (
    <div className="arts-block">
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">prompt</span>
        {masked !== null && <MaskedNote count={masked} />}
      </div>
      {state.status === 'loading' ? (
        <p className="art-loading">
          <Mark kind="pending" say="Reading the masked copy of the input. The read is in flight." />
          <span className="ctl-pending art-loading-bar" />
        </p>
      ) : copy === null ? (
        <ReadFailed error={state.status === 'error' ? state.error : null} what="the masked input" />
      ) : copy.prompt !== null ? (
        <>
          {copy.prompt.text === '' ? (
            <p className="att-none">
              <Mark kind="zero" say="The prompt was submitted as an empty string. A real, empty prompt, not a missing one." />{' '}
              empty prompt
            </p>
          ) : (
            <pre className="arts-prompt">{copy.prompt.text}</pre>
          )}
          {copy.rest !== null && <pre className="art-text">{copy.rest.text}</pre>}
        </>
      ) : copy.prompt_key === 'missing' && copy.full.text === '{}' ? (
        <p className="att-none">
          <Mark kind="zero" say="This task was submitted with no input at all. Nothing is missing: nothing was sent." /> no
          input
        </p>
      ) : (
        <>
          {copy.prompt_key === 'missing' && (
            <p className="att-none">
              <Mark
                kind="zero"
                say="This input has no prompt key. The runner takes its input as the object below, shown as the API masked it."
              />{' '}
              no prompt
            </p>
          )}
          <pre className="art-text">{copy.full.text}</pre>
        </>
      )}
    </div>
  )
}

/**
 * THE REPOSITORY, THE REF, AND THE COMMIT ACTUALLY CLONED. The URL and ref
 * are what was asked for; `git.base` is what the worker landed on, reported
 * in the result summary when the attempt finishes -- so a running task's is
 * pending, not missing. Nothing at all when no repository was asked for.
 */
function Repository({ task }: { task: Task }) {
  const url = task.repository_url
  if (url === null) return null
  const summary = task.result_summary as ResultSummary | null
  const base = summary?.git?.base
  const terminal = TERMINAL_STATES.has(task.state)
  return (
    <div className="arts-block">
      <span className="ctl-eyebrow">repository</span>
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>url</b>
          <span className="mono uri" title={url}>
            {url}
          </span>
        </li>
        <li className={`ctl-fact${task.repository_ref === null ? ' is-absent' : ''}`}>
          <b>ref</b>
          {task.repository_ref === null ? (
            <>
              <Em /> <Mark kind="zero" say="No ref was submitted with this task." />
            </>
          ) : (
            <span className="mono">{task.repository_ref}</span>
          )}
        </li>
        <li className={`ctl-fact${typeof base === 'string' && base !== '' ? '' : ' is-absent'}`}>
          <b>cloned</b>
          {typeof base === 'string' && base !== '' ? (
            <code title={base}>{base.slice(0, 12)}</code>
          ) : !terminal ? (
            <>
              <Mark kind="pending" say="The commit actually cloned is reported when the attempt finishes." /> reported at
              finish
            </>
          ) : (
            <>
              <Em />{' '}
              <Mark
                kind="absent"
                say="This run finished and its result names no cloned commit: the summary was never written, or the worker lost its marker on a resumed attempt."
              />
            </>
          )}
        </li>
      </ul>
    </div>
  )
}

/**
 * EVERY FILE STAGED FROM AN UPSTREAM STEP: `metadata.input_from` joined with
 * what the result reports arrived (`taskInputsOf`, the same join and the same
 * not-yet words the graph uses). Each row links to the run that produced it,
 * labelled with its step, and the file is read -- viewed or downloaded -- from
 * THAT run's artifact routes: the copy staged into this run's workspace is not
 * uploaded again, so the upstream artifact is the source.
 *
 * An upstream object removed since -- by the bucket's retention, usually -- is
 * `removed from the upstream run` when read, and the size shown stays the size
 * that was staged, never `0 B`.
 */
function StagedFiles({ v }: { v: ArtifactsView }) {
  const { rows, malformed } = taskInputsOf(v.task)
  const [open, setOpen] = useState<string | null>(null)
  if (rows.length === 0 && malformed === 0) return null
  const shown = open === null ? null : rows.find((r) => rowKey(r) === open) ?? null
  const upstreamTaskOf = (r: TaskInputRow): string | null => (r.from.kind === 'task' ? r.from.taskId : null)
  const shownFrom = shown === null ? null : upstreamTaskOf(shown)
  return (
    <div className="arts-block">
      <span className="ctl-eyebrow">staged files</span>
      {rows.length > 0 && (
        <div className="ctl-table is-stacked">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">File</th>
                <th role="columnheader" scope="col">From</th>
                <th role="columnheader" scope="col" className="is-num">Size</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map((r) => {
                const up = upstreamTaskOf(r)
                return (
                  <tr role="row" key={rowKey(r)} className={rowKey(r) === open ? 'is-open' : undefined}>
                    <th role="rowheader" scope="row">
                      {up === null ? (
                        <span className="mono">{r.file}</span>
                      ) : (
                        <button
                          type="button"
                          className="art-open"
                          aria-expanded={rowKey(r) === open}
                          onClick={() => setOpen((cur) => (cur === rowKey(r) ? null : rowKey(r)))}
                        >
                          {r.file}
                        </button>
                      )}
                      {up !== null && (
                        <span className="ctl-sub">
                          <a className="copy" href={artifactRawUrl(up, r.file, 'attachment')} download={fileName(r.file)}>
                            download
                          </a>
                        </span>
                      )}
                    </th>
                    <td role="cell" data-label="From">
                      <StagedFrom row={r} workflow={v.workflow} />
                    </td>
                    <td role="cell" data-label="Size" className="is-num">
                      <StagedSize row={r} workflow={v.workflow} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {malformed > 0 && (
        <span
          className="ctl-mark is-unread"
          aria-label={`${malformed} entr${malformed === 1 ? 'y' : 'ies'} in this run's staged-input report could not be read as a file. Counted here rather than dropped.`}
        >
          {malformed} unreadable
        </span>
      )}
      {shown !== null && shownFrom !== null && (
        <ArtifactViewer
          key={rowKey(shown)}
          // THE UPSTREAM TASK'S ROUTE, by the file's name there. The staged
          // copy in this run's workspace was never uploaded on its own.
          taskId={shownFrom}
          artifact={{ name: shown.file, bytes: shown.arrival.kind === 'staged' ? (shown.arrival.bytes ?? 0) : 0, uri: '' }}
          onClose={() => setOpen(null)}
          absent={{
            heading: 'removed from the upstream run',
            say: 'The upstream run still lists this file and its object is gone -- removed by the bucket’s retention, most likely, or an upload that never completed. The size in the table is what was staged into this run.',
          }}
        />
      )}
    </div>
  )
}

function rowKey(r: TaskInputRow): string {
  return `${r.from.kind === 'task' ? r.from.taskId : 'submission'}:${r.file}`
}

/** The run that produced a staged file, by its step name when the workflow read gave one. */
function StagedFrom({ row, workflow }: { row: TaskInputRow; workflow: Result<WorkflowRead> | null }) {
  if (row.from.kind === 'submission') return <>submission</>
  const taskId = row.from.taskId
  const step = ok(workflow) ? (workflow.data.workflow.steps.find((s) => s.task_id === taskId)?.step_id ?? null) : null
  return (
    <>
      {/* To the upstream run's OWN Artifacts pane: its outputs are this run's inputs. */}
      <a className="ctl-link" href={`#work/task/${encodeURIComponent(taskId)}/artifacts`}>
        {step !== null ? step : <Id title={taskId}>{taskId}</Id>}
      </a>
      {step === null && workflow !== null && !ok(workflow) && (
        <>
          {' '}
          <Mark
            kind="unread"
            say="The workflow read did not complete, so the step that produced this file is shown by its task id."
          />
        </>
      )}
      {!row.declared && <span className="ctl-sub">not declared</span>}
    </>
  )
}

/**
 * Its size when a report says it arrived; otherwise which kind of not-yet it
 * is -- and whether the upstream run still lists it, when the workflow read
 * says.
 */
function StagedSize({ row, workflow }: { row: TaskInputRow; workflow: Result<WorkflowRead> | null }) {
  const a = row.arrival
  const upstream =
    row.from.kind === 'task' && ok(workflow) ? upstreamEntry(workflow.data, row.from.taskId, row.file) : undefined
  return (
    <>
      {a.kind === 'declared' ? (
        <span className="ctl-mark is-absent" aria-label={DECLARED_WORDS[a.why].note}>
          {DECLARED_WORDS[a.why].text}
        </span>
      ) : a.bytes === null ? (
        <span className="ctl-mark is-absent" aria-label="The report names this file but carries no size for it. Not a zero.">
          size not reported
        </span>
      ) : (
        bytesLabel(a.bytes)
      )}
      {a.kind === 'staged' && a.fromCheckpoint && <span className="ctl-sub">from checkpoint</span>}
      {upstream === null && (
        <span className="ctl-sub">
          <Mark
            kind="absent"
            say="The upstream run's manifest does not list this file, so there is nothing there to read it from."
          />{' '}
          not listed upstream
        </span>
      )}
    </>
  )
}

/**
 * The upstream run's manifest entry for one file: the entry, null when that
 * run's manifest is readable and does not list it, undefined when this read
 * cannot tell (the task is not in the workflow read, or it has no manifest).
 */
function upstreamEntry(w: WorkflowRead, taskId: string, file: string): ArtifactEntry | null | undefined {
  const up = w.tasks.find((t) => t.id === taskId)
  const artifacts = (up?.result_summary as ResultSummary | null | undefined)?.artifacts
  if (!Array.isArray(artifacts)) return undefined
  return (artifacts as ArtifactEntry[]).find((e) => e !== null && typeof e === 'object' && e.name === file) ?? null
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

function Outputs({ v }: { v: ArtifactsView }) {
  return (
    <section className="section arts-outputs">
      <h2>Outputs</h2>
      <Answer v={v} />
      <Files v={v} />
    </section>
  )
}

/**
 * THE ANSWER, FIRST. The `result` of the agent's last result event, as
 * Markdown -- the thing a person opens a finished claude-code run for, and
 * which the drawer never showed. When the run has no result event, the
 * runner's summary stands in and says it may be cut: it is capped at 2,000
 * characters, and the reference task's answer is 7,124.
 */
function Answer({ v }: { v: ArtifactsView }) {
  const r = v.answer
  return (
    <div className="arts-block arts-answer">
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">answer</span>
        {ok(r) && r.data.status === 'ok' && <AnswerNote a={r.data} />}
      </div>
      {r.status === 'error' || r.status === 'stale' ? (
        <ReadFailed error={r.error} what="the answer" />
      ) : r.status !== 'ok' ? (
        <ReadFailed error={null} what="the answer" />
      ) : r.data.status === 'not_yet' ? (
        <p className="att-none">
          <Mark
            kind="pending"
            say="The attempt is still running and the agent has not written its result yet. It appears here when it does."
          />{' '}
          not yet
        </p>
      ) : r.data.status === 'absent' ? (
        <p className="att-none">
          <Mark
            kind="absent"
            say="The attempt ended and wrote no result event, and there is no runner summary to stand in for one."
          />{' '}
          no answer recorded
          {/* A CUT CAPTURE IS A REASON, and the server's sentence names it:
              the run's middle was not kept, so "no result event" is about
              what was kept. Said only then; otherwise the mark says it all. */}
          {r.data.capture_truncated === true && r.data.detail !== null && (
            <span className="ctl-sub">{r.data.detail}</span>
          )}
        </p>
      ) : r.data.status === 'unreadable' ? (
        <p className="att-none">
          <Mark
            kind="unread"
            say="An object holding the answer could not be read. Nothing may be concluded about the answer: it is not missing and it is not empty."
          />{' '}
          answer not read
          {r.data.detail !== null && <span className="ctl-sub">{r.data.detail}</span>}
        </p>
      ) : r.data.content === null ? (
        <p className="att-none">
          <Mark kind="unread" say="The read reported an answer and returned no content, so it cannot be shown." /> answer
          not read
        </p>
      ) : r.data.content === '' ? (
        <p className="att-none">
          <Mark kind="zero" say="The agent's result is an empty string: a real, empty answer." /> empty answer
        </p>
      ) : r.data.format === 'markdown' ? (
        <Markdown source={r.data.content} />
      ) : (
        <pre className="arts-prompt">{r.data.content}</pre>
      )}
    </div>
  )
}

function AnswerNote({ a }: { a: TaskAnswer }) {
  return (
    <span className="is-end ctl-card-note">
      {a.capture_truncated === true && (
        <>
          <Mark
            kind="partial"
            say="The agent's output passed the worker's capture size cap, so the middle of the run was not kept. The capture keeps the end of the run, where the result is, so this answer can still be whole; the rest of the run is not."
          />{' '}
          capture cut ·{' '}
        </>
      )}
      {a.is_error === true && (
        <>
          <Chip tone="bad">the agent reported an error</Chip>{' '}
        </>
      )}
      {a.source === 'runner_summary' && (
        <>
          <Mark
            kind={a.complete === false ? 'partial' : 'absent'}
            say={
              a.complete === false
                ? 'The agent wrote no result event, so this is the runner’s summary, which is capped at 2,000 characters and is at that cap: the answer is cut.'
                : 'The agent wrote no result event, so this is the runner’s summary, which is capped at 2,000 characters. Nothing here says whether it was cut.'
            }
          />{' '}
          runner summary ·{' '}
        </>
      )}
      {a.subtype !== null && `${a.subtype} · `}
      {a.num_turns !== null && `${a.num_turns} turns · `}
      masked <span className={`art-masked${a.redacted && a.redaction_count > 0 ? ' is-warn' : ''}`}>{a.redaction_count}</span>
    </span>
  )
}

/** The biggest image drawn inline. Past it, `open full` and `download`. */
const INLINE_IMAGE_MAX_BYTES = 20 * 1024 * 1024

/**
 * The task's own manifest, `result_summary.artifacts`, which is the record the
 * listing route serves. Null when the summary carries nothing that reads as a
 * list. An entry with no name is dropped, because there would be nothing to
 * ask the API for.
 */
function manifestOf(summary: ResultSummary | null): ArtifactRef[] | null {
  const raw: unknown = summary?.artifacts
  if (!Array.isArray(raw)) return null
  return raw.filter(
    (e): e is ArtifactRef => e !== null && typeof e === 'object' && typeof (e as { name?: unknown }).name === 'string',
  )
}

/**
 * How much of the run's file list the listing route returned. `total` is null
 * when there is no manifest to count against.
 */
interface ListingCut {
  listed: number
  total: number | null
}

/**
 * EVERY FILE THE LAST ATTEMPT UPLOADED, with a way to read each one. The
 * viewer is chosen by the server's `kind`; an image is drawn from the raw
 * route below the table, a binary file is described and downloaded, and the
 * rest open in the text viewer. Each row downloads through the API -- the raw
 * route redacts text on the way out -- and copies a `gsutil` line for reading
 * the stored object with your own credentials.
 *
 * EVERY FILE, NOT ONE PAGE OF THEM. The listing route returns one page, at
 * most `ARTIFACT_PAGE_LIMIT` entries, and it cannot be paged past that. Until
 * #187's fix-up it also returned the server's default of 50, because no
 * `limit` was sent, and the pane drew those 50 as the whole run. A file past
 * the route's page is listed from the task's own manifest, which is the same
 * record the route serves. The pane says how many the route listed. The
 * server names no kind for a file it did not list, so that row opens by its
 * name and has `open full`, and the raw route decides what the bytes are. The
 * pane counts against the manifest and not against the page limit, so a
 * deployment that clamps the page lower is caught too.
 */
function Files({ v }: { v: ArtifactsView }) {
  const { task, listing } = v
  const [open, setOpen] = useState<string | null>(null)
  const terminal = TERMINAL_STATES.has(task.state)
  const summary = task.result_summary as ResultSummary | null
  // `redaction_skipped`: files that left the pod neither rewritten nor scanned
  // clean (`_upload_outputs`), capped at 50 like `artifacts_skipped`.
  const scrub: unknown = summary === null ? undefined : summary.redaction_skipped
  const unmasked: unknown[] = Array.isArray(scrub) ? scrub : []

  let body: ReactNode
  let entries: ArtifactEntry[] = []
  let skipped: string[] = []
  let cut: ListingCut | null = null
  // Names of the rows that came from the manifest because the route did not
  // list them.
  const unlisted = new Set<string>()
  if (listing === null || (ok(listing) && !listing.data.complete)) {
    body = !terminal ? (
      <p className="att-none">
        <Mark
          kind="pending"
          say="Artifacts are uploaded when the attempt ends, never while it runs, so this list is empty because it is not written yet -- not because the agent produced nothing."
        />{' '}
        uploaded when the attempt ends
      </p>
    ) : task.started_at === null ? (
      <p className="att-none">
        <Mark kind="zero" say="No attempt of this task ever started, so no file was ever going to be uploaded." /> nothing
        ran
      </p>
    ) : (
      <p className="att-none">
        <Mark
          kind="partial"
          say="This task finished without a result summary, which is where the file list is written. The attempt may well have produced files; this is a missing record, not an empty one."
        />{' '}
        finished with no summary
      </p>
    )
  } else if (!ok(listing)) {
    body = listing.status === 'error' || listing.status === 'stale' ? <ReadFailed error={listing.error} what="the file list" /> : <ReadFailed error={null} what="the file list" />
  } else {
    const listed = listing.data.artifacts
    const manifest = manifestOf(summary)
    const names = new Set(listed.map((e) => e.name))
    const rest = manifest === null ? [] : manifest.filter((e) => !names.has(e.name))
    for (const e of rest) unlisted.add(e.name)
    // Past the page, no kind and no role: the server named neither, and a
    // name rule here would be a second copy of the server's one table.
    entries = [...listed, ...rest.map((e): ArtifactEntry => ({ name: e.name, bytes: e.bytes, uri: e.uri, kind: null, role: null }))]
    cut = { listed: listed.length, total: manifest === null ? null : listed.length + rest.length }
    skipped = Array.isArray(listing.data.artifacts_skipped) ? listing.data.artifacts_skipped : []
    body =
      entries.length === 0 ? (
        <p className="att-none">
          <Mark kind="zero" say="The last attempt uploaded no files. The list is complete and empty, not missing." /> none
          uploaded
        </p>
      ) : null
  }

  const showing = open === null ? null : (entries.find((e) => e.name === open) ?? null)
  const images = entries.filter((e) => e.kind === 'image')

  // THE NOTE BESIDE THE COUNT: each thing that makes this list less than a
  // plain answer, as a mark and a few words.
  const notes: ReactNode[] = []
  if (cut !== null && cut.total !== null && cut.total > cut.listed) {
    notes.push(
      <span key="cut">
        <Mark
          kind="partial"
          say={`The listing route returned ${cut.listed} of this run's ${cut.total} files: it answers one page and cannot be paged past it. The other ${cut.total - cut.listed} are listed from the task's own manifest, the record the route serves. The server named no kind for them, so each opens by its name, and open full lets the raw route decide what the bytes are.`}
        />{' '}
        {cut.listed} of {cut.total} listed
      </span>,
    )
  } else if (cut !== null && cut.total === null && cut.listed >= ARTIFACT_PAGE_LIMIT) {
    notes.push(
      <span key="full">
        <Mark
          kind="partial"
          say={`The listing route returned a full page of ${cut.listed} files, and this task's summary carries no manifest to check it against, so the run may have more files than are shown.`}
        />{' '}
        first {cut.listed} listed
      </span>,
    )
  }
  if (skipped.length > 0) {
    notes.push(
      <span key="skipped">
        <Mark
          kind="partial"
          say={`At least ${skipped.length} file${skipped.length === 1 ? ' was' : 's were'} dropped at the size cap, so this list is incomplete: ${skipped.join(', ')}`}
        />{' '}
        {skipped.length} over cap
      </span>,
    )
  }
  if (unmasked.length > 0) {
    notes.push(
      <span key="unmasked">
        <span className="art-masked is-warn">{unmasked.length}</span> not scrubbed before upload
      </span>,
    )
  }
  return (
    <div className="arts-block arts-files">
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">files</span>
        {entries.length > 0 && <span className="count-chip">{entries.length}</span>}
        {notes.length > 0 && (
          <span className="is-end ctl-card-note">
            {notes.map((n, i) => (
              <Fragment key={i}>
                {i > 0 && ' · '}
                {n}
              </Fragment>
            ))}
          </span>
        )}
      </div>
      {body}
      {entries.length > 0 && (
        <div className="ctl-table is-stacked">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">File</th>
                <th role="columnheader" scope="col" className="is-num">Size</th>
                <th role="columnheader" scope="col">Get</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {entries.map((e) => (
                <FileRow
                  key={e.uri || e.name}
                  task={task.id}
                  entry={e}
                  unlisted={unlisted.has(e.name)}
                  open={e.name === open}
                  onOpen={() => setOpen((cur) => (cur === e.name ? null : e.name))}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {showing !== null && showing.kind !== 'image' && showing.kind !== 'binary' && (
        <ArtifactViewer
          // One viewer per file: a window paged in one must not carry its offset to the next.
          key={showing.name}
          taskId={task.id}
          artifact={showing}
          kind={showing.kind ?? null}
          onClose={() => setOpen(null)}
        />
      )}
      {showing !== null && showing.kind === 'binary' && (
        <div className="art-viewer" role="region" aria-label={`Artifact ${showing.name}`}>
          <p className="art-binary-head">binary · {bytesLabel(showing.bytes)}</p>
        </div>
      )}
      {images.length > 0 && (
        <div className="arts-gallery">
          {images.map((e) => (
            <ImageFigure key={e.uri || e.name} task={task.id} entry={e} />
          ))}
        </div>
      )}
    </div>
  )
}

/** The words for a kind, as a reader says them. */
const KIND_WORD: Readonly<Record<ArtifactKindName, string>> = {
  markdown: 'markdown',
  json: 'json',
  ndjson: 'ndjson',
  log: 'log',
  text: 'text',
  image: 'image',
  binary: 'binary',
}

const ROLE_WORD: Readonly<Record<string, string>> = {
  agent_stdout: 'agent stdout',
  agent_stderr: 'agent stderr',
  agent_transcript: 'agent transcript',
}

function FileRow({
  task,
  entry,
  unlisted,
  open,
  onOpen,
}: {
  task: string
  entry: ArtifactEntry
  /** True when the listing route did not return this file and the row comes from the task's manifest. */
  unlisted: boolean
  open: boolean
  onOpen: () => void
}) {
  const kind = entry.kind ?? null
  const role = entry.role ?? null
  return (
    <tr role="row" className={open ? 'is-open' : undefined}>
      <th role="rowheader" scope="row">
        {kind === 'image' ? (
          // Drawn in the gallery below; the row is how to get the file.
          <span className="mono">{entry.name}</span>
        ) : (
          <button type="button" className="art-open" aria-expanded={open} onClick={onOpen}>
            {entry.name}
          </button>
        )}
        {unlisted ? (
          <span className="ctl-sub">from the task’s manifest · kind not served</span>
        ) : (
          (kind !== null || role !== null) && (
            <span className="ctl-sub">
              {kind !== null ? KIND_WORD[kind] : ''}
              {kind !== null && role !== null ? ' · ' : ''}
              {role !== null ? `${ROLE_WORD[role] ?? role} · under Logs` : ''}
            </span>
          )
        )}
      </th>
      <td role="cell" data-label="Size" className="is-num">
        {bytesLabel(entry.bytes)}
      </td>
      <td role="cell" data-label="Get">
        <span className="arts-get">
          {/* THROUGH THE API, as an attachment: the raw route redacts text
              window by window and sends an image as stored. Never `read()`,
              which would call a byte stream an expired session. */}
          <a className="copy" href={artifactRawUrl(task, entry.name, 'attachment')} download={fileName(entry.name)}>
            download
          </a>
          {/* OPEN FULL for an image, and for any file the server named no
              kind for: the raw route sniffs the bytes and serves an image as
              an image, text as text/plain and anything else as a download,
              so no name rule here has to guess. */}
          {(kind === 'image' || kind === null) && (
            <a className="copy" href={artifactRawUrl(task, entry.name, 'inline')} target="_blank" rel="noreferrer">
              open full
            </a>
          )}
          <button
            type="button"
            className="copy"
            title={entry.uri}
            onClick={() => navigator.clipboard?.writeText(`gsutil cp ${entry.uri} .`)}
          >
            copy gsutil
          </button>
        </span>
      </td>
    </tr>
  )
}

/**
 * ONE IMAGE, INLINE, FROM THE RAW ROUTE. `onError` is the honesty rule here:
 * an expired sign-in arrives as a page, not an image, and fails to decode --
 * without the mark that would be a blank box that reads as a blank screenshot.
 */
function ImageFigure({ task, entry }: { task: string; entry: ArtifactEntry }) {
  const [failed, setFailed] = useState(false)
  const inline = entry.bytes <= INLINE_IMAGE_MAX_BYTES
  const src = artifactRawUrl(task, entry.name, 'inline')
  return (
    <figure className="arts-figure">
      {!inline ? (
        <p className="att-none">
          <Mark
            kind="partial"
            say={`This image is ${bytesLabel(entry.bytes)}, over the ${bytesLabel(INLINE_IMAGE_MAX_BYTES)} drawn inline. Open it full size or download it.`}
          />{' '}
          too large to draw inline
        </p>
      ) : failed ? (
        <p className="att-none">
          <Mark
            kind="unread"
            say="The browser could not decode what the API returned for this image. An expired sign-in arrives as a page rather than an image, and a file named as an image may not be one; download it to see what it is."
          />{' '}
          image could not be loaded
        </p>
      ) : (
        <img src={src} alt={entry.name} loading="lazy" onError={() => setFailed(true)} />
      )}
      <figcaption>
        <span className="mono">{entry.name}</span>{' '}
        <a className="copy" href={src} target="_blank" rel="noreferrer">
          open full
        </a>
      </figcaption>
    </figure>
  )
}

/** The last path segment, for a download's suggested file name. The server sets its own too. */
function fileName(name: string): string {
  return name.split('/').pop() || name
}

/**
 * A READ THAT DID NOT COMPLETE, drawn as that and never as an absence. A path
 * this deployment does not serve -- an API older than #184 -- is said
 * separately, because the fix is a deploy, not a retry.
 */
function ReadFailed({ error, what }: { error: ApiError | null; what: string }) {
  if (error !== null && routeMissing(error)) {
    return (
      <p className="att-none">
        <Mark
          kind="absent"
          say={`The deployment answering this UI does not serve ${what} yet. That says nothing about the run; it is an API older than this screen.`}
        />{' '}
        not served by this API
      </p>
    )
  }
  return (
    <p className="att-none">
      <Mark
        kind="unread"
        say={`${error === null ? 'The read did not complete' : errorHeading(error)}. Nothing may be concluded about ${what}: it is not empty and it is not missing.`}
      />{' '}
      {what} not read
      {/* A separator before the server's words: the flex gap parts them on
          screen, and nothing parted them in the text (PR #210 review). */}
      {error !== null && <span className="ctl-sub">{` · ${error.message}`}</span>}
    </p>
  )
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

type LogView = 'transcript' | 'stdout' | 'stderr' | 'runner'

const LOG_VIEWS: readonly { id: LogView; label: string }[] = [
  { id: 'transcript', label: 'transcript' },
  { id: 'stdout', label: 'stdout' },
  { id: 'stderr', label: 'stderr' },
  { id: 'runner', label: 'runner (platform)' },
]

/**
 * THE AGENT'S LOGS: its transcript as steps, its own stdout and stderr, and
 * -- behind the last choice, because it is the platform's and not the agent's
 * -- the runner process's streams, which the Details pane used to present as
 * the agent's output.
 */
function Logs({ v, reading, now }: { v: ArtifactsView; reading: ScreenReading; now: number }) {
  const [view, setView] = useState<LogView>('transcript')
  const logs = v.logs
  return (
    <section className="section arts-logs">
      <h2>Logs</h2>
      <div className="ctl-toolbar att-sub-head">
        <div className="ctl-seg arts-view-seg" role="group" aria-label="Which log">
          {LOG_VIEWS.map((o) => (
            <button key={o.id} type="button" aria-pressed={view === o.id} onClick={() => setView(o.id)}>
              {o.label}
            </button>
          ))}
        </div>
        {ok(logs) && (
          <span className="is-end ctl-card-note">
            {logs.data.attempt.status === 'latest' || logs.data.attempt.status === 'requested'
              ? `${attemptLine(logs.data).toLowerCase()} · `
              : ''}
            masking{' '}
            <span className={`art-masked${logs.data.redaction.applied_at_read_time ? '' : ' is-warn'}`}>
              {logs.data.redaction.applied_at_read_time ? 'at read time' : 'not applied at read time'}
            </span>
          </span>
        )}
      </div>
      {view === 'transcript' && <TranscriptView v={v} reading={reading} now={now} />}
      {view === 'stdout' && <AgentStdout v={v} reading={reading} now={now} />}
      {view === 'stderr' && <StreamsFrom logs={logs} names={['agent_stderr']} task={v.task} now={now} />}
      {view === 'runner' && <StreamsFrom logs={logs} names={['stdout', 'stderr']} task={v.task} now={now} />}
    </section>
  )
}

/**
 * THE AGENT'S RAW STDOUT, READ ONLY WHILE THIS VIEW IS OPEN. It is the one
 * stream that can be large -- a whole stream-json transcript -- and the
 * transcript view already serves it parsed, so the pane's poll leaves it out
 * and this view reads it itself, again with every poll while it is open.
 */
function AgentStdout({ v, reading, now }: { v: ArtifactsView; reading: ScreenReading; now: number }) {
  const held = useRead<TaskLogs>(
    () => loadTaskLogs(v.task.id, { stream: 'agent_stdout', source: 'auto' }),
    v.task.id,
    `${reading.fetchedAt}`,
    null,
  )
  return <StreamsFrom logs={held.state} names={['agent_stdout']} task={v.task} now={now} />
}

/**
 * Some of a log read's streams: one table -- stream, size, age, with the four
 * answers as marks -- and each stream's window below it. A stream the read
 * did not return at all is `not served`: an API older than #184 answers only
 * the runner's two.
 *
 * THE ONE PLACE A LOG IS DRAWN. The Details pane drew the runner's two streams
 * in a panel of its own with these same rows; the owner's decision of
 * 2026-09-25 (#184) put the runner (platform) log in Artifacts › Logs only.
 * EXPORTED so `runfiles.flat.test.tsx` holds the rows' encodings here, where
 * the runner's log now lives.
 */
export function StreamsFrom({
  logs,
  names,
  task,
  now,
}: {
  logs: Result<TaskLogs>
  names: readonly LogStreamName[]
  task: Task
  now: number
}) {
  if (logs.status === 'loading') {
    return (
      <p className="art-loading">
        <Mark kind="pending" say="Reading the stream. The read is in flight." />
        <span className="ctl-pending art-loading-bar" />
      </p>
    )
  }
  if (logs.status === 'error' || logs.status === 'stale') return <ReadFailed error={logs.error} what="the log" />
  if (logs.status === 'empty') return <ReadFailed error={null} what="the log" />
  const data = logs.data
  if (data.attempt.status === 'no_attempt_yet') {
    return <Absent kind="zero" heading="No attempt yet" say="This task has no attempt yet, so nothing has written a log." />
  }
  if (data.attempt.status === 'unknown_attempt') {
    return (
      <Absent
        kind="failed"
        heading="No such attempt"
        say="The attempt asked for is not one this task has, so there is no log of it to read."
      />
    )
  }
  const rows = names.map((n) => ({ name: n, stream: data.streams.find((s) => s.stream === n) ?? null }))
  // The read's own receipt time, for the served ages: the pane's for the
  // polled read, this view's own for the stdout read -- `fetchedAt` either way.
  const fetchedAt = logs.fetchedAt
  return (
    <>
      <div className="ctl-table is-stacked">
        <table role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Stream</th>
              <th role="columnheader" scope="col" className="is-num">Size</th>
              <th role="columnheader" scope="col">Age</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {rows.map((r) =>
              r.stream === null ? (
                <tr role="row" key={r.name}>
                  <th role="rowheader" scope="row">
                    <span className="mono">{r.name}</span>
                  </th>
                  <td role="cell" data-label="Size" className="is-num">
                    <Em />{' '}
                    <Mark
                      kind="absent"
                      say={`The log read did not return ${r.name} at all. The deployment answering this UI is older than the agent streams; nothing here says the stream is empty.`}
                    />{' '}
                    not served
                  </td>
                  <td role="cell" data-label="Age">
                    <Em />
                  </td>
                </tr>
              ) : (
                <Stream key={r.name} stream={r.stream} logs={data} task={task} now={now} fetchedAt={fetchedAt} />
              ),
            )}
          </tbody>
        </table>
      </div>
      {rows.map((r) =>
        r.stream !== null && r.stream.status === 'ok' && r.stream.content !== null && r.stream.content !== '' ? (
          <div key={r.name} className="rf-window">
            <span className="ctl-eyebrow">{r.name}</span>
            <pre className="logwin-body">{r.stream.content}</pre>
          </div>
        ) : null,
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// The transcript
// ---------------------------------------------------------------------------

/**
 * THE TRANSCRIPT AS STEPS: text, thinking, every tool call with its input
 * and its result collapsed, and the final result. Parsed and redacted by the
 * server, never here; this draws what it was given and says what it was not.
 *
 * THE WINDOW IS SAID BEFORE THE FIRST ROW. A live tail that starts past the
 * first line has earlier steps nobody drew, and a final transcript longer than
 * one window has later ones: both are said, and the second has `next window`.
 * A run made with `--output-format json` recorded only its final result --
 * that is one step, and it is labelled as that rather than drawn as a short
 * transcript.
 */
function TranscriptView({ v, reading, now }: { v: ArtifactsView; reading: ScreenReading; now: number }) {
  const [records, setRecords] = useState(false)
  return records ? (
    <WithRecords v={v} reading={reading} now={now} onHide={() => setRecords(false)} />
  ) : (
    <TranscriptBody
      read={v.transcript}
      v={v}
      reading={reading}
      now={now}
      records={false}
      onRecords={() => setRecords(true)}
    />
  )
}

/** The same transcript with each step's redacted source record: a read of its own, with `include_raw`. */
function WithRecords({
  v,
  reading,
  now,
  onHide,
}: {
  v: ArtifactsView
  reading: ScreenReading
  now: number
  onHide: () => void
}) {
  const held = useRead<TaskTranscript>(
    () => loadTranscript(v.task.id, { source: 'auto', includeRaw: true }),
    v.task.id,
    `${reading.fetchedAt}`,
    null,
  )
  if (held.state.status === 'loading') {
    return (
      <p className="art-loading">
        <Mark kind="pending" say="Reading the transcript with its records. The read is in flight." />
        <span className="ctl-pending art-loading-bar" />
      </p>
    )
  }
  return <TranscriptBody read={held.state} v={v} reading={reading} now={now} records onRecords={onHide} />
}

function TranscriptBody({
  read,
  v,
  reading,
  now,
  records,
  onRecords,
}: {
  read: Result<TaskTranscript>
  v: ArtifactsView
  reading: ScreenReading
  now: number
  records: boolean
  onRecords: () => void
}) {
  // Later windows of a FINAL transcript, read on request and kept in order.
  // Keyed by the first window's identity, so a new attempt or a switch from
  // the live tail to the final object starts over rather than appending one
  // object's steps to another's.
  const [more, setMore] = useState<{ key: string; windows: TaskTranscript[]; failed: ApiError | null }>({
    key: '',
    windows: [],
    failed: null,
  })
  const [reading2, setReading2] = useState(false)

  if (read.status === 'error' || read.status === 'stale') return <ReadFailed error={read.error} what="the transcript" />
  if (read.status !== 'ok') return <ReadFailed error={null} what="the transcript" />
  const t = read.data
  const key = `${t.attempt_id ?? ''}:${t.stream.source ?? ''}:${t.stream.uri ?? ''}:${records}`
  const extra = more.key === key ? more.windows : []
  const last = extra.length > 0 ? extra[extra.length - 1]! : t
  const next = t.stream.source === 'live' ? null : last.stream.next_offset
  const fetchedAt = read.fetchedAt
  const age = servedAge(t.stream, fetchedAt, now)
  const steps = [...(t.steps ?? []), ...extra.flatMap((w) => w.steps ?? [])]
  const skipped = t.skipped_lines + extra.reduce((n, w) => n + w.skipped_lines, 0)
  const masked = t.redaction_count + extra.reduce((n, w) => n + w.redaction_count, 0)

  const readNext = async () => {
    if (next === null) return
    setReading2(true)
    const got = await loadTranscript(v.task.id, {
      source: t.stream.source === 'artifact' ? 'auto' : 'final',
      offset: next,
      includeRaw: records,
    })
    setReading2(false)
    setMore((cur) => {
      const base = cur.key === key ? cur : { key, windows: [], failed: null }
      return got.status === 'ok'
        ? { key, windows: [...base.windows, got.data], failed: null }
        : { key, windows: base.windows, failed: got.status === 'error' || got.status === 'stale' ? got.error : null }
    })
  }

  return (
    <div className="arts-transcript">
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>source</b>
          {t.stream.source === 'live' ? (
            <>
              live
              {age !== null ? (
                <span className="ctl-sub">published {ageSpan(age)} ago</span>
              ) : (
                <>
                  {' '}
                  <Mark kind="absent" say="The server did not say how old this live object is." />
                </>
              )}
            </>
          ) : t.stream.source === 'final' ? (
            'final'
          ) : t.stream.source === 'artifact' ? (
            'the artifact'
          ) : (
            <Em />
          )}
        </li>
        {t.format !== null && (
          <li className="ctl-fact">
            <b>format</b>
            {t.format}
          </li>
        )}
        {t.capture_truncated === true && (
          <li className="ctl-fact is-absent">
            <b>capture</b>
            cut at its cap{' '}
            <Mark
              kind="partial"
              say="The agent's output passed the worker's capture size cap. The capture kept the start and the end of the run and dropped the middle, with a notice where, so this transcript is not the whole run."
            />
          </li>
        )}
        {skipped > 0 && (
          <li className="ctl-fact is-absent">
            <b>skipped</b>
            {num(skipped)}{' '}
            <Mark kind="partial" say={`${skipped} line${skipped === 1 ? '' : 's'} of the stream did not parse and are not drawn as steps. Counted, never dropped silently.`} />
          </li>
        )}
        <li className="ctl-fact">
          <b>masked</b>
          <span className={`art-masked${masked > 0 ? ' is-warn' : ''}`}>{masked}</span>
        </li>
        <li className="ctl-fact">
          <button type="button" className="copy" onClick={onRecords}>
            {records ? 'hide records' : 'show records'}
          </button>
        </li>
      </ul>
      {/* THE SERVER'S OWN SENTENCE ABOUT THIS WINDOW, when it read one: a
          capture cut at its cap, a line longer than the window, a window that
          began inside such a line. The absent and unreadable answers draw it
          beside their own mark; an `ok` read that carries one is saying the
          window is less than it looks. */}
      {t.stream.status === 'ok' && t.stream.detail !== null && (
        <p className="arts-window-note">
          <Mark kind="partial" say="The server says this window is less than it looks, in the words beside this mark." />{' '}
          {t.stream.detail}
        </p>
      )}
      <TranscriptSteps t={t} steps={steps} v={v} reading={reading} now={now} />
      {next !== null && (
        <p className="arts-window-note">
          <Mark
            kind="partial"
            say={`The transcript continues past this window, from byte ${next}. Later steps are not drawn until the next window is read.`}
          />{' '}
          later steps are in the next window{' '}
          <button type="button" className="copy" onClick={() => void readNext()} disabled={reading2}>
            {reading2 ? 'reading…' : 'next window'}
          </button>
        </p>
      )}
      {more.key === key && more.failed !== null && <ReadFailed error={more.failed} what="the next window" />}
    </div>
  )
}

function TranscriptSteps({
  t,
  steps,
  v,
  reading,
  now,
}: {
  t: TaskTranscript
  steps: TranscriptStep[]
  v: ArtifactsView
  reading: ScreenReading
  now: number
}) {
  const s = t.stream
  if (s.status === 'not_applicable') {
    return (
      <p className="att-none">
        <Mark kind="zero" say="This runner has no agent CLI, so it writes no transcript. Nothing is missing." /> no agent CLI
      </p>
    )
  }
  if (s.status === 'absent') {
    return TERMINAL_STATES.has(v.task.state) ? (
      <p className="att-none">
        <Mark
          kind="absent"
          say="No transcript object exists for this attempt: nothing was uploaded, and no live tail was published."
        />{' '}
        no transcript recorded
        {s.detail !== null && <span className="ctl-sub">{s.detail}</span>}
      </p>
    ) : (
      <p className="att-none">
        <Mark
          kind="pending"
          say="Nothing has been published for this attempt yet. The agent's output is published every few seconds while it runs."
        />{' '}
        nothing published yet
      </p>
    )
  }
  if (s.status === 'unreadable') {
    return (
      <p className="att-none">
        <Mark kind="unread" say="The transcript object exists and could not be read. Nothing here says it is empty." />{' '}
        transcript not read
        {s.detail !== null && <span className="ctl-sub">{s.detail}</span>}
      </p>
    )
  }
  if (t.steps === null) {
    // PLAIN TEXT, OR NOT PARSED: the stream is shown as it was written.
    return (
      <>
        <p className="att-none">
          <Mark
            kind="partial"
            say={
              t.format === 'text'
                ? 'This agent writes plain text, not a stream of events, so there are no steps to draw. Its stdout is below, as written.'
                : 'This stream did not parse into steps, so it is shown below as written.'
            }
          />{' '}
          {t.format === 'text' ? 'plain text · no steps' : 'not parsed · shown as written'}
        </p>
        <AgentStdout v={v} reading={reading} now={now} />
      </>
    )
  }
  return (
    <>
      {t.format === 'claude-json' && (
        <p className="arts-window-note">
          <Mark
            kind="partial"
            say="This run printed one JSON document at the end, so its turns were never recorded. Turn-by-turn steps need the agent to run with --output-format stream-json."
          />{' '}
          this run recorded only its final result; turn-by-turn steps need stream-json
        </p>
      )}
      {t.window_starts_mid_stream && (
        <p className="arts-window-note">
          <Mark
            kind="partial"
            say="This window starts part-way through the stream: a live tail holds only the newest part. The whole transcript is read when the attempt ends."
          />{' '}
          earlier steps are not in this window
        </p>
      )}
      {steps.length === 0 ? (
        <p className="att-none">
          <Mark kind="zero" say="The stream was read and parsed, and this window holds no step." /> no steps in this window
        </p>
      ) : (
        <Steps steps={steps} />
      )}
    </>
  )
}

/**
 * THE STEPS, IN ORDER, with two joins the flat list cannot show: a tool call
 * and its result are ONE row (by `tool.id` and `tool_result.tool_use_id`),
 * and a sub-agent's steps sit under the tool call that spawned it (by
 * `parent_tool_use_id`). A result whose call is not in this window, and a
 * step whose parent is not, are drawn at the top level rather than dropped.
 */
function Steps({ steps }: { steps: TranscriptStep[] }) {
  // A NULL ID JOINS NOTHING. The server sends `tool.id` and
  // `tool_result.tool_use_id` as null when the event carried no string there;
  // two nulls are two unknowns, not a call and its result, so neither side is
  // entered into the join and each is drawn on its own.
  const calls = new Set<string>()
  for (const s of steps) if (s.kind === 'tool_call' && s.tool !== null && s.tool.id !== null) calls.add(s.tool.id)
  const results = new Map<string, TranscriptStep>()
  for (const s of steps) {
    const use = s.kind === 'tool_result' && s.tool_result !== null ? s.tool_result.tool_use_id : null
    if (use !== null && calls.has(use)) results.set(use, s)
  }
  const children = new Map<string, TranscriptStep[]>()
  const top: TranscriptStep[] = []
  for (const s of steps) {
    const use = s.kind === 'tool_result' && s.tool_result !== null ? s.tool_result.tool_use_id : null
    if (use !== null && results.get(use) === s) continue
    const parent = s.parent_tool_use_id
    if (parent !== null && calls.has(parent) && !(s.kind === 'tool_call' && s.tool?.id === parent)) {
      const list = children.get(parent)
      if (list) list.push(s)
      else children.set(parent, [s])
    } else {
      top.push(s)
    }
  }
  return <StepList steps={top} results={results} nested={children} seen={new Set<string>()} />
}

function StepList({
  steps,
  results,
  nested,
  seen,
}: {
  steps: TranscriptStep[]
  results: Map<string, TranscriptStep>
  /** Sub-agent steps, by the id of the tool call that spawned them. */
  nested: Map<string, TranscriptStep[]>
  /** Tool calls already drawn above this list: a cycle in the parent ids stops here. */
  seen: Set<string>
}) {
  return (
    <ol className="arts-steps">
      {steps.map((s) => (
        <StepRow key={s.id} step={s} results={results} nested={nested} seen={seen} />
      ))}
    </ol>
  )
}

function StepRow({
  step,
  results,
  nested,
  seen,
}: {
  step: TranscriptStep
  results: Map<string, TranscriptStep>
  nested: Map<string, TranscriptStep[]>
  seen: Set<string>
}) {
  const m: Record<string, unknown> = step.meta ?? {}
  const capped = step.truncated_fields.length > 0 && (
    <Mark
      kind="partial"
      say={`Capped at 16 KiB by the server: ${step.truncated_fields.join(', ')}. The rest of ${step.truncated_fields.length === 1 ? 'that field' : 'those fields'} is not shown.`}
    />
  )
  const record = step.raw !== null && (
    <details className="arts-more">
      <summary>record</summary>
      <pre className="art-code">{step.raw}</pre>
    </details>
  )
  const text = (t: string | null): ReactNode =>
    t === null ? null : t === '' ? (
      <p className="art-fallback">
        <Mark kind="zero" say="This step carries an empty text." /> no text
      </p>
    ) : (
      <Markdown source={t} />
    )

  switch (step.kind) {
    case 'text':
      return (
        <li className="arts-step is-text">
          <span className="arts-step-kind">{step.role ?? 'assistant'}</span> {capped}
          {text(step.text)}
          {record}
        </li>
      )
    case 'thinking':
      return (
        <li className="arts-step is-thinking">
          {m.redacted === true || step.text === null ? (
            <span className="arts-step-kind">
              thinking <Mark kind="absent" say="The provider redacted this thinking block, so there is no text to show." />
            </span>
          ) : (
            <details className="arts-more">
              <summary>thinking</summary>
              <pre className="art-code">{step.text}</pre>
            </details>
          )}{' '}
          {capped}
          {record}
        </li>
      )
    case 'tool_call': {
      const id = step.tool?.id ?? null
      const input = step.tool?.input ?? null
      const result = id === null ? undefined : results.get(id)
      const kids = id === null || seen.has(id) ? [] : (nested.get(id) ?? [])
      const nextSeen = id === null ? seen : new Set([...seen, id])
      const r = result?.tool_result ?? null
      return (
        <li className="arts-step is-tool">
          <details className="arts-more">
            <summary>
              tool · <b>{step.tool?.name ?? 'unnamed'}</b>
            </summary>
            {input === null ? (
              <p className="art-fallback">
                <Mark kind="absent" say="This tool call carries no input." /> no input
              </p>
            ) : (
              <pre className="art-code">{input}</pre>
            )}
          </details>{' '}
          {capped}
          {r !== null ? (
            <details className={`arts-more${r.is_error ? ' is-bad' : ''}`}>
              <summary>
                result{r.is_error ? ' · error' : ''}
                {r.images > 0 ? ` · ${r.images} image${r.images === 1 ? '' : 's'} not shown` : ''}
              </summary>
              {r.content === null ? (
                <p className="art-fallback">
                  <Mark kind="absent" say="This tool result carries no text." /> no text
                </p>
              ) : (
                <pre className="art-code">{r.content}</pre>
              )}
            </details>
          ) : (
            <span className="ctl-sub">no result in this window</span>
          )}
          {record}
          {kids.length > 0 && <StepList steps={kids} results={results} nested={nested} seen={nextSeen} />}
        </li>
      )
    }
    case 'tool_result': {
      const r = step.tool_result
      return (
        <li className="arts-step is-tool">
          <details className={`arts-more${r?.is_error ? ' is-bad' : ''}`}>
            <summary>
              tool result{r?.is_error ? ' · error' : ''}
              <span className="ctl-sub">its call is not in this window</span>
            </summary>
            {r === null || r.content === null ? (
              <p className="art-fallback">
                <Mark kind="absent" say="This tool result carries no text." /> no text
              </p>
            ) : (
              <pre className="art-code">{r.content}</pre>
            )}
          </details>{' '}
          {capped}
          {record}
        </li>
      )
    }
    case 'result': {
      const turns = typeof m.num_turns === 'number' ? m.num_turns : null
      return (
        <li className="arts-step is-result">
          <span className="arts-step-kind">
            result
            {typeof m.subtype === 'string' ? ` · ${m.subtype}` : ''}
            {turns !== null ? ` · ${turns} turns` : ''}
          </span>{' '}
          {m.is_error === true && <Chip tone="bad">the agent reported an error</Chip>} {capped}
          {text(step.text)}
          {record}
        </li>
      )
    }
    case 'init': {
      return (
        <li className="arts-step is-meta">
          <span className="arts-step-kind">
            init
            {typeof m.model === 'string' ? ` · ${m.model}` : ''}
            {typeof m.tool_count === 'number' ? ` · ${m.tool_count} tools` : ''}
          </span>
          {record}
        </li>
      )
    }
    case 'rate_limit':
      return (
        <li className="arts-step is-meta">
          <span className="arts-step-kind">rate limit</span> <Scalars meta={step.meta} />
          {record}
        </li>
      )
    case 'system':
      // THE CAPTURE'S OWN NOTICE is a system step (`meta.subtype:
      // "capture_truncated"`) whose text is the line the worker wrote where it
      // dropped the middle of the run: what was cut is in those words, so they
      // are drawn, with the mark that says this is where the run is not whole.
      return (
        <li className="arts-step is-meta">
          <span className="arts-step-kind">system{typeof m.subtype === 'string' ? ` · ${m.subtype}` : ''}</span>
          {m.subtype === 'capture_truncated' && (
            <>
              {' '}
              <Mark
                kind="partial"
                say="The worker's capture of the agent's output was cut at its size cap here: the steps before and after this line are the run's start and its end, and the middle between them was not kept."
              />
            </>
          )}
          {step.text !== null && step.text !== '' && <pre className="art-code">{step.text}</pre>}
          {record}
        </li>
      )
    case 'other':
      // `meta.oversize`: ONE LINE LONGER THAN THE WINDOW, which the server
      // does not decode and says so in the window's `stream.detail` (drawn
      // above the steps). The step carries no text, so without its own words
      // it is an empty `other` row that reads as an event with nothing in it.
      if (m.oversize === true) {
        return (
          <li className="arts-step is-meta">
            <span className="arts-step-kind">
              line longer than this window{' '}
              <Mark
                kind="partial"
                say="One line of the agent's output is longer than the window the server reads, so it was not decoded into a step and nothing of it is shown here. Its bytes are in the agent's stdout."
              />
            </span>
            {record}
          </li>
        )
      }
      return (
        <li className="arts-step is-meta">
          <span className="arts-step-kind">other{typeof m.raw_type === 'string' ? ` · ${m.raw_type}` : ''}</span>{' '}
          {capped}
          {step.text !== null && <pre className="art-code">{step.text}</pre>}
          {typeof m.detail === 'string' && <span className="ctl-sub">{m.detail}</span>}
          {record}
        </li>
      )
  }
}

/** A step's top-level scalars, as `key value` pairs. */
function Scalars({ meta }: { meta: Record<string, unknown> | null }) {
  const pairs = Object.entries(meta ?? {}).filter(
    ([, x]) => typeof x === 'string' || typeof x === 'number' || typeof x === 'boolean',
  )
  if (pairs.length === 0) return null
  return <span className="ctl-sub">{pairs.map(([k, x]) => `${k} ${String(x)}`).join(' · ')}</span>
}
