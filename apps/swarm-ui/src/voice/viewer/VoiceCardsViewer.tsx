// MOCK-HOOK FILE
/**
 * MOCK-HOOK: Voice it's cards as a compact list and ONE viewer
 * (?voicecards=viewer, direction "Cards and a viewer").
 *
 * The list keeps every card small: its kind's icon, its title, a live
 * preview and its primary action. Opening a card shows it in the viewer,
 * beside the list on a desktop and over it on a phone, with the tools its
 * content needs and its actions pinned at the foot. A second card can be
 * opened beside the first. Every content type plugs in through VIEWERS; a
 * type the console does not know is drawn by the generic viewer from its
 * fields.
 *
 * The opening state is read from the address so every frame is a URL:
 * card, beside, page, device, picked, pickframe, pins, zoom, node, filter.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { Button, CodeBlock, DiffBlock, Segmented, Tooltip, parseDiff } from '../../components'
import { PageHead } from '../../Shell'
import { InfoTip } from '../../People'
import '../../styles/voice-viewer.css'
import { MOCK_CARDS, MOCKUP_PAGES, type DEdge, type DNode, type Diagram, type DocBlock, type MockCard, type TestCase } from './fixtures'

// ---- the address -------------------------------------------------------------

const Q = typeof window === 'undefined' ? new URLSearchParams() : new URLSearchParams(window.location.search)

function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches)
  useEffect(() => {
    const m = window.matchMedia(query)
    const f = () => setOn(m.matches)
    m.addEventListener('change', f)
    return () => m.removeEventListener('change', f)
  }, [query])
  return on
}

function clock(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// ---- icons -------------------------------------------------------------------

const ICON_PATHS: Record<string, ReactNode> = {
  proposal: <path d="M4 6h10M4 12h16M4 18h7M18 4l2 2-2 2" />,
  mockup: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 8h18M7 12h6M7 15h10" />
    </>
  ),
  diagram: (
    <>
      <rect x="3" y="3" width="7" height="6" rx="1" />
      <rect x="14" y="15" width="7" height="6" rx="1" />
      <rect x="14" y="3" width="7" height="6" rx="1" />
      <path d="M10 6h4M17.5 9v6" />
    </>
  ),
  wireframe: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="2" strokeDasharray="3 2" />
      <path d="M7 8h10M7 12h4M7 16h6" />
    </>
  ),
  code: <path d="M9 7l-5 5 5 5M15 7l5 5-5 5" />,
  diff: <path d="M7 4v10M2 9h10M14 18h8M17 4l4 4-4 4" />,
  document: (
    <>
      <path d="M6 3h8l4 4v14H6z" />
      <path d="M14 3v4h4M9 12h6M9 16h6" />
    </>
  ),
  test_results: <path d="M4 12l4 4 8-9M14 18h6M17 15v6" />,
  generic: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M9 9.5a3 3 0 115 2.2c-1 .8-2 1.3-2 2.8M12 17h.01" />
    </>
  ),
  close: <path d="M6 6l12 12M18 6L6 18" />,
  prev: <path d="M15 5l-7 7 7 7" />,
  next: <path d="M9 5l7 7-7 7" />,
  beside: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M12 4v16" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  minus: <path d="M5 12h14" />,
  fit: <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />,
  full: <path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7" />,
  pin: <path d="M12 21s-6-6.2-6-11a6 6 0 1112 0c0 4.8-6 11-6 11z" />,
  check: <path d="M5 12l5 5 9-10" />,
  star: <path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" />,
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0014 0M12 18v3" />
    </>
  ),
  comment: <path d="M4 5h16v11H9l-5 4z" />,
  copy: (
    <>
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M16 8V4H4v12h4" />
    </>
  ),
}

function Ico({ name, size = 16 }: { name: string; size?: number }) {
  return (
    <svg className="vv-ico" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {ICON_PATHS[name] ?? ICON_PATHS.generic}
    </svg>
  )
}

// ---- what a viewer is handed --------------------------------------------------------

/** What the person has done to a card, held by the page (the server side is not built). */
interface Acts {
  picked?: string
  pickedAt?: string
  pickedFrame?: string
}

interface ViewerProps {
  card: MockCard
  acts: Acts
  act: (a: Acts) => void
  /** The shell the viewer draws itself into: header, tools, body, pinned actions. */
  Shell: (p: ShellProps) => ReactNode
  narrow: boolean
  phone: boolean
}

interface ShellProps {
  tools?: ReactNode
  children: ReactNode
  status?: ReactNode
  actions: ReactNode
  bodyClass?: string
}

interface Plugin {
  icon: string
  word: string
  Preview: (p: { card: MockCard; acts: Acts }) => ReactNode
  Primary: (p: { card: MockCard; acts: Acts; open: () => void; act: (a: Acts) => void }) => ReactNode
  Viewer: (p: ViewerProps) => ReactNode
}

// ---- small shared pieces ------------------------------------------------------------

