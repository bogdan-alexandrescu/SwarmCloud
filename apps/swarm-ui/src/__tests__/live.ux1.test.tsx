// LANE LB-UX1: THE BROWSER TAB, ITS TOOLBAR AND THE HAND-OFF BANNER, AS THE
// OWNER CHOSE THEM (docs/design/live-browser.md §6, "Console decisions,
// 2026-10-12"; part of #1030). A visual QA on 2026-10-11 measured each of
// these against the mockup "Browser Tab and Theater":
//
//   1. the tabs are Details, Browser, Logs, ...; Browser is the one a running
//      browser agent opens on, and says LIVE while its session is open;
//   2. Browser has its own address, `/agents/<tab>/<id>/browser`, and
//      `?theater=1` there opens the theater;
//   3. the toolbar is device chips, `⟲ rotate`, `touch: on|off`, `◫ logs`,
//      and `⤢ Theater` at the right, then "Let any member take control"
//      (since BROWSER-TAB-LIVE, #2423: the chips, rotate and touch in a
//      `Device: <preset> ▾` menu, and the members box with them, apart from
//      `◫ logs` and `⤢ Theater`);
//   4. under the device, `<device> · <W>×<H> · <path>`;
//   5. `◫ logs` puts the logs on the LEFT and the device on the right;
//   6. ONE hand-off banner on the Browser tab, carrying Take control and Open
//      in theater -- and the same two on every other tab; "I'm ready to sign
//      in" opens the Browser tab;
//   7. the header reads Copy link · Take control · Stop.
//
// MUTATIONS, one per block: put Browser back before Details; drop `browser`
// from PANE_SEGMENTS; draw the device as a <select> again; drop the
// landscape swap from the caption; put the logs after the screen; render
// LiveBrowser's own banner as well as the strip; label Stop `stop`.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { clockTime } from '../types'
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
  loadTaskEventsPage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
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
    constructor(target: HTMLElement) {
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
  binaryType = 'blob'
  readyState = 0
  onmessage: ((e: MessageEvent) => void) | null = null
  constructor(readonly url: string) {
    super()
  }
  send() {}
  close() {
    this.readyState = 3
  }
}

const { AgentSplit } = await import('../AgentSplit')
const { LiveBrowserPane } = await import('../LiveBrowser')
const { resetListSnap } = await import('../listSnap')
const { canonical, fromAddress } = await import('../App')
const { addressToPath, pathToAddress } = await import('../paths')

const ID = 'task_0123456789abcdef0789'
const ME = ['ada', 'example.com'].join('@')
const OTHER = ['grace', 'example.com'].join('@')
const NOW = Date.now()
const iso = (ms: number) => new Date(ms).toISOString()

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

const WAIT = {
  reason: 'Sign in to GitHub as the bot account',
  expect: 'github.com',
  requested_at: iso(NOW - 60_000),
  deadline: iso(NOW + 540_000),
}

/** A RUNNING browser agent holding its pod for a person (§4.3 phase H). */
function holding(): Task {
  return agent({ human_wait: { ...WAIT, controller: null } })
}

