// THE BROWSER TAB (docs/design/live-browser.md §3.4, §5.2, §5.6, §5.7, §6,
// §6.1; lane L11's acceptance row).
//
//   * the Browser tab follows Details, and is open, for a running browser agent;
//   * the viewer ticket goes in the websocket's first message, never its URL;
//   * no Take control for a viewer the server says is not eligible;
//   * a viewer's device switcher is disabled, and the controller's works
//     (LB-DEV-B): a preset by name, rotate and touch through setLiveDevice;
//   * a page off the hand-off's named site turns the strip red, and Take
//     control asks twice;
//   * keys reach the agent's browser only while its canvas has focus, and the
//     pointer from the moment this person holds control (LB-CURSOR2);
//   * a control refusal from the relay is said under the canvas;
//   * touch reaches it only in control, with touch on.
//
// noVNC is replaced by `FakeRfb` and the socket by `FakeSocket`: jsdom has no
// canvas to draw and this suite reaches no network. What is asserted is what
// LiveBrowser.tsx does with them -- the URL it opens, the first frame it
// sends, the `viewOnly` it sets, the events it lets through to noVNC's canvas.
//
// MUTATIONS, one per block: put the ticket in the socket URL's query; drop
// the `can_control` check in LiveControl; enable the device chips for every
// viewer (or decide it from `controller` instead of `you.can_switch_device`);
// send a size instead of a preset name; skip the second ask on a mismatch; gate `viewOnly` on canvas
// focus again (moves before the first click are then dropped, LB-CURSOR2); let touches through whenever in control; await
// anything between the socket's 'open' and noVNC's construction (the server's
// first frame is then lost); construct noVNC outside the retry.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
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
  mintLiveTicket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
  closeLiveToMembers: vi.fn(),
  setLiveDevice: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

/** noVNC's RFB, as far as LiveBrowser.tsx touches it. */
const rfb = vi.hoisted(() => {
  class FakeRfb {
    static made: FakeRfb[] = []
    /**
     * KeyEvents noVNC sent (`sendKey`). Like noVNC 1.4.0's Keyboard, keys are heard only on
     * its own canvas -- so only while that canvas has DOM focus -- and sent only while connected
     * and not view-only.
     */
    readonly sentKeys: [string, boolean][] = []
    private state: 'connecting' | 'connected' | 'disconnected' = 'connecting'
    private only = true
    get viewOnly() {
      return this.only
    }
    set viewOnly(v: boolean) {
      this.only = v
    }
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    showDotCursor = false
    // noVNC 1.4.0's own defaults.
    qualityLevel = 6
    compressionLevel = 2
    readonly canvas: HTMLCanvasElement
    /** Touches that reached noVNC's own canvas listener. */
    touches = 0
    /** How many frames the raw socket had carried when noVNC was handed the channel. */
    readonly sentBeforeAttach: number
    /** Server frames noVNC read: like noVNC's Websock.attach, only through `onmessage`, set here. */
    readonly received: unknown[] = []
    /** Set to make the next construction throw, as noVNC's does on an already-closed socket. */
    static throwNext = false
    private listeners = new Map<string, ((e: Event) => void)[]>()
    constructor(
      target: HTMLElement,
      readonly channel: { readyState: number; onmessage: ((e: { data: unknown }) => void) | null },
    ) {
      if (FakeRfb.throwNext || channel.readyState !== 1) {
        FakeRfb.throwNext = false
        throw new Error('Cannot use already closed WebSocket/RTCDataChannel')
      }
      channel.onmessage = (e) => this.received.push(e.data)
      this.sentBeforeAttach = FakeSocket.made.at(-1)!.sent.length
      this.canvas = document.createElement('canvas')
      this.canvas.tabIndex = -1
      this.canvas.addEventListener('touchstart', () => {
        this.touches += 1
      })
      // noVNC's `_focusCanvas` and `_handleMouse`: a press on its canvas focuses it (with
      // `focusOnClick`), and is stopped there, so nothing above the canvas hears it bubble.
      this.canvas.addEventListener('mousedown', (e) => {
        if (this.focusOnClick) this.canvas.focus({ preventScroll: true })
        e.stopPropagation()
        e.preventDefault()
      })
      const key = (down: boolean) => (e: KeyboardEvent) => {
        if (this.state === 'connected' && !this.only) this.sentKeys.push([e.key, down])
      }
      this.canvas.addEventListener('keydown', key(true))
      this.canvas.addEventListener('keyup', key(false))
      target.appendChild(this.canvas)
      FakeRfb.made.push(this)
    }
    addEventListener(type: string, fn: (e: Event) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn])
    }
    emit(type: string) {
      if (type === 'connect') this.state = 'connected'
      if (type === 'disconnect') {
        // noVNC's `_disconnect` takes its screen out of the target before it says so.
        this.state = 'disconnected'
        this.canvas.remove()
      }
      for (const fn of this.listeners.get(type) ?? []) fn(new Event(type))
    }
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

class FakeSocket extends EventTarget {
  static made: FakeSocket[] = []
  /**
   * The frames that arrive the moment the socket is open, in the same task: swarm-api's
   * status (TEXT), then the RFB server's ProtocolVersion (binary), which speaks first.
   */
  static greeting: (string | ArrayBuffer)[] = []
  binaryType = 'blob'
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  readonly sent: string[] = []
  constructor(readonly url: string) {
    super()
    FakeSocket.made.push(this)
    queueMicrotask(() => {
      this.readyState = 1
      // A browser WebSocket fires both its `onopen` property and its listeners.
      const opened = new Event('open')
      this.onopen?.(opened)
      this.dispatchEvent(opened)
      for (const data of FakeSocket.greeting) this.onmessage?.(new MessageEvent('message', { data }))
    })
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.readyState = 3
  }
}

const { AgentSplit } = await import('../AgentSplit')
const { LiveBrowserPane, LiveControl, crispAt, liveReconnectDelay, LIVE_RESTART_CODE } = await import('../LiveBrowser')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef0456'
const ME = ['ada', 'example.com'].join('@')
const OTHER = ['grace', 'example.com'].join('@')

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

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_one',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'iphone-15', orientation: 'portrait', touch: false },
    presets: [
      { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
      { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
    ],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
    ...over,
  }
}

/** A session this person is in control of: the server lets them switch the device. */
function mine(over: Partial<LiveSession> = {}): LiveSession {
  return session({
    controller: { email: ME, since: new Date().toISOString() },
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  })
}

/** A session someone else controls, seen by a member who may not take it. */
function viewer(over: Partial<LiveSession> = {}): LiveSession {
  return session({
    controller: { email: OTHER, since: new Date().toISOString() },
    you: { email: ME, can_control: false, can_preempt: false, can_open: false, can_switch_device: false },
    ...over,
  })
}

/** The chips, rotate and touch sit in the toolbar's Device menu (BT-003): opened as a person opens it. */
function openDevice() {
  const menu = screen.queryByRole('button', { name: /^Device:/ })
  if (menu !== null && menu.getAttribute('aria-expanded') !== 'true') fireEvent.click(menu)
}

function pane(s: LiveSession, reload: () => void = () => {}) {
  const view = render(
    <LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: s }} reload={reload} />,
  )
  openDevice()
  return view
}

