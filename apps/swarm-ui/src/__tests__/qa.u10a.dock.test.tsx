/**
 * U10a (owner QA, 2026-10-04): the log dock and the column it docks in.
 *
 *   D1   `Open full` drew BEHIND the spine and the panel: the dock was
 *        `position: fixed; inset: 0` inside the split, whose column is a size
 *        container and a sticky z-index 1, so the column was its containing
 *        block and its stacking context. Its `‹ Agent` lay under the Hive logo
 *        and Escape did nothing. The full view is portalled to <body>, above
 *        the shell; Escape and `‹ Agent` close it, focus returns, and the
 *        split's own Escape (which closes the agent) is not reached.
 *   D7   the column was `100vh - dock-h` tall and sticky at top 0 under the
 *        page's 16px top padding: its last 16px were under the API-reads line.
 *   D13  an open dock showed ~5 lines: ~175px of controls in 300px. One row
 *        of controls, metadata folded, and a default height with 15 lines.
 *   +    Follow on a finished agent is disabled.
 *   D13 review: the one row overflowed its column (Error and More past a
 *        390px phone's edge, Fold past the 50% split). Labels shorten under
 *        a container query, then controls spill into More while the row still
 *        overflows, then it wraps. Escape in an open More folds More, not the
 *        full view; a read the server did not mask is a chip in the row.
 *
 * MUTATIONS: render the full view in place (drop the portal); drop the
 * capture-phase Escape; restore `initial: 300` or `flex-wrap: wrap` on the
 * bar; unfold the stream table; drop `top: 16px` or the `- 16px`; enable
 * Follow on a final record; drop the `ag-logbar` container rules or the
 * spill effect; drop the `details[open]` branch in escape.ts. Each turns a
 * case red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs } from '../types'
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

const { LogDock, LOG_DOCK, BAR_SPILL } = await import('../LogDock')

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
  // `mock` opens on the runner's two streams, so the stream table is drawn.
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'mock', attempt_count: 1, completed_at: null, ...over })
}

/** The dock inside the split's column, as AgentSplit draws it. */
function inSplit(t: Task) {
  return render(
    <div className="app has-inspector">
      <div className="drawer ctl-drawer ag-split" role="dialog" aria-label="Agent">
        <LogDock task={t} />
      </div>
    </div>,
  )
}

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  // No transcript on a mock runner: a failed read, so the dock's read lands.
  api.loadTranscript.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no transcript' } })
  api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'one\ntwo\n'), stream('stderr', 'three\n')])))
})

afterEach(() => {
  vi.useRealTimers()
})

function openFull(): HTMLElement {
  const dock = screen.getByRole('region', { name: 'Log' })
  fireEvent.click(within(dock).getByRole('button', { name: 'Open full' }))
  return screen.getByRole('dialog', { name: 'Log' })
}

describe('D1: Open full is a layer over the whole app', () => {
  it('leaves the split for <body>, so the column is neither its containing block nor its stacking context', () => {
    inSplit(running())
    const full = openFull()
    expect(full.classList.contains('is-full')).toBe(true)
    expect(full.parentElement, 'the full view is still inside the split').toBe(document.body)
    expect(full.closest('.ag-split')).toBeNull()
    expect(full.getAttribute('aria-modal')).toBe('true')
  })

  it('stacks above every layer the shell draws (spine, flyouts, scrim, snaps, toasts) and under a help card', () => {
    inSplit(running())
    const full = openFull()
    const z = Number(painted(full, 'z-index', WIDE))
    expect(painted(full, 'position', WIDE)).toBe('fixed')
    expect(z).toBeGreaterThan(60)
    expect(z).toBeLessThan(200)
  })

  it('closes on Escape without closing the agent, and gives focus back to Open full', () => {
    const closeAgent = vi.fn()
    render(
      <div className="drawer ctl-drawer ag-split" onKeyDown={(e) => e.key === 'Escape' && closeAgent()}>
        <LogDock task={running()} />
      </div>,
    )
    const full = openFull()
    // Focus is in the full view, on its way out.
    expect(document.activeElement).toBe(within(full).getByRole('button', { name: '‹ Agent' }))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Log' })).toBeNull()
    expect(closeAgent, 'Escape on the full log closed the agent behind it').not.toHaveBeenCalled()
    const dock = screen.getByRole('region', { name: 'Log' })
    expect(dock.closest('.ag-split'), 'the dock did not come back to its column').not.toBeNull()
    expect(document.activeElement).toBe(within(dock).getByRole('button', { name: 'Open full' }))
  })

  it('closes on its ‹ Agent button too', () => {
    inSplit(running())
    const full = openFull()
    fireEvent.click(within(full).getByRole('button', { name: '‹ Agent' }))
    expect(screen.queryByRole('dialog', { name: 'Log' })).toBeNull()
    expect(screen.getByRole('region', { name: 'Log' }).classList.contains('is-full')).toBe(false)
  })
})

