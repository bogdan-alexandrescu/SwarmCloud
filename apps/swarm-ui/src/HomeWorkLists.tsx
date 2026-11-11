/**
 * HOME'S YOUR WORK AS TWO LISTS, AGENTS AND WORKFLOWS (the owner's pick of
 * the "Two lists" mock-up, 2026-11-11). Agents: one two-line row each, a
 * browser-window or terminal icon, the name, its workflow or the question it
 * asks, a state dot whose word is its tooltip and the elapsed time; a tiny
 * live thumbnail ONLY for an agent with a browser, repainted about once a
 * second (`HOME_THUMB_PACE_MS`). Workflows: one two-line row each, its name,
 * its pull request, a segmented step bar, the current step and the elapsed
 * time; the chevron opens its steps and which of its agents are where.
 *
 * THE DATA IS WHAT HOME READS: the person's newest tasks (`mine`), the owner
 * questions held on them (answered in place, as Needs you answers them), and
 * for the workflows those tasks belong to, one by-id workflow read
 * (`loadWorkflowsById`, the route the Timeline batches with) for each
 * workflow's name, steps and pull request. Whether an agent has a browser is
 * its runner profile's (`LIVE_BROWSER_PROFILES`), never its name.
 *
 * Waiting work holds no capacity (invariant 1): a QUEUED, READY or PARKED
 * agent is drawn as waiting and opens no live view; the thumbnail is drawn
 * only for a RUNNING browser agent (`showsLiveThumb`).
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { answerQuestion, loadWorkflowsById, WORKFLOW_IDS_PER_READ, type WorkflowRead } from './api'
import { Button, ButtonLink, Card, Tooltip } from './components'
import type { ApiError } from './fetch'
import { errorHeading } from './fetch'
import type { HeldQuestion } from './Handoff'
import { LIVE_BROWSER_PROFILES, LiveThumb, browserTabPath, showsLiveThumb } from './LiveThumb'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import { workflowPullRequest, type WorkflowPullRequest } from './stepviews'
import type { Task, Workflow } from './types'
import { workflowName } from './WorkflowFacts'
import './styles/home-worklists.css'

/** Rows drawn before "Show N more". */
export const AGENT_CAP = 6
export const WORKFLOW_CAP = 4
/** A tiny thumbnail asks for a frame about once a second, never faster than `THUMB_INTERVALS`. */
export const HOME_THUMB_PACE_MS = 1000
/** A finished agent stays, faded with a check, this long after it ended. */
export const JUST_FINISHED_MS = 5 * 60_000

const LIVE: ReadonlySet<string> = new Set(['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'])
const WAITING: ReadonlySet<string> = new Set(['QUEUED', 'READY', 'PARKED'])
const ENDED: ReadonlySet<string> = new Set(['SUCCEEDED', 'CANCELLED', 'FAILED', 'DEAD_LETTERED'])
const BAD: ReadonlySet<string> = new Set(['FAILED', 'DEAD_LETTERED'])

/** One held owner question, as Home keys it (`ownerQuestionsOf`). */
export interface AskedQuestion extends HeldQuestion {
  key: string
  holder: Task
}

type OnDone = (key: string, sentence: string) => void

const wfHref = (id: string) => addressToPath(`work/workflows?wf=${encodeURIComponent(id)}`)

/** What the work is: its title on its metadata, else its step, else its profile. */
function nameOf(t: Pick<Task, 'metadata' | 'step_id' | 'runner_profile'>): string {
  const title = t.metadata?.title
  if (typeof title === 'string' && title.trim() !== '') return title
  return t.step_id ?? t.runner_profile
}

function elapsed(iso: string | null | undefined, now: number): string {
  const at = iso ? Date.parse(iso) : NaN
  if (!Number.isFinite(at)) return '—'
  const min = Math.max(0, Math.floor((now - at) / 60_000))
  if (min < 60) return `${min}m`
  return `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m`
}

const startedAt = (t: Task): number => Date.parse(t.started_at ?? t.created_at) || 0
export const hasBrowser = (t: Pick<Task, 'runner_profile'>): boolean => LIVE_BROWSER_PROFILES.has(t.runner_profile)

