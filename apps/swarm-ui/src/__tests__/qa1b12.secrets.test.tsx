// CONSOLE VISUAL QA PASS 1B, LANE QA1B-12 (config-22; part of #2596).
//
// `Last used` read `not served` in every row of the workspace's secrets: the
// API's word, not the reader's. An API that does not serve the field gets a
// dash whose name is the reason.

import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { serve } from './repofixture'

const AT = '2026-10-26T09:30:00+00:00'

function record(name: string, lastUsed?: string | null): Record<string, unknown> {
  return {
    name,
    secret_id: `swarm-tenant-eng-wsec-${name.length}a1b2`,
    provider: 'other',
    kind: 'api_key',
    scope: 'workspace',
    managed: 'store',
    current_version: '1',
    versions: [{ version: '1', added_at: AT, added_by: 'alice@example.com', state: 'enabled', disable_after: null }],
    created_at: AT,
    created_by: 'alice@example.com',
    rotated_at: null,
    rotated_by: null,
    use_in_workspaces: [],
    grants: [],
    ...(lastUsed === undefined ? {} : { last_used_at: lastUsed }),
  }
}

function api(secrets: Record<string, unknown>[]) {
  serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets/consumers')) {
      return { status: 200, body: { consumers: [], kinds: ['api_key'], providers: ['other'] } }
    }
    if (m === 'GET' && url.startsWith('/v1/workspace/secrets')) {
      return {
        status: 200,
        body: {
          tenant_id: 'eng',
          can_manage: true,
          secrets,
          platform_credentials: { runner_provider_keys: [], pool_accounts: { readable: true, accounts: [] } },
          note: 'stored write-only',
        },
      }
    }
    if (m === 'GET' && url.startsWith('/v1/secrets/overview')) {
      return { status: 200, body: { tenant_id: 'eng', scope: 'workspace', can_manage: true, rows: [] } }
    }
    if (m === 'GET' && url.startsWith('/v1/repositories')) return { status: 200, body: { repositories: [] } }
    return null
  })
}

async function lastUsedCells(): Promise<HTMLElement[]> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SecretsScreen } = await import('../Secrets')
  render(<SecretsScreen view={null} />)
  const table = await screen.findByRole('table', { name: /workspace secrets/i })
  return [...within(table).getAllByRole('row')].slice(1).map((r) => r.querySelector<HTMLElement>('td[data-label="Last used"]')!)
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('config-22: an unserved Last used is a dash with its reason', () => {
  it('draws a named dash, never `not served`', async () => {
    api([record('alpha'), record('beta')])
    const cells = await lastUsedCells()
    expect(cells).toHaveLength(2)
    for (const cell of cells) {
      // MUTATION: put back `not served` as the cell's text.
      expect(cell.textContent).not.toMatch(/not served/)
      const dash = within(cell).getByRole('img')
      expect(dash.textContent).toBe('—')
      expect(dash.getAttribute('aria-label')).toBe(
        'Not reported by this API yet: it does not say when a secret was last released to an attempt',
      )
    }
  })

  it('still says `never` when the API serves a null', async () => {
    api([record('alpha', null)])
    const [cell] = await lastUsedCells()
    expect(cell!.textContent).toBe('never')
  })
})
