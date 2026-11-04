import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from 'react'

import { clampPane, readPane, writePane, type PaneSpec } from '../panes'
import './inspectorShell.css'

/**
 * THE WORK SECTION'S DOCKED INSPECTOR (the owner's pick of 2026-11-03: the
 * Graph Work redesign, "Graph with a docked inspector").
 *
 * Every Work screen -- Agents, Workflows, one workflow -- is the same two
 * panes: a dense one-line-per-row surface on the left and a resizable
 * inspector on the right. Whatever the reader picks opens in the inspector and
 * THE LEFT PANE NEVER MOVES, so the row they came from stays where it was.
 *
 * What this shell owns, so that no page re-implements it:
 *
 * - the drag handle, by pointer and by keyboard (the WAI-ARIA window-splitter
 *   pattern: ArrowLeft widens the inspector, ArrowRight narrows it, Home and
 *   End are its ends), clamped so neither pane can be dragged out of reach;
 * - the width, remembered PER SCREEN on this device, read and written through
 *   `panes.ts` (a private window throws on `localStorage`; that loses the
 *   preference and nothing else);
 * - the fold to a rail and back -- a page under 1366px asks for the
 *   inspector to start folded (`startFolded`), so a workflow's grid keeps its
 *   times and pull requests at 1280;
 * - the header slot (title, state, buttons) and close;
 * - the phone: one column, and the inspector a full-screen sheet with a back
 *   control, shown only once something is picked (`phoneOpen`);
 * - the left pane's WIDTH CLASS, so a page can drop columns as the left pane
 *   narrows: `data-width` on the pane, `onWidthClass` for the page, and the
 *   `work-main` container for the page's own `@container` rules.
 */

/** The narrowest the inspector is dragged: below this its tabs and header stop fitting. */
export const INSPECTOR_MIN = 340
/** The narrowest the left pane is left: the name and state columns of a row. */
export const MAIN_MIN = 360
/** The widest the inspector goes when the page's width is not known yet. */
export const INSPECTOR_MAX_FALLBACK = 960
/** What the inspector opens at before the reader has dragged it on this screen. */
export const INSPECTOR_INITIAL = 440
/** One keyboard step of the handle. */
export const KEY_STEP = 40
/** The handle's own column, in px (d.css: `9px`). */
export const HANDLE_W = 9
/** The folded rail's column, in px. */
export const RAIL_W = 36
/** Under this viewport width a page starts the inspector folded at rest (graph-notes: D·8b at 1280). */
export const FOLD_BELOW = 1366
/** The phone: one column, the inspector a sheet. The same width as `inspectorShell.css`'s media query. */
export const PHONE_QUERY = '(max-width: 720px)'

/**
 * The left pane's width class, widest first. The bounds are the ones the
 * design's columns give way at (d.css): over 700 every column fits; at 700 the
 * grid's pull-request numbers go; at 560 cost, live and the legend go; at 420
 * a row is its name and state.
 */
export type WidthClass = 'wide' | 'medium' | 'narrow' | 'slim'

export function widthClassOf(px: number): WidthClass {
  if (px > 700) return 'wide'
  if (px > 560) return 'medium'
  if (px > 420) return 'narrow'
  return 'slim'
}

/** The storage spec of one screen's inspector width. */
export function inspectorSpec(screen: string, initial: number = INSPECTOR_INITIAL): PaneSpec {
  return {
    key: `swarm.work.inspector.${screen}.w`,
    min: INSPECTOR_MIN,
    max: INSPECTOR_MAX_FALLBACK,
    initial: clampPane(initial, INSPECTOR_MIN, INSPECTOR_MAX_FALLBACK),
  }
}

/** The widest the inspector may be inside a page this wide (0: not measured yet). */
export function inspectorMax(pageWidth: number): number {
  if (!(pageWidth > 0)) return INSPECTOR_MAX_FALLBACK
  return Math.max(INSPECTOR_MIN, Math.min(INSPECTOR_MAX_FALLBACK, pageWidth - MAIN_MIN - HANDLE_W))
}

/** Whether a page this wide starts the inspector folded at rest. */
export function startsFolded(viewportWidth: number): boolean {
  return viewportWidth > 0 && viewportWidth < FOLD_BELOW
}