/** A browser agent whose hold ran out: PARKED(HUMAN_REQUIRED). */
function parked(): Task {
  return agent({ state: 'PARKED', park_reason: 'HUMAN_REQUIRED', next_eligible_at: iso(NOW - 60_000), human_wait: null })
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'pixel-8', width: 412, height: 915, scale: 2.625, mobile: true },
  { name: 'iphone-se', width: 375, height: 667, scale: 2, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_ux1',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'iphone-15', orientation: 'portrait', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

const mine = (over: Partial<LiveSession> = {}) =>
  session({ controller: { email: ME, since: iso(NOW) }, ...over })
const viewer = (over: Partial<LiveSession> = {}) =>
  session({
    controller: { email: OTHER, since: iso(NOW) },
    you: { email: ME, can_control: false, can_preempt: false, can_open: false, can_switch_device: false },
    ...over,
  })

function pane(s: LiveSession, t: Task = agent()) {
  const view = render(<LiveBrowserPane task={t} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />)
  // The chips, rotate and touch are in the toolbar's Device menu (BT-003): opened as a person opens it.
  const menu = screen.queryByRole('button', { name: /^Device:/ })
  if (menu !== null) fireEvent.click(menu)
  return view
}

type Pane = 'detail' | 'logs' | 'browser'

/** The split as App routes it: `go` moves the pane, and `?theater=1` is read from the address. */
function Routed({ start = 'detail', theater = false, onGo }: { start?: Pane; theater?: boolean; onGo: (to: string) => void }) {
  const [at, setAt] = useState<{ p: Pane; theater: boolean }>({ p: start, theater })
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane={at.p}
        theater={at.theater}
        artifact={null}
        closeTo="work/running/live"
        go={(to) => {
          onGo(to)
          const r = fromAddress(to)
          setAt({ p: r.taskPane as Pane, theater: r.theater === true })
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

function browserTab(): HTMLElement {
  return tabs().find((t) => label(t) === 'Browser')!
}

const theaterOpen = () => screen.queryByRole('dialog', { name: /^Theater/ })

/**
 * The split's waits: two reads (the task, then the session) and a redirect
 * before the tab is chosen, which under a loaded parallel run takes longer
 * than waitFor's default second. 8 s was not enough on PR #1592's run
 * 39206985893 (shard 4/4: 1,234 s of test time in 298 s of wall clock), where
 * the second wait of the LIVE test timed out with the tab still unselected;
 * the same test takes ~4 s on an idle machine. 12 s was not enough on PR
 * #2959's run 41288544689 either (shard 4/4: 3,145 s of test time in 819 s of
 * wall clock), where both item 1 tests ran out with the tab still unselected;
 * on an idle machine the first takes ~6 s and the second ~7 s. 20 s per wait,
 * and ITEM_1 for the two tests that ran out, keeps the LIVE test's two waits
 * inside its own timeout.
 */
const SPLIT = { timeout: 20_000 }
const ITEM_1 = 60_000

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  rfb.FakeRfb.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.takeLiveControl.mockResolvedValue(ok(null))
  api.returnLiveControl.mockResolvedValue(ok(null))
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.openLiveToMembers.mockResolvedValue(ok(null))
})

afterEach(() => {
  vi.unstubAllGlobals()
  delete document.documentElement.dataset.agentList
})

// ---------------------------------------------------------------------------
// 1. Tab order, the default tab and the LIVE marker
// ---------------------------------------------------------------------------

describe('item 1: Details, Browser, Logs, and Browser opens while the agent runs', () => {
  it('orders the tabs Details, Browser, Logs and opens a running browser agent on Browser, by its address', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    const went: string[] = []
    render(<Routed onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(browserTab().getAttribute('aria-selected')).toBe('true'), SPLIT)
    expect(tabs().map(label).slice(0, 3)).toEqual(['Details', 'Browser', 'Logs'])
    expect(went).toContain(`work/task/${ID}/browser`)
    expect(went.filter((to) => to.endsWith('/logs'))).toEqual([])
  }, ITEM_1)

  it('marks the Browser tab LIVE while the session is open, and not before the tunnel is up', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    const { unmount } = render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(browserTab().textContent).toContain('LIVE'), SPLIT)
    unmount()

    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: null }))
    render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(browserTab().getAttribute('aria-selected')).toBe('true'), SPLIT)
    expect(browserTab().textContent).not.toContain('LIVE')
  }, ITEM_1)
})

// ---------------------------------------------------------------------------
// 2. The Browser tab's address, and ?theater=1
// ---------------------------------------------------------------------------

