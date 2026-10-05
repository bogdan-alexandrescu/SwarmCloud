/**
 * ITEM 2 OF LANE U9 (owner, 2026-10-03, live console at 1440x900): on a
 * running agent the split's close ✕ covered the STOP button -- only 'st' of
 * it showed -- although Copy link was clear after walkthrough B.
 *
 * Now the header's actions (Copy link, Stop, ✕) have A ROW OF THEIR OWN, over
 * the state pill and the title, at every split width: the title no longer
 * competes with them for one line, the row wraps rather than overprints when
 * a confirm opens or a column is narrow, every control is in the flow (static,
 * no negative margin, never shrunk), and the drawer's sticky header band --
 * a 50px strip of `--bg` at z-index 3 over the top of the column, there to
 * seat a sticky ✕ over a scrolling drawer -- is not drawn in the split, whose
 * column does not scroll and whose ✕ is in this row.
 *
 * "No two header controls' boxes overlap" is asked of a flex-row model built
 * from the cascade (jsdom lays nothing out): each control's width from its
 * padding, border and label in its face, the row's gap and wrap, and the
 * detail column's width at each of the split's snaps -- the 380px compact
 * list, half the width, and the list folded to its strip (the agent at full
 * width) -- at 1440 and at 1100, the narrowest width the split docks at.
 * MUTATIONS: move the actions back into the title row, put `flex-wrap:
 * nowrap` on the actions, or let the band draw in the split -- each goes red.
 *
 * LINE 1 IS MEASURED WHOLE (fix of the v3 review, 2026-10-04): the pill, the
 * title at its min-width, the elapsed headline and the actions share line 1,
 * so a model of the actions alone could not see the row running past a
 * 527px column (1440, 50%) or a 547px one (1280, the 380px snap) and the
 * column's `overflow: hidden` clipping the ✕. MUTATION: put the row back to
 * `flex-wrap: nowrap`, or the narrow-column query back to 479px -- red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { fontPx, lengthPx, paddingX, textPx } from './tablefit'

const RUNNING = 'task_a073aff5'
/** The spine and the section panel beside the content column. */
const CHROME = 84 + 236
const SNAPS = [
  { pref: 'list', label: 'the 380px snap', list: (app: number) => 380 + 0 * app },
  { pref: 'half', label: '50%', list: (app: number) => app / 2 },
  { pref: 'strip', label: 'full (list folded to its strip)', list: (_app: number) => 64 },
] as const

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
  localStorage.clear()
})

async function split(pref: string): Promise<HTMLElement> {
  localStorage.setItem('swarm.agents.list', pref)
  window.location.hash = `#work/task/${RUNNING}`
  render(<App />)
  return waitFor(() => {
    const s = document.querySelector<HTMLElement>('.ag-split')
    expect(s?.querySelector('.ag-head-facts')).toBeTruthy()
    expect(s!.querySelector('.ag-head .run-stop-btn'), 'a running agent draws no Stop').not.toBeNull()
    return s!
  })
}

interface Box {
  name: string
  start: number
  end: number
  row: number
}

/** A control's border-box width from the cascade: its declared width, or padding + border + label. */
function controlPx(el: HTMLElement, env: CascadeEnv): number {
  const declared = lengthPx(painted(el, 'width', env), 0)
  if (declared !== null) return declared
  const [pl, pr] = paddingX(el, env)
  const border = lengthPx((painted(el, ['border-left-width', 'border-width'], env) ?? '1px').split(/\s+/)[0]!, 0) ?? 0
  return pl + pr + 2 * border + textPx((el.textContent ?? '').trim(), el, env)
}

