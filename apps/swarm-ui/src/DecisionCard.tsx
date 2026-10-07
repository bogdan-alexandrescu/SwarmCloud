/**
 * THE DECISION CARD: what a verdict gate decided, why, and from what (owner
 * request 2026-10-07, on a fix step the review's MERGE kept from running:
 * "right now I have no idea what happened and why that decision was made and
 * based on what").
 *
 * It leads the inspector's Details tab on two kinds of step:
 *
 *  * a GATED step (its dispatch or its result carries a verdict gate), in
 *    five parts: the rule; the verdict and where it was read; what the review
 *    weighed; what happened instead; the inputs it staged;
 *  * a REVIEW step (a finished step whose manifest lists `verdict.json` and
 *    that some step of its workflow gates on): the verdict it wrote, as the
 *    gated step recorded it, and what that step did.
 *
 * Every fact is read by `decision.ts` from what the API already serves; the
 * names of the steps behind task ids come from `GET /v1/workflows/{id}`, the
 * route Details' handed-on diffs read (once for a finished step, again with
 * the drawer for a live one). A fact the worker did not record is a
 * dash with its reason, never a guess: an unrecorded verdict is not a MERGE,
 * and unrecorded findings are not zero findings.
 */
import type { ReactNode } from 'react'

import { Dash, ToneMark } from './components'
import { loadWorkflow, type WorkflowRead } from './api'
import { absTime } from './AttemptTimeline'
import {
  artifactHref,
  branchOf,
  buildsOnOf,
  commitHref,
  countLine,
  gateDecisionOf,
  gatesOn,
  groupFindings,
  inspectorHref,
  prTextFromOf,
  publishReasonOf,
  pullRequestOf,
  runnerReportOf,
  skippedAgentOf,
  writesVerdict,
  type Finding,
  type GateDecision,
  type PrTextSource,
} from './decision'
import type { Result } from './fetch'
import { Mark } from './primitives'
import { useRead } from './RunFiles'
import { TERMINAL_STATES, stagedInputsOf, type Task } from './types'

/** The tone of a verdict word: MERGE is a pass, NOT_YET asks for work. */
function verdictTone(v: string): string {
  return v === 'MERGE' ? 'ok' : v === 'NOT_YET' ? 'warn' : 'unknown'
}

/**
 * The workflow's tasks, for step names and the steps a review gates; null when
 * the task stands alone or the read has not answered. A finished task's
 * workflow is read once; a live one re-reads with the drawer.
 */
function useWorkflow(task: Task, readAt: number | null): Result<WorkflowRead> | null {
  const wf = task.workflow_id
  const key = TERMINAL_STATES.has(task.state) ? '' : `${readAt ?? ''}`
  const { state } = useRead<WorkflowRead>(
    () => (wf === null ? Promise.resolve({ status: 'empty', fetchedAt: Date.now() }) : loadWorkflow(wf)),
    wf ?? `none:${task.id}`,
    key,
    null,
  )
  return wf === null ? null : state
}

function tasksOf(w: Result<WorkflowRead> | null): Task[] {
  return w !== null && (w.status === 'ok' || w.status === 'stale') && Array.isArray(w.data.tasks) ? w.data.tasks : []
}

/** A link to a task's inspector, by its step name when the workflow read gave one. */
function TaskLink({ id, tasks }: { id: string; tasks: readonly Task[] }) {
  const step = tasks.find((t) => t.id === id)?.step_id ?? null
  return (
    <a className="ctl-link" href={inspectorHref(id)}>
      {step ?? id}
    </a>
  )
}

/** `step (task id)`, the id in mono, for a row that names its source in full. */
function TaskNamed({ id, tasks }: { id: string; tasks: readonly Task[] }) {
  const step = tasks.find((t) => t.id === id)?.step_id ?? null
  return (
    <>
      <TaskLink id={id} tasks={tasks} />
      {step !== null && <span className="mono dc-id"> {id}</span>}
    </>
  )
}

export function DecisionCard({ task, readAt }: { task: Task; readAt: number | null }) {
  const gate = gateDecisionOf(task)
  if (gate !== null) return <GatedDecision task={task} gate={gate} readAt={readAt} />
  if (task.workflow_id !== null && writesVerdict(task)) return <ReviewDecision task={task} readAt={readAt} />
  return null
}

