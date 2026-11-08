/**
 * VOICE IT, TALK: the presence layout's first working slice
 * (docs/design/voice-it.md §3.10, §11; section 10, phase one).
 *
 * The layout is the Jarvis-like presence direction the owner picked: the
 * voice as a luminous ring at the centre of a quiet stage, the captions as a
 * subtitle band under it, the mic on the bottom edge, and the cards around
 * it. Phase one is the talk itself: the call, the ring, both sides'
 * captions, mute and push-to-talk. The cards beside it are the session's
 * card stream (CardList.tsx), drawn from the catalogue only.
 *
 * WHAT THE PAGE HOLDS. The WebRTC peer, the mic and the agent's track, the
 * captions and the ring (§3.2). It holds no key and no client secret: the
 * session is created by swarm-voice behind swarm-api (rtc.ts).
 *
 * SPEECH IS INPUT. Every final caption, from either side, while a session is
 * live calls Shell.tsx's `noteActivity`, so the console's fifteen-minute idle
 * stop does not pause the screens of someone who is only talking. When the
 * session ends the call is hung up and nothing calls it.
 *
 * RESUME (§3.7). Projects.tsx hands over the project a Resume continues
 * (`takeResumedProject`); the first Start talking takes it and sends it as
 * `project_id`, so the session continues that project. It is kept until a
 * start succeeds, so a refused start (no mic, swarm-voice away) retries the
 * same project; after that, a start opens a new project.
 *
 * TWO MODES (the owner's choice, 4 November 2026). The start answers "live"
 * (an OpenAI API key is in the Voice it pool: the GPT-Live call above) or
 * "text" (the ChatGPT plan). A text session opens no WebRTC: the browser's
 * speech recognition hears the person (the phrase being heard is the live
 * caption), each final phrase is one turn to swarm-api, both sides are
 * captioned from the turns, and the reply is spoken by speechSynthesis in one
 * voice (speech.ts). Speaking again, Stop speaking or End cancels the reply
 * being spoken. Mute and push-to-talk start and stop the recogniser as well
 * as the mic track, so a muted word is never heard, and a recognised phrase
 * counts as activity for the idle stop. Where the browser has no speech
 * recognition, or refuses it, the pane says so in one line and offers a text
 * box that sends the same turns. The ring draws the mic's level, and the
 * spoken reply as a simple level. A 429 says when the plan's limit resets;
 * a 409 (the session is over) ends the call here; any other refusal shows
 * swarm-api's own sentence. A turn's cards arrive on the card stream, where
 * swarm-api's `write_card` puts every card, so they are drawn once, there.
 *
 * THE STAGE (Stage.tsx) sits under the heads-up edge: the project's visuals,
 * their notes and the read-back the gate holds. It reads the project the
 * session continues; the console's fixture mode draws the fixture project.
 *
 * THE METER (Meter.tsx) sits beside the cards once a session has started:
 * the session's and the day's spent and reserved lines against $100 and
 * $300, as swarm-api's ledger computes them.
 *
 * OUR OWN SPEECH (docs/design/voice-it-own-speech.md §3.3, §4, §9; row U1).
 * The page reads Settings' engine when it opens. On `own`, Start opens the
 * mic and starts with no offer (speechSocket.ts `connectOwnSpeech`): a
 * `speech` session opens the audio socket and voice detection (vad.ts), so
 * only speech leaves the browser; a 422 for the offer is GPT-Live, which
 * makes today's call; an engine not read yet makes today's call too. While
 * the machine starts, the status line is its boot phases, each done one a
 * tick and the current one in words, nothing more. Then both sides are
 * captioned (the person's partials and finals, the agent's `say.start`), the
 * reply plays from the socket's PCM through the `<audio>` element and the
 * ring draws its trace, and speaking over it stops it (barge-in). Mute and
 * push-to-talk are the same controls, and also gate what voice detection
 * sends: muted, nothing; push-to-talk, what is said while held.
 *
 * THE FALLBACK (§4.1, §4.7; the owner's answers). When the engine cannot
 * start (no L4 free, the machine not ready) the start answers a text session
 * with its `fallback`; when it goes down mid-session (`engine down`, or the
 * socket refused or gone for good) the session carries on in text mode. One
 * line says why, and a close button dismisses it. Text mode keeps the
 * browser's microphone and its speech recognition, with a text box beside it.
 *
 * BY VOICE. Every control also answers a whole spoken phrase (`spokenAction`):
 * "stop the session" ends it, "mute", "push to talk", "open mic" and "stop
 * speaking" do what their buttons do. A read-back's Go and Not that are heard
 * by swarm-api on our own engine (the relay's read-back matcher); in text
 * mode the page posts a phrase that can answer one to the read-back answer
 * route first, and sends it as a turn only when it answered nothing.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { normVoiceSettings, USE_FIXTURES, VOICE_SETTINGS } from '../api'
import { Button, buttonClass, Segmented, ToneMark } from '../components'
import { read, route, write } from '../fetch'
import { noteActivity } from '../Shell'
import { CardList } from './CardList'
import { applyCaption, captionEvent, trimCaptions, type CaptionLine } from './captions'
import { BudgetMeter } from './Meter'
import { micEnabled, micWord, useMic, type MicMode, type MicState } from './mic'
import { takeResumedProject } from './Projects'
import { connectTalk, endVoiceSession, sendTurn, TalkError, TURN_MAX, TurnError, type TalkCall, type TalkMode } from './rtc'
import { Ring, type ScopeState } from './Scope'
import { Listener, NO_RECOGNITION, recogniserClass, Speaker } from './speech'
import {
  bootPhase,
  connectOwnSpeech,
  ENGINE_DOWN,
  GPT_LIVE,
  READY,
  SpeechPlayer,
  SpeechSocket,
  type BootPhase,
  type SpeechCall,
  type SpeechFallback,
} from './speechSocket'
import { stageFeed, TalkStage, TalkTip } from './Stage'
import { startVoiceDetection, type VoiceDetection } from './vad'
import '../styles/talk-pane.css'

type Phase = 'idle' | 'connecting' | 'live' | 'ending'

/** The call the page holds: GPT-Live's or a text session (rtc.ts), or our own engine's (speechSocket.ts). */
type AnyCall = TalkCall | SpeechCall
type PaneMode = TalkMode | 'speech'

