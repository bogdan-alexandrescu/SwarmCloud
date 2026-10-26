/**
 * THE LIVE-BROWSER HAND-OFF, AS THE CONSOLE DRAWS IT (docs/design/live-browser.md
 * §4 and §6, "The hand-off"; lane LB-L12, part of #1030).
 *
 * An agent that needs a person -- to sign in, approve a prompt, solve a
 * challenge; the reason is the agent's own -- asks through
 * `swarm-handoff`, and swarm-api sets the task's `human_wait` (§4.1). The task
 * STAYS RUNNING and holds its pod for `human_hold_seconds` (10 minutes, Q3);
 * if nobody takes control in that time the worker parks it
 * PARKED(HUMAN_REQUIRED), giving the capacity back, and only a person's
 * "I'm ready" (`POST /v1/tasks/{id}/human-ready`) returns it to
 * READY. Nothing else promotes it, and past 24 hours (Q4) the scheduler
 * dead-letters it.
 *
 * So there are two phases to draw, and this file is the one place that tells
 * them apart (`handoffOf`) -- and a third, after "I'm ready", that no longer
 * needs anyone but is not live yet either (`resumingAfterHandoff`). The Browser tab, the Overview's "Needs a look" and
 * the agent list all read it, so the three cannot disagree about whether a
 * task needs you.
 *
 * WHAT IS NOT HERE: Take control, Return control and Open in theater are the
 * Browser tab's and the theater's (lanes L11, L11b), and reach the banner
 * through its `children`. The tab, not this file, owns the session and its
 * ticket.
 */
import { useEffect, useState, type ReactNode } from 'react'
import { USE_FIXTURES, loadLiveSession, loadTaskEventsPage } from './api'
import { Banner, Button } from './components'
import { route, write, type ApiError, type Result } from './fetch'
import { type Task } from './types'
import { useNow } from './useNow'

/**
 * The runner profiles whose tasks carry a live browser.
 *
 * `RunnerProfile.live_browser` (contract request 71, LB-D) is the catalogue's
 * answer, and no route serves it yet: `/v1/runtimes` and the profile list
 * predate it. Until one does, this names the one profile the catalogue marks
 * (`claude-code-browser`, swarm_common/profiles.py). A task row that carries
 * `human_wait` is a browser task whatever its profile is called, so a renamed
 * profile still raises its banner; only the Live browsers filter would miss
 * its idle rows.
 */
export const LIVE_BROWSER_PROFILES: ReadonlySet<string> = new Set(['claude-code-browser'])

/**
 * How long a PARKED(HUMAN_REQUIRED) task waits before the scheduler
 * dead-letters it: `HUMAN_PARK_MAX_SECONDS` in apps/scheduler/scheduler/loop.py,
 * the owner's Q4 answer (24 hours). The banner's "time left" on a park is
 * counted to it from the park instant, which the worker writes as
 * `next_eligible_at` (`updated_at` standing in, as the sweep reads it).
 */
export const HUMAN_PARK_MAX_MS = 24 * 3_600_000

/** The task is a live-browser task. */
export function isLiveBrowser(task: Pick<Task, 'runner_profile' | 'human_wait'>): boolean {
  return LIVE_BROWSER_PROFILES.has(task.runner_profile) || isWait(task.human_wait)
}

function isWait(v: unknown): v is NonNullable<Task['human_wait']> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v : null)

function at(iso: string | null | undefined): number | null {
  if (typeof iso !== 'string') return null
  const v = Date.parse(iso)
  return Number.isFinite(v) ? v : null
}

/** A hand-off, in the phase the task is in. */
export type Handoff =
  | {
      /** RUNNING, pod held, waiting for a person: §4.3 phase H. */
      phase: 'hold'
      reason: string | null
      expect: string | null
      /** When the hold ends and the worker parks, epoch ms; null when not served. */
      deadline: number | null
      /** Who holds control now, or null. */
      controller: string | null
    }
  | {
      /** PARKED(HUMAN_REQUIRED), no capacity: §4.3 phase P. */
      phase: 'parked'
      /** When the scheduler dead-letters it, epoch ms; null when no instant was served. */
      deadline: number | null
    }

