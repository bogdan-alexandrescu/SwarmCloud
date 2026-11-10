// THE APP-WIDE PROSE RATCHET: no console page may show more words than it did
// before the change (THE OWNER'S RULE, 2026-11-05, standing, every console page:
// no explanatory prose on the surface; docs/web-ui/design-system.md).
//
// The four screen files (prose.budget, prose.budget.capacity, prose.operator,
// prose.runs) each mount one screen on hand-built mocks. This walks the whole
// App on the fixture build instead, the way the spacing sweep does
// (spacingsweep.tsx), so a page added to the nav is a page measured: PAGES is
// derived from SECTIONS.
//
// WHAT IS COUNTED. The same words prose.budget.test.tsx counts: the
// whitespace-separated tokens of the document's text with
// `[data-help-description]`, `<style>` and `<script>` taken out. The whole
// document, not `main`: the rail and the page head are what a reader sees too.
// And a second number: the links whose visible text carries an arrow (→ or ›),
// an action drawn as a text link where the rule wants a button.
//
// WHAT IT IS COMPARED WITH (the owner's pick, 2026-11-06, part of #2688): the same
// page on the change's base, rendered in the same test from a copy of the
// base's src (prosebase.ts says which commit and why). A page fails when its
// words or its arrow links went up, and the message names the words that are
// new. There used to be a table of per-page ceilings that lanes lowered by
// hand; a change that lowered one and a change that added words under the
// old number were each green and red together. A delta has no such pair, so
// the table is gone: the base is the ceiling. A page the base did not have is
// measured and reported, and passes: there is nothing to compare it with.
//
// WHY THE CLOCK, THE ZONE AND THE STORAGE ARE PINNED. The fixtures date
// everything from FIXTURE_CLOCK, and a page's "3 h ago" becomes "2 days ago"
// if the real clock moves on; each render starts at its own tree's
// FIXTURE_CLOCK with empty storage, so the base and the head differ only in
// what the change did.

import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import { chooseBase, ensureCommit, extractApp, type BaseTree, type ProseBase } from './prosebase'

type Persona = 'platform-admin'

export interface ProsePage {
  /** The page's name in a failure: the nav address, or a named state of a page. */
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
  /** The counted tokens in document order. */
  tokens: string[]
  /** The visible text of each arrow link. */
  arrowLinks: string[]
}

const ARROW = /[→›]/

/**
 * What a reader sees of `root`: the help copy, the stylesheets and the scripts
 * taken out, and the content a page shows rather than says
 * (`[data-prose-content]`: the lines of a file, diff or log Voice it's stage
 * draws, which are the planner's, not the console's explanatory prose). The
 * visually hidden words (`.sk-vh`) are out too: they are a screen reader's,
 * not the surface's -- the phone menu button's "4 waiting for approval" is
 * its accessible description, drawn as the badge's digit.
 */
export function measure(root: HTMLElement): Measure {
  const clone = root.cloneNode(true) as HTMLElement
  for (const el of [...clone.querySelectorAll('[data-help-description], [data-prose-content], .sk-vh, style, script')]) el.remove()
  const tokens = (clone.textContent ?? '').split(/\s+/).filter(Boolean)
  const arrowLinks = [...clone.querySelectorAll('a, [role="link"]')]
    .map((a) => (a.textContent ?? '').replace(/\s+/g, ' ').trim())
    .filter((t) => ARROW.test(t))
  return { words: tokens.length, arrows: arrowLinks.length, tokens, arrowLinks }
}

/**
 * The tokens cut where a lower-case letter meets a capital: `usageAgainst` is
 * two elements' text run together, and a failure names the two words.
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

/**
 * What `head` shows that `base` did not, as failure messages; none is a pass.
 * A null base is a page the base did not have.
 */
