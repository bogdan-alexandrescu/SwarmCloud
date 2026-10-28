// CONSOLE VISUAL QA PASS 1B, LANE QA1B-12 (admin-shell-15; part of #2596).
//
// Under the page's one Run (`Run the count · ~562 reads`), Over time's note
// said `The price is ~538 reads`: one half of the price, read as the whole.
// Mounted under that Run, the note calls its figure this view's part; on its
// own, with its own Run, it is still the price.

import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { PlatformTime } from '../platformTimeApi'

const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformOverTime, forgetOverTime } = await import('../PlatformOverTime')
const { readEstimate, spanDays } = await import('../platformTimeApi')
const { viewerZone } = await import('../outcomes')

const note = () => document.querySelector<HTMLElement>('.pot-note')?.textContent ?? ''

beforeEach(() => {
  forgetOverTime()
  loadPlatformTime.mockReset()
  loadPlatformTime.mockReturnValue(new Promise(() => {}))
})

describe('admin-shell-15: under the page’s Run, Over time’s figure is its part of the price', () => {
  it('says `This view’s part is ~N reads` when the page’s Run reads it', () => {
    render(<PlatformOverTime workspaces={3} run={0} />)
    const over = readEstimate(spanDays('week', Date.now(), viewerZone()), 3).total!
    // MUTATION: keep `The price is ~N reads` -- ~538 under a button that says ~562.
    expect(note()).toMatch(new RegExp(`^The page’s Run reads this too, with the snapshot below\\. This view’s part is ~${over} reads for `))
    expect(note()).not.toMatch(/The price is/)
  })

  it('says it per workspace when the roster is not known', () => {
    render(<PlatformOverTime run={0} />)
    expect(note()).toMatch(/^The page’s Run reads this too, with the snapshot below\. This view’s part is ~\d+ reads for each workspace/)
  })

  it('still calls it the price when the view has its own Run', () => {
    render(<PlatformOverTime workspaces={3} />)
    expect(note()).toMatch(/^The price is ~\d+ reads for /)
    expect(note()).not.toMatch(/page’s Run|view’s part/)
  })
})
