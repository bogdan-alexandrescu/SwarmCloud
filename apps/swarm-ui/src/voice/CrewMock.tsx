// MOCK-HOOK FILE
/**
 * VOICE IT ACTIVITY, DIRECTION "CREW" (mock-up only, the owner's ask of
 * 11 November 2026). Drawn at `/voice/talk?voiceactivity=crew&crew=<state>`
 * from the invented fixture below; nothing here reads or writes a route.
 *
 * Under the Stage a strip of agent chips grows as work is handed out: the
 * voice agent, the planner, each implementation agent and each workflow,
 * each with its icon, state colour and a thin progress bar. Hovering or
 * pressing a chip shows its one-line progress and opens its console page.
 * While the voice session is paused, the voice orb folds into a quiet paused
 * mark at the head of the strip with Resume, and the strip keeps moving.
 * Between two replies the captions hold ONE working card: collapsed to a
 * line, expanded to the actions taken so far, newest at the bottom.
 *
 * The server side of pause and of the agent roster is not built: every
 * agent, line and frame here is fixture data.
 */

import { useEffect, useId, useState, type ReactNode } from 'react'

import { Button, Segmented, ToneMark } from '../components'
import '../styles/crew-mock.css'
import { TalkTip } from './Stage'

export type CrewState = 'ask' | 'handed' | 'working' | 'paused' | 'failed' | 'resumed' | 'ended'
const STATES: readonly CrewState[] = ['ask', 'handed', 'working', 'paused', 'failed', 'resumed', 'ended']

/**
 * The mock-up's switches, read once when the console loads: the router
 * rewrites the address to its route's one path, dropping the query, before
 * Talk first draws. The mock-up's own navigations set them again (`go`).
 */
const BOOT = new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search)
const param = (name: string): string | null => BOOT.get(name)

/** Whether the page is the crew mock-up (`?voiceactivity=crew`). */
export function crewMock(): boolean {
  return BOOT.get('voiceactivity') === 'crew'
}

type Kind = 'voice' | 'planner' | 'builder' | 'workflow'
type Run = 'listening' | 'thinking' | 'speaking' | 'paused' | 'working' | 'queued' | 'done' | 'failed' | 'ended'

interface Agent {
  id: string
  kind: Kind
  name: string
  run: Run
  /** 0..1 */
  progress: number
  line: string
  /** Seconds since it started. */
  since: number
  /** Its console page. */
  href: string
  reason?: string
}

const RUN_WORD: Record<Run, string> = {
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
  paused: 'paused',
  working: 'working',
  queued: 'queued',
  done: 'done',
  failed: 'failed',
  ended: 'ended',
}

const KIND_WORD: Record<Kind, string> = { voice: 'Voice agent', planner: 'Planner', builder: 'Implementation agent', workflow: 'Workflow' }

const PROJECT = 'Crumb & Kettle bakery site'

const ASK = 'Make me three versions of the landing page: one warm and rustic, one clean and simple, one playful.'

const voice = (run: Run, line: string): Agent => ({ id: 'voice', kind: 'voice', name: 'Voice it', run, progress: 1, line, since: 0, href: '/voice/telemetry' })
const planner = (run: Run, progress: number, line: string): Agent => ({
  id: 'plan',
  kind: 'planner',
  name: 'Planner',
  run,
  progress,
  line,
  since: 4,
  href: '/work/running',
})
const b1 = (run: Run, progress: number, line: string): Agent => ({ id: 'b1', kind: 'builder', name: 'Builder 1', run, progress, line, since: 7, href: '/work/running' })
const b2 = (run: Run, progress: number, line: string, reason?: string): Agent => ({
  id: 'b2',
  kind: 'builder',
  name: 'Builder 2',
  run,
  progress,
  line,
  since: 7,
  href: '/work/running',
  reason,
})
const check = (run: Run, progress: number, line: string): Agent => ({
  id: 'wf',
  kind: 'workflow',
  name: 'Frame check',
  run,
  progress,
  line,
  since: 31,
  href: '/work/workflows',
})

