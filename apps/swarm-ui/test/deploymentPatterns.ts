// What the live deployment's values are, read from the deployment pattern list.
//
// WHY THIS FILE EXISTS. The console names no company, no person and no real
// deployment (docs/design/generic-repository.md). A guard that spells the
// value it looks for is itself a finding, so the values live outside the tree,
// in the pattern list the deployment-values guard reads
// (scripts/guards/deployment_values.py): the text of `DEPLOYMENT_PATTERNS`, or
// the file `DEPLOYMENT_PATTERNS_FILE` names, the same way
// tests/unit/scripts/test_no_live_literals.py reads it. With no list,
// `listed()` returns null and each guard skips, saying why; the guard job in CI
// is the one that fails closed on a missing list.
//
// FORMAT (section 1.4): a first line `# sha256 <digest of the rest>`, then one
// entry per line, `<category>\t<kind>\t<value>`. A `literal` is matched
// case-insensitively and word-bounded, with `-`, `_` and `.` interchangeable;
// a `regex` is compiled as written (a leading `(?i)` becomes the `i` flag).
// An entry's id is `list:<line>`, the guard's own; a hit names the path, the
// line and that id, never the value.

import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

export const LIST_VARIABLE = 'DEPLOYMENT_PATTERNS'
export const LIST_FILE_VARIABLE = 'DEPLOYMENT_PATTERNS_FILE'
export const NO_LIST =
  'no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live'

export type Entry = { id: string; category: string; pattern: RegExp }

const CATEGORY = /^C(?:[1-9]|1[01])$/
const DIGEST_LINE = /^# sha256 ([0-9a-f]{64})$/
const SEPARATORS = '-_.'

const sha256 = (text: string): string => createHash('sha256').update(text, 'utf8').digest('hex')

function escape(ch: string): string {
  return ch.replace(/[\\^$.*+?()[\]{}|/-]/g, '\\$&')
}

/** Case-insensitive and word-bounded, with `-`, `_` and `.` interchangeable. */
export function literalPattern(value: string): RegExp {
  const body = [...value].map((ch) => (SEPARATORS.includes(ch) ? '[-_.]' : escape(ch))).join('')
  return new RegExp(`(?<![A-Za-z0-9])${body}(?![A-Za-z0-9])`, 'i')
}

function regexPattern(value: string, line: number): RegExp {
  const insensitive = value.startsWith('(?i)')
  try {
    return new RegExp(insensitive ? value.slice(4) : value, insensitive ? 'i' : '')
  } catch {
    throw new Error(`pattern list line ${line}: the regex does not compile`)
  }
}

/** The entries of a list's text; throws on a missing or wrong digest or a malformed line. */
export function parseList(text: string): Entry[] {
  const cut = text.indexOf('\n')
  const first = (cut < 0 ? text : text.slice(0, cut)).replace(/\r$/, '')
  const body = cut < 0 ? '' : text.slice(cut + 1)
  const digest = DIGEST_LINE.exec(first)?.[1]
  if (!digest) throw new Error("pattern list has no '# sha256 <digest>' first line")
  // A store that drops the final newline still holds the same list.
  if (digest !== sha256(body) && digest !== sha256(`${body}\n`)) {
    throw new Error(`pattern list digest mismatch: its first line says ${digest}, its content is ${sha256(body)}`)
  }
  const entries: Entry[] = []
  body.split('\n').forEach((raw, i) => {
    const n = i + 2
    const line = raw.replace(/\r$/, '')
    if (!line.trim() || line.startsWith('#')) return
    const tab1 = line.indexOf('\t')
    const tab2 = tab1 < 0 ? -1 : line.indexOf('\t', tab1 + 1)
    const [category, kind, value] = tab2 < 0 ? [] : [line.slice(0, tab1), line.slice(tab1 + 1, tab2), line.slice(tab2 + 1)]
    if (!category || !CATEGORY.test(category) || (kind !== 'literal' && kind !== 'regex') || !value) {
      throw new Error(
        `pattern list line ${n}: malformed (expected <category>\\t<kind>\\t<value>, category C1-C11, kind literal or regex)`,
      )
    }
    entries.push({ id: `list:${n}`, category, pattern: kind === 'literal' ? literalPattern(value) : regexPattern(value, n) })
  })
  return entries
}

/** The list's text from the environment, or null when none is given. */
export function listText(env: Record<string, string | undefined> = process.env): string | null {
  const path = env[LIST_FILE_VARIABLE]
  if (path) return readFileSync(path, 'utf8')
  const text = env[LIST_VARIABLE]
  return text && text.trim() ? text : null
}

/** The live entries, or null when no list is given. */
export function listed(env: Record<string, string | undefined> = process.env): Entry[] | null {
  const text = listText(env)
  return text === null ? null : parseList(text)
}

/** The ids of the entries that `text` matches: empty when it names nothing listed. */
export function listedIn(text: string, entries: Entry[] | null = LISTED): string[] {
  return (entries ?? []).filter((e) => e.pattern.test(text)).map((e) => e.id)
}

/** A list holding `values` as literals, under a correct digest: what a test plants. */
export function plantedList(values: string[], category = 'C9'): string {
  const body = values.map((v) => `${category}\tliteral\t${v}\n`).join('')
  return `# sha256 ${sha256(body)}\n${body}`
}

/** The entries read once from the environment for this run, or null. */
export const LISTED: Entry[] | null = listed()
