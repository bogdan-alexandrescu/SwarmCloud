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
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App, SECTIONS } from '../App'
import { STATE_MARK } from '../marks'
import { TONE_MARK, ToneMark, toneOf } from '../components'
import { Dock } from '../Dock'
import { noteFixtureProbe, route } from '../fetch'
import { stateTone, type Me } from '../types'
import { declarations, flatRules, splitTop, type CascadeEnv } from './cssgate'
import { painted } from './marks'
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
 * The frame's identity read answers at once; the screen's read (`held`)
 * waits for `release`; everything else never answers.
 */
async function liveApp(hash: string, held = '/v1/admin/tenants'): Promise<{ release: (res: Response) => Promise<void> }> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const gate: { answer?: (r: Response) => void } = {}
  const screenRead = new Promise<Response>((resolve) => {
    gate.answer = resolve
  })
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/v1/tenants/me')) return json(ME)
    if (url.startsWith(held)) return screenRead
    return new Promise<Response>(() => {})
  }) as unknown as typeof fetch
  window.location.hash = hash
  const { App: LiveApp } = await import('../App')
  render(<LiveApp />)
  // The FRAME's read has landed: the tab now holds a successful read that is
  // not the screen's.
  await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Bogdan'))
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
/** The page's own refresh control: the first in the page, never the inspector's beside it. */
const pageRefresh = (): HTMLButtonElement | null => document.querySelector<HTMLButtonElement>('main.work .c-phead .c-refresh')
const refreshSays = (): string => pageRefresh()?.textContent ?? ''
/** A read age as the refresh control words it (`⟳ 12 s`, `⟳ every 30 s · read 4 s ago`). */
const AN_AGE = /\d+ (s|min|h|d)\b/

