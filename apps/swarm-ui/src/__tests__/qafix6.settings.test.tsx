// SETTINGS › MCP SERVERS, PROFILE, API KEYS AND PEOPLE, VISUAL QA RUN 6
// (lane QAFIX6-SETTINGS; part of #1680). One case per item, each named by its
// run-6 finding and its #1680 box:
//
//   a. 6:overview-automate-001 (box 6343770942) -- at 1440 the Registered MCP
//      servers table had no rule outside the phone block: centred cells, no
//      padding, 'disabled never' run together;
//   b. 6:overview-automate-003 (box 6343770942) -- 'Credential', 'Profiles
//      that may use it' and 'What reaches your bell' drew the browser's
//      fieldset border and an oversized legend;
//   c. 6:overview-automate-002, -004 (box 6343771764) -- Profile's `pf-*`
//      classes matched no rule: bulleted lists, default definition lists, and
//      cards 2px apart;
//   d. box 6343759200 -- each notification box sits beside its own words;
//   e. 6:overview-automate-017 -- '5-' / 'hour' and '3d' / '22h' broke at 390;
//   f. 6:overview-automate-016 (box 6343774039) -- a 5-hour window said
//      'resets in 19h 43m';
//   g. 6:overview-automate-019 (box 6343758363) -- the register form's
//      profile help spoke about the API, not to the person;
//   h. box 6343757522 -- the table stacks at 390;
//   i. box 6343774758 -- People said 'You are this workspace's admin' while
//      API keys said only 'an admin' could set the key, and offered nothing.
//
// Layout is read off the shipped sheets with `painted` (the cascade at a
// stated width, since jsdom applies no `@media`). Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { ALL_CSS } from './sheets'
import { lengthPx, nowrap, paddingX } from './tablefit'

type Json = Record<string, unknown>

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const HOUR = 3_600_000

