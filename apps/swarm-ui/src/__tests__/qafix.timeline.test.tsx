// QAFIX-RUNS-TIMELINE, THE TIMELINE'S LANES PAGE (part of #1680, the first
// console visual QA run, wf_9e9bf74834634db7bcc3, 2026-10-20):
//
//   QA-093 (and QA-095)  the outcome strip printed '0 generations fenced' and
//          '0 parks, 0 still parked' while the attempts were still being
//          read: it counted fences and parks over no lanes and printed the
//          zeros as measured, under its own "count only the lanes drawn".
//   QA-094  at 390 the span control (24h/7d/14d/30d/90d) wrapped into a
//          ragged three-and-two grid about 220px wide.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
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
const failed = <T,>(httpStatus: number, message: string): Result<T> => ({
  status: 'error',
  error: { kind: 'server', httpStatus, code: null, message } as never,
})
const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

const TASK = {
  id: 'task_solo', tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
  priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
  submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
  workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
  current_generation: 1, current_lease_id: null,
} as unknown as Task
const ATTEMPT = {
  attempt_id: 'att_task_solo_1', task_id: 'task_solo', tenant_id: 'eng', generation: 1, lease_id: 'l1',
  backend: 'cloud_run', execution_name: null, created_at: ago(280), started_at: ago(279), completed_at: ago(100), exit_code: 0,
  error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [], input_tokens: null,
  output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null,
} as unknown as AttemptRow
const page = (attempts: AttemptRow[]) =>
  ({ tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts, next_page_token: null, coverage: { scope: 'page' } }) as AttemptsPage

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadTaskEventsPage.mockImplementation(async () => ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: [TASK] } as TaskPage))
  api.loadTask.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadWorkflow.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const strip = () => document.querySelector('.tl-strip') as HTMLElement

describe('QA-093: no fence or park count is printed before a lane is drawn', () => {
  it('says the lanes are being read where the counts go, while the attempts read is out', async () => {
    api.loadAttemptsPage.mockImplementation(() => new Promise(() => {}))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // The outcomes read lands; the attempts read never does.
    await waitFor(() => expect(strip().textContent).toMatch(/% success/))
    const text = (strip().textContent ?? '').replace(/\s+/g, ' ')
    // MUTATION: compute the strip's fenced and parks over `allLanes` with no
    // regard for whether any lane was read (the code before this fix) and
    // these two are red: it prints '0 generations fenced' and '0 parks'.
    expect(text).not.toMatch(/\d+\+? generations? fenced/)
    expect(text).not.toMatch(/\d+ parks?, \d+ still parked/)
    // MUTATION: drop the 'reading the lanes' branch and print nothing in
    // the counts' place, and these are red.
    expect(text).toMatch(/fenced — reading the lanes/)
    expect(text).toMatch(/parks — reading the lanes/)
  })

  it('says the lanes were not read when the attempts read failed', async () => {
    api.loadAttemptsPage.mockResolvedValue(failed(503, 'unavailable'))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(strip().textContent).toMatch(/% success/))
    await waitFor(() => expect(strip().textContent).toMatch(/fenced — the lanes were not read/))
    // MUTATION: treat a failed read as a read of no lanes, and this is red.
    expect(strip().textContent).not.toMatch(/\d+ parks?, \d+ still parked/)
    expect(strip().textContent).toMatch(/parks — the lanes were not read/)
  })

  it('prints the measured counts once the lanes are drawn', async () => {
    api.loadAttemptsPage.mockResolvedValue(ok(page([ATTEMPT])))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    // MUTATION: keep the reading words after the lanes land, and this is red.
    await waitFor(() => expect(strip().textContent).toMatch(/0 generations fenced/))
    expect(strip().textContent).toMatch(/0 parks, 0 still parked/)
  })
})

describe('QA-094: the span control is one even row across a phone', () => {
  it('draws the five spans as equal cells on one line the column wide', async () => {
    api.loadAttemptsPage.mockResolvedValue(ok(page([ATTEMPT])))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const seg = await waitFor(() => within(document.body).getByRole('group', { name: 'Span' }))
    const cells = [...seg.querySelectorAll<HTMLElement>(':scope > button')]
    expect(cells.map((b) => b.textContent)).toEqual(['24h', '7d', '14d', '30d', '90d'])
    // MUTATION: drop the phone rule's `flex-wrap: nowrap` and the shared
    // control's three-to-a-row wrap (components.css V207) applies again.
    expect(painted(seg, 'flex-wrap', PHONE)).toBe('nowrap')
    // MUTATION: drop `flex: 1 1 100%` and the control is its cells' width,
    // ~220 of the column's 358px.
    expect(painted(seg, ['flex', 'flex-basis'], PHONE)).toBe('1 1 100%')
    // MUTATION: leave the cells on components.css's 30% basis or `1 0 auto`
    // and they are content-sized again, their borders out of line.
    for (const b of cells) expect(painted(b, ['flex', 'flex-basis'], PHONE), b.textContent ?? '').toBe('1 1 0')
    // The desktop control keeps the canonical packing.
    // MUTATION: move the rule out of its phone @media and this is red.
    expect(painted(cells[0]!, ['flex', 'flex-basis'], WIDE)).toBeNull()
  })
})
