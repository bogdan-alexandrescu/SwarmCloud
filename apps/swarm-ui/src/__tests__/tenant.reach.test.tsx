/**
 * A PLATFORM ADMIN'S REACH IN THE SWITCHER (lane ROLES-UI-ADMIN; part of
 * #1518; docs/design/workspace-roles.md §4.2.2 step 5, §4.8, owner answer Q5).
 *
 * `GET /v1/tenants/mine` lists a platform admin's memberships, then every
 * registered group workspace they reach only as a platform admin, each marked
 * `via: "platform_admin"`. The switcher draws those under their own heading,
 * "Other workspaces (platform admin)", and while one is selected every screen
 * carries the banner saying so and that every action is recorded. A caller
 * with no reach entry -- anyone who is not a platform admin -- sees the
 * switcher exactly as before: one heading, no banner.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { TENANT_HEADER } from '../fetch'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })

type Mine = { tenant_id: string; display_name: string; kind: 'user' | 'group'; is_default?: boolean; via?: string }

function liveApi(mine: Mine[], isAdmin: boolean) {
  const f = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER)
    const acting = chosen ?? mine.find((m) => m.is_default)?.tenant_id ?? mine[0]!.tenant_id
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) {
      return ok({
        tenant: { tenant_id: acting, display_name: acting },
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
        environment: 'dev',
        environment_declared: true,
      })
    }
    return ok({ pools: [] })
  })
  globalThis.fetch = f as unknown as typeof fetch
}

async function openPicker() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
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
  return document.querySelector<HTMLElement>('.sk-tpop[role="dialog"]')!
}

const headings = (pop: HTMLElement) => [...pop.querySelectorAll('.sk-th')].map((h) => h.textContent)
const rowIds = (el: Element) => [...el.querySelectorAll('.sk-to small')].map((s) => s.textContent)
const banner = () => document.querySelector<HTMLElement>('[data-banner="platform-admin"]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

const OWN: Mine = { tenant_id: 'u-ana-b', display_name: 'Your workspace (u-ana-b)', kind: 'user', is_default: true, via: 'personal' }
const ENG: Mine = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member' }
const OPS: Mine = { tenant_id: 'ops', display_name: 'ops@example.com', kind: 'group', via: 'platform_admin' }
const SEC: Mine = { tenant_id: 'sec', display_name: 'sec@example.com', kind: 'group', via: 'platform_admin' }

describe("a platform admin's switcher", () => {
  it('lists the reach workspaces under their own heading, after the memberships', async () => {
    // MUTATION: draw every entry under the one membership heading.
    liveApi([OWN, ENG, OPS, SEC], true)
    const pop = await openPicker()
    expect(headings(pop)).toEqual(['Act as · 2 workspaces you are a member of', 'Other workspaces (platform admin)'])
    const reach = pop.querySelector<HTMLElement>('[role="group"][aria-label="Other workspaces (platform admin)"]')!
    expect(reach, 'no reach group').not.toBeNull()
    expect(rowIds(reach)).toEqual(['ops', 'sec'])
    // Every row is in the one list the arrows walk, memberships first.
    expect(rowIds(pop)).toEqual(['u-ana-b', 'eng', 'ops', 'sec'])
    expect(banner()).toBeNull()
  })

  it('shows the platform-admin banner once a reach workspace is selected, and not for a membership', async () => {
    // MUTATION: never draw the banner, or draw it for every group workspace.
    liveApi([OWN, ENG, OPS], true)
    let pop = await openPicker()
    fireEvent.click([...pop.querySelectorAll<HTMLButtonElement>('.sk-to')].find((b) => b.querySelector('small')?.textContent === 'eng')!)
    await waitFor(() => expect(document.querySelector('.sk-tenant .sk-tsw')?.textContent).toContain('eng'))
    expect(banner()).toBeNull()
    fireEvent.click(document.querySelector<HTMLButtonElement>('.sk-tenant .sk-tsw')!)
    pop = document.querySelector<HTMLElement>('.sk-tpop[role="dialog"]')!
    fireEvent.click([...pop.querySelectorAll<HTMLButtonElement>('.sk-to')].find((b) => b.querySelector('small')?.textContent === 'ops')!)
    const b = await waitFor(() => {
      const el = banner()
      expect(el).not.toBeNull()
      return el!
    })
    expect(b.textContent).toContain('You are acting in ops as a platform admin.')
    expect(b.textContent).toContain('Every action is recorded in its audit log.')
  })
})

describe("a non-admin's switcher", () => {
  it('is unchanged: one heading, every membership, no banner', async () => {
    // MUTATION: draw the reach heading even when there is no reach entry.
    liveApi([OWN, ENG], false)
    const pop = await openPicker()
    expect(headings(pop)).toEqual(['Act as · 2 workspaces you are a member of'])
    expect(pop.querySelector('[role="group"]')).toBeNull()
    expect(rowIds(pop)).toEqual(['u-ana-b', 'eng'])
    fireEvent.click([...pop.querySelectorAll<HTMLButtonElement>('.sk-to')].find((b) => b.querySelector('small')?.textContent === 'eng')!)
    await waitFor(() => expect(document.querySelector('.sk-tenant .sk-tsw')?.textContent).toContain('eng'))
    expect(banner()).toBeNull()
  })
})
