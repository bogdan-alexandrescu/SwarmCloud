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
 * PHONE-BAR-CHIP (the PHONE-BAR-LABEL review, part of #2391): an 8em floor
 * beside the chip took 27px from it, and the admin's own chip read `Your …`
 * on every page. BGD's rule (2026-10-26, "Room left, then wrap"): the chip,
 * the env pill, the bell and the arrow keep their widths; the name takes the
 * room left and wraps at a space; no word is cut. tablefit's 0.56em is an
 * upper bound made for "if the model says it fits, it fits"; beside the
 * chip, where the room is ~80px, it cannot tell two lines from four. The
 * name and the chip's text are therefore measured with Inter Tight's own
 * advances (INTER below); every other item keeps the conservative bound,
 * which can only shrink the room.
 *
 * The PHONE-BAR-CHIP render review then measured the bar at 390 (fixture
 * build, admin in their own workspace): menu 26, mark 22, chip 119, env
 * pill 39, bell 36, `Top` 29, which left the name 79px, and `Submit from a`
 * (82.4px) could not set its first line, so the clamp lost `issue`. This
 * model had said 91px: it read the pill as `Dev` where the build served
 * `Local`, and `↑` at 0.56em where Inter Tight's arrow is 0.912em. The pill
 * and the arrow are measured now too, for every label the bar is drawn with
 * (ENVS), and RENDERED holds every item's model at or above the render's.
 *
 * Each `it` names the mutation that turns it red.
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { build, painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'
import { fontPx, lengthPx, paddingX, textPx } from './tablefit'

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

/**
 * Inter Tight's advance widths in em, rounded up, read from the shipped
 * `@fontsource-variable/inter-tight` latin file with fontTools
 * (`instancer.instantiateVariableFont(font, {'wght': w})`, `hmtx` over a
 * 2048 unitsPerEm): 650 is the bar's page name (`.sk-pbar b`), 500 the
 * chip (`.sk-tchip`) and `Top`'s arrow. The env pill is 600 and is measured
 * at 650: Inter only widens with weight, so that is an upper bound. Kerning
 * is left out; Inter's pairs only tighten.
 */
const INTER: Record<500 | 650, Record<string, number>> = {
  500: { '↑': 0.92, a: 0.529, b: 0.574, c: 0.528, d: 0.574, e: 0.543, f: 0.328, g: 0.575, h: 0.562, i: 0.208, j: 0.208, k: 0.515, l: 0.208, m: 0.843, n: 0.557, o: 0.561, p: 0.574, q: 0.574, r: 0.343, s: 0.494, t: 0.33, u: 0.553, v: 0.525, w: 0.783, x: 0.51, y: 0.525, z: 0.51, A: 0.658, B: 0.612, C: 0.694, D: 0.68, E: 0.561, F: 0.544, G: 0.708, H: 0.7, I: 0.228, J: 0.509, K: 0.623, L: 0.522, M: 0.859, N: 0.706, O: 0.727, P: 0.597, Q: 0.727, R: 0.604, S: 0.602, T: 0.609, U: 0.696, V: 0.658, W: 0.941, X: 0.624, Y: 0.643, Z: 0.597, ' ': 0.223 },
  650: { a: 0.546, b: 0.592, c: 0.548, d: 0.592, e: 0.559, f: 0.349, g: 0.594, h: 0.586, i: 0.232, j: 0.232, k: 0.541, l: 0.232, m: 0.872, n: 0.583, o: 0.576, p: 0.592, q: 0.592, r: 0.368, s: 0.521, t: 0.35, u: 0.581, v: 0.546, w: 0.809, x: 0.534, y: 0.546, z: 0.532, A: 0.701, B: 0.625, C: 0.713, D: 0.691, E: 0.575, F: 0.551, G: 0.723, H: 0.71, I: 0.243, J: 0.53, K: 0.649, L: 0.532, M: 0.878, N: 0.704, O: 0.744, P: 0.611, Q: 0.745, R: 0.619, S: 0.617, T: 0.629, U: 0.697, V: 0.701, W: 0.988, X: 0.667, Y: 0.68, Z: 0.625, ' ': 0.206 },
}

/** `text` in Inter Tight at `weight`, in px at the element's own font size. */
function interPx(text: string, weight: 500 | 650, el: Element): number {
  const size = fontPx(el, PHONE)
  return [...text].reduce((sum, c) => {
    const em = INTER[weight][c]
    if (em === undefined) throw new Error(`no measured advance for ${JSON.stringify(c)}`)
    return sum + em * size
  }, 0)
}

/** The widest single word of a name, in px: the floor below which a word is cut. */
const widestWord = (text: string, el: Element): number => Math.max(...text.split(' ').map((w) => interPx(w, 650, el)))

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

/**
 * The environments the bar's pill names: `Dev` on the live deployment, and
 * `Local`, the widest, which the render review's fixture build served.
 */
const ENVS = ['dev', 'local'] as const
type Env = (typeof ENVS)[number]

/**
 * The render review's widths at 390 (PHONE-BAR-CHIP, admin in their own
 * workspace, `Local`), by the item's class; the bell's slot is the `nt-bell`.
 */
const RENDERED: Record<string, number> = { 'sk-ibtn': 26, svg: 22, 'sk-tchip': 119, 'sk-pill': 39, 'nt-bell': 36, 'sk-top': 29 }

function liveApi(who: Who, env: Env) {
  const admin = who !== 'member'
  const current = who === 'admin in their own workspace' ? OWN : ENG
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return ok(who === 'member' ? [ENG] : who === 'admin in eng' ? [ENG, RESEARCH] : [OWN, ENG])
    if (url.includes('/v1/tenants/me')) {
      return ok({
        tenant: current,
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: admin },
        environment: env,
        environment_declared: true,
      })
    }
    return ok({ pools: [] })
  }) as unknown as typeof fetch
}

