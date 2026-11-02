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
 * THE ONE PLACE THE SESSION ROUTES ARE CALLED. swarm-api's Voice it routes are
 * built by a parallel lane (section 10, "swarm-api starts and ends a Voice it
 * session"); every call to them is in `startVoiceSession` and
 * `endVoiceSession` below, so meeting their final shape is one edit here.
 */

import { route, write, type ApiError } from '../fetch'

/** Start: the browser's SDP offer in, the session id and OpenAI's SDP answer out. */
export const VOICE_SESSIONS = '/v1/voice/sessions'
/** End: closes the GPT-Live session, which is what stops OpenAI's per-second billing. */
export const VOICE_SESSION_END = '/v1/voice/sessions/{id}/end'

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

export interface StartedSession {
  session_id: string
  answer_sdp: string
}

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

/** `POST /v1/voice/sessions`: swarm-api checks the person and forwards the offer. */
export async function startVoiceSession(offerSdp: string): Promise<StartedSession> {
  const r = await write(route(VOICE_SESSIONS), 'POST', { offer_sdp: offerSdp })
  if (r.status === 'error' || r.status === 'stale') throw new TalkError(r.error.message, r.error)
  if (r.status !== 'ok') throw new TalkError('swarm-api answered the start with nothing.')
  const d = r.data as Partial<StartedSession> | null
  if (d === null || typeof d.session_id !== 'string' || typeof d.answer_sdp !== 'string' || d.answer_sdp === '') {
    throw new TalkError("swarm-api's answer to the start carried no session id or SDP answer.")
  }
  return { session_id: d.session_id, answer_sdp: d.answer_sdp }
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
 * never leaves the mic light on.
 */
export async function connectTalk(h: TalkHandlers): Promise<TalkCall> {
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
  const hangUp = () => {
    if (!open) return
    open = false
    channel.removeEventListener('message', onMessage)
    pc.removeEventListener('track', onTrack)
    pc.removeEventListener('connectionstatechange', onState)
    try {
      channel.close()
    } catch {
      // Closing a channel that never opened is not a failure to report.
    }
    pc.close()
    micStream.getTracks().forEach((t) => t.stop())
  }
  try {
    pc.addTrack(mic, micStream)
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    if (!offer.sdp) throw new TalkError('The browser made no SDP offer.')
    const started = await startVoiceSession(offer.sdp)
    try {
      await pc.setRemoteDescription({ type: 'answer', sdp: started.answer_sdp })
    } catch (e) {
      // The session exists at OpenAI: end it, so it is not billed while nobody can hear it.
      await endVoiceSession(started.session_id)
      throw e
    }
    return { sessionId: started.session_id, mic, micStream, hangUp }
  } catch (e) {
    hangUp()
    if (e instanceof TalkError) throw e
    throw new TalkError('The call could not be set up in this browser.')
  }
}
