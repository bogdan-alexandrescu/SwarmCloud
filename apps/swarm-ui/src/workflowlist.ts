/**
 * THE WORKFLOWS LIST'S ARITHMETIC (Workflows V2, owner's pick 2026-10-01;
 * workflows.html sections A, B and F). Pure, so the filters and the buckets are
 * tested without a screen.
 *
 * THE ADDRESS IS THE STATE. Everything a reader chose on the list -- the
 * Running / Finished / Failed / All segment, the search, the owner and the
 * profile -- rides on the list's query, and one workflow's page carries the
 * same query, so its back link returns to exactly the list it was opened from
 * and a copied link reproduces the view:
 *
 *     /workflows?state=failed&owner=priya
 *     /workflows/<id>?state=failed&owner=priya
 *     /workflows/<id>/timeline?state=failed&owner=priya
 *
 * Inside the router the workflow and its tab are `wf=` and `tab=` (paths.ts).
 * `workflowQueryString` writes one canonical order -- wf, tab, then the filters
 * -- because paths.ts round-trips an address only when the order is its own.
 */
import { failureCause, WORKFLOW_TABS, type WorkflowView } from './stepviews'
import {
  formatDuration,
  reasonCopy,
  stepState,
  TERMINAL_STATES,
  timeAgo,
  type Task,
  type TaskState,
  type Workflow,
} from './types'
import { workflowNeedItems } from './work/listNeeds'
import { type NeedKind } from './work/NeedsYou'

/** The three buckets every workflow is in exactly one of: what polls, and what runs to now. */
export type WorkflowBucket = 'running' | 'finished' | 'failed'

/**
 * THE LIST'S BUCKETS (the picked Work redesign, frame graph-workflows-rest):
 * Running, Parked, Failed and Done, each by the state the read derived
 * (`listBucketOf`). A workflow whose state the read could not derive is in
 * none of them -- only All counts it -- so no bucket's count claims a state
 * nobody read (QA-036).
 */
export type ListBucket = 'running' | 'parked' | 'failed' | 'done'
/** The segment: a bucket, or all of them. */
export type BucketFilter = ListBucket | 'all'

export const BUCKET_FILTERS: readonly BucketFilter[] = ['all', 'running', 'parked', 'failed', 'done']

export const BUCKET_LABEL: Readonly<Record<BucketFilter, string>> = {
  all: 'All',
  running: 'Running',
  parked: 'Parked',
  failed: 'Failed',
  done: 'Done',
}

/**
 * THE LIST'S ORDER (#111). `state` is the default, so it is never written:
 * NEEDS YOU FIRST (the picked Work redesign) -- the workflows with the most
 * steps waiting on the person (`needsOf`) first, then the V2 grouping
 * (workflows.html frame 0, #503: running, then what waits, then what
 * finished, failures after the successes), newest first in each group
 * (`stateGroupOf`). `failed` puts the workflows with the most failed steps
 * first, which is the triage order; `newest` and `oldest` are by submission.
 */
export type WorkflowSort = 'state' | 'newest' | 'oldest' | 'failed'

export const WORKFLOW_SORTS: readonly WorkflowSort[] = ['state', 'newest', 'oldest', 'failed']

/** The sort control's words, and the list foot's description of the order. */
export const SORT_LABEL: Readonly<Record<WorkflowSort, string>> = {
  state: 'needs you first',
  newest: 'newest first',
  oldest: 'oldest first',
  failed: 'most failed steps first',
}

export interface WorkflowQuery {
  /** The open workflow, or null on the list. */
  readonly wf: string | null
  /** The open workflow's tab. `graph` is the default and is never written. */
  readonly tab: WorkflowView
  readonly state: BucketFilter
  /** Free text over the workflow id, its label and its step ids. */
  readonly q: string
  /** `submitted_by`, exactly; '' for anyone. */
  readonly owner: string
  /** A runner profile any step uses; '' for any. */
  readonly profile: string
  /** The list's order; `state` is the default and is never written. */
  readonly sort: WorkflowSort
  /**
   * THE TABLE'S STAGE FILTER (wide-workflows.html A: "a band count opens the
   * Table filtered to that stage and state"): a dependency level, from 0, and
   * the census word the band counted (`dag.ts` `censusWord`). Written only on
   * a workflow's Table, and only together; null on every other address.
   */
  readonly stage: number | null
  readonly stepState: string | null
}

