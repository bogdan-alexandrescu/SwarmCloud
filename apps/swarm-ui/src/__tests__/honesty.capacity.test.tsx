// The em-dash rule and the partial-read rule, on the screen where they matter.
//
// Capacity is where an operator goes when nothing is being admitted and they
// need to know which ceiling is binding. Three ways it can lie:
//
//   1. print 0 for a figure nobody measured, which reads as "the platform is
//      full" when the truth is "a pool could not be read";
//   2. name ONE pool when several are refusing, so the operator raises it and
//      nothing moves -- a bug this repository has already shipped;
//   3. present a shortened blocker list as a complete one, which tells someone
//      they have cleared everything when they have not.
//
// `loadCapacity` is replaced rather than `fetch`, because the states under
// test are Results and driving them through HTTP would test `read` again.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { RuntimeTopology } from '../api'
import type { Capacity, Counterfactual, Pool, ProfileAdmission, ProfileBlocker, Runtime, RunnerProfile } from '../types'
import { headroomFor } from '../types'
import { BlockerList, IncompleteNote, headroomFigure } from '../Blockers'
import { expectNoFigures } from './setup'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
const loadRuntimeTopology = vi.hoisted(() => vi.fn<() => Promise<Result<RuntimeTopology>>>())
vi.mock('../api', () => ({ loadCapacity, loadRuntimeTopology }))

const { CapacityScreen } = await import('../Capacity')
const { ProfilesScreen } = await import('../Profiles')
const { RuntimesScreen } = await import('../Runtimes')

// ---------------------------------------------------------------------------

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'global', hard_limit: 8, adaptive_target: null, quota_derived_limit: null,
    effective_limit: 8, active: 0, available: 8, enabled: true,
    updated_at: '2026-09-22T10:00:00Z', ...over,
  }
}

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 8, active: 8, group: 'no_room', ...over }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 3, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools: ['global'], ...over }
}

function capacity(over: Partial<Capacity>): Capacity {
  return {
    pools: [pool({})],
    runner_profiles: {},
    tenant_id: 'eng',
    generated_at: '2026-09-22T10:00:00Z',
    ...over,
  }
}

function cf(over: Partial<Counterfactual>): Counterfactual {
  return { pool: 'global', action: 'raise', headroom_after: 4, basis_after: 'measured', delta: 4, next_binding: [], ...over }
}

function renderCapacity(data: Capacity) {
  loadCapacity.mockResolvedValue({ status: 'ok', data, fetchedAt: Date.now(), serverAt: data.generated_at })
  return render(<CapacityScreen />)
}

/** The "Could start" cell for one runner profile, off the rendered table. */
function couldStart(name: string): HTMLElement {
  const row = screen.getByRole('rowheader', { name }).closest('tr')
  if (!row) throw new Error(`no row for ${name}`)
  const cell = row.querySelectorAll('td')[0]
  if (!cell) throw new Error(`no cells in the row for ${name}`)
  return cell as HTMLElement
}

// ---------------------------------------------------------------------------
// An unmeasured figure is an em dash. A measured zero is a 0.
// ---------------------------------------------------------------------------

describe('the headroom figure', () => {
  it('renders an em dash, not a 0, when the API sent no admission block', async () => {
    // THE DEMONSTRATION THIS LAYER EXISTS FOR. The strings "—" and "0" are
    // both present in the source of Capacity.tsx and Blockers.tsx, so no grep
    // over the source can tell these two cases apart. Only rendering can.
    renderCapacity(capacity({ runner_profiles: { 'claude-code': profile({ admission: undefined }) } }))

    const cell = await screen.findByRole('rowheader', { name: 'claude-code' })
    expect(cell).toBeTruthy()
    expect(couldStart('claude-code').textContent).toBe('—')
    expect(couldStart('claude-code').textContent).not.toBe('0')
    expect(couldStart('claude-code').closest('tr')?.className).toContain('unmeasured')
  })

  it('renders 0 when zero is what was measured', async () => {
    renderCapacity(
      capacity({
        runner_profiles: { 'claude-code': profile({ admission: admission({ headroom: 0, basis: 'measured', blockers: [blocker({})], binding: ['global'] }) }) },
      }),
    )
    await screen.findByRole('rowheader', { name: 'claude-code' })
    expect(couldStart('claude-code').textContent).toBe('0')
    expect(couldStart('claude-code').closest('tr')?.className).toContain('over')
  })

  it('says WHICH kind of not-measured it is, in the cell’s own title', async () => {
    // An em dash with no explanation is only marginally better than a zero.
    expect(headroomFigure(headroomFor(profile({ admission: admission({ headroom: null, basis: 'uncapped' }) }))).title)
      .toContain('nothing caps it')
    expect(headroomFigure(headroomFor(profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['global'] }) }))).title)
      .toContain('could not be read')
    expect(headroomFigure(headroomFor(profile({ admission: admission({ headroom: 4 }) }))).title)
      .toContain('4 more could have been admitted')
  })
})

// ---------------------------------------------------------------------------
// Every pool that refused, not the tightest one
// ---------------------------------------------------------------------------

