// "No total over a partial response", as behaviour.
//
// A sum is the most authoritative-looking thing on a screen, and the easiest
// to compute over data that is missing pieces. `/v1/stats` runs one Firestore
// count() per state; a response missing three of the nine is not a platform
// with fewer tasks, it is a read that did not finish -- and the difference is
// invisible once you have added the numbers up.
//
// Both directions are asserted. A screen that never totals anything satisfies
// "no total over a partial response" trivially, so the complete case must show
// the total or the rule is being kept by accident.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Me, Stats } from '../types'
import { NEVER_WRITTEN, REAL_STATES } from '../types'
import { expectNoFigures } from './setup'

const loadStats = vi.hoisted(() => vi.fn<() => Promise<Result<Stats>>>())
// THE SESSION READ, the one the header's admin badge is drawn from. Platform
// counts reads it too, so the cost it shows before the first run is the cost
// THIS caller's first run will have (AH-9).
const loadMe = vi.hoisted(() => vi.fn<() => Promise<Result<Me>>>())
vi.mock('../api', () => ({ loadStats, loadMe }))

const { PlatformCountsScreen } = await import('../PlatformCounts')

function session(isAdmin: boolean): Result<Me> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant: { tenant_id: 'eng' },
      principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: isAdmin },
      environment: 'dev',
      environment_declared: false,
    } as unknown as Me,
  }
}

// Every test below that does not care who is asking is asked by a non-admin,
// which is the cost the screen drew for everyone before AH-9.
beforeEach(() => {
  loadMe.mockResolvedValue(session(false))
})

const COMPLETE: Record<string, number> = {
  READY: 2, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 1,
  RUNNING: 3, SUCCEEDED: 40, FAILED: 5, CANCELLED: 2,
}

function stats(over: Partial<Stats> = {}): Stats {
  return {
    tenant_id: 'eng',
    tasks_by_state: { ...COMPLETE },
    dispatch_paused: false,
    limits: {},
    generated_at: '2026-09-22T10:00:00Z',
    ...over,
  }
}

async function run(result: Result<Stats>) {
  loadStats.mockResolvedValue(result)
  render(<PlatformCountsScreen />)
  screen.getByRole('button', { name: 'Run the count' }).click()
  await waitFor(() => expect(loadStats).toHaveBeenCalled())
  return result
}

/** The tenant block. There are two blocks when the caller is an admin. */
function tenantPanel(): HTMLElement {
  const heading = screen.getByRole('heading', { name: /This tenant/ })
  const section = heading.closest('section')
  if (!section) throw new Error('no tenant section')
  return section as HTMLElement
}

/**
 * The total's slot, which is now ALWAYS drawn.
 *
 * WHAT MOVED. The total used to be a `.provenance` line -- "54 task documents
 * across the 9 states that are written" -- rendered only when there was a
 * total, and the partial case printed a `.warn-text` sentence instead. Both are
 * gone. The card now carries one `.ctl-figure` whose slot is present either
 * way: a measured total is the digits, and a withheld one is `.ctl-em` under
 * `.ctl-figure.is-absent` with the `partial` mark beside it.
 *
 * WHY THAT IS A STRONGER CLAIM THAN THE SENTENCE WAS. A card that simply
 * omitted its total when it could not compute one is indistinguishable from a
 * card that never had a total; the reader has to notice an absence. Drawing the
 * hole means there is nothing to notice. The sentence itself did not disappear
 * -- it is the figure's `aria-label`, and the argument is at #help/withheld-total.
 */
function totalFigure(panel: HTMLElement): HTMLElement {
  const fig = panel.querySelector('.counts-total .ctl-figure')
  if (!fig) throw new Error('the total slot is not drawn at all')
  return fig as HTMLElement
}

