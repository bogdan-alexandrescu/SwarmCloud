import { useContext, useEffect, useState, type ReactNode } from 'react'

import { AgParentLink } from '../AgentChildren'
import { SplitClock } from '../AgentDetail'
import { agentName, rememberAgentName, workflowHref } from '../agentlist'
import { useResourceClasses } from '../Blockers'
import { BrowserUseFact, browserUseOf } from '../BrowserUse'
import { HelpCard } from '../HelpCard'
import { LiveControl, type LiveState } from '../LiveBrowser'
import { rowTitle } from '../OverviewRegions'
import { addressToPath } from '../paths'
import { StopRun } from '../StopRun'
import { TaskIdLine } from '../TaskIdLine'
import {
  STRATEGY_LABEL,
  accountText,
  clockTime,
  dispatchOf,
  elapsed,
  type DispatchRole,
  type DispatchStrategy,
  type Task,
} from '../types'
import { useNow } from '../useNow'
import { Button, CIcon, StateMark } from '../components'

// THE SPLIT'S HEADER (moved out of AgentSplit.tsx by SPEED-D4A, 2026-10-14,
// unchanged): the state, the agent's name, its headline, Copy link, Take
// control, Stop and ✕, the whole id, and the meta line under them.

const COPY_SAID_MS = 4000

/**
 * A LONE AGENT'S OWN TITLE, read as its Agents row reads it (`ownTitle` in
 * Agents.tsx, SUMMARY-TITLE): `metadata.title`, trimmed, and null for a step
 * or for a title that is absent, blank or not a string. Restated here rather
 * than imported because the head must not import the Agents screen: half the
 * drawer's tests replace `../Agents` with a mock that has only `AgentsScreen`,
 * and an import of `ownTitle` from it threw on every drawer they opened (50
 * tests red on PR 2616's merge with main, CIFIX-2616).
 */
export function ownTitle(t: Pick<Task, 'step_id' | 'metadata'>): string | null {
  if ((t.step_id ?? null) !== null) return null
  const v = t.metadata?.title
  return typeof v === 'string' && v.trim() !== '' ? v.trim() : null
}

/**
 * A WORKFLOW STEP'S NAME AS ITS AGENTS ROW SAYS IT (QA2 ovag2-14):
 * `<workflow title> · <step>`. The pane was headed `checkpoints` alone, and
 * so were its breadcrumb and the document title, while the row named the
 * workflow too. Every step carries its workflow's `metadata.title`; without
 * one the step alone stays, as on the row.
 */
export function stepTitle(t: Pick<Task, 'workflow_id' | 'step_id' | 'metadata'>): string | null {
  if ((t.workflow_id ?? null) === null || (t.step_id ?? null) === null) return null
  const v = t.metadata?.title
  return typeof v === 'string' && v.trim() !== '' ? `${v.trim()} · ${t.step_id}` : null
}

/**
 * THE DETAIL'S HEADER, IN TWO LINES (agent-details-v3.html A, picked
 * 2026-10-04). Line 1: the state pill, the agent's name -- its step, or what
 * it is when it stands alone -- the headline `elapsed · attempt n of N` in
 * sans, and Copy link, Stop and ✕. Line 2: one sans meta line -- profile ·
 * class · workflow · account · the whole task id with its copy button -- and
 * the dispatch as plain-language chips.
 *
 * THE ID IS ON THE META LINE, NOT A LINE OF ITS OWN (VQ-010, BGD's visual QA
 * 2026-10-26): with it under the title the header was three lines where v3 A
 * draws two. It is still printed whole, in mono, with its copy
 * (`TaskIdLine`, #94 -- it was once only the button's tooltip).
 *
 * WHAT LEFT IT, AND WHERE TO: the tenant
 * (the panel's tenant tile says it), gen and units (the Attempts tab), and
 * started (the strip's Elapsed sub-line). The actions share line 1 again:
 * the title takes the slack and clamps at two lines, and the headline and
 * the actions never shrink, so the ✕ cannot be drawn over Stop (the defect
 * lane U9 moved them to a row of their own for). A child carries its
 * parent's link above line 1 (agent-detail-2.html A2).
 */
