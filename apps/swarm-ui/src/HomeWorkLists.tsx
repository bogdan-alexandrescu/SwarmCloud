// MOCK-HOOK FILE
/**
 * MOCK-UP (the owner's ask of 2026-11-11): Home's Your work as TWO LISTS,
 * Agents and Workflows, at `?homework=lists`. Fixture data only, invented;
 * `?hwdata=empty|many` picks the other fixture sets. Not wired to any read.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { ApprovalItem } from './api'
import { Button, ButtonLink, Card, Tooltip } from './components'
import { browserTabPath } from './LiveThumb'
import { addressToPath } from './paths'
import type { Stats, Task } from './types'
import './styles/home-worklists.css'

/** Read once at load: the router rewrites the address and drops the query. */
const Q = new URLSearchParams(window.location.search)

type Kind = 'browser' | 'terminal'
type AState = 'RUNNING' | 'STARTING' | 'PARKED' | 'SUCCEEDED'
type Page = 'form' | 'map' | 'dash' | 'docs'

interface Question {
  text: string
  options: string[]
}

interface MockAgent {
  id: string
  name: string
  kind: Kind
  state: AState
  /** Minutes since it started, or since it finished for a finished one. */
  min: number
  wf?: { id: string; name: string; step: string }
  question?: Question
  page?: Page
}

type StageState = 'done' | 'now' | 'todo'
interface MockWorkflow {
  id: string
  name: string
  stages: { name: string; state: StageState; agents?: string[] }[]
  /** Set only where the workflow asks the person for something. */
  asks?: { word: string; button: string }
  pr: { n: number; state: 'open' | 'draft'; checks: 'pass' | 'run' | 'fail' } | null
  min: number
}

const PR_BASE = 'https://git.example.com/orchard/ledger/pull/'

// ------------------------------------------------------------------ fixtures

const A_RATE = { id: 'wf_7c21', name: 'Export API rate limits', step: '' }
const REST_AGENTS: MockAgent[] = [
  { id: 'task_a1f3e902', name: 'Pick the default limit', kind: 'terminal', state: 'PARKED', min: 18, wf: { ...A_RATE, step: 'decide-limit' }, question: { text: 'Default limit per key: 600 or 1,200 requests a minute?', options: ['600', '1,200'] } },
  { id: 'task_b2c4d811', name: 'Check limits in the dashboard', kind: 'browser', state: 'RUNNING', min: 12, wf: { ...A_RATE, step: 'qa-dashboard' }, page: 'dash' },
  { id: 'task_c3e5f720', name: 'Reproduce map tile glitch', kind: 'browser', state: 'RUNNING', min: 4, page: 'map' },
  { id: 'task_d4f6a633', name: 'Implement rate-limit headers', kind: 'terminal', state: 'RUNNING', min: 26, wf: { ...A_RATE, step: 'implement-1' } },
  { id: 'task_e5a7b544', name: 'Document the new limits', kind: 'terminal', state: 'STARTING', min: 1, wf: { ...A_RATE, step: 'implement-2' } },
]
const REST_FINISHED: MockAgent = { id: 'task_f6b8c455', name: 'Bump lint rules', kind: 'terminal', state: 'SUCCEEDED', min: 1 }

