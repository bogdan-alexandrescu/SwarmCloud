import { useState } from 'react'
import { loadCapacity, loadStats } from './api'
import { DispatchChoice, type DispatchDraft } from './Dispatch'
import { errorHeading, type ApiError, type ApiErrorKind, type Result } from './fetch'
import { Screen, timeAgo } from './Shell'
import {
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  consequenceOf,
  requiredInputKeys,
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

/** One reason this form refused to send, attributed to the step that caused it.
 *  `stepId` is `''` for a step that has not been named yet. */
interface StepProblem { stepId: string; message: string }

/** `not_sent`: this browser refused; NOTHING left, so there is nothing to check for.
 *  `rejected`: the API refused, so nothing was created and the form can be corrected.
 *  `uncertain`: no answer we can trust, so the workflow may already exist. */
type Submission =
  | { kind: 'idle' | 'sending' }
  | { kind: 'not_sent'; problems: StepProblem[] }
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

interface StepDraft {
  key: number
  stepId: string
  profile: string
  dependsOn: string[]
  /** RAW TEXT, not a parsed object -- the same choice `Submit.tsx` makes.
   *  A half-typed JSON object has to survive a keystroke, and parsing on every
   *  change would delete the character that made it invalid. */
  input: string
  /** upstream step_id -> artifact filename. Keyed by step id and NOT by the
   *  dependency's position, so reordering or removing a step cannot silently
   *  re-point a filename at a different upstream. */
  inputFrom: Record<string, string>
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

type ParsedInput =
  | { ok: true; input: Record<string, unknown> }
  | { ok: false; message: string }

/**
 * One step's `input`, parsed exactly as `Submit.tsx` parses the single-agent
 * form's: blank means `{}`, and anything that is not a JSON OBJECT is refused
 * here rather than sent -- `WorkflowStepCreate.input` is `dict[str, Any]`, so a
 * bare array or number is a 422 about a type the caller cannot see.
 */
function parseStepInput(text: string): ParsedInput {
  try {
    const parsed: unknown = JSON.parse(text.trim() === '' ? '{}' : text)
    if (!isRecord(parsed)) {
      return { ok: false, message: 'the API stores input as a JSON object, so this must be one' }
    }
    return { ok: true, input: parsed }
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : 'this is not JSON' }
  }
}

/**
 * The keys this step's runner demands and this step's input does not carry.
 *
 * The test is the RUNNER's, restated from `run_cli_agent`: present, a string,
 * and not blank. A looser one here would pass `{"prompt": ""}` through to the
 * identical failure, which is the whole class of defect this control exists to
 * close -- the screen accepted it, the platform accepted it, and the agent
 * refused it minutes later having already spent a slot and mounted a credential.
 */
function missingInputKeys(input: Record<string, unknown>, required: string[]): string[] {
  return required.filter((key) => {
    const value = input[key]
    return typeof value !== 'string' || value.trim() === ''
  })
}

/**
 * `input_from`, narrowed to the dependencies that still exist.
 *
 * `validate_dag` refuses a source that is not also a dependency -- "an artifact
 * cannot be staged from a step that may not have run yet" -- so the map is
 * built FROM the filtered `depends_on` rather than filtered afterwards. A
 * filename typed against a dependency that was later unchecked is kept in the
 * draft and simply not sent, so re-checking it restores what was typed.
 */
function stagedArtifacts(step: StepDraft, dependsOn: string[]): Record<string, string> {
  const staged: Record<string, string> = {}
  for (const source of dependsOn) {
    const filename = (step.inputFrom[source] ?? '').trim()
    if (filename !== '') staged[source] = filename
  }
  return staged
}

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
  const byName = new Map<string, RunnerProfile>(sources.profiles)
  // `'{}'` and not `''`, so the textarea shows the shape the field takes before
  // anything is typed into it. `Submit.tsx` seeds its own the same way.
  const blank = (key: number): StepDraft =>
    ({ key, stepId: '', profile: first ? first[0] : '', dependsOn: [], input: '{}', inputFrom: {} })
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
    // BUILT AND CHECKED BEFORE ANYTHING IS SENT. Every problem found here is a
    // workflow that would have been accepted by the API and then failed at the
    // agent, one step at a time, having spent a slot on each -- so the refusal
    // is worth more than the submission. Every step is checked, not just the
    // first bad one: fixing them one round-trip at a time is the same wait.
    const problems: StepProblem[] = []
    const body: Array<Record<string, unknown>> = []
    for (const s of steps) {
      const stepId = s.stepId.trim()
      const parsed = parseStepInput(s.input)
      if (!parsed.ok) {
        // Labelled "Not sent", as `Submit.tsx` labels its own: a refusal phrased
        // like the API's sends someone looking at the platform for a typo that
        // is in this textarea.
        problems.push({ stepId, message: `Not sent — ${parsed.message}.` })
        continue
      }
      // null means THIS API DID NOT SAY which keys the runner demands, which is
      // not the same as demanding none. Nothing is checked in that case and the
      // form says so above; inventing a rule here would refuse valid workflows.
      const required = requiredInputKeys(byName.get(s.profile))
      const missing = required === null ? [] : missingInputKeys(parsed.input, required)
      if (missing.length > 0) {
        problems.push({
          stepId,
          message:
            // The KEYS are named rather than the word "prompt": `required_keys`
            // is a list the API sends, and a message that hardcoded one of its
            // values would start lying the first time a runner demanded another.
            `Not sent — ${s.profile} refuses an attempt whose input has no ` +
            `${missing.map((k) => `"${k}"`).join(' and no ')}. Add ` +
            `${missing.map((k) => `"${k}": "…"`).join(', ')} to this step's input. ` +
            'The runner raises that refusal only once the step has been dispatched ' +
            'and given a credential, so it costs a slot and a wait to discover.',
        })
        continue
      }
      // depends_on is filtered to ids that still exist: renaming a step after another
      // depends on it would otherwise send an edge to a step that is no longer here.
      const dependsOn = s.dependsOn.filter((d) => ids.includes(d))
      const staged = stagedArtifacts(s, dependsOn)
      body.push({
        step_id: stepId,
        runner_profile: s.profile,
        // SENT ALWAYS, including when it is `{}`. `WorkflowStepCreate.input` is
        // `Field(default_factory=dict)`, so an omitted `input` is an accepted
        // workflow whose every agent step fails -- the defect this screen had.
        // An empty object here is a caller who chose it, not a form that forgot.
        input: parsed.input,
        depends_on: dependsOn,
        // Omitted when empty, unlike `input`: an empty `input_from` is exactly
        // the default and stages nothing, whereas an empty `input` is a payload
        // the runner still has to read.
        ...(Object.keys(staged).length === 0 ? {} : { input_from: staged }),
      })
    }
    if (problems.length > 0) {
      setSub({ kind: 'not_sent', problems })
      return
    }
    setSub({ kind: 'sending' })
    const repo = dispatch.repositoryUrl.trim()
    void postWorkflow({
      steps: body,
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
            required={requiredInputKeys(byName.get(s.profile))}
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

function StepRow({ step, profiles, others, removable, required, onChange, onRemove }: {
  step: StepDraft; profiles: Array<[string, RunnerProfile]>; others: string[]
  /** What this step's runner refuses to start without, or null when this API
   *  did not say. Computed once by the form so both the live warning here and
   *  the refusal in `send` read the same answer. */
  required: string[] | null
  removable: boolean; onChange: (next: StepDraft) => void; onRemove: () => void
}) {
  const chosen = profiles.find(([name]) => name === step.profile)
  // Live, so a missing prompt is visible while it is still being typed rather
  // than only on the click that would have submitted it. The refusal in `send`
  // is still the thing that stops the submission -- this only stops the reader
  // being surprised by it.
  const parsed = parseStepInput(step.input)
  const missing = parsed.ok && required !== null ? missingInputKeys(parsed.input, required) : []
  // ONLY the dependencies. `validate_dag` refuses an `input_from` source that
  // is not also a dependency, so offering one would be offering a 422 -- and
  // worse, the workflow it describes stages a file from a step that may not
  // have run. Unchecking a dependency removes its row and nothing else.
  const stageable = step.dependsOn.filter((d) => others.includes(d))
  const inputId = `wf-step-input-${step.key}`
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
            <span className="mono">{id}</span>
          </label>
        ))}
      </div>
      {/* THE FIELD WHOSE ABSENCE MADE EVERY WORKFLOW FROM THIS SCREEN FAIL.
          Same control and same error handling as the single-agent form
          (`Submit.tsx`), deliberately: two idioms for one field is how the two
          halves drifted far enough apart for one of them to lose it entirely. */}
      <label className="t-label" htmlFor={inputId}>input (JSON object)</label>
      <textarea id={inputId} className="mono" rows={4} style={{ width: '100%' }} value={step.input}
        spellCheck={false} onChange={(e) => onChange({ ...step, input: e.target.value })} />
      <p className="muted small">
        Opaque to the platform: handed to this step's agent, validated only for size.
        {required !== null && required.length > 0 && (
          <> <span className="mono">{step.profile}</span> reads{' '}
            <code>{required.map((k) => `input.${k}`).join(', ')}</code> and refuses the
            attempt without it.</>
        )}
        {required === null && ' This API did not say which keys this profile requires, so nothing is checked here.'}
      </p>
      {!parsed.ok && <p className="warn-text" role="alert">Not sent — {parsed.message}.</p>}
      {missing.length > 0 && (
        <p className="warn-text" role="alert">
          {step.profile} requires <code>{missing.map((k) => `input.${k}`).join(', ')}</code> as a
          non-empty string. Submitting without it produces a step that is dispatched, given a
          credential, and then fails — so this form will not send it.
        </p>
      )}
      <div className="filters">
        <span className="muted small">stage an artifact from</span>
        {stageable.length === 0
          ? <span className="muted small">— depend on a step first; an artifact cannot be staged from one that may not have run</span>
          : stageable.map((id) => (
            <label key={id}>
              <span className="mono">{id}</span>
              {/* The filename as the UPSTREAM step wrote it into SWARM_ARTIFACTS_DIR.
                  It arrives in this step's workspace under the same name. Blank
                  means nothing is staged from that step. */}
              <input className="mono" value={step.inputFrom[id] ?? ''} spellCheck={false}
                placeholder="artifact filename"
                onChange={(e) => onChange({ ...step, inputFrom: { ...step.inputFrom, [id]: e.target.value } })} />
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
  // REFUSED HERE, so the "it may exist anyway" warning below must not appear:
  // nothing left this browser and the Workflows board has nothing new on it.
  if (sub.kind === 'not_sent') {
    return (
      <div className="state failed" role="status">
        <h3>Not submitted: {sub.problems.length === 1 ? 'a step' : `${sub.problems.length} steps`} would have failed</h3>
        <p>
          Nothing was sent, so nothing was created. Each step below was refused here rather
          than submitted, because the API would have accepted it and the agent would then
          have refused it — after the step was dispatched and a credential was mounted.
        </p>
        <ul>
          {sub.problems.map((p, i) => (
            <li key={`${p.stepId}-${i}`}>
              <span className="mono">{p.stepId === '' ? '(unnamed step)' : p.stepId}</span> — {p.message}
            </li>
          ))}
        </ul>
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
