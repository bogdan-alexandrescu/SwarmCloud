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
import { helpAnchor, type TopicId } from './help'
import { timeAgo } from './Shell'
import { DOCK, DOCK_COLLAPSED, clampPane, readPane, summariseProbes, writePane } from './panes'

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
  const [, tick] = useState(0)
  const [open, setOpen] = useState(false)
  const [height, setHeight] = useState(() => readPane(DOCK))
  const dragging = useRef(false)

  // The ages on this strip are the whole point of it, so they move on their
  // own rather than only when a fetch happens to land.
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [])

  // The rendered height, published as a custom property so the page's bottom
  // padding can reserve exactly the collapsed bar and nothing more. Set on the
  // documentElement rather than passed down: the dock is fixed to the viewport
  // and `.app` is not its ancestor.
  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--dock-h', `${open ? height : DOCK_COLLAPSED}px`)
    return () => {
      root.style.removeProperty('--dock-h')
    }
  }, [open, height])

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

  if (probes.length === 0) {
    // NOTHING HAS BEEN READ YET, so there is nothing to be provenance ABOUT.
    // An empty dock would be a bar claiming a summary of no reads; the strip
    // has always rendered nothing in this case and that stays true.
    return null
  }

  const s = summariseProbes(probes)
  const tone = s.expired ? 'is-bad' : s.failed > 0 ? 'is-warn' : 'is-ok'

  return (
    <div className={`ctl-dock${open ? ' is-open' : ''}`} style={open ? { height } : undefined}>
      {open && (
        <div
          className="ctl-dock-grip"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize the provenance panel"
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
        <span className="ctl-dock-facts">
          {s.routes} route{s.routes === 1 ? '' : 's'}
          {' · '}
          {/* An em dash, never a 0: no sample is not a fast response. */}
          {s.p95Ms === null ? <span className="ctl-em">p95 &mdash;</span> : `p95 ${s.p95Ms}ms`}
          {' · '}
          <span className={s.failed > 0 ? 'ctl-dock-bad' : undefined}>{s.failed} failed</span>
          {s.adminOnly > 0 && ` · ${s.adminOnly} admin-only`}
          {' · '}
          {s.newestSuccessAt === null ? (
            <span className="ctl-em">nothing has loaded</span>
          ) : (
            `newest ${timeAgo(s.newestSuccessAt)}`
          )}
        </span>
        <span className="ctl-dock-caret" aria-hidden>
          {open ? '▾' : '▴'}
        </span>
      </button>

      {open && (
        <div className="ctl-dock-body">
          <p className="ctl-dock-lead">
            One cell per route this browser tab has called. The age is of the
            newest <strong>successful</strong> payload, which is the part that
            says whether a panel showing an old figure is showing a stale one.
            {' '}
            <a href={`#${helpAnchor(HELP_TOPIC)}`}>What these mean &rarr;</a>
            {' · '}
            <a href="#reference">Every read, in a table &rarr;</a>
          </p>
          <DataSourceCells probes={probes} />
          <p className="ctl-dock-foot">
            p95 is taken over the <strong>last attempt of each route</strong> —
            one sample per route, {s.routes} in total. This tab keeps no request
            history, so it is not a percentile over every request made.
          </p>
        </div>
      )}
    </div>
  )
}
