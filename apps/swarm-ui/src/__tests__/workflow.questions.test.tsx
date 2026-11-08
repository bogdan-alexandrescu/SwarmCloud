/**
 * THE WORKFLOW PAGE'S NEEDS YOU CARD AND QUESTIONS SECTION (owner, 6 November
 * 2026: "We need to have a way to act on questions in the workflow page", and
 * the Needs you card "start off as a one line collapsed section"; part of
 * #2688). The fixture: `design` asked one question (finished), `build` is
 * parked on a blocking question, `lint` failed, `ship` not started.
 *
 * MUTATIONS, one per block: start the card open, or remember it open in
 * storage; draw one mark for every kind; let the header open but not close;
 * draw a card with nothing waiting; post an answer to the wrong task; draw
 * the section with no questions; let the grid's asks-you mark pick the chip
 * instead of jumping to the step's questions; draw the mark inside the chip's
 * button or out of the tab order; give every step's cards the agent page's ids.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { TaskQuestions, WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowNeedsCard, WorkflowQuestions, questionsBlockId } = await import('../WorkflowQuestions')
const { workflowNeedItems } = await import('../work/listNeeds')
const { WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-11-06T06:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()
const WF = 'wf_questions'

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-7200),
    updated_at: iso(-60),
    started_at: iso(-7000),
    completed_at: null,
    submitted_by: 'alex@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: WF,
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
    current_generation: 0,
    current_lease_id: null,
    ...over,
  } as Task
}

const ASKED_DESIGN = { question: 'Which layout?', context: null, options: [{ label: 'Grid', description: null }, { label: 'List', description: null }], recommended: 'Grid' }
const ASKED_BUILD = { question: 'Keep the old flag?', context: null, options: [{ label: 'Keep', description: null }, { label: 'Drop', description: null }], recommended: 'Drop', blocking: true }

function tasks(over: { designAnswered?: boolean } = {}): Map<string, Task> {
  const answered = over.designAnswered === true
  const design = task('t_design', 'SUCCEEDED', {
    step_id: 'design',
    completed_at: iso(-3600),
    result_summary: { questions: 1 },
    owner_answers: answered
      ? [{ question_index: 0, question: 'Which layout?', choice: 'Grid', text: null, by: 'alex@example.com', at: iso(-600), history: [] }]
      : [],
  } as Partial<Task>)
  const build = task('t_build', 'PARKED', {
    step_id: 'build',
    park_reason: 'QUESTION_WAIT',
    metadata: { question_wait: { questions: [ASKED_BUILD], blocking: [0] } },
  } as Partial<Task>)
  const lint = task('t_lint', 'FAILED', { step_id: 'lint', completed_at: iso(-1200), last_error: 'ruff: 3 errors' })
  return new Map([
    ['t_design', design],
    ['t_build', build],
    ['t_lint', lint],
  ])
}

function wfStep(step_id: string, depends_on: string[], over: Partial<WorkflowStep> & Record<string, unknown> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over } as WorkflowStep
}

const need = (questions: { index: number; question: string }[]) => ({
  task_summary: { needs: { question_wait: false, questions: questions.map((q) => ({ ...q, options: [], recommended: null })) } },
})

function workflow(over: { designAnswered?: boolean; noQuestions?: boolean } = {}): Workflow {
  const steps = over.noQuestions
    ? [wfStep('lint', [], { task_id: 't_lint', ...need([]) }), wfStep('ship', ['lint'])]
    : [
        wfStep('design', [], { task_id: 't_design', ...need(over.designAnswered ? [] : [{ index: 0, question: 'Which layout?' }]) }),
        wfStep('build', ['design'], { task_id: 't_build', ...need([{ index: 0, question: 'Keep the old flag?' }]) }),
        wfStep('lint', ['design'], { task_id: 't_lint', ...need([]) }),
        wfStep('ship', ['build', 'lint']),
      ]
  return {
    workflow_id: WF,
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'PARKED', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-7200),
    updated_at: iso(-60),
    submitted_by: 'alex@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  } as unknown as Workflow
}

function served(taskId: string, asked: unknown[], answers: Record<number, unknown> = {}): Result<TaskQuestions> {
  return {
    status: 'ok',
    fetchedAt: T0,
    data: {
      task_id: taskId,
      count: asked.length,
      status: 'ok',
      questions: asked.map((q, i) => ({ ...(q as object), answer: answers[i] ?? null })) as TaskQuestions['questions'],
      redacted: false,
      redaction_count: 0,
      may_answer: true,
      may_answer_because: null,
    },
  }
}

beforeEach(() => {
  api.loadQuestions.mockImplementation(async (id: string) => (id === 't_build' ? served('t_build', [ASKED_BUILD]) : served('t_design', [ASKED_DESIGN])))
  api.answerQuestion.mockImplementation(async (id: string) => served(id, id === 't_build' ? [ASKED_BUILD] : [ASKED_DESIGN], { 0: { choice: 'Drop', text: null, by: 'alex@example.com', at: iso(0), history: [] } }))
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  try {
    window.localStorage.clear()
    window.sessionStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function Card({ wf = workflow(), byId = tasks() }: { wf?: Workflow; byId?: Map<string, Task> }) {
  return <WorkflowNeedsCard items={workflowNeedItems(wf, byId, (s) => s)} workflow={wf} taskById={byId} />
}

// THE KEYBOARD, as a browser drives it (jsdom moves no focus on Tab and
// activates nothing on Enter): Tab focuses the next element in the tab order;
// Enter (on keydown) and Space (on keyup) click a native button the page did
// not stop.
const TABBABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]'
function tabOrder(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(TABBABLE)].filter((e) => e.tabIndex >= 0 && e.closest('[aria-hidden="true"], [hidden]') === null)
}
function pressTab() {
  const order = tabOrder()
  order[order.indexOf(document.activeElement as HTMLElement) + 1]?.focus()
}
function press(key: 'Enter' | ' ') {
  const el = document.activeElement as HTMLElement
  const down = fireEvent.keyDown(el, { key })
  const up = fireEvent.keyUp(el, { key })
  if (el.tagName === 'BUTTON' && (key === 'Enter' ? down : up)) fireEvent.click(el)
}

const header = () => screen.getByRole('button', { name: /Needs you/ })
const body = () => document.getElementById(header().getAttribute('aria-controls') ?? '')!

describe('the Needs you card', () => {
  it('starts as one collapsed row on every mount, one mark per kind with its count', () => {
    render(<Card />)
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(body().hidden).toBe(true)
    const marks = [...header().querySelectorAll<HTMLElement>('.wq-mark')].map((m) => [m.dataset.kind, m.title])
    expect(marks).toEqual([
      ['blocking', 'Blocking question holding a step: 1'],
      ['question', 'Questions to answer: 1'],
      ['failed', 'Failed: 1'],
    ])
    // Opened, then the page reloaded: collapsed again, nothing remembered.
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('true')
    cleanup()
    render(<Card />)
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(body().hidden).toBe(true)
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0)
  })

  it('expands on a header click into the items, and collapses on the next', () => {
    render(<Card />)
    expect(header().tagName).toBe('BUTTON')
    fireEvent.click(header())
    expect(body().hidden).toBe(false)
    const rows = within(body()).getAllByRole('listitem')
    expect(rows.map((r) => r.querySelector('.ny-title')?.textContent)).toEqual(['design', 'build', 'lint'])
    expect(within(rows[0]!).getByRole('button', { name: 'Answer design question 1' })).toBeTruthy()
    expect(within(rows[2]!).getByRole('link', { name: 'Open lint' }).getAttribute('href')).toContain('t_lint')
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(body().hidden).toBe(true)
  })

  it('draws no card when nothing waits', () => {
    const wf = workflow({ noQuestions: true })
    const byId = new Map([['t_lint', task('t_lint', 'SUCCEEDED', { step_id: 'lint' })]])
    const { container } = render(<Card wf={wf} byId={byId} />)
    expect(container.innerHTML).toBe('')
  })
})

describe('the questions section', () => {
  it("lists two steps' questions, and an answer posts to that step's task", async () => {
    render(<WorkflowQuestions workflow={workflow()} taskById={tasks()} stateOf={(s) => (s.step_id === 'build' ? 'parked' : 'done')} />)
    const design = document.getElementById(questionsBlockId('design'))!
    const build = document.getElementById(questionsBlockId('build'))!
    expect(design.querySelector('h3')?.textContent).toBe('design')
    expect(build.querySelector('.wq-state')?.textContent).toBe('parked')
    expect(within(build).getByRole('link', { name: 'Agent page' }).getAttribute('href')).toContain('t_build')
    const drop = await within(build).findByRole('radio', { name: /Drop/ })
    fireEvent.click(drop)
    fireEvent.click(within(build).getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledTimes(1))
    expect(api.answerQuestion).toHaveBeenCalledWith('t_build', { question_index: 0, choice: 'Drop', text: null })
    expect(await within(design).findByRole('radio', { name: /Grid/ })).toBeTruthy()
  })

  it('reads an answered step collapsed, with who and when', () => {
    render(<WorkflowQuestions workflow={workflow({ designAnswered: true })} taskById={tasks({ designAnswered: true })} stateOf={() => 'done'} />)
    const design = document.getElementById(questionsBlockId('design'))!
    const toggle = within(design).getByRole('button', { name: /answered/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.textContent).toContain('alex@example.com')
    expect(toggle.textContent).toContain('ago')
    expect(design.querySelector('.qc')).toBeNull()
  })

  it("gives each step's cards their own ids, never the agent page's", async () => {
    render(<WorkflowQuestions workflow={workflow()} taskById={tasks()} stateOf={() => 'done'} />)
    await within(document.getElementById(questionsBlockId('build'))!).findByRole('radio', { name: /Drop/ })
    const cards = [...document.querySelectorAll('.wq .qc')].map((e) => e.id)
    expect(cards).toEqual([`${questionsBlockId('design')}-asked`, `${questionsBlockId('build')}-asked`])
    const ids = [...document.querySelectorAll('[id]')].map((e) => e.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(document.getElementById('questions')).toBeNull()
    expect(document.getElementById('held-questions')).toBeNull()
  })

  it('draws no section when no step asked', () => {
    const wf = workflow({ noQuestions: true })
    const { container } = render(<WorkflowQuestions workflow={wf} taskById={tasks()} stateOf={() => 'done'} />)
    expect(container.innerHTML).toBe('')
  })

  it("the card's Answer focuses the step's first unanswered question", async () => {
    const wf = workflow()
    const byId = tasks()
    render(
      <>
        <Card wf={wf} byId={byId} />
        <WorkflowQuestions workflow={wf} taskById={byId} stateOf={() => 'done'} />
      </>,
    )
    const design = document.getElementById(questionsBlockId('design'))!
    await within(design).findByRole('radio', { name: /Grid/ })
    fireEvent.click(header())
    fireEvent.click(screen.getByRole('button', { name: 'Answer design question 1' }))
    expect(document.activeElement?.closest(`#${questionsBlockId('design')}`)).toBe(design)
    expect(document.activeElement?.getAttribute('role')).toBe('radio')
  })
})

describe('on the workflow page', () => {
  function Routed({ initial }: { initial: string }) {
    const [view, setView] = useState(initial)
    return <WorkflowsScreen view={view} onView={setView} />
  }

  it("draws the card collapsed and the section, and the grid's asks-you mark jumps to the step's questions", async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    api.loadWorkflowBoard.mockResolvedValue({
      status: 'ok',
      data: { workflows: [workflow()], taskById: tasks(), statesDetail: null },
      fetchedAt: T0,
    } satisfies Result<WorkflowBoard>)
    api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
    render(<Routed initial={`wf=${WF}`} />)
    const ask = await screen.findByRole('button', { name: 'Answer design: 1 question' })
    expect(document.querySelector('.wfp-main section.wq-needs button[aria-expanded="false"]')).toBeTruthy()
    const block = document.getElementById(questionsBlockId('design'))!
    expect(block).toBeTruthy()
    expect(document.getElementById(questionsBlockId('build'))).toBeTruthy()
    fireEvent.click(ask)
    expect(document.activeElement?.closest(`#${questionsBlockId('design')}`)).toBe(block)
    // The chip was not picked: the inspector still shows the overview.
    expect(document.querySelector('.wi-dock')?.getAttribute('aria-label')).toBe('Workflow overview')
  })

  function openPage() {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    api.loadWorkflowBoard.mockResolvedValue({
      status: 'ok',
      data: { workflows: [workflow()], taskById: tasks(), statesDetail: null },
      fetchedAt: T0,
    } satisfies Result<WorkflowBoard>)
    api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
    render(<Routed initial={`wf=${WF}`} />)
  }

  it("Tab reaches the asks-you mark, and Enter or Space jumps to the step's first unanswered question", async () => {
    openPage()
    const mark = await screen.findByRole('button', { name: 'Answer design: 1 question' })
    const block = document.getElementById(questionsBlockId('design'))!
    await within(block).findByRole('radio', { name: /Grid/ })
    // Beside the chip, never inside its button, and next in the tab order.
    expect(mark.closest('.sg-chip')).toBeNull()
    expect(mark.parentElement!.closest('button')).toBeNull()
    document.querySelector<HTMLElement>('.sg-chip[data-step="design"]')!.focus()
    pressTab()
    expect(document.activeElement).toBe(mark)
    press('Enter')
    expect(document.activeElement?.closest(`#${questionsBlockId('design')}`)).toBe(block)
    expect(document.activeElement?.getAttribute('role')).toBe('radio')
    mark.focus()
    press(' ')
    expect(document.activeElement?.getAttribute('role')).toBe('radio')
    expect(document.querySelector('.wi-dock')?.getAttribute('aria-label')).toBe('Workflow overview')
  })

  it("the chip's own click still picks the step", async () => {
    openPage()
    await screen.findByRole('button', { name: 'Answer design: 1 question' })
    fireEvent.click(document.querySelector<HTMLElement>('.sg-chip[data-step="design"]')!)
    expect(document.querySelector('.sg-chip[data-step="design"]')?.getAttribute('aria-pressed')).toBe('true')
    expect(document.querySelector('.wi-dock')?.getAttribute('aria-label')).not.toBe('Workflow overview')
  })
})
