/**
 * THE SHELL'S FINDINGS FROM THE THIRD CONSOLE VISUAL-QA RUN (QAFIX3-SHELL,
 * part of #1680; run wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of
 * main at 51d99c53, 1440 and 390 px, light and dark). Each `describe` is one
 * finding, held by the property that was false:
 *
 *   ffp-001      The phone Overview chip row gave no sign that it scrolls, and
 *                Recent failures was off-screen.
 *   work-b-007   `↑ Top` arriving in the phone header pushed the header's
 *                other items about 60px.
 *   capacity-009 (+ work-b-005) A tab that overflows the page scroller drew a
 *                scrollbar and one that scrolls an inner pane did not, so the
 *                page's right edge jumped about 15px between tabs.
 *   task-013     The breadcrumb cut the open agent's name at `b40e…` with the
 *                row half empty: a 34ch cap, under the name's 36 characters.
 *   capacity-015 API reads printed a route twice when its call had no id or
 *                query: the template, and the identical URL under it.
 *   work-b-008   Access's title sat 2-4px lower than Setup's: the head row is
 *                centred, so a taller set of actions moved the title.
 *   work-a-008 (+ work-b-003) At 390 the dock's Reads facts ran together:
 *                the separators were hidden and only a gap was left.
 *   admin-010    At 390 the rule between Platform counts' two help links
 *                stood alone at the start of the second line.
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { noteFixtureProbe, route } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 6000 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

/** Stub `matchMedia`, which jsdom does not have, as a phone (below 760px and 560px) or a desktop. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && (query.includes('759px') || query.includes('560')),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

/** A fixture: `html` inside a host that is removed after the case. */
function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

/** Give an element the scroll geometry jsdom does not lay out. */
function geometry(el: HTMLElement, g: { scrollWidth: number; clientWidth: number; scrollLeft: number }): void {
  for (const [k, v] of Object.entries(g)) Object.defineProperty(el, k, { configurable: true, writable: true, value: v })
}

async function phoneOverview() {
  media(true)
  window.history.replaceState(null, '', '/overview')
  vi.resetModules()
  const { OVERVIEW_JUMPS, SkyShell } = await import('../Spine')
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null}>
      {OVERVIEW_JUMPS.map((j) => (
        <section key={j.id} id={j.id} />
      ))}
    </SkyShell>,
  )
}

describe('ffp-001: the phone Overview chip row says it scrolls', () => {
  it('marks the side that has more chips past it, and only that side', async () => {
    await phoneOverview()
    const row = screen.getByRole('navigation', { name: 'On this page' })
    geometry(row, { scrollWidth: 960, clientWidth: 358, scrollLeft: 0 })
    await act(async () => {
      row.dispatchEvent(new Event('scroll'))
    })
    // MUTATION: drop the scroll listener in `PhoneJumps` and the row never
    // says there is more past its right edge, where Recent failures is.
    expect(row.classList.contains('is-more-end')).toBe(true)
    // MUTATION: mark both ends unconditionally and the first chip fades at rest.
    expect(row.classList.contains('is-more-start')).toBe(false)

    geometry(row, { scrollWidth: 960, clientWidth: 358, scrollLeft: 602 })
    await act(async () => {
      row.dispatchEvent(new Event('scroll'))
    })
    // MUTATION: never clear `is-more-end` and the last chip, Pools, stays faded at the end.
    expect(row.classList.contains('is-more-end')).toBe(false)
    expect(row.classList.contains('is-more-start')).toBe(true)
  })

  it('fades the edge the row continues past, in both themes', () => {
    const end = build('nav.sk-otp.is-more-end', hosts)
    const both = build('nav.sk-otp.is-more-start.is-more-end', hosts)
    const rest = build('nav.sk-otp', hosts)
    for (const theme of THEMES) {
      const env = { ...PHONE, theme }
      // MUTATION: drop the `.sk-otp.is-more-end` mask and the row's cut chip
      // reads as the last one -- the run-3 complaint.
      expect(painted(end, ['mask-image', '-webkit-mask-image'], env)).toMatch(/to right.*transparent\)$/)
      expect(painted(both, ['mask-image', '-webkit-mask-image'], env)).toMatch(/^linear-gradient\(to right, transparent/)
      // MUTATION: mask the row at rest and a row that fits fades its last chip.
      expect(painted(rest, ['mask-image', '-webkit-mask-image'], env)).toBeNull()
    }
  })
})

