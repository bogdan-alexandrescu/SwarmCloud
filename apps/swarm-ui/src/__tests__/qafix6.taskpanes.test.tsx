/**
 * LANE QAFIX6-TASKPANES (part of #1680, console visual QA run 6,
 * wf_31aca00c977e4d37a935, the fixture build at 1440 and 390, light and
 * dark, and the earlier runs' open boxes). An agent's pane tabs, its Logs
 * toolbar, the Browser tab's frame and its start-up.
 *
 *   a  box 6343755826       each pane tab's text is its name: a tab whose
 *                           count is not known drew `—` and was named
 *                           `Attempts, count not known`.
 *   b  6:task-002           a child agent's Children tab (drawn by address,
 *                           c04) drew `—` against the pane's `none`: children
 *                           are one level deep, so it has none.
 *   d  box 6298214288       the Browser tab is on every pane (verified: held).
 *   e  box 6298218679       the pane strip's tabs keyboard pattern (verified:
 *                           held).
 *   f  6:flow-find-failure-desktop-04  `Log done` was a bordered pill, the
 *                           shape of a button.
 *   g  box 6329547758       the paused stamp is on the page's 24-hour clock
 *                           (verified: held).
 *   h  6:task-006           the live-view frame draws its own bordered box in
 *                           every state, not only through the screen it holds.
 *   i  QA-086               the start-up's two sentences are two sentences in
 *                           the DOM text too: `starting. Reading`.
 *
 * c (the overflow names what it hides) and the `Error 1 of 1` half of f
 * contradict assertions in shared test files; they are questions, not code
 * (questions.json, and the PR body).
 *
 * Layout through `painted` (marks.ts): jsdom applies no stylesheet.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadStartup: vi.fn(),
  mintLiveTicket: vi.fn(),
  openLiveSocket: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

/** noVNC's RFB, as far as LiveBrowser.tsx touches it (as live.v1.test.tsx draws it). */
const rfb = vi.hoisted(() => {
  class FakeRfb {
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    readonly canvas: HTMLCanvasElement
    constructor(readonly target: HTMLElement) {
      this.canvas = document.createElement('canvas')
      target.appendChild(this.canvas)
    }
    addEventListener() {}
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

const { AgTabsEdge, paneTabs } = await import('../agent-split/tabs')
const { AgentLogs } = await import('../AgentLogs')
const { LiveBrowserPane } = await import('../LiveBrowser')
const { StartupProgress } = await import('../StartupProgress')

const WIDE: CascadeEnv = { width: 1440 }
const ID = 'task_0123456789abcdef0q61'
const MIN = 60_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code', completed_at: null, ...over })
}

/** What axe compares (label-content-name-mismatch): lower case, no punctuation, one space. */
function words(s: string | null | undefined): string {
  return (s ?? '')
    .toLowerCase()
    .replace(/[\p{P}\p{S}]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

type Tabs = ReturnType<typeof paneTabs>

/** The strip as AgentSplit.tsx hands it to AgTabsEdge. */
function strip(tabs: Tabs, current = 'detail', onSelect: (key: string) => void = () => {}) {
  render(
    <div className="drawer ctl-drawer ag-split">
      <AgTabsEdge
        current={current}
        onSelect={onSelect}
        tabs={tabs.map((x) => ({
          key: x.id,
          label: x.label,
          ...((x.id === 'detail' && x.count === null) || x.id === 'logs' || (x.id === 'browser' && x.count === null)
            ? {}
            : { count: x.count, why: x.say ?? undefined }),
        }))}
      />
    </div>,
  )
  return screen.getByRole('tablist', { name: 'Agent panes' })
}

const base = '/agents/live/x'

// ---------------------------------------------------------------- a

describe('a (box 6343755826): each pane tab’s text is its name', () => {
  it('holds for every tab, a count not known among them', () => {
    // Attempts and Checkpoints not read yet: their badges draw `—`.
    const tabs = paneTabs({ task: row(), base, hasBrowser: false, sessionOpen: false, childCount: null, runs: null, ckpt: null, ckptCount: null })
    const all = within(strip(tabs)).getAllByRole('tab')
    expect(all.length).toBeGreaterThanOrEqual(5)
    // MUTATION: name a tab whose count is not known `Attempts, count not known` again (tabName) -- its text is `Attempts —`.
    expect(all.map((t) => words(t.getAttribute('aria-label') ?? t.textContent))).toEqual(all.map((t) => words(t.textContent)))
  })
})

// ---------------------------------------------------------------- b

describe('b (6:task-002): the Children count once there is nothing to read', () => {
  it('a child agent on /children reads 0, and its name says 0, as the pane says none', () => {
    const child = row({ parent_task_id: 'task_parent0123456789ab' } as Partial<Task>)
    const tabs = paneTabs({ task: child, base, hasBrowser: false, sessionOpen: false, childCount: null, runs: 1, ckpt: null, ckptCount: 0, pane: 'children' })
    const tab = within(strip(tabs, 'children')).getByRole('tab', { name: /^Children/ })
    // MUTATION: hand the tab the split's unread count (`childCount`, null: a child is never read) -- `Children —`.
    expect([tab.querySelector('em')?.textContent, tab.getAttribute('aria-label')]).toEqual(['0', 'Children, 0'])
  })
})

// ---------------------------------------------------------------- d

describe('d (box 6298214288): a browser agent keeps its Browser tab on every pane', () => {
  it('draws Browser second on /logs', () => {
    const t = row({ runner_profile: 'claude-code-browser' })
    const tabs = paneTabs({ task: t, base, hasBrowser: true, sessionOpen: true, childCount: 0, runs: 1, ckpt: null, ckptCount: 0, pane: 'logs' })
    const names = within(strip(tabs, 'logs')).getAllByRole('tab').map((x) => x.querySelector('.c-tab-label')?.textContent)
    // MUTATION: offer Browser only when the address names it (`pane === 'browser'`) -- Logs drops it.
    expect(names.slice(0, 3)).toEqual(['Details', 'Browser', 'Logs'])
  })
})

// ---------------------------------------------------------------- e

describe('e (box 6298218679): the pane strip is a tablist to the keyboard', () => {
  it('ArrowRight from the first tab focuses and selects the second', () => {
    const picked = vi.fn()
    const tabs = paneTabs({ task: row(), base, hasBrowser: false, sessionOpen: false, childCount: 0, runs: 1, ckpt: null, ckptCount: 0 })
    const all = within(strip(tabs, 'detail', picked)).getAllByRole('tab')
    all[0]!.focus()
    fireEvent.keyDown(all[0]!, { key: 'ArrowRight' })
    // MUTATION: drop AgTabsEdge's onKeyDown -- the arrow moves nothing and opens nothing.
    expect([document.activeElement === all[1], picked.mock.calls.at(-1)?.[0], all.map((t) => t.getAttribute('tabindex'))]).toEqual([
      true,
      tabs[1]!.id,
      all.map((_, i) => (i === 0 ? '0' : '-1')),
    ])
  })
})

// ---------------------------------------------------------- Logs fixtures

function stream(name: LogStream['stream']): LogStream {
  return {
    stream: name,
    source: 'live',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    content: 'a\nb\n',
    total_bytes: 4,
    offset: 0,
    returned_bytes: 4,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: new Date().toISOString(),
    age_seconds: 3,
  } as LogStream
}

function logs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: ago(5), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    streams: [stream('agent_stderr'), stream('stdout'), stream('stderr')],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  } as TaskLogs
}

function transcript(): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: ago(5), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: 'live',
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/agent_stdout.log`,
      object_updated_at: ago(1),
      age_seconds: 60,
      total_bytes: 100,
      offset: 0,
      returned_bytes: 100,
      next_offset: null,
      truncated: false,
      tail_window: null,
    },
    format: 'claude-stream-json',
    steps: [],
    complete: true,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  } as TaskTranscript
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

describe('Logs toolbar', () => {
  beforeEach(() => {
    localStorage.clear()
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', task_id: ID })] }))
    api.loadTaskLogs.mockResolvedValue(ok(logs()))
    api.loadTranscript.mockResolvedValue(ok(transcript()))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  // -------------------------------------------------------------- f

  describe('f (6:flow-find-failure-desktop-04): `Log done` is a status, not a button', () => {
    it('draws no button outline around the finished chip', async () => {
      render(<AgentLogs task={row({ state: 'FAILED', completed_at: ago(1) })} />)
      const chip = await waitFor(() => dock().querySelector<HTMLElement>('.ag-logs-final')!)
      // MUTATION: drop the `.ag-logs-final.is-mark` rule (live.css) -- the pill's 1px border and 999px corners are back.
      expect([painted(chip, ['border-top-style', 'border-style', 'border'], WIDE), chip.querySelector('[data-tone]') !== null]).toEqual([
        'none',
        true,
      ])
    })
  })

  // -------------------------------------------------------------- g

  describe('g (box 6329547758): one clock', () => {
    it('stamps a pause at 21:25:53 as `21:25:53`', async () => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date(2026, 9, 23, 21, 25, 53))
      render(<AgentLogs task={row()} />)
      fireEvent.click(within(dock()).getByRole('group', { name: 'Which log' }).querySelector('button')!)
      await waitFor(() => expect(dock().querySelector('.ag-logs-facts')?.textContent).toMatch(/source/))
      fireEvent.click(within(dock()).getByRole('button', { name: /^Following/ }))
      // MUTATION: print `new Date(frozen.at).toLocaleTimeString()` again (AgentLogs.tsx) -- `9:25:53 PM`.
      await waitFor(() => expect(dock().querySelector('.ag-logs-paused time')?.textContent).toBe('21:25:53'))
    })
  })
})

// ---------------------------------------------------------------- h

const ME = ['ada', 'example.com'].join('@')

function session(preset: string, orientation: 'portrait' | 'landscape'): LiveSession {
  return {
    session_id: 'ls_q6',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset, orientation, touch: false },
    presets: [
      { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
      { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
    ],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
  } as LiveSession
}

describe('h (6:task-006): the live view’s frame is a bordered box in every state', () => {
  beforeEach(() => {
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
    api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
    api.loadStartup.mockReturnValue(new Promise(() => {}))
  })

  it('connecting, connected and failing, on a desktop and on a phone', async () => {
    const t = row({ runner_profile: 'claude-code-browser', resource_class: 'browser' })
    const seen: string[] = []
    const draw = async (preset: string, orientation: 'portrait' | 'landscape', conn: 'connecting' | 'connected' | 'failing') => {
      api.mintLiveTicket.mockReset()
      api.openLiveSocket.mockReset()
      if (conn === 'connecting') api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
      else if (conn === 'failing') api.mintLiveTicket.mockResolvedValue({ status: 'error', error: { message: 'no ticket' } })
      else {
        api.mintLiveTicket.mockResolvedValue(ok({ session_id: 'ls_q6', ticket: 't'.repeat(8), expires_at: new Date(Date.now() + 30_000).toISOString() }))
        api.openLiveSocket.mockImplementation(async (_s: string, _t: string, onOpen: (ws: WebSocket) => void) => {
          onOpen({ readyState: 1, close() {} } as unknown as WebSocket)
        })
      }
      const { unmount } = render(<LiveBrowserPane task={t} live={{ status: 'ok', liveBrowser: true, session: session(preset, orientation) }} reload={() => {}} />)
      await act(async () => {
        await Promise.resolve()
      })
      const f = document.querySelector<HTMLElement>('.live-frame')!
      seen.push(`${preset} ${conn}: ${f.classList.contains('is-bordered')}`)
      unmount()
    }
    for (const [p, o] of [
      ['desktop', 'landscape'],
      ['iphone-15', 'portrait'],
    ] as const) {
      for (const conn of ['connecting', 'connected', 'failing'] as const) await draw(p, o, conn)
    }
    // MUTATION: drop `is-bordered` from the frame (LiveBrowser.tsx) -- a desktop frame draws no box of its own.
    expect(seen).toEqual(seen.map((s) => s.replace(/: \w+$/, ': true')))
    expect(seen).toHaveLength(6)
  })

  it('the bordered desktop frame draws its rounded box in the stylesheet', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="live-stage"><div class="live-frame is-desktop is-bordered"></div></div>'
    document.body.appendChild(host)
    try {
      const f = host.querySelector('.live-frame')!
      // MUTATION: drop the `.live-frame.is-desktop.is-bordered` rule (live.css) -- an empty frame is drawn as nothing.
      expect([painted(f, 'box-shadow', WIDE), painted(f, 'border-radius', WIDE)]).toEqual(['inset 0 0 0 1px var(--line)', 'var(--radius)'])
    } finally {
      host.remove()
    }
  })
})

// ---------------------------------------------------------------- i

describe('i (QA-086): the start-up’s two sentences', () => {
  it('reads `starting. Reading`, with a space between them', () => {
    api.loadStartup.mockReturnValue(new Promise(() => {}))
    api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
    render(<StartupProgress taskId="task_startup0123456789ab" />)
    // MUTATION: drop the space between the two paragraphs (StartupProgress.tsx) -- `starting.Reading`.
    expect(screen.getByTestId('startup-progress').textContent).toContain('starting. Reading')
  })
})
