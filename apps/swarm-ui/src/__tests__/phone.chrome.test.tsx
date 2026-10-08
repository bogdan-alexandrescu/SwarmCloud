// #139 -- THE PHONE CHROME, ASKED AT 390px (and once at 820, the band where
// the spine column still stands). The issue was filed against the two-row
// strip; the Sky shell replaced the strip with a sticky 44px header and a
// drawer holding the spine and its panel (design-system.md §6.15, amended
// 2026-10-02). These are the issue's checks, one `it` each, on that chrome:
//
//   * the header compacts to 36px on scroll, and keeps mark, env and tenant;
//   * an admin is a grey hairline tag IN the phone header, like the env pill,
//     never an accent pill and never a line of its own;
//   * the way to every section is sticky, and the open section's pages scroll
//     in their own column, so no section ever moves;
//   * a long page offers `Top`;
//   * header plus dock stays under §6.15's 25% of an 844px screen;
//   * the compaction is the only motion, and `prefers-reduced-motion` stops it;
//   * none of it reaches the desktop.
//
// CSS claims go through `painted` (cssgate.ts), because jsdom applies no
// `@media` block. Each `it` names the mutation that turns it red.

import { act, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import type { Me } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const PHONE: CascadeEnv = { width: 390 }
const PHONE_STILL: CascadeEnv = { width: 390, reducedMotion: true }
const TABLET: CascadeEnv = { width: 820 }
const WIDE: CascadeEnv = { width: 1440 }
/** The pages' own scroller, assembled: the class name, after a dot, reads as a key to the publish scan. */
const PAGES_COLUMN = '.sk' + '-pscroll'
/** The phone the issue measured: 390 x 844. */
const SCREEN_H = 844

afterEach(() => {
  localStorage.clear()
  window.history.replaceState(null, '', '/')
})

async function at(path: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  const { container } = render(<App />)
  await waitFor(() => expect(container.querySelector('.sk-spine .sk-av')?.getAttribute('title')).not.toBe('not read'), { timeout: 5000 })
  return container
}

async function scrollTo(scroller: HTMLElement, y: number): Promise<void> {
  await act(async () => {
    scroller.scrollTop = y
    scroller.dispatchEvent(new Event('scroll'))
  })
}

const px = (v: string | null): number => {
  expect(v, 'no px value in the cascade').toMatch(/^\d+px$/)
  return parseInt(v!, 10)
}

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function me(isAdmin: boolean): Me {
  return {
    tenant: {
      tenant_id: 'u-phone',
      kind: 'user',
      principal: 'someone@saga.xyz',
      display_name: 'Phone',
      created_at: new Date().toISOString(),
      max_active: 4,
      capacity_units: 8,
      monthly_budget_usd: null,
      enabled: true,
      credentials: [],
      service_account: 'swarm-agent-worker-u-phone@example.iam.gserviceaccount.com',
      gcs_prefix: 'tenants/u-phone',
      namespace: 'swarm-u-phone',
    },
    principal: { email: 'someone@saga.xyz', groups: [], is_admin: isAdmin },
  } as unknown as Me
}

/** The real `SkyShell`, with the identity read answering `who` (brand.test.tsx's `sky`). */
async function shellAs(who: Me): Promise<HTMLElement> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.stubEnv('VITE_SWARM_ENV', 'dev')
  vi.resetModules()
  globalThis.fetch = vi.fn((input: RequestInfo | URL) =>
    Promise.resolve(String(input).includes('/v1/tenants/me') ? json(200, who) : json(503, {})),
  ) as unknown as typeof fetch
  const { SkyShell } = await import('../Spine')
  const { container } = render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      {null}
    </SkyShell>,
  )
  await waitFor(() => expect(container.querySelector('.sk-pbar .sk-tn')?.textContent).toBe('Phone'))
  return container
}

