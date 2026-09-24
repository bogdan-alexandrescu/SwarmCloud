import { useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { loadCapacity } from './api'
import { DispatchChoice, DispatchFacts, type DispatchDraft } from './Dispatch'
import { isPaused, type ApiError } from './fetch'
import { HelpCard } from './HelpCard'
import { FailedPanel, Screen } from './Shell'
import {
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  headroomFor,
  requiredInputKeys,
  type Capacity,
  type Pool,
  type RunnerProfile,
  type Task,
} from './types'

/**
 * Submit one task -- and the honest constraints on doing so.
 *
 * WHAT THIS SCREEN USED TO BE, AND WHY IT IS NOT THAT ANY MORE. It was a
 * `<select>`, a `<textarea class="mono">` whose label read `input (JSON
 * object)` and whose initial value was the literal `{}`, and a `JSON.parse` on
 * submit. The owner's verdict was exact: *"no one expect the user to paste
 * json objects in a form field. It should be either done 100% via the UI or
 * not have this option to start a task or workflow from the UI at all."*
 *
 * IT IS 100% UI, AND REMOVAL WAS THE WRONG HALF OF THE OFFER. This form and
 * its workflow twin are the ONLY way to start work from the console; the
 * alternatives are the MCP plugin and the API. Removing them would have made a
 * read-only console out of the one surface an operator already has open when
 * they decide something needs running. So the question became what the input
 * actually has to carry, and the answer is small enough to be fields:
 *
 *   * `apps/swarm-mcp/swarm_mcp/client.py:482` and `workflows.py:143` -- the
 *     platform's own first-party client -- send `{"prompt": prompt}` and
 *     NOTHING ELSE. That is the real-world input shape, in its entirety.
 *   * Every other key any shipped runner reads is a scalar, a list of scalars,
 *     or (browser only) a list of small fixed-shape action objects. There is no
 *     open-ended nesting anywhere. See `SUGGESTED` below for the census, with
 *     the source line beside each entry.
 *
 * So `input` is edited as typed fields: a name, a kind, and a value editor per
 * kind. No brace, quote or comma is ever typed. `buildInput` is what assembles
 * the object, and it is the only place a JSON value is constructed.
 *
 * INVARIANT 10 IS STILL THE SHAPE OF THIS SCREEN: a caller picks a
 * `runner_profile` BY NAME and the frozen catalogue supplies the image,
 * command, resource class and backend. The picker is now a LIST rather than a
 * `<select>` precisely to make that visible -- the catalogue is the offer, and
 * a name that is not in it cannot be typed. `input` is data the platform never
 * reads; it is not an execution parameter, and nothing here lets one become
 * one. schemas.py sets `extra="forbid"` and main.py answers any
 * FORBIDDEN_CALLER_FIELD with a message naming it.
 *
 * IT DOES NOT START AN AGENT. `_build_task` stores the task at READY, or
 * PARKED when its provider key is missing or it waits on a dependency, and
 * invariant 1 says both cost nothing. What decides whether it moves is the
 * capacity beside the send button -- this tenant's, not the platform's.
 *
 * fetch.ts is a READ contract: `read()` takes no method or body and
 * `classify()` is private to it, so this first write in the UI does its own
 * POST. api.ts is its real home, not least for the USE_FIXTURES branch.
 */

/**
 * The TaskCreate keys this form sends (schemas.py, `TaskCreate`).
 *
 * This list is what a 422 is attributed AGAINST, so it has to hold every key
 * in the body -- including the three the dispatch control owns, whose refusals
 * would otherwise all land as "the API refused and did not say which field".
 */
const FIELDS = ['runner_profile', 'input', 'strategy', 'carrier', 'repository_url'] as const
type Field = (typeof FIELDS)[number]

type Outcome =
  | { kind: 'idle' | 'sending' }
  | { kind: 'created'; task: Task; woke: boolean }
  /** A 422, on the field that caused it. Never a page-level error. */
  | { kind: 'refused'; fields: Partial<Record<Field, string>>; unattributed: string | null }
  | { kind: 'failed'; error: ApiError }

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function fail(kind: ApiError['kind'], httpStatus: number | null, message: string): Outcome {
  return { kind: 'failed', error: { kind, httpStatus, code: null, message } }
}

async function postTask(body: Record<string, unknown>): Promise<Outcome> {
  let res: Response
  try {
    res = await fetch('/v1/tasks', {
      method: 'POST', body: JSON.stringify(body),
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      credentials: 'same-origin', // behind IAP the browser already holds the cookie
    })
  } catch (err) {
    return fail('unreachable', null, err instanceof Error ? err.message : 'The request did not complete.')
  }
  // Checked before the body and on a 2xx too, for the reason fetch.ts gives: an
  // expired IAP session arrives as a 200 carrying sign-in HTML, and parsing it
  // would report a submission that never happened.
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    return fail('session_expired', res.status,
      'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.')
  }
  let payload: unknown = null
  try { payload = await res.json() } catch { payload = null } // unreadable body, same diagnosis
  const env = isRecord(payload) ? payload : {}
  const message = typeof env.message === 'string' ? env.message : `The API returned HTTP ${res.status}.`

  if (res.status === 201) {
    const task = isRecord(env.task) ? (env.task as unknown as Task) : null
    // A 201 with no document means something WAS created that this screen
    // cannot name -- worse than a failure, and it must not read as one.
    if (!task) return fail('server_error', res.status, `${message} Check Agents before resubmitting.`)
    // `=== true`, not `?? false`: only an explicit true means it was woken.
    return { kind: 'created', task, woke: env.scheduler_woken === true }
  }
  if (res.status === 422) return { kind: 'refused', ...attribute(env, message) }

  // Restated from classify() in fetch.ts, which is private and GET-only. Two
  // screens disagreeing about what a 403 means would be worse than this.
  const lower = message.toLowerCase()
  const byStatus: Record<number, ApiError['kind']> = { 401: 'unauthenticated', 409: 'conflict', 429: 'rate_limited', 503: 'upstream_degraded' }
  const kind: ApiError['kind'] = res.status !== 403 ? byStatus[res.status] ?? 'server_error'
    : lower.includes('is disabled') ? 'tenant_disabled'
    : lower.includes('is not permitted') ? 'wrong_domain' : 'admin_required'
  const ra = Number(res.headers.get('retry-after'))
  const code = typeof env.code === 'string' ? env.code : null
  return { kind: 'failed', error: { kind, httpStatus: res.status, code, message,
    detail: env.detail, retryAfterSeconds: Number.isFinite(ra) && ra > 0 ? ra : undefined } }
}

