/**
 * THE TENANT SWITCHER (owner decision 2026-10-01; docs/multi-tenancy.md).
 *
 * The choice SELECTS among the caller's verified memberships and never grants
 * one: the server refuses any `X-Swarm-Tenant` it did not confirm. What the
 * client must get right is narrower and is held here:
 *
 *   * the header leaves from ONE place (fetch.ts's `apiHeaders`), on every
 *     read and write, and only when a choice is stored;
 *   * the choice is per browser, and storage that throws costs nothing;
 *   * a stored tenant the caller is no longer in is dropped on the first
 *     `tenant_not_member` 403 and the default used -- and no other 403 drops it;
 *   * the spine's tenant block is a switcher only for more than one tenant,
 *     and switching re-reads the screen under the new tenant.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { useEffect, useState } from 'react'

import { TENANT_HEADER, TENANT_PREF, apiHeaders, chooseTenant, chosenTenant, read, route, write } from '../fetch'
import { artifactRawUrl } from '../api'
import { postWorkflow } from '../SubmitWorkflow'

const JSON_HEADERS = { 'content-type': 'application/json' }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })
const refused = (code: string, message: string) =>
  new Response(JSON.stringify({ code, message }), { status: 403, headers: JSON_HEADERS })

/** Every request's X-Swarm-Tenant, in order; `null` where none was sent. */
function sentTenants(fetchMock: ReturnType<typeof vi.fn>): (string | null)[] {
  return fetchMock.mock.calls.map(([, init]) => {
    const h = new Headers((init as RequestInit | undefined)?.headers)
    return h.get(TENANT_HEADER)
  })
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  try {
    window.localStorage.clear()
  } catch {
    /* a test that broke storage put it back already */
  }
})

describe('the header leaves from the one fetch helper', () => {
  it('is named X-Swarm-Tenant', () => {
    expect(TENANT_HEADER).toBe('X-Swarm-Tenant')
  })

  it('is not sent when no tenant was chosen', async () => {
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    await read(route('/v1/capacity'), () => false)
    await write(route('/v1/tasks'), 'POST', { x: 1 })
    expect(sentTenants(f)).toEqual([null, null])
  })

  it('is sent on every read and write once a tenant is chosen', async () => {
    chooseTenant('research')
    expect(window.localStorage.getItem(TENANT_PREF)).toBe('research')
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    await read(route('/v1/capacity'), () => false)
    await write(route('/v1/tasks'), 'POST', { x: 1 })
    expect(sentTenants(f)).toEqual(['research', 'research'])
    expect(apiHeaders({ accept: 'application/json' })).toEqual({ accept: 'application/json', [TENANT_HEADER]: 'research' })
  })

  it('costs nothing when storage throws', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('private window')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('private window')
    })
    expect(() => chooseTenant('research')).not.toThrow()
    expect(chosenTenant()).toBeNull()
    const f = vi.fn(async () => ok({ pools: [] }))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/capacity'), () => false)
    expect(r.status).toBe('ok')
    expect(sentTenants(f)).toEqual([null])
    getItem.mockRestore()
    setItem.mockRestore()
  })

  it('every direct fetch() outside fetch.ts builds its headers with apiHeaders', () => {
    // A submit path that called `fetch` with its own headers would file the
    // work under the DEFAULT tenant while the switcher shows another one.
    const dir = join(__dirname, '..')
    const files = readdirSync(dir).filter((n) => /\.(ts|tsx)$/.test(n) && n !== 'fetch.ts')
    const offenders: string[] = []
    let calls = 0
    for (const name of files) {
      const src = readFileSync(join(dir, name), 'utf8')
      for (const m of src.matchAll(/\bawait fetch\(/g)) {
        calls++
        // The call's own options object: up to the line that closes it.
        const rest = src.slice(m.index!)
        const call = rest.slice(0, rest.search(/\n\s*\}\)/))
        if (!call.includes('headers: apiHeaders(')) offenders.push(name)
        // And forgets a refused stored tenant, as read() and write() do.
        if (!src.includes('dropRefusedTenant(')) offenders.push(`${name} (no dropRefusedTenant)`)
      }
    }
    // The sweep ran over the two submit paths, not over nothing.
    expect(calls).toBeGreaterThanOrEqual(2)
    expect(offenders).toEqual([])
  })
})

describe('a stored tenant the caller is no longer in', () => {
  it('is dropped on the first tenant_not_member 403, and the read retried on the default', async () => {
    chooseTenant('research')
    const f = vi
      .fn()
      .mockResolvedValueOnce(refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
      .mockResolvedValueOnce(ok({ pools: ['default'] }))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/capacity'), () => false)
    expect(r.status).toBe('ok')
    expect(sentTenants(f)).toEqual(['research', null])
    expect(chosenTenant()).toBeNull()
    expect(window.localStorage.getItem(TENANT_PREF)).toBeNull()
  })

  it('is dropped on a refused write, which is NOT retried under another tenant', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await write(route('/v1/tasks'), 'POST', { x: 1 })
    expect(r.status).toBe('error')
    expect(f).toHaveBeenCalledTimes(1)
    expect(chosenTenant()).toBeNull()
  })

  it('is kept on any other 403', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('forbidden', 'admin group membership is required for this operation'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/admin/tenants'), () => false)
    expect(r.status).toBe('error')
    expect(f).toHaveBeenCalledTimes(1)
    expect(chosenTenant()).toBe('research')
  })
})

