// THE PAUSE ON SILENCE (docs/design/voice-it.md §3.12.7; owner, 23 November
// 2026; voice/session.tsx, voice/Talk.tsx, voice/rtc.ts `wakeTalk`).
//
//   * the stream's `paused` event (voice_state `paused`) shows Paused and a
//     Resume control beside End; the peer is dropped, its drop ends nothing,
//     and the mic, the captions and the cards stay;
//   * Resume, the person's next word, or swarm-voice's `waking` with cause
//     `result` makes a fresh offer to `/v1/voice/sessions/{id}/reopen` with
//     that cause, and the talk goes on, Live again, ready to pause again;
//   * an end swarm-api does not confirm says what is true, its why behind the (i).
//
// MUTATIONS: end the talk on the pause's peer drop; start a new session for
// the wake; send no cause; keep Paused after the wake; say "ten minutes of
// silence" on an unconfirmed end.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakePeer, installMedia, settle, type FakeTrack } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))
const ear = vi.hoisted(() => ({ handlers: [] as { onStart: () => void }[], closed: 0 }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    get USE_FIXTURES() {
      return false
    },
  }
})

vi.mock('../fetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../fetch')>()
  return {
    ...actual,
    write: fetchMock.write,
    read: ((target, ...rest) =>
      target.url === '/v1/voice/settings'
        ? Promise.resolve({ status: 'ok', data: { settings: { engine: 'gpt-live', debug: false } }, fetchedAt: Date.now() })
        : actual.read(target, ...rest)) as typeof actual.read,
  }
})

// The detection that hears the next word while paused: its handlers, kept for the test to speak through.
vi.mock('../voice/vad', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../voice/vad')>()
  return {
    ...actual,
    startVoiceDetection: vi.fn(async (_stream: MediaStream, handlers: { onStart: () => void }) => {
      ear.handlers.push(handlers)
      return { model: 'energy', setMic: () => {}, close: () => void (ear.closed += 1) }
    }),
  }
})

import { FIXTURE_CARDS, type VoiceCard } from '../voice/CardList'
import { TalkPane } from '../voice/Talk'
import { REOPEN_WAIT_MS, VoiceSessionHolder } from '../voice/session'

const SESSION = 'vs-0001'

class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static all: FakeEventSource[] = []
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  constructor(readonly url: string) {
    super()
    FakeEventSource.all.push(this)
  }
  close(): void {
    this.readyState = 2
  }
  frame(type: string, seq: number, data: Record<string, unknown>): void {
    this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ seq, type, ...data }), lastEventId: String(seq) }))
  }
  static get last(): FakeEventSource {
    const s = FakeEventSource.all[FakeEventSource.all.length - 1]
    if (s === undefined) throw new Error('no EventSource was opened')
    return s
  }
}

let woken = 0
let endRefused = false

function answer() {
  fetchMock.write.mockImplementation(async (target: { url: string }) => {
    if (target.url === '/v1/voice/sessions') {
      return { status: 'ok', data: { session_id: SESSION, answer_sdp: 'v=0 answer-1' }, fetchedAt: Date.now() }
    }
    if (target.url === `/v1/voice/sessions/${SESSION}/reopen`) {
      woken += 1
      return { status: 'ok', data: { session_id: SESSION, state: 'live', voice_state: 'open', mode: 'live', answer_sdp: `v=0 wake-${woken}` }, fetchedAt: Date.now() }
    }
    if (target.url.endsWith('/end') && endRefused) {
      return { status: 'error', error: { status: 503, code: 'unavailable', message: 'swarm-api is unavailable' } }
    }
    return { status: 'ok', data: null, fetchedAt: Date.now() }
  })
}

function card(id: string, title: string): VoiceCard {
  const base = FIXTURE_CARDS.find((c) => c.kind === 'work') ?? FIXTURE_CARDS[0]
  if (base === undefined) throw new Error('no fixture cards')
  return { ...base, kind: 'work', card_id: id, version: 1, title }
}

async function start(): Promise<FakePeer> {
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
  return FakePeer.last
}

const calls = (suffix: string) => fetchMock.write.mock.calls.filter(([t]) => String((t as { url: string }).url).endsWith(suffix))
const status = () => document.querySelector('.vt-status')?.textContent ?? ''

