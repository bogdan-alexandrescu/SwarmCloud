/**
 * THE LOG DOCK'S ARITHMETIC (viewers.html A, picked 2026-10-02), kept apart
 * from the component so each rule can be tested without a render.
 *
 * Nothing here re-derives what the server decided. Redaction happened on the
 * server, at read time; the mask is the server's `********`. Byte positions
 * are the server's (`tail_window`, `offset`, `returned_bytes`), never counted
 * from the text, because redaction makes the text shorter than the bytes it
 * came from.
 */

/** The mask the server writes in place of a credential (swarm_redaction/rules.py). */
export const MASK = '********'

/** Where one read of a stream sits in the whole stream, as the server said. */
export interface WindowAt {
  /** Where this window starts in the WHOLE stream, or null when the read did not say. */
  start: number | null
  /** Where it ends: `start + returned_bytes`, or null when `start` is. */
  end: number | null
  /** The tail header's `at=`, when the server sent one. */
  publishedAt: string | null
  /** Whether this read is the live tail (`source: live`), the only kind that can skip bytes. */
  live: boolean
}

/** The facts `windowAt` reads: the subset of a log stream or a transcript stream it needs. */
export interface WindowFacts {
  source: string | null
  status: string
  offset: number
  returned_bytes: number
  total_bytes: number | null
  tail_window: { object_offset: number; stream_size: number; published_at?: string | null } | null
}

/**
 * WHERE A WINDOW SITS. A live tail's position in the stream is its
 * `#swarm-tail` header (`tail_window.object_offset`); a final object's is the
 * read's own `offset`, because the object IS the stream. A live tail with no
 * header (a worker older than #184) has no position, and says so as null.
 *
 * THE HEADER IS IN THE OBJECT'S BYTES AND NOT IN THE STREAM'S. The server's
 * `offset` counts from the start of the tail object, header included
 * (inspect.py skips it and adds its length), so the header's length is taken
 * back off: the object holds `stream_size - object_offset` stream bytes after
 * it, and the rest of `total_bytes` is the header.
 */
export function windowAt(s: WindowFacts): WindowAt {
  const live = s.source === 'live'
  if (s.status !== 'ok') return { start: null, end: null, publishedAt: null, live }
  if (live) {
    if (s.tail_window === null) return { start: null, end: null, publishedAt: null, live }
    const tail = s.tail_window
    const header = s.total_bytes === null ? 0 : Math.max(0, s.total_bytes - (tail.stream_size - tail.object_offset))
    const start = tail.object_offset + Math.max(0, s.offset - header)
    return { start, end: start + s.returned_bytes, publishedAt: tail.published_at ?? null, live }
  }
  return { start: s.offset, end: s.offset + s.returned_bytes, publishedAt: null, live }
}

/** Bytes that were published and overwritten between two reads. */
export interface Gap {
  kind: 'gap'
  /** The previous read's end: the first byte nobody read. */
  from: number
  /** This read's start: the first byte read again. */
  to: number
  /** The two reads' publish times, when the tail headers carried them. */
  before: string | null
  after: string | null
}

/** Two reads whose positions cannot be compared: the tail carried no header. */
export interface GapUnknown {
  kind: 'unknown'
}

/**
 * WHETHER OUTPUT FELL OUT OF THE TAIL BETWEEN TWO READS.
 *
 * The worker republishes the newest 256 KiB every 5 s. When more than that is
 * written between two of this console's reads, the bytes between the previous
 * read's end and this read's start were published and overwritten before
 * anyone read them. That is a gap, and it is drawn as one, with both bytes.
 *
 *  - null: nothing to say. No previous read, the reads are of different
 *    objects (a final record replaced the tail, another attempt), or the
 *    windows touch or overlap.
 *  - `unknown`: two live reads and at least one carried no position header,
 *    so the console cannot tell -- which is not "nothing is missing".
 */
export function tailGap(prev: WindowAt | null, next: WindowAt): Gap | GapUnknown | null {
  if (prev === null || !prev.live || !next.live) return null
  if (prev.end === null || next.start === null) return { kind: 'unknown' }
  if (next.start <= prev.end) return null
  return { kind: 'gap', from: prev.end, to: next.start, before: prev.publishedAt, after: next.publishedAt }
}

