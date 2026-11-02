// CONFIG › REPOSITORIES IS REPOSITORY MANAGEMENT (REPOS-PAGE; part of #2157,
// BGD's decisions of 2026-10-23 and his pick of proposal E, config-e.html S3a
// to S3c).
//
// WHAT EACH CASE HOLDS:
//   * "Repositories SwarmCloud may reach as you" (ReachableRepositories.tsx,
//     extracted by GITHUB-ACCESS-PAGE) is mounted at the top of the list,
//     above the registry, with its rows;
//   * Choose more on GitHub (Choose on GitHub while nothing is chosen; it was
//     "Add new repository" until QA1-FIX-21, config-16) goes to Config ›
//     GitHub access with Choose repositories open (Access.tsx's
//     ADD_REPOSITORY_ADDRESS);
//   * the registry's cards and QA targets are still on the page, below;
//   * a chosen repository that is not registered is a dashed card with
//     Register, and the page's lead names it (S3a);
//   * with nothing chosen the lead carries Choose on GitHub and the head
//     does not repeat it (S3c).
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

const GRANT = (n: number, repository: string, mode: 'read' | 'write') => ({
  repo_id: `repo_${String(n).padStart(16, '0')}`,
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

const ACCESS = (grants: unknown[]) => ({
  connection: { state: 'active', forge_login: 'octo-dev' },
  orgs: [],
  grants,
  tenant_id: 'eng',
})

function live(registered: unknown[], grants: unknown[]) {
  return serve((m, url) => {
    if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: { repositories: registered } }
    if (m === 'GET' && url === '/v1/access') return { status: 200, body: ACCESS(grants) }
    return null
  })
}

async function mount(view: string | null = null, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={view} go={go} />)
  return { ...utils, go }
}

