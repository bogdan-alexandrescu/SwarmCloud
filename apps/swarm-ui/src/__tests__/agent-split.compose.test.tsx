// THE SPLIT, COMPOSED FROM ITS PANELS (SPEED-D4A, owner decision 2026-10-14).
//
// AgentSplit.tsx was split by panel into `agent-split/` so parallel UI lanes
// stop editing the same hunks; the split was a pure move. This holds the
// composed component to the panel set it drew before the move -- the
// divider, the header, the tab bar with every tab in its order, the pane and
// the Checkpoints host -- and holds the module's exported API to what its
// importers use, re-exported from the panels rather than restated.
//
// MUTATIONS, one per block: drop a panel from the composition (the divider,
// the header or the tab bar); reorder a tab; define `PANE_LABELS` or
// `fitTabs` a second time in AgentSplit.tsx instead of re-exporting it.

import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const split = await import('../AgentSplit')
const tabs = await import('../agent-split/tabs')
const head = await import('../agent-split/head')
const divider = await import('../agent-split/divider')
const group = await import('../agent-split/group')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function finished(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    workflow_id: null,
    step_id: 'fix-heartbeat',
    attempt_count: 1,
    result_summary: null,
    ...over,
  })
}

/** The panel set the split draws, as headings: what a reader sees name each panel. */
function panels(): Record<string, unknown> {
  const panel = screen.getByRole('dialog', { name: `Agent ${ID}` })
  const list = within(panel).getByRole('tablist', { name: 'Agent panes' })
  return {
    divider: within(panel).getByRole('separator').getAttribute('aria-label'),
    back: panel.querySelector('.ctl-agent-back')?.textContent,
    title: panel.querySelector('.ag-head .ag-head-title')?.textContent,
    tabs: within(list)
      .getAllByRole('tab')
      .map((t) => t.querySelector('.c-tab-label')?.textContent),
    pane: panel.querySelector('.ag-split-pane') !== null,
    checkpoints: panel.querySelector('.ag-split-pane > .ag-ckpts-host') !== null,
  }
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockReturnValue(new Promise(() => {}))
})

describe('the composed split renders the same panel set as before the move', () => {
  it('draws the divider, the header, every tab in its order, the pane and the Checkpoints host', async () => {
    api.loadTask.mockResolvedValue(ok(finished()))
    render(
      <div className="app has-inspector">
        <split.AgentSplit
          taskId={ID}
          pane="attempts"
          artifact={null}
          closeTo="work/running/recent"
          go={() => {}}
          base={`work/task/${ID}`}
        />
      </div>,
    )
    await waitFor(() => expect(document.querySelector('.ag-head .ag-head-title')?.textContent).toBe('fix-heartbeat'))
    expect(panels()).toEqual({
      divider: 'Resize the agent list',
      back: '‹ Recent',
      title: 'fix-heartbeat',
      tabs: ['Details', 'Logs', 'Changes', 'Attempts', 'Artifacts', 'Checkpoints'],
      pane: true,
      checkpoints: true,
    })
  })
})

describe('AgentSplit keeps its exported API, re-exported from the panels', () => {
  it('exports exactly what its importers use, each the panel module’s own', () => {
    expect(Object.keys(split).sort()).toEqual(
      ['AgHeadMeta', 'AgentSplit', 'HOLD_SCROLL_MS', 'PANE_LABELS', 'agentTabOf', 'fitTabs', 'holdPageScroll'].sort(),
    )
    expect(split.PANE_LABELS).toBe(tabs.PANE_LABELS)
    expect(split.fitTabs).toBe(tabs.fitTabs)
    expect(split.AgHeadMeta).toBe(head.AgHeadMeta)
    expect(split.holdPageScroll).toBe(divider.holdPageScroll)
    expect(split.HOLD_SCROLL_MS).toBe(divider.HOLD_SCROLL_MS)
    expect(split.agentTabOf).toBe(group.agentTabOf)
  })
})
