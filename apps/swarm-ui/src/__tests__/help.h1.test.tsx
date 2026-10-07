/**
 * HELP IS H1: ONE PAGE PER GROUP, SEARCH ACROSS ALL (admin-help.html, the
 * owner's pick 2026-10-01), and the 2026-10-02 audit's findings (#503):
 *
 *   * no third column listing every topic -- the panel is the contents;
 *   * every topic a card with You see / It means / What to do rows, not
 *     paragraphs of prose;
 *   * `/help` opens one group, so the panel always has one to mark;
 *   * the search box is the column's full width and searches every group;
 *   * each card's anchor is a link with a control that copies its address
 *     (#130, kept);
 *   * on a phone, a group select under the search (the panel is behind the
 *     menu there).
 *
 * Each test names the mutation that turns it red.
 */
import STYLES from '../styles.css?raw'
import HELP_CSS from '../styles/help.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { App } from '../App'
import { HELP, HELP_GROUPS, TOPIC_IDS } from '../help'
import { HelpScreen, helpPageOf } from '../HelpSection'
import { addressToPath } from '../paths'
import { cascade } from './cssgate'

const SHEET = `${STYLES}\n${HELP_CSS}`
const won = (el: Element, prop: string | readonly string[], width: number) => cascade(SHEET, el, prop, { width }).winner?.value ?? null
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.location.hash = ''
})

const inGroup = (g: string) => TOPIC_IDS.filter((id) => HELP[id].group === g)

describe('H1: one page per group', () => {
  /** MUTATION: draw every group on /help again, or the topic index beside the column. */
  it('opens the first group at /help, and draws no contents column', () => {
    render(<HelpScreen topic="" />)
    const first = HELP_GROUPS.find((g) => inGroup(g.id).length > 0)!
    expect(helpPageOf('')).toBe(first.id)
    expect(text(document.querySelector('.head h1'))).toBe(first.title)
    const drawn = [...document.querySelectorAll('.help-topic')].map((t) => t.id)
    expect(drawn).toEqual(inGroup(first.id).map((id) => HELP[id].anchor))
    expect(document.querySelector('nav.help-index, .help-index, .help-layout'), 'the contents column is back').toBeNull()
  })

  it('draws a group page with only that group, titled with it', () => {
    const g = HELP_GROUPS[2]!
    render(<HelpScreen topic={g.id} />)
    expect(text(document.querySelector('.head h1'))).toBe(g.title)
    expect([...document.querySelectorAll('.help-topic')].map((t) => t.id)).toEqual(inGroup(g.id).map((id) => HELP[id].anchor))
    // The page's line is the count note right after the head (#138), not a line under the title.
    expect(document.querySelector('.c-phead .sub'), 'a line under the title').toBeNull()
    expect(text(document.querySelector('.c-phead + .c-count-note'))).toMatch(new RegExp(`^${inGroup(g.id).length} on this page · `))
  })

  it('opens a topic’s own group for a deep link, and marks that topic current', () => {
    render(<HelpScreen topic="paused-vs-full" />)
    const g = HELP['paused-vs-full'].group
    expect(helpPageOf('paused-vs-full')).toBe(g)
    expect(text(document.querySelector('.head h1'))).toBe(HELP_GROUPS.find((x) => x.id === g)!.title)
    expect(document.getElementById(HELP['paused-vs-full'].anchor)!.classList.contains('is-current')).toBe(true)
    expect(document.querySelectorAll('.help-topic.is-current').length).toBe(1)
  })

  it('names the group a stale link’s page is, so the panel can mark it', () => {
    const first = HELP_GROUPS.find((g) => inGroup(g.id).length > 0)!
    expect(helpPageOf('a-topic-that-was-renamed')).toBe(first.id)
  })
})

describe('H1: the panel marks the group being shown', () => {
  /**
   * #503: the panel lit no group at /help, which drew every group. A page is
   * one group now, and App asks `helpPageOf` which, so the panel marks it at
   * /help as on /help/<group>.
   *
   * MUTATION: pass the raw route tail to the panel again.
   */
  it('marks the first group at /help, and a deep link’s own group', async () => {
    const first = HELP_GROUPS.find((g) => inGroup(g.id).length > 0)!
    window.history.replaceState(null, '', '/help')
    const a = render(<App />)
    await waitFor(() => expect(text(document.querySelector('.sk-pk[aria-current="page"] .sk-pl'))).toBe(first.title))
    a.unmount()

    const g = HELP_GROUPS.find((x) => x.id === HELP['paused-vs-full'].group)!
    window.history.replaceState(null, '', `/help/${g.id}`)
    render(<App />)
    await waitFor(() => expect(text(document.querySelector('.sk-pk[aria-current="page"] .sk-pl'))).toBe(g.title))
    window.history.replaceState(null, '', '/')
  })
})

