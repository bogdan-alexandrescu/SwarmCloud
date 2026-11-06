// ONLY WHAT STILL HOLDS WORK (BGD's pick, 6 November 2026: "why do I have so
// many needs me in the console and a ton of question marks even beside
// completed workflows?"). A question counts for Needs you and the list's "?"
// only while its workflow has not finished, or while a merge is held on the
// owner's questions; the same words asked by two steps count once; a finished
// workflow's page keeps its unanswered questions behind one quiet tick beside
// its state, each still answerable there. The bell's task question source
// counts by the same rule (tests/unit/control_plane/test_notifications_holds_work.py).
//
// MUTATIONS: count a finished workflow's questions in `workflowNeedItems`;
// drop a held merge's questions; match a repeated question on its options;
// leave the finished page without its tick, or the tick without the card.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { TaskQuestions, WorkflowBoard } from '../api'
import { summaryTaskById, type StepNeeds } from '../stepviews'
import type { Workflow } from '../types'
import { finishedQuestionsOf, listNeedItems, questionKey } from '../work/listNeeds'
import { needsOf } from '../workflowlist'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')

afterEach(cleanup)

const block = (state: string, needs: StepNeeds, extra: Record<string, unknown> = {}) => ({ state, title: null, unit: null, needs, ...extra })
const parkedHuman = { reason: 'HUMAN_REQUIRED', summary: 'waits for a person' }

function workflow(state: string, steps: { id: string; summary: unknown; waiting?: unknown }[], id = 'wf_held'): Workflow {
  return {
    workflow_id: id,
    tenant_id: 'eng',
    state,
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: '2026-11-03T10:00:00Z',
    updated_at: '2026-11-03T10:30:00Z',
    submitted_by: 'priya@example.com',
    priority: 0,
    on_step_failure: 'stop',
    cancel_requested: false,
    steps: steps.map((s) => ({
      step_id: s.id,
      runner_profile: 'claude-code',
      resource_class: 'standard',
      depends_on: [],
      input_from: {},
      task_id: `task_${s.id}`,
      task_summary: s.summary,
      ...(s.waiting === undefined ? {} : { waiting: s.waiting }),
    })),
  } as unknown as Workflow
}

/** implement asked two questions, fix asked one, none answered; then the merge. */
function shipped(state: string, merge: unknown = block('SUCCEEDED', {})): Workflow {
  return workflow(state, [
    { id: 'implement', summary: block('SUCCEEDED', { questions_unread: [0, 1] }) },
    { id: 'fix', summary: block('SUCCEEDED', { questions_unread: [0] }) },
    { id: 'merge', summary: merge, ...(typeof merge === 'object' && (merge as { state: string }).state === 'PARKED' ? { waiting: parkedHuman } : {}) },
  ])
}

const questionsOf = (w: Workflow) => listNeedItems([w], summaryTaskById([w]), () => 'Ship it').filter((i) => i.kind === 'question')

describe('a finished workflow holds no work', () => {
  for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']) {
    it(`a ${state} workflow's unanswered questions are no "?" and no Needs you row`, () => {
      const w = shipped(state)
      expect(needsOf(w, summaryTaskById([w]))?.questions).toBe(0)
      expect(questionsOf(w)).toEqual([])
      expect(listNeedItems([w], summaryTaskById([w]), () => 'Ship it').unknownKinds).toEqual([])
    })
  }

  it('keeps them for its page, behind the quiet tick', () => {
    const w = shipped('SUCCEEDED')
    expect(finishedQuestionsOf(w, summaryTaskById([w])).map((q) => [q.taskId, q.index])).toEqual([
      ['task_implement', 0],
      ['task_implement', 1],
      ['task_fix', 0],
    ])
  })

  it('still counts a running workflow\'s questions, and keeps none behind a tick', () => {
    const w = shipped('RUNNING', block('RUNNING', {}))
    expect(needsOf(w, summaryTaskById([w]))?.questions).toBe(3)
    expect(questionsOf(w)).toHaveLength(3)
    expect(finishedQuestionsOf(w, summaryTaskById([w]))).toEqual([])
  })

  it('still counts the questions a held merge waits on', () => {
    const hold = { questions: [{ task_id: 'task_implement', step_id: 'implement', question_index: 0, question: 'Which?' }], unreadable: 0 }
    const w = shipped('PARKED', block('PARKED', { owner_questions: hold }))
    const items = listNeedItems([w], summaryTaskById([w]), () => 'Ship it')
    expect(items.filter((i) => i.kind === 'merge')).toHaveLength(1)
    expect(needsOf(w, summaryTaskById([w]))).toEqual({ failed: 0, questions: 3, held: 1 })
  })

  it('a finished workflow keeps a failed step in Needs you', () => {
    const w = workflow('FAILED', [
      { id: 'implement', summary: block('FAILED', { questions_unread: [0] }) },
    ])
    expect(needsOf(w, summaryTaskById([w]))).toEqual({ failed: 1, questions: 0, held: 0 })
  })
})

