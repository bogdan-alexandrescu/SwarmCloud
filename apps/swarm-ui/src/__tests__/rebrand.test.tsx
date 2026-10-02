/**
 * THE REBRAND'S THREE CONTRACTS (owner decisions 2026-10-01):
 *
 *   1. every legacy hash -- each SECTION_ALIASES, LEGACY and MOVED_PANES entry,
 *      and every tab the hash router wrote -- lands on its new History-API path,
 *      and every new path resolves back to the same route;
 *   2. every task state has exactly one mark and one hue, and the amber
 *      triangle is never a state;
 *   3. the Sky spine shell: the spine's order, the panel's pages per section,
 *      collapse with a remembered choice, Admin locked for a non-admin, and the
 *      production bar.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { App, canonical, fromAddress } from '../App'
import { STATE_MARK, StateMark, WarnMark } from '../marks'
import { addressToPath, helpGroupOf, pathToAddress } from '../paths'
import { meterOf } from '../Spine'
import type { Capacity, TaskState } from '../types'

/** The path the router writes for an old hash: what the one-time redirect does. */
function redirect(hash: string): string {
  return addressToPath(canonical(fromAddress(hash.replace(/^#/, ''))))
}

const LEGACY_TO_PATH: readonly (readonly [string, string])[] = [
  // LEGACY: the eleven-item nav's top-level hashes.
  ['#home', '/overview'],
  ['#trouble', '/overview'],
  ['#holders', '/capacity/holders'],
  ['#quota', '/capacity/accounts/quota'],
  ['#workflows', '/workflows'],
  ['#counts', '/admin/counts'],
  ['#tenants', '/admin/tenants'],
  ['#settings', '/admin/limits'],
  ['#settings/anything-else', '/admin/limits'],
  // MOVED_PANES: a retired section whose panes went different ways.
  ['#history/counts', '/admin/counts'],
  ['#activity/counts', '/admin/counts'],
  ['#settings/limits', '/admin/limits'],
  ['#settings/accounts', '/capacity/accounts'],
  // SECTION_ALIASES: renamed section ids, tail intact.
  ['#agents', '/agents'],
  ['#agents/running/recent/failed', '/agents/recent?state=failed'],
  ['#agents/task/t-2/artifacts', '/agents/live/t-2/artifacts'],
  ['#pools', '/capacity/pools'],
  ['#pools/holders', '/capacity/holders'],
  ['#activity/timeline', '/timeline'],
  ['#history/timeline', '/timeline'],
  ['#runtimes/catalogue', '/capacity/runtimes'],
  ['#runtimes', '/capacity/pools'],
  // Every tab the hash router wrote.
  ['#overview/now', '/overview'],
  ['#work/running', '/agents'],
  ['#work/running/live', '/agents/live'],
  ['#work/running/waiting', '/agents/waiting'],
  ['#work/running/recent', '/agents/recent'],
  ['#work/workflows', '/workflows'],
  ['#work/timeline', '/timeline'],
  ['#work/timeline?span=30d', '/timeline?span=30d'],
  ['#work/new', '/submit/task'],
  ['#work/new-workflow', '/submit/workflow'],
  ['#capacity/pools', '/capacity/pools'],
  ['#capacity/catalogue', '/capacity/runtimes'],
  ['#capacity/profiles', '/capacity/pools/profiles'],
  ['#capacity/holders', '/capacity/holders'],
  ['#capacity/accounts', '/capacity/accounts'],
  ['#capacity/quota', '/capacity/accounts/quota'],
  ['#admin/limits', '/admin/limits'],
  ['#admin/limits?pool=tenant%3Aeng', '/admin/limits?pool=tenant%3Aeng'],
  ['#admin/tenants', '/admin/tenants'],
  ['#admin/counts', '/admin/counts'],
  // One agent, its panes, and the old nav's bare `work/<id>`.
  ['#work/task/t-1', '/agents/live/t-1'],
  ['#work/task/t-1/attempts', '/agents/live/t-1/attempts'],
  ['#work/task/t-1/artifacts', '/agents/live/t-1/artifacts'],
  ['#work/t-9', '/agents/live/t-9'],
  // The utilities.
  ['#help', '/help'],
  ['#reference', '/api-reads'],
]

describe('every legacy hash redirects to its path', () => {
  it.each(LEGACY_TO_PATH)('%s -> %s', (hash, path) => {
    expect(redirect(hash)).toBe(path)
  })

  it('sends an old #help/<topic> link to its group page, scrolled to the topic', () => {
    const group = helpGroupOf('absent-vs-zero')
    expect(group, 'absent-vs-zero is no longer a help topic').not.toBeNull()
    expect(redirect('#help/absent-vs-zero')).toBe(`/help/${group}#absent-vs-zero`)
  })

  it('resolves every path it writes back to the same route', () => {
    const addresses = [
      'overview/now',
      'work/running/live',
      'work/running/recent/failed',
      'work/task/t-1',
      'work/task/t-1/checkpoints',
      'work/workflows',
      'work/workflows?wf=wf-7&owner=me',
      'work/timeline?span=30d',
      'work/new',
      'work/new-workflow',
      'submit',
      'capacity/pools',
      'capacity/profiles',
      'capacity/catalogue',
      'capacity/holders',
      'capacity/accounts',
      'capacity/quota',
      'admin/limits?pool=tenant%3Aeng',
      'admin/tenants',
      'admin/counts',
      'help',
      'reference',
    ]
    for (const a of addresses) {
      const path = addressToPath(a)
      const u = new URL(path, 'https://console.example')
      const back = pathToAddress(u.pathname, u.search, u.hash)
      expect(back, `${path} resolves to nothing`).not.toBeNull()
      expect(canonical(fromAddress(back!.address)), `${a} -> ${path}`).toBe(canonical(fromAddress(a)))
    }
  })

  it('carries an agent path\'s list into the route, and refuses a path it does not serve', () => {
    expect(pathToAddress('/agents/recent/t-3/attempts')).toEqual({ address: 'work/task/t-3/attempts', agentTab: 'recent' })
    expect(pathToAddress('/nowhere')).toBeNull()
    expect(pathToAddress('/')?.address).toBe('overview/now')
  })
})

describe('the app performs the redirect once, without adding history', () => {
  beforeEach(() => window.history.replaceState(null, '', '/'))

  it('rewrites an old hash in the address bar to its path', async () => {
    window.history.replaceState(null, '', '/#work/running/recent/failed')
    const before = window.history.length
    render(<App />)
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe('/agents/recent?state=failed'))
    expect(window.location.hash).toBe('')
    expect(window.history.length).toBe(before)
  })

  it('opens an agent from an old hash under its list, on the tab it named', async () => {
    window.history.replaceState(null, '', '/#work/task/t-1/attempts')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe('/agents/live/t-1/attempts'))
    const drawer = document.querySelector('.ctl-drawer')
    expect(drawer).not.toBeNull()
    expect(drawer!.querySelector('[role="tab"][aria-selected="true"] .ag-tab-label')?.textContent?.trim()).toBe('Attempts')
  })

  it('pushes an in-app #... link as a path, so Back returns', async () => {
    render(<App />)
    const a = document.createElement('a')
    a.href = '#capacity/holders'
    a.textContent = 'holders'
    document.body.appendChild(a)
    const before = window.history.length
    fireEvent.click(a)
    await waitFor(() => expect(window.location.pathname).toBe('/capacity/holders'))
    expect(window.history.length).toBe(before + 1)
    a.remove()
  })
})

// ---------------------------------------------------------------------------

const STATES: readonly TaskState[] = [
  'SUBMITTED',
  'QUEUED',
  'READY',
  'PARKED',
  'LEASED',
  'DISPATCHED',
  'STARTING',
  'RUNNING',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'DEAD_LETTERED',
]

describe('the state marks: one mark and one hue per state', () => {
  it.each(STATES)('%s draws exactly one mark, in exactly one hue', (state) => {
    const { container } = render(<StateMark state={state} />)
    const marks = container.querySelectorAll('[data-mark]')
    expect(marks).toHaveLength(1)
    expect(container.querySelectorAll('svg')).toHaveLength(1)
    const el = marks[0] as HTMLElement
    expect(el.dataset.mark).toBe(STATE_MARK[state].mark)
    expect(el.dataset.hue).toBe(STATE_MARK[state].hue)
    expect(el.className.split(' ').filter((c) => c.startsWith('is-'))).toEqual([`is-${STATE_MARK[state].hue}`])
  })

  it('draws the brand vocabulary, which supersedes #405\'s tints', () => {
    const m = (s: TaskState) => `${STATE_MARK[s].mark}/${STATE_MARK[s].hue}`
    // Teal for the four that hold capacity (invariant 1): half disc, then the haloed disc.
    expect(['LEASED', 'DISPATCHED', 'STARTING'].map((s) => m(s as TaskState))).toEqual([
      'starting/live',
      'starting/live',
      'starting/live',
    ])
    expect(m('RUNNING')).toBe('running/live')
    expect(m('PARKED')).toBe('parked/park')
    expect(m('QUEUED')).toBe('queued/neu')
    expect(m('READY')).toBe('ready/neu')
    expect(m('SUCCEEDED')).toBe('succeeded/neu')
    expect(m('CANCELLED')).toBe('cancelled/neu')
    expect(m('FAILED')).toBe('failed/bad')
    expect(m('DEAD_LETTERED')).toBe('dead/bad')
    // The live hue is exactly the concurrency states.
    const live = STATES.filter((s) => STATE_MARK[s].hue === 'live')
    expect(live).toEqual(['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'])
  })

  it('tells the stored states apart by shape alone', () => {
    const stored = STATES.filter((s) => s !== 'SUBMITTED' && !['LEASED', 'DISPATCHED'].includes(s))
    const shapes = new Set(stored.map((s) => STATE_MARK[s].mark))
    expect(shapes.size).toBe(stored.length)
  })

  it('keeps the amber triangle for warnings only', () => {
    for (const s of STATES) expect(STATE_MARK[s].mark).not.toBe('warn')
    const { container } = render(<WarnMark label="pool full" />)
    expect(container.querySelector('[data-mark="warn"]')?.getAttribute('data-hue')).toBe('warn')
  })
})

// ---------------------------------------------------------------------------

const ME = {
  tenant: { tenant_id: 'eng', display_name: 'Engineering' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}

function stubMe(me: unknown): void {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/me')) {
      return new Response(JSON.stringify(me), { status: 200, headers: { 'content-type': 'application/json' } })
    }
    return new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
}

/**
 * THE APP ON THE LIVE PATH. Under vitest `api.ts` answers from its fixtures
 * unless VITE_LIVE is set, and the fixture's own tenant ('Bogdan', dev) would
 * make a stubbed `/v1/tenants/me` unreachable: the environment and the tenant
 * these two tests measure would never come from the stub.
 */
async function liveApp(): Promise<{ LiveApp: typeof App }> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  return { LiveApp: mod.App }
}

describe('the Sky spine shell', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    try {
      localStorage.clear()
    } catch {
      /* storage may be absent */
    }
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    window.history.replaceState(null, '', '/')
  })

  it('draws the spine in one fixed order: Submit, the four sections, Help, API reads', () => {
    render(<App />)
    const spine = document.querySelector('.sk-spine')!
    // Links since #503 (a section opens in a new tab and copies as a link).
    const labels = [...spine.querySelectorAll('a.sk-ri')].map((b) => b.textContent?.trim())
    expect(labels).toEqual(['Submit', 'Overview', 'Work', 'Capacity', 'Admin', 'Help', 'API reads'])
  })

  it('lists the open section\'s pages in the panel, with the open page\'s children', async () => {
    window.history.replaceState(null, '', '/capacity/pools/profiles')
    render(<App />)
    const panel = document.querySelector('.sk-panel')!
    const pages = [...panel.querySelectorAll('.sk-pk .sk-pl')].map((e) => e.textContent)
    expect(pages).toEqual(['Pools', 'Runtimes', 'Holders', 'Accounts'])
    const kids = [...panel.querySelectorAll('.sk-kid')].map((e) => e.textContent)
    expect(kids).toEqual(['Ceilings', 'By runner profile'])
    expect(panel.querySelector('.sk-kid.is-on')?.textContent).toBe('By runner profile')
  })

  it('navigates from the panel by path, and the Submit button opens the chooser', async () => {
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: /^Work$/ }))
    await waitFor(() => expect(window.location.pathname).toBe('/agents'))
    fireEvent.click(screen.getByTitle('Submit (N)'))
    await waitFor(() => expect(window.location.pathname).toBe('/submit'))
    expect(screen.getByRole('heading', { name: 'Submit' })).toBeTruthy()
  })

  it('collapses to the spine, remembers it per browser, and survives storage that throws', () => {
    const { unmount } = render(<App />)
    expect(document.querySelector('.sk-panel')).not.toBeNull()
    fireEvent.click(screen.getByLabelText('Collapse the panel'))
    expect(document.querySelector('.sk-panel')).toBeNull()
    expect(document.querySelector('.sk-spine')).not.toBeNull()
    unmount()
    render(<App />)
    expect(document.querySelector('.sk-panel'), 'the collapse was not remembered').toBeNull()

    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => render(<App />)).not.toThrow()
    get.mockRestore()
  })

  it('shows Admin to a non-admin, locked: rows disabled and one line saying admins only', async () => {
    stubMe(ME)
    window.history.replaceState(null, '', '/admin/tenants')
    render(<App />)
    await waitFor(() => expect(document.querySelector('.sk-pnote')).not.toBeNull())
    const rows = [...document.querySelectorAll<HTMLElement>('.sk-panel .sk-pk')]
    expect(rows.map((r) => r.textContent?.trim())).toEqual(['Pool limits', 'Tenants', 'Platform counts'])
    // Disabled, and not a link anywhere: no href to follow into a new tab.
    for (const r of rows) {
      expect(r.getAttribute('aria-disabled')).toBe('true')
      expect(r.hasAttribute('href')).toBe(false)
    }
    expect(document.querySelector('.sk-pnote')?.textContent).toMatch(/^Admins only\./)
    expect(document.querySelector('.sk-spine .sk-lkd')).not.toBeNull()
  })

  it('draws the red bar and pill only where the environment measured production', async () => {
    stubMe({ ...ME, environment: 'prod' })
    const { LiveApp } = await liveApp()
    render(<LiveApp />)
    await waitFor(() => expect(document.querySelector('.sk-prodbar')).not.toBeNull())
    expect(document.querySelector('.sk-panel .sk-pill.is-prod')?.textContent).toBe('PROD')
  })

  it('leaves the bar off for a declared non-production environment', async () => {
    stubMe(ME)
    const { LiveApp } = await liveApp()
    render(<LiveApp />)
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Engineering'))
    expect(document.querySelector('.sk-prodbar')).toBeNull()
    expect(document.querySelector('.sk-panel .sk-pill')?.textContent).toBe('Dev')
  })
})

describe('the capacity meter', () => {
  const pool = (name: string, active: number, limit: number, enabled = true) => ({
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: limit - active,
    enabled,
    updated_at: '2026-10-01T00:00:00Z',
  })

  it('reads the global pool, and names the first pool that is full or paused', () => {
    const cap = { pools: [pool('global', 31, 40), pool('class:browser', 4, 4)], runner_profiles: {} } as unknown as Capacity
    expect(meterOf(cap)).toEqual({ active: 31, limit: 40, warn: 'class:browser full' })
    const paused = { pools: [pool('global', 0, 40), pool('tenant:eng', 0, 5, false)], runner_profiles: {} } as unknown as Capacity
    expect(meterOf(paused)?.warn).toBe('tenant:eng paused')
  })

  it('draws nothing it did not read', () => {
    expect(meterOf(null)).toBeNull()
    expect(meterOf({ pools: [], runner_profiles: {} } as unknown as Capacity)).toBeNull()
  })
})
