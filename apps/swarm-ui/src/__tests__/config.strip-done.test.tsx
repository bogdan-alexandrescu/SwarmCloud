/**
 * THE SETUP STRIP, FINISHED AND AGED (CONFIG-STRIP-DONE; part of #2157, BGD's
 * pick of 2026-10-24, proposal E, docs/web-ui/mockups/config-e.html `stripD`).
 *
 *   * When every step is done the strip folds to ONE line, "Setup complete ·
 *     N of N · checked <age> · Show steps", on every width: no step list and
 *     no phone strip until Show steps, which is remembered per device. The
 *     fold's N of N is the steps' own count.
 *   * Every step line drawn from a read carries that read's age, from the
 *     onboarding read's `derived_at`. A read without one draws a dash with
 *     its reason, never "just now".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CONFIG_CSS from '../styles/config.css?raw'

type Json = Record<string, unknown>

const ME = (tenant = 'eng'): Json => ({
  tenant: { tenant_id: tenant, kind: tenant.startsWith('u-') ? 'user' : 'group', display_name: `${tenant}@example.com` },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
})
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const GROUP: Json = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const REPOS = ['acme/api', 'acme/web'].map((full_name) => ({ full_name }))
const SOURCES = { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } }
const SECRETS_NONE: Json = { needed: 0, done: true, items: [], sources: SOURCES }
const SECRETS_ONE: Json = {
  needed: 1,
  done: false,
  items: [{ kind: 'mcp_server', name: 'linear', needed_by: 'linear', where: 'config/secrets' }],
  sources: { ...SOURCES, mcp_servers: { needed: 1, reason: null } },
}
const PEOPLE_DONE: Json = { applies: true, done: true, holders: 5, admins: 1, pending: null, reason: null, pending_reason: null }
const PEOPLE_PERSONAL: Json = { applies: false, done: null, holders: null, admins: null, pending: null, reason: 'a personal workspace has nobody to invite', pending_reason: null }

/** The reading time, and the page's clock five minutes after it. */
const DERIVED = '2026-10-25T09:00:00Z'
const NOW = '2026-10-25T09:05:00Z'

/** An onboarding read; `derived` undefined drops `derived_at` from the read altogether. */
const onboarding = (opts: { secrets?: Json; people?: Json; tenant?: string; derived?: string | null } = {}): Json => {
  const doc: Json = {
    tenant_id: opts.tenant ?? 'eng',
    user: 'ana.b@example.com',
    user_hash: 'h',
    steps: [step('github_connected', 'done', { login: 'ana' }), step('repos_chosen', 'done', { repositories: REPOS })],
    next_step: null,
    complete: false,
    source: 'derived',
    config_setup: { secrets: opts.secrets ?? SECRETS_NONE, people: opts.people ?? PEOPLE_DONE },
  }
  if (!('derived' in opts)) doc.derived_at = DERIVED
  else if (opts.derived !== undefined) doc.derived_at = opts.derived
  return doc
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function serve(me: Json, mine: Json[], doc: Json) {
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(mine))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(me))
    if (url.includes('/v1/onboarding')) return Promise.resolve(json(doc))
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
  return mod
}

const strip = () => document.querySelector('section.cfg-strip') as HTMLElement | null
const fold = async () => {
  await waitFor(() => expect(document.querySelector('.cfg-done')).not.toBeNull())
  return document.querySelector('.cfg-done') as HTMLElement
}
const steps = () => screen.queryByRole('list', { name: 'Workspace setup steps' })

