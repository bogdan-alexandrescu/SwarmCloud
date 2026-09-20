import { useState } from 'react'
import { loadCapacity, loadStats } from './api'
import { errorHeading, type ApiError, type ApiErrorKind, type Result } from './fetch'
import { Screen, timeAgo } from './Shell'
import type { RunnerProfile, Workflow } from './types'

/**
 * Submit a multi-step workflow. The only WRITE screen in this UI, which changes
 * two things about how it is built.
 *
 * THERE IS NO CLIENT-SIDE DAG CHECK. `validate_dag` in
 * apps/swarm-api/swarm_api/validation.py finds one concrete cycle and names it
 * -- "workflow dependency graph contains a cycle: build -> test -> build" --
 * and that sentence is the entire value of the 422. A second implementation
 * here would be a TypeScript restatement of server logic that
 * check-contract-parity.sh does not cover, so it would drift silently and
 * start disagreeing with the thing that actually decides. The server is the
 * authority; this screen's job is to show its answer word for word.
 *
 * AND A FAILED POST IS NOT A FAILED GET. A refused request created nothing; a
 * request that never came back may have created everything. Those are two
 * different renderings, and the ambiguous one never offers a retry button --
 * a blind resubmit is how a workflow runs twice.
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

  // The catalogue IS the form: a caller picks a runner_profile by name, so a
  // failed read here must not degrade into a text box to type one into.
  if (cap.status === 'loading' || cap.status === 'empty' || cap.status === 'error') return cap

  const profiles = Object.entries(cap.data.runner_profiles).sort((a, b) => a[0].localeCompare(b[0]))
  // loadCapacity's own emptiness test is `pools.length === 0`, so pools plus an
  // empty catalogue arrives here as `ok`. Still nothing submittable, and still
  // a real zero rather than a failure.
  if (profiles.length === 0) return { status: 'empty', fetchedAt: cap.fetchedAt }

  // The step limit is the server's number. A failed stats read means we do not
  // know it -- not that it is 50 -- so in that case nothing caps the form.
  const limits = stats.status === 'ok' || stats.status === 'stale' ? stats.data.limits : null
  const data: FormSources = {
    profiles,
    limits,
    limitsDetail: stats.status === 'error' ? stats.error.message : 'The stats read returned no limits.',
  }
  return cap.status === 'stale'
    ? { status: 'stale', data, fetchedAt: cap.fetchedAt, error: cap.error }
    : { status: 'ok', data, fetchedAt: cap.fetchedAt, serverAt: cap.serverAt }
}

/**
 * `rejected` means the API understood the request and refused it: nothing was
 * created and the form can be corrected. `uncertain` means no answer we can
 * trust came back, so the workflow may already exist.
 */
type Submission =
  | { kind: 'idle' | 'sending' }
  | { kind: 'rejected' | 'uncertain'; error: ApiError }
  | { kind: 'created'; workflow: Workflow }

const KIND_BY_STATUS: Partial<Record<number, ApiErrorKind>> = {
  401: 'unauthenticated', 403: 'admin_required', 409: 'conflict',
  422: 'invalid', 429: 'rate_limited', 503: 'upstream_degraded',
}

/** POST /v1/workflows. fetch.ts owns reads and has no write half yet. */
async function postWorkflow(body: unknown): Promise<Submission> {
  const fail = (kind: ApiErrorKind, message: string, httpStatus: number | null): ApiError =>
    ({ kind, httpStatus, code: null, message })

  let res: Response
  try {
    res = await fetch('/v1/workflows', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    })
  } catch (e) {
    // The request left and never came back. NOT a rejection: it may have been
    // created and the answer lost on the way home.
    const why = e instanceof Error ? e.message : 'The request did not complete.'
    return { kind: 'uncertain', error: fail('unreachable', why, null) }
  }

  // An expired IAP session is a 302 to a sign-in page, which `fetch` follows,
  // so it lands as a 200 carrying HTML -- and on a POST the redirect may have
  // happened before or after the handler ran. Content-type first, as read() does.
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    return { kind: 'uncertain', error: fail('session_expired',
      'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.',
      res.status) }
  }

  type Envelope = { code?: unknown; message?: unknown; detail?: unknown; workflow?: unknown }
  let env: Envelope | null = null
  try {
    const parsed: unknown = await res.json()
    if (typeof parsed === 'object' && parsed !== null) env = parsed as Envelope
  } catch {
    env = null
  }

  if (res.ok) {
    const wf = env?.workflow
    if (typeof wf === 'object' && wf !== null) return { kind: 'created', workflow: wf as Workflow }
    // 201 with a body we could not read. It WAS created; we simply cannot name
    // it, which is not the same as a failure to create it.
    return { kind: 'uncertain', error: fail('server_error',
      'The workflow was accepted but its id could not be read from the response.', res.status) }
  }

  const mapped = KIND_BY_STATUS[res.status]
  const error: ApiError = {
    kind: mapped === undefined ? 'server_error' : mapped,
    httpStatus: res.status,
    code: typeof env?.code === 'string' ? env.code : null,
    message: typeof env?.message === 'string' ? env.message : `The API returned HTTP ${res.status}.`,
    detail: env?.detail,
  }
  // A 4xx carrying our own error envelope was decided by a handler, so nothing
  // was written. A 5xx was not: create_workflow writes the workflow and its
  // tasks in one call, and anything after that can still throw.
  return res.status < 500 ? { kind: 'rejected', error } : { kind: 'uncertain', error }
}

