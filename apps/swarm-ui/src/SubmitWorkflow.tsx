import { useState } from 'react'
import { loadCapacity, loadStats } from './api'
import { DispatchChoice, type DispatchDraft } from './Dispatch'
import { errorHeading, type ApiError, type ApiErrorKind, type Result } from './fetch'
import { Screen, timeAgo } from './Shell'
import {
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  consequenceOf,
  type DispatchStrategy,
  type RunnerProfile,
  type Workflow,
} from './types'

/**
 * Submit a multi-step workflow -- the only WRITE screen in this UI.
 *
 * NO CLIENT-SIDE DAG CHECK. `validate_dag` (swarm_api/validation.py) names one
 * concrete cycle -- "workflow dependency graph contains a cycle: build -> test
 * -> build" -- and that sentence is the entire value of the 422. A copy of it
 * here would restate server logic check-contract-parity.sh cannot check, so it
 * would drift from the thing that actually decides.
 *
 * AND A FAILED POST IS NOT A FAILED GET: a refused request created nothing, a
 * request that never came back may have created everything. Two renderings, and
 * the ambiguous one offers no retry -- a blind resubmit runs it twice.
 */

interface FormSources {
  /** The catalogue. Invariant 10: a caller names one of these and sends nothing else. */
  profiles: Array<[string, RunnerProfile]>
  /** `limits` from /v1/stats, or null when that read did not produce them. */
  limits: Record<string, number> | null
  limitsDetail: string
}

async function loadSubmitForm(): Promise<Result<FormSources>> {
  const [cap, stats] = await Promise.all([loadCapacity(), loadStats()])
  // The catalogue IS the form: a caller picks a runner_profile by name, so a failed
  // read here must not degrade into a text box to type one into.
  if (cap.status === 'loading' || cap.status === 'empty' || cap.status === 'error') return cap
  const profiles = Object.entries(cap.data.runner_profiles).sort((a, b) => a[0].localeCompare(b[0]))
  // loadCapacity's emptiness test is `pools.length === 0`, so pools plus an empty
  // catalogue arrive as `ok`. Still nothing submittable, still a real zero.
  if (profiles.length === 0) return { status: 'empty', fetchedAt: cap.fetchedAt }
  // The limit is the server's number. A failed stats read means we do not know it
  // -- not that it is 50 -- so in that case nothing caps the form.
  const limits = stats.status === 'ok' || stats.status === 'stale' ? stats.data.limits : null
  const detail = stats.status === 'error' ? stats.error.message : 'The stats read returned no limits.'
  const data: FormSources = { profiles, limits, limitsDetail: detail }
  return cap.status === 'stale'
    ? { status: 'stale', data, fetchedAt: cap.fetchedAt, error: cap.error }
    : { status: 'ok', data, fetchedAt: cap.fetchedAt, serverAt: cap.serverAt }
}

/** `rejected`: refused, so nothing was created and the form can be corrected.
 *  `uncertain`: no answer we can trust, so the workflow may already exist. */
type Submission =
  | { kind: 'idle' | 'sending' }
  | { kind: 'rejected' | 'uncertain'; error: ApiError }
  | { kind: 'created'; workflow: Workflow; dispatch: DispatchEcho | null }

/**
 * `routes/workflows.create_workflow`'s `dispatch` block: what was ACCEPTED, not
 * what was sent. Null when the 201 carried none, which is an API older than the
 * field rather than a workflow that chose nothing -- the same distinction
 * `dispatchOf` keeps for a task.
 */
export interface DispatchEcho {
  strategy: string
  carrier: string
  integrator_step_id: string | null
}

function echoOf(raw: unknown): DispatchEcho | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const d = raw as Record<string, unknown>
  if (typeof d.strategy !== 'string' || typeof d.carrier !== 'string') return null
  return {
    strategy: d.strategy,
    carrier: d.carrier,
    integrator_step_id: typeof d.integrator_step_id === 'string' ? d.integrator_step_id : null,
  }
}

