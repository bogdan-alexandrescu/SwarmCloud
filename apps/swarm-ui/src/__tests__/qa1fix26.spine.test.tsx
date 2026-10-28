// QA1-FIX-26 (part of #2596): the Spine findings of console visual QA pass 1,
// against a live-mode shell whose reads are served here.
//
//   * admin-shell-07: Tab out of the "Act as" list closes it; Escape closes it
//     and gives focus back to the switcher.
//   * admin-shell-21: the switcher names a personal workspace's kind
//     "personal", as Workspace Settings does.
//   * config-28: the foot calls the owner of a personal workspace its admin.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import { TENANT_PREF } from '../fetch'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

const OWN = { tenant_id: 'u-ana-b', display_name: 'Ana', kind: 'user' as const }
const ENG = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group' as const, group: 'eng@example.com', role: 'member' }

function me(tenantId: string) {
  return {
    tenant: { tenant_id: tenantId, display_name: tenantId === 'eng' ? 'Engineering' : 'Ana', kind: tenantId === 'eng' ? 'group' : 'user' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
}

function liveApi(mine: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get('X-Swarm-Tenant')
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) return ok(me(chosen ?? 'u-ana-b'))
    return ok({ pools: [] })
  }) as unknown as typeof fetch
}

async function shell() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null}>
      <button type="button" className="page-refresh">Refresh</button>
    </SkyShell>,
  )
}

const switcher = () => document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')
const picker = () => document.querySelector<HTMLElement>('.sk-tpop[role="dialog"]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.sessionStorage.removeItem(TENANT_PREF)
})

describe('admin-shell-07: the "Act as" list from the keyboard', () => {
  // MUTATION: drop `close.current()` from TenantPicker's onBlur and the list
  // stays open over the page.
  it('closes when Tab takes focus out of it into the page', async () => {
    liveApi([OWN, ENG])
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    switcher()!.focus()
    fireEvent.click(switcher()!)
    expect(picker()).not.toBeNull()
    const rows = [...picker()!.querySelectorAll<HTMLElement>('.sk-to')]
    const last = rows[rows.length - 1]!
    last.focus()
    const page = document.querySelector<HTMLButtonElement>('.page-refresh')!
    act(() => {
      fireEvent.blur(last, { relatedTarget: page })
      page.focus()
    })
    expect(picker()).toBeNull()
    expect(switcher()!.getAttribute('aria-expanded')).toBe('false')
  })

  // MUTATION: drop the `focusin` listener from TenantPicker's effect and a
  // focusout with no relatedTarget leaves the list open over the page.
  it('closes when focus lands in the page though the focusout named no target', async () => {
    liveApi([OWN, ENG])
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    switcher()!.focus()
    fireEvent.click(switcher()!)
    const rows = [...picker()!.querySelectorAll<HTMLElement>('.sk-to')]
    const last = rows[rows.length - 1]!
    last.focus()
    const page = document.querySelector<HTMLButtonElement>('.page-refresh')!
    act(() => {
      fireEvent.blur(last, { relatedTarget: null })
      fireEvent.focusIn(page)
    })
    expect(picker()).toBeNull()
  })

  // MUTATION: take `opener?.focus()` out of TenantPicker's cleanup and focus
  // is left wherever it was.
  it('gives focus back to the switcher on Escape', async () => {
    liveApi([OWN, ENG])
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    switcher()!.focus()
    fireEvent.click(switcher()!)
    expect(picker()!.contains(document.activeElement)).toBe(true)
    act(() => {
      fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    })
    expect(picker()).toBeNull()
    expect(document.activeElement).toBe(switcher())
  })
})

describe('admin-shell-21: the switcher names a personal workspace personal', () => {
  // MUTATION: print `workspace ·` whatever the kind.
  it('says personal for the person\'s own workspace and workspace for a group one', async () => {
    liveApi([OWN, ENG])
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    expect(switcher()!.querySelector('small')?.textContent).toBe('personal · 1 of 2')
    window.sessionStorage.setItem(TENANT_PREF, 'eng')
    cleanup()
    await shell()
    await waitFor(() => expect(switcher()?.querySelector('b')?.textContent).toBe('Engineering'))
    expect(switcher()!.querySelector('small')?.textContent).toBe('workspace · 2 of 2')
  })
})

describe('config-28: the foot calls the owner of a personal workspace its admin', () => {
  // MUTATION: drop the `here.kind === 'user'` line from roleLine.
  it('says Workspace admin in the personal workspace and Member in a group one', async () => {
    const { roleLine } = await import('../Spine')
    const mine = { status: 'ok' as const, data: [OWN, ENG], fetchedAt: 0 }
    expect(roleLine(false, mine as never, 'u-ana-b')).toBe('Workspace admin')
    expect(roleLine(false, mine as never, 'eng')).toBe('Member')
  })
})

describe('capacity-11: the Pools badge counts what "Needs a look" counts', () => {
  const pool = (over: Record<string, unknown> & { name: string }) => ({
    hard_limit: 5, adaptive_target: null, quota_derived_limit: null, effective_limit: 5,
    active: 0, available: 5, enabled: true, updated_at: '2026-10-28T09:00:00Z', ...over,
  })
  // MUTATION: count only `enabled === false` or a positive limit with no room
  // again and the pool lowered to 0 is left out (2, not 3).
  it('counts a pool lowered to 0 of 5, which has no room, with the full and the paused', async () => {
    const { panelCounts } = await import('../Spine')
    const { needsALook } = await import('../Capacity')
    const pools = [
      pool({ name: 'global', hard_limit: 100, effective_limit: 100, available: 90, active: 10 }),
      pool({ name: 'provider:anthropic', effective_limit: 5, active: 5, available: 0 }),
      pool({ name: 'resource:large', enabled: false }),
      pool({ name: 'provider:openai:tenant:eng', adaptive_target: 0, effective_limit: 0, available: 0 }),
    ]
    const cap = { status: 'ok' as const, fetchedAt: 0, data: { pools } }
    const out = panelCounts({ status: 'loading', since: 0 }, cap as never)
    expect(out.pools?.n).toBe(3)
    expect(out.pools?.n).toBe(pools.filter((p) => needsALook(p as never, null)).length)
    expect(out.pools?.why).toBe('3 pools need a look: full, lowered, paused or near the ceiling')
  })
})