describe('work-b-007: `Top` arriving moves nothing in the phone header', () => {
  it('holds its place in the header from the first frame, hidden and out of reach until the page is far down', async () => {
    media(true)
    window.history.replaceState(null, '', '/work/new')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="work" tab="new" title="Submit a task" go={vi.fn()} foot={null}>
        <p>form</p>
      </SkyShell>,
    )
    const bar = document.querySelector<HTMLElement>('.sk-pbar')!
    const slot = bar.querySelector<HTMLElement>(':scope > .sk-top')
    // MUTATION: render `.sk-top` only once `scroll === 'far'` again and the
    // header's tags jump left by its width the moment it arrives.
    expect(slot, 'no slot is held for `Top` at the top of the page').not.toBeNull()
    expect(slot!.classList.contains('is-idle')).toBe(true)
    // MUTATION: leave the idle slot reachable and the keyboard lands on an invisible control.
    expect(screen.queryByRole('button', { name: /top of the page/ })).toBeNull()
    expect(slot!.tabIndex).toBe(-1)
    for (const theme of THEMES) {
      const env = { ...PHONE, theme }
      // MUTATION: hide the idle slot with `display: none` and it takes no room, so `Top` moves the row again.
      expect(painted(slot!, 'display', env)).toBe('inline-flex')
      expect(painted(slot!, 'visibility', env)).toBe('hidden')
    }

    const scroller = document.querySelector<HTMLElement>('.ctl-scroll')!
    await act(async () => {
      scroller.scrollTop = 4000
      scroller.dispatchEvent(new Event('scroll'))
    })
    const top = screen.getByRole('button', { name: /top of the page/ })
    // MUTATION: mount a second button for the far state and the slot and `Top` differ in width.
    expect(top).toBe(slot)
    expect(top.classList.contains('is-idle')).toBe(false)
    expect(painted(top, 'visibility', PHONE)).toBeNull()
  })
})

describe('capacity-009 (+ work-b-005): the page scroller keeps its gutter on every tab', () => {
  it('reserves the scrollbar gutter whether or not the page overflows', () => {
    const scroller = build('div.sk-main > div.ctl-scroll', hosts)
    for (const theme of THEMES) {
      // MUTATION: drop `scrollbar-gutter: stable` from `.ctl-scroll` and a tab
      // that scrolls an inner pane is 15px wider than one that scrolls the page.
      expect(painted(scroller, 'scrollbar-gutter', { ...WIDE, theme })).toBe('stable')
    }
  })
})

describe('task-013: the breadcrumb keeps the open agent whole while the row has room', () => {
  it('has no cap of its own under the agent name; it gives way only to the row', () => {
    const name = 'claude-code-browser agent · b40e5a1c'
    const f = fragment(
      '<div class="ctl-head"><nav class="ctl-crumb"><a class="ctl-crumb-at">Work</a>' +
        `<span class="ctl-crumb-sep">▸</span><span class="id ctl-crumb-obj">${name}</span></nav></div>`,
    )
    const obj = f.querySelector<HTMLElement>('.ctl-crumb-obj')!
    for (const env of [WIDE, PHONE]) {
      // MUTATION: put `max-width: 34ch` back on `.ctl-crumb-obj` and the fixture's
      // 36-character name is cut to `… · b40e…` with half the row empty.
      expect(painted(obj, 'max-width', env)).toBeNull()
      // MUTATION: drop the last segment's ellipsis and a long name pushes the age off the row.
      expect(painted(obj, 'min-width', env)).toBe('0')
      expect(painted(obj, 'text-overflow', env)).toBe('ellipsis')
    }
  })
})