/** One question a merge's owner-question hold names: whose, and which. */
export interface HeldQuestion {
  task_id: string
  step_id: string | null
  question_index: number
  question: string | null
}

/**
 * A MERGE STEP HELD BY THE OWNER'S QUESTIONS IS NOT A HAND-OFF (#2313). The
 * merge (runner profile `merge`: git only, no browser) parks
 * PARKED(HUMAN_REQUIRED) while questions its review named are unanswered,
 * and records them in its park's `blocked_by` entry as `park_phase:
 * "owner_questions"` with `owner_questions: {questions, unreadable}`
 * (agent_worker.lifecycle._park_for_owner_questions). Nobody signs in to
 * anything: the answer that clears the last named question returns it to
 * READY by itself (QUESTION-WAKE), and I'm ready only re-ran it into the
 * same park (task_ad66d2b402a847ca8b7d, 2026-10-25 07:32:52Z). So this is
 * read here, where the hand-off is told apart, and `handoffOf` is null for
 * it: no banner, no I'm ready, no Browser tab, no "needs you" browser row.
 *
 * The questions, else null. An entry that is not a task id and a
 * non-negative integer index is counted in `unreadable`, never guessed at.
 */
export function ownerQuestionsHold(
  task: Pick<Task, 'state' | 'park_reason' | 'blocked_by'>,
): { questions: HeldQuestion[]; unreadable: number } | null {
  if (task.state !== 'PARKED' || task.park_reason !== 'HUMAN_REQUIRED') return null
  for (const entry of task.blocked_by ?? []) {
    if (!isRecord(entry) || entry.reason !== 'HUMAN_REQUIRED' || entry.park_phase !== 'owner_questions') continue
    const hold = isRecord(entry.owner_questions) ? entry.owner_questions : {}
    const recorded = hold.unreadable
    let unreadable = typeof recorded === 'number' && Number.isInteger(recorded) && recorded >= 0 ? recorded : 1
    const questions: HeldQuestion[] = []
    for (const q of Array.isArray(hold.questions) ? hold.questions : []) {
      const r = isRecord(q) ? q : {}
      const index = r.question_index
      const id = text(r.task_id)
      if (id === null || typeof index !== 'number' || !Number.isInteger(index) || index < 0) {
        unreadable += 1
        continue
      }
      questions.push({ task_id: id, step_id: text(r.step_id), question_index: index, question: text(r.question) })
    }
    return { questions, unreadable }
  }
  return null
}

/**
 * Whether, and how, this task is waiting for a person. Null for every task
 * that is not, and for a merge held by the owner's questions, which waits for
 * answers rather than for a person at a browser (`ownerQuestionsHold`). The
 * PARKED case carries no reason: the park clears `human_wait`, so the reason
 * is read from the request's event (`useHandoffRequest`).
 */
export function handoffOf(task: Task): Handoff | null {
  if (task.state === 'RUNNING' && isWait(task.human_wait)) {
    const w = task.human_wait
    return {
      phase: 'hold',
      reason: text(w.reason),
      expect: text(w.expect),
      deadline: at(w.deadline),
      controller: isWait(w.controller) ? text((w.controller as { email?: unknown }).email) : null,
    }
  }
  if (task.state === 'PARKED' && task.park_reason === 'HUMAN_REQUIRED' && ownerQuestionsHold(task) === null) {
    const parkedAt = at(task.next_eligible_at) ?? at(task.updated_at)
    return { phase: 'parked', deadline: parkedAt === null ? null : parkedAt + HUMAN_PARK_MAX_MS }
  }
  return null
}

/**
 * THE STATES A RESUMED HAND-OFF PASSES THROUGH ON ITS WAY BACK (QA12-06): READY
 * after `human-ready`, then admission's LEASED, DISPATCHED and STARTING. About
 * 80 s of cold start on the live-browser QA pass p12, during which the
 * banner was gone and the tab said only "not running".
 */
const RESUMING_STATES: ReadonlySet<Task['state']> = new Set(['READY', 'LEASED', 'DISPATCHED', 'STARTING'])

