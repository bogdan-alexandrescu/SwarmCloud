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

/** A chip's state tint, for a chip that names a step in that state (the band's chips). */
export type ChipTone = 'live' | 'park' | 'bad'

export function Chip({
  children,
  href,
  onRemove,
  removeLabel,
  onClick,
  pressed,
  tone,
  faint = false,
  className,
  title,
}: {
  children: ReactNode
  /** A link chip: opens the view the chip names (`pool:global`). */
  href?: string
  /** A filter chip: the cross removes it. */
  onRemove?: () => void
  /** The cross's accessible name, e.g. "Remove filter state: failed". */
  removeLabel?: string
  /**
   * A button chip: an action in the chip's shape (Timeline's back-to-span
   * chip), or with `pressed` a toggle (a workflow band's named steps).
   */
  onClick?: () => void
  /** A toggle's pressed state; the picked chip carries the text-ink edge. Omitted, no `aria-pressed`. */
  pressed?: boolean
  /** The state's tint and edge (brand §3 hues), for a chip naming a step in that state. */
  tone?: ChipTone
  /** The quieter ink of an overflow count ("+3", "+2 more") or a sub-figure. */
  faint?: boolean
  /** Layout only (a container's truncation or gap), never a look of its own. */
  className?: string
  title?: string
}) {
  const cls = (base: string) =>
    [base, tone === undefined ? '' : `t-${tone}`, faint ? 'is-faint' : '', className ?? ''].filter(Boolean).join(' ')
  if (href !== undefined) {
    return (
      <a className={cls('c-chip is-link')} href={href} title={title}>
        {children}
      </a>
    )
  }
  if (onRemove !== undefined) {
    return (
      <span className={cls('c-chip is-filter')} title={title}>
        {children}
        <button type="button" className="c-x" aria-label={removeLabel ?? 'Remove filter'} onClick={onRemove}>
          <CIcon name="close" />
        </button>
      </span>
    )
  }
  if (onClick !== undefined) {
    return (
      <button type="button" className={cls('c-chip is-pick')} aria-pressed={pressed} title={title} onClick={onClick}>
        {children}
      </button>
    )
  }
  return (
    <span className={cls('c-chip')} title={title}>
      {children}
    </span>
  )
}

/**
 * A count chip. `n === null` is NOT zero: it is an unknown, drawn as a dash
 * whose reason is its title and its accessible name.
 */
export function Count({
  n,
  label,
  why,
  bare = false,
}: {
  n: number | null
  label: string
  why?: string
  /**
   * The figure alone, beside a heading that already names what it counts
   * ("Files 12"): the label is then its accessible name, not drawn text.
   */
  bare?: boolean
}) {
  if (n !== null && bare) {
    return (
      <span className="c-chip is-n" aria-label={`${n} ${label}`}>
        {n}
      </span>
    )
  }
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

export function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="c-tag" title={title}>
      {children}
    </span>
  )
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