const FAIL_REASON = 'Stopped at 55%: version 3 asked for a web font, and a frame fetches nothing. Retry draws it with a built-in font.'

/**
 * One clock for the whole ask, in seconds since it was heard: the planner hands out work at 0:07, the voice
 * session pauses at PAUSED_AT (past the 10-20 s rule) and stays paused until the result is ready.
 */
const PAUSED_AT = 22

const CREW: Record<CrewState, Agent[]> = {
  ask: [voice('thinking', 'Waiting on the plan'), planner('thinking', 0.35, 'Splitting the ask in three')],
  handed: [
    voice('listening', 'Told you who is on it'),
    planner('done', 1, 'Planned 3 tasks'),
    b1('working', 0.15, 'v1: drawing the hero'),
    b2('working', 0.1, 'v3: picking colours'),
    check('queued', 0, 'Waits for a frame'),
  ],
  working: [
    voice('listening', 'Waiting on the builders'),
    planner('done', 1, 'Planned 3 tasks'),
    b1('working', 0.3, 'v1: drawing the menu'),
    b2('working', 0.2, 'v3: laying out the hero'),
    check('queued', 0, 'Waits for a frame'),
  ],
  paused: [
    voice('paused', 'Paused'),
    planner('done', 1, 'Planned 3 tasks'),
    b1('working', 0.72, 'v2: drawing the hero'),
    b2('working', 0.55, 'v3: the order band'),
    check('working', 0.4, 'v1 passed; on v2'),
  ],
  failed: [
    voice('paused', 'Paused'),
    planner('done', 1, 'Planned 3 tasks'),
    b1('working', 0.84, 'v2: the footer'),
    b2('failed', 0.55, 'v3 not drawn', FAIL_REASON),
    check('working', 0.5, 'v1 passed; on v2'),
  ],
  resumed: [
    voice('speaking', 'Saying what was delivered'),
    planner('done', 1, 'Planned 3 tasks'),
    b1('done', 1, 'v1 and v2 drawn'),
    b2('done', 1, 'v3 drawn on a retry'),
    check('done', 1, 'All 3 fit 1440 and 390'),
  ],
  ended: [
    voice('ended', 'Work kept in the project'),
    planner('done', 1, 'Planned 3 tasks'),
    b1('done', 1, 'v1 and v2 drawn'),
    b2('done', 1, 'v3 drawn on a retry'),
    check('done', 1, 'All 3 fit 1440 and 390'),
  ],
}

interface Step {
  at: number
  who: Kind | 'you'
  text: string
}

/** The working card's actions so far, oldest first; the card shows up to `n` of them. */
const STEPS: Step[] = [
  { at: 0, who: 'voice', text: `Read the project “${PROJECT}”` },
  { at: 2, who: 'voice', text: 'Asked the planner to split the ask' },
  { at: 6, who: 'planner', text: 'Planned 3 versions: 2 builders, 1 frame check' },
  { at: 7, who: 'builder', text: 'Builder 1 started: versions 1 and 2' },
  { at: 7, who: 'builder', text: 'Builder 2 started: version 3' },
  { at: 17, who: 'builder', text: 'Frame drawn: version 1, hero' },
  { at: PAUSED_AT, who: 'voice', text: 'Voice session paused' },
  { at: 31, who: 'workflow', text: 'Frame check started on version 1' },
  { at: 38, who: 'builder', text: 'Frame drawn: version 1, menu band' },
  { at: 58, who: 'workflow', text: 'Version 1 fits at 1440 and 390' },
  { at: 64, who: 'builder', text: 'Frame drawn: version 2, hero' },
  { at: 71, who: 'builder', text: 'Builder 2 stopped: version 3 not drawn' },
]

const WORKING: Record<CrewState, { steps: number; now: string; at: number } | null> = {
  ask: { steps: 2, now: 'Planner splitting the ask', at: 4 },
  handed: { steps: 5, now: 'Builder 1 · hero of v1', at: 12 },
  working: { steps: 6, now: 'Builder 1 · menu of v1', at: 17 },
  paused: { steps: 11, now: 'Builder 1 · hero of v2', at: 66 },
  failed: { steps: 12, now: 'Builder 2 stopped', at: 72 },
  resumed: null,
  ended: null,
}

