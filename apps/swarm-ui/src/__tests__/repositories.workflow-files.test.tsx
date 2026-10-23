// WORK › REPOSITORIES: whether an agent's push that changes workflow files is
// made for this repository (#2029, WF-APP-UI; BGD's decision 2026-10-22:
// self-serve, per-repo opt-in, off by default, chosen when the repo is added).
//
// WHAT EACH CASE HOLDS:
//   * each card shows `workflow_files` as served, with a control for the
//     workspace admin only; a member sees the state and no control;
//   * the admin's control PATCHes `{workflow_files: true}` alone, then, read
//     back on, `{workflow_files: false}` alone;
//   * the register form sends no `workflow_files` unless the admin ticks it,
//     and a member is not offered the box;
//   * "Workflows: not granted" is drawn only where the switch is on AND the
//     forge read measured the `workflow_files` capability false, with the
//     installation's link only when the API serves one;
//   * the repository's Settings tab shows the same state, read-only.
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

describe('Repositories: workflow files, per repository', () => {
  it('an admin sees each served value, what it means, and a control', async () => {
    liveList('admin')
    await mountList()
    const off = line('example-org/example-api')
    const on = line('example-org/example-web')
    // MUTATION: draw a constant instead of the served value and one of these fails.
    expect(off.getAttribute('data-workflow-files')).toBe('false')
    expect(visible(off)).toContain('Workflow files off')
    expect(on.getAttribute('data-workflow-files')).toBe('true')
    expect(visible(on)).toContain('Workflow files on')
    // MUTATION: drop the plain-words line under the switch and this fails.
    expect(visible(off)).toContain(
      "An agent's workflow on a pushed branch runs in this repository with its secrets before any merge hold. Merges of workflow changes always wait for an owner.",
    )
    // MUTATION: draw no control for anyone (drop the `admin ? <Button>` branch) and these are null.
    await waitFor(() => expect(off.querySelector('button')).not.toBeNull(), WAIT)
    expect(visible(off.querySelector('button'))).toBe('Turn on')
    expect(visible(on.querySelector('button'))).toBe('Turn off')
  })

  it.each(['member', 'viewer'])('a %s sees the state and the read-only line, and sends nothing', async (role) => {
    const calls = liveList(role)
    await mountList()
    const off = line('example-org/example-api')
    // MUTATION: drop the admin gate (pass `admin={true}`) and this gets a button.
    await waitFor(() => expect(off.querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    expect(off.querySelector('button')).toBeNull()
    expect(line('example-org/example-web').querySelector('button')).toBeNull()
    // MUTATION: hide the state below admin and this loses it.
    expect(visible(off)).toContain('Workflow files off')
    expect(visible(off.querySelector('[data-role-gate="admin"]'))).toBe('set by a workspace admin')
    // MUTATION: send the write on mount or for any role and this is not empty.
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })

  it('toggling sends exactly {workflow_files: true}, then, read back on, {workflow_files: false}', async () => {
    const calls = liveList('admin')
    await mountList()
    await waitFor(() => expect(line('example-org/example-api').querySelector('button')).not.toBeNull(), WAIT)
    fireEvent.click(line('example-org/example-api').querySelector('button')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const first = calls.filter((c) => c.method === 'PATCH')[0]!
    // MUTATION: PATCH another repository's id or the list route and this fails.
    expect(first.url).toBe(`/v1/repositories/${OFF_ID}`)
    // MUTATION: send the current value, or any other field beside it, and this fails.
    expect(first.body).toEqual({ workflow_files: true })
    // MUTATION: drop the `reload()` after a successful change and the card never reads on.
    await waitFor(() => expect(line('example-org/example-api').getAttribute('data-workflow-files')).toBe('true'), WAIT)
    await waitFor(() => expect(visible(line('example-org/example-api').querySelector('button'))).toBe('Turn off'), WAIT)
    fireEvent.click(line('example-org/example-api').querySelector('button')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(2), WAIT)
    // MUTATION: hard-code `true` in the writer and this fails.
    expect(calls.filter((c) => c.method === 'PATCH')[1]!.body).toEqual({ workflow_files: false })
  })

  it('"Workflows: not granted" appears only where the switch is on and the requirement is missing', async () => {
    liveList('admin')
    await mountList()
    await waitFor(() => expect(cardOf('example-org/example-web').querySelector('.ur-workflows-grant')).not.toBeNull(), WAIT)
    const notGranted = cardOf('example-org/example-web').querySelector<HTMLElement>('.ur-workflows-grant')!
    // MUTATION: say it from the switch alone, or from another capability, and the wording or the cards below fail.
    expect(visible(notGranted)).toContain('Workflows: not granted by the GitHub App installation')
    // MUTATION: drop the served link, or build one, and this fails.
    expect(notGranted.querySelector('a')?.getAttribute('href')).toBe(REVIEW)
    // MUTATION: draw the line where the switch is off (ignore `on`) and this is not null.
    expect(cardOf('example-org/example-api').querySelector('.ur-workflows-grant')).toBeNull()
    // MUTATION: draw the line where the switch is on and the capability is granted and this is not null.
    expect(cardOf('example-org/example-docs').querySelector('.ur-workflows-grant')).toBeNull()
  })

  it('without a served link the not-granted line has no link, and none is built', async () => {
    liveList('admin', { reviewUrl: null })
    await mountList()
    await waitFor(() => expect(cardOf('example-org/example-web').querySelector('.ur-workflows-grant')).not.toBeNull(), WAIT)
    const notGranted = cardOf('example-org/example-web').querySelector<HTMLElement>('.ur-workflows-grant')!
    expect(visible(notGranted)).toContain('Workflows: not granted by the GitHub App installation')
    // MUTATION: build a github.com URL from the org's name and this finds one.
    expect(notGranted.querySelector('a')).toBeNull()
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

describe('Repository settings: workflow files, read-only', () => {
  const ID = ON_ID

  async function mountDetail(fields: Json, capability: boolean | undefined, card = '.ur-detail-workflow-files') {
    const calls = serve((m, url) => {
      const path = url.split('?')[0]
      if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me('admin') }
      if (m === 'GET' && path === '/v1/forge/permissions') return { status: 200, body: perms([permRow(ID, 'example-web', capability)]) }
      if (m === 'GET' && url === `/v1/repositories/${ID}`) {
        return {
          status: 200,
          body: {
            repository: {
              ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6') }),
              graph: { depth: 3, min_confidence: 0.2 },
              selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
              ...fields,
            },
            index_runs: [],
            used_by: [],
          },
        }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
    // MUTATION: leave the card out of the Settings tab and this never appears.
    await waitFor(() => expect(document.querySelector(card)).not.toBeNull(), WAIT)
    return { calls, el: document.querySelector<HTMLElement>('.ur-detail-workflow-files') }
  }

  it('on and not granted: the state, what it means and the not-granted line, with no control', async () => {
    const { calls, el: found } = await mountDetail({ workflow_files: true }, false)
    const el = found!
    expect(el.getAttribute('data-workflow-files')).toBe('true')
    expect(visible(el)).toContain('Workflow files on')
    expect(visible(el)).toContain('Merges of workflow changes always wait for an owner.')
    // MUTATION: never draw the line on the detail page and this times out.
    await waitFor(() => expect(el.querySelector('.ur-workflows-grant')).not.toBeNull(), WAIT)
    expect(visible(el.querySelector('.ur-workflows-grant'))).toContain('Workflows: not granted by the GitHub App installation')
    // MUTATION: put a toggle on the detail page and it has a button.
    expect(el.querySelector('button')).toBeNull()
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('off: no not-granted line even where the capability reads false', async () => {
    const el = (await mountDetail({ workflow_files: false }, false)).el!
    expect(visible(el)).toContain('Workflow files off')
    // The page's own GitHub read (the permissions section) has answered, so a line drawn from it would be here.
    await waitFor(() => expect(document.querySelector('.fp-repo .fp-cards')).not.toBeNull(), WAIT)
    // MUTATION: draw the line from the capability alone and this is not null.
    expect(el.querySelector('.ur-workflows-grant')).toBeNull()
  })

  it('an unserved switch draws no card, as an API older than WF-APP-TOKEN has none', async () => {
    // The issue-filing card is on the same tab: once it is drawn, the tab is.
    const { el } = await mountDetail({}, undefined, '.ur-detail-file-issues')
    // MUTATION: draw the card for a null switch (as off) and this finds it.
    expect(el).toBeNull()
  })
})
