import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { AgChildrenPane, useChildCount } from './AgentChildren'
import { AgentDetailScreen, DRAWER_POLL_MS, Mark, timesRun } from './AgentDetail'
import { AgentLogs, LogLastLine } from './AgentLogs'
import { backLabel, type AgentTab } from './agentlist'
import { agentPanelShown } from './Agents'
import { loadAttempts, loadTask } from './api'
import type { Result } from './fetch'
import type { TaskPane } from './App'
import { ArtifactsScreen } from './Artifacts'
import { AttemptTimelineScreen } from './AttemptTimeline'
import { CheckpointsPane } from './CheckpointsPane'
import { trapTab } from './focus'
import { toggleListSnap, useListSnap } from './listSnap'
import { useRead } from './RunFiles'
import { AgentChangesPane } from './WorkflowChanges'
import { isLiveBrowser, ownerQuestionsHold } from './Handoff'
import { PodStartProblem } from './PodStartProblem'
import { RecordingsPanel } from './Recordings'
import { LiveBrowserPane, useLiveSession } from './LiveBrowser'
import { CONCURRENCY_STATES, TERMINAL_STATES, type Task } from './types'
import { useCheckpointCount } from './agent-split/checkpoints'
import { SplitTick } from './agent-split/clock'
import { AgDivider, useListWidth } from './agent-split/divider'
import { isModal, useSplitFocus } from './agent-split/focus'
import { useListGroup } from './agent-split/group'
import { AgHandoff } from './agent-split/handoff'
import { AgHead } from './agent-split/head'
import { useOpeningPane } from './agent-split/opening'
import { AgTabsEdge, paneTabs } from './agent-split/tabs'
import './styles/agents.css'

/*
 * WHERE EACH PANEL LIVES (SPEED-D4A, owner decision 2026-10-14). This file
 * was 1,275 lines and a conflicted file in 3 of 31 hand-resolved merges:
 * parallel UI lanes each changing one panel edited the same hunks. It is
 * split by panel, one module each under `agent-split/`, and this file
 * composes them; a lane changing one panel edits that panel's module.
 *
 *   agent-split/head.tsx         the header: state, name, headline, Copy
 *                                link, Take control, Stop, ✕, id, meta line
 *   agent-split/tabs.tsx         the tab bar: each tab's name, which tabs
 *                                the agent offers with their counts, More
 *   agent-split/divider.tsx      the list's width, the Changes fold, and
 *                                the separator with its three stops
 *   agent-split/handoff.tsx      the hand-off banner over the pane
 *   agent-split/focus.ts         Escape from outside, modal over the list,
 *                                focus in and back out
 *   agent-split/group.ts         the list tab the agent's state puts it in
 *   agent-split/opening.ts       which pane a running or finished agent
 *                                opens on
 *   agent-split/checkpoints.ts   the Checkpoints tab's count
 *   agent-split/clock.tsx        the split's one instant (`SplitTick`)
 *
 * WHAT STAYS HERE, AND WHY: the reads (the task document and its
 * attempts), the `[` key and the panel's own Escape (qa.g1.help.test.tsx
 * holds the shortcut table to this file's handlers), and every pane's mount,
 * which source tests read from this file by name. The folder is also outside
 * the gates that scan `src/*.tsx` for who calls an API read, so a read moved
 * there would leave them. The exports are unchanged: what moved is
 * re-exported below, so no importer changed.
 */

