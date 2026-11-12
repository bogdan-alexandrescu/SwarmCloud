// HOME'S YOUR WORK AS TWO LISTS, AGENTS AND WORKFLOWS (the owner's pick of the
// "Two lists" mock-up, 11 November 2026). What each case holds:
//   * an agent without a browser draws a terminal icon and no thumbnail;
//   * a running browser agent draws one tiny thumbnail, paced at about a
//     second (`HOME_THUMB_PACE_MS`, passed to LiveThumb as its own pace);
//   * a parked agent asking a question sorts first and is answered in place,
//     posted to the asking task as Needs you posts it;
//   * a workflow row's chevron opens its steps and which agent is where, with
//     Open workflow and its pull request as buttons;
//   * six agents and four workflows, then a real "Show N more" button;
//   * on a 390 phone the lists stack and nothing is wider than the viewport.
//
// MUTATIONS: drop `showsLiveThumb` from the row's end (the terminal agent
// draws a thumbnail); pass no `paceMs` (the pace reads 0); rank browser agents
// above asking ones (the parked agent is no longer first); drop AGENT_CAP's
// slice (eight rows, no Show more); drop the phone rule (two columns at 390).
//
// THE OWNER'S PICKS ON PR #3901: a workflow an open approval names says
// Review, a link to that approval; its pull request carries the checks dot the
// workflow read serves (`workflow.pull_request`), and only for the same number.
// MUTATIONS: match approvals on `subject.run_id` (no Review); drop the
// number check in `rowPull` (another pull request's reading is drawn).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import WORKLISTS_CSS from '../styles/home-worklists.css?raw'
import type { Result } from '../fetch'
import type { Task, TaskState } from '../types'
import { painted } from './marks'
import { measure } from './proseapp'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadApprovals: vi.fn(),
  loadPeople: vi.fn(),
  loadProfile: vi.fn(),
  loadStats: vi.fn(),
  loadOnboarding: vi.fn(),
  loadLiveSession: vi.fn(),
  answerQuestion: vi.fn(),
  loadWorkflowsById: vi.fn(),
}))
/** Each LiveThumb the page draws, by its props: the live socket is LiveThumb's own tests'. */
const thumbs = vi.hoisted(() => [] as { taskId: string; paceMs?: number }[])

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})
vi.mock('../LiveThumb', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../LiveThumb')>()
  return {
    ...actual,
    LiveThumb: (p: { taskId: string; paceMs?: number }) => {
      thumbs.push(p)
      return <canvas data-testid="live-thumb" data-task={p.taskId} />
    },
  }
})

import { HomeScreen } from '../Home'
import { AGENT_CAP, HOME_THUMB_PACE_MS, WORKFLOW_CAP } from '../HomeWorkLists'
import { addressToPath } from '../paths'
import { approvalAddress } from '../Approvals'
import type { WorkflowPullRequestChecks } from '../types'

