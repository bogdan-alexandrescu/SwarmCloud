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

import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { NotificationFeed } from '../api'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadQuestions: vi.fn(),
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

  it('says who asked and that the answer goes back through whoever launched it, with no answer box', async () => {
    api.loadQuestions.mockResolvedValue(ok(QUESTIONS))
    render(<QuestionsCard task={asked()} />)

    const card = await screen.findByRole('region', { name: 'Questions for you' })
    expect(card.textContent).toContain('This agent (step design) asked you this')
    expect(card.textContent).toContain('whoever launched workflow wf_studio (owner@example.com)')
    expect(card.textContent).toContain('no answer box here yet')
    expect(card.querySelector('textarea, input, button')).toBeNull()
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
