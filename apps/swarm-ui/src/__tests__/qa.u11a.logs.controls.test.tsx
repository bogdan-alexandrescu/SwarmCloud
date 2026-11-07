// SPLIT (part of #2688, 2026-11-07): this file, `qa.u11a.logs.test.tsx`, `qa.u11a.logs.widths.test.tsx`, `qa.u11a.logs.running.test.tsx`
// were one file, 167 s alone with three workers, over the ~150 s line under a
// CI shard. Split along its describe blocks; each file keeps its own fixtures
// and shares no state.

/**
 * U11a (owner QA re-run, 2026-10-04): LOGS ARE A TAB.
 *
 * The owner does not want the log component overlapping the agent details.
 * The bottom dock -- its strip, its resize handle, `Open full` and the page
 * scroll it caused (D7) -- is gone; the log is a full-height Logs tab at
 * `/agents/<tab>/<id>/logs`, with all four streams visible (D13 hid them in a
 * scrolling pill row). An agent opens on Details, running or finished (#1646,
 * 2026-10-19; a running one opened on Logs from 2026-10-04). Escape in the log's More closes the menu and stops there (N11).
 *
 * MUTATIONS: render a log element outside the pane or position one over it
 * (`position: fixed|absolute|sticky` on `.ag-logs`/`.ag-loglast`); drop a
 * stream from the segmented control or let it spill into More; drop the
 * default-tab effect or send a running, finished or waiting agent to Logs; drop `onMoreKey`'s
 * `stopPropagation`; restore `flex-wrap: wrap` on the bar or drop the spill
 * effect; drop the `+ 16px` from `top` or the `- 16px`. Each turns a case red.
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { landed } from './reads'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { AgentLogs, BAR_SPILL } = await import('../AgentLogs')
const { resetListSnap } = await import('../listSnap')

const WIDE: CascadeEnv = { width: 1440 }
const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function stream(name: LogStream['stream'], content: string): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    content,
    total_bytes: content.length,
    offset: 0,
    returned_bytes: content.length,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: new Date().toISOString(),
    age_seconds: 3,
  }
}

function logs(streams: LogStream[]): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: {
      status: 'latest',
      known: true,
      generation: 1,
      created_at: new Date(Date.now() - 60_000).toISOString(),
      completed_at: null,
      exit_code: null,
    },
    read_at: new Date().toISOString(),
    streams,
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

function running(over: Partial<Task> = {}): Task {
  // `mock` opens on the runner's two streams.
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'mock', attempt_count: 1, completed_at: null, ...over })
}

/**
 * The header's read and the Details read of one task. The last log line is in
 * the Details tab's leading card (agent-details-v3.html A), so it is drawn
 * once the Details read lands.
 */
function serve(t: Task) {
  api.loadTask.mockResolvedValue(ok(t))
  api.loadAgentRun.mockResolvedValue(
    ok({ task: t, events: [], eventsDetail: null, attempts: [], attemptsDetail: null, classes: null, classesDetail: null, classesRouteMissing: false }),
  )
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

/** The split with a router that moves the pane as App's does. */
function Routed({ start, onGo }: { start: Pane; onGo: (to: string) => void }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    onGo(to)
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  api.loadTranscript.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no transcript' } })
  api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'one\ntwo\n'), stream('stderr', 'three\n')])))
  api.loadChildren.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  delete document.documentElement.dataset.agentList
  window.history.replaceState(null, '', '/')
})

describe('N11: Escape in More closes the menu, and only the menu', () => {
  it('folds More, keeps the agent open, and the next Escape closes the agent', async () => {
    serve(running())
    const go = vi.fn()
    render(<Routed start="logs" onGo={go} />)
    await landed(api.loadTask)
    const region = screen.getByRole('region', { name: 'Logs' })
    const more = region.querySelector<HTMLDetailsElement>('details.ag-logs-more')!
    more.open = true
    const summary = more.querySelector<HTMLElement>('summary')!
    summary.focus()
    fireEvent.keyDown(summary, { key: 'Escape' })
    expect(more.open, 'More stayed open').toBe(false)
    expect(go, 'Escape in More closed the agent').not.toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'Logs' })).toBe(region)
    expect(document.activeElement).toBe(summary)
    // With the menu closed, Escape is the split's again.
    fireEvent.keyDown(summary, { key: 'Escape' })
    expect(go).toHaveBeenCalledWith('work/running/live')
  })

  it('closes the menu from a control inside it too', async () => {
    serve(running())
    const go = vi.fn()
    render(<Routed start="logs" onGo={go} />)
    await landed(api.loadTask)
    const region = screen.getByRole('region', { name: 'Logs' })
    const more = region.querySelector<HTMLDetailsElement>('details.ag-logs-more')!
    more.open = true
    const inside = more.querySelector<HTMLElement>('.ag-logs-menu')!
    fireEvent.keyDown(inside, { key: 'Escape' })
    expect(more.open).toBe(false)
    expect(go).not.toHaveBeenCalled()
  })
})

