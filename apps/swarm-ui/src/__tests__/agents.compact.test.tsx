// AGENTS V1, THE REMAINDER (agents.html, decided 2026-10-01).
//
// Beside an open agent the list is a column, and a column of ten-cell table
// rows is a column of truncations. The owner picked two-line rows as drawn:
// the mark, the name and the elapsed time; then the profile, the owner and the
// try -- or, on a waiting row, the reason it waits. Folded to the 64px strip
// the row is only its mark, and a mark alone does not say which agent it is,
// so hovering or focusing one draws a card beside it. On a phone the agent is
// a page of its own with a back link to the tab it came from.
//
// MUTATIONS, one per block: pass `compact={false}` from FlatRows; render the
// card without the strip check, or only on mouse; return 'Agents' from
// `backLabel` whatever the address.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { backLabel } from '../agentlist'

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
    submitted_by: 'alex@acme.test',
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

const STEP = task('task_aaaaaaaa00000000000a', 'RUNNING', {
  workflow_id: 'wf_refactor-broker',
  step_id: 'fix-heartbeat',
  model: 'sonnet',
})
const LONE = task('task_bbbbbbbb00000000000b', 'RUNNING', { submitted_by: 'priya@acme.test' })

async function land(tasks: Task[], taskId: string | null): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(container.querySelector('.ctl-seg [role="tab"]')).not.toBeNull())
  return container as HTMLElement
}

function rowOf(container: HTMLElement, name: string): HTMLElement {
  const row = [...container.querySelectorAll<HTMLElement>('.row.is-compact')].find((r) =>
    r.querySelector('.cr-name')?.textContent?.includes(name),
  )
  expect(row, `no compact row named ${name}`).toBeDefined()
  return row!
}

afterEach(() => {
  delete document.documentElement.dataset.agentList
})

describe('beside an open agent, a row is two lines as drawn', () => {
  it('puts the mark, the name and the elapsed time on line one', async () => {
    const c = await land([STEP, LONE], LONE.id)
    const row = rowOf(c, 'fix-heartbeat')
    const cells = [...row.children].map((el) => el.className.split(' ')[0])
    // Line one, in the drawn order: the mark, the name, the elapsed time.
    expect(cells.slice(0, 3)).toEqual(['ctl-chip', 'agent', 'when'])
    expect(row.querySelector('.ctl-chip')?.textContent).toContain('RUNNING')
    expect(row.querySelector('.cr-name b')?.textContent).toBe('fix-heartbeat')
    expect(row.querySelector('.when')?.textContent).toMatch(/\d/)
    // A lone task is named by the id prefix, with the whole id in its title.
    const lone = rowOf(c, 'bbbbbbbb')
    expect(lone.querySelector('.cr-name .id')?.getAttribute('title')).toBe(LONE.id)
  })

  it('puts profile, owner and try on line two of a live row', async () => {
    const c = await land([STEP, LONE], LONE.id)
    const sub = rowOf(c, 'fix-heartbeat').querySelector('.cr-sub')
    expect(sub, 'no second line').not.toBeNull()
    const text = sub!.textContent ?? ''
    expect(text).toContain('claude-code · sonnet')
    expect(text).toContain('alex')
    expect(text).toContain('try 1/3')
    // The step is the name, so the task id rides on line two.
    expect(sub!.querySelector('.id')?.getAttribute('title')).toBe(STEP.id)
  })

  it('gives a waiting row its reason in place of the try', async () => {
    const parked = task('task_cccccccc00000000000c', 'PARKED')
    const queued = task('task_dddddddd00000000000d', 'QUEUED')
    const c = await land([parked, queued], parked.id)
    const row = rowOf(c, 'cccccccc')
    expect(row.querySelector('.cr-sub')?.textContent).not.toMatch(/try/)
    expect(row.querySelector('.cr-sub')?.textContent).toContain('claude-code · alex')
    const why = row.querySelector('.why')
    expect(why, 'a waiting row lost the reason it waits').not.toBeNull()
    expect((why!.textContent ?? '').trim()).not.toBe('')
  })

  it('drops the column heads and the wide row, and keeps them with no agent open', async () => {
    const open = await land([STEP, LONE], LONE.id)
    expect(open.querySelector('.row.is-head')).toBeNull()
    expect(open.querySelectorAll('.row.is-compact')).toHaveLength(2)
    expect(open.querySelector('.row.clickable:not(.is-compact)')).toBeNull()
    // Exactly the open agent says so, as on the wide list (AG-17).
    const current = open.querySelectorAll('.row[aria-current]')
    expect(current).toHaveLength(1)
    expect(current[0]!.querySelector('.id')?.getAttribute('title')).toBe(LONE.id)
  })

  it('draws the wide list, heads and all, when no agent is open', async () => {
    const c = await land([STEP, LONE], null)
    expect(c.querySelector('.row.is-head')).not.toBeNull()
    expect(c.querySelector('.row.is-compact')).toBeNull()
  })
})

