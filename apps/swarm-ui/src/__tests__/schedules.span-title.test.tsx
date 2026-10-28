// A SCHEDULE-MADE TASK IS NAMED BY ITS TITLE (VQ-028, BGD 2026-10-26 "Serve
// the task's metadata.title"; lane SPANS-TITLE, part of #2391).
//
// WHAT EACH CASE HOLDS:
//   * `/spans` serves a task's `metadata.title` as `title`; the History names
//     a task item by it, in its lane and in the list under the lanes, its
//     runner profile still said beside it;
//   * an untitled task (`title: null`) keeps today's name, what the firing's
//     type made ("issue-sweep task");
//   * a workflow's steps are unchanged: a step is named by its step, and the
//     workflow item by its type, even when the step's task carries a title.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s, `firing_to_api`'s
// and `list_firing_spans`'s shapes. Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'

const WAIT = { timeout: 4000 }
const TITLE = 'Docs drift: README install steps'

type Json = Record<string, unknown>

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()

function schedule(): Json {
  return {
    schedule_id: 'sch_t', tenant_id: 'eng', name: 'Schedule sch_t', type: 'issue-sweep',
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

function firing(n: number, work: Json[]): Json {
  const at = minutesAgo(n * 1440)
  const ended = new Date(Date.parse(at) + 60 * 60_000).toISOString()
  return {
    firing_id: `sch_t:${n}`, schedule_id: 'sch_t', type: 'issue-sweep', slot: at, fired_at: at, trigger: 'cron',
    state: 'done', work, skip: null, dry_run: null, approval_id: null, outcome: 'succeeded', cost: null, ended_at: ended,
  }
}

/** A task fired `daysAgo` days ago with one five-minute attempt. */
function spanTask(id: string, daysAgo: number, over: Json = {}): Json {
  const born = daysAgo * 1440
  return {
    task_id: id, title: null, step_id: null, label: 'claude-code', state: 'SUCCEEDED',
    created_at: minutesAgo(born), updated_at: minutesAgo(born - 60), completed_at: minutesAgo(born - 60),
    current_generation: 1, park_reason: null, next_eligible_at: null,
    attempts: [{ attempt_id: `att_${id}_1`, task_id: id, generation: 1, created_at: minutesAgo(born - 10), completed_at: minutesAgo(born - 15), exit_code: 0 }],
    attempts_truncated: false, attempts_unread: null,
    ...over,
  }
}

const FIRINGS = [
  firing(1, [{ kind: 'task', id: 'task_titled', repo_id: 'repo_a' }]),
  firing(2, [{ kind: 'task', id: 'task_plain', repo_id: 'repo_a' }]),
  firing(3, [{ kind: 'workflow', id: 'wf_1', repo_id: 'repo_a' }]),
]

const ITEMS: Record<string, Json> = {
  'sch_t:1': { kind: 'task', id: 'task_titled', state: null, updated_at: null, unread: null, tasks: [spanTask('task_titled', 1, { title: TITLE })] },
  'sch_t:2': { kind: 'task', id: 'task_plain', state: null, updated_at: null, unread: null, tasks: [spanTask('task_plain', 2)] },
  'sch_t:3': {
    kind: 'workflow', id: 'wf_1', state: 'SUCCEEDED', updated_at: null, unread: null,
    tasks: [spanTask('task_step', 3, { step_id: 'review', label: 'review', title: 'A step title the timeline does not use' })],
  },
}

async function mount() {
  serve((m, url) => {
    if (m !== 'GET') return null
    const bare = url.split('?')[0]
    if (bare === '/v1/schedules/sch_t') return { status: 200, body: { schedule: schedule(), firings: FIRINGS } }
    if (bare === '/v1/schedules/sch_t/spans') {
      const asked = new URL(url, 'http://x').searchParams.getAll('firing_id')
      return { status: 200, body: { firings: asked.map((id) => ({ firing_id: id, items: [ITEMS[id]] })), not_found: [] } }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=sch_t" spy={vi.fn()} />)
  await screen.findByRole('heading', { name: 'Schedule sch_t', level: 1 }, WAIT)
  // Frame A2's sheet draws the firings' table first; the names are the Timeline's.
  fireEvent.click(await screen.findByRole('button', { name: 'Timeline' }, WAIT))
}

function found(selector: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(selector)
  expect(el, selector).not.toBeNull()
  return el!
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a schedule-made task is named by its title (VQ-028)', () => {
  it('names a titled task by its title, in its lane and in the list, its profile beside it', async () => {
    await mount()
    const lane = await waitFor(() => found('[data-item="task:task_titled:task_titled"]'), WAIT)
    // MUTATION: drop `t.title` from `itemName` and the lane reads "issue-sweep task".
    expect(visible(lane.querySelector('.tl-lab b'))).toBe(TITLE)
    expect(visible(lane.querySelector('.tl-lab small'))).toBe('claude-code · succeeded · 1 attempt')
    const li = await waitFor(() => found('[data-testid="sc-history-items"] [data-work="task:task_titled"]'), WAIT)
    expect(visible(li.querySelector('.sht-words b'))).toBe(TITLE)
    expect(visible(li.querySelector('.sht-words'))).toContain(`${TITLE} on claude-code → task task_titled · succeeded`)
  })

  it('names an untitled task by what the firing’s type made, as before', async () => {
    await mount()
    const lane = await waitFor(() => found('[data-item="task:task_plain:task_plain"]'), WAIT)
    expect(visible(lane.querySelector('.tl-lab b'))).toBe('issue-sweep task')
    const li = await waitFor(() => found('[data-testid="sc-history-items"] [data-work="task:task_plain"]'), WAIT)
    expect(visible(li.querySelector('.sht-words b'))).toBe('issue-sweep task')
  })

  it('leaves a workflow’s steps named by their step, even when the step’s task has a title', async () => {
    await mount()
    const lane = await waitFor(() => found('[data-item="workflow:wf_1:task_step"]'), WAIT)
    expect(visible(lane.querySelector('.tl-lab b'))).toBe('review')
    const li = await waitFor(() => found('[data-testid="sc-history-items"] [data-work="workflow:wf_1"]'), WAIT)
    expect(visible(li.querySelector('.sht-words b'))).toBe('issue-sweep workflow')
  })
})
