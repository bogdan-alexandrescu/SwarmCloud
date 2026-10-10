import { useEffect, useState, type ReactNode } from 'react'
import { loadWorkflow, loadWorkflowUsage, type StepUsage, type WorkflowRead } from './api'
import { Dash } from './components/Chip'
import { PARK_WORD } from './components/StatePill'
import { workflowSpend } from './dag'
import { spanText } from './duration'
import { NamedMark, StateMark } from './marks'
import { usd } from './measure'
import { timeAgo } from './Shell'
import { mergeCardOf, type MergeCard } from './stepviews'
import { TERMINAL_STATES } from './types'
import { MergeStepCard } from './WorkflowViews'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import type { IssueRun, IssueRunState, Task, TaskState } from './types'

/**
 * THE RUN'S STEPS, LIVE (lane U14, owner 2026-10-04, after the RUNNING-stage
 * QA of run_7a37942a19aa4d2c80d1): while the run was RUNNING its page drew no
 * step row and no link to the running agent -- the running task was reachable
 * only through the workflow id under Linked. This file reads the run's
 * workflow, and each CI fix round's, through the existing `loadWorkflow`, on
 * the page's own cadence: every time the run is read (15 s while it is not
 * finished), so nothing here polls on its own.
 *
 * WHAT IS NOT READ IS SAID. A workflow that could not be read keeps its last
 * read with the reason beside it, or says it could not be read; a step whose
 * task is absent from the read is "state not read", never "queued"; a cost no
 * step has reported is a dash with that reason, never $0 (redesign-v2.md).
 *
 * Classes are `rn-`, like the rest of the run page, so a later pass can swap
 * them for lane U0's components by name.
 */

/** One workflow the run started: its main workflow (round 0) or a CI fix round's. */
export interface WfLoad {
  wf: string
  /** 0 for the run's workflow; n for CI fix round n. */
  round: number
  /** The last good read, kept while a later read fails. */
  data: WorkflowRead | null
  /** Why the newest read failed; null when it landed. */
  error: string | null
}

/** Every workflow the run started, as last read; null before the first read lands. */
export interface RunWorkflows {
  ids: string[]
  loads: WfLoad[] | null
  /**
   * Each step task's attempts, summed (`loadWorkflowUsage`, the board's read):
   * the only cost a RUNNING step has, since its result is written when it
   * finishes. Null before the first read lands or when every read failed --
   * the result's figure is then all a finished step can offer.
   */
  usage: ReadonlyMap<string, StepUsage> | null
}

/** The run's workflow, then each fix round's, in order. */
function workflowIds(run: IssueRun): string[] {
  const ids = [run.workflow_id, ...(run.ci_fix_workflows ?? [])]
  return ids.filter((id): id is string => typeof id === 'string' && id !== '')
}

/**
 * Read the run's workflows every time the run is read. The page re-reads the
 * run every 15 s until it ends, and hands this hook a new run each time; a
 * finished run is read once, so its workflows are too.
 */
export function useRunWorkflows(run: IssueRun): RunWorkflows {
  const ids = workflowIds(run)
  const key = ids.join(' ')
  const [state, setState] = useState<{ key: string; loads: WfLoad[] } | null>(null)
  const [usage, setUsage] = useState<{ key: string; byTaskId: ReadonlyMap<string, StepUsage> } | null>(null)
  useEffect(() => {
    if (ids.length === 0) return
    let live = true
    void Promise.all(ids.map((wf) => loadWorkflow(wf))).then((reads) => {
      if (!live) return
      setState((was) => {
        const prior = was !== null && was.key === key ? was.loads : []
        return {
          key,
          loads: ids.map((wf, round) => {
            const r = reads[round]!
            if (r.status === 'ok' || r.status === 'stale') return { wf, round, data: r.data, error: null }
            const why = r.status === 'error' ? r.error.message : r.status === 'empty' ? 'the API returned no workflow' : 'the read did not finish'
            return { wf, round, data: prior[round]?.data ?? null, error: why }
          }),
        }
      })
      // The steps' attempts, on the same read: a running step's cost lives
      // only there. Re-read every time (unlike the board, which re-reads on a
      // changed set of tasks) because a running attempt's figure moves while
      // the set stays the same. A failed read keeps the last one.
      const taskIds = reads.flatMap((r) =>
        r.status === 'ok' || r.status === 'stale'
          ? r.data.workflow.steps.map((s) => s.task_id ?? '').filter((id) => id !== '')
          : [],
      )
      if (taskIds.length === 0) return
      void loadWorkflowUsage(taskIds).then((u) => {
        if (!live) return
        if (u.status === 'ok' || u.status === 'stale') setUsage({ key, byTaskId: u.data.byTaskId })
      })
    })
    return () => {
      live = false
    }
  }, [run])
  return {
    ids,
    loads: state !== null && state.key === key ? state.loads : null,
    usage: usage !== null && usage.key === key ? usage.byTaskId : null,
  }
}

