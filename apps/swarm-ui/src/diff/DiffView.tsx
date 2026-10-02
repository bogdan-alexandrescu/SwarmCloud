// <DiffView patch={string} getFile? name? size? attempt? onBack? /> -- THE DIFF
// VIEWER (#310), laid out as a file list and one file (owner decision
// 2026-10-01).
//
// What it draws: a bar (`‹ Artifacts`, the patch's name, size and attempt,
// Unified/Split, Copy patch, Download); a FILE LIST COLUMN on the left,
// grouped by directory in patch order, each row a change letter, the file
// name, +N -N and a five-cell bar, under an `N files +A -D` header and a path
// filter; and on the right ONLY THE SELECTED FILE: its badges (binary,
// renamed, copied, mode change, added, deleted), hunk headers and both
// line-number gutters -- unified, or side by side from 700px of pane width.
// Under 700px the list becomes a picker at the top (`file 2 of 4`, the name,
// the counts, previous and next) and the file is always unified.
//
// THERE IS NO WHOLE-PATCH READING MODE. A patch is read a file at a time; the
// whole of it is what Copy patch and Download are for.
//
// VIEWED IS MEMORY, NOT STORAGE. A file is viewed once it has been opened, and
// the dots live in this component's state: a reload or a remount clears them.
// Nothing about which file is open reaches the address either -- the URL names
// the patch and the viewer always opens at the first file.
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
//   3. A BIG FILE COSTS WHAT IS ON SCREEN. Rows are fixed-height and drawn
//      only inside the scroller's window (rows.ts), so 50,000 lines is two
//      spacers and a hundred-odd rows.
//
// FIND IS ACROSS THE WHOLE PATCH (find.ts). The box above the lines searches
// every file, counts `n of N`, and next / previous switch the open file when
// the match lives in another one; the matches in the drawn rows are marked.
// Marking does not change the text: a mark wraps characters the patch already
// holds, still as React text nodes.
//
// Keys, while focus is anywhere inside the viewer except a text field:
// n / p next and previous file, j / k next and previous hunk, / find in the
// diff. In the find box, Enter and Shift+Enter are the next and previous
// match and Escape clears it. Each one handled calls preventDefault: the Sky
// shell's global N opens Submit otherwise (Spine.tsx). Escape that clears a
// non-empty find or path filter also stops there, because the agent drawer
// this view sits in closes on any Escape that reaches it.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

import { findMatches, firstMatchFrom, hitSegments, hitsInFile, type FindMatch, type LineHit } from './find'
import { parseUnifiedDiff, type DiffFile, type DiffLine } from './parse'
import {
  barCells,
  baseOf,
  buildRows,
  changeLetter,
  contextAgrees,
  groupByDirectory,
  listOrder,
  rowAt,
  rowHeight,
  rowOffsets,
  ROW_H,
  splitFile,
  widestLine,
  type ContextState,
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
  /** The patch's name, for the bar and the download. */
  name?: string
  /** The patch's size, already in words (`bytesLabel`). */
  size?: string
  /** The attempt that wrote the patch. */
  attempt?: string | null
  /** `‹ Artifacts`: back to the list the patch was opened from. */
  onBack?: () => void
  /**
   * The back button's words, where the patch was not opened from the
   * Artifacts list (the Code section's `show diff`, a checkpoint member):
   * `‹ Artifacts` there names a list that is not on screen.
   */
  backLabel?: string
  /**
   * What Copy hands out and what its button says. By default the patch as
   * drawn, `Copy patch`; a caller holding only a WINDOW of a patch says
   * `Copy window` and hands out the window, so nobody pastes a part of a
   * patch believing it is the whole.
   */
  copy?: { label: string; text: string }
  /**
   * Where Download gets its bytes. By default the patch as drawn, saved from
   * memory. `href`: a link to the stored object (the artifact raw route),
   * for a caller that drew only part of it. `refused`: there is no whole
   * object this viewer can hand out, and the sentence says why.
   */
  download?: { href: string } | { refused: string }
  /** Drawn instead of `no files in this patch` when the patch has no files. */
  empty?: ReactNode
  /** What the caller must say beside the name -- that the window is partial, say. */
  note?: ReactNode
  /** Drawn between the bar and the files: the caller's provenance strip. */
  meta?: ReactNode
}