interface Line {
  who: 'you' | 'agent'
  text: string
}

const LINES: Record<CrewState, Line[]> = {
  ask: [{ who: 'you', text: ASK }],
  handed: [
    { who: 'you', text: ASK },
    { who: 'agent', text: 'Three versions coming. Two builders and a frame check are on it; I’ll pause while they work.' },
  ],
  working: [
    { who: 'you', text: ASK },
    { who: 'agent', text: 'Three versions coming. Two builders and a frame check are on it; I’ll pause while they work.' },
  ],
  paused: [
    { who: 'you', text: ASK },
    { who: 'agent', text: 'Three versions coming. Two builders and a frame check are on it; I’ll pause while they work.' },
  ],
  failed: [
    { who: 'you', text: ASK },
    { who: 'agent', text: 'Three versions coming. Two builders and a frame check are on it; I’ll pause while they work.' },
  ],
  resumed: [
    { who: 'you', text: 'Resume.' },
    {
      who: 'agent',
      text: 'All three are on the stage: warm and rustic, clean and simple, and playful. Each fits a phone. Version 3 needed one retry. Which one should we take further?',
    },
  ],
  ended: [
    {
      who: 'agent',
      text: 'All three are on the stage: warm and rustic, clean and simple, and playful. Each fits a phone. Version 3 needed one retry. Which one should we take further?',
    },
    { who: 'you', text: 'Let me sleep on it. End the session.' },
  ],
}

const clock = (s: number): string => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

function go(href: string): void {
  const q = href.indexOf('?')
  if (q >= 0) new URLSearchParams(href.slice(q)).forEach((v, k) => BOOT.set(k, v))
  window.history.pushState(null, '', href)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** Projects' Resume in the mock-up: Talk on the kept project, the session live again. */
export function crewResume(): void {
  go('/voice/talk?voiceactivity=crew&crew=resumed')
}

/* ---- glyphs ---------------------------------------------------------------- */

function KindIcon({ kind }: { kind: Kind }) {
  const d: Record<Kind, ReactNode> = {
    voice: <path d="M4 12h2M8 8v8M12 5v14M16 8v8M20 12h-2" />,
    planner: (
      <>
        <circle cx="6" cy="6" r="2.2" />
        <circle cx="18" cy="6" r="2.2" />
        <circle cx="12" cy="18" r="2.2" />
        <path d="M6 8.2v2.3a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8.2M12 12.5v3.3" />
      </>
    ),
    builder: <path d="M9 7l-5 5 5 5M15 7l5 5-5 5M13.5 5l-3 14" />,
    workflow: (
      <>
        <rect x="3" y="4" width="6" height="5" rx="1.2" />
        <rect x="15" y="15" width="6" height="5" rx="1.2" />
        <path d="M6 9v3.5a2 2 0 0 0 2 2h6M12 12l2.5 2.5L12 17" />
      </>
    ),
  }
  return (
    <svg className="vtc-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {d[kind]}
    </svg>
  )
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg className={`vtc-ic vtc-chev${open ? ' is-open' : ''}`} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M7 10l5 5 5-5" />
    </svg>
  )
}

function InfoGlyph() {
  return (
    <svg className="vtc-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6M12 7.5v.5" />
    </svg>
  )
}

/* ---- the voice orb and its paused mark ------------------------------------- */

const ORB_WORD: Partial<Record<Run, string>> = { thinking: 'Thinking', listening: 'Listening', speaking: 'Speaking', ended: 'Ended' }

