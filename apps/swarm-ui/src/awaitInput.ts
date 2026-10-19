/**
 * A WORKFLOW STEP WAITING FOR A PERSON: the console's half of `await-input`
 * (docs/design/human-steps.md §4.5, lane HW-UI; part of #1436).
 *
 * Three things live here, so the card, the workflow page, the run's steps and
 * the inbox read one copy of each:
 *
 *   * the two routes the card calls -- `GET` and `POST
 *     /v1/workflows/{workflow_id}/steps/{step_id}/input` -- with the shapes
 *     `swarm_api/awaitinput.py` (`step_view`, `_response`) serves;
 *   * THE BROWSER'S LIMITS on files (§4.3): ten files at most (or the step's
 *     `max_files`), a name the server will take, an extension on the closed
 *     list, and 16 MiB for the whole answer. They are checked BEFORE anything
 *     is sent, so a person learns in a moment what the server would refuse
 *     after a 16 MiB upload. They are a fast refusal, not the safety: the
 *     server repeats every one, sniffs the bytes, and scans text files;
 *   * the words a step's state reads in: "waiting for a member" (any member
 *     of the step's tenant may answer, Q2, so never "you"), and "expires in
 *     3 h 20 m".
 *
 * NOTHING HERE MAKES THE FEATURE LIVE. The `await-input` profile ships
 * unavailable (request C), so no workflow can contain such a step until the
 * owner applies request D; every reader below is reached only through a step
 * whose `runner_profile` is `await-input`.
 */
import { apiHeaders, chosenTenant, classifyFailure, dropRefusedTenant, read, route, write, type ApiError, type ErrorEnvelope, type Result } from './fetch'
import type { AgentQuestion } from './api'
import type { Task } from './types'

/** The catalogue name of the profile (`profiles.py`, request C). */
export const AWAIT_INPUT_PROFILE = 'await-input'

/** The park reason a waiting step carries (request A). */
export const AWAITING_INPUT = 'AWAITING_INPUT'

/** The whole answer, form plus every file (`awaitinput.MAX_ANSWER_BYTES`, Q6). */
export const MAX_ANSWER_BYTES = 16 * 1024 * 1024

/** Files per answer at most, whatever a step declares (`validation.AWAIT_INPUT_MAX_FILES`). */
export const MAX_FILES = 10

/** A `verdict` form's findings: the worker's bounds (`agent_worker/verdict.py`). */
export const MAX_FINDINGS = 50
export const MAX_FINDING_CHARS = 1000

/** A `text` form's text, in bytes (`awaitinput.MAX_TEXT_BYTES`). */
export const MAX_TEXT_BYTES = 64 * 1024

/** One answer to one question of an `answers` form (`AWAIT_ANSWER_MAX_TEXT_CHARS`). */
export const MAX_ANSWER_TEXT_CHARS = 2000

/** A file's name: `NAME_PATTERN`, no directory (§4.3). */
export const FILE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/

/** The closed list of extensions (`awaitinput.TEXT_EXTENSIONS` and `BINARY_TYPES`). */
export const TEXT_EXTENSIONS: readonly string[] = [
  '.md', '.txt', '.json', '.yaml', '.yml', '.csv', '.diff', '.patch', '.py',
  '.ts', '.tsx', '.js', '.go', '.rs', '.java', '.sh', '.html', '.css', '.sql',
  '.toml',
]
export const BINARY_EXTENSIONS: readonly string[] = ['.pdf', '.png', '.jpg', '.jpeg', '.webp']

/**
 * THE WARNING (§4.3), word for word, shown above the picker and again beside
 * Send. A text file is scanned and refused on a credential; a PDF or an image
 * cannot be, so the person is told before the upload, not after it.
 */
export const FILE_WARNING =
  'Files go to the next agent byte for byte. Text files are checked for credentials and refused if one is found. PDFs and images cannot be checked, so never attach a screenshot or a document that shows a key, a token or a password.'