describe('a complete response', () => {
  it('shows the total, so the rule below is not being kept by accident', async () => {
    await run({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    const sum = Object.values(COMPLETE).reduce((a, b) => a + b, 0)

    const fig = totalFigure(panel)
    expect(fig.textContent).toContain(String(sum))
    expect(fig.className).not.toContain('is-absent')
    expect(fig.querySelector('.ctl-em')).toBeNull()
    // And nothing claims the response was partial.
    expect(panel.querySelector('.ctl-mark.is-partial')).toBeNull()
  })

  it('renders a measured zero as 0 and not as an em dash', async () => {
    await run({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    const leased = Array.from(panel.querySelectorAll('.split-row')).find((r) => r.textContent?.startsWith('LEASED'))
    expect(leased?.querySelector('.sr-n')?.textContent).toBe('0')
  })
})

describe('a partial response', () => {
  /**
   * WHAT MOVED, AND WHERE THE WORDS WENT.
   *
   * The sentence "2 states did not come back (PARKED, RUNNING). No total is
   * shown." is gone from the surface. Three things carry it now, and each is
   * visible with every help card shut:
   *
   *   - `.ctl-mark.is-partial` -- the word `partial`, dashed on one side only,
   *     which is the shape of the hole. Greyscale-safe, so it survives the
   *     screenshot pasted into an incident channel, which the amber
   *     `.warn-text` did not.
   *   - `.ctl-card-note` -- `7 of 9 states`, the coverage as a figure in the
   *     card's qualifier slot.
   *   - the histogram itself -- PARKED and RUNNING each keep their row and
   *     each show `.ctl-em`, so "which states" is read off the same rows the
   *     digits are read off rather than out of a parenthesis.
   *
   * The full sentence is the total figure's `aria-label`, and the argument is
   * at #help/withheld-total. NOTHING WAS WEAKENED: the rule that the sum must
   * not be printed is asserted below exactly as before.
   */
  it('withholds the total and names every state that did not come back', async () => {
    const partial = { ...COMPLETE }
    delete partial.RUNNING
    delete partial.PARKED
    await run({ status: 'ok', data: stats({ tasks_by_state: partial }), fetchedAt: Date.now() })

    const panel = await waitFor(tenantPanel)

    // The partial-ness is on the surface, as a mark rather than a sentence.
    expect(panel.querySelector('.ctl-mark.is-partial')?.textContent).toBe('partial')
    expect(panel.querySelector('.ctl-card-note')?.textContent).toBe(
      `${REAL_STATES.length - 2} of ${REAL_STATES.length} states`,
    )

    // WHICH states, from the rows -- every one of them, still named.
    for (const state of ['PARKED', 'RUNNING']) {
      const row = Array.from(panel.querySelectorAll('.split-row')).find((r) =>
        r.textContent?.startsWith(state),
      )
      expect(row, `${state} lost its row`).toBeTruthy()
      expect(row!.querySelector('.sr-n .ctl-em'), `${state} is not marked absent`).not.toBeNull()
    }

    // THE RULE. Not "it marks" -- it must also NOT print the sum. The slot is
    // drawn, and what is in it is the em dash and no digit at all.
    const fig = totalFigure(panel)
    expect(fig.className).toContain('is-absent')
    expect(fig.querySelector('.ctl-em')).not.toBeNull()
    expect(fig.textContent).not.toMatch(/\d/)
    expect(panel.textContent).not.toContain('task documents across')

    // The words did not evaporate; they are the figure's accessible name.
    const label = fig.getAttribute('aria-label') ?? ''
    expect(label).toContain('No total is shown')
    expect(label).toContain('PARKED')
    expect(label).toContain('RUNNING')
  })

  it('renders the missing states as em dashes rather than as zeros', async () => {
    const partial = { ...COMPLETE }
    delete partial.RUNNING
    await run({ status: 'ok', data: stats({ tasks_by_state: partial }), fetchedAt: Date.now() })

    const panel = await waitFor(tenantPanel)
    const running = Array.from(panel.querySelectorAll('.split-row')).find((r) => r.textContent?.startsWith('RUNNING'))
    // "0 RUNNING" and "the RUNNING count did not arrive" are opposite facts.
    expect(running?.querySelector('.sr-n')?.textContent).toBe('—')
  })

  /**
   * The degenerate partial: nothing arrived at all.
   *
   * WHAT MOVED. The `.warn-text` sentence is the `.ctl-mark.is-partial` and the
   * `0 of 9 states` note, exactly as in the test above -- this case is not
   * special-cased into different words, which was half the point of making the
   * encoding uniform. The rule it guards is unchanged: a response carrying no
   * counts is a failed query, so no figure on the card may be a number.
   */
  it('a response with NO counts at all is called a failed query, not an idle platform', async () => {
    await run({ status: 'ok', data: stats({ tasks_by_state: {} }), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)

    expect(panel.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(panel.querySelector('.ctl-card-note')?.textContent).toBe(
      `0 of ${REAL_STATES.length} states`,
    )

    const fig = totalFigure(panel)
    expect(fig.className).toContain('is-absent')
    expect(fig.textContent).not.toMatch(/\d/)

    for (const state of REAL_STATES) {
      const row = Array.from(panel.querySelectorAll('.split-row')).find((r) => r.textContent?.startsWith(state))
      expect(row?.querySelector('.sr-n')?.textContent).toBe('—')
      expect(row?.querySelector('.sr-n .ctl-em')).not.toBeNull()
    }
  })
})

describe('scope', () => {
  it('keeps the tenant figures and the platform figures in separate blocks', async () => {
    await run({
      status: 'ok',
      data: stats({ platform_tasks_by_state: { ...COMPLETE, RUNNING: 99 } }),
      fetchedAt: Date.now(),
    })
    await waitFor(tenantPanel)
    // An admin reading their own three running tasks as the platform total is
    // a truth bug, not a layout preference.
    expect(screen.getByRole('heading', { name: /Every tenant/ })).toBeTruthy()
    expect(tenantPanel().textContent).not.toContain('99')
  })

  /**
   * WHAT MOVED. "Admin only — these figures are absent from this response
   * rather than zero." was the only thing on this panel, and it was the only
   * panel with no figure of its own to carry a marker. It has one now: the
   * card keeps its title and its figure SLOT, and the slot holds `.ctl-em`
   * under `.ctl-figure.is-absent` with `.ctl-mark.is-admin` beside it.
   *
   * The sentence is the figure's `aria-label` and #help/admin-gate-not-failure.
   *
   * THE THIRD ASSERTION IS NEW AND IS THE POINT OF THE `is-admin` VARIANT.
   * Not entitled is not broken: a non-admin genuinely cannot read
   * /v1/admin/*, so nothing here may take the failure treatment. Painting it
   * red tells someone their platform is down when it is not, on every visit.
   */
  it('an ABSENT platform block is stated as absent, never as zero', async () => {
    await run({ status: 'ok', data: stats({ platform_tasks_by_state: undefined }), fetchedAt: Date.now() })
    await waitFor(tenantPanel)
    const everyone = screen.getByRole('heading', { name: /Every tenant/ }).closest('section')!

    // Absent, and drawn as such, with no hovering and no open card.
    const fig = everyone.querySelector('.counts-total .ctl-figure')!
    expect(fig.className).toContain('is-absent')
    expect(fig.querySelector('.ctl-em')).not.toBeNull()
    expect(fig.textContent).not.toMatch(/\d/)
    expect(everyone.querySelector('.ctl-mark.is-admin')?.textContent).toBe('admin only')
    expect(fig.getAttribute('aria-label')).toContain(
      'absent from this response rather than zero',
    )

    // Not entitled is not broken.
    expect(everyone.querySelector('.ctl-mark.is-unread')).toBeNull()
    expect(everyone.className).not.toContain('is-failed')

    expect(everyone.querySelectorAll('.split-row').length).toBe(0)
  })
})

describe('a failed read of the counts', () => {
  it('prints no number anywhere, because a zero here is the most reassuring lie available', async () => {
    await run({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'Firestore did not answer.' },
    })
    await screen.findByText('Firestore did not answer.', { exact: false })

    expect(screen.queryByRole('heading', { name: /This tenant/ })).toBeNull()
    expect(document.querySelectorAll('.split-row').length).toBe(0)

    // WHAT MOVED. "No counts are shown, because none arrived. This says nothing
    // about how much work the platform is carrying." was two sentences in the
    // middle of the failure panel. The panel is `.ctl-empty.is-failed` now and
    // the visible, greyscale-safe anchor is `.ctl-mark.is-unread` -- the word
    // `not read`, dashed, which is this sheet's mark for "the read failed, and
    // the platform may well know the answer". The sentences ride on it as its
    // accessible name, so there is a keyboard and a screen-reader route to them
    // -- which is precisely what the `title=` attempt that turned this suite
    // red the last time did not have.
    const panel = document.querySelector('.ctl-empty.is-failed')!
    expect(panel, 'the failure is not drawn as a failed empty state').not.toBeNull()
    const mark = panel.querySelector('.ctl-mark.is-unread')!
    expect(mark.textContent).toBe('not read')
    expect(mark.getAttribute('aria-label')).toContain(
      'This says nothing about how much work the platform is carrying.',
    )

    // The copy block explaining the aggregation cost names counts of STATES,
    // which are a property of the contract rather than a measurement of the
    // platform, so they are allowed through by name.
    expectNoFigures(document.body, ['1000', 'twelve', 'twenty-four'].concat(
      [String(NEVER_WRITTEN.size), String(REAL_STATES.length + NEVER_WRITTEN.size)],
    ))
  })

  it('does not count a failure as a successful aggregation run', async () => {
    // Incrementing on every settled promise would let a failure inflate a
    // number the copy then calls "aggregations billed".
    await run({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } })
    await screen.findByText('boom', { exact: false })
    expect(document.body.textContent).not.toContain('successful run')
  })
})

describe('the states that can never be written', () => {
  it('are excluded from the histogram and explained rather than drawn as zeros', async () => {
    await run({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const panel = await waitFor(tenantPanel)
    const rows = Array.from(panel.querySelectorAll('.split-row')).map((r) => r.textContent ?? '')
    for (const state of NEVER_WRITTEN) {
      // A bucket that can only ever read zero teaches "nothing is wrong"
      // rather than "this cannot happen".
      expect(rows.some((r) => r.startsWith(state))).toBe(false)
    }
    expect(rows).toHaveLength(REAL_STATES.length)

    // WHAT MOVED. "3 of the 12 states in the contract are never written to a
    // task document and are not listed above: SUBMITTED, QUEUED,
    // DEAD_LETTERED." was a 24-word footnote restating an arithmetic the
    // reader could not check. What stays is the part that cannot be inferred
    // from the histogram -- WHICH states are missing from it and why their
    // absence is deliberate -- as a named list in the card's provenance strip.
    // The sentence is at #help/states. The list is still DERIVED from
    // `NEVER_WRITTEN`, so it cannot drift if a state is added.
    const foot = panel.querySelector('.ctl-card-foot')!
    expect(foot, 'the excluded states are named nowhere').not.toBeNull()
    for (const state of NEVER_WRITTEN) {
      expect(foot.textContent, `${state} is excluded and unnamed`).toContain(state)
    }
    // And the `?` that holds the reason is present and shut.
    expect(foot.querySelector('button[aria-label^="Help: "]')).not.toBeNull()
  })
})

/**
 * AH-9 (#86). `per run · 12 count()` was shown to an admin whose first press
 * costs 24, because the screen learned who was asking only from the RESULT of
 * a run: `admin` stayed null, and null priced the run as a tenant's. The
 * figure that exists to say what the button costs was wrong on exactly the
 * press it was there for.
 *
 * The expected figures are DERIVED from the contract sets, as the screen's
 * are, so a new state moves both sides together.
 */
describe('the cost shown before the first run', () => {
  const PER_SCOPE = REAL_STATES.length + NEVER_WRITTEN.size

  function perRun(): string {
    const fact = [...document.querySelectorAll('.ctl-toolbar .ctl-fact')].find((f) =>
      (f.textContent ?? '').startsWith('per run'),
    )
    if (!fact) throw new Error('no "per run" fact in the toolbar')
    return fact.textContent ?? ''
  }

  it('is an admin’s cost for an admin, known from the session before any run', async () => {
    loadMe.mockResolvedValue(session(true))
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(perRun()).toContain(`${PER_SCOPE * 2} count()`))
    expect(loadStats, 'the figure came from a run, not from the session').not.toHaveBeenCalled()
  })

  it('is a tenant’s cost for a non-admin', async () => {
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadMe).toHaveBeenCalled())
    await waitFor(() => expect(perRun()).toContain(`${PER_SCOPE} count()`))
    expect(perRun()).not.toContain(String(PER_SCOPE * 2))
  })

  it('does not price the run as a tenant’s when nobody could say who is asking', async () => {
    loadMe.mockResolvedValue({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: null, message: 'no session' },
    })
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(loadMe).toHaveBeenCalled())
    // Both answers are possible, so both are on screen -- a single figure here
    // would be a guess about the caller wearing the clothes of a price.
    await waitFor(() => expect(perRun()).toContain(String(PER_SCOPE * 2)))
    expect(perRun()).toContain(String(PER_SCOPE))
  })
})
