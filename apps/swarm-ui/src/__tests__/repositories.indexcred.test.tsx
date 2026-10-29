// WHICH CREDENTIAL INDEXING USES (lane INDEX-CRED; part of #1041).
//
// Owner report 2026-10-13 ~20:10 PDT: "it says all tests pass for
// /repositories/tokens but it can't index the repo in my personal workspace?"
// Try it passed through the owner's GitHub connection while every index run
// of the private repository cloned with a team token the personal workspace
// never had, and nothing on either page said the two used different
// credentials. WHAT EACH CASE HOLDS:
//   * each repository card says what indexing uses -- the owner's GitHub
//     connection with its login, the team token, or none (with where to
//     connect) -- from the API's `index_credential`;
//   * Try it says the same, and the chosen repository's last index result
//     with its reason (`index.last_failure`), so a passing check beside a
//     failing index is explained;
//   * an unregistered repository is said to be unregistered, not "indexed".

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MIN, ago, repo, serve, token, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const OWNER_CRED = { source: 'owner', secret_name: 'swarm-tenant-u-octo-git-u-0123456789abcdef', login: 'octo-dev' }
const TEAM_CRED = { source: 'tenant', secret_name: 'swarm-tenant-eng-git', login: null }
const NO_CRED = { source: 'none', secret_name: null, login: null }

const ACCESS_FAILURE = {
  task_id: 'task_57773968e37d4d6d8300',
  commit_sha: null,
  at: ago(20 * MIN),
  class: 'access',
  cause: "SwarmCloud's GitHub access can't read this repository",
  detail: "remote: Repository not found. fatal: Authentication failed for 'https://github.com/example-org/example-api/'",
  end_cause: 'runner_error',
}

afterEach(() => vi.unstubAllEnvs())

function cards(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card'))
}

async function mountList(go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={null} go={go} />)
  return { go }
}

function listing(cred: Json | undefined, index: Json = {}) {
  serve((m, url) =>
    m === 'GET' && url === '/v1/repositories'
      ? { status: 200, body: { repositories: [repo(cred === undefined ? {} : { index_credential: cred }, index)] } }
      : null,
  )
}

