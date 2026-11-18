/**
 * THE CARD AND THE STAT TILE (components.html A, "Cards and stat tiles"),
 * replacing six card styles.
 *
 * A STAT TILE HAS FIVE HONEST FORMS, and they look different on purpose:
 *
 *   value     a measured figure, zero included ("0 · measured: none needed")
 *   unknown   not measured: a DASH and the reason, in the warm absent grey,
 *             dashed -- never a 0, which would be a measurement
 *   partial   a figure that could not see everything, saying what it missed
 *   loading   the tile's own size, with a skeleton where the figure goes
 *   failed    the read failed: "Could not read", and a way to retry
 *
 * The tile keeps its size in every form, so nothing moves when a read lands.
 */
import type { MouseEvent, ReactNode } from 'react'
import { BadGlyph } from './glyphs'

export function Card({
  title,
  level = 2,
  action,
  foot,
  children,
  id,
  className,
}: {
  title?: ReactNode
  level?: 2 | 3
  /** The card's one link out, right-aligned in its head (`CardLink`). */
  action?: ReactNode
  /** Provenance: what was read, from where, how long ago. */
  foot?: ReactNode
  children: ReactNode
  id?: string
  className?: string
}) {
  const H = level === 2 ? 'h2' : 'h3'
  return (
    <section className={`c-card${className === undefined ? '' : ` ${className}`}`} id={id}>
      {(title !== undefined || action !== undefined) && (
        <div className="c-card-h">
          {title !== undefined && <H>{title}</H>}
          {action}
        </div>
      )}
      {children}
      {foot !== undefined && <p className="c-card-foot">{foot}</p>}
    </section>
  )
}

/** A card's link out: accent, sans, no underline until hovered ("All 37 waiting →"). */
export function CardLink({ href, children, onClick }: { href: string; children: ReactNode; onClick?: (e: MouseEvent<HTMLAnchorElement>) => void }) {
  return (
    <a className="c-link is-card" href={href} onClick={onClick}>
      {children} &rarr;
    </a>
  )
}

export type TileValue =
  | { kind: 'value'; value: ReactNode; note?: ReactNode }
  | { kind: 'unknown'; why: string }
  | { kind: 'partial'; value: ReactNode; missed: string }
  | { kind: 'loading' }
  | { kind: 'failed'; why: string; onRetry?: () => void }

export function StatTile({ label, tile }: { label: string; tile: TileValue }) {
  switch (tile.kind) {
    case 'value':
      return (
        <div className="c-tile" data-form="value">
          <small>{label}</small>
          <b>{tile.value}</b>
          {tile.note !== undefined && <i>{tile.note}</i>}
        </div>
      )
    case 'unknown':
      return (
        <div className="c-tile is-unknown" data-form="unknown">
          <small>{label}</small>
          <b aria-label={`${label}: not measured`}>&mdash;</b>
          <i>{tile.why}</i>
        </div>
      )
    case 'partial':
      return (
        <div className="c-tile is-partial" data-form="partial">
          <small>{label}</small>
          <b>{tile.value}</b>
          <i>{tile.missed}</i>
        </div>
      )
    case 'loading':
      return (
        <div className="c-tile" data-form="loading" aria-busy="true">
          <small>{label}</small>
          <b>
            <span className="c-sk is-figure" aria-hidden />
          </b>
          <i>reading…</i>
        </div>
      )
    case 'failed':
      return (
        <div className="c-tile is-failed" data-form="failed">
          <small>{label}</small>
          <b>
            <BadGlyph />
            Could not read
          </b>
          <i>
            {tile.onRetry !== undefined && (
              <>
                <button type="button" className="c-link" onClick={tile.onRetry}>
                  Retry
                </button>{' '}
                ·{' '}
              </>
            )}
            {tile.why}
          </i>
        </div>
      )
  }
}
