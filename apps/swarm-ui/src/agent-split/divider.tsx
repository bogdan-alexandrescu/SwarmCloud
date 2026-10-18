import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type RefObject } from 'react'

import {
  SNAPS,
  SNAP_LABEL,
  SNAP_TEXT,
  SNAP_WIDTH,
  listSnap,
  nearestSnap,
  setListSnap,
  type ListSnap,
} from '../listSnap'

// THE SPLIT'S DIVIDER (moved out of AgentSplit.tsx by SPEED-D4A, 2026-10-14,
// unchanged): the list's width as `--list-w`, the fold Changes asks for, and
// the separator that drags or steps the list between its three stops.

/** How long after a keyboard move of the divider the page is held where it was. */
export const HOLD_SCROLL_MS = 400

/**
 * THE DIVIDER NEVER MOVES THE PAGE (owner QA R3, 2026-10-04: the no-jump
 * rule). A keyboard move of the divider shifted the page scroller 0 -> 12:
 * the list's new width re-lays both columns, and the browser re-anchors the
 * page's scroll while it does. The page's scroll offset is recorded at the
 * key and put back on every scroll the move causes, for the few frames the
 * re-layout takes (`HOLD_SCROLL_MS`); then the page is the reader's again.
 */
export function holdPageScroll(from: Element | null): void {
  const scroller = from?.closest<HTMLElement>('.ctl-scroll') ?? null
  if (scroller === null) return
  const top = scroller.scrollTop
  const keep = () => {
    if (scroller.scrollTop !== top) scroller.scrollTop = top
  }
  scroller.addEventListener('scroll', keep)
  setTimeout(() => scroller.removeEventListener('scroll', keep), HOLD_SCROLL_MS)
}

/** Publishes the list's width, and folds it to its strip while Changes is open. */
export function useListWidth(snap: ListSnap, onChanges: boolean): void {
  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--list-w', SNAP_WIDTH[snap])
    root.dataset.agentList = snap
    return () => {
      root.style.removeProperty('--list-w')
      delete root.dataset.agentList
    }
  }, [snap])

  // OPENING CHANGES FOLDS THE LIST TO ITS STRIP (diff-viewer.md §2 variant 2:
  // at 1280 that gives the file about 850px, and split needs 700). REMEMBERED,
  // NOT FORCED: the fold is not written to the device (`remember` false), a
  // reader who opens the list again with `[` or the divider keeps it open, and
  // leaving the tab puts back the width the list had -- unless the reader
  // moved it while on Changes, which is then theirs.
  useEffect(() => {
    if (!onChanges) return
    const was = listSnap()
    if (was === 'strip') return
    setListSnap('strip', false)
    return () => {
      if (listSnap() === 'strip') setListSnap(was, false)
    }
  }, [onChanges])
}

/** The separator between the list and the agent, and the stops it snaps to while it is dragged. */
export function AgDivider({ snap, panel }: { snap: ListSnap; panel: RefObject<HTMLDivElement> }) {
  const [dragAt, setDragAt] = useState<ListSnap | null>(null)
  const dragging = useRef(false)

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return
    // The list's width is the distance from the work area's left edge to the
    // pointer, snapped to the nearest stop -- and the stop lights up.
    const app = panel.current?.closest('.app')?.getBoundingClientRect()
    if (app === undefined) return
    const next = nearestSnap(e.clientX - app.left, app.width)
    setDragAt(next)
    setListSnap(next, false)
  }
  const endDrag = () => {
    if (!dragging.current) return
    dragging.current = false
    setListSnap(dragAt ?? snap)
    setDragAt(null)
  }
  return (
    <>
      <div
        className={`ctl-inspector-grip ag-divider${dragAt !== null ? ' is-dragging' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the agent list"
        /*
         * THE WAI-ARIA WINDOW-SPLITTER PATTERN: a focusable separator that
         * moves on the arrow keys and reports its position. RIGHT WIDENS THE
         * LIST, which is what dragging the divider right does.
         *
         * A HANDLE YOU CAN SEE (#503): this was a transparent 6px grip, so the
         * one control that resizes the list was found only by hovering the
         * right pixel. V1 draws a handle on the divider; `.ag-divider::after`
         * is it, and while it is dragged the three stops are drawn with the
         * nearest one lit.
         */
        tabIndex={0}
        aria-valuenow={SNAPS.indexOf(snap)}
        aria-valuemin={0}
        aria-valuemax={SNAPS.length - 1}
        aria-valuetext={SNAP_TEXT[snap]}
        onKeyDown={(e) => {
          const i = SNAPS.indexOf(snap)
          // HOME AND END ARE THE ENDS (live QA WA-16): the separator says
          // `aria-valuemin=0` and `aria-valuemax=2`, and the window-splitter
          // pattern moves it to those on Home and End; they did nothing.
          const to =
            e.key === 'Home'
              ? 0
              : e.key === 'End'
                ? SNAPS.length - 1
                : e.key === 'ArrowRight'
                  ? i + 1
                  : e.key === 'ArrowLeft'
                    ? i - 1
                    : null
          if (to === null) return
          e.preventDefault()
          holdPageScroll(panel.current)
          setListSnap(SNAPS[Math.max(0, Math.min(SNAPS.length - 1, to))]!)
        }}
        onPointerDown={(e) => {
          dragging.current = true
          setDragAt(snap)
          e.currentTarget.setPointerCapture?.(e.pointerId)
        }}
        onPointerMove={onMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      />
      {dragAt !== null && (
        <div className="ag-snaps" role="status" aria-live="polite">
          {SNAPS.map((s) => (
            <span key={s} className={`ag-snap${s === dragAt ? ' is-on' : ''}`}>
              {SNAP_LABEL[s]}
            </span>
          ))}
          <span className="ag-snap-say">release to snap to {SNAP_LABEL[dragAt].split(' · ')[0]}</span>
        </div>
      )}
    </>
  )
}
