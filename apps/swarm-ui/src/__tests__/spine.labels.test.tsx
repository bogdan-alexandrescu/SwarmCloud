/**
 * THE RAIL LABELS HELP, AND ADMIN STOPS REPEATING "ADMIN" (#136).
 *
 *   * Help's rail entry carries the visible word `Help` under its glyph, not
 *     a bare `?` whose only name is in `title`. (The Sky spine built it so in
 *     #432; this pins it.)
 *   * The Admin section's pages read `Pool limits`, `Tenants`, `Platform
 *     counts` -- the section heading already says Admin.
 *   * The `admin` mark appears only where an admin page sits among pages that
 *     are not: Provider quota under Capacity › Accounts. Which pages those are
 *     is read from App.tsx `SECTIONS`, so the mark cannot drift from the flag.
 *
 * MUTATION: drop `admin: true` from the quota kid in PANEL_PAGES and the third
 * case goes red; print the mark on every admin page and the second does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, within } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import { HELP_PLACES, type HelpPlace } from '../help'
import { PANEL_PAGES, SkyShell } from '../Spine'

afterEach(() => {
  cleanup()
  window.history.pushState(null, '', '/')
})

function shell(section: 'capacity' | 'admin', tab: string) {
  return render(
    <SkyShell section={section} tab={tab} title="x" go={vi.fn()} foot={null}>
      {null}
    </SkyShell>,
  )
}

describe('the rail names Help (#136)', () => {
  it('draws the word Help on the rail entry, visible, not only in a title', () => {
    window.history.pushState(null, '', '/overview')
    render(<App />)
    const util = document.querySelector('.ctl-nav-util')!
    const help = within(util as HTMLElement).getByRole('link', { name: 'Help' })
    expect(help.querySelector('small')?.textContent).toBe('Help')
    expect(help.textContent?.trim()).toBe('Help')
  })
})

describe('Admin pages do not repeat admin (#136)', () => {
  it('reads Pool limits, Tenants and Platform counts with no admin mark', () => {
    shell('admin', 'limits')
    const pages = [...document.querySelectorAll('.sk-panel .sk-pk')]
    expect(pages.map((p) => p.querySelector('.sk-pl')?.textContent)).toEqual(['Pool limits', 'Tenants', 'Platform counts'])
    for (const p of pages) {
      expect(p.textContent ?? '').not.toMatch(/admin/i)
      expect(p.querySelector('.sk-adm')).toBeNull()
    }
  })
})

describe('the admin mark sits only on an admin page among others (#136)', () => {
  it('marks Provider quota under Capacity › Accounts, and nothing beside it', () => {
    shell('capacity', 'quota')
    const kids = [...document.querySelectorAll('.sk-panel .sk-kid')]
    const quota = kids.find((k) => k.textContent?.startsWith('Provider quota'))
    expect(quota, 'Provider quota is not drawn').toBeDefined()
    expect(quota!.querySelector('.sk-adm')?.textContent).toBe('admin')
    for (const k of kids.filter((k) => k !== quota)) expect(k.querySelector('.sk-adm')).toBeNull()
  })

  it('agrees with the admin flags in App.tsx SECTIONS', () => {
    const flagged = new Set(
      SECTIONS.filter((s) => s.tabs.some((t) => !t.admin)).flatMap((s) => s.tabs.filter((t) => t.admin).map((t) => t.id)),
    )
    const marked = new Set(
      (['work', 'capacity', 'admin'] as const).flatMap((sec) =>
        PANEL_PAGES[sec].flatMap((p) => [
          ...(p.admin === true ? [p.key] : []),
          ...(p.kids ?? []).filter((k) => k.admin === true).map((k) => k.key),
        ]),
      ),
    )
    expect([...marked].sort()).toEqual([...flagged].sort())
    expect(marked.has('quota')).toBe(true)
  })
})

describe('Help names each screen as the panel does (#131)', () => {
  it('uses the panel label for every screen an action opens', () => {
    const labels = new Map<string, string>()
    for (const sec of ['work', 'capacity', 'admin'] as const) {
      for (const p of PANEL_PAGES[sec]) {
        labels.set(p.to, p.label)
        for (const k of p.kids ?? []) labels.set(k.to, k.label)
      }
    }
    for (const [place, name] of Object.entries(HELP_PLACES) as [HelpPlace, string][]) {
      if (labels.has(place)) expect(name, place).toBe(labels.get(place))
    }
  })
})

describe('the Work panel lists every Work page (visual QA Q7, 2026-10-02)', () => {
  // Runs (/runs, intake-tenants.html 1A) was a tab in SECTIONS and a route,
  // and the panel never drew it: the only way in was a submit's redirect.
  // MUTATION: drop the `runs` row from PANEL_PAGES.work and this goes red.
  it('draws a panel row for each Work tab that is not a Submit form', () => {
    const work = SECTIONS.find((s) => s.id === 'work')!
    const pages = work.tabs.map((t) => t.id).filter((id) => !id.startsWith('new'))
    const rows = new Set(PANEL_PAGES.work.flatMap((p) => [p.key === 'agents' ? 'running' : p.key, ...(p.kids ?? []).map((k) => k.key)]))
    expect(pages.filter((id) => !rows.has(id))).toEqual([])
  })

  it('lights Runs when /runs is open', () => {
    window.history.pushState(null, '', '/runs')
    render(<App />)
    const on = document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')
    expect(on?.textContent).toBe('Runs')
  })
})