function Shell({ children, sub }: { children: ReactNode; sub: string }) {
  return (
    <section className="dt-card dc" aria-label="Decision">
      <div className="dt-card-head">
        <b>Decision</b>
        <span className="dt-note">{sub}</span>
      </div>
      {children}
    </section>
  )
}

// ---------------------------------------------------------------------------
// A gated step
// ---------------------------------------------------------------------------

function GatedDecision({ task, gate, readAt }: { task: Task; gate: GateDecision; readAt: number | null }) {
  const tasks = tasksOf(useWorkflow(task, readAt))
  const review = gate.reviewTaskId
  const reviewLink =
    review === null ? <Dash why="Neither the step's dispatch nor its result names the review task its gate reads." /> : <TaskLink id={review} tasks={tasks} />
  const runsOn =
    gate.verdictIn.length > 0 ? (
      gate.verdictIn.join(' or ')
    ) : (
      <Dash why="Neither the step's dispatch nor its result names the verdicts that start its agent." />
    )
  return (
    <Shell sub="why this step's agent ran or did not">
      <p className="dc-rule">
        This step&apos;s agent runs only when {reviewLink} says {runsOn}.
      </p>
      <VerdictRow task={task} gate={gate} reviewLink={reviewLink} />
      <Weighed gate={gate} />
      <Happened task={task} gate={gate} tasks={tasks} reviewLink={reviewLink} />
      <Staged task={task} tasks={tasks} />
    </Shell>
  )
}

function VerdictRow({ task, gate, reviewLink }: { task: Task; gate: GateDecision; reviewLink: ReactNode }) {
  const v = gate.verdict
  const review = gate.reviewTaskId
  return (
    <div className="dc-row" data-testid="decision-verdict">
      <span className="dc-k">Verdict</span>
      <span className="dc-v">
        {v.kind === 'read' ? (
          <ToneMark tone={verdictTone(v.verdict)}>{v.verdict}</ToneMark>
        ) : v.kind === 'unreadable' ? (
          <ToneMark tone="bad">unreadable</ToneMark>
        ) : v.kind === 'pending' ? (
          <Mark kind="pending" say="The verdict this step read is written into its result, which is written when this step finishes." />
        ) : (
          <Mark kind="absent" say="This step finished and its result records no verdict, so what its gate read is not known here." />
        )}{' '}
        from {reviewLink}
        {' · '}
        {review === null ? (
          <Dash why="No review task is named, so there is no verdict file to open." />
        ) : gate.file !== null ? (
          <a className="ctl-link mono" href={artifactHref(review, gate.file)}>
            {gate.file}
          </a>
        ) : (
          <>
            <a className="ctl-link mono" href={artifactHref(review, 'verdict.json')}>
              verdict.json
            </a>{' '}
            <Dash why="The worker did not record which file it read the verdict from; verdict.json is the review's conventional name." />
          </>
        )}
        {v.kind === 'read' && (
          <>
            {' · '}
            {task.started_at !== null ? (
              <>read as this step started · {absTime(task.started_at)}</>
            ) : (
              <>
                read <Dash why="The worker stamps no instant on the read, and this step records no start." />
              </>
            )}
          </>
        )}
      </span>
      {v.kind === 'unreadable' && <p className="dt-note dc-why">{v.error}</p>}
    </div>
  )
}

function FindingItem({ f }: { f: Finding }) {
  return (
    <li className="dc-finding">
      {f.file !== null && <code className="dc-file">{f.file}</code>} <span>{f.problem}</span>
      {f.fix !== null && (
        <span className="dc-fix">
          <span className="dc-k">fix</span> {f.fix}
        </span>
      )}
    </li>
  )
}

