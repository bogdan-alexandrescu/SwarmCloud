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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { landed } from './reads'

// The fixtures still answer every read; the Details read is only watched, so
// a case can wait for it to land before it ends (#605, below).
const watched = vi.hoisted(() => ({ loadAgentRun: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, loadAgentRun: watched.loadAgentRun }
})

const { App } = await import('../App')
const { loadAgentRun: realAgentRun } = await vi.importActual<typeof import('../api')>('../api')

const WIDE: CascadeEnv = { width: 1440 }
const RUNNING = 'task_a073aff5'
const FINISHED = 'task_a3881bec'

beforeEach(() => {
  // `restoreMocks` strips the implementation between cases; put it back.
  watched.loadAgentRun.mockImplementation(realAgentRun)
})

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
  localStorage.clear()
})

async function split(id: string): Promise<HTMLElement> {
  window.location.hash = `#work/task/${id}`
  render(<App />)
  const s = await waitFor(() => {
    const el = document.querySelector<HTMLElement>('.ag-split')
    expect(el).not.toBeNull()
    // The header has the task, and the Details pane has its run.
    expect(el!.querySelector('.ag-head-facts')).not.toBeNull()
    // A finished agent opens on Details, a running one on its log (U11a).
    expect(el!.querySelector(id === RUNNING ? '.ag-split-pane > .ag-logs' : '.ag-split-pane .run-stack')).not.toBeNull()
    return el!
  })
  // EVERY DETAILS READ THIS SPLIT STARTED HAS LANDED BEFORE THE CASE GOES ON
  // (#605). A running agent's split mounts Details, then moves to Logs, so its
  // read can outlive the pane; one that landed after the case's environment
  // was torn down failed a CI run whose every test had passed (`window is not
  // defined`, AgentDetail.tsx's load). `0`: wait for the reads made, whether
  // or not this agent's split made one.
  await landed(watched.loadAgentRun, 0)
  return s
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
    // THE META LINE (agent-details-v3.html A): profile · class · account,
    // each once, as items the sheet separates with a `·`.
    const meta = [...head.querySelectorAll('.ag-head-facts > li')].map((li) => (li.textContent ?? '').trim())
    expect(meta[0], 'the profile is not in the header').toBe('claude-code')
    expect(meta[1], 'the class is not in the header').toMatch(/^standard( · \d+ vCPU · \d+ GiB)?$/)
    // The pane's own <h1> is hidden under the header (asserted below), and it
    // names the agent, not its profile; what the pane SHOWS repeats nothing.
    const paneShown = pane.cloneNode(true) as HTMLElement
    for (const el of paneShown.querySelectorAll('.head, [hidden]')) el.remove()
    expect(times(paneShown, 'claude-code'), 'the pane repeats the profile').toBe(0)
    // The whole id with its copy, once, in the header.
    expect(head.querySelector('button[aria-label^="Copy task id"]'), 'no id copy in the header').not.toBeNull()
    expect(pane.querySelector('button[aria-label^="Copy task id"]'), 'the pane repeats the id copy').toBeNull()
    // The id is behind its copy button, whose title is the whole id; it is
    // printed nowhere (the pane's own <h1> is hidden under the header, and
    // the title names the agent -- item G).
    expect(painted(pane.querySelector('.head')!, 'display', WIDE)).toBe('none')
    expect(head.querySelector<HTMLElement>('button[aria-label^="Copy task id"]')!.title).toContain(FINISHED)
    const shown = s.cloneNode(true) as HTMLElement
    // A tab not on screen is not printed (the Checkpoints pane is mounted
    // hidden, for its count -- U11a D21).
    for (const el of shown.querySelectorAll('.ag-split-pane .head, .ag-head-title, [hidden]')) el.remove()
    expect(times(shown, FINISHED), 'the id is printed').toBe(0)
    // The facts the header owns are said there once; the pane keys none of them.
    const keys = (root: Element) => [...root.querySelectorAll('.ctl-fact > b')].map((b) => (b.textContent ?? '').trim())
    expect(meta.some((m) => m.startsWith('account ')), 'the header has no account').toBe(true)
    expect(meta.some((m) => /tenant/.test(m)), 'the tenant is back in the header; the panel says it').toBe(false)
    // (Each attempt's card keys its own account, behind Resources' Details.)
    const facts = pane.querySelector('.run-stack > .dt-now')!
    for (const k of ['started', 'account', 'tenant', 'state', 'id', 'ended', 'wf', 'workflow', 'profile', 'class']) {
      expect(keys(facts), `the pane repeats ${k}`).not.toContain(k)
    }
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
    // Details' one log line is in the pane's flow, in the leading card at the
    // top of the stack (agent-details-v3.html A).
    expect(pane.querySelector('.dt > .dt-now > .ag-loglast')).not.toBeNull()
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