/** Asking first, then browser agents, then the rest, newest first. */
export function orderAgents(xs: readonly Task[], asks: (t: Task) => boolean): Task[] {
  const rank = (t: Task) => (asks(t) ? 0 : hasBrowser(t) ? 1 : 2)
  return [...xs].sort((a, b) => rank(a) - rank(b) || startedAt(b) - startedAt(a))
}

function KindIcon({ browser }: { browser: boolean }) {
  const tip = browser ? 'Browser agent' : 'No browser'
  return (
    <Tooltip tip={tip}>
      <span className={`wl-kind is-${browser ? 'browser' : 'terminal'}`} aria-label={tip} role="img" tabIndex={0} data-kind={browser ? 'browser' : 'terminal'}>
        {browser ? (
          <svg viewBox="0 0 16 16" aria-hidden>
            <rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
            <path d="M1.5 5.5h13" stroke="currentColor" strokeWidth="1.3" />
            <circle cx="3.7" cy="4" r=".6" fill="currentColor" />
            <circle cx="5.5" cy="4" r=".6" fill="currentColor" />
          </svg>
        ) : (
          <svg viewBox="0 0 16 16" aria-hidden>
            <rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
            <path d="M4.5 6.5l2 1.75-2 1.75M8 10.5h3.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </span>
    </Tooltip>
  )
}

/** The state mark: a dot whose word is its tooltip. */
function Dot({ t, why }: { t: Task; why: (t: Task) => string }) {
  const tone = LIVE.has(t.state) ? (t.state === 'RUNNING' ? 'running' : 'starting') : WAITING.has(t.state) ? 'parked' : BAD.has(t.state) ? 'failed' : 'succeeded'
  const word =
    t.state === 'RUNNING' ? 'Running' : LIVE.has(t.state) ? 'Starting' : WAITING.has(t.state) ? `Waiting for ${why(t)}` : t.state === 'SUCCEEDED' ? 'Finished' : t.state.toLowerCase()
  return (
    <Tooltip tip={word}>
      <span className={`wl-dot is-${tone}`} role="img" aria-label={word} tabIndex={0} />
    </Tooltip>
  )
}

/**
 * THE TINY THUMBNAIL: the live view at 60x38, paced at about a second;
 * pressing it opens the Browser tab, and hovering or focusing it offers both
 * ways in. No larger picture: it stays the only live view on Home.
 */
function TinyThumb({ t }: { t: Task }) {
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const later = (to: boolean, ms: number) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(to), ms)
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  return (
    <span className="wl-thumbhost" onMouseEnter={() => later(true, 250)} onMouseLeave={() => later(false, 150)} onFocus={() => later(true, 0)} onBlur={() => later(false, 150)}>
      <a className="wl-thumb" href={browserTabPath(t.id)} aria-label={`Live browser of ${nameOf(t)}`}>
        <LiveThumb taskId={t.id} workflowId={t.workflow_id} paceMs={HOME_THUMB_PACE_MS} />
      </a>
      {open && (
        <span className="wl-pop" aria-label={nameOf(t)}>
          <ButtonLink kind="primary" size="sm" href={browserTabPath(t.id)}>
            Watch live
          </ButtonLink>
          <ButtonLink kind="secondary" size="sm" href={agentPath(t)}>
            Agent page
          </ButtonLink>
        </span>
      )}
    </span>
  )
}

/** The answer, posted to the asking task as Needs you posts it; the question then leaves both places. */
function AnswerPanel({ q, onDone, onClose }: { q: AskedQuestion; onDone: OnDone; onClose: () => void }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const send = async () => {
    const answer = text.trim()
    if (busy || answer === '') return
    setBusy(true)
    setRefused(null)
    const r = await answerQuestion(q.task_id, { question_index: q.question_index, choice: null, text: answer })
    setBusy(false)
    if (r.status === 'error') return setRefused(r.error)
    onDone(`q:${q.key}`, `Answer sent to ${nameOf(q.holder)}.`)
  }
  return (
    <div className="wl-answer">
      <p className="wl-q">{q.question ?? 'A question waits for your answer.'}</p>
      <div className="wl-answer-row">
        <input className="c-inp wl-answer-input" maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} aria-label="Your answer" />
        <Button kind="primary" size="sm" disabled={text.trim() === '' || busy} busy={busy ? 'Sending…' : false} onClick={() => void send()}>
          Send
        </Button>
        <Button kind="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
      </div>
      {refused !== null && (
        <p className="wl-refused" role="alert">
          Not stored: {errorHeading(refused)}
        </p>
      )}
    </div>
  )
}