describe('H1: every topic is a card with You see / It means / What to do', () => {
  /** MUTATION: draw the long form as paragraphs straight under the title again. */
  it('draws the three rows in order: the claim, the long form, the action', () => {
    const g = HELP['paused-vs-full'].group
    render(<HelpScreen topic={g} />)
    for (const id of inGroup(g)) {
      const t = HELP[id]
      const card = document.getElementById(t.anchor)!
      expect(text(card.querySelector('.help-topic-head h3'))).toBe(t.subject)
      const rows = [...card.querySelectorAll(':scope > dl.help-rows > div')]
      expect(rows.map((r) => text(r.querySelector(':scope > dt')))).toEqual(['You see', 'It means', 'What to do'])
      expect(text(rows[0]!.querySelector(':scope > dd'))).toBe(t.title)
      expect(text(rows[1]!.querySelector(':scope > dd > p'))).toBe(t.long[0])
      expect(text(rows[2]!.querySelector(':scope > dd'))).toContain(t.act.say)
      expect(card.querySelector(':scope > p'), `${id} still draws prose outside its rows`).toBeNull()
    }
  })

  /** MUTATION: drop the card's border or fill. */
  it('is a card: a border, a fill and the card radius', () => {
    render(<HelpScreen topic="" />)
    const card = document.querySelector('.help-topic')!
    expect(won(card, ['border', 'border-top-width'], 1440)).toBe('1px solid var(--line)')
    expect(won(card, ['background-color', 'background'], 1440)).toBe('var(--surface)')
    expect(won(card, 'border-radius', 1440)).toBe('var(--radius)')
  })

  it('stacks the rows under their terms on a phone', () => {
    render(<HelpScreen topic="" />)
    const rows = document.querySelector('.help-rows')!
    expect(won(rows, 'grid-template-columns', 1440)).toBe('96px minmax(0, 1fr)')
    expect(won(rows, 'grid-template-columns', 390)).toBe('minmax(0, 1fr)')
  })
})

describe('H1: the search spans every group', () => {
  /** MUTATION: cap the search box at 420px again, or search only the open group. */
  it('is the column’s full width', () => {
    render(<HelpScreen topic="" />)
    const input = screen.getByRole('searchbox', { name: 'Search all topics' })
    expect(input.getAttribute('placeholder')).toBe(`Search all ${TOPIC_IDS.length} topics`)
    expect(won(input, 'width', 1440)).toBe('100%')
    expect(won(input.closest('.help-search')!, 'max-width', 1440)).toBeNull()
  })

  it('lists matches from every group with the match marked, each a link to its topic', () => {
    const first = HELP_GROUPS.find((g) => inGroup(g.id).length > 0)!
    render(<HelpScreen topic={first.id} />)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'paused' } })
    const hits = [...document.querySelectorAll<HTMLAnchorElement>('.help-hits a.help-hit')]
    expect(hits.map((a) => a.getAttribute('href'))).toContain(`#${HELP['paused-vs-full'].anchor}`)
    // Not only the open group: paused-vs-full is not in the first one.
    expect(HELP['paused-vs-full'].group).not.toBe(first.id)
    for (const a of hits) {
      expect(text(a.querySelector('mark')).toLowerCase()).toBe('paused')
      expect(text(a.querySelector('small')).length, 'a hit does not name its group').toBeGreaterThan(0)
    }
    expect(text(document.querySelector('.head h1'))).toBe('Help')
    expect(text(document.querySelector('.c-phead + .c-count-note'))).toMatch(new RegExp(`^${hits.length} of ${TOPIC_IDS.length} topics match “paused”$`))
    // The cards step aside while the results are up.
    expect(document.querySelector('.help-topic')).toBeNull()

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'zz-no-topic-says-this' } })
    expect(document.querySelectorAll('.help-hit').length).toBe(0)
    expect(text(document.body)).toContain('No topic mentions zz-no-topic-says-this.')
  })
})

describe('H1 on a phone: the group is a select', () => {
  /** MUTATION: drop the select, or show it beside the panel too. */
  it('lists every group, and choosing one navigates to it', () => {
    render(<HelpScreen topic="" />)
    const select = screen.getByRole('combobox', { name: 'Help group', hidden: true }) as HTMLSelectElement
    const groups = HELP_GROUPS.filter((g) => inGroup(g.id).length > 0)
    expect([...select.options].map((o) => o.value)).toEqual(groups.map((g) => g.id))
    fireEvent.change(select, { target: { value: groups[1]!.id } })
    expect(window.location.hash).toBe(`#help/${groups[1]!.id}`)
    const pick = select.closest('.help-group-pick')!
    expect(won(pick, 'display', 1440)).toBe('none')
    expect(won(pick, 'display', 390)).not.toBe('none')
  })
})

describe('a Help topic anchor (#130)', () => {
  it('is a link to the topic, and a control that copies its address', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<HelpScreen topic="paused-vs-full" />)
    const topic = document.getElementById(HELP['paused-vs-full'].anchor)!
    const link = topic.querySelector<HTMLAnchorElement>('.help-topic-head .help-topic-anchor a')
    expect(link, 'the anchor is not in the card head').not.toBeNull()
    expect(link!.getAttribute('href')).toBe(`#${HELP['paused-vs-full'].anchor}`)
    expect(text(link)).toBe('#paused-vs-full')

    const copy = topic.querySelector<HTMLButtonElement>('.help-topic-anchor button')!
    expect(copy.getAttribute('aria-label')).toBe(`Copy a link to ${HELP['paused-vs-full'].subject}`)
    await act(async () => {
      fireEvent.click(copy)
    })
    // The real address, as the router spells it: `/help/<group>#<topic>`.
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}${addressToPath(HELP['paused-vs-full'].anchor)}`)
    // THE AGENT HEADER'S CONFIRMATION (U10a, 2026-10-04): `copied`, and the
    // button says it too, where the reader is looking.
    await waitFor(() => expect(topic.querySelector('[role="status"]')?.textContent).toBe('copied'))
    expect(copy.textContent).toBe('copied ✓')
  })

  it('says so when the browser refuses the clipboard, and leaves the link to select', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: () => Promise.reject(new Error('no')) } })
    render(<HelpScreen topic="what-a-pool-is" />)
    const topic = document.getElementById(HELP['what-a-pool-is'].anchor)!
    await act(async () => {
      fireEvent.click(topic.querySelector<HTMLButtonElement>('.help-topic-anchor button')!)
    })
    await waitFor(() => expect(topic.querySelector('[role="status"]')?.textContent).toBe('could not copy; select the link instead'))
  })
})
