// LANE LIVE-TAB (part of #1030): THE TAKE-CONTROL ASK IS A FOCUSED DIALOG,
// MEMBER CONTROL CARRIES OVER WITH A CONFIRM, THE LIVE SCREEN IS BIGGER, AND
// TAKE CONTROL IS NOT OFFERED AFTER YOU RETURN.
//
// MEASURED 2026-10-13 in the live-browser QA pass p12, on
// task_4defa61c5eea4a5c87f5:
//
//   QA12-09 the take-control ask took no focus, had no aria-modal, closed on
//           Esc only after tabbing into it, and covered the hand-off reason;
//   QA12-11 "Let any member take control" was one way, with no confirm or
//           explanation (owner decision: "Carry over, with a confirm");
//   QA12-19 the screen was 424×291 at 1440 with the agent list open;
//   QA12-22 after Return control the banner offered Take control until the
//           next read;
//   QA12-25 POST /live/ticket answered 409 twice while the next attempt was
//           STARTING.
//
// MUTATIONS, one per block: drop the first-button focus in `useAsk` (or its
// window listener); call openLiveToMembers on the tick instead of on the
// confirm, or disable the box once open; return `want` uncapped from
// `stageFloor`, or drop the stage's min-height; drop the `human_wait` check
// on `takeOnly`, or `reload()` without the answer; mint before re-reading on
// a reconnect.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession, LiveTicket } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadLiveSession: vi.fn(),
  mintLiveTicket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
  closeLiveToMembers: vi.fn(),
  setLiveDevice: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveActions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    addEventListener() {}
    disconnect() {}
  },
}))

const { LiveBrowserPane, LiveControl, stageFloor, useLiveSession } = await import('../LiveBrowser')

const ID = 'task_4defa61c5eea4a5c87f5'
const ME = ['ada', 'example.com'].join('@')
const REASON = 'Approve the OAuth consent screen for the bot account'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const WAIT = {
  reason: REASON,
  expect: 'github.com',
  requested_at: new Date().toISOString(),
  deadline: new Date(Date.now() + 600_000).toISOString(),
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    completed_at: null,
    result_summary: null,
    ...over,
  })
}

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_tab',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: [
      { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
      { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
    ],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
    ...over,
  }
}

function pane(s: LiveSession, reload: (answer?: unknown) => void = () => {}, task: Task = agent()) {
  return render(<LiveBrowserPane task={task} live={{ status: 'ok', liveBrowser: true, session: s }} reload={reload} />)
}

beforeEach(() => {
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.loadLiveActions.mockReturnValue(new Promise(() => {}))
  api.renewLiveControl.mockResolvedValue(ok({}))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('QA12-09: the take-control ask is a focused dialog that keeps the reason in view', () => {
  function openAsk() {
    render(<LiveControl taskId={ID} session={session({ human_wait: WAIT })} reload={() => {}} />)
    const trigger = screen.getByRole('button', { name: 'Take control' })
    trigger.focus()
    fireEvent.click(trigger)
    return { trigger, dialog: screen.getByRole('dialog', { name: 'Take control of the agent’s browser' }) }
  }

  it('is modal and focuses its first button when it opens', () => {
    const { dialog } = openAsk()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(within(dialog).getAllByRole('button')[0])
  })

  it('quotes the agent’s reason, which the ask would otherwise cover', () => {
    const { dialog } = openAsk()
    expect(dialog.textContent).toContain(REASON)
  })

  it('closes on Esc pressed anywhere, and gives focus back to Take control', () => {
    const { trigger } = openAsk()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps Tab inside it', () => {
    const { dialog } = openAsk()
    const buttons = within(dialog).getAllByRole('button')
    buttons.at(-1)!.focus()
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(document.activeElement).toBe(buttons[0])
  })
})

describe('QA12-11: member control goes both ways, and asks once when turned on', () => {
  const box = () => screen.getByRole('checkbox', { name: /let any member take control/i }) as HTMLInputElement

  it('ticking it asks first, explaining it carries over; only the confirm opens it', async () => {
    const answer: LiveRead = { live_browser: true, session: session({ open_to_members: true }) }
    api.openLiveToMembers.mockResolvedValue(ok(answer))
    const reload = vi.fn()
    pane(session(), reload)
    fireEvent.click(box())
    expect(api.openLiveToMembers).not.toHaveBeenCalled()
    const ask = screen.getByRole('dialog', { name: 'Let any member take control' })
    expect(ask.getAttribute('aria-modal')).toBe('true')
    expect(ask.textContent).toMatch(/later attempts/)
    expect(ask.textContent).toMatch(/turn it off/)
    fireEvent.click(within(ask).getByRole('button', { name: 'Let any member take control' }))
    expect(api.openLiveToMembers).toHaveBeenCalledWith(ID)
    // The answer is drawn at once: the box does not wait for a poll.
    await waitFor(() => expect(reload).toHaveBeenCalledWith(answer))
  })

  it('Cancel leaves it off and sends nothing', () => {
    pane(session())
    fireEvent.click(box())
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(api.openLiveToMembers).not.toHaveBeenCalled()
    expect(box().checked).toBe(false)
  })

  it('once on, it can be turned off, at once and through the served route', async () => {
    api.closeLiveToMembers.mockResolvedValue(ok({ live_browser: true, session: session() }))
    pane(session({ open_to_members: true }))
    expect(box().checked).toBe(true)
    expect(box().disabled).toBe(false)
    fireEvent.click(box())
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(api.closeLiveToMembers).toHaveBeenCalledWith(ID))
    expect(api.openLiveToMembers).not.toHaveBeenCalled()
  })
})

describe('QA12-19: the screen uses the pane’s width', () => {
  const desktop = { width: 1440, height: 900, mobile: false }
  const phone = { width: 393, height: 852, mobile: true }

  it('asks for the height a desktop needs at the full width, outline and caption included', () => {
    // (1000 - 2) × 900 / 1440 = 623.75 → 624, + 2 hairline + 17 caption + 6 gap.
    expect(stageFloor(1000, desktop, 1200, 17)).toBe(649)
  })

  it('never more than the window less the console around it, never less than the floor', () => {
    expect(stageFloor(1000, phone, 900, 17)).toBe(780)
    expect(stageFloor(200, desktop, 900, 17)).toBe(320)
  })

  it('a device not reported yet is drawn in the desktop’s proportions', () => {
    expect(stageFloor(1000, null, 1200, 17)).toBe(stageFloor(1000, desktop, 1200, 17))
  })

  it('the stage carries that height, so the device is fitted to the width', () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1000)
    pane(session())
    const stage = document.querySelector<HTMLElement>('.live-stage')!
    expect(stage.style.minHeight).toBe(`${stageFloor(1000, desktop, window.innerHeight, 0)}px`)
    expect(stage.style.minHeight).not.toBe('320px')
  })
})

