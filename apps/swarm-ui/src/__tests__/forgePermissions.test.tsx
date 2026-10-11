// GITHUB PERMISSIONS, IN THE CONSOLE (#1086, lane PERM-2; owner decision
// 2026-10-11, design 2 "Org cards"). The read is PERM-1's
// `GET /v1/forge/permissions` (swarm_api/forgeperms.py); the bodies below are
// shaped exactly as `_for_caller` answers it.
//
// WHAT EACH CASE HOLDS:
//   * an org whose installation holds everything draws a COLLAPSED card;
//   * an org missing a permission the App requests draws OPEN, with the
//     catalogue's plain-words breakage and a link to exactly its review URL,
//     in a new tab: SwarmCloud never grants, it links;
//   * the App-level fix is a platform admin's: a non-admin never sees its
//     button, even if a server sent the URL anyway; an admin does;
//   * Work › Repositories draws a dot per repository, red with the problem in
//     its tooltip when the repository is affected;
//   * a repository's page shows its own org's card, not another org's;
//   * Re-check reads with `refresh=true`, and once GitHub answers granted the
//     card collapses and the dots turn green -- Setup and the list together,
//     because they share one read;
//   * returning to the tab (`visibilitychange` to visible) reads again.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const REPO_ID = 'repo_0a1b2c3d4e5f6071'
const OTHER_ID = 'repo_9999999999999999'
const REVIEW = 'https://github.com/organizations/example-org/settings/installations/4242/permissions/update'
const APP_URL = 'https://github.com/organizations/example-host/settings/apps/swarmcloud-example/permissions'
const STATUSES_BREAK = "merges can't see CI, so they fail"

type Json = Record<string, unknown>

function statusesGap(fix: 'accept' | 'app', url: string | null): Json {
  const gap: Json = { permission: 'statuses', level: 'read', held: null, needed_for: "the merge step's read of commit statuses", breaks: STATUSES_BREAK, fix }
  if (url !== null) gap.fix_url = url
  return gap
}

function orgCard(owner: string, gaps: Json[], repositories: string[]): Json {
  return {
    owner,
    owner_type: 'Organization',
    installation_id: owner === 'example-org' ? 4242 : 5151,
    repository_selection: 'all',
    status: gaps.length > 0 ? 'missing' : 'ok',
    reason: null,
    gaps,
    repositories,
  }
}

/** The answer, as `_for_caller` shapes it: one App card, one card per org, a row per repository. */
function answer({ orgGaps = [], appGaps = [], appUrl = null }: { orgGaps?: Json[]; appGaps?: Json[]; appUrl?: string | null } = {}): Json {
  const missing = orgGaps.length > 0
  const app: Json = { slug: 'swarmcloud-example', owner: 'example-host', owner_type: 'Organization', status: appGaps.length > 0 ? 'missing' : 'ok', reason: null, gaps: appGaps }
  if (appUrl !== null) app.permissions_url = appUrl
  return {
    tenant_id: 'eng',
    checked_at: new Date().toISOString(),
    cached: false,
    required: [],
    app,
    orgs: [orgCard('example-org', orgGaps, [REPO_ID]), orgCard('other-org', [], [OTHER_ID])],
    repositories: [
      {
        repo_id: REPO_ID,
        repository: 'example-org/example-api',
        owner: 'example-org',
        source: 'app',
        status: missing ? 'missing' : 'ok',
        reason: null,
        problems: missing ? [{ permission: 'statuses', level: 'read', breaks: STATUSES_BREAK }] : [],
      },
      { repo_id: OTHER_ID, repository: 'other-org/other-api', owner: 'other-org', source: 'app', status: 'ok', reason: null, problems: [] },
    ],
  }
}

const ONBOARDING = {
  tenant_id: 'eng',
  user: 'dev@swarm.example.com',
  user_hash: '89abcdef01234567',
  derived_at: new Date().toISOString(),
  next_step: null,
  complete: true,
  source: 'derived on this read',
  steps: [{ step: 'signed_in', state: 'done', code: null, copy: null, checked_at: null, evidence: {}, issues: [] }],
}

const me = (admin: boolean) => ({
  tenant: { tenant_id: 'eng', display_name: 'eng@swarm.example.com' },
  principal: { email: 'dev@swarm.example.com', domain: 'swarm.example.com', groups: [], is_admin: admin },
  environment: 'dev',
  environment_declared: true,
})