/** One row of the Steps card: a workflow step, its task when the read has it. */
export interface StepRow {
  key: string
  round: number
  stepId: string
  /** The plan's title for the step, or null for a step the plan does not name (review, fix, a CI round). */
  title: string | null
  taskId: string | null
  /** Null when the step has no task yet, or the read did not carry it. */
  task: Task | null
  dependsOn: string[]
  /** The merge step's card (MS4), from its task and its workflow's; null for every other step. */
  merge: MergeCard | null
}

/** Every step of every workflow the run started that was read, in workflow order. */
export function stepRows(run: IssueRun, read: RunWorkflows): StepRow[] {
  const titles = new Map((run.plan?.steps ?? []).map((s) => [s.step_id, s.title]))
  const rows: StepRow[] = []
  for (const load of read.loads ?? []) {
    if (load.data === null) continue
    const tasks = new Map(load.data.tasks.map((t) => [t.id, t]))
    for (const s of load.data.workflow.steps) {
      const taskId = s.task_id ? s.task_id : null
      const task = taskId === null ? null : (tasks.get(taskId) ?? null)
      rows.push({
        key: `${load.round}:${s.step_id}`,
        round: load.round,
        stepId: s.step_id,
        title: load.round === 0 ? (titles.get(s.step_id) ?? null) : null,
        taskId,
        task,
        dependsOn: s.depends_on ?? [],
        merge: task === null ? null : mergeCardOf(task, load.data.tasks),
      })
    }
  }
  return rows
}

/**
 * A park's reason in the pill's words (`PARK_WORD`): CI_PENDING is "waiting
 * for CI", never "ci pending". A reason this build does not know is printed
 * as it came, lower case.
 */
function parkWord(reason: string): string {
  return (PARK_WORD as Readonly<Record<string, string>>)[reason] ?? reason.toLowerCase().replace(/_/g, ' ')
}

/** A task state in words: lower case, never the API's capitals (PICKS.md). */
function stateWord(state: TaskState): string {
  return state.toLowerCase().replace('_', '-')
}

/**
 * "Open agent →", for every state a step's task can be in, under the agent
 * list that holds it (QA G2-17, 2026-10-07): a finished task opens over
 * Recent and a waiting one over Waiting -- `/agents/live/` for every state
 * opened a finished step's inspector over a Live list without it. A task
 * whose state the workflow read did not carry keeps the Live address: there
 * is no state to route by.
 */
export function OpenAgent({ taskId, state, go }: { taskId: string; state: TaskState | null; go: (to: string) => void }) {
  const href = state === null ? addressToPath(`work/task/${encodeURIComponent(taskId)}`) : agentPath({ id: taskId, state })
  return (
    <a className="rn-open" href={href} title={taskId} onClick={(e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      go(href)
    }}>Open agent →</a>
  )
}

/** A step's state mark: its task's state, "state not read", or what a gated step waits on. */
export function StepMark({ row }: { row: StepRow }) {
  if (row.task !== null) return <StateMark state={row.task.state} />
  if (row.taskId !== null) {
    return <NamedMark mark={null} hue="unknown" dataMark="unknown" word="state not read"
      title="The workflow read did not carry this step's task." />
  }
  return (
    <NamedMark mark="queued" hue="neu" dataMark="waiting"
      word={row.dependsOn.length === 0 ? 'no task yet' : `waiting on ${row.dependsOn.join(', ')}`}
      title="Holds nothing: the step's task is created when what it waits on has finished." />
  )
}

/** "elapsed 4m 10s" from the attempt's start, or "not started". */
function elapsed(task: Task, now: number): string {
  const start = task.started_at === null ? NaN : Date.parse(task.started_at)
  if (!Number.isFinite(start)) return 'not started'
  const end = task.completed_at === null ? now : Date.parse(task.completed_at)
  return `elapsed ${spanText((Number.isFinite(end) ? end : now) - start)}`
}

