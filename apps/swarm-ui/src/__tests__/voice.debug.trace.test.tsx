// VOICE IT DEBUG MODE, TELEMETRY'S TRACE (the owner's pick of 10 November
// 2026, "Trace on Telemetry"): the trace card on Voice it › Telemetry, on the
// live path (USE_FIXTURES off) against a stubbed `fetch`.
//
//   * the recent traced sessions and the opened one draw every served field,
//     each planner turn in full: received, read, drew or refused and why,
//     told the voice, the sideband counts;
//   * a turn that carries a `tier` shows its name (llm-providers.md §5.4:
//     Plan, Shared key, Voice it key); one that does not shows none;
//   * a failed read says it was not read, with Retry, never an empty trace;
//     no traced session is one line;
//   * a reader the route refuses gets no card;
//   * the card's rules hold a 390 phone.
//
// BREAK IT: drop `tier` in the normaliser, draw an empty trace on a 503,
// draw the card on a 403, or leave out a served field (the handoff, a
// refusal's reason, the empty finals).

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SHEET from '../styles/voice-telemetry.css?raw'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

const pct = (p50: number | null, p90: number | null, max: number | null) => ({ p50, p90, max })

/** One telemetry session, so the page draws its timeline. */
const TEL_SESSION = {
  session_id: 'vs_0002',
  tenant_id: 'ws-one',
  person: 'owner@example.com',
  generation: 1,
  first_at: '2026-11-10T09:00:00+00:00',
  last_at: '2026-11-10T09:02:00+00:00',
  day: '2026-11-10',
  minutes: 2,
  utterances: 4,
  syntheses: 3,
  stt_errors: 0,
  tts_errors: 0,
  stt_audio_s: 10,
  tts_audio_s: 12,
  metrics_samples: 24,
  percentiles: {
    stt_final_ms: pct(300, 400, 500),
    stt_first_partial_ms: pct(100, 150, 200),
    stt_rtf: pct(0.1, 0.1, 0.1),
    tts_first_audio_ms: pct(200, 250, 300),
    tts_rtf: pct(0.2, 0.2, 0.2),
  },
  peaks: { gpu_util_pct: 50, gpu_mem_used_mib: 1024, cpu_pct: 20, memory_used_mib: 2048, disk_used_gb: 10, vllm_queue: 0, kv_cache_pct: 5 },
  start_phases: {},
  machine_usd: 1,
  machine_usd_reason: null,
  stockouts: 0,
  stockouts_reason: null,
}
const TEL_DAYS = [{ day: '2026-11-10', sessions: 1, minutes: 2, utterances: 4, syntheses: 3, stt_errors: 0, tts_errors: 0, stt_final_ms_p90: 400, tts_first_audio_ms_p90: 250, machine_usd: 1, machine_usd_reason: null, stockouts: 0, stockouts_reason: null }]

/** Two turns as `served_turn` sends them: one drew and refused and carries a tier, one drew nothing and carries none. */
const TURNS = [
  {
    n: 1,
    at: '2026-11-10T09:00:04+00:00',
    secs: 3.25,
    lines: [
      { who: 'person', text: ['Draw the export page'] },
      { who: 'voice', text: ['On it'] },
    ],
    handoff: ['the person wants the export page drawn'],
    read: ['read_file: apps/web/export.tsx', 'list_runs'],
    drawn: [
      { kind: 'mockup', title: 'Export page', frames: ['Empty', 'Filled'] },
      { kind: 'work', title: 'Export lane', refused: 'the budget is spent' },
    ],
    none: null,
    none_code: null,
    told: ['A mock-up is on screen'],
    events: { pieces: 4, finals: 3, empty_finals: 2, handoffs: 1, barge_ins: 0 },
    tier: 'api_key',
  },
  {
    n: 2,
    at: '2026-11-10T09:00:20+00:00',
    secs: null,
    lines: [{ who: 'person', text: ['What is running now'] }],
    handoff: null,
    read: [],
    drawn: [],
    none: 'Answered in words: no card needed.',
    none_code: 'answered_in_words',
    told: ['Two runs are going'],
    events: { pieces: 2, finals: 1, empty_finals: 0, handoffs: 0, barge_ins: 1 },
  },
]

