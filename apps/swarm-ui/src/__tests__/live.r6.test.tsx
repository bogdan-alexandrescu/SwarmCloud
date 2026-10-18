// LANE LB-R6 (part of #1030): A LIVE SESSION THAT RESUMES ON A NEW ATTEMPT
// PAINTS FOR VIEWERS, AND THE CAPTION SAYS WHAT PAGE IT IS ON.
//
// MEASURED 2026-10-12, re-QA pass 8 (task_4e0cf1c649634b949703): attempt 2
// resumed, and a fresh console tab showed LIVE, `https://github.com` and the
// iPhone 15 chip over a phone frame that stayed black for 30 s, captioned
// "iPhone 15 · 393×852 · path not reported".
//
//   1. the caption is `<device> · <W>×<H> · <path>`, the path the gate
//      reported with the origin (§6 item 11), and "path not reported" only
//      when there is none;
//   2. the canvas connects to the session the pane is drawing -- the CURRENT
//      attempt's -- and nothing else: a ticket minted for another session (an
//      attempt that ended, or one that started since the read) opens no
//      socket, and the pane reads the session again;
//   3. when the attempt changes under an open tab, the canvas reconnects to
//      the new attempt's session.
//
// MUTATIONS: drop `origin.path` from `deviceCaption`; connect whatever
// session the ticket names; key LiveScreen by the task instead of the session.

import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession, LiveTicket } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  mintLiveTicket: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveActions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const rfb = vi.hoisted(() => {
  class FakeRfb {
    static made: FakeRfb[] = []
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    constructor(target: HTMLElement) {
      target.appendChild(document.createElement('canvas'))
      FakeRfb.made.push(this)
    }
    addEventListener() {}
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

class FakeSocket {
  static made: FakeSocket[] = []
  binaryType = 'blob'
  readyState = 0
  protocol = ''
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  constructor(readonly url: string) {
    FakeSocket.made.push(this)
  }
  send() {}
  close() {
    this.readyState = 3
  }
}

const { LiveBrowserPane, deviceCaption } = await import('../LiveBrowser')

const ID = 'task_4e0cf1c649634b949703'
const ME = ['ada', 'example.com'].join('@')
const ATTEMPT_1 = 'live_att_153c1d60cfef451cb69b'
const ATTEMPT_2 = 'live_att_75e17009c2a7418b85e7'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    attempt_count: 2,
    completed_at: null,
    result_summary: null,
  })
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
]

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: ATTEMPT_2,
    state: 'open',
    closed_reason: null,
    origin: {
      origin: 'https://github.com',
      host: 'github.com',
      registrable_domain: 'github.com',
      scheme: 'https:',
      path: '/settings/profile',
    },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'iphone-15', orientation: 'portrait', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

/** Ticket text is built at runtime: nothing here has a credential's shape. */
function ticketFor(sessionId: string): Result<LiveTicket> {
  return ok({ ticket: `t.${Math.random().toString(36).slice(2)}`, session_id: sessionId, expires_in: 30, socket_path: '' })
}

function pane(s: LiveSession, reload: () => void = () => {}) {
  return render(<LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: s }} reload={reload} />)
}

const sockets = (sessionId: string) => FakeSocket.made.filter((s) => s.url.includes(`/v1/live/${sessionId}/stream`))

beforeEach(() => {
  rfb.FakeRfb.made = []
  FakeSocket.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.loadLiveActions.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('1. the caption says what page the device is on', () => {
  it('reads `<device> · <W>×<H> · <path>` with the path the gate reported', () => {
    expect(deviceCaption(session())).toBe('iPhone 15 · 393×852 · /settings/profile')
  })

  it('draws it under the device', () => {
    api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
    pane(session())
    expect(document.querySelector('.live-caption')?.textContent).toBe('iPhone 15 · 393×852 · /settings/profile')
  })

  it('says "path not reported" only when no path was reported', () => {
    expect(deviceCaption(session({ origin: { ...session().origin!, path: null } }))).toBe(
      'iPhone 15 · 393×852 · path not reported',
    )
    expect(deviceCaption(session({ origin: null }))).toBe('iPhone 15 · 393×852 · path not reported')
  })
})

describe("2. the canvas connects to the current attempt's session only", () => {
  it("opens the socket on the attempt-2 session the pane is drawing", async () => {
    api.mintLiveTicket.mockResolvedValue(ticketFor(ATTEMPT_2))
    pane(session())
    await waitFor(() => expect(sockets(ATTEMPT_2)).toHaveLength(1))
    expect(api.mintLiveTicket).toHaveBeenCalledWith(ID)
    expect(sockets(ATTEMPT_1)).toEqual([])
  })

  it('opens no socket on a ticket for another session, and reads the session again', async () => {
    api.mintLiveTicket.mockResolvedValue(ticketFor(ATTEMPT_1))
    const reload = vi.fn()
    pane(session(), reload)
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(FakeSocket.made).toEqual([])
    expect(rfb.FakeRfb.made).toEqual([])
  })
})

describe('3. a new attempt under an open tab', () => {
  it("reconnects the canvas to the new attempt's session", async () => {
    api.mintLiveTicket.mockResolvedValue(ticketFor(ATTEMPT_1))
    const view = pane(session({ session_id: ATTEMPT_1 }))
    await waitFor(() => expect(sockets(ATTEMPT_1)).toHaveLength(1))

    api.mintLiveTicket.mockResolvedValue(ticketFor(ATTEMPT_2))
    await act(async () => {
      view.rerender(
        <LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: session() }} reload={() => {}} />,
      )
    })
    await waitFor(() => expect(sockets(ATTEMPT_2)).toHaveLength(1))
    expect(sockets(ATTEMPT_1)).toHaveLength(1)
  })
})
