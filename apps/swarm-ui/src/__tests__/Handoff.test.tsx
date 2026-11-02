// LANE LB-L12 UI-HANDOFF (docs/design/live-browser.md §6, "The hand-off"; part
// of #1030). The row's acceptance test, as behaviour:
//
//   * the tab banner and the Overview card show the reason, the expected site
//     and the time left;
//   * "I'm ready" calls `POST /v1/tasks/{id}/human-ready`;
//   * the Live filter lists only RUNNING browser agents, needs-you first;
//   * recordings list only the caller's tenant's.
//
// The plan names `apps/swarm-ui/test/Handoff.test.tsx`; vitest.config.ts
// includes only `src/**/*.test.tsx`, so a file there would never run. It is
// here, beside every other component test, for that reason.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTasks: vi.fn(), loadTasksInStates: vi.fn(), loadTaskEventsPage: vi.fn() }))

// The real api module, with the three reads stubbed and the fixture switch off,
// so a write in this file goes to `fetch` -- which each test stubs -- rather
// than to the development fixtures.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

import { AgentsScreen } from '../Agents'
import { HandoffBanner, handoffOf, liveBrowserRows } from '../Handoff'
import { HandoffCards } from '../Overview'
import { RecordingsPanel } from '../Recordings'

const NOW = Date.parse('2026-10-11T12:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const BROWSER = 'claude-code-browser'

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-ada',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 60_000),
    started_at: iso(NOW - 3_000_000),
    completed_at: state === 'SUCCEEDED' ? iso(NOW - 60_000) : null,
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

/** A RUNNING browser task holding for a person, with `left` ms of hold to go. */
function holding(id: string, left: number, over: Partial<Task> = {}): Task {
  return task(id, 'RUNNING', {
    runner_profile: BROWSER,
    human_wait: {
      reason: 'Sign in to GitHub as the bot account',
      expect: 'github.com',
      requested_at: iso(NOW - 120_000),
      deadline: iso(NOW + left),
      controller: null,
    },
    ...over,
  })
}

/** A browser task whose hold expired: PARKED(HUMAN_REQUIRED), no capacity. */
function parked(id: string): Task {
  return task(id, 'PARKED', {
    runner_profile: BROWSER,
    park_reason: 'HUMAN_REQUIRED',
    next_eligible_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 3_600_000),
  })
}

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function eventsWithRequest(): void {
  api.loadTaskEventsPage.mockResolvedValue({
    status: 'ok',
    fetchedAt: NOW,
    data: {
      next_page_token: null,
      events: [
        {
          event_id: 'ev_2',
          task_id: 'task_parked0000000000001',
          type: 'parked',
          at: iso(NOW - 3_600_000),
          attempt_id: 'att_1',
          lease_id: null,
          generation: 1,
          detail: {},
        },
        {
          event_id: 'ev_1',
          task_id: 'task_parked0000000000001',
          type: 'human_handoff_requested',
          at: iso(NOW - 4_200_000),
          attempt_id: 'att_1',
          lease_id: null,
          generation: 1,
          detail: { reason: 'Approve the SSO prompt', expect: 'login.example.com' },
        },
      ],
    },
  })
}

// ---------------------------------------------------------------------------
// The banner: reason, expected site, time left
// ---------------------------------------------------------------------------

