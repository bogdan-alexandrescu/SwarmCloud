import { useId, type ReactNode } from 'react'

import { Button } from './components'
import { Mark } from './primitives'
import { useReducedMotion } from './useInView'

/**
 * A Timeline card's own frame -- the same `.ctl-card.ol-card` section, head
 * and body the Ledger's cards draw -- for the two states a lazily read card
 * has before it has content: still reading, and failed. The title stays, so
 * the grid never reflows and a reader knows which card is which.
 */
function Frame({
  title,
  note,
  className,
  busy,
  children,
}: {
  title: string
  note: ReactNode
  className?: string | undefined
  busy?: boolean
  children: ReactNode
}) {
  const id = useId()
  return (
    <section
      className={`ctl-card ol-card${className ? ` ${className}` : ''}`}
      aria-labelledby={`${id}-t`}
      aria-busy={busy === true ? true : undefined}
    >
      <div className="ctl-card-head">
        <h2 className="ctl-card-title" id={`${id}-t`}>
          {title}
        </h2>
        <span className="ctl-card-note">{note}</span>
      </div>
      <div className="ctl-card-body">{children}</div>
    </section>
  )
}

/**
 * STILL READING, IN THE CARD'S SHAPE (#377). One bar per line the loaded card
 * draws, each on a real `.ol-line` so the skeleton is as tall as the card it
 * stands in for and the page does not jump when the data lands. The bars are
 * `.ctl-pending`'s moving surface; under `prefers-reduced-motion` they are
 * the static `.is-static` fill instead. The bars carry no text; the note's
 * pending mark is what a screen reader hears.
 */
export function CardSkeleton({
  title,
  note,
  say,
  lines,
  className,
}: {
  title: string
  /** Words after the pending mark in the head, e.g. what the card never applies. */
  note?: string
  /** What is being read, as a sentence for the mark's accessible name. */
  say: string
  /** Each line's width, as a percentage of the card body. */
  lines: readonly number[]
  className?: string
}) {
  const reduce = useReducedMotion()
  return (
    <Frame
      title={title}
      className={`${className ?? ''} is-loading`.trim()}
      busy
      note={
        <>
          <Mark kind="pending" say={say} />
          {note !== undefined && ` · ${note}`}
        </>
      }
    >
      {lines.map((w, i) => (
        <p key={i} className="ol-line ol-skel-line" aria-hidden="true">
          <span className={`ol-skel-bar ${reduce ? 'is-static' : 'ctl-pending'}`} style={{ width: `${w}%` }} />
        </p>
      ))}
    </Frame>
  )
}

/**
 * A FAILED READ IS A FAILURE, NEVER AN EMPTY CARD THAT LOOKS LOADED (#377).
 * No count is drawn, each read that failed is named with the server's words,
 * and the card offers to read again.
 */
export function CardFailed({
  title,
  note,
  failures,
  onRetry,
  className,
}: {
  title: string
  note?: string
  /** One sentence per read that failed. */
  failures: readonly string[]
  onRetry: () => void
  className?: string
}) {
  return (
    <Frame
      title={title}
      className={className}
      note={
        <>
          <Mark kind="unread" say={`This card could not be read: ${failures.join('; ')}.`} />
          {note !== undefined && ` · ${note}`}
        </>
      }
    >
      <div className="ol-card-failed" role="status">
        {failures.map((f, i) => (
          <p key={i} className="ol-line">
            {f}
          </p>
        ))}
        <p className="ol-line">
          {/* The canonical small button (components.html A), not Submit's
              private `.sbf-mini` (gone since the #503 swap). */}
          <Button size="sm" onClick={onRetry}>
            Try again
          </Button>
        </p>
      </div>
    </Frame>
  )
}
