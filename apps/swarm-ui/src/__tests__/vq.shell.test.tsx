/**
 * VISUAL QA, THE CONSOLE'S FRAME (lane VQ-SHELL, part of #2391; BGD's full
 * visual QA of 2026-10-26 against the decided mock-ups).
 *
 *   VQ-001  At 390 the phone header held the menu, the mark, the page name,
 *           the workspace's whole name, `Dev`, `admin`, the bell and `↑ Top`,
 *           and the page name was cut to `O…`. The mock-up's header is
 *           `≡ Hive Overview  eng DEV`: the workspace by its short key, no
 *           role tag (it lives in the drawer), the page name readable.
 *   VQ-007  Overview's "On this page" drew no counts; O1 draws one beside
 *           each section. They are the page's own sums (`OverviewCounts`).
 *   VQ-053  A page opened from the panel kept the last page's scroll offset:
 *           `.ctl-scroll` outlives the route. A new page starts at the top.
 *   VQ-056  The switcher said `tenant · 1 of 3` and `3 tenants you are a
 *           member of` beside Config's Workspace Settings, and a workspace
 *           with no stored name was named by its group email.
 *
 * Each `it` names the mutation that turns it red.
 */
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 } as const

const hosts: HTMLElement[] = []
const realFetch = globalThis.fetch
afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  try {
    window.localStorage.clear()
    window.sessionStorage.clear()
  } catch {
    /* storage refused; nothing kept */
  }
  window.history.replaceState(null, '', '/')
})

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

/** Two group workspaces; `research` has no stored name, so /mine serves its group email. */
const ENG = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group' }
const RESEARCH = { tenant_id: 'research', display_name: 'research@example.com', kind: 'group' }

function me(admin: boolean) {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', display_name: 'Engineering' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: admin },
    environment: 'dev',
    environment_declared: true,
  }
}

function liveApi(admin = false) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return ok([ENG, RESEARCH])
    if (url.includes('/v1/tenants/me')) return ok(me(admin))
    return ok({ pools: [] })
  }) as unknown as typeof fetch
}

async function shell(admin = false) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.stubEnv('VITE_SWARM_ENV', 'dev')
  vi.resetModules()
  liveApi(admin)
  const { SkyShell } = await import('../Spine')
  window.history.replaceState(null, '', '/overview')
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      <p>page</p>
    </SkyShell>,
  )
  await waitFor(() => expect(document.querySelector('.sk-pbar .sk-tchip')).not.toBeNull(), WAIT)
}

describe('VQ-001: the phone header names the page, and the workspace by its key', () => {
  it('draws the workspace as its short key, whole in its title and name', async () => {
    // MUTATION: draw `workspaceName` in `.sk-tcn` again (`Engineering`).
    await shell()
    const chip = document.querySelector<HTMLElement>('.sk-pbar .sk-tchip')!
    expect(chip.querySelector('.sk-tcn')?.textContent).toBe('eng')
    expect(chip.getAttribute('title')).toBe('Engineering')
    expect(chip.getAttribute('aria-label')).toContain('Engineering')
  })

  it('carries no role tag; an admin is tagged in the drawer foot instead', async () => {
    // MUTATION: put `{admin && <span className="sk-adm">admin</span>}` back in `.sk-pbar`.
    await shell(true)
    await waitFor(() => expect(document.querySelector('.sk-pfoot .sk-adm')?.textContent).toBe('admin'), WAIT)
    expect(document.querySelector('.sk-pbar .sk-adm')).toBeNull()
  })

  it('draws `Top` as its arrow alone, still named for a reader', async () => {
    // MUTATION: `↑ Top` as the button's visible text again.
    await shell()
    const top = document.querySelector<HTMLElement>('.sk-pbar .sk-top')!
    expect(top.textContent?.trim()).toBe('↑')
    expect(top.getAttribute('aria-label')).toBe('Back to the top of the page')
  })

  it('keeps the page name its floor beside the chip, and the chip gives way first', () => {
    // MUTATION: `.sk-pbar.has-chip > b { min-width: 0 }` back, or drop the
    // chip's `flex-shrink: 100`.
    const title = build('div.sk-app > header.sk-pbar.has-chip > b', hosts)
    expect(painted(title, 'min-width', PHONE)).toBe('5.5em')
    expect(painted(title, 'flex', PHONE)).toBe('1 1 auto')
    const chip = build('div.sk-app > header.sk-pbar.has-chip > button.sk-tchip', hosts)
    expect(painted(chip, 'flex-shrink', PHONE)).toBe('100')
    const label = build('div.sk-app > header.sk-pbar > span.sk-tn', hosts)
    expect(painted(label, 'flex-shrink', PHONE)).toBe('100')
  })
})

