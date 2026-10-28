/**
 * LANE QA1-FIX-33 (visual QA pass 1, part of #2596): the agent's tab strip
 * (agent-split/tabs.tsx) and a capacity screen's cadence (capacityPoll.ts).
 *
 *   overview-agents-33  the strip shifted when Logs was opened instead of
 *                       Details: the open tab is set bold, so it grew.
 *   overview-agents-44  ArrowDown on `More 3` did nothing; Escape with More
 *                       open and focus in the list column closed the pane.
 *   overview-agents-45  picking a tab from More pushed another tab off the
 *                       bar: a tab in More kept the width of the count it had
 *                       at mount, so the room kept for it was too small.
 *   admin-shell-19      Pool limits' head said `every 30 s` at 1440 and
 *                       `every 1 min` at 390, with nothing saying why.
 *
 * jsdom lays nothing out, so each tab's width is given from the words it
 * draws (`wide`), as browser-tab-tabs.test.tsx gives them.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { TabDef } from '../components'
import { painted } from './marks'

const { AgTabsEdge } = await import('../agent-split/tabs')
const { capacityPoll, CAPACITY_PHONE_NOTE, POOLS_POLL_MS } = await import('../capacityPoll')

const WIDE = { width: 1440, height: 900 }

// A tab is 24 px of padding and 7 px a letter, and a count's badge 16 px more
// and 7 px a character -- the same sum for the bar's tab and the hidden copy.
const wide = (label: string, count: string | null) => 24 + 7 * label.length + (count === null ? 0 : 16 + 7 * count.length)

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
      if (this.tagName !== 'BUTTON' || this.closest('.ag-tabs-edge') === null) return 0
      if (this.closest('.ag-tabs-measure') !== null) {
        // The hidden copy draws its words from `data-text`.
        const label = this.querySelector('.c-tab-label')?.getAttribute('data-text') ?? ''
        return wide(label, this.querySelector('em')?.getAttribute('data-text') ?? null)
      }
      if (this.getAttribute('role') !== 'tab') return 0
      const label = this.querySelector('.c-tab-label')?.textContent ?? ''
      return wide(label, this.querySelector('em')?.textContent ?? null)
    },
  })
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

/** A running agent's tabs; Checkpoints' count is given. */
function defs(ckpt: string | null): TabDef[] {
  return [
    { key: 'detail', label: 'Details' },
    { key: 'logs', label: 'Logs' },
    { key: 'changes', label: 'Changes', count: 'not yet' },
    { key: 'attempts', label: 'Attempts', count: 1 },
    { key: 'artifacts', label: 'Artifacts', count: 3 },
    { key: 'checkpoints', label: 'Checkpoints', count: ckpt },
  ]
}

function strip(room: number, current: string, ckpt: string | null) {
  size(room)
  const draw = (cur: string, c: string | null) => (
    <div className="drawer ctl-drawer ag-split">
      <button type="button">a row in the list column</button>
      <AgTabsEdge current={cur} onSelect={() => {}} tabs={defs(c)} />
    </div>
  )
  const r = render(draw(current, ckpt))
  const settle = () => {
    for (let i = 0; i < 2; i++) {
      act(() => {
        fireEvent(window, new Event('resize'))
      })
    }
  }
  settle()
  const onBar = () =>
    within(screen.getByRole('tablist', { name: 'Agent panes' }))
      .getAllByRole('tab')
      .map((b) => b.querySelector('.c-tab-label')?.textContent ?? '')
  const redraw = (cur: string, c: string | null) => {
    r.rerender(draw(cur, c))
    settle()
  }
  return { ...r, onBar, redraw }
}

describe('overview-agents-33: a tab does not grow when it is opened', () => {
  it('reserves every label’s bold width, so opening Logs moves no tab', () => {
    strip(2000, 'logs', null)
    const labels = [...document.querySelectorAll<HTMLElement>('.ag-tabs-edge > .c-tabs > [role="tab"] > .c-tab-label')]
    expect(labels.length).toBe(6)
    for (const l of labels) {
      // MUTATION: drop the `data-label` the strip sets and the reserved copy is empty.
      expect(l.getAttribute('data-label')).toBe(l.textContent)
      // MUTATION: drop agent-tabs.css and the label is a run of text as wide as its weight.
      expect(painted(l, 'display', WIDE)).toBe('inline-flex')
      expect(painted(l, 'flex-direction', WIDE)).toBe('column')
      expect(painted(l, 'font-weight', WIDE, 'after')).toBe('600')
      expect(painted(l, 'height', WIDE, 'after')).toBe('0')
      // The copy is not read: a tab's name is its label once.
      expect(painted(l, 'content', WIDE, 'after')).toBe("attr(data-label) / ''")
    }
    // The open tab is still the one marked.
    expect(screen.getByRole('tab', { name: 'Logs' }).getAttribute('aria-selected')).toBe('true')
  })
})