function attemptText(task: Task): string {
  return task.attempt_count > 0
    ? `attempt ${task.attempt_count} of ${task.max_attempts}`
    : `no attempt yet · up to ${task.max_attempts}`
}

function roundLabel(round: number): string | null {
  return round === 0 ? null : `fix round ${round}`
}

/**
 * "Changes ›": the run's Changes tab filtered to this step (diff-viewer.md §2
 * variant 5), by the filter key the tab gives the step -- its id, or
 * `<round>:<id>` for a CI fix round's.
 */
export function StepChanges({ runId, row, go }: { runId: string; row: StepRow; go: (to: string) => void }) {
  const step = row.round === 0 ? row.stepId : `${row.round}:${row.stepId}`
  const to = `work/runs?${new URLSearchParams({ run: runId, tab: 'changes', step }).toString()}`
  const href = addressToPath(to)
  return (
    <a className="rn-open rn-changes" href={href} title={`What ${row.stepId} changed, in the run's Changes tab`} onClick={(e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      go(to)
    }}>Changes ›</a>
  )
}

function StepLine({ row, runId, go, now }: { row: StepRow; runId: string; go: (to: string) => void; now: number }) {
  const t = row.task
  const round = roundLabel(row.round)
  return (
    <li className="rn-srow" data-step={row.stepId}>
      <StepMark row={row} />
      <span className="rn-srow-n">
        {round !== null && <span className="rn-srow-r">{round} · </span>}
        {row.title !== null && <span className="rn-srow-t">{row.title} </span>}
        <span className="mono rn-srow-id">{row.stepId}</span>
      </span>
      <span className="rn-srow-m sb-note">
        {t === null ? null : (
          <>
            <span>{elapsed(t, now)}</span>
            <span>{attemptText(t)}</span>
            {t.state === 'PARKED' && t.park_reason && <span>{parkWord(String(t.park_reason))}</span>}
          </>
        )}
      </span>
      {row.taskId !== null && (
        <span className="rn-srow-links">
          <OpenAgent taskId={row.taskId} state={row.task?.state ?? null} go={go} />
          <StepChanges runId={runId} row={row} go={go} />
        </span>
      )}
      {/* The merge step's card, across the row's whole width under it. */}
      {row.merge !== null && (
        <div className="rn-srow-merge" style={{ gridColumn: '1 / -1' }}>
          <MergeStepCard card={row.merge} now={now} />
        </div>
      )}
    </li>
  )
}

/**
 * THE STEPS CARD (item 1): one row per workflow step, under the status line,
 * while the run has a workflow. A row carries the step's name, its state mark,
 * elapsed, attempt n of N and "Open agent →" in every state; a review or fix
 * the run gates on a verdict, with no task yet, reads "waiting on <step>".
 */
export function StepsCard({ run, read, go, now }: {
  run: IssueRun; read: RunWorkflows; go: (to: string) => void; now: number
}) {
  if (read.ids.length === 0) return null
  const rows = stepRows(run, read)
  const failed = (read.loads ?? []).filter((l) => l.error !== null)
  let note: ReactNode = null
  if (read.loads === null) {
    note = <p className="sb-note">Reading the workflow&rsquo;s steps…</p>
  } else if (failed.length > 0) {
    note = (
      <ul className="rn-steps-err">
        {failed.map((l) => (
          <li key={l.wf} className="sb-note">
            {l.round === 0 ? 'The workflow' : `Fix round ${l.round}'s workflow`} <span className="mono">{l.wf}</span>{' '}
            could not be read{l.data === null ? '' : ', so its rows are the last read'}: {l.error}
          </li>
        ))}
      </ul>
    )
  }
  return (
    <section className="rn-card rn-steps-card" aria-label="Steps">
      <h3>Steps{rows.length > 0 && <span className="rn-steps-n"> · {rows.length}</span>}</h3>
      {note}
      {rows.length > 0 && (
        <ol className="rn-srows">
          {rows.map((r) => <StepLine key={r.key} row={r} runId={run.id} go={go} now={now} />)}
        </ol>
      )}
    </section>
  )
}

/**
 * A PLAN STEP'S LIVE STATE (item 2): once the run is approved, the plan's
 * steps say what their task is doing and link it, instead of "starts at once".
 */
