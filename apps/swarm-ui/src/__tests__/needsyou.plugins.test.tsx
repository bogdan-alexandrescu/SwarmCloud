// NEEDS YOU, A PLUGIN STEP'S PARKED QUESTION (the plugins console's picked
// interview, direction B's Needs-you screen; part of #2688). A question row
// whose task is a plugin step names the plugin, the mode, the skill that asked
// and the time left of its wait; beside the list, one card per parked plugin
// step. Every value is what swarm-api serves in the list read (the owner's
// pick on PR #3262, "Serve them in step_needs"): the step's
// `task_summary.needs.plugin_ask` (`codec.step_needs`), which `stepNeedItems`
// puts on the step's question rows. Nothing is read per task. The skill that
// asked is the worker's record of the last Skill call before the question
// (the owner's pick of 4 November 2026); a park with none recorded reads
// "not known", never a guess. A planner or designer card opens its interview,
// a full page (`/runs?interview=<task>`, InterviewPage.tsx), and links its run.
//
// MUTATIONS, one per block: draw no plugin line or card for a plugin step;
// invent the skill or a time ("0 min"); draw a card for a step that is no
// plugin step; link to a route that does not exist; keep the cards under a
// chip that hides questions; read a task or its questions to tell a plugin
// step; take a plugin_ask for a step that is not parked.

import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { StepNeeds } from '../stepviews'
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

const { NeedsYou, pluginAskOf, leftOfWait } = await import('../work/NeedsYou')
const { stepNeedItems } = await import('../work/listNeeds')
type NeedItem = import('../work/NeedsYou').NeedItem

const HOUR = 3_600_000
const iso = (ms: number) => new Date(ms).toISOString()

function stepTask(id: string, over: Partial<Task> = {}): Task {
  return runTask({
    id,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    workflow_id: 'wf_contacts',
    step_id: id === 't_design' ? 'design' : 'review',
    repository_url: null,
    metadata: {},
    ...over,
  })
}

/** A wait asked so that 23 h 22 min (and some seconds) of its 24 hours are left. */
function clock(left = 23 * HOUR + 22 * 60_000 + 30_000, span = 24 * HOUR) {
  const expires = Date.now() + left
  return { asked_at: iso(expires - span), expires_at: iso(expires) }
}

/** A plugin step's `plugin_ask`, as `codec.step_needs` serves it. */
function pluginAsk(mode: string, wait: { asked_at: string | null; expires_at: string | null } = clock(), skill: string | null = null): NonNullable<StepNeeds['plugin_ask']> {
  return { plugins: [{ name: 'gstack', mode }], repository_url: 'https://github.com/example-org/contacts-app', ...wait, skill }
}

const OPTIONS = [
  { label: 'Cards', description: 'one card a contact' },
  { label: 'Table', description: 'one row a contact' },
]

/** The question rows of a step whose needs block served `ask` and these questions. */
function rows(task: Task, ask: StepNeeds['plugin_ask'], qs: { index: number; question: string; recommended?: string | null }[] = [{ index: 0, question: 'Which layout?' }]): NeedItem[] {
  const needs: StepNeeds = {
    question_wait: true,
    questions: qs.map((q) => ({ index: q.index, question: q.question, options: OPTIONS, recommended: q.recommended ?? null })),
    questions_unread: [],
    plugin_ask: ask,
  }
  return stepNeedItems(task, needs, `wf_contacts › ${task.id}`)
}

const cards = () => screen.queryByRole('complementary', { name: 'Plugin steps parked on a question' })
const header = () => screen.getByRole('button', { name: /^Needs you/ })

/** Renders the strip and opens it: it starts collapsed, its rows and cards in its body. */
function show(ui: JSX.Element) {
  const r = render(ui)
  fireEvent.click(header())
  return r
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
})

describe("a plugin step's question row", () => {
  it('names the plugin, the mode, the skill that asked as not known, and the time left of its 24 hours', () => {
    show(<NeedsYou items={rows(stepTask('t_review'), pluginAsk('unattended'))} approvals={[]} />)
    const row = screen.getByRole('listitem')
    const line = row.querySelector('.ny-plugin')!
    expect(line.textContent).toContain('gstack · Unattended')
    expect(line.textContent).toContain('skill that asked: not known')
    expect(line.textContent).toContain('23 h 22 min left of 24 h')
    expect(line.textContent).toContain('never answered for anyone')
  })

  it('names the skill that asked when the list read served it, on the row and on its card', () => {
    show(<NeedsYou items={rows(stepTask('t_review'), pluginAsk('unattended', clock(), 'gstack:office-hours'))} approvals={[]} />)
    const line = screen.getByRole('listitem').querySelector('.ny-plugin')!
    expect(line.textContent).toContain('asked by gstack:office-hours')
    expect(line.textContent).not.toContain('not known')
    const card = within(cards()!).getByRole('article')
    expect(card.textContent).toContain('asked by gstack:office-hours')
    expect(card.textContent).not.toContain('not known')
    expect(api.loadTask).not.toHaveBeenCalled()
    expect(api.loadQuestions).not.toHaveBeenCalled()
  })

  it('keeps the plain row for a question whose step served no plugin_ask, and draws no card', () => {
    show(<NeedsYou items={rows(stepTask('t_plain'), null)} approvals={[]} />)
    expect(screen.getByRole('listitem').textContent).not.toContain('gstack')
    expect(cards()).toBeNull()
  })

  it('says the time left is not known when the wait served no clock, never 0', () => {
    show(<NeedsYou items={rows(stepTask('t_review'), pluginAsk('unattended', { asked_at: null, expires_at: null }))} approvals={[]} />)
    const text = screen.getByRole('listitem').textContent ?? ''
    expect(text).toContain('time left not known')
    expect(text).not.toMatch(/\b0 (h|min)\b/)
  })

  it('says the time is up once the wait has passed', () => {
    show(<NeedsYou items={rows(stepTask('t_review'), pluginAsk('unattended', clock(-60_000)))} approvals={[]} />)
    expect(screen.getByRole('listitem').textContent).toContain('time is up')
  })
})

