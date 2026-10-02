/**
 * THE TABLE (components.html A, "Tables"), replacing four table styles:
 * 32px rows, 12px sentence-case heads, figures right-aligned in tabular
 * numerals, a sticky head, sortable columns, and ONE expandable row open at
 * a time. Loading is skeleton rows at the real row height, never a spinner
 * over an empty box.
 *
 * Sorting is the caller's data, not this component's: `sort` says which
 * column is sorted and which way, and `onSort` asks for another. The table
 * never reorders rows it was handed, so what it draws is what the screen
 * decided -- and `aria-sort` says the same on the head a screen reader reads.
 */
import { Fragment, useState, type ReactNode } from 'react'
import { CIcon } from './icons'

export interface Column<R> {
  key: string
  head: ReactNode
  cell: (row: R) => ReactNode
  /** A figure: right-aligned, tabular. */
  num?: boolean
  sortable?: boolean
}

export interface SortState {
  key: string
  dir: 'asc' | 'desc'
}

export function Table<R>({
  columns,
  rows,
  rowKey,
  caption,
  sort,
  onSort,
  expand,
  selected,
  sticky = false,
  loading = false,
  foot,
}: {
  columns: readonly Column<R>[]
  rows: readonly R[]
  rowKey: (row: R) => string
  /** The table's accessible name. */
  caption: string
  sort?: SortState | null
  onSort?: (next: SortState) => void
  /** What an opened row shows under it. Omitted: rows do not expand. */
  expand?: (row: R) => ReactNode
  /** The row whose detail is open beside the table. */
  selected?: string | null
  /** The head stays put while the body scrolls (the box gets a max height). */
  sticky?: boolean
  /** Three skeleton rows at the real row height. */
  loading?: boolean
  /** The window note under the table: "Showing 7 of 212 …". */
  foot?: ReactNode
}) {
  const [open, setOpen] = useState<string | null>(null)
  const span = columns.length + (expand === undefined ? 0 : 1)
  const ask = (c: Column<R>) => {
    if (onSort === undefined) return
    const dir = sort?.key === c.key && sort.dir === 'desc' ? 'asc' : 'desc'
    onSort({ key: c.key, dir })
  }
  return (
    <>
      <div className={`c-tbl${sticky ? ' is-sticky' : ''}`}>
        <table aria-label={caption} aria-busy={loading || undefined}>
          <thead>
            <tr>
              {expand !== undefined && <th className="c-cc" aria-label="Open" />}
              {columns.map((c) => {
                const on = sort?.key === c.key
                return (
                  <th
                    key={c.key}
                    scope="col"
                    className={c.num === true ? 'is-num' : undefined}
                    aria-sort={on ? (sort!.dir === 'asc' ? 'ascending' : 'descending') : c.sortable === true ? 'none' : undefined}
                  >
                    {c.sortable === true && onSort !== undefined ? (
                      <button type="button" className={`c-th${on ? ' is-on' : ''}`} onClick={() => ask(c)}>
                        {c.head}
                        <CIcon name={on ? (sort!.dir === 'asc' ? 'up' : 'down') : 'sort'} />
                      </button>
                    ) : (
                      c.head
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {loading
              ? [0, 1, 2].map((i) => (
                  <tr key={`sk-${i}`} aria-hidden>
                    {Array.from({ length: span }, (_, j) => (
                      <td key={j}>
                        <span className="c-sk" style={{ width: j === 0 ? '40%' : '70%' }} />
                      </td>
                    ))}
                  </tr>
                ))
              : rows.map((r) => {
                  const k = rowKey(r)
                  const isOpen = open === k
                  return (
                    <Fragment key={k}>
                      <tr className={selected === k ? 'is-sel' : undefined} aria-selected={selected === undefined ? undefined : selected === k}>
                        {expand !== undefined && (
                          <td className="c-cc">
                            <button
                              type="button"
                              className="c-car"
                              aria-expanded={isOpen}
                              aria-label={isOpen ? 'Close row' : 'Open row'}
                              onClick={() => setOpen(isOpen ? null : k)}
                            >
                              <CIcon name="chevron" />
                            </button>
                          </td>
                        )}
                        {columns.map((c) => (
                          <td key={c.key} className={c.num === true ? 'is-num' : undefined}>
                            {c.cell(r)}
                          </td>
                        ))}
                      </tr>
                      {isOpen && expand !== undefined && (
                        <tr className="c-xrow">
                          <td colSpan={span}>{expand(r)}</td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
          </tbody>
        </table>
      </div>
      {foot !== undefined && <div className="c-tfoot">{foot}</div>}
    </>
  )
}
