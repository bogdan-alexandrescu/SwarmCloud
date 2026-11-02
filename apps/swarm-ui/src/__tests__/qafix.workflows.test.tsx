/**
 * VISUAL QA RUN wf_9e9bf74834634db7bcc3 (2026-10-20), WORKFLOWS LIST
 * (QAFIX-WORKFLOWS; part of #1680):
 *
 *   QA-036  the summary said `2 running` and the Running tab said 2 while one
 *           of the two rows said `state unknown`. A workflow whose state
 *           this read could not derive is said apart from running work, as
 *           parked and waiting work already were (WF-10); the Running tab,
 *           which still lists it because nothing says it ended, says so.
 *           Run 2 (wf_b6e2441b419f423a84eb, work-a-003) still read `Running 2`
 *           beside `1 running · 1 state unknown`: the tab is now named
 *           by what it counts, `Not ended` (QAFIX2-WORKFLOWS).
 *   QA-097  at 390 the owner, profile and sort selects started at 57, 56 and
 *           44 px, each after its own label's width. The labels now sit in one
 *           fixed column, so the selects share one left edge.
 *
 * QAFIX2-WORK-MINOR (visual QA run 2, wf_b6e2441b419f423a84eb, 2026-10-20):
 *
 *   work-a-012  at 390 the state filter stopped 20-60 px short of the
 *           content edge while the search box and the selects under it ran
 *           the column wide. It runs the column wide too, its segments
 *           sharing the width.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  loadOlderWorkflows: vi.fn(),
  loadTaskEventsPage: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, listSummary } = await import('../Workflows')

const T0 = Date.parse('2026-10-20T04:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, runner_profile = 'claude-code'): WorkflowStep {
  return { step_id, runner_profile, resource_class: 'standard', depends_on: [], input_from: {}, task_id: null }
}

function workflow(id: string, state: TaskState, ageSeconds: number, owner = 'priya@example.com'): Workflow {
  return {
    workflow_id: id,
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 1 },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: owner,
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps: [step('implement')],
  }
}

/** The preview's shape: an API that served the stored copy and no rollup. */
function underived(id: string, ageSeconds: number): Workflow {
  const w = workflow(id, 'QUEUED', ageSeconds)
  return { ...w, state_source: 'stored', rollup: undefined }
}

/** The crop's three rows: one running, one whose state was not derived, one failed. */
const ROWS = [workflow('wf_audit_01', 'RUNNING', 300), underived('wf_bcdc9180e4fb4a209f31', 600), workflow('wf_failed', 'FAILED', 900)]

