// THE DIFF VIEWER'S SYNTAX HIGHLIGHTER: a hand-written lexer, loaded lazily
// (owner decision 2026-10-08, design §3 question 3: "about +6 kB gz, lazily
// loaded, emitting React text nodes only").
//
// IT RETURNS TOKENS, NOT MARKUP. `tokenize` cuts a line into `{ k, s }` pieces
// whose `s` concatenate to the line exactly, character for character; DiffView
// draws each piece as a React text node, inside a `<span>` when it has a kind.
// Nothing here builds an HTML string, so nothing here can be handed to the DOM
// as one -- which is why Prism's `highlight()` and Shiki were ruled out and a
// lexer of about a hundred lines was written instead.
//
// ONE LINE AT A TIME, WITH NO STATE CARRIED BETWEEN LINES. A hunk starts in the
// middle of a file, so whether its first line is inside a block comment or a
// multi-line string is not knowable from the patch; a lexer that carried state
// would guess, and a guess that is wrong paints the rest of the hunk the wrong
// colour. Stateless, the worst case is one line of a multi-line comment drawn
// as code -- which is still its text, exactly as given.
//
// This module is a dynamic import (`useHighlighter.ts`): it is fetched the
// first time a file with a known language (`lang.ts`) is shown, and never on a
// page that shows no diff.

import type { Lang } from './lang'

export type TokenKind = 'kw' | 'str' | 'com' | 'num' | 'key'

/** A piece of a line. `k` null is plain text. The `s` of a line's tokens concatenate to the line. */
export interface Token {
  k: TokenKind | null
  s: string
}

interface Spec {
  line: readonly string[]
  block: readonly [string, string] | null
  quotes: string
  /** `#` starts a comment only at the start of the line or after whitespace (`a#b` in shell and YAML is text). */
  hashSpaced?: boolean
  /** Python's `'''` and `"""`, closed on the same line or running to its end. */
  triple?: boolean
  /** A string followed by `:` is a key (JSON). */
  keyStrings?: boolean
  /** A leading `name:` is a key (YAML). */
  keyLead?: boolean
  kw: ReadonlySet<string>
}

const words = (s: string): ReadonlySet<string> => new Set(s.split(' '))

const SLASHES = ['//']
const C_BLOCK = ['/*', '*/'] as const

const SPECS: Readonly<Record<Lang, Spec>> = {
  ts: {
    line: SLASHES,
    block: C_BLOCK,
    quotes: `'"\``,
    kw: words(
      'abstract as async await break case catch class const continue debugger declare default delete do else enum export extends false finally for from function get if implements import in instanceof interface keyof let namespace new null of private protected public readonly return satisfies set static super switch this throw true try type typeof undefined var void while yield',
    ),
  },
  go: {
    line: SLASHES,
    block: C_BLOCK,
    quotes: `'"\``,
    kw: words(
      'break case chan const continue default defer else fallthrough false for func go goto if import interface iota map nil package range return select struct switch true type var',
    ),
  },
  rust: {
    line: SLASHES,
    block: C_BLOCK,
    // `'` is a lifetime as often as a char, so it is not a quote here.
    quotes: '"',
    kw: words(
      'as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while',
    ),
  },
  c: {
    line: SLASHES,
    block: C_BLOCK,
    quotes: `'"`,
    kw: words(
      'abstract bool break case catch char class const continue default do double else enum extends false final finally float for fun if implements import int interface long new null nullptr package private protected public return short signed sizeof static struct super switch this throw throws true try typedef union unsigned val var void while',
    ),
  },
  py: {
    line: ['#'],
    block: null,
    quotes: `'"`,
    triple: true,
    kw: words(
      'False None True and as assert async await break case class continue def del elif else except finally for from global if import in is lambda match nonlocal not or pass raise return self try while with yield',
    ),
  },
  sh: {
    line: ['#'],
    block: null,
    quotes: `'"`,
    hashSpaced: true,
    kw: words(
      'case do done elif else esac exit export fi for function if in local readonly return set shift source then unset until while ADD ARG CMD COPY ENTRYPOINT ENV EXPOSE FROM LABEL RUN USER VOLUME WORKDIR',
    ),
  },
  yaml: {
    line: ['#'],
    block: null,
    quotes: `'"`,
    hashSpaced: true,
    keyLead: true,
    kw: words('true false null yes no on off'),
  },
  json: { line: SLASHES, block: C_BLOCK, quotes: '"', keyStrings: true, kw: words('true false null') },
  css: { line: [], block: C_BLOCK, quotes: `'"`, kw: words('important') },
  hcl: {
    line: ['#', '//'],
    block: C_BLOCK,
    quotes: '"',
    kw: words('count data each false for if in local locals module null output provider resource terraform true var variable'),
  },
}