interface StepDraft { key: number; stepId: string; profile: string; dependsOn: string[] }

export function SubmitWorkflowScreen() {
  return (
    <Screen
      title="Submit a workflow"
      load={loadSubmitForm}
      summary={(d) => `${d.profiles.length} runner profiles offered to this tenant`}
      empty={{
        heading: 'No runner profiles',
        body: 'The catalogue read succeeded and named no runner profile. A caller may only name a profile from it, so there is nothing to submit until one is registered.',
      }}
    >
      {(d) => <Form sources={d} />}
    </Screen>
  )
}

function Form({ sources }: { sources: FormSources }) {
  const first = sources.profiles[0]
  const blank = (key: number): StepDraft =>
    ({ key, stepId: '', profile: first ? first[0] : '', dependsOn: [] })
  const [steps, setSteps] = useState<StepDraft[]>([blank(1)])
  const [onFailure, setOnFailure] = useState('fail_workflow')
  const [sub, setSub] = useState<Submission>({ kind: 'idle' })

  // Read from the payload, never a constant. Index access on a Record is
  // `number | undefined` under noUncheckedIndexedAccess, and that undefined is
  // the case that matters: an unread limit is not a limit of 50.
  const raw = sources.limits === null ? undefined : sources.limits.max_workflow_steps
  const maxSteps = typeof raw === 'number' ? raw : null

  const ids = steps.map((s) => s.stepId.trim())
  const blocked = ids.some((id) => id === '')
    ? 'Every step needs a step id: dependencies are declared by that id, not by position.'
    : ids.some((id, i) => ids.indexOf(id) !== i)
      ? 'Two steps share a step id, so a dependency on it is ambiguous here before the API ever sees it.'
      : null

  return (
    <>
      {sub.kind === 'created' && <Created workflow={sub.workflow} />}
      {(sub.kind === 'rejected' || sub.kind === 'uncertain') && (
        <Refused kind={sub.kind} error={sub.error} />
      )}

      <section className="section panel">
        <h2>
          Steps
          <span className="count-chip">
            {steps.length}{maxSteps === null ? '' : ` of ${maxSteps}`}
          </span>
        </h2>

        {steps.map((s, i) => (
          <StepRow
            key={s.key}
            step={s}
            profiles={sources.profiles}
            // Self-dependency and a dependency on a step that is not in the
            // workflow are both rejected by validate_dag. Not offering them
            // beats explaining them afterwards.
            others={ids.filter((id, j) => id !== '' && j !== i)}
            removable={steps.length > 1}
            onChange={(next) => setSteps(steps.map((o, j) => (j === i ? next : o)))}
            onRemove={() => setSteps(steps.filter((_, j) => j !== i))}
          />
        ))}

        <div className="filters" style={{ marginTop: 10 }}>
          <button
            disabled={maxSteps !== null && steps.length >= maxSteps}
            onClick={() => setSteps([...steps, blank(Math.max(...steps.map((s) => s.key)) + 1)])}
          >
            add step
          </button>
          <label>
            on step failure
            <select value={onFailure} onChange={(e) => setOnFailure(e.target.value)}>
              <option value="fail_workflow">fail_workflow</option>
              <option value="continue">continue</option>
            </select>
          </label>
          <button
            disabled={blocked !== null || sub.kind === 'sending'}
            onClick={() => {
              setSub({ kind: 'sending' })
              void postWorkflow({
                on_step_failure: onFailure,
                steps: steps.map((s) => ({
                  step_id: s.stepId.trim(),
                  runner_profile: s.profile,
                  // Filtered to ids that still exist: renaming a step after
                  // another depends on it would otherwise send a dependency on
                  // a step that is no longer in the workflow.
                  depends_on: s.dependsOn.filter((d) => ids.includes(d)),
                })),
              }).then(setSub)
            }}
          >
            {sub.kind === 'sending' ? 'submitting…' : 'submit workflow'}
          </button>
        </div>

        {blocked !== null && <p className="warn-text">{blocked}</p>}
        {maxSteps === null && (
          <p className="warn-text">
            The step limit could not be read, so nothing here caps this form and
            no number is guessed. {sources.limitsDetail} The API still enforces
            its own limit and names it if you go over.
          </p>
        )}
      </section>
    </>
  )
}

