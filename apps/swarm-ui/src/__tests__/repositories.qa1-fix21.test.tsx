// CONSOLE VISUAL QA PASS 1, Config › Repositories (QA1-FIX-21; part of
// #2596).
//
// WHAT EACH CASE HOLDS:
//   * config-16: the page draws one Register repository and one button that
//     leaves for GitHub access › Choose repositories, and that one is named
//     for what it does -- "Choose more on GitHub" once something is chosen,
//     "Choose on GitHub" while nothing is -- never "Add new repository",
//     which read as a second button for Register's job;
//   * config-17: a red ◆ after the freshness chip says beside it how many
//     GitHub permissions are missing (the fixture console's every card), the
//     grey ring says they were not all checked, and a green dot asks for
//     nothing and carries no word.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

const GRANT = (n: number, repository: string) => ({
  repo_id: `repo_${String(n).padStart(16, '0')}`,
  repository,
  owner: repository.split('/')[0],
  mode: 'write',
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: { clone: { state: 'ok', code: null, checked_at: null } },
  verified_at: null,
})

const ACCESS = (grants: unknown[]) => ({
  connection: { state: 'active', forge_login: 'octo-dev' },
  orgs: [],
  grants,
  tenant_id: 'eng',
})

const ALL_TRUE = { push: true, pull_request: true, merge: true, ci: true, issues: true, actions: true }
const GAP = (permission: string | null, capabilities: string[]) => ({
  permission,
  access: 'read',
  granted: null,
  needed_for: `${capabilities.join(' and ')} cannot run`,
  capabilities,
})

/** `GET /v1/forge/permissions` with one row per registered repository, as the fixture console answers it. */
function forge(rows: { repo_id: unknown; capabilities: Record<string, boolean | null>; blocked_by: unknown[] }[]) {
  return {
    checked_at: new Date().toISOString(),
    required: [],
    workflow_files_required: null,
    app: { configured: true, slug: 'swarmcloud', granted: {}, missing: [], settings_url: null, error: null },
    orgs: [],
    repositories: rows.map((r) => ({
      repo: 'example-org/example-api',
      credential: 'app',
      installation_id: 51_877_204,
      error: null,
      ...r,
    })),
    cached: false,
  }
}

function live(registered: unknown[], grants: unknown[], perms: unknown = null) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === '/v1/repositories') return { status: 200, body: { repositories: registered } }
    if (url === '/v1/access') return { status: 200, body: ACCESS(grants) }
    if (url.startsWith('/v1/forge/permissions') && perms !== null) return { status: 200, body: perms }
    return null
  })
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  return render(<RepositoriesScreen view={null} go={vi.fn()} />)
}

afterEach(() => {
  vi.unstubAllEnvs()
})

const links = () => Array.from(document.querySelectorAll<HTMLAnchorElement>('a')).map((a) => visible(a))
const lead = () => document.querySelector<HTMLElement>('section[aria-label="Next step"]')

describe('config-16: one add action, named for what it does', () => {
  it('every chosen repository registered: the lead says Choose more on GitHub, and nothing says Add new repository', async () => {
    live([repo()], [GRANT(1, 'example-org/example-api')])
    await mount()
    await waitFor(() => expect(visible(lead())).toContain('Every chosen repository is registered'), WAIT)
    // MUTATION: put "Add new repository" back on the button and both of these fail.
    expect(links()).not.toContain('Add new repository')
    expect(links().filter((t) => t === 'Choose more on GitHub')).toHaveLength(1)
    // The lead's sentence names the button by its new words.
    expect(visible(lead())).toContain('Choose more on GitHub opens GitHub access › Choose repositories.')
    expect(visible(lead())).not.toContain('Add new repository')
  })

  it('nothing chosen: the button says Choose on GitHub, not "more"', async () => {
    live([], [])
    await mount()
    await waitFor(() => expect(visible(lead())).toContain('No repository chosen yet'), WAIT)
    // MUTATION: say "Choose more on GitHub" with nothing chosen and this fails.
    await waitFor(() => expect(links().filter((t) => t === 'Choose on GitHub')).toHaveLength(1), WAIT)
    expect(links()).not.toContain('Choose more on GitHub')
    expect(links()).not.toContain('Add new repository')
  })

  it('a chosen repository not registered: the lead registers it, and the head carries Choose more on GitHub as a secondary', async () => {
    live([repo()], [GRANT(1, 'example-org/example-api'), GRANT(2, 'example-labs/console')])
    await mount()
    await waitFor(() => expect(visible(lead())).toContain('example-labs/console is chosen but not registered'), WAIT)
    // QA2-FIX-19 (config2-08): the lead's Register is the one primary add
    // action; the head's Register repository beside it was a second.
    // MUTATION: draw the head's Register repository beside the lead's Register and this finds it.
    expect(links()).not.toContain('Register repository')
    expect(Array.from(lead()!.querySelectorAll('a')).map((a) => visible(a))).toEqual(['Register'])
    // MUTATION: name the head's button for the empty state ("Choose on GitHub") and this fails.
    const more = Array.from(document.querySelectorAll<HTMLAnchorElement>('a')).filter((a) => visible(a) === 'Choose more on GitHub')
    expect(more).toHaveLength(1)
    // MUTATION: leave it primary and the page has two primary add actions.
    expect(more[0]!.className).not.toMatch(/is-primary/)
    expect(links()).not.toContain('Add new repository')
  })
})