/**
 * ONE AGENT, IN THE SPLIT (agents.html V1, decided 2026-10-01; viewers.html A
 * and agent-detail-2.html A, picked 2026-10-02).
 *
 * The list on the left, the selected agent on the right: a header row (state
 * pill, the agent's name, Copy link, Take control on a live browser, Stop),
 * underline tabs with counts -- Details, Browser for a live-browser agent,
 * Logs, Changes, Children when the agent has any, Attempts, Artifacts,
 * Checkpoints -- and the open tab, which fills the rest of the column.
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
 * nothing beneath it. An agent opens on Details -- a running browser agent
 * on Browser (#1646, 2026-10-19; a running agent opened on Logs from
 * 2026-10-04) -- and Details' one-line last log line switches to Logs.
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
export { HOLD_SCROLL_MS, holdPageScroll } from './agent-split/divider'
export { agentTabOf } from './agent-split/group'
export { AgHeadMeta } from './agent-split/head'
export { PANE_LABELS, fitTabs } from './agent-split/tabs'

export function AgentSplit({
  taskId,
  pane,
  artifact,
  file = null,
  theater = false,
  detailNamed = false,
  closeTo,
  go,
  onGroup,
  base,
}: {
  taskId: string
  pane: TaskPane
  /** The output the address names in the Artifacts pane, or null. */
  artifact: string | null
  /** The file the address names in the Changes pane, or null for the patch's first. */
  file?: string | null
  /** The address asked for the Browser tab in the theater (`/browser?theater=1`, live-browser.md §6). */
  theater?: boolean
  /**
   * The address named Details itself (`<agent>/details`, QA14-UI): the
   * running agent's default tab is for an address that named no pane, and
   * this one named one.
   */
  detailNamed?: boolean
  /**
   * The list address this split was opened from (OV-10), so closing it
   * restores the address the list behind it is showing.
   */
  closeTo: string
  go: (to: string) => void
  /**
   * Told the list tab the task's state puts it in, when that is not the tab
   * `closeTo` names (QA12-26): the router moves the list, the back link and
   * the address there.
   */
  onGroup?: (tab: AgentTab) => void
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
  const panel = useRef<HTMLDivElement>(null)

  const { ckpt, ckptCount, onCheckpoints } = useCheckpointCount(taskId)

  // THE HEADER'S OWN READ of the task document, at the detail's cadence, so
  // the state pill, the name and Stop are there on every tab -- not only on
  // Details, whose read is a different, larger one.
  const [reads, setReads] = useState(0)
  // The Browser tab's own re-reads of the task (LIVE-POLL, #1343): the head
  // only, on the live session's clock, not the children and counts `reads` keys.
  const [headReads, setHeadReads] = useState(0)
  const head = useRead<Task>(() => loadTask(taskId), taskId, `${reads}:${headReads}`, null)
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
  const childCount = useChildCount(task, `${reads}`, pane)
  // HOW MANY TIMES IT RAN (live QA WA-3): the attempt documents, read again
  // only when the task says something about its attempts changed -- its
  // generation, its counter or its state -- not on every header poll.
  const runsRead = useRead<{ attempts: unknown[] }>(
    () => readAttempts(taskId),
    taskId,
    task === null ? 'none' : `${task.current_generation}:${task.attempt_count}:${task.state}`,
    null,
  )
  const runs =
    task === null
      ? null
      : timesRun(
          task,
          runsRead.state.status === 'ok' || runsRead.state.status === 'stale'
            ? runsRead.state.data.attempts
            : runsRead.state.status === 'empty'
              ? []
              : null,
        )
  const headReadAt = head.state.status === 'ok' || head.state.status === 'stale' ? head.state.fetchedAt : null
  // THE LIST WAITS FOR THIS PANEL (load-speed investigation, the owner's pick
  // 2026-11-01): opened at its own address, the agent's task, attempts and
  // events are read first, and the list's first read starts once this panel
  // has rendered with its task and attempt reads answered (`listMayStart` in
  // Agents.tsx). The panes' event reads start on this panel's first render, so
  // they are under way before it. Told again when the agent closes.
  const panelDrawn = head.state.status !== 'loading' && runsRead.state.status !== 'loading'
  useEffect(() => {
    if (!panelDrawn) return
    agentPanelShown(taskId)
    return () => agentPanelShown(null)
  }, [panelDrawn, taskId])

  useListWidth(snap, pane === 'changes')

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

  useSplitFocus(panel, close, taskId)

  // THE TAB STRIP'S HEIGHT, MEASURED (AGENT-HEAD-B, BGD's pick AH1 B,
  // 2026-10-28, part of #2596). The column scrolls and the strip sticks at its
  // top, so Logs, Changes and Browser are one column less the strip tall and
  // Details' stuck figures sit at the strip's foot (agents.css, details.css).
  // A wrapped or More-fitted strip is not one row tall, so its height is read,
  // and read again whenever it changes, into `--ag-tabs-h` on the column.
  useEffect(() => {
    const col = panel.current
    const edge = col?.querySelector<HTMLElement>(':scope > .ag-tabs-edge') ?? null
    if (col === null || edge === null || typeof ResizeObserver === 'undefined') return
    const put = () => {
      const h = edge.getBoundingClientRect().height
      if (h > 0) col.style.setProperty('--ag-tabs-h', `${h}px`)
    }
    put()
    const watch = new ResizeObserver(put)
    watch.observe(edge)
    return () => watch.disconnect()
  }, [taskId])

  // "Running" is an attempt in flight -- LEASED/DISPATCHED/STARTING/RUNNING
  // (CONTRACT.md invariant 1). A QUEUED, READY or PARKED agent has no
  // attempt and usually no log yet; its Details say why it waits.
  const inFlight = task !== null && CONCURRENCY_STATES.has(task.state)
  useListGroup(task, taskId, closeTo, onGroup)
  // THE BROWSER TAB (docs/design/live-browser.md §6, LB-5): a running agent
  // whose profile is a live-browser one opens on its browser. Whether it is
  // one is the session read's `live_browser`, the catalogue's own property,
  // never a guess from the profile's name.
  // WHILE THE BROWSER TAB (OR ITS THEATER) IS ON SCREEN the session and the
  // task are read every LIVE_POLL_MS until the task is finished (#1343): the
  // header, the start-up and the strip follow the agent without a reload.
  const rereadHead = useCallback(() => setHeadReads((n) => n + 1), [])
  const { live: liveRead, reload: reloadLive } = useLiveSession(taskId, inFlight, {
    on: pane === 'browser',
    finished: task !== null && TERMINAL_STATES.has(task.state),
    onTask: rereadHead,
  })
  const liveSession = liveRead.status === 'ok' ? liveRead.session : null
  const sessionOpen = liveSession !== null && liveSession.state === 'open'
  // THE TAB SAYS LIVE ON A PICTURE, NOT ON THE READ ALONE (visual QA
  // ovag2-12, 2026-10-29): with the Browser pane open, an open session whose
  // screen has drawn no frame (the fixture build mints no ticket) carried
  // the badge over an empty frame. Off the pane, the session read is all
  // there is, and it says live as before.
  const [picture, setPicture] = useState(false)
  const sessionLive = sessionOpen && (pane !== 'browser' || picture)
  // THE TAB STAYS WHILE THE AGENT IS BETWEEN ATTEMPTS (LB-UX1): a parked
  // hand-off's "I'm ready to sign in" opens it on a READY task, which has no
  // attempt and so no session read, and the tab says the browser is
  // starting. A finished agent has none, unless its address names it.
  // A MERGE HELD BY THE OWNER'S QUESTIONS HAS NO BROWSER (#2313): it is a
  // git-only step, and the bell's HUMAN_REQUIRED item links every such park
  // to `/browser`. That address opens Details, where its questions are,
  // rather than drawing a Browser tab over a browser that never existed.
  const held = task !== null && ownerQuestionsHold(task) !== null
  const hasBrowser =
    (liveRead.status === 'ok' && liveRead.liveBrowser) ||
    (task !== null && !TERMINAL_STATES.has(task.state) && !inFlight && isLiveBrowser(task)) ||
    (pane === 'browser' && !held)
  useEffect(() => {
    if (held && pane === 'browser') go(base)
  }, [held, pane, go, base])
  const deciding = useOpeningPane({
    taskId,
    pane,
    detailNamed,
    headStatus: head.state.status,
    inFlight,
    liveRead,
    go,
    base,
  })

  const tabs = paneTabs({ task, base, hasBrowser, sessionOpen: sessionLive, childCount, runs, ckpt, ckptCount, pane })
  const selected: TaskPane = pane
  // THE THEATER ASKED FOR IS HELD UNTIL THE BROWSER TAB IS DRAWN (part of
  // #1346, comment 3): the route drops `?theater=1` after arrival, and the
  // tab waits on the task's read, so a route rebuilt in between handed the
  // tab no request. Held per agent, from arrival or a later ask, and let go
  // once the tab has it (it holds it from there through start-up) or the
  // address leaves the Browser tab.
  const theaterAsked = useRef<string | null>(theater ? taskId : null)
  const theaterWas = useRef<string | null>(theaterAsked.current)
  const theaterNow = theater ? taskId : null
  if (theaterNow !== null && theaterNow !== theaterWas.current) theaterAsked.current = theaterNow
  theaterWas.current = theaterNow
  const browserDrawn = !deciding && selected === 'browser' && task !== null && !held
  useEffect(() => {
    if (browserDrawn || pane !== 'browser') theaterAsked.current = null
  }, [browserDrawn, pane])
  // The banner's Open in theater, from a tab that is not Browser: to the Browser tab, in the theater.
  const openTheater = useCallback(() => go(`${base}/browser?theater=1`), [base, go])
  const onHandoffReady = useCallback(() => {
    reload()
    go(`${base}/browser`)
  }, [base, go, reload])
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
      // A region, not a dialog, once it is laid out under the phone header
      // (QA-116): `useSplitFocus` sets the role from where the panel sits.
      role="dialog"
      aria-label={`Agent ${taskId}`}
      ref={panel}
      // -1, so the panel is a legal destination for `.focus()` when it opens
      // over the list and is NOT a stop Tab lands on afterwards.
      tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !e.defaultPrevented) {
          // An open help card inside stops Escape before it reaches here
          // (see `HelpCard.tsx`), so the innermost open thing closes; so does
          // the theater, portalled out of this panel but still inside it to
          // React, and a key it already used is not this panel's.
          e.stopPropagation()
          if (foldInner(e.target, e.currentTarget)) return
          close()
          return
        }
        // Held in only where it covers the whole screen: under the phone
        // header (QA-116) Tab goes on to the header above it.
        if (e.key === 'Tab' && panel.current !== null && isModal(panel.current)) {
          trapTab(e, panel.current)
        }
      }}
    >
      <AgDivider snap={snap} panel={panel} />
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
          runs={runs}
          read={head.state.status}
          reload={reload}
          onClose={close}
          live={liveRead}
          reloadLive={reloadLive}
        />

        {/* UNDERLINE TABS WITH COUNTS (agents.html V1; #503 measured a boxed
            segmented control with none). A count the task document does not
            carry is a dash with its reason in the title, never a 0. The ids and
            addresses are unchanged: `detail` is still `/agents/<tab>/<id>`. */}
        <AgTabsEdge
          current={selected}
          tabs={tabs.map((t) => ({
            key: t.id,
            label: t.label,
            ...((t.id === 'detail' && t.count === null) || t.id === 'logs' || (t.id === 'browser' && t.count === null)
              ? {}
              : { count: t.count, why: t.say ?? undefined }),
          }))}
          onSelect={(key) => go(tabs.find((x) => x.id === key)!.to)}
        />

        {/* THE HAND-OFF BANNER (live-browser.md §6, lane LB-L12): the reason,
            the expected site and the time left, over whichever pane is open,
            with Take control and Open in theater. ONE BANNER (LB-UX1): the
            Browser tab draws its own, inside its theater host, so this one
            steps aside there. "I'm ready to sign in" opens the Browser tab.
            A parked one says since when here, and the Details tab's two
            bars that said it again step aside (LB-V4, U1). */}
        {task !== null && selected !== 'browser' && (
          <AgHandoff
            task={task}
            taskId={taskId}
            sessionOpen={sessionOpen}
            liveSession={liveSession}
            reloadLive={reloadLive}
            onReady={onHandoffReady}
            onTheater={openTheater}
          />
        )}
        {task !== null && <PodStartProblem task={task} />}

        <div className={`ag-split-pane${selected === 'logs' ? ' is-logs' : ''}${selected === 'changes' ? ' is-changes' : ''}${selected === 'browser' ? ' is-browser' : ''}`}>
          {deciding ? (
            <p className="art-loading">
              <Mark kind="pending" say="Reading the agent to open it on its browser if it is a running live browser, or on its details. The read is in flight." />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : selected === 'browser' && task !== null && !held ? (
            <LiveBrowserPane
              key={taskId}
              task={task}
              live={liveRead}
              reload={reloadLive}
              theater={theater || theaterAsked.current === taskId}
              onHandoffReady={reload}
              onPicture={setPicture}
            />
          ) : selected === 'browser' && held ? null : selected === 'logs' && task !== null ? (
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
            <>
              <AttemptTimelineScreen taskId={taskId} />
              {/* §7: a live-browser task's recordings, on its attempts. */}
              {task !== null && isLiveBrowser(task) && <RecordingsPanel task={task} />}
            </>
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
 * ESCAPE FOLDS THE INNERMOST OPEN THING FIRST (QA1 overview-agents-48,
 * 2026-10-28). With a checkpoint's `files` open, Escape closed the whole agent
 * and left for the list: the disclosure has no Escape of its own, so the key
 * came straight here. A disclosure the key was pressed on (`aria-expanded`
 * true) is folded by its own control, and an open `<details>` around the
 * key's target is closed with focus back on its summary. True when one was
 * folded, so the agent stays open; the next Escape is the panel's again.
 */
function foldInner(target: EventTarget, panel: HTMLElement): boolean {
  if (!(target instanceof HTMLElement) || !panel.contains(target)) return false
  if (target.getAttribute('aria-expanded') === 'true' && (target instanceof HTMLButtonElement || target.getAttribute('role') === 'button')) {
    target.click()
    return true
  }
  const open = target.closest('details[open]')
  if (open instanceof HTMLDetailsElement && panel.contains(open)) {
    open.open = false
    open.querySelector<HTMLElement>(':scope > summary')?.focus()
    return true
  }
  return false
}

/**
 * The attempt documents for the header's count. A read that throws or answers
 * nothing is a failed read, never a count of zero: `timesRun` then falls back
 * to the task's own counter.
 */
async function readAttempts(taskId: string): Promise<Result<{ attempts: unknown[] }>> {
  const failed: Result<{ attempts: unknown[] }> = {
    status: 'error',
    error: { kind: 'unreachable', httpStatus: null, code: null, message: 'The attempt read did not answer.' },
  }
  try {
    return (await loadAttempts(taskId)) ?? failed
  } catch {
    return failed
  }
}
