// CONFIG › REPOSITORIES: whether the tenant's agents may file issues on each
// repository (AU-0 REG-FLAG, docs/design/agent-autonomy.md §3.2, owner
// decision Q2).
//
// WHERE THE SWITCH IS (BGD's answer to VQ-043, 2026-10-26: "Show state only on
// the card"). The card says the state in one read-only line; the switch is on
// the repository's own page (the card's Open), Settings tab.
//
// WHAT EACH CASE HOLDS:
//   * each card shows the flag as `to_api` serves it, with no control even for
//     an admin: a registration without the field reads off, a `platform: true`
//     one reads on;
//   * on the repository's page a workspace admin gets a control that PATCHes
//     `{agents_may_file_issues: <the other value>}` alone, then reads again;
//   * a member or a viewer sees the state and has no control, and sends no write;
//   * a refused change is said;
//   * an API that does not serve the field draws a dash and no control.
//
// The bodies are routes/repositories.py's and routes/tenants.py's shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const OFF_ID = 'repo_1111111111111111'
const PLATFORM_ID = 'repo_2222222222222222'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

/** `GET /v1/tenants/me` for the selected workspace `eng`, with the caller's role. */
function me(role: string): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

/** Two registrations as `to_api` serves them: one never set (off), one the platform's own (on). */
function list(served = true): Json {
  const flag = (on: boolean) => (served ? { agents_may_file_issues: on } : {})
  return {
    repositories: [
      repo({ repo_id: OFF_ID, repo: 'example-api', platform: false, ...flag(false) }),
      repo({ repo_id: PLATFORM_ID, repo: 'example-platform', platform: true, ...flag(true) }),
    ],
  }
}

