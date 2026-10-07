/**
 * G2 (QA pass on deployed dev, 2026-10-07): the Agents list.
 *
 *   G2-04  row elapsed and waiting ages stopped at one poll interval past the
 *          read, so with the read 5 min old the list said `12m 44s` beside an
 *          inspector saying `17m 33s` for the same task.
 *   G2-05  the tab badges and the Recent chips printed the loaded page's count
 *          as if it were the population: `Recent 191` at 1440, `Recent 46` on
 *          a phone, for the same data.
 *   G2-21  beside an open agent the list head's read status was the part cut:
 *          `read 1m ago ·…`, `not refreshe…`.
 *   G2-22  Failed first and Group by workflow lived only in component state,
 *          and the Recent state filter was dropped once an agent was opened.
 *   G2-24  a shared reason was hidden on every row after the first, across
 *          workflows, with nothing on screen saying so.
 *
 * MUTATIONS, one per block: hand the rows `rowClock(...)` again; drop the `+`
 * when a next page exists; drop the split rule that puts the meta chip on
 * its own line; drop the list flags from `addressToPath`/`pathToAddress` or
 * from the AgentsScreen effect; compare reasons without the workflow, or
 * drop the `same reason` marker. Each turns a case red.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({ loadTasks: vi.fn(), loadResourceClasses: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { Screen } = await import('../Shell')
const { addressToPath, pathToAddress } = await import('../paths')
const { backLabel, parseAgentList } = await import('../agentlist')
const { resetListSnap } = await import('../listSnap')

const WIDE: CascadeEnv = { width: 1440 }
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

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

async function land(page: TaskPage, props: Partial<Parameters<typeof AgentsScreen>[0]> = {}): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue(ok(page))
  const { container } = render(<AgentsScreen onOpen={() => {}} {...props} />)
  await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
  await act(async () => {})
  return container as HTMLElement
}

function chip(name: string): HTMLElement {
  return within(screen.getByRole('group', { name: 'Recent, by state' })).getByRole('button', { name: new RegExp(`^${name}`) })
}

function tabNamed(name: string): HTMLElement {
  return screen.getAllByRole('tab').find((t) => t.textContent?.startsWith(name))!
}

beforeEach(() => {
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: {} }))
})

afterEach(() => {
  vi.useRealTimers()
  resetListSnap()
  delete document.documentElement.dataset.agentList
})

describe('G2-04: a row’s elapsed and waiting ages move with the clock', () => {
  it('keeps counting after the read has aged, and still never ages a heartbeat past the read', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    const start = Date.now()
    // THE FIRST READ LANDS AND EVERY RE-READ FAILS: the read ages, as dev's did.
    let reads = 0
    api.loadTasks.mockImplementation(async () => {
      reads += 1
      if (reads > 1) {
        return {
          status: 'error',
          error: { kind: 'upstream_degraded', httpStatus: 503, code: 'upstream_unavailable', message: 'Busy.' },
        }
      }
      return ok({
        tasks: [
          task('task_ffffffff00000000000f', 'RUNNING', {
            step_id: 'implement',
            started_at: new Date(start - 60_000).toISOString(),
            heartbeat: 'read',
            heartbeat_at: new Date(start).toISOString(),
            heartbeat_grace_seconds: 30,
          }),
          task('task_eeeeeeee00000000000e', 'READY', {
            step_id: 'review',
            started_at: null,
            created_at: new Date(start - 60_000).toISOString(),
          }),
        ],
        tenant_id: 'acme',
      } as TaskPage)
    })
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    const when = (id: string) => container.querySelector(`[data-task-id="${id}"] .when`)?.textContent ?? ''
    await advance(0)
    expect(when('task_ffffffff00000000000f')).toBe('1m 0s')
    await advance(63_000)
    expect(reads, 'the list did not try to re-read').toBeGreaterThan(1)
    expect(when('task_ffffffff00000000000f'), 'the running row stopped at the read').toBe('2m 3s')
    // The Waiting tab's age is the same clock.
    fireEvent.click(tabNamed('Waiting'))
    await advance(0)
    expect(when('task_eeeeeeee00000000000e'), 'the waiting age stopped at the read').toBe('waiting 2m 3s')
    // A beat the read can no longer see is not silence: the heartbeat line
    // stays on the clock the read vouches for.
    fireEvent.click(tabNamed('Live'))
    await advance(0)
    expect(container.querySelector('[data-task-id="task_ffffffff00000000000f"] .why.is-warn')).toBeNull()
  })
})

describe('G2-05: a count over a capped page says it is a lower bound', () => {
  const capped: TaskPage = {
    tasks: [
      task('task_aaaaaaaa00000000000a', 'RUNNING'),
      task('task_bbbbbbbb00000000000b', 'FAILED'),
      task('task_cccccccc00000000000c', 'FAILED'),
      task('task_dddddddd00000000000d', 'SUCCEEDED'),
    ],
    next_page_token: 'more',
  }

  it('suffixes the tab badges and the Recent chips with `+` when more rows exist', async () => {
    await land(capped, { list: { tab: 'recent', state: null } })
    expect(tabNamed('Recent').querySelector('.badge')?.textContent).toBe('3+')
    expect(tabNamed('Live').querySelector('.badge')?.textContent).toBe('1+')
    expect(tabNamed('Recent').getAttribute('aria-label')).toMatch(/^Recent: .*at least 3 among the 4 newest read/)
    const failed = chip('failed')
    expect(failed.querySelector('em')?.textContent).toBe('2+')
    expect(failed.querySelector('em')?.getAttribute('title')).toMatch(/at least 2/)
  })

  it('prints a plain count when the page is every row there is', async () => {
    await land({ ...capped, next_page_token: null }, { list: { tab: 'recent', state: null } })
    expect(tabNamed('Recent').querySelector('.badge')?.textContent).toBe('3')
    expect(chip('failed').querySelector('em')?.textContent).toBe('2')
  })
})

describe('G2-21: beside an open agent the read status keeps its row', () => {
  it('drops the meta chip to its own line and gives the age the head row', async () => {
    const { container } = render(
      <div className="app has-inspector">
        <main className="work">
          <Screen title="Agents" load={async () => ok({ n: 200 })} summary={(d) => <>{d.n} loaded · 5 live · eng</>}>
            {() => <p>rows</p>}
          </Screen>
        </main>
      </div>,
    )
    const age = await waitFor(() => {
      const a = container.querySelector<HTMLElement>('.c-age')
      expect(a?.querySelector('button')).toBeTruthy()
      return a!
    })
    const sub = container.querySelector('.c-phead > .sub')!
    const meta = container.querySelector('.c-meta')!
    expect(painted(sub, 'flex-wrap', WIDE), 'the chip and the age still share one line').toBe('wrap')
    expect(painted(age, ['flex', 'flex-basis'], WIDE)).toMatch(/\b100%$/)
    expect(Number(painted(meta, 'order', WIDE) ?? '0')).toBeGreaterThan(Number(painted(age, 'order', WIDE) ?? '0'))
    // Outside the split the head is one row as before.
    const plain = document.createElement('div')
    plain.innerHTML = '<div class="c-phead"><div class="head"><h1>Agents</h1></div><p class="sub"><span class="c-meta">x</span><span class="c-age">y</span></p></div>'
    document.body.appendChild(plain)
    try {
      expect(painted(plain.querySelector('.sub')!, 'flex-wrap', WIDE)).toBe('nowrap')
    } finally {
      plain.remove()
    }
  })
})

describe('G2-22: the list’s filters are in the address', () => {
  it('writes and reads state, grouping and failed-first on the list path', () => {
    const address = 'work/running/recent/failed?group=wf&first=failed'
    const path = addressToPath(address)
    expect(path).toBe('/agents/recent?state=failed&group=wf&first=failed')
    expect(pathToAddress('/agents/recent', '?state=failed&group=wf&first=failed')?.address).toBe(address)
    expect(parseAgentList(['recent', 'failed'], 'group=wf&first=failed')).toEqual({
      tab: 'recent',
      state: 'failed',
      grouped: true,
      failedFirst: true,
    })
    // The plain list is unchanged.
    expect(addressToPath('work/running/recent/failed')).toBe('/agents/recent?state=failed')
    expect(parseAgentList(['recent'], '')).toEqual({ tab: 'recent', state: null })
    expect(backLabel('work/running/recent?group=wf')).toBe('Recent')
  })

  it('keeps them under an open agent, so a reload lands on the same filtered list', () => {
    const list = { tab: 'recent' as const, state: 'failed' as const, grouped: true }
    const path = addressToPath('work/task/task_0123456789abcdef0123', list)
    expect(path).toBe('/agents/recent/task_0123456789abcdef0123?state=failed&group=wf')
    const back = pathToAddress('/agents/recent/task_0123456789abcdef0123', '?state=failed&group=wf')
    expect(back?.address).toBe('work/task/task_0123456789abcdef0123')
    expect(back?.agentTab).toBe('recent')
    expect(back?.list).toEqual(list)
  })

  it('draws the toggles from the address, and reports a toggle as an address', async () => {
    const page: TaskPage = {
      tasks: [task('task_bbbbbbbb00000000000b', 'FAILED', { workflow_id: 'wf_one' })],
    }
    const onList = vi.fn()
    await land(page, { list: { tab: 'recent', state: 'failed', grouped: true, failedFirst: true }, onList })
    const first = screen.getByRole('checkbox', { name: 'Failed first' }) as HTMLInputElement
    const group = screen.getByRole('checkbox', { name: 'Group by workflow' }) as HTMLInputElement
    expect(first.checked).toBe(true)
    expect(group.checked).toBe(true)
    fireEvent.click(group)
    expect(onList).toHaveBeenLastCalledWith({ tab: 'recent', state: 'failed', failedFirst: true })
    fireEvent.click(first)
    expect(onList).toHaveBeenLastCalledWith({ tab: 'recent', state: 'failed' })
  })
})

describe('G2-24: a reason is said once only within one workflow, and says so', () => {
  const why = 'Waiting on an earlier step in its workflow.'

  it('collapses a repeated reason inside a workflow and marks the row that carries it', async () => {
    const page: TaskPage = {
      tasks: [
        task('task_aaaaaaaa00000000000a', 'PARKED', { park_reason: 'DEPENDENCY_INCOMPLETE', workflow_id: 'wf_one', step_id: 'a', updated_at: '2026-09-23T12:00:03.000Z' }),
        task('task_bbbbbbbb00000000000b', 'PARKED', { park_reason: 'DEPENDENCY_INCOMPLETE', workflow_id: 'wf_one', step_id: 'b', updated_at: '2026-09-23T12:00:02.000Z' }),
        task('task_cccccccc00000000000c', 'PARKED', { park_reason: 'DEPENDENCY_INCOMPLETE', workflow_id: 'wf_two', step_id: 'c', updated_at: '2026-09-23T12:00:01.000Z' }),
      ],
    }
    const c = await land(page)
    const row = (id: string) => c.querySelector<HTMLElement>(`[data-task-id="${id}"]`)!
    const shown = (id: string) => row(id).querySelector(':scope > .why:not(.is-same)')?.textContent ?? null
    expect(shown('task_aaaaaaaa00000000000a')).toBe(why)
    // Same workflow, same reason: said once, and the row says that it was.
    expect(shown('task_bbbbbbbb00000000000b')).toBeNull()
    const mark = row('task_bbbbbbbb00000000000b').querySelector(':scope > .why.is-same')
    expect(mark?.textContent).toBe('same reason')
    expect(row('task_bbbbbbbb00000000000b').querySelector('.why.is-shared')?.textContent, 'a screen reader lost the reason').toBe(why)
    // Another workflow's row prints its own reason, though it is the same words.
    expect(shown('task_cccccccc00000000000c')).toBe(why)
    expect(row('task_cccccccc00000000000c').querySelector('.why.is-same')).toBeNull()
  })

  it('draws the marker muted at the 12px floor', async () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="row clickable is-compact"><span class="why is-same">same reason</span></div>'
    document.body.appendChild(host)
    try {
      const mark = host.querySelector('.why.is-same')!
      expect(painted(mark, 'color', WIDE)).toBe('var(--text-dim)')
      expect(painted(mark, 'font-size', WIDE)).toBe('var(--t-micro)')
    } finally {
      host.remove()
    }
  })
})
