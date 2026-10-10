// WORK › ACCESS: D6's opt-in push test, D5's token owners, and an install
// someone else has to approve (#780; docs/onboarding.md §2.3, §2.4, §3.2).
//
// WHAT EACH CASE HOLDS:
//   * Push test is offered on write grants only, asks before it writes, and
//     posts {checks: ['push_test']} only once the person confirmed; Cancel
//     posts nothing;
//   * its answer is drawn as ok, missing or unknown -- unknown never a pass --
//     with the failure's §2.3 copy, and a branch the delete left behind is
//     named so the person can delete it;
//   * the policy no longer says the write test is not offered;
//   * an owner enabled through the person's token says "via token";
//   * an org with no installation offers Request install, which opens the
//     App's install page (GitHub shows a non-owner its request form there)
//     and records the request with POST .../install-request;
//   * an org whose install was requested draws ORG_APPROVAL_PENDING's copy
//     word for word, with Re-check and Withdraw.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`
const INSTALL_URL = 'https://github.com/apps/swarmcloud/installations/new'
const BRANCH = 'swarmcloud/onboarding-check-' + '0a1b2c3d4e5f6071'

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: 'swarm-tenant-eng-git-u-89abcdef01234567',
  state: 'active',
  failure: null,
  access_expires_at: new Date(Date.now() + 7 * 3600_000).toISOString(),
  refresh_expires_at: null,
  refreshed_at: new Date(Date.now() - 600_000).toISOString(),
  refreshing: false,
  created_at: new Date().toISOString(),
  connected_at: new Date().toISOString(),
  revoked_at: null,
}

const ORG = (owner: string, over: Record<string, unknown> = {}) => ({
  owner,
  method: 'app',
  owner_type: owner === 'octo-dev' ? 'User' : 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  requested_at: null,
  checked_at: null,
  ...over,
})

const GRANT = (n: number, repository: string, mode: 'read' | 'write', checks: Record<string, unknown> = {}) => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks,
  verified_at: null,
})

const OK = (extra: Record<string, unknown> = {}) => ({ state: 'ok', code: null, checked_at: null, ...extra })

const READ_GRANT = GRANT(1, 'example-org/example-docs', 'read', { clone: OK(), push: { state: 'not_required', code: null, checked_at: null } })
const WRITE_GRANT = GRANT(2, 'example-org/example-service', 'write', { clone: OK(), push: OK(), pull_request: OK() })

const PENDING_COPY =
  "You asked example-wait's owners to install SwarmCloud. Nothing can be read in example-wait until one of them approves it in example-wait's settings › GitHub Apps. You can carry on with your other orgs; this step re-checks on its own every 15 minutes, or press Re-check."

const PUSH_MISSING_COPY =
  'You chose write for example-org/example-service, but GitHub does not let octo-dev push there through SwarmCloud. Ask for write access to example-org/example-service, or change the grant to read.'

const UNREACHABLE_COPY =
  'GitHub did not answer this check, so its result is unknown, not failed. It is retried on the next pass; press Re-check to try now.'

function overview() {
  return {
    tenant_id: 'eng',
    connection: CONNECTION,
    orgs: [ORG('example-org'), ORG('octo-dev'), ORG('example-tok', { method: 'pat', install_state: 'token', installation_id: null })],
    requested: [ORG('example-wait', { install_state: 'requested', installation_id: null, enabled: false, code: 'ORG_APPROVAL_PENDING', copy: PENDING_COPY })],
    grants: [READ_GRANT, WRITE_GRANT],
  }
}

