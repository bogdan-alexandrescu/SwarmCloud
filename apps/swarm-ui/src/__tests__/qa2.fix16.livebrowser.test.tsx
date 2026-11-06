// THE BROWSER TAB WITH NO PICTURE, VISUAL QA PASS 2 (QA2-FIX-16, ovag2-12;
// part of #2694). The fixture build serves an open session and mints no
// viewer ticket, so the tab opened on a large empty frame whose only
// explanation, "The fixture build serves no live browser.", was small text in
// the caption under it. Until a first frame, a connection that is retrying or
// has given up says why IN the frame's middle; the pane tells its host whether
// a picture is on screen, so the tab strip says live on a picture.
//
// MUTATION: drop the `blank` block from `LiveScreen` -- the first case finds
// no empty state in the frame. Call `onPicture` with true on the session read
// -- the second finds it told true with nothing drawn.

import { render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTaskLogs: vi.fn(),
  loadTranscript: vi.fn(),
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

/** noVNC's RFB: draws a canvas into the node it is handed, as noVNC does. */
const rfb = vi.hoisted(() => {
  class FakeRfb {
    static made: FakeRfb[] = []
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    readonly target: HTMLElement
    readonly canvas: HTMLCanvasElement
    constructor(target: HTMLElement) {
      this.target = target
      this.canvas = document.createElement('canvas')
      target.appendChild(this.canvas)
      FakeRfb.made.push(this)
    }
    addEventListener() {}
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

class FakeSocket extends EventTarget {
  static made: FakeSocket[] = []
  binaryType = 'blob'
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  constructor(readonly url: string) {
    super()
    FakeSocket.made.push(this)
    queueMicrotask(() => {
      this.readyState = 1
      // A browser WebSocket fires both its `onopen` property and its listeners.
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

const { LiveBrowserPane } = await import('../LiveBrowser')

const ID = 'task_0123456789abcdef0789'
const ME = ['ada', 'example.com'].join('@')
const GROUP = 'eng'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(tenant: string): Task {
  return runTask({
    id: ID,
    tenant_id: tenant,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    attempt_count: 1,
    completed_at: null,
    result_summary: null,
  })
}

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_mounts',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
    ...over,
  }
}


function Pane({ tenant, s }: { tenant: string; s: LiveSession }) {
  return <LiveBrowserPane task={agent(tenant)} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />
}

beforeEach(() => {
  rfb.FakeRfb.made = []
  FakeSocket.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  // The theater's timeline reads the actions route; nothing here answers it.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404, headers: { 'content-type': 'application/json' } })),
  )
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  // Built at run time: nothing here may look like a credential in the source.
  api.mintLiveTicket.mockImplementation(async () =>
    ok({ ticket: `vt.${crypto.randomUUID()}`, session_id: 'ls_mounts', expires_in: 30, socket_path: '/v1/live/ls_mounts/stream' }),
  )
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.setLiveDevice.mockResolvedValue(ok(null))
})

afterEach(() => {
  vi.unstubAllGlobals()
})
const NOT_SERVED = 'The fixture build serves no live browser.'

describe('ovag2-12: a frame with no picture says why in its middle', () => {
  it('puts the ticket refusal in the frame, once, while nothing has drawn', async () => {
    api.mintLiveTicket.mockImplementation(async () => ({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: null, message: NOT_SERVED },
    }))
    const { container } = render(<Pane tenant={GROUP} s={session()} />)
    const empty = await waitFor(() => {
      const el = container.querySelector('.live-frame [data-testid="live-frame-empty"]')
      expect(el, 'the frame says nothing').not.toBeNull()
      return el as HTMLElement
    })
    expect(empty.textContent).toContain('No picture from the agent’s browser')
    expect(empty.textContent).toContain(NOT_SERVED)
    // Said once: the caption under the frame does not repeat it.
    expect(container.querySelector('.live-screen-say')!.textContent).not.toContain(NOT_SERVED)
    expect(rfb.FakeRfb.made).toHaveLength(0)
  })

  it('tells its host there is no picture until noVNC reports a frame', async () => {
    api.mintLiveTicket.mockImplementation(async () => ({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: null, message: NOT_SERVED },
    }))
    const told: boolean[] = []
    render(
      <LiveBrowserPane
        task={agent(GROUP)}
        live={{ status: 'ok', liveBrowser: true, session: session() }}
        reload={() => {}}
        onPicture={(shown) => told.push(shown)}
      />,
    )
    await waitFor(() => expect(document.querySelector('[data-testid="live-frame-empty"]')).not.toBeNull())
    expect(told.length).toBeGreaterThan(0)
    expect(told).not.toContain(true)
  })
})
