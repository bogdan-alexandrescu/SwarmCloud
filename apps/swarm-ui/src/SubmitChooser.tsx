import { useCallback, useEffect, useState } from 'react'
import { agentName } from './agentlist'
import { loadMe, loadRuns, loadTasks, loadWorkflows } from './api'
import { Button, Chip, NamedMark } from './components'
import { Dash } from './components/Chip'
import type { Result } from './fetch'
import { HelpCard } from './HelpCard'
import { runAddress } from './IssueSubmit'
import { addressToPath } from './paths'
import { ISSUE_FORM, TASK_FORM, WORKFLOW_FORM, shortcutsIn, typedIntoField } from './shortcuts'
import { AGED_AFTER_MS, PageHead, RefreshControl, useClaimPageAge } from './Shell'
import { firstLine, workflowLabel } from './stepviews'
import { timeAgo, type Task } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import { stateWord } from './words'
import './styles/submit.css'

/**
 * THE SUBMIT CHOOSER (/submit; submit.html M2, the owner's pick 2026-10-01).
 *
 * Three large choices, opened by the spine's Submit button and by N: a card
 * per form, each with its icon, a Start button and its key (T, W, I). The
 * third is "From a GitHub issue" (intake-tenants.html 1A, /submit/issue).
 *
 * RECENT SUBMISSIONS ARE YOURS, FROM THE READS THAT EXIST (owner QA R15,
 * 2026-10-04): the card said "No recent submissions yet" to an owner with runs
 * on the page beside it. No route serves "what did I submit, with what
 * settings", but who submitted each task, workflow and run IS served
 * (`submitted_by`, `created_by`), and so is who you are (`/v1/tenants/me`).
 * The card lists your newest lone tasks, workflows and issue runs from those
 * reads -- each a link to its page -- and says which windows it looked in. A
 * workflow's steps are not lone tasks and are not listed twice; who you are
 * unread means nothing is listed, never everyone's.
 *
 * T, W AND I FOLLOW N's GUARDS (SkyShell in Spine.tsx): never while focus is in a
 * field, never with a modifier held, never for a key another handler already
 * consumed, and never while something sits over the page.
 */

/** The three forms' addresses, as `go` takes them; owned by the shortcut table. */
export { ISSUE_FORM, TASK_FORM, WORKFLOW_FORM }

/**
 * T, W and I, READ FROM THE SHORTCUT TABLE (G1-21) that the Help topic
 * `keyboard` is generated from, so the page and Help cannot name different keys.
 */
const KEYS: Readonly<Record<string, string>> = Object.fromEntries(shortcutsIn('submit').map((s) => [s.key, s.to!]))

/**
 * EACH CARD'S KEYCAP, FROM THE SAME TABLE (live QA SUB-19): the caps were
 * typed into the cards, so a cap and the key that works could differ. A form
 * the table has no key for draws no cap rather than a guessed one.
 */
const CAP: Readonly<Record<string, string>> = Object.fromEntries(shortcutsIn('submit').map((s) => [s.to!, s.label]))

/** What sits over the page and owns the keyboard: the same list as N's. */
const OVER_THE_PAGE = '[role="dialog"], [aria-modal="true"], aside.adm-side'

/** The task card's icon: one agent (submit.html `i-agents`). */
function TaskGlyph() {
  return (
    <svg className="sb-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="5" y="7.5" width="14" height="11" rx="3" />
      <path d="M12 4v3.5M9 12.5v1M15 12.5v1M2.5 12v3M21.5 12v3" />
    </svg>
  )
}

/** The workflow card's icon: steps joined (submit.html `i-workflows`). */
function WorkflowGlyph() {
  return (
    <svg className="sb-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="6" cy="18" r="2.2" />
      <circle cx="18" cy="12" r="2.2" />
      <path d="M8.2 6h3a3 3 0 0 1 3 3v.8M8.2 18h3a3 3 0 0 0 3-3v-.8" />
    </svg>
  )
}

/** The issue card's icon: an issue's ring and dot (intake-tenants.html 1A). */
function IssueGlyph() {
  return (
    <svg className="sb-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="1.8" />
    </svg>
  )
}