const PERMS = '/v1/forge/permissions'

/** Serves every read these screens make; `perms(refresh)` answers the permissions read. */
function routes(perms: (refresh: boolean) => Json, { admin = false }: { admin?: boolean } = {}) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === PERMS) return { status: 200, body: perms(false) }
    if (url === `${PERMS}?refresh=true`) return { status: 200, body: perms(true) }
    if (url === '/v1/tenants/me') return { status: 200, body: me(admin) }
    if (url === '/v1/onboarding') return { status: 200, body: ONBOARDING }
    if (url === '/v1/repositories') return { status: 200, body: { repositories: [repo(), repo({ repo_id: OTHER_ID, owner: 'other-org', repo: 'other-api' })] } }
    if (url === `/v1/repositories/${REPO_ID}`) return { status: 200, body: { repository: repo(), index_runs: [], used_by: [] } }
    return null
  })
}

async function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { OnboardingScreen } = await import('../Onboarding')
  const { RepositoriesScreen } = await import('../Repositories')
  return { OnboardingScreen, RepositoriesScreen }
}

const orgEl = (owner: string) => document.querySelector(`[data-forge-card="org"][data-owner="${owner}"]`) as HTMLElement | null
const appEl = () => document.querySelector('[data-forge-card="app"]') as HTMLElement | null
const toggle = (card: HTMLElement) => card.querySelector('[aria-expanded]') as HTMLElement
const dot = (repoId: string) => document.querySelector(`[data-perm-dot="${repoId}"]`) as HTMLElement | null
const permReads = (calls: { method: string; url: string }[]) => calls.filter((c) => c.method === 'GET' && c.url.startsWith(PERMS))

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('Work › Setup › GitHub draws one card per org and one for the App', () => {
  it('collapses an org card when every permission is granted', async () => {
    routes(() => answer())
    const { OnboardingScreen } = await live()
    render(<OnboardingScreen />)
    await waitFor(() => expect(orgEl('example-org')).not.toBeNull(), WAIT)
    const card = orgEl('example-org')!
    expect(card.dataset.status).toBe('ok')
    expect(toggle(card).getAttribute('aria-expanded')).toBe('false')
    expect(card.querySelectorAll('[data-gap]')).toHaveLength(0)
    expect(visible(card)).toContain('all granted')
    // The App card is there too, and also collapsed.
    expect(toggle(appEl()!).getAttribute('aria-expanded')).toBe('false')
    // A fix is a link; SwarmCloud never grants.
    expect(visible(screen.getByRole('region', { name: 'GitHub' }))).toContain("GitHub does not let SwarmCloud grant permissions itself")
  })

  it('opens an org missing a requested permission, with its plain-words breakage and a link to exactly its review URL', async () => {
    routes(() => answer({ orgGaps: [statusesGap('accept', REVIEW)] }))
    const { OnboardingScreen } = await live()
    render(<OnboardingScreen />)
    await waitFor(() => expect(orgEl('example-org')?.dataset.status).toBe('missing'), WAIT)
    const card = orgEl('example-org')!
    expect(toggle(card).getAttribute('aria-expanded')).toBe('true')
    const gap = card.querySelector('[data-gap="statuses"]') as HTMLElement
    expect(visible(gap)).toContain(STATUSES_BREAK)
    expect(visible(gap)).toContain('Commit statuses')
    const link = within(gap).getByRole('link')
    expect(link.getAttribute('href')).toBe(REVIEW)
    expect(link.getAttribute('target')).toBe('_blank')
    // The fully granted org beside it stays collapsed.
    expect(toggle(orgEl('other-org')!).getAttribute('aria-expanded')).toBe('false')
  })

  it('never shows a non-admin the App-level button, even when a URL was sent', async () => {
    routes(() => answer({ appGaps: [statusesGap('app', APP_URL)], appUrl: APP_URL }), { admin: false })
    const { OnboardingScreen } = await live()
    render(<OnboardingScreen />)
    await waitFor(() => expect(appEl()?.dataset.status).toBe('missing'), WAIT)
    await waitFor(() => expect(document.querySelector('[data-forge-admin]')?.getAttribute('data-forge-admin')).toBe('false'), WAIT)
    const card = appEl()!
    expect(toggle(card).getAttribute('aria-expanded')).toBe('true')
    expect(visible(card)).toContain(STATUSES_BREAK)
    expect(within(card).queryAllByRole('link')).toHaveLength(0)
    expect(document.querySelector(`a[href="${APP_URL}"]`)).toBeNull()
  })

  it('shows an admin the App-level button to the App permissions page (the control for the case above)', async () => {
    routes(() => answer({ appGaps: [statusesGap('app', APP_URL)], appUrl: APP_URL }), { admin: true })
    const { OnboardingScreen } = await live()
    render(<OnboardingScreen />)
    await waitFor(() => expect(appEl()?.querySelector(`a[href="${APP_URL}"]`)).not.toBeNull(), WAIT)
    expect(appEl()!.querySelector(`a[href="${APP_URL}"]`)!.getAttribute('target')).toBe('_blank')
  })
})

