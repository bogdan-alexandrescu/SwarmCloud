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

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import {
  AgentsScreen,
  IDLE_POLL_MS,
  LIVE_POLL_MS,
  attemptsUsed,
  pollInterval,
  rowClock,
} from '../Agents'

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

async function land(tasks: Task[], taskId?: string | null): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
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
    // `.clickable`: the list's first `.row` is now its column heads (AG-16),
    // which is not an agent. The three agents are the three rows that open one.
    expect(document.querySelectorAll('.rows .row.clickable')).toHaveLength(3)
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

// ---------------------------------------------------------------------------
// The QA wave's findings on this list (AG-1, AG-15, AG-16, AG-17, AG-32)
// ---------------------------------------------------------------------------

describe('the list is the compact list, and its figures say what they are', () => {
  /**
   * AG-16 asked that `1/3`, `1u` and `4m 12s` not be read off their values.
   * The ten-column table it headed is gone (agents.html V1, #503: its Agent
   * column measured 26px at 1440), so the list carries no column heads; each
   * figure carries its own word instead -- `try 1/3`, and the elapsed time
   * beside the name it belongs to.
   *
   * BREAK IT: draw `RowHead` again, or print the try without its word.
   */
  it('draws no head row, and names the try in its own words', async () => {
    const container = await land([task('task_ffffffff00000000000f', 'RUNNING')])
    const rows = container.querySelector('.rows')!
    expect(rows.querySelector('.row.is-head'), 'the compact list grew a head row').toBeNull()
    const agent = rows.querySelector('.row.clickable')!
    expect(agent.classList.contains('is-compact')).toBe(true)
    expect(agent.querySelector('.cr-try')?.textContent).toBe('try 1/3')
    expect(agent.querySelector('.cr-try')?.getAttribute('aria-label')).toBe('1 of 3 attempts used')
  })

  it('draws no head in a workflow group either', async () => {
    const container = await land([
      task('task_dddddddd00000000000d', 'SUCCEEDED', { workflow_id: 'wf_one', step_id: 'a' }),
      task('task_eeeeeeee00000000000e', 'FAILED', { workflow_id: 'wf_two', step_id: 'b' }),
    ])
    fireEvent.click(screen.getByLabelText(/group by workflow/i))
    await waitFor(() => expect(container.querySelectorAll('.section.group').length).toBe(2))
    for (const group of container.querySelectorAll('.section.group .rows')) {
      expect(group.querySelector('.row.is-head'), 'a workflow group has heads').toBeNull()
      expect(group.firstElementChild?.classList.contains('is-compact')).toBe(true)
    }
  })
})

describe('an attempt count over its cap looks like one', () => {
  /**
   * AG-15. `83/3` rendered exactly like `1/3`. Over is `>`, not `>=`: `3/3`
   * is a task that used every attempt it was allowed, and `4/3` is one the
   * platform ran past its own cap.
   *
   * BREAK IT: test `>=`, or drop the class. `3/3` gains it, or `4/3` loses it.
   */
  it('marks 4/3 over the ceiling, with the overage in words, and leaves 3/3 alone', async () => {
    const container = await land([
      task('task_aaaaaaaa00000000000a', 'FAILED', { attempt_count: 4, max_attempts: 3 }),
      task('task_bbbbbbbb00000000000b', 'FAILED', { attempt_count: 3, max_attempts: 3 }),
    ])
    // A FINISHED ROW SAYS ITS TRY ONLY WHEN IT IS OVER (agents.html V1 keeps
    // the try on a live row); an overage is the one count a finished row
    // still has to show, because it is the platform's fault, not the run's.
    const cells = [...container.querySelectorAll('.row.clickable .cr-try')]
    expect(cells).toHaveLength(1)
    const over = cells[0]!
    expect(over.textContent).toBe('try 4/3')
    expect(over.classList.contains('is-over'), '4/3 is drawn like any other count').toBe(true)
    expect(over.getAttribute('aria-label') ?? '').toMatch(/1 over the ceiling/)
    const spent = [...container.querySelectorAll<HTMLElement>('.row.clickable')].find((r) => r.dataset.taskId === 'task_bbbbbbbb00000000000b')!
    expect(spent.querySelector('.is-over'), '3/3 is drawn as over its cap').toBeNull()
  })

  it('computes over from the two numbers, strictly', () => {
    expect(attemptsUsed({ attempt_count: 83, max_attempts: 3 })).toEqual({
      over: true,
      say: '83 attempts against a cap of 3: 80 over the ceiling',
    })
    expect(attemptsUsed({ attempt_count: 3, max_attempts: 3 }).over).toBe(false)
    expect(attemptsUsed({ attempt_count: 0, max_attempts: 3 }).over).toBe(false)
  })
})

