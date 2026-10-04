import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'

import { AgChildrenPane, AgParentLink, offersChildren, useChildCount } from './AgentChildren'
import { AgentDetailScreen, DRAWER_POLL_MS, IdCopy, Mark } from './AgentDetail'
import { agentName, backLabel, rememberAgentName, workflowHref } from './agentlist'
import { loadTask } from './api'
import type { TaskPane } from './App'
import { ArtifactsScreen } from './Artifacts'
import { AttemptTimelineScreen } from './AttemptTimeline'
import { CheckpointsPane } from './CheckpointsPane'
import { isOverlay, trapTab } from './focus'
import { phoneWidth } from './HelpCard'
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
import { LogDock } from './LogDock'
import { useRead } from './RunFiles'
import { StopRun } from './StopRun'
import { RESOURCE_UNITS, TERMINAL_STATES, accountText, clockTime, startedOf, type Task } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/agents.css'
import { Button, CIcon, StateMark, Tabs } from './components'

/**
 * ONE AGENT, IN THE SPLIT (agents.html V1, decided 2026-10-01; viewers.html A
 * and agent-detail-2.html A, picked 2026-10-02).
 *
 * The list on the left, the selected agent on the right: a header row (state
 * pill, the agent's name, Copy link, Stop), underline tabs with counts --
 * Details, Children when the agent has any, Attempts, Artifacts, Checkpoints
 * -- the open tab, and the log docked along the bottom of this column, open
 * across every tab.
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

  // THE CHECKPOINTS TAB'S COUNT IS THE LISTING'S (U10a D21, owner QA
  // 2026-10-04): the tab said `–` while its pane said `found 1 of 1`. The
  // task document does not count checkpoints, so the count is known once the
  // pane has read the listing, and it is that read's number, for this agent.
  const [ckpts, setCkpts] = useState<{ taskId: string; n: number } | null>(null)
  const onCheckpoints = useCallback((n: number | null) => setCkpts(n === null ? null : { taskId, n }), [taskId])
  const ckptCount = ckpts !== null && ckpts.taskId === taskId ? ckpts.n : null

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

  const phone = phoneWidth()
  const tabs: { id: TaskPane; label: string; count: number | null; say: string | null; to: string }[] = [
    { id: 'detail', label: 'Details', count: null, say: null, to: base },
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
      say:
        ckptCount === null
          ? 'The task document does not count checkpoints: the count is the listing’s, once the Checkpoints tab has read it.'
          : null,
      to: `${base}/checkpoints`,
    },
  ]
  const selected: TaskPane = pane

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
      <AgHead taskId={taskId} task={task} read={head.state.status} reload={reload} onClose={close} />

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
            ...(t.id === 'detail' ? {} : { count: t.count, why: t.say ?? undefined }),
          }))}
          onSelect={(key) => go(tabs.find((x) => x.id === key)!.to)}
        />
      </AgTabsEdge>

      <div className="ag-split-pane">
        {selected === 'children' && task !== null ? (
          <AgChildrenPane task={task} readKey={`${reads}`} />
        ) : selected === 'children' ? (
          <p className="art-loading">
            <Mark kind="pending" say="Reading the agent before its children. The read is in flight." />
            <span className="ctl-pending art-loading-bar" />
          </p>
        ) : selected === 'detail' ? (
          <AgentDetailScreen taskId={taskId} onClose={close} headed />
        ) : selected === 'attempts' ? (
          <AttemptTimelineScreen taskId={taskId} />
        ) : selected === 'checkpoints' ? (
          <CheckpointsPane taskId={taskId} onCount={onCheckpoints} />
        ) : (
          <ArtifactsScreen taskId={taskId} open={artifact} onOpen={openArtifact} />
        )}
      </div>

      {task !== null && <LogDock key={taskId} task={task} phone={phone} />}
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
 * THE DETAIL'S HEADER BLOCK (agents.html V1), AND THE ONE PLACE ITS
 * METADATA IS SAID (walkthrough B, owner 2026-10-03). The state pill, the
 * agent's name -- its step, or what it is when it stands alone -- and Copy
 * link, Stop and ✕ in the action row; under it the whole id with its copy,
 * profile · class · units · gen, and the facts: started / ended, the account,
 * the tenant and the workflow. The Details pane said these again in two other
 * shapes; it now draws only what this does not (`Headline` with `headed`).
 * A child carries its parent's link above the title (agent-detail-2.html A2).
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
  const now = useNow(AGE_TICK_MS)
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
  const units = task === null ? undefined : RESOURCE_UNITS[task.resource_class]
  const name = task === null ? taskId : agentName(task)
  // The breadcrumb has the id and no task: it names the agent with this.
  useEffect(() => {
    if (task !== null) rememberAgentName(task)
  }, [task])
  return (
    <header className="ag-head">
      {/* THE ACTIONS HAVE A ROW OF THEIR OWN (lane U9 item 2, owner
          2026-10-03): sharing the title's line, the ✕ was drawn over Stop on
          a running agent. The parent link, when there is one, takes the
          row's left; the actions are packed to its right and wrap. */}
      <div className="ag-head-bar">
        {task !== null && <AgParentLink task={task} />}
        <span className="ag-head-actions">
          <Button onClick={copy}>
            Copy link
          </Button>
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
      </div>
      <p className="ag-head-id">
        <IdCopy value={taskId} />
      </p>
      {task !== null && <AgHeadFacts task={task} units={units} now={now} />}
    </header>
  )
}

/**
 * Profile · class · units · gen, then when it started and ended, the account,
 * the tenant and the workflow -- each keyed once, in the facts-strip treatment
 * the Details pane uses, with the same formatters (`startedOf`, `clockTime`,
 * `accountText`) so the header and the attempt cards never print one instant
 * two ways. An account not read is a dash with its reason, never blank.
 */
function AgHeadFacts({ task, units, now }: { task: Task; units: number | undefined; now: number }) {
  const start = startedOf(task, now)
  const ended = clockTime(task.completed_at, now)
  const account = accountText(task.account)
  return (
    <ul className="ctl-facts ag-head-facts">
      <li className="ctl-fact ag-head-shape mono">
        {task.runner_profile} · {task.resource_class}
        {units !== undefined && ` · ${units}u`}
        {task.current_generation !== null && task.current_generation !== undefined && ` · gen ${task.current_generation}`}
      </li>
      <li className="ctl-fact" title={start.title}>
        <b>started</b>
        {start.text}
      </li>
      {ended !== null && (
        <li className="ctl-fact" title={`ended ${ended.title}`}>
          <b>ended</b>
          {ended.text}
        </li>
      )}
      <li className={`ctl-fact${account.known ? '' : ' is-absent'}`} title={account.title}>
        <b>account</b>
        <span className="mono">{account.text}</span>
      </li>
      <li className="ctl-fact">
        <b>tenant</b>
        <span className="mono">{task.tenant_id}</span>
      </li>
      {task.workflow_id !== null && (
        <li className="ctl-fact">
          <b>workflow</b>
          <a className="ctl-link mono" href={workflowHref(task.workflow_id)}>
            {task.workflow_id}
          </a>
        </li>
      )}
    </ul>
  )
}
