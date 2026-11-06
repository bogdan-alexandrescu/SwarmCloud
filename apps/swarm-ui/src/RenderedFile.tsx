import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react'

import { USE_FIXTURES, loadCheckpoints } from './api'
import { loadCheckpointFile } from './CheckpointBrowser'
import { Segmented, buttonClass } from './components'
import type { PatchSection } from './WorkflowChanges'

/**
 * AN AGENT'S MOCK-UP, DRAWN (BGD, 2026-11-06: "see the mockups produced by
 * the workflow agents in here ... and not just the file name"). The Changes
 * view's file pane opens an HTML page, an SVG or an image on a Rendered view,
 * with Diff one click away and unchanged.
 *
 * WHERE THE BYTES COME FROM, in order: the step's own checkpoint, through the
 * checkpoint file route (`GET /v1/tasks/{id}/checkpoints/{n}/files/{path}`,
 * text only, redacted at read time); then the step's patch -- the added lines
 * of a newly added text file, or the `GIT binary patch` literal of an image
 * (the worker diffs with `--binary`, gitops.py). Anything else is one line
 * saying why it was not read, and Diff still works.
 *
 * THE AGENT'S HTML NEVER TOUCHES THE CONSOLE'S DOM. It is drawn only in an
 * iframe whose `sandbox` attribute is EMPTY (no scripts, no same-origin, no
 * forms, no popups, no navigation), fed by `srcdoc` -- never `src`, never an
 * object or embed tag, never a blob URL, which would carry the console's
 * origin. A Content-Security-Policy is written into the page's head so it
 * fetches nothing either: no beacon to an outside host, no request to the
 * console's own routes resolved against its base URL. Images go through an
 * img element from a Blob object URL, revoked when it is replaced or unmounted.
 */

/** Over this many bytes a file is not rendered (BGD's brief, 2026-11-06). */
export const RENDER_CAP_BYTES = 5 * 1024 * 1024

export type RenderKind = 'html' | 'svg' | 'image'

const IMAGE_TYPES: Readonly<Record<string, string>> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
}

function extOf(path: string): string {
  const base = path.slice(path.lastIndexOf('/') + 1)
  const dot = base.lastIndexOf('.')
  return dot <= 0 ? '' : base.slice(dot + 1).toLowerCase()
}

/** How a file is rendered, by its name; null: it is not (its pane is the diff alone). */
export function renderKind(path: string): RenderKind | null {
  const ext = extOf(path)
  if (ext === 'html' || ext === 'htm') return 'html'
  if (ext === 'svg') return 'svg'
  return ext in IMAGE_TYPES ? 'image' : null
}

// ---------------------------------------------------------------------------
// The page as the frame gets it
// ---------------------------------------------------------------------------

/**
 * What the page may load: inline styles and data: URLs, plus stylesheets and
 * fonts over https (the owner's pick on #3462, so a mock-up that links a web
 * font or a CDN stylesheet draws as designed). Everything else -- scripts,
 * frames, fetches, images and media off the page, any plain-http load --
 * stays blocked by default-src 'none'.
 */
export const FRAME_CSP =
  "default-src 'none'; style-src 'unsafe-inline' https:; img-src data:; font-src data: https:; media-src data:"

export type Theme = 'light' | 'dark'

/**
 * The theme on the page's html element. The ux-mockups kit's frames set it
 * from a `?theme=` script, which the sandbox never runs, so the attribute the
 * script would have set is written into the text instead.
 */
export function withTheme(html: string, theme: Theme): string {
  const open = /<html\b[^>]*>/i.exec(html)
  if (open === null) return `<html data-theme="${theme}">${html}</html>`
  const tag = open[0].replace(/\sdata-theme\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/i, '').replace(/^<html\b/i, `<html data-theme="${theme}"`)
  return html.slice(0, open.index) + tag + html.slice(open.index + open[0].length)
}

/** The CSP meta, first in the head (or the document) so it governs everything after it. */
export function withPolicy(html: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">`
  const head = /<head\b[^>]*>/i.exec(html)
  if (head !== null) return html.slice(0, head.index + head[0].length) + meta + html.slice(head.index + head[0].length)
  const open = /<html\b[^>]*>/i.exec(html)
  if (open !== null) return html.slice(0, open.index + open[0].length) + `<head>${meta}</head>` + html.slice(open.index + open[0].length)
  return meta + html
}

