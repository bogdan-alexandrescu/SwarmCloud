// WORK › REPOSITORIES › ONE REPOSITORY (repositories.html screen 4, pick A:
// tabs Overview, Graph, Impact, Test map, Hot-spots, Index runs, Settings,
// Used by; and
// screen 11, Settings A: cards for the schedule, languages, graph, selection
// policy and the token that resolves, with its capability row).
//
// WHAT EACH CASE HOLDS:
//   * the head names the repository, its freshness and Index now; the meta
//     line names the commit each figure describes; behind the head, a banner
//     leads saying by how much and whether a run is in flight;
//   * the eight tabs, in the picked order, each its own address;
//   * Overview's figures come from the index document; Modules, Hot-spots,
//     Index runs, the schedule and Used by are cards;
//   * each region whose route is not there yet says so and names the route,
//     while the rest of the page still draws;
//   * Settings: the resolved token's metadata (scope, account, kind, last 4,
//     expiry, last verified, order R2) and its eight capabilities, an unknown
//     one grey with its reason and never "ok"; the policy shows P3 and X2;
//     changing anything is disabled with the reason;
//   * no element, attribute or text carries a token-shaped value, even when
//     the API serves one.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { DAY, HOUR, MIN, ago, repo, serve, sha, token, tokenShapedIn, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_1111111111111111'

const DETAIL = {
  repository: {
    ...repo(
      { repo_id: ID, repo: 'example-web', created_by: 'operator@swarm.example.com', created_at: '2026-10-04T09:00:00Z' },
      { current_sha: sha('9f8e7d6'), head_sha: sha('5c4b3a2'), behind_by: 3, coverage: 0.61, interval_hours: 12, in_flight_task_id: 'task_inflight' },
    ),
    graph: { depth: 3, min_confidence: 0.2 },
    selection_policy: { policy: 'P3', mode: 'X2', inherited_from_tenant: false },
  },
  index_runs: [
    { task_id: 'task_inflight', commit_sha: sha('5c4b3a2'), kind: 'incremental', trigger: 'change', state: 'RUNNING', queued_at: ago(4 * MIN), started_at: ago(4 * MIN) },
    { task_id: 'task_b', commit_sha: sha('9f8e7d6'), kind: 'incremental', trigger: 'change', state: 'SUCCEEDED', queued_at: ago(3 * HOUR), started_at: ago(3 * HOUR), ended_at: ago(3 * HOUR - 372_000) },
    { task_id: 'task_c', commit_sha: sha('77aa1b0'), kind: 'full', trigger: 'interval', state: 'FAILED', queued_at: ago(DAY), started_at: ago(DAY), ended_at: ago(DAY - 1_800_000), end_cause: 'timed_out' },
  ],
  used_by: [
    { kind: 'run', id: 'run_88', title: 'checkout totals round wrong', index_sha: sha('9f8e7d6'), state: 'DONE', role: 'planner' },
    { kind: 'workflow', id: 'wf_cart', title: 'cart-refactor', index_sha: sha('9f8e7d6'), state: 'RUNNING' },
  ],
}

const INDEX = {
  commit_sha: sha('9f8e7d6'),
  bytes: 212 * 1024,
  truncated: ['hot_spots'],
  modules: [
    { path: 'src/pages', purpose: 'route components, one per URL', files: 88, lines: 9000 },
    { path: 'src/api', purpose: 'typed client', files: 23, lines: 2000 },
  ],
  entry_points: [{ name: 'web', path: 'src/main.tsx' }, { name: 'ssr', path: 'server/main.ts' }],
  hot_spots: [{ path: 'src/api/client.ts', changes: 41 }, { path: 'src/state/cart.ts', changes: 23 }],
  co_changes: [{ a: 'src/api/client.ts', b: 'src/api/types.ts', times: 27 }],
  test_map: [
    { source: 'src/api/**', tests: ['tests/api/client.test.ts'], evidence: 'import' },
    { source: 'src/state/cart.ts', tests: ['tests/state/cart.test.ts', 'tests/e2e/checkout.spec.ts'], evidence: 'naming' },
  ],
  unmapped: ['src/legacy/old.ts'],
}

