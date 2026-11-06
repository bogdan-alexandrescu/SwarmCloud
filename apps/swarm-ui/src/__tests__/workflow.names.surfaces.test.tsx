/**
 * ONE WORKFLOW, ONE NAME, ON THE THREE SURFACES THE FACTS CARD LEFT (part of
 * #2688; Bogdan's answers on the facts card lane, 2026-11-06). Outcomes'
 * failed workflows (Ledger), the submit chooser's "your submissions" and the
 * Timeline name a workflow by `workflowName` -- its served `title`, falling
 * back to its label off the step tasks -- as the page heading, the list and
 * the sidebar already do. And the fixture workflows carry swarm-api's
 * about-fields, so the fixture facts card draws a description, with one
 * workflow left without them so 'not recorded' is still drawn.
 *
 * MUTATION: put `workflowLabel` back in any one caller -- its case finds the
 * steps' label where the served title belongs. Drop the about-fields from
 * `fixtureWorkflowRows` -- the fixture card finds 'not recorded' everywhere.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowRead } from '../api'
import type { Task, TaskPage, Workflow } from '../types'
import { ledgerFixture } from '../outcomes.fixture'
import { task } from './runfixture'
import { byIdVia } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowsById: vi.fn(),
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsFailedCard } = await import('../Ledger')
const { recentSubmissions } = await import('../SubmitChooser')
const { TimelineLanesScreen } = await import('../TimelineLanes')
const { WorkflowFacts, aboutOf } = await import('../WorkflowFacts')
const real = await vi.importActual<typeof import('../api')>('../api')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const words = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const SERVED = 'Restore runbook, end to end'
const LABEL = 'restore-runbook'

/** A workflow row with one step on `taskId`, and the about-fields swarm-api serves when `title` is given. */
function workflowRow(id: string, taskId: string, title?: string | null): Workflow {
  const about = title === undefined ? {} : { title, description: null, description_source: null, feature: null, started_from: 'console', repository_url: null }
  return {
    workflow_id: id, tenant_id: 'eng', state: 'FAILED', created_at: '2026-11-05T10:00:00.000Z', updated_at: '2026-11-05T11:00:00.000Z',
    submitted_by: 'alice@example.com', priority: 0, on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false,
    steps: [{ step_id: 'draft', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: taskId }],
    ...about,
  } as unknown as Workflow
}

const stepTask = (id: string, workflowId: string): Task =>
  task({ id, workflow_id: workflowId, step_id: 'draft', metadata: { title: LABEL } } as Partial<Task>)

describe('Outcomes › failed workflows names a workflow by its served title', () => {
  function readsOf(...rows: Workflow[]): WorkflowRead[] {
    return rows.map((w) => ({ workflow: w, tasks: [stepTask(w.steps[0]!.task_id!, w.workflow_id)] }) as unknown as WorkflowRead)
  }

  it('draws the served title, and the label when no title was served', async () => {
    const d = ledgerFixture()
    const [a, b] = d.workflows_failed.rows.map((r) => r.workflow_id)
    const [titled, untitled] = readsOf(workflowRow(a!, 'task_a', SERVED), workflowRow(b!, 'task_b', null))
    api.loadWorkflowsById.mockResolvedValue(ok({ batched: true, reads: new Map([[a!, titled!], [b!, untitled!]]), missing: [] }))
    const { container } = render(<WorkflowsFailedCard data={d} spanLabel="14d" />)
    const heads = await waitFor(() => {
      const th = [...container.querySelectorAll('.ol-workflows tbody tr th')]
      expect(words(th[0]!)).toContain(SERVED)
      return th
    })
    expect(words(heads[0]!)).not.toContain(LABEL)
    expect(words(heads[1]!), 'a null title falls back to the label').toContain(LABEL)
  })
})

describe("the submit chooser's 'your submissions' names a workflow by its served title", () => {
  it('draws the served title, and the label when no title was served', () => {
    const tasks = [stepTask('task_a', 'wf_titled'), stepTask('task_b', 'wf_label')]
    const workflows = [workflowRow('wf_titled', 'task_a', SERVED), { ...workflowRow('wf_label', 'task_b'), created_at: '2026-11-05T09:00:00.000Z' }]
    const got = recentSubmissions('alice@example.com', tasks, workflows, [])
    expect(got.map((g) => [g.id, g.name])).toEqual([
      ['wf_titled', SERVED],
      ['wf_label', LABEL],
    ])
  })
})