/** The `srcdoc` text for a page or an SVG. */
export function frameDoc(kind: 'html' | 'svg', text: string, theme: Theme): string {
  if (kind === 'svg') return withPolicy(`<!doctype html><html><head></head><body style="margin:0">${text}</body></html>`)
  return withPolicy(withTheme(text, theme))
}

// ---------------------------------------------------------------------------
// The viewport a frame was recorded at
// ---------------------------------------------------------------------------

export interface Viewport {
  width: number
  height: number
}

/** The kit's two recordings: `<name>-390` on a phone, 1440x900 otherwise. */
export const PHONE_VIEWPORT: Viewport = { width: 390, height: 844 }
export const DESKTOP_VIEWPORT: Viewport = { width: 1440, height: 900 }

function viewportOf(v: unknown): Viewport | null {
  const ok = (w: unknown, h: unknown): Viewport | null =>
    typeof w === 'number' && typeof h === 'number' && w > 0 && h > 0 && w <= 10_000 && h <= 10_000 ? { width: w, height: h } : null
  if (typeof v === 'string') {
    const m = /^\s*(\d+)\s*[x×]\s*(\d+)\s*$/i.exec(v)
    return m === null ? null : ok(Number(m[1]), Number(m[2]))
  }
  if (typeof v === 'number') return ok(v, v <= PHONE_VIEWPORT.width ? PHONE_VIEWPORT.height : DESKTOP_VIEWPORT.height)
  if (Array.isArray(v)) return ok(v[0], v[1])
  if (typeof v === 'object' && v !== null) {
    const o = v as Record<string, unknown>
    return ok(o.width ?? o.w, o.height ?? o.h)
  }
  return null
}

/**
 * The frame's viewport: its row in `frames.json` (a list of `{name,
 * viewport}`, or `{frames: [...]}`) when one names it, else the kit's naming.
 */
export function frameViewport(path: string, framesJson: string | null): Viewport {
  const base = path.slice(path.lastIndexOf('/') + 1)
  const name = base.replace(/\.[^.]*$/, '')
  if (framesJson !== null) {
    try {
      const parsed: unknown = JSON.parse(framesJson)
      const rows: unknown = Array.isArray(parsed) ? parsed : (parsed as { frames?: unknown } | null)?.frames
      if (Array.isArray(rows)) {
        for (const r of rows) {
          if (typeof r !== 'object' || r === null) continue
          const row = r as { name?: unknown; viewport?: unknown }
          if (row.name !== name && row.name !== base) continue
          const v = viewportOf(row.viewport)
          if (v !== null) return v
        }
      }
    } catch {
      // A frames.json that does not parse names nothing; the kit's naming stands.
    }
  }
  return /-390$/.test(name) ? PHONE_VIEWPORT : DESKTOP_VIEWPORT
}

// ---------------------------------------------------------------------------
// The patch's own copy of the file
// ---------------------------------------------------------------------------

/** A newly added text file's content, from its section's added lines; null for anything else. */
export function addedText(section: PatchSection): string | null {
  const f = section.file
  if (f.status !== 'added' || f.binary) return null
  const lines = f.hunks.flatMap((h) => h.lines.filter((l) => l.kind === 'add').map((l) => l.text))
  if (lines.length === 0) return ''
  const noEol = /\n\\ No newline at end of file\n?$/.test(section.text)
  return lines.join('\n') + (noEol ? '' : '\n')
}

const B85 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{|}~'

/** Git's base85 lines (a length letter, then groups of five) as bytes; null if one is malformed. */
export function decodeGitBase85(lines: readonly string[]): Uint8Array | null {
  const out: number[] = []
  for (const line of lines) {
    const c = line.charCodeAt(0)
    const len = c >= 65 && c <= 90 ? c - 64 : c >= 97 && c <= 122 ? c - 96 + 26 : -1
    const body = line.slice(1)
    if (len < 0 || body.length % 5 !== 0 || body.length / 5 < Math.ceil(len / 4)) return null
    const bytes: number[] = []
    for (let i = 0; i < body.length; i += 5) {
      let acc = 0
      for (let j = 0; j < 5; j++) {
        const d = B85.indexOf(body[i + j]!)
        if (d < 0) return null
        acc = acc * 85 + d
      }
      if (acc > 0xffffffff) return null
      bytes.push((acc >>> 24) & 255, (acc >>> 16) & 255, (acc >>> 8) & 255, acc & 255)
    }
    out.push(...bytes.slice(0, len))
  }
  return Uint8Array.from(out)
}

