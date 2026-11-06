// AN AGENT'S QUESTIONS ARE ANSWERED FROM ANY TAB OF ITS PAGE (part of #2688).
//
// BGD, 2026-11-06 ~06:00Z: "We need to have a way to act on questions in the
// workflow page and in the agent page." The agent page drew its questions
// only in Details (QuestionsCard, HeldQuestionsCard), so on Logs, Changes,
// Attempts, Artifacts or Checkpoints nothing said the agent asked anything.
// Every other tab now leads with one row: the asks-you mark, the open count,
// the first open question clipped to a line, and Answer, which opens the same
// card in place under the row.
//
// MUTATIONS: mount no row in AgentSplit; mount it on Details too; open the
// QuestionsCard for a held merge; count the Details tab from questionCount
// alone; keep the row once every question is answered on a later visit.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { at, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadChildren: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadLiveSession: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentSplit } = await import('../AgentSplit')
const { fromAddress } = await import('../App')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0a1b2c3d4e5f60718293'
const IMPL = 'task_a1b2c3d4e5f6a7b8c9d0'
const FIX = 'task_f0e1d2c3b4a5968778695'
const SPLIT = { timeout: 12_000 }
const LONG = `Keep the old route as a redirect for the clients that still call it, ${'or retire it now '.repeat(6)}?`

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

type Answer = NonNullable<TaskQuestions['questions'][number]['answer']>

function answer(choice: string, by: string): Answer {
  return { choice, text: null, by, at: new Date(Date.now() - 5 * 60_000).toISOString(), history: [] } as unknown as Answer
}

function asked(taskId: string, texts: string[], answers: (Answer | null)[] = []): TaskQuestions {
  return {
    task_id: taskId,
    count: texts.length,
    status: 'ok',
    redacted: false,
    redaction_count: 0,
    may_answer: true,
    questions: texts.map((t, i) => ({
      question: t,
      options: [
        { label: 'Yes', description: null },
        { label: 'No', description: null },
      ],
      recommended: 'Yes',
      context: null,
      answer: answers[i] ?? null,
    })),
  }
}

/** A finished agent that asked its owner two questions in its questions.json. */
function asking(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'SUCCEEDED', started_at: at(1), completed_at: at(9), result_summary: { questions: 2 }, ...over })
}

/** A merge step parked on two questions its review named, asked by two other steps. */
function mergeHeld(): Task {
  const held = [
    { task_id: IMPL, step_id: 'implement', question_index: 0, question: 'Keep the old route?' },
    { task_id: FIX, step_id: 'fix', question_index: 1, question: 'Rename the flag now?' },
  ]
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'merge',
    workflow_id: 'wf_7548cc6f627f42769919',
    step_id: 'merge',
    started_at: at(1),
    blocked_by: [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { code: 'owner_questions_open', questions: held, unreadable: 0 },
      } as unknown as NonNullable<Task['blocked_by']>[number],
    ],
  })
}

type Pane = 'detail' | 'logs' | 'attempts' | 'artifacts'

function Routed({ start }: { start: Pane }) {
  const [p, setP] = useState<Pane>(start)
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane={p}
        theater={false}
        artifact={null}
        closeTo="work/recent"
        go={(to) => setP(fromAddress(to).taskPane as Pane)}
        base={`work/task/${ID}`}
      />
    </div>
  )
}

const strip = () => within(screen.getByRole('tablist', { name: 'Agent panes' }))
const row = () => document.querySelector<HTMLElement>('.aq-row')

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveSession.mockReturnValue(new Promise(() => {}))
  api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
  api.loadQuestions.mockImplementation(async (id: string) =>
    ok(id === ID ? asked(ID, [LONG, 'Ship it behind a flag?']) : asked(id, ['Keep the old route?', 'Rename the flag now?'])),
  )
  api.answerQuestion.mockReset()
})

