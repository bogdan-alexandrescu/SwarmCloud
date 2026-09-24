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
// Everything here is pure string work over the source. It reads comments as
// comments and strings as strings, and nothing else about CSS is modelled.

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
