// THE TABLE-FIT MODEL: where each cell's text lands in a `table-layout: fixed`
// table, computed from the shipped cascade rather than from a layout engine.
//
// WHY A MODEL. jsdom lays nothing out (`getBoundingClientRect()` is all
// zeros, spaceprobe.ts measured it), so "do two cells' text boxes overlap" is
// not a question it can answer. A FIXED table is the one layout simple enough
// to compute honestly: each column is exactly the width its head declares (a
// px or a share of the table), the one column with no width takes what is
// left, and the table is its container's width or its own `min-width`,
// whichever is larger. Every number below is read off the cascade
// (`marks.painted`) for an element `<App />` rendered -- a deleted width, a
// padding that went to zero or a `nowrap` that became `normal` changes them.
//
// THE TEXT WIDTH IS AN UPPER BOUND, ON PURPOSE: 0.6em per character for the
// mono face (DM Mono's advance is 0.6em exactly) and 0.56em for the sans
// (Inter Tight averages under 0.5em on lower-case text). Over-estimating makes
// a fit claim conservative: if the model says a figure fits, it fits.
import { expect } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'

/** One length in px against `basis` (for %), or null for `auto` / none. */
export function lengthPx(value: string | null, basis: number): number | null {
  if (value === null) return null
  const v = resolveVars(value.trim(), TABLES.dark)
  if (v === 'auto' || v === '' || v === 'none') return null
  const m = /^(-?[\d.]+)(px|rem|%)?$/.exec(v)
  if (m === null) throw new Error(`tablefit cannot read the length ${JSON.stringify(value)} (${v})`)
  const n = Number(m[1])
  if (m[2] === '%') return (n / 100) * basis
  if (m[2] === 'rem') return n * 16
  if (m[2] === undefined && n !== 0) throw new Error(`unitless length ${v}`)
  return n
}

/** The first property of `props` the cascade gives `el` or an ancestor. */
function inherited(el: Element, props: readonly string[], env: CascadeEnv): string | null {
  for (let n: Element | null = el; n !== null; n = n.parentElement) {
    const v = painted(n, props, env)
    if (v !== null && v.trim() !== 'inherit') return v
  }
  return null
}

/** The font size in px the cascade gives `el` (from `font-size` or the `font` shorthand). */
export function fontPx(el: Element, env: CascadeEnv): number {
  for (let n: Element | null = el; n !== null; n = n.parentElement) {
    const size = painted(n, 'font-size', env)
    const font = painted(n, 'font', env)
    // Whichever the cascade put on this element; the shorthand resets the size.
    const v = size ?? (font === null ? null : (/(?:^|\s)([\d.]+(?:px|rem)|var\([^)]*\))(?:\/|\s)/.exec(resolveVars(font, TABLES.dark))?.[1] ?? null))
    if (v === null || v === 'inherit') continue
    const r = lengthPx(v, 16)
    if (r !== null) return r
  }
  return 16
}

/** The conservative width of `text` in `el`'s face and size. */
export function textPx(text: string, el: Element, env: CascadeEnv): number {
  const em = familyOf(el, env) === 'mono' ? 0.6 : 0.56
  return [...text].length * em * fontPx(el, env)
}

/** `[left, right]` padding in px. */
export function paddingX(el: Element, env: CascadeEnv): [number, number] {
  const side = (s: 'left' | 'right'): number => {
    const own = painted(el, [`padding-${s}`, 'padding-inline', 'padding'], env)
    if (own === null) return 0
    const parts = resolveVars(own, TABLES.dark).trim().split(/\s+/)
    // padding: a | a b | a b c | a b c d ; padding-inline: a | a b ; padding-left: a
    let pick: string
    const full = cascadeIsShorthand(el, s, env)
    if (full === 'padding') pick = parts.length === 1 ? parts[0]! : parts.length === 4 && s === 'left' ? parts[3]! : parts[1]!
    else if (full === 'padding-inline') pick = parts.length === 1 ? parts[0]! : s === 'left' ? parts[0]! : parts[1]!
    else pick = parts[0]!
    return lengthPx(pick, 0) ?? 0
  }
  return [side('left'), side('right')]
}

