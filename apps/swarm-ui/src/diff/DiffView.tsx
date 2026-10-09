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
// Under 700px of pane width, and always at the console's phone width
// (`phoneWidth`, 560px), the list becomes a picker at the top (`file 2 of 4`,
// the name, the counts, previous and next, and an `All files` sheet listing
// every file with its counts) and the file is always unified (owner decision
// 2026-10-08, design §3 question 4: one file at a time on a phone).
//
// WHAT A ROUTE GIVES IT (design docs/design/diff-viewer.md §5, lane DIFF1):
// `fullHeight` fills the route's pane instead of capping the list and the
// lines at 70vh; `initialFile` opens a named file and `onFileChange` reports
// each file the reader opens, so the route can put it in the address;
// `pullRequest` puts a link to the PR in the bar; `stepOf` names the workflow
// step each hunk came from, drawn as a row above that step's hunks.
//
// THERE IS NO WHOLE-PATCH READING MODE. A patch is read a file at a time; the
// whole of it is what Copy patch and Download are for.
//
// VIEWED IS MEMORY, NOT STORAGE. A file is viewed once it has been opened, and
// the dots live in this component's state: a reload or a remount clears them.
// The viewer never writes the address itself. A route that wants the open
// file in its URL passes `initialFile` and listens to `onFileChange`; without
// them the viewer opens at the first file, as it always did.
//
// THREE RULES IT KEEPS, each the reason something below looks the way it does.
//
//   1. TEXT EXACTLY AS GIVEN. Every character of the patch reaches the DOM as
//      a React text node: no `dangerouslySetInnerHTML`, no trimming. The
//      syntax colour (highlight.ts, loaded lazily) is TOKENS, not markup: a
//      line is cut into pieces that concatenate back to it, each drawn as a
//      text node inside a `<span>`, so colour cannot change a character. A carriage return that belongs to the content and a missing
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
// EACH FILE COLLAPSES (owner decision 2026-10-05). The open file's header
// row is a button with aria-expanded that toggles its body; a file of more
// than LARGE_FILE_LINES changed lines (rows.ts) opens collapsed and its header
// says `collapsed: large`, so the reader knows the body was hidden for its
// size and not lost. Collapse all / Expand all in the bar set every file at
// once. The state is per file and lives in this component, like viewed: a
// remount starts again from the size rule. Collapsing does not change the
// windowing -- an expanded 50,000-line file is still two spacers and a window.
// Find, j and k REACH INTO a collapsed file: a match or a hunk inside it
// expands it first, so neither stops working because a body is hidden. n and
// p move between files as before and leave each file as the reader left it.
//
// FIND IS ACROSS THE WHOLE PATCH (find.ts). The box above the lines searches
// every file, counts `n of N`, and next / previous switch the open file when
// the match lives in another one; the matches in the drawn rows are marked.
// Marking does not change the text: a mark wraps characters the patch already
// holds, still as React text nodes.
//
// Keys, while focus is anywhere inside the viewer except a text field:
// n / p next and previous file, j / k next and previous hunk (in a collapsed
// file: expand it, then its first or last hunk), / find in the diff. In the find box, Enter and Shift+Enter are the next and previous
// match and Escape clears it. Each one handled calls preventDefault: the Sky
// shell's global N opens Submit otherwise (Spine.tsx). Escape that clears a
// non-empty find or path filter also stops there, because the agent drawer
// this view sits in closes on any Escape that reaches it.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
  startsCollapsed,
  widestLine,
  type ContextState,
  type Row,
} from './rows'
import { rememberedViewMode, rememberViewMode, type ViewMode } from './storage'
import type { Token } from './highlight'
import { HIGHLIGHT_MAX_LINE, highlightLanguage } from './lang'
import { useHighlighter } from './useHighlighter'
import { Button, ButtonLink } from '../components'
// The console's one definition of phone width (HelpCard `phoneWidth`, 560px),
// as a hook that follows the media query.
import { usePhoneTables as usePhoneWidth } from '../capacityPoll'
import './diff.css'

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
  /**
   * Fill the route's pane: the viewer takes its parent's height, and the file
   * list and the lines scroll inside it instead of each stopping at 70vh. The
   * parent must give it a height (a flex or grid child with `min-height: 0`).
   */
  fullHeight?: boolean
  /**
   * The file to open first, by path (the new path, or the old one of a
   * deletion or a rename). A path the patch does not hold opens the first
   * file. Changing it later opens the file it names, so a route's back button
   * moves the viewer; that does not call `onFileChange`.
   */
  initialFile?: string | null
  /** Called with the path of each file the READER opens (list, picker, sheet, n / p, find). */
  onFileChange?: (path: string) => void
  /**
   * The pull request this patch became, linked from the bar. Drawn only when
   * given, and only for an http(s) address: a `javascript:` URL from anywhere
   * upstream never becomes a link.
   */
  pullRequest?: { url: string; label?: string }
  /**
   * The workflow step a hunk came from (variant 5: a file's hunks from each
   * step, stacked and tagged). Return null for a hunk no step is named for.
   * A file several steps changed is ONE file section whose hunks are in step
   * order; the viewer draws a `from step` row above each step's run of hunks,
   * and offers no unchanged lines between two steps' hunks. Pass a stable
   * function (useCallback): a new one rebuilds the open file's rows.
   */
  stepOf?: (path: string, hunk: number) => string | null | undefined
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
  pullRequest,
  layout,
  fold,
}: DiffViewProps & { layout?: ReactNode; fold?: ReactNode }) {
  const prHref = pullRequest === undefined ? null : httpUrl(pullRequest.url)
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
      {prHref !== null ? (
        <a className="diff-pr" href={prHref} target="_blank" rel="noopener noreferrer">
          {pullRequest?.label ?? prLabel(prHref)} ↗
        </a>
      ) : null}
      <span className="diff-bar-end">
        {fold}
        {layout}
        <Button size="sm" onClick={copyPatch} aria-live="polite">
          {copy === 'done' ? 'Copied' : copy === 'failed' ? 'Copy failed' : copyLabel}
        </Button>
        {downloadFrom === undefined ? (
          <Button size="sm" onClick={download}>
            Download
          </Button>
        ) : 'href' in downloadFrom ? (
          <ButtonLink size="sm" href={downloadFrom.href} download={fileName}>
            Download
          </ButtonLink>
        ) : (
          <Button size="sm" disabled title={downloadFrom.refused}>
            Download
          </Button>
        )}
      </span>
    </div>
  )
}

