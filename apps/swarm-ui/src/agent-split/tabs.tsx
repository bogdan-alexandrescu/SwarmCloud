import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'

import { childrenServed, offersChildren } from '../AgentChildren'
import type { TaskPane } from '../App'
import { checkpointsSay, type CheckpointCount } from '../CheckpointsPane'
import { questionCount } from '../QuestionsCard'
import { TERMINAL_STATES, type Task } from '../types'
import { changesCount } from '../WorkflowChanges'
import { Tabs, type TabDef } from '../components'

// THE SPLIT'S TAB BAR (moved out of AgentSplit.tsx by SPEED-D4A, 2026-10-14,
// unchanged): each tab's name, which tabs the agent offers with their counts,
// and the bar that fits them, with More for the ones that do not.

/** Each tab's name, which is also its address segment (QA12-17): `/agents/<tab>/<id>/<name>`. */
export const PANE_LABELS: Readonly<Record<TaskPane, string>> = {
  detail: 'Details',
  browser: 'Browser',
  logs: 'Logs',
  changes: 'Changes',
  children: 'Children',
  attempts: 'Attempts',
  artifacts: 'Artifacts',
  checkpoints: 'Checkpoints',
}

/** One tab of the split: its pane, its name, its count and the count's reason, and its address. */
export type PaneTab = { id: TaskPane; label: string; count: number | string | null; say: string | null; to: string }

/**
 * The tabs the agent offers, in their order, with their counts. Built from
 * the split's own reads; nothing here reads anything.
 */
export function paneTabs({
  task,
  base,
  hasBrowser,
  sessionOpen,
  childCount,
  runs,
  ckpt,
  ckptCount,
  pane = null,
}: {
  task: Task | null
  base: string
  hasBrowser: boolean
  sessionOpen: boolean
  childCount: number | null
  runs: number | null
  ckpt: CheckpointCount | null
  ckptCount: number | string | null
  /** The pane the address names: its own tab is always on the strip (task-009). */
  pane?: TaskPane | null
}): PaneTab[] {
  return [
    // THE QUESTIONS' COUNT (part of #1206): the agent's questions for its
    // owner lead Details, so the tab says how many; no badge when none.
    {
      id: 'detail',
      label: PANE_LABELS.detail,
      count: questionCount(task) || null,
      say: questionCount(task) > 0 ? `The agent asked you ${questionCount(task)} question(s); they are at the top of Details.` : null,
      to: base,
    },
    // DETAILS, BROWSER, LOGS (the mockup, owner decision 2026-10-12): Browser
    // sits second, and says LIVE while its session is open.
    ...(hasBrowser
      ? [
          {
            id: 'browser' as const,
            label: PANE_LABELS.browser,
            count: sessionOpen ? 'LIVE' : null,
            say: sessionOpen ? 'The agent’s browser session is open.' : null,
            to: `${base}/browser`,
          },
        ]
      : []),
    { id: 'logs', label: PANE_LABELS.logs, count: null, say: null, to: `${base}/logs` },
    // THE COUNT IS THE FILE COUNT (`git.files`, DIFF4), and words with their
    // reason where the summary does not say it -- never a 0 nobody measured.
    (() => {
      const c = changesCount(task)
      return { id: 'changes' as const, label: PANE_LABELS.changes, count: c.count ?? unknownCount(task), say: c.say, to: `${base}/changes` }
    })(),
    // CHILDREN HAS AN ADDRESS (U10a D36): `<agent>/children`, as every
    // other pane has one, so the tab moves the URL and a link can open it.
    // THE ADDRESS NAMES IT, SO THE STRIP HAS IT (visual QA run 2, task-009):
    // `/children` on an agent that does not offer children drew the pane
    // under a strip with no Children tab, and so with no tab selected. The
    // pane itself says why it is empty; the tab is how the reader sees where
    // they are. The same rule keeps Browser on `/browser` (`hasBrowser`).
    ...((task !== null && offersChildren(task)) || pane === 'children'
      ? [
          {
            id: 'children' as const,
            label: PANE_LABELS.children,
            // A CHILD HAS NONE (visual QA run 6, task-002): `/children` on a
            // child agent drew the tab (c04) with the split's count, which is
            // never read for an agent that does not offer children, so `—`
            // sat over the pane's `none`. Children are one level deep, so a
            // child's count is 0 without a read. On `/children` the split now
            // reads it too (`useChildCount`, the pane's own read), so the 0
            // stands only until that read answers.
            count: childCount ?? (isChild(task) ? 0 : null),
            say:
              childCount === null && !isChild(task)
                ? 'The children read has not answered, so their count is not known.'
                : null,
            to: `${base}/children`,
          },
        ]
      : []),
    {
      id: 'attempts',
      label: PANE_LABELS.attempts,
      count: runs,
      say: task === null ? 'The agent has not been read yet.' : null,
      to: `${base}/attempts`,
    },
    {
      id: 'artifacts',
      label: PANE_LABELS.artifacts,
      count: artifactCount(task) ?? unknownCount(task),
      say:
        artifactCount(task) === null
          ? 'Files are listed when an attempt ends: this agent has no result summary yet, so the count is not known.'
          : null,
      to: `${base}/artifacts`,
    },
    {
      id: 'checkpoints',
      label: PANE_LABELS.checkpoints,
      count: ckptCount,
      say: checkpointsSay(ckpt),
      to: `${base}/checkpoints`,
    },
  ]
}