const TRACE = {
  session_id: 'vs_0002',
  project_id: 'vp_0002',
  engine: 'Own speech',
  engine_code: 'own',
  started: '2026-11-10T09:00:00+00:00',
  minutes: 1.5,
  turns: TURNS,
  events: { pieces: 6, finals: 4, empty_finals: 2, handoffs: 1, barge_ins: 1, replies: 5 },
  scope: 'platform',
}

const OLDER = {
  ...TRACE,
  session_id: 'vs_0001',
  project_id: null,
  engine: 'GPT-Live',
  engine_code: 'gpt-live',
  started: '2026-11-09T08:00:00+00:00',
  minutes: 0.5,
  turns: [{ ...TURNS[1], n: 1, tier: 'plan' }],
  events: { pieces: 1, finals: 1, empty_finals: 0, handoffs: 0, barge_ins: 0, replies: 1 },
}

const LIST = {
  sessions: [
    { session_id: 'vs_0002', project_id: 'vp_0002', engine: 'Own speech', engine_code: 'own', started: '2026-11-10T09:00:00+00:00', turns: 2, cards: 1, refused: 1 },
    { session_id: 'vs_0001', project_id: null, engine: 'GPT-Live', engine_code: 'gpt-live', started: '2026-11-09T08:00:00+00:00', turns: 1, cards: 0, refused: 0 },
  ],
  scope: 'platform',
}

interface Serve {
  list?: () => Response
  one?: (id: string) => Response
  telemetry?: unknown[]
}

/** A `fetch` answering Telemetry's reads and the trace's; `list` and `one` answer the trace reads instead. */
function serve(opts: Serve = {}): string[] {
  const calls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    if (url.includes('/v1/voice/trace/sessions/')) {
      const id = decodeURIComponent(url.split('/v1/voice/trace/sessions/')[1]!.split('?')[0]!)
      if (opts.one) return opts.one(id)
      const found = [TRACE, OLDER].find((s) => s.session_id === id)
      return found ? json(200, found) : json(404, { code: 'not_found', message: 'no trace' })
    }
    if (url.includes('/v1/voice/trace/sessions')) return opts.list ? opts.list() : json(200, LIST)
    if (url.includes('/v1/voice/telemetry/sessions/')) return json(200, { session: TEL_SESSION, samples: [], scope: 'platform', tz: 'UTC' })
    if (url.includes('/v1/voice/telemetry/sessions')) return json(200, { sessions: opts.telemetry ?? [TEL_SESSION], scope: 'platform', tz: 'UTC' })
    if (url.includes('/v1/voice/telemetry/days')) return json(200, { days: opts.telemetry?.length === 0 ? [] : TEL_DAYS, scope: 'platform', tz: 'UTC' })
    return json(404, { code: 'not_found', message: 'no such route' })
  }) as unknown as typeof fetch
  return calls
}

class FakeSource {
  readonly url: string
  readyState = 1
  constructor(url: string) {
    this.url = url
  }
  addEventListener() {}
  close() {
    this.readyState = 2
  }
}

async function page() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { VoiceTelemetry } = await import('../voice/Telemetry')
  return render(<VoiceTelemetry />)
}

const card = (): HTMLElement | null => document.querySelector<HTMLElement>('[data-card="trace"]')
const turnRow = (n: number): HTMLElement => card()!.querySelector<HTMLElement>(`tr[data-turn="${n}"]`)!
const openTurn = (n: number) => fireEvent.click(within(turnRow(n)).getByRole('button'))
const detail = (n: number): HTMLElement => document.getElementById(`vdg-turn-${n}`)!

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeSource)
  window.history.replaceState(null, '', '/voice/telemetry')
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('the trace normaliser keeps the tier', () => {
  it('keeps a served tier and adds none when the turn carries none', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const api = await import('../api')
    const t = api.normVoiceTrace(TRACE)
    expect(t.turns[0]!.tier).toBe('api_key')
    expect('tier' in t.turns[1]!).toBe(false)
    expect(t).toEqual(TRACE)
  })
})

