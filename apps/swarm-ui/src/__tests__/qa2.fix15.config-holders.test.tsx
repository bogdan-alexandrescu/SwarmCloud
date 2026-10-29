// CONFIG SETUP AND CAPACITY › HOLDERS, VISUAL QA PASS 2 (QA2-FIX-15; part of #2694).
//
//   admin-shell-10, config2-03  the strip said "1 of 3 done" beside a link
//                "Workspace setup: 4 of 7 done": two counts, neither named.
//                The strip counts steps and says so; the link names the
//                checklist.
//   admin2-03, config2-04  "Next: GitHub connected, Enable orgs failed": the
//                next step named by its done-state label over its failure.
//                A "Next:" line names the task: "Fix GitHub access (Enable
//                orgs failed)".
//   cap2-08      under `?pool=provider:anthropic:tenant:u-bogdan` the filter
//                drew the raw id and the head said `every tenant`.
//   cap2-09      Accounting drift's head said "7 of 7 pools" and its foot
//                "over 2 rows": two counts, neither named.
//
// MUTATION: draw `nextStep.label` again in SetupProgress or the compact strip
// -- the first two cases read "GitHub connected". Drop " steps" from the
// strip's count or put "Workspace setup:" back in checklistCount -- the
// second reads the old counts. Draw `{pool}` in the chip again, or drop the
// pool from the head -- the third finds the raw id or `every tenant`. Put
// "over N rows" back in the drift foot -- the fourth finds it.

import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow, OnboardingDoc, Pool } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn(), loadTask: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { HoldersScreen } = await import('../Holders')
const { SetupContext, SetupProgress, SetupStrip, setupSteps } = await import('../ConfigSetup')

const WAIT = { timeout: 5000 } as const
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  api.loadTask.mockReset()
})

type Json = Record<string, unknown>
const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })

/** The fixture console's onboarding read (api.ts `fixtureOnboarding`): seven steps, Enable orgs failed. */
const doc = (): OnboardingDoc =>
  ({
    tenant_id: 'eng',
    user: 'alice@example.com',
    user_hash: 'h',
    derived_at: '2026-10-28T10:00:00Z',
    steps: [
      step('signed_in', 'done'),
      step('github_connected', 'done', { forge_login: 'alice-example' }),
      step('app_installed', 'done'),
      step('orgs_enabled', 'failed'),
      step('repos_chosen', 'done', { repositories: [{ repository: 'example-org/example-api' }] }),
      step('access_verified', 'in_progress'),
      step('ready', 'todo'),
    ],
    next_step: 'orgs_enabled',
    complete: false,
    source: 'derived',
  }) as unknown as OnboardingDoc

const strip = () =>
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: doc(), fetchedAt: 0 }, personal: true }}>
      <SetupStrip go={() => undefined} />
    </SetupContext.Provider>,
  )

describe('admin2-03, config2-04: the next step is named as a task, not by its done state', () => {
  it('Workspaces you admin: "Next: Fix GitHub access (Enable orgs failed)"', () => {
    const { container } = render(<SetupProgress name="eng" steps={setupSteps(doc(), true)} />)
    const next = visible(container.querySelector('.cfg-progress-next'))
    expect(next).toBe('Next: Fix GitHub access (Enable orgs failed)')
    expect(next).not.toContain('GitHub connected')
  })

  it('the phone strip: its link and its Next line name the fix', () => {
    const { container } = strip()
    const compact = container.querySelector('.cfg-compact')!
    expect(visible(compact.querySelector('a'))).toBe('Fix GitHub access ›')
    expect(visible(compact.querySelector('small'))).toMatch(/^Next: Fix GitHub access \(Enable orgs failed\)/)
    expect(visible(compact)).not.toContain('GitHub connected')
  })
})

describe('admin-shell-10, config2-03: each count names what it counts', () => {
  it('the strip counts steps; its link counts the checklist', () => {
    const { container } = strip()
    expect(visible(container.querySelector('.cfg-compact-h b'))).toBe('Setup 1 of 3 steps')
    expect(visible(container.querySelector('.cfg-count b'))).toBe('1 of 3 steps done')
    const section = screen.getByRole('region', { name: 'Workspace setup' })
    expect(within(section).getByRole('link', { name: 'Setup checklist: 4 of 7 done' })).toBeTruthy()
    expect(visible(section)).not.toContain('Workspace setup: 4 of 7')
  })

  it('Workspaces you admin says steps', () => {
    const { container } = render(<SetupProgress name="eng" steps={setupSteps(doc(), true)} />)
    expect(visible(container.querySelector('.cfg-progress-h b'))).toBe('Setup 1 of 3 steps')
  })
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function lease(n: number, pools: string[], tenant = 'u-bogdan'): LeaseRow {
  return {
    lease_id: `lease_${String(n).padStart(10, '0')}`,
    task_id: `task_${String(n).padStart(10, '0')}`,
    attempt_id: `att_${n}`,
    tenant_id: tenant,
    generation: 1,
    pools,
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-02T09:00:00Z',
    dispatch_deadline: '2026-10-02T09:05:00Z',
    expires_at: '2026-10-02T11:00:00Z',
    heartbeat_at: '2026-10-02T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
  } as LeaseRow
}

const RAW = 'provider:anthropic:tenant:u-bogdan'

function board(): HoldersBoard {
  const leases = [lease(1, ['global', RAW]), lease(2, ['global']), lease(3, ['global'], 'eng')]
  return {
    page: {
      leases,
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-02T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: leases.length,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: [{ name: 'global', active: 3 }, { name: RAW, active: 4 }, { name: 'resource:standard', active: 0 }] as unknown as Pool[],
    poolsDetail: null,
  }
}

describe('cap2-08: the pool filter names the pool, and the head says it is filtered', () => {
  it('draws the pool by its name and the head by the pool, never `every tenant`', async () => {
    window.history.replaceState(null, '', `/capacity/holders?pool=${encodeURIComponent(RAW)}`)
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    const { container } = render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const chip = await waitFor(() => {
      const el = container.querySelector('.hold-pool')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(chip)).toContain('of pool anthropic · u-bogdan')
    expect(visible(chip), 'the chip still draws the raw pool id').not.toContain(RAW)
    expect(chip.querySelector(`[title="${RAW}"]`)).not.toBeNull()
    const text = visible(container)
    expect(text).toContain('1 unreleased lease · 1 units held · anthropic · u-bogdan only')
    expect(text).not.toContain('every tenant')
  })
})

describe('cap2-09: the drift card says what each count counts', () => {
  it('reads "pools compared" in its head and "leases read" in its foot', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    render(<HoldersScreen />)
    const card = (await screen.findByRole('heading', { name: /Accounting drift/ }, WAIT)).closest('section')!
    expect(visible(card.querySelector('.ctl-card-note'))).toBe('3 of 3 pools compared')
    const foot = visible(card.querySelector('.ctl-card-foot'))
    expect(foot).toContain('against 3 leases read')
    expect(foot).not.toMatch(/rows?\b/)
  })
})
