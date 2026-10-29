// QA2B-G (visual QA pass 2b, part of #2694): ovag2-04's second half, landed
// from QA2-FIX-17's patch. Waiting, and why named a READY group's pool by its
// raw key, 'waiting for: provider:anthropic:tenant:u-bogdan  5/5 units', which
// broke mid-word at 390. It now writes the pool as Headroom does, and keeps
// the key as the title's hover.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { WaitingWhy, waitGroups } from '../OverviewRegions'
import type { Task, TaskPage, WaitingFor } from '../types'
import { task } from './runfixture'

const KEY = 'provider:anthropic:tenant:u-bogdan'

function waitingOn(pool: string): WaitingFor {
  const lead = { pool, state: 'full' as const, active: 5, limit: 5, units: 1, reason: 'POOL_FULL' }
  return { as_of: '2026-10-29T10:00:00Z', admissible_now: false, holds_capacity: false, lead, pools: [lead], complete: true }
}

const ready = (id: string, pool: string): Task => task({ id, state: 'READY', waiting_for: waitingOn(pool) })

afterEach(cleanup)

describe('ovag2-04: a READY group names its pool in words, its key on hover', () => {
  it('writes the lead pool by its label, the raw key as `pool`', () => {
    const [g] = waitGroups([ready('task_r1', KEY), ready('task_r2', KEY)])
    // MUTATION: `title = line` again in waitGroups -> the raw key: red.
    expect(g.title).toBe('waiting for: anthropic · u-bogdan  5/5 units')
    expect(g.title).not.toContain('provider:')
    expect(g.pool).toBe(KEY)
    expect(g.n).toBe(2)
  })

  it('draws the label, with the key as the title of the line', () => {
    const page: TaskPage = { tasks: [ready('task_r1', KEY)], next_page_token: null, tenant_id: 'u-bogdan' } as TaskPage
    const { container } = render(<WaitingWhy tasks={{ status: 'ok', data: page, fetchedAt: Date.now() }} />)
    const b = container.querySelector('.ov-wr .ov-wt b')!
    expect(b.textContent).toBe('waiting for: anthropic · u-bogdan  5/5 units')
    // MUTATION: drop `title={g.pool}` from WaitingWhy's <b>: red.
    expect(b.getAttribute('title')).toBe(KEY)
  })

  it('leaves a group with no pool in its line untitled', () => {
    const [g] = waitGroups([task({ id: 'task_q1', state: 'QUEUED' })])
    expect(g.pool).toBeUndefined()
    const [r] = waitGroups([task({ id: 'task_r3', state: 'READY' })])
    expect(r.title).toBe('Eligible, waiting for room')
    expect(r.pool).toBeUndefined()
  })
})