interface Ctx {
  now: number
  why: (t: Task) => string
  onDone: OnDone
  wfNames: ReadonlyMap<string, string>
}

function AgentRow({ t, q, ctx }: { t: Task; q: AskedQuestion | null; ctx: Ctx }) {
  const [answering, setAnswering] = useState(false)
  const done = ENDED.has(t.state)
  const wfName = t.workflow_id === null ? null : (ctx.wfNames.get(t.workflow_id) ?? t.workflow_id)
  const sub =
    q !== null ? (
      answering ? (
        <span className="wl-waits">
          <b>Asks you</b>
        </span>
      ) : (
        <span className="wl-waits">
          <b>Asks you</b> · {q.question ?? 'a question'}
        </span>
      )
    ) : t.workflow_id !== null ? (
      <a className="wl-wflink" href={wfHref(t.workflow_id)}>
        {wfName}
      </a>
    ) : null
  return (
    <li className={`wl-row wl-agent${q !== null ? ' is-asks' : ''}${done ? ' is-done' : ''}`} data-task={t.id}>
      <KindIcon browser={hasBrowser(t)} />
      <div className="wl-main">
        <span className="wl-line">
          <a className="wl-name" href={agentPath(t)}>
            {nameOf(t)}
          </a>
          <span className="wl-meta">
            {done ? (
              <span className={`wl-ok${BAD.has(t.state) ? ' is-bad' : ''}`} role="img" aria-label={t.state === 'SUCCEEDED' ? 'Finished' : t.state.toLowerCase()}>
                {BAD.has(t.state) ? '✕' : '✓'}
              </span>
            ) : (
              <Dot t={t} why={ctx.why} />
            )}
            <span className="wl-time">{done ? `${elapsed(t.completed_at ?? t.updated_at, ctx.now)} ago` : elapsed(t.started_at ?? t.created_at, ctx.now)}</span>
          </span>
        </span>
        {sub !== null && <span className="wl-sub">{sub}</span>}
      </div>
      <div className="wl-end">
        {q !== null && !answering ? (
          <Button kind="primary" size="sm" onClick={() => setAnswering(true)}>
            Answer
          </Button>
        ) : showsLiveThumb(t.runner_profile, t.state, t.id) ? (
          <TinyThumb t={t} />
        ) : null}
      </div>
      {q !== null && answering && <AnswerPanel q={q} onDone={ctx.onDone} onClose={() => setAnswering(false)} />}
    </li>
  )
}

// ---------------------------------------------------------------- workflows

type StepMark = 'done' | 'now' | 'todo' | 'bad'

/** One workflow row's facts: its steps in order, each with the task the read served for it. */
export interface WorkRow {
  id: string
  name: string
  steps: { id: string; mark: StepMark; task: Task | null }[]
  pr: WorkflowPullRequest | null
  since: string | null
}

function markOf(task: Task | null): StepMark {
  if (task === null) return 'todo'
  if (LIVE.has(task.state) || WAITING.has(task.state)) return 'now'
  if (BAD.has(task.state)) return 'bad'
  return 'done'
}

