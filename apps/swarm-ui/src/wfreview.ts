/**
 * THE REVIEW AND MERGE PARTS OF A WORKFLOW PAGE (agent-detail-2.html, pick A):
 * what a step's verdict gate did, the verdict a review step was given, and the
 * merge step's ordered checklist. Pure readers over `result_summary`, so the
 * page, the graph and the tests read one spelling of each.
 *
 * WHERE EACH FACT COMES FROM, and why nothing here is invented.
 *
 *  * A GATED step's worker records the gate it read in its own
 *    `result_summary.verdict_gate` (agent_worker/lifecycle.py
 *    `_evaluate_verdict_gate`): the review task it read, the verdict, the
 *    verdicts that run the agent, `agent_ran`, and the findings as text. That
 *    block is the only record of a review's verdict the API serves today: the
 *    review step writes `verdict.json` as an artifact and nothing else. So the
 *    verdict card on a review step is read off the gated steps that read it,
 *    and a review nobody gated on draws no card rather than a guessed one.
 *  * Findings carry no severity: the worker keeps each one as text
 *    (agent_worker/verdict.py, up to 50 of 1000 characters). A finding that is
 *    an object with a `severity` the console knows is grouped under it; every
 *    other finding is "Not graded", and never a Blocker.
 *  * `result_summary.merge` is the merge step's design (docs/merge-step.md §6)
 *    and is not written by any worker on main yet. The card is drawn from the
 *    block when a step carries one and not at all otherwise; a check whose
 *    state is not one of the words below reads "not read", never passed.
 */
import type { Task } from './types'

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

/** `result_summary.verdict_gate`, or null when the step has none. */
function gateBlock(task: Task): Record<string, unknown> | null {
  const s = task.result_summary
  if (!isRecord(s)) return null
  const g = s['verdict_gate']
  return isRecord(g) ? g : null
}

/**
 * WHETHER THE STEP'S AGENT WAS SKIPPED BY ITS VERDICT GATE: `agent_ran` is
 * exactly `false`. Anything else -- no gate, `true`, a value that is not a
 * boolean -- is not a skip, because "skipped" is a claim about what ran and
 * only the worker's own `false` makes it.
 */
export function skippedByVerdict(task: Task): boolean {
  const g = gateBlock(task)
  return g !== null && g['agent_ran'] === false
}

/** The verdict the gate read, `MERGE` / `NOT_YET`, or null. */
export function gateVerdictOf(task: Task): string | null {
  const v = gateBlock(task)?.['verdict']
  return typeof v === 'string' && v !== '' ? v : null
}

export type Severity = 'blocker' | 'major' | 'minor'

export const SEVERITY_LABEL: Readonly<Record<Severity | 'none', string>> = {
  blocker: 'Blocker',
  major: 'Major',
  minor: 'Minor',
  none: 'Not graded',
}

export interface VerdictGroup {
  readonly key: Severity | 'none'
  readonly label: string
  /** The review agent's own words, as text. */
  readonly items: readonly string[]
}

export interface VerdictRead {
  readonly verdict: string
  /** The gated step whose result recorded it. */
  readonly readBy: string
  readonly file: string | null
  /**
   * Blocker, Major, Minor, then Not graded. The three graded groups are drawn
   * only when at least one finding carries a severity: with none graded, a
   * `0` under Blocker would be a count of something nobody measured.
   */
  readonly groups: readonly VerdictGroup[]
  readonly total: number
  /** Findings the worker dropped past its cap (`findings_dropped`), or null when it did not say. */
  readonly dropped: number | null
}

function findingText(f: unknown): string | null {
  if (typeof f === 'string') return f
  if (!isRecord(f)) return null
  for (const k of ['summary', 'title', 'message', 'text']) {
    const v = f[k]
    if (typeof v === 'string' && v !== '') return v
  }
  return null
}

function severityOf(f: unknown): Severity | null {
  if (!isRecord(f)) return null
  const v = f['severity']
  if (typeof v !== 'string') return null
  const s = v.trim().toLowerCase()
  return s === 'blocker' || s === 'major' || s === 'minor' ? s : null
}

/**
 * The verdict review task `reviewTaskId` was given, from the first of `tasks`
 * whose verdict gate read it. Null when no step recorded one.
 */