const RESOLVED = {
  order: 'R2',
  token: token({ token_id: 'tok_r', scope: 'repository', repo_ids: [ID], forge_login: 'example-web-bot', last4: '7f3a', provider_suffix: 'git-r-1111111111111111', secret_name: 'swarm-tenant-eng-git-r-1111111111111111', expires_at: new Date(Date.now() + 41 * DAY + HOUR).toISOString(), verified_at: ago(3 * HOUR) }),
  capabilities: {
    clone: { state: 'ok', reason: 'Measured by a read the token was allowed to make' },
    push: { state: 'ok', reason: 'Push advertisement accepted' },
    open_prs: { state: 'unknown', reason: 'Fine-grained grants are not readable from the API; the role allows it' },
    read_checks: { state: 'ok', reason: 'check-runs answered 200' },
    merge: { state: 'missing', reason: 'The branch rules restrict merges on main to the merge App' },
    close_issues: { state: 'maybe', reason: 'an unexpected word' },
    read_issues: { state: 'ok', reason: 'issues answered 200' },
  },
  user_token: token({ token_id: 'tok_u', scope: 'user', forge_login: 'operator-gh', last4: '19c2' }),
}

// The resolved-token row is derived from GET /v1/git-tokens (R2: the repository's
// own token, else the tenant's): the API has no per-repository token route, and
// no per-language route either, so neither is ever fetched.
const TOKENS = {
  resolution_order: 'R2',
  git_tokens: [
    {
      ...RESOLVED.token,
      probe: { repositories: [{ repo_id: ID, repository: 'example-org/example-web', capabilities: RESOLVED.capabilities }] },
    },
  ],
}

type Routes = Partial<Record<'detail' | 'index' | 'tokens', { status: number; body: unknown }>>

function routes(over: Routes = {}) {
  const r: Required<Routes> = {
    detail: { status: 200, body: DETAIL },
    index: { status: 200, body: INDEX },
    tokens: { status: 200, body: TOKENS },
    ...over,
  }
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === `/v1/repositories/${ID}`) return r.detail
    if (url === `/v1/repositories/${ID}/index?format=json`) return r.index
    if (url === '/v1/git-tokens') return r.tokens
    return null
  })
}

async function mount(tab: string | null = null, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const view = tab === null ? `repo=${ID}` : `repo=${ID}&tab=${tab}`
  const utils = render(<RepositoriesScreen view={view} go={go} />)
  return { ...utils, go }
}

afterEach(() => vi.unstubAllEnvs())

const loaded = () => waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)

