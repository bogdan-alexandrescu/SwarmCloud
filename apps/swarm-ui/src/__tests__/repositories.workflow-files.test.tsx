// CONFIG › REPOSITORIES: whether an agent's push that changes workflow files is
// made for this repository (#2029, WF-APP-UI; BGD's decision 2026-10-22:
// self-serve, per-repo opt-in, off by default, chosen when the repo is added).
//
// WHERE THE SWITCH IS (BGD's answer to VQ-043, 2026-10-26: "Show state only on
// the card"). The card says the state in one read-only line; the switch, what
// it means and "Workflows: not granted" are on the repository's own page.
//
// WHAT EACH CASE HOLDS:
//   * each card shows `workflow_files` as served, with no control for anyone
//     and no not-granted line; what it means is said once on the list page;
//   * on the repository's Settings tab the admin's control PATCHes
//     `{workflow_files: true}` alone, then, read back on, `{workflow_files:
//     false}` alone; a member sees the state and no control;
//   * the register form sends no `workflow_files` unless the admin ticks it,
//     and a member is not offered the box;
//   * "Workflows: not granted" is drawn on the page only where the switch is on
//     AND the forge read measured the `workflow_files` capability false, with
//     the installation's link only when the API serves one.
//
// The bodies are routes/repositories.py's and forgeperms.py's shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const OFF_ID = 'repo_5555555555555555'
const ON_ID = 'repo_6666666666666666'
const GRANTED_ID = 'repo_7777777777777777'
const REVIEW = 'https://github.com/organizations/example-org/settings/installations/42'

type Json = Record<string, unknown>

/** `GET /v1/tenants/me` for the selected workspace `eng`, with the caller's role. */
function me(role: string): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

/** One forge-permissions row; `workflows` is the measured `workflow_files` capability, absent when not checked. */
function permRow(repoId: string, name: string, workflows: boolean | undefined): Json {
  return {
    repo_id: repoId,
    repo: `example-org/${name}`,
    credential: 'app',
    installation_id: 42,
    capabilities: { push: true, pull_requests: true, merge: true, ci: true, issues: true, actions: true, ...(workflows === undefined ? {} : { workflow_files: workflows }) },
    blocked_by: workflows === false ? [{ permission: 'workflows', access: 'write', granted: null, needed_for: 'workflow-file pushes fall back to workflow-lines.patch (Workflows: not granted)', capabilities: ['workflow_files'] }] : [],
    error: null,
  }
}

function perms(rows: Json[], reviewUrl: string | null = REVIEW): Json {
  return {
    tenant_id: 'eng',
    checked_at: new Date().toISOString(),
    required: [],
    app: { configured: true, slug: 'swarmcloud-example', granted: {}, requested: {}, missing: [], settings_url: null, error: null },
    orgs: [{ org: 'example-org', account_type: 'Organization', installation_id: 42, granted: { contents: 'write' }, missing: [], pending: [], review_url: reviewUrl, error: null }],
    repositories: rows,
    cached: false,
  }
}

/**
 * The list, with the switch held server-side so a PATCH is read back. OFF_ID
 * is off, ON_ID on and not granted, GRANTED_ID on and granted; OFF_ID's
 * capability is measured false too, which must not draw the line on an off switch.
 */
