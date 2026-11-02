// CONFIG › REPOSITORIES, THE FOLLOW-UPS (REPOS-FOLLOWUPS; part of #2157).
// BGD answered REPOS-PAGE's four questions on 2026-10-25 (recorded on
// task_cd6e6330782746178574), and config-e.html S3a-S3c is the frame:
//
//   1. Remove on each registered card, behind a typed owner/name, saying
//      first that its secret grants are disabled with it; a viewer sees none;
//      after it the registry is read again and a repository still chosen is
//      a dashed "chosen, not registered" card again.
//   2. The dashed card's Register opens Register with `repo=owner/name`, on
//      step 2 with it picked; a `repo` not in the reader's reach list opens
//      step 1 as before.
//   3. The reach list on this page points to GitHub access by link and has
//      no org sentence; GitHub access' own copy is unchanged.
//   4. The reach table's Registered column, and each card's "Secrets: N" and
//      "QA targets: N" lines, from ONE counts read for the page.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

const API_ID = 'repo_0a1b2c3d4e5f6071'
const WEB_ID = 'repo_1111111111111111'

const GRANT = (id: string, repository: string, mode: 'read' | 'write' = 'write') => ({
  repo_id: id,
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
})

const ACCESS = (grants: unknown[]) => ({ connection: { state: 'active', forge_login: 'octo-dev' }, orgs: [], grants, tenant_id: 'eng' })

const ME = (role: string) => ({
  tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
  principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
  environment: 'dev',
  environment_declared: true,
  role: { name: role, source: 'override' },
})

const WEB = () => repo({ repo_id: WEB_ID, repo: 'example-web', repository_url: 'https://github.com/example-org/example-web' })

/** The page's API: two registered, a third chosen only; DELETE removes from the registry the next read serves. */
function live(opts: { role?: string; counts?: unknown } = {}) {
  let registered: Record<string, unknown>[] = [repo(), WEB()]
  const grants = [GRANT(API_ID, 'example-org/example-api'), GRANT(WEB_ID, 'example-org/example-web', 'read'), GRANT('repo_2222222222222222', 'example-labs/console', 'read')]
  const calls = serve((m, url) => {
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/tenants/me' && opts.role !== undefined) return { status: 200, body: ME(opts.role) }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: registered } }
    if (m === 'GET' && path === '/v1/repositories/counts') {
      return {
        status: 200,
        body: opts.counts ?? {
          tenant_id: 'eng',
          counts: registered.map((r) => ({ repo_id: r.repo_id, repository: `example-org/${String(r.repo)}`, secrets: r.repo_id === API_ID ? 2 : 0, qa_targets: r.repo_id === API_ID ? 1 : 0 })),
        },
      }
    }
    if (m === 'GET' && path === '/v1/access') return { status: 200, body: ACCESS(grants) }
    if (m === 'DELETE' && path === `/v1/repositories/${WEB_ID}`) {
      registered = registered.filter((r) => r.repo_id !== WEB_ID)
      return { status: 200, body: null }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mount(view: string | null = null, go = vi.fn()) {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={view} go={go} />)
  return go
}

const card = (name: string) =>
  Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card')).find((c) => visible(c.querySelector('h2')) === name) ?? null
const dashed = () => Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card.is-chosen')).map((c) => visible(c.querySelector('h2')))
const reach = () => document.querySelector<HTMLElement>('.ac-grants')
const rowOf = (repository: string) => reach()?.querySelector<HTMLElement>(`li.ac-grant[data-repo="${repository}"]`) ?? null

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('Unregister (QA1 config-15: it was `Remove`) on each registered card', () => {
  it('needs the typed owner/name, says the secret grants go with it, calls DELETE once, re-reads, and the card is dashed again', async () => {
    const calls = live()
    // The API's own answer to a removal: 204, no body, no content-type.
    const inner = globalThis.fetch
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const answer = await inner(input, init)
      return init?.method === 'DELETE' && answer.ok ? new Response(null, { status: 204 }) : answer
    }) as unknown as typeof fetch
    await mount()
    await waitFor(() => expect(card('example-org/example-web')).not.toBeNull(), WAIT)
    // MUTATION: drop the Unregister button and this throws.
    fireEvent.click(within(card('example-org/example-web')!).getByRole('button', { name: 'Unregister' }))
    const dialog = await waitFor(() => {
      const d = document.querySelector<HTMLElement>('[role="dialog"]')
      expect(d).not.toBeNull()
      return d!
    }, WAIT)
    // MUTATION: drop the sentence about the grants and the person is not told beforehand.
    expect(visible(dialog)).toMatch(/secret.*grant.*disabled/i)
    const confirm = within(dialog).getByRole('button', { name: 'Remove registration' })
    // MUTATION: let the dialog confirm without the name and this is enabled.
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    const field = within(dialog).getByRole('textbox')
    fireEvent.change(field, { target: { value: 'example-web' } })
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(field, { target: { value: 'example-org/example-web' } })
    expect((confirm as HTMLButtonElement).disabled).toBe(false)
    const readsBefore = calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === '/v1/repositories').length
    fireEvent.click(confirm)
    // MUTATION: read the bare 204 as a failure (as `write` did before CONSOLE-SMALL-FIXES) and the dialog stays up, saying "Not removed".
    // MUTATION: never re-read the registry and the card stays drawn registered.
    await waitFor(() => expect(dashed()).toContain('example-org/example-web'), WAIT)
    expect(card('example-org/example-web')!.classList.contains('is-chosen')).toBe(true)
    // MUTATION: send the DELETE twice (or for every card) and this is not one.
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual([`/v1/repositories/${WEB_ID}`])
    expect(calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === '/v1/repositories').length).toBeGreaterThan(readsBefore)
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(visible(document.querySelector('.c-count-note'))).toContain('Registered repositories · 1 · 2 chosen, not registered')
  })

  it('a viewer sees no Unregister', async () => {
    live({ role: 'viewer' })
    await mount()
    await waitFor(() => expect(document.querySelector('[data-role-gate="member"]')).not.toBeNull(), WAIT)
    await waitFor(() => expect(card('example-org/example-api')).not.toBeNull(), WAIT)
    // MUTATION: draw Unregister for every role and the viewer has one.
    expect(within(card('example-org/example-api')!).queryByRole('button', { name: 'Unregister' })).toBeNull()
    expect(within(card('example-org/example-web')!).queryByRole('button', { name: 'Unregister' })).toBeNull()
  })

  it('a member sees Unregister on every registered card and none on a dashed one', async () => {
    live({ role: 'member' })
    await mount()
    await waitFor(() => expect(dashed()).toEqual(['example-labs/console']), WAIT)
    expect(within(card('example-org/example-api')!).getByRole('button', { name: 'Unregister' })).toBeTruthy()
    expect(within(card('example-labs/console')!).queryByRole('button', { name: 'Unregister' })).toBeNull()
  })
})

