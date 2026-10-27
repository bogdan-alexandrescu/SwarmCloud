// "NOT FINISHED YET" READS THE PLATFORM HALF ONCE PER SCOPE PICK (#2149,
// TIMELINE-SCOPE-READ).
//
// Since #2152 `GET /v1/stats` carries the platform half -- twelve count()s
// over every tenant -- only on `?scope=platform` from an admin, so the card's
// plain 60 s poll no longer brings it, and in platform scope the card told an
// admin "platform counts not served", the words written for a non-admin. BGD
// decided on 2026-10-23 (~20:39Z, task_875ae68942a241a899f2): "Platform scope
// stops polling the platform half" -- read once per scope pick. So:
//
//   * an admin picking platform scope makes exactly one platform read;
//   * the page's ticks after that make none (they keep reading the tenant half);
//   * the page's own refresh makes one;
//   * leaving platform scope and coming back makes one more;
//   * the card draws those counts with their age, "as of <time>";
//   * a non-admin never asks for them.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import { clockTime, type Me, type Stats, type TaskPage, type Tenant } from '../types'

const api = vi.hoisted(() => ({
  loadOutcomes: vi.fn(),
  loadMe: vi.fn(),
  loadRunnerProfiles: vi.fn(),
  loadStats: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTenants: vi.fn(),
  loadTaskWindow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { ActivityScreen } = await import('../Activity')
const { TIMELINE_POLL_MS } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

function me(admin: boolean): Me {
  return {
    tenant: {
      tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null,
      created_at: '2026-09-01T00:00:00Z', max_active: 10, capacity_units: 20, monthly_budget_usd: null,
      enabled: true, credentials: [], service_account: null, gcs_prefix: null, namespace: null,
    },
    principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: admin },
    environment: 'dev',
    environment_declared: true,
  }
}

const BY_STATE = {
  SUBMITTED: 0, QUEUED: 2, PARKED: 3, READY: 1, LEASED: 1, DISPATCHED: 0,
  STARTING: 0, RUNNING: 2, SUCCEEDED: 272, FAILED: 28, CANCELLED: 416, DEAD_LETTERED: 0,
}

/** When the platform half was counted: 3 min before the test, so its age is not "now". */
const PLATFORM_AT = new Date(Date.now() - 180_000).toISOString()

/**
 * `/v1/stats` as #2152 serves it: the tenant half on a plain read, and the
 * platform half too only on `{platform: true}` (`?scope=platform`).
 */
function stats(options: { platform?: boolean } = {}): Stats {
  return {
    tenant_id: 'eng',
    dispatch_paused: false,
    tasks_by_state: BY_STATE,
    ...(options.platform === true ? { platform_tasks_by_state: { ...BY_STATE, PARKED: 14, QUEUED: 9 } } : {}),
    limits: {},
    generated_at: options.platform === true ? PLATFORM_AT : new Date().toISOString(),
  }
}

function serve(admin: boolean): void {
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
  api.loadMe.mockResolvedValue(ok(me(admin)))
  api.loadRunnerProfiles.mockResolvedValue(ok(['claude-code', 'mock']))
  api.loadStats.mockImplementation((options?: { platform?: boolean }) => Promise.resolve(ok(stats(options))))
  api.loadTasksInState.mockResolvedValue(ok<TaskPage>({ tasks: [], next_page_token: null }))
  api.loadTenants.mockResolvedValue(ok({ tenants: ['eng', 'personal'].map((tenant_id) => ({ tenant_id }) as Tenant) }))
}

/** How many reads asked for the platform half. */
function platformReads(): number {
  return api.loadStats.mock.calls.filter((c) => (c[0] as { platform?: boolean } | undefined)?.platform === true).length
}

/** How many reads asked for the tenant half only. */
function tenantReads(): number {
  return api.loadStats.mock.calls.length - platformReads()
}

async function timeline(admin: boolean, view = ''): Promise<HTMLElement> {
  serve(admin)
  const { container } = render(<ActivityScreen view={view} />)
  await waitFor(() => expect(container.querySelector('.ol-ledger, .ctl-empty')).not.toBeNull())
  await waitFor(() => expect(api.loadMe).toHaveBeenCalled())
  return container as HTMLElement
}

function openCard(root: HTMLElement): HTMLElement {
  const h = [...root.querySelectorAll('.ol-card .ctl-card-title')].find((el) => /^Not finished yet/.test(el.textContent ?? ''))
  expect(h, 'no Not finished yet card').toBeTruthy()
  return h!.closest('.ol-card') as HTMLElement
}

/** Pick a scope on the toolbar, as an admin does. */
async function pick(root: HTMLElement, scope: 'platform' | 'my workspace'): Promise<void> {
  const seg = await waitFor(() => {
    const s = root.querySelector<HTMLElement>('.ol-scope')
    expect(s, 'no scope control').not.toBeNull()
    return s!
  })
  fireEvent.click(within(seg).getByRole('button', { name: scope }))
}

/** Let every timer the page set run once more: its poll, its idle cadence. */
async function tick(): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(TIMELINE_POLL_MS + 1_000)
  })
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    // no storage in this environment; the page must not need it
  }
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Not finished yet reads the platform half once per scope pick (#2149)', () => {
  it('makes exactly one platform read when an admin picks platform scope', async () => {
    const root = await timeline(true)
    await waitFor(() => expect(tenantReads()).toBeGreaterThan(0))
    // MUTATION: never ask with `{platform: true}` (the card's 60 s poll alone): 0, not 1.
    expect(platformReads(), 'tenant scope asked for the platform half').toBe(0)
    await pick(root, 'platform')
    await waitFor(() => expect(platformReads()).toBe(1))
    // MUTATION: ask with `{platform: true}` on every re-read of the card, so the
    // ledger's own re-read on the scope change asks twice.
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')).not.toBeNull())
    expect(platformReads(), 'one scope pick asked more than once').toBe(1)
  })

  it('makes no platform read on the ticks after the pick, which still read the tenant half', async () => {
    const root = await timeline(true)
    await pick(root, 'platform')
    await waitFor(() => expect(platformReads()).toBe(1))
    const before = tenantReads()
    await tick()
    await tick()
    // The tenant half is still polled: the ticks happened.
    await waitFor(() => expect(tenantReads(), 'the ticks never fired').toBeGreaterThan(before))
    // MUTATION: put `{platform: true}` on the polled read again (`loadStats({platform: view.platform})`).
    expect(platformReads(), 'a tick re-read the platform half').toBe(1)
  })

  it('makes one platform read when the page’s own refresh is pressed', async () => {
    const root = await timeline(true)
    await pick(root, 'platform')
    await waitFor(() => expect(platformReads()).toBe(1))
    await waitFor(() => expect(root.querySelector<HTMLButtonElement>('.c-refresh:not([aria-disabled])')).not.toBeNull())
    fireEvent.click(root.querySelector<HTMLButtonElement>('.c-refresh')!)
    // MUTATION: leave the refresh press out of the platform read's ask.
    await waitFor(() => expect(platformReads(), 'the refresh did not re-read the platform half').toBe(2))
    await tick()
    expect(platformReads(), 'the tick after the refresh read it again').toBe(2)
  })

  it('reads once more when the admin leaves platform scope and comes back', async () => {
    const root = await timeline(true)
    await pick(root, 'platform')
    await waitFor(() => expect(platformReads()).toBe(1))
    await pick(root, 'my workspace')
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')?.textContent).toContain('3 parked'))
    expect(platformReads(), 'tenant scope asked for the platform half').toBe(1)
    await pick(root, 'platform')
    // MUTATION: keep the answered ask across the scope change, so coming back reads nothing.
    await waitFor(() => expect(platformReads()).toBe(2))
  })

  it('draws the platform counts with their age, "as of <time>"', async () => {
    const root = await timeline(true)
    await pick(root, 'platform')
    await waitFor(() => expect(platformReads()).toBe(1))
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')?.textContent ?? '').toContain('parked'))
    const c = openCard(root)
    // MUTATION: draw `tasks_by_state` (the tenant's 3) in platform scope.
    expect(c.querySelector('.ol-open-counts')!.textContent).toContain('14 parked')
    expect(c.querySelector('.ol-open-counts')!.textContent).toContain('9 queued')
    // MUTATION: say "platform counts not served" to an admin, as #2152 left it.
    expect(c.textContent).not.toContain('platform counts not served')
    const at = c.querySelector('.ol-platform-asof')
    // MUTATION: drop the age, or print the tenant read's time in its place.
    expect(at, 'the platform counts carry no age').not.toBeNull()
    expect(at!.textContent).toBe(`as of ${clockTime(PLATFORM_AT)!.text}`)
    expect(at!.getAttribute('title')).toContain(PLATFORM_AT)
  })

  it('makes no platform read for a non-admin, even on a platform-scope address', async () => {
    const root = await timeline(false, 'scope=platform')
    await waitFor(() => expect(tenantReads()).toBeGreaterThan(0))
    await tick()
    fireEvent.click(root.querySelector<HTMLButtonElement>('.c-refresh')!)
    await tick()
    // MUTATION: drop the admin check from the platform read's gate.
    expect(platformReads(), 'a non-admin asked for the platform half').toBe(0)
  })
})
