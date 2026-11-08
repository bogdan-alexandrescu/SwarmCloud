// VOICE IT › TELEMETRY: the cards and the timeline over swarm-api's telemetry
// routes (routes/voice_telemetry.py), on the live path (USE_FIXTURES off)
// against a stubbed `fetch` and a stubbed `EventSource`.
//
//   * every metric is drawn from the shape speech_telemetry.py serves:
//     end of speech to final words, first partial, real-time factors, first
//     audio, the machine's peaks, model load and warm-up, the start's phases,
//     the live session's utterances and readings, minutes, cost, stock-outs;
//   * the session's cost against the $100 cap and its day's against $300;
//   * a read that fails says so and offers Retry, and Retry reads again;
//   * no sessions is one line;
//   * the Telemetry tab is a platform admin's only.
//
// BREAK IT: draw a figure the server did not serve (a zero for a null
// `machine_usd`), drop a percentile row, draw the caps from the day's own
// figure, draw an empty timeline on a failed read, or show the tab to a
// member.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SHEET from '../styles/voice-telemetry.css?raw'

const pct = (p50: number | null, p90: number | null, max: number | null) => ({ p50, p90, max })

function session(id: string, over: Record<string, unknown> = {}) {
  return {
    session_id: id,
    tenant_id: 'ws-one',
    person: 'owner@example.com',
    generation: 3,
    first_at: '2026-11-07T10:00:00+00:00',
    last_at: '2026-11-07T10:20:00+00:00',
    day: '2026-11-07',
    minutes: 20,
    utterances: 41,
    syntheses: 38,
    stt_errors: 1,
    tts_errors: 2,
    stt_audio_s: 120,
    tts_audio_s: 200,
    metrics_samples: 240,
    percentiles: {
      stt_final_ms: pct(312, 587, 941),
      stt_first_partial_ms: pct(131, 219, 402),
      stt_rtf: pct(0.07, 0.11, 0.19),
      tts_first_audio_ms: pct(228, 341, 512),
      tts_rtf: pct(0.21, 0.26, 0.33),
    },
    peaks: { gpu_util_pct: 83, gpu_mem_used_mib: 15258, cpu_pct: 41, memory_used_mib: 7475, disk_used_gb: 96.4, vllm_queue: 2, kv_cache_pct: 37 },
    start_phases: { 'start.warmup': 4.2, 'start.boot': 61.5, 'start.load': 18.3, 'start.server': 12.1 },
    machine_usd: 12.5,
    machine_usd_reason: null,
    stockouts: 1,
    stockouts_reason: null,
    ...over,
  }
}

function day(d: string, over: Record<string, unknown> = {}) {
  return {
    day: d, sessions: 0, minutes: 0, utterances: 0, syntheses: 0, stt_errors: 0, tts_errors: 0,
    stt_final_ms_p90: null, tts_first_audio_ms_p90: null, machine_usd: 0, machine_usd_reason: null, stockouts: 0, stockouts_reason: null,
    ...over,
  }
}

const DAYS = [
  day('2026-11-06', { sessions: 1, minutes: 9, utterances: 12, machine_usd: 7.25, stockouts: 1 }),
  day('2026-11-07', { sessions: 2, minutes: 32, utterances: 60, stt_final_ms_p90: 590, machine_usd: 40, stockouts: 1 }),
  day('2026-11-08'),
]
const SESSIONS = [session('vs-newest'), session('vs-older', { first_at: '2026-11-06T09:00:00+00:00', day: '2026-11-06', machine_usd: null, machine_usd_reason: 'ledger_unreadable', stockouts: 0 })]

