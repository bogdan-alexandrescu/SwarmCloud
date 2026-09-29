// <DiffView patch={string} getFile? /> -- A SELF-CONTAINED DIFF VIEWER (#310).
//
// What it draws: a file list with +/- counts, one collapsible section per file
// with badges for what the file list cannot say in a count (binary, renamed,
// copied, mode change, added, deleted), hunk headers, and both line-number
// gutters -- unified, or side by side at 700px and over.
//
// THREE RULES IT KEEPS, each the reason something below looks the way it does.
//
//   1. TEXT EXACTLY AS GIVEN. Every character of the patch reaches the DOM as
//      a React text node: no `dangerouslySetInnerHTML`, no highlighter, no
//      trimming. A carriage return that belongs to the content and a missing
//      final newline are drawn as visible marks rather than dropped, because
//      both are real differences a reader may be looking for.
//   2. NO INVENTED LINES. The unchanged lines between hunks are not in the
//      patch. Without `getFile` the viewer says `context not available`; with
//      it, the file is checked against every line the patch says it holds, and
//      a file that disagrees -- another revision, a truncated read -- shows
//      nothing rather than lines that may not be the file's.
//   3. A BIG PATCH COSTS WHAT IS ON SCREEN. Rows are fixed-height and drawn
//      only inside the scroller's window (rows.ts), so 50,000 lines is two
//      spacers and a hundred-odd rows.
//
// Keys, while focus is anywhere inside the viewer except a text field:
// n / p next and previous file, j / k next and previous hunk, / find.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

import { parseUnifiedDiff, type DiffFile, type DiffLine } from './parse'
import {
  buildRows,
  contextAgrees,
  findMatches,
  ROW_H,
  rowAt,
  rowHeight,
  rowOffsets,
  rowOfLine,
  splitFile,
  widestLine,
  type ContextState,
  type Match,
  type Row,
} from './rows'
import { rememberedViewMode, rememberViewMode, type ViewMode } from './storage'

export type FileSide = 'old' | 'new'

export interface DiffViewProps {
  patch: string
  /**
   * The whole file on one side of the patch, for expanding the unchanged lines
   * between hunks. Optional: without it those lines are reported as not
   * available, never guessed.
   */
  getFile?: (path: string, side: FileSide) => Promise<string> | string
}

/** Below this width the viewer is unified whatever was chosen: two columns of code do not fit. */
export const SPLIT_MIN_WIDTH = 700

/**
 * jsdom, and a scroller not yet laid out, report a height of 0. The window is
 * then sized as if the scroller were this tall, so the first paint is never
 * empty.
 */
const FALLBACK_VIEWPORT = 800
/** Rows drawn beyond each edge of the window, in px, so a fast scroll does not show blank space. */
const OVERSCAN_PX = 1200
/** A match is scrolled to this far below the top, so the lines above it are in view. */
const MATCH_LEAD = ROW_H * 3

type Target = { kind: 'file'; file: number } | { kind: 'match'; index: number }

export function DiffView({ patch, getFile }: DiffViewProps) {
  const parsed = useMemo(() => parseUnifiedDiff(patch), [patch])
  if (!parsed.ok) {
    return (
      <section className="diff" aria-label="Diff">
        <p className="diff-error" role="alert">
          patch not read: {parsed.error.message} · line {parsed.error.line}
        </p>
      </section>
    )
  }
  if (parsed.files.length === 0) {
    return (
      <section className="diff" aria-label="Diff">
        <p className="diff-empty">no files in this patch</p>
      </section>
    )
  }
  // Keyed by the patch, so a new patch starts with no collapsed files, no
  // loaded context and no find state that indexes into the old one.
  return <Viewer key={patch} files={parsed.files} getFile={getFile} />
}