beforeEach(() => {
  // Only Date: the page's own timers (and testing-library's waits) run as ever.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('the finished strip folds to one line', () => {
  it('draws "Setup complete", the count and the read\'s age, and no step list until Show steps', async () => {
    serve(ME(), [PERSONAL, GROUP], onboarding())
    await app('/config/secrets')
    const line = await fold()
    // MUTATION: never fold -> no `.cfg-done` and `fold()` times out.
    expect(line.textContent).toContain('Setup complete')
    expect(line.textContent).toContain('4 of 4')
    expect(line.textContent).toContain('checked 5m ago')
    // MUTATION: draw the fold above the steps instead of in their place -> fails.
    expect(steps()).toBeNull()
    // The phone's strip folds the same way: one line, not the compact strip.
    // MUTATION: keep `.cfg-compact` under the fold -> a phone draws both, and this fails.
    expect(strip()!.querySelector('.cfg-compact')).toBeNull()
    const show = within(line).getByRole('button', { name: 'Show steps' })
    expect(show.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(show)
    const list = await screen.findByRole('list', { name: 'Workspace setup steps' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(4)
    expect(document.querySelector('.cfg-done')).toBeNull()
    // Folds again from where it unfolded.
    fireEvent.click(within(strip()!).getAllByRole('button', { name: 'Hide steps' })[0]!)
    await fold()
    expect(steps()).toBeNull()
  })

  it('remembers Show steps on this device', async () => {
    serve(ME(), [PERSONAL, GROUP], onboarding())
    await app('/config/github')
    fireEvent.click(within(await fold()).getByRole('button', { name: 'Show steps' }))
    await screen.findByRole('list', { name: 'Workspace setup steps' })
    cleanup()
    serve(ME(), [PERSONAL, GROUP], onboarding())
    await app('/config/repositories')
    // MUTATION: keep the choice in component state only -> the next page folds again and this fails.
    const list = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(within(list).getAllByRole('listitem').every((li) => li.className.includes('is-done'))).toBe(true))
    expect(document.querySelector('.cfg-done')).toBeNull()
  })

  it('still folds and unfolds when the browser refuses storage', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    serve(ME(), [PERSONAL, GROUP], onboarding())
    await app('/config/secrets')
    // MUTATION: read storage without try/catch -> the strip throws and the fold never draws.
    fireEvent.click(within(await fold()).getByRole('button', { name: 'Show steps' }))
    expect(await screen.findByRole('list', { name: 'Workspace setup steps' })).toBeTruthy()
  })

  it('counts N of N from the steps: three in a personal workspace', async () => {
    serve(ME('u-ana'), [], onboarding({ tenant: 'u-ana', people: PEOPLE_PERSONAL }))
    await app('/config/secrets')
    const line = await fold()
    // MUTATION: print a fixed "4 of 4" -> fails.
    await waitFor(() => expect(line.textContent).toContain('3 of 3'))
    vi.resetModules()
    const { setupSteps, setupCount } = await import('../ConfigSetup')
    const c = setupCount(setupSteps(onboarding({ tenant: 'u-ana', people: PEOPLE_PERSONAL }) as never, true))
    expect(line.textContent).toContain(`${c.done} of ${c.of}`)
  })

  it('does not fold while a step is left', async () => {
    serve(ME(), [PERSONAL, GROUP], onboarding({ secrets: SECRETS_ONE }))
    await app('/config/secrets')
    const list = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(document.querySelector('.cfg-count b')?.textContent).toBe('3 of 4 steps done'))
    // MUTATION: fold on `done >= of - 1` -> fails.
    expect(document.querySelector('.cfg-done')).toBeNull()
    expect(within(list).getAllByRole('listitem')).toHaveLength(4)
    expect(within(strip()!).queryByRole('button', { name: 'Hide steps' })).toBeNull()
  })
})