const WHO: Record<CaptionLine['side'], string> = { you: 'You', agent: 'Voice it' }

/** The caption of the phrase a text session is hearing right now. */
const HEARD = 'you:heard'

/** The lines with the phrase being heard set to `text` (or removed, for ''). */
function withHeard(lines: readonly CaptionLine[], text: string): CaptionLine[] {
  return withPartial(lines, HEARD, text)
}

/** The lines with the partial line `id` set to `text` (or removed, for ''); a final line is never reopened. */
function withPartial(lines: readonly CaptionLine[], id: string, text: string): CaptionLine[] {
  if (lines.some((l) => l.id === id && l.final)) return [...lines]
  const at = lines.findIndex((l) => l.id === id)
  if (text === '') return lines.filter((l) => l.id !== id)
  const line: CaptionLine = { id, side: 'you', text, final: false }
  return at === -1 ? [...lines, line] : lines.map((l, i) => (i === at ? line : l))
}

/** A phrase as the matchers compare it: lower case, no punctuation, no fillers. */
export function normalPhrase(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}' ]+/gu, ' ')
    .split(/\s+/)
    .filter((w) => w !== '' && !['uh', 'um', 'okay', 'please'].includes(w))
    .join(' ')
}

export type SpokenAction = 'end' | 'mute' | 'ptt' | 'open' | 'quiet'

/** The controls by voice: a whole phrase, never a word inside a longer sentence. */
const SPOKEN: Readonly<Record<string, SpokenAction>> = {
  'stop the session': 'end',
  'end the session': 'end',
  mute: 'mute',
  'mute the mic': 'mute',
  'push to talk': 'ptt',
  'open mic': 'open',
  'stop speaking': 'quiet',
}