/**
 * A PERSON SAID "I'M READY" AND THE BROWSER IS NOT BACK YET (QA12-06). swarm-api
 * writes `metadata.human_ready` (`{by, at}`) when it accepts `human-ready`, and
 * the agent's next hand-off request consumes it (swarm_api/handoff.py), so
 * its presence on a task on its way to RUNNING is the resumed hand-off. Not
 * `needsYou`: nobody has to do anything, so Needs a look and Waiting drop it.
 *
 * What it cannot tell: an agent that came back and never asked again leaves
 * the key behind, so a later, unrelated trip back through READY (a quota
 * park, say) draws this banner too. The worker's own `park_phase` is on the
 * park event, not the task row, and an event read per row is not worth it.
 */
export function resumingAfterHandoff(task: Pick<Task, 'state' | 'metadata'>): boolean {
  return RESUMING_STATES.has(task.state) && isWait(task.metadata?.human_ready)
}

/** The task is waiting for a person, held or parked. */
export function needsYou(task: Task): boolean {
  return handoffOf(task) !== null
}

/**
 * The time left before `deadline`, as the banner prints it: `4:12 left` under
 * an hour, `23:00:00 left` beyond. ONE FORMAT, m:ss (QA12-21): the banner
 * printed `4m 12s` beside the theater checklist's `4:12` for the same hold,
 * and the Overview card is this banner. Null once the deadline has passed --
 * never a negative figure -- and when there is no deadline to count to.
 */
export function timeLeft(deadline: number | null, now: number): string | null {
  if (deadline === null) return null
  const ms = deadline - now
  if (ms <= 0) return null
  const s = Math.floor(ms / 1000)
  const ss = String(s % 60).padStart(2, '0')
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}:${ss} left`
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${ss} left`
}

/**
 * THE LIVE BROWSERS FILTER'S ROWS (§6, "All live browsers"): RUNNING browser
 * agents only, the ones waiting for a person first, each group in the order
 * it came in. Not LEASED, DISPATCHED or STARTING -- those hold capacity
 * (invariant 1) but have no browser to show yet -- and not a parked
 * hand-off, which has none at all; that one is in Waiting, marked.
 */
export function liveBrowserRows(tasks: readonly Task[]): Task[] {
  const live = tasks.filter((t) => t.state === 'RUNNING' && isLiveBrowser(t))
  return [...live.filter((t) => handoffOf(t)?.phase === 'hold'), ...live.filter((t) => handoffOf(t)?.phase !== 'hold')]
}

/**
 * `POST /v1/tasks/{id}/human-ready`: "I'm ready" (§4.3). No body:
 * the caller names the task and nothing else, and swarm-api decides whether
 * this person may say it (the submitter, §5.1 and Q1). The fixture build
 * serves no hand-off, and says so rather than pretending.
 */
export async function humanReady(taskId: string): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/human-ready', { id: taskId })
  if (USE_FIXTURES) {
    return {
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build serves no hand-off routes.' },
    }
  }
  return write(target, 'POST')
}

/** The reason and host of a parked hand-off, read from its request event. */
interface Request {
  reason: string | null
  expect: string | null
}

/**
 * A PARKED HAND-OFF'S REASON. The park clears `human_wait`, so the reason the
 * agent gave is read back from the newest `human_handoff_requested` event
 * (§4.1 step 3), whose detail carries the same redacted reason and host.
 * One read, newest first, only for a parked hand-off. Null until it lands,
 * and null when it does not: the banner then says the reason was not read.
 */
export function useHandoffRequest(task: Task, enabled: boolean): Request | null | 'unread' {
  const [got, setGot] = useState<Request | null | 'unread'>(null)
  useEffect(() => {
    if (!enabled) return
    let live = true
    void loadTaskEventsPage(task.id, { order: 'desc' }).then(
      (r) => {
        if (!live) return
        if (r.status !== 'ok' && r.status !== 'stale') {
          setGot(r.status === 'loading' ? null : 'unread')
          return
        }
        const ev = r.data.events.find((e) => e.type === 'human_handoff_requested')
        setGot(ev === undefined ? 'unread' : { reason: text(ev.detail?.reason), expect: text(ev.detail?.expect) })
      },
      () => live && setGot('unread'),
    )
    return () => {
      live = false
    }
  }, [task.id, enabled])
  return enabled ? got : null
}