const REST_WORKFLOWS: MockWorkflow[] = [
  {
    id: 'wf_7c21',
    name: 'Export API rate limits',
    stages: [
      { name: 'Plan', state: 'done' },
      { name: 'Build', state: 'now', agents: ['task_a1f3e902', 'task_b2c4d811', 'task_d4f6a633', 'task_e5a7b544'] },
      { name: 'Review', state: 'todo' },
      { name: 'Merge', state: 'todo' },
    ],
    pr: { n: 488, state: 'draft', checks: 'run' },
    min: 41,
  },
  {
    id: 'wf_5a90',
    name: 'Nightly report cleanup',
    stages: [
      { name: 'Plan', state: 'done' },
      { name: 'Build', state: 'done' },
      { name: 'Review', state: 'now' },
      { name: 'Merge', state: 'todo' },
    ],
    asks: { word: 'Your review', button: 'Review' },
    pr: { n: 479, state: 'open', checks: 'pass' },
    min: 95,
  },
  {
    id: 'wf_3e18',
    name: 'Signup form copy',
    stages: [
      { name: 'Plan', state: 'done' },
      { name: 'Build', state: 'done' },
      { name: 'Review', state: 'done' },
      { name: 'Merge', state: 'now' },
    ],
    pr: { n: 482, state: 'open', checks: 'run' },
    min: 63,
  },
]

const MANY_NAMES: [string, Kind, Page | undefined][] = [
  ['Check checkout on a phone', 'browser', 'form'],
  ['Walk the onboarding tour', 'browser', 'docs'],
  ['Port invoice totals to cents', 'terminal', undefined],
  ['Split the search module', 'terminal', undefined],
  ['Translate settings strings', 'terminal', undefined],
  ['Trim unused feature flags', 'terminal', undefined],
  ['Upgrade the chart library', 'terminal', undefined],
]
const MANY_AGENTS: MockAgent[] = [
  ...REST_AGENTS,
  ...MANY_NAMES.map(([name, kind, page], i): MockAgent => ({
    id: `task_${(0x9a10 + i * 7919).toString(16)}e0${i}`,
    name,
    kind,
    page,
    state: 'RUNNING',
    min: 6 + i * 9,
    wf: i % 3 === 2 ? { id: `wf_m${i}`, name: ({ 2: 'Billing in cents', 5: 'Search split', 8: 'Flags cleanup' } as Record<number, string>)[i] ?? 'Billing in cents', step: `implement-${i}` } : undefined,
  })),
]
const MANY_WORKFLOWS: MockWorkflow[] = [
  ...REST_WORKFLOWS,
  { id: 'wf_m2', name: 'Billing in cents', stages: [{ name: 'Plan', state: 'done' }, { name: 'Build', state: 'now' }, { name: 'Review', state: 'todo' }, { name: 'Merge', state: 'todo' }], pr: { n: 491, state: 'draft', checks: 'run' }, min: 22 },
  { id: 'wf_m5', name: 'Search split', stages: [{ name: 'Plan', state: 'now' }, { name: 'Build', state: 'todo' }, { name: 'Review', state: 'todo' }, { name: 'Merge', state: 'todo' }], pr: null, min: 8 },
  { id: 'wf_m8', name: 'Flags cleanup', stages: [{ name: 'Plan', state: 'done' }, { name: 'Build', state: 'done' }, { name: 'Review', state: 'now' }, { name: 'Merge', state: 'todo' }], pr: { n: 476, state: 'open', checks: 'fail' }, min: 130 },
]

function fixtures(): { agents: MockAgent[]; finished: MockAgent[]; workflows: MockWorkflow[] } {
  const set = Q.get('hwdata')
  if (set === 'empty') return { agents: [], finished: [REST_FINISHED], workflows: [] }
  if (set === 'many') return { agents: MANY_AGENTS, finished: [REST_FINISHED], workflows: MANY_WORKFLOWS }
  return { agents: REST_AGENTS, finished: [REST_FINISHED], workflows: REST_WORKFLOWS }
}

/** Opened from the URL so a frame can be rendered in that state: `hwopen=<id>`. */
const startOpen = (): string | null => Q.get('hwopen')

// ------------------------------------------------------------------ bits

const AGENT_CAP = 6
const WORKFLOW_CAP = 4

const agentHref = (id: string, done = false) => addressToPath(`work/task/${encodeURIComponent(id)}`, done ? 'recent' : 'live')
const wfHref = (id: string) => addressToPath(`work/workflows?wf=${encodeURIComponent(id)}`)