/**
 * WHERE A KEY MOVES IN A TABLIST (visual QA run 2, a11y-keyboard-001): the
 * WAI-ARIA tabs pattern's keys, from the tab at `at` among `count`. Arrows
 * wrap at the ends; Home and End go to them. Null for any other key, which
 * the strip leaves to the browser. Shared by this strip and the list's Live /
 * Waiting / Recent (Agents.tsx).
 */
export function tabStep(key: string, at: number, count: number): number | null {
  if (count <= 0) return null
  if (key === 'ArrowRight') return (at + 1) % count
  if (key === 'ArrowLeft') return (at - 1 + count) % count
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  return null
}

/**
 * WHICH TABS STAY ON THE BAR (QA12-18): every one when they fit; otherwise a
 * run of them that fits beside More, taken in `BAR_RANK`'s order (c01), and
 * the open tab always; each in its own place in the drawn order. Nothing measured (`room` 0: not laid out) is
 * everything fitting -- a bar is never emptied into More by a missing read.
 *
 * THE GAPS ARE PART OF THE BAR (QA14-UI, owner QA 2026-10-13): on a parked
 * browser agent at 1440 the tabs summed to 529 in a 533 strip, and the six
 * 2 px gaps between seven tabs made 541 -- `Artifacts` was cut under More
 * and the strip scrolled. `gap` is the strip's own gap between two tabs.
 */
export function fitTabs(
  keys: readonly string[],
  widths: readonly number[],
  room: number,
  moreWidth: number,
  current: string,
  gap = 0,
): string[] {
  const total = widths.reduce((n, w) => n + w, 0) + gap * Math.max(0, keys.length - 1)
  if (room <= 0 || total <= room) return [...keys]
  const open = keys.indexOf(current)
  const shown = new Set<string>(open === -1 ? [] : [current])
  let used = open === -1 ? 0 : (widths[open] ?? 0)
  for (const i of byRank(keys)) {
    if (i === open) continue
    const w = (widths[i] ?? 0) + (shown.size > 0 ? gap : 0)
    if (used + w > room - moreWidth) break
    used += w
    shown.add(keys[i]!)
  }
  return keys.filter((k) => shown.has(k))
}

/**
 * THE FAILURE PATH NEVER MOVES (owner decision 2026-10-22, Console Design
 * Calls 2, c01; visual QA run 4, flow-find-failure-phone-390-P3): filled in
 * draw order, a 390 bar read `Details · Logs · Changes not counted` and the
 * Attempts tab -- the next step after a failure -- was behind More, or on it,
 * depending on which tab was open. An overflowing bar is filled by this rank;
 * it is still DRAWN in the usual order, and a bar where every tab fits is
 * not ranked at all.
 */
const BAR_RANK: readonly string[] = ['detail', 'logs', 'attempts']

/** The indexes of `keys` in the order the bar is filled: BAR_RANK's, then the rest as drawn. */
function byRank(keys: readonly string[]): number[] {
  const ranked = BAR_RANK.map((k) => keys.indexOf(k)).filter((i) => i !== -1)
  return [...ranked, ...keys.map((_, i) => i).filter((i) => !ranked.includes(i))]
}

