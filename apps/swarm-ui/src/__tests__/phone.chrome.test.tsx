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

import AGENTS_CSS from '../styles/agents.css?raw'
import STYLES from '../styles.css?raw'

import { App } from '../App'
import type { Result } from '../fetch'
import type { Me, TaskPage } from '../types'
import { cascade, type CascadeEnv, type Declared } from './cssgate'
import { expandLocal, painted, TABLES } from './marks'
import { task } from './runfixture'
import { resolveVars } from './spaceprobe'

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

async function at(path: string, Root: typeof App = App): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  const { container } = render(<Root />)
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
      namespace: 'swarm-tenant-u-phone',
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

// THE ISSUE'S OWN CHECK, AS A VERTICAL BUDGET. jsdom does no layout and the
// Chromium suite (scripts/ui-test.sh) is not in CI, so the first row's y is
// added up from the cascade: every box that stacks above the first Agents row
// at 390, walked off the DOM from `.sk-app` down, each sized from what the
// sheet declares for it there. The sum is an UPPER BOUND on the row's y, never
// an estimate below it:
//
//   * a wrapping flex row is taken as fully wrapped, one item per line, the
//     tallest arrangement it can take whatever its widths come to;
//   * adjacent margins are added, never collapsed, and negative ones are
//     dropped;
//   * a line of text is `line-height`, and a block of prose is as many lines
//     as its characters need at 0.6em each (DM Mono's advance; Inter Tight's
//     is narrower) across the page's width.
//
// Everything above `.sk-app` is `body`'s `env(safe-area-inset-top)`, which is
// 0 in a browser tab -- the 390x844 the issue measured.
const BUDGET_SHEETS = [
  ...Object.values(import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true })),
  STYLES,
].join('\n')
/** The glyph allowance per character, in em. */
const ADVANCE_EM = 0.6

function won(el: Element, props: string | readonly string[]): Declared | null {
  const r = cascade(BUDGET_SHEETS, el, props, PHONE)
  if (r.unsupported.length > 0) throw new Error(`the resolver could not evaluate ${r.unsupported.join(', ')}`)
  return r.winner === null ? null : { ...r.winner, value: resolveVars(expandLocal(r.winner.value, el, PHONE), TABLES.dark) }
}

const label = (el: Element): string => `${el.tagName.toLowerCase()}${el.classList.length > 0 ? '.' + [...el.classList].join('.') : ''}`

/** A length as px. `0` or `<n>px` only: a `calc`, an `em`, a `%` or an unresolved var throws, so it can never count as 0. */
function len(v: string, what: string): number {
  const m = /^(-?\d+(?:\.\d+)?)(px)?$/.exec(v.trim())
  if (m === null || (m[2] === undefined && parseFloat(m[1]!) !== 0)) throw new Error(`${what} is ${JSON.stringify(v)}, not a px length`)
  return parseFloat(m[1]!)
}

/** One side of `padding`/`margin`, from the longhand or the shorthand. Undeclared is the initial value, 0. */
function side(el: Element, box: 'padding' | 'margin', at: 'top' | 'bottom' | 'left'): number {
  const w = won(el, [`${box}-${at}`, box])
  if (w === null) return 0
  if (w.property !== box) return len(w.value, `${label(el)} ${w.property}`)
  const v = w.value.trim().split(/\s+/)
  const i = { top: 0, left: v.length === 4 ? 3 : 1, bottom: v.length >= 3 ? 2 : 0 }[at]
  return len(v[i] ?? v[0]!, `${label(el)} ${box}`)
}

/** The width of one horizontal border, from whichever of its four spellings won. */
function borderOf(el: Element, at: 'top' | 'bottom'): number {
  const w = won(el, [`border-${at}-width`, `border-${at}`, 'border-width', 'border'])
  if (w === null) return 0
  const parts = w.value.trim().split(/\s+/)
  if (w.property.endsWith('width')) return len(w.property === 'border-width' && at === 'bottom' && parts.length >= 3 ? parts[2]! : parts[0]!, `${label(el)} ${w.property}`)
  const width = parts.find((t) => /^-?\d/.test(t))
  if (width !== undefined) return len(width, `${label(el)} ${w.property}`)
  if (parts.includes('none') || parts.includes('0')) return 0
  throw new Error(`${label(el)} ${w.property}: ${w.value} names no width`)
}

