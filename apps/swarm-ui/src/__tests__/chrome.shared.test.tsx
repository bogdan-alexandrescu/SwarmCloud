// THE SHARED CHROME AND ITS VOCABULARY, AS BEHAVIOUR -- the chrome-shared
// lane of the 2026-09-25 QA decisions (#87 CH-2, CH-13, CH-17, CH-21, CH-22).
//
// Each `describe` is one box, and each `it` names the mutation that turns it
// red. CSS claims are asked of `cascade` (cssgate.ts) through `marks.ts`, not
// of `getComputedStyle`: jsdom orders rules by source position alone and
// applies no `@media` block, and three of these boxes are phone rules.
//
// WHAT NONE OF THIS CAN SEE: a pixel. A sticky column, a two-row strip and a
// flat bar are asserted as the rules a browser would choose; how they look at
// 390 is for the next release's screenshots.

import STYLES from '../styles.css?raw'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App, SECTIONS } from '../App'
import { Chip } from '../AgentDetail'
import { Dock } from '../Dock'
import * as Workflows from '../Workflows'
import { stateTone, type Me } from '../types'
import { cascade, declarations, flatRules, splitTop, type CascadeEnv } from './cssgate'
import { build, painted, shapeOf } from './marks'
import { noteProbe } from './probes'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  window.location.hash = ''
})

/** A fixture to ask the cascade about. Attached, so nothing about it is special. */
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

/** The value the cascade chooses, local custom properties expanded. */
function won(el: Element, prop: string | readonly string[], env: CascadeEnv, pseudo: string | null = null): string | null {
  return painted(el, prop, env, pseudo)
}

// ===========================================================================
// CH-2 -- the head's read age is the screen's own
// ===========================================================================

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const ME: Me = {
  tenant: {
    tenant_id: 'u-bogdan',
    kind: 'user',
    principal: 'someone@saga.xyz',
    display_name: 'Bogdan',
    created_at: '2026-09-01T00:00:00Z',
    max_active: 4,
    capacity_units: 8,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: null,
    gcs_prefix: 'tenants/u-bogdan',
    namespace: 'swarm-u-bogdan',
  },
  principal: { email: 'someone@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: false,
}

/**
 * The app against a stubbed API, on the LIVE path. The fixture path answers
 * from timers inside api.ts, so it cannot hold one read open while another
 * lands -- which is the whole of CH-2's defect. `VITE_LIVE` turns the fixtures
 * off, and `resetModules` makes api.ts read it again.
 *
 * The frame's identity read answers at once; the screen's read waits for
 * `release`; everything else never answers.
 */
async function liveApp(hash: string): Promise<{ release: (res: Response) => Promise<void> }> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const gate: { answer?: (r: Response) => void } = {}
  const screenRead = new Promise<Response>((resolve) => {
    gate.answer = resolve
  })
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/v1/tenants/me')) return json(ME)
    if (url.startsWith('/v1/admin/tenants')) return screenRead
    return new Promise<Response>(() => {})
  }) as unknown as typeof fetch
  window.location.hash = hash
  const { App: LiveApp } = await import('../App')
  render(<LiveApp />)
  // The FRAME's read has landed: the tab now holds a successful read that is
  // not the screen's.
  await screen.findByText('u-bogdan')
  return {
    release: async (res: Response) => {
      await act(async () => {
        gate.answer?.(res)
      })
    },
  }
}

const headAge = (): string => document.querySelector('.ctl-head-age')?.textContent ?? ''
const dockLine = (): string => document.querySelector('.ctl-dock-line')?.textContent ?? ''

describe("CH-2: the head's read age is the screen's own", () => {
  it('says "reading…" while its screen loads, even after the frame\'s own read has landed', async () => {
    // THE DEFECT: the head's "newest read" was the newest success of ANY route
    // in the tab, so it said "just now" beside a page that was still loading.
    // MUTATION: read the age off the tab-wide registry again.
    const { release } = await liveApp('#admin/tenants')
    expect(headAge(), 'the head claims an age for a screen that has read nothing').toMatch(/reading…/)
    expect(headAge()).not.toMatch(/newest read/)
    // The dock keeps the tab-wide view, and there IS a tab-wide read.
    expect(dockLine()).toMatch(/newest/)

    await release(json({ tenants: [] }))
    await waitFor(() => expect(headAge()).toMatch(/newest read/))
  })

  it('says "not read" when its screen\'s read failed, and never shows the frame\'s age', async () => {
    // MUTATION: fall back to the tab-wide age when the screen has none.
    const { release } = await liveApp('#admin/tenants')
    await release(json({ message: 'A service the API depends on did not answer.' }, 503))
    await waitFor(() => expect(headAge()).toMatch(/not read/))
    expect(headAge()).not.toMatch(/newest read|\d/)
    expect(dockLine(), 'the dock is the tab-wide view').toMatch(/newest/)
  })
})

