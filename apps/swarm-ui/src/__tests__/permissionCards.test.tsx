// GITHUB PERMISSIONS HEALTH, DESIGN 2 "ORG CARDS" (#1086, PERM-2).
//
// WHAT EACH CASE HOLDS:
//   * an org whose installation grants everything is a COLLAPSED card that
//     says "all granted"; one missing a permission is OPEN, with a count
//     chip, the gap in plain words (`needed_for`) and a "Review on GitHub"
//     link to exactly its review URL, in a new tab;
//   * "Add to App" is drawn for a platform admin only -- never for a member,
//     even if a URL were served;
//   * "↻ Re-check" reads `refresh=true`; after a fix button, coming back to
//     the tab (focus) re-checks by itself, and a granted answer turns the card
//     collapsed and green;
//   * the list's dot is red with a tooltip naming the first blocked
//     capability and its cause, and the ok mark when everything is granted;
//   * an org whose installation could not be read shows its `error` on its
//     own card, open, while the other cards still draw;
//   * the repository page shows its own org's card;
//   * loading and the empty state (no connected org).
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const REVIEW = 'https://github.com/organizations/example-org/settings/installations/42/permissions/update'
const SETTINGS = 'https://github.com/organizations/swarm-owner/settings/apps/swarmcloud-saga/permissions'
const ORG_REPO = 'repo_0a1b2c3d4e5f6071'
const TOKEN_REPO = 'repo_2222222222222222'

const STATUSES = { permission: 'statuses', access: 'read', needed_for: "merges can't see CI, so they fail", capabilities: ['merge', 'ci'] }
const CONTENTS = { permission: 'contents', access: 'write', needed_for: "runs can't push their branch, and merges can't land", capabilities: ['push', 'merge'] }
const ALL_TRUE = { push: true, pull_request: true, merge: true, ci: true, issues: true, actions: true }

function org(over: Record<string, unknown> = {}) {
  return {
    org: 'example-org',
    account_type: 'Organization',
    installation_id: 42,
    granted: {},
    missing: [],
    pending_update: false,
    pending: [],
    review_url: REVIEW,
    error: null,
    ...over,
  }
}

function answer({ gap = false, settingsUrl = null as string | null, orgs = null as unknown[] | null, appMissing = [] as unknown[] } = {}) {
  const theOrg = gap
    ? org({ missing: [{ ...STATUSES, granted: null }], pending_update: true, pending: [{ permission: 'statuses', requested: 'read', granted: null }] })
    : org()
  return {
    tenant_id: 'eng',
    checked_at: new Date().toISOString(),
    required: [CONTENTS, STATUSES],
    app: { configured: true, slug: 'swarmcloud-saga', granted: {}, requested: {}, missing: appMissing, settings_url: settingsUrl, error: null },
    orgs: orgs ?? [theOrg],
    repositories: [
      {
        repo_id: ORG_REPO,
        repo: 'example-org/example-api',
        credential: 'app',
        installation_id: 42,
        capabilities: gap ? { ...ALL_TRUE, merge: false, ci: false } : ALL_TRUE,
        blocked_by: gap ? [{ ...STATUSES, granted: null }] : [],
        error: null,
      },
    ],
    cached: false,
  }
}

function me(isAdmin: boolean) {
  return { tenant: { tenant_id: 'eng' }, principal: { email: 'dev@swarm.example.com', domain: 'swarm.example.com', groups: [], is_admin: isAdmin } }
}

type Answer = ReturnType<typeof answer>

function routes(perms: () => Answer | { status: number; body: unknown }, isAdmin = false) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === '/v1/tenants/me') return { status: 200, body: me(isAdmin) }
    if (url === '/v1/forge/permissions' || url === '/v1/forge/permissions?refresh=true') {
      const p = perms()
      return 'status' in p && 'body' in p ? (p as { status: number; body: unknown }) : { status: 200, body: p }
    }
    return null
  })
}

async function mountSection() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { GitHubPermissions } = await import('../PermissionCards')
  return render(<GitHubPermissions />)
}

