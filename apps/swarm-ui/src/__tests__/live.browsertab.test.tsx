// LANE BROWSER-TAB-LIVE (part of #2423): THE LIVE FRAME IS THE FIRST THING
// THE EYE LANDS ON. A Claude-in-Chrome walk over seven claude-code-browser
// agents on the live deployment, 2026-10-26, measured:
//
//   BT-003 the frame sat under ~424 px of chrome and 42% of it was below the
//          407 px pane: the stage's floor asked for the device at the pane's
//          full width, so the pane scrolled and the frame's foot, caption and
//          'View only.' were hidden; a 3-line strip and a 2-row toolbar of
//          device chips sat above it;
//   BT-002 with ◫ logs on, `.live-logs` scrolled as one box, so the log's own
//          toolbar scrolled away with its lines, and the two columns had a
//          fixed 2:3 split;
//   BT-006 a finished agent's tab was one sentence, 'Attempts' plain text,
//          no player, no end time, and the frame vanished the moment the agent
//          finished;
//   BT-011 the device chips (which drive the agent's browser) and "Let any
//          member take control" sat beside ⤢ Theater, told apart by nothing;
//   BT-014 the frame was boxed in the preset's 1440×900 while the gate sends
//          1440×987, so black bars; the strip and caption said "not reported"
//          over a frame showing about:blank;
//   BT-010 a plain-HTTP page's refusal was said twice on the tab.
//
// MUTATIONS, one per block: put `minHeight: floor` back on `.live-stage` (or
// its 320 px floor in live.css); drop the `.live-strip-why` disclosure, or
// render LiveDevice straight into the toolbar again; move `.live-screen-say`
// back out of the caption; give `.live-logs` `overflow: auto` again, or drop
// the separator's key handler, or the `@container` rule; render the finished
// note without the links or RecordingsPanel, or drop LiveScreen's last-frame
// capture; put MemberControl back in `.live-toolbar-end`; fit to the preset
// alone in `fitShape`; say "not reported" for a null origin; render the
// banner's Take control with `refusal="line"`. Review fixes: drop the
// narrow tab's width-led `floor` on `.live-stage`, or its window cap; anchor
// `.live-strip-say` at the ⓘ's `left: 0` again; give the running tab's
// stacked rows `3fr 2fr` again.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LIVE_CSS from '../styles/live.css?raw'
import type { LiveSession } from '../api'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTranscript: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveActions: vi.fn(),
  mintLiveTicket: vi.fn(),
  openLiveSocket: vi.fn(),
  renewLiveControl: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

