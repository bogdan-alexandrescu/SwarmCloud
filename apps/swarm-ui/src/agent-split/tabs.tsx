import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'

import { childrenServed, offersChildren } from '../AgentChildren'
import type { TaskPane } from '../App'
import { checkpointsSay, type CheckpointCount } from '../CheckpointsPane'
import { isLiveBrowser, ownerQuestionsHold } from '../Handoff'
import { questionCount } from '../QuestionsCard'
import { TERMINAL_STATES, type Task } from '../types'
import { changesCount } from '../WorkflowChanges'
import { Tabs, type TabDef } from '../components'
import '../styles/agent-tabs.css'

// THE SPLIT'S TAB BAR (moved out of AgentSplit.tsx by SPEED-D4A, 2026-10-14,
// unchanged): each tab's name, which tabs the agent offers with their counts,
// and the bar that fits them, with More for the ones that do not.
//
// The rules the bar keeps (BROWSER-TAB-TABS, #2423): the open tab is always
// on it; the others keep their fixed order and their places whichever tab is
// open, where the room allows; More is a short `More <n>` whose own width is
// measured; and every browser agent, live or finished, has its Browser tab.

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
    // EVERY BROWSER AGENT HAS ITS TAB (BT-007, #2423, 2026-10-26): on a
    // finished agent the session is read only while Browser is open, so the
    // split's `hasBrowser` held the tab only on `/browser`, and leaving it
    // took Browser off the strip and out of More, with no way back but the
    // address. The runner says it is a browser agent, live or finished; a
    // merge held by its owner's questions is git-only and has none (#2313).
    ...(hasBrowser || (task !== null && isLiveBrowser(task) && ownerQuestionsHold(task) === null)
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
 *
 * THE BAR DOES NOT DEPEND ON WHICH TAB IS OPEN (BT-013, #2423, 2026-10-26):
 * filled around the open tab, the bar changed with it -- picking Artifacts
 * from More put Artifacts on the bar and Attempts in More, and opening Logs
 * on a running browser agent sent Browser into More, so the tab just used
 * was no longer where it was left. The bar is now filled once, from the
 * fixed order and the room, with room kept for the widest tab in More, and
 * the open tab is drawn in that room when it is not already on the bar. A
 * ranked tab (`BAR_RANK`) is never given up to keep that room; only when the
 * ranked tabs and the open one cannot all fit (a phone) does the
 * lowest-ranked of them step into More, because the open tab must be seen.
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
  const width = (i: number) => widths[i] ?? 0
  const span = (set: readonly number[]) => set.reduce((n, i) => n + width(i), 0) + gap * Math.max(0, set.length - 1)
  const free = room - moreWidth
  // The bar as the order and the room fill it, whichever tab is open.
  const base: number[] = []
  for (const i of byRank(keys)) {
    if (span([...base, i]) > free) break
    base.push(i)
  }
  // Room for any tab in More to be drawn on the bar when it is opened: an
  // unranked tab steps off the bar (the lowest-ranked first) until there is.
  const reserve = () => Math.max(0, ...keys.map((_, i) => i).filter((i) => !base.includes(i)).map(width)) + gap
  for (let at = base.length - 1; at >= 0 && span(base) + reserve() > free; at--) {
    if (!BAR_RANK.includes(keys[base[at]!]!)) base.splice(at, 1)
  }
  const open = keys.indexOf(current)
  if (open !== -1 && !base.includes(open)) {
    // The open tab is always on the bar: on a phone a ranked tab gives way to it.
    while (base.length > 0 && span([...base, open]) > free) base.pop()
    base.push(open)
  }
  const shown = new Set(base)
  return keys.filter((_, i) => shown.has(i))
}

/**
 * THE FAILURE PATH NEVER MOVES (owner decision 2026-10-22, Console Design
 * Calls 2, c01; visual QA run 4, flow-find-failure-phone-390-P3): filled in
 * draw order, a 390 bar read `Details · Logs · Changes not counted` and the
 * Attempts tab -- the next step after a failure -- was behind More, or on it,
 * depending on which tab was open. An overflowing bar is filled by this rank;
 * it is still DRAWN in the usual order, and a bar where every tab fits is
 * not ranked at all.
 *
 * BROWSER IS RANKED TOO (BT-013, #2423): it was already the first of the
 * rest, so the order the bar fills in is unchanged; ranked, it is never
 * moved into More to keep room for another tab, so opening Logs on a
 * running browser agent no longer sends its Browser tab into More.
 */
const BAR_RANK: readonly string[] = ['detail', 'logs', 'attempts', 'browser']

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
  // More's padding, border and gap: the stylesheet's, read once as the gap is.
  const moreChrome = useRef<number | null>(null)
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
    // A LABEL THAT CHANGED REPLACES ITS OLD WIDTH (QA14-UI): Browser
    // losing LIVE is a narrower tab, and the wider one must not be the
    // width it falls back to once it is in More.
    const keep = (t: TabDef, w: number) => {
      for (const k of [...widths.current.keys()]) if (k.startsWith(`${t.key}\t`)) widths.current.delete(k)
      widths.current.set(sizeKey(t), w)
    }
    barTabs.forEach((t, i) => {
      const w = drawn[i]?.offsetWidth
      if (w !== undefined) keep(t, w)
    })
    // A TAB IN MORE IS MEASURED WITH THE COUNT IT HAS NOW (overview-agents-45,
    // visual QA pass 1): measured only while it was on the bar, Checkpoints
    // kept the width of the count it had at mount, the room kept for it was
    // too small, and picking it from More pushed another tab (Attempts,
    // Changes) off the bar. The hidden copy draws every tab; a copy not laid
    // out (0) leaves the bar's own measure standing.
    const copies = b.querySelectorAll<HTMLElement>(':scope > .ag-tabs-measure > .c-tabs > button')
    tabs.forEach((t, i) => {
      const w = copies[i]?.offsetWidth ?? 0
      if (w > 0) keep(t, w)
    })
    // Nothing laid out (\`clientWidth\` 0) fits everything, so the gap is only read once there is a width to fit.
    if (gapPx.current === null && strip !== null && b.clientWidth > 0) {
      gapPx.current = parseFloat(getComputedStyle(strip).columnGap) || 0
    }
    const gap = gapPx.current ?? 0
    // MORE IS MEASURED FROM WHAT IT DRAWS, NOT FROM ITS BOX (VQ-008 review,
    // 2026-10-26; BT-004, #2423): the host gives way (agents.css), so its
    // `offsetWidth` -- and the squeezed button's `scrollWidth` -- is the width
    // it was already squeezed to: at 390 More was fitted at 32px and drawn
    // `2 m`, its chevron off the page. The room it needs is its label
    // (`More 7`, whose own `scrollWidth` is the whole text however it is cut),
    // its chevron, and the button's padding, border and one gap between them;
    // the names are read, not drawn (`.ag-tabs-more-names`), and take none.
    const m = b.querySelector<HTMLElement>(':scope > .ag-tabs-more-host')
    const mb = m?.querySelector<HTMLElement>(':scope > .ag-tabs-more') ?? null
    if (m !== null && mb !== null) {
      if (moreChrome.current === null && b.clientWidth > 0) {
        const cs = getComputedStyle(mb)
        const px = (v: string) => parseFloat(v) || 0
        moreChrome.current = px(cs.paddingLeft) + px(cs.paddingRight) + px(cs.borderLeftWidth) + px(cs.borderRightWidth) + px(cs.columnGap)
      }
      moreWidth.current = Math.ceil(
        (moreChrome.current ?? 0) +
          (mb.querySelector<HTMLElement>(':scope > .ag-tabs-more-label')?.scrollWidth ?? 0) +
          (mb.querySelector<HTMLElement>(':scope > [aria-hidden]')?.offsetWidth ?? 0),
      )
    }
    // THE OPEN TAB KEEPS ITS WIDTH (BT-004): at 390 the strip holding it was
    // squeezed to 0 px beside More, and nothing said which tab was open. The
    // strip is never narrower than its open tab, and a fitted strip
    // (`is-fitted`) never shrinks at all; More gives way instead, its label
    // cut with an ellipsis only when even `More 7` cannot fit beside the open
    // tab. And the `›` the strip's end draws sits at the strip's end, not over
    // More.
    const on = barTabs.findIndex((t) => t.key === current)
    if (strip !== null) strip.style.minWidth = on === -1 || drawn[on] === undefined ? '' : `${drawn[on]!.offsetWidth}px`
    b.style.setProperty('--ag-more-w', `${m === null ? 0 : m.offsetWidth}px`)
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
      // Its bold width is reserved (agent-tabs.css), so opening it moves nothing (overview-agents-33).
      const label = el.querySelector(':scope > .c-tab-label')
      if (label !== null && label.getAttribute('data-label') !== label.textContent) label.setAttribute('data-label', label.textContent ?? '')
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
  const stripClass = moreTabs.length === 0 ? 'ag-split-tabs' : 'ag-split-tabs is-fitted'
  return (
    <div className="ag-tabs-edge" ref={box} data-more={more || undefined} onKeyDown={onKey}>
      {/* EVERY TAB, HIDDEN, TO BE MEASURED (overview-agents-45): the widths
          the bar is fitted to, the ones in More included. Drawn as the bar
          draws them, with the bold width reserved, and never a stop. Its
          words are the sheet's (`data-text`), not text: nothing reads or
          finds them a second time. First, so More is still the box's last
          child, at the bar's end. */}
      <div className="ag-tabs-measure" aria-hidden="true">
        <div className="c-tabs ag-split-tabs">
          {tabs.map((t) => (
            <button key={t.key} type="button" tabIndex={-1}>
              <span className="c-tab-label" data-label={t.label} data-text={t.label} />
              {t.count !== undefined && <em data-text={t.count === null ? '—' : String(t.count)} />}
            </button>
          ))}
        </div>
      </div>
      {/* A FITTED BAR NEVER SHRINKS (VQ-008): it holds only the tabs that
          fit, the open one always, so More gives way and the open tab stays
          drawn and marked (agents.css `.ag-split-tabs.is-fitted`). */}
      <Tabs
        className={stripClass}
        label="Agent panes"
        current={current}
        tabs={barTabs}
        onSelect={onSelect}
      />
      {moreTabs.length > 0 && <MoreTabs tabs={moreTabs} onSelect={onSelect} />}
    </div>
  )
}

