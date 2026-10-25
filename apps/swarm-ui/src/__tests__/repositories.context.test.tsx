// CONFIG › REPOSITORIES › ONE REPOSITORY › SETTINGS: the Code intelligence
// override (lane CTXQ-UI; docs/design/context-quality.md §4.7). A repository's
// `context` is `on`, `off` or -- absent -- inherited from the workspace's
// Code intelligence setting. The tab offers all three and writes the choice
// with one PATCH /v1/repositories/{repo_id}; `inherit` is sent as itself,
// because that is what clears the override (RepositoryPatch, like
// `merge_policy`).
//
// WHAT EACH CASE HOLDS:
//   * the served value is the one selected, and an absent field is Inherit;
//   * each of On, Off and Inherit is one PATCH carrying `context`;
//   * a refused write reverts to the served value and says why;
//   * the card says local context sends nothing off the platform, and that
//     Jev is the workspace's switch.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_3333333333333333'

function detail(fields: Record<string, unknown> = {}) {
  return {
    repository: {
      ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6') }),
      graph: { depth: 3, min_confidence: 0.2 },
      selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
      ...fields,
    },
    index_runs: [],
    used_by: [],
  }
}

function routes(start: unknown, write?: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
  let current = start
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url === `/v1/repositories/${ID}`) return { status: 200, body: current }
    if (m !== 'GET' && write) {
      const r = write(m, url, body)
      if (r !== null && r.status < 300) current = r.body
      return r
    }
    return null
  })
  return calls
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)
}

const group = async () => screen.findByRole('radiogroup', { name: /code intelligence/i }, WAIT)
const option = (g: HTMLElement, name: RegExp) => within(g).getByRole('radio', { name })
const checked = (g: HTMLElement) => within(g).getAllByRole('radio').filter((r) => r.getAttribute('aria-checked') === 'true').map((r) => visible(r))

afterEach(() => vi.unstubAllEnvs())

describe('Settings: the Code intelligence override', () => {
  it('an absent `context` is Inherit; a served one is selected', async () => {
    routes(detail())
    await mount()
    expect(checked(await group())).toEqual(['Inherit'])
  })

  it('selects what the registration serves', async () => {
    routes(detail({ context: 'off' }))
    await mount()
    expect(checked(await group())).toEqual(['Off'])
  })

  it('says local context sends nothing, and that Jev is the workspace switch', async () => {
    routes(detail())
    await mount()
    const g = await group()
    const card = g.closest('section, .c-card, [class*="card"]') ?? document.body
    expect(visible(card as Element)).toMatch(/nothing leaves the platform/i)
    expect(visible(card as Element)).toContain('Config › Workspace Settings')
    // CONFIG-COPY (part of #2157): never the old Settings › Workspace, and the
    // link is Workspace Settings' own path, not the old address's redirect.
    expect(visible(document.body)).not.toMatch(/\bWork › (Setup|Access|Repositories)|(?<!Workspace )Settings › (Workspace|MCP servers)/)
    expect(within(card as HTMLElement).getByRole('link', { name: 'Config › Workspace Settings' }).getAttribute('href')).toBe('/config/workspace')
  })

  for (const [name, value] of [
    [/^on$/i, 'on'],
    [/^off$/i, 'off'],
  ] as const) {
    it(`writes ${value} with one PATCH`, async () => {
      const seen: unknown[] = []
      routes(detail(), (m, url, body) => {
        if (m === 'PATCH' && url === `/v1/repositories/${ID}`) {
          seen.push(body)
          return { status: 200, body: detail({ context: value }) }
        }
        return null
      })
      await mount()
      fireEvent.click(option(await group(), name))
      await waitFor(() => expect(seen).toEqual([{ context: value }]), WAIT)
      await waitFor(async () => expect(checked(await group())).toEqual([value === 'on' ? 'On' : 'Off']), WAIT)
    })
  }

  it('writes inherit, which clears the override', async () => {
    const seen: unknown[] = []
    routes(detail({ context: 'on' }), (m, url, body) => {
      if (m === 'PATCH' && url === `/v1/repositories/${ID}`) {
        seen.push(body)
        return { status: 200, body: detail() }
      }
      return null
    })
    await mount()
    fireEvent.click(option(await group(), /inherit/i))
    await waitFor(() => expect(seen).toEqual([{ context: 'inherit' }]), WAIT)
    await waitFor(async () => expect(checked(await group())).toEqual(['Inherit']), WAIT)
  })

  it('reverts and says why when the write is refused', async () => {
    routes(detail({ context: 'on' }), (m) =>
      m === 'PATCH' ? { status: 403, body: { code: 'FORBIDDEN', message: 'not a member of this workspace' } } : null,
    )
    await mount()
    fireEvent.click(option(await group(), /^off$/i))
    const alert = await screen.findByRole('alert', {}, WAIT)
    expect(visible(alert)).toMatch(/not changed/i)
    await waitFor(async () => expect(checked(await group())).toEqual(['On']), WAIT)
  })
})