export function PlanStepLive({ row, read, go }: { row: StepRow | null; read: RunWorkflows; go: (to: string) => void }) {
  if (row !== null) {
    return (
      <span className="rn-step-live">
        <StepMark row={row} />
        {row.taskId !== null && <OpenAgent taskId={row.taskId} state={row.task?.state ?? null} go={go} />}
      </span>
    )
  }
  const why = read.ids.length === 0
    ? 'not started · the workflow is created on approval'
    : read.loads === null ? 'state being read' : 'state not read · the workflow read has no such step'
  return <span className="rn-step-live sb-note">{why}</span>
}

/** One line of Progress: the run's transition, or a step's change. */
export type ProgressEntry =
  | { kind: 'run'; key: string; at: string | null; to: IssueRunState; from: IssueRunState | null; by: string }
  | { kind: 'step'; key: string; at: string; state: TaskState; word: string; label: string }

function ms(at: string | null | undefined): number | null {
  if (typeof at !== 'string') return null
  const t = Date.parse(at)
  return Number.isFinite(t) ? t : null
}

/**
 * THE STEPS' CHANGES UNDER THE RUN'S (item 3), oldest first. A step's changes
 * are what its task records: when its attempt started, when it ended, and a
 * park or a start in flight at its last update. A step that has not moved
 * since it was created adds nothing -- the Steps card already says it waits.
 * A run transition with no time keeps its place after the one before it.
 */
export function progressEntries(run: IssueRun, rows: StepRow[]): ProgressEntry[] {
  const out: { t: number; e: ProgressEntry }[] = []
  let last = -Infinity
  run.history.forEach((h, i) => {
    last = ms(h.at) ?? last
    out.push({ t: last, e: { kind: 'run', key: `run-${i}`, at: h.at, to: h.to, from: h.from, by: h.by } })
  })
  for (const r of rows) {
    const t = r.task
    if (t === null) continue
    const label = `${r.round === 0 ? '' : `fix round ${r.round} · `}${r.stepId}`
    const add = (at: string | null, state: TaskState, word: string) => {
      const when = ms(at)
      if (when === null || at === null) return
      out.push({ t: when, e: { kind: 'step', key: `${r.key}-${word}`, at, state, word, label } })
    }
    add(t.started_at, 'RUNNING', 'started')
    if (TERMINAL_STATES.has(t.state)) add(t.completed_at ?? t.updated_at, t.state, stateWord(t.state))
    else if (t.state === 'PARKED') add(t.updated_at, t.state, 'parked')
    else if (t.state === 'LEASED' || t.state === 'DISPATCHED' || t.state === 'STARTING') add(t.updated_at, t.state, 'starting')
  }
  // Array.prototype.sort is stable: at one instant the run's line stays first.
  return out.sort((a, b) => a.t - b.t).map((x) => x.e)
}

/**
 * WHEN A PROGRESS LINE HAPPENED (QA G2-31, 2026-10-07): every line read "3d
 * ago", so neither the order nor how long anything took could be read. The
 * clock time, local, to the second -- `HH:MM:SS` today, `MM-DD HH:MM:SS`
 * before -- and `+Δ` since the line above that carries a time; the full ISO
 * timestamp and its age are the title. A line with no time is a dash that
 * says so.
 */
