// QA2-FIX-20 (part of #2694): Admin › Runner sizes, the console visual QA
// pass 2 findings on the fixture console (QA2-ADMIN-SHELL, 2026-10-29).
//
//   admin2-05  the admit table's Pool column read `resource:standard` with a
//              Class column repeating `standard`; History read
//              `large memory_gib 16 → 32`.
//   admin2-10  'Restore the code defaults' was stretched across the card.
//   admin2-12  the admit column read `20 → 20` with nothing edited.
//   admin2-13  the ordering error read 'standard is no larger than browser in
//              memory: 100 > 8'; the Save line named no field.
//   admin2-14  'Cloud Run, does not follow: …', a broken sentence.
//
// The pools are the fixture preview's (api.ts `fixtureRunnerSizesPreview`):
// each class's own resource pool, admitting that class only. Every assertion
// names the mutation that turns it red. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { serve, visible } from './repofixture'
import { changeLine, clientRefusals, PoolsTable } from '../RunnerSizes'
import type { RunnerSizesDoc } from '../types'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const STANDARD = { cpu_request: 4, cpu_limit: 8, memory_gib: 8, disk_gib: 4, units: 1 }
const BROWSER = {
  cpu_request: 4, cpu_limit: 8, memory_gib: 8, disk_gib: 4, units: 1,
  live_split: { worker: { cpu_request: 2, cpu_limit: 4, memory_gib: 3 }, display: { cpu_request: 2, cpu_limit: 3, memory_gib: 5 } },
}
const LARGE = { cpu_request: 8, cpu_limit: 16, memory_gib: 32, disk_gib: 16, units: 4 }

function doc(version: number): Json {
  return {
    current_version: version,
    in_effect_version: version,
    malformed: false,
    classes: { standard: STANDARD, browser: BROWSER, large: LARGE },
    defaults: { standard: STANDARD, browser: BROWSER, large: LARGE },
    bounds: {
      cpu_request: { min: 0.5, max: 8, step: 0.25 },
      cpu_burst_max_ratio: 2,
      memory_gib: { min: 2, max: 32 },
      memory_per_cpu: { min: 1, max: 6.5 },
      disk_gib: { min: 1, max: 'memory_gib - 2', editable: false },
      live_split: { worker_min_cpu: 1, worker_min_memory_gib: 2, display_min_cpu: 1, display_min_memory_gib: 5 },
    },
    followers: {
      standard: { follows: ['claude-code', 'codex'], fixed: ['mock', 'generic'] },
      browser: { follows: ['browser'], fixed: [] },
      large: { follows: [], fixed: [] },
    },
    prices: { vcpu_hour: 0.0445, gib_hour: 0.0049225, source: 'docs list price, 2026-10-13' },
    updated_at: '2026-10-21T09:12:00Z',
    updated_by: 'ops@example.com',
  }
}

type Pools = { pool: string; hard_limit: number; admits_before: Record<string, number>; admits_after: Record<string, number | null> }[]

const OWN_POOLS: Pools = [
  { pool: 'resource:standard', hard_limit: 20, admits_before: { standard: 20 }, admits_after: { standard: 20 } },
  { pool: 'resource:browser', hard_limit: 4, admits_before: { browser: 4 }, admits_after: { browser: 4 } },
  { pool: 'resource:large', hard_limit: 2, admits_before: { large: 0 }, admits_after: { large: 0 } },
]

function preview(): Json {
  const one = (u: number) => ({ units_before: u, units_after: u, cost_hour_before: 0.2, cost_hour_after: 0.2 })
  return {
    base_version: 2,
    valid: true,
    refusals: [],
    classes: { standard: one(1), browser: one(1), large: one(4) },
    pools: OWN_POOLS,
    recent_use: {},
    below_recent_peak: [],
  }
}

const VERSIONS = {
  versions: [
    {
      version: 2, previous_version: 1, created_at: '2026-10-21T09:12:00Z', created_by: 'ops@example.com',
      reason: 'large agents hit the memory limit', restored_from: null,
      changed: [{ class: 'large', field: 'memory_gib', from: 16, to: 32 }],
    },
    {
      version: 1, previous_version: 0, created_at: '2026-10-20T21:00:00Z', created_by: 'root@example.com',
      reason: 'first version', restored_from: null,
      changed: [{ class: 'standard', field: 'cpu_limit', from: 4, to: 8 }],
    },
  ],
}

async function mount() {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/admin/runner-sizes') return { status: 200, body: doc(2) }
    if (m === 'GET' && url.startsWith('/v1/admin/runner-sizes/versions')) return { status: 200, body: VERSIONS }
    if (m === 'POST' && url === '/v1/admin/runner-sizes:preview') return { status: 200, body: preview() }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunnerSizesScreen } = await import('../RunnerSizes')
  render(<RunnerSizesScreen />)
  await screen.findByRole('heading', { name: 'Runner sizes', level: 1 }, WAIT)
  await waitFor(() => expect(card('standard')).not.toBeNull(), WAIT)
}