// ---------------------------------------------------------------------------
// The spine's tenant block
// ---------------------------------------------------------------------------

const ENG = { tenant_id: 'eng', display_name: 'eng@example.com' }
const RESEARCH = { tenant_id: 'research', display_name: 'research@example.com' }

function me(tenantId: string) {
  return {
    tenant: { tenant_id: tenantId, display_name: tenantId === 'eng' ? 'Engineering' : 'Research' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
}

/** A live API with `mine` as the caller's verified memberships. */
function liveApi(mine: { tenant_id: string; display_name: string }[]) {
  const f = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const chosen = new Headers(init?.headers).get(TENANT_HEADER)
    if (url.includes('/v1/tenants/mine')) return ok(mine)
    if (url.includes('/v1/tenants/me')) return ok(me(chosen ?? mine[0]?.tenant_id ?? 'eng'))
    return ok({ pools: [] })
  })
  globalThis.fetch = f as unknown as typeof fetch
  return f
}

/** A screen whose read is counted, so a re-read after switching is visible. */
let screenReads = 0
function CountingScreen() {
  const [n, setN] = useState(0)
  useEffect(() => {
    screenReads++
    setN(screenReads)
  }, [])
  return <p data-reads={n}>screen</p>
}

async function shell() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SkyShell } = await import('../Spine')
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null}>
      <CountingScreen />
    </SkyShell>,
  )
}

const switcher = () => document.querySelector<HTMLSelectElement>('.sk-tenant select')

describe('the spine tenant block', () => {
  it('is a static label with copy-id for one tenant', async () => {
    liveApi([ENG])
    await shell()
    await waitFor(() => expect(document.querySelector('.sk-tenant b')?.textContent).toBe('Engineering'))
    expect(switcher()).toBeNull()
    expect(document.querySelector('.sk-tenant .sk-cp')).not.toBeNull()
  })

  it('is a static label for a caller in no tenant group', async () => {
    liveApi([])
    await shell()
    await waitFor(() => expect(document.querySelector('.sk-tenant b')).not.toBeNull())
    expect(switcher()).toBeNull()
  })

  it('is a switcher for more than one, and switching stores, sends and re-reads', async () => {
    const f = liveApi([ENG, RESEARCH])
    screenReads = 0
    await shell()
    await waitFor(() => expect(switcher()).not.toBeNull())
    const sel = switcher()!
    expect(Array.from(sel.options).map((o) => o.value)).toEqual(['eng', 'research'])
    expect(sel.value).toBe('eng')
    expect(document.querySelector('.sk-tenant .sk-cp')).not.toBeNull()
    const readsBefore = screenReads
    const callsBefore = f.mock.calls.length

    await act(async () => {
      fireEvent.change(sel, { target: { value: 'research' } })
    })

    expect(window.localStorage.getItem(TENANT_PREF)).toBe('research')
    await waitFor(() => expect(switcher()?.value).toBe('research'))
    // The screen under the shell was mounted afresh, so its reads ran again.
    expect(screenReads).toBeGreaterThan(readsBefore)
    // And every read after the switch carried the new tenant.
    const after = sentTenants(f).slice(callsBefore)
    expect(after.length).toBeGreaterThan(0)
    expect(after.every((t) => t === 'research')).toBe(true)
  })
})

describe('a refused tenant that storage will not forget', () => {
  it('is not retried with the same header', async () => {
    chooseTenant('research')
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await read(route('/v1/capacity'), () => false)
    expect(r.status).toBe('error')
    expect(f).toHaveBeenCalledTimes(1)
    removeItem.mockRestore()
  })
})

describe('a URL the browser fetches by itself', () => {
  // An <img src>, a download href and an "open full" tab carry no header, so
  // the choice rides as ?tenant= -- validated by the API exactly as the header.
  it('carries the chosen tenant as ?tenant=', () => {
    chooseTenant('research')
    const url = new URL(artifactRawUrl('task_r', 'out.txt', 'inline'), 'http://x')
    expect(url.pathname).toBe('/v1/tasks/task_r/artifacts/raw')
    expect(url.searchParams.get('tenant')).toBe('research')
    expect(url.searchParams.get('name')).toBe('out.txt')
  })

  it('carries nothing when no tenant was chosen', () => {
    const url = new URL(artifactRawUrl('task_r', 'out.txt', 'attachment'), 'http://x')
    expect(url.searchParams.has('tenant')).toBe(false)
  })
})

describe('a direct submit path refused for its tenant', () => {
  it('drops the stored tenant, is not retried, and is not an admin refusal', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('tenant_not_member', 'X-Swarm-Tenant names a tenant you are not a verified member of.'))
    globalThis.fetch = f as unknown as typeof fetch
    const r = await postWorkflow({ tasks: [] })
    expect(f).toHaveBeenCalledTimes(1)
    expect(sentTenants(f)).toEqual(['research'])
    expect(chosenTenant()).toBeNull()
    expect(r.kind).toBe('rejected')
    if (r.kind === 'rejected') expect(r.error.kind).toBe('tenant_unresolved')
  })

  it('keeps the stored tenant on any other 403', async () => {
    chooseTenant('research')
    const f = vi.fn(async () => refused('forbidden', 'admin group membership is required for this operation'))
    globalThis.fetch = f as unknown as typeof fetch
    await postWorkflow({ tasks: [] })
    expect(chosenTenant()).toBe('research')
  })
})
