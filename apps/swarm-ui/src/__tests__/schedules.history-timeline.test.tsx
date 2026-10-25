// ONE SCHEDULE'S HISTORY AS A TIMELINE (#1109, lane SCHED-HISTORY-TIMELINE;
// schedules.html detail A's History pane, BGD 2026-10-25 "build it now").
//
// WHAT EACH CASE HOLDS:
//   * a schedule with 5 firings draws 5 lanes, and each lane's bars are its
//     work items' attempts as `/spans` served them -- one bar an attempt;
//   * the bars are the Timeline page's: a parked task is hatched (`is-park`),
//     a running one solid (`is-hold`, open to now), a fenced generation cut;
//   * Timeline is the default, the toggle switches to the table (S7's rows,
//     unchanged), and the choice is remembered on the device -- and a storage
//     that throws still opens the page, on the timeline;
//   * a firing whose attempts could not be read draws a dash with its reason,
//     never an empty lane; a failed read dashes every lane with the failure;
//   * one read per page of firings: the page's ids, at most SPAN_PAGE, and
//     Older reads the next page's.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s, `firing_to_api`'s
// and `list_firing_spans`'s shapes. Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'

const WAIT = { timeout: 4000 }
const KEY = 'swarm.schedule.history.view'

type Json = Record<string, unknown>

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()

function schedule(): Json {
  return {
    schedule_id: 'sch_a', tenant_id: 'eng', name: 'Schedule sch_a', type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] }, cron: '0 9 * * 1-5', timezone: 'Europe/London',
    params: {}, gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled', pause: null, owner: 'operator@example.com', created_by: 'operator@example.com',
    created_at: minutesAgo(10_000), next_run_at: minutesAgo(-60), last_firing: null, consecutive_failures: 0,
    revision: 2, words: 'at 09:00, Monday through Friday', tier: 'R2',
    spend_today: { day: '2026-10-25', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' }, pending_approvals: 0,
  }
}

/** Firing n, fired n days ago, which made task `task_<n>`. */
function firing(n: number, over: Json = {}): Json {
  const at = minutesAgo(n * 1440)
  return {
    firing_id: `sch_a:${n}`, schedule_id: 'sch_a', type: 'issue-sweep', slot: at, fired_at: at, trigger: 'cron',
    state: 'done', work: [{ kind: 'task', id: `task_${n}`, repo_id: 'repo_a' }], skip: null, dry_run: null,
    approval_id: null, outcome: 'succeeded', cost: null, ended_at: minutesAgo(n * 1440 - 60),
    ...over,
  }
}

/** A task fired `daysAgo` days ago with `count` attempts of 5 minutes each, ten apart. */
function spanTask(id: string, daysAgo: number, count: number, over: Json = {}): Json {
  const born = daysAgo * 1440
  return {
    task_id: id, step_id: null, label: 'mock', state: 'SUCCEEDED',
    created_at: minutesAgo(born), updated_at: minutesAgo(born - 60), completed_at: minutesAgo(born - 60),
    current_generation: count, park_reason: null, next_eligible_at: null,
    attempts: Array.from({ length: count }, (_, i) => ({
      attempt_id: `att_${id}_${i + 1}`, task_id: id, generation: i + 1,
      created_at: minutesAgo(born - 10 * (i + 1)), completed_at: minutesAgo(born - 10 * (i + 1) - 5), exit_code: 0,
    })),
    attempts_truncated: false, attempts_unread: null,
    ...over,
  }
}

function item(id: string, tasks: Json[], over: Json = {}): Json {
  return { kind: 'task', id, state: null, updated_at: null, tasks, unread: null, ...over }
}

type Handler = (url: string) => { status: number; body: unknown } | null

