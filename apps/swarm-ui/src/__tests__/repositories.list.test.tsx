// WORK › REPOSITORIES, the list (repositories.html screen 2, pick B: cards,
// one per repository, with the index at a glance).
//
// WHAT EACH CASE HOLDS:
//   * one card per registration, in the order served, each with freshness
//     against the head, the head sha, tests mapped, last indexed, the
//     schedule, Open and Index now;
//   * an unknown figure is a dash WITH ITS REASON, never 0 and never
//     "current": a head never read, a coverage with no index, an interval the
//     API did not serve;
//   * an index run in flight replaces Index now with "indexing now";
//   * Index now POSTs index:run for that repository and re-reads;
//   * no registrations is an empty state with the way to register;
//   * GET /v1/repositories answering 404 with no code (the route is not
//     there yet) is "not served yet", naming the route -- not a failure, and
//     not an empty list;
//   * a real failure is drawn as one;
//   * at 390 the cards stack into one column.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { HOUR, ago, repo, serve, sha, visible } from './repofixture'
import { cascade } from './cssgate'
import { SHEETS as ALL } from './sheets'

const WAIT = { timeout: 4000 }

async function mount(go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={null} go={go} />)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

const THREE = [
  repo(),
  repo(
    { repo_id: 'repo_1111111111111111', repo: 'example-web' },
    { current_sha: sha('9f8e7d6'), head_sha: sha('5c4b3a2'), behind_by: 3, coverage: 0.61, interval_hours: 12, last_indexed_at: ago(3 * HOUR), in_flight_task_id: 'task_abc' },
  ),
  repo(
    { repo_id: 'repo_2222222222222222', repo: 'example-infra', default_branch: 'develop', last_run: { task_id: 'task_x', state: 'FAILED', end_cause: 'timed_out', kind: 'full' } },
    { current_sha: null, head_sha: null, behind_by: null, coverage: null, last_indexed_at: null, interval_hours: 168, on_change: false },
  ),
]

function cards(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card'))
}

