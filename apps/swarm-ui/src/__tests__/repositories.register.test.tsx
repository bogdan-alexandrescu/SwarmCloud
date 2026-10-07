// WORK › REPOSITORIES › REGISTER REPOSITORY (repositories.html screen 3,
// pick C: pick from what the tenant's git token can read, then set the
// schedule and the change trigger).
//
// WHAT EACH CASE HOLDS:
//   * step 1 lists GET /v1/repositories/readable, names the secret it was read
//     with BY NAME, marks a registered repository and does not let it be
//     picked, and filters by name; a repository not listed is typed as
//     owner/repo (OB0, repositories.register.paging.test.tsx);
//   * step 2 sets the re-index interval, the change trigger, the runner
//     profiles (by name, from the catalogue) and the first index;
//   * Register POSTs exactly the registration's fields, then the first index
//     run, and opens the new repository;
//   * a refusal (the token cannot read it) is shown with the server's words;
//   * the readable route not being there yet is "not served yet", naming it.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

const READABLE = {
  secret_name: 'swarm-tenant-eng-git',
  total: 4,
  repositories: [
    { owner: 'example-org', repo: 'example-api', default_branch: 'main', private: true, pushed_at: new Date(Date.now() - 18 * 60_000).toISOString(), registered: false },
    { owner: 'example-org', repo: 'example-mobile', default_branch: 'main', private: true, pushed_at: null, registered: false },
    { full_name: 'example-org/example-web', default_branch: 'main', private: true, registered: true },
    { owner: 'example-org', repo: 'example-docs', default_branch: 'gh-pages', private: false, registered: false },
  ],
}

const RUNTIMES = { runtimes: { 'claude-code': {}, codex: {}, generic: {} } }

async function mount(go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view="page=register" go={go} />)
  return { ...utils, go }
}

afterEach(() => vi.unstubAllEnvs())

function picks(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.ur-pick'))
}

