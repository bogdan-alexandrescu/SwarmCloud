// ADMIN › RUNNER SIZES, ITS LEAD (ADMIN-LEADS; part of #2157, proposal E
// S10c and S10h).
//
// "Every Admin page opens with what needs an admin now, in one sentence with
// one button, and a green 'nothing waiting' lead when that is the truth."
//
// WHAT EACH CASE HOLDS:
//   * a class a platform-wide pool admits none of (its units above the pool's
//     ceiling, the row Pools marks `too small`) leads, named with the pool
//     in words (`the large class’s pool`), never its key,
//     and its one button takes the reader to that class's fields;
//   * a stored version that could not be read leads, in the words the
//     banner used to say, and the banner is not drawn a second time;
//   * every class fitting every pool is the green "Nothing waiting" lead;
//   * a sizes read that failed, and a preview read that failed, are each a
//     dash with its reason, never green;
//   * the lead reads only what the page already reads (the sizes and the
//     preview it posts for the saved version): no route is added.
//
// The bodies are `swarm_api.runnersizes`' shapes, as
// runnerSizes.screen.test.tsx builds them. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { serve } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const STANDARD = { cpu_request: 1, cpu_limit: 2, memory_gib: 4, disk_gib: 2, units: 1 }
const BROWSER = {
  cpu_request: 2, cpu_limit: 3, memory_gib: 6, disk_gib: 2, units: 2,
  live_split: { worker: { cpu_request: 1, cpu_limit: 2, memory_gib: 3 }, display: { cpu_request: 1, cpu_limit: 1, memory_gib: 3 } },
}
const LARGE = { cpu_request: 4, cpu_limit: 8, memory_gib: 24, disk_gib: 8, units: 6 }

function doc(over: Json = {}): Json {
  return {
    current_version: 7,
    in_effect_version: 7,
    malformed: false,
    classes: { standard: STANDARD, browser: BROWSER, large: LARGE },
    defaults: { standard: STANDARD, browser: BROWSER, large: LARGE },
    bounds: {
      cpu_request: { min: 0.5, max: 8, step: 0.25 },
      cpu_burst_max_ratio: 2,
      memory_gib: { min: 2, max: 32 },
      memory_per_cpu: { min: 1, max: 6.5 },
      live_split: { worker_min_cpu: 1, worker_min_memory_gib: 2, display_min_cpu: 1, display_min_memory_gib: 3 },
    },
    followers: { standard: { follows: ['claude-code'], fixed: [] }, browser: { follows: ['browser'], fixed: [] }, large: { follows: [], fixed: [] } },
    prices: { vcpu_hour: 0.0445, gib_hour: 0.0049225, source: 'docs list price' },
    updated_at: '2026-10-20T09:12:00Z',
    updated_by: 'operator@example.com',
    ...over,
  }
}

function preview(pools: Json[]): Json {
  const one = (u: number) => ({ units_before: u, units_after: u, cost_hour_before: 0.1, cost_hour_after: 0.1 })
  return {
    base_version: 7,
    valid: true,
    refusals: [],
    classes: { standard: one(1), browser: one(2), large: one(6) },
    pools,
    recent_use: {},
    below_recent_peak: [],
  }
}

const FITS = [
  { pool: 'global', hard_limit: 50, admits_before: { standard: 50, browser: 25, large: 8 }, admits_after: { standard: 50, browser: 25, large: 8 } },
  { pool: 'resource:browser', hard_limit: 12, admits_before: { browser: 6 }, admits_after: { browser: 6 } },
]
const TOO_SMALL = [
  { pool: 'global', hard_limit: 50, admits_before: { standard: 50, browser: 25, large: 8 }, admits_after: { standard: 50, browser: 25, large: 8 } },
  { pool: 'resource:large', hard_limit: 4, admits_before: { large: 0 }, admits_after: { large: 0 } },
]

type Reply = { status: number; body: unknown }