describe('overview-agents-45: picking a tab from More moves no other tab', () => {
  it('keeps the bar’s other tabs when Checkpoints, whose count landed while it was in More, is opened', () => {
    // 570 px, with More's label and the sheet's padding and gaps beside the
    // tabs. At mount Checkpoints draws `—` (124 px); its count then lands as
    // `0 of 3` (159 px) while it is in More. On main the bar held Changes
    // (room kept for a 124 px Checkpoints) and lost it when Checkpoints opened.
    const { onBar, redraw } = strip(570, 'detail', null)
    redraw('detail', '0 of 3')
    const before = onBar()
    expect(before).not.toContain('Checkpoints')
    redraw('checkpoints', '0 of 3')
    const after = onBar()
    expect(after).toContain('Checkpoints')
    // MUTATION: measure a tab only while it is on the bar (no hidden copy) and
    // the room kept for Checkpoints is its `—` width: opening it pushes
    // Changes off the bar.
    expect(after.filter((l) => l !== 'Checkpoints')).toEqual(before)
  })

  it('draws the hidden copy’s words from attributes, so no tab’s name is found twice', () => {
    strip(542, 'detail', '0 of 3')
    const copy = document.querySelector('.ag-tabs-measure')!
    expect(copy.getAttribute('aria-hidden')).toBe('true')
    expect((copy.textContent ?? '').trim()).toBe('')
    expect(copy.querySelectorAll('button').length).toBe(6)
    for (const b of copy.querySelectorAll('button')) expect(b.getAttribute('tabindex')).toBe('-1')
    expect(screen.getAllByText('Details').length).toBe(1)
    expect(painted(copy, 'visibility', WIDE)).toBe('hidden')
    expect(painted(copy, 'width', WIDE)).toBe('0')
  })
})

describe('overview-agents-44: More answers the arrows, and its Escape is its own', () => {
  it('opens on ArrowDown at its first tab and moves through its tabs with the arrows, Home and End', () => {
    strip(300, 'detail', '0 of 3')
    const more = document.querySelector<HTMLButtonElement>('.ag-tabs-more')!
    more.focus()
    // MUTATION: drop More's key handler and ArrowDown leaves focus on More, closed.
    fireEvent.keyDown(more, { key: 'ArrowDown' })
    const menu = screen.getByRole('group', { name: 'More agent panes' })
    const items = within(menu).getAllByRole('button')
    expect(items.length).toBeGreaterThan(2)
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(items[0]!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1])
    fireEvent.keyDown(items[1]!, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(items[0]!, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[items.length - 1])
    fireEvent.keyDown(items[items.length - 1]!, { key: 'Home' })
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(items[0]!, { key: 'End' })
    expect(document.activeElement).toBe(items[items.length - 1])
  })

  it('opens at its last tab on ArrowUp', () => {
    strip(300, 'detail', '0 of 3')
    const more = document.querySelector<HTMLButtonElement>('.ag-tabs-more')!
    more.focus()
    fireEvent.keyDown(more, { key: 'ArrowUp' })
    const items = within(screen.getByRole('group', { name: 'More agent panes' })).getAllByRole('button')
    expect(document.activeElement).toBe(items[items.length - 1])
  })

  it('closes More on the first Escape pressed in the list column, and leaves the pane to the second', () => {
    // The split's own Escape from outside it (agent-split/focus.ts): a window
    // listener in the bubble phase that closes the pane unless the key was spent.
    let paneClosed = 0
    const split = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) paneClosed++
    }
    window.addEventListener('keydown', split)
    try {
      strip(300, 'detail', '0 of 3')
      fireEvent.click(document.querySelector('.ag-tabs-more')!)
      expect(screen.getByRole('group', { name: 'More agent panes' })).toBeTruthy()
      const row = screen.getByRole('button', { name: 'a row in the list column' })
      row.focus()
      // MUTATION: hear Escape only inside More and the first one closes the pane with More open.
      fireEvent.keyDown(row, { key: 'Escape' })
      expect(screen.queryByRole('group', { name: 'More agent panes' })).toBeNull()
      expect(paneClosed).toBe(0)
      // Focus was not in More, so it stays where it was.
      expect(document.activeElement).toBe(row)
      fireEvent.keyDown(row, { key: 'Escape' })
      expect(paneClosed).toBe(1)
    } finally {
      window.removeEventListener('keydown', split)
    }
  })

  it('returns focus to More when Escape closes it from inside', () => {
    strip(300, 'detail', '0 of 3')
    const more = document.querySelector<HTMLButtonElement>('.ag-tabs-more')!
    more.focus()
    fireEvent.keyDown(more, { key: 'ArrowDown' })
    const first = within(screen.getByRole('group', { name: 'More agent panes' })).getAllByRole('button')[0]!
    fireEvent.keyDown(first, { key: 'Escape' })
    expect(screen.queryByRole('group', { name: 'More agent panes' })).toBeNull()
    expect(document.activeElement).toBe(more)
  })
})

describe('admin-shell-19: a phone’s slower cadence says why', () => {
  const at = (phone: boolean) =>
    ((q: string) => ({ matches: phone, media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia

  it('notes `on a phone` below the phone breakpoint, and nothing on a desktop', () => {
    const was = window.matchMedia
    try {
      window.matchMedia = at(true)
      const poll = capacityPoll(POOLS_POLL_MS)
      expect(poll(null)).toBe(60_000)
      // MUTATION: return the bare number again and the head says `every 1 min` with no reason.
      expect(poll.note()).toBe(CAPACITY_PHONE_NOTE)
      expect(CAPACITY_PHONE_NOTE).toBe('on a phone')
      window.matchMedia = at(false)
      expect(poll(null)).toBe(30_000)
      expect(poll.note()).toBeNull()
    } finally {
      window.matchMedia = was
    }
  })

  it('says nothing where the phone’s cadence is no slower', () => {
    const was = window.matchMedia
    try {
      window.matchMedia = at(true)
      expect(capacityPoll(60_000).note()).toBeNull()
    } finally {
      window.matchMedia = was
    }
  })
})
