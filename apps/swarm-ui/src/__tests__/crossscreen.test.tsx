// THE CROSS-SCREEN LANE OF FUNCTIONALITY WAVE 3 (#96, #98, #129, #138, #139),
// filed against the old console and re-read against the Sky one. #141 (one
// rule for tables below 900px) is not here: CH-13 had already delivered it
// (design-system §7.3), and tables.scroll / tables.phone / chrome.shared pin it.
//
// Each `describe` is one issue and each `it` names the mutation that turns it
// red. Where the new console already did what an issue asked, the `it` pins
// the property so it cannot be undone quietly; where it did not, the `it` is
// the half that was built here. CSS claims go through `cascade` (cssgate.ts)
// via `painted`, because jsdom applies no `@media` block and orders rules by
// source position alone.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App, crumbsOf, SECTIONS } from '../App'
import { StateMark, TONE_MARK, toneOf } from '../components'
import { MarkGlyph, STATE_MARK } from '../marks'
import type { Result } from '../fetch'
import { AGED_AFTER_MS, FrameAge, Screen } from '../Shell'
import { scrollBand, TOP_AFTER_MIN_PX } from '../Spine'
import { stateTone, type TaskState } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as baseTask } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  window.history.replaceState(null, '', '/')
})

function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

function pick(host: Element, sel: string): HTMLElement {
  const el = host.querySelector<HTMLElement>(sel)
  expect(el, `the fixture has no ${sel}`).not.toBeNull()
  return el!
}

const html = (node: React.ReactElement): string => renderToStaticMarkup(node)

// ===========================================================================
// #96 -- failures stand out in the light theme
// ===========================================================================

