// THEATER KEPT THROUGH BROWSER START-UP (part of #1346, comment 3).
//
// MEASURED 2026-10-15 (QA pass on the live deployment): every task opened at
// `…/browser?theater=1` while its browser was still starting landed on the
// Browser tab without the theater. The router reads `?theater=1` on arrival
// only and writes the bar back without it (App.tsx `Route.theater`), and the
// Browser tab drew StartupProgress until the session existed, mounting
// <TheaterHost initiallyOpen> only then -- by which time the route, and so
// the `theater` prop, no longer carried the request.
//
// Each test renders the Browser tab with `theater` set and no session, then
// does what the router does (the prop goes false, the bar loses the query)
// and delivers the session. Defects these catch: reading `initiallyOpen`
// from the prop as it is when the session arrives, rather than from the
// request made on arrival; or keeping the request past the first theater it
// opened, so a later session reopens a theater the person closed. The last
// test does the same one step earlier, through AgentSplit: the route drops
// the request while the task itself is still being read, before the Browser
// tab is drawn at all.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadLiveSession: vi.fn(),
  mintLiveTicket: vi.fn(),
  renewLiveControl: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveActions: vi.fn(),
  loadStartup: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadChildren: vi.fn(),
  loadTranscript: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    addEventListener() {}
    disconnect() {}
  },
}))

const { LiveBrowserPane } = await import('../LiveBrowser')
const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_theaterstartup0001'
const ADDRESS = `/agents/live/${ID}/browser`
const ME = ['ada', 'example.com'].join('@')

function agent(): Task {
  return runTask({ id: ID, state: 'STARTING', runner_profile: 'claude-code-browser', resource_class: 'browser', completed_at: null, result_summary: null })
}

function session(id = 'ls_startup'): LiveSession {
  return {
    session_id: id,
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
  }
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const theaterOpen = () => screen.queryByRole('dialog', { name: /^Theater/ })
const tab = (theater: boolean, s: LiveSession | null) => (
  <LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} theater={theater} />
)

beforeEach(() => {
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.loadLiveActions.mockReturnValue(new Promise(() => {}))
  api.loadStartup.mockReturnValue(new Promise(() => {}))
  api.loadTasks.mockReturnValue(new Promise(() => {}))
  api.renewLiveControl.mockReturnValue(new Promise(() => {}))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  localStorage.clear()
  resetListSnap()
  window.history.replaceState(null, '', `${ADDRESS}?theater=1`)
})

afterEach(() => {
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('?theater=1 survives the browser starting (#1346 comment 3)', () => {
  it('opens the theater when the session arrives after the route dropped the request', async () => {
    const view = render(tab(true, null))
    expect(theaterOpen()).toBeNull()
    // The router writes the bar back without `?theater=1`, and the route it
    // rebuilds carries no `theater`, while the browser is still starting.
    window.history.replaceState(null, '', ADDRESS)
    view.rerender(tab(false, null))
    expect(theaterOpen()).toBeNull()

    view.rerender(tab(false, session()))
    await waitFor(() => expect(theaterOpen()).not.toBeNull())
    expect(new URLSearchParams(window.location.search).get('theater')).toBe('1')
  })

  it('does not open a theater nobody asked for', async () => {
    window.history.replaceState(null, '', ADDRESS)
    const view = render(tab(false, null))
    view.rerender(tab(false, session()))
    await screen.findByText(/Nobody is in control/)
    expect(theaterOpen()).toBeNull()
    expect(window.location.search).toBe('')
  })

  it('spends the request on the first theater: a later session does not reopen one the person closed', async () => {
    const view = render(tab(true, null))
    window.history.replaceState(null, '', ADDRESS)
    view.rerender(tab(false, session('ls_first')))
    await waitFor(() => expect(theaterOpen()).not.toBeNull())
    act(() => {
      fireEvent.keyDown(window, { key: 'Escape' })
    })
    await waitFor(() => expect(theaterOpen()).toBeNull())

    // The browser restarts: start-up again, then a new session.
    view.rerender(tab(false, null))
    view.rerender(tab(false, session('ls_second')))
    await screen.findByText(/Nobody is in control/)
    expect(theaterOpen()).toBeNull()
    expect(new URLSearchParams(window.location.search).get('theater')).toBeNull()
  })
})

describe('the split holds the request while the task is read (#1346 comment 3)', () => {
  const split = (theater: boolean) => (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane="browser" theater={theater} artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${ID}`} />
    </div>
  )

  it('opens the theater when the route dropped the request before the Browser tab was drawn', async () => {
    let answer: (r: Result<Task>) => void = () => {}
    api.loadTask.mockReturnValue(new Promise<Result<Task>>((resolve) => (answer = resolve)))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    const view = render(split(true))
    window.history.replaceState(null, '', ADDRESS)
    view.rerender(split(false))
    expect(theaterOpen()).toBeNull()

    await act(async () => answer(ok(agent())))
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), { timeout: 12_000 })
    expect(new URLSearchParams(window.location.search).get('theater')).toBe('1')
  })
})
