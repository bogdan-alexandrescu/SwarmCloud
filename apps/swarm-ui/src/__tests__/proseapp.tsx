// THE APP-WIDE PROSE RATCHET: every page the console's nav reaches, one ceiling
// each (BOGDAN'S RULE, 2026-11-05, standing, every console page: no
// explanatory prose on the surface; docs/web-ui/design-system.md).
//
// The four screen files (prose.budget, prose.budget.capacity, prose.operator,
// prose.runs) each mount one screen on hand-built mocks. This walks the whole
// App on the fixture build instead, the way the spacing sweep does
// (spacingsweep.tsx), so a page added to the nav is a page measured: PAGES is
// derived from SECTIONS, and prose.app.test.tsx fails when a page has no row
// in prose.ceilings.json.
//
// WHAT IS COUNTED. The same words prose.budget.test.tsx counts: the
// whitespace-separated tokens of the document's text with
// `[data-help-description]`, `<style>` and `<script>` taken out. The whole
// document, not `main`: the rail and the page head are what a reader sees too.
// And a second number: the links whose visible text carries an arrow (→ or ›),
// an action drawn as a text link where the rule wants a button.
//
// WHY THE CLOCK AND THE ZONE ARE PINNED. The fixtures date everything from FIXTURE_CLOCK, and
// a page's "3 h ago" becomes "2 days ago" if the real clock moves on; the
// count must be the same on every run for a ceiling at today's count to hold.
//
// THE CEILINGS ARE IN prose.ceilings.json, A HISTORY PER PAGE, and may only go
// down (prose.app.test.tsx holds both the history and the previous commit's
// table). The words each ceiling was set from are in prose.words.json, so a
// failure names the words that are new rather than only a number. To lower a
// ceiling after removing prose, run the page's slice with
// PROSE_RECORD_DIR=<dir>; each page's measurement is written there as JSON.

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import { FIXTURE_CLOCK } from '../api'
import CEILINGS from './prose.ceilings.json'
import WORDS from './prose.words.json'

type Persona = 'platform-admin'

export interface ProsePage {
  /** The row's key in prose.ceilings.json: the nav address, or a named state of a page. */
  key: string
  /** The slice (prose.app.<group>.test.tsx) that measures it. */
  group: string
  /** Where the page is opened: a hash address or, starting with '/', a path. */
  open: string
  persona?: Persona
  /** What is done once the page has loaded, before it is counted. */
  then?: () => void
}

/** The sections a member cannot open: their pages are measured as the platform admin, as the QA capture does. */
const ADMIN_SECTIONS = new Set(['admin', 'voice'])

/** Which slice a nav address is measured in: one file per section, Work split in two. */
function groupOf(section: string, tab: string): string {
  if (section === 'work') return ['new', 'new-workflow', 'new-issue'].includes(tab) ? 'submit' : 'work'
  if (section === 'overview' || section === 'automate') return 'work'
  if (section === 'voice') return 'admin'
  return section
}

/** The open agent and the workflow the extra rows show: the QA capture's own fixture task (qa/routes.json). */
const AGENT = 'task_a073aff5'
const WORKFLOW = 'wf_5e5ad3b6f7da4299a839'

export const PAGES: readonly ProsePage[] = [
  ...SECTIONS.flatMap((s) =>
    s.tabs.map((t) => ({
      key: `${s.id}/${t.id}`,
      group: groupOf(s.id, t.id),
      open: `${s.id}/${t.id}`,
      ...(ADMIN_SECTIONS.has(s.id) ? { persona: 'platform-admin' as const } : {}),
    })),
  ),
  // The profile menu at the rail's foot opens it; it is not a tab.
  { key: 'settings/profile', group: 'config', open: '/settings/profile' },
  // The pages the rule names beside the nav: a workflow with a step selected,
  // and an agent's Details and Logs tabs.
  {
    key: 'workflow/step-selected',
    group: 'pages',
    open: `/workflows/${WORKFLOW}/table`,
    then: () => {
      const pick = document.querySelector<HTMLButtonElement>('tbody tr[data-step] .wf-pick')
      if (pick === null) throw new Error(`workflow ${WORKFLOW} drew no step to select`)
      fireEvent.click(pick)
    },
  },
  { key: 'agent/details', group: 'pages', open: `work/task/${AGENT}` },
  { key: 'agent/logs', group: 'pages', open: `work/task/${AGENT}/logs` },
]

export interface Measure {
  words: number
  arrows: number
  /** The counted tokens in document order; prose.words.json keeps their `pieces` at each ceiling. */
  tokens: string[]
  /** The visible text of each arrow link. */
  arrowLinks: string[]
}

const ARROW = /[→›]/

/**
 * What a reader sees of `root`: the help copy, the stylesheets and the scripts
 * taken out, and the content a page shows rather than says
 * (`[data-prose-content]`: the lines of a file, diff or log Voice it's stage
 * draws, which are the planner's, not the console's explanatory prose).
 */
export function measure(root: HTMLElement): Measure {
  const clone = root.cloneNode(true) as HTMLElement
  for (const el of [...clone.querySelectorAll('[data-help-description], [data-prose-content], style, script')]) el.remove()
  const tokens = (clone.textContent ?? '').split(/\s+/).filter(Boolean)
  const arrowLinks = [...clone.querySelectorAll('a, [role="link"]')]
    .map((a) => (a.textContent ?? '').replace(/\s+/g, ' ').trim())
    .filter((t) => ARROW.test(t))
  return { words: tokens.length, arrows: arrowLinks.length, tokens, arrowLinks }
}

