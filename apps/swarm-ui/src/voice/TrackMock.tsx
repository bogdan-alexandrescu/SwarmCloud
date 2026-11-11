// MOCK-HOOK FILE
/**
 * VOICE IT, TALK: THE ACTIVITY TIMELINE (direction "track"; mock-up only,
 * ?voiceactivity=track). The owner's ask of 11 November 2026: while work runs
 * in the background, Talk shows the agent working, the agents and workflows
 * it pulled in, each with its state and progress; the paid voice session
 * pauses during long work and resumes where it left off.
 *
 * Nothing here is served: the planner, the agents, the workflow, the pause
 * and its saving are invented fixture data, picked by `?vstate=` (ask,
 * handed, working-collapsed, working-open, paused, resumed, failed-agent,
 * ended). The server side of pause and of the roster is not built.
 */
import { useState, type ReactNode } from 'react'

import { Button, buttonClass, ToneMark } from '../components'
import type { VoiceProject } from './Projects'
import { TalkTip } from './Stage'
import '../styles/talk-track.css'

/** The address the page opened on, read once: the router drops a query it does not know. */
const OPENED = new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search)

/** On when the page opened with `voiceactivity=track`. */
export function trackMock(): boolean {
  return OPENED.get('voiceactivity') === 'track'
}

export type TrackState = 'ask' | 'handed' | 'working-collapsed' | 'working-open' | 'paused' | 'resumed' | 'failed-agent' | 'ended'
const STATES: readonly TrackState[] = ['ask', 'handed', 'working-collapsed', 'working-open', 'paused', 'resumed', 'failed-agent', 'ended']

function pickedState(): TrackState {
  const s = OPENED.get('vstate')
  return STATES.find((x) => x === s) ?? 'handed'
}

const PROJECT = 'Corner Crumb landing page'

/** Voice it › Projects' rows for the mock: the bakery's project first. */
export const TRACK_PROJECTS: VoiceProject[] = [
  { project_id: 'vp_mock_crumb', title: PROJECT, updated_at: '2026-11-11T16:05:00Z', last_session_at: '2026-11-11T16:02:00Z', open_cards: 3 },
  { project_id: 'vp_mock_order', title: 'Holiday pre-order form', updated_at: '2026-11-09T10:40:00Z', last_session_at: '2026-11-09T10:21:00Z', open_cards: 1 },
  { project_id: 'vp_mock_hours', title: 'Opening hours sign', updated_at: '2026-11-03T08:12:00Z', last_session_at: '2026-11-03T08:05:00Z', open_cards: 0 },
]

/** What each mock project's agents and workflows stand at, for Projects' Work column. */
export const TRACK_WORK: Readonly<Record<string, { tone: string; word: string; tip: string }>> = {
  vp_mock_crumb: { tone: 'ok', word: '3 versions delivered', tip: 'Planner, 2 implementation agents and 1 workflow; 3 frames on the stage' },
  vp_mock_order: { tone: 'live', word: '1 agent working', tip: 'An implementation agent is building the form; started 9 November' },
}

export function TrackWork({ id }: { id: string }) {
  const w = TRACK_WORK[id]
  if (w === undefined) return null
  return (
    <span title={w.tip}>
      <ToneMark tone={w.tone}>{w.word}</ToneMark>
    </span>
  )
}

type RowState = 'thinking' | 'working' | 'done' | 'failed' | 'waiting'
type Kind = 'planner' | 'agent' | 'workflow'

interface Row {
  id: string
  kind: Kind
  name: string
  doing: string
  /** Seconds from the ask. */
  from: number
  to: number | null
  state: RowState
  /** The console page the row opens. */
  href: string
  reason?: string
  kids?: Row[]
}

interface Lane {
  kind: 'talk' | 'paused'
  from: number
  to: number | null
}

interface Step {
  at: number
  tone: string
  text: string
}

interface Line {
  side: 'you' | 'agent'
  text: string
}

