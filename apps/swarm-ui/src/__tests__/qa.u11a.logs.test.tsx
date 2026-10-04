/**
 * U11a (owner QA re-run, 2026-10-04): LOGS ARE A TAB.
 *
 * The owner does not want the log component overlapping the agent details.
 * The bottom dock -- its strip, its resize handle, `Open full` and the page
 * scroll it caused (D7) -- is gone; the log is a full-height Logs tab at
 * `/agents/<tab>/<id>/logs`, with all four streams visible (D13 hid them in a
 * scrolling pill row). A running agent opens on Logs and a finished one on
 * Details. Escape in the log's More closes the menu and stops there (N11).
 *
 * MUTATIONS: render a log element outside the pane or position one over it
 * (`position: fixed|absolute|sticky` on `.ag-logs`/`.ag-loglast`); drop a
 * stream from the segmented control or let it spill into More; drop the
 * default-tab effect or send a finished agent to Logs; drop `onMoreKey`'s
 * `stopPropagation`; restore `flex-wrap: wrap` on the bar or drop the spill
 * effect; drop `top: 16px` or the `- 16px`. Each turns a case red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
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
const { resetListSnap, setListSnap, SNAPS } = await import('../listSnap')
const { addressToPath, pathToAddress } = await import('../paths')
const { fromAddress, canonical } = await import('../App')

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

function finished(): Task {
  return running({ state: 'SUCCEEDED', completed_at: new Date(Date.now() - 3_600_000).toISOString() })
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

function tab(name: string): HTMLElement {
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  const t = within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)
  expect(t, `no ${name} tab`).toBeTruthy()
  return t!
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

describe('the Logs tab: its address, its place, and all four streams', () => {
  it('sits beside Details and has its own route, /agents/<tab>/<id>/logs', async () => {
    api.loadTask.mockResolvedValue(ok(finished()))
    const go = vi.fn()
    render(<Routed start="detail" onGo={go} />)
    await screen.findByRole('button', { name: /^Last log line/ })
    const names = within(screen.getByRole('tablist', { name: 'Agent panes' }))
      .getAllByRole('tab')
      .map((t) => t.querySelector('.c-tab-label')?.textContent)
    expect(names.slice(0, 2)).toEqual(['Details', 'Logs'])
    fireEvent.click(tab('Logs'))
    expect(go).toHaveBeenLastCalledWith(`work/task/${ID}/logs`)
    expect(tab('Logs').getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('region', { name: 'Logs' })).toBeTruthy()
    // The address round-trips: the pane, not a piece of the id.
    expect(addressToPath(`work/task/${ID}/logs`, 'recent')).toBe(`/agents/recent/${ID}/logs`)
    expect(pathToAddress(`/agents/recent/${ID}/logs`)?.address).toBe(`work/task/${ID}/logs`)
    const r = fromAddress(`work/task/${ID}/logs`)
    expect(r).toMatchObject({ taskId: ID, taskPane: 'logs' })
    expect(canonical(r)).toBe(`work/task/${ID}/logs`)
  })

  it('renders Transcript, Agent stdout, Agent stderr and Runner as one segmented control in the row', () => {
    render(<AgentLogs task={running()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    const group = within(region).getByRole('group', { name: 'Which log' })
    expect(group.classList.contains('c-seg'), 'the streams are not the segmented control').toBe(true)
    expect(within(group).getAllByRole('button').map((b) => b.querySelector('.ag-logs-long')?.textContent ?? b.textContent)).toEqual([
      'Transcript',
      'Agent stdout',
      'Agent stderr',
      'Runner',
    ])
    // In the row itself, never scrolled away or under More (D13's hidden streams).
    expect(group.parentElement?.classList.contains('ag-logs-bar')).toBe(true)
    expect(group.closest('details')).toBeNull()
    expect(painted(group, ['overflow-x', 'overflow'], WIDE) ?? 'visible', 'the streams scroll inside the row').not.toMatch(/auto|scroll/)
    // The one on screen is pressed: a mock runner opens on Runner.
    expect(within(group).getByRole('button', { name: 'Runner' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('offers Follow on a live agent only: a finished one says its record is final', () => {
    const { unmount } = render(<AgentLogs task={running()} />)
    expect(within(screen.getByRole('region', { name: 'Logs' })).getByRole('button', { name: /^Following/ })).toBeTruthy()
    unmount()
    render(<AgentLogs task={finished()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    expect(within(region).queryByRole('button', { name: /Follow/ })).toBeNull()
    expect(region.querySelector('.ag-logs-final')?.getAttribute('title')).toMatch(/nothing more will arrive/)
    fireEvent.keyDown(window, { key: 'f' })
    expect(within(region).queryByRole('button', { name: /Follow/ })).toBeNull()
  })

  it('keeps the stream metadata folded when every stream has lines', async () => {
    render(<AgentLogs task={running()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    await waitFor(() => expect(region.querySelectorAll('.ag-logline').length).toBeGreaterThan(0))
    const meta = region.querySelector('details.ag-logmeta')
    expect(meta, 'the gs path and size are not folded').not.toBeNull()
    expect(meta!.hasAttribute('open')).toBe(false)
  })
})

describe('no element of the detail is covered by a log element, at any split width', () => {
  const POSITIONED = /^(fixed|absolute|sticky)$/

  async function check(start: Pane, task: Task) {
    api.loadTask.mockResolvedValue(ok(task))
    const r = render(<Routed start={start} onGo={() => {}} />)
    await waitFor(() => expect(document.querySelector('.ag-head-facts')).not.toBeNull())
    return r
  }

  for (const snap of SNAPS) {
    it(`draws the log in the pane's flow and nowhere else, list at ${snap}`, async () => {
      setListSnap(snap)
      // The detail column's width at this snap, 1440 wide: what the container
      // rules in the sheet are evaluated at.
      const column = snap === 'strip' ? 1000 : snap === 'list' ? 630 : 500
      const env: CascadeEnv = { width: 1440, container: column }
      for (const [start, task] of [
        ['detail', finished()],
        ['logs', running()],
        ['attempts', running()],
        ['artifacts', running()],
        ['checkpoints', running()],
      ] as const) {
        const { unmount, container } = await check(start, task)
        const split = container.querySelector('.ag-split')!
        // No dock, no strip, no grip, no full view: the dock is gone.
        expect(document.querySelector('[class*="ag-logdock"]'), `${start}: a dock element is drawn`).toBeNull()
        expect(split.querySelector('[role="separator"][aria-label="Resize the log"]')).toBeNull()
        expect(screen.queryByRole('button', { name: 'Open full' })).toBeNull()
        // Every log element is inside the open pane.
        const pane = split.querySelector(':scope > .ag-split-pane')!
        for (const el of document.querySelectorAll('.ag-logs, .ag-loglast')) {
          expect(pane.contains(el), `${start}: a log element outside the pane`).toBe(true)
          expect(painted(el, 'position', env) ?? 'static', `${start}: ${el.className} is laid over the pane`).not.toMatch(POSITIONED)
        }
        if (start === 'logs') {
          const logs = pane.querySelector(':scope > .ag-logs')!
          // The whole detail area under the header, and nothing beneath it.
          const shown = [...pane.children].filter((c) => !(c as HTMLElement).hidden)
          expect(shown, 'something is drawn beside the log in its pane').toEqual([logs])
          expect(pane.classList.contains('is-logs')).toBe(true)
          expect(painted(pane, ['overflow-y', 'overflow'], env)).toBe('hidden')
          expect(painted(logs, ['flex', 'flex-grow'], env)).toMatch(/^1\b/)
          expect(painted(logs.querySelector('.ag-logs-body')!, ['overflow-y', 'overflow'], env)).toBe('auto')
          // The pane is the column's last child: nothing is docked under it.
          expect(split.lastElementChild).toBe(pane)
        }
        if (start === 'detail') {
          const line = pane.querySelector('.ag-loglast')
          expect(line, 'Details has no last log line').not.toBeNull()
          expect(pane.firstElementChild).toBe(line)
        }
        unmount()
      }
    })
  }
})

describe('a running agent opens on Logs, a finished one on Details', () => {
  it('opens a running agent on its log', async () => {
    api.loadTask.mockResolvedValue(ok(running()))
    const go = vi.fn()
    render(<Routed start="detail" onGo={go} />)
    await screen.findByRole('region', { name: 'Logs' })
    expect(go).toHaveBeenCalledWith(`work/task/${ID}/logs`)
    expect(tab('Logs').getAttribute('aria-selected')).toBe('true')
    // A reader who then picks Details stays there.
    fireEvent.click(tab('Details'))
    await screen.findByRole('button', { name: /^Last log line/ })
    expect(tab('Details').getAttribute('aria-selected')).toBe('true')
    expect(go).toHaveBeenCalledTimes(2)
  })

  it('opens a finished agent on Details, whose last log line opens the tab', async () => {
    api.loadTask.mockResolvedValue(ok(finished()))
    const go = vi.fn()
    render(<Routed start="detail" onGo={go} />)
    const line = await screen.findByRole('button', { name: /^Last log line/ })
    expect(go).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Logs' })).toBeNull()
    await waitFor(() => expect(line.textContent).toContain('three'))
    fireEvent.click(line)
    expect(go).toHaveBeenCalledWith(`work/task/${ID}/logs`)
    expect(await screen.findByRole('region', { name: 'Logs' })).toBeTruthy()
  })

  it('leaves a link to another pane where it points', async () => {
    api.loadTask.mockResolvedValue(ok(running()))
    const go = vi.fn()
    render(<Routed start="attempts" onGo={go} />)
    await waitFor(() => expect(document.querySelector('.ag-head-facts')).not.toBeNull())
    expect(go).not.toHaveBeenCalled()
    expect(tab('Attempts').getAttribute('aria-selected')).toBe('true')
  })
})

describe('N11: Escape in More closes the menu, and only the menu', () => {
  it('folds More, keeps the agent open, and the next Escape closes the agent', async () => {
    api.loadTask.mockResolvedValue(ok(running()))
    const go = vi.fn()
    render(<Routed start="logs" onGo={go} />)
    const region = await screen.findByRole('region', { name: 'Logs' })
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
    api.loadTask.mockResolvedValue(ok(running()))
    const go = vi.fn()
    render(<Routed start="logs" onGo={go} />)
    const region = await screen.findByRole('region', { name: 'Logs' })
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
      for (const name of [/^Following/, /^No error matched/]) {
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
      expect(within(bar).getAllByRole('button', { name: /^No error matched/ })).toHaveLength(1)
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
      expect(painted(col, 'top', WIDE)).toBe(pad)
      const h = painted(col, 'height', WIDE) ?? ''
      expect(h.replace(/\s+/g, ' ')).toMatch(/^calc\(100vh - (var\(--dock-h, 28px\)|28px) - 16px\)$/)
      expect(painted(col, 'max-height', WIDE)).toBe(h)
      // Covering the list (a phone's full screen), it ends above that line too.
      expect(painted(col, 'bottom', { width: 390 })).toMatch(/^(var\(--dock-h, 28px\)|28px)$/)
    } finally {
      host.remove()
    }
  })
})