function Viewer({ files, getFile }: { files: DiffFile[]; getFile?: DiffViewProps['getFile'] }) {
  const rootRef = useRef<HTMLElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const findRef = useRef<HTMLInputElement>(null)
  const alive = useRef(true)

  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set())
  const [preferred, setPreferred] = useState<ViewMode>(rememberedViewMode)
  const [width, setWidth] = useState<number>(() => (typeof window === 'undefined' ? 1024 : window.innerWidth))
  const [viewport, setViewport] = useState(FALLBACK_VIEWPORT)
  const [scrollTop, setScrollTop] = useState(0)
  const [context, setContext] = useState<ReadonlyMap<number, ContextState>>(() => new Map())
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set())
  const [query, setQuery] = useState('')
  const [matchIdx, setMatchIdx] = useState(0)
  const [target, setTarget] = useState<Target | null>(null)

  const narrow = width < SPLIT_MIN_WIDTH
  const mode: ViewMode = narrow ? 'unified' : preferred

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // Width and height. The viewer's own box when it has been laid out, the
  // window otherwise; a ResizeObserver where there is one, `resize` always.
  useEffect(() => {
    const measure = (): void => {
      const w = rootRef.current?.clientWidth || window.innerWidth
      const h = scrollRef.current?.clientHeight || FALLBACK_VIEWPORT
      setWidth(w)
      setViewport(h)
    }
    measure()
    window.addEventListener('resize', measure)
    let ro: ResizeObserver | null = null
    if (typeof ResizeObserver !== 'undefined' && rootRef.current) {
      ro = new ResizeObserver(measure)
      ro.observe(rootRef.current)
    }
    return () => {
      window.removeEventListener('resize', measure)
      ro?.disconnect()
    }
  }, [])

  const rows = useMemo(
    () => buildRows({ files, collapsed, mode, canExpand: getFile !== undefined, context, opened }),
    [files, collapsed, mode, getFile, context, opened],
  )
  const offsets = useMemo(() => rowOffsets(rows), [rows])
  const total = offsets[rows.length] ?? 0

  const widest = useMemo(() => {
    let w = widestLine(files)
    for (const c of context.values()) {
      if (c.status === 'ready') for (const l of c.lines) if (l.length > w) w = l.length
    }
    return w
  }, [files, context])

  const matches = useMemo(() => findMatches(files, query), [files, query])
  const byLine = useMemo(() => {
    const m = new Map<string, Match[]>()
    for (const x of matches) {
      const k = `${x.file}:${x.hunk}:${x.line}`
      const list = m.get(k)
      if (list) list.push(x)
      else m.set(k, [x])
    }
    return m
  }, [matches])
  const current: Match | undefined = matches[matchIdx]

  const scrollTo = useCallback((y: number) => {
    const top = Math.max(0, y)
    if (scrollRef.current) scrollRef.current.scrollTop = top
    setScrollTop(top)
  }, [])

  // A target is resolved after the rows it needs exist: expanding a file and
  // scrolling to it is one click, but two renders.
  useLayoutEffect(() => {
    if (target === null) return
    let idx = -1
    let lead = 0
    if (target.kind === 'file') {
      idx = rows.findIndex((r) => r.t === 'file' && r.file === target.file)
    } else {
      const m = matches[target.index]
      if (m) {
        idx = rowOfLine(rows, m.file, m.hunk, m.line)
        lead = MATCH_LEAD
      }
    }
    if (idx >= 0) scrollTo((offsets[idx] ?? 0) - lead)
    setTarget(null)
  }, [target, rows, offsets, matches, scrollTo])

  const expandFiles = (which: Iterable<number>): void => {
    setCollapsed((prev) => {
      let next: Set<number> | null = null
      for (const f of which) {
        if (prev.has(f)) {
          next ??= new Set(prev)
          next.delete(f)
        }
      }
      return next ?? prev
    })
  }

  const goFile = (fi: number): void => {
    expandFiles([fi])
    setTarget({ kind: 'file', file: fi })
  }

  const toggleFile = (fi: number): void => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(fi)) next.delete(fi)
      else next.add(fi)
      return next
    })
  }

  const allCollapsed = collapsed.size === files.length
  const toggleAll = (): void => {
    setCollapsed(allCollapsed ? new Set() : new Set(files.map((_, i) => i)))
  }

  const choose = (m: ViewMode): void => {
    setPreferred(m)
    rememberViewMode(m)
  }

  const onQuery = (q: string): void => {
    setQuery(q)
    setMatchIdx(0)
    const found = findMatches(files, q)
    if (found.length > 0) {
      expandFiles(new Set(found.map((m) => m.file)))
      setTarget({ kind: 'match', index: 0 })
    }
  }

  const step = (dir: 1 | -1): void => {
    if (matches.length === 0) return
    const next = (matchIdx + dir + matches.length) % matches.length
    setMatchIdx(next)
    expandFiles([matches[next]!.file])
    setTarget({ kind: 'match', index: next })
  }

  const jump = (kind: 'file' | 'hunk', dir: 1 | -1): void => {
    const y = scrollTop
    if (dir === 1) {
      for (let i = 0; i < rows.length; i++) {
        if (rows[i]!.t === kind && offsets[i]! > y + 0.5) return scrollTo(offsets[i]!)
      }
    } else {
      for (let i = rows.length - 1; i >= 0; i--) {
        if (rows[i]!.t === kind && offsets[i]! < y - 0.5) return scrollTo(offsets[i]!)
      }
    }
  }

  const onKey = (e: KeyboardEvent<HTMLElement>): void => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
    const t = e.target as HTMLElement
    if (t.isContentEditable || t.closest('input, textarea, select, [contenteditable="true"]')) return
    switch (e.key) {
      case 'n':
        jump('file', 1)
        break
      case 'p':
        jump('file', -1)
        break
      case 'j':
        jump('hunk', 1)
        break
      case 'k':
        jump('hunk', -1)
        break
      case '/':
        e.preventDefault()
        findRef.current?.focus()
        break
      default:
        return
    }
  }

  const expandGap = (fi: number, g: number): void => {
    if (!getFile) return
    const key = `${fi}:${g}`
    const f = files[fi]!
    const state = context.get(fi)
    setOpened((prev) => (prev.has(key) ? prev : new Set(prev).add(key)))
    if (state?.status === 'ready' || state?.status === 'loading') return
    const path = f.newPath
    if (path === null) return
    setContext((prev) => new Map(prev).set(fi, { status: 'loading' }))
    Promise.resolve()
      .then(() => getFile(path, 'new'))
      .then(
        (body) => {
          if (typeof body !== 'string') throw new Error('getFile returned no text')
          const lines = splitFile(body)
          const next: ContextState = contextAgrees(f, lines) ? { status: 'ready', lines } : { status: 'mismatch' }
          if (alive.current) setContext((prev) => new Map(prev).set(fi, next))
        },
      )
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err)
        if (alive.current) setContext((prev) => new Map(prev).set(fi, { status: 'failed', message }))
      })
  }

  // The window.
  const start = Math.max(0, rowAt(offsets, scrollTop - OVERSCAN_PX))
  const end = Math.min(rows.length, rowAt(offsets, scrollTop + viewport + OVERSCAN_PX) + 1)
  const drawn: ReactNode[] = []
  for (let i = start; i < end; i++) {
    drawn.push(
      <RowView
        key={rowKey(rows[i]!)}
        row={rows[i]!}
        files={files}
        collapsed={collapsed}
        context={context}
        canExpand={getFile !== undefined}
        byLine={byLine}
        current={current}
        onToggle={toggleFile}
        onExpandGap={expandGap}
      />,
    )
  }

  const count = query === '' ? '' : matches.length === 0 ? 'no matches' : `${matchIdx + 1} of ${matches.length}`

  return (
    <section className="diff" aria-label="Diff" ref={rootRef} onKeyDown={onKey}>
      <div className="diff-bar">
        <input
          ref={findRef}
          type="search"
          className="diff-find"
          aria-label="Find in diff"
          aria-keyshortcuts="/"
          placeholder="find"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              step(e.shiftKey ? -1 : 1)
            }
          }}
        />
        <span className="diff-count" data-testid="diff-count" aria-live="polite">
          {count}
        </span>
        <button type="button" className="diff-btn" aria-label="Previous match" disabled={matches.length === 0} onClick={() => step(-1)}>
          ↑
        </button>
        <button type="button" className="diff-btn" aria-label="Next match" disabled={matches.length === 0} onClick={() => step(1)}>
          ↓
        </button>
        <div className="diff-seg" role="group" aria-label="Layout">
          <button type="button" className="diff-btn" aria-pressed={mode === 'unified'} onClick={() => choose('unified')}>
            Unified
          </button>
          <button type="button" className="diff-btn" aria-pressed={mode === 'split'} disabled={narrow} onClick={() => choose('split')}>
            Split
          </button>
        </div>
        {narrow ? <span className="diff-note">split needs 700px</span> : null}
        <button type="button" className="diff-btn" onClick={toggleAll}>
          {allCollapsed ? 'Expand all' : 'Collapse all'}
        </button>
      </div>

      <nav className="diff-files" aria-label="Files in this diff">
        <ol>
          {files.map((f, fi) => (
            <li key={fi}>
              <button type="button" onClick={() => goFile(fi)}>
                <span className="diff-path">{f.path}</span>
                <Counts file={f} />
              </button>
            </li>
          ))}
        </ol>
      </nav>

      <div
        ref={scrollRef}
        className="diff-scroll"
        role="region"
        aria-label="Diff lines"
        aria-keyshortcuts="n p j k /"
        tabIndex={0}
        data-total-rows={rows.length}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        style={{ ['--diff-ch' as string]: String(widest) } as CSSProperties}
      >
        <div className="diff-rows" data-mode={mode}>
          <div aria-hidden="true" style={{ height: offsets[start] ?? 0 }} />
          {drawn}
          <div aria-hidden="true" style={{ height: total - (offsets[end] ?? total) }} />
        </div>
      </div>
    </section>
  )
}

