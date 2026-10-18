// THE WEBHOOK CHANGE TRIGGER (lane HOOKS-UI, part of #1384).
//
// `index.on_change = "webhook"` is "poll AND push" (swarm_api/repositories.py
// ON_CHANGE_POLLED; docs/design/graph-service.md §6.2): the five-minute poll
// still reads a webhook registration, and a push to the default branch polls
// it at once through POST /v1/admin/repositories/push. So choosing it never
// leaves a repository unrefreshed, even while the receiver
// (`swarm-github-hooks`, behind `enable_github_hooks`) is off, and the console
// offers it without asking whether the receiver is on.
//
// WHAT EACH CASE HOLDS:
//   * Register: Webhook is selectable, labelled "on push to the default
//     branch", with the note that the App's webhook must be ticked (runbook
//     Step 9); "phase 3" is gone; the review reads it back as Webhook and the
//     POST carries on_change "webhook";
//   * Settings: the served trigger is selected; choosing another is one PATCH
//     carrying only { index: { on_change } }; a refused write reverts and says
//     why; while a write is in flight the group is disabled.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }

afterEach(() => vi.unstubAllEnvs())

describe('Register: the Webhook trigger is offered', () => {
  const READABLE = {
    secret_name: 'swarm-tenant-eng-git',
    total: 1,
    repositories: [{ owner: 'example-org', repo: 'example-api', default_branch: 'main', private: true, registered: false }],
  }

  async function toStep2() {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/repositories/readable') return { status: 200, body: READABLE }
      if (m === 'GET' && url === '/v1/runtimes') return { status: 200, body: { runtimes: { 'claude-code': {} } } }
      if (m === 'POST' && url === '/v1/repositories') return { status: 201, body: { repo_id: 'repo_0a1b2c3d4e5f6071' } }
      if (m === 'POST') return { status: 202, body: {} }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=register" go={vi.fn()} />)
    await waitFor(() => expect(document.querySelectorAll('.ur-pick')).toHaveLength(1), WAIT)
    fireEvent.click(document.querySelector('.ur-pick')!)
    fireEvent.click(screen.getByRole('button', { name: 'Next: schedule' }))
    await waitFor(() => screen.getByRole('button', { name: 'Next: review' }), WAIT)
    return calls
  }

  it('is selectable, labelled plainly, with the Step 9 note, and no "phase 3"', async () => {
    await toStep2()
    const group = screen.getByRole('radiogroup', { name: 'Change trigger' })
    const hook = within(group).getByRole('radio', { name: 'Webhook · on push to the default branch' }) as HTMLButtonElement
    expect(hook.disabled).toBe(false)
    expect(document.body.textContent).not.toContain('phase 3')
    const field = group.parentElement!
    expect(visible(field)).toMatch(/GitHub App's webhook must be ticked/)
    expect(visible(field)).toContain('Step 9')
    expect(visible(field)).toMatch(/five-minute poll/)
  })

  it('choosing it is read back on the review and POSTed as on_change "webhook"', async () => {
    const calls = await toStep2()
    fireEvent.click(within(screen.getByRole('radiogroup', { name: 'Change trigger' })).getByRole('radio', { name: /^Webhook/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    expect(visible(document.querySelector('[data-review="trigger"]'))).toBe('Webhook · on push to the default branch')
    fireEvent.click(screen.getByRole('button', { name: 'Register example-org/example-api' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/repositories')).toBe(true), WAIT)
    const post = calls.find((c) => c.method === 'POST' && c.url === '/v1/repositories')!
    expect((post.body as { index: unknown }).index).toEqual({ interval_hours: 24, on_change: 'webhook' })
  })
})

describe('Settings: the change trigger is changed in place', () => {
  const ID = 'repo_4444444444444444'

  function detail(onChange: string) {
    return {
      repository: {
        ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6'), on_change: onChange }),
        graph: { depth: 3, min_confidence: 0.2 },
        selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
      },
      index_runs: [],
      used_by: [],
    }
  }

  function routes(start: string, write: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
    let current: unknown = detail(start)
    return serve((m, url, body) => {
      if (m === 'GET' && url === `/v1/repositories/${ID}`) return { status: 200, body: current }
      if (m !== 'GET') {
        const r = write(m, url, body)
        if (r !== null && r.status < 300) current = r.body
        return r
      }
      return null
    })
  }

  async function mount() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
    await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)
  }

  const group = () => screen.findByRole('radiogroup', { name: 'Change trigger' }, WAIT)
  const checked = (g: HTMLElement) => within(g).getAllByRole('radio').filter((r) => r.getAttribute('aria-checked') === 'true').map((r) => visible(r))

  it('selects the served trigger, with Webhook selectable and the Step 9 note', async () => {
    routes('poll', () => null)
    await mount()
    const g = await group()
    expect(checked(g)).toEqual(['Poll the default branch (every 5 min)'])
    expect((within(g).getByRole('radio', { name: 'Webhook · on push to the default branch' }) as HTMLButtonElement).disabled).toBe(false)
    expect(visible(g.parentElement!)).toContain('Step 9')
  })

  it('choosing Webhook is one PATCH carrying only index.on_change', async () => {
    const seen: unknown[] = []
    routes('poll', (m, url, body) => {
      if (m === 'PATCH' && url === `/v1/repositories/${ID}`) {
        seen.push(body)
        return { status: 200, body: detail('webhook') }
      }
      return null
    })
    await mount()
    fireEvent.click(within(await group()).getByRole('radio', { name: /^Webhook/ }))
    await waitFor(() => expect(seen).toEqual([{ index: { on_change: 'webhook' } }]), WAIT)
    await waitFor(async () => expect(checked(await group())).toEqual(['Webhook · on push to the default branch']), WAIT)
  })

  it('a refused write reverts to the served trigger and says why', async () => {
    routes('webhook', (m) => (m === 'PATCH' ? { status: 403, body: { code: 'forbidden', message: 'Not a member of this workspace.' } } : null))
    await mount()
    fireEvent.click(within(await group()).getByRole('radio', { name: 'Off' }))
    await waitFor(() => expect(document.body.textContent).toContain('The change trigger was not changed'), WAIT)
    expect(checked(await group())).toEqual(['Webhook · on push to the default branch'])
  })
})
