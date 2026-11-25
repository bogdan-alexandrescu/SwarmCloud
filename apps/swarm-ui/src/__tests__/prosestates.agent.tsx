// The agent section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx): an agent page's folds, the stop dialog and its
// outcomes, the artifact list's rows and viewer, the live browser's strip and
// take-over, the checkpoint browser, the attempt drawer, the log tools, the
// split view's tab menus and the diff's folds. Walked on the fixture build's
// agents where it draws the opener; mounted on hand-built props (with
// fetch stubbed, prosestates.agent.mounts.tsx) where it never does.

import { fireEvent } from '@testing-library/react'

import type { ProseState } from './prosestates'
import { settle } from './proseapp'
import { screenLoaded } from './screenLoaded'
import { mountDiff, mountDiffPhone, mountStop, mountTabs, mountToolMenu, mountWait, refuseStop, releaseStop } from './prosestates.agent.mounts'
import { click } from './prosestates.shell'

/** The ordinary running agent the QA capture opens (qa/routes.json). */
const RUNNING = 'work/task/task_a073aff5'
/** The running browser agent. */
const BROWSER = 'work/task/task_b40e5a1c'
/** A finished agent with artifacts. */
const DONE = 'work/task/task_a3881bec'

/** Open the first `<details>` under `scope` whose summary starts with `text`. */
export function fold(scope: string, text: string): void {
  for (const d of document.querySelectorAll<HTMLDetailsElement>(`${scope} details`)) {
    const s = d.querySelector('summary')
    if (s === null || !(s.textContent ?? '').replace(/\s+/g, ' ').trim().startsWith(text)) continue
    d.open = true
    fireEvent(d, new Event('toggle'))
    return
  }
  throw new Error(`no fold "${text}" under ${scope}`)
}

/** Click the first `selector` whose text starts with `text`. */
export function clickText(selector: string, text: string): void {
  for (const el of document.querySelectorAll<HTMLElement>(selector)) {
    if ((el.textContent ?? '').replace(/\s+/g, ' ').trim().startsWith(text)) {
      fireEvent.click(el)
      return
    }
  }
  throw new Error(`nothing to click: ${selector} "${text}"`)
}

/** Let what the last click started load and draw. */
async function drawn(): Promise<void> {
  await screenLoaded()
  await settle()
}

