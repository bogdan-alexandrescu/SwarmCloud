/**
 * VOICE IT'S OWN SPEECH, THE BROWSER'S SIDE OF THE RELAY
 * (docs/design/voice-it-own-speech.md §3.3, §4.1-§4.5, §4.7).
 *
 * THE START (§4.1). Talk's Start posts `POST /v1/voice/sessions` with no
 * offer and no engine: swarm-api reads Settings' engine. On our own engine it
 * answers `mode: "speech"`, or `mode: "text"` with `fallback: {from, reason,
 * zone}` when the engine cannot start (no L4 free, the machine not ready, no
 * Claude account for the front). A start answered 422 for `offer_sdp` is the
 * GPT-Live engine, which needs a WebRTC offer: the page then makes today's
 * call (rtc.ts `connectTalk`), so an engine changed in Settings between the
 * page's read and the start still talks. The mic is opened before the post,
 * so a browser that refuses it starts nothing.
 *
 * THE AUDIO SOCKET (§3.3 step 2) is `GET /v1/voice/sessions/{id}/audio`,
 * upgraded behind the same front door and sign-in as every console request:
 * swarm-api authenticates the upgrade itself and checks the caller is the
 * session's person, as the live browser socket does. Its URL carries the
 * session id and nothing else. Browser to swarm-api: 20 ms binary frames of
 * 16 kHz 16-bit PCM between `{"t":"utt.start","seq":n}` and
 * `{"t":"utt.end","seq":n}`, `{"t":"barge"}`, `{"t":"point",...}` and
 * `{"t":"ping"}` every 15 s. swarm-api to the browser: `status` (the boot
 * phases, then the session's state words), `partial` and `final` (the
 * person's words), `say.start` with the agent's sentence, binary 24 kHz
 * 16-bit PCM (the spoken reply), `say.end`, `rotate` and `ended`.
 *
 * BOOT PHASES (§4.3): `starting machine`, `updating speech server`,
 * `fetching model`, `loading models`, then `ready`. Once ready, a phase seen
 * again (a reconnect re-dials the machine) is not a boot: it reads
 * `reconnecting` until the next `ready`.
 *
 * RECONNECT WITHIN THE SESSION (§4.5). swarm-api waits a minute for a
 * dropped socket to come back before it ends the session as a closed tab, so
 * a socket that drops is re-opened, with backoff, for `RECONNECT_WINDOW_MS`.
 * A refusal (4401 not signed in, 4403 not on Voice it, 4404 no such session,
 * 4409 not a live speech session, 4503 no engine) is final, as is `ended`.
 *
 * ROTATION (§3.3, the 660 s request limit). On `rotate` a new socket is
 * opened at the next silence (or within 60 s whatever happens). When it says
 * `ready` it takes every new utterance; the old one is only listened to for
 * `RETIRE_MS` more, so the words and the reply of an utterance it already
 * took still arrive, and is then closed. A rotation drops nothing.
 *
 * THE SPOKEN REPLY (§3.3 steps 5-7). `SpeechPlayer` plays the 24 kHz PCM
 * through Web Audio into a `MediaStreamAudioDestinationNode`, whose stream
 * the page sets on its `<audio>` element (so the browser's echo canceller
 * sees it as remote audio) and taps for the ring's voice trace. Barge-in
 * stops it at once.
 */

import { route, write } from '../fetch'
import { endVoiceSession, MIC_CONSTRAINTS, TalkError, VOICE_SESSIONS } from './rtc'

/** The audio socket's route (swarm_api/routes/voice_speech.py `AUDIO_ROUTE`). */
export const SPEECH_AUDIO_ROUTE = '/v1/voice/sessions/{id}/audio'
/** §4.4: the browser pings every 15 s. */
export const PING_MS = 15_000
/** §3.3: after `rotate`, the next silence, or this long at most. */
export const ROTATE_WAIT_MS = 60_000
/** How long a rotated-out socket is still listened to: its last utterance's words and reply. */
export const RETIRE_MS = 30_000
/** §4.5: swarm-api waits a minute for a reconnect before it ends the session. */
export const RECONNECT_WINDOW_MS = 60_000
/** Backoff between reconnects; the last repeats. */
export const RECONNECT_DELAYS_MS: readonly number[] = [500, 1_000, 2_000, 4_000, 8_000]
/** §3.3 step 5: the spoken reply's rate. */
export const SAY_RATE = 24_000

