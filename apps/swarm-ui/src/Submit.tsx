import { useState, type FormEvent } from 'react'
import { loadCapacity } from './api'
import { isPaused, type ApiError } from './fetch'
import { FailedPanel, Screen } from './Shell'
import { headroomFor, type Capacity, type Pool, type RunnerProfile, type Task } from './types'

/**
 * Submit one task -- and the honest constraints on doing so.
 *
 * INVARIANT 10 IS THE SHAPE OF THIS SCREEN. A caller picks a `runner_profile`
 * BY NAME; the frozen catalogue supplies the image, command, resource class
 * and backend. So the only execution control is a select of names that came
 * from `GET /v1/capacity` -- no image field, no command field, no resource
 * box, and no "advanced" disclosure hiding one. schemas.py sets
 * `extra="forbid"` and main.py turns `extra_forbidden` on any
 * FORBIDDEN_CALLER_FIELD into a message naming the invariant, so a form
 * offering those fields would build the request the API exists to refuse.
 *
 * AND SUBMITTING DOES NOT START AN AGENT. `service._build_task` stores the
 * task at READY, or PARKED when its provider key is missing or it waits on a
 * dependency, and invariant 1 says both cost nothing. What decides whether it
 * moves is the capacity shown beside the select -- which is this tenant's, not
 * the platform's.
 *
 * ON WRITES: fetch.ts is a READ contract. `read()` takes no method or body and
 * `classify()` is private to it, so this first write in the UI does its own
 * POST; api.ts is its real home, not least for the USE_FIXTURES branch every
 * other loader has.
 */

/** The TaskCreate keys this form sends (schemas.py:25-39). */
const FIELDS = ['runner_profile', 'input', 'priority'] as const
type Field = (typeof FIELDS)[number]

type Outcome =
  | { kind: 'idle' | 'sending' }
  | { kind: 'created'; task: Task; woke: boolean }
  /** A 422, attributed to the field that caused it. Never a page-level error. */
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
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      credentials: 'same-origin', // behind IAP the browser already holds the cookie
      body: JSON.stringify(body),
    })
  } catch (err) {
    return fail('unreachable', null, err instanceof Error ? err.message : 'The request did not complete.')
  }
  // Checked before the body and on a 2xx too, for the reason fetch.ts gives:
  // an expired IAP session arrives as a 200 carrying sign-in HTML, and parsing
  // that would report a submission that never happened.
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    return fail('session_expired', res.status,
      'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.')
  }
  let payload: unknown = null
  try {
    payload = await res.json()
  } catch {
    // A body we cannot read does not change the diagnosis.
  }
  const env = isRecord(payload) ? payload : {}
  const message = typeof env.message === 'string' ? env.message : `The API returned HTTP ${res.status}.`

  if (res.status === 201) {
    const task = isRecord(env.task) ? (env.task as unknown as Task) : null
    if (!task) {
      return fail('server_error', res.status,
        'The API reported a 201 but sent no task document, so this screen cannot show what it created. Check Agents before resubmitting.')
    }
    // `=== true`, not `?? false`: only an explicit true means the scheduler was
    // really woken, and a missing key is not a false one.
    return { kind: 'created', task, woke: env.scheduler_woken === true }
  }
  if (res.status === 422) return { kind: 'refused', ...attribute(env, message) }

  // Restated from classify() in fetch.ts, which is private and GET-only. Two
  // screens disagreeing about what a 403 means would be worse than this.
  const lower = message.toLowerCase()
  const byStatus: Record<number, ApiError['kind']> = {
    401: 'unauthenticated', 409: 'conflict', 429: 'rate_limited', 503: 'upstream_degraded',
  }
  const kind: ApiError['kind'] =
    res.status === 403
      ? lower.includes('is disabled') ? 'tenant_disabled'
        : lower.includes('is not permitted') ? 'wrong_domain' : 'admin_required'
      : byStatus[res.status] ?? 'server_error'
  const ra = Number(res.headers.get('retry-after'))
  return {
    kind: 'failed',
    error: {
      kind, httpStatus: res.status, message, detail: env.detail,
      code: typeof env.code === 'string' ? env.code : null,
      retryAfterSeconds: Number.isFinite(ra) && ra > 0 ? ra : undefined,
    },
  }
}