interface Shot {
  now: number
  lanes: Lane[]
  rows: Row[]
  lines: Line[]
  /** The working card: what it says it is doing now, and every step so far; null once a reply has replaced it. */
  card: { doing: string; steps: Step[] } | null
  ring: 'thinking' | 'working' | 'paused' | 'speaking' | 'idle'
  frames: FrameId[]
}

type FrameId = 'rustic' | 'modern' | 'seasonal'

const ASK: Line = {
  side: 'you',
  text: 'Can you make three versions of the landing page for my bakery? One warm and rustic, one clean and modern, and one built around the seasonal menu.',
}
const ACK: Line = { side: 'agent', text: 'Three versions, got it. I am handing it to the planner now.' }

const PLANNER = 'task_mock_planner'
const AGENT_A = 'task_mock_rustic'
const AGENT_B = 'task_mock_modern'
const WORKFLOW = 'wf_mock_seasonal'

const agentPage = (id: string) => `/agents/running/${id}`
const workflowPage = (id: string) => `/workflows/${id}`

const STEPS: Step[] = [
  { at: 0, tone: 'info', text: 'Heard the ask' },
  { at: 1, tone: 'info', text: `Read the project “${PROJECT}”` },
  { at: 2, tone: 'live', text: 'Asked the planner' },
  { at: 11, tone: 'ok', text: 'Planner: 3 versions, 2 agents and 1 workflow' },
  { at: 13, tone: 'live', text: 'Implementation agent started: Warm & rustic' },
  { at: 14, tone: 'live', text: 'Implementation agent started: Clean & modern' },
  { at: 15, tone: 'live', text: 'Workflow started: Seasonal menu' },
]

/** A word from the person while the agents work, so the working card sits between two voice replies. */
const ASIDE: Line = { side: 'you', text: 'Put the order button near the top on all three, please.' }
const NOTED: Line = { side: 'agent', text: 'Noted. I passed that on to all three.' }

/** The log up to the pause: the hand-off, the aside passed on, the first frames. */
const STEPS_ON: Step[] = [
  ...STEPS,
  { at: 19, tone: 'ok', text: 'Frame drawn: Warm & rustic, hero' },
  { at: 26, tone: 'info', text: 'Heard: order button near the top' },
  { at: 27, tone: 'info', text: 'Passed on to 2 agents and 1 workflow' },
  { at: 34, tone: 'ok', text: 'Workflow step done: autumn menu gathered' },
]

/** The voice talks until PAUSE_AT, then pauses while the agents work. */
const PAUSE_AT = 41

function planner(state: RowState, doing: string, to: number | null, kids: Row[]): Row {
  return { id: PLANNER, kind: 'planner', name: 'Planner', doing, from: 2, to, state, href: agentPage(PLANNER), kids }
}
function rustic(state: RowState, doing: string, to: number | null): Row {
  return { id: AGENT_A, kind: 'agent', name: 'Warm & rustic', doing, from: 13, to, state, href: agentPage(AGENT_A) }
}
function modern(state: RowState, doing: string, to: number | null, reason?: string): Row {
  return { id: AGENT_B, kind: 'agent', name: 'Clean & modern', doing, from: 14, to, state, href: agentPage(AGENT_B), reason }
}
function seasonal(state: RowState, doing: string, to: number | null): Row {
  return { id: WORKFLOW, kind: 'workflow', name: 'Seasonal menu', doing, from: 15, to, state, href: workflowPage(WORKFLOW) }
}

const FAILED_WHY = 'Stopped at 2:02: the font file the brief names would not load, twice. Nothing was drawn for this version; the other two are not affected.'