// ===========================================================================
// CH-17 -- the dock's read cells, and the ok and info marks
// ===========================================================================

describe("CH-17: a read cell is a row in the dock's panel, not a box", () => {
  it('draws no border, radius or fill on a cell', () => {
    // §13.3: the dock is the panel and each cell is a row, so a cell draws
    // nothing. MUTATION: any of the four back on `.source`.
    const own = flatRules(STYLES).filter((r) => splitTop(r.selector).includes('.source'))
    expect(own.length, 'no rule for .source; the check would be vacuous').toBeGreaterThan(0)
    for (const r of own) {
      for (const d of declarations(r.body)) {
        expect(d.property, `\`${r.selector}\` declares ${d.property}: ${d.value}`).not.toMatch(
          /^(border(-.+)?|background(-color)?)$/,
        )
      }
    }
    // With no edge, the gap is what separates two cells.
    const f = fragment('<div class="source-cells"><div class="source ok"></div></div>')
    expect(won(pick(f, '.source-cells'), ['gap', 'row-gap'], WIDE)).toBe('var(--ctl-s3)')
  })

  it("gives a failing cell the table row's own edge: full height for bad, half for warn", () => {
    // Joined to the `.ctl-table` row-tone rules rather than restated.
    // MUTATION: drop `.source.bad` or `.source.warn` from those selector lists.
    const f = fragment(
      '<div class="source bad"></div><div class="source warn"></div>' +
        '<div class="ctl-table"><table><tbody><tr class="is-bad"><td>a</td></tr><tr class="is-warn"><td>b</td></tr></tbody></table></div>',
    )
    for (const [cell, row] of [
      ['.source.bad', 'tr.is-bad > td'],
      ['.source.warn', 'tr.is-warn > td'],
    ] as const) {
      for (const prop of ['background-image', 'background-size', 'background-position', 'background-repeat']) {
        expect(won(pick(f, cell), prop, WIDE), `${cell} ${prop}`).toBe(won(pick(f, row), prop, WIDE))
      }
    }
    expect(won(pick(f, '.source.bad'), 'background-image', WIDE)).toContain('var(--bad)')
  })

  it('keeps the ok disc and the info flat bar apart once the colour is gone', () => {
    // Only the chips were covered before. MUTATION: `.ctl-dot.is-info` a disc.
    const ok = shapeOf(build('.ctl-dot.is-ok', hosts), WIDE)
    const info = shapeOf(build('.ctl-dot.is-info', hosts), WIDE)
    expect(info, 'the ok dot and the info dot are one shape in greyscale').not.toBe(ok)
  })

  it("draws the collapsed line's mark with the shared dot: an expired session is the diamond", () => {
    // The private `.ctl-dock-dot` drew an expired session as a RING, which is
    // the unknown silhouette. MUTATION: bring the private dot back.
    noteProbe('/v1/capacity', 12, false, 'unauthenticated')
    const { container } = render(<Dock />)
    const line = container.querySelector('.ctl-dock-line')
    expect(line?.querySelector('.ctl-dot.is-bad'), 'an expired session is not the failure diamond').not.toBeNull()
    expect(container.querySelector('.ctl-dock-dot'), 'the private dock dot is back').toBeNull()
  })

  it('draws a clean strip with the neutral disc and failures with the triangle', () => {
    noteProbe('/v1/capacity', 12, true)
    const clean = render(<Dock />)
    expect(clean.container.querySelector('.ctl-dock-line .ctl-dot.is-ok')).not.toBeNull()
    clean.unmount()
    noteProbe('/v1/stats', 12, false, 'upstream_degraded')
    const failing = render(<Dock />)
    expect(failing.container.querySelector('.ctl-dock-line .ctl-dot.is-warn')).not.toBeNull()
  })
})

// ===========================================================================
// CH-22 -- CANCELLED ends; it does not wait
// ===========================================================================

