/**
 * VOICE IT'S CALL: the browser's WebRTC peer and swarm-api's session routes
 * (docs/design/voice-it.md §3.1, §3.5, §3.10).
 *
 * The audio goes straight between this page and OpenAI's GPT-Live session;
 * SwarmCloud carries only the offer and the answer. The page never holds an
 * OpenAI key or a client secret: swarm-api forwards the offer to swarm-voice,
 * which creates the session with the owner's key and answers the offer
 * itself, so the instructions and tools are enforced on the server where the
 * page cannot override them (§3.1, "The ephemeral session").
 *
 * TWO MODES (the owner's choice, 4 November 2026). The start answers `mode`:
 * "live" when an OpenAI API key account is in the Voice it pool (the GPT-Live
 * call above, with its SDP answer), or "text" when there is none and the
 * session talks on the ChatGPT plan. In text mode the peer made for the offer
 * is closed at once, unanswered, so no WebRTC connection is ever made: the
 * page keeps only the mic (for mute and the oscilloscope), hears the person
 * with the browser's speech recognition and sends each phrase as a turn
 * (`sendTurn`), and speaks the reply itself (speech.ts). A start that names
 * no mode is a live one, as every start was before.
 *
 * THE ONE PLACE THE SESSION ROUTES ARE CALLED. swarm-api's Voice it routes are
 * built by a parallel lane (section 10, "swarm-api starts and ends a Voice it
 * session"); every call to them is in `startVoiceSession` and
 * `endVoiceSession` below, so meeting their final shape is one edit here.
 */

import { API_RATE_LIMITED, route, write, type ApiError } from '../fetch'

/** Start: the browser's SDP offer in, the session id and OpenAI's SDP answer out. */
export const VOICE_SESSIONS = '/v1/voice/sessions'
/** End: closes the GPT-Live session, which is what stops OpenAI's per-second billing. */
export const VOICE_SESSION_END = '/v1/voice/sessions/{id}/end'
/** One spoken (or typed) turn of a text-mode session: its text in, the model's reply out. */
export const VOICE_SESSION_TURNS = '/v1/voice/sessions/{id}/turns'
/** The longest turn swarm-api takes. */
export const TURN_MAX = 4000

/**
 * The data channel OpenAI's WebRTC sessions send their server events on, the
 * captions among them. The page only reads it: everything the conversation
 * does goes through the server (§3.9), never a client event from here.
 */
export const EVENTS_CHANNEL = 'oai-events'

/**
 * The browser's own echo cancellation, noise suppression and gain: open mic
 * with barge-in works only if the agent's voice, played from the WebRTC track,
 * is cancelled out of the mic (§3.10; MDN `echoCancellation`).
 */
export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
}

export type TalkMode = 'live' | 'text'

export type StartedSession = { session_id: string; mode: 'live'; answer_sdp: string } | { session_id: string; mode: 'text' }

/** Why a start failed, in words for the page. */
export class TalkError extends Error {
  constructor(
    message: string,
    readonly api: ApiError | null = null,
  ) {
    super(message)
    this.name = 'TalkError'
  }
}

/**
 * `POST /v1/voice/sessions`: swarm-api checks the person and forwards the
 * offer. With `projectId` (a Resume), the session continues that project;
 * without it, swarm-api opens a new one.
 */
export async function startVoiceSession(offerSdp: string, projectId: string | null = null): Promise<StartedSession> {
  const body = projectId === null ? { offer_sdp: offerSdp } : { offer_sdp: offerSdp, project_id: projectId }
  const r = await write(route(VOICE_SESSIONS), 'POST', body)
  if (r.status === 'error' || r.status === 'stale') throw new TalkError(r.error.message, r.error)
  if (r.status !== 'ok') throw new TalkError('swarm-api answered the start with nothing.')
  const d = r.data as { session_id?: unknown; mode?: unknown; answer_sdp?: unknown } | null
  if (d === null || typeof d.session_id !== 'string' || d.session_id === '') {
    throw new TalkError("swarm-api's answer to the start carried no session id.")
  }
  if (d.mode === 'text') return { session_id: d.session_id, mode: 'text' }
  if (d.mode !== undefined && d.mode !== null && d.mode !== 'live') {
    throw new TalkError(`swarm-api's answer to the start named a mode this console does not know (${String(d.mode).slice(0, 40)}).`)
  }
  if (typeof d.answer_sdp !== 'string' || d.answer_sdp === '') {
    throw new TalkError("swarm-api's answer to the start carried no SDP answer.")
  }
  return { session_id: d.session_id, mode: 'live', answer_sdp: d.answer_sdp }
}

