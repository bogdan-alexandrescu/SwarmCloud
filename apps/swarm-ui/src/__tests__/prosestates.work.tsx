// The work section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx): the timeline's key, filters, pickers and opened
// lane; Outcomes' filters and pickers; Home's folds and rows; the Needs-you
// strip and its answer box; the chooser; the submit forms' menus, folds and
// their answers after Submit; a QA run's dismissed routes. Walked on the
// fixture build where it draws the opener; mounted on hand-built props (with
// fetch stubbed, prosestates.work.mounts.tsx) where it never does.

import { fireEvent } from '@testing-library/react'

import { FIXTURE_CONTRACT_REFUSAL_PROMPT } from '../api'
import type { ProseState } from './prosestates'
import { settle } from './proseapp'
import { fold } from './prosestates.agent'
import {
  WORKFLOW_REFUSAL_MESSAGE,
  mountAwaitInput,
  mountHeldRunners,
  mountQuestion,
  mountTenantsRequested,
  mountWorkflowRefused,
} from './prosestates.work.mounts'
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
    files: ['TimelineLanes.tsx'],
    shows: '.tl-detail',
    walk: { open: TIMELINE, then: () => clickWhenDrawn('button.tl-name') },
  },
  {
    // The list a filter emptied: no fixture attempt is dead-lettered.
    key: 'work/timeline-filtered-empty',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-state:has(button)',
    lines: ['No lane matches'],
    moved: ['No lane matches state dead_lettered among the 31 attempts read.'],
    walk: {
      open: TIMELINE,
      then: async () => {
        await clickWhenDrawn('button.tl-name')
        click('.tl-sheet-toggle')
        fold('.tl-sheet', 'State')
        const box = [...document.querySelectorAll<HTMLInputElement>('.tl-sheet details.tl-pick[open] input')].find(
          (i) => i.closest('label')?.textContent?.trim() === 'dead-lettered',
        )
        if (box === undefined) throw new Error('the State picker offers no dead-lettered')
        fireEvent.click(box)
        await screenLoaded()
        await settle()
      },
    },
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
    // A figure's runs: the card Outcomes opens under the drawing.
    key: 'work/outcomes-rows',
    group: 'work',
    files: ['Activity.tsx', 'TimelineRows.tsx', 'Ledger.tsx'],
    shows: '.ol-rows',
    walk: { open: OUTCOMES, then: () => clickWhenDrawn('a[href*="rows="]') },
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
    files: ['work/NeedsYou.tsx'],
    shows: '.ny-answer',
    mount: mountQuestion,
    act: () => {
      click('.ny-bar')
      click('button[aria-label^="Answer "]')
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
    moved: ['lowers the child wall clock; the platform ceiling still wins', 'names one argv in the platform catalogue'],
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
    lines: ['Not running · costs nothing yet', 'Scheduler woken'],
    moved: ['That is not a running agent — it costs nothing until admission takes it.', 'The scheduler was woken by this submission.'],
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
    files: ['Submit.tsx', 'ContractJev.tsx'],
    shows: '.state.failed[role="alert"]',
    lines: ['Nothing was created', 'Rewrite each, then submit again'],
    moved: [
      'Work contract wc-lease-release (revision 1) is strict, and Jev’s review found requirements no worker could enforce:',
      'Jev’s review nothing observable when the step ends decides it: the outcome it asks for is vague',
      'Jev’s review nothing observable when the step ends decides it: it depends on something that happens after the step',
      'Rewrite each one as something the step’s end can show, or name the proof that shows it, then submit again.',
      'Nothing was created. Change what it names and submit again.',
    ],
    walk: {
      open: 'work/new',
      then: async () => {
        // The one prompt the fixture build refuses (api.ts FIXTURE_CONTRACT_REFUSAL_PROMPT), on the mock runner.
        await runner('mock')
        await clickWhenDrawn('.sbf-addset-btn')
        const item = [...document.querySelectorAll<HTMLButtonElement>('.sbf-addset-item')].find((b) => b.textContent === 'prompt')
        if (item === undefined) throw new Error('the mock runner offers no prompt setting')
        fireEvent.click(item)
        await screenLoaded()
        await settle()
        const field = [...document.querySelectorAll<HTMLElement>('.sbf-input .sbf-field')].at(-1)?.querySelector<HTMLInputElement | HTMLTextAreaElement>('textarea, input')
        if (field === null || field === undefined) throw new Error('the prompt setting drew no field')
        fireEvent.change(field, { target: { value: FIXTURE_CONTRACT_REFUSAL_PROMPT } })
        await screenLoaded()
        await settle()
        press('Start one agent')
      },
    },
  },
  {
    key: 'submit/runners-held',
    group: 'work',
    files: ['RunnerPicker.tsx'],
    shows: 'details.sbf-runners-more[open]',
    mount: mountHeldRunners,
    act: () => fold('body', 'Other runners'),
  },
  {
    key: 'submit-workflow/waits-for',
    group: 'work',
    files: ['SubmitWorkflow.tsx'],
    shows: 'details.wfb-more[open]',
    lines: ['stage a file'],
    moved: ['stage a file from a step it waits for'],
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
  {
    // Refused by the API after Submit: the fixture build creates every workflow, so it is mounted live.
    key: 'submit-workflow/refused',
    group: 'work',
    files: ['SubmitWorkflow.tsx', 'ContractJev.tsx'],
    shows: '.state.failed[role="status"]',
    // The API's own message, drawn verbatim.
    data: [WORKFLOW_REFUSAL_MESSAGE],
    lines: ['Nothing was created'],
    moved: ['Nothing was created. Correct the plan and submit again.'],
    mount: mountWorkflowRefused,
    act: async () => {
      for (let i = 0; i < 20 && document.querySelector('.wfb-step select') === null; i++) {
        await screenLoaded()
        await settle()
      }
      const select = document.querySelector<HTMLSelectElement>('.wfb-step select[aria-label="step-1 runner"], .wfb-step select')
      if (select === null) throw new Error('the workflow form drew no step runner')
      fireEvent.change(select, { target: { value: 'mock' } })
      await screenLoaded()
      await settle()
      press('Submit this workflow')
      for (let i = 0; i < 20 && document.querySelector('.state.failed') === null; i++) {
        await screenLoaded()
        await settle()
      }
    },
  },
  {
    // Admin › Tenants, which Activity.tsx draws: a workspace request approved.
    key: 'tenants/approved',
    group: 'work',
    files: ['Activity.tsx'],
    shows: '.ob-acts [role="status"]',
    mount: mountTenantsRequested,
    act: async () => {
      await clickWhenDrawn('[data-workspace-id="w-group1"] .ob-acts button')
      for (let i = 0; i < 20 && document.querySelector('.ob-acts [role="status"]') === null; i++) {
        await screenLoaded()
        await settle()
      }
    },
  },
  {
    key: 'work/await-input-answered',
    group: 'work',
    files: ['AwaitInputCard.tsx'],
    shows: '.ai-card:has([role="status"])',
    lines: ['Answered', 'Written as review.json'],
    moved: [
      'Answered. The steps after it start on the answer.',
      'The first answer is final; a correction is a new round.',
      'Written as review.json, 120 B with its files, which the next step stages.',
    ],
    mount: mountAwaitInput,
    act: async () => {
      press('Approve')
      await screenLoaded()
      await settle()
    },
  },
]
