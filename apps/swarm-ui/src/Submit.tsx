import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react'
import { fixtureSubmitTask, loadCapacity, loadModelClasses, loadStats, USE_FIXTURES, type ModelClassOffer } from './api'
import {
  CeilingTag,
  blockerVerdict,
  ceilingFigure,
  classUnits,
  useResourceClasses,
  verdictCopy,
  verdictNeedsAPerson,
} from './Blockers'
import {
  AdditionalRepositoriesPicker,
  AdditionalRepositoryFacts,
  DispatchChoice,
  DispatchFacts,
  REPOSITORY_SCHEMES,
  additionalRepositoriesBody,
  loadTaskReadableRepositories,
  offeredPicks,
  refusedSayId,
  repositorySchemeRefused,
  takesAdditionalRepositories,
  type AdditionalRepositoryPick,
  type DispatchDraft,
  type TaskReadableList,
} from './Dispatch'
import { WorkspaceRefusalBanner, workspaceRefusal } from './WorkspaceRefusal'
import { apiHeaders, chosenTenant, classifyFailure, dropRefusedTenant, isPaused, writeRefused, type ApiError, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { clearTaskPrefill, peekTaskPrefill } from './submitPrefill'
import { ModelClassSelect, RoomFact, RunnerPicker, UnreadRoom, sentModelClass, useProviderKeys } from './RunnerPicker'
import { useInView } from './useInView'
import { AUTO, BROWSER_TOOL_KEY, BrowserToolPicker, declaredBrowserTools, jevAllowed, profileEgress, useJevJudgment, useTypesafeKey, withBrowserTool } from './BrowserToolPicker'
import { QA_IDENTITY_KEY, QaIdentityOption, declaredQaIdentities, useGrantedQaIdentity, withQaIdentity } from './QaIdentityGrants'
import { useRoleGate } from './Profile'
import { MCP_SERVERS_KEY, McpServerPicker, clearCredentials, declaredMcpServers, loadMcpServers, offeredMcpServers, readCredentials, withMcpServers, type McpServerList } from './McpServers'
import { FailedPanel, Screen, useSubmitAs } from './Shell'
import {
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  consequenceOf,
  headroomFor,
  requiredInputKeys,
  type Capacity,
  type Pool,
  type RunnerProfile,
  type Task,
} from './types'
import './styles/submit.css'
import { Button } from './components'

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
 * a name that is not in it cannot be typed. `input` is data for the runner; the
 * API checks only that its keys are ones the profile declares (contract request
 * 25). It is not an execution parameter, and nothing here lets one become
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
const FIELDS = ['runner_profile', 'input', 'strategy', 'carrier', 'repository_url', 'additional_repositories'] as const
type Field = (typeof FIELDS)[number]

type Outcome =
  | { kind: 'idle' | 'sending' }
  | { kind: 'created'; task: Task; woke: boolean }
  /** A 422, on the field that caused it. Never a page-level error. `sent`
   *  tells the API's refusal from this form's own "Not sent": only the first
   *  went anywhere, and only the first is refused again by the same click. */
  | { kind: 'refused'; sent: boolean; fields: Partial<Record<Field, string>>; unattributed: string | null }
  | { kind: 'failed'; error: ApiError }

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

// `method: 'POST'`: a failed submit is a write's failure, and the panel says
// so rather than "a failure to read the platform" (fetch.ts `ApiError.method`).
function fail(kind: ApiError['kind'], httpStatus: number | null, message: string): Outcome {
  return { kind: 'failed', error: { kind, httpStatus, code: null, message, method: 'POST' } }
}

/** POST /v1/tasks. Exported for `submit.errors.test.ts`, which hands it real responses. */
export async function postTask(body: Record<string, unknown>): Promise<Outcome> {
  // The fixture build answers the submit as the 201 does (QA2-FIX-07, runs2-01).
  if (USE_FIXTURES) {
    const made = await fixtureSubmitTask(body)
    return { kind: 'created', task: made.task, woke: made.scheduler_woken }
  }
  const tenant = chosenTenant()
  let res: Response
  try {
    res = await fetch('/v1/tasks', {
      method: 'POST', body: JSON.stringify(body),
      // apiHeaders: the chosen tenant (X-Swarm-Tenant) rides on this submit as
      // on every read, so the task lands in the tenant the spine shows.
      headers: apiHeaders({ 'content-type': 'application/json', accept: 'application/json' }),
      credentials: 'same-origin', // behind IAP the browser already holds the cookie
    })
  } catch (err) {
    return fail('unreachable', null, err instanceof Error ? err.message : 'The request did not complete.')
  }
  // Checked before the body and on a 2xx too, for the reason fetch.ts gives: an
  // expired IAP session arrives as a 200 carrying sign-in HTML, and parsing it
  // would report a submission that never happened. ONLY on a 2xx or a followed
  // redirect, as fetch.ts reads it (QA1 runs-submit-01): a 404 page is a route
  // nobody answered, not a sign-in, and "Reload to sign in again" sent the
  // person to fix a session that had not expired. Any other status is told by
  // its status.
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    if (res.ok || res.redirected) {
      return fail('session_expired', res.status,
        'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.')
    }
    return { kind: 'failed', error: { ...classifyFailure(res.status, null, undefined), method: 'POST' } }
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
  if (res.status === 422) return { kind: 'refused', sent: true, ...attribute(env, message) }

  // THE ONE CLASSIFIER (fetch.ts `classifyFailure`). This used to restate a
  // piece of it and map every 503 to upstream_degraded, so a submit refused
  // because the tenant could not be resolved read as a degraded service.
  const code = typeof env.code === 'string' ? env.code : null
  // A stored tenant the caller is no longer in: forgotten, as read()/write()
  // do, and NOT resubmitted under the default -- the person picked that tenant
  // for this task.
  dropRefusedTenant(res.status, code, tenant)
  const ra = Number(res.headers.get('retry-after'))
  const error: ApiError = { ...classifyFailure(res.status, { code, message, detail: env.detail }, Number.isFinite(ra) && ra > 0 ? ra : undefined), method: 'POST' }
  return { kind: 'failed', error }
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
    // Every refusal of `extrarepos` (too many, not registered, unreadable,
    // too large, disabled, unsupported profile) names its field this way.
    else if (detail.field === 'additional_repositories') fields.additional_repositories = message
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
 * WHAT EACH PROFILE'S RUNNER READS, AND MAY BE SENT.
 *
 * READ FROM THE RUNNER SOURCE ON 2026-09-23 and keyed by PROFILE NAME, which
 * is the one thing this bundle has: `/v1/capacity` serves a profile's resource
 * class, backend, provider, pools and `input_contract`, and NOT its `command`.
 *
 * NO LONGER ONLY A SUGGESTION FOR A DECLARED PROFILE. Since contract request
 * 25 (#142, 2026-09-25) the frozen catalogue declares what `input` may carry
 * per profile, `RunnerProfile.inputs`, and swarm-api refuses any other key
 * with 422 `invalid_input`. So a key offered here for mock, claude-code or
 * codex that the catalogue does not declare is a refusal this form invites --
 * which is why `model` (the CLI runners pass it as `--model`, choosing the
 * model an agent runs) and a `timeout_seconds` offered to every profile are
 * gone. `generic` and `browser` declared nothing until contract request 32
 * (#218, 2026-09-29), and their entries were a runner census; they declare
 * now, so every entry here is held to the declaration like the rest.
 * scripts/lib/check-contract-parity.sh (section 13) holds every entry to the
 * declaration, and
 * tests/unit/control_plane/test_submit_offers_only_what_runners_read.py holds
 * every entry to the declaration AND to a key its runner reads, and every
 * action `ACTION_SHAPE` builds to the declared shape of its `type`.
 *
 * REQUESTED, NOT CHANGED (CLAUDE.md's reporting rule): `/v1/capacity` should
 * serve each profile's declared inputs beside `required_keys`, so this
 * constant can be deleted rather than compared.
 */
const SUGGESTED: Record<string, Suggestion[]> = {
  // runners/cliagent.py (prompt, also required). The prompt is all the
  // catalogue lets a caller send these two.
  'claude-code': [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  codex: [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  // claude-code's runner under its own Job (contract request 36, #295).
  'claude-code-review': [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  // claude-code's runner on agent-runtime-indexer (contract request 48, #625).
  indexer: [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  // claude-code's runner on agent-runtime-browser, Chromium beside the agent
  // (contract request 67).
  'claude-code-browser': [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  // claude-code on the large class, 14,400 s (contract request 113).
  'claude-code-large': [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  // claude-code on agent-runtime-lightpanda, a headless browser with no live
  // view (contract request 127).
  'claude-code-lightpanda': [
    { name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' },
  ],
  // The two worker actions (contract requests 33 and 35, #295) start no
  // runner and take `input: {}`, so they are offered nothing. Listed, empty,
  // so check-contract-parity.sh sees every catalogue profile here.
  merge: [
  ],
  'post-verdict': [
  ],
  // A control_plane profile (contract request 93) runs nothing and declares
  // no input; listed, empty, for the same reason. fix-loop is the second
  // (contract request 124).
  'await-input': [
  ],
  'fix-loop': [
  ],
  // runners/generic.py -- GENERIC_COMMANDS is the frozen argv catalogue, and
  // `command` NAMES an entry in it. That is invariant 10's own pattern, not an
  // exception to it: the caller picks a name, the platform owns the argv.
  generic: [
    { name: 'command', kind: 'text', note: 'names one argv in the platform catalogue', choices: ['pytest', 'npm-ci', 'npm-test', 'npm-build', 'make', 'uv-sync'] },
    { name: 'paths', kind: 'list', note: 'pytest only: existing paths inside the workspace' },
    { name: 'target', kind: 'text', note: 'make only: one target from the repository Makefile' },
    { name: 'working_directory', kind: 'text', note: 'a directory inside the workspace to run in' },
    { name: 'timeout_seconds', kind: 'number', note: 'lowers the child wall clock; the platform ceiling still wins' },
  ],
  // runners/browser.py:76-107. No `timeout_seconds`: the browser runner starts
  // no child through runners/limits.py and never reads it, so an offer here
  // told a user their run was shorter while the platform's ceiling applied
  // unchanged (the review of #213).
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

/** `timeout_seconds` (`runners/limits.py`, which a caller may only LOWER) is
 *  offered to `generic` alone: a declared profile that does not declare it
 *  refuses it, and the browser runner never reads it. */
function suggestionsFor(profile: string): Suggestion[] {
  return SUGGESTED[profile] ?? []
}

const ACTION_TYPES = ['goto', 'click', 'fill', 'press', 'wait_for', 'wait', 'screenshot', 'extract'] as const

/** The scalars each action type reads. Anything not listed is not sent.
 *  `send` is the key the value travels under when it is not `prop`: a
 *  `press` keeps its key in the draft's `text` box, and the runner reads it
 *  as `key`. Sent as `text`, nothing read it, and since contract request 32
 *  the declared `press` shape refuses it. */
const ACTION_SHAPE: Record<string, Array<{ prop: keyof BrowserAction; label: string; send?: string }>> = {
  goto: [{ prop: 'url', label: 'url' }],
  click: [{ prop: 'selector', label: 'selector' }],
  fill: [{ prop: 'selector', label: 'selector' }, { prop: 'text', label: 'text' }],
  press: [{ prop: 'selector', label: 'selector' }, { prop: 'text', label: 'key', send: 'key' }],
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
      // THE KEY NEVER OPENS THE MESSAGE (live QA SUB-15): `asSentence`
      // capitalises a fragment's first letter, and a key is case-sensitive,
      // so "Prompt is set to abc" named a setting this form does not have.
      return { ok: false, message: `the setting ${name} is set twice and an object has one value per key` }
    }
    if (f.kind === 'flag') { input[name] = f.flag; continue }
    if (f.kind === 'number') {
      const raw = f.text.trim()
      if (raw === '') continue
      const n = Number(raw)
      if (!Number.isFinite(n)) return { ok: false, message: `the setting ${name} is set to ${raw}, which is not a number` }
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
          one[part.send ?? part.prop] = part.prop === 'seconds' ? Number(raw) : raw
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
 * WHAT AN INPUT COSTS AS THE API MEASURES IT (live QA SUB-4): Firestore's own
 * rule, restated from `validation.firestore_size` -- a string its UTF-8 bytes
 * plus 1, a number 8, a boolean or null 1, a list the sum of its values, an
 * object the sum of each key (sized as a string) and its value. The API
 * refuses an input over `max_input_bytes` with a 422 after the POST; this is
 * what lets the form say so before it, with the limit in the words. The
 * server's figure stays the one that decides: the form blocks only on a limit
 * the API served (`/v1/stats` `limits.max_input_bytes`), never on a constant.
 */
export function inputBytes(value: unknown): number {
  const utf8 = new TextEncoder()
  let total = 0
  const stack: unknown[] = [value]
  while (stack.length > 0) {
    const v = stack.pop()
    if (v === null || v === undefined || typeof v === 'boolean') total += 1
    else if (typeof v === 'number') total += 8
    else if (typeof v === 'string') total += utf8.encode(v).length + 1
    else if (Array.isArray(v)) stack.push(...v)
    else if (typeof v === 'object') {
      for (const [k, item] of Object.entries(v as Record<string, unknown>)) {
        total += utf8.encode(k).length + 1
        stack.push(item)
      }
    } else total += utf8.encode(String(v)).length + 1
  }
  return total
}

/** A byte count as a reader says it: `256 KiB`, `300.5 KiB`, `812 bytes`. */
export function byteWords(n: number): string {
  if (n < 1024) return `${n} byte${n === 1 ? '' : 's'}`
  const kib = n / 1024
  return `${Number.isInteger(kib) ? kib : kib.toFixed(1)} KiB`
}

/**
 * Why an input will not be taken for its size, or null when it fits -- and
 * null when the limit was not read, since an unread limit is not a limit.
 */
export function inputSizeProblem(input: Record<string, unknown>, limit: number | null): string | null {
  if (limit === null) return null
  const bytes = inputBytes(input)
  return bytes > limit ? `the input is ${byteWords(bytes)}, over the API's ${byteWords(limit)} limit` : null
}

/**
 * `limits.max_input_bytes` off `/v1/stats`, read once for the form; null until
 * it answers and when it does not.
 */
export function useInputLimit(): number | null {
  const [limit, setLimit] = useState<number | null>(null)
  useEffect(() => {
    let live = true
    void loadStats().then((r) => {
      if (!live) return
      const raw = r.status === 'ok' || r.status === 'stale' ? r.data.limits?.max_input_bytes : undefined
      setLimit(typeof raw === 'number' && raw > 0 ? raw : null)
    })
    return () => {
      live = false
    }
  }, [])
  return limit
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
 * The rows `buildInput` leaves out, as the send panel names them (visual QA
 * QA-102): a setting with no name, or a value left blank, adds no key, and
 * the panel said "Ready to send" over a row nobody could tell was dropped.
 * A required key left blank is not here: `missingRequired` names it, and it
 * blocks the send rather than being left out of it. A row that will not build
 * at all is `buildInput`'s refusal, not a row left out.
 */
export function unsentSettings(fields: InputField[]): string[] {
  return fields.flatMap((f) => {
    if (f.required) return []
    const name = f.name.trim()
    if (name === '') return ['a setting with no name']
    const one = buildInput([f])
    return one.ok && !Object.prototype.hasOwnProperty.call(one.input, name) ? [`${name} (blank)`] : []
  })
}

/** A setting's key in words, for its label (QA1 runs-submit-18):
 *  `timeout_seconds` reads "Timeout seconds". The key itself is unchanged. */
export function settingWords(key: string): string {
  const words = key.replace(/_/g, ' ').trim().replace(/\burl\b/g, 'URL')
  return words === '' ? key : words.charAt(0).toUpperCase() + words.slice(1)
}

/** A row of one's own with neither a name nor a value: nothing to send. A
 *  yes / no always has a value, so it is never empty. */
function emptyRow(f: InputField): boolean {
  if (f.name.trim() !== '') return false
  if (f.kind === 'text' || f.kind === 'number') return f.text.trim() === ''
  if (f.kind === 'list') return f.list.every((e) => e.trim() === '')
  return false
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
function ValueEditor({ f, id, choices, invalid, describedBy, onBlur, onChange }: {
  f: InputField; id: string; choices?: string[]
  /** The field is a required key left empty (TS-15): the editor carries it. */
  invalid?: boolean
  /** The id of the line under the editor that says what is missing. */
  describedBy?: string | undefined
  /** Leaving the field is what lets it be marked; see `InputFields`. */
  onBlur?: () => void
  onChange: (next: InputField) => void
}) {
  // A SETTING OF ONE'S OWN HAS NO <label> FOR ITS VALUE (visual QA QA-104):
  // its name is a text box, so nothing could point `htmlFor` here and the
  // value was a bare "textbox". It is named after the setting it holds. A
  // suggested key keeps its label (`htmlFor` in `InputFields`).
  const named = f.own ? `${f.name.trim() || 'setting'} value` : undefined
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
      <input id={id} className="mono sbf-num" inputMode="decimal" value={f.text} placeholder="a number" aria-label={named}
        onChange={(e) => onChange({ ...f, text: e.target.value })} />
    )
  }
  if (f.kind === 'list') {
    return (
      <div className="sbf-list">
        {f.list.map((entry, i) => (
          <input key={i} className="mono" value={entry} id={i === 0 ? id : undefined}
            placeholder="one entry" aria-label={named === undefined ? undefined : `${named} ${i + 1}`}
            onChange={(e) => onChange({ ...f, list: f.list.map((o, j) => (j === i ? e.target.value : o)) })} />
        ))}
        <div className="sbf-listact">
          <button type="button" className="c-link is-sm" onClick={() => onChange({ ...f, list: [...f.list, ''] })}>one more</button>
          {f.list.length > 1 && (
            <button type="button" className="c-link is-sm" onClick={() => onChange({ ...f, list: f.list.slice(0, -1) })}>one fewer</button>
          )}
        </div>
      </div>
    )
  }
  if (f.kind === 'actions') return <BrowserActions f={f} onChange={onChange} />
  if (choices) {
    return (
      <select id={id} className="mono" value={f.text} aria-invalid={invalid || undefined} aria-label={named}
        aria-describedby={describedBy} onBlur={onBlur}
        onChange={(e) => onChange({ ...f, text: e.target.value })}>
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
        placeholder={name === 'prompt' ? 'what this agent should do' : undefined} aria-label={named}
        aria-invalid={invalid || undefined} aria-describedby={describedBy} onBlur={onBlur}
        onChange={(e) => onChange({ ...f, text: e.target.value })} />
    : <input id={id} className="mono" value={f.text} spellCheck={false} aria-label={named}
        aria-invalid={invalid || undefined} aria-describedby={describedBy} onBlur={onBlur}
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
          <button type="button" className="c-link is-sm" aria-label={`remove action ${i + 1}`}
            onClick={() => onChange({ ...f, actions: f.actions.filter((_, j) => j !== i) })}>remove</button>
        </div>
      ))}
      <button type="button" className="c-link is-sm" onClick={() => onChange({ ...f, actions: [...f.actions,
        { key: nextKey++, type: 'goto', url: '', selector: '', text: '', name: '', seconds: '1' }] })}>
        add an action
      </button>
    </div>
  )
}

/**
 * The id of one field's editor, which is what a "missing" button in either
 * send panel focuses. One spelling, so the button and the field agree.
 */
export function inputFieldId(idPrefix: string, key: number): string {
  return `${idPrefix}-f${key}`
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
 *
 * A MISSING REQUIRED KEY IS SAID AT ITS FIELD (TS-15, owner decision
 * 2026-09-25), in the API's own words for it: the key exactly as
 * `required_keys` sent it, in mono, as the field is labelled -- never
 * `input.prompt` or "as a non-empty string", which are the API's jargon. It
 * was said only in the send panel, far from the field, while the field itself
 * had no invalid state. Now the editor carries `aria-invalid` (the warn edge)
 * and one warn-ink line under it names what the runner will not do, with the
 * full consequence as the line's accessible name. Only once the field has been
 * LEFT empty: a runner just picked is not painted red, and until then the
 * `required` tag carries it.
 */
export function InputFields({ profile, fields, required, onChange, idPrefix, attempted = false }: {
  profile: string
  fields: InputField[]
  required: string[] | null
  onChange: (next: InputField[]) => void
  idPrefix: string
  /** A submit was attempted (visual QA run 6, QA-103): every required key
   *  still empty is an error at its field, left or not. */
  attempted?: boolean
}) {
  // The fields that have been left, by field key. Local on purpose: whether a
  // reader has visited a field is this editor's business, not the form's.
  const [left, setLeft] = useState<ReadonlySet<number>>(() => new Set())
  const leave = (key: number) => setLeft((prev) => (prev.has(key) ? prev : new Set(prev).add(key)))
  const missing = new Set(missingRequired(fields, required))
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
      {/* A NAME AND A FACT, NOT AN INSTRUCTION (TS-23, §8.5's forms clause).
          "starts without any input. Add a setting below if this run needs
          one." was a fact and a how-to line; the offers below are the how. */}
      {fields.length === 0 && (
        <p className="sbf-none">
          {required !== null && required.length === 0
            ? <><span className="mono">{profile}</span> requires no input</>
            : 'no settings'}
        </p>
      )}
      {fields.map((f, i) => {
        const id = inputFieldId(idPrefix, f.key)
        const note = noteFor(f.name.trim())
        const key = f.name.trim()
        // BEFORE AN ATTEMPT, ONLY THE `required` TAG (visual QA run 6,
        // 6:flow-submit-form-desktop-02; owner pick 2026-10-23, "Show only
        // 'required'"). QAFIX5 drew a quiet "prompt missing" line under an
        // untouched field; it read as an error before the reader had tried
        // anything. A field left empty, or a submit attempt, draws `gap`'s line.
        const gap = f.required && missing.has(key) && (left.has(f.key) || attempted)
        const missId = `${id}-missing`
        return (
          <div className={f.required ? 'sbf-field is-required' : 'sbf-field has-remove'} key={f.key}>
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
                // IN WORDS, THE KEY IN ITS TITLE (QA1 runs-submit-18): the
                // label was the raw key in mono ("prompt", "timeout_ms"); the
                // key is still what is sent, and the send panel names it.
                <label className="sbf-name" htmlFor={f.kind === 'flag' || f.kind === 'actions' ? undefined : id}>
                  <span className="sbf-words" title={f.name}>{settingWords(f.name)}</span>
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
            </div>
            <ValueEditor f={f} id={id} choices={choicesFor(key)} invalid={gap}
              describedBy={gap ? missId : undefined} onBlur={() => leave(f.key)}
              onChange={(next) => set(i, next)} />
            {/* AFTER ITS FIELD, AND NAMING IT (live QA SUB-17): it came before
                the value in Tab order, so a screen reader heard "remove" and
                then "prompt", and every row's button was called "remove". It
                is still drawn on the name's line (`.sbf-remove`, submit.css);
                only the order a keyboard meets it in has moved. */}
            {!f.required && (
              <button type="button" className="c-link is-sm sbf-remove" aria-label={`remove ${key || 'this setting'}`}
                onClick={() => onChange(fields.filter((_, j) => j !== i))}>
                Remove
              </button>
            )}
            {gap && (
              <p id={missId} className="sbf-miss"
                aria-label={`${profile} will not start without ${key}. A run sent without it is dispatched, given a credential, and then fails, so this form will not send it.`}>
                {profile} will not start without <code>{key}</code>
              </p>
            )}
            {note && <p className="sbf-note">{note}</p>}
            {/* A ROW WITH NO NAME IS NOT SENT, AND SAYS SO (visual QA QA-102):
                `buildInput` skips it, and it looked like any other setting.
                A row with nothing in it at all says that in as many words
                (QA-102, still open after run 6): "no name" read as a name
                still to type, not as a row the send leaves out. */}
            {f.own && key === '' && (
              <p className="sbf-note sbf-unsent">{emptyRow(f) ? 'this empty row will not be sent' : 'no name — not sent'}</p>
            )}
          </div>
        )
      })}
      <AddSetting idPrefix={idPrefix} offers={unused} onAdd={add}
        onOwn={() => onChange([...fields, field({ own: true })])} />
    </div>
  )
}

/**
 * ONE "+ Add setting ▾" BUTTON, AND THE KEYS BEHIND IT (#119, submit.html
 * decided 2026-10-01). The offers were borderless `--surface-2` tiles with a
 * note under each key -- no verb, no glyph, the same fill as the info box
 * above them -- and a first-time reader took them for reference cards. Now
 * there is one control that says what it does, and opening it lists the keys
 * this runner reads, each with the runner's own note, then a setting of your
 * own. The same control on the task form and on every workflow step.
 *
 * A DISCLOSURE, NOT AN ARIA MENU. `role="menu"` promises arrow-key roving and
 * typeahead; a list of ordinary buttons behind `aria-expanded` promises only
 * what it does, and Tab reaches every key. Escape closes it and returns focus
 * to the button. Drawn in flow, not floated: inside a 370px workflow step a
 * popover would clip or cover the step below.
 */
export function AddSetting({ idPrefix, offers, onAdd, onOwn }: {
  idPrefix: string
  offers: Suggestion[]
  onAdd: (s: Suggestion) => void
  onOwn: () => void
}) {
  const [open, setOpen] = useState(false)
  const listId = `${idPrefix}-add-setting`
  const buttonId = `${listId}-button`
  // ESCAPE CLOSES IT FROM ANYWHERE WHILE IT IS OPEN (browser QA D34,
  // 2026-10-04). Handled on the control, it missed the case the owner hit:
  // opening it left focus in a field outside it -- a click does not move
  // focus in every browser -- and Escape there did nothing. A key another
  // control already handled (a dialog, the step panel) is left alone.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      setOpen(false)
      document.getElementById(buttonId)?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, buttonId])
  // OPENED, IT IS BROUGHT INTO VIEW (VQA V137). Drawn in flow under a button
  // that often sits at the bottom of the scroller, the list opened below the
  // fold, under the Reads dock, and the click looked like it did nothing.
  // `nearest` scrolls only as far as the list needs, and not at all when it
  // is already on screen.
  useEffect(() => {
    if (open) document.getElementById(listId)?.scrollIntoView?.({ block: 'nearest' })
  }, [open, listId])
  return (
    <div className="sbf-addset">
      <button type="button" id={buttonId} className="sbf-addset-btn" aria-expanded={open}
        aria-controls={listId} onClick={() => setOpen(!open)}>
        + Add setting <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div id={listId} className="sbf-addset-list" role="group" aria-label="settings to add">
          {offers.map((s) => (
            <button type="button" key={s.name} className="sbf-addset-item" onClick={() => { onAdd(s); setOpen(false) }}>
              <span className="mono">{s.name}</span>
              <em>{s.note}</em>
            </button>
          ))}
          <button type="button" className="sbf-addset-item is-own" onClick={() => { onOwn(); setOpen(false) }}>
            <span>a setting of your own</span>
            <em>any key: text, number, yes / no, list</em>
          </button>
        </div>
      )}
    </div>
  )
}

/** The task form's card for runner `name`, or its radio when it is drawn bare. */
function runnerCard(form: HTMLFormElement | null, name: string): HTMLElement | null {
  const radio = [...(form?.querySelectorAll<HTMLInputElement>('input[name="runner-profile"]') ?? [])].find((r) => r.value === name)
  return radio === undefined ? null : (radio.closest<HTMLElement>('.sbf-runner') ?? radio)
}

/* ========================================================================== */

export function SubmitScreen() {
  return (
    <Screen
      title="Start an agent"
      load={loadCapacity}
      summary={(c) => { const n = Object.keys(c.runner_profiles).length; return `${n} runner${n === 1 ? '' : 's'} to choose from` }}
      empty={{ heading: 'No pools came back',
        body: 'The read of the pools this tenant is admitted against succeeded and returned none. The runner profiles arrive in that same response, so there is no name to choose and this screen cannot submit.' }}
    >
      {(c) => <Form capacity={c} />}
    </Screen>
  )
}

/** One numbered move of the flow: a card, its ordinal an accent disc
 *  (submit.html F1 and G1). `dim` is a move not yet reached -- on the task
 *  form, steps 2 and 3 before a runner is chosen. Dimmed, not disabled: what
 *  it holds is still readable and still reachable by the keyboard. */
export function Move({ n, title, aside, dim = false, children }: {
  n: number; title: string; aside?: ReactNode; dim?: boolean; children: ReactNode
}) {
  const heading = <h2 className="sbf-move-h"><i className="sbf-move-n" aria-hidden="true">{n}</i>{title}</h2>
  return (
    <section className={`sbf-move sb-step${dim ? ' is-dim' : ''}`}>
      {/* THE `?` AFTER THE <h2>, NOT IN IT (QAFIX-HELP), as `PageHead` draws
          the h1's: inside, its name ran on into the heading's. */}
      {aside === undefined ? heading : <div className="sbf-move-head">{heading}{aside}</div>}
      {children}
    </section>
  )
}

/**
 * WHERE A SUBMIT FORM DRAWS WHAT ITS CLICK DID: inside the send panel, under
 * the button (VQA V002/V049, owner decision Q1 on #1038). The task, workflow
 * and issue forms all draw it here, so a failure, a refusal or a 201 is on
 * screen at the place the click was made. `at` is the outcome itself: a new
 * one is brought into view once -- on a phone the panel can sit at a scroll's
 * end -- and typing into the form afterwards does not scroll anything.
 *
 * AND THE KEYBOARD GOES WITH IT (VQA V255): the click that caused it left
 * focus on a button that is now disabled or replaced, so it fell to `<body>`
 * and the next Tab started at the top of the page. The slot takes focus
 * (`tabIndex={-1}`: reachable by script, not a stop of its own), so the next
 * Tab is the outcome's own link or button. `preventScroll`: the scroll above
 * already chose the nearest edge.
 */
export function OutcomeSlot({ at, shown, children }: { at: unknown; shown: boolean; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!shown) return
    ref.current?.scrollIntoView?.({ block: 'nearest' })
    ref.current?.focus({ preventScroll: true })
  }, [at, shown])
  if (!shown) return null
  return <div ref={ref} className="sbf-outcome" tabIndex={-1}>{children}</div>
}

/** The task form's field-id prefix: the editor and the panel's buttons share it. */
const TASK_FIELDS = 'task'
/** The line under the send button that says what a disabled one waits for. */
const WHY_ID = 'task-send-why'
/** The input types a browser never submits a form from on Enter. */
const NO_IMPLICIT_SUBMIT = new Set(['button', 'checkbox', 'file', 'image', 'radio', 'reset', 'submit'])

/**
 * `owner/repo` out of a repository URL, as the send panel names it (#115).
 *
 * Every scheme the API accepts (`REPOSITORY_SCHEMES`, Dispatch.tsx) puts the
 * host first and the path last, so the path is the name a reviewer knows the
 * repository by. Anything this does not recognise comes back as sent: the
 * panel may print a long URL, never a guessed name.
 */
export function repositorySlug(url: string): string {
  const sent = url.trim()
  const m = /^(?:https:\/\/|ssh:\/\/(?:[^@/]+@)?|git@)[^/:]+(?::\d+)?[/:]([^/]+\/[^/]+?)(?:\.git)?\/?$/.exec(sent)
  return m?.[1] ?? sent
}

function Form({ capacity }: { capacity: Capacity }) {
  // A tenant switch made with this form open keeps it, and its button then
  // names the tenant it will submit as (intake-tenants.html 2A).
  const submitAs = useSubmitAs()
  // A VIEWER SUBMITS NOTHING (docs/design/workspace-roles.md §4.8): the button
  // is disabled with the role sentence beside it. The API still decides
  // (`role_member_required`); this only stops offering a click it refuses.
  const roleGate = useRoleGate('member', 'submit')
  // EMPTY, AND IT STAYS EMPTY UNTIL SOMEBODY PICKS (#118/#119, submit.html
  // decided 2026-10-01). No runner is preselected: `blocked` below holds the
  // button while this is '', so an untouched form cannot send anything.
  // THE ONE EXCEPTION: a task another screen handed over (GT-GUIDE, the Git
  // tokens page's "Run a test task"; submitPrefill.ts). It is a pick the
  // person made there, of a runner this catalogue serves; the button still
  // waits for their press here.
  const [handed] = useState(() => peekTaskPrefill())
  useEffect(() => clearTaskPrefill(), [])
  const handedProfile = handed !== null && capacity.runner_profiles[handed.profile] !== undefined ? handed.profile : ''
  const [chosen, setChosen] = useState(handedProfile)
  const [fields, setFields] = useState<InputField[]>(() =>
    handed === null || handed.prompt === ''
      ? []
      : seedFields([field({ name: 'prompt', kind: 'text', text: handed.prompt })], requiredInputKeys(capacity.runner_profiles[handedProfile] ?? undefined)),
  )
  // Seeded with the API's own defaults, so a caller who touches nothing sends
  // the dispatch they would have got before this control existed.
  const [dispatch, setDispatch] = useState<DispatchDraft>({
    strategy: DEFAULT_STRATEGY,
    carrier: DEFAULT_CARRIER,
    repositoryUrl: handed?.repositoryUrl ?? '',
  })
  const [outcome, setOutcome] = useState<Outcome>({ kind: 'idle' })
  // A SUBMIT WAS ATTEMPTED (visual QA run 6, QA-103), by Enter or the button,
  // and from then each input it was missing is an error at its own field --
  // the runner group as well as the prompt. Before it, a field reads only
  // `required`, unpainted (TS-15). Held until "Submit another" starts over.
  const [attempted, setAttempted] = useState(false)
  // Whether this tenant holds each provider's key, for every row of the picker.
  const keys = useProviderKeys()
  const catalogue = useMemo(
    () => Object.entries(capacity.runner_profiles).sort((a, b) => a[0].localeCompare(b[0])),
    [capacity],
  )
  const profile: RunnerProfile | null = capacity.runner_profiles[chosen] ?? null
  const required = requiredInputKeys(profile ?? undefined)
  const bad = outcome.kind === 'refused' ? outcome.fields : {}

  // THE BROWSER TOOL (BSTATUS-SUBMIT): drawn only for a profile that declares
  // `browser_tool`, Auto by default, and Auto sends nothing. A pick the form
  // can no longer honour -- another runner, Jev without a key -- reads as Auto,
  // so the button never sends a tool the picker is not showing as chosen.
  const tools = declaredBrowserTools(profile)
  const [browserTool, setBrowserTool] = useState(AUTO)
  const typesafe = useTypesafeKey(tools !== null)
  const judgment = useJevJudgment(tools !== null)
  const tool = tools !== null && tools.includes(browserTool) && (browserTool !== 'jev' || jevAllowed(typesafe, judgment)) ? browserTool : AUTO

  // THE QA IDENTITY (QI-UI, docs/design/qa-identity.md §4.6): offered only for
  // a profile that declares `qa_identity` AND only once
  // `GET /v1/qa-identities/mine` says this workspace is granted -- otherwise
  // the click meets a 403 `qa_identity_not_granted`. Unticked sends nothing,
  // and a tick the form no longer offers (another runner, a revoke) is not sent.
  const qaOffered = useGrantedQaIdentity(declaredQaIdentities(profile))
  const [qaOn, setQaOn] = useState(false)
  const qaSent = qaOffered !== null && qaOn ? qaOffered : null

  // THE MCP SERVERS (MCP-UI; docs/design/registered-mcp-servers.md §4.3):
  // drawn only for a profile that declares `mcp_servers`, offering the
  // workspace's enabled registrations that profile may use. A pick the list no
  // longer offers -- another runner, a server disabled since -- is dropped, so
  // the button never sends a name the picker is not showing as picked. A
  // `dispatch` server's credential stays in its field's DOM node (`mcpFields`),
  // never in state, and leaves only in the POST's `mcp_credentials`.
  const mcpMax = declaredMcpServers(profile)
  const [mcpList, setMcpList] = useState<Result<McpServerList> | null>(null)
  const mcpDeclared = mcpMax !== null
  useEffect(() => {
    if (!mcpDeclared) return
    let live = true
    void loadMcpServers().then((r) => {
      if (live) setMcpList(r)
    })
    return () => {
      live = false
    }
  }, [mcpDeclared])
  const mcpOffered = mcpMax !== null && mcpList !== null && (mcpList.status === 'ok' || mcpList.status === 'stale')
    ? offeredMcpServers(mcpList.data, chosen) : []
  const [mcpPicked, setMcpPicked] = useState<string[]>([])
  const mcpNames = mcpPicked.filter((n) => mcpOffered.some((s) => s.name === n))
  const mcpFields = useRef(new Map<string, HTMLInputElement>())
  const [mcpFilled, setMcpFilled] = useState<ReadonlySet<string>>(() => new Set())
  const [mcpLifetimes, setMcpLifetimes] = useState<Record<string, number>>({})
  const mcpUnfilled = mcpOffered.filter((s) => mcpNames.includes(s.name) && s.auth.mode === 'dispatch' && !mcpFilled.has(s.name)).map((s) => s.name)
  const onMcpFilled = (name: string, on: boolean) =>
    setMcpFilled((was) => {
      const next = new Set(was)
      if (on) next.add(name)
      else next.delete(name)
      return next
    })
  const forgetCredentials = () => {
    clearCredentials(mcpFields)
    setMcpFilled(new Set())
  }

  // ADDITIONAL REPOSITORIES (MR-UI, #1434): drawn only for a runner that
  // takes them (`takesAdditionalRepositories`), offering the workspace's
  // registrations a task may read. The list is read once, and only once such
  // a runner is chosen; a pick it no longer offers is not sent.
  const takesExtra = takesAdditionalRepositories(profile)
  const [extraList, setExtraList] = useState<Result<TaskReadableList> | null>(null)
  const [extraPicks, setExtraPicks] = useState<AdditionalRepositoryPick[]>([])
  const extraAsked = useRef(false)
  // The answer is kept while the form is mounted, not while the runner is
  // unchanged: the list is asked for once. An answer landing after the form
  // has gone sets nothing.
  const extraLive = useRef(true)
  useEffect(() => {
    extraLive.current = true
    return () => {
      extraLive.current = false
    }
  }, [])
  useEffect(() => {
    if (!takesExtra || extraAsked.current) return
    extraAsked.current = true
    void loadTaskReadableRepositories().then((r) => {
      if (extraLive.current) setExtraList(r)
    })
  }, [takesExtra])
  const extraSent = takesExtra ? additionalRepositoriesBody(offeredPicks(extraPicks, extraList)) : []

  // MODEL CLASS (MODEL-SUBMIT, #1435, design §4.8): the runner's classes
  // from `/v1/runtimes`, read once. A failed read is null and hides the
  // select, so the task runs its runner's default as it did before the select
  // existed. `modelClass` null is the default; a runner change resets it.
  const [offers, setOffers] = useState<Record<string, ModelClassOffer> | null>(null)
  useEffect(() => {
    let live = true
    void loadModelClasses().then((r) => {
      if (live) setOffers(r.status === 'ok' || r.status === 'stale' ? r.data : null)
    })
    return () => {
      live = false
    }
  }, [])
  const [modelClass, setModelClass] = useState<string | null>(null)
  const classSent = sentModelClass(offers?.[chosen], modelClass)

  const pick = (name: string) => {
    // Where the pressed card is, before the render the pick causes (N27).
    const card = runnerCard(formRef.current, name)
    pickedAt.current = card === null ? null : { name, top: card.getBoundingClientRect().top }
    setChosen(name)
    setBrowserTool(AUTO)
    setQaOn(false)
    setModelClass(null)
    setMcpPicked([])
    forgetCredentials()
    setFields((prev) => seedFields(prev, requiredInputKeys(capacity.runner_profiles[name] ?? undefined)))
  }

  const typed = buildInput(fields)
  // The picker's key beside the typed ones: a `browser_tool` typed as a
  // setting of one's own as well is the same one-value-per-key refusal.
  const withTool: BuiltInput = !typed.ok || tool === AUTO ? typed
    : Object.prototype.hasOwnProperty.call(typed.input, BROWSER_TOOL_KEY)
      ? { ok: false, message: `the setting ${BROWSER_TOOL_KEY} is set twice and an object has one value per key` }
      : { ok: true, input: withBrowserTool(typed.input, tool) }
  // The MCP picks the same way: a typed `mcp_servers` beside them is set twice.
  const withMcp: BuiltInput = !withTool.ok || mcpNames.length === 0 ? withTool
    : Object.prototype.hasOwnProperty.call(withTool.input, MCP_SERVERS_KEY)
      ? { ok: false, message: `the setting ${MCP_SERVERS_KEY} is set twice and an object has one value per key` }
      : { ok: true, input: withMcpServers(withTool.input, mcpNames) }
  // The QA identity the same way: a typed `qa_identity` beside the tick is set twice.
  const built: BuiltInput = !withMcp.ok || qaSent === null ? withMcp
    : Object.prototype.hasOwnProperty.call(withMcp.input, QA_IDENTITY_KEY)
      ? { ok: false, message: `the setting ${QA_IDENTITY_KEY} is set twice and an object has one value per key` }
      : { ok: true, input: withQaIdentity(withMcp.input, qaSent) }
  const missing = missingRequired(fields, required)
  // THE REPOSITORY THE CLICK PUSHES TO, from the draft `submit` sends: the
  // trimmed URL, and only while the chosen strategy pushes at all (#115).
  const repo = dispatch.repositoryUrl.trim()
  const pushes = consequenceOf(dispatch.strategy, 1).pushes
  // A PUSH WITH NOWHERE TO PUSH IS NOT READY (VQA V052). The panel already
  // said "none — nowhere to push" and the dispatch control that the API would
  // refuse it (`DispatchOptionError`, `missing: repository_url`), under a
  // "Ready to send" heading and a live button.
  // AND A REPOSITORY THE API WILL REFUSE IS NOT READY EITHER (live QA SUB-3):
  // the field said "it will refuse this one" under "Ready to send" and a live
  // button. `repositorySchemeRefused` is the field's own test, held to the
  // API's tuple by `dispatch.test.ts`, so the field, the heading and the
  // button read one answer. Blank is the line above's case, not this one's.
  const badRepo = repositorySchemeRefused(repo)
  // AN INPUT OVER THE SERVED LIMIT IS NOT READY (live QA SUB-4): a 300,000-
  // character prompt read "Ready to send" and found out from the 422.
  const inputLimit = useInputLimit()
  const tooBig = built.ok ? inputSizeProblem(built.input, inputLimit) : null
  const blocked = chosen === '' || !built.ok || missing.length > 0 || (pushes && repo === '') || badRepo || tooBig !== null
    || mcpUnfilled.length > 0
  // WHAT `blocked` IS WAITING FOR, IN WORDS, BESIDE THE BUTTON IT DISABLES
  // (visual QA QA-101): an untouched form drew a greyed-out button and no
  // sentence naming what was missing. One clause per term of `blocked`, in
  // its order, so the line and the button cannot disagree.
  // EACH CLAUSE KNOWS ITS STEP (visual QA run 4, N27): below 1080 the step
  // that holds the field draws its own clauses (`WhyAt`), because the panel
  // is the last thing on the page and at 390 it was three screens away.
  const whySteps: readonly (readonly [1 | 2 | 3, string])[] = [
    ...(chosen === '' ? [[1, 'choose a runner'] as const] : []),
    ...(chosen !== '' && !built.ok ? [[2, built.message] as const] : []),
    ...(chosen !== '' && missing.length > 0 ? [[2, `fill in ${missing.join(', ')}`] as const] : []),
    ...(pushes && repo === '' ? [[3, 'give a repository url to push to'] as const] : []),
    ...(badRepo ? [[3, 'give a repository url the API accepts'] as const] : []),
    ...(chosen !== '' && tooBig !== null ? [[2, tooBig] as const] : []),
    ...(mcpUnfilled.length > 0 ? [[2, `give ${mcpUnfilled.join(', ')} ${mcpUnfilled.length === 1 ? 'its' : 'their'} credential`] as const] : []),
  ]
  const why = whySteps.map(([, clause]) => clause)
  const whyAt = (step: 1 | 2 | 3) => {
    const clauses = whySteps.filter(([n]) => n === step).map(([, clause]) => clause)
    return clauses.length === 0 || justSent ? null : <p className="sb-note sbf-why-at">Not ready: {clauses.join('; ')}.</p>
  }
  const unsent = chosen === '' ? [] : unsentSettings(fields)
  // ROOM 0 IS A TASK THAT WAITS, NOT ONE REFUSED (visual QA QA-108). The send
  // is still ready -- the API takes it, and it lands READY or PARKED holding
  // no capacity (invariant 1) -- but "Ready to send" over a full pool said
  // nothing about it not starting yet. A measured zero only: an unread room
  // is never read as none (TS-23).
  // AND A ROOM NOT READ MAY WAIT (visual QA run 4, N26): claude-code-browser,
  // whose pools could not be read, said nothing while its neighbours at 0 said
  // "waits". It is told it MAY wait -- never that it will, never that it will
  // not. A runner no pool caps (`uncapped`) does not wait for room at all.
  const roomNow = profile === null ? null : headroomFor(profile)
  const waits: 'yes' | 'maybe' | 'no' =
    roomNow === null ? 'no'
      : roomNow.agents === 0 ? 'yes'
      : roomNow.agents === null && roomNow.basis !== 'uncapped' ? 'maybe'
      : 'no'
  // WHAT THIS CLICK WOULD SEND, as one comparable value. After a 201 the
  // button is "Submit another" for as long as the form still says exactly
  // what was sent (VQA V048, owner decision Q1 on #1038): one more click on an
  // unchanged form was a duplicate task. Any edit makes it a new request.
  const draftKey = JSON.stringify([chosen, built.ok ? built.input : null, dispatch.strategy, dispatch.carrier, repo, extraSent, classSent ?? null])
  const [sentKey, setSentKey] = useState<string | null>(null)
  const justSent = outcome.kind === 'created' && sentKey === draftKey
  // The runner group is an error once a submit has been attempted without one.
  const runnerMissing = attempted && chosen === ''
  // Whether the panel's "Not ready" line is on screen, for the docked strip.
  const [whyRef, whyInView] = useInView<HTMLParagraphElement>('0px')
  const whyShown = typeof globalThis.IntersectionObserver === 'function' && whyInView
  // AND A 422 IS NOT "READY TO SEND" (VQA V002): the panel said so over a live
  // button after the API had refused exactly what the form still held, and the
  // same click would be refused the same way. Held until any edit.
  const [refusedKey, setRefusedKey] = useState<string | null>(null)
  const refusedAsIs = outcome.kind === 'refused' && outcome.sent && refusedKey === draftKey
  const inFlight = useRef(false)
  // "Submit another" starts a blank form: the task just created is one click
  // away from its own card until then.
  const another = () => {
    setChosen('')
    setBrowserTool(AUTO)
    setQaOn(false)
    setModelClass(null)
    setMcpPicked([])
    setMcpLifetimes({})
    setExtraPicks([])
    forgetCredentials()
    setFields([])
    setDispatch({ strategy: DEFAULT_STRATEGY, carrier: DEFAULT_CARRIER, repositoryUrl: '' })
    setOutcome({ kind: 'idle' })
    setSentKey(null)
    setRefusedKey(null)
    setAttempted(false)
    setRestarted((n) => n + 1)
  }
  // AND THE KEYBOARD STARTS AGAIN AT THE TOP (live QA SUB-16): the button
  // that had focus is unmounted by the reset, and focus fell to `<body>`.
  // After the render, so the runner list it lands on is the blank form's.
  const formRef = useRef<HTMLFormElement>(null)
  const [restarted, setRestarted] = useState(0)
  useEffect(() => {
    if (restarted === 0) return
    formRef.current?.querySelector<HTMLElement>('input[name="runner-profile"]:not(:disabled)')?.focus()
  }, [restarted])
  // A RUNNER PICK STAYS ON THE PICK, AND ITS DETAIL COMES INTO VIEW (visual QA
  // run 4, N27). At 390 each pick left the page at its top with the runner's
  // detail -- `ProfileFacts`, drawn under the cards -- below the fold: the pick
  // re-draws all three steps, and the page moved under the card that was
  // pressed. Before the paint, the scroller is moved back by exactly what the
  // card moved, then on by as much of the detail as is below the fold, but
  // never so far that the card itself leaves the top. The scroller's own
  // `scrollTop`, not `scrollIntoView`, which moves every ancestor, the page
  // included; and nothing moves when nothing did.
  const pickedAt = useRef<{ name: string; top: number } | null>(null)
  useLayoutEffect(() => {
    const was = pickedAt.current
    pickedAt.current = null
    if (was === null || was.name !== chosen) return
    const card = runnerCard(formRef.current, chosen)
    if (card === null) return
    // The console's scroller; the document's outside it. jsdom has neither.
    const scroller = card.closest<HTMLElement>('.ctl-scroll') ?? (document.scrollingElement as HTMLElement | null | undefined) ?? null
    if (scroller === null) return
    const moved = card.getBoundingClientRect().top - was.top
    if (moved !== 0) scroller.scrollTop = scroller.scrollTop + moved
    const port = scroller.getBoundingClientRect()
    const portBottom = scroller === document.scrollingElement ? window.innerHeight : port.bottom
    const portTop = scroller === document.scrollingElement ? 0 : port.top
    const detail = formRef.current?.querySelector<HTMLElement>('.sbf-room') ?? null
    if (detail === null) return
    const below = detail.getBoundingClientRect().bottom - portBottom
    const spare = card.getBoundingClientRect().top - portTop
    const by = Math.min(below, spare)
    if (by > 0) scroller.scrollTop = scroller.scrollTop + by
  }, [chosen])
  // The send panel's "`prompt` missing" takes the reader to that field.
  const toField = (key: string) => {
    const f = fields.find((x) => x.name.trim() === key)
    if (f) document.getElementById(inputFieldId(TASK_FIELDS, f.key))?.focus()
  }
  // The refusal card's way to the field a 422 named (VQA V002): its control
  // where it has one that takes focus, else the sentence printed under it --
  // the carrier's select is disabled, and an input with no setting yet has
  // no field to land in.
  const toRefused = (f: Field) => {
    const form = formRef.current
    const control =
      f === 'runner_profile' ? form?.querySelector<HTMLElement>('input[name="runner-profile"]:checked')
        ?? form?.querySelector<HTMLElement>('input[name="runner-profile"]:not(:disabled)')
      : f === 'input' ? (fields[0] === undefined ? null : document.getElementById(inputFieldId(TASK_FIELDS, fields[0].key)))
      : f === 'strategy' ? form?.querySelector<HTMLElement>('input[name="dispatch-strategy"]:checked')
      : f === 'repository_url' ? document.getElementById('dsp-repo')
      : null
    ;(control ?? document.getElementById(refusedSayId(f)))?.focus()
  }

  // THE BUTTON IS DISABLED WHILE `blocked`, AND PRESSING IT IS STILL AN
  // ATTEMPT (visual QA run 6, QA-103). A disabled button takes no click and
  // stops Enter's implicit submission, so the empty form's Start did nothing
  // and the missing runner was named only in the panel. It stays disabled --
  // it looks it (`.sbf-send .c-btn.is-primary:disabled`, submit.css) and says
  // why -- but it lets the pointer through (`.sbf-go`) to the strip that
  // holds it, and Enter in a field of a form whose button is off is caught
  // below; both run `submit`, which marks the fields and sends nothing.
  const submitOff = outcome.kind === 'sending' || blocked || refusedAsIs || roleGate !== null
  const pressOff = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget && submitOff) void submit()
  }
  const enterOff = (e: ReactKeyboardEvent<HTMLFormElement>) => {
    if (e.key !== 'Enter' || !submitOff || e.nativeEvent.isComposing) return
    const t = e.target
    if (!(t instanceof HTMLInputElement) || NO_IMPLICIT_SUBMIT.has(t.type)) return
    e.preventDefault()
    void submit()
  }

  async function submit(e?: FormEvent) {
    e?.preventDefault()
    // ONE POST PER CLICK, WHATEVER SUBMITS THE FORM (live QA SUB-1). After a
    // 201 the button is "Submit another" (`type="button"`), and the form is
    // left with one text input -- the repository URL -- so Enter there was
    // still an implicit submission, and it posted the same task again. The
    // guard is here, not on the button: a request in flight, or the one that
    // was just created and is still what the form says, is never sent twice.
    if (inFlight.current || outcome.kind === 'sending' || justSent || refusedAsIs || roleGate !== null) return
    setAttempted(true)
    // The button is disabled with no runner; this holds the same rule for any
    // other way a form can be submitted, so nothing is posted without a name.
    if (chosen === '') {
      setOutcome({ kind: 'refused', sent: false, fields: { runner_profile: 'Not sent — no runner chosen.' }, unattributed: null })
      return
    }
    if (!built.ok) {
      // Labelled "not sent": a refusal phrased like the API's sends someone
      // looking at the platform for a typo that is in this form.
      setOutcome({ kind: 'refused', sent: false, fields: { input: `Not sent — ${built.message}.` }, unattributed: null })
      return
    }
    if (missing.length > 0) {
      // A CLICK'S RESULT, so it is the one place "Not sent" belongs (TS-15).
      // The keys as the API named them, without its `input.` prefix or its
      // "as a non-empty string".
      setOutcome({ kind: 'refused', sent: false, unattributed: null, fields: { input:
        `Not sent — ${chosen} will not start without ${missing.join(', ')}.` } })
      return
    }
    // The button is off for both; this keeps them for Enter as well, and for
    // the press the disabled button lets through to its strip.
    if (pushes && repo === '') {
      setOutcome({ kind: 'refused', sent: false, unattributed: null, fields: { repository_url:
        `Not sent — strategy ${dispatch.strategy} pushes, so it needs a repository URL.` } })
      return
    }
    if (badRepo) {
      setOutcome({ kind: 'refused', sent: false, unattributed: null, fields: { repository_url:
        `Not sent — the API accepts only a URL starting with ${REPOSITORY_SCHEMES.join(', ')}.` } })
      return
    }
    if (tooBig !== null) {
      setOutcome({ kind: 'refused', sent: false, unattributed: null, fields: { input: `Not sent — ${tooBig}.` } })
      return
    }
    if (mcpUnfilled.length > 0) {
      setOutcome({ kind: 'refused', sent: false, unattributed: null, fields: { input:
        `Not sent — ${mcpUnfilled.join(', ')} ${mcpUnfilled.length === 1 ? 'needs its' : 'need their'} credential for this run.` } })
      return
    }
    // Read once, here, from the fields; never held in state.
    const credentials = readCredentials(mcpFields, mcpOffered, mcpNames, mcpLifetimes)
    setOutcome({ kind: 'sending' })
    // A REF, NOT THE STATE ABOVE: two Enters inside one render both read
    // `outcome` as it was before the first, so only this sees the second.
    inFlight.current = true
    const sending = draftKey
    const result = await postTask({
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
        // Omitted when none is picked: an absent list and `[]` both read none,
        // and with the API's flag off only a non-empty one is refused.
        ...(extraSent.length === 0 ? {} : { additional_repositories: extraSent }),
        // A CLASS NAME, never a model ID (invariant 10), and only one chosen
        // over the runner's default (#1435).
        ...(classSent === undefined ? {} : { model_class: classSent }),
        // BESIDE `input`, never in it: `input` is stored and shown, and the
        // credential is neither (§4.3). Omitted when no `dispatch` server is picked.
        ...(Object.keys(credentials).length === 0 ? {} : { mcp_credentials: credentials }),
      })
    inFlight.current = false
    // NEVER RENDERED BACK: whatever the answer, the field is emptied. A second
    // send needs the credential typed again.
    forgetCredentials()
    if (result.kind === 'created') setSentKey(sending)
    if (result.kind === 'refused') setRefusedKey(sending)
    setOutcome(result)
  }

  return (
    <form ref={formRef} className="sbf" onSubmit={submit} onKeyDown={enterOff}>
      <div className="sbf-build">
        {/* THIS SCREEN'S ONE `?` (B7.4). It was seven: two of them repeated a
            sentence printed beside the glyph, one repeated `all at once` from
            the line it sat on, one repeated a bold `That is not zero.`, and one
            was a second copy of the topic on step 2. What stays is invariant
            10, because it is the rule about what this form may NOT accept --
            no image, no command, no resource spec, no backend parameter -- and
            a form cannot state that by labelling the fields it does have. */}
        <Move n={1} title="Choose a runner" aside={<HelpCard topic="runner-profile-by-name" />}>
          {/* THE CATALOGUE, VISIBLE, with each runner's room and key (#114).
              Invariant 10 says a caller names one of these and supplies no
              image, command, resource spec or backend -- and a `<select>` hid
              the very thing that makes that safe. `RunnerPicker` is the one
              control both Submit forms draw. */}
          <RunnerPicker group="runner-profile" label="runner" profiles={catalogue} chosen={chosen}
            keys={keys} onPick={pick} invalid={runnerMissing}
            describedBy={runnerMissing && bad.runner_profile ? refusedSayId('runner_profile') : undefined} />
          {profile && (
            <ModelClassSelect id="task-model-class" label="agent model class" offer={offers?.[chosen]} value={modelClass}
              onPick={setModelClass} />
          )}
          {/* "No runner chosen" is this form's own refusal, and a pick
              answers it; the API's own refusal stays until the next send. */}
          {bad.runner_profile && (chosen === '' || outcome.kind !== 'refused' || outcome.sent) && (
            <p id={refusedSayId('runner_profile')} tabIndex={-1} className="warn-text" role="alert">{bad.runner_profile}</p>
          )}
          {profile && <ProfileFacts name={chosen} profile={profile} pools={capacity.pools} />}
          {whyAt(1)}
        </Move>

        {/* NO `aside` (B7.4): this was the second copy of `input-is-opaque` on
            one screen, four hundred lines from the first. */}
        <Move n={2} title="Say what it should do" dim={chosen === ''}>
          {/* A STATE, NOT AN INSTRUCTION (TS-23). "Choose a runner first --
              what it reads is what this asks for." was a how-to line and a
              rationale; step 1 is directly above. */}
          {chosen === '' ? (
            <p className="sbf-none">no runner chosen</p>
          ) : (
            <>
              {tools !== null && (
                <BrowserToolPicker tools={tools} value={tool} onChange={setBrowserTool} typesafe={typesafe} judgment={judgment}
                  egress={profileEgress(profile)} />
              )}
              {qaOffered !== null && <QaIdentityOption identity={qaOffered} checked={qaOn} onChange={setQaOn} />}
              {mcpMax !== null && (
                <McpServerPicker list={mcpList} profile={chosen} max={mcpMax} picked={mcpNames} onPick={setMcpPicked}
                  fields={mcpFields} filled={mcpFilled} onFilled={onMcpFilled}
                  lifetimes={mcpLifetimes} onLifetime={(name, seconds) => setMcpLifetimes((was) => ({ ...was, [name]: seconds }))} />
              )}
              <InputFields profile={chosen} fields={fields} required={required} onChange={setFields} idPrefix={TASK_FIELDS}
                attempted={attempted} />
              {takesExtra && (
                <AdditionalRepositoriesPicker list={extraList} picks={extraPicks} onChange={setExtraPicks}
                  error={bad.additional_repositories} />
              )}
            </>
          )}
          {bad.input && <p id={refusedSayId('input')} tabIndex={-1} className="warn-text" role="alert">{bad.input}</p>}
          {whyAt(2)}
        </Move>

        {/* `steps={1}`: a standalone task IS one step, and that is the number the
            pull-request counts on the options are computed from. `scale="task"`
            is the same word `resolve_dispatch_options` takes, and it is what
            keeps `integrate` and the carrier off this form -- with one line
            pointing to the workflow form -- rather than a 422 later. */}
        <Move n={3} title="Choose what happens to the result" dim={chosen === ''}>
          <DispatchChoice
            draft={dispatch}
            onChange={setDispatch}
            steps={1}
            scale="task"
            errors={{ strategy: bad.strategy, carrier: bad.carrier, repository_url: bad.repository_url }}
          />
          {whyAt(3)}
        </Move>
        {/* THE REASON KEPT IN VIEW BELOW 1080 (visual QA r4:flow-submit-form-
            phone-390-02, run 5b). At 390 the send panel -- the only place the
            empty form said why Start was disabled -- is the last block, below
            every step and the fold. This line rides the foot of the screen
            while the steps scroll (`.sbf-dock`, submit.css) and is not drawn
            from 1080, where the panel is sticky beside the steps. Hidden from
            the accessibility tree: the button's description is the same line. */}
        {/* AND IT IS SAID ONCE (visual QA run 6, work-b-005): at 390 the strip
            came to rest about 330px above the panel's own "Not ready" line,
            both in view. It steps aside while that line is on screen. Where
            nothing can say so (no IntersectionObserver, jsdom), the strip
            stays: drawn twice beats the reason not drawn at all. */}
        {blocked && !justSent && !whyShown && <p className="sb-note sbf-dock" aria-hidden="true">Not ready: {why.join('; ')}.</p>}
      </div>

      {/* ONE PLACE TO ACT, and it carries what the click will do. The old form
          put the button at the bottom of a scroll and the capacity facts 600px
          above it, so the two things a submitter needs at the same instant --
          is there room, and what am I about to send -- were never on screen
          together. */}
      <aside className="sbf-side">
        <div className="sbf-send">
          {/* FROM `blocked`, THE SAME VALUE THAT DISABLES THE BUTTON. The
              heading said `Ready to send` over a greyed-out button with no
              runner chosen and an empty prompt -- the panel's title and its
              only control disagreeing about the one thing the panel is for. */}
          <h2>{justSent ? 'Sent' : refusedAsIs ? 'Refused' : blocked ? 'Not ready to send' : 'Ready to send'}</h2>
          <ul className="ctl-facts">
            <li className={chosen === '' ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>runner</b>
              {chosen === '' ? <i className="ctl-em">&mdash;</i> : <code>{chosen}</code>}
            </li>
            {/* ONE STATEMENT ABOUT THE INPUT (QA1 runs-submit-18): an empty
                input beside "not sent: prompt (blank)" said the same nothing
                twice, once as "the runner gets no settings". The "not sent"
                row says it, so this one is not drawn. */}
            {!(chosen !== '' && built.ok && tooBig === null && missing.length === 0
              && Object.keys(built.input).length === 0 && unsent.length > 0) && (
            <li className={chosen === '' ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>input</b>
              {/* THREE DIFFERENT NOTHINGS, AND THEY DO NOT SHARE A RENDERING.
                  No runner chosen yet is an em dash: nothing has been decided.
                  A chosen runner with no settings is a MEASURED empty object,
                  which is a real thing to send and says so in words -- but
                  only when nothing is required (TS-15): over a missing key it
                  would describe a send that cannot happen. A value that will
                  not build, or a required key with nothing in it, is neither
                  -- it is the reason the send is blocked, and a missing key is
                  a button to its field, the workflow panel's per-step pattern. */}
              {chosen === '' ? <i className="ctl-em">&mdash;</i>
                : !built.ok ? <i className="sbf-bad">{built.message}</i>
                  : tooBig !== null ? <i className="sbf-bad">{tooBig}</i>
                  : missing.length > 0 ? (
                    <span className="sbf-gaps">
                      {missing.map((k) => (
                        <button type="button" key={k} className="c-link is-sm sbf-bad" onClick={() => toField(k)}>
                          <code>{k}</code> missing
                        </button>
                      ))}
                    </span>
                  )
                  : Object.keys(built.input).length === 0
                    ? <i className="sbf-empty">empty — the runner gets no settings</i>
                    : <span className="mono">{Object.keys(built.input).join(', ')}</span>}
            </li>
            )}
            <li className="ctl-fact">
              <b>result</b>
              {dispatch.strategy}
            </li>
            {/* WHERE IT LANDS, AND WHAT THAT COSTS (F1). `_build_task` stores
                a task at READY, or PARKED when its key is missing or it waits
                on a dependency; invariant 1 says neither holds capacity. Drawn
                in the words a person reads, never the enum in capitals
                (VQ-026). */}
            <li className="ctl-fact">
              <b>lands as</b>
              {waits === 'yes' ? 'ready or parked · waits for room · costs nothing'
                : waits === 'maybe' ? 'ready or parked · may wait for room: room not read · costs nothing'
                : 'ready or parked · costs nothing'}
            </li>
            {unsent.length > 0 && (
              <li className="ctl-fact">
                <b>not sent</b>
                <i className="sbf-bad">{unsent.join(', ')}</i>
              </li>
            )}
            {/* WHAT THE CLICK COMMITS TO (#115), each read from the value the
                button and the request are built from: the chosen profile's
                weight and pool count (`in each of`, never `x`, which reads as a
                total), its room off `headroomFor` -- the same `RoomFact` the
                picker row draws -- and, when the strategy pushes, the
                repository it pushes to. A blank one is named, because the API
                refuses a push with nowhere to push. */}
            <li className={profile === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>cost</b>
              {profile === null ? <i className="ctl-em">&mdash;</i> : <>
                {profile.units} unit{profile.units === 1 ? '' : 's'} in each of {profile.pools.length}{' '}
                pool{profile.pools.length === 1 ? '' : 's'}
              </>}
            </li>
            <li className={profile === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>room</b>
              {profile === null ? <i className="ctl-em">&mdash;</i> : <span><RoomFact profile={profile} /></span>}
            </li>
            {pushes && (
              <li className="ctl-fact">
                <b>repository</b>
                {/* ONE LINE, CUT, WHOLE IN ITS TITLE (VQA V130): an address
                    `repositorySlug` does not recognise comes back whole, and it
                    wrapped over eight lines where the issue panel cuts it. */}
                {repo === '' ? <i className="sbf-bad">none — nowhere to push</i>
                  : badRepo ? <i className="sbf-bad sbf-cut" title={repo}>not a URL the API accepts</i>
                  : <code className="sbf-cut" title={repo}>{repositorySlug(repo)}</code>}
              </li>
            )}
            {!pushes && badRepo && (
              <li className="ctl-fact">
                <b>repository</b>
                <i className="sbf-bad sbf-cut" title={repo}>not a URL the API accepts</i>
              </li>
            )}
          </ul>
          {/* The panel's "{runner} requires input.x as a non-empty string"
              alert is gone (TS-15): the field says it, and the fact above is
              the way to the field. */}
          {justSent ? (
            <Button key="another" type="button" kind="primary" full onClick={another}>Submit another</Button>
          ) : (
            <div className={submitOff ? 'sbf-go is-off' : 'sbf-go'} onClick={pressOff}>
              <Button key="submit" type="submit" kind="primary" full disabled={submitOff}
                aria-describedby={blocked && !justSent ? WHY_ID : undefined}>
                {outcome.kind === 'sending' ? 'Submitting…' : submitAs === null ? 'Start one agent' : `Submit as ${submitAs}`}
              </Button>
            </div>
          )}
          {blocked && !justSent && <p ref={whyRef} id={WHY_ID} className="sb-note sbf-why">Not ready: {why.join('; ')}.</p>}
          {roleGate !== null && (
            <p className="sb-note" data-role-gate="member">
              {roleGate}
            </p>
          )}
          {/* SAY IT UNDER THE BUTTON (owner decision 2026-10-22, Console Design
              Calls 2 c09; box comment 6283735984). "Ready to send" over a live
              "Start one agent" with a measured room of 0 said nothing about the
              task not starting: the heading and the button stay -- the send is
              valid, and it lands holding nothing (invariant 1) -- and this note
              says what happens next, in the warning ink. A MEASURED zero only:
              an unread room keeps the admits note, and "lands as" says it may
              wait (N26). It follows `profile`, so each room read re-decides it.
              THE ROOM IS SAID ONCE, IN THE SUMMARY (visual QA pass 2, runs2-07):
              "Room for 0 now:" here repeated the summary's "room for 0 more"
              and its "waits for room", so the note keeps c09's what-happens-
              next and drops the figure. */}
          {waits === 'yes'
            ? <p className="sb-note sbf-wait">It is accepted and waits, holding nothing, until every pool it needs has room.</p>
            : <p className="sb-note">Nothing runs until the scheduler admits it into every pool it needs.</p>}
          {/* THE OUTCOME IS DRAWN BESIDE THE BUTTON THAT CAUSED IT (VQA V002,
              owner decision Q1 on #1038), as the issue form draws it. It was
              at the top of the build column, ~1,400px above the button at
              400px, so a failed write looked like nothing had happened.
              A REFUSAL TOO (VQA2 V002): a 422 naming a field was drawn only
              under that field, ~1,000px above the button at 400, and one
              naming none under the note, outside this slot. The field keeps
              its sentence; this card says the click was refused, and names
              each field with a way to it. */}
          <OutcomeSlot at={outcome} shown={outcome.kind === 'created' || outcome.kind === 'failed' || outcome.kind === 'refused'}>
            {outcome.kind === 'refused' && <Refused outcome={outcome} onField={toRefused} />}
            {outcome.kind === 'created' && <Created task={outcome.task} woke={outcome.woke} />}
            {/* The submission gate's refusal (#847): why, in the API's words, and the
                setup step that fixes it. It is the one failure that proves nothing
                was created, so the warning below is not drawn under it. */}
            {outcome.kind === 'failed' && workspaceRefusal(outcome.error) !== null && <WorkspaceRefusalBanner error={outcome.error} />}
            {outcome.kind === 'failed' && workspaceRefusal(outcome.error) === null && (
              <>
                <FailedPanel error={outcome.error} onRetry={() => void submit()} />
                {/* Only where the failure is ambiguous (QA2-FIX-07, runs2-01): a
                    definite refusal (4xx) created nothing, and its panel says so. */}
                {/* A write is not a read: a failure after the request left the browser
                    does not prove nothing was created. */}
                {/* NO `?` (B7.4). `ambiguous-write` is "a failure after the request
                    left the browser does not prove nothing was created", and the
                    two sentences here ARE that, with the remedy first. */}
                {!writeRefused(outcome.error) && (
                <p className="warn-text">
                  <strong>Check Agents before submitting again.</strong> This failed on
                  a write, so the agent may exist anyway.
                </p>
                )}
              </>
            )}
          </OutcomeSlot>
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
  // THE POOL THE SENTENCE NAMES, and what its ceiling says about it. The
  // sentence has room for one pool, and `room.binding` is the FIRST binding
  // pool in the profile's order -- so `global` at its ceiling was named ahead
  // of a pool an operator had capped at zero, and the sentence said "wait"
  // about a task no wait will start. The pool a person must act on wins, as it
  // does in the one-line wait reason (`leadBlocker` in types.ts).
  //
  // A LEAD WHOSE CEILING IS NOT A FULL ONE -- paused, or at limit 0 -- is
  // described by `ceilingCopy`, the sentence the wait reason uses, never as
  // "held down by X, 0 of 0 weighted units in use": that fraction is the live
  // "busy platform-wide. (0/0)" in this box's words. `blockerCeiling` decides
  // it from the blocker the server sent, not from the pool row.
  //
  // AND A LEAD WHOSE LIMIT IS BELOW ONE TASK (#66): a browser task (then 2
  // units; contract request 79 made it 1, so today it is a large task) under a
  // limit of 1 read "held down by resource:browser, 0 of 1 weighted units in
  // use" -- wait -- about a task no wait will admit. The weight is the class
  // catalogue's (`/v1/resource-classes`); null until it answers, and then the
  // box reads exactly as it did before.
  const units = classUnits(useResourceClasses(), profile.resource_class)
  const lead =
    room.blockers.find((b) => verdictNeedsAPerson(blockerVerdict(b, units))) ??
    room.blockers.find((b) => b.pool === room.binding)
  const leadCopy = lead ? verdictCopy(lead, units, lead.pool) : null
  const bindingName = lead?.pool ?? room.binding
  const binding = bindingName ? byName.get(bindingName) ?? null : null
  // headroomFor's contract: a profile's pool list is the CALLING TENANT'S, so
  // this answers "how many more could I submit", never "what the platform has"
  // -- and its doc requires the tenant beside the number. /v1/capacity carries
  // no tenant field; the tenant is the `tenant:<id>` pool it put in this list.
  const tenantPool = profile.pools.find((p) => p.startsWith('tenant:'))
  const tenant = tenantPool ? tenantPool.slice('tenant:'.length) : null
  // THE POOL THE SENTENCE ALREADY TOLD, ONCE (QA1 runs-submit-20): "held down
  // by X, 4 of 4 weighted units in use" and then a red `full` chip on X with
  // the same 4 of 4 said one fact twice in one box, a third time under the
  // button. Only that branch: a paused or limit-0 lead keeps its tag, whose
  // title says who must act. Every OTHER refusing pool keeps its row.
  const told = room.agents !== null && leadCopy === null && binding !== null && !isPaused(binding) && binding.effective_limit !== null
    ? binding.name
    : null
  const others = room.blockers.filter((b) => b.pool !== told)

  return (
    <div className="sbf-room">
      {/* ONE SENTENCE IN THIS BOX, AND THE COST IS A FACT ON ITS HEAD (TS-23).
          "Costs N weighted units in each of its M pools, all at once." was a
          second sentence; the figures are facts and sit on the head. "In each
          of", never "x", which reads as a total. "All at once" is the
          all-or-nothing rule, and that lives in the capacity help topic.
          The decided phrase at every pool count, one pool included: "in each
          of 1 pool", not a second wording. Each noun agrees with its count. */}
      <p className="sbf-room-h">
        <span className="mono">{name}</span> right now · {profile.units} unit{profile.units === 1 ? '' : 's'}{' '}
        in each of {profile.pools.length} pool{profile.pools.length === 1 ? '' : 's'}
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
              {/* NEVER A ZERO HERE, AND NOW DRAWN RATHER THAN SAID (TS-23).
                  This was "Room could not be measured: N of its pools could not
                  be read. That is not zero." -- two sentences, and "not
                  measured" is the ABSENT mark's word, not the unread one's. It
                  is the kit's unread encoding (§8.6): the `not read` mark, whose
                  accessible name is `room-unknown-not-zero`'s rule, then
                  `room unknown` and the count of pools that did not answer. No
                  room figure, and no digit in the room slot but that pool
                  count, which §8.5 allows as a bare count. The picker's own
                  `UnreadRoom`, so the two cannot drift: this line said
                  "room unknown ·" after the picker's stopped (work-b-006). */}
              <UnreadRoom profile={profile} unread={room.unread.length} />
            </>
          )
        ) : (
          <>
            Room for <strong>{room.agents}</strong> more agent{room.agents === 1 ? '' : 's'} of this
            profile right now, for {tenant ?? 'a tenant this response does not name'}
            {leadCopy !== null
              ? <>. {leadCopy}</>
              : <>
                {binding && (isPaused(binding)
                  ? <> — <code>{binding.name}</code> is paused and admits nothing at all</>
                  : binding.effective_limit === null
                  ? <> — <code>{binding.name}</code> has no limit set and admits nothing until somebody sets one</>
                  : <> — held down by <code>{binding.name}</code>, {binding.active} of {binding.effective_limit} weighted units in use</>)}.
              </>}
          </>
        )}
      </p>
      {/* EVERY pool refusing it, not the tightest. Two ceilings can bind at
          the same moment, and a submitter shown one of them raises it, tries
          again and gets refused by the other. The tag and the figure are
          Blockers.tsx's own (`CeilingTag`, `ceilingFigure`): this row was a
          second copy of that one, decided `full` on the reason alone, and
          kept printing "0 of 0" and "0 of 1000000" after the first stopped. */}
      {others.length > 0 && (
        <ul className="blocker-list compact">
          {others.map((b) => (
            <li key={b.pool} className="blocker-row">
              <span className="tags">
                <CeilingTag blocker={b} units={units} />
              </span>
              <code>{b.pool}</code> — {ceilingFigure(b, units)}
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

/** What the panel calls each field a refusal can name: its facts' own keys. */
const FIELD_WORD: Record<Field, string> = {
  runner_profile: 'runner', input: 'input', strategy: 'result', carrier: 'carrier', repository_url: 'repository',
  additional_repositories: 'additional repositories',
}

/**
 * A REFUSED CLICK, IN THE SEND PANEL (VQA V002). The API's 422 or this form's
 * own "Not sent": what was refused, each named field a button to the field
 * (where its sentence stays), and the invariant -- nothing was created.
 */
function Refused({ outcome, onField }: {
  outcome: Extract<Outcome, { kind: 'refused' }>
  onField: (f: Field) => void
}) {
  const named = FIELDS.filter((f) => outcome.fields[f] !== undefined)
  return (
    // `alert`, as the field's own sentence is: a refusal, not a record of something made.
    <div className="state failed" role="alert">
      <h3>{outcome.sent ? 'The API refused this agent' : 'Not sent'}</h3>
      {outcome.unattributed !== null && <p>It did not say which field: {outcome.unattributed}</p>}
      {named.length > 0 && (
        <ul className="sbf-refused">
          {named.map((f) => (
            <li key={f}>
              <button type="button" className="c-link is-sm" onClick={() => onField(f)}>{FIELD_WORD[f]}</button>
              {' — '}
              {/* The field's own sentence opens "Not sent —"; the heading says it here. */}
              {outcome.fields[f]!.replace(/^Not sent — /, '')}
            </li>
          ))}
        </ul>
      )}
      <p>{outcome.sent ? 'Nothing was created. Change what it names and submit again.' : 'Nothing was sent, so nothing was created.'}</p>
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
      <p className="mono sbf-id">{task.id}</p>
      {/* READ BACK OFF THE 201, not off the form. The form says what was asked
          for; `task.dispatch` is what was stored, and a caller who is about to
          wait for a pull request should be told from the second one. */}
      <DispatchFacts task={task} />
      {/* AND THE COMMIT EACH ADDITIONAL REPOSITORY WAS PINNED TO, off the 201:
          the ref was resolved at submission, so this is the code the agent
          reads even if the branch moves before it starts. */}
      <AdditionalRepositoryFacts task={task} />
      {/* A SENTENCE, SET AS ONE (live QA SUB-7): it was a `checked-at` line,
          which is the micro MONOSPACE of a read's age, not prose. */}
      <p className="sbf-woke">
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
