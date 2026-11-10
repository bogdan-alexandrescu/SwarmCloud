// MOCK-HOOK FILE
/**
 * VOICE IT DEBUG MODE, direction "Trace on Telemetry" (owner, 10 November
 * 2026): invented fixture data for the mock-ups at `?vd=telemetry`. The
 * server side is not built; everything here stands in for it.
 *
 *   `?vd=telemetry`        turns the mock on (fixture builds only);
 *   `&debug=off`           the voice settings fixture's debug flag starts off;
 *   `&turn=empty|good`     which planner turn Talk's turn line shows;
 *   `&open=<turn>`         the trace opens that turn in full.
 *
 * Every text is invented: a person planning a small bakery's website.
 */
import { FIXTURE_CLOCK, setFixtureVoiceStage, setFixtureVoiceTelemetry, USE_FIXTURES, VD_QUERY, type VoiceVisualDetail } from '../api'
import type { VoiceCard } from './CardList'
import type { CaptionLine } from './captions'

const params = (): URLSearchParams => new URLSearchParams(VD_QUERY)

/** The mock is on: a fixture build opened with `?vd=telemetry`. */
export const DEBUG_VD: boolean = USE_FIXTURES && params().get('vd') === 'telemetry'

// ---- the voice settings fixture's debug flag ----------------------------------

let debugFlag = params().get('debug') !== 'off'
const listeners = new Set<() => void>()

export function debugOn(): boolean {
  return DEBUG_VD && debugFlag
}

export function setDebugOn(on: boolean): void {
  debugFlag = on
  listeners.forEach((l) => l())
}

