// VOICE IT'S OWN SPEECH, THE BROWSER'S SIDE OF THE RELAY
// (docs/design/voice-it-own-speech.md §3.3, §4.1-§4.5; section 14, row U1).
//
//   * the start posts no offer: `speech` and `text` (with its fallback) are
//     sessions, a 422 for `offer_sdp` is GPT-Live and closes the mic again;
//   * framing: binary frames only between `utt.start` and `utt.end`, each
//     utterance its own sequence number, a ping every 15 s;
//   * down the socket: boot phases, partial and final words, the spoken
//     reply's PCM between `say.start` and `say.end`, `ended`;
//   * a boot phase after `ready` is a reconnect, not a second boot;
//   * a dropped socket redials with backoff inside the minute and gives up
//     after it; a refusal is final; a rotation swaps sockets once the new one
//     is ready and never mid-utterance.
//
// MUTATIONS: send a frame outside an utterance; reuse a sequence number;
// report a re-dial's phases as a boot; retry a refused socket; keep retrying
// past the minute; swap to the rotation's socket before it is ready.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { installMedia } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: fetchMock.write,
}))

import {
  BOOT_PHASES,
  connectOwnSpeech,
  GPT_LIVE,
  PING_MS,
  RECONNECT_DELAYS_MS,
  RECONNECT_WINDOW_MS,
  RECONNECTING,
  RETIRE_MS,
  ROTATE_WAIT_MS,
  SpeechSocket,
  bootPhase,
  fallbackOf,
  speechSocketUrl,
  type RawSpeechSocket,
  type SpeechHandlers,
} from '../voice/speechSocket'

class FakeWs implements RawSpeechSocket {
  binaryType = 'blob'
  readyState = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  sent: (string | ArrayBuffer)[] = []
  closedWith: number | null = null
  constructor(readonly url: string) {}
  send(data: string | ArrayBufferLike | ArrayBufferView): void {
    this.sent.push(typeof data === 'string' ? data : (data as ArrayBuffer))
  }
  close(code?: number): void {
    this.closedWith = code ?? 1000
    this.readyState = 3
  }
  /** What the page sent as text, parsed. */
  texts(): Record<string, unknown>[] {
    return this.sent.filter((d): d is string => typeof d === 'string').map((d) => JSON.parse(d) as Record<string, unknown>)
  }
  frames(): ArrayBuffer[] {
    return this.sent.filter((d): d is ArrayBuffer => typeof d !== 'string')
  }
  opened(): void {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }
  json(m: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(m) } as MessageEvent)
  }
  binary(b: ArrayBuffer): void {
    this.onmessage?.({ data: b } as MessageEvent)
  }
  dropped(code = 1006): void {
    this.readyState = 3
    this.onclose?.({ code } as CloseEvent)
  }
}

/** A socket under test with a hand-run clock: timers fire only through `tick`. */
function harness() {
  const sockets: FakeWs[] = []
  let clock = 0
  let timers: { at: number; fn: () => void; id: number }[] = []
  let ids = 0
  const h = {
    onStatus: vi.fn(),
    onPartial: vi.fn(),
    onFinal: vi.fn(),
    onSayStart: vi.fn(),
    onSayAudio: vi.fn(),
    onSayEnd: vi.fn(),
    onEnded: vi.fn(),
    onClosed: vi.fn(),
  } satisfies SpeechHandlers
  const s = new SpeechSocket('vs-0001', h, {
    url: 'wss://swarm.example.com/v1/voice/sessions/vs-0001/audio',
    open: (url) => {
      const ws = new FakeWs(url)
      sockets.push(ws)
      return ws
    },
    now: () => clock,
    setTimer: (fn, ms) => {
      ids += 1
      timers.push({ at: clock + ms, fn, id: ids })
      return ids
    },
    clearTimer: (t) => {
      timers = timers.filter((x) => x.id !== t)
    },
  })
  const tick = (ms: number) => {
    const until = clock + ms
    for (;;) {
      const due = timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at)[0]
      if (due === undefined) break
      timers = timers.filter((t) => t !== due)
      clock = due.at
      due.fn()
    }
    clock = until
  }
  const last = () => sockets[sockets.length - 1] as FakeWs
  return { s, h, sockets, tick, last, statuses: () => h.onStatus.mock.calls.map((c) => c[0] as string) }
}