/** The controls laid out as a flex row that wraps (or not) in `inner` px, packed to the end. */
function layout(row: HTMLElement, controls: HTMLElement[], inner: number, env: CascadeEnv): Box[] {
  const gap = lengthPx(painted(row, ['column-gap', 'gap'], env), 0) ?? 0
  const wraps = painted(row, 'flex-wrap', env) === 'wrap'
  const lines: { name: string; w: number }[][] = [[]]
  let used = 0
  for (const el of controls) {
    const w = controlPx(el, env)
    const line = lines[lines.length - 1]!
    const need = line.length === 0 ? w : used + gap + w
    if (wraps && line.length > 0 && need > inner) {
      lines.push([{ name: label(el), w }])
      used = w
    } else {
      line.push({ name: label(el), w })
      used = need
    }
  }
  const out: Box[] = []
  lines.forEach((line, r) => {
    const total = line.reduce((a, c) => a + c.w, 0) + gap * (line.length - 1)
    // Packed to the end, as `margin-inline-start: auto` puts the row; a row
    // wider than its box starts at 0 and runs out of the right edge.
    let x = Math.max(0, inner - total)
    for (const c of line) {
      out.push({ name: c.name, start: x, end: x + c.w, row: r })
      x += c.w + gap
    }
  })
  return out
}

function label(el: Element): string {
  return el.getAttribute('aria-label') ?? (el.textContent ?? '').trim()
}

