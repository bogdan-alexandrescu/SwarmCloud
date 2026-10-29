// LANE QA1-FIX-08-PATCHES (console visual QA pass 1, 2026-10-28; part of
// #2596): the halves of capacity-01 and capacity-14 that sat outside
// Capacity.tsx, applied in PR 2636 after its rendered re-review.
//
//   capacity-01  Pools › By runner profile's opened row linked `Pool limits`
//                (`#admin/limits`, an admin route) for a member too, who was
//                dropped on Overview with no word of why.
//   capacity-14  Holders' accounting drift, By runner profile and Runtimes
//                still named a backend by the API's enum (`CLOUD_RUN_JOB`);
//                each reads `Cloud Run` / `GKE Autopilot` now, the id kept
//                in a sub-line or the title.

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard, RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Capacity, LeasePage, LeaseRow, Pool, ProfileAdmission, RunnerProfile, Runtime } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(), loadMe: vi.fn(), loadHolders: vi.fn(), loadRuntimeTopology: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { ProfilesScreen } = await import('../Profiles')
const { HoldersScreen } = await import('../Holders')
const { RuntimesScreen } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-28T10:00:00Z' }
}

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-28T10:00:00Z',
  }
}

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 0, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

/** One profile whose binding pool is its backend, so `Runs out first` names it. */
function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-28T10:00:00Z',
    pools_complete: true,
    pools: [pool('global', 4, 50), pool('tenant:eng', 4, 10), pool('backend:CLOUD_RUN_JOB', 20, 20)],
    runner_profiles: {
      'claude-code': {
        resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
        pools: ['global', 'tenant:eng', 'backend:CLOUD_RUN_JOB'],
        admission: admission({ binding: ['backend:CLOUD_RUN_JOB'] }),
      } as RunnerProfile,
    },
  } as unknown as Capacity
}

async function openMatrix(isAdmin: boolean): Promise<Element> {
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  api.loadMe.mockResolvedValue(ok({ principal: { email: 'alice@example.com', is_admin: isAdmin } }))
  render(<ProfilesScreen />)
  await waitFor(() => expect(document.querySelector('.cap-mx tr[data-profile="claude-code"]')).not.toBeNull(), WAIT)
  // Let the session read settle, so a member's row is judged after it, not before.
  await new Promise((r) => setTimeout(r, 20))
  fireEvent.click(document.querySelector<HTMLButtonElement>('.cap-mx tr[data-profile="claude-code"] button')!)
  const exp = document.querySelector('.cap-mx tr.cap-mx-exp')
  expect(exp, 'the row did not open').not.toBeNull()
  return exp!
}

const limitsLinks = (el: Element) => [...el.querySelectorAll('a')].filter((a) => a.getAttribute('href') === '#admin/limits')

describe('capacity-01: the opened profile row links Pool limits only for an admin', () => {
  it('draws no `Pool limits` link for a member', async () => {
    const exp = await openMatrix(false)
    // MUTATION: draw `<a href="#admin/limits">Pool limits</a>` whatever useViewerIsAdmin says.
    expect(limitsLinks(exp)).toEqual([])
    expect(document.querySelectorAll('.cap-mx a[href^="#admin/"]').length).toBe(0)
  })

  it('keeps the link for an admin (control)', async () => {
    const exp = await openMatrix(true)
    await waitFor(() => expect(limitsLinks(document.querySelector('.cap-mx tr.cap-mx-exp') ?? exp).length).toBe(1), WAIT)
  })
})

describe('capacity-14: By runner profile names a backend pool in words', () => {
  it('reads `Cloud Run` in Runs out first and in the opened table, the id in the title', async () => {
    const exp = await openMatrix(false)
    // MUTATION: `poolLabelAmong(b, among)` -- `CLOUD_RUN_JOB`.
    const bind = document.querySelector('.cap-mx tr[data-profile="claude-code"] .cap-mx-bind[data-pool="backend:CLOUD_RUN_JOB"]')!
    expect(bind.querySelector('.cap-mx-bind-pool')!.textContent).toBe('Cloud Run')
    expect(bind.getAttribute('title')).toBe('backend:CLOUD_RUN_JOB')
    const th = exp.querySelector('tr[data-pool="backend:CLOUD_RUN_JOB"] th')!
    expect(flat(th)).toBe('Cloud Run')
    expect(th.getAttribute('title')).toBe('backend:CLOUD_RUN_JOB')
    // Every other pool keeps its label (control).
    expect(flat(exp.querySelector('tr[data-pool="tenant:eng"] th'))).toBe('eng')
  })
})