export function onDebugChange(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

export const turnParam = (): 'empty' | 'good' => (params().get('turn') === 'empty' ? 'empty' : 'good')
export const openParam = (): number | null => {
  const n = Number(params().get('open'))
  return Number.isInteger(n) && n > 0 ? n : null
}

// ---- the trace ------------------------------------------------------------------

/** A piece of text: plain, or masked as every Voice it text is (an address, a number, a key). */
export type Masked = string | { masked: 'email' | 'phone' | 'key' }

export interface TraceDrawn {
  kind: 'plan' | 'mockup' | 'work' | 'details_input'
  title: string
  /** A mock-up's frames, by label. */
  frames?: string[]
  /** Why the planner refused to draw it; absent when drawn. */
  refused?: string
}

export interface TraceTurn {
  n: number
  at: string
  /** Seconds from the transcript's last final to the voice being told. */
  secs: number
  /** What the planner received: the transcript lines it was given. */
  lines: { who: 'person' | 'voice'; text: Masked[] }[]
  handoff: Masked[] | null
  /** What it read before answering. */
  read: string[]
  drawn: TraceDrawn[]
  /** Why nothing was drawn, when nothing was. */
  none: string | null
  /** What the voice was told back. */
  told: Masked[]
  /** The sideband's events during this turn. */
  events: { pieces: number; finals: number; handoffs: number; barge_ins: number }
}

export interface TraceSession {
  session_id: string
  engine: 'Own speech' | 'GPT-Live'
  started: string
  minutes: number
  turns: TraceTurn[]
  /** The sideband's event counts over the whole session. */
  events: { pieces: number; finals: number; empty_finals: number; handoffs: number; barge_ins: number; replies: number }
}

const at = (minutesBefore: number, seconds = 0): string => new Date(Date.parse(FIXTURE_CLOCK) - minutesBefore * 60_000 + seconds * 1000).toISOString()

const MOCKUP_FRAMES = ['Warm and simple', 'Photo first', 'Order up top']

/** The four-turn session the mock-ups open: one empty transcript, one plan, one mock-up, one refusal. */
export const BAKERY: TraceSession = {
  session_id: 'vs_bakery_1019a',
  engine: 'Own speech',
  started: at(48),
  minutes: 4.2,
  events: { pieces: 41, finals: 4, empty_finals: 1, handoffs: 3, barge_ins: 1, replies: 4 },
  turns: [
    {
      n: 1,
      at: at(48, 12),
      secs: 0.4,
      lines: [{ who: 'person', text: [''] }],
      handoff: null,
      read: [],
      drawn: [],
      none: 'The transcript was empty: 3 speech pieces, one final with no words',
      told: ['Sorry, I missed that. Could you say it again?'],
      events: { pieces: 3, finals: 1, handoffs: 0, barge_ins: 0 },
    },
    {
      n: 2,
      at: at(48, 31),
      secs: 6.8,
      lines: [
        { who: 'person', text: ['I want a small website for the bakery. A home page, the menu, and a way to order cakes for pickup.'] },
        { who: 'voice', text: ['Got it: a home page, the menu and pickup orders.'] },
      ],
      handoff: ['A small bakery website: home page, menu, cake orders for pickup. No delivery.'],
      read: ['Preferences: plain words, short answers', 'Project: Bakery website (new)'],
      drawn: [{ kind: 'plan', title: 'Bakery website' }],
      none: null,
      told: ['The plan is on the cards: three pages and a pickup order form. Want to see the home page?'],
      events: { pieces: 14, finals: 1, handoffs: 1, barge_ins: 0 },
    },
    {
      n: 3,
      at: at(47, 20),
      secs: 21.4,
      lines: [{ who: 'person', text: ['Yes, show me a few ideas for the home page. Warm colours, the sourdough up top.'] }],
      handoff: ['Three home page ideas, warm palette, sourdough as the hero.'],
      read: ['Card: Bakery website, plan v1', 'Preferences: plain words, short answers'],
      drawn: [{ kind: 'mockup', title: 'Home page', frames: MOCKUP_FRAMES }],
      none: null,
      told: ['Three home pages are on the stage. Approve one, or tell me what to change.'],
      events: { pieces: 11, finals: 1, handoffs: 1, barge_ins: 1 },
    },
    {
      n: 4,
      at: at(45, 2),
      secs: 3.1,
      lines: [{ who: 'person', text: ['Send the menu to ', { masked: 'email' }, ' tonight and put the order form live.'] }],
      handoff: ['Publish the order form tonight; send the menu to ', { masked: 'email' }, '.'],
      read: ['Card: Bakery website, plan v1 (proposed)'],
      drawn: [{ kind: 'work', title: 'Publish the order form', refused: 'The plan is not approved yet: nothing is built before it is' }],
      none: null,
      told: ['I can start once you approve the plan. Shall I read it back to you?'],
      events: { pieces: 13, finals: 1, handoffs: 1, barge_ins: 0 },
    },
  ],
}

function quiet(id: string, engine: TraceSession['engine'], minutesBefore: number, minutes: number, turns: { n: number; cards: number }[]): TraceSession {
  return {
    session_id: id,
    engine,
    started: at(minutesBefore),
    minutes,
    events: { pieces: turns.length * 12, finals: turns.length, empty_finals: 0, handoffs: turns.length - 1, barge_ins: 0, replies: turns.length },
    turns: turns.map((t) => ({
      n: t.n,
      at: at(minutesBefore - t.n),
      secs: 5.2,
      lines: [{ who: 'person', text: ['Change the menu prices for the spring bakes.'] }],
      handoff: ['Update the spring menu prices.'],
      read: ['Card: Bakery website, plan v1'],
      drawn: Array.from({ length: t.cards }, (_, i) => ({ kind: 'details_input' as const, title: `Which price list? (${i + 1})` })),
      none: t.cards === 0 ? 'Answered in words: no card needed' : null,
      told: ['Which price list should I use?'],
      events: { pieces: 12, finals: 1, handoffs: 1, barge_ins: 0 },
    })),
  }
}

export const TRACE_SESSIONS: readonly TraceSession[] = [
  BAKERY,
  quiet('vs_bakery_1018b', 'GPT-Live', 1_180, 9.6, [{ n: 1, cards: 0 }, { n: 2, cards: 1 }, { n: 3, cards: 0 }, { n: 4, cards: 1 }, { n: 5, cards: 0 }, { n: 6, cards: 1 }, { n: 7, cards: 0 }]),
  quiet('vs_bakery_1018a', 'Own speech', 1_420, 2.1, [{ n: 1, cards: 0 }, { n: 2, cards: 0 }]),
  quiet('vs_bakery_1017a', 'Own speech', 2_890, 6.3, [{ n: 1, cards: 1 }, { n: 2, cards: 0 }, { n: 3, cards: 1 }, { n: 4, cards: 0 }, { n: 5, cards: 0 }]),
]

export const drawnCount = (t: TraceTurn): number => t.drawn.filter((d) => d.refused === undefined).length
export const sessionCards = (s: TraceSession): number => s.turns.reduce((a, t) => a + drawnCount(t), 0)

/** Masked pieces as the trace prints them in plain text (a tooltip). */
export const plain = (m: readonly Masked[]): string => m.map((p) => (typeof p === 'string' ? p : `[${p.masked}]`)).join('')

// ---- Talk: the live turn, the stage, the cards, the captions ---------------------

/** The turn Talk's line shows: the empty one (turn 1) or the mock-up (turn 3). */
export function liveTurn(): TraceTurn {
  const n = turnParam() === 'empty' ? 1 : 3
  return BAKERY.turns.find((t) => t.n === n) ?? BAKERY.turns[0] ?? EMPTY_TURN
}

const EMPTY_TURN: TraceTurn = { n: 1, at: FIXTURE_CLOCK, secs: 0, lines: [], handoff: null, read: [], drawn: [], none: null, told: [], events: { pieces: 0, finals: 0, handoffs: 0, barge_ins: 0 } }

export function liveCaptions(): CaptionLine[] {
  if (turnParam() === 'empty') {
    return [
      { id: 'vd-1', side: 'you', text: '…', final: true },
      { id: 'vd-2', side: 'agent', text: 'Sorry, I missed that. Could you say it again?', final: true },
    ]
  }
  return [
    { id: 'vd-1', side: 'you', text: 'Yes, show me a few ideas for the home page. Warm colours, the sourdough up top.', final: true },
    { id: 'vd-2', side: 'agent', text: 'Three home pages are on the stage. Approve one, or tell me what to change.', final: true },
  ]
}

const page = (body: string, bg: string, ink: string): string =>
  `<!doctype html><html><head><style>*{box-sizing:border-box}body{margin:0;font:11px/1.4 Georgia,serif;background:${bg};color:${ink}}` +
  `nav{display:flex;gap:10px;align-items:center;padding:10px 14px;font:600 9px/1 system-ui,sans-serif}nav b{font:700 13px/1 Georgia,serif;margin-right:auto;white-space:nowrap}` +
  `.btn{display:inline-block;padding:6px 11px;border-radius:999px;background:#8a3b12;color:#fff;font:600 9px/1 system-ui,sans-serif;white-space:nowrap}` +
  `.hero{padding:14px}.hero h1{font-size:19px;line-height:1.05;margin:0 0 6px}.loaf{border-radius:14px;background:radial-gradient(circle at 40% 35%,#e8b46a,#a8632a 70%,#6d3a15);}` +
  `.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;padding:0 14px 14px}.tile{border-radius:8px;background:rgba(0,0,0,.06);padding:8px 6px;font:9px/1.3 system-ui,sans-serif}` +
  `</style></head><body>${body}</body></html>`

const NAV = '<nav><b>Crumb &amp; Co.</b><span>Menu</span><span>Order</span><span>Visit</span></nav>'

const FRAME_HTML = [
  page(
    `${NAV}<div class="hero" style="display:grid;grid-template-columns:1.2fr 1fr;gap:10px;align-items:center"><div><h1>Sourdough, baked at dawn</h1><p>Whole cakes to order, ready for pickup from 7 am.</p><span class="btn">Order a cake</span></div><div class="loaf" style="height:92px"></div></div>` +
      `<div class="grid"><div class="tile">Country sourdough</div><div class="tile">Seeded rye</div><div class="tile">Cardamom buns</div></div>`,
    '#fbf3e7',
    '#3b2414',
  ),
  page(
    `<div class="loaf" style="height:150px;border-radius:0;display:flex;flex-direction:column;justify-content:flex-end;padding:14px;color:#fff"><b style="font-size:13px">Crumb &amp; Co.</b><h1 style="font-size:20px;line-height:1.05;margin:4px 0 0">The sourdough is out of the oven</h1></div>` +
      `<div class="hero" style="display:flex;gap:8px;align-items:center"><span class="btn">See the menu</span><span style="font:9px system-ui,sans-serif">Open 7 am to 3 pm</span></div><div class="grid"><div class="tile">Sourdough</div><div class="tile">Rye</div><div class="tile">Buns</div></div>`,
    '#fff8ef',
    '#3b2414',
  ),
  page(
    `${NAV}<div style="margin:0 14px;padding:8px 10px;gap:8px;border-radius:8px;background:#8a3b12;color:#fff;font:600 9px/1.3 system-ui,sans-serif;display:flex;justify-content:space-between;align-items:center"><span>Order a whole cake for Saturday pickup</span><span class="btn" style="background:#fff;color:#8a3b12">Order</span></div>` +
      `<div class="hero" style="display:grid;grid-template-columns:1fr 1.2fr;gap:10px;align-items:center"><div class="loaf" style="height:84px"></div><div><h1 style="font-size:16px">Sourdough every morning</h1><p>Open Tuesday to Sunday.</p></div></div>`,
    '#f6ead8',
    '#3b2414',
  ),
]

/** The stage's one visual: the bakery home page mock-up of three frames. */
export const BAKERY_MOCKUP: VoiceVisualDetail = {
  visual: {
    visual_id: 'vv_bakery_home',
    kind: 'mockup',
    title: 'Home page',
    version: 1,
    source: null,
    parts: MOCKUP_FRAMES.map((t, i) => ({ part_id: `home-${i + 1}`, title: t })),
    lines: null,
    created_by: 'planner',
    updated_at: at(47),
    approved: null,
  },
  body: JSON.stringify({ frames: MOCKUP_FRAMES.map((label, i) => ({ id: `home-${i + 1}`, label, html: FRAME_HTML[i] })) }),
  notes: [],
}

/** The cards beside Talk: the bakery's plan card. */
export const BAKERY_CARDS: readonly VoiceCard[] = [
  {
    card_id: 'vc_bakery_plan',
    kind: 'plan',
    project_id: 'vp_bakery',
    session_id: BAKERY.session_id,
    version: 1,
    state: 'proposed',
    title: 'Bakery website',
    created_by: 'planner',
    references: null,
    summary: 'Three pages and a pickup order form, $14 reserved.',
    at: at(48),
    body: {
      goal: 'A home page, the menu and cake orders for pickup.',
      size: 'feature',
      waves: [
        {
          name: 'Wave 1',
          lanes: [
            { name: 'pages', territory: ['site/pages'], reservation_usd: 6, state: null },
            { name: 'order form', territory: ['site/order'], reservation_usd: 8, state: null },
          ],
        },
      ],
      reserved_usd: 14,
      session_cap_usd: 100,
      day_cap_usd: 300,
    },
  },
]

// The first turn heard nothing, so nothing is on the stage yet.
if (DEBUG_VD) setFixtureVoiceStage(turnParam() === 'empty' ? [] : [BAKERY_MOCKUP])

export const sessionParam = (): string | null => params().get('s')

// ---- Telemetry: the same sessions, as the telemetry routes would serve them ------

const dayOf = (iso: string): string => iso.slice(0, 10)

function telemetrySession(s: TraceSession, i: number): Record<string, unknown> {
  const p = (p50: number, p90: number, max: number) => ({ p50, p90, max })
  const own = s.engine === 'Own speech'
  return {
    session_id: s.session_id,
    person: null,
    first_at: s.started,
    last_at: new Date(Date.parse(s.started) + s.minutes * 60_000).toISOString(),
    day: dayOf(s.started),
    minutes: s.minutes,
    utterances: s.events.finals,
    syntheses: s.events.replies,
    stt_errors: 0,
    tts_errors: 0,
    percentiles: {
      stt_final_ms: p(310 + i * 20, 420 + i * 30, 610),
      stt_first_partial_ms: p(180, 240, 330),
      stt_rtf: p(0.21, 0.27, 0.34),
      tts_first_audio_ms: p(260 + i * 15, 380, 520),
      tts_rtf: p(0.18, 0.22, 0.3),
    },
    peaks: own
      ? { gpu_util_pct: 64, gpu_mem_used_mib: 17_820, cpu_pct: 38, memory_used_mib: 9_410, disk_used_gb: 41, vllm_queue: 1, kv_cache_pct: 22 }
      : { gpu_util_pct: null, gpu_mem_used_mib: null, cpu_pct: 12, memory_used_mib: 1_830, disk_used_gb: null, vllm_queue: null, kv_cache_pct: null },
    start_phases: own ? { 'start.boot': 4.1, 'start.server': 2.2, 'start.fetch': 6.8, 'start.load': 11.4, 'start.warmup': 3.6 } : {},
    machine_usd: own ? Number((s.minutes * 0.031).toFixed(2)) : Number((s.minutes * 0.06).toFixed(2)),
    machine_usd_reason: null,
    stockouts: 0,
    stockouts_reason: null,
  }
}

function telemetryDays(): Record<string, unknown>[] {
  const last = Date.parse(FIXTURE_CLOCK)
  return Array.from({ length: 30 }, (_, k) => {
    const day = dayOf(new Date(last - (29 - k) * 86_400_000).toISOString())
    const mine = TRACE_SESSIONS.filter((s) => dayOf(s.started) === day)
    const sessions = mine.length + (k % 5 === 1 ? 1 : 0)
    const minutes = mine.reduce((a, s) => a + s.minutes, 0) + (k % 5 === 1 ? 5.4 : 0)
    return {
      day,
      sessions,
      minutes,
      utterances: mine.reduce((a, s) => a + s.events.finals, 0) + (k % 5 === 1 ? 6 : 0),
      syntheses: mine.reduce((a, s) => a + s.events.replies, 0) + (k % 5 === 1 ? 6 : 0),
      stt_errors: 0,
      tts_errors: 0,
      stt_final_ms_p90: sessions > 0 ? 430 : null,
      tts_first_audio_ms_p90: sessions > 0 ? 390 : null,
      machine_usd: Number((minutes * 0.04).toFixed(2)),
      machine_usd_reason: null,
      stockouts: 0,
      stockouts_reason: null,
    }
  })
}

if (DEBUG_VD) {
  setFixtureVoiceTelemetry((url) => {
    const path = url.split('?')[0] ?? ''
    if (path.endsWith('/telemetry/days')) return { days: telemetryDays() }
    if (path.endsWith('/telemetry/sessions')) return { sessions: TRACE_SESSIONS.map(telemetrySession) }
    const id = decodeURIComponent(path.split('/').at(-1) ?? '')
    const i = TRACE_SESSIONS.findIndex((s) => s.session_id === id)
    const s = TRACE_SESSIONS[i]
    return s === undefined ? null : { session: telemetrySession(s, i), samples: [] }
  })
}