/**
 * Which FIELD a 422 is about. Two writers produce one and they differ:
 * FastAPI's RequestValidationError, which main.py wraps into
 * `detail.errors[].loc` naming the field, and the service's own
 * ValidationFailed, raised before that validator runs with NO errors list --
 * it names its field by the shape of `detail` instead. What is left over is
 * reported as unattributed rather than pinned to a guess: a message under the
 * wrong input sends someone editing a value that was never the problem.
 */
function attribute(env: Record<string, unknown>, message: string):
{ fields: Partial<Record<Field, string>>; unattributed: string | null } {
  const detail = isRecord(env.detail) ? env.detail : {}
  const fields: Partial<Record<Field, string>> = {}
  for (const e of Array.isArray(detail.errors) ? detail.errors : []) {
    if (!isRecord(e)) continue
    const loc = Array.isArray(e.loc) ? e.loc.map(String) : []
    const field = FIELDS.find((f) => loc.includes(f))
    if (field) fields[field] = typeof e.msg === 'string' ? e.msg : message
  }
  if (Object.keys(fields).length === 0) {
    if (Array.isArray(detail.known_runner_profiles)) fields.runner_profile = message
    // `DispatchOptionError` (code `invalid_dispatch`) is raised before FastAPI's
    // validator and carries no errors list either. Its three detail shapes name
    // their own field: the accepted-values lists come from `_accepted_value`,
    // and `missing` comes from the needs-a-repository refusal. Matched on the
    // detail key rather than on the prose, which is the part free to change.
    else if (Array.isArray(detail.accepted_carriers)) fields.carrier = message
    else if (detail.missing === 'repository_url') fields.repository_url = message
    else if (Array.isArray(detail.accepted_strategies)) fields.strategy = message
    else if (typeof detail.max_bytes === 'number') fields.input = message
  }
  return { fields, unattributed: Object.keys(fields).length === 0 ? message : null }
}

/* ==========================================================================
   THE INPUT, AS FIELDS
   ==========================================================================

   Exported, and imported by `SubmitWorkflow.tsx`. The recorded defect this
   guards against is in that file's own history: "two idioms for one field is
   how the two halves drifted far enough apart for one of them to lose it
   entirely" -- the workflow form once sent no `input` at all, and every
   workflow it created failed a step at a time. One editor, one builder, one
   set of refusals.

   THE FOUR KINDS ARE THE CENSUS, NOT A GUESS. Every `input.*` read by every
   runner in `apps/agent-worker/agent_worker/runners/` on 2026-09-23 is a
   string, a number, a boolean, a list of strings, or `browser.actions` -- a
   list of objects drawn from eight fixed shapes, which `BrowserActions` below
   builds. Nothing else nests. */

export type ValueKind = 'text' | 'number' | 'flag' | 'list' | 'actions'

/** One browser action. Eight `type`s, each reading one or two scalars --
 *  `runners/browser.py:112-157`. Fields not used by the chosen type are kept
 *  in the draft and not sent, so flipping type and back loses nothing. */
export interface BrowserAction {
  key: number
  type: string
  url: string
  selector: string
  text: string
  name: string
  seconds: string
}

/** One key of `input`, mid-edit. `key` is React's identity and is never sent. */
export interface InputField {
  key: number
  name: string
  kind: ValueKind
  /** `text` and `number` both live here: a number is raw text until it is
   *  built, so a half-typed `1.` survives the keystroke that made it invalid. */
  text: string
  flag: boolean
  list: string[]
  actions: BrowserAction[]
  /** Named by the API's `required_keys`. Cannot be renamed, removed, or
   *  retyped: `required_input_keys` defines a required key as one that must be
   *  present AND a non-empty string, so `text` is the only correct editor. */
  required: boolean
  /** This key was typed by the caller rather than offered. ONLY these get a
   *  name box and a kind picker: a suggested key already knows both, and
   *  drawing an editable name on it is an invitation to typo `cpu_burn_secnds`
   *  into a key the runner silently ignores -- and three more bordered boxes
   *  per field for the privilege. */
  own: boolean
}

let nextKey = 1
function field(over: Partial<InputField> = {}): InputField {
  return { key: nextKey++, name: '', kind: 'text', text: '', flag: false, list: [''], actions: [], required: false, own: false, ...over }
}

/** An offer this console knows how to draw. NOT a rule, and not a schema. */
interface Suggestion {
  name: string
  kind: ValueKind
  /** What the runner does with it, in the runner's own terms. */
  note: string
  /** For a key whose value is itself a name from a frozen catalogue. */
  choices?: string[]
}

