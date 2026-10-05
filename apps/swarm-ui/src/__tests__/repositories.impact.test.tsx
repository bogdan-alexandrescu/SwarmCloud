// WORK › REPOSITORIES › ONE REPOSITORY › IMPACT (repositories.html screen 9,
// pick A: four columns, diff -> changed symbols -> callers -> tests to run).
//
// WHAT EACH CASE HOLDS:
//   * the tab asks POST .../impact for a pull request or a commit -- the body
//     names the change and nothing else (invariant 10: no paths, no tests,
//     no command from the caller);
//   * the four columns carry the plan's own counts at their heads, and every
//     test its reason and evidence;
//   * a cut path is stated in a banner, not hidden; a fallback plan's test
//     column is the full suite with its trigger;
//   * the head says how fresh the index the plan used is, and the meta line
//     names base, head, depth, the confidence floor and the policy;
//   * opened from a PR card (`?pr=57`) it asks for that pull request at once;
//   * the route not there yet is said in place, naming it.
//
// MUTATIONS: send `paths` in the body; count the listed callers instead of
// `affected_callers`; drop the cut banner; list targeted tests on a fallback
// plan; ignore `pr` from the address -- each turns a case red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_0a1b2c3d4e5f6071'
const DETAIL = { repository: repo({ repo_id: ID }), index_runs: [], used_by: [] }

const T = (name: string) => `tests/core/test_orders.py#${name}`

const PLAN = {
  plan_id: 'plan_1',
  pull_request: 57,
  commit: null,
  base_sha: sha('5c4b3a2'),
  head_sha: sha('e7f8a9b'),
  index_sha: sha('a1b2c3d'),
  stale: false,
  freshness: { state: 'current' },
  policy: 'P3',
  depth: 3,
  min_confidence: 0.2,
  changed_symbols: 4,
  affected_callers: 17,
  targeted: 12,
  selected: 12,
  total_tests: 1480,
  selection: 'targeted',
  full_suite: null,
  diff: [
    { path: 'src/core/orders.py', status: 'modified', patch: true, added: [{ start: 90, end: 107 }], removed: [{ start: 90, end: 95 }] },
    { path: 'src/core/money.py', status: 'modified', patch: true, added: [{ start: 20, end: 23 }], removed: [{ start: 21, end: 21 }] },
    { path: 'tests/core/test_orders.py', status: 'modified', patch: true, added: [{ start: 1, end: 22 }], removed: [] },
  ],
  diff_truncated: false,
  changed: [
    { id: 'src/core/orders.py#OrderService.total', kind: 'method', path: 'src/core/orders.py', start_line: 88, end_line: 131, side: 'base', lines: [], why: 'modified' },
    { id: 'src/core/orders.py#OrderService._apply_tax', kind: 'method', path: 'src/core/orders.py', start_line: 133, end_line: 150, side: 'base', lines: [], why: 'modified' },
    { id: 'src/core/money.py#Money.round', kind: 'method', path: 'src/core/money.py', start_line: 20, end_line: 34, side: 'base', lines: [], why: 'modified' },
  ],
  unindexed: [{ path: 'src/core/money.py', lines: [{ start: 36, end: 40 }], reason: 'added lines no index has a symbol for' }],
  affected: [
    { id: 'src/api/orders.py#checkout_total', path: 'src/api/orders.py', depth: 1, confidence: 0.95, evidence: 'lsp', reaches: 'src/core/orders.py#OrderService.total' },
    { id: 'workers/invoices.py#InvoiceJob.run', path: 'workers/invoices.py', depth: 2, confidence: 0.6, evidence: 'ast', reaches: 'src/core/orders.py#OrderService.total' },
  ],
  tests: [
    { id: T('test_empty_cart_total'), command: null, reason: 'calls OrderService.total directly', evidence: ['lsp'], confidence: 0.95, source: 'walk' },
    { id: T('test_checkout_rounds_half_even'), command: null, reason: 'reaches Money.round via checkout_total, depth 2', evidence: ['lsp', 'lsp'], confidence: 0.9, source: 'walk' },
  ],
  fallback_triggers: [],
  unmapped: [],
  low_confidence_cut: [{ from: 'src/api/routes.py#admin_recalc', to: 'src/core/orders.py#OrderService.total', depth: 1, confidence: 0.3, evidence: 'ast' }],
  bound: { depth: 3, max_nodes: 2000, node_cap_hit: false, stopped_at_depth: [] },
  lists_cut: [],
}

