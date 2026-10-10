// THE SPACING GATE'S PARTITION. Every route walked by exactly one slice.
//
// The sweep itself -- every route, two themes, measured rather than eyeballed
// -- lives in `spacingsweep.tsx` and runs as one `spacing.<group>.test.tsx`
// per slice of the routes, so the UI job stops waiting on one ~450 s file
// (owner decision 2026-10-09; the measurements are in that file's header).
//
// WHY THIS FILE EXISTS. Splitting a sweep is how a sweep quietly stops
// covering something. This sweep has done that once already: on 2026-09-24 a
// hand-written route list went stale and five hundred shapes went unmeasured
// with every assertion green (ROUTES' comment in `spacingsweep.tsx`). The
// slices are hand-written lists again, deliberately, so they need the guard
// the derived list made unnecessary: every route the rail can reach is in
// exactly one slice, every slice has a file that runs it, and the slices'
// floors still add up to the 800 shapes the single sweep required.

import { describe, expect, it } from 'vitest'

import { SECTIONS } from '../App'
import { ROUTES, SPACING_FLOOR_TOTAL, SPACING_GROUPS } from './spacingsweep'

/** Each slice's file, found the way vitest finds it, so a group with no file fails. */
const SLICE_FILES = Object.keys(import.meta.glob('./spacing.*.test.tsx'))

describe('spacing: the slices', () => {
  it('walks the route list the rail derives, not a copy of it', () => {
    expect(ROUTES).toEqual(SECTIONS.flatMap((s) => s.tabs.map((t) => `${s.id}/${t.id}`)))
    expect(ROUTES.length, 'the rail reached no routes').toBeGreaterThan(0)
  })

  it('puts every route in exactly one slice', () => {
    const owners = new Map<string, string[]>()
    for (const g of SPACING_GROUPS)
      for (const r of g.routes) owners.set(r, [...(owners.get(r) ?? []), g.name])

    // Named per route, so the failure says which tab needs a slice (or which
    // slice still lists a tab that was renamed away).
    expect(ROUTES.filter((r) => owners.get(r) === undefined), 'routes no slice walks').toEqual([])
    expect(
      [...owners].filter(([, gs]) => gs.length > 1).map(([r, gs]) => `${r} in ${gs.join(', ')}`),
      'routes more than one slice walks',
    ).toEqual([])
    expect(
      [...owners.keys()].filter((r) => !ROUTES.includes(r)),
      'slice routes the rail cannot reach',
    ).toEqual([])
  })

  it('runs every slice from its own file, and nothing else as a slice', () => {
    expect(new Set(SPACING_GROUPS.map((g) => g.name)).size, 'two slices share a name').toBe(SPACING_GROUPS.length)
    expect([...SLICE_FILES].sort()).toEqual(SPACING_GROUPS.map((g) => `./spacing.${g.name}.test.tsx`).sort())
  })

  it('keeps the single sweep’s floor: the slices’ floors add up to at least 800 shapes', () => {
    // Each slice asserts `elements > floor`, so every slice passing means the
    // whole sweep examined more than the sum -- the old `> 800`, unchanged.
    expect(SPACING_FLOOR_TOTAL).toBe(800)
    for (const g of SPACING_GROUPS) expect(g.floor, `${g.name} has no floor`).toBeGreaterThan(0)
    expect(SPACING_GROUPS.reduce((n, g) => n + g.floor, 0)).toBeGreaterThanOrEqual(SPACING_FLOOR_TOTAL)
  })
})