const orgCard = () => document.querySelector<HTMLElement>('.fp-card.is-bad, .fp-card.is-ok, .fp-card.is-unknown')
const cardsOf = () => Array.from(document.querySelectorAll<HTMLElement>('.fp-card'))
const cardNamed = (name: string) => cardsOf().find((c) => visible(c.querySelector('h3 b')) === name)

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('Setup › GitHub: one card for the App and one per org', () => {
  it('draws a fully granted org collapsed, saying all granted', async () => {
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    expect(card.classList.contains('is-ok')).toBe(true)
    expect(visible(card.querySelector('.c-chip'))).toBe('all granted')
    expect(card.querySelector('.fp-body')).toBeNull()
    expect(card.querySelector('button[aria-expanded]')?.getAttribute('aria-expanded')).toBe('false')
    expect(cardNamed('swarmcloud-saga')).toBeDefined()
  })

  it('draws an org missing a permission open, in plain words, with Review on GitHub to exactly its review page', async () => {
    routes(() => answer({ gap: true }))
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')?.querySelector('.fp-body')).not.toBeNull(), WAIT)
    const card = cardNamed('example-org')!
    expect(card.classList.contains('is-bad')).toBe(true)
    expect(visible(card.querySelector('.c-chip'))).toBe('1 missing')
    expect(visible(card.querySelector('.fp-words'))).toBe("merges can't see CI, so they fail")
    const link = Array.from(card.querySelectorAll('a')).find((a) => visible(a) === 'Review on GitHub')!
    expect(link.getAttribute('href')).toBe(REVIEW)
    expect(link.getAttribute('target')).toBe('_blank')
    // A gap the org itself can accept: no "the App does not ask" note.
    expect(card.querySelector('.fp-note')).toBeNull()
  })

  it('folds and unfolds on Details / Hide', async () => {
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    expect(visible(card.querySelector('.fp-body'))).toContain('GitHub grants everything SwarmCloud needs here.')
  })

  it('never draws Add to App for a member, even if a URL were served', async () => {
    routes(() => answer({ appMissing: [{ ...STATUSES, granted: null }], settingsUrl: SETTINGS }), false)
    await mountSection()
    await waitFor(() => expect(cardNamed('swarmcloud-saga')?.querySelector('.fp-body')).not.toBeNull(), WAIT)
    await new Promise((r) => setTimeout(r, 20))
    const app = cardNamed('swarmcloud-saga')!
    expect(visible(app.querySelector('.fp-words'))).toBe("merges can't see CI, so they fail")
    expect(screen.queryByText('Add to App')).toBeNull()
    expect(document.querySelector(`a[href="${SETTINGS}"]`)).toBeNull()
    expect(visible(app)).toContain('A platform admin adds these to the App')
  })

  it('draws Add to App for an admin, to the App settings page', async () => {
    routes(() => answer({ appMissing: [{ ...STATUSES, granted: null }], settingsUrl: SETTINGS }), true)
    await mountSection()
    const link = await screen.findByText('Add to App', undefined, WAIT)
    expect(link.closest('a')?.getAttribute('href')).toBe(SETTINGS)
    expect(link.closest('a')?.getAttribute('target')).toBe('_blank')
  })

  it('Re-check reads refresh=true, and a granted answer collapses the card green', async () => {
    let granted = false
    const calls = routes(() => answer({ gap: !granted }))
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')?.classList.contains('is-bad')).toBe(true), WAIT)
    granted = true
    fireEvent.click(screen.getByRole('button', { name: /Re-check/ }))
    await waitFor(() => expect(cardNamed('example-org')?.classList.contains('is-ok')).toBe(true), WAIT)
    expect(cardNamed('example-org')!.querySelector('.fp-body')).toBeNull()
    expect(calls.map((c) => c.url)).toContain('/v1/forge/permissions?refresh=true')
  })

  it('re-checks by itself when the person comes back to the tab after a fix button', async () => {
    let granted = false
    const calls = routes(() => answer({ gap: !granted }))
    await mountSection()
    const fresh = () => calls.filter((c) => c.url === '/v1/forge/permissions?refresh=true').length
    await waitFor(() => expect(cardNamed('example-org')?.classList.contains('is-bad')).toBe(true), WAIT)
    // Focus with no fix used: nothing is re-read.
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await new Promise((r) => setTimeout(r, 20))
    expect(fresh()).toBe(0)
    fireEvent.click(screen.getByText('Review on GitHub'))
    granted = true
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await waitFor(() => expect(cardNamed('example-org')?.classList.contains('is-ok')).toBe(true), WAIT)
    expect(fresh()).toBe(1)
  })

  it("shows an org's error on its own card, open, while the other cards still draw", async () => {
    const broken = org({ org: 'broken-org', installation_id: 77, granted: null, missing: null, pending: null, pending_update: null, error: 'installation read: GitHub answered HTTP 502' })
    routes(() => answer({ orgs: [org(), broken] }))
    await mountSection()
    await waitFor(() => expect(cardNamed('broken-org')).toBeDefined(), WAIT)
    const card = cardNamed('broken-org')!
    expect(card.classList.contains('is-unknown')).toBe(true)
    expect(visible(card.querySelector('.c-chip'))).toBe('not read')
    expect(visible(card.querySelector('.fp-error'))).toContain('installation read: GitHub answered HTTP 502')
    expect(cardNamed('example-org')?.classList.contains('is-ok')).toBe(true)
  })

  it('says loading while the first read runs, and that no org is connected when there is none', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => (release = r))
    const body = answer({ orgs: [] })
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/v1/tenants/me') return new Response(JSON.stringify(me(false)), { status: 200, headers: { 'content-type': 'application/json' } })
      await gate
      return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
    }) as unknown as typeof fetch
    await mountSection()
    expect(document.querySelector('[aria-busy="true"], .c-sk')).not.toBeNull()
    release()
    await screen.findByText('No connected org', undefined, WAIT)
    expect(cardNamed('swarmcloud-saga')).toBeDefined()
  })

  it('shows a failed read as a failure, not as an empty or healthy section', async () => {
    routes(() => ({ status: 500, body: { code: 'INTERNAL', message: 'boom' } }))
    await mountSection()
    await waitFor(() => expect(visible(document.querySelector('.fp-section'))).toMatch(/Could not|failed|Retry/i), WAIT)
    expect(orgCard()).toBeNull()
  })
})

