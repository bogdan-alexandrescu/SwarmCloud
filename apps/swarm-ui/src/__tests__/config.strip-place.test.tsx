/**
 * THE SETUP STRIP SITS UNDER THE TITLE AND TABS (VQ-040; part of #2391,
 * docs/web-ui/mockups/config-e.html `desk()`: head, then the strip, then the
 * page's body). `ConfigPage` mounts the strip once, before the page, and
 * config.css's wider-screen block dissolves the page's own box into the
 * Config column and orders its head and tab strip ahead of the strip.
 *
 * jsdom draws no layout, so this holds the two halves that layout rests on:
 *
 *   * the CSS: from 760px up, the head and the tabs are ordered before the
 *     strip and the page's box is `display: contents`; the phone keeps the
 *     compact strip over the head (S2d);
 *   * every Config page: its box is one of the boxes the rule dissolves, and
 *     its head and tab strip are that box's own children. A page drawn in a
 *     box the rule does not name puts the strip back over its title.
 *
 * MUTATIONS, each of which turns a case red: drop the ordering rule; order
 * the strip before the head; wrap a page's head in another element.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import CONFIG_CSS from '../styles/config.css?raw'

type Json = Record<string, unknown>

const ME: Json = {
  tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: true },
  environment: 'dev',
  environment_declared: true,
}
const MINE: Json[] = [
  { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } },
  { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } },
]
const ONBOARDING: Json = {
  tenant_id: 'eng',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: '2026-10-25T09:00:00Z',
  steps: [
    { step: 'github_connected', state: 'done', code: null, copy: null, checked_at: null, evidence: { login: 'ana' }, issues: [] },
    { step: 'repos_chosen', state: 'todo', code: null, copy: null, checked_at: null, evidence: {}, issues: [] },
  ],
  next_step: null,
  complete: false,
  source: 'derived',
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function serve() {
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(MINE))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(ME))
    if (url.includes('/v1/onboarding')) return Promise.resolve(json(ONBOARDING))
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

/** The body of config.css's `@media (min-width: 760px)` block, comments dropped. */
function wideBlock(): string {
  const flat = CONFIG_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  const open = flat.indexOf('@media (min-width: 760px) {')
  if (open < 0) throw new Error('no @media (min-width: 760px) block in config.css')
  let depth = 0
  for (let i = flat.indexOf('{', open); i < flat.length; i += 1) {
    if (flat[i] === '{') depth += 1
    if (flat[i] === '}') depth -= 1
    if (depth === 0) return flat.slice(flat.indexOf('{', open) + 1, i)
  }
  throw new Error('the 760px block in config.css does not close')
}

/** The wide block's rules, selector to declarations, whitespace folded. */
function wideRules(): Map<string, string> {
  const out = new Map<string, string>()
  for (const r of wideBlock().split('}')) {
    const [sel, decls] = r.split('{')
    if (sel === undefined || decls === undefined) continue
    out.set(sel.replace(/\s+/g, ' ').trim(), decls.replace(/\s+/g, ' ').trim())
  }
  return out
}

/** The page boxes the rule dissolves: the classes in `.cfg-page > :is(...)`. */
function dissolved(): string[] {
  const sel = [...wideRules().entries()].find(([, d]) => /display:\s*contents/.test(d))?.[0]
  const m = sel === undefined ? null : /^\.cfg-page > :is\(([^)]+)\)$/.exec(sel)
  if (m === null) throw new Error(`no ".cfg-page > :is(...) { display: contents }" rule in the 760px block (found ${sel ?? 'none'})`)
  return m[1]!.split(',').map((c) => c.trim().replace(/^\./, ''))
}

function order(selector: string): number {
  const d = wideRules().get(selector)
  const m = d === undefined ? null : /order:\s*(-?\d+)/.exec(d)
  if (m === null) throw new Error(`no order on ${selector} in the 760px block`)
  return Number(m[1])
}

describe('config.css: the head and the tabs, then the strip (VQ-040)', () => {
  it('orders the page head and its tabs ahead of the strip from 760px up, the page box dissolved into the column', () => {
    const boxes = dissolved()
    expect(boxes).toEqual(expect.arrayContaining(['pk-page', 'ur-page', 'ws-page']))
    const is = `:is(${boxes.map((c) => `.${c}`).join(', ')})`
    const head = order(`.cfg-page > ${is} > .c-phead`)
    const tabs = order(`.cfg-page > ${is} > .cfg-parts`)
    const strip = order('.cfg-page > .cfg-strip')
    // MUTATION: order the strip first, or the tabs under it -> fails.
    expect(head).toBeLessThan(tabs)
    expect(tabs).toBeLessThan(strip)
    // Everything else on the page keeps order 0, so it follows the strip.
    expect(strip).toBeLessThan(0)
  })

  it('keeps the phone\'s compact strip over the head: the rule is the wider screens\' alone', () => {
    const flat = CONFIG_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
    const outside = flat.replace(`@media (min-width: 760px) {${wideBlock()}}`, '')
    // MUTATION: move the ordering out of the media block -> the phone's strip drops under its head and this fails.
    expect(outside).not.toMatch(/display:\s*contents/)
    expect(outside).not.toMatch(/\.cfg-strip\s*\{[^}]*(?:^|[{;\s])order:/)
  })
})

const PAGES: readonly { path: string; title: string; tabs: boolean }[] = [
  { path: '/config/github', title: 'GitHub access', tabs: true },
  { path: '/config/repositories', title: 'Repositories', tabs: true },
  { path: '/config/repositories?page=qa-targets', title: 'QA targets', tabs: true },
  { path: '/config/secrets', title: 'Secrets', tabs: true },
  { path: '/config/mcp-servers', title: 'MCP servers', tabs: false },
  { path: '/config/people', title: 'People and roles', tabs: false },
  { path: '/config/workspace', title: 'Workspace Settings', tabs: true },
]

describe('every Config page: its head and tabs are its own box\'s children, in a box the rule dissolves (VQ-040)', () => {
  for (const page of PAGES) {
    it(`${page.path}: the head${page.tabs ? ' and the tabs' : ''} can be ordered over the strip`, async () => {
      serve()
      await app(page.path)
      const h1 = await screen.findByRole('heading', { level: 1, name: page.title })
      const head = h1.closest('.c-phead')
      expect(head, 'the title is not in a page head').not.toBeNull()
      const box = head!.parentElement!
      // MUTATION: wrap the head in another element, or draw the page in a box config.css does not name -> fails.
      expect(box.parentElement?.classList.contains('cfg-page'), `${page.title}'s head is not its page box's own child`).toBe(true)
      expect(dissolved().some((c) => box.classList.contains(c)), `${page.title}'s box (${box.className}) is not one the rule dissolves`).toBe(true)
      expect(box.parentElement!.querySelector(':scope > .cfg-strip'), 'the strip is not the Config column\'s own child').not.toBeNull()
      if (page.tabs) expect(box.querySelector(':scope > .cfg-parts'), `${page.title}'s tabs are not its box's own child`).not.toBeNull()
    })
  }
})