/** The noVNC instance the pane connected, once the ticket and the socket are through. */
async function connected(): Promise<InstanceType<typeof rfb.FakeRfb>> {
  const r = await waitFor(() => {
    const made = rfb.FakeRfb.made.at(-1)
    expect(made, 'noVNC was never attached').toBeTruthy()
    return made!
  })
  act(() => r.emit('connect'))
  return r
}

function Routed({ onGo }: { onGo: (to: string) => void }) {
  const [p, setP] = useState<'detail' | 'logs' | 'browser'>('detail')
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane={p}
        artifact={null}
        closeTo="work/running/live"
        go={(to) => {
          onGo(to)
          setP(to.endsWith('/logs') ? 'logs' : to.endsWith('/browser') ? 'browser' : 'detail')
        }}
        base={`work/task/${ID}`}
      />
    </div>
  )
}

function tabs(): HTMLElement[] {
  return within(screen.getByRole('tablist', { name: 'Agent panes' })).getAllByRole('tab')
}

function label(t: HTMLElement): string {
  return t.querySelector('.c-tab-label')?.textContent ?? t.textContent ?? ''
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  rfb.FakeRfb.made = []
  rfb.FakeRfb.throwNext = false
  FakeSocket.made = []
  FakeSocket.greeting = []
  vi.stubGlobal('WebSocket', FakeSocket)
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  // Built at run time: nothing here may look like a credential in the source.
  api.mintLiveTicket.mockImplementation(async () => ok({ ticket: `vt.${crypto.randomUUID()}`, session_id: 'ls_one', expires_in: 30, socket_path: '/v1/live/ls_one/stream' }))
  api.takeLiveControl.mockResolvedValue(ok(null))
  api.returnLiveControl.mockResolvedValue(ok(null))
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.openLiveToMembers.mockResolvedValue(ok(null))
  api.setLiveDevice.mockResolvedValue(ok(null))
})

afterEach(() => {
  delete document.documentElement.dataset.agentList
})

