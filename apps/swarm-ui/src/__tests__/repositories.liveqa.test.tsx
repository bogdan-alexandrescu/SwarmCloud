// LIVE QA OF WORK › REPOSITORIES, 2026-10-11 (section work-repos-setup-access).
//
// WHAT EACH CASE HOLDS:
//   * RSA-15: Index now, on the list and on one repository, and the full
//     index run queue NOTHING on their first click. A run is a task that holds
//     capacity once leased, so it is queued only from its confirmation; Cancel
//     and Esc queue nothing, and the safe choice holds the focus;
//   * RSA-4: an index whose document cut its test map at the ceiling never
//     reports the edge count as a total -- the list card, the tab badge and
//     the figures say it was cut -- and an uncut one says its count plainly;
//   * RSA-6: a language whose server pass timed out or failed is said on
//     the repository's page outside Settings, naming the language; an index
//     whose every pass finished says nothing.
//
// Fixtures only; nothing token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { coverageOf, liveIndex, repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_0a1b2c3d4e5f6071'
const RUN = `/v1/repositories/${ID}/index:run`

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

type Langs = { language: string; files: number; status: string }[]

function routes(opts: { truncated?: string[]; langs?: Langs; docLangs?: Langs; edges?: number } = {}) {
  const record = repo({ repo_id: ID }, { coverage: coverageOf(83, 20, opts.edges ?? 1000, 7) })
  const cut = opts.truncated ?? ['test_map']
  const index = liveIndex(ID, { truncated: cut, ...(opts.docLangs !== undefined ? { languages: opts.docLangs } : {}) }, { truncated: cut })
  return serve((m, url) => {
    const u = new URL(url, 'http://x')
    if (m === 'POST' && u.pathname === RUN) return { status: 202, body: { task_id: 'task_new' } }
    if (m !== 'GET') return null
    if (u.pathname === '/v1/repositories') return { status: 200, body: { repositories: [record] } }
    if (u.pathname === `/v1/repositories/${ID}`) return { status: 200, body: { repository: record, index_runs: [], used_by: [] } }
    if (u.pathname === `/v1/repositories/${ID}/index`) return { status: 200, body: index }
    if (u.pathname === `/v1/repositories/${ID}/languages` && opts.langs !== undefined) return { status: 200, body: { languages: opts.langs } }
    return null
  })
}

async function mount(view: string | null) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={view} go={vi.fn()} />)
}

async function mountDetail(tab: string | null = null) {
  await mount(tab === null ? `repo=${ID}` : `repo=${ID}&tab=${tab}`)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-api'), WAIT)
}

const posts = (calls: { method: string; url: string; body: unknown }[]) => calls.filter((c) => c.method === 'POST' && c.url === RUN)

