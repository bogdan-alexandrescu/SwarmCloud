/**
 * LANE BROWSER-TAB-TABS (part of #2423). A Claude-in-Chrome walk of seven
 * claude-code-browser agents on the deployed console, 2026-10-26, at 1440, 1024 and
 * 390, measured the agent's tab strip:
 *
 *   BT-004  at 390 the strip holding the open tab was squeezed to 0 px beside
 *           a 448 px `7 more: Details, Logs, …` that would not shrink, ran
 *           past the drawer, and had the strip's `›` drawn over its text.
 *   BT-005  More spelled out every tab it held, so it was as wide as they
 *           were: 420 px beside one tab at 1024.
 *   BT-007  a finished browser agent had its Browser tab only while on it.
 *   BT-013  the bar changed with the open tab: picking Artifacts sent
 *           Attempts into More; opening Logs sent Browser into More.
 *
 * jsdom lays nothing out, so each tab's width is given, as the walk measured
 * it (`WIDTH`), and More's is the width of the label it draws (`moreWide`).
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import AGENTS_CSS from '../styles/agents.css?raw'
import { cascade, type CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'
import type { Task } from '../types'

const { AgTabsEdge, fitTabs, paneTabs } = await import('../agent-split/tabs')

const PHONE: CascadeEnv = { width: 390, height: 844 }
const MID: CascadeEnv = { width: 1024, height: 625 }
const WIDE: CascadeEnv = { width: 1440, height: 757 }
const ID = 'task_a8f569554ee34cacbd2d'

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, runner_profile: 'claude-code-browser', state: 'RUNNING', completed_at: null, parent_task_id: null, ...over } as Partial<Task>)
}
const finished = () => row({ state: 'SUCCEEDED', completed_at: new Date().toISOString(), result_summary: { artifacts: Array.from({ length: 65 }, (_, i) => `f${i}`) } })

// Each tab as drawn with its count, in px (the walk's 1440 and 1024 reads).
const WIDTH: Readonly<Record<string, number>> = {
  Details: 70,
  Browser: 75,
  Logs: 50,
  Changes: 92,
  Children: 92,
  Attempts: 96,
  Artifacts: 104,
  Checkpoints: 118,
}
const BROWSER_LIVE = 111
// More's width follows the text it DRAWS: about 7 px a character plus 30 px
// of padding and ▾. Text that is read but not drawn takes no width.
const moreWide = (el: HTMLElement) =>
  30 + 7 * ((el.querySelector('.ag-tabs-more-label') ?? el.querySelector('.ag-tabs-more'))?.textContent ?? '').length

const proto = HTMLElement.prototype
const had = {
  client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
  offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
  scroll: Object.getOwnPropertyDescriptor(proto, 'scrollWidth'),
}
function size(room: number) {
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-edge') ? room : 0
    },
  })
  Object.defineProperty(proto, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (this.classList.contains('ag-tabs-more-host')) return moreWide(this)
      if (this.getAttribute('role') !== 'tab' || this.closest('.ag-tabs-edge') === null) return 0
      const label = this.querySelector('.c-tab-label')?.textContent ?? ''
      if (label === 'Browser' && (this.textContent ?? '').includes('LIVE')) return BROWSER_LIVE
      return WIDTH[label] ?? 0
    },
  })
  // The fit reads More from what it draws (VQ-008 review): its label's whole
  // text; the 30 px of padding and gap is the sheet's, read from the button.
  Object.defineProperty(proto, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-more-label') ? 7 * (this.textContent ?? '').length : 0
    },
  })
}
afterEach(() => {
  for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset], ['scrollWidth', had.scroll]] as const) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
})

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

/**
 * The strip as AgentSplit.tsx hands it to AgTabsEdge, for `t` with `current`
 * open. Its `hasBrowser` is true on `/browser`, as the split's is, unless the
 * live read is given (`opts.hasBrowser`).
 */
