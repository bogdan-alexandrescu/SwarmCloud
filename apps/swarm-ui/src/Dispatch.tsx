import type { ReactNode } from 'react'
import { helpAnchor, type TopicId } from './help'
import {
  CARRIER_NOTE,
  DISPATCH_CARRIERS,
  DISPATCH_STRATEGIES,
  STRATEGY_LABEL,
  TERMINAL_STATES,
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
   * task scale the API refuses it. The task form therefore does not offer it,
   * and says in one line where one pull request for several steps lives
   * (TS-14): a missing option with nothing said is a question nobody can
   * answer, and a disabled card drawn dashed read as a broken read.
   *
   * The CARRIER is a workflow question too -- "what carries work between
   * steps" has no answer when there are no steps -- so it is asked only here
   * at workflow scale (TS-6). A task still SENDS the default carrier, as it
   * always did; it just is not asked for one.
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

/**
 * The prefixes `repository_url` must start with: `check_repository_url` in
 * `swarm_api/validation.py`, which `TaskCreate` and `WorkflowCreate` both call.
 * (It also refuses a URL carrying a credential in its userinfo, since the PR
 * #229 review; this form does not pre-warn on that, and the API's 422 names
 * the tenant's git secret as the way to clone a private repository.)
 *
 * A SECOND COPY OF A SERVER RULE, KEPT FOR ONE REASON AND HELD TO THE FIRST.
 * The form only ever WARNS with it -- the API owns the refusal and names it --
 * so a drift here shows a wrong caution rather than blocking a valid submission.
 * `dispatch.test.ts` reads the validator's tuple out of `validation.py` and
 * fails when the two disagree, so the drift is caught rather than shipped.
 */
export const REPOSITORY_SCHEMES: readonly string[] = ['https://', 'ssh://', 'git@']

/**
 * Whether a repository URL, as the form will send it, is one the API's scheme
 * check refuses. Blank is not: a blank field is omitted from the request, and
 * whether this choice REQUIRES one is the other warning's job.
 */
export function repositorySchemeRefused(url: string): boolean {
  const sent = url.trim()
  return sent !== '' && !REPOSITORY_SCHEMES.some((p) => sent.startsWith(p))
}

/** Where the carrier's caveat is argued. Typed, so a renamed topic fails to build. */
const CARRIER_HELP: TopicId = 'dispatch-carrier'

/**
 * The route the single-task form points to for one pull request over several
 * steps. The rail does not show this tab at 390 (TS-14), which is why the line
 * carries a link rather than naming a place to go and look.
 */
const WORKFLOW_FORM = '#work/new-workflow'

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
  const repoRefused = repositorySchemeRefused(draft.repositoryUrl)

  // THE STRATEGIES THIS SCALE CAN TAKE, and no others (TS-14). A task cannot
  // integrate -- there is no final step to receive anyone's patches -- so at
  // task scale `integrate` is not drawn at all, rather than drawn disabled.
  const offered: readonly DispatchStrategy[] =
    scale === 'task' ? DISPATCH_STRATEGIES.filter((s) => s !== 'integrate') : DISPATCH_STRATEGIES

  return (
    <fieldset className="dsp">
      {/* NO `?` ON ANY CONTROL IN THIS FIELDSET (B7.4), AND THE REASON IS THAT
          THIS COMPONENT IS NOT A SCREEN.
          It is drawn inside Submit, inside Submit a workflow and inside the
          agent detail, so its five glyphs arrived on three screens that each
          keep exactly one of their own -- five of the console's eighty-two came
          from this one file. Every one of them explained the control it sat on,
          and every one of those controls already says what it does: the legends
          are sentences (`how this work gets merged`, `what carries work between
          steps`), each strategy prints its own consequence in numbers beside
          it, the task form says in one line where the strategy it cannot take
          lives, and the repository field says in its own label whether the
          current choice requires it. The carrier's `not acted on yet` is the one
          thing no label could carry -- that the control records a preference
          nothing acts on -- and it is printed as a plain line with a link to
          `#help/dispatch-carrier`, with `CARRIER_NOTE` as its accessible name. */}
      <legend className="t-label">how this work gets merged</legend>

      <div className="dsp-options" role="radiogroup" aria-label="dispatch strategy">
        {offered.map((s) => {
          const c = consequenceOf(s, steps)
          return (
            <label key={s} className={`dsp-option${draft.strategy === s ? ' is-on' : ''}`}>
              <input
                type="radio"
                name="dispatch-strategy"
                value={s}
                checked={draft.strategy === s}
                onChange={() => set({ strategy: s })}
              />
              <span className="dsp-option-body">
                <span className="dsp-option-head">
                  <b>{STRATEGY_LABEL[s]}</b>
                  <code className="dsp-code">{s}</code>
                  {s === 'collect' && <span className="dsp-default">default</span>}
                </span>
                {/* THE CONSEQUENCE, on the option, in numbers. It is the only
                    place it is said: the box under the options no longer
                    repeats it (TS-20). */}
                <span className={`dsp-count${c.pushes ? '' : ' is-none'}`}>{c.headline}</span>
              </span>
            </label>
          )
        })}
      </div>
      {scale === 'task' && (
        // WHERE ONE PULL REQUEST FOR SEVERAL STEPS LIVES (TS-14, the first
        // option in #121). Plain ink, no mark and no warning: nothing is wrong,
        // the option simply belongs to the other form. A link rather than a
        // place to go and look, because at 390 the rail does not show that tab.
        <p className="ctl-panel-note">
          one PR for several steps → <a href={WORKFLOW_FORM}>Submit a workflow</a>
        </p>
      )}

      <Consequence
        strategy={draft.strategy}
        steps={steps}
        scale={scale}
        terminals={terminals}
      />
      {errors?.strategy && <p className="warn-text" role="alert">{errors.strategy}</p>}

      {scale === 'workflow' && (
        <>
          {/* `.dsp-label`, NOT AN INLINE `marginTop: 14`. 14px is off the
              four-step scale and an inline style is out of the sheet's reach,
              so no spacing rule could ever correct it; the gap above each
              field is `--ctl-s3`, declared once in the sheet. */}
          <label className="t-label dsp-label" htmlFor="dsp-carrier">
            what carries work between steps
          </label>
          <select
            id="dsp-carrier"
            className="mono"
            value={draft.carrier}
            aria-describedby="dsp-carrier-note"
            onChange={(e) => set({ carrier: e.target.value as DispatchCarrier })}
          >
            {/* THE VALUE, ONCE (TS-6). It read `checkpoints — Checkpoints`:
                the token and a capitalised copy of the token. */}
            {DISPATCH_CARRIERS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          {/* THE FACT STAYS ON THE SURFACE, AS ONE PLAIN LINE (TS-6). Said on
              every carrier, not only on `branches`: the control records a
              preference the platform does not act on yet, and a caller is
              entitled to know that before they choose one. It was a permanent
              two-sentence `--warn` paragraph -- a warning on a choice nothing
              is wrong with. The two sentences are this line's accessible name
              and the select's description; the argument is the help topic. */}
          <p id="dsp-carrier-note" className="ctl-panel-note" aria-label={CARRIER_NOTE}>
            not acted on yet <a href={`#${helpAnchor(CARRIER_HELP)}`}>Why &rarr;</a>
          </p>
        </>
      )}
      {errors?.carrier && <p className="warn-text" role="alert">{errors.carrier}</p>}

      <label className="t-label dsp-label" htmlFor="dsp-repo">
        repository url {repoRequired ? '(required by this choice)' : '(optional)'}
      </label>
      {/* NOT `type="url"`. The form has no `noValidate`, so the browser would
          enforce its own URL grammar on submit -- and `git@github.com:o/r.git`,
          which the API accepts, is not a URL to it. The scheme warning below is
          this field's only check, and it never blocks. */}
      <input
        id="dsp-repo"
        className="mono dsp-repo"
        spellCheck={false}
        placeholder="https://github.com/owner/repo.git"
        value={draft.repositoryUrl}
        aria-invalid={repoRefused || undefined}
        onChange={(e) => set({ repositoryUrl: e.target.value })}
      />
      {repoRefused && (
        // UNDER ANY STRATEGY. `collect` does not need a repository, but a
        // repository that IS given is validated whatever the strategy, so
        // `not a url` under the default is refused on the click just the same.
        <p className="warn-text" role="alert">
          The API accepts only a URL starting with {REPOSITORY_SCHEMES.join(', ')}, so it will
          refuse this one.
        </p>
      )}
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

/**
 * What the options above do NOT already say, and only that (TS-20).
 *
 * This box used to open with the chosen option's own headline -- the count
 * sentence printed on the option directly above it -- so every choice was read
 * twice. What it can add is the one thing an option cannot: on a WORKFLOW with
 * `integrate`, which step would open the one pull request as the form is drawn,
 * or that the graph has no single final step. Anywhere else it draws nothing,
 * and on the task form that means never: the count on each option is the
 * consequence.
 */
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
  // IntegratorPreview has nothing to say under two steps; checked here too so
  // an empty box is never drawn around it.
  if (strategy !== 'integrate' || scale !== 'workflow' || terminals === undefined || steps < 2) return null
  return (
    <div className="dsp-consequence" role="status">
      <IntegratorPreview terminals={terminals} steps={steps} />
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
 * Whether this task's run is over AND reported what it did to the repository.
 *
 * `result_summary.git` is written by `_harvest_git` at the end of an attempt,
 * so it is the one record that the harvest the strategy describes actually
 * happened. Before a terminal state there is no harvest yet; a terminal task
 * without the block cloned nothing, or never ran at all.
 */
function harvestedGit(task: Task): { patch: string | null } | null {
  if (!TERMINAL_STATES.has(task.state)) return null
  const git: unknown = task.result_summary?.git
  if (typeof git !== 'object' || git === null || Array.isArray(git)) return null
  const patch: unknown = (git as { patch?: unknown }).patch
  return { patch: typeof patch === 'string' && patch !== '' ? patch : null }
}

/**
 * WHAT THIS RUN'S DISPATCH WAS, AND -- ONCE IT HAS HAPPENED -- WHAT IT DID.
 *
 * `null` from `dispatchOf` means the API did not report a dispatch, which is a
 * deployment older than the field rather than a caller who chose `collect`.
 * Those render differently here for the same reason a failed read never renders
 * as empty data anywhere else in this app.
 *
 * A FACTS STRIP, NOT A DEFINITION LIST OF SENTENCES. Every key used to carry a
 * sentence explaining its value -- what a strategy publishes, what a role does,
 * that no worker reads the carrier -- and those sentences are the same on every
 * task there has ever been. They are `#help/dispatch-strategies` and
 * `#help/dispatch-carrier`, in the card foot and the rail's Help section. What
 * stays on the surface is this task's values, and one mark.
 *
 * THE MARK IS THE CARRIER'S, AND IT MAY NOT GO. The carrier is recorded and
 * nothing acts on it; a value printed with no qualifier reads as a behaviour.
 * `not acted on` keeps that visible on the value itself (§9, the mark carries
 * the claim), and `CARRIER_NOTE` is its accessible name.
 *
 * NOTHING IN THE PAST TENSE BEFORE IT HAPPENED. The collect line said "The
 * patch was harvested into this task's artifacts" on tasks still QUEUED, on
 * tasks cancelled before they ran, and on the Submit read-back of a task that
 * had only just been created. `published` is shown only once the run is over
 * and its git block says a harvest happened (`harvestedGit`); until then the
 * strip states the strategy and claims no outcome.
 */
export function DispatchFacts({ task }: { task: Task }) {
  const d = dispatchOf(task)
  if (d === null) return <DispatchUnreported />
  const c = consequenceOf(d.strategy, 1)
  const harvest = harvestedGit(task)

  return (
    <ul className="ctl-facts dsp-facts">
      <li className="ctl-fact">
        <b>strategy</b>
        <code>{d.strategy}</code>
      </li>
      <li className="ctl-fact">
        <b>carrier</b>
        <code>{d.carrier}</code>
        <i className="ctl-mark" role="img" aria-label={CARRIER_NOTE}>
          not acted on
        </i>
      </li>
      {d.role !== null && (
        <li className="ctl-fact">
          <b>role</b>
          <code>{d.role}</code>
        </li>
      )}
      {d.integrates.length > 0 && (
        // The task ids, in the order their patches are applied --
        // `integrates` is the workflow's topological prefix, not a set, so the
        // arrows are the order and not decoration.
        <li className="ctl-fact">
          <b>integrates</b>
          <span className="mono" title="In the order their patches are applied.">
            {d.integrates.join(' → ')}
          </span>
        </li>
      )}
      {!c.pushes && d.role === null && harvest !== null && (
        <li className="ctl-fact">
          <b>published</b>
          {harvest.patch !== null ? 'nothing, by request · patch in artifacts' : 'nothing, by request'}
        </li>
      )}
    </ul>
  )
}

function DispatchUnreported() {
  return (
    <div className="ctl-empty is-partial" role="status">
      {/* THE HEADING IS THE MARKER. "Did not report" and "reported that it
          published nothing" are two different facts, and the panel exists so
          they never render alike. B7.4 took the `?`: the heading names the
          subject of the absence (the API, not the run) and the sentence under
          it names what may not be concluded, which between them are the topic.
          `#help/dispatch-absent-is-old-api` is on the Workflows board's own
          copy of this panel. */}
      <h3>This API did not report a dispatch</h3>
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
