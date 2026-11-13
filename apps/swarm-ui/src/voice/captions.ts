/**
 * VOICE IT'S CAPTIONS: both sides' transcripts, as they arrive on the WebRTC
 * data channel (docs/design/voice-it.md §3.5, "Captions take the short path").
 *
 * A segment arrives as deltas, drawn at once as a partial line, then one final
 * event whose text replaces them. Only final lines go into the page's role=log
 * region, so a screen reader announces each segment once, whole, and never
 * the words as they grow (§3.10; MDN's live-region guide).
 *
 * DELTAS AND NO FINAL (the live deployment, 12 November 2026: both sides read
 * one word per line; the trace counted 43 transcript pieces and 0 finals).
 * GPT-Live sends the browser deltas only, each with its own event id and no
 * item id, so a side's deltas are one line (a segment is keyed by `item_id`,
 * then `response_id`, never the event id) and that line closes, whole, on the
 * first of: a final for that side; a new item of that side; the other side
 * starting to speak over a line that names no item (one that does may still be
 * transcribed behind the reply); the end of the side's turn or response
 * (`captionEnds`); or CAPTION_PAUSE_MS with no new delta of that side, timed
 * by the session (`closeCaption`). swarm-voice's live.py joins the same stream
 * the same way for its planner ("THE SIDEBAND SENDS ONLY DELTAS" and "WHOLE
 * UTTERANCES", 10 November 2026).
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
  /** The segment it belongs to (OpenAI's item id, then response id); null when the event names neither. */
  key: string | null
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
  /** The segment's key, when its events named one: a final for it replaces the line. */
  key?: string | null
  /** Closed by the segment's own final event: a later delta of it adds nothing. */
  settled?: boolean
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
  // Never the event id: every delta has its own, and keyed by it each word was a line.
  const key = str(m.item_id) || str(m.response_id) || null
  const text = kind.final ? (str(m.transcript) ?? str(m.text)) : (str(m.delta) ?? str(m.text))
  if (text === null) return null
  return { side: kind.side, key, text, final: kind.final }
}

/**
 * Events that end a side's line without a final, by type. What the data
 * channel sends besides the transcript deltas (live.py, 10 November 2026):
 * `session.output_audio.started`/`.stopped` around the voice's playback and
 * `session.delegation.created` when the voice hands the person's request to
 * the planner; it sends no end of the person's turn. The Realtime API's names
 * are read too: `output_audio_buffer.stopped`/`.cleared` (playback ended or
 * was cut off) and `response.done` (the response is complete).
 */
const ENDS: Readonly<Record<string, CaptionSide>> = {
  'session.output_audio.stopped': 'agent',
  'output_audio_buffer.stopped': 'agent',
  'output_audio_buffer.cleared': 'agent',
  'response.done': 'agent',
  'session.delegation.created': 'you',
}

/** The side whose line a data-channel message ends (`ENDS`), or null. */
export function captionEnd(raw: unknown): CaptionSide | null {
  let msg: unknown = raw
  if (typeof raw === 'string') {
    try {
      msg = JSON.parse(raw)
    } catch {
      return null
    }
  }
  if (msg === null || typeof msg !== 'object') return null
  return ENDS[str((msg as Record<string, unknown>).type) ?? ''] ?? null
}

/**
 * How long a side's line stays open with no new delta: about one second, the
 * middle of live.py's utterance gaps (0.8 s after a sentence's end mark, 1.6 s
 * without one). A caption only draws, so one gap serves both.
 */
export const CAPTION_PAUSE_MS = 1000

let opened = 0

/** A fresh line id: a key a pause split, or no key at all, can open several lines. */
function lineId(lines: readonly CaptionLine[], side: CaptionSide, key: string | null): string {
  const id = `${side}:${key ?? '~'}`
  if (key !== null && !lines.some((l) => l.id === id)) return id
  opened += 1
  return `${id}#${opened}`
}

/** The index of the side's open line, or -1. */
function openAt(lines: readonly CaptionLine[], side: CaptionSide): number {
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = lines[i]
    if (l !== undefined && l.side === side && !l.final) return i
  }
  return -1
}

/**
 * The lines with the side's open line closed: final, its text as the deltas
 * sent it, the ends' spaces trimmed. A line with no words is dropped.
 */
export function closeCaption(lines: readonly CaptionLine[], side: CaptionSide): CaptionLine[] {
  const at = openAt(lines, side)
  if (at === -1) return [...lines]
  const was = lines[at] as CaptionLine
  const text = was.text.trim()
  if (text === '') return lines.filter((_, i) => i !== at)
  return lines.map((l, i) => (i === at ? { ...l, text, final: true } : l))
}

/**
 * Whether `after` has a line closed that `before` had open, or had not at all:
 * a line closed without a final (a pause, the other side, the end of a turn)
 * is a segment someone spoke, so it counts as activity for the idle stop just
 * as a final does (the owner's pick on #3956, 12 November 2026).
 */
export function closedLine(before: readonly CaptionLine[], after: readonly CaptionLine[]): boolean {
  const wasFinal = new Set(before.filter((l) => l.final).map((l) => l.id))
  return after.some((l) => l.final && !wasFinal.has(l.id))
}

/**
 * The lines after one event. A delta grows its side's open line, or closes it
 * and starts one when its key differs; it closes the other side's open line
 * when that line names no item. A final replaces its segment's lines (every
 * line of its key, else the side's open line) with its text, or lands as a new
 * line when its deltas never came; a final with no words drops them. A delta
 * of a segment its final already closed adds nothing.
 */
export function applyCaption(lines: readonly CaptionLine[], ev: CaptionEvent): CaptionLine[] {
  const key = ev.key
  if (ev.final) {
    const ofKey = (l: CaptionLine) => key !== null && l.side === ev.side && (l.key === key || l.id === `${ev.side}:${key}`)
    let first = lines.findIndex(ofKey)
    const open = openAt(lines, ev.side)
    // A keyed final takes over an open line of no key (or of its own); never another item's.
    if (first === -1 && open !== -1 && (key === null || (lines[open]?.key ?? null) === null)) first = open
    const gone = (l: CaptionLine, i: number) => i === first || ofKey(l)
    const text = ev.text.trim()
    const kept: CaptionLine[] = []
    lines.forEach((l, i) => {
      if (i === first && text !== '') kept.push({ id: l.id, side: ev.side, text, final: true, key, settled: true })
      else if (!gone(l, i)) kept.push(l)
    })
    if (first !== -1 || text === '') return kept
    return [...kept, { id: lineId(lines, ev.side, key), side: ev.side, text, final: true, key, settled: true }]
  }
  if (key !== null && lines.some((l) => l.side === ev.side && l.settled === true && (l.key === key || l.id === `${ev.side}:${key}`))) {
    return [...lines]
  }
  const other: CaptionSide = ev.side === 'you' ? 'agent' : 'you'
  const otherAt = openAt(lines, other)
  let next = otherAt !== -1 && (lines[otherAt]?.key ?? null) === null ? closeCaption(lines, other) : [...lines]
  const at = openAt(next, ev.side)
  const was = next[at]
  if (was !== undefined && (was.key ?? null) === key) {
    return next.map((l, i) => (i === at ? { ...l, text: l.text + ev.text } : l))
  }
  next = closeCaption(next, ev.side)
  return [...next, { id: lineId(next, ev.side, key), side: ev.side, text: ev.text, final: false, key }]
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
