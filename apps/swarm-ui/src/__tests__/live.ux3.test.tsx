// LANE LB-UX3 (part of #1030): the chosen design ("Browser Tab and Theater",
// docs/design/live-browser.md §6 and §6.1) on the Agents list, the Overview's
// hand-off card and the phone's Browser tab.
//
//   * the Agents list's Live tab carries an "All | Browsers N" switch (N =
//     browser agents, running and starting; it said "Live" until live-browser
//     QA p12, QA12-20), not a "Live browsers" checkbox; Browsers lists them
//     needs-you first, then running, then starting, each running one with a
//     device outline read from its session, and "⤢ Watch all browsers" (it
//     read "theater all" until visual QA pass 1, overview-agents-18) opens the
//     first row's Browser tab with ?theater=1;
//   * the Overview's "Needs a look" hand-off card offers Take control beside
//     "browse →" only when the session read says you may (`can_control`),
//     asks as the header's button asks, and then opens the Browser tab;
//   * at phone width the Browser tab's device switcher, rotate and touch
//     sit under a "⋯" menu, not in the toolbar (the logs toggle too, until
//     BROWSER-TAB-LIVE, #2423, kept it with the view's controls).
//
// MUTATIONS, one per block: keep the checkbox (the switch is not found); count
// every RUNNING row as N; drop the device read (every outline is "not
// reported"); point "Watch all browsers" at the agent page without ?theater=1; drop
// the `can_control` check on the card; navigate before the take resolves (or
// not at all); render LiveDevice outside LiveToolMenu.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTranscript: vi.fn(),
  loadLiveSession: vi.fn(),
  mintLiveTicket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
}))

// The fixture build is a flag the theater-all test turns on (overview-agents-17).
const build = vi.hoisted(() => ({ fixtures: false }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    ...api,
    get USE_FIXTURES() {
      return build.fixtures
    },
  }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    addEventListener() {}
    disconnect() {}
    clipboardPasteFrom() {}
  },
}))

import { AgentsScreen } from '../Agents'
import { LiveBrowserPane } from '../LiveBrowser'
import { browserTabPath, theaterPath } from '../LiveThumb'
import { HandoffCards } from '../Overview'

const NOW = Date.parse('2026-10-11T12:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const BROWSER = 'claude-code-browser'
const ME = ['ada', 'acme.test'].join('@')

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

function holding(id: string, over: Partial<Task> = {}): Task {
  return task(id, 'RUNNING', {
    runner_profile: BROWSER,
    human_wait: {
      reason: 'Sign in to GitHub as the bot account',
      expect: 'github.com',
      requested_at: iso(NOW - 120_000),
      deadline: iso(NOW + 9 * 60_000),
      controller: null,
    },
    ...over,
  })
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_ux3',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const read = (s: LiveSession | null): Result<LiveRead> => ok({ live_browser: true, session: s })

/** `matchMedia` for a width: jsdom has none. */
function atWidth(px: number): void {
  vi.stubGlobal('matchMedia', (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    const matches = (max === null || px <= Number(max[1])) && (min === null || px >= Number(min[1]))
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }
  })
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404, headers: { 'content-type': 'application/json' } })),
  )
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/')
})

// ---------------------------------------------------------------------------
// 1. The Agents list
// ---------------------------------------------------------------------------

