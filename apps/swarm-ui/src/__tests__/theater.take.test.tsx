// LANE THEATER-TAKE: IN THEATER, A PERSON WHO MAY TAKE CONTROL SEES A TAKE
// CONTROL BUTTON WHERE THE HAND-OFF ASKS FOR IT (part of #1030). Owner
// report 2026-10-13 ~03:05 PDT: "there should be a take control button while
// in theatre mode and not yet". Measured on task_8fcb2e24e15144ecaa61: the
// checklist's only button was "I'm done, return control", disabled until
// control was taken, and nothing in the checklist took it.
//
//   1. The checklist's button, while you are not in control and may take it,
//      is the tab's Take control: the same takeLiveControl, after the same
//      first ask, and the wrong-site second ask. In control, it is "I'm done,
//      return control". Not eligible: who holds control, and no button.
//   2. The bottom bar's Take control is there while you are not in control,
//      with a hand-off and without one, and its ask is drawn inside the
//      theater's portal, opening upward from the bar (it opened below it,
//      off the screen).
//
// MUTATIONS: drop the `take` slot from LiveBrowser's TheaterHost (no Take
// control in the checklist); render the checklist's take regardless of
// eligibility (a View-only person gets a button); drop `.th-bottom .live-ask`
// from theater.css (the ask opens below the bar, off the screen).

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
  api.takeLiveControl.mockReset().mockResolvedValue(ok(null))
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

const theaterRoot = () => screen.getByRole('dialog', { name: /^Theater/ })
const checklist = () => within(theaterRoot()).getByRole('region', { name: 'Hand-off checklist' })
const footer = () => theaterRoot().querySelector('footer') as HTMLElement

const WAIT = { reason: 'Sign in to GitHub', expect: 'github.com', requested_at: '2026-10-13T10:00:00Z', deadline: new Date(Date.now() + 300_000).toISOString() }

async function openTheater(s: LiveSession): Promise<void> {
  api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: s }))
  render(<Routed start="browser" theater onGo={() => {}} />)
  await waitFor(() => expect(theaterOpen()).not.toBeNull(), SPLIT)
  await waitFor(() => expect(theaterRoot().querySelector('footer .live-control')).not.toBeNull(), SPLIT)
}

