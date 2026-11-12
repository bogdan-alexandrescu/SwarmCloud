/**
 * VOICE IT'S SESSION HOLDER (docs/design/voice-it.md §3.12.2, "The session's
 * lifetime once it lives above the page").
 *
 * The call (the WebRTC peer or the speech socket, the mic, the player, voice
 * detection), the session id, the project id and the stream subscription live
 * here, in one holder the console's shell mounts above the routed screen for
 * the whole life of the tab. Talk.tsx is a view of it: it draws what the
 * holder holds and starts or ends nothing by mounting or unmounting, so
 * changing section never touches the call.
 *
 * WHAT ENDS A SESSION. End (Talk's button, or the spoken "stop the session");
 * swarm-api's own rules (the budget cap, silence, a long pause), which the
 * call hears as its end; closing the tab, where `pagehide` sends the end with
 * `fetch(..., {keepalive: true})`, carrying the session cookie, the
 * workspace header a beacon cannot send and the cause `tab_closed`; and signing out, which ends the
 * session before the page leaves for IAP's clear URL (signoutState.ts
 * `leave`). A reload is a closed tab: a fresh holder starts nothing by itself,
 * and Projects' Resume continues the project.
 *
 * WHAT DOES NOT END IT. Changing section; opening Projects, Preferences or
 * Settings; the console's idle stop (speech still counts as input through
 * `noteActivity`); a pause; a reconnect of the stream. The stream is read
 * here by session id for as long as the call is held, so a card, a stage
 * event, a trace turn or a tier reopen that arrives off Talk is still applied.
 *
 * WHAT CARRIES ON UNCHANGED. One live session per person (swarm-api's
 * `voice_live`): the holder holds one call at a time. The grant dies with the
 * session, not with a pause or a page. A reconnect of the stream keeps the
 * session: every reader on the shared stream moves to the reopened one, and an
 * event already applied is dropped by its sequence.
 *
 * THE KEYBOARD. Space and M (push-to-talk, mute) answer only while Talk is on
 * screen (`attach`), so the console's own shortcuts and text fields are never
 * captured on another page.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import {
  normVoiceSettings,
  normVoiceTraceTurnEvent,
  USE_FIXTURES,
  VOICE_SETTINGS,
  VOICE_TIERS,
  type VoiceTier,
  type VoiceTraceTurnEvent,
} from '../api'
import { apiHeaders, chosenTenant, read, route, write } from '../fetch'
import { noteActivity } from '../Shell'
import { leave } from '../signoutState'
import { applyStreamEvent, EMPTY_LIST, readSessionStream, STAGE_EVENTS, STREAM_EVENTS, TIER_EVENTS, TRACE_EVENTS, type CardListState, type StreamEvent } from './CardList'
import {
  applyCaption,
  captionEnd,
  captionEvent,
  closeCaption,
  trimCaptions,
  CAPTION_PAUSE_MS,
  closedLine,
  type CaptionLine,
  type CaptionSide,
} from './captions'
import { micEnabled, useMic, type MicMode, type MicState } from './mic'
import { takeResumedProject } from './Projects'
import { connectTalk, endVoiceSession, reopenTalk, sendTurn, TalkError, TURN_MAX, TurnError, VOICE_SESSION_END, type TalkCall, type TalkMode } from './rtc'
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
import { stageFeed, type StageFeed } from './Stage'
import { startVoiceDetection, type VoiceDetection } from './vad'

export type Phase = 'idle' | 'connecting' | 'live' | 'ending'

/** Each tier by the name the session's header gives it (llm-providers.md §5.4). */
export function tierName(tier: VoiceTier): string {
  if (tier === 'plan') return 'Plan'
  if (tier === 'voice_dedicated') return 'Voice it key'
  return 'Shared key'
}

/** The one caption a tier change draws. */
export function tierCaption(tier: VoiceTier): string {
  return `Moved to ${tierName(tier)}`
}

/** A `voice.tier.reopen` event's tier, or null when it names none Talk knows. */
export function reopenTier(ev: StreamEvent): VoiceTier | null {
  const tier = ev.data.tier
  return typeof tier === 'string' && (VOICE_TIERS as readonly string[]).includes(tier) ? (tier as VoiceTier) : null
}

/**
 * How long a dropped peer waits for `voice.tier.reopen` before the talk ends:
 * swarm-voice's lease of the next tier (the broker's 10 s timeout) and the
 * stream's one-second poll, with room to spare.
 */
export const REOPEN_WAIT_MS = 15_000

/** The call the holder holds: GPT-Live's or a text session (rtc.ts), or our own engine's (speechSocket.ts). */
type AnyCall = TalkCall | SpeechCall
export type PaneMode = TalkMode | 'speech'

