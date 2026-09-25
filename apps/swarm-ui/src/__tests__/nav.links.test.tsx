/**
 * Every internal hash link resolves, and none of them rides an alias.
 *
 * WHAT THIS CATCHES. `SECTION_ALIASES` keeps an old section id working so a
 * link someone saved in a runbook still lands. That is the whole point of it,
 * and it is also its hazard: if the app's OWN links use the old spelling too,
 * every one of them keeps working after a rename, so nothing fails, so nobody
 * updates one, and the old name outlives the rename indefinitely. The rename
 * that produced this file found `#agents/...` in eighteen places in
 * `checks.ts` alone -- all of which would have gone on saying `agents` under a
 * section called Work, silently, forever.
 *
 * So the rule is: an alias is for hashes this app did not write. Anything the
 * app writes uses the current spelling, and this test is what makes that true
 * rather than merely intended.
 *
 * It reads the SOURCE, not a render. A render exercises the links a fixture
 * happens to reach; the eighteen in `checks.ts` are produced by branches that
 * need a specific unhealthy platform to appear at all, and the one that is
 * wrong will be the one no fixture produces.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App, CAPACITY, INTERNAL_LINKS_MAY_NOT_USE_ALIASES, SECTIONS, WORK, fromHash } from '../App'
import { HELP_ROUTE } from '../help'

/**
 * The props App hands the agent list, captured. The list itself is not what
 * these tests are about -- App's half of AG-17 is WHICH props it passes -- so
 * it is replaced by a recorder, and every other screen renders for real.
 */
const agentsProps = vi.hoisted(() => [] as Record<string, unknown>[])
vi.mock('../Agents', () => ({
  AgentsScreen: (props: Record<string, unknown>) => {
    agentsProps.push(props)
    return null
  },
}))

const SRC = join(__dirname, '..')

function sources(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      // The test directory is excluded: a test may legitimately write an OLD
      // hash, because asserting that an old hash still resolves is exactly
      // what a rename needs proven.
      if (name !== '__tests__' && name !== 'node_modules') out.push(...sources(full))
    } else if (/\.(tsx?|css|html)$/.test(name)) {
      out.push(full)
    }
  }
  return out
}

/**
 * Hash links as the app writes them.
 *
 * Only `href` attributes and `location.hash` assignments, not every `#` in the
 * file: the stylesheet and the comments are full of `#agents/task/<id>` as
 * PROSE, describing what an address used to be, and prose about an old name is
 * not a link to it.
 */