/** The pill's words for a waiting step. */
export const WAITING_WORD = 'waiting for a member'

export type AwaitForm = 'verdict' | 'answers' | 'text'
export type AwaitStatus = 'waiting' | 'not_open' | 'answered' | 'expired' | 'cancelled'

/** A recorded file of an answer: its name under `<stem>.files/`, size and digest. */
export interface AnswerFileRecord {
  name: string
  bytes: number | null
  sha256: string | null
}

/** `_public_record`: who answered, when, how, and the digests. */
export interface AnswerRecord {
  answer_id: string | null
  answered_by: string | null
  answered_at: string | null
  via: string | null
  sha256: string | null
  bytes: number | null
  files: AnswerFileRecord[]
}

/** `GET .../steps/{step}/input` (`awaitinput.step_view`). */
export interface AwaitInputView {
  workflow_id: string
  step_id: string
  task_id: string
  status: AwaitStatus
  /** False when the step's signed spec did not verify: nothing of the declaration is served. */
  spec_verified: boolean
  prompt: string | null
  form: AwaitForm | null
  artifact: string | null
  max_files: number | null
  max_answer_bytes: number
  tenant: string
  may_answer: boolean
  remind: string[]
  opened_at: string | null
  expires_at: string | null
  on_expire: 'fail' | 'default' | null
  /** What the person is shown: each upstream's staged artifact. */
  shows: { step_id: string | null; task_id: string; name: string }[]
  questions: AgentQuestion[] | null
  answer: AnswerRecord | null
}

/** `POST .../steps/{step}/input`'s 200 (`awaitinput._response`). */
export interface AnswerReceipt {
  workflow_id: string
  step_id: string
  task_id: string
  state: string
  artifact: { name: string | null; bytes: number | null; sha256: string | null }
  files: AnswerFileRecord[]
  answered_by: string | null
  answered_at: string | null
  via: string | null
  replayed: boolean
}

export type VerdictPayload = { verdict: 'MERGE' | 'NOT_YET'; findings: string[] }
export type AnswersPayload = { answers: { question_index: number; choice: string | null; text: string | null }[] }
export type TextPayload = { text: string }
export type AwaitPayload = VerdictPayload | AnswersPayload | TextPayload

const INPUT_ROUTE = '/v1/workflows/{workflow_id}/steps/{step_id}/input'

/** What the step shows and asks, its window, and its answer once given. */
export async function loadAwaitInput(workflowId: string, stepId: string): Promise<Result<AwaitInputView>> {
  return read<AwaitInputView>(route(INPUT_ROUTE, { workflow_id: workflowId, step_id: stepId }), () => false)
}

/**
 * The answer's idempotency key, made once per card: a retry after a lost
 * response sends the same key, and the server answers it with the original
 * 200 rather than a 409 (§4.6). `[a-z0-9-]{1,64}`.
 */
export function newAnswerId(): string {
  const c = globalThis.crypto
  if (c !== undefined && typeof c.randomUUID === 'function') return `ui-${c.randomUUID()}`
  const bytes = new Uint8Array(16)
  if (c !== undefined && typeof c.getRandomValues === 'function') c.getRandomValues(bytes)
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  return `ui-${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`
}

/**
 * Send the answer. A form alone is `application/json` through `write`; with
 * files it is `multipart/form-data`, one `answer` part and a `file` part per
 * file (§4.3), which `write` cannot send -- so it is sent here and classified
 * with the same `classifyFailure`, as Submit.tsx does for its own post.
 * The browser sets the multipart boundary and `Content-Length` itself.
 */