export function SubmitChooser({ go }: { go: (to: string) => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const to = KEYS[e.key.toLowerCase()]
      if (to === undefined) return
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
      if (typedIntoField(e.target)) return
      if (document.querySelector(OVER_THE_PAGE) !== null) return
      e.preventDefault()
      go(to)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go])

  // THE CHOOSER READS (QA G1-05/G4-23, 2026-10-07): "Start from a recent one"
  // reads who you are, the tasks, the workflows and the issue runs, so the
  // head said "reads nothing" beside "Reading your recent submissions…". Its
  // one age is that read's, on the refresh that renews it, and the frame
  // prints none beside it (`useClaimPageAge`): one age per screen.
  const recent = useRecentRead()
  useClaimPageAge(true)
  const now = useNow(AGE_TICK_MS)

  return (
    <section className="sb-chooser" aria-labelledby="submit-h">
      {/* The page head every page draws (Q2): the title, its `?` and the
          refresh carrying the recent read's age, on one row. */}
      <PageHead title="Submit" headingId="submit-h">
        <RefreshControl
          readAt={recent.readAt}
          now={now}
          stale={recent.readAt !== null && now - recent.readAt > AGED_AFTER_MS}
          reading={recent.reading}
          onRefresh={recent.reload}
        />
      </PageHead>
      <div className="sb-choices">
        <div className="sb-card sb-choice">
          <h2><TaskGlyph />Start an agent</h2>
          <p className="sb-note">
            One agent, one runner. It lands ready or parked and costs nothing until the scheduler admits it.
          </p>
          <span className="sb-row">
            <Button kind="primary" aria-keyshortcuts={CAP[TASK_FORM]} onClick={() => go(TASK_FORM)}>
              Start an agent
            </Button>
            {CAP[TASK_FORM] !== undefined && <kbd className="sb-kbd">{CAP[TASK_FORM]}</kbd>}
          </span>
        </div>
        <div className="sb-card sb-choice">
          <h2><WorkflowGlyph />Submit a workflow</h2>
          <p className="sb-note">Steps in stages that hand work to each other, opening one PR or one per step.</p>
          <span className="sb-row">
            <Button aria-keyshortcuts={CAP[WORKFLOW_FORM]} onClick={() => go(WORKFLOW_FORM)}>
              Start a workflow
            </Button>
            {CAP[WORKFLOW_FORM] !== undefined && <kbd className="sb-kbd">{CAP[WORKFLOW_FORM]}</kbd>}
          </span>
        </div>
        <div className="sb-card sb-choice">
          <h2><IssueGlyph />From a GitHub issue</h2>
          <p className="sb-note">
            Name an issue. A planner reads it and writes a plan; once the plan is approved it runs as a workflow.
          </p>
          <span className="sb-row">
            <Button aria-keyshortcuts={CAP[ISSUE_FORM]} onClick={() => go(ISSUE_FORM)}>
              Start from an issue
            </Button>
            {CAP[ISSUE_FORM] !== undefined && <kbd className="sb-kbd">{CAP[ISSUE_FORM]}</kbd>}
          </span>
        </div>
      </div>
      <section className="sb-card sb-recent" aria-labelledby="submit-recent-h">
        <div className="sb-card-hr">
          <h2 className="sb-card-h" id="submit-recent-h">Start from a recent one</h2>
          <HelpCard topic="recent-submissions" />
        </div>
        <RecentSubmissions go={go} read={recent.read} reading={recent.reading} onRetry={recent.reload} />
      </section>
      <p className="sb-note sb-foot">
        The Submit button in the sidebar opens this page; <kbd className="sb-kbd">N</kbd> from anywhere does too.
      </p>
    </section>
  )
}

/** How many of your submissions the card lists. */
export const RECENT_SHOWN = 5

/**
 * HOW MUCH THE CARD READS TO LIST FIVE (live QA SUB-6). It read the tenant's
 * newest 200 tasks, 100 workflows and a full page of runs -- about 6 MB and
 * 7.5 s on the live tenant -- to filter five rows out in the browser. Tasks
 * are now asked for as YOURS (`submitted_by=me`, applied in the query), full
 * rows still, because a workflow's name is its steps' `metadata.unit`. The
 * workflow and run routes take no submitter filter, so they are a smaller
 * window of the tenant's newest, and the card names that window as before.
 */
export const RECENT_TASKS_READ = 30
export const RECENT_WORKFLOWS_READ = 20
export const RECENT_RUNS_READ = 20

