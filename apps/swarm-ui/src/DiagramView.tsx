/**
 * A PLAN'S DIAGRAM, DRAWN SO THAT NOTHING IN IT CAN RUN (docs/design/plans-pipeline.md
 * §6.6, lane U7). The source is untrusted agent output; the API checks it where it
 * is stored and this checks it again where it is drawn:
 *
 * 1. Refuse lexically, naming the line: a `%%{` directive or a front-matter block
 *    (either can rewrite Mermaid's configuration), `click`/`call`/`href`/`callback`,
 *    `javascript:`/`data:`/`url(`, an HTML tag or comment in a label, a markdown
 *    label (drawn as HTML), styling statements and `@{` shapes. Over-refusing is
 *    the safe side: a refused diagram is shown as its source with the reason.
 * 2. Parse. A failure shows the parser's reason and the source, never a blank;
 *    so does a source over the node or edge caps once parsed.
 * 3. Render strictly: `securityLevel: 'strict'`, no HTML labels, and every config
 *    key `secure`, so no directive could change one even if step 1 missed it.
 * 4. Sanitise the SVG with DOMPurify's SVG profile, then walk it: no `script`,
 *    `foreignObject`, `a`, `image`, `use`, no `on*`, no `href`, no `url(` but a
 *    `#`-local one.
 * 5. Show it as an `<img>` of a `data:` URL. An SVG loaded as an image cannot run
 *    script, fetch anything or receive an event, whatever 1-4 missed.
 *
 * Mermaid and DOMPurify are imported lazily, so they are their own chunk and only
 * a page that draws a diagram loads them. A drawing is kept per theme and source,
 * so switching the console's theme swaps the image without drawing it again.
 */
import { useEffect, useMemo, useState } from 'react'
import { Button, buttonClass, CodeBlock, Tooltip } from './components'
import './styles/diagram.css'

export type DiagramTheme = 'light' | 'dark'

export interface DiagramViewProps {
  /** The Mermaid source, as stored on the plan. */
  source: string
  /** The diagram's title; with `why`, the image's accessible name. */
  title: string
  why?: string
  /** Forced theme; the console's own (data-theme, else the OS) when absent. */
  theme?: DiagramTheme
  /** `strip`: height-capped, as the pinned strip draws it; `large`: as Open large draws it. */
  size?: 'strip' | 'large'
}

/** §6.6's caps, the same numbers the API stores against. */
export const DIAGRAM_MAX_CHARS = 6000
export const DIAGRAM_MAX_LINES = 150
export const DIAGRAM_MAX_NODES = 60
export const DIAGRAM_MAX_EDGES = 100
/** Only for counting: 150 lines cannot hold many more edges than this. */
const EDGE_COUNT_CEILING = 1000

