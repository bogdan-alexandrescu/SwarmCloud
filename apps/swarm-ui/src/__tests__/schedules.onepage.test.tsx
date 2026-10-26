// SCHEDULES ARE ONE PAGE (SCHED-ONE-PAGE; owner decision 2026-10-13,
// docs/schedules.md §6.1, re-deciding SD1's second page). The owner asked
// "why do I have schedules in Admin and also in Automate?" and chose one page
// with an admin toggle:
//
//   * Automate › Schedules carries a "Mine | All workspaces" switch, drawn only
//     for an admin (the session's `principal.is_admin`); a non-admin never
//     sees it, and the server still decides `GET /v1/admin/schedules`;
//   * All tenants is the cross-tenant table (a Tenant column), the schedule
//     tick's last hour and the pause-all-of-one-tenant control;
//   * the switch is in the address, `?scope=all`, so it can be linked;
//   * `/admin/schedules` is gone and lands on `/automate/schedules?scope=all`;
//   * the platform's own ticks moved to Admin › Platform counts;
//   * the Admin section, its panel and its question no longer name Schedules.
//
// Placeholders only; nothing is token-shaped.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import { SECTIONS, canonical, fromAddress } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES } from '../Spine'
import { holdScheduleClock, hoursFromNow } from './scheduleclock'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

function adminRow(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    tier: 'R2',
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    words: 'at 09:00, Monday through Friday',
    next_run_at: hoursFromNow(3),
    last_outcome: 'succeeded',
    last_firing_at: hoursFromNow(-20),
    spend_today: { day: '2026-10-13', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    ...over,
  }
}

const TICK = { window_minutes: 60, ticks: 60, disabled: 0, errors: 0, truncated: 0, fired: 4, max_lateness_seconds: 12, last: null }

const me = (admin: boolean) => ({
  tenant: { tenant_id: 'eng', kind: 'group' },
  principal: { email: 'person@example.com', domain: 'example.com', groups: [], is_admin: admin },
})

/** The page's reads: the session, the tenant's list, the repositories, and the admin list. */
function handler(admin: boolean | null) {
  return (m: string, url: string): Reply => {
    if (m !== 'GET') return null
    const path = url.split('?')[0]
    if (path === '/v1/tenants/me') return admin === null ? null : { status: 200, body: me(admin) }
    if (path === '/v1/schedules') return { status: 200, body: { schedules: [], tenant_id: 'eng' } }
    if (path === '/v1/repositories') return { status: 200, body: { repositories: [] } }
    if (path === '/v1/admin/schedules') {
      if (admin !== true) return { status: 403, body: { code: 'admin_required', message: 'admins only' } }
      return {
        status: 200,
        body: { schedules: [adminRow('sch_a'), adminRow('sch_b', { tenant_id: 'research' })], next_page_token: null, tick: TICK },
      }
    }
    return null
  }
}

async function mount(admin: boolean | null, start: string | null) {
  const calls = serve(handler(admin))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={start} spy={go} />)
  await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const SWITCH = { name: 'Whose schedules' }
const rowOf = (id: string) => document.querySelector<HTMLElement>(`tr[data-schedule="${id}"]`)
const sent = (calls: { method: string; url: string }[], path: string) => calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === path)

