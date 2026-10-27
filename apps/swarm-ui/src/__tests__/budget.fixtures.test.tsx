// THE BUDGET CARD'S FIXTURES (#2459, BUDGET-FIXTURES).
//
// The agent's Budget card (AgentBudget.tsx, BUDGET-UI-AGENT) had no task in
// the console's fixture build to draw it, so neither a developer nor a visual
// QA run could open it. BGD's answer, 2026-10-27: add them BY ID, OFF THE
// LISTS. Three agents a per-task read answers -- a running one past its 80%
// warning, one the worker stopped on its budget, one with no budget -- that no
// list read returns, because other tests pin every list and every count.
//
// These open each id through the real fixture loaders, as the agent page
// does, and read what the card draws; nothing is mocked.
//
// MUTATIONS, one per block: drop a budget fixture from the by-id lookup
// (its page 404s); drop the warning event (the card reads "under 80%");
// give the stopped one a plain `last_error` and no end cause (no stop
// heading); give the unbudgeted one a `dispatch.budget` (a card is drawn);
// list the three in `fixtureTaskRows` (the list test fails).

import { describe, expect, it } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { loadAgentDetail, loadAttempts, loadTasks, loadTaskWindow } from '../api'

const { AgentDetailScreen } = await import('../AgentDetail')

const WARN = 'task_budget_warn'
const EXCEEDED = 'task_budget_exceeded'
const NONE = 'task_budget_none'
const IDS = [WARN, EXCEEDED, NONE]
const LONG = 20_000

function open(id: string) {
  return render(<AgentDetailScreen taskId={id} onClose={() => {}} />)
}

/** The page has drawn the agent's own read (the facts strip), so an absent card is absent, not unread. */
async function drawn(container: HTMLElement): Promise<void> {
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull(), { timeout: LONG })
}

async function card(container: HTMLElement): Promise<HTMLElement> {
  return waitFor(
    () => {
      const c = container.querySelector<HTMLElement>('section.dt-budget')
      expect(c).not.toBeNull()
      return c!
    },
    { timeout: LONG },
  )
}

describe('the fixture build answers the three budget agents by id', () => {
  it('reads every one, with its own id and state', async () => {
    expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
    const states = await Promise.all(
      IDS.map(async (id) => {
        const r = await loadAgentDetail(id)
        expect(r.status, `${id} answered ${r.status}`).toBe('ok')
        return r.status === 'ok' ? [r.data.task.id, r.data.task.state, r.data.task.runner_profile] : null
      }),
    )
    expect(states).toEqual([
      [WARN, 'RUNNING', 'claude-code'],
      [EXCEEDED, 'FAILED', 'claude-code'],
      [NONE, 'SUCCEEDED', 'claude-code'],
    ])
  })

  it("the stopped agent's attempt reports the spend its budget stop says", async () => {
    const r = await loadAttempts(EXCEEDED)
    expect(r.status).toBe('ok')
    const attempts = r.status === 'ok' ? r.data.attempts : []
    expect(attempts).toHaveLength(1)
    const a = attempts[0]!
    expect(a.cost_usd).toBe(1.02)
    expect((a.input_tokens ?? 0) + (a.output_tokens ?? 0) + (a.cache_read_input_tokens ?? 0) + (a.cache_creation_input_tokens ?? 0)).toBe(1_020_000)
  })
})

describe("opening each budget agent draws its Budget card's state", () => {
  it('a running agent past 80% reads warned, with the amber mark', async () => {
    const { container } = open(WARN)
    const c = await card(container)
    expect(c.getAttribute('data-budget')).toBe('warned')
    expect(c.className).toContain('is-warn')
    expect(c.querySelector('h2')!.textContent).toBe('Budget')
    expect(c.querySelector('.dt-card-r')!.textContent).toBe('budget 80%')
    expect(c.querySelector('[data-unit="usd"]')!.textContent).toBe('limit$1.00 · set by this step')
    expect(c.querySelector('[data-fact="used"]')!.textContent).toBe('used$0.81 of $1.00 at the warning')
    expect(c.querySelector('[data-fact="warned"]')!.textContent).toMatch(/^warnedat 80%, .* · the agent was told to finish and publish$/)
  }, LONG + 10_000)

  it('a FAILED agent the worker stopped on its budget reads stopped, its work kept', async () => {
    const { container } = open(EXCEEDED)
    const c = await card(container)
    expect(c.getAttribute('data-budget')).toBe('stopped')
    expect(c.className).toContain('is-stopped')
    expect(c.querySelector('h2')!.textContent).toBe('Budget: stopped at its budget')
    expect(c.querySelector('[data-fact="used"]')!.textContent).toBe('used$1.02 of $1.00')
    expect(c.querySelector('[data-fact="kept"]')!.textContent).toContain(
      'Stopped at a turn boundary. Its work was checkpointed and published as it stood',
    )
    expect(c.querySelector('a')!.textContent).toBe('Pull request #2470')
  }, LONG + 10_000)

  it('a SUCCEEDED agent with no budget draws no Budget card', async () => {
    const { container } = open(NONE)
    await drawn(container)
    expect(container.querySelector('section.dt-budget')).toBeNull()
    // Its own read was drawn, a finished run (an unknown id draws no strip).
    expect(container.querySelector('.dt-strip')!.textContent).toMatch(/^Ran · attempt/)
    // Nothing budget-shaped beyond its own id: no heading, no mark, no stop.
    expect(container.textContent).not.toMatch(/Budget|budget \d+%|at its budget/)
  }, LONG + 10_000)
})

describe('no list read returns a budget agent', () => {
  it('the agents list and the task window hold none of the three ids', async () => {
    const [list, window] = await Promise.all([loadTasks(), loadTaskWindow(1000)])
    expect(list.status).toBe('ok')
    expect(window.status).toBe('ok')
    const listed = list.status === 'ok' ? list.data.tasks.map((t) => t.id) : []
    const windowed = window.status === 'ok' ? window.data.tasks.map((t) => t.id) : []
    // Control: the lists were read, and they hold the ordinary agents.
    expect(listed).toContain('task_a073aff5')
    expect(windowed).toContain('task_a073aff5')
    for (const id of IDS) {
      expect(listed, `${id} is on the agents list`).not.toContain(id)
      expect(windowed, `${id} is in the task window`).not.toContain(id)
    }
  })
})
