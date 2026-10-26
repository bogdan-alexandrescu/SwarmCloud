/**
 * THE PHONE BAR READS A LONG PAGE NAME WHOLE (lane PHONE-BAR-LABEL, part of
 * #2391; BGD's decision of 2026-10-26 in ADMIN-PEOPLE-ROLES, "Chrome lane").
 *
 * At 390 the bar's page name was one `nowrap` line cut with an ellipsis:
 * `People and roles` read `People an…`, and `Pe…` beside the admin's
 * workspace chip. The name now wraps at a space onto a second line inside
 * the bar's own height (44px, 36px once compacted), never mid-word, and
 * takes the room the other controls leave rather than squeezing them.
 *
 * jsdom lays nothing out, so the fit is a model read off the shipped cascade
 * (tablefit.ts's conservative text widths): the bar's 390px, less its
 * padding, its gaps and every other item in it, is the width the name has;
 * the name's words are set greedily into lines of that width.
 *
 * Each `it` names the mutation that turns it red.
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { build, painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'
import { lengthPx, paddingX, textPx } from './tablefit'

const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const

/** The longest page names in App.tsx's SECTIONS, and the two the decision named. */
const LONG_LABELS = [
  'Submit from a GitHub issue',
  'Workspaces you admin',
  'Workspace Settings',
  'Submit a workflow',
  'By runner profile',
  'People and roles',
  'Platform secrets',
  'Profile headroom',
  'Provider quota',
] as const

const hosts: HTMLElement[] = []
const realFetch = globalThis.fetch
afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  try {
    window.localStorage.clear()
    window.sessionStorage.clear()
  } catch {
    /* storage refused; nothing kept */
  }
  window.history.replaceState(null, '', '/')
})

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

const ENG = { tenant_id: 'eng', display_name: 'Engineering', kind: 'group' }
const RESEARCH = { tenant_id: 'research', display_name: 'Research', kind: 'group' }
const OWN = { tenant_id: 'u-ana', display_name: 'Ana B', kind: 'user' }

/**
 * Whose bar: an admin in a group workspace draws the chip with its key
 * (`eng`); an admin in their own workspace, the chip with the longest thing
 * it ever holds (`Your workspace`, OWN_WORKSPACE_LABEL); a member with one
 * workspace, the plain label.
 */
type Who = 'admin in eng' | 'admin in their own workspace' | 'member'
const WHO: readonly Who[] = ['admin in eng', 'admin in their own workspace', 'member']

function liveApi(who: Who) {
  const admin = who !== 'member'
  const current = who === 'admin in their own workspace' ? OWN : ENG
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return ok(who === 'member' ? [ENG] : who === 'admin in eng' ? [ENG, RESEARCH] : [OWN, ENG])
    if (url.includes('/v1/tenants/me')) {
      return ok({
        tenant: current,
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: admin },
        environment: 'dev',
        environment_declared: true,
      })
    }
    return ok({ pools: [] })
  }) as unknown as typeof fetch
}

async function bar(who: Who): Promise<{ header: HTMLElement; retitle: (title: string) => void }> {
  const admin = who !== 'member'
  vi.stubEnv('VITE_LIVE', '1')
  vi.stubEnv('VITE_SWARM_ENV', 'dev')
  vi.resetModules()
  liveApi(who)
  const { SkyShell } = await import('../Spine')
  window.history.replaceState(null, '', '/config/people')
  const shell = (title: string) => (
    <SkyShell section="config" tab="people" title={title} go={() => {}} foot={null}>
      <p>page</p>
    </SkyShell>
  )
  const { container, rerender } = render(shell('People and roles'))
  const sel = admin ? '.sk-pbar .sk-tchip' : '.sk-pbar .sk-tn'
  const key = who === 'admin in their own workspace' ? 'Your workspace' : 'eng'
  await waitFor(() => expect(container.querySelector(sel)?.textContent).toBe(key), WAIT)
  return { header: container.querySelector<HTMLElement>('.sk-pbar')!, retitle: (title) => rerender(shell(title)) }
}

const px = (v: string | null, basis = 0): number => {
  const n = lengthPx(v, basis)
  if (n === null) throw new Error(`no length in ${JSON.stringify(v)}`)
  return n
}

/** A floor said in `em`, in px at the element's own font size. */
function floorPx(el: Element): number {
  const v = painted(el, 'min-width', PHONE)
  const m = v === null ? null : /^([\d.]+)em$/.exec(v.trim())
  if (m !== null) return Number(m[1]) * px(painted(el, 'font-size', PHONE))
  return lengthPx(v, 0) ?? 0
}