/** The review's findings by severity, with the count line; a dash with its reason when none were recorded. */
function Findings({ gate, empty }: { gate: GateDecision; empty: string }) {
  const findings = gate.findings
  if (findings === null) {
    return (
      <p className="dt-note">
        <Dash why="The worker did not record the review's findings in this step's result, so what the review weighed is not shown here; the verdict file holds them." />{' '}
        findings not recorded
      </p>
    )
  }
  return (
    <>
      <p className="dc-count" data-testid="decision-count">
        {countLine(findings)}
      </p>
      {findings.length === 0 && <p className="dt-note">{empty}</p>}
      {groupFindings(findings).map((g) => (
        <div key={g.key} className="dc-group">
          <span className="dc-k">
            {g.label} · {g.items.length}
          </span>
          <ul className="dc-findings" aria-label={`${g.label} findings`}>
            {g.items.map((f, i) => (
              <FindingItem key={i} f={f} />
            ))}
          </ul>
        </div>
      ))}
      {gate.dropped !== null && (
        <p className="dt-note">
          {gate.dropped} more past the worker&apos;s cap, not recorded here; the verdict file holds every one.
        </p>
      )}
    </>
  )
}

function Weighed({ gate }: { gate: GateDecision }) {
  if (gate.verdict.kind !== 'read') return null
  return (
    <div className="dc-part" data-testid="decision-findings">
      <span className="dc-h">What the review weighed</span>
      <Findings gate={gate} empty="The review recorded no findings." />
    </div>
  )
}

function prTextWords(src: PrTextSource, upstream: ReactNode): ReactNode {
  return src === 'implementer' ? upstream : src === 'label' ? "the workflow's label" : 'none'
}

/** Whose pull request title and body the step used, `result_summary.pull_request_text_from`. */
function PrText({ task, tasks }: { task: Task; tasks: readonly Task[] }) {
  const from = prTextFromOf(task)
  const upstream = buildsOnOf(task)
  const up = upstream === null ? 'the implementer' : <TaskLink id={upstream} tasks={tasks} />
  if (from === null) {
    return (
      <p className="dt-note">
        PR text from <Dash why="The worker did not record whose pull request title and body this step used." />
      </p>
    )
  }
  if (from.title === from.body) {
    return <p className="dt-note">PR title and body from {prTextWords(from.title, up)}.</p>
  }
  return (
    <p className="dt-note">
      PR title from {prTextWords(from.title, up)}; body from {prTextWords(from.body, up)}.
    </p>
  )
}

/** The pull request, the branch and its head, or why there is none. */
function Published({ task, tasks }: { task: Task; tasks: readonly Task[] }) {
  const pr = pullRequestOf(task)
  const branch = branchOf(task)
  const upstream = buildsOnOf(task)
  const sha = branch?.sha ?? null
  const shaHref = sha === null ? null : commitHref(task.repository_url, sha)
  const short = sha === null ? null : sha.slice(0, 7)
  const reason = publishReasonOf(task)
  return (
    <p>
      {pr !== null ? (
        <>
          Published{' '}
          <a className="ctl-link" href={pr.url} target="_blank" rel="noreferrer">
            PR #{pr.number}
          </a>
        </>
      ) : (
        <>
          No pull request
          {reason !== null ? ` · ${reason}` : ''}
        </>
      )}{' '}
      from{' '}
      {branch === null ? (
        <Dash why="The worker did not record which branch this step pushed, or its head." />
      ) : (
        <>
          <code className="mono">{branch.name ?? 'an unnamed branch'}</code> at{' '}
          {short === null ? (
            <Dash why="The worker recorded the branch without its head commit." />
          ) : shaHref !== null ? (
            <a className="ctl-link mono" href={shaHref} target="_blank" rel="noreferrer">
              {short}
            </a>
          ) : (
            <code className="mono">{short}</code>
          )}
        </>
      )}
      {upstream !== null && (
        <>
          , carrying <TaskLink id={upstream} tasks={tasks} />&apos;s work
        </>
      )}
      .
    </p>
  )
}