/**
 * WHAT EACH PROFILE'S RUNNER READS -- offered, never enforced.
 *
 * READ FROM THE RUNNER SOURCE ON 2026-09-23 and keyed by PROFILE NAME, which
 * is the one thing this bundle has: `/v1/capacity` serves a profile's resource
 * class, backend, provider, pools and `input_contract`, and NOT its `command`.
 * `swarm_api/runnerinputs.py` keys its own table by runner MODULE for exactly
 * the reason a name table is worse -- a new profile pointed at an existing
 * runner inherits nothing here and needs a line added.
 *
 * THAT DRIFT IS AFFORDABLE HERE AND IT WOULD NOT BE THERE, because these are
 * suggestions and `required_keys` is a rule. A suggestion that goes stale
 * offers a key the runner ignores; it never refuses valid work and it never
 * hides a key, because "a setting of your own" is always the last option in
 * the picker. The API's `required_keys` remains the only thing that can stop a
 * submission, and an unread `required_keys` still renders as unread.
 *
 * REQUESTED, NOT CHANGED (CLAUDE.md's reporting rule): `/v1/capacity` should
 * serve an `accepted_keys` block beside `required_keys`, built in
 * `runnerinputs.py` off the same module table, so this constant can be deleted.
 */
const SUGGESTED: Record<string, Suggestion[]> = {
  // runners/cliagent.py:264 (prompt, also required) and :277 (model).
  'claude-code': [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
    { name: 'model', kind: 'text', note: 'overrides the image default; letters, digits, . : - _ only' },
  ],
  codex: [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
    { name: 'model', kind: 'text', note: 'overrides the image default; letters, digits, . : - _ only' },
  ],
  // runners/generic.py -- GENERIC_COMMANDS is the frozen argv catalogue, and
  // `command` NAMES an entry in it. That is invariant 10's own pattern, not an
  // exception to it: the caller picks a name, the platform owns the argv.
  generic: [
    { name: 'command', kind: 'text', note: 'names one argv in the platform catalogue', choices: ['pytest', 'npm-ci', 'npm-test', 'npm-build', 'make', 'uv-sync'] },
    { name: 'paths', kind: 'list', note: 'pytest only: existing paths inside the workspace' },
    { name: 'target', kind: 'text', note: 'make only: one target from the repository Makefile' },
    { name: 'working_directory', kind: 'text', note: 'a directory inside the workspace to run in' },
  ],
  // runners/browser.py:76-107.
  browser: [
    { name: 'url', kind: 'text', note: 'opened first, before any action below' },
    { name: 'actions', kind: 'actions', note: 'run in order after the first page loads' },
    { name: 'timeout_ms', kind: 'number', note: 'per-action ceiling' },
    { name: 'viewport_width', kind: 'number', note: 'defaults to 1280' },
    { name: 'viewport_height', kind: 'number', note: 'defaults to 900' },
  ],
  // runners/mock.py module docstring: every key optional, by design.
  mock: [
    { name: 'prompt', kind: 'text', note: 'recorded in the result; the mock runs either way' },
    { name: 'sleep_seconds', kind: 'number', note: 'interruptible sleep, so cancellation has something to cut' },
    { name: 'cpu_burn_seconds', kind: 'number', note: 'genuine load, for sizing and concurrency tests' },
    { name: 'steps', kind: 'number', note: 'progress files written, so a checkpoint holds partial work' },
    { name: 'fail', kind: 'flag', note: 'fail deterministically, to exercise the failure path' },
    { name: 'artifact_text', kind: 'text', note: 'written to the artifact upload path' },
  ],
}

/** Offered for every profile: `runners/limits.py` reads it for any runner, and
 *  a caller may only LOWER it -- the platform ceiling wins and says it clamped. */
const ANY_PROFILE: Suggestion[] = [
  { name: 'timeout_seconds', kind: 'number', note: 'lowers the child wall clock; the platform ceiling still wins' },
]

function suggestionsFor(profile: string): Suggestion[] {
  return [...(SUGGESTED[profile] ?? []), ...ANY_PROFILE]
}

const ACTION_TYPES = ['goto', 'click', 'fill', 'press', 'wait_for', 'wait', 'screenshot', 'extract'] as const

/** The scalars each action type reads. Anything not listed is not sent. */
const ACTION_SHAPE: Record<string, Array<{ prop: keyof BrowserAction; label: string }>> = {
  goto: [{ prop: 'url', label: 'url' }],
  click: [{ prop: 'selector', label: 'selector' }],
  fill: [{ prop: 'selector', label: 'selector' }, { prop: 'text', label: 'text' }],
  press: [{ prop: 'selector', label: 'selector' }, { prop: 'text', label: 'key' }],
  wait_for: [{ prop: 'selector', label: 'selector' }],
  wait: [{ prop: 'seconds', label: 'seconds' }],
  screenshot: [{ prop: 'name', label: 'artifact name' }],
  extract: [{ prop: 'selector', label: 'selector' }, { prop: 'name', label: 'artifact name' }],
}

export type BuiltInput =
  | { ok: true; input: Record<string, unknown> }
  | { ok: false; message: string }

/**
 * The fields, as the object the API stores. The ONLY place a JSON value is
 * constructed in this app.
 *
 * AN EMPTY VALUE IS OMITTED, NEVER COERCED. A number field left blank sends no
 * key -- it does not send `0`, because a figure nobody typed is not zero, and
 * `sleep_seconds: 0` is a different run from `sleep_seconds` absent. The same
 * rule is why a blank text field is dropped rather than sent as `""`: the
 * runner's own test for a required key is "present and non-empty", so `""` is
 * the shape that gets dispatched and then refused after a credential is
 * mounted.
 */