describe('VQ-056: the switcher says workspace, and names each workspace once', () => {
  it('says workspace on the tile and in its list', async () => {
    // MUTATION: `tenant · {at + 1} of …` or `tenants you are a member of` back.
    await shell()
    const tile = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')
      expect(b).not.toBeNull()
      return b!
    }, WAIT)
    expect(tile.querySelector('small')?.textContent).toBe('workspace · 1 of 2')
    expect(document.querySelector('.sk-tenant .sk-cp')?.getAttribute('aria-label')).toBe('copy id: workspace eng')
    fireEvent.click(tile)
    const list = document.querySelector<HTMLElement>('.sk-tpop')!
    expect(list.textContent).toContain('Act as · 2 workspaces you are a member of')
    expect(list.textContent).not.toMatch(/\btenants?\b/)
  })

  it('names a workspace with no stored name by its id, not its group email', async () => {
    // MUTATION: fall back to `c.display_name` (the group email) in nameTenants.
    await shell()
    fireEvent.click(document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')!)
    const rows = [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop .sk-to')]
    const research = rows.find((r) => r.querySelector('small')?.textContent === 'research')!
    expect(research.querySelector('b')?.textContent).toBe('research')
    expect(research.getAttribute('title')).toBe('registered as research@example.com')
  })

  it('treats an address as no display name', async () => {
    const { displayNameOf } = await import('../Spine')
    expect(displayNameOf('smoke@example.com')).toBeNull()
    expect(displayNameOf('  ')).toBeNull()
    expect(displayNameOf(null)).toBeNull()
    expect(displayNameOf('Engineering')).toBe('Engineering')
  })
})

describe('VQ-007: Overview\'s "On this page" carries the page\'s counts', () => {
  it('draws each published count beside its section, and drops them when the page goes', async () => {
    // MUTATION: drop the `<CountMark c={jumpCounts?.[j.id]} />` from the jumps.
    const { SkyShell, OverviewCounts } = await import('../Spine')
    window.history.replaceState(null, '', '/overview')
    const counts = {
      'ov-needs': { n: 3 },
      'ov-running': { n: 12 },
      'ov-waiting': { n: 37 },
      'ov-failures': { n: 6, text: '≥6' },
    }
    const view = render(
      <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
        <OverviewCounts counts={counts} />
      </SkyShell>,
    )
    const countOf = (label: string) =>
      [...document.querySelectorAll<HTMLAnchorElement>('.sk-panel a.sk-pk')]
        .find((a) => a.querySelector('.sk-pl')?.textContent === label)
        ?.querySelector('.sk-cnt')?.textContent ?? null
    await waitFor(() => expect(countOf('Needs a look')).toBe('3'))
    expect(countOf('Running now')).toBe('12')
    expect(countOf('Waiting, and why')).toBe('37')
    expect(countOf('Recent failures')).toBe('≥6')
    // A section nobody counted draws no mark, never a 0.
    expect(countOf('Pools')).toBeNull()
    view.rerender(
      <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
        <p>another page</p>
      </SkyShell>,
    )
    await waitFor(() => expect(countOf('Needs a look')).toBeNull())
  })

  it('is mounted by Overview with the sums its cards draw', async () => {
    // MUTATION: drop `<OverviewCounts counts={jumpCounts} />` from Overview.tsx,
    // or count Running now from another population than its card's.
    window.history.replaceState(null, '', '/overview')
    // A fresh registry: the fixtures' App, not a module an earlier case built over its live mock.
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    const jump = (label: string) =>
      [...document.querySelectorAll<HTMLAnchorElement>('.sk-panel a.sk-pk')].find((a) => a.querySelector('.sk-pl')?.textContent === label)
    await waitFor(() => expect(jump('Running now')?.querySelector('.sk-cnt')).not.toBeNull(), { timeout: 10000 })
    const note = document.querySelector('#ov-running')?.textContent ?? ''
    const held = /(\d+) hold capacity/.exec(note)?.[1]
    expect(held, `Running now's note: ${note}`).toBeDefined()
    expect(jump('Running now')!.querySelector('.sk-cnt')!.textContent!.replace('≥', '')).toBe(held)
    await waitFor(() => expect(jump('Waiting, and why')?.querySelector('.sk-cnt')).not.toBeNull(), { timeout: 10000 })
    const waiting = /(\d+) · none cost anything/.exec(document.querySelector('#ov-waiting')?.textContent ?? '')?.[1]
    expect(jump('Waiting, and why')!.querySelector('.sk-cnt')!.textContent).toBe(waiting)
  }, 20000)
})