async function bar(who: Who, env: Env): Promise<{ header: HTMLElement; retitle: (title: string) => void }> {
  const admin = who !== 'member'
  vi.stubEnv('VITE_LIVE', '1')
  vi.stubEnv('VITE_SWARM_ENV', env)
  vi.resetModules()
  liveApi(who, env)
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
  // The widest word: measured per name by `widestWord`, not a length here.
  if (v !== null && v.trim() === 'min-content') return 0
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
    // Measured, as the name beside it is: the chip's whole one-line width.
    return box + interPx(name.textContent ?? '', 500, el) + px(painted(el, 'gap', PHONE)) + px(painted(el.querySelector('.sk-ic')!, 'width', PHONE))
  }
  // Measured: the arrow, and the pill's label at 650 (an upper bound for its 600).
  if (el.classList.contains('sk-top')) return box + interPx(el.textContent ?? '', 500, el)
  if (el.classList.contains('sk-pill')) return box + interPx(el.textContent ?? '', 650, el)
  // The workspace label and the environment pill: text in a box.
  return box + textPx(el.textContent ?? '', el, PHONE)
}

/**
 * The width the bar leaves its page name at 390, with the bell the live bar
 * carries and every other item, the workspace chip or label included, at
 * its whole width. `chipPx` is the workspace's own width.
 */
function roomFor(header: HTMLElement): { room: number; chipPx: number; widths: Record<string, number> } {
  const items = [...header.children].filter((c) => c.tagName.toLowerCase() !== 'b') as HTMLElement[]
  // jsdom never matches the phone query, so the bell is not in this bar; the
  // live one is. Its slot, from the cascade, is budgeted all the same.
  const bell = build('div.sk-app > header.sk-pbar > span.sk-pbell > button.nt-bell', hosts)
  const widths: Record<string, number> = { 'nt-bell': px(painted(bell, 'width', PHONE)) }
  for (const el of items) widths[el.tagName.toLowerCase() === 'svg' ? 'svg' : el.classList[0]!] = itemPx(el)
  const others = Object.values(widths).reduce((sum, w) => sum + w, 0)
  const [l, r] = paddingX(header, PHONE)
  const gaps = (items.length + 1) * px(painted(header, 'gap', PHONE))
  const workspace = items.find((el) => el.classList.contains('sk-tchip') || el.classList.contains('sk-tn'))
  return { room: 390 - l - r - gaps - others, chipPx: workspace === undefined ? 0 : itemPx(workspace), widths }
}

/**
 * The name's words set greedily into lines `room` wide; a word wider than the
 * line is reported. `width` is the title's text width at its face and size.
 */
