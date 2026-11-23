// VOICE IT › TELEMETRY › REPLY LATENCY (docs/design/voice-it-own-speech.md
// §12.6): end of speech to the reply's first audio on both engines, from
// `GET /v1/voice/telemetry/latency` for the page's range, on the live path
// against a stubbed `fetch`.
//
//   * numbers: count, p50, p90 and not measured, per engine and per part, as
//     served; a part never measured is a dash, never a zero;
//   * every definition sits behind a row's tooltip, not in visible prose;
//   * nothing measured yet is one short line;
//   * a failed latency read says so and leaves the rest of the page drawn.
//
// BREAK IT: draw a null p50 as 0 ms, drop the GPT-Live row, show a definition
// as a sentence on the page, or let a failed latency read blank the page.

import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fig = (count: number, p50: number | null, p90: number | null, not_measured: number) => ({ count, p50, p90, not_measured })

const MEASURED = {
  engines: {
    own: {
      turns: 12,
      span_ms: fig(10, 1800, 2600, 2),
      parts: {
        recognition_ms: fig(12, 300, 410, 0),
        front_first_token_ms: fig(0, null, null, 12),
        front_ms: fig(11, 1100, 1700, 1),
        synthesis_ms: fig(10, 240, 330, 2),
      },
    },
    'gpt-live': { turns: 3, span_ms: fig(2, 500, 700, 1), parts: {} },
  },
  scope: 'platform',
  tz: 'UTC',
}

const NOTHING = {
  engines: {
    own: { turns: 0, span_ms: fig(0, null, null, 0), parts: {} },
    'gpt-live': { turns: 0, span_ms: fig(0, null, null, 0), parts: {} },
  },
  scope: 'platform',
  tz: 'UTC',
}

const SESSION = {
  session_id: 'vs-one',
  person: 'owner@example.com',
  first_at: '2026-11-07T10:00:00+00:00',
  last_at: '2026-11-07T10:20:00+00:00',
  day: '2026-11-07',
  minutes: 20,
  utterances: 4,
  syntheses: 4,
  stt_errors: 0,
  tts_errors: 0,
  percentiles: {},
  peaks: {},
  start_phases: {},
  machine_usd: 1,
  machine_usd_reason: null,
  stockouts: 0,
  stockouts_reason: null,
}

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function serve(latency: () => Response, opts: { sessions?: unknown[] } = {}): string[] {
  const urls: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    urls.push(url)
    if (url.includes('/v1/tenants/me')) return json(200, { principal: { is_admin: true } })
    if (url.includes('/v1/voice/telemetry/latency')) return latency()
    if (url.includes('/v1/voice/telemetry/sessions/')) return json(200, { session: SESSION, samples: [], scope: 'platform', tz: 'UTC' })
    if (url.includes('/v1/voice/telemetry/sessions')) return json(200, { sessions: opts.sessions ?? [SESSION], scope: 'platform', tz: 'UTC' })
    if (url.includes('/v1/voice/telemetry/days')) return json(200, { days: [], scope: 'platform', tz: 'UTC' })
    if (url.includes('/v1/voice/trace/sessions')) return json(200, { sessions: [], scope: 'platform' })
    return json(404, { code: 'not_found', message: 'no such route' })
  }) as unknown as typeof fetch
  return urls
}

class QuietSource {
  addEventListener() {}
  close() {}
}

async function page() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { VoiceTelemetry } = await import('../voice/Telemetry')
  return render(<VoiceTelemetry />)
}

const latencyCard = async (): Promise<HTMLElement> => {
  await screen.findByRole('heading', { name: 'Reply latency' })
  return document.querySelector<HTMLElement>('[data-card="latency"]')!
}

beforeEach(() => {
  vi.stubGlobal('EventSource', QuietSource)
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('reply latency', () => {
  it('reads the page range and draws count, p50, p90 and not measured per engine and part', async () => {
    const urls = serve(() => json(200, MEASURED))
    await page()
    const card = await latencyCard()
    const read = urls.find((u) => u.includes('/v1/voice/telemetry/latency'))!
    expect(read).toMatch(/from=\d{4}-\d{2}-\d{2}/)
    expect(read).toMatch(/to=\d{4}-\d{2}-\d{2}/)
    expect(read).toContain('scope=platform')

    const own = within(card).getByRole('row', { name: /Own speech/ })
    expect(within(own).getAllByRole('cell').map((c) => c.textContent)).toEqual(['10', '1800 ms', '2600 ms', '2'])
    const live = within(card).getByRole('row', { name: /GPT-Live/ })
    expect(within(live).getAllByRole('cell').map((c) => c.textContent)).toEqual(['2', '500 ms', '700 ms', '1'])
    const first = within(card).getByRole('row', { name: /First token/ })
    expect(within(first).getAllByRole('cell').map((c) => c.textContent)).toEqual(['0', '—', '—', '12'])
    for (const name of [/Recognition/, /^\s*Front\b(?!.*token)/, /Synthesis/]) {
      expect(within(card).getByRole('row', { name })).toBeTruthy()
    }
    // The chart: one bar per measured figure, its value in the tooltip.
    expect(within(card).getByTestId('vtel-latency-chart').querySelectorAll('rect.b').length).toBe(5)
  })

  it('keeps every definition behind a tooltip, with no sentence on the page', async () => {
    serve(() => json(200, MEASURED))
    await page()
    const card = await latencyCard()
    for (const th of within(card).getAllByRole('rowheader')) {
      expect(th.getAttribute('title') ?? '').not.toBe('')
    }
    const words = (card.textContent ?? '').replace(/\d+/g, ' ')
    expect(words).not.toMatch(/[a-z]{3,}\.\s|[a-z]{3,}\.$/)
  })

  it('says nothing measured yet in one short line', async () => {
    serve(() => json(200, NOTHING))
    await page()
    const card = await latencyCard()
    expect(within(card).queryByRole('table')).toBeNull()
    const line = within(card).getByText('Nothing measured yet')
    expect(line.textContent!.length).toBeLessThan(30)
  })

  it('a failed latency read is one line with Retry and the rest of the page stays', async () => {
    serve(() => json(503, { code: 'unavailable', message: 'down' }))
    await page()
    const card = await latencyCard()
    expect(within(card).getByRole('alert').textContent).toContain('Latency not read')
    expect(document.querySelector('[data-session="vs-one"]')).not.toBeNull()
  })

  it('shows hosted turns even when no own-speech session started in the range', async () => {
    serve(() => json(200, MEASURED), { sessions: [] })
    await page()
    const card = await latencyCard()
    expect(within(card).getByRole('row', { name: /GPT-Live/ })).toBeTruthy()
  })
})