describe('CH-22: a cancelled task ends, in the neutral flat bar', () => {
  it('gives CANCELLED its own tone, and leaves the waiting states theirs', () => {
    // MUTATION: `stateTone('CANCELLED')` back to 'wait'.
    expect(stateTone('CANCELLED')).toBe('ended')
    for (const s of ['QUEUED', 'READY', 'PARKED'] as const) expect(stateTone(s), s).toBe('wait')
    expect(stateTone('SUCCEEDED')).toBe('ok')
    expect(stateTone('FAILED')).toBe('bad')
    expect(stateTone('RUNNING')).toBe('live')
  })

  it('draws a cancelled chip as the flat bar and a queued one as the wait mark', () => {
    // Agents drew CANCELLED as the caution TRIANGLE. MUTATION: map 'ended' to
    // 'warn' in `chipTone`, or give it a modifier of its own.
    const { container } = render(
      <>
        <Chip tone={stateTone('CANCELLED')}>CANCELLED</Chip>
        <Chip tone={stateTone('QUEUED')}>QUEUED</Chip>
      </>,
    )
    const [cancelled, queued] = [...container.querySelectorAll('.ctl-chip')]
    expect(cancelled?.className, 'CANCELLED is not the one info modifier').toBe('ctl-chip is-info')
    expect(queued?.className, 'QUEUED lost its wait mark').toBe('ctl-chip is-warn')
  })

  it('makes the bare info dot the flat bar §6.6 specifies', () => {
    // It was a filled disc, so at 390 -- where the word is hidden -- a
    // cancelled and a queued workflow were the same dot. MUTATION: the disc.
    const dot = build('.ctl-dot.is-info', hosts)
    const w = Number.parseFloat(won(dot, 'width', WIDE) ?? '')
    const h = Number.parseFloat(won(dot, 'height', WIDE) ?? '')
    expect(h, 'the flat bar is at most 3px tall').toBeLessThanOrEqual(3)
    expect(w, 'the flat bar is wider than it is tall').toBeGreaterThan(h * 2)
    expect(won(dot, ['border-width', 'border'], WIDE), 'the bar draws no ring').toMatch(/^(0|none)/)
  })

  it('draws a waiting workflow with the wait mark and a cancelled one with the ended bar', () => {
    // `dotClass` gave `wait` the info disc, which is now the ended bar -- so a
    // queued workflow and a cancelled one would share it. MUTATION: `wait`
    // back on `is-info`.
    const dotClass = (Workflows as unknown as { dotClass?: (h: { tone: string; derived: boolean }) => string })
      .dotClass
    expect(typeof dotClass, 'Workflows.tsx exports no dotClass').toBe('function')
    expect(dotClass!({ tone: stateTone('QUEUED'), derived: true })).toBe('ctl-dot is-warn')
    expect(dotClass!({ tone: stateTone('CANCELLED'), derived: true })).toBe('ctl-dot is-info')
    expect(dotClass!({ tone: stateTone('SUCCEEDED'), derived: true })).toBe('ctl-dot is-ok')
  })
})

// ===========================================================================
// CH-21 -- the strip below 900px is two rows
// ===========================================================================

/** Whether nothing on the way up to the rail is `display: none` at `env`. */
function shownAt(el: Element, env: CascadeEnv): boolean {
  for (let node: Element | null = el; node !== null; node = node.parentElement) {
    if (cascade(STYLES, node, 'display', env).winner?.value === 'none') return false
    if (node.classList.contains('ctl-rail')) return true
  }
  return true
}