describe('the Browser tab follows Details for a running browser agent', () => {
  it('opens a running live-browser agent on its Browser tab, the second tab, and does not send it to Logs', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
    const went: string[] = []
    render(<Routed onGo={(to) => went.push(to)} />)
    // The file's first render pays the split's cold start: 1.5 s here, and
    // past waitFor's 1 s default on a loaded CI shard (PR #2098's red run read
    // `Logs` at tabs()[1] before the live-session read had answered).
    await waitFor(() => expect(label(tabs()[1]!)).toBe('Browser'), { timeout: 10_000 })
    await waitFor(() => expect(tabs()[1]!.getAttribute('aria-selected')).toBe('true'), { timeout: 10_000 })
    expect(went.filter((to) => to.endsWith('/logs'))).toEqual([])
    expect(document.querySelector('.live-strip')).not.toBeNull()
  })

  it('draws no Browser tab for a running agent whose profile is not a live browser, and keeps it on Details (#1646)', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ runner_profile: 'claude-code', resource_class: 'standard' })))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: false, session: null }))
    const went: string[] = []
    render(<Routed onGo={(to) => went.push(to)} />)
    // Details is drawn only once the opening pane is decided (opening.ts).
    await waitFor(() => expect(api.loadAgentRun).toHaveBeenCalled())
    expect(went).toEqual([])
    expect(tabs()[0]!.getAttribute('aria-selected')).toBe('true')
    expect(tabs().map(label)).not.toContain('Browser')
  })

  // A FINISHED BROWSER AGENT KEEPS ITS TAB (BROWSER-TAB-TABS, BT-007, #2423,
  // 2026-10-26): the tab was drawn only on `/browser`, so leaving it took
  // Browser off the strip and out of More, with no way back but the address.
  // The runner names a browser agent; the session is still not read here.
  it('does not ask a finished agent for a live session at all, and keeps its Browser tab, second', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ state: 'SUCCEEDED', completed_at: new Date().toISOString() })))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(label(tabs()[0]!)).toBe('Details'))
    expect(api.loadLiveSession).not.toHaveBeenCalled()
    // MUTATION: draw the tab only on `/browser` (or while the session is read) and it is gone here.
    expect(label(tabs()[1]!)).toBe('Browser')
    expect(tabs()[0]!.getAttribute('aria-selected')).toBe('true')
  })
})

describe('the session read exactly as swarm-api serves it (tests/fixtures/live, LB-SEAM)', () => {
  // The shared fixture, not a hand-built session: origin and device are null
  // there because nothing reports them to swarm-api yet, and the tab must
  // draw that rather than throw on `session.device.touch`.
  const served = (file: string): LiveRead =>
    JSON.parse(readFileSync(join(__dirname, '..', '..', '..', '..', 'tests', 'fixtures', 'live', file), 'utf8')) as LiveRead

  it('opens on a Browser tab, shows the device as not reported, and offers Take control', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok(served('session.open.json')))
    api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
    render(<Routed onGo={() => {}} />)
    // The live-session read answers after the split's cold start, which on a
    // loaded run of many files passes waitFor's 1 s default (as above).
    await waitFor(() => expect(label(tabs()[1]!)).toBe('Browser'), { timeout: 10_000 })
    await waitFor(() => expect(document.querySelector('.live-caption')?.textContent).toMatch(/^Device not reported/), {
      timeout: 10_000,
    })
    expect(screen.getAllByRole('button', { name: /Take control/ }).length).toBeGreaterThan(0)
  })

  it('a live-browser task whose tunnel is not up yet still has its Browser tab', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok(served('session.pending.json')))
    render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(tabs().map(label)).toContain('Browser'), { timeout: 10_000 })
  })
})

