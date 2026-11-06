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
 *           by what it counts, `Not ended` (QAFIX2-WORKFLOWS). The picked
 *           Work redesign (frame graph-workflows-rest) puts a workflow whose
 *           state was not derived in All only (`listBucketOf`), so Running
 *           counts and lists the running one alone.
 *   QA-097  at 390 the owner, profile and sort selects started at 57, 56 and
 *           44 px, each after its own label's width. The labels now sit in one
 *           fixed column, so the selects share one left edge. The redesign
 *           keeps the labels for assistive technology only (`.vh`), so each
 *           select starts at its own pick's edge and fills it at 390.
 *
 * QAFIX2-WORK-MINOR (visual QA run 2, wf_b6e2441b419f423a84eb, 2026-10-20):
 *
 *   work-a-012  at 390 the state filter stopped 20-60 px short of the
 *           content edge while the search box and the selects under it ran
 *           the column wide. It runs the column wide too, its segments
 *           sharing the width. The redesign (frame graph-workflows-rest)
 *           sets the buckets on their own line at the left, never wider than
 *           the column, scrolling inside it rather than past the edge.
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

const rowIds = () => [...document.querySelectorAll<HTMLElement>('.wfg-table tbody tr')].map((r) => r.dataset.workflow ?? '')

describe('QA-036: a workflow whose state was not derived is not counted as running', () => {
  it('the summary says it apart from the running one', () => {
    // MUTATION: count an underived workflow as running again in listSummary and this reads `2 running · 0 finished · 1 failed`.
    expect(listSummary({ workflows: ROWS, nextPageToken: null })).toBe('1 running · 1 state unknown · 0 finished · 1 failed')
    // MUTATION: say the part when there is none and this reads `· 0 state unknown ·`.
    expect(listSummary({ workflows: [ROWS[0]!, ROWS[2]!], nextPageToken: null })).toBe('1 running · 0 finished · 1 failed')
  })

  it('the rendered summary and the Running tab agree with the rows they count', async () => {
    render(<WorkflowsScreen view="state=running" />)
    // MUTATION: put the underived workflow back in the running bucket (`listBucketOf`) and Running lists two rows.
    await waitFor(() => expect(rowIds()).toEqual(['wf_audit_01']))
    // MUTATION: drop the underived part from listSummary and the page's own count line says `2 running` again.
    expect(screen.getByText(/1 running · 1 state unknown · 0 finished · 1 failed/)).toBeTruthy()
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    // Running counts the one row it lists, and agrees with the summary's `1 running`...
    const running = within(seg).getByRole('button', { name: /^Running/, pressed: true })
    expect(running.textContent).toBe('Running1')
    // ...and All, the one bucket that holds the underived row, says so in its title.
    const all = within(seg).getByRole('button', { name: /^All/ })
    expect(all.textContent).toBe('All3')
    // MUTATION: drop the words from All's title and nothing says where the underived row is counted.
    expect(all.getAttribute('title')).toMatch(/state this read could not derive/)
  })

  it('names the tab by what it counts, so its 2 never sits beside `1 running` as a running count (run 2, work-a-003)', async () => {
    render(<WorkflowsScreen view="state=running" />)
    await waitFor(() => expect(rowIds()).toHaveLength(1))
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    const tabs = within(seg).getAllByRole('button').map((b) => b.textContent)
    // MUTATION: count the underived workflow in a bucket other than All and the strip reads `Running2` beside `1 running`.
    expect(tabs).toEqual(['All3', 'Running1', 'Parked0', 'Failed1', 'Done0'])
    // MUTATION: drop the summary's `state unknown` part and the line no longer says where the third workflow is.
    const line = screen.getByText(/1 running · 1 state unknown/).textContent ?? ''
    const running = [...line.matchAll(/(\d+) running/g)].reduce((n, m) => n + Number(m[1]), 0)
    expect(running).toBe(1)
  })
})

describe('QA-097: at 390 the stacked filter selects share one left edge', () => {
  const PHONE: CascadeEnv = { width: 390 }
  const WIDE: CascadeEnv = { width: 1440 }

  it('puts every pick label in one fixed column, so no select follows its label width', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(3))
    const picks = [...document.querySelectorAll<HTMLElement>('.wfg-tools > .wfg-pick')]
    // The labels are read, not drawn: no label width sits before any select.
    // MUTATION: make a label a visible word again and its select follows its width.
    expect(picks.map((p) => (p.firstElementChild?.classList.contains('vh') ? p.firstElementChild.textContent?.trim() : null))).toEqual([
      'owner',
      'profile',
      'sort',
    ])
    for (const p of picks) {
      // MUTATION: drop the phone share and each pick shrinks to its option.
      expect(painted(p, ['flex', 'flex-basis'], PHONE)).toBe('1 1 40%')
      const select = p.querySelector('select')!
      // MUTATION: drop the width and each select shrinks to its option instead of filling its pick.
      expect(painted(select, 'width', PHONE)).toBe('100%')
    }
    // MUTATION: move the share out of the phone query and the desktop toolbar's picks stretch too.
    expect(painted(picks[0]!, 'display', WIDE)).toBe('inline-flex')
    expect(painted(picks[0]!, ['flex', 'flex-basis'], WIDE)).toBeNull()
  })
})

describe('work-a-012: at 390 the Workflows state filter runs the column wide', () => {
  const PHONE: CascadeEnv = { width: 390 }
  const WIDE: CascadeEnv = { width: 1440 }

  it('takes the whole row, as the search box and the selects under it do, and shares it among its segments', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(3))
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    // The redesign (frame graph-workflows-rest) sets the buckets on their own
    // line at the left, at every width, and never past the column's edge.
    // MUTATION: drop the cap and five buckets run past the edge at 390.
    expect(seg.classList.contains('wfg-seg')).toBe(true)
    for (const env of [PHONE, WIDE]) {
      expect(painted(seg, 'max-width', env)).toBe('100%')
      // MUTATION: drop the scroll and a bucket past the edge is cut off rather than reachable.
      expect(painted(seg, 'overflow-x', env)).toBe('auto')
      expect(painted(seg, 'align-self', env)).toBe('flex-start')
    }
    // The search box under it grows to the column wide at 390.
    // MUTATION: give the search box a fixed width and it stops short of the edge.
    expect(painted(document.querySelector('.wfg-search')!, ['flex', 'flex-basis'], PHONE)).toBe('1 1 220px')
  })
})