/** The row from the workflow read; from the person's own step tasks when the read did not serve it. */
export function workRow(id: string, read: WorkflowRead | null, mine: readonly Task[]): WorkRow {
  const own = mine.filter((t) => t.workflow_id === id)
  if (read !== null) {
    const byId = new Map<string, Task>([...own, ...read.tasks].map((t) => [t.id, t]))
    const wf: Workflow = read.workflow
    return {
      id,
      name: workflowName(wf, byId) ?? nameOf(own[0] ?? { metadata: {}, step_id: null, runner_profile: id }),
      steps: wf.steps.map((s) => {
        const task = s.task_id ? (byId.get(s.task_id) ?? null) : null
        return { id: s.step_id, mark: markOf(task), task }
      }),
      pr: workflowPullRequest(wf, byId),
      since: wf.created_at,
    }
  }
  const sorted = [...own].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))
  return {
    id,
    name: sorted[0] === undefined ? id : nameOf(sorted[0]),
    steps: sorted.map((t) => ({ id: t.step_id ?? t.id, mark: markOf(t), task: t })),
    pr: null,
    since: sorted[0]?.created_at ?? null,
  }
}

function PrChip({ pr }: { pr: WorkflowPullRequest | null }) {
  if (pr === null) return null
  const body = (
    <>
      <svg viewBox="0 0 16 16" aria-hidden>
        <circle cx="4" cy="3.5" r="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <circle cx="4" cy="12.5" r="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <circle cx="12" cy="12.5" r="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M4 5.1v5.8M12 10.9V6.5a2 2 0 0 0-2-2H7.5m1.6-1.6L7.5 4.5l1.6 1.6" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      </svg>
      #{pr.number}
    </>
  )
  return (
    <Tooltip tip={`Pull request #${pr.number}`}>
      {pr.href === null ? (
        <span className="wl-pr" tabIndex={0}>
          {body}
        </span>
      ) : (
        <a className="wl-pr" href={pr.href} target="_blank" rel="noreferrer">
          {body}
        </a>
      )}
    </Tooltip>
  )
}

function StepBar({ w, asking }: { w: WorkRow; asking: number }) {
  const now = w.steps.filter((s) => s.mark === 'now')
  const label = now.length === 0 ? (w.steps.some((s) => s.mark === 'todo') ? 'Next' : 'Done') : now.length > 1 ? `${now[0]!.id} +${now.length - 1}` : now[0]!.id
  const at = w.steps.findIndex((s) => s.mark === 'now')
  return (
    <span className="wl-stages">
      <Tooltip tip={w.steps.map((s) => `${s.mark === 'done' ? '✓' : s.mark === 'now' ? '●' : s.mark === 'bad' ? '✕' : '○'} ${s.id}`).join('   ')}>
        <span className="wl-segs" role="img" tabIndex={0} aria-label={`Step ${at + 1} of ${w.steps.length}: ${label}`}>
          {w.steps.map((s) => (
            <i key={s.id} className={`is-${s.mark}${s.mark === 'now' && asking > 0 ? ' is-asks' : ''}`} />
          ))}
        </span>
      </Tooltip>
      <span className="wl-stage-name">{label}</span>
      {asking > 0 && <span className="wl-stage-name is-warn">{asking} asks you</span>}
    </span>
  )
}

function StepAgent({ t, asks, why }: { t: Task; asks: boolean; why: (t: Task) => string }) {
  return (
    <li className={`wl-sa${asks ? ' is-asks' : ''}`}>
      <KindIcon browser={hasBrowser(t)} />
      <span className="wl-sa-main">
        <a className="wl-sa-name" href={agentPath(t)}>
          {nameOf(t)}
        </a>
      </span>
      {asks ? <b className="wl-sa-asks">Asks you</b> : ENDED.has(t.state) ? null : <Dot t={t} why={why} />}
    </li>
  )
}