/** §4.3: the machine's start, in the order they happen. */
export const BOOT_PHASES = ['starting machine', 'updating speech server', 'fetching model', 'loading models'] as const
export type BootPhase = (typeof BOOT_PHASES)[number]
export const READY = 'ready'
export const RECONNECTING = 'reconnecting'
export const ENGINE_DOWN = 'engine down'

/** The relay's refusals (voice_speech.py): final, never retried. */
export const REFUSED_CODES: readonly number[] = [4401, 4403, 4404, 4409, 4503]

/** A boot phase's word, or null: `fetching model <name>` is the phase `fetching model`. */
export function bootPhase(state: string): BootPhase | null {
  if (state.startsWith('fetching model')) return 'fetching model'
  return (BOOT_PHASES as readonly string[]).includes(state) ? (state as BootPhase) : null
}

/** The socket's address: same host, `wss` unless the console itself is served over http. */
export function speechSocketUrl(sessionId: string, loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  const scheme = loc.protocol === 'http:' ? 'ws' : 'wss'
  return `${scheme}://${loc.host}${route(SPEECH_AUDIO_ROUTE, { id: sessionId }).url}`
}

// ---------------------------------------------------------------------------
// The start
// ---------------------------------------------------------------------------

/** Why a session is in text mode instead of on its engine (§4.1), as swarm-api wrote it. */
export interface SpeechFallback {
  from: string
  /** `no_l4`, `not_ready`, `engine_down`, or swarm-api's own word for another. */
  reason: string
  zone: string | null
}

export function fallbackOf(raw: unknown): SpeechFallback | null {
  if (raw === null || typeof raw !== 'object') return null
  const f = raw as Record<string, unknown>
  if (typeof f.reason !== 'string' || f.reason === '') return null
  return {
    from: typeof f.from === 'string' && f.from !== '' ? f.from : 'own',
    reason: f.reason,
    zone: typeof f.zone === 'string' && f.zone !== '' ? f.zone : null,
  }
}

/** A session on our own engine, or the text session it fell back to. */
export interface SpeechCall {
  sessionId: string
  projectId: string | null
  mode: 'speech' | 'text'
  fallback: SpeechFallback | null
  mic: MediaStreamTrack
  micStream: MediaStream
  /** Stop the mic. Ends nothing at swarm-api. */
  hangUp: () => void
}

/** The engine is GPT-Live: make today's WebRTC call instead. */
export const GPT_LIVE = 'gpt-live' as const

async function openMic(): Promise<MediaStream> {
  if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    throw new TalkError('This browser cannot make a voice call (no microphone access).')
  }
  try {
    return await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS)
  } catch (e) {
    throw new TalkError(
      e instanceof Error && e.name === 'NotAllowedError'
        ? 'The browser was not allowed to use the microphone. Allow it for this site and start again.'
        : 'No microphone could be opened.',
    )
  }
}

/**
 * Start on our own engine: the mic, then the start with no offer. Returns
 * `GPT_LIVE` (the mic closed again) when swarm-api asks for an offer.
 */