export function AgHead({
  taskId,
  task,
  runs,
  read,
  reload,
  onClose,
  live,
  reloadLive,
}: {
  taskId: string
  task: Task | null
  /** How many times it ran (`timesRun`); null before the task is read. */
  runs: number | null
  read: string
  reload: () => void
  onClose: () => void
  /** The live browser's session read; Take control sits beside Stop (live-browser.md §6). */
  live: LiveState
  reloadLive: () => void
}) {
  // THE SPLIT'S ONE INSTANT (`SplitTick`), which the Details tab draws its
  // running figures at too; the bare clock only outside a split.
  const clock = useNow(1000)
  const now = useContext(SplitClock) ?? clock
  const [copied, setCopied] = useState<'yes' | 'no' | null>(null)
  useEffect(() => {
    if (copied === null) return
    const id = setTimeout(() => setCopied(null), COPY_SAID_MS)
    return () => clearTimeout(id)
  }, [copied])
  // THE PRESS ALWAYS ANSWERS (live QA WA-14): Copy link changed nothing on
  // screen and announced nothing. The button's own label says `Copied` (or
  // `Copy failed`) for a few seconds, and an always-mounted live region says
  // the sentence -- a region mounted with its text is not announced. A
  // clipboard that throws, or answers without a promise, is a failure too.
  const copy = () => {
    const href = typeof window === 'undefined' ? '' : window.location.href
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined || typeof clip.writeText !== 'function') {
      setCopied('no')
      return
    }
    Promise.resolve()
      .then(() => clip.writeText(href))
      .then(
        () => setCopied('yes'),
        () => setCopied('no'),
      )
  }
  // THE NAME THE LIST ROW GAVE IT (QA1-FIX-32, overview-agents-20): a lone
  // agent with a title (`ownTitle`, SUMMARY-TITLE) was `Fix flaky lease
  // heartbeat test` in its Agents row and `claude-code agent · 8ea5c26d` here
  // and in the breadcrumb. The title leads; a step leads with its workflow's
  // (`stepTitle`), and a step or an agent without one keeps `agentName`'s
  // words, as the row does.
  const title = task === null ? null : (ownTitle(task) ?? stepTitle(task))
  const name = task === null ? taskId : (title ?? agentName(task))
  // The breadcrumb has the id and no task: it names the agent with this.
  // A title stands in as the step name `agentName` reads; without one the
  // task's own step, or its kind and id, are remembered.
  useEffect(() => {
    if (task !== null) rememberAgentName(title === null ? task : { id: task.id, runner_profile: task.runner_profile, step_id: title })
  }, [task, title])
  // NOT A <header> (axe on every task route, visual QA run 3, #1680): outside
  // a sectioning element a <header> is a banner landmark, so beside the trail
  // bar's (`.ctl-bar`, App.tsx) the page had two (landmark-no-duplicate-banner,
  // landmark-unique), and on a phone, inside the split's region, a banner
  // that was not top-level. The page has one banner; this is the agent's head
  // inside the split, which is named already (`Agent <id>`).
  return (
    <div className="ag-head">
      {task !== null && <AgParentLink task={task} />}
      <div className="ag-head-row">
        {task !== null ? (
          <StateMark state={task.state} />
        ) : (
          <span className="ag-head-state">{read === 'error' ? 'not read' : 'reading'}</span>
        )}
        {/* TWO LINES, THEN AN ELLIPSIS, AND THE WHOLE NAME IN THE TOOLTIP
            (walkthrough C): a long step name ran off the column. */}
        <h2 className="ag-head-title" title={name}>
          {name}
        </h2>
        {task !== null && <AgHeadline task={task} runs={runs ?? task.attempt_count} now={now} />}
        <span className="ag-head-actions">
          <Button onClick={copy}>
            {copied === 'yes' ? 'Copied' : copied === 'no' ? 'Copy failed' : 'Copy link'}
          </Button>
          <span role="status" aria-live="polite" className="ag-head-copied">
            {copied === 'yes' ? 'copied' : copied === 'no' ? 'could not copy' : ''}
          </span>
          {/* THE REFUSAL IS THE BROWSER TAB'S LINE (BT-010, QA1-FIX-13 chrome-C06):
              drawn here as well, a plain-HTTP page's sentence was said twice on
              one screen, in this head and in the strip under the tabs. Here it
              is the disabled button's title. */}
          {live.status === 'ok' && live.session !== null && (
            <LiveControl taskId={taskId} session={live.session} reload={reloadLive} refusal="title" />
          )}
          {task !== null && (
            <span className="ag-head-stop">
              <StopRun task={task} what="this agent" reload={reload} label="Stop" />
            </span>
          )}
          <Button iconOnly icon={<CIcon name="close" />} className="drawer-close ag-head-close" onClick={onClose}>
            Close
          </Button>
        </span>
      </div>
      <AgHeadMeta task={task} taskId={taskId} />
    </div>
  )
}

/**
 * `elapsed · attempt n of N`, and when it ended once it has. `elapsed()` is
 * the Agents list's own figure, so the row and the header never disagree; the
 * attempt is the task's own counter, which the header's read carries.
 */
