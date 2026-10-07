/**
 * WHAT A VERDICT GATE DECIDED, AND FROM WHAT: pure readers for the inspector's
 * Decision card (DecisionCard.tsx; owner request 2026-10-07, on a fix step the
 * review's MERGE kept from running: "I have no idea what happened and why that
 * decision was made and based on what").
 *
 * WHERE EACH FACT COMES FROM. Nothing here is new data: the console already
 * receives all of it on the task row (`swarm_api.codec.task_to_api`).
 *
 *  * The RULE is the step's own dispatch, `dispatch.verdict_gate` --
 *    `{task_id, verdict_in}` -- which the API lifts out of
 *    `metadata.dispatch` (`codec.dispatch_of`). It is there before the step
 *    runs, so a queued or running step can already say what it waits on.
 *  * The DECISION is `result_summary.verdict_gate`, written by the worker
 *    once it has read the staged verdict file
 *    (`agent_worker/lifecycle.py::_evaluate_verdict_gate`): the review task,
 *    the file, the verdict, `verdict_in`, `agent_ran` and the findings. The
 *    worker records no instant for the read; it happens after staging and
 *    before the agent would start, so "read as this step started" is the most
 *    this screen can truthfully say.
 *  * A FINDING is kept by the worker as TEXT (`verdict._finding_text`): an
 *    object with a `summary`/`title`/`message` becomes that string, and any
 *    other object -- the `{file, problem, fix, severity}` shape the review
 *    briefs ask for -- becomes its JSON, keys sorted. So a string that parses
 *    as a JSON object is read back as the object it was. A finding the worker
 *    cut at its 1000-character bound no longer parses and is shown as the
 *    text it is.
 *  * An UNREADABLE verdict leaves no gate block: the worker refuses the step
 *    (`InputUnavailable`, "the agent was not started and nothing was
 *    published") and the reason is the task's `last_error`.
 */
import { TERMINAL_STATES, type GitSummary, type Task } from './types'

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v : null)

function summaryOf(task: Task): Record<string, unknown> | null {
  return isRecord(task.result_summary) ? task.result_summary : null
}

/** `dispatch.verdict_gate` as the API lifted it, or null when the step has none. */
function dispatchGate(task: Task): { taskId: string | null; verdictIn: string[] } | null {
  const d: unknown = task.dispatch
  if (!isRecord(d) || !isRecord(d['verdict_gate'])) return null
  const g = d['verdict_gate']
  return { taskId: text(g['task_id']), verdictIn: stringList(g['verdict_in']) }
}

function stringList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : []
}

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

export type GradedSeverity = 'blocker' | 'major' | 'minor'

export interface Finding {
  /** Lower-cased as recorded, or null when the finding carries none. */
  readonly severity: string | null
  readonly file: string | null
  /** What is wrong, in the review's words: its `problem`, else its summary, else the whole text. */
  readonly problem: string
  readonly fix: string | null
}

const PROBLEM_KEYS = ['problem', 'summary', 'title', 'message', 'what', 'text']
const FIX_KEYS = ['fix', 'suggested_fix', 'suggestion']
const FILE_KEYS = ['file', 'path', 'where']

function first(o: Record<string, unknown>, keys: readonly string[]): string | null {
  for (const k of keys) {
    const v = text(o[k])
    if (v !== null) return v
  }
  return null
}

/** One finding as the worker kept it, read back; null for an entry with nothing to say. */
export function parseFinding(raw: unknown): Finding | null {
  let o: Record<string, unknown> | null = isRecord(raw) ? raw : null
  if (o === null && typeof raw === 'string') {
    const s = raw.trim()
    if (s.startsWith('{')) {
      try {
        const parsed: unknown = JSON.parse(s)
        if (isRecord(parsed)) o = parsed
      } catch {
        // Cut at the worker's bound, or never JSON: it is the text it is.
      }
    }
    if (o === null) return s === '' ? null : { severity: null, file: null, problem: s, fix: null }
  }
  if (o === null) return null
  const problem = first(o, PROBLEM_KEYS)
  const severity = text(o['severity'])
  const file = first(o, FILE_KEYS)
  const fix = first(o, FIX_KEYS)
  if (problem === null && fix === null && file === null) return null
  return {
    severity: severity === null ? null : severity.trim().toLowerCase(),
    file,
    problem: problem ?? JSON.stringify(o),
    fix,
  }
}

export interface FindingGroup {
  /** The severity as recorded, or `none`. */
  readonly key: string
  readonly label: string
  readonly items: readonly Finding[]
}

