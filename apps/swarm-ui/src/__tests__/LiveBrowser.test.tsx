// THE BROWSER TAB (docs/design/live-browser.md §3.4, §5.2, §5.6, §5.7, §6,
// §6.1; lane L11's acceptance row).
//
//   * the Browser tab is first, and open, for a running browser agent;
//   * the viewer ticket goes in the websocket's first message, never its URL;
//   * no Take control for a viewer the server says is not eligible;
//   * a viewer's device switcher is disabled;
//   * a page off the hand-off's named site turns the strip red, and Take
//     control asks twice;
//   * keys reach the agent's browser only while its canvas has focus;
//   * touch reaches it only in control, with touch on.
//
// noVNC is replaced by `FakeRfb` and the socket by `FakeSocket`: jsdom has no
// canvas to draw and this suite reaches no network. What is asserted is what
// LiveBrowser.tsx does with them -- the URL it opens, the first frame it
// sends, the `viewOnly` it sets, the events it lets through to noVNC's canvas.
//
// MUTATIONS, one per block: put the ticket in the socket URL's query; drop
// the `can_control` check in LiveControl; enable the device select for every
// viewer; skip the second ask on a mismatch; set `viewOnly` from control
// alone, ignoring focus; let touches through whenever in control.

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
  setLiveDevice: vi.fn(),
  setLiveOpenToMembers: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

/** noVNC's RFB, as far as LiveBrowser.tsx touches it. */
const rfb = vi.hoisted(() => {
  class FakeRfb {
    static made: FakeRfb[] = []
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    readonly canvas: HTMLCanvasElement
    /** Touches that reached noVNC's own canvas listener. */
    touches = 0
    /** How many frames the socket had carried when noVNC was handed it. */
    readonly sentBeforeAttach: number
    private listeners = new Map<string, ((e: Event) => void)[]>()
    constructor(target: HTMLElement, readonly channel: { sent: string[] }) {
      this.sentBeforeAttach = channel.sent.length
      this.canvas = document.createElement('canvas')
      this.canvas.tabIndex = -1
      this.canvas.addEventListener('touchstart', () => {
        this.touches += 1
      })
      target.appendChild(this.canvas)
      FakeRfb.made.push(this)
    }
    addEventListener(type: string, fn: (e: Event) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn])
    }
    emit(type: string) {
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
  binaryType = 'blob'
  readonly sent: string[] = []
  constructor(readonly url: string) {
    super()
    FakeSocket.made.push(this)
    queueMicrotask(() => this.dispatchEvent(new Event('open')))
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {}
}

const { AgentSplit } = await import('../AgentSplit')
const { LiveBrowserPane, LiveControl } = await import('../LiveBrowser')
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
    you: { email: ME, can_control: true, can_preempt: true, can_open: true },
    ...over,
  }
}

/** A session this person is in control of. */
function mine(over: Partial<LiveSession> = {}): LiveSession {
  return session({ controller: { email: ME, since: new Date().toISOString() }, ...over })
}

/** A session someone else controls, seen by a member who may not take it. */
function viewer(over: Partial<LiveSession> = {}): LiveSession {
  return session({
    controller: { email: OTHER, since: new Date().toISOString() },
    you: { email: ME, can_control: false, can_preempt: false, can_open: false },
    ...over,
  })
}