/** The sheets' text with every `@media` block taken out: the rules that hold at every width. */
function outsideMedia(css: string): string {
  let out = ''
  let i = 0
  while (i < css.length) {
    const at = css.indexOf('@media', i)
    if (at === -1) {
      out += css.slice(i)
      break
    }
    out += css.slice(i, at)
    let j = css.indexOf('{', at)
    let depth = 1
    while (depth > 0 && ++j < css.length) {
      if (css[j] === '{') depth++
      else if (css[j] === '}') depth--
    }
    i = j + 1
  }
  return out.replace(/\/\*[\s\S]*?\*\//g, '')
}

/** The bodies of every top-level rule whose selector mentions `sel`. */
function rulesFor(css: string, sel: RegExp): string[] {
  const out: string[] = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  for (let m = re.exec(css); m !== null; m = re.exec(css)) {
    if (sel.test(m[1]!)) out.push(m[2]!)
  }
  return out
}

function px(el: Element, prop: string | string[], env: CascadeEnv = WIDE): number | null {
  return lengthPx(painted(el, prop, env), 0)
}

// ---- MCP servers ----------------------------------------------------------

function mcpServer(name: string, over: Json = {}): Json {
  return {
    server_id: `mcp_${name}`,
    tenant_id: 'eng',
    name,
    url: `https://${name}.example.com/mcp`,
    transport: 'streamable-http',
    description: `the ${name} knowledge base`,
    auth: { mode: 'dispatch', header: 'Authorization', scheme: 'Bearer' },
    allowed_profiles: [],
    allowed_tools: [],
    limits: {},
    enabled: true,
    revision: 1,
    consumer: `mcp:${name}`,
    created_at: '2026-10-17T10:00:00Z',
    created_by: 'admin@example.com',
    updated_at: '2026-10-17T10:00:00Z',
    updated_by: 'admin@example.com',
    last_attached_at: null,
    ...over,
  }
}

const MCP_LIST: Json = {
  tenant_id: 'eng',
  can_manage: true,
  servers: [
    mcpServer('docs-search', { allowed_profiles: ['claude-code'], allowed_tools: ['search'] }),
    mcpServer('status-board', { enabled: false, auth: { mode: 'none', header: 'Authorization', scheme: 'Bearer' } }),
  ],
  last_attached_complete: true,
  egress_addresses: ['203.0.113.7'],
}

type Inputs = 'unserved' | 'declared' | 'none-declare'

function capacity(inputs: Inputs): Json {
  const contract = (mcp: boolean): Json => {
    if (inputs === 'unserved') return { required_keys: ['prompt'] }
    return { required_keys: ['prompt'], inputs: inputs === 'declared' && mcp ? { mcp_servers: { type: 'list', maximum: 4 } } : {} }
  }
  return {
    generated_at: '2026-10-22T10:00:00Z',
    tenant_id: 'eng',
    pools_complete: true,
    pools: [{ name: 'claude-code', hard_limit: 4, adaptive_target: null, quota_derived_limit: null, effective_limit: 4, active: 0, available: 4, enabled: true }],
    runner_profiles: {
      'claude-code': { input_contract: contract(true), available: true, disabled_reason: '' },
      generic: { input_contract: contract(false), available: true, disabled_reason: '' },
    },
  }
}

/** `cap` null: /v1/capacity fails, so the runner list is unread. */
async function mountMcp(cap: Json | null): Promise<HTMLTableElement> {
  serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/mcp-servers')) return { status: 200, body: MCP_LIST }
    if (m === 'GET' && url.startsWith('/v1/capacity')) return cap === null ? { status: 500, body: { detail: 'down' } } : { status: 200, body: cap }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  render(<McpServersScreen />)
  return (await screen.findByRole('table', { name: 'Registered MCP servers' })) as HTMLTableElement
}

const profilesGroup = () =>
  within(screen.getByRole('form', { name: 'Register an MCP server' })).getByRole('group', { name: 'Profiles that may use it' })

// ---- Profile ----------------------------------------------------------------

const TOPICS = ['approval', 'waiting_step', 'question', 'failed', 'finished', 'account', 'admin']

function profileBody(): Json {
  const at = (ms: number) => new Date(Date.now() + ms).toISOString()
  return {
    identity: { email: 'ada@example.com', display_name: null, display_name_suggested: 'Ada', domain: 'example.com', platform_role: null, is_admin: false, is_admin_unresolved: false },
    workspaces: [
      {
        tenant_id: 'u-ada', display_name: 'Your workspace (u-ada)', kind: 'user',
        role: { name: 'owner', source: 'derived', via: null }, state: 'ready', selectable: true, is_acting: true, is_default: true,
      },
    ],
    default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: null },
    connections: {
      github: [
        {
          tenant_id: 'u-ada', state: 'connected', forge_login: 'ada', method: 'oauth',
          access_expires_at: at(6 * HOUR + 30_000), refreshed_at: null, failure: null,
        },
      ],
      typesafe: { personal: { set: true, set_at: at(-5 * 24 * HOUR), set_by: 'ada@example.com', version: '2' }, acting_workspace: { tenant_id: 'u-ada', set: false, set_at: null, set_by: null, version: null }, uses: 'user' },
    },
    notifications: { preferences: { muted: ['finished'], reminder: 'every_4h' }, topics: TOPICS },
    claude_accounts: {
      personal_workspace: 'u-ada',
      accounts: [
        {
          account_id: 'acct_main', label: 'main', relation: 'own', state: 'AVAILABLE', lent_to_count: 0, stale: false,
          windows: {
            five_hour: { utilization: 0.42, resets_at: at(2 * HOUR + 30 * 60_000) },
            seven_day: { utilization: 0.31, resets_at: at(94 * HOUR + 30_000) },
          },
        },
      ],
      unreadable_document_count: 0,
    },
    revision: 4,
    unavailable: [],
  }
}

