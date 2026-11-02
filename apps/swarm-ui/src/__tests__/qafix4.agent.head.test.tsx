/**
 * LANE QAFIX4-AGENT-HEAD (part of #1680, visual QA run 4,
 * wf_ecd935541ced4a539540, the fixture build of main at fabe4d6c, 1440 and
 * 390 px, light and dark). The agent's head: its pane tabs and its dispatch
 * chips.
 *
 *   N15  `Changes not counted` read as one sentence or as a disabled tab: the
 *        count's words ran on from the name, on the bar and in More, and at
 *        390 More wrapped `Artifacts not counted` onto two lines and its
 *        popover covered the Logs toolbar (flow-find-failure-desktop-D5,
 *        flow-find-failure-phone-390-P4, flow-workflow-graph-light-11/dark-11).
 *   N16  `One PR for all steps` and `adds to the shared PR` said nothing about
 *        what either means for this agent's pull request
 *        (flow-workflow-graph-light-11/dark-11).
 *
 * And two owner decisions of 2026-10-22 (Console Design Calls 2):
 *
 *   c01  when the bar overflows it is filled by rank -- Details, Logs,
 *        Attempts, then the rest -- so the failure path never moves.
 *   c05  the head names each count: `standard class · 4 vCPU · 8 GiB`.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'
import type { Task } from '../types'

const api = vi.hoisted(() => ({
  loadResourceClasses: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

// The class catalogue, as the head's hook hands it: unanswered unless a test sets it.
const catalogue = vi.hoisted(() => ({ now: null as unknown }))
vi.mock('../Blockers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../Blockers')>()
  return { ...actual, useResourceClasses: () => catalogue.now }
})

const { AgHeadMeta } = await import('../agent-split/head')
const { AgTabsEdge, paneTabs } = await import('../agent-split/tabs')
const { HELP } = await import('../help')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const ID = 'task_0123456789abcdef0123'

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', completed_at: null, ...over })
}
const ended = () => row({ state: 'FAILED', completed_at: new Date().toISOString() })

// THE BAR AS A 390 PHONE LAYS IT OUT: each tab's width as drawn with its
// count, and More's. jsdom lays nothing out, so the widths are given.
const WIDTH: Readonly<Record<string, number>> = {
  Details: 62,
  Browser: 70,
  Logs: 46,
  Changes: 130,
  Children: 90,
  Attempts: 84,
  Artifacts: 130,
  Checkpoints: 118,
}
// More is measured from what it draws (VQ-008 review, BT-004): its label
// (`More <n>`) and its chevron; the host's own box is the width CSS squeezed
// it to and is never read for the fit.
const MORE_LABEL = 46
const MORE_CHEVRON = 10
let room = 358
const proto = HTMLElement.prototype
const had = {
  client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
  offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
  scroll: Object.getOwnPropertyDescriptor(proto, 'scrollWidth'),
}
beforeEach(() => {
  room = 358
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-edge') ? room : 0
    },
  })
  Object.defineProperty(proto, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (this.classList.contains('ag-tabs-more-host')) return 0
      if (this.getAttribute('aria-hidden') === 'true' && this.parentElement?.classList.contains('ag-tabs-more')) return MORE_CHEVRON
      if (this.getAttribute('role') !== 'tab' || this.closest('.ag-tabs-edge') === null) return 0
      return WIDTH[this.querySelector('.c-tab-label')?.textContent ?? ''] ?? 0
    },
  })
  Object.defineProperty(proto, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-more-label') ? MORE_LABEL : 0
    },
  })
})
afterEach(() => {
  for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset], ['scrollWidth', had.scroll]] as const) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
})

/** The split's bar, as AgentSplit hands it its tabs, inside the split (on Logs when `pane` is logs). */
function bar(t: Task, current: string) {
  const tabs = paneTabs({ task: t, base: '/agents/live/x', hasBrowser: false, sessionOpen: false, childCount: null, runs: 3, ckpt: null, ckptCount: 2 })
  const r = render(
    <div className={`drawer ctl-drawer ag-split${current === 'logs' ? ' on-logs' : ''}`}>
      <AgTabsEdge
        current={current}
        onSelect={() => {}}
        tabs={tabs.map((x) => ({
          key: x.id,
          label: x.label,
          ...((x.id === 'detail' && x.count === null) || x.id === 'logs' ? {} : { count: x.count, why: x.say ?? undefined }),
        }))}
      />
    </div>,
  )
  act(() => {
    fireEvent(window, new Event('resize'))
  })
  const strip = screen.getByRole('tablist', { name: 'Agent panes' })
  const onBar = () => within(strip).getAllByRole('tab').map((b) => b.querySelector('.c-tab-label')?.textContent)
  return { ...r, strip, onBar }
}