async function mount(firings: Json[], spans: Handler) {
  const calls = serve((m, url) => {
    if (m !== 'GET') return null
    const bare = url.split('?')[0]
    if (bare === '/v1/schedules/sch_a') return { status: 200, body: { schedule: schedule(), firings } }
    if (bare === '/v1/schedules/sch_a/spans') return spans(url)
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=sch_a" spy={go} />)
  await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

/** The firing ids one `/spans` call asked for. */
const askedOf = (url: string) => new URL(url, 'http://x').searchParams.getAll('firing_id')

/** `/spans` answering every asked firing with its one task, n attempts for firing n. */
const fiveSpans: Handler = (url) => ({
  status: 200,
  body: {
    firings: askedOf(url).map((id) => {
      const n = Number(id.split(':')[1])
      return { firing_id: id, items: [item(`task_${n}`, [spanTask(`task_${n}`, n, n)])] }
    }),
    not_found: [],
  },
})

const lanes = () => [...document.querySelectorAll<HTMLElement>('[data-firing-lane]')]

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.localStorage.removeItem(KEY)
})

describe("a schedule's History timeline", () => {
  it('draws 5 lanes for 5 firings, with a bar for each of their work items’ attempts', async () => {
    const { calls } = await mount([1, 2, 3, 4, 5].map((n) => firing(n)), fiveSpans)
    await waitFor(() => expect(document.querySelectorAll('[data-firing-lane] [data-seg="hold"]').length).toBe(15), WAIT)
    expect(lanes().map((l) => l.dataset.firingLane)).toEqual(['sch_a:1', 'sch_a:2', 'sch_a:3', 'sch_a:4', 'sch_a:5'])
    // Firing n made one task with n attempts: n bars, in its own lane.
    expect(lanes().map((l) => l.querySelectorAll('[data-seg="hold"]').length)).toEqual([1, 2, 3, 4, 5])
    // One read for the page, naming exactly its firings.
    const reads = calls.filter((c) => c.url.startsWith('/v1/schedules/sch_a/spans'))
    expect(reads).toHaveLength(1)
    expect(askedOf(reads[0]!.url)).toEqual(['sch_a:1', 'sch_a:2', 'sch_a:3', 'sch_a:4', 'sch_a:5'])
    // The table is behind the toggle, not drawn as well.
    expect(document.querySelector('tr[data-firing]')).toBeNull()
  })

  it('hatches a parked step, draws a running one solid, and a fenced generation cut', async () => {
    const parked = spanTask('task_p', 1, 1, { state: 'PARKED', completed_at: null, updated_at: minutesAgo(1440 - 20), park_reason: 'QUOTA_EXHAUSTED' })
    const running = spanTask('task_r', 1, 1, { state: 'RUNNING', completed_at: null, updated_at: minutesAgo(1) })
    ;(running.attempts as Json[])[0]!.completed_at = null
    const fenced = spanTask('task_f', 1, 2)
    ;(fenced.attempts as Json[])[0]!.exit_code = 70
    await mount([firing(1, { work: [{ kind: 'workflow', id: 'wf_1' }] })], (url) => ({
      status: 200,
      body: {
        firings: askedOf(url).map((id) => ({
          firing_id: id,
          items: [item('wf_1', [{ ...parked, label: 'plan' }, { ...running, label: 'build' }, { ...fenced, label: 'review' }], { kind: 'workflow', state: 'RUNNING' })],
        })),
        not_found: [],
      },
    }))
    const row = (task: string) => document.querySelector<HTMLElement>(`[data-item="workflow:wf_1:${task}"]`)
    await waitFor(() => expect(row('task_p')).not.toBeNull(), WAIT)
    const park = row('task_p')!.querySelector<HTMLElement>('[data-seg="park"]')
    expect(park, 'the parked step draws no hatch').not.toBeNull()
    expect(park!.classList.contains('is-park')).toBe(true)
    const hold = row('task_r')!.querySelector<HTMLElement>('[data-seg="hold"]')!
    expect(hold.classList.contains('is-open'), 'the running attempt is not drawn to now').toBe(true)
    expect(row('task_r')!.querySelector('[data-seg="park"]')).toBeNull()
    expect(row('task_f')!.querySelectorAll('[data-seg="cut"]')).toHaveLength(1)
    expect(row('task_f')!.querySelectorAll('[data-seg="hold"]')).toHaveLength(1)
    // The row names the step and links its task.
    expect(visible(row('task_r')!)).toContain('build')
  })

  it('keeps the scale to what ended, so a step parked for days does not crush every other bar', async () => {
    // Rendered review 2026-10-25: one 4-day park drew the axis to +5d.
    const parked = spanTask('task_p', 4, 1, { state: 'PARKED', completed_at: null, updated_at: minutesAgo(4 * 1440 - 20), park_reason: 'QUOTA_EXHAUSTED' })
    await mount([firing(4, { work: [{ kind: 'task', id: 'task_p' }] })], (url) => ({
      status: 200,
      body: { firings: askedOf(url).map((id) => ({ firing_id: id, items: [item('task_p', [parked])] })), not_found: [] },
    }))
    await waitFor(() => expect(document.querySelector('[data-seg="park"]')).not.toBeNull(), WAIT)
    const ticks = [...document.querySelectorAll<HTMLElement>('.sht-axis .tl-tk')].map((t) => visible(t))
    // The firing ended an hour after it fired: that is the page's scale.
    expect(ticks[ticks.length - 1]).toBe('+1h')
  })

  it('hatches an issue run waiting for its plan’s approval, which holds nothing', async () => {
    const planner = spanTask('task_plan', 1, 1, { label: 'plan' })
    await mount([firing(1, { work: [{ kind: 'issue_run', id: 'run_1' }] })], (url) => ({
      status: 200,
      body: {
        firings: askedOf(url).map((id) => ({
          firing_id: id,
          items: [item('run_1', [planner], { kind: 'issue_run', state: 'PLANNED', updated_at: minutesAgo(1440 - 30) })],
        })),
        not_found: [],
      },
    }))
    const wait = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-item="issue_run:run_1:waiting"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(wait.querySelector('[data-seg="park"]')).not.toBeNull()
    expect(visible(wait)).toContain('plan waiting for approval')
  })

  it('switches to the table, and remembers the choice on this device', async () => {
    const first = await mount([firing(1), firing(2)], fiveSpans)
    await waitFor(() => expect(lanes()).toHaveLength(2), WAIT)
    expect(screen.getByRole('button', { name: 'Timeline' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Table' }))
    await waitFor(() => expect(document.querySelectorAll('tr[data-firing]')).toHaveLength(2), WAIT)
    expect(lanes()).toHaveLength(0)
    expect(window.localStorage.getItem(KEY)).toBe('table')
    first.unmount()
    await mount([firing(1), firing(2)], fiveSpans)
    await waitFor(() => expect(document.querySelectorAll('tr[data-firing]')).toHaveLength(2), WAIT)
    expect(screen.getByRole('button', { name: 'Table' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('opens on the timeline when storage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage is off')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage is off')
    })
    await mount([firing(1)], fiveSpans)
    await waitFor(() => expect(lanes()).toHaveLength(1), WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Table' }))
    await waitFor(() => expect(document.querySelectorAll('tr[data-firing]')).toHaveLength(1), WAIT)
  })

  it('draws a dash with its reason for a firing whose attempts could not be read, never an empty lane', async () => {
    await mount([firing(2), firing(1)], (url) => ({
      status: 200,
      body: {
        firings: askedOf(url).map((id) =>
          id === 'sch_a:2'
            ? { firing_id: id, items: [item('task_2', [spanTask('task_2', 2, 0, { attempts: null, attempts_unread: 'its attempts could not be read' })])] }
            : { firing_id: id, items: [item('task_1', [spanTask('task_1', 1, 1)])] },
        ),
        not_found: [],
      },
    }))
    await waitFor(() => expect(document.querySelectorAll('[data-firing-lane] [data-seg="hold"]')).toHaveLength(1), WAIT)
    const [unread, read] = lanes()
    expect(unread!.querySelector('[data-seg]')).toBeNull()
    const gap = unread!.querySelector<HTMLElement>('[data-unread]')!
    expect(visible(gap)).toBe('— its attempts could not be read')
    expect(read!.querySelector('[data-unread]')).toBeNull()
  })

  it('dashes every lane of the page with the failure when the read fails', async () => {
    await mount([firing(2), firing(1)], () => ({ status: 503, body: { code: 'unavailable', message: 'the store did not answer' } }))
    await waitFor(() => expect(document.querySelectorAll('[data-firing-lane] [data-unread]')).toHaveLength(2), WAIT)
    for (const lane of lanes()) {
      expect(visible(lane.querySelector('[data-unread]'))).toMatch(/^— its work was not read/)
      expect(lane.querySelector('[data-seg]')).toBeNull()
    }
    expect(screen.getByRole('button', { name: 'Read the bars again' })).toBeTruthy()
  })

  it('says what a firing that made no work came to, as the table does, and reads nothing for it', async () => {
    const { calls } = await mount(
      [firing(1, { work: [], outcome: null, skip: { code: 'NOTHING_STARTED', detail: { reason: 'no_eligible_issue' } } })],
      fiveSpans,
    )
    await waitFor(() => expect(lanes()).toHaveLength(1), WAIT)
    const lane = lanes()[0]!
    expect(visible(lane)).toContain('nothing started: every open issue was passed over')
    expect(visible(lane)).toContain('this firing made no work')
    expect(lane.querySelector('[data-unread]')).toBeNull()
    expect(calls.some((c) => c.url.startsWith('/v1/schedules/sch_a/spans'))).toBe(false)
  })

  it('reads one page of firings at a time, and Older reads the next page’s', async () => {
    const firings = Array.from({ length: 12 }, (_, i) => firing(i + 1))
    const { calls } = await mount(firings, fiveSpans)
    await waitFor(() => expect(lanes()).toHaveLength(10), WAIT)
    const reads = () => calls.filter((c) => c.url.startsWith('/v1/schedules/sch_a/spans')).map((c) => askedOf(c.url))
    await waitFor(() => expect(reads()).toHaveLength(1), WAIT)
    expect(reads()[0]).toHaveLength(10)
    fireEvent.click(screen.getByRole('button', { name: /Older/ }))
    await waitFor(() => expect(lanes().map((l) => l.dataset.firingLane)).toEqual(['sch_a:11', 'sch_a:12']), WAIT)
    await waitFor(() => expect(reads()).toHaveLength(2), WAIT)
    expect(reads()[1]).toEqual(['sch_a:11', 'sch_a:12'])
  })
})
