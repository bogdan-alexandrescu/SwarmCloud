// PLATFORM COUNTS BEFORE THE FIRST RUN (#135, admin-help.html §B, decided
// 2026-10-01): both cards are drawn with "not run", so the page has its shape
// before the press, and the placeholder is gone once a run is asked for. The
// mark is the neutral `not measured` one, never a task state's (#503).

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Me, Stats } from '../types'

const loadStats = vi.hoisted(() => vi.fn<() => Promise<Result<Stats>>>())
const loadMe = vi.hoisted(() => vi.fn<() => Promise<Result<Me>>>())
vi.mock('../api', () => ({ loadStats, loadMe }))

const { PlatformCountsScreen } = await import('../PlatformCounts')

function session(isAdmin: boolean): Result<Me> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant: { tenant_id: 'eng' },
      principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: isAdmin },
      environment: 'dev',
      environment_declared: false,
    } as unknown as Me,
  }
}

const COUNTS: Record<string, number> = {
  READY: 2, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 1,
  RUNNING: 3, SUCCEEDED: 40, FAILED: 5, CANCELLED: 2,
}

beforeEach(() => {
  loadStats.mockReset()
  loadMe.mockReset()
  loadMe.mockResolvedValue(session(true))
})

describe('Platform counts before the first run (#135)', () => {
  it('draws This tenant and Every tenant, each marked not run, with no figure', () => {
    render(<PlatformCountsScreen />)
    for (const title of [/This tenant/, /Every tenant/]) {
      const card = screen.getByRole('heading', { name: title }).closest('section')!
      expect(card.classList.contains('counts-notrun')).toBe(true)
      // NOT MEASURED, the neutral mark of the six kinds of nothing (#503) --
      // never a task-state mark: the QUEUED ring here read as queued work.
      // MUTATION: draw the queued ring again, or any `[data-mark]`.
      expect(card.querySelector('[data-mark]'), `${title} wears a task-state mark`).toBeNull()
      const mark = card.querySelector('.ctl-mark.is-absent')
      expect(mark?.textContent, `${title} carries no not-measured mark`).toBe('not measured')
      expect(card.textContent ?? '').toContain('not run yet')
      // Nothing failed: not the dashed `not read` failure mark.
      expect(card.querySelector('.ctl-mark.is-unread')).toBeNull()
      // And no number pretending to be a count.
      expect(card.querySelector('.ctl-figure')).toBeNull()
      expect(card.querySelectorAll('.split-row')).toHaveLength(0)
    }
    expect(loadStats).not.toHaveBeenCalled()
  })

  it('replaces both placeholders with the counts once a run lands', async () => {
    loadStats.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        tenant_id: 'eng',
        tasks_by_state: { ...COUNTS },
        platform_tasks_by_state: { ...COUNTS },
        dispatch_paused: false,
        limits: {},
        generated_at: '2026-10-01T10:00:00Z',
      } as Stats,
    })
    render(<PlatformCountsScreen />)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0))
    expect(document.querySelector('.counts-notrun')).toBeNull()
    expect(document.body.textContent ?? '').not.toContain('not run yet')
    expect(screen.getAllByRole('heading', { name: /This tenant/ })).toHaveLength(1)
  })
})