export function ProgressTime({ at, prev, now }: { at: string | null; prev: string | null; now: number }) {
  const t = ms(at)
  if (t === null || at === null) {
    return <span className="sb-note" title="The run recorded no time for this change.">—</span>
  }
  const d = new Date(t)
  const pad = (n: number) => String(n).padStart(2, '0')
  const today = new Date(now)
  const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate()
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  const text = sameDay ? clock : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock}`
  const before = ms(prev)
  const gap = before === null ? null : spanText(t - before)
  const title = `${d.toISOString()} (${timeAgo(at, now)})${gap === null ? '' : ` · ${gap} after the line above`}`
  return (
    <time className="sb-note rn-htime" dateTime={at} title={title}>
      {text}{gap === null ? null : <> <span className="rn-hgap">+{gap}</span></>}
    </time>
  )
}

/** The time of the nearest entry before `i` that carries one: what `+Δ` is counted from. */
export function previousAt(entries: readonly ProgressEntry[], i: number): string | null {
  for (let j = i - 1; j >= 0; j--) {
    const at = entries[j]!.at
    if (ms(at) !== null) return at
  }
  return null
}

/** One step change in Progress: its mark, what changed and when. */
export function StepProgress({ e, prev, now }: { e: Extract<ProgressEntry, { kind: 'step' }>; prev: string | null; now: number }) {
  return (
    <li className="rn-hstep">
      <StateMark state={e.state} bare />
      <span><span className="mono">{e.label}</span> {e.word} · step</span>
      <ProgressTime at={e.at} prev={prev} now={now} />
    </li>
  )
}

/**
 * COST SO FAR (item 4): the step tasks' cost, summed by the board's one rule
 * (`workflowSpend`): a step's attempts where they carry a cost -- which is
 * how a RUNNING step reports -- otherwise a finished task's result. With how
 * many steps report, and how many of those figures are the result's. No step
 * reporting is a dash with its reason, never $0.
 */
export function CostFact({ read }: { read: RunWorkflows }) {
  let body: ReactNode
  let absent = true
  if (read.ids.length === 0) {
    body = <Dash why="No workflow yet: cost is recorded by the workflow's step tasks, and the workflow is created on approval." />
  } else if (read.loads === null) {
    body = <Dash why="The workflow is being read for its steps' cost." />
  } else {
    let total = 0
    let covered = 0
    let fromResult = 0
    let steps = 0
    const unread: string[] = []
    for (const l of read.loads) {
      if (l.data === null) {
        unread.push(l.wf)
        continue
      }
      const spend = workflowSpend(l.data.workflow.steps, new Map(l.data.tasks.map((t) => [t.id, t])), read.usage)
      steps += spend.steps
      covered += spend.covered
      fromResult += spend.fromResult
      total += spend.usd ?? 0
    }
    const gap = unread.length === 0 ? '' : ` ${unread.length === 1 ? 'One workflow' : `${unread.length} workflows`} could not be read (${unread.join(', ')}), so its steps are not counted.`
    const attempts = read.usage === null
      ? ' The steps\' attempts have not been read, so only a finished step\'s result can report.'
      : ''
    if (covered === 0) {
      body = <Dash why={`No step has reported a cost yet: a step reports through its attempts while it runs, and through its result once it finishes.${attempts}${gap}`} />
    } else {
      absent = false
      const source = fromResult === 0
        ? 'Summed from the step tasks\' attempts.'
        : fromResult === covered
          ? 'Summed from the finished step tasks\' results.'
          : `Summed from the step tasks' attempts; ${fromResult} of ${covered} from a finished task's result.`
      body = (
        <span title={`${source}${attempts}${gap}`}>
          {usd(total)} · {covered} of {steps} {steps === 1 ? 'step' : 'steps'} reporting
        </span>
      )
    }
  }
  return (
    <li className={absent ? 'ctl-fact is-absent' : 'ctl-fact'}><b>cost so far</b>{body}</li>
  )
}

/** `abc1234`: a sha short enough to read; the whole sha is its title. */
export function shortSha(sha: string): string {
  return sha.length > 12 ? sha.slice(0, 7) : sha
}

/** The failed checks the CI loop named, and the sha it read them at. */
export interface RedReading {
  /** The first 12 characters of the sha the checks were red at. */
  sha: string
  names: string[]
}

/**
 * The failed checks' names from the run's failure excerpt (`issueci.build_excerpt`):
 * its first line is `CI is red at <sha12>: <name>, <name>`. The run serves no
 * per-check list (`pull_request` carries one aggregate reading), so this is
 * the only place the names are served; null when the excerpt does not say.
 */
export function redReading(excerpt: string | null | undefined): RedReading | null {
  if (typeof excerpt !== 'string') return null
  const first = excerpt.split('\n', 1)[0] ?? ''
  const m = /^CI is red at ([0-9a-f]{4,40}): (.+)$/.exec(first.trim())
  if (m === null) return null
  const sections = new Set<string>()
  for (const line of excerpt.split('\n')) {
    const s = /^## (.+) \([^()]*\)$/.exec(line)
    if (s !== null) sections.add(s[1]!)
  }
  // A name may itself hold ", ": the sections, when they name every check, are the truer split.
  const listed = m[2]!.trim()
  const fromSections = [...sections]
  const names = fromSections.length > 0 && fromSections.join(', ') === listed
    ? fromSections
    : listed === 'a required check' ? [] : listed.split(', ')
  return { sha: m[1]!, names }
}