const FALLBACK = {
  ...PLAN,
  pull_request: null,
  commit: sha('9d8c7b6'),
  stale: true,
  behind_by: 3,
  freshness: { state: 'stale', reason: 'built more than 7 days ago and behind the head' },
  selection: 'full_suite',
  selected: 1480,
  full_suite: { command: 'uv run pytest -q', because: ['shared_fixture_changed', 'stale_index'] },
  fallback_triggers: [
    { kind: 'shared_fixture_changed', path: 'tests/conftest.py', reason: 'tests/conftest.py is a shared fixture: any test may use it, and the graph does not follow fixtures' },
    { kind: 'stale_index', reason: 'the index is stale: built more than 7 days ago and behind the head' },
  ],
  low_confidence_cut: [],
}

function routes(impact: { status: number; body: unknown } | null = { status: 200, body: PLAN }) {
  return serve((m, url) => {
    if (m === 'GET' && url === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
    if (m === 'POST' && url === `/v1/repositories/${ID}/impact`) return impact
    return null
  })
}

async function mount(extra = '') {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={`repo=${ID}&tab=impact${extra}`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-api'), WAIT)
  return utils
}

const impact = () => document.querySelector<HTMLElement>('.ri-impact')!
const cols = () => Array.from(document.querySelectorAll<HTMLElement>('.ri-col'))

async function askForPr(n: string) {
  fireEvent.click(within(impact()).getByRole('radio', { name: 'Pull request' }))
  fireEvent.change(within(impact()).getByLabelText('Pull request number'), { target: { value: n } })
  fireEvent.click(within(impact()).getByRole('button', { name: 'Show impact' }))
  await waitFor(() => expect(cols()).toHaveLength(4), WAIT)
}

afterEach(() => vi.unstubAllEnvs())

describe('the Impact tab asks for one change and nothing else', () => {
  it('posts the pull request number alone', async () => {
    const calls = routes()
    await mount()
    await askForPr('57')
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts.map((c) => c.body)).toEqual([{ pull_request: 57 }])
  })

  it('posts a commit as its full sha, and refuses a short one before asking', async () => {
    const calls = routes({ status: 200, body: FALLBACK })
    await mount()
    fireEvent.click(within(impact()).getByRole('radio', { name: 'Commit' }))
    const input = within(impact()).getByLabelText('Commit sha')
    fireEvent.change(input, { target: { value: '9d8c7b6' } })
    fireEvent.click(within(impact()).getByRole('button', { name: 'Show impact' }))
    expect(visible(impact())).toContain('the full 40-character sha')
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
    fireEvent.change(input, { target: { value: sha('9d8c7b6') } })
    fireEvent.click(within(impact()).getByRole('button', { name: 'Show impact' }))
    await waitFor(() => expect(cols()).toHaveLength(4), WAIT)
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.body)).toEqual([{ commit: sha('9d8c7b6') }])
  })

  it('asks at once for the pull request a PR card opened it on', async () => {
    const calls = routes()
    await mount('&pr=57')
    await waitFor(() => expect(cols()).toHaveLength(4), WAIT)
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.body)).toEqual([{ pull_request: 57 }])
    expect(visible(impact().querySelector('h2'))).toBe('Pull request #57')
  })
})