/**
 * MORE: the tabs that did not fit, as a disclosure like `Menu`
 * (components/Overlay.tsx) -- a labelled group of buttons, not an ARIA menu.
 * Each says its count as the bar does, in the bar's badge and with the bar's
 * name (`tabName`). Choosing a tab opens it and closes.
 *
 * THE KEYS REACH IT (overview-agents-44, visual QA pass 1): ArrowDown on
 * `More 3` did nothing, and Escape pressed with the menu open and focus in
 * the list column closed the whole agent pane, because only a key pressed
 * inside More was More's. Now ArrowDown (ArrowUp) on More opens it at its
 * first (last) tab, the arrows, Home and End move through its tabs, and
 * while it is open its Escape is taken first, wherever focus is: it closes
 * the menu and is spent (`defaultPrevented`, which the split's own Escape
 * leaves alone, agent-split/focus.ts), so only a second Escape closes the
 * pane. A press outside closes it too.
 */
function MoreTabs({ tabs, onSelect }: { tabs: readonly TabDef[]; onSelect: (key: string) => void }) {
  const [open, setOpen] = useState(false)
  // The tab to focus once the menu is drawn, from an arrow pressed on More.
  const [focusAt, setFocusAt] = useState<number | null>(null)
  const host = useRef<HTMLSpanElement>(null)
  const names = tabs.map((t) => t.label).join(', ')
  const items = () => [...(host.current?.querySelectorAll<HTMLButtonElement>(':scope > .ag-tabs-menu > .c-menu-item') ?? [])]
  useEffect(() => {
    if (!open || focusAt === null) return
    const all = items()
    all[(focusAt + all.length) % Math.max(1, all.length)]?.focus()
    setFocusAt(null)
  }, [open, focusAt])
  useEffect(() => {
    if (!open) return
    const away = (e: Event) => {
      if (e.target instanceof Node && host.current?.contains(e.target)) return
      setOpen(false)
    }
    // Capture, so it is heard before the panel's and the split's own Escape.
    const escape = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      const inside = document.activeElement !== null && host.current?.contains(document.activeElement) === true
      setOpen(false)
      if (inside) host.current?.querySelector<HTMLButtonElement>('.ag-tabs-more')?.focus()
    }
    document.addEventListener('pointerdown', away)
    window.addEventListener('keydown', escape, true)
    return () => {
      document.removeEventListener('pointerdown', away)
      window.removeEventListener('keydown', escape, true)
    }
  }, [open])
  const onKey = (e: KeyboardEvent<HTMLSpanElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return
    const all = items()
    const at = all.indexOf(e.target as HTMLButtonElement)
    if (at === -1) {
      // On More itself: the arrows open it, at its first or its last tab.
      if (!(e.target as HTMLElement).classList.contains('ag-tabs-more')) return
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
      e.preventDefault()
      setOpen(true)
      setFocusAt(e.key === 'ArrowDown' ? 0 : -1)
      return
    }
    const to =
      e.key === 'ArrowDown'
        ? (at + 1) % all.length
        : e.key === 'ArrowUp'
          ? (at - 1 + all.length) % all.length
          : e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? all.length - 1
              : null
    if (to === null) return
    e.preventDefault()
    all[to]!.focus()
  }
  return (
    <span className="c-menu-host ag-tabs-more-host" ref={host} onKeyDown={onKey}>
      {/* IT SAYS WHAT IT HOLDS (visual QA QA-116, run 2): `More 2 ▾` at 390
          read as two hidden tabs or as two checkpoints, and nothing said
          Checkpoints was behind it. Its name and its title list them.
          IT IS SHORT (BT-004, BT-005, #2423, 2026-10-26): it drew every name,
          `7 more: Details, Logs, Changes, …`, and so was as wide as the tabs
          it hid -- 420 px at 1024, beside one tab, and wider than a 390
          drawer, where it covered the open tab. It draws `More 7`; the names
          stay its title and the rest of its name (`More 7 tabs: Details,
          Logs, …`), in text that is read but not drawn, so the name still starts
          with what it shows and needs no aria-label to drift from it. */}
      <button type="button" className="ag-tabs-more" aria-expanded={open} title={names} onClick={() => setOpen((v) => !v)}>
        <span className="ag-tabs-more-label">More {tabs.length}</span>
        <span className="ag-tabs-more-names"> tabs: {names}</span> <span aria-hidden="true">▾</span>
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
