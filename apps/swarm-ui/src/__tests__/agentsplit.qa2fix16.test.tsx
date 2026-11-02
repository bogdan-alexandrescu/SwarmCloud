// THE BROWSER TAB SAYS LIVE ON A PICTURE (QA2-FIX-16, visual QA pass 2
// ovag2-12; part of #2694). The fixture build serves an open session and
// mints no viewer ticket, so the Browser tab carried LIVE over an empty
// frame. With the Browser pane open, the tab strip says LIVE once the pane
// reports a picture (`LiveBrowserPane`'s `onPicture`); off that pane, the
// session read is all there is, and it says LIVE as before.
//
// The pane is stood in for here: what it reports is LiveBrowser.tsx's own
// test (qa2.fix16.livebrowser.test.tsx); this file holds what the split
// does with the report.
//
// MUTATION: hand `paneTabs` the read's `sessionOpen` again and the first
// case finds LIVE with no picture.

import { act, render, screen, waitFor, within } from '@testing-library/react'
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
  loadTaskEventsPage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

/** The pane's report, as the test makes it: `picture.tell(true)` is noVNC's first frame. */
const picture = vi.hoisted(() => ({ tell: null as ((shown: boolean) => void) | null }))

vi.mock('../LiveBrowser', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../LiveBrowser')>()
  return {
    ...actual,
    LiveBrowserPane: ({ onPicture }: { onPicture?: (shown: boolean) => void }) => {
      picture.tell = onPicture ?? null
      return <div data-testid="pane-stand-in" />
    },
  }
})

const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef0716'
const ME = ['ada', 'example.com'].join('@')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    attempt_count: 1,
    completed_at: null,
    result_summary: null,
  })
}

function session(): LiveSession {
  return {
    session_id: 'ls_fix16',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
  }
}

function split(pane: 'browser' | 'logs') {
  return render(
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} theater={false} artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${ID}`} />
    </div>,
  )
}

const label = (t: HTMLElement) => t.querySelector('.c-tab-label')?.textContent ?? t.textContent ?? ''
const browserTab = () => within(screen.getByRole('tablist', { name: 'Agent panes' })).getAllByRole('tab').find((t) => label(t) === 'Browser')!

const SPLIT = { timeout: 12_000 }

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  picture.tell = null
  api.loadTask.mockResolvedValue(ok(agent()))
  api.loadLiveSession.mockResolvedValue(ok<LiveRead>({ live_browser: true, session: session() }))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.unstubAllGlobals()
  delete document.documentElement.dataset.agentList
})

describe('ovag2-12: the Browser tab says LIVE on a picture, not on the read alone', () => {
  it('on the Browser pane, an open session with no picture is not LIVE until a frame is drawn, and again not once it goes', async () => {
    split('browser')
    await screen.findByTestId('pane-stand-in', undefined, SPLIT)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalled(), SPLIT)
    await waitFor(() => expect(picture.tell).not.toBeNull(), SPLIT)
    act(() => picture.tell!(false))
    expect(browserTab().textContent).not.toContain('LIVE')
    act(() => picture.tell!(true))
    await waitFor(() => expect(browserTab().textContent).toContain('LIVE'), SPLIT)
    act(() => picture.tell!(false))
    await waitFor(() => expect(browserTab().textContent).not.toContain('LIVE'), SPLIT)
  })

  it('off the Browser pane, an open session says LIVE on the read', async () => {
    split('logs')
    await waitFor(() => expect(browserTab().textContent).toContain('LIVE'), SPLIT)
    expect(screen.queryByTestId('pane-stand-in')).toBeNull()
  })
})
