// SETTINGS › MCP SERVERS, PROFILE, API KEYS AND WORKSPACE, VISUAL QA RUN 5b
// (lane QAFIX5-SETTINGS; part of #1680). One case per finding a unit test can
// hold, each named by its 5b id:
//
//   * 5b:QA-009 -- at 390 the Registered MCP servers table was wider than the
//     viewport. Below 720px each server is now a stacked card, every cell
//     named by its column (`data-label`), so the page never scrolls sideways
//     and the row actions stay on screen;
//   * 5b:QA-010 -- the register form said "No profile here takes MCP servers
//     yet" over a table listing docs-search for claude-code. An API (or the
//     fixture build) that serves no `inputs` on any runner's contract has not
//     said which profiles take MCP servers; the form no longer reads that
//     silence as "none", and offers the profiles the registrations name;
//   * 5b:QA-011 -- Profile's notification checkboxes ran on after the previous
//     option's label. Each topic is now its own row, box first, as MCP
//     servers' Credential radios are;
//   * 5b:QA-012 -- in dark, an unticked native checkbox or radio on these four
//     pages measured about 1.6:1 against its card. Each is drawn with the
//     `--ctl-bd` ring Submit's radios carry (3:1 or more in both themes);
//     ticked, disabled and focused stay distinguishable.
//
// Layout and colour are read off the shipped sheets with `painted` (the
// cascade at a stated width and theme, since jsdom applies no `@media`).
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted, resolveColour, tokensIn, type Theme } from './marks'
import { contrast } from './spaceprobe'

type Json = Record<string, unknown>

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function mcpServer(name: string, over: Json = {}): Json {
  return {
    server_id: `mcp_${name}`,
    tenant_id: 'eng',
    name,
    url: `https://${name}.example.com/a/rather/long/path/to/the/mcp/endpoint`,
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
    mcpServer('docs-search', { allowed_profiles: ['claude-code'], allowed_tools: ['search', 'fetch_page'] }),
    mcpServer('status-board', { enabled: false, auth: { mode: 'none', header: 'Authorization', scheme: 'Bearer' } }),
  ],
  last_attached_complete: true,
  egress_addresses: ['203.0.113.7'],
}