describe('the questions row on a tab that is not Details', () => {
  it('shows the mark, the open count and the first question clipped to one line, card closed', async () => {
    api.loadTask.mockResolvedValue(ok(asking()))
    render(<Routed start="logs" />)
    await waitFor(() => expect(row()).not.toBeNull(), SPLIT)
    const r = row()!
    expect(r.querySelector('.aq-mark')).not.toBeNull()
    expect(r.querySelector('.aq-count')?.textContent).toBe('2')
    const text = r.querySelector<HTMLElement>('.aq-text')!
    expect(text.textContent).toBe(LONG)
    // Clipped by the sheet to one line; the whole question is its title.
    expect(text.title).toBe(LONG)
    expect(within(r).getByRole('button', { name: 'Answer' }).getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('region', { name: 'Questions for you' })).toBeNull()
    expect(strip().getByRole('tab', { selected: true }).textContent).toContain('Logs')
  })

  it('opens the card in place under the row, posts the answer to the task, and folds back', async () => {
    api.loadTask.mockResolvedValue(ok(asking()))
    api.answerQuestion.mockResolvedValue(ok(asked(ID, [LONG, 'Ship it behind a flag?'], [answer('Yes', 'bgd@example.com')])))
    render(<Routed start="attempts" />)
    await waitFor(() => expect(row()).not.toBeNull(), SPLIT)
    fireEvent.click(within(row()!).getByRole('button', { name: 'Answer' }))
    const card = await screen.findByRole('region', { name: 'Questions for you' }, SPLIT)
    // In place: under the row, on the same tab.
    expect(row()!.parentElement!.contains(card)).toBe(true)
    expect(strip().getByRole('tab', { selected: true }).textContent).toContain('Attempts')
    const first = within(card).getByRole('radiogroup', { name: 'Answer question 1' })
    fireEvent.click(within(first).getByRole('radio', { name: /Yes/ }))
    fireEvent.click(within(first).getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledTimes(1))
    expect(api.answerQuestion.mock.calls[0]![0]).toBe(ID)
    expect(api.answerQuestion.mock.calls[0]![1]).toMatchObject({ question_index: 0, choice: 'Yes' })
    fireEvent.click(within(row()!).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Questions for you' })).toBeNull())
    expect(row()).not.toBeNull()
  })

  it('draws no row when the agent asked nothing', async () => {
    api.loadTask.mockResolvedValue(ok(asking({ result_summary: null })))
    render(<Routed start="logs" />)
    await waitFor(() => expect(document.querySelector('.ag-head')?.textContent ?? '').toContain(ID.slice(0, 8)), SPLIT)
    await waitFor(() => expect(strip().getAllByRole('tab').length).toBeGreaterThan(1), SPLIT)
    expect(row()).toBeNull()
    expect(api.loadQuestions).not.toHaveBeenCalled()
  })

  it('opens the held card for a merge a blocking question holds', async () => {
    api.loadTask.mockResolvedValue(ok(mergeHeld()))
    render(<Routed start="logs" />)
    await waitFor(() => expect(row()).not.toBeNull(), SPLIT)
    expect(row()!.querySelector('.aq-count')?.textContent).toBe('2')
    expect(row()!.querySelector('.aq-text')?.textContent).toBe('Keep the old route?')
    fireEvent.click(within(row()!).getByRole('button', { name: 'Answer' }))
    await screen.findByRole('region', { name: 'Questions holding this merge' }, SPLIT)
    expect(screen.queryByRole('region', { name: 'Questions for you' })).toBeNull()
  })

  it('reads answered with who and when once all are answered, and is gone on the next visit', async () => {
    let answered = false
    api.loadQuestions.mockImplementation(async () =>
      ok(asked(ID, ['One?', 'Two?'], answered ? [answer('Yes', 'bgd@example.com'), answer('No', 'bgd@example.com')] : [])),
    )
    api.loadTask.mockResolvedValue(ok(asking()))
    const view = render(<Routed start="logs" />)
    await waitFor(() => expect(row()).not.toBeNull(), SPLIT)
    fireEvent.click(within(row()!).getByRole('button', { name: 'Answer' }))
    await screen.findByRole('region', { name: 'Questions for you' }, SPLIT)
    answered = true
    fireEvent.click(within(row()!).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(row()?.textContent ?? '').toContain('answered'), SPLIT)
    expect(row()!.textContent).toContain('bgd@example.com')
    expect(row()!.textContent).toContain('ago')
    view.unmount()
    render(<Routed start="logs" />)
    await waitFor(() => expect(api.loadQuestions.mock.calls.length).toBeGreaterThanOrEqual(3), SPLIT)
    await waitFor(() => expect(strip().getAllByRole('tab').length).toBeGreaterThan(1), SPLIT)
    expect(row()).toBeNull()
  })
})

describe('the Details tab', () => {
  it('draws no row there: the cards lead Details', async () => {
    api.loadTask.mockResolvedValue(ok(asking()))
    render(<Routed start="detail" />)
    await waitFor(() => expect(strip().getByRole('tab', { name: /^Details/ }).textContent).toContain('2'), SPLIT)
    expect(row()).toBeNull()
  })

  it("carries the open-question count in its label, a held merge's too", async () => {
    api.loadTask.mockResolvedValue(ok(mergeHeld()))
    render(<Routed start="logs" />)
    await waitFor(() => expect(strip().getByRole('tab', { name: 'Details, 2' })).toBeTruthy(), SPLIT)
  })
})
