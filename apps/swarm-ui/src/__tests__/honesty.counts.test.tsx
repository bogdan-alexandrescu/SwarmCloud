// "No total over a partial response", as behaviour.
//
// A sum is the most authoritative-looking thing on a screen, and the easiest
// to compute over data that is missing pieces. `/v1/stats` runs one Firestore
// count() per state; a response missing three of the nine is not a platform
// with fewer tasks, it is a read that did not finish -- and the difference is
// invisible once you have added the numbers up.
//
// Both directions are asserted. A screen that never totals anything satisfies
// "no total over a partial response" trivially, so the complete case must show
// the total or the rule is being kept by accident.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Stats } from '../types'
import { NEVER_WRITTEN, REAL_STATES } from '../types'
import { expectNoFigures } from './setup'

const loadStats = vi.hoisted(() => vi.fn<() => Promise<Result<Stats>>>())
vi.mock('../api', () => ({ loadStats }))

const { PlatformCountsScreen } = await import('../PlatformCounts')

const COMPLETE: Record<string, number> = {
  READY: 2, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 1,
  RUNNING: 3, SUCCEEDED: 40, FAILED: 5, CANCELLED: 2,
}

function stats(over: Partial<Stats> = {}): Stats {
  return {
    tenant_id: 'eng',
    tasks_by_state: { ...COMPLETE },
    dispatch_paused: false,
    limits: {},
    generated_at: '2026-09-22T10:00:00Z',
    ...over,
  }
}

async function run(result: Result<Stats>) {
  loadStats.mockResolvedValue(result)
  render(<PlatformCountsScreen />)
  screen.getByRole('button', { name: 'Run the count' }).click()
  await waitFor(() => expect(loadStats).toHaveBeenCalled())
  return result
}

/** The tenant block. There are two blocks when the caller is an admin. */
function tenantPanel(): HTMLElement {
  const heading = screen.getByRole('heading', { name: /This tenant/ })
  const section = heading.closest('section')
  if (!section) throw new Error('no tenant section')
  return section as HTMLElement
}

describe('a complete response', () => {
  it('shows the total, so the rule below is not being kept by accident', async () => {
    await run({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    const sum = Object.values(COMPLETE).reduce((a, b) => a + b, 0)
    expect(panel.querySelector('.provenance')?.textContent).toContain(String(sum))
    expect(panel.textContent).not.toContain('did not come back')
  })

  it('renders a measured zero as 0 and not as an em dash', async () => {
    await run({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    const leased = Array.from(panel.querySelectorAll('.split-row')).find((r) => r.textContent?.startsWith('LEASED'))
    expect(leased?.querySelector('.sr-n')?.textContent).toBe('0')
  })
})

describe('a partial response', () => {
  it('withholds the total and names every state that did not come back', async () => {
    const partial = { ...COMPLETE }
    delete partial.RUNNING
    delete partial.PARKED
    await run({ status: 'ok', data: stats({ tasks_by_state: partial }), fetchedAt: Date.now() })

    const panel = await waitFor(tenantPanel)
    const warning = panel.querySelector('.warn-text')?.textContent ?? ''
    expect(warning).toContain('did not come back')
    expect(warning).toContain('PARKED')
    expect(warning).toContain('RUNNING')
    expect(warning).toContain('No total is shown')

    // THE RULE. Not "it warns" -- it must also NOT print the sum.
    expect(panel.querySelector('.provenance')).toBeNull()
    expect(panel.textContent).not.toContain('task documents across')
  })

  it('renders the missing states as em dashes rather than as zeros', async () => {
    const partial = { ...COMPLETE }
    delete partial.RUNNING
    await run({ status: 'ok', data: stats({ tasks_by_state: partial }), fetchedAt: Date.now() })

    const panel = await waitFor(tenantPanel)
    const running = Array.from(panel.querySelectorAll('.split-row')).find((r) => r.textContent?.startsWith('RUNNING'))
    // "0 RUNNING" and "the RUNNING count did not arrive" are opposite facts.
    expect(running?.querySelector('.sr-n')?.textContent).toBe('—')
  })

  it('a response with NO counts at all is called a failed query, not an idle platform', async () => {
    await run({ status: 'ok', data: stats({ tasks_by_state: {} }), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    expect(panel.querySelector('.provenance')).toBeNull()
    expect(panel.querySelector('.warn-text')?.textContent).toContain('did not come back')
    for (const state of REAL_STATES) {
      const row = Array.from(panel.querySelectorAll('.split-row')).find((r) => r.textContent?.startsWith(state))
      expect(row?.querySelector('.sr-n')?.textContent).toBe('—')
    }
  })
})

describe('scope', () => {
  it('keeps the tenant figures and the platform figures in separate blocks', async () => {
    await run({
      status: 'ok',
      data: stats({ platform_tasks_by_state: { ...COMPLETE, RUNNING: 99 } }),
      fetchedAt: Date.now(),
    })
    await waitFor(tenantPanel)
    // An admin reading their own three running tasks as the platform total is
    // a truth bug, not a layout preference.
    expect(screen.getByRole('heading', { name: /Every tenant/ })).toBeTruthy()
    expect(tenantPanel().textContent).not.toContain('99')
  })

  it('an ABSENT platform block is stated as absent, never as zero', async () => {
    await run({ status: 'ok', data: stats({ platform_tasks_by_state: undefined }), fetchedAt: Date.now() })
    await waitFor(tenantPanel)
    const everyone = screen.getByRole('heading', { name: /Every tenant/ }).closest('section')
    expect(everyone?.textContent).toContain('absent from this response rather than zero')
    expect(everyone?.querySelectorAll('.split-row').length).toBe(0)
  })
})

describe('a failed read of the counts', () => {
  it('prints no number anywhere, because a zero here is the most reassuring lie available', async () => {
    await run({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'Firestore did not answer.' },
    })
    await screen.findByText('Firestore did not answer.', { exact: false })

    expect(screen.queryByRole('heading', { name: /This tenant/ })).toBeNull()
    expect(document.querySelectorAll('.split-row').length).toBe(0)
    expect(document.body.textContent).toContain('This says nothing about how much work the platform is carrying.')

    // The copy block explaining the aggregation cost names counts of STATES,
    // which are a property of the contract rather than a measurement of the
    // platform, so they are allowed through by name.
    expectNoFigures(document.body, ['1000', 'twelve', 'twenty-four'].concat(
      [String(NEVER_WRITTEN.size), String(REAL_STATES.length + NEVER_WRITTEN.size)],
    ))
  })

  it('does not count a failure as a successful aggregation run', async () => {
    // Incrementing on every settled promise would let a failure inflate a
    // number the copy then calls "aggregations billed".
    await run({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } })
    await screen.findByText('boom', { exact: false })
    expect(document.body.textContent).not.toContain('successful run')
  })
})

describe('the states that can never be written', () => {
  it('are excluded from the histogram and explained rather than drawn as zeros', async () => {
    await run({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    const rows = Array.from(panel.querySelectorAll('.split-row')).map((r) => r.textContent ?? '')
    for (const state of NEVER_WRITTEN) {
      // A bucket that can only ever read zero teaches "nothing is wrong"
      // rather than "this cannot happen".
      expect(rows.some((r) => r.startsWith(state))).toBe(false)
    }
    expect(panel.textContent).toContain('never written to a task document')
    expect(rows).toHaveLength(REAL_STATES.length)
  })
})