function strip(t: Task, current: string, opts: { hasBrowser?: boolean; sessionOpen?: boolean } = {}) {
  const tabsFor = (cur: string) =>
    paneTabs({
      task: t,
      base: `/agents/live/${ID}`,
      hasBrowser: opts.hasBrowser ?? cur === 'browser',
      sessionOpen: opts.sessionOpen ?? false,
      childCount: 0,
      runs: 1,
      ckpt: null,
      ckptCount: 4,
      pane: cur as never,
    })
  const defs = (cur: string) => (
    <div className="drawer ctl-drawer ag-split">
      <AgTabsEdge
        current={cur}
        onSelect={() => {}}
        tabs={tabsFor(cur).map((x) => ({
          key: x.id,
          label: x.label,
          ...((x.id === 'detail' && x.count === null) || x.id === 'logs' || (x.id === 'browser' && x.count === null)
            ? {}
            : { count: x.count, why: x.say ?? undefined }),
        }))}
      />
    </div>
  )
  const r = render(defs(current))
  const settle = () =>
    act(() => {
      fireEvent(window, new Event('resize'))
    })
  settle()
  settle()
  const list = () => screen.getByRole('tablist', { name: 'Agent panes' })
  const onBar = () => within(list()).getAllByRole('tab').map((b) => b.querySelector('.c-tab-label')?.textContent ?? '')
  const reopen = (cur: string) => {
    r.rerender(defs(cur))
    settle()
    settle()
  }
  return { ...r, list, onBar, reopen }
}

describe('BT-005: More is a short `More <n>`, and its width is what is fitted', () => {
  it('draws `More <n>`, not the name of every tab it holds', () => {
    size(499) // 1024x625, a running agent: the walk's tablist 79 + More 420
    strip(row(), 'browser', { hasBrowser: true, sessionOpen: true })
    const more = document.querySelector<HTMLElement>('.ag-tabs-more')!
    expect(more).not.toBeNull()
    const label = more.querySelector('.ag-tabs-more-label')
    // MUTATION: draw `7 more: Details, Logs, …` again -- More is 420 px at 1024.
    expect(flat(label)).toMatch(/^More \d+$/)
    // The names it holds are its title and the rest of its name, never drawn.
    const names = more.getAttribute('title')!
    expect(names.split(', ').length).toBeGreaterThan(1)
    expect(screen.getByRole('button', { name: `${flat(label)} tabs: ${names}` })).toBe(more)
    // MUTATION: an aria-label would let the name and the drawn text part again.
    expect(more.hasAttribute('aria-label')).toBe(false)
    for (const env of [PHONE, MID, WIDE]) {
      // MUTATION: drop the visually-hidden rule and the names are drawn, as wide as before.
      expect(painted(more.querySelector('.ag-tabs-more-names')!, 'position', env)).toBe('absolute')
      expect(painted(more.querySelector('.ag-tabs-more-names')!, ['clip-path', 'clip'], env)).not.toBeNull()
    }
  })

  it('at 1024 on a running agent keeps Details, Browser LIVE, Logs and Attempts on the bar, not Browser alone', () => {
    size(499)
    const { onBar } = strip(row(), 'browser', { hasBrowser: true, sessionOpen: true })
    // MUTATION: the long label is fitted (420 px) and only Browser is left on the bar.
    expect(onBar()).toEqual(['Details', 'Browser', 'Logs', 'Attempts'])
  })

  it('at 1440 a finished browser agent has five tabs on the bar beside `More 3`, not four beside a 290 px More', () => {
    size(587) // 1440x757: the walk's tablist 297 + More about 290
    const { onBar } = strip(finished(), 'browser')
    // MUTATION: the long More is fitted and only Details, Browser, Logs, Attempts are left.
    expect(onBar()).toEqual(['Details', 'Browser', 'Logs', 'Changes', 'Attempts'])
    expect(flat(document.querySelector('.ag-tabs-more-label'))).toBe('More 3')
  })

  it('where every tab fits there is no More at all', () => {
    size(720)
    const { onBar } = strip(finished(), 'browser')
    expect(onBar()).toEqual(['Details', 'Browser', 'Logs', 'Changes', 'Children', 'Attempts', 'Artifacts', 'Checkpoints'])
    expect(document.querySelector('.ag-tabs-more')).toBeNull()
  })
})

