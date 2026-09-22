import type { ReactNode } from 'react'
import { HelpCard } from './HelpCard'
import {
  CARRIER_LABEL,
  CARRIER_NOTE,
  DISPATCH_CARRIERS,
  DISPATCH_STRATEGIES,
  STRATEGY_LABEL,
  consequenceOf,
  dispatchOf,
  needsRepository,
  type DispatchCarrier,
  type DispatchStrategy,
  type Task,
  type TaskDispatch,
} from './types'

/**
 * The dispatch control, and the read-back of what a dispatch chose.
 *
 * ONE FILE FOR BOTH HALVES ON PURPOSE. The words a caller reads when they pick
 * `integrate` and the words an operator reads three days later asking why that
 * run produced one pull request instead of six have to be the same words. They
 * were written apart once already, in the worker and in swarm-api, and the
 * vocabularies drifted (`patches` vs `checkpoints`); a second copy of the
 * consequence text would drift the same way and nothing would notice.
 *
 * WHAT THIS CONTROL IS FOR. Not offering three words -- making the CONSEQUENCE
 * of the three words visible at the moment of choosing. `integrate` over six
 * steps is one pull request; `direct-pr` over the same six is six. That number
 * is computed from the steps actually on the form, recomputed as steps are
 * added, and shown on the option itself rather than in help text nobody opens.
 *
 * `collect` is the default and it PUSHES NOTHING. A caller must not be able to
 * leave this screen believing a pull request is coming, so the default option
 * says "no pull request" in the place where the other two say how many.
 */

// ---------------------------------------------------------------------------
// Choosing
// ---------------------------------------------------------------------------

export interface DispatchDraft {
  strategy: DispatchStrategy
  carrier: DispatchCarrier
  repositoryUrl: string
}

export interface DispatchChoiceProps {
  draft: DispatchDraft
  onChange: (next: DispatchDraft) => void
  /**
   * How many steps this dispatch has. 1 for a standalone task; the live step
   * count for a workflow, so the pull-request number on each option moves as
   * steps are added.
   */
  steps: number
  /**
   * "task" or "workflow", exactly as `resolve_dispatch_options` means it.
   * `integrate` names a FINAL STEP that receives the others' patches, so at
   * task scale the API refuses it -- and this control says so on the option
   * rather than hiding it, because a missing option is a question nobody can
   * answer.
   */
  scale: 'task' | 'workflow'
  /**
   * The step ids nothing else depends on, for a workflow. `integrate` opens ONE
   * pull request, so exactly one step may be final. Supplying this lets the
   * option name the step that will open it.
   *
   * A PREVIEW, NOT A CHECK: `resolve_integrator_step` decides, and SubmitWorkflow's
   * standing rule is that no DAG reasoning here may gate a submission. Nothing
   * below disables anything; the worst a wrong preview does is show a caution.
   */
  terminals?: string[]
  /** 422 text the API attributed to these fields, if any. */
  errors?: { strategy?: string; carrier?: string; repository_url?: string }
}

