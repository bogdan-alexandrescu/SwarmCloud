/**
 * THE SHELL AGAINST THE PICKED FRAMES (navigation.html V2, states.html C) AND
 * THE LIVE AUDIT (#503).
 *
 * Each `describe` is one #503 finding or one states.html tier, and each `it`
 * names the mutation that turns it red.
 */
import STYLES from '../styles.css?raw'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { wholeAppFault } from '../AppStates'
import { forgetProbes, type ProbeRecord, type Result } from '../fetch'
import { Screen } from '../Shell'
import { panelCounts, RECENT_WORKFLOWS_KEY } from '../Spine'
import type { Capacity, Me, Stats } from '../types'
import { cascade, type CascadeEnv } from './cssgate'

const WIDE: CascadeEnv = { width: 1440 }

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const ME: Me = {
  tenant: {
    tenant_id: 'eng',
    kind: 'group',
    principal: 'eng@example.com',
    display_name: 'eng',
    created_at: '2026-09-01T00:00:00Z',
    max_active: 4,
    capacity_units: 8,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: null,
    gcs_prefix: 'tenants/eng',
    namespace: 'swarm-tenant-eng',
  },
  principal: { email: 'operator@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}

beforeEach(() => {
  try {
    window.localStorage.clear()
  } catch {
    /* storage may be absent */
  }
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

/** Render the app at a path, on the fixture API, and let the frame's reads land. */
async function at(path: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  const { container } = render(<App />)
  await waitFor(() => expect(container.querySelector('.sk-tenant b')?.textContent).not.toBe('reading…'), { timeout: 5000 })
  return container
}

// ---------------------------------------------------------------------------
// #503 Shell: the panel's page rows have icons and counts
// ---------------------------------------------------------------------------

describe('#503: the panel draws an icon per page and a count beside Live, Waiting and Workflows', () => {
  it('draws an icon on every page row', async () => {
    // MUTATION: drop `<Icon name={p.icon} />` from PanelPages.
    const c = await at('/agents/live')
    const rows = [...c.querySelectorAll('.sk-panel .sk-pk')]
    expect(rows.map((r) => r.querySelector('.sk-pl')?.textContent)).toEqual(['Agents', 'Workflows', 'Runs', 'Timeline', 'Repositories', 'Setup', 'Access'])
    for (const r of rows) expect(r.querySelector('svg.sk-ic'), `${r.textContent} has no icon`).not.toBeNull()
  })

  it('counts Live and Waiting from /v1/stats, LEASED through RUNNING and QUEUED through PARKED', async () => {
    // The fixture: LEASED 1, DISPATCHED 1, STARTING 1, RUNNING 2 -> 5 live;
    // QUEUED 0, READY 1, PARKED 1 -> 2 waiting. MUTATION: count RUNNING only.
    // Asked of `panelCounts` with no list on screen: on the Agents page the
    // panel counts the list's own read instead (U10a D27,
    // qa.u10a.shell.test.tsx).
    const { loadStats } = await import('../api')
    const stats = await loadStats({ frame: true })
    const counts = panelCounts(stats, { status: 'loading', since: Date.now() })
    expect([counts.live?.n, counts.waiting?.n]).toEqual([5, 2])
  })

  it('draws the Workflows count as a dash with its reason: no route serves it', async () => {
    // MUTATION: draw 0, or nothing, for a figure nobody measured.
    const c = await at('/agents/live')
    const wf = [...c.querySelectorAll('.sk-panel .sk-pk')].find((r) => r.textContent?.includes('Workflows'))!
    const cnt = wf.querySelector('.sk-cnt')!
    expect(cnt.textContent).toBe('—')
    expect(cnt.getAttribute('title')).toMatch(/^not served/)
  })

  it('never draws a failed read as 0', () => {
    const failed: Result<Stats> = { status: 'error', error: { kind: 'upstream_degraded', httpStatus: 503, code: null, message: 'm' } }
    const cap: Result<Capacity> = { status: 'loading', since: 0 }
    const out = panelCounts(failed, cap)
    expect(out.live).toEqual({ n: null, why: 'not read: A service the API depends on did not answer' })
    expect(out.waiting?.n).toBeNull()
    // A reply missing a state is not a zero either.
    const odd: Result<Stats> = { status: 'ok', fetchedAt: 0, data: { tenant_id: 'eng', tasks_by_state: { RUNNING: 3 }, dispatch_paused: false, limits: {}, generated_at: '' } }
    expect(panelCounts(odd, cap).live?.n).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// #503 Shell: the panel highlights the page itself
// ---------------------------------------------------------------------------

describe('#503: the panel lights the current page, not its parent', () => {
  it.each([
    ['/agents/live', 'Live'],
    ['/agents/waiting', 'Waiting'],
    ['/agents/recent', 'Recent'],
  ])('on %s it lights %s, and Agents is the open group, unlit', async (path, page) => {
    // MUTATION: light the parent row again (`p.key === lit`).
    const c = await at(path)
    const on = c.querySelector('.sk-panel .sk-kid.is-on')
    expect(on?.querySelector('span')?.textContent).toBe(page)
    expect(on?.getAttribute('aria-current')).toBe('page')
    expect(c.querySelector('.sk-panel .sk-pk.is-on'), 'the parent row is lit').toBeNull()
    expect(c.querySelector('.sk-panel .sk-pk.is-group .sk-pl')?.textContent).toBe('Agents')
  })

  it.each([
    ['/capacity/pools', 'Pools', 'Ceilings'],
    ['/capacity/pools/profiles', 'Pools', 'By runner profile'],
  ])('on %s it lights only the deepest match, %s › %s', async (path, group, page) => {
    // MUTATION: light the group row as well (`on = group`).
    const c = await at(path)
    const current = [...c.querySelectorAll('.sk-panel [aria-current="page"]')]
    expect(current.map((e) => e.textContent?.trim())).toEqual([page])
    expect(c.querySelector('.sk-panel .sk-pk.is-group .sk-pl')?.textContent).toBe(group)
    expect(c.querySelector('.sk-panel .sk-pk.is-group')?.hasAttribute('aria-current')).toBe(false)
  })

  it('lights a page with no children itself', async () => {
    const c = await at('/workflows')
    expect(c.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('Workflows')
  })
})

// ---------------------------------------------------------------------------
// #503 Shell: the spine's sections are links
// ---------------------------------------------------------------------------

describe('#503: every spine item is a link, so it opens in a new tab and copies', () => {
  it('draws Submit, the four sections, Help and API reads as anchors with a path', async () => {
    // MUTATION: put `<button data-sec>` back.
    const c = await at('/overview')
    const spine = c.querySelector('.sk-spine')!
    expect(spine.querySelectorAll('button[data-sec]')).toHaveLength(0)
    const hrefs = Object.fromEntries([...spine.querySelectorAll<HTMLAnchorElement>('a.sk-ri')].map((a) => [a.textContent?.trim(), a.getAttribute('href')]))
    expect(hrefs).toEqual({
      Submit: '/submit',
      Overview: '/overview',
      Work: '/agents',
      Capacity: '/capacity/pools',
      Admin: '/admin/limits',
      Help: '/help',
      'API reads': '/api-reads',
    })
  })

  it('routes a plain click in the app, and leaves a modified click to the browser', async () => {
    const c = await at('/overview')
    const work = c.querySelector<HTMLAnchorElement>('.sk-spine a[data-sec="work"]')!
    const ctrl = new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true })
    work.dispatchEvent(ctrl)
    expect(ctrl.defaultPrevented, 'a ctrl-click was taken from the browser').toBe(false)
    expect(window.location.pathname).toBe('/overview')
    fireEvent.click(work)
    await waitFor(() => expect(window.location.pathname).toBe('/agents'))
  })

  it.each([
    ['meta', { metaKey: true }],
    ['shift', { shiftKey: true }],
    ['middle', { button: 1 }],
  ])('leaves a %s-click on every section to the browser', async (_, init) => {
    // MUTATION: drop a clause from `routedClick`, or bind onClick without it.
    const c = await at('/overview')
    const sections = [...c.querySelectorAll<HTMLAnchorElement>('.sk-spine a[data-sec]')]
    expect(sections).toHaveLength(4)
    for (const a of sections) {
      const e = new MouseEvent('click', { bubbles: true, cancelable: true, ...init })
      a.dispatchEvent(e)
      expect(e.defaultPrevented, `${a.dataset.sec} took the click from the browser`).toBe(false)
    }
    expect(window.location.pathname).toBe('/overview')
  })

  it('takes a plain click from the browser, so the app routes without a reload', async () => {
    // MUTATION: drop `e.preventDefault()` in `routed`.
    const c = await at('/overview')
    const cap = c.querySelector<HTMLAnchorElement>('.sk-spine a[data-sec="capacity"]')!
    const e = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    cap.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(true)
    await waitFor(() => expect(window.location.pathname).toBe('/capacity/pools'))
  })

  it('draws a recent workflow and "All workflows" as links', async () => {
    // MUTATION: put the Recent rows back as `<button>`s.
    window.localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_a', state: 'RUNNING', name: 'lane-a' }]))
    const c = await at('/workflows')
    const group = await waitFor(() => screen.getByRole('group', { name: 'Recent workflows' }))
    expect(group.querySelectorAll('button')).toHaveLength(0)
    const hrefs = [...group.querySelectorAll<HTMLAnchorElement>('a')].map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(['/workflows/wf_a', '/workflows'])
    const row = group.querySelector<HTMLAnchorElement>('a[href="/workflows/wf_a"]')!
    const ctrl = new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true })
    row.dispatchEvent(ctrl)
    expect(ctrl.defaultPrevented).toBe(false)
    fireEvent.click(row)
    await waitFor(() => expect(window.location.pathname).toBe('/workflows/wf_a'))
    expect(c.querySelector('.sk-recent a[aria-current="page"]')?.getAttribute('href')).toBe('/workflows/wf_a')
  })

  it('draws the panel’s pages as links too', async () => {
    const c = await at('/capacity/pools')
    const hrefs = [...c.querySelectorAll<HTMLAnchorElement>('.sk-panel [class$="pscroll"] a')].map((a) => a.getAttribute('href'))
    expect(hrefs).toContain('/capacity/runtimes')
    expect(hrefs).toContain('/capacity/pools/profiles')
  })
})

// ---------------------------------------------------------------------------
// #503 Shell: no full-width bar under the spine
// ---------------------------------------------------------------------------

describe('#503: the reads strip is the content column’s last row, not a bar under the spine', () => {
  it('renders the dock inside the content column, beside the spine', async () => {
    // MUTATION: render <Dock /> as a row of `.ctl-frame` again.
    const c = await at('/agents/live')
    await waitFor(() => expect(c.querySelector('.ctl-dock')).not.toBeNull())
    const dock = c.querySelector('.ctl-dock')!
    expect(dock.parentElement?.classList.contains('sk-main'), 'the dock is not the content column’s row').toBe(true)
    expect(dock.closest('.sk-side')).toBeNull()
    expect(c.querySelector('.ctl-frame > .ctl-dock')).toBeNull()
  })

  it('makes the content column the two-row grid and the spine the frame’s full height', () => {
    // The avatar was cut off because the spine ended where the frame's first
    // row did. MUTATION: give the frame its two rows back.
    const host = document.createElement('div')
    host.innerHTML = '<div class="ctl-frame"><div class="sk-app"><div class="sk-side"></div><div class="sk-main"></div></div></div>'
    document.body.appendChild(host)
    const pick = (sel: string) => host.querySelector(sel)!
    expect(cascade(STYLES, pick('.sk-main'), 'grid-template-rows', WIDE).winner?.value).toBe('minmax(0, 1fr) auto')
    expect(cascade(STYLES, pick('.ctl-frame'), 'grid-template-rows', WIDE).winner).toBeNull()
    expect(cascade(STYLES, pick('.sk-side'), 'height', WIDE).winner?.value).toBe('100%')
    expect(cascade(STYLES, pick('.sk-app'), 'height', WIDE).winner?.value).toBe('100%')
    host.remove()
  })
})

// ---------------------------------------------------------------------------
// #503 Links: sky and sans
// ---------------------------------------------------------------------------

describe('#503: in-card links are sky and sans, not underlined mono ink', () => {
  it('paints the shared link in the accent and the body face', () => {
    // MUTATION: `.ctl-link { color: var(--text) }` again. (--info IS the Open
    // sky accent, #0369a1 / #7cc8f8, as --sk-ac is.)
    const host = document.createElement('div')
    host.innerHTML = '<div class="ctl-card-head"><a class="ctl-link ov-link" href="/agents">agents →</a></div><p><a class="ctl-link" href="/x">holders</a></p>'
    document.body.appendChild(host)
    const [card, inline] = [...host.querySelectorAll('a')]
    expect(cascade(STYLES, inline!, 'color', WIDE).winner?.value).toBe('var(--info)')
    expect(cascade(STYLES, inline!, 'font-family', WIDE).winner?.value).toBe('var(--font)')
    // In a card head, the link is not underlined until hovered, and the
    // screen's mono `.ov-link` does not out-rank the face.
    expect(cascade(STYLES, card!, 'font-family', WIDE).winner?.value).toBe('var(--font)')
    expect(cascade(STYLES, card!, 'text-decoration-color', WIDE).winner?.value).toBe('transparent')
    host.remove()
  })
})

// ---------------------------------------------------------------------------
// #503 Page head: one row
// ---------------------------------------------------------------------------

// THE ROW'S CONTENTS CHANGED UNDER #138 (owner ruling 2026-10-07, design-system
// §6.12): title left, actions right, nothing else. The meta chip left the row
// for a note over the first card (`.c-count-note`), and `read · poll ·
// refresh` became one quiet control in the actions (`.c-refresh`) carrying
// the screen's ticking age, so the frame draws no second age on the row.
describe('#503/#138: the page head is one row -- title left, refresh with its age right', () => {
  it('puts the title and the refresh carrying its age in one row, and the count over the first card', async () => {
    // MUTATION: render a sub-line or meta chip under or beside the title
    // again, move the refresh out of the head's actions, or let the frame
    // draw its age on the row of a screen that carries its own.
    const c = await at('/capacity/pools')
    const row = await waitFor(() => {
      const r = c.querySelector('main.work .c-phead')
      expect(r).not.toBeNull()
      return r!
    })
    expect(row.querySelector(':scope > .head > h1')).not.toBeNull()
    const acts = row.querySelector(':scope > .c-acts')
    expect(acts, 'the head has no actions on its right').not.toBeNull()
    expect(row.lastElementChild, 'the actions are not on the right of the row').toBe(acts)
    const control = () => acts!.querySelector('.c-refresh')?.textContent ?? ''
    // Pools polls (#117), so the control says its cadence and its read age.
    await waitFor(() => expect(control()).toMatch(/^⟳ every \d+ (s|min) · read \d+ s ago$/), { timeout: 5000 })
    expect(row.querySelector('.sub, .c-meta, .c-age'), 'a second line or chip in the head').toBeNull()
    expect(c.querySelector('main.work p.sub'), 'a sub-line under the title').toBeNull()
    // One age, one place: the frame draws none on this row nor by the breadcrumb.
    expect(c.querySelector('.ctl-head-age')).toBeNull()
    // The count is a note below the head, not part of it.
    const note = await waitFor(() => {
      const n = c.querySelector('main.work .c-count-note')
      expect(n, 'the count note is missing').not.toBeNull()
      return n!
    })
    expect(row.contains(note)).toBe(false)
  })

  it('keeps the head one row, its refresh whole, and its actions pushed right', () => {
    // MUTATION: let `.c-phead` wrap, drop `nowrap` from `.c-refresh` (its
    // `⟳` would wrap away from its age), or drop the actions' `margin-left: auto`.
    const host = document.createElement('div')
    host.innerHTML = '<div class="c-phead"><div class="head"><h1>x</h1></div><div class="c-acts"><button type="button" class="c-refresh">⟳ every 30 s · read 4 s ago</button></div></div>'
    document.body.appendChild(host)
    const head = host.querySelector('.c-phead')!
    expect(cascade(STYLES, head, 'display', WIDE).winner?.value).toBe('flex')
    expect(cascade(STYLES, head, 'flex-wrap', WIDE).winner?.value).toBe('nowrap')
    expect(cascade(STYLES, host.querySelector('.c-acts')!, 'margin-left', WIDE).winner?.value).toBe('auto')
    expect(cascade(STYLES, host.querySelector('.c-refresh')!, 'white-space', WIDE).winner?.value).toBe('nowrap')
    host.remove()
  })
})

// ---------------------------------------------------------------------------
// states.html C: the whole-app tier
// ---------------------------------------------------------------------------

function probe(path: string, kind: ProbeRecord['lastKind'], at: number, success: number | null): ProbeRecord {
  return { path, lastUrl: path, lastStatus: kind === null ? 200 : 503, lastKind: kind, lastLatencyMs: 1, lastAttemptAt: at, lastSuccessAt: success }
}

describe('states.html C: which states reach the whole app', () => {
  const err = (kind: 'session_expired' | 'tenant_unresolved' | 'wrong_domain' | 'tenant_disabled' | 'unreachable' | 'upstream_degraded'): Result<Me> => ({
    status: 'error',
    error: { kind, httpStatus: kind === 'unreachable' ? null : 503, code: null, message: 'm' },
  })

  it.each(['session_expired', 'tenant_unresolved', 'wrong_domain', 'tenant_disabled'] as const)('the identity read failing %s takes over', (kind) => {
    expect(wholeAppFault(err(kind), [])?.kind).toBe(kind)
  })

  it('unreachable takes over only before anything was read', () => {
    // MUTATION: take over on any unreachable -- a blip would flash the page away.
    expect(wholeAppFault(err('unreachable'), [probe('/v1/tenants/me', 'unreachable', 2, null)])?.kind).toBe('unreachable')
    expect(wholeAppFault(err('unreachable'), [probe('/v1/tasks', null, 1, 1)])).toBeNull()
  })

  it('a degraded identity read is a page state, not a takeover', () => {
    expect(wholeAppFault(err('upstream_degraded'), [])).toBeNull()
  })

  it('an expired session met by any read, newest, takes over', () => {
    const me: Result<Me> = { status: 'ok', data: ME, fetchedAt: 0 }
    expect(wholeAppFault(me, [probe('/v1/tenants/me', null, 1, 1), probe('/v1/tasks', 'session_expired', 9, 1)])?.kind).toBe('session_expired')
    // Signed in again since: the newest read landed.
    expect(wholeAppFault(me, [probe('/v1/tasks', 'session_expired', 1, 1), probe('/v1/tasks/{id}', null, 9, 9)])).toBeNull()
  })
})

describe('states.html C: the takeover replaces the content area and keeps the nav', () => {
  async function liveAt(path: string, answer: (url: string) => Response | Promise<Response>) {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    forgetProbes()
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => answer(String(input))) as unknown as typeof fetch
    window.history.replaceState(null, '', path)
    const { App: LiveApp } = await import('../App')
    return render(<LiveApp />).container
  }

  it('draws an expired session as one panel, with the spine still usable', async () => {
    // An expired IAP session arrives as a 200 carrying HTML. MUTATION: let
    // each region fail on its own again.
    const c = await liveAt('/agents/live', () => new Response('<html>sign in</html>', { status: 200, headers: { 'content-type': 'text/html' } }))
    const panel = await screen.findByRole('alert', {}, { timeout: 5000 })
    await waitFor(() => expect(c.querySelector('.app-takeover')).not.toBeNull())
    expect(c.querySelector('.app-takeover h1')?.textContent).toBe('Your session expired')
    expect(c.querySelector('.app-takeover')?.textContent).toContain('says nothing about what is running')
    expect(screen.getByRole('button', { name: 'Reload to sign in' })).toBeTruthy()
    expect(panel).toBeTruthy()
    // The page is gone; the nav is not.
    expect(c.querySelector('main.work')).toBeNull()
    expect(c.querySelectorAll('.sk-spine a[data-sec]')).toHaveLength(4)
    expect(c.querySelector('.sk-panel')).not.toBeNull()
    // The meter claims nothing during a whole-app state.
    expect(c.querySelector('.sk-meter b')?.textContent).toBe('not read')
  })

  it('draws an unreachable API before any read as a takeover with Try again', async () => {
    const c = await liveAt('/overview', () => Promise.reject(new TypeError('Failed to fetch')))
    await waitFor(() => expect(c.querySelector('.app-takeover h1')?.textContent).toBe('SwarmCloud is unreachable'), { timeout: 5000 })
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('draws a tenant that could not be resolved in amber, with Try now', async () => {
    const c = await liveAt('/overview', () => json({ code: 'tenant_unresolved', message: 'The tenant could not be resolved: group membership lookup failed.' }, 503))
    await waitFor(() => expect(c.querySelector('.app-takeover')?.getAttribute('data-kind')).toBe('tenant_unresolved'), { timeout: 5000 })
    expect(c.querySelector('.app-takeover')?.className).toContain('is-warn')
    expect(screen.getByRole('button', { name: 'Try now' })).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// states.html §13: offline, and a hidden tab
// ---------------------------------------------------------------------------

describe('states.html §13: an offline browser is named as the device', () => {
  it('shows the offline banner while navigator.onLine is false', async () => {
    // MUTATION: drop the online/offline subscription.
    const c = await at('/overview')
    expect(c.querySelector('.app-banner')).toBeNull()
    const nav = vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false)
    await act(async () => {
      window.dispatchEvent(new Event('offline'))
    })
    expect(c.querySelector('.app-banner')?.textContent).toContain('Your browser is offline — showing older data')
    expect(c.querySelector('.app-banner')?.textContent).toContain('The request never left this device.')
    nav.mockReturnValue(true)
    await act(async () => {
      window.dispatchEvent(new Event('online'))
    })
    expect(c.querySelector('.app-banner')).toBeNull()
  })
})

describe('states.html §13: a tab that was hidden says so on return', () => {
  it('reads "This tab was hidden for 2m, so it stopped reading. Reading now…" until the read lands', async () => {
    // MUTATION: drop the line, or keep it after the read lands.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    let hidden = false
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    let release: (() => void) | null = null
    let reads = 0
    const load = (): Promise<Result<{ n: number }>> => {
      reads++
      if (reads === 1) return Promise.resolve({ status: 'ok', data: { n: 1 }, fetchedAt: Date.now() })
      return new Promise((resolve) => {
        release = () => resolve({ status: 'ok', data: { n: reads }, fetchedAt: Date.now() })
      })
    }
    render(
      <Screen title="Agents" load={load} pollMs={5000}>
        {(d) => <p>rows {d.n}</p>}
      </Screen>,
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
    expect(screen.getByText('rows 1')).toBeTruthy()
    // Hidden for two minutes: the poll is disarmed, nothing reads.
    hidden = true
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(reads).toBe(1)
    hidden = false
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(screen.getByRole('status').textContent).toBe('This tab was hidden for 2m, so it stopped reading. Reading now…')
    await act(async () => {
      release?.()
      await vi.advanceTimersByTimeAsync(10)
    })
    expect(document.querySelector('.app-hidden-line')).toBeNull()
  })
})
