/**
 * WORKFLOWS V2 (owner's pick, 2026-10-01; workflows.html): the full-width list
 * at /workflows, one workflow's page, Cancel workflow's inline confirm, the
 * Recent (5) switcher's store, polling, and the brand marks on step nodes.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { CancelWorkflowResult, WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { CancelWorkflow, WorkflowsScreen, WORKFLOW_POLL_MS } = await import('../Workflows')
const { stageCensus, stepLook } = await import('../dag')
const { RECENT_WORKFLOWS_KEY, recentWorkflows, rememberWorkflow } = await import('../Spine')
const wl = await import('../workflowlist')
const { addressToPath, pathToAddress } = await import('../paths')

const T0 = Date.parse('2026-10-01T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'priya@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

function workflow(id: string, state: TaskState, owner: string, steps: WorkflowStep[], ageSeconds = 600): Workflow {
  return {
    workflow_id: id,
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: {
      state,
      complete: true,
      reason: 'test',
      counts: {},
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: owner,
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

/** Three workflows: priya's running one, alex's failed one, priya's finished one. */
function fixture(): WorkflowBoard {
  const running = workflow('wf_broker', 'RUNNING', 'priya', [
    step('plan', [], { task_id: 't_plan' }),
    step('core', ['plan'], { task_id: 't_core', runner_profile: 'codex' }),
    step('verify', ['core']),
  ])
  const failed = workflow('wf_bumps', 'FAILED', 'alex', [
    step('scan', [], { task_id: 't_scan' }),
    step('rewrite-b', ['scan'], { task_id: 't_rw' }),
  ])
  const done = workflow('wf_lint', 'SUCCEEDED', 'priya', [step('lint', [], { task_id: 't_lint' })], 7200)
  const tasks = new Map<string, Task>([
    ['t_plan', task('t_plan', 'SUCCEEDED')],
    ['t_core', task('t_core', 'RUNNING', { started_at: iso(-300) })],
    ['t_scan', task('t_scan', 'SUCCEEDED')],
    ['t_rw', task('t_rw', 'FAILED', { last_error: 'exit 1: 4 tests failing' })],
    ['t_lint', task('t_lint', 'SUCCEEDED')],
  ])
  return { workflows: [running, failed, done], taskById: tasks, statesDetail: null }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: fixture(), fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadWorkflow.mockResolvedValue({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
  })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused; the store's own try/catch is what is under test elsewhere */
  }
})

/** The screen as App mounts it: its address is a prop, and every change goes back through `onView`. */
function Routed({ initial, onView }: { initial: string; onView: (v: string) => void }) {
  const [view, setView] = useState(initial)
  return (
    <WorkflowsScreen
      view={view}
      onView={(v) => {
        onView(v)
        setView(v)
      }}
    />
  )
}

function rowIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>('.wfl-table tbody tr')].map((r) => r.dataset.workflow ?? '')
}

// ---------------------------------------------------------------------------
// The address
// ---------------------------------------------------------------------------