export function DispatchChoice({
  draft,
  onChange,
  steps,
  scale,
  terminals,
  errors,
}: DispatchChoiceProps) {
  const set = (patch: Partial<DispatchDraft>) => onChange({ ...draft, ...patch })
  const repoRequired = needsRepository(draft.strategy, draft.carrier)
  const repoMissing = repoRequired && draft.repositoryUrl.trim() === ''

  return (
    <fieldset className="dsp">
      <legend className="t-label">
        how this work gets merged
        <HelpCard topic="dispatch-strategies" />
      </legend>

      <div className="dsp-options" role="radiogroup" aria-label="dispatch strategy">
        {DISPATCH_STRATEGIES.map((s) => {
          const c = consequenceOf(s, steps)
          // The API refuses `integrate` outside a workflow. The option stays
          // visible and carries the refusal's own reasoning, because a caller
          // who came looking for "one pull request" needs to be told where it
          // lives, not shown two options and left to guess.
          const unavailable = s === 'integrate' && scale === 'task'
          return (
            <label
              key={s}
              className={`dsp-option${draft.strategy === s ? ' is-on' : ''}${unavailable ? ' is-off' : ''}`}
            >
              <input
                type="radio"
                name="dispatch-strategy"
                value={s}
                checked={draft.strategy === s}
                disabled={unavailable}
                onChange={() => set({ strategy: s })}
              />
              <span className="dsp-option-body">
                <span className="dsp-option-head">
                  <b>{STRATEGY_LABEL[s]}</b>
                  <code className="dsp-code">{s}</code>
                  {s === 'collect' && <span className="dsp-default">default</span>}
                </span>
                {/* THE CONSEQUENCE, on the option, in numbers. */}
                <span className={`dsp-count${c.pushes ? '' : ' is-none'}`}>{c.headline}</span>
                {unavailable && (
                  <span className="dsp-off-why">
                    Not available for a single task
                    <HelpCard topic="integrate-needs-final-step" />
                  </span>
                )}
              </span>
            </label>
          )
        })}
      </div>

      <Consequence
        strategy={draft.strategy}
        steps={steps}
        scale={scale}
        terminals={terminals}
      />
      {errors?.strategy && <p className="warn-text" role="alert">{errors.strategy}</p>}

      <label className="t-label" htmlFor="dsp-carrier" style={{ marginTop: 14 }}>
        what carries work between steps
        <HelpCard topic="dispatch-carrier" />
      </label>
      <select
        id="dsp-carrier"
        className="mono"
        value={draft.carrier}
        onChange={(e) => set({ carrier: e.target.value as DispatchCarrier })}
      >
        {DISPATCH_CARRIERS.map((c) => (
          <option key={c} value={c}>
            {c} — {CARRIER_LABEL[c]}
          </option>
        ))}
      </select>
      {/* Said on every carrier, not only on `branches`: the control records a
          preference the platform does not act on yet, and a caller is entitled
          to know that before they choose one. `CARRIER_DETAIL` -- what the
          carrier would mean if something read it -- moved to
          `#help/dispatch-carrier`; this did not, and must not. */}
      <p className="warn-text">{CARRIER_NOTE}</p>
      {errors?.carrier && <p className="warn-text" role="alert">{errors.carrier}</p>}

      <label className="t-label" htmlFor="dsp-repo" style={{ marginTop: 14 }}>
        repository url {repoRequired ? '(required by this choice)' : '(optional)'}
        <HelpCard topic="repository-url" />
      </label>
      <input
        id="dsp-repo"
        className="mono dsp-repo"
        spellCheck={false}
        placeholder="https://github.com/owner/repo.git"
        value={draft.repositoryUrl}
        onChange={(e) => set({ repositoryUrl: e.target.value })}
      />
      {repoMissing && (
        // A warning, never a block. The API owns this rule
        // (`DispatchOptions.needs_repository`) and names its own refusal; this
        // copy exists so the refusal is not a surprise, and if it ever drifts
        // it shows a wrong caution rather than stopping a valid submission.
        <p className="warn-text" role="alert">
          {draft.strategy === 'collect'
            ? `carrier ${draft.carrier} has to push, so the API will refuse this without a repository URL.`
            : `strategy ${draft.strategy} ends in a pull request, so the API will refuse this without a repository URL.`}
        </p>
      )}
      {errors?.repository_url && <p className="warn-text" role="alert">{errors.repository_url}</p>}
    </fieldset>
  )
}

/** The selected strategy, spelled out in full underneath the options. */
function Consequence({
  strategy,
  steps,
  scale,
  terminals,
}: {
  strategy: DispatchStrategy
  steps: number
  scale: 'task' | 'workflow'
  terminals?: string[]
}) {
  const c = consequenceOf(strategy, steps)
  return (
    <div className={`dsp-consequence${c.pushes ? '' : ' is-none'}`} role="status">
      <p className="dsp-consequence-head">{c.headline}</p>
      {strategy === 'integrate' && scale === 'workflow' && terminals !== undefined && (
        <IntegratorPreview terminals={terminals} steps={steps} />
      )}
    </div>
  )
}

/**
 * Which step opens the one pull request, as the form is drawn right now.
 *
 * `resolve_integrator_step` picks the workflow's single sink and refuses a graph
 * with two. Naming that step here is what turns "one pull request" from a claim
 * into something a caller can check -- and when the graph has two sinks it says
 * so, because the alternative is a caller who reads "exactly one pull request",
 * submits, and gets a 422 whose sentence arrives with no context.
 */
function IntegratorPreview({ terminals, steps }: { terminals: string[]; steps: number }) {
  if (steps < 2) return null
  if (terminals.length === 1) {
    return (
      <p className="muted small">
        As drawn, <code>{terminals[0]}</code> is the only step nothing depends on, so it
        is the step that would open it.
      </p>
    )
  }
  if (terminals.length === 0) {
    // Only reachable from a cyclic graph, which validate_dag rejects first and
    // names precisely. Not diagnosed here.
    return (
      <p className="warn-text">
        As drawn, no step is final — every step is depended on by another. The API
        validates the graph and will say exactly what is wrong.
      </p>
    )
  }
  return (
    <p className="warn-text">
      As drawn, {terminals.length} steps are final ({terminals.join(', ')}). One pull
      request needs exactly one final step, so the API will refuse this until the
      integrating step depends on the others.
    </p>
  )
}

// ---------------------------------------------------------------------------
// Reading it back
// ---------------------------------------------------------------------------

/**
 * WHY THIS RUN PRODUCED A PULL REQUEST, OR DID NOT -- shown wherever a task is.
 *
 * `null` from `dispatchOf` means the API did not report a dispatch, which is a
 * deployment older than the field rather than a caller who chose `collect`.
 * Those render differently here for the same reason a failed read never renders
 * as empty data anywhere else in this app.
 */
