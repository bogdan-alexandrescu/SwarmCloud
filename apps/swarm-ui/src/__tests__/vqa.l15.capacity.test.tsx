// VISUAL QA, LANE VQA-L15 (2026-10-11, part of #1038): the capacity boards and
// the shared stacked-record mode. Each case asserts the PROPERTY the finding
// was about -- the rule the cascade picks for a rendered element, the words on
// screen, the order of a card's facts -- not the markup's shape.
//
//   V011  A stacked record's values start at one x: numeric cells too.
//   V012  A stacked capacity field is one grid item: no bare text beside an element.
//   V030  Runtimes' `not read` pools mark is as wide as its words.
//   V031  A long profile name is cut in the matrix, and Runs out first may wrap.
//   V032  Runtimes' Backends and Sizing are a record per row on a phone.
//   V033  The phone matrix's note names what it draws; no Runs out first is blank.
//   V085  The key column holds `Ceiling (units)` (and every capacity key).
//   V086  One key width in a mono cell and a sans cell; Use is the track and its % (run 3).
//   V087  The disabled runtime's reason is spaced from its facts.
//   V088  The image fact spans the facts grid, after the short facts; a row's cards share its height.
//   V089  Set by is wide enough for its words (`AIMD back-off` then, `lowered
//         automatically after errors` since QA1B-13, on two lines).
//   V090  The binding outline is on a box of one minimum width, not the cell.
//   V091  Pools' and the matrix's column heads stick above 900px.
//   V092  Runtimes re-reads on Pools' cadence, so its refresh says `every 30 s`.
//   V093  The open matrix row and its detail are one tint, with no seam on a phone.
//   V094  Why the holders links carry no count is said on the page.
//
// BREAK IT: put `13ch` back as the stacked key column -- V085/V086 fail; drop
// the `td.is-num, td.n { text-align: left }` rule -- V011 fails; unwrap
// `.cap-open` -- V012 fails; drop `pollMs` from RuntimesScreen -- V092 fails.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CAPACITY_CSS from '../styles/capacity.css?raw'
import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Capacity, LeasePage, Pool, Runtime, RunnerProfile } from '../types'
import { declarations, flatRules, type CascadeEnv } from './cssgate'
import { TABLES, build, painted } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadLeases: vi.fn(),
  loadRuntimeTopology: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { ProfileMatrix } = await import('../ProfileMatrix')
const { RuntimesScreen } = await import('../Runtimes')

const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
})

/** `usePhoneTables` reads `(max-width: 560px)`; this makes it true. */
function atPhone(): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(max-width: 560px)',
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }))
}

function ok<T>(data: T): Result<T> {
  // The refresh age is measured from `serverAt`, so it is stamped now: a fixed
  // instant made V092's `read N s ago` true only within a minute of it.
  const now = Date.now()
  return { status: 'ok', data, fetchedAt: now, serverAt: new Date(now).toISOString() }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-11T10:00:00Z', ...over,
  }
}

function emptyLeases(): LeasePage {
  return {
    leases: [],
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: '2026-10-11T10:00:00Z',
    active_only: true,
    tenant_id: null,
    units_held: 0,
    truncated: false,
    active_beyond_window: 0,
  } as unknown as LeasePage
}

function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-11T10:00:00Z',
    pools: [
      pool('global', 3, 40),
      // Lowered by AIMD: `6` and `/10`, the two-part ceiling of V012.
      pool('tenant:eng', 0, 6, { hard_limit: 10, adaptive_target: 6 }),
      pool('runner:codex', 10, 10),
    ],
    runner_profiles: {},
  }
}

async function renderPools(leases: Result<LeasePage> = ok(emptyLeases())): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(board()))
  api.loadLeases.mockResolvedValue(leases)
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