async function mountProfile(): Promise<HTMLElement> {
  serve((m, url) => (m === 'GET' && url === '/v1/profile' ? { status: 200, body: profileBody() } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ProfileScreen } = await import('../Profile')
  const { container } = render(<ProfileScreen />)
  await screen.findByRole('form', { name: 'Change your notifications' })
  return container
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

// ---------------------------------------------------------------------------

describe('a. 6:overview-automate-001: the MCP servers table is a table at 1440', () => {
  it('has header and cell rules outside any media query: left-aligned, padded, Last used on one line', async () => {
    const table = await mountMcp(capacity('declared'))
    const sheet = outsideMedia(ALL_CSS)
    // MUTATION: delete keys.css' desktop `.mcp-servers` rules -> only the phone block styles it: red.
    expect(rulesFor(sheet, /\.mcp-servers\b[^,]*\b(th|td)\b/).some((b) => /text-align:\s*left/.test(b) && /padding/.test(b))).toBe(true)
    const head = table.querySelector('thead th')!
    const cell = table.querySelector('tbody td')!
    expect(painted(head, 'text-align', WIDE)).toBe('left')
    expect(paddingX(head, WIDE)[0]).toBeGreaterThan(0)
    expect(paddingX(cell, WIDE)[1]).toBeGreaterThan(0)
    const lastHead = [...table.querySelectorAll('thead th')].find((th) => th.textContent === 'Last used')!
    const lastCell = table.querySelector('tbody td[data-label="Last used"]')!
    // MUTATION: let Last used wrap -> 'Last' / 'used' and the 'disabled never' run-on return: red.
    expect(nowrap(lastHead, WIDE)).toBe(true)
    expect(nowrap(lastCell, WIDE)).toBe(true)
  })
})

describe('b. 6:overview-automate-003: grouped controls have no browser border and a label-sized legend', () => {
  it('draws Credential, Profiles that may use it and What reaches your bell without a border', async () => {
    await mountMcp(capacity('declared'))
    const form = screen.getByRole('form', { name: 'Register an MCP server' })
    const sets = [...form.querySelectorAll('fieldset')]
    expect(sets.map((f) => f.querySelector('legend')?.textContent)).toEqual(['Credential', 'Profiles that may use it'])
    cleanup()
    const page = await mountProfile()
    sets.push(...page.querySelectorAll('fieldset'))
    expect(sets).toHaveLength(3)
    for (const f of sets) {
      // MUTATION: drop `border: 0` from the fieldset rule -> the 1px near-white rectangle: red.
      expect(String(painted(f, ['border', 'border-width', 'border-style'], WIDE))).toMatch(/^(0|none|0px)$/)
      // MUTATION: leave the legend at the browser's size -> an oversized group name: red.
      expect(painted(f.querySelector('legend')!, 'font-size', WIDE)).toBe('var(--t-micro)')
    }
  })
})

describe('c. 6:overview-automate-002, -004: Profile reads like API keys', () => {
  it('styles every pf-* class it draws: no bullets, label and value rows, 12px or more between cards', async () => {
    const page = await mountProfile()
    const classes = new Set<string>()
    for (const el of page.querySelectorAll('[class]')) for (const c of el.classList) if (c.startsWith('pf-')) classes.add(c)
    expect([...classes].sort()).toEqual(expect.arrayContaining(['pf-facts', 'pf-list', 'pf-page']))
    for (const c of classes) {
      // MUTATION: delete any `.pf-*` rule -> that class draws browser defaults: red.
      expect(ALL_CSS, `.${c} has no rule`).toMatch(new RegExp(`\\.${c}\\b[^{}]*\\{`))
    }
    const list = page.querySelector('.pf-list')!
    expect(painted(list, ['list-style', 'list-style-type'], WIDE)).toBe('none')
    expect(px(list, ['padding-left', 'padding'])).toBe(0)
    const pageEl = page.querySelector('.pf-page')!
    // MUTATION: drop `.pf-page`'s gap -> cards 2px apart, doubled borders: red.
    expect(px(pageEl, ['gap', 'row-gap'])).toBeGreaterThanOrEqual(12)
    const facts = page.querySelector('.pf-facts')!
    expect(painted(facts, 'display', WIDE)).toBe('grid')
    expect(px(facts.querySelector('dd')!, ['margin-left', 'margin'])).toBe(0)
  })
})

describe('d. box 6343759200: each notification box sits beside its own words', () => {
  it('puts each input inside its own label, 6-8px from the words, with rows apart', async () => {
    const page = await mountProfile()
    const rows = [...page.querySelectorAll('fieldset label')]
    expect(rows).toHaveLength(TOPICS.length)
    for (const r of rows) {
      // MUTATION: move the input out of its label -> a box that belongs to no words: red.
      expect(r.querySelectorAll('input[type="checkbox"]')).toHaveLength(1)
      expect(r.textContent?.trim()).not.toBe('')
    }
    const gap = px(rows[0]!, ['column-gap', 'gap'])
    // MUTATION: set `.jb-option`'s gap to 0 -> the box touches its words: red.
    expect(gap).toBeGreaterThanOrEqual(6)
    expect(gap).toBeLessThanOrEqual(8)
    expect(painted(rows[0]!.parentElement!, 'flex-direction', WIDE)).toBe('column')
  })
})

describe('e. 6:overview-automate-017: Profile keeps each quantity whole', () => {
  it("writes '5‑hour' with a non-breaking hyphen and joins every duration's figures with U+00A0", async () => {
    const page = await mountProfile()
    const text = page.textContent ?? ''
    // MUTATION: build the label with an ASCII hyphen -> '5-' / 'hour' at 390: red.
    expect(text).toContain('5‑hour')
    expect(text).toContain('7‑day')
    expect(text).not.toMatch(/\d-(hour|day)/)
    const durations = text.match(/\d+[dh][\s ]\d+[hm]/g) ?? []
    // 7-day '3d 22h', 5-hour '2h 30m' and GitHub's '6h 00m'.
    expect(durations.length).toBeGreaterThanOrEqual(3)
    // MUTATION: drop the U+00A0 join -> '3d' / '22h' break apart: red.
    for (const d of durations) expect(d).toMatch(/^\d+[dh] \d+[hm]$/)
  })
})

describe('f. 6:overview-automate-016: a reset further away than its window is not a countdown', () => {
  it('says the reading is out of date instead of "resets in 19h" on a 5-hour window', async () => {
    vi.resetModules()
    const { windowWords } = await import('../Profile')
    const far = windowWords('5‑hour', 5 * HOUR, { utilization: 0.88, resets_at: new Date(Date.now() + 19 * HOUR + 43 * 60_000).toISOString() })
    // MUTATION: drop the window-length check -> '5‑hour 88%, resets in 19h 43m': red.
    expect(far).not.toMatch(/resets in/)
    expect(far).toMatch(/^5‑hour 88%, .*out of date/)
    const near = windowWords('5‑hour', 5 * HOUR, { utilization: 0.88, resets_at: new Date(Date.now() + 2 * HOUR).toISOString() })
    expect(near).toMatch(/resets in/)
  })
})

describe('g. 6:overview-automate-019: the profile help says what ticking does, and agrees with the table', () => {
  it('words the served, unserved and unread cases for the person', async () => {
    await mountMcp(capacity('declared'))
    // MUTATION: keep the old API-facing note -> the person reads about the API: red.
    await waitFor(() => expect(visible(profilesGroup())).toMatch(/Tick a profile to let only its agents use this server/))
    cleanup()

    await mountMcp(capacity('unserved'))
    await waitFor(() => expect(visible(profilesGroup())).toMatch(/Could not read which runners take MCP servers; offering the ones your servers already use/))
    expect(visible(profilesGroup())).not.toMatch(/This API does not say/)
    cleanup()

    await mountMcp(null)
    await waitFor(() => expect(visible(profilesGroup())).toMatch(/Could not read the runner list; offering the ones your servers already use/))
    cleanup()

    // Every served contract declares none, yet docs-search names claude-code.
    await mountMcp(capacity('none-declare'))
    await waitFor(() => expect(within(profilesGroup()).getByRole('checkbox', { name: 'claude-code' })).toBeTruthy())
    // MUTATION: offer only the served list -> 'No profile here takes MCP servers yet' under a table naming one: red.
    expect(visible(profilesGroup())).not.toMatch(/No profile here takes MCP servers/)
  })
})

describe('h. box 6343757522: at 390 the MCP servers table stacks', () => {
  it('draws the table as blocks below 720px', async () => {
    const table = await mountMcp(capacity('declared'))
    // MUTATION: drop the phone block's `.mcp-servers { display: block }` -> eight columns scroll sideways: red.
    expect(painted(table, 'display', PHONE)).toBe('block')
  })
})

describe("i. box 6343774758: People and API keys read the same role", () => {
  function meBody(admin: boolean, kind: 'group' | 'user'): Json {
    return {
      tenant: { tenant_id: kind === 'user' ? 'u-carol' : 'eng', kind, display_name: null },
      principal: { email: 'carol@example.com', domain: 'example.com', groups: [], is_admin: admin },
      environment: 'dev',
      environment_declared: true,
    }
  }
  function keysBody(tenant: string): Json {
    const off = { available: true, set: false, set_at: null, set_by: null, version: null, secret_id: null }
    return { provider: 'typesafe', user: off, tenant: { ...off, tenant_id: tenant }, resolution: { tenant_id: tenant, uses: 'none', secret_id: null }, consumed_by: [] }
  }
  const PERSONAL: Json = {
    tenant_id: 'u-carol', kind: 'user', default_role: null, default_source: null,
    me: { email: 'carol@example.com', role: 'admin', source: 'personal', via: 'personal' },
    people: [], roles: [], can_manage: false, audit: [],
  }

  async function both(kind: 'group' | 'user'): Promise<{ people: string; keys: string }> {
    const tenant = kind === 'user' ? 'u-carol' : 'eng'
    serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/tenants/me')) return { status: 200, body: meBody(false, kind) }
      if (m === 'GET' && url.startsWith('/v1/provider-keys/typesafe')) return { status: 200, body: keysBody(tenant) }
      if (m === 'GET' && url === '/v1/workspace/roles') return { status: 200, body: PERSONAL }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ProviderKeysScreen } = await import('../ProviderKeys')
    const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
    const keys = render(<ProviderKeysScreen link={null} />)
    await screen.findByText(new RegExp(`your agents in ${tenant} would use`, 'i'))
    const people = render(<WorkspacePeopleScreen />)
    await waitFor(() => expect(visible(people.container)).toMatch(/admin/))
    return { people: visible(people.container), keys: visible(keys.container) }
  }

  it("never tells the personal workspace's admin that only an admin can set its key", async () => {
    const { people, keys } = await both('user')
    expect(people).toMatch(/You are this workspace’s admin/)
    // MUTATION: draw the non-admin refusal on a personal workspace -> 'only an admin can set' to its admin: red.
    expect(keys).not.toMatch(/only an admin can/)
    expect(keys).toMatch(/u-carol is your personal workspace, so its key is Your key above/)
  })

  it('names the role the tenant key needs: platform admin', async () => {
    serve((m, url) => {
      if (m === 'GET' && url.startsWith('/v1/tenants/me')) return { status: 200, body: meBody(false, 'group') }
      if (m === 'GET' && url.startsWith('/v1/provider-keys/typesafe')) return { status: 200, body: keysBody('eng') }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ProviderKeysScreen } = await import('../ProviderKeys')
    render(<ProviderKeysScreen link={null} />)
    await screen.findByText(/your agents in eng would use/i)
    // MUTATION: say 'only an admin' -> a workspace admin reads it as themself: red.
    expect(visible(document.body)).toMatch(/only a platform admin can set or remove it/)
  })
})
