// THE THEATER'S STRIP SAYS WHY PART OF THE PAGE IS COVERED, AND THE DEVICE
// AND CONTROL LABELS FOLLOW THE SESSION (lane STRIP-LABEL; part of #1341).
//
// MEASURED 2026-10-15 ~04:05Z on task_2ac5d7e22a51460995a2, `?theater=1`, as
// the person in control with the Desktop preset selected:
//   (a) the strip above the browser read only "The page is on google.com, the
//       site the agent named. You are in control." The gate's cover reason
//       ("Part of this page is covered: an embedded frame on it cannot be
//       checked.", the RFB desktop name, livegate mask.py PARTLY_COVERED) was
//       drawn only under the tab's screen, below the fold behind the theater.
//   (b) a span below the fold read "iPad · 810×1080 · /sorry/index" and
//       "View only", while the Device panel showed Desktop and the header said
//       "You are in control".
//
//   1. In theater, the gate's reason is in the strip above the browser while
//      the desktop name carries one, and goes when it does not.
//   2. The device label follows a preset change: the device route's answer is
//      the session as the switch left it, and it is drawn.
//   3. The control label reads in-control while the viewer holds control,
//      also when the relay's first status frame says so before the session
//      read drawn has caught up: that frame is checked against what is drawn.
//
// MUTATIONS: draw the reason only under the screen (1 fails); call `reload()`
// without the device route's answer (2 fails); take the relay's first status
// frame as what the read already drew, unchecked (3 fails).

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
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

/** noVNC's RFB, with its events: `connect` and `desktopname` are dispatched by the test. */
const rfb = vi.hoisted(() => {
  class FakeRfb extends EventTarget {
    static made: FakeRfb[] = []
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
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

/** The relay's socket: opens at once, and the test sends its status frames. */
class FakeSocket extends EventTarget {
  static made: FakeSocket[] = []
  binaryType = 'blob'
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  constructor(readonly url: string) {
    super()
    FakeSocket.made.push(this)
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
  /** A text frame from swarm-api, as the relay sends its status. */
  status(body: Record<string, unknown>) {
    const ev = new MessageEvent('message', { data: JSON.stringify(body) })
    this.onmessage?.(ev)
    this.dispatchEvent(ev)
  }
}

const { LiveBrowserPane, liveReadOf } = await import('../LiveBrowser')

const ID = 'task_0123456789abcdef1341'
const ME = ['ada', 'example.com'].join('@')
const COVERED = 'Part of this page is covered: an embedded frame on it cannot be checked'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_strip',
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

/**
 * The pane as the split holds it (`useLiveSession`): `reload(answer)` draws a
 * control route's answer, and `reload()` reads the session again.
 */
function Held({ first }: { first: LiveSession }) {
  const [s, setS] = useState(first)
  const draw = (answer: unknown) => {
    const read = liveReadOf(answer)
    if (read !== null && read.session !== null) setS(read.session)
  }
  return (
    <LiveBrowserPane
      task={task}
      theater
      live={{ status: 'ok', liveBrowser: true, session: s }}
      reload={(answer) => {
        if (answer !== undefined) draw(answer)
        else void api.loadLiveSession(ID).then((r: Result<LiveRead>) => r.status === 'ok' && draw(r.data))
      }}
    />
  )
}

const theaterRoot = () => screen.getByRole('dialog', { name: /^Theater/ })
const theaterStrip = () => theaterRoot().querySelector('.th-strip') as HTMLElement

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
  FakeSocket.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
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
    ok({ ticket: `vt.${crypto.randomUUID()}`, session_id: 'ls_strip', expires_in: 30, socket_path: '/v1/live/ls_strip/stream' }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('1. the theater strip says why part of the page is covered', () => {
  it('shows a partly-covered desktop name above the browser, and clears it when the gate names the page again', async () => {
    render(<Held first={session()} />)
    const r = await connected()
    expect(within(theaterStrip()).queryByText(COVERED, { exact: false })).toBeNull()

    act(() => {
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: COVERED } }))
    })
    const strip = theaterStrip()
    expect(within(strip).getByText(COVERED, { exact: false })).toBeTruthy()
    // Above the browser: the strip comes before the stage the screen is borrowed into.
    const stage = theaterRoot().querySelector('.th-stage') as HTMLElement
    expect(strip.compareDocumentPosition(stage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    act(() => {
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: 'swarm live browser' } }))
    })
    expect(within(theaterStrip()).queryByText(COVERED, { exact: false })).toBeNull()
  })
})

describe('2. the device label follows the preset', () => {
  it('reads Desktop once the person in control switches to it, from the device route’s answer', async () => {
    render(<Held first={session()} />)
    await connected()
    const caption = () => document.querySelector('.live-caption')?.textContent ?? ''
    expect(caption()).toBe('iPad · 810×1080 · /sorry/index')

    const desktop = session({ device: { preset: 'desktop', orientation: 'landscape', touch: false } })
    api.setLiveDevice.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: desktop }))
    const panel = theaterRoot().querySelector('.th-device') as HTMLElement
    fireEvent.click(within(panel).getByRole('button', { name: 'Desktop' }))
    await waitFor(() => expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'desktop' }))

    await waitFor(() => expect(caption()).toBe('Desktop · 1440×900 · /sorry/index'))
    expect(within(panel).getByRole('button', { name: 'Desktop' }).getAttribute('aria-pressed')).toBe('true')
    expect(panel.querySelector('.th-device-now')?.textContent).toContain('Desktop · landscape')
  })
})

describe('3. the control label follows who holds control', () => {
  it('reads in-control, not View only, while the viewer holds control', async () => {
    render(<Held first={session()} />)
    await connected()
    expect(screen.queryByText('View only.')).toBeNull()
    expect(screen.getByText('Click the browser to type into it.')).toBeTruthy()
    expect(document.querySelector('.live-screen')?.getAttribute('aria-label')).toBe('The agent’s browser. Click it to type into it.')
  })

  it('reads in-control when the relay says this viewer holds control before the session read has caught up', async () => {
    // The read drawn is older than the take; the next read has it.
    render(<Held first={session({ controller: null })} />)
    await connected()
    expect(screen.getByText('View only.')).toBeTruthy()
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    const ws = FakeSocket.made.at(-1)!
    act(() => {
      ws.status({ type: 'status', in_control: true, controller: ME, open: true })
    })
    await waitFor(() => expect(screen.queryByText('View only.')).toBeNull())
    expect(screen.getByText('Click the browser to type into it.')).toBeTruthy()
  })
})