/**
 * A TAB'S NAME, WITH ITS COUNT SET APART (visual QA run 4, N15): the label
 * and the count are two elements, and with nothing between them a reader's
 * name for the tab ran them together -- `Changes not counted` read as a
 * sentence, or as a tab that is switched off. The name puts a comma between
 * them, `Changes, not counted`; a tab with no count is its label alone.
 *
 * A COUNT NOT KNOWN IS NOT NAMED IN WORDS THE TAB DOES NOT SHOW (visual QA
 * run 6, box 6343755826): the badge draws `—` and the name said `Attempts,
 * count not known`, a name that is not the tab's text (axe
 * label-content-name-mismatch). The name is the label, as the text is once
 * the dash is read as the symbol it is; the reason stays the badge's title.
 */
export function tabName(t: TabDef): string {
  if (t.count === undefined || t.count === null) return t.label
  return `${t.label}, ${t.count}`
}

/**
 * THE TAB BAR FITS (QA12-18, live-browser QA p12): at 1440 with the list open
 * the bar was cut at `C ›` -- eight tabs with counts in a column a third of
 * the page wide. The tabs that do not fit go into More at the bar's end, and
 * the open tab is never one of them. Measured, never guessed: each tab's width
 * as drawn on the bar, with its count, and the box's width is the room.
 * Measured on every draw (counts land a read later, WA-17) and on resize.
 *
 * THE TABS SAY WHEN THERE ARE MORE OF THEM (U10a D39, owner QA at 390px,
 * 2026-10-04): on a phone the strip was cut at `Che…` and nothing said it
 * scrolls. The strip still scrolls sideways (Q4) if it ever must; this marks
 * which of its ends has more beyond it, `data-more="start end"`, and the
 * sheet fades that edge with a chevron. Measured on scroll and on resize.
 */