function links(): { file: string; hash: string }[] {
  const found: { file: string; hash: string }[] = []
  for (const file of sources(SRC)) {
    const text = readFileSync(file, 'utf8')
    // BOTH SPELLINGS OF A LINK. `href=` is the JSX attribute; `href:` is an
    // object property, which is how every one of `checks.ts`'s links is
    // written -- and `checks.ts` holds more of them than the rest of the app
    // put together. Matching only the attribute form found 14 links and
    // reported them all sound while the eighteen that mattered went unread.
    for (const m of text.matchAll(/href\s*[=:]\s*(?:"|'|`|\{"|\{'|\{`)#([A-Za-z0-9/_.-]+)/g)) {
      found.push({ file: file.slice(SRC.length + 1), hash: m[1]! })
    }
    for (const m of text.matchAll(/location\.hash\s*=\s*(?:'|"|`)#?([A-Za-z0-9/_.-]+)/g)) {
      found.push({ file: file.slice(SRC.length + 1), hash: m[1]! })
    }
  }
  return found
}

/** `fromHash` reads `window.location.hash`, so the hash is set and it is called. */
function resolve(hash: string) {
  window.location.hash = `#${hash}`
  return fromHash()
}

describe('internal navigation links', () => {
  it('binds the id constants to sections that exist', () => {
    // SECTIONS declares its ids as string LITERALS, because a Python gate
    // parses the array out of this file with a regex and cannot resolve a
    // constant; the constants exist so that the nine `SectionBody` cases and
    // the four comparisons in `fromHash` cannot drift from it. Two spellings
    // of one id is exactly the defect this file was written to stop, so the
    // two are tied together here rather than trusted to stay in step.
    const ids = SECTIONS.map((s) => s.id)
    expect(ids, 'the WORK constant names no section').toContain(WORK)
    expect(ids, 'the CAPACITY constant names no section').toContain(CAPACITY)
  })

  it('finds links to check, so an empty sweep cannot pass as a clean one', () => {
    // The guard this repository keeps needing: a regex that matched nothing
    // reports the same "no failures" as a codebase with no defects.
    expect(links().length).toBeGreaterThan(25)
  })

  it('never writes a section id that only an alias keeps alive', () => {
    const aliased = links().filter((l) =>
      INTERNAL_LINKS_MAY_NOT_USE_ALIASES.includes(l.hash.split('/')[0]!),
    )
    expect(
      aliased.map((l) => `${l.file}: #${l.hash}`),
      'these use a retired section id. It still resolves, which is why nothing ' +
        'else would have told you. Write the current spelling; the alias is for ' +
        'links this app did not write.',
    ).toEqual([])
  })

  it('resolves every link to a pane that exists', () => {
    const dead: string[] = []
    for (const l of links()) {
      const head = l.hash.split('/')[0]!
      // `#help/<topic>` carries a topic verbatim and HelpScreen answers for an
      // unknown one by name, so there is no dead tail here to find.
      if (head === HELP_ROUTE) continue
      const r = resolve(l.hash)
      // A hash that resolves to the HOME section while naming something else
      // is the failure mode: `fromHash` falls back to the first section rather
      // than rendering nothing, so a typo looks like a working link.
      if (r.sectionId !== head && r.taskId === null) {
        dead.push(`${l.file}: #${l.hash} -> ${r.sectionId}/${r.tab}`)
      }
    }
    expect(dead, 'these fell through to another section').toEqual([])
  })

  it('resolves every link to a TAB that exists, not just a section', () => {
    const dead: string[] = []
    for (const l of links()) {
      const [head, ...rest] = l.hash.split('/')
      if (head === HELP_ROUTE || rest.length === 0) continue
      const wanted = rest.join('/')
      const r = resolve(l.hash)
      // A task drawer link carries an id, not a tab, and is resolved as one.
      if (r.taskId !== null) continue
      if (r.tab !== wanted) dead.push(`${l.file}: #${l.hash} -> tab "${r.tab}"`)
    }
    expect(dead, 'these named a pane that does not exist and fell back to the first one').toEqual([])
  })
})

describe('the links the frame draws itself', () => {
  afterEach(() => {
    window.location.hash = ''
    agentsProps.length = 0
  })

  /**
   * CH-5 (App half). The API reads page's "What these mean" was an unclassed
   * anchor, so it fell back to the browser's own blue -- visited purple once
   * followed -- in a product whose links are ink plus an underline.
   *
   * MUTATION: drop `ctl-link` from the anchor.
   */
  it('draws the API reads help link in the product link treatment', () => {
    window.location.hash = '#reference'
    render(<App />)
    const link = screen.getByRole('link', { name: /What these mean/ })
    expect(link.getAttribute('href')).toBe(`#${HELP_ROUTE}/api-reads`)
    expect(link.className.split(/\s+/)).toContain('ctl-link')
  })

  /**
   * AH-5. The head `?` is the way into Help -- ui-audit §B7.2/§B7.3 say so,
   * and so did the comment on the rail's `?` -- but its card was a dead end:
   * the section's question and nothing to follow.
   *
   * MUTATION: remove the link from the section card.
   */
  it("ends the head `?`'s card in a way into Help", async () => {
    window.location.hash = '#overview/now'
    const { container } = render(<App />)
    const glyph = container.querySelector<HTMLButtonElement>('.ctl-q-glyph')
    expect(glyph, 'the head carries no section `?`').not.toBeNull()
    await act(async () => {
      glyph!.focus()
    })
    const card = document.getElementById(glyph!.getAttribute('aria-controls') ?? '')
    expect(card, 'focusing the head `?` opened no card').not.toBeNull()
    const link = card!.querySelector('a')
    expect(link, 'the section card is a dead end').not.toBeNull()
    expect(link!.getAttribute('href')).toBe(`#${HELP_ROUTE}`)
    expect(link!.textContent).toMatch(/Help/)
  })

  /**
   * AG-17 (App half). With the inspector open, no row said which agent it was
   * showing, because App never told the list: `taskId` stopped at the drawer.
   *
   * MUTATION: stop passing it. The list is rendered with no `taskId`.
   */
  it('tells the agent list which agent the inspector has open', () => {
    window.location.hash = '#work/task/task_0123456789abcdef0123'
    render(<App />)
    const last = agentsProps[agentsProps.length - 1]
    expect(last, 'the agent list was not rendered under an open inspector').toBeTruthy()
    expect(last!.taskId).toBe('task_0123456789abcdef0123')
  })

  it('tells it that none is open when the inspector is shut', () => {
    window.location.hash = '#work/running'
    render(<App />)
    const last = agentsProps[agentsProps.length - 1]
    expect(last).toBeTruthy()
    expect(last!.taskId).toBeNull()
  })
})