async function pause(es: FakeEventSource, seq: number): Promise<void> {
  await act(async () => es.frame('paused', seq, { voice_state: 'paused', cause: null }))
  await settle(act)
}

describe('a silent live voice pauses, and its wake opens it again', () => {
  let mic: FakeTrack
  beforeEach(() => {
    FakeEventSource.all = []
    ear.handlers = []
    ear.closed = 0
    woken = 0
    endRefused = false
    vi.stubGlobal('EventSource', FakeEventSource)
    ;({ mic } = installMedia({ audioContext: true }))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    fetchMock.write.mockReset()
  })

  it('shows Paused with Resume, drops the peer without ending, and Resume wakes it by cause', async () => {
    answer()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const first = await start()
    const es = FakeEventSource.last
    await act(async () => first.events.deliver({ type: 'session.input_transcript.done', item_id: 'u1', transcript: 'Plan the login page.' }))
    await act(async () => es.frame('card.upsert', 1, { card: card('vc_w', 'Filed') }))
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()

    await pause(es, 2)
    expect(status()).toContain('Paused')
    const resume = screen.getByRole('button', { name: 'Resume' })
    expect(resume.getAttribute('title')).toContain('45 seconds')
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
    expect(first.closed).toBe(true)
    expect(mic.stop).not.toHaveBeenCalled()
    expect(screen.getByRole('log', { name: 'Captions' }).textContent).toContain('Plan the login page.')
    expect(document.querySelectorAll('article[data-card-id="vc_w"]')).toHaveLength(1)
    // The paused peer's drop is no drop: nothing ends, however long.
    vi.useFakeTimers()
    first.connectionState = 'closed'
    await act(async () => first.dispatchEvent(new Event('connectionstatechange')))
    await act(async () => {
      vi.advanceTimersByTime(REOPEN_WAIT_MS * 4)
    })
    vi.useRealTimers()
    await settle(act)
    expect(calls('/end')).toHaveLength(0)

    fireEvent.click(resume)
    await settle(act)
    const wakes = calls('/reopen')
    expect(wakes).toHaveLength(1)
    expect(wakes[0]?.[1]).toBe('POST')
    expect(wakes[0]?.[2]).toEqual({ offer_sdp: 'v=0 fake-offer', cause: 'resume' })
    expect(calls('/v1/voice/sessions').filter(([t]) => (t as { url: string }).url === '/v1/voice/sessions')).toHaveLength(1)
    expect(FakePeer.all).toHaveLength(2)
    expect(FakePeer.last.remote).toEqual({ type: 'answer', sdp: 'v=0 wake-1' })
    expect(FakePeer.last.sent[0]?.track).toBe(mic)
    expect(status()).toContain('Live')
    expect(status()).not.toContain('Paused')
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    expect(ear.closed).toBeGreaterThan(0)

    // A second silence pauses again, and the person's next word wakes it.
    await pause(es, 3)
    expect(status()).toContain('Paused')
    expect(ear.handlers).toHaveLength(2)
    await act(async () => ear.handlers[1]?.onStart())
    await settle(act)
    expect(calls('/reopen')[1]?.[2]).toEqual({ offer_sdp: 'v=0 fake-offer', cause: 'speech' })
    expect(status()).not.toContain('Paused')
    expect(FakePeer.all).toHaveLength(3)
    expect(calls('/end')).toHaveLength(0)
  })

  it("wakes on swarm-voice's own `waking` with cause result", async () => {
    answer()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    const es = FakeEventSource.last
    await pause(es, 1)
    await act(async () => es.frame('paused', 2, { voice_state: 'waking', cause: 'result' }))
    await settle(act)
    expect(calls('/reopen').map(([, , body]) => (body as { cause: string }).cause)).toEqual(['result'])
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
  })

  it('says what is true when swarm-api does not confirm the end, the why behind its (i)', async () => {
    answer()
    endRefused = true
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    const problem = document.querySelector('.vt-problem')
    expect(problem?.querySelector('span')?.textContent).toBe('swarm-api did not confirm the end (swarm-api is unavailable). The call here is closed.')
    expect(problem?.textContent).not.toContain('ten minutes')
    expect(problem?.querySelector('button')).not.toBeNull()
  })
})