describe('the Agents list: All | Browsers N', () => {
  const rows = (): Task[] => [
    task('task_browser0000000000001', 'RUNNING', { runner_profile: BROWSER, created_at: iso(NOW - 60_000) }),
    task('task_coder000000000000001', 'RUNNING', { created_at: iso(NOW - 120_000) }),
    task('task_tablet0000000000001', 'RUNNING', { runner_profile: BROWSER, created_at: iso(NOW - 150_000) }),
    task('task_starting00000000001', 'STARTING', { runner_profile: BROWSER, created_at: iso(NOW - 180_000) }),
    holding('task_needsyou00000000001', { created_at: iso(NOW - 900_000) }),
  ]

  async function mount() {
    api.loadTasks.mockResolvedValue({ status: 'ok', data: { tasks: rows(), tenant_id: 'u-ada' }, fetchedAt: Date.now() } satisfies Result<TaskPage>)
    // The live and waiting states' one read answers from the same rows.
    api.loadTasksInStates.mockImplementation(async (states: readonly TaskState[]) => ({
      status: 'ok',
      data: { tasks: rows().filter((t) => states.includes(t.state)), tenant_id: 'u-ada' },
      fetchedAt: Date.now(),
    }) satisfies Result<TaskPage>)
    api.loadLiveSession.mockImplementation(async (id: string) =>
      id === 'task_needsyou00000000001'
        ? read(session({ device: { preset: 'iphone-15', orientation: 'portrait', touch: true } }))
        : id === 'task_tablet0000000000001'
          ? read(session({ device: { preset: 'ipad-7', orientation: 'landscape', touch: true } }))
          : read(session({ device: null })),
    )
    const view = render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(view.container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
    return view
  }

  it('is a switch, not a checkbox, and N counts the browser agents, the starting one too', async () => {
    await mount()
    expect(screen.queryByRole('checkbox', { name: /live browsers/i })).toBeNull()
    const sw = screen.getByRole('group', { name: /browser agents/i })
    const all = within(sw).getByRole('button', { name: /^All/ })
    const browsers = within(sw).getByRole('button', { name: /^Browsers/ })
    expect(all.getAttribute('aria-pressed')).toBe('true')
    // Three RUNNING browser agents and the STARTING one (QA12-20).
    expect(browsers.textContent).toMatch(/Browsers\s*4/)
  })

  it('Browsers lists them needs-you first, each with its device outline, and theater all opens the first', async () => {
    const { container } = await mount()
    expect(screen.queryByRole('link', { name: /watch all browsers/i })).toBeNull()
    fireEvent.click(within(screen.getByRole('group', { name: /browser agents/i })).getByRole('button', { name: /^Browsers/ }))
    await waitFor(() =>
      expect([...container.querySelectorAll('.rows .row.clickable')].map((r) => r.getAttribute('data-task-id'))).toEqual([
        'task_needsyou00000000001',
        'task_browser0000000000001',
        'task_tablet0000000000001',
        'task_starting00000000001',
      ]),
    )
    // The starting one has no browser yet: a mark, not an outline.
    expect(container.querySelector('[data-task-id="task_starting00000000001"] .ag-device')).toBeNull()
    expect(container.querySelector('[data-task-id="task_starting00000000001"] .cr-starting')).not.toBeNull()
    const device = (id: string) => container.querySelector(`[data-task-id="${id}"] .ag-device`)
    await waitFor(() => expect(device('task_needsyou00000000001')?.getAttribute('data-device')).toBe('phone'))
    expect(device('task_needsyou00000000001')?.getAttribute('data-orientation')).toBe('portrait')
    await waitFor(() => expect(device('task_tablet0000000000001')?.getAttribute('data-device')).toBe('tablet'))
    expect(device('task_tablet0000000000001')?.getAttribute('data-orientation')).toBe('landscape')
    // Not reported (LB-SEAM serves null): an outline that says so, not a guessed desktop.
    await waitFor(() => expect(device('task_browser0000000000001')?.getAttribute('data-device')).toBe('unknown'))
    expect(device('task_browser0000000000001')?.getAttribute('aria-label')).toMatch(/not reported/i)
    // An ordinary agent gets no outline and no session read.
    expect(api.loadLiveSession.mock.calls.map(([id]) => id)).not.toContain('task_coder000000000000001')

    const theaterAll = screen.getByRole('link', { name: /watch all browsers/i })
    expect(theaterAll.getAttribute('href')).toBe(theaterPath('task_needsyou00000000001', null))
    expect(theaterAll.getAttribute('href')).toMatch(/\/browser\?theater=1$/)
  })

  /**
   * Visual QA pass 1, overview-agents-17 (QA1B-07). The fixture build serves
   * no live session, actions or recordings, so '⤢ theater all' opened a
   * theater on 'Not found' and 'No recording to play'. The owner's answer:
   * hide the link in fixtures. MUTATION: drop `!USE_FIXTURES` from the
   * link's condition in Agents.tsx and the link is found.
   */
  it('offers no theater all in the fixture build, which has no live browser to show', async () => {
    build.fixtures = true
    try {
      const { container } = await mount()
      fireEvent.click(within(screen.getByRole('group', { name: /browser agents/i })).getByRole('button', { name: /^Browsers/ }))
      await waitFor(() => expect(container.querySelectorAll('.rows .row.clickable')).toHaveLength(4))
      expect(screen.queryByRole('link', { name: /theater all/i })).toBeNull()
    } finally {
      build.fixtures = false
    }
  })
})

// ---------------------------------------------------------------------------
// 2. The Overview hand-off card
// ---------------------------------------------------------------------------

describe("the Overview's hand-off card: Take control", () => {
  const ID = 'task_hold00000000000000001'

  it('is offered when the session says you may, asks first, takes, then opens the Browser tab', async () => {
    let mine = false
    api.loadLiveSession.mockImplementation(async () =>
      read(session({ human_wait: { reason: 'Sign in', expect: 'github.com', requested_at: iso(NOW), deadline: iso(NOW + 540_000) }, controller: mine ? { email: ME, since: iso(NOW) } : null })),
    )
    api.takeLiveControl.mockImplementation(async () => {
      mine = true
      return ok(null)
    })
    render(<HandoffCards tasks={[holding(ID)]} now={NOW} onOpen={() => {}} />)
    const banner = document.querySelector('.c-banner') as HTMLElement
    // The card keeps its reason, site and time left, and its Open →.
    expect(banner.textContent).toContain('Sign in to GitHub as the bot account')
    expect(banner.textContent).toContain('github.com')
    expect(banner.textContent).toContain('9:00 left')
    expect(within(banner).getByRole('link', { name: /→/ })).toBeTruthy()

    const take = await within(banner).findByRole('button', { name: /^Take control$/ })
    fireEvent.click(take)
    // The header's own confirmation, not a one-click take.
    expect(api.takeLiveControl).not.toHaveBeenCalled()
    const ask = screen.getByRole('dialog', { name: /take control of the agent/i })
    await act(async () => {
      fireEvent.click(within(ask).getByRole('button', { name: /^Take control$/ }))
    })
    await waitFor(() => expect(window.location.pathname).toBe(browserTabPath(ID)))
    expect(api.takeLiveControl).toHaveBeenCalledWith(ID, {})
  })

  it('is not offered when the session says you may not', async () => {
    api.loadLiveSession.mockResolvedValue(read(session({ you: { email: ME, can_control: false, can_preempt: false, can_open: false, can_switch_device: false } })))
    render(<HandoffCards tasks={[holding(ID)]} now={NOW} onOpen={() => {}} />)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalledWith(ID))
    await act(async () => {})
    expect(screen.queryByRole('button', { name: /take control/i })).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 3. The phone's Browser tab
// ---------------------------------------------------------------------------

describe("the phone's Browser tab: the device switcher under ⋯", () => {
  class Socket extends EventTarget {
    binaryType = 'blob'
    readyState = 0
    onmessage = null
    send() {}
    close() {}
  }
  const agent = task('task_0123456789abcdef0789', 'RUNNING', { runner_profile: BROWSER })
  const pane = () => (
    <LiveBrowserPane task={agent} live={{ status: 'ok', liveBrowser: true, session: session() }} reload={() => {}} />
  )

  it('at phone width, the toolbar holds ⋯, and the device, rotate and touch are in its menu; ◫ logs stays with the view’s controls', () => {
    atWidth(390)
    vi.stubGlobal('WebSocket', Socket)
    render(pane())
    const bar = screen.getByRole('toolbar', { name: /browser view/i })
    expect(within(bar).queryByRole('group', { name: /^device$/i })).toBeNull()
    expect(within(bar).queryByRole('button', { name: /rotate/i })).toBeNull()
    const more = within(bar).getByRole('button', { name: /browser view options/i })
    expect(more.textContent).toContain('⋯')
    expect(more.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    const menu = screen.getByRole('group', { name: /browser view options/i })
    expect(within(menu).getByRole('group', { name: /^device$/i })).toBeTruthy()
    expect(within(menu).getByRole('button', { name: /rotate/i })).toBeTruthy()
    expect(within(menu).getByRole('button', { name: /touch/i })).toBeTruthy()
    // ◫ logs changes this view, not the agent's browser (BT-011, #2423): it stays in the view's group.
    expect(within(menu).queryByRole('button', { name: /logs/i })).toBeNull()
    expect(within(within(bar).getByRole('group', { name: 'This view' })).getByRole('button', { name: /logs/i })).toBeTruthy()
    // No Device menu inside the ⋯ menu: a menu in a menu is one press too many.
    expect(within(menu).queryByRole('button', { name: /^Device:/ })).toBeNull()
  })

  it('at desktop width, there is no ⋯ and the device switcher is the toolbar’s Device menu', () => {
    atWidth(1440)
    vi.stubGlobal('WebSocket', Socket)
    render(pane())
    const bar = screen.getByRole('toolbar', { name: /browser view/i })
    expect(within(bar).queryByRole('button', { name: /browser view options/i })).toBeNull()
    fireEvent.click(within(bar).getByRole('button', { name: /^Device:/ }))
    expect(within(bar).getByRole('group', { name: /^device$/i })).toBeTruthy()
  })
})
