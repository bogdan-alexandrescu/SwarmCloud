// ADMIN › RUNNER SIZES, THE SCREEN (SIZES-UI; part of #1689,
// docs/design/runner-sizes.md §4.5, §4.6, §4.9 and §6).
//
// WHAT EACH CASE HOLDS:
//   * a CPU limit above 2 × the request shows the bound at the field, in the
//     bound's own words, and disables Save; the preview's refusal then
//     REPLACES the client's (the preview is the authority, §4.6);
//   * the preview's units and dollars before -> after, and what the
//     platform-wide pools admit before -> after, are drawn;
//   * a 409 `stale_base_version` reads the sizes again, shows what is now
//     current and says the edit was not saved;
//   * a memory cut below the class's 7-day max peak asks for the class name
//     typed (owner Q6), and only then sends `confirm_below_recent_peak`;
//   * History lists each version -- who, when, why, what changed -- and
//     Restore posts to the restore route with a reason;
//   * §4.9's pattern hints appear with 30 attempts and not with 29, and a
//     share the API reports as null reads "no data", never 0%;
//   * `disk_gib` is shown and never an input (owner Q5); Cloud Run profiles
//     are listed as not following this size (owner Q3).
//
// The bodies are `swarm_api.runnersizes`' shapes (`served`, `preview`,
// `versions`; tests/unit/control_plane/test_runner_sizes_api.py). Nothing
// here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const STANDARD = { cpu_request: 4, cpu_limit: 8, memory_gib: 8, disk_gib: 4, units: 1 }
const BROWSER = {
  cpu_request: 4, cpu_limit: 8, memory_gib: 8, disk_gib: 4, units: 1,
  live_split: { worker: { cpu_request: 2, cpu_limit: 4, memory_gib: 3 }, display: { cpu_request: 2, cpu_limit: 3, memory_gib: 5 } },
}
const LARGE = { cpu_request: 8, cpu_limit: 16, memory_gib: 32, disk_gib: 16, units: 4 }

function doc(version: number, over: Json = {}): Json {
  return {
    current_version: version,
    in_effect_version: version,
    malformed: false,
    classes: { standard: STANDARD, browser: BROWSER, large: LARGE, ...over },
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
      standard: { follows: ['claude-code', 'codex', 'generic', 'indexer', 'merge'], fixed: ['claude-code-review', 'mock', 'post-verdict'] },
      browser: { follows: ['browser', 'claude-code-browser'], fixed: [] },
      large: { follows: [], fixed: [] },
    },
    prices: { vcpu_hour: 0.0445, gib_hour: 0.0049225, source: 'docs list price, 2026-10-13' },
    updated_at: '2026-10-21T09:12:00Z',
    updated_by: 'ops@example.com',
  }
}

function use(attempts: number, over: Json = {}): Json {
  return {
    attempts,
    since: '2026-10-14T09:00:00Z',
    memory_peak_gib_max: 6.0,
    memory_peak_gib_p95: 5.4,
    cpu_mean_cores_median: 1.74,
    share_idle: 0.15,
    share_above_request: null,
    share_at_limit: 0.22,
    share_at_limit_long: 0.3,
    throttled_share: null,
    oom_near_miss: 0,
    by_size_version: [{ size_version: 0, attempts, wall_minutes_median: 31, wall_minutes_median_long: 52, memory_peak_gib_max: 6.0, share_at_limit: 0.22, throttled_share: null }],
    ...over,
  }
}

const EMPTY_USE = use(0, {
  memory_peak_gib_max: null, memory_peak_gib_p95: null, cpu_mean_cores_median: null, share_idle: null,
  share_at_limit: null, share_at_limit_long: null, by_size_version: [],
})

function preview(over: Json = {}, classes: Json = {}): Json {
  const one = (u: number, cost: number) => ({ units_before: u, units_after: u, cost_hour_before: cost, cost_hour_after: cost })
  return {
    base_version: 2,
    valid: true,
    refusals: [],
    classes: { standard: one(1, 0.2174), browser: one(1, 0.2174), large: one(4, 0.5135), ...classes },
    pools: [
      { pool: 'global', hard_limit: 40, admits_before: { standard: 40, browser: 40, large: 10 }, admits_after: { standard: 40, browser: 40, large: 10 } },
    ],
    recent_use: { standard: use(41), browser: use(12), large: EMPTY_USE },
    below_recent_peak: [],
    ...over,
  }
}