const frame = (n: number) => new Uint8Array(640).fill(n).buffer

describe('the socket address', () => {
  it('is the session audio route on the console host, wss unless the console is on http', () => {
    expect(speechSocketUrl('vs-0001', { protocol: 'https:', host: 'swarm.example.com' })).toBe(
      'wss://swarm.example.com/v1/voice/sessions/vs-0001/audio',
    )
    expect(speechSocketUrl('vs-0001', { protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/v1/voice/sessions/vs-0001/audio')
  })
})

describe('the start on our own engine', () => {
  let mic: ReturnType<typeof installMedia>['mic']
  beforeEach(() => {
    ;({ mic } = installMedia())
  })
  afterEach(() => fetchMock.write.mockReset())

  it('posts no offer and returns a speech session on the mic it opened', async () => {
    fetchMock.write.mockResolvedValue({ status: 'ok', data: { session_id: 'vs-0001', mode: 'speech' }, fetchedAt: 0 })
    const c = await connectOwnSpeech()
    expect(fetchMock.write.mock.calls[0]?.[0].url).toBe('/v1/voice/sessions')
    expect(fetchMock.write.mock.calls[0]?.[2]).toEqual({})
    expect(c).not.toBe(GPT_LIVE)
    if (c === GPT_LIVE) return
    expect(c.mode).toBe('speech')
    expect(c.fallback).toBeNull()
    expect(c.mic).toBe(mic)
    expect(mic.stop).not.toHaveBeenCalled()
  })

  it('carries a text session with its fallback, the zone kept', async () => {
    fetchMock.write.mockResolvedValue({
      status: 'ok',
      data: { session_id: 'vs-0001', mode: 'text', fallback: { from: 'own', reason: 'no_l4', zone: 'us-central1-a' } },
      fetchedAt: 0,
    })
    const c = await connectOwnSpeech('vp-0001')
    expect(fetchMock.write.mock.calls[0]?.[2]).toEqual({ project_id: 'vp-0001' })
    if (c === GPT_LIVE) throw new Error('not a text session')
    expect(c.mode).toBe('text')
    expect(c.fallback).toEqual({ from: 'own', reason: 'no_l4', zone: 'us-central1-a' })
    // Text mode keeps the browser's microphone.
    expect(mic.stop).not.toHaveBeenCalled()
  })

  it('answers GPT-Live, the mic closed again, when swarm-api asks for the offer', async () => {
    fetchMock.write.mockResolvedValue({
      status: 'error',
      error: { httpStatus: 422, message: 'offer_sdp is required', detail: { field: 'offer_sdp' } },
    })
    expect(await connectOwnSpeech()).toBe(GPT_LIVE)
    expect(mic.stop).toHaveBeenCalled()
  })

  it('reads a fallback only when it names a reason', () => {
    expect(fallbackOf(null)).toBeNull()
    expect(fallbackOf({ from: 'own' })).toBeNull()
    expect(fallbackOf({ reason: 'not_ready' })).toEqual({ from: 'own', reason: 'not_ready', zone: null })
  })
})

describe('framing', () => {
  it('sends frames only inside an utterance, each utterance its own sequence number', () => {
    const { s, last } = harness()
    s.connect()
    const ws = last()
    expect(ws.binaryType).toBe('arraybuffer')
    // Not open yet: nothing can start.
    expect(s.startUtterance()).toBeNull()
    ws.opened()
    s.sendFrame(frame(9))
    expect(ws.sent).toEqual([])
    expect(s.startUtterance()).toBe(1)
    s.sendFrame(frame(1))
    s.sendFrame(frame(2))
    s.endUtterance()
    s.sendFrame(frame(3))
    expect(s.startUtterance()).toBe(2)
    s.endUtterance()
    expect(ws.texts()).toEqual([
      { t: 'utt.start', seq: 1 },
      { t: 'utt.end', seq: 1 },
      { t: 'utt.start', seq: 2 },
      { t: 'utt.end', seq: 2 },
    ])
    expect(ws.frames().map((f) => new Uint8Array(f)[0])).toEqual([1, 2])
    expect(ws.frames().every((f) => f.byteLength === 640)).toBe(true)
  })

  it('pings every 15 s, and sends barge and point as text', () => {
    const { s, last, tick } = harness()
    s.connect()
    last().opened()
    tick(PING_MS * 2)
    s.barge()
    s.point('v-1', { node: 'n-1' })
    expect(last().texts()).toEqual([{ t: 'ping' }, { t: 'ping' }, { t: 'barge' }, { t: 'point', visual: 'v-1', anchor: { node: 'n-1' } }])
  })

  it('closing ends an utterance in flight and shuts the socket, nothing more reported', () => {
    const { s, h, last } = harness()
    s.connect()
    const ws = last()
    ws.opened()
    s.startUtterance()
    s.close()
    expect(ws.texts().at(-1)).toEqual({ t: 'utt.end', seq: 1 })
    expect(ws.closedWith).toBe(1000)
    expect(h.onClosed).not.toHaveBeenCalled()
  })
})

describe('what comes down', () => {
  it('reports the boot phases, then ready, then the state words', () => {
    const { s, last, statuses } = harness()
    s.connect()
    const ws = last()
    ws.opened()
    for (const p of BOOT_PHASES) ws.json({ t: 'status', state: p === 'fetching model' ? 'fetching model speech-small' : p })
    ws.json({ t: 'status', state: 'ready' })
    ws.json({ t: 'status', state: 'listening' })
    expect(statuses()).toEqual(['starting machine', 'updating speech server', 'fetching model speech-small', 'loading models', 'ready', 'listening'])
    expect(bootPhase('fetching model speech-small')).toBe('fetching model')
    expect(bootPhase('listening')).toBeNull()
  })

  it('a phase after ready is a reconnect, not a second boot', () => {
    const { s, last, statuses } = harness()
    s.connect()
    const ws = last()
    ws.opened()
    ws.json({ t: 'status', state: 'ready' })
    ws.json({ t: 'status', state: 'starting machine' })
    expect(statuses()).toEqual(['ready', RECONNECTING])
  })

  it('passes partial and final words, the reply and its audio, and the end', () => {
    const { s, h, last } = harness()
    s.connect()
    const ws = last()
    ws.opened()
    ws.json({ t: 'partial', seq: 1, text: 'add a' })
    ws.json({ t: 'final', seq: 1, text: 'add a chart' })
    ws.json({ t: 'say.start', id: 's1', text: 'Drawing it.', point_at: { visual: 'v-1' } })
    const pcm = new Uint8Array(480).buffer
    ws.binary(pcm)
    ws.json({ t: 'say.end', id: 's1' })
    // Not JSON, or not an object: ignored.
    ws.onmessage?.({ data: 'nonsense' } as MessageEvent)
    ws.json({ t: 'ended', cause: 'idle' })
    expect(h.onPartial).toHaveBeenCalledWith(1, 'add a')
    expect(h.onFinal).toHaveBeenCalledWith(1, 'add a chart')
    expect(h.onSayStart).toHaveBeenCalledWith({ id: 's1', text: 'Drawing it.', pointAt: { visual: 'v-1' } })
    expect(h.onSayAudio).toHaveBeenCalledWith(pcm)
    expect(h.onSayEnd).toHaveBeenCalledWith('s1')
    expect(h.onEnded).toHaveBeenCalledWith('idle')
    expect(ws.closedWith).toBe(1000)
    expect(h.onClosed).not.toHaveBeenCalled()
  })
})

describe('reconnect within the session', () => {
  it('redials with backoff after a drop, and a new socket resets it', () => {
    const { s, h, sockets, tick, statuses } = harness()
    s.connect()
    sockets[0]?.opened()
    sockets[0]?.json({ t: 'status', state: 'ready' })
    s.startUtterance()
    sockets[0]?.dropped()
    expect(statuses().at(-1)).toBe(RECONNECTING)
    expect(s.speaking).toBe(false)
    tick((RECONNECT_DELAYS_MS[0] ?? 0) - 1)
    expect(sockets).toHaveLength(1)
    tick(1)
    expect(sockets).toHaveLength(2)
    expect(sockets[1]?.url).toBe(sockets[0]?.url)
    sockets[1]?.dropped()
    tick((RECONNECT_DELAYS_MS[1] ?? 0) - 1)
    expect(sockets).toHaveLength(2)
    tick(1)
    expect(sockets).toHaveLength(3)
    sockets[2]?.opened()
    sockets[2]?.json({ t: 'status', state: 'ready' })
    expect(s.startUtterance()).toBe(2)
    expect(sockets[2]?.texts()).toEqual([{ t: 'utt.start', seq: 2 }])
    expect(h.onClosed).not.toHaveBeenCalled()
  })

  it('gives up after a minute of trying, closed with 1006', () => {
    const { s, h, sockets, tick } = harness()
    s.connect()
    sockets[0]?.opened()
    for (let i = 0; i < 40 && h.onClosed.mock.calls.length === 0; i++) {
      ;(sockets[sockets.length - 1] as FakeWs).dropped()
      tick(8_000)
    }
    expect(h.onClosed).toHaveBeenCalledWith(1006)
    const n = sockets.length
    tick(RECONNECT_WINDOW_MS)
    expect(sockets).toHaveLength(n)
  })

  it('never retries a refusal', () => {
    const { s, h, sockets, tick } = harness()
    s.connect()
    sockets[0]?.opened()
    sockets[0]?.dropped(4403)
    tick(RECONNECT_WINDOW_MS)
    expect(sockets).toHaveLength(1)
    expect(h.onClosed).toHaveBeenCalledWith(4403)
  })

  it('rotates in silence: the new socket takes over once ready, the old one retires', () => {
    const { s, h, sockets, tick, statuses } = harness()
    s.connect()
    sockets[0]?.opened()
    sockets[0]?.json({ t: 'status', state: 'ready' })
    s.startUtterance()
    sockets[0]?.json({ t: 'rotate' })
    // Mid-utterance: no new socket yet.
    expect(sockets).toHaveLength(1)
    s.endUtterance()
    expect(sockets).toHaveLength(2)
    const fresh = sockets[1] as FakeWs
    fresh.opened()
    fresh.json({ t: 'status', state: 'starting machine' })
    // The new socket is silent until ready: its phases are not the session's.
    expect(statuses()).toEqual(['ready'])
    expect(s.startUtterance()).toBe(2)
    expect(sockets[0]?.texts().at(-1)).toEqual({ t: 'utt.start', seq: 2 })
    fresh.json({ t: 'status', state: 'ready' })
    // The utterance the old socket took ends there; the next goes to the new one.
    expect(sockets[0]?.texts().at(-1)).toEqual({ t: 'utt.end', seq: 2 })
    expect(s.startUtterance()).toBe(3)
    expect(fresh.texts()).toEqual([{ t: 'utt.start', seq: 3 }])
    // The old socket still plays the reply it was speaking, then retires.
    const tail = new Uint8Array(4).buffer
    sockets[0]?.binary(tail)
    expect(h.onSayAudio).toHaveBeenCalledWith(tail)
    tick(RETIRE_MS)
    expect(sockets[0]?.closedWith).toBe(1000)
    expect(fresh.closedWith).toBeNull()
  })

  it('a rotation waiting on speech goes ahead after the wait anyway', () => {
    const { s, sockets, tick } = harness()
    s.connect()
    sockets[0]?.opened()
    s.startUtterance()
    sockets[0]?.json({ t: 'rotate' })
    tick(ROTATE_WAIT_MS)
    expect(sockets).toHaveLength(2)
  })
})
