/**
 * VOICE IT'S TEXT CONVERSATION: the browser's own speech, in and out, for a
 * session on the ChatGPT plan (the owner's choice of 4 November 2026: GPT-Live
 * when an OpenAI API key is in the Voice it pool, else a text conversation).
 *
 * In text mode there is no WebRTC and no audio leaves for OpenAI. The browser
 * hears the person (`SpeechRecognition`, or Chrome's and Safari's
 * `webkitSpeechRecognition`), each final phrase goes to swarm-api as one turn
 * (rtc.ts `sendTurn`), and the reply is read aloud by `speechSynthesis`.
 *
 * LISTENING. Continuous, with interim results: the phrase still being heard
 * is the live caption, and each final phrase is one turn. A recogniser ends
 * by itself after a silence; while the pane still wants to listen it is
 * started again. `stop()` (push-to-talk let go) still delivers the phrase
 * being heard; `abort()` (mute, End) discards it, so a muted word never
 * becomes a turn. A refusal of the microphone or of the recognition service
 * stops listening for good and says why: the pane then offers a text box.
 *
 * SPEAKING. One voice for the whole session, picked once. Speaking again
 * cancels what is playing, and so does `cancel()` (the Stop speaking button,
 * a new phrase heard, End). `speechSynthesis` gives the page no audio stream,
 * so the oscilloscope draws the playback as a simple level: raised while an
 * utterance plays, bumped on each word boundary, and gone when it stops.
 */

/** The slice of the Web Speech API's recogniser the pane uses. */
export interface Recogniser {
  continuous: boolean
  interimResults: boolean
  lang: string
  onresult: ((e: RecognitionResultEvent) => void) | null
  onerror: ((e: { error: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}

export interface RecognitionResultEvent {
  resultIndex: number
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } } & ArrayLike<{ transcript: string }>>
}

export type RecogniserClass = new () => Recogniser

/** The browser's recogniser, standard or prefixed, or null where it has none (Firefox). */
export function recogniserClass(): RecogniserClass | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as { SpeechRecognition?: RecogniserClass; webkitSpeechRecognition?: RecogniserClass }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

/** The one line the pane shows where the browser cannot hear speech. */
export const NO_RECOGNITION = 'This browser has no speech recognition: type to Voice it below, and it answers aloud.'

/** Recogniser errors that mean listening cannot go on, in words for the page. */
const FATAL: Readonly<Record<string, string>> = {
  'not-allowed': 'The browser was not allowed to recognise speech here: type to Voice it below instead.',
  'service-not-allowed': "The browser's speech recognition service refused: type to Voice it below instead.",
  'audio-capture': 'The browser could not hear the microphone for speech recognition: type to Voice it below instead.',
  network: "The browser's speech recognition could not reach its service: type to Voice it below instead.",
  'language-not-supported': "The browser's speech recognition does not support this language: type to Voice it below instead.",
}

export interface ListenHandlers {
  /** The phrase still being heard; '' when there is none. */
  onInterim: (text: string) => void
  /** One whole phrase: a turn. Never empty. */
  onFinal: (text: string) => void
  /** Listening has stopped for good; the sentence says why. */
  onUnavailable: (why: string) => void
}

/**
 * One recogniser for one session. `listen(true)` starts it (and restarts it
 * after each pause of its own) until `listen(false)`, `abort()` or `close()`.
 */
export class Listener {
  private readonly rec: Recogniser
  private want = false
  private running = false
  private dropping = false
  private closed = false

  constructor(Klass: RecogniserClass, private readonly h: ListenHandlers) {
    this.rec = new Klass()
    this.rec.continuous = true
    this.rec.interimResults = true
    this.rec.lang = typeof navigator !== 'undefined' && navigator.language ? navigator.language : 'en-US'
    this.rec.onresult = (e) => this.result(e)
    this.rec.onerror = (e) => this.error(e.error)
    this.rec.onend = () => this.ended()
  }

  /** Listen, or stop listening while still delivering the phrase being heard. */
  listen(on: boolean): void {
    if (this.closed) return
    this.want = on
    if (on) {
      this.dropping = false
      this.begin()
    } else if (this.running) {
      this.rec.stop()
    }
  }

  /** Stop listening and discard the phrase being heard. */
  abort(): void {
    this.want = false
    this.dropping = true
    this.h.onInterim('')
    if (this.running) this.rec.abort()
  }

