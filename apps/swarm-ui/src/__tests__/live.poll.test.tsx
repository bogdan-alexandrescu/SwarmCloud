// THE BROWSER TAB READS ITS SESSION AGAIN WHILE IT IS OPEN (lane LIVE-POLL;
// part of #1343).
//
// MEASURED 2026-10-15, visual QA of release b93ad6cb: the Browser tab read
// `GET /v1/tasks/{id}/live/session` at page load and never again over 30 s
// (resource timing). The header stayed "dispatched" and the tab "browser is
// starting" after RUNNING; the strip said the gate had not reported the
// page's address and the device buttons were disabled while the API already
// served both; a SUCCEEDED task still read running; and rotate after choosing
// iPhone 15 sent "Desktop, portrait".
//
//   1. Origin and device appear without a reload once a session read returns
//      them -- also when the read before failed: a failure backs off, it does
//      not end the reads.
//   2. A finished task replaces "starting", from the task's own re-read on
//      the Browser tab's clock, and the reads stop.
//   3. Rotate after an iPhone 15 change sends iPhone 15: the device controls
//      wait for the gate's report of the change, read again until it comes,
//      and then act on the device it reports.
//   4. Nothing is read while the page is hidden; showing it reads at once.
//
// MUTATIONS: re-read only while the last read said `live_browser` (1 fails);
// leave the task on the drawer's 10 s clock (2 fails); let rotate act on the
// device reported before the change (3 fails); drop the visibilitychange
// read (4 fails).

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadChildren: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadLiveSession: vi.fn(),
  loadStartup: vi.fn(),
  mintLiveTicket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
  setLiveDevice: vi.fn(),
  loadTaskEventsPage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const rfb = vi.hoisted(() => {
  class FakeRfb {
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    constructor(target: HTMLElement) {
      target.appendChild(document.createElement('canvas'))
    }
    addEventListener() {}
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

const { AgentSplit } = await import('../AgentSplit')
const { LIVE_POLL_MS } = await import('../LiveBrowser')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef1343'
const ME = ['ada', 'example.com'].join('@')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    attempt_count: 1,
    completed_at: null,
    result_summary: null,
    ...over,
  })
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

const ORIGIN: NonNullable<LiveSession['origin']> = {
  origin: 'https://github.com',
  host: 'github.com',
  registrable_domain: 'github.com',
  scheme: 'https:',
  path: '/login',
}

/** An open session this viewer controls; origin and device as the gate has reported them. */
function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_poll',
    state: 'open',
    closed_reason: null,
    origin: ORIGIN,
    human_wait: null,
    controller: { email: ME, since: new Date().toISOString() },
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

const read = (s: LiveSession | null): Result<LiveRead> => ok<LiveRead>({ live_browser: true, session: s })

function Split() {
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane="browser"
        theater={false}
        artifact={null}
        closeTo="work/running/live"
        go={() => {}}
        base={`work/task/${ID}`}
      />
    </div>
  )
}

const toolbar = () => screen.getByRole('toolbar', { name: 'Browser view' })
/** The toolbar's Device menu (BT-003), opened if it is not: the chips, rotate and touch are in it. */
const openDevice = () => {
  const menu = within(toolbar()).getByRole('button', { name: /^Device:/ })
  if (menu.getAttribute('aria-expanded') !== 'true') fireEvent.click(menu)
}
const chip = (name: string) => {
  openDevice()
  return within(within(toolbar()).getByRole('group', { name: 'Device' })).getByRole('button', { name })
}
const rotate = () => {
  openDevice()
  return within(toolbar()).getByRole('button', { name: '⟲ rotate' })
}

