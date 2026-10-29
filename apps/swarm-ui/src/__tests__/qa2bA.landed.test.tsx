// QA2B-A (part of #2694, console visual QA pass 2b, and #2391): the QA2-FIX-25 patches outside that lane's own
// territory, landed. Its helpers (healthyWithheld, canTake's state check, AdminRoleRefusal's audience) are held by
// qa2fix25.test.tsx; these cases hold the call sites that draw them. WHAT EACH CASE HOLDS:
//   * capacity-04: the Healthy tile names the healthy accounts the pool withholds;
//   * cap2-01: a paused account's can-take chip says why it is No;
//   * admin2-02: the frame refuses a workspace admin's own page as "for workspace admins", and the owner of only a
//     personal workspace is told it is their own, never called its admin.
//
// Fixtures and a stubbed fetch only; no network.

import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AccountHealthBody } from '../AccountHealthPage'
import { CanTakeChip } from '../AccountsTable'
import { loadAccountHealthBoard, loadAccountPool } from '../api'
import type { PoolAccount } from '../accountsPool'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('capacity-04: the Healthy tile names the accounts the pool withholds', () => {
  it('draws the withheld line under Healthy, naming shared and team-copy', async () => {
    const r = await loadAccountHealthBoard()
    if (r.status !== 'ok' || r.data.health.kind !== 'ok') throw new Error('the fixture board did not load')
    const { container } = render(<AccountHealthBody board={r.data} now={r.data.readAt} phone={false} />)
    const tile = container.querySelector('[data-tile="healthy"]') as HTMLElement
    expect(tile).not.toBeNull()
    // MUTATION: drop <WithheldLine> from Tiles and this is null.
    const line = tile.querySelector('[data-withheld]') as HTMLElement
    expect(line).not.toBeNull()
    expect(Number(line.getAttribute('data-withheld'))).toBeGreaterThanOrEqual(2)
    expect(line.textContent).toMatch(/of them cannot take an agent: /)
    expect(line.textContent).toContain('shared')
    expect(line.textContent).toContain('team-copy')
    // Only Healthy carries it.
    expect(container.querySelectorAll('[data-withheld]')).toHaveLength(1)
  })
})

describe('cap2-01: a paused account says why it cannot take an agent', () => {
  it('reads "No · paused", and a plain No stays plain', async () => {
    const r = await loadAccountPool()
    if (r.status !== 'ok') throw new Error('the fixture accounts did not load')
    const scope = r.data.tenant_id ?? null
    const primary = (r.data.accounts as PoolAccount[]).find((a) => a.account_id === 'u-bogdan:primary') as PoolAccount
    const paused: PoolAccount = { ...primary, state: 'PAUSED', reason: 'holiday', can_take_new_agent: true }
    const { container } = render(<CanTakeChip a={paused} scope={scope} />)
    // MUTATION: drop the PAUSED branch from CanTakeChip and this reads "No".
    expect(container.textContent).toBe('No · paused')
    cleanup()
    const draining: PoolAccount = { ...primary, state: 'DRAINING', can_take_new_agent: true }
    const plain = render(<CanTakeChip a={draining} scope={scope} />)
    expect(plain.container.textContent).toBe('No')
  })
})

describe('admin2-02: the frame refuses a workspace admin’s own page as such', () => {
  type Json = Record<string, unknown>
  const ME: Json = {
    tenant: { tenant_id: 'u-ana', display_name: 'Your workspace' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
  const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
  const ENG_MEMBER: Json = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'member', source: 'override' } }
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

  async function shell(tab: string, mine: Json[]) {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(mine))
      if (url.includes('/v1/tenants/me')) return Promise.resolve(json(ME))
      return Promise.resolve(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } }))
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="admin" tab={tab} title="x" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
  }
  const refusal = () => document.querySelector<HTMLElement>('[data-refused="admin"]')

  it('titles Workspaces you admin "for workspace admins" for a member of a group workspace', async () => {
    await shell('workspaces', [PERSONAL, ENG_MEMBER])
    await waitFor(() => expect(refusal()).not.toBeNull())
    // MUTATION: drop `audience` from the frame's AdminRoleRefusal and this reads "platform".
    expect(refusal()!.getAttribute('data-audience')).toBe('workspace')
    expect(refusal()!.textContent).toContain('Workspaces you admin is for workspace admins')
    expect(refusal()!.textContent).toContain('you administer none')
  })

  it('tells the owner of only a personal workspace it is their own, never its admin', async () => {
    await shell('people', [PERSONAL])
    await waitFor(() => expect(refusal()).not.toBeNull())
    expect(refusal()!.textContent).toContain('People and roles is for workspace admins')
    // MUTATION: drop `personal` from the frame's AdminRoleRefusal and this reads "You are an admin of ...".
    expect(refusal()!.textContent).toContain('is your own workspace')
    expect(refusal()!.textContent).not.toContain('an admin of')
  })

  it('keeps a platform-only page "for platform admins"', async () => {
    await shell('limits', [PERSONAL, ENG_MEMBER])
    await waitFor(() => expect(refusal()).not.toBeNull())
    expect(refusal()!.getAttribute('data-audience')).toBe('platform')
    expect(refusal()!.textContent).toContain('Pool limits is for platform admins')
  })
})