async function inflate(data: Uint8Array): Promise<Uint8Array> {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(data)
      c.close()
    },
  }).pipeThrough(new DecompressionStream('deflate') as unknown as ReadableWritablePair<Uint8Array, Uint8Array>)
  const reader = stream.getReader()
  const parts: Uint8Array[] = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    parts.push(value)
    total += value.length
    if (total > RENDER_CAP_BYTES) throw new Error('over the cap')
  }
  const out = new Uint8Array(total)
  let at = 0
  for (const p of parts) {
    out.set(p, at)
    at += p.length
  }
  return out
}

/** Why a binary section gives no bytes, or its declared size and data lines. */
function binaryLiteral(section: PatchSection): { size: number; lines: string[] } | string {
  const rows = section.text.split('\n')
  const at = rows.findIndex((r) => r === 'GIT binary patch')
  if (at < 0) return 'the patch records this image as changed without its bytes'
  const head = /^(literal|delta) (\d+)$/.exec(rows[at + 1] ?? '')
  if (head === null) return 'the patch’s binary section did not parse'
  if (head[1] === 'delta') return 'the patch holds this image as a delta against its old bytes, which are not in the patch'
  const lines: string[] = []
  for (const r of rows.slice(at + 2)) {
    if (r === '') break
    lines.push(r)
  }
  return { size: Number(head[2]), lines }
}

// ---------------------------------------------------------------------------
// Reading the file
// ---------------------------------------------------------------------------

export type RenderedContent =
  | { kind: 'text'; text: string; from: 'checkpoint' | 'patch' }
  | { kind: 'bytes'; bytes: Uint8Array; type: string }
  | { kind: 'unread'; reason: string }
  | { kind: 'too-big'; bytes: number }

/** Where a step's copy of the file can be read. */
export interface RenderSource {
  taskId: string | null
  section: PatchSection | null
}

const byteLength = (s: string) => new TextEncoder().encode(s).length

/**
 * The file as the step's newest checkpoint holds it: the checkpoint the task's
 * pointer names, else the newest listed. The archive is the work directory, so
 * a repository file is `repo/<path>`; the bare path is tried after it.
 */
async function fromCheckpoint(taskId: string, path: string): Promise<{ text: string } | { tooBig: number } | { why: string }> {
  // The fixtures' checkpoint archive holds no file of the fixture workflows'
  // patches (CheckpointBrowser's fixtureFile answers every path with a line of
  // its own), so development reads the patch, as a step without a checkpoint does.
  if (USE_FIXTURES) return { why: 'no checkpoint holds it' }
  const page = await loadCheckpoints(taskId)
  if (page.status !== 'ok' && page.status !== 'stale') return { why: page.status === 'error' ? `the checkpoint list was not read: ${page.error.message}` : 'no checkpoint holds it' }
  const rows = page.data.checkpoints
  const row = rows.find((c) => c.is_latest_pointer) ?? rows[0]
  if (row === undefined) return { why: 'this step wrote no checkpoint' }
  let why = 'its checkpoint does not hold it'
  for (const member of [`repo/${path}`, path]) {
    const r = await loadCheckpointFile(taskId, row.attempt_id, row.checkpoint_id, member)
    if (r.status === 'error') {
      if (r.error.httpStatus === 404) continue
      return { why: `its checkpoint was not read: ${r.error.message}` }
    }
    if (r.status !== 'ok' && r.status !== 'stale') continue
    const d = r.data
    if (d.total_bytes !== null && d.total_bytes > RENDER_CAP_BYTES) return { tooBig: d.total_bytes }
    if (d.status === 'absent') continue
    if (d.status !== 'ok' || d.content === null) return { why: d.detail ?? `its checkpoint serves it as ${d.status}` }
    if (d.truncated || d.next_offset !== null || d.offset !== 0) {
      why = 'its checkpoint served only a window of it'
      break
    }
    return { text: d.content }
  }
  return { why }
}