describe('#96: a failed run is a row form, not only a mark', () => {
  // The rows as Agents draws them: the state mark (`StateMark`) first, its `data-hue` from
  // `STATE_MARK`, and the reason line's `is-warn` from `whyNeedsAction`.
  const ROWS =
    '<div class="rows">' +
    `<div class="row clickable" data-k="failed">${html(<StateMark state="FAILED" />)}<span class="agent">a</span></div>` +
    `<div class="row clickable" data-k="dead">${html(<StateMark state="DEAD_LETTERED" />)}<span class="agent">b</span></div>` +
    `<div class="row clickable" data-k="cancelled">${html(<StateMark state="CANCELLED" />)}<span class="agent">c</span></div>` +
    `<div class="row clickable" data-k="warn">${html(<StateMark state="READY" />)}<span class="agent">d</span><span class="why is-warn">pool paused</span></div>` +
    `<div class="row clickable" data-k="open" aria-current="true">${html(<StateMark state="FAILED" />)}<span class="agent">e</span></div>` +
    '</div>'

  const row = (f: HTMLElement, k: string) => pick(f, `.row[data-k="${k}"]`)

  it('draws a full-height --bad rule down a failed row, and nothing on a cancelled one', () => {
    // MUTATION: drop the row rule, or key it on anything the row does not
    // carry (a cancelled row gets it, a failed one does not).
    const f = fragment(ROWS)
    for (const k of ['failed', 'dead']) {
      expect(painted(row(f, k), 'background-image', WIDE), `${k}: no tone rule`).toMatch(/linear-gradient\(var\(--bad\)/)
      expect(painted(row(f, k), 'background-size', WIDE)).toBe('3px 100%')
      expect(painted(row(f, k), 'background-image', PHONE)).toMatch(/var\(--bad\)/)
    }
    expect(painted(row(f, 'cancelled'), 'background-image', WIDE) ?? 'none', 'a cancelled row is drawn as a failure').toBe('none')
  })

  it('draws a half-height --warn rule on a row whose reason needs action', () => {
    // MUTATION: the warn rule at full height, or not drawn.
    const f = fragment(ROWS)
    expect(painted(row(f, 'warn'), 'background-image', WIDE)).toMatch(/var\(--warn\)/)
    expect(painted(row(f, 'warn'), 'background-size', WIDE)).toBe('3px 46%')
  })

  it('leaves the text in ink and the selection rule as it was', () => {
    // MUTATION: colour the failed row's text, or let the tone rule replace
    // the open row's 2px ink rule or sit under it.
    const f = fragment(ROWS)
    expect(painted(row(f, 'failed'), 'color', WIDE) ?? 'inherit').not.toMatch(/--bad/)
    const open = row(f, 'open')
    expect(painted(open, 'box-shadow', WIDE)).toBe('inset 2px 0 0 var(--text)')
    expect(painted(open, ['background', 'background-color'], WIDE)).toBe('var(--surface-2)')
    expect(painted(open, 'background-image', WIDE)).toMatch(/var\(--bad\)/)
    expect(painted(open, 'background-position', WIDE), 'the tone rule hides under the selection rule').toBe('2px center')
  })

  it('pins the hook the rule is keyed on: a failed chip says bad, a cancelled one does not', () => {
    // MUTATION: change `STATE_MARK`'s hue for FAILED, DEAD_LETTERED or
    // CANCELLED, or stop `StateMark` writing it.
    const hue = (s: TaskState) => fragment(html(<StateMark state={s} />)).querySelector('.sk-st')!.getAttribute('data-hue')
    expect(hue('FAILED')).toBe('bad')
    expect(hue('DEAD_LETTERED')).toBe('bad')
    expect(hue('CANCELLED')).not.toBe('bad')
    expect(hue('SUCCEEDED')).not.toBe('bad')
  })

  it("gives Overview's check cards the same two edges", () => {
    // The issue's other option, the one taken: the attention row gets the
    // rule rather than a bigger mark -- and O1's check cards (`.ov-att`) keep
    // it. MUTATION: drop either edge.
    const f = fragment(
      '<div class="ov-atts">' +
        '<a class="ov-att is-bad" data-k="bad"><span class="sk-st is-bad"></span><span class="ov-att-t"><b>dispatch failing</b></span></a>' +
        '<a class="ov-att is-warn" data-k="warn"><span class="sk-st is-warn"></span><span class="ov-att-t"><b>a pool is paused</b></span></a>' +
        '</div>',
    )
    const bad = pick(f, '[data-k="bad"]')
    const warn = pick(f, '[data-k="warn"]')
    expect(painted(bad, 'background-image', WIDE)).toMatch(/var\(--bad\)/)
    expect(painted(bad, 'background-size', WIDE)).toBe('3px 100%')
    expect(painted(warn, 'background-image', WIDE)).toMatch(/var\(--warn\)/)
    expect(painted(warn, 'background-size', WIDE)).toBe('3px 46%')
    // The rule has room: the mark does not sit on it.
    expect(painted(bad, ['padding-left', 'padding'], WIDE)).toMatch(/12px/)
  })
})

// ===========================================================================
// #98 -- one read age per screen
// ===========================================================================

interface Rows {
  rows: string[]
}

function screenOf(result: Result<Rows>) {
  return (
    <Screen<Rows> title="Things" load={async () => result} summary={(d) => `${d.rows.length} rows`}>
      {(d) => <ul>{d.rows.map((r) => <li key={r}>{r}</li>)}</ul>}
    </Screen>
  )
}

const sub = (): string => document.querySelector('.sub')?.textContent ?? ''

describe('#98: one read age per screen', () => {
  it("prints a fresh read's age once: in the frame's head, not again under the title", async () => {
    // MUTATION: keep `read …` on the sub-line inside the frame.
    render(<FrameAge.Provider value={true}>{screenOf({ status: 'ok', data: { rows: ['a'] }, fetchedAt: Date.now() })}</FrameAge.Provider>)
    await waitFor(() => expect(sub()).toContain('1 rows'))
    expect(sub()).not.toMatch(/\bread\b/)
    expect(sub(), 'the refresh control went with the age').toContain('refresh')
  })

  it('still prints it where no head carries it, and whenever the read is stale', async () => {
    // MUTATION: drop the age everywhere, or drop it from an aged read too --
    // a panel states its freshness when it is stale, and only then.
    const first = render(screenOf({ status: 'ok', data: { rows: ['a'] }, fetchedAt: Date.now() }))
    await waitFor(() => expect(sub()).toContain('read just now'))
    first.unmount()

    render(
      <FrameAge.Provider value={true}>
        {screenOf({ status: 'ok', data: { rows: ['a'] }, fetchedAt: Date.now() - AGED_AFTER_MS - 60_000 })}
      </FrameAge.Provider>,
    )
    await waitFor(() => expect(sub()).toMatch(/not refreshed · read \d+m ago/))
  })

  it("keeps a cached payload's own age on the sub-line, beside a head that times the fetch", async () => {
    // A read that landed just now of a payload generated 30m ago: the head
    // says `just now`, so the data's age has to be said here. MUTATION:
    // `ownAge = aged || !frameAge` -- the 30m disappears from the screen.
    render(
      <FrameAge.Provider value={true}>
        {screenOf({
          status: 'ok',
          data: { rows: ['a'] },
          fetchedAt: Date.now(),
          serverAt: new Date(Date.now() - 30 * 60_000).toISOString(),
        })}
      </FrameAge.Provider>,
    )
    await waitFor(() => expect(sub()).toContain('1 rows'))
    expect(sub()).toMatch(/read 30m ago/)
  })

  it('leads with the cadence, not a separator, when a fresh in-frame line has nothing before it', async () => {
    // MUTATION: always print `every`'s leading ` · `.
    render(
      <FrameAge.Provider value={true}>
        <Screen<Rows> title="Things" load={async () => ({ status: 'ok', data: { rows: ['a'] }, fetchedAt: Date.now() })} pollMs={10_000}>
          {(d) => <ul>{d.rows.map((r) => <li key={r}>{r}</li>)}</ul>}
        </Screen>
      </FrameAge.Provider>,
    )
    await waitFor(() => expect(sub()).toContain('every'))
    expect(sub().trim()).toMatch(/^every /)
  })

  it('keeps the list\'s own age while an agent is open over it', async () => {
    // With an agent open the head times the INSPECTOR (`shownBy`), so the
    // list under it must keep its own `read …`. MUTATION: one
    // `FrameAge.Provider value={true}` around the page as well as the drawer.
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const id = 'task_' + 'crossscreen0open'.padEnd(20, '0')
    const other = baseTask({ id: 'task_' + 'crossscreen0list'.padEnd(20, '0'), tenant_id: 'eng', state: 'RUNNING' })
    const open = baseTask({ id, tenant_id: 'eng', state: 'RUNNING' })
    const routes: Record<string, unknown> = {
      '/v1/tasks': { tasks: [other], next_page_token: null },
      [`/v1/tasks/${id}`]: { task: open },
      [`/v1/tasks/${id}/events`]: { events: [] },
      [`/v1/tasks/${id}/attempts`]: { attempts: [] },
      '/v1/resource-classes': { resource_classes: {} },
    }
    const real = globalThis.fetch
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const answer = routes[new URL(String(input), 'http://ui.test').pathname]
      if (answer === undefined) return new Promise<Response>(() => {})
      return new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json' } })
    }) as unknown as typeof fetch
    try {
      window.history.replaceState(null, '', `/agents/live/${id}`)
      const { App: LiveApp } = await import('../App')
      render(<LiveApp />)
      const listSub = () => document.querySelector('main.work .sub')?.textContent ?? ''
      await waitFor(() => expect(listSub()).toMatch(/\bread (just now|\d+s ago)/))
    } finally {
      globalThis.fetch = real
      vi.unstubAllEnvs()
    }
  })

  it("lets the head print the screen's age on a Screen route, and none on Platform counts", async () => {
    // Counts' figures are as old as the count, not as the session read the
    // head would time. MUTATION: drop the claim, or the head's check of it.
    window.history.replaceState(null, '', '/admin/tenants')
    const a = render(<App />)
    expect(document.querySelector('.ctl-head-age'), 'a Screen route lost the head age').not.toBeNull()
    a.unmount()

    window.history.replaceState(null, '', '/admin/counts')
    render(<App />)
    await waitFor(() => expect(document.querySelector('.head > h1')?.textContent).toBe('Platform counts'))
    expect(document.querySelector('.ctl-head-age'), 'two ages on Platform counts').toBeNull()
  })

  it("labels Overview's lead count as the checks it counts", async () => {
    // MUTATION: an unlabelled `{n}/{m}` fraction back beside `8/8 reads`.
    window.history.replaceState(null, '', '/')
    render(<App />)
    const line = await waitFor(() => {
      const el = document.querySelector('#ov-needs .ov-lh .ov-cnt')
      expect(el).not.toBeNull()
      return el!
    })
    expect(line.textContent).toMatch(/^(\d+ checks? of \d+ · derived on this read|\d+ of \d+ checks still reading)/)
    expect(line.textContent).not.toMatch(/^\d+\/\d+/)
  })
})

