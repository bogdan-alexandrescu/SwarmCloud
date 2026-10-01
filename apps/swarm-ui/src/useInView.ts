import { useEffect, useState } from 'react'

/**
 * How far below (and above) the viewport a card counts as in view. A card
 * starts reading this far before it scrolls on screen, so the next one is
 * usually drawn by the time it appears -- small enough that a page open reads
 * only what is near the fold (#377).
 */
export const CARD_ROOT_MARGIN = '200px 0px'

/**
 * Whether the element the returned callback ref is attached to is within
 * `rootMargin` of the viewport, live: it goes back to false when the element
 * scrolls away, so a read in flight can be dropped rather than drawn late.
 *
 * WITHOUT IntersectionObserver IT IS ALWAYS IN VIEW. A browser that lacks the
 * API (and jsdom) must still draw the card, so it reads at once, exactly as the
 * page did before reads were lazy. The check is made on every render rather
 * than at import, so a test can install a double.
 */
export function useInView<T extends Element>(rootMargin: string = CARD_ROOT_MARGIN): [(node: T | null) => void, boolean] {
  const supported = typeof globalThis.IntersectionObserver === 'function'
  const [node, setNode] = useState<T | null>(null)
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    if (!supported) return
    if (node === null) {
      setSeen(false)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.target === node) setSeen(e.isIntersecting)
      },
      { rootMargin },
    )
    io.observe(node)
    return () => io.disconnect()
  }, [node, rootMargin, supported])
  return [setNode, supported ? seen : true]
}

const REDUCE = '(prefers-reduced-motion: reduce)'

function prefersReduced(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(REDUCE).matches
}

/**
 * The reader's `prefers-reduced-motion`, followed as it changes. False where
 * there is no `matchMedia` (jsdom), which is the default answer. The sheet
 * stops `.ctl-pending`'s sweep under the same query; a component that draws a
 * different placeholder for it needs the answer in JS.
 */
export function useReducedMotion(): boolean {
  const [reduce, setReduce] = useState(prefersReduced)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(REDUCE)
    const on = () => setReduce(query.matches)
    if (typeof query.addEventListener !== 'function') return
    query.addEventListener('change', on)
    return () => query.removeEventListener('change', on)
  }, [])
  return reduce
}
