// A MERGE STEP HELD BY THE OWNER'S QUESTIONS IS DRAWN AS THOSE QUESTIONS, NOT
// AS A BROWSER SIGN-IN (MERGE-PARK-QUESTIONS, #2313).
//
// BGD, 2026-10-25 ~07:38Z, on task_7ee2d99642c84dc0b3b2 (GA-MOVE-VIEWS'
// merge, held by 2 open questions): "why a merge step in a workflow needs to
// use a browser agent and also needs user input to move forward?" The merge
// (runner profile `merge`: git only, no browser) parks PARKED(HUMAN_REQUIRED)
// with `park_phase: owner_questions` in its `blocked_by` entry
// (agent_worker.lifecycle._park_for_owner_questions), and the console drew
// the live-browser hand-off for it: the yellow banner, I'm ready (which only
// re-ran the merge into the same park, 07:32:52Z on task_ad66d2b402a847ca8b7d),
// and the Browser tab. The answer that clears the last named question returns
// it to READY by itself (QUESTION-WAKE), so what the page owes the owner is
// the questions and the answer control.
//
// MUTATIONS: drop the owner-question exclusion from `handoffOf`; drop the
// `held` guard on the Browser tab in AgentSplit; render `HeldQuestionsCard`
// nowhere; answer on the merge task instead of the asking one.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { Task, Waiting } from '../types'
import { at, ev, task as runTask } from './runfixture'

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
const { Run } = await import('../AgentDetail')
const { handoffOf, needsYou, ownerQuestionsHold } = await import('../Handoff')
const { fromAddress } = await import('../App')
const { resetListSnap } = await import('../listSnap')
const { waitingShort } = await import('../waitingShort')

const ID = 'task_7ee2d99642c84dc0b3b2'
const IMPL = 'task_a1b2c3d4e5f6a7b8c9d0'
const FIX = 'task_f0e1d2c3b4a5968778695'
const NOW = Date.now()
const iso = (ms: number) => new Date(ms).toISOString()
const SPLIT = { timeout: 12_000 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const HELD = [
  { task_id: IMPL, step_id: 'implement', question_index: 0, question: 'Keep the old route as a redirect?' },
  { task_id: FIX, step_id: 'fix', question_index: 1, question: 'Rename the flag now or in a follow-up?' },
]

const SUMMARY = 'Waiting for answers to 2 owner questions before merging (implement: 1, fix: 1)'

function waiting(over: Partial<Waiting> = {}): Waiting {
  return {
    reason: 'HUMAN_REQUIRED',
    summary: SUMMARY,
    since: iso(NOW - 9 * 60_000),
    next_check_at: null,
    resumes_when: 'the last named question is answered (it resumes on its own)',
    detail: {
      park_phase: 'owner_questions',
      questions: HELD,
      unreadable: 0,
      who_may_answer: { submitted_by: 'bgd@example.com', admin: true, text: 'bgd@example.com, who launched workflow wf_7548, or an admin' },
    },
    history: [],
    history_read: true,
    read_at: iso(NOW),
    ...over,
  }
}

/** The merge step, parked on the owner's questions as the worker writes it. */
function mergeHeld(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'merge',
    workflow_id: 'wf_7548cc6f627f42769919',
    step_id: 'merge',
    submitted_by: 'bgd@example.com',
    started_at: at(1),
    completed_at: null,
    result_summary: null,
    next_eligible_at: iso(NOW - 9 * 60_000),
    blocked_by: [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { code: 'owner_questions_open', questions: HELD, unreadable: 0 },
      } as unknown as NonNullable<Task['blocked_by']>[number],
    ],
    waiting: waiting(),
    ...over,
  })
}