describe('the list address', () => {
  it('parses and writes one canonical query, and drops what it does not know', () => {
    const q = wl.parseWorkflowQuery('owner=priya&state=failed&q=core&profile=codex&junk=1')
    expect(q).toEqual({ wf: null, tab: 'graph', state: 'failed', q: 'core', owner: 'priya', profile: 'codex' })
    expect(wl.workflowQueryString(q)).toBe('state=failed&q=core&owner=priya&profile=codex')
    expect(wl.parseWorkflowQuery('state=bogus&tab=table').state).toBe('all')
    // A tab means nothing on the list, so it is not written there.
    expect(wl.workflowQueryString(wl.parseWorkflowQuery('tab=table'))).toBe('')
    expect(wl.workflowQueryString(wl.EMPTY_QUERY)).toBe('')
  })

  it('round-trips a workflow, its tab and the list filters through the path', () => {
    const address = `work/workflows?${wl.workflowQueryString({ ...wl.EMPTY_QUERY, wf: 'wf_broker', tab: 'timeline', state: 'failed', owner: 'priya' })}`
    const path = addressToPath(address)
    expect(path).toBe('/workflows/wf_broker/timeline?state=failed&owner=priya')
    const u = new URL(path, 'https://x.test')
    expect(pathToAddress(u.pathname, u.search)?.address).toBe(address)
    // The Graph tab is the bare page.
    expect(addressToPath('work/workflows?wf=wf_broker&state=running')).toBe('/workflows/wf_broker?state=running')
    expect(pathToAddress('/workflows/wf_broker/table')?.address).toBe('work/workflows?wf=wf_broker&tab=table')
  })

  it('links a row to the workflow carrying the filters, and the back link to the filtered list', () => {
    const q = { ...wl.EMPTY_QUERY, state: 'running' as const, owner: 'priya' }
    expect(wl.workflowHref('wf_broker', q)).toBe('/workflows/wf_broker?state=running&owner=priya')
    expect(wl.listHref(q)).toBe('/workflows?state=running&owner=priya')
    expect(wl.backLabel(q)).toBe('Workflows · Running · owner: priya')
  })

  it('puts every workflow in exactly one bucket, and an underived one in Running, never Finished', () => {
    const b = fixture()
    expect(b.workflows.map(wl.bucketOf)).toEqual(['running', 'failed', 'finished'])
    const unread: Workflow = { ...b.workflows[2]!, rollup: { ...b.workflows[2]!.rollup!, complete: false } }
    expect(wl.bucketOf(unread)).toBe('running')
    const cancelled = workflow('wf_c', 'CANCELLED', 'sam', [])
    expect(wl.bucketOf(cancelled)).toBe('finished')
  })
})

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