const SAMPLES = [
  { seq: '0000000000001-3-aaaaaa', kind: 'stt', at: '2026-11-07T10:01:00+00:00', generation: 3, data: { final_ms: 300, first_partial_ms: 120, rtf: 0.07, ok: true } },
  { seq: '0000000000002-3-aaaaaa', kind: 'stt', at: '2026-11-07T10:02:00+00:00', generation: 3, data: { final_ms: 640, first_partial_ms: 200, rtf: 0.1, ok: true } },
  { seq: '0000000000003-3-aaaaaa', kind: 'tts', at: '2026-11-07T10:02:01+00:00', generation: 3, data: { first_audio_ms: 250, rtf: 0.2, ok: true } },
  {
    seq: '0000000000004-3-aaaaaa', kind: 'metrics', at: '2026-11-07T10:02:05+00:00', generation: 3,
    data: { gpu: { util_pct: 64, mem_used_mib: 14336, mem_total_mib: 23034 }, cpu_pct: 30, memory: { used_mib: 6144, total_mib: 16384 }, disk: { used_gb: 96.4, total_gb: 150 } },
  },
]

interface Call {
  url: string
}

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** A `fetch` answering the telemetry routes and the identity read; `fail` answers every telemetry read instead. */
function serve(opts: { sessions?: unknown[]; days?: unknown[]; admin?: boolean; fail?: () => Response | null } = {}): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push({ url })
    if (url.includes('/v1/tenants/me')) return json(200, { principal: { is_admin: opts.admin ?? true } })
    if (url.includes('/v1/voice/telemetry/')) {
      const failed = opts.fail?.() ?? null
      if (failed !== null) return failed
    }
    if (url.includes('/v1/voice/telemetry/sessions/')) {
      const id = decodeURIComponent(url.split('/v1/voice/telemetry/sessions/')[1]!.split('?')[0]!)
      return json(200, { session: (opts.sessions ?? SESSIONS).find((s) => (s as { session_id: string }).session_id === id), samples: SAMPLES, scope: 'platform', tz: 'UTC' })
    }
    if (url.includes('/v1/voice/telemetry/sessions')) return json(200, { sessions: opts.sessions ?? SESSIONS, scope: 'platform', tz: 'UTC' })
    if (url.includes('/v1/voice/telemetry/days')) return json(200, { days: opts.days ?? DAYS, scope: 'platform', tz: 'UTC' })
    return json(404, { code: 'not_found', message: 'no such route' })
  }) as unknown as typeof fetch
  return calls
}

/** A stand-in `EventSource`: the test sends the server's events by name. */
class FakeSource {
  static last: FakeSource | null = null
  readonly url: string
  readyState = 1
  private listeners: Record<string, ((e: MessageEvent) => void)[]> = {}
  constructor(url: string) {
    this.url = url
    FakeSource.last = this
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    ;(this.listeners[name] ??= []).push(fn)
  }
  close() {
    this.readyState = 2
  }
  emit(name: string, data: unknown) {
    for (const fn of this.listeners[name] ?? []) fn(new MessageEvent(name, { data: JSON.stringify(data) }))
  }
}

async function page() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { VoiceTelemetry } = await import('../voice/Telemetry')
  return render(<VoiceTelemetry />)
}

const card = (id: string): HTMLElement => document.querySelector<HTMLElement>(`[data-card="${id}"]`)!

