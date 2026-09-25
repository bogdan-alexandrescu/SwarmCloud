// THE DUPLICATE-SELECTOR GATE, AND THE PARSE CHECK IT CANNOT RUN WITHOUT.
//
// WHY A SECOND CSS READER, when jsdom already parses the sheet for every
// `getComputedStyle` in this suite. Because jsdom's parser is the one that let
// the defect this file was written for through. `§B6 — THE DENSE AND OPERATOR
// SCREENS` lost its opening `/*`, which turned a 17-line comment into the
// PRELUDE of the `@media (max-width: 899px)` block below it. A browser reads
// that prelude as one qualified rule whose selector is prose -- invalid -- and
// drops the whole block, so the stacked-table layout for every screen under
// 900px never reached a phone. jsdom's CSSOM sees `@media` inside the garbage,
// discards the text before it and parses the block anyway. A gate built on the
// same parser as the tests it guards inherits the tests' blind spot; this one
// follows the CSS Syntax spec's rule for a qualified rule instead: EVERYTHING
// from the end of the previous rule to the next `{` is the prelude.
//
// WHAT IT REPORTS, and each is a defect this sheet has actually shipped:
//
//   problems     a rule whose prelude is not a selector (an orphan `*/`, an
//                at-keyword or a `;` inside it), a rule with a block nested in
//                it (this sheet uses no CSS nesting, so a nested `{` is a rule
//                that swallowed its neighbour), and a stray or missing brace.
//   duplicates   one selector list declared twice NON-ADJACENTLY in the same
//                context. `.filters` was two unrelated rule families 1,300
//                lines apart that merged in the cascade; `.hold-top` said
//                `align-items: start` twice, 240 lines apart, each with its own
//                paragraph arguing for it. An ADJACENT split is allowed: two
//                consecutive rules for one selector are one rule written in two
//                pieces, and the reader of one is looking at the other.
//   rootTokens   one custom property declared in two `:root` blocks of the
//                same context. The sheet keeps five top-level `:root` blocks on
//                purpose -- each wave added names beside the rules that use
//                them -- so `:root` itself is exempt from `duplicates`; what is
//                not allowed is the collision that makes that pattern
//                dangerous. `--measure` was 78ch in the first block and 74ch in
//                the third, with a comment beside each defending its number.
//   keyframes    one `@keyframes` name declared twice anywhere. The document has
//                ONE namespace for them, so the later silently replaces the
//                earlier for every rule that names it: the loading skeleton
//                flashed 1 -> .35 because the liveness dot's `pulse` was
//                declared 2,500 lines after the skeleton's.
//   animations   an `animation` that names a `@keyframes` the sheet does not
//                declare, which is the same fault from the other end.
//
// Everything above `cascade` is pure string work over the source. It reads
// comments as comments and strings as strings, and nothing else about CSS is
// modelled.
//
// `cascade`, AT THE FOOT OF THIS FILE, IS THE ONE EXCEPTION, AND IT EXISTS
// BECAUSE jsdom'S CASCADE CANNOT ANSWER THE QUESTIONS THE 2026-09-25 QA PASS
// ASKED. jsdom orders matching rules by SOURCE POSITION ALONE -- its own
// source says "specificity is only implemented by the order in which the
// matching rules appear" -- and it applies no `@media` block that does not
// name `screen`. Four of that pass's defects were exactly those two things:
// a phone rule placed above the base rule it meant to beat (`.ctl-seg`), a
// `.state p` out-ranking `.checked-at` on font-size, `.limit-edit input`
// out-ranking `.acct-wide` on width, and `.ctl-table .is-num` out-ranking a
// stacked key's alignment. `getComputedStyle` reports the WRONG winner for
// every one of them, so a test built on it would pass on the broken sheet.
// `cascade` weighs importance, specificity and order, evaluates the media and
// container conditions against a stated width, and uses jsdom only for what
// jsdom does correctly: `Element.matches`.