/**
 * THE CHIP'S WORD FOR A KIND (VOCAB-AGENTS-UI, part of #1680): the console
 * calls the running unit an agent; `kind: 'task'` stays the identifier.
 */
const KIND_LABEL: Record<RecentSubmission['kind'], string> = { task: 'agent', workflow: 'workflow', 'issue run': 'issue run' }

export interface RecentSubmission {
  kind: 'task' | 'workflow' | 'issue run'
  id: string
  name: string
  at: string
  /**
   * Where it stands, as a word (`stateWord`): what tells two rows apart when
   * neither has a title. Null when the row carried no state, and then the row
   * says only its age rather than a guessed state.
   */
  state: string | null
  /** The router address `go` takes, and the path a new tab opens. */
  to: string
}

/**
 * WHAT A LONE AGENT WAS (QA1 runs-submit-26): five rows read
 * "claude-code agent · <hex>" and nothing said what each did. Its title
 * (`metadata.title`), else the first line of its prompt (`input.prompt`,
 * which the API masks at read time), else `agentName` as before.
 */
function stateOf(state: unknown): string | null {
  return typeof state === 'string' && state !== '' ? stateWord(state) : null
}

function taskWhat(t: Task): string {
  const title = t.metadata?.title
  if (typeof title === 'string' && title.trim() !== '') return title.trim()
  const input = t.input
  const prompt = typeof input === 'object' && input !== null ? (input as Record<string, unknown>).prompt : undefined
  const line = typeof prompt === 'string' ? firstLine(prompt.trim()).trim() : ''
  return line !== '' ? line : agentName(t)
}

type RecentRead =
  | { kind: 'reading' }
  | { kind: 'who-unread'; why: string; unread: string[] }
  | { kind: 'read'; items: RecentSubmission[]; looked: RecentWindow[]; unread: string[] }

function okData<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

function unreadWhy(what: string, r: Result<unknown>): string | null {
  return r.status === 'error' ? `${what} not read: ${r.error.message}` : null
}

/**
 * YOUR SUBMISSIONS, NEWEST FIRST, from what the reads served (owner QA R15):
 * a lone task you submitted (not a workflow's step, not an agent's child), a
 * workflow you submitted, an issue run you created. Pure, for the test.
 */
export function recentSubmissions(
  email: string,
  tasks: readonly Task[] | null,
  workflows: readonly import('./types').Workflow[] | null,
  runs: readonly import('./types').IssueRunSummary[] | null,
): RecentSubmission[] {
  return yourSubmissions(email, tasks, workflows, runs).slice(0, RECENT_SHOWN)
}

/** All of yours in what was read, newest first: what the card's five are cut from. */
function yourSubmissions(
  email: string,
  tasks: readonly Task[] | null,
  workflows: readonly import('./types').Workflow[] | null,
  runs: readonly import('./types').IssueRunSummary[] | null,
): RecentSubmission[] {
  const mine = (who: string | null | undefined) => typeof who === 'string' && who.toLowerCase() === email.toLowerCase()
  const byId = new Map((tasks ?? []).map((t) => [t.id, t]))
  const out: RecentSubmission[] = []
  for (const t of tasks ?? []) {
    if (!mine(t.submitted_by) || t.workflow_id !== null || t.step_id !== null || (t.parent_task_id ?? null) !== null) continue
    out.push({ kind: 'task', id: t.id, name: taskWhat(t), at: t.created_at, state: stateOf(t.state), to: `work/task/${t.id}` })
  }
  for (const w of workflows ?? []) {
    if (!mine(w.submitted_by)) continue
    out.push({
      kind: 'workflow', id: w.workflow_id, name: workflowLabel(w, byId) ?? w.workflow_id, at: w.created_at, state: stateOf(w.state),
      to: `work/workflows?${new URLSearchParams({ wf: w.workflow_id }).toString()}`,
    })
  }
  for (const r of runs ?? []) {
    if (!mine(r.created_by)) continue
    out.push({ kind: 'issue run', id: r.id, name: r.issue_read?.title || r.issue.ref, at: r.created_at, state: stateOf(r.state), to: runAddress(r.id) })
  }
  return out.sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
}

/**
 * A WINDOW THE CARD READ: which kind it holds, how many it held, and how many
 * of those were yours.
 */
export interface RecentWindow {
  kind: RecentSubmission['kind']
  read: number
  yours: number
}

