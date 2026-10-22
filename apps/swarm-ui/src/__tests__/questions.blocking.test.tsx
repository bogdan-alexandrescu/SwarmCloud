// A BLOCKING QUESTION HOLDS THE AGENT (lane AU-7, docs/design/agent-autonomy.md
// §3.9, owner decision Q5). An agent that asked with `"blocking": true` is
// parked until the owner answers, holding no capacity. Its questions are in
// the park's `question_wait` record, not in a result summary, so the card
// counts them from there, leads with "this agent is waiting for you", marks
// each blocking question, names the `/answer <n> <option>` reply on its issue
// and links the comment, and says it resumed once the last answer moved it.

import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskQuestions } from '../api'
import type { Result } from '../fetch'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

// One snapshot object: `useSyncExternalStore` re-renders for ever on a new one each call.
const feed = vi.hoisted(() => ({ snapshot: { status: 'loading', since: 0 } as unknown }))

vi.mock('../notificationFeed', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../notificationFeed')>()
  return {
    ...actual,
    notificationsSnapshot: () => feed.snapshot,
    subscribeNotifications: () => () => {},
    markRead: vi.fn(async () => {}),
  }
})

const { QuestionsCard, questionCount } = await import('../QuestionsCard')

const ID = 'task_wait00000000000000'
const COMMENT = 'https://github.com/example-org/widgets/issues/42#issuecomment-7'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function question(text: string, blocking: boolean) {
  return {
    question: text,
    context: null,
    recommended: 'keep',
    options: [
      { label: 'keep', description: 'the old route stays a release' },
      { label: 'drop', description: 'one route' },
    ],
    blocking,
  }
}

function parked(over: Record<string, unknown> = {}) {
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    result_summary: null,
    submitted_by: 'owner@example.com',
    blocked_by: [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'question_wait',
        question_wait: {
          questions: [question('Keep the old route?', true), question('Rename the flag?', false)],
          blocking: [0],
          attempt_id: 'att_1',
        },
      },
    ],
    ...over,
  })
}

function read(open: boolean): TaskQuestions {
  return {
    task_id: ID,
    count: 2,
    status: 'ok',
    attempt_id: 'att_1',
    redacted: false,
    redaction_count: 0,
    may_answer: true,
    may_answer_because: null,
    questions: [
      { ...question('Keep the old route?', true), answer: null },
      { ...question('Rename the flag?', false), answer: null },
    ],
    waiting: {
      open,
      blocking: [0],
      unanswered: open ? [0] : [],
      asked_at: '2026-10-22T10:00:00+00:00',
      expires_at: '2026-10-25T10:00:00+00:00',
      issue_comment_url: COMMENT,
    },
  } as unknown as TaskQuestions
}

beforeEach(() => {
  api.loadQuestions.mockReset()
  localStorage.clear()
})

describe('a blocking question on the card', () => {
  it('counts a parked wait that has no result summary', () => {
    // MUTATION: count only `result_summary.questions` and a parked agent's card never draws.
    expect(questionCount(parked())).toBe(2)
    expect(questionCount(runTask({ id: ID, result_summary: null, blocked_by: [] }))).toBe(0)
    // Adopted into metadata, the wait is still counted once the park is gone.
    expect(
      questionCount(runTask({ id: ID, result_summary: null, blocked_by: [], metadata: { question_wait: { questions: [1] } } })),
    ).toBe(1)
  })

  it('leads with the wait, marks the blocking question and names the reply on the issue', async () => {
    api.loadQuestions.mockResolvedValue(ok(read(true)))
    render(<QuestionsCard task={parked()} />)

    const card = await screen.findByRole('region', { name: 'Questions for you' })
    // MUTATION: drop `WaitingNote` and nothing says the agent is held.
    const note = await within(card).findByRole('status')
    expect(note.textContent).toContain('This agent is waiting for you.')
    expect(note.textContent).toContain('holding no capacity')
    expect(note.textContent).toContain('the blocking question below is answered')
    // MUTATION: drop the reply syntax and an owner on GitHub cannot answer there.
    expect(note.textContent).toContain('/answer <n> <option>')
    expect(within(note).getByRole('link').getAttribute('href')).toBe(COMMENT)

    const items = card.querySelectorAll('.qc-q')
    // MUTATION: drop the chip and every question reads as optional.
    expect(items[0]!.querySelector('.qc-blocking')?.textContent).toBe('blocking')
    expect(items[1]!.querySelector('.qc-blocking')).toBeNull()
  })

  it('says the agent resumed once the wait is closed', async () => {
    api.loadQuestions.mockResolvedValue(ok(read(false)))
    render(<QuestionsCard task={parked({ state: 'READY', park_reason: null, blocked_by: [], metadata: { question_wait: { questions: [1, 2] } } })} />)

    const card = await screen.findByRole('region', { name: 'Questions for you' })
    // MUTATION: draw the open note for a closed wait and a resumed agent still reads as held.
    await waitFor(() => expect(card.textContent).toContain('was resumed when its blocking questions were answered'))
    expect(within(card).queryByRole('status')).toBeNull()
  })
})
