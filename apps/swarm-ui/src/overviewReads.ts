/*
 * THE OVERVIEW'S TASK READS, AND HOW MUCH OF EACH STATE THEY COVER (live QA
 * OV-1/OV-2/OV-4/OV-7, 2026-10-11).
 *
 * A MODULE THAT IMPORTS ONLY TYPES, like `pageLimits.ts`: the check layer
 * (checks.ts) counts failures over these reads, and it is loaded by
 * `node --test` with no bundler, so nothing here may pull in React or the
 * client. `OverviewRegions.tsx` re-exports it for the screen.
 */
import type { Task, TaskPage, TaskState } from './types'

/**
 * ONE STATE, READ BY STATE (live QA OV-1/OV-2/OV-4, 2026-10-11).
 *
 * The page is the newest-CREATED N tasks: 50 on a phone, 200 elsewhere. On a
 * busy tenant the phone's 50 were 35 SUCCEEDED, 11 PARKED and 4 RUNNING, so
 * the 11 tasks that had FAILED in the last 24h -- created earlier, failed
 * recently -- were not on it, and Recent failures said `0 failed` under a
 * `real zero` badge while the desktop listed all 11. The fifth running task
 * was missing from Running now in the same way. So whenever the page has more
 * behind it, the Overview also reads the states it must not miss
 * (`OVERVIEW_STATE_READS` in Overview.tsx), one `?state=` read each, and
 * these are those reads.
 */
export interface StateRead {
  state: TaskState
  tasks: Task[]
  /** No next page: every task in this state was read. */
  complete: boolean
}

/**
 * The Overview's task read: the list's page (OV-10), and the state reads.
 *
 * `byState` is ABSENT when the page has nothing behind it -- it already holds
 * every task, so no state read is made. A state whose read failed is absent
 * from the array, and every card then falls back to the page and says so.
 */
export interface OverviewTaskPage extends TaskPage {
  byState?: StateRead[]
}

/** The rows in some states, from every read that carries them, and how much of each state that is. */
export interface StateView {
  rows: Task[]
  /** Per state: every task in it was read. */
  whole: (s: TaskState) => boolean
  /**
   * For a state that is not whole, the instant from which its tasks were all
   * read: they are read newest-CREATED first, so a task created before this
   * one may be missing. The latest such instant over the states asked, ISO;
   * null when every state is whole or no row says when.
   */
  since: string | null
}

const createdMs = (t: Task): number => {
  const v = new Date(t.created_at).getTime()
  return Number.isFinite(v) ? v : NaN
}

/** The oldest creation on a set of rows, or null. */
function oldestCreated(all: readonly Task[]): number | null {
  let m: number | null = null
  for (const t of all) {
    const v = createdMs(t)
    if (Number.isFinite(v) && (m === null || v < m)) m = v
  }
  return m
}

/**
 * The tasks in `states`: the page's rows and the state reads' together, one
 * row per task, the more recently written copy winning (the reads land a
 * moment apart, so a task can be on both in two states).
 */
export function stateView(page: OverviewTaskPage, states: readonly TaskState[]): StateView {
  const pageWhole = (page.next_page_token ?? null) === null
  const reads = page.byState ?? []
  const byId = new Map<string, Task>()
  const take = (t: Task) => {
    const had = byId.get(t.id)
    if (had === undefined || t.updated_at > had.updated_at) byId.set(t.id, t)
  }
  for (const t of page.tasks) take(t)
  for (const r of reads) if (states.includes(r.state)) for (const t of r.tasks) if (t.state === r.state) take(t)
  const wanted = new Set<TaskState>(states)
  const readOf = (s: TaskState) => reads.find((r) => r.state === s)
  const whole = (s: TaskState) => pageWhole || readOf(s)?.complete === true
  let since: number | null = null
  for (const s of states) {
    if (whole(s)) continue
    const r = readOf(s)
    // A short state read reaches back to its own oldest row; a failed one
    // leaves the page, which reaches back to the page's.
    const from = r !== undefined ? oldestCreated(r.tasks) : oldestCreated(page.tasks)
    if (from !== null && (since === null || from > since)) since = from
  }
  return {
    rows: [...byId.values()].filter((t) => wanted.has(t.state)),
    whole,
    since: since === null ? null : new Date(since).toISOString(),
  }
}

/** HH:MM UTC of an ISO instant, the way this screen names a time of day. */
export function hhmmUtc(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? `${d.toISOString().slice(11, 16)} UTC` : iso
}

/**
 * HOW FAR BACK A PARTIAL READ REACHES, IN ONE FORM FOR EVERY FIGURE (visual QA
 * VQ-005, 2026-10-26). Needs a look said "FAILED agents created since 06:04
 * UTC read", Recent failures "partial: agents created since 09:06 UTC read"
 * and Finished today "created since 18:46 UTC" on one page, so a reader
 * could not tell three samples from three wordings of one. Every figure off a
 * partial read now says its reach in these words; two figures off the same
 * sample (`stateView` over the same states) print the same instant.
 */
export function readReach(since: string | null): string {
  return since === null ? 'partial: not every agent was read' : `partial: agents created since ${hhmmUtc(since)} read`
}

/** The three states Recent failures lists, and the Overview reads by state for it. */
export const ENDED_BADLY: readonly TaskState[] = ['FAILED', 'DEAD_LETTERED', 'CANCELLED']

/** How much of each state a set of rows is: every task in it, or only those created since an instant. */
export type Coverage = Pick<StateView, 'whole' | 'since'>

/** Every task in every state was read: a page with nothing behind it. */
export const WHOLE: Coverage = { whole: () => true, since: null }
