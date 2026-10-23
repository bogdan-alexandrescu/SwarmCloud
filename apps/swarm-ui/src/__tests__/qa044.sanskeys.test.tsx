// QA044-SANS-KEYS (part of #1680): every stacked record in the console draws
// its field keys in the sans face.
//
// QA-044 (visual QA run wf_9e9bf74834634db7bcc3, crop admin-005) saw Pool
// limits' phone card keys in mono, beside sans column heads. QAFIX5 switched
// Pool limits alone, with an override in styles/admin.css, and asked whether
// the key face should change console-wide. BGD decided on 2026-10-23 (~17:30Z,
// QA-044, recorded on task_7f0095fb2fd9481790cc q0): console-wide. The shared
// rule in styles.css (`.is-stacked > table > tbody > tr > td[data-label]::before`)
// now sets the key in `var(--font)`, and the Pool limits override is gone
// because it is the default.
//
// A key is a label; a value that is an identifier (a task id, a principal, a
// pool's raw name) is still drawn mono, so the record reads key in sans, id in
// mono. Only the key's face changed: its size, weight, colour and the key
// column's width are pinned here as they were.
//
// Read off the shipped sheets through `painted` (the cascade, with its
// `@media` and `@container` blocks), at 390. Every assertion names the
// mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, Task, Tenant } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { SHEETS } from './sheets'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen } = await import('../AdminSettings')
const { TenantsScreen } = await import('../Activity')
const { StagedInputs } = await import('../StagedInputs')

const WAIT = { timeout: 5000 } as const
const PHONE: CascadeEnv = { width: 390 }
/** The agent drawer at 1440: its tables stack under `@container ctl-inspector`. */
const DRAWER: CascadeEnv = { width: 1440, height: 900, container: 360 }

/** The stacked key as it was drawn before, with the sans face in place of mono. */
const KEY_FONT = '500 var(--t-micro)/var(--lh-micro) var(--font)'
const KEY_COLOUR = 'var(--text-faint)'
const KEY_TRACKS = 'minmax(0, calc(var(--t-micro) * 10.5)) minmax(0, 1fr)'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-23T18:00:00Z' }
}

function pool(name: string): Pool {
  return {
    name,
    hard_limit: 5,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 5,
    active: 0,
    available: 5,
    enabled: true,
    updated_at: '2026-10-23T09:00:00Z',
  } as Pool
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-23T09:00:00Z' } as Capacity
}

function tenant(): Tenant {
  return {
    tenant_id: 'u-bogdan',
    kind: 'user',
    principal: 'alice@example.com',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 2,
    capacity_units: 40,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: 'swarm-agent-worker-u-bogdan@example-project.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
  } as Tenant
}

/** `matchMedia`, which jsdom lacks, answering each query by the width it names. */
function viewport(width: number): void {
  vi.stubGlobal('matchMedia', (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    const matches = (max === null || width <= Number(max[1])) && (min === null || width >= Number(min[1]))
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }
  })
}

/** The face a key or a value is drawn in, as the cascade leaves it. */
function face(el: Element, env: CascadeEnv, pseudo: string | null = null): string {
  return painted(el, ['font-family', 'font'], env, pseudo) ?? ''
}

/** A stacked key: sans, and otherwise exactly the key it was. */
function expectSansKey(td: Element, env: CascadeEnv): void {
  const label = td.getAttribute('data-label')!
  expect(painted(td, 'content', env, 'before'), `${label} is said`).toBe('attr(data-label)')
  const f = face(td, env, 'before')
  expect(f, `${label}'s key face`).toMatch(/var\(--font\)/)
  expect(f, `${label}'s key face`).not.toMatch(/--mono/)
}

const hosts: HTMLElement[] = []

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  document.body.innerHTML = ''
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  for (const h of hosts.splice(0)) h.remove()
})

describe('QA-044, console-wide: the shared stacked-record rule sets the key in sans', () => {
  it('draws a stacked key in the prose face on a page with no override of its own', () => {
    const td = build('div.ctl-table.is-stacked > table > tbody > tr > td', hosts)
    td.setAttribute('data-label', 'Key')
    // MUTATION: put `var(--mono)` back in styles.css's phone-block
    // `td[data-label]::before` font: every stacked key outside Pool limits is
    // mono again, and this reads `... var(--mono)`.
    expect(painted(td, 'font', PHONE, 'before')).toBe(KEY_FONT)
    // MUTATION: change the key's colour along with its face: the face was the
    // only thing BGD's decision moved.
    expect(painted(td, 'color', PHONE, 'before')).toBe(KEY_COLOUR)
    // MUTATION: widen or narrow the key column with the face change: values
    // start at a new x on every record in the console.
    expect(painted(td, 'grid-template-columns', PHONE)).toBe(KEY_TRACKS)
  })

  it('draws the inspector drawer’s stacked key in the prose face too', () => {
    const td = build('div.ctl-table.is-stacked > table > tbody > tr > td', hosts)
    td.setAttribute('data-label', 'Key')
    // MUTATION: change only the phone block and leave the `@container
    // ctl-inspector` restatement mono: the agent drawer's records at 1440
    // keep mono keys while the same records at 390 are sans.
    expect(painted(td, 'font', DRAWER, 'before')).toBe(KEY_FONT)
    expect(painted(td, 'color', DRAWER, 'before')).toBe(KEY_COLOUR)
  })

  it('carries no Pool limits override for the key face, since it is the default', () => {
    const admin = SHEETS.find(([path]) => path === 'src/styles/admin.css')
    expect(admin, 'styles/admin.css is a shipped sheet').toBeDefined()
    // MUTATION: leave QAFIX5's `.adm-family .is-stacked ... td[data-label]::before
    // { font-family: var(--font) }` in admin.css: a second statement of the key
    // face that drifts the day someone changes the shared one.
    expect(admin![1]).not.toMatch(/td\[data-label\]::before\s*\{[^}]*font-family/)
  })
})

