/**
 * The 12-unit marks the components put beside words: the amber triangle (a
 * warning, never a state), the failure diamond, and the "not read" ring.
 * Geometry is brand §3's, through `MarkGlyph`, so there is one copy of it.
 */
import { MarkGlyph } from '../marks'

export function WarnGlyph() {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <MarkGlyph mark="warn" />
    </svg>
  )
}

export function BadGlyph() {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <MarkGlyph mark="failed" />
    </svg>
  )
}

export function RingGlyph() {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <MarkGlyph mark="queued" />
    </svg>
  )
}

export function InfoGlyph() {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <circle cx="6" cy="6" r="5" fill="currentColor" />
    </svg>
  )
}
