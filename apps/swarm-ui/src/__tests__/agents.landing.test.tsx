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

describe('the list says what its columns are', () => {
  /**
   * AG-16. `1/3`, `1u` and `4m 12s` had no heading, so three columns were read
   * off their values. The heads are a `.row` whose cells carry the SAME
   * classes, in the same order, as an agent row -- which is what makes every
   * breakpoint that drops a column drop its head too. Asserted as that
   * property, cell by cell, rather than as a list of words.
   *
   * BREAK IT: remove `<RowHead />`, or give a head cell a class the row's cell
   * in that position does not carry.
   */
  it('heads each list with a row on the same cells as an agent row', async () => {
    const container = await land([task('task_ffffffff00000000000f', 'RUNNING')])
    const rows = container.querySelector('.rows')!
    const head = rows.firstElementChild as HTMLElement
    expect(head.classList.contains('is-head'), 'the list has no heading row').toBe(true)
    expect(head.classList.contains('row'), 'the heads are not on the row grid').toBe(true)
    const agent = rows.querySelector('.row.clickable')!
    const cellClass = (el: Element) => el.className.split(/\s+/)[0]
    const headCells = [...head.children].map(cellClass)
    const rowCells = [...agent.children].map(cellClass)
    // The state cell is a `.ctl-chip` on a row and a word in the head; from
    // the name onwards every head sits over the cell it names.
    for (let i = 1; i < headCells.length; i++) {
      expect(headCells[i], `head ${i} is not over the cell it names`).toBe(rowCells[i])
    }
    // The three the finding named are labelled.
    const text = (cls: string) => head.querySelector(`.${cls}`)?.textContent ?? ''
    expect(text('try')).not.toBe('')
    expect(text('class')).not.toBe('')
    expect(text('when')).not.toBe('')
    // A heading is not an agent: it opens nothing.
    expect(head.getAttribute('role')).toBeNull()
    expect(head.hasAttribute('tabindex')).toBe(false)
  })

  it('heads each workflow group as well', async () => {
    const container = await land([
      task('task_dddddddd00000000000d', 'SUCCEEDED', { workflow_id: 'wf_one', step_id: 'a' }),
      task('task_eeeeeeee00000000000e', 'FAILED', { workflow_id: 'wf_two', step_id: 'b' }),
    ])
    fireEvent.click(screen.getByLabelText(/group by workflow/i))
    await waitFor(() => expect(container.querySelectorAll('.section.group').length).toBe(2))
    for (const group of container.querySelectorAll('.section.group .rows')) {
      expect(group.firstElementChild?.classList.contains('is-head'), 'a workflow group has no heads').toBe(true)
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
    const cells = [...container.querySelectorAll('.row.clickable .try')]
    expect(cells).toHaveLength(2)
    const over = cells.find((c) => c.textContent === '4/3')!
    const spent = cells.find((c) => c.textContent === '3/3')!
    expect(over.classList.contains('is-over'), '4/3 is drawn like any other count').toBe(true)
    expect(over.getAttribute('aria-label') ?? '').toMatch(/1 over the ceiling/)
    expect(spent.classList.contains('is-over'), '3/3 is drawn as over its cap').toBe(false)
    expect(spent.getAttribute('aria-label') ?? '').not.toMatch(/over/)
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

describe('the row clock does not run past the read (AG-1)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /**
   * The list read once and its 1s clock went on adding to every row: a
   * finished agent read `running` and its elapsed kept climbing, on a page
   * nobody had re-read. The cadence is §2.5's, and the clock stops one
   * interval past the read -- when a fresh read was due and has not come.
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
   * BREAK IT: go back to `elapsed(task, Date.now())` on a 1s interval. A
   * minute later the row reads `2m 3s` for an agent that was read at `1m 0s`.
   */
  it('stops a running row’s elapsed time once the read is older than one interval', async () => {
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
    // Inside one interval the clock runs, as it should.
    await advance(3_000)
    expect(when()).toBe('1m 3s')
    // A minute on, with no fresh read, it has stopped at the interval's edge.
    await advance(60_000)
    expect(reads, 'the list did not try to re-read at all').toBeGreaterThan(1)
    expect(when(), 'the row went on counting past a read nobody refreshed').toBe('1m 5s')
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
    await waitFor(() => expect(container.querySelector('.ctl-seg [role="tab"]')).not.toBeNull())
    return container as HTMLElement
  }

  const states = (c: HTMLElement) =>
    [...c.querySelectorAll('.rows .row.clickable .ctl-chip')].map((n) => (n.textContent ?? '').trim())

  it('opens recent/failed on Recent with only the FAILED rows, though Live has one', async () => {
    const c = await landAt(MIXED, { list: { tab: 'recent', state: 'failed' } as const })
    expect(selectedTab()).toBe('Recent')
    expect(states(c)).toEqual(['FAILED'])
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
    await waitFor(() => expect(states(c)).toEqual(['CANCELLED']))
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