/** What the step did: no agent and a publish, or the agent and why. */
function Happened({ task, gate, tasks, reviewLink }: { task: Task; gate: GateDecision; tasks: readonly Task[]; reviewLink: ReactNode }) {
  const v = gate.verdict
  const runner = runnerReportOf(task)
  const skipped = skippedAgentOf(task)
  let body: ReactNode
  if (v.kind === 'unreadable') {
    body = <p>No agent was started and nothing was published: an unreadable review is not a MERGE.</p>
  } else if (v.kind === 'pending') {
    body = (
      <p className="dt-note">
        <Mark kind="pending" say="Whether this step's agent runs is decided when it reads the verdict, and recorded when it finishes." /> not decided yet
      </p>
    )
  } else if (gate.agentRan === false) {
    body = (
      <>
        <p>
          No agent was started.{skipped !== null ? ` The worker's reason: ${skipped}.` : ''}
        </p>
        <Published task={task} tasks={tasks} />
        <PrText task={task} tasks={tasks} />
      </>
    )
  } else if (gate.agentRan === true) {
    const n = gate.findings?.length ?? null
    body = (
      <>
        <p>
          This step&apos;s agent ran because {reviewLink} said {v.kind === 'read' ? v.verdict : 'its verdict'}, one of the verdicts this step runs on.{' '}
          {n === null ? (
            <>
              It was handed the verdict file{gate.file !== null ? ` ${gate.file}` : ''}; the findings in it were not recorded here.
            </>
          ) : n === 0 ? (
            <>It was handed {gate.file ?? 'the verdict file'}, which recorded no findings.</>
          ) : (
            <>
              It was handed the {n} finding{n === 1 ? '' : 's'} above, in {gate.file ?? 'the verdict file'}.
            </>
          )}
        </p>
        {pullRequestOf(task) !== null && <Published task={task} tasks={tasks} />}
      </>
    )
  } else {
    body = (
      <p className="dt-note">
        <Dash why="The worker did not record whether this step's agent ran." /> whether the agent ran is not recorded
      </p>
    )
  }
  return (
    <div className="dc-part" data-testid="decision-happened">
      <span className="dc-h">What happened</span>
      {body}
      {runner.summary !== null && (
        <p className="dt-note">
          The worker: <q>{runner.summary}</q>
          {runner.status !== null && <> · {runner.status}</>}
        </p>
      )}
    </div>
  )
}

