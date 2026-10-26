// QAFIX3-WORKFLOWS, THE TIMELINE'S LANE LABELS (part of #1680; visual QA run 3,
// wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of main at 51d99c53):
//
//   work-a-004  at 1440, light and dark, the lane labels were cut at two
//          different points. A lane's name is its own flex item with
//          `min-width: 0` and an ellipsis, so it gives way with a `…` before
//          its state mark and duration. A workflow group's title was a bare
//          link (or a bare text run) in the same flex `<b>`: a flex item whose
//          minimum width is its text, so it ran on to the column's edge and
//          was sheared off there by the `<b>`'s `overflow: hidden`, mid-letter
//          and with no ellipsis (a flex container draws none of its own). The
//          title is now a name part that gives way the way a lane's name does.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowRead } from '../api'
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
const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const WIDE: CascadeEnv = { width: 1440 }
const ENVS: CascadeEnv[] = [
  { width: 1440, theme: 'light' },
  { width: 1440, theme: 'dark' },
]

// A label long enough to pass the 240px label column, as a workflow's units do.
const LABEL = 'PERM-2, S9, QA-104, QA-107, QA-108, QA-109, QA-113'

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: 'wf_long', step_id: 'implement', depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null, metadata: { unit: LABEL }, ...extra,
  } as Task
}
const STEP = task('task_impl')
const ATTEMPT = {
  attempt_id: 'att_task_impl_1', task_id: 'task_impl', tenant_id: 'eng', generation: 1, lease_id: 'l1',
  backend: 'cloud_run', execution_name: null, created_at: ago(280), started_at: ago(279), completed_at: ago(100), exit_code: 0,
  error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [], input_tokens: null,
  output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null,
} as unknown as AttemptRow
const PAGE = { tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts: [ATTEMPT], next_page_token: null, coverage: { scope: 'page' } } as AttemptsPage
const WF_READ = {
  workflow: { workflow_id: 'wf_long', state: 'SUCCEEDED', steps: [{ step_id: 'implement', task_id: 'task_impl' }] },
  tasks: [STEP],
} as unknown as WorkflowRead

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockResolvedValue(ok(PAGE))
  api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: [STEP] } as TaskPage))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
  api.loadWorkflow.mockResolvedValue(ok(WF_READ))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('work-a-004: a workflow title is cut the way a lane name is', () => {
  it('gives the group title a shrinkable box with an ellipsis, as the lane name has', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const link = await waitFor(() => {
      const a = document.querySelector<HTMLAnchorElement>('.tl-row.is-group[data-group="wf_long"] .tl-lab b a')
      expect(a).not.toBeNull()
      return a!
    })
    expect(link.textContent).toBe(LABEL)
    const lane = document.querySelector<HTMLElement>('.tl-row[data-lane="task_impl"] .tl-name')!
    expect(lane).not.toBeNull()
    for (const env of ENVS) {
      const at = `${env.width} ${env.theme}`
      // The control: the lane's name already gives way with an ellipsis.
      expect(painted(lane, 'min-width', env), `lane name at ${at}`).toBe('0')
      expect(painted(lane, 'text-overflow', env), `lane name at ${at}`).toBe('ellipsis')
      // MUTATION: render the title as the bare `<a>` again (no name-part
      // class) and these three are red: its min-width is its text, so it
      // runs to the column's edge and is sheared there with no ellipsis.
      expect(painted(link, 'min-width', env), `group title at ${at}`).toBe('0')
      expect(painted(link, 'overflow', env), `group title at ${at}`).toBe('hidden')
      expect(painted(link, 'text-overflow', env), `group title at ${at}`).toBe('ellipsis')
    }
    // MUTATION: give the title the lane's `.tl-name` button class and the
    // button reset paints it `--text`: a workflow link that no longer looks
    // like a link.
    expect(painted(link, 'color', WIDE)).toBe('var(--info)')
    // The whole label stays the link's tooltip, so nothing the cut takes is lost.
    expect(link.getAttribute('title')).toBe('wf_long')
  })

  it('cuts a title that is not a link the same way', async () => {
    // The workflow read fails, so the group is titled in plain text -- by the
    // label its own lane carries (VQ-025), the id closing the note under it.
    api.loadWorkflow.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 503, code: null, message: 'down' } })
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const b = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_long"] .tl-lab b')
      expect(el?.textContent).toContain(LABEL)
      // The title shows before the read fails; until it does the note says
      // "steps: reading…", so wait for the note too.
      expect(document.querySelector('.tl-row.is-group[data-group="wf_long"] .tl-lab small')?.textContent).toMatch(/^steps not read: /)
      return el!
    })
    expect(document.querySelector('.tl-row.is-group[data-group="wf_long"] .tl-lab small')!.textContent).toMatch(/^steps not read: .* · retry · wf_long$/)
    // MUTATION: leave the plain title as a bare text run in the flex `<b>` and
    // there is no element here to give way: it is sheared at the edge.
    const title = [...b.children].find((c) => c.textContent === LABEL) as HTMLElement | undefined
    expect(title, 'the plain title is an element of its own').toBeDefined()
    expect(painted(title!, 'min-width', WIDE)).toBe('0')
    expect(painted(title!, 'text-overflow', WIDE)).toBe('ellipsis')
  })
})
