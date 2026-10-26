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
//   * FP-DETAIL (owner report 2026-10-12: "all granted" said nothing): an
//     all-granted card's collapsed header carries "6 of 6 needed · Push, ..."
//     and a "6/6 granted" chip; expanded it is a table of what SwarmCloud
//     needs (plain words + GitHub's name, needed, granted, a mark, what for),
//     the grants it does not use folded under "Also granted", what it covers,
//     the installation it came through with "Manage on GitHub", and when it
//     was checked; a User installation is an "account", not an "org"; a
//     missing card still opens with its fix link and shows the same table;
//   * loading and the empty state (no connected org);
//   * WF-APP-PERMS (#2029): Workflows (write), `workflow_files_required`, is
//     a row of every needed-permissions table, the App card's included. Granted,
//     it is green and counted ("7 of 7 needed"); not granted, it is a counted
//     gap only when a repository that installation covers has Workflow files
//     on, and otherwise a neutral row that leaves the card green; it is never
//     under "Also granted".
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const REVIEW = 'https://github.com/organizations/example-org/settings/installations/42/permissions/update'
const SETTINGS = 'https://github.com/organizations/swarm-owner/settings/apps/swarmcloud-example/permissions'
const ORG_REPO = 'repo_0a1b2c3d4e5f6071'
const TOKEN_REPO = 'repo_2222222222222222'

const STATUSES = { permission: 'statuses', access: 'read', needed_for: "merges can't see CI, so they fail", capabilities: ['merge', 'ci'] }
const CONTENTS = { permission: 'contents', access: 'write', needed_for: "runs can't push their branch, and merges can't land", capabilities: ['push', 'merge'] }
const PULLS = { permission: 'pull_requests', access: 'write', needed_for: "runs can't open or update pull requests, or merge them", capabilities: ['pull_request', 'merge'] }
const CHECKS = { permission: 'checks', access: 'read', needed_for: "merges and CI fixes can't see check runs", capabilities: ['merge', 'ci'] }
const ISSUES = { permission: 'issues', access: 'write', needed_for: "issue runs and the observer can't read or comment on issues", capabilities: ['issues'] }
const ACTIONS = { permission: 'actions', access: 'read', needed_for: "ci-flake-hunter and release-health can't read workflow runs", capabilities: ['actions'] }
const REQUIRED = [CONTENTS, PULLS, STATUSES, CHECKS, ISSUES, ACTIONS]
// forgeperms.py's WORKFLOW_FILES_REQUIREMENT, served beside `required` as `workflow_files_required` (#2029).
const WORKFLOWS = {
  permission: 'workflows',
  access: 'write',
  needed_for: 'workflow-file pushes fall back to workflow-lines.patch (Workflows: not granted)',
  capabilities: ['workflow_files'],
}
// What GitHub answered for a fully granted installation: the six and Workflows, plus Metadata, which SwarmCloud does not use.
const GRANTED = { contents: 'write', pull_requests: 'write', statuses: 'read', checks: 'read', issues: 'write', actions: 'read', metadata: 'read', workflows: 'write' }
const ALL_TRUE = { push: true, pull_request: true, merge: true, ci: true, issues: true, actions: true }

function org(over: Record<string, unknown> = {}) {
  return {
    org: 'example-org',
    account_type: 'Organization',
    installation_id: 42,
    granted: GRANTED,
    missing: [],
    pending_update: false,
    pending: [],
    review_url: REVIEW,
    error: null,
    ...over,
  }
}