function AgHeadline({ task, runs, now }: { task: Task; runs: number; now: number }) {
  const el = elapsed(task, now)
  const ended = clockTime(task.completed_at, now)
  // GENERATION 0 IS NEVER ADMITTED (QA1-FIX-32, overview-agents-59): the
  // generation is never reset, so only a task no attempt was ever leased for
  // is at 0. A READY task there read `attempt 1 of 3` here while the Now card
  // said `no attempt yet`; the counter is not a run when nothing ran.
  const ran = task.current_generation === 0 ? 0 : runs
  return (
    <span className="ag-head-hl">
      <b>{el.text}</b> ·{' '}
      {ran > 0 ? `attempt ${ran} of ${task.max_attempts}` : 'no attempt yet'}
      {ended !== null && (
        <span title={`ended ${ended.title}`}> · ended {ended.text}</span>
      )}
    </span>
  )
}

/** What each role does, in the words of the dispatch chips. */
const ROLE_SAY: Readonly<Record<DispatchRole, string>> = {
  contributor: 'adds to the shared PR',
  integrator: 'opens the shared PR',
}

/**
 * WHAT EACH CHIP MEANS FOR THIS AGENT'S PULL REQUEST (visual QA run 4, N16,
 * flow-workflow-graph-light-11): `One PR for all steps` and `adds to the
 * shared PR` named the dispatch and left the reader to guess whether this
 * agent opens a pull request. Each chip carries its sentence as its
 * description (and its title); the `?` after them opens
 * `strategy-on-a-task`, which says the same, for a phone and a keyboard.
 */
const STRATEGY_MEANS: Readonly<Record<DispatchStrategy, string>> = {
  collect: 'This agent opens no pull request: what it made is collected as its artifacts and checkpoints.',
  'direct-pr': 'This agent opens its own pull request with its changes.',
  integrate: 'This agent’s changes go into one pull request for the whole workflow, opened by its integrating step.',
}
const ROLE_MEANS: Readonly<Record<DispatchRole, string>> = {
  contributor:
    'This agent opens no pull request of its own: it pushes its branch, and the workflow’s integrating step applies its changes to the shared pull request.',
  integrator: 'This agent opens the workflow’s one pull request, with the contributing steps’ changes applied to it.',
}

/**
 * WHAT WAS NOT READ, AND WHY (visual QA run 3, ff-007, #1680). `accountText`'s
 * title for an account that is not a real id repeats the chip's words
 * (`not read.`), so the fact said neither which account was meant nor why it
 * was missing. Each status the API answers (`swarm_api/task_accounts.py`) gets
 * its sentence; a status this console does not know keeps `accountText`'s.
 */
const ACCOUNT_SAY: Readonly<Record<string, string>> = {
  absent:
    'The subscription account this agent’s latest attempt runs on is not shown: the API did not send it with this task (an older API, or a read that does not carry accounts).',
  unread:
    'The subscription account this agent’s latest attempt runs on was not read: the read of its account events failed or stopped short at its cap, and no older attempt’s account is shown in its place.',
  not_assigned_yet: 'The latest attempt has not been given a subscription account yet: it has not asked the pool for one.',
  not_assigned: 'The latest attempt ended without being given a subscription account.',
  no_model_call: 'This agent’s profile calls no model, so it runs on no subscription account.',
  not_pooled: 'This agent’s profile does not take a subscription token, so it never asks the account pool.',
}

/** The account fact's title: the head's sentence for a status, then any swaps `accountText` noted. */
function accountTitle(account: Task['account'], text: { title: string; known: boolean }): string {
  if (text.known) return text.title
  const say = ACCOUNT_SAY[account === null || account === undefined ? 'absent' : account.status]
  if (say === undefined) return text.title
  const gaveBack = /Gave back: .*$/.exec(text.title)?.[0]
  return gaveBack === undefined ? say : `${say} ${gaveBack}`
}

/**
 * Profile · `<class> class` (with its size, once the catalogue answers) · workflow ·
 * account · the task id, and the dispatch chips. Sans; mono only for the ids.
 * An account not read is a dash with its reason, never blank. The id is drawn
 * before the task is read, since it is all the header knows then.
 */