describe('the row the inspector has open says so', () => {
  /**
   * AG-17. With the inspector open, no row showed which agent it was.
   *
   * BREAK IT: stop passing `open` to `TaskRow`. No row carries the attribute.
   */
  it('marks exactly the open agent aria-current, and no other', async () => {
    const open = 'task_bbbbbbbb00000000000b'
    const container = await land(
      [task('task_aaaaaaaa00000000000a', 'RUNNING'), task(open, 'RUNNING')],
      open,
    )
    const current = container.querySelectorAll('.row[aria-current]')
    expect(current, 'no row, or more than one, is marked open').toHaveLength(1)
    expect(current[0]!.getAttribute('aria-current')).toBe('true')
    expect(current[0]!.querySelector('.id')?.getAttribute('title')).toBe(open)
  })

  it('marks none when nothing is open', async () => {
    const container = await land([task('task_aaaaaaaa00000000000a', 'RUNNING')], null)
    expect(container.querySelector('.row[aria-current]')).toBeNull()
  })
})

describe('the capacity `?` belongs to the empty Live tab alone', () => {
  /**
   * AG-32. The one glyph on this screen is there because an empty LIVE tab
   * can be misread -- "nothing running" is not "nothing costing". An empty
   * Waiting or Recent tab makes no claim about cost, and the capacity topic
   * opened there answered a question nobody asked.
   *
   * BREAK IT: render the glyph for every empty tab again.
   */
  it('draws no `?` on an empty Waiting tab', async () => {
    await land([task('task_dddddddd00000000000d', 'SUCCEEDED')])
    fireEvent.click(screen.getByRole('tab', { name: /^Waiting/ }))
    await waitFor(() => expect(selectedTab()).toBe('Waiting'))
    const empty = document.querySelector('.ctl-empty')!
    expect(empty.querySelector('.ctl-mark.is-zero'), 'the empty tab lost its real-zero mark').not.toBeNull()
    expect(empty.querySelector('button[aria-expanded]'), 'the capacity `?` is on the Waiting tab').toBeNull()
  })

  it('draws no `?` on an empty Recent tab', async () => {
    await land([task('task_ffffffff00000000000f', 'RUNNING')])
    fireEvent.click(screen.getByRole('tab', { name: /^Recent/ }))
    await waitFor(() => expect(selectedTab()).toBe('Recent'))
    expect(document.querySelector('.ctl-empty button[aria-expanded]')).toBeNull()
  })

  it('keeps it on the empty Live tab', async () => {
    await land([task('task_dddddddd00000000000d', 'SUCCEEDED')])
    fireEvent.click(screen.getByRole('tab', { name: /^Live/ }))
    await waitFor(() => expect(selectedTab()).toBe('Live'))
    expect(document.querySelector('.ctl-empty button[aria-expanded]')).not.toBeNull()
  })
})

