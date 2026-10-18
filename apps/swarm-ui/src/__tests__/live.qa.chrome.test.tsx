/**
 * LIVE QA, SECTION 10: THE CHROME (2026-10-11). The shell shared by every
 * page: it fits a tablet, keeps keyboard focus, and keeps Recent per tenant.
 *
 *   CH-1  between the phone layout and ~1100px the open panel left the page
 *         narrower than the phone layout does (440-500px of content).
 *   CH-2  Runs' state filter did not wrap, and the page scrolled sideways at 400.
 *   CH-3  the panel's Recent workflows were one list for the browser, so a
 *         switch to another tenant still offered (and titled pages with) the
 *         first tenant's workflows.
 *   CH-5  the keyboard could not scroll a page until focus was inside it, and
 *         Back did not put the scroll back.
 *   CH-9, CH-15, CH-16: the S3 parts asserted here (focus after a collapse,
 *         the skip link and focus on a page change, the document title).
 *
 * Each case asserts the property the finding names, not the markup that
 * happens to produce it today.
 *
 * MUTATIONS: drop `tablet` from `collapsed` in SkyShell (CH-1); delete the
 * phone `.c-seg { flex-wrap: wrap; max-width: 100% }` (CH-2); return the bare
 * key from `recentKey` (CH-3); drop the `onPop` read or the `focusPage` call
 * in the place effect (CH-5); drop `flip`'s refocus (CH-9).
 */
import { act, cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import STYLES from '../styles.css?raw'
import COMPONENTS from '../styles/components.css?raw'
import INTAKE from '../styles/intake.css?raw'
import { documentObjectOf, documentTitleOf, fromAddress } from '../App'
import { chooseTenant } from '../fetch'
import { HELP_GROUPS } from '../help'
import {
  DEFAULT_TENANT_KEY,
  RECENT_WORKFLOWS_KEY,
  SkyShell,
  TABLET_QUERY,
  recentName,
  recentWorkflows,
  rememberWorkflow,
} from '../Spine'
import { cascade } from './cssgate'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
})

function shell(props: { section?: 'overview' | 'work'; tab?: string; place?: string; children?: React.ReactNode } = {}) {
  const el = (p: typeof props) => (
    <SkyShell section={p.section ?? 'work'} tab={p.tab ?? 'workflows'} title="Workflows" go={() => {}} foot={null} place={p.place ?? '/a'}>
      {p.children ?? <h1>Page</h1>}
    </SkyShell>
  )
  const r = render(el(props))
  return { ...r, again: (next: typeof props) => r.rerender(el(next)) }
}

/** `matchMedia` answering true for the tablet band only, as an 820px window would. */
function atTablet(): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === TABLET_QUERY,
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
  }))
}

describe('CH-1: the tablet band gives the page the room the panel took', () => {
  it('starts collapsed between the phone layout and 1100px, whatever was stored', () => {
    atTablet()
    const { container } = shell()
    expect(container.querySelector('.sk-panel'), 'the 236px panel is open at tablet width').toBeNull()
    expect(container.querySelector('.sk-app')?.classList.contains('is-collapsed')).toBe(true)
  })

  it('opens the panel on request without changing the desktop’s stored choice', () => {
    atTablet()
    const { container } = shell()
    fireEvent.click(container.querySelector<HTMLButtonElement>('button[aria-label="Expand the panel"]')!)
    expect(container.querySelector('.sk-panel')).not.toBeNull()
    expect(window.localStorage.getItem('swarm.shell.collapsed'), 'a tablet expand rewrote the desktop preference').toBeNull()
  })

  it('a section click at tablet width navigates and keeps the page its room', () => {
    atTablet()
    const go = vi.fn()
    const { container } = render(
      <SkyShell section="work" tab="workflows" title="Workflows" go={go} foot={null}>
        <h1>Page</h1>
      </SkyShell>,
    )
    fireEvent.click(container.querySelector<HTMLAnchorElement>('.sk-spine [data-sec="capacity"]')!)
    expect(go).toHaveBeenCalledWith('capacity/pools')
    expect(container.querySelector('.sk-panel')).toBeNull()
  })

  it('keeps the panel open on a desktop that never collapsed it', () => {
    const { container } = shell()
    expect(container.querySelector('.sk-panel')).not.toBeNull()
  })
})

describe('CH-2: a segmented filter never widens a phone page', () => {
  const SHEETS = [COMPONENTS, INTAKE, STYLES].join('\n')
  function runsFilter(): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML = '<div class="rn-filters"><div class="c-seg" role="group"><button>all</button><button>rejected</button></div></div>'
    document.body.appendChild(host)
    return host.querySelector<HTMLElement>('.c-seg')!
  }

  it('wraps, and is capped at its container, at 400px', () => {
    const seg = runsFilter()
    expect(cascade(SHEETS, seg, 'flex-wrap', { width: 400 }).winner?.value.trim()).toBe('wrap')
    expect(cascade(SHEETS, seg, 'max-width', { width: 400 }).winner?.value.trim()).toBe('100%')
    seg.parentElement!.parentElement!.remove()
  })
})

