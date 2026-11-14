// THE NEEDS-YOU ROW ON A PHONE, ON EVERY WORK SCREEN (the owner's pick, 4
// November 2026). With nothing waiting the row is one line at rest: no box,
// no "Nothing waits on you here", its chips on one line that scrolls sideways
// rather than wraps. It was built for Work › Agents alone (agents.css, scoped
// to `.wi[data-screen='agents']`), so the Workflows list and the workflow page
// still drew the boxed, wrapped, three-line row; it lives in needs-you.css now,
// unscoped. When something waits the row keeps its box and its chips, and is
// the toggle (the owner's ask, 11 November 2026): collapsed it is one line,
// its list in a body opened from the header.
//
// And on the phone every faint "not known" chip drops its kind word
// (`.ny-word`), so it is named by an aria-label, as the counted chips are
// ("Failed: 2"): a screen reader hears "Approvals: not known", not "not known".
//
// MUTATIONS: scope the rest rule back to `.wi[data-screen='agents']` (the
// Workflows list and the workflow page come to three lines); drop
// `.ny:not(.has-some)` from it, so a waiting row loses its box; drop the faint
// chip's aria-label; let the body's own display win over `hidden`; draw the
// zero chips in the collapsed header, or spell out a faint chip's "not known";
// let a chip shrink and cut its count (min-width 0, overflow hidden), or stop
// the header wrapping when its chips do not fit.

import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import AGENTS_CSS from '../styles/agents.css?raw'
import STYLES from '../styles.css?raw'
import NEEDS_YOU_CSS from '../work/needs-you.css?raw'
import { cascade, type CascadeEnv } from './cssgate'
import { expandLocal, TABLES, THEMES, type Theme } from './marks'
import { resolveVars } from './spaceprobe'

vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()) }))

const { NeedsYou, NEED_KINDS, NEED_WORD } = await import('../work/NeedsYou')
type NeedItem = import('../work/NeedsYou').NeedItem
type NeedItems = import('../work/NeedsYou').NeedItems

const SHEETS = [
  ...Object.values(import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true })),
  STYLES,
  NEEDS_YOU_CSS,
].join('\n')

const PHONE: CascadeEnv = { width: 390 }

/** The Work screens that draw the row, each as the page wraps it. */
const SCREENS: readonly { name: string; wrap: (row: JSX.Element) => JSX.Element }[] = [
  {
    name: 'the Workflows list',
    wrap: (row) => (
      <div className="wi" data-screen="workflows">
        <div className="wi-main">
          <div className="wfg">{row}</div>
        </div>
      </div>
    ),
  },
  {
    name: 'Work › Agents',
    wrap: (row) => (
      <div className="wi" data-screen="agents">
        <div className="wi-main">
          <div className="work">{row}</div>
        </div>
      </div>
    ),
  },
  {
    name: 'the workflow page',
    wrap: (row) => (
      <div className="wfp-screen">
        <div className="wfp">{row}</div>
      </div>
    ),
  },
]

function won(el: Element, props: string | readonly string[], theme: Theme): string | null {
  const r = cascade(SHEETS, el, props, { ...PHONE, theme })
  if (r.unsupported.length > 0) throw new Error(`the resolver could not evaluate ${r.unsupported.join(', ')}`)
  return r.winner === null ? null : resolveVars(expandLocal(r.winner.value, el, PHONE), TABLES[theme]).trim()
}

function px(v: string | null, what: string): number {
  if (v === null) return 0
  const m = /^(-?\d+(?:\.\d+)?)(px)?$/.exec(v)
  if (m === null || (m[2] === undefined && parseFloat(m[1]!) !== 0)) throw new Error(`${what} is ${JSON.stringify(v)}, not a px length`)
  return parseFloat(m[1]!)
}

/** The first and last value of a 1-4 value shorthand (top, bottom). */
function vertical(v: string | null, what: string): number {
  if (v === null) return 0
  const parts = v.split(/\s+/)
  return px(parts[0]!, what) + px(parts[parts.length >= 3 ? 2 : 0]!, what)
}

function borderY(el: Element, theme: Theme): number {
  const v = won(el, ['border', 'border-width'], theme)
  if (v === null) return 0
  const w = v.split(/\s+/).find((t) => /^-?\d/.test(t))
  return w === undefined ? 0 : px(w, 'border width') * 2
}

const shown = (el: Element, theme: Theme): boolean => won(el, 'display', theme) !== 'none'

/** A chip's or the count's line box, px: its font's line, or its own fixed height. */
function lineOf(el: Element, theme: Theme): number {
  const h = won(el, 'height', theme)
  if (h !== null && h !== 'auto') return px(h, 'height')
  const font = won(el, 'font', theme) ?? ''
  const m = /(\d+(?:\.\d+)?)px\s*\/\s*(\d+(?:\.\d+)?)/.exec(font)
  if (m === null) throw new Error(`${el.className} declares no px font with a unitless line height: ${font}`)
  return parseFloat(m[1]!) * parseFloat(m[2]!) + vertical(won(el, ['padding'], theme), 'padding')
}

