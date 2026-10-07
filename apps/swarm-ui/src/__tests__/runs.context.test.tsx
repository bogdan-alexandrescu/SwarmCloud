// AN ISSUE RUN'S PAGE: THE CONTEXT CARD AND THE SELECTED-TESTS GATE ROW
// (repositories.html screen 5, pick B: "a Context card above the plan: the
// index used, its freshness, the modules and impact it found"; screen 10,
// pick C = merge policy P3: "selected tests N of M passed · full suite:
// fallback reason / not needed").
//
// WHAT EACH CASE HOLDS:
//   * the card sits above the plan and names what the planner read: the repo
//     index, the issue and the open work;
//   * WHICH index version the planner was given is not served (the run
//     carries no `index_sha`), so it is a dash with that reason -- the card
//     shows the repository's index NOW, said as now, with its freshness;
//   * a repository not registered here says so, and reads no index;
//   * with a pull request, the impact is asked ONCE (POST .../impact with the
//     PR number and nothing else) and the PR card carries the P3 gate row:
//     N of M selected, passed a dash (the check's result is not served), the
//     full suite not needed or the fallback with its reason, and Impact;
//   * every route not there yet is said in place and named.
//
// MUTATIONS: put the card below the plan; print the current index as the one
// the planner used; ask impact on every poll; print "passed 12" from the
// selection; drop the fallback reason -- each turns a case red.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { digest, issueRun } from './runfixture'
import { repo, sha } from './repofixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const RUN_ID = 'run_4c1e09d2'
const REPO_ID = 'repo_0a1b2c3d4e5f6071'
const HEAD = sha('e7f8a9b')

const REG = repo({ repo_id: REPO_ID, repo: 'infra' })
const INDEX = {
  commit_sha: sha('a1b2c3d'),
  modules: [{ path: 'apps/swarm-api', files: 80 }, { path: 'apps/swarm-ui', files: 120 }, { path: 'terraform', files: 40 }],
}

const OPEN_WORK = {
  repository: 'example-org/infra',
  read_at: '2026-10-02T14:02:00Z',
  issues: [{ number: 498, title: 'a' }, { number: 501, title: 'b' }],
  issues_truncated: false,
  pull_requests: [{ number: 507, title: 'c', files: [], files_truncated: false }],
  pull_requests_truncated: false,
}

const ISSUE_READ = {
  title: 'Sum step spend', labels: [], state: 'open', comments: 4, url: 'https://github.com/example-org/infra/issues/512',
  body: 'Body.', body_truncated: false, body_redacted: false, read_at: '2026-10-02T14:02:00Z',
}

const PLAN = {
  plan_id: 'plan_1', pull_request: 57, base_sha: sha('5c4b3a2'), head_sha: HEAD, index_sha: sha('a1b2c3d'),
  stale: false, freshness: { state: 'current' }, policy: 'P3', depth: 3, min_confidence: 0.2,
  changed_symbols: 4, affected_callers: 17, targeted: 12, selected: 12, total_tests: 1480, selection: 'targeted',
  full_suite: null, diff: [{ path: 'a.py', status: 'modified', patch: true, added: [], removed: [] }, { path: 'b.py', status: 'modified', patch: true, added: [], removed: [] }, { path: 'c.py', status: 'added', patch: true, added: [], removed: [] }],
  changed: [], affected: [], tests: [], fallback_triggers: [], unindexed: [], low_confidence_cut: [], lists_cut: [],
}

const FALLBACK = {
  ...PLAN,
  selection: 'full_suite', selected: 1480,
  full_suite: { command: 'uv run pytest -q', because: ['shared_fixture_changed'] },
  fallback_triggers: [{ kind: 'shared_fixture_changed', path: 'tests/conftest.py', reason: 'tests/conftest.py is a shared fixture (used by 214 tests)' }],
}

type R = { status: number; body: unknown } | null