function rowKey(r: Row): string {
  switch (r.t) {
    case 'file':
      return `f${r.file}`
    case 'note':
      return `n${r.file}`
    case 'hunk':
      return `h${r.file}:${r.hunk}`
    case 'gap':
      return `g${r.file}:${r.gap}`
    case 'line':
      return `l${r.file}:${r.hunk}:${r.line}`
    case 'pair':
      return `p${r.file}:${r.hunk}:${r.left ?? '-'}:${r.right ?? '-'}`
    case 'context':
      return `c${r.file}:${r.gap}:${r.newNo}`
  }
}

function Counts({ file }: { file: DiffFile }) {
  return (
    <span className="diff-counts">
      <span className="diff-plus">+{file.additions}</span> <span className="diff-minus">−{file.deletions}</span>
    </span>
  )
}

function badges(f: DiffFile): string[] {
  const out: string[] = []
  if (f.status === 'added') out.push('added')
  if (f.status === 'deleted') out.push('deleted')
  if (f.status === 'renamed') out.push(f.similarity === null ? 'renamed' : `renamed ${f.similarity}%`)
  if (f.status === 'copied') out.push(f.similarity === null ? 'copied' : `copied ${f.similarity}%`)
  if (f.binary) out.push('binary')
  if (f.oldMode !== null && f.newMode !== null && f.oldMode !== f.newMode) out.push(`mode ${f.oldMode} → ${f.newMode}`)
  return out
}