/** The step's copy of the file, from its checkpoint, else its patch, else the reasons neither gave it. */
export async function readRendered(path: string, kind: RenderKind, source: RenderSource): Promise<RenderedContent> {
  const why: string[] = []
  if (kind !== 'image') {
    if (source.taskId !== null) {
      const c = await fromCheckpoint(source.taskId, path)
      if ('text' in c) return byteLength(c.text) > RENDER_CAP_BYTES ? { kind: 'too-big', bytes: byteLength(c.text) } : { kind: 'text', text: c.text, from: 'checkpoint' }
      if ('tooBig' in c) return { kind: 'too-big', bytes: c.tooBig }
      why.push(c.why)
    }
    const s = source.section
    const text = s === null ? null : addedText(s)
    if (text !== null) return byteLength(text) > RENDER_CAP_BYTES ? { kind: 'too-big', bytes: byteLength(text) } : { kind: 'text', text, from: 'patch' }
    why.push(s === null ? 'its patch section is not in hand' : 'the patch holds only the changed lines of a file that already existed')
    return { kind: 'unread', reason: why.join('; ') }
  }
  // The checkpoint file route serves text only, so an image's bytes are the patch's.
  const s = source.section
  if (s === null) return { kind: 'unread', reason: 'its patch section is not in hand' }
  if (!s.file.binary) return { kind: 'unread', reason: 'the patch holds no bytes for this image' }
  const lit = binaryLiteral(s)
  if (typeof lit === 'string') return { kind: 'unread', reason: lit }
  if (lit.size > RENDER_CAP_BYTES) return { kind: 'too-big', bytes: lit.size }
  const packed = decodeGitBase85(lit.lines)
  if (packed === null) return { kind: 'unread', reason: 'the patch’s binary section is not valid base85' }
  if (typeof DecompressionStream === 'undefined') return { kind: 'unread', reason: 'this browser cannot inflate the patch’s bytes' }
  try {
    const bytes = await inflate(packed)
    if (bytes.length !== lit.size) return { kind: 'unread', reason: `the patch’s bytes inflate to ${bytes.length}, not the ${lit.size} it declares` }
    return { kind: 'bytes', bytes, type: IMAGE_TYPES[extOf(path)] ?? 'application/octet-stream' }
  } catch {
    return { kind: 'unread', reason: 'the patch’s bytes did not inflate' }
  }
}

// ---------------------------------------------------------------------------
// The pane
// ---------------------------------------------------------------------------

/** The (i): the explanation the page does not print (BGD's rule, 2026-11-05). */
function About({ about, tip }: { about: string; tip: string }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  return (
    <span className="c-tip-host" style={{ verticalAlign: 'middle' }} onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })}
        aria-label={`About ${about}`}
        aria-describedby={id}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false)
        }}
      >
        <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 11v5.5M12 7.75v.01" />
        </svg>
      </button>
      <span role="tooltip" id={id} className="c-tip" data-help-description="" hidden={!open}>
        {tip}
      </span>
    </span>
  )
}

/** The pane's width, for scaling a frame to fit; null until measured. */
function usePaneWidth(): [RefObject<HTMLDivElement>, number | null] {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState<number | null>(null)
  useEffect(() => {
    const el = ref.current
    if (el === null) return
    const measure = () => setWidth(el.clientWidth > 0 ? el.clientWidth : null)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, width]
}

function Frame({ path, doc, viewport }: { path: string; doc: string; viewport: Viewport }) {
  const [ref, width] = usePaneWidth()
  const [zoom, setZoom] = useState<'fit' | 'full'>('fit')
  const scale = zoom === 'full' || width === null ? 1 : Math.min(1, width / viewport.width)
  return (
    <div ref={ref} className="chg-render-frame" style={{ overflow: 'auto', maxWidth: '100%' }}>
      <div className="chg-sum">
        <span className="mono">
          {viewport.width}×{viewport.height}
        </span>
        <Segmented
          small
          label="Zoom"
          value={zoom}
          options={[
            { key: 'fit', label: 'Fit' },
            { key: 'full', label: '100%' },
          ]}
          onChange={setZoom}
        />
      </div>
      <div style={{ width: viewport.width * scale, height: viewport.height * scale, overflow: 'hidden' }}>
        <iframe
          title={`${path}, rendered`}
          sandbox=""
          srcDoc={doc}
          referrerPolicy="no-referrer"
          width={viewport.width}
          height={viewport.height}
          style={{ border: 0, display: 'block', background: '#fff', transform: scale === 1 ? undefined : `scale(${scale})`, transformOrigin: '0 0' }}
        />
      </div>
    </div>
  )
}

function Picture({ path, bytes, type }: { path: string; bytes: Uint8Array; type: string }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    const u = URL.createObjectURL(new Blob([bytes as Uint8Array<ArrayBuffer>], { type }))
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [bytes, type])
  if (url === null) return null
  return <img className="chg-render-img" src={url} alt={path} style={{ maxWidth: '100%', height: 'auto', display: 'block' }} />
}