/** Border-left plus border-right, read from the `border` shorthand or its width. */
function bordersX(el: Element): number {
  const w = painted(el, 'border-width', PHONE)
  if (w !== null) return 2 * px(w.split(/\s+/)[0] ?? null)
  const b = painted(el, 'border', PHONE)
  if (b === null || b === '0' || b === 'none') return 0
  const m = /([\d.]+)px/.exec(b)
  return m === null ? 0 : 2 * Number(m[1])
}

/** The conservative width of one item in the bar other than the page name. */
function itemPx(el: HTMLElement): number {
  const [l, r] = paddingX(el, PHONE)
  const box = l + r + bordersX(el)
  if (el.tagName.toLowerCase() === 'svg') return Number(el.getAttribute('width'))
  if (el.classList.contains('sk-ibtn')) return box + px(painted(el.querySelector('.sk-ic')!, 'width', PHONE))
  if (el.classList.contains('sk-tchip')) {
    const name = el.querySelector<HTMLElement>('.sk-tcn')!
    return box + textPx(name.textContent ?? '', name, PHONE) + px(painted(el, 'gap', PHONE)) + px(painted(el.querySelector('.sk-ic')!, 'width', PHONE))
  }
  if (el.classList.contains('sk-top')) return box + textPx('↑', el, PHONE)
  // The workspace label and the environment pill: text in a box.
  return box + textPx(el.textContent ?? '', el, PHONE)
}

/**
 * The width the bar leaves its page name at 390, with the bell the live bar
 * carries and every other item at its whole width. Below the name's floor the
 * chip or label gives way (`flex-shrink: 100`, `min-width: 0`) and the name
 * holds its floor; `chipPx` is how much the workspace has to give.
 */
function roomFor(header: HTMLElement): { room: number; chipPx: number } {
  const items = [...header.children].filter((c) => c.tagName.toLowerCase() !== 'b') as HTMLElement[]
  // jsdom never matches the phone query, so the bell is not in this bar; the
  // live one is. Its slot, from the cascade, is budgeted all the same.
  const bell = build('div.sk-app > header.sk-pbar > span.sk-pbell > button.nt-bell', hosts)
  const others = items.reduce((sum, el) => sum + itemPx(el), 0) + px(painted(bell, 'width', PHONE))
  const [l, r] = paddingX(header, PHONE)
  const gaps = (items.length + 1) * px(painted(header, 'gap', PHONE))
  const workspace = items.find((el) => el.classList.contains('sk-tchip') || el.classList.contains('sk-tn'))
  return { room: 390 - l - r - gaps - others, chipPx: workspace === undefined ? 0 : itemPx(workspace) }
}

/**
 * The name's words set greedily into lines `room` wide; a word wider than the
 * line is reported. `perChar` is tablefit's conservative advance for the
 * title's face and size (`textPx` of one character), read once.
 */
function lines(text: string, perChar: number, room: number): string[] {
  const width = (t: string) => [...t].length * perChar
  const out: string[] = []
  for (const word of text.split(' ')) {
    expect(width(word), `"${word}" alone is wider than the ${room.toFixed(0)}px the bar leaves`).toBeLessThanOrEqual(room)
    const last = out.length - 1
    if (last >= 0 && width(`${out[last]} ${word}`) <= room) out[last] = `${out[last]} ${word}`
    else out.push(word)
  }
  return out
}