function subscribePhone(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const mq = window.matchMedia(PHONE_QUERY)
  mq.addEventListener?.('change', onChange)
  return () => mq.removeEventListener?.('change', onChange)
}

/** Whether the viewport is phone-width. False where there is no `matchMedia`. */
function phoneNow(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(PHONE_QUERY).matches
}

export function useIsPhone(): boolean {
  return useSyncExternalStore(subscribePhone, phoneNow, () => false)
}

/** The page's own width, kept current by a ResizeObserver where there is one and the window's resize otherwise. */
function usePageWidth(root: RefObject<HTMLDivElement>): number {
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = root.current
    if (el === null) return
    const measure = () => setW(Math.round(el.getBoundingClientRect().width))
    measure()
    if (typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(measure)
      ro.observe(el)
      return () => ro.disconnect()
    }
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [root])
  return w
}

export interface InspectorProps {
  /** Which screen this is ("agents", "workflows", "workflow"): the width is remembered per screen. */
  screen: string
  /** The left pane: the dense table or grid. It never moves when the inspector changes. */
  main: ReactNode
  /** The inspector's body. Null: nothing to inspect, the left pane takes the page. */
  children: ReactNode | null
  /** The header slot: the subject's title. */
  title?: ReactNode
  /** The header slot: the subject's state (a state pill, an elapsed time). */
  state?: ReactNode
  /** The header slot: the subject's buttons (copy link, take control, stop). */
  actions?: ReactNode
  /** Close the subject. Without it the header has no close button. */
  onClose?: () => void
  /** The inspector's accessible name ("Agent inspector", "Workflow inspector"). */
  label?: string
  /** What the inspector opens at before the reader drags it on this screen. */
  initialWidth?: number
  /** Start folded to the rail (a page under `FOLD_BELOW` at rest asks for this). */
  startFolded?: boolean
  /** On a phone the inspector is a sheet over the page, shown only once something is picked. */
  phoneOpen?: boolean
  /** The phone sheet's back control. Defaults to `onClose`. */
  onBack?: () => void
  /** The back control's words, naming where it goes ("Agents"). */
  backLabel?: string
  /** Called with the left pane's width class whenever it changes. */
  onWidthClass?: (cls: WidthClass) => void
  /** Called when the inspector folds (true) or unfolds (false). */
  onFold?: (folded: boolean) => void
  /**
   * What the inspector shows (a picked step's id); each new non-null value
   * unfolds a folded rail, so a pick on a page that started folded opens.
   */
  openFor?: string | null
}

