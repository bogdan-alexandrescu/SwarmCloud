// THE PILL SAYS WHERE THE REQUESTS GO.
//
// PR #19 made `GET /v1/tenants/me` report `environment` and
// `environment_declared` -- the environment the API process ACTS on -- and the
// header read neither. So a console run from a laptop with the dev proxy
// pointed at a deployed API (`SWARM_API_ORIGIN=… VITE_LIVE=1 npm run dev`)
// drew `Local`, quiet and dashed, over a console whose pool ceilings were a
// deployed environment's; and a bundle built as `dev` drew `Dev` whatever the
// API behind it said. The environment is a safety property (Brand.tsx), and
// the only source that cannot be pointed somewhere else is the API's own
// answer.
//
// RE-POINTED BY THE REBRAND (2026-10-01). These cases were written against the
// product header's badge. The header is gone; what names the environment now
// is the Sky spine (Spine.tsx): the panel's pill, the phone header's mini pill
// and the red `.sk-prodbar`. So every case renders the real `SkyShell`, with
// its real identity read answered through `fetch`, the build declared through
// `VITE_SWARM_ENV`, and the browser's host set on jsdom itself -- the three
// sources `classifyEnvironment` reads, each supplied the way the product
// receives it rather than as a prop the shell does not take.
//
// The rules this must not regress (design-system.md §13.6, brand.test.tsx):
// production and "unknown" are the loud kinds -- capitals and the full-width
// bar -- and everything else is sentence case with no bar; and a DEFAULTED
// "dev" is not a declared one.

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
      namespace: 'swarm-tenant-u-bogdan',
    },
    principal: { email: 'someone@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: false },
  }
  // An older API sends neither key; `undefined` here leaves them out.
  const env: Record<string, unknown> = {}
  if (environment !== undefined) env.environment = environment
  if (declared !== undefined) env.environment_declared = declared
  return { ...base, ...env } as unknown as Me
}

/** jsdom's own default, which every other test file runs at. */
const DEFAULT_URL = 'http://localhost:3000/'

/**
 * The browser's host, set where `window.location.hostname` reads it. Vitest
 * exposes the JSDOM instance as the `jsdom` global; `reconfigure` changes the
 * URL without a navigation, which is the only way to put a tab on a host that
 * is not this machine.
 */
function atUrl(url: string): void {
  const dom = (globalThis as unknown as { jsdom?: { reconfigure(o: { url: string }): void } }).jsdom
  if (dom === undefined) throw new Error('vitest exposes no jsdom global, so this file cannot set the host')
  dom.reconfigure({ url })
}

afterEach(() => atUrl(DEFAULT_URL))

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/**
 * Render the real shell with the identity read answering `answer`, the build
 * declaring `build` ('' declares nothing) and the tab on `host`.
 */
async function shell(answer: Response, build: string, host: string) {
  atUrl(host === 'localhost' ? DEFAULT_URL : `https://${host}/`)
  expect(window.location.hostname).toBe(host)
  // Fixtures off, so the identity read goes through `fetch`; `resetModules`
  // makes api.ts read the flag again (chrome.shared.test.tsx's `liveApp`).
  vi.stubEnv('VITE_LIVE', '1')
  vi.stubEnv('VITE_SWARM_ENV', build)
  vi.resetModules()
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) =>
    String(input).includes('/v1/tenants/me') ? answer.clone() : json(503, {}),
  ) as unknown as typeof fetch
  const { SkyShell } = await import('../Spine')
  const { container } = render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      {null}
    </SkyShell>,
  )
  const pill = (): Element => container.querySelector('.sk-panel .sk-pill')!
  return {
    pill,
    name: () => (pill().textContent ?? '').trim(),
    // The phone header draws the same verdict; it may never say another one.
    mini: () => (container.querySelector('.sk-pbar .sk-pill')?.textContent ?? '').trim(),
    bar: () => container.querySelector('.sk-prodbar') !== null,
    explain: () => pill().getAttribute('title') ?? '',
  }
}

describe('the environment pill follows the API when the API declared one', () => {
  it('says PROD, loudly, when a dev-built console is talking to a production API', async () => {
    const h = await shell(json(200, me('prod', true)), 'dev', 'swarm.saga.xyz')
    // The panel's tenant is drawn from the same read, so once it is there the
    // pill has had its chance to follow the API.
    await screen.findAllByText('Bogdan', undefined, { timeout: 5000 })
    expect(h.name()).toBe('PROD')
    expect(h.mini()).toBe('PROD')
    expect(h.bar(), 'a production API drew no bar').toBe(true)
    expect(h.pill().className).toContain('is-prod')
    // Both sources are named, because a surprising pill owes its reader why.
    expect(h.explain()).toMatch(/reported by the API/)
    expect(h.explain()).toMatch(/The build declared dev/)
  })

  it('says Dev, not Local, on a laptop whose requests go to a deployed dev API', async () => {
    const h = await shell(json(200, me('dev', true)), '', 'localhost')
    await screen.findAllByText('Bogdan', undefined, { timeout: 5000 })
    expect(h.name()).toBe('Dev')
    expect(h.mini()).toBe('Dev')
    expect(h.explain()).toMatch(/reported by the API/)
    expect(h.pill().className).not.toContain('is-prod')
    expect(h.bar()).toBe(false)
  })
})

describe('and does not over-correct', () => {
  it('treats a DEFAULTED dev as no answer: an undeclared API keeps ENVIRONMENT UNKNOWN', async () => {
    const h = await shell(json(200, me('dev', false)), '', 'swarm.saga.xyz')
    await screen.findAllByText('Bogdan', undefined, { timeout: 5000 })
    expect(h.name()).toBe('ENVIRONMENT UNKNOWN')
    expect(h.mini()).toBe('ENVIRONMENT UNKNOWN')
    expect(h.bar()).toBe(true)
    // The pill draws the loud treatment for not-knowing, as for production.
    expect(h.pill().className).toContain('is-prod')
    expect(h.explain()).toContain('treat it as production')
    // The host it could not classify is named, so the reader can act on it.
    expect(h.explain()).toContain('swarm.saga.xyz')
  })

  it('falls back to the build when the API is older than the fields', async () => {
    const h = await shell(json(200, me(undefined, undefined)), 'staging', 'swarm.saga.xyz')
    await screen.findAllByText('Bogdan', undefined, { timeout: 5000 })
    expect(h.name()).toBe('Staging')
    expect(h.explain()).toMatch(/declared by the build/)
    expect(h.bar()).toBe(false)
  })

  it('falls back to the host when the identity read fails', async () => {
    const h = await shell(json(500, { detail: 'boom' }), '', 'localhost')
    // The failed identity is said in the panel's tenant block, never left blank.
    await screen.findAllByText('not read', undefined, { timeout: 5000 })
    expect(h.name()).toBe('Local')
    expect(h.explain()).toMatch(/which is this machine/)
    expect(h.bar()).toBe(false)
  })
})