/** noVNC's RFB, as far as LiveBrowser.tsx touches it: a canvas, and a 1440×987 frame reported on connect. */
const rfb = vi.hoisted(() => {
  class FakeRfb {
    _fbWidth = 1440
    _fbHeight = 987
    constructor(readonly target: HTMLElement) {
      const canvas = document.createElement('canvas')
      canvas.width = 1440
      canvas.height = 987
      target.appendChild(canvas)
    }
    addEventListener(name: string, cb: () => void) {
      if (name === 'connect') cb()
    }
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

const { LiveBrowserPane, deviceCaption, fitShape, stripVerdict } = await import('../LiveBrowser')

const ID = 'task_0123456789abcdef0bt1'
const ME = ['ada', 'example.com'].join('@')
const OTHER = ['grace', 'example.com'].join('@')
const HTTP_SAY = 'The page is plain HTTP, so the gate will not hand over control on it.'

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

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_bt',
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

const viewer = () =>
  session({
    controller: { email: OTHER, since: new Date().toISOString() },
    you: { email: ME, can_control: false, can_preempt: false, can_open: true, can_switch_device: false },
  })

function pane(s: LiveSession, task: Task = agent()) {
  return render(<LiveBrowserPane task={task} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />)
}

const toolbar = () => screen.getByRole('toolbar', { name: 'Browser view' })

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.loadLiveActions.mockReturnValue(new Promise(() => {}))
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.openLiveSocket.mockReturnValue(new Promise(() => {}))
  api.renewLiveControl.mockReturnValue(new Promise(() => {}))
  const style = document.createElement('style')
  style.dataset.bt = ''
  style.textContent = LIVE_CSS
  document.head.appendChild(style)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.head.querySelectorAll('style[data-bt]').forEach((n) => n.remove())
  try {
    window.localStorage.clear()
  } catch {
    /* no storage in this environment */
  }
})

/** The rules live.css declares for `selector` exactly, comments dropped. */
function rulesFor(selector: string): string[] {
  const css = LIVE_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  const rules = css.match(/[^{}]+\{[^{}]*\}/g) ?? []
  return rules.filter((r) => r.slice(0, r.indexOf('{')).split(',').some((s) => s.trim() === selector))
}

// ---------------------------------------------------------------------------
// BT-003: the frame fits the pane's height; the chrome above it is short
// ---------------------------------------------------------------------------

describe('BT-003: the frame is fitted into the room the pane has, never pushed below it', () => {
  it('gives the stage no height of its own beyond a small floor, whatever the pane’s width', () => {
    // MEASURED: at 1440 the stage asked for the device at the pane's full
    // width (609 × 900/1440 = 380 px), more than the pane had, and the pane
    // scrolled the frame's foot out of view.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1000)
    pane(session())
    const stage = document.querySelector<HTMLElement>('.live-stage')!
    expect(stage.style.minHeight).toBe('')
    expect(Number(getComputedStyle(stage).minHeight.replace(/px$/, ''))).toBeLessThanOrEqual(160)
  })

  it('gives a narrow tab’s stage the device at the tab’s width, since its pane is sized to its content', () => {
    // RENDERED (review, 2026-10-26) at 390: the pane is content-sized, so
    // the stage fell to its 160 px floor and a desktop was drawn 182x115 in
    // a 358 px column. Desktop 1440x900 at 358 - 2 px of hairline: 223 px,
    // + 2 px of hairline + the 6 px gap (the caption measures 0 here).
    const narrow = (el: HTMLElement) => el.classList.contains('live-stage') || el.classList.contains('live-tab')
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return narrow(this) ? 358 : 0
    })
    pane(session())
    const stage = document.querySelector<HTMLElement>('.live-stage')!
    expect(stage.style.minHeight).toBe(`${Math.floor((900 * 356) / 1440) + 2 + 6}px`)
  })

  it('holds a narrow tab’s stage to the window’s height', () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('live-stage') || this.classList.contains('live-tab') ? 358 : 0
    })
    vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(200)
    pane(session())
    expect(document.querySelector<HTMLElement>('.live-stage')!.style.minHeight).toBe('200px')
  })

  it('opens the address’s explanation inside the strip, at its right, never past its edge', () => {
    // RENDERED (review, 2026-10-26) at 1440: anchored at the ⓘ's left, near
    // the strip's right end, it ran to x 1526 in a 1440 window, cut mid-word.
    pane(session())
    const strip = document.querySelector<HTMLElement>('.live-strip')!
    const why = strip.querySelector<HTMLElement>('details.live-strip-why')!
    expect(getComputedStyle(strip).position).toBe('relative')
    expect(getComputedStyle(why).position).toBe('static')
    const say = rulesFor('.live-strip-why > .live-strip-say').join('')
    expect(say).toMatch(/position:\s*absolute/)
    expect(say).toMatch(/right:\s*\d+px/)
    expect(say).not.toMatch(/(^|[\s;{])left:/)
    expect(say).toMatch(/max-width:\s*min\(\d+px,\s*calc\(100%\s*-\s*\d+px\)\)/)
  })

  it('draws the strip as one line: the address, who drives, and the address’s explanation folded away', () => {
    pane(session())
    const strip = document.querySelector<HTMLElement>('.live-strip')!
    const line = strip.querySelector<HTMLElement>('.live-strip-line')!
    expect(line).not.toBeNull()
    expect(getComputedStyle(line).flexWrap).toBe('nowrap')
    expect(line.querySelector('.live-origin')?.textContent).toMatch(/github\.com/)
    expect(within(line).getByText('Nobody is in control: the agent is driving.')).toBeTruthy()
    const why = strip.querySelector<HTMLDetailsElement>('details.live-strip-why')!
    expect(why).not.toBeNull()
    expect(why.open).toBe(false)
    expect(why.querySelector('.live-strip-say')?.textContent).toBe('The address Chromium reports for the page the agent is on.')
  })

  it('folds the device chips into one Device menu, so the toolbar is one row', () => {
    pane(session())
    const bar = toolbar()
    expect(within(bar).queryByRole('group', { name: 'Device' })).toBeNull()
    const menu = within(bar).getByRole('button', { name: /^Device: Desktop/ })
    expect(menu.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(menu)
    expect(menu.getAttribute('aria-expanded')).toBe('true')
    const chips = within(bar).getByRole('group', { name: 'Device' })
    expect(within(chips).getByRole('button', { name: 'iPhone 15' })).toBeTruthy()
    fireEvent.keyDown(chips, { key: 'Escape' })
    expect(within(bar).queryByRole('group', { name: 'Device' })).toBeNull()
  })

  it('says the connection’s state (View only., Connecting…) on the caption’s line, not on a line of its own below the stage', () => {
    pane(viewer())
    const say = document.querySelector<HTMLElement>('.live-screen-say')!
    expect(say.getAttribute('role')).toBe('status')
    expect(say.textContent).toBe('Connecting to the agent’s browser.')
    expect(say.closest('.live-stage-caption')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// BT-002: the logs beside the frame
// ---------------------------------------------------------------------------

describe('BT-002: the logs column keeps its toolbar and scrolls only its lines', () => {
  function withLogs() {
    pane(session())
    fireEvent.click(within(toolbar()).getByRole('button', { name: '◫ logs' }))
  }

  it('makes the logs column a flex column that never scrolls as a whole', () => {
    withLogs()
    const col = document.querySelector<HTMLElement>('.live-logs')!
    const css = getComputedStyle(col)
    expect(css.display).toBe('flex')
    expect(css.flexDirection).toBe('column')
    expect(css.overflow).toBe('hidden')
  })

  it('has a separator that moves the split by keyboard, held between 20% and 75%', () => {
    withLogs()
    const sep = screen.getByRole('separator', { name: 'Logs width' })
    const body = document.querySelector<HTMLElement>('.live-body')!
    const at = Number(sep.getAttribute('aria-valuenow'))
    expect(body.style.getPropertyValue('--live-split')).toBe(`${at}%`)
    fireEvent.keyDown(sep, { key: 'ArrowRight' })
    expect(Number(sep.getAttribute('aria-valuenow'))).toBe(at + 5)
    expect(body.style.getPropertyValue('--live-split')).toBe(`${at + 5}%`)
    fireEvent.keyDown(sep, { key: 'End' })
    expect(sep.getAttribute('aria-valuenow')).toBe('75')
    fireEvent.keyDown(sep, { key: 'Home' })
    expect(sep.getAttribute('aria-valuenow')).toBe('20')
  })

  it('drags the split with the pointer, as a share of the body’s width', () => {
    withLogs()
    const sep = screen.getByRole('separator', { name: 'Logs width' })
    const body = document.querySelector<HTMLElement>('.live-body')!
    vi.spyOn(body, 'getBoundingClientRect').mockReturnValue({ left: 100, width: 600, top: 0, height: 300, right: 700, bottom: 300, x: 100, y: 0, toJSON() {} })
    fireEvent.pointerDown(sep, { clientX: 370, pointerId: 1 })
    act(() => {
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 400 }))
    })
    expect(sep.getAttribute('aria-valuenow')).toBe('50')
    act(() => {
      window.dispatchEvent(new MouseEvent('pointerup'))
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 160 }))
    })
    expect(sep.getAttribute('aria-valuenow')).toBe('50')
  })

  it('stacks the logs under the frame in a narrow tab, and hides the separator there', () => {
    const css = LIVE_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
    const block = /@container\s+live-tab\s*\(max-width:\s*(\d+)px\)\s*\{([\s\S]*?\})\s*\}/.exec(css)
    expect(block).not.toBeNull()
    expect(Number(block![1])).toBeLessThan(527) // the 1024 pane stays side by side
    expect(Number(block![1])).toBeGreaterThanOrEqual(390) // a phone stacks
    expect(block![2]).toMatch(/grid-template-areas:\s*"screen"\s*"logs"/)
    expect(block![2]).toMatch(/\.live-split\s*\{\s*display:\s*none/)
    expect(rulesFor('.live-tab').join('')).toMatch(/container:\s*live-tab\s*\/\s*inline-size/)
  })

  it('bounds the stacked rows of a running tab, never shares of the log’s own height', () => {
    // RENDERED (review, 2026-10-26) at 390: `3fr 2fr` on the phone's
    // content-sized pane made the body 3299 px, the frame at y≈1343 and the
    // logs ~2470 px down.
    const css = LIVE_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
    const at = css.search(/@container\s+live-tab/)
    const inner = css.slice(at)
    const rule = /\.live-tab\.with-logs:not\(\.is-finished\)\s+\.live-body\s*\{([^}]*)\}/.exec(inner)
    expect(rule).not.toBeNull()
    const rows = /grid-template-rows:\s*([^;]+);/.exec(rule![1] ?? '')
    expect(rows).not.toBeNull()
    expect(rows![1]).not.toMatch(/fr\b/)
    expect(rows![1]).toMatch(/^auto\s+min\(\d+vh,\s*\d+px\)$/)
    expect(rule![1]).toMatch(/flex:\s*none/)
  })
})