interface RowProps {
  row: Row
  files: DiffFile[]
  collapsed: ReadonlySet<number>
  context: ReadonlyMap<number, ContextState>
  canExpand: boolean
  byLine: ReadonlyMap<string, Match[]>
  current: Match | undefined
  onToggle: (file: number) => void
  onExpandGap: (file: number, gap: number) => void
}

function RowView({ row, files, collapsed, context, canExpand, byLine, current, onToggle, onExpandGap }: RowProps) {
  const f = files[row.file]!
  const common = { 'data-path': f.path, style: { height: rowHeight(row) } }

  switch (row.t) {
    case 'file': {
      const open = !collapsed.has(row.file)
      const moved = (f.status === 'renamed' || f.status === 'copied') && f.oldPath !== null && f.oldPath !== f.path
      return (
        <div className="diff-row is-file" data-diff-row="file" {...common}>
          <button
            type="button"
            className="diff-toggle"
            aria-expanded={open}
            aria-label={`${open ? 'Collapse' : 'Expand'} ${f.path}`}
            onClick={() => onToggle(row.file)}
          >
            <span aria-hidden="true">{open ? '▾' : '▸'}</span>
          </button>
          <span className="diff-path">
            {moved ? `${f.oldPath} → ` : ''}
            {f.path}
          </span>
          {badges(f).map((b) => (
            <span key={b} className="diff-badge">
              {b}
            </span>
          ))}
          <Counts file={f} />
        </div>
      )
    }
    case 'note':
      return (
        <div className="diff-row is-note" data-diff-row="note" {...common}>
          {row.text}
        </div>
      )
    case 'hunk':
      return (
        <div className="diff-row is-hunk" data-diff-row="hunk" {...common}>
          {f.hunks[row.hunk]!.header}
        </div>
      )
    case 'gap':
      return (
        <div className="diff-row is-gap" data-diff-row="gap" {...common}>
          <GapBody file={f} fi={row.file} gap={row.gap} hidden={row.hidden} state={context.get(row.file)} canExpand={canExpand} onExpand={onExpandGap} />
        </div>
      )
    case 'context':
      return (
        <div className="diff-row diff-line is-context is-expanded" data-diff-row="context" {...common}>
          <span className="diff-no">{row.oldNo}</span>
          <span className="diff-no">{row.newNo}</span>
          <span className="diff-sign"> </span>
          <span className="diff-text">
            <LineText text={row.text} marks={[]} current={undefined} noNewline={false} />
          </span>
        </div>
      )
    case 'line': {
      const l = f.hunks[row.hunk]!.lines[row.line]!
      const marks = byLine.get(`${row.file}:${row.hunk}:${row.line}`) ?? []
      return (
        <div className={`diff-row diff-line is-${l.kind}`} data-diff-row="line" {...common}>
          <span className="diff-no">{l.oldNo ?? ''}</span>
          <span className="diff-no">{l.newNo ?? ''}</span>
          <span className="diff-sign">{sign(l)}</span>
          <span className="diff-text">
            <LineText text={l.text} marks={marks} current={current} noNewline={l.noNewlineAtEnd} />
          </span>
        </div>
      )
    }
    case 'pair': {
      const lines = f.hunks[row.hunk]!.lines
      const half = (idx: number | null, side: 'old' | 'new'): ReactNode => {
        const l = idx === null ? undefined : lines[idx]
        if (!l || idx === null) {
          return (
            <div className="diff-half is-empty">
              <span className="diff-no" />
              <span className="diff-sign" />
              <span className="diff-text" />
            </div>
          )
        }
        const marks = byLine.get(`${row.file}:${row.hunk}:${idx}`) ?? []
        return (
          <div className={`diff-half is-${l.kind}`}>
            <span className="diff-no">{(side === 'old' ? l.oldNo : l.newNo) ?? ''}</span>
            <span className="diff-sign">{sign(l)}</span>
            <span className="diff-text">
              <LineText text={l.text} marks={marks} current={current} noNewline={l.noNewlineAtEnd} />
            </span>
          </div>
        )
      }
      return (
        <div className="diff-row diff-pair" data-diff-row="pair" {...common}>
          {half(row.left, 'old')}
          {half(row.right, 'new')}
        </div>
      )
    }
  }
}

