/**
 * VOICE IT, TALK: the presence layout's first working slice
 * (docs/design/voice-it.md §3.10, §11; section 10, phase one).
 *
 * The layout is the Jarvis-like presence direction the owner picked: the
 * voice as a luminous ring at the centre of a quiet stage, the captions as a
 * subtitle band under it, the mic on the bottom edge, and the cards around
 * it. Phase one is the talk itself: the call, the ring, both sides'
 * captions, mute and push-to-talk. Cards are phase two, so their area is
 * drawn empty and says so rather than showing anything invented.
 *
 * WHAT THE PAGE HOLDS. The WebRTC peer, the mic and the agent's track, the
 * captions and the ring (§3.2). It holds no key and no client secret: the
 * session is created by swarm-voice behind swarm-api (rtc.ts).
 *
 * SPEECH IS INPUT. Every final caption, from either side, while a session is
 * live calls Shell.tsx's `noteActivity`, so the console's fifteen-minute idle
 * stop does not pause the screens of someone who is only talking. When the
 * session ends the call is hung up and nothing calls it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { Button, Segmented, ToneMark } from '../components'
import { noteActivity } from '../Shell'
import { applyCaption, captionEvent, trimCaptions, type CaptionLine } from './captions'
import { micWord, useMic, type MicMode } from './mic'
import { connectTalk, endVoiceSession, type TalkCall } from './rtc'
import { Ring, type ScopeState } from './Scope'
import './talk-pane.css'

type Phase = 'idle' | 'connecting' | 'live' | 'ending'

const WHO: Record<CaptionLine['side'], string> = { you: 'You', agent: 'Voice it' }

/**
 * OpenAI's WebRTC sessions say when the agent's audio starts and stops
 * playing; the ring blooms between them. A partial agent caption counts too,
 * for a session that sends only transcripts.
 */
const VOICE_STARTED = new Set(['output_audio_buffer.started', 'session.output_audio.started'])
const VOICE_STOPPED = new Set(['output_audio_buffer.stopped', 'output_audio_buffer.cleared', 'session.output_audio.stopped'])

function eventType(data: unknown): string | null {
  if (typeof data !== 'string') return null
  try {
    const m = JSON.parse(data) as { type?: unknown }
    return typeof m?.type === 'string' ? m.type : null
  } catch {
    return null
  }
}

function MicGlyph({ off = false }: { off?: boolean }) {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="16" height="16">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      {off && <path d="M4 4l16 16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />}
    </svg>
  )
}

/**
 * Both sides, speaker-labelled, newest last. The log holds final lines only,
 * so a reader hears each segment once, whole; the line still being spoken is
 * drawn muted under it and hidden from the reader.
 */
function Captions({ lines }: { lines: CaptionLine[] }) {
  const finals = lines.filter((l) => l.final)
  const partials = lines.filter((l) => !l.final)
  const box = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight
  }, [lines])
  return (
    <div ref={box} className="vt-subs">
      <div className="vt-captions" role="log" aria-live="polite" aria-relevant="additions" aria-label="Captions">
        {finals.map((l) => (
          <p key={l.id} className={`vt-cap is-${l.side}`}>
            <span className="vt-cap-who">{WHO[l.side]}</span>
            <span className="vt-cap-text">{l.text}</span>
          </p>
        ))}
      </div>
      {partials.length > 0 && (
        <div className="vt-captions is-partials" aria-hidden="true">
          {partials.map((l) => (
            <p key={l.id} className={`vt-cap is-${l.side} is-partial`}>
              <span className="vt-cap-who">{WHO[l.side]}</span>
              <span className="vt-cap-text">
                {l.text}
                <span className="vt-cap-more">…</span>
              </span>
            </p>
          ))}
        </div>
      )}
      {lines.length === 0 && <p className="vt-cap-empty">Nothing said yet.</p>}
    </div>
  )
}