// ONE AGE PER SCREEN (#98, owner ruling 2026-10-07). On a `Screen` route the
// screen's age is its head's refresh control (`.c-refresh`), and the screen
// claims the page age, so the frame's `.ctl-head-age` is not drawn there at
// all. The Repositories pages read through `useUrRead`, not `Screen`, and
// carry the same control (`UrRefresh`); there too the age must be THAT
// screen's reads, never the frame's identity read: CH-2's defect, kept pinned
// below. The frame's span is left only on heads that read nothing.
describe("CH-2: the head's read age is the screen's own", () => {
  it('says "reading…" while its screen loads, even after the frame\'s own read has landed', async () => {
    // THE DEFECT: the head's age was the newest success of ANY route in the
    // tab, so it said "just now" beside a page that was still loading.
    // MUTATION: give the refresh control the tab-wide newest read as its
    // `readAt`, or stop `Screen` claiming the page age (the frame's age comes
    // back beside the control) -- either turns this red.
    const { release } = await liveApp('#admin/tenants')
    await waitFor(() => expect(pageRefresh(), 'the screen has no refresh control').not.toBeNull())
    expect(refreshSays(), 'the head claims an age for a screen that has read nothing').toBe('⟳ reading…')
    expect(pageRefresh()!.disabled).toBe(true)
    expect(document.querySelector('.ctl-head-age'), 'the frame prints a second age on a Screen route').toBeNull()
    // The dock keeps the tab-wide view, and there IS a tab-wide read.
    expect(dockLine()).toMatch(/newest/)

    await release(json({ tenants: [] }))
    await waitFor(() => expect(refreshSays()).toMatch(/^⟳ \d+ s$/))
    expect(document.querySelector('.ctl-head-age')).toBeNull()
  })

  it('says "reading…" on the Repositories list\'s own refresh until its read lands', async () => {
    // The Repositories list is not a `Screen`, and its age is still on the
    // control that renews it (#98): `UrRefresh`, over the list's own read.
    // MUTATION: give `UrRefresh` the tab-wide newest read, or stop it
    // claiming the page age (the frame's span comes back beside it).
    const { release } = await liveApp('#work/repositories', '/v1/repositories')
    await waitFor(() => expect(pageRefresh(), 'the Repositories list has no refresh control').not.toBeNull())
    expect(refreshSays(), 'the head claims an age for a screen that has read nothing').toBe('⟳ reading…')
    expect(document.querySelector('.ctl-head-age'), 'the frame prints a second age on the Repositories list').toBeNull()
    expect(dockLine()).toMatch(/newest/)

    await release(json({ repositories: [] }))
    await waitFor(() => expect(refreshSays()).toMatch(/^⟳ \d+ s$/))
    expect(document.querySelector('.ctl-head-age')).toBeNull()
  })

  it('says "not read" when its screen\'s read failed, and never shows the frame\'s age', async () => {
    // MUTATION: fall back to the tab-wide age when the screen has none.
    const { release } = await liveApp('#admin/tenants')
    await release(json({ message: 'A service the API depends on did not answer.' }, 503))
    await waitFor(() => expect(pageRefresh()?.getAttribute('aria-label')).toBe('Refresh · not read'))
    expect(refreshSays()).toBe('⟳ refresh')
    expect(refreshSays()).not.toMatch(/\d/)
    expect(document.querySelector('.ctl-head-age'), 'the frame prints its age beside a failed screen').toBeNull()
    expect(dockLine(), 'the dock is the tab-wide view').toMatch(/newest/)
  })

  it('says "not read" on the Repositories list\'s own refresh when its read failed', async () => {
    // MUTATION: fall back to the tab-wide age when the screen has none.
    const { release } = await liveApp('#work/repositories', '/v1/repositories')
    await release(json({ message: 'A service the API depends on did not answer.' }, 503))
    await waitFor(() => expect(pageRefresh()?.getAttribute('aria-label')).toBe('Refresh · not read'))
    expect(refreshSays()).toBe('⟳ refresh')
    expect(document.querySelector('.ctl-head-age'), 'the frame prints its age beside a failed screen').toBeNull()
    expect(dockLine(), 'the dock is the tab-wide view').toMatch(/newest/)
  })

  it("shows the list's own age again when the inspector over it closes, with no read in flight", async () => {
    // THE DEFECT: closing the agent drawer routes back to work/running, and
    // the Agents list under the drawer stayed mounted and does not read again
    // until its next poll. So the head said "reading…" with nothing in
    // flight, beside a list fully drawn: a claim about the screen's reads
    // that was false. The list's age is its own refresh control now (#98).
    // MUTATION: remount the list on the route change (its control falls back
    // to "reading…"), or stop the list claiming the page age while the
    // inspector is over it (the frame's age is drawn beside it again).
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
    await waitFor(() => expect(refreshSays()).toMatch(AN_AGE), { timeout: 5000 })
    expect(headAge(), 'the frame prints a second age beside the list').toBe('')

    // Open the inspector over the list. Its head is the inspector's own.
    await act(async () => {
      window.location.hash = '#work/task/tsk_done'
    })
    // The crumb names the open agent (walkthrough G): its id until read, then what it is.
    await waitFor(() => expect(document.querySelector('.ctl-crumb [aria-current="page"]')).not.toBeNull())
    await waitFor(() => expect(refreshSays()).toMatch(AN_AGE), { timeout: 5000 })
    expect(headAge(), 'the frame prints an age beside the breadcrumb, over a list that has its own').toBe('')

    // Close it: the list never went away and reads nothing now.
    await act(async () => {
      window.location.hash = '#work/running'
    })
    // No crumb at all on the list once the object closes (visual QA Q2).
    await waitFor(() => expect(document.querySelector('.ctl-crumb [aria-current="page"]')).toBeNull())
    await act(async () => {})
    expect(pending, 'a read is in flight after all; the check would prove nothing').toBe(0)
    expect(refreshSays(), 'the list says "reading…" with nothing being read').not.toMatch(/reading…/)
    expect(refreshSays(), "the list lost its own read").toMatch(AN_AGE)
    expect(headAge()).toBe('')
  })

  it("keeps the list's own age across its tab and state addresses (OV-10), which read nothing", async () => {
    // THE DEFECT: a list address (`#work/running/recent/succeeded`) is a view
    // of one mounted list, but a segment click, and closing the inspector
    // back to the list address it opened from, each said "reading…" beside a
    // drawn list, with nothing in flight until the next poll.
    // MUTATION: key the list's mount by the whole address (so a segment click
    // remounts it and its refresh control says "reading…"), or let the frame
    // draw its own age on these routes again.
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
    await waitFor(() => expect(refreshSays()).toMatch(AN_AGE), { timeout: 5000 })

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
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe('/agents/recent?state=succeeded'))
    await act(async () => {})
    expect(listReads, 'the click read the list again; the check would prove nothing').toBe(readsBefore)
    expect(pending).toBe(0)
    expect(refreshSays(), 'a segment click began an empty read').toMatch(AN_AGE)
    expect(headAge(), 'the frame prints a second age beside the list').toBe('')

    // Open the inspector over that address, then close back to it.
    await act(async () => {
      window.location.hash = '#work/task/tsk_done'
    })
    // The crumb names the agent humanly (U8 G); the full id stays reachable as its title.
    await waitFor(() => expect(document.querySelector('.ctl-crumb [aria-current="page"]')?.getAttribute('title')).toBe('tsk_done'))
    expect(document.querySelector('.ctl-crumb')?.textContent).toContain('task · done')
    await waitFor(() => expect(refreshSays()).toMatch(AN_AGE), { timeout: 5000 })
    await act(async () => {
      window.location.hash = '#work/running/recent/succeeded'
    })
    // No crumb at all on the list once the object closes (visual QA Q2).
    await waitFor(() => expect(document.querySelector('.ctl-crumb [aria-current="page"]')).toBeNull())
    await act(async () => {})
    expect(pending, 'a read is in flight after all; the check would prove nothing').toBe(0)
    expect(refreshSays(), 'closing to a list address began an empty read').not.toMatch(/reading…/)
    expect(refreshSays(), "the list lost its own read").toMatch(AN_AGE)
    expect(headAge()).toBe('')
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

  it('keeps the ok mark and the info flat bar apart once the colour is gone', () => {
    // The bare dot is the canonical `ToneMark` now (#503 swap): brand glyphs,
    // so the shape is the mark's. MUTATION: give `info` the ok mark.
    const { container } = render(
      <>
        <ToneMark tone="ok" />
        <ToneMark tone="info" />
      </>,
    )
    const [ok, info] = [...container.querySelectorAll('.sk-st')]
    expect(info!.getAttribute('data-mark'), 'the ok mark and the info mark are one shape in greyscale').not.toBe(ok!.getAttribute('data-mark'))
    expect(info!.querySelector('svg')!.innerHTML).not.toBe(ok!.querySelector('svg')!.innerHTML)
  })

  it("draws the collapsed line's mark with the shared dot: an expired session is the diamond", () => {
    // The private `.ctl-dock-dot` drew an expired session as a RING, which is
    // the unknown silhouette. MUTATION: bring the private dot back.
    noteProbe('/v1/capacity', 12, false, 'unauthenticated')
    const { container } = render(<Dock />)
    const line = container.querySelector('.ctl-dock-line')
    expect(line?.querySelector('.sk-st[data-tone="bad"][data-mark="failed"]'), 'an expired session is not the failure diamond').not.toBeNull()
    expect(container.querySelector('.ctl-dock-dot'), 'the private dock dot is back').toBeNull()
  })

  it('draws a clean strip with the neutral ok mark and failures with the triangle', () => {
    noteProbe('/v1/capacity', 12, true)
    const clean = render(<Dock />)
    expect(clean.container.querySelector('.ctl-dock-line .sk-st[data-tone="ok"][data-hue="neu"]')).not.toBeNull()
    clean.unmount()
    noteProbe('/v1/stats', 12, false, 'upstream_degraded')
    const failing = render(<Dock />)
    expect(failing.container.querySelector('.ctl-dock-line .sk-st[data-tone="warn"][data-mark="warn"]')).not.toBeNull()
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
    // 'warn' in `toneOf`, or give it a mark of its own.
    const { container } = render(
      <>
        <ToneMark tone={stateTone('CANCELLED')}>cancelled</ToneMark>
        <ToneMark tone={stateTone('QUEUED')}>queued</ToneMark>
      </>,
    )
    const [cancelled, queued] = [...container.querySelectorAll('.sk-st')]
    expect(cancelled?.getAttribute('data-tone'), 'CANCELLED is not the one info tone').toBe('info')
    expect(cancelled?.getAttribute('data-mark'), 'CANCELLED is not the flat bar').toBe('cancelled')
    expect(queued?.getAttribute('data-tone'), 'QUEUED lost its wait mark').toBe('warn')
  })

  it('makes the bare info mark the flat bar §6.6 specifies', () => {
    // It was a filled disc, so at 390 -- where the word is hidden -- a
    // cancelled and a queued workflow were the same dot. It is CANCELLED's own
    // brand glyph now, the flat bar. MUTATION: give `info` another mark.
    expect(TONE_MARK.info.mark).toBe(STATE_MARK.CANCELLED.mark)
    const { container } = render(<ToneMark tone="info" />)
    const bar = container.querySelector('.sk-st svg rect, .sk-st svg path, .sk-st svg line')
    expect(bar, 'the info mark draws no bar').not.toBeNull()
    expect(container.querySelector('.sk-st svg circle'), 'the bar draws a ring').toBeNull()
  })

  it('draws a waiting workflow with the wait mark and a cancelled one with the ended bar', () => {
    // `dotClass` gave `wait` the info disc, which is now the ended bar -- so a
    // queued workflow and a cancelled one would share it. The tones are the
    // canonical `toneOf` now (#503 swap). MUTATION: `wait` back on `info`.
    expect(toneOf(stateTone('QUEUED'))).toBe('warn')
    expect(toneOf(stateTone('CANCELLED'))).toBe('info')
    expect(toneOf(stateTone('SUCCEEDED'))).toBe('ok')
    expect(TONE_MARK[toneOf(stateTone('QUEUED'))].mark).not.toBe(TONE_MARK[toneOf(stateTone('CANCELLED'))].mark)
  })
})

// ===========================================================================
// CH-21 -- a position means one thing (the strip, then the Sky spine)
// ===========================================================================

describe('CH-21: a position in the navigation means one thing, at every width', () => {
  const ROUTES = SECTIONS.flatMap((s) => s.tabs.map((t) => `#${s.id}/${t.id}`))

  // THE SKY SPINE REPLACED THE TWO-ROW STRIP (rebrand 2026-10-01). The
  // property CH-21 bought is kept, on the new chrome: a position means one
  // thing, on every route.
  it('holds the same spine items, in the same order, on every section route', () => {
    expect(ROUTES.length, 'the sweep is not every section route').toBeGreaterThanOrEqual(15)
    let first: string[] | null = null
    for (const hash of ROUTES) {
      window.history.replaceState(null, '', `/${hash}`)
      const { container, unmount } = render(<App />)
      // Links since #503 (`a.sk-ri`): Submit, the four sections, Help, API reads.
      const items = [...container.querySelectorAll('.sk-spine .sk-ri')].map((b) => (b.textContent ?? '').trim())
      unmount()
      expect(items.length, `${hash}: the spine holds fewer than Submit, four sections and two utilities`).toBe(7)
      if (first === null) first = items
      expect(items, `${hash}: the spine is not the one every other route draws`).toEqual(first)
    }
  })

  it("lists the open section's pages in the panel, and lights the open one", () => {
    window.history.replaceState(null, '', '/capacity/accounts')
    const { container } = render(<App />)
    // THE PAGE ITSELF IS LIT (#503): Accounts is the open GROUP, and its page
    // -- Subscription accounts -- is the one lit and `aria-current`.
    expect(container.querySelector('.sk-panel .sk-pk.is-group .sk-pl')?.textContent).toBe('Accounts')
    expect(container.querySelector('.sk-panel .sk-pk.is-on'), 'the group is lit, not the page').toBeNull()
    const kid = container.querySelector('.sk-panel .sk-kid.is-on')
    expect(kid?.textContent).toBe('Subscription accounts')
    expect(kid?.getAttribute('aria-current')).toBe('page')
  })

  // THE PHONE CASE OF THE SAME PROPERTY. The two-row strip's fragment tests
  // (inline tabs vs. second row, rule vs. fill, the second row's 44px) went
  // with the strip. What a phone gets now is the spine's own header and a
  // drawer holding the SAME spine and panel -- not a second navigation with
  // its own order. MUTATION: show the spine column at 390 without the drawer
  // open, hide the phone header, or stop the drawer showing the column.
  it('puts the same spine and panel in a drawer at phone width, behind the phone header', () => {
    const f = fragment(
      '<div class="sk-app"><header class="sk-pbar"></header><div class="sk-side"></div></div>' +
        '<div class="sk-app has-drawer"><header class="sk-pbar"></header><div class="sk-side"></div></div>',
    )
    const [closed, open] = [...f.querySelectorAll('.sk-side')] as [Element, Element]
    expect(won(closed, 'display', WIDE), 'the spine column is hidden on the desktop').not.toBe('none')
    expect(won(pick(f, '.sk-pbar'), 'display', WIDE), 'the phone header shows on the desktop').toBe('none')
    expect(won(closed, 'display', PHONE), 'the spine column takes the phone width with the drawer shut').toBe('none')
    expect(won(pick(f, '.sk-pbar'), 'display', PHONE)).toBe('flex')
    expect(won(open, 'display', PHONE), 'the open drawer does not show the spine column').toBe('flex')
    expect(won(open, 'position', PHONE), 'the drawer pushes the page instead of covering it').toBe('fixed')
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
    // A literal class list, or a template whose mode is `tableMode(phone)`
    // (capacityPoll.ts, QA G5-08): `.is-scroll` above the phone breakpoint and
    // a stacked record below it, read here as the desktop's `is-scroll` plus
    // `phone-stack`.
    for (const m of text.matchAll(/className=(?:"|\{`)((?:ctl-table|table-wrap)\b[^"`]*)(?:"|`\})/g)) {
      const at = m.index ?? 0
      const rest = text.slice(at, at + 6000)
      const table = rest.indexOf('<table')
      if (table === -1 || table > 400) continue
      const head = /<thead[\s\S]*?<\/thead>/.exec(rest.slice(table))
      if (head === null) continue
      out.push({
        file: name,
        line: text.slice(0, at).split('\n').length,
        classes: m[1]!.replace('${tableMode(phone)}', 'is-scroll phone-stack').split(/\s+/).filter(Boolean),
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
    // AMENDED BELOW 560px (QA G5-08, 2026-10-07): at 390 the scroll hid every
    // capacity and admin table's answer column, so those tables are a record
    // per row on a phone only (`tableMode`, read here as `phone-stack`) and
    // keep this rule from 561 to 899px. Every other table is unchanged.
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