function serve(run: unknown, over: { repos?: R; index?: R; impact?: R } = {}) {
  const calls: { method: string; url: string; body: unknown }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null })
    let r: R = null
    if (method === 'GET' && url === `/v1/runs/${RUN_ID}`) r = { status: 200, body: { run } }
    else if (method === 'GET' && url === '/v1/repositories') r = over.repos !== undefined ? over.repos : { status: 200, body: { repositories: [REG] } }
    else if (method === 'GET' && url === `/v1/repositories/${REPO_ID}/index?format=json`) r = over.index !== undefined ? over.index : { status: 200, body: INDEX }
    else if (method === 'POST' && url === `/v1/repositories/${REPO_ID}/impact`) r = over.impact !== undefined ? over.impact : { status: 200, body: PLAN }
    const res = r ?? { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(res.body), { status: res.status, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  return calls
}

async function mount(run: unknown, over: Parameters<typeof serve>[1] = {}) {
  const calls = serve(run, over)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return { ...utils, calls }
}

const card = () => document.querySelector<HTMLElement>('.rc-ctx')!
const line = (what: string) => card().querySelector<HTMLElement>(`[data-ctx="${what}"]`)!

const planned = () => issueRun({ issue_read: ISSUE_READ, open_work: OPEN_WORK })
const checking = () =>
  issueRun({
    state: 'CHECKING', issue_read: ISSUE_READ, open_work: OPEN_WORK,
    approved_by: 'operator@example.com', approved_at: '2026-10-02T14:10:00Z', approved_digest: digest('a1'),
    pull_request: { number: 57, url: 'https://github.com/example-org/infra/pull/57', head_sha: HEAD, checks: 'pending' },
  })

afterEach(() => vi.unstubAllEnvs())

describe('the Context card (screen 5, pick B)', () => {
  it('sits above the plan and names what the planner read', async () => {
    await mount(planned())
    await waitFor(() => expect(card()).not.toBeNull(), WAIT)
    const plan = document.querySelector('.rn-plan')!
    expect(card().compareDocumentPosition(plan) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(visible(card().querySelector('h3'))).toBe('Context the planner was given')
    expect(visible(line('issue'))).toBe('Issue #512 with 4 comments')
    expect(visible(line('open-work'))).toBe('Open work 2 issues, 1 pull request, read when the run was created')
  })

  it('shows the repository’s index NOW with its freshness and modules, and the version given as a dash with its reason', async () => {
    await mount(planned())
    await waitFor(() => expect(visible(line('index'))).toContain('3 modules'), WAIT)
    const text = visible(line('index'))
    expect(text).toContain('index now at a1b2c3d')
    expect(text).toContain('test map 20 of 83 modules')
    expect(line('index').querySelector('.c-pill')?.getAttribute('data-fresh')).toBe('current')
    const given = line('index').querySelector('.c-dash')!
    expect(given.getAttribute('title')).toMatch(/index_sha/)
    expect(card().querySelector('a.rc-open')?.getAttribute('href')).toBe(`/repositories/${REPO_ID}`)
    // No pull request yet: nothing to ask the impact of.
    expect(visible(line('impact'))).toContain('no pull request yet')
  })

  it('says a repository that is not registered has no index, and reads none', async () => {
    const { calls } = await mount(planned(), { repos: { status: 200, body: { repositories: [repo({ repo: 'another' })] } } })
    await waitFor(() => expect(visible(line('index'))).toContain('example-org/infra is not registered in this tenant'), WAIT)
    expect(calls.some((c) => c.url.includes('/index'))).toBe(false)
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('says the registrations route is not served yet, naming it', async () => {
    await mount(planned(), { repos: null })
    await waitFor(() => expect(line('index').querySelector('[data-notserved="GET /v1/repositories"]')).not.toBeNull(), WAIT)
  })
})

describe('the selected-tests gate row on the PR card (screen 10, policy P3)', () => {
  const gate = (k: string) => document.querySelector<HTMLElement>(`.rn-ci .rc-gate[data-gate="${k}"]`)!

  it('asks the impact of the pull request once, by number alone, and draws N of M with passed a dash', async () => {
    const { calls } = await mount(checking())
    await waitFor(() => expect(gate('selected')).not.toBeNull(), WAIT)
    await waitFor(() => expect(visible(gate('selected'))).toContain('12 of 1,480 selected'), WAIT)
    const passed = gate('selected').querySelector('.c-dash')!
    expect(passed.getAttribute('title')).toMatch(/swarmcloud\/selected-tests/)
    expect(visible(gate('selected'))).toContain('passed')
    expect(visible(gate('selected'))).not.toMatch(/passed \d/)
    // An unserved conclusion draws the ring: never the succeeded mark.
    expect(gate('selected').querySelector('[data-tone]')?.getAttribute('data-tone')).toBe('unknown')
    expect(gate('full').querySelector('[data-tone]')?.getAttribute('data-tone')).toBe('unknown')
    expect(visible(gate('full'))).toContain('Full suite')
    expect(visible(gate('full'))).toContain('not required for this change')
    expect(visible(document.querySelector('.rn-ci .rc-gate-h'))).toContain('policy P3')
    expect(document.querySelector('.rn-ci .rc-gate-h a')?.getAttribute('href')).toBe(`/repositories/${REPO_ID}/impact?pr=57`)
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts.map((c) => c.body)).toEqual([{ pull_request: 57 }])
    // The Context card reads the same answer, not a second query.
    await waitFor(() => expect(visible(line('impact'))).toContain('pull request #57: 3 files · 4 changed symbols · 17 affected callers · 12 of 1,480 tests selected'), WAIT)
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1)
  })

  it('names the fallback and its reason when the change falls back to the full suite', async () => {
    await mount(checking(), { impact: { status: 200, body: FALLBACK } })
    await waitFor(() => expect(visible(gate('selected'))).toContain('Selected tests → full suite'), WAIT)
    expect(visible(gate('selected'))).toContain('fallback · 1,480 tests')
    expect(visible(gate('selected'))).toContain('fallback reason: tests/conftest.py is a shared fixture (used by 214 tests)')
    expect(document.querySelector('.rn-ci .rc-gate[data-gate="full"]')).toBeNull()
  })

  it('says the impact route is not served yet, naming it, and the card still draws', async () => {
    await mount(checking(), { impact: null })
    await waitFor(() => expect(gate('selected')?.querySelector('[data-notserved="POST /v1/repositories/{repo_id}/impact"]')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.rn-ci'))).toContain('fix rounds')
  })
})
