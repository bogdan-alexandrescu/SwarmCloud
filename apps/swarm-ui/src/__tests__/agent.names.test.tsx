// AN AGENT IS NAMED BY ITS WORKFLOW STEP, AND ONE CLICK REACHES ITS WORKFLOW.
//
//   #94   Four running rows of one workflow all read `claude-code task_…`, and
//         the inspector was headed by the raw task id with `wf … · scan-08`
//         as plain text under it. The step is the name; the id stays, whole
//         and copyable; the workflow id is a link to that workflow's page
//         (`#work/workflows?wf=<id>`, which App routes to `/workflows/<id>`).
//         A standalone task keeps its id as its name. The group header keeps
//         `N here` -- there is no per-workflow read to replace it with yet.
//   #109  At 390px the wide row drops its Step column, so five live rows read
//         `running claude-code <hex> 7m 20s`. At ≤560 the name cell shows the
//         step in place of the profile.
//
// MUTATIONS, one per block: head the inspector with `taskId` again; drop the
// id fact; print the workflow id as text; unlink the group header; delete the
// `.agent-step` phone rule (or render no `.agent-step`).

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { AgentDetailScreen, Run, agentName } = await import('../AgentDetail')

afterEach(() => vi.clearAllMocks())

const NOW = '2026-09-23T12:00:00.000Z'
const THEN = '2026-09-23T10:00:00.000Z'

function listTask(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id,
    state,
    created_at: THEN,
    updated_at: NOW,
    started_at: THEN,
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? NOW : null,
    submitted_by: 'ada@acme.test',
    ...over,
  })
}

async function land(tasks: Task[]): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} />)
  await waitFor(() => expect(container.querySelector('.row.clickable')).not.toBeNull())
  return container as HTMLElement
}

function run(t: Task): AgentRun {
  return {
    task: t,
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function quietPanels(): void {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
}

const STEP = runTask({
  id: 'task_9c0ade75fd0c4f1c9a6e',
  workflow_id: 'wf_a25f6eb11a5b40bd8b58',
  step_id: 'scan-08',
  state: 'SUCCEEDED',
  started_at: at(1),
  completed_at: at(10),
})

describe('#94: the inspector is headed by the step, with the id kept and copyable', () => {
  it('names a workflow step by its step, and a standalone task by its whole id', () => {
    expect(agentName(STEP)).toBe('scan-08')
    expect(agentName(runTask({ id: 'task_0123456789abcdef0123' }))).toBe('task_0123456789abcdef0123')
  })

  it('heads the drawer with the step once the run is read', async () => {
    quietPanels()
    api.loadAgentRun.mockResolvedValue({ status: 'ok', data: run(STEP), fetchedAt: Date.now() })
    render(<AgentDetailScreen taskId={STEP.id} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'scan-08' })).toBeTruthy())
    expect(screen.queryByRole('heading', { name: STEP.id })).toBeNull()
  })

  it('keeps the whole task id as a fact with a copy control beside it', async () => {
    quietPanels()
    const { container } = render(<Run run={run(STEP)} />)
    await waitFor(() => expect(container.querySelector('.ctl-facts')).not.toBeNull())
    const fact = container.querySelector<HTMLElement>('.ctl-fact.ad-id')
    expect(fact, 'no id fact').not.toBeNull()
    expect(fact!.textContent).toContain(STEP.id)
    const copy = fact!.querySelector('button')
    expect(copy?.getAttribute('aria-label')).toBe(`Copy task id ${STEP.id}`)
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true })
    fireEvent.click(copy!)
    expect(write).toHaveBeenCalledWith(STEP.id)
    await waitFor(() => expect(fact!.textContent).toContain('task id copied'))
  })

  it('draws no id fact for a standalone task, whose heading already is the id', async () => {
    quietPanels()
    const { container } = render(<Run run={run(runTask({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) }))} />)
    await waitFor(() => expect(container.querySelector('.ctl-facts')).not.toBeNull())
    expect(container.querySelector('.ctl-fact.ad-id')).toBeNull()
  })

  it("links the workflow id to that workflow's page", async () => {
    quietPanels()
    const { container } = render(<Run run={run(STEP)} />)
    await waitFor(() => expect(container.querySelector('.ctl-facts')).not.toBeNull())
    const link = [...container.querySelectorAll<HTMLAnchorElement>('.ctl-facts a')].find((a) =>
      a.textContent?.includes('wf_a25f6eb11a5b40bd8b58'),
    )
    expect(link, 'the workflow id is not a link').toBeTruthy()
    expect(link!.getAttribute('href')).toBe('#work/workflows?wf=wf_a25f6eb11a5b40bd8b58')
  })
})

describe('#94: a workflow group header reaches its workflow', () => {
  it('links the group id, and keeps the honest `N here` count', async () => {
    const c = await land([
      listTask('task_aaaaaaaa00000000000a', 'SUCCEEDED', { workflow_id: 'wf_one', step_id: 'scan-01' }),
      listTask('task_bbbbbbbb00000000000b', 'FAILED', { workflow_id: 'wf_one', step_id: 'scan-04' }),
    ])
    fireEvent.click(screen.getByLabelText(/group by workflow/i))
    await waitFor(() => expect(c.querySelector('.section.group')).not.toBeNull())
    const head = c.querySelector('.section.group h2')!
    const link = head.querySelector('a')
    expect(link?.getAttribute('href')).toBe('#work/workflows?wf=wf_one')
    expect(link?.textContent).toContain('wf_one')
    expect(head.textContent).toContain('2 here')
  })
})

describe('#94 and #109: at phone width a row is named by its step', () => {
  const PHONE: CascadeEnv = { width: 390 }
  const WIDE: CascadeEnv = { width: 1440 }
  const display = (el: Element, env: CascadeEnv) => {
    const r = cascade(STYLES, el, 'display', env)
    expect(r.unsupported).toEqual([])
    return r.winner?.value ?? null
  }

  it('shows the step in place of the profile at 390, and only the profile at 1440 where Step is a column', async () => {
    const c = await land([
      listTask('task_aaaaaaaa00000000000a', 'RUNNING', { workflow_id: 'wf_one', step_id: 'scan-01' }),
      listTask('task_bbbbbbbb00000000000b', 'RUNNING', { workflow_id: 'wf_one', step_id: 'scan-04' }),
    ])
    const rows = [...c.querySelectorAll('.row.clickable')]
    const steps = rows.map((r) => r.querySelector('.agent .agent-step')?.textContent)
    expect(steps.sort()).toEqual(['scan-01', 'scan-04'])
    const row = rows[0]!
    const step = row.querySelector('.agent .agent-step')!
    const profile = row.querySelector('.agent .agent-profile')!
    expect(profile.textContent).toBe('claude-code')
    expect(display(step, PHONE)).not.toBe('none')
    expect(display(profile, PHONE)).toBe('none')
    expect(display(step, WIDE)).toBe('none')
    expect(display(profile, WIDE)).not.toBe('none')
  })

  it('keeps the profile on a standalone row, which has no step to show', async () => {
    const c = await land([listTask('task_cccccccc00000000000c', 'RUNNING')])
    const row = c.querySelector('.row.clickable')!
    expect(row.querySelector('.agent-step')).toBeNull()
    const profile = row.querySelector('.agent > b')!
    expect(profile.classList.contains('agent-profile')).toBe(false)
    expect(display(profile, PHONE)).not.toBe('none')
  })
})