function shot(state: TrackState): Shot {
  switch (state) {
    case 'ask':
      return {
        now: 7,
        lanes: [{ kind: 'talk', from: 0, to: null }],
        rows: [{ ...planner('thinking', 'Reading the brief, splitting it into three versions', null, []) }],
        lines: [ASK, ACK],
        card: { doing: 'Planner thinking', steps: STEPS.slice(0, 3) },
        ring: 'thinking',
        frames: [],
      }
    case 'handed':
      return {
        now: 17,
        lanes: [{ kind: 'talk', from: 0, to: null }],
        rows: [
          planner('done', 'Handed out 3 versions', 11, [
            rustic('working', 'Hero and order button · 1 of 4', null),
            modern('working', 'Type and colours', null),
            seasonal('working', 'Step 1 of 4 · autumn menu', null),
          ]),
        ],
        lines: [ASK, ACK],
        card: { doing: 'Three versions in the works', steps: STEPS },
        ring: 'working',
        frames: [],
      }
    case 'working-collapsed':
    case 'working-open':
      return {
        now: 38,
        lanes: [{ kind: 'talk', from: 0, to: null }],
        rows: [
          planner('done', 'Handed out 3 versions', 11, [
            rustic('working', 'Menu strip · 2 of 4', null),
            modern('working', 'Header · 1 of 4', null),
            seasonal('working', 'Step 2 of 4 · menu layout', null),
          ]),
        ],
        lines: [ASK, ACK, ASIDE, NOTED],
        card: { doing: 'Drawing the first frames', steps: STEPS_ON },
        ring: 'working',
        frames: [],
      }
    case 'paused':
      return {
        now: 96,
        lanes: [
          { kind: 'talk', from: 0, to: PAUSE_AT },
          { kind: 'paused', from: PAUSE_AT, to: null },
        ],
        rows: [
          planner('done', 'Handed out 3 versions', 11, [
            rustic('working', 'Opening hours · 3 of 4', null),
            modern('working', 'Hero · 2 of 4', null),
            seasonal('working', 'Step 3 of 4 · drawing', null),
          ]),
        ],
        lines: [ASK, ACK, ASIDE, NOTED],
        card: {
          doing: 'Drawing the opening hours',
          steps: [
            ...STEPS_ON,
            { at: PAUSE_AT, tone: 'paused', text: 'Voice paused' },
            { at: 52, tone: 'ok', text: 'Frame drawn: Clean & modern, header' },
            { at: 71, tone: 'ok', text: 'Workflow step done: menu laid out' },
          ],
        },
        ring: 'paused',
        frames: [],
      }
    case 'resumed':
      return {
        now: 161,
        lanes: [
          { kind: 'talk', from: 0, to: PAUSE_AT },
          { kind: 'paused', from: PAUSE_AT, to: 148 },
          { kind: 'talk', from: 148, to: null },
        ],
        rows: [
          planner('done', 'Handed out 3 versions', 11, [
            rustic('done', '4 of 4 drawn', 121),
            modern('done', '4 of 4 drawn', 139),
            seasonal('done', '4 of 4 steps', 146),
          ]),
        ],
        lines: [
          ASK,
          ACK,
          ASIDE,
          NOTED,
          {
            side: 'agent',
            text: 'All three are on the stage: warm and rustic, clean and modern, and the seasonal menu. Which one shall we take forward?',
          },
        ],
        card: null,
        ring: 'speaking',
        frames: ['rustic', 'modern', 'seasonal'],
      }
    case 'failed-agent':
      return {
        now: 140,
        lanes: [
          { kind: 'talk', from: 0, to: PAUSE_AT },
          { kind: 'paused', from: PAUSE_AT, to: 131 },
          { kind: 'talk', from: 131, to: null },
        ],
        rows: [
          planner('done', 'Handed out 3 versions', 11, [
            rustic('done', '4 of 4 drawn', 121),
            modern('failed', 'Stopped at 2 of 4', 122, FAILED_WHY),
            seasonal('done', '4 of 4 steps', 129),
          ]),
        ],
        lines: [
          ASK,
          ACK,
          ASIDE,
          NOTED,
          { side: 'agent', text: 'Two versions are ready. The clean and modern one stopped before it finished. Shall I try it again?' },
        ],
        card: null,
        ring: 'speaking',
        frames: ['rustic', 'seasonal'],
      }
    case 'ended':
      return {
        now: 182,
        lanes: [
          { kind: 'talk', from: 0, to: PAUSE_AT },
          { kind: 'paused', from: PAUSE_AT, to: 148 },
          { kind: 'talk', from: 148, to: 182 },
        ],
        rows: [
          planner('done', 'Handed out 3 versions', 11, [
            rustic('done', '4 of 4 drawn', 121),
            modern('done', '4 of 4 drawn', 139),
            seasonal('done', '4 of 4 steps', 146),
          ]),
        ],
        lines: [
          ASK,
          ACK,
          ASIDE,
          NOTED,
          {
            side: 'agent',
            text: 'All three are on the stage: warm and rustic, clean and modern, and the seasonal menu. Which one shall we take forward?',
          },
          { side: 'you', text: 'Let me think about it overnight. That is all for today.' },
          { side: 'agent', text: 'Sleep on it. Everything stays in the project for next time.' },
        ],
        card: null,
        ring: 'idle',
        frames: ['rustic', 'modern', 'seasonal'],
      }
  }
}

