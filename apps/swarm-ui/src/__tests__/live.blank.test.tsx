// THE STRIP SAYS WHY THE PAGE IS NOT SHOWN (LIVE-BLANK; part of #1030).
//
// MEASURED 2026-10-13 (release 1e86c37e): on Google's reCAPTCHA page the
// canvas showed the browser's tab strip over a flat #222 page area for ten
// minutes, and on a resumed attempt a flat #555 frame, and nothing on the
// tab said why. The gate still fills a frame it cannot vouch for -- every
// pixel sent passes its mask -- and now names the reason as the RFB desktop
// name (livegate/rfb.py, DesktopName -307), which noVNC hands the page as a
// `desktopname` event. The line under the screen says it.
//
// MUTATIONS: drop the `desktopname` listener; show every desktop name (the
// gate's ordinary "swarm live browser" would read as a fault); keep the
// reason after the gate names the page again.

import { act, render, screen, waitFor } from '@testing-library/react'
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

class FakeSocket extends EventTarget {
  binaryType = 'blob'
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
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

const { LiveBrowserPane, gateReason } = await import('../LiveBrowser')

const ID = 'task_0123456789abcdef0790'
const ME = ['ada', 'example.com'].join('@')
const WHY = 'Can’t show this page safely: the tab on screen has not reported its layout yet'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function session(): LiveSession {
  return {
    session_id: 'ls_blank',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://www.google.com', host: 'www.google.com', registrable_domain: 'google.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
  }
}

const task: Task = runTask({
  id: ID,
  tenant_id: 'eng',
  state: 'RUNNING',
  runner_profile: 'claude-code-browser',
  resource_class: 'browser',
  attempt_count: 2,
  completed_at: null,
  result_summary: null,
})

beforeEach(() => {
  rfb.FakeRfb.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404, headers: { 'content-type': 'application/json' } })),
  )
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.mintLiveTicket.mockImplementation(async () =>
    ok({ ticket: `vt.${crypto.randomUUID()}`, session_id: 'ls_blank', expires_in: 30, socket_path: '/v1/live/ls_blank/stream' }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the gate’s reason for not showing the page', () => {
  it('is read only from a desktop name that says it cannot show the page', () => {
    expect(gateReason('Can’t show this page safely: a frame on the page cannot be placed')).toBe(
      'Can’t show this page safely: a frame on the page cannot be placed',
    )
    expect(gateReason("Can't show this page safely: switching tabs")).toBe("Can't show this page safely: switching tabs")
    expect(gateReason('swarm live browser')).toBeNull()
    expect(gateReason('')).toBeNull()
  })

  it('is said under the screen while the gate names it, and goes when the page is back', async () => {
    render(<LiveBrowserPane task={task} live={{ status: 'ok', liveBrowser: true, session: session() }} reload={() => {}} />)
    const r = await waitFor(() => {
      const made = rfb.FakeRfb.made.at(-1)
      expect(made, 'noVNC was never attached').toBeTruthy()
      return made!
    })
    act(() => {
      r.dispatchEvent(new Event('connect'))
    })
    expect(screen.getByText('View only.')).toBeTruthy()
    act(() => {
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: WHY } }))
    })
    expect(screen.getByText(WHY, { exact: false })).toBeTruthy()
    act(() => {
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: 'swarm live browser' } }))
    })
    expect(screen.queryByText(WHY, { exact: false })).toBeNull()
    expect(screen.getByText('View only.')).toBeTruthy()
  })
})
