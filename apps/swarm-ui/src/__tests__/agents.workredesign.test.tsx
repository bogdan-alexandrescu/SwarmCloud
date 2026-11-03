// THE AGENTS PAGE IN THE WORK REDESIGN (the owner's pick of 2026-11-03,
// "Graph with a docked inspector, plus the Attention inbox's row of what
// needs you"; part of #2688).
//
// The list is the left pane of the Work section's docked inspector: with no
// agent open the inspector is the wall of live browser stills, on a phone a
// sheet opened from the list; the Needs-you row sits over the list and counts
// only the rows the page read (approvals, which it does not read, are "not
// known"); the tools row gains the owner filter and "Most spent first"; a row
// gains its cost and pull request, never $0 for an unmeasured cost; and the
// attempt, owner, cost and pull request give way, in that order, as the
// inspector widens.
//
// MUTATIONS, one per block: drop the `wi-hide-*` class from a cell; pass the
// wall when an agent is open; open the phone sheet without the button; leave
// the owner filter out of the rows; count a null cost as $0.00; drop the
// failed rows from `agentNeeds`.

import SHELL_CSS from '../work/inspectorShell.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(async () => ({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

// The still opens a live socket; here it is a marked box, so the wall is what is tested.
vi.mock('../LiveThumb', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../LiveThumb')>()
  return { ...actual, LiveThumb: ({ taskId }: { taskId: string }) => <span className="still-stub" data-still={taskId} /> }
})

import { AgentsScreen, DROP_CLASS, WALL_WIDTH, agentNeeds, bySpent, costCell, droppedColumns, rowPullRequest } from '../Agents'
import { resetListSnap } from '../listSnap'

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

const BROWSER = task('task_aaaaaaaa00000000000a', 'RUNNING', {
  runner_profile: 'claude-code-browser',
  metadata: { title: 'Book the demo' },
  cost_usd_total: 1.5,
})
const CHEAP = task('task_bbbbbbbb00000000000b', 'RUNNING', {
  submitted_by: 'priya@acme.test',
  metadata: { title: 'Cheap one' },
  cost_usd_total: 0.25,
  cost_incomplete: true,
})
const UNMEASURED = task('task_cccccccc00000000000c', 'RUNNING', { metadata: { title: 'Never measured' }, cost_usd_total: null })

function media(phone: boolean, width = phone ? 390 : 1440): void {
  // A desktop at 1440: under 1366 the shell starts the inspector folded at rest (`startsFolded`).
  vi.stubGlobal('innerWidth', width)
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('720'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

async function land(tasks: Task[], taskId: string | null, onOpen: (id: string) => void = () => {}): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={onOpen} taskId={taskId} list={{ tab: 'live', state: null }} />)
  await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
  await act(async () => {})
  return container as HTMLElement
}

function rowOf(container: HTMLElement, name: string): HTMLElement {
  const row = [...container.querySelectorAll<HTMLElement>('.row.is-compact')].find((r) => r.querySelector('.cr-name')?.textContent?.includes(name))
  expect(row, `no row named ${name}`).toBeDefined()
  return row!
}

function names(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>('.row.is-compact .cr-name b')].map((b) => b.textContent ?? '')
}

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  resetListSnap()
})