describe('CH-3: Recent workflows are kept per tenant', () => {
  function engList(): void {
    window.localStorage.setItem(DEFAULT_TENANT_KEY, 'eng')
    window.localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_eng1', state: 'SUCCEEDED', name: 'phone-bottom-bar' }]))
  }

  it('offers another tenant none of the first tenant’s workflows, and gives them back on the way back', () => {
    engList()
    chooseTenant('eng')
    expect(recentWorkflows().map((w) => w.id)).toEqual(['wf_eng1'])
    chooseTenant('smoke')
    expect(recentWorkflows(), 'smoke was offered eng’s workflows').toEqual([])
    // The page heading is seeded from here: smoke must not name eng's workflow.
    expect(recentName('wf_eng1')).toBeNull()
    rememberWorkflow('wf_smoke1', null, 'smoke-flow')
    expect(recentWorkflows().map((w) => w.id)).toEqual(['wf_smoke1'])
    chooseTenant('eng')
    expect(recentWorkflows().map((w) => w.id)).toEqual(['wf_eng1'])
  })

  it('redraws the panel’s Recent on a switch', () => {
    engList()
    chooseTenant('eng')
    const { container } = shell()
    const recent = () => container.querySelector<HTMLElement>('[aria-label="Recent workflows"]')
    expect(within(recent()!).getByText('phone-bottom-bar')).toBeTruthy()
    act(() => chooseTenant('smoke'))
    expect(recent(), 'the panel still lists eng’s workflows under smoke').toBeNull()
    act(() => chooseTenant('eng'))
    expect(within(recent()!).getByText('phone-bottom-bar')).toBeTruthy()
  })
})

/** A scroller jsdom gives no layout: a settable `scrollTop` over a tall page. */
function scrollable(container: HTMLElement): HTMLElement {
  const scroller = container.querySelector<HTMLElement>('.ctl-scroll')!
  let top = 0
  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 10_000 })
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 800 })
  Object.defineProperty(scroller, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = Math.max(0, Math.min(v, 10_000 - 800))
    },
  })
  return scroller
}

describe('CH-5: the keyboard scrolls the page, and Back puts the scroll back', () => {
  it('puts focus inside the page scroller on load, so PageDown and Space reach it', () => {
    const { container } = shell()
    const scroller = container.querySelector('.ctl-scroll')!
    expect(document.activeElement, 'focus was left on <body>').not.toBe(document.body)
    expect(scroller.contains(document.activeElement), 'focus is outside the element that scrolls').toBe(true)
  })

  it('restores an address’s scroll on Back', () => {
    window.history.replaceState(null, '', '/a')
    const s = shell({ place: '/a' })
    const scroller = scrollable(s.container)
    scroller.scrollTop = 4000
    fireEvent.scroll(scroller)
    // Forward to another page, which starts where the content leaves it.
    window.history.pushState(null, '', '/b')
    s.again({ place: '/b' })
    scroller.scrollTop = 0
    fireEvent.scroll(scroller)
    // Back: the browser changes the address, then says so.
    window.history.replaceState(null, '', '/a')
    act(() => void window.dispatchEvent(new PopStateEvent('popstate')))
    s.again({ place: '/a' })
    expect(scroller.scrollTop).toBe(4000)
  })
})

describe('CH-15: the first Tab skips to the page, and a page change moves focus to it', () => {
  it('draws a skip link first, which lands on the page', () => {
    const { container } = shell()
    const first = container.querySelector<HTMLElement>('.sk-app a[href], .sk-app button')!
    expect(first.textContent).toMatch(/skip/i)
    act(() => first.focus())
    fireEvent.click(first)
    expect(container.querySelector('.ctl-scroll')!.contains(document.activeElement)).toBe(true)
  })

  it('moves focus from the clicked rail link to the new page, and leaves focus in the page alone', () => {
    const s = shell({ place: '/a' })
    const link = s.container.querySelector<HTMLElement>('.sk-spine [data-sec="capacity"]')!
    act(() => link.focus())
    s.again({ place: '/b', children: <h1>Capacity</h1> })
    expect(document.activeElement).toBe(s.container.querySelector('.ctl-scroll h1'))
    s.again({ place: '/b', children: <><h1>Capacity</h1><button type="button">filter</button></> })
    const filter = s.container.querySelector<HTMLElement>('.ctl-scroll button')!
    act(() => filter.focus())
    s.again({ place: '/b?state=failed', children: <><h1>Capacity</h1><button type="button">filter</button></> })
    expect(document.activeElement).toBe(filter)
  })
})

describe('CH-9: collapsing hands focus to the control that undoes it', () => {
  it('focuses Expand after Collapse, and Collapse after Expand', () => {
    const { container } = shell()
    const collapse = container.querySelector<HTMLButtonElement>('button[aria-label="Collapse the panel"]')!
    act(() => collapse.focus())
    fireEvent.click(collapse)
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Expand the panel')
    fireEvent.click(document.activeElement as HTMLElement)
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Collapse the panel')
  })
})

describe('CH-16: a document title names the page, not only its section', () => {
  const titled = (address: string, title: string) => documentTitleOf(title, documentObjectOf(fromAddress(address)))

  it('names the new-schedule form, a repository and a help page', () => {
    expect(titled('automate/schedules?schedule=new', 'Schedules')).toBe('New schedule · Schedules · SwarmCloud')
    expect(titled('work/repositories?repo=acme%2Fapi', 'Repositories')).toBe('acme/api · Repositories · SwarmCloud')
    const group = HELP_GROUPS[0]!
    expect(titled(`help/${group.id}`, 'Help')).toBe(`${group.title} · Help · SwarmCloud`)
    // Two different help pages are two different titles.
    expect(titled(`help/${HELP_GROUPS[1]!.id}`, 'Help')).not.toBe(titled(`help/${group.id}`, 'Help'))
  })
})