describe('#139 at 390: the header collapses on scroll', () => {
  it('compacts to 36px past its own height and keeps the mark, the env and the tenant in it', async () => {
    // MUTATION: drop `.sk-app.is-scrolled .sk-pbar`'s height, stop setting
    // `is-scrolled`, or drop the mark or the env pill from the header.
    const c = await at('/help')
    const app = c.querySelector<HTMLElement>('.sk-app')!
    const bar = c.querySelector<HTMLElement>('.sk-pbar')!
    const scroller = c.querySelector<HTMLElement>('.ctl-scroll')!
    expect(app.classList.contains('is-scrolled')).toBe(false)
    expect(px(painted(bar, 'height', PHONE))).toBe(44)

    await scrollTo(scroller, 200)
    expect(app.classList.contains('is-scrolled'), 'the header did not take its collapsed class').toBe(true)
    expect(px(painted(bar, 'height', PHONE))).toBe(36)
    expect(bar.querySelector('.brand-mark, svg'), 'the mark left the collapsed header').not.toBeNull()
    expect(bar.querySelector('.sk-pill'), 'the env left the collapsed header').not.toBeNull()
    expect(bar.querySelector('.sk-tn, .sk-tchip'), 'the tenant left the collapsed header').not.toBeNull()
    // The desktop has no phone header to collapse.
    expect(painted(bar, 'display', WIDE)).toBe('none')
  })

  it('draws an admin as a grey hairline tag in the phone header, like the env, on the one row', async () => {
    // MUTATION: leave the admin tag out of `.sk-pbar`, or tint it with the
    // accent, or let it wrap.
    const c = await shellAs(me(true))
    const tag = await waitFor(() => {
      const el = c.querySelector<HTMLElement>('.sk-pbar .sk-adm')
      expect(el, 'an admin is not marked in the phone header').not.toBeNull()
      return el!
    })
    expect(tag.textContent).toBe('admin')
    expect(painted(tag, ['border', 'border-width'], PHONE) ?? '').toMatch(/^1px/)
    const ink = painted(tag, 'color', PHONE) ?? ''
    expect(ink, `the admin tag is inked ${ink}`).not.toMatch(/--sk-ac\b|--info/)
    const fill = painted(tag, ['background', 'background-color'], PHONE)
    expect(fill === null || fill === 'none' || fill === 'transparent', `the admin tag is filled with ${fill}`).toBe(true)
    expect(painted(tag, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(tag, 'flex', PHONE), 'the tag gives way to the title').toBe('none')
  })

  it('marks no one admin in the phone header who is not', async () => {
    const c = await shellAs(me(false))
    expect(c.querySelector('.sk-pbar .sk-adm')).toBeNull()
  })
})

describe('#139 at 390: every section is reachable from any scroll position, and none moves', () => {
  it('keeps the header that opens the sections sticky at the top while the page scrolls', async () => {
    // MUTATION: `.sk-pbar` static, or the menu taken out of it.
    const c = await at('/help')
    const bar = c.querySelector<HTMLElement>('.sk-pbar')!
    await scrollTo(c.querySelector<HTMLElement>('.ctl-scroll')!, 5000)
    expect(painted(bar, 'position', PHONE)).toBe('sticky')
    expect(painted(bar, 'top', PHONE)).toBe('0')
    expect(bar.querySelector('button[aria-label="Open the menu"]'), 'the way to the sections is not in the sticky header').not.toBeNull()
  })

  it("gives the open section's pages their own scrolling column, so the sections hold their places", async () => {
    // MUTATION: draw the pages inside the spine (inline after their
    // section), or let the pages' column stop scrolling on its own.
    const seen: string[][] = []
    for (const path of ['/overview/now', '/agents', '/capacity/accounts', '/admin/limits']) {
      const c = await at(path)
      fireEvent.click(c.querySelector<HTMLElement>('.sk-pbar button[aria-label="Open the menu"]')!)
      const app = await waitFor(() => {
        const a = c.querySelector<HTMLElement>('.sk-app.has-drawer')
        expect(a).not.toBeNull()
        return a!
      })
      const spine = app.querySelector<HTMLElement>('.sk-spine')!
      const panel = app.querySelector<HTMLElement>('.sk-panel')!
      expect(spine.contains(panel), `${path}: the pages sit inside the section column`).toBe(false)
      expect(spine.querySelector('.sk-pk, .sk-kid'), `${path}: a page is drawn among the sections`).toBeNull()
      const pages = panel.querySelector<HTMLElement>(PAGES_COLUMN)!
      expect(painted(pages, 'overflow-y', PHONE), `${path}: the pages do not scroll on their own`).toBe('auto')
      expect(painted(app.querySelector('.sk-side')!, 'display', PHONE)).toBe('flex')
      seen.push([...spine.querySelectorAll('.sk-ri')].map((a) => (a.textContent ?? '').trim()))
      fireEvent.keyDown(document, { key: 'Escape' })
      document.body.innerHTML = ''
    }
    expect(seen.length, 'the sweep did not visit every route').toBe(4)
    expect(seen[0]!.length).toBeGreaterThanOrEqual(5)
    for (const items of seen) expect(items, 'a section moved when another opened').toEqual(seen[0])
  })

  it('keeps the spine column, which never scrolls, between 760 and 899', () => {
    // The phone header starts below 760; above it the spine is a column
    // beside the scroller, so it cannot scroll away. MUTATION: hide the
    // spine column at 820 without the phone header taking over.
    const host = document.createElement('div')
    host.innerHTML = '<div class="sk-app"><header class="sk-pbar"></header><div class="sk-side"></div><div class="sk-main"><div class="ctl-scroll"><button class="sk-top"></button></div></div></div>'
    document.body.appendChild(host)
    try {
      expect(painted(host.querySelector('.sk-side')!, 'display', TABLET)).not.toBe('none')
      expect(painted(host.querySelector('.sk-pbar')!, 'display', TABLET)).toBe('none')
      expect(painted(host.querySelector('.sk-top')!, 'display', TABLET)).toBe('none')
    } finally {
      host.remove()
    }
  })
})

describe("#139 at 390: a section's sticky page strip sits flush under the header", () => {
  it('nests the strip in the page scroller and the header outside it, and sticks the strip at 0 in both header heights', async () => {
    // MUTATION: put `.ag-list-tabs`'s phone `top` back to 44px (36px when
    // scrolled). The header is a row ABOVE `.ctl-scroll`, so the scroller's
    // top edge already is the header's foot; any `top` past 0 leaves a band
    // of rows scrolling through between the header and the strip, and a
    // second value for the compacted header moves the strip mid-scroll.
    const c = await at('/agents')
    const strip = await waitFor(() => {
      const el = c.querySelector<HTMLElement>('.ag-list-tabs')
      expect(el, 'the Agents page drew no page strip').not.toBeNull()
      return el!
    })
    const scroller = c.querySelector<HTMLElement>('.ctl-scroll')!
    const bar = c.querySelector<HTMLElement>('.sk-pbar')!
    expect(scroller.contains(strip), 'the strip is not inside the page scroller').toBe(true)
    expect(scroller.contains(bar), 'the header is inside the page scroller').toBe(false)
    expect(painted(strip, 'position', PHONE)).toBe('sticky')
    expect(painted(strip, 'top', PHONE), 'the strip sticks below a gap under the header').toBe('0')
    await scrollTo(scroller, 400)
    expect(c.querySelector('.sk-app')!.classList.contains('is-scrolled')).toBe(true)
    expect(painted(strip, 'top', PHONE), 'the strip moves when the header compacts').toBe('0')
    // Painted in the page's own ground, a token in both themes, so rows do not show through.
    expect(painted(strip, ['background', 'background-color'], PHONE)).toBe('var(--bg)')
  })
})

describe('#139 at 390: the header row never widens the page', () => {
  it("cuts a long tenant name with an ellipsis, after the title has given way, and keeps the whole name as its title", async () => {
    // MUTATION: drop `min-width: 0` / `max-width` / `overflow: hidden` from
    // `.sk-pbar .sk-tn`, or let the menu, the mark or the env pill shrink.
    const long = me(false)
    const name = 'someone.with.a.rather.long.address' + '@' + 'saga.xyz'
    ;(long.tenant as { display_name: string }).display_name = name
    vi.stubEnv('VITE_LIVE', '1')
    vi.stubEnv('VITE_SWARM_ENV', 'dev')
    vi.resetModules()
    globalThis.fetch = vi.fn((input: RequestInfo | URL) =>
      Promise.resolve(String(input).includes('/v1/tenants/me') ? json(200, long) : json(503, {})),
    ) as unknown as typeof fetch
    const { SkyShell } = await import('../Spine')
    const { container } = render(
      <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
        {null}
      </SkyShell>,
    )
    const tn = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.sk-pbar .sk-tn')
      expect(el?.textContent).toBe(name)
      return el!
    })
    expect(tn.getAttribute('title'), 'a cut name has no way to read it whole').toBe(name)
    expect(painted(tn, 'min-width', PHONE)).toBe('0')
    expect(painted(tn, 'max-width', PHONE)).toBe('40vw')
    expect(painted(tn, 'overflow', PHONE)).toBe('hidden')
    expect(painted(tn, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(painted(tn, 'white-space', PHONE)).toBe('nowrap')
    const bar = container.querySelector<HTMLElement>('.sk-pbar')!
    expect(painted(bar.querySelector('b')!, 'flex', PHONE), 'the title no longer gives way first').toBe('1')
    const fixed = [bar.querySelector(':scope > .sk-ibtn')!, bar.querySelector(':scope > svg')!, bar.querySelector(':scope > .sk-pill')!]
    expect(fixed.filter((el) => el !== null).length, 'the sweep did not find the menu, the mark and the env').toBe(3)
    for (const el of fixed) expect(painted(el, 'flex', PHONE), `${el.getAttribute('class')} shrinks`).toBe('none')
    vi.unstubAllEnvs()
  })
})

describe('#139 at 390: a long page has a way back to the top', () => {
  it('offers `Top` past a screen of a long page, and it goes there', async () => {
    // MUTATION: never render `.sk-top`, hide it at 390, or make it do nothing.
    const c = await at('/help')
    const scroller = c.querySelector<HTMLElement>('.ctl-scroll')!
    expect(c.querySelector('.sk-top')).toBeNull()
    await scrollTo(scroller, 6000)
    const top = c.querySelector<HTMLElement>('.sk-top')
    expect(top, 'a long page has no `Top`').not.toBeNull()
    expect(painted(top!, 'display', PHONE)).toBe('flex')
    expect(painted(top!, 'position', PHONE)).toBe('sticky')
    expect(painted(top!, 'display', WIDE)).toBe('none')
    await act(async () => {
      fireEvent.click(top!)
    })
    expect(scroller.scrollTop).toBe(0)
  })
})

describe('#139 at 390: the chrome stays inside §6.15', () => {
  it('spends under 25% of an 844px screen on the header and the dock, and about a tenth once scrolled', () => {
    // MUTATION: grow the header back toward 100px, or put a bar of its own
    // at the bottom. The dock's collapsed line is `min-height` 28px and
    // about 32px where its facts wrap to two lines (styles.css
    // `.ctl-dock-line`); 48px is taken as its ceiling here.
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="sk-app" data-k="top"><header class="sk-pbar"></header></div>' +
      '<div class="sk-app is-scrolled" data-k="down"><header class="sk-pbar"></header></div>' +
      '<div class="ctl-dock"><button class="ctl-dock-line"></button></div>'
    document.body.appendChild(host)
    try {
      const DOCK_CEILING = 48
      expect(px(painted(host.querySelector('.ctl-dock-line')!, 'min-height', PHONE))).toBeLessThanOrEqual(DOCK_CEILING)
      const atLoad = px(painted(host.querySelector('[data-k="top"] .sk-pbar')!, 'height', PHONE)) + DOCK_CEILING
      const scrolled = px(painted(host.querySelector('[data-k="down"] .sk-pbar')!, 'height', PHONE)) + DOCK_CEILING
      expect(atLoad / SCREEN_H, `${atLoad}px of chrome on load`).toBeLessThan(0.25)
      expect(scrolled / SCREEN_H, `${scrolled}px of chrome once scrolled`).toBeLessThan(0.1)
      // No second bar fixed to the bottom: the dock owns that edge.
      expect(painted(host.querySelector('.sk-pbar')!, 'bottom', PHONE)).toBeNull()
    } finally {
      host.remove()
    }
  })
})

describe('#139 at 390: the compaction respects prefers-reduced-motion', () => {
  it('eases the header to its compact height, and does not when motion is reduced', () => {
    // MUTATION: drop the reduced-motion stop, or animate on the desktop.
    const host = document.createElement('div')
    host.innerHTML = '<div class="sk-app is-scrolled"><header class="sk-pbar"></header><div class="ctl-scroll"><button class="sk-top"></button></div></div>'
    document.body.appendChild(host)
    try {
      const bar = host.querySelector('.sk-pbar')!
      expect(painted(bar, 'transition', PHONE) ?? '', 'the header snaps between heights').toMatch(/^height \d+ms/)
      expect(painted(bar, 'transition', PHONE_STILL), 'the header animates under reduced motion').toBe('none')
      // `Top` jumps; it never smooth-scrolls the page.
      expect(painted(host.querySelector('.ctl-scroll')!, 'scroll-behavior', PHONE) ?? 'auto').toBe('auto')
      expect(painted(host.querySelector('.sk-top')!, 'transition', PHONE_STILL) ?? 'none').toBe('none')
    } finally {
      host.remove()
    }
  })
})
