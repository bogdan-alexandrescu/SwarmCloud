// NEEDS YOU (the picked Work redesign's row of what needs the person, part of
// #2688): counted chips over a list that deals with each item in its row.
//
// MUTATIONS, one per block: count a kind by hand (or approvals as 0 when they
// were not read); drop the quiet day's line; enable Approve merge while
// questions hold it, or stop naming them; post the answer to the wrong task or
// index, or open no box; draw the header as no button (out of the tab order);
// report a pick without the task ids it covers, or keep it once folded.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApprovalItem, TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  answerQuestion: vi.fn(),
  approveApproval: vi.fn(),
  loadTaskInputOnce: vi.fn(),
  rerunWorkflowStep: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { NeedsYou, mergeHeldBecause } = await import('../work/NeedsYou')
type NeedKind = import('../work/NeedsYou').NeedKind
type NeedItem = import('../work/NeedsYou').NeedItem

const iso = (ms: number) => new Date(ms).toISOString()

function failed(id: string, over: Partial<Task> = {}): NeedItem {
  return {
    kind: 'failed',
    task: runTask({ id, state: 'FAILED', workflow_id: null, step_id: null, ...over }),
    title: `agent ${id}`,
    why: 'The tests failed',
  }
}

function question(taskId: string, index: number, over: Partial<Extract<NeedItem, { kind: 'question' }>> = {}): NeedItem {
  return {
    kind: 'question',
    taskId,
    index,
    title: `step-${taskId}`,
    question: `Which schema, ${index}?`,
    options: [
      { label: 'v1', description: 'keep it' },
      { label: 'v2', description: 'move on' },
    ],
    recommended: 'v2',
    ...over,
  }
}

function heldMerge(id = 'task_merge'): NeedItem {
  return {
    kind: 'merge',
    title: 'merge',
    pr: { number: 2400, url: 'https://example.com/pr/2400' },
    task: runTask({
      id,
      state: 'PARKED',
      park_reason: 'HUMAN_REQUIRED',
      runner_profile: 'merge',
      workflow_id: 'wf_one',
      step_id: 'merge',
      completed_at: null,
      blocked_by: [
        {
          reason: 'HUMAN_REQUIRED',
          park_phase: 'owner_questions',
          owner_questions: {
            questions: [
              { task_id: 'task_design', step_id: 'design', question_index: 0, question: 'Which schema?' },
              { task_id: 'task_review', step_id: 'review', question_index: 1, question: 'Ship on Friday?' },
            ],
            unreadable: 0,
          },
        } as unknown as NonNullable<Task['blocked_by']>[number],
      ],
    }),
  }
}

function handoff(id: string): NeedItem {
  return {
    kind: 'handoff',
    title: `browser ${id}`,
    task: runTask({
      id,
      state: 'RUNNING',
      runner_profile: 'claude-code-browser',
      human_wait: { reason: 'Sign in to the store', expect: 'example.com', deadline: iso(Date.now() + 5 * 60_000) } as Task['human_wait'],
    }),
  }
}

function approval(id: string): ApprovalItem {
  return { approval_id: id, kind: 'plan', subject: {}, summary: `Plan ${id}`, state: 'pending', requested_at: null, expires_at: null }
}

// A chip is a plain count in the header, which is the strip's one toggle.
const chip = (name: RegExp) => screen.getByRole('img', { name })
const header = () => screen.getByRole('button', { name: /^Needs you/ })

/** Renders the strip and opens it, as a person presses its header to read the rows. */
function show(ui: JSX.Element) {
  const r = render(ui)
  const toggle = screen.queryByRole('button', { name: /^Needs you/, expanded: false })
  if (toggle !== null) fireEvent.click(toggle)
  return r
}

beforeEach(() => {
  api.answerQuestion.mockReset()
  api.approveApproval.mockReset()
  api.loadTaskInputOnce.mockReset()
  api.rerunWorkflowStep.mockReset()
})

describe('the counted chips', () => {
  it('counts each kind from the items it was given, and approvals it could not read are "not known", never 0', () => {
    const items = [failed('a'), failed('b'), failed('c'), question('q1', 0), heldMerge(), handoff('h1')]
    show(<NeedsYou items={items} approvals={null} />)
    expect(chip(/^Failed: 3$/)).toBeTruthy()
    expect(chip(/^Question: 1$/)).toBeTruthy()
    expect(chip(/^Merge waiting: 1$/)).toBeTruthy()
    expect(chip(/^Your turn in a browser: 1$/)).toBeTruthy()
    expect(header().textContent).toContain('Approvals not known')
    expect(chip(/^Approvals: not known$/)).toBeTruthy()
    expect(header().textContent).toMatch(/Needs you\s*6\+/)
  })

  it('counts approvals it was given, and a kind with none reads 0', () => {
    show(<NeedsYou items={[question('q1', 0)]} approvals={[approval('ap1'), approval('ap2')]} />)
    expect(chip(/^Approvals: 2$/)).toBeTruthy()
    expect(chip(/^Failed: 0$/)).toBeTruthy()
    expect(header().textContent).toMatch(/^Needs you\s*3\D/)
  })
})