describe('CH-21: below 900px the strip is two rows, and a position means one thing', () => {
  const ROUTES = SECTIONS.flatMap((s) => s.tabs.map((t) => `#${s.id}/${t.id}`))

  it('holds the same row-1 items, in the same order, on every section route', () => {
    // Row 1 is the sections and the utility corner. The open section's tabs
    // used to be inserted INLINE, so every section after it moved.
    // MUTATION: draw the open section's tabs inside row 1 again.
    expect(ROUTES.length, 'the sweep is not every section route').toBeGreaterThanOrEqual(15)
    let first: string[] | null = null
    for (const hash of ROUTES) {
      window.location.hash = hash
      const { container, unmount } = render(<App />)
      const main = container.querySelector('.ctl-rail-main')
      const items = main === null
        ? []
        : [...main.querySelectorAll('button')].filter((b) => shownAt(b, PHONE)).map((b) => (b.textContent ?? '').trim())
      unmount()
      expect(items.length, `${hash}: row 1 holds fewer than four sections and two utilities`).toBeGreaterThanOrEqual(6)
      if (first === null) first = items
      expect(items, `${hash}: row 1 is not the row every other route draws`).toEqual(first)
    }
  })

  it("draws row 2 only for a section with more than one tab, holding that section's tabs", () => {
    window.location.hash = '#overview/now'
    const over = render(<App />)
    expect(over.container.querySelector('.ctl-rail-sub'), 'Overview has one pane and draws no second row').toBeNull()
    over.unmount()

    window.location.hash = '#capacity/accounts'
    const { container } = render(<App />)
    const sub = container.querySelector('.ctl-rail-sub')
    expect(sub, 'no second row for Capacity').not.toBeNull()
    expect(sub!.getAttribute('role')).toBe('tablist')
    const tabs = [...sub!.querySelectorAll('[role="tab"]')].map((b) => (b.firstChild?.textContent ?? '').trim())
    const capacity = SECTIONS.find((s) => s.id === 'capacity')!
    expect(tabs).toEqual(capacity.tabs.map((t) => t.label))
    expect(sub!.querySelector('[aria-selected="true"]')?.firstChild?.textContent?.trim()).toBe('Accounts')
  })

  const STRIP =
    '<nav class="ctl-rail"><div class="ctl-rail-main"><div class="ctl-rail-sections">' +
    '<div class="ctl-rail-group is-on"><button class="ctl-nav-link is-on">Capacity</button>' +
    '<div class="ctl-rail-tabs" role="tablist"><button role="tab" aria-selected="true">Pools</button></div></div>' +
    '</div><div class="ctl-nav-util"><button>API reads</button></div></div>' +
    '<div class="ctl-rail-tabs ctl-rail-sub" role="tablist"><button role="tab" aria-selected="true">Pools</button>' +
    '<button role="tab" aria-selected="false">Runtimes</button></div></nav>'

  it('shows the inline tabs above 900px and the second row below it, never both', () => {
    // MUTATION: drop either `display` rule.
    const f = fragment(STRIP)
    const inline = pick(f, '.ctl-rail-group .ctl-rail-tabs')
    const sub = pick(f, '.ctl-rail-sub')
    expect(won(pick(f, '.ctl-rail-main'), 'display', WIDE), 'row 1 is not transparent to the desktop column').toBe(
      'contents',
    )
    expect(won(sub, 'display', WIDE), 'the second row shows on the desktop rail').toBe('none')
    expect(won(inline, 'display', PHONE), 'the inline tabs still show in row 1 on a phone').toBe('none')
    expect(won(sub, 'display', PHONE)).not.toBe('none')
  })

  it('marks a section with a rule and a tab with a fill, so the two levels stop looking alike', () => {
    // §1.3's "surface step plus a 2px rule", split between the two levels.
    // MUTATION: the section's fill back below 900px, or a rule on the tab.
    const f = fragment(STRIP)
    const section = pick(f, '.ctl-nav-link.is-on')
    expect(won(section, ['background', 'background-color'], PHONE), 'the selected section keeps a fill').toBe('none')
    expect(won(section, ['border-bottom-color', 'border-bottom', 'border-color', 'border'], PHONE)).toBe('var(--text)')

    const tab = pick(f, '.ctl-rail-sub [aria-selected="true"]')
    expect(won(tab, ['background', 'background-color'], PHONE)).toBe('var(--surface-2)')
    expect(won(tab, 'color', PHONE)).toBe('var(--text)')
    expect(
      won(tab, ['border-bottom-color', 'border-bottom', 'border-color', 'border'], PHONE),
      'the selected tab still draws a rule',
    ).toBe('transparent')
    expect(won(pick(f, '.ctl-rail-sub [aria-selected="false"]'), 'color', PHONE)).toBe('var(--text-faint)')
  })

  it('gives the second row the same 44px target as every tab', () => {
    const f = fragment(STRIP)
    const px = Number.parseFloat(won(pick(f, '.ctl-rail-sub button'), 'min-height', PHONE) ?? '')
    expect(px).toBeGreaterThanOrEqual(44)
  })
})

// ===========================================================================
// CH-13 -- one rule for tables below 900px
// ===========================================================================

const SRC = join(__dirname, '..')