export function DispatchFacts({ task }: { task: Task }) {
  const d = dispatchOf(task)
  if (d === null) return <DispatchUnreported />
  const c = consequenceOf(d.strategy, 1)

  return (
    <dl className="kv dsp-facts">
      <dt>Strategy</dt>
      <dd>
        <code>{d.strategy}</code>{' '}
        <span className="muted small">{strategyOutcome(d)}</span>
      </dd>

      <dt>Carrier</dt>
      <dd>
        <code>{d.carrier}</code> <span className="muted small">{CARRIER_NOTE}</span>
      </dd>

      {d.role !== null && (
        <>
          <dt>Role</dt>
          <dd>
            <code>{d.role}</code>{' '}
            <span className="muted small">
              {d.role === 'integrator'
                ? 'This step merges the other steps’ branches and opens the workflow’s single pull request.'
                : 'This step pushes its branch and opens nothing; the integrator merges it.'}
            </span>
          </dd>
        </>
      )}

      {d.integrates.length > 0 && (
        <>
          <dt>Integrates</dt>
          <dd>
            {/* The task ids, in the order their patches must be applied --
                `integrates` is the workflow's topological prefix, not a set. */}
            <span className="mono">{d.integrates.join(', ')}</span>
            <span className="muted small">
              {' '}
              · {d.integrates.length} upstream task
              {d.integrates.length === 1 ? '' : 's'}, in the order they are applied
            </span>
          </dd>
        </>
      )}

      {!c.pushes && d.role === null && (
        <>
          <dt>Published</dt>
          <dd className="muted">
            Nothing, by request. The patch is in this task&rsquo;s artifacts.
          </dd>
        </>
      )}
    </dl>
  )
}

function strategyOutcome(d: TaskDispatch): string {
  switch (d.strategy) {
    case 'collect':
      return 'The patch was harvested into this task’s artifacts and nothing was pushed.'
    case 'direct-pr':
      return 'This task pushes its own branch and opens its own pull request.'
    case 'integrate':
      return d.role === 'integrator'
        ? 'One pull request for the whole workflow, opened by this step.'
        : 'One pull request for the whole workflow, opened by a later step.'
  }
}

function DispatchUnreported() {
  return (
    <div className="ctl-empty is-partial" role="status">
      {/* THE HEADING IS THE MARKER. "Did not report" and "reported that it
          published nothing" are two different facts, and the panel exists so
          they never render alike. Why an absence means an old deployment
          rather than a caller's choice is the topic. */}
      <h3>
        This API did not report a dispatch
        <HelpCard topic="dispatch-absent-is-old-api" />
      </h3>
      <p>Nothing here says what this run would have published.</p>
    </div>
  )
}

/**
 * The one-line form, for a table row.
 *
 * Renders NOTHING when the dispatch is the default and carries no role. A chip
 * on every row saying "collect" is noise that trains an eye to skip the column,
 * and the column exists for the rows that differ. An unreported dispatch is not
 * silent though: it gets a chip of its own, because "we did not ask" and "we
 * were not told" must not look alike.
 */
export function DispatchChip({ task }: { task: Task }): ReactNode {
  const d = dispatchOf(task)
  if (d === null) {
    return (
      <span className="tag unknown" title="This API did not report a dispatch for this task.">
        dispatch?
      </span>
    )
  }
  if (d.strategy === 'collect' && d.role === null) return null
  const title =
    d.role === 'integrator'
      ? `strategy ${d.strategy}: this step opens the workflow's single pull request`
      : d.role === 'contributor'
        ? `strategy ${d.strategy}: this step pushes a branch and opens no pull request`
        : `strategy ${d.strategy}: this task opens its own pull request`
  return (
    <span className={`tag ${d.role === 'contributor' ? 'wait' : 'ok'}`} title={title}>
      {d.role === null ? d.strategy : `${d.strategy}/${d.role}`}
    </span>
  )
}

/**
 * A workflow's dispatch, rolled up from the tasks its steps created.
 *
 * `codec.workflow_dispatch` does exactly this server-side for
 * `GET /v1/workflows/{id}`, because the frozen `Workflow` dataclass has no
 * metadata field to store it on. The board screen has the task join already, so
 * it rolls up the same way rather than fetching every workflow individually.
 *
 * Returns null when the join produced no task to read, which is NOT the same as
 * `collect`: it means the states banner is already explaining that the task read
 * failed, and inventing a strategy under it would be the failure that banner
 * exists to prevent.
 */
export function workflowDispatchOf(
  tasks: readonly Task[],
): { dispatch: TaskDispatch; integratorTaskId: string | null } | null {
  let found: TaskDispatch | null = null
  let integratorTaskId: string | null = null
  for (const t of tasks) {
    const d = dispatchOf(t)
    if (d === null) continue
    if (found === null) found = d
    if (d.role === 'integrator') {
      integratorTaskId = t.id
      // Keep the integrator's own block: it is the one carrying `integrates`.
      found = d
      break
    }
  }
  return found === null ? null : { dispatch: found, integratorTaskId }
}
