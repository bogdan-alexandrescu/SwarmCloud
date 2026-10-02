// FIND IN THE DIFF (#310): one string, searched across EVERY file of the
// patch, not only the file that is open. Pure functions, so the counter and
// the jumps are testable without a DOM.
//
// WHAT IS SEARCHED is the patch's own lines -- context, added and removed, the
// text exactly as given. Not the hunk headers, not the paths (the path filter
// is for those), and not the unchanged lines `getFile` expanded between hunks:
// those are not in the patch, and a match there would count something the
// patch does not hold. A content carriage return at the end of a line is drawn
// as a mark of its own (DiffView `LineText`), so it is not searched either.
//
// THE QUERY IS TEXT, NOT A PATTERN, and matched case-insensitively. It is
// escaped into a RegExp with the `i` flag rather than compared lower-cased,
// because lower-casing can change a string's length (`İ` becomes two code
// units) and every offset below indexes the line as given.
//
// MATCHES COME IN LIST ORDER -- the file list's reading order (rows.ts
// `listOrder`), then hunk, line and position -- so next and previous walk the
// patch the way the list reads, switching the open file as they cross one.

import type { DiffFile } from './parse'

export interface FindMatch {
  /** Index into the patch's files. */
  file: number
  hunk: number
  line: number
  /** [start, end) in the line's text as given. */
  start: number
  end: number
}

/** One match on one line, with its place in the whole patch's list of matches. */
export interface LineHit {
  start: number
  end: number
  index: number
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** The searchable part of a line: its text without the carriage return drawn as its own mark. */
function body(text: string): string {
  return text.endsWith('\r') ? text.slice(0, -1) : text
}

/** Every match of `query` in every file, in `order` (the list's reading order). */
export function findMatches(files: readonly DiffFile[], order: readonly number[], query: string): FindMatch[] {
  if (query === '') return []
  const re = new RegExp(escapeRegExp(query), 'gi')
  const out: FindMatch[] = []
  for (const fi of order) {
    const f = files[fi]
    if (f === undefined) continue
    f.hunks.forEach((h, hi) => {
      h.lines.forEach((l, li) => {
        const text = body(l.text)
        re.lastIndex = 0
        let m: RegExpExecArray | null
        while ((m = re.exec(text)) !== null) {
          out.push({ file: fi, hunk: hi, line: li, start: m.index, end: m.index + m[0].length })
        }
      })
    })
  }
  return out
}

/**
 * Where a new find starts: the first match in the open file, or in the first
 * file after it in the list, wrapping to the top. -1 when there is no match.
 */
export function firstMatchFrom(matches: readonly FindMatch[], order: readonly number[], file: number): number {
  if (matches.length === 0) return -1
  const rank = new Map<number, number>()
  order.forEach((fi, i) => rank.set(fi, i))
  const from = rank.get(file) ?? 0
  const at = matches.findIndex((m) => (rank.get(m.file) ?? 0) >= from)
  return at === -1 ? 0 : at
}

/** The open file's matches, keyed `${hunk}:${line}`, for the rows to mark. */
export function hitsInFile(matches: readonly FindMatch[], file: number): Map<string, LineHit[]> {
  const out = new Map<string, LineHit[]>()
  matches.forEach((m, index) => {
    if (m.file !== file) return
    const key = `${m.hunk}:${m.line}`
    let list = out.get(key)
    if (list === undefined) {
      list = []
      out.set(key, list)
    }
    list.push({ start: m.start, end: m.end, index })
  })
  return out
}

/** A line cut at its matches: plain pieces carry `index: null`, marked ones the match's index. */
export function hitSegments(text: string, hits: readonly LineHit[]): Array<{ text: string; index: number | null }> {
  const out: Array<{ text: string; index: number | null }> = []
  let at = 0
  for (const h of hits) {
    const start = Math.max(at, Math.min(h.start, text.length))
    const end = Math.min(h.end, text.length)
    if (start > at) out.push({ text: text.slice(at, start), index: null })
    if (end > start) out.push({ text: text.slice(start, end), index: h.index })
    at = Math.max(at, end)
  }
  if (at < text.length) out.push({ text: text.slice(at), index: null })
  return out
}
