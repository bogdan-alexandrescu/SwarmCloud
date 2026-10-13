// LANE HANDOFF-COPY (live-browser QA pass p12, 2026-10-13; part of #1030).
// Owner decision "Neutral wording": a hand-off is not always a sign-in.
//
//   * QA12-04: every hand-off was worded as a sign-in ("Parked until you are
//     ready to sign in", "I'm ready to sign in", "asked a person to sign in").
//     Now: "The agent needs your help", the agent's own reason as the
//     headline, "I'm ready"; password masking said as a fact.
//   * QA12-05: while the viewer held control the banner said
//     "<their own email> is in control". Now: "You are in control · return
//     control when done".
//   * QA12-06: after "I'm ready" the task is READY, `handoffOf` is null and the
//     banner unmounted, leaving ~83 s of "not running" with no reason. Now a
//     banner stays while a resumed hand-off is READY/LEASED/DISPATCHED/STARTING.
//   * QA12-21: one countdown format, m:ss, as the theater's checklist prints.
//   * QA12-28: the Overview card's actions run Take control (or I'm ready),
//     Open, then the rest.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'

import type { Task, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTask: vi.fn(), loadTaskEventsPage: vi.fn(), loadLiveSession: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { HandoffBanner, HandoffStrip, needsYou, resumingAfterHandoff, timeLeft } from '../Handoff'
import { HandoffCards } from '../Overview'
import { PARK_WORD } from '../components/StatePill'
import { REASON_COPY } from '../types'

const NOW = Date.parse('2026-10-13T12:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const ME = 'ada@acme.test'
const OTHER = 'grace@acme.test'
const REASON = 'Approve the payment on the bank page'

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

function holding(id: string, controller: string | null = null): Task {
  return task(id, 'RUNNING', {
    human_wait: {
      reason: REASON,
      expect: 'bank.example',
      requested_at: iso(NOW - 120_000),
      deadline: iso(NOW + 4 * 60_000 + 12_000),
      controller: controller === null ? null : { email: controller, since: iso(NOW - 30_000) },
    },
  })
}

function parked(id: string): Task {
  return task(id, 'PARKED', {
    park_reason: 'HUMAN_REQUIRED',
    next_eligible_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 3_600_000),
  })
}

/** What `human-ready` leaves on the task: READY, with `metadata.human_ready`. */
function resumed(id: string, state: TaskState): Task {
  return task(id, state, { metadata: { human_ready: { by: ME, at: iso(NOW - 20_000) } } })
}

function eventsWithRequest(): void {
  api.loadTaskEventsPage.mockResolvedValue({
    status: 'ok',
    fetchedAt: NOW,
    data: {
      next_page_token: null,
      events: [
        {
          event_id: 'ev_1',
          task_id: 'task_parked0000000000001',
          type: 'human_handoff_requested',
          at: iso(NOW - 4_200_000),
          attempt_id: 'att_1',
          lease_id: null,
          generation: 1,
          detail: { reason: REASON, expect: 'bank.example' },
        },
      ],
    },
  })
}

function sessionRead(you: string, controller: string | null) {
  return {
    status: 'ok',
    fetchedAt: NOW,
    data: {
      live_browser: true,
      session: {
        session_id: 'ls_copy',
        state: 'open',
        closed_reason: null,
        origin: { origin: 'https://bank.example', host: 'bank.example', registrable_domain: 'bank.example', scheme: 'https:' },
        human_wait: null,
        controller: controller === null ? null : { email: controller, since: iso(NOW - 30_000) },
        open_to_members: false,
        device: { preset: 'desktop', orientation: 'landscape', touch: false },
        presets: [],
        you: { email: you, can_control: true, can_preempt: false, can_open: true, can_switch_device: false },
      },
    },
  }
}

describe('QA12-04: a hand-off is worded for any reason, not as a sign-in', () => {
  it('a held hand-off leads with "The agent needs your help" and the agent\'s own reason', () => {
    render(<HandoffBanner task={holding('task_hold00000000000000001')} now={NOW} />)
    const text = screen.getByRole('status').textContent ?? ''
    expect(text).toContain(`The agent needs your help: ${REASON}`)
    expect(text).not.toMatch(/sign in|sign-in/i)
    // Password masking is a fact about the browser, not a step.
    expect(text).toMatch(/password fields are masked/i)
  })

  it('a parked hand-off offers "I\'m ready", and says nothing about signing in', async () => {
    eventsWithRequest()
    render(<HandoffBanner task={parked('task_parked0000000000001')} now={NOW} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain(REASON))
    const text = screen.getByRole('status').textContent ?? ''
    expect(text).toContain(`The agent needs your help: ${REASON}`)
    expect(text).not.toMatch(/sign in|sign-in/i)
    expect(screen.getByRole('button', { name: "I'm ready" })).toBeTruthy()
  })

  it('the park reason, as the Attempts tab and the pills print it, is not a sign-in', () => {
    expect(REASON_COPY.HUMAN_REQUIRED).not.toMatch(/sign in/i)
    expect(REASON_COPY.HUMAN_REQUIRED).toMatch(/asked for a person/i)
    expect(PARK_WORD.HUMAN_REQUIRED).toBe('waiting for you')
  })
})

describe('QA12-05: the person in control is told so in the second person', () => {
  it('says "You are in control · return control when done" when the caller says who you are', () => {
    render(<HandoffBanner task={holding('task_hold00000000000000001', ME)} now={NOW} you={ME} />)
    const text = screen.getByRole('status').textContent ?? ''
    expect(text).toContain('You are in control · return control when done')
    expect(text).not.toContain(`${ME} is in control`)
  })

  it('learns who you are from the session read when the caller did not say', async () => {
    api.loadLiveSession.mockResolvedValue(sessionRead(ME, ME))
    render(<HandoffBanner task={holding('task_hold00000000000000002', ME)} now={NOW} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('You are in control · return control when done'))
    expect(api.loadLiveSession).toHaveBeenCalledWith('task_hold00000000000000002')
  })

  it('still names somebody else who holds control', async () => {
    api.loadLiveSession.mockResolvedValue(sessionRead(ME, OTHER))
    render(<HandoffBanner task={holding('task_hold00000000000000003', OTHER)} now={NOW} />)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalled())
    const text = screen.getByRole('status').textContent ?? ''
    expect(text).toContain(`${OTHER} is in control`)
    expect(text).not.toMatch(/you are in control/i)
  })
})