export function proseDelta(key: string, base: Measure | null, head: Measure): string[] {
  if (base === null) return []
  const out: string[] = []
  if (head.words > base.words) {
    const extra = newWords(pieces(head.tokens), pieces(base.tokens))
    out.push(
      `${key} shows ${head.words} words, ${head.words - base.words} more than the ${base.words} on the base. ` +
        `New words (${extra.length}): ${extra.join(' ')}. Move any explanation behind a tooltip, an (i) or a status tick.`,
    )
  }
  if (head.arrows > base.arrows) {
    const extra = newWords(head.arrowLinks, base.arrowLinks)
    out.push(
      `${key} draws ${head.arrows} text links with an arrow, ${base.arrows} on the base. ` +
        `Make each new one a button: ${extra.map((t) => JSON.stringify(t)).join(', ')}`,
    )
  }
  return out
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

const HERE = dirname(fileURLToPath(import.meta.url))
/** The app directory: src/__tests__ is two below it. */
const APP_DIR = join(HERE, '..', '..')

interface Tree {
  src: string | null
  App: typeof App
  sections: typeof SECTIONS
  clock: string
}

/**
 * The App, its nav and its fixture clock from `src` (null: the head's own),
 * imported afresh. Afresh for every page and both trees: the fixture modules
 * keep state (a read counter, what an earlier page opened), and a page
 * measured after another would otherwise differ between the base and the
 * head for no change of theirs.
 */
async function load(src: string | null): Promise<Tree> {
  vi.resetModules()
  if (src === null) {
    const app = await import('../App')
    const api = await import('../api')
    return { src, App: app.App, sections: app.SECTIONS, clock: api.FIXTURE_CLOCK }
  }
  const app = (await import(/* @vite-ignore */ join(src, 'App.tsx'))) as typeof import('../App')
  const api = (await import(/* @vite-ignore */ join(src, 'api.ts'))) as typeof import('../api')
  return { src, App: app.App, sections: app.SECTIONS, clock: api.FIXTURE_CLOCK }
}

/** Whether the base drew `page` at all: a nav page is there when the base's nav has its tab. */
function baseHas(page: ProsePage, sections: typeof SECTIONS): boolean {
  if (page.key !== page.open) return true
  const [section, tab] = page.key.split('/')
  return sections.some((s) => s.id === section && s.tabs.some((t) => t.id === tab))
}

/** Render `Shown` at `page`, let it settle, and measure what the document shows. */
async function renderPage(tree: Tree, page: ProsePage): Promise<Measure> {
  localStorage.clear()
  sessionStorage.clear()
  vi.setSystemTime(new Date(tree.clock))
  openAt(page.open)
  const Shown = tree.App
  const { unmount } = render(<Shown />)
  try {
    await settle()
    if (page.then !== undefined) {
      page.then()
      await settle()
    }
    return measure(document.body)
  } finally {
    unmount()
  }
}

/** Register one slice: an `it` per page in `group`, each holding its page to what the base showed. */
export function proseGroup(group: string): void {
  const pages = PAGES.filter((p) => p.group === group)
  if (pages.length === 0) throw new Error(`no prose pages in group ${group}`)
  const persona = pages[0]?.persona
  if (pages.some((p) => p.persona !== persona)) throw new Error(`group ${group} mixes personas`)

  describe(`the prose ratchet: ${group}`, () => {
    let base: Tree
    let chosen: ProseBase
    let tree: BaseTree | undefined
    let zone: string | undefined
    beforeAll(async () => {
      chosen = chooseBase(APP_DIR)
      ensureCommit(APP_DIR, chosen.sha)
      tree = extractApp(APP_DIR, chosen.sha)
      console.log(`PROSE BASE: ${chosen.sha} (${chosen.why})`)
      // The zone, for the clock's reason: Settings › Profile names the
      // browser's own zone, and "Asia/Calcutta" is one token where "UTC" is
      // another sentence's worth. Node applies a TZ set at run time.
      zone = process.env.TZ
      process.env.TZ = 'UTC'
      if (persona !== undefined) document.cookie = `swarm_qa_persona=${persona}; path=/`
      base = await load(join(tree.app, 'src'))
      vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    }, 120_000)
    afterAll(() => {
      tree?.remove()
      if (zone === undefined) delete process.env.TZ
      else process.env.TZ = zone
      vi.useRealTimers()
      document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
      window.location.hash = ''
      window.history.replaceState(null, '', '/')
      localStorage.clear()
    })

    for (const page of pages) {
      it(`${page.key} shows no more words or arrow links than on the base`, async () => {
        const was = baseHas(page, base.sections) ? await renderPage(await load(base.src), page) : null
        const now = await renderPage(await load(null), page)
        console.log(
          `PROSE ${page.key}: ${was === null ? 'new page' : `${was.words} words, ${was.arrows} arrows`} on ${chosen.sha.slice(0, 12)}; ` +
            `${now.words} words, ${now.arrows} arrows now`,
        )

        // A page that drew nothing would pass any comparison: the shell alone
        // is over this, so a screen that stopped rendering fails here instead.
        expect(now.words, `${page.key} rendered almost nothing`).toBeGreaterThan(20)
        const up = proseDelta(page.key, was, now)
        expect(up, up.join('\n')).toEqual([])
      }, 240_000)
    }
  })
}