export function spokenAction(text: string): SpokenAction | null {
  return SPOKEN[normalPhrase(text)] ?? null
}

/**
 * The phrases that can answer a read-back (swarm_api/voice/readback.py, §7.3).
 * swarm-api decides; the page only spares it the sentences that cannot be one.
 */
const READBACK_PHRASES = new Set(['go', 'go ahead', 'yes go', 'do it', 'confirm', 'yes do it', 'not that', 'no', 'stop', 'cancel', "don't", 'wait'])

export function mayAnswerReadback(text: string): boolean {
  return READBACK_PHRASES.has(normalPhrase(text))
}

/** `POST /v1/voice/sessions/{id}/readbacks/answer`: true when swarm-api took the phrase as Go or Not that. */
async function answeredReadback(sessionId: string, text: string): Promise<boolean> {
  try {
    const r = await write(route('/v1/voice/sessions/{session_id}/readbacks/answer', { session_id: sessionId }), 'POST', { text })
    if (r.status !== 'ok') return false
    const d = r.data as { answer?: unknown } | null
    return d !== null && (d.answer === 'go' || d.answer === 'refuse')
  } catch {
    return false
  }
}

/** The one line a fallback draws (§4.7). */
export function fallbackLine(f: SpeechFallback): string {
  if (f.reason === 'no_l4') return f.zone === null ? 'No L4 free.' : `No L4 free in ${f.zone}.`
  if (f.reason === 'not_ready') return 'Speech machine not ready.'
  if (f.reason === 'engine_down') return 'Engine down.'
  return 'Own speech unavailable.'
}

function CloseGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

/** The machine's start (§4.3): each phase done is a tick, named for a reader; the current one is in words. */
function BootPhases({ boot }: { boot: readonly BootPhase[] }) {
  const done = boot.slice(0, -1)
  const now = boot[boot.length - 1]
  return (
    <span className="vt-status" role="list" aria-label="Starting">
      {done.map((p) => (
        <span key={p} role="listitem" aria-label={p} title={p}>
          <ToneMark tone="ok" />
        </span>
      ))}
      {now !== undefined && (
        <span role="listitem" aria-current="step">
          {now}
        </span>
      )}
    </span>
  )
}

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

/**
 * Why a start failed, for the page. swarm-api's refusal is shown as served:
 * its sentence already says what happened and that nothing was started (a 503
 * while swarm-voice is not deployed here, a 403 for anyone it is not open
 * to), so it is not wrapped in words of the page's own. A failure in this
 * browser has no such sentence and is headed as one.
 */
