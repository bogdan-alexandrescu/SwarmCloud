// VISUAL QA RUN 4 (wf_ecd935541ced4a539540, 2026-10-22, the fixture build of
// main at fabe4d6c), LANE QAFIX4-CAPACITY (part of #1680).
//
//   N9 (capacity-010)  Holders' Accounting drift table lost its column heads
//                      on scroll: at 1440 its second screenful was 19 rows of
//                      three unlabelled number columns, while Pools keeps its
//                      heads pinned (V091). The drift table now follows V091
//                      at 900px and up, and the page scroller's padding
//                      counts its head, so a row scrolled into view clears it.
//
// Each case renders the real screen inside the shell's scroller and asks the
// cascade, through every sheet in the bundle's order (`painted()`, marks.ts),
// what it gives the rendered drift table -- not the markup's shape.

import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow, Pool } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []

afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-22T10:00:00Z' }
}

function lease(n: number): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global', 'resource:standard'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-22T09:00:00Z',
    dispatch_deadline: '2026-10-22T09:05:00Z',
    expires_at: '2026-10-22T11:00:00Z',
    heartbeat_at: '2026-10-22T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
  }
}

function pool(name: string, active: number): Pool {
  return {
    name,
    hard_limit: 40,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 40,
    active,
    available: 40 - active,
    enabled: true,
    updated_at: '2026-10-22T10:00:00Z',
  }
}

function board(): HoldersBoard {
  return {
    page: {
      leases: [lease(1), lease(2)],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-22T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 2,
      active_beyond_window: 0,
      truncated: false,
    } as unknown as LeasePage,
    pools: [pool('global', 2), pool('resource:standard', 3)],
    poolsDetail: null,
  }
}

/** Holders rendered where the shell draws it: in `.ctl-scroll`, under `.ctl-frame`. */
async function renderDrift(): Promise<{ head: HTMLElement; wrap: HTMLElement; scroller: HTMLElement }> {
  api.loadHolders.mockResolvedValue(ok(board()))
  const frame = document.createElement('div')
  frame.className = 'ctl-frame'
  const scroller = document.createElement('div')
  scroller.className = 'ctl-scroll'
  const app = document.createElement('div')
  app.className = 'app'
  frame.appendChild(scroller).appendChild(app)
  document.body.appendChild(frame)
  hosts.push(frame)
  render(<HoldersScreen />, { container: app })
  await waitFor(
    () => expect(app.querySelector('section.hold-drift .ctl-table > table > thead')).not.toBeNull(),
    WAIT,
  )
  const head = app.querySelector<HTMLElement>('section.hold-drift .ctl-table > table > thead')!
  return { head, wrap: head.closest<HTMLElement>('.ctl-table')!, scroller }
}

describe('N9 (run-4 capacity-010): the Accounting drift table keeps its column heads on scroll, as Pools does', () => {
  it('pins the head row group to the page scroller at 1440, and its wrapper does not scroll', async () => {
    const { head, wrap } = await renderDrift()
    expect([...head.querySelectorAll('th')].map((th) => th.textContent)).toEqual(['Pool', 'From leases', 'Counter', 'Delta'])
    // MUTATION: drop `position: sticky` from the `.hold-drift` thead rule
    // (styles/capacity.css, V091) -- the heads scroll away with the page.
    expect(painted(head, 'position', WIDE)).toBe('sticky')
    // MUTATION: drop `top: 0` from the same rule -- a sticky box with no
    // inset never sticks.
    expect(painted(head, 'top', WIDE)).toBe('0')
    // MUTATION: drop `z-index: 1` -- the rows' marks paint over the stuck head.
    expect(painted(head, 'z-index', WIDE)).toBe('1')
    // MUTATION: drop `.hold-drift .ctl-table.is-stacked { overflow: clip }` --
    // styles.css's `overflow-x: auto` makes the wrapper the head's scroll
    // container on both axes (WF-21), and it never scrolls vertically.
    expect(painted(wrap, ['overflow', 'overflow-x'], WIDE)).toBe('clip')
    // MUTATION: drop `height: var(--row-h)` from the head row -- the padding
    // below no longer names the height the head is drawn at.
    expect(painted(head.querySelector('tr')!, 'height', WIDE)).toBe('var(--row-h)')
  })

  it('pads the page scroller by the bar and the drift head, so a focused or anchored row clears it', async () => {
    const { head, scroller } = await renderDrift()
    const h = painted(head.querySelector('tr')!, 'height', WIDE) ?? 'no height on the head row'
    // MUTATION: drop `.hold-drift` from the `.ctl-scroll:has(...)` padding
    // rule -- the scroller keeps the bar-only padding and a row Tabbed to
    // stops under the stuck head, as capacity-007 captured on Pools.
    expect(painted(scroller, 'scroll-padding-top', WIDE)).toBe(`calc(0px + ${h})`)
  })

  it('sticks nothing at 390, where the stacked records hide the head', async () => {
    const { head, wrap, scroller } = await renderDrift()
    // MUTATION: lift the `.hold-drift` rules out of `@media (min-width: 900px)`
    // -- a phone record would carry a sticky, clipped head it does not draw,
    // and its rows would stop a head's height short.
    expect(painted(head, 'position', PHONE)).not.toBe('sticky')
    expect(painted(wrap, ['overflow', 'overflow-x'], PHONE)).not.toBe('clip')
    expect(painted(scroller, 'scroll-padding-top', PHONE)).toBe('0px')
  })
})