describe('each step line names its reading\'s age', () => {
  it('draws "checked <age>" from `derived_at` on every step line', async () => {
    serve(ME(), [PERSONAL, GROUP], onboarding({ secrets: SECRETS_ONE }))
    await app('/config/secrets')
    const list = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(within(list).getAllByRole('listitem')[0]!.textContent).toContain('@ana'))
    // MUTATION: draw only the evidence ("@ana") -> no age and this fails.
    const ages = within(list)
      .getAllByRole('listitem')
      .map((li) => li.querySelector('.cfg-step-age')?.textContent ?? '')
    expect(ages).toEqual(['checked 5m ago', 'checked 5m ago', 'checked 5m ago', 'checked 5m ago'])
    // The line itself is unchanged, so the panel's badges still name it word for word.
    expect(within(list).getAllByRole('listitem')[0]!.querySelector('small')?.textContent).toBe('@ana')
  })

  it('draws no age on a step the read did not measure', async () => {
    const unread: Json = { needed: null, done: null, items: [], sources: { ...SOURCES, mcp_servers: { needed: null, reason: 'the MCP registry could not be read' } } }
    serve(ME(), [PERSONAL, GROUP], onboarding({ secrets: unread }))
    await app('/config/secrets')
    const list = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(within(list).getAllByRole('listitem')[0]!.textContent).toContain('@ana'))
    const items = within(list).getAllByRole('listitem')
    // MUTATION: age every step line -> the unmeasured Secrets step reads "checked 5m ago" and this fails.
    expect(items.map((li) => li.querySelector('.cfg-step-age')?.textContent ?? null)).toEqual(['checked 5m ago', 'checked 5m ago', null, 'checked 5m ago'])
  })

  it('draws a dash with its reason where the read carried no `derived_at`, never "just now"', async () => {
    serve(ME(), [PERSONAL, GROUP], onboarding({ secrets: SECRETS_ONE, derived: undefined }))
    await app('/config/secrets')
    const list = await screen.findByRole('list', { name: 'Workspace setup steps' })
    await waitFor(() => expect(within(list).getAllByRole('listitem')[0]!.textContent).toContain('@ana'))
    for (const li of within(list).getAllByRole('listitem')) {
      const age = li.querySelector('.cfg-step-age')
      // MUTATION: fall back to the render's clock -> "checked just now" (or "0s ago") and this fails.
      expect(age?.textContent).toBe('checked —')
      expect(within(age as HTMLElement).getByRole('img').getAttribute('aria-label')).toBe('the setup read carried no time')
      expect(li.textContent).not.toContain('just now')
    }
  })

  it('draws a dash on the fold too, and one for a time it cannot read', async () => {
    serve(ME(), [PERSONAL, GROUP], onboarding({ derived: 'yesterday-ish' }))
    await app('/config/secrets')
    const line = await fold()
    // MUTATION: pass an unparseable time to timeAgo -> "at an unknown time" with no reason, and this fails.
    await waitFor(() => expect(within(line).getByRole('img', { name: "the setup read's time could not be read" })).toBeTruthy())
    expect(line.textContent).not.toContain('just now')
  })
})

describe('readingAge', () => {
  it('ages a time, and names why there is none', async () => {
    vi.resetModules()
    const { readingAge } = await import('../ConfigSetup')
    const now = Date.parse(NOW)
    expect(readingAge(DERIVED, now)).toEqual({ age: 'checked 5m ago', why: null })
    // Under five seconds is still a number, never "just now".
    expect(readingAge(new Date(now - 2000).toISOString(), now)).toEqual({ age: 'checked 2s ago', why: null })
    expect(readingAge(undefined, now)).toEqual({ age: null, why: 'the setup read carried no time' })
    expect(readingAge('', now)).toEqual({ age: null, why: 'the setup read carried no time' })
    expect(readingAge('nonsense', now)).toEqual({ age: null, why: "the setup read's time could not be read" })
  })
})

/** The declarations of the one rule in config.css whose selector is exactly `selector`. */
function rule(selector: string): string {
  const flat = CONFIG_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  const at = flat.split('}').find((r) => (r.split('{')[0] ?? '').trim() === selector)
  if (at === undefined) throw new Error(`no ${selector} rule in config.css`)
  return (at.split('{')[1] ?? '').replace(/\s+/g, ' ')
}