type Envelope = {
  code?: unknown
  message?: unknown
  detail?: unknown
  workflow?: unknown
  dispatch?: unknown
}
const KIND_BY_STATUS: Partial<Record<number, ApiErrorKind>> = {
  401: 'unauthenticated', 403: 'admin_required', 409: 'conflict',
  422: 'invalid', 429: 'rate_limited', 503: 'upstream_degraded',
}
const unsure = (kind: ApiErrorKind, httpStatus: number | null, message: string): Submission => ({ kind: 'uncertain', error: { kind, httpStatus, code: null, message } })

/** POST /v1/workflows. fetch.ts owns reads and has no write half yet. */
async function postWorkflow(body: unknown): Promise<Submission> {
  let res: Response
  try {
    res = await fetch('/v1/workflows', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    })
  } catch (e) {
    // It left and never came back. NOT a rejection: it may have been created and only the answer lost.
    return unsure('unreachable', null, e instanceof Error ? e.message : 'The request did not complete.')
  }
  // An expired IAP session is a 302 to a sign-in page that `fetch` follows, so it
  // lands as a 200 carrying HTML -- and on a POST it may have redirected either side
  // of the handler. Content-type is therefore checked first, as read() does.
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    return unsure('session_expired', res.status,
      'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.')
  }
  let env: Envelope | null = null
  try {
    const p: unknown = await res.json()
    if (typeof p === 'object' && p !== null) env = p as Envelope
  } catch { env = null }
  if (res.ok) {
    const wf = env?.workflow
    if (typeof wf === 'object' && wf !== null) {
      return { kind: 'created', workflow: wf as Workflow, dispatch: echoOf(env?.dispatch) }
    }
    // 201 with a body we could not read. It WAS created; we just cannot name it.
    return unsure('server_error', res.status, 'The workflow was accepted but its id could not be read.')
  }
  const mapped = KIND_BY_STATUS[res.status]
  const error: ApiError = {
    kind: mapped === undefined ? 'server_error' : mapped, httpStatus: res.status, detail: env?.detail,
    code: typeof env?.code === 'string' ? env.code : null,
    message: typeof env?.message === 'string' ? env.message : `The API returned HTTP ${res.status}.`,
  }
  // A 4xx carrying our own error envelope was decided by a handler, so nothing was
  // written. A 5xx was not: create_workflow writes the workflow and its tasks, and everything after can still throw.
  return res.status < 500 ? { kind: 'rejected', error } : { kind: 'uncertain', error }
}

interface StepDraft { key: number; stepId: string; profile: string; dependsOn: string[] }

export function SubmitWorkflowScreen() {
  return (
    <Screen title="Submit a workflow" load={loadSubmitForm}
      summary={(d) => `${d.profiles.length} runner profiles offered to this tenant`}
      empty={{ heading: 'No runner profiles', body: 'The catalogue read succeeded and named no runner profile. A caller may only name a profile from it, so there is nothing to submit until one is registered.' }}>
      {(d) => <Form sources={d} />}
    </Screen>
  )
}