function answer({
  gap = false,
  settingsUrl = null as string | null,
  orgs = null as unknown[] | null,
  appMissing = [] as unknown[],
  appGranted = GRANTED as Record<string, string>,
} = {}) {
  const theOrg = gap
    ? org({
        granted: Object.fromEntries(Object.entries(GRANTED).filter(([k]) => k !== 'statuses')),
        missing: [{ ...STATUSES, granted: null }],
        pending_update: true,
        pending: [{ permission: 'statuses', requested: 'read', granted: null }],
      })
    : org()
  return {
    tenant_id: 'eng',
    checked_at: new Date().toISOString(),
    required: REQUIRED,
    workflow_files_required: WORKFLOWS,
    app: { configured: true, slug: 'swarmcloud-example', granted: appGranted, requested: appGranted, missing: appMissing, settings_url: settingsUrl, error: null },
    orgs: orgs ?? [theOrg],
    repositories: [
      {
        repo_id: ORG_REPO,
        repo: 'example-org/example-api',
        credential: 'app',
        installation_id: 42,
        capabilities: (gap ? { ...ALL_TRUE, merge: false, ci: false } : ALL_TRUE) as Record<string, boolean>,
        blocked_by: (gap ? [{ ...STATUSES, granted: null }] : []) as Record<string, unknown>[],
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

describe('Workspace setup › GitHub: one card for the App and one per org', () => {
  it('draws a fully granted org collapsed, with a count chip and what it enables in the header', async () => {
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    expect(card.classList.contains('is-ok')).toBe(true)
    expect(visible(card.querySelector('.c-chip'))).toBe('7/7 granted')
    expect(visible(card.querySelector('.fp-summary'))).toBe('7 of 7 needed · Push, Pull requests, Merge, CI, Issues, Actions, Workflow files')
    expect(visible(card.querySelector('h3 small'))).toBe('org')
    expect(card.querySelector('.fp-body')).toBeNull()
    expect(card.querySelector('button[aria-expanded]')?.getAttribute('aria-expanded')).toBe('false')
    expect(cardNamed('swarmcloud-example')).toBeDefined()
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
    expect(card.querySelector('.fp-body')).not.toBeNull()
    expect(card.querySelector('button[aria-expanded]')?.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    expect(card.querySelector('.fp-body')).toBeNull()
  })

  it('expands an all-granted card to the table of what SwarmCloud needs, what GitHub granted and what each is for', async () => {
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    const heads = Array.from(card.querySelectorAll('.fp-grants thead th')).map(visible)
    expect(heads).toEqual(['GitHub permission', 'Needs', 'Granted', '', 'For'])
    const rows = Array.from(card.querySelectorAll<HTMLElement>('.fp-grants tbody tr'))
    expect(rows.map((r) => r.getAttribute('data-permission'))).toEqual(['contents', 'pull_requests', 'statuses', 'checks', 'issues', 'actions', 'workflows'])
    const statuses = rows[2]!
    const cells = Array.from(statuses.querySelectorAll('td')).map(visible)
    expect(cells[0]).toBe('Commit statuses statuses')
    expect(cells[1]).toBe('read')
    expect(cells[2]).toBe('read')
    expect(statuses.getAttribute('data-ok')).toBe('true')
    expect(cells[4]).toContain("merges can't see CI, so they fail")
    expect(cells[4]).toContain('Merge, CI')
    expect(rows.every((r) => r.getAttribute('data-ok') === 'true')).toBe(true)
  })

  it("draws each row's capability words at the table's own size, never in a <small> (CONFIG-COPY render review)", async () => {
    // The table is already --t-micro; a <small> inside a cell shrank "Push,
    // Merge" one step further, to 10px measured on the rendered Workspace
    // setup page (2026-10-25, both themes, 1440 and 390), under the 11.5px floor.
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    const caps = Array.from(card.querySelectorAll<HTMLElement>('.fp-grants .fp-caps'))
    expect(caps.length).toBeGreaterThan(0)
    expect(caps.map((c) => c.tagName)).toEqual(caps.map(() => 'SPAN'))
    expect(card.querySelectorAll('.fp-grants small')).toHaveLength(0)
  })

  it('folds the grants SwarmCloud does not use under "Also granted"', async () => {
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    const extra = card.querySelector<HTMLElement>('.fp-extra')!
    expect(extra.tagName).toBe('DETAILS')
    expect(extra.hasAttribute('open')).toBe(false)
    expect(visible(extra.querySelector('summary'))).toBe('Also granted (not used by SwarmCloud): 1')
    expect(visible(extra)).toContain('metadata: read')
    // A grant SwarmCloud needs is in the table, not among the extras.
    expect(visible(extra)).not.toContain('contents')
  })

  it('says which repositories the installation covers, the installation it came through, and when it was checked', async () => {
    const body = answer()
    const first = body.repositories[0]!
    body.repositories.push({ ...first, repo_id: TOKEN_REPO, repo: 'example-org/example-web' })
    body.repositories.push({ ...first, repo_id: 'repo_3333333333333333', repo: 'other-org/elsewhere', installation_id: 99 })
    routes(() => body)
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    expect(visible(card.querySelector('.fp-covers'))).toBe('Covers: 2 registered repositories (example-org/example-api, example-org/example-web)')
    expect(visible(card.querySelector('.fp-via'))).toContain('Via: SwarmCloud GitHub App installation 42')
    const manage = Array.from(card.querySelectorAll('a')).find((a) => visible(a) === 'Manage on GitHub')!
    expect(manage.getAttribute('href')).toBe('https://github.com/organizations/example-org/settings/installations/42')
    expect(manage.getAttribute('target')).toBe('_blank')
    expect(visible(card.querySelector('.fp-when'))).toBe('Checked just now')
    // And beside the section's Re-check.
    expect(visible(document.querySelector('.fp-head .fp-when'))).toBe('Checked just now')
  })

  it('names a User installation an account, and an Organization an org', async () => {
    const user = org({ org: 'example-person', account_type: 'User', installation_id: 43, review_url: 'https://github.com/settings/installations/43/permissions/update' })
    routes(() => answer({ orgs: [org(), user] }))
    await mountSection()
    await waitFor(() => expect(cardNamed('example-person')).toBeDefined(), WAIT)
    expect(visible(cardNamed('example-person')!.querySelector('h3 small'))).toBe('account')
    expect(visible(cardNamed('example-org')!.querySelector('h3 small'))).toBe('org')
    const card = cardNamed('example-person')!
    fireEvent.click(card.querySelector('button[aria-expanded]')!)
    const manage = Array.from(card.querySelectorAll('a')).find((a) => visible(a) === 'Manage on GitHub')!
    expect(manage.getAttribute('href')).toBe('https://github.com/settings/installations/43')
  })

  it('shows the same table on a missing card, open, with the gap marked and its fix link', async () => {
    routes(() => answer({ gap: true }))
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')?.querySelector('.fp-body')).not.toBeNull(), WAIT)
    const card = cardNamed('example-org')!
    expect(visible(card.querySelector('.fp-summary'))).toBe('6 of 7 needed · Push, Pull requests, Issues, Actions, Workflow files · blocked: Merge, CI')
    const row = card.querySelector<HTMLElement>('.fp-grants tr[data-permission="statuses"]')!
    expect(row.getAttribute('data-ok')).toBe('false')
    expect(visible(row.querySelectorAll('td')[2] ?? null)).toBe('none')
    expect(card.querySelectorAll('.fp-grants tr[data-ok="false"]').length).toBe(1)
    expect(Array.from(card.querySelectorAll('a')).find((a) => visible(a) === 'Review on GitHub')?.getAttribute('href')).toBe(REVIEW)
  })

  it('never draws Add to App for a member, even if a URL were served', async () => {
    routes(() => answer({ appMissing: [{ ...STATUSES, granted: null }], settingsUrl: SETTINGS }), false)
    await mountSection()
    await waitFor(() => expect(cardNamed('swarmcloud-example')?.querySelector('.fp-body')).not.toBeNull(), WAIT)
    await new Promise((r) => setTimeout(r, 20))
    const app = cardNamed('swarmcloud-example')!
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
    expect(cardNamed('swarmcloud-example')).toBeDefined()
  })

  it('shows a failed read as a failure, not as an empty or healthy section', async () => {
    routes(() => ({ status: 500, body: { code: 'INTERNAL', message: 'boom' } }))
    await mountSection()
    await waitFor(() => expect(visible(document.querySelector('.fp-section'))).toMatch(/Could not|failed|Retry/i), WAIT)
    expect(orgCard()).toBeNull()
  })
})

describe('Workflows (write): a needed row, a gap only where a covered repository has Workflow files on (#2029)', () => {
  const NO_WF = Object.fromEntries(Object.entries(GRANTED).filter(([k]) => k !== 'workflows'))
  const NEUTRAL = 'Not granted, needed only for repositories with Workflow files on'

  /** An org that has not accepted Workflows: the App asks for it, so it is pending, and `missing` (REQUIRED only) is empty. */
  function unaccepted() {
    return org({ granted: NO_WF, pending: [{ permission: 'workflows', requested: 'write', granted: null }] })
  }

  /** The covered repository with its switch on: forgeperms.py's row carries `workflow_files` and its gap. */
  function switchedOn(body: Answer, granted: boolean) {
    const row = body.repositories[0]!
    row.capabilities = { ...ALL_TRUE, workflow_files: granted }
    row.blocked_by = granted ? [] : [{ ...WORKFLOWS, granted: null }]
    return body
  }

  const open = (card: HTMLElement) => {
    if (card.querySelector('.fp-body') === null) fireEvent.click(card.querySelector('button[aria-expanded]')!)
  }
  const wfRow = (card: HTMLElement) => card.querySelector<HTMLElement>('.fp-grants tr[data-permission="workflows"]')

  it('a granted Workflows is a green row on the org card, counted in "7 of 7 needed"', async () => {
    routes(() => switchedOn(answer(), true))
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    // MUTATION: build the table from `required` alone and the header says "6 of 6 needed" without Workflow files.
    expect(visible(card.querySelector('.fp-summary'))).toBe('7 of 7 needed · Push, Pull requests, Merge, CI, Issues, Actions, Workflow files')
    // MUTATION: leave the Workflows row out of the chip's count and this reads "6/6 granted".
    expect(visible(card.querySelector('.c-chip'))).toBe('7/7 granted')
    expect(card.classList.contains('is-ok')).toBe(true)
    open(card)
    const row = wfRow(card)!
    // MUTATION: drop `workflow_files_required` from the rows and there is no Workflows row.
    expect(row).not.toBeNull()
    // MUTATION: mark the row by `missing` alone (REQUIRED only) without its own grant check and a granted row could read short.
    expect(row.getAttribute('data-ok')).toBe('true')
    const cells = Array.from(row.querySelectorAll('td')).map(visible)
    expect(cells[0]).toBe('Workflows workflows')
    expect(cells[1]).toBe('write')
    expect(cells[2]).toBe('write')
    // MUTATION: draw the neutral words on a granted row and the server's `needed_for` is gone.
    expect(cells[4]).toContain(WORKFLOWS.needed_for)
    // MUTATION: drop `workflow_files` from CAPABILITY_WORD and this reads "workflow_files".
    expect(cells[4]).toContain('Workflow files')
  })

  it('the App card lists Workflows as a needed row too, green when the App asks for it', async () => {
    routes(() => answer())
    await mountSection()
    await waitFor(() => expect(cardNamed('swarmcloud-example')).toBeDefined(), WAIT)
    const app = cardNamed('swarmcloud-example')!
    // MUTATION: give the App card no summary and this is empty.
    expect(visible(app.querySelector('.fp-summary'))).toBe('7 of 7 needed · Push, Pull requests, Merge, CI, Issues, Actions, Workflow files')
    open(app)
    // MUTATION: give the App card no needed-permissions table and there is no row.
    expect(wfRow(app)?.getAttribute('data-ok')).toBe('true')
  })

  it('an ungranted Workflows is a counted gap when a repository the installation covers has the switch on', async () => {
    routes(() => switchedOn(answer({ orgs: [unaccepted()] }), false))
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')?.querySelector('.fp-body')).not.toBeNull(), WAIT)
    const card = cardNamed('example-org')!
    // MUTATION: never count the Workflows row as a gap and the card stays green and folded.
    expect(card.classList.contains('is-bad')).toBe(true)
    // MUTATION: count the gap and its pending request both and this reads "1 missing · 1 pending".
    expect(visible(card.querySelector('.c-chip'))).toBe('1 missing')
    expect(visible(card.querySelector('.fp-summary'))).toBe('6 of 7 needed · Push, Pull requests, Merge, CI, Issues, Actions · blocked: Workflow files')
    // MUTATION: use the pending line's generic words and the server's `needed_for` is not the gap's words.
    expect(visible(card.querySelector('.fp-line .fp-words'))).toBe(WORKFLOWS.needed_for)
    expect(visible(card.querySelector('.fp-line .fp-jargon'))).toBe('workflows: needs write, has none')
    // The App asks for it, so the org can accept it: no "the App does not ask" note.
    expect(card.querySelector('.fp-note')).toBeNull()
    // MUTATION: draw the gap as neutral and the row is not short.
    expect(wfRow(card)?.getAttribute('data-ok')).toBe('false')
    expect(Array.from(card.querySelectorAll('a')).find((a) => visible(a) === 'Review on GitHub')?.getAttribute('href')).toBe(REVIEW)
  })

  it('the App card counts an unrequested Workflows as a gap, with Add to App, when any repository has the switch on', async () => {
    // The App does not ask for Workflows, so no org has a request pending for it.
    routes(() => switchedOn(answer({ orgs: [org({ granted: NO_WF })], appGranted: NO_WF, settingsUrl: SETTINGS }), false), true)
    await mountSection()
    await waitFor(() => expect(cardNamed('swarmcloud-example')?.querySelector('.fp-body')).not.toBeNull(), WAIT)
    const app = cardNamed('swarmcloud-example')!
    // MUTATION: leave the App card's lines at `app.missing` alone and it stays green.
    expect(app.classList.contains('is-bad')).toBe(true)
    expect(visible(app.querySelector('.c-chip'))).toBe('1 missing')
    expect(visible(app.querySelector('.fp-line .fp-words'))).toBe(WORKFLOWS.needed_for)
    expect(wfRow(app)?.getAttribute('data-ok')).toBe('false')
    const link = await screen.findByText('Add to App', undefined, WAIT)
    expect(link.closest('a')?.getAttribute('href')).toBe(SETTINGS)
    // The org has not been asked for it: accepting there cannot fix it, and its card says so.
    const card = cardNamed('example-org')!
    expect(visible(card.querySelector('.fp-note'))).toBe('The App itself does not ask for this yet: an admin adds it to the App first.')
  })

  it('the same row is neutral, and the cards stay green, when no covered repository has the switch on', async () => {
    const body = answer({ orgs: [unaccepted()] })
    // Another installation's repository with the switch on does not make this org's row a gap.
    body.repositories.push({ ...body.repositories[0]!, repo_id: 'repo_3333333333333333', repo: 'other-org/elsewhere', installation_id: 99, capabilities: { ...ALL_TRUE, workflow_files: false }, blocked_by: [{ ...WORKFLOWS, granted: null }] })
    routes(() => body)
    await mountSection()
    await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
    const card = cardNamed('example-org')!
    // MUTATION: count the Workflows row (or its pending request) whenever it is not granted and the card turns red.
    expect(card.classList.contains('is-ok')).toBe(true)
    // MUTATION: count the neutral row in the denominator and this reads "6/7 granted".
    expect(visible(card.querySelector('.c-chip'))).toBe('6/6 granted')
    expect(visible(card.querySelector('.fp-summary'))).toBe('6 of 6 needed · Push, Pull requests, Merge, CI, Issues, Actions')
    expect(card.querySelector('.fp-body')).toBeNull()
    open(card)
    const row = wfRow(card)!
    // MUTATION: leave a neutral row out of the table and the person cannot see Workflows is not granted.
    expect(row).not.toBeNull()
    // MUTATION: draw the neutral row as a gap and this reads "false".
    expect(row.getAttribute('data-ok')).toBe('neutral')
    expect(visible(row.querySelectorAll('td')[2] ?? null)).toBe('none')
    // MUTATION: keep the server's `needed_for` on a neutral row and these words are missing.
    expect(visible(row.querySelector('.fp-for'))).toContain(NEUTRAL)
    expect(card.querySelector('.fp-lines')).toBeNull()
    expect(Array.from(card.querySelectorAll('a')).find((a) => visible(a) === 'Review on GitHub')).toBeUndefined()
  })

  it('the App card is neutral on an unrequested Workflows when no repository has the switch on', async () => {
    routes(() => answer({ appGranted: NO_WF }))
    await mountSection()
    await waitFor(() => expect(cardNamed('swarmcloud-example')).toBeDefined(), WAIT)
    const app = cardNamed('swarmcloud-example')!
    // MUTATION: count the App's unrequested Workflows whenever it is not asked for and the App card turns red.
    expect(app.classList.contains('is-ok')).toBe(true)
    expect(visible(app.querySelector('.fp-summary'))).toBe('6 of 6 needed · Push, Pull requests, Merge, CI, Issues, Actions')
    open(app)
    expect(wfRow(app)?.getAttribute('data-ok')).toBe('neutral')
    expect(visible(wfRow(app)!.querySelector('.fp-for'))).toContain(NEUTRAL)
  })

  it('workflows never appears under "Also granted"', async () => {
    for (const body of [answer(), switchedOn(answer(), true)]) {
      routes(() => body)
      const { unmount } = await mountSection()
      await waitFor(() => expect(cardNamed('example-org')).toBeDefined(), WAIT)
      const card = cardNamed('example-org')!
      open(card)
      const extra = card.querySelector<HTMLElement>('.fp-extra')!
      // MUTATION: leave `workflows` out of the needed set and it is folded among the extras again.
      expect(visible(extra)).not.toContain('workflows')
      expect(visible(extra.querySelector('summary'))).toBe('Also granted (not used by SwarmCloud): 1')
      unmount()
    }
  })
})

describe('Config › Repositories: a status dot per repository', () => {
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

describe("TENANT-WORDING-G: a token-backed repository's card says workspace token (#2391)", () => {
  it('names the card and its hint for the workspace token, never the tenant token', async () => {
    // MUTATION: put back 'tenant token' in PermCard's kindWord or TokenCard's hint.
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const perms = answer()
    perms.repositories.push({
      repo_id: TOKEN_REPO,
      repo: 'example-org/example-infra',
      credential: 'token',
      installation_id: null as unknown as number,
      capabilities: { ...ALL_TRUE, merge: false, ci: false },
      blocked_by: [{ ...STATUSES, granted: null }],
      error: null,
    })
    serve((m, url) => {
      if (m !== 'GET') return null
      if (url === `/v1/repositories/${TOKEN_REPO}`)
        return { status: 200, body: { repository: repo({ repo_id: TOKEN_REPO, repo: 'example-infra' }), index_runs: [], used_by: [] } }
      if (url === '/v1/forge/permissions') return { status: 200, body: perms }
      return null
    })
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={`repo=${TOKEN_REPO}`} go={vi.fn()} />)
    await waitFor(() => expect(cardNamed('example-org/example-infra')).toBeDefined(), WAIT)
    const card = cardNamed('example-org/example-infra')!
    expect(visible(card.querySelector('h3 small'))).toBe('workspace token')
    expect(visible(card)).toContain('This repository is reached through the workspace token, not the App')
    expect(visible(card)).not.toMatch(/tenant/i)
  })
})

describe('visual QA VQA2-R5', () => {
  it('V231: Setup › GitHub says when it was checked once, in its head, not again in its lede', async () => {
    // MUTATION: call `checkedLine(doc)` (with the time) for the section's lede again.
    routes(() => ({ ...answer(), checked_at: null }) as unknown as Answer)
    await mountSection()
    await waitFor(() => expect(document.querySelector('.fp-checked')).not.toBeNull(), WAIT)
    const section = document.querySelector<HTMLElement>('.fp-section')!
    const head = section.querySelector<HTMLElement>('.fp-head')!
    expect(visible(head.querySelector('.fp-when'))).toBe('Checked at an unknown time')
    const lede = visible(section.querySelector('.fp-checked'))
    expect(lede).not.toMatch(/checked/i)
    expect(lede).toBe('GitHub does not let SwarmCloud grant permissions itself, so a fix is always a link.')
    // Everything the section prints outside the folded org details says the time once.
    const said = visible(section).match(/Checked at an unknown time/g) ?? []
    const folded = [...section.querySelectorAll('.fp-detail')].reduce((n, d) => n + (visible(d).match(/Checked at an unknown time/g) ?? []).length, 0)
    expect(said.length - folded).toBe(1)
  })
})