const card = (name: string) => document.querySelector<HTMLElement>(`.rs-class[data-class="${name}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('admin2-05: the admit table names each pool, and History names each field in words', () => {
  it('draws the pool by its name with the key under it, and no Class column repeating it', () => {
    render(<PoolsTable pools={OWN_POOLS} />)
    const table = screen.getByRole('table', { name: 'What the pools admit' })
    const row = table.querySelector<HTMLElement>('tr[data-pool="resource:standard"]')!
    const head = within(row).getByRole('rowheader')
    // MUTATION: draw `{p.pool}` as the row header again: it reads `resource:standard`.
    expect(head.firstChild?.textContent).toBe('standard')
    // The raw key stays, as Pool limits keeps it, on a line of its own.
    // MUTATION: drop `PoolName`'s key line.
    expect(head.querySelector('.rs-pool-key')?.textContent).toBe('resource:standard')
    // MUTATION: always draw the Class column: it repeats the pool's name.
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Pool', 'Ceiling (units)', 'Agents admitted, before → after'])
  })

  it('keeps the Class column where a pool admits more than its own class', () => {
    render(<PoolsTable pools={[{ pool: 'global', hard_limit: 40, admits_before: { standard: 40, large: 10 }, admits_after: { standard: 40, large: 10 } }]} />)
    // MUTATION: drop the Class column everywhere: the global pool's rows lose which class each counts.
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toContain('Class')
    // `global` is its own name: no key line under it.
    expect(screen.getAllByRole('rowheader')[0]!.querySelector('.rs-pool-key')).toBeNull()
  })

  it('writes a changed field as its words with its unit', () => {
    // MUTATION: write `${c.field}` raw: `large memory_gib 16 → 32`.
    expect(changeLine('large', 'memory_gib', 16, 32)).toBe('large memory 16 → 32 GiB')
    expect(changeLine('browser', 'live_split.worker.cpu_limit', 4, 6)).toBe('browser worker CPU limit 4 → 6 cores')
    // An unknown figure is a dash, never 0.
    expect(changeLine('large', 'memory_gib', null, 32)).toBe('large memory — → 32 GiB')
  })

  it('History lists the change in words on the screen', async () => {
    await mount()
    const row = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.rs-version[data-version="2"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    // MUTATION: render History's change line from the raw field again.
    expect(visible(row.querySelector('.rs-version-changes'))).toContain('large memory 16 → 32 GiB')
    expect(visible(row)).not.toContain('memory_gib')
  })
})

describe('admin2-12: an unchanged admit count is one figure', () => {
  it('draws `20`, not `20 → 20`, and the arrow only where a save moves it', () => {
    const moved = [{ pool: 'resource:large', hard_limit: 8, admits_before: { large: 2 }, admits_after: { large: 4 } }]
    render(<PoolsTable pools={[...OWN_POOLS.slice(0, 2), ...moved]} />)
    const cell = (pool: string) => document.querySelector(`tr[data-pool="${pool}"] td[data-label="Agents admitted, before → after"]`)!.textContent
    // MUTATION: draw `${before} → ${after}` whatever they are.
    expect(cell('resource:standard')).toBe('20')
    expect(cell('resource:browser')).toBe('4')
    expect(cell('resource:large')).toBe('2 → 4')
  })
})

describe('admin2-13: the ordering rule says what must hold, with units, and Save names the field', () => {
  const draftOf = (d: Json, over: Record<string, Record<string, string>>) => {
    const classes = d.classes as Record<string, Json>
    const out: Record<string, Record<string, string>> = {}
    for (const [name, size] of Object.entries(classes)) {
      out[name] = { cpu_request: String(size.cpu_request), cpu_limit: String(size.cpu_limit), memory_gib: String(size.memory_gib), ...(over[name] ?? {}) }
    }
    return out
  }

  it('reads `standard’s memory (16 GiB) must be no larger than browser’s (8 GiB).`', () => {
    const d = doc(2)
    const refusals = clientRefusals(d as unknown as RunnerSizesDoc, draftOf(d, { standard: { memory_gib: '16' } }))
    const bounds = refusals.filter((r) => r.field === 'memory_gib').map((r) => r.bound)
    // MUTATION: restore `${low} is no larger than ${high} in ${words}: ${a} > ${c}`.
    expect(bounds).toContain('standard’s memory (16 GiB) must be no larger than browser’s (8 GiB).')
    expect(bounds.join(' ')).not.toContain('>')
  })

  it('names the first failing field on the Save line', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'builds run out of memory' } })
    fireEvent.change(screen.getByLabelText('standard memory (GiB)'), { target: { value: '100' } })
    // MUTATION: say only 'A size is outside its bound.'
    await waitFor(() => expect(document.body.textContent).toContain('standard memory is outside its bound.'), WAIT)
    expect(document.body.textContent).not.toContain('A size is outside its bound.')
  })
})

describe('admin2-10 and admin2-14: the History foot and the Cloud Run note', () => {
  it('keeps the code-defaults button out of the card’s stretching column', async () => {
    await mount()
    const button = await screen.findByRole('button', { name: 'Restore the code defaults' }, WAIT)
    // MUTATION: render the Button as the card's direct child: the card's flex
    // column stretches it into a full-width bar (components.css `.c-card`).
    expect(button.parentElement?.classList.contains('c-card')).toBe(false)
    expect(button.parentElement?.classList.contains('rs-restore-defaults')).toBe(true)
  })

  it('says which Cloud Run profiles do not follow, in a sentence', async () => {
    await mount()
    const fixed = card('standard')!.querySelector<HTMLElement>('.rs-fixed')
    // MUTATION: restore 'Cloud Run, does not follow: …'.
    expect(visible(fixed)).toContain('These Cloud Run profiles do not follow this size: mock, generic.')
    expect(visible(fixed)).not.toContain('Cloud Run, does not follow')
  })
})
