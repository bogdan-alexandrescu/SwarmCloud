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
import { noteFixtureProbe, route } from '../fetch'
import { stateTone, type Me } from '../types'
import { cascade, declarations, flatRules, splitTop, type CascadeEnv } from './cssgate'
import { build, painted, shapeOf } from './marks'
import { noteProbe } from './probes'
import { at, task } from './runfixture'

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

  it("shows the list's own age again when the inspector over it closes, with no read in flight", async () => {
    // THE DEFECT: closing the agent drawer routes back to work/running, and
    // that began a new, empty scope -- but the Agents list under the drawer
    // stayed mounted and does not read again until its next poll (30s with
    // nothing live; never, once polling has stopped on an answer only a person
    // can change). So the head said "reading…" with nothing in flight, beside
    // a list fully drawn: a claim about the screen's reads that was false.
    // MUTATION: begin a fresh scope on every route change, or count the
    // list's polls to the inspector while it is open.
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const done = task({ id: 'tsk_done', tenant_id: 'u-bogdan', state: 'SUCCEEDED', completed_at: at(59) })
    let pending = 0
    const answer = (url: string): Response => {
      if (url.startsWith('/v1/tenants/me')) return json(ME)
      if (url.startsWith('/v1/tasks/tsk_done/events')) return json({ events: [] })
      if (url.startsWith('/v1/tasks/tsk_done/attempts')) return json({ attempts: [] })
      if (url === '/v1/tasks/tsk_done') return json({ task: done })
      if (url.startsWith('/v1/tasks?')) return json({ tasks: [done] })
      if (url.startsWith('/v1/resource-classes')) {
        return json({ resource_classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } } })
      }
      return json({ message: 'not stubbed here' }, 404)
    }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      pending += 1
      try {
        await Promise.resolve()
        return answer(String(input))
      } finally {
        pending -= 1
      }
    }) as unknown as typeof fetch
    window.location.hash = '#work/running'
    const { App: LiveApp } = await import('../App')
    render(<LiveApp />)
    await waitFor(() => expect(headAge()).toMatch(/newest read/), { timeout: 5000 })

    // Open the inspector over the list. Its head is the inspector's own.
    await act(async () => {
      window.location.hash = '#work/task/tsk_done'
    })
    await waitFor(() => expect(document.querySelector('.ctl-crumb')?.textContent).toContain('tsk_done'))
    await waitFor(() => expect(headAge()).toMatch(/newest read/), { timeout: 5000 })

    // Close it: the list never went away and reads nothing now.
    await act(async () => {
      window.location.hash = '#work/running'
    })
    await waitFor(() => expect(document.querySelector('.ctl-crumb')?.textContent).not.toContain('tsk_done'))
    await act(async () => {})
    expect(pending, 'a read is in flight after all; the check would prove nothing').toBe(0)
    expect(headAge(), 'the head says "reading…" with nothing being read').not.toMatch(/reading…/)
    expect(headAge(), "the head lost the list's own read").toMatch(/newest read/)
  })

  it("keeps the list's own age across its tab and state addresses (OV-10), which read nothing", async () => {
    // THE DEFECT: a list address (`#work/running/recent/succeeded`) is a view
    // of one mounted list, but keying the reads scope by the whole address
    // began an empty scope on every segment click, and again when the
    // inspector closed back to the list address it opened from -- "reading…"
    // beside a drawn list, with nothing in flight until the next poll.
    // MUTATION: key `beginScreenReads` and `ScreenAge` by `canonical(at)`
    // again instead of `readsKey(at)`.
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const done = task({ id: 'tsk_done', tenant_id: 'u-bogdan', state: 'SUCCEEDED', completed_at: at(59) })
    let pending = 0
    let listReads = 0
    const answer = (url: string): Response => {
      if (url.startsWith('/v1/tenants/me')) return json(ME)
      if (url.startsWith('/v1/tasks/tsk_done/events')) return json({ events: [] })
      if (url.startsWith('/v1/tasks/tsk_done/attempts')) return json({ attempts: [] })
      if (url === '/v1/tasks/tsk_done') return json({ task: done })
      if (url.startsWith('/v1/tasks?')) {
        listReads += 1
        return json({ tasks: [done] })
      }
      if (url.startsWith('/v1/resource-classes')) {
        return json({ resource_classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } } })
      }
      return json({ message: 'not stubbed here' }, 404)
    }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      pending += 1
      try {
        await Promise.resolve()
        return answer(String(input))
      } finally {
        pending -= 1
      }
    }) as unknown as typeof fetch
    window.location.hash = '#work/running/recent'
    const { App: LiveApp } = await import('../App')
    render(<LiveApp />)
    await waitFor(() => expect(headAge()).toMatch(/newest read/), { timeout: 5000 })

    // A segment click: a route change the list answers from the rows it holds.
    const seg = await waitFor(() => {
      const el = document.querySelector('[aria-label="Recent, by state"]')
      expect(el, 'Recent carries no state segment').not.toBeNull()
      return el!
    })
    const readsBefore = listReads
    const succeeded = [...seg.querySelectorAll('button')].find((b) => /^succeeded/.test(b.textContent ?? ''))
    expect(succeeded, 'the segment offers no "succeeded"').toBeDefined()
    await act(async () => {
      succeeded!.click()
    })
    await waitFor(() => expect(window.location.hash).toBe('#work/running/recent/succeeded'))
    await act(async () => {})
    expect(listReads, 'the click read the list again; the check would prove nothing').toBe(readsBefore)
    expect(pending).toBe(0)
    expect(headAge(), 'a segment click began an empty scope').toMatch(/newest read/)

    // Open the inspector over that address, then close back to it.
    await act(async () => {
      window.location.hash = '#work/task/tsk_done'
    })
    await waitFor(() => expect(document.querySelector('.ctl-crumb')?.textContent).toContain('tsk_done'))
    await waitFor(() => expect(headAge()).toMatch(/newest read/), { timeout: 5000 })
    await act(async () => {
      window.location.hash = '#work/running/recent/succeeded'
    })
    await waitFor(() => expect(document.querySelector('.ctl-crumb')?.textContent).not.toContain('tsk_done'))
    await act(async () => {})
    expect(pending, 'a read is in flight after all; the check would prove nothing').toBe(0)
    expect(headAge(), 'closing to a list address began an empty scope').not.toMatch(/reading…/)
    expect(headAge(), "the head lost the list's own read").toMatch(/newest read/)
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

  // THE HELD COLUMN, SEEN ON A SCREEN THAT DRAWS IT. This used to be a bare
  // `.is-scroll` fixture outside every screen, and it passed while Pools and
  // Profile headroom did not scroll at all (CP-18's fixed layout sized them to
  // the phone). Those two are rendered in `tables.scroll.test.tsx`; this one
  // is API reads, whose held column had no ceiling.
  it("caps the held column, so a long route cannot cover the columns beside it at 390", async () => {
    // A task read's concrete URL is ~53 characters -- about 400px of 12px mono
    // in a 356px scrollport -- and a sticky cell wider than the scrollport
    // covers it at every offset: Last attempt, Outcome, Took and Newest
    // payload scrolled under it and were never visible. MUTATION: drop the
    // ceiling, let the cell or the URL line refuse to wrap or cut, or drop the
    // URL line's title.
    const long = route('/v1/tasks/{id}/attempts', { id: 'tsk_0123456789abcdef0123' }, 'limit=200')
    noteFixtureProbe(long, 40, true)
    window.location.hash = '#reference'
    render(<App />)
    const sub = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.ctl-table.is-scroll .ctl-ref-path > .ctl-sub')
      expect(el, 'API reads drew no concrete URL under the route').not.toBeNull()
      return el!
    })
    expect(sub.textContent).toBe(long.url)
    expect(sub.getAttribute('title'), 'the cut URL is not whole in its title').toBe(long.url)
    expect(won(sub, 'white-space', PHONE)).toBe('nowrap')
    expect(won(sub, ['overflow', 'overflow-x'], PHONE)).toBe('hidden')
    expect(won(sub, 'text-overflow', PHONE)).toBe('ellipsis')
    // It adds nothing to the column's width, and fills the width it is given.
    expect(won(sub, 'width', PHONE), 'the URL line widens the held column').toBe('0')
    expect(won(sub, 'min-width', PHONE)).toBe('100%')

    const held = sub.closest('th')!
    expect(won(held, 'position', PHONE)).toBe('sticky')
    // `min-width` TOO (#222): a cell's `width` is not a floor in automatic
    // table layout, and without one a table laid out at its min-content width
    // squeezes the held column to one character.
    for (const prop of ['width', 'min-width', 'max-width']) {
      const v = won(held, prop, PHONE) ?? ''
      const m = /^min\((\d+(?:\.\d+)?)vw,\s*\d+ch\)$/.exec(v)
      expect(m, `the held column's ${prop} is ${JSON.stringify(v)}, not a ceiling`).not.toBeNull()
      // Under half the viewport, so under half of any phone's scrollport
      // plus its gutters: the other columns always have the rest.
      expect(Number(m![1]), `the held column may take ${m![1]}vw`).toBeLessThanOrEqual(50)
    }
    expect(won(held, 'white-space', PHONE), 'the route cannot wrap inside its ceiling').toBe('normal')
    expect(won(held, 'overflow-wrap', PHONE)).toBe('anywhere')
    // A phone rule: the desktop's route column is as wide as its route.
    expect(won(held, 'max-width', WIDE) ?? 'none').not.toMatch(/vw/)
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
