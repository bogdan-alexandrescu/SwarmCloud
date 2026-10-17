// THE CAPTION NEVER NAMES A DEVICE THE SCREEN CONTRADICTS (lane LB-GREY; part
// of #1341).
//
// MEASURED 2026-10-15 ~04:05Z on task_2ac5d7e22a51460995a2, `?theater=1`, as
// the person in control with the Desktop preset selected: the caption under
// the screen read "iPad · 810×1080 · /sorry/index". PR 1349 made the caption
// say a preset ASKED for beside the one reported; what put "iPad" there was
// never found. Every device label in the console is `session.device`, which
// swarm-api writes from the gate's report alone (live.py `record_report`):
// the console does not derive "iPad", it prints it. What it can check is the
// session's own device -- the frame the gate sends, which it crops to the
// device's display (livegate/device.py `captured`): Desktop is landscape,
// 1440 wide; iPad is portrait, 810×1080, until it is turned. A report whose
// orientation the frame contradicts is not printed as the device.
//
//   1. `deviceCaption`: with no frame, or a frame that agrees, the caption is
//      the report's, as before; with a frame of the other orientation it says
//      the device is not confirmed and names the report as a report.
//   2. In the pane: a Desktop-shaped frame under an iPad report never reads
//      "iPad · 810×1080"; when the frame turns to the report's shape the
//      caption is the report's again.
//
// MUTATION: draw `deviceCaption(session)` without the frame (1 and 2 fail).

import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTaskLogs: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveSession: vi.fn(),
  mintLiveTicket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
  setLiveDevice: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