const IDENT_START = /[A-Za-z_$]/
const IDENT = /[\w$]/
const NUMBER = /^(?:0[xXbBoO][\da-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)/
const YAML_KEY = /^(\s*(?:-\s+)?)([^\s#:'"][^#:]*?|"[^"]*"|'[^']*')(\s*:)(?=\s|$)/

/** The end of a string opened at `at` with `q`: the index after its closing quote, or the line's length. */
function stringEnd(text: string, at: number, q: string): number {
  let i = at + q.length
  while (i < text.length) {
    if (q.length === 1 && text[i] === '\\') {
      i += 2
      continue
    }
    if (text.startsWith(q, i)) return i + q.length
    i += 1
  }
  return text.length
}

/** Cut one line into tokens. Their `s` joined is `text`, always. */
export function tokenize(text: string, lang: Lang): Token[] {
  const spec = SPECS[lang]
  const out: Token[] = []
  const push = (k: TokenKind | null, s: string): void => {
    if (s === '') return
    const last = out[out.length - 1]
    if (last !== undefined && last.k === k) last.s += s
    else out.push({ k, s })
  }
  let i = 0
  if (spec.keyLead) {
    const m = YAML_KEY.exec(text)
    if (m) {
      push(null, m[1]!)
      push('key', m[2]!)
      push(null, m[3]!)
      i = m[0].length
    }
  }
  while (i < text.length) {
    const c = text[i]!
    const lineComment = spec.line.find(
      (p) => text.startsWith(p, i) && (!(spec.hashSpaced && p === '#') || i === 0 || /\s/.test(text[i - 1]!)),
    )
    if (lineComment !== undefined) {
      push('com', text.slice(i))
      break
    }
    if (spec.block !== null && text.startsWith(spec.block[0], i)) {
      const close = text.indexOf(spec.block[1], i + spec.block[0].length)
      const end = close === -1 ? text.length : close + spec.block[1].length
      push('com', text.slice(i, end))
      i = end
      continue
    }
    if (spec.quotes.includes(c)) {
      const q = spec.triple && text.startsWith(c.repeat(3), i) ? c.repeat(3) : c
      const end = stringEnd(text, i, q)
      const isKey = spec.keyStrings === true && /^\s*:/.test(text.slice(end))
      push(isKey ? 'key' : 'str', text.slice(i, end))
      i = end
      continue
    }
    const prev = i === 0 ? '' : text[i - 1]!
    if (/\d/.test(c) && !IDENT.test(prev)) {
      const m = NUMBER.exec(text.slice(i))
      if (m) {
        push('num', m[0])
        i += m[0].length
        continue
      }
    }
    if (IDENT_START.test(c) && !IDENT.test(prev)) {
      let j = i + 1
      while (j < text.length && IDENT.test(text[j]!)) j += 1
      const word = text.slice(i, j)
      push(spec.kw.has(word) ? 'kw' : null, word)
      i = j
      continue
    }
    push(null, c)
    i += 1
  }
  return out
}