describe('each repository card says which credential indexing uses', () => {
  it("names the owner's GitHub connection and its login", async () => {
    listing(OWNER_CRED)
    await mountList()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    const line = cards()[0]!.querySelector<HTMLElement>('.ur-cred')
    expect(line).not.toBeNull()
    expect(visible(line)).toContain('Indexing uses your GitHub connection (@octo-dev)')
    expect(line!.getAttribute('data-source')).toBe('owner')
  })

  it('names the workspace token (QA2-FIX-19 config2-12: "team" was said in a personal workspace)', async () => {
    listing(TEAM_CRED)
    await mountList()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    expect(visible(cards()[0]!.querySelector('.ur-cred'))).toContain('Indexing uses the workspace token')
  })

  it('says none and where to connect, beside the failure it explains', async () => {
    listing(NO_CRED, { last_failure: ACCESS_FAILURE, failure_streak: { class: 'access', count: 2 } })
    const { go } = await mountList()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    const card = cards()[0]!
    const line = card.querySelector<HTMLElement>('.ur-cred')!
    expect(visible(line)).toContain('Indexing uses none — connect GitHub under Setup › Git tokens')
    expect(visible(card.querySelector('.ur-fail'))).toContain('Last index run failed')
    const link = Array.from(line.querySelectorAll('a')).find((a) => visible(a) === 'Setup › Git tokens')
    fireEvent.click(link!)
    expect(go).toHaveBeenCalled()
  })

  it('draws nothing when the API did not serve it', async () => {
    listing(undefined)
    await mountList()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    expect(cards()[0]!.querySelector('.ur-cred')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Try it, on the Git tokens page
// ---------------------------------------------------------------------------

function step(name: string, state: string, evidence: Json = {}): Json {
  return { step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] }
}

const GRANTS = [
  { repository: 'example-org/example-api', repo_id: 'repo_0a1b2c3d4e5f6071', owner: 'example-org', mode: 'write', archived: false },
  { repository: 'example-org/example-web', repo_id: 'repo_1111111111111111', owner: 'example-org', mode: 'read', archived: false },
]

/** Every step done: Try it said "Every chosen repository passed". */
const ALL_PASSED: Json = {
  tenant_id: 'u-octo',
  user: 'dev@swarm.example.com',
  user_hash: '89abcdef01234567',
  derived_at: new Date().toISOString(),
  next_step: null,
  complete: true,
  source: 'derived on this read',
  steps: [
    step('signed_in', 'done', { email: 'dev@swarm.example.com', tenant_id: 'u-octo', is_admin: false }),
    step('github_connected', 'done', { via: 'user', kind: 'app_user', forge_login: 'octo-dev', token_state: 'active', token_id: 'tok_app' }),
    step('app_installed', 'done', { needed: true, read: true, installed: ['example-org'] }),
    step('orgs_enabled', 'done', { owners: [], orgs_read: true, orgs_capped: false, sso_hidden_orgs: 0, read_at: null, enabled: ['example-org'] }),
    step('repos_chosen', 'done', { repositories: GRANTS, capped: false }),
    step('access_verified', 'done', {
      source: 'grants',
      token_id: 'tok_app',
      repositories: GRANTS.map((g) => ({ ...g, checks: {}, result: 'passed', verified_at: ago(MIN) })),
      unverified: [],
    }),
  ],
}

async function mountTokens(repositories: Json[]) {
  serve((m, url) => {
    if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: ALL_PASSED }
    if (m === 'GET' && url === '/v1/git-tokens') return { status: 200, body: { git_tokens: [token()] } }
    if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: { repositories } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=tokens" go={vi.fn()} />)
  const head = await screen.findByText('Set up GitHub access', { selector: 'h2' }, WAIT)
  return head.closest<HTMLElement>('.ur-steps')!.querySelector<HTMLElement>('.ur-step[data-n="3"]')!
}

describe('Try it says what indexing uses and how the repository last indexed', () => {
  it('a passing check beside a failing index names the credential and the failure', async () => {
    const tryIt = await mountTokens([repo({ index_credential: OWNER_CRED }, { last_failure: ACCESS_FAILURE, backoff: { since: ago(MIN), failures: 2 } })])
    expect(visible(tryIt)).toContain('Every chosen repository passed its clone, push and pull request checks.')
    const note = await within(tryIt).findByTestId('indexing-note', {}, WAIT)
    await waitFor(() => expect(visible(note)).toContain('Indexing uses: your GitHub connection (@octo-dev)'), WAIT)
    expect(visible(note)).toContain('Try it checks the access your own agents use; indexing clones with this.')
    expect(visible(note)).toContain("Last index of example-org/example-api failed 20m ago: SwarmCloud's GitHub access can't read this repository")
    expect(visible(note)).toContain('Polling stopped; Index now retries.')
  })

  it('a repository that indexed says when', async () => {
    const tryIt = await mountTokens([repo({ index_credential: TEAM_CRED }, { last_indexed_at: ago(5 * MIN) })])
    const note = await within(tryIt).findByTestId('indexing-note', {}, WAIT)
    await waitFor(() => expect(visible(note)).toContain('Indexing uses: the workspace token'), WAIT)
    expect(visible(note)).toContain('Last index of example-org/example-api: indexed 5m ago.')
  })

  it('a chosen repository that is not registered says so', async () => {
    const tryIt = await mountTokens([repo({ index_credential: NO_CRED })])
    fireEvent.change(within(tryIt).getByRole('combobox', { name: 'Repository to check' }), { target: { value: 'repo_1111111111111111' } })
    const note = await within(tryIt).findByTestId('indexing-note', {}, WAIT)
    await waitFor(() => expect(visible(note)).toContain('Indexing uses: none — connect GitHub'), WAIT)
    expect(visible(note)).toContain('example-org/example-web is not registered for indexing.')
  })
})