describe('item 2: the agent header\'s actions never overlap, at every split width', () => {
  for (const viewport of [1440, 1280, 1100]) {
    for (const snap of SNAPS) {
      it(`no two controls overlap, and none leaves the column, at ${snap.label} in a ${viewport}px window`, async () => {
        const env: CascadeEnv = { width: viewport }
        const s = await split(snap.pref)
        const actions = s.querySelector<HTMLElement>('.ag-head-actions')!
        const controls = [...actions.querySelectorAll<HTMLElement>(':scope > button, :scope > span > button')]
        expect(controls.map(label), 'the header does not draw Copy link, Stop and ✕').toEqual(['Copy link', 'stop', 'Close'])
        for (const c of controls) {
          expect(painted(c, 'position', env) ?? 'static', `${label(c)} is positioned over the row`).toBe('static')
          expect(painted(c, ['flex', 'flex-shrink'], env) ?? '', `${label(c)} may be squeezed`).toMatch(/^(none|0)\b/)
          for (const side of ['margin-left', 'margin-right', 'margin-inline-start', 'margin-inline-end']) {
            expect(lengthPx(painted(c, side, env), 0) ?? 0, `${label(c)} has a negative ${side}`).toBeGreaterThanOrEqual(0)
          }
        }
        const app = viewport - CHROME
        const [pl, pr] = paddingX(s, env)
        const inner = app - snap.list(app) - pl - pr - 1
        const boxes = layout(actions, controls, inner, env)
        for (let i = 1; i < boxes.length; i++) {
          const a = boxes[i - 1]!
          const b = boxes[i]!
          if (a.row === b.row) expect(b.start, `${a.name} and ${b.name} overlap in a ${inner}px column`).toBeGreaterThanOrEqual(a.end)
        }
        for (const b of boxes) expect(b.end, `${b.name} runs out of a ${inner}px column`).toBeLessThanOrEqual(inner)
      })
    }
  }

  for (const viewport of [1440, 1280, 1100]) {
    for (const snap of SNAPS) {
      it(`line 1 -- pill, title, headline and actions -- stays inside the column at ${snap.label} in a ${viewport}px window`, async () => {
        const s = await split(snap.pref)
        const base: CascadeEnv = { width: viewport }
        const app = viewport - CHROME
        const [pl, pr] = paddingX(s, base)
        const inner = app - snap.list(app) - pl - pr - 1
        const env: CascadeEnv = { width: viewport, container: inner }
        const row = s.querySelector<HTMLElement>('.ag-head-row')!
        const pill = row.firstElementChild as HTMLElement
        const title = row.querySelector<HTMLElement>('.ag-head-title')!
        const hl = row.querySelector<HTMLElement>('.ag-head-hl')!
        const actions = row.querySelector<HTMLElement>('.ag-head-actions')!
        const full = (el: HTMLElement): boolean => painted(el, ['flex-basis', 'flex'], env)?.split(/\s+/).includes('100%') ?? false
        const order = (el: HTMLElement): number => Number(painted(el, 'order', env) ?? '0')
        const controls = [...actions.querySelectorAll<HTMLElement>(':scope > button, :scope > span > button')]
        const agap = lengthPx(painted(actions, ['column-gap', 'gap'], env), 0) ?? 0
        // The headline at its longest: a finished agent adds ` · ended HH:MM`,
        // and an elapsed of hours is longer than this fixture's.
        const hlText = `${(hl.textContent ?? '').trim()} · ended 23:59`
        const items = [
          { name: 'the state pill', el: pill, w: full(pill) ? inner : paddingX(pill, env).reduce((a, b) => a + b, 0) + 18 + textPx((pill.textContent ?? '').trim(), pill, env) },
          { name: 'the title', el: title, w: full(title) ? inner : (lengthPx(painted(title, 'min-width', env), 0) ?? fontPx(title, env) * 4) },
          { name: 'the headline', el: hl, w: full(hl) ? inner : textPx(hlText, hl, env) },
          { name: 'the actions', el: actions, w: controls.reduce((a, c) => a + controlPx(c, env), 0) + agap * (controls.length - 1) },
        ].sort((a, b) => order(a.el) - order(b.el))
        const gap = lengthPx(painted(row, ['column-gap', 'gap'], env), 0) ?? 0
        const wraps = painted(row, 'flex-wrap', env) === 'wrap'
        let line = 0
        let x = 0
        let first = true
        const boxes: Box[] = []
        for (const it of items) {
          const start = first ? 0 : x + gap
          if (wraps && !first && start + it.w > inner) {
            line += 1
            boxes.push({ name: it.name, start: 0, end: it.w, row: line })
            x = it.w
          } else {
            boxes.push({ name: it.name, start, end: start + it.w, row: line })
            x = start + it.w
          }
          first = false
        }
        for (const b of boxes) expect(b.end, `${b.name} runs out of a ${Math.round(inner)}px column (line ${b.row + 1})`).toBeLessThanOrEqual(inner)
        // Nothing else rides the pill's line once the row is narrow: the title
        // and the headline have lines of their own, as A5 draws it.
        if (inner < 640) {
          expect(full(title), `the title shares line 1 in a ${Math.round(inner)}px column`).toBe(true)
          expect(full(hl), `the headline shares line 1 in a ${Math.round(inner)}px column`).toBe(true)
        }
      })
    }
  }

  // LINE 1 AGAIN (agent-details-v3.html A, picked 2026-10-04): the pill, the
  // title, the elapsed headline and the actions share one line. What U9's own
  // row was FOR still holds: nothing the actions hold can be drawn over
  // another control -- the title takes the slack and clamps, the headline and
  // the actions never shrink, and the actions wrap inside themselves.
  // MUTATION: let the actions or the headline shrink, or stop the title
  // taking the slack, or the actions wrapping.
  it('shares line 1 with the title, and neither the actions nor the headline is squeezed under it', async () => {
    const s = await split('list')
    const env = { width: 1440 }
    const actions = s.querySelector<HTMLElement>('.ag-head-actions')!
    const row = actions.parentElement!
    expect(row.classList.contains('ag-head-row'), 'the actions are not on line 1').toBe(true)
    expect(row.querySelector('[data-mark]')).not.toBeNull()
    const title = row.querySelector<HTMLElement>('.ag-head-title')!
    expect(title).not.toBeNull()
    expect(painted(row, 'flex-wrap', env), 'line 1 runs out of the column rather than wrap').toBe('wrap')
    expect(painted(actions, ['flex', 'flex-shrink'], env), 'the actions may be squeezed').toMatch(/^(none|0)\b/)
    expect(painted(row.querySelector('.ag-head-hl')!, ['flex', 'flex-shrink'], env), 'the headline may be squeezed').toMatch(/^(none|0)\b/)
    expect(painted(title, ['flex', 'flex-grow'], env), 'the title does not take the slack').toMatch(/^1\b/)
    expect(painted(actions, 'flex-wrap', env), 'the actions overprint rather than wrap').toBe('wrap')
  })

  it('draws no sticky header band over the split\'s header', async () => {
    const s = await split('list')
    expect(painted(s, 'display', { width: 1440 }, 'before'), 'the drawer\'s band is drawn over the header').toBe('none')
  })
})