/** What step 1 refuses, in order; the first hit on the earliest line is the reason shown. */
const REFUSALS: readonly { test: RegExp; why: string }[] = [
  { test: /%%\{/, why: 'a %%{init} directive, which can change the renderer’s settings' },
  { test: /^\s*---\s*$/, why: 'a front-matter block, which can change the renderer’s settings' },
  { test: /(^|;)\s*click\b/i, why: 'a click handler' },
  { test: /(^|;)\s*(call|callback|href|links?)\b/i, why: 'a link or callback' },
  { test: /javascript:|data:|url\(/i, why: 'a script, data or url() reference' },
  { test: /<[A-Za-z/!]/, why: 'an HTML label' },
  { test: /["']`/, why: 'a markdown label, which is drawn as HTML' },
  { test: /(^|;)\s*(style|classDef|class|linkStyle)\b/, why: 'a styling statement' },
  { test: /@\{/, why: 'an @{…} shape or link property' },
]

export interface Refusal {
  line: number
  why: string
}

/** Step 1. The first refused line, or null for a source that may be parsed. */
export function refuseSource(source: string): Refusal | null {
  if (source.length > DIAGRAM_MAX_CHARS) return { line: 1, why: `over ${DIAGRAM_MAX_CHARS} characters` }
  const lines = source.split('\n')
  if (lines.length > DIAGRAM_MAX_LINES) return { line: DIAGRAM_MAX_LINES + 1, why: `over ${DIAGRAM_MAX_LINES} lines` }
  for (let i = 0; i < lines.length; i++) {
    const hit = REFUSALS.find((r) => r.test.test(lines[i]!))
    if (hit !== undefined) return { line: i + 1, why: hit.why }
  }
  return null
}

/** The parser's reason, short: "line 7, expecting '-->'". */
export function parseReason(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error)
  const line = /line (\d+)/i.exec(text)?.[1]
  const expecting = /Expecting (.*?)(?:, got (.*))?$/m.exec(text)
  if (expecting !== null) {
    const tokens = expecting[1]!.split(/,\s*/)
    const shown = tokens.slice(0, 4).join(', ') + (tokens.length > 4 ? ', …' : '')
    const got = expecting[2] !== undefined ? `, got ${expecting[2]}` : ''
    return `${line !== undefined ? `line ${line}, ` : ''}expecting ${shown}${got}`
  }
  const first = text.split('\n')[0]!.trim()
  return first.length > 160 ? `${first.slice(0, 159)}…` : first
}

// ---- the console's tokens, for the renderer ------------------------------------------------------------
// An SVG inside an <img> cannot read the page's CSS variables, so the image carries the
// colours itself. These mirror styles.css's tokens: --surface, --surface-2, --text,
// --text-dim and --info, dark (the base :root) and light.
const PALETTE: Readonly<Record<DiagramTheme, Readonly<Record<string, string | boolean>>>> = {
  dark: {
    darkMode: true,
    background: '#0c1a2e',
    mainBkg: '#11233b',
    primaryColor: '#11233b',
    primaryTextColor: '#eef4fa',
    primaryBorderColor: '#9fb0c4',
    secondaryColor: '#0c1a2e',
    tertiaryColor: '#0c1a2e',
    lineColor: '#9fb0c4',
    textColor: '#eef4fa',
    edgeLabelBackground: '#0c1a2e',
    noteBkgColor: '#11233b',
    noteTextColor: '#eef4fa',
    noteBorderColor: '#7cc8f8',
  },
  light: {
    darkMode: false,
    background: '#ffffff',
    mainBkg: '#eef4fa',
    primaryColor: '#eef4fa',
    primaryTextColor: '#0d1724',
    primaryBorderColor: '#4f5d6e',
    secondaryColor: '#ffffff',
    tertiaryColor: '#ffffff',
    lineColor: '#4f5d6e',
    textColor: '#0d1724',
    edgeLabelBackground: '#ffffff',
    noteBkgColor: '#eef4fa',
    noteTextColor: '#0d1724',
    noteBorderColor: '#0369a1',
  },
}
const FONT = 'ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif'

/** The console's theme as drawn now: `data-theme` on <html>, else the OS (dark is the base). */
function consoleTheme(): DiagramTheme {
  const set = document.documentElement.getAttribute('data-theme')
  if (set === 'light' || set === 'dark') return set
  return typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

function useConsoleTheme(forced: DiagramTheme | undefined): DiagramTheme {
  const [theme, setTheme] = useState<DiagramTheme>(() => forced ?? consoleTheme())
  useEffect(() => {
    if (forced !== undefined) {
      setTheme(forced)
      return
    }
    const update = () => setTheme(consoleTheme())
    update()
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: light)') : null
    media?.addEventListener('change', update)
    return () => {
      observer.disconnect()
      media?.removeEventListener('change', update)
    }
  }, [forced])
  return theme
}

// ---- parse, outline, render, sanitise ------------------------------------------------------------------
type Libs = { mermaid: typeof import('mermaid').default; purify: typeof import('dompurify').default }
let libs: Promise<Libs> | null = null
function loadLibs(): Promise<Libs> {
  libs ??= Promise.all([import('mermaid'), import('dompurify')]).then(([m, p]) => ({ mermaid: m.default, purify: p.default }))
  libs.catch(() => {
    libs = null
  })
  return libs
}

/** Mermaid's configuration is global: one diagram at a time is configured and drawn. */
let queue: Promise<unknown> = Promise.resolve()
function serially<T>(work: () => Promise<T>): Promise<T> {
  const next = queue.then(work, work)
  queue = next.catch(() => undefined)
  return next
}

function strictConfig(mermaid: Libs['mermaid'], theme: DiagramTheme, maxEdges = DIAGRAM_MAX_EDGES): Parameters<Libs['mermaid']['initialize']>[0] {
  const config = {
    startOnLoad: false,
    securityLevel: 'strict' as const,
    htmlLabels: false,
    flowchart: { htmlLabels: false },
    maxTextSize: DIAGRAM_MAX_CHARS,
    maxEdges,
    theme: 'base' as const,
    themeVariables: { ...PALETTE[theme], fontFamily: FONT },
    fontFamily: FONT,
  }
  const keys = new Set([...Object.keys(mermaid.mermaidAPI.defaultConfig ?? {}), ...Object.keys(config), 'secure'])
  return { ...config, secure: [...keys] }
}

interface Parsed {
  outline: string[] | null
  nodes: number
  edges: number
}

type Db = Record<string, unknown>
const call = <T,>(db: Db, name: string): T | undefined => (typeof db[name] === 'function' ? (db[name] as () => T).call(db) : undefined)
const asEntries = (v: unknown): [string, Record<string, unknown>][] =>
  v instanceof Map ? [...v.entries()] : v !== null && typeof v === 'object' ? Object.entries(v as Record<string, Record<string, unknown>>) : []

/** The outline from the parse (§6.6 "A text alternative"), and the counts the caps are held against. */
function outlineOf(type: string, db: Db): Parsed {
  if (type.startsWith('flowchart') || type === 'graph') {
    const vertices = new Map(asEntries(call(db, 'getVertices')))
    const name = (id: string) => String(vertices.get(id)?.text ?? id)
    const edges = (call<{ start: string; end: string; text?: string }[]>(db, 'getEdges') ?? []).map(
      (e) => `${name(e.start)} → ${name(e.end)}${e.text ? `, ${e.text}` : ''}`,
    )
    return { outline: edges, nodes: vertices.size, edges: edges.length }
  }
  if (type === 'sequence') {
    const actors = new Map(asEntries(call(db, 'getActors')))
    const name = (id: string) => String(actors.get(id)?.description ?? id)
    const types = (db.LINETYPE ?? {}) as Record<string, number>
    const lines = (call<{ from?: string; to?: string; message?: string; type?: number }[]>(db, 'getMessages') ?? [])
      .filter((m) => m.from !== undefined && m.to !== undefined)
      .map((m) => (m.type === types.NOTE ? `Note, ${name(m.from!)}: ${m.message ?? ''}` : `${name(m.from!)} → ${name(m.to!)}: ${m.message ?? ''}`))
    return { outline: lines, nodes: actors.size, edges: lines.length }
  }
  if (type.startsWith('state')) {
    const name = (id: string) => (id.endsWith('_start') ? 'start' : id.endsWith('_end') ? 'end' : id)
    const rels = (call<{ id1: string; id2: string; relationTitle?: string }[]>(db, 'getRelations') ?? []).map(
      (r) => `${name(r.id1)} → ${name(r.id2)}${r.relationTitle ? `, ${r.relationTitle}` : ''}`,
    )
    return { outline: rels, nodes: asEntries(call(db, 'getStates')).length, edges: rels.length }
  }
  if (type === 'er') {
    const entities = asEntries(call(db, 'getEntities'))
    const byId = new Map(entities.map(([key, e]) => [String(e.id ?? key), key]))
    const rels = (call<{ entityA: string; entityB: string; roleA?: string }[]>(db, 'getRelationships') ?? []).map(
      (r) => `${byId.get(r.entityA) ?? r.entityA} → ${byId.get(r.entityB) ?? r.entityB}${r.roleA ? `, ${r.roleA}` : ''}`,
    )
    return { outline: [...entities.map(([key]) => `Entity ${key}`), ...rels], nodes: entities.length, edges: rels.length }
  }
  return { outline: null, nodes: 0, edges: 0 }
}

const FORBIDDEN_TAGS = new Set(['script', 'foreignobject', 'a', 'image', 'use', 'iframe', 'embed', 'object', 'animate', 'set', 'animatemotion', 'animatetransform'])
const NON_LOCAL_URL = /url\(\s*(?!['"]?#)/i

/** Step 4: DOMPurify's SVG profile, then an explicit walk for what this page refuses outright. Well-formed XML out. */
export function sanitiseSvg(purify: Libs['purify'], svg: string): string {
  const fragment = purify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ['foreignObject', 'a', 'image', 'use', 'script'],
    FORBID_ATTR: ['href', 'xlink:href'],
    RETURN_DOM_FRAGMENT: true,
  })
  const root = fragment.querySelector('svg')
  if (root === null) throw new Error('the renderer returned no SVG')
  for (const el of [root, ...Array.from(root.querySelectorAll('*'))]) {
    if (FORBIDDEN_TAGS.has(el.localName.toLowerCase())) {
      el.remove()
      continue
    }
    if (el.localName === 'style' && (NON_LOCAL_URL.test(el.textContent ?? '') || /@import/i.test(el.textContent ?? ''))) {
      el.remove()
      continue
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase()
      if (name.startsWith('on') || name === 'href' || name.endsWith(':href') || NON_LOCAL_URL.test(attr.value)) el.removeAttribute(attr.name)
    }
  }
  return new XMLSerializer().serializeToString(root)
}

interface Drawn {
  svg: string
  parsed: Parsed
}

class DiagramFailure extends Error {}

const drawings = new Map<string, Promise<Drawn>>()

function draw(source: string, theme: DiagramTheme): Promise<Drawn> {
  const key = `${theme}\n${source}`
  let drawing = drawings.get(key)
  if (drawing === undefined) {
    drawing = loadLibs().then(({ mermaid, purify }) =>
      serially(async () => {
        // Parsed under a looser edge ceiling only to count, so an over-cap source is told its real
        // count (Mermaid's own limit stops counting at the cap); drawn under the cap itself.
        mermaid.initialize(strictConfig(mermaid, theme, EDGE_COUNT_CEILING))
        let parsed: Parsed
        try {
          const diagram = await mermaid.mermaidAPI.getDiagramFromText(source)
          parsed = outlineOf(diagram.type, diagram.db as unknown as Db)
        } catch (error) {
          throw new DiagramFailure(parseReason(error))
        }
        if (parsed.nodes > DIAGRAM_MAX_NODES) throw new DiagramFailure(`${parsed.nodes} nodes, over the cap of ${DIAGRAM_MAX_NODES}`)
        if (parsed.edges > DIAGRAM_MAX_EDGES) throw new DiagramFailure(`${parsed.edges} edges, over the cap of ${DIAGRAM_MAX_EDGES}`)
        mermaid.initialize(strictConfig(mermaid, theme))
        let svg: string
        try {
          svg = (await mermaid.render(`dg-${Math.random().toString(36).slice(2)}`, source)).svg
        } catch (error) {
          throw new DiagramFailure(parseReason(error))
        }
        return { svg: sanitiseSvg(purify, svg), parsed }
      }),
    )
    drawings.set(key, drawing)
    drawing.catch(() => drawings.delete(key))
  }
  return drawing
}

// ---- export --------------------------------------------------------------------------------------------
const fileName = (title: string, ext: string) => `${title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'diagram'}.${ext}`

function download(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** A PNG drawn from the image on a canvas, at twice its size, on the theme's background. */
function downloadPng(src: string, background: string, name: string, done: (ok: boolean) => void): void {
  const img = new Image()
  img.onload = () => {
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, img.naturalWidth * 2)
    canvas.height = Math.max(1, img.naturalHeight * 2)
    const ctx = canvas.getContext('2d')
    if (ctx === null) return done(false)
    ctx.fillStyle = background
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
    canvas.toBlob((blob) => {
      if (blob === null) return done(false)
      download(blob, name)
      done(true)
    }, 'image/png')
  }
  img.onerror = () => done(false)
  img.src = src
}

// ---- the view ------------------------------------------------------------------------------------------
type View = { kind: 'drawing' } | { kind: 'failed'; reason: string } | { kind: 'drawn'; drawn: Drawn }

export default function DiagramView({ source, title, why, theme: forced, size = 'strip' }: DiagramViewProps) {
  const theme = useConsoleTheme(forced)
  const refusal = useMemo(() => refuseSource(source), [source])
  const [view, setView] = useState<View>({ kind: 'drawing' })
  const [note, setNote] = useState('')

  useEffect(() => {
    if (refusal !== null) return
    let live = true
    draw(source, theme).then(
      (drawn) => live && setView({ kind: 'drawn', drawn }),
      (error: unknown) => live && setView({ kind: 'failed', reason: error instanceof DiagramFailure ? error.message : parseReason(error) }),
    )
    return () => {
      live = false
    }
  }, [source, theme, refusal])

  const label = why ? `${title}: ${why}` : title
  const failure = refusal !== null ? `refused at line ${refusal.line}: ${refusal.why}` : view.kind === 'failed' ? view.reason : null

  if (failure !== null)
    return (
      <figure className="dg is-failed" data-testid="dg-failed" aria-label={title}>
        <figcaption className="dg-why">
          <b>This diagram did not render:</b> {failure}
          <Tooltip tip="Diagram source is untrusted. Anything that could run, or that does not parse, is shown as source instead.">
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About diagram safety">
              <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <circle cx="12" cy="12" r="8.5" />
                <path d="M12 11v5.5M12 7.75v.01" />
              </svg>
            </button>
          </Tooltip>
        </figcaption>
        <CodeBlock title="Source" lang="mermaid" text={source} />
      </figure>
    )

  if (view.kind !== 'drawn')
    return (
      <figure className="dg is-drawing" aria-busy="true" aria-label={title}>
        <span className="dg-dim">Drawing…</span>
      </figure>
    )

  const { svg, parsed } = view.drawn
  const src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
  const copy = () => {
    const done = (ok: boolean) => setNote(ok ? 'Copied' : 'Copy failed')
    if (navigator.clipboard?.writeText === undefined) return done(false)
    navigator.clipboard.writeText(source).then(
      () => done(true),
      () => done(false),
    )
  }
  return (
    <figure className={`dg is-${size} is-${theme}`} aria-label={title}>
      <div className="dg-img">
        <img src={src} alt={label} />
      </div>
      <figcaption className="dg-bar">
        <Button kind="ghost" size="sm" aria-label="Copy the source" onClick={copy}>
          Copy source
        </Button>
        <Button kind="ghost" size="sm" aria-label="Download the SVG" onClick={() => download(new Blob([svg], { type: 'image/svg+xml' }), fileName(title, 'svg'))}>
          SVG
        </Button>
        <Button
          kind="ghost"
          size="sm"
          aria-label="Download a PNG"
          onClick={() => downloadPng(src, String(PALETTE[theme].background), fileName(title, 'png'), (ok) => setNote(ok ? '' : 'PNG failed'))}
        >
          PNG
        </Button>
        <span className="dg-dim" role="status">
          {note}
        </span>
      </figcaption>
      <details className="dg-text">
        <summary>Read as text</summary>
        {parsed.outline !== null && parsed.outline.length > 0 ? (
          <ol data-testid="dg-outline">
            {parsed.outline.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ol>
        ) : (
          <p className="dg-dim" data-testid="dg-outline">
            No outline for this kind; the source follows.
          </p>
        )}
        <CodeBlock title="Source" lang="mermaid" text={source} />
      </details>
    </figure>
  )
}
