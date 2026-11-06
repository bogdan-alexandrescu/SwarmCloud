// WORK › REPOSITORIES › QA TARGETS: A WRITING FLOW'S `writes` (part of #2688; qa-environments.md §6).
//
//   * a target's row names each flow marked `mutating` with a `writes` sentence, the sentence behind an (i);
//   * a read-only flow, and a mutating one with no `writes` (it runs read-only), is not named.
//
// The bodies are routes/qatargets.py's shapes. Nothing here is a credential.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { repo, serve } from './repofixture'

const WAIT = { timeout: 4000 }
const SHOP = 'qat_' + 'c3'.repeat(8)
const REPO_ID = 'repo_' + '0'.repeat(16)
const WRITES = 'One order for the cheapest plan, in the test catalogue.'

type Json = Record<string, unknown>

function me(): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: 'member', source: 'override' },
  }
}

const SHOP_TARGET: Json = {
  id: SHOP,
  tenant_id: 'eng',
  revision: 1,
  name: 'shop',
  base_url: 'https://shop.example.test/',
  host: 'shop.example.test',
  host_state: 'approved',
  approved_by: 'ops@example.com',
  approved_at: '2026-10-22T09:00:00+00:00',
  approved_origins: [],
  repo_id: REPO_ID,
  repository: 'https://github.com/example-org/example-api',
  sign_in: { kind: 'none' },
  credential: { set: false, set_at: null, set_by: null, version: null },
  authorized_by: "the app's owner",
  environment: 'non_production',
  screens: [{ id: 'home', path: '/' }],
  flows: [
    { id: 'checkout', goal: 'Buy the cheapest plan', start_path: '/', mutating: true, writes: WRITES },
    { id: 'look', goal: 'Find pricing', start_path: '/' },
    { id: 'legacy', goal: 'Sign up', start_path: '/', mutating: true },
  ],
}

function live() {
  serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: me() }
    if (m === 'GET' && path === '/v1/qa-targets') return { status: 200, body: { qa_targets: [SHOP_TARGET] } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo({ repo_id: REPO_ID })] } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('QA targets: a writing flow’s writes', () => {
  it('names the writing flow on its row, its writes sentence behind an (i)', async () => {
    live()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view="page=qa-targets" go={vi.fn()} />)
    await waitFor(() => expect(document.querySelector(`.qa-target[data-target-id="${SHOP}"]`)).not.toBeNull(), WAIT)
    const card = document.querySelector<HTMLElement>(`.qa-target[data-target-id="${SHOP}"]`)!
    // MUTATION: drop `writingFlows` from the target's row and this is empty.
    const named = Array.from(card.querySelectorAll<HTMLElement>('[data-writing-flow]')).map((el) => el.dataset.writingFlow)
    expect(named).toEqual(['checkout'])
    const about = card.querySelector<HTMLButtonElement>('button[aria-label="About what checkout writes"]')
    expect(about).not.toBeNull()
    expect(document.getElementById(about!.getAttribute('aria-describedby') ?? '')?.textContent).toBe(WRITES)
    // Behind the (i): the sentence is the tooltip's, not a line of its own on the row.
    expect(document.getElementById(about!.getAttribute('aria-describedby') ?? '')?.closest('.c-tip-host')).not.toBeNull()
  })
})
