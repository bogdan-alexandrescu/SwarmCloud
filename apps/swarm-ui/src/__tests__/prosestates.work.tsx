// The work section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx): the timeline's key, filters, pickers and opened
// lane; Outcomes' filters and pickers; Home's folds and rows; the Needs-you
// strip and its answer box; the chooser; the submit forms' menus, folds and
// their answers after Submit; a QA run's dismissed routes. Walked on the
// fixture build where it draws the opener; mounted on hand-built props (with
// fetch stubbed, prosestates.work.mounts.tsx) where it never does.

import { fireEvent } from '@testing-library/react'

import type { ProseState } from './prosestates'
import { settle } from './proseapp'
import { fold } from './prosestates.agent'
import { click } from './prosestates.shell'
import { screenLoaded } from './screenLoaded'

/** Click `selector` once the page's later reads have drawn it (a lane, a form's runner list). */
async function clickWhenDrawn(selector: string): Promise<void> {
  for (let i = 0; i < 20 && document.querySelector(selector) === null; i++) {
    await screenLoaded()
    await settle()
  }
  click(selector)
}

/** Choose the task form's runner `name` (step 1), then let step 2 draw. */
async function runner(name: string): Promise<void> {
  await clickWhenDrawn('label.sbf-runner input')
  const input = [...document.querySelectorAll<HTMLInputElement>('label.sbf-runner input')].find(
    (i) => i.closest('label')?.querySelector('.sbf-runner-name')?.textContent === name,
  )
  if (input === undefined) throw new Error(`no runner ${name} on the form`)
  fireEvent.click(input)
  await screenLoaded()
  await settle()
}

/** Click the button whose whole text is `text`. */
function press(text: string): void {
  const b = [...document.querySelectorAll<HTMLButtonElement>('button')].find((x) => (x.textContent ?? '').trim() === text)
  if (b === undefined) throw new Error(`no button "${text}"`)
  fireEvent.click(b)
}

const TIMELINE = 'work/timeline'
const WORKFLOW_FORM = 'work/new-workflow'

/** The workflow form's first step, on the runner `name`. */
async function stepRunner(name: string): Promise<void> {
  await clickWhenDrawn('select.sb-runner-select')
  const select = document.querySelector<HTMLSelectElement>('select.sb-runner-select')!
  fireEvent.change(select, { target: { value: name } })
  await screenLoaded()
  await settle()
}
const OUTCOMES = '/timeline/outcomes'