describe('capacity-015: API reads prints a route once', () => {
  it('draws the concrete call under a route only where it differs from the route', async () => {
    media(false)
    noteFixtureProbe(route('/v1/qafix3/plain', {}), 40, true)
    noteFixtureProbe(route('/v1/qafix3/{id}/attempts', { id: 'task_b40e5a1c' }), 40, true)
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    const table = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('main .ctl-table')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const cell = (path: string) => {
      const th = [...table.querySelectorAll<HTMLElement>('th.ctl-ref-path')].find((t) => t.firstChild?.textContent === path)
      expect(th, `no row for ${path}`).not.toBeUndefined()
      return th!
    }
    // MUTATION: draw `.ctl-sub` unconditionally again and `/v1/qafix3/plain` is printed twice.
    expect(within(cell('/v1/qafix3/plain')).queryAllByText('/v1/qafix3/plain')).toHaveLength(1)
    expect(cell('/v1/qafix3/plain').querySelector('.ctl-sub')).toBeNull()
    // MUTATION: drop the line for every row and a task's read cannot be traced to its task.
    expect(cell('/v1/qafix3/{id}/attempts').querySelector('.ctl-sub')?.textContent).toBe('/v1/qafix3/task_b40e5a1c/attempts')
    for (const th of table.querySelectorAll<HTMLElement>('th.ctl-ref-path')) {
      const sub = th.querySelector('.ctl-sub')
      if (sub !== null) expect(sub.textContent, 'a route printed twice').not.toBe(th.firstChild?.textContent)
    }
  })
})

describe('work-b-008: the page title sits at one height whatever the actions beside it', () => {
  it('holds the title block to the top of the row, centred in the control height', () => {
    const head = build('div.c-phead > div.head', hosts)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop `align-self: flex-start` and the title is centred on
      // the tallest action, so Access (a segmented control) sits lower than Setup.
      expect(painted(head, 'align-self', env)).toBe('flex-start')
      // MUTATION: drop the floor and a one-line title rises to the row's top edge.
      expect(painted(head, 'min-height', env)).toBe('var(--h-ctl)')
      expect(painted(head, 'align-items', env)).toBe('center')
    }
  })
})

describe('work-a-008 (+ work-b-003): the Reads facts are parted on a phone', () => {
  it('rules a hairline before each fact, clipped where a fact starts a line', () => {
    const f = fragment(
      '<div class="ctl-dock-facts"><span class="ctl-dock-fact">3 routes<span class="ctl-dock-sep"> · </span></span>' +
        '<span class="ctl-dock-fact">p95 40ms</span></div>',
    )
    const facts = f.querySelector<HTMLElement>('.ctl-dock-facts')!
    const fact = f.querySelectorAll<HTMLElement>('.ctl-dock-fact')[1]!
    for (const theme of THEMES) {
      const env = { ...PHONE, theme }
      // MUTATION: drop the phone `.ctl-dock-fact` border and the facts run
      // together, `3 routes p95 40ms`, with only a gap between them.
      expect(painted(fact, 'border-left-style', env)).toBe('solid')
      expect(painted(fact, 'border-left-width', env)).toBe('1px')
      // MUTATION: drop the -1px margin or the clip and a fact that starts the
      // second line carries a rule before it, the QA-039 defect turned round.
      expect(painted(fact, 'margin-left', env)).toBe('-1px')
      expect(painted(facts, 'overflow', env)).toBe('hidden')
    }
    // Wider, the text ` · ` is the separator and no rule is drawn.
    expect(painted(fact, 'border-left-style', WIDE)).toBeNull()
  })
})

describe('admin-010: the rule between the help links never starts a line', () => {
  it('draws the rule in the gap before the link, and clips it where the link wraps', () => {
    const f = fragment(
      '<div class="counts-help"><p><span>Reading this screen:</span><a href="#a">Platform counts</a><a href="#b">Absent, zero, unread and stale</a></p></div>',
    )
    const box = f.querySelector<HTMLElement>('.counts-help')!
    const [first, second] = [...f.querySelectorAll<HTMLElement>('a')]
    for (const env of [WIDE, PHONE]) {
      // MUTATION: put the border back on the link itself: at 390 the second
      // link wraps and its rule stands alone at the start of line two.
      expect(painted(second!, 'border-left-style', env)).toBeNull()
      expect(painted(second!, 'border-left-style', env, 'before')).toBe('solid')
      expect(painted(second!, 'left', env, 'before')).toBe('-5px')
      expect(painted(first!, 'border-left-style', env, 'before')).toBeNull()
      // MUTATION: drop the clip and the wrapped link's rule is drawn at x = -5px, in the open.
      expect(painted(box, 'overflow', env)).toBe('hidden')
      // MUTATION: drop the 4px of room inside the clip and a link's focus ring is cut.
      expect(painted(box, 'padding-left', env)).toBe('4px')
    }
  })
})
