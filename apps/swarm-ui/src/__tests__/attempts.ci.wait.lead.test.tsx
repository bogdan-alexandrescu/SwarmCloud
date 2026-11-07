// A RE-CHECKED MERGE STEP'S ATTEMPTS PAGE LEADS WITH ONE LINE (part of #2688).
//
// owner, 6 November 2026: a merge step waited on CI for 8 hours and its page at
// /agents/waiting/<id>/attempts listed 28 identical re-check attempts, one by
// one, and nothing said how long, how often or why. The page now leads with
// the wait's one line -- the API's summary for a wait handed to a person, or
// the span and re-checks from the park's own record -- and folds the earlier
// attempts under the newest.
//
// MUTATION: drop the `lead` block in AttemptTimeline's `Body`. The first two
// tests fail: no line, and all thirty cards drawn open. The third is the
// control: a step that re-checked once draws as before.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AttemptRow, Task } from '../types'
import { at, attempt, task } from './runfixture'
import { ciWaitLead, waitSpan } from '../ciwaitlead'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const LINE =
  'CI on PR #3412 waited 8 h 0 m over 30 re-checks; failing: unit (worker); fix rounds 2 of 2 spent; main is red on unit (worker) too'

function reChecks(n: number): AttemptRow[] {
  return Array.from({ length: n }, (_, i) =>
    attempt(i + 1, { exit_code: 75, error: 'CI_PENDING', completed_at: at(i * 16 + 1) }),
  )
}

function stuck(): Task {
  return task({
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'merge',
    waiting: {
      reason: 'HUMAN_REQUIRED',
      summary: LINE,
      since: null,
      resumes_when: 'a member of the workspace calls human-ready',
      detail: { park_phase: 'ci_wait_stuck', re_checks: 30 },
      history: [],
    },
  } as Partial<Task>)
}

async function mount(t: Task, attempts: AttemptRow[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events: [], eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

describe('a merge step re-checked for hours', () => {
  it('leads with the wait handed to a person, before any attempt card', async () => {
    const el = await mount(stuck(), reChecks(30))
    const lead = el.querySelector<HTMLElement>('[data-ci-wait-lead]')
    expect(lead, 'no lead line').not.toBeNull()
    expect(lead!.textContent).toContain(LINE)
    expect(lead!.getAttribute('data-stuck')).toBe('true')
    const first = el.querySelector('[data-ci-wait-lead], .att-card')
    expect(first, 'an attempt card comes before the line').toBe(lead)
  })

  it('folds the 29 earlier re-checks under the newest attempt', async () => {
    const el = await mount(stuck(), reChecks(30))
    const earlier = el.querySelector<HTMLDetailsElement>('[data-ci-wait-earlier]')
    expect(earlier, 'the earlier attempts are not folded').not.toBeNull()
    expect(earlier!.open).toBe(false)
    expect(earlier!.querySelector('summary')?.textContent).toBe('29 earlier attempts')
    const open = Array.from(el.querySelectorAll('.att-card')).filter((c) => !earlier!.contains(c))
    expect(open).toHaveLength(1)
  })

  it('draws a step that re-checked once as before', async () => {
    const t = task({ state: 'PARKED', park_reason: 'CI_PENDING', runner_profile: 'merge' } as Partial<Task>)
    const el = await mount(t, reChecks(1))
    expect(el.querySelector('[data-ci-wait-lead]')).toBeNull()
    expect(el.querySelector('[data-ci-wait-earlier]')).toBeNull()
  })
})

describe('ciWaitLead, from the park record', () => {
  it('spans the whole wait and counts its re-checks while CI is still pending', () => {
    const now = Date.parse('2026-11-06T05:20:00Z')
    const t = task({
      state: 'PARKED',
      park_reason: 'CI_PENDING',
      runner_profile: 'merge',
      metadata: {
        merge_wait: {
          wait_started_at: '2026-11-05T21:18:00Z',
          rechecks: 30,
          failed_heads: { 'unit (worker)': ['a'] },
        },
      },
    } as Partial<Task>)
    expect(ciWaitLead(t, now)).toEqual({
      line: 'CI wait 8 h 2 m · 30 re-checks · failed: unit (worker)',
      stuck: false,
      rechecks: 30,
    })
  })

  it('is null for any task that is not a parked merge step', () => {
    expect(ciWaitLead(task({ state: 'PARKED', runner_profile: 'claude-code' } as Partial<Task>), 0)).toBeNull()
    expect(waitSpan(40 * 60_000)).toBe('40 m')
  })
})

// THE BANNER ABOVE THE LINE IS NOT A BROWSER HAND-OFF (review, part of #2688).
// A stuck CI wait parks HUMAN_REQUIRED, which `handoffOf` drew as a
// live-browser hand-off: "The agent gave no reason that could be read" and
// "its browser starts again" above a merge step that has no browser. It leads
// with the wait's one line and keeps I'm ready, which re-checks CI.
// MUTATION: drop `ci` from `handoffOf`'s parked case; these fail.
describe('the hand-off banner of a CI wait handed to a person', () => {
  it('leads with the one line, keeps I\'m ready, and says nothing of a browser', async () => {
    const { HandoffBanner, ciWaitStuckLine, needsYou } = await import('../Handoff')
    const t = stuck()
    expect(ciWaitStuckLine(t)).toBe(LINE)
    expect(needsYou(t)).toBe(true)
    const { container } = render(<HandoffBanner task={t} now={Date.now()} />)
    const text = container.textContent ?? ''
    expect(text).toContain(LINE)
    expect(text).toContain("I'm ready")
    expect(text).not.toMatch(/browser|gave no reason|expected site/)
  })

  it('is null for a hand-off that is not a CI wait', async () => {
    const { ciWaitStuckLine } = await import('../Handoff')
    expect(ciWaitStuckLine(task({ state: 'PARKED', park_reason: 'HUMAN_REQUIRED' } as Partial<Task>))).toBeNull()
  })
})