export const EMPTY_QUERY: WorkflowQuery = {
  wf: null,
  tab: 'graph',
  state: 'all',
  q: '',
  owner: '',
  profile: '',
  sort: 'state',
  stage: null,
  stepState: null,
}

/** The query a route's `view` carries, with anything unrecognised dropped to its default. */
export function parseWorkflowQuery(view: string | null | undefined): WorkflowQuery {
  const p = new URLSearchParams(view ?? '')
  const wf = p.get('wf')
  const tab = p.get('tab')
  const state = p.get('state')
  const sort = p.get('sort')
  const stageRaw = p.get('stage')
  const stage = stageRaw !== null && /^\d{1,2}$/.test(stageRaw) ? Number(stageRaw) : null
  const stepState = p.get('stepstate')
  return {
    wf: wf === null || wf === '' ? null : wf,
    tab: tab !== null && (WORKFLOW_TABS as readonly string[]).includes(tab) ? (tab as WorkflowView) : 'graph',
    // `finished` was Done's name before the redesign: an old link still opens Done.
    state: state === 'finished' ? 'done' : state !== null && (BUCKET_FILTERS as readonly string[]).includes(state) ? (state as BucketFilter) : 'all',
    q: p.get('q') ?? '',
    owner: p.get('owner') ?? '',
    profile: p.get('profile') ?? '',
    sort: sort !== null && (WORKFLOW_SORTS as readonly string[]).includes(sort) ? (sort as WorkflowSort) : 'state',
    stage,
    stepState: stage === null || stepState === null || stepState === '' ? null : stepState,
  }
}

/**
 * The canonical query: `wf`, `tab` (only off the default), then the filters
 * (only the ones set). An all-default list is the empty string, which is the
 * bare `/workflows`.
 */
export function workflowQueryString(q: WorkflowQuery): string {
  const p = new URLSearchParams()
  if (q.wf !== null) p.set('wf', q.wf)
  if (q.wf !== null && q.tab !== 'graph') p.set('tab', q.tab)
  if (q.wf !== null && q.tab === 'table' && q.stage !== null && q.stepState !== null) {
    p.set('stage', String(q.stage))
    p.set('stepstate', q.stepState)
  }
  appendFilters(p, q)
  return p.toString()
}

/** The list's own filters only: what the back link and a row's link carry. */
export function filterQueryString(q: WorkflowQuery): string {
  const p = new URLSearchParams()
  appendFilters(p, q)
  return p.toString()
}

function appendFilters(p: URLSearchParams, q: WorkflowQuery): void {
  if (q.state !== 'all') p.set('state', q.state)
  if (q.q !== '') p.set('q', q.q)
  if (q.owner !== '') p.set('owner', q.owner)
  if (q.profile !== '') p.set('profile', q.profile)
  if (q.sort !== 'state') p.set('sort', q.sort)
}

/** The list's path with its filters: the back link's `href`. */
export function listHref(q: WorkflowQuery): string {
  const f = filterQueryString(q)
  return f === '' ? '/workflows' : `/workflows?${f}`
}

/** One workflow's path, carrying the list's filters so its back link can return. */
export function workflowHref(id: string, q: WorkflowQuery, tab: WorkflowView = 'graph'): string {
  const f = filterQueryString(q)
  const base = `/workflows/${encodeURIComponent(id)}${tab === 'graph' ? '' : `/${tab}`}`
  return f === '' ? base : `${base}?${f}`
}