/** Every table in the app, its wrapper's classes and its column count. */
function tablesInSource(): { file: string; line: number; classes: string[]; columns: number }[] {
  const out: { file: string; line: number; classes: string[]; columns: number }[] = []
  for (const name of readdirSync(SRC)) {
    if (!name.endsWith('.tsx')) continue
    const text = readFileSync(join(SRC, name), 'utf8')
    for (const m of text.matchAll(/className="((?:ctl-table|table-wrap)\b[^"]*)"/g)) {
      const at = m.index ?? 0
      const rest = text.slice(at, at + 6000)
      const table = rest.indexOf('<table')
      if (table === -1 || table > 400) continue
      const head = /<thead[\s\S]*?<\/thead>/.exec(rest.slice(table))
      if (head === null) continue
      out.push({
        file: name,
        line: text.slice(0, at).split('\n').length,
        classes: m[1]!.split(/\s+/),
        columns: (head[0].match(/<th\b/g) ?? []).length,
      })
    }
  }
  return out
}

describe('CH-13: one rule for tables below 900px', () => {
  it('scrolls every data table and stacks only the records of four columns or fewer', () => {
    // design-system.md §7.3. A stacked record is right for an inspector fact
    // and wrong for a comparison: Pools stacked was 5,924px tall at 390 and
    // Profile headroom 9,882px. MUTATION: `is-stacked` back on any table of
    // five or more columns, or a data table left with neither class.
    const tables = tablesInSource()
    expect(tables.length, 'the source scan found too few tables to mean anything').toBeGreaterThanOrEqual(15)
    const data = tables.filter((t) => t.classes.includes('is-scroll'))
    const records = tables.filter((t) => t.classes.includes('is-stacked'))
    expect(data.length, 'no table scrolls').toBeGreaterThanOrEqual(10)
    for (const t of records) {
      expect(t.columns, `${t.file}:${t.line} stacks a ${t.columns}-column table`).toBeLessThanOrEqual(4)
    }
    for (const t of tables.filter((x) => x.columns > 4)) {
      expect(t.classes, `${t.file}:${t.line} is a ${t.columns}-column data table`).toContain('is-scroll')
    }
  })

  it('keeps a scrolling table\'s first column in view at 390, on an opaque fill', () => {
    // MUTATION: drop `position: sticky` or the fill, or let the table stack.
    for (const wrap of ['ctl-table is-scroll', 'table-wrap is-scroll']) {
      const f = fragment(
        `<div class="${wrap}"><table><thead><tr><th scope="col">Pool</th><th scope="col">Units</th></tr></thead>` +
          '<tbody><tr><th scope="row">global</th><td data-label="Units">4</td></tr></tbody></table></div>',
      )
      expect(won(pick(f, 'div'), ['overflow-x', 'overflow'], PHONE), wrap).toBe('auto')
      for (const cell of ['thead th', 'tbody th']) {
        const el = pick(f, cell)
        expect(won(el, 'position', PHONE), `${wrap} ${cell}`).toBe('sticky')
        expect(won(el, 'left', PHONE), `${wrap} ${cell}`).toBe('0')
        const fill = won(el, ['background-color', 'background'], PHONE) ?? ''
        expect(fill, `${wrap} ${cell} is see-through, so the scrolled columns show under it`).toMatch(
          /^var\(--surface(-2)?\)$/,
        )
      }
      expect(won(pick(f, 'td'), 'display', PHONE), `${wrap} stacks`).not.toBe('grid')
    }
  })

  it('ellipsizes a long value in a stacked record, on the page and in the inspector', () => {
    // An attempt's gs:// uri wrapped over nine lines at 390. It is copied, not
    // read, and the copy action carries the whole value. MUTATION: the
    // `overflow-wrap: anywhere` break back on `.uri`.
    const f = fragment(
      '<div class="ctl-table is-stacked"><table><tbody><tr><td data-label="Location">' +
        '<span class="mono uri">gs://swarm-artifacts/tenants/eng/tasks/tsk_1/attempts/att_1/checkpoints/ckpt-00001/</span>' +
        '<button class="copy">copy gsutil</button></td></tr></tbody></table></div>',
    )
    const uri = pick(f, '.uri')
    for (const env of [PHONE, { width: 1440, container: 480 }]) {
      expect(won(uri, 'white-space', env)).toBe('nowrap')
      expect(won(uri, ['overflow', 'overflow-x'], env)).toBe('hidden')
      expect(won(uri, 'text-overflow', env)).toBe('ellipsis')
    }
  })
})