function lines(text: string, width: (t: string) => number, room: number): string[] {
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

  it("floors the name beside the admin's chip at its widest word, one size step down, not at a fixed width", () => {
    // MUTATION: an `em` floor back on `.sk-pbar.has-chip b` (the 8em of the
    // PHONE-BAR-LABEL fix took 27px from `Your workspace`), or drop the size step.
    const title = build('div.sk-app > header.sk-pbar.has-chip > b', hosts)
    const plain = build('div.sk-app > header.sk-pbar > b', hosts)
    expect(painted(title, 'font-size', PHONE)).toBe('var(--t-meta)')
    expect(painted(plain, 'font-size', PHONE)).toBe('var(--t-body)')
    expect(painted(title, 'min-width', PHONE)).toBe('min-content')
  })

  it("wraps the chip's name at its space before cutting it, inside the compacted bar", () => {
    // MUTATION: `nowrap`/`ellipsis` back on `.sk-tcn` in the bar: a single
    // page-name word wider than the room would cut the chip to `Your …`; or
    // the chip's own line-height or padding back up, so two lines outgrow 36px.
    const chip = build('div.sk-app.is-scrolled > header.sk-pbar.has-chip > button.sk-tchip', hosts)
    const name = document.createElement('span')
    name.className = 'sk-tcn'
    chip.appendChild(name)
    expect(painted(name, 'white-space', PHONE)).toBe('normal')
    expect(painted(name, 'text-overflow', PHONE)).not.toBe('ellipsis')
    expect(painted(name, 'overflow-wrap', PHONE) ?? 'normal').toBe('normal')
    expect(painted(name, 'word-break', PHONE) ?? 'normal').toBe('normal')
    expect(painted(name, '-webkit-line-clamp', PHONE)).toBe('2')
    const lh = Number(resolveVars(painted(name, 'line-height', PHONE)!, TABLES.dark))
    expect(Number.isFinite(lh) && lh > 0, "the chip's name states a unitless line-height").toBe(true)
    const size = fontPx(name, PHONE)
    const block = px(painted(chip, 'padding-block', PHONE) ?? painted(chip, 'padding', PHONE)!.split(/\s+/)[0]!)
    const tall = 2 * size * lh + 2 * block + bordersX(chip)
    expect(tall, "two lines of the chip's name outgrow the compacted bar").toBeLessThanOrEqual(px(painted(chip.parentElement!, 'height', PHONE)))
  })

  it("models every item in the admin's bar no narrower than the render review measured it", async () => {
    // MUTATION: `Top`'s arrow back at tablefit's 0.56em, or the pill read as
    // `Dev` only: the model's room is then wider than the real bar's, and the
    // two-line test below passes while the clamp cuts `issue`.
    const { header } = await bar('admin in their own workspace', 'local')
    const { widths } = roomFor(header)
    expect(Object.keys(widths).sort()).toEqual(Object.keys(RENDERED).sort())
    // Half a pixel for the render's own rounding to whole pixels.
    for (const [item, seen] of Object.entries(RENDERED)) expect(widths[item]!, `${item} modelled narrower than the ${seen}px rendered`).toBeGreaterThanOrEqual(seen - 0.5)
  })

  for (const [who, env] of WHO.flatMap((w) => ENVS.map((e) => [w, e] as const))) {
    it(`keeps the workspace whole and sets every long page name in at most two whole lines: ${who}, ${env}`, async () => {
      // MUTATION: an `em` floor on the name beside the chip wider than the
      // room (the chip pays the difference), the bar's gap or padding back up
      // beside the chip, or the title's font a step up, until the workspace
      // gives way, a word is wider than its box or a name needs a third line.
      let visited = 0
      const { header, retitle } = await bar(who, env)
      expect(header.classList.contains('has-chip')).toBe(who !== 'member')
      // The room is the bar's less every item but the name, so one reading
      // serves every name; so does the title's face, size, wrap and clamp.
      const title = [...header.children].find((c) => c.tagName.toLowerCase() === 'b') as HTMLElement
      const { room, chipPx } = roomFor(header)
      expect(chipPx).toBeGreaterThan(0)
      // Past its floor the name takes from the workspace (`flex-shrink:
      // 100`): a floor no wider than the room leaves the workspace whole.
      const floor = floorPx(title)
      expect(floor, `the name's floor takes ${(floor - room).toFixed(0)}px from the workspace`).toBeLessThanOrEqual(room)
      // A `nowrap` name has one line; a wrapping one as many as its clamp.
      const most = painted(title, 'white-space', PHONE) === 'nowrap' ? 1 : Number(painted(title, '-webkit-line-clamp', PHONE) ?? 1)
      expect(most).toBeLessThanOrEqual(2)
      const width = (t: string) => interPx(t, 650, title)
      for (const label of LONG_LABELS) {
        retitle(label)
        expect(title.textContent).toBe(label)
        // The name's floor is its widest word (`min-content`) where no
        // length is said: that word fits the room, so the chip is not asked for it.
        expect(widestWord(label, title), `"${label}"'s widest word takes from the workspace`).toBeLessThanOrEqual(room)
        const set = lines(label, width, room)
        expect(set.length, `"${label}" needs ${set.length} lines in ${room.toFixed(0)}px, and the bar draws ${most}: ${JSON.stringify(set)}`).toBeLessThanOrEqual(most)
        expect(set.join(' ')).toBe(label)
        visited++
      }
      expect(visited).toBe(LONG_LABELS.length)
      // No per-test timeout: vitest.config.ts's 30s `testTimeout` is the
      // headroom for a loaded runner, and a 15s one here timed out on #2590's
      // CI while a neighbouring file in the same shard took 370s.
    })
  }

  it('leaves the desktop unchanged: no phone bar from 760px up', () => {
    // MUTATION: take the phone bar's rules out of the `max-width: 759px` query.
    const header = build('div.sk-app > header.sk-pbar', hosts)
    expect(painted(header, 'display', WIDE)).toBe('none')
  })
})