describe('D13: an open dock shows at least 15 lines', () => {
  it('draws its controls as ONE row that never wraps', async () => {
    inSplit(running())
    const dock = screen.getByRole('region', { name: 'Log' })
    const bars = dock.querySelectorAll(':scope > .ag-logdock-bar')
    expect(bars, 'the controls are not one row').toHaveLength(1)
    const bar = bars[0]! as HTMLElement
    expect(painted(bar, 'display', WIDE)).toBe('flex')
    expect(painted(bar, 'flex-wrap', WIDE)).toBe('nowrap')
    // Everything the bar needs is in it; the rest is under More.
    for (const name of [/^Following/, /^No error matched|^Error/, /^Fold$/]) {
      expect(within(bar).getByRole('button', { name })).toBeTruthy()
    }
    const more = bar.querySelector('details.ag-logdock-more')!
    expect(more.hasAttribute('open'), 'More is open by default').toBe(false)
    expect(more.querySelector('select[aria-label="Attempt, by generation"]')).not.toBeNull()
    expect(within(more as HTMLElement).getByRole('button', { name: /^Wrap/ })).toBeTruthy()
  })

  it('has room for 15 lines at its default height, by the shipped sheet', () => {
    inSplit(running())
    const dock = screen.getByRole('region', { name: 'Log' })
    const px = (v: string | null) => Number.parseFloat(v ?? 'NaN')
    const grip = px(painted(dock.querySelector('.ag-logdock-grip')!, 'height', WIDE))
    const bar = px(painted(dock.querySelector('.ag-logdock-bar')!, 'min-height', WIDE))
    // The log's line: `.logwin-body` is --t-body / --lh-body (14px x 1.5).
    const line = 14 * 1.5
    const chrome = grip + bar + 2 /* borders */ + 8 /* body foot padding */ + 8 /* the window's own padding */ * 2
    expect(Number.isFinite(grip) && Number.isFinite(bar)).toBe(true)
    expect(Math.floor((LOG_DOCK.initial - chrome) / line), `${LOG_DOCK.initial}px leaves too few lines`).toBeGreaterThanOrEqual(15)
  })

  it('folds the streams’ metadata when every stream has lines, so the first thing in view is a line', async () => {
    inSplit(running())
    const dock = screen.getByRole('region', { name: 'Log' })
    await waitFor(() => expect(dock.querySelectorAll('.ag-logline').length).toBeGreaterThan(0))
    const meta = dock.querySelector('details.ag-logmeta')
    expect(meta, 'the stream table is not folded').not.toBeNull()
    expect(meta!.hasAttribute('open')).toBe(false)
    expect(meta!.querySelector('table')).not.toBeNull()
  })

  it('keeps the table open when a stream has nothing to show, because then it is the reason', async () => {
    api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'one\n'), stream('stderr', '')])))
    inSplit(running())
    const dock = screen.getByRole('region', { name: 'Log' })
    await waitFor(() => expect(dock.querySelector('table')).not.toBeNull())
    expect(dock.querySelector('details.ag-logmeta')).toBeNull()
  })
})

describe('Follow on a finished agent', () => {
  it('is disabled, says the record is final, and F does nothing', async () => {
    inSplit(running({ state: 'SUCCEEDED', completed_at: new Date(Date.now() - 3_600_000).toISOString() }))
    const dock = screen.getByRole('region', { name: 'Log' })
    // A finished agent opens folded: its line says the record is final.
    expect(dock.querySelector('.ag-logdock-line')?.textContent).toMatch(/Final record/)
    fireEvent.click(within(dock).getByRole('button', { expanded: false }))
    const follow = within(dock).getByRole('button', { name: /^Final record/ })
    expect((follow as HTMLButtonElement).disabled).toBe(true)
    expect(follow.getAttribute('title')).toMatch(/nothing more will arrive/)
    fireEvent.keyDown(window, { key: 'f' })
    expect(follow.getAttribute('aria-pressed')).toBe('false')
  })

  it('stays a live control on a running agent', () => {
    inSplit(running())
    const follow = within(screen.getByRole('region', { name: 'Log' })).getByRole('button', { name: /^Following/ })
    expect((follow as HTMLButtonElement).disabled).toBe(false)
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
    } finally {
      host.remove()
    }
  })
})