describe('the trace card draws every served field', () => {
  it('lists the recent traced sessions: when, engine, turns, cards, refusals, the ids behind the tooltip', async () => {
    serve()
    await page()
    const list = await screen.findByRole('list', { name: 'Traced sessions' })
    const rows = within(list).getAllByRole('button')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.textContent).toContain('Own speech')
    expect(rows[0]!.textContent).toContain('2 turns')
    expect(rows[0]!.textContent).toContain('1 card')
    expect(rows[0]!.textContent).toContain('1 refused')
    expect(rows[0]!.textContent).toMatch(/Nov 10|09:00/)
    expect(rows[0]!.getAttribute('title')).toBe('vs_0002 · vp_0002')
    expect(rows[0]!.dataset.engine).toBe('own')
    expect(rows[1]!.textContent).toContain('GPT-Live')
    expect(rows[1]!.textContent).toContain('1 turn')
    expect(rows[1]!.textContent).toContain('0 cards')
    expect(rows[1]!.textContent).not.toContain('refused')
    expect(rows[1]!.getAttribute('title')).toBe('vs_0001')
  })

  it('opens the page’s session when it has a trace: its head, its sideband and every turn as a row', async () => {
    serve()
    await page()
    const head = await within(await waitCard()).findByRole('heading', { level: 3 })
    expect(head.textContent).toContain('Own speech')
    expect(head.textContent).toContain('1.5 min')
    expect(head.getAttribute('title')).toBe('vs_0002 · vp_0002')
    const events = within(card()!).getByLabelText('Sideband events').textContent
    for (const s of ['6 speech pieces', '4 finals (2 empty)', '1 handoffs', '1 barge-in', '5 replies']) expect(events).toContain(s)
    const one = turnRow(1).textContent
    expect(one).toContain('09:00')
    expect(one).toContain('Draw the export page')
    expect(one).toContain('Mock-up · 2 frames')
    expect(one).toContain('Refused · Work card')
    expect(one).toContain('the budget is spent')
    expect(one).toContain('3.3 s')
    const two = turnRow(2).textContent
    expect(two).toContain('What is running now')
    expect(two).toContain('No card')
    expect(two).toContain('—')
  })

  it('opens a turn in full: received, handoff, read, drew and refused, told, the sideband', async () => {
    serve()
    await page()
    await within(await waitCard()).findByRole('heading', { level: 3 })
    openTurn(1)
    const d = detail(1).textContent
    for (const s of [
      'Draw the export page',
      'On it',
      'the person wants the export page drawn',
      'read_file: apps/web/export.tsx',
      'list_runs',
      'Export page',
      'Empty',
      'Filled',
      'Export lane',
      'the budget is spent',
      'A mock-up is on screen',
      '4 speech pieces',
      '3 finals (2 empty)',
      '1 handoff',
    ])
      expect(d).toContain(s)
    openTurn(2)
    expect(document.getElementById('vdg-turn-1')).toBeNull()
    const two = detail(2)
    expect(two.textContent).toContain('Drew nothing')
    expect(two.textContent).toContain('Answered in words: no card needed.')
    expect(two.querySelector('[data-none]')!.getAttribute('data-none')).toBe('answered_in_words')
    expect(two.textContent).toContain('Two runs are going')
    expect(two.textContent).toContain('1 barge-in')
  })

  it('opens another traced session when it is picked', async () => {
    serve()
    await page()
    await within(await waitCard()).findByRole('heading', { level: 3 })
    fireEvent.click(within(screen.getByRole('list', { name: 'Traced sessions' })).getAllByRole('button')[1]!)
    expect((await within(await waitCard()).findByRole('heading', { level: 3, name: /GPT-Live/ })).textContent).toContain('0.5 min')
  })

  it('opens the session and turn Talk’s Trace names (`?s=&open=`)', async () => {
    window.history.replaceState(null, '', '/voice/telemetry?s=vs_0001&open=1')
    serve()
    await page()
    await within(await waitCard()).findByRole('heading', { level: 3, name: /GPT-Live/ })
    expect(detail(1).textContent).toContain('What is running now')
  })
})

describe('the tier', () => {
  it('names a turn’s tier, and draws none for a turn that carries none', async () => {
    serve()
    await page()
    await within(await waitCard()).findByRole('heading', { level: 3 })
    openTurn(1)
    expect(detail(1).querySelector('[data-tier]')!.textContent).toBe('Shared key')
    expect(detail(1).textContent).toContain('Tier')
    openTurn(2)
    expect(detail(2).querySelector('[data-tier]')).toBeNull()
    expect(detail(2).textContent).not.toContain('Tier')
  })

  it('names Plan and Voice it key by their §5.4 names', async () => {
    const { TIER_NAMES } = await import('../voice/DebugTrace')
    expect(TIER_NAMES).toEqual({ plan: 'Plan', api_key: 'Shared key', voice_dedicated: 'Voice it key' })
  })
})