/**
 * A window in words. Whose window each is, said per window: the agents are
 * yours, the workflows and runs the tenant's (live QA SUB-6); a run window
 * straight after the workflows' is "its".
 */
function windowWords(w: RecentWindow, after: RecentWindow | undefined): string {
  const plural = w.read === 1 ? '' : 's'
  if (w.kind === 'task') return `your newest ${w.read} agent${plural}`
  if (w.kind === 'workflow') return `the tenant\u2019s newest ${w.read} workflow${plural}`
  return `${after?.kind === 'workflow' ? 'its' : 'the tenant\u2019s'} newest ${w.read} issue run${plural}`
}

function windowsWords(ws: readonly RecentWindow[], last: ', ' | ' and ' | ' or '): string {
  const said = ws.map((w, i) => windowWords(w, ws[i - 1]))
  return said.length < 2 ? said.join('') : `${said.slice(0, -1).join(', ')}${last}${said[said.length - 1]}`
}

/**
 * THE CARD'S FOOTNOTE (QA1 runs-submit-26): it said only the windows read --
 * "Among your newest 30 agents, the tenant's newest 3 workflows, its newest 2
 * issue runs" -- under a list of five agents, so it counted workflows and runs
 * the list never showed. It counts what is listed first, then where it was
 * found. With nothing listed it names the windows alone, as before (VQA V135);
 * with no window, nothing.
 *
 * AND EACH WINDOW SAYS WHAT BECAME OF IT (visual QA pass 2, runs2-10): "Found
 * among your newest 27 agents, the tenant's newest 3 workflows, its newest 2
 * issue runs" under five agents still read as if workflows and runs had been
 * listed. A window is "found among" only when the list shows its kind; one
 * whose yours are older than the five says so; one with none of yours says
 * that. Pure, for the test.
 */
export function recentScope(items: readonly RecentSubmission[], looked: readonly RecentWindow[]): string | null {
  if (looked.length === 0) return null
  if (items.length === 0) return `Among ${windowsWords(looked, ', ')}.`
  const kinds: RecentSubmission['kind'][] = ['task', 'workflow', 'issue run']
  const listed = kinds.flatMap((k) => {
    const n = items.filter((it) => it.kind === k).length
    return n === 0 ? [] : [`${n} ${KIND_LABEL[k]}${n === 1 ? '' : 's'}`]
  })
  const shown = (w: RecentWindow) => items.some((it) => it.kind === w.kind)
  const found = looked.filter(shown)
  const older = looked.filter((w) => !shown(w) && w.yours > 0)
  const none = looked.filter((w) => !shown(w) && w.yours === 0)
  return [
    `Listed: ${listed.join(', ')}, newest first.`,
    `Found among ${windowsWords(found, ', ')}.`,
    ...(older.length === 0 ? [] : [`Yours among ${windowsWords(older, ' and ')} are older than these.`]),
    ...(none.length === 0 ? [] : [`None of yours among ${windowsWords(none, ' or ')}.`]),
  ].join(' ')
}

/**
 * THE RECENT READ, re-run by the head's refresh. The list on screen stays
 * drawn while it is read again; `readAt` is the newest payload the read
 * received (null until one lands, and when who you are was not read).
 */
