/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04): Submit.
 *
 *   R15  The issue preview's meta ended a line on "3 comments ·" with nothing
 *        after it; and "Start from a recent one" said "No recent submissions
 *        yet" to an owner with runs. The issue's facts are one line dotted
 *        only between them; the card lists the caller's own newest lone tasks,
 *        workflows and issue runs from the reads that exist, and says which
 *        windows it looked in. Who the caller is unread lists nothing.
 *
 * MUTATIONS: put the trailing dot back after the comments; list a task that
 * is not the caller's, a workflow's step or an agent's child; list anything
 * when who the caller is was not read -- each turns a case red.
 */
import { render, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { IssueRun, Workflow } from '../types'
import { task } from './runfixture'

const api = vi.hoisted(() => ({
  loadMe: vi.fn(),
  loadTasks: vi.fn(),
  loadWorkflows: vi.fn(),
  loadRuns: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { SubmitChooser, recentSubmissions } = await import('../SubmitChooser')
const { IssuePreviewCard } = await import('../IssueSubmit')

const ME = 'bogdan@saga.xyz'
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function run(id: string, by: string, at: string, title: string | null): IssueRun {
  return {
    id, tenant_id: 'eng', state: 'DONE', terminal: true, created_by: by, created_at: at, updated_at: at,
    issue: { ref: 'bogdan-alexandrescu/SwarmCloud#454', owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454, url: 'https://github.com/x', repository_url: 'https://github.com/y' },
    issue_read: title === null ? null : { title },
  } as unknown as IssueRun
}

function workflow(id: string, by: string, at: string): Workflow {
  return { workflow_id: id, submitted_by: by, created_at: at, steps: [] } as unknown as Workflow
}

const TASKS = [
  task({ id: 'task_mine_lone', submitted_by: ME, created_at: '2026-10-04T10:00:00Z', step_id: null, workflow_id: null }),
  task({ id: 'task_mine_step', submitted_by: ME, created_at: '2026-10-04T11:00:00Z', step_id: 'impl', workflow_id: 'wf_mine' }),
  task({ id: 'task_mine_child', submitted_by: ME, created_at: '2026-10-04T11:30:00Z', parent_task_id: 'task_mine_lone' }),
  task({ id: 'task_theirs', submitted_by: 'someone@saga.xyz', created_at: '2026-10-04T11:45:00Z' }),
]
const WORKFLOWS = [workflow('wf_mine', ME, '2026-10-04T09:00:00Z'), workflow('wf_theirs', 'someone@saga.xyz', '2026-10-04T11:50:00Z')]
const RUNS = [run('run_mine', ME, '2026-10-04T11:55:00Z', 'Fix the run page'), run('run_theirs', 'other@saga.xyz', '2026-10-04T11:58:00Z', 'Not mine')]

beforeEach(() => {
  api.loadMe.mockResolvedValue(ok({ principal: { email: ME, domain: 'saga.xyz', groups: [], is_admin: false }, tenant: { tenant_id: 'eng' } }))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS, next_page_token: null }))
  api.loadWorkflows.mockResolvedValue(ok({ workflows: WORKFLOWS }))
  api.loadRuns.mockResolvedValue(ok({ runs: RUNS, next_page_token: null }))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('R15: the issue preview\'s meta never ends on a dot', () => {
  it('dots between the issue\'s facts and puts when it was read on its own line', () => {
    const read = {
      issue: {
        ref: 'bogdan-alexandrescu/SwarmCloud#454', owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454,
        url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454', repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
        title: 'A title', body: '', body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 3,
      },
      tenant_id: 'eng',
    }
    const { container } = render(<IssuePreviewCard read={read as never} at={Date.now()} closedOk={false} onPlanAnyway={() => {}} />)
    for (const line of container.querySelectorAll('.in-meta')) {
      expect(visible(line), 'a line ends on a dot').not.toMatch(/·$/)
      const items = [...line.querySelectorAll(':scope > .in-meta-i')]
      if (items.length > 0) expect(visible(items[items.length - 1]!)).not.toMatch(/·$/)
    }
    expect(visible(container)).toContain('3 comments')
    expect(visible(container.querySelector('.in-read-at'))).toMatch(/^read .* with this tenant’s forge credential$/)
  })
})

describe('R15: your recent submissions', () => {
  it('lists your own lone tasks, workflows and issue runs, newest first, and nobody else\'s', () => {
    const got = recentSubmissions(ME, TASKS, WORKFLOWS, RUNS)
    expect(got.map((g) => `${g.kind}:${g.id}`)).toEqual(['issue run:run_mine', 'task:task_mine_lone', 'workflow:wf_mine'])
    expect(got[0]!.name).toBe('Fix the run page')
    expect(got[0]!.to).toBe('work/runs?run=run_mine')
  })

  it('draws them in the card, each a link, with the windows it looked in', async () => {
    const go = vi.fn()
    const { container } = render(<SubmitChooser go={go} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(recent.querySelectorAll('.sb-recent-i').length).toBe(3))
    expect(visible(recent)).not.toContain('No recent submissions yet')
    const link = within(recent).getByRole('link', { name: 'Fix the run page' })
    expect(link.getAttribute('href')).toBe('/runs/run_mine')
    link.click()
    expect(go).toHaveBeenCalledWith('work/runs?run=run_mine')
    expect(visible(recent.querySelector('.sb-recent-scope'))).toBe('Among the newest 4 tasks, 2 workflows, 2 issue runs this tenant has.')
  })

  it('lists nothing, and says why, when who you are was not read', async () => {
    api.loadMe.mockResolvedValue({ status: 'error', error: { kind: 'unavailable', httpStatus: 503, code: null, message: 'Busy.' } })
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(visible(recent)).toMatch(/who you are is unknown/))
    expect(recent.querySelectorAll('.sb-recent-i').length).toBe(0)
    expect(recent.querySelector('.c-dash')?.getAttribute('title')).toMatch(/Busy/)
  })

  it('says none of yours when the reads hold none', async () => {
    api.loadRuns.mockResolvedValue(ok({ runs: [], next_page_token: null }))
    api.loadMe.mockResolvedValue(ok({ principal: { email: 'nobody@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: false }, tenant: { tenant_id: 'eng' } }))
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(visible(recent.querySelector('.sb-empty'))).toBe('None of yours yet.'))
  })
})