function sign(l: DiffLine): string {
  return l.kind === 'add' ? '+' : l.kind === 'del' ? '-' : ' '
}

function GapBody({
  file,
  fi,
  gap,
  hidden,
  state,
  canExpand,
  onExpand,
}: {
  file: DiffFile
  fi: number
  gap: number
  hidden: number | null
  state: ContextState | undefined
  canExpand: boolean
  onExpand: (file: number, gap: number) => void
}) {
  const what = hidden === null ? 'unchanged lines to the end of the file' : `${hidden} unchanged ${hidden === 1 ? 'line' : 'lines'}`
  if (!canExpand) return <span>{what} · context not available</span>
  if (state?.status === 'loading') return <span>reading context</span>
  if (state?.status === 'mismatch') return <span>context not shown: the file does not match this patch</span>
  const label =
    hidden === null
      ? `Expand the lines after the last hunk in ${file.path}`
      : `Expand ${hidden} hidden ${hidden === 1 ? 'line' : 'lines'} in ${file.path}`
  return (
    <>
      {state?.status === 'failed' ? <span>context not read: {state.message}</span> : null}
      <button type="button" className="diff-btn" aria-label={label} onClick={() => onExpand(fi, gap)}>
        {state?.status === 'failed' ? 'Retry' : hidden === null ? 'Expand to end of file' : `Expand ${what}`}
      </button>
    </>
  )
}

/**
 * The line's text as React text nodes, with find matches in `<mark>` and a
 * content CR drawn as a visible `␍` rather than as nothing.
 */
function LineText({
  text,
  marks,
  current,
  noNewline,
}: {
  text: string
  marks: readonly Match[]
  current: Match | undefined
  noNewline: boolean
}) {
  const cr = text.endsWith('\r')
  const body = cr ? text.slice(0, -1) : text
  const parts: ReactNode[] = []
  let at = 0
  for (const m of marks) {
    if (m.start < at || m.start >= body.length) continue
    const stop = Math.min(body.length, m.start + m.length)
    if (m.start > at) parts.push(body.slice(at, m.start))
    const isCurrent = current === m
    parts.push(
      <mark key={m.start} className={isCurrent ? 'is-current' : undefined}>
        {body.slice(m.start, stop)}
      </mark>,
    )
    at = stop
  }
  if (at < body.length) parts.push(body.slice(at))
  return (
    <>
      {parts}
      {cr ? (
        <span className="diff-cr" role="img" aria-label="carriage return">
          ␍
        </span>
      ) : null}
      {noNewline ? <span className="diff-nonl">no newline at end of file</span> : null}
    </>
  )
}
