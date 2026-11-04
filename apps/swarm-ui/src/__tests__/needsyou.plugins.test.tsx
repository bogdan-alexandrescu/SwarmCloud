// NEEDS YOU, A PLUGIN STEP'S PARKED QUESTION (the plugins console's picked
// interview, direction B's Needs-you screen; part of #2688). A question row
// whose task is a plugin step names the plugin, the mode, the skill that asked
// and the time left of its wait; beside the list, one card per parked plugin
// step. Every value is read from what swarm-api serves: the plugin and mode
// from the task's work contract (`GET /v1/tasks/{id}`), the clock from the
// wait (`GET /v1/tasks/{id}/questions`, `waiting`). The skill that asked is
// not served, so it reads "not known"; no interview route exists yet, so a
// planner or designer card says the interview opens from its run's page.
//
// MUTATIONS, one per block: draw no plugin line or card for a plugin step;
// invent the skill or a time ("0 min"); draw a card for a step that is no
// plugin step; link to a route that does not exist; keep the cards under a
// chip that hides questions; read a task twice, or read a question whose
// words are on its page.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  answerQuestion: vi.fn(),
  approveApproval: vi.fn(),
  loadTaskInputOnce: vi.fn(),
  rerunWorkflowStep: vi.fn(),
  loadTask: vi.fn(),
  loadQuestions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { NeedsYou, pluginsOf, leftOfWait } = await import('../work/NeedsYou')
type NeedItem = import('../work/NeedsYou').NeedItem

const HOUR = 3_600_000
const iso = (ms: number) => new Date(ms).toISOString()

function pluginTask(id: string, mode: string, over: Partial<Task> = {}): Task {
  return runTask({
    id,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    workflow_id: 'wf_contacts',
    step_id: mode === 'unattended' ? 'review' : 'design',
    repository_url: 'https://github.com/example-org/contacts-app',
    metadata: { work_contract: { capabilities: { plugins: { gstack: { mode, skills: ['review', 'design-html'] } } } } },
    ...over,
  })
}

function questions(taskId: string, waiting: Record<string, unknown> | null): TaskQuestions {
  return {
    task_id: taskId,
    count: 1,
    status: 'ok',
    questions: [],
    redacted: false,
    redaction_count: 0,
    ...(waiting === null ? {} : { waiting }),
  } as TaskQuestions
}

/** A wait asked so that 23 h 22 min (and some seconds) of its 24 hours are left. */
function waiting(left = 23 * HOUR + 22 * 60_000 + 30_000, span = 24 * HOUR) {
  const expires = Date.now() + left
  return { open: true, blocking: [0], unanswered: [0], asked_at: iso(expires - span), expires_at: iso(expires) }
}

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

function question(taskId: string, over: Partial<Extract<NeedItem, { kind: 'question' }>> = {}): NeedItem {
  return {
    kind: 'question',
    taskId,
    index: 0,
    title: `wf_contacts › ${taskId}`,
    question: 'Which layout?',
    options: [
      { label: 'Cards', description: 'one card a contact' },
      { label: 'Table', description: 'one row a contact' },
    ],
    recommended: null,
    ...over,
  }
}

function serve(tasks: Record<string, Task>, waits: Record<string, Record<string, unknown> | null>) {
  api.loadTask.mockImplementation(async (id: string) => (id in tasks ? ok(tasks[id]!) : { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no' } }))
  api.loadQuestions.mockImplementation(async (id: string) => ok(questions(id, waits[id] ?? null)))
}

async function settle() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve()
  })
}

const cards = () => screen.queryByRole('complementary', { name: 'Plugin steps parked on a question' })

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
})

describe("a plugin step's question row", () => {
  it('names the plugin, the mode, the skill that asked as not known, and the time left of its 24 hours', async () => {
    serve({ t_review: pluginTask('t_review', 'unattended') }, { t_review: waiting() })
    render(<NeedsYou items={[question('t_review')]} approvals={[]} />)
    await settle()
    const row = screen.getByRole('listitem')
    const line = within(row).getByText(/gstack/)
    expect(line.textContent).toContain('gstack · Unattended')
    expect(line.textContent).toContain('skill that asked: not known')
    expect(line.textContent).toContain('23 h 22 min left of 24 h')
    expect(line.textContent).toContain('never answered for anyone')
  })

  it('keeps the plain row for a question whose task is no plugin step, and draws no card', async () => {
    serve({ t_plain: runTask({ id: 't_plain', state: 'PARKED', park_reason: 'HUMAN_REQUIRED', metadata: {} }) }, { t_plain: waiting() })
    render(<NeedsYou items={[question('t_plain')]} approvals={[]} />)
    await settle()
    expect(screen.getByRole('listitem').textContent).not.toContain('gstack')
    expect(cards()).toBeNull()
    expect(api.loadQuestions).not.toHaveBeenCalled()
  })

  it('says the time left is not known when the wait served no clock, never 0', async () => {
    serve({ t_review: pluginTask('t_review', 'unattended') }, { t_review: null })
    render(<NeedsYou items={[question('t_review')]} approvals={[]} />)
    await settle()
    const text = screen.getByRole('listitem').textContent ?? ''
    expect(text).toContain('time left not known')
    expect(text).not.toMatch(/\b0 (h|min)\b/)
  })

  it('says the time is up once the wait has passed', async () => {
    serve({ t_review: pluginTask('t_review', 'unattended') }, { t_review: waiting(-60_000) })
    render(<NeedsYou items={[question('t_review')]} approvals={[]} />)
    await settle()
    expect(screen.getByRole('listitem').textContent).toContain('time is up')
  })
})

