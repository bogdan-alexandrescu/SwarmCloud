// LANE LIVE-UNUSED (part of #1030): THE CONSOLE SAYS WHEN A RUNNING BROWSER
// AGENT IS NOT USING THE LIVE BROWSER.
//
// MEASURED 2026-10-13 (task_7cda0d5335f049b4b442, "the agent is not doing the
// job"): the agent worked in a browser of its own while the live view stayed
// blank, and the tab said nothing. The gate's CDP proxy now reports when the
// agent first connected and how many connections it holds; the strip (the
// tab's, which the theater draws too) says so a minute after the first frame.
//
//   1. `agentUnusedDue`: only a RUNNING task, an open session, a gate that
//      reported its connections (null is unknown, never "not using"), no
//      connection ever, and a first frame to time it from;
//   2. the note is not shown at 59 s past the first frame, is at 60 s, links
//      to the Logs tab, and goes the moment the agent connects.
//
// MUTATIONS: drop the `agent_connections` null check (case 1's older-gate
// row turns red); show it from the first frame (case 2's 59 s turns red);
// drop the `agent_connected_at` check (case 2's connect turns red).

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  mintLiveTicket: vi.fn(),
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

const { LiveBrowserPane, agentUnusedDue, AGENT_UNUSED_AFTER_MS } = await import('../LiveBrowser')

const ID = 'task_7cda0d5335f049b4b442'
const ME = ['ada', 'example.com'].join('@')
const FRAME = Date.parse('2026-10-13T13:20:00Z')

function agent(state: Task['state'] = 'RUNNING'): Task {
  return runTask({ id: ID, state, runner_profile: 'claude-code-browser', resource_class: 'browser' })
}

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'live_att_0f3c2d1e4b5a69788796',
    state: 'open',
    closed_reason: null,
    origin: null,
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: null,
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
    agent_connected_at: null,
    agent_connections: 0,
    first_frame_at: new Date(FRAME).toISOString(),
    ...over,
  }
}

function pane(s: LiveSession, t: Task = agent()) {
  return <LiveBrowserPane task={t} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.loadLiveActions.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('1. when the note is due', () => {
  it('is a minute after the first frame, for a RUNNING agent that has never connected', () => {
    expect(AGENT_UNUSED_AFTER_MS).toBe(60_000)
    expect(agentUnusedDue(session(), 'RUNNING')).toBe(FRAME + 60_000)
  })

  it.each([
    ['the agent has connected', session({ agent_connected_at: new Date(FRAME + 5000).toISOString(), agent_connections: 1 })],
    ['the agent connected and left', session({ agent_connected_at: new Date(FRAME + 5000).toISOString(), agent_connections: 0 })],
    ['an older gate reported no connections', session({ agent_connections: null })],
    ['an older API serves none of the fields', session({ agent_connections: undefined, agent_connected_at: undefined })],
    ['no first frame was reported', session({ first_frame_at: null })],
    ['the session closed', session({ state: 'closed' })],
  ])('is never when %s', (_why, s) => {
    expect(agentUnusedDue(s, 'RUNNING')).toBeNull()
  })

  it('is never for an agent that is not RUNNING', () => {
    expect(agentUnusedDue(session(), 'STARTING')).toBeNull()
    expect(agentUnusedDue(session(), 'PARKED')).toBeNull()
  })
})

describe('2. the strip says it, and stops saying it', () => {
  it('appears 60 s after the first frame, links to the logs, and goes when the agent connects', async () => {
    vi.setSystemTime(FRAME + 59_000)
    const view = render(pane(session()))
    expect(screen.queryByTestId('agent-unused')).toBeNull()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    const note = screen.getByTestId('agent-unused')
    expect(note.textContent).toContain('The agent isn’t using this browser yet.')
    expect(note.textContent).toContain('its log shows what it is doing')
    expect(note.closest('.live-strip')).not.toBeNull()
    const link = note.querySelector('a')!
    expect(link.getAttribute('href')).toMatch(new RegExp(`/${ID}/logs$`))

    view.rerender(pane(session({ agent_connected_at: new Date(FRAME + 61_000).toISOString(), agent_connections: 1 })))
    expect(screen.queryByTestId('agent-unused')).toBeNull()
  })

  it('is not shown for an agent whose gate never reported its connections', async () => {
    vi.setSystemTime(FRAME + 120_000)
    render(pane(session({ agent_connections: null })))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.queryByTestId('agent-unused')).toBeNull()
  })
})
