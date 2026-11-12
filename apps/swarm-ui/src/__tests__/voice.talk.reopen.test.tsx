// A SPENT LIVE KEY REOPENS ON THE NEXT TIER (docs/design/llm-providers.md
// §5.3, §5.4; voice/Talk.tsx, voice/rtc.ts `reopenTalk`).
//
//   * on `voice.tier.reopen` Talk closes the spent peer, keeps the mic, and
//     makes a fresh offer to the SAME session (`{offer_sdp, session_id}`),
//     applying the answer to a new peer, with nothing for the person to do;
//   * the captions and the cards stay, and one caption names the new tier
//     (a status line, never a dialog);
//   * a reopen swarm-api refuses ends the talk with its reason;
//   * a peer that drops waits REOPEN_WAIT_MS for the event before the talk ends.
//
// MUTATIONS: start a new session for the reopen; stop the mic on the reopen;
// clear the captions; draw the tier in a dialog; leave the talk up when the
// reopen is refused; end at once on a drop.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakePeer, installMedia, settle, type FakeTrack } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))

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

import { FIXTURE_CARDS, type VoiceCard } from '../voice/CardList'
import { REOPEN_WAIT_MS, TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'

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

type Answer = { status: 'ok'; data: unknown; fetchedAt: number } | { status: 'error'; error: { status: number; code: string; message: string; detail?: unknown } }

/** swarm-api's start: the first answers a new session, a reopen answers `reopen`. */
function answer(reopen: Answer = { status: 'ok', data: { session_id: SESSION, mode: 'live', answer_sdp: 'v=0 answer-2', tier: 'voice_dedicated' }, fetchedAt: 0 }) {
  fetchMock.write.mockImplementation(async (target: { url: string }, _method: string, body?: Record<string, unknown>) => {
    if (target.url === '/v1/voice/sessions') {
      if (body?.session_id !== undefined) return reopen
      return { status: 'ok', data: { session_id: SESSION, answer_sdp: 'v=0 answer-1' }, fetchedAt: Date.now() }
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

const ends = () => fetchMock.write.mock.calls.filter(([t]) => String((t as { url: string }).url).endsWith('/end'))
const starts = () => fetchMock.write.mock.calls.filter(([t]) => (t as { url: string }).url === '/v1/voice/sessions')

describe('a spent Live key reopens on the next tier', () => {
  let mic: FakeTrack
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    ;({ mic } = installMedia({ audioContext: true }))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    fetchMock.write.mockReset()
  })

  it('offers again to the same session, keeps the captions and the cards, and draws one caption', async () => {
    answer()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const first = await start()
    const es = FakeEventSource.last
    await act(async () => first.events.deliver({ type: 'session.input_transcript.done', item_id: 'u1', transcript: 'Add CSV export to Runs.' }))
    await act(async () => es.frame('card.upsert', 1, { card: card('vc_w', 'Filed') }))

    await act(async () => es.frame('voice.tier.reopen', 2, { tier: 'voice_dedicated' }))
    await settle(act)

    expect(FakePeer.all).toHaveLength(2)
    const second = FakePeer.last
    expect(first.closed).toBe(true)
    expect(second.closed).toBe(false)
    expect(second.remote).toEqual({ type: 'answer', sdp: 'v=0 answer-2' })
    // The same mic, still on: the person did nothing.
    expect(second.sent[0]?.track).toBe(mic)
    expect(mic.stop).not.toHaveBeenCalled()
    const [, method, body] = starts()[1] ?? []
    expect(method).toBe('POST')
    expect(body).toEqual({ offer_sdp: 'v=0 fake-offer', session_id: SESSION })
    expect(starts()).toHaveLength(2)
    expect(ends()).toHaveLength(0)
    // The talk goes on: the captions, the cards, one caption naming the tier, no dialog.
    expect(screen.getByRole('log', { name: 'Captions' }).textContent).toContain('Add CSV export to Runs.')
    expect(document.querySelectorAll('article[data-card-id="vc_w"]')).toHaveLength(1)
    const said = screen.getAllByRole('status').filter((s) => s.textContent === 'Moved to Voice it key')
    expect(said).toHaveLength(1)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
    // The new peer carries the session's events: a caption on it lands in the same log.
    await act(async () => second.events.deliver({ type: 'session.input_transcript.done', item_id: 'u2', transcript: 'And PDF.' }))
    expect(screen.getByRole('log', { name: 'Captions' }).textContent).toContain('Add CSV export to Runs.')
    expect(screen.getByRole('log', { name: 'Captions' }).textContent).toContain('And PDF.')
    // The spent peer's drop, once replaced, ends nothing.
    first.connectionState = 'failed'
    await act(async () => first.dispatchEvent(new Event('connectionstatechange')))
    expect(ends()).toHaveLength(0)
  })

  it('draws nothing and offers nothing for a tier it does not know', async () => {
    answer()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()
    await act(async () => FakeEventSource.last.frame('voice.tier.reopen', 1, { tier: 'sk-not-a-tier' }))
    await settle(act)
    expect(FakePeer.all).toHaveLength(1)
    expect(starts()).toHaveLength(1)
  })

  it('ends the talk with its reason when the reopen is refused', async () => {
    const message = 'swarm-voice refused the session (HTTP 500); it was ended.'
    answer({ status: 'error', error: { status: 502, code: 'voice_unreachable', message } })
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await start()

    await act(async () => FakeEventSource.last.frame('voice.tier.reopen', 1, { tier: 'api_key' }))
    await settle(act)

    expect(ends()).toHaveLength(1)
    expect(String((ends()[0]?.[0] as { url: string }).url)).toBe(`/v1/voice/sessions/${SESSION}/end`)
    expect(screen.getAllByRole('alert').filter((a) => a.classList.contains('vt-problem')).map((a) => a.textContent)).toEqual([message])
    expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
    expect(mic.stop).toHaveBeenCalled()
    expect(FakePeer.all.every((p) => p.closed)).toBe(true)
    expect(screen.queryAllByRole('status').some((s) => s.textContent?.startsWith('Moved to'))).toBe(false)
  })

  it('a dropped peer waits for the reopen, and with none ends the talk', async () => {
    answer()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const first = await start()
    vi.useFakeTimers()
    first.connectionState = 'failed'
    await act(async () => first.dispatchEvent(new Event('connectionstatechange')))
    await act(async () => {
      vi.advanceTimersByTime(REOPEN_WAIT_MS - 1)
    })
    expect(ends()).toHaveLength(0)
    await act(async () => {
      vi.advanceTimersByTime(1)
    })
    vi.useRealTimers()
    await settle(act)
    expect(ends()).toHaveLength(1)
  })

  it('a dropped peer reopens when the event comes within the wait', async () => {
    answer()
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    const first = await start()
    vi.useFakeTimers()
    first.connectionState = 'failed'
    await act(async () => first.dispatchEvent(new Event('connectionstatechange')))
    await act(async () => FakeEventSource.last.frame('voice.tier.reopen', 1, { tier: 'plan' }))
    vi.useRealTimers()
    await settle(act)
    vi.useFakeTimers()
    await act(async () => {
      vi.advanceTimersByTime(REOPEN_WAIT_MS * 2)
    })
    vi.useRealTimers()
    await settle(act)
    expect(ends()).toHaveLength(0)
    expect(FakePeer.all).toHaveLength(2)
    expect(screen.getAllByRole('status').filter((s) => s.textContent === 'Moved to Plan')).toHaveLength(1)
  })
})