/** The back link's words: `Workflows · Failed · owner: priya`. */
export function backLabel(q: WorkflowQuery): string {
  const parts = ['Workflows']
  if (q.state !== 'all') parts.push(BUCKET_LABEL[q.state])
  if (q.q !== '') parts.push(`“${q.q}”`)
  if (q.owner !== '') parts.push(`owner: ${q.owner}`)
  if (q.profile !== '') parts.push(`profile: ${q.profile}`)
  return parts.join(' · ')
}

/**
 * The workflow's state as far as this read can say: the derived rollup's state
 * when the rollup is complete, otherwise null. Never the stored copy, which
 * nothing advances (types.ts `workflowHeaderState`).
 */
export function derivedStateOf(w: Workflow): TaskState | null {
  const r = w.rollup
  if (w.state_source !== 'derived' || r === undefined || !r.complete) return null
  return r.state as TaskState
}

/**
 * WHICH BUCKET. A workflow whose state could not be derived is RUNNING, not
 * Finished: "it ended" is a claim, and nothing on this read supports it. The
 * row still says `state unread` in its own words, so the bucket never passes it
 * off as a running workflow. Cancelled is Finished: somebody asked for it.
 */
export function bucketOf(w: Workflow): WorkflowBucket {
  const s = derivedStateOf(w)
  if (s === null) return 'running'
  if (s === 'FAILED' || s === 'DEAD_LETTERED') return 'failed'
  if (TERMINAL_STATES.has(s)) return 'finished'
  return 'running'
}

/** Whether the page should keep re-reading: anything in the Running bucket. */
export function anyRunning(workflows: readonly Workflow[]): boolean {
  return workflows.some((w) => bucketOf(w) === 'running')
}

/** Every filter except the segment, so the segment's counts follow the others. */
export function matchesFilters(w: Workflow, label: string | null, q: WorkflowQuery): boolean {
  if (q.owner !== '' && (w.submitted_by ?? '') !== q.owner) return false
  if (q.profile !== '' && !w.steps.some((s) => s.runner_profile === q.profile)) return false
  const needle = q.q.trim().toLowerCase()
  if (needle !== '') {
    const hay = [w.workflow_id, label ?? '', ...w.steps.map((s) => s.step_id)]
    if (!hay.some((h) => h.toLowerCase().includes(needle))) return false
  }
  return true
}

/**
 * The list bucket a workflow is in, by its derived state; null when the read
 * could not derive one (All counts it, no bucket does). Running is every
 * state that has not ended and is not parked: live work, and work waiting its
 * turn (ready, queued), which the tab's title says.
 */
export function listBucketOf(w: Workflow): ListBucket | null {
  const s = derivedStateOf(w)
  if (s === null) return null
  if (s === 'FAILED' || s === 'DEAD_LETTERED') return 'failed'
  if (TERMINAL_STATES.has(s)) return 'done'
  if (s === 'PARKED') return 'parked'
  return 'running'
}

/** The segment's counts over the rows the other filters leave. */
export function bucketCounts(rows: readonly Workflow[]): Record<BucketFilter, number> {
  const out: Record<BucketFilter, number> = { all: rows.length, running: 0, parked: 0, failed: 0, done: 0 }
  for (const w of rows) {
    const b = listBucketOf(w)
    if (b !== null) out[b] += 1
  }
  return out
}

/**
 * WHAT WAITS ON THE PERSON IN ONE WORKFLOW, from the list read alone (the
 * Needs-you column), counted off the very rows and unknown kinds the
 * Needs-you bar takes for it (`workflowNeedItems`, one classifier for both):
 * `failed`, the steps that failed or were dead-lettered; `questions`, every
 * open question -- a question wait's, a finished step's unanswered artifact
 * questions, an await-input step's input -- while the workflow has not
 * finished or a merge is held on them, the same words asked by two steps
 * once (`workflowFinished`, `questionKey`); `held`, the merges held by the
 * owner's questions and the turns in a browser. A count is null -- "not
 * known", never 0 -- for a kind the bar cannot count either: questions and
 * held when a step was served no needs block, every kind when a step's task
 * is not in the read. Null as a whole when nothing is known: the steps were
 * not read. A workflow whose state could not be derived still counts what its
 * steps' read says, as the bar does.
 */