async function mount(get: () => Reply, pools: () => Reply = () => ({ status: 200, body: preview(FITS) })) {
  const calls = serve((m, url) => {
    if (m === 'GET' && url === '/v1/admin/runner-sizes') return get()
    if (m === 'GET' && url.startsWith('/v1/admin/runner-sizes/versions')) return { status: 200, body: { versions: [] } }
    if (m === 'POST' && url === '/v1/admin/runner-sizes:preview') return pools()
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunnerSizesScreen } = await import('../RunnerSizes')
  render(<RunnerSizesScreen />)
  await screen.findByRole('heading', { name: 'Runner sizes', level: 1 }, WAIT)
  const lead = await waitFor(() => {
    const l = document.querySelector<HTMLElement>('.adm-lead')
    expect(l, 'Runner sizes drew no lead').not.toBeNull()
    return l as HTMLElement
  }, WAIT)
  return { lead, calls }
}

const text = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()
const card = (name: string) => document.querySelector<HTMLElement>(`.rs-class[data-class="${name}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Runner sizes leads with the size that needs an admin (S10c)', () => {
  it('names a class its pool admits none of, with the pool', async () => {
    const { lead } = await mount(() => ({ status: 200, body: doc() }), () => ({ status: 200, body: preview(TOO_SMALL) }))
    expect(lead.dataset.lead).toBe('warn')
    // The pool is named as Pool limits names it, never by its key
    // (QA1-FIX-23, admin-shell-04: the lead read `Pool resource:large`).
    // MUTATION: print `small.pool` in the title or the body again.
    expect(text(lead)).toContain('The large class’s pool admits no large agent')
    expect(text(lead)).toContain('A large agent takes 6 units and the pool’s ceiling is 4.')
    expect(text(lead)).not.toContain('resource:')
    expect(within(lead).getAllByRole('button')).toHaveLength(1)
  })

  it('names a pool that is not a class’s own in words too', async () => {
    const GLOBAL_TOO_SMALL = [
      { pool: 'global', hard_limit: 4, admits_before: { standard: 4, browser: 2, large: 0 }, admits_after: { standard: 4, browser: 2, large: 0 } },
    ]
    const { lead } = await mount(() => ({ status: 200, body: doc() }), () => ({ status: 200, body: preview(GLOBAL_TOO_SMALL) }))
    // MUTATION: call every pool "the <x> class’s pool", `global` included.
    expect(text(lead)).toContain('The platform-wide pool admits no large agent')
    expect(text(lead)).toContain('A large agent takes 6 units and the pool’s ceiling is 4.')
  })

  it('takes the reader to that class’s fields from its one button', async () => {
    const { lead } = await mount(() => ({ status: 200, body: doc() }), () => ({ status: 200, body: preview(TOO_SMALL) }))
    await waitFor(() => expect(card('large')).not.toBeNull(), WAIT)
    fireEvent.click(within(lead).getByRole('button', { name: 'Open large' }))
    expect(card('large')!.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).toBe(screen.getByLabelText('large memory (GiB)'))
  })

  it('leads with a stored version that could not be read, and draws it once', async () => {
    const { lead } = await mount(() => ({ status: 200, body: doc({ malformed: true, in_effect_version: 0 }) }))
    expect(lead.dataset.lead).toBe('warn')
    expect(text(lead)).toContain('Version 7 could not be read')
    // The banner said this; the lead says it now, and only the lead.
    await waitFor(() => expect(card('standard')).not.toBeNull(), WAIT)
    expect(document.body.textContent!.split('Version 7 could not be read').length - 1).toBe(1)
  })
})

describe('nothing waiting reads the green lead (S10c)', () => {
  it('says every class fits every pool, with no button', async () => {
    const { lead } = await mount(() => ({ status: 200, body: doc() }))
    expect(lead.dataset.lead).toBe('ok')
    expect(text(lead)).toContain('Nothing waiting')
    expect(text(lead)).toContain('Every class fits every pool’s ceiling')
    expect(within(lead).queryAllByRole('button')).toHaveLength(0)
  })
})

describe('a read that failed is a dash with its reason, never green', () => {
  it('when the sizes could not be read', async () => {
    const { lead } = await mount(() => ({ status: 500, body: { detail: 'Internal Server Error' } }))
    expect(lead.dataset.lead).toBe('unread')
    expect(text(lead)).toContain('—, the runner sizes could not be read')
    expect(text(lead)).toContain('The API failed on this request')
    expect(document.querySelector('.adm-lead[data-lead="ok"]')).toBeNull()
  })

  it('when what the pools admit could not be read', async () => {
    const { lead } = await mount(
      () => ({ status: 200, body: doc() }),
      () => ({ status: 500, body: { detail: 'Internal Server Error' } }),
    )
    expect(lead.dataset.lead).toBe('unread')
    expect(text(lead)).toContain('—, what the pools admit could not be read')
    expect(document.querySelector('.adm-lead[data-lead="ok"]')).toBeNull()
  })
})