describe('VQ-053: a new page opens at its top', () => {
  /** The shell over a scroller whose offset jsdom keeps. */
  async function harness() {
    const { SkyShell } = await import('../Spine')
    let top = 0
    const pages: Record<string, { tab: string; title: string }> = {
      'capacity/holders': { tab: 'holders', title: 'Holders' },
      'capacity/catalogue': { tab: 'catalogue', title: 'Runtimes' },
    }
    let open: (place: string, section?: string, tab?: string) => void = () => {}
    function Harness() {
      const [at, setAt] = useState({ section: 'capacity', tab: 'holders', title: 'Holders', place: '/capacity/holders' })
      open = (place, section = at.section, tab = at.tab) => setAt({ ...at, section, tab, place })
      return (
        <SkyShell
          section={at.section as 'capacity'}
          tab={at.tab}
          title={at.title}
          place={at.place}
          foot={null}
          go={(to) => {
            const p = pages[to]
            if (p === undefined) return
            window.history.pushState(null, '', `/${to}`)
            setAt({ section: 'capacity', tab: p.tab, title: p.title, place: `/${to}` })
          }}
        >
          <p>page</p>
        </SkyShell>
      )
    }
    window.history.replaceState(null, '', '/capacity/holders')
    render(<Harness />)
    const scroller = document.querySelector<HTMLElement>('.ctl-scroll')!
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => (top = v) })
    const scrollTo = (y: number) =>
      act(() => {
        scroller.scrollTop = y
        scroller.dispatchEvent(new Event('scroll'))
      })
    return { scroller, scrollTo, open: (place: string, section?: string, tab?: string) => act(() => open(place, section, tab)) }
  }

  it('resets the scroller when the panel opens another page', async () => {
    // MUTATION: drop the `scroller.scrollTop = 0` on a new page.
    const { scroller, scrollTo } = await harness()
    await scrollTo(2400)
    const runtimes = [...document.querySelectorAll<HTMLAnchorElement>('.sk-panel a')].find((a) => a.textContent?.startsWith('Runtimes'))!
    await act(async () => {
      fireEvent.click(runtimes)
    })
    expect(window.location.pathname).toBe('/capacity/catalogue')
    expect(scroller.scrollTop).toBe(0)
  })

  it('resets it for a page opened from inside the page, and keeps it for an object opened on the same page', async () => {
    // MUTATION: reset on every address change (an agent opened beside its
    // list would throw the list to its top), or only on the chrome's `nav`.
    const { scroller, scrollTo, open } = await harness()
    await scrollTo(1800)
    await open('/capacity/holders?pool=global')
    expect(scroller.scrollTop, 'the same page, another object').toBe(1800)
    await open('/work/runs', 'work', 'runs')
    expect(scroller.scrollTop, 'another page').toBe(0)
  })

  it('leaves an in-page jump where it went', async () => {
    // MUTATION: reset even when the address carries a `#…`.
    const { scroller, scrollTo, open } = await harness()
    await scrollTo(1200)
    window.history.replaceState(null, '', '/overview#ov-failures')
    await open('/overview', 'overview', 'now')
    expect(scroller.scrollTop).toBe(1200)
  })
})
