// NEEDS YOU (the picked Work redesign's row of what needs the person, part of
// #2688): counted chips over a list that deals with each item in its row.
//
// MUTATIONS, one per block: count a kind by hand (or approvals as 0 when they
// were not read); drop the quiet day's line; enable Approve merge while
// questions hold it, or stop naming them; post the answer to the wrong task or
// index, or open no box; draw a chip that is not a button (out of the tab
// order); report a chip's pick without the task ids it covers.

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
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { NeedsYou, mergeHeldBecause } = await import('../work/NeedsYou')
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

const chip = (name: RegExp) => screen.getByRole('button', { name })

beforeEach(() => {
  api.answerQuestion.mockReset()
  api.approveApproval.mockReset()
  api.loadTaskInputOnce.mockReset()
})

describe('the counted chips', () => {
  it('counts each kind from the items it was given, and approvals it could not read are "not known", never 0', () => {
    const items = [failed('a'), failed('b'), failed('c'), question('q1', 0), heldMerge(), handoff('h1')]
    render(<NeedsYou items={items} approvals={null} />)
    expect(chip(/^Failed: 3$/)).toBeTruthy()
    expect(chip(/^Question: 1$/)).toBeTruthy()
    expect(chip(/^Merge waiting: 1$/)).toBeTruthy()
    expect(chip(/^Your turn in a browser: 1$/)).toBeTruthy()
    const bar = screen.getByRole('group', { name: 'What needs you, by kind' })
    expect(bar.textContent).toContain('Approvals not known')
    expect(within(bar).queryByRole('button', { name: /^Approvals/ })).toBeNull()
    expect(screen.getByRole('button', { name: /Needs you/ }).textContent).toMatch(/Needs you\s*6\+/)
  })

  it('counts approvals it was given, and a kind with none reads 0', () => {
    render(<NeedsYou items={[question('q1', 0)]} approvals={[approval('ap1'), approval('ap2')]} />)
    expect(chip(/^Approvals: 2$/)).toBeTruthy()
    expect(chip(/^Failed: 0$/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Needs you/ }).textContent).toMatch(/Needs you\s*3$/)
  })
})

describe('a quiet day', () => {
  it('says nothing waits on the person, and keeps approvals not known', () => {
    render(<NeedsYou items={[]} approvals={null} />)
    expect(screen.getByText('Nothing waits on you here')).toBeTruthy()
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.getByRole('group', { name: 'What needs you, by kind' }).textContent).toContain('not known')
  })

  it('reads the same with approvals read and none waiting', () => {
    render(<NeedsYou items={[]} approvals={[]} />)
    expect(screen.getByText('Nothing waits on you here')).toBeTruthy()
    expect(chip(/^Approvals: 0$/)).toBeTruthy()
  })
})

