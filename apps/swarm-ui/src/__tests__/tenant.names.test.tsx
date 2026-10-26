/**
 * THE SWITCHER NAMES A RENAMED WORKSPACE BY ITS NEW NAME AT ONCE (MINE-NAMES,
 * part of #2157). BGD, 2026-10-25: when a workspace's admins rename it
 * (WORKSPACE-ADMIN-FIELDS), everyone's switcher shows the new name, not only
 * after they next act in that workspace. `/v1/tenants/mine` now serves a
 * group workspace's stored name in `display_name`, with the group email as
 * `group`; the list prefers that over the name this browser last saw from
 * `/v1/tenants/me`. An entry without `group` is today's (the document could
 * not be read, or names nothing yet): the last-seen name still wins there.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import { TENANT_HEADER } from '../fetch'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

const ENG = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member' }

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

function liveApi(mine: object[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER) ?? 'eng'
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) {
      return ok({
        tenant: { tenant_id: chosen, display_name: chosen === 'eng' ? 'Engineering' : 'Research' },
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
        environment: 'dev',
        environment_declared: true,
      })
    }
    return ok({ pools: [] })
  }) as unknown as typeof fetch
}

async function openSwitcher() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell, TENANT_NAMES_STORE } = await import('../Spine')
  // This browser last saw research, while acting there, by its old name.
  window.localStorage.setItem(TENANT_NAMES_STORE, JSON.stringify({ research: 'Research (old)' }))
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null} keepOnSwitch={false}>
      <p>screen</p>
    </SkyShell>,
  )
  const btn = await waitFor(() => {
    const b = document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')
    expect(b).not.toBeNull()
    return b!
  })
  fireEvent.click(btn)
  return [...document.querySelectorAll<HTMLButtonElement>('.sk-tpop .sk-to')]
}

const named = (rows: HTMLButtonElement[], id: string) =>
  rows.find((r) => r.querySelector('small')?.textContent === id)?.querySelector('b')?.textContent

describe('a workspace the person is not acting in', () => {
  it('is drawn by the name /mine served, not the one this browser last saw', async () => {
    // MUTATION: put `known[c.tenant_id]` before the served name in nameTenants.
    liveApi([ENG, { tenant_id: 'research', display_name: 'Research Lab', group: 'research@example.com', kind: 'group', via: 'member' }])
    const rows = await openSwitcher()
    expect(named(rows, 'research')).toBe('Research Lab')
    const row = rows.find((r) => r.querySelector('small')?.textContent === 'research')!
    expect(row.getAttribute('title')).toBe('registered as research@example.com')
    // The workspace in force is still named by /v1/tenants/me.
    expect(named(rows, 'eng')).toBe('Engineering')
  })

  it('keeps the last-seen name when /mine served only the group email', async () => {
    // An entry without `group`: the document was missing or unreadable.
    liveApi([ENG, { tenant_id: 'research', display_name: 'research@example.com', kind: 'group', via: 'member' }])
    const rows = await openSwitcher()
    expect(named(rows, 'research')).toBe('Research (old)')
  })
})