// ---------------------------------------------------------------------------
// BT-006: a finished agent's Browser tab
// ---------------------------------------------------------------------------

describe('BT-006: a finished agent’s tab says when it ended and plays its recordings in place', () => {
  const done = () => agent({ state: 'SUCCEEDED', completed_at: '2026-10-26T20:49:30Z' })

  it('links Attempts to the recordings heading and Artifacts to the screenshots, and holds the player', () => {
    render(<LiveBrowserPane task={done()} live={{ status: 'idle' }} reload={() => {}} />)
    const attempts = screen.getByRole('link', { name: 'Attempts' })
    expect(attempts.getAttribute('href')).toBe(`/agents/recent/${ID}/attempts#rec-h-${ID}`)
    expect(screen.getByRole('link', { name: 'Artifacts' }).getAttribute('href')).toBe(`/agents/recent/${ID}/artifacts`)
    expect(screen.getByRole('heading', { name: 'Browser recordings' })).toBeTruthy()
    const hhmm = new Date('2026-10-26T20:49:30Z').toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    expect(document.querySelector('.live-ended')?.textContent).toContain(`ended ${hhmm}`)
  })

  it('keeps the last frame, and the logs that were open, when the agent finishes while watched', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,QUJD')
    api.mintLiveTicket.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { session_id: 'ls_bt', ticket: 'x'.repeat(8), expires_at: new Date(Date.now() + 30_000).toISOString() },
    })
    api.openLiveSocket.mockImplementation(async (_s: string, _t: string, onOpen: (ws: WebSocket) => void) => {
      onOpen({ readyState: 1, close() {} } as unknown as WebSocket)
    })
    const { rerender } = pane(session())
    fireEvent.click(within(toolbar()).getByRole('button', { name: '◫ logs' }))
    // The screen drew a frame: noVNC is connected and has reported its size.
    await waitFor(() => expect(document.querySelector('[data-live-screen] canvas')).not.toBeNull())
    await waitFor(() => expect(document.querySelector('.live-caption')?.textContent).toMatch(/^Desktop · 1440×900/))
    rerender(<LiveBrowserPane task={done()} live={{ status: 'idle' }} reload={() => {}} />)
    const last = screen.getByRole('img', { name: /last drew/ }) as HTMLImageElement
    expect(last.getAttribute('src')).toBe('data:image/png;base64,QUJD')
    expect(document.querySelector('.live-logs')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// BT-011: view controls and controls of the agent's browser, apart
// ---------------------------------------------------------------------------

describe('BT-011: the toolbar keeps what changes the agent’s browser apart from what changes this view', () => {
  it('puts the device menu and member control in one group, ◫ logs and Theater in another', () => {
    pane(session())
    const bar = toolbar()
    const control = within(bar).getByRole('group', { name: 'The agent’s browser' })
    const view = within(bar).getByRole('group', { name: 'This view' })
    expect(within(control).getByRole('checkbox', { name: 'Let any member take control' })).toBeTruthy()
    expect(within(control).getByRole('button', { name: /^Device:/ })).toBeTruthy()
    expect(within(view).getByRole('button', { name: 'Open theater (T)' })).toBeTruthy()
    expect(within(view).getByRole('button', { name: '◫ logs' })).toBeTruthy()
    expect(within(view).queryByRole('checkbox')).toBeNull()
  })

  it('keeps the toolbar off the pane’s right edge, where an overlay scrollbar sits', () => {
    expect(rulesFor('.live-toolbar').join('')).toMatch(/padding-inline-end:\s*[1-9]/)
  })

  it('draws each chip disabled with the reason as its title for a viewer not in control', () => {
    pane(viewer())
    fireEvent.click(within(toolbar()).getByRole('button', { name: /^Device:/ }))
    const chip = within(toolbar()).getByRole('button', { name: 'iPhone 15' }) as HTMLButtonElement
    expect(chip.disabled).toBe(true)
    expect(chip.title).toBe('Only the person in control can change the device.')
  })
})

// ---------------------------------------------------------------------------
// BT-014: the frame's own proportions, and labels that agree with it
// ---------------------------------------------------------------------------

describe('BT-014: the frame is boxed in what the gate sends, and labelled as what it shows', () => {
  const desktop = { width: 1440, height: 900, mobile: false }

  it('fits to the frame the gate sends when it is the way round the device is', () => {
    // MEASURED: a 1440×987 backing store in a 1440×900 box, 27 px bars each side.
    expect(fitShape(desktop, { width: 1440, height: 987 })).toEqual({ width: 1440, height: 987 })
  })

  it('keeps the device’s proportions with no frame yet, or one that contradicts the device', () => {
    expect(fitShape(desktop, null)).toEqual({ width: 1440, height: 900 })
    expect(fitShape(desktop, { width: 900, height: 1440 })).toEqual({ width: 1440, height: 900 })
    expect(fitShape(null, { width: 1440, height: 987 })).toBeNull()
  })

  it('says a page with no address may be about:blank, in the strip and the caption alike', () => {
    expect(stripVerdict(session({ origin: null })).say).toMatch(/about:blank/)
    expect(deviceCaption(session({ origin: null }))).toBe('Desktop · 1440×900 · no web address')
    pane(session({ origin: null }))
    expect(document.querySelector('.live-strip .live-origin')?.textContent).toBe('no web address')
  })
})

// ---------------------------------------------------------------------------
// BT-010: the plain-HTTP refusal, said once on the tab
// ---------------------------------------------------------------------------

describe('BT-010: the strip is the one place the tab says a plain-HTTP page refuses control', () => {
  it('says it once, as a line of the strip, while the banner’s Take control carries it as its title', () => {
    const wait = {
      reason: 'Sign in to the staging site',
      expect: 'staging.example.com',
      requested_at: new Date().toISOString(),
      deadline: new Date(Date.now() + 600_000).toISOString(),
    }
    const http = { origin: 'http://staging.example.com', host: 'staging.example.com', registrable_domain: 'example.com', scheme: 'http:' }
    pane(session({ origin: http, human_wait: wait }), agent({ human_wait: { ...wait, controller: null } }))
    const said = [...document.querySelectorAll('*')].filter((el) => el.childElementCount === 0 && el.textContent?.includes(HTTP_SAY))
    expect(said).toHaveLength(1)
    expect(said[0]!.closest('.live-strip')).not.toBeNull()
    expect(said[0]!.closest('details')).toBeNull()
    const take = screen.getByRole('button', { name: 'Take control' }) as HTMLButtonElement
    expect(take.disabled).toBe(true)
    expect(take.title).toBe(HTTP_SAY)
  })

  it('is not said again in the agent’s head above the tab (QA1-FIX-13, chrome-C06)', async () => {
    // MEASURED on the live deployment (a0f2d100a, 2026-10-27): the head's Take
    // control drew the refusal as a line, and the Browser tab's strip drew it
    // again under the tabs. The strip keeps the line (BT-010); the head's
    // disabled button carries it as its title, as the banner's does.
    const { AgHead } = await import('../agent-split/head')
    const http = { origin: 'http://staging.example.com', host: 'staging.example.com', registrable_domain: 'example.com', scheme: 'http:' }
    const s = session({ origin: http })
    const t = agent()
    render(
      <>
        <AgHead taskId={ID} task={t} runs={1} read="ok" reload={() => {}} onClose={() => {}} live={{ status: 'ok', liveBrowser: true, session: s }} reloadLive={() => {}} />
        <LiveBrowserPane task={t} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />
      </>,
    )
    // The innermost elements that say it: the disabled button's reason line carries a glyph beside its words.
    const says = (el: Element) => el.textContent?.includes(HTTP_SAY) === true
    const said = [...document.querySelectorAll('*')].filter((el) => says(el) && ![...el.children].some(says))
    // MUTATION: drop `refusal="title"` from agent-split/head.tsx's LiveControl and this is 2.
    expect(said).toHaveLength(1)
    expect(said[0]!.closest('.live-strip')).not.toBeNull()
    const headTake = document.querySelector('.ag-head .live-control button') as HTMLButtonElement
    expect(headTake.disabled).toBe(true)
    expect(headTake.title).toBe(HTTP_SAY)
  })
})

// ---------------------------------------------------------------------------
// TENANT-E-ASK (part of #2391): the member-control ask opens inside its pane
// ---------------------------------------------------------------------------

/** The `@media (max-width: 560px)` blocks of live.css, comments dropped. */
function phoneBlocks(): string {
  const css = LIVE_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: string[] = []
  const re = /@media \(max-width: 560px\) \{/g
  for (let m = re.exec(css); m !== null; m = re.exec(css)) {
    let depth = 1
    let i = m.index + m[0].length
    for (; i < css.length && depth > 0; i++) depth += css[i] === '{' ? 1 : css[i] === '}' ? -1 : 0
    out.push(css.slice(m.index, i))
  }
  return out.join('\n')
}

describe('TENANT-E-ASK: "Let any member take control?" opens inside the pane at every width', () => {
  // RENDERED (review task_c00bd5af9cad483db21e, 2026-10-26): the 420 px ask
  // was anchored right:0 to its checkbox, which sits at the toolbar's LEFT,
  // so at 1440 it ran ~104 px past the pane's left edge and the heading read
  // "er take control?"; at 390 it started at x=-110.
  it('anchors the ask to the toolbar, not the checkbox, so its box is the pane’s width', () => {
    expect(rulesFor('.live-toolbar').join('\n')).toMatch(/position:\s*relative/)
    const wrap = rulesFor('.live-open-wrap').join('\n')
    expect(wrap).not.toMatch(/position:\s*(relative|absolute|fixed|sticky)/)
  })

  it('opens from the left, where its checkbox is, and never wider than the toolbar', () => {
    const ask = rulesFor('.live-ask.is-members').join('\n')
    expect(ask).toMatch(/left:\s*0/)
    expect(ask).toMatch(/right:\s*auto/)
    expect(ask).toMatch(/width:\s*min\(420px,\s*100%\)/)
    expect(ask).toMatch(/box-sizing:\s*border-box/)
  })

  it('spans the toolbar on a phone, inside the page’s gutters', () => {
    const phone = phoneBlocks()
    expect(phone).toMatch(/\.live-ask\.is-members\s*\{[^}]*left:\s*0[^}]*right:\s*0[^}]*width:\s*auto/)
  })

  it('renders the ask with the class that picks its side, its words unchanged', () => {
    pane(session())
    fireEvent.click(screen.getByRole('checkbox', { name: 'Let any member take control' }))
    const ask = screen.getByRole('dialog', { name: 'Let any member take control' })
    expect(ask.classList.contains('live-ask')).toBe(true)
    expect(ask.classList.contains('is-members')).toBe(true)
    expect(within(ask).getByRole('heading', { name: 'Let any member take control?' })).toBeTruthy()
    expect(toolbar().contains(ask)).toBe(true)
  })
})
