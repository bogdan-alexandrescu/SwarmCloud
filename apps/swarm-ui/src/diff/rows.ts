// THE DIFF AS A FLAT LIST OF FIXED-HEIGHT ROWS, which is what makes a
// 50,000-line patch cheap: the viewer draws only the rows whose offsets fall
// inside the scroller's window, and the height of everything else is two
// spacers. Pure functions, so the model is testable without a DOM.
//
// A ROW'S HEIGHT IS FIXED BY ITS KIND, and DiffView draws every row with that
// height inline, so the stylesheet cannot move a row away from its offset.
// Lines never wrap -- they scroll sideways, the text exactly as given -- so a
// row cannot grow, and an offset computed here is where the row is drawn.

import type { DiffFile, DiffHunk } from './parse'
import type { ViewMode } from './storage'

export const ROW_H = 22
export const FILE_H = 36

/** Where a gap's hidden lines come from, once `getFile` has answered. */
export type ContextState =
  | { status: 'loading' }
  | { status: 'failed'; message: string }
  | { status: 'mismatch' }
  | { status: 'ready'; lines: string[] }

export type Row =
  | { t: 'file'; file: number }
  | { t: 'note'; file: number; text: string }
  | { t: 'hunk'; file: number; hunk: number }
  | { t: 'gap'; file: number; gap: number; hidden: number | null }
  | { t: 'line'; file: number; hunk: number; line: number }
  | { t: 'pair'; file: number; hunk: number; left: number | null; right: number | null }
  | { t: 'context'; file: number; gap: number; oldNo: number; newNo: number; text: string }

export function rowHeight(r: Row): number {
  return r.t === 'file' ? FILE_H : ROW_H
}

// ---------------------------------------------------------------------------
// Gaps: the unchanged lines between, before and after the hunks.
// ---------------------------------------------------------------------------

/**
 * The first line a hunk covers on one side. git writes the line BEFORE the
 * hunk as the start when that side is empty (`@@ -5,2 +4,0 @@` sits after new
 * line 4), so an empty side starts one further on.
 */
function first(start: number, count: number): number {
  return count === 0 ? start + 1 : start
}

/** The new-side line range a gap hides, [from, to] inclusive; `to` is null for "to the end of the file". */
export interface GapRange {
  from: number
  to: number | null
  /** Add to a new-side number to get the old-side number of the same unchanged line. */
  oldShift: number
}

/**
 * Gap `g` is the one before hunk `g`; gap `hunks.length` is the one after the
 * last hunk. Only text files that exist on both sides have gaps: an added or
 * a deleted file's one hunk is the whole file.
 */
export function gapRange(f: DiffFile, g: number): GapRange | null {
  if (f.binary || f.status === 'added' || f.status === 'deleted' || f.hunks.length === 0) return null
  const next: DiffHunk | undefined = f.hunks[g]
  const prev: DiffHunk | undefined = f.hunks[g - 1]
  const prevEnd = prev ? first(prev.newStart, prev.newLines) + prev.newLines - 1 : 0
  const prevOldEnd = prev ? first(prev.oldStart, prev.oldLines) + prev.oldLines - 1 : 0
  if (next) {
    const nFirst = first(next.newStart, next.newLines)
    const oFirst = first(next.oldStart, next.oldLines)
    if (nFirst - 1 < prevEnd + 1) return null
    return { from: prevEnd + 1, to: nFirst - 1, oldShift: oFirst - nFirst }
  }
  return { from: prevEnd + 1, to: null, oldShift: prevOldEnd - prevEnd }
}

/**
 * Does the file `getFile` returned agree with the patch? Every line the patch
 * says the new side holds must be in the file, at that number, verbatim.
 * Anything else -- a different revision, a truncated read -- and the viewer
 * shows NO context from it rather than lines that may not be the file's.
 */
export function contextAgrees(f: DiffFile, lines: readonly string[]): boolean {
  for (const h of f.hunks) {
    for (const l of h.lines) {
      if (l.newNo === null) continue
      if (lines[l.newNo - 1] !== l.text) return false
    }
  }
  return true
}

/** A file body as lines: one trailing newline ends the last line, it does not add one. */
export function splitFile(body: string): string[] {
  if (body === '') return []
  const lines = body.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines
}

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

export interface RowInput {
  files: readonly DiffFile[]
  collapsed: ReadonlySet<number>
  mode: ViewMode
  /** Whether a `getFile` was given; without it no trailing gap is offered. */
  canExpand: boolean
  context: ReadonlyMap<number, ContextState>
  /** `${file}:${gap}` for each gap the reader opened. */
  opened: ReadonlySet<string>
}

function pairs(h: DiffHunk): Array<[number | null, number | null]> {
  const out: Array<[number | null, number | null]> = []
  let i = 0
  while (i < h.lines.length) {
    const l = h.lines[i]!
    if (l.kind === 'context') {
      out.push([i, i])
      i += 1
      continue
    }
    const dels: number[] = []
    const adds: number[] = []
    while (i < h.lines.length && h.lines[i]!.kind === 'del') dels.push(i++)
    while (i < h.lines.length && h.lines[i]!.kind === 'add') adds.push(i++)
    for (let k = 0; k < Math.max(dels.length, adds.length); k++) out.push([dels[k] ?? null, adds[k] ?? null])
  }
  return out
}