export type GateNode =
  | { kind: 'rule'; prelude: string; body: string; line: number; nested: boolean }
  | { kind: 'group'; prelude: string; line: number; children: GateNode[] }
  | { kind: 'opaque'; prelude: string; line: number; body: string }
  | { kind: 'statement'; prelude: string; line: number }

export interface GateReport {
  problems: string[]
  duplicates: string[]
  rootTokens: string[]
  keyframes: string[]
  animations: string[]
}

/** At-rules whose block holds RULES, and so opens a new context. */
const GROUPING = new Set(['media', 'supports', 'container', 'layer', 'document', 'scope'])

/**
 * Comments replaced by spaces, newlines kept so line numbers survive.
 *
 * A comment opener inside a string is not a comment, and a string ends at an
 * unescaped newline exactly as the CSS tokenizer ends a bad-string there -- so
 * a stray apostrophe in prose that escaped its comment cannot swallow the rest
 * of the sheet here any more than it does in a browser.
 */
export function blankComments(css: string): string {
  let out = ''
  let quote: string | null = null
  for (let i = 0; i < css.length; i++) {
    const c = css[i]!
    if (quote !== null) {
      out += c
      if (c === '\\' && i + 1 < css.length) {
        out += css[i + 1]!
        i++
      } else if (c === quote || c === '\n') {
        quote = null
      }
      continue
    }
    if (c === '"' || c === "'") {
      quote = c
      out += c
      continue
    }
    if (c === '/' && css[i + 1] === '*') {
      const end = css.indexOf('*/', i + 2)
      const stop = end === -1 ? css.length : end + 2
      out += css.slice(i, stop).replace(/[^\n]/g, ' ')
      i = stop - 1
      continue
    }
    out += c
  }
  return out
}

/** The index of the `}` that closes the `{` at `open`, strings respected. */
function closeBrace(s: string, open: number): number {
  let depth = 0
  let quote: string | null = null
  for (let i = open; i < s.length; i++) {
    const c = s[i]!
    if (quote !== null) {
      if (c === '\\') i++
      else if (c === quote || c === '\n') quote = null
      continue
    }
    if (c === '"' || c === "'") quote = c
    else if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return i
    }
  }
  return -1
}

/** A line-number lookup for one text, built once: the sheet is 8,000 lines
 *  and a linear count per rule would be quadratic over a thousand rules. */
function lineIndex(s: string): (index: number) => number {
  const breaks: number[] = []
  for (let i = 0; i < s.length; i++) if (s[i] === '\n') breaks.push(i)
  return (index) => {
    let lo = 0
    let hi = breaks.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (breaks[mid]! < index) lo = mid + 1
      else hi = mid
    }
    return lo + 1
  }
}

function squash(s: string): string {
  return s.replace(/\s+/g, ' ').trim()
}

/**
 * The sheet as a tree: rules, grouping at-rules with their children, and
 * opaque at-rules (`@keyframes`, `@font-face`) whose block is not rules.
 */