function liveList(role: string, opts: { reviewUrl?: string | null } = {}) {
  const on = new Map<string, boolean>([
    [OFF_ID, false],
    [ON_ID, true],
    [GRANTED_ID, true],
  ])
  const calls = serve((m, url, body) => {
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
    if (m === 'GET' && path === '/v1/repositories') {
      return {
        status: 200,
        body: {
          repositories: [
            repo({ repo_id: OFF_ID, repo: 'example-api', workflow_files: on.get(OFF_ID) }),
            repo({ repo_id: ON_ID, repo: 'example-web', workflow_files: on.get(ON_ID) }),
            repo({ repo_id: GRANTED_ID, repo: 'example-docs', workflow_files: on.get(GRANTED_ID) }),
          ],
        },
      }
    }
    if (m === 'GET' && path === '/v1/forge/permissions') {
      return {
        status: 200,
        body: perms(
          [permRow(OFF_ID, 'example-api', false), permRow(ON_ID, 'example-web', false), permRow(GRANTED_ID, 'example-docs', true)],
          opts.reviewUrl === undefined ? REVIEW : opts.reviewUrl,
        ),
      }
    }
    if (m === 'PATCH' && path !== undefined && path.startsWith('/v1/repositories/')) {
      const id = path.split('/').pop()!
      const sent = (body as Json | null)?.workflow_files
      if (typeof sent === 'boolean') on.set(id, sent)
      return { status: 200, body: { repository: { repo_id: id, workflow_files: on.get(id) }, tenant_id: 'eng' } }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountList() {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={null} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.ur-workflow-files').length).toBe(3), WAIT)
}

function cardOf(name: string): HTMLElement {
  const card = Array.from(document.querySelectorAll<HTMLElement>('.ur-card')).find((c) => visible(c.querySelector('h2, h3')) === name || visible(c).includes(name))
  if (card === undefined) throw new Error(`no card for ${name}`)
  return card
}

function line(name: string): HTMLElement {
  const el = cardOf(name).querySelector<HTMLElement>('.ur-workflow-files')
  if (el === null) throw new Error(`no workflow-files line on ${name}`)
  return el
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

const WHY =
  "An agent's workflow on a pushed branch runs in this repository with its secrets before any merge hold. Merges of workflow changes always wait for an owner."

describe('Repositories: the card says workflow files on or off, and has no switch', () => {
  it.each(['admin', 'member'])('a %s sees each served value read-only, and what it means once on the page', async (role) => {
    const calls = liveList(role)
    await mountList()
    const off = line('example-org/example-api')
    const on = line('example-org/example-web')
    // MUTATION: draw a constant instead of the served value and one of these fails.
    expect(off.getAttribute('data-workflow-files')).toBe('false')
    expect(visible(off)).toBe('Workflow files off')
    expect(on.getAttribute('data-workflow-files')).toBe('true')
    expect(visible(on)).toBe('Workflow files on')
    // WHAT IT MEANS IS SAID ONCE ON THE PAGE, NOT ON EVERY CARD (visual QA VQ-043): the same three lines
    // under each card's switch doubled the cards' height against S3a.
    // MUTATION: put the plain-words line back under each card's switch -> fails.
    for (const el of document.querySelectorAll('.ur-card')) expect(visible(el)).not.toContain(WHY)
    // MUTATION: drop the page's one note -> fails.
    const notes = [...document.querySelectorAll<HTMLElement>('p.ur-hint')].filter((p) => visible(p).includes(WHY))
    expect(notes).toHaveLength(1)
    expect(notes[0]!.closest('.ur-card')).toBeNull()
    // The role and the GitHub permissions have been read before the absences are.
    await waitFor(() => expect(calls.some((c) => c.url.split('?')[0] === '/v1/tenants/me')).toBe(true), WAIT)
    await waitFor(() => expect(calls.some((c) => c.url.split('?')[0] === '/v1/forge/permissions')).toBe(true), WAIT)
    // MUTATION: put Turn on / Turn off back on the card (BGD's VQ-043 pick moved it to the page) -> fails.
    expect(off.querySelector('button')).toBeNull()
    expect(on.querySelector('button')).toBeNull()
    expect(cardOf('example-org/example-api').querySelector('[data-workflow-files-set]')).toBeNull()
    expect(cardOf('example-org/example-api').querySelector('[data-role-gate="admin"]')).toBeNull()
    // MUTATION: keep the not-granted line on the card (it moved to the page with the switch) -> fails.
    expect(cardOf('example-org/example-web').querySelector('.ur-workflows-grant')).toBeNull()
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })

  it("the card's one state line carries both switches' words", async () => {
    liveList('member')
    await mountList()
    const states = cardOf('example-org/example-web').querySelector<HTMLElement>('[data-row="switches"]')
    // MUTATION: draw the two states on two rows again -> this is null, or holds one of them.
    expect(states).not.toBeNull()
    expect(states!.querySelector('.ur-file-issues')).not.toBeNull()
    expect(states!.querySelector('.ur-workflow-files')).not.toBeNull()
    expect(states!.querySelectorAll(':scope > span')).toHaveLength(2)
  })
})

describe('Register repository: workflow files, off by default', () => {
  const READABLE = {
    secret_name: 'swarm-tenant-eng-git',
    total: 1,
    repositories: [{ owner: 'example-org', repo: 'example-api', default_branch: 'main', private: true, pushed_at: null, registered: false }],
  }
  const RUNTIMES = { runtimes: { 'claude-code': {} } }

  async function register(role: string, tick: boolean) {
    const calls = serve((m, url) => {
      const path = url.split('?')[0]
      if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
      if (m === 'GET' && path === '/v1/repositories/readable') return { status: 200, body: READABLE }
      if (m === 'GET' && path === '/v1/runtimes') return { status: 200, body: RUNTIMES }
      if (m === 'POST' && path === '/v1/repositories') return { status: 201, body: { repo_id: OFF_ID, owner: 'example-org', repo: 'example-api' } }
      if (m === 'POST' && path === `/v1/repositories/${OFF_ID}/index:run`) return { status: 202, body: {} }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const go = vi.fn()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=register" go={go} />)
    await waitFor(() => expect(document.querySelectorAll('.ur-pick').length).toBe(1), WAIT)
    fireEvent.click(document.querySelector('.ur-pick')!)
    fireEvent.click(screen.getByRole('button', { name: 'Next: schedule' }))
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'claude-code' })).toBeTruthy(), WAIT)
    if (role === 'admin') {
      await waitFor(() => expect(screen.queryByRole('checkbox', { name: 'Agents may push changes to workflow files' })).not.toBeNull(), WAIT)
      const box = screen.getByRole('checkbox', { name: 'Agents may push changes to workflow files' }) as HTMLInputElement
      // MUTATION: default the box to ticked and this fails.
      expect(box.checked).toBe(false)
      if (tick) fireEvent.click(box)
    } else {
      // MUTATION: offer the box below admin and this finds it.
      await waitFor(() => expect(document.querySelector('.ur-register-workflow-files [data-role-gate="admin"]')).not.toBeNull(), WAIT)
      expect(screen.queryByRole('checkbox', { name: 'Agents may push changes to workflow files' })).toBeNull()
    }
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    fireEvent.click(screen.getByRole('button', { name: 'Register example-org/example-api' }))
    await waitFor(() => expect(go).toHaveBeenCalled(), WAIT)
    return calls.filter((c) => c.method === 'POST' && c.url === '/v1/repositories')[0]!.body as Json
  }

  it('an admin who leaves the box sends no workflow_files', async () => {
    const body = await register('admin', false)
    // MUTATION: always send the field (`workflow_files: ticked`) and this has the key.
    expect('workflow_files' in body).toBe(false)
  })

  it('an admin who ticks the box sends workflow_files: true', async () => {
    const body = await register('admin', true)
    // MUTATION: never send the field, or send it false, and this fails.
    expect(body.workflow_files).toBe(true)
  })

  it('a member is not offered the box and sends no workflow_files', async () => {
    const body = await register('member', false)
    expect('workflow_files' in body).toBe(false)
  })
})

describe("the repository's page: workflow files", () => {
  const ID = ON_ID

  /** One repository's Settings tab, the switch held server-side so a PATCH is read back. */
  async function mountDetail(role: string, fields: Json, capability: boolean | undefined, opts: { card?: string; reviewUrl?: string | null } = {}) {
    let held = fields
    const calls = serve((m, url, body) => {
      const path = url.split('?')[0]
      if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
      if (m === 'GET' && path === '/v1/forge/permissions') {
        return { status: 200, body: perms([permRow(ID, 'example-web', capability)], opts.reviewUrl === undefined ? REVIEW : opts.reviewUrl) }
      }
      if (m === 'GET' && url === `/v1/repositories/${ID}`) {
        return {
          status: 200,
          body: {
            repository: {
              ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6') }),
              graph: { depth: 3, min_confidence: 0.2 },
              selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
              ...held,
            },
            index_runs: [],
            used_by: [],
          },
        }
      }
      if (m === 'PATCH' && path === `/v1/repositories/${ID}`) {
        held = { ...held, ...(body as Json) }
        return { status: 200, body: { repository: { repo_id: ID, ...held }, tenant_id: 'eng' } }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
    // MUTATION: leave the card out of the Settings tab and this never appears.
    await waitFor(() => expect(document.querySelector(opts.card ?? '.ur-detail-workflow-files')).not.toBeNull(), WAIT)
    return { calls, el: () => document.querySelector<HTMLElement>('.ur-detail-workflow-files') }
  }

  it('an admin toggles it: exactly {workflow_files: true}, then, read back on, {workflow_files: false}', async () => {
    const { calls, el } = await mountDetail('admin', { workflow_files: false }, true)
    expect(el()!.getAttribute('data-workflow-files')).toBe('false')
    expect(visible(el())).toContain('Workflow files off')
    expect(visible(el())).toContain(WHY)
    // MUTATION: draw no control on the page (the switch stayed on the card) and this times out.
    await waitFor(() => expect(el()!.querySelector('button')).not.toBeNull(), WAIT)
    expect(visible(el()!.querySelector('button'))).toBe('Turn on')
    fireEvent.click(el()!.querySelector('button')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const first = calls.filter((c) => c.method === 'PATCH')[0]!
    // MUTATION: PATCH another repository's id or the list route and this fails.
    expect(first.url).toBe(`/v1/repositories/${ID}`)
    // MUTATION: send the current value, or any other field beside it, and this fails.
    expect(first.body).toEqual({ workflow_files: true })
    // MUTATION: drop the read after a successful change and the page never reads on.
    await waitFor(() => expect(el()!.getAttribute('data-workflow-files')).toBe('true'), WAIT)
    await waitFor(() => expect(visible(el()!.querySelector('button'))).toBe('Turn off'), WAIT)
    fireEvent.click(el()!.querySelector('button')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(2), WAIT)
    // MUTATION: hard-code `true` in the writer and this fails.
    expect(calls.filter((c) => c.method === 'PATCH')[1]!.body).toEqual({ workflow_files: false })
  })

  it.each(['member', 'viewer'])('a %s sees the state and the read-only line, and sends nothing', async (role) => {
    const { calls, el } = await mountDetail(role, { workflow_files: false }, true)
    // MUTATION: drop the admin gate and this gets a button.
    await waitFor(() => expect(el()!.querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    expect(el()!.querySelector('button')).toBeNull()
    // MUTATION: hide the state below admin and this loses it.
    expect(visible(el())).toContain('Workflow files off')
    expect(visible(el()!.querySelector('[data-role-gate="admin"]'))).toBe('set by a workspace admin')
    // MUTATION: send the write on mount or for any role and this is not empty.
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })

  it('on and not granted: the state, what it means and the not-granted line with its served link', async () => {
    const { el } = await mountDetail('member', { workflow_files: true }, false)
    expect(el()!.getAttribute('data-workflow-files')).toBe('true')
    expect(visible(el())).toContain('Workflow files on')
    // MUTATION: never draw the line on the detail page and this times out.
    await waitFor(() => expect(el()!.querySelector('.ur-workflows-grant')).not.toBeNull(), WAIT)
    const notGranted = el()!.querySelector<HTMLElement>('.ur-workflows-grant')!
    // MUTATION: say it from the switch alone, or from another capability, and the wording fails.
    expect(visible(notGranted)).toContain('Workflows: not granted by the GitHub App installation')
    // MUTATION: drop the served link, or build one, and this fails.
    expect(notGranted.querySelector('a')?.getAttribute('href')).toBe(REVIEW)
  })

  it('without a served link the not-granted line has no link, and none is built', async () => {
    const { el } = await mountDetail('admin', { workflow_files: true }, false, { reviewUrl: null })
    await waitFor(() => expect(el()!.querySelector('.ur-workflows-grant')).not.toBeNull(), WAIT)
    const notGranted = el()!.querySelector<HTMLElement>('.ur-workflows-grant')!
    expect(visible(notGranted)).toContain('Workflows: not granted by the GitHub App installation')
    // MUTATION: build a github.com URL from the org's name and this finds one.
    expect(notGranted.querySelector('a')).toBeNull()
  })

  it('on and granted: no not-granted line', async () => {
    const { el } = await mountDetail('admin', { workflow_files: true }, true)
    await waitFor(() => expect(document.querySelector('.fp-repo .fp-cards')).not.toBeNull(), WAIT)
    // MUTATION: draw the line where the switch is on and the capability is granted and this is not null.
    expect(el()!.querySelector('.ur-workflows-grant')).toBeNull()
  })

  it('off: no not-granted line even where the capability reads false', async () => {
    const { el } = await mountDetail('admin', { workflow_files: false }, false)
    expect(visible(el())).toContain('Workflow files off')
    // The page's own GitHub read (the permissions section) has answered, so a line drawn from it would be here.
    await waitFor(() => expect(document.querySelector('.fp-repo .fp-cards')).not.toBeNull(), WAIT)
    // MUTATION: draw the line from the capability alone and this is not null.
    expect(el()!.querySelector('.ur-workflows-grant')).toBeNull()
  })

  it('an unserved switch draws no card, as an API older than WF-APP-TOKEN has none', async () => {
    // The issue-filing card is on the same tab: once it is drawn, the tab is.
    const { el } = await mountDetail('admin', {}, undefined, { card: '.ur-detail-file-issues' })
    // MUTATION: draw the card for a null switch (as off) and this finds it.
    expect(el()).toBeNull()
  })
})