describe('the ticket goes in the first message, never the URL', () => {
  it('opens /v1/live/<session>/stream with nothing else in the URL and sends the ticket as the first frame, before noVNC has the socket', async () => {
    const ticket = `vt.${crypto.randomUUID()}`
    api.mintLiveTicket.mockResolvedValue(ok({ ticket, session_id: 'ls_one', expires_in: 30, socket_path: '/v1/live/ls_one/stream' }))
    pane(session())
    const r = await connected()
    const ws = FakeSocket.made.at(-1)!
    expect(ws.url).toMatch(new RegExp(`^wss?://[^/?#]*/v1/live/ls_one/stream$`))
    expect(ws.url).not.toContain(ticket)
    expect(ws.sent[0]).toBe(JSON.stringify({ ticket }))
    expect(r.sentBeforeAttach, 'noVNC was handed the socket before the ticket went').toBe(1)
    expect(r.channel, 'noVNC was handed the raw socket, status text frames and all').not.toBe(ws)
    expect(r.channel.readyState).toBe(1)
  })

  it('turns on noVNC\'s dot cursor, so a pointer shows even when the gate sends no cursor shape (LB-CURSOR)', async () => {
    // Measured 2026-10-12: the canvas had `cursor: none`, so the person in control clicked blind.
    pane(session())
    const r = await connected()
    expect(r.showDotCursor).toBe(true)
  })

  it('asks the gate for a lossless picture at the zlib level the gate was measured at (STREAM-SMOOTH)', async () => {
    // The gate never sends JPEG (livegate/rfb.py); quality 9 says so to noVNC too, and
    // compression 1 is rfb.py ZLIB_LEVEL, chosen from livegate.bench.
    pane(session())
    const r = await connected()
    expect(r.qualityLevel).toBe(9)
    expect(r.compressionLevel).toBe(1)
  })

  it('draws the frame pixel for pixel at a whole scale, and smooths it at a fractional fit (STREAM-SMOOTH)', () => {
    expect(crispAt(1440, 1440, 1)).toBe(true) // 1:1
    expect(crispAt(1440, 1440, 2)).toBe(true) // 1:1 on a 2x screen: 2x2 device pixels each
    expect(crispAt(786, 393, 1)).toBe(true) // a phone drawn at twice its size
    expect(crispAt(1000, 1440, 1)).toBe(false) // fitted down: nearest-neighbour would drop rows of text
    expect(crispAt(1000, 1440, 1.5)).toBe(false)
    expect(crispAt(0, 1440, 1)).toBe(false) // not laid out yet
  })

  it('has noVNC reading the socket before the relay\'s first frame can arrive', async () => {
    const version = new TextEncoder().encode('RFB 003.008\n').buffer as ArrayBuffer
    FakeSocket.greeting = [JSON.stringify({ type: 'status', open: true, in_control: false, controller: null }), version]
    pane(session())
    const r = await connected()
    expect(r.received, 'noVNC read swarm-api\'s status as RFB, or the ProtocolVersion went past it').toEqual([version])
  })

  it('re-reads the session when swarm-api\'s status frames say control changed, and only then', async () => {
    const status = (in_control: boolean, controller: string | null) =>
      new MessageEvent('message', { data: JSON.stringify({ type: 'status', open: true, in_control, controller }) })
    const reload = vi.fn()
    pane(session(), reload)
    const r = await connected()
    const ws = FakeSocket.made.at(-1)!
    act(() => ws.onmessage?.(status(false, null)))
    act(() => ws.onmessage?.(status(false, null)))
    expect(reload, 'the first status is the state the session read already drew').not.toHaveBeenCalled()
    act(() => ws.onmessage?.(status(true, ['ada', 'example.com'].join('@'))))
    expect(reload).toHaveBeenCalledTimes(1)
    expect(r.received, 'a status frame reached noVNC').toEqual([])
  })

  it('retries, with a message, when noVNC refuses the socket', async () => {
    rfb.FakeRfb.throwNext = true
    pane(session())
    await waitFor(() => expect(FakeSocket.made.length).toBe(1))
    // Nothing has drawn yet, so the reason is said in the frame (QA2-FIX-16, ovag2-12).
    await waitFor(() => expect(document.querySelector('.live-frame-empty')?.textContent).toContain('already closed'))
    expect(rfb.FakeRfb.made).toHaveLength(0)
    expect(FakeSocket.made[0]!.readyState, 'the refused socket was left open').toBe(3)
  })
})

