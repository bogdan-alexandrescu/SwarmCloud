// PLATFORM COUNTS HAS ONE RUN (COUNTS-ONE-RUN, #2149). BGD, CTV-5
// (2026-10-24 17:30Z): "roster on mount, one shared Run"; and 2026-10-25: one
// shared Run, and the page in mock-up F1's order (docs/web-ui/mockups/
// counts-a.html, frames F1 and F7).
//
// The page drew two primary Runs -- "Run the count · N reads" in the head and
// "Run · ~N reads" inside Over time -- each pricing half of what the page
// reads. Now the head's Run is the only one: its price is both reads summed,
// the over-time half priced from the roster read on mount, and one press reads
// both. Over time is drawn first, then "Right now" (the state counts), then
// the ticks.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { PlatformTime } from '../platformTimeApi'
import type { Me, Stats, Tenant } from '../types'
import { NEVER_WRITTEN, REAL_STATES } from '../types'

const loadStats = vi.hoisted(() => vi.fn<() => Promise<Result<Stats>>>())
const loadMe = vi.hoisted(() => vi.fn<() => Promise<Result<Me>>>())
const loadTenants = vi.hoisted(() => vi.fn<() => Promise<Result<{ tenants: Tenant[] }>>>())
vi.mock('../api', () => ({ loadStats, loadMe, loadTenants }))
const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformCountsScreen, forgetLastRun } = await import('../PlatformCounts')
const { forgetOverTime } = await import('../PlatformOverTime')
const { readEstimate, spanDays } = await import('../platformTimeApi')
const { viewerZone } = await import('../outcomes')

const PER_SCOPE = REAL_STATES.length + NEVER_WRITTEN.size

function session(isAdmin: boolean): Result<Me> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant: { tenant_id: 'eng' },
      principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
      environment: 'dev',
      environment_declared: false,
    } as unknown as Me,
  }
}

function roster(n: number): Result<{ tenants: Tenant[] }> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { tenants: Array.from({ length: n }, (_, i) => ({ tenant_id: `t${i}` })) as unknown as Tenant[] },
  }
}

/** The over-time half of the price, for the week view the page opens on, as the view computes it. */
function overTimeReads(workspaces: number): number {
  return readEstimate(spanDays('week', Date.now(), viewerZone()), workspaces).total!
}

function runButtons(): HTMLElement[] {
  return screen.queryAllByRole('button', { name: /^Run/ })
}

beforeEach(() => {
  forgetLastRun()
  forgetOverTime()
  loadStats.mockReset()
  loadStats.mockReturnValue(new Promise(() => {}))
  loadPlatformTime.mockReset()
  loadPlatformTime.mockReturnValue(new Promise(() => {}))
  loadMe.mockReset()
  loadMe.mockResolvedValue(session(true))
  loadTenants.mockReset()
  loadTenants.mockResolvedValue(roster(3))
})