describe('config-17: the permission dot says what it asks for', () => {
  const card = () => document.querySelector<HTMLElement>('.ur-cards > .ur-card:not(.is-chosen)')
  const word = () => card()?.querySelector<HTMLElement>('[data-perm-word]') ?? null

  it('a red dot with one permission missing (the fixture console) names it beside it, and where it is granted (QA2-FIX-19 config2-01)', async () => {
    const r = repo()
    live([r], [GRANT(1, 'example-org/example-api')], forge([{ repo_id: r.repo_id, capabilities: { ...ALL_TRUE, actions: false }, blocked_by: [GAP('actions', ['actions'])] }]))
    await mount()
    await waitFor(() => expect(card()?.querySelector('.fp-dot')).toBeTruthy(), WAIT)
    // MUTATION: drop the word beside RepoDot and the red ◆ is unexplained again; this is null.
    expect(word()).not.toBeNull()
    expect(visible(word())).toBe('Not granted on GitHub: Actions (read) Grant it in Workspace setup')
    expect(word()!.getAttribute('data-perm-word')).toBe('bad')
    // It follows the dot in the badge row, and its words' tooltip is the dot's.
    const badge = card()!.querySelector<HTMLElement>('[data-badge]')!
    expect(badge.contains(word())).toBe(true)
    expect(word()!.querySelector('[title]')!.getAttribute('title')).toBe(card()!.querySelector('.fp-dot')!.getAttribute('title'))
  })

  it('names permissions, not gaps: two gaps on one permission are one; a refusal with no permission named counts on its own', async () => {
    const r = repo()
    live(
      [r],
      [GRANT(1, 'example-org/example-api')],
      forge([
        {
          repo_id: r.repo_id,
          capabilities: { ...ALL_TRUE, merge: false, ci: false, push: false },
          blocked_by: [GAP('statuses', ['merge']), GAP('statuses', ['ci']), GAP(null, ['push'])],
        },
      ]),
    )
    await mount()
    await waitFor(() => expect(word()).not.toBeNull(), WAIT)
    // MUTATION: name each gap and Statuses is said twice.
    expect(visible(word())).toBe('Not granted on GitHub: Statuses (read) and 1 more Grant it in Workspace setup')
  })

  it('the grey ring says the permissions were not all checked; the green dot carries no word', async () => {
    const r = repo()
    live([r], [GRANT(1, 'example-org/example-api')], forge([{ repo_id: r.repo_id, capabilities: { ...ALL_TRUE, actions: null }, blocked_by: [] }]))
    const { unmount } = await mount()
    await waitFor(() => expect(word()).not.toBeNull(), WAIT)
    // MUTATION: word only the red dot and the ring is unexplained; this is null above.
    expect(visible(word())).toBe('GitHub permissions not all checked')
    unmount()

    live([r], [GRANT(1, 'example-org/example-api')], forge([{ repo_id: r.repo_id, capabilities: ALL_TRUE, blocked_by: [] }]))
    await mount()
    await waitFor(() => expect(card()?.querySelector('.fp-dot[data-status="ok"]')).toBeTruthy(), WAIT)
    // MUTATION: word every tone and a healthy card says something here.
    expect(word()).toBeNull()
  })
})