/**
 * THE FILE PANE'S RENDERED | DIFF SWITCH. `diff` is today's view, drawn as it
 * was; Rendered reads the step's copy once per file and draws it. A file that
 * cannot be rendered says why in one line and leaves Diff to read.
 */
export function RenderedOrDiff({
  path,
  source,
  framesSource,
  diff,
}: {
  path: string
  source: RenderSource
  /** Where the frames' `frames.json` beside this file can be read; null: nowhere. */
  framesSource: RenderSource | null
  diff: ReactNode
}) {
  const kind = renderKind(path)
  const [view, setView] = useState<'rendered' | 'diff'>('rendered')
  const [theme, setTheme] = useState<Theme>('light')
  const [read, setRead] = useState<{ key: string; content: RenderedContent; frames: string | null } | null>(null)
  const sectionKey = source.section?.text ?? ''
  const key = `${path}\0${source.taskId ?? ''}\0${sectionKey.length}\0${sectionKey.slice(0, 256)}`
  useEffect(() => {
    if (kind === null) return
    let live = true
    void (async () => {
      const content = await readRendered(path, kind, source)
      let frames: string | null = null
      if (kind === 'html' && content.kind === 'text' && framesSource !== null) {
        const f = await readRendered(`${path.slice(0, path.lastIndexOf('/') + 1)}frames.json`, 'html', framesSource)
        if (f.kind === 'text') frames = f.text
      }
      if (live) setRead({ key, content, frames })
    })()
    return () => {
      live = false
    }
    // `source` is new on every render; `key` names what it reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, kind])
  if (kind === null) return <>{diff}</>
  const content = read !== null && read.key === key ? read.content : null
  return (
    <div className="chg-render">
      <div className="chg-sum">
        <Segmented
          small
          label="File view"
          value={view}
          options={[
            { key: 'rendered', label: 'Rendered' },
            { key: 'diff', label: 'Diff' },
          ]}
          onChange={setView}
        />
        {view === 'rendered' && kind === 'html' && (
          <Segmented
            small
            label="Theme"
            value={theme}
            options={[
              { key: 'light', label: 'Light' },
              { key: 'dark', label: 'Dark' },
            ]}
            onChange={setTheme}
          />
        )}
        {view === 'rendered' && (
          <About
            about="the rendered view"
            tip="Drawn from the step’s own checkpoint, else from its patch, in a sandbox that runs no script and loads nothing. The theme is set on the page’s html element, since the sandbox never runs the frame’s own theme script."
          />
        )}
      </div>
      {view === 'diff' ? (
        diff
      ) : content === null ? (
        <p className="art-loading" role="status" aria-label={`Reading ${path}`}>
          <span className="ctl-pending art-loading-bar" />
        </p>
      ) : content.kind === 'unread' ? (
        <p className="chg-render-why" role="status">
          not read: {content.reason}
        </p>
      ) : content.kind === 'too-big' ? (
        <p className="chg-render-why" role="status">
          not rendered: {(content.bytes / (1024 * 1024)).toFixed(1)} MB, over the 5 MB cap
        </p>
      ) : content.kind === 'bytes' ? (
        <Picture path={path} bytes={content.bytes} type={content.type} />
      ) : kind === 'image' ? (
        <p className="chg-render-why" role="status">
          not read: the image arrived as text
        </p>
      ) : (
        <Frame
          path={path}
          doc={frameDoc(kind, content.text, theme)}
          viewport={kind === 'html' ? frameViewport(path, read?.frames ?? null) : DESKTOP_VIEWPORT}
        />
      )}
    </div>
  )
}