/** `POST /v1/voice/sessions/{id}/end`. Never throws: an end is also a page leaving. */
export async function endVoiceSession(sessionId: string): Promise<ApiError | null> {
  try {
    const r = await write(route(VOICE_SESSION_END, { id: sessionId }), 'POST')
    return r.status === 'error' || r.status === 'stale' ? r.error : null
  } catch {
    return null
  }
}

/** A text-mode turn's answer: the model's reply; its cards also arrive on the session's card stream. */
export interface TurnReply {
  turn_id: string
  reply: string
  cards: unknown[]
}

/** Why a turn was refused. `notLive` is swarm-api's 409: the session is over. */
export class TurnError extends Error {
  constructor(
    message: string,
    readonly api: ApiError | null = null,
    readonly notLive = false,
  ) {
    super(message)
    this.name = 'TurnError'
  }
}

/** When the plan's usage limit resets, from the refusal: its `detail` first, then `Retry-After`. */
export function limitResetsAt(e: ApiError, now: number = Date.now()): Date | null {
  const d = e.detail
  if (d !== null && typeof d === 'object') {
    const r = d as Record<string, unknown>
    for (const k of ['next_reset_at', 'resets_at', 'reset_at']) {
      const v = r[k]
      if (typeof v === 'string' && v !== '') {
        const t = Date.parse(v)
        if (Number.isFinite(t)) return new Date(t)
      }
    }
  }
  if (typeof e.retryAfterSeconds === 'number' && e.retryAfterSeconds > 0) return new Date(now + e.retryAfterSeconds * 1000)
  return null
}

/** The page's sentence for the plan's usage limit. */
export function limitSentence(at: Date | null): string {
  if (at === null) return "The ChatGPT plan's usage limit is reached; swarm-api did not say when it resets."
  const when = at.toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  return `The ChatGPT plan's usage limit is reached; it resets ${when}.`
}

/**
 * `POST /v1/voice/sessions/{id}/turns`: one phrase in, the reply out. A 429
 * that is not swarm-api's own request limiter is the plan's usage limit and
 * says when it resets; every other refusal carries swarm-api's own sentence.
 */
export async function sendTurn(sessionId: string, text: string): Promise<TurnReply> {
  const said = text.trim().slice(0, TURN_MAX)
  if (said === '') throw new TurnError('Nothing was said.')
  const r = await write(route(VOICE_SESSION_TURNS, { id: sessionId }), 'POST', { text: said })
  if (r.status === 'error' || r.status === 'stale') {
    const e = r.error
    if (e.httpStatus === 429 && e.code !== API_RATE_LIMITED) throw new TurnError(limitSentence(limitResetsAt(e)), e)
    const message = e.message.trim() !== '' ? e.message : 'swarm-api refused the turn.'
    throw new TurnError(message, e, e.httpStatus === 409)
  }
  const d = (r.status === 'ok' ? r.data : null) as { turn_id?: unknown; reply?: unknown; cards?: unknown } | null
  if (d === null || typeof d.reply !== 'string') throw new TurnError("swarm-api's answer to the turn carried no reply.")
  return {
    turn_id: typeof d.turn_id === 'string' ? d.turn_id : '',
    reply: d.reply,
    cards: Array.isArray(d.cards) ? d.cards : [],
  }
}