export function buildInput(fields: InputField[]): BuiltInput {
  // `Object.create(null)`, NOT `{}`. The key is typed by the caller, and
  // `plain.__proto__ = 'x'` sets the prototype instead of adding a key: the
  // screen would show the setting, the duplicate check would not see it, and
  // the request body would silently not carry it. On a null-prototype object
  // it is an ordinary own key that serialises like any other -- which is the
  // honest behaviour, since what was typed is what has to be sent.
  const input: Record<string, unknown> = Object.create(null) as Record<string, unknown>
  for (const f of fields) {
    const name = f.name.trim()
    if (name === '') continue
    if (Object.prototype.hasOwnProperty.call(input, name)) {
      return { ok: false, message: `${name} is set twice and an object has one value per key` }
    }
    if (f.kind === 'flag') { input[name] = f.flag; continue }
    if (f.kind === 'number') {
      const raw = f.text.trim()
      if (raw === '') continue
      const n = Number(raw)
      if (!Number.isFinite(n)) return { ok: false, message: `${name} is set to ${raw}, which is not a number` }
      input[name] = n
      continue
    }
    if (f.kind === 'list') {
      const entries = f.list.map((e) => e.trim()).filter((e) => e !== '')
      if (entries.length > 0) input[name] = entries
      continue
    }
    if (f.kind === 'actions') {
      const built: Array<Record<string, unknown>> = []
      for (const a of f.actions) {
        const shape = ACTION_SHAPE[a.type]
        if (!shape) return { ok: false, message: `${name} holds an action of an unknown kind` }
        const one: Record<string, unknown> = { type: a.type }
        for (const part of shape) {
          const raw = String(a[part.prop] ?? '').trim()
          if (raw === '') return { ok: false, message: `a ${a.type} action needs its ${part.label}` }
          // `wait` is the one numeric field inside an action (`:141`).
          one[part.prop === 'seconds' ? 'seconds' : part.prop] = part.prop === 'seconds' ? Number(raw) : raw
        }
        if (a.type === 'wait' && !Number.isFinite(Number(a.seconds))) {
          return { ok: false, message: `a wait action needs a number of seconds` }
        }
        built.push(one)
      }
      if (built.length > 0) input[name] = built
      continue
    }
    const text = f.text
    if (text.trim() === '') continue
    input[name] = text
  }
  return { ok: true, input }
}

/**
 * Required keys with nothing in them, named.
 *
 * The test is the RUNNER's, restated from `run_cli_agent`: present, a string,
 * and not blank. A looser one here would pass `{"prompt": ""}` through to the
 * identical failure -- the screen accepted it, the platform accepted it, and
 * the agent refused it minutes later having already spent a slot and mounted a
 * credential.
 */
export function missingRequired(fields: InputField[], required: string[] | null): string[] {
  if (required === null) return []
  const built = buildInput(fields)
  if (!built.ok) return []
  return required.filter((k) => {
    const v = built.input[k]
    return typeof v !== 'string' || v.trim() === ''
  })
}

/**
 * The fields a profile starts with, carrying over anything already typed.
 *
 * Switching `claude-code` -> `codex` must not delete the prompt: both demand
 * one, it is the same instruction, and retyping it is the cost of a control
 * that forgets. Keys the new profile does not require are kept too -- the
 * platform does not read `input`, so an extra key is inert, and silently
 * dropping what someone typed is worse than carrying it.
 */
export function seedFields(previous: InputField[], required: string[] | null): InputField[] {
  const keys = required ?? []
  const kept = previous.map((f) => {
    const req = keys.includes(f.name.trim())
    // A key that BECOMES required is a string by the contract's own definition,
    // so its editor changes with it. Keeping a number editor on a required key
    // would offer a value the runner refuses.
    return req ? { ...f, required: true, kind: 'text' as ValueKind } : { ...f, required: false }
  })
  const have = new Set(kept.map((f) => f.name.trim()))
  const added = keys.filter((k) => !have.has(k)).map((k) => field({ name: k, kind: 'text', required: true }))
  // Required first: they are the ones that stop a submission.
  return [...added, ...kept].sort((a, b) => Number(b.required) - Number(a.required))
}

/** A value editor, chosen by kind. Never a JSON literal.
 *
 *  `choices` is passed alongside rather than stored on the field: it belongs to
 *  the SUGGESTION, not to the value, and a list of catalogue names is never
 *  part of what gets sent. */
