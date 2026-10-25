// ADMIN › TENANTS, ITS LEAD (ADMIN-LEADS; part of #2157, proposal E S10d and
// S10i).
//
// "Every Admin page opens with what needs an admin now, in one sentence with
// one button, and a green 'nothing waiting' lead when that is the truth."
// Tenants is "led by the workspace waiting to be created".
//
// WHAT EACH CASE HOLDS:
//   * a workspace waiting for this admin's approval leads ahead of one being
//     set up, and its one button opens its request in "Being created";
//   * a workspace whose setting up stopped leads ahead of one being set up;
//   * one being set up, with nothing stopped or waiting, leads in the info
//     tone: nothing needs the admin unless a step fails;
//   * nothing being created is the green "Nothing waiting" lead, no button;
//   * a "Being created" read that failed, and a roster read that failed, are
//     each a dash with its reason, never green;
//   * the lead reads only the page's own reads: no route is added.
//
// The api module is replaced so the page renders from records built here.
// Nothing here is token-shaped.

import ADMIN_CSS from '../styles/admin.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { ApiError, Result } from '../fetch'
import type { AdminWorkspaceView, Capacity, Profile, Tenant } from '../types'
import { serve } from './repofixture'

const api = vi.hoisted(() => ({
  loadTenants: vi.fn(),
  loadCapacity: vi.fn(),
  loadProfile: vi.fn(),
  loadAdminWorkspaces: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TenantsScreen } = await import('../Activity')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const FAILED: ApiError = { kind: 'server_error', httpStatus: 500, code: null, message: 'Internal Server Error' }

function tenant(id: string): Tenant {
  return {
    tenant_id: id,
    kind: 'group',
    principal: `${id}@example.com`,
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 20,
    capacity_units: 20,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: `swarm-agent-worker-${id}@example.iam.gserviceaccount.com`,
    gcs_prefix: null,
    namespace: null,
  }
}

function profileOf(role: 'owner' | 'admin'): Profile {
  return {
    identity: {
      email: 'operator@example.com',
      display_name: null,
      display_name_suggested: null,
      domain: 'example.com',
      platform_role: role,
      is_admin: true,
      is_admin_unresolved: false,
    },
    workspaces: [],
    revision: 1,
    unavailable: [],
  } as unknown as Profile
}

function ws(over: Partial<AdminWorkspaceView>): AdminWorkspaceView {
  return {
    state: 'applying',
    kind: 'group',
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    workspace_id: 'w-research',
    display_name: 'Example Research',
    principal: 'research@example.com',
    requested_at: '2026-10-24T08:00:00Z',
    decision: null,
    held: null,
    limits: { max_active: 8, capacity_units: 8 },
    steps: { A1: { state: 'done', at: '2026-10-24T08:01:00Z', code: null } },
    failure: null,
    ...over,
  }
}

const APPLYING = ws({})
const REQUESTED = ws({ state: 'requested', workspace_id: 'w-labs', display_name: 'Example Labs', principal: 'labs@example.com', steps: {} })
const FAILED_SETUP = ws({
  state: 'failed',
  workspace_id: 'w-pod',
  display_name: 'Research Pod',
  principal: 'pod@example.com',
  failure: { code: 'NAMESPACE_APPLY_FAILED', step: 'A7', retryable: true, at: '2026-10-24T08:05:00Z', copy: 'The namespace was not applied.' },
})

beforeEach(() => {
  serve(() => null)
  api.loadTenants.mockReset()
  api.loadCapacity.mockReset()
  api.loadProfile.mockReset()
  api.loadAdminWorkspaces.mockReset()
  api.loadCapacity.mockResolvedValue(ok({ pools: [], runner_profiles: {} } as unknown as Capacity))
  api.loadProfile.mockResolvedValue(ok(profileOf('admin')))
})

async function lead(records: Result<AdminWorkspaceView[]>, roster: Result<{ tenants: Tenant[] }> = ok({ tenants: [tenant('example-team')] })): Promise<HTMLElement> {
  api.loadTenants.mockResolvedValue(roster)
  api.loadAdminWorkspaces.mockResolvedValue(records)
  render(<TenantsScreen />)
  return waitFor(() => {
    const l = document.querySelector<HTMLElement>('.adm-lead')
    expect(l, 'Tenants drew no lead').not.toBeNull()
    return l as HTMLElement
  }, WAIT)
}

const text = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('Tenants leads with the workspace waiting to be created (S10d)', () => {
  it('names the one waiting for approval first, and its button opens its request', async () => {
    const l = await lead(ok([APPLYING, REQUESTED]))
    expect(l.dataset.lead).toBe('warn')
    expect(text(l)).toContain('Example Labs is waiting for your approval')
    expect(within(l).getAllByRole('button')).toHaveLength(1)
    fireEvent.click(within(l).getByRole('button', { name: 'Open its request' }))
    const row = document.querySelector<HTMLElement>('[data-testid="being-created"] [data-workspace-id="w-labs"]')
    expect(row).not.toBeNull()
    expect(document.activeElement).toBe(row)
    // The request it opens is the one with Approve on it.
    expect(within(row!).getByRole('button', { name: 'Approve' })).toBeTruthy()
  })

  // RENDERED 2026-10-25 (ADMIN-LEADS review): a mouse click focuses the row
  // with no `:focus-visible`, so the record it opened carried no mark at all.
  // The row is marked `is-opened`, which styles/admin.css outlines.
  it('marks the record it opened, where a click draws no focus ring', async () => {
    const l = await lead(ok([APPLYING, REQUESTED]))
    const row = () => document.querySelector<HTMLElement>('[data-workspace-id="w-labs"]')!
    expect(row().classList.contains('is-opened')).toBe(false)
    fireEvent.click(within(l).getByRole('button', { name: 'Open its request' }))
    await waitFor(() => expect(row().classList.contains('is-opened')).toBe(true), WAIT)
    expect(document.querySelector('[data-workspace-id="w-research"]')!.classList.contains('is-opened')).toBe(false)
    expect(ADMIN_CSS).toMatch(/\.ten-being-created > li\.is-opened\s*\{[^}]*outline:/)
  })

  it('names one whose setting up stopped ahead of one being set up', async () => {
    const l = await lead(ok([APPLYING, FAILED_SETUP]))
    expect(l.dataset.lead).toBe('warn')
    expect(text(l)).toContain('Setting up Research Pod stopped at A7')
    fireEvent.click(within(l).getByRole('button', { name: 'Open its request' }))
    expect(document.activeElement).toBe(document.querySelector('[data-workspace-id="w-pod"]'))
  })

  it('names one being set up in the info tone, and its button opens its progress', async () => {
    const l = await lead(ok([APPLYING]))
    expect(l.dataset.lead).toBe('next')
    expect(text(l)).toContain('Example Research is being created')
    expect(text(l)).toContain('Nothing needs you unless a step fails')
    fireEvent.click(within(l).getByRole('button', { name: 'Open its progress' }))
    expect(document.activeElement).toBe(document.querySelector('[data-workspace-id="w-research"]'))
  })

  it('sits above "Being created" and the roster', async () => {
    const l = await lead(ok([APPLYING]))
    const section = document.querySelector('[data-testid="being-created"]') as HTMLElement
    expect(l.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('nothing waiting reads the green lead (S10d)', () => {
  it('says no workspace is waiting, with no button', async () => {
    const l = await lead(ok([ws({ state: 'ready', workspace_id: 'w-done' })]))
    expect(l.dataset.lead).toBe('ok')
    expect(text(l)).toContain('Nothing waiting')
    expect(text(l)).toContain('No workspace is waiting to be created')
    expect(within(l).queryAllByRole('button')).toHaveLength(0)
  })
})

describe('a read that failed is a dash with its reason, never green', () => {
  it('when "Being created" could not be read', async () => {
    const l = await lead({ status: 'error', error: FAILED })
    expect(l.dataset.lead).toBe('unread')
    expect(text(l)).toContain('—, the workspaces being created could not be read')
    expect(text(l)).toContain('The API failed on this request')
    expect(document.querySelector('.adm-lead[data-lead="ok"]')).toBeNull()
  })

  it('when the roster could not be read', async () => {
    const l = await lead(ok([]), { status: 'error', error: FAILED })
    expect(l.dataset.lead).toBe('unread')
    expect(text(l)).toContain('—, the tenants could not be read')
    expect(document.querySelector('.adm-lead[data-lead="ok"]')).toBeNull()
  })
})