// ===========================================================================
// #129 -- cancelled, queued and succeeded are three shapes
// ===========================================================================

describe('#129: cancelled, queued and succeeded are three shapes once the word is hidden', () => {
  it('draws the three as three different glyphs in the state-mark vocabulary', () => {
    // marks.tsx `STATE_MARK`, which the Agents list, the workflow nodes and
    // the chip draw at every width. MUTATION: give two of the three one mark.
    const glyph = (s: TaskState) => html(<svg>{MarkGlyph({ mark: STATE_MARK[s].mark })}</svg>)
    const three = (['CANCELLED', 'QUEUED', 'SUCCEEDED'] as const).map(glyph)
    expect(new Set(three).size).toBe(3)
    // Every state whose meaning differs has its own mark: the only shared
    // marks are SUBMITTED/QUEUED (in line) and LEASED/DISPATCHED/STARTING
    // (holds a slot, not yet running).
    const byMark = new Map<string, TaskState[]>()
    for (const [s, m] of Object.entries(STATE_MARK) as [TaskState, { mark: string }][]) {
      byMark.set(m.mark, [...(byMark.get(m.mark) ?? []), s])
    }
    const shared = [...byMark.values()].filter((v) => v.length > 1).map((v) => v.sort().join('/'))
    expect(shared.sort()).toEqual(['DISPATCHED/LEASED/STARTING', 'QUEUED/SUBMITTED'])
  })

  it('draws the bare marks for the three tones as three shapes, the info one the flat bar', () => {
    // The bare dot is the canonical `ToneMark` (#503 swap), whose shapes are
    // the brand glyphs at every width. MUTATION: give `info` the ok mark, or
    // CANCELLED's tone back to `wait`.
    const marks = (['warn', 'info', 'ok'] as const).map((t) => TONE_MARK[t].mark)
    expect(new Set(marks).size).toBe(3)
    expect(stateTone('CANCELLED')).toBe('ended')
    // The info silhouette is the flat bar §6.6 specifies (CH-22): CANCELLED's own.
    expect(TONE_MARK[toneOf(stateTone('CANCELLED'))].mark).toBe(STATE_MARK.CANCELLED.mark)
    const svg = fragment(renderToStaticMarkup(<MarkGlyph mark={TONE_MARK.info.mark} />))
    const bar = svg.querySelector('rect')!
    expect(Number(bar.getAttribute('width')), 'the flat bar is wider than it is tall').toBeGreaterThan(Number(bar.getAttribute('height')) * 2)
  })
})

