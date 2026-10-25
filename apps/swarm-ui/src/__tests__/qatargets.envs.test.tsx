// WORK › REPOSITORIES › QA TARGETS: one target's environments on its card,
// and one Run over the ticked ones (lane QAENV-UI-FORM, part of #2136;
// docs/design/qa-environments.md §7 "The dispatch form", §5 Promote, §10;
// Config proposal E, S9a and S9c, BGD's pick 2026-10-24).
//
// WHAT EACH CASE HOLDS, over `to_api`'s shape for a target of several
// entries (the app's fields, and `environments[]` each with its own host,
// approval, sign-in, credential status and `new_since_last_run`):
//   * a leg that cannot run is drawn disabled with its reason while the
//     other stays ticked and runnable;
//   * Run's body is exactly `{environments, file, browser_tool?}`: names
//     only, never a URL, a sign-in or a credential (invariant 10), and the
//     answer's run group is linked;
//   * a target that discovers runs with nothing stored (QAENV-RUN, §3.1);
//   * Promote is drawn to a workspace admin only, and is a PATCH of `screens`;
//   * Set credential on one leg mints a link naming that environment, and
//     the form says which;
//   * a platform admin's Approve host on one leg names that environment.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const APP = 'qat_' + '46'.repeat(8)
const CRAWL = 'qat_' + '2c'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)
const GROUP = 'qrg_' + '5e'.repeat(8)
const WF = 'wf_' + 'a1'.repeat(10)

type Json = Record<string, unknown>

const UNSET = { set: false, set_at: null, set_by: null, version: null }
const SET = { set: true, set_at: '2026-10-24T22:00:00+00:00', set_by: 'bogdan@example.com', version: '3' }

function me(role: string, isAdmin = false): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: isAdmin, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

/** One entry as `_entry_api` serves it. */
function entry(name: string, over: Json = {}): Json {
  return {
    name,
    base_url: `https://${name}.app.example.test`,
    host: `${name}.app.example.test`,
    environment: name === 'production' ? 'production' : 'non_production',
    sign_in: { kind: 'own_login', login_path: '/login', start_clicks: [] },
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: [],
    approved_idp_token_path: null,
    credential: SET,
    challenge_bypass_value: UNSET,
    discovered: null,
    new_since_last_run: [],
    discovered_at: null,
    discovered_by: null,
    discovered_workflow: null,
    discovery_error: null,
    discovering: null,
    ...over,
  }
}

/** A target of several entries as `to_api` serves it: no per-leg key beside the list. */
function app(environments: Json[], over: Json = {}): Json {
  return {
    id: APP,
    tenant_id: 'eng',
    revision: 2,
    name: 'studio',
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    authorized_by: "the app's owner",
    screens: [
      { id: 'home', path: '/' },
      { id: 'settings', path: '/settings' },
    ],
    environments,
    ...over,
  }
}

const STAGING = entry('staging', {
  new_since_last_run: [{ id: 'beta-reports', path: '/beta/reports', first_seen: '2026-10-24T21:12:00+00:00' }],
})
const PRODUCTION_NO_CREDENTIAL = entry('production', { credential: UNSET })

