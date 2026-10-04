import { useEffect, useRef } from 'react'

/**
 * ESCAPE CLOSES THE INNERMOST LAYER, AND ONLY IT (U10a, owner QA 2026-10-04).
 *
 * The agent split closes on Escape (AgentSplit's own `onKeyDown`). Two things
 * open inside it and must close first: the log's full view (D1) and an
 * artifact's viewer. Each registers here while it is open; one listener, in
 * the CAPTURE phase on the window, closes the most recently opened and stops
 * the key there -- before React's listener at the app root, so the split
 * never sees an Escape a layer above it already used.
 *
 * An open help card (`[data-focus-return]`, HelpCard.tsx) keeps its own
 * Escape: it is above everything, and it closes first.
 */
const layers: { close: () => void }[] = []

function onKey(e: KeyboardEvent): void {
  if (e.key !== 'Escape' || e.defaultPrevented || layers.length === 0) return
  if (document.querySelector('[data-focus-return]') !== null) return
  e.preventDefault()
  e.stopPropagation()
  layers[layers.length - 1]!.close()
}

/** While `open`, Escape calls `close` -- unless a layer opened after this one is still open. */
export function useEscapeLayer(open: boolean, close: () => void): void {
  const latest = useRef(close)
  latest.current = close
  useEffect(() => {
    if (!open) return
    const layer = { close: () => latest.current() }
    if (layers.length === 0) window.addEventListener('keydown', onKey, true)
    layers.push(layer)
    return () => {
      const i = layers.indexOf(layer)
      if (i >= 0) layers.splice(i, 1)
      if (layers.length === 0) window.removeEventListener('keydown', onKey, true)
    }
  }, [open])
}