function lease(n: number): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`, task_id: `task-${String(n).padStart(10, '0')}`, attempt_id: `att-${n}`,
    tenant_id: 'eng', generation: 1, pools: ['global'], units: 1, dispatch_state: 'DISPATCHED',
    created_at: '2026-10-28T09:00:00Z', dispatch_deadline: '2026-10-28T09:05:00Z', expires_at: '2026-10-28T11:00:00Z',
    heartbeat_at: '2026-10-28T09:59:00Z', released_at: null, release_reason: null, released: false, expired: false,
    dispatch_overdue: false, silent_seconds: 30, heartbeat_ever: true, last_error: null,
  } as unknown as LeaseRow
}

describe('capacity-14: Holders accounting drift names a backend pool in words', () => {
  it('reads `Cloud Run` over the `backend:CLOUD_RUN_JOB` sub-line', async () => {
    const board: HoldersBoard = {
      page: {
        leases: [lease(1), lease(2)], thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
        evaluated_at: '2026-10-28T10:00:00Z', active_only: true, tenant_id: null, units_held: 2,
        truncated: false, active_beyond_window: 0,
      } as unknown as LeasePage,
      // The backend counter holds 3 units no listed lease names: a drifted row, drawn open.
      pools: [pool('global', 2, 40), pool('backend:CLOUD_RUN_JOB', 3, 20)],
      poolsDetail: null,
    }
    api.loadHolders.mockResolvedValue(ok(board))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const th = [...document.querySelectorAll('.hold-drift th[title]')].find((t) => t.getAttribute('title') === 'backend:CLOUD_RUN_JOB')
    expect(th, 'no drift row for the backend pool').toBeTruthy()
    // MUTATION: `poolLabel(r.name)` -- `CLOUD_RUN_JOB`.
    expect(th!.firstChild?.textContent).toBe('Cloud Run')
    expect(th!.querySelector('.ctl-sub')!.textContent).toBe('backend:CLOUD_RUN_JOB')
  })
})

function runtime(name: string, over: Partial<Runtime> = {}): Runtime {
  return {
    name, available: true, disabled_reason: '', image: 'agent-runtime-base',
    backend: 'CLOUD_RUN_JOB', resolved_backend: 'CLOUD_RUN_JOB', provider: null, secrets: [],
    secrets_any_of: false, timeout_seconds: 600, resource_class: 'standard',
    resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
    model_classes: [], default_model_class: null, models: {}, ...over,
  } as Runtime
}

describe('capacity-14: Runtimes names each backend in words', () => {
  it('reads `Cloud Run` / `GKE Autopilot` in Backends and on each card, the id kept beside it', async () => {
    const topology = {
      runtimes: {
        probe: runtime('probe'),
        scout: runtime('scout', { backend: 'GKE_AUTOPILOT', resolved_backend: 'GKE_AUTOPILOT' }),
      },
      classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
      classesDetail: null,
      pools: [pool('backend:CLOUD_RUN_JOB', 1, 20), pool('backend:GKE_AUTOPILOT', 0, 10)],
      poolsDetail: null,
      profilePools: { probe: [], scout: [] },
      profileUncapped: { probe: [], scout: [] },
    } as unknown as RuntimeTopology
    api.loadRuntimeTopology.mockResolvedValue(ok(topology))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    // MUTATION: `<span className="mono">{backend}</span>` alone -- `CLOUD_RUN_JOB`.
    const heads = [...document.querySelectorAll('tbody th[role="rowheader"]')]
      .filter((th) => th.querySelector('.ctl-sub') !== null)
      .map((th) => [th.firstChild?.textContent, th.querySelector('.ctl-sub')!.textContent])
    expect(heads).toEqual(expect.arrayContaining([['Cloud Run', 'CLOUD_RUN_JOB'], ['GKE Autopilot', 'GKE_AUTOPILOT']]))
    // MUTATION: `{runtime.resolved_backend}` in `.rt-backend` -- the enum on the card.
    const runsOn = [...document.querySelectorAll('.rt-backend')].map((b) => [b.textContent, b.getAttribute('title')])
    expect(runsOn).toEqual(expect.arrayContaining([['Cloud Run', 'CLOUD_RUN_JOB'], ['GKE Autopilot', 'GKE_AUTOPILOT']]))
    expect(runsOn.some(([text]) => text === 'CLOUD_RUN_JOB' || text === 'GKE_AUTOPILOT')).toBe(false)
  })
})
