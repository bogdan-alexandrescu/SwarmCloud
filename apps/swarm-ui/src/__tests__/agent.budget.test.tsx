// THE PER-AGENT BUDGET WHERE PEOPLE WATCH AGENTS (#2459, BUDGET-UI-AGENT).
//
// The worker warns at 80% and stops at 100% (docs/design/agent-budget.md §5).
// Work › Agents marks a live agent past its warning with an amber
// `budget 80%`, and an agent the budget stopped reads `stopped at its budget`
// with the amount, not as a plain failure. The agent's Details carry a
// Budget card: the limit and where it came from, what was used, when it was
// warned, and for a stop that the work was kept and published. An agent with
// no budget shows nothing new, and makes no read for one.
//
// Both shapes are read: the one shipped (a `running` event with
// `detail.cause: budget_warning`; `last_error` beginning `budget_exceeded:`)
// and the one contract requests 121 and 118 add (`budget_warning` events;
// `end_cause: budget_exceeded`).
//
// MUTATIONS, one per block: draw the mark whenever `dispatch.budget` is set;
// drop the `stoppedAtBudget` branch from `rowReason`; take a warning from
// another generation; draw the Budget card for an unbudgeted agent.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Task, TaskEvent } from '../types'
import { at, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(async () => ({ status: 'empty', fetchedAt: Date.now() })),
  loadTaskEventsPage: vi.fn(),
  loadCheckpoints: vi.fn(async () => ({ status: 'empty', fetchedAt: Date.now() })),
  loadTaskLogs: vi.fn(async () => ({ status: 'empty', fetchedAt: Date.now() })),
  loadWorkflow: vi.fn(async () => ({ status: 'empty', fetchedAt: Date.now() })),
  loadArtifactContent: vi.fn(async () => ({ status: 'empty', fetchedAt: Date.now() })),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { TaskRow } = await import('../Agents')
const { Run } = await import('../AgentDetail')

const NOW = Date.parse(at(30))

/** `dispatch.budget` as `swarm_api.budget.dispatch_block` writes it: a step's $1 under a $5 default. */
const BUDGET = { usd: 1, from: { step: { usd: 1 }, workspace: { usd: 5 }, ceiling: { usd: 50 } } }
const DISPATCH = { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] }

function budgeted(over: Partial<Task> = {}): Task {
  return task({
    id: 'tsk_budget',
    state: 'RUNNING',
    started_at: at(1),
    current_generation: 2,
    heartbeat_at: at(29),
    dispatch: { ...DISPATCH, budget: BUDGET } as unknown as Task['dispatch'],
    ...over,
  })
}

/** The 80% warning as the worker writes it today: a `running` event with the cause. */
function warning(generation: number | null = 2, type = 'running'): TaskEvent {
  return {
    ...ev(type, at(20), 'att_2', {
      cause: 'budget_warning',
      percent: 80,
      told: true,
      limit: { usd: 1 },
      used: { tokens: 812000, usd: 0.81 },
    }),
    generation,
  }
}

function page(events: TaskEvent[]) {
  return { status: 'ok', fetchedAt: Date.now(), data: { events, next_page_token: null } }
}

const STOP_ERROR =
  'budget_exceeded: the agent used $1.02 of a budget of $1.00 and was stopped at a turn boundary; ' +
  'its work was checkpointed and published as it stood'

function row(t: Task) {
  return render(<TaskRow task={t} now={NOW} onOpen={() => {}} classes={null} />)
}

beforeEach(() => {
  api.loadTaskEventsPage.mockReset()
})