export async function postAnswer(
  workflowId: string,
  stepId: string,
  body: { answer_id: string; payload: AwaitPayload },
  files: readonly File[] = [],
): Promise<Result<AnswerReceipt>> {
  const target = route(INPUT_ROUTE, { workflow_id: workflowId, step_id: stepId })
  const message = { ...body, via: 'console' }
  if (files.length === 0) {
    const r = await write(target, 'POST', message)
    return r.status === 'ok' ? { ...r, data: r.data as AnswerReceipt } : (r as Result<AnswerReceipt>)
  }
  const form = new FormData()
  form.append('answer', new Blob([JSON.stringify(message)], { type: 'application/json' }))
  for (const f of files) form.append('file', f, f.name)
  const tenant = chosenTenant()
  let res: Response
  try {
    res = await fetch(target.url, {
      method: 'POST',
      // No credentials option: the Fetch default is same-origin, which is
      // what `write` asks for, so the session cookie rides as on every write.
      headers: apiHeaders({ accept: 'application/json' }),
      body: form,
    })
  } catch (err) {
    return {
      status: 'error',
      error: { kind: 'unreachable', httpStatus: null, code: null, message: err instanceof Error ? err.message : 'The answer did not arrive.', method: 'POST' },
    }
  }
  const isJson = (res.headers.get('content-type') ?? '').includes('application/json')
  let parsed: unknown = null
  if (isJson) {
    try {
      parsed = await res.json()
    } catch {
      parsed = null
    }
  }
  if (res.ok && !isJson) {
    return {
      status: 'error',
      error: {
        kind: 'session_expired',
        httpStatus: res.status,
        code: null,
        message: 'The API answered with a page instead of data, which is how an expired sign-in arrives. Your answer was NOT recorded. Reload to sign in again.',
        method: 'POST',
      },
    }
  }
  if (!res.ok) {
    // `write`'s reading of the envelope: a bare FastAPI error has `detail` and no `message`.
    const v = typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null
    const env: ErrorEnvelope | null =
      v === null ? null : typeof v.message === 'string' ? (v as ErrorEnvelope) : { message: typeof v.detail === 'string' ? v.detail : undefined }
    const ra = Number(res.headers.get('retry-after'))
    const error: ApiError = { ...classifyFailure(res.status, env, Number.isFinite(ra) && ra > 0 ? ra : undefined), method: 'POST' }
    // A stored tenant the caller is no longer in is dropped, as `write` does;
    // the answer is NOT retried under another tenant.
    dropRefusedTenant(res.status, error.code, tenant)
    return { status: 'error', error }
  }
  return { status: 'ok', data: parsed as AnswerReceipt, fetchedAt: Date.now() }
}

/** The part of a name from its last dot, lower case: `.md`, or '' for none. */
export function extensionOf(name: string): string {
  const at = name.lastIndexOf('.')
  return at <= 0 ? '' : name.slice(at).toLowerCase()
}

/** Whether a file is shown as an image or a PDF: offered as a download, never drawn in the console. */
export function isBinaryName(name: string): boolean {
  return BINARY_EXTENSIONS.includes(extensionOf(name))
}

