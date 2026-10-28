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
export function useSplitFocus(panel: RefObject<HTMLDivElement>, close: () => void, taskId: string | null = null): void {
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
  // UNDER THE PHONE HEADER IT IS A REGION (QA-116): below 760px it starts at
  // the header's foot and the header and its menu stay usable above it, so
  // a modal dialog would hide from a screen reader, and keep Tab from, the
  // controls in plain view. The back link and Escape still close it.
  useEffect(() => {
    const el = panel.current
    if (el === null) return
    const mark = () => {
      el.setAttribute('role', underHeader(el) ? 'region' : 'dialog')
      if (isModal(el)) el.setAttribute('aria-modal', 'true')
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
    // FOCUS MOVES IN WITHOUT SCROLLING THE PAGE (visual QA pass 1,
    // overview-agents-55): a plain `focus()` scrolled the page column `.ctl-scroll`
    // to bring the panel into view, 37px at 1440, and the `Agents` heading went
    // under the sticky breadcrumb. The panel is placed by its own CSS; nothing
    // behind it needs to move.
    if (el !== null && isOverlay(el)) el.focus(openedByAddress(came) ? { ...QUIET, ...STILL } : STILL)
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

  // A KEY THAT OPENED AN AGENT MOVES FOCUS INTO IT (visual QA run 2,
  // a11y-keyboard-002). Above, focus moves in only over an overlay; beside
  // the list at 1440, Enter on a row opened the panel and left focus on the
  // row, and the panel's first control was the 9th Tab stop. A row opened by
  // a key asks (`focusSplitOnOpen`), and the split showing that agent takes
  // the request -- on its first open, or when it swaps to that agent -- and
  // makes the asking row the place focus goes back to on close. A pointer
  // opens no request, so a click beside the list still leaves focus where it
  // was (V152).
  const shows = useRef(taskId)
  shows.current = taskId
  useEffect(() => {
    const take = () => {
      if (asked === null || asked.taskId !== shows.current) return
      const from = asked.from
      asked = null
      opener.current = from
      panel.current?.focus(STILL)
    }
    takers.add(take)
    take()
    return () => {
      takers.delete(take)
    }
  }, [taskId, panel])
}

/** The open a key asked for, until the split showing that agent takes it. */
let asked: { taskId: string; from: HTMLElement } | null = null
/** Each mounted split's taker: one asked for an agent already open takes it at once. */
const takers = new Set<() => void>()

/**
 * Asks the split to take focus when it shows `taskId`, and to give it back to
 * `from` when it closes. Called by a list row opened by a key (Agents.tsx).
 */
export function focusSplitOnOpen(taskId: string, from: HTMLElement): void {
  asked = { taskId, from }
  for (const take of [...takers]) take()
}

/**
 * WHETHER THE SPLIT STARTS UNDER THE PHONE HEADER (QA-116, owner 2026-10-20):
 * an overlay whose top is below the screen's. Read off the computed `top`,
 * which the phone rule sets to the header's height (agents.css,
 * `--sk-pbar-h`), so the markup cannot disagree with the layout; between
 * 760 and 1100px the overlay is at 0 and covers everything.
 */
export function underHeader(panel: HTMLElement): boolean {
  if (!isOverlay(panel)) return false
  const view = panel.ownerDocument.defaultView
  return view !== null && parseFloat(view.getComputedStyle(panel).top) > 0
}

/** Whether the split is modal: an overlay over the whole screen, header and all. Tab is held in it only then. */
export function isModal(panel: HTMLElement): boolean {
  return isOverlay(panel) && !underHeader(panel)
}

/**
 * FOCUS WITHOUT THE RING WHEN NOTHING OPENED THE SPLIT (V262, VQA2-R13).
 * `/agents/<tab>/<id>` loaded on a phone mounts the drawer before any click or
 * key, so the browser judged its programmatic focus keyboard-driven and
 * `.ctl-drawer:focus-visible` (styles.css, AG-31) drew a 2px ring round the
 * whole screen. That ring is right when a key opened the drawer -- the reader
 * must see where focus went -- so it is kept there, and only an open that no
 * element caused (focus on `<body>` or nowhere) asks for no ring.
 */
const QUIET: FocusOptions & { focusVisible?: boolean } = { focusVisible: false }

/** Focus that leaves every scroll container where it was (overview-agents-55). */
const STILL: FocusOptions = { preventScroll: true }

/** Whether the split opened with nothing focused: by its address, not from a row or a key. */
export function openedByAddress(came: Element | null): boolean {
  return came === null || (typeof document !== 'undefined' && came === document.body)
}