describe('BT-004: at 390 the open tab is on the bar, at its width, and More fits beside it', () => {
  it('keeps the open tab on the bar with its width as the strip’s floor, and More short', () => {
    size(358) // .ag-tabs-edge at 390x844
    const { list, onBar } = strip(row(), 'browser', { hasBrowser: true, sessionOpen: true })
    expect(onBar()).toContain('Browser')
    // MUTATION: drop the strip's min-width and a squeezed strip hides the open tab (clientWidth 0).
    expect(list().style.minWidth).toBe(`${BROWSER_LIVE}px`)
    const host = document.querySelector<HTMLElement>('.ag-tabs-more-host')!
    // MUTATION: draw the names again and More (448 px) is wider than the drawer.
    expect(moreWide(host)).toBeLessThan(358 - BROWSER_LIVE)
  })

  it('lets More shrink, with an ellipsis, rather than the strip that holds the open tab', () => {
    const { container } = render(
      <div className="drawer ctl-drawer ag-split">
        <div className="ag-tabs-edge">
          <div className="c-tabs ag-split-tabs" role="tablist" />
          <span className="c-menu-host ag-tabs-more-host">
            <button className="ag-tabs-more">
              <span className="ag-tabs-more-label">More 7</span>
            </button>
          </span>
        </div>
      </div>,
    )
    for (const env of [PHONE, MID, WIDE]) {
      // MUTATION: `flex: none` again and More never gives way to the open tab.
      expect(painted(container.querySelector('.ag-tabs-more-host')!, ['flex', 'flex-shrink'], env)).toMatch(/^0 1 auto$|^1$/)
      expect(painted(container.querySelector('.ag-tabs-more-host')!, 'min-width', env)).toBe('0')
      expect(painted(container.querySelector('.ag-tabs-more-label')!, 'text-overflow', env)).toBe('ellipsis')
      expect(painted(container.querySelector('.ag-tabs-more-label')!, ['overflow', 'overflow-x'], env)).toBe('hidden')
    }
  })

  it('draws the strip’s `›` at the strip’s end, not over More', () => {
    size(358)
    strip(row(), 'browser', { hasBrowser: true, sessionOpen: true })
    const edge = document.querySelector<HTMLElement>('.ag-tabs-edge')!
    const host = document.querySelector<HTMLElement>('.ag-tabs-more-host')!
    // MUTATION: leave `›` at `right: 0` and it sits over More's text.
    expect(edge.style.getPropertyValue('--ag-more-w')).toBe(`${moreWide(host)}px`)
    edge.setAttribute('data-more', 'end')
    const right = cascade(AGENTS_CSS, edge, 'right', PHONE, 'after').winner?.value ?? ''
    expect(right).toContain('var(--ag-more-w')
  })
})

describe('BT-007: every claude-code-browser agent has its Browser tab, live or finished', () => {
  const ids = (t: Task, pane: string, hasBrowser = false) =>
    paneTabs({ task: t, base: '/x', hasBrowser, sessionOpen: false, childCount: 0, runs: 1, ckpt: null, ckptCount: 4, pane: pane as never }).map((x) => x.id)

  it('a finished browser agent keeps Browser on Attempts, Logs and Details', () => {
    for (const pane of ['attempts', 'logs', 'detail', 'artifacts']) {
      // MUTATION: base the tab on `hasBrowser` alone (the live read, or `/browser`) and it vanishes here.
      expect(ids(finished(), pane)).toContain('browser')
    }
  })

  it('and after picking Attempts, Browser is on the bar or in More, never gone', () => {
    size(358)
    const { reopen } = strip(finished(), 'browser')
    reopen('attempts')
    const onBar = within(screen.getByRole('tablist', { name: 'Agent panes' }))
      .getAllByRole('tab')
      .map((b) => b.querySelector('.c-tab-label')?.textContent)
    const more = document.querySelector('.ag-tabs-more')?.getAttribute('title') ?? ''
    expect([...onBar, ...more.split(', ')]).toContain('Browser')
  })

  it('an agent of another runner has none, and neither has a merge held by its owner’s questions', () => {
    expect(ids(row({ runner_profile: 'claude-code' }), 'attempts')).not.toContain('browser')
    const held = row({
      state: 'PARKED',
      park_reason: 'HUMAN_REQUIRED',
      blocked_by: [{ reason: 'HUMAN_REQUIRED', park_phase: 'owner_questions', owner_questions: { questions: [] } }] as never,
    })
    // MUTATION: drop the hold check and a git-only merge draws a Browser tab over no browser (#2313).
    expect(ids(held, 'detail')).not.toContain('browser')
  })
})