function Form({ sources }: { sources: FormSources }) {
  const first = sources.profiles[0]
  const blank = (key: number): StepDraft => ({ key, stepId: '', profile: first ? first[0] : '', dependsOn: [] })
  const [steps, setSteps] = useState<StepDraft[]>([blank(1)])
  const [dispatch, setDispatch] = useState<DispatchDraft>({
    strategy: DEFAULT_STRATEGY,
    carrier: DEFAULT_CARRIER,
    repositoryUrl: '',
  })
  const [sub, setSub] = useState<Submission>({ kind: 'idle' })
  // Read from the payload, never a constant: an unread limit is not a limit of 50,
  // so `maxSteps` stays null and nothing here caps the form.
  const raw = sources.limits === null ? undefined : sources.limits.max_workflow_steps
  const maxSteps = typeof raw === 'number' ? raw : null
  // The only check made here: duplicate ids, cycles and unknown profiles are all named precisely by the API.
  const ids = steps.map((s) => s.stepId.trim())
  const unnamed = ids.some((id) => id === '')
  // The steps nothing else depends on. `integrate` opens ONE pull request and
  // `resolve_integrator_step` therefore requires exactly one of these, so this
  // is what lets the control NAME the step that would open it. It is a preview
  // only: nothing below is gated on it, matching this screen's standing rule
  // that the API decides the DAG and names its own refusal.
  const dependedOn = new Set(steps.flatMap((s) => s.dependsOn.filter((d) => ids.includes(d))))
  const terminals = ids.filter((id) => id !== '' && !dependedOn.has(id))
  const send = () => {
    setSub({ kind: 'sending' })
    const repo = dispatch.repositoryUrl.trim()
    // depends_on is filtered to ids that still exist: renaming a step after another
    // depends on it would otherwise send an edge to a step that is no longer here.
    void postWorkflow({
      steps: steps.map((s) => ({
        step_id: s.stepId.trim(), runner_profile: s.profile,
        depends_on: s.dependsOn.filter((d) => ids.includes(d)),
      })),
      // Workflow-level, not per step: `integrate` produces ONE pull request, so
      // "which repository" cannot be a per-step answer (schemas.WorkflowCreate).
      strategy: dispatch.strategy,
      carrier: dispatch.carrier,
      ...(repo === '' ? {} : { repository_url: repo }),
    }).then(setSub)
  }
  return (
    <>
      <Outcome sub={sub} />
      <section className="section panel">
        <h2>Steps<span className="count-chip">{steps.length}{maxSteps === null ? '' : ` of ${maxSteps}`}</span></h2>
        {steps.map((s, i) => (
          <StepRow key={s.key} step={s} profiles={sources.profiles} removable={steps.length > 1}
            // Self-dependency and a dependency on a step not in the workflow are
            // both rejected by validate_dag; not offering them beats explaining them.
            others={ids.filter((id, j) => id !== '' && j !== i)}
            onChange={(next) => setSteps(steps.map((o, j) => (j === i ? next : o)))}
            onRemove={() => setSteps(steps.filter((_, j) => j !== i))} />
        ))}
        <div className="filters" style={{ marginTop: 10 }}>
          <button disabled={maxSteps !== null && steps.length >= maxSteps}
            onClick={() => setSteps([...steps, blank(Math.max(...steps.map((s) => s.key)) + 1)])}>add step</button>
          <button disabled={unnamed || sub.kind === 'sending'} onClick={send}>
            {sub.kind === 'sending' ? 'submitting…' : 'submit workflow'}
          </button>
          {unnamed && <span className="warn-text">Every step needs an id: dependencies are declared by id, not by position.</span>}
        </div>
        {maxSteps === null && (
          <p className="warn-text">
            The step limit could not be read, so nothing caps this form and no number
            is guessed. {sources.limitsDetail} The API enforces its own and names it.
          </p>
        )}
      </section>

      <section className="section panel">
        <h2>What happens to the work</h2>
        {/* `steps.length` is passed live, so the pull-request count on each
            option moves as steps are added. That is the entire point: with six
            steps on the form, `direct-pr` reads "up to 6 pull requests" and
            `integrate` reads "exactly one", side by side, before anything is
            submitted. */}
        <DispatchChoice
          draft={dispatch}
          onChange={setDispatch}
          steps={steps.length}
          scale="workflow"
          terminals={terminals}
        />
      </section>
    </>
  )
}