export function parseSheet(source: string): { nodes: GateNode[]; problems: string[] } {
  const s = blankComments(source)
  const problems: string[] = []
  const lineOf = lineIndex(s)

  const parseList = (from: number, to: number): GateNode[] => {
    const nodes: GateNode[] = []
    let start = from
    let quote: string | null = null
    for (let i = from; i < to; i++) {
      const c = s[i]!
      if (quote !== null) {
        if (c === '\\') i++
        else if (c === quote || c === '\n') quote = null
        continue
      }
      if (c === '"' || c === "'") {
        quote = c
        continue
      }
      if (c === ';') {
        const prelude = squash(s.slice(start, i))
        // A statement at-rule (`@import`, `@charset`) ends at `;`. Anywhere
        // else a `;` belongs to the prelude it sits in -- which is how the
        // browser reads it, and how the broken comment's "draw a box;" became
        // part of the @media block's selector.
        if (prelude.startsWith('@')) {
          nodes.push({ kind: 'statement', prelude, line: lineOf(start) })
          start = i + 1
        }
        continue
      }
      if (c === '}') {
        problems.push(`line ${lineOf(i)}: a \`}\` closes nothing`)
        start = i + 1
        continue
      }
      if (c !== '{') continue

      const raw = s.slice(start, i)
      const prelude = squash(raw)
      const line = lineOf(start + (raw.length - raw.trimStart().length))
      const close = closeBrace(s, i)
      if (close === -1 || close >= to) {
        problems.push(`line ${line}: \`${prelude.slice(0, 60)}\` opens a block that is never closed`)
        return nodes
      }
      const body = s.slice(i + 1, close)
      const at = /^@(-webkit-)?([\w-]+)/.exec(prelude)
      if (at !== null) {
        if (prelude.includes('*/')) problems.push(`line ${line}: an orphan \`*/\` in \`${prelude.slice(0, 80)}\``)
        if (GROUPING.has(at[2]!)) {
          nodes.push({ kind: 'group', prelude, line, children: parseList(i + 1, close) })
        } else {
          nodes.push({ kind: 'opaque', prelude, line, body })
        }
      } else {
        const nested = body.includes('{')
        if (prelude.includes('*/') || prelude.includes('@') || prelude.includes(';') || prelude === '') {
          problems.push(
            `line ${line}: a rule whose selector is not a selector -- ` +
              `\`${prelude.slice(0, 100)}${prelude.length > 100 ? '…' : ''}\`. ` +
              'A browser drops this whole block; a comment has probably lost its `/*`.',
          )
        } else if (nested) {
          problems.push(`line ${line}: \`${prelude}\` contains a nested block; this sheet uses no CSS nesting`)
        }
        nodes.push({ kind: 'rule', prelude, body, line, nested })
      }
      i = close
      start = close + 1
    }
    const tail = squash(s.slice(start, to))
    if (tail !== '') problems.push(`line ${lineOf(start)}: text outside any rule -- \`${tail.slice(0, 60)}\``)
    return nodes
  }

  return { nodes: parseList(0, s.length), problems }
}

/** One selector list, written the same way however it was wrapped. */
function selectorKey(prelude: string): string {
  const parts: string[] = []
  let depth = 0
  let from = 0
  for (let i = 0; i < prelude.length; i++) {
    const c = prelude[i]
    if (c === '(' || c === '[') depth++
    else if (c === ')' || c === ']') depth--
    else if (c === ',' && depth === 0) {
      parts.push(squash(prelude.slice(from, i)))
      from = i + 1
    }
  }
  parts.push(squash(prelude.slice(from)))
  return parts.join(', ')
}

function isRoot(prelude: string): boolean {
  return prelude.startsWith(':root')
}

