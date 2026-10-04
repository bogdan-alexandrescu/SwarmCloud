/**
 * U10a (owner QA, 2026-10-04): the shell -- spine, panel, page head, help,
 * and the API-reads line.
 *
 *   D14  the panel's Recent workflows broke an id mid-token over two lines.
 *   D25  on /submit the spine lit Work and the panel lit Agents › Live.
 *   D26  collapsed, the tenant tile read `E Engin…` and nothing expanded the
 *        panel but a click on a section.
 *   D27  the panel's Live / Waiting lagged the Agents tabs (14/1 vs 8/2).
 *   D31  help popover headings were mono on one card, sans on the other.
 *   D39  at 390px the Overview head put a lone `?` above the tenant chip, and
 *        the API-reads line wrapped to two lines.
 *   D40  the meta pill had a fill in dark and none to see in light.
 *   +    Help's `copy link` confirms as the agent header's Copy link does.
 *
 * MUTATIONS: restore any one rule or branch these cases name.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { HelpCard } from '../HelpCard'
import { HelpScreen } from '../HelpSection'
import { HELP } from '../help'
import { RECENT_WORKFLOWS_KEY } from '../Spine'
import type { CascadeEnv } from './cssgate'
import { painted, resolveColour, THEMES } from './marks'
import { contrast } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
function tree(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  localStorage.clear()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

async function at(path: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  const { container } = render(<App />)
  await waitFor(() => expect(container.querySelector('.sk-spine .sk-av')?.getAttribute('title')).not.toBe('not read'), { timeout: 5000 })
  return container
}

describe('D25: Submit reads as the place you are on', () => {
  for (const path of ['/submit', '/submit/task', '/submit/workflow', '/submit/issue']) {
    it(`lights Submit, and neither Work nor Agents › Live, on ${path}`, async () => {
      const c = await at(path)
      const cta = c.querySelector('.sk-spine .sk-cta')!
      expect(cta.getAttribute('aria-current')).toBe('page')
      expect(c.querySelector('.sk-spine .sk-ri.is-on'), 'a spine section is lit on Submit').toBeNull()
      expect(c.querySelector('.sk-panel [aria-current="page"]'), 'a panel page is lit on Submit').toBeNull()
      expect(c.querySelector('.sk-panel .sk-pk.is-on, .sk-panel .sk-kid.is-on'), 'a panel page is lit on Submit').toBeNull()
    })
  }

  it('draws the lit CTA differently from the resting one', () => {
    const host = tree('<nav class="sk-spine"><a class="sk-ri sk-cta" aria-current="page"></a><a class="sk-ri sk-cta"></a></nav>')
    const [on, off] = [...host.querySelectorAll('.sk-cta')]
    expect(painted(on!, 'box-shadow', WIDE)).not.toBeNull()
    expect(painted(off!, 'box-shadow', WIDE)).toBeNull()
    expect(painted(on!, 'content', WIDE, 'before')).not.toBeNull()
  })

  it('still lights Work on a Work page', async () => {
    const c = await at('/workflows')
    expect(c.querySelector('.sk-spine .sk-ri.is-on')?.getAttribute('data-sec')).toBe('work')
    expect(c.querySelector('.sk-spine .sk-cta')?.getAttribute('aria-current')).toBeNull()
  })
})

describe('D26: the collapsed spine', () => {
  it('draws the tenant as its initial only, the name in the tooltip, and offers Expand', async () => {
    localStorage.setItem('swarm.shell.collapsed', '1')
    const c = await at('/workflows')
    expect(c.querySelector('.sk-panel'), 'the panel is drawn while collapsed').toBeNull()
    const tile = await waitFor(() => {
      const t = c.querySelector<HTMLElement>('.sk-ttile')
      expect(t?.getAttribute('title') ?? '').toMatch(/^Tenant /)
      return t!
    })
    expect(tile.querySelector('small'), 'the tile still prints a cut name').toBeNull()
    expect((tile.textContent ?? '').trim()).toMatch(/^\S$/)
    const expand = within(c.querySelector('.sk-spine') as HTMLElement).getByRole('button', { name: 'Expand the panel' })
    fireEvent.click(expand)
    expect(c.querySelector('.sk-panel')).not.toBeNull()
    expect(c.querySelector('.sk-spine .sk-expand'), 'Expand stays once the panel is open').toBeNull()
  })
})

describe('D27: the panel counts what the Agents tabs count', () => {
  it('draws the list’s Live and Waiting beside the tabs that say them', async () => {
    const c = await at('/agents/live')
    const badge = (name: string) =>
      [...c.querySelectorAll('.ag-list-tabs [role="tab"]')].find((b) => b.textContent?.startsWith(name))?.querySelector('.badge')?.textContent
    const panel = (name: string) =>
      [...c.querySelectorAll('.sk-panel .sk-kid')].find((k) => k.querySelector('span')?.textContent === name)?.querySelector('.sk-cnt')?.textContent
    await waitFor(() => expect(badge('Live')).toBeDefined(), { timeout: 5000 })
    await waitFor(() => {
      expect(panel('Live')).toBe(badge('Live'))
      expect(panel('Waiting')).toBe(badge('Waiting'))
    })
    const live = [...c.querySelectorAll('.sk-panel .sk-kid')].find((k) => k.querySelector('span')?.textContent === 'Live')!
    expect(live.querySelector('.sk-cnt')?.getAttribute('title')).toMatch(/Agents list/)
  })
})

describe('D14: a recent workflow is one line, never broken mid-id', () => {
  it('ellipses on one line and keeps the whole name and id in the title', async () => {
    const id = 'wf_' + '8fa28bbc798e4b8a8e7e'.repeat(2)
    localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify([{ id, state: 'RUNNING', name: null }]))
    const c = await at('/workflows')
    const button = await waitFor(() => {
      const b = c.querySelector<HTMLElement>('.sk-recent > button.sk-kid[title]')
      expect(b).not.toBeNull()
      return b!
    })
    expect(button.getAttribute('title')).toBe(id)
    const name = button.querySelector(':scope > span > span:last-child')!
    expect(name.textContent).toBe(id)
    expect(painted(name, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(name, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(name, 'overflow-wrap', WIDE), 'an id may not break anywhere').toBe('normal')
    expect(painted(name, '-webkit-line-clamp', WIDE)).toBeNull()
  })
})

describe('D31: every help popover heads itself in one face', () => {
  it('a topic card’s title is the sans face, as the section question card’s is', async () => {
    render(<HelpCard topic="paused-vs-full" />)
    fireEvent.click(screen.getByRole('button', { name: `Help: ${HELP['paused-vs-full'].title}` }))
    const card = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-focus-return]')
      expect(el).not.toBeNull()
      return el!
    })
    const title = card.querySelector('strong')!
    expect(title.style.fontFamily).toBe('var(--font)')
    const q = tree('<span class="ctl-q-card"><strong class="ctl-q-title">Agents answers</strong></span>')
    expect(painted(q.querySelector('.ctl-q-title')!, ['font', 'font-family'], WIDE)).toMatch(/var\(--font\)/)
  })
})

describe('Help’s copy link confirms as Copy link does', () => {
  it('says copied on the button and beside it, and clears after 4s', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    render(<HelpScreen topic="paused-vs-full" />)
    const topic = document.getElementById(HELP['paused-vs-full'].anchor)!
    const copy = topic.querySelector<HTMLButtonElement>('.help-topic-anchor button')!
    await act(async () => {
      fireEvent.click(copy)
    })
    expect(topic.querySelector('[role="status"]')?.textContent).toBe('copied')
    expect(copy.textContent).toBe('copied ✓')
    act(() => {
      vi.advanceTimersByTime(4000)
    })
    expect(topic.querySelector('[role="status"]')?.textContent).toBe('')
    expect(copy.textContent).toBe('copy link')
  })
})

describe('D39: the phone frame', () => {
  it('keeps the Overview’s `?` and its tenant chip on one line', () => {
    const host = tree('<div class="ov-page"><div class="c-phead"><div class="head"><h1>Overview</h1></div><p class="sub"><span class="c-meta">tenant eng</span></p></div></div>')
    const ph = host.querySelector('.c-phead')!
    expect(painted(ph, 'flex-wrap', PHONE)).toBe('nowrap')
    const sub = host.querySelector('.sub')!
    expect(painted(sub, 'min-width', PHONE)).toBe('0')
    expect(painted(sub, ['flex', 'flex-basis'], PHONE)).toMatch(/^1 1 0\b|^0$/)
  })

  it('draws the API-reads line as one line: the caveats stay, routes and p95 step aside', async () => {
    const { Dock } = await import('../Dock')
    const { noteFixtureProbe, route } = await import('../fetch')
    noteFixtureProbe(route('/v1/capacity'), 120, true)
    const { container } = render(<Dock />)
    const wide = [...container.querySelectorAll('.ctl-dock-facts .ctl-dock-fact.is-wide')]
    expect(wide.map((w) => (w.textContent ?? '').trim())).toEqual([expect.stringMatching(/routes? ·$/), expect.stringMatching(/^p95 .* ·$/)])
    for (const w of wide) expect(painted(w, 'display', PHONE)).toBe('none')
    expect(painted(wide[0]!, 'display', WIDE)).not.toBe('none')
    // What a phone draws: `failed`, any `admin-only`, and the age, which is
    // the fact the old ellipsis ate (F10) -- and it fits 390px at the strip's
    // 12px mono (7.2px a character) beside the mark, `Reads` and the caret.
    const kept = [...container.querySelectorAll('.ctl-dock-facts > *')]
      .filter((f) => !f.classList.contains('is-wide'))
      .map((f) => f.textContent ?? '')
      .join('')
    expect(kept).toMatch(/failed/)
    expect(kept).toMatch(/newest|nothing has loaded/)
    expect(kept.length * 7.2 + 120, `"${kept}" does not fit 390px`).toBeLessThanOrEqual(390 - 2 * 16)
  })
})

describe('D40: the meta pill is drawn the same in both themes', () => {
  it('has an edge that stands off the page in light and in dark', () => {
    const host = tree('<div class="c-phead"><p class="sub"><span class="c-meta">2 read</span></p></div>')
    const meta = host.querySelector('.c-meta')!
    for (const theme of THEMES) {
      const border = painted(meta, ['border', 'border-color'], { ...WIDE, theme }) ?? ''
      const edge = /var\((--[\w-]+)\)/.exec(border)?.[1]
      expect(edge, `${theme}: the pill has no edge`).toBeDefined()
      const ratio = contrast(resolveColour(`var(${edge})`, theme), resolveColour('var(--bg)', theme))
      expect(ratio, `${theme}: the pill's edge does not stand off the page`).toBeGreaterThan(1.15)
    }
  })
})
