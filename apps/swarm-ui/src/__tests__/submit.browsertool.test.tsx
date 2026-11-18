// BSTATUS-SUBMIT (owner decision 2026-10-14, "Picker on Submit, Auto by
// default"): a browser task's Submit form lets a person pick the browser tool.
//
//   * the picker is drawn from what the profile DECLARES -- `browser_tool` in
//     the served `input_contract.inputs` -- and only then: a profile that does
//     not declare it refuses the key with 422 `invalid_input`;
//   * Auto is the default and sends nothing; any other pick sends
//     `input.browser_tool`;
//   * Jev is disabled, with a link to Config › Secrets (Settings › API keys until SSC-UI), when
//     `GET /v1/provider-keys/typesafe` says no key would be used, and picking
//     it prints the TypeSafe data notice inline;
//   * the read-only Network row is drawn from the profile's served `egress`,
//     and hidden when the API does not serve it;
//   * at 390px the segments sit two to a row.
//
// MUTATIONS, each of which turns a case red: draw the picker for a profile
// that does not declare `browser_tool`; send `browser_tool: "auto"`; enable
// Jev when `resolution.uses` is "none"; drop the notice; guess "Direct" when no
// egress is served; drop the 560px rule from submit.css.
//
// Nothing here is credential-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity } from '../types'
import SUBMIT_CSS from '../styles/submit.css?raw'
import { cascade } from './cssgate'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadProviderKeys: vi.fn(),
  loadJudgment: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  api.loadCapacity.mockImplementation(actual.loadCapacity)
  // The submit answers through fetch here, not the fixture build's 201
  // (QA2-FIX-07, runs2-01 and runs2-02).
  return { ...actual, ...api, USE_FIXTURES: false }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitScreen } = await import('../Submit')
const { BrowserToolPicker, declaredBrowserTools, profileEgress } = await import('../BrowserToolPicker')

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const realFetch = globalThis.fetch

const TOOLS = ['auto', 'playwright', 'dev-browser', 'jev']
// These cases are the TypeSafe backend's: the key decides (JEV-OFF's
// jev.off.test.tsx covers the internal backends).
const TYPESAFE_JEV = {
  backend: 'typesafe' as const, label: 'Jev (TypeSafe)', available: true, reason: null,
  default: 'internal-jev' as const, set_at: null, set_by: null, options: [],
}
const TYPESAFE = { kind: 'read' as const, jev: TYPESAFE_JEV }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function keys(uses: 'user' | 'tenant' | 'none') {
  const blank = { available: true, set: false, set_at: null, set_by: null, version: null }
  return ok({
    provider: 'typesafe',
    user: { ...blank, set: uses === 'user' },
    tenant: { ...blank, set: uses === 'tenant', tenant_id: 'eng' },
    resolution: { tenant_id: 'eng', uses },
    consumed_by: [],
  })
}

/** The fixture's capacity, with `claude-code-browser` declaring `browser_tool` (and serving `egress`) as asked. */
async function capacity(opts: { declare: boolean; egress?: unknown }): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  const p = data.runner_profiles['claude-code-browser']! as unknown as Record<string, unknown>
  if (opts.declare) {
    p.input_contract = {
      required_keys: ['prompt'],
      inputs: { browser_tool: { kind: 'string', choices: TOOLS, means: 'which browser tool the agent drives' } },
    }
  }
  if (opts.egress !== undefined) p.egress = opts.egress
  return ok(data)
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/tasks')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      return new Response(JSON.stringify({ task: { id: 't_bt_1', state: 'READY' }, scheduler_woken: true }), { status: 201, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
  return posts
}

async function browserForm() {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code-browser"]')!)
  const prompt = r.container.querySelector<HTMLTextAreaElement>('.sbf-build textarea')!
  fireEvent.change(prompt, { target: { value: 'open the console and screenshot the Browser tab' } })
  return r.container
}

const picker = () => screen.queryByRole('radiogroup', { name: 'Browser tool' })
const tool = (name: RegExp) => within(picker()!).getByRole('radio', { name })