function elapsed(min: number): string {
  if (min < 60) return `${min}m`
  return `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m`
}

/** Needs-you first, then browser agents, then the newest. */
function order(xs: MockAgent[]): MockAgent[] {
  const rank = (a: MockAgent) => (a.question ? 0 : a.kind === 'browser' ? 1 : 2)
  return [...xs].sort((a, b) => rank(a) - rank(b) || a.min - b.min)
}

export function KindIcon({ kind }: { kind: Kind }) {
  const tip = kind === 'browser' ? 'Browser agent' : 'No browser'
  return (
    <Tooltip tip={tip}>
      <span className={`wl-kind is-${kind}`} aria-label={tip} role="img" tabIndex={0}>
        {kind === 'browser' ? (
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

/** The state mark: a dot whose word is a tooltip; a word on the surface only where it asks for something. */
function Dot({ state }: { state: AState }) {
  const word = state === 'RUNNING' ? 'Running' : state === 'STARTING' ? 'Starting' : state === 'PARKED' ? 'Parked' : 'Finished'
  return (
    <Tooltip tip={word}>
      <span className={`wl-dot is-${state.toLowerCase()}`} role="img" aria-label={word} tabIndex={0} />
    </Tooltip>
  )
}

/** A fake page, redrawn once a second as the tiny live thumbnail would be. */
function FakePage({ page, tick }: { page: Page; tick: number }) {
  const t = tick % 6
  const cx = [18, 34, 50, 42, 26, 58][t]
  const cy = [14, 20, 28, 34, 24, 16][t]
  return (
    <svg className="wl-page" viewBox="0 0 80 50" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <rect width="80" height="50" fill="#f6f8fb" />
      <rect width="80" height="6" fill="#dfe6ef" />
      {page === 'map' && (
        <>
          <rect y="6" width="80" height="44" fill="#d8ecd9" />
          <path d={`M0 ${30 - t} C 20 ${18 + t}, 44 ${40 - t}, 80 ${22 + t}`} stroke="#f2c14e" strokeWidth="3" fill="none" />
          <path d="M30 6 L 38 50 M 0 18 L 80 26" stroke="#ffffff" strokeWidth="2" />
          <rect x={46 + (t % 3) * 4} y={14 + (t % 2) * 6} width="12" height="10" fill="#c9d3de" opacity=".9" />
        </>
      )}
      {page === 'dash' && (
        <>
          <rect x="4" y="10" width="22" height="10" rx="1" fill="#e3e9f2" />
          <rect x="29" y="10" width="22" height="10" rx="1" fill="#e3e9f2" />
          <rect x="54" y="10" width="22" height="10" rx="1" fill="#e3e9f2" />
          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => {
            const h = 6 + ((i * 5 + t * 3) % 16)
            return <rect key={i} x={6 + i * 9} y={46 - h} width="6" height={h} fill="#3b82c4" opacity=".8" />
          })}
        </>
      )}
      {page === 'form' && (
        <>
          <rect x="20" y="11" width="40" height="5" rx="1" fill="#c5d0dc" />
          {[0, 1, 2].map((i) => (
            <rect key={i} x="20" y={20 + i * 8} width="40" height="5" rx="1" fill="#fff" stroke="#c5d0dc" strokeWidth=".6" />
          ))}
          <rect x="20" y="20" width={Math.min(40, 6 + t * 7)} height="5" rx="1" fill="#e8f0fb" />
          <rect x="44" y="44" width="16" height="4" rx="1" fill="#3b82c4" />
        </>
      )}
      {page === 'docs' && (
        <>
          <rect x="4" y="10" width="16" height="36" fill="#e3e9f2" />
          {[0, 1, 2, 3, 4].map((i) => (
            <rect key={i} x="24" y={11 + i * 7 - (t % 3)} width={48 - ((i * 11) % 18)} height="3" rx="1" fill="#c5d0dc" />
          ))}
        </>
      )}
      <path d={`M${cx} ${cy} l0 7 l2 -2 l2.4 3.4 l1 -.7 l-2.3 -3.3 l3 -.3 z`} fill="#111" stroke="#fff" strokeWidth=".5" />
    </svg>
  )
}

/** One tick a second, shared by every thumbnail. */
function useTick(): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [])
  return tick
}

