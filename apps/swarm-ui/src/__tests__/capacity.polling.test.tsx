// Capacity polling, decided 2026-10-01 (#117, capacity.html §G): Pools and
// Holders re-read every 30s, Accounts every 60s, and none of them reads while
// the tab is hidden.
//
// Holders is the screen driven here because its read is one call with no
// side reads; Pools and Accounts pass the same `pollMs` through the same
// `Screen`, and the cadences themselves are pinned below.
//
// BREAK IT: drop `pollMs` from HoldersScreen -- the read happens once. Or
// make `Screen` ignore `document.hidden` -- the hidden tab keeps reading.

import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { LeasePage } from '../types'
import { ACCOUNTS_POLL_MS, HOLDERS_POLL_MS, POOLS_POLL_MS } from '../capacityPoll'

const loadHolders = vi.hoisted(() => vi.fn())
vi.mock('../api', () => ({ loadHolders }))

const { HoldersScreen } = await import('../Holders')

const BOARD: HoldersBoard = {
  page: {
    leases: [],
    units_held: 0,
    tenant_id: null,
    active_only: true,
    active_beyond_window: 0,
    truncated: false,
    examined: 0,
  } as unknown as LeasePage,
  pools: [
    {
      name: 'global', hard_limit: 8, adaptive_target: null, quota_derived_limit: null,
      effective_limit: 8, active: 2, available: 6, enabled: true, updated_at: '2026-10-01T10:00:00Z',
    },
  ],
  poolsDetail: null,
}

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => (hidden ? 'hidden' : 'visible'),
  })
  document.dispatchEvent(new Event('visibilitychange'))
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

afterEach(() => {
  setHidden(false)
  vi.useRealTimers()
})

describe('the capacity screens poll at the decided cadences', () => {
  it('pins Pools and Holders at 30s and Accounts at 60s', () => {
    expect(POOLS_POLL_MS).toBe(30_000)
    expect(HOLDERS_POLL_MS).toBe(30_000)
    expect(ACCOUNTS_POLL_MS).toBe(60_000)
  })

  it('re-reads Holders every 30s, reads nothing while the tab is hidden, and reads at once on return', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    loadHolders.mockImplementation(async () => ({ status: 'ok', data: BOARD, fetchedAt: Date.now() }))
    render(<HoldersScreen />)

    await advance(0)
    expect(loadHolders).toHaveBeenCalledTimes(1)

    // One cadence on: a second read.
    await advance(HOLDERS_POLL_MS)
    expect(loadHolders, 'Holders did not re-read after 30s').toHaveBeenCalledTimes(2)

    // Hidden: four cadences pass and nothing is read.
    setHidden(true)
    await advance(HOLDERS_POLL_MS * 4)
    expect(loadHolders, 'a hidden tab kept polling').toHaveBeenCalledTimes(2)

    // Back: the read that fell due while away happens at once, not 30s later.
    setHidden(false)
    await advance(0)
    expect(loadHolders, 'the tab came back and did not read').toHaveBeenCalledTimes(3)

    // And the cadence resumes.
    await advance(HOLDERS_POLL_MS)
    expect(loadHolders).toHaveBeenCalledTimes(4)
  })
})
