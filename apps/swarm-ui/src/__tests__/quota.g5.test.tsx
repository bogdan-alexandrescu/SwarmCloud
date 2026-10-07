// PROVIDER QUOTA, QA 2026-10-07 (G5-02, G5-03).
//
// G5-02: anthropic . eng read `Quota cap 50` beside the pool it feeds, which
// Pools showed at 40 -- and 40 is what admission enforces. The row now draws
// that pool's ceiling, served by `/v1/admin/quota` in the same response as
// `feeds_pool`, so it cannot be older or newer than the cap beside it.
//
// G5-03: u-bogdan read `429s (this run) 1` beside `Last 429 —`, on a document
// 17 days old. A count with no time is a time nobody recorded, not an absence
// of 429s; and a count on a document older than twice the sweep is a count
// at the last report, not "this run".
//
// BREAK IT: drop the ceiling from the Feeds pool cell -- `ceiling 40` is
// gone. Draw `last_429_at === null` as the plain dash again -- `time not
// recorded` is gone. Drop the stale qualifier -- `at last report` is gone.

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Pool, QuotaState } from '../types'

const api = vi.hoisted(() => ({ loadAdminQuota: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { QuotaDetailScreen } = await import('../QuotaDetail')

const WAIT = { timeout: 5000 } as const
const minutesAgo = (m: number): string => new Date(Date.now() - m * 60_000).toISOString()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function pool(over: Partial<Pool> = {}): Pool {
  return {
    name: 'provider:anthropic:tenant:eng', hard_limit: 40, adaptive_target: null, quota_derived_limit: 50,
    effective_limit: 40, active: 0, available: 40, enabled: true, updated_at: minutesAgo(2), ...over,
  }
}

function quota(over: Partial<QuotaState>): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: minutesAgo(1),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
    retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, ...over,
  }
}

async function renderRow(q: QuotaState): Promise<HTMLElement> {
  api.loadAdminQuota.mockResolvedValue(ok({ quota: [q] }))
  render(<QuotaDetailScreen />)
  const [th] = await screen.findAllByRole('rowheader', { name: q.tenant_id }, WAIT)
  return th!.closest('tr') as HTMLElement
}

const cell = (row: HTMLElement, label: string): string =>
  (row.querySelector(`td[data-label="${label}"]`)?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('G5-02: the Feeds pool cell draws the ceiling admission enforces', () => {
  it('names the pool\'s ceiling, what sets it, and that it is below the cap', async () => {
    const row = await renderRow(quota({ feeds_pool: pool() }))
    expect(cell(row, 'Quota cap (units)')).toBe('50')
    expect(cell(row, 'Feeds pool')).toBe('provider:anthropic:tenant:eng · ceiling 40 (configured, below cap)')
  })

  it('says when the pool is at the cap', async () => {
    const row = await renderRow(quota({ feeds_pool: pool({ hard_limit: 60, effective_limit: 50, available: 50 }) }))
    expect(cell(row, 'Feeds pool')).toContain('ceiling 50 (provider quota, at cap)')
  })

  it('says a pool with no document is not there, rather than drawing a ceiling', async () => {
    const row = await renderRow(quota({ feeds_pool: null }))
    expect(cell(row, 'Feeds pool')).toBe('provider:anthropic:tenant:eng · no pool document')
  })

  it('draws the link alone beside an API that does not serve the pool', async () => {
    const row = await renderRow(quota({}))
    expect(cell(row, 'Feeds pool')).toBe('provider:anthropic:tenant:eng')
  })
})

describe('G5-03: a 429 count with no time, and a count from an old document', () => {
  it('marks a counted 429 with no time as not recorded, not a bare dash', async () => {
    const row = await renderRow(quota({ tenant_id: 'u-bogdan', rate_limit_count: 1, last_429_at: null }))
    const last = row.querySelector('td[data-label="Last 429"]')!
    expect(last.querySelector('.ctl-mark.is-unread')?.textContent).toBe('time not recorded')
    expect(last.querySelector('.ctl-em'), 'a counted 429 is drawn as though none was ever recorded').toBeNull()
  })

  it('keeps the dash when no 429 was counted and none was timed', async () => {
    const row = await renderRow(quota({ rate_limit_count: 0, last_429_at: null }))
    expect(row.querySelector('td[data-label="Last 429"] .ctl-em')).not.toBeNull()
    expect(row.querySelector('td[data-label="Last 429"] .is-unread')).toBeNull()
  })

  it('labels the count of a document older than twice the sweep at last report', async () => {
    const row = await renderRow(
      quota({ tenant_id: 'u-bogdan', updated_at: minutesAgo(17 * 24 * 60), rate_limit_count: 1, last_429_at: null }),
    )
    expect(cell(row, '429s (this run)')).toBe('1 (at last report)')
  })

  it('leaves the count of a fresh document unqualified', async () => {
    const row = await renderRow(quota({ rate_limit_count: 3, last_429_at: minutesAgo(2) }))
    expect(cell(row, '429s (this run)')).toBe('3')
  })
})
