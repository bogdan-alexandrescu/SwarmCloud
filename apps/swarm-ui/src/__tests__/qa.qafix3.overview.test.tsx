/**
 * VISUAL QA RUN 3 (wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of
 * main at 51d99c53): the Overview findings, lanes QAFIX3-OVERVIEW and
 * QAFIX3-OVERVIEW-B (#1680).
 *
 * BY DESIGN, OWNER 2026-10-21 (~15:20Z): four findings answer an earlier owner
 * decision and are kept as main drew them. Their tests hold that decision:
 *   overview-automate-003  the O1 390 frame (owner pick 2026-10-01): three
 *          tiles across at 390, figures only; "costs nothing" and the
 *          per-state breakdown are the desktop's.
 *   overview-automate-005  G1-13: a disabled Headroom tile's reason keeps
 *          its two-line clamp on a desktop and is whole on a phone (OV-13).
 *   overview-automate-006  owner Q5 (V263, 2026-10-16): a phone keeps the
 *          10-character head cut of a workflow id.
 *   ff-001  a Recent failures row's state is the glyph in the 28px mark
 *          column, its word the mark's accessible name.
 *
 * FIXED:
 *   ffp-002  The phone's Running rows cut their titles, so rows of one
 *          workflow were cut at the same point. They wrapped, whole, until
 *          VQ-002 (2026-10-26) found a long title took four lines a row: the
 *          row is one line now, the title cut and the step kept. The test
 *          reads the CASCADE in the bundle's order (overview.css, then
 *          names.css): PR 1848's override lost to names.css's clamp there.
 *   a11y-keyboard-003  The Overview's tables had no accessible name.
 *
 * Each assertion carries the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Stats, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { cascade } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import NAMES_CSS from '../styles/names.css?raw'
import STYLES_CSS from '../styles.css?raw'
import OVERVIEW_TSX from '../Overview.tsx?raw'
import APP_TSX from '../App.tsx?raw'

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

describe('overview-automate-003: the lifecycle band is three figures across at 390 (by design, owner 2026-10-21)', () => {
  it('draws three tiles across, figures only, and keeps the breakdown for the desktop', () => {
    const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={page([])} />)
    const band = container.querySelector('#ov-band')!
    const tiles = [...container.querySelectorAll('#ov-band .ov-lc')]
    expect(tiles.length).toBe(3)
    // By design, owner 2026-10-21 (the O1 390 frame, owner pick 2026-10-01).
    // MUTATION: give the phone band one tile per line (`grid-template-columns: minmax(0, 1fr)`).
    expect(painted(band, 'grid-template-columns', PHONE)).toBe('repeat(3, minmax(0, 1fr))')
    for (const t of tiles) {
      // MUTATION: show `.ov-lc-h small` ("costs nothing") in overview.css's 560px block.
      expect(painted(t.querySelector('.ov-lc-h small')!, 'display', PHONE)).toBe('none')
      // MUTATION: show `.ov-lc-ps` (the per-state breakdown) in the 560px block.
      expect(painted(t.querySelector('.ov-lc-ps')!, 'display', PHONE)).toBe('none')
      // The desktop draws both.
      // MUTATION: hide the caption at desktop width.
      expect(painted(t.querySelector('.ov-lc-h small')!, 'display', WIDE)).not.toBe('none')
    }
    const foot = document.createElement('span')
    foot.className = 'ov-lc-foot'
    tiles[0]!.appendChild(foot)
    // MUTATION: show the stats foot in the 560px block.
    expect(painted(foot, 'display', PHONE)).toBe('none')
    // The page foot stays (G1-02): a phone's 50 is not a desktop's 200.
    foot.classList.add('is-page')
    // MUTATION: drop `.ov-lc-foot.is-page { display: block }` from the 560px block.
    expect(painted(foot, 'display', PHONE)).toBe('block')
  })
})

describe('overview-automate-005: a disabled Headroom tile clamps its reason at 1440, whole on a phone (by design, owner 2026-10-21)', () => {
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

  it("keeps G1-13's two lines on a desktop and OV-13's whole reason on a phone", () => {
    const { container } = render(
      <div className="ov-headroom">
        <div className="ov-hps">
          <Overview.ProfileTile name="codex" profile={profile} byName={new Map()} tenant="eng" />
        </div>
      </div>,
    )
    const small = container.querySelector('.ov-hp.is-off small')!
    // MUTATION: drop the reason from the tile, or from its title.
    expect(text(small)).toContain('Use claude-code.')
    expect(small.getAttribute('title')).toContain(REASON)
    // By design, owner 2026-10-21 (G1-13): two lines on a desktop.
    // MUTATION: unclamp `.ov-hp.is-off small` at desktop width.
    expect(painted(small, '-webkit-line-clamp', WIDE)).toBe('2')
    // MUTATION: drop `overflow: hidden` from the desktop's disabled reason.
    expect(painted(small, 'overflow', WIDE)).toBe('hidden')
    // MUTATION: drop `display: -webkit-box` from the desktop's disabled reason.
    expect(painted(small, 'display', WIDE)).toBe('-webkit-box')
    // Whole on a phone (OV-13).
    // MUTATION: drop the phone block's `-webkit-line-clamp: unset`.
    expect(painted(small, '-webkit-line-clamp', PHONE)).toBe('unset')
    // MUTATION: drop the phone block's `overflow: visible`.
    expect(painted(small, 'overflow', PHONE)).toBe('visible')
  })
})

describe('overview-automate-006: a phone keeps the 10-character head of a workflow id (by design, owner 2026-10-21)', () => {
  const failed = (id: string, wf: string) =>
    task({ id, state: 'FAILED', completed_at: ago(3), updated_at: ago(3), step_id: 'scan-scripts', workflow_id: wf, metadata: null })

  it('cuts "wf_audit_01" to its head in a Recent failures row at 390, whole at 1440 and in the title', () => {
    const { container } = render(<RecentFailures tasks={page([failed('tsk_a', 'wf_audit_01')])} />)
    const name = container.querySelector('.ov-fails .ov-name')!
    // The title holds the link's words, its short id `a` included (walk.long
    // C, CIFIX-2632), then the whole id.
    // MUTATION: drop the id from the row's title.
    expect(name.getAttribute('title')).toBe('scan-scripts · wf_audit_01 · a · tsk_a')
    const id = name.querySelector('.ov-wf-id')!
    // MUTATION: draw the id bare, without the `.ov-wf-id` span the phone cuts.
    expect(text(id)).toBe('wf_audit_01')
    // By design, owner 2026-10-21 (Q5, V263).
    // MUTATION: drop the phone's `.ov-wf-id { max-width: 10ch }`.
    expect(painted(id, 'max-width', PHONE)).toBe('10ch')
    // MUTATION: drop the phone's ellipsis on the id.
    expect(painted(id, 'text-overflow', PHONE)).toBe('ellipsis')
    // MUTATION: cut the id at desktop width too.
    expect(painted(id, 'max-width', WIDE)).toBeNull()
  })

  it('draws the short id in a phone Running row, whole in its title', async () => {
    const el = await mount(
      page([task({ id: 'tsk_r', state: 'RUNNING', step_id: 'scan-scripts', runner_profile: 'claude-code', workflow_id: 'wf_audit_01', started_at: ago(5), metadata: null })]),
    )
    const sub = el.querySelector('#ov-running .ov-prun li .ov-prun-sub')!
    // By design, owner 2026-10-21 (Q5): nine characters and an ellipsis.
    // MUTATION: draw the whole `t.workflow_id` in the phone row.
    expect(text(sub)).toBe('running · claude-code · wf_audit_…')
    // MUTATION: drop the whole id from the link's title.
    expect(sub.querySelector('a')!.getAttribute('title')).toBe('step scan-scripts of wf_audit_01')
  })
})

/**
 * THE BUNDLE'S ORDER FOR THE TWO SHEETS THAT STYLE AN OVERVIEW NAME. Vite
 * emits CSS in first-import order: App.tsx imports Overview before Workflows
 * (names.css's other importer), and Overview.tsx imports overview.css, then
 * names.css. So names.css comes AFTER overview.css, and a tie in specificity
 * goes to names.css -- the opposite of `marks.ts`'s alphabetical glob, which
 * is how PR 1848's override passed its test and lost in the built console.
 */