function gapRows(input: RowInput, fi: number, g: number, out: Row[]): void {
  const f = input.files[fi]!
  const range = gapRange(f, g)
  if (range === null) return
  const trailing = range.to === null
  if (trailing && !input.canExpand) return
  const ctx = input.context.get(fi)
  if (ctx?.status === 'ready') {
    const to = range.to ?? ctx.lines.length
    if (to < range.from) return
    if (input.opened.has(`${fi}:${g}`)) {
      for (let n = range.from; n <= to; n++) {
        out.push({ t: 'context', file: fi, gap: g, newNo: n, oldNo: n + range.oldShift, text: ctx.lines[n - 1] ?? '' })
      }
      return
    }
    out.push({ t: 'gap', file: fi, gap: g, hidden: to - range.from + 1 })
    return
  }
  out.push({ t: 'gap', file: fi, gap: g, hidden: trailing ? null : range.to! - range.from + 1 })
}

export function buildRows(input: RowInput): Row[] {
  const out: Row[] = []
  input.files.forEach((f, fi) => {
    out.push({ t: 'file', file: fi })
    if (input.collapsed.has(fi)) return
    if (f.binary) {
      out.push({ t: 'note', file: fi, text: 'binary file not shown' })
      return
    }
    if (f.hunks.length === 0) {
      out.push({ t: 'note', file: fi, text: 'no content change' })
      return
    }
    f.hunks.forEach((h, hi) => {
      gapRows(input, fi, hi, out)
      out.push({ t: 'hunk', file: fi, hunk: hi })
      if (input.mode === 'split') {
        for (const [left, right] of pairs(h)) out.push({ t: 'pair', file: fi, hunk: hi, left, right })
      } else {
        h.lines.forEach((_, li) => out.push({ t: 'line', file: fi, hunk: hi, line: li }))
      }
    })
    gapRows(input, fi, f.hunks.length, out)
  })
  return out
}

/** offsets[i] is the top of row i; offsets[rows.length] is the total height. */
export function rowOffsets(rows: readonly Row[]): Float64Array {
  const out = new Float64Array(rows.length + 1)
  for (let i = 0; i < rows.length; i++) out[i + 1] = out[i]! + rowHeight(rows[i]!)
  return out
}

/** The last row whose top is at or above `y`. */
export function rowAt(offsets: Float64Array, y: number): number {
  let lo = 0
  let hi = offsets.length - 2
  if (hi < 0) return 0
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (offsets[mid]! <= y) lo = mid
    else hi = mid - 1
  }
  return lo
}

// ---------------------------------------------------------------------------
// Find
// ---------------------------------------------------------------------------

export interface Match {
  file: number
  hunk: number
  line: number
  start: number
  length: number
}

/**
 * Case-insensitive where lower-casing keeps every index in place, which is
 * every string except a handful of Unicode letters whose lower case is longer;
 * for those the search is exact, so a highlight can never land on the wrong
 * characters.
 */
export function occurrences(text: string, query: string): number[] {
  if (query === '') return []
  const lt = text.toLowerCase()
  const lq = query.toLowerCase()
  const [hay, needle] = lt.length === text.length && lq.length === query.length ? [lt, lq] : [text, query]
  const out: number[] = []
  let at = hay.indexOf(needle)
  while (at !== -1) {
    out.push(at)
    at = hay.indexOf(needle, at + needle.length)
  }
  return out
}

/** Every match in every hunk line of every file, in reading order. */
export function findMatches(files: readonly DiffFile[], query: string): Match[] {
  const out: Match[] = []
  if (query === '') return out
  files.forEach((f, fi) =>
    f.hunks.forEach((h, hi) =>
      h.lines.forEach((l, li) => {
        for (const start of occurrences(l.text, query)) out.push({ file: fi, hunk: hi, line: li, start, length: query.length })
      }),
    ),
  )
  return out
}

/** The row that draws hunk line `line`, in either mode, or -1. */
export function rowOfLine(rows: readonly Row[], file: number, hunk: number, line: number): number {
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i]!
    if (r.file !== file) continue
    if (r.t === 'line' && r.hunk === hunk && r.line === line) return i
    if (r.t === 'pair' && r.hunk === hunk && (r.left === line || r.right === line)) return i
  }
  return -1
}

/** The widest line, in characters with a tab counted at the stylesheet's tab-size. */
export function widestLine(files: readonly DiffFile[]): number {
  let w = 0
  for (const f of files) {
    for (const h of f.hunks) {
      if (h.header.length > w) w = h.header.length
      for (const l of h.lines) {
        let n = l.text.length
        for (let i = 0; i < l.text.length; i++) if (l.text.charCodeAt(i) === 9) n += 3
        if (n > w) w = n
      }
    }
  }
  return w
}