function live(role: string, targets: Json[], run: { status: number; body: Json } | null = null, platformAdmin = false) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role, platformAdmin) }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: targets } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'POST' && path.endsWith(':run')) {
      return run ?? { status: 201, body: { group_id: GROUP, file: 'issues', legs: [{ environment: 'staging', state: 'submitted', workflow_id: WF, reason: null }] } }
    }
    if (m === 'POST' && path.endsWith('/credential-links')) {
      return { status: 201, body: { url: null, path: `/qa-targets/${APP}/credential?t=${'k'.repeat(43)}&environment=production`, expires_at: '2026-10-25T09:00:00+00:00' } }
    }
    if (m === 'POST' && path.endsWith(':approve-host')) return { status: 200, body: { qa_target: app([STAGING, entry('production')]) } }
    if (m === 'PATCH' && path === `/v1/qa-targets/${APP}`) return { status: 200, body: { qa_target: app([STAGING, PRODUCTION_NO_CREDENTIAL]) } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountPage(n = 1) {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.qa-target').length).toBe(n), WAIT)
}

function card(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.qa-target[data-target-id="${id}"]`)
  if (el === null) throw new Error(`no card for ${id}`)
  return el
}

function leg(root: ParentNode, name: string): HTMLElement {
  const el = root.querySelector<HTMLElement>(`[data-environment="${name}"]`)
  if (el === null) throw new Error(`no leg ${name}`)
  return el
}

function tick(root: ParentNode, name: string): HTMLInputElement {
  const el = leg(root, name).querySelector<HTMLInputElement>('input[type="checkbox"][data-run-leg]')
  if (el === null) throw new Error(`no checkbox for ${name}`)
  return el
}

function button(root: ParentNode, label: string | RegExp): HTMLButtonElement | null {
  return (
    Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) =>
      typeof label === 'string' ? visible(b) === label : label.test(visible(b)),
    ) ?? null
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('QA targets: a target with environments, one Run over the ticked ones (QAENV-UI-FORM, §7)', () => {
  it('draws a leg that cannot run disabled with its reason next to a runnable one', async () => {
    live('member', [app([STAGING, PRODUCTION_NO_CREDENTIAL])])
    await mountPage()
    const c = card(APP)
    // MUTATION: draw one Run for the whole target and no leg can be told from another.
    expect(c.querySelectorAll('[data-environment]').length).toBe(2)
    // MUTATION: disable every leg when one cannot run and staging cannot run either.
    expect(tick(c, 'staging').disabled).toBe(false)
    expect(tick(c, 'staging').checked).toBe(true)
    // MUTATION: leave production's box enabled and Run asks for a leg the API refuses.
    expect(tick(c, 'production').disabled).toBe(true)
    expect(tick(c, 'production').checked).toBe(false)
    // MUTATION: drop the per-leg reason and nobody can tell why production is off.
    expect(visible(leg(c, 'production').querySelector('[data-cannot-run]'))).toMatch(/Cannot run: .*credential/)
    expect(leg(c, 'staging').querySelector('[data-cannot-run]')).toBeNull()
    // MUTATION: read the host and credential from the app's fields (absent here) and every leg is "unknown".
    expect(visible(leg(c, 'staging'))).toContain('staging.app.example.test')
    expect(visible(leg(c, 'staging'))).toContain('approved')
    expect(visible(leg(c, 'production'))).toContain('not set')
    // MUTATION: keep Run disabled because one leg cannot run.
    const run = button(c, /^Run 1 environment$/)
    expect(run).not.toBeNull()
    expect(run!.disabled).toBe(false)
    // MUTATION: drop the run options beside the legs.
    expect(c.querySelector('[data-report-only]')).not.toBeNull()
    expect(c.querySelector('[data-run-browser]')).not.toBeNull()
  })

  it('sends exactly {environments, file} on Run, and {environments, file, browser_tool} with a tool, then links the run group', async () => {
    const calls = live('member', [app([STAGING, entry('production')])])
    await mountPage()
    const c = card(APP)
    expect(button(c, 'Run 2 environments')).not.toBeNull()
    fireEvent.click(tick(c, 'production'))
    await waitFor(() => expect(button(card(APP), 'Run 1 environment')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(APP), 'Run 1 environment')!)
    await waitFor(() => expect(calls.filter((c2) => c2.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c2) => c2.method === 'POST')!
    expect(sent.url).toBe(`/v1/qa-targets/${APP}:run`)
    // MUTATION: send a URL, a sign-in, a credential, an unticked leg or any other key and this is not equal.
    expect(sent.body).toEqual({ environments: ['staging'], file: 'issues' })
    // MUTATION: link the first leg's workflow instead of the run group and the group's page is unreachable.
    await waitFor(() => expect(card(APP).querySelector(`a[href="/qa-runs/${GROUP}"]`)).not.toBeNull(), WAIT)

    fireEvent.click(card(APP).querySelector<HTMLInputElement>('[data-report-only]')!)
    fireEvent.change(card(APP).querySelector<HTMLSelectElement>('[data-run-browser]')!, { target: { value: 'jev' } })
    fireEvent.click(tick(card(APP), 'production'))
    await waitFor(() => expect(button(card(APP), 'Run 2 environments')).not.toBeNull(), WAIT)
    fireEvent.click(button(card(APP), 'Run 2 environments')!)
    await waitFor(() => expect(calls.filter((c2) => c2.method === 'POST').length).toBe(2), WAIT)
    // MUTATION: drop `file` or `browser_tool`, or reorder the legs, and this is not equal.
    expect(calls.filter((c2) => c2.method === 'POST')[1]!.body).toEqual({ environments: ['staging', 'production'], file: 'none', browser_tool: 'jev' })
  })

  it('a leg the group could not start says so with the refusal (207)', async () => {
    live('member', [app([STAGING, entry('production')])], {
      status: 207,
      body: {
        group_id: GROUP,
        file: 'issues',
        legs: [
          { environment: 'staging', state: 'submitted', workflow_id: WF, reason: null },
          { environment: 'production', state: 'not_started', workflow_id: null, reason: { code: 'quota_exceeded', message: 'the workspace is at its run limit' } },
        ],
      },
    })
    await mountPage()
    fireEvent.click(button(card(APP), 'Run 2 environments')!)
    // MUTATION: read a 207 as a plain success and the leg that never started goes unsaid.
    await waitFor(() => expect(visible(card(APP))).toContain('production did not start: the workspace is at its run limit'), WAIT)
    expect(card(APP).querySelector(`a[href="/qa-runs/${GROUP}"]`)).not.toBeNull()
  })

  it('a target that discovers runs with no screens stored (QAENV-RUN: its capture steps crawl)', async () => {
    live('member', [app([entry('production', { sign_in: { kind: 'none' }, credential: UNSET })], { id: CRAWL, screens: null, discover: { max_pages: 60 } })])
    await mountPage()
    const c = card(CRAWL)
    // MUTATION: keep refusing a discovering target until a separate discovery stored screens (qa_target_no_screens).
    expect(tick(c, 'production').disabled).toBe(false)
    expect(c.querySelector('[data-cannot-run]')).toBeNull()
    expect(button(c, 'Run 1 environment')!.disabled).toBe(false)
  })

  it('Promote is absent for a member, and for an admin is a PATCH of screens with the new route', async () => {
    live('member', [app([STAGING, PRODUCTION_NO_CREDENTIAL])])
    await mountPage()
    // The new route is shown to anyone who reads the card.
    expect(visible(leg(card(APP), 'staging'))).toContain('/beta/reports')
    // MUTATION: draw Promote to every role and a member's press is a certain 403.
    expect(button(card(APP), /^Promote/)).toBeNull()
    cleanup()

    const calls = live('admin', [app([STAGING, PRODUCTION_NO_CREDENTIAL])])
    await mountPage()
    const promote = button(card(APP), /^Promote/)
    // MUTATION: hide Promote from an admin too and a found route is never stored.
    expect(promote).not.toBeNull()
    fireEvent.click(promote!)
    await waitFor(() => expect(calls.filter((c2) => c2.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c2) => c2.method === 'PATCH')!
    expect(sent.url).toBe(`/v1/qa-targets/${APP}`)
    // MUTATION: replace `screens` with the new route alone, or patch anything else, and the stored list is lost.
    expect(sent.body).toEqual({
      screens: [
        { id: 'home', path: '/' },
        { id: 'settings', path: '/settings' },
        { id: 'beta-reports', path: '/beta/reports' },
      ],
    })
  })

  it("an admin's Set credential on one leg mints a link naming that environment, and the form names it", async () => {
    const calls = live('admin', [app([STAGING, PRODUCTION_NO_CREDENTIAL])])
    await mountPage()
    const set = button(leg(card(APP), 'production'), 'Set credential')
    expect(set).not.toBeNull()
    fireEvent.click(set!)
    await waitFor(() => expect(calls.filter((c2) => c2.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c2) => c2.method === 'POST')!
    expect(sent.url).toBe(`/v1/qa-targets/${APP}/credential-links`)
    // MUTATION: mint without the environment and the API refuses a target of several entries.
    expect(sent.body).toEqual({ environment: 'production' })
    // MUTATION: open the form with `environment: null` and its title cannot say which leg it sets.
    await waitFor(() => expect(document.querySelector('.qa-cred')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.qa-cred')!.closest('.ur-form'))).toContain('Test credential for studio · production')
    // The token is held in memory, never drawn.
    expect(document.body.textContent ?? '').not.toContain('k'.repeat(43))
  })

  it('leads the page with the leg that still needs something', async () => {
    live('admin', [app([STAGING, PRODUCTION_NO_CREDENTIAL])])
    await mountPage()
    const lead = document.querySelector<HTMLElement>('[data-qa-lead]')
    // MUTATION: drop the lead and a leg that cannot run is found only by reading every card.
    expect(lead).not.toBeNull()
    expect(visible(lead)).toContain('studio production has no test credential')
    expect(button(lead!, 'Set production’s credential')).not.toBeNull()
  })

  it("a platform admin's Approve host on a pending leg names that environment, and the approved leg is drawn", async () => {
    const calls = live('member', [app([STAGING, entry('production', { host_state: 'pending', approved_by: null, approved_at: null })])], null, true)
    await mountPage()
    // MUTATION: draw Approve host from the app's fields (absent on a target of entries) and no leg offers it.
    await waitFor(() => expect(button(leg(card(APP), 'production'), 'Approve host')).not.toBeNull(), WAIT)
    expect(button(leg(card(APP), 'staging'), 'Approve host')).toBeNull()
    fireEvent.click(button(leg(card(APP), 'production'), 'Approve host')!)
    const form = await waitFor(() => {
      const f = card(APP).querySelector<HTMLFormElement>('form.qa-approve')
      if (f === null) throw new Error('no approve form')
      return f
    }, WAIT)
    fireEvent.change(form.querySelector<HTMLInputElement>('input[name="confirm_host"]')!, { target: { value: 'production.app.example.test' } })
    fireEvent.submit(form)
    await waitFor(() => expect(calls.filter((c2) => c2.method === 'POST').length).toBe(1), WAIT)
    // MUTATION: drop `environment` and the API refuses an approval on a target of several entries.
    expect(calls.find((c2) => c2.method === 'POST')!.body).toEqual({ host: 'production.app.example.test', environment: 'production' })
    await waitFor(() => expect(tick(card(APP), 'production').disabled).toBe(false), WAIT)
  })
})