export function AgHeadMeta({ task, taskId = task?.id ?? null }: { task: Task | null; taskId?: string | null }) {
  const classes = useResourceClasses()
  const cls = task === null ? null : (classes?.[task.resource_class] ?? null)
  const account = task === null ? null : accountText(task.account)
  const d = task === null ? null : dispatchOf(task)
  // WHETHER ITS BROWSER WAS USED (BROWSER-USE-UI, #2322), beside the profile
  // it is a fact about: used, not used (warning), or a dash with its reason,
  // the counts on a press. Nothing for a runtime with no browser.
  const browser = task === null ? null : browserUseOf(task)
  // A WRAPPED LINE NEITHER STARTS NOR ENDS WITH A MIDDOT. G2-20 (QA
  // 2026-10-07) moved the separator from each fact's `::before` to its
  // `::after`, so a wrapped fact no longer took its dot to the head of the
  // next line; then every wrapped line ended on one (`account not read ·`,
  // QA1-FIX-32 overview-agents-27). The dot opens every fact but the first
  // (`data-sep`), hung in the gap before it, and the list clips its inline
  // overflow (agents.css), so the dot of a fact that opens a line falls
  // outside the list and is not drawn.
  // THE WORKFLOW BY ITS NAME (VQ-010): the link read `wf_65e76c6a… ›`. It
  // reads the workflow's title -- every step carries its workflow's metadata,
  // the Overview's row label reads the same (`rowTitle`) -- with the id its
  // title; a workflow submitted without one keeps the id, in mono.
  const wfTitle = task === null ? null : rowTitle(task)
  const idFact =
    taskId === null ? [] : [{ key: 'id', li: <TaskIdLine id={taskId} className="ag-head-id" /> }]
  const facts: { key: string; li: ReactNode; className?: string; title?: string }[] =
    task === null
      ? idFact
      : [
          { key: 'profile', li: <b>{task.runner_profile}</b> },
          ...(browser === null ? [] : [{ key: 'browser-use', li: <BrowserUseFact view={browser} /> }]),
          {
            key: 'class',
            title: cls === null ? 'The class catalogue has not answered, so its size is not shown.' : undefined,
            li: (
              <>
                {/* EVERY COUNT IS NAMED WHERE IT IS PRINTED (owner decision
                    2026-10-22, Console Design Calls 2, c05; visual QA run 4,
                    task-002): beside a CPU tile measured against the
                    attempt's enforced limit, a bare `standard · 4 vCPU` did
                    not say the 4 was the class's size. The class is named as
                    one, and each figure carries its unit. */}
                {task.resource_class} class
                {cls !== null && ` · ${cls.cpu} vCPU · ${cls.memory_gib} GiB`}
              </>
            ),
          },
          ...(task.workflow_id === null
            ? []
            : [
                {
                  key: 'workflow',
                  li: (
                    <>
                      workflow{' '}
                      <a
                        className={wfTitle === null ? 'ctl-link mono' : 'ctl-link'}
                        href={addressToPath(workflowHref(task.workflow_id).replace(/^#/, ''))}
                        title={task.workflow_id}
                      >
                        {wfTitle ?? task.workflow_id} ›
                      </a>
                    </>
                  ),
                },
              ]),
          ...(account === null
            ? []
            : [
                {
                  key: 'account',
                  className: account.known ? undefined : 'is-absent ag-head-why',
                  title: accountTitle(task.account, account),
                  // AN ID IS MONO, A SENTENCE IS NOT (V149, visual QA #1038):
                  // `account not read` was bold mono in a sans line, drawn as
                  // if `not read` were an account's name. Only a real account
                  // id takes the id face; a status reads as the line's words.
                  li: account.known ? (
                    <>
                      account <b className="mono">{account.text}</b>
                    </>
                  ) : (
                    <>account {account.text}</>
                  ),
                },
              ]),
          ...idFact,
        ]
  return (
    <ul className="ag-head-facts">
      {facts.map((f, i) => (
        <li key={f.key} className={f.className} title={f.title} data-sep={i > 0 ? '' : undefined}>
          {f.li}
        </li>
      ))}
      {task !== null && (
        // ONE `?` FOR THE DISPATCH CHIPS, after them: what the strategy and
        // the role mean is one topic. The carrier gets no chip -- no worker
        // reads it yet (`CARRIER_NOTE`) -- and the topic names it.
        <li className="is-nodot ag-head-dispatch">
          {d !== null && d.role !== null && (
            <span className="ag-head-dchip" title={STRATEGY_MEANS[d.strategy]}>
              {STRATEGY_LABEL[d.strategy]}
            </span>
          )}
          {/* THE `?` NEVER SITS ALONE ON A LINE (G2-20): it is held to the
              last chip, so where the chips wrap it goes with one. And it is
              `strategy-on-a-task`, not the Submit form's
              `dispatch-strategies`, which spoke of a number computed from the
              steps on the form (G2-10). */}
          <span className="ag-head-dlast">
            {d === null ? (
              <span className="ag-head-dchip is-absent">dispatch not served</span>
            ) : (
              <span className="ag-head-dchip" title={d.role !== null ? ROLE_MEANS[d.role] : STRATEGY_MEANS[d.strategy]}>
                {d.role !== null ? ROLE_SAY[d.role] : STRATEGY_LABEL[d.strategy]}
              </span>
            )}
            <HelpCard topic={d === null ? 'dispatch-absent-is-old-api' : 'strategy-on-a-task'} />
          </span>
        </li>
      )}
    </ul>
  )
}
