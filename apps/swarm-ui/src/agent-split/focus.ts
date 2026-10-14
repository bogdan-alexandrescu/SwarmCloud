import { useEffect, useRef, type RefObject } from 'react'

import { isOverlay } from '../focus'

// THE SPLIT'S FOCUS (moved out of AgentSplit.tsx by SPEED-D4A, 2026-10-14,
// unchanged): Escape from outside the panel, modal only where it covers the
// list, and focus in and back out.

/**
 * Closes the split on an Escape pressed outside it, captures what held focus
 * as the split first renders, marks the panel modal while it is an overlay,
 * moves focus in over the list and puts it back on the way out.
 */
export function useSplitFocus(panel: RefObject<HTMLDivElement>, close: () => void): void {
  /** The element that was focused when this opened. Where Escape puts you back. */
  const opener = useRef<Element | null>(null)
  /** What held focus when this first rendered, and which agent's row it was in. */
  const born = useRef<{ el: Element | null; rowId: string | null } | null>(null)
  if (born.current === null) {
    const a = typeof document === 'undefined' ? null : document.activeElement
    born.current = {
      el: a,
      rowId: a instanceof HTMLElement ? (a.closest('[data-task-id]')?.getAttribute('data-task-id') ?? null) : null,
    }
  }

  // ESCAPE CLOSES THE SPLIT WHEREVER FOCUS IS (V152, visual QA #1038). The
  // panel's own `onKeyDown` only hears keys pressed inside it, and a mouse
  // click on a row opens the split beside the list WITHOUT moving focus (it
  // moves in only over an overlay, below), so the key went to the row and
  // nothing closed. This listener takes the key from anywhere outside the
  // panel, in the bubble phase: a layer above (`escape.ts`, capture phase), an
  // open help card, a select, a menu or flyout, a text field and any other
  // dialog all keep their own Escape, exactly as they do inside the panel.
  const closeRef = useRef(close)
  closeRef.current = close
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return
      if (document.querySelector('[data-focus-return]') !== null) return
      // An open theater (Theater.tsx) is above the split: its Escape closes it
      // and only it, back to the Browser tab (LB-V2, visual QA pass 5).
      if (document.querySelector('[data-theater]') !== null) return
      const t = e.target instanceof Element ? e.target : null
      if (t !== null && panel.current?.contains(t)) return
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (t?.closest('.sk-flyout, .sk-spine, .c-menu-host, [role="dialog"], [role="menu"], details[open]')) return
      closeRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /*
   * FOCUS, ON THE WAY IN AND ON THE WAY OUT (moved with the split; see the
   * history of App.tsx `AgentDrawer`). Focus moves in only when the panel
   * covers the list; on the way out it goes back to what opened it, or to the
   * row of the agent it showed, found again by id because the list draws a
   * different row node while an agent is open.
   */
  // MODAL WHERE IT COVERS THE LIST (live QA WA-17): below 1100px the split is
  // an overlay that traps Tab, and it said `role=dialog` without
  // `aria-modal`, so a screen reader still offered the list behind it.
  // Beside the list it is a column, not modal, and says nothing.
  useEffect(() => {
    const el = panel.current
    if (el === null) return
    const mark = () => {
      if (isOverlay(el)) el.setAttribute('aria-modal', 'true')
      else el.removeAttribute('aria-modal')
    }
    mark()
    globalThis.addEventListener?.('resize', mark)
    return () => globalThis.removeEventListener?.('resize', mark)
  }, [])

  useEffect(() => {
    const el = panel.current
    const { el: came, rowId } = born.current ?? { el: null, rowId: null }
    opener.current = came
    if (el !== null && isOverlay(el)) el.focus()
    return () => {
      const back = opener.current
      opener.current = null
      if (back instanceof HTMLElement && back.isConnected && back !== document.body) {
        back.focus()
        return
      }
      if (rowId !== null) {
        const again = [...document.querySelectorAll<HTMLElement>('.row.clickable[data-task-id]')].find(
          (r) => r.getAttribute('data-task-id') === rowId,
        )
        again?.focus()
      }
    }
    // Once per open. `taskId` changing swaps the CONTENTS of an open split and
    // must not re-capture an opener that is now inside it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}