function useRecentRead(): { read: RecentRead; readAt: number | null; reading: boolean; reload: () => void } {
  const [read, setRead] = useState<RecentRead>({ kind: 'reading' })
  const [readAt, setReadAt] = useState<number | null>(null)
  const [reading, setReading] = useState(true)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    setReading(true)
    void Promise.all([
      loadMe(),
      loadTasks(RECENT_TASKS_READ, { submittedBy: 'me' }),
      loadWorkflows(RECENT_WORKFLOWS_READ),
      loadRuns(null, RECENT_RUNS_READ),
    ]).then(([me, tasks, workflows, runs]) => {
      if (!live) return
      setReading(false)
      const who = okData(me)
      const unread = [unreadWhy('Agents', tasks), unreadWhy('Workflows', workflows), unreadWhy('Issue runs', runs)]
        .filter((x): x is string => x !== null)
      if (who === null) {
        setReadAt(null)
        setRead({ kind: 'who-unread', why: me.status === 'error' ? me.error.message : 'the read returned nothing', unread })
        return
      }
      const ats = [me, tasks, workflows, runs].flatMap((r) => (r.status === 'ok' || r.status === 'stale' || r.status === 'empty' ? [r.fetchedAt] : []))
      setReadAt(Math.max(...ats))
      const t = tasks.status === 'empty' ? [] : (okData(tasks)?.tasks ?? null)
      const w = workflows.status === 'empty' ? [] : (okData(workflows)?.workflows ?? null)
      const r = runs.status === 'empty' ? [] : (okData(runs)?.runs ?? null)
      // A WINDOW THAT HELD NOTHING IS NOT A WINDOW (VQA V135): "Among the
      // newest 0 tasks, 0 workflows, 0 issue runs" under "None of yours yet."
      // said nothing twice. Only a window that held something is named.
      const all = yourSubmissions(who.principal.email, t, w, r)
      const windowOf = (kind: RecentSubmission['kind'], rows: readonly unknown[] | null): RecentWindow | null =>
        rows === null || rows.length === 0 ? null : { kind, read: rows.length, yours: all.filter((it) => it.kind === kind).length }
      const looked = [windowOf('task', t), windowOf('workflow', w), windowOf('issue run', r)].filter((x): x is RecentWindow => x !== null)
      setRead({ kind: 'read', items: all.slice(0, RECENT_SHOWN), looked, unread })
    })
    return () => {
      live = false
    }
  }, [nonce])
  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { read, readAt, reading, reload }
}

/**
 * THE READS THAT FAILED, EACH ON ITS OWN LINE, AND ONE "Try again" (VQA V136):
 * the card said only "who you are is unknown" while the task read beside it
 * had failed too, and offered nothing to do about either.
 */
function UnreadLines({ unread, reading, onRetry }: { unread: readonly string[]; reading: boolean; onRetry: () => void }) {
  return (
    <>
      {unread.map((u) => (
        <p key={u} className="sb-note sb-recent-scope"><Dash why={u} /> {u}</p>
      ))}
      <p className="sb-row sb-recent-scope">
        <Button size="sm" className="sb-recent-retry" busy={reading ? 'Reading…' : false} onClick={onRetry}>Try again</Button>
      </p>
    </>
  )
}

function RecentSubmissions({ go, read, reading, onRetry }: {
  go: (to: string) => void
  read: RecentRead
  reading: boolean
  onRetry: () => void
}) {
  if (read.kind === 'reading') {
    return (
      <p className="sb-empty" aria-busy="true">
        <NamedMark mark="queued" hue="neu" />
        <span>Reading your recent submissions…</span>
      </p>
    )
  }
  if (read.kind === 'who-unread') {
    // WHO YOU ARE IS UNREAD: nothing can be called yours, so nothing is listed.
    return (
      <>
        <p className="sb-empty">
          <Dash why={`Who you are could not be read (${read.why}), so your submissions cannot be told from anyone else's.`} />
          <span>Not read: who you are is unknown here ({read.why}), so nothing can be called yours.</span>
        </p>
        <UnreadLines unread={read.unread} reading={reading} onRetry={onRetry} />
      </>
    )
  }
  const scope = recentScope(read.items, read.looked)
  return (
    <>
      {read.items.length === 0 ? (
        <p className="sb-empty">
          <NamedMark mark="queued" hue="neu" />
          <span>
            {read.unread.length > 0
              ? 'None of yours in what was read.'
              : read.looked.length === 0 ? 'Nothing has been submitted here yet.' : 'None of yours yet.'}
          </span>
        </p>
      ) : (
        <ul className="sb-recent-list">
          {read.items.map((it) => (
            <li key={`${it.kind}:${it.id}`} className="sb-recent-i">
              <Chip>{KIND_LABEL[it.kind]}</Chip>
              <a className="sb-recent-n" href={addressToPath(it.to)} title={`${it.name} · ${it.id}`} onClick={(e) => {
                if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                e.preventDefault()
                go(it.to)
              }}>{it.name}</a>
              {/* Its state beside its age (QA1 runs-submit-26): rows that
                  share a name and an age are still told apart. */}
              <span className="sb-recent-at" title={it.at}>{it.state === null ? '' : `${it.state} · `}{timeAgo(it.at)}</span>
            </li>
          ))}
        </ul>
      )}
      {scope !== null && <p className="sb-note sb-recent-scope">{scope}</p>}
      {read.unread.length > 0 && <UnreadLines unread={read.unread} reading={reading} onRetry={onRetry} />}
    </>
  )
}