/**
 * Which FIELD a 422 is about. Two writers produce one and they differ:
 * FastAPI's RequestValidationError, which main.py wraps into
 * `detail.errors[].loc` naming the field, and the service's own
 * ValidationFailed, raised before that validator ever runs with NO errors
 * list -- it identifies its field by the shape of `detail` instead.
 *
 * What is left over is reported as unattributed rather than pinned to a
 * guessed field: a message under the wrong input sends someone editing a value
 * that was never the problem.
 */
function attribute(
  env: Record<string, unknown>,
  message: string,
): { fields: Partial<Record<Field, string>>; unattributed: string | null } {
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
    else if (typeof detail.max_bytes === 'number') fields.input = message
  }
  return { fields, unattributed: Object.keys(fields).length === 0 ? message : null }
}

export function SubmitScreen() {
  return (
    <Screen
      title="Submit a task"
      load={loadCapacity}
      summary={(c) =>
        `${Object.keys(c.runner_profiles).length} runner profiles · ${c.pools.length} pools you are admitted against`}
      empty={{
        heading: 'No pools came back',
        body: 'The read of the pools this tenant is admitted against succeeded and returned none. The runner profiles arrive in that same response, so there is no name to choose and this screen cannot submit.',
      }}
    >
      {(c) => <Form capacity={c} />}
    </Screen>
  )
}