describe('QA12-22: after Return control, Take control is not offered', () => {
  it('the banner offers no Take control once the session has no hand-off, though the task still shows one', () => {
    // The task is read on its own clock: it still carries the hand-off the
    // return just resolved. The session's answer says it is over.
    pane(session(), () => {}, agent({ human_wait: { ...WAIT, controller: null } }))
    expect(document.querySelector('.ho-strip')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Take control' })).toBeNull()
  })

  it('draws the return’s answer at once, before the next read comes back', async () => {
    const held = session({ human_wait: WAIT, controller: { email: ME, since: new Date().toISOString() } })
    api.loadLiveSession.mockResolvedValueOnce(ok<LiveRead>({ live_browser: true, session: held }))
    api.loadLiveSession.mockReturnValue(new Promise(() => {}))
    api.returnLiveControl.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    function Header() {
      const { live, reload } = useLiveSession(ID, true)
      const s = live.status === 'ok' ? live.session : null
      return (
        <>
          <LiveControl taskId={ID} session={s} reload={reload} />
          <LiveControl taskId={ID} session={s} reload={reload} takeOnly />
        </>
      )
    }
    render(<Header />)
    fireEvent.click(await screen.findByRole('button', { name: 'Return control' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Return control' })).toBeNull())
    // Only the header's own Take control, the submitter's at any time (Q6);
    // the banner's copy went with the hand-off.
    expect(screen.getAllByRole('button', { name: 'Take control' })).toHaveLength(1)
  })
})

describe('QA12-25: no ticket is asked for while the session is not open', () => {
  it('a reconnect reads the session first, and hands the pane a closed one without minting', async () => {
    const refused: Result<LiveTicket> = {
      status: 'error',
      error: { kind: 'http', status: 409, code: 'live_session_closed', message: 'closed' },
    } as unknown as Result<LiveTicket>
    api.mintLiveTicket.mockResolvedValueOnce(refused)
    const next: LiveRead = { live_browser: true, session: null }
    api.loadLiveSession.mockResolvedValue(ok(next))
    const reload = vi.fn()
    pane(session(), reload)
    await waitFor(() => expect(reload).toHaveBeenCalledWith(next), { timeout: 5000 })
    expect(api.mintLiveTicket).toHaveBeenCalledTimes(1)
  })

  it('a reconnect to a session still open mints as before', async () => {
    api.mintLiveTicket.mockResolvedValueOnce({ status: 'error', error: { message: 'blip' } } as unknown as Result<LiveTicket>)
    api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
    pane(session())
    await waitFor(() => expect(api.mintLiveTicket).toHaveBeenCalledTimes(2), { timeout: 5000 })
    await act(async () => {})
  })
})