describe('RSA-15: an index run spends capacity, so nothing is queued before it is confirmed', () => {
  it('list: Index now queues nothing; Cancel queues nothing; the confirmation queues one incremental run', async () => {
    const calls = routes()
    await mount(null)
    const card = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.ur-cards > .ur-card')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)
    fireEvent.click(within(card).getByRole('button', { name: 'Index now' }))
    expect(posts(calls)).toHaveLength(0)
    // The safe choice holds the focus, so Enter from the keyboard cancels rather than spends.
    const cancel = within(card).getByRole('button', { name: 'Cancel' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.click(cancel)
    expect(within(card).queryByRole('button', { name: 'Queue incremental run' })).toBeNull()
    expect(posts(calls)).toHaveLength(0)

    fireEvent.click(within(card).getByRole('button', { name: 'Index now' }))
    fireEvent.click(within(card).getByRole('button', { name: 'Queue incremental run' }))
    await waitFor(() => expect(posts(calls)).toHaveLength(1), WAIT)
    expect(posts(calls)[0]!.body).toEqual({ kind: 'incremental' })
  })

  it('detail: Esc closes the confirmation without queueing; the full run is queued only from its own confirmation', async () => {
    const calls = routes()
    await mountDetail()
    fireEvent.click(screen.getByRole('button', { name: 'Index now' }))
    const box = document.querySelector<HTMLElement>('.ur-confirm')!
    expect(box.getAttribute('data-confirm')).toBe('incremental')
    fireEvent.keyDown(document.activeElement ?? box, { key: 'Escape' })
    expect(document.querySelector('.ur-confirm')).toBeNull()
    expect(posts(calls)).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Queue full index run (re-resolves with LSP)' }))
    expect(posts(calls)).toHaveLength(0)
    expect(visible(document.querySelector('.ur-confirm'))).toContain('holds capacity')
    fireEvent.click(screen.getByRole('button', { name: 'Queue full run' }))
    await waitFor(() => expect(posts(calls)).toHaveLength(1), WAIT)
    expect(posts(calls)[0]!.body).toEqual({ kind: 'full' })
  })
})

describe('RSA-4: a test map cut at its ceiling is never reported as a complete count', () => {
  const badge = () => {
    const tab = Array.from(document.querySelectorAll<HTMLElement>('.ur-tabs a')).find((a) => visible(a.querySelector('.c-tab-label')) === 'Test map')!
    return visible(tab.querySelector('em'))
  }
  const tile = () => visible(Array.from(document.querySelectorAll('.ur-tile')).find((t) => visible(t).startsWith('Tests mapped')) ?? null)

  it('cut: the badge and the Overview say the edges stop at the ceiling', async () => {
    routes({ truncated: ['hot_spots', 'test_map'] })
    await mountDetail()
    await waitFor(() => expect(document.querySelector('.ur-kv')).not.toBeNull(), WAIT)
    expect(badge()).toBe('1,000+')
    expect(tile()).toContain('1,000+ edges (cut)')
    expect(tile()).not.toMatch(/1,000 edges/)
  })

  it('cut: the Test map tab says so on its figure too', async () => {
    routes({ truncated: ['test_map'] })
    await mountDetail('test-map')
    await waitFor(() => expect(document.querySelector('.ur-tmap .ur-tm')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.ur-tmap .ur-tm'))).toContain('1,000+ edges (cut)')
  })

  const card = () =>
    waitFor(() => {
      const c = document.querySelector<HTMLElement>('.ur-cards > .ur-card .ur-tm')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)

  it('cut: the list card says the edges stop at the ceiling, never "1,000 edges"', async () => {
    routes({ truncated: ['hot_spots', 'test_map'] })
    await mount(null)
    const tm = await card()
    await waitFor(() => expect(visible(tm)).toContain('1,000+ edges (cut)'), WAIT)
    expect(visible(tm)).not.toMatch(/1,000 edges/)
  })

  it('not cut: the list card says its count plainly, after reading the document', async () => {
    const calls = routes({ truncated: ['hot_spots'], edges: 3150 })
    await mount(null)
    const tm = await card()
    await waitFor(() => expect(calls.some((c) => c.method === 'GET' && c.url.startsWith(`/v1/repositories/${ID}/index`))).toBe(true), WAIT)
    expect(visible(tm)).toContain('3,150 edges')
    expect(visible(tm)).not.toContain('cut')
  })

  it('not cut: the count is said as it is, with no "+"', async () => {
    routes({ truncated: [], edges: 3150 })
    await mountDetail()
    await waitFor(() => expect(document.querySelector('.ur-kv')).not.toBeNull(), WAIT)
    expect(badge()).toBe('3,150')
    expect(tile()).toContain('3,150 edges')
    expect(tile()).not.toContain('cut')
  })
})

describe('RSA-6: a degraded index is said outside Settings', () => {
  it('names each language whose pass timed out, and only those', async () => {
    const langs = [
      { language: 'python', files: 1046, status: 'timed_out' },
      { language: 'javascript', files: 12, status: 'timed_out' },
      { language: 'typescript', files: 300, status: 'ok' },
    ]
    routes({ langs })
    await mountDetail()
    const warn = await waitFor(() => {
      const w = document.querySelector<HTMLElement>('.ur-degraded')
      expect(w).not.toBeNull()
      return w!
    }, WAIT)
    expect(warn.getAttribute('data-degraded')).toBe('python javascript')
    expect(visible(warn)).toContain('python (1,046 files)')
    expect(visible(warn)).toContain('timed out')
    expect(visible(warn)).not.toContain('typescript')
  })

  it('says nothing when every pass finished', async () => {
    routes({ langs: [{ language: 'python', files: 10, status: 'ok' }, { language: 'hcl', files: 3, status: 'unsupported' }] })
    await mountDetail()
    await waitFor(() => expect(document.querySelector('.ur-kv')).not.toBeNull(), WAIT)
    expect(document.querySelector('.ur-degraded')).toBeNull()
  })

  it('reads the index document\'s languages when the route is not served, on a tab other than Settings', async () => {
    routes({ docLangs: [{ language: 'python', files: 40, status: 'failing' }] })
    await mountDetail('hot-spots')
    const warn = await waitFor(() => {
      const w = document.querySelector<HTMLElement>('.ur-degraded')
      expect(w).not.toBeNull()
      return w!
    }, WAIT)
    expect(visible(warn)).toContain('python (40 files) failed')
    expect(within(warn).getByRole('link', { name: /Languages in Settings/ })).toBeTruthy()
  })
})