function WorkflowRow({ w, asksOf, ctx }: { w: WorkRow; asksOf: (t: Task) => boolean; ctx: Ctx }) {
  const [open, setOpen] = useState(false)
  const asking = w.steps.filter((s) => s.task !== null && asksOf(s.task)).length
  return (
    <li className={`wl-row wl-wf${open ? ' is-open' : ''}`} data-workflow={w.id}>
      <button type="button" className="wl-disclose" aria-expanded={open} aria-label={`${open ? 'Close' : 'Open'} ${w.name}`} onClick={() => setOpen((o) => !o)}>
        <svg viewBox="0 0 16 16" aria-hidden>
          <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <div className="wl-main">
        <span className="wl-line">
          <a className="wl-name" href={wfHref(w.id)}>
            {w.name}
          </a>
          <span className="wl-meta">
            <PrChip pr={w.pr} />
          </span>
        </span>
        <span className="wl-line">
          <StepBar w={w} asking={asking} />
          <span className="wl-time">{elapsed(w.since, ctx.now)}</span>
        </span>
      </div>
      {open && (
        <div className="wl-wfopen">
          <ol className="wl-stagelist">
            {w.steps.map((s) => (
              <li key={s.id} className={`is-${s.mark}`}>
                <span className="wl-stage-mark" aria-hidden>
                  {s.mark === 'done' && (
                    <svg viewBox="0 0 12 12">
                      <path d="M3 6.2l2 2 4-4.4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                </span>
                <span className="wl-stage-title">{s.id}</span>
                {s.task !== null && s.mark !== 'todo' && (
                  <ul className="wl-inner">
                    <StepAgent t={s.task} asks={asksOf(s.task)} why={ctx.why} />
                  </ul>
                )}
              </li>
            ))}
          </ol>
          <div className="wl-wfacts">
            <ButtonLink kind="secondary" size="sm" href={wfHref(w.id)}>
              Open workflow
            </ButtonLink>
            {w.pr !== null && w.pr.href !== null && (
              <ButtonLink kind="ghost" size="sm" href={w.pr.href} target="_blank" rel="noreferrer">
                Pull request #{w.pr.number}
              </ButtonLink>
            )}
          </div>
        </div>
      )}
    </li>
  )
}

function ListHead({ title, n, browser }: { title: string; n: number; browser?: number }) {
  return (
    <h3 className="wl-lhead">
      {title}
      <em className="wl-n">{n}</em>
      {browser !== undefined && browser > 0 && (
        <Tooltip tip={`${browser} with a browser`}>
          <span className="wl-n-browser" tabIndex={0} aria-label={`${browser} with a browser`}>
            <svg viewBox="0 0 16 16" aria-hidden className="wl-glyph">
              <rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
              <path d="M1.5 5.5h13" stroke="currentColor" strokeWidth="1.3" />
            </svg>{' '}
            {browser}
          </span>
        </Tooltip>
      )}
    </h3>
  )
}

function More({ n, what, open, onToggle }: { n: number; what: string; open: boolean; onToggle: () => void }) {
  return (
    <Button kind="ghost" size="sm" className="wl-more" aria-expanded={open} onClick={onToggle}>
      {open ? `Show fewer ${what}` : `Show ${n} more ${what}`}
    </Button>
  )
}

/** The workflows of the work in progress, read by id once per Home poll; null until read or when it failed. */
function useWorkflowReads(ids: readonly string[], round: number): ReadonlyMap<string, WorkflowRead> | null {
  const key = ids.join(',')
  const [reads, setReads] = useState<ReadonlyMap<string, WorkflowRead> | null>(null)
  useEffect(() => {
    if (key === '') {
      setReads(new Map())
      return
    }
    let live = true
    void loadWorkflowsById(key.split(',').slice(0, WORKFLOW_IDS_PER_READ)).then((r) => {
      if (!live) return
      setReads(r.status === 'ok' || r.status === 'stale' ? r.data.reads : null)
    })
    return () => {
      live = false
    }
  }, [key, round])
  return reads
}

// ---------------------------------------------------------------- the card

export function WorkLists({
  mine,
  questions,
  now,
  round,
  why,
  onDone,
  head,
}: {
  /** The person's newest tasks, as Home read them; null while not read. */
  mine: readonly Task[] | null
  /** The owner questions still open (Home's `ownerQuestionsOf`, less what was answered here). */
  questions: readonly AskedQuestion[] | null
  now: number
  round: number
  why: (t: Task) => string
  onDone: OnDone
  head: (count: number | null, action: ReactNode) => ReactNode
}) {
  const [allAgents, setAllAgents] = useState(false)
  const [allWf, setAllWf] = useState(false)
  const qOf = useMemo(() => {
    const m = new Map<string, AskedQuestion>()
    for (const q of questions ?? []) if (!m.has(q.holder.id)) m.set(q.holder.id, q)
    return m
  }, [questions])
  const asks = (t: Task) => qOf.has(t.id)
  const going = (mine ?? []).filter((t) => LIVE.has(t.state) || WAITING.has(t.state))
  const finished = (mine ?? [])
    .filter((t) => ENDED.has(t.state) && now - (Date.parse(t.completed_at ?? t.updated_at) || 0) <= JUST_FINISHED_MS)
    .sort((a, b) => Date.parse(b.completed_at ?? b.updated_at) - Date.parse(a.completed_at ?? a.updated_at))
  const sorted = orderAgents(going, asks)
  const shownAgents = allAgents ? sorted : sorted.slice(0, AGENT_CAP)
  const wfIds = [...new Set(going.map((t) => t.workflow_id).filter((x): x is string => x !== null))].sort()
  const reads = useWorkflowReads(wfIds, round)
  const rows = wfIds.map((id) => workRow(id, reads?.get(id) ?? null, mine ?? []))
  const asking = (w: WorkRow) => w.steps.some((s) => s.task !== null && asks(s.task))
  const sortedWf = [...rows].sort((a, b) => Number(asking(b)) - Number(asking(a)) || (Date.parse(b.since ?? '') || 0) - (Date.parse(a.since ?? '') || 0))
  const shownWf = allWf ? sortedWf : sortedWf.slice(0, WORKFLOW_CAP)
  const ctx: Ctx = { now, why, onDone, wfNames: new Map(rows.map((w) => [w.id, w.name])) }
  const nothing = mine !== null && going.length === 0
  return (
    <Card id="home-work" className="wl-card">
      {head(
        mine === null ? null : going.length,
        <ButtonLink kind="ghost" size="sm" href={addressToPath('work/running/live')}>
          All running
        </ButtonLink>,
      )}
      {mine === null ? (
        <p className="wl-none">— not read yet</p>
      ) : nothing ? (
        <div className="wl-empty">
          <span className="wl-empty-line">Nothing running.</span>
          <ButtonLink kind="primary" size="sm" href={addressToPath('work/new')}>
            Start an agent
          </ButtonLink>
          <ButtonLink kind="secondary" size="sm" href={addressToPath('work/new-workflow')}>
            Start a workflow
          </ButtonLink>
          {finished.length > 0 && (
            <ul className="wl-list wl-justdone">
              {finished.map((t) => (
                <AgentRow key={t.id} t={t} q={null} ctx={ctx} />
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="wl-cols">
          <section className="wl-col" aria-label="Agents">
            <ListHead title="Agents" n={going.length} browser={going.filter(hasBrowser).length} />
            <ul className="wl-list">
              {shownAgents.map((t) => (
                <AgentRow key={t.id} t={t} q={qOf.get(t.id) ?? null} ctx={ctx} />
              ))}
              {finished.map((t) => (
                <AgentRow key={t.id} t={t} q={null} ctx={ctx} />
              ))}
            </ul>
            {sorted.length > AGENT_CAP && <More n={sorted.length - AGENT_CAP} what="agents" open={allAgents} onToggle={() => setAllAgents((o) => !o)} />}
          </section>
          <section className="wl-col" aria-label="Workflows">
            <ListHead title="Workflows" n={rows.length} />
            {rows.length === 0 ? (
              <p className="wl-none">None in progress.</p>
            ) : (
              <ul className="wl-list">
                {shownWf.map((w) => (
                  <WorkflowRow key={w.id} w={w} asksOf={asks} ctx={ctx} />
                ))}
              </ul>
            )}
            {sortedWf.length > WORKFLOW_CAP && <More n={sortedWf.length - WORKFLOW_CAP} what="workflows" open={allWf} onToggle={() => setAllWf((o) => !o)} />}
          </section>
        </div>
      )}
    </Card>
  )
}