describe('the repository page leads with what the index says about the head (Detail A)', () => {
  it('heads the page with the name, the freshness pill and Index now; the meta names each commit', async () => {
    routes()
    await mount()
    await loaded()
    const head = document.querySelector('.c-phead')!
    expect(head.querySelector('.c-pill')?.getAttribute('data-fresh')).toBe('behind')
    expect(visible(head.querySelector('.c-pill'))).toBe('3 behind')
    const meta = visible(document.querySelector('.ur-meta'))
    expect(meta).toContain('Default branch main')
    expect(meta).toContain('index 9f8e7d6')
    expect(meta).toContain('head 5c4b3a2, read 2m ago')
    expect(meta).toContain('claude-code')
    expect(meta).toContain('registered by operator@swarm.example.com, 2026-10-04')
    expect(visible(document.querySelector('.ur-crumb'))).toBe('Work › Repositories › example-org/example-web')
  })

  it('leads with a banner when the index is behind, saying a run is in flight', async () => {
    routes()
    await mount()
    await loaded()
    const banner = document.querySelector('.c-banner')!
    expect(visible(banner)).toContain('Behind by 3 commits.')
    expect(visible(banner)).toContain('An index run is in flight')
  })

  it('draws the eight tabs in the picked order, each with its own address', async () => {
    routes()
    await mount()
    await loaded()
    const tabs = Array.from(document.querySelectorAll<HTMLAnchorElement>('.ur-tabs a'))
    // Graph and Impact (screens 8 and 9) sit after Overview, as their frames draw them.
    expect(tabs.map((t) => visible(t.querySelector('.c-tab-label')))).toEqual(['Overview', 'Graph', 'Impact', 'Test map', 'Hot-spots', 'Index runs', 'Settings', 'Used by'])
    expect(tabs.map((t) => t.getAttribute('href'))).toEqual([
      `/repositories/${ID}`,
      `/repositories/${ID}/graph`,
      `/repositories/${ID}/impact`,
      `/repositories/${ID}/test-map`,
      `/repositories/${ID}/hot-spots`,
      `/repositories/${ID}/index-runs`,
      `/repositories/${ID}/settings`,
      `/repositories/${ID}/used-by`,
    ])
    expect(visible(tabs[3]!.querySelector('em'))).toBe('61%')
    expect(visible(tabs[5]!.querySelector('em'))).toBe('3')
    expect(visible(tabs[7]!.querySelector('em'))).toBe('2')
    expect(tabs[0]!.getAttribute('aria-current')).toBe('page')
  })

  it('Overview: the figures, then Modules and Hot-spots, then Index runs and the schedule with Used by', async () => {
    routes()
    await mount()
    await loaded()
    await waitFor(() => expect(document.querySelector('.ur-kv')).not.toBeNull(), WAIT)
    const kv = visible(document.querySelector('.ur-kv'))
    expect(kv).toContain('Modules 2')
    expect(kv).toContain('Entry points 2')
    expect(kv).toContain('Tests mapped 61%')
    expect(kv).toContain('Index 212 KiB')
    expect(kv).toContain('truncated: hot_spots')
    const titles = Array.from(document.querySelectorAll('.ur-detail .c-card h2')).map((h) => visible(h))
    expect(titles).toEqual(['Modules', 'Hot-spots', 'Index runs', 'Schedule and change trigger'])
    expect(visible(document.querySelector('.ur-runs'))).toContain('5c4b3a2')
    expect(visible(document.querySelector('.ur-runs'))).toContain('timed_out')
    const sched = visible(document.querySelector('.ur-sched'))
    expect(sched).toContain('Re-index every 12 h')
    expect(sched).toContain('Change trigger poll main, at most one run per 30 min')
    expect(sched).toContain('Full run every 7 days')
    expect(sched).toContain('Paused no')
    expect(visible(document.querySelector('.ur-used'))).toContain('checkout totals round wrong')
    expect(visible(document.querySelector('.ur-cochange'))).toContain('27 times')
  })

  it('the index document not being served is said in its region; the rest still draws', async () => {
    routes({ index: { status: 404, body: { detail: 'Not Found' } } })
    await mount()
    await loaded()
    await waitFor(() => expect(document.querySelector('[data-notserved]')).not.toBeNull(), WAIT)
    expect(document.querySelector('[data-notserved]')!.getAttribute('data-notserved')).toBe('GET /v1/repositories/{repo_id}/index')
    expect(document.querySelector('.ur-runs')).not.toBeNull()
  })

  it('a detail served without index_runs or used_by draws a dash and "not served", never 0 or "none"', async () => {
    const { index_runs: _runs, used_by: _used, ...bare } = DETAIL
    void _runs
    void _used
    routes({ detail: { status: 200, body: bare } })
    await mount()
    await loaded()
    const tab = (name: string) => Array.from(document.querySelectorAll<HTMLElement>('.ur-tabs a')).find((a) => a.querySelector('.c-tab-label')?.textContent === name)!
    for (const name of ['Index runs', 'Used by']) {
      const em = tab(name).querySelector('em')!
      expect(em.textContent).not.toContain('0')
      expect(em.querySelector('.c-dash')).not.toBeNull()
    }
    expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id} index_runs"]')).not.toBeNull()
    expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id} used_by"]')).not.toBeNull()
    expect(document.body.textContent).not.toContain('No index runs yet.')
    expect(document.body.textContent).not.toContain('No run or workflow has used this index yet.')
  })

  it('the repository route not being served is said, naming it', async () => {
    routes({ detail: { status: 404, body: { detail: 'Not Found' } } })
    await mount()
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id}"]')).not.toBeNull(), WAIT)
  })

  it('a missing repository (the API\'s own 404) is not found, not "not served"', async () => {
    routes({ detail: { status: 404, body: { code: 'not_found', message: 'No repository repo_1111111111111111.' } } })
    await mount()
    await waitFor(() => expect(document.body.textContent).toContain('No repository repo_1111111111111111.'), WAIT)
    expect(document.querySelector('[data-notserved]')).toBeNull()
  })
})