const OWNERS = {
  tenant_id: 'eng',
  orgs_listed: true,
  install_url: INSTALL_URL,
  owners: [
    { ...ORG('octo-dev'), install_url: null },
    { ...ORG('example-org'), install_url: null },
    { ...ORG('example-tok', { method: 'pat', install_state: 'token', installation_id: null, repository_selection: null }), install_url: null },
    { ...ORG('example-new', { installation_id: null, repository_selection: null, install_state: 'not_installed', enabled: false }), install_url: INSTALL_URL },
    {
      ...ORG('example-wait', { installation_id: null, repository_selection: null, install_state: 'requested', enabled: false }),
      install_url: INSTALL_URL,
      requested_at: new Date(Date.now() - 3600_000).toISOString(),
      code: 'ORG_APPROVAL_PENDING',
      copy: PENDING_COPY,
    },
  ],
}

type Handler = (m: string, url: string, body: unknown) => { status: number; body: unknown } | null

function api(extra: Handler = () => null) {
  return serve((m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: overview() }
    if (m === 'GET' && url === '/v1/access/orgs') return { status: 200, body: OWNERS }
    if (m === 'GET' && url.startsWith('/v1/access/orgs/example-org/repositories')) {
      return { status: 200, body: { tenant_id: 'eng', owner: 'example-org', repositories: [], page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: 0 } }
    }
    return null
  })
}

/** A verify answer for the write grant, its push_test check set to `check`. */
function pushAnswer(check: Record<string, unknown>, failures: unknown[] = [], pushed: Record<string, unknown> | null = null) {
  const body: Record<string, unknown> = {
    tenant_id: 'eng',
    grant: { ...WRITE_GRANT, checks: { ...WRITE_GRANT.checks, push_test: check } },
    failures,
    passed: failures.length === 0 && check.state === 'ok',
  }
  if (pushed !== null) body.push_test = pushed
  return body
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  return render(<AccessScreen />)
}

const grantRow = (repository: string) => document.querySelector(`li.ac-grant[data-repo="${repository}"]`) as HTMLElement
const owner = (name: string) => document.querySelector(`li.ac-owner[data-owner="${name}"]`) as HTMLElement
const pushCell = (repository: string) => grantRow(repository).querySelector('[data-check="push_test"]') as HTMLElement
const verifyPosts = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method === 'POST' && c.url.endsWith('/verify'))

async function grid() {
  return screen.findByRole('list', { name: 'Your granted repositories, failures first' })
}