/** m:ss */
const clock = (s: number): string => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

/** The session's GPT-Live rate the mock counts the saving at, dollars a minute (invented). */
const RATE_PER_MIN = 0.18
const pausedFor = (lanes: Lane[], now: number): number => lanes.filter((l) => l.kind === 'paused').reduce((t, l) => t + ((l.to ?? now) - l.from), 0)
const dollars = (n: number): string => `$${n.toFixed(2)}`

function go(href: string): void {
  window.history.pushState(null, '', href)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

const ROW_TONE: Record<RowState, string> = { thinking: 'live', working: 'live', done: 'ok', failed: 'bad', waiting: 'unknown' }
const ROW_WORD: Record<RowState, string> = { thinking: 'Thinking', working: 'Working', done: 'Done', failed: 'Failed', waiting: 'Waiting' }
const KIND_WORD: Record<Kind, string> = { planner: 'Planner', agent: 'Agent', workflow: 'Workflow' }

function InfoGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5M12 7.75v.01" />
    </svg>
  )
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg className={`vtk-chev${open ? ' is-open' : ''}`} viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <path d="M8 10l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function PauseGlyph() {
  return (
    <svg className="vtk-pause-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="16" height="16">
      <rect x="7" y="5.5" width="3.4" height="13" rx="1" />
      <rect x="13.6" y="5.5" width="3.4" height="13" rx="1" />
    </svg>
  )
}

function MicGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

/** The presence orb: the voice, and a satellite for each agent or workflow pulled in while it works. */
function Orb({ ring, crew }: { ring: Shot['ring']; crew: Row[] }) {
  const word = { thinking: 'Thinking', working: 'Working', paused: 'Paused', speaking: 'Speaking', idle: 'Ended' }[ring]
  const sats = ring === 'working' || ring === 'paused' ? crew : []
  return (
    <figure className={`vtk-orb is-${ring}`} aria-label={`Voice it: ${word}`}>
      <svg viewBox="0 0 200 200" aria-hidden="true">
        <circle className="vtk-orb-glow" cx="100" cy="100" r="62" />
        <circle className="vtk-orb-base" cx="100" cy="100" r="62" />
        <circle className="vtk-orb-still" cx="100" cy="100" r="62" />
        {ring === 'speaking' && (
          <path
            className="vtk-orb-wave"
            d="M38 100 C 50 78, 60 122, 72 100 S 92 70, 100 100 S 122 132, 132 100 S 150 82, 162 100"
          />
        )}
        {sats.length > 0 && <circle className="vtk-orb-orbit" cx="100" cy="100" r="86" />}
        {sats.map((s, i) => {
          const a = (-90 + (360 / sats.length) * i) * (Math.PI / 180)
          return <circle key={s.id} className={`vtk-orb-sat is-${s.kind} is-${s.state}`} cx={100 + 86 * Math.cos(a)} cy={100 + 86 * Math.sin(a)} r="6" />
        })}
      </svg>
      <figcaption className="vtk-orb-word">
        {ring === 'paused' && <PauseGlyph />}
        <span>{word}</span>
      </figcaption>
    </figure>
  )
}

