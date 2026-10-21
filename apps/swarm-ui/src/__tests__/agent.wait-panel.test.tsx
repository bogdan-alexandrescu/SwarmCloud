// WAIT-UI-PANEL (part of #1782): THE AGENT PAGE SAYS WHY IT IS WAITING.
//
// The page for task_31c301ecb33940eeb278 (SIZES-SCHED-2's merge step,
// 2026-10-21) said "waiting for CI · eligible since 19:46:25" and "last
// scheduler pass CI_PENDING", while its park events held the pull request,
// the head commit, five pending checks by name, two wakes and a refunded
// attempt. WAIT-API serves those facts as `task.waiting`
// (swarm_api/waiting_detail.py, docs/waiting.md); this panel draws them, for
// every reason, and a fact the platform did not record reads "not recorded"
// -- never blank, never 0, never "none".
//
// MUTATIONS, each named on the assertion it breaks.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Task, Waiting, WaitingHistoryEntry } from '../types'
import { clockTime } from '../types'
import { at, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')
const { WaitPanel, fullWaiting, HISTORY_SHOWN } = await import('../WaitPanel')

/** The page's instant: two hours after `at(0)`. */
const NOW = Date.parse(at(120))

function waiting(over: Partial<Waiting>): Waiting {
  return {
    reason: 'CI_PENDING',
    summary: 'a summary',
    since: at(60),
    next_check_at: at(130),
    resumes_when: 'its pull request checks settle',
    detail: {},
    history: [],
    history_read: true,
    read_at: at(120),
    ...over,
  }
}

function parked(w: Waiting, over: Partial<Task> = {}): Task {
  return task({
    id: 'task_31c301ecb33940eeb278',
    state: w.reason === 'READY_CAPACITY' ? 'READY' : 'PARKED',
    park_reason: w.reason === 'READY_CAPACITY' ? null : (w.reason as Task['park_reason']),
    next_eligible_at: w.next_check_at,
    repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
    waiting: w,
    ...over,
  })
}

function draw(w: Waiting, over: Partial<Task> = {}): HTMLElement {
  const { container } = render(<WaitPanel task={parked(w, over)} now={NOW} />)
  const panel = container.querySelector<HTMLElement>('[data-wait-panel]')
  expect(panel, 'no wait panel').not.toBeNull()
  return panel!
}

/** The text a fact row shows, by its `data-fact` key. */
function fact(panel: HTMLElement, key: string): string {
  const row = panel.querySelector<HTMLElement>(`[data-fact="${key}"]`)
  expect(row, `no ${key} row`).not.toBeNull()
  return row!.querySelector('dd')?.textContent ?? ''
}

const CI_DETAIL = {
  pull_request: { number: 1779, url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/1779' },
  head: '1968b677a1c2d3e4f5061728394a5b6c7d8e9f00',
  code: 'CI_PENDING',
  pending: ['checkov', 'ci-gate', 'python (unit)', 'ui (typecheck + component)', 'manifests'],
  failed: ['shell'],
  wakes: 2,
  attempts_counted: false,
  last_checked_at: at(100),
  wake_requested_at: null,
}

describe('the CI case', () => {
  it('links the pull request and lists every pending and failed check by name', () => {
    const p = draw(waiting({ reason: 'CI_PENDING', summary: 'CI on PR #1779: 5 checks pending', detail: CI_DETAIL }))
    const pr = p.querySelector<HTMLAnchorElement>('a[data-wait-pr]')
    // MUTATION: render the PR number as text without the link -> pr is null.
    expect(pr).not.toBeNull()
    // MUTATION: build the href from the number instead of the served url -> wrong href.
    expect(pr!.getAttribute('href')).toBe(CI_DETAIL.pull_request.url)
    expect(pr!.textContent).toContain('#1779')
    const checks = [...p.querySelectorAll<HTMLElement>('[data-check]')].map((c) => [
      c.getAttribute('data-check'),
      c.getAttribute('data-check-state'),
    ])
    // MUTATION: slice the pending list to the first three (as the summary does) -> two names missing.
    expect(checks).toEqual([
      ['shell', 'failed'],
      ['checkov', 'pending'],
      ['ci-gate', 'pending'],
      ['python (unit)', 'pending'],
      ['ui (typecheck + component)', 'pending'],
      ['manifests', 'pending'],
    ])
    // MUTATION: drop the state word beside each name -> the text no longer says which ones failed.
    expect(p.querySelector('[data-check="shell"]')!.textContent).toContain('failed')
    // MUTATION: print the whole sha -> the short head is not what is shown.
    expect(fact(p, 'head')).toBe('1968b677')
    // MUTATION: print `wakes` as "re-checked" only when non-zero, or drop it -> missing.
    expect(fact(p, 'rechecks')).toBe('2')
    // MUTATION: read attempts_counted truthily (false -> counted) -> wrong words.
    expect(fact(p, 'attempts')).toBe('not counted against attempts')
    // MUTATION: label the check list with the read time (read_at) instead of the check's own time.
    expect(p.querySelector('[data-wait-as-of]')!.textContent).toBe(`as of ${clockTime(at(100), NOW)!.text}`)
  })

  it('says how long it has waited and when it is checked next, in the viewer’s time', () => {
    const p = draw(waiting({ summary: 'CI on PR #1779: 5 checks pending', detail: CI_DETAIL }))
    // MUTATION: drop the summary from the heading -> the sentence is missing.
    expect(p.querySelector('[data-wait-summary]')!.textContent).toBe('CI on PR #1779: 5 checks pending')
    // MUTATION: measure from read_at, or omit -> not "1h 0m".
    expect(p.querySelector('[data-wait-since]')!.textContent).toContain('waited 1h 0m')
    // MUTATION: format next_check_at in UTC or omit it -> not the viewer's clock text.
    expect(fact(p, 'next-check')).toBe(clockTime(at(130), NOW)!.text)
    // MUTATION: drop the resumes_when row.
    expect(fact(p, 'resumes')).toBe('its pull request checks settle')
  })

  it('reads counted attempts as counted, and a PR with no url as plain text', () => {
    const p = draw(
      waiting({
        detail: { ...CI_DETAIL, attempts_counted: true, pull_request: { number: 12, url: null } },
      }),
    )
    // MUTATION: always print "not counted" -> wrong for a counted attempt.
    expect(fact(p, 'attempts')).toBe('counted against attempts')
    // MUTATION: render an <a href="null"> for a url the API could not build.
    expect(p.querySelector('a[data-wait-pr]')).toBeNull()
    expect(fact(p, 'pr')).toBe('#12')
  })
})

describe('a null fact reads "not recorded"', () => {
  it('in the CI case, for every unrecorded fact', () => {
    const p = draw(
      waiting({
        since: null,
        next_check_at: null,
        detail: {
          pull_request: null,
          head: null,
          pending: null,
          failed: null,
          wakes: null,
          attempts_counted: null,
          last_checked_at: null,
        },
      }),
    )
    // MUTATION: render a null as '' / '0' / 'none' in any of these rows.
    for (const key of ['pr', 'head', 'checks', 'rechecks', 'attempts', 'next-check']) {
      expect(fact(p, key), key).toBe('not recorded')
    }
    // MUTATION: print "waited 0s" for an unrecorded start.
    expect(p.querySelector('[data-wait-since]')!.textContent).toContain('not recorded')
    // MUTATION: print "as of" with no time when the check time was not recorded.
    expect(p.querySelector('[data-wait-as-of]')!.textContent).toBe('as of: not recorded')
  })

  it('in a detail object that is missing a key altogether', () => {
    const p = draw(waiting({ reason: 'BUDGET_EXHAUSTED', summary: 'Budget exhausted', detail: {} }))
    // MUTATION: render an absent key as blank.
    expect(fact(p, 'budget')).toBe('not recorded')
  })

  it('a recorded zero stays a zero', () => {
    const p = draw(waiting({ detail: { ...CI_DETAIL, wakes: 0 } }))
    // MUTATION: treat 0 as absent (`wakes || null`) -> "not recorded".
    expect(fact(p, 'rechecks')).toBe('0')
  })
})

describe('each reason renders its own detail', () => {
  it('capacity: the binding pool, its units used and limit, and the place in line', () => {
    const p = draw(
      waiting({
        reason: 'READY_CAPACITY',
        summary: 'Ready; capacity binds on runner:merge (4 of 4 units in use)',
        next_check_at: null,
        detail: {
          pool: 'runner:merge',
          pool_state: 'full',
          active: 4,
          limit: 4,
          units: 1,
          place_in_line: 3,
          place_in_line_unavailable: null,
        },
      }),
    )
    // MUTATION: drop the pool row.
    expect(fact(p, 'pool')).toBe('runner:merge')
    // MUTATION: print limit/active the wrong way round or drop one.
    expect(fact(p, 'units')).toBe('4 of 4 units in use · needs 1')
    // MUTATION: drop the place in line.
    expect(fact(p, 'place')).toBe('3')
  })

  it('capacity: an unserved place in line says not recorded, with why', () => {
    const p = draw(
      waiting({
        reason: 'READY_CAPACITY',
        detail: { pool: 'runner:merge', active: null, limit: null, units: 1, place_in_line: null,
          place_in_line_unavailable: 'not computed: it would read every READY task' },
      }),
    )
    // MUTATION: render a null place as 0 or blank.
    expect(fact(p, 'place')).toBe('not recorded')
    // MUTATION: drop the served reason from the row's title.
    expect(p.querySelector('[data-fact="place"] dd [title]')!.getAttribute('title')).toContain('not computed')
    // MUTATION: print "null of null" for an unread pool.
    expect(fact(p, 'units')).toBe('not recorded · needs 1')
  })

  it.each(['PROVIDER_QUOTA_EXHAUSTED', 'PROVIDER_COOLDOWN', 'PROVIDER_OUTAGE'])(
    '%s: the provider, the account and until when',
    (reason) => {
      const p = draw(
        waiting({ reason, detail: { provider: 'anthropic', account: 'acct-team-2', until: at(150) } }),
      )
      // MUTATION: drop the provider or the account row, or format until in UTC.
      expect(fact(p, 'provider')).toBe('anthropic')
      expect(fact(p, 'account')).toBe('acct-team-2')
      expect(fact(p, 'until')).toBe(clockTime(at(150), NOW)!.text)
    },
  )

  it('retry: the last error, attempt n of N and the next try', () => {
    const p = draw(
      waiting({
        reason: 'SCHEDULED_RETRY',
        detail: {
          last_error: 'exit status 1: pytest collected 2 failures',
          attempt: 1,
          next_attempt: 2,
          max_attempts: 3,
          next_try_at: at(125),
          cause: 'worker_exit',
        },
      }),
    )
    // MUTATION: drop the last error.
    expect(fact(p, 'last-error')).toBe('exit status 1: pytest collected 2 failures')
    // MUTATION: print the used count (attempt) instead of the next one.
    expect(fact(p, 'attempt')).toBe('2 of 3')
    // MUTATION: drop the next try.
    expect(fact(p, 'next-try')).toBe(clockTime(at(125), NOW)!.text)
  })

  it('dependency: each upstream step as a link with its state', () => {
    const p = draw(
      waiting({
        reason: 'DEPENDENCY_INCOMPLETE',
        detail: {
          waiting_on: [
            { task_id: 'task_up1', step_id: 'implement', state: 'RUNNING' },
            { task_id: 'task_up2', step_id: null, state: null },
          ],
          states_read: true,
        },
      }),
    )
    const rows = [...p.querySelectorAll<HTMLElement>('[data-wait-on]')]
    // MUTATION: list only the first, or none.
    expect(rows).toHaveLength(2)
    // MUTATION: drop the link, or link the wrong task.
    expect(rows[0]!.querySelector('a')!.getAttribute('href')).toContain('task_up1')
    expect(rows[0]!.textContent).toContain('implement')
    expect(rows[0]!.textContent).toContain('RUNNING')
    // MUTATION: render an unread state blank.
    expect(rows[1]!.textContent).toContain('task_up2')
    expect(rows[1]!.textContent).toContain('not recorded')
  })

  it('children: each child as a link with its state', () => {
    const p = draw(
      waiting({
        reason: 'CHILDREN_INCOMPLETE',
        detail: {
          children: [
            { task_id: 'task_kid1', state: 'RUNNING' },
            { task_id: 'task_kid2', state: 'SUCCEEDED' },
          ],
          unfinished: 1,
          total: 2,
          states_read: true,
        },
      }),
    )
    const rows = [...p.querySelectorAll<HTMLElement>('[data-wait-on]')]
    // MUTATION: drop the children list.
    expect(rows.map((r) => r.querySelector('a')!.textContent)).toEqual(['task_kid1', 'task_kid2'])
    expect(rows[1]!.textContent).toContain('SUCCEEDED')
    // MUTATION: drop the tally.
    expect(fact(p, 'unfinished')).toBe('1 of 2')
  })

  it('person needed: who may answer, until when, and how', () => {
    const p = draw(
      waiting({
        reason: 'AWAITING_INPUT',
        next_check_at: null,
        detail: {
          who_may_answer: { tenant_id: 'u-bogdan', text: 'any member of tenant u-bogdan' },
          opened_at: at(60),
          expires_at: at(180),
          answer_command: '/sc answer wf_1 approve',
        },
      }),
    )
    // MUTATION: drop who may answer, the expiry, or the command.
    expect(fact(p, 'who')).toBe('any member of tenant u-bogdan')
    expect(fact(p, 'expires')).toBe(clockTime(at(180), NOW)!.text)
    expect(fact(p, 'answer')).toBe('/sc answer wf_1 approve')
  })

  it('human required: the answer route, and an unrecorded expiry', () => {
    const p = draw(
      waiting({
        reason: 'HUMAN_REQUIRED',
        next_check_at: null,
        detail: {
          who_may_answer: { tenant_id: 'u-bogdan', text: 'any member of tenant u-bogdan' },
          expires_at: null,
          answer_route: 'POST /v1/tasks/task_31c301ecb33940eeb278/human-ready',
          cause: null,
        },
      }),
    )
    // MUTATION: read only answer_command, so a HUMAN_REQUIRED park shows no way to answer.
    expect(fact(p, 'answer')).toBe('POST /v1/tasks/task_31c301ecb33940eeb278/human-ready')
    // MUTATION: render an unrecorded expiry blank.
    expect(fact(p, 'expires')).toBe('not recorded')
  })

  it('credential missing: the secret names', () => {
    const p = draw(
      waiting({
        reason: 'CREDENTIAL_MISSING',
        detail: { provider: 'anthropic', secret_names: ['anthropic key', 'github/token'] },
      }),
    )
    // MUTATION: drop the names, or print only the first.
    expect(fact(p, 'secrets')).toBe('anthropic key, github/token')
  })

  it('manual pause: who paused it and when', () => {
    const p = draw(
      waiting({
        reason: 'MANUAL_PAUSE',
        next_check_at: null,
        detail: { paused_by: 'alice@example.com', paused_at: at(90), error: null },
      }),
    )
    // MUTATION: drop who paused it, or when.
    expect(fact(p, 'paused-by')).toBe('alice@example.com')
    expect(fact(p, 'paused-at')).toBe(clockTime(at(90), NOW)!.text)
  })

  it('budget: which budget', () => {
    const p = draw(waiting({ reason: 'BUDGET_EXHAUSTED', detail: { budget: 'tenant monthly' } }))
    // MUTATION: drop the budget row.
    expect(fact(p, 'budget')).toBe('tenant monthly')
  })
})

describe('wait history', () => {
  const entries = (n: number): WaitingHistoryEntry[] =>
    Array.from({ length: n }, (_, i) => ({ at: at(60 + i), kind: 'parked', text: `entry ${i + 1}` }))

  it('collapses past 5, newest last, and "show all N" opens the rest', () => {
    const p = draw(waiting({ history: entries(8) }))
    const shown = () => [...p.querySelectorAll<HTMLElement>('[data-history] li')].map((li) => li.textContent ?? '')
    // MUTATION: show every entry regardless of length -> 8, not 5.
    expect(HISTORY_SHOWN).toBe(5)
    expect(shown()).toHaveLength(5)
    // MUTATION: keep the OLDEST five (slice(0, 5)) -> entry 1 first; the newest must stay, last.
    expect(shown()[0]).toContain('entry 4')
    expect(shown()[4]).toContain('entry 8')
    // MUTATION: drop the time beside the text.
    expect(shown()[4]).toContain(clockTime(at(67), NOW)!.text)
    const more = within(p).getByRole('button', { name: 'show all 8' })
    fireEvent.click(more)
    // MUTATION: a toggle that does nothing -> still 5.
    expect(shown()).toHaveLength(8)
    expect(shown()[0]).toContain('entry 1')
  })

  it('does not offer "show all" at 5 or fewer', () => {
    const p = draw(waiting({ history: entries(5) }))
    // MUTATION: collapse at `>= 5` -> a button that shows nothing more.
    expect(p.querySelectorAll('[data-history] li')).toHaveLength(5)
    expect(within(p).queryByRole('button', { name: /show all/ })).toBeNull()
  })

  it('says the history was not read, rather than drawing an empty list', () => {
    const p = draw(waiting({ history: [], history_read: false }))
    // MUTATION: ignore history_read -> an empty read reads as "nothing happened".
    expect(p.querySelector('[data-history]')!.textContent).toContain('not read')
  })
})

describe('on the agent page', () => {
  it('a task the list served (a row form) draws no panel', () => {
    // MUTATION: treat `{reason, summary}` as the full object -> a panel of "not recorded".
    expect(fullWaiting(task({ state: 'PARKED', waiting: { reason: 'CI_PENDING', summary: 's' } }))).toBeNull()
  })

  it('Run draws the panel in place of the bare park bar', async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const t = parked(waiting({ summary: 'CI on PR #1779: 5 checks pending', detail: CI_DETAIL }), {
      blocked_by: [{ reason: 'CI_PENDING' }],
    })
    const r: AgentRun = {
      task: t,
      events: [ev('submitted', at(-1), null)],
      eventsDetail: null,
      attempts: [],
      attemptsDetail: null,
      classes: null,
      classesDetail: null,
      classesRouteMissing: false,
    }
    const { container } = render(<Run run={r} />)
    await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
    // MUTATION: never mount WaitPanel in Run.
    expect(container.querySelector('[data-wait-panel]')).not.toBeNull()
    // MUTATION: keep the old "last scheduler pass" bar beside the panel -> said twice.
    expect(container.querySelector('[data-blocked-by]')).toBeNull()
    // MUTATION: keep the amber park bar ("waiting for CI · eligible ...") beside the panel.
    expect(container.querySelector('.dt-top > .bar.amber')).toBeNull()
  })
})