describe('the Repositories list is cards, one per repository (pick B)', () => {
  it('draws one card per registration in the served order, headed Repositories', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    expect(document.querySelector('h1')?.textContent).toBe('Repositories')
    expect(visible(document.querySelector('.c-phead'))).toContain('3 registered')
    expect(cards().map((c) => visible(c.querySelector('h2')))).toEqual([
      'example-org/example-api',
      'example-org/example-web',
      'example-org/example-infra',
    ])
  })

  it('gives each card freshness vs head, head sha, tests mapped, last indexed and schedule', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    const [api, web] = cards()
    expect(api!.querySelector('.c-pill')?.getAttribute('data-fresh')).toBe('current')
    expect(visible(api!)).toContain('Default branch main')
    expect(visible(api!)).toContain('head a1b2c3d')
    expect(visible(api!.querySelector('.ur-tm'))).toContain('86%')
    expect(visible(api!)).toContain('Last indexed 18m ago')
    expect(visible(api!)).toContain('Schedule 24 h + on change')

    expect(web!.querySelector('.c-pill')?.getAttribute('data-fresh')).toBe('behind')
    expect(visible(web!.querySelector('.c-pill'))).toBe('3 behind')
    expect(visible(web!.querySelector('.ur-tm'))).toContain('61%')
  })

  it('says what it does not know as a dash with its reason, never 0 or current', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    const infra = cards()[2]!
    expect(infra.querySelector('.c-pill')?.getAttribute('data-fresh')).toBe('none')
    expect(visible(infra.querySelector('.c-pill'))).toBe('no index')
    const dashes = Array.from(infra.querySelectorAll('.c-dash')).map((d) => d.getAttribute('title'))
    expect(dashes).toContain('The default branch head has never been read: change polling is off or the token lost access')
    expect(dashes).toContain('No index has been built yet')
    // The bar is hatched (not measured), never an empty bar that reads as 0%.
    expect(infra.querySelector('.ur-tm .ur-bar')?.classList.contains('is-unmeasured')).toBe(true)
    expect(visible(infra.querySelector('.ur-tm'))).not.toContain('0%')
    expect(visible(infra)).toContain('Last run failed · timed_out')
    expect(visible(infra)).toContain('Schedule 168 h')
  })

  it('shows "indexing now" in place of Index now while a run is in flight', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    const web = cards()[1]!
    expect(visible(web)).toContain('indexing now')
    expect(within(web).queryByRole('button', { name: 'Index now' })).toBeNull()
    expect(within(cards()[0]!).getByRole('button', { name: 'Index now' })).toBeTruthy()
  })

  it('Index now queues an index run for that repository and re-reads', async () => {
    let reads = 0
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/repositories') {
        reads += 1
        return { status: 200, body: { repositories: THREE } }
      }
      if (m === 'POST' && url === '/v1/repositories/repo_0a1b2c3d4e5f6071/index:run') return { status: 202, body: { task_id: 'task_new' } }
      return null
    })
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    fireEvent.click(within(cards()[0]!).getByRole('button', { name: 'Index now' }))
    await waitFor(() => expect(reads).toBe(2), WAIT)
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.body).toEqual({ kind: 'incremental' })
    // A repository with no index yet gets a full run.
    fireEvent.click(within(cards()[2]!).getByRole('button', { name: 'Index now' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(2), WAIT)
    expect(calls.filter((c) => c.method === 'POST')[1]!.url).toBe('/v1/repositories/repo_2222222222222222/index:run')
    expect(calls.filter((c) => c.method === 'POST')[1]!.body).toEqual({ kind: 'full' })
  })

  it('Open goes to the repository; Register repository and Git tokens go to their pages', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    const { go } = await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    const open = within(cards()[0]!).getByRole('link', { name: 'Open' })
    expect(open.getAttribute('href')).toBe('/repositories/repo_0a1b2c3d4e5f6071')
    fireEvent.click(open)
    expect(go).toHaveBeenLastCalledWith('work/repositories?repo=repo_0a1b2c3d4e5f6071')
    fireEvent.click(document.querySelector<HTMLAnchorElement>('a[href="/repositories/register"]')!)
    expect(go).toHaveBeenLastCalledWith('work/repositories?page=register')
    fireEvent.click(document.querySelector<HTMLAnchorElement>('a[href="/repositories/tokens"]')!)
    expect(go).toHaveBeenLastCalledWith('work/repositories?page=tokens')
  })
})

describe('the list says what it could not read', () => {
  it('no registrations is an empty state with the way to register', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: [] } } : null))
    await mount()
    await waitFor(() => expect(document.querySelector('.c-emp')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.c-emp'))).toContain('No repositories registered')
    expect(document.querySelector('.c-emp a[href="/repositories/register"]')).not.toBeNull()
  })

  it('a route that is not there yet is "not served yet", naming the route', async () => {
    serve(() => null)
    await mount()
    await waitFor(() => expect(document.querySelector('[data-notserved]')).not.toBeNull(), WAIT)
    const el = document.querySelector('[data-notserved]')!
    expect(el.getAttribute('data-notserved')).toBe('GET /v1/repositories')
    expect(visible(el)).toContain('Not served yet')
    expect(visible(el)).toContain('GET /v1/repositories')
    expect(cards()).toHaveLength(0)
  })

  it('a 501 is not served yet too', async () => {
    serve(() => ({ status: 501, body: { detail: 'Not Implemented' } }))
    await mount()
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories"]')).not.toBeNull(), WAIT)
  })

  it('a real failure is a failure, not "not served"', async () => {
    serve(() => ({ status: 500, body: { code: 'internal', message: 'Firestore is unavailable.' } }))
    await mount()
    await waitFor(() => expect(document.body.textContent).toContain('Firestore is unavailable.'), WAIT)
    expect(document.querySelector('[data-notserved]')).toBeNull()
  })
})

describe('the phone frame', () => {
  it('stacks the cards into one column at 390 and three across at 1440', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
    const sheets = ALL.map(([, t]) => t).join('\n')
    const grid = document.querySelector('.ur-cards')!
    expect(cascade(sheets, grid, 'grid-template-columns', { width: 390 }).winner?.value).toBe('minmax(0, 1fr)')
    expect(cascade(sheets, grid, 'grid-template-columns', { width: 1440 }).winner?.value).toBe('repeat(3, minmax(0, 1fr))')
  })
})