beforeEach(() => {
  api.loadProviderKeys.mockResolvedValue(keys('user'))
  api.loadJudgment.mockResolvedValue(ok({ tenant_id: 'eng', can_edit: true, jev: TYPESAFE_JEV }))
})

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the picker is drawn from the declared inputs', () => {
  it('reads `browser_tool` and its choices off the served contract, Auto first', async () => {
    const r = await capacity({ declare: true })
    if (r.status !== 'ok') throw new Error('unreachable')
    expect(declaredBrowserTools(r.data.runner_profiles['claude-code-browser'])).toEqual(TOOLS)
    // The control: a profile that declares nothing has no picker.
    expect(declaredBrowserTools(r.data.runner_profiles['claude-code'])).toBeNull()
  })

  it('draws Auto, Playwright, dev-browser and Jev for a declaring profile, Auto selected', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    await browserForm()
    const group = picker()
    expect(group, 'no picker for a profile that declares browser_tool').not.toBeNull()
    const radios = within(group!).getAllByRole('radio') as HTMLInputElement[]
    expect(radios.map((r) => r.value)).toEqual(TOOLS)
    expect(radios.find((r) => r.checked)?.value).toBe('auto')
  })

  it('draws no picker when the profile does not declare `browser_tool`', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: false }))
    await browserForm()
    expect(picker(), 'a picker invites a key the API refuses with 422').toBeNull()
  })

  it('gives each tool its one-line hint on hover and on focus', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    const hint = () => visible(c.querySelector('.btp-hint'))
    expect(hint()).toBe('the agent picks per step')
    fireEvent.focus(tool(/^Playwright/))
    expect(hint()).toBe('step-by-step browser tools')
    fireEvent.mouseEnter(tool(/^dev-browser/).closest('label')!)
    expect(hint()).toBe('fast scripted multi-step flows')
    fireEvent.mouseEnter(tool(/^Jev/).closest('label')!)
    expect(hint()).toBe('goal-driven clicks and reading long pages — sends page text to TypeSafe')
    // Each hint is also the radio's description, for a screen reader.
    expect(tool(/^Playwright/).getAttribute('aria-describedby')).toBeTruthy()
    expect(visible(document.getElementById(tool(/^Playwright/).getAttribute('aria-describedby')!))).toBe('step-by-step browser tools')
  })
})

describe('what the click sends', () => {
  it('Auto, the default, sends no browser_tool', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    const posts = capturePosts()
    fireEvent.submit(c.querySelector('form')!)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect(posts[0]!.runner_profile).toBe('claude-code-browser')
    expect(Object.keys(posts[0]!.input as object)).not.toContain('browser_tool')
  })

  it('a pick other than Auto is sent as input.browser_tool', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    fireEvent.click(tool(/^dev-browser/))
    expect(visible(c.querySelector('.sbf-send'))).toContain('browser_tool')
    const posts = capturePosts()
    fireEvent.submit(c.querySelector('form')!)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect((posts[0]!.input as Record<string, unknown>).browser_tool).toBe('dev-browser')
  })

  it('a runner change puts the pick back to Auto', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    fireEvent.click(tool(/^Playwright/))
    fireEvent.click(c.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    expect(picker()).toBeNull()
    fireEvent.click(c.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code-browser"]')!)
    expect((tool(/^Auto/) as HTMLInputElement).checked).toBe(true)
  })
})

