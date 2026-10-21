// LANE QAFIX2-QABROWSER (part of #1680): THE FIXTURE BUILD HAS ONE AGENT WITH
// A LIVE BROWSER, AND ITS BROWSER PANE DRAWS THE LIVE STATE.
//
// MEASURED 2026-10-21 by lane QAFIX2-QAROUTE: the QA route `task-browser`
// pointed at task_a073aff5, an ordinary `claude-code` agent, and the fixture
// branch of `loadLiveSession` answered `{ live_browser: false, session: null }`
// for every task id. No fixture task ran `claude-code-browser`, so the route
// captured a pane the fixture build could never fill. Owner decision
// 2026-10-21 ~06:00Z: add one live-browser fixture agent and point the route
// at it (tests/unit/scripts/test_qa_routes.py holds the route).
//
// This reads the REAL fixture build -- `USE_FIXTURES` is true under vitest,
// and nothing in '../api' is mocked -- so it is the data the QA capture
// sweeps, not a stand-in for it. Only noVNC's canvas is faked, as in
// live.tab.test.tsx: jsdom has no canvas for it to draw into.

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Task } from '../types'

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    addEventListener() {}
    disconnect() {}
  },
}))

const { USE_FIXTURES, loadLiveSession, loadTasks } = await import('../api')
const { LiveBrowserPane, useLiveSession } = await import('../LiveBrowser')

const ORDINARY = 'task_a073aff5'

async function fixtureTasks(): Promise<Task[]> {
  const page = await loadTasks()
  if (page.status !== 'ok') throw new Error(`the fixture task list read ${page.status}`)
  return page.data.tasks
}

/** The one fixture agent on `claude-code-browser`, asserted present rather than assumed. */
function browserTask(tasks: Task[]): Task {
  const found = tasks.find((t) => t.runner_profile === 'claude-code-browser')
  expect(found, 'the fixture task list has no claude-code-browser task').toBeDefined()
  return found!
}

/** The Browser pane as the split draws it: the session read through `useLiveSession`, for an agent in flight. */
function Pane({ task }: { task: Task }) {
  const { live, reload } = useLiveSession(task.id, true)
  return (
    <div data-testid="pane" data-live-browser={live.status === 'ok' ? String(live.liveBrowser) : live.status}>
      <LiveBrowserPane task={task} live={live} reload={reload} />
    </div>
  )
}

let fetchSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchSpy = vi.fn(() => Promise.reject(new Error('the fixture build made a network call')))
  vi.stubGlobal('fetch', fetchSpy)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the fixture build has one live-browser agent', () => {
  it('lists exactly one RUNNING claude-code-browser task, and keeps task_a073aff5 the ordinary running agent', async () => {
    expect(USE_FIXTURES).toBe(true)
    const tasks = await fixtureTasks()
    const browsers = tasks.filter((t) => t.runner_profile === 'claude-code-browser')
    // MUTATION: drop the new mk(..., 'RUNNING', 'claude-code-browser', ...) row from fixtureTasks -> fails.
    expect(browsers.map((t) => t.state)).toEqual(['RUNNING'])
    expect(browsers[0]!.resource_class).toBe('browser')
    const ordinary = tasks.find((t) => t.id === ORDINARY)
    // MUTATION: turn task_a073aff5 into the browser agent instead of adding one -> fails.
    expect(ordinary?.runner_profile).toBe('claude-code')
    expect(ordinary?.state).toBe('RUNNING')
  })

  it('answers live_browser true with an open session for that task only', async () => {
    const tasks = await fixtureTasks()
    const browser = browserTask(tasks)
    const read = await loadLiveSession(browser.id)
    if (read.status !== 'ok') throw new Error(`the live session read ${read.status}`)
    // MUTATION: leave the fixture branch answering { live_browser: false, session: null } for every id -> fails.
    expect(read.data.live_browser).toBe(true)
    expect(read.data.session?.state).toBe('open')
    expect(read.data.session?.session_id).toMatch(/^live_/)
    for (const t of tasks.filter((x) => x.id !== browser.id)) {
      const other = await loadLiveSession(t.id)
      if (other.status !== 'ok') throw new Error(`the live session of ${t.id} read ${other.status}`)
      // MUTATION: answer live_browser true for every id -> fails on task_a073aff5 first.
      expect({ id: t.id, ...other.data }).toEqual({ id: t.id, live_browser: false, session: null })
    }
  })
})

describe('the Browser pane, drawn from the fixture build with no network call', () => {
  it('draws the live state for the browser agent', async () => {
    const tasks = await fixtureTasks()
    const browser = browserTask(tasks)
    const { container } = render(<Pane task={browser} />)
    await waitFor(() => expect(screen.getByTestId('pane').dataset.liveBrowser).toBe('true'))
    // MUTATION: serve the session with state 'closed' (or null) -> no strip, fails.
    const strip = await waitFor(() => {
      const s = container.querySelector('.live-strip')
      expect(s).not.toBeNull()
      return s!
    })
    expect(strip.getAttribute('data-tone')).toBe('ok')
    expect(strip.textContent).toContain('The address Chromium reports for the page the agent is on.')
    expect(strip.textContent).toContain('Nobody is in control: the agent is driving.')
    expect(container.querySelector('.live-origin-host')?.textContent).toContain('example.com')
    // MUTATION: serve agent_connections 0 with no agent_connected_at -> the "isn't using" note draws, fails.
    expect(screen.queryByTestId('agent-unused')).toBeNull()
    // MUTATION: route the fixture branch through read() -> fetch is called, fails.
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('still draws no live browser for task_a073aff5', async () => {
    const tasks = await fixtureTasks()
    const ordinary = tasks.find((t) => t.id === ORDINARY)!
    const { container } = render(<Pane task={ordinary} />)
    // MUTATION: answer live_browser true for task_a073aff5 -> fails.
    await waitFor(() => expect(screen.getByTestId('pane').dataset.liveBrowser).toBe('false'))
    expect(container.querySelector('.live-strip')).toBeNull()
    expect(container.querySelector('.live-origin-host')).toBeNull()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