// ===========================================================================
// #138 -- one page-head shape
// ===========================================================================

describe('#138: the breadcrumb is the trail to the page, in links, and never the title again', () => {
  const route = (sectionId: string, tab: string, taskId: string | null = null) => ({
    sectionId,
    tab,
    taskId,
    taskPane: 'detail' as const,
  })
  const crumbs = (sectionId: string, tab: string, taskId: string | null = null) => {
    const section = SECTIONS.find((s) => s.id === sectionId)!
    const t = section.tabs.find((x) => x.id === tab) ?? null
    return crumbsOf({
      at: route(sectionId, tab, taskId),
      head: section.label,
      home: `${section.id}/${section.tabs[0]!.id}`,
      tab: t,
      tabs: section.tabs.length,
      title: t?.label ?? section.label,
      closeTo: 'work/running/recent',
    })
  }

  it('draws no crumb where the only segment would be the title', () => {
    // MUTATION: push the section unconditionally. Overview's crumb said
    // `Overview` over an `<h1>` that said `Overview`.
    expect(crumbs('overview', 'now')).toEqual([])
  })

  it("stops before the page: the section, as a link to the section's first page", () => {
    const capacity = SECTIONS.find((s) => s.id === 'capacity')!
    const last = capacity.tabs[capacity.tabs.length - 1]!
    const c = crumbs('capacity', last.id)
    expect(c.map((x) => x.label)).toEqual([capacity.label])
    expect(c[0]!.to).toBe(`capacity/${capacity.tabs[0]!.id}`)
  })

  it('names the open agent last, and makes the list a link that closes it', () => {
    const work = SECTIONS.find((s) => s.id === 'work')!
    const c = crumbs('work', 'running', 'tsk_open')
    expect(c.map((x) => x.label)).toEqual([work.label, work.tabs.find((t) => t.id === 'running')!.label, 'tsk_open'])
    expect(c[1]!.to, 'the list segment does not close the agent').toBe('work/running/recent')
    expect(c[2]!.to, 'the open object is a link to itself').toBeNull()
  })

  it('renders the segments as links that navigate, and no segment repeats the h1', async () => {
    // MUTATION: render the segments as spans again, or add the page back.
    // A SECTION PAGE DRAWS NO TRAIL (visual QA Q2, 2026-10-02): the panel
    // lights the page and the title names it. The trail is drawn inside an
    // open object, where it is the way back -- and there it is still links.
    window.history.replaceState(null, '', '/capacity/accounts')
    const page = render(<App />)
    expect(document.querySelector('.ctl-crumb'), 'a section page draws a breadcrumb row').toBeNull()
    page.unmount()
    window.location.hash = '#work/task/t-1'
    render(<App />)
    const crumb = document.querySelector('.ctl-crumb')!
    expect(crumb, 'no breadcrumb inside an open agent').not.toBeNull()
    const links = [...crumb.querySelectorAll('a')]
    expect(links.map((a) => a.textContent)).toEqual(['Work', 'Agents'])
    // The object is the last segment, and the one that is not a link.
    expect(crumb.querySelector('[aria-current="page"]')?.textContent).toBe('t-1')
    await act(async () => {
      fireEvent.click(links[0]!)
    })
    const work = SECTIONS.find((s) => s.id === 'work')!
    await waitFor(() =>
      // The open group (#503: the page itself, its first child, is the lit one).
      expect(document.querySelector('.sk-panel .sk-pk.is-group .sk-pl')?.textContent?.trim()).toBe(work.tabs[0]!.label),
    )
    expect(document.querySelector('.ctl-crumb'), 'the object closed and its trail stayed').toBeNull()
  })

  it('puts the cost of a count on the button that spends it', async () => {
    // MUTATION: the cost beside the button again, or the button bare.
    window.history.replaceState(null, '', '/admin/counts')
    render(<App />)
    const button = await screen.findByRole('button', { name: /^Run the count · \d+(–\d+)? reads$/ })
    expect(button.querySelector('.counts-cost'), 'the price is not inside the control').not.toBeNull()
  })
})