/** Stop every side's caption pause timer. */
function clearPauses(timers: { current: Partial<Record<CaptionSide, ReturnType<typeof setTimeout>>> }): void {
  for (const t of Object.values(timers.current)) clearTimeout(t)
  timers.current = {}
}

/**
 * `after`, having noted activity when it closed a line: a segment closed with
 * no final (a pause, the other side, the end of a turn) is speech as much as a
 * final is. Calling it twice, as React may an updater, notes the same moment.
 */
function noted(before: readonly CaptionLine[], after: CaptionLine[]): CaptionLine[] {
  if (closedLine(before, after)) noteActivity()
  return after
}

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

/**
 * Whether a text session runs on Voice it's ChatGPT plan: one that started as
 * text, or whose fallback started swarm-voice's text session because no front
 * held anything (`front_refused`, `speech_not_deployed`). A no-L4, not-ready or
 * engine-down fallback stays on the Claude front with the account it holds.
 */
export function onChatGptPlan(f: SpeechFallback | null): boolean {
  return f === null || f.reason === 'front_refused' || f.reason === 'speech_not_deployed'
}

/**
 * A start refusal's one line, by its cause (`detail.reason`); swarm-api's full
 * sentence goes behind the (i). The OpenAI key is named only when a Live
 * start found it held by another session.
 */