const VERSIONS = {
  versions: [
    {
      version: 2, previous_version: 1, created_at: '2026-10-21T09:12:00Z', created_by: 'ops@example.com',
      reason: 'long implement runs pinned at 4 cores', restored_from: null,
      changed: [{ class: 'standard', field: 'cpu_limit', from: 4, to: 8 }],
    },
    {
      version: 1, previous_version: 0, created_at: '2026-10-20T21:00:00Z', created_by: 'root@example.com',
      reason: 'first version', restored_from: null,
      changed: [{ class: 'large', field: 'memory_gib', from: 32, to: 24 }],
    },
  ],
}

type Reply = { status: number; body: unknown }
type Routes = {
  get?: () => Reply
  preview?: (body: Json) => Reply
  put?: (body: Json) => Reply
  restore?: (body: Json) => Reply
}

async function mount(routes: Routes = {}) {
  const calls = serve((m, url, b) => {
    if (m === 'GET' && url === '/v1/admin/runner-sizes') return routes.get?.() ?? { status: 200, body: doc(2) }
    if (m === 'GET' && url.startsWith('/v1/admin/runner-sizes/versions')) return { status: 200, body: VERSIONS }
    if (m === 'POST' && url === '/v1/admin/runner-sizes:preview') return routes.preview?.(b as Json) ?? { status: 200, body: preview() }
    if (m === 'PUT' && url === '/v1/admin/runner-sizes') return routes.put?.(b as Json) ?? { status: 200, body: doc(3) }
    if (m === 'POST' && url === '/v1/admin/runner-sizes/restore') return routes.restore?.(b as Json) ?? { status: 200, body: doc(3) }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunnerSizesScreen } = await import('../RunnerSizes')
  const utils = render(<RunnerSizesScreen />)
  await screen.findByRole('heading', { name: 'Runner sizes', level: 1 }, WAIT)
  await waitFor(() => expect(card('standard')).not.toBeNull(), WAIT)
  return { ...utils, calls }
}

const card = (name: string) => document.querySelector<HTMLElement>(`.rs-class[data-class="${name}"]`)
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement
const type = (label: string, value: string) => fireEvent.change(field(label), { target: { value } })
const saveButton = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
const previews = (calls: { method: string; url: string; body: unknown }[]) =>
  calls.filter((c) => c.method === 'POST' && c.url === '/v1/admin/runner-sizes:preview')

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('editing a class', () => {
  it('shows the bound when a limit is above 2 × the request, and disables Save', async () => {
    await mount({
      preview: (b) => {
        const classes = (b.classes ?? {}) as Json
        const std = (classes.standard ?? {}) as Json
        return std.cpu_limit === 12
          ? { status: 200, body: preview({ valid: false, refusals: [{ class: 'standard', field: 'cpu_limit', value: 12, bound: 'cpu_limit <= 2 x cpu_request (8)' }] }) }
          : { status: 200, body: preview() }
      },
    })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'long runs pinned' } })
    type('standard CPU limit', '12')
    // The client's check, at once, in the bound's words.
    // MUTATION: drop the ratio check from the client's validation.
    expect(visible(card('standard'))).toContain('limit is at most 2 × request: 8')
    // MUTATION: leave Save enabled while a refusal stands.
    expect(saveButton().disabled).toBe(true)
    // The preview is the authority: its refusal replaces the client's.
    // MUTATION: draw the client's refusals only, never the preview's.
    await waitFor(() => expect(visible(card('standard'))).toContain('cpu_limit <= 2 x cpu_request (8)'), WAIT)
    expect(visible(card('standard'))).not.toContain('limit is at most 2 × request: 8')
    expect(saveButton().disabled).toBe(true)
    // Back inside the bound, Save opens.
    type('standard CPU limit', '6')
    await waitFor(() => expect(saveButton().disabled).toBe(false), WAIT)
  })

  it('draws the preview’s units, dollars and what each pool admits, before and after', async () => {
    const { calls } = await mount({
      preview: (b) => {
        const classes = (b.classes ?? {}) as Json
        if ((classes.large as Json | undefined)?.memory_gib !== 24) return { status: 200, body: preview() }
        return {
          status: 200,
          body: preview(
            {
              pools: [{ pool: 'global', hard_limit: 40, admits_before: { standard: 40, browser: 40, large: 10 }, admits_after: { standard: 40, browser: 40, large: 13 } }],
            },
            { large: { units_before: 4, units_after: 3, cost_hour_before: 0.5135, cost_hour_after: 0.4741 } },
          ),
        }
      },
    })
    type('large memory (GiB)', '24')
    // MUTATION: draw units_before alone, or never draw the preview's classes.
    await waitFor(() => expect(visible(card('large')!.querySelector('.rs-units'))).toContain('4 → 3'), WAIT)
    expect(visible(card('large')!.querySelector('.rs-cost'))).toContain('$0.5135 → $0.4741')
    // MUTATION: drop the pools table.
    const row = document.querySelector<HTMLElement>('.rs-pools tr[data-pool="global"][data-class="large"]')
    expect(row, 'no pools row for global × large').not.toBeNull()
    expect(visible(row)).toContain('10 → 13')
    // The body names only what changed: the class and its one field.
    // MUTATION: send the whole table, or `units`.
    const last = previews(calls).at(-1)!.body as Json
    expect(last.classes).toEqual({ large: { memory_gib: 24 } })
  })

  it('reads the sizes again on a 409 and says the edit was not saved', async () => {
    let gets = 0
    const { calls } = await mount({
      get: () => {
        gets += 1
        return { status: 200, body: gets === 1 ? doc(2) : doc(3, { standard: { ...STANDARD, cpu_limit: 6 } }) }
      },
      put: () => ({
        status: 409,
        body: { code: 'stale_base_version', message: 'runner sizes are at version 3, not 2; read them again', detail: { current_version: 3, base_version: 2 } },
      }),
    })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'try 7' } })
    type('standard CPU limit', '7')
    await waitFor(() => expect(saveButton().disabled).toBe(false), WAIT)
    fireEvent.click(saveButton())
    // MUTATION: show the 409's message and keep the stale draft, without reading again.
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET' && c.url === '/v1/admin/runner-sizes').length).toBe(2), WAIT)
    await waitFor(() => expect(field('standard CPU limit').value).toBe('6'), WAIT)
    const banner = document.querySelector<HTMLElement>('.rs-stale')
    expect(visible(banner)).toContain('version 3')
    expect(visible(banner)).toContain('not saved')
    const put = calls.find((c) => c.method === 'PUT')!.body as Json
    expect(put.base_version).toBe(2)
    expect(put.classes).toEqual({ standard: { cpu_limit: 7 } })
  })

  it('asks for the class name typed before a memory cut below the 7-day max peak', async () => {
    const { calls } = await mount({
      preview: (b) => {
        const classes = (b.classes ?? {}) as Json
        const cut = (classes.standard as Json | undefined)?.memory_gib === 4
        return { status: 200, body: preview(cut ? { below_recent_peak: ['standard'] } : {}) }
      },
    })
    // The 7-day max and p95 are beside memory, from the preview's recent use.
    await waitFor(() => expect(visible(card('standard')!.querySelector('.rs-recent'))).toContain('6.0 GiB'), WAIT)
    expect(visible(card('standard')!.querySelector('.rs-recent'))).toContain('5.4 GiB')
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'memory is idle' } })
    type('standard memory (GiB)', '4')
    await waitFor(() => expect(saveButton().disabled).toBe(false), WAIT)
    fireEvent.click(saveButton())
    // MUTATION: send the PUT without asking.
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
    expect(visible(dialog)).toContain('standard')
    const confirm = within(dialog).getByRole('button', { name: 'Save the cut' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'standard' } })
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)
    // MUTATION: drop `confirm_below_recent_peak` from the body.
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true), WAIT)
    const put = calls.find((c) => c.method === 'PUT')!.body as Json
    expect(put).toEqual({ base_version: 2, reason: 'memory is idle', classes: { standard: { memory_gib: 4 } }, confirm_below_recent_peak: true })
  })

  it('shows disk as a figure, never an input, and lists Cloud Run profiles as not following', async () => {
    await mount()
    // MUTATION: draw disk_gib as an input (owner Q5: shown, not edited).
    expect(screen.queryByLabelText(/standard (workspace|disk)/)).toBeNull()
    expect(visible(card('standard')!.querySelector('.rs-disk'))).toContain('4 GiB')
    // MUTATION: drop the fixed list (owner Q3).
    const fixed = card('standard')!.querySelector<HTMLElement>('.rs-fixed')
    expect(visible(fixed)).toContain('do not follow this size')
    expect(visible(fixed)).toContain('claude-code-review')
  })
})