describe('BT-013: the bar does not change with the open tab', () => {
  it('fitTabs: the same tabs stay on the bar whichever tab is open, and the open one joins them', () => {
    const keys = ['detail', 'browser', 'logs', 'changes', 'children', 'attempts', 'artifacts', 'checkpoints']
    const W = [70, 75, 50, 92, 92, 96, 104, 118]
    for (const room of [500, 600, 700]) {
      const base = fitTabs(keys, W, room, 66, 'detail')
      for (const open of keys) {
        const shown = fitTabs(keys, W, room, 66, open)
        // MUTATION: fill the bar around the open tab and a tab on it steps into More.
        expect(shown.filter((k) => k !== open)).toEqual(base.filter((k) => k !== open))
        expect(shown).toContain(open)
        // Drawn in the fixed order.
        expect(shown).toEqual(keys.filter((k) => shown.includes(k)))
        // Beside More when there is one; every tab, and no More, when they all fit.
        const used = shown.reduce((n, k) => n + W[keys.indexOf(k)]!, 0)
        expect(used).toBeLessThanOrEqual(shown.length < keys.length ? room - 66 : room)
      }
    }
  })

  it('a finished agent at 1024: picking Artifacts from More leaves Attempts on the bar', () => {
    size(499)
    const { onBar, reopen } = strip(finished(), 'browser')
    const before = onBar()
    expect(before).toContain('Attempts')
    reopen('artifacts')
    // MUTATION: the old fit -- `Details, Logs, Artifacts 65`, Attempts sent into More.
    expect(onBar()).toEqual([...before, 'Artifacts'].filter((l, i, a) => a.indexOf(l) === i).sort(byOrder))
    expect(onBar()).toContain('Attempts')
  })

  it('a running agent at 1024: opening Logs or Attempts leaves Browser LIVE on the bar', () => {
    size(499)
    const { onBar, reopen } = strip(row(), 'browser', { hasBrowser: true, sessionOpen: true })
    expect(onBar()).toContain('Browser')
    for (const open of ['logs', 'attempts', 'detail']) {
      reopen(open)
      // MUTATION: leave Browser unranked and it is the tab given up to keep room for More.
      expect(onBar()).toContain('Browser')
    }
  })
})

const ORDER = ['Details', 'Browser', 'Logs', 'Changes', 'Children', 'Attempts', 'Artifacts', 'Checkpoints']
function byOrder(a: string, b: string): number {
  return ORDER.indexOf(a) - ORDER.indexOf(b)
}

describe('the render fixture: the running browser agent has a long log', () => {
  it('serves task_b40e5a1c a log of more than forty steps, each with its own id', async () => {
    const { USE_FIXTURES, loadTranscript } = await import('../api')
    expect(USE_FIXTURES).toBe(true)
    const read = await loadTranscript('task_b40e5a1c')
    if (read.status !== 'ok') throw new Error(`the fixture transcript read ${read.status}`)
    const steps = read.data.steps ?? []
    // MUTATION: drop the browser steps and the log is four lines, which no pane runs past.
    expect(steps.length).toBeGreaterThan(40)
    expect(new Set(steps.map((s) => s.id)).size).toBe(steps.length)
    // The ordinary running agent keeps its short log.
    const ordinary = await loadTranscript('task_a073aff5')
    if (ordinary.status !== 'ok') throw new Error(`the fixture transcript read ${ordinary.status}`)
    expect((ordinary.data.steps ?? []).length).toBeLessThan(10)
  })
})