/** noVNC's RFB, with the framebuffer size noVNC 1.4.0 keeps (`_fbWidth`, `_fbHeight`). */
const rfb = vi.hoisted(() => {
  class FakeRfb extends EventTarget {
    static made: FakeRfb[] = []
    static fb = { width: 0, height: 0 }
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    _fbWidth = FakeRfb.fb.width
    _fbHeight = FakeRfb.fb.height
    constructor(target: HTMLElement) {
      super()
      target.appendChild(document.createElement('canvas'))
      FakeRfb.made.push(this)
    }
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

class FakeSocket extends EventTarget {
  binaryType = 'blob'
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  constructor(readonly url: string) {
    super()
    queueMicrotask(() => {
      this.readyState = 1
      const opened = new Event('open')
      this.onopen?.(opened)
      this.dispatchEvent(opened)
    })
  }
  send() {}
  close() {
    this.readyState = 3
  }
}

/** A ResizeObserver the test fires: noVNC rescales its canvas when the framebuffer changes size. */
let observers: Array<() => void> = []
class FakeResizeObserver {
  constructor(readonly cb: () => void) {}
  observe() {
    observers.push(this.cb)
  }
  unobserve() {}
  disconnect() {
    observers = observers.filter((c) => c !== this.cb)
  }
}

const { LiveBrowserPane, deviceCaption } = await import('../LiveBrowser')

const ID = 'task_0123456789abcdef1341'
const ME = ['ada', 'example.com'].join('@')

// The frames the gate sends (presets.py: SCREEN_SCALE 1, BROWSER_UI_HEIGHT 87;
// device.py `captured`: the desktop keeps its browser band, a tablet does not).
const DESKTOP_FRAME = { width: 1440, height: 900 + 87 }
const IPAD_FRAME = { width: 810, height: 1080 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_caption',
    state: 'open',
    closed_reason: null,
    origin: {
      origin: 'https://www.google.com',
      host: 'www.google.com',
      registrable_domain: 'google.com',
      scheme: 'https:',
      path: '/sorry/index',
    },
    human_wait: null,
    controller: { email: ME, since: '2026-10-15T04:00:00Z' },
    open_to_members: false,
    device: { preset: 'ipad-7', orientation: 'portrait', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

const task: Task = runTask({
  id: ID,
  tenant_id: 'eng',
  state: 'RUNNING',
  runner_profile: 'claude-code-browser',
  resource_class: 'browser',
  attempt_count: 1,
  completed_at: null,
  result_summary: null,
})

const caption = () => document.querySelector('.live-caption')?.textContent ?? ''

async function connected(): Promise<InstanceType<typeof rfb.FakeRfb>> {
  const r = await waitFor(() => {
    const made = rfb.FakeRfb.made.at(-1)
    expect(made, 'noVNC was never attached').toBeTruthy()
    return made!
  })
  act(() => {
    r.dispatchEvent(new Event('connect'))
  })
  return r
}

beforeEach(() => {
  rfb.FakeRfb.made = []
  rfb.FakeRfb.fb = { width: 0, height: 0 }
  observers = []
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ tasks: [] }), { status: 200, headers: { 'content-type': 'application/json' } })),
  )
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.loadLiveSession.mockReturnValue(new Promise(() => {}))
  api.mintLiveTicket.mockImplementation(async () =>
    ok({ ticket: `vt.${crypto.randomUUID()}`, session_id: 'ls_caption', expires_in: 30, socket_path: '/v1/live/ls_caption/stream' }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('1. deviceCaption checks the report against the frame', () => {
  it('is the report with no frame drawn yet, and with a frame that agrees', () => {
    expect(deviceCaption(session())).toBe('iPad · 810×1080 · /sorry/index')
    expect(deviceCaption(session(), null)).toBe('iPad · 810×1080 · /sorry/index')
    expect(deviceCaption(session(), IPAD_FRAME)).toBe('iPad · 810×1080 · /sorry/index')
    const desktop = session({ device: { preset: 'desktop', orientation: 'landscape', touch: false } })
    expect(deviceCaption(desktop, DESKTOP_FRAME)).toBe('Desktop · 1440×900 · /sorry/index')
    // A turned iPad is landscape, and so is its frame.
    const turned = session({ device: { preset: 'ipad-7', orientation: 'landscape', touch: false } })
    expect(deviceCaption(turned, { width: 1080, height: 810 })).toBe('iPad · 1080×810 · /sorry/index')
  })

  it('never names a portrait iPad over a landscape Desktop frame', () => {
    const said = deviceCaption(session(), DESKTOP_FRAME)
    expect(said).not.toContain('iPad · 810×1080')
    expect(said).toBe('Device not confirmed: the gate reported iPad (portrait), but the screen it sends is landscape · /sorry/index')
  })

  it('never names a Desktop over a portrait frame either', () => {
    const desktop = session({ device: { preset: 'desktop', orientation: 'landscape', touch: false } })
    expect(deviceCaption(desktop, IPAD_FRAME)).toBe(
      'Device not confirmed: the gate reported Desktop (landscape), but the screen it sends is portrait · /sorry/index',
    )
  })

  it('does not judge a frame with no size, or a square one', () => {
    expect(deviceCaption(session(), { width: 0, height: 0 })).toBe('iPad · 810×1080 · /sorry/index')
    expect(deviceCaption(session(), { width: 900, height: 900 })).toBe('iPad · 810×1080 · /sorry/index')
  })
})

describe('2. the pane reads the frame noVNC draws', () => {
  it('says not confirmed while a Desktop frame is under an iPad report, and iPad once the frame is one', async () => {
    rfb.FakeRfb.fb = DESKTOP_FRAME
    render(<LiveBrowserPane task={task} live={{ status: 'ok', liveBrowser: true, session: session() }} reload={() => {}} />)
    const r = await connected()
    await waitFor(() => expect(caption()).toContain('Device not confirmed'))
    expect(caption()).not.toContain('iPad · 810×1080')

    // The gate's frame turns into the iPad's: noVNC resizes, and its canvas with it.
    act(() => {
      r._fbWidth = IPAD_FRAME.width
      r._fbHeight = IPAD_FRAME.height
      for (const cb of observers) cb()
    })
    await waitFor(() => expect(caption()).toBe('iPad · 810×1080 · /sorry/index'))
  })

  it('is the report while the frame agrees with it', async () => {
    rfb.FakeRfb.fb = DESKTOP_FRAME
    const desktop = session({ device: { preset: 'desktop', orientation: 'landscape', touch: false } })
    render(<LiveBrowserPane task={task} live={{ status: 'ok', liveBrowser: true, session: desktop }} reload={() => {}} />)
    await connected()
    expect(caption()).toBe('Desktop · 1440×900 · /sorry/index')
  })
})
