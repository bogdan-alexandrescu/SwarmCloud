// PLATFORM COUNTS KEEPS ITS LAST RUN FOR THE SESSION (#135).
//
// A run bills one count() per state per scope, so throwing its answer away
// when the reader navigates off the screen and back made them pay again to
// see what they had already seen. The last run, with its age, is kept for
// the rest of the session (this tab's lifetime: a module-level record, not
// storage), and the not-run cards are not drawn over it.
//
// MUTATION: start `run` at null again and the first case goes red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Me, Stats } from '../types'

const loadStats = vi.hoisted(() => vi.fn<() => Promise<Result<Stats>>>())
const loadMe = vi.hoisted(() => vi.fn<() => Promise<Result<Me>>>())
vi.mock('../api', () => ({ loadStats, loadMe }))

const COUNTS: Record<string, number> = {
  READY: 2, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 1,
  RUNNING: 3, SUCCEEDED: 40, FAILED: 5, CANCELLED: 2,
}

function stats(generatedAt: string): Result<Stats> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'eng',
      tasks_by_state: { ...COUNTS },
      platform_tasks_by_state: { ...COUNTS },
      dispatch_paused: false,
      limits: {},
      generated_at: generatedAt,
    } as Stats,
  }
}

async function fresh() {
  // A fresh module per case: the session record lives in the module.
  vi.resetModules()
  return (await import('../PlatformCounts')).PlatformCountsScreen
}

beforeEach(() => {
  loadStats.mockReset()
  loadMe.mockReset()
  loadMe.mockResolvedValue({ status: 'error', fetchedAt: Date.now(), error: { kind: 'network', message: 'x' } } as unknown as Result<Me>)
})
afterEach(cleanup)

describe('Platform counts keeps the last run for the session (#135)', () => {
  it('draws the last run, with its age, after navigating away and back', async () => {
    const Screen = await fresh()
    const at = new Date(Date.now() - 5 * 60_000).toISOString()
    loadStats.mockResolvedValue(stats(at))
    const first = render(<Screen />)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0))
    first.unmount()

    render(<Screen />)
    // No new run was billed to draw it.
    expect(loadStats).toHaveBeenCalledTimes(1)
    expect(document.querySelector('.counts-notrun')).toBeNull()
    expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0)
    // Its age, and the press that refreshes it.
    expect(document.body.textContent).toMatch(/1 run · read 5m ago/)
    expect(screen.getByRole('button', { name: /^Run it again · / })).toBeTruthy()
  })

  it('starts at not run in a session that has not run a count', async () => {
    const Screen = await fresh()
    render(<Screen />)
    expect(document.querySelectorAll('.counts-notrun')).toHaveLength(2)
    expect(screen.getByRole('button', { name: /^Run the count · / })).toBeTruthy()
  })

  it('keeps the last good run when a later one fails, and says the later one failed', async () => {
    const Screen = await fresh()
    loadStats.mockResolvedValueOnce(stats(new Date().toISOString()))
    const first = render(<Screen />)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0))
    loadStats.mockResolvedValueOnce({
      status: 'error',
      fetchedAt: Date.now(),
      error: { kind: 'network', message: 'offline' },
    } as unknown as Result<Stats>)
    fireEvent.click(screen.getByRole('button', { name: /^Run it again · / }))
    await waitFor(() => expect(document.body.textContent).toMatch(/last run failed/))
    first.unmount()

    // Back on the screen: the failure was this visit's, the counts are the session's.
    render(<Screen />)
    expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0)
    expect(document.body.textContent).toMatch(/1 run · read/)
  })
})