export interface WorkflowNeeds {
  readonly failed: number | null
  readonly questions: number | null
  readonly held: number | null
}

export function needsOf(w: Workflow, taskById: ReadonlyMap<string, Task> | null): WorkflowNeeds | null {
  if (!stepsRead(w, taskById)) return null
  const items = workflowNeedItems(w, taskById, (stepId) => stepId)
  const unknown = items.unknownKinds ?? []
  const count = (...kinds: NeedKind[]): number | null =>
    kinds.some((k) => unknown.includes(k)) ? null : items.filter((i) => kinds.includes(i.kind)).length
  const needs = { failed: count('failed'), questions: count('question'), held: count('merge', 'handoff') }
  return needs.failed === null && needs.questions === null && needs.held === null ? null : needs
}

/**
 * How many things wait on the person in `w`, for "needs you first": what is
 * counted, and null when a kind is not known and nothing is counted -- an
 * unknown is never ranked as 0 (`sortWorkflows`).
 */
export function needsTotal(w: Workflow, taskById: ReadonlyMap<string, Task> | null): number | null {
  const n = needsOf(w, taskById)
  if (n === null) return null
  const known = (n.failed ?? 0) + (n.questions ?? 0) + (n.held ?? 0)
  return known === 0 && (n.failed === null || n.questions === null || n.held === null) ? null : known
}

/**
 * "Needs you first"'s rank: a workflow with something counted first, by how
 * many, then one whose needs are not known (it may have some), then one known
 * to need nothing.
 */
function needsRank(n: number | null): number {
  return n === null ? 0.5 : n === 0 ? 0 : n + 1
}

/** The owners and profiles the filters offer, from the rows read, sorted. */
export function ownersOf(rows: readonly Workflow[]): string[] {
  return [...new Set(rows.map((w) => w.submitted_by).filter((o): o is string => typeof o === 'string' && o !== ''))].sort()
}

export function profilesOf(rows: readonly Workflow[]): string[] {
  return [...new Set(rows.flatMap((w) => w.steps.map((s) => s.runner_profile)))].sort()
}

/**
 * WHETHER THE ROW'S STEPS WERE READ (QA G3-06, 2026-10-07): every step that
 * has a task has it in the read. The board joins through a window of the
 * tenant's newest 200 tasks, so an older workflow's steps fall outside it and
 * its row knows no title, start, cost or end -- 55 of 100 rows did. A step
 * with no task never ran and has nothing to read. Null read: nothing was.
 */
export function stepsRead(w: Workflow, taskById: ReadonlyMap<string, Task> | null): boolean {
  if (taskById === null) return false
  return w.steps.every((s) => !s.task_id || taskById.has(s.task_id))
}

/** How many of the workflow's steps the task read shows FAILED or DEAD_LETTERED. */
export function failedSteps(w: Workflow, taskById: ReadonlyMap<string, Task> | null): number {
  let n = 0
  for (const s of w.steps) {
    const st = stepState(s, taskById)
    if (st.kind === 'state' && (st.state === 'FAILED' || st.state === 'DEAD_LETTERED')) n += 1
  }
  return n
}

/**
 * THE DEFAULT ORDER'S GROUPS (workflows.html frame 0, #503): live work first --
 * the four states that hold capacity -- then parked, ready and queued work,
 * which holds none, then succeeded, then failed and dead-lettered, then
 * cancelled, and last a workflow whose state this read could not derive,
 * which says so in its own row. It led with the failures, 24 of them before
 * the one workflow running.
 */