export const WORK: readonly ProseState[] = [
  {
    key: 'work/timeline-key',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-key.is-open',
    walk: { open: TIMELINE, then: () => click('.tl-key-toggle') },
  },
  {
    key: 'work/timeline-filters',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-sheet.is-open',
    walk: { open: TIMELINE, then: () => click('.tl-sheet-toggle') },
  },
  {
    key: 'work/timeline-state-picker',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-sheet details.tl-pick[open]',
    walk: { open: TIMELINE, then: () => fold('.tl-sheet', 'State') },
  },
  {
    key: 'work/timeline-lane',
    group: 'work',
    files: ['TimelineLanes.tsx', 'TimelineRows.tsx'],
    shows: '.tl-detail',
    walk: { open: TIMELINE, then: () => clickWhenDrawn('button.tl-name') },
  },
  {
    key: 'work/outcomes-filters',
    group: 'work',
    files: ['Activity.tsx'],
    shows: '.ol-sheet.is-open',
    walk: { open: OUTCOMES, then: () => click('.ol-sheet-toggle') },
  },
  {
    key: 'work/outcomes-picker',
    group: 'work',
    files: ['Activity.tsx'],
    shows: '.ol-sheet details.tl-pick[open]',
    walk: { open: OUTCOMES, then: () => fold('.ol-sheet', '') },
  },
  {
    key: 'work/needs-you',
    group: 'work',
    files: ['work/NeedsYou.tsx', 'Agents.tsx'],
    shows: '.ny.is-open',
    // A fixture agent's task title, as it was submitted.
    data: ['Check the five contract invariants, then write one synthesis'],
    walk: { open: 'work/running', then: () => click('.ny-bar') },
  },
  {
    key: 'work/needs-you-answer',
    group: 'work',
    files: ['work/NeedsYou.tsx', 'AwaitInputCard.tsx'],
    shows: '.ny-answer',
    walk: {
      open: 'work/running',
      then: async () => {
        await clickWhenDrawn('.ny-bar')
        await clickWhenDrawn('button[aria-label^="Answer "]')
      },
    },
  },
  {
    key: 'work/chooser',
    group: 'work',
    files: ['SubmitChooser.tsx'],
    shows: '.sb-chooser',
    // The recent prompts the fixture build lists: what was submitted, not the console's words.
    data: [
      'Write the runbook for rotating a tenant git secret.',
      'Show the queue depth per pool on the Capacity page.',
      'Add a test for a park that outlives its provider wait.',
      'Make the reconciler release a lease whose generation is stale.',
    ],
    lines: ['Opens with N'],
    moved: [
      'Listed: 5 agents, newest first. Found among your newest 27 agents. Yours among the workspace’s newest 4 workflows are older than these. None of yours among the workspace’s newest 2 issue runs.',
      'One agent, one runner. It lands ready or parked and costs nothing until the scheduler admits it.',
      'Steps in stages that hand work to each other, opening one PR or one per step.',
      'Name an issue. A planner reads it and writes a plan; once the plan is approved it runs as a workflow.',
      'The Submit button in the sidebar opens this page; N from anywhere does too.',
    ],
    walk: { open: 'work/running', then: () => click('a.sk-cta') },
  },
  {
    // Was a `?` fold that drew the note as a paragraph; now its (i), opened.
    key: 'work/home-why',
    group: 'work',
    files: ['Home.tsx'],
    shows: '#home-needs .hm-why:has(.c-tip:not([hidden]))',
    moved: [
      'Questions your agents asked you, approvals in your inbox, the requests you may decide, your agents waiting for you at a browser, and your own runs that failed today, of your newest tasks read. Waiting items hold no capacity.',
    ],
    walk: { open: 'home', then: () => click('#home-needs button[aria-label="About Needs you"]') },
  },
  {
    key: 'work/home-workflow-row',
    group: 'work',
    files: ['HomeWorkLists.tsx'],
    shows: '.wl-wf.is-open',
    walk: { open: 'home', then: () => click('.wl-disclose') },
  },
  {
    key: 'submit/add-setting',
    group: 'work',
    files: ['Submit.tsx'],
    shows: '.sbf-addset-list',
    walk: {
      open: 'work/new',
      then: async () => {
        await runner('generic')
        await clickWhenDrawn('.sbf-addset-btn')
      },
    },
  },
  {
    key: 'submit/created',
    group: 'work',
    files: ['Submit.tsx'],
    shows: '.state[role="status"]',
    walk: {
      open: 'work/new',
      then: async () => {
        await runner('generic')
        press('Start one agent')
      },
    },
  },
  {
    key: 'submit/refused',
    group: 'work',
    files: ['Submit.tsx'],
    shows: '.state.failed[role="alert"]',
    walk: {
      open: 'work/new',
      then: async () => {
        await runner('claude-code')
        press('Start one agent')
      },
    },
  },
  {
    key: 'submit/runners-held',
    group: 'work',
    files: ['RunnerPicker.tsx', 'BrowserToolPicker.tsx'],
    shows: 'details.sbf-runners-more[open]',
    walk: { open: 'work/new', then: () => clickWhenDrawn('details.sbf-runners-more > summary') },
  },
  {
    key: 'submit-workflow/waits-for',
    group: 'work',
    files: ['SubmitWorkflow.tsx'],
    shows: 'details.wfb-more[open]',
    walk: {
      open: WORKFLOW_FORM,
      then: async () => {
        await clickWhenDrawn('.wfb-add.is-stage')
        await screenLoaded()
        await settle()
        fold('body', 'waits for')
      },
    },
  },
  {
    key: 'submit-workflow/not-sent',
    group: 'work',
    files: ['SubmitWorkflow.tsx'],
    shows: '.state.failed',
    walk: {
      open: WORKFLOW_FORM,
      then: async () => {
        await clickWhenDrawn('.wfb-add.is-stage')
        press('Submit this workflow')
      },
    },
  },
  {
    key: 'submit-workflow/created',
    group: 'work',
    files: ['SubmitWorkflow.tsx'],
    shows: '.state[role="status"]',
    walk: {
      open: WORKFLOW_FORM,
      then: async () => {
        await stepRunner('generic')
        press('Submit this workflow')
      },
    },
  },
]