describe('history', () => {
  it('lists each version, and Restore posts to the restore route with a reason', async () => {
    const { calls } = await mount()
    const row = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.rs-version[data-version="1"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    // Who, why and what changed.
    // MUTATION: drop the reason or the changes from a row.
    expect(visible(row)).toContain('root@example.com')
    expect(visible(row)).toContain('first version')
    // The field in words with its unit (QA2-FIX-20, admin2-05), not `memory_gib`.
    expect(visible(row)).toContain('large memory 32 → 24 GiB')
    // The current version is not offered as a restore.
    const current = document.querySelector<HTMLElement>('.rs-version[data-version="2"]')!
    expect(within(current).queryByRole('button', { name: /Restore/ })).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Restore version 1' }))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    const go = within(dialog).getByRole('button', { name: 'Restore' }) as HTMLButtonElement
    // A version says why: no reason, no restore.
    expect(go.disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText('Reason for the restore'), { target: { value: 'back to 24 GiB' } })
    fireEvent.click(go)
    // MUTATION: restore by PUTting the version's classes, or rewind with no reason.
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/runner-sizes/restore')).toBe(true), WAIT)
    const post = calls.find((c) => c.url === '/v1/admin/runner-sizes/restore')!.body as Json
    expect(post).toEqual({ base_version: 2, version: 1, reason: 'back to 24 GiB', confirm_below_recent_peak: false })
  })
})

describe('the readings and their hints (§4.9)', () => {
  it('draws the pattern hints with 30 attempts', async () => {
    await mount({ preview: () => ({ status: 200, body: preview({ recent_use: { standard: use(30), browser: use(12), large: EMPTY_USE } }) }) })
    // MUTATION: drop the long-run at-limit hint.
    await waitFor(() => expect(visible(card('standard')!.querySelector('.rs-hints'))).toContain('Long runs sit at the CPU limit 30% of the time'), WAIT)
  })

  it('draws no hint with 29 attempts, and says why', async () => {
    await mount({ preview: () => ({ status: 200, body: preview({ recent_use: { standard: use(29), browser: use(12), large: EMPTY_USE } }) }) })
    await waitFor(() => expect(visible(card('standard')!.querySelector('.rs-recent'))).toContain('29 attempts'), WAIT)
    // MUTATION: compare with `>= 29`, or drop the threshold.
    expect(card('standard')!.querySelector('.rs-hint')).toBeNull()
    expect(visible(card('standard'))).toContain('Fewer than 30 attempts')
  })

  it('reads a null share as "no data", never 0%', async () => {
    await mount()
    const reading = await waitFor(() => {
      const el = card('standard')!.querySelector<HTMLElement>('[data-reading="share_above_request"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    // MUTATION: coalesce a null share to 0.
    expect(visible(reading)).toContain('no data')
    expect(visible(reading)).not.toContain('0%')
    expect(visible(card('standard')!.querySelector('[data-reading="share_at_limit"]'))).toContain('22%')
  })
})

// CONSOLE VISUAL QA PASS 1 (QA1-FIX-23; part of #2596). Each case is one
// finding, held by the property the sweep found false.
describe('visual QA pass 1 (QA1-FIX-23)', () => {
  // admin-shell-16: with nothing edited, every card read `Units 1 → 1` and
  // `$0.4176 → $0.4176`, a change arrow over no change.
  it('draws one value until an edit moves it, and the arrow only when it differs', async () => {
    await mount()
    await waitFor(() => expect(visible(card('large')!.querySelector('.rs-cost'))).toContain('$0.5135'), WAIT)
    // MUTATION: draw `before → after` whenever a preview answered.
    expect(visible(card('large')!.querySelector('.rs-units'))).toBe('4')
    expect(visible(card('large')!.querySelector('.rs-cost'))).toBe('$0.5135')
    expect(visible(card('standard')!.querySelector('.rs-units'))).not.toContain('→')
  })

  // chrome-C24: `Recent use: reading…` stayed on screen with nothing to say
  // what it waited for, and a preview that failed, or answered without the
  // class, left it reading for good.
  it('says what recent use waits for while it reads', async () => {
    await mount()
    // MUTATION: go back to the bare `Recent use: reading…`.
    expect(visible(card('standard')!.querySelector('.rs-recent'))).toBe('Recent use: reading the last 7 days of attempts with the preview…')
  })

  it('reads a failed preview as a dash with its reason, never reading for good', async () => {
    await mount({ preview: () => ({ status: 500, body: { error: { code: 'internal', message: 'preview broke' } } }) })
    // MUTATION: leave the card at `reading…` when the preview answers with an error.
    await waitFor(() => expect(visible(card('standard')!.querySelector('.rs-recent'))).toMatch(/^Recent use: — could not be read/), WAIT)
    expect(visible(card('standard'))).not.toContain('reading')
  })

  it('reads a class the preview has no record for as a dash with its reason', async () => {
    await mount({ preview: () => ({ status: 200, body: preview({ recent_use: { standard: use(41), browser: use(12) } }) }) })
    await waitFor(() => expect(visible(card('standard')!.querySelector('.rs-recent'))).toContain('41 attempts'), WAIT)
    // MUTATION: treat a class missing from `recent_use` as still reading.
    expect(visible(card('large')!.querySelector('.rs-recent'))).toBe('Recent use: — the preview returned no record for large.')
  })
})