describe('Jev and the TypeSafe key', () => {
  it('is disabled, with a link to Config › Secrets, when no key would be used', async () => {
    api.loadProviderKeys.mockResolvedValue(keys('none'))
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    await waitFor(() => expect((tool(/^Jev/) as HTMLInputElement).disabled).toBe(true), WAIT)
    const why = c.querySelector<HTMLElement>('.btp-jev-off')!
    expect(visible(why)).toBe('Add a TypeSafe key in Config › Secrets')
    expect(within(why).getByRole('link', { name: 'Config › Secrets' }).getAttribute('href')).toBe('/config/secrets')
    expect(api.loadProviderKeys).toHaveBeenCalledWith('typesafe')
  })

  it('is enabled when a key resolves, and picking it prints the data notice inline', async () => {
    api.loadProviderKeys.mockResolvedValue(keys('tenant'))
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    await waitFor(() => expect((tool(/^Jev/) as HTMLInputElement).disabled).toBe(false), WAIT)
    expect(c.querySelector('.btp-jev-off')).toBeNull()
    expect(c.querySelector('.btp-notice'), 'the notice before Jev is picked').toBeNull()
    fireEvent.click(tool(/^Jev/))
    const notice = c.querySelector<HTMLElement>('.btp-notice')!
    expect(visible(notice)).toBe('Jev sends the text of the pages it reads to TypeSafe, using the tenant key.')
    // Inline, not a modal.
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('stays disabled while the key read is unanswered or failed', () => {
    const { rerender, container } = render(
      <BrowserToolPicker tools={TOOLS} value="auto" onChange={() => {}} judgment={TYPESAFE} typesafe={{ kind: 'reading' }} egress={null} />,
    )
    expect((screen.getByRole('radio', { name: /^Jev/ }) as HTMLInputElement).disabled).toBe(true)
    expect(visible(container.querySelector('.btp-jev-off'))).toBe('Checking for a TypeSafe key…')
    rerender(<BrowserToolPicker tools={TOOLS} value="auto" onChange={() => {}} judgment={TYPESAFE} typesafe={{ kind: 'unread', detail: 'HTTP 503' }} egress={null} />)
    expect((screen.getByRole('radio', { name: /^Jev/ }) as HTMLInputElement).disabled).toBe(true)
    expect(visible(container.querySelector('.btp-jev-off'))).toContain('Whether a TypeSafe key exists could not be read')
  })
})

describe('the Network row', () => {
  it('reads Residential · US from the served egress, and Direct', async () => {
    expect(profileEgress({ egress: { mode: 'residential', country: 'us' } } as never)).toEqual({ mode: 'residential', country: 'us' })
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true, egress: { mode: 'residential', country: 'us' } }))
    const c = await browserForm()
    expect(visible(c.querySelector('.btp-net'))).toBe('Network Residential · US')
    document.body.innerHTML = ''
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true, egress: { mode: 'direct', country: null } }))
    const d = await browserForm()
    expect(visible(d.querySelector('.btp-net'))).toBe('Network Direct')
  })

  it('is hidden, not guessed, when the API serves no egress', async () => {
    api.loadCapacity.mockResolvedValue(await capacity({ declare: true }))
    const c = await browserForm()
    expect(picker()).not.toBeNull()
    expect(c.querySelector('.btp-net')).toBeNull()
    expect(profileEgress({ egress: { mode: 'satellite' } } as never)).toBeNull()
  })
})

describe('keyboard and phone width', () => {
  it('is a native radio group: arrows move the pick, and a disabled Jev is skipped by the browser', () => {
    const onChange = vi.fn()
    render(<BrowserToolPicker tools={TOOLS} value="auto" onChange={onChange} judgment={TYPESAFE} typesafe={{ kind: 'read', uses: 'user' }} egress={null} />)
    const radios = screen.getAllByRole('radio') as HTMLInputElement[]
    expect(new Set(radios.map((r) => r.name)).size, 'one name, so arrows move within the group').toBe(1)
    expect(radios.every((r) => r.type === 'radio')).toBe(true)
    fireEvent.click(radios[1]!)
    expect(onChange).toHaveBeenLastCalledWith('playwright')
  })

  it('puts the segments two to a row at 390px and in one row at 1440px', () => {
    const { container } = render(
      <BrowserToolPicker tools={TOOLS} value="auto" onChange={() => {}} judgment={TYPESAFE} typesafe={{ kind: 'read', uses: 'user' }} egress={{ mode: 'residential', country: 'us' }} />,
    )
    const seg = container.querySelector<HTMLElement>('.btp-seg')!
    const at = (width: number) => cascade(SUBMIT_CSS, seg, 'grid-template-columns', { width }).winner?.value
    expect(at(1440)).toBe('repeat(4, auto)')
    expect(at(390)).toBe('repeat(2, minmax(0, 1fr))')
    for (const theme of ['dark', 'light'] as const) {
      // Both themes: the checked segment is coloured from the theme's tokens, never a literal.
      const on = container.querySelector<HTMLElement>('.btp-opt.is-on')!
      const bg = cascade(SUBMIT_CSS, on, ['background', 'background-color'], { width: 1440, theme }).winner?.value ?? ''
      expect(bg).toMatch(/^var\(--/)
    }
  })
})