export function verdictFor(reviewTaskId: string, tasks: readonly Task[]): VerdictRead | null {
  for (const t of tasks) {
    const g = gateBlock(t)
    if (g === null || g['task_id'] !== reviewTaskId) continue
    const verdict = g['verdict']
    if (typeof verdict !== 'string' || verdict === '') continue
    const raw = Array.isArray(g['findings']) ? (g['findings'] as unknown[]) : []
    const by: Record<Severity | 'none', string[]> = { blocker: [], major: [], minor: [], none: [] }
    let total = 0
    for (const f of raw) {
      const text = findingText(f)
      if (text === null) continue
      total += 1
      by[severityOf(f) ?? 'none'].push(text)
    }
    const graded = by.blocker.length + by.major.length + by.minor.length > 0
    const keys: (Severity | 'none')[] = graded ? ['blocker', 'major', 'minor', 'none'] : ['none']
    const dropped = g['findings_dropped']
    return {
      verdict,
      readBy: t.step_id ?? t.id,
      file: typeof g['file'] === 'string' ? g['file'] : null,
      groups: keys.map((k) => ({ key: k, label: SEVERITY_LABEL[k], items: by[k] })),
      total,
      dropped: typeof dropped === 'number' && Number.isFinite(dropped) ? dropped : null,
    }
  }
  return null
}

/** Whether some other step's verdict gate names `taskId`: the step is a review. */
export function isReviewedBy(taskId: string, tasks: readonly Task[]): boolean {
  return tasks.some((t) => {
    const d = (t as { dispatch?: unknown }).dispatch
    const fromDispatch = isRecord(d) && isRecord(d['verdict_gate']) && d['verdict_gate']['task_id'] === taskId
    return fromDispatch || gateBlock(t)?.['task_id'] === taskId
  })
}

/** One check of the merge step, in its design's row order. */
export type CheckState = 'passed' | 'failed' | 'waiting' | 'not_read'

export interface MergeCheck {
  readonly name: string
  readonly state: CheckState
  /** The refusal code(s) this check stands for, as the worker wrote them. */
  readonly code: string | null
  readonly message: string | null
}

export interface MergeRead {
  /** The task's end cause (`MERGE_REFUSED`, `MERGE_FAILED`), else the task state as read. */
  readonly outcome: string
  readonly refusal: { readonly code: string; readonly message: string | null } | null
  readonly headline: string | null
  /** The ready label as the step read it, or that it was not read. Null when there is no human gate. */
  readonly label: string | null
  readonly mergedByThisTask: boolean | null
  readonly checks: readonly MergeCheck[]
}

const CHECK_WORDS: Readonly<Record<string, CheckState>> = {
  passed: 'passed',
  ok: 'passed',
  pass: 'passed',
  failed: 'failed',
  refused: 'failed',
  no: 'failed',
  fail: 'failed',
  waiting: 'waiting',
  wait: 'waiting',
  pending: 'waiting',
  not_read: 'not_read',
  na: 'not_read',
}

/** The merge step's card, from its `result_summary.merge`, or null when it has none. */
export function mergeOf(task: Task): MergeRead | null {
  const s = task.result_summary
  if (!isRecord(s) || !isRecord(s['merge'])) return null
  const m = s['merge']
  const r = m['refusal']
  const refusal =
    typeof r === 'string' && r !== ''
      ? { code: r, message: null }
      : isRecord(r) && typeof r['code'] === 'string'
        ? { code: r['code'], message: typeof r['message'] === 'string' ? r['message'] : null }
        : null
  const merged = typeof m['merged_by_this_task'] === 'boolean' ? m['merged_by_this_task'] : null
  const human = m['awaiting_human'] === true
  const ready = m['ready_label']
  const checks: MergeCheck[] = (Array.isArray(m['checks']) ? (m['checks'] as unknown[]) : []).flatMap((c) => {
    if (!isRecord(c)) return []
    const name = [c['name'], c['check'], c['label']].find((v): v is string => typeof v === 'string' && v !== '')
    if (name === undefined) return []
    const word = typeof c['state'] === 'string' ? c['state'] : typeof c['status'] === 'string' ? c['status'] : ''
    return [
      {
        name,
        state: CHECK_WORDS[word.toLowerCase()] ?? 'not_read',
        code: typeof c['code'] === 'string' ? c['code'] : null,
        message: typeof c['message'] === 'string' ? c['message'] : null,
      },
    ]
  })
  // The task's own end cause when the API sent one (MERGE_REFUSED or
  // MERGE_FAILED, docs/merge-step.md §6); a refusal on a failed task with no
  // cause read is a refusal, which is what the code in the block says it is.
  const cause = typeof task.end_cause === 'string' && task.end_cause !== '' ? task.end_cause : null
  const outcome = cause ?? (refusal !== null && task.state === 'FAILED' ? 'MERGE_REFUSED' : task.state)
  let headline: string | null = null
  if (refusal !== null && task.state === 'FAILED') headline = 'Not merged. Nothing changed on the forge.'
  else if (task.state === 'SUCCEEDED' && human) {
    headline = ready === true ? 'Ready. GitHub merges it when its required checks are green.' : 'Every check passed. Awaiting a person: add ready.'
  } else if (task.state === 'SUCCEEDED' && merged === true) headline = 'Merged by this step.'
  return {
    outcome,
    refusal,
    headline,
    label: human ? (ready === true ? 'ready · added' : ready === false ? 'ready · not added' : 'ready label not read') : null,
    mergedByThisTask: merged,
    checks,
  }
}
