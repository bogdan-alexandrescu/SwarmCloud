/**
 * ONE CHIP SHAPE (components.html A, "Chips and tags"), replacing eleven
 * chip, tag and badge styles at three radii and two fonts.
 *
 *   Chip     a fact ("claude-code", "gen 1"), a count, a removable filter, or
 *            a link to a filtered view
 *   Count    a measured count -- or, when the count is unknown, a DASH WITH
 *            ITS REASON, never a 0 (the honesty rule, redesign-v2.md)
 *   Tag      a label someone attached ("nightly")
 *   LiveChip the live pulse; the one animation the console keeps
 *   Dash     an unknown figure anywhere: a dash, and the reason as its name
 */
import type { ReactNode } from 'react'
import { CIcon } from './icons'

export function Chip({
  children,
  href,
  onRemove,
  removeLabel,
  title,
}: {
  children: ReactNode
  /** A link chip: opens the view the chip names (`pool:global`). */
  href?: string
  /** A filter chip: the cross removes it. */
  onRemove?: () => void
  /** The cross's accessible name, e.g. "Remove filter state: failed". */
  removeLabel?: string
  title?: string
}) {
  if (href !== undefined) {
    return (
      <a className="c-chip is-link" href={href} title={title}>
        {children}
      </a>
    )
  }
  if (onRemove !== undefined) {
    return (
      <span className="c-chip is-filter" title={title}>
        {children}
        <button type="button" className="c-x" aria-label={removeLabel ?? 'Remove filter'} onClick={onRemove}>
          <CIcon name="close" />
        </button>
      </span>
    )
  }
  return (
    <span className="c-chip" title={title}>
      {children}
    </span>
  )
}

/**
 * A count chip. `n === null` is NOT zero: it is an unknown, drawn as a dash
 * whose reason is its title and its accessible name.
 */
export function Count({ n, label, why }: { n: number | null; label: string; why?: string }) {
  if (n === null) {
    return (
      <span className="c-chip is-dash" title={why ?? 'not read'} aria-label={`${label}: ${why ?? 'not read'}`}>
        &mdash; {label}
      </span>
    )
  }
  return (
    <span className="c-chip">
      {n} {label}
    </span>
  )
}

export function Tag({ children }: { children: ReactNode }) {
  return <span className="c-tag">{children}</span>
}

export function LiveChip({ children = 'live' }: { children?: ReactNode }) {
  return (
    <span className="c-live">
      <i aria-hidden />
      {children}
    </span>
  )
}

/** An unknown figure: a dash, with the reason it is unknown as its name. */
export function Dash({ why }: { why: string }) {
  return (
    <span className="c-dash" title={why} aria-label={why} role="img">
      &mdash;
    </span>
  )
}
