/**
 * HELP HAS AN INDEX, A FILTER AND ANCHORS THAT COPY A LINK (#130).
 *
 * Help was one column of every topic with no way to reach the fortieth but
 * scrolling to it. The property pinned here is the issue's own test: any
 * topic is two interactions from the top of Help.
 *
 *   * a topic index on the page -- every topic the page draws, by its subject,
 *     each a link to the topic -- sticky beside the column at desktop width;
 *   * the same index as a select at phone width (choose, and it navigates);
 *   * the filter box narrows the index with the topics, so the index never
 *     names a topic the page is not drawing;
 *   * each topic's anchor is a link, with a control that copies the topic's
 *     address.
 *
 * MUTATION: drop the index and the first case goes red; drop `matches` from
 * the index and the search case does; make the anchor plain text again and
 * the anchor case does.
 */
import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { HELP, HELP_GROUPS, TOPIC_IDS } from '../help'
import { HelpScreen } from '../HelpSection'
import { addressToPath } from '../paths'
import { cascade } from './cssgate'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.location.hash = ''
})

const indexLinks = () => [...document.querySelectorAll<HTMLAnchorElement>('nav.help-index a')]

describe('the Help topic index (#130)', () => {
  it('lists every topic the page draws, by subject, each linking to the topic', () => {
    render(<HelpScreen topic="" />)
    const nav = screen.getByRole('navigation', { name: 'Topics on this page' })
    expect(nav.classList.contains('help-index')).toBe(true)
    const links = indexLinks()
    expect(links).toHaveLength(TOPIC_IDS.length)
    links.forEach((a, i) => {
      const id = TOPIC_IDS[i]!
      expect(a.getAttribute('href')).toBe(`#${HELP[id].anchor}`)
      expect(a.textContent).toBe(HELP[id].subject)
      // A link nobody can follow is not an index: the topic is on the page.
      expect(document.getElementById(HELP[id].anchor), `${id} is indexed but not drawn`).not.toBeNull()
    })
    // Grouped as the page is, under the same group names.
    const heads = [...nav.querySelectorAll('.help-index-group')].map((h) => h.textContent)
    expect(heads).toEqual(HELP_GROUPS.filter((g) => TOPIC_IDS.some((id) => HELP[id].group === g.id)).map((g) => g.title))
  })

  it('lists only the open group on a group page', () => {
    const g = HELP_GROUPS[2]!
    render(<HelpScreen topic={g.id} />)
    const want = TOPIC_IDS.filter((id) => HELP[id].group === g.id)
    expect(indexLinks().map((a) => a.getAttribute('href'))).toEqual(want.map((id) => `#${HELP[id].anchor}`))
  })

  it('narrows with the filter box, and says so when nothing matches', () => {
    render(<HelpScreen topic="" />)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'paused' } })
    const hrefs = indexLinks().map((a) => a.getAttribute('href'))
    expect(hrefs).toContain(`#${HELP['paused-vs-full'].anchor}`)
    expect(hrefs.length).toBeLessThan(TOPIC_IDS.length)
    for (const href of hrefs) expect(document.getElementById(href!.slice(1))).not.toBeNull()

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'zz-no-topic-says-this' } })
    expect(indexLinks()).toHaveLength(0)
    expect(document.querySelector('nav.help-index')).toBeNull()
  })

  it('is a select at phone width, and choosing a topic navigates to it', () => {
    render(<HelpScreen topic="" />)
    const select = screen.getByRole('combobox', { name: 'Go to a topic' }) as HTMLSelectElement
    const options = [...select.options].filter((o) => o.value !== '')
    expect(options).toHaveLength(TOPIC_IDS.length)
    expect(options.map((o) => o.textContent)).toEqual(TOPIC_IDS.map((id) => HELP[id].subject))
    fireEvent.change(select, { target: { value: HELP['paused-vs-full'].anchor } })
    expect(window.location.hash).toBe(`#${HELP['paused-vs-full'].anchor}`)

    // Which of the two is drawn is the sheet's call: the list at 1440, the
    // select at 390, and never both.
    const list = document.querySelector('nav.help-index')!
    const pick = select.closest('.help-index-pick')!
    const display = (el: Element, width: number) => cascade(STYLES, el, 'display', { width }).winner?.value ?? null
    expect(display(list, 1440)).not.toBe('none')
    expect(display(pick, 1440)).toBe('none')
    expect(display(list, 390)).toBe('none')
    expect(display(pick, 390)).not.toBe('none')
  })

  it('sticks beside the column at desktop width', () => {
    render(<HelpScreen topic="" />)
    const list = document.querySelector('nav.help-index')!
    expect(cascade(STYLES, list, 'position', { width: 1440 }).winner?.value).toBe('sticky')
  })
})

describe('a Help topic anchor (#130)', () => {
  it('is a link to the topic, and a control that copies its address', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<HelpScreen topic="" />)
    const topic = document.getElementById(HELP['paused-vs-full'].anchor)!
    const link = topic.querySelector<HTMLAnchorElement>('.help-topic-anchor a')
    expect(link, 'the anchor is still plain text').not.toBeNull()
    expect(link!.getAttribute('href')).toBe(`#${HELP['paused-vs-full'].anchor}`)

    const copy = topic.querySelector<HTMLButtonElement>('.help-topic-anchor button')!
    expect(copy.getAttribute('aria-label')).toBe(`Copy a link to ${HELP['paused-vs-full'].subject}`)
    await act(async () => {
      fireEvent.click(copy)
    })
    // The real address, as the router spells it: `/help/<group>#<topic>`.
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}${addressToPath(HELP['paused-vs-full'].anchor)}`)
    await waitFor(() => expect(topic.querySelector('[role="status"]')?.textContent).toBe('link copied'))
  })

  it('says so when the browser refuses the clipboard, and leaves the link to select', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: () => Promise.reject(new Error('no')) } })
    render(<HelpScreen topic="" />)
    const topic = document.getElementById(HELP['what-a-pool-is'].anchor)!
    await act(async () => {
      fireEvent.click(topic.querySelector<HTMLButtonElement>('.help-topic-anchor button')!)
    })
    await waitFor(() => expect(topic.querySelector('[role="status"]')?.textContent).toBe('copy refused; select the link instead'))
  })
})