/**
 * An element's accessible description, by the accname rule this needs: the
 * text its `aria-describedby` names, else its title (no dependency declares
 * `dom-accessibility-api`, so it is not imported).
 */
function computeAccessibleDescription(el: Element): string {
  const ids = (el.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
  if (ids.length > 0) return ids.map((id) => document.getElementById(id)?.textContent ?? '').join(' ').trim()
  return (el.getAttribute('title') ?? '').trim()
}

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('N15: a tab’s count is a badge, apart from its name', () => {
  it('a running agent: the label and `not yet` are two elements, and the name says them apart', () => {
    room = 2000
    const { strip } = bar(row(), 'detail')
    const changes = within(strip).getByRole('tab', { name: /^Changes/ })
    // MUTATION: draw the count inside the label's span and the badge is not its own element.
    expect(flat(changes.querySelector('.c-tab-label'))).toBe('Changes')
    expect(flat(changes.querySelector('em'))).toBe('not yet')
    // MUTATION: drop the comma from the tab's accessible name and it reads `Changes not yet` as one phrase.
    expect(within(strip).getByRole('tab', { name: 'Changes, not yet' })).toBe(changes)
    expect(within(strip).getByRole('tab', { name: 'Artifacts, not yet' })).not.toBeNull()
    // MUTATION: name only the counts in words and a number runs on from its name again.
    expect(within(strip).getByRole('tab', { name: 'Attempts, 3' })).not.toBeNull()
    // MUTATION: give a tab with no count a comma and Logs reads `Logs, `.
    expect(within(strip).getByRole('tab', { name: 'Logs' })).not.toBeNull()
  })

  it('an ended agent: `not counted`, apart from the name, on the bar and in More', () => {
    const { strip } = bar(ended(), 'detail')
    // MUTATION: drop the bar's accessible names and `Attempts 3` is a run-on again.
    expect(within(strip).getByRole('tab', { name: 'Attempts, 3' })).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^More \d+ tabs: / }))
    const menu = screen.getByRole('group', { name: 'More agent panes' })
    const item = within(menu).getByRole('button', { name: 'Artifacts, not counted' })
    // MUTATION: drop the item's own name and the menu reads `Artifacts not counted`.
    expect(flat(item.querySelector('.c-tab-label'))).toBe('Artifacts')
    expect(flat(item.querySelector('em'))).toBe('not counted')
    // MUTATION: drop the item's `aria-label` and Changes is a run-on in the menu.
    expect(within(menu).getByRole('button', { name: 'Changes, not counted' })).not.toBeNull()
  })

  it('the badge is drawn apart from the label, on the bar and in More', () => {
    const { strip } = bar(ended(), 'detail')
    const tab = within(strip).getByRole('tab', { name: /^Attempts/ })
    const label = tab.querySelector('.c-tab-label')!
    const badge = tab.querySelector('em')!
    const look = (el: Element, env: CascadeEnv) => [
      painted(el, ['background', 'background-color'], env),
      painted(el, ['border', 'border-color'], env),
    ]
    for (const env of [WIDE, PHONE, { ...WIDE, theme: 'light' as const }]) {
      // MUTATION: drop the badge rule and the count is drawn exactly as the label is.
      expect(look(badge, env)).not.toEqual(look(label, env))
      // MUTATION: drop the badge's background and only the face sets the count apart.
      expect(painted(badge, ['background', 'background-color'], env)).not.toBeNull()
    }
    fireEvent.click(screen.getByRole('button', { name: /^More \d+ tabs: / }))
    const item = within(screen.getByRole('group', { name: 'More agent panes' })).getByRole('button', { name: /^Artifacts/ })
    // MUTATION: scope the badge to the bar alone and More's counts run on from their names.
    expect(look(item.querySelector('em')!, PHONE)).not.toEqual(look(item.querySelector('.c-tab-label')!, PHONE))
  })
})

