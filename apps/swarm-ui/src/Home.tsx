/**
 * HOME: THE SIGNED-IN PERSON'S OWN PAGE, AT THE CONSOLE'S ROOT (the owner's
 * pick of the 'desk' mock-up, 2026-11-02; part of #2688). It answers "what
 * waits on me, and what of mine is moving" -- the question Overview answered
 * only by mixing a personal setup card and a personal approvals card into the
 * deployment's page. Overview keeps its own route, reads and content; this
 * page adds none of either: every figure here is a read the console already
 * makes, and every action calls what the console already calls for it.
 *
 *   Needs you          the owner's questions (answered in place), the
 *                      approvals inbox, the workspace and Claude account
 *                      requests a platform admin may decide, agents waiting
 *                      for this person at a browser, and their own runs that
 *                      failed today
 *   Your work          two lists, their agents and their workflows in
 *                      progress (HomeWorkLists.tsx); a waiting agent says why
 *                      in its dot's tooltip (waiting costs nothing: invariant 1)
 *   Recently finished  their agents that ended
 *   Start, Your workspaces, This workspace   the side column
 *
 * A COUNT THE CONSOLE CANNOT KNOW READS "not known", NEVER 0. Every tile and
 * every chip draws a dash with its reason until its read has answered, and
 * the "need you" total is a dash while any of its parts is.
 *
 * WHAT IS THE PERSON'S OWN. Their tasks are `GET /v1/tasks?submitted_by=me`,
 * filtered by the API (live QA SUB-6), so the page is full of theirs rather
 * than the workspace's newest filtered down. Requests are drawn only from the
 * platform admin's read of `GET /v1/admin/people`: a workspace admin's scoped
 * read and a member's refusal draw no Requests at all, since neither may
 * decide one.
 */
import { useCallback, useEffect, useId, useState, type MouseEvent, type ReactNode } from 'react'
import {
  answerQuestion,
  approveApproval,
  approveWorkspace,
  declineLoanRequest,
  denyWorkspace,
  loadApproval,
  loadApprovals,
  loadLiveSession,
  loadOnboarding,
  loadPeople,
  loadProfile,
  loadStats,
  loadTasks,
  rejectApproval,
  setWorkspaceLoan,
  type ApprovalItem,
  type LiveSession,
} from './api'
import { approvalAddress } from './Approvals'
import { Button, ButtonLink, Card, CardLink, StatTile, type TileValue } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { guidePath, openGuide } from './Guide'
import { handoffOf, humanReady, needsYou, ownerQuestionsHold, type HeldQuestion } from './Handoff'
import { LiveControl } from './LiveBrowser'
import { WorkLists } from './HomeWorkLists'
import { browserTabPath } from './LiveThumb'
import { doneCount, SetupCard, stepRequired } from './Onboarding'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import { lentToRow, waitingRequests, type WaitingRequest } from './People'
import type { LendableAccount, PeopleDoc, Profile, ScopedPeopleDoc, Stats, Task } from './types'
import './styles/home.css'

/** How many of the person's newest tasks Home reads: one page, the size the Agents list asks for. */
export const HOME_TASK_LIMIT = 100

/** How often Home reads again while it is open; each action reads again at once. */
export const HOME_POLL_MS = 30_000

/** The jump links of Home's own panel, in the order the page draws them (Spine.tsx). */
export const HOME_JUMPS: readonly { id: string; label: string }[] = [
  { id: 'home-needs', label: 'Needs you' },
  { id: 'home-work', label: 'Your work' },
  { id: 'home-finished', label: 'Recently finished' },
  { id: 'home-start', label: 'Start' },
]

const LIVE: ReadonlySet<string> = new Set(['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'])
const WAITING: ReadonlySet<string> = new Set(['QUEUED', 'READY', 'PARKED'])
const FAILED: ReadonlySet<string> = new Set(['FAILED', 'DEAD_LETTERED'])
const FINISHED: ReadonlySet<string> = new Set(['SUCCEEDED', 'CANCELLED'])

const STATE_SAY: Readonly<Record<string, string>> = {
  RUNNING: 'running',
  STARTING: 'starting',
  DISPATCHED: 'dispatched',
  LEASED: 'leased',
  QUEUED: 'queued',
  READY: 'waiting for room',
  PARKED: 'parked',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  DEAD_LETTERED: 'dead-lettered',
  CANCELLED: 'cancelled',
}

/**
 * Why one of the person's tasks waits, in the words the chips use. An
 * await-input step (AWAITING_INPUT, human-steps.md §3.4) is not a hand-off:
 * `needsYou` is false for it, so it is not an "At a browser" row and has no
 * I'm ready. It waits in Your work, saying it waits on the person's input,
 * which its own page takes.
 */
const PARK_SAY: Readonly<Record<string, string>> = {
  HUMAN_REQUIRED: 'a person',
  AWAITING_INPUT: 'your input',
  PROVIDER_QUOTA_EXHAUSTED: 'provider quota to return',
  CI_PENDING: 'CI to finish',
}

