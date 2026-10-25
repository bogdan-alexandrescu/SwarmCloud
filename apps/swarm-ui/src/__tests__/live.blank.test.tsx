// THE STRIP SAYS ONLY WHAT THE GATE STILL NAMES (LIVE-BLANK, part of #1030;
// LIVE-NOMASK-COPY, part of #2298).
//
// The gate used to fill a frame it could not vouch for and name the reason as
// the RFB desktop name ("Can't show this page safely: ...", "Part of this page
// is covered: ..."), which the strip drew. BGD decided on 2026-10-25 that the
// live browser redacts no part of the screen in any situation, so the gate no
// longer blanks or covers anything and the strip has no masking reason to
// draw: a desktop name with either lead is not a reason here. The one name the
// strip still draws is the gate's word that the agent finished and its browser
// closed (mask.py `REASON_BROWSER_CLOSED`; GREY-TAIL, part of #1344).
//
// MUTATIONS: drop the `desktopname` listener (the closed browser says
// nothing); show every desktop name (the gate's ordinary "swarm live browser"
// would read as a fault); put the masking leads back (the "safely" cases
// fail); keep the label after the gate names the page again.

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
const CLOSED = 'The agent has finished; its browser is closed.'
const SAFELY = 'Can’t show this page safely: the tab on screen has not reported its layout yet'

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

describe('the gate’s reason in the strip', () => {
  it('is never a masking reason: the gate blanks and covers nothing (BGD, 2026-10-25)', () => {
    expect(gateReason('Can’t show this page safely: a frame on the page cannot be placed')).toBeNull()
    expect(gateReason("Can't show this page safely: switching tabs")).toBeNull()
    expect(gateReason('Part of this page is covered: an embedded frame on it cannot be checked')).toBeNull()
    expect(gateReason('swarm live browser')).toBeNull()
    expect(gateReason('')).toBeNull()
  })

  it('reads the gate’s word that the agent finished, said with no lead (GREY-TAIL; part of #1344)', () => {
    // MUTATION: a gateReason that drops it leaves the closed browser's grey saying nothing.
    expect(gateReason(CLOSED)).toBe(CLOSED)
    // MUTATION: matching it as a prefix would read any name that starts with it.
    expect(gateReason('The agent has finished; its browser is closed. Or is it')).toBeNull()
  })

  it('draws no masking reason, draws the closed browser’s label, and clears it when the gate names the page again', async () => {
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
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: SAFELY } }))
    })
    expect(screen.queryByText(SAFELY, { exact: false })).toBeNull()
    expect(document.querySelector('[data-testid="live-cover"]')).toBeNull()
    act(() => {
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: CLOSED } }))
    })
    expect(screen.getByTestId('live-cover').textContent).toContain(CLOSED)
    act(() => {
      r.dispatchEvent(new CustomEvent('desktopname', { detail: { name: 'swarm live browser' } }))
    })
    expect(document.querySelector('[data-testid="live-cover"]')).toBeNull()
    expect(screen.getByText('View only.')).toBeTruthy()
  })
})
