import { Fragment, useState } from 'react'
import { loadCapacity, loadStats } from './api'
import { DispatchChoice, type DispatchDraft } from './Dispatch'
import { errorHeading, type ApiError, type ApiErrorKind, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { Screen, timeAgo } from './Shell'
import {
  InputFields,
  Move,
  buildInput,
  inputFieldId,
  missingRequired,
  seedFields,
  type InputField,
} from './Submit'
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
 * Build a multi-step workflow -- the only WRITE screen in this UI besides
 * `Submit.tsx`, and the one the owner's verdict hit hardest.
 *
 * WHAT WAS WRONG WAS THE MODEL, NOT THE STYLING. The old form was a flat list
 * of steps. Each step had a `step id` TEXT BOX that had to be filled in before
 * anything else on the screen worked; dependencies were checkboxes over the
 * ids you had already typed; and the `input` was a `<textarea class="mono">`
 * whose label read `input (JSON object)` and whose value started as `{}`. On a
 * first visit that produced three dead ends stacked on top of each other --
 * "Every step needs an id.", "— no other named step yet", "— depend on a step
 * first" -- and the only way out of all three was to type an identifier whose
 * only job was to be typed again later.
 *
 * SO THE MODEL IS STAGES, AND IDS ARE GENERATED. A workflow is a column of
 * stages; a stage holds the steps that run at the same time. "These three run
 * in parallel, then this one" is two gestures -- add three steps to a stage,
 * add a stage -- and no id is ever typed. Ids are still SHOWN and still
 * editable, because the API's refusals name them ("workflow dependency graph
 * contains a cycle: build -> test -> build") and a reader has to be able to
 * find the step a message is about.
 *
 * THE DAG IS STILL FULLY EXPRESSIBLE. A stage is the DEFAULT for
 * `depends_on`, not a replacement for it: every step can narrow its own
 * dependencies to any subset of the steps in earlier stages, from a list of
 * generated names. Any DAG can be layered topologically, so nothing the API
 * accepts has become unreachable from here -- the common shape just costs
 * nothing.
 *
 * NO CLIENT-SIDE DAG CHECK, unchanged. `validate_dag`
 * (swarm_api/validation.py) names one concrete cycle and that sentence is the
 * entire value of the 422. A copy of it here would restate server logic
 * check-contract-parity.sh cannot check. Stages cannot produce a cycle in the
 * first place, and a narrowed dependency can only ever point backwards.
 *
 * AND A FAILED POST IS NOT A FAILED GET: a refused request created nothing, a
 * request that never came back may have created everything. Two renderings,
 * and the ambiguous one offers no retry -- a blind resubmit runs it twice.
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

/** One reason this form will not send, attributed to the step that causes it.
 *  `key` is the step's React identity, which -- unlike its name -- exists and
 *  is unique even for the two problems that are ABOUT the name. `missing` is
 *  the required keys with nothing in them, as the API named them, so the send
 *  panel can take a reader to the first one's field (TS-15). */
interface StepProblem {
  key: number
  stepId: string
  kind: 'name' | 'input' | 'required'
  message: string
  missing?: string[]
}

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
 * field rather than a workflow that chose nothing.
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

/* ==========================================================================
   THE PLAN
   ========================================================================== */

interface StepDraft {
  /** React identity and nothing else; never sent. */
  key: number
  /** The `step_id` the API sees. GENERATED from the profile, editable, never
   *  a prerequisite for anything else on the screen working. */
  id: string
  profile: string
  /** Which parallel band this runs in. 0 is "starts immediately". */
  stage: number
  /** `null` -- the default -- means "every step in the previous stage". A list
   *  narrows it to a chosen subset of the steps in EARLIER stages. Nothing can
   *  point sideways or forwards, so nothing here can make a cycle. */
  after: string[] | null
  input: InputField[]
  /** upstream step_id -> artifact filename. Keyed by step id and NOT by the
   *  dependency's position, so reordering or removing a step cannot silently
   *  re-point a filename at a different upstream. */
  from: Record<string, string>
}

/** A name nothing else in the plan is using. The profile is the stem because
 *  it is the one word about a step a reader already knows. */
function autoId(profile: string, taken: Set<string>): string {
  const stem = profile.trim() === '' ? 'step' : profile.trim()
  for (let i = 1; i < 999; i++) {
    const candidate = `${stem}-${i}`
    if (!taken.has(candidate)) return candidate
  }
  return `${stem}-${taken.size + 1}`
}

/** What this step actually waits for, resolved. */
function dependsOf(step: StepDraft, steps: StepDraft[]): string[] {
  const earlier = steps.filter((s) => s.stage < step.stage).map((s) => s.id)
  if (step.after === null) return steps.filter((s) => s.stage === step.stage - 1).map((s) => s.id)
  // Filtered against the plan as it stands now: a narrowed dependency whose
  // step was deleted or moved into this stage is silently unwired rather than
  // sent as an edge to a step that is no longer upstream.
  return step.after.filter((id) => earlier.includes(id))
}

/** The id a step's name box carries, so the send panel can take a reader to it. */
const stepNameId = (key: number) => `wfb-${key}-name`

/** The field-id prefix of a step's input editor; see `inputFieldId`. */
const stepFields = (key: number) => `wf${key}`

/**
 * THE PLAN AS IT WOULD BE SENT, AND EVERY REASON IT WOULD NOT BE.
 *
 * Built on every render, not on the click. The problems used to be found only
 * inside `send`, and shown in a panel at the TOP of the build column -- about
 * 2,100px above the button at 390 -- so the click looked like it had done
 * nothing, while every step still read `Not sent` and the button stayed
 * enabled. Now the same pass drives the button (disabled while any problem
 * stands), the panel's heading, and the count beside the button with a way to
 * each step; `send` re-runs it rather than trusting the last render.
 *
 * Every problem found here is a workflow that would have been accepted by the
 * API and then failed at the agent, one step at a time, having spent a slot on
 * each. Every step is checked, not just the first bad one: fixing them one
 * round-trip at a time is the same wait.
 *
 * THESE ARE LIVE MESSAGES, SO NONE OF THEM SAYS "Not sent" (TS-15). That
 * phrase is the result of a click -- the `not_sent` outcome, whose heading
 * already says nothing was submitted -- and it stood in front of every problem
 * on every step before anything had been clicked at all.
 */
function planOf(
  steps: StepDraft[],
  byName: ReadonlyMap<string, RunnerProfile>,
): { problems: StepProblem[]; body: Array<Record<string, unknown>> } {
  const problems: StepProblem[] = []
  const body: Array<Record<string, unknown>> = []
  const seen = new Set<string>()
  for (const s of steps) {
    const id = s.id.trim()
    if (id === '') { problems.push({ key: s.key, stepId: id, kind: 'name', message: 'this step has no name' }); continue }
    if (seen.has(id)) { problems.push({ key: s.key, stepId: id, kind: 'name', message: 'two steps are called this' }); continue }
    seen.add(id)
    const built = buildInput(s.input)
    if (!built.ok) {
      // Worded as this form's own finding, not as the API's: a refusal
      // phrased like the API's sends someone looking at the platform for a
      // mistake that is on this screen.
      problems.push({ key: s.key, stepId: id, kind: 'input', message: built.message })
      continue
    }
    // null means THIS API DID NOT SAY which keys the runner demands, which is
    // not the same as demanding none. Nothing is checked in that case and the
    // step says so; inventing a rule here would refuse valid workflows.
    const missing = missingRequired(s.input, requiredInputKeys(byName.get(s.profile)))
    if (missing.length > 0) {
      problems.push({
        key: s.key,
        stepId: id,
        kind: 'required',
        // The KEYS are named rather than the word "prompt": `required_keys`
        // is a list the API sends, and a message that hardcoded one of its
        // values would start lying the first time a runner demanded another.
        // As the API named them, without its `input.` prefix or its "as a
        // non-empty string" (TS-15).
        message: `${s.profile} will not start without ${missing.join(', ')}`,
        missing,
      })
      continue
    }
    const depends = dependsOf(s, steps)
    const staged: Record<string, string> = {}
    for (const source of depends) {
      const filename = (s.from[source] ?? '').trim()
      if (filename !== '') staged[source] = filename
    }
    body.push({
      step_id: id,
      runner_profile: s.profile,
      // SENT ALWAYS, including when it is `{}`. `WorkflowStepCreate.input` is
      // `Field(default_factory=dict)`, so an omitted `input` is an accepted
      // workflow whose every agent step fails -- the defect this screen had.
      // An empty object here is a caller who chose it, not a form that forgot.
      input: built.input,
      depends_on: depends,
      // Omitted when empty, unlike `input`: an empty `input_from` is exactly
      // the default and stages nothing, whereas an empty `input` is a payload
      // the runner still has to read.
      ...(Object.keys(staged).length === 0 ? {} : { input_from: staged }),
    })
  }
  return { problems, body }
}

export function SubmitWorkflowScreen() {
  return (
    // ONE SENTENCE IN THE EMPTY STATE (§6.9).
    <Screen title="Submit a workflow" load={loadSubmitForm}
      summary={(d) => `${d.profiles.length} runner profiles offered to this tenant`}
      empty={{ heading: 'No runner profiles', body: 'The catalogue read succeeded and named no runner profile.' }}>
      {(d) => <Form sources={d} />}
    </Screen>
  )
}

function Form({ sources }: { sources: FormSources }) {
  const byName = new Map<string, RunnerProfile>(sources.profiles)
  // `?? true` and not `|| true`: an older API omits `available`, and `false ||
  // true` is true, which would offer a profile we know the API would refuse.
  const offered = sources.profiles.filter(([, p]) => (p.available ?? true) !== false)
  // `loadSubmitForm` returns `empty` for a zero-length catalogue, so
  // `sources.profiles[0]` exists. The fallback is for the case where every
  // profile in a non-empty catalogue is disabled: the form still has to name a
  // profile in its first step rather than render a step with no runner.
  const firstProfile = (offered[0] ?? sources.profiles[0])?.[0] ?? ''
  const [nextKey, setNextKey] = useState(2)
  const [steps, setSteps] = useState<StepDraft[]>(() => [{
    key: 1, id: `${firstProfile}-1`, profile: firstProfile, stage: 0,
    after: null, input: seedFields([], requiredInputKeys(byName.get(firstProfile))), from: {},
  }])
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

  const stageCount = steps.reduce((m, s) => Math.max(m, s.stage + 1), 1)
  const stages = Array.from({ length: stageCount }, (_, i) => steps.filter((s) => s.stage === i))
  const atCeiling = maxSteps !== null && steps.length >= maxSteps

  const addStep = (stage: number) => {
    const taken = new Set(steps.map((s) => s.id))
    setSteps([...steps, {
      key: nextKey, id: autoId(firstProfile, taken), profile: firstProfile, stage,
      after: null, input: seedFields([], requiredInputKeys(byName.get(firstProfile))), from: {},
    }])
    setNextKey(nextKey + 1)
  }
  const patch = (key: number, next: StepDraft) => setSteps(steps.map((s) => (s.key === key ? next : s)))
  const drop = (key: number) => {
    const left = steps.filter((s) => s.key !== key)
    // Close the gap a removed stage leaves, so "stage 3" never sits under
    // "stage 1" with an empty band between them.
    const live = Array.from(new Set(left.map((s) => s.stage))).sort((a, b) => a - b)
    setSteps(left.map((s) => ({ ...s, stage: live.indexOf(s.stage) })))
  }

  // The steps nothing else depends on. `integrate` opens ONE pull request and
  // `resolve_integrator_step` therefore requires exactly one of these, so this
  // is what lets the control NAME the step that would open it. A preview only:
  // nothing below is gated on it, matching this screen's standing rule that the
  // API decides the DAG and names its own refusal.
  const dependedOn = new Set(steps.flatMap((s) => dependsOf(s, steps)))
  const terminals = steps.map((s) => s.id).filter((id) => !dependedOn.has(id))

  // THE LIVE PLAN. What disables the button, titles the send panel and lists
  // the steps that stop it -- the same pass `send` makes, so what the panel
  // says and what the click does cannot disagree.
  const plan = planOf(steps, byName)
  const blocked = plan.problems.length > 0
  // TO WHAT HAS TO CHANGE. A step whose problem is a required key goes to
  // that key's field rather than to the step's name (TS-15); a name problem,
  // or input that will not build, goes to the name as before.
  const toProblem = (p: StepProblem) => {
    const first = p.missing?.[0]
    if (first !== undefined) {
      const field = steps.find((s) => s.key === p.key)?.input.find((f) => f.name.trim() === first)
      const el = field === undefined ? null : document.getElementById(inputFieldId(stepFields(p.key), field.key))
      if (el !== null) {
        el.focus()
        return
      }
    }
    document.getElementById(stepNameId(p.key))?.focus()
  }

  const send = () => {
    // RE-BUILT AT THE CLICK, and still refused before anything is sent. The
    // button is disabled while a problem stands, so this branch should not be
    // reachable; it stays because "the form refuses before it posts" is the
    // invariant, and a disabled attribute is only one way of keeping it.
    const { problems, body } = planOf(steps, byName)
    if (problems.length > 0) { setSub({ kind: 'not_sent', problems }); return }
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
    <div className="sbf">
      <div className="sbf-build">
        <Outcome sub={sub} />

        {/* THIS SCREEN'S ONE `?` (B7.4). Invariant 10 is the rule that shapes
            every control below it -- a caller names a runtime and supplies no
            image, no command, no resource spec and no backend -- and it is a
            rule about what this form is NOT ALLOWED to ask for, which a form
            cannot state by labelling what it does ask for. It sits on the first
            step because that is where the reader meets the constraint. */}
        <Move n={1} title="Lay out the plan" aside={<HelpCard topic="runner-profile-by-name" />}>
          {/* STAGES, NOT A LIST. Everything in one band runs at the same time;
              the next band waits for it. That is the whole dependency model a
              reader needs for the common shape, and it is expressed by WHERE a
              step is rather than by what its neighbours are called. */}
          {/* "WAITS FOR" IS SAID ONCE PER STEP, ON THE STEP (TS-20). It was
              said three times over one step: on the stage header ("waits for
              everything above"), on the add-a-stage button, and on the step's
              own disclosure -- and only the last one is the control that
              changes it. So a later stage's header reads `then`, plus how many
              steps run together when there are two or more. */}
          {stages.map((inStage, i) => (
            <div className="wfb-stage" key={i}>
              <div className="wfb-stage-h">
                <span className="wfb-stage-n">{i === 0 ? 'first' : `then`}</span>
                {i === 0 ? (
                  <span className="wfb-stage-say">
                    {inStage.length === 1 ? 'starts immediately' : `${inStage.length} steps start together`}
                  </span>
                ) : inStage.length > 1 && (
                  <span className="wfb-stage-say">{inStage.length} steps run together</span>
                )}
              </div>
              <div className="wfb-steps">
                {inStage.map((s) => (
                  <StepCard key={s.key} step={s} steps={steps} profiles={offered}
                    required={requiredInputKeys(byName.get(s.profile))}
                    nameProblem={plan.problems.find((p) => p.key === s.key && p.kind === 'name')?.message ?? null}
                    removable={steps.length > 1}
                    onChange={(next) => patch(s.key, next)} onRemove={() => drop(s.key)} />
                ))}
                <button type="button" className="wfb-add" disabled={atCeiling} onClick={() => addStep(i)}>
                  add a step here <span className="wfb-add-say">runs alongside</span>
                </button>
              </div>
            </div>
          ))}
          <button type="button" className="wfb-add is-stage" disabled={atCeiling} onClick={() => addStep(stageCount)}>
            add a stage
          </button>
          {atCeiling && maxSteps !== null && (
            <p className="warn-text">This tenant&apos;s limit is {maxSteps} steps.</p>
          )}
          {/* AN UNREAD LIMIT IS NOT AN ABSENT LIMIT, and it is drawn as one:
              the step counter beside the heading says `N` with no `of M`, and
              this line carries the mark for why. */}
          {maxSteps === null && (
            <p className="ctl-panel-note"
              aria-label={`The step limit could not be read, so nothing caps this form and no number is guessed. ${sources.limitsDetail} The API enforces its own limit and names it.`}>
              <i className="ctl-mark is-unread">not read</i>
              step limit · nothing caps this form
              <span className="ctl-panel-note-detail">{sources.limitsDetail}</span>
            </p>
          )}
        </Move>

        {/* `steps.length` is passed live, so the pull-request count on each
            option moves as steps are added. That is the entire point: with six
            steps on the form, `direct-pr` reads "up to 6 pull requests" and
            `integrate` reads "exactly one", side by side, before anything is
            submitted. */}
        <Move n={2} title="Choose what happens to the work">
          <DispatchChoice
            draft={dispatch}
            onChange={setDispatch}
            steps={steps.length}
            scale="workflow"
            terminals={terminals}
          />
        </Move>
      </div>

      <aside className="sbf-side">
        <div className="sbf-send">
          {/* From the live plan, like the button: this said `Ready to send`
              over a plan with three steps reading `Not sent`. */}
          <h2>{blocked ? 'Not ready to send' : 'Ready to send'}</h2>
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>steps</b>
              {steps.length}{maxSteps === null ? '' : ` of ${maxSteps}`}
            </li>
            <li className="ctl-fact">
              <b>stages</b>
              {stageCount}
            </li>
            <li className="ctl-fact">
              <b>result</b>
              {dispatch.strategy}
            </li>
          </ul>
          {/* THE COUNT, BESIDE THE BUTTON IT DISABLES, AND A WAY TO EACH STEP.
              The step's own card says what is wrong with it; this says which
              steps, and takes a reader to one -- on a phone the cards are a
              long scroll above. Each is a button that focuses the step's name,
              not an in-page anchor: a hash is a ROUTE in this app, and
              following one would leave the form. */}
          {blocked && (
            <p className="warn-text" role="status">
              {plan.problems.length === 1 ? '1 step' : `${plan.problems.length} steps`} not ready:{' '}
              {plan.problems.map((p, i) => (
                <Fragment key={p.key}>
                  {i > 0 ? ', ' : ''}
                  <button type="button" className="sbf-mini" title={p.message} onClick={() => toProblem(p)}>
                    {p.stepId === '' ? '(unnamed step)' : p.stepId}
                  </button>
                </Fragment>
              ))}
            </p>
          )}
          <button type="button" className="sbf-go" disabled={sub.kind === 'sending' || blocked} onClick={send}>
            {sub.kind === 'sending' ? 'Submitting…' : 'Submit this workflow'}
          </button>
        </div>
      </aside>
    </div>
  )
}

function StepCard({ step, steps, profiles, required, nameProblem, removable, onChange, onRemove }: {
  step: StepDraft
  steps: StepDraft[]
  profiles: Array<[string, RunnerProfile]>
  /** What this step's runner refuses to start without, or null when this API
   *  did not say. Computed by the form so the live warning here and the
   *  refusal in `send` read the same answer. */
  required: string[] | null
  /** Why this step's NAME stops the plan -- blank, or taken by another step --
   *  from the form's live plan. The name is the one problem only the whole
   *  plan can see, so the card is told it rather than working it out. */
  nameProblem: string | null
  removable: boolean
  onChange: (next: StepDraft) => void
  onRemove: () => void
}) {
  const [open, setOpen] = useState(false)
  const chosen = profiles.find(([name]) => name === step.profile)
  const depends = dependsOf(step, steps)
  const built = buildInput(step.input)
  // Every step in an EARLIER stage. Offering a step in the same stage or a
  // later one would be offering a 422, and worse, a workflow that stages a
  // file from a step that may not have run.
  const upstream = steps.filter((s) => s.stage < step.stage).map((s) => s.id)

  const retitle = (profile: string) => onChange({
    ...step, profile,
    // The id follows the profile ONLY while it is still the generated one.
    // A name somebody typed is theirs and survives a profile change.
    id: /^[a-z0-9-]+-\d+$/.test(step.id) && step.id.startsWith(`${step.profile}-`)
      ? autoId(profile, new Set(steps.filter((s) => s.key !== step.key).map((s) => s.id)))
      : step.id,
    input: seedFields(step.input, requiredInputKeys(profiles.find(([n]) => n === profile)?.[1])),
  })

  return (
    <div className="wfb-step">
      <div className="wfb-step-h">
        {/* The id is SHOWN because the API's refusals name it, and editable
            because someone may want a word that means something. It is never
            a prerequisite: it already has a value. */}
        <input id={stepNameId(step.key)} className="mono wfb-id" value={step.id} spellCheck={false} aria-label="step name"
          aria-invalid={nameProblem !== null || undefined}
          onChange={(e) => onChange({ ...step, id: e.target.value })} />
        <select className="wfb-profile" aria-label={`${step.id} runner profile`} value={step.profile}
          onChange={(e) => retitle(e.target.value)}>
          {/* Only what the API would accept. Offering a disabled profile and
              then refusing it on submit makes the form the liar. */}
          {profiles.map(([name]) => <option key={name} value={name}>{name}</option>)}
        </select>
        {removable && (
          <button type="button" className="sbf-mini wfb-drop" onClick={onRemove}>remove</button>
        )}
      </div>
      {/* UNITS, never "agents": admission increments every pool this step needs
          by its resource class's weight, so one large step costs four. */}
      {/* AND NO `?` (B7.4). The line below prints the class, then the weight
          with the word `unit` on it, then the backend -- all three read from
          the response. The word on the figure is the whole of what
          `units-not-agents` was here to say about a step's cost, so the glyph
          repeated the line it sat under. No example is spelled out in this
          comment: the class names are the frozen catalogue's and this screen
          restates none of them. */}
      {chosen && (
        <p className="wfb-cost">
          {chosen[1].resource_class} · {chosen[1].units} unit{chosen[1].units === 1 ? '' : 's'} · {chosen[1].backend}
        </p>
      )}

      {/* A MISSING REQUIRED KEY IS SAID AT ITS FIELD, by `InputFields`, once
          the field has been left (TS-15). This card's own copy of it -- "Not
          sent -- claude-code requires input.prompt as a non-empty string",
          before anything had been sent -- is deleted rather than kept
          agreeing. */}
      <InputFields profile={step.profile} fields={step.input} required={required}
        idPrefix={stepFields(step.key)} onChange={(input) => onChange({ ...step, input })} />

      {nameProblem !== null && <p className="warn-text" role="alert">{nameProblem}</p>}
      {!built.ok && <p className="warn-text" role="alert">{built.message}</p>}

      {/* THE TWO ADVANCED CONTROLS, BEHIND THE ANSWER THEY ALREADY HAVE.
          Both used to be open rows saying "— no other named step yet" and "—
          depend on a step first" on a form where nothing could yet be either.
          The summary line states what IS true; opening it is for changing it. */}
      {step.stage > 0 && (
        <details className="wfb-more" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
          {/* CLOSED, THE SUMMARY IS THE ANSWER; OPEN, IT IS THE QUESTION. The
              two read the same sentence otherwise -- the summary said "waits
              for everything in the stage above" directly above a checkbox
              labelled "everything in the stage above", which is one fact
              rendered twice and neither of them obviously the control. */}
          <summary>
            {open ? 'choose what this waits for' : <>
              waits for{' '}
              {step.after === null
                ? <span className="wfb-dep">everything in the stage above</span>
                : depends.length === 0
                  ? <span className="wfb-dep">nothing — it starts with the first stage</span>
                  : <span className="wfb-dep mono">{depends.join(', ')}</span>}
            </>}
          </summary>
          <div className="wfb-deps">
            <label className="check">
              <input type="checkbox" checked={step.after === null}
                onChange={(e) => onChange({ ...step, after: e.target.checked ? null : depends })} />
              <span>everything in the stage above</span>
            </label>
            {step.after !== null && upstream.map((id) => (
              <label className="check" key={id}>
                <input type="checkbox" checked={step.after?.includes(id) ?? false}
                  onChange={(e) => onChange({ ...step, after: e.target.checked
                    ? [...(step.after ?? []), id]
                    : (step.after ?? []).filter((d) => d !== id) })} />
                <span className="mono">{id}</span>
              </label>
            ))}
          </div>
          {depends.length > 0 && (
            <div className="wfb-stage-from">
              <p className="t-label">stage a file from a step it waits for</p>
              {depends.map((id) => (
                <label className="wfb-from" key={id}>
                  <span className="mono">{id}</span>
                  {/* The filename as the UPSTREAM step wrote it into
                      SWARM_ARTIFACTS_DIR. It arrives in this step's workspace
                      under the same name. Blank stages nothing. */}
                  <input className="mono" value={step.from[id] ?? ''} spellCheck={false}
                    placeholder="artifact filename"
                    onChange={(e) => onChange({ ...step, from: { ...step.from, [id]: e.target.value } })} />
                </label>
              ))}
            </div>
          )}
        </details>
      )}
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
    // AN ABSENT ECHO IS NOT A CHOSEN DEFAULT, and the mark is what says so.
    return (
      <p
        className="ctl-panel-note"
        aria-label="The 201 carried no dispatch block, so this screen cannot say what strategy was stored. That is an API older than the field. Open the workflow to read it off its tasks."
      >
        <i className="ctl-mark is-absent">not measured</i>
        strategy not echoed by the API
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
    <ul className="ctl-facts">
      <li className="ctl-fact">
        <b>accepted as</b>
        <code>{echo.strategy}</code> / <code>{echo.carrier}</code>
      </li>
      <li className={known === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
        <b>outcome</b>
        {known === null ? (
          // A STRATEGY THIS BUNDLE DOES NOT KNOW IS NOT A STRATEGY WITH NO
          // OUTCOME. Same rule as every other absence on these screens: an em
          // dash and a mark, never a claim.
          <>
            <i className="ctl-em">&mdash;</i>
            <i className="ctl-mark is-absent">not measured</i>
          </>
        ) : (
          consequenceOf(known, steps).headline
        )}
      </li>
      {echo.integrator_step_id !== null && (
        <li className="ctl-fact">
          <b>opened by</b>
          <code>{echo.integrator_step_id}</code>
        </li>
      )}
    </ul>
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
        <ul className="ctl-facts">
          <li className="ctl-fact">
            <b>id</b>
            <span className="mono">{sub.workflow.workflow_id}</span>
          </li>
          <li className="ctl-fact">
            <b>steps</b>
            {sub.workflow.steps.length}
          </li>
          <li className="ctl-fact">
            <b>created</b>
            {timeAgo(sub.workflow.created_at)}
          </li>
          <li
            className="ctl-fact is-absent"
            aria-label="No progress is shown here: a workflow's own state is written once at submission and never updated."
          >
            <b>progress</b>
            <i className="ctl-em">&mdash;</i>
          </li>
        </ul>
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
        {/* THE ONE SENTENCE IS THE INVARIANT. "Nothing was sent, so nothing
            was created" is what stops someone opening the Workflows board to
            look for a workflow that is not there, and no encoding carries it
            -- an absence of a side effect has nothing to attach a mark to. */}
        <p>Nothing was sent, so nothing was created.</p>
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
      <h3>
        {uncertain ? (
          // THE MARK IS THE CLAIM. `partial` is dashed on one edge -- the
          // shape of a hole -- and this outcome is exactly a hole: the request
          // left and the answer did not come back, so the workflow may or may
          // not exist. A reader who sees only the shape still knows not to
          // resubmit.
          <>
            <i className="ctl-mark is-partial">partial</i> We cannot say whether
            that workflow was created
          </>
        ) : (
          errorHeading(error)
        )}
      </h3>
      {/* VERBATIM: the cycle the server named is the only part that says where to
          look, and "invalid DAG" would throw it away. */}
      <p>{error.message}</p>
      {/* The server's own detail, unedited: `{"cycle": [...]}` from a DagError, the
          per-field list for a schema failure (whose message is only ever the generic
          "request body failed validation"). Raw, so no pattern is restated here. */}
      {error.detail !== undefined && <pre>{JSON.stringify(error.detail, null, 1)}</pre>}
      {/* ONE SENTENCE EACH, AND BOTH ARE THE INVARIANT RATHER THAN AN
          EXPLANATION OF IT: whether anything was created. */}
      <p>{uncertain
        ? 'The workflow may exist. Open the Workflows board and look before submitting again.'
        : 'Nothing was created. Correct the steps below and submit again.'}</p>
      {error.httpStatus !== null && <p className="checked-at">HTTP {error.httpStatus}{error.code ? ` · ${error.code}` : ''}</p>}
    </div>
  )
}
