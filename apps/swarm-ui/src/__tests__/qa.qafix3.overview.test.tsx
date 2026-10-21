/**
 * VISUAL QA RUN 3 (wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of
 * main at 51d99c53): the Overview findings, lane QAFIX3-OVERVIEW (#1680).
 *
 *   overview-automate-003  At 390 the lifecycle tiles were bare figures: the
 *          captions ("costs nothing"), the per-state breakdown and the stale
 *          foot were hidden. The band is one tile per line on a phone now,
 *          each with all of it.
 *   overview-automate-005  A disabled Headroom tile clamped its reason to two
 *          lines at 1440, cutting "Use claude-code.". It is whole at every
 *          width, as the phone already drew it (OV-13).
 *   overview-automate-006  A workflow id was cut to "wf_audit_…" on a phone:
 *          the short form dropped two characters of "wf_audit_01" and left
 *          every step of that workflow reading alike. The id is whole at every
 *          width and breaks where it must.
 *   ffp-002  The phone's Running rows cut their titles on one line, so rows
 *          of one workflow were cut at the same point. They wrap.
 *   ff-001  A Recent failures row said its state only by a glyph. The word
 *          is drawn under the name.
 *   a11y-keyboard-003  The Overview's tables had no accessible name.
 *
 * Each assertion carries the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Stats, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const Overview = await import('../Overview')
const { LifecycleBand, RecentFailures } = await import('../OverviewRegions')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}
const empty = { status: 'empty', fetchedAt: Date.now() } as const
const page = (tasks: ReturnType<typeof task>[]): Result<TaskPage> => ok({ tasks, next_page_token: null, tenant_id: 'eng' } as TaskPage)

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 2, READY: 1, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 10, SUCCEEDED: 0, FAILED: 0, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

async function mount(tasks: Result<TaskPage>): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(empty)
  api.loadTasks.mockResolvedValue(tasks)
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(empty)
  api.loadSpend.mockResolvedValue(empty)
  api.loadTask.mockResolvedValue(empty)
  const { container } = render(<Overview.OverviewScreen />)
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

afterEach(cleanup)

/** Every element from `el` down, `el` included. */
const selfAndBelow = (el: Element): Element[] => [el, ...el.querySelectorAll('*')]