describe('four columns (Impact A)', () => {
  it('heads each column with the plan’s own count, and every test with its reason', async () => {
    routes()
    await mount()
    await askForPr('57')
    const heads = cols().map((c) => visible(c.querySelector('h3')))
    expect(heads).toEqual(['3 files in the diff', '4 changed symbols', '17 affected callers', '12 tests to run of 1,480'])
    expect(visible(cols()[0]!)).toContain('src/core/orders.py')
    expect(visible(cols()[0]!)).toContain('modified · +18 −6')
    expect(visible(cols()[1]!)).toContain('OrderService.total')
    expect(visible(cols()[1]!)).toContain('lines 88-131 · modified')
    // A symbol the head added that no index has seen: file level, said so.
    expect(visible(cols()[1]!)).toContain('src/core/money.py')
    expect(visible(cols()[1]!)).toContain('not in index a1b2c3d, file level')
    expect(visible(cols()[2]!)).toContain('InvoiceJob.run')
    expect(visible(cols()[2]!)).toContain('depth 2 · ast 0.6')
    // 17 callers served, 2 listed: the rest is said, never dropped silently.
    expect(visible(cols()[2]!)).toContain('15 more not listed')
    const tests = Array.from(cols()[3]!.querySelectorAll('.ri-it')).map(visible)
    expect(tests[0]).toContain('test_empty_cart_total')
    expect(tests[0]).toContain('because it calls OrderService.total directly (lsp)')
    expect(tests[1]).toContain('because it reaches Money.round via checkout_total, depth 2 (lsp, lsp)')
  })

  it('names base, head, depth, the floor and the policy, and how fresh the index was', async () => {
    routes()
    await mount()
    await askForPr('57')
    const meta = visible(impact().querySelector('.ri-meta'))
    expect(meta).toContain('base 5c4b3a2')
    expect(meta).toContain('head e7f8a9b')
    expect(meta).toContain('depth 3')
    expect(meta).toContain('min confidence 0.2')
    expect(meta).toContain('policy P3 · no fallback')
    expect(visible(impact().querySelector('.ri-phead .c-pill'))).toBe('index current · a1b2c3d')
  })

  it('states a cut path in a banner', async () => {
    routes()
    await mount()
    await askForPr('57')
    const cut = visible(impact().querySelector('.ri-cut'))
    expect(cut).toContain('1 path cut')
    expect(cut).toContain('admin_recalc → OrderService.total is an ast edge at 0.3')
  })

  it('draws a fallback as the full suite with its trigger, and the stale index on the pill', async () => {
    routes({ status: 200, body: FALLBACK })
    await mount()
    fireEvent.click(within(impact()).getByRole('radio', { name: 'Commit' }))
    fireEvent.change(within(impact()).getByLabelText('Commit sha'), { target: { value: sha('9d8c7b6') } })
    fireEvent.click(within(impact()).getByRole('button', { name: 'Show impact' }))
    await waitFor(() => expect(cols()).toHaveLength(4), WAIT)
    expect(visible(cols()[3]!.querySelector('h3'))).toBe('1,480 tests to run')
    const items = Array.from(cols()[3]!.querySelectorAll('.ri-it')).map(visible)
    expect(items).toHaveLength(1)
    expect(items[0]).toContain('the full suite')
    expect(items[0]).toContain('tests/conftest.py is a shared fixture')
    expect(visible(impact().querySelector('.ri-meta'))).toContain('policy P3 · fallback: full suite')
    expect(impact().querySelector('.ri-phead .c-pill')?.className).toContain('is-bad')
    expect(visible(impact().querySelector('h2'))).toBe('Commit 9d8c7b6')
  })
})

describe('honest states', () => {
  it('says the impact route is not served yet, naming it', async () => {
    routes(null)
    await mount()
    fireEvent.click(within(impact()).getByRole('radio', { name: 'Pull request' }))
    fireEvent.change(within(impact()).getByLabelText('Pull request number'), { target: { value: '57' } })
    fireEvent.click(within(impact()).getByRole('button', { name: 'Show impact' }))
    await waitFor(() => expect(document.querySelector('[data-notserved="POST /v1/repositories/{repo_id}/impact"]')).not.toBeNull(), WAIT)
    expect(cols()).toHaveLength(0)
  })

  it('shows the forge’s refusal as a failure, not as an empty plan', async () => {
    routes({ status: 403, body: { code: 'forge_forbidden', message: 'the token cannot read pull request 57' } })
    await mount()
    fireEvent.click(within(impact()).getByRole('radio', { name: 'Pull request' }))
    fireEvent.change(within(impact()).getByLabelText('Pull request number'), { target: { value: '57' } })
    fireEvent.click(within(impact()).getByRole('button', { name: 'Show impact' }))
    await waitFor(() => expect(visible(impact())).toContain('the token cannot read pull request 57'), WAIT)
    expect(cols()).toHaveLength(0)
  })
})
