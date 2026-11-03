// THE CACHE SECTION UNDER EACH INDEXED REPOSITORY (lane CACHE-4-CONSOLE, part
// of #1835; docs/design/graph-answer-cache.md §5.1-§5.5, BGD's pick C, "live
// replicas, v2 from the start", 2026-10-27). What each case holds:
//   * nothing is read while the section is folded or the page is hidden, and
//     reopening or becoming visible reads once at once (§5.5);
//   * open, it reads once and then every 30 s (§5.5);
//   * a replica that was not read shows its reason, never zeros (§5.3, §5.4);
//   * the reuse tile is a dash with no `carried` count until there is one
//     (§5.2, BGD Q4);
//   * a failed read keeps the last good values, with their time and the
//     reason the new read failed (§5.5);
//   * swarm-graph not answering at all still shows the latency tile (§5.3);
//   * an index not promoted yet says so and reads nothing (§5.1).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { repo, serve } from './repofixture'
import { RepoGraphCache } from '../RepoGraphCache'
import { normGraphCache, type GraphCacheDoc } from '../api'
import type { ApiError, Result } from '../fetch'

const MiB = 1024 * 1024
const REPO = 'repo_5e1f0c2a9b3d4e61'

function body(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    repo_id: REPO,
    index: { commit_sha: '4c1e9a0000', manifest_digest: 'sha256:aa', previous_sha: null, previous_digest: null },
    facts: {
      window_hours: 24, answers: 1284, hit: 1168, carried: null, miss: 116, hit_rate: 1168 / 1284,
      reused_share: null, served_p95_ms: 38, last_warm: { digest: 'sha256:aa', ms: 2100, shards: 40, replayed: 200 },
    },
    replicas: [
      {
        replica: 'swarm-graph-0', state: 'read', started_at: '2026-10-25T10:00:00+00:00', uptime_seconds: 3 * 86400,
        result_bytes: 64 * MiB, result_bound: 256 * MiB, shard_bytes: 128 * MiB, shard_bound: 512 * MiB,
        answers_hot: 412, shards_hot: 31, last_warm: null,
      },
      { replica: 'swarm-graph-1', state: 'not_read', reason: 'timeout' },
    ],
    stats: { state: 'read' },
    latency: { p50_ms: 61, p95_ms: 152, reference_ms: 20.5 },
    ...over,
  }
}

const ok = (raw: Record<string, unknown> = body()): Result<GraphCacheDoc> => ({ status: 'ok', data: normGraphCache(raw), fetchedAt: Date.now() })
const UNREACHABLE: ApiError = { kind: 'unreachable', httpStatus: null, code: null, message: 'The API could not be reached' }
const failed = (): Result<GraphCacheDoc> => ({ status: 'error', error: UNREACHABLE })

let visibility: DocumentVisibilityState = 'visible'

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: false })
  visibility = 'visible'
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

async function setVisibility(v: DocumentVisibilityState) {
  visibility = v
  await act(async () => {
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
  })
}

function opener(): HTMLElement {
  return screen.getByRole('button', { name: /^Cache/ })
}

async function open() {
  await act(async () => {
    fireEvent.click(opener())
    await vi.advanceTimersByTimeAsync(0)
  })
}

function tile(label: string): HTMLElement {
  const t = Array.from(document.querySelectorAll<HTMLElement>('.c-tile')).find((n) => n.querySelector('small')?.textContent === label)
  if (t === undefined) throw new Error(`no tile ${label}`)
  return t
}

function panel(name: string): HTMLElement {
  const p = document.querySelector<HTMLElement>(`[data-replica="${name}"]`)
  if (p === null) throw new Error(`no panel ${name}`)
  return p
}

describe('the Cache section reads only while it is open and the page is visible (§5.5)', () => {
  it('reads nothing while folded, once on opening, nothing while hidden, and once at once on becoming visible', async () => {
    const load = vi.fn(async () => ok())
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={load} />)
    expect(opener().getAttribute('aria-expanded')).toBe('false')
    await tick(90_000)
    expect(load).not.toHaveBeenCalled()

    await open()
    expect(load).toHaveBeenCalledTimes(1)
    expect(load).toHaveBeenLastCalledWith(REPO, null)

    await setVisibility('hidden')
    await tick(95_000)
    expect(load).toHaveBeenCalledTimes(1)

    await setVisibility('visible')
    expect(load).toHaveBeenCalledTimes(2)
    await tick(30_000)
    expect(load).toHaveBeenCalledTimes(3)
  })

  it('opened while the page is hidden, it waits for the page to be visible', async () => {
    const load = vi.fn(async () => ok())
    visibility = 'hidden'
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={load} />)
    await open()
    await tick(60_000)
    expect(load).not.toHaveBeenCalled()
    await setVisibility('visible')
    expect(load).toHaveBeenCalledTimes(1)
  })
})

