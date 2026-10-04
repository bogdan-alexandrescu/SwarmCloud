/**
 * VISUAL QA Q4 (owner, 2026-10-02): the agent split.
 *
 *   * The log dock drew at the TOP of the Details tab whenever the tab was
 *     short or still reading, and was cut off at the right. The column is a
 *     full-height flex column whose pane takes the slack. The dock itself is
 *     gone (U11a, owner decision 2026-10-04): the log is the Logs tab, which
 *     fills that pane and is never wider than it.
 *   * The header was clipped at the split's width ("task_0064264e0630462…"
 *     cut, the tabs "Details Children 0 Att…" cut): the title ellipses and the
 *     actions keep their width; the tabs scroll sideways if they do not fit.
 *
 * MUTATIONS: drop the pane's `flex: 1` or the column's height,
 * let the title wrap, or let the tabs wrap -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
})

async function split(): Promise<HTMLElement> {
  window.location.hash = '#work/task/task_a073aff5'
  render(<App />)
  return waitFor(() => {
    const s = document.querySelector<HTMLElement>('.ag-split')
    expect(s).not.toBeNull()
    // A running agent opens on its log (U11a).
    expect(s!.querySelector('.ag-split-pane > .ag-logs')).not.toBeNull()
    return s!
  })
}

describe('Q4: the open tab takes the column to its foot', () => {
  it('is the column’s last row, and the column is the viewport’s height at any pane height', async () => {
    const s = await split()
    const pane = s.querySelector(':scope > .ag-split-pane')!
    expect(s.lastElementChild, 'something is docked under the pane').toBe(pane)
    expect(painted(s, 'display', WIDE)).toBe('flex')
    expect(painted(s, 'flex-direction', WIDE)).toBe('column')
    expect(painted(pane, ['flex', 'flex-grow'], WIDE)).toMatch(/^1\b/)
    // A column with only a max-height is as short as a short pane. In the
    // grid it is the viewport's height.
    const host = document.createElement('div')
    host.innerHTML = '<div class="app has-inspector"><main class="work"></main><div class="drawer ctl-drawer ag-split"></div></div>'
    document.body.appendChild(host)
    try {
      expect(painted(host.querySelector('.ctl-drawer')!, 'height', WIDE) ?? '', 'the column has no height').toMatch(/calc\(100vh/)
    } finally {
      host.remove()
    }
  })

  it('draws the log no wider than the pane it fills', async () => {
    const s = await split()
    const logs = s.querySelector('.ag-split-pane > .ag-logs')!
    expect(painted(logs, 'min-width', WIDE)).toBe('0')
    expect(painted(logs.querySelector('.ag-logs-bar')!, 'min-width', WIDE)).toBe('0')
  })
})

describe('Q4: the detail header and tabs fit the split', () => {
  it('ellipses the title and keeps the actions whole', async () => {
    const s = await split()
    // TWO LINES, THEN AN ELLIPSIS (walkthrough C, 2026-10-03): the title is
    // clamped rather than cut at one line, the whole name in its title.
    const title = s.querySelector('.ag-head-title')!
    expect(painted(title, '-webkit-line-clamp', WIDE)).toBe('2')
    expect(painted(title, 'overflow', WIDE)).toBe('hidden')
    expect(title.getAttribute('title')).toBe(title.textContent)
    expect(painted(s.querySelector('.ag-head-row')!, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(s.querySelector('.ag-head-actions')!, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)\b/)
  })

  it('scrolls the tabs sideways rather than cutting or wrapping them', async () => {
    const s = await split()
    const tabs = s.querySelector('.c-tabs[role="tablist"]')!
    expect(painted(tabs, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(tabs, ['overflow-x', 'overflow'], WIDE)).toBe('auto')
    expect(painted(tabs.querySelector('button')!, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)\b/)
  })
})