function Form({ capacity }: { capacity: Capacity }) {
  const [draft, setDraft] = useState({ runner_profile: '', input: '{}', priority: '' })
  const [outcome, setOutcome] = useState<Outcome>({ kind: 'idle' })
  const names = Object.keys(capacity.runner_profiles).sort()
  const profile: RunnerProfile | null = capacity.runner_profiles[draft.runner_profile] ?? null
  const bad = outcome.kind === 'refused' ? outcome.fields : {}
  const set = (f: Field, v: string) => setDraft((d) => ({ ...d, [f]: v }))

  async function submit(e?: FormEvent) {
    e?.preventDefault()
    const body: Record<string, unknown> = { runner_profile: draft.runner_profile }
    try {
      const parsed: unknown = JSON.parse(draft.input.trim() === '' ? '{}' : draft.input)
      if (!isRecord(parsed)) throw new Error('the API stores input as a JSON object, so this must be one')
      body.input = parsed
    } catch (err) {
      // Caught here and labelled "not sent": a refusal phrased like the API's
      // sends someone looking at the platform for a typo in this textarea.
      const why = err instanceof Error ? err.message : 'this is not JSON'
      setOutcome({ kind: 'refused', fields: { input: `Not sent — ${why}.` }, unattributed: null })
      return
    }
    // Sent as typed, with no min/max on the control and no local integer
    // check. The bounds and the type are the API's; a copy of them here drifts
    // silently the day they move, and the 422 already lands on this field.
    if (draft.priority.trim() !== '') body.priority = Number(draft.priority)
    setOutcome({ kind: 'sending' })
    setOutcome(await postTask(body))
  }

  return (
    <form className="section panel" onSubmit={submit}>
      <h2>One task</h2>
      <label className="t-label" htmlFor="rp">runner profile</label>
      {/* The only execution knob there is. No text-transform: these are real
          identifiers, and a name shown differently from the one sent is
          unusable. */}
      <select id="rp" className="mono" required value={draft.runner_profile}
        onChange={(ev) => set('runner_profile', ev.target.value)}>
        <option value="">choose a profile…</option>
        {names.map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
      <FieldError msg={bad.runner_profile} />
      {profile && <ProfileFacts profile={profile} pools={capacity.pools} />}

      <label className="t-label" htmlFor="in">input (JSON object)</label>
      <textarea id="in" className="mono" rows={5} style={{ width: '100%' }}
        value={draft.input} onChange={(ev) => set('input', ev.target.value)} />
      <p className="muted small">Opaque to the platform: handed to the profile's agent, validated only for size.</p>
      <FieldError msg={bad.input} />

      <label className="t-label" htmlFor="pr">priority</label>
      <input id="pr" className="mono" inputMode="numeric" placeholder="0"
        value={draft.priority} onChange={(ev) => set('priority', ev.target.value)} />
      <FieldError msg={bad.priority} />

      <p style={{ marginTop: 14 }}>
        <button type="submit" disabled={outcome.kind === 'sending' || draft.runner_profile === ''}>
          {outcome.kind === 'sending' ? 'Submitting…' : 'Submit one task'}
        </button>
      </p>

      {outcome.kind === 'refused' && outcome.unattributed && (
        <p className="warn-text" role="alert">
          The API refused this submission and did not say which field: {outcome.unattributed}
        </p>
      )}
      {outcome.kind === 'created' && <Created task={outcome.task} woke={outcome.woke} />}
      {outcome.kind === 'failed' && (
        <>
          <FailedPanel error={outcome.error} onRetry={() => void submit()} />
          {/* A write is not a read: a failure after the request left the
              browser does not prove nothing was created. */}
          <p className="warn-text">
            This failed on a write. If it failed after reaching the API the task may exist anyway — check Agents before submitting again.
          </p>
        </>
      )}
    </form>
  )
}

function FieldError({ msg }: { msg?: string }) {
  return msg ? <p className="warn-text" role="alert">{msg}</p> : null
}

/** What the chosen name actually selects, and whether it can be admitted now. */
function ProfileFacts({ profile, pools }: { profile: RunnerProfile; pools: Pool[] }) {
  const byName = new Map<string, Pool>(pools.map((p) => [p.name, p]))
  const room = headroomFor(profile, byName)
  const binding = room.binding ? byName.get(room.binding) ?? null : null
  // headroomFor's contract: a profile's pool list is the CALLING TENANT'S, so
  // this answers "how many more could I submit", never "what the platform
  // has" -- and its doc requires the tenant beside the number. /v1/capacity
  // carries no tenant field; the tenant is the `tenant:<id>` pool the API
  // itself put in this profile's list.
  const tenantPool = profile.pools.find((p) => p.startsWith('tenant:'))
  const tenant = tenantPool ? tenantPool.slice('tenant:'.length) : null

  return (
    <div className="conjunction">
      <p className="muted">
        <strong>{profile.resource_class}</strong> on <strong>{profile.backend}</strong>
        {profile.provider ? <> · provider <strong>{profile.provider}</strong></> : <> · needs no provider key</>}
        {' '}· costs {profile.units} weighted unit{profile.units === 1 ? '' : 's'} in each of its{' '}
        {profile.pools.length} pools, every one of which must admit it in the same transaction.
      </p>
      <p className="muted">
        Room for <strong>{room.agents}</strong> more task{room.agents === 1 ? '' : 's'} of this
        profile right now, for {tenant ?? 'a tenant this response does not name'}
        {binding && (isPaused(binding)
          ? <> — <code>{binding.name}</code> is paused and admits nothing at all</>
          : <> — held down by <code>{binding.name}</code>, {binding.active} of {binding.effective_limit} weighted units in use</>)}.
      </p>
      {/* Unconfigured means uncapped, so a pool the response does not carry is
          skipped rather than counted as a zero that would read as "full". */}
      {room.missing.length > 0 && (
        <p className="muted small">
          Not in the response, so not counted: {room.missing.join(', ')}.
        </p>
      )}
    </div>
  )
}

function Created({ task, woke }: { task: Task; woke: boolean }) {
  return (
    <div className="state" role="status">
      <h3>Created at {task.state}</h3>
      <p>
        That is not a running agent. Only LEASED, DISPATCHED, STARTING and RUNNING hold capacity,
        so this costs nothing until admission takes it.
        {task.park_reason ? <> It is parked: <code>{String(task.park_reason)}</code>.</> : null}
      </p>
      {/* The id verbatim: not truncated, not transformed. This is what gets pasted. */}
      <p className="mono">{task.id}</p>
      <p className="checked-at">
        {woke ? 'The scheduler was woken by this submission.'
          : 'The scheduler was not woken; it will pick this up on its next pass.'}{' '}
        <a href={`#agents/${encodeURIComponent(task.id)}`}>Open it</a>
      </p>
    </div>
  )
}