describe('no Take control for a non-eligible viewer', () => {
  it('offers Take control to a person the server says may control', () => {
    render(<LiveControl taskId={ID} session={session()} reload={() => {}} />)
    expect(screen.getByRole('button', { name: /take control/i })).toBeTruthy()
  })

  it('offers none to a member control was not opened to, and says who holds it', () => {
    render(<LiveControl taskId={ID} session={viewer()} reload={() => {}} />)
    expect(screen.queryByRole('button', { name: /take control/i })).toBeNull()
    expect(document.body.textContent).toContain(`${OTHER} is in control`)
  })

  it('offers none over someone else’s control to a person who may not preempt', () => {
    const s = session({
      controller: { email: OTHER, since: '' },
      you: { email: ME, can_control: true, can_preempt: false, can_open: false, can_switch_device: false },
    })
    render(<LiveControl taskId={ID} session={s} reload={() => {}} />)
    expect(screen.queryByRole('button', { name: /take control/i })).toBeNull()
  })

  it('offers Return control, not Take control, to the person in control', () => {
    render(<LiveControl taskId={ID} session={mine()} reload={() => {}} />)
    expect(screen.queryByRole('button', { name: /take control/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Return control' }))
    expect(api.returnLiveControl).toHaveBeenCalledWith(ID)
  })
})

describe('a viewer’s device switcher is disabled', () => {
  it('shows a viewer the current device, disabled, with rotate and touch disabled too', () => {
    pane(viewer())
    const chip = screen.getByRole('button', { name: 'iPhone 15' }) as HTMLButtonElement
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.disabled).toBe(true)
    expect((screen.getByRole('button', { name: /rotate/i }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /touch/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  // LB-V1 (visual QA 5, U3): the why is the switcher's tooltip and accessible
  // description, no longer a toolbar line that pushed ⤢ Theater to a second row.
  it('says why, on the switcher, as its tooltip and its description', () => {
    pane(viewer())
    const chip = screen.getByRole('button', { name: 'iPhone 15' }) as HTMLButtonElement
    expect(chip.title).toMatch(/only the person in control/i)
    expect(document.getElementById(chip.getAttribute('aria-describedby') ?? '')?.textContent).toMatch(/only the person in control/i)
    expect(document.querySelector('.live-toolbar-why')).toBeNull()
  })

  it('follows the server, not the controller field: in control but not allowed to switch is disabled', () => {
    pane(mine({ you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false } }))
    expect((screen.getByRole('button', { name: 'iPhone 15' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /rotate/i }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('the person in control switches the device (LB-DEV-B)', () => {
  it('enables the switcher, rotate and touch, with nothing said against them', () => {
    pane(mine())
    const chip = screen.getByRole('button', { name: 'iPhone 15' }) as HTMLButtonElement
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.disabled).toBe(false)
    expect(chip.title).toBe('')
    expect((screen.getByRole('button', { name: /rotate/i }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: /touch/i }) as HTMLButtonElement).disabled).toBe(false)
    expect(document.querySelector('.live-toolbar-why')).toBeNull()
  })

  it('a preset is sent by name, never a size, and the session is read again', async () => {
    const reload = vi.fn()
    render(<LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: mine() }} reload={reload} />)
    openDevice()
    fireEvent.click(screen.getByRole('button', { name: 'Desktop' }))
    expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'desktop' })
    await waitFor(() => expect(reload).toHaveBeenCalled())
  })

  it('rotate asks for the same preset in the other orientation', () => {
    pane(mine())
    fireEvent.click(screen.getByRole('button', { name: /rotate/i }))
    expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'iphone-15', orientation: 'landscape' })
  })

  it('touch turns the toggle over, on the same preset and orientation', () => {
    pane(mine())
    fireEvent.click(screen.getByRole('button', { name: /touch: off/i }))
    expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'iphone-15', orientation: 'portrait', touch: true })
  })

  it('a preset with no touch offers no touch toggle', () => {
    pane(mine({ device: { preset: 'desktop', orientation: 'landscape', touch: false } }))
    const touch = screen.getByRole('button', { name: /touch/i }) as HTMLButtonElement
    expect(touch.disabled).toBe(true)
    expect(touch.title).toMatch(/no touch/i)
    expect((screen.getByRole('button', { name: /rotate/i }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('a refusal is shown, not swallowed', async () => {
    api.setLiveDevice.mockResolvedValue({
      status: 'error',
      error: { kind: 'conflict', httpStatus: 409, code: 'not_controller', message: 'you are not in control of this browser' },
    })
    pane(mine())
    fireEvent.click(screen.getByRole('button', { name: /rotate/i }))
    expect((await screen.findByRole('alert')).textContent).toContain('not in control')
  })
})

describe('the submitter opens control to members through the served route', () => {
  // Both ways since the owner's decision of 2026-10-13 (QA12-11): the confirm
  // and the carry-over are held by live.tab.test.tsx.
  it('posts open-to-members once the box is ticked and the confirm said yes', () => {
    pane(mine())
    const box = screen.getByRole('checkbox', { name: /let any member take control/i }) as HTMLInputElement
    expect(box.disabled).toBe(false)
    fireEvent.click(box)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Let any member take control' }))
    expect(api.openLiveToMembers).toHaveBeenCalledWith(ID)
  })

  it('keeps the box enabled once control is open to members, so it can be turned off', () => {
    pane(mine({ open_to_members: true }))
    const box = screen.getByRole('checkbox', { name: /let any member take control/i }) as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(box.disabled).toBe(false)
  })
})

describe('a mismatched origin turns the strip red and asks twice', () => {
  const wait = (expect: string | null) => ({
    reason: 'Sign in to GitHub as the bot account',
    expect,
    requested_at: new Date().toISOString(),
    deadline: new Date(Date.now() + 600_000).toISOString(),
  })
  const phish = {
    origin: 'https://github-login.example',
    host: 'github-login.example',
    registrable_domain: 'github-login.example',
    scheme: 'https:',
  }

  it('paints the strip red, naming both sites, when the page is not on the site the hand-off named', () => {
    pane(session({ human_wait: wait('github.com'), origin: phish }))
    const strip = document.querySelector<HTMLElement>('.live-strip')!
    expect(strip.dataset.tone).toBe('mismatch')
    expect(strip.classList.contains('is-mismatch')).toBe(true)
    expect(strip.textContent).toContain('github-login.example')
    expect(strip.textContent).toContain('github.com')
  })

  it('keeps the strip out of red on the named site, and on a subdomain of it', () => {
    const { unmount } = pane(session({ human_wait: wait('github.com') }))
    expect(document.querySelector<HTMLElement>('.live-strip')!.dataset.tone).toBe('ok')
    unmount()
    pane(
      session({
        human_wait: wait('github.com'),
        origin: { origin: 'https://gist.github.com', host: 'gist.github.com', registrable_domain: 'github.com', scheme: 'https:' },
      }),
    )
    expect(document.querySelector<HTMLElement>('.live-strip')!.dataset.tone).toBe('ok')
  })

  it('asks a second time, naming both domains, and takes control only on that second yes', async () => {
    render(<LiveControl taskId={ID} session={session({ human_wait: wait('github.com'), origin: phish })} reload={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    const first = screen.getByRole('dialog')
    fireEvent.click(within(first).getByRole('button', { name: 'Take control' }))
    expect(api.takeLiveControl).not.toHaveBeenCalled()
    const second = screen.getByRole('alertdialog')
    expect(second.textContent).toContain('github-login.example')
    expect(second.textContent).toContain('github.com')
    fireEvent.click(within(second).getByRole('button', { name: /take control on github-login\.example/i }))
    await waitFor(() =>
      expect(api.takeLiveControl).toHaveBeenCalledWith(ID, {
        confirmed_mismatch: { expected: 'github.com', actual: 'github-login.example' },
      }),
    )
  })

  it('one take is one request: the header opens the ask, the ask posts once, however often it is pressed (LB-R4)', async () => {
    let settle: (v: unknown) => void = () => {}
    api.takeLiveControl.mockReturnValue(new Promise((r) => (settle = r)))
    render(<LiveControl taskId={ID} session={session({ human_wait: wait('github.com') })} reload={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    expect(api.takeLiveControl).not.toHaveBeenCalled()
    const yes = within(screen.getByRole('dialog')).getByRole('button', { name: 'Take control' })
    fireEvent.click(yes)
    fireEvent.click(yes)
    // The header button again while the first request is in flight.
    fireEvent.click(screen.getByRole('button', { name: /Take control|Taking control/ }))
    const again = screen.queryByRole('dialog')
    if (again !== null) fireEvent.click(within(again).getByRole('button', { name: 'Take control' }))
    expect(api.takeLiveControl).toHaveBeenCalledTimes(1)
    await act(async () => settle(ok(null)))
    expect(api.takeLiveControl).toHaveBeenCalledTimes(1)
  })

  it('asks once on the named site', async () => {
    render(<LiveControl taskId={ID} session={session({ human_wait: wait('github.com') })} reload={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Take control' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    await waitFor(() => expect(api.takeLiveControl).toHaveBeenCalledWith(ID, {}))
  })
})

describe('keys are captured only with canvas focus', () => {
  it('opens input for the person in control before any click, so a move reaches the agent’s page (LB-CURSOR2)', async () => {
    // MEASURED 2026-10-13: 20 moves, a click and two keys on the canvas reached the gate as two
    // pointer events. noVNC sends no move while view-only, and it was view-only until a click focused it.
    pane(mine())
    const r = await connected()
    expect(r.viewOnly, 'a move before the first click is dropped by noVNC').toBe(false)
    act(() => r.canvas.focus())
    expect(r.viewOnly).toBe(false)
    act(() => r.canvas.blur())
    expect(r.viewOnly, 'clicking outside the canvas does not take the pointer back').toBe(false)
  })

  it('says under the canvas when the relay refused this connection’s control, with its cause (LB-CURSOR2)', async () => {
    const reload = vi.fn()
    pane(mine(), reload)
    await connected()
    const raw = FakeSocket.made.at(-1)!
    expect(screen.queryByText(/refused this connection’s control/)).toBeNull()
    act(() =>
      raw.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'refused', reason: 'control', cause: 'wrong_session' }) })),
    )
    expect(screen.getByText(/The relay refused this connection’s control \(wrong_session\)/)).toBeTruthy()
  })

  it('keeps a viewer’s connection view-only even with the canvas focused', async () => {
    pane(viewer())
    const r = await connected()
    act(() => r.canvas.focus())
    expect(r.viewOnly).toBe(true)
  })

  it('keeps a key typed into the focused canvas from the console’s own shortcuts, and leaves them alone otherwise', async () => {
    const seen: string[] = []
    const onKey = (e: KeyboardEvent) => seen.push(e.key)
    window.addEventListener('keydown', onKey)
    try {
      pane(mine())
      const r = await connected()
      act(() => r.canvas.focus())
      fireEvent.keyDown(r.canvas, { key: 'Escape' })
      expect(seen).toEqual([])
      act(() => r.canvas.blur())
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(seen).toEqual(['Escape'])
    } finally {
      window.removeEventListener('keydown', onKey)
    }
  })
})

// KEYS-FOCUS (part of #1030). MEASURED 2026-10-14 on task_84cb2038aa704c829d48 generation 5:
// the owner took control, switched iphone-15 -> desktop twice and typed into the form; the gate's
// summary for that connection read keys=0 pointer=87 and the agent read the field back empty.
// noVNC hears keys only on its own canvas, so a key typed while a device chip, the theater or a
// canvas noVNC has since replaced holds focus never becomes a KeyEvent -- and the line under the
// canvas went on saying the keys went to the agent's browser. The gate side of the same path is
// tests/unit/worker/test_live_rfb.py::test_the_controllers_keys_are_injected_and_counted_after_a_device_switch.
describe('every key typed in control reaches the agent’s page (KEYS-FOCUS)', () => {
  /** A press with the mouse: down, then up, where the browser would deliver them. */
  const press = (el: Element) => {
    fireEvent.mouseDown(el)
    fireEvent.mouseUp(el)
  }
  /** A key, typed the way a browser delivers one: to whatever holds focus. */
  const type = (key: string) => {
    const at = document.activeElement ?? document.body
    fireEvent.keyDown(at, { key })
    fireEvent.keyUp(at, { key })
  }
  /** A button pressed with the mouse: the browser focuses it, then clicks it. */
  const clickButton = (b: HTMLElement) => {
    act(() => b.focus())
    fireEvent.click(b)
  }
  const both = (key: string): [string, boolean][] => [
    [key, true],
    [key, false],
  ]
  const at = (preset: string) =>
    mine({ device: { preset, orientation: preset === 'desktop' ? 'landscape' : 'portrait', touch: false } })
  const live = (s: LiveSession) => <LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />

  it('sends the keys typed after a device switch, with no second click on the canvas', async () => {
    const view = render(live(at('iphone-15')))
    const r = await connected()
    press(r.canvas)
    type('a')
    expect(r.sentKeys).toEqual(both('a'))

    for (const [chip, preset, key] of [['Desktop', 'desktop', 'b'], ['iPhone 15', 'iphone-15', 'c']] as const) {
      api.setLiveDevice.mockClear()
      openDevice()
      clickButton(screen.getByRole('button', { name: chip }))
      await waitFor(() => expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset }))
      // The re-read after the switch: the gate applied the preset and the canvas is redrawn to it.
      view.rerender(live(at(preset)))
      await waitFor(() => expect(document.activeElement, `focus stayed on the ${chip} chip`).toBe(r.canvas))
      type(key)
    }
    expect(r.sentKeys).toEqual([...both('a'), ...both('b'), ...both('c')])
    expect(rfb.FakeRfb.made, 'a device switch is not a reconnect').toHaveLength(1)
    expect(screen.getByText('Your keys go to the agent’s browser. Click outside it to use the console.')).toBeTruthy()
  })

  it('gives the new canvas the keys after a reconnect, rather than saying they go to the one noVNC removed', async () => {
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: mine() }))
    pane(mine())
    const first = await connected()
    press(first.canvas)
    type('a')
    expect(first.sentKeys).toEqual(both('a'))

    act(() => first.emit('disconnect'))
    const second = await waitFor(
      () => {
        expect(rfb.FakeRfb.made).toHaveLength(2)
        return rfb.FakeRfb.made[1]!
      },
      { timeout: 4000 },
    )
    act(() => second.emit('connect'))
    expect(document.activeElement, 'the keys were left on a canvas that is no longer in the page').toBe(second.canvas)
    type('b')
    expect(second.sentKeys).toEqual(both('b'))
  }, 10_000)

  it('sends keys in the theater and back in the tab, and says “Click to type” wherever the canvas has no focus', async () => {
    pane(mine())
    const r = await connected()
    const hint = () => screen.queryByText('Click to type')
    expect(hint(), 'in control with no focus on the canvas, and no hint').not.toBeNull()
    press(r.canvas)
    expect(hint()).toBeNull()

    clickButton(screen.getByRole('button', { name: 'Open theater (T)' }))
    const theater = screen.getByRole('dialog', { name: /^Theater/ })
    expect(theater.contains(r.canvas)).toBe(true)
    expect(hint(), 'the theater took focus and the hint did not say so').not.toBeNull()
    expect(theater.contains(hint())).toBe(true)
    press(r.canvas)
    type('a')
    expect(r.sentKeys).toEqual(both('a'))
    expect(hint()).toBeNull()

    clickButton(within(theater).getByRole('button', { name: /back to tab/ }))
    expect(screen.queryByRole('dialog', { name: /^Theater/ })).toBeNull()
    expect(document.querySelector('.live-body')!.contains(r.canvas)).toBe(true)
    expect(hint(), 'back in the tab with no focus on the canvas, and no hint').not.toBeNull()
    expect(screen.queryByText(/Your keys go to the agent’s browser/)).toBeNull()
    press(r.canvas)
    type('b')
    expect(r.sentKeys).toEqual([...both('a'), ...both('b')])
  })

  it('takes the keys for a click anywhere on the screen, not only on the drawn canvas', async () => {
    pane(mine())
    const r = await connected()
    // The screen around a canvas scaled to fit (a letterbox, the frame's edge): noVNC hears no press there.
    const screenNode = document.querySelector<HTMLElement>('.live-screen')!
    press(screenNode)
    expect(document.activeElement).toBe(r.canvas)
    type('a')
    expect(r.sentKeys).toEqual(both('a'))
  })

  it('sends no key for a viewer, and offers a viewer no “Click to type”', async () => {
    pane(viewer())
    const r = await connected()
    press(r.canvas)
    type('a')
    expect(r.sentKeys).toEqual([])
    expect(screen.queryByText('Click to type')).toBeNull()
  })
})

describe('touch events are sent only in control with touch on', () => {
  async function touchesFrom(s: LiveSession): Promise<number> {
    pane(s)
    const r = await connected()
    fireEvent.touchStart(r.canvas, { touches: [{ clientX: 10, clientY: 10 }] })
    return r.touches
  }

  it('lets a touch through to noVNC for the person in control with touch on', async () => {
    expect(await touchesFrom(mine({ device: { preset: 'iphone-15', orientation: 'portrait', touch: true } }))).toBe(1)
  })

  it('stops it for the person in control with touch off', async () => {
    expect(await touchesFrom(mine())).toBe(0)
  })

  it('stops it for a viewer, even with touch on', async () => {
    expect(await touchesFrom(viewer({ device: { preset: 'iphone-15', orientation: 'portrait', touch: true } }))).toBe(0)
  })

})

// RELAY-ROLL (part of #1030). MEASURED 2026-10-12/14: the relay restarted 40
// times in ~48 h, once per release, and every viewer waited the 2 s, 4 s, 8 s
// ... of an ordinary drop before it rejoined. A close with 1012 ("restarting,
// reconnect now") is rejoined at once. MUTATION: drop the `restart` branch in
// `liveReconnectDelay`, or stop reading the close code: the first block's
// reconnect then waits 2 s and the test times out.
describe('a relay restart is rejoined at once', () => {
  /** Close the connected socket with `code`, as swarm-api does, then let noVNC say so. */
  function drop(r: InstanceType<typeof rfb.FakeRfb>, code: number) {
    const raw = FakeSocket.made.at(-1) as unknown as { onclose: ((e: CloseEvent) => void) | null }
    act(() => {
      raw.onclose?.({ code } as CloseEvent)
      r.emit('disconnect')
    })
  }

  beforeEach(() => {
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
  })

  it('reconnects in well under a second when the relay closed with 1012', async () => {
    pane(session())
    const r = await connected()
    expect(api.mintLiveTicket).toHaveBeenCalledTimes(1)
    drop(r, LIVE_RESTART_CODE)
    await waitFor(() => expect(api.mintLiveTicket).toHaveBeenCalledTimes(2), { timeout: 900 })
    await waitFor(() => expect(FakeSocket.made).toHaveLength(2))
    expect(document.querySelector('.live-screen-say')?.textContent ?? '').not.toContain('failed')
  })

  it('keeps the ordinary backoff for any other close', async () => {
    pane(session())
    const r = await connected()
    drop(r, 1006)
    await new Promise((done) => setTimeout(done, 900))
    expect(api.mintLiveTicket, 'an ordinary drop reconnected at the restart pace').toHaveBeenCalledTimes(1)
  })

  it('starts at ~250 ms, jittered, and never waits past 4 s while the relay comes back', () => {
    expect(LIVE_RESTART_CODE).toBe(1012)
    for (const rand of [0, 0.5, 0.999]) {
      const first = liveReconnectDelay(1, true, () => rand)
      expect(first).toBeGreaterThanOrEqual(150)
      expect(first).toBeLessThan(1000)
    }
    expect(liveReconnectDelay(3, true, () => 0)).toBeLessThan(liveReconnectDelay(3, true, () => 0.999))
    expect(Math.max(...[1, 2, 4, 8, 20].map((n) => liveReconnectDelay(n, true, () => 0.999)))).toBe(4000)
    expect(liveReconnectDelay(1, false)).toBe(2000)
    expect(liveReconnectDelay(9, false)).toBe(30_000)
  })
})