// ===========================================================================
// #139 -- phone chrome
// ===========================================================================

describe('#139: the phone header compacts on scroll, and a long page has a way back up', () => {
  it('bands the scroll: top, compact past the header, `Top` past a screen', () => {
    // MUTATION: change either threshold's meaning.
    expect(scrollBand(0, 844)).toBe('top')
    expect(scrollBand(44, 844)).toBe('top')
    expect(scrollBand(45, 844)).toBe('down')
    expect(scrollBand(844, 844)).toBe('down')
    expect(scrollBand(845, 844)).toBe('far')
    // Floored, so a short landscape scrollport is not offered `Top` at once.
    expect(scrollBand(TOP_AFTER_MIN_PX, 300)).toBe('down')
  })

  it('draws the header at 36px once the page is under it, and 44px at the top', () => {
    // MUTATION: drop the compact height, or apply it on the desktop.
    const f = fragment(
      '<div class="sk-app" data-k="top"><header class="sk-pbar"></header></div>' +
        '<div class="sk-app is-scrolled" data-k="down"><header class="sk-pbar"></header></div>',
    )
    expect(painted(pick(f, '[data-k="top"] .sk-pbar'), 'height', PHONE)).toBe('44px')
    expect(painted(pick(f, '[data-k="down"] .sk-pbar'), 'height', PHONE)).toBe('36px')
    expect(painted(pick(f, '[data-k="down"] .sk-pbar'), 'position', PHONE), 'the header scrolls away').toBe('sticky')
  })

  it('offers `Top` on the phone only, sticky above the dock, and it goes to the top', async () => {
    // MUTATION: never render it, render it on the desktop, or make it do
    // nothing.
    window.history.replaceState(null, '', '/help')
    render(<App />)
    const scroller = document.querySelector<HTMLElement>('.ctl-scroll')!
    const app = document.querySelector('.sk-app')!
    expect(app.classList.contains('is-scrolled')).toBe(false)
    expect(document.querySelector('.sk-top')).toBeNull()

    await act(async () => {
      scroller.scrollTop = 120
      scroller.dispatchEvent(new Event('scroll'))
    })
    expect(app.classList.contains('is-scrolled'), 'the header did not compact').toBe(true)
    expect(document.querySelector('.sk-top'), '`Top` after one swipe').toBeNull()

    await act(async () => {
      scroller.scrollTop = 4000
      scroller.dispatchEvent(new Event('scroll'))
    })
    const top = screen.getByRole('button', { name: /Top/ })
    expect(painted(top, 'display', PHONE)).toBe('flex')
    expect(painted(top, 'display', WIDE)).toBe('none')
    expect(painted(top, 'position', PHONE)).toBe('sticky')
    expect(painted(top, 'min-height', PHONE), 'not a touch target').toBe('44px')

    await act(async () => {
      fireEvent.click(top)
    })
    expect(scroller.scrollTop).toBe(0)
    expect(document.querySelector('.sk-top')).toBeNull()
    expect(app.classList.contains('is-scrolled')).toBe(false)
  })

  it('keeps every section one tap away at any scroll: the menu rides in the sticky header', () => {
    // CH-21 pins the drawer's contents and positions; this pins that the way
    // to it never scrolls away. MUTATION: `.sk-pbar` static at 390.
    const f = fragment('<div class="sk-app is-scrolled"><header class="sk-pbar"><button class="sk-ibtn"></button></header></div>')
    const bar = pick(f, '.sk-pbar')
    expect(painted(bar, 'position', PHONE)).toBe('sticky')
    expect(painted(bar, 'top', PHONE)).toBe('0')
  })
})
