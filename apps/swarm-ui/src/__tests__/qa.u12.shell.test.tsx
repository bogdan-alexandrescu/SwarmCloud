/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04): the phone drawer and the section
 * roots.
 *
 *   R5   On a 390px phone `nav.sk-panel` was 1394px wide (`flex: 1` with no
 *        `min-width: 0`, and a Recent name 1298px long), so the Recent names,
 *        the Global pool's number and the Dark/System buttons were off-screen.
 *   R14  `/capacity` and `/admin` said "No page at /capacity".
 *
 * MUTATIONS: drop the panel's `min-width: 0`, or the Recent row's; drop a
 * section root -- each turns a case red.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { RECENT_WORKFLOWS_KEY } from '../Spine'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const PHONE: CascadeEnv = { width: 390 }

afterEach(() => {
  localStorage.clear()
  window.history.replaceState(null, '', '/')
})

async function at(path: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  const { container } = render(<App />)
  await waitFor(() => expect(container.querySelector('.sk-spine .sk-av')?.getAttribute('title')).not.toBe('not read'), { timeout: 5000 })
  return container
}

describe('R5: the phone drawer is the drawer\'s width', () => {
  it('lets the panel shrink to the drawer and cuts a long Recent name with an ellipsis', async () => {
    const long = `UI U7 ${'a very long workflow lane label '.repeat(8)}`
    localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_8fa28bbc798e4b8a8e7e', state: 'RUNNING', name: long }]))
    const c = await at('/workflows')
    const menu = c.querySelector<HTMLElement>('.sk-pbar button[aria-label]')!
    fireEvent.click(menu)
    const app = await waitFor(() => {
      const a = c.querySelector('.sk-app.has-drawer')
      expect(a).not.toBeNull()
      return a!
    })
    const panel = app.querySelector('nav.sk-panel')!
    // A flex item's floor is its content's min-content unless it is zero.
    expect(painted(panel, 'min-width', PHONE), 'the panel is as wide as its longest name').toBe('0')
    expect(painted(panel, 'flex', PHONE)).toMatch(/^1 1 0%?$/)
    const row = await waitFor(() => {
      const b = panel.querySelector<HTMLElement>('.sk-recent > button.sk-kid[title]')
      expect(b).not.toBeNull()
      return b!
    })
    expect(painted(row, 'min-width', PHONE)).toBe('0')
    expect(painted(row, 'max-width', PHONE)).toBe('100%')
    const name = row.querySelector(':scope > span > span:last-child')!
    expect(name.textContent).toBe(long)
    expect(painted(name, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(name, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(painted(name, ['overflow-x', 'overflow'], PHONE)).toBe('hidden')
    expect(row.getAttribute('title')).toContain(long)
  })
})

describe('R14: a section\'s root opens its first page', () => {
  it('reads /capacity, /admin and /work as their first pages, one way', () => {
    expect(pathToAddress('/capacity')?.address).toBe('capacity/pools')
    expect(pathToAddress('/capacity/')?.address).toBe('capacity/pools')
    expect(pathToAddress('/admin')?.address).toBe('admin/limits')
    expect(pathToAddress('/work')?.address).toBe('work/running')
    // The page keeps its own path: the root is a redirect, not a second name.
    expect(addressToPath('capacity/pools')).toBe('/capacity/pools')
    expect(addressToPath('admin/limits')).toBe('/admin/limits')
  })

  for (const [root, page] of [['/capacity', '/capacity/pools'], ['/admin', '/admin/limits']] as const) {
    it(`lands ${root} on ${page}, never "No page at"`, async () => {
      const c = await at(root)
      await waitFor(() => expect(window.location.pathname).toBe(page))
      expect(c.textContent).not.toContain('No page at')
    })
  }
})