describe('held back by', () => {
  it('names EVERY refusing pool, so raising one is not mistaken for the fix', async () => {
    renderCapacity(
      capacity({
        runner_profiles: {
          'claude-code': profile({
            pools: ['global', 'resource:large', 'provider:anthropic'],
            admission: admission({
              headroom: 0,
              blockers: [
                blocker({ pool: 'resource:large', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4 }),
                blocker({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 6, active: 6 }),
              ],
              binding: ['resource:large', 'provider:anthropic'],
            }),
          }),
        },
      }),
    )

    // B4.5 RE-POINT. The blocker list moved from `.tag` to `.ctl-chip`, the
    // design system's one status chip (§6.6) -- four different chip classes
    // were the audit's finding and `.tag` was one of them. NOTHING ABOUT THE
    // CLAIM CHANGED: this still asserts that EVERY refusing pool is named, by
    // counting the chips in the cell, because naming one of two sends an
    // operator to raise a ceiling and watch nothing move. The words are
    // unchanged and still on the surface; only the class they are drawn with
    // moved.
    const row = (await screen.findByRole('rowheader', { name: 'claude-code' })).closest('tr')
    const chips = row?.querySelectorAll('.ctl-chip') ?? []
    const labels = Array.from(chips).map((t) => t.textContent ?? '')
    expect(labels).toHaveLength(2)
    expect(labels.join(' ')).toContain('large')
    expect(labels.join(' ')).toContain('anthropic')
  })

  it('draws a paused pool differently from a full one, because the remedies are opposite', async () => {
    renderCapacity(
      capacity({
        runner_profiles: {
          'claude-code': profile({
            admission: admission({
              headroom: 0,
              // A paused pool can read 0 of 8 units in use and admit nothing:
              // the case that looks healthiest and is not. It is told apart by
              // the REASON the server sent, never by the count.
              blockers: [blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', limit: 8, active: 0 })],
            }),
          }),
        },
      }),
    )
    // B4.5 RE-POINT. `.tag.paused` became `.ctl-chip.is-paused`, and the chip
    // carries a SHAPE as well as a hue -- §6.6 gives `is-paused` the two-bar
    // pause glyph, so the two remedies stay apart in greyscale and for a
    // colour-blind reader, which the old tag's colour alone did not. The
    // assertion is the same one and is now slightly stronger: the word, the
    // modifier, and the absence of the opposite modifier.
    const row = (await screen.findByRole('rowheader', { name: 'claude-code' })).closest('tr')
    const chip = row?.querySelector('.ctl-chip')
    expect(chip?.className).toContain('is-paused')
    expect(chip?.className).not.toContain('is-bad')
    expect(chip?.textContent).toContain('paused')
  })

  /**
   * THE SAME POOL THE AGENTS LIST CALLED "busy" (live, 2026-09-24). A
   * `resource:` pool an operator set to `hard_limit 0` refuses with the same
   * RESOURCE_CLASS_LIMIT a full one does, and the chip drew it as `0/0` in the
   * full pool's red -- a fraction that reads as "all of nothing is in use".
   * MUTATION: draw every non-MANUAL_PAUSE blocker as `active/limit`.
   */
  it('draws a pool an operator capped at zero as limit 0, with the paused tone, never 0/0', async () => {
    renderCapacity(
      capacity({
        runner_profiles: {
          browser: profile({
            resource_class: 'browser',
            pools: ['resource:browser'],
            admission: admission({
              headroom: 0,
              blockers: [blocker({ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 0, active: 0 })],
            }),
          }),
        },
      }),
    )
    const row = (await screen.findByRole('rowheader', { name: 'browser' })).closest('tr')
    const chip = row?.querySelector('.ctl-chip')
    expect(chip?.textContent).toContain('limit 0')
    expect(chip?.textContent).not.toContain('0/0')
    expect(chip?.className).toContain('is-paused')
    expect(chip?.className).not.toContain('is-bad')
  })
})

// ---------------------------------------------------------------------------
// A partial read says what it could not see
// ---------------------------------------------------------------------------

describe('an incomplete read', () => {
  it('says the list is incomplete and names the pools nobody read', () => {
    const h = headroomFor(profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['provider:anthropic', 'resource:large'], blockers: [blocker({})] }) }))
    render(<IncompleteNote h={h} />)

    const note = screen.getByRole('status')
    expect(note.textContent).toContain('incomplete')
    expect(note.textContent).toContain('anthropic')
    expect(note.textContent).toContain('large')
    expect(note.textContent).toContain('withheld rather than guessed')
  })

  it('says nothing at all when the read WAS complete', () => {
    const { container } = render(<IncompleteNote h={headroomFor(profile({ admission: admission({}) }))} />)
    expect(container.textContent).toBe('')
  })

  /**
   * CP-6 (#85) RE-POINT, BOTH, after #159's review. These rendered
   * `<BlockerList>` on its own with nothing refusing and read the sentence it
   * printed: "No pool is refusing this profile." for a complete read, "No
   * refusal was measured -- but see above" for an incomplete one. The owner's
   * decision leaves each Profile headroom card ONE fact sentence, and the
   * grouped list is back on the card under it -- so the list draws nothing
   * when there is nothing to list, and the card's sentence says what is true.
   *
   * WHAT DID NOT MOVE: an empty list under an incomplete read is still never
   * reported as a clean bill of health. That is now asked of the card, which
   * is the thing a reader sees.
   */
  it('an empty blocker list under an incomplete read is not reported as "nothing is refusing"', async () => {
    // The dangerous case: measured nothing, and saying so as though it were a
    // clean bill of health.
    renderProfiles(
      capacity({
        pools: [pool({ name: 'global' })],
        runner_profiles: {
          'claude-code': profile({
            pools: ['global', 'tenant:eng'],
            admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['tenant:eng'], blockers: [] }),
          }),
        },
      }),
    )
    const card = await profileCard('claude-code')
    expect(card.textContent).toContain('could not be read')
    expect(card.textContent).not.toContain('No pool is refusing')
    expect(card.textContent).not.toMatch(/nothing is refusing/i)
  })

  it('draws no list and no banner when nothing refuses, complete or not (CP-6)', () => {
    for (const complete of [true, false]) {
      const h = headroomFor(
        profile({ admission: admission({ complete, unread: complete ? [] : ['global'], blockers: [] }) }),
      )
      const { container, unmount } = render(
        <>
          <IncompleteNote h={h} />
          <BlockerList h={h} groups={undefined} />
        </>,
      )
      expect(container.textContent, `complete=${complete}: the empty list still says something`).toBe('')
      unmount()
    }
  })

  /**
   * CP-6 (#85) RE-POINT. This rendered `<Counterfactuals>`, the list under a
   * Profile headroom card, and asserted it said "Not computed" with no figure
   * anywhere. The list is gone: what relaxing a ceiling would buy is now the
   * `+N if lifted` column, on the rows of the pools that cap the card. The
   * claim is unchanged and pinned where the figure would now be: a refusing
   * pool on a read that missed a ceiling gets the em dash, the words on its
   * accessible name, and no digit.
   */
  it('does not compute a counterfactual over ceilings it never read', async () => {
    renderProfiles(
      capacity({
        pools: [pool({ name: 'global', active: 8, available: 0 })],
        runner_profiles: {
          'claude-code': profile({
            pools: ['global', 'tenant:eng'],
            admission: admission({
              headroom: null, basis: 'unknown', complete: false, unread: ['tenant:eng'],
              blockers: [blocker({})], counterfactual: [],
            }),
          }),
        },
      }),
    )
    const card = await profileCard('claude-code')
    const cell = liftCell(card, 'global')
    expect(cell.getAttribute('aria-label') ?? '').toContain('Not computed')
    expect(cell.querySelector('.ctl-em'), 'the refusing pool drew no em dash').not.toBeNull()
    // And no number is offered in its place.
    expect(cell.textContent ?? '').not.toMatch(/\d/)
  })
})

// ---------------------------------------------------------------------------
// The grouping is the server's
// ---------------------------------------------------------------------------