/**
 * THE TINY THUMBNAIL: pressing it opens the live browser; hovering or
 * focusing it opens a small menu with both ways in. No larger picture: the
 * tiny thumbnail stays the only live view on Home.
 */
function TinyThumb({ a, tick }: { a: MockAgent; tick: number }) {
  const [open, setOpen] = useState(() => startOpen() === `thumb:${a.id}`)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const show = () => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(true), 250)
  }
  const hide = () => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(false), 150)
  }
  return (
    <span className="wl-thumbhost" onMouseEnter={show} onMouseLeave={hide} onFocus={() => setOpen(true)} onBlur={hide}>
      <a className="wl-thumb" href={browserTabPath(a.id)} aria-label={`Live browser of ${a.name}`}>
        <FakePage page={a.page ?? 'docs'} tick={tick} />
        <i className="wl-live" aria-hidden />
      </a>
      {open && (
        <span className="wl-pop" role="menu" aria-label={a.name}>
          <ButtonLink kind="primary" size="sm" href={browserTabPath(a.id)} role="menuitem">
            Watch live
          </ButtonLink>
          <ButtonLink kind="secondary" size="sm" href={agentHref(a.id)} role="menuitem">
            Agent page
          </ButtonLink>
        </span>
      )}
    </span>
  )
}

function AnswerPanel({ a, onClose }: { a: MockAgent; onClose: () => void }) {
  const [sent, setSent] = useState<string | null>(null)
  const [text, setText] = useState('')
  const q = a.question!
  if (sent !== null)
    return (
      <div className="wl-answer is-sent" role="status">
        <span className="wl-ok" aria-hidden>
          ✓
        </span>{' '}
        Sent “{sent}”
      </div>
    )
  return (
    <div className="wl-answer">
      <p className="wl-q">{q.text}</p>
      <div className="wl-answer-row">
        {q.options.map((o) => (
          <Button key={o} kind="secondary" size="sm" onClick={() => setSent(o)}>
            {o}
          </Button>
        ))}
        <input className="c-inp wl-answer-input" placeholder="Or write an answer" value={text} onChange={(e) => setText(e.target.value)} aria-label="Your answer" />
        <Button kind="primary" size="sm" disabled={text.trim() === ''} onClick={() => setSent(text.trim())}>
          Send
        </Button>
        <Button kind="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </div>
  )
}

function PrChip({ pr }: { pr: MockWorkflow['pr'] }) {
  if (pr === null) return <span className="wl-pr is-none" aria-label="No pull request yet">—</span>
  const checks = pr.checks === 'pass' ? 'checks passed' : pr.checks === 'run' ? 'checks running' : 'checks failed'
  return (
    <Tooltip tip={`${pr.state === 'draft' ? 'Draft' : 'Open'} pull request · ${checks}`}>
      <a className={`wl-pr is-${pr.state} ck-${pr.checks}`} href={`${PR_BASE}${pr.n}`} target="_blank" rel="noreferrer">
        <svg viewBox="0 0 16 16" aria-hidden>
          <circle cx="4" cy="3.5" r="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
          <circle cx="4" cy="12.5" r="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
          <circle cx="12" cy="12.5" r="1.6" fill="none" stroke="currentColor" strokeWidth="1.3" />
          <path d="M4 5.1v5.8M12 10.9V6.5a2 2 0 0 0-2-2H7.5m1.6-1.6L7.5 4.5l1.6 1.6" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
        #{pr.n}
        <i className="wl-ck" aria-hidden />
      </a>
    </Tooltip>
  )
}

