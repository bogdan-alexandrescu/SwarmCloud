/**
 * WALKTHROUGH B (owner, 2026-10-03, live console at 1440x900): the agent
 * detail in the split.
 *
 * Measured: the split's close ✕ was drawn over the Copy link button; the
 * metadata appeared three times (the header's "task_… · claude-code ·
 * standard · 1u · gen 1", the pane's "claude-code · standard · 1 attempt",
 * and the Details facts' "state ? succeeded" and "id task_… copy"); the log
 * panel floated mid-panel and covered content ("requested vs utilised").
 *
 * Now the header block says it ONCE -- state pill, title, the id with copy,
 * profile · class · units · gen, started / ended, account, tenant, workflow
 * link -- with Copy link and ✕ in their own places in its action row; the
 * pane repeats none of it; and the log is the bottom dock of viewers.html A:
 * the column does not scroll, the pane above the dock does, so the dock never
 * covers it; it is folded to its one line when the agent has finished and
 * open while it runs, and its height is remembered per device.
 *
 * MUTATIONS: put the summary back on the pane's Screen, the started / account
 * / id facts back in Details, the ✕ back on the column, or `position: sticky`
 * back on the dock -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 8000 }
const RUNNING = 'task_a073aff5'
const FINISHED = 'task_a3881bec'

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
  localStorage.clear()
})

async function split(id: string): Promise<HTMLElement> {
  window.location.hash = `#work/task/${id}`
  render(<App />)
  return waitFor(() => {
    const s = document.querySelector<HTMLElement>('.ag-split')
    expect(s).not.toBeNull()
    // The header has the task, and the Details pane has its run.
    expect(s!.querySelector('.ag-head-facts')).not.toBeNull()
    expect(s!.querySelector('.ag-split-pane .run-stack')).not.toBeNull()
    expect(s!.querySelector('.ag-logdock')).not.toBeNull()
    return s!
  }, WAIT)
}

/** How many times `needle` is in the visible text of `root` (attributes are not text). */
function times(root: Element, needle: string): number {
  return (root.textContent ?? '').split(needle).length - 1
}

describe('B: the metadata is said once, in the header block', () => {
  it('draws profile · class, the id and the facts in the header and nowhere in the pane', async () => {
    const s = await split(FINISHED)
    const head = s.querySelector('.ag-head')!
    const pane = s.querySelector('.ag-split-pane')!
    expect(times(head, 'claude-code · standard'), 'profile · class is not in the header').toBe(1)
    expect(times(pane, 'claude-code · standard'), 'the pane repeats profile · class').toBe(0)
    // The whole id with its copy, once, in the header.
    expect(head.querySelector('button[aria-label^="Copy task id"]'), 'no id copy in the header').not.toBeNull()
    expect(pane.querySelector('button[aria-label^="Copy task id"]'), 'the pane repeats the id copy').toBeNull()
    // The whole id is printed once, by its copy (the pane's own <h1> is hidden
    // under the header, and the title names the agent -- item G).
    expect(painted(pane.querySelector('.head')!, 'display', WIDE)).toBe('none')
    const shown = s.cloneNode(true) as HTMLElement
    for (const el of shown.querySelectorAll('.ag-split-pane .head, .ag-head-title')) el.remove()
    expect(times(shown, FINISHED), 'the id is printed more than once').toBe(1)
    // The facts the header owns, keyed once; the pane keys none of them.
    const keys = (root: Element) => [...root.querySelectorAll('.ctl-fact > b')].map((b) => (b.textContent ?? '').trim())
    // (Each attempt's card keys its own account; the run's facts are the
    // Details pane's first section.)
    const facts = pane.querySelector('.run-stack > section')!
    for (const k of ['started', 'account', 'tenant']) {
      expect(keys(head), `the header has no ${k}`).toContain(k)
      expect(keys(facts), `the pane repeats ${k}`).not.toContain(k)
    }
    for (const k of ['state', 'id', 'ended', 'wf', 'workflow']) expect(keys(facts), `the pane repeats ${k}`).not.toContain(k)
    expect(head.querySelector('[data-mark]'), 'no state pill in the header').not.toBeNull()
  })
})

describe('B: Copy link and the close ✕ each have their own space', () => {
  it('puts the ✕ in the header action row after Copy link, laid out in the row rather than over it', async () => {
    const s = await split(RUNNING)
    const actions = s.querySelector('.ag-head-actions')!
    const buttons = [...actions.querySelectorAll('button')]
    const copy = buttons.findIndex((b) => (b.textContent ?? '').trim() === 'Copy link')
    const close = buttons.findIndex((b) => b.classList.contains('drawer-close'))
    expect(copy, 'no Copy link').toBeGreaterThanOrEqual(0)
    expect(close, 'the ✕ is not in the header action row').toBeGreaterThan(copy)
    expect(s.querySelectorAll('.drawer-close'), 'not exactly one close').toHaveLength(1)
    const x = buttons[close]!
    expect(painted(x, 'position', WIDE) ?? 'static', 'the ✕ is positioned over the row').toBe('static')
  })
})

describe('B: the log is the bottom dock of the detail column', () => {
  it('scrolls the pane above it and never the column, so the dock covers nothing', async () => {
    const s = await split(RUNNING)
    const pane = s.querySelector(':scope > .ag-split-pane')!
    const dock = s.querySelector(':scope > .ag-logdock')!
    expect([...s.children].indexOf(dock)).toBeGreaterThan([...s.children].indexOf(pane))
    expect(painted(s, ['overflow-y', 'overflow'], WIDE), 'the column scrolls, so the dock floats over it').toBe('hidden')
    expect(painted(pane, ['overflow-y', 'overflow'], WIDE)).toBe('auto')
    expect(painted(pane, 'min-height', WIDE)).toBe('0')
    expect(painted(pane, ['flex', 'flex-grow'], WIDE)).toMatch(/^1\b/)
    expect(painted(dock, 'position', WIDE) ?? 'static', 'the dock is laid over the pane').toBe('static')
    expect(painted(dock, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)\b/)
  })

  it('is open while the agent runs and folded to its one line once it has finished', async () => {
    let s = await split(RUNNING)
    expect(s.querySelector('.ag-logdock')!.classList.contains('is-open'), 'a running agent opens folded').toBe(true)
    window.location.hash = ''
    document.body.innerHTML = ''
    s = await split(FINISHED)
    const dock = s.querySelector('.ag-logdock')!
    expect(dock.classList.contains('is-strip'), 'a finished agent opens with its log open').toBe(true)
    expect(dock.querySelector('button.ag-logdock-line[aria-expanded="false"]')).not.toBeNull()
  })

  it('remembers its height per device, through the drag handle', async () => {
    localStorage.setItem('swarm.agents.logdock.h', '420')
    const s = await split(RUNNING)
    const dock = s.querySelector<HTMLElement>('.ag-logdock')!
    expect(dock.style.height).toBe('420px')
    expect(dock.querySelector('[role="separator"][aria-label="Resize the log"]')).not.toBeNull()
  })
})
