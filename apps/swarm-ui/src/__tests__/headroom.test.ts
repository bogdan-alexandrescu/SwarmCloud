/**
 * The subscription-headroom tile's arithmetic.
 *
 * Written because a verifier mutated `usable` to `accounts.length` and the
 * whole suite stayed green: the function was not exported, so nothing could
 * reach it. A derivation a test cannot import is a derivation nothing guards.
 */
import { describe, expect, it } from 'vitest'

import { accountHeadroom } from '../Overview'

const win = (utilization: number) => ({
  utilization,
  resets_at: '2026-09-22T18:00:00Z',
  reset: false,
})

const account = (label: string, opts: { windows?: unknown; state?: string } = {}) => ({
  account_id: `acc_${label}`,
  owner_tenant: 'eng',
  label,
  provider: 'anthropic',
  state: opts.state ?? 'AVAILABLE',
  reason: '',
  lend_to: [],
  assigned: 0,
  windows: opts.windows ?? { five_hour: win(0.1), seven_day: win(0.05) },
  observed_at: '2026-09-22T12:00:00Z',
  stale: false,
  unreadable_by: [],
  last_assigned_at: null,
})

const page = (accounts: unknown[]) =>
  ({ status: 'ok', data: { accounts, tenant_id: 'eng' } }) as never

describe('accountHeadroom counts only accounts it can actually serve from', () => {
  it('does not count an account that has never been polled', () => {
    // THE MUTATION THAT WENT UNDETECTED: `usable = accounts.length` makes this 3.
    const r = accountHeadroom(
      page([
        account('team'),
        account('devops-main', { windows: {} }),
        account('personal', { windows: {} }),
      ]),
    )
    expect(r.sub).toContain('best of 1 usable account')
    expect(r.sub).not.toContain('3 usable')
  })

  it('does not count an account that is not AVAILABLE', () => {
    const r = accountHeadroom(page([account('team'), account('paused', { state: 'DISABLED' })]))
    expect(r.sub).toContain('best of 1 usable account')
  })

  it('counts every account it can serve from', () => {
    const r = accountHeadroom(page([account('a'), account('b')]))
    expect(r.sub).toContain('best of 2 usable accounts')
  })
})