export function refusalLine(reason: string): string | null {
  if (reason === 'openai_key_in_use') return 'OpenAI key in use by another session.'
  if (reason === 'no_voice_account_available') return 'No Voice it plan account free.'
  return null
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
function startProblem(e: unknown): { line: string; tip: string | null } {
  if (e instanceof TalkError && e.api !== null && e.api.message.trim() !== '') {
    const detail = e.api.detail as { reason?: unknown } | undefined
    const line = typeof detail?.reason === 'string' ? refusalLine(detail.reason) : null
    return line === null ? { line: e.api.message, tip: null } : { line, tip: e.api.message }
  }
  return { line: `Voice it could not start: ${e instanceof Error ? e.message : 'the call failed.'}`, tip: null }
}

/**
 * The end a closing tab sends: the session's end route, kept alive past the
 * page, with the session cookie and the workspace the session started in.
 * A `sendBeacon` cannot carry `X-Swarm-Tenant`, so it is a fetch. It names
 * its cause, `tab_closed`; End names none, which swarm-api records as
 * `ended_by_person`.
 */
/** The session cookie goes with it: this origin's, as every request of the console's (fetch.ts `READ_INIT`). */
const LEAVE_INIT = { sameOrigin: 'same-origin' } as const satisfies Record<string, RequestCredentials>

function endOnLeave(sessionId: string, tenant: string | null): void {
  try {
    void fetch(route(VOICE_SESSION_END, { id: sessionId }).url, {
      method: 'POST',
      keepalive: true,
      credentials: LEAVE_INIT.sameOrigin,
      headers: apiHeaders({ accept: 'application/json', 'content-type': 'application/json' }, tenant),
      body: JSON.stringify({ cause: 'tab_closed' }),
    }).catch(() => undefined)
  } catch {
    /* the page is going; swarm-api's own rules end what this could not */
  }
}

/** What the holder hands its views: the session's state and its controls. */
export interface VoiceSession {
  phase: Phase
  problem: string | null
  /** A start refusal's full sentence, behind the problem line's (i). */
  problemTip: string | null
  lines: CaptionLine[]
  /** Settings' debug mode, read with the engine; and the session's last traced turn while it is on. */
  debug: boolean
  turn: VoiceTraceTurnEvent | null
  voice: MediaStream | null
  micStream: MediaStream | null
  voicePlaying: boolean
  /** The last session's id: its meter stays on the pane after it ends. */
  sessionId: string | null
  /** The held call's session id, null once the call is closed: the stream is read while it is set. */
  streamId: string | null
  /** The project the stage draws visuals from: the Resume's, the one a start continues. */
  projectId: string | null
  talkMode: PaneMode
  /** Our own engine: the machine's boot phases seen so far, then the session's state word. */
  boot: BootPhase[]
  speechState: string | null
  /** Why this session is in text mode instead of on its engine; null once dismissed. */
  fallback: SpeechFallback | null
  dismissFallback: () => void
  /** The one caption of a tier change mid-session (§5.4). */
  tierLine: string | null
  onPlan: boolean
  /** A text session's spoken reply, 0..1, or null when nothing is spoken. */
  level: number | null
  /** Why a text session types instead of listening; null while it listens. */
  typed: string | null
  draft: string
  setDraft: (text: string) => void
  /** The session's cards, as its stream drew them; kept after the end until the next start. */
  cards: CardListState
  /** The last `meter.update` the stream carried for the session, as served; the pill reads the ledger itself. */
  meter: Record<string, unknown> | null
  /** The stage's visual, note and focus events, as the stream carries them. */
  feed: StageFeed
  mic: MicState
  setMuted: (muted: boolean) => void
  setMode: (mode: MicMode) => void
  hold: (held: boolean) => void
  start: () => Promise<void>
  end: () => Promise<void>
  /** One turn of a text session. */
  say: (said: string) => void
  /** Stop speaking: the reply being read aloud is cut off. */
  quiet: () => void
  /** A view on screen: reads Settings' engine and debug, and lets the mic's keys answer. Returns its leave. */
  attach: () => () => void
  /** The console's navigation, for the navigation offer (§3.12.3). */
  navigate: (to: string) => void
}

const VoiceSessionContext = createContext<VoiceSession | null>(null)

/** The holder above the page; Talk and any other view read it. */
export function useVoiceSession(): VoiceSession {
  const held = useContext(VoiceSessionContext)
  if (held === null) throw new Error('Voice it: a view of the session is drawn outside its holder (VoiceSessionHolder)')
  return held
}

/** The console's navigation when the shell hands none: a path pushed as the console's own links push one. */
function pushPath(to: string): void {
  window.history.pushState(null, '', to)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function VoiceSessionHolder({ children, navigate = pushPath }: { children?: ReactNode; navigate?: (to: string) => void }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [problem, setProblem] = useState<string | null>(null)
  const [problemTip, setProblemTip] = useState<string | null>(null)
  const [lines, setLines] = useState<CaptionLine[]>([])
  const [debug, setDebug] = useState(false)
  const [turn, setTurn] = useState<VoiceTraceTurnEvent | null>(null)
  const [voice, setVoice] = useState<MediaStream | null>(null)
  const [micStream, setMicStream] = useState<MediaStream | null>(null)
  const [track, setTrack] = useState<MediaStreamTrack | null>(null)
  const [voicePlaying, setVoicePlaying] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [streamId, setStreamId] = useState<string | null>(null)
  const [projectId, setProjectId] = useState<string | null>(null)
  const [talkMode, setTalkMode] = useState<PaneMode>('live')
  const [boot, setBoot] = useState<BootPhase[]>([])
  const [speechState, setSpeechState] = useState<string | null>(null)
  const [fallback, setFallback] = useState<SpeechFallback | null>(null)
  const [tierLine, setTierLine] = useState<string | null>(null)
  // Kept apart from `fallback`, which Dismiss clears: the status line must not change with it.
  const [onPlan, setOnPlan] = useState(true)
  const [level, setLevel] = useState<number | null>(null)
  const [typed, setTyped] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [cards, setCards] = useState<CardListState>(EMPTY_LIST)
  const [meter, setMeter] = useState<Record<string, unknown> | null>(null)
  // How many views are on screen: the mic's keys answer only while one is.
  const [viewing, setViewing] = useState(0)
  const call = useRef<AnyCall | null>(null)
  // The workspace the held session started in: a closing tab's end names it, whatever the switcher says by then.
  const tenant = useRef<string | null>(null)
  const socket = useRef<SpeechSocket | null>(null)
  const player = useRef<SpeechPlayer | null>(null)
  const detection = useRef<VoiceDetection | null>(null)
  // Settings' engine, read when a view opens; null until read, or when it could not be.
  const engine = useRef<string | null>(null)
  const listener = useRef<Listener | null>(null)
  const speaker = useRef<Speaker | null>(null)
  // Turns go to swarm-api one after another, in the order they were said.
  const turns = useRef<Promise<void>>(Promise.resolve())
  const turnSeq = useRef(0)
  const mounted = useRef(true)
  // Each side's open caption line closes after CAPTION_PAUSE_MS with no new delta.
  const captionPause = useRef<Partial<Record<CaptionSide, ReturnType<typeof setTimeout>>>>({})
  const audio = useRef<HTMLAudioElement | null>(null)
  const feed = useMemo(stageFeed, [])
  // The Resume's project: undefined until the first start takes it, then the
  // id until a start succeeds, then null.
  const resumed = useRef<string | null | undefined>(undefined)
  const live = phase === 'live'
  const textMode = live && talkMode === 'text'
  const { mic, setMuted, setMode, hold } = useMic(track, live && viewing > 0)
  const micRef = useRef<MicState>(mic)
  micRef.current = mic

  /** Settings' engine and debug mode, from the route itself: the fixture console has no machine to start, so it makes today's call. */
  const settingsRead = useRef(0)
  const readSettings = useCallback(() => {
    if (USE_FIXTURES) return
    const n = ++settingsRead.current
    void read<unknown>(route(VOICE_SETTINGS), () => false).then((r) => {
      if (!mounted.current || n !== settingsRead.current || (r.status !== 'ok' && r.status !== 'stale')) return
      const view = normVoiceSettings(r.data)
      engine.current = view.settings.engine || null
      setDebug(view.debug)
    })
  }, [])

  const attach = useCallback(() => {
    setViewing((n) => n + 1)
    readSettings()
    return () => {
      // A view that leaves before the read answers takes nothing from it.
      settingsRead.current += 1
      setViewing((n) => n - 1)
    }
  }, [readSettings])

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
    const ended = captionEnd(data)
    if (ended !== null) setLines((l) => noted(l, closeCaption(l, ended)))
    const ev = captionEvent(data)
    if (ev === null) return
    // Speech is proof someone is there; a word still being spoken is not yet a segment.
    if (ev.final) noteActivity()
    setLines((l) => trimCaptions(noted(l, applyCaption(l, ev))))
    const side = ev.side
    clearTimeout(captionPause.current[side])
    if (!ev.final) {
      captionPause.current[side] = setTimeout(() => {
        if (mounted.current) setLines((l) => trimCaptions(noted(l, closeCaption(l, side))))
      }, CAPTION_PAUSE_MS)
    }
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
    setStreamId(null)
    // A segment cut off mid-word stays as it was heard, not as a growing line.
    clearPauses(captionPause)
    setLines((l) => l.filter((x) => x.final))
    return c
  }, [closeSpeech])

  // A drop waits for a reopen; a reopen in flight is not a drop.
  const dropTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reopening = useRef(false)
  const clearDrop = useCallback(() => {
    if (dropTimer.current !== null) clearTimeout(dropTimer.current)
    dropTimer.current = null
  }, [])

  const end = useCallback(async () => {
    clearDrop()
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
  }, [hangUp, clearDrop])

  const dropped = useCallback(() => {
    if (reopening.current || dropTimer.current !== null) return
    dropTimer.current = setTimeout(() => {
      dropTimer.current = null
      void end()
    }, REOPEN_WAIT_MS)
  }, [end])

  /** The reopen: a fresh offer to the same session, on the tier swarm-api named. */
  const reopen = useCallback(
    async (tier: VoiceTier) => {
      const c = call.current
      if (c === null || reopening.current || c.mode !== 'live' || !('dropPeer' in c)) return
      reopening.current = true
      clearDrop()
      setVoicePlaying(false)
      try {
        const next = await reopenTalk({ onEvent, onVoice: (s) => setVoice(s), onDropped: dropped }, c)
        if (!mounted.current || call.current !== c) {
          // Ended while the offer was out: the new peer goes too.
          next.hangUp()
          if (!mounted.current) void endVoiceSession(next.sessionId)
          return
        }
        call.current = next
        setTierLine(tierCaption(tier))
      } catch (e) {
        if (!mounted.current || call.current !== c) return
        // A reopen that fails ends the talk with its reason, as a spent key does.
        await end()
        if (!mounted.current) return
        const said = startProblem(e)
        setProblem(said.line)
        setProblemTip(said.tip)
      } finally {
        reopening.current = false
      }
    },
    [onEvent, dropped, clearDrop, end],
  )
  const reopenRef = useRef(reopen)
  reopenRef.current = reopen

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

  const quiet = useCallback(() => speaker.current?.cancel(), [])

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
      setOnPlan(onChatGptPlan(text.fallback))
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
    // One live session per person: a held call is ended by End, never replaced.
    if (call.current !== null) return
    setProblem(null)
    setProblemTip(null)
    setLines([])
    setTyped(null)
    setDraft('')
    setFallback(null)
    setTierLine(null)
    setPhase('connecting')
    if (resumed.current === undefined) resumed.current = takeResumedProject()
    setProjectId(resumed.current)
    try {
      // Only an engine read as GPT-Live sends the offer; `own` or not read yet starts with none.
      const own = !USE_FIXTURES && engine.current !== GPT_LIVE ? await connectOwnSpeech(resumed.current) : GPT_LIVE
      const c: AnyCall =
        own !== GPT_LIVE
          ? own
          : await connectTalk(
              {
                onEvent,
                onVoice: (s) => setVoice(s),
                onDropped: dropped,
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
      tenant.current = chosenTenant()
      // A new session's project is the one swarm-api opened: the stage reads its visuals.
      setProjectId(c.projectId)
      setTalkMode(c.mode)
      if (c.mode === 'text') {
        const f = 'fallback' in c ? c.fallback : null
        setFallback(f)
        setOnPlan(onChatGptPlan(f))
        beginText()
      } else if (c.mode === 'speech') beginSpeech(c)
      setSessionId(c.sessionId)
      setStreamId(c.sessionId)
      setTurn(null)
      setMicStream(c.micStream)
      setTrack(c.mic)
      setPhase('live')
    } catch (e) {
      if (!mounted.current) return
      const said = startProblem(e)
      setProblem(said.line)
      setProblemTip(said.tip)
      setPhase('idle')
    }
  }, [onEvent, dropped, beginText, beginSpeech])

  // THE STREAM, for as long as the call is held, whichever screen is mounted.
  // A new session starts an empty list; an ended one keeps its cards. An event
  // at or below the last sequence applied (a stream reopened from further
  // back) is dropped, so nothing reaches the stage, the turn line or the
  // reopen twice.
  const shownFor = useRef<string | null>(null)
  const lastSeq = useRef(0)
  useEffect(() => {
    if (streamId === null) return
    if (shownFor.current !== streamId) {
      shownFor.current = streamId
      lastSeq.current = 0
      setCards(EMPTY_LIST)
      setMeter(null)
    }
    if (typeof EventSource === 'undefined') return
    // Read here, not at load: CardList.tsx's import graph comes back to this module.
    const isStage: readonly string[] = STAGE_EVENTS
    const isTrace: readonly string[] = TRACE_EVENTS
    const isTier: readonly string[] = TIER_EVENTS
    return readSessionStream(streamId, lastSeq.current, {
      // Every event the stream carries: the card list's, the stage's, the trace's and the tier's.
      types: [...STREAM_EVENTS, ...isStage, ...isTrace, ...isTier],
      onEvent: (ev) => {
        if (ev.seq <= lastSeq.current) return
        lastSeq.current = ev.seq
        if (isStage.includes(ev.type)) feed.emit(ev)
        if (isTrace.includes(ev.type)) {
          const t = normVoiceTraceTurnEvent({ ...ev.data, seq: ev.seq })
          if (t !== null) setTurn(t)
        }
        if (isTier.includes(ev.type)) {
          const tier = reopenTier(ev)
          if (tier !== null) void reopenRef.current(tier)
        }
        if (ev.type === 'meter.update') setMeter(ev.data)
        setCards((s) => applyStreamEvent(s, ev))
      },
    })
  }, [streamId, feed])

  // CLOSING THE TAB ends the session: a call nobody can hear is still billed per second.
  useEffect(() => {
    const gone = () => {
      const c = call.current
      if (c === null) return
      clearDrop()
      hangUp()
      setPhase('idle')
      endOnLeave(c.sessionId, tenant.current)
    }
    window.addEventListener('pagehide', gone)
    return () => window.removeEventListener('pagehide', gone)
  }, [hangUp, clearDrop])

  // SIGNING OUT ends the session first, while the session cookie still holds:
  // the page then leaves for IAP's clear URL (signoutState.ts `leave`).
  useEffect(() => {
    const go = leave.to
    leave.to = (url: string) => {
      const c = call.current
      if (c !== null) {
        clearDrop()
        hangUp()
        endOnLeave(c.sessionId, tenant.current)
      }
      go.call(leave, url)
    }
    return () => {
      leave.to = go
    }
  }, [hangUp, clearDrop])

  // The holder lives for the tab; only its own unmount (the console's) hangs up and ends.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      clearPauses(captionPause)
      if (dropTimer.current !== null) clearTimeout(dropTimer.current)
      dropTimer.current = null
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

  const dismissFallback = useCallback(() => setFallback(null), [])

  const held: VoiceSession = {
    phase,
    problem,
    problemTip,
    lines,
    debug,
    turn,
    voice,
    micStream,
    voicePlaying,
    sessionId,
    streamId,
    projectId,
    talkMode,
    boot,
    speechState,
    fallback,
    dismissFallback,
    tierLine,
    onPlan,
    level,
    typed,
    draft,
    setDraft,
    cards,
    meter,
    feed,
    mic,
    setMuted,
    setMode,
    hold,
    start,
    end,
    say,
    quiet,
    attach,
    navigate,
  }

  return (
    <VoiceSessionContext.Provider value={held}>
      {children}
      {/* The agent's voice, above the page, so it keeps playing on any screen. */}
      <audio ref={audio} autoPlay className="vt-voice" />
    </VoiceSessionContext.Provider>
  )
}