/**
 * THE CONSOLE-SIDE ERROR PATTERN. The API marks no log line as an error
 * (routes/tasks.py), so "jump to error" matches this, plus a transcript tool
 * result's own `is_error`. It can miss an error that says none of these words
 * and it can match a line that only quotes one; the button says so.
 *
 * BROADENED (owner QA, 2026-10-04): the control was disabled at 0 on every
 * agent tried, the mock's own stderr `deterministic failure, exit 2` among
 * them, because only capitals (`ERROR`, `FAILED`) and `exit code N` matched.
 * It now matches, in any case, the words error, exception, failed, failure
 * and fatal; a `SomethingError` or `SomethingException` name; Traceback and
 * `panic:`; and a non-zero exit however it is written (`exit 2`, `exit code
 * 2`, `exited with status 2`, `exit_code=2`). Not `errors` or `failures`:
 * "0 failures" and "errors were handled" are not errors.
 */
const ERROR_WORDS = /\b(?:error|exception|failed|failure|fatal)\b|\bexit(?:ed)?(?:\s+with)?(?:[\s_]+(?:code|status))?[\s:=]+[1-9]\d*\b/i
const ERROR_NAMES = /\b[A-Z]\w*(?:Error|Exception)\b|\bTraceback\b|\bpanic:/

/** The words the pattern matches, for the button that says what it matches. */
export const ERROR_PATTERN_SAYS =
  'error, exception, failed, failure or fatal in any case, a name ending in Error or Exception, Traceback, panic:, and a non-zero exit (exit 2, exit code 2, exited with status 2)'

export function isErrorLine(line: string): boolean {
  return ERROR_WORDS.test(line) || ERROR_NAMES.test(line)
}

/** A window's lines. A trailing newline does not make an empty last line. */
export function linesOf(content: string): string[] {
  if (content === '') return []
  const lines = content.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines
}

/** The indexes of the lines that contain `needle`, case-insensitively. Empty needle, none. */
export function searchLines(lines: readonly string[], needle: string): number[] {
  const n = needle.trim().toLowerCase()
  if (n === '') return []
  const hits: number[] = []
  lines.forEach((l, i) => {
    if (l.toLowerCase().includes(n)) hits.push(i)
  })
  return hits
}

/** One run of a line: plain text, a search hit, or the server's mask. */
export interface Segment {
  text: string
  kind: 'text' | 'hit' | 'mask'
}

/**
 * A line, cut into what it draws: the server's mask in its own ink, each
 * search hit marked. The mask is matched first and a hit never splits one, so
 * a search for `*` cannot make a masked value look like two.
 */
export function segments(line: string, needle: string): Segment[] {
  const out: Segment[] = []
  const n = needle.trim().toLowerCase()
  const parts = line.split(MASK)
  parts.forEach((part, i) => {
    if (i > 0) out.push({ text: MASK, kind: 'mask' })
    if (part === '') return
    if (n === '') {
      out.push({ text: part, kind: 'text' })
      return
    }
    const lower = part.toLowerCase()
    let at = 0
    for (;;) {
      const found = lower.indexOf(n, at)
      if (found < 0) break
      if (found > at) out.push({ text: part.slice(at, found), kind: 'text' })
      out.push({ text: part.slice(found, found + n.length), kind: 'hit' })
      at = found + n.length
    }
    if (at < part.length) out.push({ text: part.slice(at), kind: 'text' })
  })
  return out
}

/** How many lines arrived while the dock was paused, and whether that count is exact. */
export interface Arrived {
  lines: number
  exact: boolean
}

const utf8 = new TextEncoder()

/**
 * LINES THAT ARRIVED SINCE THE PAUSE.
 *
 * Exact when both windows start at the same byte of the same stream: the new
 * window is the old one plus what was appended, so the difference in lines is
 * the count. Once the tail has slid, the count is the newlines in the last
 * `end - end` BYTES of the new window, which redaction can shift by a line --
 * so it is marked approximate and the pill says "about".
 */
export function arrivedLines(
  paused: { at: WindowAt; content: string },
  next: { at: WindowAt; content: string },
): Arrived | null {
  if (paused.at.end === null || next.at.end === null || paused.at.start === null || next.at.start === null) return null
  if (next.at.end <= paused.at.end) return { lines: 0, exact: true }
  if (next.at.start === paused.at.start) {
    return { lines: Math.max(0, linesOf(next.content).length - linesOf(paused.content).length), exact: true }
  }
  const bytes = utf8.encode(next.content)
  const fresh = Math.min(bytes.length, next.at.end - paused.at.end)
  let n = 0
  for (let i = bytes.length - fresh; i < bytes.length; i++) if (bytes[i] === 10) n++
  return { lines: n, exact: false }
}

/** `1,318,400`: a byte position as the frames print one. */
export function bytePos(n: number): string {
  return n.toLocaleString('en-US')
}

/** `111 KiB`: a gap's size. */
export function gapSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}
