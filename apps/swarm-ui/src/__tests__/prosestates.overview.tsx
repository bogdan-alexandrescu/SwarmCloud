// The overview section's click-only states (issue 439; the harness and the
// rule are in prosestates.tsx): Overview's folds; a schedule's sheet, its
// folds, and its run-now, pause, delete and merge-switch dialogs; an
// approval's decision page and its folds; Capacity's profile row, holders'
// agreeing pools, LLM plans' sheet and an API key's delete box; Platform
// counts' breakdown menu; the permission cards' fold; Account health's tips.
// Walked on the fixture build where it draws the opener.

import { fireEvent } from '@testing-library/react'

import type { ProseState } from './prosestates'
import { settle } from './proseapp'
import { fold } from './prosestates.agent'
import { click } from './prosestates.shell'
import { screenLoaded } from './screenLoaded'
import { mountApproval, mountDeleteRefused, mountFailures, mountInterviewDesign, mountMergeSwitch, mountRunningMore } from './prosestates.overview.mounts'

/** Wait for `selector` to be drawn by the page's later reads; throws when it never is. */
async function drawn(selector: string): Promise<HTMLElement> {
  for (let i = 0; i < 20; i++) {
    const el = document.querySelector<HTMLElement>(selector)
    if (el !== null) return el
    await screenLoaded()
    await settle()
  }
  throw new Error(`never drawn: ${selector}`)
}

/** Click `selector` once drawn, then let what it opens draw. */
async function clickDrawn(selector: string): Promise<void> {
  fireEvent.click(await drawn(selector))
  await screenLoaded()
  await settle()
}

/** Click the button under `scope` whose whole text starts with `text`. */
async function press(scope: string, text: string): Promise<void> {
  await drawn(scope)
  const b = [...document.querySelectorAll<HTMLElement>(`${scope} button, ${scope} a`)].find((x) => (x.textContent ?? '').trim().startsWith(text))
  if (b === undefined) throw new Error(`no button "${text}" under ${scope}`)
  fireEvent.click(b)
  await screenLoaded()
  await settle()
}

/** Open a fold under `scope` once drawn. */
async function foldDrawn(scope: string, text: string): Promise<void> {
  await drawn(scope)
  fold(scope, text)
  await settle()
}

const SCHEDULES = 'automate/schedules'

/** The first schedule's sheet. */
const sheet = (): Promise<void> => clickDrawn('a.acd-manage')

/** Type `text` into the open dialog's field. */
async function typeIn(text: string): Promise<void> {
  const input = await drawn('.c-dlg input')
  fireEvent.change(input, { target: { value: text } })
  await settle()
}

/** The schedule's name, as its sheet's heading says it. */
const sheetName = (): string => (document.querySelector('.as-sheet h2, .as-sheet [id]')?.textContent ?? '').trim()

/** Hover and focus the first tip host under `scope`. */
async function hover(scope: string): Promise<void> {
  const host = await drawn(`${scope} .c-tip-host`)
  fireEvent.mouseEnter(host)
  fireEvent.focus(host)
  await settle()
}