describe('the workflow list', () => {
  it('reads its filters from the address and draws only what they leave', async () => {
    render(<WorkflowsScreen view="state=running&owner=priya" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_broker']))
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    const pressed = within(seg).getByRole('button', { pressed: true })
    expect(pressed.textContent).toBe('Running1')
    // The counts follow the other filters: priya has no failed workflow.
    const failedSeg = within(seg).getAllByRole('button').find((b) => b.textContent?.startsWith('Failed'))!
    expect(failedSeg.textContent).toBe('Failed0')
    expect((screen.getByRole('combobox', { name: /owner/ }) as HTMLSelectElement).value).toBe('priya')
  })

  it('writes every filter change back to the address, and a new address redraws the list', async () => {
    const onView = vi.fn()
    render(<Routed initial="" onView={onView} />)
    await waitFor(() => expect(rowIds()).toHaveLength(3))

    fireEvent.click(within(screen.getByRole('group', { name: 'Which workflows' })).getByText('Failed'))
    expect(onView).toHaveBeenLastCalledWith('state=failed')
    await waitFor(() => expect(rowIds()).toEqual(['wf_bumps']))

    fireEvent.change(screen.getByRole('combobox', { name: /owner/ }), { target: { value: 'alex' } })
    expect(onView).toHaveBeenLastCalledWith('state=failed&owner=alex')

    fireEvent.change(screen.getByRole('combobox', { name: /profile/ }), { target: { value: 'claude-code' } })
    expect(onView).toHaveBeenLastCalledWith('state=failed&owner=alex&profile=claude-code')

    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a workflow or step' }), { target: { value: 'rewrite' } })
    expect(onView).toHaveBeenLastCalledWith('state=failed&q=rewrite&owner=alex&profile=claude-code')
    await waitFor(() => expect(rowIds()).toEqual(['wf_bumps']))

    // A search for a step only one workflow has finds that workflow.
    fireEvent.click(within(screen.getByRole('group', { name: 'Which workflows' })).getByText('All'))
    fireEvent.change(screen.getByRole('combobox', { name: /owner/ }), { target: { value: '' } })
    fireEvent.change(screen.getByRole('combobox', { name: /profile/ }), { target: { value: '' } })
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a workflow or step' }), { target: { value: 'core' } })
    expect(onView).toHaveBeenLastCalledWith('q=core')
    await waitFor(() => expect(rowIds()).toEqual(['wf_broker']))
  })

  it('names the failing step and why on a failed row, and links each row with the filters', async () => {
    render(<WorkflowsScreen view="state=failed" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_bumps']))
    const row = document.querySelector<HTMLElement>('tr[data-workflow="wf_bumps"]')!
    expect(row.className).toContain('is-failed')
    const why = row.querySelector('.wfl-why.is-bad')!
    expect(why.textContent).toBe('failed at rewrite-b: exit 1')
    expect(row.querySelector('[data-mark="failed"][data-hue="bad"]')).toBeTruthy()
    expect(row.querySelector('.wfl-name > a')!.getAttribute('href')).toBe('/workflows/wf_bumps?state=failed')
  })

  it('re-reads every 10s while a workflow runs, and not once none does', () => {
    const b = fixture()
    expect(WORKFLOW_POLL_MS).toBe(10_000)
    expect(wl.anyRunning(b.workflows)).toBe(true)
    expect(wl.anyRunning(b.workflows.filter((w) => wl.bucketOf(w) !== 'running'))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// One workflow
// ---------------------------------------------------------------------------

describe('one workflow', () => {
  it('opens on its graph beside the step that needs a look, with the step table under it, and records itself in Recent', async () => {
    render(<WorkflowsScreen view="wf=wf_broker&state=running&owner=priya" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    // The back link keeps the filters.
    const back = screen.getByText(/Workflows · Running · owner: priya/)
    expect(back.closest('a')!.getAttribute('href')).toBe('/workflows?state=running&owner=priya')
    // No board controls on a page.
    expect(screen.queryByRole('checkbox', { name: /chains only/i })).toBeNull()
    // The running step is picked into the card beside the graph.
    await waitFor(() => expect(document.querySelector('.wf-split.has-panel')).toBeTruthy())
    expect(document.querySelector('.wfp-steps table, .wfp-steps [role="table"], .wfp-steps .wf-table')).toBeTruthy()
    expect(recentWorkflows()[0]).toEqual({ id: 'wf_broker', state: 'RUNNING' })
  })

  it('draws each node in its brand mark and tint', async () => {
    render(<WorkflowsScreen view="wf=wf_broker" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    const node = (id: string) => document.querySelector<HTMLElement>(`button.node[data-step="${id}"]`)!
    expect(node('core').className).toContain('t-live')
    expect(node('core').querySelector('[data-mark="running"][data-hue="live"]')).toBeTruthy()
    expect(node('plan').querySelector('[data-mark="succeeded"][data-hue="neu"]')).toBeTruthy()
    // Not started is the grey ring: in line, nothing decided.
    expect(node('verify').querySelector('[data-mark="queued"][data-hue="neu"]')).toBeTruthy()
  })

  it('switches tabs through the address', async () => {
    const onView = vi.fn()
    render(<Routed initial="wf=wf_broker&owner=priya" onView={onView} />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    const tab = screen.getAllByRole('button').find((b) => b.textContent === 'Timeline')
    expect(tab, 'no Timeline tab').toBeTruthy()
    fireEvent.click(tab!)
    expect(onView).toHaveBeenLastCalledWith('wf=wf_broker&tab=timeline&owner=priya')
  })
})

describe('stage census and step looks', () => {
  it('maps every step state to exactly one brand mark and hue, and an unread step to amber', () => {
    const b = fixture()
    const steps = b.workflows[0]!.steps
    const census = stageCensus(steps, b.taskById)
    const looks = Object.fromEntries(census.counts.map((c) => [c.word, c.look]))
    expect(looks['running']).toEqual({ kind: 'state', mark: 'running', hue: 'live' })
    expect(looks['succeeded']).toEqual({ kind: 'state', mark: 'succeeded', hue: 'neu' })
    expect(looks['not started']).toEqual({ kind: 'state', mark: 'queued', hue: 'neu' })
    expect(stepLook({ kind: 'unknown', taskId: 't' })).toEqual({ kind: 'unknown' })
  })
})

// ---------------------------------------------------------------------------
// Cancel workflow
// ---------------------------------------------------------------------------

describe('Cancel workflow', () => {
  const ok: Result<CancelWorkflowResult> = {
    status: 'ok',
    fetchedAt: T0,
    data: { workflow_id: 'wf_broker', tasks_cancelled: ['t_core'], tasks_already_terminal: ['t_plan'] },
  }

  it('asks first, and sends nothing on the first click', () => {
    const cancel = vi.fn().mockResolvedValue(ok)
    const b = fixture()
    render(<CancelWorkflow workflow={b.workflows[0]!} taskById={b.taskById} onDone={vi.fn()} cancel={cancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    expect(cancel).not.toHaveBeenCalled()
    const ask = screen.getByRole('group', { name: 'Confirm cancelling this workflow' })
    // It names what it cancels and what keeps running.
    expect(ask.textContent).toContain('1 step not started will never start')
    expect(ask.textContent).toContain('1 running step is asked to stop and keeps its slot until its worker exits')
    expect(ask.textContent).toContain('Steps that finished keep their results.')
  })

  it('calls cancelWorkflow on confirm, then re-reads', async () => {
    const cancel = vi.fn().mockResolvedValue(ok)
    const onDone = vi.fn()
    const b = fixture()
    render(<CancelWorkflow workflow={b.workflows[0]!} taskById={b.taskById} onDone={onDone} cancel={cancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    fireEvent.click(screen.getByRole('button', { name: 'Yes, cancel the workflow' }))
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(cancel).toHaveBeenCalledWith('wf_broker')
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
  })

  it('sends nothing when dismissed, and asks again next time', () => {
    const cancel = vi.fn().mockResolvedValue(ok)
    const b = fixture()
    render(<CancelWorkflow workflow={b.workflows[0]!} taskById={b.taskById} onDone={vi.fn()} cancel={cancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    fireEvent.click(screen.getByRole('button', { name: 'Keep it running' }))
    expect(cancel).not.toHaveBeenCalled()
    expect(screen.queryByRole('group', { name: 'Confirm cancelling this workflow' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel workflow' })).toBeTruthy()
  })

  it('says why when the cancel was not recorded, and does not re-read', async () => {
    const cancel = vi.fn().mockResolvedValue({
      status: 'error',
      error: { kind: 'conflict', httpStatus: 409, code: null, message: 'not a member' },
    })
    const onDone = vi.fn()
    const b = fixture()
    render(<CancelWorkflow workflow={b.workflows[0]!} taskById={b.taskById} onDone={onDone} cancel={cancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    fireEvent.click(screen.getByRole('button', { name: 'Yes, cancel the workflow' }))
    expect((await screen.findByRole('alert')).textContent).toContain('not a member')
    expect(onDone).not.toHaveBeenCalled()
  })

  it('is not offered on a workflow that has ended', () => {
    const b = fixture()
    const { container } = render(
      <CancelWorkflow workflow={b.workflows[2]!} taskById={b.taskById} onDone={vi.fn()} cancel={vi.fn()} />,
    )
    expect(container.textContent).toBe('')
  })
})

// ---------------------------------------------------------------------------
// Recent (5)
// ---------------------------------------------------------------------------

describe('the Recent (5) store', () => {
  it('keeps the five most recently opened, newest first, with their states', () => {
    for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) rememberWorkflow(id, 'RUNNING')
    rememberWorkflow('c', 'FAILED')
    expect(recentWorkflows().map((w) => w.id)).toEqual(['c', 'f', 'e', 'd', 'b'])
    expect(recentWorkflows()[0]).toEqual({ id: 'c', state: 'FAILED' })
  })

  it('still opens the bare ids the first form of the store held, unmarked', () => {
    window.localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify(['wf_old', { id: 'wf_new', state: 'PARKED' }, 7]))
    expect(recentWorkflows()).toEqual([
      { id: 'wf_old', state: null },
      { id: 'wf_new', state: 'PARKED' },
    ])
  })
})