export const AGENT: readonly ProseState[] = [
  {
    key: 'agent/resources',
    group: 'agent',
    files: ['AgentDetail.tsx'],
    shows: 'details.dt-res[open]',
    walk: { open: RUNNING, then: () => click('details.dt-res > summary') },
  },
  {
    key: 'agent/utilisation',
    group: 'agent',
    files: ['AgentDetail.tsx', 'UtilisationPanel.tsx'],
    shows: 'details.ctl-util-disc[open]',
    walk: { open: RUNNING, then: () => fold('.ag-split', 'CPU and memory over time') },
  },
  {
    key: 'agent/over-time',
    group: 'agent',
    files: ['AgentDetail.tsx'],
    shows: 'details.dt-disc[open]',
    walk: { open: RUNNING, then: () => fold('.ag-split', 'Details: memory, spend') },
  },
  {
    key: 'agent/events',
    group: 'agent',
    files: ['AgentDetail.tsx'],
    shows: 'details.dt-disc[open]',
    walk: { open: RUNNING, then: () => fold('.ag-split', 'All 8 events') },
  },
  {
    key: 'agent/metadata',
    group: 'agent',
    files: ['AgentDetail.tsx'],
    shows: 'details.dt-disc[open]',
    walk: { open: RUNNING, then: () => fold('.ag-split', 'Metadata') },
  },
  {
    key: 'agent/stop-dialog',
    group: 'agent',
    files: ['StopRun.tsx', 'agent-split/head.tsx'],
    shows: '.stop-confirm',
    walk: { open: RUNNING, then: () => click('.ag-split .run-stop-btn') },
  },
  {
    key: 'agent/split',
    group: 'agent',
    files: ['AgentSplit.tsx'],
    shows: '.ag-split[role="dialog"]',
    // The first row's agent's own prompt.
    data: ['Show the queue depth per pool on the Capacity page.'],
    walk: { open: 'work/running', then: () => click('.cr-open') },
  },
  {
    key: 'agent/log-facts',
    group: 'agent',
    files: ['trace/Notes.tsx', 'trace/Trace.tsx'],
    shows: 'details.trc-readfacts[open]',
    walk: { open: `${RUNNING}/logs`, then: () => click('details.trc-readfacts > summary') },
  },
  {
    key: 'agent/attempt-detail',
    group: 'agent',
    files: ['AttemptTimeline.tsx'],
    shows: 'details.ev-more[open]',
    walk: { open: `${RUNNING}/attempts`, then: () => click('details.ev-more > summary') },
  },
  {
    key: 'agent/browser-use',
    group: 'agent',
    files: ['BrowserUse.tsx'],
    shows: '.bu-detail:not([hidden])',
    walk: { open: BROWSER, then: () => click('.bu-toggle') },
  },
  {
    key: 'agent/live-strip-why',
    group: 'agent',
    files: ['LiveBrowser.tsx'],
    shows: 'details.live-strip-why[open]',
    moved: ['The address Chromium reports for the page the agent is on.'],
    walk: { open: BROWSER, then: () => click('details.live-strip-why > summary') },
  },
  {
    key: 'agent/artifact-open',
    group: 'agent',
    files: ['Artifacts.tsx', 'ArtifactViewer.tsx'],
    shows: 'tr.is-open + tr',
    lines: ['not redacted'],
    moved: ['not redacted: the patch as stored, for you as its submitter'],
    walk: { open: `${DONE}/artifacts`, then: () => click('.art-open') },
  },
  {
    key: 'agent/prompt',
    group: 'agent',
    files: ['AgentDetail.tsx', 'Artifacts.tsx'],
    shows: 'details.dt-disc[open]',
    // The agent's own prompt, as submitted.
    data: ['Browser QA agent: open https://example.com'],
    walk: { open: 'work/task/task_f523c160', then: () => fold('.ag-split', 'Show prompt') },
  },
  {
    key: 'agent/logs-more',
    group: 'agent',
    files: ['AgentLogs.tsx'],
    shows: 'details.ag-logs-more[open]',
    walk: {
      open: `${RUNNING}/logs`,
      then: async () => {
        clickText('.ag-split button', 'Agent stdout')
        await drawn()
        click('details.ag-logs-more > summary')
      },
    },
  },
  {
    key: 'agent/stream-facts',
    group: 'agent',
    files: ['Artifacts.tsx'],
    shows: 'details.ag-logmeta[open]',
    walk: {
      open: `${RUNNING}/logs`,
      then: async () => {
        clickText('.ag-split button', 'Agent stdout')
        await drawn()
        click('details.ag-logmeta:not(.trc-readfacts) > summary')
      },
    },
  },
  {
    key: 'agent/checkpoint-files',
    group: 'agent',
    files: ['CheckpointBrowser.tsx', 'CheckpointsPane.tsx'],
    shows: '.ckb',
    walk: { open: `${RUNNING}/checkpoints`, then: () => clickText('.ag-split button.copy', 'files') },
  },
  {
    key: 'agent/checkpoint-file',
    group: 'agent',
    files: ['CheckpointBrowser.tsx'],
    shows: '.ckb .art-viewer',
    walk: {
      open: `${RUNNING}/checkpoints`,
      then: async () => {
        clickText('.ag-split button.copy', 'files')
        await drawn()
        click('.ckb li[role="treeitem"]:not([aria-expanded]) .ckb-open')
      },
    },
  },
  {
    key: 'agent/device-menu',
    group: 'agent',
    files: ['LiveBrowser.tsx'],
    shows: '.live-device-pop',
    walk: { open: `${BROWSER}/browser`, then: () => click('.live-device-btn') },
  },
  {
    key: 'agent/theater',
    group: 'agent',
    files: ['Theater.tsx', 'TheaterTimeline.tsx'],
    moved: ['SwarmCloud is unreachable: the agent’s actions could not be read.'],
    lines: ['Actions not read'],
    shows: '.th-root[role="dialog"]',
    walk: { open: `${BROWSER}/browser`, then: () => click('.th-open') },
  },
  {
    key: 'agent/stop-requested',
    group: 'agent',
    files: ['StopRun.tsx'],
    shows: '.stop-note, .stop-pending',
    lines: ['Stop requested.'],
    moved: ['Still running until the next heartbeat.'],
    walk: {
      open: RUNNING,
      then: async () => {
        click('.ag-split .run-stop-btn')
        await drawn()
        click('.stop-confirm .stop-actions button')
      },
    },
  },
  {
    key: 'agent/stop-refused',
    group: 'agent',
    files: ['StopRun.tsx'],
    shows: '.stop-confirm .warn-text',
    data: ['the store refused the write'],
    lines: ['Nothing was stopped.'],
    mount: () => {
      refuseStop('the store refused the write')
      return mountStop()
    },
    act: async () => {
      click('.run-stop-btn')
      await drawn()
      click('.stop-confirm .stop-actions button')
    },
  },
  {
    key: 'agent/stop-released',
    group: 'agent',
    files: ['StopRun.tsx'],
    shows: '.stop-note',
    lines: ['Stopped.'],
    mount: () => {
      releaseStop()
      return mountStop()
    },
    act: async () => {
      click('.run-stop-btn')
      await drawn()
      click('.stop-confirm .stop-actions button')
    },
  },
  {
    key: 'agent/wait-history',
    group: 'agent',
    files: ['WaitPanel.tsx'],
    shows: 'entry 1',
    mount: mountWait,
    act: () => click('.wp-history .dt-more'),
  },
  {
    key: 'agent/diff-files',
    group: 'agent',
    files: ['diff/DiffView.tsx'],
    shows: '.diff-sheet[role="dialog"]',
    mount: mountDiffPhone,
    act: () => click('.diff-all'),
  },
  {
    key: 'agent/diff-file-fold',
    group: 'agent',
    files: ['diff/DiffView.tsx'],
    shows: 'collapsed',
    mount: mountDiff,
    act: () => click('.diff-row.is-file[aria-expanded="true"]'),
  },
  {
    key: 'agent/tool-menu',
    group: 'agent',
    files: ['LiveToolMenu.tsx'],
    shows: '.live-more-menu',
    mount: mountToolMenu,
    act: () => click('.live-more-btn'),
  },
  {
    key: 'agent/tabs-more',
    group: 'agent',
    files: ['agent-split/tabs.tsx', 'AgentSplit.tsx'],
    shows: '.ag-tabs-menu',
    mount: mountTabs,
    act: () => click('.ag-tabs-more'),
  },
  {
    key: 'agent/recording-parts',
    group: 'agent',
    files: ['Recordings.tsx'],
    shows: 'details.rec-parts[open]',
    walk: { open: `${BROWSER}/attempts`, then: () => click('details.rec-parts > summary') },
  },
  {
    key: 'agent/take-control',
    group: 'agent',
    files: ['LiveBrowser.tsx'],
    shows: '.live-ask[role="dialog"]',
    lines: ['Every member watching sees the page'],
    moved: [
      'The agent waits while you are in control, and every member watching sees the page; password fields are painted over.',
      'Where the page wants an account, use one made for this agent where you can, not your own.',
    ],
    walk: { open: `${BROWSER}/browser`, then: () => clickText('.live-control button', 'Take control') },
  },
  {
    key: 'agent/open-to-members',
    group: 'agent',
    files: ['LiveBrowser.tsx'],
    shows: '.live-ask.is-members',
    lines: ['Stays on for later attempts'],
    moved: [
      'While the agent asks for help, any member of this workspace can take control of its browser, not only you.',
      "This stays on for this agent's later attempts too, until you turn it off here.",
      'Every take is in the audit, and you can still take control from them.',
    ],
    walk: { open: `${BROWSER}/browser`, then: () => click('.live-open input[type="checkbox"]') },
  },
]