export function AgTabsEdge({
  tabs,
  current,
  onSelect,
}: {
  tabs: readonly TabDef[]
  current: string
  onSelect: (key: string) => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState('')
  // The keys on the bar, or null for all of them.
  const [shownKeys, setShownKeys] = useState<string | null>(null)
  // Each tab's width as last drawn on the bar, keyed with its label and
  // count so a count that lands is measured again; and More's own.
  const widths = useRef(new Map<string, number>())
  const moreWidth = useRef(0)
  // The strip's gap, read once: it is the stylesheet's, and `getComputedStyle`
  // on every fit -- which every strip mutation now runs -- held the panes'
  // first reads back by seconds in jsdom (walk.agent, reads.dedup).
  const gapPx = useRef<number | null>(null)
  const onBar = shownKeys === null ? null : new Set(shownKeys.split('\n'))
  const barTabs = onBar === null ? tabs : tabs.filter((t) => onBar.has(t.key))
  const moreTabs = onBar === null ? [] : tabs.filter((t) => !onBar.has(t.key))
  const sizeKey = (t: TabDef) => `${t.key}\t${t.label}\t${t.count ?? ''}`
  const fit = useRef(() => {})
  fit.current = () => {
    const b = box.current
    if (b === null) return
    const strip = b.querySelector<HTMLElement>(':scope > .c-tabs')
    const drawn = b.querySelectorAll<HTMLElement>(':scope > .c-tabs > [role="tab"]')
    barTabs.forEach((t, i) => {
      const w = drawn[i]?.offsetWidth
      if (w === undefined) return
      // A LABEL THAT CHANGED REPLACES ITS OLD WIDTH (QA14-UI): Browser
      // losing LIVE is a narrower tab, and the wider one must not be the
      // width it falls back to once it is in More.
      for (const k of [...widths.current.keys()]) if (k.startsWith(`${t.key}\t`)) widths.current.delete(k)
      widths.current.set(sizeKey(t), w)
    })
    // Nothing laid out (\`clientWidth\` 0) fits everything, so the gap is only read once there is a width to fit.
    if (gapPx.current === null && strip !== null && b.clientWidth > 0) {
      gapPx.current = parseFloat(getComputedStyle(strip).columnGap) || 0
    }
    const gap = gapPx.current ?? 0
    const m = b.querySelector<HTMLElement>(':scope > .ag-tabs-more-host')
    if (m !== null) moreWidth.current = m.offsetWidth
    // A tab not drawn since its count changed keeps its last width (or its
    // label's), and is measured again the first time it is back on the bar.
    const last = (t: TabDef) =>
      widths.current.get(sizeKey(t)) ??
      [...widths.current].find(([k]) => k.startsWith(`${t.key}\t`))?.[1] ??
      0
    const shown = fitTabs(tabs.map((t) => t.key), tabs.map(last), b.clientWidth, moreWidth.current, current, gap)
    const next = shown.length === tabs.length ? null : shown.join('\n')
    setShownKeys((was) => (was === next ? was : next))
  }
  // Every draw is measured before it is painted; it settles in a draw or two
  // because a fit is only set when it differs from the last.
  useLayoutEffect(() => fit.current())
  useEffect(() => {
    const b = box.current
    if (b === null) return
    const measure = () => fit.current()
    globalThis.addEventListener?.('resize', measure)
    const seen = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    seen?.observe(b)
    return () => {
      globalThis.removeEventListener?.('resize', measure)
      seen?.disconnect()
    }
  }, [])

  // THE OPEN TAB IS ALWAYS IN VIEW (V148): on a phone the strip is one row
  // that scrolls, so a tab chosen by address (`/changes`, `/checkpoints`) can
  // sit past the edge. The STRIP is scrolled, never the page:
  // `scrollIntoView` would move every scroller above it as well.
  useEffect(() => {
    const strip = box.current?.querySelector<HTMLElement>(':scope > .c-tabs')
    const on = strip?.querySelector<HTMLElement>('[aria-selected="true"], [aria-current="page"]')
    if (strip === null || strip === undefined || on === null || on === undefined) return
    const s = strip.getBoundingClientRect()
    const t = on.getBoundingClientRect()
    if (t.left < s.left) strip.scrollLeft -= s.left - t.left
    else if (t.right > s.right) strip.scrollLeft += t.right - s.right
  }, [current])
  useEffect(() => {
    const strip = box.current?.querySelector<HTMLElement>(':scope > .c-tabs')
    if (strip === null || strip === undefined) return
    const measure = () => {
      const over = strip.scrollWidth - strip.clientWidth
      const at = strip.scrollLeft
      setMore([over > 1 && at > 1 ? 'start' : '', over > 1 && at < over - 1 ? 'end' : ''].filter(Boolean).join(' '))
    }
    measure()
    strip.addEventListener('scroll', measure, { passive: true })
    globalThis.addEventListener?.('resize', measure)
    const seen = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    seen?.observe(strip)
    // THE TABS GROW AFTER THE STRIP IS MEASURED (live QA WA-17, 1000x800):
    // the counts land a read later, the labels widen and the strip's own box
    // does not change size, so the ResizeObserver never fired and the
    // overlay's strip was cut at `Checkpo` with no `›`. A change to what the
    // strip holds measures it again.
    // A label or count that changed is fitted again too (QA14-UI).
    const grew =
      typeof MutationObserver === 'undefined'
        ? null
        : new MutationObserver(() => {
            fit.current()
            measure()
          })
    grew?.observe(strip, { childList: true, subtree: true, characterData: true })
    return () => {
      strip.removeEventListener('scroll', measure)
      globalThis.removeEventListener?.('resize', measure)
      seen?.disconnect()
      grew?.disconnect()
    }
  }, [])
  // THE TABS PATTERN (visual QA run 2, a11y-keyboard-001): the strip said
  // role=tab and did nothing a tablist does -- every tab was its own Tab
  // stop and the arrows did nothing. Now the open tab is the strip's one Tab
  // stop (a roving tabindex), and Left / Right / Home / End move focus and
  // open the tab they land on: automatic activation, since a pane opens as
  // fast as a click opens it. `Tabs` (components/Tabs.tsx) draws the buttons
  // and takes no tabIndex, so it is set on the drawn strip after every draw.
  // Each tab's name (`tabName`) is set the same way, after every draw, for
  // the same reason: `Tabs` draws every strip in the console and takes none.
  useLayoutEffect(() => {
    const drawn = [...(box.current?.querySelectorAll<HTMLElement>(':scope > .c-tabs > [role="tab"]') ?? [])]
    const on = drawn.findIndex((el) => el.getAttribute('aria-selected') === 'true')
    drawn.forEach((el, i) => {
      el.setAttribute('tabindex', i === (on === -1 ? 0 : on) ? '0' : '-1')
      const t = barTabs[i]
      if (t === undefined || t.count === undefined) el.removeAttribute('aria-label')
      else el.setAttribute('aria-label', tabName(t))
    })
  })
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const drawn = [...(box.current?.querySelectorAll<HTMLElement>(':scope > .c-tabs > [role="tab"]') ?? [])]
    const at = drawn.indexOf(e.target as HTMLElement)
    if (at === -1 || e.altKey || e.ctrlKey || e.metaKey) return
    const to = tabStep(e.key, at, drawn.length)
    const key = to === null ? undefined : barTabs[to]?.key
    if (to === null || key === undefined) return
    e.preventDefault()
    drawn[to]!.focus()
    if (key !== current) onSelect(key)
  }
  return (
    <div className="ag-tabs-edge" ref={box} data-more={more || undefined} onKeyDown={onKey}>
      <Tabs className="ag-split-tabs" label="Agent panes" current={current} tabs={barTabs} onSelect={onSelect} />
      {moreTabs.length > 0 && <MoreTabs tabs={moreTabs} onSelect={onSelect} />}
    </div>
  )
}

