// VISUAL QA RE-RUN, LANE VQA2-R16 (2026-10-27, part of #1427): the capacity
// boards -- Runtimes' cards and Sizing, and the By runner profile matrix.
// Each case asserts the PROPERTY the finding was about -- the rule the
// cascade picks for the element the screen draws -- not the markup's shape.
//
//   V162  A long tenant id in "Pools it must clear" is cut inside its chip,
//         and the chip row is held to the card, instead of running past it.
//   V194  Sizing › "Resolves from" draws each runtime as its own unbroken
//         span, so `demo-scribe` never breaks at its hyphen.
//   V090  Every "runs out first" outline is ONE width, not a 5ch minimum a
//         six-character figure grows past.
//   V195  A Runs out first entry holds one line at one width: its pool name
//         is cut, never its `(5/5) fits 0`.
//   V091  A family's sticky head sits under the bell bar (`--ctl-bar-h`), not
//         at the page's top edge where the bar covers it.
//   FIT   (review of U01, 2026-10-27) The box and entry widths V090/V195 fix
//         keep the matrix inside its 1039px scroller at 1440: at 9ch and 34ch
//         the review measured the table at 1247px, Runs out first off-screen.
//
// BREAK IT: drop `max-width: 100%` from `.rt-pool-chips` -- V162 fails; put
// `users.join(', ')` back in one span -- V194 fails; `min-width: 5ch` for the
// box's width -- V090 fails; `white-space: normal` on `.cap-mx-bind` -- V195
// fails; `top: 0` on the family head -- V091 fails; `inline-size: 9ch` on the
// box or `34ch` on the entry -- FIT fails.

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Capacity, Pool, ProfileAdmission, RunnerProfile, Runtime } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn(), loadCapacity: vi.fn(), loadAdminPools: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen } = await import('../Runtimes')
const { ProfileMatrix } = await import('../ProfileMatrix')

const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const
const LONG_TENANT = 'data-platform-ingestion-team-europe-west-' + 'x'.repeat(24)
const hosts: HTMLElement[] = []

afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-27T12:00:00Z' }
}

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-27T12:00:00Z',
  }
}

function runtime(name: string): Runtime {
  return {
    name,
    available: true,
    disabled_reason: '',
    image: `img-${name}`,
    backend: 'CLOUD_RUN_JOB',
    resolved_backend: 'CLOUD_RUN_JOB',
    provider: null,
    secrets: [],
    secrets_any_of: false,
    timeout_seconds: 600,
    resource_class: 'standard',
    resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
  } as Runtime
}

function topology(): RuntimeTopology {
  const tenant = `tenant:${LONG_TENANT}`
  return {
    runtimes: { 'demo-scribe': runtime('demo-scribe'), 'demo-mock': runtime('demo-mock') },
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [pool('global', 1, 40), pool(tenant, 1, 10), pool('runner:demo-mock', 0, 4)],
    poolsDetail: null,
    profilePools: { 'demo-scribe': ['global', tenant], 'demo-mock': ['global', tenant, 'runner:demo-mock'] },
    profileUncapped: { 'demo-scribe': [], 'demo-mock': [] },
  } as unknown as RuntimeTopology
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 1, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: null, units: 1, pools: [], ...over }
}

/** A matrix whose figures run from one digit to six characters, and whose binding pool is a long tenant. */
const CAPACITY: Capacity = {
  tenant_id: 'eng',
  generated_at: '2026-10-27T12:00:00Z',
  pools: [pool('global', 120000, 125000), pool(`tenant:${LONG_TENANT}`, 5, 5), pool('runner:codex', 1, 4)],
  runner_profiles: {
    ['p'.repeat(70)]: profile({
      pools: ['global', `tenant:${LONG_TENANT}`, 'runner:codex'],
      admission: admission({ headroom: 0, binding: [`tenant:${LONG_TENANT}`, 'global'] }),
    }),
    codex: profile({
      pools: ['global', 'runner:codex'],
      admission: admission({ headroom: 3, binding: ['runner:codex'] }),
    }),
  },
}

async function runtimes(): Promise<void> {
  api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
  render(<RuntimesScreen />)
  await screen.findByText('Sizing', undefined, WAIT)
}