/** The working card: between two replies, in place of the next one. */
function WorkingCard({ card, now, paused, open, onToggle }: { card: NonNullable<Shot['card']>; now: number; paused: boolean; open: boolean; onToggle: () => void }) {
  return (
    <div className={`vtk-work${open ? ' is-open' : ''}`} role="group" aria-label="Voice it is working">
      <button type="button" className="vtk-work-line" aria-expanded={open} onClick={onToggle}>
        <span className="vtk-live" aria-hidden="true" />
        <span className="vtk-cap-who">Voice it</span>
        <span className="vtk-work-now">{card.doing}</span>
        {paused && (
          <span className="vtk-work-voice">
            <PauseGlyph />
            voice paused
          </span>
        )}
        <span className="vtk-time">{clock(now)}</span>
        <Chevron open={open} />
      </button>
      {open && (
        <div className="vtk-steps-scroll">
          <ol className="vtk-steps" aria-label="What Voice it has done">
            {card.steps.map((s) => (
              <li key={`${s.at}-${s.text}`} className="vtk-step">
                <span className="vtk-step-at">{clock(s.at)}</span>
                <ToneMark tone={s.tone} />
                <span className="vtk-step-text">{s.text}</span>
              </li>
            ))}
            <li className="vtk-step is-now" aria-current="step">
              <span className="vtk-step-at">{clock(now)}</span>
              <span className="vtk-live" aria-hidden="true" />
              <span className="vtk-step-text">{card.doing}</span>
            </li>
          </ol>
        </div>
      )}
    </div>
  )
}

/** The time axis' ticks: every 15 s to a minute, every 30 s past it, every minute past three. */
function ticks(span: number): number[] {
  const step = span <= 60 ? 15 : span <= 180 ? 30 : 60
  const out: number[] = []
  for (let t = 0; t <= span; t += step) out.push(t)
  return out
}

const pct = (t: number, span: number) => `${((Math.min(t, span) / span) * 100).toFixed(2)}%`

function Span({ from, to, now, span, tone }: { from: number; to: number | null; now: number; span: number; tone: string }) {
  return (
    <span className="vtk-span" aria-hidden="true">
      <span className={`vtk-span-bar is-${tone}${to === null ? ' is-open' : ''}`} style={{ left: pct(from, span), width: pct((to ?? now) - from, span) }} />
    </span>
  )
}

function TimelineRow({ row, now, span, nested }: { row: Row; now: number; span: number; nested: boolean }) {
  const page = row.kind === 'workflow' ? 'Workflows' : 'Agents'
  return (
    <li className={`vtk-row is-${row.state}${nested ? ' is-nested' : ''}`}>
      <div className="vtk-row-main">
        <button type="button" className="vtk-row-open" onClick={() => go(row.href)} aria-label={`${row.name}, ${ROW_WORD[row.state]}: open in ${page}`}>
          <span className="vtk-row-head">
            <ToneMark tone={ROW_TONE[row.state]} label={ROW_WORD[row.state]} />
            <span className="vtk-row-name">{row.name}</span>
            {row.kind !== 'planner' && <span className={`vtk-kind is-${row.kind}`}>{KIND_WORD[row.kind]}</span>}
            <span className="vtk-time">{clock((row.to ?? now) - row.from)}</span>
          </span>
          <span className="vtk-row-doing">{row.doing}</span>
          <Span from={row.from} to={row.to} now={now} span={span} tone={ROW_TONE[row.state]} />
        </button>
        {row.state === 'failed' && (
          <span className="vtk-row-acts">
            {row.reason !== undefined && (
              <TalkTip tip={row.reason} end>
                <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label={`Why ${row.name} failed`} data-why="">
                  <InfoGlyph />
                </button>
              </TalkTip>
            )}
            <Button kind="secondary" size="sm">
              Retry
            </Button>
          </span>
        )}
      </div>
      {row.kids !== undefined && row.kids.length > 0 && (
        <ul className="vtk-kids">
          {row.kids.map((k) => (
            <TimelineRow key={k.id} row={k} now={now} span={span} nested />
          ))}
        </ul>
      )}
    </li>
  )
}

