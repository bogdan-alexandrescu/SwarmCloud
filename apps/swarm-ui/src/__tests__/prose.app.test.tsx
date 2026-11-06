// THE PROSE TABLE ITSELF: every page has a row, and no ceiling ever rises.
//
// The slices (prose.app.<group>.test.tsx) hold each page to its row; this file
// holds the rows. A ceiling may only go DOWN (BOGDAN'S RULE, 2026-11-05), so
// two things are checked, and each catches what the other cannot:
//
//   THE HISTORY IN THE FILE. Every row keeps every ceiling it has had, oldest
//   first, and the history must not rise. This is what CI sees: the UI job
//   checks out one commit, so it has no previous table to read.
//   THE PREVIOUS COMMIT'S TABLE, read with git where the history is there (a
//   working tree, or a checkout deeper than one commit). A row's history must
//   start with the one that commit had, so an edit that rewrites a number
//   instead of appending a lower one is refused, as is a raise in place.

import { execFileSync } from 'node:child_process'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { measure, newWords, PAGES, pieces, TABLE, type CeilingRow } from './proseapp'
import WORDS from './prose.words.json'

const HERE = dirname(fileURLToPath(import.meta.url))

/** The table as `ref` had it, or null where git has no such commit or the file was not there yet. */
function tableAt(ref: string): Record<string, CeilingRow> | null {
  try {
    const raw = execFileSync('git', ['show', `${ref}:./prose.ceilings.json`], {
      cwd: HERE,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
    return (JSON.parse(raw) as { pages: Record<string, CeilingRow> }).pages
  } catch {
    return null
  }
}

function rising(history: readonly number[]): boolean {
  return history.some((n, i) => i > 0 && n > (history[i - 1] ?? n))
}

describe('the prose ceilings table', () => {
  it('has a row for every page the nav reaches and the named extra pages, and no other', () => {
    expect(Object.keys(TABLE).sort()).toEqual(PAGES.map((p) => p.key).sort())
    expect(Object.keys(WORDS.pages).sort()).toEqual(PAGES.map((p) => p.key).sort())
  })

  it('keeps every row a non-empty history of whole numbers', () => {
    for (const [key, row] of Object.entries(TABLE)) {
      for (const h of [row.words, row.arrows]) {
        expect(h.length, `${key} has an empty history`).toBeGreaterThan(0)
        expect(h.every((n) => Number.isInteger(n) && n >= 0), `${key}: ${JSON.stringify(h)}`).toBe(true)
      }
    }
  })

  it('never lets a ceiling rise within its history', () => {
    const up = Object.entries(TABLE)
      .flatMap(([key, row]) => [
        ...(rising(row.words) ? [`${key} words ${JSON.stringify(row.words)}`] : []),
        ...(rising(row.arrows) ? [`${key} arrows ${JSON.stringify(row.arrows)}`] : []),
      ])
    expect(up, 'a ceiling may only go down: append a lower number').toEqual([])
  })

  it("only appends to the previous commit's table", () => {
    // HEAD for an uncommitted edit; HEAD~1 for the commit that made it.
    const refs = ['HEAD', 'HEAD~1']
    const read = refs.map((ref) => [ref, tableAt(ref)] as const)
    console.log(
      `PROSE TABLE COMPARED WITH: ${read.map(([ref, t]) => `${ref} (${t === null ? 'not there' : `${Object.keys(t).length} rows`})`).join(', ')}`,
    )
    const bad: string[] = []
    for (const [ref, old] of read) {
      if (old === null) continue
      for (const [key, before] of Object.entries(old)) {
        const now = TABLE[key]
        if (now === undefined) continue
        for (const k of ['words', 'arrows'] as const) {
          const kept = before[k].every((n, i) => now[k][i] === n)
          if (!kept || now[k].length < before[k].length) {
            bad.push(`${key} ${k}: ${ref} had ${JSON.stringify(before[k])}, now ${JSON.stringify(now[k])}`)
          }
        }
      }
    }
    expect(bad, 'a ceiling is lowered by appending to its history, never by editing it').toEqual([])
  })
})

describe('what the prose meter counts', () => {
  it('counts visible words, not the help copy, the stylesheet or the script', () => {
    // Sibling elements' text runs together ("Poolstwo"), as prose.operator
    // explains: data costs little, a sentence a token per word. Spaced here.
    const root = document.createElement('div')
    root.innerHTML =
      '<h1>Pools</h1> <p>two words</p>' +
      '<span data-help-description>an explanation nobody sees until they ask</span>' +
      '<style>.a { color: red }</style><script>var x = 1</script>'
    expect(measure(root).words).toBe(3)
  })

  it("does not count the content a page draws, a stage's lines of code", () => {
    const root = document.createElement('div')
    root.innerHTML = '<h1>Talk</h1> <code data-prose-content>export function StepFilters({ steps }) {</code>'
    expect(measure(root).words).toBe(1)
  })

  it('counts a text link with an arrow as an action that should be a button, and a button not', () => {
    const root = document.createElement('div')
    root.innerHTML =
      '<a href="#a">All workflows →</a><a href="#b">Open › setup</a><a href="#c">plain</a>' +
      '<span role="link">Next →</span><button>Retry →</button>'
    const m = measure(root)
    expect(m.arrows).toBe(3)
    expect(m.arrowLinks).toEqual(['All workflows →', 'Open › setup', 'Next →'])
  })

  it('names the words a page added since its ceiling, once per extra occurrence', () => {
    expect(newWords(['a', 'b', 'b', 'c', 'd'], ['b', 'a', 'c'])).toEqual(['b', 'd'])
    expect(newWords(['a'], ['a', 'a'])).toEqual([])
  })

  it('cuts a run of two elements at the capital, and keeps a word whole', () => {
    expect(pieces(['usageAgainst', 'GitHub', 'ok', 'Profile?'])).toEqual(['usage', 'Against', 'Git', 'Hub', 'ok', 'Profile?'])
  })
})