describe('the other tabs', () => {
  it('Test map lists each source path with its tests and evidence, and filters by path', async () => {
    routes()
    await mount('test-map')
    await loaded()
    await waitFor(() => expect(document.querySelectorAll('.ur-tmap-row')).toHaveLength(2), WAIT)
    expect(visible(document.querySelector('.ur-tmap-row'))).toContain('tests/api/client.test.ts')
    expect(visible(document.querySelector('.ur-tmap-row'))).toContain('import')
    expect(visible(document.querySelector('.ur-tmap'))).toContain('src/legacy/old.ts')
    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter by path' }), { target: { value: 'cart' } })
    expect(document.querySelectorAll('.ur-tmap-row')).toHaveLength(1)
  })

  it('Hot-spots lists every hot-spot with its change count', async () => {
    routes()
    await mount('hot-spots')
    await loaded()
    await waitFor(() => expect(document.querySelectorAll('.ur-hs')).toHaveLength(2), WAIT)
    expect(visible(document.querySelectorAll('.ur-hs')[0]!)).toContain('src/api/client.ts')
    expect(visible(document.querySelectorAll('.ur-hs')[0]!)).toContain('41')
  })

  it('Index runs lists every run with its state mark, commit, kind, trigger and duration', async () => {
    routes()
    await mount('index-runs')
    await loaded()
    const rows = Array.from(document.querySelectorAll('.ur-runs .ur-run'))
    expect(rows).toHaveLength(3)
    expect(rows[0]!.querySelector('[data-mark]')?.getAttribute('data-mark')).toBe('running')
    expect(visible(rows[1]!)).toContain('6m 12s')
    expect(rows[2]!.querySelector('[data-mark]')?.getAttribute('data-mark')).toBe('failed')
    // An unfinished run has no duration yet: a dash with its reason.
    expect(rows[0]!.querySelector('.c-dash')?.getAttribute('title')).toBe('Still running')
  })

  it('Used by lists the runs and workflows that read this index, linking each', async () => {
    routes()
    await mount('used-by')
    await loaded()
    const links = Array.from(document.querySelectorAll<HTMLAnchorElement>('.ur-used a'))
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/runs/run_88', '/workflows/wf_cart'])
  })
})

