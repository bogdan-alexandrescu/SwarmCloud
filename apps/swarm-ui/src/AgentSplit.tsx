import {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'

import { AgChildrenPane, AgParentLink, offersChildren, useChildCount } from './AgentChildren'
import { AgentDetailScreen, DRAWER_POLL_MS, Mark, SplitClock } from './AgentDetail'
import { AgentLogs, LogLastLine } from './AgentLogs'
import { agentName, backLabel, rememberAgentName, workflowHref } from './agentlist'
import { loadTask } from './api'
import type { TaskPane } from './App'
import { ArtifactsScreen } from './Artifacts'
import { AttemptTimelineScreen } from './AttemptTimeline'
import { CheckpointsPane, checkpointsSay, type CheckpointCount } from './CheckpointsPane'
import { isOverlay, trapTab } from './focus'
import {
  SNAPS,
  SNAP_LABEL,
  SNAP_TEXT,
  SNAP_WIDTH,
  listSnap,
  nearestSnap,
  setListSnap,
  toggleListSnap,
  useListSnap,
  type ListSnap,
} from './listSnap'
import { useRead } from './RunFiles'
import { StopRun } from './StopRun'
import { TaskIdLine } from './TaskIdLine'
import { useResourceClasses } from './Blockers'
import { AgentChangesPane, changesCount } from './WorkflowChanges'
import { HelpCard } from './HelpCard'
import {
  CONCURRENCY_STATES,
  STRATEGY_LABEL,
  TERMINAL_STATES,
  accountText,
  clockTime,
  dispatchOf,
  elapsed,
  type DispatchRole,
  type Task,
} from './types'
import { rowClock, useNow } from './useNow'
import './styles/agents.css'
import { Button, CIcon, StateMark, Tabs } from './components'

/**
 * ONE AGENT, IN THE SPLIT (agents.html V1, decided 2026-10-01; viewers.html A
 * and agent-detail-2.html A, picked 2026-10-02).
 *
 * The list on the left, the selected agent on the right: a header row (state
 * pill, the agent's name, Copy link, Stop), underline tabs with counts --
 * Details, Logs, Changes, Children when the agent has any, Attempts,
 * Artifacts, Checkpoints -- and the open tab, which fills the rest of the
 * column.
 *
 * CHANGES IS A TAB (docs/design/diff-viewer.md §2 variant 2, owner decision
 * 2026-10-08): the agent's patch in the shipped viewer at the pane's full
 * height, the open file in the address (`/changes/<path>`). Opening it folds
 * the list to its strip so the file has the room split needs; see the fold
 * effect below for why that is remembered and not forced.
 *
 * THE LOG IS A TAB (owner decision 2026-10-04). It was a dock along the foot
 * of this column, open across every tab, and it lay over the agent's details;
 * it is the Logs tab now (AgentLogs.tsx), full height under the header with
 * nothing beneath it. A running agent opens on Logs, a finished one on
 * Details, whose one-line last log line switches to it.
 *
 * MOVED OUT OF App.tsx (lane U1, 2026-10-02). It was `AgentDrawer` there; the
 * shell is another lane's, and this is the Agents section's own screen. App
 * keeps the route and renders this with the same five props.
 *
 * IT IS A GRID COLUMN, NOT AN OVERLAY, WHEREVER TWO PANES FIT (§B3). Below
 * 1100px total width two panes genuinely do not fit and it reverts to the
 * overlay, `role="dialog"` and all -- which is why that role is on the
 * element unconditionally rather than switched with the layout. On a phone
 * the list and the agent are two pages, and the back link is the way back.
 *
 * THE LIST'S WIDTH IS THE VIEWER'S: the divider snaps it to a 64px strip, the
 * 380px compact list or half the work area, remembered per device
 * (listSnap.ts), and published as `--list-w` on the documentElement because
 * the element that has to react to it is `.app`'s grid template.
 *
 * `AgentDetailScreen` draws its own `.drawer` and its own close button. Both
 * are flattened by two rules scoped to `.ctl-drawer` in styles.css. Its own
 * stop control is not drawn here (`headed`): Stop is in this header row.
 */
/** How long after a keyboard move of the divider the page is held where it was. */
export const HOLD_SCROLL_MS = 400

/**
 * THE DIVIDER NEVER MOVES THE PAGE (owner QA R3, 2026-10-04: the no-jump
 * rule). A keyboard move of the divider shifted the page scroller 0 -> 12:
 * the list's new width re-lays both columns, and the browser re-anchors the
 * page's scroll while it does. The page's scroll offset is recorded at the
 * key and put back on every scroll the move causes, for the few frames the
 * re-layout takes (`HOLD_SCROLL_MS`); then the page is the reader's again.
 */
export function holdPageScroll(from: Element | null): void {
  const scroller = from?.closest<HTMLElement>('.ctl-scroll') ?? null
  if (scroller === null) return
  const top = scroller.scrollTop
  const keep = () => {
    if (scroller.scrollTop !== top) scroller.scrollTop = top
  }
  scroller.addEventListener('scroll', keep)
  setTimeout(() => scroller.removeEventListener('scroll', keep), HOLD_SCROLL_MS)
}

export function AgentSplit({
  taskId,
  pane,
  artifact,
  file = null,
  closeTo,
  go,
  base,
}: {
  taskId: string
  pane: TaskPane
  /** The output the address names in the Artifacts pane, or null. */
  artifact: string | null
  /** The file the address names in the Changes pane, or null for the patch's first. */
  file?: string | null
  /**
   * The list address this split was opened from (OV-10), so closing it
   * restores the address the list behind it is showing.
   */
  closeTo: string
  go: (to: string) => void
  /** This agent's address, `work/task/<id>`: every tab is a segment under it. */
  base: string
}) {
  const close = () => go(closeTo)
  // Opening an output writes its name into the address; closing it takes it out.
  const openArtifact = useCallback(
    (name: string | null) => go(name === null ? `${base}/artifacts` : `${base}/artifacts/${encodeURIComponent(name)}`),
    [base, go],
  )
  const snap = useListSnap()
  const [dragAt, setDragAt] = useState<ListSnap | null>(null)
  const dragging = useRef(false)
  const panel = useRef<HTMLDivElement>(null)
  /** The element that was focused when this opened. Where Escape puts you back. */
  const opener = useRef<Element | null>(null)
  /** What held focus when this first rendered, and which agent's row it was in. */
  const born = useRef<{ el: Element | null; rowId: string | null } | null>(null)
  if (born.current === null) {
    const a = typeof document === 'undefined' ? null : document.activeElement
    born.current = {
      el: a,
      rowId: a instanceof HTMLElement ? (a.closest('[data-task-id]')?.getAttribute('data-task-id') ?? null) : null,
    }
  }

  // THE CHECKPOINTS TAB'S COUNT IS ITS PANE'S READ (U10a D21, owner QA
  // 2026-10-04, and again in U11a): the tab said `–` until it was opened,
  // then `0`, while Details said `1 written · 0 in bucket`. The pane is
  // mounted with the split, hidden until its tab is chosen, so its one read
  // is both the count and the content from the start; the count is what the
  // bucket keeps, and its reason says what was written beside it in the
  // words Details uses: `1 written, 0 kept`.
  const [ckpts, setCkpts] = useState<{ taskId: string; c: CheckpointCount } | null>(null)
  const onCheckpoints = useCallback((c: CheckpointCount | null) => setCkpts(c === null ? null : { taskId, c }), [taskId])
  const ckpt = ckpts !== null && ckpts.taskId === taskId ? ckpts.c : null
  // KEPT OF WRITTEN WHEN THE TWO DIFFER (G2-12, QA 2026-10-07): on a
  // succeeded review the tab said `0` while Details and Attempts said 3, the
  // three having been reclaimed once the task finished. `0 of 3` says both.
  // Only when fewer are kept than written: a bucket holding more than the
  // records name (records not yet written, or older ones) keeps its count.
  const ckptCount: number | string | null =
    ckpt === null ? null : ckpt.kept !== null && ckpt.written !== null && ckpt.kept < ckpt.written ? `${ckpt.kept} of ${ckpt.written}` : ckpt.kept

  // THE HEADER'S OWN READ of the task document, at the detail's cadence, so
  // the state pill, the name and Stop are there on every tab -- not only on
  // Details, whose read is a different, larger one.
  const [reads, setReads] = useState(0)
  const head = useRead<Task>(() => loadTask(taskId), taskId, `${reads}`, null)
  const task = head.state.status === 'ok' || head.state.status === 'stale' ? head.state.data : null
  const live = task !== null && !TERMINAL_STATES.has(task.state)
  useEffect(() => {
    if (task !== null && !live) return
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      setReads((n) => n + 1)
    }, DRAWER_POLL_MS)
    return () => clearInterval(id)
  }, [live, task === null])
  const reload = useCallback(() => setReads((n) => n + 1), [])
  const childCount = useChildCount(task, `${reads}`)
  const headReadAt = head.state.status === 'ok' || head.state.status === 'stale' ? head.state.fetchedAt : null

  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--list-w', SNAP_WIDTH[snap])
    root.dataset.agentList = snap
    return () => {
      root.style.removeProperty('--list-w')
      delete root.dataset.agentList
    }
  }, [snap])

  // OPENING CHANGES FOLDS THE LIST TO ITS STRIP (diff-viewer.md §2 variant 2:
  // at 1280 that gives the file about 850px, and split needs 700). REMEMBERED,
  // NOT FORCED: the fold is not written to the device (`remember` false), a
  // reader who opens the list again with `[` or the divider keeps it open, and
  // leaving the tab puts back the width the list had -- unless the reader
  // moved it while on Changes, which is then theirs.
  const onChanges = pane === 'changes'
  useEffect(() => {
    if (!onChanges) return
    const was = listSnap()
    if (was === 'strip') return
    setListSnap('strip', false)
    return () => {
      if (listSnap() === 'strip') setListSnap(was, false)
    }
  }, [onChanges])

  // `[` folds the list to the strip and back, as `«`/`»` in the list header does.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '[' || e.metaKey || e.ctrlKey || e.altKey) return
      const el = e.target as HTMLElement | null
      if (el !== null && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return
      toggleListSnap()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // ESCAPE CLOSES THE SPLIT WHEREVER FOCUS IS (V152, visual QA #1038). The
  // panel's own `onKeyDown` only hears keys pressed inside it, and a mouse
  // click on a row opens the split beside the list WITHOUT moving focus (it
  // moves in only over an overlay, below), so the key went to the row and
  // nothing closed. This listener takes the key from anywhere outside the
  // panel, in the bubble phase: a layer above (`escape.ts`, capture phase), an
  // open help card, a select, a menu or flyout, a text field and any other
  // dialog all keep their own Escape, exactly as they do inside the panel.
  const closeRef = useRef(close)
  closeRef.current = close
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return
      if (document.querySelector('[data-focus-return]') !== null) return
      const t = e.target instanceof Element ? e.target : null
      if (t !== null && panel.current?.contains(t)) return
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (t?.closest('.sk-flyout, .sk-spine, .c-menu-host, [role="dialog"], [role="menu"], details[open]')) return
      closeRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /*
   * FOCUS, ON THE WAY IN AND ON THE WAY OUT (moved with the split; see the
   * history of App.tsx `AgentDrawer`). Focus moves in only when the panel
   * covers the list; on the way out it goes back to what opened it, or to the
   * row of the agent it showed, found again by id because the list draws a
   * different row node while an agent is open.
   */
  useEffect(() => {
    const el = panel.current
    const { el: came, rowId } = born.current ?? { el: null, rowId: null }
    opener.current = came
    if (el !== null && isOverlay(el)) el.focus()
    return () => {
      const back = opener.current
      opener.current = null
      if (back instanceof HTMLElement && back.isConnected && back !== document.body) {
        back.focus()
        return
      }
      if (rowId !== null) {
        const again = [...document.querySelectorAll<HTMLElement>('.row.clickable[data-task-id]')].find(
          (r) => r.getAttribute('data-task-id') === rowId,
        )
        again?.focus()
      }
    }
    // Once per open. `taskId` changing swaps the CONTENTS of an open split and
    // must not re-capture an opener that is now inside it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return
    // The list's width is the distance from the work area's left edge to the
    // pointer, snapped to the nearest stop -- and the stop lights up.
    const app = panel.current?.closest('.app')?.getBoundingClientRect()
    if (app === undefined) return
    const next = nearestSnap(e.clientX - app.left, app.width)
    setDragAt(next)
    setListSnap(next, false)
  }
  const endDrag = () => {
    if (!dragging.current) return
    dragging.current = false
    setListSnap(dragAt ?? snap)
    setDragAt(null)
  }

  /*
   * A RUNNING AGENT OPENS ON LOGS, A FINISHED ONE ON DETAILS (owner decision
   * 2026-10-04). Decided once per agent, on the first read of its document,
   * and only when the address named no pane: a link to `/attempts` stays
   * there, and a reader who then picks Details is not sent back to Logs.
   * Until that read lands nothing is drawn in the pane, so a running agent's
   * Details are not read and thrown away on the way to its log.
   */
  // "Running" is an attempt in flight -- LEASED/DISPATCHED/STARTING/RUNNING
  // (CONTRACT.md invariant 1). A QUEUED, READY or PARKED agent has no
  // attempt and usually no log yet; its Details say why it waits.
  const inFlight = task !== null && CONCURRENCY_STATES.has(task.state)
  const [decided, setDecided] = useState<string | null>(null)
  const deciding = decided !== taskId && pane === 'detail' && (head.state.status === 'loading' || inFlight)
  useEffect(() => {
    if (decided === taskId) return
    if (pane !== 'detail') {
      setDecided(taskId)
      return
    }
    if (head.state.status === 'loading') return
    setDecided(taskId)
    if (inFlight) go(`${base}/logs`)
  }, [decided, taskId, pane, head.state.status, inFlight, go, base])

  const tabs: { id: TaskPane; label: string; count: number | string | null; say: string | null; to: string }[] = [
    { id: 'detail', label: 'Details', count: null, say: null, to: base },
    { id: 'logs', label: 'Logs', count: null, say: null, to: `${base}/logs` },
    // THE COUNT IS THE FILE COUNT (`git.files`, DIFF4), and a dash with its
    // reason where the summary does not say it -- never a 0 nobody measured.
    { id: 'changes', label: 'Changes', ...changesCount(task), to: `${base}/changes` },
    // CHILDREN HAS AN ADDRESS (U10a D36): `<agent>/children`, as every
    // other pane has one, so the tab moves the URL and a link can open it.
    ...(task !== null && offersChildren(task)
      ? [
          {
            id: 'children' as const,
            label: 'Children',
            count: childCount,
            say: childCount === null ? 'The children read has not answered, so their count is not known.' : null,
            to: `${base}/children`,
          },
        ]
      : []),
    {
      id: 'attempts',
      label: 'Attempts',
      count: task?.attempt_count ?? null,
      say: task === null ? 'The task has not been read yet.' : null,
      to: `${base}/attempts`,
    },
    {
      id: 'artifacts',
      label: 'Artifacts',
      count: artifactCount(task),
      say:
        artifactCount(task) === null
          ? 'Files are listed when an attempt ends: this task has no result summary yet, so the count is not known.'
          : null,
      to: `${base}/artifacts`,
    },
    {
      id: 'checkpoints',
      label: 'Checkpoints',
      count: ckptCount,
      say: checkpointsSay(ckpt),
      to: `${base}/checkpoints`,
    },
  ]
  const selected: TaskPane = pane
  // The file the reader opens is written into the address, so Back moves the viewer and a link names the file.
  const openFile = useCallback((path: string) => go(`${base}/changes/${encodeURIComponent(path)}`), [base, go])
  const links = useMemo(
    () => ({
      logs: () => go(`${base}/logs`),
      attempts: () => go(`${base}/attempts`),
      artifacts: () => go(`${base}/artifacts`),
      changes: () => go(`${base}/changes`),
    }),
    [base, go],
  )

  return (
    <div
      className={`drawer ctl-drawer ag-split${selected === 'logs' ? ' on-logs' : ''}${selected === 'changes' ? ' on-changes' : ''}`}
      role="dialog"
      aria-label={`Agent ${taskId}`}
      ref={panel}
      // -1, so the panel is a legal destination for `.focus()` when it opens
      // over the list and is NOT a stop Tab lands on afterwards.
      tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          // An open help card inside stops Escape before it reaches here
          // (see `HelpCard.tsx`), so the innermost open thing closes.
          e.stopPropagation()
          close()
          return
        }
        if (e.key === 'Tab' && panel.current !== null && isOverlay(panel.current)) {
          trapTab(e, panel.current)
        }
      }}
    >
      <div
        className={`ctl-inspector-grip ag-divider${dragAt !== null ? ' is-dragging' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the agent list"
        /*
         * THE WAI-ARIA WINDOW-SPLITTER PATTERN: a focusable separator that
         * moves on the arrow keys and reports its position. RIGHT WIDENS THE
         * LIST, which is what dragging the divider right does.
         *
         * A HANDLE YOU CAN SEE (#503): this was a transparent 6px grip, so the
         * one control that resizes the list was found only by hovering the
         * right pixel. V1 draws a handle on the divider; `.ag-divider::after`
         * is it, and while it is dragged the three stops are drawn with the
         * nearest one lit.
         */
        tabIndex={0}
        aria-valuenow={SNAPS.indexOf(snap)}
        aria-valuemin={0}
        aria-valuemax={SNAPS.length - 1}
        aria-valuetext={SNAP_TEXT[snap]}
        onKeyDown={(e) => {
          const i = SNAPS.indexOf(snap)
          const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
          if (step === 0) return
          e.preventDefault()
          holdPageScroll(panel.current)
          setListSnap(SNAPS[Math.max(0, Math.min(SNAPS.length - 1, i + step))]!)
        }}
        onPointerDown={(e) => {
          dragging.current = true
          setDragAt(snap)
          e.currentTarget.setPointerCapture?.(e.pointerId)
        }}
        onPointerMove={onMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      />
      {dragAt !== null && (
        <div className="ag-snaps" role="status" aria-live="polite">
          {SNAPS.map((s) => (
            <span key={s} className={`ag-snap${s === dragAt ? ' is-on' : ''}`}>
              {SNAP_LABEL[s]}
            </span>
          ))}
          <span className="ag-snap-say">release to snap to {SNAP_LABEL[dragAt].split(' · ')[0]}</span>
        </div>
      )}
      {/* THE WAY BACK TO THE LIST: on a phone the list and the agent are two
          pages, and this is the agent page's back link. Shown wherever the
          agent covers the list (below 1100px). */}
      <button type="button" className="ctl-agent-back" onClick={close} aria-label={`Back to ${backLabel(closeTo)}`}>
        ‹ {backLabel(closeTo)}
      </button>
      {/* THE ✕ IS IN THE HEADER'S ACTION ROW, after Copy link (walkthrough
          B, 2026-10-03): a sticky band of its own on the column, it was
          drawn over Copy link. */}
      <SplitTick live={live} readAt={headReadAt}>
        <AgHead
          taskId={taskId}
          task={task}
          read={head.state.status}
          reload={reload}
          onClose={close}
        />

        {/* UNDERLINE TABS WITH COUNTS (agents.html V1; #503 measured a boxed
            segmented control with none). A count the task document does not
            carry is a dash with its reason in the title, never a 0. The ids and
            addresses are unchanged: `detail` is still `/agents/<tab>/<id>`. */}
        <AgTabsEdge current={selected}>
          <Tabs
            className="ag-split-tabs"
            label="Agent panes"
            current={selected}
            tabs={tabs.map((t) => ({
              key: t.id,
              label: t.label,
              ...(t.id === 'detail' || t.id === 'logs' ? {} : { count: t.count, why: t.say ?? undefined }),
            }))}
            onSelect={(key) => go(tabs.find((x) => x.id === key)!.to)}
          />
        </AgTabsEdge>

        <div className={`ag-split-pane${selected === 'logs' ? ' is-logs' : ''}${selected === 'changes' ? ' is-changes' : ''}`}>
          {deciding ? (
            <p className="art-loading">
              <Mark kind="pending" say="Reading the agent to open it on its log if it is running, or on its details. The read is in flight." />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : selected === 'logs' && task !== null ? (
            <AgentLogs key={taskId} task={task} />
          ) : selected === 'logs' ? (
            <p className="art-loading">
              <Mark kind="pending" say="Reading the agent before its log. The read is in flight." />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : selected === 'changes' && task !== null ? (
            <AgentChangesPane key={taskId} task={task} file={file} onFile={openFile} />
          ) : selected === 'changes' ? (
            <p className="art-loading">
              <Mark kind="pending" say="Reading the agent before its changes. The read is in flight." />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : selected === 'children' && task !== null ? (
            <AgChildrenPane task={task} readKey={`${reads}`} />
          ) : selected === 'children' ? (
            <p className="art-loading">
              <Mark kind="pending" say="Reading the agent before its children. The read is in flight." />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : selected === 'detail' ? (
            // THE LAST LOG LINE IS IN THE LEADING CARD (agent-details-v3.html
            // A): a lone row above the body, it was disconnected from the state
            // it explains. The split draws it, since only the split can switch
            // to Logs; the card places it.
            <AgentDetailScreen
              taskId={taskId}
              onClose={close}
              headed
              checkpoints={ckpt}
              links={links}
              lastLine={task !== null ? <LogLastLine key={taskId} task={task} onOpen={links.logs} /> : undefined}
            />
          ) : selected === 'attempts' ? (
            <AttemptTimelineScreen taskId={taskId} />
          ) : selected === 'checkpoints' ? null : (
            <ArtifactsScreen taskId={taskId} open={artifact} onOpen={openArtifact} />
          )}
          {/* Mounted with the split, shown on its tab (D21, above). */}
          <div className="ag-ckpts-host" hidden={selected !== 'checkpoints'}>
            <CheckpointsPane key={taskId} taskId={taskId} onCount={onCheckpoints} />
          </div>
        </div>
      </SplitTick>
    </div>
  )
}

/**
 * THE SPLIT'S ONE INSTANT (G2-03, QA 2026-10-07). One running task showed
 * `17m 33s` in the header and `17m 39s` on the Elapsed tile: one 1s clock,
 * capped by two different reads (the header's and the Details pane's), so
 * the two figures stopped at different instants. It is taken once here --
 * ticking every second, and stopping one poll past the header's read
 * (`rowClock`), so a failed re-read never ages a live agent -- and the
 * header and the Details tab both read it (`SplitClock`).
 *
 * A COMPONENT OF ITS OWN so the tick re-renders only what reads the clock:
 * its children are the split's elements, unchanged between ticks, so React
 * keeps them and only the context's readers draw again. Null until the
 * header's read lands, so the Details tab keeps its own clock until then.
 */
function SplitTick({ live, readAt, children }: { live: boolean; readAt: number | null; children: ReactNode }) {
  const clock = useNow(1000)
  const now = readAt === null ? null : live ? rowClock(clock, readAt, DRAWER_POLL_MS) : clock
  return <SplitClock.Provider value={now}>{children}</SplitClock.Provider>
}

/**
 * THE TABS SAY WHEN THERE ARE MORE OF THEM (U10a D39, owner QA at 390px,
 * 2026-10-04): on a phone the strip was cut at `Che…` and nothing said it
 * scrolls. The strip still scrolls sideways (Q4); this marks which of its
 * ends has more beyond it, `data-more="start end"`, and the sheet fades that
 * edge with a chevron. Measured on scroll and on resize, never guessed.
 */
function AgTabsEdge({ children, current }: { children: ReactNode; current?: string }) {
  const box = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState('')
  // THE OPEN TAB IS ALWAYS IN VIEW (V148): on a phone the strip is one row
  // that scrolls, so a tab chosen by address (`/changes`, `/checkpoints`) can
  // sit past the edge. The STRIP is scrolled, never the page:
  // `scrollIntoView` would move every scroller above it as well.
  useEffect(() => {
    const strip = box.current?.querySelector<HTMLElement>('.c-tabs')
    const on = strip?.querySelector<HTMLElement>('[aria-selected="true"], [aria-current="page"]')
    if (strip === null || strip === undefined || on === null || on === undefined) return
    const s = strip.getBoundingClientRect()
    const t = on.getBoundingClientRect()
    if (t.left < s.left) strip.scrollLeft -= s.left - t.left
    else if (t.right > s.right) strip.scrollLeft += t.right - s.right
  }, [current])
  useEffect(() => {
    const strip = box.current?.querySelector<HTMLElement>('.c-tabs')
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
    return () => {
      strip.removeEventListener('scroll', measure)
      globalThis.removeEventListener?.('resize', measure)
      seen?.disconnect()
    }
  }, [])
  return (
    <div className="ag-tabs-edge" ref={box} data-more={more || undefined}>
      {children}
    </div>
  )
}

/** The Artifacts tab's count: the manifest's files once a summary is written, else unknown. */
function artifactCount(task: Task | null): number | null {
  const a = (task?.result_summary as Record<string, unknown> | null | undefined)?.artifacts
  return Array.isArray(a) ? a.length : null
}

const COPY_SAID_MS = 4000

/**
 * THE DETAIL'S HEADER, IN TWO LINES (agent-details-v3.html A, picked
 * 2026-10-04). Line 1: the state pill, the agent's name -- its step, or what
 * it is when it stands alone -- the headline `elapsed · attempt n of N` in
 * sans, and Copy link, Stop and ✕. Under it the whole task id, printed in
 * mono with its copy button (`TaskIdLine`, #94 -- it was only the button's
 * tooltip). Then one sans meta line -- profile · class · workflow · account --
 * and the dispatch as plain-language chips.
 *
 * WHAT LEFT IT, AND WHERE TO: the tenant
 * (the panel's tenant tile says it), gen and units (the Attempts tab), and
 * started (the strip's Elapsed sub-line). The actions share line 1 again:
 * the title takes the slack and clamps at two lines, and the headline and
 * the actions never shrink, so the ✕ cannot be drawn over Stop (the defect
 * lane U9 moved them to a row of their own for). A child carries its
 * parent's link above line 1 (agent-detail-2.html A2).
 */
function AgHead({
  taskId,
  task,
  read,
  reload,
  onClose,
}: {
  taskId: string
  task: Task | null
  read: string
  reload: () => void
  onClose: () => void
}) {
  // THE SPLIT'S ONE INSTANT (`SplitTick`), which the Details tab draws its
  // running figures at too; the bare clock only outside a split.
  const clock = useNow(1000)
  const now = useContext(SplitClock) ?? clock
  const [copied, setCopied] = useState<'yes' | 'no' | null>(null)
  useEffect(() => {
    if (copied === null) return
    const id = setTimeout(() => setCopied(null), COPY_SAID_MS)
    return () => clearTimeout(id)
  }, [copied])
  const copy = () => {
    const href = typeof window === 'undefined' ? '' : window.location.href
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined) {
      setCopied('no')
      return
    }
    clip.writeText(href).then(
      () => setCopied('yes'),
      () => setCopied('no'),
    )
  }
  const name = task === null ? taskId : agentName(task)
  // The breadcrumb has the id and no task: it names the agent with this.
  useEffect(() => {
    if (task !== null) rememberAgentName(task)
  }, [task])
  return (
    <header className="ag-head">
      {task !== null && <AgParentLink task={task} />}
      <div className="ag-head-row">
        {task !== null ? (
          <StateMark state={task.state} />
        ) : (
          <span className="ag-head-state">{read === 'error' ? 'not read' : 'reading'}</span>
        )}
        {/* TWO LINES, THEN AN ELLIPSIS, AND THE WHOLE NAME IN THE TOOLTIP
            (walkthrough C): a long step name ran off the column. */}
        <h2 className="ag-head-title" title={name}>
          {name}
        </h2>
        {task !== null && <AgHeadline task={task} now={now} />}
        <span className="ag-head-actions">
          <Button onClick={copy}>Copy link</Button>
          {copied !== null && (
            <span role="status" className="ag-head-copied">
              {copied === 'yes' ? 'copied' : 'could not copy'}
            </span>
          )}
          {task !== null && (
            <span className="ag-head-stop">
              <StopRun task={task} what="this agent" reload={reload} />
            </span>
          )}
          <Button iconOnly icon={<CIcon name="close" />} className="drawer-close ag-head-close" onClick={onClose}>
            Close
          </Button>
        </span>
      </div>
      {/* UNDER THE NAME, THE WHOLE ID, PRINTED (#94): it was only the copy
          button's tooltip, so it had to be hovered to be read. */}
      <TaskIdLine id={taskId} className="ag-head-id" />
      <AgHeadMeta task={task} />
    </header>
  )
}

/**
 * `elapsed · attempt n of N`, and when it ended once it has. `elapsed()` is
 * the Agents list's own figure, so the row and the header never disagree; the
 * attempt is the task's own counter, which the header's read carries.
 */
function AgHeadline({ task, now }: { task: Task; now: number }) {
  const el = elapsed(task, now)
  const ended = clockTime(task.completed_at, now)
  return (
    <span className="ag-head-hl">
      <b>{el.text}</b> ·{' '}
      {task.attempt_count > 0 ? `attempt ${task.attempt_count} of ${task.max_attempts}` : 'no attempt yet'}
      {ended !== null && (
        <span title={`ended ${ended.title}`}> · ended {ended.text}</span>
      )}
    </span>
  )
}

/** What each role does, in the words of the dispatch chips. */
const ROLE_SAY: Readonly<Record<DispatchRole, string>> = {
  contributor: 'adds to the shared PR',
  integrator: 'opens the shared PR',
}

/**
 * Profile · class (with its size, once the catalogue answers) · workflow ·
 * account, and the dispatch chips. Sans; mono only for the ids. An account
 * not read is a dash with its reason, never blank. The task id is the line
 * above this one (`TaskIdLine`).
 */
export function AgHeadMeta({ task }: { task: Task | null }) {
  const classes = useResourceClasses()
  const cls = task === null ? null : (classes?.[task.resource_class] ?? null)
  const account = task === null ? null : accountText(task.account)
  const d = task === null ? null : dispatchOf(task)
  // A WRAPPED LINE NEVER STARTS WITH A MIDDOT (G2-20, QA 2026-10-07). The
  // separator was each fact's `::before`, so on a phone a fact that wrapped
  // took its dot to the head of the next line (`· workflow wf_…`). It is the
  // `::after` of every fact but the last now, so it stays at the end of the
  // line it separates on.
  const facts: { key: string; li: ReactNode; className?: string; title?: string }[] =
    task === null
      ? []
      : [
          { key: 'profile', li: <b>{task.runner_profile}</b> },
          {
            key: 'class',
            title: cls === null ? 'The class catalogue has not answered, so its size is not shown.' : undefined,
            li: (
              <>
                {task.resource_class}
                {cls !== null && ` · ${cls.cpu} vCPU · ${cls.memory_gib} GiB`}
              </>
            ),
          },
          ...(task.workflow_id === null
            ? []
            : [
                {
                  key: 'workflow',
                  li: (
                    <>
                      workflow{' '}
                      <a className="ctl-link mono" href={workflowHref(task.workflow_id)}>
                        {task.workflow_id} ›
                      </a>
                    </>
                  ),
                },
              ]),
          ...(account === null
            ? []
            : [
                {
                  key: 'account',
                  className: account.known ? undefined : 'is-absent',
                  title: account.title,
                  // AN ID IS MONO, A SENTENCE IS NOT (V149, visual QA #1038):
                  // `account not read` was bold mono in a sans line, drawn as
                  // if `not read` were an account's name. Only a real account
                  // id takes the id face; a status reads as the line's words.
                  li: account.known ? (
                    <>
                      account <b className="mono">{account.text}</b>
                    </>
                  ) : (
                    <>account {account.text}</>
                  ),
                },
              ]),
        ]
  return (
    <ul className="ag-head-facts">
      {facts.map((f, i) => (
        <li key={f.key} className={f.className} title={f.title} data-sep={i < facts.length - 1 ? '' : undefined}>
          {f.li}
        </li>
      ))}
      {task !== null && (
        // ONE `?` FOR THE DISPATCH CHIPS, after them: what the strategy and
        // the role mean is one topic. The carrier gets no chip -- no worker
        // reads it yet (`CARRIER_NOTE`) -- and the topic names it.
        <li className="is-nodot ag-head-dispatch">
          {d !== null && d.role !== null && <span className="ag-head-dchip">{STRATEGY_LABEL[d.strategy]}</span>}
          {/* THE `?` NEVER SITS ALONE ON A LINE (G2-20): it is held to the
              last chip, so where the chips wrap it goes with one. And it is
              `strategy-on-a-task`, not the Submit form's
              `dispatch-strategies`, which spoke of a number computed from the
              steps on the form (G2-10). */}
          <span className="ag-head-dlast">
            {d === null ? (
              <span className="ag-head-dchip is-absent">dispatch not served</span>
            ) : (
              <span className="ag-head-dchip">{d.role !== null ? ROLE_SAY[d.role] : STRATEGY_LABEL[d.strategy]}</span>
            )}
            <HelpCard topic={d === null ? 'dispatch-absent-is-old-api' : 'strategy-on-a-task'} />
          </span>
        </li>
      )}
    </ul>
  )
}