/**
 * The row's height at 390, px: its box's edges, then its bar -- one line when
 * it does not wrap, else as many as its chips at 0.6em of their 12px a
 * character need across the page's 358px less the box's sides -- then at
 * least a line for the quiet line or the list under it, the row's gap apart.
 */
function rowHeight(ny: HTMLElement, theme: Theme): { px: number; barLines: number } {
  const bar = ny.querySelector<HTMLElement>('.ny-bar')!
  const parts = [...bar.children].filter((c) => shown(c, theme))
  const line = Math.max(...parts.map((c) => lineOf(c.classList.contains('ny-all') ? c.querySelector('.ny-n')! : c, theme)))
  let barLines = 1
  if (won(bar, 'flex-wrap', theme) !== 'nowrap') {
    const sides = (won(ny, 'padding', theme) ?? '0').split(/\s+/)
    const width = 358 - 2 * px(sides[1] ?? sides[0]!, 'padding')
    const chars = parts.reduce((n, c) => n + [...c.childNodes].filter((k) => !(k instanceof Element) || shown(k, theme)).map((k) => k.textContent ?? '').join('').length + 3, 0)
    barLines = Math.max(1, Math.ceil((chars * 0.6 * 12) / width))
  }
  const under = [...ny.children].filter((c) => c !== bar && shown(c, theme))
  const gap = px(won(ny, ['gap', 'row-gap'], theme)?.split(/\s+/)[0] ?? null, 'gap')
  const underPx = under.length * (gap + lineOf(bar.querySelector('.ny-chip')!, theme))
  return { px: vertical(won(ny, 'padding', theme), 'padding') + borderY(ny, theme) + barLines * line + underPx, barLines }
}

function draw(items: NeedItems, at: (typeof SCREENS)[number]): HTMLElement {
  const { container } = render(at.wrap(<NeedsYou items={items} approvals={null} />))
  return container.querySelector<HTMLElement>('section.ny')!
}

const question: NeedItem = {
  kind: 'question',
  taskId: 'task_phone_q',
  index: 0,
  title: 'step-schema',
  question: 'Which schema?',
  options: [{ label: 'v1', description: 'keep it' }],
  recommended: 'v1',
}