function StepRow({ step, profiles, others, removable, onChange, onRemove }: {
  step: StepDraft
  profiles: Array<[string, RunnerProfile]>
  others: string[]
  removable: boolean
  onChange: (next: StepDraft) => void
  onRemove: () => void
}) {
  const chosen = profiles.find(([name]) => name === step.profile)
  return (
    <div className="row" style={{ display: 'block', paddingBottom: 10 }}>
      <div className="filters">
        <label>
          step id
          {/* No text-transform and no normalising. What is typed here is the id
              the DAG is built from and the id a 422 will name back; a displayed
              id that differs from the real one is unusable. */}
          <input className="mono" value={step.stepId} spellCheck={false}
            onChange={(e) => onChange({ ...step, stepId: e.target.value })} />
        </label>
        <label>
          runner profile
          <select value={step.profile} onChange={(e) => onChange({ ...step, profile: e.target.value })}>
            {profiles.map(([name]) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        {/* UNITS, never "agents": admission increments every pool this step
            needs by its resource class's weight, so one large step costs four. */}
        {chosen && (
          <span className="muted small">
            {chosen[1].resource_class} · {chosen[1].units} units · {chosen[1].backend}
          </span>
        )}
        {removable && <button onClick={onRemove}>remove</button>}
      </div>

      <div className="filters">
        <span className="muted small">depends on</span>
        {others.length === 0 ? (
          <span className="muted small">— no other named step yet</span>
        ) : others.map((id) => (
          <label key={id} className="check">
            <input type="checkbox" checked={step.dependsOn.includes(id)}
              onChange={(e) => onChange({
                ...step,
                dependsOn: e.target.checked
                  ? [...step.dependsOn, id]
                  : step.dependsOn.filter((d) => d !== id),
              })} />
            <span className="mono">{id}</span>
          </label>
        ))}
      </div>
    </div>
  )
}

function Created({ workflow }: { workflow: Workflow }) {
  return (
    <div className="state" role="status">
      <h3>Workflow submitted</h3>
      <p>
        <span className="mono">{workflow.workflow_id}</span> · {workflow.steps.length} steps ·
        created {timeAgo(workflow.created_at)}.
      </p>
      {/* `workflow.state` IS NOT SHOWN. submit_workflow writes it once as
          QUEUED and nothing in apps/scheduler, apps/reconciler or
          apps/agent-worker writes the workflows collection again, so a state
          chip here would read "queued" for the life of the workflow --
          including long after every step finished. */}
      <p className="muted small">
        No progress is shown here: a workflow's own state field is written once
        at submission and never updated. The Workflows board derives each step's
        state from the task that step created.
      </p>
    </div>
  )
}

function Refused({ kind, error }: { kind: 'rejected' | 'uncertain'; error: ApiError }) {
  const uncertain = kind === 'uncertain'
  return (
    <div className={uncertain ? 'state partial' : 'state failed'} role="status">
      <h3>{uncertain ? 'We cannot say whether that workflow was created' : errorHeading(error)}</h3>
      {/* VERBATIM. A cycle arrives as "workflow dependency graph contains a
          cycle: build -> test -> build"; condensing that to "invalid DAG"
          throws away the only part of it that says where to look. */}
      <p>{error.message}</p>
      {/* The server's own detail, unedited: `{"cycle": [...]}` for a DagError,
          and the per-field list for a schema failure, whose message is only
          ever the generic "request body failed validation". Printing it raw is
          why this screen does not restate step_id's pattern and drift from it. */}
      {error.detail !== undefined && <pre>{JSON.stringify(error.detail, null, 1)}</pre>}
      <p>
        {uncertain
          ? 'The request left this browser without a usable answer, so the workflow may exist. Open the Workflows board and look before submitting again — resubmitting blind is how a workflow runs twice.'
          : 'Nothing was created. Correct the steps below and submit again.'}
      </p>
      {error.httpStatus !== null && (
        <p className="checked-at">
          HTTP {error.httpStatus}{error.code ? ` · ${error.code}` : ''}
        </p>
      )}
    </div>
  )
}
