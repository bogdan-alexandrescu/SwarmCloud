// THE BADGE SAYS WHERE THE REQUESTS GO.
//
// PR #19 made `GET /v1/tenants/me` report `environment` and
// `environment_declared` -- the environment the API process ACTS on -- and the
// header read neither. So a console run from a laptop with the dev proxy
// pointed at a deployed API (`SWARM_API_ORIGIN=… VITE_LIVE=1 npm run dev`)
// drew `Local`, quiet and dashed, over a console whose pool ceilings were a
// deployed environment's; and a bundle built as `dev` drew `Dev` whatever the
// API behind it said. The badge is a safety property (Brand.tsx), and the only
// source that cannot be pointed somewhere else is the API's own answer.
//
// The badge rules this must not regress (design-system.md §13.6, brand.test.tsx):
// production and "unknown" are the loud kinds -- capitals and the full-width
// bar -- and everything else is sentence case with no bar; and a DEFAULTED
// "dev" is not a declared one.
//
// The first two cases were pushed before Brand.tsx read the fields and failed
// there. The last three are guards against over-correcting, and passed there:
// they hold what must NOT change.

import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ProductHeader } from '../Brand'
import type { Result } from '../fetch'
import type { Me } from '../types'

function me(environment: unknown, declared: unknown): Me {
  const base = {
    tenant: {
      tenant_id: 'u-bogdan',
      kind: 'user',
      principal: 'someone@saga.xyz',
      display_name: 'Bogdan',
      created_at: new Date().toISOString(),
      max_active: 4,
      capacity_units: 8,
      monthly_budget_usd: null,
      enabled: true,
      credentials: ['anthropic'],
      service_account: 'swarm-agent-worker-u-bogdan@example.iam.gserviceaccount.com',
      gcs_prefix: 'tenants/u-bogdan',
      namespace: 'swarm-u-bogdan',
    },
    principal: { email: 'someone@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: false },
  }
  // An older API sends neither key; `undefined` here leaves them out.
  const env: Record<string, unknown> = {}
  if (environment !== undefined) env.environment = environment
  if (declared !== undefined) env.environment_declared = declared
  return { ...base, ...env } as unknown as Me
}

function ok(data: Me): Result<Me> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' }
}

/** Render the header and wait for the identity read to land. */
async function header(load: () => Promise<Result<Me>>, declaredEnv: string | undefined, host: string) {
  const { container } = render(<ProductHeader load={load} declaredEnv={declaredEnv} host={host} />)
  // The identity half is drawn from the same read, so once the tenant is on
  // the header the badge has had its chance to follow the API.
  await screen.findByText('u-bogdan', undefined, { timeout: 5000 })
  const badge = container.querySelector('.brand-env')!
  return {
    badge,
    name: () => (badge.querySelector('.brand-env-name')?.textContent ?? '').trim(),
    bar: () => container.querySelector('.brand-bar') !== null,
    explain: () => badge.getAttribute('title') ?? '',
  }
}

describe('the environment badge follows the API when the API declared one', () => {
  it('says PRODUCTION, loudly, when a dev-built console is talking to a production API', async () => {
    const h = await header(() => Promise.resolve(ok(me('prod', true))), 'dev', 'swarm.saga.xyz')
    await waitFor(() => expect(h.name()).toBe('PROD'))
    expect(h.bar(), 'a production API drew no bar').toBe(true)
    expect(h.badge.className).toContain('is-prod')
    // Both sources are named, because a surprising badge owes its reader why.
    expect(h.explain()).toMatch(/reported by the API/)
    expect(h.explain()).toMatch(/The build declared dev/)
  })

  it('says Dev, not Local, on a laptop whose requests go to a deployed dev API', async () => {
    const h = await header(() => Promise.resolve(ok(me('dev', true))), undefined, 'localhost')
    await waitFor(() => expect(h.name()).toBe('Dev'))
    expect(h.badge.className).toContain('is-nonprod')
    expect(h.badge.className).not.toContain('is-local')
    expect(h.bar()).toBe(false)
  })
})

describe('and does not over-correct', () => {
  it('treats a DEFAULTED dev as no answer: an undeclared API keeps ENVIRONMENT UNKNOWN', async () => {
    const h = await header(() => Promise.resolve(ok(me('dev', false))), undefined, 'swarm.saga.xyz')
    expect(h.name()).toBe('ENVIRONMENT UNKNOWN')
    expect(h.bar()).toBe(true)
    expect(h.explain()).toContain('treat it as production')
  })

  it('falls back to the build when the API is older than the fields', async () => {
    const h = await header(() => Promise.resolve(ok(me(undefined, undefined))), 'staging', 'swarm.saga.xyz')
    expect(h.name()).toBe('Staging')
    expect(h.explain()).toMatch(/declared by the build/)
    expect(h.bar()).toBe(false)
  })

  it('falls back to the host when the identity read fails', async () => {
    const { container } = render(
      <ProductHeader
        load={() =>
          Promise.resolve({
            status: 'error',
            error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' },
          })
        }
        declaredEnv={undefined}
        host="localhost"
      />,
    )
    // RE-POINTED BY CH-20: the failed identity is the tenant key and the
    // `not read` mark, and "… tenant and sign-in unread" is its accessible
    // name rather than a sentence in the bar -- so the wait is for the name.
    await screen.findByLabelText(/tenant and sign-in unread/, undefined, { timeout: 5000 })
    expect((container.querySelector('.brand-env-name')?.textContent ?? '').trim()).toBe('Local')
  })
})