export async function connectOwnSpeech(projectId: string | null = null): Promise<SpeechCall | typeof GPT_LIVE> {
  const micStream = await openMic()
  const mic = micStream.getAudioTracks()[0]
  const stop = () => micStream.getTracks().forEach((t) => t.stop())
  if (mic === undefined) {
    stop()
    throw new TalkError('No microphone could be opened.')
  }
  try {
    const r = await write(route(VOICE_SESSIONS), 'POST', projectId === null ? {} : { project_id: projectId })
    if (r.status === 'error' || r.status === 'stale') {
      const detail = r.error.detail as { field?: unknown } | null
      if (r.error.httpStatus === 422 && detail !== null && typeof detail === 'object' && detail.field === 'offer_sdp') {
        stop()
        return GPT_LIVE
      }
      throw new TalkError(r.error.message, r.error)
    }
    const d = (r.status === 'ok' ? r.data : null) as { session_id?: unknown; project_id?: unknown; mode?: unknown; fallback?: unknown } | null
    if (d === null || typeof d.session_id !== 'string' || d.session_id === '') {
      throw new TalkError("swarm-api's answer to the start carried no session id.")
    }
    const sessionId = d.session_id
    const project = typeof d.project_id === 'string' && d.project_id !== '' ? d.project_id : projectId
    if (d.mode !== 'speech' && d.mode !== 'text') {
      // A live session needs the offer this start did not send: it cannot be heard, so it is ended.
      await endVoiceSession(sessionId)
      throw new TalkError(`swarm-api's answer to the start named a mode this start cannot use (${String(d.mode).slice(0, 40)}).`)
    }
    return { sessionId, projectId: project, mode: d.mode, fallback: d.mode === 'text' ? fallbackOf(d.fallback) : null, mic, micStream, hangUp: stop }
  } catch (e) {
    stop()
    if (e instanceof TalkError) throw e
    throw new TalkError('The start could not be sent from this browser.')
  }
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

/** The members of a WebSocket the relay drives: the ones a test fake carries. */
export interface RawSpeechSocket {
  binaryType: string
  readonly readyState: number
  onopen: ((ev: Event) => void) | null
  onmessage: ((ev: MessageEvent) => void) | null
  onclose: ((ev: CloseEvent) => void) | null
  onerror: ((ev: Event) => void) | null
  send(data: string | ArrayBufferLike | ArrayBufferView): void
  close(code?: number): void
}

const OPEN = 1

export interface SpeechSay {
  id: string
  text: string
  /** §8.5: the visual and anchor the sentence points at, as swarm-api sent them. */
  pointAt: unknown
}

export interface SpeechHandlers {
  /** A boot phase, `ready`, or a state word (`listening`, `hearing`, `thinking`, `speaking`, `reconnecting`, `engine down`). */
  onStatus: (state: string) => void
  onPartial: (seq: number, text: string) => void
  onFinal: (seq: number, text: string) => void
  onSayStart: (say: SpeechSay) => void
  onSayAudio: (pcm: ArrayBuffer) => void
  onSayEnd: (id: string) => void
  /** swarm-api ended the session (idle, the budget's cap, ...). */
  onEnded: (cause: string) => void
  /** The socket is closed for good: a refusal's code, or 1006 once reconnecting gave up. */
  onClosed: (code: number) => void
}

export interface SpeechSocketOptions {
  open?: (url: string) => RawSpeechSocket
  url?: string
  now?: () => number
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (t: unknown) => void
}

interface Link {
  ws: RawSpeechSocket
  /** This link has said `ready` (or it is the first, whose statuses pass through). */
  ready: boolean
}

export class SpeechSocket {
  private active: Link | null = null
  private next: Link | null = null
  private retiring: Link | null = null
  private retireTimer: unknown = null
  private seq = 0
  private utterance: number | null = null
  private everReady = false
  private finished = false
  private rotateDue = false
  private rotateTimer: unknown = null
  private pingTimer: unknown = null
  private retryTimer: unknown = null
  private droppedAt: number | null = null
  private attempts = 0
  private readonly open: (url: string) => RawSpeechSocket
  private readonly url: string
  private readonly now: () => number
  private readonly setTimer: (fn: () => void, ms: number) => unknown
  private readonly clearTimer: (t: unknown) => void

  constructor(
    readonly sessionId: string,
    private readonly h: SpeechHandlers,
    opts: SpeechSocketOptions = {},
  ) {
    this.open = opts.open ?? ((url) => new WebSocket(url) as unknown as RawSpeechSocket)
    this.url = opts.url ?? speechSocketUrl(sessionId)
    this.now = opts.now ?? (() => Date.now())
    this.setTimer = opts.setTimer ?? ((fn, ms) => window.setTimeout(fn, ms))
    this.clearTimer = opts.clearTimer ?? ((t) => window.clearTimeout(t as number))
  }

  /** Whether an utterance is open on the socket. */
  get speaking(): boolean {
    return this.utterance !== null
  }

  connect(): void {
    if (this.finished || this.active !== null) return
    this.active = this.dial(true)
    this.schedulePing()
  }

  /** An utterance begins; returns its sequence number, or null when no socket is open to take it. */
  startUtterance(): number | null {
    const ws = this.active?.ws
    if (this.finished || ws === undefined || ws.readyState !== OPEN) return null
    if (this.utterance !== null) this.endUtterance()
    this.seq += 1
    this.utterance = this.seq
    ws.send(JSON.stringify({ t: 'utt.start', seq: this.seq }))
    return this.seq
  }

  /** One 20 ms PCM frame, sent only inside an utterance. */
  sendFrame(frame: ArrayBuffer): void {
    const ws = this.active?.ws
    if (this.utterance === null || ws === undefined || ws.readyState !== OPEN) return
    ws.send(frame)
  }

  endUtterance(): void {
    if (this.utterance === null) return
    const seq = this.utterance
    this.utterance = null
    const ws = this.active?.ws
    if (ws !== undefined && ws.readyState === OPEN) ws.send(JSON.stringify({ t: 'utt.end', seq }))
    // The silence a rotation waits for.
    if (this.rotateDue) this.rotate()
  }

  /** Stop the agent speaking (§3.3 step 6). */
  barge(): void {
    this.sendText({ t: 'barge' })
  }

  /** §8.3: the pointer rests on a visual. */
  point(visual: string, anchor: Record<string, unknown>): void {
    this.sendText({ t: 'point', visual, anchor })
  }

  /** Close for good: no reconnect, nothing more reported. */
  close(): void {
    if (this.finished) return
    this.finished = true
    this.endUtterance()
    this.clearAll()
    this.shutAll()
  }

  private shutAll(): void {
    for (const l of [this.active, this.next, this.retiring]) if (l !== null) this.shut(l)
    this.active = null
    this.next = null
    this.retiring = null
  }

  private sendText(m: Record<string, unknown>): void {
    const ws = this.active?.ws
    if (!this.finished && ws !== undefined && ws.readyState === OPEN) ws.send(JSON.stringify(m))
  }

  private dial(ready: boolean): Link {
    const ws = this.open(this.url)
    ws.binaryType = 'arraybuffer'
    const link: Link = { ws, ready }
    ws.onopen = () => {
      if (link === this.active) {
        this.attempts = 0
        this.droppedAt = null
      }
    }
    ws.onmessage = (ev) => this.message(link, ev.data)
    ws.onerror = () => undefined
    ws.onclose = (ev) => this.closed(link, ev.code)
    return link
  }

  private shut(link: Link): void {
    link.ws.onopen = null
    link.ws.onmessage = null
    link.ws.onclose = null
    link.ws.onerror = null
    try {
      link.ws.close(1000)
    } catch {
      // A socket that never opened has nothing to close.
    }
  }

  private message(link: Link, data: unknown): void {
    if (this.finished) return
    if (typeof data !== 'string') {
      if ((link === this.active || link === this.retiring) && data instanceof ArrayBuffer) this.h.onSayAudio(data)
      return
    }
    let m: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(data)
      if (parsed === null || typeof parsed !== 'object') return
      m = parsed as Record<string, unknown>
    } catch {
      return
    }
    if (link === this.next) {
      // The rotation's new socket: silent until it is ready, then it takes over.
      if (m.t === 'status' && m.state === READY) this.swap()
      return
    }
    if (link !== this.active && link !== this.retiring) return
    switch (m.t) {
      case 'status':
        // The retiring socket's state is not the session's any more.
        if (typeof m.state === 'string' && link === this.active) this.status(m.state)
        break
      case 'partial':
      case 'final':
        if (typeof m.seq === 'number' && typeof m.text === 'string') {
          if (m.t === 'partial') this.h.onPartial(m.seq, m.text)
          else this.h.onFinal(m.seq, m.text)
        }
        break
      case 'say.start':
        if (typeof m.id === 'string') this.h.onSayStart({ id: m.id, text: typeof m.text === 'string' ? m.text : '', pointAt: m.point_at ?? null })
        break
      case 'say.end':
        if (typeof m.id === 'string') this.h.onSayEnd(m.id)
        break
      case 'rotate':
        if (link !== this.active) break
        this.rotateDue = true
        if (this.utterance === null) this.rotate()
        else this.rotateTimer = this.setTimer(() => this.rotate(), ROTATE_WAIT_MS)
        break
      case 'ended':
        this.finished = true
        this.clearAll()
        this.shutAll()
        this.utterance = null
        this.h.onEnded(typeof m.cause === 'string' ? m.cause : '')
        break
      default:
        break
    }
  }

  private status(state: string): void {
    if (state === READY) {
      this.everReady = true
      this.h.onStatus(READY)
      return
    }
    // A reconnect re-dials the machine: its phases are not a second boot.
    if (this.everReady && bootPhase(state) !== null) {
      this.h.onStatus(RECONNECTING)
      return
    }
    this.h.onStatus(state)
  }

  private rotate(): void {
    this.rotateDue = false
    if (this.rotateTimer !== null) this.clearTimer(this.rotateTimer)
    this.rotateTimer = null
    if (this.finished || this.next !== null || this.active === null) return
    this.next = this.dial(false)
  }

  private swap(): void {
    const old = this.active
    const fresh = this.next
    if (fresh === null) return
    this.next = null
    fresh.ready = true
    this.active = fresh
    if (old === null) return
    // An utterance cannot span two sockets: one the old socket took ends there, and the next starts here.
    if (this.utterance !== null) {
      if (old.ws.readyState === OPEN) old.ws.send(JSON.stringify({ t: 'utt.end', seq: this.utterance }))
      this.utterance = null
    }
    if (this.retiring !== null) this.shut(this.retiring)
    if (this.retireTimer !== null) this.clearTimer(this.retireTimer)
    this.retiring = old
    this.retireTimer = this.setTimer(() => {
      this.retireTimer = null
      if (this.retiring === old) {
        this.retiring = null
        this.shut(old)
      }
    }, RETIRE_MS)
  }

  private closed(link: Link, code: number): void {
    if (this.finished) return
    if (link === this.retiring) {
      this.retiring = null
      return
    }
    if (link === this.next) {
      // The rotation's socket failed: the old one carries on, and its own close is handled when it comes.
      this.next = null
      return
    }
    if (link !== this.active) return
    this.utterance = null
    if (REFUSED_CODES.includes(code)) {
      this.giveUp(code)
      return
    }
    if (this.next !== null) {
      // Mid-rotation: the new socket takes over, its statuses passing through.
      this.active = this.next
      this.active.ready = true
      this.next = null
      this.h.onStatus(RECONNECTING)
      return
    }
    this.active = null
    if (this.droppedAt === null) this.droppedAt = this.now()
    if (this.now() - this.droppedAt >= RECONNECT_WINDOW_MS) {
      this.giveUp(code === 1000 ? 1006 : code)
      return
    }
    this.h.onStatus(RECONNECTING)
    const delay = RECONNECT_DELAYS_MS[Math.min(this.attempts, RECONNECT_DELAYS_MS.length - 1)] ?? 8_000
    this.attempts += 1
    this.retryTimer = this.setTimer(() => {
      this.retryTimer = null
      if (!this.finished && this.active === null) this.active = this.dial(true)
    }, delay)
  }

  private giveUp(code: number): void {
    this.finished = true
    this.clearAll()
    this.shutAll()
    this.h.onClosed(code)
  }

  private schedulePing(): void {
    this.pingTimer = this.setTimer(() => {
      this.pingTimer = null
      if (this.finished) return
      this.sendText({ t: 'ping' })
      const n = this.next?.ws
      if (n !== undefined && n.readyState === OPEN) n.send(JSON.stringify({ t: 'ping' }))
      this.schedulePing()
    }, PING_MS)
  }

  private clearAll(): void {
    for (const t of [this.pingTimer, this.retryTimer, this.rotateTimer, this.retireTimer]) if (t !== null) this.clearTimer(t)
    this.retireTimer = null
    this.pingTimer = null
    this.retryTimer = null
    this.rotateTimer = null
  }
}