/** The activity timeline: the voice session's own lane, then each hand-off, agents nested under the planner that called them. */
function Timeline({ s, ended }: { s: Shot; ended: boolean }) {
  const span = Math.max(30, Math.ceil(s.now / 15) * 15)
  const all = s.rows.flatMap((r) => [r, ...(r.kids ?? [])])
  const working = all.filter((r) => r.state === 'working' || r.state === 'thinking').length
  const done = all.filter((r) => r.state === 'done').length
  const failed = all.filter((r) => r.state === 'failed').length
  const saved = pausedFor(s.lanes, s.now)
  return (
    <section className="vtk-track" aria-label="Activity">
      <header className="vtk-track-h">
        <h2 className="vtk-track-title">Activity</h2>
        <span className="vtk-counts">
          {working > 0 && <ToneMark tone="live">{working} working</ToneMark>}
          {done > 0 && <ToneMark tone="ok">{done} done</ToneMark>}
          {failed > 0 && <ToneMark tone="bad">{failed} failed</ToneMark>}
        </span>
        <span className="vtk-time is-total">{clock(s.now)}</span>
      </header>
      <div className="vtk-axis" aria-hidden="true">
        {ticks(span).map((t) => (
          <span key={t} className="vtk-tick" style={{ left: pct(t, span) }}>
            {clock(t)}
          </span>
        ))}
        {!ended && <span className="vtk-nowline" style={{ left: pct(s.now, span) }} />}
      </div>
      <div className="vtk-lane" role="group" aria-label="Voice session">
        <span className="vtk-lane-head">
          <span className="vtk-row-name">Voice session</span>
          {saved > 0 && (
            <TalkTip tip={`Paused ${clock(saved)} in all, while the agents worked: nothing billed for it. Counted at ${dollars(RATE_PER_MIN)} a minute.`} end>
              <span className="vtk-saved" tabIndex={0}>
                {dollars((saved / 60) * RATE_PER_MIN)} saved
              </span>
            </TalkTip>
          )}
        </span>
        <span className="vtk-span is-lane" aria-hidden="true">
          {s.lanes.map((l) => (
            <span
              key={l.from}
              className={`vtk-span-bar is-${l.kind}${l.to === null && !ended ? ' is-open' : ''}`}
              style={{ left: pct(l.from, span), width: pct((l.to ?? s.now) - l.from, span) }}
            />
          ))}
        </span>
      </div>
      <ul className="vtk-rows">
        {s.rows.map((r) => (
          <TimelineRow key={r.id} row={r} now={s.now} span={span} nested={false} />
        ))}
      </ul>
    </section>
  )
}

const FRAME_LABEL: Record<FrameId, string> = { rustic: 'Warm & rustic', modern: 'Clean & modern', seasonal: 'Seasonal menu' }