/** `--name` declarations in a rule body. */
function customProperties(body: string): string[] {
  return [...body.matchAll(/(?:^|[;{\s])(--[A-Za-z0-9_-]+)\s*:/g)].map((m) => m[1]!)
}

const ANIMATION_KEYWORDS = new Set([
  'none', 'infinite', 'normal', 'reverse', 'alternate', 'alternate-reverse',
  'forwards', 'backwards', 'both', 'running', 'paused',
  'linear', 'ease', 'ease-in', 'ease-out', 'ease-in-out', 'step-start', 'step-end',
  'initial', 'inherit', 'unset', 'revert',
])

/** The `@keyframes` names an `animation` / `animation-name` value refers to. */
function animationNames(value: string): string[] {
  const names: string[] = []
  const flat = value.replace(/!\s*important/g, ' ').replace(/[\w-]+\([^()]*\)/g, ' ')
  for (const layer of flat.split(',')) {
    for (const token of layer.trim().split(/\s+/)) {
      if (token === '' || ANIMATION_KEYWORDS.has(token)) continue
      if (/^-?[\d.]+(m?s)?$/.test(token)) continue
      names.push(token)
    }
  }
  return names
}

/** Run every check over one sheet. `label` prefixes each finding. */
export function gate(source: string, label = 'sheet'): GateReport {
  const { nodes, problems } = parseSheet(source)
  const report: GateReport = {
    problems: problems.map((p) => `${label} ${p}`),
    duplicates: [],
    rootTokens: [],
    keyframes: [],
    animations: [],
  }
  const frames = new Map<string, number[]>()
  const animations: { name: string; line: number }[] = []

  // KEYED BY CONDITION, NOT BY BLOCK. Two `@media (max-width: 560px)` blocks
  // are one context to the cascade, and design-system.md §12.6 records the
  // sheet carrying exactly that: two such blocks, ~360 lines apart, both
  // styling `.row`, silently costing a column. So a rule is compared with every
  // rule under the same chain of conditions, in whichever block it sits; two
  // rules are ADJACENT only when they are neighbours in the same block.
  type Seen = { block: number; position: number; line: number }
  const seen = new Map<string, Seen[]>()
  const tokens = new Map<string, number[]>()
  let blocks = 0

  const walk = (list: readonly GateNode[], context: string): void => {
    const block = blocks++
    list.forEach((node, position) => {
      if (node.kind === 'group') {
        walk(node.children, `${context}${node.prelude} > `)
        return
      }
      if (node.kind === 'opaque') {
        const m = /^@(?:-webkit-)?keyframes\s+(\S+)/.exec(node.prelude)
        if (m) frames.set(m[1]!, [...(frames.get(m[1]!) ?? []), node.line])
        return
      }
      if (node.kind !== 'rule') return
      for (const m of node.body.matchAll(/(?:^|[;\s])animation(?:-name)?\s*:\s*([^;]+)/g)) {
        for (const name of animationNames(m[1]!)) animations.push({ name, line: node.line })
      }
      if (isRoot(node.prelude)) {
        for (const prop of customProperties(node.body)) {
          const key = `${context}\`${node.prelude} ${prop}\``
          tokens.set(key, [...(tokens.get(key) ?? []), node.line])
        }
        return
      }
      const key = `${context}\`${selectorKey(node.prelude)}\``
      seen.set(key, [...(seen.get(key) ?? []), { block, position, line: node.line }])
    })
  }
  walk(nodes, '')

  for (const [key, at] of seen) {
    if (at.length < 2) continue
    const apart = at.some((a, i) => {
      const prev = at[i - 1]
      return prev !== undefined && (a.block !== prev.block || a.position - prev.position > 1)
    })
    if (apart) report.duplicates.push(`${label} ${key} is declared at lines ${at.map((a) => a.line).join(', ')}`)
  }
  for (const [key, lines] of tokens) {
    if (lines.length > 1) report.rootTokens.push(`${label} ${key} is declared at lines ${lines.join(', ')}`)
  }

  for (const [name, lines] of frames) {
    if (lines.length > 1) report.keyframes.push(`${label} @keyframes ${name} is declared at lines ${lines.join(', ')}`)
  }
  for (const a of animations) {
    if (!frames.has(a.name)) {
      report.animations.push(`${label} line ${a.line}: animation names \`${a.name}\`, which no @keyframes declares`)
    }
  }
  return report
}

// ---------------------------------------------------------------------------
// The cascade, for the questions jsdom answers wrongly
// ---------------------------------------------------------------------------

/** What a rule is evaluated against. Nothing here is guessed: an at-rule or a
 *  media feature this cannot evaluate THROWS, so an unanswerable question is
 *  loud rather than read as "no rule applies". */
export interface CascadeEnv {
  /** The viewport width a `@media (min|max-width)` is evaluated against, px. */
  width: number
  /** `prefers-color-scheme`. `:root` carries the dark palette, so dark is the default. */
  theme?: 'dark' | 'light'
  /** The inline size of the nearest size container, px, or null for none. An
   *  element with no container matches no `@container` rule, as in a browser. */
  container?: number | null
  /** Dynamic pseudo-classes to treat as active ('hover', 'focus-visible', …).
   *  Any other dynamic pseudo-class in a selector makes that branch not match. */
  states?: readonly string[]
  /** `prefers-reduced-motion: reduce`. */
  reducedMotion?: boolean
}

export interface Declared {
  property: string
  value: string
  important: boolean
  /** The selector BRANCH that matched, not the whole list. */
  selector: string
  line: number
  /** The at-rule preludes around the rule, outermost first. */
  conditions: string[]
  specificity: [number, number, number]
}

export interface CascadeResult {
  /** The declaration the cascade chose, or null when no rule declares it. */
  winner: Declared | null
  /** Every matching declaration, lowest priority first. */
  matched: Declared[]
  /** Selector branches the engine would not evaluate. Never silently skipped. */
  unsupported: string[]
}

/** Split on `sep` where it is not inside parentheses, brackets or a string. */
export function splitTop(text: string, sep = ','): string[] {
  const out: string[] = []
  let depth = 0
  let quote: string | null = null
  let from = 0
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!
    if (quote !== null) {
      if (c === '\\') i++
      else if (c === quote) quote = null
      continue
    }
    if (c === '"' || c === "'") quote = c
    else if (c === '(' || c === '[') depth++
    else if (c === ')' || c === ']') depth--
    else if (c === sep && depth === 0) {
      out.push(text.slice(from, i))
      from = i + 1
    }
  }
  out.push(text.slice(from))
  return out.map((s) => s.trim()).filter((s) => s !== '')
}

