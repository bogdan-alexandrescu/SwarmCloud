import { Button, CIcon, Count, ToneMark } from './components'
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import {
  EVENT_PAGE_LIMIT,
  loadAgentRun,
  loadArtifactContent,
  loadTaskInputOnce,
  loadWorkflow,
  type AgentRun,
  type ResourceClasses,
} from './api'
import { ArtifactViewer, diffStat } from './ArtifactViewer'
import { absTime, gapText } from './AttemptTimeline'
import { classUnits } from './Blockers'
import { AttemptDurations } from './charts/AttemptPhases'
import { CheckpointStrip } from './charts/CheckpointStrip'
import { DiffstatChart } from './charts/Diffstat'
import { PeakMemoryChart } from './charts/PeakMemory'
import { TokenSpendChart } from './charts/TokenSpend'
import { agentName, workflowHref } from './agentlist'
import { resultIsNewest } from './dag'
import { DecisionCard } from './DecisionCard'
import { pullRequestOf } from './decision'
import { PHASE_LABEL, attemptEnd, instant, phasesFor, spanText, type AttemptEnd, type AttemptPhases, type Segment } from './duration'
import { eventKind, isTerminalEvent } from './events'
import { backendWord, eventWord, parkWord, stateWord } from './words'
import { num, type Result } from './fetch'
import { type TopicId } from './help'
import { HelpCard, HelpNote } from './HelpCard'
import { LivenessBadge, livenessOf } from './Liveness'
import { tokenCount } from './measure'
import { Absent, Mark, UtilRow, type MarkKind } from './primitives'
import { checkpointsLine, checkpointsSay, type CheckpointCount } from './CheckpointsPane'
import { useCheckpointListing, useRead, type CheckpointListing } from './RunFiles'
import { Screen, timeAgo, type ScreenReading } from './Shell'
import { StagedInputs } from './StagedInputs'
import { StopRun } from './StopRun'
import { rowClock, useNow } from './useNow'
import {
  CONCURRENCY_STATES,
  END_CAUSES,
  GIB,
  REASON_COPY,
  TERMINAL_STATES,
  accountText,
  ageSpan,
  artifactKind,
  attemptOutcome,
  bytesLabel,
  checkpointsFor,
  clockTime,
  dispatchOf,
  elapsed,
  formatDuration,
  newestHeartbeat,
  reasonCopy,
  restoredFrom,
  startedOf,
  usageOf,
  waitingLine,
  whyAgent,
  whyNeedsAction,
  type ArtifactContent,
  type ArtifactRef,
  type AttemptRow,
  type DispatchRole,
  type DispatchStrategy,
  type EndCause,
  type ElapsedPhase,
  type GitSummary,
  type ResourceClassSpec,
  type ResultSummary,
  type Task,
  type TaskEvent,
  type TaskInputCopy,
  type Tone,
} from './types'
import './styles/details.css'

/**
 * THE SPLIT'S ONE INSTANT (G2-03). `AgentSplit` computes it once -- the 1s
 * clock, capped one poll past the header's read (`rowClock`) -- and provides
 * it here, so the header's elapsed figure and every running figure on the
 * Details tab are taken at the same moment. Null outside the split, where
 * `Run` keeps its own clock.
 */
export const SplitClock = createContext<number | null>(null)

/**
 * THE OPEN RUN'S LENGTH ON THAT ONE CLOCK, or null when the task is not
 * running. `elapsed()` is the header's and the Elapsed tile's figure, so the
 * Now card's `run` chip and the Progress total use it too, rather than the
 * newest heartbeat on the event page (which lagged the tile by minutes).
 * Only a running task's own start, never an attempt that is over: the
 * duration rows' rule that the client's clock measures no ended attempt
 * holds, because a task in a run state has exactly one attempt open.
 */
function liveRun(task: Task, now: number): { ms: number; text: string } | null {
  const el = elapsed(task, now)
  const start = instant(task.started_at)
  if (el.phase !== 'running' || start === null) return null
  return { ms: Math.max(0, now - start), text: el.text }
}

/**
 * ONE AGENT RUN, IN FULL.
 *
 * WHAT CHANGED AND WHY. The previous version of this screen showed the task's
 * `result_summary` -- which `finish()` writes ONCE, at terminal state -- as if
 * it described the run. It describes the LAST ATTEMPT. A task that failed
 * twice and succeeded on the third showed attempt three's artifacts, attempt
 * three's tokens and attempt three's exit code, and the two failures were a
 * row in a five-column table with no resources, no spend and no checkpoints.
 * Everything a person opens this page to find out about a retried run was the
 * part that was missing.
 *
 * So the unit of this screen is now the ATTEMPT. Each one carries its own
 * runtime, its own requested-vs-utilised, its own tokens and cost, its own
 * checkpoints and its own error, and the run-level panels say explicitly which
 * attempt they describe.
 *
 * THE THREE THINGS THIS SCREEN MUST NOT DO, in the order they were got wrong
 * before:
 *
 *  1. Render an absent measurement as a number. Peak RSS is written at the END
 *     of an attempt, tokens are null on every attempt that ran before the
 *     worker capture shipped, and a checkpoint's size lives on an event that
 *     may be off the page. All three are em dashes with a reason, never zeros.
 *  2. Draw a ceiling it does not have. `RESOURCE_CLASSES` is served by
 *     `/v1/resource-classes`; when that read fails the bars are hatched with no
 *     fill, because an unfilled plain track reads as "0% used".
 *  3. Invent what it cannot see. Checkpoint CONTENTS are not recorded anywhere
 *     -- only id, size and uri -- so this screen says that instead of drawing a
 *     file tree.
 */
export function AgentDetailScreen({
  taskId,
  onClose,
  headed = false,
  checkpoints,
  links,
  lastLine,
}: {
  taskId: string
  onClose: () => void
  /** The split's tab switches, for the cards' links (`Run`). */
  links?: DetailLinks
  /** The split's last log line, drawn in the leading card (`Run`). */
  lastLine?: ReactNode
  /**
   * THE CHECKPOINTS TAB'S READ, when the split has one (owner QA D21,
   * 2026-10-04): the tile said 4 then 6 while the tab said `2 of 2`, two reads
   * of a moving count. Passed, the tile draws this read and no other, so the
   * two always agree; null while it has not answered. Undefined outside the
   * split, where the tile reads its own.
   */
  checkpoints?: CheckpointCount | null
  /**
   * Drawn under the split's header row (AgentSplit.tsx), which carries the
   * state pill, the name and Stop for every tab: this pane then draws no
   * stop control of its own, so there is one Stop on screen, in the header.
   */
  headed?: boolean
}) {
  // THE HEADING IS THE AGENT'S NAME ONCE THE TASK IS READ (#94): its step in
  // a workflow, its id when it stands alone. Before the read only the id is
  // known, so the heading starts there. The name is set from inside the load
  // because `Screen` hands its data to children only, and `title` is a string;
  // the whole id stays on the facts strip, with a copy control.
  const [name, setName] = useState(taskId)
  // Another agent opened in the same drawer is headed by its own id until its
  // read lands, never by the last agent's step.
  useEffect(() => setName(taskId), [taskId])
  // A read still in flight for the previous agent must not head this one.
  const shown = useRef(taskId)
  shown.current = taskId
  // NOR MAY A READ THAT LANDS AFTER THIS PANE IS GONE SET ITS NAME (#605).
  // `Screen` drops a late answer itself (its effect's `live`); this `.then`
  // runs before Screen's sees it, so it needs its own guard. Without one a
  // read that outlived its pane scheduled a React update into a torn-down
  // document: in CI, `ReferenceError: window is not defined` after
  // walk.agent.test.tsx's environment was gone, failing a run whose every
  // test had passed.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const load = useCallback(
    () =>
      loadAgentRun(taskId).then((r) => {
        if (mounted.current && shown.current === taskId && (r.status === 'ok' || r.status === 'stale')) setName(agentName(r.data.task))
        return r
      }),
    [taskId],
  )
  // `Screen` owns its own retry nonce and does not expose it to children, so
  // the stop control needs one of its own: bumping this re-keys `Screen`,
  // which remounts it and re-runs the load. It is in the key rather than in a
  // prop for the reason the comment below gives -- `Screen`'s effect depends
  // on its nonce alone, so nothing short of a remount refetches.
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads((n) => n + 1), [])

  // HEADED, THE SPLIT IS THE DRAWER. `AgentSplit` is itself the one
  // `role="dialog"` and draws the one close control, and wraps this in
  // `.ag-split-pane`, so the old `.ctl-drawer > .drawer` flattening no longer
  // matched and this component's own fixed `.drawer` drew a second panel over
  // it (measured live, 1440x900). Headed, it draws neither the wrapper nor its
  // close button; standalone use keeps both.
  const screenEl = (
      <Screen
        // KEYED ON THE TASK, AND THIS IS NOT A DETAIL. `Screen` re-runs its
        // load effect on its retry nonce only (`[nonce]`, with the exhaustive
        // -deps rule disabled), so when the drawer stays mounted and `taskId`
        // changes -- clicking a second agent while this one is open -- the
        // effect never fires again. The heading updated and the body did not,
        // so the page showed ANOTHER task's attempts, checkpoints and spend
        // under this task's id: not stale data, wrong data, which is worse
        // than either. The key remounts `Screen`, which is the fix available
        // from inside this file; Shell.tsx belongs to another track and the
        // dependency list is theirs to widen.
        key={`${taskId}:${reloads}`}
        title={name}
        load={load}
        // THE DRAWER RE-READS (AG-2). It read once, so a ticking clock over it
        // could only slide `live 36s ago` into `silent` against events that
        // were merely old -- the clock alone makes a live agent look dead.
        // `drawerPoll` stops once a finished task's last writes are in:
        // nothing it draws changes after that.
        pollMs={drawerPoll}
        // HEADED, THE SPLIT'S HEADER BLOCK SAYS PROFILE · CLASS ONCE
        // (walkthrough B, 2026-10-03): this line repeated it under it.
        summary={headed ? undefined : (r) => (
          <>
            {r.task.runner_profile} · {r.task.resource_class} ·{' '}
            {r.attempts === null
              ? 'attempts unread'
              : `${r.attempts.length} attempt${r.attempts.length === 1 ? '' : 's'}`}
          </>
        )}
      >
        {/* THE READING GOES DOWN WITH THE RUN. `Run` caps its clock at one
            poll past it, and the checkpoint and log panels re-read when it
            moves, so every part of the drawer is as of the same read. */}
        {(r, reading) => <Run run={r} reload={reload} reading={reading} headed={headed} checkpoints={checkpoints} links={links} lastLine={lastLine} />}
      </Screen>
  )
  if (headed) return screenEl
  return (
    <div className="drawer" role="dialog" aria-label={`Agent ${taskId}`}>
      <Button iconOnly icon={<CIcon name="close" />} className="drawer-close" onClick={onClose}>
        Close
      </Button>
      {screenEl}
    </div>
  )
}

/**
 * HOW OFTEN THE OPEN DRAWER RE-READS ITS RUN, or null to stop.
 *
 * 10s, not the Agents list's 5s: one read here is FOUR requests -- the task,
 * its events, its attempts and the resource-class catalogue (`loadAgentRun`)
 * -- and a successful one is followed by two more, the checkpoint listing and
 * the log window (RunFiles.tsx), which re-read with it. Six requests per 10s
 * is 0.6 rps against the 20 rps per-principal budget, and a heartbeat lands
 * only every ~150s anyway. A finished task stops once its last writes are in
 * (`drawerPoll`): its documents are final after that, and polling them is
 * spend with nothing to learn.
 */
export const DRAWER_POLL_MS = 10_000

/**
 * HOW LONG A FINISH MAY TAKE TO BECOME WHOLE, measured from the task's
 * `completed_at`, before the drawer stops waiting for it.
 *
 * `finish()` (agent_worker/control.py) writes a terminal task in three steps:
 * the state and `completed_at` (`transition`), then the attempt's end
 * (`record_attempt_end`), then the terminal event (`emit`). They land within
 * moments of each other, and `loadAgentRun` reads the task, the events and
 * the attempts in parallel, so a poll can land between them. Some finishes are
 * never whole: a cancel of a PARKED task leaves its attempt with no end, and a
 * run with more events than one page never shows its terminal event. 30s is
 * three more reads for the first kind to complete, and a bound on the second.
 * A chosen value, not a measured one: nothing publishes how long `finish()`
 * takes, and the three writes are one round trip apart.
 */
export const DRAWER_SETTLE_MS = 30_000

/**
 * Whether a terminal run's last writes are all on this read: the attempt's end
 * (when there is an attempt) and the terminal event. A failed attempt or event
 * read is not settled -- another read may succeed.
 */
function finishSettled(r: AgentRun): boolean {
  if (r.events === null || !r.events.some(isTerminalEvent)) return false
  if (r.attempts === null) return false
  const latest = [...r.attempts].sort((x, y) => x.created_at.localeCompare(y.created_at)).at(-1)
  return latest === undefined || latest.completed_at !== null
}

/**
 * THE DRAWER STOPS ON A SETTLED READ, NOT ON THE FIRST TERMINAL ONE.
 *
 * It stopped on the first read that showed a terminal state. A read that
 * landed between `finish()`'s writes stopped it on SUCCEEDED with the attempt
 * still open and no terminal event, and nothing corrected it: the attempt chip
 * said `ended, no end recorded`, the run length said no finish time was ever
 * written, and the Timeline marked the terminal event missing -- all false a
 * moment later, and on screen for as long as the drawer stayed open. Watching a
 * task finish is the flow AG-2 exists for.
 *
 * So a terminal run is re-read until its finish is whole, or until its
 * `completed_at` is `DRAWER_SETTLE_MS` old. The age is taken either way round
 * (`Math.abs`): `completed_at` is the server's clock and `now` is this
 * browser's, and a browser running behind must not keep an old finish
 * polling until its own clock catches up.
 */
export function drawerPoll(r: AgentRun | null, now: number = Date.now()): number | null {
  if (r === null || !TERMINAL_STATES.has(r.task.state)) return DRAWER_POLL_MS
  if (finishSettled(r)) return null
  const done = r.task.completed_at === null ? Number.NaN : Date.parse(r.task.completed_at)
  return Number.isFinite(done) && Math.abs(now - done) <= DRAWER_SETTLE_MS ? DRAWER_POLL_MS : null
}

/**
 * The screen's body, once the load has resolved.
 *
 * EXPORTED FOR ONE REASON. `tests/agentdetail.test.tsx` is the acceptance test
 * for B7.1 -- it renders this screen with a cost that was never reported and
 * asserts that the surface, with every help card CLOSED, still tells absent
 * from zero. `AgentDetailScreen` above wraps this in `Screen`, whose load runs
 * in an effect, so rendering that statically yields a skeleton and would make
 * the acceptance test assert nothing at all. The test drives the real
 * component with a real `AgentRun` instead.
 *
 * `reload` is OPTIONAL for the same reason. The stop control needs it to
 * refresh after a cancel, and the screen always passes it -- but requiring it
 * would force the acceptance test to invent a stub, and a test that has to
 * fabricate a callback in order to assert on STATIC markup is asserting on its
 * own scaffolding. Absent, the stop control simply has nothing to call.
 */
