// QAFIX-PHONE-TASK (part of #1680; finding QA-116 of visual QA run
// wf_9e9bf74834634db7bcc3). The owner's two answers, 2026-10-20, recorded on
// task_ef764de3fbea4155bf0f:
//
// 1. "‹ Failed". At 390 px a task opened from Agents › Recent with a state
//    filter led back with "‹ Recent". It went back to the filter; only its
//    word left the filter out. The back link names the filter now.
// 2. "Start under the header". Below 1100 px the task is the overlay
//    `.drawer`, fixed at `top: 0` and z-index 40, so on a phone it covered
//    the sticky app header (`.sk-pbar`, z-index 15) and its menu. Below
//    760 px it starts at the header's foot -- the header's own height, ONE
//    custom property (`--sk-pbar-h`, styles.css), 44px or 36px compacted --
//    stays under the menu, and is no longer a modal dialog: the header above
//    it is visible and usable, so focus is not trapped away from it.
//
// SCREENSHOTS FOR THE NEXT VISUAL QA RUN: 390 px, a task opened from the
// failed filter (`/agents/recent/<task>?state=failed`), in light and in dark.
// Each should show the phone header with its menu above the task, the back
// link reading "‹ Failed", and the pane tabs flush under the back link row
// once the task is scrolled.

import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AGENTS_CSS from '../styles/agents.css?raw'
import STYLES from '../styles.css?raw'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')
const { tabStops } = await import('../focus')

const ID = 'task_0123456789abcdef0123'
const PHONE: CascadeEnv = { width: 390, height: 844 }
const TABLET: CascadeEnv = { width: 900, height: 900 }
/** The sheets in the bundle's order: a section's sheet before styles.css (marks.ts). */
const SHEETS = AGENTS_CSS + '\n' + STYLES

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function failed(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'FAILED',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    workflow_id: null,
    step_id: 'fix-heartbeat',
    attempt_count: 1,
    result_summary: null,
    ...over,
  })
}

const hosts: HTMLElement[] = []
const sheets: HTMLStyleElement[] = []

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadTask.mockResolvedValue(ok(failed()))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  for (const s of sheets.splice(0)) s.remove()
})

function split(closeTo: string, go: (to: string) => void = () => {}) {
  return render(
    <div className="app">
      <AgentSplit taskId={ID} pane="detail" artifact={null} closeTo={closeTo} go={go} base={`work/task/${ID}`} />
    </div>,
  )
}

function back(): HTMLElement {
  const b = document.querySelector<HTMLElement>('.ctl-agent-back')
  expect(b, 'the split draws its back link').not.toBeNull()
  return b!
}

describe('QA-116, 1: the back link names the list it returns to, filter and all', () => {
  it.each([
    ['work/running/recent/failed', 'Failed'],
    ['work/running/recent/cancelled', 'Cancelled'],
    ['work/running/recent/succeeded', 'Succeeded'],
    ['work/running/recent/failed?group=wf', 'Failed'],
  ])('from %s it reads ‹ %s', (closeTo, word) => {
    split(closeTo)
    // MUTATION: drop the state segment from `backLabel` (agentlist.ts) and the link reads "‹ Recent".
    expect(back().textContent).toBe(`‹ ${word}`)
    // MUTATION: name the aria-label from the tab alone and a screen reader still hears "Back to Recent".
    expect(back().getAttribute('aria-label')).toBe(`Back to ${word}`)
  })

  it.each([
    ['work/running/recent', 'Recent'],
    ['work/running/live', 'Live'],
    ['work/running/waiting', 'Waiting'],
  ])('from %s, with no filter, it still reads ‹ %s', (closeTo, word) => {
    split(closeTo)
    // MUTATION: name a state where the address carries none and these change.
    expect(back().textContent).toBe(`‹ ${word}`)
    expect(back().getAttribute('aria-label')).toBe(`Back to ${word}`)
  })

  it('still goes back to the filtered list', () => {
    const go = vi.fn()
    split('work/running/recent/failed', go)
    fireEvent.click(back())
    // MUTATION: close to the tab rather than the address and the filter is lost on the way back.
    expect(go).toHaveBeenCalledWith('work/running/recent/failed')
  })
})

/** The frame's nesting at a phone width: header above the scroller, the task inside `.app`. */
function frame(cls = ''): { app: HTMLElement; bar: HTMLElement; side: HTMLElement; drawer: HTMLElement; edge: HTMLElement; pane: HTMLElement } {
  const host = document.createElement('div')
  host.innerHTML =
    `<div class="sk-app${cls}"><header class="sk-pbar"></header><div class="sk-side"></div>` +
    '<div class="sk-main"><div class="ctl-scroll"><div class="app"><main class="work"></main>' +
    '<div class="drawer ctl-drawer ag-split"><button class="ctl-agent-back">‹ Failed</button>' +
    '<div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist"></div></div>' +
    '<div class="ag-split-pane"></div></div></div></div></div></div>'
  document.body.appendChild(host)
  hosts.push(host)
  const q = (s: string) => host.querySelector<HTMLElement>(s)!
  return { app: q('.sk-app'), bar: q('.sk-pbar'), side: q('.sk-side'), drawer: q('.ag-split'), edge: q('.ag-tabs-edge'), pane: q('.ag-split-pane') }
}

function zOf(el: Element, env: CascadeEnv): number {
  const v = painted(el, 'z-index', env)
  expect(v, 'a z-index is declared').not.toBeNull()
  return Number(v)
}