function pane(s: LiveSession) {
  return render(
    <LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />,
  )
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
  const [p, setP] = useState<'detail' | 'logs'>('detail')
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane={p}
        artifact={null}
        closeTo="work/running/live"
        go={(to) => {
          onGo(to)
          setP(to.endsWith('/logs') ? 'logs' : 'detail')
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
  FakeSocket.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  // Built at run time: nothing here may look like a credential in the source.
  api.mintLiveTicket.mockImplementation(async () => ok({ ticket: `vt.${crypto.randomUUID()}`, expires_at: '' }))
  api.takeLiveControl.mockResolvedValue(ok(null))
  api.returnLiveControl.mockResolvedValue(ok(null))
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.setLiveDevice.mockResolvedValue(ok(null))
  api.setLiveOpenToMembers.mockResolvedValue(ok(null))
})

afterEach(() => {
  delete document.documentElement.dataset.agentList
})

describe('the Browser tab is first for a running browser agent', () => {
  it('opens a running live-browser agent on its Browser tab, the first tab, and does not send it to Logs', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
    const went: string[] = []
    render(<Routed onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(label(tabs()[0]!)).toBe('Browser'))
    await waitFor(() => expect(tabs()[0]!.getAttribute('aria-selected')).toBe('true'))
    expect(went.filter((to) => to.endsWith('/logs'))).toEqual([])
    expect(document.querySelector('.live-strip')).not.toBeNull()
  })

  it('draws no Browser tab for a running agent whose profile is not a live browser, and opens it on Logs', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ runner_profile: 'claude-code', resource_class: 'standard' })))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: false, session: null }))
    const went: string[] = []
    render(<Routed onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(went).toContain(`work/task/${ID}/logs`))
    expect(tabs().map(label)).not.toContain('Browser')
  })

  it('does not ask a finished agent for a live session at all', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ state: 'SUCCEEDED', completed_at: new Date().toISOString() })))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(label(tabs()[0]!)).toBe('Details'))
    expect(api.loadLiveSession).not.toHaveBeenCalled()
    expect(tabs().map(label)).not.toContain('Browser')
  })
})

describe('the ticket goes in the first message, never the URL', () => {
  it('opens /v1/tasks/<id>/live with nothing else in the URL and sends the ticket as the first frame, before noVNC has the socket', async () => {
    const ticket = `vt.${crypto.randomUUID()}`
    api.mintLiveTicket.mockResolvedValue(ok({ ticket, expires_at: '' }))
    pane(session())
    const r = await connected()
    const ws = FakeSocket.made.at(-1)!
    expect(ws.url).toMatch(new RegExp(`^wss?://[^/?#]*/v1/tasks/${ID}/live$`))
    expect(ws.url).not.toContain(ticket)
    expect(ws.sent[0]).toBe(JSON.stringify({ ticket }))
    expect(r.sentBeforeAttach, 'noVNC was handed the socket before the ticket went').toBe(1)
    expect(r.channel).toBe(ws)
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
      you: { email: ME, can_control: true, can_preempt: false, can_open: false },
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
    const select = screen.getByRole('combobox') as HTMLSelectElement
    expect(select.value).toBe('iphone-15')
    expect(select.disabled).toBe(true)
    expect((screen.getByRole('button', { name: /rotate/i }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /touch/i }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(select, { target: { value: 'desktop' } })
    expect(api.setLiveDevice).not.toHaveBeenCalled()
  })

  it('lets the person in control switch, by preset name only', () => {
    pane(mine())
    const select = screen.getByRole('combobox') as HTMLSelectElement
    expect(select.disabled).toBe(false)
    fireEvent.change(select, { target: { value: 'desktop' } })
    expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'desktop', orientation: 'portrait', touch: false })
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

  it('asks once on the named site', async () => {
    render(<LiveControl taskId={ID} session={session({ human_wait: wait('github.com') })} reload={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Take control' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    await waitFor(() => expect(api.takeLiveControl).toHaveBeenCalledWith(ID, {}))
  })
})

describe('keys are captured only with canvas focus', () => {
  it('keeps the connection view-only until the person in control focuses the canvas, and again once it loses focus', async () => {
    pane(mine())
    const r = await connected()
    expect(r.viewOnly).toBe(true)
    act(() => r.canvas.focus())
    expect(r.viewOnly).toBe(false)
    act(() => r.canvas.blur())
    expect(r.viewOnly).toBe(true)
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

  it('turns touch on only from the person in control, through the device route', () => {
    pane(mine())
    fireEvent.click(screen.getByRole('button', { name: /touch off/i }))
    expect(api.setLiveDevice).toHaveBeenCalledWith(ID, { preset: 'iphone-15', orientation: 'portrait', touch: true })
  })
})