/** The declarations of one rule body, in order, `!important` split off. */
export function declarations(body: string): { property: string; value: string; important: boolean }[] {
  const out: { property: string; value: string; important: boolean }[] = []
  for (const chunk of splitTop(body, ';')) {
    const colon = chunk.indexOf(':')
    if (colon === -1) continue
    const property = chunk.slice(0, colon).trim().toLowerCase()
    let value = chunk.slice(colon + 1).trim()
    const imp = /!\s*important\s*$/i.exec(value)
    if (imp) value = value.slice(0, imp.index).trim()
    if (property === '' || value === '') continue
    out.push({ property, value: squash(value), important: imp !== null })
  }
  return out
}

function identEnd(s: string, from: number): number {
  let i = from
  while (i < s.length && /[\w-]/.test(s[i]!)) i++
  return i
}

function matchingClose(s: string, open: number, o: string, c: string): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    if (s[i] === o) depth++
    else if (s[i] === c) {
      depth--
      if (depth === 0) return i
    }
  }
  return s.length - 1
}

/**
 * Selectors Level 4 specificity of ONE complex selector, as (ids, classes,
 * types). `:where()` is zero; `:is()`, `:not()` and `:has()` take their most
 * specific argument; a pseudo-element counts as a type. That is the whole of
 * what this sheet uses (stylesheet.gate.test.ts checks it against fixtures).
 */
export function specificity(selector: string): [number, number, number] {
  let a = 0
  let b = 0
  let c = 0
  const s = selector
  let i = 0
  while (i < s.length) {
    const ch = s[i]!
    if (ch === '#') {
      a++
      i = identEnd(s, i + 1)
    } else if (ch === '.') {
      b++
      i = identEnd(s, i + 1)
    } else if (ch === '[') {
      b++
      i = matchingClose(s, i, '[', ']') + 1
    } else if (ch === ':') {
      if (s[i + 1] === ':') {
        c++
        i = identEnd(s, i + 2)
        if (s[i] === '(') i = matchingClose(s, i, '(', ')') + 1
        continue
      }
      const end = identEnd(s, i + 1)
      const name = s.slice(i + 1, end).toLowerCase()
      if (s[end] === '(') {
        const close = matchingClose(s, end, '(', ')')
        const inner = s.slice(end + 1, close)
        if (name === 'is' || name === 'not' || name === 'has' || name === 'matches') {
          let best: [number, number, number] = [0, 0, 0]
          for (const arg of splitTop(inner)) {
            const sp = specificity(arg)
            if (sp[0] > best[0] || (sp[0] === best[0] && (sp[1] > best[1] || (sp[1] === best[1] && sp[2] > best[2])))) best = sp
          }
          a += best[0]
          b += best[1]
          c += best[2]
        } else if (name !== 'where') {
          b++
        }
        i = close + 1
      } else {
        if (['before', 'after', 'first-line', 'first-letter'].includes(name)) c++
        else b++
        i = end
      }
    } else if (/[a-zA-Z_]/.test(ch)) {
      c++
      i = identEnd(s, i)
    } else {
      i++
    }
  }
  return [a, b, c]
}

