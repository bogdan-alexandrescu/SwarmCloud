// THE MARK VOCABULARY, READ OFF THE SHIPPED SHEET -- the helpers the
// chrome-shared lane's assertions share (CH-13, CH-17, CH-19, CH-21, CH-22,
// CH-23, TS-4).
//
// WHY A HELPER FILE AND NOT FOUR COPIES. Four test files ask the same two
// questions of `styles.css`: "what colour does the cascade paint this mark, in
// each theme" and "what SHAPE is it once the colour is taken away". A second
// copy of either is how two files start disagreeing about what a mark is.
//
// EVERYTHING GOES THROUGH `cascade` (cssgate.ts), NOT `getComputedStyle`. jsdom
// orders rules by source position alone and applies no `@media` block, and
// several of the marks below are decided by a specificity or a phone rule.
//
// A SELECTOR THIS CANNOT DRAW THROWS. `build` understands compounds of an
// optional tag and classes joined by `>` or a space, which is every mark in the
// sheet today; an attribute or a pseudo-class is a loud failure here rather
// than an element that quietly matches nothing.

import STYLES from '../styles.css?raw'
import { cascade, type CascadeEnv } from './cssgate'
import { colour, resolveVars, tokenTables, type RGBA } from './spaceprobe'

export type Theme = 'dark' | 'light'
export const THEMES: readonly Theme[] = ['dark', 'light']
export const TABLES = tokenTables(STYLES)

/** The five hues that carry a verdict or an accent (design-system.md §1.2). */
export const STATE_TOKENS = ['--ok', '--info', '--warn', '--bad', '--paused'] as const

/** Build the element a simple selector names and return its leaf. */
export function build(selector: string, hosts: HTMLElement[]): HTMLElement {
  const parts = selector
    .trim()
    .split(/\s*>\s*|\s+/)
    .filter((p) => p !== '')
  const host = document.createElement('div')
  document.body.appendChild(host)
  hosts.push(host)
  let parent: HTMLElement = host
  for (const part of parts) {
    const m = /^([a-z][a-z0-9]*)?((?:\.[\w-]+)+)?$/i.exec(part)
    if (m === null || (m[1] === undefined && m[2] === undefined)) {
      throw new Error(`build() cannot draw \`${part}\` of \`${selector}\``)
    }
    const el = document.createElement(m[1] ?? 'span')
    if (m[2] !== undefined) el.className = m[2].slice(1).split('.').join(' ')
    parent.appendChild(el)
    parent = el
  }
  return parent
}

/** The index of the `)` closing the `(` at `open`. */
function closing(s: string, open: number): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    if (s[i] === '(') depth++
    else if (s[i] === ')') {
      depth--
      if (depth === 0) return i
    }
  }
  return -1
}

/**
 * Every `var()` that names a property declared on a RULE rather than on
 * `:root` -- `--chip-tone`, say -- replaced by what the cascade gives the
 * element or its nearest ancestor that declares it, or by the fallback.
 * `:root` tokens are left for `resolveVars`, which reads them per theme.
 */
export function expandLocal(value: string, el: Element, env: CascadeEnv): string {
  let out = value
  let from = 0
  for (let guard = 0; guard < 50; guard++) {
    const at = out.indexOf('var(', from)
    if (at === -1) return out
    const end = closing(out, at + 3)
    if (end === -1) throw new Error(`unbalanced var() in ${JSON.stringify(out)}`)
    const inner = out.slice(at + 4, end)
    let depth = 0
    let comma = -1
    for (let i = 0; i < inner.length; i++) {
      if (inner[i] === '(') depth++
      else if (inner[i] === ')') depth--
      else if (inner[i] === ',' && depth === 0) {
        comma = i
        break
      }
    }
    const name = (comma === -1 ? inner : inner.slice(0, comma)).trim()
    const fallback = comma === -1 ? null : inner.slice(comma + 1).trim()
    if (TABLES.dark.has(name)) {
      from = end + 1
      continue
    }
    let found: string | null = null
    for (let node: Element | null = el; node !== null && found === null; node = node.parentElement) {
      found = cascade(STYLES, node, name, env).winner?.value ?? null
    }
    const replacement = found ?? fallback
    if (replacement === null) throw new Error(`${name} is declared nowhere above the element and has no fallback`)
    out = out.slice(0, at) + replacement + out.slice(end + 1)
    from = at
  }
  throw new Error(`var() expansion did not settle for ${JSON.stringify(value)}`)
}