describe('the list re-reads, and its row clock runs with the shared clock (AG-1, G2-04)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /**
   * The list read once and its 1s clock went on adding to every row: a
   * finished agent read `running` and its elapsed kept climbing, on a page
   * nobody had re-read. The list re-reads at §2.5's cadence now. `rowClock`
   * still stops one interval past the read, for the silent-worker line and
   * the inspector's drawer; the row's elapsed figure no longer reads it.
   */
  it('re-reads fast only while Live holds a row', () => {
    const live: TaskPage = { tasks: [task('task_ffffffff00000000000f', 'RUNNING')] }
    const idle: TaskPage = { tasks: [task('task_dddddddd00000000000d', 'SUCCEEDED')] }
    expect(pollInterval(live)).toBe(LIVE_POLL_MS)
    expect(pollInterval(idle)).toBe(IDLE_POLL_MS)
    expect(pollInterval(null)).toBe(IDLE_POLL_MS)
    expect(LIVE_POLL_MS).toBeLessThan(IDLE_POLL_MS)
  })

  it('holds the clock at one interval past the read', () => {
    expect(rowClock(1_000, 0, 5_000)).toBe(1_000)
    expect(rowClock(60_000, 0, 5_000)).toBe(5_000)
    // Not yet read: nothing to hold it to.
    expect(rowClock(60_000, null, 5_000)).toBe(60_000)
  })

  /**
   * G2-04 (dev QA 2026-10-07) REVERSED THIS CASE'S OLD CLAIM. The row's elapsed
   * figure was held one interval past the read, and with the read 5 min old
   * the list said `12m 44s` beside an inspector saying `17m 33s` for the same
   * task. It runs on the shared clock now; the read's age is the Screen's to
   * show, and the list still re-reads (the case below). What stays capped is
   * the silent-worker line -- `qa.g2.agents.test.tsx`.
   *
   * BREAK IT: hand the rows `rowClock(useNow(1000), readAt, interval)` again.
   */
  it('keeps a running row’s elapsed time moving after the read is older than one interval', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    const start = Date.now()
    // THE FIRST READ LANDS AND EVERY RE-READ FAILS. The list polls now, and a
    // re-read that lands moves the read forward -- which is the clock
    // resuming correctly, not the defect. What must not happen is the clock
    // running on over rows no read has refreshed.
    let reads = 0
    api.loadTasks.mockImplementation(async () => {
      reads += 1
      if (reads > 1) {
        return {
          status: 'error',
          error: { kind: 'upstream_degraded', httpStatus: 503, code: 'upstream_unavailable', message: 'Busy.' },
        }
      }
      return {
        status: 'ok',
        data: {
          tasks: [
            task('task_ffffffff00000000000f', 'RUNNING', {
              started_at: new Date(start - 60_000).toISOString(),
            }),
          ],
          tenant_id: 'acme',
        },
        fetchedAt: Date.now(),
      }
    })
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    const when = () => container.querySelector('.row.clickable .when')?.textContent ?? ''
    await advance(0)
    expect(when()).toBe('1m 0s')
    await advance(3_000)
    expect(when()).toBe('1m 3s')
    // A minute on, with no fresh read, it has gone on with the clock.
    await advance(60_000)
    expect(reads, 'the list did not try to re-read at all').toBeGreaterThan(1)
    expect(when(), 'the row stopped at the read while the clock went on').toBe('2m 3s')
  })

  /**
   * AG-1. THE LIST RE-READS, at §2.5's cadence: 5s while Live holds a row,
   * 30s when it does not. It read once and never again.
   *
   * BREAK IT: drop `pollMs` from AgentsScreen. `loadTasks` is called once.
   */
  it('re-reads every 5s while Live holds a row, and every 30s when it does not', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    const page = (state: TaskState) => async (): Promise<Result<TaskPage>> => ({
      status: 'ok',
      data: { tasks: [task('task_ffffffff00000000000f', state)], tenant_id: 'acme' },
      fetchedAt: Date.now(),
    })

    api.loadTasks.mockImplementation(page('RUNNING'))
    const live = render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    await advance(LIVE_POLL_MS)
    expect(api.loadTasks, 'a Live row did not bring a re-read at 5s').toHaveBeenCalledTimes(2)
    live.unmount()

    api.loadTasks.mockReset()
    api.loadTasks.mockImplementation(page('SUCCEEDED'))
    render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    await advance(LIVE_POLL_MS)
    expect(api.loadTasks, 'an idle list re-read at the Live cadence').toHaveBeenCalledTimes(1)
    await advance(IDLE_POLL_MS - LIVE_POLL_MS)
    expect(api.loadTasks, 'an idle list never re-read').toHaveBeenCalledTimes(2)
  })
})

