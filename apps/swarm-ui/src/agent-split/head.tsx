import { useContext, useEffect, useState, type ReactNode } from 'react'

import { AgParentLink } from '../AgentChildren'
import { SplitClock } from '../AgentDetail'
import { agentName, rememberAgentName, workflowHref } from '../agentlist'
import { useResourceClasses } from '../Blockers'
import { BrowserUseFact, browserUseOf } from '../BrowserUse'
import { HelpCard } from '../HelpCard'
import { LiveControl, type LiveState } from '../LiveBrowser'
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
 * THE DETAIL'S HEADER, IN TWO LINES (agent-details-v3.html A, picked
 * 2026-10-04). Line 1: the state pill, the agent's name -- its step, or what
 * it is when it stands alone -- the headline `elapsed · attempt n of N` in
 * sans, and Copy link, Stop and ✕. Under it the whole task id, printed in
 * mono with its copy button (`TaskIdLine`, #94 -- it was only the button's
 * tooltip). Then one sans meta line -- profile · class · workflow · account --
 * and the dispatch as plain-language chips.
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
  const name = task === null ? taskId : agentName(task)
  // The breadcrumb has the id and no task: it names the agent with this.
  useEffect(() => {
    if (task !== null) rememberAgentName(task)
  }, [task])
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
          {live.status === 'ok' && live.session !== null && (
            <LiveControl taskId={taskId} session={live.session} reload={reloadLive} />
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
      {/* UNDER THE NAME, THE WHOLE ID, PRINTED (#94): it was only the copy
          button's tooltip, so it had to be hovered to be read. */}
      <TaskIdLine id={taskId} className="ag-head-id" />
      <AgHeadMeta task={task} />
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
  return (
    <span className="ag-head-hl">
      <b>{el.text}</b> ·{' '}
      {runs > 0 ? `attempt ${runs} of ${task.max_attempts}` : 'no attempt yet'}
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
 * account, and the dispatch chips. Sans; mono only for the ids. An account
 * not read is a dash with its reason, never blank. The task id is the line
 * above this one (`TaskIdLine`).
 */
export function AgHeadMeta({ task }: { task: Task | null }) {
  const classes = useResourceClasses()
  const cls = task === null ? null : (classes?.[task.resource_class] ?? null)
  const account = task === null ? null : accountText(task.account)
  const d = task === null ? null : dispatchOf(task)
  // WHETHER ITS BROWSER WAS USED (BROWSER-USE-UI, #2322), beside the profile
  // it is a fact about: used, not used (warning), or a dash with its reason,
  // the counts on a press. Nothing for a runtime with no browser.
  const browser = task === null ? null : browserUseOf(task)
  // A WRAPPED LINE NEVER STARTS WITH A MIDDOT (G2-20, QA 2026-10-07). The
  // separator was each fact's `::before`, so on a phone a fact that wrapped
  // took its dot to the head of the next line (`· workflow wf_…`). It is the
  // `::after` of every fact but the last now, so it stays at the end of the
  // line it separates on.
  const facts: { key: string; li: ReactNode; className?: string; title?: string }[] =
    task === null
      ? []
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
                      <a className="ctl-link mono" href={addressToPath(workflowHref(task.workflow_id).replace(/^#/, ''))}>
                        {task.workflow_id} ›
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
        ]
  return (
    <ul className="ag-head-facts">
      {facts.map((f, i) => (
        <li key={f.key} className={f.className} title={f.title} data-sep={i < facts.length - 1 ? '' : undefined}>
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