/* Drawn at the Stage's own size, no zoom: every word is at least 12px where the person reads it. */
const FRAME_HTML: Record<FrameId, string> = {
  rustic: `<!doctype html><html style="overflow:hidden"><body style="margin:0;font-family:Georgia,serif;background:#f4ead8;color:#3b2a1a;font-size:12px">
<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px"><b style="font-size:14px">Corner Crumb</b><span>Breads · Visit</span></div>
<div style="margin:0 12px;padding:12px 14px;border-radius:8px;background:linear-gradient(135deg,#a8673a,#6b3f22);color:#fff7ea">
<div style="font-size:19px;line-height:1.15">Baked at dawn,<br>on Mill Lane.</div>
<div style="margin-top:10px;display:inline-block;padding:5px 12px;border-radius:999px;background:#f4ead8;color:#6b3f22">Order for pick-up</div></div>
<div style="display:flex;gap:8px;margin:10px 12px">${['Country loaf', 'Seeded rye'].map((t) => `<div style="flex:1;padding:7px 9px;border:1px solid #c9ad86;border-radius:6px">${t}</div>`).join('')}</div>
</body></html>`,
  modern: `<!doctype html><html style="overflow:hidden"><body style="margin:0;font-family:Helvetica,Arial,sans-serif;background:#ffffff;color:#111;font-size:12px">
<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid #e4e4e4"><b style="letter-spacing:.14em">CORNER CRUMB</b><span>Menu · Order</span></div>
<div style="display:grid;grid-template-columns:1.25fr 1fr;gap:12px;padding:14px 12px 10px"><div><div style="font-size:21px;font-weight:700;line-height:1.05">Good bread.<br>Every day.</div><div style="margin-top:6px;color:#444">Fresh from 7:00</div>
<div style="margin-top:10px;display:inline-block;padding:6px 12px;background:#111;color:#fff">Order now</div></div>
<div style="border-radius:4px;min-height:96px;background:radial-gradient(ellipse 30% 20% at 38% 58%,#b9773f 0 96%,transparent 100%),radial-gradient(ellipse 24% 17% at 66% 42%,#d9a061 0 96%,transparent 100%),radial-gradient(ellipse 18% 13% at 58% 74%,#8f5a2e 0 96%,transparent 100%),#efe7dc"></div></div>
<div style="display:flex;gap:1px;background:#e4e4e4;margin:0 12px">${['Sourdough €5', 'Rye €4'].map((t) => `<div style="flex:1;padding:7px 9px;background:#fff">${t}</div>`).join('')}</div>
</body></html>`,
  seasonal: `<!doctype html><html style="overflow:hidden"><body style="margin:0;font-family:Verdana,sans-serif;background:#2f3b2a;color:#f6efe2;font-size:12px">
<div style="padding:8px 12px;display:flex;justify-content:space-between"><b>Corner Crumb</b><span style="color:#e8b45a">Autumn menu</span></div>
<div style="padding:6px 12px 4px;font-size:18px;line-height:1.2">This autumn at<br>the counter</div>
<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;padding:8px 12px">${[
    ['Pumpkin brioche', '#e8b45a'],
    ['Chestnut tart', '#d58a55'],
  ]
    .map(([t, c]) => `<div style="padding:9px;border-radius:6px;background:${c};color:#22180f;min-height:30px">${t}</div>`)
    .join('')}</div>
<div style="margin:2px 12px;color:#e2d8c4">Order ahead for weekends</div>
</body></html>`,
}

function StageFrames({ frames, failed }: { frames: FrameId[]; failed: boolean }) {
  return (
    <section className="vts vtk-stage" aria-label="Stage">
      <figure className="vts-frame is-mockup" aria-label="Landing page, three versions" data-prose-content="">
        <div className="vts-mockups">
          {frames.map((f) => (
            <div key={f} className="vts-mockup-one" role="group" aria-label={FRAME_LABEL[f]}>
              <div className="vts-mockup-head">
                <span className="vts-mockup-label">{FRAME_LABEL[f]}</span>
                <Button kind="ghost" size="sm">
                  Approve
                </Button>
              </div>
              <div className="vts-mockup">
                <iframe className="vts-mockup-frame" title={FRAME_LABEL[f]} sandbox="" srcDoc={FRAME_HTML[f]} />
              </div>
            </div>
          ))}
          {failed && (
            <div className="vts-mockup-one vtk-frame-missing" role="group" aria-label="Clean & modern">
              <div className="vts-mockup-head">
                <span className="vts-mockup-label">Clean & modern</span>
                <ToneMark tone="bad">Failed</ToneMark>
                <Button kind="ghost" size="sm">
                  Retry
                </Button>
              </div>
              <div className="vts-mockup vtk-frame-hole" />
            </div>
          )}
        </div>
      </figure>
    </section>
  )
}

function Caption({ line }: { line: Line }) {
  return (
    <p className={`vt-cap is-${line.side}`}>
      <span className="vt-cap-who">{line.side === 'you' ? 'You' : 'Voice it'}</span>
      <span className="vt-cap-text">{line.text}</span>
    </p>
  )
}