// ---------------------------------------------------------------------------
// The spoken reply
// ---------------------------------------------------------------------------

type AudioCtor = new () => AudioContext

/**
 * Plays the reply's 24 kHz PCM as it arrives, chunk after chunk with no gap,
 * into a stream for the page's `<audio>` element and the ring. A browser
 * without Web Audio's stream destination plays nothing; the captions still
 * show what was said.
 */
export class SpeechPlayer {
  readonly stream: MediaStream | null
  private readonly ctx: AudioContext | null
  private readonly dest: MediaStreamAudioDestinationNode | null
  private nextAt = 0
  private live = new Set<AudioBufferSourceNode>()
  private carry: number | null = null

  constructor(private readonly onPlaying: (playing: boolean) => void = () => undefined) {
    const w = (typeof window === 'undefined' ? {} : window) as { AudioContext?: AudioCtor; webkitAudioContext?: AudioCtor }
    const Klass = w.AudioContext ?? w.webkitAudioContext
    let ctx: AudioContext | null = null
    let dest: MediaStreamAudioDestinationNode | null = null
    if (Klass !== undefined) {
      ctx = new Klass()
      if (typeof ctx.createMediaStreamDestination === 'function' && typeof ctx.createBuffer === 'function') {
        dest = ctx.createMediaStreamDestination()
      }
    }
    this.ctx = ctx
    this.dest = dest
    this.stream = dest?.stream ?? null
  }