function Staged({ task, tasks }: { task: Task; tasks: readonly Task[] }) {
  const s = stagedInputsOf(task)
  const terminal = TERMINAL_STATES.has(task.state)
  return (
    <div className="dc-part">
      <span className="dc-h">Inputs it staged</span>
      {s.kind === 'unreported' ? (
        <p className="dt-note">
          {terminal ? (
            <Dash why="This step's result reported no staged inputs." />
          ) : (
            <Mark kind="pending" say="Staged inputs are reported in the result, which is written when this step finishes." />
          )}
        </p>
      ) : (
        <ul className="dc-staged" aria-label="Inputs it staged">
          {s.inputs.map((i, n) => (
            <li key={`${i.filename}-${n}`}>
              <code className="mono">{i.filename}</code> from{' '}
              {i.upstreamTaskId === null ? 'the submission' : <TaskNamed id={i.upstreamTaskId} tasks={tasks} />}
            </li>
          ))}
          {s.inputs.length === 0 && <li className="dt-note">none staged</li>}
          {s.malformed > 0 && (
            <li className="dt-note">
              {s.malformed} entr{s.malformed === 1 ? 'y' : 'ies'} could not be read as a staged file
            </li>
          )}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// A review step
// ---------------------------------------------------------------------------

/** What a gated step did with the verdict, in one line. */
function GatedOutcome({ step }: { step: Task }) {
  const gate = gateDecisionOf(step)
  const pr = pullRequestOf(step)
  if (gate === null || gate.verdict.kind === 'pending') {
    return <>has not read it yet · {step.state.toLowerCase()}</>
  }
  if (gate.verdict.kind === 'unreadable') return <>could not read it: no agent was started and nothing was published</>
  if (gate.verdict.kind === 'unrecorded') {
    return (
      <>
        <Dash why="That step finished without recording what its gate read." /> not recorded
      </>
    )
  }
  const what = gate.agentRan === false ? 'No agent was started' : gate.agentRan === true ? 'Its agent ran' : 'Whether its agent ran is not recorded'
  return (
    <>
      {what}
      {pr !== null && (
        <>
          {'; published '}
          <a className="ctl-link" href={pr.url} target="_blank" rel="noreferrer">
            PR #{pr.number}
          </a>
        </>
      )}
      .
    </>
  )
}

function ReviewDecision({ task, readAt }: { task: Task; readAt: number | null }) {
  const w = useWorkflow(task, readAt)
  const tasks = tasksOf(w)
  const gated = tasks.filter((t) => t.id !== task.id && gatesOn(t, task.id))
  if (gated.length === 0) return null
  // The verdict as the first gated step that read it recorded it: the review
  // writes `verdict.json` as an artifact and nothing else, so the gate block
  // is the one record of it the API serves.
  const reader = gated.find((t) => gateDecisionOf(t)?.verdict.kind === 'read') ?? null
  const gate = reader === null ? null : gateDecisionOf(reader)
  const file = gate?.file ?? 'verdict.json'
  return (
    <Shell sub="what this review decided for the steps after it">
      <div className="dc-part">
        <span className="dc-h">Verdict this review wrote</span>
        <div className="dc-row" data-testid="decision-verdict">
          <span className="dc-k">Verdict</span>
          <span className="dc-v">
            {gate !== null && gate.verdict.kind === 'read' ? (
              <ToneMark tone={verdictTone(gate.verdict.verdict)}>{gate.verdict.verdict}</ToneMark>
            ) : (
              <Mark kind="pending" say="No step gated on this review has recorded reading its verdict yet; the verdict file holds it." />
            )}
            {' · '}
            <a className="ctl-link mono" href={artifactHref(task.id, file)}>
              {file}
            </a>
            {reader !== null && (
              <>
                {' · read by '}
                <TaskLink id={reader.id} tasks={tasks} />
              </>
            )}
          </span>
        </div>
        {gate !== null && <Findings gate={gate} empty="This review recorded no findings." />}
      </div>
      <div className="dc-part" data-testid="decision-gated">
        <span className="dc-h">What it gated</span>
        <ul className="dc-staged">
          {gated.map((g) => {
            const d = gateDecisionOf(g)
            const on = d !== null && d.verdictIn.length > 0 ? d.verdictIn.join(' or ') : null
            return (
              <li key={g.id}>
                <TaskLink id={g.id} tasks={tasks} />
                {on !== null ? <>, whose agent runs only when this review says {on}: </> : ': '}
                <GatedOutcome step={g} />
              </li>
            )
          })}
        </ul>
      </div>
    </Shell>
  )
}

// ---------------------------------------------------------------------------
// The Artifacts tab of a skipped step
// ---------------------------------------------------------------------------

/**
 * WHY A SKIPPED STEP'S ARTIFACTS ARE ONLY ITS PR TEXT. No agent ran, so
 * nothing wrote outputs; the worker copied (or made) `pr-title.txt` and
 * `pr-body.md` to title the pull request it published
 * (`_adopt_pull_request_text`). Without this the tab looked empty.
 */
export function SkippedArtifactsNote({ task, tasks }: { task: Task; tasks: readonly Task[] }) {
  const gate = gateDecisionOf(task)
  if (gate === null || gate.agentRan !== false) return null
  const skipped = skippedAgentOf(task)
  const from = prTextFromOf(task)
  const upstream = buildsOnOf(task)
  const up = upstream === null ? 'the implementer' : <TaskLink id={upstream} tasks={tasks} />
  const source =
    from === null ? (
      <>
        from <Dash why="The worker did not record where this step's pull request title and body came from." />
      </>
    ) : from.title === from.body ? (
      <>{from.title === 'implementer' ? <>copied from {up}</> : from.title === 'label' ? "made from the workflow's label" : 'from nowhere recorded'}</>
    ) : (
      <>
        title {from.title === 'implementer' ? <>copied from {up}</> : prTextWords(from.title, up)}, body{' '}
        {from.body === 'implementer' ? <>copied from {up}</> : prTextWords(from.body, up)}
      </>
    )
  return (
    <p className="dt-note dc-arts" data-testid="skipped-artifacts">
      No agent was started{skipped !== null ? ` (${skipped})` : ''}, so this step wrote no outputs of its own. It holds{' '}
      <code className="mono">pr-title.txt</code> and <code className="mono">pr-body.md</code>, {source}, to title the pull
      request it published. Why: the{' '}
      <a className="ctl-link" href={inspectorHref(task.id)}>
        Decision card
      </a>{' '}
      on Details.
    </p>
  )
}
