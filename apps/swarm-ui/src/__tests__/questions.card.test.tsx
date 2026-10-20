// "QUESTIONS FOR YOU" (owner report 2026-10-13, part of #1206): the bell said
// "Your agent asked 1 question" and nothing on the agent's page said what it
// was. The task's Details now leads with the questions -- each with its
// context, its options with their descriptions, the recommended one marked --
// the Details tab carries their count, and the notification lands there.
//
// MUTATIONS, one per block: drop the `is-recommended` mark; draw an option's
// description nowhere; read the questions for a task that asked none; leave
// the bell's item unread; draw the Details tab with no count; point the
// notification back at Artifacts.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import STYLES from '../styles.css?raw'
import DETAILS_CSS from '../styles/details.css?raw'
import type { TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { NotificationFeed } from '../api'
import { cascade } from './cssgate'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

const feed = vi.hoisted(() => ({
  snapshot: { status: 'loading', since: 0 } as unknown,
  markRead: vi.fn(async () => {}),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

vi.mock('../notificationFeed', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../notificationFeed')>()
  return {
    ...actual,
    notificationsSnapshot: () => feed.snapshot,
    subscribeNotifications: () => () => {},
    markRead: feed.markRead,
  }
})

const { QuestionsCard, questionCount, questionsNotificationId } = await import('../QuestionsCard')
const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')
const { destinationOf } = await import('../Notifications')
const { pathToAddress } = await import('../paths')

const ID = 'task_e376006b452b4cd38df7'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function asked(over: Record<string, unknown> = {}) {
  return runTask({
    id: ID,
    state: 'SUCCEEDED',
    workflow_id: 'wf_studio',
    step_id: 'design',
    submitted_by: 'owner@example.com',
    result_summary: { artifacts: [{ name: 'questions.json' }], questions: 1, ...over },
  })
}

const QUESTIONS: TaskQuestions = {
  task_id: ID,
  count: 1,
  status: 'ok',
  attempt_id: 'att_1',
  redacted: false,
  redaction_count: 0,
  questions: [
    {
      question: 'Per task or per workflow?',
      context: 'The step writes one file today.',
      recommended: 'per task',
      options: [
        { label: 'per task', description: 'one file per task' },
        { label: 'per workflow', description: 'one file for the run' },
      ],
    },
  ],
}

function feedWith(read: boolean): Result<NotificationFeed> {
  return ok({
    items: [
      {
        id: questionsNotificationId(ID),
        group: 'task',
        needs_you: true,
        title: 'Your agent asked 1 question',
        detail: `Task ${ID}`,
        href: `/agents/recent/${ID}#questions`,
        at: new Date().toISOString(),
        read,
      },
    ],
  } as unknown as NotificationFeed)
}

beforeEach(() => {
  feed.snapshot = { status: 'loading', since: 0 }
  localStorage.clear()
  resetListSnap()
})

describe('the Questions for you card', () => {
  it('draws each question, its context, its options with descriptions, and marks the recommended one', async () => {
    api.loadQuestions.mockResolvedValue(ok(QUESTIONS))
    render(<QuestionsCard task={asked()} />)

    const card = await screen.findByRole('region', { name: 'Questions for you' })
    expect(card.id).toBe('questions')
    await waitFor(() => expect(card.textContent).toContain('Per task or per workflow?'))
    expect(card.textContent).toContain('The step writes one file today.')
    const options = within(card).getByRole('list', { name: 'Options for question 1' })
    const rows = within(options).getAllByRole('listitem')
    expect(rows.map((r) => r.querySelector('.qc-label')?.textContent)).toEqual(['per task', 'per workflow'])
    expect(rows[0]!.className).toContain('is-recommended')
    expect(rows[0]!.textContent).toContain('recommended')
    expect(rows[0]!.textContent).toContain('one file per task')
    expect(rows[1]!.className).not.toContain('is-recommended')
    expect(rows[1]!.textContent).not.toContain('recommended')
    expect(rows[1]!.textContent).toContain('one file for the run')
    expect(api.loadQuestions).toHaveBeenCalledWith(ID)
  })

  it('says who asked and that the answer is stored on the task for whoever launched it', async () => {
    api.loadQuestions.mockResolvedValue(ok(QUESTIONS))
    render(<QuestionsCard task={asked()} />)

    const card = await screen.findByRole('region', { name: 'Questions for you' })
    expect(card.textContent).toContain('This agent (step design) asked you this')
    expect(card.textContent).toContain('whoever launched workflow wf_studio (owner@example.com)')
    expect(card.textContent).not.toContain('no answer box')
  })

  it('draws nothing, and reads nothing, for a task that asked none', () => {
    const { container } = render(<QuestionsCard task={asked({ questions: 0 })} />)
    expect(container.innerHTML).toBe('')
    expect(api.loadQuestions).not.toHaveBeenCalled()
    expect(questionCount(asked({ questions: 0 }))).toBe(0)
    expect(questionCount(asked({ questions: true }))).toBe(0)
    expect(questionCount(asked({ questions: 3 }))).toBe(3)
  })

  it('says why when the counted questions could not be read', async () => {
    api.loadQuestions.mockResolvedValue(
      ok({ ...QUESTIONS, status: 'unavailable', questions: [], unavailable_because: 'the file served is not JSON' }),
    )
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    await waitFor(() => expect(card.textContent).toContain('the file served is not JSON'))
  })
})

// TASK-ANSWERS (owner decision 2026-10-13, "Yes, pick an option on the task").
// MUTATIONS: post the recommended option instead of the one picked; drop the
// free text; draw the saved answer without who answered; leave the buttons
// live for a viewer the API says may not answer.
describe('answering a question on the card', () => {
  const SAVED = {
    question_index: 0,
    question: 'Per task or per workflow?',
    choice: 'per workflow',
    text: null,
    by: 'owner@example.com',
    at: '2026-10-13T12:30:00Z',
    history: [],
  }

  function answered(over: Record<string, unknown> = {}): TaskQuestions {
    return { ...QUESTIONS, questions: [{ ...QUESTIONS.questions[0]!, answer: { ...SAVED, ...over } }] }
  }

  it('posts the option picked, and shows the saved answer with who and when', async () => {
    api.loadQuestions.mockResolvedValue(ok({ ...QUESTIONS, may_answer: true }))
    api.answerQuestion.mockResolvedValue(ok({ ...answered(), may_answer: true }))
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    const group = await within(card).findByRole('radiogroup', { name: 'Answer question 1' })

    const pick = within(group).getByRole('radio', { name: /per workflow/ })
    fireEvent.click(pick)
    expect(pick.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(within(card).getByRole('button', { name: 'Answer' }))

    await waitFor(() =>
      expect(api.answerQuestion).toHaveBeenCalledWith(ID, { question_index: 0, choice: 'per workflow', text: null }),
    )
    const saved = await within(card).findByLabelText('Your answer to question 1')
    expect(saved.textContent).toContain('per workflow')
    expect(saved.textContent).toContain('owner@example.com')
    expect(within(saved).getByRole('button', { name: 'Change' })).toBeTruthy()
  })

  it('posts an "Other…" free-text answer', async () => {
    api.loadQuestions.mockResolvedValue(ok({ ...QUESTIONS, may_answer: true }))
    api.answerQuestion.mockResolvedValue(ok({ ...answered({ choice: null, text: 'both, behind a flag' }), may_answer: true }))
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    const group = await within(card).findByRole('radiogroup', { name: 'Answer question 1' })

    fireEvent.click(within(group).getByRole('radio', { name: /Other/ }))
    fireEvent.change(within(card).getByRole('textbox', { name: 'Your own answer to question 1' }), {
      target: { value: 'both, behind a flag' },
    })
    fireEvent.click(within(card).getByRole('button', { name: 'Answer' }))

    await waitFor(() =>
      expect(api.answerQuestion).toHaveBeenCalledWith(ID, { question_index: 0, choice: null, text: 'both, behind a flag' }),
    )
    const saved = await within(card).findByLabelText('Your answer to question 1')
    expect(saved.textContent).toContain('both, behind a flag')
  })

  it('draws a saved answer, and Change reopens the options with it picked', async () => {
    api.loadQuestions.mockResolvedValue(ok({ ...answered(), may_answer: true }))
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    const saved = await within(card).findByLabelText('Your answer to question 1')
    expect(saved.textContent).toContain('per workflow')
    expect(within(card).queryByRole('radiogroup')).toBeNull()

    fireEvent.click(within(saved).getByRole('button', { name: 'Change' }))

    const group = within(card).getByRole('radiogroup', { name: 'Answer question 1' })
    expect(within(group).getByRole('radio', { name: /per workflow/ }).getAttribute('aria-checked')).toBe('true')
  })

  it('is disabled, with the reason, for a viewer who may not answer', async () => {
    api.loadQuestions.mockResolvedValue(
      ok({ ...QUESTIONS, may_answer: false, may_answer_because: 'only owner@example.com, who launched this task, or an admin may answer' }),
    )
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    const group = await within(card).findByRole('radiogroup', { name: 'Answer question 1' })

    for (const r of within(group).getAllByRole('radio')) expect((r as HTMLButtonElement).disabled).toBe(true)
    expect((within(card).getByRole('button', { name: 'Answer' }) as HTMLButtonElement).disabled).toBe(true)
    expect(card.textContent).toContain('only owner@example.com, who launched this task, or an admin may answer')
  })

  it('says why when the answer was refused, and keeps the pick', async () => {
    api.loadQuestions.mockResolvedValue(ok({ ...QUESTIONS, may_answer: true }))
    api.answerQuestion.mockResolvedValue({
      status: 'error',
      error: { kind: 'invalid', httpStatus: 422, code: null, message: 'choice is not one of the options' },
    })
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    const group = await within(card).findByRole('radiogroup', { name: 'Answer question 1' })
    fireEvent.click(within(group).getByRole('radio', { name: /per task/ }))
    fireEvent.click(within(card).getByRole('button', { name: 'Answer' }))

    await waitFor(() => expect(card.textContent).toContain('The answer was not saved'))
    expect(within(group).getByRole('radio', { name: /per task/ }).getAttribute('aria-checked')).toBe('true')
  })
})

// ANSWER-SYNC-UI (part of #1644, owner decision 2026-10-19, "Plugin records
// it, marked as from Claude Code"): an answer the plugin's swarm_task_answer
// posted carries `via: 'claude-code'` and the session, and the card says so
// under the answer. A console answer, or one stored before `via` existed,
// looks as it always did.
// MUTATIONS: draw the line for every answer; print the whole session id; drop
// the line out of the question's column, where nothing wraps it at 390 px.
describe('an answer recorded from Claude Code', () => {
  // A Claude Code session id is a UUID; the card shows its first 8 characters.
  const SESSION = 'ce05454f-6d0b-4a8e-9a63-2f1c0d7e5b21'

  function answeredVia(via: string | null, session: string | null): TaskQuestions {
    const answer = {
      question_index: 0,
      question: 'Per task or per workflow?',
      choice: 'per workflow',
      text: null,
      by: 'owner@example.com',
      at: new Date(Date.now() - 3 * 3600_000).toISOString(),
      via,
      session,
      history: [],
    }
    return { ...QUESTIONS, may_answer: true, questions: [{ ...QUESTIONS.questions[0]!, answer }] } as TaskQuestions
  }

  async function savedCard(data: TaskQuestions): Promise<HTMLElement> {
    api.loadQuestions.mockResolvedValue(ok(data))
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    await within(card).findByLabelText('Your answer to question 1')
    return card
  }

  it('shows "answered in Claude Code" with the session’s first 8 characters and the time', async () => {
    const card = await savedCard(answeredVia('claude-code', SESSION))
    const line = card.querySelector('.qc-via')
    expect(line, 'no Claude Code line under the answer').not.toBeNull()
    expect(line!.textContent).toContain('answered in Claude Code')
    expect(line!.textContent).toContain('(session ce05454f)')
    expect(line!.textContent).not.toContain(SESSION.slice(0, 9))
    expect(line!.textContent).toContain('3h ago')
    expect(line!.className).toContain('dt-note')
  })

  it('shows no line for a console answer', async () => {
    const card = await savedCard(answeredVia('console', null))
    expect(card.querySelector('.qc-via')).toBeNull()
    expect(card.textContent).not.toContain('Claude Code')
  })

  it('shows no line for an answer stored before `via` existed', async () => {
    const card = await savedCard(answeredVia(null, null))
    expect(card.querySelector('.qc-via')).toBeNull()
    expect(card.textContent).not.toContain('Claude Code')
  })

  it('keeps the line inside the card at 390 px', async () => {
    const card = await savedCard(answeredVia('claude-code', SESSION))
    const line = card.querySelector('.qc-via')!
    expect(line).not.toBeNull()
    expect(card.contains(line)).toBe(true)
    // Under the answer, in the question's own column, which may shrink.
    const column = line.closest('.qc-q')!
    expect(column, 'the line is not in the question’s column').not.toBeNull()
    const sheet = `${STYLES}\n${DETAILS_CSS}`
    const env = { width: 390 }
    const shrink = cascade(sheet, column, 'min-width', env)
    expect(shrink.unsupported).toEqual([])
    expect(shrink.winner?.value).toBe('0')
    // A long word in it still breaks rather than running past the card's edge.
    const wrap = cascade(sheet, line, ['overflow-wrap', 'word-wrap'], env)
    expect(wrap.unsupported).toEqual([])
    expect(wrap.winner?.value).toBe('anywhere')
  })
})

describe('opening the card reads the notification', () => {
  it('marks the bell’s item read once, while the feed lists it unread', async () => {
    feed.snapshot = feedWith(false)
    api.loadQuestions.mockResolvedValue(ok(QUESTIONS))
    render(<QuestionsCard task={asked()} />)
    await waitFor(() => expect(feed.markRead).toHaveBeenCalledWith([questionsNotificationId(ID)]))
    expect(feed.markRead).toHaveBeenCalledTimes(1)
  })

  it('does not post a read for an item already read', async () => {
    feed.snapshot = feedWith(true)
    api.loadQuestions.mockResolvedValue(ok(QUESTIONS))
    render(<QuestionsCard task={asked()} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' })
    await waitFor(() => expect(card.textContent).toContain('Per task or per workflow?'))
    expect(feed.markRead).not.toHaveBeenCalled()
  })
})

describe('the Details tab badge', () => {
  function detailsTab(): HTMLElement {
    const list = screen.getByRole('tablist', { name: 'Agent panes' })
    const t = within(list)
      .getAllByRole('tab')
      .find((b) => b.querySelector('.c-tab-label')?.textContent === 'Details')
    expect(t, 'no Details tab').toBeTruthy()
    return t!
  }

  function split() {
    api.loadAgentRun.mockReturnValue(new Promise(() => {}))
    api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
    api.loadTranscript.mockReturnValue(new Promise(() => {}))
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
    api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
    return (
      <div className="app has-inspector">
        <AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/recent" go={() => {}} base={`work/task/${ID}`} />
      </div>
    )
  }

  it('counts the agent’s questions on the Details tab', async () => {
    api.loadTask.mockResolvedValue(ok(asked({ questions: 2 })))
    render(split())
    await waitFor(() => expect(detailsTab().querySelector('em')?.textContent).toBe('2'))
  })

  it('carries no badge when the agent asked none', async () => {
    api.loadTask.mockResolvedValue(ok(asked({ questions: 0 })))
    render(split())
    await waitFor(() => expect(screen.getByRole('tablist', { name: 'Agent panes' })).toBeTruthy())
    await waitFor(() => expect(api.loadTask).toHaveBeenCalled())
    expect(detailsTab().querySelector('em')).toBeNull()
  })
})

describe('the notification opens the task’s Details at #questions', () => {
  it('resolves the href the API now sends to the agent’s Details pane, not Artifacts', () => {
    const href = `/agents/recent/${ID}#questions`
    expect(pathToAddress(href.split('#')[0]!)?.address).toBe(`work/task/${ID}`)
    expect(destinationOf(href)).toEqual(['Work', 'Agents', ID])
  })
})