export function Run({
  run,
  reload,
  reading,
  headed = false,
  checkpoints,
  links,
  lastLine,
}: {
  run: AgentRun
  /** The Checkpoints tab's read, for the tile (`AgentDetailScreen`). */
  checkpoints?: CheckpointCount | null
  reload?: () => void
  /** The split's tab switches, for each card's link; absent outside the split. */
  links?: DetailLinks
  /** The split's one-line last log line (AgentLogs.tsx `LogLastLine`), drawn in the leading card. */
  lastLine?: ReactNode
  /** Under the split's header row, which holds Stop; see `AgentDetailScreen`. */
  headed?: boolean
  /**
   * When this run was read and how often the drawer re-reads it, from
   * `Screen`. OPTIONAL for the reason `reload` is: the acceptance test renders
   * this body with no `Screen` around it, where there is no read to age and
   * nothing re-reads, so the clock runs uncapped and the panels read once.
   */
  reading?: ScreenReading
}) {
  const { task, events } = run
  // THE SHARED CLOCK (AG-2). This was `Date.now()` taken once at render, so
  // `run`, `Elapsed` and the liveness badge's `live 36s ago` never moved. One
  // 1s clock -- the one the Agents list's elapsed column reads -- re-renders
  // the drawer, and LivenessBadge ages against the same instant.
  //
  // AND IT STOPS WHERE THE READ STOPS VOUCHING FOR IT, as the Agents list's
  // rows do (`rowClock`). When a re-read fails, `Screen` keeps the last good
  // run on screen, dimmed, and backs off for up to five minutes -- and the
  // clock went on aging that read: a worker read at `live 20s ago` became
  // `silent 7m ago`, "the worker may be gone", when only the reads had failed.
  // `livenessOf` answers `unknown` for an event read that failed precisely to
  // keep "could not look" apart from "nothing happened"; a stale read is the
  // same thing, so the figures stop one poll past it. A FINISHED task is not
  // capped: every age it shows is measured from an instant that will not move,
  // so `finished 3m ago` is as true an hour later as the clock says.
  //
  // ONE INSTANT FOR THE WHOLE SPLIT (G2-03, QA 2026-10-07). One running task
  // showed `17m 33s` in the header and `17m 39s` on the Elapsed tile: the
  // same clock, capped by two different reads (the header's and this pane's),
  // so the two figures stopped at different instants. Inside the split the
  // header's instant is handed down (`SplitClock`) and this pane draws every
  // running figure at it.
  const clock = useNow(1000)
  const shared = useContext(SplitClock)
  const now = TERMINAL_STATES.has(task.state)
    ? clock
    : shared !== null
      ? shared
      : reading === undefined
        ? clock
        : rowClock(clock, reading.fetchedAt, reading.pollMs ?? DRAWER_POLL_MS)
  // ONE LISTING, FOR THE TILE (#103). The Checkpoints tile says how many of
  // the checkpoints written are still in the bucket. The Checkpoints TAB
  // lists them (`CheckpointsPane`); Details no longer draws that panel.
  const listing = useCheckpointListing(task, run.attempts, reading?.fetchedAt ?? null)

  const lead = leadOf(task)
  const readAt = reading?.fetchedAt ?? null
  const stop = !headed && reload !== undefined ? (
    <span className="run-stop">
      <StopRun task={task} what="this agent" reload={reload} />
    </span>
  ) : null

  return (
    /* ONE CARD LEADS, THEN THE FIVE FIGURES, THEN TWO COLUMNS
       (agent-details-v3.html A). The failure leads a failed agent and the
       outcome a finished one; a running one leads with what it is doing now.
       `Why` and the alerts stay above it: they render nothing when there is
       nothing to say, so a healthy run pays no space for them. On a phone the
       strip moves above the leading card (details.css), so the figures come
       first on a small screen. */
    <div className="run-stack dt">
      <div className="dt-top">
        <Alerts task={task} />
        <Why task={task} events={events} now={now} classes={run.classes} />
      </div>
      {/* WHY THE AGENT RAN OR DID NOT (owner request 2026-10-07): a step with
          a verdict gate, or the review that gated one, leads with the rule,
          the verdict, what the review weighed and what happened instead.
          Nothing on any other step. */}
      <DecisionCard task={task} readAt={readAt} />
      {lead === 'failed' ? (
        <DtFailure run={run} links={links} lastLine={lastLine} />
      ) : lead === 'outcome' ? (
        <Output run={run} readAt={readAt} lead={{ now, listing, links, lastLine, checkpoints }} />
      ) : (
        <DtNow run={run} now={now} listing={listing} checkpoints={checkpoints} lastLine={lastLine} stop={stop} />
      )}
      {lead !== 'failed' && <ErrorBanner run={run} />}
      <DtStrip run={run} now={now} />
      <div className="dt-cols">
        <DtProgress run={run} now={now} links={links} />
        <DtResources run={run} now={now} />
      </div>
      {/* THE OUTPUT CARD ONLY ONCE THERE IS OUTPUT: a result summary, or a
          publish the worker refused. Before that the Now card says `Output
          none yet · written when the attempt ends`. */}
      {lead !== 'outcome' && (task.result_summary != null || publishRefusals(task, run.attempts).length > 0) && (
        <Output run={run} readAt={readAt} />
      )}
      <Input run={run} readAt={readAt} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// THE DETAILS TAB, v3 (agent-details-v3.html A, picked 2026-10-04)
// ---------------------------------------------------------------------------
//
// Measured live at 1440 on a running claude-code agent, the pane was 2,770px
// tall and almost none of what answers "is this agent all right?" was above
// the fold. The pick leads with ONE card -- Now while it runs, the Outcome
// once it has finished, the failure when it failed -- then ONE stat strip,
// then Progress | Resources side by side from a 640px pane. Everything long
// (every attempt card, the charts, the whole event page, the prompt, the
// metadata) is still on this tab, folded behind a disclosure on the card it
// belongs to, so nothing measured left the screen: it moved one click down.
//
// THE `Dt` PREFIX marks what this lane built locally because the canonical
// components lane (U0) was building in parallel: a later pass swaps these
// for U0's without hunting for them.

/** What a Details link opens: the split passes its own tab switches. */
export interface DetailLinks {
  logs: () => void
  attempts: () => void
  artifacts: () => void
}

/** Which card leads: the failure, the outcome, or what the agent is doing now. */
function leadOf(task: Task): 'failed' | 'outcome' | 'now' {
  if (!TERMINAL_STATES.has(task.state)) return 'now'
  if (task.state === 'SUCCEEDED') return 'outcome'
  // A CANCEL LEADS WITH THE NEUTRAL 'Cancelled' CARD whether or not an error
  // was written: a cancel with no error is not an 'Outcome'.
  if (task.state === 'CANCELLED') return 'failed'
  return task.last_error || task.state === 'FAILED' || task.state === 'DEAD_LETTERED' ? 'failed' : 'outcome'
}

/**
 * `end_cause` IN PLAIN WORDS, for the failure card's heading. The cause itself
 * stays beside it as a chip, verbatim, because it is what a reader pastes
 * into a search; the heading is what they read.
 */
const END_CAUSE_SAY: Readonly<Record<EndCause, string>> = {
  timeout: 'it ran out of time',
  cannot_start: 'it could not start',
  lost_worker: 'the worker stopped answering',
  outputs_missing: 'its outputs were missing',
  inputs_unavailable: 'its inputs could not be read',
  dispatch_failed: 'it could not be dispatched',
  runner_error: 'the agent exited with an error',
  cancel_requested: 'it was cancelled on request',
  failed_parent: 'an earlier step failed',
  cancelled_parent: 'an earlier step was cancelled',
  workflow_sweep: 'its workflow was swept',
  spec_signature_invalid: 'its workflow signature did not verify',
  merge_refused: 'the merge was refused',
  merge_failed: 'the merge failed',
  verdict_refused: 'the verdict was refused',
  verdict_failed: 'the verdict failed',
  publish_refused: 'publishing was refused',
  child_cascade: 'a child task ended it',
}

function endCauseOf(task: Task): EndCause | null {
  const c = task.end_cause
  return typeof c === 'string' && (END_CAUSES as readonly string[]).includes(c) ? (c as EndCause) : null
}

/** A card's head: its title, then its links and its one `?`. */
function DtCardHead({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="dt-card-head">
      <b>{title}</b>
      {children !== undefined && <span className="dt-card-r">{children}</span>}
    </div>
  )
}

/**
 * A link inside a card to another of the agent's tabs. A real address, so it
 * opens in a new tab and works outside the split; inside it, the split's own
 * switch moves the pane.
 */
function DtMore({ href, onClick, children }: { href: string; onClick: (() => void) | undefined; children: ReactNode }) {
  return (
    <a
      className="dt-more"
      href={href}
      onClick={
        onClick === undefined
          ? undefined
          : (e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
              e.preventDefault()
              onClick()
            }
      }
    >
      {children} ›
    </a>
  )
}

/** The address of one of this agent's tabs. */
function tabHref(task: Task, tab: 'attempts' | 'artifacts'): string {
  return `#work/task/${encodeURIComponent(task.id)}/${tab}`
}

/** The oldest-first copy of a run's attempts, and the newest of them. */
function orderedAttempts(attempts: readonly AttemptRow[] | null): { ordered: AttemptRow[]; latest: AttemptRow | null } {
  const ordered = attempts === null ? [] : [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  return { ordered, latest: ordered.at(-1) ?? null }
}

/**
 * THE CHECKPOINT FACT, for the Now and Outcome cards: how many the attempt
 * records name, and when the newest was written -- from the newest
 * `checkpoint_completed` event on the page. `attempt.checkpoints` is a list of
 * names with no times, and the event page is capped and oldest first, so on
 * a long run the time can be missing: then it says so, with a dash, rather
 * than borrowing any other instant.
 */
function DtCheckpoints({
  run,
  now,
  listing,
  checkpoints,
}: {
  run: AgentRun
  now: number
  listing: CheckpointListing | undefined
  /** The Checkpoints tab's read: when passed, the fact's only source (D21). */
  checkpoints?: CheckpointCount | null
}) {
  if (checkpoints !== undefined) return <SharedCheckpoints c={checkpoints} />
  const { attempts, events } = run
  if (attempts === null) {
    return (
      <span className="dt-nf is-absent">
        Checkpoints <b>—</b> attempt read failed
      </span>
    )
  }
  const written = attempts.reduce((t, a) => t + a.checkpoints.length, 0)
  const newest = (events ?? [])
    .filter((e) => e.type === 'checkpoint_completed')
    .map((e) => e.at)
    .sort()
    .at(-1)
  const held = listing?.state
  const bucket = held !== undefined && (held.status === 'ok' || held.status === 'stale') && held.data.listed ? held.data : null
  const cut = bucket !== null && (bucket.truncated || bucket.next_page_token !== null)
  return (
    <span className="dt-nf">
      Checkpoints <b>{written}</b>
      {written > 0 &&
        (newest !== undefined ? (
          <> · last {timeAgo(newest, now)}</>
        ) : events === null ? (
          <> · — time not read</>
        ) : (
          <> · — time not on this page</>
        ))}
      {bucket !== null && (
        <>
          {' '}
          · {bucket.total_found} kept
          {(cut || bucket.total_found !== written) && (
            <>
              {' '}
              <Mark
                kind="partial"
                say={
                  cut
                    ? `The attempt records name ${written} checkpoints written. The listing was cut before its end, so ${bucket.total_found} kept is only what it reached.`
                    : `The attempt records name ${written} checkpoints written and the listing, read to its end, finds ${bucket.total_found} kept in the bucket. The Checkpoints tab lists what the bucket keeps.`
                }
              />
            </>
          )}
        </>
      )}
    </span>
  )
}

/**
 * THE CHECKPOINT FACT FROM THE TAB'S READ (owner QA D21, 2026-10-04). While
 * an agent ran the Details figure said 4, then 6, and the tab `2 of 2`: two
 * reads of a count that moves between them. In the split the Now and Outcome
 * cards draw the tab's read, so both say the same number from the same
 * moment. A read not answered yet is said as reading; a part it could not
 * read is a dash with its reason.
 */
function SharedCheckpoints({ c }: { c: CheckpointCount | null }) {
  if (c === null) {
    return (
      <span className="dt-nf is-absent" data-checkpoints="shared">
        Checkpoints <b>reading</b>
      </span>
    )
  }
  if (c.written === null) {
    return (
      <span className="dt-nf is-absent" data-checkpoints="shared">
        Checkpoints <b>—</b> not read <Mark kind="partial" say={checkpointsSay(c)} />
      </span>
    )
  }
  const line = checkpointsLine(c)
  return (
    <span className="dt-nf" data-checkpoints="shared">
      Checkpoints <b>{c.written}</b> · {line ?? <>{c.written} written, kept not read</>}
      {(c.kept === null || c.kept !== c.written) && (
        <>
          {' '}
          <Mark kind="partial" say={checkpointsSay(c)} />
        </>
      )}
    </span>
  )
}

/**
 * THE PHASES OF THE NEWEST ATTEMPT, as chips: done (with its measured span),
 * current (the OPEN interval of the newest attempt -- "running now" is that
 * and nothing else; no field names the current phase), or over with its end
 * never written. An absent boundary draws no chip at all rather than a guess.
 */
function DtPhases({ run, now }: { run: AgentRun; now: number }) {
  const { task, attempts, events } = run
  if (attempts === null) return <span className="dt-phase is-todo">attempt not read</span>
  if (attempts.length === 0) {
    const el = elapsed(task, now)
    return (
      <span className="dt-phase is-todo">
        no attempt yet · {el.phase === 'waiting' && el.ticking ? el.text : stateWord(task.state)}
      </span>
    )
  }
  const rows = phasesFor(task, attempts, events).rows
  const row = rows.at(-1)
  if (row === undefined) return <span className="dt-phase is-todo">phases not datable</span>
  const segs = [row.queue, row.cold, ...(row.run === null ? [] : [row.run])]
  const live = liveRun(task, now)
  const chips = segs.flatMap((s): { key: Segment['phase']; cls: string; text: string; title: string | undefined }[] => {
    if (s.kind === 'absent') return []
    if (s.kind === 'closed') return [{ key: s.phase, cls: 'is-done', text: `${PHASE_LABEL[s.phase]} ${spanText(s.ms)}`, title: undefined }]
    // THE RUN IN PROGRESS IS THE ELAPSED TILE'S FIGURE (G2-03): it said
    // `run 17m 30s` -- to the newest heartbeat -- beside a tile at 17m 39s.
    if (s.phase === 'run' && s.live && live !== null) {
      return [{ key: s.phase, cls: 'is-cur', text: `${PHASE_LABEL.run} ${live.text}`, title: 'Running now: the time since this attempt started, the Elapsed figure.' }]
    }
    return [
      {
        key: s.phase,
        cls: s.live ? 'is-cur' : 'is-gap',
        text: `${PHASE_LABEL[s.phase]} ${spanText(s.atLeastMs)}${s.live ? '' : '+'}`,
        title: s.why,
      },
    ]
  })
  if (chips.length === 0) return <span className="dt-phase is-todo">phases not datable</span>
  return (
    <span className="dt-phases">
      {chips.map((c, i) => (
        <span key={c.key} className="dt-phase-step">
          {i > 0 && (
            <span className="dt-phase-arr" aria-hidden>
              ›
            </span>
          )}
          <span className={`dt-phase ${c.cls}`} title={c.title}>
            {c.text}
          </span>
        </span>
      ))}
    </span>
  )
}

/** The Now card: the phase, the last log line, the heartbeat and the newest checkpoint. */
function DtNow({
  run,
  now,
  listing,
  checkpoints,
  lastLine,
  stop,
}: {
  run: AgentRun
  now: number
  listing: CheckpointListing | undefined
  /** The Checkpoints tab's read (D21); undefined outside the split. */
  checkpoints?: CheckpointCount | null
  lastLine: ReactNode
  stop: ReactNode
}) {
  const { task, events } = run
  const live = livenessOf(task, events, now)
  return (
    <section className={`dt-card dt-now${live.kind === 'silent' ? ' is-warn' : ''}`} data-lead="now">
      <DtCardHead title="Now">
        {stop}
        <HelpCard topic="checkpoints" />
      </DtCardHead>
      <DtPhases run={run} now={now} />
      {lastLine}
      <p className="dt-nfs">
        {live.kind !== 'finished' && (
          <span className="dt-nf">
            Heartbeat <LivenessBadge task={task} events={events} now={now} />
          </span>
        )}
        <DtCheckpoints run={run} now={now} listing={listing} checkpoints={checkpoints} />
        {task.result_summary == null && (
          <span className="dt-nf">
            Output <b>none yet</b> · written when the attempt ends
          </span>
        )}
      </p>
      {live.kind === 'silent' && (
        <p className="why-full is-warn" data-why="silent">
          {live.say}
        </p>
      )}
    </section>
  )
}

/**
 * THE FAILURE CARD, which leads a failed agent: a plain sentence for its end
 * cause, the cause itself verbatim, who wrote the error (`errorWriter`) and
 * the whole error -- never truncated, it is the reason the page was opened --
 * then the last thing it logged.
 */
function DtFailure({ run, links, lastLine }: { run: AgentRun; links: DetailLinks | undefined; lastLine: ReactNode }) {
  const { task } = run
  const cause = endCauseOf(task)
  const summary = (task.result_summary ?? null) as ResultSummary | null
  const git = summary?.git
  const nothing = typeof git === 'object' && git !== null && !Array.isArray(git) && publishedNothing(task, git)
  const heading = nothing
    ? 'Failed: nothing to publish'
    : cause !== null
      ? `${task.state === 'CANCELLED' ? 'Cancelled' : 'Failed'}: ${END_CAUSE_SAY[cause]}`
      : task.state === 'CANCELLED'
        ? 'Cancelled'
        : 'Failed'
  const text = task.last_error
  return (
    // A CANCEL IS NOT A FAILURE: it leads the same way, on the neutral rule,
    // so an operator's own Stop does not read as the platform's fault.
    <section className={`dt-card dt-now ${task.state === 'CANCELLED' ? 'is-neu' : 'is-bad'}`} data-lead="failed">
      <DtCardHead title={heading}>
        <DtMore href={tabHref(task, 'attempts')} onClick={links?.attempts}>
          Attempts
        </DtMore>
        <HelpCard topic="attempt-documents" />
      </DtCardHead>
      <p className="dt-nfs">
        {cause !== null ? (
          <span className="dt-chip">{cause}</span>
        ) : (
          <span className="dt-nf is-absent" title="This API wrote no end cause on the task, so why it ended is only what the error below says.">
            end cause — not recorded
          </span>
        )}
        {anythingRan(task, run.attempts) ? (
          <span className="dt-nf">
            after {task.attempt_count} of {task.max_attempts} attempts
          </span>
        ) : (
          // NOTHING RAN, SO NOTHING WAS DUE (AG-8): a cascade cancel, a
          // cancel before admission, a dispatch that never came up. A real
          // zero, not a missing summary.
          <span className="dt-nf">
            <Mark
              kind="zero"
              say="No attempt of this task ever started, so no result summary was ever going to be written. This is a real zero, not a missing summary."
            />{' '}
            nothing ran · {stateWord(task.state)}
          </span>
        )}
      </p>
      {text ? (
        <>
          <span className="dt-who">
            Error written by <b>{errorWriter(run)}</b>
          </span>
          <pre className="err full dt-err">{text}</pre>
        </>
      ) : (
        <p className="dt-nf is-absent">No error text was written on the task.</p>
      )}
      {lastLine}
    </section>
  )
}

// ---------------------------------------------------------------------------
// The stat strip
// ---------------------------------------------------------------------------

type DtTone = 'absent' | 'unread' | 'reading' | 'alert' | 'live'

interface DtCell {
  label: string
  value: ReactNode
  /** The small part of the value: `of 3`, `24%`, `/ 2 cores`. */
  unit?: string
  sub?: ReactNode
  tone?: DtTone
  /** Percent of the limit, or null for a hatched track (no ceiling, or no figure). Undefined: no bar. */
  bar?: number | null
  help?: TopicId
}

/**
 * One strip cell. A value not due, not read or not recorded is small and
 * muted: never full size, never 0. The cell's topic is PUBLISHED at its label
 * (`HelpNote`, read by a screen reader, drawn nowhere), as the tiles' `explain`
 * did: the strip is one card, and its `?`s are the cards' below.
 */
function DtStatCell({ c }: { c: DtCell }) {
  const id = useId()
  return (
    <div className={`dt-sc${c.tone ? ` is-${c.tone}` : ''}`}>
      <span className="dt-sc-l" aria-describedby={c.help === undefined ? undefined : id}>
        {c.label}
        {c.help !== undefined && <HelpNote topic={c.help} id={id} />}
      </span>
      <span className="dt-sc-v">
        {c.value}
        {c.unit !== undefined && <small> {c.unit}</small>}
      </span>
      {c.bar !== undefined && <DtBar pct={c.bar} warn={c.tone === 'alert'} />}
      {c.sub !== undefined && c.sub !== '' && <span className="dt-sc-s">{c.sub}</span>}
    </div>
  )
}

/** A thin bar against a limit; hatched, never empty, when there is no figure or no ceiling. */
function DtBar({ pct, warn = false }: { pct: number | null; warn?: boolean }) {
  if (pct === null) return <span className="dt-bar is-hatch" aria-hidden />
  const w = Math.max(0, Math.min(100, pct))
  return (
    <span className="dt-bar" aria-hidden>
      <i className={warn || w >= 90 ? 'is-warn' : undefined} style={{ width: `${w}%` }} />
    </span>
  )
}

function pctOf(v: number, of: number): number {
  return Math.round((v / of) * 100)
}

/** A core count as the strip prints it: one decimal, `2` for a whole limit. */
function cores(n: number): string {
  return Number.isInteger(n) ? `${n}` : n.toFixed(1)
}

/**
 * THE MEMORY FIGURE, decided once for the strip and the Resources card.
 * Final peak when an attempt wrote one; the heartbeat's high-water mark so
 * far while the newest attempt runs, labelled with its age; the LAST reading
 * when the attempt is over and wrote no peak (a lost worker), labelled as
 * that and never as a peak.
 */
interface MemoryFigure {
  kind: 'peak' | 'live' | 'last' | 'pending' | 'absent' | 'unread'
  bytes: number | null
  at: string | null
  measured: number
}

function memoryOf(run: AgentRun): MemoryFigure {
  const { task, attempts, events } = run
  if (attempts === null) return { kind: 'unread', bytes: null, at: null, measured: 0 }
  const { latest } = orderedAttempts(attempts)
  const measured = attempts.filter((a) => a.peak_rss_bytes !== null)
  if (measured.length > 0) {
    return { kind: 'peak', bytes: Math.max(...measured.map((a) => a.peak_rss_bytes ?? 0)), at: null, measured: measured.length }
  }
  if (latest === null) return { kind: 'absent', bytes: null, at: null, measured: 0 }
  const open = !attemptEnd(latest, task, true).over
  const hb = newestHeartbeat(latest, events)
  if (hb?.peakRssBytes != null) return { kind: open ? 'live' : 'last', bytes: hb.peakRssBytes, at: hb.at, measured: 0 }
  return { kind: open ? 'pending' : 'absent', bytes: null, at: null, measured: 0 }
}

function DtStrip({ run, now }: { run: AgentRun; now: number }) {
  const { task, attempts, events } = run
  const el = elapsed(task, now)
  const cls = run.classes?.[task.resource_class] ?? null
  const noCeiling = ceilingNote(run)
  // WHEN IT STARTED AND ENDED ARE THE ELAPSED FIGURE'S SUB-LINE (agent-
  // details-v3.html A: `since 14:02`, `13:18 → 13:41`), in the one formatter
  // (`clockTime`): local time, the UTC instant and its age in the title.
  const start = clockTime(task.started_at, now)
  const end = clockTime(task.completed_at, now)
  // A WAIT IS NAMED BY THE LABEL, NOT THE VALUE (G2-13, QA 2026-10-07): a
  // never-started parked task's tile read `waiting 19…`, the figure cut by
  // the tile's one-line value. The word moves up to the label and the value
  // is the duration alone, from the same `elapsed()` instant.
  const created = instant(task.created_at)
  const waited = el.phase === 'waiting' && el.ticking && created !== null ? formatDuration(now - created) : null
  const cells: DtCell[] = [
    {
      label: waited !== null ? 'Waiting' : 'Elapsed',
      value: waited ?? el.text,
      sub:
        el.phase === 'running' && start !== null ? (
          <span title={`started ${start.title}`}>since {start.text}</span>
        ) : el.phase === 'ran' && start !== null && end !== null ? (
          <span title={`started ${start.title} · ended ${end.title}`}>
            {start.text} → {end.text}
          </span>
        ) : (
          elapsedNote(task, el.phase, now)
        ),
    },
  ]

  if (attempts === null) {
    cells.push(
      { label: 'Attempt', value: `${task.attempt_count}`, unit: `of ${task.max_attempts}`, sub: 'task counter' },
      { label: 'Peak memory', value: '— read failed', tone: 'unread', bar: null, sub: 'attempt query', help: 'read-failed' },
      { label: 'CPU', value: '— read failed', tone: 'unread', bar: null, sub: 'attempt query' },
      { label: 'Cost', value: '— read failed', tone: 'unread', sub: 'attempt query' },
    )
    return <DtStripView cells={cells} />
  }

  const { latest } = orderedAttempts(attempts)
  const open = latest !== null && !attemptEnd(latest, task, true).over ? latest : null
  const retries = attempts.length - 1
  cells.push({
    label: 'Attempt',
    value: `${attempts.length}`,
    unit: `of ${task.max_attempts}`,
    // THE TWO NUMBERS ARE THE FACT when they disagree: what the task counts
    // against the documents that came back.
    sub:
      attempts.length !== task.attempt_count
        ? `task counts ${task.attempt_count}`
        : attempts.length === 0
          ? 'none admitted yet'
          : retries === 0
            ? 'no retries'
            : `${retries} ${retries === 1 ? 'retry' : 'retries'}`,
    tone: attempts.length === task.attempt_count ? undefined : 'unread',
  })

  // MEMORY, against the class's limit when the catalogue answered.
  const mem = memoryOf(run)
  const nearMiss = attempts.some((a) => a.oom_near_miss)
  const limit = cls === null ? null : cls.memory_gib * GIB
  const memPct = mem.bytes !== null && limit !== null ? pctOf(mem.bytes, limit) : null
  const ofLimit = cls === null ? (noCeiling ?? 'limit unknown') : `of ${cls.memory_gib} GiB`
  if (mem.bytes !== null) {
    cells.push({
      label: mem.kind === 'last' ? 'Memory · last reading' : 'Peak memory',
      value: bytesLabel(mem.bytes),
      unit: memPct === null ? undefined : `${memPct}%`,
      bar: memPct,
      sub:
        mem.kind === 'live'
          ? `${ofLimit} · so far · ${timeAgo(mem.at ?? '', now)}`
          : mem.kind === 'last'
            ? `${ofLimit} · no peak was written`
            : // A PEAK, WRITTEN AT EXIT (G2-11, QA 2026-10-07): `at exit` alone
              // read as the value at exit, and 26.7 MiB for a Claude Code run
              // looked like a residue. The worker records the high-water mark
              // it sampled (`peak_rss_bytes`, agent_worker/metrics.py) and
              // writes it when the attempt ends.
              `${ofLimit} · ${attempts.length > 1 ? `worst of ${mem.measured}` : 'peak, written at exit'}${nearMiss ? ' · OOM near miss' : ''}`,
      // THE LIVE HIGH-WATER MARK IS NOT THE FINAL FIGURE (AG-4): full size,
      // because it is a real measurement, but on its own tone and labelled
      // `so far` with its age.
      tone: nearMiss || mem.kind === 'last' ? 'alert' : mem.kind === 'live' ? 'live' : undefined,
      help: 'peak-memory',
    })
  } else if (mem.kind === 'pending') {
    cells.push({ label: 'Peak memory', value: 'at exit', tone: 'reading', bar: null, sub: `${ofLimit} · no heartbeat reading yet`, help: 'peak-memory' })
  } else if (latest === null) {
    // ONE CAUSE, ONE WORD (QA G2-28): with no attempt, Peak memory, CPU and
    // Cost each say `no attempt`. They said `not recorded`, `no attempt yet`
    // and `nothing ran yet` for the same fact, and `yet` on a cancelled task.
    cells.push({ label: 'Peak memory', value: '— no attempt', tone: 'absent', bar: null, help: 'peak-memory' })
  } else {
    cells.push({
      label: 'Peak memory',
      value: '— not recorded',
      tone: 'absent',
      bar: null,
      sub: 'no attempt wrote one',
      help: 'peak-memory',
    })
  }

  // CPU: the newest attempt's peak against its limit, the mean under it.
  cells.push(cpuCell(latest, open !== null, events, cls))

  // COST, with tokens as its sub-line (#322: every kind summed on its own).
  const reports = reportsSpend(task.runner_profile)
  const spent = attempts.filter((a) => a.cost_usd !== null)
  const cost = spent.length === 0 ? null : spent.reduce((t, a) => t + (a.cost_usd ?? 0), 0)
  const kinds = tokenKinds(task, attempts)
  const kindTotal = tokenTotal(kinds)
  const withTokens = attempts.filter((a) => reportedTokens(a) || a.attempt_id === kinds.fromSummary).length
  // THE HEADLINE IS ALL FOUR KINDS, THE CAPTION EACH ONE (#322, owner
  // decision 2026-09-29): `1.41M tokens`, then `in 52 · out 9,955 · cache
  // read 1.34M · write 59.7k` under it. Coverage only when it is not whole.
  const tokenLine: ReactNode =
    kindTotal === null ? null : (
      <>
        {tokenCount(kindTotal)} tokens
        {withTokens === attempts.length ? '' : ` · ${withTokens} of ${attempts.length} attempts reported`}
        <span className="dt-sc-k">{tokenKindsNote(kinds)}</span>
      </>
    )
  if (!reports) {
    // NO MODEL CALL IS A FACT, NOT A GAP (owner decision, 2026-09-29).
    cells.push({ label: 'Cost', value: 'no model call', tone: 'absent', sub: 'this profile reports no spend', help: 'token-cost' })
  } else if (cost === null && open !== null) {
    cells.push({ label: 'Cost', value: 'at exit', tone: 'reading', sub: tokenLine ?? 'with tokens, when the attempt ends', help: 'token-cost' })
  } else if (cost === null && latest === null) {
    cells.push({ label: 'Cost', value: '— no attempt', tone: 'absent', ...(tokenLine === null ? {} : { sub: tokenLine }), help: 'token-cost' })
  } else if (cost === null) {
    cells.push({
      label: 'Cost',
      value: '— not reported',
      tone: 'absent',
      sub: tokenLine ?? 'tokens not reported',
      help: 'token-cost',
    })
  } else {
    // THE TOTAL OVER EVERY ATTEMPT, the last attempt as secondary text (lane
    // review P1, 2026-10-05): `result_summary` is the last attempt's alone, and
    // a retried task read from it under-reported every earlier attempt. An
    // attempt with no figure makes the total a floor, and it says `at least`
    // rather than a sum that looks whole.
    const lastCost = attempts.length > 1 && latest !== null ? latest.cost_usd : undefined
    cells.push({
      label: 'Cost',
      value: spent.length < attempts.length ? <>at least {usd(cost)}</> : usd(cost),
      sub: (
        <>
          {spent.length !== attempts.length && `cost ${spent.length} of ${attempts.length} reported · `}
          {lastCost !== undefined && (
            <>
              last attempt {lastCost === null ? 'not reported' : usd(lastCost)}
              {' · '}
            </>
          )}
          {tokenLine ?? 'tokens not reported'}
        </>
      ),
      // A gap that is only the open attempt is pending; an ended attempt with no figure is a hole.
      tone:
        spent.length === attempts.length
          ? undefined
          : attempts.every((a) => a.cost_usd !== null || a === open)
            ? 'reading'
            : 'unread',
      help: 'token-cost',
    })
  }
  return <DtStripView cells={cells} />
}

function DtStripView({ cells }: { cells: DtCell[] }) {
  return (
    <div className="dt-strip">
      {cells.map((c) => (
        <DtStatCell key={c.label} c={c} />
      ))}
    </div>
  )
}

/** The CPU cell: the newest attempt's peak against its limit, its mean in the sub-line. */
function cpuCell(latest: AttemptRow | null, open: boolean, events: TaskEvent[] | null, cls: ResourceClassSpec | null): DtCell {
  if (latest === null) return { label: 'CPU', value: '— no attempt', tone: 'absent', bar: null }
  const from = cpuOf(latest, events)
  if (from.kind === 'not_served') {
    return { label: 'CPU', value: '— not served', tone: 'absent', bar: null, sub: 'this API sends no CPU fields', help: 'cpu-figures' }
  }
  if (from.kind === 'unread') {
    return { label: 'CPU', value: '— events unread', tone: 'unread', bar: null, sub: 'heartbeat read failed', help: 'cpu-figures' }
  }
  const f = from.figures
  const limit = f.cpu_limit_cores ?? cls?.cpu ?? null
  if (f.peak_cpu_cores === null) {
    return open
      ? { label: 'CPU', value: 'at exit', tone: 'reading', bar: null, sub: 'no reading yet', help: 'cpu-figures' }
      : { label: 'CPU', value: '— not measured', tone: 'absent', bar: null, sub: 'no figure was written', help: 'cpu-figures' }
  }
  const pct = limit === null ? null : pctOf(f.peak_cpu_cores, limit)
  return {
    label: 'CPU',
    value: cores(f.peak_cpu_cores),
    unit: limit === null ? 'cores · limit unknown' : `/ ${cores(limit)} cores`,
    bar: pct,
    tone: pct !== null && pct >= 100 ? 'alert' : undefined,
    sub: `${from.kind === 'heartbeat' && !from.final ? 'last reading' : 'peak'}${f.mean_cpu_cores === null ? '' : ` · mean ${cores(f.mean_cpu_cores)}`}`,
    help: 'cpu-figures',
  }
}

// ---------------------------------------------------------------------------
// Progress and Resources
// ---------------------------------------------------------------------------

/** A local HH:MM for an event row; the whole instant is its title. */
function hhmm(iso: string): string {
  const t = new Date(iso)
  if (!Number.isFinite(t.getTime())) return '—'
  return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`
}

/**
 * One event row: its local time (the absolute instant in its title, as the
 * Attempts tab writes it), its kind in words, the gap since the event before
 * it on the page, and whose attempt it was. No JSON: the detail is on the
 * Attempts tab.
 */
function DtEventRow({ e, prev, owner }: { e: TaskEvent; prev: TaskEvent | undefined; owner: string | null }) {
  const kind = eventKind(e)
  const bad = /fail|lost|expired|refused/.test(kind)
  const at = instant(e.at)
  const before = prev === undefined ? null : instant(prev.at)
  return (
    <li className={`dt-ev${bad ? ' is-bad' : ''}`} data-kind={kind}>
      <time className="dt-ev-t mono" dateTime={e.at} title={absTime(e.at)}>
        {hhmm(e.at)}
      </time>
      <span>
        <span className="dt-ev-k">{eventWord(e)}</span>
        {at !== null && before !== null && <small> {gapText(at - before)}</small>}
        {owner !== null && <small> · {owner}</small>}
      </span>
    </li>
  )
}

/**
 * PROGRESS: the phases of every attempt as one compact bar each, then the last
 * five events NEWEST FIRST -- what "what is it doing" needs -- and the whole
 * page behind "All N events". The page's limits are said as they were: a
 * terminal task with no terminal event on the page is proven short.
 */
function DtProgress({ run, now, links }: { run: AgentRun; now: number; links: DetailLinks | undefined }) {
  const { task, events, attempts } = run
  const { ordered } = orderedAttempts(attempts)
  const rows = attempts === null || attempts.length === 0 ? [] : phasesFor(task, attempts, events).rows
  const ownerOf = (e: TaskEvent): string | null => {
    if (e.attempt_id == null || attempts === null) return null
    const i = ordered.findIndex((a) => a.attempt_id === e.attempt_id)
    return i < 0 ? 'no attempt document' : attemptLabel(i + 1, ordered[i]!.generation)
  }
  const newest = events === null ? [] : [...events].reverse()
  const endMissing = events !== null && TERMINAL_STATES.has(task.state) && !events.some(isTerminalEvent)
  const start = startedOf(task, now)
  return (
    <section className="dt-card dt-progress">
      <DtCardHead title="Progress">
        <DtMore href={tabHref(task, 'attempts')} onClick={links?.attempts}>
          Attempts tab
        </DtMore>
        <HelpCard topic="attempt-documents" />
      </DtCardHead>
      {attempts === null ? (
        <p className="dt-empty is-err">
          <b>The attempt history could not be read</b>
          <span>This is a failed read, not a task with no attempts.{run.attemptsDetail ? ` ${run.attemptsDetail}` : ''}</span>
        </p>
      ) : attempts.length === 0 ? (
        task.attempt_count > 0 ? (
          <p className="dt-empty">
            <b>
              counts {task.attempt_count} · returned 0{' '}
              <Mark
                kind="partial"
                say={`This task counts ${task.attempt_count} attempts and the query returned none, so there is a hole in the record.`}
              />
            </b>
            <span>The attempt documents are missing, not absent.</span>
          </p>
        ) : (
          // SAID BY THE TASK'S STATE (QA G2-28, 2026-10-07): "while it waits"
          // and "yet" were printed on a cancelled task, which waits for nothing
          // and will never have an attempt.
          <p className="dt-empty">
            <b>
              <Mark
                kind="zero"
                say={
                  TERMINAL_STATES.has(task.state)
                    ? 'The attempt query succeeded and returned nothing. This task ended before anything was admitted, so this is a real zero rather than a failed read.'
                    : 'The attempt query succeeded and returned nothing. Nothing has been admitted for this task yet, so this is a real zero rather than a failed read.'
                }
              />{' '}
              {TERMINAL_STATES.has(task.state) ? 'no attempt' : 'no attempt yet'} · {stateWord(task.state)}
            </b>
            <span>
              {TERMINAL_STATES.has(task.state)
                ? 'It ended before anything was admitted, so there is no phase to draw. It held no capacity.'
                : 'Nothing has been admitted, so there is no phase to draw. It holds no capacity while it waits.'}
            </span>
          </p>
        )
      ) : (
        <>
          {/* THE RIGHT-HAND FIGURE IS EACH ATTEMPT'S WHOLE SPAN (G2-03): queue
              and cold start are in it, which is why it is longer than the
              Elapsed tile, and it is labelled so. */}
          <div className="dt-phrow is-head">
            <span />
            <span />
            <span>total incl. queue</span>
          </div>
          {rows.map((r, i) => (
            <DtPhaseRow key={r.attempt.attempt_id} r={r} live={i === rows.length - 1 ? liveRun(task, now) : null} />
          ))}
          <p className="dt-lg">
            <span>
              <i className="is-q" />
              {PHASE_LABEL.queue}
            </span>
            <span>
              <i className="is-c" />
              {PHASE_LABEL.cold}
            </span>
            <span>
              <i className="is-r" />
              {PHASE_LABEL.run}
            </span>
            {rows.some((r) => r.run?.kind === 'open' && r.run.live) && <span>· open: still running</span>}
          </p>
        </>
      )}
      {events === null ? (
        <p className="dt-empty is-err">
          <b>The event history could not be read</b>
          <span>This is a failed read, not an empty history.{run.eventsDetail ? ` ${run.eventsDetail}` : ''}</span>
        </p>
      ) : events.length === 0 ? (
        <p className="dt-empty is-err">
          <b>0 events · a task always has one</b>
          <span>A submitted event is written with the task itself, and the query returned none. This is a failed read.</span>
        </p>
      ) : (
        <>
          <ol className="dt-evs">
            {newest.slice(0, 5).map((e, i) => (
              <DtEventRow key={e.event_id} e={e} prev={newest[i + 1]} owner={ownerOf(e)} />
            ))}
          </ol>
          {/* WHAT THE PAGE DOES AND DOES NOT COVER, AS ONE QUALIFIER. Proven
              short -- a terminal task with no terminal event on the page -- is
              `partial`; unproven is a plain caveat and no mark (AG-9): a
              caveat about a route's paging is not a read in flight. */}
          {endMissing ? (
            <p className="dt-note">
              <Mark
                kind="partial"
                say={`The task is ${stateWord(task.state)} and a terminal task writes a terminal event — none is on this page. This screen reads one page of events, oldest-first, and does not follow the page token the events route returns, so the newest events are not on it. The end of this task's history is missing, not absent.`}
              />{' '}
              {newest[0] === undefined
                ? 'the end of this history is not on this page'
                : `the page ends at ${eventWord(newest[0])}, ${timeAgo(newest[0].at, now)}`}
            </p>
          ) : (
            <p
              className="dt-note"
              aria-label={`One page, as served. This screen asks for ${EVENT_PAGE_LIMIT} events and the server may clamp that lower, and it does not follow the page token the events route returns — so a page that looks complete is not evidence that it is.`}
            >
              newest first · one page, cap unknown
            </p>
          )}
          {events.length > 5 && (
            <details className="dt-disc">
              <summary>All {events.length} events</summary>
              <ol className="dt-evs dt-evs-all">
                {newest.slice(5).map((e, i) => (
                  <DtEventRow key={e.event_id} e={e} prev={newest[i + 6]} owner={ownerOf(e)} />
                ))}
              </ol>
            </details>
          )}
        </>
      )}
      <p className="dt-note" title={start.submittedTitle}>
        submitted {start.submitted} · {task.submitted_by === null ? 'by — not recorded' : `by ${task.submitted_by}`} · {timeAgo(task.created_at, now)}
      </p>
    </section>
  )
}

