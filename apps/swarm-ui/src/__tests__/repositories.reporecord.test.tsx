// WORK › REPOSITORIES: `agents_may_file_issues` travels on `RepoRecord`
// (AU0-REPORECORD, part of #1933; docs/design/agent-autonomy.md §3.2).
// AU0-REG-FLAG read the flag off the raw list body inside Repositories.tsx,
// because `normRepo` dropped it, and built its PATCH there too. Now:
//
// WHAT EACH CASE HOLDS:
//   * `normRepo` keeps the flag: true, false, and null when it is absent or
//     not a boolean (null is "not served", never a confident off);
//   * `patchRepositoryFileIssues` PATCHes the one registration with
//     `{agents_may_file_issues: <bool>}` and no other field;
//   * the fixture build answers it, and its list then serves the new value;
//   * the list card draws the flag from the record `loadRepositories`
//     returns (a dash for null), an admin's toggle calls the helper, and a
//     member sees the state with no control;
//   * the repository's Settings tab shows the flag read-only, a dash for null.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const OFF_ID = 'repo_1111111111111111'
const ON_ID = 'repo_2222222222222222'
const UNSERVED_ID = 'repo_3333333333333333'

type Json = Record<string, unknown>

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

afterEach(() => {
  vi.doUnmock('../api')
  vi.unstubAllEnvs()
  vi.resetModules()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('normRepo: agents_may_file_issues', () => {
  it('keeps true and false, and is null when absent or not a boolean', async () => {
    const { normRepo } = await import('../RepositoriesData')
    // MUTATION: leave the field out of normRepo's return and this is undefined.
    expect(normRepo(repo({ agents_may_file_issues: true }))!.agents_may_file_issues).toBe(true)
    // MUTATION: read it as `v.agents_may_file_issues === true || null` and false becomes null.
    expect(normRepo(repo({ agents_may_file_issues: false }))!.agents_may_file_issues).toBe(false)
    // MUTATION: default a missing flag to `false` (`?? false`) and this reads off.
    expect(normRepo(repo({}))!.agents_may_file_issues).toBeNull()
    // MUTATION: copy the value through untyped (`v.agents_may_file_issues as boolean`) and this is "yes".
    expect(normRepo(repo({ agents_may_file_issues: 'yes' }))!.agents_may_file_issues).toBeNull()
  })

  it('survives the list and the detail normalisers', async () => {
    const { normRepoDetail, normRepoList } = await import('../RepositoriesData')
    const list = normRepoList({ repositories: [repo({ repo_id: OFF_ID, agents_may_file_issues: false }), repo({ repo_id: ON_ID, agents_may_file_issues: true })] })
    // MUTATION: rebuild the list's records from a subset of fields and the flags are lost.
    expect(list.map((r) => r.agents_may_file_issues)).toEqual([false, true])
    // MUTATION: drop the flag on the detail's `repository` path and this is null.
    expect(normRepoDetail({ repository: repo({ agents_may_file_issues: true }), index_runs: [], used_by: [] })!.repository.agents_may_file_issues).toBe(true)
  })
})

describe('patchRepositoryFileIssues', () => {
  it.each([true, false])('PATCHes the one registration with {agents_may_file_issues: %s} alone', async (value) => {
    const calls = serve((m) => (m === 'PATCH' ? { status: 200, body: { repository: {}, tenant_id: 'eng' } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const api = await import('../api')
    const res = await api.patchRepositoryFileIssues(OFF_ID, value)
    // MUTATION: answer as not served in the live build (the fixture branch unconditionally) and this is an error.
    expect(res.status).toBe('ok')
    expect(calls).toHaveLength(1)
    // MUTATION: send it as a PUT or a POST and this fails.
    expect(calls[0]!.method).toBe('PATCH')
    // MUTATION: PATCH the list route, or interpolate the wrong id, and this fails.
    expect(calls[0]!.url).toBe(`/v1/repositories/${OFF_ID}`)
    // MUTATION: send the whole record, nest it under `index`, or send `!value`, and this fails.
    expect(calls[0]!.body).toEqual({ agents_may_file_issues: value })
  })

  it('the fixture build answers it, and its list then serves the new value', async () => {
    vi.resetModules()
    const api = await import('../api')
    expect(api.USE_FIXTURES).toBe(true)
    const before = await api.loadRepositories()
    if (before.status !== 'ok') throw new Error(`fixture list read ${before.status}`)
    const first = before.data[0]!
    // MUTATION: leave the flag out of the fixture's served registrations and this is null.
    expect(first.agents_may_file_issues).toBe(false)
    const res = await api.patchRepositoryFileIssues(first.repo_id, true)
    // MUTATION: answer the fixture write with `repoNotServed` and this is an error.
    expect(res.status).toBe('ok')
    const after = await api.loadRepositories()
    if (after.status !== 'ok') throw new Error(`fixture list read ${after.status}`)
    // MUTATION: answer ok without keeping the change and the next read still says off.
    expect(after.data.find((r) => r.repo_id === first.repo_id)!.agents_may_file_issues).toBe(true)
    // MUTATION: set the flag on every fixture registration and the others turn on too.
    expect(after.data.filter((r) => r.repo_id !== first.repo_id).map((r) => r.agents_may_file_issues)).toEqual(
      before.data.filter((r) => r.repo_id !== first.repo_id).map((r) => r.agents_may_file_issues),
    )
  })
})

describe('the list card reads the flag from RepoRecord', () => {
  /**
   * The list served through a stubbed `loadRepositories` (records already
   * normalised, flag included) and a spied `patchRepositoryFileIssues`. The
   * raw `GET /v1/repositories` route answers nothing, so a card that still
   * read the raw body itself would find no list at all.
   */
  async function mount(role: string) {
    serve((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/tenants/me' ? { status: 200, body: me(role) } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { normRepoList } = await import('../RepositoriesData')
    const records = normRepoList({
      repositories: [
        repo({ repo_id: OFF_ID, repo: 'example-api', agents_may_file_issues: false }),
        repo({ repo_id: ON_ID, repo: 'example-platform', agents_may_file_issues: true }),
        repo({ repo_id: UNSERVED_ID, repo: 'example-web' }),
      ],
    })
    const loads = vi.fn(async () => ({ status: 'ok' as const, fetchedAt: Date.now(), data: records }))
    const patch = vi.fn(async () => ({ status: 'ok' as const, fetchedAt: Date.now(), data: {} }))
    vi.doMock('../api', async (importOriginal) => ({
      ...(await importOriginal<typeof import('../api')>()),
      loadRepositories: loads,
      patchRepositoryFileIssues: patch,
    }))
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={null} go={vi.fn()} />)
    await waitFor(() => expect(document.querySelectorAll('.ur-file-issues').length).toBe(3), WAIT)
    return { loads, patch }
  }

  function line(name: string): HTMLElement {
    const card = Array.from(document.querySelectorAll<HTMLElement>('.ur-card')).find((c) => visible(c).includes(name))
    const el = card?.querySelector<HTMLElement>('.ur-file-issues')
    if (el === null || el === undefined) throw new Error(`no agents-may-file-issues line on ${name}`)
    return el
  }

  it("an admin's card draws the record's value, a dash for null, and its toggle calls the helper", async () => {
    const { loads, patch } = await mount('admin')
    // MUTATION: read the list off the raw route instead of `loadRepositories` and no card is drawn at all.
    expect(loads).toHaveBeenCalled()
    const off = line('example-org/example-api')
    const on = line('example-org/example-platform')
    const unserved = line('example-org/example-web')
    // MUTATION: overwrite the record's flag with a value read elsewhere (`null`) and these read a dash.
    expect(off.getAttribute('data-file-issues')).toBe('false')
    expect(visible(off)).toContain('Agents may file issues off')
    expect(on.getAttribute('data-file-issues')).toBe('true')
    expect(visible(on)).toContain('Agents may file issues on')
    // MUTATION: draw a null flag as `off` and this has a value and no dash.
    expect(unserved.getAttribute('data-file-issues')).toBeNull()
    expect(unserved.querySelector('.c-dash')).not.toBeNull()
    expect(unserved.querySelector('button')).toBeNull()
    // MUTATION: drop the admin control and there is no button to press.
    await waitFor(() => expect(off.querySelector('button')).not.toBeNull(), WAIT)
    const reads = loads.mock.calls.length
    fireEvent.click(off.querySelector('button')!)
    // MUTATION: keep a local PATCH in Repositories.tsx instead of the api helper and the spy is never called.
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1), WAIT)
    // MUTATION: pass the current value (`on` for `!on`) or another card's id and this fails.
    expect(patch).toHaveBeenCalledWith(OFF_ID, true)
    // MUTATION: drop the `reload()` after a successful change and the list is not read again.
    await waitFor(() => expect(loads.mock.calls.length).toBeGreaterThan(reads), WAIT)
  })

  it('a member sees the state with no button, and nothing is sent', async () => {
    const { patch } = await mount('member')
    const off = line('example-org/example-api')
    // MUTATION: drop the admin gate (`admin={true}`) and the member gets a button.
    await waitFor(() => expect(off.querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    expect(off.querySelector('button')).toBeNull()
    expect(line('example-org/example-platform').querySelector('button')).toBeNull()
    // MUTATION: hide the value below admin and this loses it.
    expect(visible(off)).toContain('Agents may file issues off')
    // MUTATION: call the helper on mount or for any role and this is called.
    expect(patch).not.toHaveBeenCalled()
  })
})

describe("the repository's Settings tab shows the flag read-only", () => {
  const ID = 'repo_4444444444444444'

  async function mount(fields: Json) {
    const calls = serve((m, url) => {
      if (m === 'GET' && url.split('?')[0] === '/v1/tenants/me') return { status: 200, body: me('admin') }
      if (m === 'GET' && url === `/v1/repositories/${ID}`) {
        return {
          status: 200,
          body: {
            repository: {
              ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6') }),
              graph: { depth: 3, min_confidence: 0.2 },
              selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
              ...fields,
            },
            index_runs: [],
            used_by: [],
          },
        }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
    await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)
    // MUTATION: leave the line out of the Settings tab and this never appears.
    await waitFor(() => expect(document.querySelector('.ur-detail-file-issues')).not.toBeNull(), WAIT)
    return { calls, el: document.querySelector<HTMLElement>('.ur-detail-file-issues')! }
  }

  it.each([
    [true, 'on'],
    [false, 'off'],
  ])('a served %s reads %s, with no control even for an admin', async (served, word) => {
    const { calls, el } = await mount({ agents_may_file_issues: served })
    // MUTATION: draw the served value inverted, or a constant, and one of these fails.
    expect(el.getAttribute('data-file-issues')).toBe(String(served))
    expect(visible(el)).toContain(`Agents may file issues ${word}`)
    // MUTATION: put a toggle on the detail page and it has a button.
    expect(el.querySelector('button')).toBeNull()
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('an unserved flag is a dash', async () => {
    const { el } = await mount({})
    // MUTATION: draw a null flag as off and this has a value and no dash.
    expect(el.getAttribute('data-file-issues')).toBeNull()
    expect(el.querySelector('.c-dash')).not.toBeNull()
  })
})