function Orb({ run }: { run: Run }) {
  return (
    <figure className={`vtc-orb is-${run}`} aria-label={`Voice it, ${ORB_WORD[run] ?? run}`}>
      <svg viewBox="0 0 200 200" aria-hidden="true" focusable="false">
        <circle className="vtc-orb-glow" cx="100" cy="100" r="74" />
        <circle className="vtc-orb-ring" cx="100" cy="100" r="74" />
        <circle className="vtc-orb-arc" cx="100" cy="100" r="74" />
        <circle className="vtc-orb-wave" cx="100" cy="100" r="74" />
        <circle className="vtc-orb-wave is-2" cx="100" cy="100" r="74" />
      </svg>
      <figcaption>
        <span className="vtc-orb-who">Voice it</span>
        <span className="vtc-orb-state">{ORB_WORD[run] ?? run}</span>
      </figcaption>
    </figure>
  )
}

function PausedMark({ since, onResume }: { since: number; onResume: () => void }) {
  return (
    <div className="vtc-pausemark" role="group" aria-label="Voice session paused">
      <span className="vtc-pause-glyph" aria-hidden="true">
        <i />
        <i />
      </span>
      <span className="vtc-pause-words">
        <span className="vtc-pause-top">
          <b>
            Paused <span className="vtc-pause-for">{clock(since)}</span>
          </b>
          <TalkTip tip="The paid voice session is closed while the agents work, so it costs nothing now. Resume reopens it where it left off in about a second; saying “resume” does the same, heard by this browser's own voice detection.">
            <button type="button" className="c-btn is-ghost is-sm is-icon vtc-pause-i" aria-label="About the pause">
              <InfoGlyph />
            </button>
          </TalkTip>
        </span>
        <span className="vtc-pause-meta">
          <span className="vtc-free">$0/min</span>
          <span className="vtc-hear" aria-label="Say resume">
            <svg className="vtc-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
            </svg>
            “resume”
          </span>
        </span>
      </span>
      <Button kind="primary" size="sm" onClick={onResume}>
        Resume
      </Button>
    </div>
  )
}

/* ---- the crew strip ---------------------------------------------------------- */

