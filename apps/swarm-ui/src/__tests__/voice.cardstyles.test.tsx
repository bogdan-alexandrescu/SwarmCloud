// VOICE IT'S CARDS, AS THE PRESENCE DIRECTION DRAWS THEM (docs/design/voice-it.md
// §11): each card is a panel of glass around the ring, its facts a two-column
// list, its lanes rows on the glass line, and nothing in it wider than the
// viewport, in both themes, at 1440 and at 390 wide.
//
// The card list is rendered from the fixture cards (one of each kind) and the
// SHIPPED sheets are weighed with `cascade` (cssgate.ts), which evaluates the
// phone `@media` block against the stated width and ranks by specificity, as a
// browser does: components.css first, talk-pane.css after it, the order the
// console loads them.
//
//   * the list carries the card classes: vt-card, vt-card-summary,
//     vt-card-facts, vt-card-lanes and vt-card-refs;
//   * the card is glass: `--vt-glass` over the glass line, blurred;
//   * a card's title wraps rather than pushing its tags past the edge;
//   * no element in a card holds its text on one line (`nowrap`) unless it
//     sits in a box that scrolls sideways, so a long reference, option, tag
//     or territory path wraps inside a 390-wide phone; a button is the one
//     exception, since its label is the console's own short copy ("Open in
//     Work"), never card data;
//   * the facts are a label column and a value column that may shrink;
//   * lanes are rows without bullets, each on the glass line's left edge,
//     its territory on a line of its own;
//   * every colour a card rule names is a console token, so the light theme
//     and the dark one draw the same card in their own palette.
//
// MUTATIONS: let the card title keep its min-content width; leave a reference
// chip `nowrap`; give the lanes their bullets back; put a literal colour in a
// card rule.

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import COMPONENTS_CSS from '../styles/components.css?raw'
import TALK_CSS from '../styles/talk-pane.css?raw'
import { CardList, FIXTURE_CARDS } from '../voice/CardList'
import { cascade, flatRules, type CascadeEnv } from './cssgate'

const SHEETS = `${COMPONENTS_CSS}\n${TALK_CSS}`

const THEMES = ['light', 'dark'] as const
const WIDTHS = [1440, 390] as const

function value(el: Element, prop: string, env: CascadeEnv): string | null {
  const r = cascade(SHEETS, el, prop, env)
  return r.winner?.value ?? null
}

/** Inside a box that scrolls sideways (the budget's `.c-tbl`): a long line scrolls there, not the page. */
function scrolls(el: Element, env: CascadeEnv): boolean {
  for (let p = el.parentElement; p !== null; p = p.parentElement) {
    const o = value(p, 'overflow-x', env) ?? value(p, 'overflow', env)
    if (o !== null && /\b(auto|scroll)\b/.test(o)) return true
    if (p.classList.contains('vt-card-list')) return false
  }
  return false
}

function drawList(): HTMLElement {
  const { container } = render(<CardList sessionId={null} fixtures={FIXTURE_CARDS} />)
  return container.querySelector('.vt-card-list') as HTMLElement
}

describe('the card list carries the card classes', () => {
  it('draws one vt-card per fixture kind, each around a console card', () => {
    const list = drawList()
    const cards = [...list.querySelectorAll('article.vt-card')]
    expect(cards.map((c) => c.getAttribute('data-kind'))).toEqual(FIXTURE_CARDS.map((c) => c.kind))
    for (const c of cards) expect(c.querySelector(':scope > .c-card')).not.toBeNull()
  })

  it('draws the summary, facts, lanes and references the cards hold', () => {
    const list = drawList()
    for (const cls of ['vt-card-summary', 'vt-card-facts', 'vt-card-lanes', 'vt-card-refs']) {
      expect(list.querySelectorAll(`.${cls}`).length, cls).toBeGreaterThan(0)
    }
    const work = list.querySelector('.vt-card[data-kind="work"]') as HTMLElement
    expect(work.querySelector('dl.vt-card-facts > dt')).not.toBeNull()
    expect(work.querySelector('ul.vt-card-lanes > li .vt-card-territory code')).not.toBeNull()
  })
})

describe.each(THEMES)('the %s theme', (theme) => {
  describe.each(WIDTHS)('at %i wide', (width) => {
    const env: CascadeEnv = { width, theme }

    it('every card is a panel of glass', () => {
      const list = drawList()
      for (const panel of list.querySelectorAll('.vt-card > .c-card')) {
        expect(value(panel, 'background', env)).toBe('var(--vt-glass)')
        expect(value(panel, 'border-color', env)).toBe('var(--vt-glass-line)')
        expect(value(panel, 'backdrop-filter', env)).toMatch(/^blur\(/)
      }
    })

    it("a card's title may wrap, and gives way before its tags", () => {
      const list = drawList()
      for (const h of list.querySelectorAll('.vt-card .c-card-h > h3')) {
        expect(value(h, 'min-width', env)).toBe('0')
        expect(value(h, 'overflow-wrap', env)).toBe('anywhere')
      }
    })

    it('nothing in a card holds one line wider than the card, outside a box that scrolls', () => {
      const list = drawList()
      const held = [...list.querySelectorAll('.vt-card *')]
        .filter((el) => !el.classList.contains('c-btn'))
        .filter((el) => value(el, 'white-space', env) === 'nowrap' && !scrolls(el, env))
        .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} in ${el.closest('.vt-card')?.getAttribute('data-kind')}`)
      expect(held).toEqual([])
    })

    it('the facts are a label column and a value column that can shrink', () => {
      const list = drawList()
      const dl = list.querySelector('.vt-card-facts') as HTMLElement
      expect(value(dl, 'display', env)).toBe('grid')
      expect(value(dl, 'grid-template-columns', env)).toBe('max-content minmax(0, 1fr)')
      const dd = dl.querySelector('dd') as HTMLElement
      expect(value(dd, 'overflow-wrap', env)).toBe('anywhere')
    })

    it('lanes are rows on the glass line, without bullets, the territory on its own line', () => {
      const list = drawList()
      for (const ul of list.querySelectorAll('.vt-card-lanes')) {
        expect(value(ul, 'list-style', env)).toBe('none')
        expect(value(ul, 'padding-left', env)).toBe('0')
        for (const li of ul.querySelectorAll(':scope > li')) {
          expect(value(li, 'box-shadow', env)).toBe('inset 2px 0 0 var(--vt-glass-line)')
        }
      }
      const territory = list.querySelector('.vt-card-territory') as HTMLElement
      expect(value(territory, 'display', env)).toBe('flex')
    })
  })
})

describe('the card rules name console tokens only', () => {
  it('no card rule, in either theme, carries a literal colour', () => {
    const rules = flatRules(TALK_CSS).filter((r) => r.selector.includes('.vt-card'))
    expect(rules.length).toBeGreaterThan(10)
    for (const r of rules) expect(r.body, r.selector).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|(?<![\w-])(white|black)(?![\w-])/i)
  })
})