class NeverInView {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  vi.stubGlobal('IntersectionObserver', NeverInView)
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: ROWS, taskById: new Map(), statesDetail: null, nextPageToken: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadTaskEventsPage.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const rowIds = () => [...document.querySelectorAll<HTMLElement>('.wfl-table tbody tr')].map((r) => r.dataset.workflow ?? '')

describe('QA-036: a workflow whose state was not derived is not counted as running', () => {
  it('the summary says it apart from the running one', () => {
    // MUTATION: count an underived workflow as running again in listSummary and this reads `2 running · 0 finished · 1 failed`.
    expect(listSummary({ workflows: ROWS, nextPageToken: null })).toBe('1 running · 1 state unknown · 0 finished · 1 failed')
    // MUTATION: say the part when there is none and this reads `· 0 state unknown ·`.
    expect(listSummary({ workflows: [ROWS[0]!, ROWS[2]!], nextPageToken: null })).toBe('1 running · 0 finished · 1 failed')
  })

  it('the rendered summary and the Running tab agree with the rows they count', async () => {
    render(<WorkflowsScreen view="state=running" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_audit_01', 'wf_bcdc9180e4fb4a209f31']))
    // MUTATION: drop the underived part from listSummary and the page's own count line says `2 running` again.
    expect(screen.getByText(/1 running · 1 state unknown · 0 finished · 1 failed/)).toBeTruthy()
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    // The tab lists both rows, because nothing on this read says the underived one ended...
    const notEnded = within(seg).getByRole('button', { name: /^Not ended/, pressed: true })
    // MUTATION: count the underived row out of the tab and it says 1 over the two rows it lists.
    expect(notEnded.textContent).toBe('Not ended2')
    // MUTATION: restore the old title ("running, parked or waiting to start") and the tab claims both rows are running.
    expect(notEnded.getAttribute('title')).toMatch(/state this read could not derive/)
  })

  it('names the tab by what it counts, so its 2 never sits beside `1 running` as a running count (run 2, work-a-003)', async () => {
    render(<WorkflowsScreen view="state=running" />)
    await waitFor(() => expect(rowIds()).toHaveLength(2))
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    const tabs = within(seg).getAllByRole('button').map((b) => b.textContent)
    // MUTATION: label the tab `BUCKET_LABEL.running` again and the strip reads `Running2` beside `1 running`.
    expect(tabs).toEqual(['Not ended2', 'Finished0', 'Failed1', 'All3'])
    // MUTATION: drop the summary's `state unknown` part and the line no longer sums to the tab's 2.
    const line = screen.getByText(/1 running · 1 state unknown/).textContent ?? ''
    const parts = [...line.matchAll(/(\d+) (running|parked|waiting|state unknown)/g)].reduce((n, m) => n + Number(m[1]), 0)
    expect(parts).toBe(2)
  })
})

describe('QA-097: at 390 the stacked filter selects share one left edge', () => {
  const PHONE: CascadeEnv = { width: 390 }
  const WIDE: CascadeEnv = { width: 1440 }

  it('puts every pick label in one fixed column, so no select follows its label width', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(3))
    const picks = [...document.querySelectorAll<HTMLElement>('.wfl-filters > .wfl-pick')]
    expect(picks.map((p) => p.firstChild?.textContent?.trim())).toEqual(['owner', 'profile', 'sort'])
    for (const p of picks) {
      // MUTATION: leave the pick an inline-flex row and each select starts after its own label (57, 56, 44 px).
      expect(painted(p, 'display', PHONE)).toBe('grid')
      // MUTATION: size the label track by its content (`auto` / `max-content`) and the edges go ragged again.
      expect(painted(p, 'grid-template-columns', PHONE)).toBe('4.5em minmax(0, 1fr)')
      const select = p.querySelector('select')!
      // MUTATION: drop the width and each select shrinks to its option instead of filling its row.
      expect(painted(select, 'width', PHONE)).toBe('100%')
    }
    // MUTATION: move the grid out of the phone query and the desktop toolbar's picks stack their selects too.
    expect(painted(picks[0]!, 'display', WIDE)).toBe('inline-flex')
  })
})

describe('work-a-012: at 390 the Workflows state filter runs the column wide', () => {
  const PHONE: CascadeEnv = { width: 390 }
  const WIDE: CascadeEnv = { width: 1440 }

  it('takes the whole row, as the search box and the selects under it do, and shares it among its segments', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(3))
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    // MUTATION: drop the `.wfl-seg` phone rule and the shared `.c-seg`'s
    // `flex: none` sizes the control to its segments, short of the edge.
    expect(painted(seg, ['flex', 'flex-basis'], PHONE)).toBe('1 1 100%')
    // The search box it lines up with is the column wide at 390.
    // MUTATION: (control) give the search box a fixed width and it is no
    // longer the edge the filter is held to.
    expect(painted(document.querySelector('.wfl-search')!, ['flex', 'flex-basis'], PHONE)).toBe('1 1 200px')
    for (const b of within(seg).getAllByRole('button')) {
      // MUTATION: leave the segments content-packed at the left (no grow, or
      // `justify-content` unset) and the spare width pools at the right end.
      expect(painted(b, ['flex', 'flex-grow'], PHONE), b.textContent ?? '').toBe('1 0 auto')
      expect(painted(b, 'justify-content', PHONE), b.textContent ?? '').toBe('center')
    }
    // MUTATION: move the rule out of the phone query and the desktop toolbar's
    // filter fills its row too.
    expect(painted(seg, ['flex', 'flex-basis'], WIDE)).toBe('none')
  })
})
