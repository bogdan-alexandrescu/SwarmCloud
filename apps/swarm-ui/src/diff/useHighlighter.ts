// THE HIGHLIGHTER IS FETCHED WHEN A FILE NEEDS IT, NOT WITH THE PAGE.
//
// `highlight.ts` is reached only through the dynamic import below, so the
// bundler puts it in a chunk of its own, and nothing downloads it until the
// viewer shows a file whose language is known (`lang.ts`). Until it arrives
// -- and for good, if the fetch fails -- the file is drawn as plain text, which
// is what it was before the highlighter existed: colour is an addition, never
// a gate on reading the diff.
//
// The module is loaded once per page. A failed load is forgotten, so the next
// file shown tries again rather than staying plain until a reload.

import { useEffect, useState } from 'react'

export type Highlighter = typeof import('./highlight')

let loaded: Highlighter | null = null
let pending: Promise<Highlighter> | null = null

export function loadHighlighter(): Promise<Highlighter> {
  if (pending === null) {
    pending = import('./highlight').then(
      (m) => {
        loaded = m
        return m
      },
      (err: unknown) => {
        pending = null
        throw err
      },
    )
  }
  return pending
}

/** The highlighter once loaded, when `want`; null while it loads, when it failed, or when not wanted. */
export function useHighlighter(want: boolean): Highlighter | null {
  const [mod, setMod] = useState<Highlighter | null>(loaded)
  useEffect(() => {
    if (!want || mod !== null) return
    let live = true
    loadHighlighter().then(
      (m) => {
        if (live) setMod(m)
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [want, mod])
  return want ? mod : null
}