describe('the same question asked by two steps counts once', () => {
  const ask = (question: string, labels: string[]) => ({
    question_wait: true,
    questions: [{ index: 0, question, options: labels.map((label) => ({ label, description: null })), recommended: null }],
  })

  it('matches the words, not the option labels, case and spacing aside', () => {
    const w = workflow('RUNNING', [
      { id: 'implement', summary: block('PARKED', ask('Which database  should it use?', ['Postgres', 'SQLite'])), waiting: parkedHuman },
      { id: 'fix', summary: block('PARKED', ask('which database should it use?', ['Postgres (keep)', 'SQLite (small)'])), waiting: parkedHuman },
      { id: 'other', summary: block('PARKED', ask('Which region?', ['eu', 'us'])), waiting: parkedHuman },
    ])
    expect(questionKey('Which database  should it use?')).toBe(questionKey('which database should it use?'))
    expect(needsOf(w, summaryTaskById([w]))?.questions).toBe(2)
    expect(questionsOf(w).map((q) => (q.kind === 'question' ? q.taskId : ''))).toEqual(['task_implement', 'task_other'])
  })
})

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

function questionsRead(taskId: string, question: string, may = true): TaskQuestions {
  return {
    task_id: taskId,
    count: 1,
    status: 'ok',
    attempt_id: 'att_1',
    redacted: false,
    redaction_count: 0,
    may_answer: may,
    questions: [{ question, context: null, recommended: 'yes', options: [{ label: 'yes', description: null }, { label: 'no', description: null }] }],
  } as unknown as TaskQuestions
}

describe("a finished workflow's page", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  })

  function serve(w: Workflow): void {
    api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: { workflows: [w], taskById: summaryTaskById([w]), statesDetail: null }, fetchedAt: Date.now() } satisfies Result<WorkflowBoard>)
    api.loadWorkflow.mockResolvedValue(ok({ workflow: w, tasks: [...summaryTaskById([w]).values()] }))
  }

  async function head(): Promise<HTMLElement> {
    render(<WorkflowsScreen view="wf=wf_held" onView={() => {}} />)
    return waitFor(() => {
      const h = document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')
      expect(h).toBeTruthy()
      return h!
    })
  }

  it('shows one quiet tick with the count beside its state, and no Needs you question', async () => {
    serve(shipped('SUCCEEDED'))
    const h = await head()
    const tick = within(h).getByRole('button', { name: '3 unanswered questions' })
    expect(tick.textContent?.replace(/\s+/g, '')).toBe('?3')
    expect(tick.getAttribute('title')).toContain('not in Needs you')
    expect(document.querySelector('section[aria-label="Needs you"] li')).toBeNull()
  })

  it('opens each asking step\'s questions from the tick, and answering one records the answer', async () => {
    serve(shipped('SUCCEEDED'))
    api.loadQuestions.mockImplementation(async (id: string) => ok(questionsRead(id, `Ship ${id}?`)))
    api.answerQuestion.mockImplementation(async (id: string) => ok(questionsRead(id, `Ship ${id}?`)))
    const h = await head()
    fireEvent.click(within(h).getByRole('button', { name: '3 unanswered questions' }))
    const cards = await within(h).findAllByRole('region', { name: 'Questions for you' })
    expect(cards).toHaveLength(2)
    await waitFor(() => expect(api.loadQuestions).toHaveBeenCalledWith('task_fix'))
    const fix = cards.find((c) => c.textContent?.includes('step fix'))!
    const group = await within(fix).findByRole('radiogroup', { name: 'Answer question 1' })
    fireEvent.click(within(group).getByRole('radio', { name: /no/ }))
    fireEvent.click(within(fix).getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledWith('task_fix', { question_index: 0, choice: 'no', text: null }))
  })

  it('a running workflow draws no tick', async () => {
    serve(shipped('RUNNING', block('RUNNING', {})))
    const h = await head()
    expect(within(h).queryByRole('button', { name: /unanswered question/ })).toBeNull()
  })
})