describe('a merge held by two questions', () => {
  it('keeps Approve merge disabled and names both questions', () => {
    const merge = heldMerge()
    render(<NeedsYou items={[merge]} approvals={null} />)
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
    render(<NeedsYou items={[free]} approvals={null} onChanged={onChanged} />)
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
    render(<NeedsYou items={[question('task_q', 2)]} approvals={null} onChanged={onChanged} />)
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
    render(<NeedsYou items={[question('task_w', 0, { options: [] })]} approvals={null} />)
    fireEvent.click(screen.getByRole('button', { name: 'Answer step-task_w question 1' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Your own answer to step-task_w question 1' }), { target: { value: '  use v3  ' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    })
    expect(api.answerQuestion).toHaveBeenCalledWith('task_w', { question_index: 0, choice: null, text: 'use v3' })
  })

  it('says why the answer was not saved', async () => {
    api.answerQuestion.mockResolvedValue({ status: 'error', error: { kind: 'forbidden', httpStatus: 403, code: null, message: 'no' } })
    render(<NeedsYou items={[question('task_x', 0)]} approvals={null} />)
    fireEvent.click(screen.getByRole('button', { name: 'Answer step-task_x question 1' }))
    fireEvent.click(screen.getByRole('radio', { name: /v1/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    })
    expect(screen.getByRole('alert').textContent).toMatch(/^The answer was not saved/)
    expect(screen.queryByText('Answered')).toBeNull()
  })
})

describe('the other rows act as the console does today', () => {
  it('offers Retry for a lone agent, not for a workflow step, and opens the logs of either', () => {
    render(<NeedsYou items={[failed('lone'), failed('step', { workflow_id: 'wf_one', step_id: 'build' })]} approvals={null} />)
    const retry = screen.getByRole('link', { name: 'Retry agent lone' }) as HTMLAnchorElement
    expect(retry.getAttribute('href')).toBe('/submit/task')
    expect(screen.queryByRole('link', { name: 'Retry agent step' })).toBeNull()
    expect(screen.getByRole('link', { name: 'Open agent step' }).getAttribute('href')).toBe('/agents/live/step/logs')
  })

  it('Retry fills Submit as Run again does: the profile by name, the masked prompt and the repository', async () => {
    api.loadTaskInputOnce.mockResolvedValue({ status: 'ok', fetchedAt: Date.now(), data: { prompt: { text: 'fix the build' } } })
    const { peekTaskPrefill, clearTaskPrefill } = await import('../submitPrefill')
    clearTaskPrefill()
    render(<NeedsYou items={[failed('lone', { runner_profile: 'claude-code', repository_url: 'https://example.com/r.git' })]} approvals={null} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Retry agent lone' }))
    })
    expect(api.loadTaskInputOnce).toHaveBeenCalledWith('lone')
    expect(peekTaskPrefill()).toEqual({ profile: 'claude-code', prompt: 'fix the build', repositoryUrl: 'https://example.com/r.git' })
  })

  it("Take your turn opens a held browser's Browser tab, with its time left", () => {
    render(<NeedsYou items={[handoff('task_h')]} approvals={null} />)
    const take = screen.getByRole('link', { name: 'Take your turn in browser task_h' })
    expect(take.getAttribute('href')).toBe('/agents/live/task_h/browser')
    expect((take.closest('li') as HTMLElement).textContent).toMatch(/\d:\d\d left/)
  })
})

describe('keyboard and the chip filter', () => {
  it('draws every chip and action as a button or link, so each is in the tab order', () => {
    render(<NeedsYou items={[failed('a'), question('q', 0), heldMerge(), handoff('h')]} approvals={[approval('ap')]} />)
    const bar = screen.getByRole('group', { name: 'What needs you, by kind' })
    const chips = within(bar).getAllByRole('button')
    expect(chips.map((c) => c.getAttribute('aria-label') ?? c.textContent)).toEqual([
      expect.stringMatching(/^Needs you/),
      'Failed: 1',
      'Question: 1',
      'Merge waiting: 1',
      'Your turn in a browser: 1',
      'Approvals: 1',
    ])
    for (const c of chips) {
      c.focus()
      expect(document.activeElement).toBe(c)
    }
    const answer = screen.getByRole('button', { name: 'Answer step-q question 1' })
    answer.focus()
    expect(document.activeElement).toBe(answer)
  })

  it('filters the list to the picked kind and reports the task ids it covers', () => {
    const onSelect = vi.fn()
    render(<NeedsYou items={[failed('a'), question('q', 0), heldMerge('task_m')]} approvals={null} onSelect={onSelect} />)
    fireEvent.click(chip(/^Merge waiting: 1$/))
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'merge', taskIds: ['task_m', 'task_design', 'task_review'] })
    const list = screen.getByRole('list', { name: 'Needs you: Merge waiting' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(chip(/^Merge waiting: 1$/).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(chip(/^Merge waiting: 1$/))
    expect(onSelect).toHaveBeenLastCalledWith(null)
    expect(within(screen.getByRole('list', { name: 'Needs you' })).getAllByRole('listitem')).toHaveLength(3)
  })

  it('folds a kind past two rows into "N more", which picks the kind', () => {
    render(<NeedsYou items={[failed('a'), failed('b'), failed('c'), failed('d'), failed('e')]} approvals={null} />)
    expect(within(screen.getByRole('list')).getAllByRole('listitem')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: '3 more failed' }))
    expect(within(screen.getByRole('list')).getAllByRole('listitem')).toHaveLength(5)
  })
})