function profile(over: Partial<RunnerProfile> = {}): RunnerProfile {
  return {
    backend: 'CLOUD_RUN_JOB',
    resource_class: 'standard',
    units: 1,
    pools: ['global', 'tenant:eng'],
    headroom: 5,
    available: true,
    disabled_reason: '',
    ...over,
  } as unknown as RunnerProfile
}

function matrix(): Capacity {
  return {
    ...board(),
    runner_profiles: {
      alpha: profile({ admission: { headroom: 6, basis: 'measured', complete: true, binding: ['tenant:eng'], blockers: [], unread: [], uncapped: [], counterfactual: [] } } as unknown as Partial<RunnerProfile>),
      off: profile({ available: false, disabled_reason: 'off is disabled on this platform.' }),
    },
  }
}

function runtime(name: string, over: Partial<Runtime> = {}): Runtime {
  return {
    name,
    available: true,
    disabled_reason: '',
    image: 'agent-runtime-base',
    backend: 'CLOUD_RUN_JOB',
    resolved_backend: 'CLOUD_RUN_JOB',
    provider: null,
    secrets: [],
    secrets_any_of: false,
    timeout_seconds: 600,
    resource_class: 'standard',
    resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
    ...over,
  } as Runtime
}

function topology(over: Partial<RuntimeTopology> = {}): RuntimeTopology {
  return {
    runtimes: {
      alpha: runtime('alpha'),
      beta: runtime('beta', { available: false, disabled_reason: 'beta is switched off; use alpha.' }),
    },
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [pool('backend:CLOUD_RUN_JOB', 2, 20)],
    poolsDetail: null,
    profilePools: { alpha: ['global'], beta: ['global'] },
    profileUncapped: { alpha: [], beta: [] },
    ...over,
  } as unknown as RuntimeTopology
}

async function renderRuntimes(data: RuntimeTopology = topology()): Promise<void> {
  api.loadRuntimeTopology.mockResolvedValue(ok(data))
  render(<RuntimesScreen />)
  await screen.findByText('Sizing', undefined, WAIT)
}

/** The px width of a stacked field's key column, read through the cascade. */
function keyPx(td: Element): number {
  const template = painted(td, 'grid-template-columns', PHONE) ?? ''
  expect(template, 'the key column is measured in the cell\'s own `ch` again').not.toMatch(/\d\s*ch\b/)
  const m = /calc\(var\(--t-micro\) \* ([\d.]+)\)/.exec(template)
  expect(m, `the key column is not a multiple of the key face: ${template}`).not.toBeNull()
  return Number(m![1]) * parseFloat(TABLES.dark.get('--t-micro')!)
}

/**
 * The width a key needs: DM Mono advances 0.6em, the key is `--t-micro`. The
 * key is sans since QA-044 (BGD, 2026-10-23) and its column kept the mono
 * measure, which is an upper bound for the same key in DM Sans.
 */
function keyNeeds(label: string): number {
  return label.length * 0.6 * parseFloat(TABLES.dark.get('--t-micro')!)
}

// ---------------------------------------------------------------------------
// The stacked record (styles.css §B6.3)
// ---------------------------------------------------------------------------