function AgentRow({ a, tick }: { a: MockAgent; tick: number }) {
  const [answering, setAnswering] = useState(() => startOpen() === `answer:${a.id}`)
  const done = a.state === 'SUCCEEDED'
  const sub = a.question && answering ? (
    <span className="wl-waits">
      <b>Asks you</b>
    </span>
  ) : a.question ? (
    <Tooltip tip={a.question.text}>
      <span className="wl-waits" tabIndex={0}>
        <b>Asks you</b> · {a.question.text}
      </span>
    </Tooltip>
  ) : a.wf ? (
    <a className="wl-wflink" href={wfHref(a.wf.id)}>
      {a.wf.name}
    </a>
  ) : null
  return (
    <li className={`wl-row wl-agent${a.question ? ' is-asks' : ''}${done ? ' is-done' : ''}`}>
      <KindIcon kind={a.kind} />
      <div className="wl-main">
        <span className="wl-line">
          <a className="wl-name" href={agentHref(a.id, done)}>
            {a.name}
          </a>
          <span className="wl-meta">
            {done ? (
              <span className="wl-ok" role="img" aria-label="Finished">
                ✓
              </span>
            ) : (
              <Dot state={a.state} />
            )}
            <span className="wl-time">{done ? `${elapsed(a.min)} ago` : elapsed(a.min)}</span>
          </span>
        </span>
        {sub !== null && <span className="wl-sub">{sub}</span>}
      </div>
      <div className="wl-end">
        {a.question && !answering ? (
          <Button kind="primary" size="sm" onClick={() => setAnswering(true)}>
            Answer
          </Button>
        ) : a.kind === 'browser' && !done ? (
          <TinyThumb a={a} tick={tick} />
        ) : null}
      </div>
      {answering && <AnswerPanel a={a} onClose={() => setAnswering(false)} />}
    </li>
  )
}

function StageBar({ w, asking }: { w: MockWorkflow; asking: number }) {
  const at = w.stages.findIndex((s) => s.state === 'now')
  const now = at >= 0 ? (w.stages[at] ?? null) : null
  const label = w.asks ? w.asks.word : now === null ? 'Done' : now.agents ? `${now.name} ×${now.agents.length}` : now.name
  return (
    <span className="wl-stages">
      <Tooltip tip={w.stages.map((s) => `${s.state === 'done' ? '✓' : s.state === 'now' ? '●' : '○'} ${s.name}`).join('   ')}>
        <span className="wl-segs" role="img" tabIndex={0} aria-label={`Stage ${at + 1} of ${w.stages.length}: ${now?.name ?? 'done'}`}>
          {w.stages.map((s) => (
            <i key={s.name} className={`is-${s.state}${s.state === 'now' && (w.asks || asking > 0) ? ' is-asks' : ''}`} />
          ))}
        </span>
      </Tooltip>
      <span className={`wl-stage-name${w.asks ? ' is-warn' : ''}`}>{label}</span>
      {asking > 0 && <span className="wl-stage-name is-warn">{asking} asks you</span>}
    </span>
  )
}

/**
 * AN AGENT INSIDE AN OPENED WORKFLOW: a reference, not a second copy of its
 * Agents row. Its step, its name (wrapping, never cut), its glyph and state;
 * no time, thumbnail or Answer here: those stay on its one row in Agents.
 */
function StageAgent({ a }: { a: MockAgent }) {
  return (
    <li className={`wl-sa${a.question ? ' is-asks' : ''}`}>
      <KindIcon kind={a.kind} />
      <span className="wl-sa-main">
        <a className="wl-sa-name" href={agentHref(a.id)}>
          {a.name}
        </a>
        <span className="wl-step">{a.wf?.step}</span>
      </span>
      {a.question ? <b className="wl-sa-asks">Asks you</b> : <Dot state={a.state} />}
    </li>
  )
}