describe('the tab and the Recent state are addresses (OV-10)', () => {
  /**
   * The Overview's failed-agents item could only say "agents · Recent tab",
   * and the list opened on Live whenever anything ran. The tab and a Recent
   * state filter are now part of the address: App passes them as `list`, and
   * a tab named there overrides where the screen would have landed.
   *
   * The props are passed through a spread so this compiles against a screen
   * that does not declare them yet -- App passes `taskId` the same way, for
   * the same reason.
   *
   * MUTATION: ignore `list`, or filter Recent by anything but the state.
   */
  const MIXED = [
    task('task_aaaaaaaa00000000000a', 'RUNNING'),
    task('task_bbbbbbbb00000000000b', 'FAILED'),
    task('task_cccccccc00000000000c', 'SUCCEEDED'),
    task('task_dddddddd00000000000d', 'CANCELLED'),
  ]

  async function landAt(tasks: Task[], props: object): Promise<HTMLElement> {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: { tasks, tenant_id: 'acme' },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    const { container } = render(<AgentsScreen onOpen={() => {}} {...props} />)
    await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
    return container as HTMLElement
  }

  const states = (c: HTMLElement) =>
    [...c.querySelectorAll('.rows .row.clickable .sk-st')].map((n) => (n.textContent ?? '').trim())

  it('opens recent/failed on Recent with only the FAILED rows, though Live has one', async () => {
    const c = await landAt(MIXED, { list: { tab: 'recent', state: 'failed' } as const })
    expect(selectedTab()).toBe('Recent')
    expect(states(c)).toEqual(['failed'])
  })

  it('offers all, failed, cancelled and succeeded on Recent, counted from the loaded rows', async () => {
    const seen: unknown[] = []
    const c = await landAt(MIXED, {
      list: { tab: 'recent', state: null } as const,
      onList: (l: unknown) => seen.push(l),
    })
    const seg = c.querySelector('[aria-label="Recent, by state"]')
    expect(seg, 'Recent carries no state segment').not.toBeNull()
    const buttons = [...seg!.querySelectorAll('button')]
    expect(buttons.map((b) => (b.textContent ?? '').replace(/\d+/g, '').trim())).toEqual([
      'all',
      'failed',
      'cancelled',
      'succeeded',
    ])
    // DEAD_LETTERED is never written, so it is never offered.
    expect((seg!.textContent ?? '').toLowerCase()).not.toContain('dead')
    expect(buttons.map((b) => (b.textContent ?? '').replace(/\D+/g, ''))).toEqual(['3', '1', '1', '1'])
    expect(states(c)).toHaveLength(3)

    fireEvent.click(buttons[2]!)
    await waitFor(() => expect(states(c)).toEqual(['cancelled']))
    // The click is reported, and the screen never writes the hash itself.
    expect(seen[seen.length - 1]).toEqual({ tab: 'recent', state: 'cancelled' })

    fireEvent.click(screen.getByRole('tab', { name: /^Live/ }))
    await waitFor(() => expect(selectedTab()).toBe('Live'))
    expect(seen[seen.length - 1]).toEqual({ tab: 'live', state: null })
    expect(c.querySelector('[aria-label="Recent, by state"]'), 'the state segment is shown off Recent').toBeNull()
  })

  it('lands as before when the address names no tab', async () => {
    await landAt(MIXED, { list: null })
    expect(selectedTab()).toBe('Live')
  })
})