export function stateGroupOf(w: Workflow): number {
  const s = derivedStateOf(w)
  if (s === null) return 8
  switch (s) {
    case 'LEASED':
    case 'DISPATCHED':
    case 'STARTING':
    case 'RUNNING':
      return 0
    case 'PARKED':
      return 1
    case 'READY':
      return 2
    case 'QUEUED':
    case 'SUBMITTED':
      return 3
    case 'SUCCEEDED':
      return 4
    case 'FAILED':
      return 5
    case 'DEAD_LETTERED':
      return 6
    case 'CANCELLED':
      return 7
  }
}

function submittedMs(w: Workflow): number {
  return Date.parse(w.created_at) || 0
}

/**
 * THE ROWS IN THE CHOSEN ORDER (#111). Every order breaks its ties newest
 * first, so two workflows that tie never swap places between two reads.
 */
export function sortWorkflows(
  rows: readonly Workflow[],
  sort: WorkflowSort,
  taskById: ReadonlyMap<string, Task> | null,
): Workflow[] {
  const newest = (a: Workflow, b: Workflow) => submittedMs(b) - submittedMs(a)
  const out = rows.slice()
  switch (sort) {
    case 'state': {
      const needs = new Map(out.map((w) => [w.workflow_id, needsRank(needsTotal(w, taskById))] as const))
      return out.sort(
        (a, b) =>
          (needs.get(b.workflow_id) ?? 0) - (needs.get(a.workflow_id) ?? 0) || stateGroupOf(a) - stateGroupOf(b) || newest(a, b),
      )
    }
    case 'newest':
      return out.sort(newest)
    case 'oldest':
      return out.sort((a, b) => submittedMs(a) - submittedMs(b))
    case 'failed': {
      const failed = new Map(out.map((w) => [w.workflow_id, failedSteps(w, taskById)] as const))
      return out.sort((a, b) => (failed.get(b.workflow_id) ?? 0) - (failed.get(a.workflow_id) ?? 0) || newest(a, b))
    }
  }
}

/** A workflow's wall-clock run, as the list's Duration column prints it. */
export interface WorkflowDuration {
  /** Null when the read cannot say. */
  readonly ms: number | null
  /** Still running: the span runs to now and keeps growing. */
  readonly live: boolean
  readonly text: string
  readonly title: string
}

/**
 * WALL CLOCK, FROM SUBMISSION TO THE LAST STEP'S END (#111). Submission, not
 * the first step's start, because the wait for capacity is part of how long a
 * workflow took -- and the Started column beside it says when the first step
 * actually began. A workflow still in the Running bucket runs to `now` and
 * says `so far`.
 *
 * AN END IS NEVER GUESSED. A finished workflow is measured to the latest
 * `completed_at` among its step tasks; if a step task was not in the read, or
 * a finished task recorded no end, the column says so rather than measuring to
 * whichever end it happened to find -- that would be a shorter run than the
 * real one, printed as if it were the real one. A step with no task never ran
 * and has no end to wait for.
 */
export function workflowDuration(
  w: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
  now: number,
): WorkflowDuration {
  const from = Date.parse(w.created_at)
  if (!Number.isFinite(from)) {
    return { ms: null, live: false, text: 'not recorded', title: 'no submit time was recorded' }
  }
  if (bucketOf(w) === 'running') {
    const ms = Math.max(0, now - from)
    return { ms, live: true, text: `${formatDuration(ms)} so far`, title: `submitted ${w.created_at}; still running` }
  }
  if (taskById === null) {
    return { ms: null, live: false, text: 'not read', title: 'the step agents were not read, so the end is unknown' }
  }
  let end = -Infinity
  for (const s of w.steps) {
    if (!s.task_id) continue
    const t = taskById.get(s.task_id)
    if (t === undefined) {
      return { ms: null, live: false, text: 'not read', title: `step ${s.step_id}'s agent was not in the read` }
    }
    const done = t.completed_at ? Date.parse(t.completed_at) : NaN
    if (!Number.isFinite(done)) {
      if (!TERMINAL_STATES.has(t.state)) continue
      return { ms: null, live: false, text: 'not recorded', title: `step ${s.step_id} ended without recording when` }
    }
    end = Math.max(end, done)
  }
  if (end === -Infinity) {
    return { ms: null, live: false, text: 'not recorded', title: 'no step recorded an end' }
  }
  const ms = Math.max(0, end - from)
  return {
    ms,
    live: false,
    text: formatDuration(ms),
    title: `submitted ${w.created_at}; last step ended ${new Date(end).toISOString()}`,
  }
}