beforeEach(() => {
  FakeSource.last = null
  vi.stubGlobal('EventSource', FakeSource)
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Voice it › Telemetry draws every metric as served', () => {
  it('reads the platform view for the range in the viewer zone', async () => {
    const calls = serve()
    await page()
    await screen.findByRole('heading', { name: 'Models' })
    const days = calls.find((c) => c.url.includes('/v1/voice/telemetry/days'))!
    const q = new URL(days.url, 'http://h').searchParams
    expect(q.get('scope')).toBe('platform')
    expect(q.get('tz')).toBeTruthy()
    expect(q.get('from')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(q.get('to')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(FakeSource.last?.url).toContain('/v1/voice/telemetry/live')
  })

  it('draws model performance: every percentile of every timing', async () => {
    serve()
    await page()
    await screen.findByRole('heading', { name: 'Models' })
    const row = (m: string) => [...card('models').querySelectorAll(`tr[data-metric="${m}"] td`)].map((td) => td.textContent)
    expect(row('stt_final_ms')).toEqual(['312 ms', '587 ms', '941 ms'])
    expect(row('stt_first_partial_ms')).toEqual(['131 ms', '219 ms', '402 ms'])
    expect(row('stt_rtf')).toEqual(['0.07', '0.11', '0.19'])
    expect(row('tts_first_audio_ms')).toEqual(['228 ms', '341 ms', '512 ms'])
    expect(row('tts_rtf')).toEqual(['0.21', '0.26', '0.33'])
    // The session's samples: one bar per utterance, with its p90 rule.
    const bars = await within(card('models')).findByTestId('vtel-utterances')
    expect(bars.querySelectorAll('rect.b')).toHaveLength(2)
    expect(bars.textContent).toContain('p90 640 ms')
    expect(within(card('models')).getByTestId('vtel-syntheses').querySelectorAll('rect.b')).toHaveLength(1)
  })

  it('draws machine load, model load and warm-up, and the start in its order', async () => {
    serve()
    await page()
    await screen.findByRole('heading', { name: 'Machine' })
    const peak = (k: string) => card('machine').querySelector(`[data-peak="${k}"]`)!.textContent
    expect([peak('gpu_util_pct'), peak('gpu_mem_used_mib'), peak('cpu_pct'), peak('memory_used_mib'), peak('disk_used_gb'), peak('vllm_queue'), peak('kv_cache_pct')]).toEqual([
      '83%', '14.9 GiB', '41%', '7.3 GiB', '96.4 GB', '2', '37%',
    ])
    expect(card('machine').querySelector('[data-warm="load"]')!.textContent).toBe('18.3 s')
    expect(card('machine').querySelector('[data-warm="warmup"]')!.textContent).toBe('4.2 s')
    // The samples' load tiles: the last reading.
    expect((await within(card('machine')).findByText('64%')).closest('[data-tile="GPU"]')).toBeTruthy()
    expect([...card('start').querySelectorAll('li[data-phase]')].map((li) => li.getAttribute('data-phase'))).toEqual(['start.boot', 'start.server', 'start.load', 'start.warmup'])
    expect(within(card('start')).getByText('96.1 s')).toBeTruthy()
  })

  it('draws minutes, cost and stock-outs per session and per day, and a null cost as a dash with its reason', async () => {
    serve()
    await page()
    await screen.findByRole('heading', { name: 'Cost' })
    expect(screen.getByTestId('vtel-session-usd').textContent).toBe('$12.50')
    expect(screen.getByTestId('vtel-session-stockouts').textContent).toBe('1')
    const total = screen.getByTestId('vtel-days-total').textContent
    expect(total).toContain('41 min')
    expect(total).toContain('$47.25')
    expect(total).toContain('72 utterances')
    expect(total).toContain('2 stock-outs')
    expect(screen.getByTestId('vtel-days-chart').querySelectorAll('rect.b')).toHaveLength(3)

    const older = document.querySelector<HTMLElement>('[data-session="vs-older"] button')!
    await act(async () => {
      fireEvent.click(older)
    })
    expect(older.getAttribute('aria-pressed')).toBe('true')
    const usd = screen.getByTestId('vtel-session-usd')
    expect(usd.textContent).toBe('—')
    expect(usd.getAttribute('title')).toBe('The ledger could not be read')
  })

  it('draws the live session: its state, its utterances and its readings', async () => {
    serve()
    await page()
    await screen.findByRole('heading', { name: 'Models' })
    await act(async () => {
      FakeSource.last!.emit('session', { session_id: 'vs-live', generation: 4, state: 'RUNNING' })
      for (const s of SAMPLES) FakeSource.last!.emit('sample', s)
    })
    const live = document.querySelector<HTMLElement>('[data-live="session"]')!
    expect(within(live).getByText('running')).toBeTruthy()
    expect(within(live).getByText('2 utterances')).toBeTruthy()
    expect(within(live).getByTestId('vtel-utterances').querySelectorAll('rect.b')).toHaveLength(2)
    expect(live.querySelector('[data-tile="CPU"]')!.textContent).toContain('30%')
    expect(live.querySelector('[data-tile="Disk"]')!.textContent).toContain('96.4 / 150 GB')
    await act(async () => {
      FakeSource.last!.emit('end', { session_id: 'vs-live' })
    })
    expect(document.querySelector('[data-live="idle"]')!.textContent).toContain('Stopped')
  })
})

describe('the caps', () => {
  it('draws the session against $100 and its day against $300', async () => {
    serve()
    await page()
    await screen.findByRole('heading', { name: 'Cost' })
    expect(screen.getByTestId('vtel-cap-session').textContent).toContain('$12.50 / $100')
    expect(screen.getByTestId('vtel-cap-day').textContent).toContain('$40.00 / $300')
    expect(screen.getByTestId('vtel-cap-day-busiest').textContent).toContain('2026-11-07')
    const fill = screen.getByTestId('vtel-cap-session').querySelector<HTMLElement>('.vtel-cap-bar i')!
    expect(fill.style.width).toBe('12.5%')
  })
})

describe('a failed read and no sessions', () => {
  it('says the read failed with its status, draws no timeline, and Retry reads again', async () => {
    let failing = true
    const calls = serve({ fail: () => (failing ? json(503, { code: 'unavailable', message: 'the store is down' }) : null) })
    await page()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Telemetry not read · 503')
    expect(document.querySelector('[data-card]')).toBeNull()
    expect(screen.queryByText('No sessions yet')).toBeNull()
    const before = calls.length
    failing = false
    await act(async () => {
      fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    })
    expect(calls.length).toBeGreaterThan(before)
    expect(await screen.findByRole('heading', { name: 'Models' })).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('draws one line when there are no sessions', async () => {
    serve({ sessions: [], days: [day('2026-11-07'), day('2026-11-08')] })
    await page()
    expect(await screen.findByText('No sessions yet')).toBeTruthy()
    expect(document.querySelector('[data-card]')).toBeNull()
    expect(document.querySelectorAll('svg')).toHaveLength(0)
  })
})

describe('the Telemetry tab', () => {
  async function section() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { VoiceSettingsSectionScreen } = await import('../voice/Section')
    return render(<VoiceSettingsSectionScreen />)
  }

  it('is beside Settings for a platform admin and opens Telemetry', async () => {
    serve({ admin: true })
    await section()
    const tabs = await screen.findByRole('tablist')
    expect(within(tabs).getAllByRole('tab').map((t) => t.textContent)).toEqual(['Settings', 'Telemetry'])
    await act(async () => {
      fireEvent.click(within(tabs).getByRole('tab', { name: 'Telemetry' }))
    })
    expect(within(tabs).getByRole('tab', { name: 'Telemetry' }).getAttribute('aria-selected')).toBe('true')
    expect(await screen.findByRole('heading', { name: 'Telemetry' })).toBeTruthy()
  })

  it('is not shown to anyone else, and no telemetry is read', async () => {
    const calls = serve({ admin: false })
    await section()
    await screen.findByRole('heading', { name: 'Settings' })
    expect(screen.queryByRole('tab', { name: 'Telemetry' })).toBeNull()
    expect(calls.some((c) => c.url.includes('/v1/voice/telemetry'))).toBe(false)
  })
})

describe('nothing is wider than a 390 phone', () => {
  it('fixes no width a phone cannot hold', () => {
    const body = SHEET.replace(/\/\*[\s\S]*?\*\//g, '')
    for (const m of body.matchAll(/(?:^|[;{\s])(width|min-width|flex-basis)\s*:\s*([^;}]+)/g)) {
      const n = [...m[2]!.matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((a, x) => a + Number(x[1]), 0)
      expect(n, `${m[1]}: ${m[2]}`).toBeLessThanOrEqual(300)
    }
    expect(body).toMatch(/@media \(max-width: 720px\)/)
  })
})