describe('overview-automate-003: the lifecycle tiles keep their captions and breakdowns at 390', () => {
  it('draws the caption, the per-state breakdown and the foot on a phone', () => {
    const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={page([])} />)
    const tiles = [...container.querySelectorAll('#ov-band .ov-lc')]
    expect(tiles.length).toBe(3)
    const waiting = tiles[0]!
    expect(text(waiting.querySelector('.ov-lc-h small'))).toBe('costs nothing')
    for (const t of tiles) {
      // MUTATION: hide `.ov-lc-h small` again in overview.css's 560px block.
      expect(painted(t.querySelector('.ov-lc-h small')!, 'display', PHONE)).not.toBe('none')
      // MUTATION: hide `.ov-lc-ps` again in overview.css's 560px block.
      expect(painted(t.querySelector('.ov-lc-ps')!, 'display', PHONE)).not.toBe('none')
    }
    // The page foot was already kept (G1-02); the stats foot is now too.
    const foot = document.createElement('span')
    foot.className = 'ov-lc-foot'
    waiting.appendChild(foot)
    // MUTATION: hide `.ov-lc-foot` again in overview.css's 560px block.
    expect(painted(foot, 'display', PHONE)).not.toBe('none')
    // MUTATION: keep the phone band at three tiles across.
    expect(painted(container.querySelector('#ov-band')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    // The desktop band is unchanged: three across.
    // MUTATION: give the desktop band the phone's one column.
    expect(painted(container.querySelector('#ov-band')!, 'grid-template-columns', WIDE)).toBe('repeat(3, minmax(0, 1fr))')
  })
})

describe('overview-automate-005: a disabled Headroom tile draws its whole reason at 1440', () => {
  const REASON = 'codex is disabled on this platform. The provider refused the registered credential and the platform is focused on Claude. Use claude-code.'
  const profile = {
    resource_class: 'standard',
    backend: 'cloud_run',
    provider: 'openai',
    units: 1,
    pools: ['global'],
    available: false,
    disabled_reason: REASON,
  } as unknown as Capacity['runner_profiles'][string]

  it('does not clamp the reason at any width', () => {
    const { container } = render(
      <div className="ov-headroom">
        <div className="ov-hps">
          <Overview.ProfileTile name="codex" profile={profile} byName={new Map()} tenant="eng" />
        </div>
      </div>,
    )
    const small = container.querySelector('.ov-hp.is-off small')!
    // MUTATION: drop the reason from the tile.
    expect(text(small)).toContain('Use claude-code.')
    for (const env of [WIDE, PHONE]) {
      // MUTATION: put the two-line clamp back on `.ov-hp.is-off small` at desktop width.
      expect(painted(small, '-webkit-line-clamp', env) ?? 'none').toBe('none')
      // MUTATION: put `overflow: hidden` back on the disabled reason.
      expect(painted(small, 'overflow', env) ?? 'visible').toBe('visible')
      // MUTATION: put `white-space: nowrap` back on the disabled reason.
      expect(painted(small, 'white-space', env)).toBe('normal')
    }
  })
})

describe('overview-automate-006: a workflow id is never cut on a phone', () => {
  const failed = (id: string, wf: string) =>
    task({ id, state: 'FAILED', completed_at: ago(3), updated_at: ago(3), step_id: 'scan-scripts', workflow_id: wf, metadata: null })

  it('draws "wf_audit_01" whole in a Recent failures row at 390, uncut by the sheet', () => {
    const { container } = render(<RecentFailures tasks={page([failed('tsk_a', 'wf_audit_01')])} />)
    const name = container.querySelector('.ov-fails .ov-name')!
    // MUTATION: draw `shortWf` in the row again.
    expect(text(name)).toBe('scan-scripts · wf_audit_01')
    for (const el of selfAndBelow(name)) {
      // MUTATION: put the phone's `.ov-wf-id { max-width: 10ch }` back.
      expect(painted(el, 'max-width', PHONE), el.className).toBeNull()
      // MUTATION: put the phone's ellipsis back on the id.
      expect(painted(el, 'text-overflow', PHONE), el.className).toBeNull()
    }
  })

  it('draws the whole id in a phone Running row, on a line that wraps', async () => {
    const el = await mount(
      page([task({ id: 'tsk_r', state: 'RUNNING', step_id: 'scan-scripts', runner_profile: 'claude-code', workflow_id: 'wf_audit_01', started_at: ago(5), metadata: null })]),
    )
    const sub = el.querySelector('#ov-running .ov-prun li .ov-prun-sub')!
    // MUTATION: draw `shortWorkflowId` in the phone row again.
    expect(text(sub)).toBe('running · claude-code · wf_audit_01')
    // MUTATION: put `white-space: nowrap` back on `.ov-prun-sub`.
    expect(painted(sub, 'white-space', PHONE)).toBe('normal')
    // MUTATION: put the ellipsis back on `.ov-prun-sub`.
    expect(painted(sub, 'text-overflow', PHONE)).toBeNull()
    // MUTATION: drop `overflow-wrap: anywhere`, so a long id runs out of the card.
    expect(painted(sub, 'overflow-wrap', PHONE)).toBe('anywhere')
  })
})

describe('ffp-002: phone Running row titles wrap rather than cut at the same point', () => {
  it('lets the row name wrap, whole', async () => {
    const el = await mount(
      page([
        task({ id: 'tsk_r1', state: 'RUNNING', step_id: 'scan-scripts', workflow_id: 'wf_audit_01', started_at: ago(5), metadata: null }),
        task({ id: 'tsk_r2', state: 'RUNNING', step_id: 'scan-terraform', workflow_id: 'wf_audit_01', started_at: ago(4), metadata: null }),
      ]),
    )
    const names = [...el.querySelectorAll('#ov-running .ov-prun li .ov-prun-n')]
    expect(names.length).toBe(2)
    for (const n of names) {
      // MUTATION: put `white-space: nowrap` back on `.ov-prun-n`.
      expect(painted(n, 'white-space', PHONE)).toBe('normal')
      // MUTATION: put the ellipsis back on `.ov-prun-n`.
      expect(painted(n, 'text-overflow', PHONE)).toBeNull()
      // MUTATION: drop `overflow-wrap: anywhere` from `.ov-prun-n`.
      expect(painted(n, 'overflow-wrap', PHONE)).toBe('anywhere')
    }
  })
})

describe('ff-001: a Recent failures row says its state in a word', () => {
  it('draws the state word, in sight, under each row name', () => {
    const { container } = render(
      <RecentFailures
        tasks={page([
          task({ id: 'tsk_f', state: 'FAILED', completed_at: ago(2), updated_at: ago(2), last_error: 'exit status 1' }),
          task({ id: 'tsk_d', state: 'DEAD_LETTERED', completed_at: ago(3), updated_at: ago(3) }),
          task({ id: 'tsk_c', state: 'CANCELLED', completed_at: ago(4), updated_at: ago(4) }),
        ])}
      />,
    )
    const rows = [...container.querySelectorAll('.ov-fails tbody tr')]
    expect(rows.length).toBe(3)
    const words = rows.map((r) => r.querySelector('th .ov-fstate'))
    // MUTATION: drop the `.ov-fstate` word from the row head.
    expect(words.map(text)).toEqual(['failed', 'dead-lettered', 'cancelled'])
    for (const w of words) {
      // MUTATION: draw the word visually hidden (`.sk-vh`), as the bare mark does.
      expect(w!.closest('.sk-vh')).toBeNull()
      expect(painted(w!, 'display', WIDE)).not.toBe('none')
    }
    // The row's accessible text says the state once: the bare mark beside it is decoration.
    // MUTATION: leave the mark's own hidden word exposed beside the new one.
    expect(rows[0]!.querySelector('td:first-child [aria-hidden="true"]')).not.toBeNull()
    // The error still reads after the word.
    // MUTATION: drop the error line when a state word is drawn.
    expect(text(rows[0]!.querySelector('th .ov-why'))).toBe('failed · exit status 1')
  })
})

describe('a11y-keyboard-003: every Overview table has an accessible name', () => {
  it('names the Running now tables', async () => {
    const running = Array.from({ length: 10 }, (_, i) =>
      task({ id: `tsk_run_${i}`, state: 'RUNNING', step_id: `step-${i}`, started_at: ago(10 + i), metadata: null }),
    )
    await mount(page(running))
    // MUTATION: drop `aria-label` from the Running now table.
    expect(screen.getByRole('table', { name: 'Running now' })).toBeTruthy()
    // MUTATION: drop `aria-label` from the "N more" table under it.
    expect(screen.getByRole('table', { name: 'Running now, the rest', hidden: true })).toBeTruthy()
  })

  it('names the Recent failures tables', () => {
    const failed = Array.from({ length: 8 }, (_, i) =>
      task({ id: `tsk_fail_${i}`, state: 'FAILED', completed_at: ago(2 + i), updated_at: ago(2 + i) }),
    )
    render(<RecentFailures tasks={page(failed)} />)
    // MUTATION: drop `aria-label` from the Recent failures table.
    const first = screen.getByRole('table', { name: 'Recent failures' })
    expect(within(first).getAllByRole('row').length).toBe(6)
    // MUTATION: drop `aria-label` from the "N more" table under it.
    const rest = screen.getByRole('table', { name: 'Recent failures, the rest', hidden: true })
    expect(within(rest).getAllByRole('row', { hidden: true }).length).toBe(2)
  })
})