const DYNAMIC = /:(hover|focus-visible|focus-within|focus|active|visited)(?![\w-])/g
const PSEUDO_ELEMENT = /::([\w-]+)(\([^)]*\))?\s*$/

function mediaFeature(feature: string, env: CascadeEnv, width: number): boolean {
  const f = feature.trim()
  if (f === 'screen' || f === 'all') return true
  if (f === 'print') return false
  const m = /^\(\s*([\w-]+)\s*(?::\s*([^)]+?))?\s*\)$/.exec(f)
  if (m === null) throw new Error(`cascade cannot evaluate the media feature \`${f}\``)
  const name = m[1]!
  const value = (m[2] ?? '').trim()
  const px = (): number => {
    const n = /^([\d.]+)px$/.exec(value)
    if (n === null) throw new Error(`cascade cannot evaluate \`${f}\`: only px widths are modelled`)
    return Number(n[1])
  }
  switch (name) {
    case 'min-width':
      return width >= px()
    case 'max-width':
      return width <= px()
    case 'prefers-color-scheme':
      return value === (env.theme ?? 'dark')
    case 'prefers-reduced-motion':
      return (value === 'reduce') === (env.reducedMotion ?? false)
    case 'hover':
      return value === 'hover'
    case 'pointer':
      return value === 'fine'
    default:
      throw new Error(`cascade cannot evaluate the media feature \`${f}\``)
  }
}

function queryList(list: string, env: CascadeEnv, width: number): boolean {
  return splitTop(list).some((query) => {
    let q = query.trim()
    let negate = false
    if (/^not\s/.test(q)) {
      negate = true
      q = q.slice(4).trim()
    }
    q = q.replace(/^only\s+/, '')
    const holds = q.split(/\s+and\s+/).every((part) => mediaFeature(part, env, width))
    return negate ? !holds : holds
  })
}

