// WORK › REPOSITORIES › QA TARGETS: a crawl target's discovery, started from
// the console (lane QA-DISCOVER, part of #1933; docs/design/visual-qa-any-app.md
// §4.1). BGD, 2026-10-23: "build discovery, so a crawl target is started
// entirely from the console".
//
// WHAT EACH CASE HOLDS, over routes/qatargets.py's shapes:
//   * a target with `discover` and nothing discovered shows Run discovery to a
//     member, and Run is enabled: its capture steps crawl (QAENV-RUN, §3.1);
//   * Run discovery POSTs the target's own `:discover` with nothing a caller
//     picks, and the card then shows Discovering… linking the workflow;
//   * a target with discovered screens shows "N screens discovered, <time
//     ago>", Run discovery again, and Run enabled;
//   * a failed discovery's `discovery_error` is said on the card;
//   * a viewer is shown the role gate and the button sends nothing;
//   * a target that lists its screens shows no discovery control.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const CRAWL = 'qat_' + '2c'.repeat(8)
const LISTED = 'qat_' + 'a1'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)
const WORKFLOW = 'wf_' + 'd4'.repeat(8)

type Json = Record<string, unknown>

function me(role: string): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

/** A target as `to_api` serves it, discovery fields included. */
function target(id: string, name: string, over: Json = {}): Json {
  return {
    id,
    tenant_id: 'eng',
    revision: 1,
    name,
    base_url: `https://${name}.example.test`,
    host: `${name}.example.test`,
    host_state: 'approved',
    approved_by: 'ops@example.com',
    approved_at: '2026-10-22T09:00:00+00:00',
    approved_origins: [],
    repo_id: REPO_ID,
    repository: 'https://github.com/example-org/example-api',
    sign_in: { kind: 'none' },
    credential: { set: false, set_at: null, set_by: null, version: null },
    authorized_by: "the app's owner",
    discovered: null,
    discovered_at: null,
    discovered_by: null,
    discovered_workflow: null,
    discovery_error: null,
    discovering: null,
    ...over,
  }
}

function crawl(over: Json = {}): Json {
  return target(CRAWL, 'crawl', { discover: { max_pages: 60, max_depth: 2 }, ...over })
}

function live(role: string, first: Json, after: Json | null = null) {
  let current = first
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me(role) }
    if (m === 'GET' && path === '/v1/qa-targets') {
      return { status: 200, body: { qa_targets: [current, target(LISTED, 'shop', { screens: [{ id: 'home', path: '/' }] })] } }
    }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'POST' && path.endsWith(':discover')) {
      if (after !== null) current = after
      return { status: 201, body: { workflow_id: WORKFLOW, qa_target: { id: CRAWL, revision: 1 } } }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountPage() {
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
  await waitFor(() => expect(document.querySelectorAll('.qa-target').length).toBe(2), WAIT)
}

function card(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.qa-target[data-target-id="${id}"]`)
  if (el === null) throw new Error(`no card for ${id}`)
  return el
}

function button(root: ParentNode, label: string): HTMLButtonElement | null {
  return Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) => visible(b) === label) ?? null
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('QA targets: discovery from the console (QA-DISCOVER, §4.1)', () => {
  it('Run discovery, then Discovering… with the workflow, then N screens discovered with Run enabled', async () => {
    const running = crawl({ discovering: { workflow_id: WORKFLOW, by: 'vera@example.com', at: '2026-10-23T09:00:00+00:00' } })
    const calls = live('member', crawl(), running)
    await mountPage()
    const before = card(CRAWL)
    // MUTATION: draw no discovery control and a crawl target can never run from the console.
    const start = button(before, 'Run discovery')
    expect(start).not.toBeNull()
    expect(start!.disabled).toBe(false)
    // MUTATION: keep Run disabled with nothing discovered: since QAENV-RUN the API runs a target that
    // discovers from its base alone, its capture steps crawling (qa-environments.md §3.1).
    expect(button(before, 'Run')!.disabled).toBe(false)
    expect(before.querySelector('[data-cannot-run]')).toBeNull()

    fireEvent.click(start!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'POST')!
    // MUTATION: post to `:run`, or another target's id.
    expect(sent.url).toBe(`/v1/qa-targets/${CRAWL}:discover`)
    // MUTATION: send anything a caller picks (a URL, a page count, a profile).
    expect(sent.body === null || JSON.stringify(sent.body) === '{}').toBe(true)
    await waitFor(() => expect(visible(card(CRAWL))).toContain('Discovering…'), WAIT)
    const link = Array.from(card(CRAWL).querySelectorAll('a, button')).find((el) => visible(el) === WORKFLOW)
    // MUTATION: say Discovering without the workflow and nobody can follow it.
    expect(link).not.toBeUndefined()
    // MUTATION: leave the button enabled while one runs and a second press is a 409.
    expect(button(card(CRAWL), 'Run discovery')?.disabled ?? true).toBe(true)
  })

  it('a target with discovered screens says how many and when, and Run is enabled', async () => {
    const at = new Date(Date.now() - 5 * 60 * 1000).toISOString()
    const discovered = [
      { id: 'home', path: '/' },
      { id: 'pricing', path: '/pricing' },
      { id: 'docs', path: '/docs' },
    ]
    live('member', crawl({ discovered, discovered_at: at, discovered_by: 'vera@example.com', discovered_workflow: WORKFLOW }))
    await mountPage()
    const c = card(CRAWL)
    // MUTATION: ignore `discovered` and this says nothing was found.
    expect(visible(c)).toContain('3 screens discovered')
    expect(visible(c)).toMatch(/3 screens discovered, \S+/)
    // MUTATION: keep Run disabled once screens exist and the target still cannot run.
    expect(button(c, 'Run')!.disabled).toBe(false)
    expect(c.querySelector('[data-cannot-run]')).toBeNull()
    // MUTATION: keep the first label and a person cannot tell a rerun from a first run.
    expect(button(c, 'Run discovery again')).not.toBeNull()
    expect(button(c, 'Run discovery')).toBeNull()
  })

  it('a failed discovery says its error sentence', async () => {
    const error = 'the discovery step ended FAILED before its screens could be read; workflow wf_1 says why'
    live('member', crawl({ discovery_error: error }))
    await mountPage()
    // MUTATION: drop `discovery_error` and a failed discovery looks like one never run.
    expect(visible(card(CRAWL))).toContain(error)
    expect(button(card(CRAWL), 'Run discovery')).not.toBeNull()
  })

  it('a viewer sees the role gate, and Run discovery sends nothing', async () => {
    const calls = live('viewer', crawl())
    await mountPage()
    const start = button(card(CRAWL), 'Run discovery')
    // MUTATION: gate discovery on nothing and a viewer's press is a certain 403.
    expect(start === null || start.disabled).toBe(true)
    if (start !== null) fireEvent.click(start)
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
  })

  it('a target that lists its screens shows no discovery control', async () => {
    live('member', crawl())
    await mountPage()
    // MUTATION: draw the control on every target and a listed target offers a discovery the API refuses.
    expect(button(card(LISTED), 'Run discovery')).toBeNull()
    expect(button(card(LISTED), 'Run discovery again')).toBeNull()
  })
})