function ValueEditor({ f, id, choices, onChange }: {
  f: InputField; id: string; choices?: string[]; onChange: (next: InputField) => void
}) {
  if (f.kind === 'flag') {
    return (
      <label className="sbf-flag">
        <input type="checkbox" checked={f.flag} onChange={(e) => onChange({ ...f, flag: e.target.checked })} />
        <span>{f.name.trim() === '' ? (f.flag ? 'yes' : 'no') : `${f.name} is ${f.flag ? 'yes' : 'no'}`}</span>
      </label>
    )
  }
  if (f.kind === 'number') {
    return (
      <input id={id} className="mono sbf-num" inputMode="decimal" value={f.text} placeholder="a number"
        onChange={(e) => onChange({ ...f, text: e.target.value })} />
    )
  }
  if (f.kind === 'list') {
    return (
      <div className="sbf-list">
        {f.list.map((entry, i) => (
          <input key={i} className="mono" value={entry} id={i === 0 ? id : undefined}
            placeholder="one entry"
            onChange={(e) => onChange({ ...f, list: f.list.map((o, j) => (j === i ? e.target.value : o)) })} />
        ))}
        <div className="sbf-listact">
          <button type="button" className="sbf-mini" onClick={() => onChange({ ...f, list: [...f.list, ''] })}>one more</button>
          {f.list.length > 1 && (
            <button type="button" className="sbf-mini" onClick={() => onChange({ ...f, list: f.list.slice(0, -1) })}>one fewer</button>
          )}
        </div>
      </div>
    )
  }
  if (f.kind === 'actions') return <BrowserActions f={f} onChange={onChange} />
  if (choices) {
    return (
      <select id={id} className="mono" value={f.text} onChange={(e) => onChange({ ...f, text: e.target.value })}>
        <option value="">choose one…</option>
        {choices.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    )
  }
  // A prompt is the one value that is genuinely long, so it is the one that
  // gets height. Everything else is a line, because a five-row box for a model
  // name is what made the old form look like a code editor.
  const name = f.name.trim()
  const tall = name === 'prompt' || name.endsWith('_text')
  return tall
    ? <textarea id={id} rows={4} value={f.text}
        // ONLY `prompt` GETS THE PROMPT'S PLACEHOLDER. `artifact_text` is also
        // a long string and also gets the height, and it was being offered
        // "what this agent should do" -- a placeholder describing a different
        // key, which is worse than none at all.
        placeholder={name === 'prompt' ? 'what this agent should do' : undefined}
        onChange={(e) => onChange({ ...f, text: e.target.value })} />
    : <input id={id} className="mono" value={f.text} spellCheck={false}
        onChange={(e) => onChange({ ...f, text: e.target.value })} />
}

/** The browser runner's action list, built rather than typed. */
function BrowserActions({ f, onChange }: { f: InputField; onChange: (next: InputField) => void }) {
  const set = (i: number, next: BrowserAction) => onChange({ ...f, actions: f.actions.map((o, j) => (j === i ? next : o)) })
  return (
    <div className="sbf-acts">
      {f.actions.map((a, i) => (
        <div className="sbf-act" key={a.key}>
          <span className="sbf-act-n">{i + 1}</span>
          <select className="mono" aria-label={`action ${i + 1} kind`} value={a.type}
            onChange={(e) => set(i, { ...a, type: e.target.value })}>
            {ACTION_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          {(ACTION_SHAPE[a.type] ?? []).map((part) => (
            <input key={String(part.prop)} className="mono" placeholder={part.label}
              aria-label={`action ${i + 1} ${part.label}`}
              value={String(a[part.prop] ?? '')} spellCheck={false}
              onChange={(e) => set(i, { ...a, [part.prop]: e.target.value })} />
          ))}
          <button type="button" className="sbf-mini" aria-label={`remove action ${i + 1}`}
            onClick={() => onChange({ ...f, actions: f.actions.filter((_, j) => j !== i) })}>remove</button>
        </div>
      ))}
      <button type="button" className="sbf-mini" onClick={() => onChange({ ...f, actions: [...f.actions,
        { key: nextKey++, type: 'goto', url: '', selector: '', text: '', name: '', seconds: '1' }] })}>
        add an action
      </button>
    </div>
  )
}

/**
 * THE INPUT EDITOR. Shared by both submit screens.
 *
 * THE ABSENCE HERE IS `required === null`, and it is the whole reason this
 * component takes a nullable rather than a list. `required_keys` unread is NOT
 * "this profile requires nothing": the first is a rule nobody could read, the
 * second is a measured empty. They get different renderings, the unread one
 * carries `.ctl-mark.is-unread` and an accessible sentence, and neither blocks
 * the form -- inventing a rule would refuse valid work.
 */
export function InputFields({ profile, fields, required, onChange, idPrefix }: {
  profile: string
  fields: InputField[]
  required: string[] | null
  onChange: (next: InputField[]) => void
  idPrefix: string
}) {
  const offers = suggestionsFor(profile)
  const used = new Set(fields.map((f) => f.name.trim()))
  const unused = offers.filter((o) => !used.has(o.name))
  const noteFor = (name: string) => offers.find((o) => o.name === name)?.note ?? null
  const choicesFor = (name: string) => offers.find((o) => o.name === name)?.choices
  const set = (i: number, next: InputField) => onChange(fields.map((o, j) => (j === i ? next : o)))

  const add = (s: Suggestion) => onChange([...fields, field({
    name: s.name, kind: s.kind,
    actions: s.kind === 'actions' ? [{ key: nextKey++, type: 'goto', url: '', selector: '', text: '', name: '', seconds: '1' }] : [],
  })])

  return (
    <div className="sbf-input">
      {required === null && (
        // The unread rule, drawn as unread. Not a warning: nothing is wrong,
        // this API simply did not say, and the form still sends.
        //
        // THE MARK IS THE CLAIM AND THE WORDS ARE ON THE LABEL (§13.5). The
        // surface carries three words; the sentence that says WHY -- that an
        // unread rule is not a runner which requires nothing -- is the
        // accessible name and the `?` topic. It is also what lets this fit on
        // one line inside a 370px workflow step, where the longer phrasing
        // wrapped and left the `?` glyph alone on a third line.
        <p className="ctl-panel-note"
          aria-label="This API did not say which keys this runner refuses to start without, so nothing is checked here. That is not the same as a runner that requires nothing.">
          <i className="ctl-mark is-unread">not read</i>
          required keys
          {/* NO `?` (B7.4). This paragraph's own accessible name is longer than
              `input-is-opaque`'s card and says the part that matters here --
              that nothing is being checked, and that this is not the same as a
              runner which requires nothing. A glyph beside it opened a shorter
              version of the sentence it was standing in. */}
        </p>
      )}
      {fields.length === 0 && (
        <p className="sbf-none">
          {required !== null && required.length === 0
            ? `${profile} starts without any input. Add a setting below if this run needs one.`
            : 'No settings yet.'}
        </p>
      )}
      {fields.map((f, i) => {
        const id = `${idPrefix}-f${f.key}`
        const note = noteFor(f.name.trim())
        return (
          <div className={f.required ? 'sbf-field is-required' : 'sbf-field'} key={f.key}>
            <div className="sbf-key">
              {f.own ? (
                <label className="sbf-name">
                  <input className="mono sbf-rename" value={f.name} spellCheck={false} placeholder="setting name"
                    aria-label="setting name"
                    onChange={(e) => set(i, { ...f, name: e.target.value })} />
                </label>
              ) : (
                // `htmlFor` only where the editor's direct child IS the one
                // labelable control. `flag` and `actions` wrap their controls
                // in their own labels, and a second label pointing at the same
                // checkbox concatenates into an accessible name nobody wrote.
                <label className="sbf-name" htmlFor={f.kind === 'flag' || f.kind === 'actions' ? undefined : id}>
                  <span className="mono">{f.name}</span>
                  {f.required && <i className="sbf-req">required</i>}
                </label>
              )}
              {f.own && (
                <select className="sbf-kind" aria-label={`${f.name || 'this setting'} value kind`} value={f.kind}
                  onChange={(e) => set(i, { ...f, kind: e.target.value as ValueKind })}>
                  <option value="text">text</option>
                  <option value="number">number</option>
                  <option value="flag">yes / no</option>
                  <option value="list">list of text</option>
                </select>
              )}
              {!f.required && (
                <button type="button" className="sbf-mini" onClick={() => onChange(fields.filter((_, j) => j !== i))}>
                  remove
                </button>
              )}
            </div>
            <ValueEditor f={f} id={id} choices={choicesFor(f.name.trim())} onChange={(next) => set(i, next)} />
            {note && <p className="sbf-note">{note}</p>}
          </div>
        )
      })}
      <div className="sbf-add">
        {unused.map((s) => (
          <button type="button" key={s.name} className="sbf-offer" onClick={() => add(s)}>
            <span className="mono">{s.name}</span>
            <span className="sbf-offer-note">{s.note}</span>
          </button>
        ))}
        <button type="button" className="sbf-offer is-own" onClick={() => onChange([...fields, field({ own: true })])}>
          <span>a setting of your own</span>
          <span className="sbf-offer-note">any key, any of the four kinds</span>
        </button>
      </div>
    </div>
  )
}

/* ========================================================================== */

export function SubmitScreen() {
  return (
    <Screen
      title="Submit a task"
      load={loadCapacity}
      summary={(c) => `${Object.keys(c.runner_profiles).length} runner profiles · ${c.pools.length} pools you are admitted against`}
      empty={{ heading: 'No pools came back',
        body: 'The read of the pools this tenant is admitted against succeeded and returned none. The runner profiles arrive in that same response, so there is no name to choose and this screen cannot submit.' }}
    >
      {(c) => <Form capacity={c} />}
    </Screen>
  )
}

/** One numbered move of the flow. A row draws nothing (§13.3); the ordinal is
 *  a `--surface-2` disc, the same line-to-step substitution §13.3 made for the
 *  help glyph. */
export function Move({ n, title, aside, children }: { n: number; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="sbf-move">
      <h2 className="sbf-move-h"><i className="sbf-move-n" aria-hidden="true">{n}</i>{title}{aside}</h2>
      {children}
    </section>
  )
}

function Form({ capacity }: { capacity: Capacity }) {
  const [chosen, setChosen] = useState('')
  const [fields, setFields] = useState<InputField[]>([])
  // Seeded with the API's own defaults, so a caller who touches nothing sends
  // the dispatch they would have got before this control existed.
  const [dispatch, setDispatch] = useState<DispatchDraft>({
    strategy: DEFAULT_STRATEGY,
    carrier: DEFAULT_CARRIER,
    repositoryUrl: '',
  })
  const [outcome, setOutcome] = useState<Outcome>({ kind: 'idle' })
  const catalogue = useMemo(
    () => Object.entries(capacity.runner_profiles).sort((a, b) => a[0].localeCompare(b[0])),
    [capacity],
  )
  const profile: RunnerProfile | null = capacity.runner_profiles[chosen] ?? null
  const required = requiredInputKeys(profile ?? undefined)
  const bad = outcome.kind === 'refused' ? outcome.fields : {}

  const pick = (name: string) => {
    setChosen(name)
    setFields((prev) => seedFields(prev, requiredInputKeys(capacity.runner_profiles[name] ?? undefined)))
  }

  const built = buildInput(fields)
  const missing = missingRequired(fields, required)
  const blocked = chosen === '' || !built.ok || missing.length > 0

  async function submit(e?: FormEvent) {
    e?.preventDefault()
    if (!built.ok) {
      // Labelled "not sent": a refusal phrased like the API's sends someone
      // looking at the platform for a typo that is in this form.
      setOutcome({ kind: 'refused', fields: { input: `Not sent — ${built.message}.` }, unattributed: null })
      return
    }
    if (missing.length > 0) {
      setOutcome({ kind: 'refused', unattributed: null, fields: { input:
        `Not sent — ${chosen} requires ${missing.join(', ')} as a non-empty string.` } })
      return
    }
    setOutcome({ kind: 'sending' })
    const repo = dispatch.repositoryUrl.trim()
    setOutcome(
      await postTask({
        runner_profile: chosen,
        input: built.input,
        // Sent ALWAYS, including when they are the defaults. `TaskCreate` has
        // defaults of its own, so omitting them would produce the same task --
        // but then the request body would not say what the screen said, and the
        // 201 echo below would be the first place the two could be compared.
        strategy: dispatch.strategy,
        carrier: dispatch.carrier,
        // Omitted when blank: `repository_url` is `str | None` with a scheme
        // validator, and `""` fails it with a message about URL schemes rather
        // than about the field being empty.
        ...(repo === '' ? {} : { repository_url: repo }),
      }),
    )
  }

  return (
    <form className="sbf" onSubmit={submit}>
      <div className="sbf-build">
        {outcome.kind === 'created' && <Created task={outcome.task} woke={outcome.woke} />}
        {outcome.kind === 'failed' && (
          <>
            <FailedPanel error={outcome.error} onRetry={() => void submit()} />
            {/* A write is not a read: a failure after the request left the browser
                does not prove nothing was created. */}
            {/* NO `?` (B7.4). `ambiguous-write` is "a failure after the request
                left the browser does not prove nothing was created", and the
                two sentences here ARE that, with the remedy first. */}
            <p className="warn-text">
              <strong>Check Agents before submitting again.</strong> This failed on
              a write, so the task may exist anyway.
            </p>
          </>
        )}

        {/* THIS SCREEN'S ONE `?` (B7.4). It was seven: two of them repeated a
            sentence printed beside the glyph, one repeated `all at once` from
            the line it sat on, one repeated a bold `That is not zero.`, and one
            was a second copy of the topic on step 2. What stays is invariant
            10, because it is the rule about what this form may NOT accept --
            no image, no command, no resource spec, no backend parameter -- and
            a form cannot state that by labelling the fields it does have. */}
        <Move n={1} title="Choose a runner" aside={<HelpCard topic="runner-profile-by-name" />}>
          {/* THE CATALOGUE, VISIBLE. Invariant 10 says a caller names one of
              these and supplies no image, command, resource spec or backend --
              and a `<select>` hid the very thing that makes that safe. A row
              per profile shows what each name actually selects. */}
          {/* RADIOS, NOT BUTTONS. This is a pick-one, the screen's own
              neighbour (`.dsp-option`) already expresses a pick-one with a
              radio, and a native radio carries the affordance, the keyboard
              behaviour and the accessible role without a box — `accent-color`
              is already set for the whole app. A row of five buttons has none
              of that and, at rest, does not look like a choice at all. */}
          <ul className="sbf-runners" role="none">
            {catalogue.map(([n, p]) => {
              // `available` is `?? true` and not `|| true`: an older API omits
              // the field, and `false || true` is true, which would offer a
              // profile we know is refused.
              const off = (p.available ?? true) === false
              return (
                <li key={n}>
                  <label className={`sbf-runner${chosen === n ? ' is-on' : ''}${off ? ' is-off' : ''}`}>
                    <input type="radio" name="runner-profile" value={n} checked={chosen === n}
                      disabled={off} onChange={() => pick(n)} />
                    <span className="sbf-runner-name mono">{n}</span>
                    <span className="sbf-runner-facts">
                      {p.resource_class} · {p.units} unit{p.units === 1 ? '' : 's'} · {p.backend}
                      {p.provider ? <> · needs a {p.provider} key</> : <> · needs no provider key</>}
                    </span>
                    {/* A DISABLED PROFILE IS KEPT AND EXPLAINED, not hidden.
                        The catalogue says a disabled profile is known and
                        refused, and "unknown runner_profile" would send
                        somebody hunting a typo that is not there. */}
                    {off && <span className="sbf-runner-off">{p.disabled_reason || 'refused by the platform'}</span>}
                  </label>
                </li>
              )
            })}
          </ul>
          {bad.runner_profile && <p className="warn-text" role="alert">{bad.runner_profile}</p>}
          {profile && <ProfileFacts name={chosen} profile={profile} pools={capacity.pools} />}
        </Move>

        {/* NO `aside` (B7.4): this was the second copy of `input-is-opaque` on
            one screen, four hundred lines from the first. */}
        <Move n={2} title="Say what it should do">
          {chosen === '' ? (
            <p className="sbf-none">Choose a runner first — what it reads is what this asks for.</p>
          ) : (
            <InputFields profile={chosen} fields={fields} required={required} onChange={setFields} idPrefix="task" />
          )}
          {bad.input && <p className="warn-text" role="alert">{bad.input}</p>}
        </Move>

        {/* `steps={1}`: a standalone task IS one step, and that is the number the
            pull-request counts on the options are computed from. `scale="task"`
            is the same word `resolve_dispatch_options` takes, and it is what
            makes `integrate` show as unavailable here rather than 422 later. */}
        <Move n={3} title="Choose what happens to the result">
          <DispatchChoice
            draft={dispatch}
            onChange={setDispatch}
            steps={1}
            scale="task"
            errors={{ strategy: bad.strategy, carrier: bad.carrier, repository_url: bad.repository_url }}
          />
        </Move>
      </div>

      {/* ONE PLACE TO ACT, and it carries what the click will do. The old form
          put the button at the bottom of a scroll and the capacity facts 600px
          above it, so the two things a submitter needs at the same instant --
          is there room, and what am I about to send -- were never on screen
          together. */}
      <aside className="sbf-side">
        <div className="sbf-send">
          <h2>Ready to send</h2>
          <ul className="ctl-facts">
            <li className={chosen === '' ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>runner</b>
              {chosen === '' ? <i className="ctl-em">&mdash;</i> : <code>{chosen}</code>}
            </li>
            <li className={chosen === '' ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>input</b>
              {/* THREE DIFFERENT NOTHINGS, AND THEY DO NOT SHARE A RENDERING.
                  No runner chosen yet is an em dash: nothing has been decided.
                  A chosen runner with no settings is a MEASURED empty object,
                  which is a real thing to send and says so in words. A value
                  that will not build is neither -- it is the reason the send
                  is blocked. */}
              {chosen === '' ? <i className="ctl-em">&mdash;</i>
                : built.ok
                  ? (Object.keys(built.input).length === 0
                    ? <i className="sbf-empty">empty — the runner gets no settings</i>
                    : <span className="mono">{Object.keys(built.input).join(', ')}</span>)
                  : <i className="sbf-bad">{built.message}</i>}
            </li>
            <li className="ctl-fact">
              <b>result</b>
              {dispatch.strategy}
            </li>
          </ul>
          {missing.length > 0 && (
            <p className="warn-text" role="alert">
              {chosen} requires <code>{missing.map((k) => `input.${k}`).join(', ')}</code> as a non-empty string.
            </p>
          )}
          <button type="submit" className="sbf-go" disabled={outcome.kind === 'sending' || blocked}>
            {outcome.kind === 'sending' ? 'Submitting…' : 'Submit one task'}
          </button>
          {outcome.kind === 'refused' && outcome.unattributed && (
            <p className="warn-text" role="alert">The API refused this and did not say which field: {outcome.unattributed}</p>
          )}
        </div>
      </aside>
    </form>
  )
}

/** What the chosen name actually selects, and whether it can be admitted now.
 *
 *  A FILL, AND THE NAME AT THE TOP OF IT. These facts are about ONE row of the
 *  list above; loose paragraphs under a five-row list read as being about the
 *  list. The `--surface-2` block is the same line-to-step substitution §13.3
 *  applies everywhere else, and the name is what binds it to the choice.
 *
 *  Exported for `submit.room.test.tsx`, which renders it with hand-built
 *  arguments rather than through the whole form. */
export function ProfileFacts({ name, profile, pools }: { name: string; profile: RunnerProfile; pools: Pool[] }) {
  const byName = new Map<string, Pool>(pools.map((p) => [p.name, p]))
  // Read off `profile.admission`: the server computed it from
  // `evaluate_capacity`, so this box and the Capacity board cannot disagree.
  const room = headroomFor(profile)
  const binding = room.binding ? byName.get(room.binding) ?? null : null
  // headroomFor's contract: a profile's pool list is the CALLING TENANT'S, so
  // this answers "how many more could I submit", never "what the platform has"
  // -- and its doc requires the tenant beside the number. /v1/capacity carries
  // no tenant field; the tenant is the `tenant:<id>` pool it put in this list.
  const tenantPool = profile.pools.find((p) => p.startsWith('tenant:'))
  const tenant = tenantPool ? tenantPool.slice('tenant:'.length) : null

  return (
    <div className="sbf-room">
      <p className="sbf-room-h"><span className="mono">{name}</span> right now</p>
      <p className="muted">
        Costs {profile.units} weighted unit{profile.units === 1 ? '' : 's'} in each of its{' '}
        {profile.pools.length} pools, all at once.
        {/* NO `?` (B7.4). "all at once" is the last three words of the sentence
            the glyph was attached to, and the sentence already names the count
            and the weight. */}
      </p>
      <p className="muted">
        {room.agents === null ? (
          /* Never a 0. Either nothing caps this, or a required pool could not
             be read -- and a submitter told "room for 0" when the truth is
             "we do not know" stops submitting for the wrong reason. */
          room.basis === 'uncapped' ? (
            <>No pool in this profile&apos;s list is configured, so nothing caps it right now.</>
          ) : (
            <>
              {/* NEVER A ZERO HERE. The count of unread pools is the figure and
                  the clause that it is not zero stays beside it. */}
              Room could not be measured: {room.unread.length} of its pools could
              not be read. <strong>That is not zero.</strong>
              {/* NO `?` (B7.4), AND THIS IS THE ONE DELETION THAT NEEDED THE
                  MOST CARE, because `room-unknown-not-zero` is the rule the
                  whole console is built around. It stays on the surface in
                  bold, in four words, with the count of unread pools beside
                  it -- which is stronger than a glyph, not weaker: a reader
                  cannot fail to open it. The rule is only allowed to move
                  behind a `?` where the surface cannot carry it, and here the
                  surface carries it. */}
            </>
          )
        ) : (
          <>
            Room for <strong>{room.agents}</strong> more task{room.agents === 1 ? '' : 's'} of this
            profile right now, for {tenant ?? 'a tenant this response does not name'}
            {binding && (isPaused(binding)
              ? <> — <code>{binding.name}</code> is paused and admits nothing at all</>
              : <> — held down by <code>{binding.name}</code>, {binding.active} of {binding.effective_limit} weighted units in use</>)}.
          </>
        )}
      </p>
      {/* EVERY pool refusing it, not the tightest. Two ceilings can bind at
          the same moment, and a submitter shown one of them raises it, tries
          again and gets refused by the other. */}
      {room.blockers.length > 0 && (
        <ul className="blocker-list compact">
          {room.blockers.map((b) => (
            <li key={b.pool} className="blocker-row">
              <span className="tags">
                <span className={`tag ${b.reason === 'MANUAL_PAUSE' ? 'paused' : 'full'}`}>
                  {b.reason === 'MANUAL_PAUSE' ? 'paused' : 'full'}
                </span>
              </span>
              <code>{b.pool}</code> — {b.active} of {b.limit} units in use
            </li>
          ))}
        </ul>
      )}
      {/* Unconfigured means uncapped: skipped, not counted as a zero reading "full". */}
      {room.missing.length > 0 && (
        <p className="muted small">Not in the response, so not counted: {room.missing.join(', ')}.</p>
      )}
    </div>
  )
}

function Created({ task, woke }: { task: Task; woke: boolean }) {
  return (
    <div className="state" role="status">
      <h3>Created at {task.state}</h3>
      {/* THE FOUR STATE NAMES ARE GONE FROM THE PROSE, not from the product:
          `#help/capacity` lists them, read from `CONCURRENCY_STATES` at render
          time. B7.4 ALSO TOOK THE `?`: the sentence here states the consequence
          -- not running, costs nothing until admission -- which is the whole
          reason a reader needs `capacity` at this moment, and the heading above
          names the state the task is actually in. The glyph's own screen keeps
          one, on step 1. The topic is still one click away on the Agents screen,
          where an empty `live` tab is the case the sentence cannot cover. */}
      <p>
        That is not a running agent — it costs nothing until admission takes
        it.
        {task.park_reason ? <> It is parked: <code>{String(task.park_reason)}</code>.</> : null}</p>
      {/* The id verbatim: not truncated, not transformed. This is what gets pasted. */}
      <p className="mono">{task.id}</p>
      {/* READ BACK OFF THE 201, not off the form. The form says what was asked
          for; `task.dispatch` is what was stored, and a caller who is about to
          wait for a pull request should be told from the second one. */}
      <DispatchFacts task={task} />
      <p className="checked-at">
        {woke ? 'The scheduler was woken by this submission.'
          : 'The scheduler was not woken; it will pick this up on its next pass.'}{' '}
        {/* The EXPLICIT drawer form. `#work/<id>` also resolves -- it is
            the shape the old nav wrote -- but only after every tab name
            has had its chance to match, so a task whose id ever spelled
            `running` or `workflows` would open that tab instead of the
            agent. This screen writes ids it was just handed by the API
            and has no say in, so it writes the form that cannot collide. */}
        <a href={`#work/task/${encodeURIComponent(task.id)}`}>Open it</a>
      </p>
    </div>
  )
}
