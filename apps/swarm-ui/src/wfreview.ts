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

/**
 * FINDINGS BESIDE THEIR LINES (docs/design/diff-viewer.md §2 variant 3).
 *
 * The gated step's worker keeps, beside `findings` (text), where each finding
 * said it is -- `finding_locations: [{finding, file, line?, side?}]`, where
 * `finding` indexes `findings` -- and the patches the review READ, each with
 * the sha256 its own worker measured as it staged them:
 * `reviewed_patches: [{task_id, filename, sha256}]` (agent_worker/verdict.py
 * `FindingLocation`, `reviewed_patches`). The path was validated there and is
 * displayed here, never opened.
 */
export interface ReviewedPatch {
  readonly taskId: string
  readonly filename: string
  readonly sha256: string
}

export interface ReviewFinding {
  /** `<review task>:<index>`. */
  readonly id: string
  readonly review: string
  readonly summary: string
  readonly severity: Severity | 'none'
  readonly file: string | null
  /** Set together with `side` or not at all: a line in an unknown half of a patch cannot be placed. */
  readonly line: number | null
  readonly side: 'old' | 'new' | null
  /** Empty when the worker recorded no digest: then nothing is pinned. */
  readonly patches: readonly ReviewedPatch[]
}

const SHA256_HEX = /^[0-9a-f]{64}$/

function reviewedPatchesOf(g: Record<string, unknown>): ReviewedPatch[] {
  const raw = Array.isArray(g['reviewed_patches']) ? (g['reviewed_patches'] as unknown[]) : []
  return raw.flatMap((p) =>
    isRecord(p) && typeof p['task_id'] === 'string' && typeof p['filename'] === 'string' && typeof p['sha256'] === 'string' && SHA256_HEX.test(p['sha256'])
      ? [{ taskId: p['task_id'], filename: p['filename'], sha256: p['sha256'] }]
      : [],
  )
}

/**
 * Every finding of every review the gated steps among `tasks` read, once per
 * review (the first gate that read it, as `verdictFor`), in the review's
 * order, each with the location the worker kept for it, if any.
 */
export function reviewFindings(tasks: readonly Task[]): ReviewFinding[] {
  const out: ReviewFinding[] = []
  const seen = new Set<string>()
  for (const t of tasks) {
    const g = gateBlock(t)
    const review = g?.['task_id']
    if (g === null || typeof review !== 'string' || review === '' || seen.has(review)) continue
    seen.add(review)
    const where = new Map<number, Record<string, unknown>>()
    for (const loc of Array.isArray(g['finding_locations']) ? (g['finding_locations'] as unknown[]) : []) {
      if (isRecord(loc) && typeof loc['finding'] === 'number' && !where.has(loc['finding'])) where.set(loc['finding'], loc)
    }
    const patches = reviewedPatchesOf(g)
    const raw = Array.isArray(g['findings']) ? (g['findings'] as unknown[]) : []
    raw.forEach((f, index) => {
      const summary = findingText(f)
      if (summary === null) return
      const loc = where.get(index)
      const file = typeof loc?.['file'] === 'string' && loc['file'] !== '' ? loc['file'] : null
      const n = loc?.['line']
      const sd = loc?.['side']
      const placed = file !== null && typeof n === 'number' && Number.isInteger(n) && n > 0 && (sd === 'old' || sd === 'new')
      out.push({
        id: `${review}:${index}`,
        review,
        summary,
        severity: severityOf(f) ?? 'none',
        file,
        line: placed ? (n as number) : null,
        side: placed ? (sd as 'old' | 'new') : null,
        patches,
      })
    })
  }
  return out
}

/** A patch this page shows, as far as its digest is known. */
export interface ShownPatch {
  /** The caller's key for where it is drawn (a matrix column). */
  readonly key: string
  readonly label: string
  readonly taskId: string
  /** The artifact's name, as the review staged it (`swarm-work.patch`). */
  readonly name: string
  /**
   * The sha256 of the bytes shown: a string once measured; null when it cannot
   * be (a window, masked text, bytes that are not UTF-8); undefined until read.
   */
  readonly digest: string | null | undefined
  /** The paths the patch changes, null while not known. */
  readonly files: ReadonlySet<string> | null
}

export type Placement =
  | { readonly kind: 'pinned'; readonly key: string; readonly label: string; readonly file: string; readonly line: number; readonly side: 'old' | 'new' }
  /** The review read a patch these steps have since replaced: its lines may have drifted. */
  | { readonly kind: 'earlier'; readonly label: string }
  | { readonly kind: 'unplaced'; readonly why: string }
  | { readonly kind: 'checking' }
  | { readonly kind: 'uncompared'; readonly label: string }

/**
 * THE DIGEST CHECK: where finding `f` may be pinned among the `shown` patches.
 * Pinned only on a patch the review read, by task and name, whose bytes as
 * shown hash to the digest the review's worker measured. A finding with no
 * line and side, or no digest to compare, is not placed; a patch whose digest
 * differs is "from an earlier patch"; one not yet read, or not measurable
 * here, is said to be exactly that. Never the nearest line, never a guess.
 */
export function placeFinding(f: ReviewFinding, shown: readonly ShownPatch[]): Placement {
  if (f.file === null) return { kind: 'unplaced', why: 'the review named no file' }
  if (f.line === null || f.side === null) return { kind: 'unplaced', why: 'the review named the file and no line in it' }
  if (f.patches.length === 0) return { kind: 'unplaced', why: 'no digest of the patch the review read was recorded, so its line cannot be checked against this one' }
  const read = (s: ShownPatch) => f.patches.filter((p) => p.taskId === s.taskId && p.filename === s.name)
  const candidates = shown.filter((s) => read(s).length > 0)
  if (candidates.length === 0) return { kind: 'unplaced', why: 'the patch the review read is not one shown here' }
  const same = candidates.filter((s) => typeof s.digest === 'string' && read(s).some((p) => p.sha256 === s.digest))
  if (same.length > 0) {
    const holding = same.filter((s) => s.files === null || s.files.has(f.file!))
    if (holding.length === 1) {
      const s = holding[0]!
      return { kind: 'pinned', key: s.key, label: s.label, file: f.file, line: f.line, side: f.side }
    }
    return holding.length === 0
      ? { kind: 'unplaced', why: 'the patch the review read does not change this file' }
      : { kind: 'unplaced', why: 'the review read several patches that change this file, and the finding does not say which one its line counts in' }
  }
  if (candidates.some((s) => s.digest === undefined)) return { kind: 'checking' }
  const label = candidates.map((s) => s.label).join(', ')
  if (candidates.some((s) => s.digest === null)) return { kind: 'uncompared', label }
  return { kind: 'earlier', label }
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