/** A browser agent's hand-off that nobody took in time: the real sign-in park. */
function handOff(): Task {
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    started_at: at(1),
    completed_at: null,
    result_summary: null,
    next_eligible_at: iso(NOW - 60_000),
    blocked_by: [{ reason: 'HUMAN_REQUIRED' } as unknown as NonNullable<Task['blocked_by']>[number]],
    waiting: waiting({
      summary: 'Waiting for a person: sign in on its console page, then say it is ready',
      resumes_when: "a member of the tenant calls human-ready (the console's Ready button)",
      detail: {
        who_may_answer: { tenant_id: 'acme', text: 'any member of tenant acme' },
        expires_at: null,
        answer_route: `POST /v1/tasks/${ID}/human-ready`,
        cause: 'hold_expired',
      },
    }),
  })
}

function asked(taskId: string, n: number): TaskQuestions {
  return {
    task_id: taskId,
    count: n,
    status: 'ok',
    redacted: false,
    redaction_count: 0,
    may_answer: true,
    questions: Array.from({ length: n }, (_, i) => ({
      question: `${taskId} question ${i}`,
      options: [
        { label: 'Yes', description: `yes to ${i}` },
        { label: 'No', description: `no to ${i}` },
      ],
      recommended: 'Yes',
      context: null,
      answer: null,
    })),
  }
}

type Pane = 'detail' | 'logs' | 'browser'

function Routed({ start = 'detail', onGo }: { start?: Pane; onGo: (to: string) => void }) {
  const [p, setP] = useState<Pane>(start)
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane={p}
        theater={false}
        artifact={null}
        closeTo="work/waiting"
        go={(to) => {
          onGo(to)
          setP(fromAddress(to).taskPane as Pane)
        }}
        base={`work/task/${ID}`}
      />
    </div>
  )
}

const tabLabels = () =>
  within(screen.getByRole('tablist', { name: 'Agent panes' }))
    .getAllByRole('tab')
    .map((t) => t.querySelector('.c-tab-label')?.textContent ?? t.textContent ?? '')

function run(t: Task): AgentRun {
  return {
    task: t,
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts: [],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

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
  api.loadQuestions.mockImplementation(async (id: string) => ok(asked(id, 2)))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the hold is told apart from a hand-off', () => {
  it('reads the owner-question hold off blocked_by, and only off a HUMAN_REQUIRED park', () => {
    expect(ownerQuestionsHold(mergeHeld())?.questions.map((q) => [q.task_id, q.question_index])).toEqual([
      [IMPL, 0],
      [FIX, 1],
    ])
    expect(ownerQuestionsHold(handOff())).toBeNull()
    expect(ownerQuestionsHold(mergeHeld({ state: 'READY', park_reason: null }))).toBeNull()
  })

  it('is no hand-off: no banner, no I am ready, no "needs you" browser row', () => {
    expect(handoffOf(mergeHeld())).toBeNull()
    expect(needsYou(mergeHeld())).toBe(false)
    expect(handoffOf(handOff())?.phase).toBe('parked')
  })

  it('says it waits for answers in the short line, never "sign in"', () => {
    expect(waitingShort({ reason: 'HUMAN_REQUIRED', summary: SUMMARY })).toBe('answers: 2 owner questions')
    expect(waitingShort({ reason: 'HUMAN_REQUIRED', summary: 'Waiting for a person: sign in on its console page, then say it is ready' })).toBe(
      'needs a person to sign in',
    )
  })
})

describe("the merge step's page", () => {
  it('draws no Browser tab, no hand-off banner and no I am ready', async () => {
    api.loadTask.mockResolvedValue(ok(mergeHeld()))
    render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(tabLabels()).toContain('Details'), SPLIT)
    // The read has landed: the head names the step.
    await waitFor(() => expect(document.querySelector('.ag-head')?.textContent ?? '').toContain('merge'), SPLIT)
    expect(tabLabels()).not.toContain('Browser')
    expect(document.querySelector('.ho-strip')).toBeNull()
    expect(screen.queryByRole('button', { name: /I.m ready/ })).toBeNull()
  })

  it('sends a Browser address (the bell links one) to Details, and draws no Browser tab there', async () => {
    api.loadTask.mockResolvedValue(ok(mergeHeld()))
    const went: string[] = []
    render(<Routed start="browser" onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(went).toContain(`work/task/${ID}`), SPLIT)
    await waitFor(() => expect(tabLabels()).not.toContain('Browser'), SPLIT)
  })

  it('a real hand-off park keeps the banner, I am ready and the Browser tab', async () => {
    api.loadTask.mockResolvedValue(ok(handOff()))
    render(<Routed start="browser" onGo={() => {}} />)
    await waitFor(() => expect(tabLabels()).toContain('Browser'), SPLIT)
    api.loadTask.mockResolvedValue(ok(handOff()))
    const other = render(<Routed onGo={() => {}} />)
    await within(other.container).findByRole('button', { name: /I.m ready/ }, SPLIT)
    expect(other.container.querySelector('.ho-strip')).not.toBeNull()
  })
})