/** The value the cascade chooses for `props` on `el`, with local vars expanded. */
export function painted(
  el: Element,
  props: string | readonly string[],
  env: CascadeEnv,
  pseudo: string | null = null,
): string | null {
  const r = cascade(STYLES, el, props, env, pseudo)
  if (r.unsupported.length > 0) throw new Error(`the resolver could not evaluate ${r.unsupported.join(', ')}`)
  const v = r.winner?.value ?? null
  return v === null ? null : expandLocal(v, el, env)
}

/** One `var(--token)` or literal colour, resolved for a theme. */
export function resolveColour(value: string, theme: Theme): RGBA {
  const v = resolveVars(value.trim(), TABLES[theme])
  const c = colour(v)
  if (c === null) throw new Error(`could not read ${JSON.stringify(value)} -> ${JSON.stringify(v)} as a colour`)
  return c
}

/** Every `:root` token a value names, resolved for a theme. */
export function tokensIn(value: string, theme: Theme): { token: string; rgba: RGBA }[] {
  return [...value.matchAll(/var\(\s*(--[\w-]+)\s*\)/g)].map((m) => ({
    token: m[1]!,
    rgba: resolveColour(`var(${m[1]!})`, theme),
  }))
}

/** Two colours a reader cannot tell apart. */
export function sameColour(a: RGBA, b: RGBA): boolean {
  return Math.abs(a.r - b.r) < 1 && Math.abs(a.g - b.g) < 1 && Math.abs(a.b - b.b) < 1 && Math.abs(a.a - b.a) < 0.01
}

/**
 * The state hue a value paints, or null. A value that names `--text-faint`
 * but resolves to the same pixel as `--ok` still counts: the question is what
 * the reader sees, not which name was typed.
 */
export function stateHueIn(value: string | null, theme: Theme): string | null {
  if (value === null) return null
  const states = STATE_TOKENS.map((t) => ({ t, c: resolveColour(`var(${t})`, theme) }))
  for (const { token, rgba } of tokensIn(value, theme)) {
    if ((STATE_TOKENS as readonly string[]).includes(token)) return token
    const hit = states.find((s) => sameColour(s.c, rgba))
    if (hit !== undefined) return `${token} (= ${hit.t})`
  }
  return null
}

/** Colour taken out of a declaration, leaving the geometry a greyscale reader keeps. */
export function stripColours(value: string): string {
  let out = value
  for (;;) {
    const at = out.search(/color-mix\(|var\(|rgba?\(/)
    if (at === -1) break
    const open = out.indexOf('(', at)
    const end = closing(out, open)
    if (end === -1) break
    out = out.slice(0, at) + out.slice(end + 1)
  }
  return out
    .replace(/#[0-9a-fA-F]{3,8}\b/g, '')
    .replace(/\b(currentColor|transparent)\b/gi, '')
    .replace(/\s*,\s*/g, ',')
    .replace(/\s+/g, ' ')
    .trim()
}

/** The declarations that make a mark's silhouette. `color` is deliberately absent. */
const SHAPE: ReadonlyArray<readonly string[]> = [
  ['width'],
  ['height'],
  ['border-radius'],
  ['clip-path'],
  ['transform'],
  ['border-width', 'border'],
  ['border-left-width', 'border-left', 'border-width', 'border'],
  ['background-image', 'background'],
  ['background-size', 'background'],
  ['box-shadow'],
]

/** The silhouette the cascade gives `el`, colours stripped. */
export function shapeOf(el: Element, env: CascadeEnv, pseudo: string | null = null): string {
  return SHAPE.map((props) => {
    const r = cascade(STYLES, el, props, env, pseudo)
    if (r.unsupported.length > 0) throw new Error(`the resolver could not evaluate ${r.unsupported.join(', ')}`)
    const w = r.winner
    return `${props[0]}=${w === null ? '' : stripColours(w.value)}`
  }).join('; ')
}
