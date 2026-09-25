import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { DataSourceCells } from './DataSources'
import { probeSnapshot, subscribeProbes } from './fetch'
import { nudgePane } from './focus'
import { helpAnchor, type TopicId } from './help'
import { timeAgo } from './Shell'
import { DOCK, DOCK_COLLAPSED, clampPane, readPane, summariseProbes, writePane } from './panes'
import { AGE_TICK_MS, useNow } from './useNow'

/**
 * THE DOCK (§B3), AND THE TELEMETRY STRIP IT SWALLOWS (§B18).
 *
 * WHAT WAS WRONG. The provenance strip rendered sixteen cards, about 200px
 * tall, at the bottom of all fifteen routes, identical on every one of them.
 * It is genuinely useful -- it is the only thing on the page that says a
 * figure is four minutes old while its route has been failing for three of
 * them -- so §B18 is explicit that it must not be deleted. The defect is that
 * it outranked the page's own content for vertical space on every short
 * screen, and repeated itself fifteen times to say the same thing.
 *
 * SO IT COLLAPSES TO ONE LINE and expands on click. The collapsed line carries
 * the four facts that decide whether to expand it:
 *
 *     16 reads · p95 382ms · 0 failed · 1 admin-only · newest 9s
 *
 * EVERY ONE OF THOSE IS A MEASUREMENT AND THE LABELS SAY WHICH. "16 reads" is
 * sixteen ROUTES, not sixteen requests -- the registry holds one record per
 * route -- so the word is "routes" in the expanded panel where there is room
 * for the accurate noun, and the p95's own label says it is taken over the
 * last attempt of each route rather than over a request history this app does
 * not keep. A p95 that quietly means something else is the same class of
 * defect as a count computed over a partial read.
 *
 * "admin-only" IS NOT "failed", and is counted apart, because a non-admin
 * genuinely cannot read `/v1/admin/*` and a console that reports that as a
 * fault is a console reporting itself broken.
 *
 * IT SURVIVES NAVIGATION. The dock is rendered by App, outside the routed
 * body, so expanding it and moving to another section does not close it --
 * §B3's "it survives navigation within a session".
 *
 * THE HEIGHT IS THE VIEWER'S. Dragging the top edge resizes it, clamped to
 * 120px-70vh (§B3), and the size is remembered per viewer in `localStorage`
 * through `panes.ts`, which reads and writes inside `try/catch`: a private
 * window must lose a preference, not the shell.
 */

/**
 * The Help topic that explains this strip (§B18: "full detail in Help").
 *
 * Typed as a `TopicId` rather than spelled into the href, so a renamed topic
 * is a compile error here instead of a `?` link that lands on the top of the
 * Help page and answers nothing.
 */
const HELP_TOPIC: TopicId = 'api-reads'