  close(): void {
    this.abort()
    this.closed = true
    this.rec.onresult = null
    this.rec.onerror = null
    this.rec.onend = null
  }

  private begin(): void {
    if (this.running) return
    try {
      this.rec.start()
      this.running = true
    } catch {
      // Already started (InvalidStateError): it is listening, which is what was asked.
      this.running = true
    }
  }

  private result(e: RecognitionResultEvent): void {
    if (this.dropping || this.closed) return
    let interim = ''
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i]
      if (r === undefined) continue
      const text = (r[0]?.transcript ?? '').trim()
      if (r.isFinal) {
        if (text !== '') this.h.onFinal(text)
      } else if (text !== '') {
        interim = interim === '' ? text : `${interim} ${text}`
      }
    }
    this.h.onInterim(interim)
  }

  private error(code: string): void {
    const why = FATAL[code]
    if (why === undefined) return // no-speech, aborted: the end that follows restarts it
    this.want = false
    this.h.onInterim('')
    this.h.onUnavailable(why)
  }

  private ended(): void {
    this.running = false
    if (this.closed) return
    if (this.want) this.begin()
    else this.h.onInterim('')
  }
}

// ---- speaking -------------------------------------------------------------------

/** The level the oscilloscope draws while an utterance plays, and on a word. */
export const SPEAKING_LEVEL = 0.55
export const WORD_LEVEL = 1
/** How long a word's bump lasts before the level falls back. */
export const WORD_MS = 140

export interface SpeakHandlers {
  /** 0..1 while speaking, null when nothing is. */
  onLevel: (level: number | null) => void
}

/** Whether the browser can speak at all. */
export function canSpeak(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window && typeof SpeechSynthesisUtterance !== 'undefined'
}

/**
 * The voice the whole session speaks in: the browser's default for the page's
 * language, else any voice in that language, else its default. Null while the
 * browser has not listed its voices, which leaves the browser's own default.
 */
export function pickVoice(voices: readonly SpeechSynthesisVoice[], lang: string): SpeechSynthesisVoice | null {
  if (voices.length === 0) return null
  const base = lang.toLowerCase().split('-')[0] ?? ''
  const same = voices.filter((v) => v.lang.toLowerCase().split('-')[0] === base)
  return same.find((v) => v.default) ?? same[0] ?? voices.find((v) => v.default) ?? voices[0] ?? null
}

/** One session's speaker: one voice, and only ever one utterance at a time. */
export class Speaker {
  private voice: SpeechSynthesisVoice | null = null
  private current: SpeechSynthesisUtterance | null = null
  private timer: ReturnType<typeof setTimeout> | null = null

  constructor(private readonly h: SpeakHandlers) {}

  /** Speak `text`, cancelling whatever was playing. A browser that cannot speak shows the caption only. */
  speak(text: string): void {
    this.cancel()
    if (!canSpeak() || text.trim() === '') return
    const synth = window.speechSynthesis
    if (this.voice === null) {
      this.voice = pickVoice(synth.getVoices?.() ?? [], typeof navigator !== 'undefined' ? navigator.language || 'en' : 'en')
    }
    const u = new SpeechSynthesisUtterance(text)
    if (this.voice !== null) {
      u.voice = this.voice
      u.lang = this.voice.lang
    }
    const mine = () => this.current === u
    u.onstart = () => {
      if (mine()) this.h.onLevel(SPEAKING_LEVEL)
    }
    u.onboundary = () => {
      if (!mine()) return
      this.h.onLevel(WORD_LEVEL)
      this.clearTimer()
      this.timer = setTimeout(() => {
        if (mine()) this.h.onLevel(SPEAKING_LEVEL)
      }, WORD_MS)
    }
    const done = () => {
      if (!mine()) return
      this.current = null
      this.clearTimer()
      this.h.onLevel(null)
    }
    u.onend = done
    u.onerror = done
    this.current = u
    this.h.onLevel(SPEAKING_LEVEL)
    synth.speak(u)
  }

  /** Whether an utterance is playing or queued. */
  get speaking(): boolean {
    return this.current !== null
  }

  /** Stop speaking now. */
  cancel(): void {
    const was = this.current
    this.current = null
    this.clearTimer()
    if (was !== null) {
      if (canSpeak()) window.speechSynthesis.cancel()
      this.h.onLevel(null)
    }
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
  }
}
