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
import { failureCause, WORKFLOW_VIEWS, type WorkflowView } from './stepviews'
import { reasonCopy, stepState, TERMINAL_STATES, type Task, type TaskState, type Workflow } from './types'

/** The three buckets every workflow is in exactly one of. */
export type WorkflowBucket = 'running' | 'finished' | 'failed'
/** The segment: a bucket, or all of them. */
export type BucketFilter = WorkflowBucket | 'all'

export const BUCKET_FILTERS: readonly BucketFilter[] = ['running', 'finished', 'failed', 'all']

export const BUCKET_LABEL: Readonly<Record<BucketFilter, string>> = {
  running: 'Running',
  finished: 'Finished',
  failed: 'Failed',
  all: 'All',
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
}

export const EMPTY_QUERY: WorkflowQuery = { wf: null, tab: 'graph', state: 'all', q: '', owner: '', profile: '' }

/** The query a route's `view` carries, with anything unrecognised dropped to its default. */
export function parseWorkflowQuery(view: string | null | undefined): WorkflowQuery {
  const p = new URLSearchParams(view ?? '')
  const wf = p.get('wf')
  const tab = p.get('tab')
  const state = p.get('state')
  return {
    wf: wf === null || wf === '' ? null : wf,
    tab: tab !== null && (WORKFLOW_VIEWS as readonly string[]).includes(tab) ? (tab as WorkflowView) : 'graph',
    state: state !== null && (BUCKET_FILTERS as readonly string[]).includes(state) ? (state as BucketFilter) : 'all',
    q: p.get('q') ?? '',
    owner: p.get('owner') ?? '',
    profile: p.get('profile') ?? '',
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

/** The segment's counts over the rows the other filters leave. */
export function bucketCounts(rows: readonly Workflow[]): Record<BucketFilter, number> {
  const out: Record<BucketFilter, number> = { running: 0, finished: 0, failed: 0, all: rows.length }
  for (const w of rows) out[bucketOf(w)] += 1
  return out
}

/** The owners and profiles the filters offer, from the rows read, sorted. */
export function ownersOf(rows: readonly Workflow[]): string[] {
  return [...new Set(rows.map((w) => w.submitted_by).filter((o): o is string => typeof o === 'string' && o !== ''))].sort()
}

export function profilesOf(rows: readonly Workflow[]): string[] {
  return [...new Set(rows.flatMap((w) => w.steps.map((s) => s.runner_profile)))].sort()
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
    return { kind: 'failed-unread', why: 'the failing step was not in the task read' }
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