/** Whether a grouping at-rule's condition holds in `env`. */
export function conditionHolds(prelude: string, env: CascadeEnv): boolean {
  const at = /^@(?:-webkit-)?([\w-]+)\s*([\s\S]*)$/.exec(prelude)
  if (at === null) throw new Error(`cascade cannot read the at-rule \`${prelude}\``)
  const kind = at[1]!
  const rest = at[2]!.trim()
  if (kind === 'media') return queryList(rest, env, env.width)
  if (kind === 'container') {
    const size = env.container ?? null
    if (size === null) return false
    // A leading container NAME is dropped: this models one container at a
    // time, the one `env.container` measures.
    return queryList(rest.replace(/^[a-zA-Z][\w-]*\s+(?=\()/, ''), env, size)
  }
  if (kind === 'supports' || kind === 'layer') return true
  throw new Error(`cascade cannot evaluate \`${prelude}\``)
}

/** One style rule, flattened out of its at-rules. `body` has its comments blanked. */
export type FlatRule = { selector: string; body: string; line: number; conditions: string[]; order: number }

const FLAT = new Map<string, FlatRule[]>()

/** Every style rule with the chain of at-rules around it, in source order. */
export function flatRules(source: string): FlatRule[] {
  const hit = FLAT.get(source)
  if (hit !== undefined) return hit
  const out: FlatRule[] = []
  const walk = (nodes: readonly GateNode[], conditions: string[]): void => {
    for (const n of nodes) {
      if (n.kind === 'group') walk(n.children, [...conditions, n.prelude])
      else if (n.kind === 'rule') {
        out.push({ selector: n.prelude, body: n.body, line: n.line, conditions, order: out.length })
      }
    }
  }
  walk(parseSheet(source).nodes, [])
  FLAT.set(source, out)
  return out
}

/**
 * The declaration a browser would use for `property` on `el` (or on its
 * `pseudo`-element), with the shipped sheet, at `env`.
 *
 * `property` may be a list, because a shorthand and its longhands compete for
 * one value: ask for `['font-size', 'font']` and whichever declaration wins is
 * the one that sets the size. Reading the value out of a shorthand is the
 * caller's business, because only the caller knows which part it is about.
 *
 * NOT MODELLED, and each is a reason a test must not lean on this for it:
 * inheritance (a property no rule declares on the element resolves to
 * `winner: null`, and the test walks up itself), cascade layers, `revert`,
 * and shadow trees. The order is importance, then specificity, then source
 * order -- author origin only, which is the only origin this sheet is in.
 */
export function cascade(
  source: string,
  el: Element,
  property: string | readonly string[],
  env: CascadeEnv,
  pseudo: string | null = null,
): CascadeResult {
  const props = new Set(typeof property === 'string' ? [property] : property)
  const states = new Set(env.states ?? [])
  const matched: (Declared & { order: number; index: number })[] = []
  const unsupported: string[] = []

  for (const rule of flatRules(source)) {
    if (!rule.conditions.every((c) => conditionHolds(c, env))) continue
    const decls = declarations(rule.body).filter((d) => props.has(d.property))
    if (decls.length === 0) continue

    let best: { branch: string; spec: [number, number, number] } | null = null
    for (const branch of splitTop(rule.selector)) {
      const pe = PSEUDO_ELEMENT.exec(branch)
      if ((pe?.[1] ?? null) !== pseudo) continue
      let base = pe === null ? branch : branch.slice(0, pe.index)
      let inactive = false
      base = base.replace(DYNAMIC, (_m, name: string) => {
        if (!states.has(name)) inactive = true
        return ''
      })
      if (inactive) continue
      base = base.trim() === '' ? '*' : base.trim()
      let hit: boolean
      try {
        hit = el.matches(base)
      } catch {
        if (!unsupported.includes(branch)) unsupported.push(branch)
        continue
      }
      if (!hit) continue
      const spec = specificity(branch)
      if (
        best === null ||
        spec[0] > best.spec[0] ||
        (spec[0] === best.spec[0] && (spec[1] > best.spec[1] || (spec[1] === best.spec[1] && spec[2] > best.spec[2])))
      ) {
        best = { branch, spec }
      }
    }
    if (best === null) continue
    const chosen = best
    decls.forEach((d, index) =>
      matched.push({
        ...d,
        selector: chosen.branch,
        line: rule.line,
        conditions: rule.conditions,
        specificity: chosen.spec,
        order: rule.order,
        index,
      }),
    )
  }

  matched.sort(
    (x, y) =>
      Number(x.important) - Number(y.important) ||
      x.specificity[0] - y.specificity[0] ||
      x.specificity[1] - y.specificity[1] ||
      x.specificity[2] - y.specificity[2] ||
      x.order - y.order ||
      x.index - y.index,
  )
  const clean: Declared[] = matched.map((d) => ({
    property: d.property,
    value: d.value,
    important: d.important,
    selector: d.selector,
    line: d.line,
    conditions: d.conditions,
    specificity: d.specificity,
  }))
  return { winner: clean[clean.length - 1] ?? null, matched: clean, unsupported }
}