/** A byte count as a person reads it: `512 B`, `40.2 KiB`, `16 MiB`. */
export function bytesWords(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1).replace(/\.0$/, '')} KiB`
  return `${(n / (1024 * 1024)).toFixed(1).replace(/\.0$/, '')} MiB`
}

/**
 * Why these files cannot be sent with this answer, or null when they can.
 * The step's `max_files` (0 to 10) bounds the count; the whole answer --
 * `formBytes` plus every file -- is at most 16 MiB.
 */
export function filesProblem(
  files: readonly { name: string; size: number }[],
  maxFiles: number,
  formBytes = 0,
): string | null {
  const bound = Math.max(0, Math.min(MAX_FILES, maxFiles))
  if (files.length > bound) {
    return bound === 0
      ? 'This step takes no files.'
      : `This step takes at most ${bound} file${bound === 1 ? '' : 's'}; ${files.length} are chosen. Remove ${files.length - bound}.`
  }
  const seen = new Set<string>()
  for (const f of files) {
    if (!FILE_NAME_RE.test(f.name)) {
      return `${f.name} cannot be sent under that name: a name is letters, digits, '.', '_' and '-', starts with a letter or digit, and is at most 128 characters. Rename it and choose it again.`
    }
    // Case-insensitive, as the server compares them (`check_files`).
    if (seen.has(f.name.toLowerCase())) return `Two files are called ${f.name}; each file needs its own name.`
    seen.add(f.name.toLowerCase())
    const ext = extensionOf(f.name)
    if (!TEXT_EXTENSIONS.includes(ext) && !BINARY_EXTENSIONS.includes(ext)) {
      return `${f.name} is not a type this step takes: text (${TEXT_EXTENSIONS.join(' ')}), PDF, PNG, JPEG or WebP. Archives and executables are refused.`
    }
  }
  const total = formBytes + files.reduce((n, f) => n + f.size, 0)
  if (total > MAX_ANSWER_BYTES) {
    return `The answer is ${bytesWords(total)}; at most ${bytesWords(MAX_ANSWER_BYTES)} is taken, the form and every file together.`
  }
  return null
}

/** The serialised form's size, as the server counts the form toward the 16 MiB. */
export function formBytes(payload: AwaitPayload): number {
  return new TextEncoder().encode(JSON.stringify(payload)).length
}

/** "3 h 20 m", "12 m", "under a minute": the time left until `iso`. */
export function leftWords(iso: string, now: number): string | null {
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return null
  const s = Math.floor((t - now) / 1000)
  if (s <= 0) return null
  if (s < 60) return 'under a minute'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h === 0 ? `${m} m` : m === 0 ? `${h} h` : `${h} h ${m} m`
}

/** "expires in 3 h 20 m", or "past its expiry" once the deadline has gone by. */
export function expiresWords(iso: string | null | undefined, now: number): string | null {
  if (iso === null || iso === undefined || iso === '') return null
  const left = leftWords(iso, now)
  if (left !== null) return `expires in ${left}`
  return Number.isFinite(Date.parse(iso)) ? 'past its expiry' : null
}

function record(v: unknown): Record<string, unknown> | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

/** Whether a task is an `await-input` step. */
export function isAwaitStep(task: Pick<Task, 'runner_profile'> | null | undefined): boolean {
  return task !== null && task !== undefined && task.runner_profile === AWAIT_INPUT_PROFILE
}

/** Whether a task is an `await-input` step waiting for its answer right now. */
export function isWaitingForMember(task: Pick<Task, 'state' | 'park_reason'> | null | undefined): boolean {
  return task !== null && task !== undefined && task.state === 'PARKED' && task.park_reason === AWAITING_INPUT
}

/** `metadata.await_input.expires_at` of a waiting step, as the scheduler wrote it at the park (§4.4). */
export function expiresAtOf(task: Pick<Task, 'metadata'> | null | undefined): string | null {
  const held = record(record(task?.metadata ?? null)?.await_input)
  const at = held?.expires_at
  return typeof at === 'string' && at !== '' ? at : null
}

/** The tooltip on a waiting step's pill: whose answer it waits for (§4.5). */
export function waitingTitle(tenant: string | null | undefined): string {
  return tenant
    ? `Waiting for any member of tenant ${tenant} to answer. Holds no capacity; it ends on the first answer or at its declared expiry.`
    : 'Waiting for any member of its tenant to answer. Holds no capacity; it ends on the first answer or at its declared expiry.'
}

// --------------------------------------------------------------------------
// The composer: the `await_input` block on a submitted step (§4.1)
// --------------------------------------------------------------------------

/** The longest wait a step may declare (`AWAIT_INPUT_MAX_SECONDS`, Q4). */
export const MAX_EXPIRES_SECONDS = 86_400
/** The shortest (§4.1). */
export const MIN_EXPIRES_SECONDS = 300
/** The shortest reminder period (§4.1). */
export const MIN_REMIND_SECONDS = 3_600

/** The artifact a form is written as by default (§4.1). */
export const DEFAULT_ARTIFACT: Readonly<Record<AwaitForm, string>> = {
  verdict: 'verdict.json',
  answers: 'answers.json',
  text: 'input.json',
}

/** What the composer holds for one `await-input` step. Hours, as a person types them. */
export interface AwaitDraft {
  form: AwaitForm
  /** '' for the form's default. */
  artifact: string
  maxFiles: number
  expiresHours: string
  onExpire: 'fail' | 'default'
  /** The default verdict, for `verdict` under `on_expire: "default"`. */
  defaultVerdict: 'MERGE' | 'NOT_YET'
  /** The default text, for `text` under `on_expire: "default"`. */
  defaultText: string
  /** '' for no reminders. */
  remindHours: string
  /** Comma- or space-separated addresses; '' for the submitter only. */
  remind: string
}

export const FIRST_AWAIT_DRAFT: AwaitDraft = {
  form: 'verdict',
  artifact: '',
  maxFiles: MAX_FILES,
  expiresHours: '24',
  onExpire: 'fail',
  defaultVerdict: 'MERGE',
  defaultText: '',
  remindHours: '',
  remind: '',
}

/**
 * The `await_input` block a draft sends, or why it would be refused. The
 * server checks every rule again (`validation.py`); this names the field
 * before a round trip.
 */
export function awaitBlockOf(d: AwaitDraft): { ok: true; block: Record<string, unknown> } | { ok: false; message: string } {
  const hours = Number(d.expiresHours)
  if (d.expiresHours.trim() === '' || !Number.isFinite(hours)) return { ok: false, message: 'the wait needs an expiry, in hours' }
  const expires = Math.round(hours * 3600)
  if (expires < MIN_EXPIRES_SECONDS || expires > MAX_EXPIRES_SECONDS) {
    return { ok: false, message: 'the wait expires after 5 minutes at least and 24 hours at most' }
  }
  const artifact = d.artifact.trim()
  if (artifact !== '' && !FILE_NAME_RE.test(artifact)) {
    return { ok: false, message: 'the answer’s file name is letters, digits, ., _ and -, starting with a letter or digit' }
  }
  if (!Number.isInteger(d.maxFiles) || d.maxFiles < 0 || d.maxFiles > MAX_FILES) {
    return { ok: false, message: `an answer carries 0 to ${MAX_FILES} files` }
  }
  let remindAfter: number | null = null
  if (d.remindHours.trim() !== '') {
    const rh = Number(d.remindHours)
    if (!Number.isFinite(rh)) return { ok: false, message: 'the reminder period is a number of hours' }
    remindAfter = Math.round(rh * 3600)
    if (remindAfter < MIN_REMIND_SECONDS || remindAfter > expires) {
      return { ok: false, message: 'a reminder comes every hour at most often, and not later than the expiry' }
    }
  }
  const remind = d.remind.split(/[\s,]+/).map((s) => s.trim().toLowerCase()).filter((s) => s !== '')
  if (remind.length > 10) return { ok: false, message: 'at most 10 people are reminded' }
  if (remind.some((r) => !/^[^@\s]+@[^@\s]+$/.test(r))) return { ok: false, message: 'a reminder goes to an email address' }
  const block: Record<string, unknown> = {
    form: d.form,
    max_files: d.maxFiles,
    expires_after_seconds: expires,
    on_expire: d.onExpire,
  }
  if (artifact !== '') block.artifact = artifact
  if (d.onExpire === 'default') {
    if (d.form === 'verdict') block.default = { verdict: d.defaultVerdict, findings: [] }
    else if (d.form === 'text') {
      if (d.defaultText.trim() === '') return { ok: false, message: 'the default answer needs its text' }
      block.default = { text: d.defaultText }
    } else return { ok: false, message: 'an answers form has no default: its questions are read when the step opens, so it can only fail at expiry' }
  }
  if (remindAfter !== null) block.remind_after_seconds = remindAfter
  if (remind.length > 0) block.remind = remind
  return { ok: true, block }
}