describe('the questions are drawn inline, with the answer control', () => {
  async function draw(t: Task): Promise<HTMLElement> {
    const { container } = render(<Run run={run(t)} />)
    await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
    return container
  }

  it('lists each held question with its options, links its asking task, and answers on that task', async () => {
    api.answerQuestion.mockImplementation(async (id: string) => ok(asked(id, 2)))
    const c = await draw(mergeHeld())
    const card = await within(c).findByRole('region', { name: 'Questions holding this merge' })
    await waitFor(() => expect(within(card).getAllByRole('radiogroup')).toHaveLength(2))
    // Only the named index of each asking task, in the asking task's words.
    expect(card.textContent).toContain(`${IMPL} question 0`)
    expect(card.textContent).not.toContain(`${IMPL} question 1`)
    expect(card.textContent).toContain(`${FIX} question 1`)
    expect(card.textContent).not.toContain(`${FIX} question 0`)
    const links = within(card).getAllByRole('link').map((a) => a.getAttribute('href') ?? '')
    expect(links.some((h) => h.includes(IMPL))).toBe(true)
    expect(links.some((h) => h.includes(FIX))).toBe(true)
    expect(c.textContent).not.toMatch(/sign in/i)
    expect(within(c).queryByRole('button', { name: /I.m ready/ })).toBeNull()
    // The answer goes to the ASKING task, at its own index.
    const fix = within(card).getByRole('radiogroup', { name: /fix question 2/i })
    fireEvent.click(within(fix).getByRole('radio', { name: /^No/ }))
    await act(async () => {
      fireEvent.click(within(fix.closest('li')!).getByRole('button', { name: 'Answer' }))
    })
    expect(api.answerQuestion).toHaveBeenCalledWith(FIX, { question_index: 1, choice: 'No', text: null })
  })

  it('says how it resumes, in the wait panel, without a human-ready route', async () => {
    const c = await draw(mergeHeld())
    const panel = c.querySelector('.dt-top')!
    expect(panel.textContent).toContain(SUMMARY)
    expect(panel.textContent).not.toContain('human-ready')
  })

  it('offers the re-run only for a hold the answer route cannot read whole', async () => {
    const c = await draw(mergeHeld())
    const card = await within(c).findByRole('region', { name: 'Questions holding this merge' })
    expect(within(card).queryByRole('button', { name: 'Re-run the merge' })).toBeNull()
    const blocked = [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { code: 'owner_questions_open', questions: HELD.slice(0, 1), unreadable: 1 },
      } as unknown as NonNullable<Task['blocked_by']>[number],
    ]
    const u = await draw(mergeHeld({ id: 'task_unreadable0000000000', blocked_by: blocked }))
    const held = (await within(u).findAllByRole('region', { name: 'Questions holding this merge' })).at(-1)!
    // MUTATION: drop `UnreadableHold` -- an unreadable hold has no way back but this.
    expect(within(held).getByRole('button', { name: 'Re-run the merge' })).toBeTruthy()
    expect(held.textContent).toContain('could not be read')
  })

  it('draws no held-questions card for a hand-off park', async () => {
    const c = await draw(handOff())
    expect(within(c).queryByRole('region', { name: 'Questions holding this merge' })).toBeNull()
  })
})