const NOW = Date.parse('2026-11-11T12:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: NOW })

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-ada',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 60_000),
    started_at: iso(NOW - 3_000_000),
    completed_at: null,
    submitted_by: 'ada@example.test',
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
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: {},
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

const terminal = task('task_term1', 'RUNNING', { metadata: { title: 'Port the parser' }, started_at: iso(NOW - 60_000) })
const browser = task('task_brow1', 'RUNNING', { runner_profile: 'claude-code-browser', metadata: { title: 'Check the dashboard' }, started_at: iso(NOW - 600_000) })
/** A merge parked on one owner question its review named, oldest of all. */
const asking = task('task_merge1', 'PARKED', {
  runner_profile: 'merge',
  park_reason: 'HUMAN_REQUIRED',
  started_at: iso(NOW - 7_000_000),
  workflow_id: 'wf_one',
  step_id: 'merge',
  metadata: { title: 'Merge the login fix' },
  blocked_by: [
    {
      reason: 'HUMAN_REQUIRED',
      park_phase: 'owner_questions',
      owner_questions: { questions: [{ task_id: 'task_review1', step_id: 'review', question_index: 0, question: 'Keep the old flag?' }], unreadable: 0 },
    },
  ] as unknown as Task['blocked_by'],
})

function workflowRead(id: string, steps: { step: string; task: Task | null }[]) {
  return {
    workflow: {
      workflow_id: id,
      state: 'RUNNING',
      tenant_id: 'u-ada',
      stored_state: 'RUNNING',
      state_source: 'stored',
      created_at: iso(NOW - 4_000_000),
      updated_at: iso(NOW - 60_000),
      submitted_by: null,
      priority: 5,
      on_step_failure: 'stop',
      cancel_requested: false,
      title: `Workflow ${id}`,
      steps: steps.map((s) => ({ step_id: s.step, runner_profile: s.task?.runner_profile ?? 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: s.task?.id ?? null })),
    },
    tasks: steps.map((s) => s.task).filter((t): t is Task => t !== null),
  }
}

function serve(tasks: Task[], reads: ReturnType<typeof workflowRead>[] = []) {
  api.loadTasks.mockResolvedValue(ok({ tasks }))
  api.loadApprovals.mockResolvedValue(ok({ approvals: [] }))
  api.loadPeople.mockResolvedValue({ status: 'error', error: { kind: 'forbidden', httpStatus: 403, code: null, message: 'no' } })
  api.loadProfile.mockResolvedValue(ok({ identity: { email: 'ada@example.test', display_name: 'Ada', platform_role: null, is_admin: false }, workspaces: [] }))
  api.loadStats.mockResolvedValue(ok({ tasks_by_state: {} }))
  api.loadOnboarding.mockResolvedValue({ status: 'empty', fetchedAt: NOW })
  api.loadLiveSession.mockResolvedValue({ status: 'empty', fetchedAt: NOW })
  api.loadWorkflowsById.mockResolvedValue(ok({ batched: true, reads: new Map(reads.map((r) => [r.workflow.workflow_id, r])), missing: [] }))
}

const work = () => document.getElementById('home-work') as HTMLElement
const agents = () => within(work()).getByRole('region', { name: 'Agents' })
const workflows = () => within(work()).getByRole('region', { name: 'Workflows' })
const rowOf = (id: string) => work().querySelector(`li[data-task="${id}"]`) as HTMLElement

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  thumbs.length = 0
})
afterEach(cleanup)