describe('Work › Agents: the budget mark on a row', () => {
  it('marks a running agent amber once its 80% warning is written', async () => {
    api.loadTaskEventsPage.mockResolvedValue(page([warning()]))
    const { container } = row(budgeted())
    await waitFor(() => expect(container.querySelector('.cr-budget.is-warned')).not.toBeNull())
    const mark = container.querySelector<HTMLElement>('.cr-budget.is-warned')!
    expect(mark.textContent).toBe('budget 80%')
    expect(mark.querySelector('.mark, svg, [class*="warn"]')).not.toBeNull()
    expect(mark.title).toContain('of its budget of $1.00')
    // The row's opener says it too: a screen reader hears the mark.
    expect(container.querySelector('.cr-open')!.getAttribute('aria-label')).toContain('Past 80% of its budget')
    expect(api.loadTaskEventsPage).toHaveBeenCalledWith('tsk_budget', { order: 'desc' })
  })

  it('takes the warning event in its own type once contract request 121 lands', async () => {
    api.loadTaskEventsPage.mockResolvedValue(page([{ ...warning(2, 'budget_warning'), detail: { percent: 80 } }]))
    const { container } = row(budgeted())
    await waitFor(() => expect(container.querySelector('.cr-budget.is-warned')).not.toBeNull())
  })

  it('draws no mark before the warning (control)', async () => {
    api.loadTaskEventsPage.mockResolvedValue(page([ev('running', at(2), 'att_2')]))
    const { container } = row(budgeted())
    await waitFor(() => expect(api.loadTaskEventsPage).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(container.querySelector('.cr-budget')).toBeNull()
    expect(container.textContent).not.toContain('budget 80%')
  })

  it("does not mark a continuation with its predecessor's warning", async () => {
    api.loadTaskEventsPage.mockResolvedValue(page([warning(1)]))
    const { container } = row(budgeted())
    await waitFor(() => expect(api.loadTaskEventsPage).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(container.querySelector('.cr-budget')).toBeNull()
  })

  it('reads a budget stop as one, with the amount, and not as a plain failure', () => {
    const { container } = row(budgeted({ state: 'FAILED', completed_at: at(28), last_error: STOP_ERROR }))
    expect(container.querySelector('.cr-budget.is-stopped')?.textContent).toBe('stopped at its budget')
    const why = container.querySelector<HTMLElement>('.why')!
    expect(why.textContent).toBe('Stopped at its budget of $1.00 after using $1.02; its work was kept and published.')
    expect(why.className).not.toContain('is-bad')
    expect(container.textContent).not.toContain('budget_exceeded:')
    // An ended agent reads no events for the row.
    expect(api.loadTaskEventsPage).not.toHaveBeenCalled()
  })

  it('reads the end cause alone as a budget stop, with the pinned limit', () => {
    const { container } = row(budgeted({ state: 'FAILED', completed_at: at(28), end_cause: 'budget_exceeded', last_error: null }))
    expect(container.querySelector('.cr-budget.is-stopped')).not.toBeNull()
    expect(container.querySelector('.why')!.textContent).toBe('Stopped at its budget of $1.00; its work was kept and published.')
  })

  it('a plain failure of a budgeted agent stays a failure (control)', () => {
    const { container } = row(budgeted({ state: 'FAILED', completed_at: at(28), last_error: 'runner exited 1' }))
    expect(container.querySelector('.cr-budget')).toBeNull()
    expect(container.querySelector('.why')!.className).toContain('is-bad')
  })

  it('shows nothing new, and reads nothing, for an agent with no budget', async () => {
    api.loadTaskEventsPage.mockResolvedValue(page([warning()]))
    const { container } = row(budgeted({ id: 'tsk_plain', dispatch: DISPATCH as unknown as Task['dispatch'] }))
    await new Promise((r) => setTimeout(r, 0))
    expect(container.querySelector('.cr-budget')).toBeNull()
    expect(container.textContent).not.toMatch(/budget/i)
    expect(api.loadTaskEventsPage).not.toHaveBeenCalled()
  })
})

function detail(t: Task, events: TaskEvent[] = []): AgentRun {
  return {
    task: t,
    events,
    eventsDetail: null,
    attempts: [],
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

describe("the agent's Details: the Budget card", () => {
  it('says the limit, where it came from, what it used and when it was warned', async () => {
    const { container } = render(<Run run={detail(budgeted(), [warning()])} />)
    const card = await waitFor(() => {
      const c = container.querySelector<HTMLElement>('section.dt-budget')
      expect(c).not.toBeNull()
      return c!
    })
    const c = within(card)
    expect(c.getByRole('heading', { name: 'Budget' })).toBeTruthy()
    expect(card.querySelector('[data-unit="usd"]')!.textContent).toBe('limit$1.00 · set by this step')
    expect(card.querySelector('[data-fact="used"]')!.textContent).toBe('used$0.81 of $1.00 at the warning')
    expect(card.querySelector('[data-fact="warned"]')!.textContent).toMatch(/^warnedat 80%, .* · the agent was told to finish and publish$/)
    expect(card.getAttribute('data-budget')).toBe('warned')
    expect(c.getByText('budget 80%')).toBeTruthy()
    // The page Details already holds had the warning: no second read.
    expect(api.loadTaskEventsPage).not.toHaveBeenCalled()
  })

  it('says a running agent short of its warning is under 80%, once its newest events are read', async () => {
    api.loadTaskEventsPage.mockResolvedValue(page([]))
    const { container } = render(<Run run={detail(budgeted())} />)
    await waitFor(() =>
      expect(container.querySelector('section.dt-budget [data-fact="used"]')?.textContent).toContain('under 80% so far'),
    )
    expect(container.querySelector('section.dt-budget [data-fact="warned"]')).toBeNull()
  })

  it('says a stop kept and published the work, and links its pull request', async () => {
    const stopped = budgeted({
      state: 'FAILED',
      completed_at: at(28),
      last_error: STOP_ERROR,
      result_summary: {
        budget: { limit: { usd: 1 }, used: { tokens: 1_020_000, usd: 1.02 }, warned: true, end_cause: 'budget_exceeded', stopped: 'turn_boundary' },
        git: { pull_request: { number: 2470, url: 'https://github.test/o/r/pull/2470', state: 'open' } },
      } as unknown as Task['result_summary'],
    })
    const { container } = render(<Run run={detail(stopped, [warning()])} />)
    const card = await waitFor(() => {
      const c = container.querySelector<HTMLElement>('section.dt-budget')
      expect(c).not.toBeNull()
      return c!
    })
    expect(within(card).getByRole('heading', { name: 'Budget: stopped at its budget' })).toBeTruthy()
    expect(card.querySelector('[data-fact="used"]')!.textContent).toBe('used$1.02 of $1.00')
    expect(card.querySelector('[data-fact="kept"]')!.textContent).toContain('Stopped at a turn boundary. Its work was checkpointed and published as it stood')
    const link = within(card).getByRole('link', { name: 'Pull request #2470' })
    expect(link.getAttribute('href')).toBe('https://github.test/o/r/pull/2470')
  })

  it('names a token limit from the workspace default', async () => {
    const t = budgeted({
      dispatch: { ...DISPATCH, budget: { tokens: 15_000_000, from: { step: {}, workspace: { tokens: 15_000_000 }, ceiling: {} } } } as unknown as Task['dispatch'],
    })
    api.loadTaskEventsPage.mockResolvedValue(page([]))
    const { container } = render(<Run run={detail(t)} />)
    await waitFor(() => expect(container.querySelector('section.dt-budget')).not.toBeNull())
    expect(container.querySelector('section.dt-budget [data-unit="tokens"]')!.textContent).toBe("limit15M tokens · the workspace's default")
  })

  it('draws no Budget card for an agent with no budget', async () => {
    const { container } = render(<Run run={detail(budgeted({ dispatch: DISPATCH as unknown as Task['dispatch'] }))} />)
    await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
    expect(container.querySelector('section.dt-budget')).toBeNull()
    expect(screen.queryByRole('heading', { name: /^Budget/ })).toBeNull()
    expect(api.loadTaskEventsPage).not.toHaveBeenCalled()
  })
})