describe('PHONE-BAR-LABEL: a long page name reads whole at 390', () => {
  it('lets the name wrap at a space, never mid-word, and draws no ellipsis', () => {
    // MUTATION: `white-space: nowrap` / `text-overflow: ellipsis` back on
    // `.sk-pbar b`, or `overflow-wrap: anywhere` / `word-break: break-all` on it.
    const title = build('div.sk-app > header.sk-pbar.has-chip > b', hosts)
    expect(painted(title, 'white-space', PHONE)).toBe('normal')
    expect(painted(title, 'text-overflow', PHONE)).not.toBe('ellipsis')
    expect(painted(title, 'overflow-wrap', PHONE) ?? 'normal').toBe('normal')
    expect(painted(title, 'word-break', PHONE) ?? 'normal').toBe('normal')
    expect(painted(title, '-webkit-line-clamp', PHONE)).toBe('2')
  })

  it('holds two lines of the name inside the bar, at 44px and compacted to 36px', () => {
    // MUTATION: drop the title's `line-height`, or grow its font a step, so
    // two lines no longer fit the compacted bar and the bar would have to grow.
    const title = build('div.sk-app.is-scrolled > header.sk-pbar > b', hosts)
    const compact = px(painted(title.parentElement!, 'height', PHONE))
    expect(compact).toBe(36)
    const atLoad = px(painted(build('div.sk-app > header.sk-pbar', hosts), 'height', PHONE))
    expect(atLoad).toBe(44)
    const size = px(painted(title, 'font-size', PHONE))
    const said = painted(title, 'line-height', PHONE)
    expect(said, 'the title states no line-height of its own').not.toBeNull()
    const lh = Number(resolveVars(said!, TABLES.dark))
    expect(Number.isFinite(lh) && lh > 0, 'the title states a unitless line-height').toBe(true)
    expect(2 * size * lh, 'two lines of the name are taller than the compacted bar').toBeLessThanOrEqual(compact)
  })

  it('gives the name the room the controls leave, not its own whole width', () => {
    // MUTATION: `flex: 1 1 auto` back on `.sk-pbar b`: its basis is then the
    // whole one-line name, and the workspace chip (`flex-shrink: 100`) is the
    // one squeezed to make room for it.
    const title = build('div.sk-app > header.sk-pbar.has-chip > b', hosts)
    expect(painted(title, 'flex', PHONE)).toBe('1 1 0%')
    const plain = build('div.sk-app > header.sk-pbar > b', hosts)
    expect(painted(plain, 'flex', PHONE)).toBe('1 1 0%')
    expect(painted(plain, 'min-width', PHONE)).toBe('5.5em')
  })

  it("holds beside the admin's chip a floor two whole lines of the longest name fit, one size step down", () => {
    // MUTATION: drop `.sk-pbar.has-chip b`'s size step or its 8em floor: the
    // name falls back to 77px beside `Your workspace`, where `Submit from a
    // GitHub issue` needs four lines and `Workspaces` is wider than the box.
    const title = build('div.sk-app > header.sk-pbar.has-chip > b', hosts)
    const plain = build('div.sk-app > header.sk-pbar > b', hosts)
    expect(painted(title, 'font-size', PHONE)).toBe('var(--t-meta)')
    expect(painted(plain, 'font-size', PHONE)).toBe('var(--t-body)')
    const perChar = textPx('x', title, PHONE)
    const floor = floorPx(title)
    expect(floor).toBeGreaterThan(floorPx(plain))
    for (const label of LONG_LABELS) expect(lines(label, perChar, floor).length, `"${label}" in ${floor}px`).toBeLessThanOrEqual(2)
  })

  for (const who of WHO) {
    it(`sets every long page name in at most two whole lines: ${who}`, async () => {
      // MUTATION: shrink the room (a wider gap or padding in the bar), or
      // grow the title's font, or lower its floor beside the chip, until a
      // name needs a third line or a word is wider than its box.
      let visited = 0
      const { header, retitle } = await bar(who)
      expect(header.classList.contains('has-chip')).toBe(who !== 'member')
      // The room is the bar's less every item but the name, so one reading
      // serves every name; so does the title's face, size, wrap and clamp.
      const title = [...header.children].find((c) => c.tagName.toLowerCase() === 'b') as HTMLElement
      const { room: left, chipPx } = roomFor(header)
      const floor = floorPx(title)
      // Below its floor the name holds it and the workspace gives the rest,
      // never more than its own width: the row stays inside the glass.
      expect(left + chipPx, 'the workspace cannot give the name its floor').toBeGreaterThanOrEqual(floor)
      const room = Math.max(left, floor)
      // A `nowrap` name has one line; a wrapping one as many as its clamp.
      const most = painted(title, 'white-space', PHONE) === 'nowrap' ? 1 : Number(painted(title, '-webkit-line-clamp', PHONE) ?? 1)
      expect(most).toBeLessThanOrEqual(2)
      const perChar = textPx('x', title, PHONE)
      for (const label of LONG_LABELS) {
        retitle(label)
        expect(title.textContent).toBe(label)
        const set = lines(label, perChar, room)
        expect(set.length, `"${label}" needs ${set.length} lines in ${room.toFixed(0)}px, and the bar draws ${most}: ${JSON.stringify(set)}`).toBeLessThanOrEqual(most)
        expect(set.join(' ')).toBe(label)
        visited++
      }
      expect(visited).toBe(LONG_LABELS.length)
    }, 15000)
  }

  it('leaves the desktop unchanged: no phone bar from 760px up', () => {
    // MUTATION: take the phone bar's rules out of the `max-width: 759px` query.
    const header = build('div.sk-app > header.sk-pbar', hosts)
    expect(painted(header, 'display', WIDE)).toBe('none')
  })
})