/**
 * MORE: the tabs that did not fit, as a disclosure like `Menu`
 * (components/Overlay.tsx) -- a labelled group of buttons, not an ARIA menu,
 * which would promise arrow-key roving. Each says its count as the bar does,
 * in the bar's badge and with the bar's name (`tabName`).
 * Escape and a press outside close it; choosing a tab opens it and closes.
 */
function MoreTabs({ tabs, onSelect }: { tabs: readonly TabDef[]; onSelect: (key: string) => void }) {
  const [open, setOpen] = useState(false)
  const host = useRef<HTMLSpanElement>(null)
  const names = tabs.map((t) => t.label).join(', ')
  useEffect(() => {
    if (!open) return
    const away = (e: Event) => {
      if (e.target instanceof Node && host.current?.contains(e.target)) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [open])
  return (
    <span
      className="c-menu-host ag-tabs-more-host"
      ref={host}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.stopPropagation()
          setOpen(false)
          host.current?.querySelector<HTMLButtonElement>('.ag-tabs-more')?.focus()
        }
      }}
    >
      {/* IT SAYS WHAT IT HOLDS (visual QA QA-116, run 2): `More 2 ▾` at 390
          read as two hidden tabs or as two checkpoints, and nothing said
          Checkpoints was behind it. It reads `2 more tabs`, and its name and
          its title list them.
          AND THE GLASS NAMES THEM (visual QA run 6, flow-find-failure-phone-05,
          box 6343793416): on a phone no one hovers, so a name and a title
          were all that said what `2 more tabs` hid. It reads
          `2 more: Artifacts, Checkpoints ▾`, keeping the count (c02/c20), and
          it is named by that text, with no aria-label to drift from it. */}
      <button type="button" className="ag-tabs-more" aria-expanded={open} title={names} onClick={() => setOpen((v) => !v)}>
        {tabs.length} more: {names} <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="c-menu ag-tabs-menu" role="group" aria-label="More agent panes">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              className="c-menu-item"
              aria-label={t.count === undefined ? undefined : tabName(t)}
              onClick={() => {
                setOpen(false)
                onSelect(t.key)
              }}
            >
              <span className="c-tab-label">{t.label}</span>
              {/* A WORD APART, as the bar's (`TabCount`, QA-007): the item's
                  text is `Changes not yet`, which its name holds. */}
              {t.count !== undefined && (
                <>
                  {' '}
                  <em title={t.why}>{t.count === null ? '—' : t.count}</em>
                </>
              )}
            </button>
          ))}
        </div>
      )}
    </span>
  )
}

/**
 * WHAT AN UNKNOWN COUNT MEANS, IN WORDS (visual QA run 3, ff-009, #1680):
 * `Changes —` and `Artifacts —` drew a bare dash, which read as zero, as a
 * failure or as nothing at all. Both counts come from what an attempt writes
 * when it ends, so the words say where the agent is: `not yet` while it runs
 * (a count is coming), `not counted` once it has ended without one (none is
 * coming), `not read` before the task is. The reason stays the title.
 */
export function unknownCount(task: Task | null): string {
  if (task === null) return 'not read'
  return TERMINAL_STATES.has(task.state) ? 'not counted' : 'not yet'
}

/** The agent is itself a child, on an API that serves the child fields: it has no children (depth 1). */
function isChild(task: Task | null): boolean {
  return task !== null && childrenServed(task) && (task.parent_task_id ?? null) !== null
}

/** The Artifacts tab's count: the manifest's files once a summary is written, else unknown. */
function artifactCount(task: Task | null): number | null {
  const a = (task?.result_summary as Record<string, unknown> | null | undefined)?.artifacts
  return Array.isArray(a) ? a.length : null
}
