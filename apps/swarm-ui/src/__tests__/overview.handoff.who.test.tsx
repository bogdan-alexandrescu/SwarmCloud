// LANE LB-V5 (visual QA pass 5, 2026-10-12, U2; part of #1030). With five
// parked hand-offs, Overview's "Needs a look" drew five identical yellow cards
// ("Parked until you are ready to sign in: … expects github.com · 19h 14m
// left … [I'm ready to sign in] browse →") and nothing told them apart.
//
// Each card now names who is waiting: "<agent name> · <short task id> ·
// waiting <age>", and the workflow's label when it has one. It keeps the
// reason, the expected site, the time left and the actions. The cards run
// oldest-waiting first; past three, three are drawn and a "+N more waiting"
// link opens the Agents list's Waiting tab.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Task, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTask: vi.fn(), loadTaskEventsPage: vi.fn(), loadLiveSession: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { HandoffCards } from '../Overview'

const NOW = Date.parse('2026-10-12T12:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const MIN = 60_000
const HOUR = 3_600_000

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-ada',
    state,
    runner_profile: 'claude-code-browser',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: iso(NOW - 30 * HOUR),
    updated_at: iso(NOW - MIN),
    started_at: iso(NOW - 29 * HOUR),
    completed_at: null,
    submitted_by: 'ada@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
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

/** PARKED(HUMAN_REQUIRED) since `ago` ms. */
function parked(id: string, ago: number, over: Partial<Task> = {}): Task {
  return task(id, 'PARKED', {
    park_reason: 'HUMAN_REQUIRED',
    next_eligible_at: iso(NOW - ago),
    updated_at: iso(NOW - ago),
    ...over,
  })
}

/** RUNNING and holding for a person, asked `ago` ms ago. */
function holding(id: string, ago: number, over: Partial<Task> = {}): Task {
  return task(id, 'RUNNING', {
    human_wait: {
      reason: 'Sign in to GitHub as the bot account',
      expect: 'github.com',
      requested_at: iso(NOW - ago),
      deadline: iso(NOW - ago + 10 * MIN),
      controller: null,
    },
    ...over,
  })
}

function quiet(): void {
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no' } })
  api.loadTaskEventsPage.mockResolvedValue({
    status: 'ok',
    fetchedAt: NOW,
    data: {
      next_page_token: null,
      events: [
        {
          event_id: 'ev_1',
          task_id: 'x',
          type: 'human_handoff_requested',
          at: iso(NOW - 20 * HOUR),
          attempt_id: 'att_1',
          lease_id: null,
          generation: 1,
          detail: { reason: 'Please take control and look around the GitHub sign-in page', expect: 'github.com' },
        },
      ],
    },
  })
  api.loadLiveSession.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: { session: null } })
}

const cards = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.ov-handoff')]
const who = (c: HTMLElement): string => c.querySelector('.ov-handoff-who')?.textContent ?? ''

describe("the Overview's hand-off cards say which agent is waiting (LB-V5)", () => {
  it('name the agent, its short task id and how long it has waited', async () => {
    quiet()
    render(
      <HandoffCards
        tasks={[parked('task_aaaa1111bbbb2222cccc', 5 * HOUR, { step_id: 'login-github' })]}
        now={NOW}
        onOpen={() => {}}
      />,
    )
    const [c] = cards()
    expect(c).toBeTruthy()
    expect(who(c!)).toBe('login-github · aaaa1111 · waiting 5h')
    // The reason, the site, the time left and the action are kept.
    const banner = c!.querySelector('.c-banner')!
    await waitFor(() => expect(banner.textContent).toContain('look around the GitHub sign-in page'))
    expect(banner.textContent).toContain('github.com')
    expect(banner.textContent).toMatch(/19:00:00 left/)
    expect(screen.getByRole('button', { name: /I'm ready/ })).toBeTruthy()
  })

  it('names a lone agent by what it is, without repeating its id', () => {
    quiet()
    render(<HandoffCards tasks={[parked('task_dddd4444eeee5555ffff', 2 * HOUR)]} now={NOW} onOpen={() => {}} />)
    expect(who(cards()[0]!)).toBe('claude-code-browser agent · dddd4444 · waiting 2h')
  })

  it('counts a held hand-off from when the agent asked', () => {
    quiet()
    render(<HandoffCards tasks={[holding('task_hold00001111222233', 3 * MIN)]} now={NOW} onOpen={() => {}} />)
    expect(who(cards()[0]!)).toBe('claude-code-browser agent · hold0000 · waiting 3m')
  })

  it("adds the workflow's label when the run has one", async () => {
    quiet()
    api.loadTask.mockImplementation(async (id: string) => ({
      status: 'ok',
      fetchedAt: NOW,
      data: task(id, 'PARKED', { workflow_id: 'wf_lbv5', metadata: { title: 'Nightly dependency bump' } }),
    }))
    render(
      <HandoffCards
        tasks={[parked('task_wf001111222233334444', HOUR, { workflow_id: 'wf_lbv5', step_id: 'sign-in' })]}
        now={NOW}
        onOpen={() => {}}
      />,
    )
    await waitFor(() => expect(who(cards()[0]!)).toBe('sign-in · wf001111 · waiting 1h · Nightly dependency bump'))
  })

  it('run oldest-waiting first, a held hand-off included', () => {
    quiet()
    render(
      <HandoffCards
        tasks={[
          holding('task_newest0000000000000', 2 * MIN),
          parked('task_middle0000000000000', 3 * HOUR),
          parked('task_oldest0000000000000', 9 * HOUR),
        ]}
        now={NOW}
        onOpen={() => {}}
      />,
    )
    expect(cards().map((c) => who(c).split(' · ')[1])).toEqual(['oldest00', 'middle00', 'newest00'])
  })

  it('past three, draw the three oldest and link "+N more waiting" to the Waiting tab', () => {
    quiet()
    const five = [1, 2, 3, 4, 5].map((n) => parked(`task_park${n}000000000000000`, n * HOUR))
    render(<HandoffCards tasks={five} now={NOW} onOpen={() => {}} />)
    expect(cards().map((c) => who(c).split(' · ')[1])).toEqual(['park5000', 'park4000', 'park3000'])
    const more = screen.getByRole('link', { name: '+2 more waiting' })
    expect(more.getAttribute('href')).toBe('/agents/waiting')
    // The group still counts every agent that is waiting, not the three drawn.
    expect(document.querySelector('.ov-handoffs')?.getAttribute('aria-label')).toBe('5 agents are waiting for a person')
  })

  it('draws no "more" link at three or fewer', () => {
    quiet()
    const three = [1, 2, 3].map((n) => parked(`task_park${n}000000000000000`, n * HOUR))
    render(<HandoffCards tasks={three} now={NOW} onOpen={() => {}} />)
    expect(cards()).toHaveLength(3)
    expect(screen.queryByRole('link', { name: /more waiting/ })).toBeNull()
  })
})