function ScaledFrame({
  html,
  designW,
  designH,
  cropH,
  title,
  children,
  onClick,
  thumb = false,
  zoom = null,
  onScale,
}: {
  html: string
  designW: number
  designH: number
  cropH?: number
  title: string
  children?: ReactNode
  onClick?: (x: number, y: number) => void
  thumb?: boolean
  // A number draws the page at that zoom (it may be wider than its box: the box scrolls); null fits it to the width.
  zoom?: number | null
  onScale?: (s: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (el === null) return
    setW(el.clientWidth)
    const ro = new ResizeObserver(() => setW(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const s = w > 0 ? zoom ?? Math.min(1, w / designW) : 0
  const h = Math.min(designH, cropH ?? designH)
  useEffect(() => {
    if (s > 0) onScale?.(s)
  }, [s, onScale])
  return (
    <div ref={ref} className={`vv-sf${thumb ? ' is-thumb' : ''}`}>
      <div className="vv-sf-box" style={{ width: designW * s, height: h * s }}>
        {s > 0 ? (
          <iframe
            sandbox=""
            srcDoc={html}
            title={title}
            tabIndex={thumb ? -1 : 0}
            style={{ width: designW, height: designH, transform: `scale(${s})` }}
          />
        ) : null}
        {onClick !== undefined ? (
          <div
            className="vv-sf-catch"
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect()
              onClick(((e.clientX - r.left) / r.width) * 100, ((e.clientY - r.top) / r.height) * 100)
            }}
          />
        ) : null}
        {children}
      </div>
    </div>
  )
}

// A page under review opens at 100%, where its own text is readable; Fit shrinks it to the pane's width.
function usePageZoom(initial: number | null) {
  const [zoom, setZoom] = useState<number | null>(initial)
  // 0 until the frame has measured its box, so what waits on the page's size runs again once it is drawn.
  const [shown, setShown] = useState(0)
  const step = (k: number) => setZoom(Math.max(0.25, Math.min(2, Math.round((shown * k) / 0.05) * 0.05)))
  const tools = (
    <span className="vv-zoom" role="group" aria-label="Zoom">
      <Button kind="ghost" size="sm" iconOnly icon={<Ico name="minus" />} onClick={() => step(1 / 1.25)}>
        Zoom out
      </Button>
      <output className="vv-zoom-v">{Math.round((shown || (initial ?? 1)) * 100)}%</output>
      <Button kind="ghost" size="sm" iconOnly icon={<Ico name="plus" />} onClick={() => step(1.25)}>
        Zoom in
      </Button>
      <Button kind="ghost" size="sm" icon={<Ico name="fit" />} onClick={() => setZoom(null)} disabled={zoom === null}>
        Fit
      </Button>
    </span>
  )
  return { zoom, shown, onScale: setShown, tools }
}

function StateWord({ state }: { state: MockCard['state'] }) {
  const word = { proposed: 'new', waiting: 'needs you', done: 'done', failed: 'failed' }[state]
  return <span className={`vv-state is-${state}`}>{word}</span>
}

// ---- diagrams ---------------------------------------------------------------------------

function center(n: DNode): [number, number] {
  return [n.x + n.w / 2, n.y + n.h / 2]
}

/** Where a line from the node's centre towards (tx, ty) leaves the node's outline. */
function exitPoint(n: DNode, tx: number, ty: number): [number, number] {
  const [cx, cy] = center(n)
  const dx = tx - cx
  const dy = ty - cy
  if (dx === 0 && dy === 0) return [cx, cy]
  const hw = n.w / 2
  const hh = n.h / 2
  let t: number
  if (n.shape === 'diamond') t = 1 / (Math.abs(dx) / hw + Math.abs(dy) / hh)
  else if (n.shape === 'dot') t = hw / Math.hypot(dx, dy)
  else t = Math.min(hw / Math.abs(dx || 1e-9), hh / Math.abs(dy || 1e-9))
  return [cx + dx * t, cy + dy * t]
}

function edgePoints(e: DEdge, by: Map<string, DNode>): [number, number][] {
  const a = by.get(e.from)
  const b = by.get(e.to)
  if (a === undefined || b === undefined) return []
  const via = e.via ?? []
  const first = via[0] ?? center(b)
  const last = via[via.length - 1] ?? center(a)
  return [exitPoint(a, first[0], first[1]), ...via.map((v) => [v[0], v[1]] as [number, number]), exitPoint(b, last[0], last[1])]
}

function DiagramSvg({ d, outline = false, selected, onSelect }: { d: Diagram; outline?: boolean; selected?: string | null; onSelect?: (id: string) => void }) {
  const by = useMemo(() => new Map(d.nodes.map((n) => [n.id, n])), [d])
  const mid = `vv-ar-${d.diagram_type}${outline ? '-o' : ''}`
  return (
    <svg className={`vv-dg${outline ? ' is-outline' : ''}`} viewBox={`0 0 ${d.width} ${d.height}`} role={outline ? undefined : 'img'} aria-hidden={outline || undefined}>
      <defs>
        <marker id={mid} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" className="vv-dg-head" />
        </marker>
      </defs>
      {(d.groups ?? []).map((g) => (
        <g key={g.label} className="vv-dg-group">
          <rect x={g.x} y={g.y} width={g.w} height={g.h} rx="14" />
          {outline ? null : (
            <text x={g.x + 14} y={g.y + 22}>
              {g.label}
            </text>
          )}
        </g>
      ))}
      {d.edges.map((e, i) => {
        const pts = edgePoints(e, by)
        if (pts.length < 2) return null
        const m = Math.floor((pts.length - 1) / 2)
        const lx = (pts[m]![0] + pts[m + 1]![0]) / 2
        const ly = (pts[m]![1] + pts[m + 1]![1]) / 2
        return (
          <g key={i} className={`vv-dg-edge${e.dashed ? ' is-dashed' : ''}`}>
            <polyline points={pts.map((p) => p.join(',')).join(' ')} markerEnd={`url(#${mid})`} />
            {!outline && e.label !== undefined ? (
              <g className="vv-dg-elabel">
                <rect x={lx - e.label.length * 3.7 - 6} y={ly - 10} width={e.label.length * 7.4 + 12} height="20" rx="10" />
                <text x={lx} y={ly + 4} textAnchor="middle">
                  {e.label}
                </text>
              </g>
            ) : null}
          </g>
        )
      })}
      {d.nodes.map((n) => {
        const [cx, cy] = center(n)
        const cls = `vv-dg-node${n.tone ? ` is-${n.tone}` : ''}${selected === n.id ? ' is-sel' : ''}${onSelect ? ' is-live' : ''}`
        const shape =
          n.shape === 'diamond' ? (
            <polygon points={`${cx},${n.y} ${n.x + n.w},${cy} ${cx},${n.y + n.h} ${n.x},${cy}`} />
          ) : n.shape === 'dot' ? (
            <circle cx={cx} cy={cy} r={n.w / 2} className="is-dot" />
          ) : n.shape === 'store' ? (
            <path d={`M${n.x} ${n.y + 10} a${n.w / 2} 10 0 0 1 ${n.w} 0 v${n.h - 20} a${n.w / 2} 10 0 0 1 ${-n.w} 0z M${n.x} ${n.y + 10} a${n.w / 2} 10 0 0 0 ${n.w} 0`} />
          ) : (
            <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={n.shape === 'pill' ? n.h / 2 : n.shape === 'actor' ? 26 : 10} />
          )
        return (
          <g
            key={n.id}
            className={cls}
            onClick={onSelect ? () => onSelect(n.id) : undefined}
            role={onSelect ? 'button' : undefined}
            aria-label={onSelect ? n.label : undefined}
            aria-pressed={onSelect ? selected === n.id : undefined}
            tabIndex={onSelect ? 0 : undefined}
          >
            {shape}
            {outline || n.label === '' ? null : (
              <>
                <text x={cx} y={n.sub ? cy - 3 : cy + 5} textAnchor="middle" className="vv-dg-t">
                  {n.label}
                </text>
                {n.sub ? (
                  <text x={cx} y={cy + 15} textAnchor="middle" className="vv-dg-s">
                    {n.sub}
                  </text>
                ) : null}
              </>
            )}
          </g>
        )
      })}
    </svg>
  )
}

const DIAGRAM_WORD: Record<Diagram['diagram_type'], string> = { architecture: 'System diagram', user_flow: 'User flow', state: 'State diagram' }

function diagramOf(card: MockCard): Diagram {
  return card.body.diagram as Diagram
}

function DiagramViewer({ card, Shell, narrow }: ViewerProps) {
  const d = diagramOf(card)
  const init = Q.get('card') === card.card_id ? Number(Q.get('zoom') ?? '') : NaN
  const [zoom, setZoom] = useState<number | null>(Number.isFinite(init) && init > 0 ? init : null)
  const [node, setNode] = useState<string | null>(Q.get('card') === card.card_id ? Q.get('node') : null)
  const scroller = useRef<HTMLDivElement>(null)
  const [box, setBox] = useState({ w: 0, h: 0 })
  const [view, setView] = useState({ x: 0, y: 0, w: 1, h: 1 })
  useLayoutEffect(() => {
    const el = scroller.current
    if (el === null) return
    const measure = () => setBox({ w: el.clientWidth - 48, h: el.clientHeight - 48 })
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const fit = box.w > 0 ? Math.min(1.4, box.w / d.width, box.h > 0 ? box.h / d.height : Infinity) : 1
  const scale = zoom ?? fit
  const w = d.width * scale
  const h = d.height * scale
  const track = () => {
    const el = scroller.current
    if (el === null) return
    setView({ x: el.scrollLeft / (w + 48), y: el.scrollTop / (h + 48), w: Math.min(1, el.clientWidth / (w + 48)), h: Math.min(1, el.clientHeight / (h + 48)) })
  }
  useEffect(() => {
    const el = scroller.current
    if (el !== null && zoom !== null) {
      // Zooming keeps the middle of the drawing in the middle of the view.
      el.scrollLeft = (w + 48 - el.clientWidth) / 2
      el.scrollTop = (h + 48 - el.clientHeight) / 2
    }
    track()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scale])
  const step = (k: number) => setZoom(Math.max(0.4, Math.min(3, Math.round((scale * k) / 0.1) * 0.1)))
  const picked = node === null ? null : d.nodes.find((n) => n.id === node) ?? null
  return (
    <Shell
      tools={
        <>
          <span className="vv-tag">{DIAGRAM_WORD[d.diagram_type]}</span>
          <span className="vv-zoom" role="group" aria-label="Zoom">
            <Button kind="ghost" size="sm" iconOnly icon={<Ico name="minus" />} onClick={() => step(1 / 1.25)}>
              Zoom out
            </Button>
            <output className="vv-zoom-v">{Math.round(scale * 100)}%</output>
            <Button kind="ghost" size="sm" iconOnly icon={<Ico name="plus" />} onClick={() => step(1.25)}>
              Zoom in
            </Button>
            <Button kind="ghost" size="sm" icon={<Ico name="fit" />} onClick={() => setZoom(null)} disabled={zoom === null}>
              Fit
            </Button>
          </span>
          {picked !== null ? (
            <span className="vv-sel-chip">
              {picked.label}
              <button type="button" aria-label="Clear selection" onClick={() => setNode(null)}>
                <Ico name="close" size={12} />
              </button>
            </span>
          ) : null}
        </>
      }
      bodyClass="is-canvas"
      status={picked === null ? null : <>Selected: {picked.label}</>}
      actions={
        <>
          <Button kind="primary" icon={<Ico name="mic" />}>
            Edit by voice
          </Button>
          <Button kind="secondary" disabled={picked === null} title={picked === null ? 'Select a box first' : undefined}>
            {picked === null ? 'File issue from box' : `File issue: ${picked.label}`}
          </Button>
          {narrow ? null : (
            <Button kind="ghost" icon={<Ico name="full" />}>
              Full size
            </Button>
          )}
        </>
      }
    >
      <div className="vv-canvas" ref={scroller} onScroll={track}>
        <div className="vv-canvas-in" style={{ width: w + 48, height: h + 48 }}>
          <div style={{ width: w, height: h }}>
            <DiagramSvg d={d} selected={node} onSelect={(id) => setNode(id === node ? null : id)} />
          </div>
        </div>
      </div>
      {zoom !== null && (view.w < 1 || view.h < 1) ? (
        <div className="vv-minimap" aria-hidden="true" style={{ aspectRatio: `${d.width} / ${d.height}`, width: d.width >= d.height ? 168 : 110 }}>
          <DiagramSvg d={d} outline />
          <span style={{ left: `${view.x * 100}%`, top: `${view.y * 100}%`, width: `${view.w * 100}%`, height: `${view.h * 100}%` }} />
        </div>
      ) : null}
    </Shell>
  )
}

// ---- proposal -----------------------------------------------------------------------------

interface Option {
  key: string
  label: string
  swatch: string[]
  gets_right: string[]
  costs: string[]
  recommended: boolean
}

function optionsOf(card: MockCard): Option[] {
  return card.body.options as Option[]
}

function Swatch({ colors }: { colors: string[] }) {
  return (
    <span className="vv-swatch" aria-hidden="true">
      {colors.map((c) => (
        <i key={c} style={{ background: c }} />
      ))}
    </span>
  )
}

function ProposalViewer({ card, acts, act, Shell }: ViewerProps) {
  const opts = optionsOf(card)
  const rec = opts.find((o) => o.recommended) ?? opts[0]!
  const [sel, setSel] = useState<string>(rec.key)
  const [mix, setMix] = useState<string[] | null>(null)
  const done = acts.picked !== undefined
  const pickedOpt = opts.find((o) => o.key === acts.picked)
  const choose = (k: string) => {
    if (done) return
    if (mix !== null) setMix(mix.includes(k) ? mix.filter((m) => m !== k) : [...mix, k])
    else setSel(k)
  }
  const selOpt = opts.find((o) => o.key === sel) ?? rec
  return (
    <Shell
      status={
        done ? (
          <span className="vv-done">
            <Ico name="check" /> Picked {pickedOpt?.label} · {clock(acts.pickedAt ?? card.at)}
          </span>
        ) : mix !== null ? (
          <>{mix.length} of {opts.length} to mix</>
        ) : null
      }
      actions={
        done ? (
          <Button kind="ghost" onClick={() => act({})}>
            Undo pick
          </Button>
        ) : mix !== null ? (
          <>
            <Button kind="primary" disabled={mix.length < 2}>
              Mix {mix.length < 2 ? '' : mix.map((k) => opts.find((o) => o.key === k)?.label.split(' ')[0]).join(' + ')}
            </Button>
            <Button kind="ghost" onClick={() => setMix(null)}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            <Button kind="primary" icon={<Ico name="check" />} onClick={() => act({ picked: sel, pickedAt: '2026-10-19T12:14:00Z' })}>
              Pick {selOpt.label}
            </Button>
            <Button kind="secondary" onClick={() => setMix([sel])}>
              Mix
            </Button>
            <Button kind="ghost" icon={<Ico name="mic" />}>
              Ask again
            </Button>
          </>
        )
      }
    >
      <div className="vv-prop">
        <h3 className="vv-q">{String(card.body.question)}</h3>
        <div className="vv-opts" role={done ? undefined : mix !== null ? 'group' : 'radiogroup'} aria-label="Options">
          {opts.map((o, i) => {
            const on = done ? o.key === acts.picked : mix !== null ? mix.includes(o.key) : o.key === sel
            return (
              <div
                key={o.key}
                className={`vv-opt${on ? ' is-on' : ''}${done && !on ? ' is-off' : ''}`}
                role={done ? undefined : mix !== null ? 'checkbox' : 'radio'}
                aria-checked={done ? undefined : on}
                tabIndex={done ? undefined : 0}
                onClick={() => choose(o.key)}
                onKeyDown={(e) => {
                  if (e.key === ' ' || e.key === 'Enter') {
                    e.preventDefault()
                    choose(o.key)
                  }
                }}
              >
                <div className="vv-opt-top">
                  <Swatch colors={o.swatch} />
                  <span className="vv-opt-n">{String.fromCharCode(65 + i)}</span>
                  {done && on ? (
                    <span className="vv-badge is-picked">
                      <Ico name="check" size={12} /> Picked
                    </span>
                  ) : o.recommended ? (
                    <span className="vv-badge is-rec">
                      <Ico name="star" size={12} /> Recommended
                    </span>
                  ) : null}
                </div>
                <div className="vv-sample" aria-hidden="true" style={{ background: o.swatch[0], color: o.swatch[2] }}>
                  <b style={{ fontFamily: o.key === 'a' ? 'Georgia, serif' : o.key === 'c' ? 'Impact, var(--font)' : 'var(--font)' }}>Slow bread, baked before the town wakes.</b>
                  <span>Sourdough · rye · buns</span>
                  <i style={{ background: o.swatch[1] }}>Order for pickup</i>
                </div>
                <h4>{o.label}</h4>
                <ul className="vv-pros">
                  {o.gets_right.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
                <ul className="vv-cons">
                  {o.costs.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            )
          })}
        </div>
        <p className="vv-rec">
          <Ico name="star" size={14} />
          <span>{String(card.body.recommendation)}</span>
        </p>
      </div>
    </Shell>
  )
}

// ---- mock-up --------------------------------------------------------------------------------

interface Pin {
  page: string
  x: number
  y: number
  text: string
  at: string
}

function initialPins(card: MockCard): Pin[] {
  if (Q.get('card') !== card.card_id || Q.get('pins') !== '1') return []
  return [{ page: 'story', x: 74, y: 40, text: 'Put the founders’ photo first on a phone, before the long text.', at: '2026-10-19T12:16:00Z' }]
}

function MockupViewer({ card, acts, act, Shell, phone, narrow }: ViewerProps) {
  const mine = Q.get('card') === card.card_id
  const startPage = mine ? Math.max(0, MOCKUP_PAGES.findIndex((p) => p.key === Q.get('page'))) : 0
  const [at, setAt] = useState(startPage)
  const [device, setDevice] = useState<'desktop' | 'phone'>(phone || (mine && Q.get('device') === 'phone') ? 'phone' : 'desktop')
  const [commenting, setCommenting] = useState(false)
  const [pins, setPins] = useState<Pin[]>(() => initialPins(card))
  const [draft, setDraft] = useState<{ x: number; y: number; text: string } | null>(null)
  const pz = usePageZoom(phone ? null : 1)
  const stage = useRef<HTMLDivElement>(null)
  const pg = MOCKUP_PAGES[at]!
  const designW = device === 'desktop' ? 1280 : 390
  const designH = pg.height[device]
  const here = pins.filter((p) => p.page === pg.key)
  const picked = acts.pickedFrame
  const go = (i: number) => {
    setAt((i + MOCKUP_PAGES.length) % MOCKUP_PAGES.length)
    setDraft(null)
  }
  const firstPin = here[0]
  useLayoutEffect(() => {
    // A page opened at 100% is wider than the pane: bring its first pinned comment into view.
    const el = stage.current
    if (el === null || firstPin === undefined || pz.shown === 0) return
    el.scrollLeft = Math.max(0, (firstPin.x / 100) * designW * pz.shown - el.clientWidth * 0.75)
    el.scrollTop = Math.max(0, (firstPin.y / 100) * designH * pz.shown - el.clientHeight * 0.4)
  }, [pg.key, firstPin, designW, designH, pz.shown])
  return (
    <Shell
      tools={
        <>
          <span className="vv-pager" role="group" aria-label="Pages">
            <Button kind="ghost" size="sm" iconOnly icon={<Ico name="prev" />} onClick={() => go(at - 1)}>
              Previous page
            </Button>
            <span className="vv-pager-n">
              <b>{pg.name}</b> {at + 1} of {MOCKUP_PAGES.length}
            </span>
            <Button kind="ghost" size="sm" iconOnly icon={<Ico name="next" />} onClick={() => go(at + 1)}>
              Next page
            </Button>
          </span>
          {phone ? null : (
            <Segmented
              small
              label="Preview width"
              value={device}
              onChange={setDevice}
              options={[
                { key: 'desktop', label: 'Desktop' },
                { key: 'phone', label: 'Phone' },
              ]}
            />
          )}
          {phone ? null : pz.tools}
          {pins.length > 0 ? (
            <span className="vv-count">
              <Ico name="comment" size={14} /> {pins.length}
            </span>
          ) : null}
        </>
      }
      bodyClass="is-stage is-fill"
      status={
        picked !== undefined ? (
          <span className="vv-done">
            <Ico name="check" /> Picked {MOCKUP_PAGES.find((p) => p.key === picked)?.name}
          </span>
        ) : commenting ? (
          <>Click the page to pin a comment</>
        ) : null
      }
      actions={
        <>
          {picked === pg.key ? (
            <Button kind="secondary" icon={<Ico name="check" />} onClick={() => act({ ...acts, pickedFrame: undefined })}>
              Picked
            </Button>
          ) : (
            <Button kind="primary" onClick={() => act({ ...acts, pickedFrame: pg.key })}>
              Pick this page
            </Button>
          )}
          <Button kind={commenting ? 'primary' : 'secondary'} icon={<Ico name="pin" />} aria-pressed={commenting} onClick={() => setCommenting(!commenting)}>
            {commenting ? 'Done' : 'Comment'}
          </Button>
          {narrow ? null : (
            <Button kind="ghost" icon={<Ico name="full" />}>
              Full size
            </Button>
          )}
        </>
      }
    >
      <div className={`vv-mock${phone ? ' is-phone' : ''}`}>
        {phone ? null : (
          <ol className="vv-strip" aria-label="Pages">
            {MOCKUP_PAGES.map((p, i) => (
              <li key={p.key}>
                <button type="button" className={`vv-strip-b${i === at ? ' is-on' : ''}`} aria-current={i === at || undefined} onClick={() => go(i)}>
                  <ScaledFrame thumb html={p.html} designW={1280} designH={p.height.desktop} cropH={900} title={`${p.name} thumbnail`} />
                  <span>
                    {i + 1}. {p.name}
                    {picked === p.key ? <Ico name="check" size={13} /> : null}
                    {pins.some((x) => x.page === p.key) ? <Ico name="comment" size={12} /> : null}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        )}
        <div ref={stage} className={`vv-page${commenting ? ' is-commenting' : ''}${picked === pg.key ? ' is-picked' : ''}`}>
          <ScaledFrame
            key={`${pg.key}-${device}`}
            html={pg.html}
            designW={designW}
            designH={designH}
            zoom={pz.zoom}
            onScale={pz.onScale}
            title={`${pg.name} mock-up`}
            onClick={commenting ? (x, y) => setDraft({ x, y, text: '' }) : undefined}
          >
            {here.map((p, i) => (
              <div key={i} className="vv-pin" style={{ left: `${p.x}%`, top: `${p.y}%` }}>
                <span className="vv-pin-n">{pins.indexOf(p) + 1}</span>
                <div className={`vv-pin-note${p.x > 60 ? ' is-left' : ''}`}>
                  <b>You</b> <time>{clock(p.at)}</time>
                  <p>{p.text}</p>
                </div>
              </div>
            ))}
            {draft !== null ? (
              <div className="vv-pin is-draft" style={{ left: `${draft.x}%`, top: `${draft.y}%` }}>
                <span className="vv-pin-n">{pins.length + 1}</span>
                <form
                  className={`vv-pin-note${draft.x > 60 ? ' is-left' : ''}`}
                  onSubmit={(e) => {
                    e.preventDefault()
                    if (draft.text.trim() === '') return
                    setPins([...pins, { page: pg.key, x: draft.x, y: draft.y, text: draft.text, at: '2026-10-19T12:16:00Z' }])
                    setDraft(null)
                  }}
                >
                  <textarea aria-label="Comment" autoFocus rows={3} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
                  <span className="vv-row">
                    <Button kind="primary" size="sm" type="submit">
                      Pin
                    </Button>
                    <Button kind="ghost" size="sm" onClick={() => setDraft(null)}>
                      Cancel
                    </Button>
                  </span>
                </form>
              </div>
            ) : null}
          </ScaledFrame>
        </div>
      </div>
    </Shell>
  )
}

// ---- wireframe ------------------------------------------------------------------------------

interface WireNote {
  n: number
  x: number
  y: number
  text: string
}

function WireframeViewer({ card, Shell, phone }: ViewerProps) {
  const [device, setDevice] = useState<'desktop' | 'phone'>(phone ? 'phone' : 'desktop')
  const [notes, setNotes] = useState(true)
  const hs = card.body.height as { desktop: number; phone: number }
  const list = card.body.notes as WireNote[]
  const pz = usePageZoom(phone ? null : 1)
  return (
    <Shell
      tools={
        <>
          {phone ? null : (
            <Segmented
              small
              label="Preview width"
              value={device}
              onChange={setDevice}
              options={[
                { key: 'desktop', label: 'Desktop' },
                { key: 'phone', label: 'Phone' },
              ]}
            />
          )}
          {phone ? null : pz.tools}
          <Button kind={notes ? 'secondary' : 'ghost'} size="sm" aria-pressed={notes} onClick={() => setNotes(!notes)}>
            Notes {list.length}
          </Button>
        </>
      }
      bodyClass="is-stage is-fill"
      actions={
        <>
          <Button kind="primary" icon={<Ico name="check" />}>
            Approve
          </Button>
          <Button kind="secondary" icon={<Ico name="pin" />}>
            Comment
          </Button>
          <Button kind="ghost" icon={<Ico name="mic" />}>
            Ask again
          </Button>
        </>
      }
    >
      <div className="vv-wire">
        <div className="vv-page">
          <ScaledFrame key={device} html={String(card.body.html)} designW={device === 'desktop' ? 1100 : 390} designH={hs[device]} zoom={pz.zoom} onScale={pz.onScale} title="Order-ahead wireframe">
            {notes && device === 'desktop'
              ? list.map((n) => (
                  <span key={n.n} className="vv-pin is-note" style={{ left: `${n.x}%`, top: `${n.y}%` }}>
                    <span className="vv-pin-n">{n.n}</span>
                  </span>
                ))
              : null}
          </ScaledFrame>
        </div>
        {notes ? (
          <ol className="vv-notes">
            {list.map((n) => (
              <li key={n.n}>
                <span className="vv-pin-n">{n.n}</span>
                {n.text}
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </Shell>
  )
}

// ---- code and diff --------------------------------------------------------------------------

function CodeViewer({ card, Shell }: ViewerProps) {
  const [wrap, setWrap] = useState(false)
  const [copied, setCopied] = useState(false)
  const code = String(card.body.code)
  return (
    <Shell
      tools={
        <>
          <span className="vv-tag">{String(card.body.language)}</span>
          <span className="vv-path">{String(card.body.path)}</span>
          <Button kind={wrap ? 'secondary' : 'ghost'} size="sm" aria-pressed={wrap} onClick={() => setWrap(!wrap)}>
            Wrap
          </Button>
        </>
      }
      actions={
        <>
          <Button
            kind="primary"
            icon={<Ico name={copied ? 'check' : 'copy'} />}
            onClick={() => {
              void navigator.clipboard?.writeText(code).catch(() => undefined)
              setCopied(true)
            }}
          >
            {copied ? 'Copied' : 'Copy'}
          </Button>
          <Button kind="secondary" icon={<Ico name="pin" />}>
            Comment
          </Button>
        </>
      }
    >
      <div className={`vv-code${wrap ? ' is-wrap' : ''}`}>
        <CodeBlock title={String(card.body.path).split('/').pop() ?? ''} lang={String(card.body.language)} text={code} firstLine={Number(card.body.first_line)} />
      </div>
    </Shell>
  )
}

interface DiffFile {
  path: string
  patch: string
}

function tally(patch: string): { add: number; del: number } {
  const l = parseDiff(patch)
  return { add: l.filter((x) => x.kind === 'add').length, del: l.filter((x) => x.kind === 'del').length }
}

function DiffViewer({ card, Shell }: ViewerProps) {
  const files = card.body.files as DiffFile[]
  const refs = useRef<Record<string, HTMLDivElement | null>>({})
  const [on, setOn] = useState(files[0]!.path)
  const all = files.reduce((s, f) => {
    const t = tally(f.patch)
    return { add: s.add + t.add, del: s.del + t.del }
  }, { add: 0, del: 0 })
  return (
    <Shell
      tools={
        <span className="vv-files" role="group" aria-label="Files">
          {files.map((f) => {
            const t = tally(f.patch)
            return (
              <button
                key={f.path}
                type="button"
                className={`vv-file${on === f.path ? ' is-on' : ''}`}
                aria-pressed={on === f.path}
                onClick={() => {
                  setOn(f.path)
                  refs.current[f.path]?.scrollIntoView({ block: 'start', behavior: 'smooth' })
                }}
              >
                <span className="vv-file-p">{f.path}</span>
                <span className="c-ds">
                  <span className="is-add">+{t.add}</span> <span className="is-del">−{t.del}</span>
                </span>
              </button>
            )
          })}
        </span>
      }
      status={
        <span className="c-ds">
          {files.length} files <span className="is-add">+{all.add}</span> <span className="is-del">−{all.del}</span>
        </span>
      }
      actions={
        <>
          <Button kind="primary" icon={<Ico name="check" />}>
            Approve
          </Button>
          <Button kind="secondary">Ask for changes</Button>
          <Button kind="ghost" icon={<Ico name="pin" />}>
            Comment
          </Button>
        </>
      }
    >
      <div className="vv-diff">
        {files.map((f) => (
          <div key={f.path} ref={(el) => void (refs.current[f.path] = el)}>
            <DiffBlock file={f.path} patch={f.patch} />
          </div>
        ))}
      </div>
    </Shell>
  )
}

// ---- document --------------------------------------------------------------------------------

function DocumentViewer({ card, Shell }: ViewerProps) {
  const blocks = card.body.blocks as DocBlock[]
  const heads = blocks.filter((b) => b.h !== undefined).map((b) => b.h!)
  const refs = useRef<Record<string, HTMLHeadingElement | null>>({})
  return (
    <Shell
      tools={
        <span className="vv-files" role="group" aria-label="Sections">
          {heads.map((h) => (
            <button key={h} type="button" className="vv-file" onClick={() => refs.current[h]?.scrollIntoView({ block: 'start', behavior: 'smooth' })}>
              {h}
            </button>
          ))}
        </span>
      }
      status={<>{String(card.body.words)} words</>}
      actions={
        <>
          <Button kind="primary" icon={<Ico name="check" />}>
            Approve
          </Button>
          <Button kind="secondary" icon={<Ico name="pin" />}>
            Comment
          </Button>
          <Button kind="ghost" icon={<Ico name="mic" />}>
            Ask again
          </Button>
        </>
      }
    >
      <article className="vv-doc">
        {blocks.map((b, i) =>
          b.h !== undefined ? (
            <h2 key={i} ref={(el) => void (refs.current[b.h!] = el)}>
              {b.h}
            </h2>
          ) : b.p !== undefined ? (
            <p key={i}>{b.p}</p>
          ) : (
            <ul key={i}>
              {(b.li ?? []).map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          ),
        )}
      </article>
    </Shell>
  )
}

// ---- test results ------------------------------------------------------------------------------

type TestFilter = 'all' | 'failed' | 'passed' | 'skipped'
const ORDER: Record<TestCase['status'], number> = { failed: 0, skipped: 1, passed: 2 }

function counts(tests: TestCase[]) {
  return {
    failed: tests.filter((t) => t.status === 'failed').length,
    passed: tests.filter((t) => t.status === 'passed').length,
    skipped: tests.filter((t) => t.status === 'skipped').length,
  }
}

function TallyBar({ tests }: { tests: TestCase[] }) {
  const c = counts(tests)
  const n = tests.length
  return (
    <span className="vv-tally" aria-hidden="true">
      <i className="is-failed" style={{ width: `${(c.failed / n) * 100}%` }} />
      <i className="is-skipped" style={{ width: `${(c.skipped / n) * 100}%` }} />
      <i className="is-passed" style={{ width: `${(c.passed / n) * 100}%` }} />
    </span>
  )
}

function TestsViewer({ card, Shell }: ViewerProps) {
  const tests = card.body.tests as TestCase[]
  const c = counts(tests)
  const [f, setF] = useState<TestFilter>((Q.get('filter') as TestFilter | null) ?? 'all')
  const shown = tests.filter((t) => f === 'all' || t.status === f).sort((a, b) => ORDER[a.status] - ORDER[b.status])
  return (
    <Shell
      tools={
        <Segmented
          small
          label="Show"
          value={f}
          onChange={setF}
          options={[
            { key: 'all', label: 'All', count: tests.length },
            { key: 'failed', label: 'Failed', count: c.failed },
            { key: 'passed', label: 'Passed', count: c.passed },
            ...(c.skipped > 0 ? [{ key: 'skipped' as const, label: 'Skipped', count: c.skipped }] : []),
          ]}
        />
      }
      status={<>{(Number(card.body.ran_in_ms) / 1000).toFixed(1)} s</>}
      actions={
        <>
          <Button kind="primary" icon={<Ico name="mic" />}>
            Ask to fix {c.failed}
          </Button>
          <Button kind="secondary">Rerun failed</Button>
          <Button kind="ghost">File issue</Button>
        </>
      }
    >
      <div className="vv-tests">
        <div className="vv-tests-sum">
          <span className="vv-big is-failed">{c.failed}</span> failed
          <span className="vv-big is-passed">{c.passed}</span> passed
          {c.skipped > 0 ? (
            <>
              <span className="vv-big">{c.skipped}</span> skipped
            </>
          ) : null}
          <TallyBar tests={tests} />
        </div>
        <ul className="vv-tlist">
          {shown.map((t) => (
            <li key={t.name} className={`vv-t is-${t.status}`}>
              <span className="vv-t-mark" aria-label={t.status}>
                {t.status === 'failed' ? '✕' : t.status === 'passed' ? '✓' : '–'}
              </span>
              <span className="vv-t-name">{t.name}</span>
              <span className="vv-t-suite">{t.suite}</span>
              <span className="vv-t-ms">{t.ms} ms</span>
              {t.status === 'failed' ? (
                <div className="vv-t-fail">
                  <p>{t.message}</p>
                  <dl>
                    <dt>Expected</dt>
                    <dd className="is-exp">{t.expected}</dd>
                    <dt>Got</dt>
                    <dd className="is-got">{t.received}</dd>
                  </dl>
                  <span className="vv-path">{t.at}</span>
                </div>
              ) : t.status === 'skipped' && t.message ? (
                <span className="vv-t-why">{t.message}</span>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
    </Shell>
  )
}

// ---- the generic viewer: a kind the console does not know --------------------------------

function humanize(k: string): string {
  const s = k.replace(/[_-]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function GenericValue({ v }: { v: unknown }): ReactNode {
  if (Array.isArray(v)) {
    if (v.length > 0 && v.every((x) => typeof x === 'object' && x !== null && !Array.isArray(x))) {
      const cols = [...new Set(v.flatMap((x) => Object.keys(x as object)))]
      return (
        <div className="vv-gtable">
          <table>
            <thead>
              <tr>
                {cols.map((c) => (
                  <th key={c}>{humanize(c)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {v.map((row, i) => (
                <tr key={i}>
                  {cols.map((c) => {
                    const cell = (row as Record<string, unknown>)[c]
                    return <td key={c}>{typeof cell === 'object' && cell !== null ? JSON.stringify(cell) : String(cell ?? '')}</td>
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }
    return (
      <ul>
        {v.map((x, i) => (
          <li key={i}>{typeof x === 'object' ? JSON.stringify(x) : String(x)}</li>
        ))}
      </ul>
    )
  }
  if (typeof v === 'object' && v !== null) return <GenericFields body={v as Record<string, unknown>} />
  return <>{String(v)}</>
}

function GenericFields({ body }: { body: Record<string, unknown> }) {
  const keys = Object.keys(body).filter((k) => k !== 'actions')
  const short = keys.filter((k) => ['string', 'number', 'boolean'].includes(typeof body[k]) && String(body[k]).length <= 60)
  const long = keys.filter((k) => !short.includes(k))
  return (
    <>
      {short.length > 0 ? (
        <dl className="vv-gfacts">
          {short.map((k) => (
            <div key={k}>
              <dt>{humanize(k)}</dt>
              <dd>{String(body[k])}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {long.map((k) => (
        <section key={k} className="vv-gsec">
          <h3>{humanize(k)}</h3>
          <GenericValue v={body[k]} />
        </section>
      ))}
    </>
  )
}

function GenericViewer({ card, Shell }: ViewerProps) {
  const [mode, setMode] = useState<'read' | 'raw'>('read')
  return (
    <Shell
      tools={
        <>
          <Segmented
            small
            label="Show as"
            value={mode}
            onChange={setMode}
            options={[
              { key: 'read', label: 'Readable' },
              { key: 'raw', label: 'Raw' },
            ]}
          />
          <span className="vv-tag is-new">
            {card.kind}
            <InfoTip about="a new kind of card" tip="No viewer for this kind yet: its fields are shown as they came." />
          </span>
        </>
      }
      actions={
        <>
          <Button kind="primary" icon={<Ico name="mic" />}>
            Ask about it
          </Button>
          <Button kind="secondary" icon={<Ico name="pin" />}>
            Comment
          </Button>
        </>
      }
    >
      {mode === 'read' ? (
        <div className="vv-generic">
          <GenericFields body={card.body} />
        </div>
      ) : (
        <div className="vv-code">
          <CodeBlock title={`${card.kind}.json`} lang="json" text={JSON.stringify(card.body, null, 2)} />
        </div>
      )}
    </Shell>
  )
}

// ---- the registry: one entry per content type ---------------------------------------------

const OpenBtn = (label: string) =>
  function Primary({ open }: { open: () => void }) {
    return (
      <Button kind="secondary" size="sm" onClick={open}>
        {label}
      </Button>
    )
  }

const VIEWERS: Readonly<Record<string, Plugin>> = {
  proposal: {
    icon: 'proposal',
    word: 'Proposal',
    Preview: ({ card, acts }) => {
      const opts = optionsOf(card)
      return (
        <ul className="vv-pv-opts">
          {opts.map((o) => (
            <li key={o.key} className={acts.picked === o.key ? 'is-picked' : acts.picked !== undefined ? 'is-off' : ''}>
              <Swatch colors={o.swatch} />
              <span>{o.label}</span>
              {acts.picked === o.key ? <Ico name="check" size={12} /> : acts.picked === undefined && o.recommended ? <Ico name="star" size={12} /> : null}
            </li>
          ))}
        </ul>
      )
    },
    Primary: ({ card, acts, act, open }) => {
      if (acts.picked !== undefined)
        return (
          <Button kind="ghost" size="sm" onClick={open}>
            Open
          </Button>
        )
      const rec = optionsOf(card).find((o) => o.recommended)
      return (
        <Button kind="primary" size="sm" onClick={() => (rec ? act({ picked: rec.key, pickedAt: '2026-10-19T12:14:00Z' }) : open())}>
          Pick {rec?.label ?? ''}
        </Button>
      )
    },
    Viewer: ProposalViewer,
  },
  mockup: {
    icon: 'mockup',
    word: 'Mock-up',
    Preview: ({ acts }) => (
      <div className="vv-pv-pages">
        {MOCKUP_PAGES.map((p) => (
          <span key={p.key} className={acts.pickedFrame === p.key ? 'is-picked' : ''}>
            <ScaledFrame thumb html={p.html} designW={1280} designH={p.height.desktop} cropH={860} title={`${p.name} thumbnail`} />
          </span>
        ))}
      </div>
    ),
    Primary: OpenBtn('Review 3 pages'),
    Viewer: MockupViewer,
  },
  diagram: {
    icon: 'diagram',
    word: 'Diagram',
    Preview: ({ card }) => (
      <div className="vv-pv-dg">
        <DiagramSvg d={diagramOf(card)} outline />
      </div>
    ),
    Primary: OpenBtn('Open diagram'),
    Viewer: DiagramViewer,
  },
  wireframe: {
    icon: 'wireframe',
    word: 'Wireframe',
    Preview: ({ card }) => (
      <div className="vv-pv-frame">
        <ScaledFrame thumb html={String(card.body.html)} designW={1100} designH={620} title="Wireframe thumbnail" />
      </div>
    ),
    Primary: OpenBtn('Review'),
    Viewer: WireframeViewer,
  },
  code: {
    icon: 'code',
    word: 'Code',
    Preview: ({ card }) => (
      <pre className="vv-pv-code">
        {String(card.body.code)
          .split('\n')
          .filter((l) => !l.startsWith('//'))
          .slice(0, 3)
          .join('\n')}
      </pre>
    ),
    Primary: OpenBtn('Read'),
    Viewer: CodeViewer,
  },
  diff: {
    icon: 'diff',
    word: 'Diff',
    Preview: ({ card }) => (
      <ul className="vv-pv-files">
        {(card.body.files as DiffFile[]).map((f) => {
          const t = tally(f.patch)
          return (
            <li key={f.path}>
              <span>{f.path}</span>
              <span className="c-ds">
                <span className="is-add">+{t.add}</span> <span className="is-del">−{t.del}</span>
              </span>
            </li>
          )
        })}
      </ul>
    ),
    Primary: OpenBtn('Review 2 files'),
    Viewer: DiffViewer,
  },
  document: {
    icon: 'document',
    word: 'Document',
    Preview: ({ card }) => {
      const blocks = card.body.blocks as DocBlock[]
      return (
        <div className="vv-pv-doc">
          <p>{blocks.find((b) => b.p !== undefined)?.p}</p>
          <span>
            {blocks.filter((b) => b.h !== undefined).length} sections · {String(card.body.words)} words
          </span>
        </div>
      )
    },
    Primary: OpenBtn('Read'),
    Viewer: DocumentViewer,
  },
  test_results: {
    icon: 'test_results',
    word: 'Tests',
    Preview: ({ card }) => {
      const tests = card.body.tests as TestCase[]
      const c = counts(tests)
      return (
        <div className="vv-pv-tests">
          <span>
            <b className="is-failed">{c.failed} failed</b> · {c.passed} passed{c.skipped > 0 ? ` · ${c.skipped} skipped` : ''}
          </span>
          <TallyBar tests={tests} />
        </div>
      )
    },
    Primary: OpenBtn('See failures'),
    Viewer: TestsViewer,
  },
}

const GENERIC: Plugin = {
  icon: 'generic',
  word: '',
  Preview: ({ card }) => {
    const b = card.body
    const firstList = Object.entries(b).find(([, v]) => Array.isArray(v))
    const firstShort = Object.entries(b).find(([, v]) => typeof v === 'string' && v.length <= 40)
    return (
      <div className="vv-pv-doc">
        <span>
          {firstList ? `${(firstList[1] as unknown[]).length} ${humanize(firstList[0]).toLowerCase()}` : `${Object.keys(b).length} fields`}
          {firstShort ? ` · ${humanize(firstShort[0])} ${String(firstShort[1])}` : ''}
        </span>
      </div>
    )
  },
  Primary: OpenBtn('Open'),
  Viewer: GenericViewer,
}

function pluginOf(card: MockCard): Plugin {
  return VIEWERS[card.content_type] ?? GENERIC
}

function wordOf(card: MockCard): string {
  const p = pluginOf(card)
  if (p === GENERIC) return humanize(card.kind)
  if (card.content_type === 'diagram') return DIAGRAM_WORD[diagramOf(card).diagram_type]
  return p.word
}

// ---- the list --------------------------------------------------------------------------------

function CardTile({
  card,
  acts,
  act,
  open,
  on,
  rail,
}: {
  card: MockCard
  acts: Acts
  act: (a: Acts) => void
  open: () => void
  on: boolean
  rail: boolean
}) {
  const p = pluginOf(card)
  const state = acts.picked !== undefined ? 'done' : card.state
  return (
    <li className={`vv-tile${on ? ' is-on' : ''}${p === GENERIC ? ' is-generic' : ''}`} data-kind={card.kind} title={rail ? card.title : undefined} onClick={open}>
      <div className="vv-tile-h">
        <span className={`vv-kico is-${p.icon}`}>
          <Ico name={p.icon} />
        </span>
        <div className="vv-tile-t">
          <button type="button" className="vv-tile-title" aria-current={on || undefined} onClick={(e) => (e.stopPropagation(), open())}>
            {card.title}
          </button>
          <span className="vv-tile-m">
            {wordOf(card)}
            <span className="vv-sep"> · </span>
            <time dateTime={card.at}>{clock(card.at)}</time>
            {state === 'done' || state === 'failed' || state === 'waiting' ? (
              <>
                {' · '}
                <StateWord state={state} />
              </>
            ) : null}
          </span>
        </div>
      </div>
      <div className="vv-tile-pv">
        <p.Preview card={card} acts={acts} />
      </div>
      {rail ? null : (
        <div className="vv-tile-a" onClick={(e) => e.stopPropagation()}>
          <p.Primary card={card} acts={acts} act={act} open={open} />
        </div>
      )}
    </li>
  )
}

// ---- the viewer pane -------------------------------------------------------------------------

function ViewerShell({ tools, children, status, actions, bodyClass }: ShellProps) {
  return (
    <>
      {tools ? <div className="vv-tools">{tools}</div> : null}
      <div className={`vv-body${bodyClass ? ` ${bodyClass}` : ''}`}>{children}</div>
      <footer className="vv-foot">
        {status ? <span className="vv-foot-s">{status}</span> : null}
        <span className="vv-foot-a">{actions}</span>
      </footer>
    </>
  )
}

function ViewerPane({
  card,
  acts,
  act,
  close,
  step,
  beside,
  narrow,
  phone,
}: {
  card: MockCard
  acts: Acts
  act: (a: Acts) => void
  close: () => void
  step?: (d: number) => void
  beside?: () => void
  narrow: boolean
  phone: boolean
}) {
  const p = pluginOf(card)
  const state = acts.picked !== undefined ? 'done' : card.state
  return (
    <section className="vv-pane" aria-label={card.title}>
      <header className="vv-head">
        {phone ? (
          <Button kind="ghost" size="sm" iconOnly icon={<Ico name="prev" />} onClick={close}>
            Back to cards
          </Button>
        ) : null}
        <span className={`vv-kico is-${p.icon}`}>
          <Ico name={p.icon} size={18} />
        </span>
        <div className="vv-head-t">
          <span className="vv-head-k">
            {wordOf(card)} · <time dateTime={card.at}>{clock(card.at)}</time> · <StateWord state={state} />
          </span>
          <h2>{card.title}</h2>
        </div>
        <span className="vv-head-a">
          {step && !phone ? (
            <>
              <Button kind="ghost" size="sm" iconOnly icon={<Ico name="prev" />} onClick={() => step(-1)}>
                Previous card
              </Button>
              <Button kind="ghost" size="sm" iconOnly icon={<Ico name="next" />} onClick={() => step(1)}>
                Next card
              </Button>
            </>
          ) : null}
          {beside ? (
            <Tooltip tip="Open the next card beside this one">
              <Button kind="ghost" size="sm" iconOnly icon={<Ico name="beside" />} onClick={beside}>
                Open beside
              </Button>
            </Tooltip>
          ) : null}
          {phone ? null : (
            <Button kind="ghost" size="sm" iconOnly icon={<Ico name="close" />} onClick={close}>
              Close
            </Button>
          )}
        </span>
      </header>
      <p.Viewer card={card} acts={acts} act={act} Shell={ViewerShell} narrow={narrow} phone={phone} />
    </section>
  )
}

// ---- the page --------------------------------------------------------------------------------

function initialActs(): Record<string, Acts> {
  const out: Record<string, Acts> = {}
  const picked = Q.get('picked')
  if (picked !== null) out['c-proposal'] = { picked, pickedAt: '2026-10-19T12:14:00Z' }
  const frame = Q.get('pickframe')
  if (frame !== null) out['c-mockup'] = { pickedFrame: frame }
  return out
}

export function VoiceCardsViewerScreen() {
  const phone = useMedia('(max-width: 640px)')
  const [open, setOpen] = useState<string | null>(Q.get('card'))
  const [beside, setBeside] = useState<string | null>(Q.get('beside'))
  const [acts, setActs] = useState<Record<string, Acts>>(initialActs)
  const [only, setOnly] = useState<'all' | 'you'>('all')
  const cards = MOCK_CARDS
  const byId = (id: string | null) => (id === null ? undefined : cards.find((c) => c.card_id === id))
  const a = byId(open)
  const b = phone ? undefined : byId(beside)
  const actOn = (id: string) => (x: Acts) => setActs({ ...acts, [id]: x })
  const needsYou = (c: MockCard) => (acts[c.card_id]?.picked === undefined && c.state === 'waiting') || c.state === 'failed'
  const shown = only === 'you' ? cards.filter(needsYou) : cards
  const step = (d: number) => {
    const i = cards.findIndex((c) => c.card_id === open)
    setOpen(cards[(i + d + cards.length) % cards.length]!.card_id)
  }
  useEffect(() => {
    if (open === null) return
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(null)
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [open])
  return (
    <>
      <PageHead title="Cards" info={<InfoTip about="cards" tip="What the agent made in this conversation. Open one to look at it, act on it or comment." />}>
        <Segmented
          small
          label="Show"
          value={only}
          onChange={setOnly}
          options={[
            { key: 'all', label: 'All', count: cards.length },
            { key: 'you', label: 'Needs you', count: cards.filter(needsYou).length },
          ]}
        />
      </PageHead>
      <div className={`vv${a ? ' is-open' : ''}${b ? ' is-two' : ''}`}>
        <ul className={`vv-list${a ? ' is-rail' : ''}`} aria-label="Cards">
          {shown.map((c) => (
            <CardTile
              key={c.card_id}
              card={c}
              acts={acts[c.card_id] ?? {}}
              act={actOn(c.card_id)}
              on={c.card_id === open || c.card_id === beside}
              rail={a !== undefined}
              open={() => {
                setOpen(c.card_id)
                if (c.card_id === beside) setBeside(null)
              }}
            />
          ))}
        </ul>
        {a ? (
          <div className="vv-stage">
            <ViewerPane
              key={a.card_id}
              card={a}
              acts={acts[a.card_id] ?? {}}
              act={actOn(a.card_id)}
              close={() => {
                setOpen(b ? b.card_id : null)
                setBeside(null)
              }}
              step={b ? undefined : step}
              beside={phone || b ? undefined : () => setBeside(cards[(cards.findIndex((c) => c.card_id === a.card_id) + 1) % cards.length]!.card_id)}
              narrow={b !== undefined || phone}
              phone={phone}
            />
            {b ? (
              <ViewerPane key={b.card_id} card={b} acts={acts[b.card_id] ?? {}} act={actOn(b.card_id)} close={() => setBeside(null)} narrow phone={false} />
            ) : null}
          </div>
        ) : null}
      </div>
    </>
  )
}