describe('open, it refreshes every 30 s (§5.5)', () => {
  it('reads again at 30 s and 60 s, and stops when folded', async () => {
    const load = vi.fn(async () => ok())
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={load} />)
    await open()
    expect(load).toHaveBeenCalledTimes(1)
    await tick(29_000)
    expect(load).toHaveBeenCalledTimes(1)
    await tick(1_000)
    expect(load).toHaveBeenCalledTimes(2)
    await tick(30_000)
    expect(load).toHaveBeenCalledTimes(3)

    await act(async () => {
      fireEvent.click(opener())
    })
    await tick(120_000)
    expect(load).toHaveBeenCalledTimes(3)
  })

  it('each read shows its own time', async () => {
    const load = vi.fn(async () => ok())
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={load} />)
    await open()
    await tick(12_000)
    expect(document.querySelector('[data-read-age]')?.textContent).toMatch(/read 12 s ago/)
  })
})

describe('a replica that was not read (§5.3, §5.4)', () => {
  it('shows its reason and no zeros', async () => {
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok()} />)
    await open()
    const p = panel('swarm-graph-1')
    expect(p.getAttribute('data-reason')).toBe('timeout')
    expect(p.textContent).toMatch(/not read/i)
    expect(p.textContent).toMatch(/did not answer/i)
    expect(p.textContent).not.toMatch(/\b0\b/)
    expect(p.textContent).not.toMatch(/MiB/)
    // The replica that WAS read draws its figures.
    expect(panel('swarm-graph-0').textContent).toMatch(/64 \/ 256 MiB/)
    expect(panel('swarm-graph-0').textContent).toMatch(/128 \/ 512 MiB/)
    // One bordered panel per replica; the one not read is drawn apart from the read ones.
    expect(panel('swarm-graph-0').classList.contains('c-card')).toBe(true)
    expect(p.classList.contains('c-card')).toBe(true)
    expect(p.style.borderStyle).toBe('dashed')
    expect(panel('swarm-graph-0').style.borderStyle).toBe('')
  })

  it('swarm-graph not answering at all still shows the latency tile, read from Firestore', async () => {
    const down = body({ replicas: [], stats: { state: 'not_read', reason: 'timeout' }, facts: {} })
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok(down)} />)
    await open()
    expect(document.querySelectorAll('[data-replica]')).toHaveLength(0)
    const line = document.querySelector('[data-stats="not_read"]')
    expect(line?.getAttribute('data-reason')).toBe('timeout')
    expect(line?.textContent).toMatch(/swarm-graph was not read: did not answer in time/)
    expect(tile('p95 served / uncached').textContent).toMatch(/152 ms/)
    expect(tile('p95 served / uncached').textContent).toMatch(/7\.4× reference/)
    expect(tile('Answer hit rate, 24 h').getAttribute('data-form')).toBe('unknown')
    expect(tile('Answer hit rate, 24 h').textContent).not.toMatch(/0%/)
  })
})

describe('the reuse tile (§5.2, BGD Q4)', () => {
  it('is a dash with no carried count until carry forward exists', async () => {
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok()} />)
    await open()
    const t = tile('Reused from the last index')
    expect(t.getAttribute('data-form')).toBe('unknown')
    expect(t.querySelector('b')?.textContent).toBe('—')
    expect(t.textContent).not.toMatch(/carried/i)
    expect(t.textContent).not.toMatch(/0%/)
    expect(tile('Answer hit rate, 24 h').querySelector('b')?.textContent).toBe('91%')
    expect(tile('Answer hit rate, 24 h').querySelector('i')?.textContent).toBe('of 1,284 answers')
  })

  it('shows the share and the carried count once there is one', async () => {
    const f = { ...(body().facts as Record<string, unknown>), carried: 300, hit: 868, reused_share: 300 / 1168 }
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok(body({ facts: f }))} />)
    await open()
    expect(tile('Reused from the last index').textContent).toMatch(/26%/)
    expect(tile('Reused from the last index').textContent).toMatch(/300 carried/)
  })
})