describe('Work › Repositories: a status dot per repository', () => {
  async function mountList() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    return render(<RepositoriesScreen view={null} go={vi.fn()} />)
  }

  function listRoutes(perms: Answer) {
    const repos = [repo(), repo({ repo_id: TOKEN_REPO, repo: 'example-infra' })]
    perms.repositories.push({
      repo_id: TOKEN_REPO,
      repo: 'example-org/example-infra',
      credential: 'token',
      installation_id: null as unknown as number,
      capabilities: { ...ALL_TRUE, actions: null as unknown as boolean },
      blocked_by: [],
      error: null,
    })
    return serve((m, url) => {
      if (m !== 'GET') return null
      if (url === '/v1/repositories') return { status: 200, body: { repositories: repos } }
      if (url === '/v1/forge/permissions') return { status: 200, body: perms }
      return null
    })
  }

  const dot = (name: string) =>
    Array.from(document.querySelectorAll<HTMLElement>('.ur-card')).find((c) => visible(c.querySelector('h2')) === name)?.querySelector<HTMLElement>('.fp-dot')

  it('draws a red dot whose tooltip names the first blocked capability and its cause', async () => {
    listRoutes(answer({ gap: true }))
    await mountList()
    await waitFor(() => expect(dot('example-org/example-api')).toBeTruthy(), WAIT)
    const d = dot('example-org/example-api')!
    expect(d.getAttribute('data-status')).toBe('bad')
    expect(d.getAttribute('title')).toBe("Merge is blocked: merges can't see CI, so they fail")
    expect(d.getAttribute('aria-label')).toBe("GitHub permissions: Merge is blocked: merges can't see CI, so they fail")
  })

  it('draws the ok mark when everything is granted, and the ring where something was not measured', async () => {
    listRoutes(answer())
    await mountList()
    await waitFor(() => expect(dot('example-org/example-api')).toBeTruthy(), WAIT)
    expect(dot('example-org/example-api')!.getAttribute('data-status')).toBe('ok')
    const infra = dot('example-org/example-infra')!
    expect(infra.getAttribute('data-status')).toBe('unknown')
    expect(infra.getAttribute('title')).toBe('Nothing refused; not measurable here: Actions')
  })
})

describe("the repository page shows its own org's card", () => {
  it('draws the org card for an App-backed repository', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    serve((m, url) => {
      if (m !== 'GET') return null
      if (url === `/v1/repositories/${ORG_REPO}`) return { status: 200, body: { repository: repo(), index_runs: [], used_by: [] } }
      if (url === '/v1/forge/permissions') return { status: 200, body: answer({ gap: true }) }
      return null
    })
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={`repo=${ORG_REPO}`} go={vi.fn()} />)
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    expect(card.closest('.fp-repo')).not.toBeNull()
    expect(visible(card.querySelector('.fp-words'))).toBe("merges can't see CI, so they fail")
    expect(card.querySelector(`a[href="${REVIEW}"]`)).not.toBeNull()
  })
})