function WorkflowRow({ w, agents }: { w: MockWorkflow; agents: MockAgent[] }) {
  const [open, setOpen] = useState(() => startOpen() === `wf:${w.id}`)
  const mine = agents.filter((a) => a.wf?.id === w.id)
  const asking = mine.filter((a) => a.question).length
  return (
    <li className={`wl-row wl-wf${open ? ' is-open' : ''}${w.asks ? ' is-asks' : ''}`}>
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
          <StageBar w={w} asking={asking} />
          <span className="wl-time">{elapsed(w.min)}</span>
        </span>
      </div>
      <div className="wl-end">
        {w.asks && w.pr !== null && (
          <ButtonLink kind="primary" size="sm" href={`${PR_BASE}${w.pr.n}`} target="_blank" rel="noreferrer">
            {w.asks.button}
          </ButtonLink>
        )}
      </div>
      {open && (
        <div className="wl-wfopen">
          <ol className="wl-stagelist">
            {w.stages.map((s) => {
              const inStage = s.agents ? mine.filter((a) => s.agents!.includes(a.id)) : []
              return (
                <li key={s.name} className={`is-${s.state}`}>
                  <span className="wl-stage-mark" aria-hidden>
                    {s.state === 'done' && (
                      <svg viewBox="0 0 12 12">
                        <path d="M3 6.2l2 2 4-4.4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    )}
                  </span>
                  <span className="wl-stage-title">{s.name}</span>
                  {inStage.length > 0 && (
                    <ul className="wl-inner">
                      {order(inStage).map((a) => (
                        <StageAgent key={a.id} a={a} />
                      ))}
                    </ul>
                  )}
                </li>
              )
            })}
          </ol>
          <div className="wl-wfacts">
            <ButtonLink kind="secondary" size="sm" href={wfHref(w.id)}>
              Open workflow
            </ButtonLink>
            {w.pr !== null && (
              <ButtonLink kind="ghost" size="sm" href={`${PR_BASE}${w.pr.n}`} target="_blank" rel="noreferrer">
                Pull request #{w.pr.n}
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
        <Tooltip tip={`${browser} with a live browser`}>
          <span className="wl-n-browser" tabIndex={0} aria-label={`${browser} with a live browser`}>
            <KindGlyph /> {browser}
          </span>
        </Tooltip>
      )}
    </h3>
  )
}

function KindGlyph() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="wl-glyph">
      <rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M1.5 5.5h13" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  )
}

function More({ n, what, open, onToggle }: { n: number; what: string; open: boolean; onToggle: () => void }) {
  return (
    <Button kind="ghost" size="sm" className="wl-more" aria-expanded={open} onClick={onToggle}>
      {open ? `Show fewer ${what}` : `Show ${n} more ${what}`}
    </Button>
  )
}

// ------------------------------------------------------------------ the card

export function WorkListsMock({ head }: { head: (count: number, action: ReactNode) => ReactNode }) {
  const { agents, finished, workflows } = fixtures()
  const tick = useTick()
  const [allAgents, setAllAgents] = useState(false)
  const [allWf, setAllWf] = useState(false)
  const sorted = order(agents)
  const shownAgents = allAgents ? sorted : sorted.slice(0, AGENT_CAP)
  const sortedWf = [...workflows].sort((a, b) => Number(!!b.asks) - Number(!!a.asks) || a.min - b.min)
  const shownWf = allWf ? sortedWf : sortedWf.slice(0, WORKFLOW_CAP)
  const nothing = agents.length === 0 && workflows.length === 0
  return (
    <Card id="home-work" className="wl-card">
      {head(
        agents.length,
        nothing ? null : (
          <ButtonLink kind="ghost" size="sm" href={addressToPath('work/running/live')}>
            All running
          </ButtonLink>
        ),
      )}
      {nothing ? (
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
              {finished.map((a) => (
                <AgentRow key={a.id} a={a} tick={tick} />
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="wl-cols">
          <section className="wl-col" aria-label="Agents">
            <ListHead title="Agents" n={agents.length} browser={agents.filter((a) => a.kind === 'browser').length} />
            <ul className="wl-list">
              {shownAgents.map((a) => (
                <AgentRow key={a.id} a={a} tick={tick} />
              ))}
              {finished.map((a) => (
                <AgentRow key={a.id} a={a} tick={tick} />
              ))}
            </ul>
            {sorted.length > AGENT_CAP && <More n={sorted.length - AGENT_CAP} what="agents" open={allAgents} onToggle={() => setAllAgents((o) => !o)} />}
          </section>
          <section className="wl-col" aria-label="Workflows">
            <ListHead title="Workflows" n={workflows.length} />
            {workflows.length === 0 ? (
              <p className="wl-none">None in progress.</p>
            ) : (
              <ul className="wl-list">
                {shownWf.map((w) => (
                  <WorkflowRow key={w.id} w={w} agents={agents} />
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

export const homeworkMock = (): string | null => Q.get('homework')

// ------------------------------------------------------------------ the rest of the page
// The same fixture reaches Needs you, the header chips, Recently finished and
// This workspace, so the page agrees with Your work: the parked agent is the
// question in Needs you (PARKED holds no capacity, so it counts as waiting),
// and the workflow at its review is the approval in the inbox.

const iso = (now: number, min: number) => new Date(now - min * 60_000).toISOString()

/** The person's tasks, as the tasks read would serve them. */
export function homeworkTasks(now: number): Task[] {
  const { agents, finished } = fixtures()
  return [...agents, ...finished].map((a) => {
    const asks = a.question !== undefined
    const done = a.state === 'SUCCEEDED'
    return {
      id: a.id,
      tenant_id: 'ws-orchard',
      state: a.state,
      runner_profile: a.kind === 'browser' ? 'claude-code-browser' : 'claude-code',
      metadata: { title: a.name },
      workflow_id: a.wf?.id ?? null,
      step_id: a.wf?.step ?? null,
      created_at: iso(now, a.min + 1),
      started_at: iso(now, a.min),
      updated_at: iso(now, done ? a.min : 0),
      completed_at: done ? iso(now, a.min) : null,
      park_reason: asks ? 'HUMAN_REQUIRED' : null,
      blocked_by: asks
        ? [{ reason: 'HUMAN_REQUIRED', park_phase: 'owner_questions', owner_questions: { questions: [{ task_id: a.id, step_id: a.wf?.step ?? null, question_index: 0, question: a.question!.text }] } }]
        : null,
      last_error: null,
      submitted_by: null,
      cancel_requested: false,
    } as unknown as Task
  })
}

/** The approvals inbox: the review each workflow that asks for one waits on. */
export function homeworkApprovals(now: number): ApprovalItem[] {
  return fixtures()
    .workflows.filter((w) => w.asks !== undefined && w.pr !== null)
    .map((w) => ({
      approval_id: `appr_${w.id}`,
      kind: 'merge',
      subject: { run_id: w.id },
      summary: `Merge ${w.name} (#${w.pr!.n})`,
      state: 'pending',
      requested_at: iso(now, 9),
      expires_at: iso(now, -60 * 23),
    }))
}

/** This workspace's counts: only the person's work runs in it in the mock-up. */
export function homeworkStats(read: Stats | null): Stats | null {
  if (read === null) return null
  const { agents } = fixtures()
  const live = agents.filter((a) => a.state !== 'PARKED')
  const by: Record<string, number> = Object.fromEntries(Object.keys(read.tasks_by_state).map((k) => [k, 0]))
  for (const a of live) by[a.state] = (by[a.state] ?? 0) + 1
  by.PARKED = agents.length - live.length
  return { ...read, tasks_by_state: by }
}