export function waitWhy(t: Pick<Task, 'state' | 'park_reason'>): string {
  if (t.state === 'QUEUED') return 'admission'
  if (t.state === 'READY') return 'room in a pool'
  return PARK_SAY[t.park_reason ?? ''] ?? 'a parked reason'
}

function dataOf<T>(r: Result<T> | null): T | null {
  return r !== null && (r.status === 'ok' || r.status === 'stale') ? r.data : null
}

/** A list read, or null while it is not known: `empty` is a measured nothing. */
function listOf<T, D>(r: Result<D> | null, pick: (d: D) => T[] | undefined): T[] | null {
  if (r === null) return null
  if (r.status === 'empty') return []
  const d = dataOf(r)
  if (d === null) return null
  const xs = pick(d)
  return Array.isArray(xs) ? xs : null
}

const tile = (n: number | null, why: string, note?: ReactNode): TileValue =>
  n === null ? { kind: 'unknown', why } : { kind: 'value', value: n, ...(note === undefined ? {} : { note }) }

/** One read, again on every `round`. Null until its first answer lands. */
function useLoad<T>(load: () => Promise<Result<T>>, round: number, enabled = true): Result<T> | null {
  const [got, setGot] = useState<Result<T> | null>(null)
  useEffect(() => {
    if (!enabled) return
    let live = true
    void load().then((r) => {
      if (live) setGot(r)
    })
    return () => {
      live = false
    }
  }, [load, round, enabled])
  return got
}

function age(iso: string | null | undefined, now: number): string {
  if (iso === null || iso === undefined) return '—'
  const at = Date.parse(iso)
  if (!Number.isFinite(at)) return '—'
  const s = Math.max(0, (now - at) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min`
  if (s < 86400) return `${Math.round(s / 3600)} h`
  return `${Math.round(s / 86400)} d`
}

function isToday(iso: string | null | undefined, now: number): boolean {
  if (iso === null || iso === undefined) return false
  const at = Date.parse(iso)
  const midnight = new Date(now)
  midnight.setHours(0, 0, 0, 0)
  return Number.isFinite(at) && at >= midnight.getTime()
}

/** What the work is: the workflow's title on its metadata, else the step, else what the agent is. */
export function titleOf(t: Pick<Task, 'id' | 'metadata' | 'step_id' | 'runner_profile'>): string {
  const title = t.metadata?.title
  if (typeof title === 'string' && title.trim() !== '') return title
  return t.step_id ?? t.runner_profile
}

/** A state mark: a dot in the state's tone and the plain word. */
function StateDot({ state, say }: { state: string; say?: string }) {
  const tone = LIVE.has(state) ? 'live' : WAITING.has(state) ? 'park' : state === 'SUCCEEDED' ? 'ok' : state === 'CANCELLED' ? 'neu' : 'bad'
  return (
    <span className={`hm-state is-${tone}`}>
      <i aria-hidden />
      {say ?? STATE_SAY[state] ?? state.toLowerCase()}
    </span>
  )
}

/** A task's short id, so two agents that share a title read apart. */
function Tid({ id }: { id: string }) {
  return (
    <span className="hm-tid" title={`agent id ${id}: tells apart agents that share a title`}>
      agent {id.replace(/^task_/, '').slice(0, 8)}
    </span>
  )
}

/** A card's head: its title, its count (a dash while not known) and an optional note behind a `?`. */
function Head({ title, count, why, action }: { title: string; count?: number | null; why?: string; action?: ReactNode }) {
  return (
    <div className="hm-head">
      <h2>
        {title}
        {count !== undefined && (
          <em className="hm-count" aria-label={count === null ? `${title}: not known` : `${title}: ${count}`}>
            {count === null ? '—' : count}
          </em>
        )}
      </h2>
      {why !== undefined && (
        <details className="hm-why">
          <summary aria-label={`About ${title}`}>?</summary>
          <p>{why}</p>
        </details>
      )}
      {action}
    </div>
  )
}

/** An action's outcome, said under its row. */
function Refused({ error, what }: { error: ApiError; what: string }) {
  return (
    <p className="hm-refused" role="alert">
      {what}: {errorHeading(error)}
    </p>
  )
}

function jump(id: string): { href: string; onClick: (e: { preventDefault: () => void }) => void } {
  return {
    href: `#${id}`,
    onClick: (e) => {
      e.preventDefault()
      document.getElementById(id)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
    },
  }
}

// ---------------------------------------------------------------- reads

const readTasks = () => loadTasks(HOME_TASK_LIMIT, { submittedBy: 'me' })
const readApprovals = () => loadApprovals()
const readPeople = () => loadPeople()
const readProfile = () => loadProfile()
const readStats = () => loadStats()
const readOnboarding = () => loadOnboarding()