describe('QA12-06: a resumed hand-off keeps a banner until the browser is back', () => {
  const STARTING = 'Starting your browser again — the agent will ask for you when it is back'

  it.each(['READY', 'LEASED', 'DISPATCHED', 'STARTING'] as const)('draws the banner while %s', (state) => {
    const t = resumed('task_resume0000000000001', state)
    expect(resumingAfterHandoff(t)).toBe(true)
    // It no longer needs you: Needs a look and the Waiting tab drop it.
    expect(needsYou(t)).toBe(false)
    render(<HandoffStrip task={t} />)
    expect(screen.getByRole('status').textContent).toContain(STARTING)
  })

  it('draws nothing for a READY task no person resumed, or once the browser is running', () => {
    const plain = task('task_plain0000000000000001', 'READY')
    const running = resumed('task_resume0000000000002', 'RUNNING')
    expect(resumingAfterHandoff(plain)).toBe(false)
    expect(resumingAfterHandoff(running)).toBe(false)
    const { container } = render(
      <>
        <HandoffStrip task={plain} />
        <HandoffStrip task={running} />
      </>,
    )
    expect(container.textContent).toBe('')
  })
})

describe('QA12-21: one countdown format, m:ss', () => {
  it('prints minutes and seconds as m:ss, and an hour or more as h:mm:ss', () => {
    expect(timeLeft(NOW + 4 * 60_000 + 12_000, NOW)).toBe('4:12 left')
    expect(timeLeft(NOW + 9_000, NOW)).toBe('0:09 left')
    expect(timeLeft(NOW + 23 * 3_600_000, NOW)).toBe('23:00:00 left')
    expect(timeLeft(NOW - 1, NOW)).toBeNull()
  })

  it('the banner and the Overview card print the same figure', () => {
    const t = holding('task_hold00000000000000004')
    api.loadLiveSession.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: { live_browser: true, session: null } })
    const { unmount } = render(<HandoffBanner task={t} now={NOW} />)
    expect(screen.getByRole('status').textContent).toContain('4:12 left')
    unmount()
    const { container } = render(<HandoffCards tasks={[t]} now={NOW} onOpen={() => {}} />)
    expect(container.querySelector('.c-banner')!.textContent).toContain('4:12 left')
  })
})

describe('QA12-28: the Overview card runs Take control, Open, then the rest', () => {
  it('on a held hand-off, Take control comes before Open', async () => {
    api.loadLiveSession.mockResolvedValue(sessionRead(ME, null))
    render(<HandoffCards tasks={[holding('task_hold00000000000000005')]} now={NOW} onOpen={() => {}} />)
    const banner = document.querySelector('.c-banner') as HTMLElement
    const take = await within(banner).findByRole('button', { name: /^Take control$/ })
    const open = within(banner).getByRole('link', { name: /^Open/ })
    expect(take.compareDocumentPosition(open) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it("on a parked hand-off, I'm ready comes before Open", async () => {
    eventsWithRequest()
    api.loadLiveSession.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: { live_browser: true, session: null } })
    render(<HandoffCards tasks={[parked('task_parked0000000000001')]} now={NOW} onOpen={() => {}} />)
    const banner = document.querySelector('.c-banner') as HTMLElement
    const ready = within(banner).getByRole('button', { name: "I'm ready" })
    const open = within(banner).getByRole('link', { name: /^Open/ })
    expect(ready.compareDocumentPosition(open) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('the strip passes who you are through to its banner', () => {
  it('says "You are in control" without a session read of its own', () => {
    render(<HandoffStrip task={holding('task_hold00000000000000006', ME)} you={ME} />)
    expect(screen.getByRole('status').textContent).toContain('You are in control · return control when done')
    expect(api.loadLiveSession).not.toHaveBeenCalled()
  })
})
