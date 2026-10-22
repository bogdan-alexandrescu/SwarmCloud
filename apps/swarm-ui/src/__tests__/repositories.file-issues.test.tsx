// WORK › REPOSITORIES: whether the tenant's agents may file issues on each
// repository (AU-0 REG-FLAG, docs/design/agent-autonomy.md §3.2, owner
// decision Q2).
//
// WHAT EACH CASE HOLDS:
//   * each card shows the flag as `to_api` serves it: a registration without
//     the field reads off, a `platform: true` one reads on;
//   * a workspace admin gets a control that PATCHes
//     `{agents_may_file_issues: <the other value>}` alone, then reads again;
//   * a member or a viewer sees the state and has no control, and sends no write;
//   * a refused change is said, naming the repository;
//   * an API that does not serve the field draws a dash and no control.
//
// The bodies are routes/repositories.py's and routes/tenants.py's shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

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

function live(role: string, opts: { served?: boolean; patch?: Reply } = {}) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: list(opts.served ?? true) }
    if (m === 'PATCH') return opts.patch ?? { status: 200, body: { repository: {}, tenant_id: 'eng' } }
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

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('Repositories: agents may file issues', () => {
  it('an admin sees each served value and a control that PATCHes the other one alone', async () => {
    const calls = live('admin')
    await mount()
    const off = line('example-org/example-api')
    const on = line('example-org/example-platform')
    // MUTATION: draw `on` for every card (drop the served value) and this reads on.
    expect(off.getAttribute('data-file-issues')).toBe('false')
    expect(visible(off)).toContain('Agents may file issues off')
    // MUTATION: ignore the served flag for a platform registration and this reads off.
    expect(on.getAttribute('data-file-issues')).toBe('true')
    expect(visible(on)).toContain('Agents may file issues on')
    // MUTATION: draw no control for anyone (drop the `admin ? <Button>` branch) and these are null.
    await waitFor(() => expect(off.querySelector('button')).not.toBeNull(), WAIT)
    expect(visible(off.querySelector('button'))).toBe('Turn on')
    expect(visible(on.querySelector('button'))).toBe('Turn off')
    const reads = calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === '/v1/repositories').length
    fireEvent.click(off.querySelector('button')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!
    // MUTATION: PATCH the list route, or another repository's id, and this fails.
    expect(sent.url).toBe(`/v1/repositories/${OFF_ID}`)
    // MUTATION: send the current value (`on` instead of `!on`) or any other field and this fails.
    expect(sent.body).toEqual({ agents_may_file_issues: true })
    // MUTATION: drop the `reload()` after a successful change and the list is not read again.
    await waitFor(
      () => expect(calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === '/v1/repositories').length).toBeGreaterThan(reads),
      WAIT,
    )
  })

  it.each(['member', 'viewer'])('a %s sees the state and has no control', async (role) => {
    const calls = live(role)
    await mount()
    const off = line('example-org/example-api')
    const on = line('example-org/example-platform')
    // MUTATION: drop the `admin` gate (pass `admin={true}`) and these get a button.
    await waitFor(() => expect(off.querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    expect(off.querySelector('button')).toBeNull()
    expect(on.querySelector('button')).toBeNull()
    // MUTATION: hide the state below admin and these lose it.
    expect(visible(off)).toContain('Agents may file issues off')
    expect(visible(on)).toContain('Agents may file issues on')
    expect(visible(off.querySelector('[data-role-gate="admin"]'))).toBe('set by a workspace admin')
    // MUTATION: send the write on mount or for any role and this is not empty.
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })

  it("says a refused change, naming the repository", async () => {
    live('admin', { patch: { status: 403, body: { code: 'forbidden', message: "only the workspace's admin may set agents_may_file_issues" } } })
    await mount()
    const on = line('example-org/example-platform')
    await waitFor(() => expect(on.querySelector('button')).not.toBeNull(), WAIT)
    fireEvent.click(on.querySelector('button')!)
    // MUTATION: drop the `fileFailed` banner and a 403 changes nothing on screen.
    await waitFor(() => expect(document.body.textContent).toContain('Agents may file issues was not changed'), WAIT)
    expect(document.body.textContent).toContain('example-org/example-platform:')
  })

  it('an API that does not serve the field draws a dash and no control', async () => {
    const calls = live('admin', { served: false })
    await mount()
    const off = line('example-org/example-api')
    // MUTATION: read a missing field as `false` and this draws "off" with a Turn on button.
    expect(off.getAttribute('data-file-issues')).toBeNull()
    expect(off.querySelector('.c-dash')).not.toBeNull()
    expect(off.querySelector('button')).toBeNull()
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })
})