describe('a quiet day', () => {
  it('says nothing waits on the person, and keeps approvals not known', () => {
    show(<NeedsYou items={[]} approvals={null} />)
    expect(screen.getByText('Nothing waits on you here')).toBeTruthy()
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.getByRole('group', { name: 'What needs you, by kind' }).textContent).toContain('not known')
  })

  it('does not call the day quiet while a kind is not known, and names the kinds', () => {
    const items = Object.assign([] as NeedItem[], { unknownKinds: ['question', 'merge', 'handoff'] as NeedKind[] })
    show(<NeedsYou items={items} approvals={null} />)
    expect(screen.queryByText('Nothing waits on you here')).toBeNull()
    // 'Nothing found', the sentence naming the kinds not known as its tooltip.
    expect(screen.getByText('Nothing found').getAttribute('title')).toBe(
      'Nothing this screen read waits on you; Question, Merge waiting, Your turn in a browser not known here',
    )
  })

  it('reads the same with approvals read and none waiting', () => {
    show(<NeedsYou items={[]} approvals={[]} />)
    expect(screen.getByText('Nothing waits on you here')).toBeTruthy()
    expect(chip(/^Approvals: 0$/)).toBeTruthy()
  })
})

describe('a merge held by two questions', () => {
  it('keeps Approve merge disabled and names both questions', () => {
    const merge = heldMerge()
    show(<NeedsYou items={[merge]} approvals={null} />)
    const button = screen.getByRole('button', { name: 'Approve merge' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    const row = button.closest('li') as HTMLElement
    expect(row.textContent).toContain('held by 2 questions')
    expect(row.textContent).toContain('Held by 2 questions: design question 1, review question 2')
    fireEvent.click(button)
    expect(api.approveApproval).not.toHaveBeenCalled()
  })

  it('stays disabled even with an approval read, while the questions hold it', () => {
    const merge = { ...heldMerge(), approval: { id: 'ap_merge', digest: 'd1' } } as NeedItem
    expect(mergeHeldBecause(merge as Extract<NeedItem, { kind: 'merge' }>)).toMatch(/^Held by 2 questions/)
  })

  it('approves through the approval it was given once nothing holds it', async () => {
    api.approveApproval.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    const onChanged = vi.fn()
    const free: NeedItem = {
      kind: 'merge',
      title: 'merge',
      task: runTask({ id: 'task_free', state: 'PARKED', park_reason: 'HUMAN_REQUIRED', runner_profile: 'merge', blocked_by: [] }),
      approval: { id: 'ap_merge', digest: 'digest-1', head: 'abcdef1234' },
    }
    show(<NeedsYou items={[free]} approvals={null} onChanged={onChanged} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Approve merge at abcdef1' }))
    })
    expect(api.approveApproval).toHaveBeenCalledWith('ap_merge', { digest: 'digest-1' })
    expect(screen.getByText('Approved')).toBeTruthy()
    expect(onChanged).toHaveBeenCalledTimes(1)
  })
})

describe('Answer', () => {
  it('opens the answer box in the row and sends the pick through the owner-question answer call', async () => {
    const saved: Result<TaskQuestions> = {
      status: 'ok',
      fetchedAt: Date.now(),
      data: { task_id: 'task_q', count: 1, status: 'ok', questions: [], redacted: false, redaction_count: 0 },
    }
    api.answerQuestion.mockResolvedValue(saved)
    const onChanged = vi.fn()
    show(<NeedsYou items={[question('task_q', 2)]} approvals={null} onChanged={onChanged} />)
    const row = screen.getByRole('button', { name: 'Answer step-task_q question 3' }).closest('li') as HTMLElement
    expect(within(row).queryByRole('group', { name: /Your answer/ })).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Answer step-task_q question 3' }))
    const box = within(row).getByRole('group', { name: 'Your answer to step-task_q question 3' })
    const send = within(box).getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.click(within(box).getByRole('radio', { name: /v2/ }))
    expect(send.disabled).toBe(false)
    await act(async () => {
      fireEvent.click(send)
    })
    expect(api.answerQuestion).toHaveBeenCalledWith('task_q', { question_index: 2, choice: 'v2', text: null })
    expect(within(row).getByText('Answered')).toBeTruthy()
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it("sends the person's own words when the question has no options", async () => {
    api.answerQuestion.mockResolvedValue({ status: 'ok', fetchedAt: Date.now(), data: {} })
    show(<NeedsYou items={[question('task_w', 0, { options: [] })]} approvals={null} />)
    fireEvent.click(screen.getByRole('button', { name: 'Answer step-task_w question 1' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Your own answer to step-task_w question 1' }), { target: { value: '  use v3  ' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    })
    expect(api.answerQuestion).toHaveBeenCalledWith('task_w', { question_index: 0, choice: null, text: 'use v3' })
  })

  it('says why the answer was not saved', async () => {
    api.answerQuestion.mockResolvedValue({ status: 'error', error: { kind: 'forbidden', httpStatus: 403, code: null, message: 'no' } })
    show(<NeedsYou items={[question('task_x', 0)]} approvals={null} />)
    fireEvent.click(screen.getByRole('button', { name: 'Answer step-task_x question 1' }))
    fireEvent.click(screen.getByRole('radio', { name: /v1/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    })
    expect(screen.getByRole('alert').textContent).toMatch(/^The answer was not saved/)
    expect(screen.queryByText('Answered')).toBeNull()
  })
})

// MUTATION: drop the error line whatever step the card shows, or keep it while
// the card shows this very step; or drop Retry and Open with the line.
describe("a failed step's row beside the workflow page's step card", () => {
  const items = [failed('step', { workflow_id: 'wf_one', step_id: 'build' }), failed('other', { workflow_id: 'wf_one', step_id: 'test' })]
  const rowOf = (name: string) => screen.getByText(name).closest('li') as HTMLElement

  it('drops its error line while the card is open on that step, and keeps Retry and Open', () => {
    show(<NeedsYou items={items} approvals={null} cardStep="build" />)
    const row = rowOf('agent step')
    expect(within(row).queryByText('The tests failed')).toBeNull()
    expect(within(row).getByRole('button', { name: 'Retry agent step' })).toBeTruthy()
    expect(within(row).getByRole('link', { name: 'Open agent step' })).toBeTruthy()
    expect(within(rowOf('agent other')).getByText('The tests failed')).toBeTruthy()
  })

  it('keeps its error line while the card is on another step, or closed, or on a page with no card', () => {
    const { rerender } = show(<NeedsYou items={items} approvals={null} cardStep="test" />)
    expect(within(rowOf('agent step')).getByText('The tests failed')).toBeTruthy()
    rerender(<NeedsYou items={items} approvals={null} cardStep={null} />)
    expect(within(rowOf('agent step')).getByText('The tests failed')).toBeTruthy()
    rerender(<NeedsYou items={items} approvals={null} />)
    expect(within(rowOf('agent step')).getByText('The tests failed')).toBeTruthy()
    expect(within(rowOf('agent other')).getByText('The tests failed')).toBeTruthy()
  })
})

describe('the other rows act as the console does today', () => {
  it('offers Retry for a lone agent as Submit, for a workflow step as a re-run, and opens the logs of either', () => {
    show(<NeedsYou items={[failed('lone'), failed('step', { workflow_id: 'wf_one', step_id: 'build' })]} approvals={null} />)
    const retry = screen.getByRole('link', { name: 'Retry agent lone' }) as HTMLAnchorElement
    expect(retry.getAttribute('href')).toBe('/submit/task')
    expect(screen.queryByRole('link', { name: 'Retry agent step' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Retry agent step' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open agent step' }).getAttribute('href')).toBe('/agents/live/step/logs')
  })

  it("a workflow step's Retry asks first, naming the step, and sends nothing until confirmed", async () => {
    show(<NeedsYou items={[failed('step', { workflow_id: 'wf_one', step_id: 'build' })]} approvals={null} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry agent step' }))
    // MUTATION: send on the first press and one stray click re-runs a workflow.
    expect(api.rerunWorkflowStep).not.toHaveBeenCalled()
    const ask = screen.getByRole('group', { name: 'Re-run step build?' })
    expect(ask.textContent).toContain('Re-run step build inside its workflow')
    fireEvent.click(within(ask).getByRole('button', { name: 'Keep it' }))
    expect(screen.queryByRole('group', { name: 'Re-run step build?' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Retry agent step' })).toBeTruthy()
    expect(api.rerunWorkflowStep).not.toHaveBeenCalled()
  })

  it("a workflow step's confirmed Retry re-runs the step inside its workflow, says which steps, then the page reads again", async () => {
    api.rerunWorkflowStep.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        workflow_id: 'wf_one',
        step_id: 'build',
        replaced: { build: { from: 'step', to: 'task_new' }, ship: { from: 'task_ship', to: 'task_ship2' } },
      },
    })
    const onChanged = vi.fn()
    show(<NeedsYou items={[failed('step', { workflow_id: 'wf_one', step_id: 'build' })]} approvals={null} onChanged={onChanged} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry agent step' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Re-run build' }))
    })
    expect(api.rerunWorkflowStep).toHaveBeenCalledWith('wf_one', 'build')
    expect(api.loadTaskInputOnce).not.toHaveBeenCalledWith('step')
    expect(screen.getByRole('status').textContent).toBe('Re-running build, ship')
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it("a refused step re-run says why and leaves Retry to press again", async () => {
    api.rerunWorkflowStep.mockResolvedValue({
      status: 'error',
      error: { kind: 'conflict', httpStatus: 409, code: 'conflict', message: 'a step can be re-run once every step of its workflow has finished' },
    })
    const onChanged = vi.fn()
    show(<NeedsYou items={[failed('step', { workflow_id: 'wf_one', step_id: 'build' })]} approvals={null} onChanged={onChanged} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry agent step' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Re-run build' }))
    })
    expect(screen.getByRole('alert').textContent).toContain('every step of its workflow has finished')
    expect(screen.getByRole('button', { name: 'Retry agent step' })).toBeTruthy()
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('Retry fills Submit as Run again does: the profile by name, the masked prompt and the repository', async () => {
    api.loadTaskInputOnce.mockResolvedValue({ status: 'ok', fetchedAt: Date.now(), data: { prompt: { text: 'fix the build' } } })
    const { peekTaskPrefill, clearTaskPrefill } = await import('../submitPrefill')
    clearTaskPrefill()
    show(<NeedsYou items={[failed('lone', { runner_profile: 'claude-code', repository_url: 'https://example.com/r.git' })]} approvals={null} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Retry agent lone' }))
    })
    expect(api.loadTaskInputOnce).toHaveBeenCalledWith('lone')
    expect(peekTaskPrefill()).toEqual({ profile: 'claude-code', prompt: 'fix the build', repositoryUrl: 'https://example.com/r.git' })
  })

  it("Take your turn opens a held browser's Browser tab, with its time left", () => {
    show(<NeedsYou items={[handoff('task_h')]} approvals={null} />)
    const take = screen.getByRole('link', { name: 'Take your turn in browser task_h' })
    expect(take.getAttribute('href')).toBe('/agents/live/task_h/browser')
    expect((take.closest('li') as HTMLElement).textContent).toMatch(/\d:\d\d left/)
  })
})

describe('keyboard and the kind filter', () => {
  it('draws the header as one button and every action as a button or link, so each is in the tab order', () => {
    show(<NeedsYou items={[failed('a'), question('q', 0), heldMerge(), handoff('h')]} approvals={[approval('ap')]} />)
    const h = header()
    // The chips are plain counts in the header: the header is the one button.
    expect(within(h).queryAllByRole('button')).toHaveLength(0)
    expect(within(h).getAllByRole('img').map((c) => c.getAttribute('aria-label'))).toEqual([
      'Failed: 1',
      'Question: 1',
      'Merge waiting: 1',
      'Your turn in a browser: 1',
      'Approvals: 1',
    ])
    h.focus()
    expect(document.activeElement).toBe(h)
    const answer = screen.getByRole('button', { name: 'Answer step-q question 1' })
    answer.focus()
    expect(document.activeElement).toBe(answer)
  })

  it('filters the list to the kind "N more" picks, reports the task ids it covers, and clears it when folded', () => {
    const onSelect = vi.fn()
    show(<NeedsYou items={[failed('a'), failed('b'), failed('c'), question('q', 0), heldMerge('task_m')]} approvals={null} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button', { name: '1 more failed' }))
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'failed', taskIds: ['a', 'b', 'c'] })
    expect(within(screen.getByRole('list', { name: 'Needs you: Failed' })).getAllByRole('listitem')).toHaveLength(3)
    fireEvent.click(header())
    expect(onSelect).toHaveBeenLastCalledWith(null)
    fireEvent.click(header())
    expect(within(screen.getByRole('list', { name: 'Needs you' })).getAllByRole('listitem')).toHaveLength(4)
  })

  it('folds a kind past two rows into "N more", which picks the kind', () => {
    show(<NeedsYou items={[failed('a'), failed('b'), failed('c'), failed('d'), failed('e')]} approvals={null} />)
    expect(within(screen.getByRole('list')).getAllByRole('listitem')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: '3 more failed' }))
    expect(within(screen.getByRole('list')).getAllByRole('listitem')).toHaveLength(5)
  })
})