  get playing(): boolean {
    return this.live.size > 0
  }

  push(pcm: ArrayBuffer): void {
    const ctx = this.ctx
    const dest = this.dest
    if (ctx === null || dest === null) return
    let bytes = new Uint8Array(pcm)
    if (this.carry !== null) {
      const joined = new Uint8Array(bytes.length + 1)
      joined[0] = this.carry
      joined.set(bytes, 1)
      bytes = joined
      this.carry = null
    }
    if (bytes.length % 2 === 1) {
      this.carry = bytes[bytes.length - 1] ?? 0
      bytes = bytes.subarray(0, bytes.length - 1)
    }
    const n = bytes.length / 2
    if (n === 0) return
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length)
    const buf = ctx.createBuffer(1, n, SAY_RATE)
    const ch = buf.getChannelData(0)
    for (let i = 0; i < n; i++) ch[i] = view.getInt16(i * 2, true) / 0x8000
    if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
    const src = ctx.createBufferSource()
    src.buffer = buf
    src.connect(dest)
    const at = Math.max(ctx.currentTime + 0.02, this.nextAt)
    this.nextAt = at + n / SAY_RATE
    const was = this.live.size
    this.live.add(src)
    src.onended = () => {
      this.live.delete(src)
      if (this.live.size === 0) this.onPlaying(false)
    }
    src.start(at)
    if (was === 0) this.onPlaying(true)
  }

  /** Barge-in: cut the reply off now. */
  stop(): void {
    const had = this.live.size > 0
    for (const s of this.live) {
      s.onended = null
      try {
        s.stop()
      } catch {
        // A source that already ended cannot be stopped again.
      }
    }
    this.live.clear()
    this.carry = null
    this.nextAt = 0
    if (had) this.onPlaying(false)
  }

  close(): void {
    this.stop()
    void this.ctx?.close().catch(() => undefined)
  }
}