export const OVERVIEW: readonly ProseState[] = [
  {
    key: 'capacity/profile-row',
    group: 'overview',
    files: ['ProfileMatrix.tsx'],
    shows: '.cap-mx-exp',
    walk: { open: 'capacity/profiles', then: () => click('.cap-mx-toggle') },
  },
  {
    key: 'overview/running-more',
    group: 'overview',
    files: ['Overview.tsx'],
    shows: 'details.ov-running-more[open]',
    mount: mountRunningMore,
    act: () => foldDrawn('#ov-running', '4 more'),
  },
  {
    key: 'overview/failures-more',
    group: 'overview',
    files: ['OverviewRegions.tsx'],
    shows: 'details.ov-failures-more[open]',
    mount: mountFailures,
    act: () => foldDrawn('body', '3 more'),
  },
  {
    key: 'overview/failures-cancelled',
    group: 'overview',
    files: ['OverviewRegions.tsx'],
    shows: 'details.ov-failures-cancelled[open]',
    mount: mountFailures,
    act: () => foldDrawn('body', '2 cancelled'),
  },
  {
    key: 'automate/schedule-sheet',
    group: 'overview',
    files: ['Schedules.tsx', 'ScheduleDetail.tsx'],
    shows: '.as-sheet',
    walk: { open: SCHEDULES, then: sheet },
  },
  {
    key: 'automate/schedule-fold',
    group: 'overview',
    files: ['ScheduleDetail.tsx'],
    shows: '.as-fold-body',
    walk: { open: SCHEDULES, then: async () => { await sheet(); await clickDrawn('.as-fold[data-open="shut"] .as-fold-b') } },
  },
  {
    key: 'automate/schedule-run-now',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '.c-dlg',
    walk: { open: SCHEDULES, then: async () => { await sheet(); await press('.as-sheet', 'Run now') } },
  },
  {
    key: 'automate/schedule-ran',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '[data-testid="sc-said"]',
    // The fixture build refuses every schedule write: the API's own message.
    data: ['The fixture build serves no schedule routes.'],
    walk: { open: SCHEDULES, then: async () => { await sheet(); await press('.as-sheet', 'Run now'); await press('.c-dlg', 'Run now') } },
  },
  {
    key: 'automate/schedule-dry-run',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '[data-testid="sc-said"]',
    // The fixture build refuses every schedule write: the API's own message.
    data: ['The fixture build serves no schedule routes.'],
    walk: { open: SCHEDULES, then: async () => { await sheet(); await press('.as-sheet', 'Dry run') } },
  },
  {
    key: 'automate/schedule-pause',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '.c-dlg',
    walk: { open: SCHEDULES, then: async () => { await sheet(); await press('.as-sheet', 'Pause') } },
  },
  {
    key: 'automate/schedule-paused',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '[data-testid="sc-said"]',
    // The fixture build refuses every schedule write: the API's own message.
    data: ['The fixture build serves no schedule routes.'],
    walk: { open: SCHEDULES, then: async () => { await sheet(); await press('.as-sheet', 'Pause'); await press('.c-dlg', 'Pause') } },
  },
  {
    key: 'automate/schedule-delete',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '.c-dlg',
    walk: { open: SCHEDULES, then: async () => { await sheet(); await press('.as-sheet', 'Delete') } },
  },
  {
    key: 'automate/schedule-delete-refused',
    group: 'overview',
    files: ['Schedules.tsx'],
    shows: '.c-dlg [role="alert"]',
    data: ['schedule is firing'],
    mount: mountDeleteRefused,
    act: async () => {
      await press('.sc-head-acts', 'Delete')
      await typeIn('Plan sweep, docs')
      await press('.c-dlg', 'Delete schedule')
    },
  },
  {
    key: 'automate/schedule-merge-switch',
    group: 'overview',
    files: ['ScheduleDetail.tsx'],
    shows: '.c-dlg',
    mount: mountMergeSwitch,
    act: () => press('.sc-switch', 'Merge automatically'),
  },
  {
    key: 'automate/schedule-merge-rules',
    group: 'overview',
    files: ['ScheduleDetail.tsx'],
    shows: 'details.sc-rules[open]',
    mount: mountMergeSwitch,
    act: async () => {
      await press('.sc-switch', 'Merge automatically')
      await foldDrawn('.c-dlg', 'Details')
    },
  },
  {
    key: 'automate/schedule-merge-typed',
    group: 'overview',
    files: ['ScheduleDetail.tsx'],
    shows: '.c-dlg .c-field',
    mount: mountMergeSwitch,
    act: async () => {
      await press('.sc-switch', 'Merge automatically')
      await press('.c-dlg', 'Switch to automatic')
    },
  },
  {
    // The decision page an inbox item opens: drawn at its address.
    key: 'automate/approval-decision',
    group: 'overview',
    files: ['Approvals.tsx', 'ApprovalDecision.tsx', 'PlanDocument.tsx', 'PlanThread.tsx'],
    shows: '[data-testid="ab-head"]',
    // The plan's own purpose and why, as its planner wrote them.
    data: ['Date each exported note in the zone it was written in', "The export formats every date in the server's zone"],
    mount: mountApproval,
  },
  {
    key: 'automate/approval-reject',
    group: 'overview',
    files: ['ApprovalDecision.tsx'],
    shows: ':has(> input.apd-reason)',
    mount: mountApproval,
    act: () => press('[data-testid="ab-head"]', 'Reject'),
  },
  {
    key: 'automate/approval-approved',
    group: 'overview',
    files: ['ApprovalDecision.tsx', 'Approvals.tsx'],
    shows: '[role="status"]',
    mount: mountApproval,
    act: () => press('[data-testid="ab-head"]', 'Approve'),
  },
  {
    key: 'automate/approval-done-when',
    group: 'overview',
    files: ['ApprovalDecision.tsx'],
    shows: 'details.ab-reqlist[open]',
    // The plan's requirements, as its planner wrote them.
    data: ['An exported note is dated in the zone it was written in', 'A note with no zone is dated in UTC'],
    mount: mountApproval,
    act: () => foldDrawn('body', 'Done when'),
  },

  {
    key: 'automate/approval-step-prompt',
    group: 'overview',
    files: ['PlanDocument.tsx'],
    shows: 'details.pd-more[open]',
    // The step's prompt, as its planner wrote it.
    data: ["Pass the note's zone to format_day()"],
    mount: mountApproval,
    act: () => foldDrawn('body', 'Prompt'),
  },

  {
    key: 'automate/approval-briefs',
    group: 'overview',
    files: ['ApprovalDecision.tsx'],
    shows: 'details[data-testid="ab-rawbriefs"][open]',
    // The step's prompt, as its planner wrote it.
    data: ["Pass the note's zone to format_day()"],
    mount: mountApproval,
    act: () => foldDrawn('body', 'Raw step briefs'),
  },

  {
    key: 'capacity/holders-agreeing',
    group: 'overview',
    files: ['Holders.tsx'],
    shows: '.hold-drift:has(.hold-pool-clear[aria-expanded="true"])',
    walk: { open: 'capacity/holders', then: () => clickDrawn('.hold-pool-clear') },
  },
  {
    key: 'capacity/llm-plan-sheet',
    group: 'overview',
    files: ['LlmPlans.tsx'],
    shows: '.llp-sheet',
    walk: { open: 'capacity/llm-providers/plans', then: () => press('main', 'Add') },
  },
  {
    key: 'capacity/llm-plan-manage',
    group: 'overview',
    files: ['LlmPlans.tsx'],
    shows: '.llp-sheet',
    walk: { open: 'capacity/llm-providers/plans', then: () => press('main', 'Manage') },
  },
  {
    key: 'capacity/api-key-delete',
    group: 'overview',
    files: ['LlmApiKeys.tsx'],
    shows: '.lpc-panel.is-bad',
    walk: { open: 'capacity/llm-providers/api-keys', then: () => press('main', 'Delete') },
  },
  {
    key: 'capacity/api-key-replace',
    group: 'overview',
    files: ['LlmApiKeys.tsx'],
    shows: 'main .lpc-panel',
    walk: { open: 'capacity/llm-providers/api-keys', then: () => press('main', 'Replace') },
  },
  {
    key: 'capacity/api-key-caps',
    group: 'overview',
    files: ['LlmApiKeys.tsx'],
    shows: 'main .lpc-panel',
    walk: { open: 'capacity/llm-providers/api-keys', then: () => press('main', 'Caps') },
  },
  {
    key: 'capacity/api-key-pause',
    group: 'overview',
    files: ['LlmApiKeys.tsx'],
    shows: ':has(> .lpc-panel)',
    walk: { open: 'capacity/llm-providers/api-keys', then: () => press('main', 'Pause') },
  },
  {
    key: 'capacity/account-health-tip',
    group: 'overview',
    files: ['AccountHealthPage.tsx'],
    shows: '.c-tip:not([hidden])',
    walk: { open: 'capacity/account-health', then: () => hover('main') },
  },
  {
    key: 'admin/counts-breakdown',
    group: 'overview',
    files: ['PlatformOverTime.tsx'],
    shows: '.pot-menu',
    walk: { open: 'admin/counts', persona: 'platform-admin', then: () => clickDrawn('button[aria-haspopup="menu"]') },
  },
  {
    key: 'admin/counts-all',
    group: 'overview',
    files: ['PlatformOverTime.tsx'],
    shows: '.pot-fold button[aria-expanded="true"]',
    walk: { open: 'admin/counts', persona: 'platform-admin', then: async () => { await press('main', 'Run the count'); await clickDrawn('.pot-fold button') } },
  },
  {
    key: 'config/permission-card',
    group: 'overview',
    files: ['PermissionCards.tsx'],
    shows: '.fp-detail',
    walk: { open: 'config/workspace', then: async () => { await press('main', 'Setup'); await press('main', 'Details') } },
  },
  {
    key: 'work/interview-design',
    group: 'overview',
    files: ['InterviewPage.tsx'],
    shows: '.iv-big',
    mount: mountInterviewDesign,
    act: () => clickDrawn('.iv-shot-big'),
  },
]