/**
 * THE ROW'S WIDTH, BY A STATED MODEL (jsdom lays nothing out). Each control is
 * its visible text at 7px a character (the bar's 12px type, generously) plus
 * its own chrome -- a small button or More's summary 22px of padding and
 * border, More's caret 14px, a segment 24px, a chip 12px, a key hint 10px, the
 * attempt select 30px beside its text -- and the search is its `min-width`
 * floor from the shipped sheet. Visibility comes from the cascade at the
 * stated container width, so short labels count only where the sheet applies
 * them.
 */
const CH = 7
function shown(el: Element, env: CascadeEnv): boolean {
  return painted(el, 'display', env) !== 'none' && painted(el, 'position', env) !== 'absolute'
}
function textWidth(el: Element, env: CascadeEnv): number {
  let w = 0
  for (const n of Array.from(el.childNodes)) {
    if (n.nodeType === Node.TEXT_NODE) w += (n.textContent ?? '').length * CH
    else if (n instanceof HTMLSelectElement) w += (n.selectedOptions[0]?.textContent ?? '').length * CH + 30
    else if (n instanceof Element && shown(n, env)) w += textWidth(n, env) + (n.tagName === 'KBD' ? 10 : 0)
  }
  return w
}
function itemWidth(c: Element, env: CascadeEnv): number {
  if (c.matches('.ag-logs-search')) return Number.parseFloat(painted(c, 'min-width', env) ?? 'NaN')
  if (c.matches('.c-seg')) return Array.from(c.children).reduce((t, b) => t + textWidth(b, env) + 24, 0)
  if (c.matches('details')) return textWidth(c.querySelector(':scope > summary')!, env) + 22 + 14
  if (c.matches('.c-btn')) return textWidth(c, env) + 22
  if (c.matches('.art-masked, .ag-logs-final')) return textWidth(c, env) + 12
  return textWidth(c, env)
}
/**
 * The widest LINE the bar draws. An item the sheet gives a whole line
 * (`flex-basis: 100%` where the bar may wrap) is a line of its own; the
 * rest are one line, as the row never wraps them.
 */
function rowWidth(bar: Element, env: CascadeEnv): number {
  const items = Array.from(bar.children).filter((c) => shown(c, env))
  const wraps = painted(bar, 'flex-wrap', env) === 'wrap'
  const own = (c: Element) => wraps && /(^|\s)100%/.test(painted(c, ['flex', 'flex-basis'], env) ?? '')
  const line = (xs: Element[]) => 24 + 8 * Math.max(0, xs.length - 1) + xs.reduce((t, c) => t + itemWidth(c, env), 0)
  const lines = [items.filter((c) => !own(c)), ...items.filter(own).map((c) => [c])]
  return Math.max(...lines.map(line))
}

/** Lays the bar out by `rowWidth` in a box `box` px wide; restores jsdom after. */
function laidOut(box: number, env: CascadeEnv): () => void {
  const proto = HTMLElement.prototype
  Object.defineProperty(proto, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (!this.classList.contains('ag-logs-bar')) return 0
      return this.classList.contains('is-wrap') ? box : rowWidth(this, env)
    },
  })
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-logs-bar') ? box : 0
    },
  })
  return () => {
    delete (proto as unknown as Record<string, unknown>).scrollWidth
    delete (proto as unknown as Record<string, unknown>).clientWidth
  }
}

/**
 * THE 380px SPLIT AT 1440 WIDE: 1440 - the 84px spine - the 236px panel - two
 * 24px page gutters - the 24px gap - the 380px list leaves a 668px column,
 * and the drawer's padding and the log's border leave its bar ~630px.
 */