export function Dock() {
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  // The ages on this strip are the whole point of it, so they move on their
  // own rather than only when a fetch happens to land -- on the SHARED clock
  // (useNow.ts) the head and every screen's sub-line read, so the dock's
  // `newest 22s ago` and a sub-line's `read just now` are one instant (CH-1).
  const now = useNow(AGE_TICK_MS)
  const [open, setOpen] = useState(false)
  const [height, setHeight] = useState(() => readPane(DOCK))
  const dragging = useRef(false)
  const shell = useRef<HTMLDivElement | null>(null)
  const s = summariseProbes(probes)
  const drawn = probes.length > 0

  // `--dock-h`: WHAT IT IS STILL FOR, NOW THAT NOTHING RESERVES SPACE.
  //
  // THE OVERLAP IS FIXED IN THE FRAME, NOT HERE. This used to be
  // `position: fixed` over a scrolling document, and `.app` bought the space
  // back as bottom padding through this property. Both are gone: `.ctl-frame`
  // is a two-row grid and this component is row 2, so the bar cannot overlap
  // the scroller at any offset and there is no reservation left to keep in
  // step. See `App.tsx`'s frame comment and design-system §3.4.
  //
  // THE PROPERTY SURVIVES BECAUSE TWO STICKY COLUMNS STILL NEED IT. `.ctl-rail`
  // and the inspector size themselves against the SCROLLPORT -- the viewport
  // minus this row -- and neither can express that in CSS without knowing this
  // height. Dragging the dock taller shortens both, which is correct and is
  // the whole reason it is measured rather than assumed.
  //
  // IT IS THE MEASURED BOX, NOT `open ? height : DOCK_COLLAPSED`. That
  // arithmetic is what the dock INTENDS to be, and it was wrong in two states
  // that are both on screen today:
  //
  //   * COLLAPSED WITH AN EXPIRED SESSION. The re-auth button sits outside the
  //     disclosure on purpose, so a collapsed dock is the 28px line PLUS a
  //     button, and the arithmetic says 28.
  //   * NO DOCK AT ALL. Before any route has been called this component
  //     renders nothing, and the arithmetic still claims 28px.
  //
  // Reading the box removes the class of bug rather than the two instances:
  // anything that changes this element's height -- a drag, a wrap at 390px, a
  // future row -- moves both readers with it. The fallback is the old
  // arithmetic, for jsdom (no layout engine, every box is 0px high) and for
  // any browser without ResizeObserver.
  useEffect(() => {
    const root = document.documentElement
    const el = shell.current
    if (el === null) {
      root.style.setProperty('--dock-h', '0px')
      return () => {
        root.style.removeProperty('--dock-h')
      }
    }
    const assumed = open ? height : DOCK_COLLAPSED
    const publish = () =>
      root.style.setProperty(
        '--dock-h',
        `${Math.round(el.getBoundingClientRect().height || assumed)}px`,
      )
    publish()
    const Observer = globalThis.ResizeObserver
    if (typeof Observer !== 'function') {
      return () => {
        root.style.removeProperty('--dock-h')
      }
    }
    const ro = new Observer(publish)
    ro.observe(el)
    return () => {
      ro.disconnect()
      root.style.removeProperty('--dock-h')
    }
  }, [open, height, drawn, s.expired])

  const ceiling = useCallback(
    () => Math.max(DOCK.min, Math.round((globalThis.innerHeight || DOCK.max) * 0.7)),
    [],
  )

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!open) return
      dragging.current = true
      e.currentTarget.setPointerCapture(e.pointerId)
    },
    [open],
  )

  const onPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!dragging.current) return
      // The dock grows upward, so the height is the distance from the pointer
      // to the bottom of the viewport.
      const next = (globalThis.innerHeight || 0) - e.clientY
      setHeight(clampPane(next, DOCK.min, ceiling()))
    },
    [ceiling],
  )

  const endDrag = useCallback(() => {
    if (!dragging.current) return
    dragging.current = false
    writePane(DOCK, height)
  }, [height])

  if (!drawn) {
    // NOTHING HAS BEEN READ YET, so there is nothing to be provenance ABOUT.
    // An empty dock would be a bar claiming a summary of no reads; the strip
    // has always rendered nothing in this case and that stays true.
    return null
  }

  const tone = s.expired ? 'is-bad' : s.failed > 0 ? 'is-warn' : 'is-ok'

  return (
    <div
      ref={shell}
      className={`ctl-dock${open ? ' is-open' : ''}`}
      style={open ? { height } : undefined}
    >
      {open && (
        <div
          className="ctl-dock-grip"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize the provenance panel"
          /*
           * THE SAME DEFECT AS THE INSPECTOR'S GRIP, in the other axis: four
           * pointer handlers on a `<div>` and no keyboard path at all, so the
           * one control that decides how much of the screen the provenance
           * panel takes could only be moved by dragging. See `nudgePane` in
           * `focus.ts` for the pattern and for why the step is 16px.
           *
           * UP GROWS IT, because the dock is anchored to the bottom edge and
           * expands upward -- the handle moving up is the panel getting
           * taller, which is exactly what the pointer drag does.
           *
           * `aria-valuemax` IS `ceiling()`, NOT `DOCK.max`. The real ceiling is
           * 70% of the viewport, computed at the call site for the reason
           * `panes.ts` records; announcing the 640px fallback instead would
           * report a limit the drag does not honour.
           */
          tabIndex={0}
          aria-valuenow={height}
          aria-valuemin={DOCK.min}
          aria-valuemax={ceiling()}
          onKeyDown={(e) => {
            const next = nudgePane(height, e.key, { min: DOCK.min, max: ceiling() }, 'ArrowUp')
            if (next === null) return
            e.preventDefault()
            setHeight(next)
            writePane(DOCK, next)
          }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        />
      )}

      {/* OUTSIDE THE DISCLOSURE, deliberately. An expired session is the one
          thing in here that carries an ACTION, and a control that appears only
          after a click is a control that is not there. The collapsed line's
          dot turns `is-bad` for the same state, but a dot is not a button. */}
      {s.expired && (
        <button className="reauth" onClick={() => window.location.reload()}>
          Session expired — reload to sign in
        </button>
      )}

      <button
        type="button"
        className="ctl-dock-line"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`ctl-dock-dot ${tone}`} aria-hidden />
        <span className="ctl-dock-label">Reads</span>
        {/* EVERY FACT IS ONE UNWRAPPABLE UNIT, and the strip breaks BETWEEN
            them — F10 of `docs/audits/2026-09-23/overflow-inventory.md`.

            This was five facts and four ` · ` separators in one nowrap span
            with an ellipsis on it, which at 390pt rendered
            `… · 0 failed · 2 …`: 40% gone, and the 40% was the admin-only
            count and the read age. Those are the two that distinguish a
            console that is fine from one that is stale or half-blind; what
            survived was `0 failed`, the reassuring half. So the ellipsis is
            gone (see `.ctl-dock-facts`) and the line wraps instead.

            The wrapping is why each fact needs a box of its own. Left as bare
            text the browser would break inside a fact — `2` on one line and
            `admin-only` on the next, or worse, `newest 3m` over `ago` —
            which is the same defect in a new shape. `.ctl-dock-fact` is
            `white-space: nowrap`, so the only break opportunities left are
            the spaces in the separators between them.

            The separators stay as text rather than becoming a CSS `::before`:
            the prose budget tests count the text nodes a reader can see, and
            moving punctuation into generated content would change what those
            read without changing what the screen says. */}
        <span className="ctl-dock-facts">
          <span className="ctl-dock-fact">
            {s.routes} route{s.routes === 1 ? '' : 's'}
          </span>
          {' · '}
          {/* An em dash, never a 0: no sample is not a fast response. */}
          <span className="ctl-dock-fact">
            {s.p95Ms === null ? <span className="ctl-em">p95 &mdash;</span> : `p95 ${s.p95Ms}ms`}
          </span>
          {' · '}
          <span className={`ctl-dock-fact${s.failed > 0 ? ' ctl-dock-bad' : ''}`}>
            {s.failed} failed
          </span>
          {s.adminOnly > 0 && (
            <>
              {' · '}
              <span className="ctl-dock-fact">{s.adminOnly} admin-only</span>
            </>
          )}
          {' · '}
          <span className="ctl-dock-fact">
            {s.newestSuccessAt === null ? (
              <span className="ctl-em">nothing has loaded</span>
            ) : (
              `newest ${timeAgo(s.newestSuccessAt, now)}`
            )}
          </span>
        </span>
        <span className="ctl-dock-caret" aria-hidden>
          {open ? '▾' : '▴'}
        </span>
      </button>

      {open && (
        <div className="ctl-dock-body">
          {/* THE LEAD PARAGRAPH IS GONE AND ITS TWO FACTS ARE STILL HERE.
              It ran to 36 words to say two things: one cell per route, and the
              age is of the newest SUCCESSFUL payload. The first is what the
              grid of cells already looks like; the second is fused to the
              figure it qualifies, in `DataSources.tsx`'s own cell labels and
              in the foot below. What is left is the two destinations, which
              were the only part of that paragraph anyone clicked. */}
          <div className="ctl-toolbar ctl-dock-tools">
            <span className="ctl-eyebrow">Routes called in this tab</span>
            <a className="is-end" href={`#${helpAnchor(HELP_TOPIC)}`}>
              What these mean &rarr;
            </a>
            <a href="#reference">Every read, in a table &rarr;</a>
          </div>
          {/* The cells read the wall clock as they render, and they render on
              this component's shared tick, so their ages move with the line
              above them. */}
          <DataSourceCells probes={probes} />
          {/* THE PROVENANCE STRIP: when, over what, from how many. One line,
              and it stays because a p95 whose basis is unstated is a p95 that
              means something else -- the same class of defect as a count
              computed over a partial read. The paragraph that used to say so
              twice is in `#help/api-reads`. */}
          <p className="ctl-dock-foot">
            newest successful payload · p95 over the{' '}
            <strong>last attempt of each route</strong>, {s.routes} sample
            {s.routes === 1 ? '' : 's'}
          </p>
        </div>
      )}
    </div>
  )
}