/**
 * THE ROW'S WIDTH, BY A STATED MODEL (jsdom lays nothing out). Each control is
 * its visible text at 7px a character (the bar's 12-14px type) plus its own
 * chrome -- a small button or More's summary 22px of padding and border, More's
 * caret 14px, a chip 12px, a key hint 10px -- and the two controls that give
 * way are their `min-width` floor from the shipped sheet. Visibility comes from
 * the cascade at the stated container width, so the short labels and the
 * hidden heading count only where the sheet really applies them.
 */
const CH = 7
function shown(el: Element, env: CascadeEnv): boolean {
  return painted(el, 'display', env) !== 'none' && painted(el, 'position', env) !== 'absolute'
}
function textWidth(el: Element, env: CascadeEnv): number {
  let w = 0
  for (const n of Array.from(el.childNodes)) {
    if (n.nodeType === Node.TEXT_NODE) w += (n.textContent ?? '').length * CH
    else if (n instanceof Element && shown(n, env)) w += textWidth(n, env) + (n.tagName === 'KBD' ? 10 : 0)
  }
  return w
}
function rowWidth(bar: Element, env: CascadeEnv): number {
  const items = Array.from(bar.children).filter((c) => shown(c, env))
  let w = 24 + 8 * Math.max(0, items.length - 1)
  for (const c of items) {
    if (c.matches('.ag-logdock-streams, .ag-logdock-search')) w += Number.parseFloat(painted(c, 'min-width', env) ?? 'NaN')
    else if (c.matches('details')) w += textWidth(c.querySelector(':scope > summary')!, env) + 22 + 14
    else if (c.matches('.c-btn')) w += textWidth(c, env) + 22
    else if (c.matches('.art-masked')) w += textWidth(c, env) + 12
    else w += textWidth(c, env)
  }
  return w
}

/** Lays the bar out by `rowWidth` in a column `box` px wide; restores jsdom after. */
function laidOut(box: number, env: CascadeEnv): () => void {
  const proto = HTMLElement.prototype
  const sw = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollWidth')
  const cw = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')
  Object.defineProperty(proto, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (!this.classList.contains('ag-logdock-bar')) return 0
      return this.classList.contains('is-wrap') ? box : rowWidth(this, env)
    },
  })
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-logdock-bar') ? box : 0
    },
  })
  return () => {
    delete (proto as unknown as Record<string, unknown>).scrollWidth
    delete (proto as unknown as Record<string, unknown>).clientWidth
    if (sw !== undefined) Object.defineProperty(Element.prototype, 'scrollWidth', sw)
    if (cw !== undefined) Object.defineProperty(Element.prototype, 'clientWidth', cw)
  }
}

const PHONE: CascadeEnv = { width: 390, container: 390 }
const HALF: CascadeEnv = { width: 1440, container: 550 }

function phoneFull(t: Task): HTMLElement {
  render(<LogDock task={t} phone />)
  fireEvent.click(within(screen.getByRole('region', { name: 'Log' })).getByRole('button', { expanded: false }))
  return screen.getByRole('dialog', { name: 'Log' })
}