/** A `/v1/capacity` whose runner contracts carry `inputs` or not, as `inputs` says. */
function capacity(inputs: 'unserved' | 'declared' | 'none-declare'): Json {
  const contract = (mcp: boolean): Json => {
    if (inputs === 'unserved') return { required_keys: ['prompt'] }
    const declared = inputs === 'declared' && mcp
    return { required_keys: ['prompt'], inputs: declared ? { mcp_servers: { type: 'list', maximum: 4 } } : {} }
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

async function mountMcp(cap: Json | null = capacity('unserved')): Promise<void> {
  serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/mcp-servers')) return { status: 200, body: MCP_LIST }
    if (m === 'GET' && url.startsWith('/v1/capacity') && cap !== null) return { status: 200, body: cap }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  render(<McpServersScreen />)
  await screen.findByRole('table', { name: 'Registered MCP servers' })
}

const TOPICS = ['approval', 'waiting_step', 'question', 'failed', 'finished', 'account', 'admin']

function profileBody(): Json {
  return {
    identity: { email: 'ada@example.com', display_name: null, display_name_suggested: 'Ada', domain: 'example.com', platform_role: null, is_admin: false, is_admin_unresolved: false },
    workspaces: [
      {
        tenant_id: 'u-ada', display_name: 'Your workspace (u-ada)', kind: 'user',
        role: { name: 'owner', source: 'derived', via: null }, state: 'ready', selectable: true, is_acting: true, is_default: true,
      },
    ],
    default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: null },
    connections: { github: [], typesafe: { personal: null, acting_workspace: { tenant_id: 'u-ada', set: false }, uses: 'none' } },
    notifications: { preferences: { muted: ['finished'], reminder: 'every_4h' }, topics: TOPICS },
    claude_accounts: { personal_workspace: 'u-ada', accounts: [], unreadable_document_count: 0 },
    revision: 4,
    unavailable: [],
  }
}

async function mountProfile(): Promise<HTMLElement> {
  serve((m, url) => (m === 'GET' && url === '/v1/profile' ? { status: 200, body: profileBody() } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ProfileScreen } = await import('../Profile')
  render(<ProfileScreen />)
  return (await screen.findByRole('form', { name: 'Change your notifications' })) as HTMLElement
}

function judgment(): Json {
  const labels: Record<string, string> = { 'internal-jev': 'Jev (Internal)', 'internal-laya': 'Jev (Internal Laya)', typesafe: 'Jev (TypeSafe)' }
  const options = Object.keys(labels).map((b) => ({ backend: b, label: labels[b], available: b !== 'typesafe', reason: b === 'typesafe' ? 'no TypeSafe key resolves for you in this workspace' : null }))
  return {
    tenant_id: 'eng',
    judgment_backend: 'internal-jev',
    can_edit: true,
    jev: { ...options[0]!, default: 'internal-jev', set_at: null, set_by: null, options },
  }
}

function codeIntel(): Json {
  return {
    tenant_id: 'eng', can_edit: true, jev: false, context: 'inherit', context_ab: 'inherit',
    defaults: { jev: false, context: 'on', context_ab: 'ab' }, updated_at: null, updated_by: null, repositories: [],
  }
}

/** API keys' Jev backend radios and Workspace's code-intelligence switch, mounted together. */
async function mountKeysAndWorkspace(): Promise<void> {
  serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/judgment')) return { status: 200, body: judgment() }
    if (m === 'GET' && url.startsWith('/v1/code-intelligence')) return { status: 200, body: codeIntel() }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { JevBackendCard } = await import('../JudgmentBackend')
  const { CodeIntelligenceCard } = await import('../CodeIntelligence')
  render(<><JevBackendCard /><CodeIntelligenceCard /></>)
  await screen.findByRole('radiogroup', { name: /jev backend/i })
  await screen.findByRole('switch', { name: /send code to jev/i })
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

// ---------------------------------------------------------------------------

describe('5b:QA-009: at 390 the Registered MCP servers table stacks, and nothing is off-screen', () => {
  it('names every body cell by its column, so a stacked card still says what each value is', async () => {
    await mountMcp()
    const table = screen.getByRole('table', { name: 'Registered MCP servers' })
    const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent?.trim() || th.getAttribute('aria-label'))
    expect(heads).toEqual(['Name', 'URL', 'Credential', 'Profiles', 'Tools', 'State', 'Last used', 'Actions'])
    const rows = [...table.querySelectorAll<HTMLTableRowElement>('tbody > tr')]
    expect(rows).toHaveLength(2)
    for (const r of rows) {
      // MUTATION: drop `data-label` from any cell in `Registrations` (or from
      // `LastUsed`'s cells) -> that value is drawn on a phone with no name: red.
      expect([...r.cells].map((c) => c.getAttribute('data-label'))).toEqual(heads)
    }
  })

  it('draws the table, its rows and cells as blocks below 720px and hides the header row there', async () => {
    await mountMcp()
    const table = screen.getByRole('table', { name: 'Registered MCP servers' })
    // MUTATION: drop the `.mcp-servers` stacking rules from keys.css' phone
    // block -> the table keeps its eight columns at 390 and scrolls the page: red.
    expect(painted(table, 'display', PHONE)).toBe('block')
    const thead = table.querySelector('thead')!
    // MUTATION: leave the header row in flow -> a row of eight squeezed column names heads the cards: red.
    expect(painted(thead, 'position', PHONE)).toBe('absolute')
    for (const r of table.querySelectorAll<HTMLTableRowElement>('tbody > tr')) {
      expect(painted(r, 'display', PHONE)).toBe('block')
      for (const c of r.cells) {
        // MUTATION: leave a cell `table-cell` -> it keeps the row's width and the actions fall off the right edge: red.
        expect(painted(c, 'display', PHONE), c.getAttribute('data-label') ?? '').toBe('block')
        // MUTATION: drop `overflow-wrap: anywhere` -> the URL, one unbroken word, is wider than the phone: red.
        expect(painted(c, 'overflow-wrap', PHONE), c.getAttribute('data-label') ?? '').toBe('anywhere')
      }
      const named = r.querySelector<HTMLElement>('td[data-label="URL"]')!
      // MUTATION: drop the `::before { content: attr(data-label) }` rule -> the cards carry values with no column names: red.
      expect(painted(named, 'content', PHONE, 'before')).toBe('attr(data-label)')
    }
    // Every row action is a named button in its card.
    expect(screen.getByRole('button', { name: 'Edit docs-search' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Enable status-board' })).toBeTruthy()
  })

  it('keeps the table a table at desktop width', async () => {
    await mountMcp()
    const table = screen.getByRole('table', { name: 'Registered MCP servers' })
    // MUTATION: move the stacking rules out of the phone block -> the desktop table loses its columns: red.
    expect(painted(table, 'display', WIDE)).not.toBe('block')
    expect(painted(table.querySelector('thead')!, 'position', WIDE)).not.toBe('absolute')
  })
})

// ---------------------------------------------------------------------------

const profilesGroup = () => {
  const form = screen.getByRole('form', { name: 'Register an MCP server' })
  return within(form).getByRole('group', { name: 'Profiles that may use it' })
}

describe('5b:QA-010: the register form does not say no profile takes MCP servers over a table that lists one', () => {
  it('offers the profiles the registrations name when the API serves no runner inputs', async () => {
    await mountMcp(capacity('unserved'))
    const box = await within(profilesGroup()).findByRole('checkbox', { name: 'claude-code' })
    expect((box as HTMLInputElement).checked).toBe(false)
    // MUTATION: read a contract with no `inputs` as "declares nothing" (the
    // old `mcpProfiles`) -> no box, and the note says no profile takes MCP servers: red.
    expect(visible(profilesGroup())).not.toMatch(/No profile here takes MCP servers/)
    // MUTATION: drop the unserved note -> the form offers names without saying where they came from: red.
    expect(visible(profilesGroup())).toMatch(/does not say which profiles take MCP servers/)
  })

  it('offers what the runners declare when the API serves their inputs', async () => {
    await mountMcp(capacity('declared'))
    const box = await within(profilesGroup()).findByRole('checkbox', { name: 'claude-code' })
    expect(box).toBeTruthy()
    // MUTATION: offer every profile once `inputs` is served -> generic, which declares none, is offered: red.
    expect(within(profilesGroup()).queryByRole('checkbox', { name: 'generic' })).toBeNull()
    expect(visible(profilesGroup())).not.toMatch(/does not say/)
  })

  it('still says none takes them when every served contract declares none', async () => {
    await mountMcp(capacity('none-declare'))
    // A measured "none": `inputs` is served and empty on every runner.
    // MUTATION: treat an empty `inputs` as unserved -> the measured answer is lost: red.
    expect(await within(profilesGroup()).findByText(/No profile here takes MCP servers yet/)).toBeTruthy()
    expect(within(profilesGroup()).queryAllByRole('checkbox')).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------

describe('5b:QA-011: each Profile notification topic is its own row, box first', () => {
  for (const env of [WIDE, PHONE]) {
    it(`one option per row at ${env.width}`, async () => {
      const form = await mountProfile()
      const group = within(form).getByRole('group', { name: 'What reaches your bell' })
      const boxes = within(group).getAllByRole('checkbox')
      expect(boxes).toHaveLength(TOPICS.length)
      // MUTATION: drop `jb-options` from the fieldset -> the labels flow inline, one after another: red.
      expect(painted(group, 'flex-direction', env)).toBe('column')
      for (const b of boxes) {
        const label = b.closest('label')!
        // MUTATION: drop `jb-option` from `.pf-topic` -> an unstyled inline label, the box after the last one's words: red.
        expect(painted(label, 'display', env)).toBe('flex')
        // MUTATION: put the words before the box -> the box ends one option and the next one's words follow: red.
        expect(label.firstElementChild).toBe(b)
        expect(b.nextElementSibling?.className).toBe('jb-text')
        expect(visible(b.nextElementSibling)).not.toBe('')
      }
    })
  }
})

// ---------------------------------------------------------------------------

/** The colour token a `border` (or `border-color`) value paints, resolved for `theme`. */
function edgeContrast(value: string, theme: Theme): number {
  const toks = tokensIn(value, theme)
  expect(toks.length, value).toBeGreaterThan(0)
  return contrast(toks[0]!.rgba, resolveColour('var(--surface)', theme))
}

const unticked = (els: HTMLInputElement[]) => els.filter((e) => !e.checked && !e.disabled)

describe('5b:QA-012: an unticked checkbox or radio on these settings pages is a 3:1 ring in both themes', () => {
  async function controls(): Promise<HTMLInputElement[]> {
    await mountMcp(capacity('declared'))
    await within(profilesGroup()).findByRole('checkbox', { name: 'claude-code' })
    const mcp = [...document.querySelectorAll<HTMLInputElement>('.jb-option > input')]
    cleanup()
    const form = await mountProfile()
    const prof = [...form.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
    cleanup()
    await mountKeysAndWorkspace()
    const rest = [...document.querySelectorAll<HTMLInputElement>('.jb-option > input')]
    return [...mcp, ...prof, ...rest]
  }

  for (const theme of THEMES) {
    it(`draws every unticked control's edge in --ctl-bd at 3:1 on the card, ${theme}`, async () => {
      const all = await controls()
      // Credential radios, a profile box, the topic boxes, Jev radios, the code switch.
      const kinds = new Set(all.map((e) => e.type))
      expect(kinds).toEqual(new Set(['radio', 'checkbox']))
      const open = unticked(all)
      expect(open.length).toBeGreaterThanOrEqual(5)
      for (const el of open) {
        for (const width of [1440, 390]) {
          const env: CascadeEnv = { width, theme }
          // MUTATION: drop `appearance: none` -> the platform draws its own grey control and the ring is never seen: red.
          expect(painted(el, 'appearance', env), el.outerHTML).toBe('none')
          const edge = painted(el, ['border', 'border-color'], env)
          // MUTATION: drop the keys.css ring -> no edge at all, the native 1.6:1 control: red.
          expect(edge, el.outerHTML).toContain('var(--ctl-bd)')
          // MUTATION: draw the ring in `--line` -> about 1.4:1: red.
          expect(edgeContrast(edge!, theme), `${el.outerHTML} ${theme}`).toBeGreaterThanOrEqual(3)
        }
      }
    })
  }

  it('fills a ticked control in the accent, keeps a refused one on the hairline, and rings focus', async () => {
    const all = await controls()
    const env: CascadeEnv = { width: 1440, theme: 'dark' }
    const ticked = all.filter((e) => e.checked && !e.disabled)
    expect(ticked.some((e) => e.type === 'radio')).toBe(true)
    expect(ticked.some((e) => e.type === 'checkbox')).toBe(true)
    for (const el of ticked) {
      // MUTATION: drop the `:checked` rule -> a ticked control is the same ring as an unticked one: red.
      expect(painted(el, ['border', 'border-color'], env), el.outerHTML).toContain('var(--info)')
    }
    for (const el of ticked.filter((e) => e.type === 'checkbox')) {
      // MUTATION: drop the tick -> a filled square with no mark: red.
      expect(painted(el, 'content', env, 'after'), el.outerHTML).toBe("''")
    }
    // The TypeSafe radio is refused: no key resolves.
    const refused = all.filter((e) => e.disabled && !e.checked)
    expect(refused.length).toBeGreaterThan(0)
    for (const el of refused) {
      // MUTATION: drop the `:disabled` rule -> a refused option reads as live: red.
      expect(painted(el, ['border', 'border-color'], env), el.outerHTML).toContain('var(--line)')
    }
    const focused: CascadeEnv = { ...env, states: ['focus-visible'] }
    for (const el of unticked(all)) {
      // MUTATION: drop the `:focus-visible` rule -> `appearance: none` leaves no focus mark: red.
      expect(painted(el, 'outline', focused), el.outerHTML).toContain('var(--info)')
    }
  })
})
