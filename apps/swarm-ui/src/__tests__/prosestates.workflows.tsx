// The workflows section's click-only states (issue 439; the harness and the
// rule are in prosestates.tsx): a workflow page's step inputs, its question
// strip and the finished workflow's quiet questions; a run's decision page --
// the folds, the meta chip, Reject's form, Edit plan, Approve's answer; and,
// mounted on hand-built props (prosestates.workflows.mounts.tsx) where the
// fixture build never draws them, a step card's folds, the run files'
// listings, the diagram's text, the QA run's dismissed routes, the facts
// card's More, Stop and Pause with their refusals, and a question answered.

import { fireEvent } from '@testing-library/react'

import type { ProseState } from './prosestates'
import { settle } from './proseapp'
import { clickText, fold } from './prosestates.agent'
import { click } from './prosestates.shell'
import {
  mountCancelRefused,
  mountCancelTyped,
  mountDiagram,
  mountPauseRefused,
  mountQaDismissed,
  mountQuestionsAnswered,
  mountRunFiles,
  mountStepCard,
  mountWorkflowFacts,
} from './prosestates.workflows.mounts'
import { screenLoaded } from './screenLoaded'

/** The fixture workflow of five checks and a synthesis, one failed. */
const CHECKS = '/workflows/wf_5e5ad3b6f7da4299a839'
/** The finished workflow with a question nobody answered. */
const QUIET = '/workflows/wf_quiet_q_01'
/** The planned issue run waiting on a decision. */
const PLANNED = '/runs/run_4c1e09d2'
/** The running issue run, its workflow started. */
const RUNNING = '/runs/run_19ab77e0'

/** Let what the last click started load and draw. */
async function drawn(): Promise<void> {
  await screenLoaded()
  await settle()
}

/** Wait for `selector` to be drawn, then click it. */
async function clickWhenDrawn(selector: string): Promise<void> {
  for (let i = 0; i < 20 && document.querySelector(selector) === null; i++) await drawn()
  click(selector)
}

/** Open the first `<details>` matching `selector`. */
function openDetails(selector: string): void {
  const d = document.querySelector<HTMLDetailsElement>(selector)
  if (d === null) throw new Error(`no fold ${selector}`)
  d.open = true
  fireEvent(d, new Event('toggle'))
}

