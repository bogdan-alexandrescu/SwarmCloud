// VOICE IT DEBUG MODE, the console's calls (the owner's pick of 10 November
// 2026, "Trace on Telemetry"): `debug` in the settings view and its PATCH,
// the two trace reads (swarm-api routes/voice_trace.py, shapes from
// voice/trace.py) and the `trace.turn` stream event (voice/stream.py).
//
//   * the settings view reads `settings.debug`, and only a served `true` is on;
//   * a debug save is `PATCH {version, debug}`;
//   * the trace normalisers keep every field the server sends, snake_case;
//   * a failed trace read is an error result, never an empty trace;
//   * the fixture build serves no traces and debug off.
//
// BREAK IT: read `debug` truthily, drop a served field from a normaliser, or
// answer a failed read with `[]`.

import { afterEach, describe, expect, it, vi } from 'vitest'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The live path: USE_FIXTURES is decided at import. */
async function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../api')
}

async function fixtures() {
  vi.stubEnv('VITE_LIVE', '')
  vi.resetModules()
  return import('../api')
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

/** Two turns as `served_turn` sends them: one drew a card and refused one, one drew nothing. */
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
    events: { pieces: 4, finals: 1, empty_finals: 0, handoffs: 1, barge_ins: 0 },
  },
  {
    n: 2,
    at: '2026-11-10T09:00:20+00:00',
    secs: null,
    lines: [],
    handoff: null,
    read: [],
    drawn: [],
    none: 'Answered in words: no card needed.',
    none_code: 'answered_in_words',
    told: [''],
    events: { pieces: 2, finals: 1, empty_finals: 0, handoffs: 0, barge_ins: 1 },
  },
]

const SESSION = {
  session_id: 'vs_0001',
  project_id: 'vp_0001',
  engine: 'Own speech',
  engine_code: 'own',
  started: '2026-11-10T09:00:00+00:00',
  minutes: 1.5,
  turns: TURNS,
  events: { pieces: 6, finals: 2, empty_finals: 0, handoffs: 1, barge_ins: 1, replies: 2 },
  scope: 'platform',
}

const LIST = {
  sessions: [
    { session_id: 'vs_0001', project_id: 'vp_0001', engine: 'Own speech', engine_code: 'own', started: '2026-11-10T09:00:00+00:00', turns: 2, cards: 1, refused: 1 },
    { session_id: 'vs_0000', project_id: null, engine: 'GPT-Live', engine_code: 'gpt-live', started: null, turns: 0, cards: 0, refused: 0 },
  ],
  scope: 'platform',
}

describe('the settings view carries debug mode', () => {
  it('reads settings.debug, on only for a served true', async () => {
    const api = await live()
    const on = api.normVoiceSettings({ settings: { engine: 'own', debug: true }, version: 4 })
    expect(on.debug).toBe(true)
    expect(api.normVoiceSettings({ settings: { engine: 'own', debug: false } }).debug).toBe(false)
    expect(api.normVoiceSettings({ settings: { engine: 'own' } }).debug).toBe(false)
    expect(api.normVoiceSettings({ settings: { engine: 'own', debug: 'true' } }).debug).toBe(false)
  })

  it('saves it as PATCH {version, debug}', async () => {
    const api = await live()
    const sent: { method: string; body: unknown }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
      sent.push({ method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? JSON.parse(init.body) : null })
      return json(200, { settings: { engine: 'own', debug: true }, version: 5 })
    })
    const r = await api.saveVoiceSettings(4, { debug: true })
    expect(sent).toEqual([{ method: 'PATCH', body: { version: 4, debug: true } }])
    expect(r.status === 'ok' && r.data.debug).toBe(true)
  })
})

describe('the trace normalisers keep every served field', () => {
  it('keeps a session trace as served, every turn whole', async () => {
    const api = await live()
    expect(api.normVoiceTrace(SESSION)).toEqual(SESSION)
  })

  it('keeps the traced sessions as served', async () => {
    const api = await live()
    expect(api.normVoiceTraceSessions(LIST)).toEqual(LIST)
  })

  it('keeps the trace.turn event: number, drawn, refused and the no-cards code', async () => {
    const api = await live()
    const ev = {
      type: 'trace.turn',
      seq: 9,
      at: '2026-11-10T09:00:04+00:00',
      n: 3,
      drawn: [{ kind: 'work', title: 'Export lane' }],
      refused: [{ kind: 'plan', title: null, reason: 'the budget is spent' }],
      none: null,
    }
    expect(api.normVoiceTraceTurnEvent(ev)).toEqual(ev)
    expect(api.normVoiceTraceTurnEvent({ ...ev, drawn: [], refused: [], none: 'empty_transcript' })?.none).toBe('empty_transcript')
    expect(api.normVoiceTraceTurnEvent({ ...ev, n: '3' })).toBeNull()
  })
})

describe('a failed trace read is not an empty trace', () => {
  it('answers the error for the list and for one session', async () => {
    const api = await live()
    const urls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      urls.push(String(input))
      return json(403, { code: 'forbidden', message: 'not a Voice it person' })
    })
    const list = await api.loadVoiceTraceSessions()
    const one = await api.loadVoiceTrace('vs_0001')
    expect(list.status).toBe('error')
    expect(one.status).toBe('error')
    expect(urls[0]).toContain('/v1/voice/trace/sessions')
    expect(urls[1]).toContain('/v1/voice/trace/sessions/vs_0001')
  })

  it('answers the served trace when the read succeeds', async () => {
    const api = await live()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => json(200, String(input).includes('vs_0001') ? SESSION : LIST))
    const list = await api.loadVoiceTraceSessions()
    const one = await api.loadVoiceTrace('vs_0001')
    expect(list.status === 'ok' && list.data).toEqual(LIST)
    expect(one.status === 'ok' && one.data).toEqual(SESSION)
  })
})

describe('the fixture build invents no trace', () => {
  it('serves no traced session, an empty trace and debug off', async () => {
    const api = await fixtures()
    if (!api.USE_FIXTURES) throw new Error('the fixture build did not load')
    const list = await api.loadVoiceTraceSessions()
    expect(list.status === 'ok' && list.data.sessions).toEqual([])
    const one = await api.loadVoiceTrace('vs_0001')
    expect(one.status === 'ok' && one.data.turns).toEqual([])
    const settings = await api.loadVoiceSettings()
    expect(settings.status === 'ok' && settings.data.debug).toBe(false)
  })
})