function live(role: string) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: list() }
    if (m === 'PATCH') return { status: 200, body: { repository: {}, tenant_id: 'eng' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mount() {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={null} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.ur-file-issues').length).toBe(2), WAIT)
}

function line(name: string): HTMLElement {
  const card = Array.from(document.querySelectorAll<HTMLElement>('.ur-card')).find((c) => visible(c).includes(name))
  const el = card?.querySelector<HTMLElement>('.ur-file-issues')
  if (el === null || el === undefined) throw new Error(`no agents-may-file-issues line on ${name}`)
  return el
}

/**
 * One repository's Settings tab, the flag held server-side so a PATCH is read
 * back. `fields` is merged into the registration (`{}` serves no flag).
 */
function liveDetail(role: string, fields: Json, opts: { patch?: Reply } = {}) {
  let held = fields
  const calls = serve((m, url, body) => {
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
    if (m === 'GET' && url === `/v1/repositories/${OFF_ID}`) {
      return {
        status: 200,
        body: {
          repository: {
            ...repo({ repo_id: OFF_ID, repo: 'example-api' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6') }),
            graph: { depth: 3, min_confidence: 0.2 },
            selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
            ...held,
          },
          index_runs: [],
          used_by: [],
        },
      }
    }
    if (m === 'PATCH' && path === `/v1/repositories/${OFF_ID}`) {
      if (opts.patch !== undefined) return opts.patch
      held = { ...held, ...(body as Json) }
      return { status: 200, body: { repository: { repo_id: OFF_ID, ...held }, tenant_id: 'eng' } }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountDetail(): Promise<() => HTMLElement> {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={`repo=${OFF_ID}&tab=settings`} go={vi.fn()} />)
  // MUTATION: leave the card out of the Settings tab and this never appears.
  await waitFor(() => expect(document.querySelector('.ur-detail-file-issues')).not.toBeNull(), WAIT)
  return () => document.querySelector<HTMLElement>('.ur-detail-file-issues')!
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('Repositories: the card says whether agents may file issues, and has no switch', () => {
  it.each(['admin', 'member'])('a %s sees each served value read-only', async (role) => {
    const calls = live(role)
    await mount()
    const off = line('example-org/example-api')
    const on = line('example-org/example-platform')
    // MUTATION: draw `on` for every card (drop the served value) and this reads on.
    expect(off.getAttribute('data-file-issues')).toBe('false')
    expect(visible(off)).toBe('Agents may file issues off')
    // MUTATION: ignore the served flag for a platform registration and this reads off.
    expect(on.getAttribute('data-file-issues')).toBe('true')
    expect(visible(on)).toBe('Agents may file issues on')
    // The role is read before the absence is: a card still carrying the switch draws it for an admin at once,
    // and for a member until the role read answers.
    await waitFor(() => expect(calls.some((c) => c.url.split('?')[0] === '/v1/tenants/me')).toBe(true), WAIT)
    // MUTATION: put the Turn on / Turn off back on the card (BGD's VQ-043 pick moved it to the page) -> fails.
    expect(off.querySelector('button')).toBeNull()
    expect(on.querySelector('button')).toBeNull()
    expect(off.closest('.ur-card')!.querySelector('[data-file-issues-set]')).toBeNull()
    expect(off.closest('.ur-card')!.querySelector('[data-role-gate="admin"]')).toBeNull()
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })
})

describe("the repository's page: agents may file issues", () => {
  it('an admin sees the served value and a control that PATCHes the other one alone, then reads again', async () => {
    const calls = liveDetail('admin', { agents_may_file_issues: false })
    const el = await mountDetail()
    expect(el().getAttribute('data-file-issues')).toBe('false')
    expect(visible(el())).toContain('Agents may file issues off')
    // MUTATION: draw no control on the page (the switch stayed on the card) and this times out.
    await waitFor(() => expect(el().querySelector('button')).not.toBeNull(), WAIT)
    expect(visible(el().querySelector('button'))).toBe('Turn on')
    const reads = calls.filter((c) => c.method === 'GET' && c.url === `/v1/repositories/${OFF_ID}`).length
    fireEvent.click(el().querySelector('button')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!
    // MUTATION: PATCH the list route, or another repository's id, and this fails.
    expect(sent.url).toBe(`/v1/repositories/${OFF_ID}`)
    // MUTATION: send the current value (`on` instead of `!on`) or any other field and this fails.
    expect(sent.body).toEqual({ agents_may_file_issues: true })
    // MUTATION: drop the read after a successful change and the registration is not read again.
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET' && c.url === `/v1/repositories/${OFF_ID}`).length).toBeGreaterThan(reads), WAIT)
    await waitFor(() => expect(el().getAttribute('data-file-issues')).toBe('true'), WAIT)
    await waitFor(() => expect(visible(el().querySelector('button'))).toBe('Turn off'), WAIT)
  })

  it.each(['member', 'viewer'])('a %s sees the state and has no control', async (role) => {
    const calls = liveDetail(role, { agents_may_file_issues: true })
    const el = await mountDetail()
    // MUTATION: drop the `admin` gate and this gets a button.
    await waitFor(() => expect(el().querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    expect(el().querySelector('button')).toBeNull()
    // MUTATION: hide the state below admin and this loses it.
    expect(visible(el())).toContain('Agents may file issues on')
    expect(visible(el().querySelector('[data-role-gate="admin"]'))).toBe('set by a workspace admin')
    // MUTATION: send the write on mount or for any role and this is not empty.
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })

  it('says a refused change', async () => {
    liveDetail('admin', { agents_may_file_issues: true }, { patch: { status: 403, body: { code: 'forbidden', message: "only the workspace's admin may set agents_may_file_issues" } } })
    const el = await mountDetail()
    await waitFor(() => expect(el().querySelector('button')).not.toBeNull(), WAIT)
    fireEvent.click(el().querySelector('button')!)
    // MUTATION: drop the failure banner and a 403 changes nothing on screen.
    await waitFor(() => expect(document.body.textContent).toContain('Agents may file issues was not changed'), WAIT)
    expect(el().getAttribute('data-file-issues')).toBe('true')
  })

  it('an API that does not serve the field draws a dash and no control', async () => {
    const calls = liveDetail('admin', {})
    const el = await mountDetail()
    // MUTATION: read a missing field as `false` and this draws "off" with a Turn on button.
    expect(el().getAttribute('data-file-issues')).toBeNull()
    expect(el().querySelector('.c-dash')).not.toBeNull()
    expect(el().querySelector('button')).toBeNull()
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })
})