/** The address as given when it is http(s), else null: nothing else becomes a link. */
export function httpUrl(url: string): string | null {
  try {
    const u = new URL(url)
    return u.protocol === 'https:' || u.protocol === 'http:' ? url : null
  } catch {
    return null
  }
}

/** `PR #123` for a GitHub pull request address, `Pull request` for any other. */
function prLabel(href: string): string {
  const m = /\/pull\/(\d+)(?:[/?#]|$)/.exec(href)
  return m ? `PR #${m[1]}` : 'Pull request'
}

/** The index of the file a path names: its shown path first, then its new or old path. -1 for none. */
export function fileIndex(files: readonly DiffFile[], path: string | null | undefined): number {
  if (path === null || path === undefined || path === '') return -1
  const exact = files.findIndex((f) => f.path === path)
  return exact !== -1 ? exact : files.findIndex((f) => f.newPath === path || f.oldPath === path)
}

function Viewer({ files, props }: { files: DiffFile[]; props: DiffViewProps }) {
  const { getFile, stepOf, onFileChange, initialFile, fullHeight } = props
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
  // `groupByDirectory`), so opening at 0 is opening at the top of the list --
  // unless the route named a file.
  const [first] = useState(() => Math.max(0, fileIndex(files, initialFile)))
  const [selected, setSelected] = useState(first)
  const [viewed, setViewed] = useState<ReadonlySet<number>>(() => new Set([first]))
  // The phone's `All files` sheet.
  const [sheet, setSheet] = useState(false)
  // Find: the query, its matches across the whole patch, which one is current,
  // and the match still to be scrolled to once its file's rows are built.
  const [found, setFound] = useState<{ query: string; matches: FindMatch[] }>({ query: '', matches: [] })
  const [current, setCurrent] = useState(-1)
  const [reveal, setReveal] = useState<FindMatch | null>(null)
  // Collapsed files: every large one to begin with. And the hunk j or k is
  // still to scroll to once the collapsed file it expanded has its rows.
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(
    () => new Set(files.flatMap((f, fi) => (startsCollapsed(f) ? [fi] : []))),
  )
  const [hunkReveal, setHunkReveal] = useState<{ file: number; hunk: number } | null>(null)

  const phone = usePhoneWidth()
  const narrow = width < SPLIT_MIN_WIDTH || phone
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

  const isCollapsed = collapsed.has(selected)
  const rows = useMemo(() => {
    const path = files[selected]!.path
    const sourceOf = stepOf === undefined ? undefined : (hunk: number) => stepOf(path, hunk)
    return buildRows({ files, file: selected, mode, canExpand: getFile !== undefined, context, opened, collapsed: isCollapsed, sourceOf })
  }, [files, selected, mode, getFile, context, opened, isCollapsed, stepOf])
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

  // Syntax colour for the open file: its lexer is fetched the first time a
  // file with a known language is shown (useHighlighter.ts), and until then,
  // or for an unknown language or a file over the size cap, lines are plain.
  const lang = highlightLanguage(files[selected]!)
  const highlighter = useHighlighter(lang !== null)
  const paint = useMemo(() => {
    if (highlighter === null || lang === null) return undefined
    // Rows are redrawn on every scroll; a line is cut once per file shown.
    const cache = new Map<string, Token[]>()
    return (text: string): Token[] | null => {
      if (text.length > HIGHLIGHT_MAX_LINE) return null
      let t = cache.get(text)
      if (t === undefined) {
        t = highlighter.tokenize(text, lang)
        cache.set(text, t)
      }
      return t
    }
  }, [highlighter, lang])

  const selectedRef = useRef(selected)
  selectedRef.current = selected

  const open = (fi: number, notify = true): void => {
    if (notify && fi !== selectedRef.current) onFileChange?.(files[fi]!.path)
    selectedRef.current = fi
    setSelected(fi)
    setViewed((prev) => (prev.has(fi) ? prev : new Set(prev).add(fi)))
    scrollTo(0)
  }

  // The route moved (its back button, a link to another file of this patch):
  // open the file it names. Keyed on the prop alone, so a route that does not
  // follow onFileChange never pulls the reader back to the file it named first.
  useEffect(() => {
    const fi = fileIndex(files, initialFile)
    if (fi !== -1 && fi !== selectedRef.current) open(fi, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFile])

  const stepFile = (dir: 1 | -1): void => {
    if (order.length === 0) return
    const at = order.indexOf(selected)
    // The open file may be filtered out of the list; the next one is then the
    // list's first, and the previous its last.
    const next = at === -1 ? order[dir === 1 ? 0 : order.length - 1] : order[at + dir]
    if (next !== undefined && next !== selected) open(next)
  }

  const expand = (fi: number): void => {
    setCollapsed((prev) => {
      if (!prev.has(fi)) return prev
      const next = new Set(prev)
      next.delete(fi)
      return next
    })
  }

  const toggle = (fi: number): void => {
    if (!collapsed.has(fi)) {
      setCollapsed((prev) => new Set(prev).add(fi))
      // The body is gone, so is everything below the header to scroll to.
      scrollTo(0)
    } else expand(fi)
  }

  const collapseAll = (): void => {
    setCollapsed(new Set(files.map((_, fi) => fi)))
    scrollTo(0)
  }

  const expandAll = (): void => setCollapsed(new Set())

  const jumpHunk = (dir: 1 | -1): void => {
    if (collapsed.has(selected)) {
      // A hunk inside a collapsed file is reached by expanding it: j lands on
      // its first hunk, k on its last, once the rows exist (effect below).
      const n = files[selected]!.hunks.length
      expand(selected)
      if (n > 0) setHunkReveal({ file: selected, hunk: dir === 1 ? 0 : n - 1 })
      return
    }
    const y = scrollTop
    // A hunk under a step's `from step` row is reached at that row, so the
    // step that wrote it is on screen with it.
    const top = (i: number): number => offsets[rows[i - 1]?.t === 'source' ? i - 1 : i]!
    if (dir === 1) {
      for (let i = 0; i < rows.length; i++) {
        if (rows[i]!.t === 'hunk' && top(i) > y + 0.5) return scrollTo(top(i))
      }
    } else {
      for (let i = rows.length - 1; i >= 0; i--) {
        if (rows[i]!.t === 'hunk' && top(i) < y - 0.5) return scrollTo(top(i))
      }
    }
  }

  const goToMatch = (matches: readonly FindMatch[], i: number): void => {
    const m = matches[i]
    setCurrent(m === undefined ? -1 : i)
    if (m === undefined) return
    if (m.file !== selected) open(m.file)
    // A match inside a collapsed file opens its body: a counter that says
    // `3 of 9` over a hidden line would point at nothing.
    expand(m.file)
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

  // The hunk j or k expanded a collapsed file to reach, scrolled to the top.
  useEffect(() => {
    if (hunkReveal === null || hunkReveal.file !== selected || isCollapsed) return
    const i = rows.findIndex((r) => r.t === 'hunk' && r.hunk === hunkReveal.hunk)
    setHunkReveal(null)
    if (i !== -1) scrollTo(offsets[rows[i - 1]?.t === 'source' ? i - 1 : i]!)
  }, [hunkReveal, selected, isCollapsed, rows, offsets, scrollTo])

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
        collapsed={isCollapsed}
        onToggle={toggle}
        paint={paint}
      />,
    )
  }

  // Split is OFFERED only where it fits: under 700px there is no toggle at all.
  const layout = narrow ? null : (
    <span className="diff-seg" role="group" aria-label="Layout">
      <Button size="sm" aria-pressed={mode === 'unified'} onClick={() => choose('unified')}>
        Unified
      </Button>
      <Button size="sm" aria-pressed={mode === 'split'} onClick={() => choose('split')}>
        Split
      </Button>
    </span>
  )

  // Offered at every width: the phone picker shows one file too, and a large
  // one starts collapsed there as well.
  const fold = (
    <span className="diff-seg" role="group" aria-label="Files">
      <Button size="sm" disabled={collapsed.size === files.length} onClick={collapseAll}>
        Collapse all
      </Button>
      <Button size="sm" disabled={collapsed.size === 0} onClick={expandAll}>
        Expand all
      </Button>
    </span>
  )

  const shownCount = listOrder(groups).length

  return (
    <section
      className={`diff${narrow ? ' is-narrow' : ''}${phone ? ' is-phone' : ''}${fullHeight ? ' is-full' : ''}`}
      aria-label="Diff"
      ref={rootRef}
      onKeyDown={onKey}
    >
      <Bar {...props} layout={layout} fold={fold} />
      {props.meta}
      <div className="diff-body">
        {narrow ? (
          <Picker
            files={files}
            order={everyFile}
            selected={selected}
            onOpen={open}
            sheet={sheet}
            onSheet={setSheet}
            totals={totals}
            viewed={viewed}
          />
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
            <Button
              size="sm"
              aria-label="Previous match"
              disabled={found.matches.length === 0}
              onClick={() => stepMatch(-1)}
            >
              ‹
            </Button>
            <Button
              size="sm"
              aria-label="Next match"
              disabled={found.matches.length === 0}
              onClick={() => stepMatch(1)}
            >
              ›
            </Button>
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

/**
 * One row of the file list: change letter, name, +N -N, the five-cell bar, the
 * viewed dot. `whole`: the whole path rather than the name, for the phone's
 * sheet, which has no directory groups to say where a name lives.
 */
function FileRow({
  file,
  on,
  viewed,
  onOpen,
  whole = false,
}: {
  file: DiffFile
  on: boolean
  viewed: boolean
  onOpen: () => void
  whole?: boolean
}) {
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
      <span className="diff-fname">{whole ? file.path : baseOf(file.path)}</span>
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

/**
 * The phone's file list: where you are, the open file, previous / next, and
 * `All files`, a sheet of every file with its counts. Choosing a file in the
 * sheet opens it and closes the sheet; Escape and Close close it without
 * moving. ONE FILE IS DRAWN AT A TIME here as everywhere else (owner decision
 * 2026-10-08, design §3 question 4).
 */
function Picker({
  files,
  order,
  selected,
  onOpen,
  sheet,
  onSheet,
  totals,
  viewed,
}: {
  files: DiffFile[]
  order: number[]
  selected: number
  onOpen: (fi: number) => void
  sheet: boolean
  onSheet: (open: boolean) => void
  totals: { add: number; del: number }
  viewed: ReadonlySet<number>
}) {
  const at = order.indexOf(selected)
  const f = files[selected]!
  const prev = order[at - 1]
  const next = order[at + 1]
  const allRef = useRef<HTMLButtonElement>(null)
  const sheetRef = useRef<HTMLDivElement>(null)
  const wasOpen = useRef(sheet)
  // Focus follows the sheet: into it on the open file's row when it opens,
  // back to `All files` when it closes, so a keyboard reader is never left on
  // a control that has gone.
  useEffect(() => {
    if (sheet) sheetRef.current?.querySelector<HTMLElement>('.diff-file.is-on, .diff-file')?.focus()
    else if (wasOpen.current) allRef.current?.focus()
    wasOpen.current = sheet
  }, [sheet])
  return (
    <div className="diff-picker-wrap">
      <div className="diff-picker" role="group" aria-label="File">
        <Button
          size="sm"
          aria-label="Previous file"
          disabled={prev === undefined}
          onClick={() => prev !== undefined && onOpen(prev)}
        >
          ‹
        </Button>
        <span className="diff-picker-at">
          <span className="diff-picker-pos" data-testid="diff-picker-pos">
            file {at + 1} of {order.length}
          </span>
          <span className="diff-fname">{f.path}</span>
          <span className="diff-plus">+{f.additions}</span> <span className="diff-minus">−{f.deletions}</span>
        </span>
        <Button
          size="sm"
          aria-label="Next file"
          disabled={next === undefined}
          onClick={() => next !== undefined && onOpen(next)}
        >
          ›
        </Button>
        <button
          ref={allRef}
          type="button"
          className="diff-all"
          aria-haspopup="dialog"
          aria-expanded={sheet}
          onClick={() => onSheet(!sheet)}
        >
          All files
        </button>
      </div>
      {sheet ? (
        <div
          ref={sheetRef}
          className="diff-sheet"
          role="dialog"
          aria-label="All files"
          onKeyDown={(e) => {
            if (e.key !== 'Escape') return
            // Consumed: the agent drawer around the viewer closes on any Escape that reaches it.
            e.preventDefault()
            e.stopPropagation()
            onSheet(false)
          }}
        >
          <div className="diff-sheet-head">
            <p className="diff-files-head" data-testid="diff-sheet-head">
              {files.length} {files.length === 1 ? 'file' : 'files'} <span className="diff-plus">+{totals.add}</span>{' '}
              <span className="diff-minus">−{totals.del}</span>
            </p>
            <Button size="sm" onClick={() => onSheet(false)}>
              Close
            </Button>
          </div>
          <ol className="diff-sheet-list">
            {order.map((fi) => (
              <li key={fi}>
                <FileRow
                  file={files[fi]!}
                  on={fi === selected}
                  viewed={viewed.has(fi)}
                  whole
                  onOpen={() => {
                    onOpen(fi)
                    onSheet(false)
                  }}
                />
              </li>
            ))}
          </ol>
        </div>
      ) : null}
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
    case 'source':
      return `s${r.file}:${r.hunk}`
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
  /** The open file is collapsed: its header is the only row. */
  collapsed: boolean
  onToggle: (file: number) => void
  /** The open file's lexer, once loaded: a line's tokens, or null to draw it plain. */
  paint?: (text: string) => Token[] | null
}

function RowView({ row, files, context, canExpand, onExpandGap, hits, current, collapsed, onToggle, paint }: RowProps) {
  const f = files[row.file]!
  const common = { 'data-path': f.path, style: { height: rowHeight(row) } }

  switch (row.t) {
    case 'file': {
      const moved = (f.status === 'renamed' || f.status === 'copied') && f.oldPath !== null && f.oldPath !== f.path
      const large = startsCollapsed(f)
      return (
        <button
          type="button"
          className={`diff-row is-file${collapsed ? ' is-collapsed' : ''}`}
          data-diff-row="file"
          aria-expanded={!collapsed}
          title={collapsed ? 'Expand this file' : 'Collapse this file'}
          onClick={() => onToggle(row.file)}
          {...common}
        >
          <span className="diff-fold" aria-hidden="true">
            {collapsed ? '▸' : '▾'}
          </span>
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
          {collapsed ? <span className="diff-folded">{large ? 'collapsed: large' : 'collapsed'}</span> : null}
        </button>
      )
    }
    case 'note':
      return (
        <div className="diff-row is-note" data-diff-row="note" {...common}>
          {row.text}
        </div>
      )
    case 'source':
      return (
        <div className="diff-row is-source" data-diff-row="source" data-step={row.step} {...common}>
          <span className="diff-source-what">from step</span> <span className="diff-source-step">{row.step}</span>
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
            <LineText text={row.text} noNewline={false} paint={paint} />
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
            <LineText
              text={l.text}
              noNewline={l.noNewlineAtEnd}
              hits={hits.get(`${row.hunk}:${row.line}`)}
              current={current}
              paint={paint}
            />
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
              <LineText
                text={l.text}
                noNewline={l.noNewlineAtEnd}
                hits={hits.get(`${row.hunk}:${idx}`)}
                current={current}
                paint={paint}
              />
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
      <Button size="sm" aria-label={label} onClick={() => onExpand(fi, gap)}>
        {state?.status === 'failed' ? 'Retry' : hidden === null ? 'Expand to end of file' : `Expand ${what}`}
      </Button>
    </>
  )
}

/**
 * The pieces of `tokens` that fall in [from, to) of the line: plain text as
 * text nodes, a token with a kind as a `<span>` around its text node. Never
 * markup from a string -- the characters are the line's, cut, not rewritten.
 */
function painted(tokens: readonly Token[], from: number, to: number): ReactNode[] {
  const out: ReactNode[] = []
  let at = 0
  for (const t of tokens) {
    const start = at
    at += t.s.length
    if (at <= from) continue
    if (start >= to) break
    const s = t.s.slice(Math.max(0, from - start), Math.min(t.s.length, to - start))
    out.push(
      t.k === null ? (
        s
      ) : (
        <span key={start} className={`diff-tk is-${t.k}`}>
          {s}
        </span>
      ),
    )
  }
  return out
}

/**
 * The line's text as React text nodes, with a content CR drawn as a visible
 * `␍` rather than as nothing, and each find match wrapped in a `<mark>` -- the
 * current one `is-current` -- around the characters already there. With
 * `paint`, the characters are also cut into syntax tokens (`painted`); a
 * match that crosses tokens holds the pieces of each.
 */
function LineText({
  text,
  noNewline,
  hits,
  current = -1,
  paint,
}: {
  text: string
  noNewline: boolean
  hits?: readonly LineHit[]
  current?: number
  paint?: (text: string) => Token[] | null
}) {
  const cr = text.endsWith('\r')
  const body = cr ? text.slice(0, -1) : text
  const tokens = paint?.(body) ?? null
  let at = 0
  return (
    <>
      {hits === undefined
        ? tokens === null
          ? body
          : painted(tokens, 0, body.length)
        : hitSegments(body, hits).map((p, i) => {
            const from = at
            at += p.text.length
            const inner = tokens === null ? p.text : painted(tokens, from, at)
            return p.index === null ? (
              <Fragment key={i}>{inner}</Fragment>
            ) : (
              <mark key={i} className={`diff-hit${p.index === current ? ' is-current' : ''}`} data-match={p.index + 1}>
                {inner}
              </mark>
            )
          })}
      {cr ? (
        <span className="diff-cr" role="img" aria-label="carriage return">
          ␍
        </span>
      ) : null}
      {noNewline ? <span className="diff-nonl">no newline at end of file</span> : null}
    </>
  )
}