describe('Settings A: schedule, languages, graph, selection policy, and the token that resolves', () => {
  it('draws the five cards', async () => {
    routes()
    await mount('settings')
    await loaded()
    await waitFor(() => expect(document.querySelector('.ur-tokrow')).not.toBeNull(), WAIT)
    const titles = Array.from(document.querySelectorAll('.ur-detail .c-card h2')).map((h) => visible(h))
    expect(titles).toEqual(['Schedule and change trigger', 'Languages detected', 'Graph', 'Selection policy', 'Resolved token'])
  })

  it('languages: the API serves no per-language route, so the region says "not served yet" and nothing is fetched', async () => {
    const calls = routes()
    await mount('settings')
    await loaded()
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories/{repo_id}/languages"]')).not.toBeNull(), WAIT)
    expect(document.querySelectorAll('.ur-lang')).toHaveLength(0)
    expect(calls.some((c) => c.url.includes('/languages'))).toBe(false)
  })

  it('the policy is P3 with X2, the graph depth 3 of 1-6; changing them is disabled with the reason', async () => {
    routes()
    await mount('settings')
    await loaded()
    const policy = document.querySelector('.ur-policy')!
    expect(within(policy as HTMLElement).getByRole('radio', { name: 'P3 gate with fallback' }).getAttribute('aria-checked')).toBe('true')
    expect(within(policy as HTMLElement).getByRole('radio', { name: "X2 run in this repository's GitHub Actions" }).getAttribute('aria-checked')).toBe('true')
    expect(visible(document.querySelector('.ur-graph'))).toContain('Graph depth')
    expect(visible(document.querySelector('.ur-graph'))).toContain('3 of 1-6')
    expect(visible(document.querySelector('.ur-graph'))).toContain('0.2')
    // The route is named on the element; the words say it in plain language (walkthrough E).
    const locked = document.querySelector('.ur-locked')!
    expect(locked.getAttribute('data-route')).toBe('PATCH /v1/repositories/{repo_id}')
    expect(visible(locked)).toContain('cannot be changed from this console yet')
    expect(visible(locked)).not.toContain('/v1/')
  })

  it('the resolved token: scope, account, kind, last 4, expiry, last verified, order R2; never the value', async () => {
    routes()
    await mount('settings')
    await loaded()
    await waitFor(() => expect(document.querySelector('.ur-tokrow')).not.toBeNull(), WAIT)
    const card = document.querySelector('.ur-resolved')!
    const text = visible(card)
    expect(text).toContain('repository token example-web-bot')
    expect(text).toContain('fine-grained PAT ··· 7f3a')
    expect(text).toContain('expires in 41 days')
    expect(text).toContain('last verified 3h ago')
    expect(text).toContain('order R2: repository token, then tenant token')
    // No record in the list is marked yours, so no attribution line is drawn.
    expect(text).not.toContain('your user token')
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
  })

  it('the capability row: eight capabilities, an unknown or unrecognised one grey with its reason, never ok', async () => {
    routes()
    await mount('settings')
    await loaded()
    await waitFor(() => expect(document.querySelector('.ur-tokrow')).not.toBeNull(), WAIT)
    const caps = Array.from(document.querySelectorAll<HTMLElement>('.ur-tokrow .ur-cap'))
    expect(caps.map((c) => visible(c))).toEqual(['Clone', 'Push branches', 'Open PRs', 'Read checks', 'Merge', 'Close issues', 'Read issues', 'Workflow dispatch'])
    expect(caps.map((c) => c.getAttribute('data-cap'))).toEqual(['ok', 'ok', 'unknown', 'ok', 'missing', 'unknown', 'ok', 'unknown'])
    expect(caps[4]!.getAttribute('title')).toBe('Merge: missing. The branch rules restrict merges on main to the merge App')
    expect(caps[7]!.getAttribute('title')).toBe('Workflow dispatch: unknown. Not served for this pair')
  })

  it('a region whose route is not there yet names its route; the others still draw', async () => {
    const calls = routes({ tokens: { status: 501, body: {} } })
    await mount('settings')
    await loaded()
    await waitFor(() => expect(document.querySelectorAll('[data-notserved]')).toHaveLength(2), WAIT)
    expect(Array.from(document.querySelectorAll('[data-notserved]')).map((e) => e.getAttribute('data-notserved'))).toEqual([
      'GET /v1/repositories/{repo_id}/languages',
      'GET /v1/git-tokens',
    ])
    expect(calls.some((c) => c.url.includes('/languages') || c.url.includes('/token?'))).toBe(false)
    expect(document.querySelector('.ur-policy')).not.toBeNull()
  })
})