describe('Register opens with the repository picked', () => {
  it("the dashed card's Register goes to Register with repo=owner/name", async () => {
    live()
    const go = await mount()
    await waitFor(() => expect(dashed()).toEqual(['example-labs/console']), WAIT)
    fireEvent.click(within(card('example-labs/console')!).getByRole('link', { name: 'Register' }))
    // MUTATION: link to the bare Register page and the picked repository is lost.
    expect(go).toHaveBeenCalledWith('config/repositories?page=register&repo=example-labs%2Fconsole')
  })

  it('Register with a repo in the reach list opens on step 2 with it picked', async () => {
    live()
    await mount('page=register&repo=example-labs%2Fconsole')
    // MUTATION: ignore `repo` and the page stays on step 1.
    await waitFor(() => expect(document.querySelector('.ur-steps b')?.textContent).toContain('Schedule and profiles'), WAIT)
    expect(visible(document.querySelector('.ur-form h2'))).toBe('example-labs/console')
    expect(document.querySelector('[role="radiogroup"][aria-label="Repository"]')).toBeNull()
  })

  it('Register with a repo not in the reach list opens on step 1, as today', async () => {
    const calls = live()
    await mount('page=register&repo=example-org%2Fnot-chosen')
    await waitFor(() => expect(calls.some((c) => c.url.split('?')[0] === '/v1/access')).toBe(true), WAIT)
    await new Promise((r) => setTimeout(r, 50))
    // MUTATION: pick whatever `repo` names and this opens on step 2.
    expect(document.querySelector('.ur-steps b')?.textContent).toContain('Choose repository')
    expect(document.querySelector('.ur-form')).toBeNull()
  })

  it('the Register address keeps repo= in the router', async () => {
    vi.resetModules()
    const { fromAddress, canonical } = await import('../App')
    const r = fromAddress('config/repositories?page=register&repo=example-labs%2Fconsole')
    expect([r.sectionId, r.tab, r.view]).toEqual(['config', 'repositories', 'page=register&repo=example-labs%2Fconsole'])
    expect(canonical(r)).toBe('config/repositories?page=register&repo=example-labs%2Fconsole')
  })
})