const reach = () => document.querySelector<HTMLElement>('.ac-grants')
const cards = () => Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card'))
const ADD_WORDS = ['Choose on GitHub', 'Choose more on GitHub']
const addButtons = () => Array.from(document.querySelectorAll<HTMLAnchorElement>('a')).filter((a) => ADD_WORDS.includes(visible(a)))

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('Config › Repositories: what SwarmCloud may reach as you, then the registry', () => {
  it('mounts "Repositories SwarmCloud may reach as you" at the top, above the registered cards', async () => {
    live([repo()], [GRANT(1, 'example-org/example-api', 'write'), GRANT(2, 'example-org/example-docs', 'read')])
    await mount()
    // MUTATION: drop the ReachableRepositories mount from the list and this never appears.
    await waitFor(() => expect(reach()).not.toBeNull(), WAIT)
    expect(within(reach()!).getByRole('heading', { level: 2, name: 'Repositories SwarmCloud may reach as you' })).toBeTruthy()
    // MUTATION: mount the card without the access read's grants and the rows are missing.
    await waitFor(() => expect(reach()!.querySelectorAll('li.ac-grant')).toHaveLength(2), WAIT)
    await waitFor(() => expect(cards().length).toBeGreaterThan(0), WAIT)
    // MUTATION: mount the card under the registry and it follows the first card.
    expect(reach()!.compareDocumentPosition(cards()[0]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('Choose more on GitHub opens GitHub access with Choose repositories open', async () => {
    live([repo()], [GRANT(1, 'example-org/example-api', 'write')])
    const { go } = await mount()
    await waitFor(() => expect(addButtons()).toHaveLength(1), WAIT)
    const { ADD_REPOSITORY_ADDRESS } = await import('../Access')
    // MUTATION: point the button at config/github (Connection) and both of these fail.
    expect(ADD_REPOSITORY_ADDRESS).toBe('config/github?view=choose')
    expect(addButtons()[0]!.getAttribute('href')).toBe('/config/github?view=choose')
    fireEvent.click(addButtons()[0]!)
    // MUTATION: make the click a plain link the router never sees and go is not called.
    expect(go).toHaveBeenCalledWith('config/github?view=choose')
  })

  it('the address the choose button goes to is GitHub access with view=choose kept', async () => {
    vi.resetModules()
    const { fromAddress, canonical } = await import('../App')
    const r = fromAddress('config/github?view=choose')
    // MUTATION: drop `view` from GitHub access' kept keys and the chooser would open on Connection.
    expect([r.sectionId, r.tab, r.view]).toEqual(['config', 'github', 'view=choose'])
    expect(canonical(r)).toBe('config/github?view=choose')
  })

  it('keeps the registry cards and QA targets on the page', async () => {
    live([repo(), repo({ repo_id: 'repo_1111111111111111', repo: 'example-web' })], [GRANT(1, 'example-org/example-api', 'write')])
    await mount()
    // MUTATION: drop the RepositoryList from the page and there are no cards.
    await waitFor(() => expect(cards()).toHaveLength(2), WAIT)
    expect(cards().map((c) => visible(c.querySelector('h2')))).toEqual(['example-org/example-api', 'example-org/example-web'])
    expect(within(cards()[0]!).getByRole('button', { name: 'Index now' })).toBeTruthy()
    // MUTATION: drop the QA targets tab and this is null.
    const tabs = document.querySelector<HTMLElement>('[role="tablist"][aria-label="Repositories pages"]')
    expect(tabs).not.toBeNull()
    expect(document.querySelector('.c-phead')!.compareDocumentPosition(tabs!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(Array.from(tabs!.querySelectorAll('a')).map((a) => [visible(a), a.getAttribute('href'), a.getAttribute('aria-current')])).toEqual([
      ['Repositories', '/config/repositories', 'page'],
      ['QA targets', '/config/repositories?page=qa-targets', null],
    ])
  })

  it('QA targets is the second tab of the same strip, current on its page', async () => {
    serve(() => null)
    await mount('page=qa-targets')
    const tabs = document.querySelector<HTMLElement>('[role="tablist"][aria-label="Repositories pages"]')
    // MUTATION: draw the strip on the list only and the QA targets page loses its way back.
    expect(tabs).not.toBeNull()
    expect(tabs!.querySelector('a[aria-current="page"]')?.textContent).toBe('QA targets')
    expect(document.querySelector('h1')?.textContent).toBe('QA targets')
    // MUTATION: draw the strip above the QA targets head and it moves between the two tabs' pages.
    expect(document.querySelector('.c-phead')!.compareDocumentPosition(tabs!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('the page is led by what is missing (proposal E)', () => {
  it('a chosen repository that is not registered is a dashed card with Register, and the lead names it (S3a)', async () => {
    live([repo()], [GRANT(1, 'example-org/example-api', 'write'), GRANT(2, 'example-labs/console', 'read')])
    const { go } = await mount()
    const dashed = () => document.querySelector<HTMLElement>('.ur-cards > .ur-card.is-chosen')
    // MUTATION: drop the chosen-but-not-registered cards and this is null.
    await waitFor(() => expect(dashed()).not.toBeNull(), WAIT)
    expect(visible(dashed()!.querySelector('h2'))).toBe('example-labs/console')
    expect(visible(dashed()!)).toContain('chosen, not registered')
    expect(visible(dashed()!)).toContain('Read grant')
    // MUTATION: match the registry by repo name alone and the registered one is drawn dashed too.
    expect(document.querySelectorAll('.ur-cards > .ur-card.is-chosen')).toHaveLength(1)
    // MUTATION: count the dashed card as registered, or leave it uncounted, and the note disagrees with the grid.
    expect(visible(document.querySelector('.c-count-note'))).toContain('Registered repositories · 1 · 1 chosen, not registered')
    const lead = document.querySelector<HTMLElement>('section[aria-label="Next step"]')
    expect(lead).not.toBeNull()
    expect(visible(lead!)).toContain('example-labs/console is chosen but not registered')
    // MUTATION: lead with the choose button here and the head would not carry it.
    expect(addButtons()).toHaveLength(1)
    expect(lead!.contains(addButtons()[0]!)).toBe(false)
    fireEvent.click(within(dashed()!).getByRole('link', { name: 'Register' }))
    expect(go).toHaveBeenCalledWith('config/repositories?page=register&repo=example-labs%2Fconsole')
  })

  it('with nothing chosen the lead carries Choose on GitHub and the head does not repeat it (S3c)', async () => {
    live([], [])
    await mount()
    const lead = () => document.querySelector<HTMLElement>('section[aria-label="Next step"]')
    await waitFor(() => expect(lead()).not.toBeNull(), WAIT)
    // MUTATION: read the step's place from a fixed "of 4" and a personal workspace's strip disagrees.
    expect(visible(lead()!)).toMatch(/Step 2 of \d · next\./)
    expect(visible(lead()!)).toContain('No repository chosen yet')
    // MUTATION: draw the choose button in the head as well and there are two.
    await waitFor(() => expect(addButtons()).toHaveLength(1), WAIT)
    expect(lead()!.contains(addButtons()[0]!)).toBe(true)
    // The registry's empty state says what it waits for (S3c's Registered repositories).
    await waitFor(() => expect(visible(document.querySelector('.ur-list'))).toContain('A repository is registered after it is chosen'), WAIT)
  })

  it('every chosen repository registered: the lead says so and carries Choose more on GitHub', async () => {
    live([repo()], [GRANT(1, 'Example-Org/Example-API', 'write')])
    await mount()
    const lead = () => document.querySelector<HTMLElement>('section[aria-label="Next step"]')
    await waitFor(() => expect(lead()).not.toBeNull(), WAIT)
    // MUTATION: compare owner/repo case-sensitively and this one reads as not registered (GitHub names are not).
    await waitFor(() => expect(visible(lead()!)).toContain('Every chosen repository is registered'), WAIT)
    expect(document.querySelector('.ur-card.is-chosen')).toBeNull()
    expect(addButtons()).toHaveLength(1)
    expect(lead()!.contains(addButtons()[0]!)).toBe(true)
  })
})