describe('the Timeline names a workflow by its served title', () => {
  const NOW = Date.now()
  const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
  const lanes = (title?: string | null) => {
    const t = { ...stepTask('task_plan', 'wf_7c1e'), state: 'SUCCEEDED', created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100) } as Task
    api.loadTasks.mockResolvedValue(ok({ tasks: [t] } as TaskPage))
    api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no' } })
    api.loadAttemptsPage.mockResolvedValue(
      ok({
        tenant_id: 'eng', read_at: new Date(NOW).toISOString(), next_page_token: null, coverage: { scope: 'page' },
        attempts: [{
          attempt_id: 'att_1', task_id: 'task_plan', tenant_id: 'eng', generation: 1, lease_id: 'l1', backend: 'cloud_run', execution_name: null,
          created_at: ago(240), started_at: ago(239), completed_at: ago(160), exit_code: 0, error: null, peak_rss_bytes: null, peak_disk_bytes: null,
          oom_near_miss: false, checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
          cache_creation_input_tokens: null, cost_usd: null,
        }],
      }),
    )
    api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
    api.loadOutcomes.mockResolvedValue(ok({ ...ledgerFixture(), tz: 'UTC', bucket: 'hour', since: new Date(NOW - 86_400_000).toISOString(), until: new Date(NOW).toISOString() }))
    api.loadWorkflow.mockResolvedValue(ok({ workflow: workflowRow('wf_7c1e', 'task_plan', title), tasks: [t] } as unknown as WorkflowRead))
    api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  }
  const option = () => [...document.querySelectorAll('option')].find((o) => o.value === 'wf_7c1e') ?? null

  it('draws the served title in the Workflow filter and the group head once the read lands', async () => {
    lanes(SERVED)
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(words(option())).toBe(`${SERVED} · wf_7c1e`))
    const head = document.querySelector('.tl-row.is-group[data-group="wf_7c1e"] .tl-name-k')
    expect(words(head)).toBe(SERVED)
  })

  it('falls back to the label when no title was served', async () => {
    lanes(null)
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(words(option())).toBe(`${LABEL} · wf_7c1e`))
  })
})

describe('the fixture workflows carry the about-fields swarm-api serves', () => {
  async function board() {
    const r = await real.loadWorkflowBoard()
    expect(r.status).toBe('ok')
    return (r as { data: import('../api').WorkflowBoard }).data
  }

  it('a fixture facts card draws a description, one submitted and one generated, and one workflow has none', async () => {
    const { workflows, taskById } = await board()
    const abouts = new Map(workflows.map((w) => [w.workflow_id, aboutOf(w)]))
    const sources = workflows.map((w) => (w as unknown as Record<string, unknown>).description_source).filter((s) => typeof s === 'string')
    expect(new Set(sources)).toEqual(new Set(['submitted', 'generated']))
    const none = [...abouts.values()].filter((a) => a.title === undefined && a.description === undefined)
    expect(none.length, "one fixture workflow keeps 'not recorded'").toBeGreaterThanOrEqual(1)

    const submitted = workflows.find((w) => (w as unknown as Record<string, unknown>).description_source === 'submitted')!
    const about = abouts.get(submitted.workflow_id)!
    expect(about.title).toBeTypeOf('string')
    expect(about.description).toBeTypeOf('string')
    expect(about.feature).not.toBeUndefined()
    expect(about.startedFrom).toBeTypeOf('string')
    expect(about.repositoryUrl).toBeTypeOf('string')
    const { container } = render(<WorkflowFacts workflow={submitted} taskById={taskById} started={null} now={Date.now()} />)
    expect(words(container.querySelector('.wff-text'))).toBe(about.description)
    expect(container.querySelector('.wff-gen')).toBeNull()
    cleanup()

    const generated = workflows.find((w) => (w as unknown as Record<string, unknown>).description_source === 'generated')!
    const g = render(<WorkflowFacts workflow={generated} taskById={taskById} started={null} now={Date.now()} />)
    expect(g.container.querySelector('.wff-gen'), 'a generated description is marked').not.toBeNull()
    expect(words(g.container.querySelector('.wff-text'))).toBe(aboutOf(generated).description)
    cleanup()

    const bare = workflows.find((w) => aboutOf(w).description === undefined)!
    const n = render(<WorkflowFacts workflow={bare} taskById={taskById} started={null} now={Date.now()} />)
    expect(words(n.container.querySelector('.wff-text'))).toBe('not recorded')
  }, 20_000)

  it("a fixture workflow's served title is the one its steps carry", async () => {
    const { workflows, taskById } = await board()
    const { workflowLabel } = await import('../stepviews')
    for (const w of workflows) {
      const title = aboutOf(w).title
      if (title !== undefined) expect(title, w.workflow_id).toBe(workflowLabel(w, taskById))
    }
  }, 20_000)
})