describe('Your work: an Agents list and a Workflows list', () => {
  it('an agent without a browser draws a terminal icon and no thumbnail', async () => {
    serve([terminal])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(rowOf('task_term1')).toBeTruthy())
    const row = rowOf('task_term1')
    expect(row.querySelector('[data-kind="terminal"]')?.getAttribute('aria-label')).toBe('No browser')
    expect(row.querySelector('[data-kind="browser"]')).toBeNull()
    expect(within(row).queryByTestId('live-thumb')).toBeNull()
    expect(thumbs).toEqual([])
    // The state's word is the dot's tooltip, not a word on the surface.
    expect(within(row).getByRole('img', { name: 'Running' })).toBeTruthy()
  })

  it('a running browser agent draws one tiny thumbnail, paced at about a second', async () => {
    serve([terminal, browser])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(rowOf('task_brow1')).toBeTruthy())
    const row = rowOf('task_brow1')
    expect(row.querySelector('[data-kind="browser"]')?.getAttribute('aria-label')).toBe('Browser agent')
    expect(within(work()).getAllByTestId('live-thumb')).toHaveLength(1)
    expect(within(row).getByTestId('live-thumb').getAttribute('data-task')).toBe('task_brow1')
    expect(HOME_THUMB_PACE_MS).toBe(1000)
    expect(new Set(thumbs.map((p) => p.paceMs))).toEqual(new Set([HOME_THUMB_PACE_MS]))
    // The thumbnail opens the live browser; the old big cards are gone.
    expect(row.querySelector('a.wl-thumb')?.getAttribute('aria-label')).toBe('Live browser of Check the dashboard')
    expect(work().querySelector('.hm-workcard')).toBeNull()
    // Focused, it offers both ways in as buttons; there is no larger picture.
    fireEvent.focus(row.querySelector('a.wl-thumb')!)
    await waitFor(() => expect(within(row).getByRole('link', { name: 'Watch live' }).className).toContain('c-btn'))
    expect(within(row).getByRole('link', { name: 'Agent page' })).toBeTruthy()
    expect(within(work()).getAllByTestId('live-thumb')).toHaveLength(1)
  })

  it('a parked agent asking a question sorts first and is answered in place', async () => {
    serve([terminal, browser, asking], [workflowRead('wf_one', [{ step: 'build', task: null }, { step: 'merge', task: asking }])])
    api.answerQuestion.mockResolvedValue(ok({}))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(rowOf('task_merge1')).toBeTruthy())
    const order = [...agents().querySelectorAll('li[data-task]')].map((r) => r.getAttribute('data-task'))
    expect(order).toEqual(['task_merge1', 'task_brow1', 'task_term1'])
    const row = rowOf('task_merge1')
    expect(row.textContent).toContain('Asks you')
    expect(row.textContent).toContain('Keep the old flag?')
    // Only what asks shows a word: one Answer, on the asking row.
    expect(within(agents()).getAllByRole('button', { name: 'Answer' })).toHaveLength(1)
    fireEvent.click(within(row).getByRole('button', { name: 'Answer' }))
    fireEvent.change(within(row).getByLabelText('Your answer'), { target: { value: 'Keep it' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledWith('task_review1', { question_index: 0, choice: null, text: 'Keep it' }))
    await waitFor(() => expect(within(agents()).queryByRole('button', { name: 'Answer' })).toBeNull())
  })

  it('a workflow row opens to its steps, which of its agents is where, and its buttons', async () => {
    const step = task('task_step1', 'RUNNING', { workflow_id: 'wf_one', step_id: 'build', metadata: { title: 'Build the fix' } })
    const planned = task('task_plan1', 'SUCCEEDED', { workflow_id: 'wf_one', step_id: 'plan', completed_at: iso(NOW - 3_000_000) })
    const read = workflowRead('wf_one', [
      { step: 'plan', task: planned },
      { step: 'build', task: step },
      { step: 'merge', task: null },
    ])
    serve([step], [read])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(within(workflows()).getByText('Workflow wf_one')).toBeTruthy())
    expect(api.loadWorkflowsById).toHaveBeenCalledWith(['wf_one'])
    const wf = work().querySelector('[data-workflow="wf_one"]') as HTMLElement
    expect(within(wf).getByRole('img', { name: 'Step 2 of 3: build' })).toBeTruthy()
    expect(wf.querySelectorAll('.wl-segs > i')).toHaveLength(3)
    expect(within(wf).queryByRole('link', { name: 'Open workflow' })).toBeNull()
    const chevron = within(wf).getByRole('button', { name: 'Open Workflow wf_one' })
    fireEvent.click(chevron)
    expect(chevron.getAttribute('aria-expanded')).toBe('true')
    const steps = [...wf.querySelectorAll('.wl-stagelist > li')]
    expect(steps.map((s) => s.querySelector('.wl-stage-title')?.textContent)).toEqual(['plan', 'build', 'merge'])
    expect(steps.map((s) => s.className)).toEqual(['is-done', 'is-now', 'is-todo'])
    expect(within(steps[1] as HTMLElement).getByRole('link', { name: 'Build the fix' })).toBeTruthy()
    expect(within(wf).getByRole('link', { name: 'Open workflow' }).className).toContain('c-btn')
    // The agent's own row names its workflow as a link.
    expect(within(rowOf('task_step1')).getByRole('link', { name: 'Workflow wf_one' })).toBeTruthy()
  })

  it('caps the lists at six agents and four workflows, then a real Show more button', async () => {
    const many = Array.from({ length: AGENT_CAP + 2 }, (_, i) =>
      task(`task_many${i}`, 'RUNNING', { workflow_id: i < WORKFLOW_CAP + 1 ? `wf_${i}` : null, started_at: iso(NOW - (i + 1) * 60_000) }),
    )
    serve(many, many.filter((t) => t.workflow_id !== null).map((t) => workflowRead(t.workflow_id!, [{ step: 'build', task: t }])))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(agents().querySelectorAll('li[data-task]')).toHaveLength(AGENT_CAP))
    expect(AGENT_CAP).toBe(6)
    expect(WORKFLOW_CAP).toBe(4)
    await waitFor(() => expect(workflows().querySelectorAll('[data-workflow]')).toHaveLength(WORKFLOW_CAP))
    const moreAgents = within(agents()).getByRole('button', { name: 'Show 2 more agents' })
    fireEvent.click(moreAgents)
    expect(agents().querySelectorAll('li[data-task]')).toHaveLength(AGENT_CAP + 2)
    expect(within(agents()).getByRole('button', { name: 'Show fewer agents' })).toBeTruthy()
    fireEvent.click(within(workflows()).getByRole('button', { name: 'Show 1 more workflows' }))
    expect(workflows().querySelectorAll('[data-workflow]')).toHaveLength(WORKFLOW_CAP + 1)
    // "All running" in the head goes to the Running page.
    expect(within(work()).getByRole('link', { name: 'All running' }).getAttribute('href')).toBe(addressToPath('work/running/live'))
  })

  it('an agent finished in the last minutes stays at the bottom, faded, with a check', async () => {
    const ended = task('task_done1', 'SUCCEEDED', { completed_at: iso(NOW - 60_000), updated_at: iso(NOW - 60_000) })
    const long = task('task_old1', 'SUCCEEDED', { completed_at: iso(NOW - 3_600_000), updated_at: iso(NOW - 3_600_000) })
    serve([ended, terminal, long])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(rowOf('task_done1')).toBeTruthy())
    const order = [...agents().querySelectorAll('li[data-task]')].map((r) => r.getAttribute('data-task'))
    expect(order).toEqual(['task_term1', 'task_done1'])
    expect(rowOf('task_done1').className).toContain('is-done')
    expect(within(rowOf('task_done1')).getByRole('img', { name: 'Finished' }).textContent).toBe('✓')
  })
})

