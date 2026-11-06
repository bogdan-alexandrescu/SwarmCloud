/**
 * VOICE IT'S CAPTIONS: both sides' transcripts, as they arrive on the WebRTC
 * data channel (docs/design/voice-it.md §3.5, "Captions take the short path").
 *
 * A segment arrives as deltas, drawn at once as a partial line, then one final
 * event whose text replaces them. Only final lines go into the page's role=log
 * region, so a screen reader announces each segment once, whole, and never
 * the words as they grow (§3.10; MDN's live-region guide).
 *
 * WHICH EVENT NAMES. OpenAI's Live reference names the transcript deltas
 * `session.input_transcript.delta` (the person) and
 * `session.output_transcript.delta` (the agent); the final events are read as
 * the same names ending `.done`. The Realtime API's names, which a Live
 * session may still send, are read too:
 * `conversation.item.input_audio_transcription.{delta,completed}` and
 * `response.output_audio_transcript.{delta,done}` (beta:
 * `response.audio_transcript.*`). Anything else is not a caption.
 */

export type CaptionSide = 'you' | 'agent'

/** One caption-bearing event, reduced to what the page draws. */
export interface CaptionEvent {
  side: CaptionSide
  /** The segment it belongs to (OpenAI's item id). */
  key: string
  /** A delta's new words, or a final event's whole text. */
  text: string
  final: boolean
}

export interface CaptionLine {
  id: string
  side: CaptionSide
  text: string
  /** False while the segment is still being spoken: muted, and not announced. */
  final: boolean
}

const EVENTS: Readonly<Record<string, { side: CaptionSide; final: boolean }>> = {
  'session.input_transcript.delta': { side: 'you', final: false },
  'session.input_transcript.done': { side: 'you', final: true },
  'session.output_transcript.delta': { side: 'agent', final: false },
  'session.output_transcript.done': { side: 'agent', final: true },
  'conversation.item.input_audio_transcription.delta': { side: 'you', final: false },
  'conversation.item.input_audio_transcription.completed': { side: 'you', final: true },
  'response.output_audio_transcript.delta': { side: 'agent', final: false },
  'response.output_audio_transcript.done': { side: 'agent', final: true },
  'response.audio_transcript.delta': { side: 'agent', final: false },
  'response.audio_transcript.done': { side: 'agent', final: true },
}

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : null
}

/**
 * The caption a data-channel message carries, or null. Takes the parsed
 * object or the raw string the channel delivered; a message that is not JSON
 * is not a caption, never an error.
 */
export function captionEvent(raw: unknown): CaptionEvent | null {
  let msg: unknown = raw
  if (typeof raw === 'string') {
    try {
      msg = JSON.parse(raw)
    } catch {
      return null
    }
  }
  if (msg === null || typeof msg !== 'object') return null
  const m = msg as Record<string, unknown>
  const kind = EVENTS[str(m.type) ?? '']
  if (kind === undefined) return null
  const key = str(m.item_id) ?? str(m.response_id) ?? str(m.event_id)
  const text = kind.final ? (str(m.transcript) ?? str(m.text)) : (str(m.delta) ?? str(m.text))
  if (key === null || text === null) return null
  return { side: kind.side, key, text, final: kind.final }
}

/**
 * The lines after one event. A delta grows its segment's partial line (or
 * starts one); a final replaces it with the final text, or starts a final
 * line when its deltas never came. A final with no words drops the line.
 */
export function applyCaption(lines: readonly CaptionLine[], ev: CaptionEvent): CaptionLine[] {
  const id = `${ev.side}:${ev.key}`
  const at = lines.findIndex((l) => l.id === id)
  if (ev.final) {
    const text = ev.text.trim()
    if (text === '') return lines.filter((l) => l.id !== id)
    const line: CaptionLine = { id, side: ev.side, text, final: true }
    return at === -1 ? [...lines, line] : lines.map((l, i) => (i === at ? line : l))
  }
  if (at === -1) return [...lines, { id, side: ev.side, text: ev.text, final: false }]
  const was = lines[at]
  // A delta after the final (out of order) changes nothing that was announced.
  if (was === undefined || was.final) return [...lines]
  return lines.map((l, i) => (i === at ? { ...l, text: l.text + ev.text } : l))
}

/**
 * How many final lines the page keeps. A conversation runs for an hour; the
 * stored transcript is swarm-voice's redacted copy (§3.9), so the page needs
 * only enough to read back, and an unbounded list would grow the log forever.
 */
export const KEPT_LINES = 400

/** Drop the oldest final lines past `KEPT_LINES`; partial lines always stay. */
export function trimCaptions(lines: readonly CaptionLine[]): CaptionLine[] {
  const finals = lines.filter((l) => l.final).length
  let drop = finals - KEPT_LINES
  if (drop <= 0) return [...lines]
  return lines.filter((l) => {
    if (drop > 0 && l.final) {
      drop -= 1
      return false
    }
    return true
  })
}