export function TalkPane() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [problem, setProblem] = useState<string | null>(null)
  const [lines, setLines] = useState<CaptionLine[]>([])
  const [voice, setVoice] = useState<MediaStream | null>(null)
  const [micStream, setMicStream] = useState<MediaStream | null>(null)
  const [track, setTrack] = useState<MediaStreamTrack | null>(null)
  const [voicePlaying, setVoicePlaying] = useState(false)
  const call = useRef<TalkCall | null>(null)
  const mounted = useRef(true)
  const audio = useRef<HTMLAudioElement | null>(null)
  const live = phase === 'live'
  const { mic, setMuted, setMode, hold } = useMic(track, live)

  const onEvent = useCallback((data: unknown) => {
    const type = eventType(data)
    if (type !== null && VOICE_STARTED.has(type)) setVoicePlaying(true)
    if (type !== null && VOICE_STOPPED.has(type)) setVoicePlaying(false)
    const ev = captionEvent(data)
    if (ev === null) return
    // Speech is proof someone is there; a word still being spoken is not yet a segment.
    if (ev.final) noteActivity()
    setLines((l) => trimCaptions(applyCaption(l, ev)))
  }, [])

  const hangUp = useCallback((): TalkCall | null => {
    const c = call.current
    call.current = null
    c?.hangUp()
    setVoice(null)
    setMicStream(null)
    setTrack(null)
    setVoicePlaying(false)
    // A segment cut off mid-word stays as it was heard, not as a growing line.
    setLines((l) => l.filter((x) => x.final))
    return c
  }, [])

  const end = useCallback(async () => {
    const c = hangUp()
    if (c === null) return
    setPhase('ending')
    const refused = await endVoiceSession(c.sessionId)
    if (!mounted.current) return
    setProblem(
      refused === null
        ? null
        : `swarm-api did not confirm the end (${refused.message}). The call here is closed; Voice it ends a session after ten minutes of silence.`,
    )
    setPhase('idle')
  }, [hangUp])

  const start = useCallback(async () => {
    setProblem(null)
    setLines([])
    setPhase('connecting')
    try {
      const c = await connectTalk({
        onEvent,
        onVoice: (s) => setVoice(s),
        onDropped: () => void end(),
      })
      if (!mounted.current) {
        c.hangUp()
        void endVoiceSession(c.sessionId)
        return
      }
      call.current = c
      setMicStream(c.micStream)
      setTrack(c.mic)
      setPhase('live')
    } catch (e) {
      if (!mounted.current) return
      setProblem(`Voice it could not start: ${e instanceof Error ? e.message : 'the call failed.'}`)
      setPhase('idle')
    }
  }, [onEvent, end])

  // Leaving the page ends the session: a call nobody can hear is still billed per second.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      const c = call.current
      call.current = null
      if (c !== null) {
        c.hangUp()
        void endVoiceSession(c.sessionId)
      }
    }
  }, [])

  // The agent's voice plays from the WebRTC track itself, never through Web Audio.
  useEffect(() => {
    if (audio.current) audio.current.srcObject = voice
  }, [voice])

  const speaking = voicePlaying || lines.some((l) => l.side === 'agent' && !l.final)
  const state: ScopeState = phase === 'connecting' ? 'connecting' : live ? (speaking ? 'speaking' : 'listening') : 'idle'

  return (
    <div className="vt" data-state={state}>
      <div className="vt-sky" aria-hidden="true" />

      <div className="vt-hud">
        <p className="vt-status" role="status">
          <ToneMark tone={live ? 'live' : 'unknown'} />
          {phase === 'idle' && 'Not connected'}
          {phase === 'connecting' && 'Connecting to Voice it…'}
          {phase === 'live' && (mic.muted ? 'Live, muted' : 'Live')}
          {phase === 'ending' && 'Ending the session…'}
        </p>
        <span className="vt-hud-actions">
          {live || phase === 'ending' ? (
            <Button kind="secondary" size="sm" onClick={() => void end()} busy={phase === 'ending'}>
              End
            </Button>
          ) : (
            <Button kind="primary" size="sm" onClick={() => void start()} busy={phase === 'connecting'}>
              Start talking
            </Button>
          )}
        </span>
        <p className="vt-note">Headphones are advised: the browser's echo cancellation is what lets you talk over Voice it.</p>
        {problem !== null && (
          <p className="vt-problem" role="alert">
            {problem}
          </p>
        )}
      </div>

      <div className="vt-presence" aria-label="Voice it">
        <div className="vt-core">
          <Ring state={state} voice={voice} mic={live ? micStream : null} />
        </div>
        <div className="vt-subs-band">
          <Captions lines={lines} />
        </div>
        <div className="vt-mic">
          <div className="vt-mic-group" role="group" aria-label="Microphone">
            <Button kind="secondary" aria-pressed={mic.muted} icon={<MicGlyph off={mic.muted} />} onClick={() => setMuted(!mic.muted)} disabled={!live}>
              {mic.muted ? 'Unmute' : 'Mute'}
            </Button>
            <Segmented<MicMode>
              small
              label="How the mic listens"
              value={mic.mode}
              options={[
                { key: 'open', label: 'Open mic', title: 'Always listening; talk over Voice it to interrupt' },
                { key: 'ptt', label: 'Push to talk', title: 'Listens only while you hold Space or the button' },
              ]}
              onChange={setMode}
            />
            {mic.mode === 'ptt' && (
              <Button
                kind={mic.held ? 'primary' : 'secondary'}
                aria-pressed={mic.held}
                disabled={!live || mic.muted}
                onPointerDown={() => hold(true)}
                onPointerUp={() => hold(false)}
                onPointerLeave={() => hold(false)}
                onPointerCancel={() => hold(false)}
                onContextMenu={(e) => e.preventDefault()}
              >
                Hold to talk
              </Button>
            )}
            {live && <span className="vt-mic-state">{micWord(mic)}</span>}
          </div>
        </div>
      </div>

      <section className="vt-cards" aria-label="Cards">
        <p className="vt-cards-h">Cards</p>
        <p className="vt-cards-none">Cards arrive in phase two: what the conversation designs, plans and starts will appear here.</p>
      </section>

      <audio ref={audio} autoPlay className="vt-voice" />
    </div>
  )
}
