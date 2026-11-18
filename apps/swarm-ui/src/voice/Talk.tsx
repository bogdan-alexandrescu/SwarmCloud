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
 * WHAT THE PAGE HOLDS. Nothing of the call: the WebRTC peer or the speech
 * socket, the mic, the player, voice detection, the session and project ids
 * and the stream live in the session holder above the page (session.tsx,
 * §3.12.2), so leaving Talk keeps the call and coming back draws the same
 * session. Talk draws the captions and the ring (§3.2) from the holder. No
 * key and no client secret is held anywhere here: the session is created by
 * swarm-voice behind swarm-api (rtc.ts).
 *
 * SPEECH IS INPUT. Every final caption, from either side, while a session is
 * live calls Shell.tsx's `noteActivity`, and so does every caption line closed
 * without a final (a pause, the other side, the end of a turn), so the console's fifteen-minute idle
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
 * makes today's call. An engine not read yet (still in flight, or the read
 * failed) starts the same way, so an `own` deployment never gets an offer it
 * would answer with a machine; only Settings' `gpt-live` sends one. While
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
 * DEBUG MODE (the owner's pick of 10 November 2026, "Trace on Telemetry").
 * While Settings' Debug is on (read with the engine, when the page opens),
 * each planner turn's `trace.turn` event on the session's stream (the card
 * list hands it up, `onTraceEvent`) draws one line under the Stage: the
 * turn's number, the cards drawn and refused, and why nothing was drawn; what
 * was drawn and each refusal's reason are behind its tooltip, and Trace opens
 * the turn on Telemetry. Off, no line, whatever the stream carries.
 *
 * BY VOICE. Every control also answers a whole spoken phrase (`spokenAction`):
 * "stop the session" ends it, "mute", "push to talk", "open mic" and "stop
 * speaking" do what their buttons do. A read-back's Go and Not that are heard
 * by swarm-api on our own engine (the relay's read-back matcher); in text
 * mode the page posts a phrase that can answer one to the read-back answer
 * route first, and sends it as a turn only when it answered nothing.
 *
 * A SPENT LIVE KEY REOPENS (docs/design/llm-providers.md §5.3, §5.4). When
 * the key's credit is gone, swarm-api streams `voice.tier.reopen` with the
 * next tier's name; the page makes a fresh offer to the same session at once
 * (rtc.ts `reopenTalk`), keeps the captions and the cards, and shows one
 * caption naming the tier, never a modal. A reopen that fails ends the talk
 * with its reason. A peer that drops waits REOPEN_WAIT_MS for that event
 * before it ends the talk: the spent Live session can close before the event
 * reaches the page.
 */

import { useEffect, useRef } from 'react'

import { USE_FIXTURES, type VoiceTraceTurnEvent } from '../api'
import { Button, buttonClass, Segmented, ToneMark } from '../components'
import { CardList } from './CardList'
import { type CaptionLine } from './captions'
import { CostPill } from './CostPill'
import { micWord, type MicMode } from './mic'
import { TURN_MAX } from './rtc'
import { Ring, type ScopeState } from './Scope'
import { useVoiceSession } from './session'
import { WorkingCard } from './WorkingCard'
import { type BootPhase, type SpeechFallback } from './speechSocket'
import { TalkStage, TalkTip } from './Stage'
import '../styles/talk-pane.css'

// The session's rules and words live with the holder (session.tsx); they are named here too, where Talk always named them.
export {
  mayAnswerReadback,
  normalPhrase,
  onChatGptPlan,
  refusalLine,
  REOPEN_WAIT_MS,
  reopenTier,
  spokenAction,
  tierCaption,
  tierName,
  type SpokenAction,
} from './session'

/** Why a turn drew nothing, by the server's code (swarm_api/voice/trace.py `NONE_CODES`), in the line's words. */
const NONE_WORDS: Readonly<Record<string, string>> = {
  empty_transcript: 'empty transcript',
  refused: 'all refused',
  planner_error: 'planner error',
  timeout: 'timed out',
  no_grant: 'no grant',
  answered_in_words: 'answered in words',
}

const plural = (n: number, one: string): string => `${n} ${one}${n === 1 ? '' : 's'}`
const named = (d: { kind: string; title: string | null }): string => (d.title ? `${d.kind} “${d.title}”` : d.kind)

/** The turn in full on Telemetry, out of the router as the console's other navigations go. */
function openTrace(sessionId: string, n: number): void {
  window.history.pushState(null, '', `/voice/telemetry?${new URLSearchParams({ s: sessionId, open: String(n) })}`)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** Debug mode's one line under the Stage: the last planner turn's outcome, what it drew and refused in its tooltip. */
function TurnLine({ turn, sessionId }: { turn: VoiceTraceTurnEvent; sessionId: string | null }) {
  const cards = turn.drawn.length
  const out = [
    plural(cards, 'card'),
    ...(turn.refused.length > 0 ? [`${turn.refused.length} refused`] : []),
    ...(cards === 0 && turn.none !== null ? [NONE_WORDS[turn.none] ?? turn.none] : []),
  ].join(' · ')
  const tip = [
    ...(cards > 0 ? [`Drawn: ${turn.drawn.map(named).join(', ')}`] : []),
    ...turn.refused.map((r) => `Refused: ${named(r)}, ${r.reason ?? 'no reason given'}`),
  ].join('. ')
  return (
    <div className="vt-turn" role="group" aria-label="Last planner turn" data-outcome={cards > 0 ? 'drawn' : 'none'}>
      <TalkTip tip={tip || `Turn ${turn.n}: nothing drawn`}>
        <span className="vt-turn-main" tabIndex={0}>
          <span className={`vt-turn-dot ${cards > 0 ? 'is-ok' : 'is-warn'}`} aria-hidden="true" />
          <span className="vt-turn-n">Turn {turn.n}</span>
          <span className="vt-turn-out">{out}</span>
        </span>
      </TalkTip>
      {sessionId !== null && (
        <Button kind="ghost" size="sm" onClick={() => openTrace(sessionId, turn.n)}>
          Trace
        </Button>
      )}
    </div>
  )
}

const WHO: Record<CaptionLine['side'], string> = { you: 'You', agent: 'Voice it' }

/** The one line a fallback draws (§4.7); its details are `fallbackTip`'s. */
export function fallbackLine(f: SpeechFallback): string {
  if (f.reason === 'no_l4') return f.zone === null ? 'No L4 free: text mode.' : `No L4 free in ${f.zone}: text mode.`
  if (f.reason === 'not_ready') return 'Speech machine not ready: text mode.'
  if (f.reason === 'engine_down') return 'Engine down: text mode.'
  if (f.reason === 'front_refused') return 'No Claude account free: text mode.'
  return 'Own speech unavailable: text mode.'
}

/** What the fallback's line holds behind its (i): why, what carries the talk, and when to retry. */
export function fallbackTip(f: SpeechFallback): string {
  if (f.reason === 'no_l4' || f.reason === 'not_ready') {
    const why = f.reason === 'no_l4' ? 'No speech machine (L4) was free.' : 'The speech machine did not start in time.'
    return `${why} This talk continues as text with Voice it's Claude front, on the account it already holds. Start again in a few minutes to try own speech.`
  }
  if (f.reason === 'engine_down') return "The speech engine went down. This talk continues as text with Voice it's Claude front."
  if (f.reason === 'front_refused') return "No Claude account was free for the voice front. This talk continues as text on Voice it's plan."
  return 'Own speech could not start. This talk continues as text.'
}

function InfoGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5M12 7.75v.01" />
    </svg>
  )
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
    <span className="vt-boot" role="list" aria-label="Starting">
      {done.map((p) => (
        <span key={p} className="vt-boot-step is-done" role="listitem" aria-label={p} title={p}>
          <ToneMark tone="ok" />
        </span>
      ))}
      {now !== undefined && (
        <span className="vt-boot-step is-now" role="listitem" aria-current="step">
          {now}
        </span>
      )}
    </span>
  )
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

/**
 * The Talk pane: a view of the session holder (session.tsx). It draws the
 * call the holder holds and starts or ends nothing by mounting or unmounting:
 * leaving Talk keeps the call, and coming back draws the same session.
 */
export function TalkPane() {
  const held = useVoiceSession()
  const { phase, problem, problemTip, lines, debug, turn, voice, micStream, voicePlaying, sessionId, streamId, projectId, talkMode, boot, speechState, fallback, tierLine, onPlan, level, typed, draft, cards, feed, mic } =
    held
  const { attach, setMuted, setMode, hold, setDraft, say, start, end } = held
  const live = phase === 'live'
  const textMode = live && talkMode === 'text'
  const speechMode = live && talkMode === 'speech'
  // On screen, the pane reads Settings' engine and lets the mic's keys answer.
  useEffect(() => attach(), [attach])

  const speaking = textMode ? level !== null : voicePlaying || lines.some((l) => l.side === 'agent' && !l.final)
  const booting = speechMode && boot.length > 0
  const state: ScopeState = phase === 'connecting' || booting ? 'connecting' : live ? (speaking ? 'speaking' : 'listening') : 'idle'
  // The held session's cards; before any session the list draws its own empty (or, in fixture mode, fixture) cards.
  const heldCards = streamId !== null || cards.cards.length > 0 ? cards.cards : undefined

  return (
    <div className={`vt has-stage${debug && turn !== null ? ' has-turn' : ''}`} data-state={state}>
      <div className="vt-sky" aria-hidden="true" />

      <div className="vt-hud">
        <p className="vt-status" role="status">
          <ToneMark tone={live ? 'live' : 'unknown'} />
          {phase === 'idle' && 'Not connected'}
          {phase === 'connecting' && 'Connecting to Voice it…'}
          {phase === 'live' && !speechMode && (mic.muted ? 'Live, muted' : 'Live')}
          {speechMode && boot.length > 0 && <BootPhases boot={boot} />}
          {speechMode && boot.length === 0 && (speechState ?? (mic.muted ? 'muted' : 'listening'))}
          {textMode && onPlan && ', on the ChatGPT plan'}
          {phase === 'ending' && 'Ending the session…'}
        </p>
        <span className="vt-hud-actions">
          {/* The session's cost, beside End: the ledger's figures, every line behind the pill. */}
          {sessionId !== null && <CostPill sessionId={sessionId} live={live} />}
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
                ? "A text conversation: this browser hears you and reads Voice it's replies aloud. Headphones are advised, so Voice it does not hear itself."
                : "Headphones are advised: the browser's echo cancellation is what lets you talk over Voice it."
            }
          >
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About headphones">
              <InfoGlyph />
            </button>
          </TalkTip>
        </span>
        {textMode && fallback !== null && (
          <p className="vt-fallback" role="status">
            <span>{fallbackLine(fallback)}</span>{' '}
            <TalkTip tip={fallbackTip(fallback)}>
              <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Why text mode">
                <InfoGlyph />
              </button>
            </TalkTip>
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Dismiss" onClick={held.dismissFallback}>
              <CloseGlyph />
            </button>
          </p>
        )}
        {live && tierLine !== null && (
          <p className="vt-note" role="status">
            {tierLine}
          </p>
        )}
        {textMode && typed !== null && <p className="vt-note">{typed}</p>}
        {problem !== null && (
          <p className="vt-problem" role="alert">
            <span>{problem}</span>
            {problemTip !== null && (
              <>
                {' '}
                <TalkTip tip={problemTip}>
                  <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Why it did not start">
                    <InfoGlyph />
                  </button>
                </TalkTip>
              </>
            )}
          </p>
        )}
      </div>

      {USE_FIXTURES && sessionId === null ? (
        <TalkStage projectId="vp_fixture" sessionId="vs_fixture" live />
      ) : (
        <TalkStage projectId={projectId} sessionId={live ? sessionId : null} live={live} feed={feed} />
      )}
      {debug && turn !== null && <TurnLine turn={turn} sessionId={sessionId} />}

      <div className="vt-presence" aria-label="Voice it">
        <div className="vt-core">
          <Ring state={state} voice={voice} mic={live ? micStream : null} level={textMode ? level : null} />
        </div>
        <div className="vt-subs-band">
          <Captions lines={lines} />
          <WorkingCard work={cards.work ?? []} />
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
              <Button kind="secondary" size="sm" onClick={held.quiet}>
                Stop speaking
              </Button>
            )}
            {live && <span className="vt-mic-state">{micWord(mic)}</span>}
          </div>
        </div>
      </div>

      <section className="vt-cards" aria-label="Cards">
        <p className="vt-cards-h">Cards</p>
        <CardList sessionId={streamId} fixtures={heldCards} projectId={projectId} navigate={held.navigate} />
      </section>
    </div>
  )
}
