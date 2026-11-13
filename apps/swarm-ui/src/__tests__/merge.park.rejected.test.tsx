// A QUESTIONS.JSON THE PLATFORM DID NOT TAKE IS SHOWN WITH ITS PLACE AND ITS
// REASON, NEVER AS "ASKED NO QUESTIONS" (#3086).
//
// A build step's questions.json the worker's validator rejects is recorded as
// `questions: 0` with `questions_rejected: <reason>`. The merge it holds parks
// on the owner's questions with `rejected_files: [{task_id, step_id, file,
// reason}]` in its owner-question record, and the task's questions read
// answers `status: 'unavailable'` with `unavailable_because` and `rejected`
// while its count is 0. Before this the merge's wait panel drew a bare
// "Unreadable entries 1" and the asking task's Details drew no questions card
// at all, so nothing on the page said which step wrote what, or why it was
// not taken.
//
// MUTATIONS: draw no `rejected` fact in OwnerQuestionFacts; read only
// `detail.rejected_files` (drop the blocked_by fallback); link the step's name
// nowhere; return null from QuestionsCard whenever the count is 0.

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RejectedQuestionsFile, TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { Task, Waiting } from '../types'
import { task as runTask, at } from './runfixture'

const api = vi.hoisted(() => ({
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { WaitPanel } = await import('../WaitPanel')
const { QuestionsCard } = await import('../QuestionsCard')

const ID = 'task_7ee2d99642c84dc0b3b2'
const IMPL = 'task_a1b2c3d4e5f6a7b8c9d0'
const FIX = 'task_f0e1d2c3b4a5968778695'
const NOW = Date.parse(at(120))
const REASON = "question 1 carries a key the platform does not know: 'priority'"

const REJECTED: RejectedQuestionsFile[] = [
  { task_id: IMPL, step_id: 'implement', file: 'questions.json', reason: REASON },
  { task_id: FIX, step_id: null, file: 'questions.json', reason: 'the file is not JSON' },
]

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function waiting(detail: Record<string, unknown>): Waiting {
  return {
    reason: 'HUMAN_REQUIRED',
    summary: 'Waiting for the owner questions the review named before merging',
    since: at(100),
    next_check_at: null,
    resumes_when: 'the last named question is answered (it resumes on its own)',
    detail: {
      park_phase: 'owner_questions',
      questions: [],
      unreadable: 2,
      who_may_answer: { submitted_by: 'owner@example.com', admin: true, text: 'owner@example.com, or an admin' },
      answer_route: `POST /v1/tasks/${ID}/human-ready`,
      ...detail,
    },
    history: [],
    history_read: true,
    read_at: at(120),
  }
}

function mergeHeld(detail: Record<string, unknown>, record: Record<string, unknown> = {}): Task {
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'merge',
    workflow_id: 'wf_7548cc6f627f42769919',
    step_id: 'merge',
    submitted_by: 'owner@example.com',
    completed_at: null,
    result_summary: null,
    blocked_by: [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { code: 'owner_questions_open', questions: [], unreadable: 2, ...record },
      } as unknown as NonNullable<Task['blocked_by']>[number],
    ],
    waiting: waiting(detail),
  })
}

function rejectedRows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[data-fact="rejected"] li[data-rejected-file]'))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('a merge held on questions the platform did not take', () => {
  it('lists each rejected file with its step, its reason and a link to its task', () => {
    const { container } = render(<WaitPanel task={mergeHeld({ rejected_files: REJECTED })} now={NOW} />)
    const rows = rejectedRows(container)
    // MUTATION: no `rejected` fact -- the panel says only "Unreadable entries 2".
    expect(rows).toHaveLength(2)
    expect(rows[0]!.textContent).toBe(`implement wrote questions.json the platform did not take: ${REASON}`)
    // A file whose step was not recorded is named by its task.
    expect(rows[1]!.textContent).toBe(`${FIX} wrote questions.json the platform did not take: the file is not JSON`)
    const link = rows[0]!.querySelector<HTMLAnchorElement>('a')
    // MUTATION: the step's name drawn as plain text.
    expect(link, 'no link to the asking task').not.toBeNull()
    expect(link!.textContent).toBe('implement')
    expect(link!.getAttribute('href')).toBe(`/agents/recent/${IMPL}/details`)
    expect(rows[0]!.getAttribute('data-rejected-file')).toBe(IMPL)
  })

  it('reads them off the park record when the wait detail does not carry them', () => {
    const { container } = render(<WaitPanel task={mergeHeld({}, { rejected_files: REJECTED.slice(0, 1) })} now={NOW} />)
    const rows = rejectedRows(container)
    // MUTATION: read only `detail.rejected_files`.
    expect(rows).toHaveLength(1)
    expect(rows[0]!.textContent).toContain(REASON)
  })

  it('draws no rejected row for a hold that names none', () => {
    const { container } = render(<WaitPanel task={mergeHeld({})} now={NOW} />)
    expect(container.querySelector('[data-fact="rejected"]')).toBeNull()
    expect(container.querySelector('[data-fact="unreadable"]')?.textContent).toContain('2')
  })

  it('skips an entry that names no task', () => {
    const bad = [{ task_id: '', step_id: 'implement', file: 'questions.json', reason: REASON }, 'junk']
    const { container } = render(<WaitPanel task={mergeHeld({ rejected_files: bad })} now={NOW} />)
    expect(container.querySelector('[data-fact="rejected"]')).toBeNull()
  })
})

describe("the asking task's questions card, for a file the platform did not take", () => {
  it('says why the questions could not be read, though it counted none', async () => {
    const UNAVAILABLE = `questions.json was not taken: ${REASON}`
    api.loadQuestions.mockResolvedValue(
      ok<TaskQuestions>({
        task_id: IMPL,
        count: 0,
        status: 'unavailable',
        questions: [],
        unavailable_because: UNAVAILABLE,
        rejected: { file: 'questions.json', reason: REASON },
        redacted: false,
        redaction_count: 0,
      }),
    )
    const t = runTask({
      id: IMPL,
      state: 'SUCCEEDED',
      step_id: 'implement',
      result_summary: { questions: 0, questions_rejected: REASON },
    })
    render(<QuestionsCard task={t} />)
    // MUTATION: return null whenever the count is 0.
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    await waitFor(() => expect(card.textContent).toContain(UNAVAILABLE))
    expect(api.loadQuestions).toHaveBeenCalledWith(IMPL)
    expect(card.textContent).not.toMatch(/asked no questions|served none/)
    expect(card.textContent).not.toContain('asked you these')
  })

  it('draws nothing for a task that wrote no questions.json', () => {
    const t = runTask({ id: IMPL, state: 'SUCCEEDED', result_summary: { questions: 0 } })
    const { container } = render(<QuestionsCard task={t} />)
    expect(container.innerHTML).toBe('')
    expect(api.loadQuestions).not.toHaveBeenCalled()
  })
})