function Chip({ a, tick, now, open, onOpen, onRetry }: { a: Agent; tick: number; now: number; open: boolean; onOpen: (id: string | null) => void; onRetry: () => void }) {
  const id = useId()
  const [hover, setHover] = useState(false)
  const moving = a.run === 'working' || a.run === 'thinking'
  // The strip keeps moving: a working agent creeps on, a second at a time, whatever the voice session does.
  const progress = moving ? Math.min(0.97, a.progress + tick * 0.004) : a.progress
  const shown = open || hover
  const tone = a.run === 'failed' ? 'bad' : a.run === 'done' ? 'ok' : a.run === 'paused' || a.run === 'ended' ? 'paused' : a.run === 'queued' ? 'idle' : 'live'
  return (
    <li className={`vtc-chip is-${tone} is-${a.kind}`} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <button
        type="button"
        className="vtc-chip-btn"
        aria-expanded={shown}
        aria-controls={id}
        aria-label={`${a.name}, ${KIND_WORD[a.kind]}, ${RUN_WORD[a.run]}`}
        onClick={() => onOpen(open ? null : a.id)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onOpen(null)
        }}
      >
        <span className="vtc-chip-ic">
          <KindIcon kind={a.kind} />
          {moving && <span className="vtc-chip-live" aria-hidden="true" />}
        </span>
        <span className="vtc-chip-name">{a.name}</span>
        <span className="vtc-chip-run">{RUN_WORD[a.run]}</span>
        <span className="vtc-chip-line">{a.line}</span>
        {a.kind !== 'voice' && (
          <span className="vtc-bar" role="progressbar" aria-label={`${a.name} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
            <span style={{ width: `${Math.round(progress * 100)}%` }} />
          </span>
        )}
      </button>
      {a.run === 'failed' && (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
      <div id={id} className="vtc-pop" role="dialog" aria-label={a.name} hidden={!shown}>
        <p className="vtc-pop-head">
          <KindIcon kind={a.kind} />
          <b>{a.name}</b>
          <span className="vtc-pop-kind">{KIND_WORD[a.kind]}</span>
        </p>
        <p className="vtc-pop-line">{a.reason ?? a.line}</p>
        <p className="vtc-pop-meta">
          {a.kind !== 'voice' && <span>{Math.round(progress * 100)}%</span>}
          {a.kind !== 'voice' && <span>{clock(Math.max(0, now - a.since))}</span>}
          <Button size="sm" onClick={() => go(a.href)}>
            Open
          </Button>
        </p>
      </div>
    </li>
  )
}

/* ---- the working card ------------------------------------------------------- */

function WorkingCard({ state, tick, open, onToggle }: { state: CrewState; tick: number; open: boolean; onToggle: () => void }) {
  const id = useId()
  const w = WORKING[state]
  if (w === null) return null
  const steps = STEPS.slice(0, w.steps)
  return (
    <div className={`vtc-work${open ? ' is-open' : ''}`}>
      <button type="button" className="vtc-work-head" aria-expanded={open} aria-controls={id} onClick={onToggle}>
        <span className="vtc-work-live" aria-hidden="true" />
        <span className="vtc-work-now">{w.now}</span>
        <span className="vtc-work-time">{clock(w.at + tick)}</span>
        <Chevron open={open} />
      </button>
      {open && (
        <ol id={id} className="vtc-steps" aria-label="What Voice it has done">
          {steps.map((s, i) => (
            <li key={i} className={`vtc-step is-${s.who}${i === steps.length - 1 ? ' is-newest' : ''}`}>
              <span className="vtc-step-at">{clock(s.at)}</span>
              <span className="vtc-step-dot" aria-hidden="true" />
              <span className="vtc-step-text">{s.text}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/* ---- the stage -------------------------------------------------------------- */

const FONT = "font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;"

const VERSIONS: { id: string; label: string; short: string; by: string; html: string }[] = [
  {
    id: 'v1',
    label: '1 · Warm and rustic',
    short: 'v1 · Rustic',
    by: 'Builder 1',
    html: `<!doctype html><html><body style="margin:0;overflow:hidden;${FONT}font-size:12px;background:#f4ead9;color:#3b2a1a">
<div style="padding:8px 14px;display:flex;justify-content:space-between;letter-spacing:.06em;text-transform:uppercase"><b>Crumb &amp; Kettle</b><span>Menu · Visit · Order</span></div>
<div style="margin:0 14px;padding:12px 14px;border-radius:6px;background:linear-gradient(135deg,#a8642f,#6e3b18);color:#fff6ea">
<div style="font-family:Georgia,serif;font-size:22px;line-height:1.1">Baked at dawn,<br>gone by noon.</div>
<div style="margin-top:6px">Sourdough, buns and pies from a wood oven.</div>
<div style="margin-top:10px;display:inline-block;padding:5px 10px;border-radius:4px;background:#fff6ea;color:#6e3b18;font-weight:700">Order for tomorrow</div></div>
<div style="display:flex;gap:8px;padding:8px 14px">
<div style="flex:1;padding:5px 8px;border:1px solid #d8c3a3;border-radius:4px;line-height:1.25"><b>Rye loaf</b><br>4.20</div>
<div style="flex:1;padding:5px 8px;border:1px solid #d8c3a3;border-radius:4px;line-height:1.25"><b>Fig bun</b><br>2.60</div>
<div style="flex:1;padding:5px 8px;border:1px solid #d8c3a3;border-radius:4px;line-height:1.25"><b>Plum tart</b><br>3.80</div></div></body></html>`,
  },
  {
    id: 'v2',
    label: '2 · Clean and simple',
    short: 'v2 · Clean',
    by: 'Builder 1',
    html: `<!doctype html><html><body style="margin:0;overflow:hidden;${FONT}font-size:12px;background:#ffffff;color:#16181d">
<div style="padding:9px 16px;display:flex;justify-content:space-between;border-bottom:1px solid #e6e8ec"><b>crumb&amp;kettle</b><span style="color:#5b6170">Today · Order · Find us</span></div>
<div style="padding:16px 16px 8px"><div style="font-size:24px;font-weight:700;letter-spacing:-.02em;line-height:1.1">Fresh bread,<br>every morning.</div>
<div style="margin-top:6px;color:#5b6170">Open Tue–Sun, 7:00–15:00 · 12 Mill Lane</div>
<div style="margin-top:10px;display:flex;gap:6px"><span style="padding:5px 10px;border-radius:999px;background:#16181d;color:#fff">Order ahead</span><span style="padding:5px 10px;border-radius:999px;border:1px solid #c9ccd3">Today's bakes</span></div></div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;padding:6px 16px">
<div style="padding:5px 8px;border-radius:6px;background:#eef0f3;line-height:1.25"><b>Sourdough</b><br>4.20</div><div style="padding:5px 8px;border-radius:6px;background:#eef0f3;line-height:1.25"><b>Rye</b><br>3.90</div><div style="padding:5px 8px;border-radius:6px;background:#eef0f3;line-height:1.25"><b>Croissant</b><br>2.10</div></div></body></html>`,
  },
  {
    id: 'v3',
    label: '3 · Playful',
    short: 'v3 · Playful',
    by: 'Builder 2',
    html: `<!doctype html><html><body style="margin:0;overflow:hidden;${FONT}font-size:12px;background:#fff3f6;color:#2b1638">
<div style="padding:8px 14px;display:flex;justify-content:space-between;align-items:center"><b style="font-size:15px;color:#c2255c">Crumb &amp; Kettle!</b><span style="padding:4px 10px;border-radius:999px;background:#2b1638;color:#fff">Order</span></div>
<div style="margin:4px 14px;padding:14px;border-radius:18px;background:#ffd43b;transform:rotate(-1.2deg)">
<div style="font-size:22px;font-weight:800;line-height:1.05">Buns so good<br>they wave back.</div>
<div style="margin-top:6px">Fresh out at 7, sold out by lunch.</div></div>
<div style="display:flex;gap:8px;align-items:center;padding:12px 14px">
<span style="width:32px;height:32px;border-radius:50%;background:#ff8fab"></span><span style="width:32px;height:32px;border-radius:50%;background:#74c0fc"></span><span style="width:32px;height:32px;border-radius:50%;background:#8ce99a"></span>
<span>Today: 9 kinds of bun</span></div></body></html>`,
  },
]

const FRAME_POLICY = "default-src 'none'; style-src 'unsafe-inline'"
const framed = (html: string): string => html.replace('<html>', `<html><head><meta http-equiv="Content-Security-Policy" content="${FRAME_POLICY}"></head>`)

/** Which version slot each state has drawn: the frame, the agent still on it (with progress), or its failure. */
const SLOTS: Record<CrewState, ('drawn' | 'drawing' | 'failed' | 'queued')[]> = {
  ask: [],
  handed: ['drawing', 'queued', 'drawing'],
  working: ['drawing', 'queued', 'drawing'],
  paused: ['drawn', 'drawing', 'drawing'],
  failed: ['drawn', 'drawing', 'failed'],
  resumed: ['drawn', 'drawn', 'drawn'],
  ended: ['drawn', 'drawn', 'drawn'],
}

function Brief() {
  const facts: [string, string][] = [
    ['Bakery', 'Crumb & Kettle, 12 Mill Lane, Millbrook'],
    ['Open', 'Tue–Sun, 7:00–15:00'],
    ['Must show', 'Today’s bakes, order ahead, where to find us'],
    ['Asked for', 'Landing page, three versions'],
  ]
  return (
    <dl className="vtc-brief">
      {facts.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function Stage({ state, crew }: { state: CrewState; crew: Agent[] }) {
  const slots = SLOTS[state]
  const [tab, setTab] = useState<'brief' | 'landing' | null>(null)
  const shown = tab ?? (slots.length > 0 ? 'landing' : 'brief')
  const drawn = slots.filter((s) => s === 'drawn').length
  return (
    <section className="vtc-stage" aria-label="Stage">
      <div className="vtc-tabs" role="tablist" aria-label="Visuals">
        {slots.length > 0 && (
          <button type="button" role="tab" aria-selected={shown === 'landing'} className="vtc-tab" onClick={() => setTab('landing')}>
            Landing page
            <span className="vtc-tab-n">
              {drawn}/{slots.length}
            </span>
          </button>
        )}
        <button type="button" role="tab" aria-selected={shown === 'brief'} className="vtc-tab" onClick={() => setTab('brief')}>
          Brief
        </button>
      </div>
      {shown === 'brief' ? (
        <Brief />
      ) : (
        <div className="vtc-frames">
          {VERSIONS.map((v, i) => {
            const s = slots[i]
            const who = crew.find((a) => a.name === v.by)
            return (
              <figure key={v.id} className={`vtc-frame is-${s}`}>
                <figcaption>
                  <span className="vtc-frame-label">{v.label}</span>
                  <span className="vtc-frame-short">{v.short}</span>
                  <span className="vtc-frame-by">{v.by}</span>
                </figcaption>
                {s === 'drawn' ? (
                  <iframe title={v.label} sandbox="" srcDoc={framed(v.html)} />
                ) : (
                  <div className="vtc-frame-wait" aria-label={`${v.label}: ${s}`}>
                    <span className="vtc-frame-skel" />
                    <span className="vtc-frame-skel is-short" />
                    <span className="vtc-frame-skel is-block" />
                    <span className="vtc-frame-word">
                      {s === 'failed' ? 'Not drawn' : s === 'queued' ? 'Next' : `${who === undefined ? '' : Math.round(who.progress * 100) + '%'}`}
                    </span>
                  </div>
                )}
              </figure>
            )
          })}
        </div>
      )}
    </section>
  )
}

/* ---- the page ----------------------------------------------------------------- */

function initial(): CrewState {
  const s = param('crew')
  return (STATES as readonly string[]).includes(s ?? '') ? (s as CrewState) : 'ask'
}

export function CrewTalk() {
  const [state, setState] = useState<CrewState>(initial)
  const [openChip, setOpenChip] = useState<string | null>(() => param('crewopen'))
  const [logOpen, setLogOpen] = useState(() => param('crewlog') === 'open')
  const [tick, setTick] = useState(0)
  const [micMode, setMicMode] = useState<'open' | 'ptt'>('open')
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 1000)
    return () => window.clearInterval(t)
  }, [])
  useEffect(() => {
    if (openChip === null) return
    const close = (e: MouseEvent) => {
      if (!(e.target instanceof Element) || e.target.closest('.vtc-chip') === null) setOpenChip(null)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [openChip])

  const crew = state === 'ask' ? CREW.ask : CREW[state]
  const me: Agent = crew[0] ?? voice('listening', '')
  const paused = me.run === 'paused'
  const ended = state === 'ended'
  const live = !ended
  const working = crew.filter((a) => a.run === 'working' || a.run === 'thinking').length
  const retry = () => setState('paused')
  // The ask's one clock: the working card, the chips' times and the pause all read it, so they never disagree.
  const now = (WORKING[state]?.at ?? 0) + tick
  // While paused the voice agent's chip IS the paused mark, so the strip does not show it twice.
  const chips = paused ? crew.filter((a) => a.kind !== 'voice') : crew

  return (
    <div className={`vt vtc${paused ? ' is-paused' : ''}${ended ? ' is-ended' : ''}`} data-crew={state}>
      <div className="vt-sky" aria-hidden="true" />

      <div className="vt-hud">
        <p className="vt-status" role="status">
          <ToneMark tone={paused ? 'paused' : live ? 'live' : 'unknown'} />
          {ended ? 'Session ended' : paused ? <span className="vtc-sr">Paused</span> : 'Live'}
          <span className={`vtc-proj${paused ? ' is-first' : ''}`}>{PROJECT}</span>
        </p>
        <span className="vt-hud-actions">
          {ended ? (
            <>
              <Button size="sm" onClick={() => go('/voice/projects?voiceactivity=crew')}>
                Open project
              </Button>
              <Button kind="primary" size="sm" onClick={() => setState('resumed')}>
                Resume
              </Button>
            </>
          ) : (
            <>
              <span className="vtc-mic" role="group" aria-label="Microphone">
                <Button size="sm" kind="secondary">
                  Mute
                </Button>
                <Segmented<'open' | 'ptt'>
                  small
                  label="How the mic listens"
                  value={micMode}
                  options={[
                    { key: 'open', label: 'Open mic', title: 'Always listening; talk over Voice it to interrupt' },
                    { key: 'ptt', label: 'Push to talk', title: 'Listens only while you hold Space or the button' },
                  ]}
                  onChange={setMicMode}
                />
              </span>
              <Button kind="secondary" size="sm" onClick={() => setState('ended')}>
                End session
              </Button>
            </>
          )}
        </span>
        {ended && (
          <p className="vtc-kept" role="status">
            <svg className="vtc-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M5 12.5l4.5 4.5L19 7.5" />
            </svg>
            Kept in “{PROJECT}”: 3 frames, the brief, the work of 3 agents and 1 workflow
          </p>
        )}
      </div>

      <Stage key={state} state={state} crew={crew} />

      <section className="vtc-crew" aria-label="Agents on this ask">
        {paused && <PausedMark since={now - PAUSED_AT} onResume={() => setState('resumed')} />}
        <ol className="vtc-chips">
          {chips.map((a) => (
            <Chip key={a.id} a={a} tick={tick} now={now} open={openChip === a.id} onOpen={setOpenChip} onRetry={retry} />
          ))}
        </ol>
        {working > 0 && !paused && (
          <span className="vtc-count" aria-label={`${working} agents working`}>
            {working} working
          </span>
        )}
      </section>

      <div className="vt-presence" aria-label="Voice it">
        {!paused && (
          <div className="vt-core vtc-core">
            <Orb run={me.run} />
          </div>
        )}
        <div className="vt-subs-band">
          <div className="vt-subs">
            <div className="vt-captions" role="log" aria-label="Captions">
              {LINES[state].map((l, i) => (
                <p key={i} className={`vt-cap is-${l.who === 'you' ? 'you' : 'agent'}`}>
                  <span className="vt-cap-who">{l.who === 'you' ? 'You' : 'Voice it'}</span>
                  <span className="vt-cap-text">{l.text}</span>
                </p>
              ))}
            </div>
          </div>
          <WorkingCard state={state} tick={tick} open={logOpen} onToggle={() => setLogOpen((o) => !o)} />
        </div>
      </div>
    </div>
  )
}

/** The fixture's Voice it projects for the crew mock-up's Projects page. */
export const CREW_PROJECTS = [
  { project_id: 'vp_crew_bakery', title: PROJECT, updated_at: '2026-11-11T21:20:00Z', last_session_at: '2026-11-11T21:12:00Z', open_cards: 2 },
  { project_id: 'vp_crew_allot', title: 'Allotment watering timer', updated_at: '2026-11-09T18:02:00Z', last_session_at: '2026-11-09T17:40:00Z', open_cards: 0 },
  { project_id: 'vp_crew_books', title: 'Book club reading list', updated_at: '2026-11-04T09:15:00Z', last_session_at: '2026-11-04T09:01:00Z', open_cards: 1 },
]

/** What each fixture project's sessions left behind, for the Projects page's Work column. */
export const CREW_KEPT: Record<string, { frames: number; agents: number; workflows: number }> = {
  vp_crew_bakery: { frames: 3, agents: 3, workflows: 1 },
  vp_crew_allot: { frames: 1, agents: 2, workflows: 0 },
  vp_crew_books: { frames: 0, agents: 1, workflows: 0 },
}

const count = (n: number, one: string): string => `${n} ${one}${n === 1 ? '' : 's'}`

export function CrewKept({ id }: { id: string }) {
  const k = CREW_KEPT[id]
  if (k === undefined) return null
  return (
    <span className="vtc-keptcell">
      <span className="vtc-keptdots" aria-hidden="true">
        {Array.from({ length: k.agents + k.workflows }, (_, i) => (
          <i key={i} />
        ))}
      </span>
      {[k.frames > 0 ? count(k.frames, 'frame') : '', count(k.agents, 'agent'), k.workflows > 0 ? count(k.workflows, 'workflow') : '']
        .filter((w) => w !== '')
        .join(' · ')}
    </span>
  )
}