type Ready = { kind: 'idle' } | { kind: 'busy' } | { kind: 'done' } | { kind: 'refused'; error: ApiError }

/**
 * WHO "YOU" ARE, for "You are in control" (QA12-05). The console has no
 * identity read of its own; the live session's `you.email` is the one place
 * the server names the viewer. Read once per task, and only while somebody
 * holds control -- the only time the banner has a sentence that depends on
 * it. Null until it lands, and when it does not: the banner then names the
 * controller by address, as it did before.
 */
function useYou(taskId: string, enabled: boolean): string | null {
  const [you, setYou] = useState<string | null>(null)
  useEffect(() => {
    if (!enabled) return
    let live = true
    void loadLiveSession(taskId).then(
      (r) => {
        if (!live || (r.status !== 'ok' && r.status !== 'stale')) return
        setYou(text(r.data.session?.you?.email))
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [taskId, enabled])
  return enabled ? you : null
}

/**
 * THE HAND-OFF BANNER: the agent's reason as the headline, the expected site
 * and the time left, and on a park "I'm ready". The Browser tab draws it
 * across the tab; the Overview draws one per waiting task (`HandoffCards`).
 * Yellow, as a warning is: the agent is stopped until a person comes.
 *
 * NEUTRAL WORDING (owner decision, 2026-10-13, QA12-04): a hand-off is not
 * always a sign-in, so nothing here says "sign in". The headline is "The agent
 * needs your help" with the agent's own reason; the button is "I'm ready".
 * The live view shows the page as Chromium draws it (BGD, 2026-10-25;
 * LIVE-NOMASK-COPY, part of #2298), so nothing here says a field is masked.
 * The theater's checklist (TheaterTimeline.tsx) is its own lane's.
 *
 * After "I'm ready" the task is READY and `handoffOf` is null; while it is on
 * its way back to RUNNING the banner says so instead of vanishing
 * (`resumingAfterHandoff`, QA12-06).
 *
 * `now` is the caller's clock, so a list of banners ticks on one instant.
 * `onReady` is called after swarm-api accepted `human-ready`, with the task
 * id; the Overview opens the agent with it, the tab has nowhere to go.
 * `you` is the viewer's address when the caller has read it (the tab's
 * session); left out, the banner reads it itself while someone holds control.
 * `children` are the caller's own actions (Take control, Open, Open in
 * theater), drawn after the banner's own.
 */
export function HandoffBanner({
  task,
  now,
  onReady,
  you,
  children,
}: {
  task: Task
  now: number
  onReady?: (taskId: string) => void
  you?: string | null
  children?: ReactNode
}) {
  const h = handoffOf(task)
  const request = useHandoffRequest(task, h?.phase === 'parked')
  const [ready, setReady] = useState<Ready>({ kind: 'idle' })
  const controller = h?.phase === 'hold' ? h.controller : null
  const read = useYou(task.id, you === undefined && controller !== null)
  if (h === null) return resumingAfterHandoff(task) ? <ResumingBanner>{children}</ResumingBanner> : null

  const viewer = you === undefined ? read : you
  const left = timeLeft(h.deadline, now)
  const reason = h.phase === 'hold' ? h.reason : request !== null && request !== 'unread' ? request.reason : null
  const expect = h.phase === 'hold' ? h.expect : request !== null && request !== 'unread' ? request.expect : null

  const press = async () => {
    setReady({ kind: 'busy' })
    const r = await humanReady(task.id)
    if (r.status === 'error') {
      setReady({ kind: 'refused', error: r.error })
      return
    }
    setReady({ kind: 'done' })
    onReady?.(task.id)
  }

  const reasonLine =
    reason !== null
      ? reason
      : h.phase === 'parked' && request === null
        ? 'Reading why it asked…'
        : 'The agent gave no reason that could be read.'

  // WHAT THE TIME LEFT IS TIME UNTIL, said with the figure: a hold ends in a
  // park and the capacity goes back; a park ends in a dead letter.
  const clock =
    h.phase === 'hold'
      ? left !== null
        ? `${left} before the browser is parked`
        : 'The hold is over: the agent is parking, and its browser closes. Once it has parked, say you are ready and it starts again.'
      : ready.kind === 'done'
        ? null
        : left !== null
          ? `${left} before it is given up (dead-lettered)`
          : 'Past its 24 hours; the scheduler gives it up on its next pass.'

  // QA12-05: the person in control is "you", not their own address.
  const control =
    controller === null
      ? null
      : viewer !== null && viewer.toLowerCase() === controller.toLowerCase()
        ? 'You are in control · return control when done'
        : `${controller} is in control`

  return (
    <Banner
      tone="warn"
      role="status"
      title={`The agent needs your help: ${reasonLine}`}
      actions={
        h.phase === 'parked' && ready.kind !== 'done' ? (
          <>
            <Button kind="primary" size="sm" busy={ready.kind === 'busy' ? 'Asking…' : false} onClick={() => void press()}>
              I'm ready
            </Button>
            {children}
          </>
        ) : (
          children
        )
      }
    >
      <span className="ho-facts">
        {h.phase === 'parked' && ready.kind !== 'done' && <span className="ho-parked">Parked until you say you are ready · </span>}
        <span className="ho-expect">
          {expect !== null ? (
            <>
              expects <b className="id">{expect}</b>
            </>
          ) : (
            'no expected site given'
          )}
        </span>
        {control !== null && <span className="ho-ctl"> · {control}</span>}
        {clock !== null && <span className="ho-left"> · {clock}</span>}
      </span>
      {ready.kind === 'done' && (
        <span className="ho-starting">
          Starting your browser. It is admitted like any agent and starts cold, so allow a minute or two; the agent
          asks again when it is up.
        </span>
      )}
      {ready.kind === 'refused' && (
        <span className="ho-refused" role="alert">
          Not resumed: {ready.error.message}
        </span>
      )}
    </Banner>
  )
}

/**
 * THE BANNER WHILE A RESUMED HAND-OFF COMES BACK (QA12-06). Nothing to do and
 * nothing to count down: admission and a cold start, about a minute and a
 * half on the QA pass. Blue, not yellow -- nobody is being waited for.
 */
function ResumingBanner({ children }: { children?: ReactNode }) {
  return (
    <Banner tone="info" role="status" title="Starting your browser again — the agent will ask for you when it is back" actions={children}>
      <span className="ho-starting">
        You said you are ready. It is admitted like any agent and starts cold, so allow a minute or two.
      </span>
    </Banner>
  )
}

/**
 * The banner as the agent's view mounts it: on the shared 1s clock, and
 * nothing at all for a task nobody is waited for, so the clock is held only
 * while there is a figure to tick. `onReady` re-reads the agent, which is
 * READY after `human-ready` and is admitted from there; the strip then stays
 * up, saying the browser is starting again, until it is RUNNING. `you` is
 * passed through to the banner (a tab that has read its session already
 * knows it, and saves the banner its own read).
 */
export function HandoffStrip({
  task,
  onReady,
  you,
  children,
}: {
  task: Task
  onReady?: () => void
  you?: string | null
  children?: ReactNode
}) {
  if (handoffOf(task) === null && !resumingAfterHandoff(task)) return null
  return (
    <TickingBanner task={task} onReady={onReady} you={you}>
      {children}
    </TickingBanner>
  )
}

function TickingBanner({
  task,
  onReady,
  you,
  children,
}: {
  task: Task
  onReady: (() => void) | undefined
  you: string | null | undefined
  children?: ReactNode
}) {
  const now = useNow(1000)
  return (
    <div className="ho-strip">
      <HandoffBanner task={task} now={now} you={you} onReady={onReady === undefined ? undefined : () => onReady()}>
        {children}
      </HandoffBanner>
    </div>
  )
}