const SPLIT_380: CascadeEnv = { width: 1440, container: 630 }
const PHONE: CascadeEnv = { width: 390, container: 360 }

describe('the log’s controls fit one row', () => {
  it('at the 380px split: every stream, Follow and Error in the row, the rest under More, nothing past the edge', () => {
    const restore = laidOut(630, SPLIT_380)
    try {
      render(<AgentLogs task={running()} />)
      const bar = screen.getByRole('region', { name: 'Logs' }).querySelector<HTMLElement>('.ag-logs-bar')!
      expect(bar.classList.contains('is-wrap'), 'the row wrapped').toBe(false)
      expect(painted(bar, 'flex-wrap', SPLIT_380)).toBe('nowrap')
      expect(rowWidth(bar, SPLIT_380), 'the row is wider than its column').toBeLessThanOrEqual(630)
      expect(Number(bar.dataset.spill)).toBeLessThan(BAR_SPILL.error)
      // Measured before the read lands, so Error says it has not counted yet
      // (QA1 overview-agents-32): `Errors · reading`, as long as `No error matched`.
      for (const name of [/^Following/, /^Errors · reading/]) {
        expect(within(bar).getByRole('button', { name }).closest('details'), `${name} is under More`).toBeNull()
      }
      const group = within(bar).getByRole('group', { name: 'Which log' })
      expect(within(group).getAllByRole('button')).toHaveLength(4)
      // What spilled is under More, not lost.
      const more = bar.querySelector<HTMLElement>('details.ag-logs-more')!
      expect(within(bar).getAllByRole('combobox', { name: 'Attempt, by generation' })).toHaveLength(1)
      expect(within(bar).getAllByRole('button', { name: /^Wrap/ })).toHaveLength(1)
      expect(more.querySelector('select, .c-btn')).not.toBeNull()
    } finally {
      restore()
    }
  })

  it('on a 390px phone: the same tab, full width, controls spilled into More until the row fits', () => {
    const restore = laidOut(360, PHONE)
    try {
      render(<AgentLogs task={running()} />)
      const bar = screen.getByRole('region', { name: 'Logs' }).querySelector<HTMLElement>('.ag-logs-bar')!
      expect(bar.classList.contains('is-wrap'), 'the spill ran out and the row wrapped').toBe(false)
      // The streams are a line of their own; the controls one row under them.
      expect(painted(bar.querySelector('.ag-logs-streams')!, ['flex', 'flex-basis'], PHONE)).toMatch(/100%/)
      expect(rowWidth(bar, PHONE), 'the row is wider than the phone').toBeLessThanOrEqual(360)
      // Every control is in the row or under More: none is lost.
      expect(within(bar).getAllByRole('button', { name: /^Following/ })).toHaveLength(1)
      expect(within(bar).getAllByRole('button', { name: /^Errors · reading/ })).toHaveLength(1)
      expect(within(within(bar).getByRole('group', { name: 'Which log' })).getAllByRole('button')).toHaveLength(4)
    } finally {
      restore()
    }
  })
})

describe('D7: the column ends at the scrollport, above the API-reads line', () => {
  it('sticks 16px down and is 16px shorter, the page padding it sits under', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="sk-main"><div class="ctl-scroll"><div class="app has-inspector"><main class="work"></main>' +
      '<div class="drawer ctl-drawer ag-split"></div></div></div></div>'
    document.body.appendChild(host)
    try {
      const app = host.querySelector('.app')!
      const col = host.querySelector('.ag-split')!
      const pad = painted(app, ['padding-top'], WIDE)
      expect(pad).toBe('16px')
      // No trail bar here, so `--ctl-bar-h` is 0 and the column sits the page padding down
      // (under a bar it sits the bar's height lower too: qafix3.agenthead.test.tsx, task-010).
      expect(painted(col, 'top', WIDE)).toBe(`calc(0px + ${pad})`)
      const h = painted(col, 'height', WIDE) ?? ''
      expect(h.replace(/\s+/g, ' ')).toMatch(/^calc\(100vh - (var\(--dock-h, 28px\)|28px) - 0px - 16px\)$/)
      expect(painted(col, 'max-height', WIDE)).toBe(h)
      // Covering the list (a phone's full screen), it ends above that line too.
      expect(painted(col, 'bottom', { width: 390 })).toMatch(/^(var\(--dock-h, 28px\)|28px)$/)
    } finally {
      host.remove()
    }
  })
})
