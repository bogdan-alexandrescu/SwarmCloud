// What prose81.states.test.tsx mounts its states with: the fixtures, the
// mount list and the one check (`holds`) each state is put through.

import type { ReactElement } from 'react'
import { render, type RenderResult } from '@testing-library/react'
import { expect, vi } from 'vitest'

import type { PoolAccount } from '../accountsPool'
import type { Result } from '../fetch'
import { helpIn, longRuns, shownIn } from './proseruns'
import { repo, serve } from './repofixture'

/** What each mount left in the document, unmounted after each case and between a case's mounts. */
const mounted: (() => void)[] = []
export function unmountAll(): void {
  for (const unmount of mounted.splice(0)) unmount()
}
export function mount(ui: ReactElement): RenderResult {
  const view = render(ui)
  mounted.push(view.unmount)
  return view
}

export type Json = Record<string, unknown>
export type Reply = { status: number; body: unknown } | null

export const WAIT = { timeout: 5000 } as const
export const MIN = 60_000
export const HOUR = 60 * MIN
export const DAY = 24 * HOUR
export const AT = '2026-10-20T09:30:00+00:00'

/** A password- or token-shaped value, built at runtime. */
export const made = (s: string) => [s, 'demo'].join('-') + 'Qx7'.repeat(9)

/** `root` shows no long run but the `data` it only shows, shows each line, and keeps each moved sentence behind a mark. */
export function holds(root: Element, want: { lines?: string[]; moved?: string[]; data?: string[] }): void {
  const el = root as HTMLElement
  const runs = longRuns(el).filter((r) => !(want.data ?? []).some((d) => r.includes(d)))
  expect(runs, 'a long run is on the surface').toEqual([])
  for (const line of want.lines ?? []) expect(shownIn(el), `"${line}" left the surface`).toContain(line)
  for (const s of want.moved ?? []) {
    expect(shownIn(el), `"${s}" is on the surface`).not.toContain(s)
    expect(helpIn(el), `"${s}" is behind no mark`).toContain(s)
  }
}

export const REPO_ID = 'repo_' + '0'.repeat(16)
export const SHOP = 'qat_' + 'a1'.repeat(8)
export const BLOG = 'qat_' + 'b2'.repeat(8)
export const PORTAL = 'qat_' + 'c3'.repeat(8)
export const UNSET = { set: false, set_at: null, set_by: null, version: null }
export const OWN_LOGIN = { kind: 'own_login', login_path: '/login', signed_in_selector: '#me' }

export function target(id: string, name: string, over: Json = {}): Json {
  return {
    id, tenant_id: 'eng', revision: 1, name,
    base_url: `https://${name}.example.test/`, host: `${name}.example.test`, host_state: 'approved',
    approved_by: 'ops@example.com', approved_at: AT, approved_origins: [],
    repo_id: REPO_ID, repository: 'https://github.com/example-org/example-api',
    sign_in: { kind: 'none' }, credential: UNSET, challenge_bypass_value: UNSET,
    authorized_by: "the app's owner", screens: [{ id: 'home', path: '/' }], ...over,
  }
}

export function qaServe(list: Json[], opts: { role?: string; write?: (m: string, path: string) => Reply } = {}) {
  serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') {
      return {
        status: 200,
        body: {
          tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
          principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: true, is_admin_unresolved: false },
          environment: 'dev', environment_declared: true, role: { name: opts.role ?? 'admin', source: 'override' },
        },
      }
    }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: list } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    return opts.write?.(m, path) ?? null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
}

export function account(over: Partial<PoolAccount> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: 'eng:laptop', owner_tenant: 'eng', label: 'laptop', provider: 'anthropic', state: 'AVAILABLE', reason: '',
    lend_to: [], assigned: 0, observed_at: new Date(now - 5 * MIN).toISOString(), stale: false, unreadable_by: [], unreadable_now: [],
    last_assigned_at: new Date(now - 26 * MIN).toISOString(),
    windows: {
      five_hour: { utilization: 0.4, resets_at: new Date(now + 2 * HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.5, resets_at: new Date(now + 3 * DAY).toISOString(), reset: false },
    },
    max_agents: 6, can_take_new_agent: true, blocked_by: [], next_eligible_at: null, organisation: { name: 'Acme', plan: 'Team' },
    ...over,
  }
}

export const week = (used: number, resetsIn: number, reset = false) => ({
  five_hour: { utilization: 0.4, resets_at: new Date(Date.now() + 2 * HOUR).toISOString(), reset: false },
  seven_day: { utilization: used, resets_at: new Date(Date.now() + resetsIn).toISOString(), reset },
})

export const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() })
