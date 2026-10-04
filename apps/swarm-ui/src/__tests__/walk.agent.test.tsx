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
 * pane repeats none of it. The log was the bottom dock of viewers.html A; it
 * is the Logs tab now (U11a, owner decision 2026-10-04): a running agent
 * opens on it, a finished one on Details, and nothing is drawn under the pane.
 *
 * MUTATIONS: put the summary back on the pane's Screen, the started / account
 * / id facts back in Details, the ✕ back on the column, or a log element
 * after the pane -- each turns a case red.
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
    // A finished agent opens on Details, a running one on its log (U11a).
    expect(s!.querySelector(id === RUNNING ? '.ag-split-pane > .ag-logs' : '.ag-split-pane .run-stack')).not.toBeNull()
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
    // A tab not on screen is not printed (the Checkpoints pane is mounted
    // hidden, for its count -- U11a D21).
    for (const el of shown.querySelectorAll('.ag-split-pane .head, .ag-head-title, [hidden]')) el.remove()
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

describe('B: the log is a tab, and nothing is drawn under the pane', () => {
  it('scrolls the pane and never the column, and puts nothing after the pane', async () => {
    const s = await split(FINISHED)
    const pane = s.querySelector(':scope > .ag-split-pane')!
    expect(s.lastElementChild, 'something is docked under the pane').toBe(pane)
    expect(painted(s, ['overflow-y', 'overflow'], WIDE), 'the column scrolls').toBe('hidden')
    expect(painted(pane, ['overflow-y', 'overflow'], WIDE)).toBe('auto')
    expect(painted(pane, 'min-height', WIDE)).toBe('0')
    expect(painted(pane, ['flex', 'flex-grow'], WIDE)).toMatch(/^1\b/)
    // Details' one log line is in the pane's flow, at its top.
    expect(pane.firstElementChild?.classList.contains('ag-loglast')).toBe(true)
  })

  it('opens a running agent on Logs and a finished one on Details', async () => {
    let s = await split(RUNNING)
    expect(s.querySelector('[role="tab"][aria-selected="true"] .c-tab-label')?.textContent).toBe('Logs')
    window.location.hash = ''
    document.body.innerHTML = ''
    s = await split(FINISHED)
    expect(s.querySelector('[role="tab"][aria-selected="true"] .c-tab-label')?.textContent).toBe('Details')
    expect(s.querySelector('.ag-logs'), 'a finished agent opens on its log').toBeNull()
  })
})
