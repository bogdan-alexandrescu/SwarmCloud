/**
 * EMPTY, PARTIAL, FAILED AND LOADING ARE FOUR DIFFERENT THINGS AND LOOK IT
 * (components.html A, "Empty, partial, failed, loading"; four empty styles,
 * four skeletons and two failed panels before this).
 *
 *   empty    the read succeeded and returned nothing: a real zero
 *   partial  some sources were read and some were not, and it says which
 *   failed   the read failed -- "This is a failed read, not an empty list"
 *   loading  skeleton lines at the real size, never a spinner
 *
 * Each names what it could not see and gives the way out.
 */
import type { ReactNode } from 'react'
import { BadGlyph, RingGlyph, WarnGlyph } from './glyphs'

export type EmptyKind = 'empty' | 'partial' | 'failed'

export function EmptyState({
  kind,
  heading,
  children,
  action,
  page = false,
}: {
  kind: EmptyKind
  heading: string
  children: ReactNode
  action?: ReactNode
  /** The state IS the page (the not-found page): its heading is the page's `<h1>`, in the same face. */
  page?: boolean
}) {
  const Heading = page ? 'h1' : 'h3'
  return (
    <div className={`c-emp${kind === 'empty' ? '' : ` is-${kind}`}`} data-kind={kind} role={kind === 'failed' ? 'alert' : undefined}>
      <Heading>
        <span className="c-emp-mark">{kind === 'failed' ? <BadGlyph /> : kind === 'partial' ? <WarnGlyph /> : <RingGlyph />}</span>
        {heading}
      </Heading>
      <p>{children}</p>
      {action}
    </div>
  )
}

/** A skeleton line at the size the real text will take. */
export function Skeleton({ width = '60%', title = false }: { width?: string; title?: boolean }) {
  return <span className={`c-sk${title ? ' is-title' : ''}`} style={{ width }} aria-hidden />
}

/** Skeleton lines in a box the size of the region they stand in for. */
export function LoadingState({ lines = 3, label = 'Reading…' }: { lines?: number; label?: string }) {
  const widths = ['40%', '70%', '55%', '80%', '60%']
  return (
    <div className="c-emp is-loading" data-kind="loading" aria-busy="true" aria-label={label}>
      <div className="c-sk-lines">
        {Array.from({ length: lines }, (_, i) => (
          <Skeleton key={i} width={widths[i % widths.length]} title={i === 0} />
        ))}
      </div>
    </div>
  )
}