describe('QA-044 at 390: Pool limits (Settings) draws keys in sans and the pool id in mono', () => {
  it('sets In use, Ceiling, Set by and Last changed in the prose face', async () => {
    viewport(390)
    api.loadCapacity.mockResolvedValue(ok(capacity([pool('global')])))
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(document.querySelector('table.adm-limits')).not.toBeNull(), WAIT)
    const row = document.getElementById('limit-global')!
    const cells = [...row.querySelectorAll('td[data-label]')]
    expect(cells.map((c) => c.getAttribute('data-label'))).toEqual(['In use (units)', 'Ceiling (units)', 'Set by', 'Last changed'])
    // MUTATION: drop the sans face from the shared rule (the admin.css
    // override is gone): every Pool limits key is mono again.
    for (const c of cells) expectSansKey(c, PHONE)
    // MUTATION: set the row header's raw pool name in `var(--font)`: the name
    // pasted into pool-limit.sh is no longer drawn as the literal it is.
    const id = row.querySelector('th.pool-name > .ctl-sub')!
    expect(id.textContent).toContain('global')
    expect(face(id, PHONE)).toMatch(/var\(--mono\)/)
  })
})

describe('QA-044 at 390: a staged-inputs record (Agents) draws its key in sans and the task id in mono', () => {
  it('sets From and Size in the prose face and the upstream task id in mono', () => {
    viewport(390)
    const upstream = 'task_' + 'a1b2c3d4'.repeat(3)
    const task = {
      task_id: 'task_' + 'f0e1d2c3'.repeat(3),
      state: 'SUCCEEDED',
      metadata: { input_from: { [upstream]: 'plan.md' } },
      result_summary: { staged_inputs: [{ task_id: upstream, filename: 'plan.md', path: 'plan.md', bytes: 17 * 1024 }] },
    } as unknown as Task
    render(<StagedInputs task={task} />)
    const row = screen.getByRole('rowheader', { name: 'plan.md' }).closest('tr')!
    const cells = [...row.querySelectorAll('td[data-label]')]
    expect(cells.map((c) => c.getAttribute('data-label'))).toEqual(['From', 'Size'])
    for (const env of [PHONE, DRAWER]) {
      // MUTATION: put `var(--mono)` back in the shared key rule (the phone
      // block or the drawer's `@container` restatement): `From` and `Size`
      // read as values beside a mono task id.
      for (const c of cells) expectSansKey(c, env)
      // MUTATION: drop `.id`'s mono face, or let the key's face leak into the
      // value: the task id's characters can no longer be told apart one by one.
      const id = row.querySelector('td[data-label="From"] .id')!
      expect(id.textContent).toBe(upstream)
      expect(face(id, env)).toMatch(/var\(--mono\)/)
    }
  })
})

describe('QA-044 at 390: a Tenants record (Settings) draws its key in sans and the principal in mono', () => {
  it('sets Status, Kind, Principal and the rest in the prose face and the principal in mono', async () => {
    viewport(390)
    api.loadTenants.mockResolvedValue(ok({ tenants: [tenant()] }))
    api.loadCapacity.mockResolvedValue(ok(capacity([])))
    const { container } = render(<TenantsScreen />)
    await screen.findByText('u-bogdan', undefined, WAIT)
    expect(container.querySelector('.table-wrap.is-stacked'), 'the roster is stacked at 390').not.toBeNull()
    const cells = [...container.querySelectorAll('tbody td[data-label]')]
    expect(cells.length).toBeGreaterThanOrEqual(4)
    // MUTATION: drop the sans face from the shared key rule: the roster's
    // keys are mono again, the same face as the principal under them.
    for (const c of cells) expectSansKey(c, PHONE)
    // MUTATION: drop `className="mono"` from the Principal cell: an address
    // that is an identity is drawn in the prose face.
    const principal = container.querySelector('td[data-label="Principal"]')!
    expect(principal.textContent).toContain('alice@example.com')
    expect(face(principal, PHONE)).toMatch(/var\(--mono\)/)
  })
})