describe('the Needs-you row on a phone, on every Work screen', () => {
  for (const at of SCREENS) {
    for (const theme of THEMES) {
      it(`is one line at rest on ${at.name} at 390 (${theme})`, () => {
        const ny = draw([], at)
        expect(ny.classList.contains('has-some')).toBe(false)
        const bar = ny.querySelector('.ny-bar')!
        const chip = bar.querySelector('.ny-chip')!
        const { px: h, barLines } = rowHeight(ny, theme)
        expect(barLines, 'the chips wrap').toBe(1)
        expect(won(bar, 'flex-wrap', theme)).toBe('nowrap')
        expect(won(bar, 'overflow-x', theme), 'a long line does not scroll sideways').toBe('auto')
        expect(shown(ny.querySelector('.ny-quiet')!, theme), 'the quiet line is drawn under the chips').toBe(false)
        expect(borderY(ny, theme), 'the row at rest is boxed').toBe(0)
        expect(vertical(won(ny, 'padding', theme), 'padding')).toBe(0)
        expect(won(ny, ['background', 'background-color'], theme)).toBe('none')
        expect(shown(chip, theme), 'the chips are hidden').toBe(true)
        // The line is the count's 22px pill, the tallest thing on it.
        expect(h, `${at.name}'s row at rest is ${h}px`).toBe(22)
      })

      it(`is one boxed line, collapsed, every count whole, on ${at.name} at 390 when a question waits, and opens its list from the header (${theme})`, () => {
        const ny = draw([question], at)
        expect(ny.classList.contains('has-some')).toBe(true)
        const bar = ny.querySelector<HTMLElement>('.ny-bar')!
        expect(bar.tagName, 'the header row is not the toggle').toBe('BUTTON')
        expect(bar.getAttribute('aria-expanded')).toBe('false')
        const chips = [...ny.querySelectorAll('.ny-bar > .ny-chip')]
        expect(chips.length).toBe(NEED_KINDS.length)
        // As WorkflowNeedsCard: a kind with nothing waiting is not drawn in the
        // collapsed header, so the counts that are drawn fit whole.
        for (const c of chips) {
          expect(shown(c, theme), `${c.className} is ${c.classList.contains('is-zero') ? 'drawn at zero' : 'hidden'}`).toBe(!c.classList.contains('is-zero'))
        }
        expect(within(bar).getByRole('img', { name: 'Question: 1' })).toBeTruthy()
        // The faint "not known" mark is its glyph alone, its words in the tooltip.
        const faint = bar.querySelector<HTMLElement>('.ny-chip.is-approval.is-faint')!
        expect(shown(faint, theme), 'the faint mark is hidden').toBe(true)
        expect(shown(faint.querySelector('.ny-glyph')!, theme), "the faint mark's glyph is hidden").toBe(true)
        expect(shown(faint.querySelector('b')!, theme), '"not known" is spelled out in the header').toBe(false)
        expect(faint.title).toMatch(/not known/)
        expect(shown(bar.querySelector('.ny-caret')!, theme), 'the caret is hidden').toBe(true)
        expect(borderY(ny, theme), 'the waiting row lost its box').toBeGreaterThan(0)
        expect(vertical(won(ny, 'padding', theme), 'padding')).toBeGreaterThan(0)
        // NO COUNT IS CLIPPED: the toggle neither scrolls nor cuts, a chip never
        // shrinks below its content, and if the chips do not fit they wrap to a
        // second line; "Needs you" and the caret keep their width.
        expect(won(bar, 'flex-wrap', theme), 'the header cannot wrap, so a count is cut').toBe('wrap')
        expect(won(bar, ['overflow', 'overflow-x'], theme) ?? 'visible', 'the toggle cuts or scrolls').toBe('visible')
        const all = bar.querySelector('.ny-all')!
        expect(won(all, 'white-space', theme), '"Needs you" wraps').toBe('nowrap')
        expect(won(all, ['flex', 'flex-shrink'], theme), '"Needs you" shrinks').toBe('none')
        const caret = bar.querySelector('.ny-caret')!
        expect(won(caret, ['flex', 'flex-shrink'], theme), 'the caret shrinks off the row').toBe('none')
        for (const c of chips) {
          expect(won(c, ['flex', 'flex-shrink'], theme), `${c.className} shrinks below its count`).toBe('none')
          expect(won(c, 'white-space', theme), `${c.className} breaks inside its count`).toBe('nowrap')
          const clips = won(c, 'min-width', theme) === '0' && won(c, ['overflow', 'overflow-x'], theme) === 'hidden'
          expect(clips, `${c.className} is cut at its edge`).toBe(false)
          expect(won(c, ['overflow', 'overflow-x'], theme) ?? 'visible', `${c.className} hides its overflow`).toBe('visible')
        }
        expect(won(ny, 'max-width', theme)).toBe('100%')
        const body = ny.querySelector<HTMLElement>('.ny-body')!
        expect(body.hidden).toBe(true)
        expect(shown(body, theme), 'the collapsed body is drawn').toBe(false)
        const { px: h, barLines } = rowHeight(ny, theme)
        expect(barLines).toBe(1)
        expect(h, 'the collapsed row is more than one line').toBe(vertical(won(ny, 'padding', theme), 'padding') + borderY(ny, theme) + 22)
        expect(screen.queryAllByRole('listitem')).toHaveLength(0)

        fireEvent.click(bar)
        expect(bar.getAttribute('aria-expanded')).toBe('true')
        expect(body.hidden).toBe(false)
        expect(shown(body, theme), 'the opened body is hidden').toBe(true)
        expect(within(ny).getByRole('list', { name: 'Needs you' }).children.length).toBe(1)
        expect(rowHeight(ny, theme).px, 'the opened row is only one line').toBeGreaterThan(h)
      })
    }
  }

  it('declares the rest look once, in needs-you.css, and Agents keeps no copy of it', () => {
    expect(AGENTS_CSS).not.toMatch(/\.ny[\s:.{>-]/)
    expect(NEEDS_YOU_CSS).toMatch(/\.ny:not\(\.has-some\)\s*\{[^}]*padding:\s*0/)
  })
})

describe("a faint 'not known' chip is named by its kind", () => {
  // Every kind the read can leave uncounted: approvals (null here) and each
  // kind a page marks unknown (the Workflows list over a step without needs).
  const unknown = Object.assign([] as NeedItem[], { unknownKinds: NEED_KINDS.filter((k) => k !== 'approval') }) as NeedItems

  for (const k of NEED_KINDS) {
    it(`labels the ${k} chip "${NEED_WORD[k]}: not known", the word hidden on a phone`, () => {
      const ny = draw(unknown, SCREENS[0]!)
      const chip = ny.querySelector<HTMLElement>(`.ny-chip.is-${k}.is-faint`)
      expect(chip, `no faint ${k} chip`).not.toBeNull()
      expect(shown(chip!.querySelector('.ny-word')!, 'dark'), 'the kind word shows on a phone').toBe(false)
      expect(within(ny).getByRole('img', { name: `${NEED_WORD[k]}: not known` })).toBe(chip)
    })
  }
})