/** One attempt's phases as a compact bar: queue, cold start, run -- open runs drawn open, never closed at now. */
function DtPhaseRow({ r, live }: { r: AttemptPhases; live: { ms: number } | null }) {
  const part = (s: Segment | null): number => (s === null ? 0 : s.kind === 'closed' ? s.ms : s.kind === 'open' ? s.atLeastMs : 0)
  const q = part(r.queue)
  const c = part(r.cold)
  // AN OPEN RUN THAT IS RUNNING NOW is measured on the split's one clock, as
  // the Elapsed tile is (G2-03), and is then a figure rather than a floor:
  // no `+`. Any other open run is still a floor at its newest event.
  const ticking = r.run?.kind === 'open' && r.run.live && live !== null
  const run = ticking ? live.ms : part(r.run)
  const total = q + c + run
  const open = r.run?.kind === 'open' && !ticking
  const failed = r.attempt.exit_code !== null && r.attempt.exit_code !== 0 && !isParked(r.attempt)
  return (
    <div className="dt-phrow">
      <span>Attempt {r.ordinal}</span>
      <span className="dt-phb" aria-hidden>
        {total > 0 && (
          <>
            <i className="is-q" style={{ flexGrow: q }} />
            <i className="is-c" style={{ flexGrow: c }} />
            <i className={`${failed ? 'is-b' : 'is-r'}${r.run?.kind === 'open' ? ' is-open' : ''}`} style={{ flexGrow: run }} />
          </>
        )}
      </span>
      <b className="mono" title="This attempt's whole span: queue, cold start and run.">
        {total > 0 ? `${spanText(total)}${open ? '+' : ''}` : '—'}
      </b>
    </div>
  )
}

/**
 * RESOURCES: thin bars with the % of each limit (requests == limits, so 100%
 * is the ceiling, not a target), and every attempt's own card and chart
 * behind "Details". A hatched track is a figure not measured yet or a
 * ceiling not read -- never a zero.
 */