describe('the parked plugin steps beside the list', () => {
  it('draws one card for an unattended step: workflow, repository, plugin and mode, skill, time left, answer it in its row', () => {
    const items = rows(stepTask('t_review'), pluginAsk('unattended'), [
      { index: 0, question: 'Which layout?' },
      { index: 1, question: 'Which font?' },
    ])
    show(<NeedsYou items={items} approvals={[]} />)
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
    // Told from the list read alone: no task and no questions are read.
    expect(api.loadTask).not.toHaveBeenCalled()
    expect(api.loadQuestions).not.toHaveBeenCalled()
  })

  it("opens a designer step's interview as its full page, and links its run beside it", () => {
    show(<NeedsYou items={rows(stepTask('t_design'), pluginAsk('designer'), [{ index: 0, question: 'Which layout?', recommended: 'Cards' }])} approvals={[]} />)
    const card = within(cards()!).getByRole('article')
    expect(card.textContent).toContain('Designer')
    const open = within(card).getByRole('link', { name: /Open the interview/ })
    expect(open.getAttribute('href')).toBe('/runs?interview=t_design')
    const link = within(card).getByRole('link', { name: /Open its run/ })
    expect(link.getAttribute('href')).toMatch(/\/t_design$/)
    expect(screen.getByRole('listitem').textContent).toContain('gstack · Designer')
  })

  it('hides the cards while the strip is collapsed, and shows them once it is opened', () => {
    render(<NeedsYou items={rows(stepTask('t_review'), pluginAsk('unattended'))} approvals={[]} />)
    expect(cards()).toBeNull()
    fireEvent.click(header())
    expect(cards()).toBeTruthy()
  })

  it('hides the cards when "N more" picks a kind other than questions, and shows them once folded and opened again', () => {
    const failed = (id: string): NeedItem => ({ kind: 'failed', task: runTask({ id, state: 'FAILED', workflow_id: null, step_id: null }), title: `agent ${id}`, why: 'no' })
    show(<NeedsYou items={[...rows(stepTask('t_review'), pluginAsk('unattended')), failed('f1'), failed('f2'), failed('f3')]} approvals={[]} />)
    expect(cards()).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '1 more failed' }))
    expect(cards()).toBeNull()
    fireEvent.click(header())
    fireEvent.click(header())
    expect(cards()).toBeTruthy()
  })

  it('draws a card for every parked plugin step in the read, past the ten the per-task reads stopped at', () => {
    const items = Array.from({ length: 12 }, (_, i) => rows(stepTask(`t_${i}`, { step_id: `step_${i}` }), pluginAsk('unattended'))).flat()
    show(<NeedsYou items={items} approvals={[]} />)
    expect(within(cards()!).getAllByRole('article')).toHaveLength(12)
    expect(api.loadTask).not.toHaveBeenCalled()
  })
})

describe('the served values, read', () => {
  it("reads each plugin's name and mode from the served plugin_ask, and nothing from a malformed one or a step not parked", () => {
    const parked = stepTask('a')
    expect(pluginAskOf(parked, { plugin_ask: pluginAsk('planner') })?.plugins).toEqual([{ name: 'gstack', mode: 'planner' }])
    expect(pluginAskOf(parked, { plugin_ask: pluginAsk('planner') })?.repository).toBe('https://github.com/example-org/contacts-app')
    expect(pluginAskOf(parked, { plugin_ask: pluginAsk('planner') })?.skill).toBeNull()
    expect(pluginAskOf(parked, { plugin_ask: pluginAsk('planner', clock(), 'gstack:office-hours') })?.skill).toBe('gstack:office-hours')
    expect(pluginAskOf(parked, { plugin_ask: { ...pluginAsk('planner'), skill: '  ' } })?.skill).toBeNull()
    expect(pluginAskOf(parked, { plugin_ask: { plugins: [{ name: 'gstack', mode: null }], repository_url: null, asked_at: null, expires_at: null } })).toBeNull()
    expect(pluginAskOf(parked, { plugin_ask: null })).toBeNull()
    expect(pluginAskOf(parked, null)).toBeNull()
    expect(pluginAskOf(stepTask('b', { state: 'RUNNING' }), { plugin_ask: pluginAsk('planner') })).toBeNull()
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