/** Below this pane width the viewer is unified and the list is a picker: two columns of code do not fit. */
export const SPLIT_MIN_WIDTH = 700

/**
 * jsdom, and a scroller not yet laid out, report a height of 0. The window is
 * then sized as if the scroller were this tall, so the first paint is never
 * empty.
 */
const FALLBACK_VIEWPORT = 800
/** Rows drawn beyond each edge of the window, in px, so a fast scroll does not show blank space. */
const OVERSCAN_PX = 1200

const STATUS_WORD: Readonly<Record<DiffFile['status'], string>> = {
  modified: 'modified',
  added: 'added',
  deleted: 'deleted',
  renamed: 'renamed',
  copied: 'copied',
}

export function DiffView(props: DiffViewProps) {
  const { patch } = props
  const parsed = useMemo(() => parseUnifiedDiff(patch), [patch])
  if (!parsed.ok) {
    return (
      <section className="diff" aria-label="Diff">
        <Bar {...props} />
        {props.meta}
        <p className="diff-error" role="alert">
          patch not read: {parsed.error.message} · line {parsed.error.line}
        </p>
      </section>
    )
  }
  if (parsed.files.length === 0) {
    return (
      <section className="diff" aria-label="Diff">
        <Bar {...props} />
        {props.meta}
        {props.empty ?? <p className="diff-empty">no files in this patch</p>}
      </section>
    )
  }
  // Keyed by the patch, so a new patch starts at its first file with no
  // viewed dots, no loaded context and no filter that indexes into the old one.
  return <Viewer key={patch} files={parsed.files} props={props} />
}

/**
 * The bar. Its identity half is the caller's (what the patch is); its action
 * half is the patch's (copy it, save it). `layout` is the Unified/Split toggle,
 * which only a readable patch has.
 */