const BUNDLE = [OVERVIEW_CSS, NAMES_CSS, STYLES_CSS].join('\n')

describe('ffp-002, VQ-002: a phone Running row is one line that keeps the step, never cut at the same point', () => {
  it('loads overview.css before names.css, as the app imports them', () => {
    const imports = (src: string) => [...src.matchAll(/^import\s+(?:[^'\n]*from\s+)?'([^']+)'/gm)].map((m) => m[1]!)
    const ov = imports(OVERVIEW_TSX)
    // MUTATION: import names.css before overview.css in Overview.tsx (the test's order would then be wrong).
    expect(ov.indexOf('./styles/overview.css')).toBeGreaterThanOrEqual(0)
    expect(ov.indexOf('./styles/overview.css')).toBeLessThan(ov.indexOf('./styles/names.css'))
    const app = imports(APP_TSX)
    // MUTATION: import Workflows (names.css's other importer) before Overview in App.tsx.
    expect(app.indexOf('./Overview')).toBeGreaterThanOrEqual(0)
    expect(app.indexOf('./Overview')).toBeLessThan(app.indexOf('./Workflows'))
  })

  it('cuts the title and keeps the step, in the cascade the built console paints', async () => {
    const el = await mount(
      page([
        task({ id: 'tsk_r1', state: 'RUNNING', step_id: 'scan-scripts', workflow_id: 'wf_audit_01', started_at: ago(5), metadata: null }),
        task({ id: 'tsk_r2', state: 'RUNNING', step_id: 'scan-terraform', workflow_id: 'wf_audit_01', started_at: ago(4), metadata: null }),
      ]),
    )
    const names = [...el.querySelectorAll('#ov-running .ov-prun li .ov-prun-n')]
    expect(names.length).toBe(2)
    const at = (n: Element, prop: string | readonly string[]) => {
      const r = cascade(BUNDLE, n, prop, PHONE)
      expect(r.unsupported).toEqual([])
      return r.winner?.value ?? null
    }
    for (const n of names) {
      // MUTATION: put `.ov-page a.ov-prun-n` back in names.css's two-line clamp.
      expect(at(n, '-webkit-line-clamp') ?? 'none').toBe('none')
      // MUTATION: put `.ov-page a.ov-prun-n` back in names.css's two-line clamp.
      expect(at(n, 'line-clamp') ?? 'none').toBe('none')
      // ONE LINE (VQ-002): a flex row of its parts, nothing wrapping.
      // MUTATION: put `.ov-page a.ov-prun-n` back in names.css's clamp (its `display: -webkit-box`).
      expect(at(n, 'display')).toBe('flex')
      // MUTATION: put `white-space: normal` back on `.ov-prun-n`.
      expect(at(n, 'white-space')).toBe('nowrap')
      // NOT CUT AT ITS END (ffp-002): the anchor itself has no ellipsis; the
      // part that gives way is the one that does not tell the rows apart.
      // MUTATION: put the ellipsis on `.ov-prun-n` itself.
      expect(at(n, 'text-overflow')).toBeNull()
      const cut = n.querySelector('.ov-prun-t')!
      // MUTATION: drop the ellipsis from `.ov-prun-t`.
      expect(at(cut, 'text-overflow')).toBe('ellipsis')
      expect(at(cut, 'min-width')).toBe('0')
    }
    // These two rows carry no workflow title, so the step is the one part,
    // and it is the step that differs between them.
    expect(names.map((n) => (n.textContent ?? '').trim())).toEqual(['scan-scripts', 'scan-terraform'])
  })

  it("keeps the two-line clamp on the desktop cards' names", () => {
    const { container } = render(
      <div className="ov-page">
        <RecentFailures tasks={page([task({ id: 'tsk_f', state: 'FAILED', completed_at: ago(2), updated_at: ago(2) })])} />
      </div>,
    )
    const name = container.querySelector('.ov-fails a.ov-name')!
    // MUTATION: drop `.ov-page a.ov-name` from names.css's clamp along with the phone rows.
    expect(cascade(BUNDLE, name, '-webkit-line-clamp', WIDE).winner?.value).toBe('2')
  })
})

describe('ff-001: a Recent failures row says its state by the mark, its word the accessible name (by design, owner 2026-10-21)', () => {
  it('draws the glyph in the mark column and no state line under the name', () => {
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
    const marks = rows.map((r) => r.querySelector('td:first-child')!)
    // By design, owner 2026-10-21: the word is the mark's accessible name.
    // MUTATION: wrap the mark in `aria-hidden`, hiding its word from a screen reader.
    for (const m of marks) expect(m.querySelector('[aria-hidden="true"] .sk-st, .sk-st[aria-hidden="true"]')).toBeNull()
    // MUTATION: drop the mark's word (or the mark) from the 28px column.
    expect(marks.map(text)).toEqual([expect.stringContaining('failed'), expect.stringContaining('dead-lettered'), expect.stringContaining('cancelled')])
    // MUTATION: draw a `.ov-fstate` word under the name again.
    for (const r of rows) expect(r.querySelector('th .ov-fstate')).toBeNull()
    // MUTATION: lead the error line with the state word ("failed · exit status 1").
    expect(text(rows[0]!.querySelector('th .ov-why'))).toBe('exit status 1')
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