/**
 * The tokens cut where a lower-case letter meets a capital: `usageAgainst` is
 * two elements' text run together, and prose.words.json keeps the two words.
 * Kept whole, a run-together pair can spell a name a guard refuses (the
 * company-name guard in test/no-company-name.test.mjs did, once), and the
 * failure message reads better in words than in runs.
 */
export function pieces(tokens: readonly string[]): string[] {
  return tokens.flatMap((t) => t.split(/(?<=\p{Ll})(?=\p{Lu})/u))
}

/** The tokens of `now` that `then` does not account for, one per extra occurrence. */
export function newWords(now: readonly string[], then: readonly string[]): string[] {
  const left = new Map<string, number>()
  for (const w of then) left.set(w, (left.get(w) ?? 0) + 1)
  const out: string[] = []
  for (const w of now) {
    const n = left.get(w) ?? 0
    if (n > 0) left.set(w, n - 1)
    else out.push(w)
  }
  return out
}

export interface CeilingRow {
  /** Every ceiling this page has had, oldest first; the last is the one in force. */
  words: number[]
  arrows: number[]
}

export const TABLE = CEILINGS.pages as Record<string, CeilingRow>
const BASELINE = WORDS.pages as Record<string, string>

export function ceilingOf(key: string): { words: number; arrows: number } {
  const row = TABLE[key]
  if (row === undefined) throw new Error(`no row for ${key} in prose.ceilings.json`)
  return { words: row.words.at(-1) ?? 0, arrows: row.arrows.at(-1) ?? 0 }
}

/** The failure message: the page, its count, its ceiling and what is new since the ceiling was set. */
export function overWords(key: string, m: Measure): string {
  const extra = newWords(pieces(m.tokens), (BASELINE[key] ?? '').split(' ').filter(Boolean))
  return (
    `${key} shows ${m.words} words; its ceiling is ${ceilingOf(key).words}. ` +
    `New since the ceiling (${extra.length}): ${extra.join(' ')}`
  )
}

export function overArrows(key: string, m: Measure): string {
  return (
    `${key} draws ${m.arrows} text links with an arrow; its ceiling is ${ceilingOf(key).arrows}. ` +
    `Make each a button: ${m.arrowLinks.map((t) => JSON.stringify(t)).join(', ')}`
  )
}

/** Past every fixture delay (30-220 ms) and short of the 1 s refresh intervals, as spacingsweep.tsx explains. */
const CLOCK_MS = 700
/**
 * Steps of CLOCK_MS. One left Timeline on "reading…": its lanes are read in
 * stages, each behind the last. The clock is fake, so every run still sees
 * the same ticks; the steps only decide which state of the page is counted.
 */
const SETTLE_STEPS = 4

async function settle(): Promise<void> {
  for (let i = 0; i < SETTLE_STEPS; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CLOCK_MS)
    })
  }
}

function openAt(open: string): void {
  if (open.startsWith('/')) {
    window.location.hash = ''
    window.history.replaceState(null, '', open)
  } else {
    window.history.replaceState(null, '', '/')
    window.location.hash = `#${open}`
  }
}

/** Register one slice: an `it` per page in `group`, each holding its page to its row. */
export function proseGroup(group: string): void {
  const pages = PAGES.filter((p) => p.group === group)
  if (pages.length === 0) throw new Error(`no prose pages in group ${group}`)
  const persona = pages[0]?.persona
  if (pages.some((p) => p.persona !== persona)) throw new Error(`group ${group} mixes personas`)

  describe(`the prose ceilings: ${group}`, () => {
    let Shown: typeof App = App
    let zone: string | undefined
    beforeAll(async () => {
      // The zone, for the clock's reason: Settings › Profile names the
      // browser's own zone, and "Asia/Calcutta" is one token where "UTC" is
      // another sentence's worth. Node applies a TZ set at run time.
      zone = process.env.TZ
      process.env.TZ = 'UTC'
      if (persona !== undefined) {
        document.cookie = `swarm_qa_persona=${persona}; path=/`
        vi.resetModules()
        Shown = (await import('../App')).App
      }
      vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
      vi.setSystemTime(new Date(FIXTURE_CLOCK))
    })
    afterAll(() => {
      if (zone === undefined) delete process.env.TZ
      else process.env.TZ = zone
      vi.useRealTimers()
      document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
      window.location.hash = ''
      window.history.replaceState(null, '', '/')
      localStorage.clear()
    })

    for (const page of pages) {
      it(`${page.key} stays under its word and arrow-link ceilings`, async () => {
        openAt(page.open)
        const { unmount } = render(<Shown />)
        await settle()
        if (page.then !== undefined) {
          page.then()
          await settle()
        }
        const m = measure(document.body)
        unmount()

        const dir = process.env.PROSE_RECORD_DIR
        if (dir !== undefined && dir !== '') {
          mkdirSync(dir, { recursive: true })
          writeFileSync(
            join(dir, `${page.key.replace(/\//g, '__')}.json`),
            JSON.stringify({ key: page.key, words: m.words, arrows: m.arrows, tokens: pieces(m.tokens).join(' ') }),
          )
        }

        // A page that drew nothing would pass any ceiling: the shell alone is
        // over this, so a screen that stopped rendering fails here instead.
        expect(m.words, `${page.key} rendered almost nothing`).toBeGreaterThan(20)
        expect(m.words, overWords(page.key, m)).toBeLessThanOrEqual(ceilingOf(page.key).words)
        expect(m.arrows, overArrows(page.key, m)).toBeLessThanOrEqual(ceilingOf(page.key).arrows)
      }, 120_000)
    }
  })
}
