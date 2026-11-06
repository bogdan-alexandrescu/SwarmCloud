/**
 * LANE QA2-FIX-26 (part of #2694, the console visual QA pass 2 epic, and
 * #2391). The pass-2 findings whose main file is paths.ts or primitives.tsx,
 * each pinned here against the fixture build of main.
 *
 *   runs2-14       (was runs-submit-32) `/work/runs`, typed, was "Not found":
 *                  `/work` opens Work, and its tabs by name opened nothing.
 *   automate2-17   (was automate-19) every Absent box led with its mark's
 *                  words before the heading -- `none Never fired`, `not read
 *                  Not found` -- a chip restating the heading beside it.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { Absent, Mark } from '../primitives'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('runs2-14: Work’s tabs, typed under /work, land on the tab', () => {
  it('reads /work/<tab name> as the tab, and writes it back as the tab’s own path', () => {
    for (const [typed, path] of [
      ['/work/runs', '/runs'],
      ['/work/agents', '/agents'],
      ['/work/workflows', '/workflows'],
      ['/work/timeline', '/timeline'],
    ] as const) {
      const p = pathToAddress(typed)
      // MUTATION: drop WORK_TAB_PATHS from pathToAddress's last lookup and each is null -- "Not found".
      expect(p, `${typed} is not a page`).not.toBeNull()
      expect(addressToPath(p!.address)).toBe(path)
    }
    // The query rides along, as it does on a section root.
    expect(pathToAddress('/work/runs/', '?state=failed')?.address).toBe('work/runs?state=failed')
    // The internal address is not a tab's name and stays a missing page.
    expect(pathToAddress('/work/running')).toBeNull()
  })

  it('rewrites the bar to /runs and draws Runs, not "No page at"', async () => {
    window.history.replaceState(null, '', '/work/runs')
    const { container } = render(<App />)
    // MUTATION: drop the '/work/runs' row and the page says "No page at /work/runs".
    await waitFor(() => expect(window.location.pathname).toBe('/runs'))
    expect(container.textContent).not.toContain('No page at')
  })
})

describe('automate2-17: an empty state’s heading is the heading alone', () => {
  const cases = [
    ['zero', 'Never fired', 'is-zero'],
    ['failed', 'Not found', 'is-unread'],
    ['partial', 'Some of it', 'is-partial'],
    ['admin', 'Admins only', 'is-admin'],
  ] as const

  it('draws no mark words before the heading, and keeps the sentence on the mark', () => {
    for (const [kind, heading, markCls] of cases) {
      const { container } = render(<Absent kind={kind} heading={heading} say={`The ${kind} sentence.`} />)
      const h3 = container.querySelector('h3')!
      // MUTATION: render the Mark's word in Absent and this reads `none Never fired`.
      expect(h3.textContent?.trim()).toBe(heading)
      const mark = h3.querySelector(`.ctl-mark.${markCls}`)
      expect(mark, `${kind}: the silhouette went with the words`).not.toBeNull()
      expect(mark!.getAttribute('role')).toBe('img')
      expect(mark!.getAttribute('aria-label')).toBe(`The ${kind} sentence.`)
      expect(mark!.classList.contains('is-icon')).toBe(true)
    }
  })

  it('sizes the icon as a square one line of 12px type tall, with no word padding', () => {
    const { container } = render(<Absent kind="failed" heading="Not found" say="The read failed." />)
    const mark = container.querySelector('h3 > .ctl-mark')!
    // MUTATION: drop styles/primitives.css and the empty mark is the word mark's 6px-padded sliver.
    expect(painted(mark, 'width', WIDE)).toBe('12px')
    expect(painted(mark, 'height', WIDE)).toBe('12px')
    expect(painted(mark, 'padding', WIDE)).toBe('0')
  })

  it('leaves a mark beside a figure worded', () => {
    const { container } = render(<Mark kind="unread" say="The read failed." />)
    expect(container.textContent).toBe('not read')
    expect(container.querySelector('.is-icon')).toBeNull()
  })
})