function Bar({
  patch,
  name,
  size,
  attempt,
  onBack,
  backLabel,
  note,
  copy: copyWhat,
  download: downloadFrom,
  layout,
}: DiffViewProps & { layout?: ReactNode }) {
  const [copy, setCopy] = useState<'idle' | 'done' | 'failed'>('idle')
  const copyText = copyWhat?.text ?? patch
  const copyLabel = copyWhat?.label ?? 'Copy patch'
  const fileName = (name ?? 'change.diff').replace(/\//g, '-')
  const copyPatch = (): void => {
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (!clip) {
      setCopy('failed')
      return
    }
    clip.writeText(copyText).then(
      () => setCopy('done'),
      () => setCopy('failed'),
    )
  }
  const download = (): void => {
    const url = URL.createObjectURL(new Blob([patch], { type: 'text/x-diff;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    a.click()
    // Revoked on the next task, not now: Safari cancels a download whose
    // object URL is revoked in the same task as the click.
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
  return (
    <div className="diff-bar">
      {onBack ? (
        <button type="button" className="diff-back" onClick={onBack}>
          {backLabel ?? '‹ Artifacts'}
        </button>
      ) : null}
      {name !== undefined ? <span className="diff-name-main">{name}</span> : null}
      {size !== undefined ? <span className="diff-meta">{size}</span> : null}
      {attempt ? <span className="diff-meta">attempt {attempt}</span> : null}
      {note}
      <span className="diff-bar-end">
        {layout}
        <button type="button" className="diff-btn" onClick={copyPatch} aria-live="polite">
          {copy === 'done' ? 'Copied' : copy === 'failed' ? 'Copy failed' : copyLabel}
        </button>
        {downloadFrom === undefined ? (
          <button type="button" className="diff-btn" onClick={download}>
            Download
          </button>
        ) : 'href' in downloadFrom ? (
          <a className="diff-btn" href={downloadFrom.href} download={fileName}>
            Download
          </a>
        ) : (
          <button type="button" className="diff-btn" disabled title={downloadFrom.refused}>
            Download
          </button>
        )}
      </span>
    </div>
  )
}

function Viewer({ files, props }: { files: DiffFile[]; props: DiffViewProps }) {
  const { getFile } = props
  const rootRef = useRef<HTMLElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const filterRef = useRef<HTMLInputElement>(null)
  const findRef = useRef<HTMLInputElement>(null)
  const alive = useRef(true)

  const [preferred, setPreferred] = useState<ViewMode>(rememberedViewMode)
  const [width, setWidth] = useState<number>(() => (typeof window === 'undefined' ? 1024 : window.innerWidth))
  const [viewport, setViewport] = useState(FALLBACK_VIEWPORT)
  const [scrollTop, setScrollTop] = useState(0)
  const [context, setContext] = useState<ReadonlyMap<number, ContextState>>(() => new Map())
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set())
  const [filter, setFilter] = useState('')
  // The first file of the patch is the first row of the list (rows.ts
  // `groupByDirectory`), so opening at 0 is opening at the top of the list.
  const [selected, setSelected] = useState(0)
  const [viewed, setViewed] = useState<ReadonlySet<number>>(() => new Set([0]))
  // Find: the query, its matches across the whole patch, which one is current,
  // and the match still to be scrolled to once its file's rows are built.
  const [found, setFound] = useState<{ query: string; matches: FindMatch[] }>({ query: '', matches: [] })
  const [current, setCurrent] = useState(-1)
  const [reveal, setReveal] = useState<FindMatch | null>(null)

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
  // The width is the PANE's: the detail pane is narrower than the window.
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

  const everyGroup = useMemo(() => groupByDirectory(files), [files])
  const groups = useMemo(() => (filter === '' ? everyGroup : groupByDirectory(files, filter)), [everyGroup, files, filter])
  const everyFile = useMemo(() => listOrder(everyGroup), [everyGroup])
  // n and p walk the list as drawn: the filtered list beside a wide pane, the
  // whole patch in the phone picker, which has no filter.
  const order = useMemo(() => (narrow ? everyFile : listOrder(groups)), [narrow, everyFile, groups])

  const totals = useMemo(() => {
    let add = 0
    let del = 0
    for (const f of files) {
      add += f.additions
      del += f.deletions
    }
    return { add, del }
  }, [files])

  const rows = useMemo(
    () => buildRows({ files, file: selected, mode, canExpand: getFile !== undefined, context, opened }),
    [files, selected, mode, getFile, context, opened],
  )
  const offsets = useMemo(() => rowOffsets(rows), [rows])
  const total = offsets[rows.length] ?? 0

  const widest = useMemo(() => {
    let w = widestLine(files[selected]!)
    const c = context.get(selected)
    if (c?.status === 'ready') for (const l of c.lines) if (l.length > w) w = l.length
    return w
  }, [files, selected, context])

  const scrollTo = useCallback((y: number) => {
    const top = Math.max(0, y)
    if (scrollRef.current) scrollRef.current.scrollTop = top
    setScrollTop(top)
  }, [])

  const open = (fi: number): void => {
    setSelected(fi)
    setViewed((prev) => (prev.has(fi) ? prev : new Set(prev).add(fi)))
    scrollTo(0)
  }

  const stepFile = (dir: 1 | -1): void => {
    if (order.length === 0) return
    const at = order.indexOf(selected)
    // The open file may be filtered out of the list; the next one is then the
    // list's first, and the previous its last.
    const next = at === -1 ? order[dir === 1 ? 0 : order.length - 1] : order[at + dir]
    if (next !== undefined && next !== selected) open(next)
  }

  const jumpHunk = (dir: 1 | -1): void => {
    const y = scrollTop
    if (dir === 1) {
      for (let i = 0; i < rows.length; i++) {
        if (rows[i]!.t === 'hunk' && offsets[i]! > y + 0.5) return scrollTo(offsets[i]!)
      }
    } else {
      for (let i = rows.length - 1; i >= 0; i--) {
        if (rows[i]!.t === 'hunk' && offsets[i]! < y - 0.5) return scrollTo(offsets[i]!)
      }
    }
  }

  const goToMatch = (matches: readonly FindMatch[], i: number): void => {
    const m = matches[i]
    setCurrent(m === undefined ? -1 : i)
    if (m === undefined) return
    if (m.file !== selected) open(m.file)
    setReveal(m)
  }

  const search = (query: string): void => {
    const matches = findMatches(files, everyFile, query)
    setFound({ query, matches })
    goToMatch(matches, firstMatchFrom(matches, everyFile, selected))
  }

  const stepMatch = (dir: 1 | -1): void => {
    const n = found.matches.length
    if (n === 0) return
    goToMatch(found.matches, current === -1 ? 0 : (current + dir + n) % n)
  }

  // The current match's row, once the rows of its file exist: scrolled into
  // the window if it is not already in it, a third of the way down.
  useEffect(() => {
    if (reveal === null || reveal.file !== selected) return
    const i = rows.findIndex((r) =>
      r.t === 'line'
        ? r.hunk === reveal.hunk && r.line === reveal.line
        : r.t === 'pair' && r.hunk === reveal.hunk && (r.left === reveal.line || r.right === reveal.line),
    )
    setReveal(null)
    if (i === -1) return
    const top = offsets[i]!
    const view = scrollRef.current?.clientHeight || viewport
    if (top < scrollTop || top + ROW_H > scrollTop + view) scrollTo(top - Math.floor(view / 3))
  }, [reveal, selected, rows, offsets, scrollTop, viewport, scrollTo])

  const hits = useMemo(() => hitsInFile(found.matches, selected), [found.matches, selected])

  const choose = (m: ViewMode): void => {
    setPreferred(m)
    rememberViewMode(m)
  }

  const onKey = (e: KeyboardEvent<HTMLElement>): void => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
    const t = e.target as HTMLElement
    if (t.isContentEditable || t.closest('input, textarea, select, [contenteditable="true"]')) return
    // Each handled key is consumed (`preventDefault`), so the shell's global
    // `n` (Spine.tsx: N opens Submit) does not also navigate away from the diff.
    switch (e.key) {
      case 'n':
        e.preventDefault()
        stepFile(1)
        break
      case 'p':
        e.preventDefault()
        stepFile(-1)
        break
      case 'j':
        e.preventDefault()
        jumpHunk(1)
        break
      case 'k':
        e.preventDefault()
        jumpHunk(-1)
        break
      case '/':
        if (!findRef.current) return
        e.preventDefault()
        findRef.current.focus()
        findRef.current.select()
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
      .then((body) => {
        if (typeof body !== 'string') throw new Error('getFile returned no text')
        const lines = splitFile(body)
        const next: ContextState = contextAgrees(f, lines) ? { status: 'ready', lines } : { status: 'mismatch' }
        if (alive.current) setContext((prev) => new Map(prev).set(fi, next))
      })
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
        context={context}
        canExpand={getFile !== undefined}
        onExpandGap={expandGap}
        hits={hits}
        current={current}
      />,
    )
  }

  // Split is OFFERED only where it fits: under 700px there is no toggle at all.
  const layout = narrow ? null : (
    <span className="diff-seg" role="group" aria-label="Layout">
      <button type="button" className="diff-btn" aria-pressed={mode === 'unified'} onClick={() => choose('unified')}>
        Unified
      </button>
      <button type="button" className="diff-btn" aria-pressed={mode === 'split'} onClick={() => choose('split')}>
        Split
      </button>
    </span>
  )

  const shownCount = listOrder(groups).length

  return (
    <section className={`diff${narrow ? ' is-narrow' : ''}`} aria-label="Diff" ref={rootRef} onKeyDown={onKey}>
      <Bar {...props} layout={layout} />
      {props.meta}
      <div className="diff-body">
        {narrow ? (
          <Picker files={files} order={everyFile} selected={selected} onOpen={open} />
        ) : (
          <nav className="diff-files" aria-label="Files in this diff">
            <p className="diff-files-head" data-testid="diff-files-head">
              {files.length} {files.length === 1 ? 'file' : 'files'} <span className="diff-plus">+{totals.add}</span>{' '}
              <span className="diff-minus">−{totals.del}</span>
            </p>
            <input
              ref={filterRef}
              type="search"
              className="diff-filter"
              aria-label="Filter files by path"
              placeholder="filter paths"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  const first = listOrder(groups)[0]
                  if (first !== undefined) open(first)
                } else if (e.key === 'Escape' && filter !== '') {
                  // Clearing a non-empty filter consumes Escape: the agent
                  // drawer around this view closes on any Escape that reaches
                  // it. An empty box lets Escape through so the drawer still closes.
                  e.preventDefault()
                  e.stopPropagation()
                  setFilter('')
                }
              }}
            />
            {filter !== '' ? (
              <p className="diff-files-note" aria-live="polite">
                {shownCount === 0 ? 'no path matches' : `${shownCount} of ${files.length} shown`}
              </p>
            ) : null}
            {groups.map((g) => (
              <div key={g.dir} className="diff-dir" role="group" aria-label={g.dir === '' ? 'repository root' : g.dir}>
                <p className="diff-dir-name">{g.dir === '' ? '/' : `${g.dir}/`}</p>
                <ol>
                  {g.files.map((fi) => (
                    <li key={fi}>
                      <FileRow file={files[fi]!} on={fi === selected} viewed={viewed.has(fi)} onOpen={() => open(fi)} />
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </nav>
        )}

        <div className="diff-main">
          <div className="diff-find" role="search" aria-label="Find">
            <input
              ref={findRef}
              type="search"
              className="diff-filter diff-find-box"
              aria-label="Find in diff"
              aria-keyshortcuts="/"
              placeholder="find in diff  /"
              value={found.query}
              onChange={(e) => search(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  stepMatch(e.shiftKey ? -1 : 1)
                } else if (e.key === 'Escape' && found.query !== '') {
                  // As the path filter: clearing consumes Escape, an empty box passes it on.
                  e.preventDefault()
                  e.stopPropagation()
                  search('')
                }
              }}
            />
            <span className="diff-find-count" data-testid="diff-find-count" aria-live="polite">
              {found.query === '' ? '' : found.matches.length === 0 ? 'no matches' : `${current + 1} of ${found.matches.length}`}
            </span>
            <button
              type="button"
              className="diff-btn"
              aria-label="Previous match"
              disabled={found.matches.length === 0}
              onClick={() => stepMatch(-1)}
            >
              ‹
            </button>
            <button
              type="button"
              className="diff-btn"
              aria-label="Next match"
              disabled={found.matches.length === 0}
              onClick={() => stepMatch(1)}
            >
              ›
            </button>
          </div>
          <div
            ref={scrollRef}
            className="diff-scroll"
            role="region"
            aria-label="Diff lines"
            aria-keyshortcuts="n p j k /"
            tabIndex={0}
            data-total-rows={rows.length}
            data-open-file={files[selected]!.path}
            onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
            style={{ ['--diff-ch' as string]: String(widest) } as CSSProperties}
          >
            <div className="diff-rows" data-mode={mode}>
              <div aria-hidden="true" style={{ height: offsets[start] ?? 0 }} />
              {drawn}
              <div aria-hidden="true" style={{ height: total - (offsets[end] ?? total) }} />
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

/** One row of the file list: change letter, name, +N -N, the five-cell bar, the viewed dot. */
function FileRow({ file, on, viewed, onOpen }: { file: DiffFile; on: boolean; viewed: boolean; onOpen: () => void }) {
  const letter = changeLetter(file)
  const moved = (file.status === 'renamed' || file.status === 'copied') && file.oldPath !== null && file.oldPath !== file.path
  return (
    <button
      type="button"
      className={`diff-file${on ? ' is-on' : ''}`}
      aria-current={on ? 'true' : undefined}
      aria-label={`${file.path}, ${STATUS_WORD[file.status]}, ${file.additions} added, ${file.deletions} removed${viewed ? ', viewed' : ''}`}
      title={moved ? `${file.oldPath} → ${file.path}` : file.path}
      data-path={file.path}
      data-viewed={viewed ? 'true' : 'false'}
      onClick={onOpen}
    >
      <span className={`diff-kind is-${letter}`} aria-hidden="true">
        {letter}
      </span>
      <span className="diff-fname">{baseOf(file.path)}</span>
      <span className="diff-plus">+{file.additions}</span>
      <span className="diff-minus">−{file.deletions}</span>
      <StatBar file={file} />
      <span className={`diff-viewed${viewed ? ' is-viewed' : ''}`} aria-hidden="true" />
    </button>
  )
}

function StatBar({ file }: { file: DiffFile }) {
  return (
    <span className="diff-stat" aria-hidden="true">
      {barCells(file.additions, file.deletions).map((c, i) => (
        <span key={i} className={`diff-cell is-${c}`} />
      ))}
    </span>
  )
}

/** The phone's file list: where you are, the open file, and previous / next. */
function Picker({
  files,
  order,
  selected,
  onOpen,
}: {
  files: DiffFile[]
  order: number[]
  selected: number
  onOpen: (fi: number) => void
}) {
  const at = order.indexOf(selected)
  const f = files[selected]!
  const prev = order[at - 1]
  const next = order[at + 1]
  return (
    <div className="diff-picker" role="group" aria-label="File">
      <button
        type="button"
        className="diff-btn"
        aria-label="Previous file"
        disabled={prev === undefined}
        onClick={() => prev !== undefined && onOpen(prev)}
      >
        ‹
      </button>
      <span className="diff-picker-at">
        <span className="diff-picker-pos" data-testid="diff-picker-pos">
          file {at + 1} of {order.length}
        </span>
        <span className="diff-fname">{f.path}</span>
        <span className="diff-plus">+{f.additions}</span> <span className="diff-minus">−{f.deletions}</span>
      </span>
      <button
        type="button"
        className="diff-btn"
        aria-label="Next file"
        disabled={next === undefined}
        onClick={() => next !== undefined && onOpen(next)}
      >
        ›
      </button>
    </div>
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
  context: ReadonlyMap<number, ContextState>
  canExpand: boolean
  onExpandGap: (file: number, gap: number) => void
  /** The open file's find matches, keyed `${hunk}:${line}`. */
  hits: ReadonlyMap<string, LineHit[]>
  /** The current match's index in the whole patch's matches, -1 for none. */
  current: number
}

function RowView({ row, files, context, canExpand, onExpandGap, hits, current }: RowProps) {
  const f = files[row.file]!
  const common = { 'data-path': f.path, style: { height: rowHeight(row) } }

  switch (row.t) {
    case 'file': {
      const moved = (f.status === 'renamed' || f.status === 'copied') && f.oldPath !== null && f.oldPath !== f.path
      return (
        <div className="diff-row is-file" data-diff-row="file" {...common}>
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
            <LineText text={row.text} noNewline={false} />
          </span>
        </div>
      )
    case 'line': {
      const l = f.hunks[row.hunk]!.lines[row.line]!
      return (
        <div className={`diff-row diff-line is-${l.kind}`} data-diff-row="line" {...common}>
          <span className="diff-no">{l.oldNo ?? ''}</span>
          <span className="diff-no">{l.newNo ?? ''}</span>
          <span className="diff-sign">{sign(l)}</span>
          <span className="diff-text">
            <LineText text={l.text} noNewline={l.noNewlineAtEnd} hits={hits.get(`${row.hunk}:${row.line}`)} current={current} />
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
        return (
          <div className={`diff-half is-${l.kind}`}>
            <span className="diff-no">{(side === 'old' ? l.oldNo : l.newNo) ?? ''}</span>
            <span className="diff-sign">{sign(l)}</span>
            <span className="diff-text">
              <LineText text={l.text} noNewline={l.noNewlineAtEnd} hits={hits.get(`${row.hunk}:${idx}`)} current={current} />
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
 * The line's text as React text nodes, with a content CR drawn as a visible
 * `␍` rather than as nothing, and each find match wrapped in a `<mark>` -- the
 * current one `is-current` -- around the characters already there.
 */
function LineText({
  text,
  noNewline,
  hits,
  current = -1,
}: {
  text: string
  noNewline: boolean
  hits?: readonly LineHit[]
  current?: number
}) {
  const cr = text.endsWith('\r')
  const body = cr ? text.slice(0, -1) : text
  return (
    <>
      {hits === undefined
        ? body
        : hitSegments(body, hits).map((p, i) =>
            p.index === null ? (
              p.text
            ) : (
              <mark key={i} className={`diff-hit${p.index === current ? ' is-current' : ''}`} data-match={p.index + 1}>
                {p.text}
              </mark>
            ),
          )}
      {cr ? (
        <span className="diff-cr" role="img" aria-label="carriage return">
          ␍
        </span>
      ) : null}
      {noNewline ? <span className="diff-nonl">no newline at end of file</span> : null}
    </>
  )
}
