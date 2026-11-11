// THE NEEDS-YOU STRIP STARTS COLLAPSED (the owner's ask, 11 November 2026, on
// the Agents page and the Workflows list, as the workflow page's own card does
// since 6 November). Its header row -- Needs you, the count, one mark per kind
// -- is the toggle: one press opens the item rows, one more folds them back
// into the row. It ALWAYS starts collapsed: nothing is stored, so a fresh
// mount after opening is collapsed again. Every row's action works once open.
//
// MUTATIONS: start open; store the open state (localStorage, the URL) or keep
// it in module state; leave the header a non-button or drop aria-expanded or
// aria-controls; hide a kind's count while collapsed; let a row action stop
// calling what it calls.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApprovalItem } from '../api'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  answerQuestion: vi.fn(),
  approveApproval: vi.fn(),
  loadTaskInputOnce: vi.fn(),
  rerunWorkflowStep: vi.fn(),
  humanReady: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, answerQuestion: api.answerQuestion, approveApproval: api.approveApproval, loadTaskInputOnce: api.loadTaskInputOnce, rerunWorkflowStep: api.rerunWorkflowStep }
})
vi.mock('../Handoff', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../Handoff')>()
  return { ...actual, humanReady: api.humanReady }
})

const { NeedsYou, NEED_KINDS, NEED_WORD } = await import('../work/NeedsYou')
type NeedItem = import('../work/NeedsYou').NeedItem

const ok = <T,>(data: T) => ({ status: 'ok' as const, data, fetchedAt: Date.now() })

const failedStep: NeedItem = {
  kind: 'failed',
  task: runTask({ id: 'task_build', state: 'FAILED', workflow_id: 'wf_one', step_id: 'build' }),
  title: 'build',
  why: 'The tests failed',
}

const question: NeedItem = {
  kind: 'question',
  taskId: 'task_q',
  index: 0,
  title: 'design',
  question: 'Which schema?',
  options: [
    { label: 'v1', description: 'keep it' },
    { label: 'v2', description: 'move on' },
  ],
  recommended: 'v2',
}

const merge: NeedItem = {
  kind: 'merge',
  title: 'merge',
  task: runTask({ id: 'task_merge', state: 'PARKED', park_reason: 'HUMAN_REQUIRED', runner_profile: 'merge', blocked_by: [] }),
  approval: { id: 'ap_merge', digest: 'digest-1', head: 'abcdef1234' },
}

const parkedBrowser: NeedItem = {
  kind: 'handoff',
  title: 'browser',
  task: runTask({ id: 'task_h', state: 'PARKED', park_reason: 'HUMAN_REQUIRED', runner_profile: 'claude-code-browser', blocked_by: [] }),
}

const approval: ApprovalItem = { approval_id: 'ap1', kind: 'plan', subject: {}, summary: 'Plan one', state: 'pending', requested_at: null, expires_at: null }

const ALL = [failedStep, question, merge, parkedBrowser]

const toggle = () => screen.getByRole('button', { name: /^Needs you/, expanded: false })
const header = () => screen.getByRole('button', { name: /^Needs you/ })

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  localStorage.clear()
})

describe('the Needs-you strip starts collapsed', () => {
  it('mounts collapsed with every count visible and no item row visible', () => {
    render(<NeedsYou items={ALL} approvals={[approval]} />)
    const h = toggle()
    expect(h.getAttribute('aria-expanded')).toBe('false')
    const body = document.getElementById(h.getAttribute('aria-controls') ?? '')
    expect(body, 'aria-controls names the body').not.toBeNull()
    expect(body!.hidden).toBe(true)
    for (const k of NEED_KINDS) expect(within(h).getByRole('img', { name: `${NEED_WORD[k]}: 1` })).toBeTruthy()
    expect(h.textContent).toMatch(/Needs you\s*5/)
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /^Answer/ })).toBeNull()
  })

  it('opens the rows on a press of the header, and folds them back on one more', () => {
    render(<NeedsYou items={ALL} approvals={[approval]} />)
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('true')
    expect(within(screen.getByRole('list', { name: 'Needs you' })).getAllByRole('listitem')).toHaveLength(5)
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  })

  it('toggles from a press on a count as well as on the words', () => {
    render(<NeedsYou items={ALL} approvals={null} />)
    fireEvent.click(screen.getByRole('img', { name: 'Failed: 1' }))
    expect(header().getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(screen.getByRole('img', { name: 'Question: 1' }))
    expect(header().getAttribute('aria-expanded')).toBe('false')
  })

  it('starts collapsed again on a fresh mount after opening, and stores nothing', () => {
    const before = window.location.href
    const first = render(<NeedsYou items={ALL} approvals={null} />)
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('true')
    first.unmount()
    render(<NeedsYou items={ALL} approvals={null} />)
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
    expect(localStorage.length).toBe(0)
    expect(window.location.href).toBe(before)
  })

  it('draws no toggle when nothing waits, and says so as it did', () => {
    render(<NeedsYou items={[]} approvals={null} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('Nothing waits on you here')).toBeTruthy()
  })
})

describe('every row action works once the strip is open', () => {
  const open = () => fireEvent.click(header())

  it('Answer opens the box and sends through the owner-question answer call', async () => {
    api.answerQuestion.mockResolvedValue(ok({ task_id: 'task_q', count: 1, status: 'ok', questions: [], redacted: false, redaction_count: 0 }))
    render(<NeedsYou items={ALL} approvals={null} />)
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Answer design question 1' }))
    fireEvent.click(screen.getByRole('radio', { name: /v2/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    })
    expect(api.answerQuestion).toHaveBeenCalledWith('task_q', { question_index: 0, choice: 'v2', text: null })
  })

  it("Retry re-runs a workflow step inside its workflow once confirmed", async () => {
    api.rerunWorkflowStep.mockResolvedValue(ok({ workflow_id: 'wf_one', step_id: 'build', replaced: { build: { from: 'task_build', to: 'task_new' } } }))
    render(<NeedsYou items={ALL} approvals={null} />)
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Retry build' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Re-run build' }))
    })
    expect(api.rerunWorkflowStep).toHaveBeenCalledWith('wf_one', 'build')
  })

  it('Approve merge approves through the approval it was given', async () => {
    api.approveApproval.mockResolvedValue(ok({}))
    const onChanged = vi.fn()
    render(<NeedsYou items={ALL} approvals={null} onChanged={onChanged} />)
    open()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Approve merge at abcdef1' }))
    })
    expect(api.approveApproval).toHaveBeenCalledWith('ap_merge', { digest: 'digest-1' })
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it("I'm ready resumes a parked browser", async () => {
    api.humanReady.mockResolvedValue(ok({}))
    render(<NeedsYou items={ALL} approvals={null} />)
    open()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: "I'm ready for browser" }))
    })
    expect(api.humanReady).toHaveBeenCalledWith('task_h')
  })

  it("Open goes to an approval's page in Automate › Approvals", () => {
    render(<NeedsYou items={ALL} approvals={[approval]} />)
    open()
    expect(screen.getByRole('link', { name: 'Open approval Plan one' }).getAttribute('href')).toBe('/automate/approvals/ap1')
  })
})