describe('blocker grouping', () => {
  const needsAction = blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', group: null })
  const noRoom = blocker({ pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', group: null })

  it('uses the groups the response carried', () => {
    const h = headroomFor(profile({ admission: admission({ blockers: [needsAction, noRoom] }) }))
    render(<BlockerList h={h} groups={{ needs_action: ['MANUAL_PAUSE'], no_room: ['GLOBAL_CONCURRENCY_LIMIT'] }} />)
    expect(screen.getByText('Somebody has to act')).toBeTruthy()
    expect(screen.getByText('Eligible, no room')).toBeTruthy()
    expect(screen.queryByText('Not grouped')).toBeNull()
  })

  it('puts a reason in NEITHER group in its own section rather than under "waiting is fine"', () => {
    // Filing an unknown reason under "waiting is a valid answer" is a lie
    // about the remedy, and the more comfortable of the two lies available.
    const h = headroomFor(profile({ admission: admission({ blockers: [blocker({ pool: 'backend:gke', reason: 'SOMETHING_NEW', group: null })] }) }))
    render(<BlockerList h={h} groups={{ needs_action: [], no_room: [] }} />)
    expect(screen.getByText('Not grouped')).toBeTruthy()
    expect(screen.queryByText('Eligible, no room')).toBeNull()
    // And the raw reason is still printed, so the staleness is visible.
    expect(document.body.textContent).toContain('SOMETHING_NEW')
  })
})

// ---------------------------------------------------------------------------
// The counterfactual is a snapshot, not a promise
// ---------------------------------------------------------------------------

/**
 * CP-6 (#85) RE-POINT, ALL THREE. These rendered `<Counterfactuals>`, the
 * "If one ceiling were lifted" list under each Profile headroom card, whose
 * rows were sentences: "4 more would have started", "nothing would have
 * changed -- anthropic still binds". The owner's decision made the list a
 * `+N if lifted` column on the pools that cap the card, and dropped the rows
 * for pools whose lifting buys nothing.
 *
 * WHAT DID NOT MOVE, and each test below still pins it: the prediction is
 * worded in the past conditional against the server's own instant (the
 * instant is now said once, in the page foot, rather than once per card); a
 * zero on a pool that binds names what still binds; an unmeasurable change
 * is an em dash with its reason, never a 0. The sentence each row used to be
 * is now the cell's accessible name, beside the figure it explains.
 */
describe('the counterfactual', () => {
  function oneProfile(list: Counterfactual[], binding: string[] = ['global']) {
    return capacity({
      generated_at: '2026-09-22T10:00:00Z',
      pools: [pool({ name: 'global' }), pool({ name: 'provider:anthropic' })],
      runner_profiles: {
        'claude-code': profile({
          pools: ['global', 'provider:anthropic'],
          admission: admission({ headroom: 3, binding, counterfactual: list }),
        }),
      },
    })
  }

  it('is worded in the past conditional, against the server’s own instant', async () => {
    renderProfiles(oneProfile([cf({})]))
    const card = await profileCard('claude-code')
    const said = liftCell(card, 'global').getAttribute('aria-label') ?? ''
    expect(said).toContain('would have started')
    const text = `${document.body.textContent ?? ''} ${said}`
    expect(text).not.toContain('will start')
    expect(text).not.toContain('you can start')
    // The instant, once for the whole page, from the server's own clock.
    const time = document.querySelector('.provenance time')
    expect(time, 'the page does not say when the counts were read').not.toBeNull()
    expect(time!.getAttribute('dateTime') ?? time!.getAttribute('datetime')).toBe('2026-09-22T10:00:00Z')
  })

  it('a zero delta names what still binds, rather than reading as a glitch', async () => {
    renderProfiles(oneProfile([cf({ delta: 0, headroom_after: 3, next_binding: ['provider:anthropic'] })]))
    const cell = liftCell(await profileCard('claude-code'), 'global')
    expect(cell.className).toContain('is-pointless')
    expect(cell.textContent).toBe('+0')
    expect(cell.getAttribute('aria-label')).toContain('nothing would have changed')
    expect(cell.getAttribute('aria-label')).toContain('anthropic')
  })

  it('an unmeasurable delta is said to be unmeasurable, not shown as 0', async () => {
    renderProfiles(oneProfile([cf({ delta: null, headroom_after: 3 })]))
    const cell = liftCell(await profileCard('claude-code'), 'global')
    expect(cell.querySelector('.ctl-em'), 'an unmeasured change drew no em dash').not.toBeNull()
    expect(cell.textContent ?? '').not.toMatch(/\d/)
    expect(cell.getAttribute('aria-label')).toContain('the change could not be measured')
  })
})

// ---------------------------------------------------------------------------
// Whole-screen invariants
// ---------------------------------------------------------------------------

describe('the capacity board as a whole', () => {
  it('states the conjunction on the column whose figure it governs', async () => {
    // B4.5 RE-POINT -- AND THE CLAIM GOT STRONGER, WHICH IS THE POINT OF THE
    // MEDIUM BEING FREE.
    //
    // WHAT MOVED. `<Conjunction/>` was a paragraph above the table: "A task
    // must clear EVERY pool in its list at the same moment. Capacity is the
    // MINIMUM across them, never a sum." It is deleted as a paragraph and is
    // now the name of the column it is about -- `Could start (min across
    // pools)` -- with the argument at `#help/pools-all-at-once`, reachable
    // from the `?` in that same header cell.
    //
    // WHY THAT IS NOT A WEAKENING. The paragraph sat above a table and a
    // reader who scrolled past it read every figure with no qualifier at all;
    // worse, it could be read as qualifying the wrong column, since it named
    // none. A parenthetical in the column head cannot be separated from the
    // numbers under it and cannot be applied to another column. The old
    // assertion proved a sentence existed SOMEWHERE on the screen. This one
    // proves the qualifier is attached to the figure.
    renderCapacity(
      capacity({ runner_profiles: { 'claude-code': profile({ admission: admission({}) }) } }),
    )
    await screen.findByRole('rowheader', { name: 'claude-code' })

    const head = screen.getByRole('columnheader', { name: /Could start/ })
    expect(head.textContent).toContain('min across pools')
    // The figure the qualifier governs is in this column and no other.
    const heads = [...document.querySelectorAll('thead th')]
    expect(heads.indexOf(head)).toBe(1)
    // The paragraph is gone from the surface entirely.
    expect(document.querySelector('.conjunction')).toBeNull()
  })

  /**
   * CP-11 (#85) RE-POINT. The assertion above used to end by pinning the `?`
   * to the `Could start` header cell. Below 900px §B6.3 stacks the table and
   * HIDES its `<thead>`, so the screen's one in-content `?` went with it --
   * still in the tab order, invisible -- and the stacked key read a bare
   * `Could start`, dropping the conjunction the column name exists to carry.
   *
   * WHAT DID NOT MOVE: the qualifier is still attached to the figure, and the
   * argument is still one focusable click away. What moved is WHERE each
   * lives, to the two places that survive stacking: the qualifier into the
   * cell's own `data-label` (the stacked key), and the `?` into the panel
   * head, which no breakpoint hides.
   */
  it('keeps the conjunction and its ? where a phone still shows them', async () => {
    renderCapacity(
      capacity({ runner_profiles: { 'claude-code': profile({ admission: admission({}) }) } }),
    )
    await screen.findByRole('rowheader', { name: 'claude-code' })

    // The stacked key is the column's name, qualifier included.
    expect(couldStart('claude-code').getAttribute('data-label')).toContain('min across pools')

    // The `?` is in the panel's head and in no part of the table a phone hides.
    // Found by what it IS -- a disclosure button drawing `?` -- rather than by
    // its accessible name, whose wording is HelpCard's to change.
    const panel = document.querySelector('.cap-headroom')!
    const glyphs = [...panel.querySelectorAll('button[aria-expanded]')].filter(
      (b) => b.textContent === '?',
    )
    expect(glyphs.length, 'the headroom panel lost its ?').toBeGreaterThan(0)
    for (const glyph of glyphs) {
      expect(glyph.closest('thead'), 'a ? is inside the <thead> that stacking hides').toBeNull()
      expect(glyph.closest('table'), 'a ? is inside the table').toBeNull()
    }
  })

  it('declares the tenant beside the headroom figures, every time', async () => {
    // Trap D: the pool list is the CALLING tenant's, including for an admin.
    // An unlabelled figure here reads as the platform's capacity.
    // B4.5 RE-POINT. The scope badge `<span class="scope tenant">for tenant
    // eng</span>` beside an `<h2>` became `.ctl-card-note` in the card head --
    // §6.1's qualifier slot, which exists for exactly this: a fact ABOUT the
    // card's figures, right-aligned, mono, muted, one line, no verb. The word
    // "for" went; the tenant did not, and the tenant is the whole assertion.
    // An unlabelled figure here reads as the platform's capacity, and the pool
    // list is the CALLING tenant's even for an admin.
    renderCapacity(capacity({ tenant_id: 'eng', runner_profiles: { 'claude-code': profile({ admission: admission({}) }) } }))
    await screen.findByRole('rowheader', { name: 'claude-code' })

    // §B6.1 RE-POINT. The claim, the slot and the words are unchanged; the
    // PANEL moved. Headroom was a `.ctl-card` wrapping a `.ctl-table` -- two
    // concentric boxes -- and is now a `.section.cap-headroom` whose heading
    // and qualifier sit on the page above the one box, which is the
    // construction the pool families beside it already used. So this asks the
    // headroom panel by name instead of asking for "the first card on the
    // screen", which after the change is a pool family and answers
    // `platform-wide`. The qualifier is still a `.ctl-card-note`, still in the
    // panel's own head, still beside the figures rather than in a banner.
    const panel = document.querySelector('.cap-headroom')
    const note = panel?.querySelector('.ctl-card-note')
    expect(note, 'the headroom panel declares no scope at all').not.toBeNull()
    expect(note?.textContent).toContain('eng')
    // It is in the same panel as the figures it scopes, not a banner above them.
    expect(panel?.querySelector('table')).not.toBeNull()
  })

  /**
   * CP-2 (#85) RE-POINT. The claim is Trap E's and did not move: a figure may
   * only sit beside a figure of the same scope, so every figure on Pools
   * declares its scope. What moved is WHERE. This asserted one
   * `.ctl-card-note` per family, and that note was computed from the family's
   * FIRST row: an admin's Tenants family printed "this tenant" above four
   * tenants' pools, and Providers printed "platform-wide" above per-tenant
   * slices. The owner's decision is a Scope column on every row, and the
   * family note is removed, so a family holding two scopes says both.
   */
  it('declares scope on every pool row, so two figures are never silently compared', async () => {
    renderCapacity(
      capacity({
        tenant_id: 'eng',
        pools: [
          pool({ name: 'global' }),
          pool({ name: 'tenant:eng' }),
          pool({ name: 'tenant:research' }),
          pool({ name: 'provider:anthropic' }),
          pool({ name: 'provider:anthropic:tenant:eng' }),
          pool({ name: 'provider:anthropic:tenant:research' }),
        ],
      }),
    )
    await screen.findByText('Global')

    const families = [...document.querySelectorAll('.cap-families > .ctl-card')]
    expect(families.length).toBe(3)
    for (const f of families) {
      expect(
        f.querySelector('.ctl-card-head > .ctl-card-note'),
        'a family still declares one scope, read off its first row, for all of them',
      ).toBeNull()
      const heads = [...f.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
      expect(heads, 'a family table has no Scope column').toContain('Scope')
    }

    expect(scopeCell('global')).toBe('platform')
    expect(scopeCell('provider:anthropic')).toBe('platform')
    // The per-tenant slice of a provider pool is tenant scope, not platform.
    expect(scopeCell('tenant:eng')).toBe('this tenant')
    expect(scopeCell('provider:anthropic:tenant:eng')).toBe('this tenant')
    // The rows the first-row note mislabelled: another tenant's pools.
    expect(scopeCell('tenant:research')).toBe('tenant research')
    expect(scopeCell('provider:anthropic:tenant:research')).toBe('tenant research')
  })

  it('carries the same scope on every card in the Cards view (CP-2)', async () => {
    renderCapacity(
      capacity({ tenant_id: 'eng', pools: [pool({ name: 'global' }), pool({ name: 'tenant:research' })] }),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Cards' }))
    expect(poolCard('global').querySelector('.cap-pool-scope')?.textContent).toBe('platform')
    expect(poolCard('tenant:research').querySelector('.cap-pool-scope')?.textContent).toBe('tenant research')
  })

  it('marks a pool carrying more than its ceiling as drift rather than as full', async () => {
    renderCapacity(capacity({ pools: [pool({ name: 'global', active: 9, effective_limit: 8, available: 0 })] }))
    expect(await screen.findByText('over ceiling')).toBeTruthy()
    expect(screen.queryByText('full')).toBeNull()
  })

  it('shows a paused pool as paused even when it is carrying nothing', async () => {
    renderCapacity(capacity({ pools: [pool({ name: 'tenant:eng', enabled: false, active: 0 })] }))
    expect(await screen.findByText('paused')).toBeTruthy()
    expect(screen.queryByText('ok')).toBeNull()
  })

  it('a failed read of capacity renders no pool numbers at all', async () => {
    loadCapacity.mockResolvedValue({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'Firestore did not answer.' },
    })
    render(<CapacityScreen />)
    await screen.findByText('Firestore did not answer.')
    expect(screen.queryByText('Headroom')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
    expectNoFigures(document.body, ['HTTP 503'])
  })
})

// ---------------------------------------------------------------------------
// Visual QA 2026-09-25, Capacity (#85). Each block below was pushed before the
// fix it demands, so the red run on the PR is the proof it can see the defect.
// ---------------------------------------------------------------------------

function renderProfiles(data: Capacity) {
  loadCapacity.mockResolvedValue({ status: 'ok', data, fetchedAt: Date.now(), serverAt: data.generated_at })
  return render(<ProfilesScreen />)
}

/** One Profile headroom card, by the runner profile it is about. */
async function profileCard(name: string): Promise<HTMLElement> {
  const id = await screen.findByText(name, { selector: 'h2 .mono' })
  const card = id.closest('section')
  if (!card) throw new Error(`no card for ${name}`)
  return card as HTMLElement
}

/** The `<dd>` a Profile headroom card files under `key`. */
function kv(card: HTMLElement, key: string): HTMLElement {
  const dt = [...card.querySelectorAll('dl.kv > dt')].find((d) => d.textContent === key)
  expect(dt, `the card has no ${key} key`).toBeTruthy()
  return dt!.nextElementSibling as HTMLElement
}

/** The row a Profile headroom card draws for one pool. */
function poolRow(card: HTMLElement, name: string): HTMLElement {
  const th = [...card.querySelectorAll('th[title]')].find((t) => t.getAttribute('title') === name)
  expect(th, `the card draws no row for ${name}`).toBeTruthy()
  return th!.closest('tr') as HTMLElement
}

/** The name of the column that carries the counterfactual (CP-6). */
const LIFTED = '+N if lifted'

/**
 * A pool's cell in a Profile headroom card's `+N if lifted` column, found by
 * the column's HEADER rather than by a class, so the test reads the column a
 * reader reads.
 */
function liftCell(card: HTMLElement, name: string): HTMLElement {
  const heads = [...card.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
  const at = heads.indexOf(LIFTED)
  expect(at, `the card has no ${LIFTED} column: ${heads.join(' | ')}`).toBeGreaterThanOrEqual(0)
  const cell = poolRow(card, name).children[at]
  expect(cell, `${name} has no ${LIFTED} cell`).toBeTruthy()
  return cell as HTMLElement
}

/** The Status cell a Profile headroom card draws for one pool. */
function statusCell(card: HTMLElement, name: string): HTMLElement {
  const cell = poolRow(card, name).querySelector('td[data-label="Status"]')
  expect(cell, `${name} has no Status cell`).not.toBeNull()
  return cell as HTMLElement
}

/** The Scope a Pools family table prints for one pool (CP-2). */
function scopeCell(name: string): string {
  const th = [...document.querySelectorAll('.cap-families tbody th[title]')].find(
    (t) => t.getAttribute('title') === name,
  )
  expect(th, `Pools draws no row for ${name}`).toBeTruthy()
  const cell = th!.closest('tr')!.querySelector('td[data-label="Scope"]')
  expect(cell, `${name} has no Scope cell`).not.toBeNull()
  return (cell!.textContent ?? '').trim()
}

/** The Pools family-table row for one pool. */
function familyRow(name: string): HTMLElement {
  const th = [...document.querySelectorAll('.cap-families tbody th[title]')].find(
    (t) => t.getAttribute('title') === name,
  )
  expect(th, `Pools draws no row for ${name}`).toBeTruthy()
  return th!.closest('tr') as HTMLElement
}

/** One tile in Pools' Cards view. */
function poolCard(name: string): HTMLElement {
  const card = [...document.querySelectorAll('.cap-pool')].find(
    (p) => p.querySelector('.cap-pool-name')?.getAttribute('title') === name,
  )
  expect(card, `the Cards view draws no tile for ${name}`).toBeTruthy()
  return card as HTMLElement
}

/** The state marks a Pools row or tile draws, as `word|modifier` pairs. */
function chipsOf(el: Element): string[] {
  return [...el.querySelectorAll('.ctl-chip')].map((c) => {
    const mod = [...c.classList].find((k) => k.startsWith('is-')) ?? '(none)'
    return `${(c.textContent ?? '').trim()}|${mod}`
  })
}

describe('an em dash is only ever "not measured" (CP-1)', () => {
  it('Held back by draws a word when the read is complete and nothing refuses', async () => {
    renderCapacity(
      capacity({
        runner_profiles: {
          'claude-code': profile({ admission: admission({ headroom: 3, complete: true, blockers: [], uncapped: [] }) }),
        },
      }),
    )
    const row = (await screen.findByRole('rowheader', { name: 'claude-code' })).closest('tr')!
    const held = row.querySelector('td[data-label="Held back by"]')!
    // Measured, complete, and nothing refusing: a fact, so not the mark this
    // screen reserves for a figure nobody measured.
    expect(held.textContent).not.toContain('—')
    expect(held.querySelector('.ctl-em')).toBeNull()
    expect(held.textContent).toMatch(/[a-z]/)
  })

  it('Profile headroom says a profile needs no provider rather than drawing a dash', async () => {
    renderProfiles(
      capacity({ runner_profiles: { mock: profile({ provider: null, admission: admission({}) }) } }),
    )
    const card = await profileCard('mock')
    const provider = kv(card, 'Provider')
    expect(provider.textContent).not.toContain('—')
    // Runtimes' words for the same fact, so the two screens say it one way.
    expect(provider.textContent).toContain('none needed')
  })
})

describe('a disabled runner profile is not advertised with headroom (CP-3)', () => {
  const REASON = 'codex is disabled on this platform. Use claude-code.'
  const codex = () =>
    profile({
      provider: 'openai',
      available: false,
      disabled_reason: REASON,
      admission: admission({
        headroom: 10,
        counterfactual: [
          { pool: 'global', action: 'raise', headroom_after: 14, basis_after: 'measured', delta: 4, next_binding: [] },
        ],
      }),
    })

  it('Pools draws disabled, with its reason, where the figure would be', async () => {
    renderCapacity(capacity({ runner_profiles: { codex: codex() } }))
    await screen.findByRole('rowheader', { name: 'codex' })
    const cell = couldStart('codex')
    expect(cell.textContent).toContain('disabled')
    expect(cell.textContent, 'a disabled profile still shows a headroom figure').not.toMatch(/\d/)
    // The reason is on the row, as text a reader can see.
    expect(cell.closest('tr')!.textContent).toContain(REASON)
  })

  it('Profile headroom draws disabled with its reason, and no counterfactual', async () => {
    renderProfiles(capacity({ runner_profiles: { codex: codex() } }))
    const card = await profileCard('codex')
    expect(card.textContent).toContain('disabled')
    expect(card.textContent).toContain(REASON)
    expect(card.textContent).not.toContain('could start')
    expect(card.textContent).not.toContain('would have started')
    expect(card.querySelector('.counterfactual')).toBeNull()
    expect(card.querySelector('h2')!.textContent).not.toMatch(/\d/)
  })

  it('an available profile is still priced as before', async () => {
    renderProfiles(capacity({ runner_profiles: { 'claude-code': profile({ available: true, admission: admission({ headroom: 3 }) }) } }))
    const card = await profileCard('claude-code')
    expect(card.querySelector('h2')!.textContent).toContain('3')
    expect(card.textContent).not.toContain('disabled')
  })
})

describe('Profile headroom names every binding pool (CP-4, CP-13)', () => {
  const tie = () =>
    capacity({
      pools: [
        pool({ name: 'global', effective_limit: 5, available: 5 }),
        pool({ name: 'tenant:eng', effective_limit: 5, available: 5 }),
        pool({ name: 'resource:standard', effective_limit: 20, available: 20 }),
      ],
      runner_profiles: {
        'claude-code': profile({
          pools: ['global', 'tenant:eng', 'resource:standard'],
          admission: admission({ headroom: 5, binding: ['global', 'tenant:eng'], blockers: [] }),
        }),
      },
    })

  it('tags every pool the server says binds, and only those', async () => {
    renderProfiles(tie())
    const card = await profileCard('claude-code')
    for (const name of ['global', 'tenant:eng']) {
      expect(poolRow(card, name).textContent, `${name} binds and is not tagged`).toContain('binding')
    }
    expect(poolRow(card, 'resource:standard').textContent).not.toContain('binding')
  })

  it('draws binding as the info chip, not the retired warn tag', async () => {
    renderProfiles(tie())
    const card = await profileCard('claude-code')
    const chip = [...poolRow(card, 'global').querySelectorAll('.ctl-chip')].find((c) =>
      (c.textContent ?? '').includes('binding'),
    )
    expect(chip, 'binding is not drawn as a .ctl-chip').toBeTruthy()
    expect(chip!.className).toContain('is-info')
    expect(card.querySelector('.tag.capped')).toBeNull()
  })

  it('names every binding pool in the line under the table', async () => {
    renderProfiles(tie())
    const card = await profileCard('claude-code')
    const foot = [...card.querySelectorAll('p')].find((p) => (p.textContent ?? '').includes('run out first'))
    expect(foot, 'no "would run out first" line').toBeTruthy()
    expect(foot!.textContent).toContain('global')
    expect(foot!.textContent).toContain('eng')
  })
})

describe('Profile headroom carries its scope as a card note, not paragraphs (CP-5)', () => {
  it('drops the conjunction banner and the tenant-scope paragraph, and notes the tenant on every card', async () => {
    renderProfiles(
      capacity({
        tenant_id: 'eng',
        pools: [pool({ name: 'global' }), pool({ name: 'tenant:eng' })],
        runner_profiles: {
          'claude-code': profile({ pools: ['global', 'tenant:eng'], admission: admission({}) }),
          mock: profile({ provider: null, pools: ['global', 'tenant:eng'], admission: admission({}) }),
        },
      }),
    )
    const cards = [await profileCard('claude-code'), await profileCard('mock')]
    expect(document.querySelector('.conjunction')).toBeNull()
    const paragraphs = [...document.querySelectorAll('p')].map((p) => p.textContent ?? '')
    expect(paragraphs.some((t) => t.includes('never a sum'))).toBe(false)
    expect(paragraphs.some((t) => t.includes('including if'))).toBe(false)
    for (const card of cards) {
      const note = card.querySelector('.ctl-card-note')
      expect(note, 'a Profile headroom card declares no tenant').not.toBeNull()
      expect(note!.textContent).toBe('tenant eng')
    }
  })

  /**
   * CP-5'S SECOND HALF, which #144 listed as not done: "use a column name plus
   * the one `?`, as Pools does". Pools says the conjunction in the name of the
   * column it governs -- `Could start (min across pools)` -- and draws one `?`
   * for `pools-all-at-once`, outside any table. Profile headroom deleted its
   * banner and said the conjunction nowhere on the cards: the pool column
   * read `Pool it must clear`, which is true of one pool at a time.
   *
   * ONE `?` FOR THE SCREEN, NOT ONE PER CARD. The screen draws a card per
   * profile, so a glyph in each card head would be five glyphs saying one
   * thing. The conjunction is a property of every card on the screen, and
   * AH-24's slot for a property of the whole screen is after its title.
   *
   * MUTATION: put `Pool it must clear` back. The column no longer says every
   * pool at once. MUTATION: drop `help` from the Screen, or draw the glyph in
   * each card. The screen has no `?`, or one per card.
   */
  it('names the conjunction on every card’s pool column and draws one ? for the screen, after its title', async () => {
    renderProfiles(
      capacity({
        tenant_id: 'eng',
        pools: [pool({ name: 'global' }), pool({ name: 'tenant:eng' })],
        runner_profiles: {
          'claude-code': profile({ pools: ['global', 'tenant:eng'], admission: admission({}) }),
          mock: profile({ provider: null, pools: ['global', 'tenant:eng'], admission: admission({}) }),
        },
      }),
    )
    const cards = [await profileCard('claude-code'), await profileCard('mock')]
    for (const card of cards) {
      const first = card.querySelector('thead th')
      expect(first, 'a card has no pool column').not.toBeNull()
      expect(first!.textContent, 'the pool column does not say every pool is cleared at once').toMatch(/all at once/)
    }
    const glyphs = [...document.querySelectorAll('button[aria-expanded]')].filter((b) => b.textContent === '?')
    expect(glyphs, 'Profile headroom draws no ?, or one per card').toHaveLength(1)
    const glyph = glyphs[0]!
    expect(glyph.closest('.head'), 'the ? is not beside the screen title').not.toBeNull()
    expect(glyph.closest('table'), 'the ? is inside a table').toBeNull()
    expect(glyph.getAttribute('aria-label')).toContain('Every pool at once, or none of them')
  })
})

describe('units are named on every column that counts them (CP-24)', () => {
  it('Pools: Weight, In use and Ceiling all carry (units), stacked keys too', async () => {
    renderCapacity(
      capacity({ pools: [pool({ name: 'global' })], runner_profiles: { 'claude-code': profile({ admission: admission({}) }) } }),
    )
    await screen.findByRole('rowheader', { name: 'claude-code' })
    const heads = [...document.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
    for (const name of ['Weight', 'In use', 'Ceiling']) {
      const found = heads.filter((h) => h.startsWith(name))
      expect(found.length, `no ${name} column`).toBeGreaterThan(0)
      for (const h of found) expect(h, `${name} does not say its unit`).toContain('(units)')
    }
    const keyed = [
      ...document.querySelectorAll('td[data-label^="Weight"], td[data-label^="In use"], td[data-label^="Ceiling"]'),
    ]
    expect(keyed.length).toBeGreaterThan(0)
    for (const td of keyed) expect(td.getAttribute('data-label')).toContain('(units)')
  })

  it('Profile headroom writes Weight as the figure, not as a rationale', async () => {
    renderProfiles(capacity({ runner_profiles: { browser: profile({ units: 2, admission: admission({}) }) } }))
    const weight = kv(await profileCard('browser'), 'Weight')
    expect(weight.textContent).not.toContain('added to every pool')
    expect(weight.textContent).toMatch(/^2u\b/)
  })
})

/**
 * CP-6 (#85) RE-POINT of the CP-13 and CP-15 rows. These rendered the
 * `.cf-row` list and read each row's class and sentence. The rows are now
 * cells in the `+N if lifted` column, so the same four claims are asked of
 * the cells: the class still says which KIND of answer a cell is, and the
 * sentence the row used to print is the cell's accessible name, where its
 * grammar and its pool labels are still read.
 */
describe('a counterfactual cell says what kind of answer it is (CP-13, CP-15)', () => {
  /** One `claude-code` card over `pools`, `binding` of which cap its figure. */
  function cells(list: Counterfactual[], pools: string[], binding: string[]): void {
    renderProfiles(
      capacity({
        pools: pools.map((name) => pool({ name })),
        runner_profiles: {
          'claude-code': profile({ pools, admission: admission({ headroom: 3, binding, counterfactual: list }) }),
        },
      }),
    )
  }

  it('marks an unmeasured change apart from a measured result', async () => {
    const pools = ['global', 'tenant:eng', 'resource:standard']
    cells(
      [
        cf({ pool: 'global' }),
        cf({ pool: 'tenant:eng', delta: null, headroom_after: 3 }),
        cf({ pool: 'resource:standard', delta: null, headroom_after: null }),
      ],
      pools,
      pools,
    )
    const card = await profileCard('claude-code')
    const [measured, unmeasured, uncaps] = pools.map((p) => liftCell(card, p))
    expect(unmeasured!.className).toContain('is-unmeasured')
    expect(measured!.className).not.toContain('is-unmeasured')
    expect(measured!.textContent).toBe('+4')
    // "Nothing else would have been left to cap it" is an answer, not a gap:
    // a word, not a dash and not a number.
    expect(uncaps!.className).not.toContain('is-unmeasured')
    expect(uncaps!.textContent).toBe('uncapped')
  })

  it('says bind, not binds, when more than one pool would still bind', async () => {
    const pools = ['global', 'tenant:eng', 'provider:anthropic']
    cells([cf({ delta: 0, headroom_after: 3, next_binding: ['tenant:eng', 'provider:anthropic'] })], pools, ['global'])
    const said = liftCell(await profileCard('claude-code'), 'global').getAttribute('aria-label') ?? ''
    expect(said).toMatch(/still bind\b/)
    expect(said).not.toContain('still binds')
  })

  it('keeps binds for a single pool', async () => {
    const pools = ['global', 'tenant:eng']
    cells([cf({ delta: 0, headroom_after: 3, next_binding: ['tenant:eng'] })], pools, ['global'])
    const said = liftCell(await profileCard('claude-code'), 'global').getAttribute('aria-label') ?? ''
    expect(said).toContain('still binds')
  })

  it('never prints two different pools under one label', async () => {
    const pools = ['resource:browser', 'runner:browser']
    cells(
      [
        cf({ pool: 'resource:browser', delta: 0, headroom_after: 3, next_binding: ['runner:browser'] }),
        cf({ pool: 'runner:browser', delta: 0, headroom_after: 3, next_binding: ['resource:browser'] }),
      ],
      pools,
      pools,
    )
    const card = await profileCard('claude-code')
    const labels = pools.map((p) => {
      const th = [...card.querySelectorAll('tbody th[title]')].find((t) => t.getAttribute('title') === p)
      return th?.firstChild?.textContent ?? ''
    })
    expect(new Set(labels).size, `two pools share a label: ${labels.join(' | ')}`).toBe(labels.length)
    for (const p of pools) {
      expect(liftCell(card, p).getAttribute('aria-label') ?? '').not.toMatch(/browser and browser/)
    }
  })
})

// ---------------------------------------------------------------------------
// The owner's decisions on #85, 2026-09-25 (CP-2, CP-6, CP-12, CP-14). Each
// block was pushed before the change it pins.
// ---------------------------------------------------------------------------

describe('Profile headroom gives every Status cell one mark (CP-12)', () => {
  /**
   * One card with a pool of every kind the Status column tells apart, and one
   * card whose read missed a pool. `global` is read, capped, running and
   * refusing nothing: the healthy row that used to be blank.
   */
  const marked = () =>
    capacity({
      tenant_id: 'eng',
      pools: [
        pool({ name: 'global', active: 3, available: 5 }),
        pool({ name: 'tenant:eng', enabled: false }),
        pool({ name: 'resource:browser', hard_limit: 0, effective_limit: 0, available: 0 }),
        pool({ name: 'runner:browser', hard_limit: 4, effective_limit: 4, active: 4, available: 0 }),
      ],
      runner_profiles: {
        browser: profile({
          resource_class: 'browser',
          units: 2,
          pools: ['global', 'tenant:eng', 'resource:browser', 'runner:browser', 'provider:anthropic'],
          admission: admission({
            units: 2,
            headroom: 0,
            blockers: [
              blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', limit: 8, active: 0, group: 'needs_action' }),
              blocker({ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 0, active: 0 }),
              blocker({ pool: 'runner:browser', reason: 'RUNNER_LIMIT', limit: 4, active: 4 }),
            ],
            binding: ['tenant:eng', 'resource:browser', 'runner:browser'],
            uncapped: ['provider:anthropic'],
          }),
        }),
        mock: profile({
          provider: null,
          pools: ['global', 'backend:cloudrun'],
          admission: admission({
            headroom: null, basis: 'unknown', complete: false, unread: ['backend:cloudrun'], blockers: [],
          }),
        }),
      },
    })

  it('holds a chip or a mark in every row’s Status cell, and exactly one status mark', async () => {
    renderProfiles(marked())
    for (const name of ['browser', 'mock']) {
      const card = await profileCard(name)
      const rows = [...card.querySelectorAll('tbody tr')]
      expect(rows.length, `${name} drew no pool rows`).toBeGreaterThan(0)
      for (const row of rows) {
        const cell = row.querySelector('td[data-label="Status"]')!
        const pool = row.querySelector('th')?.getAttribute('title')
        expect(cell.querySelector('.ctl-chip, .ctl-mark'), `${name} ${pool}: the Status cell is blank`).not.toBeNull()
        // The `binding` fact may follow the status; it is not a status.
        const marks = [...cell.querySelectorAll('.ctl-chip, .ctl-mark')].filter(
          (m) => (m.textContent ?? '').trim() !== 'binding',
        )
        expect(marks.length, `${name} ${pool}: ${marks.map((m) => m.textContent).join(', ')}`).toBe(1)
      }
    }
  })

  it('draws a healthy pool that was read with the ok chip Pools draws', async () => {
    renderProfiles(marked())
    const chip = statusCell(await profileCard('browser'), 'global').querySelector('.ctl-chip')
    expect(chip?.className).toContain('is-ok')
    expect(chip?.textContent).toBe('ok')
  })

  it('draws an unread pool with the not-read mark', async () => {
    renderProfiles(marked())
    const mark = statusCell(await profileCard('mock'), 'backend:cloudrun').querySelector('.ctl-mark')
    expect(mark?.className).toContain('is-unread')
    expect(mark?.textContent).toBe('not read')
  })

  it('says limit 0, not full, for a blocker at a ceiling of zero', async () => {
    renderProfiles(marked())
    const card = await profileCard('browser')
    const cell = statusCell(card, 'resource:browser')
    expect(cell.textContent).toContain('limit 0')
    expect(cell.textContent).not.toContain('full')
    // Capped by a person, so the paused tone: waiting does not clear it.
    expect(cell.querySelector('.ctl-chip')?.className).toContain('is-paused')
    // The row follows the mark.
    expect(poolRow(card, 'resource:browser').className).not.toContain('full')
  })

  it('draws full, paused and uncapped in Pools’ vocabulary', async () => {
    renderProfiles(marked())
    const card = await profileCard('browser')
    const first = (pool: string) => statusCell(card, pool).querySelector('.ctl-chip, .ctl-mark')
    expect(first('runner:browser')?.className).toContain('is-warn')
    expect(first('runner:browser')?.textContent).toBe('full')
    expect(first('tenant:eng')?.className).toContain('is-paused')
    expect(first('tenant:eng')?.textContent).toBe('paused')
    expect(first('provider:anthropic')?.className).toContain('is-info')
    expect(first('provider:anthropic')?.textContent).toBe('uncapped')
    // Each mark carries the explanation its `.tag` carried.
    for (const pool of ['runner:browser', 'tenant:eng', 'provider:anthropic', 'resource:browser']) {
      expect(first(pool)?.getAttribute('title'), `${pool}'s mark lost its explanation`).toBeTruthy()
      expect(first(pool)?.getAttribute('aria-label'), `${pool}'s mark has no accessible sentence`).toBeTruthy()
    }
  })

  it('keeps no .tag in a Profile headroom card', async () => {
    renderProfiles(marked())
    for (const name of ['browser', 'mock']) {
      expect((await profileCard(name)).querySelector('.tag'), `${name} still draws a .tag`).toBeNull()
    }
  })
})

describe('Profile headroom draws the counterfactual as a column (CP-6)', () => {
  /**
   * `claude-code` is capped by a TIE: `global` and `tenant:eng` both bind, so
   * lifting either one alone buys nothing (+0), which is the case the card's
   * tags and its sentences used to contradict each other on. `browser` is
   * capped by `global` alone. The other pools bind nothing, and those are the
   * "nothing would have changed" rows that go.
   */
  const two = () =>
    capacity({
      generated_at: '2026-09-22T10:00:00Z',
      pools: [
        pool({ name: 'global', effective_limit: 5, available: 5 }),
        pool({ name: 'tenant:eng', effective_limit: 5, available: 5 }),
        pool({ name: 'resource:standard', effective_limit: 20, available: 20 }),
        pool({ name: 'resource:browser', effective_limit: 20, available: 20 }),
      ],
      runner_profiles: {
        'claude-code': profile({
          pools: ['global', 'tenant:eng', 'resource:standard'],
          admission: admission({
            headroom: 5,
            binding: ['global', 'tenant:eng'],
            counterfactual: [
              cf({ pool: 'global', delta: 0, headroom_after: 5, next_binding: ['tenant:eng'] }),
              cf({ pool: 'tenant:eng', delta: 0, headroom_after: 5, next_binding: ['global'] }),
              cf({ pool: 'resource:standard', delta: 0, headroom_after: 5, next_binding: ['global', 'tenant:eng'] }),
            ],
          }),
        }),
        browser: profile({
          resource_class: 'browser',
          pools: ['global', 'resource:browser'],
          admission: admission({
            headroom: 5,
            binding: ['global'],
            counterfactual: [
              cf({ pool: 'global', delta: 15, headroom_after: 20, next_binding: ['resource:browser'] }),
              cf({ pool: 'resource:browser', delta: 0, headroom_after: 5, next_binding: ['global'] }),
            ],
          }),
        }),
      },
    })

  it('puts +N on every pool that binds, and nothing on a pool whose lifting buys nothing', async () => {
    renderProfiles(two())
    const browser = await profileCard('browser')
    expect(liftCell(browser, 'global').textContent).toBe('+15')
    expect(liftCell(browser, 'resource:browser').textContent).toBe('')

    const tie = await profileCard('claude-code')
    expect(liftCell(tie, 'global').textContent).toBe('+0')
    expect(liftCell(tie, 'tenant:eng').textContent).toBe('+0')
    expect(liftCell(tie, 'resource:standard').textContent).toBe('')
  })

  it('draws no counterfactual list and none of its sentences', async () => {
    renderProfiles(two())
    for (const name of ['browser', 'claude-code']) {
      const card = await profileCard(name)
      expect(card.querySelector('.counterfactual, .cf-list, .cf-row'), `${name} still draws the list`).toBeNull()
      expect(card.textContent).not.toContain('nothing would have changed')
      expect(card.textContent).not.toContain('No pool is refusing')
      expect(card.textContent).not.toContain('From pool counts read')
    }
  })

  it('says one fact per card, and it is what runs out first', async () => {
    renderProfiles(two())
    for (const name of ['browser', 'claude-code']) {
      const card = await profileCard(name)
      const said = [...card.querySelectorAll('p')].map((p) => (p.textContent ?? '').trim())
      expect(said, `${name}: ${said.join(' / ')}`).toHaveLength(1)
      expect(said[0]).toContain('run out first')
      // One sentence, not a sentence and a rider.
      expect(said[0]!.split(/[.!?](\s|$)/).filter((s) => /[a-z]/i.test(s)), said[0]).toHaveLength(1)
    }
  })
})

describe('Pools draws one classification in its table and its cards (CP-14)', () => {
  /**
   * `global` is healthy. `resource:browser` was set to 0 by a person and
   * `provider:anthropic:tenant:eng` was zeroed by its quota; neither is paused
   * and neither is `ok`. `runner:claude-code` is at a positive ceiling.
   */
  const board = () =>
    capacity({
      tenant_id: 'eng',
      pools: [
        pool({ name: 'global', active: 2, available: 6 }),
        pool({ name: 'resource:browser', hard_limit: 0, effective_limit: 0, available: 0 }),
        pool({
          name: 'provider:anthropic:tenant:eng', hard_limit: 20, quota_derived_limit: 0, effective_limit: 0, available: 0,
        }),
        pool({ name: 'runner:claude-code', hard_limit: 4, effective_limit: 4, active: 4, available: 0 }),
      ],
    })

  it('the table draws limit 0 for a pool with a ceiling of 0, never ok', async () => {
    renderCapacity(board())
    await screen.findByText('Global')
    expect(chipsOf(familyRow('resource:browser'))).toEqual(['limit 0|is-paused'])
    expect(chipsOf(familyRow('provider:anthropic:tenant:eng'))).toEqual(['limit 0|is-bad'])
    expect(chipsOf(familyRow('global'))).toEqual(['ok|is-ok'])
  })

  it('the Cards view draws the ok chip for a healthy pool and limit 0 for a pool at 0', async () => {
    renderCapacity(board())
    fireEvent.click(await screen.findByRole('button', { name: 'Cards' }))
    expect(chipsOf(poolCard('global'))).toEqual(['ok|is-ok'])
    expect(chipsOf(poolCard('resource:browser'))).toEqual(['limit 0|is-paused'])
    expect(chipsOf(poolCard('provider:anthropic:tenant:eng'))).toEqual(['limit 0|is-bad'])
  })

  it('draws a full pool as warn in the chip, the row and the track', async () => {
    renderCapacity(board())
    await screen.findByText('Global')
    expect(chipsOf(familyRow('runner:claude-code'))).toEqual(['full|is-warn'])
    expect(familyRow('runner:claude-code').className).toContain('is-warn')

    fireEvent.click(screen.getByRole('button', { name: 'Cards' }))
    const tile = poolCard('runner:claude-code')
    expect(chipsOf(tile)).toEqual(['full|is-warn'])
    expect(tile.querySelector('.ctl-util-fill.is-warn'), 'the full tile’s track is not warn').not.toBeNull()
    expect(tile.querySelector('.ctl-util-fill.is-bad'), 'the full tile’s track is bad').toBeNull()
  })

  /**
   * #159 REVIEW. The tile passed `pct: null` for a ceiling of 0, and the
   * shared track draws null as the hatched NOT MEASURED picture -- beside
   * `/ 0` and a `limit 0` chip, which say the ceiling WAS read. The track
   * ignored the classification's tone as well, so the one pool on the card
   * that admits nothing was the only one whose track did not say so.
   */
  it('draws a pool at a ceiling of 0 with a measured track in its chip’s tone, never the not-measured hatch', async () => {
    renderCapacity(board())
    fireEvent.click(await screen.findByRole('button', { name: 'Cards' }))
    for (const [name, tone] of [
      ['resource:browser', 'is-paused'],
      ['provider:anthropic:tenant:eng', 'is-bad'],
    ] as const) {
      const tile = poolCard(name)
      const track = tile.querySelector('.ctl-util-track')
      expect(track, `${name} draws no track`).not.toBeNull()
      expect(track!.className, `${name}: a ceiling that was read is drawn as not measured`).not.toContain('is-unknown')
      expect(tile.querySelector(`.ctl-util-fill.${tone}`), `${name}: the track does not take its chip’s ${tone}`).not.toBeNull()
    }
    // Control: the healthy tile's track is a plain measured fill.
    expect(poolCard('global').querySelector('.ctl-util-track.is-unknown')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// #159 review of the CP-6 column (#85). Pushed before the fix it demands.
// ---------------------------------------------------------------------------

describe('Profile headroom says which remedy a +N is (CP-6, #159 review)', () => {
  /**
   * `tenant:eng` is PAUSED and refusing `claude-code`. The server models a
   * paused pool's counterfactual as RESUMING it, limit left alone
   * (`headroom.py` `_counterfactual`, `action: 'resume'`), so its +4 is what
   * resuming buys. Under a header that says `if lifted`, a bare `+4` told an
   * operator to raise a limit the row's own Status title says changes nothing.
   */
  const paused = () =>
    capacity({
      tenant_id: 'eng',
      pools: [pool({ name: 'global', active: 2, available: 6 }), pool({ name: 'tenant:eng', enabled: false })],
      runner_profiles: {
        'claude-code': profile({
          pools: ['global', 'tenant:eng'],
          admission: admission({
            headroom: 0,
            blockers: [blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', limit: 8, active: 0, group: 'needs_action' })],
            binding: ['tenant:eng'],
            counterfactual: [cf({ pool: 'tenant:eng', action: 'resume', delta: 4, headroom_after: 4, next_binding: ['global'] })],
          }),
        }),
      },
    })

  it('says resumed, in the cell’s visible text, for a paused pool', async () => {
    renderProfiles(paused())
    const cell = liftCell(await profileCard('claude-code'), 'tenant:eng')
    expect(cell.textContent).toBe('+4 if resumed')
    expect(cell.textContent, 'a resume is drawn as a lift').not.toMatch(/lift/i)
    expect(cell.getAttribute('aria-label') ?? '').toContain('Resume')
    // The raise is unchanged: `+15` on `browser`'s `global` is pinned bare in
    // "puts +N on every pool that binds" above.
  })
})

describe('Profile headroom keeps the remedy on screen (CP-6, #159 review)', () => {
  /**
   * The grouped blocker list went with the counterfactual list, which the
   * owner's decision did not ask for: the remedy -- somebody has to act, or
   * waiting clears it -- and each refusal's reason and figures were left in a
   * `title`, which a phone never shows. It is back under the one sentence, in
   * the card's own marks (CP-12), and it draws nothing when nothing refuses.
   */
  const refused = (over: Partial<ProfileAdmission> = {}) =>
    capacity({
      tenant_id: 'eng',
      pools: [
        pool({ name: 'global', active: 3, available: 5 }),
        pool({ name: 'tenant:eng', enabled: false }),
        pool({ name: 'runner:browser', hard_limit: 4, effective_limit: 4, active: 4, available: 0 }),
      ],
      runner_profiles: {
        browser: profile({
          resource_class: 'browser',
          pools: ['global', 'tenant:eng', 'runner:browser'],
          admission: admission({
            headroom: 0,
            blockers: [
              blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', limit: 8, active: 0, group: 'needs_action' }),
              blocker({ pool: 'runner:browser', reason: 'RUNNER_LIMIT', limit: 4, active: 4, group: 'no_room' }),
            ],
            binding: ['tenant:eng', 'runner:browser'],
            ...over,
          }),
        }),
      },
    })

  it('lists every refusing pool under the remedy that clears it, as visible text', async () => {
    renderProfiles(refused())
    const card = await profileCard('browser')
    const acting = card.querySelector('.blocker-group.needs-action')
    const room = card.querySelector('.blocker-group.no-room')
    expect(acting, 'the card does not say somebody has to act').not.toBeNull()
    expect(room, 'the card does not say waiting clears it').not.toBeNull()
    expect(acting!.textContent).toContain('Somebody has to act')
    expect(acting!.textContent).toContain('MANUAL_PAUSE')
    expect(room!.textContent).toContain('Eligible, no room')
    expect(room!.textContent).toContain('4 of 4 units in use')
  })

  it('draws the list in the card’s own marks, so no .tag comes back (CP-12)', async () => {
    renderProfiles(refused())
    const card = await profileCard('browser')
    expect(card.querySelector('.tag'), 'the blocker list brought a .tag back').toBeNull()
    expect(card.querySelector('.blocker-group.needs-action .ctl-chip.is-paused')?.textContent).toContain('paused')
    expect(card.querySelector('.blocker-group.no-room .ctl-chip.is-warn')?.textContent).toContain('full')
  })

  it('says the list is incomplete, above it, when a pool was not read', async () => {
    renderProfiles(refused({ headroom: null, basis: 'unknown', complete: false, unread: ['global'], binding: [] }))
    const card = await profileCard('browser')
    const note = card.querySelector('[role="status"]')
    expect(note, 'a partial blocker list is drawn as a whole one').not.toBeNull()
    expect(note!.textContent).toContain('incomplete')
    expect(note!.textContent).toMatch(/global/i)
    const list = card.querySelector('.blocker-group')
    expect(list).not.toBeNull()
    expect(note!.compareDocumentPosition(list!) & Node.DOCUMENT_POSITION_FOLLOWING, 'the banner is not above the list').toBeTruthy()
  })

  it('draws no list, and no "nothing is refusing" sentence, when no pool refuses', async () => {
    renderProfiles(refused({ headroom: 5, blockers: [], binding: ['global'] }))
    const card = await profileCard('browser')
    expect(card.querySelector('.blocker-group, .blocker-list')).toBeNull()
    expect(card.textContent).not.toContain('No pool is refusing')
  })
})

// ---------------------------------------------------------------------------
// Runtimes (CP-16, CP-17)
// ---------------------------------------------------------------------------

function rt(over: Partial<Runtime>): Runtime {
  return {
    name: 'claude-code',
    available: true,
    disabled_reason: '',
    image: 'img-claude-code',
    backend: 'cloudrun',
    resolved_backend: 'cloudrun',
    provider: 'anthropic',
    secrets: ['CLAUDE_CODE_OAUTH_TOKEN'],
    secrets_any_of: false,
    timeout_seconds: 3600,
    resource_class: 'standard',
    resources: { name: 'standard', cpu: 2, memory_gib: 4, disk_gib: 1, units: 1 },
    ...over,
  }
}

function renderRuntimes(list: Runtime[]) {
  loadRuntimeTopology.mockResolvedValue({
    status: 'ok',
    data: {
      runtimes: Object.fromEntries(list.map((r) => [r.name, r])),
      pools: [],
      poolsDetail: null,
      classes: null,
      classesDetail: null,
    },
    fetchedAt: Date.now(),
    serverAt: '2026-09-22T10:00:00Z',
  })
  return render(<RuntimesScreen />)
}

async function runtimeCard(name: string): Promise<HTMLElement> {
  const title = await screen.findByText(name, { selector: '.ctl-card-title .id' })
  return title.closest('.ctl-card') as HTMLElement
}

function keysOf(list: Element | null): string[] {
  if (!list) return []
  return [...list.querySelectorAll(':scope > .ctl-fact > b')].map((b) => (b.textContent ?? '').trim())
}

describe('Sets it apart lists only what nothing else shares (CP-16)', () => {
  const catalogue = () => [
    rt({ name: 'mock', image: 'img-mock', provider: null, secrets: [] }),
    rt({ name: 'mock-slow', image: 'img-mock-slow', provider: null, secrets: [] }),
    rt({ name: 'browser', image: 'img-browser', backend: 'gke', resolved_backend: 'gke' }),
    rt({ name: 'claude-code' }),
  ]

  it('files every distinction under a key the card itself uses', async () => {
    renderRuntimes(catalogue())
    for (const name of ['mock', 'mock-slow', 'browser', 'claude-code']) {
      const card = await runtimeCard(name)
      const own = new Set(keysOf(card.querySelector('.ctl-card-body > .ctl-facts')))
      expect(own.size, `${name}: the card drew no facts`).toBeGreaterThan(0)
      for (const key of keysOf(card.querySelector('.rt-apart-facts'))) {
        expect(own.has(key), `${name}: "${key}" is not a key on its own card (${[...own].join(', ')})`).toBe(true)
      }
    }
  })

  it('does not call a fact shared with another runtime a distinction', async () => {
    renderRuntimes(catalogue())
    // `mock` shares its backend with two others and its absent provider with
    // `mock-slow`. Only its image is its own.
    expect(keysOf((await runtimeCard('mock')).querySelector('.rt-apart-facts'))).toEqual(['image'])
    // `browser` is the only one on gke, which IS a distinction.
    expect(keysOf((await runtimeCard('browser')).querySelector('.rt-apart-facts'))).toContain('runs on')
  })
})

describe('a disabled runtime shows why (CP-17)', () => {
  it('prints the reason on the card, not only in an accessible name', async () => {
    const reason = 'codex is disabled on this platform. The provider refused the registered credential.'
    renderRuntimes([
      rt({}),
      rt({ name: 'codex', image: 'img-codex', provider: 'openai', available: false, disabled_reason: reason }),
    ])
    const card = await runtimeCard('codex')
    // `textContent` holds what is drawn; an aria-label is not in it.
    expect(card.textContent).toContain(reason)
    expect(card.querySelector('.ctl-chip.is-bad')?.textContent).toContain('disabled')
  })
})
