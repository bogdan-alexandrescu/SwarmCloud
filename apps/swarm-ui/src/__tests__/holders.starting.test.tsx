// HOLDERS, QA 2026-10-07 (G5-01, G5-06, G5-14).
//
// G5-01: two leases a Cloud Run cold start kept from beating for 2m 38s were
// drawn red "presumed dead". The API said `heartbeat_ever:false`,
// `expired:true`, `dispatch_overdue:false` -- the 120 s lease TTL had run out,
// the 8-minute dispatch deadline had not -- and both were RUNNING three minutes
// later. `LeaseHeartbeat`'s own contract says a booting agent is not a silent
// one: before the first beat the reconciler judges a lease by
// `dispatch_overdue` alone.
//
// G5-06: Held for ticked on the shared age clock while the Heartbeat age was
// the server's `silent_seconds` as of the read, so one row said "Held for
// 3m 29s" beside "never beat, 2m 38s".
//
// G5-14: a pool filter no lease matched drew a table head and nothing else.
//
// BREAK IT: check `row.expired` before `heartbeat_ever` in `leaseLiveliness`
// -- the booting lease is "presumed dead" again. Draw the age from
// `silent_seconds` alone -- the beaten lease reads 20s, not 1m 20s. Drop the
// empty row -- the filtered table has no body row.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import { leaseLiveliness, type LeasePage, type LeaseRow } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const
const NOW = Date.parse('2026-10-07T05:28:20Z')
const THRESHOLDS = { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 }
const iso = (ms: number): string => new Date(ms).toISOString()

beforeEach(() => {
  // Only the clock: timers stay real, so the screen's reads still resolve.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: NOW, serverAt: iso(NOW) }
}

function lease(id: string, over: Partial<LeaseRow>): LeaseRow {
  return {
    lease_id: `lease-${id}`,
    task_id: id,
    attempt_id: `att-${id}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global', 'runner:claude-code'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: iso(NOW - 10 * 60_000),
    dispatch_deadline: iso(NOW - 2 * 60_000),
    expires_at: iso(NOW + 60_000),
    heartbeat_at: iso(NOW - 80_000),
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 20,
    heartbeat_ever: true,
    last_error: null,
    ...over,
  }
}

/** The lease QA read: created 2m 38s ago, TTL run out, deadline 5m 22s away. */
const booting = (id = 'tsk_booting', over: Partial<LeaseRow> = {}): LeaseRow =>
  lease(id, {
    created_at: iso(NOW - 158_000),
    dispatch_deadline: iso(NOW + 322_000),
    expires_at: iso(NOW - 38_000),
    heartbeat_at: null,
    heartbeat_ever: false,
    expired: true,
    dispatch_overdue: false,
    // The server's value at its read, 60 s before now.
    silent_seconds: 98,
    ...over,
  })

function board(leases: LeaseRow[]): HoldersBoard {
  return {
    page: {
      leases,
      thresholds: THRESHOLDS,
      // The read is a minute old: every age on the row must have moved since.
      evaluated_at: iso(NOW - 60_000),
      active_only: true,
      tenant_id: null,
      units_held: leases.length,
      active_beyond_window: 0,
      truncated: false,
      examined: leases.length,
    } as LeasePage,
    pools: null,
    poolsDetail: 'not read in this test',
  }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function row(id: string): Element {
  const r = [...document.querySelectorAll('.hold-all tbody tr')].find((x) => x.querySelector(`a[title="${id}"]`))
  expect(r, `no holder row for ${id}`).toBeDefined()
  return r!
}

describe('G5-01: leaseLiveliness judges a never-beaten lease by its dispatch deadline', () => {
  it('calls an expired, never-beaten lease inside its deadline starting, not presumed dead', () => {
    const v = leaseLiveliness(booting(), THRESHOLDS)
    expect(v.kind).toBe('starting')
  })

  it('calls a never-beaten lease past its dispatch deadline presumed dead', () => {
    expect(leaseLiveliness(booting('x', { dispatch_overdue: true }), THRESHOLDS).kind).toBe('presumed-dead')
    expect(leaseLiveliness(booting('x', { dispatch_overdue: true, expired: false }), THRESHOLDS).kind).toBe(
      'presumed-dead',
    )
  })

  it('never calls a never-beaten lease silent, however long it has been booting', () => {
    expect(leaseLiveliness(booting('x', { expired: false, silent_seconds: 400 }), THRESHOLDS).kind).toBe('starting')
  })

  it('still judges a lease that has beaten by its TTL and its silence', () => {
    expect(leaseLiveliness(lease('x', { expired: true }), THRESHOLDS).kind).toBe('presumed-dead')
    expect(leaseLiveliness(lease('x', { silent_seconds: 95 }), THRESHOLDS).kind).toBe('silent')
    expect(leaseLiveliness(lease('x', {}), THRESHOLDS).kind).toBe('alive')
  })

  it('draws the booting lease neutral, with its age and its deadline', async () => {
    api.loadHolders.mockResolvedValue(ok(board([booting()])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const cell = row('tsk_booting').querySelector('td[data-label="Heartbeat"]')
    expect(text(cell)).toBe('starting · no beat yet, 2m 38s · deadline in 5m 22s')
    expect(cell!.querySelector('.sk-st[data-tone="bad"]'), 'a booting worker is drawn as dead').toBeNull()
    expect(cell!.querySelector('.sk-st[data-tone="info"]')).not.toBeNull()
  })
})

describe('G5-06: Held for and the heartbeat age move on one clock', () => {
  it('ages a never-beaten lease from its creation, exactly as Held for does', async () => {
    api.loadHolders.mockResolvedValue(ok(board([booting()])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const r = row('tsk_booting')
    expect(text(r.querySelector('td[data-label="Held for"]'))).toBe('2m 38s')
    expect(text(r.querySelector('td[data-label="Heartbeat"]'))).toContain('no beat yet, 2m 38s')
  })

  it('ages a beaten lease by the time since the read, not the read alone', async () => {
    api.loadHolders.mockResolvedValue(ok(board([lease('tsk_beating', { silent_seconds: 20 })])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    // 20 s at the read, which was 60 s ago.
    expect(text(row('tsk_beating').querySelector('td[data-label="Heartbeat"]'))).toBe('beating · 1m 20s')
  })
})

describe('G5-14: a pool filter no lease matches says so', () => {
  it('draws one row naming the pool, the measured zero and the way back', async () => {
    window.history.replaceState(null, '', `/capacity/holders?pool=${encodeURIComponent('resource:browser')}`)
    api.loadHolders.mockResolvedValue(ok(board([lease('tsk_a', {}), lease('tsk_b', {})])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const body = document.querySelectorAll('.hold-all tbody tr')
    expect(body.length, 'the filtered table has no body row').toBe(1)
    const empty = body[0]!
    expect(text(empty)).toContain('No lease holds resource:browser right now')
    expect(empty.querySelector('.ctl-mark.is-zero')?.textContent).toBe('real zero')
    fireEvent.click(empty.querySelector('button')!)
    await waitFor(() => expect(document.querySelectorAll('.hold-all tbody tr a').length).toBe(2), WAIT)
  })
})
