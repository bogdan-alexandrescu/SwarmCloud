/**
 * VISUAL QA Q4 (owner, 2026-10-02): the agent split.
 *
 *   * The log dock drew at the TOP of the Details tab whenever the tab was
 *     short or still reading, and was cut off at the right: viewers.html A
 *     docks it under the detail column, above the API-reads strip, the full
 *     width of the detail. The column is now a full-height flex column whose
 *     pane takes the slack, so the dock is at its foot whatever the pane
 *     holds, and the dock cannot be wider than the column.
 *   * The header was clipped at the split's width ("task_0064264e0630462…"
 *     cut, the tabs "Details Children 0 Att…" cut): the title ellipses and the
 *     actions keep their width; the tabs scroll sideways if they do not fit.
 *
 * MUTATIONS: drop `margin-top: auto` from the dock or the column's height,
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
    expect(s!.querySelector('.ag-logdock')).not.toBeNull()
    return s!
  })
}

describe('Q4: the log dock is at the foot of the detail column', () => {
  it('comes after the pane, and the column pushes it to its foot at any pane height', async () => {
    const s = await split()
    const kids = [...s.children]
    const pane = s.querySelector(':scope > .ag-split-pane')!
    const dock = s.querySelector(':scope > .ag-logdock')!
    expect(kids.indexOf(dock), 'the dock is not after the pane').toBeGreaterThan(kids.indexOf(pane))
    expect(painted(s, 'display', WIDE)).toBe('flex')
    expect(painted(s, 'flex-direction', WIDE)).toBe('column')
    expect(painted(pane, ['flex', 'flex-grow'], WIDE)).toMatch(/^1\b/)
    expect(painted(dock, ['margin-top', 'margin'], WIDE)).toBe('auto')
    // A column with only a max-height is as short as a short pane: the dock
    // would sit under the tabs. In the grid it is the viewport's height.
    const host = document.createElement('div')
    host.innerHTML = '<div class="app has-inspector"><main class="work"></main><div class="drawer ctl-drawer ag-split"></div></div>'
    document.body.appendChild(host)
    try {
      expect(painted(host.querySelector('.ctl-drawer')!, 'height', WIDE) ?? '', 'the column has no height').toMatch(/calc\(100vh/)
    } finally {
      host.remove()
    }
  })

  it('is never wider than the column it docks under', async () => {
    const s = await split()
    const dock = s.querySelector(':scope > .ag-logdock')!
    expect(painted(dock, 'min-width', WIDE)).toBe('0')
    expect(painted(dock, 'max-width', WIDE)).toBe('100%')
  })
})

describe('Q4: the detail header and tabs fit the split', () => {
  it('ellipses the title and keeps the actions whole', async () => {
    const s = await split()
    const title = s.querySelector('.ag-head-title')!
    expect(painted(title, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(title, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(title, 'overflow', WIDE)).toBe('hidden')
    expect(painted(s.querySelector('.ag-head-row')!, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(s.querySelector('.ag-head-actions')!, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)\b/)
    expect(painted(s.querySelector('.ag-head-sub')!, 'text-overflow', WIDE)).toBe('ellipsis')
  })

  it('scrolls the tabs sideways rather than cutting or wrapping them', async () => {
    const s = await split()
    const tabs = s.querySelector('.c-tabs[role="tablist"]')!
    expect(painted(tabs, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(tabs, ['overflow-x', 'overflow'], WIDE)).toBe('auto')
    expect(painted(tabs.querySelector('button')!, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)\b/)
  })
})
