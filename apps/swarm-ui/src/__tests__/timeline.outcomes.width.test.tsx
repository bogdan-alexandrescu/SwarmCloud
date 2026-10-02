// AUDIT #503, Timeline (measured at 1440, dark): "the Success rate chart is
// drawn about 410px wide in a 1050px column, leaving the right part empty.
// 'Time to result, by profile' takes half the width, with nothing beside it."
//
// The ledger draws three fixed drawings (1080 / 640 / 300) and shows the one
// whose width fits its box -- so in any box between two of them the shown
// drawing stopped short of the column's edge. The shown drawing now takes
// its box's width: never narrower than it was authored at (so no tick is
// scaled under the type floor), but no longer leaving the rest of the column
// empty. And the cards' grid packs densely, so a half-width card is never
// left alone on its row while a later half-width card could sit beside it.

import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import STYLES from '../styles.css?raw'
import { OutcomeLedger, fittedWidth, LEDGER_DRAWN } from '../charts/OutcomeLedger'
import { ledgerFixture } from '../outcomes.fixture'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the success-rate drawing fills its column', () => {
  it('fits the shown drawing to its box, and only the shown one', () => {
    const [wide, mid, narrow] = LEDGER_DRAWN
    expect(fittedWidth(mid, 1050)).toBe(1050)
    expect(fittedWidth(wide, 1050)).toBe(wide.w)
    expect(fittedWidth(wide, 1400)).toBe(1400)
    expect(fittedWidth(mid, 1400)).toBe(mid.w)
    // The phone's drawing keeps its floor and scrolls; it is never stretched.
    expect(fittedWidth(narrow, 500)).toBe(narrow.w)
    // An unmeasured box draws as authored.
    expect(fittedWidth(mid, null)).toBe(mid.w)
  })

  it('draws the mid drawing across a 1050px box, not 640px of it', () => {
    let fire: (() => void) | null = null
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fire = cb
        }
        observe() {}
        disconnect() {}
      },
    )
    const { container } = render(<OutcomeLedger data={ledgerFixture()} picked={null} onPick={() => {}} onZoom={() => {}} />)
    const figure = container.querySelector('figure.ol-chart') as HTMLElement
    Object.defineProperty(figure, 'clientWidth', { configurable: true, get: () => 1050 })
    act(() => fire?.())
    const svg = container.querySelector('.ol-drawing.is-mid svg.ol-svg') as SVGElement
    const left = LEDGER_DRAWN[1].left
    expect(Number(svg.getAttribute('width'))).toBe(1050 - left)
  })
})

describe('the cards leave no half-width card alone on its row', () => {
  it('packs the cards grid densely', () => {
    const rule = /^\.ol-cards\s*\{([^}]*)\}/m.exec(STYLES)
    expect(rule, '.ol-cards has no rule').not.toBeNull()
    expect(rule![1]).toMatch(/grid-auto-flow:\s*row dense/)
  })
})