describe('the hand-off banner', () => {
  it('shows the reason, the expected site and the time left during the hold', () => {
    const t = holding('task_hold00000000000000001', 4 * 60_000 + 12_000)
    render(<HandoffBanner task={t} now={NOW} />)
    const banner = screen.getByRole('status')
    expect(banner.textContent).toContain('Sign in to GitHub as the bot account')
    expect(banner.textContent).toContain('github.com')
    expect(banner.textContent).toContain('4:12 left')
    // A held task is not parked: there is nothing to be ready for yet.
    expect(within(banner).queryByRole('button', { name: /I'm ready/i })).toBeNull()
  })

  it('says the hold is over once its deadline has passed, never a negative time', () => {
    const t = holding('task_hold00000000000000002', -5_000)
    render(<HandoffBanner task={t} now={NOW} />)
    const text = screen.getByRole('status').textContent ?? ''
    expect(text).not.toMatch(/-\d/)
    expect(text).toMatch(/parking/i)
  })

  it('draws nothing for a task nobody is needed for', () => {
    const { container } = render(<HandoffBanner task={task('task_plain0000000000000001', 'RUNNING')} now={NOW} />)
    expect(container.textContent).toBe('')
    expect(handoffOf(task('task_plain0000000000000001', 'RUNNING'))).toBeNull()
  })

  it('on a parked hand-off reads the reason from its request event and offers "I\'m ready to sign in"', async () => {
    eventsWithRequest()
    const t = parked('task_parked0000000000001')
    render(<HandoffBanner task={t} now={NOW} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Approve the SSO prompt'))
    const text = screen.getByRole('status').textContent ?? ''
    expect(text).toContain('login.example.com')
    // 24 hours from the park, one hour ago: 23 hours to go.
    expect(text).toMatch(/23:00:00 left/)
    expect(screen.getByRole('button', { name: /I'm ready/ })).toBeTruthy()
  })

  it('"I\'m ready to sign in" POSTs human-ready for that task and reports the task back', async () => {
    eventsWithRequest()
    const t = parked('task_parked0000000000001')
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okJson({ task: { ...t, state: 'READY', park_reason: null } }))
    vi.stubGlobal('fetch', fetchMock)
    const onReady = vi.fn()
    render(<HandoffBanner task={t} now={NOW} onReady={onReady} />)
    fireEvent.click(screen.getByRole('button', { name: /I'm ready/ }))
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1))
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(writes).toHaveLength(1)
    const [url, init] = writes[0]!
    expect(url).toBe('/v1/tasks/task_parked0000000000001/human-ready')
    // The route takes no body: a person's readiness, nothing a caller shapes.
    expect(init?.body).toBeUndefined()
    expect(screen.getByRole('status').textContent).toMatch(/starting your browser/i)
  })

  it('a refused human-ready says so and does not claim the browser is starting', async () => {
    eventsWithRequest()
    const t = parked('task_parked0000000000001')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => okJson({ code: 'forbidden', message: 'only the submitter may resume this hand-off' }, 403)),
    )
    const onReady = vi.fn()
    render(<HandoffBanner task={t} now={NOW} onReady={onReady} />)
    fireEvent.click(screen.getByRole('button', { name: /I'm ready/ }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/only the submitter/))
    expect(onReady).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).not.toMatch(/starting your browser/i)
  })
})

// ---------------------------------------------------------------------------
// The Overview card
// ---------------------------------------------------------------------------

describe("the Overview's hand-off cards", () => {
  it('show the reason, the expected site and the time left, one card per waiting task', () => {
    const rows = [
      holding('task_hold00000000000000001', 9 * 60_000),
      task('task_plain0000000000000001', 'RUNNING'),
    ]
    const { container } = render(<HandoffCards tasks={rows} now={NOW} onOpen={() => {}} />)
    const cards = container.querySelectorAll('.c-banner')
    expect(cards).toHaveLength(1)
    const text = cards[0]!.textContent ?? ''
    expect(text).toContain('Sign in to GitHub as the bot account')
    expect(text).toContain('github.com')
    expect(text).toContain('9:00 left')
  })

  it('draws nothing when no task is waiting for a person', () => {
    const { container } = render(
      <HandoffCards tasks={[task('task_plain0000000000000001', 'RUNNING')]} now={NOW} onOpen={() => {}} />,
    )
    expect(container.textContent).toBe('')
  })

  it("a parked card's \"I'm ready\" calls human-ready and opens the agent", async () => {
    eventsWithRequest()
    const t = parked('task_parked0000000000001')
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okJson({ task: { ...t, state: 'READY' } }))
    vi.stubGlobal('fetch', fetchMock)
    const onOpen = vi.fn()
    render(<HandoffCards tasks={[t]} now={NOW} onOpen={onOpen} />)
    fireEvent.click(screen.getByRole('button', { name: /I'm ready/ }))
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith('task_parked0000000000001'))
    expect(fetchMock.mock.calls.some(([u]) => u === '/v1/tasks/task_parked0000000000001/human-ready')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// The Live filter
// ---------------------------------------------------------------------------

describe('the Live browsers filter (the All | Browsers N switch)', () => {
  const rows = (): Task[] => [
    // Newest first, as the list reads them; the needs-you row is the OLDEST,
    // so only the needs-you rule can put it first.
    task('task_browser0000000000001', 'RUNNING', { runner_profile: BROWSER, created_at: iso(NOW - 60_000) }),
    task('task_coder000000000000001', 'RUNNING', { created_at: iso(NOW - 120_000) }),
    task('task_starting00000000001', 'STARTING', { runner_profile: BROWSER, created_at: iso(NOW - 180_000) }),
    holding('task_needsyou00000000001', 5 * 60_000, { created_at: iso(NOW - 900_000) }),
    parked('task_parked0000000000001'),
    task('task_done00000000000000001', 'SUCCEEDED', { runner_profile: BROWSER }),
  ]

  it('liveBrowserRows keeps only RUNNING browser agents, needs-you first', () => {
    expect(liveBrowserRows(rows()).map((t) => t.id)).toEqual(['task_needsyou00000000001', 'task_browser0000000000001'])
  })

  // The list's switch adds the STARTING browser agents after these, marked
  // (`browserRows`, live-browser QA p12 QA12-20); `liveBrowserRows` stays the
  // running ones, which is what the theater walks.
  it('on the Agents list, lists the browser agents with the needs-you row first and yellow, starting ones last', async () => {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: { tasks: rows(), tenant_id: 'u-ada' },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    // The live and waiting states' one read answers from the same rows.
    api.loadTasksInStates.mockImplementation(async (states: readonly TaskState[]) => ({
      status: 'ok',
      data: { tasks: rows().filter((t) => states.includes(t.state)), tenant_id: 'u-ada' },
      fetchedAt: Date.now(),
    }) satisfies Result<TaskPage>)
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
    // The "All | Browsers N" switch (LB-UX3; it was a "Live browsers"
    // checkbox, then "All | Live N" until QA12-20).
    fireEvent.click(within(screen.getByRole('group', { name: /browser agents/i })).getByRole('button', { name: /^Browsers/ }))
    await waitFor(() =>
      expect([...container.querySelectorAll('.rows .row.clickable')].map((r) => r.getAttribute('data-task-id'))).toEqual([
        'task_needsyou00000000001',
        'task_browser0000000000001',
        'task_starting00000000001',
      ]),
    )
    const first = container.querySelector('.rows .row.clickable')!
    expect(first.classList.contains('is-needs-you')).toBe(true)
    expect(first.textContent).toMatch(/needs you/i)
  })
})

// ---------------------------------------------------------------------------
// Recordings
// ---------------------------------------------------------------------------

describe('recordings', () => {
  it("list only the caller's tenant's segments of this task, under live/ with the recording suffix", async () => {
    const t = task('task_browser0000000000001', 'RUNNING', { runner_profile: BROWSER })
    const mine = (a: string, seq: string) => `tenants/u-ada/tasks/${t.id}/attempts/${a}/live/rec-${seq}.live.webm`
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      okJson({
        recordings: [
          { attempt_id: 'att_1', name: 'rec-00001.live.webm', key: mine('att_1', '00001'), bytes: 2048, created_at: iso(NOW - 600_000) },
          { attempt_id: 'att_1', name: 'rec-00002.live.webm', key: mine('att_1', '00002'), bytes: 4096, created_at: iso(NOW - 540_000) },
          // Another tenant's segment, served by a misbehaving route.
          {
            attempt_id: 'att_9',
            name: 'rec-00001.live.webm',
            key: `tenants/eng/tasks/${t.id}/attempts/att_9/live/rec-00001.live.webm`,
            bytes: 999,
            created_at: iso(NOW - 600_000),
          },
          // This tenant, but not a recording.
          {
            attempt_id: 'att_1',
            name: 'stdout.log',
            key: `tenants/u-ada/tasks/${t.id}/attempts/att_1/logs/stdout.log`,
            bytes: 10,
            created_at: iso(NOW - 600_000),
          },
        ],
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { container } = render(<RecordingsPanel task={t} />)
    await waitFor(() => expect(container.querySelectorAll('[data-recording]')).toHaveLength(2))
    const keys = [...container.querySelectorAll('[data-recording]')].map((r) => r.getAttribute('data-recording'))
    expect(keys).toEqual([mine('att_1', '00001'), mine('att_1', '00002')])
    expect(container.textContent).not.toContain('tenants/eng')
    expect(container.textContent).not.toContain('stdout.log')
    // The read is the task's own route, which checks the tenant (§7).
    expect(String(fetchMock.mock.calls[0]![0])).toMatch(/^\/v1\/tasks\/task_browser0000000000001\/live\/recordings/)
    // It says what is dropped rather than hiding it silently.
    expect(container.textContent).toMatch(/2 entries .*not this agent's recordings/i)
  })

  it('says plainly when the deployment does not serve recordings yet', async () => {
    const t = task('task_browser0000000000001', 'RUNNING', { runner_profile: BROWSER })
    vi.stubGlobal('fetch', vi.fn(async () => okJson({ code: 'not_found', message: 'Not Found' }, 404)))
    const { container } = render(<RecordingsPanel task={t} />)
    await waitFor(() => expect(container.textContent).toMatch(/not served/i))
    expect(container.querySelectorAll('[data-recording]')).toHaveLength(0)
  })
})