/** The declarations of `selector`'s rule inside config.css's phone block, `@media (max-width: 759px)`. */
function phoneRule(selector: string): string {
  const flat = CONFIG_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  const open = flat.indexOf('@media (max-width: 759px) {')
  if (open < 0) throw new Error('no phone block in config.css')
  let depth = 0
  let end = open
  for (let i = flat.indexOf('{', open); i < flat.length; i += 1) {
    if (flat[i] === '{') depth += 1
    if (flat[i] === '}') depth -= 1
    if (depth === 0) {
      end = i
      break
    }
  }
  const block = flat.slice(flat.indexOf('{', open) + 1, end)
  const at = block.split('}').find((r) => (r.split('{')[0] ?? '').trim() === selector)
  if (at === undefined) throw new Error(`no ${selector} rule in config.css's phone block`)
  return (at.split('{')[1] ?? '').replace(/\s+/g, ' ')
}

/** The pixels of `name` (a `gap` or the horizontal half of a `padding`) in a rule's declarations. */
function px(decls: string, name: 'gap' | 'padding-x'): number {
  const prop = name === 'gap' ? 'gap' : 'padding'
  const m = new RegExp(`(?:^|;)\\s*${prop}:\\s*([^;]+)`).exec(decls)
  if (m === null) throw new Error(`no ${prop} in ${decls}`)
  const parts = (m[1] ?? '').trim().split(/\s+/).map((v) => parseFloat(v))
  if (name === 'gap') return parts[parts.length - 1] ?? NaN
  return parts[1] ?? parts[0] ?? NaN
}

describe('the fold stays one line on a phone', () => {
  // jsdom lays nothing out, so the review's phone-390 render (Show steps
  // wrapped under the count, a 71px strip) is held by the rules that caused it.
  it('never wraps the fold, and lets the count give way first', () => {
    // MUTATION: put `flex-wrap: wrap` back on `.cfg-done` and this fails.
    expect(rule('.cfg-done')).toContain('flex-wrap: nowrap')
    const small = rule('.cfg-done small')
    expect(small).toContain('min-width: 0')
    expect(small).toContain('text-overflow: ellipsis')
    expect(small).toContain('white-space: nowrap')
    expect(rule('.cfg-done .ctl-link')).toContain('white-space: nowrap')
  })

  it("leaves the count its whole 'checked <age> ago' at phone-390, a two- or three-character age", () => {
    // The re-review's phone-390 render measured the fold's <small> at 124px
    // with `gap: 10px; padding: 8px 14px`, and '4 of 4 · checked 2m ago' cut
    // by its own ellipsis ('checked 2m a…'): it needs 125px, and a 3-character
    // age ('12m', '23h') 129px. The fold has four boxes (tick, words, count,
    // Show steps), so three gaps and two side paddings come out of the count.
    // MUTATION: drop the phone `.cfg-done` rule (back to 10px and 14px) and
    // the count has 124px, under 129, and this fails.
    const phone = phoneRule('.cfg-done')
    const gap = px(phone, 'gap')
    const side = px(phone, 'padding-x')
    const small = 124 + 3 * (10 - gap) + 2 * (14 - side)
    expect(small).toBeGreaterThanOrEqual(129)
    // The row still never wraps, and the ellipsis stays the last resort.
    expect(rule('.cfg-done')).toContain('flex-wrap: nowrap')
    expect(rule('.cfg-done small')).toContain('text-overflow: ellipsis')
  })

  it("draws the strip's buttons as plain links, as config-e.html's <a>", () => {
    // MUTATION: drop the reset and the global button box (1px border, 5px 12px
    // padding) comes back, 88px wide on the fold.
    const reset = rule('.cfg-strip button.ctl-link')
    expect(reset).toContain('border: 0')
    expect(reset).toContain('padding: 0')
    expect(reset).toContain('background: none')
  })
})