describe('V011, V085, V086: a stacked record has one key width and one value edge', () => {
  it('left-aligns every value, numeric or not, on Pools and in a `table.pools` record', async () => {
    atPhone()
    await renderPools()
    const cells = [...document.querySelectorAll('.cap-pools.is-stacked td[data-label]')]
    expect(cells.length).toBeGreaterThan(10)
    for (const td of cells) {
      expect(painted(td, 'text-align', PHONE), `the value of "${td.getAttribute('data-label')}"`).toBe('left')
    }
    // Tenants' Enforced / Configured are `td.n` in a `table.pools`.
    const host = build('div.table-wrap.is-stacked > table.pools > tbody > tr > td.n', hosts)
    expect(painted(host, 'text-align', PHONE)).toBe('left')
    // The control: above the phone a figure is still right-aligned in its column.
    expect(painted(cells.find((c) => c.classList.contains('is-num'))!, 'text-align', WIDE)).toBe('right')
  })

  it('gives a mono cell and a sans cell the same key column, wide enough for every capacity key', async () => {
    atPhone()
    await renderPools()
    const cells = [...document.querySelectorAll('.cap-pools.is-stacked td[data-label]')]
    const widths = new Set(cells.map(keyPx))
    expect(widths.size, 'the value column starts at two x positions in one record').toBe(1)
    const px = [...widths][0]!
    // Every key the capacity and admin records draw, Sizing's apart (below).
    const keys = ['Ceiling (units)', 'Leased (units)', 'Units (weighted)', 'Quota cap (units)', '429s (this run)', 'Runs out first', 'In use (units)', 'Resolves from']
    for (const k of keys) expect(keyNeeds(k), `"${k}" wraps in its key column`).toBeLessThanOrEqual(px)
  })

  it('draws Use as the track and its figure on a phone, as at 1440 (capacity-012, run 3)', () => {
    // V086 made it a block because the track was hidden and its empty grid
    // track left the figure 8px short. The track is drawn on a phone now, so
    // the grid's gap is the space between the bar and its figure again.
    const use = build('div.ctl-table.cap-pools.is-stacked > table > tbody > tr > td > span.cap-use', hosts)
    expect(painted(use, 'display', PHONE)).toBe('grid')
    expect(painted(use, 'display', WIDE)).toBe('grid')
  })
})

