// LANE QA1-FIX-11 (console visual QA pass 1, part of #2596), finding
// chrome-C28: a parked hand-off whose request event could not be read said
// "The agent needs your help: The agent gave no reason that could be read …
// I'm ready" and never said what the person should be ready FOR. With no
// reason to say it, the banner now says what pressing I'm ready does and what
// it asks of the person: its browser starts again and the agent asks there,
// so be ready to stay with that browser.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'

import type { Task, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTask: vi.fn(), loadTaskEventsPage: vi.fn(), loadLiveSession: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { HandoffBanner } from '../Handoff'

const NOW = Date.parse('2026-10-28T10:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const ME = 'ada@example.test'

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-ada',
    state,
    runner_profile: 'claude-code-browser',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 60_000),
    started_at: iso(NOW - 3_000_000),
    completed_at: null,
    submitted_by: ME,
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

function parked(id: string): Task {
  return task(id, 'PARKED', {
    park_reason: 'HUMAN_REQUIRED',
    next_eligible_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 3_600_000),
  })
}

function events(detail: Record<string, unknown> | null) {
  api.loadTaskEventsPage.mockResolvedValue({
    status: 'ok',
    fetchedAt: NOW,
    data: {
      next_page_token: null,
      events:
        detail === null
          ? []
          : [
              {
                event_id: 'ev_1',
                task_id: 'task_parked0000000000001',
                type: 'human_handoff_requested',
                at: iso(NOW - 4_200_000),
                attempt_id: 'att_1',
                lease_id: null,
                generation: 1,
                detail,
              },
            ],
    },
  })
}

const BE_READY = /its browser starts again and the agent asks you there for what it needs/

describe('chrome-C28: a parked hand-off with no readable reason says what to be ready for', () => {
  it('says what I\'m ready does when the request event is missing', async () => {
    // MUTATION: delete the `ho-unread` sentence in Handoff.tsx and the banner
    // reads "no reason that could be read … I'm ready" with nothing between,
    // the live walk-through's text.
    events(null)
    render(<HandoffBanner task={parked('task_parked0000000000001')} now={NOW} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/no reason that could be read/))
    const banner = screen.getByRole('status')
    expect(banner.textContent).toMatch(BE_READY)
    expect(within(banner).getByRole('button', { name: "I'm ready" })).toBeTruthy()
  })

  it('says it too when the event carries no reason', async () => {
    events({ reason: null, expect: 'bank.example' })
    render(<HandoffBanner task={parked('task_parked0000000000001')} now={NOW} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/no reason that could be read/))
    expect(screen.getByRole('status').textContent).toMatch(BE_READY)
  })

  it('stays out of the way when the agent said why: its reason says what to be ready for', async () => {
    // MUTATION: draw the sentence whatever the reason and this fails.
    events({ reason: 'Approve the payment on the bank page', expect: 'bank.example' })
    render(<HandoffBanner task={parked('task_parked0000000000001')} now={NOW} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/Approve the payment/))
    expect(screen.getByRole('status').textContent).not.toMatch(BE_READY)
  })

  it('is not drawn while the reason is still being read', () => {
    api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
    render(<HandoffBanner task={parked('task_parked0000000000001')} now={NOW} />)
    expect(screen.getByRole('status').textContent).toMatch(/Reading why it asked/)
    expect(screen.getByRole('status').textContent).not.toMatch(BE_READY)
  })
})