describe('the list is the left pane of the docked inspector', () => {
  it('docks the wall of live stills while no agent is open, the held hand-off first', async () => {
    media(false)
    const c = await land([CHEAP, BROWSER], null)
    const shell = c.querySelector<HTMLElement>('.wi[data-screen="agents"]')
    expect(shell?.classList.contains('is-docked')).toBe(true)
    // At the design's resting width, two stills a row, every column kept beside it.
    expect(shell?.style.getPropertyValue('--wi-dock')).toBe(`${WALL_WIDTH}px`)
    const dock = screen.getByRole('complementary', { name: 'Live browsers' })
    const cards = within(dock).getAllByRole('listitem')
    expect(cards.map((li) => li.getAttribute('data-task-id'))).toEqual([BROWSER.id])
    expect(cards[0]!.querySelector('[data-still]')?.getAttribute('data-still')).toBe(BROWSER.id)
    // The list stays the left pane beside it.
    expect(shell?.querySelector('.wi-main .row.is-compact')).not.toBeNull()
  })

  it('opens the agent a still names', async () => {
    media(false)
    const open = vi.fn()
    await land([BROWSER], null, open)
    const dock = screen.getByRole('complementary', { name: 'Live browsers' })
    fireEvent.click(within(dock).getByRole('button', { name: /Book the demo/ }))
    expect(open).toHaveBeenCalledWith(BROWSER.id)
  })

  it('gives the page to the list while an agent is open beside it', async () => {
    media(false)
    const c = await land([BROWSER, CHEAP], CHEAP.id)
    expect(c.querySelector('.wi[data-screen="agents"]')?.classList.contains('is-alone')).toBe(true)
    expect(screen.queryByRole('complementary', { name: 'Live browsers' })).toBeNull()
  })

  it('docks nothing when no browser is live', async () => {
    media(false)
    const c = await land([CHEAP], null)
    expect(c.querySelector('.wi[data-screen="agents"]')?.classList.contains('is-alone')).toBe(true)
  })
})

