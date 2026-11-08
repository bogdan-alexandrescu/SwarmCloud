import type { CSSProperties } from 'react'

/**
 * A NAME ON ONE ROW (owner, 2026-11-07, part of #2688). A name is capped at
 * 30 characters at submission (measured at 1440 pixels: the Workflows list's
 * name column holds 34); one submitted before the cap is cut here with an
 * ellipsis, the whole name its tooltip, so no list row wraps onto a second.
 */
const ONE_ROW: CSSProperties = {
  display: 'inline-block',
  maxWidth: '100%',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  verticalAlign: 'bottom',
}

export function OneRowName({ name, title }: { name: string; title?: string }) {
  return (
    <span className="one-row-name" title={title ?? name} style={ONE_ROW}>
      {name}
    </span>
  )
}
