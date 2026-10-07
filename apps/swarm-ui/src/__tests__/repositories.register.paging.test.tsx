// WORK › REPOSITORIES › REGISTER REPOSITORY, onboarding phase 0 (OB0,
// docs/onboarding.md §5, part of #780).
//
// WHAT EACH CASE HOLDS:
//   * the picker follows `next_page` until the API stops serving one, so a
//     token that reads more than one page of 100 lists every repository, not
//     the first hundred;
//   * a list the API `capped` says so, and points at the typed field;
//   * a later page that fails keeps the pages read and names the one that did
//     not come back, rather than passing the short list off as complete;
//   * owner chips, one per owner in the list, narrow it to that owner;
//   * "Not listed? Type owner/repo" registers by name through
//     POST /v1/repositories, and the API's refusal is shown in its own words;
//   * the API serves `visibility` (public, private, internal), and the picker
//     reads it instead of drawing "visibility not read" on every row (G4-16).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const BASE = '/v1/repositories/readable'

type Json = Record<string, unknown>

/** One entry as `swarm_api/repositories.py::readable` serves it. */
function entry(owner: string, repo: string, over: Json = {}): Json {
  return {
    repository: `${owner}/${repo}`,
    owner,
    repo,
    visibility: 'private',
    default_branch: 'main',
    archived: false,
    can_push: true,
    can_admin: false,
    repo_id: `repo_${owner.length}${repo.length}`.padEnd(21, '0'),
    registered: false,
    ...over,
  }
}

/** One page of the readable list, in the API's shape. */
function page(n: number, repositories: Json[], over: Json = {}): Json {
  return {
    repositories,
    skipped: 0,
    page: n,
    per_page: 100,
    max_pages: 10,
    next_page: null,
    capped: false,
    token_scope: 'tenant',
    secret_name: 'swarm-tenant-eng-git',
    tenant_id: 'eng',
    ...over,
  }
}

const RUNTIMES = { runtimes: { 'claude-code': {}, codex: {} } }

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

function names(): string[] {
  return picks().map((p) => visible(p.querySelector('b')))
}