function DtResources({ run, now }: { run: AgentRun; now: number }) {
  const { task, attempts, events } = run
  const cls = run.classes?.[task.resource_class] ?? null
  const noCeiling = ceilingNote(run)
  const { latest } = orderedAttempts(attempts)
  const open = latest !== null && !attemptEnd(latest, task, true).over
  const mem = memoryOf(run)
  const rows: { name: string; value: string; note: string; pct: number | null; warn?: boolean }[] = []
  const unknownLimit = noCeiling ?? 'limit unknown'
  if (attempts === null) {
    for (const name of ['Memory', 'Workspace', 'CPU peak', 'CPU mean']) rows.push({ name, value: '—', note: 'read failed', pct: null })
  } else {
    rows.push(
      mem.bytes === null
        ? { name: 'Memory', value: '—', note: latest === null ? 'no attempt' : mem.kind === 'pending' ? 'written at exit' : 'not recorded', pct: null }
        : {
            name: 'Memory',
            value: bytesLabel(mem.bytes),
            note:
              (cls === null ? unknownLimit : `of ${cls.memory_gib} GiB`) +
              (mem.kind === 'live' ? ` · so far · ${timeAgo(mem.at ?? '', now)}` : mem.kind === 'last' ? ' · last reading' : ''),
            pct: cls === null ? null : pctOf(mem.bytes, cls.memory_gib * GIB),
            warn: mem.kind === 'last',
          },
    )
    const disk = latest?.peak_disk_bytes ?? null
    rows.push(
      disk === null
        ? { name: 'Workspace', value: '—', note: latest === null ? 'no attempt' : open ? 'written at exit' : 'never written', pct: null }
        : {
            name: 'Workspace',
            value: bytesLabel(disk),
            note: cls === null ? unknownLimit : `of ${cls.disk_gib} GiB`,
            pct: cls === null ? null : pctOf(disk, cls.disk_gib * GIB),
          },
    )
    const from = latest === null ? null : cpuOf(latest, events)
    const f = from !== null && (from.kind === 'attempt' || from.kind === 'heartbeat') ? from.figures : null
    const limit = f?.cpu_limit_cores ?? cls?.cpu ?? null
    for (const [name, v] of [
      ['CPU peak', f?.peak_cpu_cores ?? null],
      ['CPU mean', f?.mean_cpu_cores ?? null],
    ] as const) {
      rows.push(
        v === null
          ? {
              name,
              value: '—',
              note:
                from === null ? 'no attempt' : from.kind === 'not_served' ? 'not served' : from.kind === 'unread' ? 'events unread' : open ? 'written at exit' : 'not measured',
              pct: null,
            }
          : { name, value: cores(v), note: limit === null ? unknownLimit : `of ${cores(limit)} cores`, pct: limit === null ? null : pctOf(v, limit) },
      )
    }
  }
  const note =
    attempts === null
      ? 'The attempt read failed, so no figure is drawn.'
      : latest === null
        ? TERMINAL_STATES.has(task.state)
          ? 'It ended before anything was admitted, so nothing was measured.'
          : 'Nothing has been admitted yet, so nothing has been measured.'
        : open
          ? 'Live readings from the attempt record. Final figures are written when the attempt ends.'
          : mem.kind === 'last'
            ? 'No figure was written at exit: these are the last readings.'
            : `Figures written at exit by attempt ${attempts.length}.`
  return (
    <section className="dt-card dt-resources">
      <DtCardHead title="Resources">
        {cls === null && <span className="dt-note">{unknownLimit}</span>}
        <HelpCard topic="requests-are-ceilings" />
      </DtCardHead>
      {rows.map((r) => (
        <div key={r.name} className="dt-rr">
          <span className="dt-rr-n">{r.name}</span>
          <DtBar pct={r.value === '—' ? null : r.pct} warn={r.warn} />
          <span className="dt-rr-v">
            <b>{r.value}</b> <small>{r.pct === null || r.value === '—' ? r.note : `${r.pct}% ${r.note}`}</small>
          </span>
        </div>
      ))}
      <p className="dt-note">{note}</p>
      <details className="dt-disc">
        <summary>Details: memory, spend and each attempt over time</summary>
        <Attempts run={run} now={now} />
      </details>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Primitives, built from the ctl-* vocabulary in styles.css
// ---------------------------------------------------------------------------

/**
 * The tones a state mark may take (the canonical `ToneMark` draws them; `wait`
 * is drawn as warn, `ended` as the flat bar). The tones a chip may take. `Tone` from types.ts is the derived one; the
 * other three are states a chip can be in that no task ever is -- an outcome
 * nobody recorded (`unknown`), a fact rather than a verdict (`info`), and work
 * an operator has held (`paused`).
 */
export type ChipTone = Tone | 'unknown' | 'info' | 'paused'

/**
 * `ExitCode.PARKED` (agent_worker/errors.py): the worker checkpointed, parked
 * the task and exited, and the attempt document records that end as exit 75
 * with the park reason in `error` (#163). An attempt parked before the worker
 * wrote that end has no exit code and no `completed_at`, and nothing on its
 * document tells it apart from one still running.
 */
export const EXIT_PARKED = 75

export function isParked(a: AttemptRow): boolean {
  return a.exit_code === EXIT_PARKED
}

/**
 * A PARKED ATTEMPT'S CHIP, for both attempt cards (here and AttemptTimeline).
 * Parked is not a failure: without this, `exit 75` drew in the failure tone
 * and a quota park read as a crash. `info` is the neutral bar -- a fact about
 * the attempt, not a verdict on it -- and the reason is the token the platform
 * wrote, verbatim, as the detail pane prints `park_reason`.
 */
export function parkedOutcome(a: AttemptRow): { label: string; tone: ChipTone } {
  return { label: a.error ? `parked · ${a.error}` : 'parked', tone: 'info' }
}


/** The em dash, as a token rather than a bare character, so "not measured" is
 *  styleable as a class of thing and never mistaken for a digit.
 *
 *  EXPORTED because the other three screens of this group -- Agents,
 *  AttemptTimeline and ArtifactViewer -- draw the same absence and were each
 *  drawing it their own way. One definition, four screens. */
export function Em() {
  return <span className="ctl-em">—</span>
}

/**
 * THE SIX KINDS OF NOTHING, AS A SHAPE RATHER THAN A SENTENCE.
 *
 * `Mark` lives in ./primitives.tsx with the other primitives this file used to
 * define (`Metric`, the utilisation track, `Absent`). It is RE-EXPORTED here
 * because Agents, AttemptTimeline, ArtifactViewer and CheckpointBrowser import
 * it from this module, and those are other lanes' files: the re-export keeps
 * one definition without editing four call sites that did nothing wrong.
 *
 * The REASONING -- why a figure can be absent, what would have written it --
 * is neither the mark nor its sentence. It is `#help/<topic>`: published at
 * the label by `explain`, indexed in the card foot, and on this screen behind
 * exactly one `?` rather than the twenty-two it had before B7.4.
 */
export { Mark, type MarkKind } from './primitives'

/**
 * THE TILE, THE TRACK AND THE EMPTY STATE ARE THE SHARED PRIMITIVES.
 *
 * `Metric` and `Absent` are imported from ./primitives.tsx and called here
 * under the names this file always used. This file defined its own of each,
 * beside Overview's, and the two had already diverged (design-system.md
 * §9.4): Overview's track drew the measured-zero baseline tick and this one
 * did not, so a workspace that peaked at 0 B read here as a widget that
 * failed to paint. The tick is now drawn on both, by the one track.
 *
 * WHAT STAYS HERE IS THIS SCREEN'S POLICY, and it is unchanged:
 *
 *   - an absent tile's value is a PHRASE ("not recorded", "not reported") on
 *     the `is-absent` treatment, where the landing strip draws a mark. Both
 *     are the design system's; `tests/agentdetail.test.tsx` renders this
 *     screen with every help card CLOSED and asserts the phrase.
 *   - `explain` publishes a topic's short form at the label and draws no `?`
 *     (B7.4). The tile already tells an absence from a zero on the surface.
 */

/**
 * used / ceiling, as one `.ctl-util` row that stays honest when either side
 * is missing.
 *
 * The TRACK is the canonical `UsageTrack`, so its four states are the product's:
 * unknown (no measurement, or no ceiling to measure it against) is hatched
 * with no fill; a measured zero draws the baseline tick; over the ceiling, the
 * track stands for what was used and the excess is hatched in the failure
 * colour rather than clipped; anything else is a fill.
 *
 * What this row decides is the VERDICT and the units. The warn/bad colouring
 * at 75% and 90% is PRESENTATION. The claim about whether an attempt came
 * dangerously close to its ceiling is `oom_near_miss`, which the worker sets
 * from its own threshold against the cgroup the kernel's OOM killer reads.
 * This bar never contradicts that flag; it just makes a tall bar visible
 * before you get to it.
 */
function CeilingRow({
  label,
  used,
  ceiling,
  fmt,
  by,
}: {
  label: ReactNode
  /** null means NOT MEASURED. */
  used: number | null
  /** null means the ceiling is unknown -- the catalogue read failed. */
  ceiling: number | null
  fmt: (v: number) => string
  /** The right-hand column: where the ceiling came from, or why `used` is absent. */
  by: string
}) {
  const known = used !== null && ceiling !== null && ceiling > 0
  const ratio = known && ceiling !== null && used !== null ? used / ceiling : null

  return (
    <UtilRow
      name={label}
      track={{
        pct: ratio === null ? null : ratio * 100,
        tone: ratio === null ? undefined : ratio >= 0.9 ? 'is-bad' : ratio >= 0.75 ? 'is-warn' : undefined,
      }}
      figure={
        <>
          {used === null ? <Em /> : fmt(used)}
          <span className="ctl-util-of"> / {ceiling === null ? '?' : fmt(ceiling)}</span>
        </>
      }
      byTitle={by}
      by={by}
    />
  )
}

/**
 * THE ATTEMPT CARD'S BOX IS NO LONGER INLINE.
 *
 * It used to be, and the comment that stood here said why: "`.panel` is a
 * heading modifier and draws no container at all, so three attempts ran
 * together into one column with nothing marking where the second began; the
 * `ctl-` block has no card primitive, so the box is inline." That was true and
 * it is the exact observation design-system.md §6.1 quotes as its reason for
 * existing. The primitive exists now, so the inline object is gone and the
 * card is `.ctl-card` / `.ctl-card-head` / `.ctl-card-body` / `.ctl-card-foot`
 * like every other card in the product.
 *
 * `SUB` stays: it is a sub-BLOCK inside a card, not a card, and `.section`'s
 * own 28px break is too much three deep.
 */
const SUB: CSSProperties = { marginBottom: 'var(--ctl-s3)' }

/**
 * `plural` IS GONE, and its absence is the prose rule in one function.
 *
 * It existed so this screen could write "3 attempts" and "1 attempt" rather
 * than "3 attempt(s)" -- correct English, and every call site of it was a
 * figure wearing a noun. A count strip reads `3` under the key `att`, a chip
 * reads `3`, and the noun is the column head or the key beside it, said once.
 * Fourteen calls to this helper were fourteen repetitions of a word the reader
 * had already read.
 */

function usd(v: number | null | undefined): ReactNode {
  // NEVER $0.00 for an unmeasured run. A zero here reads as "this was free",
  // which is the single most expensive misreading available on this screen.
  if (typeof v !== 'number' || !Number.isFinite(v)) return <Em />
  return `$${v.toFixed(4)}`
}

function tokens(v: number | null | undefined): ReactNode {
  if (typeof v !== 'number' || !Number.isFinite(v)) return <Em />
  return v.toLocaleString()
}

/**
 * Whether any attempt of this task ever reached a running worker.
 *
 * `task.started_at` is written on DISPATCHED -> STARTING, and an attempt's
 * `started_at` by the worker's `record_attempt_start`, so a task with neither
 * never had an agent to write an error, a result summary or a figure. Read
 * from both because either can be missing on its own: a failed attempt read
 * leaves the task's field, and a retried task keeps the attempt records.
 */
function anythingRan(task: Task, attempts: AttemptRow[] | null): boolean {
  return task.started_at !== null || (attempts ?? []).some((a) => a.started_at !== null)
}

/**
 * Only the CLI agents parse a usage block out of the runner's output
 * (`cliagent.run_cli_agent`); the others report no tokens and no cost by
 * construction. One predicate for the run tiles and the attempt cards, so the
 * two cannot disagree about whether a figure is coming.
 */
function reportsSpend(profile: string): boolean {
  // claude-code-gke is claude-code on GKE (contract request 55, temporary).
  return profile === 'claude-code' || profile === 'claude-code-gke' || profile === 'codex'
}

/**
 * ONE NAME FOR ONE ATTEMPT, on every pane that draws it (AG-21).
 *
 * The same attempt was `Attempt 1` with a `gen 1` chip on its card, `Attempt
 * 1 · generation 1` over its events and `Attempt · gen 1` -- no ordinal at all
 * -- in the Attempts view. A reader matching a card to its events across two
 * panes had three spellings to reconcile. The ordinal is the attempt's place
 * among the documents, oldest first; the generation is the fencing number it
 * was minted with, and the two are not the same number on a fenced task.
 *
 * EXPORTED because AttemptTimeline draws the same attempts and imports its
 * primitives from here already.
 */
export function attemptLabel(ordinal: number, generation: number): string {
  return `Attempt ${ordinal} · gen ${generation}`
}

// `agentName` and `workflowHref` live in `agentlist.ts` so the Agents list
// and Overview name an agent and link its workflow by the same rule.
export { agentName, workflowHref }

/**
 * Why there is no ceiling to read a measurement against -- or null when there
 * is one.
 *
 * `classes[task.resource_class]` is null for THREE different reasons and only
 * two of them are read failures: the catalogue read failed, this deployment
 * has no catalogue route at all, or the catalogue came back perfectly well and
 * does not contain the class this task names. The third is a class that was
 * renamed or retired after the task was submitted, and reporting it as a read
 * failure sends its reader to check an API that is answering correctly.
 * `AttemptResources` draws the same three as panels; this is the one-line form
 * a metric tile can carry.
 */
function ceilingNote(run: AgentRun): string | null {
  const { task, classes, classesRouteMissing } = run
  // THREE WORDS, NOT THREE SENTENCES. Each one still names which of the three
  // causes it is -- which is the whole reason this helper exists -- and the
  // consequence they all shared ("so the ceiling is unknown") is drawn by the
  // hatched track rather than restated in every tile that reads this.
  if (classes === null) return classesRouteMissing ? 'no catalogue route' : 'catalogue unread'
  if (classes[task.resource_class] === undefined) return `no class ${task.resource_class}`
  return null
}

/**
 * THE FOUR KINDS OF TOKEN A RUN USED, each summed on its own (#322).
 *
 * `null` IS "NO ATTEMPT REPORTED THIS KIND", never zero: `record_spend` writes
 * only the keys the runner reported, and a kind nobody wrote is left out of
 * the total and the caption rather than counted as 0.
 *
 * `modelUsage` OUTRANKS THE ATTEMPT DOCUMENT FOR THE ATTEMPT IT DESCRIBES. The
 * attempt's four fields are lifted from the CLI's top-level `usage` block,
 * which #323 found can cover only the LAST result event
 * (task_9be4128488d342208947 hid 128,570 subagent cache-write tokens).
 * `modelUsage`, per model, is the whole run. It is on `result_summary`, which
 * describes ONE attempt: the one that wrote it. That is the newest attempt
 * only once the task has finished (`resultIsNewest`, the board's borrowing
 * rule) AND the newest attempt has an end recorded -- `finish()` writes both.
 * A task on its second attempt still carries the FIRST attempt's summary
 * (`fail_retryably` writes it and sends the task back to READY), and so does
 * a finished task whose last attempt ended without writing one. Borrowing it
 * there would count the first attempt twice, so the documents are read
 * instead, and the tile says `written at exit` while the task runs.
 *
 * 5m AND 1h CACHE WRITES are split only when both durations were used and the
 * two parts add up to the write total. The parts come from `usage.cache_
 * creation`, the same last-event block, so a split that does not add up
 * covers part of the run and is not drawn; nor is one for a run whose earlier
 * attempts wrote cache with no split recorded.
 */
export interface TokenKinds {
  input: number | null
  output: number | null
  cacheRead: number | null
  cacheWrite: number | null
  write5m: number | null
  write1h: number | null
  /** The attempt whose counts came from `modelUsage`, or null when none did. */
  fromSummary: string | null
}

function count(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

function addCount(acc: number | null, v: number | null): number | null {
  return v === null ? acc : (acc ?? 0) + v
}

function record(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

/** Whether an attempt document carries any of the four counts. */
function reportedTokens(a: AttemptRow): boolean {
  return [a.input_tokens, a.output_tokens, a.cache_read_input_tokens, a.cache_creation_input_tokens].some(
    (v) => count(v) !== null,
  )
}

/** The CLI's own JSON on a summary: `runner.output.structured_output`, or null. */
function structuredOutput(task: Task): Record<string, unknown> | null {
  const runner = record((task.result_summary as ResultSummary | null)?.runner)
  return record(record(runner?.output)?.structured_output)
}

export function tokenKinds(task: Task, attempts: readonly AttemptRow[]): TokenKinds {
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  const out = structuredOutput(task)
  const models = record(out?.modelUsage)
  // AN ENTRY WITH NONE OF THE FOUR COUNTS IS NOT A MEASUREMENT: a cost-only
  // `modelUsage` displaced the newest attempt's own fields, and a run whose
  // document said `in 100 · out 50` read `tokens not reported`.
  const perModel = (models === null ? [] : Object.values(models).map(record)).filter(
    (m): m is Record<string, unknown> =>
      m !== null &&
      [m.inputTokens, m.outputTokens, m.cacheReadInputTokens, m.cacheCreationInputTokens].some((v) => count(v) !== null),
  )
  const newest = ordered.at(-1)
  const summed =
    perModel.length > 0 && resultIsNewest(task) && (newest === undefined || newest.completed_at !== null)
  const latest = summed ? newest : undefined

  let input: number | null = null
  let output: number | null = null
  let cacheRead: number | null = null
  let cacheWrite: number | null = null
  let earlierWrites = false
  for (const a of ordered) {
    if (a === latest) continue
    input = addCount(input, count(a.input_tokens))
    output = addCount(output, count(a.output_tokens))
    cacheRead = addCount(cacheRead, count(a.cache_read_input_tokens))
    const w = count(a.cache_creation_input_tokens)
    cacheWrite = addCount(cacheWrite, w)
    if (w !== null && w > 0) earlierWrites = true
  }
  let summaryWrite: number | null = null
  for (const m of summed ? perModel : []) {
    input = addCount(input, count(m.inputTokens))
    output = addCount(output, count(m.outputTokens))
    cacheRead = addCount(cacheRead, count(m.cacheReadInputTokens))
    summaryWrite = addCount(summaryWrite, count(m.cacheCreationInputTokens))
  }
  cacheWrite = addCount(cacheWrite, summaryWrite)

  const creation = record(record(out?.usage)?.cache_creation)
  const w5 = count(creation?.ephemeral_5m_input_tokens)
  const w1 = count(creation?.ephemeral_1h_input_tokens)
  const split =
    summed && !earlierWrites && w5 !== null && w1 !== null && w5 > 0 && w1 > 0 && w5 + w1 === cacheWrite
  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    write5m: split ? w5 : null,
    write1h: split ? w1 : null,
    fromSummary: latest?.attempt_id ?? null,
  }
}

/** The total over the kinds that were reported, or null when none was. */
function tokenTotal(k: TokenKinds): number | null {
  return [k.input, k.output, k.cacheRead, k.cacheWrite].reduce<number | null>((t, v) => addCount(t, v), null)
}

/** `in 52 · out 9,955 · cache read 1.34M · write 59.7k`, leaving out what nobody reported. */
function tokenKindsNote(k: TokenKinds): string {
  const parts: string[] = []
  if (k.input !== null) parts.push(`in ${tokenCount(k.input)}`)
  if (k.output !== null) parts.push(`out ${tokenCount(k.output)}`)
  if (k.cacheRead !== null) parts.push(`cache read ${tokenCount(k.cacheRead)}`)
  if (k.write5m !== null && k.write1h !== null) {
    parts.push(`write 5m ${tokenCount(k.write5m)}`, `write 1h ${tokenCount(k.write1h)}`)
  } else if (k.cacheWrite !== null) {
    parts.push(`write ${tokenCount(k.cacheWrite)}`)
  }
  return parts.join(' · ')
}

/**
 * What the elapsed figure is a measure of, which is not the same thing in
 * every state.
 *
 * "Still running" on a PARKED task was the first version of this and it is a
 * flat lie: a parked attempt released its capacity and nothing is executing.
 *
 * `running` IS READ FROM `elapsed()`'s PHASE, NOT FROM `started_at`. The start
 * on the task document survives a park and a requeue, so a retry waiting in
 * READY, LEASED or DISPATCHED has one -- and this note said `running` under
 * it. The note follows `elapsed()`'s answer rather than a second reading of
 * the same field.
 *
 * WHERE THE FIGURE IS ONLY A STATE WORD, THE NOTE CARRIES THE AGE, LABELLED AS
 * ONE. Between attempts, and on a LEASED or DISPATCHED task, `elapsed()` has
 * no span to time and prints the state word alone. This note says why -- an
 * earlier attempt ran, or nothing has started -- and gives `submitted … ago`,
 * which is what the age is. It used to read `wall time, not work` under a
 * parked retry's `waiting 50m`, which called the age a wait.
 *
 * NOTHING HERE SAYS `still counting` ANY MORE. It described `elapsed()` going
 * on to `now` for a finished task with no recorded end, and for one that never
 * started. Neither ticks now: a run with no recorded end has no length and
 * reads `—`, and a task that never started reads `never ran`.
 *
 * EVERY AGE HERE IS TAKEN AT `now`, the drawer's shared 1s clock (AG-2), so
 * the note moves on the same tick as the figure beside it rather than on
 * whatever `Date.now()` said when the tile last happened to render.
 */
function elapsedNote(task: Task, phase: ElapsedPhase, now: number): string {
  // NEVER RAN (AG-3). A finished task with no start never had a worker, so
  // there is no run to time. `elapsed()` says so in the figure itself, so the
  // note does not say it twice: it names how the task ended and when, which is
  // what the figure does not carry. Read from `phase`, the answer `elapsed()`
  // gives for exactly this, never from the figure's text.
  if (phase === 'never-ran') {
    return task.completed_at !== null
      ? `${stateWord(task.state)} ${timeAgo(task.completed_at, now)}`
      : `${stateWord(task.state)} · no finish recorded`
  }
  if (TERMINAL_STATES.has(task.state)) {
    if (task.completed_at !== null) return `finished ${timeAgo(task.completed_at, now)}`
    // NO completion time. Every terminal write sets one with the state, so
    // this is an older document or a writer that forgot, and the figure above
    // is an absence rather than a length. This says which absence.
    return 'no finish recorded'
  }
  if (phase === 'running') return 'running'
  // No span to time: STARTING or RUNNING with no start recorded, or a task
  // with no submission time. The figure is `—`; this says which absence.
  if (phase === 'unknown') return task.created_at ? 'no start recorded' : 'no submission time recorded'
  // Between two attempts: the figure is the state word, and the age includes
  // the run that already happened, so it is given as an age and nothing else.
  if (task.started_at !== null) return `an earlier attempt ran · submitted ${timeAgo(task.created_at, now)}`
  // Holding a slot before the first attempt starts: the figure is the state
  // word, because the age after it read as time held.
  if (CONCURRENCY_STATES.has(task.state)) return `nothing started · submitted ${timeAgo(task.created_at, now)}`
  // "Parked -- wall time, not work. Nothing is executing and no capacity is
  // held" was the first version, and the fact in it is the first three words:
  // the figure is a clock, not a measure of work. Only a PARKED task that has
  // never started gets here, so the clock is all wait.
  if (task.state === 'PARKED') return 'wall time, not work'
  // The label says `Waiting` (G2-13), so the note does not say it again.
  return 'nothing started'
}

function Alerts({ task }: { task: Task }) {
  const live = !TERMINAL_STATES.has(task.state)

  return (
    <>
      {/* THE REASON TOKEN IS THE FACT; THE SENTENCE UNDER IT WAS NOT.
          `REASON_COPY` is a table of platform invariants -- what
          QUOTA_EXHAUSTED means is true of every parked task there has ever
          been -- so it is `#help/park-on-missing-credential` and the rest of
          the help index now, and the banner carries the enum the platform
          recorded plus the one figure that varies: when it is eligible again.
          The enum is the title's tooltip and the pill's words are its text
          (QA G2-25, 2026-10-07): `DEPENDENCY_INCOMPLETE` in capitals was a
          machine token used as a heading, beside a pill that said `waiting
          on a step`. `reasonText` remains the fallback for a reason this screen does not
          recognise, because "we have no copy for that" is a fact about THIS
          SCREEN and cannot live in a help topic keyed on the reason. */}
      {task.park_reason && (
        <div className="bar amber">
          <strong title={task.park_reason}>{parkWord(task.park_reason)}</strong>
          {task.next_eligible_at && (
            <> · eligible {new Date(task.next_eligible_at).toLocaleString()}</>
          )}
          {REASON_COPY[task.park_reason] === undefined && (
            <span className="blocker-copy">{reasonText(task.park_reason)}</span>
          )}
        </div>
      )}

      {/* #362: WHICH POOL REFUSES IT NOW. `waiting_for` is read live on the
          GET from this task's own pools (swarm_api/waiting.py); the bar below
          it is the scheduler's record from its last pass, which can be older
          or absent. "Holds no capacity" is invariant 1: a READY task costs
          nothing while it waits. An unknown pool prints no number, never a 0
          or a full fraction. Amber only when a person must act (AG-14), by
          the same rule as the why line. */}
      {waitingLine(task) !== null && (
        <div className={`bar${whyNeedsAction(task) ? ' amber' : ''}`} data-waiting-for="">
          <span data-waiting-lead="">{waitingLine(task)}</span>
          {task.waiting_for?.holds_capacity === false && (
            <span className="blocker-copy">(holds no capacity)</span>
          )}
        </div>
      )}

      {/* NOT the same thing as park_reason, and this is the case a header that
          only renders park_reason gets wrong. record_blockers writes
          blocked_by while deliberately leaving the task READY -- the platform
          being busy is not a durable condition -- so READY with blockers and
          no park reason is the commonest "why is nothing happening", and it
          would otherwise show as a bare READY chip with no explanation. */}
      {task.blocked_by && task.blocked_by.length > 0 && (
        <div className="bar amber" data-blocked-by="">
          <span className="blocker-copy">last scheduler pass</span>
          {task.blocked_by.map((b, i) => (
            <div className="blocker" key={`${b.reason}-${i}`}>
              {b.pool && <code>{b.pool}</code>} <strong>{b.reason}</strong>
              {typeof b.active === 'number' && typeof b.limit === 'number' && (
                <>
                  {' '}
                  · {b.active} active / {b.limit} limit
                </>
              )}
              {/* ONE table of copy, in types.ts, shared with the agents list
                  and the trouble board -- and now drawn only for a reason this
                  screen has no copy for. The twelve recognised reasons carry
                  their meaning in `#help/blockers-at-an-instant`; an
                  UNRECOGNISED one has nowhere else to say that it is
                  unrecognised, so it still says it here. */}
              {REASON_COPY[b.reason] === undefined && (
                <span className="blocker-copy">{reasonText(b.reason)}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* WHY THE STATE DOES NOT CHANGE was two sentences about the lease and
          the pool; it is `#help/lease-and-pool-are-two-records`. What is on
          the glass is that a cancellation has been asked for. */}
      {task.cancel_requested && live && (
        <div className="bar red">
          Cancellation requested
        </div>
      )}
    </>
  )
}

/**
 * The sentence for a park reason or a blocker reason.
 *
 * `REASON_COPY` is the single table, shared with the agents list and the
 * trouble board. `reasonCopy` falls back to the raw string, which is right in
 * a table cell and wrong here: the enum token is already printed in bold
 * beside this, so the fallback would print it twice. An unrecognised reason
 * therefore gets a sentence about THIS SCREEN not knowing it -- a fact about
 * the UI rather than an invented explanation of the platform's state.
 */
function reasonText(reason: string): string {
  return REASON_COPY[reason] !== undefined
    ? reasonCopy(reason)
    : 'This screen has no copy for that reason — it is printed above exactly as the platform recorded it.'
}

/**
 * THE ONE SENTENCE THIS SCREEN KEEPS, and it keeps it deliberately.
 *
 * `whyAgent` is the answer to the question that brings people here: why has
 * this agent not moved. §8.5 of design-system.md allows a name, a unit, a
 * control's own label, a bare count and a qualifier -- and this is none of
 * them, it is running text. It stays because it is a fact about THIS RUN, it
 * is derived rather than looked up, and there is no encoding for it: a shape
 * cannot say "the pool it wants is at its ceiling and three agents are ahead
 * of it". The heading went, because a one-line panel headed "Why" is a word of
 * chrome per word of content.
 */
function Why({
  task,
  events,
  now,
  classes,
}: {
  task: Task
  events: TaskEvent[] | null
  now: number
  /**
   * The catalogue `Run` already read alongside this task (`loadAgentRun`,
   * api.ts) -- not a second fetch of `/v1/resource-classes`. `classUnits`
   * (#66) turns it into this task's weight, never the bundled
   * `RESOURCE_UNITS` table: #66's own repro (`resource:browser` at
   * `hard_limit 1`, a 2-unit browser task) read "busy platform-wide. (0/1)"
   * here before this was threaded through. null keeps the pre-#66 reading
   * when the catalogue has not answered.
   */
  classes: ResourceClasses | null
}) {
  const units = classUnits(classes, task.resource_class)
  const why = whyAgent(task, units)
  if (!why) return <SilentWorker task={task} events={events} now={now} />
  // #362: the "waiting for" bar in `Alerts` already prints this line, with
  // "(holds no capacity)" beside it; once is enough, as for `last_error`.
  if (why === waitingLine(task)) return null
  // ONCE, NOT THREE TIMES (AG-7). For a FAILED task `whyAgent` IS
  // `last_error`, and the error banner directly below prints that same text
  // in full -- so a failed agent's reason stood here, then in the banner, then
  // again in the attempt card. The banner is the one that keeps it: full,
  // monospace, with who wrote it.
  if (why === task.last_error) return null
  // THE SAME INK RULE AS THE LIST'S ROW (AG-14): `--warn` only when the
  // sentence asks a person to act, plain ink for a routine wait or a cancel.
  return (
    <section className="section">
      <p className={`why-full${whyNeedsAction(task, units) ? ' is-warn' : ''}`}>{why}</p>
    </section>
  )
}

/**
 * AG-14'S FOURTH KIND: A STUCK OR SILENT WORKER, which had no line to colour.
 *
 * The owner's decision names "stuck/silent workers" among the why lines that
 * need a person and so take `--warn`. `whyAgent` writes nothing for a task
 * that holds a slot -- it answers "why is this not running", and a leased or
 * running task is -- so the first pass had no line here and coloured none.
 *
 * The inspector reads the task's events, and `livenessOf` (Liveness.tsx)
 * already derives the answer the badge in the heading draws: a task in a
 * concurrency state with no event for seven minutes is `silent`, "the worker
 * may be gone". That sentence is the why line, in `--warn`, and nothing else
 * is -- not `quiet` (heartbeats are ~150s apart, so three to seven minutes is
 * not yet alarming), and not an event read that failed or a re-read that
 * stopped vouching (`now` stops with it, `rowClock` in `Run`): "could not
 * look" is never drawn as "nothing happened".
 *
 * NOT ON THE AGENTS LIST. A list row is a task document, and a worker's
 * heartbeat is written to its LEASE (control.py `heartbeat`), which no
 * tenant-scoped route serves; the admin leases route is the only reader. The
 * list half is a backend change, #179, rather than a guess from
 * `updated_at`, which a heartbeat does not move.
 */
function SilentWorker({ task, events, now }: { task: Task; events: TaskEvent[] | null; now: number }) {
  const live = livenessOf(task, events, now)
  if (live.kind !== 'silent') return null
  return (
    <section className="section">
      <p className="why-full is-warn" data-why="silent">
        {live.say}
      </p>
    </section>
  )
}

/**
 * The error banner. Full text, monospace, NEVER one-line-truncated -- it is
 * the reason the page was opened.
 *
 * WHO WROTE IT IS THE FACT, AND IT IS ONE WORD. Three flavours, distinguished
 * by prefix because each means something different about who decided the task
 * had failed -- and each used to carry a sentence saying what that writer is.
 * The writer's NAME is the part that varies and the part a reader acts on; an
 * eyebrow carries it in the same treatment every other section label on this
 * screen uses. What a reconciler is, and that the dispatch field is capped at
 * 1000 characters, are platform invariants and live in `#help/read-failed`'s
 * neighbourhood rather than above every error.
 */
function ErrorBanner({ run }: { run: AgentRun }) {
  const text = run.task.last_error
  if (!text) return null
  const origin = errorWriter(run)

  return (
    <section className="section">
      <div className="ctl-empty is-failed" role="status">
        <span className="ctl-eyebrow">error · {origin}</span>
        <pre className="err full">{text}</pre>
      </div>
    </section>
  )
}

/**
 * `cancelled on request; ` is prepended by the scheduler's dispatch rollback
 * and by the reconciler's repair when a cancel had been asked for; what
 * follows it is still the writer's own words.
 */
const CANCELLED_ON_REQUEST = /^cancelled on request;\s*/

/**
 * A dispatch failure, as the scheduler writes it: `<code> (attempt <ref>)`
 * (scheduler/store.py, `public = f"{error_code} (attempt {reference})"`).
 *
 * CASE-INSENSITIVE, AND THAT IS THE FIX. This was `^[A-Z][A-Z0-9_]+`, and the
 * codes are lowercase -- `gke_create_job_failed`, `cloud_run_run_job_failed` -- so it
 * matched none of them and every dispatch failure was labelled the agent's.
 */
const DISPATCH_CODE = /^[a-z][a-z0-9_]* \(attempt /i

/**
 * WHO WROTE `task.last_error`, from what this page can see (AG-6).
 *
 * The banner said `agent, at finish` for anything it did not recognise, and
 * the commonest thing it did not recognise was the scheduler: a cascade cancel
 * of a step whose parent failed writes "an upstream workflow step did not
 * succeed" onto a task that NEVER RAN -- no agent existed to write anything.
 * So the writer is read, in order, from:
 *
 *   1. the text's own prefix, where the writer puts one (`reconciled:`, the
 *      dispatch code);
 *   2. the CANCEL EVENT'S REASON: the scheduler's cancels write a `cancelled`
 *      event whose `detail.reason` is exactly the `last_error` they wrote
 *      (scheduler/store.py `cancel`, `cancel_if_not_started`);
 *   3. the state and the attempts: a task with no started attempt had no
 *      agent, so whatever wrote this was the platform, and a CANCELLED one
 *      was the scheduler's cancel even when its event is off this page.
 *
 * Only a task that ran, with none of those signs, is the agent's own error.
 */
function errorWriter(run: AgentRun): string {
  const { task, attempts, events } = run
  const text = task.last_error ?? ''
  const body = text.replace(CANCELLED_ON_REQUEST, '')
  if (body.startsWith('reconciled:')) return 'reconciler'
  if (DISPATCH_CODE.test(body)) return 'scheduler · dispatch'
  const cancelEvent = (events ?? []).find(
    (e) => eventKind(e) === 'cancelled' && e.detail?.['reason'] === text,
  )
  if (cancelEvent !== undefined) return 'scheduler · cancel'
  if (!anythingRan(task, attempts)) {
    return task.state === 'CANCELLED' ? 'scheduler · cancel' : 'scheduler'
  }
  return 'agent, at finish'
}

// ---------------------------------------------------------------------------
// Attempts -- the unit of this screen
// ---------------------------------------------------------------------------

function Attempts({ run, now }: { run: AgentRun; now: number }) {
  const { task, attempts, attemptsDetail } = run

  if (attempts === null) {
    return (
      <section className="section">
        <span className="ctl-eyebrow">attempts</span>
        <Absent
          kind="failed"
          heading="Attempt history"
          say="The attempt history could not be read. This is a failed read, not a task with no attempts."
          explain="read-failed"
        >
          {/* The DETAIL is the fact -- which read failed and how -- and §8.4
              keeps it as the empty state's one allowed sentence. What may not
              be concluded from a failed read is a standing rule, and moved. A
              null detail passes no child at all rather than padding the slot
              with a sentence about having nothing to say. */}
          {attemptsDetail ?? undefined}
        </Absent>
      </section>
    )
  }

  if (attempts.length === 0) {
    // A REAL ZERO, and which real zero depends on the COUNTER, not on the
    // state. `create_attempt` runs at dispatch and `attempt_count` is
    // incremented inside the admission transaction, so ANY task whose counter
    // is above zero and whose query returned nothing has a hole in the record
    // -- a RUNNING one as much as a finished one. Gating this on `finished &&`
    // sent every live task with that hole into the branch below, which
    // interpolates its state into a sentence asserting the opposite.
    const finished = TERMINAL_STATES.has(task.state)
    return (
      <section className="section">
        <span className="ctl-eyebrow">attempts</span>
        {task.attempt_count > 0 ? (
          // THE TWO NUMBERS ARE THE FACT, and they are now the heading rather
          // than a sentence under one: what the task counts against what came
          // back. A reader needs no help card to see the gap, and the `PARTIAL`
          // mark beside it says which kind of nothing this is.
          <Absent
            kind="partial"
            heading={`counts ${task.attempt_count} · returned 0`}
            say={`This task counts ${task.attempt_count} attempts and the query returned none, so there is a hole in the record.${finished ? '' : ` The task is ${stateWord(task.state)}.`}`}
            foot={finished ? undefined : stateWord(task.state)}
            explain="attempt-documents"
          />
        ) : (
          // "REAL ZERO" is the mark, always rendered, in words -- so the one
          // thing a reader must not have to hover for is the one thing they
          // cannot miss. The heading is the state the measurement was taken in.
          <Absent
            kind="zero"
            heading={`no attempt yet · ${stateWord(task.state)}`}
            say="The attempt query succeeded and returned nothing. Nothing has been admitted for this task yet, so this is a real zero rather than a failed read."
            explain="attempt-documents"
          />
        )}
      </section>
    )
  }

  // Oldest first. A retried run only parses in the order it happened, and the
  // route hands them back newest-first. Sorted on `created_at` (ISO-8601 UTC,
  // so lexicographic) rather than on `generation`, which fences an attempt and
  // is not promised to be dense.
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  const latest = ordered[ordered.length - 1]

  return (
    <section className="section panel">
      <h2>
        Attempts
        <Count n={ordered.length} label="attempts" bare />
        {ordered.length < task.attempt_count && (
          // THE GAP, IN THE HEADING IT QUALIFIES. This was a full partial
          // panel with a two-sentence body; the figures are the fact and the
          // consequence ("every figure below describes only the attempts
          // shown") is what the mark means.
          <Mark
            kind="partial"
            say={`The task records ${task.attempt_count} attempts and ${ordered.length} documents came back. The rest are missing, not absent, so every figure below describes only the attempts shown.`}
          />
        )}
      </h2>

      {/* SPEND OVER THE RUN, before the cards rather than after them: with
          three or more attempts the question "did the retries cost anything"
          is asked of the run, and answering it requires reading three cards
          and doing the arithmetic -- over a series where some attempts have
          no figure at all, which is where that arithmetic goes wrong. The
          chart states the coverage with the total, always. */}
      <TokenSpendChart attempts={ordered} profile={task.runner_profile} />

      {/* WHERE THE WALL CLOCK WENT, attempt by attempt, and the summed agent
          work under it (redesign-v2 §4 viz #3 and #15). The cards below still
          carry each attempt's start, end and `ran` as facts; this is what
          they cannot show -- that a run spent three minutes waiting for a
          container and eighteen seconds working, and how the retries compare.
          Each phase is drawn between two RECORDED instants and an interval
          with no recorded end is drawn OPEN, never closed at "now". */}
      <AttemptDurations task={task} attempts={ordered} events={run.events} />

      {ordered.map((a, i) => (
        <AttemptCard
          key={a.attempt_id}
          a={a}
          ordinal={i + 1}
          isLatest={latest !== undefined && a.attempt_id === latest.attempt_id}
          several={ordered.length > 1}
          run={run}
          now={now}
        />
      ))}

      {/* THE RESTORE POINTER HAS ONE HOME (#102): the Checkpoints tab's
          `restore` fact, which reads the pointer against the listing -- what
          is actually in the bucket -- rather than against the attempt cards,
          which no longer list checkpoints one by one. THE LINK BLOCK THAT
          STOOD HERE IS GONE (agent-details-v3.html A): each card carries its
          own `?` for its own topics. */}
    </section>
  )
}

/**
 * An attempt's account fact (#379). The row's own `account`, joined on from
 * `accounts_by_attempt` by `loadAgentRun`; absent means not read. Whether an
 * attempt with none is `not assigned yet` or `not assigned` is the API's
 * answer (`task_accounts.accounts_for_attempts`); this only prints it.
 */
function AttemptAccount({ a }: { a: AttemptRow }) {
  const account = accountText(a.account)
  return (
    <li className={`ctl-fact${account.known ? '' : ' is-absent'}`} title={account.title}>
      <b>account</b>
      <span className="mono">{account.text}</span>
    </li>
  )
}

function AttemptCard({
  a,
  ordinal,
  isLatest,
  several,
  run,
  now,
}: {
  a: AttemptRow
  ordinal: number
  isLatest: boolean
  /** More than one attempt came back, so a per-attempt `ran` says something the Elapsed tile does not. */
  several: boolean
  run: AgentRun
  now: number
}) {
  const out = attemptOutcome(a)
  const end = attemptEnd(a, run.task, isLatest)

  // `attemptOutcome` reads the attempt document ALONE, and on the document
  // alone a reclaimed or parked attempt is indistinguishable from a running
  // one: each has a start time and no finish time. This card can see what the
  // document cannot -- whether a later attempt exists, what state the task is
  // in and whose lease it holds -- so the chip is corrected here rather than
  // left saying "running" directly above a paragraph that says the attempt is
  // over. The correction
  // belongs in `attemptOutcome`; that lives in types.ts, which another track
  // owns, so it is reported rather than edited.
  const chip: { label: string; tone: ChipTone } = isParked(a)
    ? parkedOutcome(a)
    : end.over && out.label === 'running'
      ? {
          label: end.by === 'superseded' ? 'superseded' : 'ended, no end recorded',
          tone: 'wait',
        }
      : out

  // THE DURATION, WHEN IT CANNOT BE COMPUTED. `attemptRan` measures from the
  // start to NOW when there is no finish time, which is right for a running
  // attempt and false for this one: an attempt that ended without its end
  // being written stopped at some unknown moment, and "1h 40m so far" is a
  // clock still running on a process that is gone. The sentence that said so
  // is the mark's accessible name; what is on the glass is the em dash and
  // `FINISH_NOT_RECORDED`'s own two words, which `measure.ts` already owns.
  const lostEnd = end.over && a.completed_at === null && a.started_at !== null

  return (
    <section className="ctl-card att-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">
          {attemptLabel(ordinal, a.generation)}
          <ToneMark tone={chip.tone}>{chip.label}</ToneMark>
          {/* `latest` IS METADATA, NOT A STATE, and it is the one place on
              these four screens where a hairline box is the right answer --
              Koyeb's rule, quoted in §6.6: their one pill-shaped element is
              metadata in a grey outline, which is how they keep a pill from
              meaning "status". It stays a `.tag`; what changed is that the two
              STATES beside it stopped being one. */}
          {isLatest && <span className="tag">latest</span>}
          {a.oom_near_miss && <ToneMark tone="bad">OOM near miss</ToneMark>}
        </h2>
        {/* THE BACKEND IS A STRING THE API CHOSE, SO IT IS BROUGHT INTO THIS
            CONSOLE'S REGISTER RATHER THAN LEFT SHOUTING. `a.backend` arrives
            as `CLOUD_RUN_JOB` and was printed verbatim, two inches from a
            state chip that lowercases the API's `RUNNING` to `running` -- two
            machine tokens of the same kind, on the same line, in two different
            cases. §13.2 of design-system.md allows `text-transform` for
            exactly this and allows it in exactly this direction: quieter, and
            only on a string we did not author. The words are the runner
            picker's (`backendWord`), so this card and the Attempts tab's say
            `Cloud Run` alike (QA G2-25). */}
        <span className="ctl-card-note att-backend">{backendWord(a.backend)}</span>
      </div>

      <div className="ctl-card-body">
        {/* EIGHT `<dt>/<dd>` PAIRS BECAME ONE STRIP. The values are short and
            the keys are shorter; a 96px label column down the side of eight
            single-line values is chrome outweighing content, three cards
            deep. An absent value keeps its key and its slot. */}
        <ul className="ctl-facts">
          <li className={`ctl-fact${a.started_at === null ? ' is-absent' : ''}`}>
            <b>start</b>
            {a.started_at === null ? <Em /> : timeAgo(a.started_at)}
          </li>
          {/* Null is three different things and the chip above says which:
              still running, never started, or ended without the field being
              written. */}
          <li className={`ctl-fact${a.completed_at === null ? ' is-absent' : ''}`}>
            <b>end</b>
            {a.completed_at === null ? <Em /> : timeAgo(a.completed_at)}
          </li>
          {/* THIS ATTEMPT'S ACCOUNT (#379), swaps included:
              `acct-eng-01 → 02 (swapped: unreadable)`. */}
          <AttemptAccount a={a} />
          {/* `ran` ONLY BESIDE ANOTHER ATTEMPT (#102). With one attempt it
              is the Elapsed tile's figure a second time -- measured between
              the attempt's instants rather than the task's, so the two read
              48s and 49s for one run. With several, it is how the run split. */}
          {several && (
            <li className={`ctl-fact${lostEnd ? ' is-absent' : ''}`}>
              <b>ran</b>
              {lostEnd ? (
                <>
                  <Em />{' '}
                  <Mark
                    kind="absent"
                    say={`This attempt started ${timeAgo(a.started_at ?? '')} and no finish time was ever written, so how long it ran is unknown.`}
                  />
                </>
              ) : (
                ranText(a, now)
              )}
            </li>
          )}
          {/* THE EXECUTION, ATTEMPT AND LEASE IDS ARE ON THE ATTEMPTS TAB
              (agent-details-v3.html A): per-attempt ids are what that tab is
              for, and here they were three mono lines on every card. */}
        </ul>

        {/* ONLY WHEN IT SAYS SOMETHING THE BANNER DOES NOT (AG-7). The last
            attempt's error is usually the task's `last_error`, which the
            banner above already prints in full; an EARLIER attempt's error,
            or one the task's final record replaced, is the retry history and
            stays on its own card. */}
        {/* A PARKED ATTEMPT'S `error` IS ITS PARK REASON (#163), already in
            the chip, so it is not printed as a failure in the red `pre`. */}
        {a.error !== null && a.error !== run.task.last_error && !isParked(a) && (
          <pre className="err full">{a.error}</pre>
        )}

        <AttemptResources a={a} run={run} isLatest={isLatest} />
        <AttemptSpend a={a} profile={run.task.runner_profile} />
        <AttemptCheckpoints a={a} run={run} />
      </div>
    </section>
  )
}

/**
 * How long one attempt ran, through `spanText` -- the formatter the phase
 * chart and the Attempts pane draw the same span with (#102), so one attempt
 * cannot read `48s` on one pane and `49s` on the next. An open attempt counts
 * to `now` and says so.
 */
function ranText(a: AttemptRow, now: number): string {
  if (a.started_at === null) return 'never started'
  const started = instant(a.started_at)
  if (started === null) return 'start time unreadable'
  if (a.completed_at === null) return `${spanText(now - started)} so far`
  const done = instant(a.completed_at)
  if (done === null) return 'finish time unreadable'
  return spanText(done - started)
}

// HAS THIS ATTEMPT ENDED, and on what evidence: `attemptEnd` in duration.ts.
//
// It lived here until the phase chart needed the same answer and grew its own
// copy, and the copy is where the defect was: both read `completed_at`, a
// later attempt and a terminal task, and neither read the task's lease. A
// quota park, a failed dispatch and a reconciler reclaim each leave the task
// non-terminal and the attempt's `completed_at` null, so the card called a
// parked attempt "running" and measured its `ran` against the clock. One
// predicate now, read by the card, the resources panel and the chart, so
// they cannot disagree about the same attempt on the same screen.

/**
 * REQUESTED vs UTILISED, for one attempt.
 *
 * The requested side is `RESOURCE_CLASSES` from the frozen catalogue, served
 * by `/v1/resource-classes`. It is a route rather than a table because a
 * served value cannot drift at all -- `check-contract-parity.sh` now has a
 * TypeScript section, but an asserted copy still has to be edited in two
 * places every time a class is resized, and a route has to be edited in none.
 *
 * THE COMPARISON ONLY MEANS ANYTHING BECAUSE `requests == limits`. There is no
 * bursting on this platform, so the requested figure is also the ceiling: 90%
 * of it is not "well utilised", it is one chatty prompt from an OOM kill.
 *
 * THE LIVE CASE. `peak_rss_bytes` is written by `record_resource_usage` at the
 * end of an attempt, so a RUNNING agent has none and this panel would be
 * entirely em dashes for exactly the agent someone is watching because they
 * are worried about it. Every fifth heartbeat event carries a real reading of
 * the live process, so that is used when there is no final figure -- labelled
 * with its age, never presented as the final number.
 */
function AttemptResources({
  a,
  run,
  isLatest,
}: {
  a: AttemptRow
  run: AgentRun
  /** From the attempt list: whether any later attempt document exists. */
  isLatest: boolean
}) {
  const { task, events, classes, classesDetail, classesRouteMissing } = run
  const cls: ResourceClassSpec | null = classes?.[task.resource_class] ?? null
  const hb = newestHeartbeat(a, events)
  // Which of the three reasons there is no ceiling, as three words. One
  // helper, shared with the metric strip, so the tile and the bars cannot
  // disagree about why the same catalogue is missing.
  const noCeiling = ceilingNote(run)

  // THE ATTEMPT HAS ENDED OR IT HAS NOT, and the same fallback figure means
  // two different things either way. A running attempt has no final peak YET.
  // An attempt that ended without `record_resource_usage` has none at all and
  // none is coming, and calling that figure "live" tells its reader to wait
  // for a write that has already not happened.
  //
  // `completed_at` alone does not separate the two -- it is not written on the
  // paths this distinction exists for. `attemptEnd` (duration.ts) is where
  // the four pieces of evidence this page actually holds are read.
  const end = attemptEnd(a, task, isLatest)
  const ended = end.over

  // WHAT IS KNOWN ABOUT THE END, in the words of what was read. This page
  // cannot see a kill, a reclaim, a park or a crash; it can see a finish
  // time, a later attempt document, the task's state and whose lease the task
  // holds, so it says those and stops.
  //
  // IT IS A SENTENCE STILL, AND IT IS NOT ON THE GLASS. Two `<p className=
  // "muted small">` blocks carried it under every attempt's bars -- around
  // ninety words on a three-attempt run, repeated three times, saying the same
  // thing about the same platform each time. The sentence is now the accessible
  // name of the mark beside the figure it is about, which is where it can never
  // be read as describing a different figure.
  const endedBecause: string =
    end.over === false
      ? ''
      : end.by === 'recorded'
        ? 'Its finish time is recorded, and no peak memory was written with it.'
        : end.by === 'superseded'
          ? 'A later attempt has replaced it, so nothing writes to this document again.'
          : end.by === 'released'
            ? `The task is ${stateWord(task.state)} and no longer holds this attempt’s lease, and this attempt was never marked finished — the shape a quota park, a failed dispatch or a reconciler reclaim leaves behind.`
            : `The task is ${stateWord(task.state)} and this attempt was never marked finished — the shape a kill, or a reconciler reclaim of a stale generation, leaves behind.`
  const liveRss = a.peak_rss_bytes === null ? (hb?.peakRssBytes ?? null) : null
  const rss = a.peak_rss_bytes ?? liveRss
  const rssBy =
    a.peak_rss_bytes !== null
      ? 'at exit'
      : liveRss !== null && hb !== null
        ? `${ended ? 'last' : 'latest'} heartbeat ${timeAgo(hb.at)}`
        : a.started_at === null
          ? 'never ran'
          : ended
            ? 'never written'
            : 'not yet written'

  const diskBy =
    a.peak_disk_bytes !== null
      ? 'at exit'
      : a.started_at === null
        ? 'never ran'
        : ended
          ? 'never written'
          : 'not yet written'

  return (
    <div className="section" style={SUB}>
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">requested vs utilised</span>
        {/* THE CEILING'S ABSENCE, AS A MARK RATHER THAN A PANEL. Two
            `.ctl-empty.is-partial` boxes with a heading, a paragraph and a
            foot stood here -- and the foot said "the bars are hatched with no
            fill rather than drawn empty", which is a caption for a thing the
            reader is looking at. The bars below ARE hatched with no fill;
            saying so is the one kind of sentence design-system.md §8.5
            forbids outright. The three causes stay apart, because they send a
            reader to three different places, and they stay apart in the WORD:
            `no catalogue route` / `catalogue unread` / `no class <name>`. */}
        {noCeiling !== null && (
          <span className="is-end ctl-card-note">
            {noCeiling}{' '}
            <Mark
              kind={classes === null ? 'unread' : 'absent'}
              say={
                classesRouteMissing
                  ? 'The deployment answering this UI predates GET /v1/resource-classes, so the ceiling these measurements were taken under is unknown to this page. The measured side is unaffected and is real.'
                  : classes === null
                    ? `The resource-class catalogue could not be read, so only the ceiling is missing; the measurements are real. ${classesDetail ?? ''}`
                    : `The catalogue has no class called ${task.resource_class}. It was renamed or retired after this task was submitted, so there is nothing to compare against.`
              }
            />
          </span>
        )}
      </div>

      <CeilingRow
        label={
          <>
            <b>memory</b> peak RSS
          </>
        }
        used={rss}
        ceiling={cls === null ? null : cls.memory_gib * GIB}
        fmt={bytesLabel}
        by={rssBy}
      />
      <CeilingRow
        label={
          <>
            <b>workspace</b> peak
          </>
        }
        used={a.peak_disk_bytes}
        ceiling={cls === null ? null : cls.disk_gib * GIB}
        fmt={bytesLabel}
        by={diskBy}
      />
      <CpuRows a={a} cls={cls} end={end} events={events} />

      {/* The standing rules -- requests == limits, the tmpfs workspace, what
          oom_near_miss actually asserts -- live ONCE in the card foot at the
          end of this section. Four lines of them repeated under every attempt
          made a three-attempt run unreadable, and a note nobody reads is not a
          note.

          WHAT VARIES PER ATTEMPT IS A MARK NOW. Two paragraphs stood here --
          "the memory figure is the last heartbeat reading before it stopped",
          "this attempt ended with no peak memory recorded" -- roughly sixty
          words under every attempt card. Both are one strip: the mark says
          which kind of nothing, the `by` column beside the bar says where the
          figure came from and how old it is, and the sentence is the mark's
          accessible name. The AGE still reaches a phone, because it is in the
          strip and not only in the `by` column, which is display:none below
          560px. */}
      {a.peak_rss_bytes === null && (liveRss !== null || (ended && a.started_at !== null)) && (
        <p className="att-rss-note">
          {/* NAMED, since the CPU rows grew a strip of their own just above
              this one: two unlabelled lines of `heartbeat 3m ago` under five
              bars would not say which bar each is about. */}
          <b>memory</b>
          {liveRss !== null && hb !== null ? (
            <>
              <Mark
                kind={ended ? 'partial' : 'pending'}
                say={
                  ended
                    ? `This attempt is over, so the memory figure is the last heartbeat reading before it stopped, ${timeAgo(hb.at)} — not a live one. ${endedBecause} The final high-water mark is written at the end of an attempt and this one has already ended without it, so there is none and none is coming.`
                    : `The memory figure is a live reading from the newest heartbeat event on this page, ${timeAgo(hb.at)}, not the final high-water mark — that is written when the attempt ends. The event page is oldest-first and capped, so on a long attempt the newest reading available here can be far older than the agent.`
                }
              />{' '}
              {ended ? 'last' : 'live'} heartbeat {timeAgo(hb.at)}
            </>
          ) : (
            <>
              <Mark
                kind="absent"
                say={`This attempt ended with no peak memory recorded, and no heartbeat carrying one is on this page. ${endedBecause} What it used is unknown, which is why the figure is an em dash rather than a zero, and no later write will fill it in.`}
              />{' '}
              {end.over && end.by === 'superseded'
                ? 'superseded'
                : end.over && end.by === 'task-ended'
                  ? stateWord(task.state)
                  : 'ended'}
            </>
          )}
        </p>
      )}

      {/* THE BAR ABOVE IS THE FIGURE; THIS IS ITS HISTORY. The bar keeps the
          one number and where it came from. The step line shows how the peak
          was reached, from every heartbeat of this attempt on the page -- and
          its title says "peak reached by", because the reading is a running
          maximum and would lie if it were read as memory in use now. */}
      <PeakMemoryChart attempt={a} events={events} />
    </div>
  )
}

/**
 * CPU, AS PEAK AND MEAN CORES AGAINST THE LIMIT (#184), in the style of the
 * memory and workspace rows above.
 *
 * WHERE THE FIGURES COME FROM NOW. Contract request #15 was ACCEPTED on #184
 * (2026-09-25): the attempt document carries `cpu_seconds`, `peak_cpu_cores`,
 * `mean_cpu_cores` and `cpu_limit_cores`, served on every attempt row. They
 * replaced #188's interim path -- the same figures on HEARTBEAT events, read
 * back by `attempts?include=usage` into a `usage` block, which this no longer
 * reads. The worker rewrites them with each periodic reading while a runner
 * runs and when each runner is reaped, so the document IS the live reading.
 *
 * WHAT EACH ROW CAN SAY, and why it is the attempt's end that decides it: the
 * document records no time for the figures, so what they are is read off
 * what is known about the attempt (`attemptEnd`, the memory row's evidence):
 *
 *   at exit          its finish is recorded, and the runner was reaped and
 *                    wrote its figures before that;
 *   live reading     it is running. NO AGE IS CLAIMED: there is no served
 *                    time to age, and an age off this browser's clock would be
 *                    a guess drawn as a measurement (the #187 review's point);
 *   last written     it ended without a recorded finish -- a kill, a reclaim
 *                    -- so these are the figures it last wrote, and none at
 *                    exit are coming;
 *   heartbeat event  its typed fields are empty and a HEARTBEAT of it on this
 *                    page carries a figure (`interimReading`): peak, mean and
 *                    cpu-seconds from #188's window, or the cpu-seconds only,
 *                    as every heartbeat before #188 carried them -- and, on a
 *                    full page, `page full; newer readings may exist`;
 *   events not read  its typed fields are empty and the event read failed,
 *                    so the page that could hold its reading was not looked
 *                    at: not `never measured`.
 *
 * TWO ROWS, peak and mean, as the owner decided ("as built"). The ceiling is
 * the limit the worker wrote, labelled by the class read here when that class
 * has the same cpu, else `reported limit`: the typed fields do not say
 * whether the kernel's `cpu.max` or the catalogue gave it, and naming either
 * would be a guess. Only when no limit was written does the ceiling fall back
 * to the task's class as read here. A figure over the ceiling takes the
 * track's over-ceiling hatch.
 *
 * ONE ROW WHEN THERE IS NOTHING TO SPLIT. With nothing measured -- not served,
 * never ran, events not read, not yet written, never measured -- two identical hatched rows
 * would say one fact twice. The one row still says WHICH kind of nothing.
 *
 * THE STRIP UNDER THE ROWS IS HOW ANY OF THAT REACHES A PHONE. The `by`
 * column is `display: none` below 560px, so `CpuNote` carries the same words,
 * from the same `cpuReading`, outside the row.
 */
function CpuRows({
  a,
  cls,
  end,
  events,
}: {
  a: AttemptRow
  cls: ResourceClassSpec | null
  /** What is known about this attempt's end: the same judgement the memory row makes. */
  end: AttemptEnd
  /** The drawer's event page, where an attempt from before the typed fields keeps its reading. */
  events: TaskEvent[] | null
}) {
  const from = cpuOf(a, events)
  const figures = from.kind === 'not_served' ? null : from.figures
  const reported = figures?.cpu_limit_cores ?? null
  const ceiling = reported ?? (cls === null ? null : cls.cpu)
  const fmt = (v: number) => `${Number(v.toFixed(2))} vCPU`
  const reading = cpuReading(from, a, end)

  if (figures === null || !reading.measured) {
    return (
      <>
        <CeilingRow label={<b>cpu</b>} used={null} ceiling={ceiling} fmt={fmt} by={reading.by} />
        <CpuNote reading={reading} seconds={null} from={null} />
      </>
    )
  }

  // The typed source (contract request #26) for the attempt's own figures; a
  // heartbeat reading carries none, and older attempts recorded none.
  const source = limitSource(figures.cpu_limit_cores, cls, from.kind === 'attempt' ? (a.cpu_limit_source ?? null) : null)
  const seconds = figures.cpu_seconds === null ? null : `${Number(figures.cpu_seconds.toFixed(1))} cpu-s`
  return (
    <>
      <CeilingRow
        label={
          <>
            <b>cpu</b> peak
          </>
        }
        used={figures.peak_cpu_cores}
        ceiling={ceiling}
        fmt={fmt}
        by={source === null ? reading.by : `${reading.by} · ${source}`}
      />
      <CeilingRow
        label={
          <>
            <b>cpu</b> mean
          </>
        }
        used={figures.mean_cpu_cores}
        ceiling={ceiling}
        fmt={fmt}
        by={seconds === null ? reading.by : `${reading.by} · ${seconds}`}
      />
      <CpuNote reading={reading} seconds={seconds} from={source} />
    </>
  )
}

/** The four typed CPU fields of one attempt (contract request #15). Null is not measured. */
interface CpuFigures {
  cpu_seconds: number | null
  peak_cpu_cores: number | null
  mean_cpu_cores: number | null
  cpu_limit_cores: number | null
}

/** Where an attempt's CPU figures were read from. */
type CpuFrom =
  /** The row carries none of the four keys: an API older than the typed fields. */
  | { kind: 'not_served' }
  /** The attempt's own typed fields -- the only source for any attempt after this change. */
  | { kind: 'attempt'; figures: CpuFigures }
  /**
   * A HEARTBEAT reading, for an attempt whose typed fields are empty.
   * `secondsOnly`: the event carries cpu-seconds and no cores, as every
   * HEARTBEAT before #188 did. `pageFull`: the page holds as many events as
   * this screen asks for, so the route has more, and a newer reading of this
   * attempt may be among them.
   */
  | { kind: 'heartbeat'; figures: CpuFigures; final: boolean; secondsOnly: boolean; pageFull: boolean }
  /**
   * The typed fields are empty and the EVENT READ FAILED, so whether a
   * heartbeat of this attempt carries a figure is unknown (PR #210
   * re-review). Not `never measured`: that says the page was looked at.
   */
  | { kind: 'unread'; figures: CpuFigures }

function measuredAny(f: CpuFigures): boolean {
  return f.cpu_seconds !== null || f.peak_cpu_cores !== null || f.mean_cpu_cores !== null
}

/**
 * THE ATTEMPT'S FIGURES, from its row -- or, for an attempt from before the
 * typed fields, from the interim reading its heartbeat events carry.
 */
function cpuOf(a: AttemptRow, events: TaskEvent[] | null): CpuFrom {
  if (
    a.cpu_seconds === undefined &&
    a.peak_cpu_cores === undefined &&
    a.mean_cpu_cores === undefined &&
    a.cpu_limit_cores === undefined
  ) {
    return { kind: 'not_served' }
  }
  const figures: CpuFigures = {
    cpu_seconds: a.cpu_seconds ?? null,
    peak_cpu_cores: a.peak_cpu_cores ?? null,
    mean_cpu_cores: a.mean_cpu_cores ?? null,
    cpu_limit_cores: a.cpu_limit_cores ?? null,
  }
  if (measuredAny(figures)) return { kind: 'attempt', figures }
  // The legacy reader needs the page; a failed event read is not an empty one.
  if (events === null) return { kind: 'unread', figures }
  return interimReading(a, events) ?? { kind: 'attempt', figures }
}

/**
 * THE LEGACY READER, and the only thing left of #188's interim path.
 *
 * TWO KINDS OF ATTEMPT HAVE EMPTY TYPED FIELDS AND WERE MEASURED ANYWAY:
 *
 *   - From #188's window (its deploy, 2026-09-25 about 23:08 UTC on dev, to
 *     this change's): the worker put peak, mean, cpu-seconds and the limit on
 *     HEARTBEAT events -- the newest with `final: true` when a runner was
 *     reaped -- and wrote none on the attempt. A read-only look at dev's
 *     Firestore at 23:16 UTC found one such attempt.
 *   - From BEFORE #188: every HEARTBEAT carried `cpu_seconds` and
 *     `cpu_source`, and no cores (5262b74^ lifecycle.py `_heartbeat`). #188's
 *     own reader drew those cpu-seconds on main. This reader first asked for
 *     `peak_cpu_cores` only, and those attempts read `never measured` while
 *     the page held their figure (the PR #210 review).
 *
 * "Never measured" would be false of both. So, for an attempt whose typed
 * fields carry nothing, the newest HEARTBEAT of it ON THIS PAGE that carries a
 * figure. When that event has no cores, the figures are the cpu-seconds
 * alone (`secondsOnly`), and the peak and mean rows draw em dashes. No
 * server read is added: the drawer already holds the page. The page is the
 * task's first `EVENT_PAGE_LIMIT` events, so on a long attempt the newest
 * reading here can be an early one, and the words say it is the newest ON
 * THIS PAGE. When the page is FULL and the reading is not the one taken at
 * exit, the strip says so -- `page full; newer readings may exist` -- because
 * then there are more events than this page holds (PR #210 review). It reads
 * nothing on any attempt the worker wrote typed fields for, which is every
 * measured attempt after this change.
 */
function interimReading(a: AttemptRow, events: TaskEvent[]): CpuFrom | null {
  const n = (d: Record<string, unknown>, key: string): number | null => {
    const v = d[key]
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }
  let best: { figures: CpuFigures; detail: Record<string, unknown> } | null = null
  let bestAt = -Infinity
  for (const e of events) {
    if (e.type !== 'heartbeat' || e.attempt_id !== a.attempt_id || e.detail === null) continue
    const t = Date.parse(e.at)
    if (!Number.isFinite(t) || t <= bestAt) continue
    const d = e.detail
    const figures: CpuFigures = {
      cpu_seconds: n(d, 'cpu_seconds'),
      peak_cpu_cores: n(d, 'peak_cpu_cores'),
      mean_cpu_cores: n(d, 'mean_cpu_cores'),
      cpu_limit_cores: n(d, 'cpu_limit_cores'),
    }
    // A figure, not a key: a heartbeat that carried the key empty is no reading.
    if (!measuredAny(figures)) continue
    bestAt = t
    best = { figures, detail: d }
  }
  const found = best
  if (found === null) return null
  const secondsOnly = found.figures.peak_cpu_cores === null && found.figures.mean_cpu_cores === null
  return {
    kind: 'heartbeat',
    figures: found.figures,
    final: found.detail['final'] === true,
    secondsOnly,
    pageFull: events.length >= EVENT_PAGE_LIMIT,
  }
}

/**
 * WHERE THE CPU CEILING CAME FROM, in words for the `by` column and the strip.
 *
 *   no limit written     the task's class read here, by name;
 *   source `cgroup`      `cgroup limit`: the worker read the kernel's
 *                        `cpu.max` (contract request #26, accepted on #184,
 *                        2026-09-26), whatever the class says;
 *   source `resource_class`
 *                        the class's name (`standard limit`) when the class
 *                        read here has that cpu, else `class limit`: the worker
 *                        took it from the catalogue of the class it was sized
 *                        with, which may not be the one read here;
 *   no source recorded   an attempt from before #26: the class's name when
 *                        the class has the same cpu (with `requests ==
 *                        limits` the two agree), else `reported limit` --
 *                        the legacy words, kept for older attempts.
 */
function limitSource(cores: number | null, cls: ResourceClassSpec | null, recorded: string | null = null): string | null {
  if (cores === null) return cls !== null ? `${cls.name} limit` : null
  if (recorded === 'cgroup') return 'cgroup limit'
  if (recorded === 'resource_class') return cls !== null && cls.cpu === cores ? `${cls.name} limit` : 'class limit'
  if (cls !== null && cls.cpu === cores) return `${cls.name} limit`
  return 'reported limit'
}

/** One attempt's CPU reading, in words, for the rows' `by` column and for the strip under them. */
interface CpuReading {
  /** False when nothing was measured, or nothing was served. */
  measured: boolean
  /** The `by` column's words, as the memory row writes them: `at exit`, `live reading`. */
  by: string
  /** The strip's words: the same, with what the narrow `by` column has no room for. */
  strip: string
  /** Null for figures at exit: `at exit` needs no mark, and the memory strip draws none for it either. */
  mark: MarkKind | null
  say: string
}

/**
 * WHAT AN ATTEMPT'S CPU FIGURES ARE, in words. The one place those words are
 * decided; the `by` column and the strip both read them from here, so they
 * cannot disagree.
 */
function cpuReading(from: CpuFrom, a: AttemptRow, end: AttemptEnd): CpuReading {
  if (from.kind === 'not_served') {
    return {
      measured: false,
      by: 'not served',
      strip: 'not served',
      mark: 'absent',
      say: 'The API answering this UI sent no CPU fields with this attempt. It is older than the typed fields (contract request #15), so this says nothing about how much CPU the attempt used.',
    }
  }
  if (a.started_at === null) {
    return {
      measured: false,
      by: 'never ran',
      strip: 'never ran',
      mark: 'zero',
      say: 'This attempt never started, so it used no CPU. That is a fact about the attempt, not a missing measurement.',
    }
  }
  if (from.kind === 'unread') {
    // THE PAGE WAS NOT READ, so nothing can be said about what it holds. On
    // every attempt from before the typed fields this was `never measured`,
    // a claim about a page no one had looked at (PR #210 re-review).
    return {
      measured: false,
      by: 'events not read',
      strip: 'events not read',
      mark: 'unread',
      say: 'No CPU figures were written on this attempt, and the task’s events could not be read, so whether one of its heartbeat events carries a reading is unknown. This is neither a zero nor a missing measurement.',
    }
  }
  // A FULL PAGE, AND A READING THAT IS NOT THE ONE AT EXIT: the route has
  // more events than this page, and a newer reading of this attempt may be
  // among them (PR #210 review). A reading taken when the runner was reaped
  // is the attempt's last whatever else is off the page.
  const pageFull = from.kind === 'heartbeat' && from.pageFull && !from.final
  const pageFullSay = pageFull
    ? ` This page is full — the task has more events than it holds — so newer readings of this attempt may exist that this one does not include.`
    : ' The page is the task’s first events, so on a long attempt this reading can be an early one, not the total at exit.'
  if (from.kind === 'heartbeat' && from.secondsOnly) {
    // Before #188 a heartbeat carried the cumulative cpu-seconds and nothing
    // else about CPU. The cores it never carried are em dashes, not zeros.
    return {
      measured: true,
      by: 'heartbeat event',
      strip: `heartbeat event · cpu-seconds only${pageFull ? ' · page full; newer readings may exist' : ''}`,
      mark: 'partial',
      say: `No CPU figures were written on this attempt. Its newest heartbeat event on this page with a figure carries its cumulative cpu-seconds and no peak or mean cores (every heartbeat before #188 carried only that), so the peak and mean are unknown: em dashes, not zeros.${pageFullSay}`,
    }
  }
  if (from.kind === 'heartbeat') {
    return {
      measured: true,
      by: 'heartbeat event',
      strip: from.final
        ? 'heartbeat event · at exit'
        : `heartbeat event${pageFull ? ' · page full; newer readings may exist' : ''}`,
      mark: 'partial',
      say: `This attempt ran before the attempt carried its CPU figures (contract request #15), so they are read from its newest heartbeat event on this page${from.final ? ', taken when its runner was reaped' : ', a periodic one: not the figures at exit'}. Nothing was written on the attempt itself.${pageFull ? pageFullSay : ''}`,
    }
  }
  if (!measuredAny(from.figures)) {
    return end.over
      ? {
          measured: false,
          by: 'never measured',
          strip: 'never measured',
          mark: 'absent',
          say: 'No CPU figures were written on this attempt, and none of its heartbeat events on this page carries one. The page is the task’s first events, so a later heartbeat is not read here. What the attempt used is unknown, which is why the figure is an em dash and not a zero.',
        }
      : {
          measured: false,
          by: 'not yet written',
          strip: 'not yet written',
          mark: 'pending',
          say: 'The worker writes the attempt’s CPU figures with its first periodic reading after the runner starts, and none has landed yet. This is not a zero.',
        }
  }
  // THE READING'S AGE (contract request #26, accepted on #184, 2026-09-26),
  // as the API measured it against its own clock at the read: never off this
  // browser's clock, which would be a guess drawn as a measurement. An attempt
  // from before #26 records no time, and says so.
  const age = typeof a.cpu_reading_age_seconds === 'number' && Number.isFinite(a.cpu_reading_age_seconds)
    ? `${ageSpan(a.cpu_reading_age_seconds * 1000)} ago`
    : null
  if (!end.over) {
    return age !== null
      ? {
          measured: true,
          by: 'live reading',
          strip: `live reading · ${age}`,
          mark: 'pending',
          say: `A live reading: the running worker rewrites these figures on the attempt with each periodic reading, and wrote this one ${age} by the API's clock when it was read. They are not the figures at exit.`,
        }
      : {
          measured: true,
          by: 'live reading',
          strip: 'live reading · age not recorded',
          mark: 'pending',
          say: 'A live reading: the running worker rewrites these figures on the attempt with each periodic reading. This attempt records no time for them, so no age is shown. They are not the figures at exit.',
        }
  }
  if (end.by === 'recorded') return { measured: true, by: 'at exit', strip: 'at exit', mark: null, say: '' }
  return {
    measured: true,
    by: 'last written',
    strip: age !== null ? `last written · ${age}` : 'last written',
    mark: 'partial',
    say: `This attempt ended without a recorded finish, so these are the figures its worker last wrote while it ran${age !== null ? `, ${age} by the API's clock` : ''}. They are not the figures at exit, and none are coming.`,
  }
}

/**
 * THE CPU READING, OUTSIDE THE ROWS, SO IT REACHES A PHONE.
 *
 * WHAT SHOWS AT EACH WIDTH, and why it is not the same everywhere:
 *
 *   - The MARK AND ITS WORDS (`live reading`, `never ran`, `last written`)
 *     show at every width, as the memory strip's do. That is the precedent
 *     this follows, and the mark's sentence is the accessible name.
 *   - The cpu-seconds and the ceiling's source, `.att-cpu-by`, show only below
 *     560px. Above that they are already in the `by` column beside each bar,
 *     and a second copy would caption the thing the reader is looking at.
 *   - Figures AT EXIT have no mark, so the whole strip is `.is-final` and
 *     shows only below 560px. On a phone it is the only place `at exit`
 *     appears.
 *
 * The rules are in styles.css beside `.att-rss-note`. details.cpu.test.tsx
 * asks the shipped sheet's cascade what a 390px viewport shows, because jsdom
 * applies no stylesheet and would pass either way.
 */
function CpuNote({ reading, seconds, from }: { reading: CpuReading; seconds: string | null; from: string | null }) {
  const extra = [seconds, from].filter((x): x is string => x !== null)
  return (
    <p className={`att-rss-note att-cpu-note${reading.mark === null ? ' is-final' : ''}`}>
      <b>cpu</b>
      {reading.mark !== null && <Mark kind={reading.mark} say={reading.say} />}
      <span>{reading.strip}</span>
      {extra.length > 0 && <span className="att-cpu-by">· {extra.join(' · ')}</span>}
    </p>
  )
}

/**
 * What this attempt SPENT.
 *
 * These five fields are typed on the attempt document precisely so a question
 * like "spend per tenant last week" can be a query rather than a scan. They
 * are null on every attempt that ran before the worker capture shipped, and
 * NULL IS NOT ZERO -- a mock task costs nothing on purpose, and an attempt
 * whose result could not be parsed cost an unknown amount. Rendering both as
 * "$0.00" lies about one of them.
 *
 * The columns are shown WITH their em dashes rather than omitted. Omitting
 * them, which the previous attempts table did, also hides the numbers on the
 * new attempts that do carry them.
 */
function AttemptSpend({ a, profile }: { a: AttemptRow; profile: string }) {
  const anything =
    a.input_tokens !== null ||
    a.output_tokens !== null ||
    a.cache_read_input_tokens !== null ||
    a.cache_creation_input_tokens !== null ||
    a.cost_usd !== null
  // Only the CLI agents go through `cliagent.run_cli_agent`, which is what
  // parses a usage block out of the runner's own output. The others report
  // nothing by construction, which is a different sentence from "this one
  // should have reported and did not". `reportsSpend` is the one copy of that
  // rule; the run tiles read it too.
  const reports = reportsSpend(profile)

  if (!anything) {
    // THREE DIFFERENT REASONS, and they still need three different answers --
    // but the answer is a MARK plus two or three words, not a paragraph. The
    // one that was collapsed first is the middle one: a running attempt has
    // nothing yet BECAUSE IT HAS NOT FINISHED, and telling its owner the
    // numbers are missing because of an old image sends them to rebuild an
    // image over an attempt that is working correctly. `pending` is the mark
    // for that and it is a different silhouette from `not measured`, which is
    // the distinction design-system.md §8.7.1 says must never collapse: still
    // reading is not nothing reported.
    const running = a.completed_at === null && a.started_at !== null
    const kind: MarkKind = !reports ? 'absent' : running ? 'pending' : a.started_at === null ? 'zero' : 'absent'
    const word = !reports
      ? `${profile} reports none`
      : running
        ? 'written at exit'
        : a.started_at === null
          ? 'never started'
          : 'none recorded'
    const say = !reports
      ? `The ${profile} runner does not report tokens or cost at all. That is an absence of measurement, not a run that cost nothing.`
      : running
        ? "This attempt has not finished. Spend is parsed out of the runner's result and written when the attempt ends, so there is nothing yet — nothing is wrong and nothing needs doing."
        : a.started_at === null
          ? 'This attempt never started, so it consumed no tokens. That is a fact about the attempt rather than a missing measurement.'
          : 'This attempt finished and recorded no usage. Attempts that ran before the worker capture shipped in an agent-runtime-base image carry null for every field — an absent measurement, not a free run.'
    return (
      <div className="section" style={SUB}>
        <p className="att-spend-none">
          <span className="ctl-eyebrow">tokens and cost</span>
          <Mark kind={kind} say={say} /> {word}
        </p>
      </div>
    )
  }

  return (
    <div className="section" style={SUB}>
      <span className="ctl-eyebrow">tokens and cost</span>
      {/* The columns are shown WITH their em dashes rather than omitted:
          omitting them, which the previous attempts table did, also hides the
          numbers on the new attempts that do carry them. */}
      <ul className="ctl-facts">
        <li className={`ctl-fact${a.input_tokens === null ? ' is-absent' : ''}`}>
          <b>in</b>
          {tokens(a.input_tokens)}
        </li>
        <li className={`ctl-fact${a.output_tokens === null ? ' is-absent' : ''}`}>
          <b>out</b>
          {tokens(a.output_tokens)}
        </li>
        <li className={`ctl-fact${a.cache_read_input_tokens === null ? ' is-absent' : ''}`}>
          <b>cache r</b>
          {tokens(a.cache_read_input_tokens)}
        </li>
        <li className={`ctl-fact${a.cache_creation_input_tokens === null ? ' is-absent' : ''}`}>
          <b>cache w</b>
          {tokens(a.cache_creation_input_tokens)}
        </li>
        <li className={`ctl-fact${a.cost_usd === null ? ' is-absent' : ''}`}>
          <b>cost</b>
          {usd(a.cost_usd)}
        </li>
      </ul>
    </div>
  )
}

/**
 * What this attempt wrote, in one line -- and the cadence strip.
 *
 * ONE HOME FOR THE LIST (#102). This was a table per attempt -- id, size and
 * location, with `copy gsutil` on every row -- directly above the Checkpoints
 * section's table of the same checkpoints, read from the bucket. The bucket's
 * table is the one kept: it is what a restore reads, it lists what was written
 * and since reclaimed, and it opens each checkpoint's files. What stays on the
 * card is what only the attempt knows: how many it recorded, what it resumed
 * from, and WHEN it checkpointed, which the strip draws and no table can.
 *
 * TWO RECORDS, NEITHER COMPLETE. `attempt.checkpoints` is ids and nothing
 * else; the `checkpoint_completed` event carries the uri. A checkpoint with no
 * location is therefore one whose event is off this page, or one whose event
 * read FAILED -- and the line keeps the mark that says which, because those
 * were once one sentence that reported a failed read as benign paging.
 */
function AttemptCheckpoints({ a, run }: { a: AttemptRow; run: AgentRun }) {
  const rows = checkpointsFor(a, run.events)
  const restored = restoredFrom(a, run.events)
  const eventsRead = run.events !== null
  const missingLocation = rows.filter((r) => r.uri === null).length

  return (
    <div className="section" style={{ ...SUB, marginBottom: 0 }}>
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">checkpoints</span>
        {/* RESUMED FROM, AS FACTS. "This attempt did not start from an empty
            workspace" was the sentence; the checkpoint id IS that fact, and
            the file count and byte total beside it are what a reader checks. */}
        {restored !== null && (
          <span className="is-end ctl-card-note">
            resumed {restored.checkpoint_id}
            {restored.from_attempt !== null && ` · from ${restored.from_attempt}`}
            {restored.files !== null && ` · ${restored.files} files`}
            {restored.bytes !== null && ` · ${bytesLabel(restored.bytes)}`}
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        // A REAL ZERO OR AN UNKNOWN, AND THEY ARE DIFFERENT MARKS. The
        // document lists none; whether that is the whole story depends on
        // whether the events were read, because a checkpoint can be recorded
        // in an event alone. Why periodic checkpointing legitimately writes
        // none on a short attempt is `#help/checkpoints`.
        <p className="att-ckpt-none att-ckpt-line">
          <Mark
            kind={eventsRead ? 'zero' : 'partial'}
            say={
              !eventsRead
                ? "This attempt's document lists no checkpoint, and the event read failed — so a checkpoint recorded only in an event would not be visible here either. Whether this attempt wrote one is unknown."
                : a.started_at === null
                  ? "This attempt's document lists no checkpoint. It never started, so there was nothing to checkpoint."
                  : "This attempt's document lists no checkpoint. Checkpointing is periodic, so an attempt shorter than one interval legitimately writes none."
            }
          />{' '}
          {a.started_at === null ? 'never started' : 'none written'}
        </p>
      ) : (
        <>
          <p className="att-ckpt-line">
            {rows.length} written
            {/* THE COVERAGE IS A FRACTION, NOT A PARAGRAPH, and it says which
                absence it is: an event off this page, or an event read that
                failed. */}
            {(!eventsRead || missingLocation > 0) && (
              <>
                {' · '}
                {eventsRead ? rows.length - missingLocation : 0} of {rows.length} located{' '}
                <Mark
                  kind={eventsRead ? 'partial' : 'unread'}
                  say={
                    eventsRead
                      ? "A checkpoint's location is recorded on its event, and this checkpoint's event is not on this page, so its location is unknown here. This screen reads one page of events, oldest-first, and does not follow the page token the events route returns. The Checkpoints tab lists what the bucket holds."
                      : 'The event read failed, so no checkpoint of this attempt has a location attached. It is unknown rather than missing, and nothing here says whether the checkpoint itself is fine. The Checkpoints tab lists what the bucket holds.'
                  }
                />
              </>
            )}
          </p>
          <CheckpointStrip attempt={a} events={run.events} />
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function Output({
  run,
  readAt,
  lead,
}: {
  run: AgentRun
  readAt: number | null
  /**
   * Set when this card LEADS a finished agent (agent-details-v3.html A3): it
   * is then headed Outcome, links to Artifacts, and carries the checkpoint
   * fact and the last log line the Now card carried while it ran.
   */
  lead?: {
    now: number
    listing: CheckpointListing | undefined
    links: DetailLinks | undefined
    lastLine: ReactNode
    /** The Checkpoints tab's read, when the split has one (D21). */
    checkpoints?: CheckpointCount | null
  }
}) {
  const { task, attempts } = run
  const summary = (task.result_summary ?? null) as ResultSummary | null
  // `result_summary` is an untyped Firestore dict: `ResultSummary` describes
  // what the worker writes, not what the field is guaranteed to hold. A
  // document written by an older worker -- or by a test fixture -- can carry
  // `artifacts: 1`, and `(x ?? []) as ArtifactRef[]` would hand that straight
  // to `.find` and blank the page with a runtime error. Shape is CHECKED.
  //
  // THE LIST ITSELF IS NOT DRAWN HERE ANY MORE (#184, owner decision of
  // 2026-09-25): Details' own artifact table -- a viewer and a `copy gsutil`
  // per file -- duplicated Artifacts › Outputs, which lists every file with a
  // viewer by kind and a download through the API. The manifest is still read
  // here for the one thing Details says about a file: which one is the patch
  // the git outcome names.
  const artifactsRaw = summary?.artifacts
  const artifacts: ArtifactRef[] = Array.isArray(artifactsRaw) ? (artifactsRaw as ArtifactRef[]) : []
  // NO LOG ROWS (owner decision, 2026-09-26, on #184). Output listed
  // `result_summary.logs` -- the stdout and stderr object locations, each
  // with `copy gsutil`. "The runner log's object locations move to
  // Artifacts › Logs, beside the logs they point to. Details holds no log
  // rows." Each log's location and its copy now sit on its own row there.
  const terminal = TERMINAL_STATES.has(task.state)

  const retried = attempts !== null ? attempts.length > 1 : task.attempt_count > 1
  // ONE HEADING, NOT TWO STACKED (#102). `Output` sat directly on top of the
  // git outcome's own `Code` heading with nothing between them, so the panel
  // opened on two titles for one subject. When the summary carries a git
  // outcome the panel IS the code, and says so once; otherwise it is Output.
  const git = summary?.git
  const refusals = publishRefusals(task, attempts)
  const hasCode = (terminal || refusals.length > 0) && typeof git === 'object' && git !== null && !Array.isArray(git)

  return (
    <section
      className={`dt-card run-output${lead !== undefined ? ' dt-now is-ok' : ' dt-output'}`}
      data-lead={lead !== undefined ? 'outcome' : undefined}
    >
      <div className="dt-card-head">
        <b>{lead !== undefined ? 'Outcome' : hasCode ? 'Code' : 'Output'}</b>
        {/* THE SCOPE LINE IS NOT OPTIONAL, AND IT IS NOW A QUALIFIER.
            Everything in this panel comes from result_summary, which finish()
            writes ONCE at terminal state. On a retried task it is the last
            attempt's output and nothing else, and a panel that does not say so
            gets read as the run's. Three words plus the mark carry that; the
            two sentences explaining that the earlier attempts' output was
            never summarised anywhere are `#help/attempt-documents`.

            THE FALLBACK MATTERS MORE THAN THE PRIMARY. With the attempt read
            failed this panel cannot count attempts -- which is precisely when
            the caveat is most needed -- and it used to vanish, leaving
            artifacts, git outcome and logs rendered unqualified as the run's
            output. `task.attempt_count` is the task's own counter and answers
            the question when the query does not. */}
        {retried && (
          <span className="is-end ctl-card-note">
            <Mark
              kind="partial"
              say={`Everything in this panel describes the LAST attempt only. The result summary is written once, at terminal state; the earlier attempts' output was never summarised anywhere.${attempts === null ? ` The attempt read failed, so the ${task.attempt_count} attempts are the task's own count rather than a document each.` : ''}`}
            />{' '}
            last attempt of {attempts !== null ? attempts.length : task.attempt_count}
          </span>
        )}
        <span className="dt-card-r">
          <DtMore href={tabHref(task, 'artifacts')} onClick={lead?.links?.artifacts}>
            Artifacts
          </DtMore>
          {/* What the outcome describes, not when an attempt document is
              written (G2-10). */}
          <HelpCard topic="outcome-explained" />
        </span>
      </div>

      {!terminal && refusals.length > 0 ? (
        // REFUSED AND RETRYING (PUBLISH_REFUSED, request 29): each refusal
        // failed its attempt retryably, so the task is not terminal -- and
        // the refusals are the one thing about its code a reader needs now.
        <div className="git-outcome">
          <AgCodeLead git={typeof git === 'object' && git !== null && !Array.isArray(git) ? git : {}} task={task} refusals={refusals} />
          {typeof git === 'object' && git !== null && !Array.isArray(git) && <AgBasePin git={git} />}
        </div>
      ) : !terminal ? (
        <Absent
          kind="zero"
          heading={`nothing written yet · ${stateWord(task.state)}`}
          say="A result summary is written only when an attempt finishes. This agent has not finished, so there is nothing here — which is not the same as producing nothing."
          explain="attempt-documents"
        />
      ) : summary === null && !anythingRan(task, attempts) ? (
        // NOTHING RAN, SO NOTHING WAS DUE (AG-8). `finish()` writes the
        // summary at the end of an attempt, and this task never had one that
        // started -- a cascade cancel, a cancel before admission, a dispatch
        // that never came up. No summary is the expected result here, so it
        // is a real zero; drawn as `partial` it said a record was missing and
        // sent its reader to the timeline to find it.
        <Absent
          kind="zero"
          heading={`nothing ran · ${stateWord(task.state)}`}
          say="No attempt of this task ever started, so no result summary was ever going to be written. This is a real zero, not a missing summary."
          explain="attempt-documents"
        />
      ) : summary === null ? (
        <Absent
          kind="partial"
          heading={`finished with no summary · ${stateWord(task.state)}`}
          say="This agent reached a terminal state without a result summary. A parked attempt puts its summary in the event detail and in blocked_by instead, so the timeline below is where to look."
          explain="attempt-documents"
        />
      ) : (
        <>
          <GitOutcome git={summary.git} artifacts={artifacts} task={task} readAt={readAt} attempts={attempts} />
          <SummaryUsage task={task} attempts={attempts} />
        </>
      )}
      {lead !== undefined && (
        <p className="dt-nfs">
          <DtCheckpoints run={run} now={lead.now} listing={lead.listing} checkpoints={lead.checkpoints} />
        </p>
      )}
      {lead?.lastLine}
    </section>
  )
}

/**
 * The untyped `result_summary.runner.usage`, shown when the typed per-attempt
 * fields have nothing -- or when nobody could read them.
 *
 * It is the OLD home for these numbers -- an untyped dict no index can reach.
 * The attempt fields replaced it, so preferring them and falling back here
 * means an attempt that predates the typed capture still shows its spend
 * instead of five em dashes.
 */
function SummaryUsage({ task, attempts }: { task: Task; attempts: AttemptRow[] | null }) {
  const usage = usageOf(task)
  if (usage === null) return null
  // A FAILED ATTEMPT READ IS NOT A NEGATIVE ANSWER. `attempts.some(...)` on a
  // null read is `false`, which had this panel leading with "No attempt
  // document carries a typed spend figure" -- a positive claim about documents
  // nobody read, when those documents may well carry `cost_usd`. The untyped
  // numbers below are real either way, so they stay; only the sentence that
  // frames them changes.
  const unread = attempts === null
  const typed = attempts !== null && attempts.some((a) => a.cost_usd !== null || a.input_tokens !== null)
  if (typed) return null

  const n = (k: string) => (typeof usage[k] === 'number' ? (usage[k] as number) : null)
  const models = usage['models']

  return (
    <div className="section">
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">spend · result summary</span>
        <span className="is-end ctl-card-note">
          <Mark
            kind={unread ? 'unread' : 'partial'}
            say={
              unread
                ? 'The attempt read failed, so whether any attempt document carries a typed spend figure is unknown. What follows is the last attempt’s own result summary — an untyped dict no query can reach — and it describes that attempt only.'
                : 'No attempt document carries a typed spend figure, but the last attempt’s result summary does. These come from an untyped dict that no query can reach, and they describe the last attempt only.'
            }
          />{' '}
          last attempt only
        </span>
      </div>
      <ul className="ctl-facts">
        <li className={`ctl-fact${n('input_tokens') === null ? ' is-absent' : ''}`}>
          <b>in</b>
          {tokens(n('input_tokens'))}
        </li>
        <li className={`ctl-fact${n('output_tokens') === null ? ' is-absent' : ''}`}>
          <b>out</b>
          {tokens(n('output_tokens'))}
        </li>
        <li className={`ctl-fact${n('cache_read_input_tokens') === null ? ' is-absent' : ''}`}>
          <b>cache r</b>
          {tokens(n('cache_read_input_tokens'))}
        </li>
        <li className={`ctl-fact${n('total_cost_usd') === null ? ' is-absent' : ''}`}>
          <b>cost</b>
          {usd(n('total_cost_usd'))}
        </li>
        {Array.isArray(models) && (
          <li className="ctl-fact">
            <b>models</b>
            {(models as string[]).join(', ')}
          </li>
        )}
      </ul>
    </div>
  )
}

/**
 * What happened to the code, if the task had any.
 *
 * WHY THIS PANEL LEADS WITH A REASON RATHER THAN A LINK. A pull request is
 * absent for at least six different causes, and they need completely different
 * responses from the person reading this:
 *
 *   the tenant's token has no push permission   -> grant write scope
 *   the attempt parked                          -> wait; it is not finished
 *   the agent changed nothing                   -> the run did nothing useful
 *   the host is not a forge we can publish to   -> apply the patch by hand
 *   the push was rejected                       -> something else moved the branch
 *   the pull request call failed                -> the branch IS pushed; retry the PR
 *
 * The last one is the one most worth separating: `published: true` with no
 * `pull_request` means the work reached the forge and only the final API call
 * did not, so the response is to open the PR from a branch that already
 * exists. Collapsing it into "no pull request" sends someone to re-run an
 * agent whose output is already pushed.
 *
 * THE CLASSIFIER KEYS ON STRUCTURED FIELDS -- `published`, `can_push`,
 * `pull_request`, `branch` -- and only falls back to matching the free-text
 * `publish_reason` for the two causes that have no structured signal. An
 * unrecognised reason is NOT forced into a bucket: it is printed verbatim and
 * labelled as one this screen cannot advise on, the same discipline
 * `fetch.ts::classify` uses for an unrecognised 403.
 *
 * DIRTY FILES ARE SHOWN EVEN WHEN THERE ARE NO COMMITS, because that is the
 * common case: most agents edit files and never run `git commit`. A panel that
 * only counted commits would report "no changes" for the majority of real runs.
 */

function GitOutcome({
  git,
  artifacts,
  task,
  readAt,
  attempts = null,
}: {
  git: GitSummary | undefined
  artifacts: ArtifactRef[]
  task: Task
  readAt: number | null
  /** The attempt documents, for the refusals each one recorded; null when unread. */
  attempts?: AttemptRow[] | null
}) {
  // Same untyped-dict caution as the artifact list: `GitSummary` describes what
  // `_harvest_git` writes, and the field is whatever is in Firestore.
  if (typeof git !== 'object' || git === null || Array.isArray(git)) return null

  const commits = Array.isArray(git.commits) ? git.commits : []
  const dirty = Array.isArray(git.dirty) ? git.dirty : []
  const pr = git.pull_request
  // The patch is an ordinary artifact; matching by name is what turns the
  // recorded name into the GCS uri without minting a second copy of it.
  const patch = git.patch ? artifacts.find((a) => a.name === git.patch) : undefined
  // CODE HANDED ON RATHER THAN PUSHED (#278). A workflow step can change
  // nothing in its clone and still write a patch into its artifacts for a
  // dependant to stage -- the implement step of an implement -> review -> fix
  // workflow does exactly that. "changed nothing in the repository" stays
  // true, and it is not the whole story: the diff files are what went on.
  const changedNothing = commits.length === 0 && dirty.length === 0 && patch === undefined
  const handed =
    changedNothing && task.workflow_id !== null && task.step_id !== null
      ? artifacts.filter((a) => artifactKind(a.name) === 'diff')
      : []

  const refusals = publishRefusals(task, attempts)
  // A CREDENTIAL REFUSAL WITHHOLDS THE DIFF: the refused file's lines are
  // never drawn, redacted or not (agent-detail-2.html A4), so the patch that
  // carries them is not offered here at all.
  const withhold = refusals.some((r) => r.kind === 'credential')

  return (
    // NO HEADING OF ITS OWN (#102): the panel around it says `Code`.
    // THE ORDER IS agent-detail-2.html A's: one sentence on what happened and
    // what to do, the base-pin line, then the per-file diff, then the facts.
    <div className="git-outcome">
      <AgCodeLead git={git} task={task} refusals={refusals} />
      <AgBasePin git={git} />
      {patch !== undefined && !withhold && <AgPatchDiff taskId={task.id} patch={patch} />}

      {git.error ? (
        <p className="warn-text">
          <Mark
            kind="unread"
            say="The change could not be read from the workspace. This says nothing about whether the agent did work — only that git could not be asked."
          />{' '}
          {git.error}
        </p>
      ) : null}

      {handed.length > 0 && task.workflow_id !== null && task.step_id !== null && (
        <HandedOn task={task} workflowId={task.workflow_id} stepId={task.step_id} files={handed} readAt={readAt} />
      )}

      <ul className="ctl-facts">
        <li className={`ctl-fact${commits.length === 0 ? ' is-absent' : ''}`}>
          <b>commits</b>
          {commits.length === 0 ? (
            <>
              0
              {dirty.length > 0 && (
                <>
                  {' '}
                  <Mark
                    kind="zero"
                    say="The agent edited files and never ran git commit, which is the common case. The changes are in the patch and in the uncommitted count beside this."
                  />
                </>
              )}
            </>
          ) : (
            <>
              {git.commit_count ?? commits.length} on{' '}
              <span className="mono">{git.base ? git.base.slice(0, 10) : '—'}</span>
              {typeof git.insertions === 'number' && typeof git.deletions === 'number' && (
                <> · +{git.insertions} −{git.deletions}</>
              )}
            </>
          )}
        </li>

        {dirty.length > 0 && (
          <li className="ctl-fact">
            <b>dirty</b>
            {git.dirty_count ?? dirty.length}
            {git.dirty_truncated && ' · truncated'}
          </li>
        )}

        <li className={`ctl-fact${patch ? '' : ' is-absent'}`}>
          <b>patch</b>
          {patch ? (
            <>
              <span className="mono uri">{patch.uri}</span>
              <Button
                size="sm"
                className="copy"
                onClick={() => navigator.clipboard?.writeText(`gsutil cat ${patch.uri} | git apply -`)}
              >
                copy apply
              </Button>
            </>
          ) : git.patch_omitted ? (
            <>
              <Mark
                kind="partial"
                say="The patch was discarded for exceeding the size cap. It was NOT truncated: a truncated patch applies cleanly and silently drops the rest of the change."
              />{' '}
              discarded at {num(git.patch_bytes)} bytes
            </>
          ) : (
            <>
              <Em />{' '}
              <Mark
                kind="zero"
                say="Nothing differed from the clone, so there is no patch. This is a real zero rather than a patch that failed to upload."
              />
            </>
          )}
        </li>

        {git.branch && (
          <li className="ctl-fact">
            <b>branch</b>
            <span className="mono">
              {git.branch}
              {git.pushed_head && ` · ${git.pushed_head.slice(0, 10)}`}
            </span>
          </li>
        )}

        {git.repository && (
          <li className="ctl-fact">
            <b>repo</b>
            <span className="mono uri">{git.repository}</span>
          </li>
        )}

        {pr && (
          <li className="ctl-fact">
            <b>pr</b>
            <a href={pr.url} target="_blank" rel="noreferrer">
              #{pr.number}
            </a>{' '}
            {/* The pull request's own state, from the same vocabulary as every
                other state on this screen. An open PR is `info` rather than
                `ok`: it is a FACT about the branch, not a verdict that the run
                went well (§1.3 -- the accent is a fact or a link, never a
                verdict), and `is-info`'s flat bar is the mark that says so. */}
            <ToneMark tone={pr.state === 'open' ? 'info' : 'ok'}>{pr.state}</ToneMark>
            {pr.created === false && ' · reused'}
          </li>
        )}

        {git.auto_committed && (
          <li className="ctl-fact">
            <b>by</b>
            worker
            <Mark
              kind="partial"
              say="One commit on this branch was made by the worker, not the agent: the agent left changes uncommitted and they would otherwise not have reached the branch at all."
            />
          </li>
        )}
      </ul>

      {/* THE SHAPE OF THE CHANGE, ABOVE THE TABLE THAT STATES IT. The table
          keeps every subject and exact count as text; the diverging bars show
          at a glance which commit carried the work and which only deleted.
          A binary change gets a diamond, never a 0/0 row -- `binary_files`
          exists so it does not read as "changed nothing". */}
      {commits.length > 0 && <DiffstatChart commits={commits} commitCount={git.commit_count} />}

      {commits.length > 0 && (
        /* `is-stacked` (F6), and this is the widest of the drawer's tables:
           four columns in a panel that is 413px at its default and 390 at a
           phone. The subject is the long one and it is the column a reader is
           here for, so squeezing all four is the worst of the options —
           stacked, the sha leads the record and the subject gets the full width
           under it. (Unbraced, for the reason the checkpoint table's comment
           gives.) */
        <div className="ctl-table is-stacked" style={{ marginTop: 10 }}>
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Commit</th>
                <th role="columnheader" scope="col">Subject</th>
                <th role="columnheader" scope="col" className="is-num">Files</th>
                <th role="columnheader" scope="col" className="is-num">+/−</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {commits.map((c) => (
                <tr role="row" key={c.sha}>
                  <th role="rowheader" scope="row" className="mono">
                    {c.sha.slice(0, 10)}
                  </th>
                  <td role="cell" data-label="Subject">{c.subject}</td>
                  <td role="cell" data-label="Files" className="is-num">
                    {c.files_changed}
                    {/* git prints `-` for both counts on a binary change; those
                        are counted separately rather than folded into a 0/0
                        line count that would read as "changed nothing". */}
                    {c.binary_files > 0 && (
                      <span className="ctl-sub">{c.binary_files} binary</span>
                    )}
                  </td>
                  <td role="cell" data-label="+/−" className="is-num">
                    +{c.insertions} −{c.deletions}
                  </td>
                </tr>
              ))}
            </tbody>
            {(git.commit_count ?? 0) > commits.length && (
              // THE FRACTION IS THE CAVEAT. "the patch above carries all of
              // them" is a standing fact about how the worker harvests, and
              // the patch row above is one line up.
              <caption>
                newest {commits.length} of {git.commit_count}
              </caption>
            )}
          </table>
        </div>
      )}
    </div>
  )
}

/**
 * WHERE A STEP'S DIFF WENT: which dependant stages it, what is in it, and the
 * pull request it ended up in (#278).
 *
 * Every part is an existing read. The edges are the workflow's `input_from`
 * map (`GET /v1/workflows/{id}`, the read the Artifacts pane makes for staged
 * inputs); the figures are the diff's own lines, read through the artifact
 * content route the viewer uses; the pull request is the integrator's own
 * result summary, from the same workflow read. Re-read with the drawer, so a
 * pull request that is opened while this is on screen appears.
 *
 * A file no dependant stages is not "handed on", and is left to Artifacts.
 */
function HandedOn({
  task,
  workflowId,
  stepId,
  files,
  readAt,
}: {
  task: Task
  workflowId: string
  stepId: string
  files: ArtifactRef[]
  readAt: number | null
}) {
  const { state } = useRead(() => loadWorkflow(workflowId), workflowId, `${readAt ?? ''}`, null)
  if (state.status === 'loading') return null
  if (state.status !== 'ok' && state.status !== 'stale') {
    return (
      <p className="git-handed">
        <Mark
          kind="unread"
          say="The workflow could not be read, so whether a later step stages this diff, and what it opened, is unknown."
        />{' '}
        {files.map((f) => f.name).join(', ')} · handed on: not read
      </p>
    )
  }
  const { workflow, tasks } = state.data
  const steps = Array.isArray(workflow.steps) ? workflow.steps : []
  const to = (name: string) =>
    steps
      .filter((s) => typeof s.input_from === 'object' && s.input_from !== null && s.input_from[stepId] === name)
      .map((s) => s.step_id)
  const handedOn = files.map((f) => ({ file: f, to: to(f.name) })).filter((h) => h.to.length > 0)
  if (handedOn.length === 0) return null
  // THE INTEGRATOR'S PULL REQUEST, once it exists. The integrator is the one
  // step whose stored dispatch names that role; its own git outcome carries
  // the pull request the whole workflow opens.
  const integrator = (Array.isArray(tasks) ? tasks : []).find((t) => dispatchOf(t)?.role === 'integrator')
  const pr = integrator === undefined ? null : pullRequestOf(integrator)
  return (
    <div className="git-handed">
      {handedOn.map((h) => (
        <HandedFile key={h.file.name} task={task} file={h.file} to={h.to} />
      ))}
      {pr !== null && (
        <p className="git-handed-pr">
          <b>pr</b>{' '}
          <a href={pr.url} target="_blank" rel="noreferrer">
            #{pr.number}
          </a>{' '}
          <ToneMark tone={pr.state === 'open' ? 'info' : 'ok'}>{pr.state}</ToneMark>
          {integrator?.step_id ? ` · opened by ${integrator.step_id}` : ''}
        </p>
      )}
    </div>
  )
}

/**
 * One handed-on diff: `+N −M in K files, handed to <step> as <name>`, and the
 * diff itself in the artifact viewer. The figures and the viewer are ONE read:
 * the viewer is handed the answer rather than asking again.
 */
function HandedFile({ task, file, to }: { task: Task; file: ArtifactRef; to: string[] }) {
  const [open, setOpen] = useState(true)
  // A finished task's artifacts do not change, so this reads once.
  const { state } = useRead(() => loadArtifactContent(task.id, file.name), `${task.id}/${file.name}`, '', null)
  const read = state.status === 'ok' || state.status === 'stale' ? state.data : null
  const load = useCallback(
    (): Promise<Result<ArtifactContent>> => Promise.resolve(state as Result<ArtifactContent>),
    [state],
  )
  const text = read !== null && read.status === 'ok' && read.content !== null ? read.content : null
  const stat = text === null ? null : diffStat(text)
  const whole = read !== null && read.offset === 0 && read.next_offset === null && !read.truncated
  const handed = `handed to ${to.join(', ')} as ${file.name}`
  return (
    <div className="git-handed-file">
      <p className="git-handed-line">
        {stat === null ? (
          state.status === 'loading' ? (
            <>
              <Mark kind="pending" say={`Reading ${file.name}.`} /> {handed}
            </>
          ) : (
            <>
              <Mark
                kind="unread"
                say={`${file.name} could not be read as text, so what it changes is not counted here. The Artifacts pane lists it.`}
              />{' '}
              {handed}
            </>
          )
        ) : (
          <>
            +{stat.insertions} −{stat.deletions} in {stat.files} file{stat.files === 1 ? '' : 's'}, {handed}
            {!whole && (
              <>
                {' '}
                <Mark
                  kind="partial"
                  say="Counted over the window the content route served, which is not the whole diff. The rest of it was not counted."
                />
              </>
            )}
          </>
        )}
        {!open && (
          <Button size="sm" className="copy" onClick={() => setOpen(true)}>
            show diff
          </Button>
        )}
      </p>
      {open && read !== null && (
        <ArtifactViewer taskId={task.id} artifact={file} onClose={() => setOpen(false)} load={load} backLabel="Close" />
      )}
    </div>
  )
}

/**
 * The causes, each with the response it actually needs.
 *
 * TWO OF THESE USED TO BE DIAGNOSED WRONG, and both wrong answers sent someone
 * to do work that would have made things worse. Neither was a rendering bug:
 * the screen had no access to the dispatch and was matching free text, so the
 * two outcomes that are CORRECT BY REQUEST were indistinguishable from failures.
 *
 *  * `collect` -- the DEFAULT, so this was the common case -- produces
 *    `published: false` with a reason that matched none of the patterns below,
 *    and fell through to "the reason is git's own / a rejected push usually
 *    means something else moved the branch". Nothing was pushed and nothing was
 *    rejected: the caller asked for the patch to be harvested and it was.
 *  * an `integrate` CONTRIBUTOR produces `published: true` with no pull request,
 *    and read as "only the pull-request call did not complete, so opening one
 *    by hand from that branch is all that is left". Opening one by hand is the
 *    one thing that must not happen -- it is a second pull request against a
 *    strategy whose whole promise is exactly one, and the integrator is already
 *    going to merge that branch.
 *
 * So the two are keyed on the STRUCTURED dispatch now, not on prose. The
 * worker's own `strategy`/`role` fields in the git summary come first because
 * they record what the run did; the task's stored dispatch answers for a
 * summary written before those fields existed. The prose patterns stay as a
 * third fallback for a task whose dispatch cannot be read at all.
 */
// ---------------------------------------------------------------------------
// The Code card's lead, its base pin and its diff (agent-detail-2.html, pick A)
// ---------------------------------------------------------------------------

/** One publish refusal an attempt recorded (PUBLISH_REFUSED, contract request 29). */
export interface PublishRefusal {
  /** 1-based, in generation order. */
  attempt: number
  generation: number | null
  kind: 'credential' | 'title'
  /** The file a credential refusal names. Never its content: the worker never records it. */
  file: string | null
}

/**
 * THE WORKER'S TWO REFUSALS, BY ITS OWN WORDS. Until request 29 lands the
 * worker records them as `OUTPUTS_MISSING` and `RUNNER_ERROR` with these
 * errors (agent_worker/lifecycle.py `_fail_for_final_tree_leak`,
 * `_refused_title_reason`), so the error text is the only signal; the end
 * cause `publish_refused` is read too, for the day it is written.
 */
const CREDENTIAL_REFUSAL = /the final tree adds a credential in (.+?) \(rule /
const TITLE_REFUSAL = /pr-title\.txt refused: /

function refusalOf(error: string | null | undefined): Omit<PublishRefusal, 'attempt' | 'generation'> | null {
  if (typeof error !== 'string') return null
  const cred = CREDENTIAL_REFUSAL.exec(error)
  if (cred !== null) return { kind: 'credential', file: cred[1] ?? null }
  if (TITLE_REFUSAL.test(error)) return { kind: 'title', file: null }
  return null
}

export function publishRefusals(task: Task, attempts: readonly AttemptRow[] | null): PublishRefusal[] {
  if (attempts !== null && attempts.length > 0) {
    const ordered = [...attempts].sort((a, b) => a.generation - b.generation)
    const out: PublishRefusal[] = []
    ordered.forEach((a, i) => {
      const r = refusalOf(a.error)
      if (r !== null) out.push({ ...r, attempt: i + 1, generation: a.generation })
    })
    if (out.length > 0) return out
  }
  // No attempt documents, or none that says: the task's own last error.
  const last = refusalOf(task.last_error)
  if (last !== null) return [{ ...last, attempt: task.attempt_count, generation: task.current_generation ?? null }]
  if (task.end_cause === 'publish_refused') {
    return [{ kind: 'title', file: null, attempt: task.attempt_count, generation: task.current_generation ?? null }]
  }
  return []
}

/**
 * FAILED `published_nothing`: a step meant to open a pull request whose branch
 * has no commits beyond its base. NOT WRITTEN BY ANY WORKER ON MAIN; the shape
 * is the mock-up's reading of the brief -- the end cause, or the publish
 * reason's own word -- and a summary without either is never read as it.
 */
export function publishedNothing(task: Task, git: GitSummary): boolean {
  if (task.state !== 'FAILED') return false
  return task.end_cause === 'published_nothing' || (git.publish_reason ?? '').toLowerCase().startsWith('published_nothing')
}

/**
 * THE CODE CARD'S ONE SENTENCE: what happened, and what to do. A pull request
 * opened, a refusal, a step that published nothing -- or, for every other
 * missing pull request, the six causes `PublishOutcome` tells apart, with an
 * unrecognised reason printed as the worker wrote it.
 */
function AgCodeLead({ git, task, refusals }: { git: GitSummary; task: Task; refusals: PublishRefusal[] }) {
  if (refusals.length > 0) {
    const times = refusals.length === 1 ? '' : ` ${refusals.length} times`
    return (
      <div className="ag-code-lead is-bad" role="status">
        <Absent
          kind="failed"
          heading={`Publish refused${times}. Nothing was pushed.`}
          say="The worker refused to publish this attempt's work. Each refusal fails that attempt and retries it, and the next attempt starts from the last checkpoint with the agent's files in place."
        >
          Each refusal fails its attempt and retries it; fix what each one names below.
        </Absent>
        <ul className="ag-refusals">
          {refusals.map((r) => (
            <li key={`${r.attempt}:${r.kind}`} className="ag-refusal">
              <b>
                Attempt {r.attempt}
                {r.generation !== null ? ` · gen ${r.generation}` : ''} ·{' '}
                {r.kind === 'credential' ? 'a credential in the final tree' : 'an unusable pr-title.txt'}
              </b>{' '}
              {r.kind === 'credential' ? (
                <>
                  in <code>{r.file ?? 'a file the error does not name'}</code>; remove it. Only the file is named: its
                  content is never shown here, redacted or not.
                </>
              ) : (
                <>write one line with no task id and no attribution.</>
              )}{' '}
              <span className="ctl-sub">publish_refused</span>
            </li>
          ))}
        </ul>
      </div>
    )
  }
  if (publishedNothing(task, git)) {
    return (
      <div className="ag-code-lead is-bad">
        <Absent
          kind="failed"
          heading="Failed: nothing to publish"
          say="This step opens a pull request, but its branch has no commits beyond its base and no files were left uncommitted, so nothing was pushed and there is nothing to review."
        >
          The branch has no commits beyond its base, so nothing was pushed; read the agent&apos;s log for why it stopped.
        </Absent>
      </div>
    )
  }
  const pr = git.pull_request
  if (pr) {
    return (
      <div className="ag-code-lead is-ok">
        <p className="ag-code-sentence">
          <b>
            Pull request{' '}
            <a href={pr.url} target="_blank" rel="noreferrer">
              #{pr.number}
            </a>{' '}
            {pr.created === false ? 'reused' : 'opened'}
          </b>
          {git.branch ? ` from ${git.branch}` : ''}: review it on the forge.
        </p>
      </div>
    )
  }
  return <PublishOutcome git={git} task={task} />
}

/**
 * THE BASE-PIN LINE (`result_summary.git.base_pin`): the commit this run's
 * clone was pinned to and where it came from, or that it was not pinned and
 * why. No worker on main writes it, so a summary without it says "not
 * recorded" -- never "not pinned", which would be a claim.
 */
function AgBasePin({ git }: { git: GitSummary }) {
  const pin = git.base_pin
  const cloned = typeof git.base === 'string' && git.base !== '' ? git.base.slice(0, 10) : null
  if (pin === undefined || pin === null || typeof pin !== 'object') {
    return (
      <p className="ag-basepin is-absent">
        <b>Base</b> <Em />{' '}
        <Mark
          kind="absent"
          say="This worker does not record where the clone's base was pinned (result_summary.git.base_pin), so whether it was is not known."
        />{' '}
        pin not recorded
        {cloned !== null && (
          <>
            {' · cloned at '}
            <span className="mono">{cloned}</span>
          </>
        )}
      </p>
    )
  }
  if (pin.pinned === true) {
    const from = pin.from ?? pin.upstream_task_id ?? null
    return (
      <p className="ag-basepin is-pinned">
        <b>Base</b> pinned to <span className="mono">{String(pin.sha).slice(0, 10)}</span>
        {from !== null ? (
          <>
            , from <span className="mono">{from}</span>
          </>
        ) : (
          ', its source not recorded'
        )}
      </p>
    )
  }
  return (
    <p className="ag-basepin is-unpinned">
      <b>Base</b> not pinned · {typeof pin.reason === 'string' && pin.reason !== '' ? pin.reason : 'no reason recorded'}
      {cloned !== null && (
        <>
          {' · cloned at '}
          <span className="mono">{cloned}</span>
        </>
      )}
    </p>
  )
}

/**
 * THE PER-FILE DIFF, ON REQUEST: the approved DiffView (#310) through the
 * artifact viewer, which reads the patch by its manifest name. Behind a button
 * because it is one more read of a file that can be large, and Details
 * re-reads every 10 s.
 */
function AgPatchDiff({ taskId, patch }: { taskId: string; patch: ArtifactRef }) {
  const [open, setOpen] = useState(false)
  if (!open) {
    return (
      <p className="ag-diff-open">
        <Button size="sm" className="copy" onClick={() => setOpen(true)}>
          Show the diff
        </Button>{' '}
        <span className="ctl-sub">
          {patch.name} · {num(patch.bytes)} bytes
        </span>
      </p>
    )
  }
  return <ArtifactViewer taskId={taskId} artifact={patch} onClose={() => setOpen(false)} backLabel="Code" />
}

function PublishOutcome({ git, task }: { git: GitSummary; task: Task }) {
  const reason = git.publish_reason ?? null
  const lower = (reason ?? '').toLowerCase()
  const stored = dispatchOf(task)
  // The run's own record wins over the task's, and neither is invented: an
  // unrecognised value falls through to null rather than being coerced.
  const strategy: DispatchStrategy | null =
    git.strategy === 'collect' || git.strategy === 'direct-pr' || git.strategy === 'integrate'
      ? git.strategy
      : (stored?.strategy ?? null)
  const role: DispatchRole | null =
    git.role === 'contributor' || git.role === 'integrator' ? git.role : (stored?.role ?? null)

  // THE PROSE IS THE THIRD FALLBACK, not the first, and it exists for one case:
  // a task whose dispatch this API did not report AND whose summary predates the
  // worker's structured fields. Both substrings are the worker's own, quoted
  // from `_publish` in agent_worker/lifecycle.py -- see the seam test in
  // tests/unit/control_plane/test_dispatch_ui_surface.py, which asserts these
  // still appear there verbatim.
  const isCollect = strategy === 'collect' || lower.includes("strategy is 'collect'")
  const isContributor = role === 'contributor' || lower.includes('this step is a contributor')

  // 1. There is a pull request. Nothing to explain.
  if (git.pull_request) return null

  // 2. A CONTRIBUTOR IN AN `integrate` WORKFLOW. Pushed, deliberately opened
  //    nothing, and must not be "finished off" by hand. Ahead of the general
  //    pushed-but-no-PR case below, which would tell you to do exactly that.
  if (isContributor && git.published === true) {
    // THE ONE SENTENCE IS NOT NEGOTIABLE AND IT IS NOT THE EXPLANATION. §8.5
    // allows an empty state one sentence; this is the sentence that stops an
    // operator doing the expensive wrong thing, so it is the one kept and the
    // description of what `integrate` is went to `#help/dispatch-strategies`.
    return (
      <Absent
        kind="zero"
        heading="pushed · no pull request, by request"
        say="This step is a contributor in an integrate workflow. Its branch is on the forge and the integrating step merges it into the single pull request the whole workflow opens."
        foot={reason ?? undefined}
        explain="dispatch-strategies"
      >
        <strong>Do not open one from this branch</strong> — that is the second
        pull request this strategy exists to prevent.
      </Absent>
    )
  }

  // 3. PUSHED, NO PULL REQUEST. The branch is on the forge; only the final API
  //    call did not land. Re-running the agent would be the wrong response.
  if (git.published === true) {
    return (
      <Absent
        kind="partial"
        heading={`pushed · no pull request${git.branch ? ` · ${git.branch}` : ''}`}
        say="The work reached the forge. Only the pull-request call did not complete, so the branch exists and the pull request does not."
        foot={reason ?? undefined}
      >
        Open one by hand from that branch — re-running this agent would
        duplicate work that already exists.
      </Absent>
    )
  }

  // 4. `collect`: the caller asked for nothing to be pushed. Before the
  //    strategy check below the forge was never even contacted, so there is no
  //    can_push, no branch and no git error to report -- and this fell through
  //    to the "rejected push" ending, which described a push that never
  //    happened. It is checked ahead of can_push for that reason: on this path
  //    the worker returns before `probe_repository`, so every field those
  //    branches read is absent.
  if (isCollect) {
    // WHAT THE THREE STRATEGIES ARE is `#help/dispatch-strategies`, which is
    // where the two sentences about `direct-pr` and `integrate` went. What is
    // on the glass is that nothing was pushed BY REQUEST, which is the fact
    // that stops someone debugging a token scope.
    return (
      <Absent
        kind="zero"
        heading="nothing pushed · collect"
        say="collect is the default strategy: the agent's work is harvested into this task's patch and artifacts and the repository is never written to. Nothing failed, and no token or forge setting changes this."
        foot={reason ?? undefined}
        explain="dispatch-strategies"
      />
    )
  }

  // 5. The tenant's token cannot push. EXPECTED TODAY, and a fact rather than a
  //    failure: the secret holds a clone token, the forge was asked and said
  //    no. The patch is the deliverable.
  if (git.can_push === false) {
    return (
      <Absent
        kind="zero"
        heading="not published · token cannot write"
        say="The forge was asked and refused write access, so the patch is the deliverable. Nothing failed — granting the tenant's credential write scope is what changes this."
        foot={reason ?? undefined}
      />
    )
  }

  // 6. No reason at all.
  if (reason === null) {
    return (
      <Absent
        kind="partial"
        heading="no pull request · no reason recorded"
        say="The worker writes publish_reason on every path, including the successful ones, so its absence means this summary was written by something other than the publish step."
      />
    )
  }

  // 7+. No structured signal beyond `published: false`, so the free-text
  //      reason is matched -- carefully. An unrecognised reason is printed
  //      verbatim and labelled as one, never forced into a bucket.
  //
  // EACH IS NOW A HEADING AND A SENTENCE ON THE MARK, not a heading and a
  // paragraph. The headings are the causes, unchanged in substance and shorter
  // in words, because the cause is what a reader acts on; the reasoning that
  // used to sit under each one is the mark's accessible name. `kind` still
  // separates the six, and it separates them the way §8.7.3 requires: a
  // deliberate outcome is a real zero, a failure to reach the forge is a
  // partial read, and neither is painted as the other.
  const known:
    | { match: string; heading: string; say: string; kind: 'zero' | 'partial' }
    | undefined = [
    {
      match: 'changed nothing',
      heading: 'not published · the agent changed nothing',
      say: 'The workspace was identical to the clone, so there was nothing to push. This is a statement about the run, not about publishing — the agent did no work on the repository.',
      kind: 'zero' as const,
    },
    {
      match: 'parked',
      heading: 'not published · this attempt parked',
      say: 'Publishing waits for the run to finish, and this attempt did not finish — it checkpointed and released its capacity. The next attempt resumes from the checkpoint and publishes then. Nothing is lost and nothing needs doing.',
      kind: 'zero' as const,
    },
    {
      match: 'not on a forge',
      heading: 'not published · not a forge we can publish to',
      say: 'The repository is not on a forge this worker knows how to open a pull request against, so the patch is the deliverable — apply it by hand.',
      kind: 'zero' as const,
    },
    {
      match: 'could not reach the forge',
      heading: 'not published · the forge did not answer',
      say: "The forge could not be reached at all, so nothing is known about whether publishing would have worked. This says nothing about the agent's work, which is in the patch.",
      kind: 'partial' as const,
    },
    {
      match: 'publishing is disabled',
      heading: 'not published · publishing is off for this worker',
      say: 'A configuration decision, not a failure. The patch is the deliverable.',
      kind: 'zero' as const,
    },
    {
      match: 'repository url is unknown',
      heading: 'not published · repository url unknown',
      say: 'The attempt had no repository to publish to. If the task was meant to have one, it was submitted without repository_url.',
      kind: 'zero' as const,
    },
  ].find((c) => lower.includes(c.match))

  if (known !== undefined) {
    return <Absent kind={known.kind} heading={known.heading} say={known.say} foot={reason} />
  }

  // The push itself was rejected -- a GitError, so the reason is git's own
  // message. This is the "something else moved the branch" case and it is the
  // one that needs a person to look at the branch.
  return (
    <Absent
      kind="partial"
      heading="not published · git's own message"
      say="The push did not land. A rejected push usually means something else moved the branch — this screen does not recognise the message well enough to say which, so it is reproduced exactly as the worker recorded it."
      foot={reason}
    />
  )
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

/**
 * What this run was asked to do.
 *
 * `input` is a free-form dict: the API accepts it whole and each runner reads
 * what it needs. `input.prompt` is the CONVENTION the CLI agents and the mock
 * runner use, not a schema field, so it is surfaced as the prompt when it is a
 * string, and the rest of the object is shown underneath -- or the whole
 * object, when there is no prompt string (`RestOfInput`). A screen that only
 * rendered `prompt` would show nothing for a runner that names it differently.
 *
 * MASKED, WITH A COUNT (#184, owner decision of 2026-09-25). The prompt and
 * the input under it come from the API's read-time-redacted copy (`GET
 * /v1/tasks/{id}/input`), never from `task.input`, which is the document as
 * submitted: a token pasted into a prompt was drawn here in clear. Each block
 * says `masked N` beside it, in the ink the artifact viewer's count takes.
 * When the copy was not read, the block says so -- it is never replaced by
 * the raw input.
 *
 * THE METADATA TABLE IS MASKED TOO (the owner's "mask it everywhere",
 * 2026-09-26). It drew `task.metadata` as submitted, between two masked
 * blocks: `TaskCreate.metadata` is caller-supplied, and a token in it was
 * drawn in clear. It now draws the copy's `metadata` block -- the caller's
 * keys through the same masker as the input, the platform's own keys
 * (`dispatch`, `input_from`, `expected_outputs`) as stored, each such row
 * saying so -- with its own `masked N`. A copy that was not read, or an API
 * that does not serve the block, is said as that; `task.metadata` is never
 * drawn in its place.
 *
 * ITS OWN READ, NOT THE DRAWER'S (PR #210 re-review). The copy rode in
 * `loadAgentRun`'s `Promise.all`, so the whole drawer waited on it. It is read
 * here, through the same once-per-task cache the Artifacts pane uses, and a
 * failed read is asked again with each re-read of the drawer (`readAt`), so a
 * copy that failed once is not `input not read` until the drawer is reopened.
 */
function Input({ run, readAt }: { run: AgentRun; readAt: number | null }) {
  const { task } = run
  const { state: copy } = useRead<TaskInputCopy>(
    () => loadTaskInputOnce(task.id),
    task.id,
    `input:${readAt ?? ''}`,
    null,
  )
  const served = copy.status === 'ok' || copy.status === 'stale' ? copy.data : null
  const prompt = served?.prompt ?? null
  const keys = served?.metadata === null || served?.metadata === undefined ? null : Object.keys(served.metadata.value).length

  // FOLDED (agent-details-v3.html A): a facts line, a three-line preview of
  // the prompt in the sans face, and the whole prompt and the metadata each
  // behind a disclosure. The whole prompt is no longer a 320px box with its
  // own scroller inside the pane's: the pane is the one scroll container.
  return (
    <section className="dt-card dt-input">
      <DtCardHead title="Input">
        {prompt !== null && <MaskedNote count={prompt.redaction_count} />}
        {/* The input as submitted, read; `input-is-opaque` is the Submit
            form's ("whatever you type"), and nothing is typed here (G2-10). */}
        <HelpCard topic="input-as-submitted" />
      </DtCardHead>
      {/* WHAT THE PROFILE NAME MEANS IS NOT ON THIS PAGE: the catalogue is
          frozen and no route serves it (`#help/runner-profile-by-name`). The
          profile and class are the header's meta line; what the submission
          carried besides them is here. */}
      <ul className="ctl-facts dt-ifacts">
        <li className={`ctl-fact${task.repository_url === null ? ' is-absent' : ''}`}>
          <b>repo</b>
          <span className="uri mono">
            {task.repository_url ?? <Em />}
            {task.repository_ref && ` @ ${task.repository_ref}`}
          </span>
        </li>
        {/* Recorded for attribution. It selects NOTHING about the container --
            image, command and resource spec all come from the profile. */}
        <li className={`ctl-fact${task.model === null ? ' is-absent' : ''}`}>
          <b>model</b>
          {task.model ?? <Em />}
        </li>
        <li className={`ctl-fact${task.provider === null ? ' is-absent' : ''}`}>
          <b>provider</b>
          {task.provider ?? <Em />}
        </li>
        <li className="ctl-fact">
          <b>priority</b>
          {task.priority}
        </li>
        <li className={`ctl-fact${task.timeout_seconds === null ? ' is-absent' : ''}`}>
          <b>timeout</b>
          {task.timeout_seconds !== null ? `${task.timeout_seconds}s` : <Em />}
        </li>
      </ul>

      {served === null ? (
        <InputNotRead copy={copy} />
      ) : prompt === null ? (
        <p className="att-none">
          <Mark
            kind="zero"
            say={
              served.prompt_key === 'other'
                ? "This task's input has a prompt key whose value is not text, so there is no prompt to show. The full input is under Metadata."
                : "This task's input has no prompt string. That is legitimate — the field is a convention of the CLI and mock runners, not part of the submission schema. The full input is under Metadata."
            }
          />{' '}
          {served.prompt_key === 'other' ? 'prompt is not text' : 'no prompt key'}
        </p>
      ) : prompt.text === '' ? (
        <p className="att-none">
          <Mark kind="zero" say="The prompt was submitted empty. A real, empty prompt, not a missing one." /> empty prompt
        </p>
      ) : (
        <>
          <p className="dt-prev">{prompt.text}</p>
          <details className="dt-disc">
            <summary>Show prompt · {prompt.text.length.toLocaleString('en-US')} characters</summary>
            <pre className="json dt-prompt">{prompt.text}</pre>
          </details>
        </>
      )}

      <details className="dt-disc">
        <summary>Metadata{keys === null ? '' : ` · ${keys} ${keys === 1 ? 'key' : 'keys'}`}</summary>
        {/* NO PROFILE OR CLASS HERE: the header's meta line says them, once
            (walkthrough B, owner 2026-10-03). */}
        <MaskedMetadataBlock served={served} copy={copy} />
        {served !== null && <RestOfInput served={served} />}
      </details>

      {/* redesign-v2 Panel 3: the files staged into the workspace, each linked
          back to the run that produced it. Renders nothing for a run that
          declared no input and reported none. See StagedInputs.tsx. */}
      <StagedInputs task={task} />
    </section>
  )
}

/**
 * THE TASK'S METADATA, AS THE API MASKED IT (the owner's "mask it everywhere",
 * 2026-09-26). Drawn from the `/input` copy's `metadata` block, which the same
 * masker as the input made, so a literal masked in the prompt is masked here
 * too. A row whose key only the platform writes is served as stored, and the
 * row says so: those keys are not masked, and a reader should not take them
 * for masked ones.
 *
 * NEVER `task.metadata` IN ITS PLACE. A copy still reading, a copy that failed
 * and an API that does not serve the block are each said as that.
 */
function MaskedMetadataBlock({ served, copy }: { served: TaskInputCopy | null; copy: Result<TaskInputCopy> }) {
  const block = served?.metadata ?? null
  const entries = block === null ? [] : Object.entries(block.value)
  const platform = new Set(block?.platform_keys ?? [])
  return (
    <div className="section" style={SUB}>
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">metadata</span>
        {block !== null && <MaskedNote count={block.redaction_count} />}
      </div>
      {served === null ? (
        <InputNotRead copy={copy} />
      ) : block === null ? (
        <p className="att-none">
          <Mark
            kind="absent"
            say="The deployment answering this UI does not serve the masked copy of a task's metadata yet. The metadata is not drawn unmasked in its place."
          />{' '}
          masked metadata not served by this API
        </p>
      ) : entries.length === 0 ? (
        <p className="att-none">
          <Mark
            kind="zero"
            say="Submitted with no metadata. The read succeeded and the object is empty, so this is a real zero."
          />{' '}
          submitted with none
        </p>
      ) : (
        <div className="ctl-table">
          <table>
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Value</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([k, v]) => (
                <tr key={k}>
                  <th scope="row" className="mono">
                    {k}
                    {platform.has(k) && (
                      <>
                        {' '}
                        <span className="ctl-sub">platform · as stored</span>
                      </>
                    )}
                  </th>
                  {/* Metadata is arbitrary, so a nested object is stringified
                      rather than rendered -- React would throw on an object
                      child, and "[object Object]" is worse than the JSON. */}
                  <td>{typeof v === 'string' ? v : JSON.stringify(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/**
 * WHAT IS UNDER THE PROMPT: the rest of the input when the prompt block drew
 * a prompt, the whole input when it did not -- as the Artifacts pane draws
 * them.
 *
 * NEVER THE WHOLE INPUT UNDER ITS OWN PROMPT (the PR #210 review). This block
 * drew `full` under the prompt, so the prompt was on screen twice, once in
 * each block's masking, and `full` was where a secret quoted inside the
 * prompt came out in clear while the block above masked it. The route now
 * masks both the same way; drawing `rest` is what keeps one prompt one block
 * with one count. Each block's `masked N` is its own, so the two counts on
 * screen add up to the input's.
 */
function RestOfInput({ served }: { served: TaskInputCopy }) {
  if (served.prompt === null) {
    return (
      <div className="section" style={SUB}>
        <div className="ctl-toolbar att-sub-head">
          <span className="ctl-eyebrow">full input</span>
          <MaskedNote count={served.full.redaction_count} />
        </div>
        <pre className="json" style={{ maxHeight: 320, overflowY: 'auto' }}>
          {served.full.text}
        </pre>
      </div>
    )
  }
  return (
    <div className="section" style={SUB}>
      <div className="ctl-toolbar att-sub-head">
        <span className="ctl-eyebrow">rest of the input</span>
        {served.rest !== null && <MaskedNote count={served.rest.redaction_count} />}
      </div>
      {served.rest === null ? (
        <p className="att-none">
          <Mark kind="zero" say="The prompt is the whole of this task's input: nothing else was submitted with it." />{' '}
          nothing else submitted
        </p>
      ) : (
        <pre className="json" style={{ maxHeight: 320, overflowY: 'auto' }}>
          {served.rest.text}
        </pre>
      )}
    </div>
  )
}

/**
 * `masked N` beside a block of the input: how many credential-shaped runs the
 * API masked in what the block draws. A measured fact, drawn as the artifact
 * viewer draws its count (AG-5): no mark, and `--warn` ink above zero.
 */
export function MaskedNote({ count }: { count: number }) {
  return (
    <span className="is-end ctl-card-note">
      masked <span className={`art-masked${count > 0 ? ' is-warn' : ''}`}>{count}</span>
    </span>
  )
}

/**
 * THE COPY WAS NOT READ, and nothing stands in for it. The raw `task.input`
 * is right there on the task document, and drawing it here would be exactly
 * the unmasked prompt this read exists to replace -- so a failed or missing
 * copy is said as one, and the input is not shown.
 */
function InputNotRead({ copy }: { copy: Result<TaskInputCopy> }) {
  if (copy.status === 'loading') {
    return (
      <p className="att-none">
        <Mark kind="pending" say="Reading the masked copy of this task's input. The read is in flight." /> reading
      </p>
    )
  }
  if (copy.status === 'error' && copy.error.kind === 'not_found' && copy.error.code === null) {
    return (
      <p className="att-none">
        <Mark
          kind="absent"
          say="The deployment answering this UI does not serve the masked copy of a task's input yet. The input is not drawn unmasked in its place."
        />{' '}
        not served by this API
      </p>
    )
  }
  const message = copy.status === 'error' ? copy.error.message : null
  return (
    <p className="att-none">
      <Mark
        kind="unread"
        say="The masked copy of this task's input could not be read, so it is not shown — and it is not drawn unmasked instead. Nothing may be concluded about the input: it is not empty and it is not missing."
      />{' '}
      input not read
      {/* A SEPARATOR BEFORE THE SERVER'S WORDS. `.att-none` is a flex row, so
          the gap parted them on screen, but its text ran them together --
          `input not readBusy.` -- which is what a screen reader and a copy
          read (PR #210 review). */}
      {message !== null && <span className="ctl-sub">{` · ${message}`}</span>}
    </p>
  )
}