describe('a strip row shows its agent on hover and on focus', () => {
  it('draws the card on hover, names the agent in it, and takes it down on leave', async () => {
    const c = await land([STEP, LONE], LONE.id)
    document.documentElement.dataset.agentList = 'strip'
    const row = rowOf(c, 'fix-heartbeat')
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.mouseEnter(row)
    const card = screen.getByRole('tooltip')
    expect(card.textContent).toContain('fix-heartbeat')
    expect(card.textContent).toContain('RUNNING')
    expect(card.textContent).toContain('claude-code · sonnet')
    expect(card.textContent).toContain('alex')
    expect(card.querySelector('.ag-hovcard-when')?.textContent).toBe(row.querySelector('.when')?.textContent)
    expect(row.getAttribute('aria-describedby')).toBe(card.id)

    fireEvent.mouseLeave(row)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(row.hasAttribute('aria-describedby')).toBe(false)
  })

  it('draws the card on keyboard focus too, and ↓ moves it to the next row', async () => {
    const c = await land([STEP, LONE], LONE.id)
    document.documentElement.dataset.agentList = 'strip'
    // THE LIST'S OWN ORDER, whatever it sorts by: ↓ goes to the next row in the
    // document, so the pair is read off the DOM rather than assumed.
    const [first, second] = [...c.querySelectorAll<HTMLElement>('.row.is-compact')]
    expect(first && second, 'the list drew fewer than two compact rows').toBeTruthy()
    const nameOf = (row: HTMLElement) => row.querySelector('.cr-name b')?.textContent ?? ''

    act(() => first!.focus())
    expect(screen.getByRole('tooltip').textContent).toContain(nameOf(first!))

    fireEvent.keyDown(first!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(second)
    const cards = screen.getAllByRole('tooltip')
    expect(cards).toHaveLength(1)
    expect(cards[0]!.textContent).toContain(nameOf(second!))
    expect(cards[0]!.textContent).not.toContain(nameOf(first!))

    act(() => second!.blur())
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('draws no card when the list is the compact column, which already says it all', async () => {
    const c = await land([STEP, LONE], LONE.id)
    document.documentElement.dataset.agentList = 'list'
    const row = rowOf(c, 'fix-heartbeat')
    fireEvent.mouseEnter(row)
    act(() => row.focus())
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('the agent page leads back to the tab it came from', () => {
  it('names the tab in the list address', () => {
    expect(backLabel('work/running/waiting')).toBe('Waiting')
    expect(backLabel('work/running/live')).toBe('Live')
    expect(backLabel('work/running/recent/failed')).toBe('Recent')
  })

  it('says Agents when the address names no tab', () => {
    expect(backLabel('work/running')).toBe('Agents')
    expect(backLabel('work/running/livex')).toBe('Agents')
  })
})
