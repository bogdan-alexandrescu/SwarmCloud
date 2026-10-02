/**
 * WORKFLOWS V2 (owner's pick, 2026-10-01; workflows.html): the full-width list
 * at /workflows, one workflow's page, Cancel workflow's inline confirm, the
 * Recent (5) switcher's store, polling, and the brand marks on step nodes.
 */
import STYLES from '../styles.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { CancelWorkflowResult, WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskDispatch, TaskState, Workflow, WorkflowStep } from '../types'

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
    expect(q).toEqual({
      wf: null,
      tab: 'graph',
      state: 'failed',
      q: 'core',
      owner: 'priya',
      profile: 'codex',
      sort: 'state',
      stage: null,
      stepState: null,
    })
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
  it('opens on its graph with the step that needs a look in the card under it, the step table below, and records itself in Recent', async () => {
    render(<WorkflowsScreen view="wf=wf_broker&state=running&owner=priya" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    // The back link keeps the filters.
    const back = screen.getByText(/Workflows · Running · owner: priya/)
    expect(back.closest('a')!.getAttribute('href')).toBe('/workflows?state=running&owner=priya')
    // No board controls on a page.
    expect(screen.queryByRole('checkbox', { name: /chains only/i })).toBeNull()
    // The running step is picked into the card, stacked under the graph (#503).
    await waitFor(() => expect(document.querySelector('.wf-split.has-panel.is-stack')).toBeTruthy())
    expect(document.querySelector('.wfp-steps table, .wfp-steps [role="table"], .wfp-steps .wf-table')).toBeTruthy()
    expect(recentWorkflows()[0]).toEqual({ id: 'wf_broker', state: 'RUNNING', name: null })
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
    // A step runs, so the id is typed first (states.html §12; workflow.u2.test.tsx holds the lock).
    fireEvent.change(screen.getByRole('textbox', { name: 'Type wf_broker to confirm' }), { target: { value: 'wf_broker' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the workflow' }))
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
    fireEvent.change(screen.getByRole('textbox', { name: 'Type wf_broker to confirm' }), { target: { value: 'wf_broker' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the workflow' }))
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
    expect(recentWorkflows()[0]).toEqual({ id: 'c', state: 'FAILED', name: null })
  })

  it('still opens the bare ids the first form of the store held, unmarked', () => {
    window.localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify(['wf_old', { id: 'wf_new', state: 'PARKED' }, 7]))
    expect(recentWorkflows()).toEqual([
      { id: 'wf_old', state: null, name: null },
      { id: 'wf_new', state: 'PARKED', name: null },
    ])
  })
})

// ---------------------------------------------------------------------------
// MOVED FROM workflow.board.test.tsx WITH THE REBRAND (2026-10-01). The
// board's own row was removed; what it said about a workflow -- its name, its
// shape, its runners, its spend, a pending cancel, its pull request -- is now
// said by the list row (`WorkflowListRow`) and the page head (`WorkflowHead`),
// and these hold it there.
// ---------------------------------------------------------------------------

/** Serve these workflows and tasks as the board read. */
function serve(workflows: Workflow[], taskById: Map<string, Task> | null = new Map()): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows, taskById, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

/** The list row for `id`, once the list has drawn it. */
async function listRow(id: string): Promise<HTMLElement> {
  render(<WorkflowsScreen />)
  return waitFor(() => {
    const row = document.querySelector<HTMLElement>(`.wfl-table tr[data-workflow="${id}"]`)
    expect(row, `no list row for ${id}`).toBeTruthy()
    return row!
  })
}

/** One workflow's page head, once the page has drawn it. */
async function pageHead(id: string): Promise<HTMLElement> {
  render(<WorkflowsScreen view={`wf=${id}`} />)
  return waitFor(() => {
    const head = document.querySelector<HTMLElement>('.wfp-head')
    expect(head, `no page head for ${id}`).toBeTruthy()
    return head!
  })
}

/** A two-step chain whose step tasks carry the spec's label as `metadata.unit`. */
function labelled(label: string | null): { w: Workflow; tasks: Map<string, Task> } {
  const w = workflow('wf_5e5ad3b6f7da4299a839', 'RUNNING', 'priya', [
    step('implement', [], { task_id: 't_impl' }),
    step('review', ['implement'], { task_id: 't_rev' }),
  ])
  const metadata = label === null ? { origin: 'swarm-mcp' } : { origin: 'swarm-mcp', unit: label }
  const tasks = new Map<string, Task>([
    ['t_impl', task('t_impl', 'SUCCEEDED', { started_at: iso(-300), completed_at: iso(-200), metadata })],
    ['t_rev', task('t_rev', 'RUNNING', { started_at: iso(-100), metadata })],
  ])
  return { w, tasks }
}

/** A result summary whose git outcome names a pull request. */
function withPr(url: string, number = 412): Record<string, unknown> {
  return { git: { pull_request: { number, url, state: 'open', created: true } } }
}

const dispatchAs = (strategy: TaskDispatch['strategy'], role: TaskDispatch['role']): TaskDispatch => ({
  strategy,
  carrier: 'checkpoints',
  role,
  integrates: [],
})

describe('#330: the list row leads with the label', () => {
  // Was 'leads with the spec label and puts the wf_ id on a second line' on the board row.
  it('leads with the spec label and puts the wf_ id on a second line', async () => {
    const { w, tasks } = labelled('workflows-board')
    serve([w], tasks)
    const name = (await listRow(w.workflow_id)).querySelector<HTMLElement>('.wfl-name')!
    expect(name.querySelector('a')!.textContent).toBe('workflows-board')
    // The id is still the `.id`, whole in its title.
    const id = name.querySelector<HTMLElement>('.id')
    expect(id, 'the label hid the id').toBeTruthy()
    expect(id!.textContent).toBe('wf_5e5ad3b6f7da4299a839')
    expect(id!.getAttribute('title')).toBe('wf_5e5ad3b6f7da4299a839')
  })

  // Was 'falls back to the id alone when there is no label, or no task was read'.
  for (const [what, tasks] of [
    ['no unit in the metadata', labelled(null).tasks],
    ['a blank unit', labelled('   ').tasks],
    ['no task read', null],
  ] as const) {
    it(`falls back to the id alone: ${what}`, async () => {
      serve([labelled(null).w], tasks)
      const name = (await listRow('wf_5e5ad3b6f7da4299a839')).querySelector<HTMLElement>('.wfl-name')!
      expect(name.querySelector('a')!.textContent).toBe('wf_5e5ad3b6f7da4299a839')
      expect(name.querySelector('.id'), 'the id is printed twice').toBeNull()
    })
  }
})

describe('#330: the page head links the pull request the run opened', () => {
  async function head(over: Partial<Task>, other: Partial<Task> = {}): Promise<HTMLElement> {
    const w = workflow('wf_pr', 'SUCCEEDED', 'priya', [step('a', [], { task_id: 't_a' }), step('b', ['a'], { task_id: 't_b' })])
    serve(
      [w],
      new Map<string, Task>([
        ['t_a', task('t_a', 'SUCCEEDED', other)],
        ['t_b', task('t_b', 'SUCCEEDED', over)],
      ]),
    )
    return pageHead('wf_pr')
  }
  const prChip = (h: HTMLElement) => [...h.querySelectorAll<HTMLElement>('.wfp-chip')].find((c) => /^PR #/.test(c.textContent ?? ''))

  it('shows PR #N, linked, for the integrator’s pull request', async () => {
    const h = await head({ dispatch: dispatchAs('integrate', 'integrator'), result_summary: withPr('https://github.com/o/r/pull/412') })
    const pr = prChip(h)
    expect(pr, 'the head does not link the pull request').toBeTruthy()
    expect(pr!.tagName).toBe('A')
    expect(pr!.textContent).toBe('PR #412')
    expect(pr!.getAttribute('href')).toBe('https://github.com/o/r/pull/412')
    expect(pr!.getAttribute('rel')).toContain('noreferrer')
    // The number alone: the recorded state is a creation-time word.
    expect(pr!.textContent).not.toMatch(/open|merged/)
  })

  it('does the same for a direct-pr step', async () => {
    const h = await head({ dispatch: dispatchAs('direct-pr', null), result_summary: withPr('http://forge.example/pr/7', 7) })
    expect(prChip(h)!.textContent).toBe('PR #7')
  })

  it('prints the number without a link when the URL is not http(s)', async () => {
    const h = await head({ dispatch: dispatchAs('direct-pr', null), result_summary: withPr('javascript:alert(1)', 9) })
    const pr = prChip(h)
    expect(pr, 'a PR with a non-http URL lost its number too').toBeTruthy()
    expect(pr!.tagName).not.toBe('A')
    expect(pr!.textContent).toBe('PR #9')
    expect(h.querySelector('.wfp-chips a')).toBeNull()
  })

  for (const [what, over] of [
    ['a contributor', { dispatch: dispatchAs('integrate', 'contributor'), result_summary: withPr('https://x/pull/1') }],
    ['a collect step', { dispatch: dispatchAs('collect', null), result_summary: withPr('https://x/pull/1') }],
    ['no pull request', { dispatch: dispatchAs('direct-pr', null), result_summary: { git: {} } }],
  ] as const) {
    it(`names no PR from ${what}`, async () => {
      const h = await head(over as Partial<Task>)
      expect(prChip(h), what).toBeUndefined()
    })
  }
})

describe('WF-15: a cancel is said to be requested only while the workflow has not ended', () => {
  const live = () => ({ ...workflow('wf_live', 'RUNNING', 'priya', [step('a', [])]), cancel_requested: true })
  // Finished cancelled a day ago; the flag is never cleared.
  const over = () => ({ ...workflow('wf_over', 'CANCELLED', 'priya', [step('a', [])]), cancel_requested: true })
  // A rollup that could not read every step has not shown it is over.
  const unread = (): Workflow => {
    const w = workflow('wf_unread', 'CANCELLED', 'priya', [step('a', [])])
    return { ...w, cancel_requested: true, rollup: { ...w.rollup!, complete: false } }
  }
  const tag = (root: Element) => root.querySelector('.tag.wait')?.textContent ?? ''

  it('on the list row', async () => {
    serve([live(), over(), unread()])
    expect(tag(await listRow('wf_live'))).toBe('cancel requested')
    expect(tag(document.querySelector('tr[data-workflow="wf_over"]')!), 'a finished workflow still reads "cancel requested"').toBe('')
    expect(tag(document.querySelector('tr[data-workflow="wf_unread"]')!)).toBe('cancel requested')
  })

  it('on the page head', async () => {
    serve([live(), over()])
    expect(tag(await pageHead('wf_over')), 'a finished workflow still reads "cancel requested"').toBe('')
  })

  it('on the page head of a live workflow', async () => {
    serve([live()])
    expect(tag(await pageHead('wf_live'))).toBe('cancel requested')
  })
})

describe('the list row: shape, runners and spend', () => {
  // Was 'distinguishes a fan-out from a chain' / 'names the shape in words a reader can hover'.
  it('tells a fan-out from a chain, in its widths and in words a reader can hover', async () => {
    const chain = workflow('wf_chain', 'RUNNING', 'priya', [step('a', []), step('b', ['a']), step('c', ['b'])])
    const fan = workflow('wf_fan', 'RUNNING', 'priya', [
      step('root', []),
      ...['p1', 'p2', 'p3', 'p4', 'p5'].map((id) => step(id, ['root'])),
      step('join', ['p1', 'p2', 'p3', 'p4', 'p5']),
    ])
    serve([chain, fan])
    const chainShape = (await listRow('wf_chain')).querySelector<HTMLElement>('.wf-shape')!
    const fanShape = document.querySelector<HTMLElement>('tr[data-workflow="wf_fan"] .wf-shape')!
    expect(chainShape.textContent).toContain('1 → 1 → 1')
    expect(fanShape.textContent).toContain('1 → 5 → 1')
    expect(chainShape.getAttribute('title')).toMatch(/chain/i)
    const fanTitle = fanShape.getAttribute('title') ?? ''
    expect(fanTitle).toContain('parallel')
    expect(fanTitle).toContain('converge')
  })

  // Was WF-16 'folds whole runner chips into the count rather than letting the column cut one'.
  it('WF-16: folds whole runner chips into the count rather than letting the column cut one', async () => {
    const steps = [
      ...Array.from({ length: 20 }, (_, i) => step(`cc-${i}`, [], { runner_profile: 'claude-code' })),
      ...Array.from({ length: 6 }, (_, i) => step(`br-${i}`, [], { runner_profile: 'browser' })),
      ...Array.from({ length: 4 }, (_, i) => step(`mk-${i}`, [], { runner_profile: 'mock' })),
    ]
    serve([workflow('wf_mix', 'RUNNING', 'priya', steps)])
    const chips = async (room: number) => {
      // THE COLUMN'S WIDTH, stubbed, because jsdom has no layout.
      const spy = vi.spyOn(Element.prototype, 'clientWidth', 'get').mockImplementation(function (this: Element) {
        return this.classList.contains('wf-mix') ? room : 0
      })
      const { unmount } = render(<WorkflowsScreen />)
      const mix = await waitFor(() => {
        const m = document.querySelector('tr[data-workflow="wf_mix"] .wf-mix')
        expect(m).toBeTruthy()
        return m!
      })
      const named = [...mix.querySelectorAll('.wf-chip:not(.more)')].map((c) => c.firstChild?.textContent)
      const more = mix.querySelector('.wf-chip.more')?.textContent ?? null
      const title = mix.getAttribute('title')
      unmount()
      spy.mockRestore()
      return { named, more, title }
    }
    // 160px: `claude-code ×20` and `+2` fit; a second whole chip does not.
    const narrow = await chips(160)
    expect(narrow.named, 'a chip the column cannot hold was drawn anyway').toEqual(['claude-code'])
    expect(narrow.more).toBe('+2')
    // Room for two: two named and the third counted.
    expect(await chips(400)).toMatchObject({ named: ['claude-code', 'browser'], more: '+1' })
    // Every profile stays reachable whatever was folded.
    expect(narrow.title).toBe('Runner profiles: claude-code ×20, browser ×6, mock ×4')
  })

  /** One finished workflow whose steps reported these costs (null: none reported). */
  async function spendCell(costs: (number | null)[]): Promise<HTMLElement> {
    const steps = costs.map((_, i) => step(`s${i}`, i === 0 ? [] : ['s0'], { task_id: `t${i}` }))
    const tasks = new Map<string, Task>(
      costs.map((c, i) => [
        `t${i}`,
        task(`t${i}`, 'SUCCEEDED', { result_summary: c === null ? {} : { runner: { usage: { total_cost_usd: c } } } }),
      ]),
    )
    serve([workflow('wf_spend', 'SUCCEEDED', 'priya', steps)], tasks)
    return (await listRow('wf_spend')).querySelector<HTMLElement>('.wf-spend')!
  }

  // Was 'what a workflow has cost' on the board row.
  it('says "not reported" when nothing reported one, and prints no zero', async () => {
    const cell = await spendCell([null, null])
    expect(cell.textContent).toBe('not reported')
    expect(cell.textContent, 'an unreported cost rendered a figure').not.toMatch(/\d/)
    expect(cell.className).toContain('absent')
    expect(cell.getAttribute('title')).toContain('not $0.00')
  })

  it('prints a REPORTED zero as a number, because that one was measured', async () => {
    const cell = await spendCell([0, 0])
    expect(cell.textContent).toContain('$0.0000')
    expect(cell.className).not.toContain('absent')
  })

  // Was the board's 'draws an absent figure differently from a measured one'.
  it('draws an absent spend differently from a measured one, in the shipped stylesheet', async () => {
    const style = document.createElement('style')
    style.textContent = STYLES
    document.head.appendChild(style)
    const absentCell = await spendCell([null, null])
    const absent = { italic: getComputedStyle(absentCell).fontStyle, color: getComputedStyle(absentCell).color }
    cleanup()
    const measuredCell = await spendCell([0, 0])
    expect(absent.italic).toBe('italic')
    expect(getComputedStyle(measuredCell).fontStyle).toBe('normal')
    expect(absent.color).not.toBe(getComputedStyle(measuredCell).color)
    style.remove()
  })

  it('never shows a partial total without its coverage', async () => {
    const cell = await spendCell([0.0642, null, null])
    expect(cell.textContent).toContain('$0.0642')
    expect(cell.querySelector('.wf-spend-cov')!.textContent).toBe('1/3')
    expect(cell.getAttribute('title')).toContain('floor rather than the total')
  })
})