describe("Your work's prose may only go down", () => {
  // The prose ratchet (proseapp.tsx) does not open Home, so the card is held
  // here with the ratchet's own `measure`. On main before this change the same
  // five tasks drew 61 words and one arrow link; the lists draw 37 and none.
  const CEILING = { words: 37, arrows: 0 }

  it('the card draws no more words than its ceiling', async () => {
    const queued = task('task_q1', 'QUEUED', { metadata: { title: 'Queued one' } })
    const step = task('task_step1', 'RUNNING', { workflow_id: 'wf_one', step_id: 'build', metadata: { title: 'Build the fix' } })
    serve([terminal, browser, asking, queued, step], [workflowRead('wf_one', [{ step: 'build', task: step }, { step: 'merge', task: asking }])])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(within(workflows()).getAllByText('Workflow wf_one').length).toBeGreaterThan(0))
    const m = measure(work())
    expect(m.words).toBeLessThanOrEqual(CEILING.words)
    expect(m.arrows).toBeLessThanOrEqual(CEILING.arrows)
  })
})

describe('Your work on a 390 phone', () => {
  const PHONE = { width: 390 }

  it('stacks the two lists and draws nothing wider than the viewport', async () => {
    serve([terminal, browser, asking], [workflowRead('wf_one', [{ step: 'merge', task: asking }])])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(rowOf('task_merge1')).toBeTruthy())
    expect(painted(work().querySelector('.wl-cols')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(work().querySelector('.wl-cols')!, 'grid-template-columns', { width: 1440 })).toBe('minmax(0, 1fr) minmax(0, 1fr)')
    // Every row's text column may shrink to nothing; only the icon and the end are fixed.
    expect(painted(rowOf('task_term1'), 'grid-template-columns', PHONE)).toBe('22px minmax(0, 1fr) auto')
    expect(painted(rowOf('task_brow1').querySelector('.wl-thumb')!, 'width', PHONE)).toBe('60px')
    expect(painted(rowOf('task_brow1').querySelector('.wl-thumb')!, 'height', PHONE)).toBe('38px')
    // A name wraps on the phone rather than pushing the row wide.
    expect(painted(rowOf('task_term1').querySelector('.wl-name')!, 'white-space', PHONE)).toBe('normal')
    fireEvent.click(within(rowOf('task_merge1')).getByRole('button', { name: 'Answer' }))
    expect(painted(rowOf('task_merge1').querySelector('.wl-answer-row')!, 'flex-wrap', PHONE)).toBe('wrap')
    expect(painted(rowOf('task_merge1').querySelector('.wl-answer-input')!, 'min-width', PHONE)).toBe('0')
    // No fixed width anywhere in the sheet is wider than the phone.
    const widths = [...WORKLISTS_CSS.matchAll(/(?:^|[;{\s])(?:min-)?width:\s*(\d+)px/g)].map((m) => Number(m[1]))
    expect(widths.length).toBeGreaterThan(0)
    expect(Math.max(...widths)).toBeLessThan(390)
  })
})

describe("Your work's workflow rows: Review and the pull request's checks (PR #3901)", () => {
  const step = task('task_step1', 'RUNNING', { workflow_id: 'wf_one', step_id: 'build', metadata: { title: 'Build the fix' } })
  const other = task('task_step2', 'RUNNING', { workflow_id: 'wf_two', step_id: 'build', metadata: { title: 'Build the other' } })
  const integrator = (wf: string, number: number) =>
    task(`task_int_${wf}`, 'SUCCEEDED', {
      workflow_id: wf,
      step_id: 'integrate',
      completed_at: iso(NOW - 60_000),
      result_summary: { git: { role: 'integrator', pull_request: { number, url: `https://github.com/example-org/widgets/pull/${number}` } } } as Task['result_summary'],
    })
  const pull = (number: number, checks: WorkflowPullRequestChecks['checks']): WorkflowPullRequestChecks => ({
    number,
    url: `https://github.com/example-org/widgets/pull/${number}`,
    head_sha: 'a'.repeat(40),
    checks,
    check_counts: { passed: 5, failed: 1, pending: 0, skipped: 2 },
    checked_at: iso(NOW - 120_000),
    merged: false,
  })
  const withPull = (read: ReturnType<typeof workflowRead>, pr: WorkflowPullRequestChecks | null) => ({ ...read, workflow: { ...read.workflow, pull_request: pr } })
  const wfRow = (id: string) => work().querySelector(`[data-workflow="${id}"]`) as HTMLElement

  it('says Review, a link to the approval, on the workflow an open approval names', async () => {
    serve([step, other], [workflowRead('wf_one', [{ step: 'build', task: step }]), workflowRead('wf_two', [{ step: 'build', task: other }])])
    api.loadApprovals.mockResolvedValue(
      ok({
        approvals: [
          { approval_id: 'apr_merge_1', kind: 'merge', subject: { run_id: 'run_1', workflow_id: 'wf_two', pr: 41 }, summary: 'widgets#7: pull request #41 waits to merge', state: 'pending', requested_at: iso(NOW - 60_000), expires_at: null },
        ],
      }),
    )
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(within(wfRow('wf_two')).getByRole('link', { name: /^Review/ })).toBeTruthy())
    const review = within(wfRow('wf_two')).getByRole('link', { name: /^Review/ })
    expect(review.textContent).toBe('Review')
    expect(review.getAttribute('href')).toBe(addressToPath(approvalAddress('apr_merge_1', 'merge')))
    expect(within(wfRow('wf_one')).queryByRole('link', { name: /^Review/ })).toBeNull()
    // The workflow that waits on a review sorts first.
    const order = [...within(workflows()).getAllByRole('listitem')].map((li) => li.getAttribute('data-workflow')).filter(Boolean)
    expect(order[0]).toBe('wf_two')
  })

  it("draws the served reading as a dot on the same pull request, the number alone without one", async () => {
    const int1 = integrator('wf_one', 41)
    const int2 = integrator('wf_two', 52)
    serve(
      [step, other],
      [
        withPull(workflowRead('wf_one', [{ step: 'build', task: step }, { step: 'integrate', task: int1 }]), pull(41, 'red')),
        withPull(workflowRead('wf_two', [{ step: 'build', task: other }, { step: 'integrate', task: int2 }]), pull(99, 'green')),
      ],
    )
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(wfRow('wf_one')?.querySelector('.wl-pr')).toBeTruthy())
    const dot = wfRow('wf_one').querySelector('.wl-checks') as HTMLElement
    expect(dot.getAttribute('data-checks')).toBe('red')
    expect(dot.getAttribute('aria-label')).toBe('Checks failing · 5 passed · 1 failed · 0 pending')
    expect(wfRow('wf_one').querySelector('.wl-pr')!.textContent).toContain('#41')
    // Another pull request's reading is never put on this one.
    expect(wfRow('wf_two').querySelector('.wl-pr')!.textContent).toContain('#52')
    expect(wfRow('wf_two').querySelector('.wl-checks')).toBeNull()
  })

  it('draws a pull request the workflow read alone serves, with its dot', async () => {
    serve([step], [withPull(workflowRead('wf_one', [{ step: 'build', task: step }]), pull(41, 'pending'))])
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(wfRow('wf_one')?.querySelector('.wl-checks')).toBeTruthy())
    expect(wfRow('wf_one').querySelector('.wl-checks')!.getAttribute('data-checks')).toBe('pending')
    expect(wfRow('wf_one').querySelector('a.wl-pr')!.getAttribute('href')).toBe('https://github.com/example-org/widgets/pull/41')
  })
})