describe('the parked plugin steps beside the list', () => {
  it('draws one card for an unattended step: workflow, repository, plugin and mode, skill, time left, answer it in its row', async () => {
    serve({ t_review: pluginTask('t_review', 'unattended') }, { t_review: waiting() })
    render(<NeedsYou items={[question('t_review'), question('t_review', { index: 1, question: 'Which font?' })]} approvals={[]} />)
    await settle()
    const side = cards()!
    expect(side).toBeTruthy()
    expect(within(side).getByRole('heading').textContent).toMatch(/Plugin steps parked\s*1/)
    expect(side.textContent).toContain('Each holds no machine while it waits. Unanswered in 24 hours, it ends with the question open')
    const card = within(side).getByRole('article')
    expect(card.textContent).toContain('review')
    expect(card.textContent).toContain('wf_contacts')
    expect(card.textContent).toContain('example-org/contacts-app')
    expect(card.textContent).toContain('gstack')
    expect(card.textContent).toContain('Unattended')
    expect(card.textContent).toContain('asked by: not known')
    expect(card.textContent).toContain('23 h 22 min')
    expect(card.textContent).toContain('no recommended answer')
    expect(card.textContent).toContain('answer it in its row (Question, wf_contacts › t_review)')
    expect(api.loadTask).toHaveBeenCalledTimes(1)
    expect(api.loadQuestions).toHaveBeenCalledTimes(1)
  })

  it("says a designer step's interview opens from its run's page, linking the run, not a route that does not exist", async () => {
    serve({ t_design: pluginTask('t_design', 'designer') }, { t_design: waiting() })
    render(<NeedsYou items={[question('t_design', { recommended: 'Cards' })]} approvals={[]} />)
    await settle()
    const card = within(cards()!).getByRole('article')
    expect(card.textContent).toContain('Designer')
    expect(card.textContent).toContain("Its interview opens from its run's page")
    const link = within(card).getByRole('link', { name: /Open its run/ })
    expect(link.getAttribute('href')).toContain('work/task/t_design')
    expect(card.textContent).not.toContain('Open the interview')
    expect(screen.getByRole('listitem').textContent).toContain('gstack · Designer')
  })

  it('hides the cards when a chip picks a kind other than questions', async () => {
    serve({ t_review: pluginTask('t_review', 'unattended') }, { t_review: waiting() })
    const failed: NeedItem = { kind: 'failed', task: runTask({ id: 'f1', state: 'FAILED', workflow_id: null, step_id: null }), title: 'agent f1', why: 'no' }
    render(<NeedsYou items={[question('t_review'), failed]} approvals={[]} />)
    await settle()
    expect(cards()).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^Failed: 1$/ }))
    expect(cards()).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^Question: 1$/ }))
    expect(cards()).toBeTruthy()
  })

  it('reads nothing for a question whose words are on its page', async () => {
    serve({}, {})
    render(<NeedsYou items={[question('t_page', { question: null, onItsPage: true })]} approvals={[]} />)
    await settle()
    expect(api.loadTask).not.toHaveBeenCalled()
    expect(cards()).toBeNull()
  })
})

describe('the served values, read', () => {
  it("reads each plugin's name and mode from the task's work contract, and nothing from a malformed one", () => {
    expect(pluginsOf(pluginTask('a', 'planner'))).toEqual([{ name: 'gstack', mode: 'planner' }])
    expect(pluginsOf(runTask({ metadata: { work_contract: { capabilities: { plugins: { gstack: 'x' } } } } }))).toEqual([])
    expect(pluginsOf(runTask({ metadata: null }))).toEqual([])
  })

  it('words the time left in hours and minutes, and null once it has passed or is not known', () => {
    const now = Date.parse('2026-11-04T12:00:00Z')
    expect(leftOfWait(now + 23 * HOUR + 22 * 60_000 + 5_000, now)).toBe('23 h 22 min')
    expect(leftOfWait(now + 9 * 60_000 + 5_000, now)).toBe('9 min')
    expect(leftOfWait(now + 20_000, now)).toBe('under a minute')
    expect(leftOfWait(now - 1, now)).toBeNull()
    expect(leftOfWait(null, now)).toBeNull()
  })
})