describe('a failed read (§5.5)', () => {
  it('keeps the last good values, marked with their time and the reason the new read failed', async () => {
    const load = vi.fn<(repoId: string, page: number | null) => Promise<Result<GraphCacheDoc>>>()
    load.mockResolvedValueOnce(ok()).mockResolvedValue(failed())
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={load} />)
    await open()
    expect(tile('Answer hit rate, 24 h').textContent).toMatch(/91%/)
    await tick(30_000)
    expect(load).toHaveBeenCalledTimes(2)
    expect(tile('Answer hit rate, 24 h').querySelector('b')?.textContent).toBe('91%')
    expect(tile('Answer hit rate, 24 h').querySelector('i')?.textContent).toBe('of 1,284 answers')
    expect(panel('swarm-graph-0').textContent).toMatch(/64 \/ 256 MiB/)
    const age = document.querySelector('[data-read-age]')
    expect(age?.textContent).toMatch(/read 30 s ago/)
    expect(document.querySelector('[data-read-failed]')?.textContent).toMatch(/could not be reached/)
  })
})

describe('an index not promoted yet (§5.1)', () => {
  it('says so and reads nothing', async () => {
    const load = vi.fn(async () => ok())
    render(<RepoGraphCache repoId={REPO} indexSha={null} load={load} />)
    await open()
    await tick(60_000)
    expect(screen.getByText('No index promoted yet')).toBeTruthy()
    expect(load).not.toHaveBeenCalled()
  })
})

describe('the shard list (§5.2)', () => {
  it('is read only when unfolded, changed shards first', async () => {
    const shards = {
      state: 'read', comparison: 'read', total: 2, changed: 1, page: 1, page_rows: 100,
      rows: [
        { layer: 'symbols', module: 'src/a', blob: `sha256:${'ab'.repeat(32)}`, raw_bytes: 2048, last_merge: 'changed', replicas: [{ replica: 'swarm-graph-0', hot: true }], hits_24h: 4, last_read: null },
        { layer: 'callers', module: 'src/b', blob: `sha256:${'cd'.repeat(32)}`, raw_bytes: 1024, last_merge: 'unchanged', replicas: [{ replica: 'swarm-graph-0', hot: false }], hits_24h: 90, last_read: null },
      ],
    }
    const load = vi.fn(async (_repo: string, page: number | null) => ok(page === null ? body() : body({ shards })))
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={load} />)
    await open()
    expect(load).toHaveBeenLastCalledWith(REPO, null)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^Shards/ }))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(load).toHaveBeenLastCalledWith(REPO, 1)
    expect(screen.getByRole('button', { name: /^Shards/ }).textContent).toMatch(/Shards \(2\) · 1 changed by the last merge/)
    const rows = Array.from(document.querySelectorAll('[data-shard-row]'))
    expect(rows.map((r) => r.getAttribute('data-last-merge'))).toEqual(['changed', 'unchanged'])
    // The shared scrolling table (components.css .c-tbl), so a wide list scrolls inside the card.
    expect(rows[0]!.closest('table')?.parentElement?.classList.contains('c-tbl')).toBe(true)
  })
})