describe('V162: a long tenant id is cut inside its chip, held to the card', () => {
  it('holds the chip row to the card and cuts the id inside each chip, whole in its title', async () => {
    await runtimes()
    const rows = [...document.querySelectorAll<HTMLElement>('.rt-pools > .rt-pool-chips')]
    expect(rows.length, 'no "Pools it must clear" row drew').toBe(2)
    let cut = 0
    for (const env of [PHONE, WIDE]) {
      for (const row of rows) {
        expect(painted(row, 'max-width', env), 'the chip row is not held to its card').toBe('100%')
        expect(painted(row, 'min-width', env)).toBe('0')
        for (const chip of row.querySelectorAll<HTMLElement>(':scope > .c-chip')) {
          expect(painted(chip, 'min-width', env), 'a chip cannot shrink below its id').toBe('0')
          expect(painted(chip, 'max-width', env)).toBe('100%')
          const id = chip.querySelector<HTMLElement>(':scope > .rt-chip-id')
          expect(id, `chip ${chip.textContent} draws its id in no cuttable span`).not.toBeNull()
          expect(painted(id!, 'overflow', env)).toBe('hidden')
          expect(painted(id!, 'text-overflow', env)).toBe('ellipsis')
          expect(painted(id!, 'white-space', env)).toBe('nowrap')
          expect(painted(id!, 'min-width', env)).toBe('0')
          // Cut on screen, whole where a pointer can read it.
          expect(chip.getAttribute('title') ?? id!.getAttribute('title')).toContain(chip.textContent ?? '?')
          if ((chip.textContent ?? '').includes(LONG_TENANT)) cut++
        }
      }
    }
    expect(cut, 'the long tenant chip was not visited on both cards at both widths').toBe(4)
  })
})

describe('V194: "Resolves from" draws each runtime as its own unbroken span', () => {
  it('gives every runtime a nowrap span, its comma inside, at 400 and 1440', async () => {
    await runtimes()
    const cell = document.querySelector<HTMLElement>('.rt-sizing td[data-label="Resolves from"]')
    expect(cell, 'Sizing drew no Resolves from cell').not.toBeNull()
    const ids = [...cell!.querySelectorAll<HTMLElement>('.rt-resolves > .rt-resolves-id')]
    expect(ids.map((s) => s.textContent)).toEqual(['demo-mock,', 'demo-scribe'])
    expect(cell!.textContent).toBe('demo-mock, demo-scribe')
    for (const env of [PHONE, WIDE]) {
      for (const id of ids) {
        expect(painted(id, 'white-space', env), `${id.textContent} may break inside itself`).toBe('nowrap')
      }
    }
  })
})

describe('V090: every "runs out first" outline is one width', () => {
  it('fixes the figures box to one width in every kind of cell, not a minimum a figure grows past', () => {
    render(<ProfileMatrix capacity={CAPACITY} />)
    const boxes = [...document.querySelectorAll<HTMLElement>('.cap-mx td > .cap-mx-v')]
    expect(boxes.length).toBeGreaterThanOrEqual(4)
    expect(document.querySelectorAll('.cap-mx td.is-binding > .cap-mx-v').length).toBe(3)
    for (const box of boxes) {
      expect(painted(box, 'inline-size', WIDE), `${box.textContent}: the box has no fixed width`).toBe('5ch')
      expect(painted(box, 'box-sizing', WIDE), 'the padding would eat a border-box width').toBe('content-box')
      expect(painted(box, 'min-width', WIDE)).toBe('0')
      // A worded figure (`no limit set`) wraps inside the box rather than widening it.
      expect(painted(box, 'white-space', WIDE)).toBe('normal')
    }
  })
})

describe('V195: a Runs out first entry holds one line at one width', () => {
  it('cuts the pool name and never wraps its use and fits, beside a 70-character profile name', () => {
    render(<ProfileMatrix capacity={CAPACITY} />)
    const entries = [...document.querySelectorAll<HTMLElement>('.cap-mx td.cap-mx-first > .cap-mx-bind')]
    expect(entries.length).toBe(3)
    for (const env of [PHONE, WIDE]) {
      for (const e of entries) {
        expect(painted(e, 'white-space', env), `${e.textContent} wraps inside itself`).toBe('nowrap')
        expect(painted(e, 'display', env)).toBe('flex')
        expect(painted(e, 'inline-size', env), 'entries are not one width').toBe('21ch')
        expect(painted(e, 'max-inline-size', env)).toBe('100%')
        const name = e.querySelector<HTMLElement>(':scope > .cap-mx-bind-pool')
        expect(name, `${e.textContent}: the pool name is in no cuttable span`).not.toBeNull()
        expect(painted(name!, 'min-width', env)).toBe('0')
        expect(painted(name!, 'overflow', env)).toBe('hidden')
        expect(painted(name!, 'text-overflow', env)).toBe('ellipsis')
        for (const tail of e.querySelectorAll<HTMLElement>(':scope > .cap-mx-bind-use, :scope > small')) {
          expect(painted(tail, 'flex-shrink', env), `${tail.textContent} gives way to the name`).toBe('0')
        }
      }
    }
    // The words did not change: the name, its use and its fits, in order.
    const tenant = entries.find((e) => e.dataset.pool === `tenant:${LONG_TENANT}`)
    expect(tenant?.textContent).toMatch(/\(5\/5\) fits 0$/)
  })
})