/** The Talk page as the activity-timeline direction draws it, at one fixture moment. */
export function TalkTrack() {
  const state = pickedState()
  const s = shot(state)
  const [open, setOpen] = useState(state === 'working-open')
  const paused = state === 'paused'
  const ended = state === 'ended'
  const saved = pausedFor(s.lanes, s.now)
  const crew = s.rows.flatMap((r) => r.kids ?? [])

  let status: ReactNode
  if (ended || paused) status = null
  else status = <ToneMark tone="live">Live</ToneMark>

  return (
    <div className={`vt vtk${paused ? ' is-paused' : ''}${paused || ended ? ' has-band' : ''}${s.frames.length > 0 ? ' has-frames' : ''}`} data-vstate={state}>
      <div className="vt-sky" aria-hidden="true" />

      {paused && (
        <div className="vtk-band" role="status" aria-label="Voice session paused">
          <span className="vtk-band-word">
            <PauseGlyph />
            Paused
          </span>
          <span className="vtk-band-fact">
            <span className="vtk-time">{clock(saved)}</span>
          </span>
          <span className="vtk-band-fact is-saved">{dollars((saved / 60) * RATE_PER_MIN)} saved</span>
          <TalkTip tip="The paid voice session is paused while the agents work, so it costs nothing now. It picks up where it left off, on Resume, on the word “resume”, or by itself when the work is ready.">
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About the pause">
              <InfoGlyph />
            </button>
          </TalkTip>
          <span className="vtk-band-acts">
            <TalkTip tip="This browser's own voice detection listens for the word; nothing is sent until it hears it." end>
              <span className="vtk-say" tabIndex={0}>
                <MicGlyph />
                “resume”
              </span>
            </TalkTip>
            <Button kind="primary" size="sm">
              Resume
            </Button>
          </span>
        </div>
      )}

      {ended && (
        <div className="vtk-band is-ended" role="status" aria-label="Session ended">
          <span className="vtk-band-word">
            <ToneMark tone="ok" />
            Session ended
          </span>
          <span className="vtk-band-fact">
            Work kept in <b>{PROJECT}</b>
          </span>
          <span className="vtk-band-acts">
            <Button kind="secondary" size="sm" onClick={() => go('/voice/projects?voiceactivity=track')}>
              Projects
            </Button>
            <Button kind="primary" size="sm">
              Resume
            </Button>
          </span>
        </div>
      )}

      <div className="vt-hud">
        <p className="vt-status" role="status">
          {status}
          <span className={`vtk-project${status === null ? ' is-only' : ''}`}>{PROJECT}</span>
        </p>
        <span className="vt-hud-actions">
          {!ended && (
            <Button kind="secondary" size="sm">
              End session
            </Button>
          )}
        </span>
      </div>

      {s.frames.length > 0 && <StageFrames frames={s.frames} failed={state === 'failed-agent'} />}

      <div className="vtk-talk">
        <div className="vtk-core">
          <Orb ring={s.ring} crew={crew} />
        </div>
        <div className="vt-subs-band vtk-subs">
          <div className="vt-subs vtk-lines">
            <div className="vt-captions" role="log" aria-label="Captions">
              {s.lines.map((l, i) => (
                <Caption key={i} line={l} />
              ))}
            </div>
          </div>
          {s.card !== null && <WorkingCard card={s.card} now={s.now} paused={paused} open={open} onToggle={() => setOpen(!open)} />}
        </div>
      </div>

      <Timeline s={s} ended={ended} />

      {!ended && (
        <div className="vt-mic">
          <div className="vt-mic-group" role="group" aria-label="Microphone">
            <Button kind="secondary" icon={<MicGlyph />}>
              Mute
            </Button>
            <span className="vt-mic-state">{paused ? 'Listening in this browser' : 'Open mic'}</span>
          </div>
        </div>
      )}
    </div>
  )
}