const GRADED: readonly GradedSeverity[] = ['blocker', 'major', 'minor']

function capital(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/**
 * Blocker, Major, Minor, then any other severity as the review recorded it,
 * then Not graded. Empty groups are not drawn; the count line carries the
 * zeros for the three graded ones.
 */
export function groupFindings(findings: readonly Finding[]): FindingGroup[] {
  const order: string[] = [...GRADED]
  for (const f of findings) if (f.severity !== null && !order.includes(f.severity)) order.push(f.severity)
  order.push('none')
  return order
    .map((key) => ({
      key,
      label: key === 'none' ? 'Not graded' : capital(key),
      items: findings.filter((f) => (f.severity ?? 'none') === key),
    }))
    .filter((g) => g.items.length > 0)
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

/**
 * `0 blockers, 0 majors, 4 minors` -- the three graded counts always (each is
 * a count over findings that WERE recorded, so a zero is measured), then any
 * other recorded severity and the ungraded, only when there are some.
 */
export function countLine(findings: readonly Finding[]): string {
  const groups = groupFindings(findings)
  const n = (k: string) => groups.find((g) => g.key === k)?.items.length ?? 0
  const parts = GRADED.map((k) => plural(n(k), k))
  for (const g of groups) {
    if ((GRADED as readonly string[]).includes(g.key) || g.key === 'none') continue
    parts.push(`${g.items.length} ${g.key}`)
  }
  if (n('none') > 0) parts.push(`${n('none')} not graded`)
  return parts.join(', ')
}

// ---------------------------------------------------------------------------
// The gate
// ---------------------------------------------------------------------------

/**
 * What the step's verdict reads as:
 *
 *   read        the worker recorded it (`verdict`)
 *   unreadable  the step failed refusing its verdict file or gate (`error`)
 *   pending     the step has not finished, so nothing is recorded yet
 *   unrecorded  the step finished and its result carries no verdict
 */
export type VerdictState =
  | { kind: 'read'; verdict: string }
  | { kind: 'unreadable'; error: string }
  | { kind: 'pending' }
  | { kind: 'unrecorded' }

export interface GateDecision {
  readonly reviewTaskId: string | null
  /** The verdicts that start this step's agent. Empty when neither record names them. */
  readonly verdictIn: readonly string[]
  readonly verdict: VerdictState
  /** The staged file the verdict was read from, or null when not recorded. */
  readonly file: string | null
  /** `agent_ran` exactly as the worker wrote it; null when it wrote none. */
  readonly agentRan: boolean | null
  /** Null when the worker recorded no `findings` key: not the same as none. */
  readonly findings: readonly Finding[] | null
  /** Past the worker's cap (`findings_dropped`), or null when not said. */
  readonly dropped: number | null
}

/** The worker's refusals of a verdict (agent_worker/verdict.py, lifecycle.py). */
const VERDICT_REFUSAL = /verdict (file|gate)/

/** The step's verdict gate and what it decided, or null for a step with no gate. */
export function gateDecisionOf(task: Task): GateDecision | null {
  const s = summaryOf(task)
  const recorded = s !== null && isRecord(s['verdict_gate']) ? s['verdict_gate'] : null
  const asked = dispatchGate(task)
  if (recorded === null && asked === null) return null
  const verdictWord = recorded === null ? null : text(recorded['verdict'])
  const error = text(task.last_error)
  let verdict: VerdictState
  if (verdictWord !== null) verdict = { kind: 'read', verdict: verdictWord }
  else if (!TERMINAL_STATES.has(task.state)) verdict = { kind: 'pending' }
  else if (task.state === 'FAILED' && error !== null && VERDICT_REFUSAL.test(error)) verdict = { kind: 'unreadable', error }
  else verdict = { kind: 'unrecorded' }
  const raw = recorded === null ? undefined : recorded['findings']
  const findings = Array.isArray(raw)
    ? raw.map(parseFinding).filter((f): f is Finding => f !== null)
    : null
  const dropped = recorded?.['findings_dropped']
  const ran = recorded?.['agent_ran']
  const fromRecord = recorded === null ? [] : stringList(recorded['verdict_in'])
  return {
    reviewTaskId: (recorded === null ? null : text(recorded['task_id'])) ?? asked?.taskId ?? null,
    verdictIn: fromRecord.length > 0 ? fromRecord : (asked?.verdictIn ?? []),
    verdict,
    file: recorded === null ? null : text(recorded['file']),
    agentRan: typeof ran === 'boolean' ? ran : null,
    findings,
    dropped: typeof dropped === 'number' && Number.isFinite(dropped) && dropped > 0 ? dropped : null,
  }
}

/** Whether `task` is gated on review task `reviewId`, by its dispatch or by its recorded gate. */
export function gatesOn(task: Task, reviewId: string): boolean {
  return gateDecisionOf(task)?.reviewTaskId === reviewId
}

/** Whether a finished task's manifest lists a verdict file: the mark of a review step. */
export function writesVerdict(task: Task): boolean {
  const s = summaryOf(task)
  const a = s?.['artifacts']
  return Array.isArray(a) && a.some((e) => isRecord(e) && typeof e['name'] === 'string' && /(^|\/)verdict\.json$/.test(e['name']))
}

// ---------------------------------------------------------------------------
// What the step did
// ---------------------------------------------------------------------------

/** `result_summary.skipped_agent`, the worker's one line for a shut gate. */
export function skippedAgentOf(task: Task): string | null {
  return text(summaryOf(task)?.['skipped_agent'])
}

/** The runner's own status and sentence, `result_summary.runner`. */
export function runnerReportOf(task: Task): { status: string | null; summary: string | null } {
  const r = summaryOf(task)?.['runner']
  if (!isRecord(r)) return { status: null, summary: null }
  return { status: text(r['status']), summary: text(r['summary']) }
}

/** Where the PR title and body came from: `implementer`, `label`, or null for none. */
export type PrTextSource = 'implementer' | 'label' | null

/** `result_summary.pull_request_text_from`, or null when the step did not record it. */
export function prTextFromOf(task: Task): { title: PrTextSource; body: PrTextSource } | null {
  const p = summaryOf(task)?.['pull_request_text_from']
  if (!isRecord(p)) return null
  const one = (v: unknown): PrTextSource => (v === 'implementer' || v === 'label' ? v : null)
  return { title: one(p['title']), body: one(p['body']) }
}

/** The branch the step pushed, by name and head: `result_summary.branch`, else the git summary's. */
export function branchOf(task: Task): { name: string | null; sha: string | null } | null {
  const s = summaryOf(task)
  const b = s?.['branch']
  if (isRecord(b)) {
    const name = text(b['name'])
    const sha = text(b['head_sha']) ?? text(b['head'])
    if (name !== null || sha !== null) return { name, sha }
  }
  const git = isRecord(s?.['git']) ? (s?.['git'] as GitSummary) : null
  const name = text(git?.branch)
  const sha = text(git?.pushed_head)
  return name === null && sha === null ? null : { name, sha }
}

/** The step's pull request, when its git summary carries a well-formed one. */
export function pullRequestOf(task: Task): { number: number; url: string; state: string } | null {
  const git = summaryOf(task)?.['git']
  if (!isRecord(git) || !isRecord(git['pull_request'])) return null
  const { number, url, state } = git['pull_request']
  if (typeof number !== 'number' || typeof url !== 'string' || !/^https?:\/\//.test(url)) return null
  return { number, url, state: typeof state === 'string' ? state : 'open' }
}

/** `git.publish_reason`, the worker's sentence for what the publish did. */
export function publishReasonOf(task: Task): string | null {
  const git = summaryOf(task)?.['git']
  return isRecord(git) ? text(git['publish_reason']) : null
}

/** `dispatch.builds_on`: the upstream TASK whose branch this step starts from. */
export function buildsOnOf(task: Task): string | null {
  const d: unknown = task.dispatch
  return isRecord(d) ? text(d['builds_on']) : null
}

/**
 * A commit's page on the forge, when the repository is a plain `https` URL
 * of the `host/owner/repo` shape. Null otherwise -- a masked or unusual URL
 * is not guessed at, and the sha is drawn as text.
 */
export function commitHref(repositoryUrl: string | null | undefined, sha: string): string | null {
  if (typeof repositoryUrl !== 'string' || !/^[0-9a-f]{7,64}$/i.test(sha)) return null
  const m = /^https:\/\/([^/@\s]+)\/([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/.exec(repositoryUrl)
  return m === null ? null : `https://${m[1]}/${m[2]}/${m[3]}/commit/${sha}`
}

/** The inspector's address for a task, and the viewer's for one of its artifacts. */
export function inspectorHref(taskId: string): string {
  return `#work/task/${encodeURIComponent(taskId)}`
}

export function artifactHref(taskId: string, name: string): string {
  return `#work/task/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(name)}`
}