function cascadeIsShorthand(el: Element, s: 'left' | 'right', env: CascadeEnv): string {
  for (const p of [`padding-${s}`, 'padding-inline', 'padding']) {
    // The winner among all three is what `painted` returned; find which property it was.
    const only = painted(el, p, env)
    const all = painted(el, [`padding-${s}`, 'padding-inline', 'padding'], env)
    if (only !== null && only === all) return p
  }
  return 'padding'
}

export interface ColumnBox {
  col: string
  start: number
  end: number
}

/** Each head's column in a fixed table `containerPx` wide (or its min-width, if larger). */
export function fixedColumns(table: HTMLTableElement, containerPx: number, env: CascadeEnv): { width: number; cols: ColumnBox[] } {
  expect(painted(table, 'table-layout', env), 'the table is not laid out fixed').toBe('fixed')
  const min = lengthPx(painted(table, 'min-width', env), containerPx) ?? 0
  const width = Math.max(containerPx, min)
  const heads = [...table.querySelectorAll<HTMLTableCellElement>('thead th')]
  const declared = heads.map((th) => lengthPx(painted(th, 'width', env), width))
  const fixed = declared.reduce<number>((a, w) => a + (w ?? 0), 0)
  const free = declared.filter((w) => w === null).length
  const slack = free === 0 ? 0 : Math.max(0, (width - fixed) / free)
  let x = 0
  const cols = heads.map((th, i) => {
    const w = declared[i] ?? slack
    const box = { col: th.getAttribute('data-col') ?? `#${i}`, start: x, end: x + w }
    x += w
    return box
  })
  return { width, cols }
}

/** Whether the cascade clips `el`'s overflowing text inside its own box. */
export function clips(el: Element, env: CascadeEnv): boolean {
  const o = painted(el, ['overflow-x', 'overflow'], env)
  return o !== null && /^(hidden|clip|auto|scroll)\b/.test(o)
}

/** Whether `el`'s text stays on one line. */
export function nowrap(el: Element, env: CascadeEnv): boolean {
  return inherited(el, ['white-space'], env) === 'nowrap'
}

export interface TextBox {
  col: string
  text: string
  start: number
  end: number
  /** The text is wider than its cell and the cell clips it. */
  clipped: boolean
}

/**
 * Where `text` is drawn in `cell` (in column `box`): its alignment, its
 * padding, and -- when it is wider than the content box -- either clipped to
 * that box (the cell clips) or running out of it (the overprint the owner saw).
 */
export interface CellStyle {
  pl: number
  pr: number
  /** px per character in the cell's face and size. */
  em: number
  align: string
  clips: boolean
}

/** What a cell's cascade says about where its text goes (read once per column: the cascade is slow). */
export function cellStyle(cell: Element, env: CascadeEnv): CellStyle {
  const [pl, pr] = paddingX(cell, env)
  return {
    pl,
    pr,
    em: (familyOf(cell, env) === 'mono' ? 0.6 : 0.56) * fontPx(cell, env),
    align: inherited(cell, ['text-align'], env) ?? 'left',
    clips: clips(cell, env),
  }
}

export function textBox(cell: Element | CellStyle, box: ColumnBox, text: string, env: CascadeEnv): TextBox {
  const st = cell instanceof Element ? cellStyle(cell, env) : cell
  const left = box.start + st.pl
  const right = box.end - st.pr
  const w = [...text].length * st.em
  const align = st.align
  const fits = w <= right - left
  const clipped = !fits && st.clips
  if (align === 'right' || align === 'end') {
    return { col: box.col, text, start: clipped ? left : right - w, end: right, clipped }
  }
  return { col: box.col, text, start: left, end: clipped ? right : left + w, clipped }
}

/** Every pair of neighbouring text boxes closer than `gap` px (or overlapping). */
export function crowded(boxes: readonly TextBox[], gap: number): string[] {
  const out: string[] = []
  const sorted = [...boxes].sort((a, b) => a.start - b.start)
  for (let i = 1; i < sorted.length; i++) {
    const a = sorted[i - 1]!
    const b = sorted[i]!
    const space = b.start - a.end
    if (space < gap) out.push(`${a.col} "${a.text}" and ${b.col} "${b.text}" are ${space.toFixed(1)}px apart`)
  }
  return out
}