describe('item 2: `browser` is a routed pane, and ?theater=1 opens the theater', () => {
  it('reads /agents/live/<id>/browser as the Browser tab of that agent, and writes it back the same', () => {
    const p = pathToAddress(`/agents/live/${ID}/browser`)!
    const r = fromAddress(p.address)
    expect([r.taskId, r.taskPane]).toEqual([ID, 'browser'])
    expect(r.theater ?? false).toBe(false)
    expect(addressToPath(canonical(r), 'live')).toBe(`/agents/live/${ID}/browser`)
  })

  it('reads ?theater=1 on the Browser address, and only there', () => {
    const r = fromAddress(pathToAddress(`/agents/live/${ID}/browser`, '?theater=1')!.address)
    expect([r.taskId, r.taskPane, r.theater]).toEqual([ID, 'browser', true])
    expect(fromAddress(`work/task/${ID}/browser?theater=1`).theater).toBe(true)
    expect(fromAddress(`work/task/${ID}/logs?theater=1`).theater ?? false).toBe(false)
    expect(fromAddress(`work/task/${ID}/browser?theater=0`).theater ?? false).toBe(false)
  })

  it('opens the theater when the split is routed to Browser with theater set', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    render(<Routed start="browser" theater onGo={() => {}} />)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
  })

  it('opens the Browser tab, not the theater, without it', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    render(<Routed start="browser" onGo={() => {}} />)
    await waitFor(() => expect(document.querySelector('.live-toolbar')).not.toBeNull(), SPLIT)
    expect(browserTab().getAttribute('aria-selected')).toBe('true')
    expect(theaterOpen()).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 3. The toolbar
// ---------------------------------------------------------------------------

describe('item 3: the toolbar is the mockup’s, left to right', () => {
  const toolbar = () => screen.getByRole('toolbar', { name: 'Browser view' })

  it('draws the five device chips with the mockup’s labels, the current one pressed, and no <select>', () => {
    pane(session())
    const chips = within(within(toolbar()).getByRole('group', { name: 'Device' })).getAllByRole('button')
    expect(chips.map((c) => c.textContent)).toEqual(['Desktop', 'iPhone 15', 'Pixel 8', 'iPhone SE', 'iPad'])
    expect(chips.filter((c) => c.getAttribute('aria-pressed') === 'true').map((c) => c.textContent)).toEqual(['iPhone 15'])
    expect(within(toolbar()).queryByRole('combobox')).toBeNull()
  })

  it('labels rotate, touch and logs exactly: the members box after the device, ◫ logs and ⤢ Theater apart at the right', () => {
    pane(session({ device: { preset: 'iphone-15', orientation: 'portrait', touch: true } }))
    const bar = toolbar()
    const names = [...bar.querySelectorAll('button, input')].map((el) =>
      el instanceof HTMLInputElement ? 'checkbox' : (el.textContent ?? '').trim(),
    )
    const after = names.slice(names.indexOf('iPad') + 1)
    expect(after).toEqual(['⟲ rotate', 'touch: on', 'checkbox', '◫ logs', '⤢ Theater'])
    expect(bar.querySelector('.live-toolbar-end')?.contains(within(bar).getByRole('button', { name: 'Open theater (T)' }))).toBe(true)
    expect(within(bar).getByRole('checkbox', { name: 'Let any member take control' })).toBeTruthy()
  })

  it('says touch: off when touch is off', () => {
    pane(session())
    expect(within(toolbar()).getByRole('button', { name: 'touch: off' })).toBeTruthy()
  })

  it('keeps every device control disabled for a viewer, saying why', () => {
    pane(viewer())
    const bar = toolbar()
    for (const name of ['Desktop', 'iPad', '⟲ rotate', 'touch: off']) {
      const b = within(bar).getByRole('button', { name }) as HTMLButtonElement
      expect(b.disabled, `${name} is enabled for a viewer`).toBe(true)
      expect(b.title).toMatch(/only the person in control/i)
    }
  })

  it('enables them for the person in control (LB-DEV-B), with nothing said against them', () => {
    pane(mine())
    for (const name of ['Pixel 8', '⟲ rotate']) {
      const b = within(toolbar()).getByRole('button', { name }) as HTMLButtonElement
      expect(b.disabled, `${name} is disabled for the controller`).toBe(false)
      expect(b.title).toBe('')
    }
  })

  it('draws the chips unpressed when the gate has not reported the device', () => {
    pane(session({ device: null }))
    const chips = within(within(toolbar()).getByRole('group', { name: 'Device' })).getAllByRole('button')
    expect(chips.filter((c) => c.getAttribute('aria-pressed') === 'true')).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 4. The caption under the device
// ---------------------------------------------------------------------------

describe('item 4: the caption under the device', () => {
  const caption = () => document.querySelector('.live-caption')?.textContent

  it('reads `<device> · <W>×<H> · <path>`', () => {
    pane(session())
    expect(caption()).toBe('iPhone 15 · 393×852 · path not reported')
  })

  it('swaps the sides in landscape', () => {
    pane(session({ device: { preset: 'iphone-15', orientation: 'landscape', touch: false } }))
    expect(caption()).toBe('iPhone 15 · 852×393 · path not reported')
  })

  it('says the device is not reported rather than guess one', () => {
    pane(session({ device: null }))
    expect(caption()).toBe('Device not reported · path not reported')
  })

  it('sits under the canvas, in the device column', () => {
    pane(session())
    const screenNode = document.querySelector('.live-screen')!
    const cap = document.querySelector('.live-caption')!
    expect(screenNode.compareDocumentPosition(cap) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// 5. Logs beside: logs left, device right
// ---------------------------------------------------------------------------

describe('item 5: ◫ logs puts the logs on the left and the device on the right', () => {
  it('draws the logs first in the body once toggled, and the device after them', () => {
    pane(session())
    fireEvent.click(screen.getByRole('button', { name: '◫ logs' }))
    expect(screen.getByRole('button', { name: '◫ logs' }).getAttribute('aria-pressed')).toBe('true')
    const body = document.querySelector('.live-body')!
    const kids = [...body.children]
    expect(kids[0]!.classList.contains('live-logs')).toBe(true)
    // Between them, the separator that shares the width (BT-002, #2423).
    expect(kids[1]!.getAttribute('role')).toBe('separator')
    expect(kids[2]!.querySelector('.live-screen')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 6. The hand-off banner
// ---------------------------------------------------------------------------

describe('item 6: one hand-off banner, with Take control and Open in theater', () => {
  const strips = () => [...document.querySelectorAll('.ho-strip')]

  async function onBrowser(s: LiveSession) {
    api.loadTask.mockResolvedValue(ok(holding()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: s }))
    render(<Routed start="browser" onGo={() => {}} />)
    await waitFor(() => expect(document.querySelector('.live-toolbar')).not.toBeNull(), SPLIT)
    await waitFor(() => expect(strips()).toHaveLength(1), SPLIT)
  }

  it('draws exactly one banner on the Browser tab, with the reason, the site and the time left', async () => {
    await onBrowser(session({ human_wait: WAIT }))
    expect(document.querySelector('.live-handoff')).toBeNull()
    const text = strips()[0]!.textContent ?? ''
    expect(text).toContain(WAIT.reason)
    expect(text).toContain('github.com')
    expect(text).toMatch(/\d:\d\d left/)
  })

  it('carries Take control, which asks as the header’s does, and Open in theater, which opens it', async () => {
    await onBrowser(session({ human_wait: WAIT }))
    const banner = strips()[0] as HTMLElement
    fireEvent.click(within(banner).getByRole('button', { name: 'Take control' }))
    expect(within(banner).getByRole('dialog', { name: /take control/i })).toBeTruthy()
    fireEvent.click(within(banner).getByRole('button', { name: 'Open in theater' }))
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
  })

  it('hides Take control from a person who may not take it', async () => {
    await onBrowser(viewer({ human_wait: WAIT }))
    const banner = strips()[0] as HTMLElement
    expect(within(banner).queryByRole('button', { name: /take control/i })).toBeNull()
    expect(banner.textContent).not.toMatch(/view only|is in control/i)
    expect(within(banner).getByRole('button', { name: 'Open in theater' })).toBeTruthy()
  })

  it('keeps the strip on Logs, with the same two buttons; Open in theater goes to the Browser tab in theater', async () => {
    api.loadTask.mockResolvedValue(ok(holding()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session({ human_wait: WAIT }) }))
    const went: string[] = []
    render(<Routed start="logs" onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(strips()).toHaveLength(1), SPLIT)
    const banner = strips()[0] as HTMLElement
    await waitFor(() => expect(within(banner).getByRole('button', { name: 'Take control' })).toBeTruthy(), SPLIT)
    fireEvent.click(within(banner).getByRole('button', { name: 'Open in theater' }))
    expect(went).toContain(`work/task/${ID}/browser?theater=1`)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
  })

  it('on a parked hand-off, "I\'m ready to sign in" calls human-ready and then opens the Browser tab', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ task: { ...parked(), state: 'READY' } }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    api.loadTask.mockResolvedValue(ok(parked()))
    const went: string[] = []
    render(<Routed onGo={(to) => went.push(to)} />)
    const ready = await screen.findByRole('button', { name: /I'm ready/ }, SPLIT)
    expect(strips()).toHaveLength(1)
    await act(async () => {
      fireEvent.click(ready)
    })
    await waitFor(() => expect(went).toContain(`work/task/${ID}/browser`), SPLIT)
    const posts = fetchMock.mock.calls.filter((c) => (c as unknown[])[1] !== undefined)
    expect(String((posts[0] as unknown[])[0])).toBe(`/v1/tasks/${ID}/human-ready`)
    await waitFor(() => expect(browserTab().getAttribute('aria-selected')).toBe('true'), SPLIT)
    expect(strips(), 'the Browser tab drew a second banner').toHaveLength(1)
  })

  // LB-V4, U1: the Details tab's `eligible <time>` bar steps aside for a
  // parked hand-off, so the banner is the only place its park instant is
  // printed. Without `HandoffParkedSince` in the strip it is printed nowhere.
  it('on a parked hand-off, the banner says since when it is parked', async () => {
    api.loadTask.mockResolvedValue(ok(parked()))
    render(<Routed onGo={() => {}} />)
    await screen.findByRole('button', { name: /I'm ready/ }, SPLIT)
    expect(strips()).toHaveLength(1)
    const since = strips()[0]!.querySelector('.ho-since')
    expect(since, 'the banner dropped the park instant').not.toBeNull()
    expect(since!.textContent).toBe(`parked since ${clockTime(new Date(NOW - 60_000).toISOString())!.text}`)
  })

  it('on a held hand-off, the banner says nothing about being parked', async () => {
    api.loadTask.mockResolvedValue(ok(holding()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session({ human_wait: WAIT }) }))
    render(<Routed start="logs" onGo={() => {}} />)
    await waitFor(() => expect(strips()).toHaveLength(1), SPLIT)
    expect(strips()[0]!.textContent).toContain(WAIT.reason)
    expect(strips()[0]!.textContent).not.toMatch(/parked since/)
  })
})

// ---------------------------------------------------------------------------
// 7. The header
// ---------------------------------------------------------------------------

describe('item 7: the header reads Copy link · Take control · Stop', () => {
  it('draws the three actions in that order, Stop capitalised', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    render(<Routed onGo={() => {}} />)
    const actions = await waitFor(() => {
      const a = document.querySelector<HTMLElement>('.ag-head-actions')!
      expect(within(a).getByRole('button', { name: 'Take control' })).toBeTruthy()
      return a
    }, SPLIT)
    const names = within(actions)
      .getAllByRole('button')
      .map((b) => b.getAttribute('aria-label') ?? (b.textContent ?? '').trim())
    expect(names).toEqual(['Copy link', 'Take control', 'Stop', 'Close'])
  })
})
