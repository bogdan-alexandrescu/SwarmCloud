// WORK › REPOSITORIES › ONE REPOSITORY › SETTINGS: "your user token (…last4)
// is used only for attribution" (owner decision 2026-10-05, U1 follow-up).
//
// GET /v1/git-tokens marks each record `yours` for the caller, computed by
// the API from the verified identity. The line is drawn from the record so
// marked and from nothing else.
//
// WHAT EACH CASE HOLDS:
//   * the caller's own user token draws the line, with its last 4 when known;
//   * another member's user token (`yours: false`, no email) draws nothing,
//     nor does a record from an API that does not mark `yours` at all;
//   * a revoked own token, or one narrowed away from this repository, draws
//     nothing: it is not what attributes work here;
//   * no element, attribute or text carries a token-shaped value, even when
//     the API serves one.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { HOUR, ago, repo, serve, sha, token, tokenShapedIn, visible } from './repofixture'
import { normResolvedFromTokens, normToken } from '../RepositoriesData'

const WAIT = { timeout: 4000 }
const ID = 'repo_1111111111111111'
const OTHER = 'repo_2222222222222222'

const DETAIL = {
  repository: {
    ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6'), behind_by: 0 }),
    selection_policy: { policy: 'P3', mode: 'X2', inherited_from_tenant: false },
  },
}

const REPO_TOKEN = token({ token_id: 'tok_r', scope: 'repository', repo_ids: [ID], forge_login: 'example-web-bot', last4: '7f3a', yours: false, verified_at: ago(HOUR) })
const MINE = token({ token_id: 'tok_mine', scope: 'user', forge_login: 'operator-gh', last4: '19c2', yours: true, owner: 'token-owner@swarm.example.com' })
const THEIRS = token({ token_id: 'tok_theirs', scope: 'user', forge_login: 'colleague-gh', last4: 'b7e1', yours: false, owner: null, user: null })

function routes(tokens: unknown[]) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
    if (url === '/v1/git-tokens') return { status: 200, body: { resolution_order: 'R2', git_tokens: tokens } }
    return null
  })
}

async function settings() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)
  await waitFor(() => expect(visible(document.querySelector('.ur-resolved'))).toContain('order R2'), WAIT)
  return visible(document.querySelector('.ur-resolved'))
}

afterEach(() => vi.unstubAllEnvs())

describe('the attribution line is drawn from the record marked yours', () => {
  it('draws the line for the owner, with the last 4 known', async () => {
    routes([REPO_TOKEN, THEIRS, MINE])
    const text = await settings()
    expect(text).toContain('your user token (…19c2) is used only for attribution')
    // Not another member's last 4, login or email.
    expect(text).not.toContain('b7e1')
    expect(text).not.toContain('colleague-gh')
    expect(document.documentElement.outerHTML).not.toContain('token-owner@swarm.example.com')
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
  })

  it('draws no line for anyone but the owner', async () => {
    routes([REPO_TOKEN, THEIRS])
    const text = await settings()
    expect(text).not.toContain('your user token')
    expect(text).not.toContain('attribution')
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
  })

  it('draws no line from a record the API did not mark yours', async () => {
    const unmarked = { ...MINE }
    delete (unmarked as Record<string, unknown>).yours
    routes([REPO_TOKEN, unmarked, { ...THEIRS, yours: 'true' }])
    const text = await settings()
    expect(text).not.toContain('your user token')
  })

  it('without a known last 4 the line says so by leaving it out', async () => {
    routes([REPO_TOKEN, { ...MINE, last4: null }])
    const text = await settings()
    expect(text).toContain('your user token is used only for attribution')
    expect(text).not.toContain('(…')
  })
})

describe('which of the caller’s records attributes work in this repository', () => {
  const list = (...rows: unknown[]) => ({ resolution_order: 'R2', git_tokens: [REPO_TOKEN, ...rows] })

  it('normToken keeps yours only when it is exactly true, and the owner email as served', () => {
    expect(normToken(MINE)?.yours).toBe(true)
    expect(normToken(MINE)?.owner).toBe('token-owner@swarm.example.com')
    expect(normToken(THEIRS)?.yours).toBe(false)
    expect(normToken(THEIRS)?.owner).toBeNull()
    expect(normToken({ ...MINE, yours: 'true' })?.yours).toBe(false)
    expect(normToken({ ...MINE, yours: 1 })?.yours).toBe(false)
  })

  it('a revoked or expired own token is not the attribution', () => {
    expect(normResolvedFromTokens(list({ ...MINE, state: 'revoked' }), ID).user_token).toBeNull()
    expect(normResolvedFromTokens(list({ ...MINE, state: 'expired' }), ID).user_token).toBeNull()
  })

  it('an own token narrowed to other repositories is not the attribution here', () => {
    expect(normResolvedFromTokens(list({ ...MINE, repo_ids: [OTHER] }), ID).user_token).toBeNull()
    expect(normResolvedFromTokens(list({ ...MINE, repo_ids: [OTHER, ID] }), ID).user_token?.token_id).toBe('tok_mine')
    expect(normResolvedFromTokens(list(MINE), ID).user_token?.token_id).toBe('tok_mine')
  })

  it('the own token is found even when nothing resolves to clone with', () => {
    const r = normResolvedFromTokens({ resolution_order: 'R2', git_tokens: [MINE] }, ID)
    expect(r.token).toBeNull()
    expect(r.user_token?.token_id).toBe('tok_mine')
  })

  it('a record yours but of another scope is not a user token', () => {
    expect(normResolvedFromTokens(list({ ...REPO_TOKEN, token_id: 'tok_t', scope: 'tenant', yours: true }), ID).user_token).toBeNull()
  })
})