export const WORKFLOWS: readonly ProseState[] = [
  // ------------------------------------------------------------ walked
  {
    key: 'workflow/step-inputs',
    group: 'workflows',
    files: ['WorkflowViews.tsx'],
    shows: '.wf-xinputs',
    walk: { open: '/workflows/wf_5e5ad3b6f7da4299a839/table', then: () => click('.wf-inputs-toggle') },
  },
  {
    key: 'workflow/needs-you',
    group: 'workflows',
    files: ['WorkflowQuestions.tsx'],
    shows: '.wq-needs-btn[aria-expanded="true"]',
    walk: { open: CHECKS, then: () => click('.wq-needs-btn') },
  },
  {
    key: 'workflow/quiet-questions',
    group: 'workflows',
    files: ['Workflows.tsx', 'QuestionsCard.tsx'],
    shows: '.wfp-quiet-qs',
    // The agent's own question, as it wrote it.
    data: ['Rename the views flag in this change or in a follow-up?'],
    lines: ['Asked instead of guessing'],
    moved: [
      'This agent (step implement) asked you these instead of guessing. Pick an option or write your own: the answer is stored on this agent, where whoever launched workflow wf_quiet_q_01 (alice@example.com) reads it with swarm_result.',
    ],
    walk: { open: QUIET, then: () => click('.wfp-quiet-q') },
  },
  {
    key: 'run/meta',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-meta.is-open',
    walk: { open: PLANNED, then: () => click('.rn-meta') },
  },
  {
    key: 'run/full-plan',
    group: 'workflows',
    files: ['Runs.tsx'],
    // The plan as the planner wrote it, and its JSON.
    data: ['Sum the steps\' recorded spend into the workflow read'],
    shows: 'details.rn-raw[open]',
    walk: { open: PLANNED, then: () => openDetails('details.rn-raw') },
  },
  {
    key: 'run/step-prompt',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: 'details.rn-prompt-d[open]',
    walk: { open: PLANNED, then: () => openDetails('details.rn-prompt-d') },
  },
  {
    key: 'run/issue-body',
    group: 'workflows',
    files: ['Runs.tsx'],
    // The issue's own body, as GitHub served it.
    data: ['See what a finished workflow cost without opening each step.'],
    shows: 'details.rn-read-body[open]',
    walk: { open: PLANNED, then: () => openDetails('details.rn-read-body') },
  },
  {
    key: 'run/fold',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: 'details.rn-fold[open]',
    lines: ['Not checked'],
    moved: ['This plan does not say: it was written without the planner’s read of the repository’s open issues and pull requests, so nothing here rules overlaps out.'],
    walk: { open: RUNNING, then: () => openDetails('details.rn-fold') },
  },
  {
    key: 'run/reject-form',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-reject',
    walk: { open: PLANNED, then: () => clickText('.rn-actions button', 'Reject') },
  },
  {
    key: 'run/edit-plan',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-edit',
    // The plan's own summary and prompts, in the fields that edit them.
    data: ['Sum the steps\' recorded spend into the workflow read', 'Draw the column in Workflows.tsx'],
    lines: ['Optional', 'Checked by the review'],
    moved: [
      'Free text, as the planner wrote it: a time or a size, such as “about 2 hours”. Optional.',
      'The requirements are what the review checks, and what decides Closes against part of. The mode and the overlaps are kept as the planner found them.',
      'Saving sends the digest of the plan you opened; an edit made by someone else since is refused.',
    ],
    walk: { open: PLANNED, then: () => clickText('.rn-actions button', 'Edit plan') },
  },
  {
    key: 'run/approved',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-decided',
    moved: ['The run is Running now, with its workflow created.'],
    walk: { open: PLANNED, then: () => clickText('.rn-actions button', 'Approve and run') },
  },
  // ------------------------------------------------------------ mounted
  {
    key: 'step/details',
    group: 'workflows',
    files: ['StepCard.tsx'],
    shows: 'details.sc-more[open]',
    mount: mountStepCard,
    act: () => click('details.sc-more > summary'),
  },
  {
    key: 'step/cause-whole',
    group: 'workflows',
    files: ['StepCard.tsx'],
    shows: '.sc-err-text.is-whole',
    // The step's own failure, as the API stored it.
    data: ['build failed: the agent exited with code 2'],
    mount: mountStepCard,
    act: () => click('.sc-err button[aria-expanded]'),
  },
  {
    key: 'run-files/objects',
    group: 'workflows',
    files: ['RunFiles.tsx'],
    shows: 'tr.rf-object',
    mount: mountRunFiles,
    act: () => clickText('button.copy[aria-expanded]', '2 objects'),
  },
  {
    key: 'run-files/browse',
    group: 'workflows',
    files: ['RunFiles.tsx'],
    shows: '.ckb[role="region"]',
    mount: mountRunFiles,
    act: () => clickText('button.copy[aria-expanded]', 'files'),
  },
  {
    key: 'diagram/as-text',
    group: 'workflows',
    files: ['DiagramView.tsx'],
    shows: 'details.dg-text[open]',
    mount: mountDiagram,
    act: async () => {
      await clickWhenDrawn('details.dg-text > summary')
      fold('body', 'Read as text')
    },
  },
  {
    key: 'qa-run/dismissed',
    group: 'workflows',
    files: ['QaRunGroup.tsx'],
    shows: '.qr-dismissed-list',
    mount: mountQaDismissed,
    act: () => clickWhenDrawn('.qr-dismissed .qr-linkish'),
  },
  {
    key: 'workflow/facts-more',
    group: 'workflows',
    files: ['WorkflowFacts.tsx'],
    shows: '.wff-parts:not(.is-clamped)',
    // The workflow's own description, as it was submitted.
    data: ['reads the pool table and notes the queue depth of pool'],
    mount: mountWorkflowFacts,
    act: () => click('.wff-more'),
  },
  {
    key: 'workflow/stop-confirm',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: '.wfp-confirm',
    lines: ['1 waiting cancelled · results kept'],
    moved: ['1 waiting step is cancelled now. Steps that finished keep their results.'],
    mount: mountCancelRefused,
    act: () => clickText('button', 'Stop workflow'),
  },
  {
    key: 'workflow/stop-refused',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: '.wfp-confirm-err',
    data: ['workflow store unavailable'],
    mount: mountCancelRefused,
    act: async () => {
      clickText('button', 'Stop workflow')
      await drawn()
      clickText('button', 'Yes, stop the workflow')
    },
  },
  {
    key: 'workflow/stop-typed',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: '[role="dialog"]',
    moved: ['1 running step is asked to stop and keeps its slot until its worker exits. Steps that finished keep their results.'],
    mount: mountCancelTyped,
    act: () => clickText('button', 'Stop workflow'),
  },
  {
    key: 'workflow/pause-refused',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: '.wfp-confirm-err',
    data: ['workflow store unavailable'],
    mount: mountPauseRefused,
    act: () => clickText('button', 'Pause workflow'),
  },
  {
    key: 'questions/answered',
    group: 'workflows',
    files: ['QuestionsCard.tsx'],
    shows: '.qc-saved',
    mount: mountQuestionsAnswered,
    act: async () => {
      await clickWhenDrawn('.qc-pick')
      clickText('[role="radio"].qc-pick', 'per workflow')
      await drawn()
      clickText('button', 'Answer')
    },
  },
]