function startProblem(e: unknown): string {
  if (e instanceof TalkError && e.api !== null && e.api.message.trim() !== '') return e.api.message
  return `Voice it could not start: ${e instanceof Error ? e.message : 'the call failed.'}`
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
  // The last session's id: its meter stays on the pane after it ends.
  const [sessionId, setSessionId] = useState<string | null>(null)
  // The project the stage draws visuals from: the Resume's, the one a start continues.
  const [projectId, setProjectId] = useState<string | null>(null)
  const [talkMode, setTalkMode] = useState<PaneMode>('live')
  // Our own engine: the machine's boot phases seen so far, then the session's state word.
  const [boot, setBoot] = useState<BootPhase[]>([])
  const [speechState, setSpeechState] = useState<string | null>(null)
  // Why this session is in text mode instead of on its engine; null once dismissed.
  const [fallback, setFallback] = useState<SpeechFallback | null>(null)
  // A text session's spoken reply, 0..1, or null when nothing is spoken.
  const [level, setLevel] = useState<number | null>(null)
  // Why a text session types instead of listening; null while it listens.
  const [typed, setTyped] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const call = useRef<AnyCall | null>(null)
  const socket = useRef<SpeechSocket | null>(null)
  const player = useRef<SpeechPlayer | null>(null)
  const detection = useRef<VoiceDetection | null>(null)
  // Settings' engine, read when the page opens; null until read, or when it could not be.
  const engine = useRef<string | null>(null)
  const listener = useRef<Listener | null>(null)
  const speaker = useRef<Speaker | null>(null)
  // Turns go to swarm-api one after another, in the order they were said.
  const turns = useRef<Promise<void>>(Promise.resolve())
  const turnSeq = useRef(0)
  const mounted = useRef(true)
  const audio = useRef<HTMLAudioElement | null>(null)
  // The card list reads the session's one stream and hands the stage its visual, note and focus events through this.
  const feed = useMemo(stageFeed, [])
  // The Resume's project: undefined until the first start takes it, then the
  // id until a start succeeds, then null.
  const resumed = useRef<string | null | undefined>(undefined)
  const live = phase === 'live'
  const textMode = live && talkMode === 'text'
  const speechMode = live && talkMode === 'speech'
  const { mic, setMuted, setMode, hold } = useMic(track, live)
  const micRef = useRef<MicState>(mic)
  micRef.current = mic

  // Settings' engine, from the route itself: the fixture console has no machine to start, so it makes today's call.
  useEffect(() => {
    if (USE_FIXTURES) return
    let stop = false
    void read<unknown>(route(VOICE_SETTINGS), () => false).then((r) => {
      if (!stop && (r.status === 'ok' || r.status === 'stale')) engine.current = normVoiceSettings(r.data).settings.engine || null
    })
    return () => {
      stop = true
    }
  }, [])

  /** Our own engine's parts: the socket, the player and voice detection. The mic is the call's. */
  const closeSpeech = useCallback(() => {
    socket.current?.close()
    socket.current = null
    player.current?.close()
    player.current = null
    detection.current?.close()
    detection.current = null
    setBoot([])
    setSpeechState(null)
  }, [])

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

  const hangUp = useCallback((): AnyCall | null => {
    const c = call.current
    call.current = null
    c?.hangUp()
    closeSpeech()
    listener.current?.close()
    listener.current = null
    speaker.current?.cancel()
    speaker.current = null
    setLevel(null)
    setVoice(null)
    setMicStream(null)
    setTrack(null)
    setVoicePlaying(false)
    // A segment cut off mid-word stays as it was heard, not as a growing line.
    setLines((l) => l.filter((x) => x.final))
    return c
  }, [closeSpeech])

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

  /** One turn of a text session: caption it, send it, caption and speak the reply. */
  const say = useCallback(
    (said: string) => {
      const c = call.current
      const words = said.trim().slice(0, TURN_MAX)
      if (c === null || c.mode !== 'text' || words === '') return
      // A phrase is proof someone is there, and speaking again cuts the reply off.
      noteActivity()
      speaker.current?.cancel()
      turnSeq.current += 1
      const key = `t${turnSeq.current}`
      setLines((l) =>
        trimCaptions([
          ...l.filter((x) => x.id !== HEARD),
          { id: `you:${key}`, side: 'you', text: words, final: true },
          { id: `agent:${key}`, side: 'agent', text: '', final: false },
        ]),
      )
      turns.current = turns.current.then(async () => {
        if (call.current !== c) return
        try {
          // "Go" or "Not that" answering a held read-back is not a turn (§7.3).
          if (mayAnswerReadback(words) && (await answeredReadback(c.sessionId, words))) {
            if (mounted.current) setLines((l) => l.filter((x) => x.id !== `agent:${key}`))
            return
          }
          if (call.current !== c) return
          const r = await sendTurn(c.sessionId, words)
          if (!mounted.current || call.current !== c) return
          noteActivity()
          setProblem(null)
          setLines((l) => trimCaptions(applyCaption(l, { side: 'agent', key, text: r.reply, final: true })))
          speaker.current?.speak(r.reply)
        } catch (e) {
          if (!mounted.current || call.current !== c) return
          setLines((l) => l.filter((x) => x.id !== `agent:${key}`))
          setProblem(e instanceof Error && e.message !== '' ? e.message : 'swarm-api refused the turn.')
          // A session that is no longer live has nothing to end: the call here closes.
          if (e instanceof TurnError && e.notLive) {
            hangUp()
            setPhase('idle')
          }
        }
      })
    },
    [hangUp],
  )
  const sayRef = useRef(say)
  sayRef.current = say

  /** A whole spoken phrase that is a control does what its button does; true when it was one. */
  const act = useCallback((text: string): boolean => {
    const a = spokenAction(text)
    if (a === null) return false
    if (a === 'end') void endRef.current()
    else if (a === 'mute') setMutedRef.current(true)
    else if (a === 'ptt') setModeRef.current('ptt')
    else if (a === 'open') setModeRef.current('open')
    else {
      speaker.current?.cancel()
      if (player.current?.playing) {
        player.current.stop()
        socket.current?.barge()
      }
    }
    return true
  }, [])
  const endRef = useRef(end)
  endRef.current = end
  const setMutedRef = useRef(setMuted)
  setMutedRef.current = setMuted
  const setModeRef = useRef(setMode)
  setModeRef.current = setMode

  /** A text session's browser parts: the voice that reads replies, and the recogniser on the kept mic. */
  const beginText = useCallback(() => {
    speaker.current = new Speaker({ onLevel: (v) => mounted.current && setLevel(v) })
    const Klass = recogniserClass()
    if (Klass === null) setTyped(NO_RECOGNITION)
    else
      listener.current = new Listener(Klass, {
        onInterim: (heard) => {
          if (!mounted.current) return
          if (heard !== '') speaker.current?.cancel()
          setLines((l) => withHeard(l, heard))
        },
        onFinal: (phrase) => {
          if (actRef.current(phrase)) {
            setLines((l) => withHeard(l, ''))
            return
          }
          sayRef.current(phrase)
        },
        onUnavailable: (why) => mounted.current && setTyped(why),
      })
  }, [])
  const actRef = useRef(act)
  actRef.current = act

  /** Our own engine went down mid-session: the session carries on in text mode (§4.5), on the same mic. */
  const toText = useCallback(
    (c: SpeechCall, reason: string) => {
      if (call.current !== c || !mounted.current) return
      closeSpeech()
      const text: SpeechCall = { ...c, mode: 'text', fallback: { from: 'own', reason, zone: null } }
      call.current = text
      setVoice(null)
      setVoicePlaying(false)
      setLines((l) => l.filter((x) => x.final))
      setFallback(text.fallback)
      setTalkMode('text')
      beginText()
    },
    [closeSpeech, beginText],
  )

  /** A `speech` session: the player, the socket, and voice detection on the mic. */
  const beginSpeech = useCallback(
    (c: SpeechCall) => {
      const p = new SpeechPlayer((playing) => mounted.current && setVoicePlaying(playing))
      player.current = p
      setVoice(p.stream)
      setBoot([])
      setSpeechState(null)
      const s = new SpeechSocket(c.sessionId, {
        onStatus: (state) => {
          if (!mounted.current || call.current !== c) return
          const phase = bootPhase(state)
          if (phase !== null) setBoot((b) => (b.includes(phase) ? b : [...b, phase]))
          else if (state === ENGINE_DOWN) toText(c, 'engine_down')
          else {
            setBoot([])
            setSpeechState(state === READY ? null : state)
          }
        },
        onPartial: (seq, text) => mounted.current && setLines((l) => trimCaptions(withPartial(l, `you:u${seq}`, text))),
        onFinal: (seq, text) => {
          if (!mounted.current) return
          // Speech is proof someone is there.
          noteActivity()
          setLines((l) => trimCaptions(applyCaption(l, { side: 'you', key: `u${seq}`, text, final: true })))
          act(text)
        },
        onSayStart: (say) => {
          if (!mounted.current) return
          noteActivity()
          setLines((l) => trimCaptions(applyCaption(l, { side: 'agent', key: say.id, text: say.text, final: true })))
        },
        onSayAudio: (pcm) => player.current?.push(pcm),
        onSayEnd: () => undefined,
        onEnded: () => {
          if (!mounted.current || call.current !== c) return
          // swarm-api ended it (idle, the cap): there is nothing left to end from here.
          hangUp()
          setPhase('idle')
        },
        onClosed: (code) => {
          if (!mounted.current || call.current !== c) return
          if (code === 4401 || code === 4403 || code === 4404) {
            setProblem(`The speech socket was refused (${code}).`)
            void end()
          } else toText(c, 'engine_down')
        },
      })
      socket.current = s
      s.connect()
      void startVoiceDetection(
        c.micStream,
        {
          onStart: () => {
            // Speaking over the agent stops it at once (§3.3 step 6).
            if (player.current?.playing) {
              player.current.stop()
              s.barge()
            }
            s.startUtterance()
          },
          onFrame: (f) => s.sendFrame(f),
          onEnd: () => s.endUtterance(),
        },
        micRef.current,
      ).then(
        (d) => {
          if (call.current === c && socket.current === s) detection.current = d
          else d.close()
        },
        (e: unknown) => {
          if (mounted.current && call.current === c) setProblem(e instanceof Error ? e.message : 'Voice detection did not start.')
        },
      )
    },
    [act, end, hangUp, toText],
  )

  const start = useCallback(async () => {
    setProblem(null)
    setLines([])
    setTyped(null)
    setDraft('')
    setFallback(null)
    setPhase('connecting')
    if (resumed.current === undefined) resumed.current = takeResumedProject()
    setProjectId(resumed.current)
    try {
      const own = engine.current === 'own' ? await connectOwnSpeech(resumed.current) : GPT_LIVE
      const c: AnyCall =
        own !== GPT_LIVE
          ? own
          : await connectTalk(
              {
                onEvent,
                onVoice: (s) => setVoice(s),
                onDropped: () => void end(),
              },
              resumed.current,
            )
      resumed.current = null
      if (!mounted.current) {
        c.hangUp()
        void endVoiceSession(c.sessionId)
        return
      }
      call.current = c
      // A new session's project is the one swarm-api opened: the stage reads its visuals.
      setProjectId(c.projectId)
      setTalkMode(c.mode)
      if (c.mode === 'text') {
        if ('fallback' in c) setFallback(c.fallback)
        beginText()
      } else if (c.mode === 'speech') beginSpeech(c)
      setSessionId(c.sessionId)
      setMicStream(c.micStream)
      setTrack(c.mic)
      setPhase('live')
    } catch (e) {
      if (!mounted.current) return
      setProblem(startProblem(e))
      setPhase('idle')
    }
  }, [onEvent, end, beginText, beginSpeech])

  // Leaving the page ends the session: a call nobody can hear is still billed per second.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      listener.current?.close()
      listener.current = null
      speaker.current?.cancel()
      speaker.current = null
      socket.current?.close()
      socket.current = null
      player.current?.close()
      player.current = null
      detection.current?.close()
      detection.current = null
      const c = call.current
      call.current = null
      if (c !== null) {
        c.hangUp()
        void endVoiceSession(c.sessionId)
      }
    }
  }, [])

  // A text session hears only while the mic would send: muting discards the
  // phrase being heard, letting go of push-to-talk still sends it.
  const hearing = textMode && typed === null && micEnabled(mic)
  useEffect(() => {
    const l = listener.current
    if (l === null) return
    if (hearing) l.listen(true)
    else if (mic.muted) l.abort()
    else l.listen(false)
  }, [hearing, mic.muted, track])

  // Voice detection sends only what the mic would: nothing muted, and on push-to-talk what is said while held.
  useEffect(() => {
    detection.current?.setMic(mic)
  }, [mic])

  // The agent's voice plays from the WebRTC track itself, never through Web Audio; our own engine's from its player's stream.
  useEffect(() => {
    if (audio.current) audio.current.srcObject = voice
  }, [voice])

  const speaking = textMode ? level !== null : voicePlaying || lines.some((l) => l.side === 'agent' && !l.final)
  const booting = speechMode && boot.length > 0
  const state: ScopeState = phase === 'connecting' || booting ? 'connecting' : live ? (speaking ? 'speaking' : 'listening') : 'idle'

  return (
    <div className="vt has-stage" data-state={state}>
      <div className="vt-sky" aria-hidden="true" />

      <div className="vt-hud">
        <p className="vt-status" role="status">
          <ToneMark tone={live ? 'live' : 'unknown'} />
          {phase === 'idle' && 'Not connected'}
          {phase === 'connecting' && 'Connecting to Voice it…'}
          {phase === 'live' && !speechMode && (mic.muted ? 'Live, muted' : 'Live')}
          {speechMode && boot.length > 0 && <BootPhases boot={boot} />}
          {speechMode && boot.length === 0 && (speechState ?? (mic.muted ? 'muted' : 'listening'))}
          {textMode && ', on the ChatGPT plan'}
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
          {/* The advice sits behind an (i), not on the surface (the owner's
              standing rule, 2026-11-05: no explanatory prose on the surface). */}
          <TalkTip
            tip={
              textMode
                ? "A text conversation on the ChatGPT plan: this browser hears you and reads Voice it's replies aloud. Headphones are advised, so Voice it does not hear itself."
                : "Headphones are advised: the browser's echo cancellation is what lets you talk over Voice it."
            }
          >
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About headphones">
              <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <circle cx="12" cy="12" r="8.5" />
                <path d="M12 11v5.5M12 7.75v.01" />
              </svg>
            </button>
          </TalkTip>
        </span>
        {textMode && fallback !== null && (
          <p className="vt-note" role="status">
            {fallbackLine(fallback)}{' '}
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Dismiss" onClick={() => setFallback(null)}>
              <CloseGlyph />
            </button>
          </p>
        )}
        {textMode && typed !== null && <p className="vt-note">{typed}</p>}
        {problem !== null && (
          <p className="vt-problem" role="alert">
            {problem}
          </p>
        )}
      </div>

      {USE_FIXTURES && sessionId === null ? (
        <TalkStage projectId="vp_fixture" sessionId="vs_fixture" live />
      ) : (
        <TalkStage projectId={projectId} sessionId={live ? sessionId : null} live={live} feed={feed} />
      )}

      <div className="vt-presence" aria-label="Voice it">
        <div className="vt-core">
          <Ring state={state} voice={voice} mic={live ? micStream : null} level={textMode ? level : null} />
        </div>
        <div className="vt-subs-band">
          <Captions lines={lines} />
          {textMode && (
            <form
              className="c-field"
              onSubmit={(e) => {
                e.preventDefault()
                say(draft)
                setDraft('')
              }}
            >
              <label className="c-field">
                <span className="c-lbl">Say to Voice it</span>
                <input className="c-inp" value={draft} maxLength={TURN_MAX} onChange={(e) => setDraft(e.target.value)} />
              </label>
              <span className="c-acts">
                <Button kind="primary" size="sm" type="submit" disabled={draft.trim() === ''}>
                  Send
                </Button>
              </span>
            </form>
          )}
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
            {textMode && level !== null && (
              <Button kind="secondary" size="sm" onClick={() => speaker.current?.cancel()}>
                Stop speaking
              </Button>
            )}
            {live && <span className="vt-mic-state">{micWord(mic)}</span>}
          </div>
        </div>
      </div>

      <section className="vt-cards" aria-label="Cards">
        <p className="vt-cards-h">Cards</p>
        <CardList sessionId={call.current?.sessionId ?? null} onStageEvent={feed.emit} />
      </section>

      {sessionId !== null && <BudgetMeter sessionId={sessionId} live={live} />}

      <audio ref={audio} autoPlay className="vt-voice" />
    </div>
  )
}