describe('QA-116, 2: on a phone the open task starts under the app header', () => {
  it('reads its top from the one property the header’s height is set by', () => {
    const { drawer, bar } = frame()
    // MUTATION: put a literal (44px) on the phone drawer's `top` instead of the property, and a compacted header leaves a gap.
    expect(cascade(SHEETS, drawer, 'top', PHONE).winner?.value).toMatch(/^var\(--sk-pbar-h\b/)
    // MUTATION: give `.sk-pbar` its own literal height again and the two can drift.
    expect(cascade(SHEETS, bar, 'height', PHONE).winner?.value).toMatch(/^var\(--sk-pbar-h\b/)
  })

  it.each([
    ['at the top of the page', '', '44px'],
    ['with the header compacted', ' is-scrolled', '36px'],
  ])('starts at the header’s foot %s', (_label, cls, height) => {
    const { drawer, bar } = frame(cls)
    // MUTATION: drop `--sk-pbar-h` from `.sk-app`, or the 36px override from `.sk-app.is-scrolled`.
    expect(painted(bar, 'height', PHONE)).toBe(height)
    // MUTATION: drop the phone `top` rule (agents.css) and the task starts at 0, over the header (QA-116).
    expect(painted(drawer, 'top', PHONE)).toBe(height)
  })

  it('leaves the header and its menu above the task', () => {
    const { drawer, bar } = frame()
    const open = frame(' has-drawer')
    // The premise: the header sticks at the top, so starting at its foot is not covering it.
    expect(painted(bar, 'position', PHONE)).toBe('sticky')
    expect(painted(bar, 'top', PHONE)).toBe('0')
    const scrim = document.createElement('div')
    scrim.className = 'sk-scrim'
    open.app.appendChild(scrim)
    // MUTATION: keep the drawer's z-index 40 on a phone and the opened menu and its scrim paint under the task.
    expect(zOf(drawer, PHONE)).toBeLessThan(zOf(scrim, PHONE))
    expect(zOf(drawer, PHONE)).toBeLessThan(zOf(open.side, PHONE))
    // ... and above the page's own sticky bars, which it still covers (`.ctl-bar`, 20).
    expect(zOf(drawer, PHONE)).toBeGreaterThan(20)
  })

  it('leaves 760-1100 px as it was: the overlay at the top of the screen, at z-index 40', () => {
    const { drawer } = frame()
    // MUTATION: widen the phone rule past 759px and the tablet overlay moves too.
    expect(painted(drawer, 'top', TABLET)).toBe('0')
    expect(zOf(drawer, TABLET)).toBe(40)
  })

  it('keeps the pane tabs flush under the back link row, in one scroller', () => {
    const { drawer, edge, pane } = frame(' is-scrolled')
    const pad = painted(drawer, ['padding-top', 'padding'], PHONE)!.trim().split(/\s+/)[0]!
    // The tabs stick at the drawer's own top edge (QA-019), wherever the drawer starts.
    expect(painted(edge, 'position', PHONE)).toBe('sticky')
    // MUTATION: offset the strip by the header height as well and it sticks a header below the drawer's top.
    expect(painted(edge, 'top', PHONE)).toBe(`-${pad}`)
    // One scroller: the column scrolls and its pane does not, so no second scrollbar.
    expect(painted(drawer, ['overflow-y', 'overflow'], PHONE)).toBe('auto')
    expect(painted(pane, ['overflow-y', 'overflow'], PHONE)).toBe('visible')
  })
})

/** A sheet that places the split the way the phone rule does, for jsdom, which applies no `@media`. */
function place(css: string): void {
  const s = document.createElement('style')
  s.textContent = css
  document.head.appendChild(s)
  sheets.push(s)
}

describe('QA-116, 2: under the header the task is a region, not a modal dialog', () => {
  it('drops role=dialog and aria-modal, and lets Tab leave for the header', async () => {
    place('.ag-split.ctl-drawer { position: fixed; top: 44px; }')
    const go = vi.fn()
    split('work/running/recent/failed', go)
    const panel = document.querySelector<HTMLElement>('.ag-split')!
    await waitFor(() => expect(document.querySelector('.ag-head .ag-head-title')?.textContent).toBe('fix-heartbeat'))
    // MUTATION: mark the split modal wherever it is an overlay (`isOverlay` alone) and it traps focus under a visible header.
    expect(panel.getAttribute('aria-modal')).toBeNull()
    expect(panel.getAttribute('role')).toBe('region')
    expect(panel.getAttribute('aria-label')).toBe(`Agent ${ID}`)
    const stops = tabStops(panel)
    const last = stops[stops.length - 1]!
    last.focus()
    // MUTATION: trap Tab on `isOverlay` in AgentSplit's onKeyDown and Tab from the last stop wraps instead of leaving.
    expect(fireEvent.keyDown(last, { key: 'Tab' }), 'Tab was held inside the task').toBe(true)
    // Escape and the back link are still ways out.
    fireEvent.keyDown(panel, { key: 'Escape' })
    expect(go).toHaveBeenLastCalledWith('work/running/recent/failed')
  })

  it('stays the modal dialog where it covers the whole screen (760-1100 px)', async () => {
    place('.ag-split.ctl-drawer { position: fixed; top: 0; }')
    split('work/running/recent/failed')
    const panel = document.querySelector<HTMLElement>('.ag-split')!
    await waitFor(() => expect(document.querySelector('.ag-head .ag-head-title')?.textContent).toBe('fix-heartbeat'))
    // MUTATION: drop the dialog role everywhere and the tablet overlay stops being modal (WA-17).
    expect(panel.getAttribute('role')).toBe('dialog')
    expect(panel.getAttribute('aria-modal')).toBe('true')
    const stops = tabStops(panel)
    const last = stops[stops.length - 1]!
    last.focus()
    expect(fireEvent.keyDown(last, { key: 'Tab' }), 'Tab left the modal task').toBe(false)
  })
})