describe('at 1280 the wall starts folded to its rail', () => {
  it('keeps the list its width and unfolds on the rail', async () => {
    media(false, 1280)
    const c = await land([BROWSER], null)
    expect(c.querySelector('.wi[data-screen="agents"]')?.classList.contains('is-folded')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Open the inspector' }))
    expect(screen.getByRole('complementary', { name: 'Live browsers' })).toBeTruthy()
  })
})

describe('on a phone the wall is a sheet', () => {
  it('opens from the list and goes back to it', async () => {
    media(true)
    await land([BROWSER], null)
    expect(screen.queryByRole('dialog', { name: 'Live browsers' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Watch 1 live browser' }))
    const sheet = screen.getByRole('dialog', { name: 'Live browsers' })
    expect(within(sheet).getAllByRole('listitem')).toHaveLength(1)
    fireEvent.click(within(sheet).getByRole('button', { name: /Agents/ }))
    expect(screen.queryByRole('dialog', { name: 'Live browsers' })).toBeNull()
  })

  it('offers no sheet on a desktop, where the wall is docked', async () => {
    media(false)
    await land([BROWSER], null)
    expect(screen.queryByRole('button', { name: /^Watch \d+ live/ })).toBeNull()
  })
})

describe('a row carries its cost and pull request', () => {
  it('reads the spend, a floor with +, and not known -- never $0', async () => {
    media(false)
    const c = await land([BROWSER, CHEAP, UNMEASURED], null)
    expect(rowOf(c, 'Book the demo').querySelector('.cr-cost')?.textContent).toBe('$1.50')
    expect(rowOf(c, 'Cheap one').querySelector('.cr-cost')?.textContent).toBe('$0.25+')
    const unknown = rowOf(c, 'Never measured').querySelector('.cr-cost')
    expect(unknown?.textContent).toBe('cost not known')
    expect(unknown?.classList.contains('is-unknown')).toBe(true)
    expect(costCell({ cost_usd_total: undefined, cost_incomplete: null }).text).toBe('cost not known')
  })

  it('links the pull request the row carries and says when it is not known', async () => {
    media(false)
    const withPr = task('task_dddddddd00000000000d', 'RUNNING', {
      metadata: { title: 'Has a PR' },
      result_summary: { git: { pull_request: { number: 2403, url: 'https://github.test/o/r/pull/2403' } } },
    })
    const c = await land([withPr, CHEAP], null)
    const link = rowOf(c, 'Has a PR').querySelector<HTMLAnchorElement>('a.cr-pr')
    expect(link?.textContent).toBe('#2403')
    expect(link?.getAttribute('href')).toBe('https://github.test/o/r/pull/2403')
    expect(rowOf(c, 'Cheap one').querySelector('.cr-pr')?.textContent).toBe('PR not known')
    expect(rowPullRequest({ result_summary: { git: { pull_request: { number: 1, url: 'javascript:alert(1)' } } } })).toBeNull()
  })
})

describe('the columns give way as the inspector widens', () => {
  it('keeps every column beside the wall at rest, then drops attempt and owner, then cost and the pull request', () => {
    expect(droppedColumns('wide')).toEqual([])
    expect(droppedColumns('medium')).toEqual([])
    expect(droppedColumns('narrow')).toEqual(['attempt', 'owner'])
    expect(droppedColumns('slim')).toEqual(['attempt', 'owner', 'cost', 'pr'])
    expect(DROP_CLASS).toEqual({ attempt: 'wi-hide-narrow', owner: 'wi-hide-narrow', cost: 'wi-hide-slim', pr: 'wi-hide-slim' })
  })

  it('marks each cell with the shell class that hides it at that width', async () => {
    media(false)
    const c = await land([CHEAP], null)
    const row = rowOf(c, 'Cheap one')
    const holder = (sel: string) => row.querySelector(sel)?.parentElement?.className
    expect(holder('.cr-try')).toBe(DROP_CLASS.attempt)
    expect(holder('.cr-owner')).toBe(DROP_CLASS.owner)
    expect(holder('.cr-cost')).toBe(DROP_CLASS.cost)
    expect(holder('.cr-pr')).toBe(DROP_CLASS.pr)
    // The shell's container queries are what hide them, at widthClassOf's bounds.
    for (const [cls, px] of [['wi-hide-medium', 700], ['wi-hide-narrow', 560], ['wi-hide-slim', 420]] as const) {
      expect(SHELL_CSS).toMatch(new RegExp(`@container work-main \\(max-width: ${px}px\\) \\{ \\.${cls} \\{ display: none`))
    }
  })
})

describe('the tools row: owner and most spent first', () => {
  it('narrows the rows to one owner', async () => {
    media(false)
    const c = await land([BROWSER, CHEAP, UNMEASURED], null)
    expect(names(c)).toHaveLength(3)
    fireEvent.change(screen.getByRole('combobox', { name: 'Owner' }), { target: { value: 'priya' } })
    expect(names(c)).toEqual(['Cheap one'])
  })

  it('puts the most spent first and an unmeasured cost last', async () => {
    media(false)
    const c = await land([UNMEASURED, CHEAP, BROWSER], null)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Most spent first' }))
    expect(names(c)).toEqual(['Book the demo', 'Cheap one', 'Never measured'])
    expect(bySpent({ cost_usd_total: null }, { cost_usd_total: 0 })).toBeGreaterThan(0)
  })
})

describe('the Needs-you row counts what the page read', () => {
  it('counts the failed rows and says approvals are not known', async () => {
    media(false)
    const failed = task('task_eeeeeeee00000000000e', 'FAILED', { metadata: { title: 'Broke' }, last_error: 'exit 1' })
    await land([BROWSER, failed], null)
    const row = screen.getByRole('region', { name: 'Needs you' })
    expect(within(row).getByRole('button', { name: 'Failed: 1' })).toBeTruthy()
    expect(row.textContent).toMatch(/Approvals\s*not known/)
  })

  it('builds a merge held by questions and each question once', () => {
    const blocked = [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { unreadable: 0, questions: [{ task_id: 'task_q', step_id: 'plan', question_index: 0, question: 'Which?' }] },
      },
    ]
    const merge = task('task_ffffffff00000000000f', 'PARKED', { park_reason: 'HUMAN_REQUIRED', blocked_by: blocked as Task['blocked_by'] })
    const again = task('task_ffffffff0000000000ff', 'PARKED', { park_reason: 'HUMAN_REQUIRED', blocked_by: blocked as Task['blocked_by'] })
    const failed = task('task_eeeeeeee00000000000e', 'FAILED')
    const kinds = agentNeeds([merge, again, failed], (t) => t.id).map((i) => i.kind)
    expect(kinds).toEqual(['merge', 'question', 'merge', 'failed'])
  })
})
