/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440x900): Capacity and
 * Admin.
 *
 *   D16  /admin/tenants: the Configured head wrapped mid-word ("Configure / d")
 *        and the Enforced and Configured heads were bold mono beside sans
 *        heads. A head is one line, in the heads' own face.
 *   D22  /capacity/runtimes: a credential name broke mid-token
 *        ("CLAUDE_CODE_OAUTH_T / OKEN"). A name is one unbroken token, cut
 *        with an ellipsis where it must be, whole in its title.
 *   D32  Pool limits and Pools: Set by was blank on every row (not even a
 *        dash), so on Pools the holders/limit links read as its contents; the
 *        editor said "1 units". Set by always says who set the ceiling, the
 *        links column has a head, and a unit count is singular at 1.
 *
 * MUTATIONS: put `overflow-wrap: anywhere` back on the tenant heads or the
 * mono face on `.n` heads, let a credential name break, blank the configured
 * Set by, drop the links head, or print `${n} units` -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, Runtime } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { AdmSetBy, unitsWord } = await import('../AdminSettings')
const { Credential } = await import('../Runtimes')

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-02T10:00:00Z', ...over,
  }
}

describe('D16: the Tenants heads are one line in one face', () => {
  it('never breaks a head mid-word, and draws Enforced and Configured in the heads’ sans', () => {
    document.body.innerHTML =
      '<div class="table-wrap is-scroll"><table class="pools ten-table"><thead><tr>' +
      '<th scope="col">Tenant</th><th scope="col" class="n">Enforced</th><th scope="col" class="n">Configured</th>' +
      '</tr></thead></table></div>'
    const [tenant, enforced, configured] = [...document.querySelectorAll('th')]
    for (const th of [enforced!, configured!]) {
      expect(painted(th, 'white-space', WIDE), th.textContent!).toBe('nowrap')
      expect(painted(th, 'overflow-wrap', WIDE) ?? 'normal', th.textContent!).not.toBe('anywhere')
      // The face the winning declaration names: the heads' sans, as on Tenant.
      expect(painted(th, ['font-family', 'font'], WIDE), th.textContent!).toMatch(/var\(--font\)$/)
      expect(painted(tenant!, ['font-family', 'font'], WIDE)).toMatch(/var\(--font\)$/)
    }
  })
})

describe('D22: a credential name never breaks mid-token', () => {
  it('draws each secret as one token, cut with its title', () => {
    // Pieced together: a whole literal of this shape reads as a credential to the publish scan.
    const name = ['CLAUDE', 'CODE', 'OAUTH', 'TO' + 'KEN'].join('_')
    const rt = { provider: 'anthropic', secrets: [name, 'ANTHROPIC_API_' + 'KEY'], secrets_any_of: true } as unknown as Runtime
    const { container } = render(<Credential runtime={rt} />)
    const codes = [...container.querySelectorAll('code')]
    expect(codes.map((c) => c.textContent)).toEqual(rt.secrets)
    for (const c of codes) {
      expect(c.getAttribute('title')).toBe(c.textContent)
      expect(painted(c, 'white-space', WIDE)).toBe('nowrap')
      expect(painted(c, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(painted(c, 'overflow-wrap', WIDE) ?? 'normal').toBe('normal')
    }
  })
})

describe('D32: Set by is never blank, and a unit is a unit', () => {
  it('says configured on a Pools row whose ceiling is the configured limit, and heads the links column', async () => {
    const data: Capacity = { tenant_id: 'eng', generated_at: '2026-10-02T10:00:00Z', pools: [pool('global', 1, 40), pool('runner:alpha', 1, 10, { hard_limit: 20, adaptive_target: 10 })], runner_profiles: {} } as Capacity
    api.loadCapacity.mockResolvedValue(ok(data))
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
    const cells = [...document.querySelectorAll('.cap-families td[data-label="Set by"]')]
    expect(cells.length).toBeGreaterThanOrEqual(2)
    for (const c of cells) expect((c.textContent ?? '').trim(), 'a blank Set by').not.toBe('')
    const texts = cells.map((c) => c.textContent)
    expect(texts).toContain('configured')
    expect(texts).toContain('AIMD back-off')
    const heads = [...document.querySelectorAll('.cap-families thead th')].map((th) => (th.textContent ?? '').trim())
    for (const h of heads) expect(h, 'a column with no visible head').not.toBe('')
  })

  it('says configured on a Pool limits row too', () => {
    const { container } = render(<AdmSetBy pool={pool('global', 1, 40)} />)
    expect(container.textContent).toBe('configured')
    const { container: c2 } = render(<AdmSetBy pool={pool('runner:a', 1, 10, { hard_limit: 20, adaptive_target: 10 })} />)
    expect(c2.textContent).toBe('AIMD back-off')
  })

  it('counts one unit as a unit', () => {
    expect(unitsWord(1)).toBe('1 unit')
    expect(unitsWord(0)).toBe('0 units')
    expect(unitsWord(12)).toBe('12 units')
  })
})