async function runPushTest(answer: unknown) {
  const calls = api((m, url) => (m === 'POST' && url === `/v1/access/grants/${ID(2)}/verify` ? { status: 200, body: answer } : null))
  await mount()
  await grid()
  fireEvent.click(within(grantRow('example-org/example-service')).getByRole('button', { name: 'Push test' }))
  fireEvent.click(await within(grantRow('example-org/example-service')).findByRole('button', { name: 'Run push test' }))
  await waitFor(() => expect(verifyPosts(calls)).toHaveLength(1))
  return calls
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('the opt-in push test (D6)', () => {
  it('is offered on write grants only, and the policy no longer says it is not offered', async () => {
    api()
    await mount()
    await grid()
    expect(within(grantRow('example-org/example-docs')).queryByRole('button', { name: 'Push test' })).toBeNull()
    expect(pushCell('example-org/example-docs').querySelector('.c-dash')?.getAttribute('aria-label')).toContain('read grant')
    expect(within(grantRow('example-org/example-service')).getByRole('button', { name: 'Push test' })).toBeTruthy()
    const d6 = visible(document.querySelector('[data-rule="D6"]'))
    expect(d6).not.toContain('not offered yet')
    expect(d6).toContain('swarmcloud/onboarding-check-')
    expect(visible(document.body)).not.toContain('not served by this API yet')
  })

  it('asks before it writes, and Cancel posts nothing', async () => {
    const calls = api()
    await mount()
    await grid()
    fireEvent.click(within(grantRow('example-org/example-service')).getByRole('button', { name: 'Push test' }))
    const row = grantRow('example-org/example-service')
    expect(visible(row)).toContain('swarmcloud/onboarding-check-')
    expect(visible(row)).toContain('example-org/example-service')
    expect(verifyPosts(calls)).toEqual([])
    fireEvent.click(within(row).getByRole('button', { name: 'Cancel' }))
    await new Promise((r) => setTimeout(r, 20))
    expect(verifyPosts(calls)).toEqual([])
    expect(within(row).queryByRole('button', { name: 'Run push test' })).toBeNull()
  })

  it('posts only push_test once confirmed, and draws ok', async () => {
    const calls = await runPushTest(pushAnswer(OK({ branch: BRANCH, leftover: false }), [], { branch: BRANCH, leftover: false }))
    expect(verifyPosts(calls)).toEqual([expect.objectContaining({ url: `/v1/access/grants/${ID(2)}/verify`, body: { checks: ['push_test'] } })])
    await waitFor(() => expect(pushCell('example-org/example-service').querySelector('[data-cap]')?.getAttribute('data-cap')).toBe('ok'))
    expect(visible(grantRow('example-org/example-service'))).not.toContain('left behind')
  })

  it("draws missing with the failure's §2.3 copy", async () => {
    await runPushTest(
      pushAnswer({ state: 'missing', code: 'PERMISSION_MISSING', checked_at: null }, [{ check: 'push_test', code: 'PERMISSION_MISSING', copy: PUSH_MISSING_COPY }]),
    )
    expect(await screen.findByText(PUSH_MISSING_COPY)).toBeTruthy()
    expect(pushCell('example-org/example-service').querySelector('[data-cap]')?.getAttribute('data-cap')).toBe('missing')
  })

  it('draws unknown as unknown, never as a pass', async () => {
    await runPushTest(
      pushAnswer({ state: 'unknown', code: 'FORGE_UNREACHABLE', checked_at: null }, [{ check: 'push_test', code: 'FORGE_UNREACHABLE', copy: UNREACHABLE_COPY }]),
    )
    expect(await screen.findByText(UNREACHABLE_COPY)).toBeTruthy()
    expect(pushCell('example-org/example-service').querySelector('[data-cap]')?.getAttribute('data-cap')).toBe('unknown')
  })

  it('names a branch the delete left behind', async () => {
    await runPushTest(pushAnswer(OK({ branch: BRANCH, leftover: true }), [], { branch: BRANCH, leftover: true }))
    await waitFor(() => expect(visible(grantRow('example-org/example-service'))).toContain(BRANCH))
    expect(visible(grantRow('example-org/example-service'))).toContain('left behind')
  })
})

describe('owners: via token, and an install someone else approves', () => {
  it('says an owner enabled through the person\'s token is via token', async () => {
    api()
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    expect(visible(owner('example-tok'))).toContain('via token')
    expect(visible(owner('example-org'))).not.toContain('via token')
  })

  it('Request install opens the install page and records the request', async () => {
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
    const calls = api((m, url) =>
      m === 'POST' && url === '/v1/access/orgs/example-new/install-request'
        ? { status: 200, body: { tenant_id: 'eng', recorded: true, install_url: INSTALL_URL, org: ORG('example-new', { install_state: 'requested', enabled: false }) } }
        : null,
    )
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    fireEvent.click(within(owner('example-new')).getByRole('button', { name: 'Request install' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST' && c.url === '/v1/access/orgs/example-new/install-request')).toHaveLength(1))
    expect(opened).toHaveBeenCalledWith(INSTALL_URL, '_blank', 'noopener,noreferrer')
  })

  it('draws ORG_APPROVAL_PENDING\'s copy word for word on a requested org, with Re-check and Withdraw', async () => {
    api()
    await mount()
    await screen.findByRole('list', { name: 'Owners your connection reaches' })
    const row = owner('example-wait')
    expect(visible(row)).toContain(PENDING_COPY)
    expect(visible(row)).toContain('ORG_APPROVAL_PENDING')
    expect(within(row).getByRole('button', { name: 'Re-check' })).toBeTruthy()
    expect(within(row).getByRole('button', { name: 'Withdraw request' })).toBeTruthy()
    expect(within(row).queryByRole('button', { name: 'Enable' })).toBeNull()
  })
})