describe('V091: a sticky family head sits under the bell bar', () => {
  const head = (frame: string, path: string) => build(`${frame} ${path} > table > thead`, hosts)

  it('offsets a family\'s head by --ctl-bar-h, 48px under a bar and 0 without', () => {
    const path = 'div.cap-families div.ctl-table.is-scroll'
    expect(painted(head('div.ctl-frame.has-ctl-bar', path), 'top', WIDE), 'the family head slides under the bar').toBe('48px')
    expect(painted(head('div.ctl-frame', path), 'top', WIDE)).toBe('0px')
  })

  it('keeps the matrix head at its own scroller\'s top, and fits that scroller below the bar', () => {
    const matrix = 'div.cap-mx > div.ctl-table.is-scroll'
    // The matrix is its own vertical scroller: its head sticks to ITS top,
    // and an offset there would open a gap its rows show through.
    expect(painted(head('div.ctl-frame.has-ctl-bar', matrix), 'top', WIDE)).toBe('0')
    const box = build(`div.ctl-frame.has-ctl-bar ${matrix}`, hosts)
    expect(painted(box, 'max-height', WIDE), 'the matrix is taller than the room under the bar').toBe(
      'calc(100vh - 180px - 48px)',
    )
  })
})

describe('FIT: the matrix stays inside its scroller at 1440 (review of U01, 2026-10-27)', () => {
  // jsdom lays nothing out, so this is the review's live measurement turned
  // into arithmetic over what the sheet sets. Measured in the live browser at
  // 1440 with the 10-27 sheet: the scroller is 1039px; the table was 1247px,
  // its Runs out first column 276px (a 34ch entry and the cell's 2 x 10px
  // padding) and every figure box 92px (9ch of content and 2 x 8px padding).
  // So a `ch` is 76/9 px in a figure cell (DM Mono at --t-body) and 256/34 px
  // in an entry (Inter Tight at --t-meta). A family column is as wide as the
  // wider of its box and its one-line head (the head never wraps: the sticky
  // head is one --row-h line, capacity-007); a head's width is estimated at
  // 6.6px a character, Inter Tight 600 at --t-meta. The fixture draws all six
  // families, so what the profile and Can start columns took is the rest of
  // the measured 1247 under the old widths, and it does not change here.
  const SCROLLER = 1039
  const MEASURED_TABLE = 1247
  const OLD = { box: 92, entry: 256 }
  const MONO_CH = 76 / 9
  const SANS_CH = 256 / 34
  const CELL_PAD = 20
  const BOX_PAD = 16
  const HEADS = ['Global', 'Workspaces', 'Resource classes', 'Runner profiles', 'Backends', 'Providers']
  const head = (title: string) => title.length * 6.6
  const families = (box: number) => HEADS.reduce((sum, t) => sum + Math.max(head(t), box) + CELL_PAD, 0)
  const REST = MEASURED_TABLE - (OLD.entry + CELL_PAD) - families(OLD.box)

  const ch = (v: string | null | undefined) => {
    const m = /^([\d.]+)ch$/.exec(v ?? '')
    expect(m, `${v} is not a width in ch`).not.toBeNull()
    return Number(m![1])
  }

  it('sums the six family columns, the Runs out first entry and the rest to no more than the scroller', () => {
    render(<ProfileMatrix capacity={CAPACITY} />)
    const box = document.querySelector<HTMLElement>('.cap-mx td > .cap-mx-v')!
    const entry = document.querySelector<HTMLElement>('.cap-mx td.cap-mx-first > .cap-mx-bind')!
    expect(painted(box, 'box-sizing', WIDE)).toBe('content-box')
    const boxPx = ch(painted(box, 'inline-size', WIDE)) * MONO_CH + BOX_PAD
    const entryPx = ch(painted(entry, 'inline-size', WIDE)) * SANS_CH
    const table = REST + families(boxPx) + entryPx + CELL_PAD
    expect(Math.round(table), `the matrix is ${Math.round(table)}px in a ${SCROLLER}px scroller`).toBeLessThanOrEqual(SCROLLER)
    // The control: the 10-27 widths reproduce the review's measurement.
    expect(Math.round(REST + families(OLD.box) + OLD.entry + CELL_PAD)).toBe(MEASURED_TABLE)
  })
})