describe('a failed read is not an empty trace', () => {
  it('says the list was not read, with Retry, and Retry reads again', async () => {
    let failing = true
    serve({ list: () => (failing ? json(503, { code: 'unavailable', message: 'the store is down' }) : json(200, LIST)) })
    await page()
    const alert = await within(await waitCard()).findByRole('alert')
    expect(alert.textContent).toContain('Trace not read · 503')
    expect(card()!.textContent).not.toContain('No traced sessions')
    expect(card()!.querySelector('table')).toBeNull()
    failing = false
    await act(async () => {
      fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    })
    await screen.findByRole('list', { name: 'Traced sessions' })
    expect(within(card()!).queryByRole('alert')).toBeNull()
  })

  it('says one session was not read, and draws no turns', async () => {
    serve({ one: () => json(500, { code: 'internal', message: 'boom' }) })
    await page()
    const alert = await within(await waitCard()).findByRole('alert')
    expect(alert.textContent).toContain('Trace not read · 500')
    expect(card()!.querySelector('table')).toBeNull()
    expect(card()!.textContent).not.toContain('No traced sessions')
  })

  it('draws one line when no session has a trace', async () => {
    serve({ list: () => json(200, { sessions: [], scope: 'platform' }) })
    await page()
    const c = await waitCard()
    expect(await within(c).findByText('No traced sessions')).toBeTruthy()
    expect(c.querySelector('table, ol')).toBeNull()
  })
})

describe('the access rule', () => {
  for (const status of [401, 403]) {
    it(`draws no card for a reader the route refuses (${status})`, async () => {
      const calls = serve({ list: () => json(status, { code: 'forbidden', message: 'not a Voice it person' }) })
      await page()
      await screen.findByRole('heading', { name: 'Models' })
      await act(async () => {})
      expect(calls.some((u) => u.includes('/v1/voice/trace/sessions'))).toBe(true)
      expect(card()).toBeNull()
      expect(screen.queryByText(/Trace not read/)).toBeNull()
    })
  }
})

describe('a page with no telemetry sessions', () => {
  it('still draws the traced sessions, and adds nothing when there are none', async () => {
    serve({ telemetry: [] })
    await page()
    await screen.findByText('No sessions yet')
    expect(await screen.findByRole('list', { name: 'Traced sessions' })).toBeTruthy()
  })

  it('adds no line to "No sessions yet" when nothing is traced', async () => {
    serve({ telemetry: [], list: () => json(200, { sessions: [], scope: 'platform' }) })
    await page()
    await screen.findByText('No sessions yet')
    await act(async () => {})
    expect(card()).toBeNull()
  })
})

describe('nothing is wider than a 390 phone', () => {
  it('keeps the trace card’s rules in the sheet Telemetry loads, each phone-safe', () => {
    const body = SHEET.replace(/\/\*[\s\S]*?\*\//g, '')
    for (const rule of ['.vdg-trace', '.vdg-turns', '.vdg-detail', '.vdg-trow-detail', '.vdg-chip.is-refused']) expect(body).toContain(rule)
    const phone = /@media \(max-width: 760px\) \{([\s\S]*)\}\s*$/.exec(body)?.[1] ?? ''
    expect(phone).toContain('.vdg-turns thead')
    expect(phone).toMatch(/\.vdg-detail \{ grid-template-columns: minmax\(0, 1fr\)/)
    for (const m of body.matchAll(/(?:^|[;{\s])(width|min-width|flex-basis)\s*:\s*([^;}]+)/g)) {
      const n = [...m[2]!.matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((a, x) => a + Number(x[1]), 0)
      expect(n, `${m[1]}: ${m[2]}`).toBeLessThanOrEqual(300)
    }
  })
})

/** The trace card, once the page has drawn it. */
async function waitCard(): Promise<HTMLElement> {
  await screen.findByRole('heading', { name: 'Trace' })
  return card()!
}
