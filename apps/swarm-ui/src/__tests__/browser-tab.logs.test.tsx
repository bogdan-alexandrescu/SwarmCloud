// LANE BROWSER-TAB-LOGS (part of #2423). A Claude-in-Chrome walk over seven
// claude-code-browser agents on swarm.saga.xyz, 2026-10-26, measured the log
// beside the live browser and in the theater's Logs panel:
//
//   BT-001  it never follows: it opened at scrollTop 0 of 1632, stayed at 860
//           of a new max 889 as step 21 arrived, and had no Follow control --
//           its toolbar was the streams, the search and More.
//   BT-009  the stream switch was 238px in a 213px (1440) or 180px (1024)
//           bar, cut by `overflow: hidden`: `Run` at 1440, no Runner at 1024.
//   BT-018  a finished agent's transcript opened on its first window, ending
//           `later steps are in the next window`.
//
// MUTATIONS, one per block: put `follow` back in `BAR_SPILL` and spill it
// under More; drop the body's ResizeObserver in AgentLogs.tsx; drop the
// `.live-logs > .ag-logs, .th-left-body > .ag-logs` rule (styles/agents.css),
// or its theater half's `min-width: 641px` scope;
// drop the `max-width: 259px` streams grid; have `readTranscriptWindow` return
// the first window for a finished agent, or drop the open-at-end effect.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentLogs } = await import('../AgentLogs')

const ID = 'task_0123456789abcdef0b70'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function stream(name: LogStream['stream']): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    content: '',
    total_bytes: 0,
    offset: 0,
    returned_bytes: 0,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: new Date().toISOString(),
    age_seconds: 3,
  }
}

function logs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: new Date().toISOString(), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    streams: [stream('agent_stderr'), stream('stdout'), stream('stderr')],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

function step(n: number): TranscriptStep {
  return {
    id: `L${n}:0`,
    line_offset: n * 100,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: `step ${n}`,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: null,
  }
}