describe('the repository card mounts it when indexing is on (§5.1)', () => {
  afterEach(() => vi.unstubAllEnvs())

  it('draws a folded section on an indexed card, none on a card with indexing off, and reads only once opened', async () => {
    vi.useRealTimers()
    const ON = 'repo_0a1b2c3d4e5f6071'
    const OFF = 'repo_9f8e7d6c5b4a3921'
    const calls = serve((m, url) => {
      if (m !== 'GET') return null
      if (url === '/v1/repositories') {
        return {
          status: 200,
          body: {
            repositories: [
              repo(),
              repo({ repo_id: OFF, repo: 'example-docs' }, { interval_hours: 'off', on_change: 'off', current_sha: null, head_sha: null, last_indexed_at: null }),
            ],
          },
        }
      }
      if (url.startsWith(`/v1/repositories/${ON}/graph-cache`)) return { status: 200, body: body({ repo_id: ON }) }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={null} go={vi.fn()} />)
    await waitFor(() => expect(document.querySelectorAll('.ur-cards > .ur-card').length).toBeGreaterThanOrEqual(2), { timeout: 4000 })
    const cards = Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card'))
    const on = cards.find((c) => c.id === `ur-card-${ON}`)!
    const off = cards.find((c) => c.id === `ur-card-${OFF}`)!
    expect(off.querySelector('[data-cache-section]')).toBeNull()
    expect(on.querySelector('[data-cache-section]')).not.toBeNull()
    expect(calls.some((c) => c.url.includes('/graph-cache'))).toBe(false)

    fireEvent.click(within(on).getByRole('button', { name: /^Cache/ }))
    await waitFor(() => expect(on.querySelector('[data-replica="swarm-graph-1"]')).not.toBeNull(), { timeout: 4000 })
    expect(calls.filter((c) => c.url.includes('/graph-cache'))).toHaveLength(1)
    expect(on.querySelector('[data-replica="swarm-graph-1"]')?.getAttribute('data-reason')).toBe('timeout')
  })
})

describe('the fixture build draws the hard case', () => {
  it('serves one replica not read, no carried count, and a shard page only when asked', async () => {
    vi.useRealTimers()
    expect(import.meta.env.DEV, 'this case reads the fixture build').toBeTruthy()
    vi.unstubAllEnvs()
    vi.resetModules()
    const api = await import('../api')
    const plain = await api.loadRepositoryGraphCache('repo_5e1f0c2a9b3d4e61')
    expect(plain.status).toBe('ok')
    const doc = (plain as { data: GraphCacheDoc }).data
    const notRead = doc.replicas.filter((r) => r.state === 'not_read')
    expect(notRead).toHaveLength(1)
    expect(doc.replicas.length).toBeGreaterThan(1)
    expect(doc.facts.carried).toBeNull()
    expect(doc.shards).toBeNull()
    const paged = await api.loadRepositoryGraphCache('repo_5e1f0c2a9b3d4e61', 1)
    const shards = (paged as { data: GraphCacheDoc }).data.shards
    expect(shards?.state).toBe('read')
    if (shards?.state === 'read') {
      expect(shards.rows.length).toBeGreaterThan(0)
      expect(shards.rows[0]!.last_merge).toBe('changed')
    }
  })
})

// The served answer latency (lane graph-cache-panel-latency, part of #2688):
// the route merges every replica's `answer_latency` into `facts`, and the
// section draws it in its tiles, a table by outcome and temperature, and each
// replica's panel. A replica not read is named, never counted as zero.
describe('the served answer latency', () => {
  const g = (count: number, p50: number | null, p95: number | null, max: number | null, exact = true) => ({ count, p50_ms: p50, p95_ms: p95, p99_ms: p95, max_ms: max, exact })
  const served = (over: Record<string, unknown> = {}) => ({
    state: 'read', replicas_read: 2, not_read: [], window_hours: 24, ...g(1284, 8, 40, 410, false),
    by_outcome: { hit: g(1168, 6, 25, 90, false), carried: g(12, 5, 9, 9), miss: g(104, 70, 300, 410, false), partial: g(0, null, null, null), uncached: g(0, null, null, null) },
    by_temperature: { warm: g(1160, 6, 25, 90, false), cold: g(124, 80, 380, 410, false) },
    ...over,
  })
  const withServed = (answerLatency: Record<string, unknown>, replicas?: unknown[]) => {
    const b = body()
    return body({ facts: { ...(b.facts as Record<string, unknown>), answer_latency: answerLatency }, ...(replicas === undefined ? {} : { replicas }) })
  }

  it('draws the merged figures as upper bounds, by outcome with carried apart, and cold or warm', async () => {
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok(withServed(served()))} />)
    await open()
    const t = tile('Answer latency, served')
    expect(t.getAttribute('data-form')).toBe('value')
    expect(t.querySelector('b')?.textContent).toBe('≤ 8 ms / ≤ 40 ms')
    expect(t.textContent).toMatch(/of 1,284 answers · slowest 410 ms/)
    const table = screen.getByRole('table', { name: /Served answer latency/ })
    const row = (k: string) => table.querySelector<HTMLElement>(`[data-latency-row="${k}"]`)!
    expect(row('carried').textContent).toBe('Carried from the last index125 ms9 ms9 ms')
    expect(row('hit').textContent).toContain('≤ 25 ms')
    expect(row('cold').textContent).toContain('≤ 380 ms')
    // Nothing cut at the deadline: dashes, never a 0 ms.
    expect(row('partial').textContent).not.toMatch(/0 ms/)
    expect(row('partial').querySelectorAll('.c-dash')).toHaveLength(3)
    // The indexer's uncached measurement is still its own tile.
    expect(tile('p95 served / uncached').textContent).toMatch(/152 ms/)
  })

  it('names a replica whose figure was not read, and draws its panel with no latency', async () => {
    const replicas = [
      { replica: 'swarm-graph-0', state: 'read', started_at: null, uptime_seconds: 60, result_bytes: MiB, result_bound: 4 * MiB, shard_bytes: MiB, shard_bound: 4 * MiB, answers_hot: 1, shards_hot: 1, last_warm: null, answer_latency: { ...g(5, 4, 9, 12), by_temperature: { cold: g(1, 12, 12, 12) } } },
      { replica: 'swarm-graph-1', state: 'not_read', reason: 'timeout' },
    ]
    const doc = withServed(served({ replicas_read: 1, not_read: ['swarm-graph-1'], ...g(5, 4, 9, 12) }), replicas)
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok(doc)} />)
    await open()
    const t = tile('Answer latency, served')
    expect(t.getAttribute('data-form')).toBe('partial')
    expect(t.querySelector('b')?.textContent).toBe('4 ms / 9 ms')
    expect(t.textContent).toMatch(/1 of 2 replicas; not read: swarm-graph-1/)
    expect(panel('swarm-graph-0').querySelector('[data-row="replica-latency"]')?.textContent).toBe('Answer latency: middle 4 ms, slow 9 ms, slowest 12 ms over 5 answers · cold slow 12 ms')
    expect(panel('swarm-graph-1').textContent).not.toMatch(/latency|0 ms/i)
  })

  it('is not read, with its reason and no figure, when no replica served it', async () => {
    render(<RepoGraphCache repoId={REPO} indexSha="4c1e9a0000" load={async () => ok(withServed({ state: 'not_read', reason: 'no_replica_read', not_read: ['swarm-graph-0'] }))} />)
    await open()
    const t = tile('Answer latency, served')
    expect(t.getAttribute('data-form')).toBe('unknown')
    expect(t.textContent).toMatch(/Not read: no replica was read \(0 of 1 replicas; not read: swarm-graph-0\)/)
    expect(t.textContent).not.toMatch(/\d+ ms/)
    expect(screen.queryByRole('table', { name: /Served answer latency/ })).toBeNull()
    // A body with no figure at all (an older swarm-api) reads as not read too.
    expect(normGraphCache(body()).facts.answer_latency).toEqual({ state: 'not_read', reason: 'bad_response', not_read: [] })
  })

  it('renders from the fixture build: two replicas merged, the third named not read', async () => {
    vi.useRealTimers()
    vi.unstubAllEnvs()
    vi.resetModules()
    const api = await import('../api')
    const read = await api.loadRepositoryGraphCache('repo_5e1f0c2a9b3d4e61')
    expect(read.status).toBe('ok')
    const doc = (read as { data: GraphCacheDoc }).data
    const s = doc.facts.answer_latency
    expect(s.state).toBe('read')
    if (s.state === 'read') {
      expect(s.not_read).toEqual(['swarm-graph-2'])
      expect(s.exact).toBe(false)
      expect(s.by_outcome.carried?.p95_ms).toBeNull()
    }
    render(<RepoGraphCache repoId="repo_5e1f0c2a9b3d4e61" indexSha="4c1e9a0000" load={async () => read} />)
    fireEvent.click(opener())
    await waitFor(() => expect(document.querySelector('[data-row="answer-latency"]')).not.toBeNull())
    expect(tile('Answer latency, served').textContent).toMatch(/2 of 3 replicas; not read: swarm-graph-2/)
    expect(panel('swarm-graph-0').querySelector('[data-row="replica-latency"]')?.textContent).toMatch(/middle 6 ms, slow 30 ms/)
  })
})