/** The inherited font size and line height, px. A `normal` line height throws. */
function fontOf(el: Element): { size: number; line: number } {
  let size: number | null = null
  let line: string | null = null
  for (let n: Element | null = el; n !== null && (size === null || line === null); n = n.parentElement) {
    if (size === null) {
      const w = won(n, ['font', 'font-size'])
      if (w !== null) {
        const m = w.property === 'font' ? /(\d+(?:\.\d+)?px)\s*(?:\/|\s)/.exec(w.value) : null
        size = len(w.property === 'font' ? (m?.[1] ?? 'none') : w.value, `${label(n)} ${w.property} size`)
      }
    }
    if (line === null) {
      const w = won(n, ['font', 'line-height'])
      if (w !== null) line = w.property === 'font' ? (/\/\s*(\S+)/.exec(w.value)?.[1] ?? 'normal') : w.value.trim()
    }
  }
  if (size === null || line === null) throw new Error(`${label(el)} inherits no font size or line height`)
  const unitless = /^\d+(?:\.\d+)?$/.test(line)
  return { size, line: unitless ? parseFloat(line) * size : len(line, `${label(el)} line-height`) }
}

const BLOCK_TAGS = new Set(['div', 'p', 'main', 'header', 'nav', 'section', 'h1', 'h2', 'h3', 'ul', 'ol', 'li', 'form'])
const displayOf = (el: Element): string =>
  won(el, 'display')?.value.trim() ?? (BLOCK_TAGS.has(el.tagName.toLowerCase()) ? 'block' : 'inline')

/** The tallest the cascade lets `el`'s border box be at 390, px. */
function boxHeight(el: Element, width: number): number {
  const display = displayOf(el)
  if (display === 'none') return 0
  expect(won(el, 'box-sizing')?.value, `${label(el)} is not border-box`).toBe('border-box')
  const height = won(el, 'height')
  if (height !== null && height.value !== 'auto') return len(height.value, `${label(el)} height`)
  const min = won(el, 'min-height')
  const floor = min === null || min.value === 'auto' ? 0 : len(min.value, `${label(el)} min-height`)
  const edges = side(el, 'padding', 'top') + side(el, 'padding', 'bottom') + borderOf(el, 'top') + borderOf(el, 'bottom')
  const kids = [...el.children]
  // A flex or grid container's children are its items whatever their own
  // display says; any other box whose children are all inline is a run of text.
  const container = /(flex|grid)$/.test(display)
  const leaf = ['input', 'select', 'textarea'].includes(el.tagName.toLowerCase()) || (!container && kids.every((k) => displayOf(k).startsWith('inline')))
  let content: number
  if (leaf) {
    const { size, line } = fontOf(el)
    const text = (el.textContent ?? '').trim()
    const nowrap = /^(nowrap|pre)$/.test(won(el, 'white-space')?.value.trim() ?? 'normal')
    const formField = el.tagName.toLowerCase() !== 'button' && (el instanceof HTMLInputElement || el instanceof HTMLSelectElement)
    let lines = formField ? 1 : text === '' ? 0 : nowrap ? 1 : Math.ceil((text.length * ADVANCE_EM * size) / width)
    const clamp = won(el, ['-webkit-line-clamp', 'line-clamp'])
    if (clamp !== null && /^\d+$/.test(clamp.value.trim())) lines = Math.min(lines, parseInt(clamp.value, 10))
    // An inline-block child (a badge, the `?` glyph) can stand taller than the line.
    content = Math.max(lines * line, ...kids.filter((k) => displayOf(k) !== 'inline').map((k) => outerHeight(k, width)))
  } else {
    const heights = kids.map((k) => outerHeight(k, width))
    const flex = /flex$/.test(display)
    const row = flex && !/^column/.test(won(el, 'flex-direction')?.value.trim() ?? 'row')
    const wraps = row && /^wrap/.test(won(el, 'flex-wrap')?.value.trim() ?? 'nowrap')
    const gapDecl = won(el, ['row-gap', 'gap'])
    const gap = gapDecl === null ? 0 : len(gapDecl.property === 'gap' ? gapDecl.value.trim().split(/\s+/)[0]! : gapDecl.value, `${label(el)} ${gapDecl.property}`)
    if (row && !wraps) content = Math.max(0, ...heights)
    else content = heights.reduce((a, b) => a + b, 0) + (container ? gap * Math.max(0, heights.length - 1) : 0)
  }
  return Math.max(floor, content + edges)
}

/** The border box plus its vertical margins, or 0 for a box out of the flow. */
function outerHeight(el: Element, width: number): number {
  if (displayOf(el) === 'none' || /^(absolute|fixed)$/.test(won(el, 'position')?.value.trim() ?? 'static')) return 0
  return Math.max(0, side(el, 'margin', 'top')) + boxHeight(el, width) + Math.max(0, side(el, 'margin', 'bottom'))
}