/** One window of a transcript object `total` bytes long, `size` bytes from `offset`. */
function win(steps: number[], at: { offset: number; size: number; total: number; source: 'live' | 'final' }): TaskTranscript {
  const end = at.offset + at.size
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: new Date().toISOString(), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: at.source,
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/agent_stdout.log`,
      object_updated_at: new Date().toISOString(),
      age_seconds: 3,
      total_bytes: at.total,
      offset: at.offset,
      returned_bytes: at.size,
      next_offset: end < at.total ? end : null,
      truncated: end < at.total,
      tail_window: null,
    },
    format: 'claude-stream-json',
    steps: steps.map(step),
    complete: at.offset === 0 && end >= at.total && at.source === 'final',
    window_starts_mid_stream: at.offset > 0,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  }
}

function running(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code-browser', attempt_count: 1, current_generation: 1, completed_at: null, ...over })
}
const finished = () => running({ state: 'SUCCEEDED', completed_at: new Date(Date.now() - 3_600_000).toISOString() })

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

/**
 * The log's body as a browser lays it out: 200px tall over `content` px, its
 * `scrollTop` kept. jsdom lays nothing out and keeps no scroll position.
 */
function bodyLaidOut(content: () => number): () => void {
  const proto = HTMLElement.prototype
  const top = new WeakMap<Element, number>()
  const isBody = (el: HTMLElement) => el.classList.contains('ag-logs-body')
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) {
      return isBody(this) ? content() : 0
    },
  })
  Object.defineProperty(proto, 'clientHeight', {
    configurable: true,
    get(this: HTMLElement) {
      return isBody(this) ? 200 : 0
    },
  })
  Object.defineProperty(proto, 'scrollTop', {
    configurable: true,
    get(this: HTMLElement) {
      return top.get(this) ?? 0
    },
    set(this: HTMLElement, v: number) {
      top.set(this, Math.max(0, Math.min(v, isBody(this) ? content() - 200 : 0)))
    },
  })
  return () => {
    for (const k of ['scrollHeight', 'clientHeight', 'scrollTop']) delete (proto as unknown as Record<string, unknown>)[k]
  }
}

/** A ResizeObserver whose callbacks the test fires, for the elements they observe. */
const observers: { cb: ResizeObserverCallback; targets: Element[] }[] = []
class TestResizeObserver {
  private entry: { cb: ResizeObserverCallback; targets: Element[] }
  constructor(cb: ResizeObserverCallback) {
    this.entry = { cb, targets: [] }
    observers.push(this.entry)
  }
  observe(el: Element): void {
    this.entry.targets.push(el)
  }
  unobserve(): void {}
  disconnect(): void {
    this.entry.targets = []
  }
}
function resized(el: Element): void {
  for (const o of observers) if (o.targets.includes(el)) o.cb([], o as unknown as ResizeObserver)
}

beforeEach(() => {
  observers.length = 0
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  api.loadTranscript.mockResolvedValue(ok(win([1, 2, 3], { offset: 0, size: 1000, total: 1000, source: 'live' })))
  api.loadTaskLogs.mockResolvedValue(ok(logs()))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('BT-001: the log beside the browser follows, and says so', () => {
  it('keeps Follow in the row of a bar too narrow for anything else, never only under More', async () => {
    // The ~210px bar beside the browser: every spillable control overflows.
    const proto = HTMLElement.prototype
    Object.defineProperty(proto, 'scrollWidth', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('ag-logs-bar') && !this.classList.contains('is-wrap') ? 400 : 0
      },
    })
    Object.defineProperty(proto, 'clientWidth', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('ag-logs-bar') ? 210 : 0
      },
    })
    try {
      render(<AgentLogs task={running()} />)
      const bar = dock().querySelector<HTMLElement>('.ag-logs-bar')!
      await waitFor(() => expect(bar.classList.contains('is-wrap')).toBe(true))
      const follow = within(bar).getByRole('button', { name: /^Following/ })
      expect(follow.closest('details'), 'Follow is only under More').toBeNull()
      expect(follow.getAttribute('aria-pressed')).toBe('true')
      expect(dock().dataset.follow).toBe('on')
    } finally {
      delete (proto as unknown as Record<string, unknown>).scrollWidth
      delete (proto as unknown as Record<string, unknown>).clientWidth
    }
  })

  it('brings a following body back to its newest line when the body is resized (shown, toggled, the column moved)', async () => {
    vi.stubGlobal('ResizeObserver', TestResizeObserver)
    let content = 1200
    const restore = bodyLaidOut(() => content)
    try {
      render(<AgentLogs task={running()} />)
      await waitFor(() => expect(dock().textContent).toContain('step 3'))
      const body = dock().querySelector<HTMLElement>('.ag-logs-body')!
      // The column was hidden when the read landed; shown, the body is laid
      // out at its top, as measured (scrollTop 0 of max 1632).
      body.scrollTop = 0
      content = 1832
      act(() => resized(body))
      expect(body.scrollTop, 'the body stayed at its top').toBe(1832 - 200)
      expect(dock().dataset.follow, 'the log’s own scroll paused it').toBe('on')
    } finally {
      restore()
    }
  })

  it('beside the browser and in the theater, the log is its column’s height, so its body is the scroller', () => {
    const env: CascadeEnv = { width: 1440 }
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="live-logs"><section class="ag-logs"></section></div>' +
      '<div class="th-left-body"><section class="ag-logs"></section></div>' +
      '<div class="ag-split-pane"><section class="ag-logs"></section></div>'
    document.body.appendChild(host)
    try {
      const [beside, theater, tab] = Array.from(host.querySelectorAll('.ag-logs'))
      for (const el of [beside!, theater!]) {
        expect(painted(el, 'height', env)).toBe('100%')
        // The border inside that height, or the column scrolls 2px.
        expect(painted(el, 'box-sizing', env)).toBe('border-box')
        expect(painted(el, 'margin-bottom', env)).toBe('0')
      }
      // The Logs tab's own flex layout is untouched.
      expect(painted(tab!, 'height', env)).toBeNull()
    } finally {
      host.remove()
    }
  })

  it('in the phone theater, the log keeps its content height, so the 51px Logs panel scrolls a readable log', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="th-left-body"><section class="ag-logs"></section></div>'
    document.body.appendChild(host)
    try {
      const theater = host.querySelector('.ag-logs')!
      // Measured on the review render at 390: the panel was 51px, the log
      // 35px and its body 8-9px.
      expect(painted(theater, 'height', { width: 390 })).not.toBe('100%')
      expect(painted(theater, 'height', { width: 640 })).not.toBe('100%')
      expect(painted(theater, 'height', { width: 641 })).toBe('100%')
    } finally {
      host.remove()
    }
  })
})

describe('BT-009: every stream can be chosen in a narrow bar', () => {
  const host = () => {
    const h = document.createElement('div')
    h.innerHTML = '<div class="ag-logs-bar is-wrap"><div class="c-seg is-sm ag-logs-streams"></div></div>'
    document.body.appendChild(h)
    return h
  }

  it.each([
    ['beside the browser at 1440', { width: 1440, container: 213 }],
    ['beside the browser at 1024', { width: 1024, container: 180 }],
  ] as [string, CascadeEnv][])('%s: the streams are two rows the bar’s width, not one row cut off', (_, env) => {
    const h = host()
    try {
      const seg = h.querySelector('.ag-logs-streams')!
      expect(painted(seg, 'display', env)).toBe('grid')
      expect(painted(seg, 'grid-template-columns', env)).toBe('repeat(2, minmax(0, 1fr))')
      expect(painted(seg, 'height', env)).toBe('auto')
    } finally {
      h.remove()
    }
  })

  it('in the Logs tab’s wide bar and on a phone, the streams stay one row', () => {
    const h = host()
    try {
      const seg = h.querySelector('.ag-logs-streams')!
      for (const env of [{ width: 1440, container: 630 }, { width: 390, container: 334 }] as CascadeEnv[]) {
        expect(painted(seg, 'display', env)).not.toBe('grid')
      }
    } finally {
      h.remove()
    }
  })
})

describe('BT-018: a finished agent’s transcript opens on how it finished', () => {
  const FIRST = win([1, 2, 3], { offset: 0, size: 1000, total: 3000, source: 'final' })
  const NEWEST = win([20, 21, 22], { offset: 2000, size: 1000, total: 3000, source: 'final' })
  beforeEach(() => {
    api.loadTranscript.mockImplementation(async (_id: string, o: { offset?: number }) => ok(o.offset === 2000 ? NEWEST : FIRST))
  })

  it('reads the window that ends at the last byte, and opens scrolled to its end', async () => {
    const restore = bodyLaidOut(() => 900)
    try {
      render(<AgentLogs task={finished()} />)
      await waitFor(() => expect(dock().textContent).toContain('step 22'))
      expect(api.loadTranscript).toHaveBeenCalledWith(ID, expect.objectContaining({ offset: 2000, source: 'final' }))
      expect(within(dock()).queryByText('step 1')).toBeNull()
      expect(dock().textContent).not.toMatch(/later steps are in the next window/)
      expect(dock().textContent).toMatch(/earlier steps are not in this window/)
      expect(dock().querySelector<HTMLElement>('.ag-logs-body')!.scrollTop).toBe(700)
    } finally {
      restore()
    }
  })

  it('reads its first window when the reader asks, and the newest again', async () => {
    render(<AgentLogs task={finished()} />)
    await waitFor(() => expect(dock().textContent).toContain('step 22'))
    fireEvent.click(within(dock()).getByRole('button', { name: 'Read from the first window' }))
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
    expect(dock().textContent).not.toContain('step 22')
    expect(within(dock()).getByRole('button', { name: 'next window' })).toBeTruthy()
    fireEvent.click(within(dock()).getByRole('button', { name: 'Read the newest window' }))
    await waitFor(() => expect(dock().textContent).toContain('step 22'))
  })

  it('a running agent’s live tail is read as it was: no second read at an offset, no window choice', async () => {
    render(<AgentLogs task={running()} />)
    await waitFor(() => expect(dock().textContent).toContain('step 3'))
    // Read again once the attempts land and name the attempt; never at an offset.
    for (const [, o] of api.loadTranscript.mock.calls as [string, { offset?: number }][]) expect(o.offset).toBeUndefined()
    expect(within(dock()).queryByRole('button', { name: /first window/ })).toBeNull()
  })
})
