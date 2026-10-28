// CONSOLE VISUAL QA PASS 1, LANE QA1-FIX-18 (admin-shell-15; part of #2596).
//
// Before a run an admin's button said `Run the count · ~562 reads` and Over
// time's note under it `The price is ~538 reads`: the 24 between them are the
// two twelve-read state snapshots (the workspace's and the platform's), and
// nothing on the page added them up. Right now's note says the parts and the
// button's total, in the button's own terms for each kind of caller.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

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


// The Right now note: what the button's total is made of.
const note = () => document.querySelector<HTMLElement>('.counts-now .sb-note')

describe('admin-shell-15: the page says what the Run’s total is made of', () => {
  it('an admin with a known roster: Over time’s part plus two snapshots of twelve, summing to the button', async () => {
    render(<PlatformCountsScreen />)
    const over = overTimeReads(3)
    const total = PER_SCOPE * 2 + over
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ~${total} reads`))
    // MUTATION: keep "A snapshot, read by the same Run." alone, and ~538 under Over time never meets ~562 on the button.
    expect(note()!.textContent).toBe(`A snapshot, read by the same Run: 2 × ${PER_SCOPE} reads, which with Over time’s ~${over} make its ~${total}.`)
  })

  it('while nobody can say who is asking, the parts are an admin’s', async () => {
    loadMe.mockResolvedValue({ status: 'error', error: { kind: 'network', httpStatus: null, code: null, message: 'no' } } as unknown as Result<Me>)
    render(<PlatformCountsScreen />)
    const over = overTimeReads(3)
    const total = PER_SCOPE * 2 + over
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ${PER_SCOPE}–~${total} reads`))
    // MUTATION: drop "an admin’s", and a member reads the ~${total} as their own.
    expect(note()!.textContent).toBe(`A snapshot, read by the same Run: 2 × ${PER_SCOPE} reads, which with Over time’s ~${over} make an admin’s ~${total}.`)
  })

  it('while the roster is not known, Over time’s part is per workspace, as the button says', async () => {
    loadTenants.mockResolvedValue({ status: 'error', error: { kind: 'admin_required', httpStatus: 403, code: null, message: 'no' } })
    render(<PlatformCountsScreen />)
    const per = readEstimate(spanDays('week', Date.now(), viewerZone()), null).perWorkspace
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ${PER_SCOPE * 2} reads + ~${per} a workspace`))
    expect(note()!.textContent).toBe(`A snapshot, read by the same Run: 2 × ${PER_SCOPE} of its reads, the rest Over time’s ~${per} a workspace.`)
  })

  it('a non-admin’s Run is the snapshot alone, so the note adds nothing up', async () => {
    loadMe.mockResolvedValue(session(false))
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(runButtons()[0]!.textContent).toBe(`Run the count · ${PER_SCOPE} reads`))
    expect(note()!.textContent).toBe('A snapshot, read by the same Run.')
  })
})