describe('D13 review: the one row fits its column', () => {
  it('shortens its labels and drops the key hints under 720px, and keeps them wide', () => {
    inSplit(running())
    const bar = screen.getByRole('region', { name: 'Log' }).querySelector<HTMLElement>('.ag-logdock-bar')!
    const follow = within(bar).getByRole('button', { name: /^Following/ })
    const error = within(bar).getByRole('button', { name: /^No error matched/ })
    const wide: CascadeEnv = { width: 1440, container: 900 }
    for (const b of [follow, error]) {
      expect(shown(b.querySelector('.ag-logdock-long') ?? b, wide)).toBe(true)
      expect(b.querySelector('kbd') && shown(b.querySelector('kbd')!, wide)).toBe(true)
      expect(b.querySelector('kbd') && shown(b.querySelector('kbd')!, HALF), 'a key hint stayed in a narrow row').toBe(false)
    }
    const long = error.querySelector('.ag-logdock-long')!
    const short = error.querySelector('.ag-logdock-short')!
    expect(shown(short, wide)).toBe(false)
    expect(shown(long, HALF), 'the long label stayed in a narrow row').toBe(false)
    expect(shown(short, HALF)).toBe(true)
    expect(short.textContent).toBe('Errors 0')
    // The name a reader hears is the long one, at every width.
    expect(error.getAttribute('aria-label')).toBeNull()
  })

  it('at the 50% split (550px column) keeps every control in the row, and the row fits', () => {
    const restore = laidOut(550, HALF)
    try {
      inSplit(running())
      const bar = screen.getByRole('region', { name: 'Log' }).querySelector<HTMLElement>('.ag-logdock-bar')!
      expect(bar.dataset.spill).toBe('0')
      expect(rowWidth(bar, HALF)).toBeLessThanOrEqual(550)
      for (const name of [/^Following/, /^No error matched/, /^Fold$/]) {
        expect(within(bar).getByRole('button', { name }).closest('details'), `${name} is under More`).toBeNull()
      }
    } finally {
      restore()
    }
  })

  it('on a 390px phone moves controls into More until the row fits, and leaves none past the edge', () => {
    const restore = laidOut(390, PHONE)
    try {
      const full = phoneFull(running())
      const bar = full.querySelector<HTMLElement>('.ag-logdock-bar')!
      expect(Number(bar.dataset.spill)).toBeGreaterThan(0)
      expect(bar.classList.contains('is-wrap')).toBe(false)
      expect(rowWidth(bar, PHONE), 'the row is wider than the phone').toBeLessThanOrEqual(390)
      const more = bar.querySelector('details.ag-logdock-more')!
      // Every control is in the row or under More: none is lost.
      expect(within(bar).getAllByRole('button', { name: /^Following/ })).toHaveLength(1)
      expect(within(more as HTMLElement).getByRole('button', { name: /^No error matched/ })).toBeTruthy()
      expect(within(bar).getByRole('button', { name: '‹ Agent' })).toBeTruthy()
    } finally {
      restore()
    }
  })

  it('wraps rather than hide a control when even the spilled row is too wide', async () => {
    api.loadTaskLogs.mockResolvedValue(
      ok({ ...logs([{ ...stream('stdout', 'one\ntwo\n'), redaction_count: 3 }, stream('stderr', 'three\n')]), redaction: { applied_at_read_time: false, rules: 0 } }),
    )
    const restore = laidOut(300, { width: 300, container: 300 })
    try {
      const full = phoneFull(running())
      await waitFor(() => expect(full.querySelectorAll('.ag-logdock-maskchip')).toHaveLength(2))
      fireEvent.change(within(full).getByRole('searchbox', { name: 'Search this window' }), { target: { value: 'one' } })
      const bar = full.querySelector<HTMLElement>('.ag-logdock-bar')!
      await waitFor(() => expect(bar.dataset.spill).toBe(String(BAR_SPILL.wrap)))
      expect(bar.classList.contains('is-wrap')).toBe(true)
      expect(painted(bar, 'flex-wrap', PHONE)).toBe('wrap')
    } finally {
      restore()
    }
  })
})

describe('D13 review: what moved under More', () => {
  it('a read the server did not mask is a warning chip in the row, not only a fact under More', async () => {
    api.loadTaskLogs.mockResolvedValue(
      ok({ ...logs([stream('stdout', 'one\n'), stream('stderr', 'two\n')]), redaction: { applied_at_read_time: false, rules: 0 } }),
    )
    inSplit(running())
    const bar = screen.getByRole('region', { name: 'Log' }).querySelector<HTMLElement>('.ag-logdock-bar')!
    await waitFor(() => expect(bar.querySelector(':scope > .ag-logdock-maskchip')?.textContent).toBe('not masked'))
    expect(bar.querySelector(':scope > .ag-logdock-maskchip')!.classList.contains('is-warn')).toBe(true)
  })

  it('Escape in an open More folds More and leaves the full view open', () => {
    inSplit(running())
    const full = openFull()
    const more = full.querySelector<HTMLDetailsElement>('details.ag-logdock-more')!
    more.open = true
    const summary = more.querySelector<HTMLElement>('summary')!
    summary.focus()
    fireEvent.keyDown(summary, { key: 'Escape' })
    expect(more.open, 'More stayed open').toBe(false)
    expect(screen.getByRole('dialog', { name: 'Log' })).toBe(full)
    expect(document.activeElement).toBe(summary)
    // The next Escape is the full view's.
    fireEvent.keyDown(summary, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Log' })).toBeNull()
  })
})
