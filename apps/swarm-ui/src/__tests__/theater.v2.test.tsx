// LANE LB-V2: THE THEATER'S NAVIGATION, PILL AND DEVICE COLUMN MATCH THE
// MOCKUP (docs/design/live-browser.md §6; part of #1030). Measured in the
// visual QA pass 5 on SwarmCloud, 2026-10-12 05:45-06:00 PDT:
//
//   B1. Esc in the theater closed the theater AND the agent pane: the address
//       went from /agents/live/<task>/browser to /agents/live. Esc closes only
//       the theater, back to the Browser tab, with focus on ⤢ Theater.
//   B4. The right DEVICE column clipped its chip row ("iPhone SE" cut off).
//       It is a vertical list there: an outline icon and the name per device,
//       the current one marked.
//   B5. The top pill read "LIVE task_…". It reads "LIVE <agent name>", with
//       the task id as its title.
//
// MUTATIONS, one per block: drop the theater root's onKeyDown (the split's
// own onKeyDown hears the key through the portal and closes); drop the
// `[data-theater]` guard in AgentSplit's window listener; focus what opened
// the theater instead of ⤢; drop the `.th-device-ctl` wrapper or its column
// rule; label the pill with the task id again.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
const { resetListSnap } = await import('../listSnap')
const { fromAddress } = await import('../App')

const ID = 'task_0123456789abcdef0789'
const ME = ['ada', 'example.com'].join('@')

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
const SPLIT = { timeout: 8000 }

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  rfb.FakeRfb.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  // The theater's self-read list of live agents: answered empty, so the
  // pill's name can only come from the agent the split read.
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ tasks: [] }), { status: 200, headers: { 'content-type': 'application/json' } })))
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
  api.loadTask.mockResolvedValue(ok(agent({ step_id: 'browse' })))
  api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  delete document.documentElement.dataset.agentList
})

const theaterButton = () => screen.getByRole('button', { name: 'Open theater (T)' })

describe('B1: Esc closes only the theater, back to the Browser tab and ⤢', () => {
  it('Esc pressed in the theater closes it and leaves the split on Browser', async () => {
    const went: string[] = []
    render(<Routed start="browser" theater onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
    went.length = 0

    fireEvent.keyDown(theaterOpen()!, { key: 'Escape' })

    expect(theaterOpen()).toBeNull()
    expect(went).not.toContain('work/running/live')
    expect(screen.getByRole('dialog', { name: `Agent ${ID}` })).toBeTruthy()
    expect(browserTab().getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(theaterButton())
  })

  it('Esc with focus outside the theater (on the page body) still closes only the theater', async () => {
    const went: string[] = []
    render(<Routed start="browser" theater onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
    ;(document.activeElement as HTMLElement | null)?.blur()
    went.length = 0

    fireEvent.keyDown(document.body, { key: 'Escape' })

    expect(theaterOpen()).toBeNull()
    expect(went).not.toContain('work/running/live')
    expect(screen.getByRole('dialog', { name: `Agent ${ID}` })).toBeTruthy()
  })

  it('opened from ⤢, Esc gives focus back to ⤢; the next Esc closes the split as before', async () => {
    const went: string[] = []
    render(<Routed start="browser" onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(document.querySelector('.live-toolbar')).not.toBeNull(), SPLIT)
    theaterButton().focus()
    fireEvent.click(theaterButton())
    expect(theaterOpen()).not.toBeNull()
    went.length = 0

    fireEvent.keyDown(theaterOpen()!, { key: 'Escape' })
    expect(theaterOpen()).toBeNull()
    expect(went).toEqual([])
    expect(document.activeElement).toBe(theaterButton())

    // The control: the split's own Escape still works once the theater is gone.
    fireEvent.keyDown(theaterButton(), { key: 'Escape' })
    expect(went).toEqual(['work/running/live'])
  })

  it('Esc in the screen is the remote browser’s: the theater stays and the split never hears it', async () => {
    const went: string[] = []
    render(<Routed start="browser" theater onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
    went.length = 0
    const screenNode = theaterOpen()!.querySelector('[data-live-screen]')
    expect(screenNode).not.toBeNull()

    fireEvent.keyDown(screenNode!, { key: 'Escape' })

    expect(theaterOpen()).not.toBeNull()
    expect(went).toEqual([])
  })
})

describe('B4: the DEVICE column is a vertical list, nothing clipped', () => {
  it('lists every device with its outline and name, the current one marked', async () => {
    render(<Routed start="browser" theater onGo={() => {}} />)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
    const column = screen.getByRole('region', { name: 'DEVICE' })
    const list = column.querySelector('.th-device-ctl')
    expect(list).not.toBeNull()
    const chips = within(within(list as HTMLElement).getByRole('group', { name: 'Device' })).getAllByRole('button')
    expect(chips.map((c) => [c.textContent, c.getAttribute('data-outline')])).toEqual([
      ['Desktop', 'desktop'],
      ['iPhone 15', 'phone'],
      ['Phone 390', 'phone'],
      ['Pixel 8', 'phone'],
      ['iPhone SE', 'phone'],
      ['iPad', 'tablet'],
    ])
    expect(chips.filter((c) => c.getAttribute('aria-pressed') === 'true').map((c) => c.textContent)).toEqual(['iPhone 15'])
  })

  it('the stylesheet stacks the chips in a column that fits the column’s width', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'theater.css'), 'utf8').replace(/\s+/g, ' ')
    const rule = /\.th-device-ctl \.live-chips \{([^}]*)\}/.exec(css)
    expect(rule).not.toBeNull()
    expect(rule![1]).toMatch(/flex-direction: column/)
    const chip = /\.th-device-ctl \.live-chip \{([^}]*)\}/.exec(css)
    expect(chip).not.toBeNull()
    expect(chip![1]).toMatch(/width: 100%/)
    expect(css).toMatch(/\.th-device-ctl \{[^}]*flex-wrap: wrap/)
  })
})

describe('B5: the pill reads LIVE <agent name>, with the task id as its title', () => {
  it('names the agent the split read, not its id', async () => {
    render(<Routed start="browser" theater onGo={() => {}} />)
    await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
    const pill = screen.getByTestId('theater-live-pill')
    expect(pill.textContent).toMatch(/^LIVE\s*browse$/)
    expect(pill.getAttribute('title')).toBe(ID)
  })
})
