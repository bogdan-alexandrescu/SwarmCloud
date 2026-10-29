/**
 * VISUAL QA PASS 2 (2026-10-29, the fixture console, dark desktop), lane
 * QA2-FIX-08, part of #2694: Work › Timeline › Lanes.
 *
 *   runs2-28  Scrolled to the workflow group (14d), the sticky time axis ('2
 *             workflows · 15 standalone / times UTC / Oct 16…now') covered
 *             the top half of the group header 'Audit the operator scripts
 *             and t…'.
 *
 * The axis sticks at the top of the page scroller (G3-12, qa.c6s.lanes), so a
 * row scrolled into view must stop the axis's height lower. That height is
 * measured here from the sheet's own values -- the label cell's padding, its
 * lines of `--t-micro` × `--lh-micro`, the axis track's height -- at 1440,
 * where the label sits beside the track, and at 390, where it sits above it.
 *
 * MUTATION, turns both cases red: drop `.tl-row { scroll-margin-top }` from
 * timeline.css, or set `--tl-axis-h` below the axis's drawn height.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import type { WorkflowRead } from '../api'
import type { CascadeEnv } from './cssgate'
import { painted, TABLES } from './marks'
import { byIdVia } from './workflowsById'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowsById: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

function task(id: string, wf: string, step: string): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'mock', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: wf, step_id: step, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null,
  } as Task
}

function attempt(taskId: string, startMin: number, endMin: number): AttemptRow {
  return {
    attempt_id: `att_${taskId}`, task_id: taskId, tenant_id: 'eng', generation: 1, lease_id: `l_${taskId}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(startMin + 1), started_at: ago(startMin),
    completed_at: ago(endMin), exit_code: 0, error: null, peak_rss_bytes: null, peak_disk_bytes: null,
    oom_near_miss: false, checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
    cache_creation_input_tokens: null, cost_usd: null,
  } as AttemptRow
}

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockResolvedValue(
    ok({
      tenant_id: 'eng',
      read_at: new Date(NOW).toISOString(),
      attempts: [attempt('task_plan', 200, 150), attempt('task_build', 140, 100)],
      next_page_token: null,
      coverage: { scope: 'page' },
    } as AttemptsPage),
  )
  api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: [task('task_plan', 'wf_audit', 'plan'), task('task_build', 'wf_audit', 'build')] } as TaskPage))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
  api.loadWorkflow.mockImplementation(async (wf: string) =>
    ok({ workflow: { workflow_id: wf, state: 'SUCCEEDED', steps: [{}, {}] }, tasks: [] } as unknown as WorkflowRead),
  )
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/** A `px` length, a unitless `0`, or a `:root` token naming one. */
function px(value: string | null, what: string): number {
  const v = (value ?? '').trim()
  const token = /^var\(\s*(--[\w-]+)\s*\)$/.exec(v)
  if (token !== null) return px(TABLES.dark.get(token[1]!) ?? null, `${what} (${token[1]})`)
  if (v === '0') return 0
  const m = /^(-?[\d.]+)px$/.exec(v)
  if (m === null) throw new Error(`${what}: ${JSON.stringify(value)} is not a px length`)
  return Number(m[1])
}

/** One side of an element's padding, from a longhand or the shorthand. */
function padding(el: Element, side: 'top' | 'bottom', env: CascadeEnv): number {
  const v = painted(el, [`padding-${side}`, 'padding'], env)
  if (v === null) return 0
  const parts = v.trim().split(/\s+/)
  if (parts.length === 1) return px(parts[0]!, `padding-${side}`)
  // A shorthand: top is its first value, bottom its third (or its first).
  return px(side === 'top' ? parts[0]! : (parts[2] ?? parts[0]!), `padding-${side}`)
}

const LINE = (): number => px('var(--t-micro)', 'font') * Number(TABLES.dark.get('--lh-micro'))

/** The axis row's drawn height: a label of `lines` lines, beside or above its track. */
function axisHeight(env: CascadeEnv, lines: number, stacked: boolean): number {
  const lab = document.querySelector('.tl-row.tl-axis > .tl-lab')!
  const trk = document.querySelector('.tl-row.tl-axis > .tl-trk')!
  const gap = px(painted(lab, ['row-gap', 'gap'], env), 'gap')
  const label = padding(lab, 'top', env) + padding(lab, 'bottom', env) + lines * LINE() + (lines - 1) * gap
  const track = px(painted(trk, 'height', env), 'track height')
  return stacked ? label + track : Math.max(label, track)
}

async function drawn(): Promise<void> {
  render(<TimelineLanesScreen view={null} />)
  await waitFor(() => expect(document.querySelector('.tl-row.is-group a')).not.toBeNull())
  await waitFor(() => expect(document.querySelector('[data-lane="task_build"]')).not.toBeNull())
}

describe('runs2-28: a group header scrolled to is not covered by the sticky axis', () => {
  // 1440: the lane count may wrap in the 240px label column, so three lines.
  // 390: the label is the row's full width, so two lines, above the track.
  const CASES: { env: CascadeEnv; lines: number; stacked: boolean }[] = [
    { env: { width: 1440 }, lines: 3, stacked: false },
    { env: { width: 390 }, lines: 2, stacked: true },
  ]

  it('stops every group and lane row at least the axis’s height below the scroller’s top', async () => {
    await drawn()
    const group = document.querySelector('.tl-row.is-group')!
    const lane = document.querySelector('[data-lane="task_build"]')!.closest('.tl-row')!
    for (const { env, lines, stacked } of CASES) {
      const axis = axisHeight(env, lines, stacked)
      for (const row of [group, lane]) {
        const margin = px(painted(row, 'scroll-margin-top', env), `scroll-margin-top at ${env.width}`)
        expect(margin, `${row.className} at ${env.width}: axis ${axis.toFixed(1)}px`).toBeGreaterThanOrEqual(axis)
        // No more than a line over it: a jump should not leave a gap of lanes above.
        expect(margin, `${row.className} at ${env.width}`).toBeLessThan(axis + LINE())
      }
    }
  })

  it('keeps the axis itself sticky at the scroller’s top (G3-12)', async () => {
    await drawn()
    const axis = document.querySelector('.tl-row.tl-axis')!
    for (const { env } of CASES) {
      expect(painted(axis, 'position', env)).toBe('sticky')
      expect(painted(axis, 'top', env)).toBe('0')
    }
  })
})