/** The page: the left pane, a drag handle, the docked inspector -- or its rail, or on a phone its sheet. */
export function Inspector({
  screen,
  main,
  children,
  title,
  state,
  actions,
  onClose,
  label = 'Inspector',
  initialWidth,
  startFolded = false,
  phoneOpen = false,
  onBack,
  backLabel,
  onWidthClass,
  onFold,
  openFor = null,
}: InspectorProps) {
  const root = useRef<HTMLDivElement>(null)
  const spec = inspectorSpec(screen, initialWidth)
  const [stored, setStored] = useState(() => readPane(spec))
  const [folded, setFolded] = useState(startFolded)
  const [dragging, setDragging] = useState(false)
  const drag = useRef<{ right: number; at: number } | null>(null)
  const phone = useIsPhone()
  const pageWidth = usePageWidth(root)

  // A remount on another screen reads that screen's own width.
  useEffect(() => {
    setStored(readPane(inspectorSpec(screen, initialWidth)))
  }, [screen, initialWidth])

  const max = inspectorMax(pageWidth)
  const width = clampPane(stored, INSPECTOR_MIN, max)
  const has = children !== null && children !== undefined && children !== false
  const docked = has && !folded && !phone
  const mainWidth = pageWidth - (docked ? width + HANDLE_W : has && !phone ? RAIL_W : 0)
  const widthClass: WidthClass = pageWidth > 0 ? widthClassOf(mainWidth) : 'wide'

  const report = useRef(onWidthClass)
  report.current = onWidthClass
  useEffect(() => {
    report.current?.(widthClass)
  }, [widthClass])

  const setWidth = useCallback(
    (px: number, remember = true) => {
      const next = clampPane(px, INSPECTOR_MIN, max)
      setStored(next)
      if (remember) writePane(inspectorSpec(screen, initialWidth), next)
    },
    [max, screen, initialWidth],
  )

  const fold = (to: boolean) => {
    setFolded(to)
    onFold?.(to)
  }

  // A NEW PICK OPENS THE RAIL: a page folded at rest (1280) would otherwise
  // take a click on a step and show nothing for it.
  const opened = useRef(openFor)
  useEffect(() => {
    if (openFor !== null && openFor !== opened.current && folded) {
      setFolded(false)
      onFold?.(false)
    }
    opened.current = openFor
  }, [openFor, folded, onFold])

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    const box = root.current?.getBoundingClientRect()
    if (box === undefined) return
    drag.current = { right: box.right, at: width }
    setDragging(true)
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current === null) return
    // The inspector's width is the distance from the pointer to the page's right edge.
    const at = clampPane(drag.current.right - e.clientX, INSPECTOR_MIN, max)
    drag.current.at = at
    setWidth(at, false)
  }
  // The release remembers where the last move left the inspector (the up event's own position is not read).
  const endDrag = () => {
    if (drag.current === null) return
    const { at } = drag.current
    drag.current = null
    setDragging(false)
    setWidth(at)
  }
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const to =
      e.key === 'ArrowLeft'
        ? width + KEY_STEP
        : e.key === 'ArrowRight'
          ? width - KEY_STEP
          : e.key === 'Home'
            ? INSPECTOR_MIN
            : e.key === 'End'
              ? max
              : null
    if (to === null) return
    e.preventDefault()
    setWidth(to)
  }

  const back = onBack ?? onClose
  const sheet = phone && has && phoneOpen
  useEffect(() => {
    if (!sheet || back === undefined) return
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') back()
    }
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [sheet, back])

  const cls = [
    'wi',
    has && !phone ? (folded ? 'is-folded' : 'is-docked') : 'is-alone',
    phone ? 'is-phone' : '',
    sheet ? 'is-sheet' : '',
    dragging ? 'is-dragging' : '',
  ]
    .filter(Boolean)
    .join(' ')

  const head = (
    <div className="wi-head">
      {sheet && back !== undefined && (
        <button type="button" className="wi-back" onClick={back}>
          ← {backLabel ?? 'Back'}
        </button>
      )}
      <div className="wi-title">
        {title !== undefined && <span className="wi-title-text">{title}</span>}
        {state !== undefined && <span className="wi-state">{state}</span>}
      </div>
      {actions !== undefined && <div className="wi-actions">{actions}</div>}
      {!phone && (
        <button
          type="button"
          className="wi-btn wi-fold"
          onClick={() => fold(true)}
          aria-label="Fold the inspector"
          title="Fold the inspector"
        >
          ⇥
        </button>
      )}
      {onClose !== undefined && !sheet && (
        <button type="button" className="wi-btn wi-x" onClick={onClose} aria-label="Close the inspector" title="Close the inspector">
          ×
        </button>
      )}
    </div>
  )

  return (
    <div ref={root} className={cls} style={{ ['--wi-dock' as string]: `${width}px` }} data-screen={screen}>
      <div className="wi-main" data-width={widthClass}>
        {main}
      </div>
      {has && !phone && folded && (
        <div className="wi-rail">
          <button
            type="button"
            className="wi-btn wi-unfold"
            onClick={() => fold(false)}
            aria-label="Open the inspector"
            title="Open the inspector"
          >
            ⇤
          </button>
          {title !== undefined && <span className="wi-rail-title">{title}</span>}
        </div>
      )}
      {docked && (
        <div
          className="wi-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the inspector"
          aria-valuenow={width}
          aria-valuemin={INSPECTOR_MIN}
          aria-valuemax={max}
          aria-valuetext={`inspector ${width} px wide`}
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onKeyDown={onKeyDown}
        />
      )}
      {(docked || sheet) && (
        <aside className="wi-dock" aria-label={label} role={sheet ? 'dialog' : undefined} aria-modal={sheet ? true : undefined}>
          {head}
          <div className="wi-body">{children}</div>
        </aside>
      )}
    </div>
  )
}