export interface TalkHandlers {
  /** One data-channel message, unparsed. */
  onEvent: (data: unknown) => void
  /** The agent's audio has arrived: play it, and tap the oscilloscope beside it. */
  onVoice: (stream: MediaStream) => void
  /** The peer dropped or failed after it was connected. */
  onDropped: () => void
}

export interface TalkCall {
  sessionId: string
  /** "text": no WebRTC; the page listens, sends turns and speaks the replies. */
  mode: TalkMode
  /** The mic's track: mute and push-to-talk set its `enabled`. */
  mic: MediaStreamTrack
  micStream: MediaStream
  /** Stop listening, close the peer and stop the mic. Ends nothing at swarm-api. */
  hangUp: () => void
}

/**
 * Open the call: the mic, one peer carrying its track, the events channel,
 * the offer to swarm-api and its answer back. On any failure everything
 * opened so far is closed before the error is thrown, so a refused start
 * never leaves the mic light on. `projectId` is the project a Resume
 * continues, sent with the offer; null starts a new project.
 */
export async function connectTalk(h: TalkHandlers, projectId: string | null = null): Promise<TalkCall> {
  if (typeof RTCPeerConnection === 'undefined' || typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    throw new TalkError('This browser cannot make a voice call (no WebRTC or no microphone access).')
  }
  let micStream: MediaStream
  try {
    micStream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS)
  } catch (e) {
    const name = e instanceof Error ? e.name : ''
    throw new TalkError(
      name === 'NotAllowedError'
        ? 'The browser was not allowed to use the microphone. Allow it for this site and start again.'
        : 'No microphone could be opened.',
    )
  }
  const mic = micStream.getAudioTracks()[0]
  if (mic === undefined) {
    micStream.getTracks().forEach((t) => t.stop())
    throw new TalkError('No microphone could be opened.')
  }
  const pc = new RTCPeerConnection()
  const channel = pc.createDataChannel(EVENTS_CHANNEL)
  let open = true
  const onMessage = (e: Event) => {
    if (open) h.onEvent((e as MessageEvent).data)
  }
  const onTrack = (e: Event) => {
    const t = e as RTCTrackEvent
    const stream = t.streams?.[0] ?? (t.track ? new MediaStream([t.track]) : null)
    if (open && stream !== null) h.onVoice(stream)
  }
  const onState = () => {
    if (open && (pc.connectionState === 'failed' || pc.connectionState === 'closed')) h.onDropped()
  }
  channel.addEventListener('message', onMessage)
  pc.addEventListener('track', onTrack)
  pc.addEventListener('connectionstatechange', onState)
  let peerOpen = true
  // Close the peer and keep the mic: a text session needs the mic but no WebRTC.
  const closePeer = () => {
    if (!peerOpen) return
    peerOpen = false
    channel.removeEventListener('message', onMessage)
    pc.removeEventListener('track', onTrack)
    pc.removeEventListener('connectionstatechange', onState)
    try {
      channel.close()
    } catch {
      // Closing a channel that never opened is not a failure to report.
    }
    pc.close()
  }
  const hangUp = () => {
    if (!open) return
    open = false
    closePeer()
    micStream.getTracks().forEach((t) => t.stop())
  }
  try {
    pc.addTrack(mic, micStream)
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    if (!offer.sdp) throw new TalkError('The browser made no SDP offer.')
    const started = await startVoiceSession(offer.sdp, projectId)
    if (started.mode === 'text') {
      closePeer()
      return { sessionId: started.session_id, mode: 'text', mic, micStream, hangUp }
    }
    try {
      await pc.setRemoteDescription({ type: 'answer', sdp: started.answer_sdp })
    } catch (e) {
      // The session exists at OpenAI: end it, so it is not billed while nobody can hear it.
      await endVoiceSession(started.session_id)
      throw e
    }
    return { sessionId: started.session_id, mode: 'live', mic, micStream, hangUp }
  } catch (e) {
    hangUp()
    if (e instanceof TalkError) throw e
    throw new TalkError('The call could not be set up in this browser.')
  }
}