/** One held owner question, keyed by the task that asked it and its number there. */
interface OwnerQuestion extends HeldQuestion {
  key: string
  /** The merge it holds, whose page names it. */
  holder: Task
}

export function ownerQuestionsOf(tasks: readonly Task[]): OwnerQuestion[] {
  const out: OwnerQuestion[] = []
  for (const t of tasks) {
    const hold = ownerQuestionsHold(t)
    if (hold === null) continue
    for (const q of hold.questions) out.push({ ...q, key: `${q.task_id}:${q.question_index}`, holder: t })
  }
  return out
}

/** Whose role line the head says: a platform role first, else the role in the acting workspace. */
function roleLine(p: Profile | null): string | null {
  if (p === null) return null
  const platform = p.identity.platform_role
  if (platform === 'owner') return 'platform owner'
  if (platform === 'admin' || p.identity.is_admin) return 'platform admin'
  const acting = p.workspaces.find((w) => w.is_acting)
  return acting === undefined ? 'no workspace yet' : `${acting.role.name} of ${acting.kind === 'user' ? 'your own workspace' : acting.display_name}`
}

// ---------------------------------------------------------------- the page

export function HomeScreen({ now: fixedNow }: { now?: number } = {}) {
  const [round, setRound] = useState(0)
  const reload = useCallback(() => setRound((n) => n + 1), [])
  useEffect(() => {
    const t = setInterval(reload, HOME_POLL_MS)
    return () => clearInterval(t)
  }, [reload])
  const now = fixedNow ?? Date.now()

  const tasksR = useLoad(readTasks, round)
  const approvalsR = useLoad(readApprovals, round)
  const peopleR = useLoad(readPeople, round)
  const profileR = useLoad(readProfile, round)

  // WHAT WAS DECIDED HERE LEAVES AT ONCE: the next read would drop it too, a
  // poll later; until then the item is gone and its count has dropped.
  const [gone, setGone] = useState<ReadonlySet<string>>(() => new Set())
  const [said, setSaid] = useState<string | null>(null)
  const done = useCallback(
    (key: string, sentence: string) => {
      setGone((g) => new Set([...g, key]))
      setSaid(sentence)
      reload()
    },
    [reload],
  )

  const mine = listOf(tasksR, (d) => d.tasks)
  const profile = dataOf(profileR)
  const approvals = listOf(approvalsR, (d) => d.approvals)
  const peopleDoc = dataOf(peopleR) as PeopleDoc | ScopedPeopleDoc | null
  // A refusal (a member, 403) or a workspace admin's scoped read: nothing here
  // is theirs to decide, so Requests is not drawn. Any other failure is a
  // platform admin's read that did not answer: not known, never 0.
  const peopleRefused = peopleR !== null && peopleR.status === 'error' && (peopleR.error.httpStatus === 403 || peopleR.error.httpStatus === 401)
  const decides = peopleR !== null && !peopleRefused && peopleDoc?.scope !== 'workspaces'
  const platformDoc = peopleDoc !== null && peopleDoc.scope !== 'workspaces' ? (peopleDoc as PeopleDoc) : null
  const requests: WaitingRequest[] | null = !decides
    ? []
    : peopleR?.status === 'empty'
      ? []
      : platformDoc === null
        ? null
        : waitingRequests(platformDoc.people)

  const questions = mine === null ? null : ownerQuestionsOf(mine).filter((q) => !gone.has(`q:${q.key}`))
  const openApprovals = approvals === null ? null : approvals.filter((a) => !gone.has(`a:${a.approval_id}`))
  const openRequests = requests === null ? null : requests.filter((r) => !gone.has(`r:${r.kind}:${r.row.email}`))
  const handoffs = mine === null ? null : mine.filter(needsYou).filter((t) => !gone.has(`h:${t.id}`))
  const failed = mine === null ? null : mine.filter((t) => FAILED.has(t.state) && isToday(t.updated_at, now))
  const kinds = [questions, openApprovals, ...(decides ? [openRequests] : []), handoffs, failed]
  const total = kinds.reduce<number | null>((n, xs) => (n === null || xs === null ? null : n + xs.length), 0)

  const running = mine === null ? null : mine.filter((t) => LIVE.has(t.state))
  const waiting = mine === null ? null : mine.filter((t) => WAITING.has(t.state))
  const name = profile?.identity.display_name ?? profile?.identity.email ?? null
  const role = roleLine(profile)

  return (
    <div className="hm-page">
      <header className="hm-pagehead">
        <div>
          <h1>Home</h1>
          <p className="hm-sub">{name === null ? 'reading who you are…' : [name, role].filter(Boolean).join(' · ')}</p>
        </div>
        <div className="hm-headchips" aria-label="What needs you">
          <span className={`hm-hc${total !== null && total > 0 ? ' is-warn' : ''}`} data-testid="home-total">
            <b>{total ?? '—'}</b> {total === 1 ? 'needs' : 'need'} you
          </span>
          <a className="hm-hc" {...jump('home-work')}>
            <b>{running?.length ?? '—'}</b> of yours running
          </a>
          <a className="hm-hc" {...jump('home-work')}>
            <b>{waiting?.length ?? '—'}</b> of yours waiting
          </a>
        </div>
      </header>
      <SetupCard />
      <div className="hm-grid">
        <div className="hm-col">
          <Card id="home-needs" className={`hm-needs${total === 0 ? ' is-clear' : ''}`}>
            <Head
              title="Needs you"
              count={total}
              why="Questions your agents asked you, approvals in your inbox, the requests you may decide, your agents waiting for you at a browser, and your own runs that failed today, of your newest tasks read. Waiting items hold no capacity."
            />
            <div className="hm-tiles">
              <StatTile label="Questions" tile={tile(questions?.length ?? null, 'your tasks not read')} />
              <StatTile label="Approvals" tile={tile(openApprovals?.length ?? null, 'the approvals inbox not read')} />
              {decides && <StatTile label="Requests" tile={tile(openRequests?.length ?? null, 'the people read did not answer')} />}
              <StatTile label="At a browser" tile={tile(handoffs?.length ?? null, 'your tasks not read')} />
              <StatTile
                label="Your failures today"
                tile={tile(failed?.length ?? null, 'your tasks not read', mine === null ? undefined : `of your ${mine.length} newest read`)}
              />
            </div>
            {total === 0 && (
              <p className="hm-clear" data-testid="home-clear">
                <span className="hm-okmark" aria-hidden>
                  ✓
                </span>{' '}
                Nothing waits on you. Checked your {mine?.length ?? 0} newest tasks and the approvals inbox.
              </p>
            )}
            <ul className="hm-items">
              {(questions ?? []).map((q) => (
                <QuestionItem key={q.key} q={q} now={now} onDone={done} />
              ))}
              {(openApprovals ?? []).map((a) => (
                <ApprovalRow key={a.approval_id} a={a} now={now} onDone={done} />
              ))}
              {(openRequests ?? []).map((r) => (
                <RequestRow key={`${r.kind}:${r.row.email}`} r={r} now={now} accounts={platformDoc?.lendable_accounts ?? null} lendWhy={platformDoc?.lendable_error ?? null} onDone={done} />
              ))}
              {(handoffs ?? []).map((t) => (
                <HandoffRow key={t.id} t={t} now={now} onDone={done} />
              ))}
              {(failed ?? []).map((t) => (
                <li key={t.id} className="hm-item is-failed" data-kind="failed">
                  <span className="hm-kind">Failed</span>
                  <div className="hm-item-main">
                    <a href={agentPath(t)} className="hm-item-title">
                      {titleOf(t)}
                    </a>{' '}
                    <Tid id={t.id} />
                    <p className="hm-meta">{t.last_error ?? 'it reported no error'}</p>
                  </div>
                  <div className="hm-acts">
                    <ButtonLink kind="ghost" size="sm" href={agentPath(t)}>
                      Open
                    </ButtonLink>
                  </div>
                  <span className="hm-age">{age(t.updated_at, now)}</span>
                </li>
              ))}
            </ul>
            {said !== null && (
              <p className="hm-done" role="status">
                <span className="hm-okmark" aria-hidden>
                  ✓
                </span>{' '}
                {said}
              </p>
            )}
          </Card>
          <WorkLists
            mine={mine}
            questions={questions}
            approvals={openApprovals}
            now={now}
            round={round}
            why={waitWhy}
            onDone={done}
            head={(count, action) => <Head title="Your work" count={count} why="Your agents and workflows in progress. Waiting work holds no capacity." action={action} />}
          />
          <RecentlyFinished mine={mine} now={now} />
        </div>
        <aside className="hm-col hm-rail">
          <StartCard profile={profile} />
          <YourWorkspaces profile={profile} round={round} />
          <WorkspaceGlance round={round} />
        </aside>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- Needs you

type OnDone = (key: string, sentence: string) => void

/**
 * AN OWNER QUESTION, ANSWERED IN PLACE. The answer is posted to the ASKING
 * task (`POST /v1/tasks/{id}/answers`), where the route stores it and wakes
 * the merge it holds once the last named question is answered (QUESTION-WAKE),
 * exactly as the merge's own Questions card does (QuestionsCard.tsx). Who may
 * answer is the API's to say: a refusal is drawn under the box.
 */
function QuestionItem({ q, now, onDone }: { q: OwnerQuestion; now: number; onDone: OnDone }) {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const box = useId()
  const send = async () => {
    const text = draft.trim()
    if (busy || text === '') return
    setBusy(true)
    setRefused(null)
    const r = await answerQuestion(q.task_id, { question_index: q.question_index, choice: null, text })
    setBusy(false)
    if (r.status === 'error') return setRefused(r.error)
    onDone(`q:${q.key}`, `Answer sent to ${titleOf(q.holder)}. Once its last question is answered it leaves Parked and waits for room, by itself.`)
  }
  return (
    <li className="hm-item is-question" data-kind="question">
      <span className="hm-kind">Question</span>
      <div className="hm-item-main">
        <a href={agentPath(q.holder)} className="hm-item-title">
          {titleOf(q.holder)}
        </a>{' '}
        <Tid id={q.holder.id} />
        <p className="hm-q">“{q.question ?? 'a question waits for your answer'}”</p>
        <div className="hm-answer">
          <label htmlFor={box} className="hm-sr">
            Your answer
          </label>
          <textarea
            id={box}
            rows={2}
            maxLength={4000}
            placeholder="Type your answer; the agent resumes when you send it"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <Button
            kind="primary"
            size="sm"
            disabled={draft.trim() === '' || busy}
            busy={busy ? 'Sending…' : false}
            title={draft.trim() === '' ? 'type an answer first' : undefined}
            onClick={() => void send()}
          >
            Send answer
          </Button>
        </div>
        {refused !== null && <Refused error={refused} what="The answer was not stored" />}
      </div>
      <span className="hm-age">parked {age(q.holder.updated_at, now)}</span>
    </li>
  )
}

/**
 * AN APPROVAL, DECIDED IN PLACE, as Automate › Approvals decides it
 * (ApprovalDecision.tsx): Approve sends back the digest the item was read at,
 * so the server refuses one that changed; Decline asks a reason, which the
 * route requires. An item that needs a typed confirmation (a one-person
 * workspace's own hold) is refused here with the words to type, and its page
 * is where they are typed.
 */
function ApprovalRow({ a, now, onDone }: { a: ApprovalItem; now: number; onDone: OnDone }) {
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [asking, setAsking] = useState(false)
  const [reason, setReason] = useState('')
  const page = addressToPath(approvalAddress(a.approval_id, a.kind))
  const approve = async () => {
    setBusy('approve')
    setRefused(null)
    const d = await loadApproval(a.approval_id)
    if (d.status === 'error') {
      setBusy(null)
      return setRefused(d.error)
    }
    const digest = dataOf(d)?.approval.digest ?? null
    const r = await approveApproval(a.approval_id, { digest })
    setBusy(null)
    if (r.status === 'error') return setRefused(r.error)
    onDone(`a:${a.approval_id}`, `Approved: ${a.summary}.`)
  }
  const reject = async () => {
    if (reason.trim() === '') return
    setBusy('reject')
    setRefused(null)
    const r = await rejectApproval(a.approval_id, { reason: reason.trim() })
    setBusy(null)
    if (r.status === 'error') return setRefused(r.error)
    onDone(`a:${a.approval_id}`, `Declined: ${a.summary}.`)
  }
  const hours = a.expires_at === null ? null : Math.round((Date.parse(a.expires_at) - now) / 3_600_000)
  return (
    <li className="hm-item" data-kind="approval">
      <span className="hm-kind">Approval</span>
      <div className="hm-item-main">
        <a href={page} className="hm-item-title">
          {a.summary}
        </a>
        <p className="hm-meta">
          {hours === null || !Number.isFinite(hours) ? 'no expiry served' : hours > 0 ? `expires in ${hours} h` : 'expires within the hour'} · {a.kind}
        </p>
        {asking && (
          <div className="hm-reason">
            <label className="c-field">
              <span className="c-lbl">Reason, required</span>
              <textarea className="c-inp" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <span className="hm-acts">
              <Button size="sm" onClick={() => setAsking(false)}>
                Cancel
              </Button>
              <Button size="sm" kind="primary" disabled={reason.trim() === '' || busy !== null} busy={busy === 'reject' ? 'Declining…' : false} onClick={() => void reject()}>
                Decline
              </Button>
            </span>
          </div>
        )}
        {refused !== null && (
          <>
            <Refused error={refused} what="Not decided" />
            <a className="hm-meta" href={page}>
              Decide it on its page
            </a>
          </>
        )}
      </div>
      <div className="hm-acts">
        <Button kind="primary" size="sm" disabled={busy !== null} busy={busy === 'approve' ? 'Approving…' : false} onClick={() => void approve()}>
          Approve
        </Button>
        {!asking && (
          <Button kind="secondary" size="sm" disabled={busy !== null} onClick={() => setAsking(true)}>
            Decline…
          </Button>
        )}
      </div>
      <span className="hm-age">{age(a.requested_at, now)}</span>
    </li>
  )
}

const accountName = (a: LendableAccount): string => a.display_name ?? a.label ?? a.account_id

/**
 * A WORKSPACE OR CLAUDE ACCOUNT REQUEST, decided as Admin › People and roles'
 * Waiting card decides it (People.tsx): Approve in one click; Deny asks the
 * reason the person is shown (required, 500 at most); Lend asks which account
 * from the pool and then confirms, naming it and the person, before it
 * writes; Decline's reason is optional. Lend and Reclaim are never offered
 * together: a request is for an account they do not hold.
 */
function RequestRow({ r, now, accounts, lendWhy, onDone }: { r: WaitingRequest; now: number; accounts: LendableAccount[] | null; lendWhy: string | null; onDone: OnDone }) {
  const id = r.row.workspace.workspace_id ?? ''
  const who = r.row.display_name ?? r.row.email
  const key = `r:${r.kind}:${r.row.email}`
  const [open, setOpen] = useState<'deny' | 'lend' | 'decline' | null>(null)
  const [reason, setReason] = useState('')
  const [account, setAccount] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const pool = (accounts ?? []).filter((a) => !lentToRow(a, r.row))
  const chosen = pool.find((a) => a.account_id === account) ?? null
  const run = async <T,>(call: () => Promise<Result<T>>, sentence: string) => {
    setBusy(true)
    setRefused(null)
    const res = await call()
    setBusy(false)
    if (res.status === 'error') return setRefused(res.error)
    onDone(key, sentence)
  }
  const cancel = () => {
    setOpen(null)
    setConfirming(false)
    setReason('')
  }
  return (
    <li className="hm-item" data-kind={r.kind === 'workspace' ? 'workspace-request' : 'loan-request'}>
      <span className="hm-kind">{r.kind === 'workspace' ? 'Workspace request' : 'Account loan'}</span>
      <div className="hm-item-main">
        <a href={addressToPath('admin/people')} className="hm-item-title hm-ellipsis" title={r.row.email}>
          {who}
        </a>
        <p className="hm-meta hm-ellipsis">
          {r.kind === 'workspace'
            ? 'asks for a personal workspace'
            : `asks to borrow a Claude account${r.row.loan_request?.reason ? `: “${r.row.loan_request.reason}”` : ''}`}
        </p>
        {open === 'deny' && (
          <div className="hm-reason">
            <label className="c-field">
              <span className="c-lbl">Reason, shown to {r.row.email}</span>
              <textarea className="c-inp" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <span className="hm-acts">
              <Button size="sm" onClick={cancel}>
                Cancel
              </Button>
              <Button
                size="sm"
                kind="primary"
                disabled={reason.trim() === '' || busy}
                busy={busy ? 'Denying…' : false}
                onClick={() => void run(() => denyWorkspace(id, reason.trim()), `Denied ${who}'s workspace. They see the reason.`)}
              >
                Deny
              </Button>
            </span>
          </div>
        )}
        {open === 'decline' && (
          <div className="hm-reason">
            <label className="c-field">
              <span className="c-lbl">Reason, shown to {r.row.email} (optional)</span>
              <input className="c-inp" maxLength={200} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <span className="hm-acts">
              <Button size="sm" onClick={cancel}>
                Cancel
              </Button>
              <Button
                size="sm"
                kind="primary"
                disabled={busy}
                busy={busy ? 'Declining…' : false}
                onClick={() => void run(() => declineLoanRequest(id, reason), `Declined ${who}'s request to borrow a Claude account. They are told.`)}
              >
                Decline
              </Button>
            </span>
          </div>
        )}
        {open === 'lend' && (
          <div className="hm-reason">
            {accounts === null ? (
              <p className="hm-meta">The accounts you may lend were not read: {lendWhy ?? 'the broker did not answer'}</p>
            ) : pool.length === 0 ? (
              <p className="hm-meta">No account in the pool can be lent to them.</p>
            ) : confirming && chosen !== null ? (
              <p className="hm-confirm">
                Lend {accountName(chosen)} ({chosen.owner_tenant}) to {r.row.email}? Their work may then run on an account {chosen.owner_tenant} owns.
              </p>
            ) : (
              <label className="c-field">
                <span className="c-lbl">Account from the pool</span>
                <select className="c-inp" value={account} aria-label={`Account to lend to ${r.row.email}`} onChange={(e) => setAccount(e.target.value)}>
                  <option value="">choose…</option>
                  {pool.map((a) => (
                    <option key={a.account_id} value={a.account_id}>
                      {accountName(a)} · {a.owner_tenant}
                      {a.state !== null ? ` · ${a.state.toLowerCase()}` : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <span className="hm-acts">
              <Button size="sm" onClick={cancel}>
                Cancel
              </Button>
              {confirming && chosen !== null ? (
                <Button
                  size="sm"
                  kind="primary"
                  disabled={busy}
                  busy={busy ? 'Lending…' : false}
                  onClick={() => void run(() => setWorkspaceLoan(id, chosen.account_id, true), `Lent ${accountName(chosen)} to ${who}.`)}
                >
                  Lend
                </Button>
              ) : (
                <Button size="sm" kind="primary" disabled={chosen === null} onClick={() => setConfirming(true)}>
                  Lend…
                </Button>
              )}
            </span>
          </div>
        )}
        {refused !== null && <Refused error={refused} what="Not decided" />}
      </div>
      <div className="hm-acts">
        {open === null &&
          (r.kind === 'workspace' ? (
            <>
              <Button
                kind="primary"
                size="sm"
                disabled={busy}
                busy={busy ? 'Approving…' : false}
                onClick={() => void run(() => approveWorkspace(id), `Approved ${who}'s workspace. The job is starting.`)}
              >
                Approve
              </Button>
              <Button kind="secondary" size="sm" onClick={() => setOpen('deny')}>
                Deny…
              </Button>
            </>
          ) : (
            <>
              <Button kind="secondary" size="sm" onClick={() => setOpen('lend')}>
                Lend…
              </Button>
              <Button kind="ghost" size="sm" onClick={() => setOpen('decline')}>
                Decline…
              </Button>
            </>
          ))}
      </div>
      <span className="hm-age">{age(r.at, now)}</span>
    </li>
  )
}

/**
 * AN AGENT WAITING FOR THIS PERSON AT A BROWSER, acted on as Overview's
 * hand-off card acts (Overview.tsx `HandoffCardList`): a held hand-off offers
 * Take control, the live session's own control, once the session says this
 * person may take it; a parked one offers I'm ready (`human-ready`), which
 * re-admits it. Open is the agent's page either way.
 */
function HandoffRow({ t, now, onDone }: { t: Task; now: number; onDone: OnDone }) {
  const h = handoffOf(t)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const ready = async () => {
    setBusy(true)
    setRefused(null)
    const r = await humanReady(t.id)
    setBusy(false)
    if (r.status === 'error') return setRefused(r.error)
    onDone(`h:${t.id}`, `${titleOf(t)} is ready to run again; it is admitted like any agent and starts cold.`)
  }
  const reason = h?.phase === 'hold' ? h.reason : null
  const left = h?.deadline == null ? null : Math.max(0, Math.round((h.deadline - now) / 60_000))
  return (
    <li className="hm-item" data-kind="handoff">
      <span className="hm-kind">At a browser</span>
      <div className="hm-item-main">
        <a href={agentPath(t)} className="hm-item-title">
          {titleOf(t)}
        </a>{' '}
        <Tid id={t.id} />
        <p className="hm-meta">
          {h?.phase === 'parked' ? 'parked until you say you are ready' : (reason ?? 'needs a person at its browser')}
          {left !== null ? ` · ${left} min left` : ''}
        </p>
        {refused !== null && <Refused error={refused} what="Not done" />}
      </div>
      <div className="hm-acts">
        {h?.phase === 'hold' && <TakeControl taskId={t.id} />}
        {h?.phase === 'parked' && (
          <Button kind="primary" size="sm" disabled={busy} busy={busy ? 'Asking…' : false} onClick={() => void ready()}>
            I&apos;m ready
          </Button>
        )}
        <ButtonLink kind="ghost" size="sm" href={agentPath(t)}>
          Open
        </ButtonLink>
      </div>
      <span className="hm-age">{age(t.updated_at, now)}</span>
    </li>
  )
}

/**
 * TAKE CONTROL, as Overview's hand-off card offers it: the session is read
 * once, and the live view's own control is drawn only when the session is
 * open and says this person may control it. Once the take has gone through,
 * the agent's Browser tab opens, where the canvas they now control is.
 */
function TakeControl({ taskId }: { taskId: string }) {
  const [session, setSession] = useState<LiveSession | null>(null)
  const read = useCallback(
    (afterTake: boolean) => {
      let live = true
      void loadLiveSession(taskId).then((r) => {
        if (!live) return
        const s = dataOf(r)?.session ?? null
        if (afterTake && s !== null && s.controller !== null && s.controller.email === s.you.email) {
          window.history.pushState(null, '', browserTabPath(taskId))
          window.dispatchEvent(new PopStateEvent('popstate'))
          return
        }
        setSession(s)
      })
      return () => {
        live = false
      }
    },
    [taskId],
  )
  useEffect(() => read(false), [read])
  if (session === null || session.state !== 'open' || !session.you.can_control) return null
  if (session.controller !== null && session.controller.email === session.you.email) return null
  return <LiveControl taskId={taskId} session={session} reload={() => void read(true)} takeOnly />
}

// ---------------------------------------------------------------- recently finished

function RecentlyFinished({ mine, now }: { mine: Task[] | null; now: number }) {
  const ended = mine === null ? null : mine.filter((t) => FINISHED.has(t.state)).slice(0, 5)
  return (
    <Card id="home-finished">
      <Head
        title="Recently finished"
        count={ended?.length ?? null}
        why="Your agents that ended among your newest tasks read. Failures are in Needs you."
        action={<CardLink href={addressToPath('work/running/recent')}>History</CardLink>}
      />
      {ended === null ? (
        <p className="hm-dash">— your tasks not read yet</p>
      ) : ended.length === 0 ? (
        <p className="hm-empty">Nothing of yours finished among your newest read.</p>
      ) : (
        <table className="hm-table">
          <tbody>
            {ended.map((t) => (
              <tr key={t.id}>
                <td>
                  <StateDot state={t.state} />
                </td>
                <td className="hm-ellipsis">
                  <a href={agentPath(t)}>{titleOf(t)}</a> <Tid id={t.id} />
                </td>
                <td className="hm-age">{age(t.completed_at ?? t.updated_at, now)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------- side column

/** Start: the three ways the console starts work. A person with no workspace yet cannot, and is told why. */
function StartCard({ profile }: { profile: Profile | null }) {
  const blocked = profile !== null && !profile.workspaces.some((w) => w.selectable) ? 'Starting work needs a workspace: request yours first.' : null
  const ways: [string, string][] = [
    ['Start an agent', 'work/new'],
    ['Start a workflow', 'work/new-workflow'],
    ['From a GitHub issue', 'work/new-issue'],
  ]
  return (
    <Card id="home-start" className="hm-start">
      <Head title="Start" />
      <div className="hm-startbtns">
        {ways.map(([label, to], i) =>
          blocked === null ? (
            <ButtonLink key={to} kind={i === 0 ? 'primary' : 'secondary'} href={addressToPath(to)}>
              {label}
            </ButtonLink>
          ) : (
            <Button key={to} kind={i === 0 ? 'primary' : 'secondary'} disabled title={blocked}>
              {label}
            </Button>
          ),
        )}
      </div>
      {blocked !== null && <p className="hm-meta">{blocked}</p>}
    </Card>
  )
}

/** The user guide's page for a person with no workspace yet, opened in the console without a reload. */
const STARTER_GUIDE = guidePath('start-here')

function openInApp(e: MouseEvent<HTMLAnchorElement>) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  e.preventDefault()
  openGuide(e.currentTarget.getAttribute('href') ?? STARTER_GUIDE)
}

/**
 * The workspaces the person belongs to, with their role in each. Setup
 * progress is read for the acting workspace only (`GET /v1/onboarding`): a
 * figure for any other would need a read per workspace the console does not
 * make here, so none is drawn rather than one guessed.
 */
function YourWorkspaces({ profile, round }: { profile: Profile | null; round: number }) {
  const ob = dataOf(useLoad(readOnboarding, round))
  const ws = profile?.workspaces ?? null
  return (
    <Card>
      <Head title="Your workspaces" count={ws === null ? null : ws.length} />
      {ws !== null && ws.length === 0 && (
        <p className="hm-empty">
          No workspace yet.{' '}
          <ButtonLink size="sm" href={STARTER_GUIDE} onClick={openInApp}>
            Read the starter guide
          </ButtonLink>
        </p>
      )}
      <ul className="hm-wslist">
        {(ws ?? []).map((w) => {
          const total = w.is_acting && ob !== null && Array.isArray(ob.steps) ? ob.steps.filter(stepRequired).length : null
          const doneN = total !== null && ob !== null ? doneCount(ob) : null
          return (
            <li key={w.tenant_id}>
              <span className="hm-ellipsis" title={w.tenant_id}>
                {w.kind === 'user' ? 'Your own workspace' : w.display_name}
                {w.is_acting && <span className="hm-meta"> · acting</span>}
              </span>
              <span className={`hm-role is-${w.role.name}`}>{w.role.name}</span>
              {total !== null && doneN !== null && total > 0 ? (
                <span className={`hm-prog${doneN >= total ? ' is-done' : ''}`} title={`${doneN} of ${total} setup steps done`}>
                  <span className="hm-prog-bar">
                    <i style={{ width: `${Math.min(100, (doneN / total) * 100)}%` }} />
                  </span>
                  <span>
                    {doneN}/{total}
                  </span>
                </span>
              ) : (
                <span className="hm-meta">{w.state !== null && w.state !== 'ready' ? w.state : ''}</span>
              )}
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

/** The acting workspace in three numbers, for whoever lands here: Overview has the rest. */
function WorkspaceGlance({ round }: { round: number }) {
  const stats: Stats | null = dataOf(useLoad(readStats, round))
  const by = stats?.tasks_by_state ?? null
  const sum = (ks: readonly string[]) => (by === null ? null : ks.reduce((n, k) => n + (by[k] ?? 0), 0))
  const failed = sum([...FAILED])
  return (
    <Card>
      <Head title="This workspace" action={<CardLink href={addressToPath('overview/now')}>Overview</CardLink>} />
      <dl className="hm-glance">
        <div>
          <dt>holding capacity</dt>
          <dd>{sum([...LIVE]) ?? '—'}</dd>
        </div>
        <div>
          <dt>waiting</dt>
          <dd>{sum([...WAITING]) ?? '—'}</dd>
        </div>
        <div>
          <dt>failed</dt>
          <dd className={failed ? 'is-bad' : ''}>{failed ?? '—'}</dd>
        </div>
      </dl>
    </Card>
  )
}
