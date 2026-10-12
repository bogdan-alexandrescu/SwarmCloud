// THE BROWSER TAB'S MOUNTS (docs/design/live-browser.md §5.5.1, §6; lane
// LB-L11w): the theater (L11b, Theater.tsx) and Save as login (L12c,
// BrowserLogins.tsx) were built as self-contained pieces; this suite holds
// them mounted in `LiveBrowserPane`.
//
//   * on a live-browser task the tab shows ⤢, and pressing it opens the
//     theater with the tab's OWN screen node -- the element noVNC draws into
//     -- in its stage, on the one socket the tab already opened; Esc puts the
//     node back;
//   * the theater's device slot is the tab's own bar: it switches the device
//     for the person in control and is disabled for a viewer (LB-DEV-B);
//   * Save as login is offered once the person returns control in a personal
//     (`u-`) workspace, and never in a group one, never before control is
//     returned, and never when someone else took control over them.
//
// MUTATIONS: drop <TheaterButton> from the toolbar; hand the theater a node
// other than LiveScreen's; derive "returned" from `controller === null` alone
// (offered before anyone held control); treat a takeover as a return.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
const OTHER = ['grace', 'example.com'].join('@')
const PERSONAL = 'u-ada'
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

const held = (email: string) => ({ email, since: new Date().toISOString() })

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

describe('the Browser tab opens the theater on its own screen', () => {
  it('shows ⤢ on a live-browser task, and ⤢ opens the theater with the tab’s live screen in its stage', async () => {
    render(<Pane tenant={GROUP} s={session()} />)
    const r = await waitFor(() => {
      const made = rfb.FakeRfb.made.at(-1)
      expect(made, 'noVNC was never attached').toBeTruthy()
      return made!
    })
    const node = r.target
    expect(node.classList.contains('live-screen')).toBe(true)
    expect(screen.queryByRole('dialog', { name: /^Theater/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Open theater (T)' }))

    const theater = screen.getByRole('dialog', { name: /^Theater/ })
    const stage = theater.querySelector('.th-stage')
    expect(stage, 'the theater has no stage').not.toBeNull()
    expect(stage!.contains(node), 'the stage does not hold the tab’s screen node').toBe(true)
    expect(stage!.contains(r.canvas)).toBe(true)
    // A view, not a second session: still the one socket and the one noVNC.
    expect(FakeSocket.made).toHaveLength(1)
    expect(rfb.FakeRfb.made).toHaveLength(1)

    act(() => {
      theater.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(screen.queryByRole('dialog', { name: /^Theater/ })).toBeNull()
    expect(document.querySelector('.live-body')!.contains(node), 'Esc did not put the screen back in the tab').toBe(true)
  })
})

describe('the theater’s device slot switches the device for the person in control (LB-DEV-B)', () => {
  const theaterDevice = () => {
    fireEvent.click(screen.getByRole('button', { name: 'Open theater (T)' }))
    const slot = screen.getByRole('dialog', { name: /^Theater/ }).querySelector('.th-device')
    expect(slot, 'the theater has no device slot').not.toBeNull()
    return slot as HTMLElement
  }

  it('rotates through setLiveDevice from the theater', () => {
    const s = session({
      controller: held(ME),
      you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    })
    render(<Pane tenant={GROUP} s={s} />)
    const slot = theaterDevice()
    const rotate = Array.from(slot.querySelectorAll('button')).find((b) => /rotate/i.test(b.textContent ?? ''))!
    expect(rotate.disabled).toBe(false)
    fireEvent.click(rotate)
    expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'desktop', orientation: 'portrait' })
  })

  it('is disabled in the theater for a viewer', () => {
    render(<Pane tenant={GROUP} s={session({ controller: held(OTHER) })} />)
    const slot = theaterDevice()
    // The device control is UX1's chip group now (owner decision: chips, not a <select>).
    const chips = slot.querySelector('[role="group"][aria-label="Device"]')
    expect(chips).not.toBeNull()
    const buttons = Array.from(slot.querySelectorAll('button'))
    expect(buttons.length).toBeGreaterThan(0)
    for (const b of buttons) expect(b.disabled).toBe(true)
  })
})

describe('Save as login after Return control', () => {
  const offer = () => screen.queryByRole('button', { name: 'Save as login' })

  it('is offered once the person returns control in a personal workspace, and not before', () => {
    const { rerender } = render(<Pane tenant={PERSONAL} s={session()} />)
    expect(offer(), 'offered before anyone held control').toBeNull()
    rerender(<Pane tenant={PERSONAL} s={session({ controller: held(ME) })} />)
    expect(offer(), 'offered while still in control').toBeNull()
    rerender(<Pane tenant={PERSONAL} s={session({ controller: null })} />)
    expect(offer(), 'not offered after Return control').not.toBeNull()
  })

  it('is absent in a group workspace after the same return', () => {
    const { rerender } = render(<Pane tenant={GROUP} s={session({ controller: held(ME) })} />)
    rerender(<Pane tenant={GROUP} s={session({ controller: null })} />)
    expect(offer()).toBeNull()
  })

  it('is absent when someone else took control over the person, which is not a return', () => {
    const { rerender } = render(<Pane tenant={PERSONAL} s={session({ controller: held(ME) })} />)
    rerender(<Pane tenant={PERSONAL} s={session({ controller: held(OTHER) })} />)
    expect(offer()).toBeNull()
  })
})