/** Move the clock, letting every read it starts land. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

let hidden = false
function setHidden(on: boolean): void {
  hidden = on
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  localStorage.clear()
  resetListSnap()
  hidden = false
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') })
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({}), { status: 200, headers: { 'content-type': 'application/json' } })),
  )
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadStartup.mockReturnValue(new Promise(() => {}))
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.renewLiveControl.mockResolvedValue(ok(null))
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  // The prototype's own accessors come back.
  delete (document as unknown as Record<string, unknown>).visibilityState
  delete (document as unknown as Record<string, unknown>).hidden
  vi.clearAllMocks()
})

describe('1. origin and device appear without a reload', () => {
  it('draws them from a later read once the gate has reported them', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    // The read at load: the gate has reported nothing yet.
    api.loadLiveSession.mockResolvedValueOnce(read(session({ origin: null, device: null })))
    api.loadLiveSession.mockResolvedValue(read(session()))
    render(<Split />)

    await waitFor(() => expect(screen.getAllByText(/has not reported the page’s address/).length).toBeGreaterThan(0))
    expect((chip('Desktop') as HTMLButtonElement).disabled).toBe(true)

    await advance(LIVE_POLL_MS)
    await waitFor(() => expect(document.querySelector('.live-origin')?.textContent).toContain('github.com'))
    expect(screen.queryByText(/has not reported the page’s address/)).toBeNull()
    expect((chip('Desktop') as HTMLButtonElement).disabled).toBe(false)
    expect(chip('Desktop').getAttribute('aria-pressed')).toBe('true')
  })

  it('keeps reading after the read at load failed: a failure backs off, it does not end the reads', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    // A blip as the attempt starts, then the API serves what the gate reported.
    api.loadLiveSession.mockResolvedValueOnce({ status: 'error', error: { kind: 'http', status: 503, message: 'blip' } })
    api.loadLiveSession.mockResolvedValue(read(session()))
    render(<Split />)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalledTimes(1))

    // Backed off: the next read waits longer than a poll, and still comes.
    await advance(LIVE_POLL_MS * 4)
    await waitFor(() => expect(document.querySelector('.live-origin')?.textContent).toContain('github.com'))
    expect((chip('Desktop') as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('2. a finished task replaces "starting"', () => {
  it('re-reads the task on the Browser tab’s clock, says it finished, and stops reading', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(read(null))
    render(<Split />)
    await waitFor(() => expect(screen.getAllByText('The agent’s browser is starting.').length).toBeGreaterThan(0))

    api.loadTask.mockResolvedValue(ok(agent({ state: 'SUCCEEDED', completed_at: new Date().toISOString() })))
    // Well inside the drawer's own 10 s clock.
    await advance(LIVE_POLL_MS + 500)
    await waitFor(() => expect(screen.getByText(/This agent has finished/)).toBeTruthy())
    expect(screen.queryByText('The agent’s browser is starting.')).toBeNull()

    // The checkpoints pane reads the task on its own 10 s clock until it sees
    // the finish; that read is its own, not this tab's, so it is let land first.
    await advance(10_500)
    const tasks = api.loadTask.mock.calls.length
    const sessions = api.loadLiveSession.mock.calls.length
    await advance(LIVE_POLL_MS * 10)
    expect(api.loadTask.mock.calls.length).toBe(tasks)
    expect(api.loadLiveSession.mock.calls.length).toBe(sessions)
  })
})

describe('3. device actions use the device the gate reported', () => {
  it('rotate after an iPhone 15 change sends iPhone 15, never the Desktop from before it', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(read(session()))
    // swarm-api's set_device answers the session as it stands: the device
    // the gate last reported, from before the switch.
    api.setLiveDevice.mockResolvedValue(read(session()))
    render(<Split />)
    await waitFor(() => expect((chip('iPhone 15') as HTMLButtonElement).disabled).toBe(false))

    fireEvent.click(chip('iPhone 15'))
    await waitFor(() => expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'iphone-15' }))
    // Until the gate reports the change, rotate waits rather than act on Desktop.
    await waitFor(() => expect((rotate() as HTMLButtonElement).disabled).toBe(true))
    fireEvent.click(rotate())
    expect(api.setLiveDevice).toHaveBeenCalledTimes(1)

    // The gate reports it; the tab reads again until a read carries it.
    api.loadLiveSession.mockResolvedValue(read(session({ device: { preset: 'iphone-15', orientation: 'portrait', touch: false } })))
    await advance(LIVE_POLL_MS)
    await waitFor(() => expect((rotate() as HTMLButtonElement).disabled).toBe(false))
    expect(chip('iPhone 15').getAttribute('aria-pressed')).toBe('true')

    api.setLiveDevice.mockResolvedValue(read(session({ device: { preset: 'iphone-15', orientation: 'portrait', touch: false } })))
    fireEvent.click(rotate())
    await waitFor(() => expect(api.setLiveDevice).toHaveBeenLastCalledWith(ID, { preset: 'iphone-15', orientation: 'landscape' }))

    api.loadLiveSession.mockResolvedValue(read(session({ device: { preset: 'iphone-15', orientation: 'landscape', touch: false } })))
    await advance(LIVE_POLL_MS)
    await waitFor(() => expect((rotate() as HTMLButtonElement).disabled).toBe(false))
    await waitFor(() => expect(document.querySelector('.live-caption')?.textContent).toMatch(/^iPhone 15 · 852×393/))
    fireEvent.click(rotate())
    await waitFor(() => expect(api.setLiveDevice).toHaveBeenLastCalledWith(ID, { preset: 'iphone-15', orientation: 'portrait' }))
    for (const [, body] of api.setLiveDevice.mock.calls) expect((body as { preset: string }).preset).toBe('iphone-15')
  })
})

describe('4. no reads while the page is hidden', () => {
  it('reads neither the session nor the task while hidden, and reads at once when shown', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(read(session()))
    render(<Split />)
    await waitFor(() => expect(document.querySelector('.live-toolbar')).not.toBeNull())
    await advance(LIVE_POLL_MS)

    setHidden(true)
    const sessions = api.loadLiveSession.mock.calls.length
    const tasks = api.loadTask.mock.calls.length
    await advance(LIVE_POLL_MS * 10)
    expect(api.loadLiveSession.mock.calls.length).toBe(sessions)
    expect(api.loadTask.mock.calls.length).toBe(tasks)

    // Shown again: read at once, not a poll later.
    setHidden(false)
    await act(async () => {})
    await waitFor(() => expect(api.loadLiveSession.mock.calls.length).toBe(sessions + 1))
    expect(api.loadTask.mock.calls.length).toBeGreaterThan(tasks)
  })
})
