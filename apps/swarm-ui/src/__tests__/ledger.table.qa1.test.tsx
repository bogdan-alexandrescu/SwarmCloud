// THE OUTCOMES DAY TABLE FITS AND SAYS WHEN IT DOES NOT (QA1-FIX-13,
// runs-submit-40, part of #2596). MEASURED on the fixture console at 1440: the
// table needed a sideways scroller and a column was cut at the right edge; at
// 390 only Day and Succeeded showed, with nothing saying more columns existed;
// and the Cancelled cell "0 · 0 / 0 / 0" sat under a four-part head
// ("Cancelled · requested or other / after a failure / after a cancel") that
// was most of the overflow on its own.
//
// So: the Cancelled cell is four columns under one "Cancelled" group head, every
// head's qualifier is a second line rather than a run-on, and while columns are
// off the scroller's right edge a line under the table says so in words.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { LedgerTable } from '../Ledger'
import { ledgerFixture } from '../outcomes.fixture'

afterEach(cleanup)

/** The head cells that name one column each: the leaves of the head, left to right. */
function leaves(root: HTMLElement): HTMLTableCellElement[] {
  const rows = [...root.querySelectorAll<HTMLTableRowElement>('.ol-table thead tr')]
  const top = [...rows[0]!.cells]
  const under = rows[1] === undefined ? [] : [...rows[1].cells]
  const out: HTMLTableCellElement[] = []
  for (const th of top) {
    if (th.colSpan > 1) out.push(...under.splice(0, th.colSpan))
    else out.push(th)
  }
  return out
}

const words = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('the Outcomes day table (runs-submit-40)', () => {
  it('splits Cancelled into four columns under one group head, one figure per cell', () => {
    const d = ledgerFixture()
    const { container } = render(<LedgerTable data={d} />)
    const group = [...container.querySelectorAll<HTMLTableCellElement>('.ol-table thead th')].find((th) => words(th) === 'Cancelled')
    // MUTATION: put back the one "Cancelled · requested or other / after a failure / after a cancel" head and this is undefined.
    expect(group, 'no "Cancelled" group head').toBeDefined()
    expect(group!.colSpan).toBe(4)
    expect(group!.getAttribute('scope')).toBe('colgroup')
    const heads = leaves(container).map(words)
    const at = heads.indexOf('all')
    expect(heads.slice(at, at + 4)).toEqual(['all', 'requested or other', 'after a failure', 'after a cancel'])
    const i = d.buckets.findIndex((b) => b.state !== 'unread' && (b.cancelled?.total ?? 0) > 0)
    const b = d.buckets[i]!
    const row = container.querySelectorAll('.ol-table tbody tr')[i]!
    expect(row.children).toHaveLength(heads.length)
    const cells = [...row.children].map(words)
    expect(cells.slice(at, at + 4)).toEqual([
      String(b.cancelled!.total),
      String(b.cancelled!.requested + b.cancelled!.other),
      String(b.cancelled!.after_failure + b.cancelled!.workflow_sweep),
      String(b.cancelled!.after_cancel),
    ])
    // No cell is a compound of slash-separated figures any more.
    for (const r of container.querySelectorAll('.ol-table tbody tr')) {
      for (const c of r.children) expect(words(c)).not.toMatch(/\d+ · \d+ \/ \d+/)
    }
  })

  it('writes every head qualifier as its own line, never as a run-on that sets the column width', () => {
    const { container } = render(<LedgerTable data={ledgerFixture()} />)
    const ths = [...container.querySelectorAll<HTMLTableCellElement>('.ol-table thead th')]
    // MUTATION: restore "Rate · k of n · 95 %" as one head string and the first of these fails.
    for (const th of ths) expect(words(th), 'a head is one run-on string').not.toMatch(/ · | \/ /)
    const failed = ths.find((th) => th.firstChild?.textContent === 'Failed')!
    expect(failed.querySelector('.ol-th-sub')?.textContent).toBe('incl. dead-lettered')
    const rate = ths.find((th) => th.firstChild?.textContent === 'Rate')!
    expect(rate.querySelector('.ol-th-sub')?.textContent).toBe('k of n, 95 % range')
  })

  it('splits the Rate cell into two lines, never one run-on that sets the column width (review of runs-submit-40)', () => {
    // MEASURED by the review on the rendered fixture console at 1440: the Rate
    // column was 297 px of one run-on "100.0 % · 18 of 18 · 82.4-100.0 %", most
    // of why the table (1125 px) overran its 1039 px scroller.
    const d = ledgerFixture()
    const { container } = render(<LedgerTable data={d} />)
    const at = leaves(container).map((th) => th.firstChild?.textContent).indexOf('Rate')
    const i = d.buckets.findIndex((b) => b.state !== 'unread' && b.rate !== null)
    const b = d.buckets[i]!
    const cell = container.querySelectorAll('.ol-table tbody tr')[i]!.children[at]!
    const range = cell.querySelector('.ol-rate-range')
    // MUTATION: put back the one `${pct} · ${k} of ${n} · ${interval}` string and this is null.
    expect(range, 'the Rate cell is one run-on string').not.toBeNull()
    expect(words(cell)).toBe(`${words(cell.firstElementChild!)} ${words(range!)}`)
    expect(words(cell.firstElementChild!)).toMatch(new RegExp(`^\\S+ % · ${b.rate!.k} of ${b.rate!.n}$`))
    expect(words(range!)).toMatch(/%$/)
    for (const r of container.querySelectorAll('.ol-table tbody tr')) {
      for (const c of r.children) expect(words(c)).not.toMatch(/ of \d+ · \d/)
    }
  })

  it('says in words that more columns are to the right while the scroller hides some, and only then', () => {
    const proto = HTMLElement.prototype
    const sw = Object.getOwnPropertyDescriptor(proto, 'scrollWidth')
    const cw = Object.getOwnPropertyDescriptor(proto, 'clientWidth')
    try {
      Object.defineProperty(proto, 'scrollWidth', { configurable: true, get: () => 900 })
      Object.defineProperty(proto, 'clientWidth', { configurable: true, get: () => 360 })
      const narrow = render(<LedgerTable data={ledgerFixture()} />)
      const note = narrow.container.querySelector('.ol-table-more')
      // MUTATION: drop the note (or render it unconditionally) and one of these two fails.
      expect(note, 'a phone-width table says nothing about its hidden columns').not.toBeNull()
      expect(words(note!)).toMatch(/^More columns to the right: scroll the table sideways to read all \d+\.$/)
      cleanup()
      Object.defineProperty(proto, 'scrollWidth', { configurable: true, get: () => 360 })
      const wide = render(<LedgerTable data={ledgerFixture()} />)
      expect(wide.container.querySelector('.ol-table-more')).toBeNull()
    } finally {
      if (sw) Object.defineProperty(proto, 'scrollWidth', sw)
      else delete (proto as unknown as Record<string, unknown>).scrollWidth
      if (cw) Object.defineProperty(proto, 'clientWidth', cw)
      else delete (proto as unknown as Record<string, unknown>).clientWidth
    }
  })
})
