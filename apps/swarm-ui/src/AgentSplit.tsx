import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'

import { AgChildrenPane, AgParentLink, offersChildren, useChildCount } from './AgentChildren'
import { AgentDetailScreen, DRAWER_POLL_MS, Mark } from './AgentDetail'
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
  nearestSnap,
  setListSnap,
  toggleListSnap,
  useListSnap,
  type ListSnap,
} from './listSnap'
import { useRead } from './RunFiles'
import { StopRun } from './StopRun'
import { useResourceClasses } from './Blockers'
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
 * Details, Logs, Children when the agent has any, Attempts, Artifacts,
 * Checkpoints -- and the open tab, which fills the rest of the column.
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
export function AgentSplit({
  taskId,
  pane,
  artifact,
  closeTo,
  go,
  base,
}: {
  taskId: string
  pane: TaskPane
  /** The output the address names in the Artifacts pane, or null. */
  artifact: string | null
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
  const ckptCount = ckpt === null ? null : ckpt.kept

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

  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--list-w', SNAP_WIDTH[snap])
    root.dataset.agentList = snap
    return () => {
      root.style.removeProperty('--list-w')
      delete root.dataset.agentList
    }
  }, [snap])

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

  const tabs: { id: TaskPane; label: string; count: number | null; say: string | null; to: string }[] = [
    { id: 'detail', label: 'Details', count: null, say: null, to: base },
    { id: 'logs', label: 'Logs', count: null, say: null, to: `${base}/logs` },
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
  const links = useMemo(
    () => ({
      logs: () => go(`${base}/logs`),
      attempts: () => go(`${base}/attempts`),
      artifacts: () => go(`${base}/artifacts`),
    }),
    [base, go],
  )

  return (
    <div
      className="drawer ctl-drawer ag-split"
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
      <AgHead
        taskId={taskId}
        task={task}
        read={head.state.status}
        readAt={head.state.status === 'ok' || head.state.status === 'stale' ? head.state.fetchedAt : null}
        reload={reload}
        onClose={close}
      />

      {/* UNDERLINE TABS WITH COUNTS (agents.html V1; #503 measured a boxed
          segmented control with none). A count the task document does not
          carry is a dash with its reason in the title, never a 0. The ids and
          addresses are unchanged: `detail` is still `/agents/<tab>/<id>`. */}
      <AgTabsEdge>
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

      <div className={`ag-split-pane${selected === 'logs' ? ' is-logs' : ''}`}>
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
    </div>
  )
}

/**
 * THE TABS SAY WHEN THERE ARE MORE OF THEM (U10a D39, owner QA at 390px,
 * 2026-10-04): on a phone the strip was cut at `Che…` and nothing said it
 * scrolls. The strip still scrolls sideways (Q4); this marks which of its
 * ends has more beyond it, `data-more="start end"`, and the sheet fades that
 * edge with a chevron. Measured on scroll and on resize, never guessed.
 */
function AgTabsEdge({ children }: { children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState('')
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
 * sans, and Copy link, Stop and ✕. Line 2: one sans meta line -- profile ·
 * class · workflow · account -- the task id behind a copy button whose title
 * is the whole id, and the dispatch as plain-language chips.
 *
 * WHAT LEFT IT, AND WHERE TO: the printed id (behind `copy id`), the tenant
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
  readAt,
  reload,
  onClose,
}: {
  taskId: string
  task: Task | null
  read: string
  /** When the header's read landed, so the elapsed figure stops where the read stops vouching for it. */
  readAt: number | null
  reload: () => void
  onClose: () => void
}) {
  // THE HEADLINE TICKS EVERY SECOND, as the Agents list's elapsed column
  // does, and stops one poll past a read that has not been renewed
  // (`rowClock`): a failed re-read must not age a live agent into a long run.
  const clock = useNow(1000)
  const now = task !== null && !TERMINAL_STATES.has(task.state) ? rowClock(clock, readAt, DRAWER_POLL_MS) : clock
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
      <AgHeadMeta taskId={taskId} task={task} />
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
 * account, the id's copy button, and the dispatch chips. Sans; mono only for
 * the ids. An account not read is a dash with its reason, never blank.
 */
export function AgHeadMeta({ taskId, task }: { taskId: string; task: Task | null }) {
  const classes = useResourceClasses()
  const cls = task === null ? null : (classes?.[task.resource_class] ?? null)
  const account = task === null ? null : accountText(task.account)
  const d = task === null ? null : dispatchOf(task)
  return (
    <ul className="ag-head-facts">
      {task !== null && (
        <>
          <li>
            <b>{task.runner_profile}</b>
          </li>
          <li title={cls === null ? 'The class catalogue has not answered, so its size is not shown.' : undefined}>
            {task.resource_class}
            {cls !== null && ` · ${cls.cpu} vCPU · ${cls.memory_gib} GiB`}
          </li>
          {task.workflow_id !== null && (
            <li>
              workflow{' '}
              <a className="ctl-link mono" href={workflowHref(task.workflow_id)}>
                {task.workflow_id} ›
              </a>
            </li>
          )}
          {account !== null && (
            <li className={account.known ? undefined : 'is-absent'} title={account.title}>
              account <b className="mono">{account.text}</b>
            </li>
          )}
        </>
      )}
      <li className="is-nodot">
        <AgCopyId value={taskId} />
      </li>
      {task !== null && (
        // ONE `?` FOR THE DISPATCH CHIPS, after them: what the strategy and
        // the role mean is one topic. The carrier gets no chip -- no worker
        // reads it yet (`CARRIER_NOTE`) -- and the topic names it.
        <li className="is-nodot ag-head-dispatch">
          {d === null ? (
            <span className="ag-head-dchip is-absent">dispatch not served</span>
          ) : (
            <>
              <span className="ag-head-dchip">{STRATEGY_LABEL[d.strategy]}</span>
              {d.role !== null && <span className="ag-head-dchip">{ROLE_SAY[d.role]}</span>}
            </>
          )}
          <HelpCard topic={d === null ? 'dispatch-absent-is-old-api' : 'dispatch-strategies'} />
        </li>
      )}
    </ul>
  )
}

/**
 * THE TASK ID, BEHIND ITS COPY (agent-details-v3.html A). Not printed: the
 * button's title is the whole id, and the status says whether the copy
 * happened -- `navigator.clipboard` is undefined outside a secure context.
 */
function AgCopyId({ value }: { value: string }) {
  const [said, setSaid] = useState('')
  useEffect(() => {
    if (said === '') return
    const t = setTimeout(() => setSaid(''), COPY_SAID_MS)
    return () => clearTimeout(t)
  }, [said])
  const copy = () => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clipboard === undefined) {
      setSaid('copy refused')
      return
    }
    clipboard.writeText(value).then(
      () => setSaid('task id copied'),
      () => setSaid('copy refused'),
    )
  }
  return (
    <>
      <button
        type="button"
        className="ag-head-idcopy"
        title={`${value} (click to copy)`}
        aria-label={`Copy task id ${value}`}
        onClick={copy}
      >
        <CIcon name="copy" />
        copy id
      </button>
      <span className="ag-head-said" role="status">
        {said}
      </span>
    </>
  )
}
