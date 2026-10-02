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
import type { Capacity, Pool, ProfileAdmission, Runtime, RunnerProfile } from '../types'
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

function renderCapacity(data: Capacity) {
  loadCapacity.mockResolvedValue({ status: 'ok', data, fetchedAt: Date.now(), serverAt: data.generated_at })
  return render(<CapacityScreen />)
}

// ---------------------------------------------------------------------------
// THE PER-PROFILE PANELS ARE GONE (owner's decision, 2026-10-01). Pools'
// `Could start / Held back by` table and Profiles' per-profile cards were
// removed with the rebrand, and with them every test of their figures, the
// blocker list grouped by remedy, the incomplete-read banner and the `+N if
// lifted` column. What is left here is what the surviving screens draw: the
// Pools ceiling tables and the Profiles matrix.
// ---------------------------------------------------------------------------

describe('the capacity board as a whole', () => {
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

  // The Cards view was the alternative set aside on 2026-10-01 (capacity.html);
  // its scope test went with it. The Scope column above is the one place the
  // scope is drawn, on every row, including the "Needs action" group's. The
  // group holds the pools a person has to act on (owner decision 2026-10-01,
  // `poolGroup`), so the pool here is paused; a merely full pool stays in its
  // family.
  it('declares scope on a row the "Needs action" group took out of its family', async () => {
    renderCapacity(
      capacity({
        tenant_id: 'eng',
        pools: [pool({ name: 'global' }), pool({ name: 'tenant:research', enabled: false })],
      }),
    )
    await screen.findByText('Global')
    expect(familyRow('tenant:research').closest('.cap-needs'), 'a paused pool is not in Needs action').not.toBeNull()
    expect(scopeCell('tenant:research')).toBe('tenant research')
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

function renderProfiles(data: Capacity) {
  loadCapacity.mockResolvedValue({ status: 'ok', data, fetchedAt: Date.now(), serverAt: data.generated_at })
  return render(<ProfilesScreen />)
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


/** The state marks a Pools row or tile draws, as `word|modifier` pairs. */
function chipsOf(el: Element): string[] {
  return [...el.querySelectorAll('.ctl-chip')].map((c) => {
    const mod = [...c.classList].find((k) => k.startsWith('is-')) ?? '(none)'
    return `${(c.textContent ?? '').trim()}|${mod}`
  })
}

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

  // RE-POINTED 2026-10-01: the Pools headroom row and the Profile headroom
  // card that drew this are gone; the Profiles matrix is the surviving place a
  // profile's figure is drawn, and it must not advertise a disabled one.
  it('the Profiles matrix draws disabled where the figure would be, with no counterfactual', async () => {
    renderProfiles(capacity({ runner_profiles: { codex: codex() } }))
    const toggle = (await screen.findByText('codex', { selector: '.cap-mx-toggle .mono' })).closest('button')!
    const row = toggle.closest('tr')!
    const start = row.querySelector('td[data-label="Can start"]')!
    expect(start.textContent).toContain('disabled')
    expect(start.textContent, 'a disabled profile still shows a headroom figure').not.toMatch(/\d/)
    expect(row.querySelector('td[data-label="Runs out first"]')!.textContent, 'a disabled profile is priced').toBe('')
    // The reason is text a reader can see, once the row is opened.
    fireEvent.click(toggle)
    expect(document.querySelector('.cap-mx-exp')!.textContent).toContain(REASON)
  })

  it('an available profile is still priced as before', async () => {
    renderProfiles(capacity({ runner_profiles: { 'claude-code': profile({ available: true, admission: admission({ headroom: 3 }) }) } }))
    const toggle = (await screen.findByText('claude-code', { selector: '.cap-mx-toggle .mono' })).closest('button')!
    const start = toggle.closest('tr')!.querySelector('td[data-label="Can start"]')!
    expect(start.textContent).toBe('3')
    expect(start.textContent).not.toContain('disabled')
  })
})

describe('units are named on every column that counts them (CP-24)', () => {
  it('Pools: Leased and Ceiling both carry (units), stacked keys too', async () => {
    renderCapacity(
      capacity({ pools: [pool({ name: 'global' })] }),
    )
    await screen.findByText('Global')
    const heads = [...document.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
    // `Weight` was the removed headroom table's column; the ceiling tables
    // count Leased and Ceiling.
    for (const name of ['Leased', 'Ceiling']) {
      const found = heads.filter((h) => h.startsWith(name))
      expect(found.length, `no ${name} column`).toBeGreaterThan(0)
      for (const h of found) expect(h, `${name} does not say its unit`).toContain('(units)')
    }
    const keyed = [
      ...document.querySelectorAll('td[data-label^="Leased"], td[data-label^="Ceiling"]'),
    ]
    expect(keyed.length).toBeGreaterThan(0)
    for (const td of keyed) expect(td.getAttribute('data-label')).toContain('(units)')
  })
})

describe('Pools draws one classification in its chip, its row and its track (CP-14)', () => {
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

  it('draws a full pool as warn in the chip, the row and the track', async () => {
    renderCapacity(board())
    await screen.findByText('Global')
    expect(chipsOf(familyRow('runner:claude-code'))).toEqual(['full|is-warn'])
    expect(familyRow('runner:claude-code').className).toContain('is-warn')

    // The Use column's track, which the Cards view used to carry.
    const row = familyRow('runner:claude-code')
    expect(row.querySelector('.ctl-util-fill.is-warn'), 'the full row’s track is not warn').not.toBeNull()
    expect(row.querySelector('.ctl-util-fill.is-bad'), 'the full row’s track is bad').toBeNull()
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
    await screen.findByText('Global')
    for (const [name, tone] of [
      ['resource:browser', 'is-paused'],
      ['provider:anthropic:tenant:eng', 'is-bad'],
    ] as const) {
      const tile = familyRow(name)
      const track = tile.querySelector('.ctl-util-track')
      expect(track, `${name} draws no track`).not.toBeNull()
      expect(track!.className, `${name}: a ceiling that was read is drawn as not measured`).not.toContain('is-unknown')
      expect(tile.querySelector(`.ctl-util-fill.${tone}`), `${name}: the track does not take its chip’s ${tone}`).not.toBeNull()
    }
    // Control: the healthy tile's track is a plain measured fill.
    expect(familyRow('global').querySelector('.ctl-util-track.is-unknown')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// #159 review of the CP-6 column (#85). Pushed before the fix it demands.
// ---------------------------------------------------------------------------

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