describe('#139 at 390: the first Agents row starts above the midpoint', () => {
  afterEach(() => {
    vi.doUnmock('../api')
    vi.resetModules()
  })

  it('stacks less than half of an 844px screen above the first row, every block resolved to px and counted', async () => {
    // MUTATION: set `.sk-pbar`'s phone height back to the old header's 100px.
    // The budget is 368.2px today (measured 2026-10-08); the 56px that adds
    // takes it to 424.2, past the midpoint. The list toolbar, taken fully
    // wrapped, is the largest block at 161.4px.
    expect(BUDGET_SHEETS.includes(AGENTS_CSS), 'the Agents sheet is not in the cascade').toBe(true)
    const live = task({ id: 'task_phone139000000000000a', state: 'RUNNING', started_at: new Date().toISOString(), attempt_count: 1, max_attempts: 3 })
    const page = { status: 'ok', data: { tasks: [live], tenant_id: 'acme' }, fetchedAt: Date.now() } satisfies Result<TaskPage>
    // The agents read answers this test alone: a scoped mock and a fresh App,
    // since a file-wide `vi.mock` outlives `shellAs`'s `resetModules`.
    vi.resetModules()
    vi.doMock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), loadTasks: () => Promise.resolve(page) }))
    const { App: Stubbed } = await import('../App')
    const c = await at('/agents', Stubbed)
    const row = await waitFor(() => {
      const el = c.querySelector<HTMLElement>('.rows > .row')
      expect(el, 'the Agents page drew no row').not.toBeNull()
      return el!
    })
    expect(row.textContent, 'the first row is not the stubbed live agent').toContain('phone139')
    const app = c.querySelector<HTMLElement>('.sk-app')!
    expect(app.contains(row)).toBe(true)

    // Text wraps across the page's own width, inside `.app`'s side padding.
    const pageBox = c.querySelector<HTMLElement>('.app')!
    const width = 390 - side(pageBox, 'padding', 'left') * 2

    // Up from the row: each ancestor's top padding and border, and every
    // sibling drawn before the branch that holds the row.
    const blocks: { what: string; px: number }[] = [{ what: `${label(row)} margin-top`, px: Math.max(0, side(row, 'margin', 'top')) }]
    const above: string[] = []
    for (let node: HTMLElement = row; node !== app; node = node.parentElement!) {
      const parent = node.parentElement!
      for (let sib = node.previousElementSibling; sib !== null; sib = sib.previousElementSibling) {
        blocks.push({ what: label(sib), px: outerHeight(sib, width) })
        above.push(`.${[...sib.classList][0] ?? sib.tagName.toLowerCase()}`)
      }
      blocks.push({ what: `${label(parent)} top padding + border`, px: side(parent, 'padding', 'top') + borderOf(parent, 'top') })
    }
    blocks.reverse()
    above.reverse()

    // The boxes the issue names, in the order they stack: the header, the
    // drawer (out of the flow until opened), the page head, the count, the
    // tabs strip and the list's own toolbar. A box added above the first row
    // fails here before it can slip into the sum unnoticed.
    expect(above).toEqual(['.sk-pbar', '.sk-side', '.c-phead', '.c-count-note', '.ag-list-tabs', '.ctl-toolbar'])
    // The row's margin, 7 ancestors' top edges (.rows, its list, .work, .app,
    // .ctl-scroll, .sk-main, .sk-app) and the 6 boxes above.
    expect(blocks.length, 'the walk did not visit every block above the row').toBe(14)
    for (const b of blocks) expect(Number.isFinite(b.px) && b.px >= 0, `${b.what} came to ${b.px}`).toBe(true)
    // The ones that carry the budget must have come to something.
    for (const what of ['header.sk-pbar', 'div.c-phead', 'div.ag-list-tabs']) {
      expect(blocks.find((b) => b.what === what)?.px ?? 0, `${what} counted as nothing`).toBeGreaterThan(0)
    }

    const total = blocks.reduce((a, b) => a + b.px, 0)
    const breakdown = blocks.map((b) => `  ${b.px.toFixed(1).padStart(6)}px  ${b.what}`).join('\n')
    expect(total, `the first Agents row starts by ${total.toFixed(1)}px of ${SCREEN_H}:\n${breakdown}`).toBeLessThan(SCREEN_H / 2)
  })
})