// One instant for the fixture and the page (CLOCK-ONCE).
beforeEach(() => holdScheduleClock())

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('one Schedules page, with an admin switch', () => {
  it('draws "Mine | All workspaces" for an admin, with Mine chosen on the bare page', async () => {
    await mount(true, null)
    // MUTATION: draw the switch for nobody, or for everyone.
    const nav = await screen.findByRole('navigation', SWITCH, WAIT)
    const links = [...nav.querySelectorAll('a')]
    // VQ-030: BGD on #2157 (2026-10-23) kept the "All workspaces" switch; the
    // console says workspace, never tenant, in its words and its titles.
    expect(links.map((a) => a.textContent)).toEqual(['Mine', 'All workspaces'])
    for (const a of links) expect(a.getAttribute('title') ?? '').not.toMatch(/tenant/i)
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/automate/schedules', '/automate/schedules?scope=all'])
    expect(links[0]!.getAttribute('aria-current')).toBe('page')
  })

  it('draws no switch for a non-admin, and never reads the admin list', async () => {
    const { calls } = await mount(false, null)
    await waitFor(() => expect(sent(calls, '/v1/tenants/me').length).toBeGreaterThan(0), WAIT)
    await screen.findByText('No schedules yet', undefined, WAIT)
    expect(screen.queryByRole('navigation', SWITCH)).toBeNull()
    expect(sent(calls, '/v1/admin/schedules')).toHaveLength(0)
  })

  it('a non-admin following a ?scope=all link gets their own list, with no switch', async () => {
    const { calls } = await mount(false, 'scope=all')
    await screen.findByText('No schedules yet', undefined, WAIT)
    expect(screen.queryByRole('navigation', SWITCH)).toBeNull()
    expect(document.querySelector('th[data-col="tenant"]')).toBeNull()
    expect(sent(calls, '/v1/admin/schedules')).toHaveLength(0)
  })

  it('switches to All tenants by its address', async () => {
    const { go } = await mount(true, null)
    const nav = await screen.findByRole('navigation', SWITCH, WAIT)
    fireEvent.click([...nav.querySelectorAll('a')][1]!)
    expect(go).toHaveBeenCalledWith('automate/schedules?scope=all')
    await waitFor(() => expect(rowOf('sch_b')).not.toBeNull(), WAIT)
  })

  it('All tenants is the cross-tenant table, the tick’s last hour and the pause-all control', async () => {
    await mount(true, 'scope=all')
    await waitFor(() => expect(rowOf('sch_b')).not.toBeNull(), WAIT)
    const nav = screen.getByRole('navigation', SWITCH)
    expect([...nav.querySelectorAll('a')][1]!.getAttribute('aria-current')).toBe('page')
    expect(document.querySelector('th[data-col="tenant"]')).not.toBeNull()
    expect(visible(rowOf('sch_b')!.querySelector('td[data-label="Tenant"]'))).toBe('research')
    expect(visible(screen.getByTestId('as-tick'))).toContain('60 ticks, 4 fired, 0 errors')
    expect(screen.getByRole('button', { name: /Pause all of eng’s schedules/ })).toBeTruthy()
    // The platform's own ticks are Admin › Platform counts' now, not this page's.
    expect(screen.queryByTestId('as-platform-ticks')).toBeNull()
  })
})

describe('the address', () => {
  it('carries the switch as ?scope=all, both ways', () => {
    // MUTATION: drop `scope` from keptKeys and the link opens Mine.
    const all = fromAddress('automate/schedules?scope=all')
    expect(all).toMatchObject({ sectionId: 'automate', tab: 'schedules', view: 'scope=all' })
    expect(canonical(all)).toBe('automate/schedules?scope=all')
    expect(addressToPath(canonical(all))).toBe('/automate/schedules?scope=all')
    expect(pathToAddress('/automate/schedules', '?scope=all')?.address).toBe('automate/schedules?scope=all')
  })

  it('sends /admin/schedules to /automate/schedules?scope=all', () => {
    expect(pathToAddress('/admin/schedules')?.address).toBe('automate/schedules?scope=all')
    const old = fromAddress('admin/schedules')
    expect(old).toMatchObject({ sectionId: 'automate', tab: 'schedules', view: 'scope=all' })
    expect(addressToPath(canonical(old))).toBe('/automate/schedules?scope=all')
  })
})

describe('Admin no longer has a Schedules page', () => {
  it('has no Schedules tab, panel entry or question clause', () => {
    const admin = SECTIONS.find((s) => s.id === 'admin')!
    expect(admin.tabs.map((t) => t.id)).not.toContain('schedules')
    expect(PANEL_PAGES.admin.map((p) => p.to)).not.toContain('admin/schedules')
    expect(admin.question).not.toMatch(/scheduled/)
  })
})

/** `terraform/modules/scheduler/jobs.tf`'s Cloud Scheduler jobs, in file order. */
function jobsTf(): string[] {
  const tf = readFileSync(join(__dirname, '..', '..', '..', '..', 'terraform', 'modules', 'scheduler', 'jobs.tf'), 'utf8')
  return [...tf.matchAll(/^resource "google_cloud_scheduler_job" "([a-z_]+)"/gm)].map((m) => m[1]!)
}

describe('Admin › Platform counts lists the platform ticks', () => {
  it('draws every Cloud Scheduler job under its own heading, with the intro sentence', async () => {
    serve((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/tenants/me' ? { status: 200, body: me(true) } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { PlatformCountsScreen } = await import('../PlatformCounts')
    render(<PlatformCountsScreen />)
    expect(screen.getByRole('heading', { name: 'Platform ticks', level: 2 })).toBeTruthy()
    expect(screen.getByText(/The platform’s own machinery\. No tenant manages these/)).toBeTruthy()
    const ticks = screen.getByTestId('as-platform-ticks')
    const names = [...ticks.querySelectorAll<HTMLElement>('tr[data-tick]')].map((r) => r.dataset.tick)
    expect(jobsTf().length).toBeGreaterThanOrEqual(11)
    expect(names).toEqual(jobsTf())
  })
})
