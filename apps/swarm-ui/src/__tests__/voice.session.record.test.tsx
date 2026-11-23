// VOICE IT › TELEMETRY › ONE SESSION: its transcript and its summary
// (swarm-api `GET /v1/voice/sessions/{id}/transcript` and `/summary`).
//
//   * one button, "Download transcript", saves the transcript the api layer
//     read (a Blob through an object URL, never a bare link: the read carries
//     the tenant header a link cannot);
//   * a written summary is a "Summary" heading and its text; capped, failed
//     and pending are one short line each; none is nothing at all;
//   * Telemetry draws both for the session picked, ended sessions included.
//
// BREAK IT: drop the button, add a second one, link the route bare, draw a
// heading for "none", or reword a state's line.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The live path for the whole file: USE_FIXTURES is decided when api.ts is
// first imported, and the mocked module below is that first import.
const voice = vi.hoisted(() => {
  vi.stubEnv('VITE_LIVE', '1')
  return {
    loadVoiceSessionSummary: vi.fn(),
    loadVoiceTranscript: vi.fn(),
  }
})

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  loadVoiceSessionSummary: voice.loadVoiceSessionSummary,
  loadVoiceTranscript: voice.loadVoiceTranscript,
}))

import { SessionRecord } from '../voice/SessionRecord'

const SESSION = 'vs_0001'

function summary(state: string, text: string | null = null) {
  return { status: 'ok' as const, fetchedAt: 0, data: { session_id: SESSION, state, text, at: text === null ? null : '2026-11-20T10:00:00+00:00' } }
}

function transcript(body = 'person: hello\nvoice: hi\n', filename = `voice-${SESSION}.txt`) {
  return { status: 'ok' as const, fetchedAt: 0, data: { blob: new Blob([body], { type: 'text/plain' }), filename } }
}

let created: Blob[] = []
let revoked: string[] = []
let clicked: { href: string; download: string }[] = []

beforeEach(() => {
  created = []
  revoked = []
  clicked = []
  voice.loadVoiceSessionSummary.mockReset()
  voice.loadVoiceTranscript.mockReset()
  // jsdom has no object URLs: these stand in for the browser's two.
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: (b: Blob) => {
      created.push(b)
      return `blob:transcript-${created.length}`
    },
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: (u: string) => {
      revoked.push(u)
    },
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push({ href: this.getAttribute('href') ?? '', download: this.download })
  })
})

afterEach(() => {
  delete (URL as { createObjectURL?: unknown }).createObjectURL
  delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

async function drawn(state: string, text: string | null = null) {
  voice.loadVoiceSessionSummary.mockResolvedValue(summary(state, text))
  const view = render(<SessionRecord sessionId={SESSION} />)
  await waitFor(() => expect(voice.loadVoiceSessionSummary).toHaveBeenCalledWith(SESSION))
  // Let the summary's read settle into the page.
  await act(async () => {})
  return view
}

const record = (): HTMLElement => document.querySelector<HTMLElement>('[data-session-record]')!

describe('the session record: the transcript button', () => {
  it('draws one button, "Download transcript"', async () => {
    await drawn('none')
    const buttons = within(record()).getAllByRole('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]!.textContent).toBe('Download transcript')
    // Not a bare link: the route needs the api layer's headers.
    expect(record().querySelector('a[href*="/transcript"]')).toBeNull()
  })

  it('reads the transcript through the api layer and saves it as a Blob through an object URL', async () => {
    await drawn('none')
    const file = transcript()
    voice.loadVoiceTranscript.mockResolvedValue(file)
    fireEvent.click(screen.getByRole('button', { name: 'Download transcript' }))
    await waitFor(() => expect(clicked).toHaveLength(1))
    expect(voice.loadVoiceTranscript).toHaveBeenCalledWith(SESSION)
    expect(created).toEqual([file.data.blob])
    expect(clicked[0]).toEqual({ href: 'blob:transcript-1', download: `voice-${SESSION}.txt` })
    expect(revoked).toEqual(['blob:transcript-1'])
  })

  it('says a failed download in two words, the reason behind its tooltip, and saves nothing', async () => {
    await drawn('none')
    voice.loadVoiceTranscript.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'No transcript for this session.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Download transcript' }))
    const said = await within(record()).findByRole('alert')
    expect(said.textContent).toContain('Not downloaded')
    expect(created).toEqual([])
    expect(clicked).toEqual([])
    // The reason is help, not surface prose: the tooltip's text is marked as such.
    const tip = record().querySelector('[data-help-description]')
    expect(tip?.textContent).toBe('No transcript for this session.')
  })
})