describe("the reach list's copy on Config › Repositories", () => {
  it('points to GitHub access by link and has no org sentence here', async () => {
    serve((m, url) => {
      const path = url.split('?')[0]
      if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [] } }
      if (m === 'GET' && path === '/v1/access') return { status: 200, body: ACCESS([]) }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const go = await mount()
    await waitFor(() => expect(reach()).not.toBeNull(), WAIT)
    const empty = reach()!.querySelector<HTMLElement>('.c-emp')!
    // MUTATION: keep "from an enabled owner above" and there is nothing above on this page.
    expect(visible(empty)).toContain('on GitHub access')
    expect(visible(empty)).not.toContain('above')
    const link = within(empty).getByRole('link', { name: 'GitHub access' })
    expect(link.getAttribute('href')).toBe('/config/github?view=choose')
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith('config/github?view=choose')
    // MUTATION: keep the org sentence on this page and it names orgs nothing here shows.
    expect(visible(reach())).not.toContain('Removing an org')
  })

  it("GitHub access' own copy is unchanged", async () => {
    serve((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/access' ? { status: 200, body: ACCESS([]) } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ReachableRepositoriesRead } = await import('../ReachableRepositories')
    render(<ReachableRepositoriesRead />)
    await waitFor(() => expect(reach()).not.toBeNull(), WAIT)
    // MUTATION: change the default copy rather than this page's and these fail.
    expect(visible(reach()!.querySelector('.c-emp'))).toContain('Choose repositories from an enabled owner above, or type owner/repo.')
    expect(visible(reach())).toContain('Removing an org deletes its grants at once')
    expect(reach()!.querySelector('[data-check="registered"]')).toBeNull()
  })
})

describe('the Registered column and the counts', () => {
  it('the reach table says which are registered, linking to the card or to Register', async () => {
    live()
    const go = await mount()
    await waitFor(() => expect(rowOf('example-labs/console')).not.toBeNull(), WAIT)
    await waitFor(() => expect(card('example-org/example-api')).not.toBeNull(), WAIT)
    // MUTATION: drop the column's head and the cells have no label at desktop width.
    expect(Array.from(reach()!.querySelectorAll('.ac-grid-h > span')).map((s) => s.textContent)).toContain('Registered')
    const cell = (repository: string) => rowOf(repository)!.querySelector<HTMLElement>('[data-check="registered"]')!
    // MUTATION: draw the column from the grants alone and every row says the same.
    await waitFor(() => expect(visible(cell('example-org/example-api'))).toContain('registered'), WAIT)
    expect(visible(cell('example-org/example-api'))).not.toContain('not registered')
    expect(visible(cell('example-labs/console'))).toContain('not registered')
    fireEvent.click(within(cell('example-labs/console')).getByRole('link', { name: 'not registered' }))
    expect(go).toHaveBeenCalledWith('config/repositories?page=register&repo=example-labs%2Fconsole')
    const toCard = within(cell('example-org/example-api')).getByRole('link', { name: 'registered' })
    const scrolled = vi.fn()
    card('example-org/example-api')!.scrollIntoView = scrolled
    fireEvent.click(toCard)
    // MUTATION: make the link navigate away rather than to the card and this is not called.
    expect(scrolled).toHaveBeenCalled()
  })

  it('each card has Secrets: N and QA targets: N, from one counts read for the page', async () => {
    const calls = live()
    const go = await mount()
    await waitFor(() => expect(card('example-org/example-api')?.querySelector('[data-row="secrets"]')).not.toBeFalsy(), WAIT)
    const api = card('example-org/example-api')!
    const web = card('example-org/example-web')!
    // MUTATION: draw the names, or the grants, instead of the count and these differ.
    await waitFor(() => expect(visible(api.querySelector('[data-row="secrets"]'))).toBe('Secrets: 2'), WAIT)
    expect(visible(api.querySelector('[data-row="qa-targets"]'))).toBe('QA targets: 1')
    expect(visible(web.querySelector('[data-row="secrets"]'))).toBe('Secrets: none')
    expect(visible(web.querySelector('[data-row="qa-targets"]'))).toBe('QA targets: none')
    // ONE LINE FOR BOTH COUNTS (visual QA VQ-043, S3a's compact card).
    // MUTATION: draw each count as its own row again -> fails.
    const countRow = api.querySelector('[data-row="secrets"]')!.parentElement!
    expect(countRow.classList.contains('ur-cmeta')).toBe(true)
    expect(countRow.contains(api.querySelector('[data-row="qa-targets"]'))).toBe(true)
    fireEvent.click(within(api.querySelector<HTMLElement>('[data-row="qa-targets"]')!).getByRole('link'))
    // MUTATION: link the bare QA targets tab and the list is not narrowed to this repository.
    expect(go).toHaveBeenCalledWith('config/repositories?page=qa-targets&repository=example-org%2Fexample-api')
    // MUTATION: read the counts per card and there are two of these.
    expect(calls.filter((c) => c.url.split('?')[0] === '/v1/repositories/counts')).toHaveLength(1)
    expect(calls.filter((c) => /^\/v1\/repositories\/repo_[^/]+\/secrets/.test(c.url))).toHaveLength(0)
    // The dashed card has no counts: it is not registered.
    expect(card('example-labs/console')!.querySelector('[data-row="secrets"]')).toBeNull()
  })

  it('a counts read that fails is a dash with its reason, never a zero', async () => {
    serve((m, url) => {
      const path = url.split('?')[0]
      if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo()] } }
      if (m === 'GET' && path === '/v1/access') return { status: 200, body: ACCESS([GRANT(API_ID, 'example-org/example-api')]) }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    await mount()
    await waitFor(() => expect(card('example-org/example-api')?.querySelector('[data-row="secrets"] .c-dash, [data-row="secrets"] [title]')).not.toBeFalsy(), WAIT)
    // MUTATION: default an unread count to 0 and this says "none".
    expect(visible(card('example-org/example-api')!.querySelector('[data-row="secrets"]'))).not.toContain('none')
    expect(visible(card('example-org/example-api')!.querySelector('[data-row="qa-targets"]'))).not.toContain('none')
  })
})

describe('QA targets narrowed to one repository from the address', () => {
  const TARGET = (n: number, repository: string) => ({
    id: `qat_${String(n).repeat(16).slice(0, 16)}`,
    name: `${repository.split('/')[1]}-staging`,
    base_url: `https://${n}.staging.example.com/`,
    host: `${n}.staging.example.com`,
    host_state: 'approved',
    repo_id: `repo_${String(n).repeat(16).slice(0, 16)}`,
    repository: `https://github.com/${repository}`,
    sign_in: { kind: 'none' },
    screens: [],
    revision: 1,
  })

  it('shows only that repository, says so, and offers every target back', async () => {
    serve((m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/qa-targets'
        ? { status: 200, body: { qa_targets: [TARGET(1, 'example-org/example-api'), TARGET(2, 'example-org/example-web')] } }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const go = await mount('page=qa-targets&repository=example-org%2Fexample-api')
    const names = () =>
      Array.from(document.querySelectorAll('.qa-page .ur-cards > .qa-target')).map((el) => ['example-api-staging', 'example-web-staging'].find((n) => visible(el).includes(n)))
    // MUTATION: ignore `repository` and both targets are drawn.
    await waitFor(() => expect(names()).toEqual(['example-api-staging']), WAIT)
    const note = document.querySelector<HTMLElement>('[data-qa-filter]')
    expect(visible(note)).toContain('example-org/example-api')
    fireEvent.click(within(note!).getByRole('link', { name: 'Show every QA target' }))
    expect(go).toHaveBeenCalledWith('config/repositories?page=qa-targets')
  })

  it('the filtered address survives the address bar', async () => {
    vi.resetModules()
    const { addressToPath, pathToAddress } = await import('../paths')
    const path = addressToPath('config/repositories?page=qa-targets&repository=example-org%2Fexample-api')
    const [p, q] = path.split('?')
    expect(new URLSearchParams(q).get('repository')).toBe('example-org/example-api')
    expect(new URLSearchParams(pathToAddress(p!, `?${q}`)!.address.split('?')[1]).get('repository')).toBe('example-org/example-api')
  })
})

describe('the fixture build serves only counts it can show', () => {
  it('counts no secret and no QA target, as its Secrets and QA targets pages serve none for a repository', async () => {
    vi.resetModules()
    const { loadRepositoryCounts } = await import('../api')
    const { loadQaTargets } = await import('../qaTargetsApi')
    // The fixture QA targets list is empty, so any QA-target count above 0 links to a list that contradicts it.
    expect((await loadQaTargets()).status).toBe('empty')
    const r = await loadRepositoryCounts()
    expect(r.status).toBe('ok')
    const counts = r.status === 'ok' ? Object.values(r.data) : []
    expect(counts.length).toBeGreaterThan(0)
    // MUTATION: invent a count for a fixture registration and this differs.
    expect(counts.map((c) => [c.secrets, c.qa_targets])).toEqual(counts.map(() => [0, 0]))
  })
})