describe('Recent can be searched and put failures first (#99)', () => {
  /**
   * The state segment answered "which failed"; it did not answer "where is
   * mine". A pasted full task id or a step name has to leave only the rows it
   * names, over the same loaded page every other count here runs over -- and
   * the query stays on the screen, never in the address.
   *
   * MUTATION: drop the text filter from `rows`, match case-sensitively, or
   * ignore `failedFirst` in the sort.
   */
  const OLDER = '2026-09-23T11:00:00.000Z'
  const OLDEST = '2026-09-23T09:00:00.000Z'
  const PAGE = [
    task('task_aaaaaaaa00000000000a', 'SUCCEEDED', { workflow_id: 'wf_build', step_id: 'Scan-Terraform' }),
    task('task_bbbbbbbb00000000000b', 'FAILED', { updated_at: OLDEST, workflow_id: 'wf_build', step_id: 'lint' }),
    task('task_cccccccc00000000000c', 'CANCELLED', { updated_at: OLDER }),
    task('task_dddddddd00000000000d', 'FAILED', { updated_at: OLDER, workflow_id: 'wf_docs', step_id: 'publish' }),
  ]

  async function landRecent(props: object = {}): Promise<HTMLElement> {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: { tasks: PAGE, tenant_id: 'acme' },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    const { container } = render(
      <AgentsScreen onOpen={() => {}} {...{ list: { tab: 'recent', state: null }, ...props }} />,
    )
    await waitFor(() => expect(container.querySelector('.rows .row.clickable')).not.toBeNull())
    return container as HTMLElement
  }

  const ids = (c: HTMLElement) =>
    [...c.querySelectorAll<HTMLElement>('.rows .row.clickable')].map((n) => n.dataset.taskId ?? null)

  it('offers one "Find by name or task id" box above the list, on every tab (agents.html V1)', async () => {
    const c = await landRecent()
    const box = screen.getByRole('searchbox', { name: /find by name or task id/i })
    expect(box.getAttribute('placeholder')).toBe('Find by name or task id')
    expect(box.closest('.ag-list-head'), 'the box is not in the list header').not.toBeNull()
    expect(ids(c)).toHaveLength(4)
    // #503: the LIVE list had no search box. It is the same box on every tab.
    for (const tab of ['Live', 'Waiting']) {
      fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${tab}`) }))
      expect(screen.getByRole('searchbox', { name: /find by name or task id/i })).toBe(box)
    }
  })

  it('narrows the Live tab by name too', async () => {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: {
        tasks: [
          task('task_eeeeeeee00000000000e', 'RUNNING', { step_id: 'fix-heartbeat', workflow_id: 'wf_a' }),
          task('task_ffffffff00000000000f', 'RUNNING'),
        ],
        tenant_id: 'acme',
      },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    const { container } = render(<AgentsScreen onOpen={() => {}} list={{ tab: 'live', state: null }} />)
    await waitFor(() => expect(container.querySelectorAll('.rows .row.clickable')).toHaveLength(2))
    fireEvent.change(screen.getByRole('searchbox', { name: /find by name or task id/i }), { target: { value: 'HEARTBEAT' } })
    await waitFor(() => expect(ids(container as HTMLElement)).toEqual(['task_eeeeeeee00000000000e']))
  })

  it('leaves only the row a pasted full task id names', async () => {
    const c = await landRecent()
    fireEvent.change(screen.getByRole('searchbox', { name: /find by name or task id/i }), {
      target: { value: '  task_cccccccc00000000000c ' },
    })
    await waitFor(() => expect(ids(c)).toEqual(['task_cccccccc00000000000c']))
  })

  it('matches a step name and a workflow id case-insensitively', async () => {
    const c = await landRecent()
    const box = screen.getByRole('searchbox', { name: /find by name or task id/i })
    fireEvent.change(box, { target: { value: 'scan-terraform' } })
    await waitFor(() => expect(ids(c)).toEqual(['task_aaaaaaaa00000000000a']))
    fireEvent.change(box, { target: { value: 'WF_BUILD' } })
    await waitFor(() =>
      expect(ids(c)).toEqual(['task_aaaaaaaa00000000000a', 'task_bbbbbbbb00000000000b']),
    )
  })

  it('applies after the state filter, and says so when nothing is left', async () => {
    const c = await landRecent({ list: { tab: 'recent', state: 'failed' } })
    expect(ids(c)).toEqual(['task_dddddddd00000000000d', 'task_bbbbbbbb00000000000b'])
    fireEvent.change(screen.getByRole('searchbox', { name: /find by name or task id/i }), { target: { value: 'scan' } })
    await waitFor(() => expect(c.querySelector('.ctl-empty')).not.toBeNull())
    expect(c.querySelector('.ctl-empty')!.textContent).toMatch(/scan/)
  })

  it('never writes the query to the address', async () => {
    const seen: unknown[] = []
    await landRecent({ onList: (l: unknown) => seen.push(l) })
    const before = window.location.href
    fireEvent.change(screen.getByRole('searchbox', { name: /find by name or task id/i }), { target: { value: 'lint' } })
    expect(seen).toEqual([])
    expect(window.location.href).toBe(before)
  })

  it('puts FAILED rows first, then newest first, when asked', async () => {
    const c = await landRecent()
    // Newest first by default: the SUCCEEDED row is the newest.
    expect(ids(c)[0]).toBe('task_aaaaaaaa00000000000a')
    fireEvent.click(screen.getByRole('checkbox', { name: /failed first/i }))
    await waitFor(() =>
      expect(ids(c)).toEqual([
        'task_dddddddd00000000000d',
        'task_bbbbbbbb00000000000b',
        'task_aaaaaaaa00000000000a',
        'task_cccccccc00000000000c',
      ]),
    )
  })

  it("keeps the search and the sort inside the loaded-rows qualifier", async () => {
    const c = await landRecent()
    const say = c.querySelector('.ag-scope')!.getAttribute('aria-label') ?? ''
    expect(say).toMatch(/search/)
    expect(say).toMatch(/sort/)
    expect(say).toMatch(/4 rows loaded/)
  })
})

describe('a reason shared by neighbouring rows is said once (#100)', () => {
  /**
   * Twenty-one parked steps each printed "Waiting on an earlier step in its
   * workflow." -- twenty-one lines of the same sentence, which is what pushed
   * the list off one screen. A run of rows sharing a reason says it once: on
   * the first row of the run in the flat list, on the group header in the
   * grouped one. The hidden copies stay in each row's accessible name, and a
   * reason that needs a person is never hidden.
   *
   * MUTATION: compare with the next row instead of the previous one, hide
   * warn lines, or drop the shared line from the group header.
   */
  const WAIT = 'Waiting on an earlier step in its workflow.'
  // Newest first is the list's order, so each fixture is a minute older than
  // the one before it and the rows land in the order written.
  let minute = 0
  const at = () => new Date(Date.parse(NOW) - 60_000 * minute++).toISOString()
  const parked = (id: string, over: Partial<Task> = {}) =>
    task(id, 'PARKED', { park_reason: 'DEPENDENCY_INCOMPLETE', workflow_id: 'wf_chain', updated_at: at(), ...over })

  const rowEls = (c: HTMLElement) => [...c.querySelectorAll<HTMLElement>('.rows .row.clickable')]
  const shownWhy = (r: HTMLElement) => {
    const w = r.querySelector('.why')
    return w && !w.classList.contains('is-shared') ? (w.textContent ?? '') : null
  }

  it('says a run of equal reasons once in the flat list, and keeps it in every row', async () => {
    const c = await land([
      parked('task_aaaaaaaa00000000000a'),
      parked('task_bbbbbbbb00000000000b'),
      parked('task_cccccccc00000000000c'),
      task('task_dddddddd00000000000d', 'PARKED', { park_reason: 'PROVIDER_COOLDOWN', updated_at: at() }),
    ])
    const rows = rowEls(c)
    expect(rows).toHaveLength(4)
    const shown = rows.map(shownWhy)
    expect(shown[0]).toBe(WAIT)
    expect(shown[1]).toBeNull()
    expect(shown[2]).toBeNull()
    // A differing neighbour keeps its own line.
    expect(shown[3]).not.toBeNull()
    expect(shown[3]).not.toBe(WAIT)
    // Hidden from sight, never from the row's name.
    for (const r of rows.slice(0, 3)) {
      // The row, not its id line's copy button, whose name carries the id
      // too (#94).
      const named = screen.getAllByRole('button', { name: new RegExp(r.querySelector('.id')!.textContent!) }).filter((b) => b.classList.contains('row'))
      expect(named).toHaveLength(1)
      expect(named[0]!.textContent).toContain(WAIT)
      expect(r.textContent).toContain(WAIT)
    }
  })

  it('never hides a reason that needs a person', async () => {
    const c = await land([
      task('task_aaaaaaaa00000000000a', 'PARKED', { park_reason: 'MANUAL_PAUSE' }),
      task('task_bbbbbbbb00000000000b', 'PARKED', { park_reason: 'MANUAL_PAUSE' }),
    ])
    const rows = rowEls(c)
    expect(rows.map((r) => r.querySelector('.why.is-warn') !== null)).toEqual([true, true])
    expect(c.querySelectorAll('.why.is-shared')).toHaveLength(0)
  })

  it('prints a shared reason once on the group header, with its count', async () => {
    const c = await land([
      parked('task_aaaaaaaa00000000000a'),
      parked('task_bbbbbbbb00000000000b'),
      parked('task_cccccccc00000000000c'),
      parked('task_eeeeeeee00000000000e', { park_reason: 'PROVIDER_COOLDOWN' }),
    ])
    fireEvent.click(screen.getByRole('checkbox', { name: /group by workflow/i }))
    await waitFor(() => expect(c.querySelector('.section.group')).not.toBeNull())
    const group = c.querySelector('.section.group')!
    const said = [...group.querySelectorAll('.group-why')].map((n) => (n.textContent ?? '').trim())
    expect(said).toEqual([`3 ${WAIT}`])
    const rows = rowEls(c)
    expect(rows.slice(0, 3).map(shownWhy)).toEqual([null, null, null])
    expect(shownWhy(rows[3]!)).not.toBeNull()
    for (const r of rows) expect(r.querySelector('.why')).not.toBeNull()
  })

  it('keeps a shared reason out of the row grid, inside the agent cell', async () => {
    // MUTATION: render the hidden reason as a direct child of `.row` again.
    // As a grid item it must be placed, and every placement tried shifted the
    // row's columns or cost it a line.
    const c = await land([parked('task_aaaaaaaa00000000000a'), parked('task_bbbbbbbb00000000000b')])
    const rows = rowEls(c)
    const hidden = rows[1]!.querySelector('.why.is-shared')
    expect(hidden).not.toBeNull()
    expect(hidden!.parentElement!.classList.contains('agent')).toBe(true)
    for (const r of rows) expect(r.querySelector(':scope > .why.is-shared')).toBeNull()
  })

  it('draws a shared reason visually hidden, not display:none and not on hover', async () => {
    const { default: css } = await import('../styles.css?raw')
    const rule = /\.row \.why\.is-shared\s*\{([^}]*)\}/.exec(css)
    expect(rule, 'no `.row .why.is-shared` rule').not.toBeNull()
    expect(rule![1]).toMatch(/clip-path:\s*inset\(50%\)/)
    expect(rule![1]).not.toMatch(/display:\s*none/)
    expect(css).not.toMatch(/:hover[^{]*\.why\.is-shared/)
    // Not a grid item at a fixed cell: that took (1,1) before auto-placement
    // and moved every other cell of the row one column right.
    expect(rule![1]).not.toMatch(/grid-(row|column)\s*:/)
    expect(rule![1]).toMatch(/position:\s*absolute/)
    // ...against `.agent`, which clips it, not against the initial containing block.
    const agent = /\.row \.agent\s*\{([^}]*)\}/.exec(css)
    expect(agent![1]).toMatch(/position:\s*relative/)
    expect(agent![1]).toMatch(/overflow:\s*hidden/)
  })
})
