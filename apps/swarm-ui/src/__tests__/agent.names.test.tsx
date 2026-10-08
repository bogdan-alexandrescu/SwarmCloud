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
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadResourceClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { AgentDetailScreen, agentName } = await import('../AgentDetail')
const { AgentSplit } = await import('../AgentSplit')

/**
 * The split's header for `t`: the id and the workflow are said there, once
 * (walkthrough B), in the meta line (agent-details-v3.html A).
 */
async function header(t: Task): Promise<HTMLElement> {
  quietPanels()
  const ok = <T,>(data: T) => ({ status: 'ok' as const, data, fetchedAt: Date.now() })
  api.loadTask.mockResolvedValue(ok(t))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockReturnValue(new Promise(() => {}))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  render(<AgentSplit taskId={t.id} pane="attempts" artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${t.id}`} />)
  return waitFor(() => {
    const h = document.querySelector<HTMLElement>('.ag-head')
    expect(h?.querySelector('.ag-head-facts li')).toBeTruthy()
    return h!
  })
}

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
  it('names a workflow step by its step, and a standalone task by what it is and its short id', () => {
    expect(agentName(STEP)).toBe('scan-08')
    // Walkthrough G (2026-10-03): a lone task was its bare hash.
    expect(agentName(runTask({ id: 'task_0123456789abcdef0123', runner_profile: 'claude-code', step_id: null }))).toBe('claude-code task · 01234567')
  })

  it('heads the drawer with the step once the run is read', async () => {
    quietPanels()
    api.loadAgentRun.mockResolvedValue({ status: 'ok', data: run(STEP), fetchedAt: Date.now() })
    render(<AgentDetailScreen taskId={STEP.id} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'scan-08' })).toBeTruthy())
    expect(screen.queryByRole('heading', { name: STEP.id })).toBeNull()
  })

  it('prints the whole task id under the name, with a copy control, in the header', async () => {
    const head = await header(STEP)
    expect(head.querySelector('.ag-head-id .tid-text')?.textContent).toBe(STEP.id)
    const copy = head.querySelector<HTMLButtonElement>('.ag-head-id .tid-copy')
    expect(copy, 'no id copy').not.toBeNull()
    expect(copy!.title).toContain(STEP.id)
    expect(copy!.getAttribute('aria-label')).toBe(`Copy task id ${STEP.id}`)
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true })
    fireEvent.click(copy!)
    expect(write).toHaveBeenCalledWith(STEP.id)
    await waitFor(() => expect(head.querySelector('.ag-head-id [role="status"]')?.textContent).toBe('task id copied'))
  })

  it('prints a standalone task’s id once, on the id line, not again in the meta line', async () => {
    const t = runTask({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) })
    const head = await header(t)
    const shown = head.cloneNode(true) as HTMLElement
    shown.querySelector('.ag-head-title')?.remove()
    shown.querySelector('.ag-head-id')?.remove()
    expect(shown.textContent).not.toContain(t.id)
    expect(head.querySelector('.ag-head-id .tid-text')?.textContent).toBe(t.id)
    expect(head.querySelector<HTMLButtonElement>('.ag-head-id .tid-copy')!.title).toContain(t.id)
  })

  it("links the workflow id to that workflow's page", async () => {
    const head = await header(STEP)
    const link = [...head.querySelectorAll<HTMLAnchorElement>('.ag-head-facts a')].find((a) =>
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

  // THE COMPACT ROW AT EVERY WIDTH (agents.html V1, #503): the step IS the
  // row's name, on line one, at 390 and at 1440 alike; the profile is line
  // two. The wide table's Step column, and the swap at 390 it needed, are gone.
  it('names a workflow row by its step at 390 and at 1440, with the profile on line two', async () => {
    const c = await land([
      listTask('task_aaaaaaaa00000000000a', 'RUNNING', { workflow_id: 'wf_one', step_id: 'scan-01' }),
      listTask('task_bbbbbbbb00000000000b', 'RUNNING', { workflow_id: 'wf_one', step_id: 'scan-04' }),
    ])
    const rows = [...c.querySelectorAll('.row.clickable')]
    const steps = rows.map((r) => r.querySelector('.cr-name b')?.textContent)
    expect(steps.sort()).toEqual(['scan-01', 'scan-04'])
    const row = rows[0]!
    const name = row.querySelector('.cr-name b')!
    const profile = row.querySelector('.cr-sub .cr-profile')!
    expect(profile.textContent).toBe('claude-code')
    for (const env of [PHONE, WIDE]) {
      expect(display(name, env)).not.toBe('none')
      expect(display(profile, env)).not.toBe('none')
    }
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
