// A PROPORTION FILL TAKES A HUE ONLY FROM A VERDICT (#74, the #18 rule past
// `.ctl-util-fill`).
//
// design-system.md §6.4: a bar that is fine is grey, and a hue on a bar is a
// verdict. #18 held that for `.ctl-util-fill`; #74 named the two fills it
// stopped short of:
//
// * `.sr-bar > i`, the per-state split on Admin > Platform counts. The
//   selector-level probe in encoding.hues.test.ts reads it off a built
//   element; this file renders the SCREEN and reads every fill it actually
//   draws, so a class added to the `<i>` in PlatformCounts.tsx (an `info`, an
//   `is-running`) that re-hues it fails here even though the bare selector is
//   still grey. The one verdict this bar has is `.bad`, and it keeps `--bad`.
// * `.coverage > i`, the Timeline's usage-coverage strip. It left the product
//   with its only caller (TS-12), so no rule may bring it back blue: whatever
//   a shipped sheet paints it is grey or nothing.
//
// MUTATION: `background: var(--info)` back on `.sr-bar > i` (or on
// `.coverage > i`) in any shipped sheet, or a hued class on the `<i>` in
// PlatformCounts.tsx's `split-row`, and this goes red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Me, Stats } from '../types'
import { build, painted, resolveColour, sameColour, stateHueIn, THEMES, type Theme } from './marks'

const loadStats = vi.hoisted(() => vi.fn<() => Promise<Result<Stats>>>())
const loadMe = vi.hoisted(() => vi.fn<() => Promise<Result<Me>>>())
vi.mock('../api', () => ({ loadStats, loadMe }))

const COUNTS: Record<string, number> = {
  READY: 2, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 1,
  RUNNING: 3, SUCCEEDED: 40, FAILED: 5, CANCELLED: 2,
}

const PROPS = ['background', 'background-color'] as const

function grey(value: string | null, theme: Theme, what: string) {
  expect(value, `${what} declares no fill`).not.toBeNull()
  expect(stateHueIn(value, theme), `${what} is painted ${value}, a hue with no verdict`).toBeNull()
  expect(
    sameColour(resolveColour(value!, theme), resolveColour('var(--text-dim)', theme)),
    `${what} is ${value}, not --text-dim`,
  ).toBe(true)
}

const hosts: HTMLElement[] = []
beforeEach(() => {
  loadStats.mockReset()
  loadMe.mockReset()
  loadMe.mockResolvedValue({ status: 'error', fetchedAt: Date.now(), error: { kind: 'network', message: 'x' } } as unknown as Result<Me>)
  loadStats.mockResolvedValue({
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'eng',
      tasks_by_state: { ...COUNTS },
      platform_tasks_by_state: { ...COUNTS },
      dispatch_paused: false,
      limits: {},
      generated_at: new Date().toISOString(),
    } as Stats,
  })
})
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

describe('Platform counts draws every state split in grey, with no verdict hue (#74)', () => {
  for (const theme of THEMES) {
    it(`paints each rendered .sr-bar > i --text-dim in the ${theme} theme`, async () => {
      vi.resetModules()
      const { PlatformCountsScreen } = await import('../PlatformCounts')
      render(<PlatformCountsScreen />)
      fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
      await waitFor(() => expect(document.querySelectorAll('.split-row').length).toBeGreaterThan(0))

      const fills = [...document.querySelectorAll<HTMLElement>('.split-row .sr-bar > i')]
      // Every state that came back drew a fill: an empty list is not a pass.
      expect(fills.length, 'no .sr-bar fill was rendered').toBeGreaterThanOrEqual(Object.keys(COUNTS).length)
      for (const fill of fills) {
        const state = fill.closest('.split-row')?.querySelector('.sr-name')?.textContent ?? '?'
        // No state the screen draws carries the `.bad` verdict today, so every
        // fill is the plain one.
        expect(fill.classList.contains('bad'), `${state} drew a verdict`).toBe(false)
        grey(painted(fill, PROPS, { width: 1440, theme }), theme, `the ${state} fill`)
      }
    })
  }
})

describe('the verdict and the retired strip (#74)', () => {
  for (const theme of THEMES) {
    const env = { width: 1440, theme }

    it(`keeps --bad, the one verdict, on .sr-bar > i.bad in the ${theme} theme`, () => {
      const value = painted(build('.sr-bar > i.bad', hosts), PROPS, env)
      expect(stateHueIn(value, theme), `.sr-bar > i.bad is ${value}`).toMatch(/--bad/)
    })

    it(`lets no shipped sheet paint .coverage > i a hue in the ${theme} theme`, () => {
      const value = painted(build('.coverage > i', hosts), PROPS, env)
      // Retired with its caller (TS-12): nothing, or grey if it ever returns.
      if (value !== null) grey(value, theme, '.coverage > i')
    })
  }
})
