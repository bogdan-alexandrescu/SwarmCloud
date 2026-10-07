import { useCallback, useEffect, useState } from 'react'
import { agentName } from './agentlist'
import { loadMe, loadRuns, loadTasks, loadWorkflows } from './api'
import { Button, Chip, NamedMark } from './components'
import { Dash } from './components/Chip'
import type { Result } from './fetch'
import { HelpCard } from './HelpCard'
import { runAddress } from './IssueSubmit'
import { addressToPath } from './paths'
import { AGED_AFTER_MS, PageHead, RefreshControl, useClaimPageAge } from './Shell'
import { workflowLabel } from './stepviews'
import { timeAgo, type Task } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
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

/** The task form's and the workflow form's addresses, as `go` takes them. */
export const TASK_FORM = 'work/new'
export const WORKFLOW_FORM = 'work/new-workflow'
/** The issue form (intake-tenants.html 1A): /submit/issue, key I. */
export const ISSUE_FORM = 'work/new-issue'

const KEYS: Readonly<Record<string, string>> = { t: TASK_FORM, w: WORKFLOW_FORM, i: ISSUE_FORM }

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
      const el = e.target instanceof Element ? e.target : null
      if (el !== null && el.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') !== null) return
      if (el instanceof HTMLElement && el.isContentEditable) return
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
          <h2><TaskGlyph />Submit a task</h2>
          <p className="sb-note">
            One agent, one runner. It lands READY or PARKED and costs nothing until the scheduler admits it.
          </p>
          <span className="sb-row">
            <Button kind="primary" aria-keyshortcuts="T" onClick={() => go(TASK_FORM)}>
              Start a task
            </Button>
            <kbd className="sb-kbd">T</kbd>
          </span>
        </div>
        <div className="sb-card sb-choice">
          <h2><WorkflowGlyph />Submit a workflow</h2>
          <p className="sb-note">Steps in stages that hand work to each other, opening one PR or one per step.</p>
          <span className="sb-row">
            <Button aria-keyshortcuts="W" onClick={() => go(WORKFLOW_FORM)}>
              Start a workflow
            </Button>
            <kbd className="sb-kbd">W</kbd>
          </span>
        </div>
        <div className="sb-card sb-choice">
          <h2><IssueGlyph />From a GitHub issue</h2>
          <p className="sb-note">
            Name an issue. A planner reads it and writes a plan; once the plan is approved it runs as a workflow.
          </p>
          <span className="sb-row">
            <Button aria-keyshortcuts="I" onClick={() => go(ISSUE_FORM)}>
              Start from an issue
            </Button>
            <kbd className="sb-kbd">I</kbd>
          </span>
        </div>
      </div>
      <section className="sb-card sb-recent" aria-labelledby="submit-recent-h">
        <div className="sb-card-hr">
          <h2 className="sb-card-h" id="submit-recent-h">Start from a recent one</h2>
          <HelpCard topic="recent-submissions" />
        </div>
        <RecentSubmissions go={go} read={recent.read} />
      </section>
      <p className="sb-note sb-foot">
        The spine&rsquo;s Submit button opens this page; <kbd className="sb-kbd">N</kbd> from anywhere does too.
      </p>
    </section>
  )
}

/** How many of your submissions the card lists. */
export const RECENT_SHOWN = 5

export interface RecentSubmission {
  kind: 'task' | 'workflow' | 'issue run'
  id: string
  name: string
  at: string
  /** The router address `go` takes, and the path a new tab opens. */
  to: string
}