/** What a row's second line says, when it has one. */
export type RowWhy =
  | { readonly kind: 'failed'; readonly step: string; readonly why: string }
  | { readonly kind: 'failed-unread'; readonly why: string }
  | { readonly kind: 'parked'; readonly step: string; readonly why: string }
  | { readonly kind: 'unread'; readonly why: string }

/**
 * THE ROW'S SECOND LINE: for a failed or dead-lettered workflow, WHICH STEP
 * failed and why, so triage starts from the list (workflows.html A). The cause
 * is the first line of the step task's `last_error` as `failureCause`
 * normalises it; a failed step whose task wrote no error says so rather than
 * printing nothing. A parked workflow says which step is parked and on what;
 * one whose state was not read says how much of it was not.
 */
export function rowWhy(w: Workflow, taskById: ReadonlyMap<string, Task> | null): RowWhy | null {
  const bucket = bucketOf(w)
  const derived = derivedStateOf(w)
  if (bucket === 'failed') {
    for (const s of w.steps) {
      const st = stepState(s, taskById)
      if (st.kind === 'state' && (st.state === 'FAILED' || st.state === 'DEAD_LETTERED')) {
        const cause = failureCause(st.task.last_error)
        const word = st.state === 'DEAD_LETTERED' ? 'dead-lettered' : 'no error recorded'
        return { kind: 'failed', step: s.step_id, why: cause ?? word }
      }
    }
    return { kind: 'failed-unread', why: 'the failing step was not in the agent read' }
  }
  if (derived === null) {
    const r = w.rollup
    if (r !== undefined && !r.complete) {
      return { kind: 'unread', why: `${r.unreadable_steps.length} of ${w.steps.length} steps could not be read` }
    }
    return { kind: 'unread', why: 'this API did not derive the state from the steps' }
  }
  if (bucket === 'running') {
    for (const s of w.steps) {
      const st = stepState(s, taskById)
      if (st.kind === 'state' && st.state === 'PARKED') {
        const reason = st.task.park_reason
        return { kind: 'parked', step: s.step_id, why: reason ? reasonCopy(reason).replace(/\.$/, '') : 'no park reason recorded' }
      }
    }
  }
  return null
}

/**
 * THE ROW'S COMPACT LINE AT PHONE WIDTH (#109): `9/30 · 4✕ · 2h ago`. At 390
 * the list scrolls sideways and the done, failed and age columns sit off the
 * right edge, so each row alone did not say how it ended or when. This line
 * carries the three under the name. The failed count is drawn only when there
 * is one, and only from a COMPLETE census: over a partial read it would be a
 * wrong number (`rollupLine`'s rule), so the line keeps the step count alone.
 * The age is since submission, the one instant every workflow has.
 */
export function phoneSummary(w: Pick<Workflow, 'steps' | 'rollup' | 'created_at'>, now: number): string {
  const total = w.steps.length
  const roll = w.rollup
  const parts: string[] = []
  if (roll && roll.complete) {
    parts.push(`${roll.counts.SUCCEEDED ?? 0}/${total}`)
    const failed = roll.counts.FAILED ?? 0
    if (failed > 0) parts.push(`${failed}✕`)
  } else {
    parts.push(`${total} step${total === 1 ? '' : 's'}`)
  }
  if (Number.isFinite(Date.parse(w.created_at))) parts.push(timeAgo(w.created_at, now))
  return parts.join(' · ')
}