describe('N15: at 390 More wraps no item and keeps off the Logs toolbar', () => {
  it('draws each item on one line', () => {
    const { strip } = bar(ended(), 'logs')
    expect(strip).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^More \d+ tabs: / }))
    const menu = screen.getByRole('group', { name: 'More agent panes' })
    const item = within(menu).getByRole('button', { name: /^Artifacts/ })
    const badge = item.querySelector('em')!
    // MUTATION: let the item wrap and `not counted` drops under `Artifacts`.
    expect(painted(item, 'flex-wrap', PHONE)).toBe('nowrap')
    // MUTATION: leave `.c-menu-item > em`'s `flex-basis: 100%` and the badge takes a line of its own.
    expect(painted(badge, ['flex-basis', 'flex'], PHONE) ?? '').not.toMatch(/100%/)
    // MUTATION: leave `.c-menu-item > em`'s `white-space: normal` and `not counted` breaks at its space.
    expect(painted(badge, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(item, 'white-space', PHONE)).toBe('nowrap')
  })

  it('on a phone\'s Logs it opens upward, over the head, not down over the Logs toolbar', () => {
    bar(ended(), 'logs')
    fireEvent.click(screen.getByRole('button', { name: /^More \d+ tabs: / }))
    const menu = screen.getByRole('group', { name: 'More agent panes' })
    // MUTATION: drop the on-logs placement and the menu hangs `top: calc(100% + 4px)`, over the Logs bar.
    expect(painted(menu, 'top', PHONE)).toBe('auto')
    // MUTATION: keep `bottom` unset and the menu has no edge to open from.
    expect((painted(menu, 'bottom', PHONE) ?? '').replace(/\s+/g, ' ')).toBe('calc(100% + 4px)')
    // On desktop the column scrolls on Logs too (AGENT-HEAD-B, BGD's pick AH1
    // B, 2026-10-28): the head may be gone from above the stuck strip, so the
    // menu drops, over the log's bar (the strip's layer is above it).
    expect((painted(menu, 'top', WIDE) ?? '').replace(/\s+/g, ' ')).toBe('calc(100% + 4px)')
  })

  it('off Logs it still drops under the bar, where the pane has no toolbar to cover', () => {
    bar(ended(), 'detail')
    fireEvent.click(screen.getByRole('button', { name: /^More \d+ tabs: / }))
    const menu = screen.getByRole('group', { name: 'More agent panes' })
    // MUTATION: open upward everywhere and on a phone's sticky strip the menu leaves the top of the screen.
    expect((painted(menu, 'top', PHONE) ?? '').replace(/\s+/g, ' ')).toBe('calc(100% + 4px)')
  })
})

describe('c01: an overflowing bar ranks Details, Logs and Attempts first', () => {
  for (const open of ['detail', 'logs', 'attempts'] as const) {
    it(`at 390 with ${open} open: Details · Logs · Attempts 3 · More <n> ▾`, () => {
      const { onBar } = bar(ended(), open)
      // MUTATION: fill the bar in draw order and it reads `Details · Logs · Changes`, Attempts hidden in More.
      expect(onBar()).toEqual(['Details', 'Logs', 'Attempts'])
      // MUTATION (c02/c20 kept): move More off the bar's end or drop its count.
      // (BROWSER-TAB-TABS, #2423: More draws `More <n>`; the names are its name, not its glass.)
      expect(flat(document.querySelector('.ag-tabs-edge > :last-child .ag-tabs-more-label'))).toMatch(/^More \d+$/)
    })
  }

  // A 390 bar has room for three of these tabs, so the open one takes the
  // lowest-ranked place; where there is room the bar does not change with
  // the open tab at all (BROWSER-TAB-TABS, BT-013, browser-tab-tabs.test.tsx).
  it('with Changes open, Changes joins the bar and Attempts steps into More', () => {
    const { onBar } = bar(ended(), 'changes')
    // MUTATION: drop the open tab's place on the bar (QA12-18) and Changes is chosen and unseen.
    expect(onBar()).toEqual(['Details', 'Logs', 'Changes'])
    fireEvent.click(screen.getByRole('button', { name: /^More \d+ tabs: / }))
    const items = within(screen.getByRole('group', { name: 'More agent panes' }))
      .getAllByRole('button')
      .map((b) => b.querySelector('.c-tab-label')?.textContent)
    // MUTATION: pin Attempts to the bar and Changes pushes the bar past its room.
    expect(items).toContain('Attempts')
  })

  it('where every tab fits, nothing moves: the usual order, no More', () => {
    room = 2000
    const { onBar } = bar(ended(), 'attempts')
    // MUTATION: draw the bar in rank order and a wide desktop's tabs are shuffled.
    expect(onBar()).toEqual(['Details', 'Logs', 'Changes', 'Attempts', 'Artifacts', 'Checkpoints'])
    expect(screen.queryByRole('button', { name: /^More \d+ tabs: / })).toBeNull()
  })
})