describe('one Run for the whole page (BGD, CTV-5)', () => {
  it('draws exactly one Run, in the head, and it is the page’s one primary button', async () => {
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadTenants).toHaveBeenCalled())
    // MUTATION: draw Over time's own Run again while the page passes `run`.
    expect(runButtons(), 'more than one Run on the page').toHaveLength(1)
    const only = runButtons()[0]!
    expect(only.closest('.c-phead > .c-acts'), 'the one Run is not the head’s').not.toBeNull()
    expect(document.querySelectorAll('button.is-primary'), 'a second primary button').toHaveLength(1)
    expect(only.classList.contains('is-primary')).toBe(true)
    expect(document.querySelector('.pot button.is-primary')).toBeNull()
  })

  it('prices both reads summed, the over-time half from the roster read on mount', async () => {
    render(<PlatformCountsScreen />)
    const want = PER_SCOPE * 2 + overTimeReads(3)
    // MUTATION: price the state counts alone (`24 reads`), or Over time alone.
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ~${want} reads`))
    // The roster moves it: four workspaces is four workspaces' stored days.
    expect(want).not.toBe(PER_SCOPE * 2 + overTimeReads(4))
  })

  it('reprices when the view’s bucket changes, since each bucket spans different days', async () => {
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ~${PER_SCOPE * 2 + overTimeReads(3)} reads`))
    fireEvent.click(screen.getByRole('radio', { name: /^Month/ }))
    const month = PER_SCOPE * 2 + readEstimate(spanDays('month', Date.now(), viewerZone()), 3).total!
    // MUTATION: report the price once, on mount.
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ~${month} reads`))
  })

  it('prices the over-time half per workspace while the roster is not known', async () => {
    loadTenants.mockResolvedValue({ status: 'error', error: { kind: 'admin_required', httpStatus: 403, code: null, message: 'no' } })
    render(<PlatformCountsScreen />)
    const per = readEstimate(spanDays('week', Date.now(), viewerZone()), null).perWorkspace
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ${PER_SCOPE * 2} reads + ~${per} a workspace`))
  })

  it('starts both reads on one press, and a second press while they run bills neither again', async () => {
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadTenants).toHaveBeenCalled())
    expect(loadStats).not.toHaveBeenCalled()
    expect(loadPlatformTime).not.toHaveBeenCalled()
    fireEvent.click(runButtons()[0]!)
    // MUTATION: drop the `run` prop from <PlatformOverTime />, and the over-time read never starts.
    await waitFor(() => expect(loadPlatformTime).toHaveBeenCalledTimes(1))
    expect(loadStats).toHaveBeenCalledTimes(1)
    fireEvent.click(runButtons()[0]!)
    expect(loadStats).toHaveBeenCalledTimes(1)
    expect(loadPlatformTime).toHaveBeenCalledTimes(1)
  })

  it('a non-admin’s Run reads the state counts only, and is priced as that', async () => {
    loadMe.mockResolvedValue(session(false))
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ${PER_SCOPE} reads`))
    fireEvent.click(runButtons()[0]!)
    await waitFor(() => expect(loadStats).toHaveBeenCalledTimes(1))
    // The platform-scope read would only be refused: it is not asked for.
    expect(loadPlatformTime).not.toHaveBeenCalled()
  })
})

describe('a caller nobody could name yet', () => {
  function counted(platform: boolean): Result<Stats> {
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        tenant_id: 'eng',
        tasks_by_state: { RUNNING: 1 },
        ...(platform ? { platform_tasks_by_state: { RUNNING: 4 } } : {}),
        dispatch_paused: false,
        limits: {},
        generated_at: new Date().toISOString(),
      } as Stats,
    }
  }

  beforeEach(() => {
    loadMe.mockResolvedValue({ status: 'error', error: { kind: 'upstream_degraded', httpStatus: 503, code: null, message: 'no session' } })
  })

  it('reads Over time once the count’s answer shows an admin', async () => {
    loadStats.mockResolvedValue(counted(true))
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadMe).toHaveBeenCalled())
    fireEvent.click(runButtons()[0]!)
    // MUTATION: press Over time only for an admin known from the session.
    await waitFor(() => expect(loadPlatformTime).toHaveBeenCalledTimes(1))
  })

  it('says the per-workspace part is the admin’s alone while the roster is not known either', async () => {
    loadTenants.mockResolvedValue({ status: 'error', error: { kind: 'admin_required', httpStatus: 403, code: null, message: 'no' } })
    render(<PlatformCountsScreen />)
    const per = readEstimate(spanDays('week', Date.now(), viewerZone()), null).perWorkspace
    // MUTATION: "12–24 reads + ~N a workspace" again, as if the 12 paid it too (review, 2026-10-25).
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ${PER_SCOPE} reads, or ${PER_SCOPE * 2} reads + ~${per} a workspace`))
  })

  it('does not read Over time when the count’s answer shows a tenant', async () => {
    loadStats.mockResolvedValue(counted(false))
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadMe).toHaveBeenCalled())
    fireEvent.click(runButtons()[0]!)
    await waitFor(() => expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0))
    expect(loadPlatformTime).not.toHaveBeenCalled()
  })
})

describe('mock-up F1’s order: Over time, then Right now, then the ticks', () => {
  it('draws Over time above the state counts, which sit under a Right now heading', async () => {
    render(<PlatformCountsScreen />)
    const over = screen.getByRole('region', { name: 'Over time' })
    const now = screen.getByRole('region', { name: /^Right now/ })
    const scopes = document.querySelector('.counts-scopes')!
    const ticks = document.querySelector('.counts-ticks')!
    // MUTATION: mount <PlatformOverTime /> after the state cards again.
    expect(over.compareDocumentPosition(scopes) & Node.DOCUMENT_POSITION_FOLLOWING, 'Over time is not above the state counts').toBeTruthy()
    expect(now.contains(scopes), 'the state counts are not in Right now').toBe(true)
    expect(over.compareDocumentPosition(now) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(now.compareDocumentPosition(ticks) & Node.DOCUMENT_POSITION_FOLLOWING, 'the ticks are not last').toBeTruthy()
    await waitFor(() => expect(loadTenants).toHaveBeenCalled())
  })

  it('before a Run, both sections say they have not been run', async () => {
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadTenants).toHaveBeenCalled())
    const over = screen.getByRole('region', { name: 'Over time' })
    const now = screen.getByRole('region', { name: /^Right now/ })
    expect(over.textContent ?? '').toMatch(/Not run yet\./)
    expect(over.querySelector('.pot-prov')?.textContent).toBe('not read yet')
    expect(now.querySelectorAll('.counts-notrun')).toHaveLength(2)
    expect(now.textContent ?? '').toContain('not run yet')
    expect(loadStats).not.toHaveBeenCalled()
    expect(loadPlatformTime).not.toHaveBeenCalled()
  })
})