function StepRow({ step, profiles, others, removable, onChange, onRemove }: {
  step: StepDraft; profiles: Array<[string, RunnerProfile]>; others: string[]
  removable: boolean; onChange: (next: StepDraft) => void; onRemove: () => void
}) {
  const chosen = profiles.find(([name]) => name === step.profile)
  return (
    <div className="row" style={{ display: 'block', paddingBottom: 10 }}>
      <div className="filters">
        <label>
          step id
          {/* No text-transform, no normalising: this is the id the DAG is built from, and the id a 422 names back. */}
          <input className="mono" value={step.stepId} spellCheck={false}
            onChange={(e) => onChange({ ...step, stepId: e.target.value })} />
        </label>
        <label>
          runner profile
          <select value={step.profile} onChange={(e) => onChange({ ...step, profile: e.target.value })}>
            {profiles.map(([name]) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        {/* UNITS, never "agents": admission increments every pool this step needs
            by its resource class's weight, so one large step costs four. */}
        {chosen && <span className="muted small">{chosen[1].resource_class} · {chosen[1].units} units · {chosen[1].backend}</span>}
        {removable && <button onClick={onRemove}>remove</button>}
      </div>
      <div className="filters">
        <span className="muted small">depends on</span>
        {others.length === 0 ? <span className="muted small">— no other named step yet</span> : others.map((id) => (
          <label key={id} className="check">
            <input type="checkbox" checked={step.dependsOn.includes(id)}
              onChange={(e) => onChange({ ...step, dependsOn: e.target.checked
                ? [...step.dependsOn, id] : step.dependsOn.filter((d) => d !== id) })} />
            <span className="mono ident">{id}</span>
          </label>
        ))}
      </div>
    </div>
  )
}

/**
 * WHAT THE API ACCEPTED, read off the 201 rather than off the form.
 *
 * `create_workflow` echoes the resolved dispatch precisely so a caller sees the
 * accepted values and not the sent ones -- and an `integrate` workflow is told
 * WHICH step will open its single pull request, which is the fact the form
 * could only preview. If that step is not the one the caller expected, this is
 * where it is cheap to find out.
 */
function Accepted({ echo, steps }: { echo: DispatchEcho | null; steps: number }) {
  if (echo === null) {
    return (
      <p className="warn-text">
        The 201 carried no dispatch block, so this screen cannot say what strategy
        was stored — an API older than the field. Open the workflow to read it off
        its tasks.
      </p>
    )
  }
  // The consequence sentence is only computed for a strategy this bundle knows.
  // A value it does not is printed as the API spelled it and nothing is claimed
  // about the outcome, which is the same rule `dispatchOf` follows.
  const known: DispatchStrategy | null =
    echo.strategy === 'collect' || echo.strategy === 'direct-pr' || echo.strategy === 'integrate'
      ? echo.strategy
      : null
  return (
    <p className="muted" style={{ marginTop: 8 }}>
      Accepted as <code>{echo.strategy}</code> / <code>{echo.carrier}</code>.{' '}
      {known === null
        ? 'This bundle does not recognise that strategy, so no outcome is claimed for it.'
        : consequenceOf(known, steps).headline}
      {echo.integrator_step_id !== null && (
        <>
          {' '}
          Step <code>{echo.integrator_step_id}</code> opens it.
        </>
      )}
    </p>
  )
}

function Outcome({ sub }: { sub: Submission }) {
  // `workflow.state` IS NOT SHOWN, here or anywhere. submit_workflow writes it once
  // as QUEUED and nothing in apps/scheduler, apps/reconciler or apps/agent-worker
  // writes that collection again, so a chip would read "queued" forever.
  if (sub.kind === 'created') {
    return (
      <div className="state" role="status">
        <h3>Workflow submitted</h3>
        <p>
          <span className="mono">{sub.workflow.workflow_id}</span> · {sub.workflow.steps.length} steps ·
          created {timeAgo(sub.workflow.created_at)}. No progress is shown here: a workflow's
          own state is written once at submission and never updated.
        </p>
        <Accepted echo={sub.dispatch} steps={sub.workflow.steps.length} />
      </div>
    )
  }
  if (sub.kind !== 'rejected' && sub.kind !== 'uncertain') return null
  const { error } = sub
  const uncertain = sub.kind === 'uncertain'
  // A 403 is information -- you may not submit -- not a broken platform, so it gets
  // the gate treatment rather than the red one, exactly as FailedPanel does.
  return (
    <div className={uncertain ? 'state partial' : error.kind === 'admin_required' ? 'state admin-gate' : 'state failed'} role="status">
      <h3>{uncertain ? 'We cannot say whether that workflow was created' : errorHeading(error)}</h3>
      {/* VERBATIM: the cycle the server named is the only part that says where to
          look, and "invalid DAG" would throw it away. */}
      <p>{error.message}</p>
      {/* The server's own detail, unedited: `{"cycle": [...]}` from a DagError, the
          per-field list for a schema failure (whose message is only ever the generic
          "request body failed validation"). Raw, so no pattern is restated here. */}
      {error.detail !== undefined && <pre>{JSON.stringify(error.detail, null, 1)}</pre>}
      <p>{uncertain
        ? 'The request left this browser without a usable answer, so the workflow may exist. Open the Workflows board and look before submitting again — resubmitting blind is how a workflow runs twice.'
        : 'Nothing was created. Correct the steps below and submit again.'}</p>
      {error.httpStatus !== null && <p className="checked-at">HTTP {error.httpStatus}{error.code ? ` · ${error.code}` : ''}</p>}
    </div>
  )
}
