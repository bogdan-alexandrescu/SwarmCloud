// ADMIN › TENANTS, THE 2026-10-07 QA PASS (G5-12, G5-13).
//
//   G5-12  every Identity read "swarm-agent-work…" and every Principal ended
//          "…@saga.x…": the end-ellipsis cut exactly the part that tells one
//          tenant's identity from the next. The cut moves to the middle; the
//          tenant's own part is never cut.
//   G5-13  Enforced was `Math.min(max_active, capacity_units)` computed here,
//          a second statement of a rule the tenant pool already enforces, and
//          one that mixes agents with units. It is the pool's own
//          `effective_limit`, read from `/v1/capacity`.

import SHEET from '../styles.css?raw'
import ADMIN from '../styles/admin.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, Tenant } from '../types'
import { cascade, type CascadeEnv } from './cssgate'

const STYLES = `${SHEET}\n${ADMIN}`

const api = vi.hoisted(() => ({
  loadTenants: vi.fn(),
  loadCapacity: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TenantsScreen } = await import('../Activity')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 } as const

const DOMAIN = 'saga-agents.iam.gserviceaccount.com'

function tenant(over: Partial<Tenant>): Tenant {
  return {
    tenant_id: 'eng',
    kind: 'group',
    principal: 'eng@saga.xyz',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 45,
    capacity_units: 40,
    monthly_budget_usd: null,
    enabled: true,
    credentials: [],
    service_account: `swarm-agent-worker-eng@${DOMAIN}`,
    gcs_prefix: null,
    namespace: null,
    ...over,
  }
}

function pool(name: string, effective: number | null, hard: number | null = effective): Pool {
  return {
    name,
    hard_limit: hard,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: effective,
    active: 0,
    available: effective,
    enabled: true,
    updated_at: '2026-10-07T09:00:00Z',
  }
}

const ROSTER: Tenant[] = [
  tenant({}),
  tenant({
    tenant_id: 'u-bogdan',
    kind: 'user',
    principal: 'bogdan@saga.xyz',
    max_active: 4,
    capacity_units: 8,
    service_account: `swarm-agent-worker-u-bogdan@${DOMAIN}`,
  }),
  tenant({ tenant_id: 'smoke', max_active: 10, capacity_units: 4, service_account: null }),
]

function serve(capacity: Result<Capacity>) {
  api.loadTenants.mockResolvedValue({ status: 'ok', data: { tenants: ROSTER }, fetchedAt: Date.now() })
  api.loadCapacity.mockResolvedValue(capacity)
}

function capacity(pools: Pool[]): Result<Capacity> {
  return { status: 'ok', data: { pools, runner_profiles: {} } as Capacity, fetchedAt: Date.now() }
}

async function roster(): Promise<HTMLElement> {
  const { container } = render(<TenantsScreen />)
  await screen.findByText('u-bogdan', undefined, WAIT)
  return container
}

function row(container: HTMLElement, id: string): HTMLTableRowElement {
  const th = [...container.querySelectorAll('tbody th[scope="row"]')].find((h) => h.textContent === id)
  expect(th, `no row for ${id}`).toBeTruthy()
  return th!.closest('tr') as HTMLTableRowElement
}

function won(el: Element, prop: string | readonly string[], env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, prop, env)
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

beforeEach(() => {
  api.loadTenants.mockReset()
  api.loadCapacity.mockReset()
})

describe('G5-12: an identity is cut in the middle, never through the tenant’s own part', () => {
  it('keeps the tenant suffix of a service account whole and cuts the shared prefix and the domain', async () => {
    serve(capacity([]))
    const c = await roster()
    const ident = row(c, 'u-bogdan').querySelector('td[data-label="Identity"] .ten-ident')!
    const full = `swarm-agent-worker-u-bogdan@${DOMAIN}`
    // The whole value is still the element's text and its title: a selection
    // copies all of it, and a hover shows all of it.
    expect(ident.textContent).toBe(full)
    expect(ident.getAttribute('title')).toBe(full)
    const head = ident.querySelector('.ten-ident-head')
    const key = ident.querySelector('.ten-ident-key')
    const tail = ident.querySelector('.ten-ident-tail')
    expect(head?.textContent).toBe('swarm-agent-worker-')
    expect(key?.textContent).toBe('u-bogdan@')
    expect(tail?.textContent).toBe(DOMAIN)
    // MUTATION: let the key shrink, or cut the whole value at its end again.
    expect(won(key!, ['flex', 'flex-shrink'], WIDE)).toBe('none')
    for (const part of [head!, tail!]) {
      expect(won(part, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(won(part, ['overflow', 'overflow-x'], WIDE)).toBe('hidden')
      expect(won(part, 'min-width', WIDE)).toBe('0')
    }
    expect(won(ident, 'display', WIDE)).toBe('flex')
    // The phone table scrolls, so the identity shows whole there (CH-13).
    expect(won(head!, 'text-overflow', PHONE)).toBeNull()
    expect(won(tail!, 'text-overflow', PHONE)).toBeNull()
  })

  it('keeps a principal’s local part whole and cuts only its domain', async () => {
    serve(capacity([]))
    const c = await roster()
    const ident = row(c, 'u-bogdan').querySelector('td[data-label="Principal"] .ten-ident')!
    expect(ident.textContent).toBe('bogdan@saga.xyz')
    expect(ident.querySelector('.ten-ident-head')).toBeNull()
    expect(ident.querySelector('.ten-ident-key')?.textContent).toBe('bogdan@')
    expect(ident.querySelector('.ten-ident-tail')?.textContent).toBe('saga.xyz')
  })
})

describe('G5-13: Enforced is the tenant pool’s effective limit, as /v1/capacity serves it', () => {
  it('prints the served figure, not the smaller of the two configured values', async () => {
    // The served figures differ from min(max_active, units) on purpose: the
    // screen must print what the pool says, whatever the registry says.
    serve(capacity([pool('tenant:eng', 30, 40), pool('tenant:u-bogdan', 8), pool('global', 100)]))
    const c = await roster()
    const enforced = (id: string) => row(c, id).querySelector('td[data-label="Enforced"]')!
    // MUTATION: put `Math.min(t.max_active, t.capacity_units)` back: eng reads 40.
    await waitFor(() => expect(enforced('eng').textContent).toBe('30'), WAIT)
    expect(enforced('u-bogdan').textContent).toBe('8')
    expect(api.loadCapacity).toHaveBeenCalledTimes(1)
    // The registry values are the explanation, on the figure.
    const link = enforced('eng').querySelector('a')!
    expect(link.getAttribute('title')).toBe('effective limit of tenant:eng · configured 45 · 40u')
  })

  it('draws a dash with its reason when the tenant has no pool, or its pool no limit', async () => {
    serve(capacity([pool('tenant:eng', null, null)]))
    const c = await roster()
    await waitFor(
      () => expect(row(c, 'eng').querySelector('td[data-label="Enforced"] .ctl-em')?.getAttribute('title')).toBe('tenant:eng has no limit set'),
      WAIT,
    )
    const unset = row(c, 'eng').querySelector('td[data-label="Enforced"] .ctl-em')!
    expect(unset.textContent).toBe('—')
    const absent = row(c, 'u-bogdan').querySelector('td[data-label="Enforced"] .ctl-em')!
    expect(absent.textContent).toBe('—')
    expect(absent.getAttribute('title')).toBe('no tenant:u-bogdan pool in /v1/capacity')
  })

  it('keeps the roster when /v1/capacity fails, and says the figure was not read', async () => {
    serve({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: 'unavailable', message: 'down' } })
    const c = await roster()
    await waitFor(
      () => expect(row(c, 'eng').querySelector('td[data-label="Enforced"] .ctl-em')?.getAttribute('title')).toBe('/v1/capacity was not read'),
      WAIT,
    )
    expect(row(c, 'eng').querySelector('td[data-label="Enforced"]')!.textContent).toBe('—')
    // Configured is the registry's, and still there.
    expect(row(c, 'eng').querySelector('td[data-label="Configured"]')!.textContent).toBe('45 · 40u')
  })

  it('draws the roster without waiting for /v1/capacity, each Enforced a dash until it lands', async () => {
    // A capacity read that never answers must not hold the page back: the
    // roster is the screen's read, and its age is the page's.
    api.loadTenants.mockResolvedValue({ status: 'ok', data: { tenants: ROSTER }, fetchedAt: Date.now() })
    api.loadCapacity.mockReturnValue(new Promise(() => {}))
    const c = await roster()
    const cell = row(c, 'eng').querySelector('td[data-label="Enforced"] .ctl-em')!
    expect(cell.textContent).toBe('—')
    expect(cell.getAttribute('title')).toBe('reading /v1/capacity')
  })
})
