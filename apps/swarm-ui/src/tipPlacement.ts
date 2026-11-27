/**
 * WHERE A HELP MARK'S TIP SITS, WRITTEN ONCE (the rendered review of
 * 2026-11-26: a tip drawn inside a dialog's scroller widened it, and a tip
 * behind a line in the split drawer ran past the drawer's edge).
 *
 * THE RULE. The tip stays inside its nearest clipping container (the first
 * ancestor whose overflow is not visible, else the window): when it would
 * overflow it shifts toward the inside, and when it is wider than the
 * container it wraps to the container's width. `left` is the tip's offset
 * from the mark's own left edge (the tip is `position: absolute` in the
 * mark's host). Pure numbers, no DOM, so a unit test drives it with a narrow
 * container; `placeTipIn` is the one DOM reader.
 */

export interface Span {
  left: number
  right: number
}

/** The gap kept between a tip and its container's edge. */
export const TIP_EDGE_GAP = 8

/**
 * `host` is the mark's box, `container` the clip box, `tipWidth` the tip's
 * natural width. `end` anchors the tip's right edge to the mark's.
 * Returns the tip's offset from the mark's left edge and the widest it may be.
 */
export function placeTip(host: Span, container: Span, tipWidth: number, end: boolean): { left: number; maxWidth: number } {
  const room = Math.max(0, container.right - container.left - 2 * TIP_EDGE_GAP)
  const width = Math.min(tipWidth, room)
  const wanted = end ? host.right - width : host.left
  const lo = container.left + TIP_EDGE_GAP
  const hi = container.right - TIP_EDGE_GAP - width
  const at = Math.max(lo, Math.min(wanted, Math.max(lo, hi)))
  return { left: at - host.left, maxWidth: room }
}

/** The nearest ancestor that clips (or scrolls) its content, else null for the window. */
export function clipParent(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p !== null; p = p.parentElement) {
    const s = getComputedStyle(p)
    if (s.overflowX !== 'visible' || s.overflowY !== 'visible') return p
  }
  return null
}

/** Place `tip` (a shown, absolutely positioned tooltip) inside its clip container, beside `host`. */
export function placeTipIn(host: HTMLElement, tip: HTMLElement, end: boolean): void {
  const c = clipParent(host)
  const box = c === null ? { left: 0, right: document.documentElement.clientWidth } : c.getBoundingClientRect()
  // Inside a scroller the visible box is its client box, not the border box (the scrollbar is not room).
  const right = c === null ? box.right : box.left + c.clientLeft + c.clientWidth
  const container = { left: box.left + (c === null ? 0 : c.clientLeft), right }
  tip.style.maxWidth = ''
  tip.style.left = '0px'
  tip.style.right = 'auto'
  const room = Math.max(0, container.right - container.left - 2 * TIP_EDGE_GAP)
  tip.style.maxWidth = `${room}px`
  const h = host.getBoundingClientRect()
  const w = tip.getBoundingClientRect().width
  const at = placeTip({ left: h.left, right: h.right }, container, w, end)
  tip.style.left = `${at.left}px`
  tip.style.maxWidth = `${at.maxWidth}px`
}