describe('V012: a stacked capacity field is ONE grid item', () => {
  it('wraps every multi-part value in an element, so no text node is auto-placed into a keyless row', async () => {
    atPhone()
    await renderPools()
    const cells = [...document.querySelectorAll('.cap-pools.is-stacked td[data-label]')]
    for (const td of cells) {
      if (td.children.length === 0) continue
      const loose = [...td.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() !== '')
      expect(loose.map((n) => n.textContent), `"${td.getAttribute('data-label')}" has bare text beside an element`).toEqual([])
    }
    const row = document.getElementById('pool-tenant:eng')!
    const ceiling = row.querySelector('td[data-label="Ceiling (units)"]')!
    expect(ceiling.children.length, 'the figure and its `/10` are two grid items').toBe(1)
    expect((ceiling.textContent ?? '').replace(/\s+/g, '')).toBe('6/10')
    expect(row.querySelector('td[data-label="Links"]')!.children.length).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// Pools
// ---------------------------------------------------------------------------

describe('V089: Set by has the room for its words', () => {
  it('is 11% and the seven widths still add to 100%', () => {
    const width = (cls: string) => parseFloat(painted(build(`table > colgroup > col.${cls}`, hosts), 'width', WIDE) ?? 'NaN')
    expect(width('cap-c-by')).toBeGreaterThanOrEqual(11)
    const sum = width('cap-c-pool') + 2 * width('cap-c-num') + width('cap-c-use') + width('cap-c-state') + width('cap-c-by') + width('cap-c-links')
    expect(sum).toBe(100)
  })
})

describe('V091: the column heads stay in view down a long table above 900px', () => {
  it('sticks a family table\'s head, in a wrapper that clips rather than scrolls', () => {
    const head = build('div.cap-families > div.ctl-table.is-scroll > table > thead', hosts)
    const wrap = head.parentElement!.parentElement!
    expect(painted(head, 'position', WIDE)).toBe('sticky')
    expect(painted(head, 'top', WIDE)).toBe('0')
    // A wrapper that scrolls on either axis is the head's scroll container and
    // never scrolls vertically (WF-21), so the head would stick to nothing.
    expect(painted(wrap, ['overflow', 'overflow-x'], WIDE)).toBe('clip')
    // Below 900px the families scroll sideways with a held column: not sticky.
    expect(painted(head, 'position', { width: 800 })).not.toBe('sticky')
  })

  it('sticks the matrix head inside the matrix\'s own scroller, which keeps its sideways scroll', () => {
    const head = build('div.cap-mx > div.ctl-table.is-scroll > table > thead', hosts)
    const wrap = head.parentElement!.parentElement!
    expect(painted(head, 'position', WIDE)).toBe('sticky')
    expect(painted(wrap, ['overflow', 'overflow-y'], WIDE)).toBe('auto')
    expect(painted(wrap, 'max-height', WIDE)).not.toBeNull()
  })
})

describe('V094: why no holders link carries a count is said on the page', () => {
  it('draws the reason once, with the mark, and each link is described by it', async () => {
    await renderPools({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 500, code: null, message: 'the lease read timed out' },
    })
    const note = document.querySelector('.cap-holders-note')
    expect(note, 'the reason is only in a title').not.toBeNull()
    expect(note!.textContent).toContain('the lease read timed out')
    expect(note!.querySelector('.ctl-mark.is-unread')?.textContent).toBe('not counted')
    expect(document.querySelectorAll('.cap-holders-note')).toHaveLength(1)
    const links = [...document.querySelectorAll('.cap-links a')].filter((a) => a.textContent === 'holders')
    expect(links.length).toBe(3)
    for (const a of links) expect(a.getAttribute('aria-describedby')).toBe(note!.id)
  })

  it('draws no such line when every live lease was counted', async () => {
    await renderPools()
    expect(document.querySelector('.cap-holders-note')).toBeNull()
    expect(document.querySelector('.cap-links a')!.textContent).toBe('0 holders')
  })
})

// ---------------------------------------------------------------------------
// By runner profile
// ---------------------------------------------------------------------------

describe('V031: one long profile name does not widen the matrix', () => {
  it('holds the toggle to a width, cuts the name with it whole in the title, and lets Runs out first wrap', () => {
    render(<ProfileMatrix capacity={matrix()} />)
    const toggle = document.querySelector<HTMLButtonElement>('tr[data-profile="alpha"] .cap-mx-toggle')!
    expect(toggle.getAttribute('title')).toBe('alpha')
    expect(painted(toggle, 'max-width', WIDE)).toBe('30ch')
    const name = toggle.querySelector('.mono')!
    expect(painted(name, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(name, 'min-width', WIDE)).toBe('0')
    // `.ctl-table td`'s nowrap out-ranked `.cap-mx-first` and the answer
    // column could never give back width.
    const first = document.querySelector('tr[data-profile="alpha"] td.cap-mx-first')!
    expect(painted(first, 'white-space', WIDE)).toBe('normal')
  })
})

describe('V033: the phone matrix describes what it draws, and no field is blank', () => {
  it('drops the per-cell caption on a phone and keeps it on a desktop', () => {
    atPhone()
    render(<ProfileMatrix capacity={matrix()} />)
    const note = document.querySelector('.cap-mx-note')!.textContent ?? ''
    expect(note, 'the caption names cells a phone does not draw').not.toMatch(/cell/i)
    expect(note).toMatch(/Open a profile/)
    cleanup()
    vi.unstubAllGlobals()
    render(<ProfileMatrix capacity={matrix()} />)
    expect(document.querySelector('.cap-mx-note')!.textContent).toMatch(/outlined cell runs out first/)
  })

  it('says `none · disabled` for a disabled profile and an em dash for an unmeasured one', () => {
    atPhone()
    const cap = matrix()
    cap.runner_profiles.unread = profile({ pools: ['global'] })
    render(<ProfileMatrix capacity={cap} />)
    const first = (name: string) => document.querySelector(`tr[data-profile="${name}"] td[data-label="Runs out first"]`)!
    expect(first('off').textContent).toBe('none · disabled')
    expect(first('off').querySelector('.cap-mx-bind'), 'a disabled profile is priced').toBeNull()
    for (const name of ['off', 'unread', 'alpha']) {
      expect((first(name).textContent ?? '').trim(), `${name}'s Runs out first is blank`).not.toBe('')
    }
  })
})

describe('V090: the binding outline is one size and stands clear of the next row', () => {
  it('is drawn on the figures\' box, which has one minimum width, and never on the cell', () => {
    render(<ProfileMatrix capacity={matrix()} />)
    const bound = document.querySelector('tr[data-profile="alpha"] td.is-binding')!
    const box = bound.querySelector(':scope > .cap-mx-v')
    expect(box, 'the figures are not one box').not.toBeNull()
    expect(painted(box!, 'outline', WIDE)).toContain('var(--s-warn)')
    expect(painted(bound, 'outline', WIDE), 'the cell draws a column-wide outline again').toBeNull()
    const free = document.querySelector('tr[data-profile="alpha"] td.is-num:not(.is-binding) > .cap-mx-v')!
    expect(painted(free, 'min-width', WIDE)).toBe(painted(box!, 'min-width', WIDE))
    expect(painted(free, 'outline', WIDE)).toBeNull()
  })
})

describe('V093: the open row and its detail are one tinted block', () => {
  it('tints the rows, not their cells, and closes the seam between them on a phone', () => {
    atPhone()
    render(<ProfileMatrix capacity={matrix()} />)
    const row = document.querySelector<HTMLElement>('tr[data-profile="off"]')!
    fireEvent.click(within(row).getByRole('button'))
    const exp = document.querySelector('tr.cap-mx-exp')!
    for (const tr of [row, exp]) {
      expect(painted(tr, ['background', 'background-color'], PHONE), tr.className).toBe('var(--surface-2)')
    }
    expect(painted(exp, ['padding-top', 'padding'], PHONE)).toBe('0')
    // The control: a closed record keeps the stacked padding.
    const closed = document.querySelector('tr[data-profile="alpha"]')!
    expect(painted(closed, ['padding-top', 'padding'], PHONE)).not.toBe('0')
  })
})

// ---------------------------------------------------------------------------
// Runtimes
// ---------------------------------------------------------------------------

describe('V030: the `not read` pools mark is as wide as its words', () => {
  it('does not stretch the mark across the card', async () => {
    await renderRuntimes(topology({ profilePools: null }))
    const mark = document.querySelector('.rt-pools > .ctl-mark.is-unread')
    expect(mark, 'the degraded card drew no mark').not.toBeNull()
    for (const env of [PHONE, WIDE]) expect(painted(mark!.parentElement!, 'align-items', env)).toBe('flex-start')
  })
})

describe('V032: Backends and Sizing are a record per row on a phone', () => {
  it('stacks both at 400 with every field labelled, and Sizing\'s keys fit its own key column', async () => {
    atPhone()
    await renderRuntimes()
    for (const sel of ['.rt-backends > .ctl-table', '.rt-sizing > .ctl-table']) {
      const wrap = document.querySelector(sel)!
      expect(wrap.classList.contains('is-stacked'), `${sel} still scrolls at 400`).toBe(true)
      for (const td of wrap.querySelectorAll('tbody td')) expect(td.getAttribute('data-label'), sel).not.toBeNull()
    }
    const sizing = [...document.querySelectorAll('.rt-sizing td[data-label]')]
    for (const td of sizing) {
      const label = td.getAttribute('data-label')!
      expect(keyNeeds(label), `"${label}" wraps in Sizing's key column`).toBeLessThanOrEqual(keyPx(td))
    }
    // A stacked `Resolves from` wraps inside the card; only the scrolling one holds a line.
    const from = document.querySelector('.rt-sizing td[data-label="Resolves from"]')!
    expect(painted(from, 'white-space', PHONE)).not.toBe('nowrap')
  })

  it('keeps both scrolling tables on a desktop', async () => {
    await renderRuntimes()
    for (const sel of ['.rt-backends > .ctl-table', '.rt-sizing > .ctl-table']) {
      expect(document.querySelector(sel)!.classList.contains('is-scroll'), sel).toBe(true)
    }
  })
})

describe('V087: the disabled runtime\'s reason is spaced from its facts', () => {
  it('carries the reason class, and the cascade gives it a bottom margin over `.muted`\'s 0', async () => {
    await renderRuntimes()
    const reason = screen.getByText('beta is switched off; use alpha.')
    expect(reason.classList.contains('rt-reason')).toBe(true)
    expect(painted(reason, ['margin-bottom', 'margin'], WIDE)).toContain('var(--ctl-s3)')
  })
})

describe('V088: the image has a full line of its own, and the cards of a row share its height', () => {
  it('puts the image after the short facts, spanning the grid', async () => {
    await renderRuntimes()
    const facts = document.querySelector('.ctl-cards .ctl-facts.is-rows')!
    const keys = [...facts.querySelectorAll(':scope > .ctl-fact > b')].map((b) => b.textContent)
    expect(keys.indexOf('image'), 'the image sits among the short facts').toBe(keys.indexOf('weight') + 1)
    const image = [...facts.children].find((li) => li.querySelector('b')?.textContent === 'image')!
    expect(painted(image, 'grid-column', WIDE)).toBe('1 / -1')
  })

  it('stretches every card to its row, at every width, and no card opts out', async () => {
    await renderRuntimes()
    const grid = document.querySelector('.rt-cards > .ctl-cards')!
    const cards = [...grid.children]
    expect(cards.length, 'one card is no row to share').toBeGreaterThan(1)
    // MUTATION: put `align-items: start` back on `.rt-cards > .ctl-cards` and
    // each card is as tall as its own facts again, three bottoms at three
    // heights (6:capacity-010); delete the rule and the row's height rests on
    // the grid's unstated `normal` default, which this pins as a stated rule.
    for (const env of [PHONE, WIDE]) {
      expect(painted(grid, 'align-items', env)).toBe('stretch')
      // MUTATION: give `.ctl-card` (or a Runtimes card) an `align-self` other
      // than stretch and that card leaves the row's height on its own.
      for (const card of cards) expect(painted(card, 'align-self', env) ?? 'auto').toMatch(/^(auto|stretch)$/)
    }
  })

  it('declares no `align-items: start` that reaches the Runtimes grid', async () => {
    await renderRuntimes()
    const grid = document.querySelector('.rt-cards > .ctl-cards')!
    const reaches = (selector: string): boolean => {
      try {
        return grid.matches(selector)
      } catch {
        return false
      }
    }
    // MUTATION: put `.rt-cards > .ctl-cards { align-items: start; }` back in
    // styles/capacity.css -- even beaten by a later rule, it is the rule no
    // card follows that 6:capacity-010 retired, and this names its line.
    const start = flatRules(CAPACITY_CSS).filter(
      (r) => reaches(r.selector) && declarations(r.body).some((d) => d.property === 'align-items' && d.value.trim() === 'start'),
    )
    expect(start.map((r) => `capacity.css:${r.line} ${r.selector}`)).toEqual([])
  })
})

describe('V092: Runtimes re-reads on Pools\' cadence and says so', () => {
  it('reads `every 30 s · read 0 s ago`, not a bare age', async () => {
    await renderRuntimes()
    await waitFor(() => expect(document.querySelector('.c-refresh')?.textContent ?? '').toMatch(/every 30 s · read \d+ s ago/), WAIT)
  })
})

// The capacity sheet holds the matrix's answer column wrappable at the
// specificity that beats `.ctl-table td` -- read off the source too, so a
// reorder that drops the element from the selector is caught by name.
describe('the capacity sheet', () => {
  it('names the answer column\'s wrap at (0,2,1)', () => {
    expect(CAPACITY_CSS).toMatch(/\.cap-mx td\.cap-mx-first\s*\{\s*white-space:\s*normal;/)
  })
})