describe('the session record: the summary', () => {
  it('draws a "Summary" heading and the text when it is written', async () => {
    await drawn('written', 'Planned the export page and opened two lanes.')
    expect(within(record()).getByRole('heading', { name: 'Summary' })).toBeTruthy()
    expect(record().textContent).toContain('Planned the export page and opened two lanes.')
  })

  it('draws only "No summary: session at its cap." when it is capped', async () => {
    await drawn('capped')
    expect(within(record()).queryByRole('heading')).toBeNull()
    expect(record().textContent).toBe('Download transcriptNo summary: session at its cap.')
  })

  it('draws only "No summary." when it failed', async () => {
    await drawn('failed')
    expect(within(record()).queryByRole('heading')).toBeNull()
    expect(record().textContent).toBe('Download transcriptNo summary.')
  })

  it('draws only "Summary pending." while it is pending', async () => {
    await drawn('pending')
    expect(within(record()).queryByRole('heading')).toBeNull()
    expect(record().textContent).toBe('Download transcriptSummary pending.')
  })

  it('draws nothing, not even the heading, when there is none', async () => {
    await drawn('none')
    expect(within(record()).queryByRole('heading')).toBeNull()
    expect(record().textContent).toBe('Download transcript')
  })

  it('reads the summary again for another session', async () => {
    voice.loadVoiceSessionSummary.mockResolvedValue(summary('written', 'First.'))
    const view = render(<SessionRecord sessionId={SESSION} />)
    await within(record()).findByText('First.')
    voice.loadVoiceSessionSummary.mockResolvedValue({ ...summary('pending'), data: { ...summary('pending').data, session_id: 'vs_0002' } })
    view.rerender(<SessionRecord sessionId="vs_0002" />)
    await within(record()).findByText('Summary pending.')
    expect(voice.loadVoiceSessionSummary).toHaveBeenLastCalledWith('vs_0002')
    expect(record().textContent).not.toContain('First.')
  })
})

describe('Telemetry draws the record for the session picked', () => {
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  const pct = { p50: null, p90: null, max: null }
  const row = (id: string, first: string) => ({
    session_id: id, tenant_id: 'ws-one', person: 'owner@example.com', generation: 1, first_at: first, last_at: first, day: first.slice(0, 10),
    minutes: 4, utterances: 3, syntheses: 3, stt_errors: 0, tts_errors: 0, stt_audio_s: 10, tts_audio_s: 10, metrics_samples: 0,
    percentiles: { stt_final_ms: pct, stt_first_partial_ms: pct, stt_rtf: pct, tts_first_audio_ms: pct, tts_rtf: pct },
    peaks: { gpu_util_pct: null, gpu_mem_used_mib: null, cpu_pct: null, memory_used_mib: null, disk_used_gb: null, vllm_queue: null, kv_cache_pct: null },
    start_phases: {}, machine_usd: 1, machine_usd_reason: null, stockouts: 0, stockouts_reason: null,
  })
  const SESSIONS = [row('vs-newest', '2026-11-07T10:00:00+00:00'), row('vs-ended', '2026-11-06T09:00:00+00:00')]

  class QuietSource {
    readyState = 1
    addEventListener() {}
    close() {}
  }

  it('reads the picked session\'s summary and offers its transcript', async () => {
    vi.stubGlobal('EventSource', QuietSource)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/v1/voice/telemetry/sessions/')) return json(200, { session: SESSIONS[0], samples: [] })
      if (url.includes('/v1/voice/telemetry/sessions')) return json(200, { sessions: SESSIONS })
      if (url.includes('/v1/voice/telemetry/days')) return json(200, { days: [] })
      if (url.includes('/v1/voice/telemetry/latency')) return json(200, { engines: {} })
      if (url.includes('/v1/voice/trace/sessions')) return json(200, { sessions: [] })
      return json(404, { code: 'not_found', message: 'no such route' })
    }))
    voice.loadVoiceSessionSummary.mockImplementation(async (id: string) => ({ ...summary('written', `Summary of ${id}.`), data: { ...summary('written', `Summary of ${id}.`).data, session_id: id } }))
    const { VoiceTelemetry } = await import('../voice/Telemetry')
    render(<VoiceTelemetry />)
    await screen.findByText('Summary of vs-newest.')
    expect(screen.getAllByRole('button', { name: 'Download transcript' })).toHaveLength(1)
    // An ended session, picked from the timeline.
    fireEvent.click(document.querySelector<HTMLButtonElement>('[data-session="vs-ended"] button')!)
    await screen.findByText('Summary of vs-ended.')
    voice.loadVoiceTranscript.mockResolvedValue(transcript('x', 'voice-vs-ended.txt'))
    fireEvent.click(screen.getByRole('button', { name: 'Download transcript' }))
    await waitFor(() => expect(voice.loadVoiceTranscript).toHaveBeenCalledWith('vs-ended'))
  })
})