type RecentRead =
  | { kind: 'reading' }
  | { kind: 'who-unread'; why: string }
  | { kind: 'read'; items: RecentSubmission[]; looked: string[]; unread: string[] }

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
  runs: readonly import('./types').IssueRun[] | null,
): RecentSubmission[] {
  const mine = (who: string | null | undefined) => typeof who === 'string' && who.toLowerCase() === email.toLowerCase()
  const byId = new Map((tasks ?? []).map((t) => [t.id, t]))
  const out: RecentSubmission[] = []
  for (const t of tasks ?? []) {
    if (!mine(t.submitted_by) || t.workflow_id !== null || t.step_id !== null || (t.parent_task_id ?? null) !== null) continue
    out.push({ kind: 'task', id: t.id, name: agentName(t), at: t.created_at, to: `work/task/${t.id}` })
  }
  for (const w of workflows ?? []) {
    if (!mine(w.submitted_by)) continue
    out.push({
      kind: 'workflow', id: w.workflow_id, name: workflowLabel(w, byId) ?? w.workflow_id, at: w.created_at,
      to: `work/workflows?${new URLSearchParams({ wf: w.workflow_id }).toString()}`,
    })
  }
  for (const r of runs ?? []) {
    if (!mine(r.created_by)) continue
    out.push({ kind: 'issue run', id: r.id, name: r.issue_read?.title || r.issue.ref, at: r.created_at, to: runAddress(r.id) })
  }
  return out.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, RECENT_SHOWN)
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
    void Promise.all([loadMe(), loadTasks(), loadWorkflows(), loadRuns()]).then(([me, tasks, workflows, runs]) => {
      if (!live) return
      setReading(false)
      const who = okData(me)
      if (who === null) {
        setReadAt(null)
        setRead({ kind: 'who-unread', why: me.status === 'error' ? me.error.message : 'the read returned nothing' })
        return
      }
      const ats = [me, tasks, workflows, runs].flatMap((r) => (r.status === 'ok' || r.status === 'stale' || r.status === 'empty' ? [r.fetchedAt] : []))
      setReadAt(Math.max(...ats))
      const t = tasks.status === 'empty' ? [] : (okData(tasks)?.tasks ?? null)
      const w = workflows.status === 'empty' ? [] : (okData(workflows)?.workflows ?? null)
      const r = runs.status === 'empty' ? [] : (okData(runs)?.runs ?? null)
      const looked = [
        t === null ? null : `the newest ${t.length} task${t.length === 1 ? '' : 's'}`,
        w === null ? null : `${w.length} workflow${w.length === 1 ? '' : 's'}`,
        r === null ? null : `${r.length} issue run${r.length === 1 ? '' : 's'}`,
      ].filter((x): x is string => x !== null)
      const unread = [unreadWhy('Tasks', tasks), unreadWhy('Workflows', workflows), unreadWhy('Issue runs', runs)]
        .filter((x): x is string => x !== null)
      setRead({ kind: 'read', items: recentSubmissions(who.principal.email, t, w, r), looked, unread })
    })
    return () => {
      live = false
    }
  }, [nonce])
  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { read, readAt, reading, reload }
}

function RecentSubmissions({ go, read }: { go: (to: string) => void; read: RecentRead }) {
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
      <p className="sb-empty">
        <Dash why={`Who you are could not be read (${read.why}), so your submissions cannot be told from anyone else's.`} />
        <span>Not read: who you are is unknown here.</span>
      </p>
    )
  }
  const scope = read.looked.length === 0 ? null : `Among ${read.looked.join(', ')} this tenant has.`
  return (
    <>
      {read.items.length === 0 ? (
        <p className="sb-empty">
          <NamedMark mark="queued" hue="neu" />
          <span>{read.unread.length === 0 ? 'None of yours yet.' : 'None of yours in what was read.'}</span>
        </p>
      ) : (
        <ul className="sb-recent-list">
          {read.items.map((it) => (
            <li key={`${it.kind}:${it.id}`} className="sb-recent-i">
              <Chip>{it.kind}</Chip>
              <a className="sb-recent-n" href={addressToPath(it.to)} title={`${it.name} · ${it.id}`} onClick={(e) => {
                if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                e.preventDefault()
                go(it.to)
              }}>{it.name}</a>
              <span className="sb-recent-at" title={it.at}>{timeAgo(it.at)}</span>
            </li>
          ))}
        </ul>
      )}
      {scope !== null && <p className="sb-note sb-recent-scope">{scope}</p>}
      {read.unread.map((u) => (
        <p key={u} className="sb-note sb-recent-scope"><Dash why={u} /> {u}</p>
      ))}
    </>
  )
}