describe('N16: each dispatch chip says what it means for this agent’s pull request', () => {
  const chips = (t: Task) => {
    const { container } = render(<AgHeadMeta task={t} />)
    return [...container.querySelectorAll<HTMLElement>('.ag-head-dchip')]
  }

  it('a contributing step: the strategy chip and the role chip each describe the PR', () => {
    const [strategy, role] = chips(row({ dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] } }))
    expect(flat(strategy)).toBe('One PR for all steps')
    expect(flat(role)).toBe('adds to the shared PR')
    // MUTATION: drop the strategy chip's description and `One PR for all steps` is a label with no meaning.
    expect(computeAccessibleDescription(strategy!)).toMatch(/one pull request for the whole workflow/i)
    // MUTATION: drop the role chip's description and the reader cannot tell this agent opens no PR of its own.
    expect(computeAccessibleDescription(role!)).toMatch(/opens no pull request of its own/i)
    expect(computeAccessibleDescription(role!)).toMatch(/integrat/i)
  })

  it('the integrating step: its role chip says it opens the one PR', () => {
    const [, role] = chips(row({ dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'integrator', integrates: ['task_a'] } }))
    // MUTATION: describe every role alike and the integrator reads as a contributor.
    expect(computeAccessibleDescription(role!)).toMatch(/opens the workflow’s one pull request/i)
  })

  it('a step with its own PR, and one that publishes nothing', () => {
    const [direct] = chips(row({ dispatch: { strategy: 'direct-pr', carrier: 'checkpoints', role: null, integrates: [] } }))
    // MUTATION: drop the strategy-only chip's description and `A PR per step` says nothing of this agent.
    expect(computeAccessibleDescription(direct!)).toMatch(/opens its own pull request/i)
    const [collect] = chips(row({ dispatch: { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] } }))
    // MUTATION: describe collect as a PR and the reader looks for one that never comes.
    expect(computeAccessibleDescription(collect!)).toMatch(/opens no pull request/i)
  })

  it('the `?` topic says the same, so a phone reaches it by tap and a keyboard by Tab', () => {
    const all = [HELP['strategy-on-a-task'].short, ...HELP['strategy-on-a-task'].long].join(' ')
    // MUTATION: drop the chips' meanings from the topic and only a hover tooltip carries them.
    expect(all).toMatch(/‘One PR for all steps’[^.]*one pull request for the whole workflow/i)
    expect(all).toMatch(/‘adds to the shared PR’[^.]*opens no pull request of its own/i)
    expect(all).toMatch(/‘opens the shared PR’[^.]*opens the workflow’s one pull request/i)
    expect(all).toMatch(/‘A PR per step’[^.]*opens its own pull request/i)
  })
})

describe('c05: the head names its class and the units of its size', () => {
  afterEach(() => {
    catalogue.now = null
  })

  it('reads `standard class · 4 vCPU · 8 GiB`', () => {
    catalogue.now = { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 10, units: 2 } }
    const { container } = render(<AgHeadMeta task={row()} />)
    const li = container.querySelectorAll('.ag-head-facts > li')[1]
    // MUTATION: print the class bare (`standard · 4 vCPU`) and its word is not a class.
    expect(flat(li)).toBe('standard class · 4 vCPU · 8 GiB')
  })

  it('names the class before the catalogue answers too', () => {
    const { container } = render(<AgHeadMeta task={row()} />)
    // MUTATION: name the class only once its size is known and `standard` stands bare.
    expect(flat(container.querySelectorAll('.ag-head-facts > li')[1])).toBe('standard class')
  })
})