describe('Register repository picks from what the token can read (pick C)', () => {
  it('lists the readable repositories, naming the secret by name, with the registered one not pickable', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories/readable' ? { status: 200, body: READABLE } : null))
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    expect(document.querySelector('h1')?.textContent).toBe('Register repository')
    expect(visible(document.querySelector('.ur-steps'))).toBe('1 Choose repository › 2 Schedule and profiles › 3 Register')
    expect(screen.getByRole('searchbox').getAttribute('placeholder')).toBe('Filter the 4 repositories swarm-tenant-eng-git can read')
    const web = picks()[2]!
    expect(visible(web)).toContain('example-org/example-web')
    expect(visible(web)).toContain('already registered')
    expect(web.getAttribute('aria-disabled')).toBe('true')
    expect(visible(picks()[0]!)).toContain('private · pushed 18m ago')
    expect(visible(picks()[3]!)).toContain('gh-pages')
    // One owner/repo text box, for a repository the list does not carry (OB0).
    expect(screen.getByRole('textbox', { name: 'Not listed? Type owner/repo' })).toBeTruthy()
  })

  it('filters the list by name', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories/readable' ? { status: 200, body: READABLE } : null))
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'mob' } })
    expect(picks().map((p) => visible(p.querySelector('b')))).toEqual(['example-org/example-mobile'])
  })

  it('Next is disabled until a repository is picked; Register posts the registration, then the first index, and opens it', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/repositories/readable') return { status: 200, body: READABLE }
      if (m === 'GET' && url === '/v1/runtimes') return { status: 200, body: RUNTIMES }
      if (m === 'POST' && url === '/v1/repositories') {
        return { status: 201, body: { repo_id: 'repo_0a1b2c3d4e5f6071', owner: 'example-org', repo: 'example-api' } }
      }
      if (m === 'POST' && url === '/v1/repositories/repo_0a1b2c3d4e5f6071/index:run') return { status: 202, body: {} }
      return null
    })
    const { go } = await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    const next = screen.getByRole('button', { name: 'Next: schedule' }) as HTMLButtonElement
    expect(next.disabled).toBe(true)
    fireEvent.click(picks()[0]!)
    expect(picks()[0]!.getAttribute('aria-checked')).toBe('true')
    expect(next.disabled).toBe(false)
    fireEvent.click(next)

    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'claude-code' })).toBeTruthy(), WAIT)
    expect(visible(document.querySelector('.ur-steps b'))).toBe('2 Schedule and profiles')
    expect((screen.getByRole('checkbox', { name: 'claude-code' }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('checkbox', { name: 'codex' }) as HTMLInputElement).checked).toBe(false)
    fireEvent.click(screen.getByRole('radio', { name: '12 h' }))
    fireEvent.click(screen.getByRole('button', { name: 'Register example-org/example-api' }))

    await waitFor(() => expect(go).toHaveBeenCalledWith('work/repositories?repo=repo_0a1b2c3d4e5f6071'), WAIT)
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts[0]!.body).toEqual({
      repository: 'example-org/example-api',
      default_branch: 'main',
      allowed_profiles: ['claude-code'],
      index: { interval_hours: 12, on_change: 'poll' },
    })
    expect(posts[1]!.url).toBe('/v1/repositories/repo_0a1b2c3d4e5f6071/index:run')
    expect(posts[1]!.body).toEqual({ kind: 'full' })
  })

  it('shows a refusal in the server\'s words and stays on the form', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/repositories/readable') return { status: 200, body: READABLE }
      if (m === 'GET' && url === '/v1/runtimes') return { status: 200, body: RUNTIMES }
      if (m === 'POST' && url === '/v1/repositories') {
        return { status: 422, body: { code: 'no_access', message: 'swarm-tenant-eng-git cannot read example-org/example-api (GitHub answered 404).' } }
      }
      return null
    })
    const { go } = await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    fireEvent.click(picks()[0]!)
    fireEvent.click(screen.getByRole('button', { name: 'Next: schedule' }))
    await waitFor(() => screen.getByRole('button', { name: 'Register example-org/example-api' }), WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Register example-org/example-api' }))
    await waitFor(() => expect(document.body.textContent).toContain('cannot read example-org/example-api (GitHub answered 404).'), WAIT)
    expect(go).not.toHaveBeenCalled()
  })

  it('the readable route not being there yet is "not served yet", naming it', async () => {
    serve(() => null)
    await mount()
    await waitFor(() => expect(document.querySelector('[data-notserved]')).not.toBeNull(), WAIT)
    expect(document.querySelector('[data-notserved]')!.getAttribute('data-notserved')).toBe('GET /v1/repositories/readable')
    expect(picks()).toHaveLength(0)
  })

  it('the {repo_id} route answering not_found for "readable" is "not served yet" too, not a failure', async () => {
    serve((m, url) =>
      m === 'GET' && url === '/v1/repositories/readable' ? { status: 404, body: { code: 'not_found', message: 'No repository readable.' } } : null,
    )
    await mount()
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories/readable"]')).not.toBeNull(), WAIT)
    expect(document.body.textContent).not.toContain('No repository readable.')
  })

  it('choosing Off sends the designed shape: interval_hours "off", on_change "off"', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/repositories/readable') return { status: 200, body: READABLE }
      if (m === 'GET' && url === '/v1/runtimes') return { status: 200, body: RUNTIMES }
      if (m === 'POST' && url === '/v1/repositories') return { status: 201, body: { repository: { repo_id: 'repo_0a1b2c3d4e5f6071', owner: 'example-org', repo: 'example-api' } } }
      if (m === 'POST') return { status: 202, body: {} }
      return null
    })
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    fireEvent.click(picks()[0]!)
    fireEvent.click(screen.getByRole('button', { name: 'Next: schedule' }))
    await waitFor(() => screen.getByRole('button', { name: 'Register example-org/example-api' }), WAIT)
    fireEvent.click(screen.getByRole('radiogroup', { name: 'Re-index every' }).querySelector('[role="radio"]:last-child')!)
    fireEvent.click(within(screen.getByRole('radiogroup', { name: 'Change trigger' })).getByRole('radio', { name: 'Off' }))
    fireEvent.click(screen.getByRole('button', { name: 'Register example-org/example-api' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true), WAIT)
    expect((calls.find((c) => c.method === 'POST')!.body as { index: unknown }).index).toEqual({ interval_hours: 'off', on_change: 'off' })
  })
})