describe('Register repository pages through everything the token can read (OB0)', () => {
  it('follows next_page until the API serves none, and lists every page', async () => {
    const calls = serve((m, url) => {
      if (m !== 'GET') return null
      if (url === BASE) return { status: 200, body: page(1, [entry('example-org', 'example-api'), entry('example-org', 'example-web')], { next_page: 2 }) }
      if (url === `${BASE}?page=2`) return { status: 200, body: page(2, [entry('example-org', 'example-docs')], { next_page: 3 }) }
      if (url === `${BASE}?page=3`) return { status: 200, body: page(3, [entry('other-org', 'other-cli')]) }
      return null
    })
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    expect(names()).toEqual(['example-org/example-api', 'example-org/example-web', 'example-org/example-docs', 'other-org/other-cli'])
    const reads = calls.filter((c) => c.method === 'GET' && c.url.startsWith(BASE)).map((c) => c.url)
    expect(reads).toEqual([BASE, `${BASE}?page=2`, `${BASE}?page=3`])
    expect(screen.getByRole('searchbox').getAttribute('placeholder')).toBe('Filter the 4 repositories swarm-tenant-eng-git can read')
    expect(document.querySelector('.ur-capped')).toBeNull()
  })

  it('says so when the API capped the list, and points at the typed field', async () => {
    serve((m, url) => {
      if (m !== 'GET') return null
      if (url === BASE) return { status: 200, body: page(1, [entry('example-org', 'example-api')], { next_page: 2, max_pages: 2 }) }
      if (url === `${BASE}?page=2`) return { status: 200, body: page(2, [entry('example-org', 'example-web')], { max_pages: 2, capped: true }) }
      return null
    })
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(2), WAIT)
    const note = document.querySelector('.ur-capped')
    expect(note).not.toBeNull()
    expect(visible(note)).toBe(
      'The list stops at 2 pages of 100, and swarm-tenant-eng-git can read more than that. A repository not listed here can still be registered: type its owner/repo below.',
    )
  })

  it('a later page that fails keeps the pages read and names the one that did not come back', async () => {
    serve((m, url) => {
      if (m !== 'GET') return null
      if (url === BASE) return { status: 200, body: page(1, [entry('example-org', 'example-api')], { next_page: 2 }) }
      if (url === `${BASE}?page=2`) return { status: 502, body: { code: 'forge_unavailable', message: 'GitHub did not answer.' } }
      return null
    })
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(1), WAIT)
    const note = document.querySelector('.ur-capped')
    expect(visible(note)).toBe('Page 2 of the list could not be read (GitHub did not answer.), so this shows the first 1 only. Refresh to try again, or type owner/repo below.')
  })

  it('filters by owner with one chip per owner in the list', async () => {
    serve((m, url) => {
      if (m !== 'GET') return null
      if (url === BASE) {
        return {
          status: 200,
          body: page(1, [entry('example-org', 'example-api'), entry('other-org', 'other-cli'), entry('example-org', 'example-web'), entry('example-user', 'dotfiles')]),
        }
      }
      return null
    })
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    const owners = screen.getByRole('group', { name: 'Owner' })
    const chips = within(owners).getAllByRole('button')
    expect(chips.map((c) => visible(c))).toEqual(['All owners 4', 'example-org 2', 'example-user 1', 'other-org 1'])
    expect(chips[0]!.getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(within(owners).getByRole('button', { name: /^example-org/ }))
    expect(names()).toEqual(['example-org/example-api', 'example-org/example-web'])
    expect(within(owners).getByRole('button', { name: /^example-org/ }).getAttribute('aria-pressed')).toBe('true')

    // The name filter narrows within the owner.
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'web' } })
    expect(names()).toEqual(['example-org/example-web'])

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } })
    fireEvent.click(within(owners).getByRole('button', { name: /^All owners/ }))
    expect(picks()).toHaveLength(4)
  })

  it('a typed owner/repo is registered by name, and the API\'s refusal is shown in its own words', async () => {
    const refusal = 'swarm-tenant-eng-git cannot read sso-org/private-app: sso-org enforces SAML single sign-on and the token is not authorised for it.'
    const calls = serve((m, url) => {
      if (m === 'GET' && url === BASE) return { status: 200, body: page(1, [entry('example-org', 'example-api')]) }
      if (m === 'GET' && url === '/v1/runtimes') return { status: 200, body: RUNTIMES }
      if (m === 'POST' && url === '/v1/repositories') return { status: 422, body: { code: 'no_access', message: refusal } }
      return null
    })
    const { go } = await mount()
    await waitFor(() => expect(picks()).toHaveLength(1), WAIT)
    const next = screen.getByRole('button', { name: 'Next: schedule' }) as HTMLButtonElement
    const typed = screen.getByRole('textbox', { name: 'Not listed? Type owner/repo' }) as HTMLInputElement

    fireEvent.change(typed, { target: { value: 'not-a-name' } })
    expect(next.disabled).toBe(true)
    fireEvent.change(typed, { target: { value: ' sso-org/private-app ' } })
    expect(next.disabled).toBe(false)
    // Typing a name is the pick: no listed row stays chosen beside it.
    expect(picks().some((p) => p.getAttribute('aria-checked') === 'true')).toBe(false)
    fireEvent.click(next)

    await waitFor(() => screen.getByRole('button', { name: 'Register sso-org/private-app' }), WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Register sso-org/private-app' }))
    await waitFor(() => expect(document.body.textContent).toContain(refusal), WAIT)
    const post = calls.find((c) => c.method === 'POST')!
    // No default branch: the API reads it from the forge when it registers.
    expect(post.body).toEqual({ repository: 'sso-org/private-app', allowed_profiles: ['claude-code'], index: { interval_hours: 24, on_change: 'poll' } })
    expect(go).not.toHaveBeenCalled()
  })

  it('the typed field is there even when the token can list nothing', async () => {
    serve((m, url) => (m === 'GET' && url === BASE ? { status: 200, body: page(1, []) } : null))
    await mount()
    await waitFor(() => expect(document.body.textContent).toContain('The token can read no repositories'), WAIT)
    fireEvent.change(screen.getByRole('textbox', { name: 'Not listed? Type owner/repo' }), { target: { value: 'example-org/example-api' } })
    expect((screen.getByRole('button', { name: 'Next: schedule' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('reads the visibility the API serves, and says archived', async () => {
    serve((m, url) => {
      if (m !== 'GET' || url !== BASE) return null
      return {
        status: 200,
        body: page(1, [
          entry('example-org', 'example-api', { visibility: 'private' }),
          entry('example-org', 'example-web', { visibility: 'internal' }),
          entry('example-org', 'example-docs', { visibility: 'public', archived: true }),
          entry('example-org', 'example-old', { visibility: 'unknown' }),
        ]),
      }
    })
    await mount()
    await waitFor(() => expect(picks()).toHaveLength(4), WAIT)
    const notes = picks().map((p) => visible(p.querySelector('small')))
    expect(notes[0]).toMatch(/^private · /)
    expect(notes[1]).toMatch(/^internal · /)
    expect(notes[2]).toMatch(/^public · archived · /)
    expect(notes[3]).toMatch(/^visibility not read · /)
  })
})