describe('1. the hand-off checklist takes control', () => {
  it('not in control and eligible: the checklist offers Take control, which asks, then takes', async () => {
    await openTheater(session({ human_wait: WAIT }))
    const list = checklist()
    expect(within(list).queryByRole('button', { name: "I'm done, return control" })).toBeNull()
    fireEvent.click(within(list).getByRole('button', { name: 'Take control' }))
    expect(api.takeLiveControl).not.toHaveBeenCalled()

    // The first ask, the tab's own, drawn in the checklist inside the theater portal.
    const ask = within(list).getByRole('dialog', { name: 'Take control of the agent’s browser' })
    expect(ask.closest('[data-theater]')).toBe(theaterRoot())
    fireEvent.click(within(ask).getByRole('button', { name: 'Take control' }))
    await waitFor(() => expect(api.takeLiveControl).toHaveBeenCalledWith(ID, {}))
  })

  it('on the wrong site, the checklist’s take asks the second time, and says what was confirmed', async () => {
    const evil = { origin: 'https://evil.test', host: 'evil.test', registrable_domain: 'evil.test', scheme: 'https:' }
    await openTheater(session({ human_wait: WAIT, origin: evil }))
    const list = checklist()
    fireEvent.click(within(list).getByRole('button', { name: 'Take control' }))
    fireEvent.click(within(within(list).getByRole('dialog')).getByRole('button', { name: 'Take control' }))
    expect(api.takeLiveControl).not.toHaveBeenCalled()
    const second = within(list).getByRole('alertdialog', { name: 'This is not the site the agent named' })
    fireEvent.click(within(second).getByRole('button', { name: 'Take control on evil.test' }))
    await waitFor(() =>
      expect(api.takeLiveControl).toHaveBeenCalledWith(ID, { confirmed_mismatch: { expected: 'github.com', actual: 'evil.test' } }),
    )
  })

  it('someone else in control, and you may preempt: the checklist offers to take it from them', async () => {
    const other = ['bo', 'example.com'].join('@')
    await openTheater(session({ human_wait: WAIT, controller: { email: other, since: '2026-10-13T10:01:00Z' } }))
    fireEvent.click(within(checklist()).getByRole('button', { name: `Take control from ${other}` }))
    fireEvent.click(within(within(checklist()).getByRole('dialog')).getByRole('button', { name: `Take control from ${other}` }))
    await waitFor(() => expect(api.takeLiveControl).toHaveBeenCalledWith(ID, { preempt: true }))
  })

  it('in control: the checklist’s button is "I\'m done, return control", and no Take control', async () => {
    await openTheater(session({ human_wait: WAIT, controller: { email: ME, since: '2026-10-13T10:01:00Z' } }))
    const list = checklist()
    const back = within(list).getByRole('button', { name: "I'm done, return control" }) as HTMLButtonElement
    expect(back.disabled).toBe(false)
    expect(within(list).queryByRole('button', { name: /^Take control/ })).toBeNull()
  })

  it('not eligible, nobody in control: "View only", and no button', async () => {
    await openTheater(session({ human_wait: WAIT, you: { email: ME, can_control: false, can_preempt: false, can_open: false, can_switch_device: false } }))
    const list = checklist()
    expect(within(list).queryByRole('button', { name: /Take control|return control/ })).toBeNull()
    expect(list.textContent).toContain('View only')
  })

  it('not eligible, someone else in control: names them, and no button', async () => {
    const other = ['bo', 'example.com'].join('@')
    await openTheater(
      session({
        human_wait: WAIT,
        controller: { email: other, since: '2026-10-13T10:01:00Z' },
        you: { email: ME, can_control: true, can_preempt: false, can_open: false, can_switch_device: false },
      }),
    )
    const list = checklist()
    expect(within(list).queryByRole('button', { name: /Take control|return control/ })).toBeNull()
    expect(list.textContent).toContain(`${other} is in control`)
  })
})

describe('2. the bottom bar’s Take control', () => {
  it('without a hand-off, the bar offers Take control; its ask is inside the theater and reachable', async () => {
    await openTheater(session())
    expect(within(theaterRoot()).queryByRole('region', { name: 'Hand-off checklist' })).toBeNull()
    const take = within(footer()).getByRole('button', { name: 'Take control' })
    fireEvent.click(take)
    const ask = within(footer()).getByRole('dialog', { name: 'Take control of the agent’s browser' })
    expect(ask.closest('[data-theater]')).toBe(theaterRoot())
    // Reachable from the keyboard: real buttons, in the tab order after the bar's.
    const yes = within(ask).getByRole('button', { name: 'Take control' }) as HTMLButtonElement
    expect(yes.disabled).toBe(false)
    expect(yes.tabIndex).toBe(0)
    expect(take.compareDocumentPosition(yes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    yes.focus()
    expect(document.activeElement).toBe(yes)
    // Esc in the ask closes the ask, not the theater.
    fireEvent.keyDown(ask, { key: 'Escape' })
    expect(theaterOpen()).not.toBeNull()
    expect(within(footer()).queryByRole('dialog')).toBeNull()
  })

  it('with a hand-off, the bar offers Take control too; in control it reads Return control', async () => {
    await openTheater(session({ human_wait: WAIT }))
    expect(within(footer()).getByRole('button', { name: 'Take control' })).toBeTruthy()
  })

  it('in control, the bar reads Return control', async () => {
    await openTheater(session({ controller: { email: ME, since: '2026-10-13T10:01:00Z' } }))
    expect(within(footer()).getByRole('button', { name: 'Return control' })).toBeTruthy()
    expect(within(footer()).queryByRole('button', { name: /^Take control/ })).toBeNull()
  })

  it('the bar’s ask opens upward, over the theater, not below the bar off the screen', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'theater.css'), 'utf8')
    const rule = css.match(/\.th-bottom \.live-ask\s*\{([^}]*)\}/)
    expect(rule).not.toBeNull()
    expect(rule![1]).toMatch(/top:\s*auto/)
    expect(rule![1]).toMatch(/bottom:\s*100%/)
  })
})
