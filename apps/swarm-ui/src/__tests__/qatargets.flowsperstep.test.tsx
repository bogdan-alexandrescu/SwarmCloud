// WORK › REPOSITORIES › QA TARGETS: A RUN'S FLOWS PER STEP (lane QA-FLOWS-UI
// over QA-FLOWS-CAP, part of #2136; BGD, 2026-10-26).
//
// WHAT EACH CASE HOLDS:
//   * beside Run, a target with flows draws a "Flows per step" number field
//     prefilled from the workspace's `qa_flows_per_step` (`GET
//     /v1/qa-settings`), and names that default beside it;
//   * Run sends `flows_per_step` only when the field was changed from it;
//   * a value outside 1-30 (or not a whole number) cannot be sent: Run is
//     disabled, the bound is said, and nothing is POSTed;
//   * a target with no flows draws no such field.
//
// The bodies are routes/qatargets.py's and routes/qa_settings.py's shapes.
// Nothing here is a credential.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const FLOWS = 'qat_' + 'f1'.repeat(8)
const PLAIN = 'qat_' + 'a2'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)

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

function target(id: string, name: string, over: Json = {}): Json {
  return {
    id,
    tenant_id: 'eng',
    revision: 1,
    name,
    base_url: `https://${name}.example.test/`,
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
    screens: [{ id: 'home', path: '/' }],
    ...over,
  }
}

const SETTINGS = (value: number): Json => ({
  tenant_id: 'eng',
  qa_flows_per_step: value,
  defaults: { qa_flows_per_step: 15 },
  bounds: { qa_flows_per_step: { min: 1, max: 30 } },
  set_at: null,
  set_by: null,
  can_edit: false,
})

function live(workspaceDefault = 12) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me('member') }
    if (m === 'GET' && path === '/v1/qa-settings') return { status: 200, body: SETTINGS(workspaceDefault) }
    if (m === 'GET' && path === '/v1/qa-targets') {
      return {
        status: 200,
        body: {
          qa_targets: [
            target(FLOWS, 'shop', { flows: [{ id: 'checkout', goal: 'Put one item in the basket and open checkout' }] }),
            target(PLAIN, 'blog'),
          ],
        },
      }
    }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    if (m === 'POST' && path.endsWith(':run')) return { status: 201, body: { workflow_id: 'wf_' + 'd4'.repeat(8), qa_target: {}, file: 'issues', browser_steps: [] } }
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

function runButton(root: ParentNode): HTMLButtonElement {
  const b = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((x) => visible(x) === 'Run')
  if (b === undefined) throw new Error('no Run button')
  return b
}

const field = (root: ParentNode) => root.querySelector<HTMLInputElement>('input[data-run-flows-per-step]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('QA targets: a run’s flows per step', () => {
  it('is prefilled from the workspace default, names it, and Run sends nothing for it unchanged', async () => {
    const calls = live(12)
    await mountPage()
    // MUTATION: draw no "Flows per step" field beside Run and this is null.
    await waitFor(() => expect(field(card(FLOWS))?.value).toBe('12'), WAIT)
    const input = field(card(FLOWS))!
    expect(input.min).toBe('1')
    expect(input.max).toBe('30')
    // MUTATION: drop the default's name beside the field.
    expect(visible(card(FLOWS))).toContain('workspace default: 12')
    fireEvent.click(runButton(card(FLOWS)))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    // MUTATION: always send the field's value and the run carries flows_per_step: 12.
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ file: 'issues' })
  })

  it('Run sends flows_per_step once it is changed', async () => {
    const calls = live(12)
    await mountPage()
    await waitFor(() => expect(field(card(FLOWS))?.value).toBe('12'), WAIT)
    fireEvent.change(field(card(FLOWS))!, { target: { value: '20' } })
    fireEvent.click(runButton(card(FLOWS)))
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(1), WAIT)
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.url).toContain(`/v1/qa-targets/${FLOWS}:run`)
    // MUTATION: drop `flows_per_step` from `runQaTarget`'s body, or send it as a string.
    expect(post.body).toEqual({ file: 'issues', flows_per_step: 20 })
  })

  it('a value outside 1-30 cannot be sent', async () => {
    const calls = live(12)
    await mountPage()
    await waitFor(() => expect(field(card(FLOWS))?.value).toBe('12'), WAIT)
    for (const bad of ['31', '0', '2.5', '']) {
      fireEvent.change(field(card(FLOWS))!, { target: { value: bad } })
      // MUTATION: leave Run enabled on an out-of-range value and the click posts it.
      expect(runButton(card(FLOWS)).disabled, `Run with ${JSON.stringify(bad)}`).toBe(true)
      expect(visible(card(FLOWS))).toContain('1-30')
      fireEvent.click(runButton(card(FLOWS)))
    }
    await new Promise((r) => setTimeout(r, 50))
    expect(calls.filter((c) => c.method === 'POST').length).toBe(0)
    fireEvent.change(field(card(FLOWS))!, { target: { value: '30' } })
    expect(runButton(card(FLOWS)).disabled).toBe(false)
  })

  it('a target with no flows draws no such field', async () => {
    live(12)
    await mountPage()
    await waitFor(() => expect(field(card(FLOWS))).not.toBeNull(), WAIT)
    // MUTATION: draw the field on every card and this finds one.
    expect(field(card(PLAIN))).toBeNull()
    expect(visible(card(PLAIN))).not.toContain('Flows per step')
  })
})