describe('Work › Repositories carries each repository status', () => {
  it('draws a red dot whose tooltip names the problem on an affected repository', async () => {
    routes(() => answer({ orgGaps: [statusesGap('accept', REVIEW)] }))
    const { RepositoriesScreen } = await live()
    render(<RepositoriesScreen view={null} go={vi.fn()} />)
    await waitFor(() => expect(dot(REPO_ID)?.dataset.status).toBe('missing'), WAIT)
    const red = dot(REPO_ID)!
    expect(red.getAttribute('title')).toContain(STATUSES_BREAK)
    expect(red.getAttribute('aria-label')).toContain(STATUSES_BREAK)
    expect(red.className).toContain('is-missing')
    expect(dot(OTHER_ID)!.dataset.status).toBe('ok')
  })

  it("shows the repository page its own org's card", async () => {
    routes(() => answer({ orgGaps: [statusesGap('accept', REVIEW)] }))
    const { RepositoriesScreen } = await live()
    render(<RepositoriesScreen view={`repo=${REPO_ID}`} go={vi.fn()} />)
    await waitFor(() => expect(orgEl('example-org')).not.toBeNull(), WAIT)
    expect(orgEl('other-org')).toBeNull()
    expect(appEl()).toBeNull()
    const card = orgEl('example-org')!
    expect(toggle(card).getAttribute('aria-expanded')).toBe('true')
    expect(card.querySelector('[data-gap="statuses"] a')?.getAttribute('href')).toBe(REVIEW)
  })
})

describe('the console re-checks', () => {
  it('reads with refresh=true on Re-check, and a granted answer turns the card and the dots green', async () => {
    const calls = routes((refresh) => (refresh ? answer() : answer({ orgGaps: [statusesGap('accept', REVIEW)] })))
    const { OnboardingScreen, RepositoriesScreen } = await live()
    render(
      <>
        <OnboardingScreen />
        <RepositoriesScreen view={null} go={vi.fn()} />
      </>,
    )
    await waitFor(() => expect(dot(REPO_ID)?.dataset.status).toBe('missing'), WAIT)
    expect(orgEl('example-org')!.dataset.status).toBe('missing')
    expect(permReads(calls).some((c) => c.url.includes('refresh'))).toBe(false)

    fireEvent.click(within(screen.getByRole('region', { name: 'GitHub' })).getByRole('button', { name: 'Re-check' }))
    await waitFor(() => expect(orgEl('example-org')?.dataset.status).toBe('ok'), WAIT)
    expect(permReads(calls).map((c) => c.url)).toContain(`${PERMS}?refresh=true`)
    expect(toggle(orgEl('example-org')!).getAttribute('aria-expanded')).toBe('false')
    expect(dot(REPO_ID)!.dataset.status).toBe('ok')
    expect(dot(REPO_ID)!.className).toContain('is-ok')
  })

  it('reads again when the tab becomes visible', async () => {
    const calls = routes(() => answer())
    const { OnboardingScreen } = await live()
    render(<OnboardingScreen />)
    await waitFor(() => expect(orgEl('example-org')).not.toBeNull(), WAIT)
    const before = permReads(calls).length
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await waitFor(() => expect(permReads(calls).length).toBe(before + 1), WAIT)
    expect(permReads(calls).at(-1)!.url).toBe(PERMS)
  })
})
