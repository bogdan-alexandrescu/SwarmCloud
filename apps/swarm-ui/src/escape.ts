import { useEffect, useRef } from 'react'

/**
 * ESCAPE CLOSES THE INNERMOST LAYER, AND ONLY IT (U10a, owner QA 2026-10-04).
 *
 * The agent split closes on Escape (AgentSplit's own `onKeyDown`). What opens
 * inside it must close first: an artifact's viewer (the log's full view was
 * the other, until the log became a tab). Each registers here while it is open; one listener, in
 * the CAPTURE phase on the window, closes the most recently opened and stops
 * the key there -- before React's listener at the app root, so the split
 * never sees an Escape a layer above it already used.
 *
 * An open help card (`[data-focus-return]`, HelpCard.tsx) keeps its own
 * Escape: it is above everything, and it closes first.
 *
 * SO DOES ANY SMALLER LAYER THE KEY WAS PRESSED IN (U10a review). A capture
 * listener sees the key before the layer that holds focus, so without this an
 * Escape in the tenant flyout, a menu or a select closed the whole viewer
 * instead. A focused `<select>` and a spine flyout or menu host close
 * themselves on their own listener, so the key is left to them; an open
 * `<details>` (the log's More) has no Escape of its own, so it is folded
 * here and the focus goes back to its summary.
 */
const OWN_ESCAPE = 'select, .sk-flyout, .sk-spine, .c-menu-host'

const layers: { close: () => void }[] = []

function onKey(e: KeyboardEvent): void {
  if (e.key !== 'Escape' || e.defaultPrevented || layers.length === 0) return
  if (document.querySelector('[data-focus-return]') !== null) return
  const t = e.target instanceof Element ? e.target : null
  if (t?.closest(OWN_ESCAPE)) return
  const details = t?.closest('details[open]')
  if (details instanceof HTMLDetailsElement) {
    e.preventDefault()
    e.stopPropagation()
    details.open = false
    details.querySelector<HTMLElement>(':scope > summary')?.focus()
    return
  }
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
