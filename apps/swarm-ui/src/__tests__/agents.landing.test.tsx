// WHERE THE AGENTS SCREEN LANDS, AND WHAT ITS ROW ID CAN BE USED FOR.
//
// THE LANDING TAB. `useState<Tab>('live')` put every visitor on Live whatever
// the page held. The audit's screenshot (ui-audit-and-build-prompt.md §A1.2)
// is the failure in one frame: "Nothing in this tab" under a heading asking
// "why has mine not moved?", with the three steps that had not moved one tab
// over behind `Waiting 3`. Live is the one tab that STRUCTURALLY cannot hold a
// step that has not moved, so a fixed default lands the question on the tab
// that cannot answer it. The screen now lands on the first tab that has rows,
// in the order Live, Waiting, Recent -- and a reader's own choice is never
// overridden after that.
//
// THE ROW ID. `task.id.slice(-8)` printed the LAST eight characters, and
// `40158851` is not a prefix of `task_b5dc2568713a40158851`: it cannot be typed
// into a search, matched against the `task_b5dc25…` the rest of the product
// prints, or found in the drawer title. The id a person can use is the one the
// id starts with.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'

const NOW = '2026-09-23T12:00:00.000Z'
const THEN = '2026-09-23T10:00:00.000Z'

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'acme',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: THEN,
    updated_at: NOW,
    started_at: THEN,
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? NOW : null,
    submitted_by: 'ada@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: state === 'PARKED' ? 'QUOTA_EXHAUSTED' : null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: {},
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

async function land(tasks: Task[]): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} />)
  await waitFor(() => expect(container.querySelector('.ctl-seg [role="tab"]')).not.toBeNull())
  return container as HTMLElement
}

/** The tab the screen is showing, by its visible word. */
function selectedTab(): string {
  const tab = screen.getAllByRole('tab').find((t) => t.getAttribute('aria-selected') === 'true')
  expect(tab, 'no tab is selected').toBeDefined()
  return (tab!.textContent ?? '').replace(/\d+/g, '').trim()
}

describe('the Agents screen lands where the rows are', () => {
  it('lands on Waiting when nothing is live and three steps are waiting', async () => {
    // The audit's case, exactly: an empty Live tab and three waiting steps.
    await land([
      task('task_aaaaaaaa00000000000a', 'READY'),
      task('task_bbbbbbbb00000000000b', 'PARKED'),
      task('task_cccccccc00000000000c', 'READY'),
    ])
    expect(selectedTab()).toBe('Waiting')
    // And it shows them, rather than an empty state about a different tab.
    expect(document.querySelectorAll('.rows .row')).toHaveLength(3)
    expect(document.querySelector('.ctl-empty')).toBeNull()
  })

  it('lands on Recent when everything has finished', async () => {
    await land([task('task_dddddddd00000000000d', 'SUCCEEDED')])
    expect(selectedTab()).toBe('Recent')
  })

  it('still lands on Live when something is live', async () => {
    // The order is Live first: a slot being held is the costliest fact on
    // the page, so when there is one it is what the screen opens on.
    await land([
      task('task_eeeeeeee00000000000e', 'READY'),
      task('task_ffffffff00000000000f', 'RUNNING'),
    ])
    expect(selectedTab()).toBe('Live')
  })

  it('honours the tab a reader picks, including an empty one', async () => {
    await land([task('task_dddddddd00000000000d', 'SUCCEEDED')])
    fireEvent.click(screen.getByRole('tab', { name: /^Live/ }))
    await waitFor(() => expect(selectedTab()).toBe('Live'))
    // The empty Live tab is still drawn as a real zero when asked for.
    expect(document.querySelector('.ctl-empty .ctl-mark.is-zero')).not.toBeNull()
  })
})

describe('the row id is one a person can use', () => {
  it('prints a prefix of the real id, not its tail', async () => {
    const realId = 'task_b5dc2568713a40158851'
    const container = await land([task(realId, 'RUNNING')])
    const shown = container.querySelector('.row .agent .id')
    expect(shown, 'the row prints no id').not.toBeNull()
    const displayed = (shown!.textContent ?? '').trim()
    expect(displayed.length, 'an id this short identifies nothing').toBeGreaterThanOrEqual(6)
    // BOTH assertions, and the second is the one that matters: the tail of
    // the id IS a substring of it, so `includes` alone passes `slice(-8)`.
    expect(realId.includes(displayed), `${displayed} is not part of ${realId}`).toBe(true)
    expect(
      realId.indexOf(displayed),
      `${displayed} is taken from the END of ${realId}; nothing else in the product prints that`,
    ).toBeLessThan(8)
    // The whole id stays one hover away.
    expect(shown!.getAttribute('title')).toBe(realId)
  })
})
