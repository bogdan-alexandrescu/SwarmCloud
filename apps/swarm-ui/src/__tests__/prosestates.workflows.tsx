// The workflows section's click-only states (issue 439; the harness and the
// rule are in prosestates.tsx): a workflow page's step inputs, its question
// strip and the finished workflow's quiet questions; a run's decision page --
// the folds, the meta chip, Reject's form, Edit plan, Approve's answer; and,
// mounted on hand-built props (prosestates.workflows.mounts.tsx) where the
// fixture build never draws them, a step card's folds, the run files'
// listings, the diagram's text, the QA run's dismissed routes, the facts
// card's More, Stop and Pause with their refusals, and a question answered.

import { act, fireEvent } from '@testing-library/react'
import { vi } from 'vitest'

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
import { mountAnsweredQuestions, mountWideStage } from './prosestates.workflows.board'
import {
  mountRunApproveRefused,
  mountRunChecks,
  mountRunEditRefused,
  mountRunHeld,
  mountRunLongSummary,
  mountRunMovedOn,
  mountRunOverlaps,
  mountRunPlanChanged,
  mountRunRejected,
  mountRunRejectRefused,
  mountRunsOlder,
  mountRunsOlderRefused,
} from './prosestates.workflows.runs'
import { RUN_POLL_MS } from '../Runs'
import { screenLoaded } from './screenLoaded'
import { mountStepInspector } from './prosestates.workflows.mounts'

/** The fixture workflow of five checks and a synthesis, one failed. */
const CHECKS = '/workflows/wf_5e5ad3b6f7da4299a839'
/** The running audit workflow: a step runs, one is parked. */
const AUDIT = '/workflows/wf_audit_01'
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

/** Wait for `selector` to be drawn. */
async function whenDrawn(selector: string): Promise<void> {
  for (let i = 0; i < 20 && document.querySelector(selector) === null; i++) await drawn()
}

/** Wait for `selector` to be drawn, then click it. */
async function clickWhenDrawn(selector: string): Promise<void> {
  await whenDrawn(selector)
  click(selector)
}

/** Open Reject's form, give a reason and send it. */
async function rejectWith(reason: string): Promise<void> {
  clickText('.rn-actions button', 'Reject')
  await drawn()
  const field = document.querySelector<HTMLTextAreaElement>('.rn-reject textarea')
  if (field === null) throw new Error('no reason field in the reject form')
  fireEvent.change(field, { target: { value: reason } })
  clickText('.rn-reject button', 'Reject this plan')
}

/** Open the first `<details>` matching `selector`. */
function openDetails(selector: string): void {
  const d = document.querySelector<HTMLDetailsElement>(selector)
  if (d === null) throw new Error(`no fold ${selector}`)
  d.open = true
  fireEvent(d, new Event('toggle'))
}

/** Pick the board's step drawn in `state` (its chip's `is-<state>` class). */
function pickStep(state: string): void {
  click(`button[data-step].is-${state}`)
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
  {
    key: 'workflow/step-picked',
    group: 'workflows',
    files: ['StepCard.tsx', 'Workflows.tsx', 'WorkflowViews.tsx'],
    shows: '.sc-card',
    walk: { open: AUDIT, then: () => pickStep('running') },
  },
  {
    key: 'workflow/step-failed',
    group: 'workflows',
    files: ['StepCard.tsx', 'Workflows.tsx'],
    shows: '.sc-card',
    walk: { open: CHECKS, then: () => pickStep('failed') },
  },
  {
    key: 'workflow/step-retry',
    group: 'workflows',
    // The confirm is work/NeedsYou.tsx's StepRetryButton, cleared here; that file's own states are the work step's (PENDING).
    files: ['StepCard.tsx', 'Workflows.tsx'],
    shows: '.sc-card [role="group"][aria-label^="Re-run step"]',
    moved: ['Re-run step checkpoints inside its workflow, with the steps its failure stopped?'],
    walk: {
      open: CHECKS,
      then: async () => {
        pickStep('failed')
        await drawn()
        clickText('.sc-card button', 'Retry')
      },
    },
  },
  {
    key: 'workflow/step-stop',
    group: 'workflows',
    files: ['StepCard.tsx', 'Workflows.tsx'],
    shows: '.stop-confirm',
    walk: {
      open: AUDIT,
      then: async () => {
        pickStep('running')
        await drawn()
        clickText('.sc-card button', 'Stop')
      },
    },
  },
  {
    key: 'workflow/overview',
    group: 'workflows',
    files: ['Workflows.tsx', 'work/Inspector.tsx'],
    shows: '.wi-dock',
    lines: ['Configuration, not an outcome'],
    moved: ['A step runs once every step it depends on is done. A failed step stops the workflow: no step after it starts. This is what the workflow was submitted with: configuration, not an outcome.'],
    walk: { open: AUDIT, then: () => click('.wi-unfold') },
  },
  {
    key: 'workflow/policy',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: 'details.wfp-details[open]',
    lines: ['Configuration, not an outcome'],
    moved: ['A step runs once every step it depends on is done. A failed step stops the workflow: no step after it starts. This is what the workflow was submitted with: configuration, not an outcome.'],
    walk: {
      open: AUDIT,
      then: async () => {
        click('.wi-unfold')
        await drawn()
        fold('.wi-dock', 'Policy')
      },
    },
  },
  {
    key: 'workflow/id-and-reads',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: 'details.wfp-details[open]',
    walk: {
      open: AUDIT,
      then: async () => {
        click('.wi-unfold')
        await drawn()
        fold('.wi-dock', 'Workflow id and reads')
      },
    },
  },
  {
    key: 'workflow/stop-running',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: '[role="dialog"][aria-modal="true"]:not(.wi-dock)',
    walk: {
      open: AUDIT,
      then: async () => {
        click('.wi-unfold')
        await drawn()
        clickText('.wfp-actions button', 'Stop workflow')
      },
    },
  },
  {
    key: 'workflow/changes-step',
    group: 'workflows',
    files: ['WorkflowChanges.tsx'],
    shows: '.chg-mx:has(.chg-mx-chip.mono[aria-pressed="true"])',
    lines: ['Only the files of scan-terraform'],
    moved: ['Only the files of scan-terraform, one column of this page; All steps shows every column. A step’s whole patch, to read or download, is its agent’s own Changes tab: Agent’s changes ›.'],
    walk: { open: `${AUDIT}/changes`, then: () => clickText('.chg-mx-chip.mono', 'scan-terraform') },
  },
  {
    key: 'workflow/changes-unstarted',
    group: 'workflows',
    files: ['WorkflowChanges.tsx'],
    shows: '.chg-mx:has(.chg-mx-chip.mono[aria-pressed="true"])',
    lines: ['Only the files of publish'],
    moved: ['Only the files of publish, one column of this page; All steps shows every column. A step’s whole patch, to read or download, is its agent’s own Changes tab: Agent’s changes ›.'],
    walk: { open: `${AUDIT}/changes`, then: () => clickText('.chg-mx-chip.mono', 'publish') },
  },
  // ------------------------------------------------------------ mounted
  {
    key: 'workflow/stage-open',
    group: 'workflows',
    files: ['Workflows.tsx'],
    shows: '.wf-band:has(> .wf-band-toggle[aria-expanded="true"])',
    mount: mountWideStage,
    act: () => click('.wf-band > .wf-band-toggle'),
  },
  {
    key: 'workflow/card-step',
    group: 'workflows',
    files: ['Workflows.tsx', 'WorkflowViews.tsx'],
    shows: '.wf-inspect',
    lines: ['No other workflow of this shape'],
    moved: ['No other workflow of this shape on this board.'],
    mount: mountWideStage,
    act: () => click('button[data-step="plan"]'),
  },
  {
    key: 'workflow/questions-answered',
    group: 'workflows',
    files: ['WorkflowQuestions.tsx', 'QuestionsCard.tsx'],
    shows: '.wq-step-body',
    lines: ['Questions not read · the API served none'],
    mount: mountAnsweredQuestions,
    act: () => click('.wq-answered'),
  },
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
  {
    key: 'run/summary-whole',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-summary:has(.rn-more-btn[aria-expanded="true"])',
    // The plan's own summary, as the planner wrote it.
    data: ['Sum each step', 'The sum is taken in the route', 'The UI then draws the column', 'A workflow with no priced step'],
    mount: mountRunLongSummary,
    act: () => click('.rn-more-btn'),
  },
  {
    key: 'run/overlap-open',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: 'details.rn-overlap[open]',
    // The planner's own note on the overlap.
    data: ['Already edits routes/workflows.py, so the two changes will conflict unless this plan waits for it.'],
    mount: mountRunOverlaps,
    act: () => openDetails('details.rn-overlap'),
  },
  {
    key: 'run/check-list',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: 'details.rn-check-list[open]',
    mount: mountRunChecks,
    act: () => openDetails('details.rn-check-list'),
  },
  {
    key: 'run/approve-refused',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-wait .state.failed',
    data: ['the store refused the approval'],
    lines: ['The change was not confirmed'],
    moved: ['The platform did not confirm this change. Read the page again to see whether it was made before sending it again.'],
    mount: mountRunApproveRefused,
    act: () => clickText('.rn-actions button', 'Approve and run'),
  },
  {
    key: 'run/rejected',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-decided',
    moved: ['The run is Rejected now; nothing ran.'],
    mount: mountRunRejected,
    act: () => rejectWith('Out of scope'),
  },
  {
    key: 'run/reject-refused',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.state.failed',
    data: ['the store refused the rejection'],
    lines: ['The change was not confirmed'],
    moved: ['The platform did not confirm this change. Read the page again to see whether it was made before sending it again.'],
    mount: mountRunRejectRefused,
    act: () => rejectWith('Out of scope'),
  },
  {
    key: 'run/edit-refused',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.state.failed',
    data: ['the store refused the edit'],
    lines: ['The change was not confirmed'],
    moved: ['The platform did not confirm this change. Read the page again to see whether it was made before sending it again.'],
    mount: mountRunEditRefused,
    act: async () => {
      await whenDrawn('.rn-actions button')
      clickText('.rn-actions button', 'Edit plan')
      await drawn()
      const summary = document.querySelector<HTMLTextAreaElement>('.rn-edit textarea')
      if (summary === null) throw new Error('no summary field in the plan editor')
      fireEvent.change(summary, { target: { value: 'My edit of the plan.' } })
      clickText('.rn-edit button', 'Save the plan')
    },
  },
  {
    key: 'run/plan-changed',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-notice',
    lines: ['The plan changed; nothing was done'],
    moved: ['The plan changed since you opened it, so nothing was done. This is the plan now: read it, then act on it again.'],
    mount: mountRunPlanChanged,
    act: async () => {
      await whenDrawn('.rn-actions button')
      clickText('.rn-actions button', 'Approve and run')
    },
  },
  {
    key: 'run/moved-on',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-notice',
    lines: ['The run moved on; nothing was done'],
    moved: ['The run moved on since you opened it, so nothing was done: the run is REJECTED'],
    mount: mountRunMovedOn,
    act: async () => {
      await whenDrawn('.rn-actions button')
      clickText('.rn-actions button', 'Approve and run')
    },
  },
  {
    // Reached by the page's own next read, not a click: the poll serves someone else's plan.
    key: 'run/plan-held',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-held',
    lines: ['The plan changed since this page drew it'],
    moved: ['The actions below still name the plan shown, so the API will refuse them.'],
    mount: mountRunHeld,
    act: async () => {
      await whenDrawn('.rn-actions button')
      await act(async () => {
        await vi.advanceTimersByTimeAsync(RUN_POLL_MS)
      })
    },
  },
  {
    key: 'runs/older',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-row:nth-child(2)',
    mount: mountRunsOlder,
    act: async () => {
      await clickWhenDrawn('.rn-more button')
    },
  },
  {
    key: 'runs/older-refused',
    group: 'workflows',
    files: ['Runs.tsx'],
    shows: '.rn-list > .state.failed',
    data: ['the store refused the page'],
    mount: mountRunsOlderRefused,
    act: async () => {
      await clickWhenDrawn('.rn-more button')
    },
  },
  // ---------------------------------------------------------------- openers no state opened (review round 2)
  {
    key: 'workflows/inspector-more-facts',
    group: 'workflows',
    files: ['WorkflowViews.tsx'],
    shows: 'details.wf-inspect-more[open]',
    moved: ['Raised by every new lease; a worker holding an older one exits without running.'],
    mount: mountStepInspector,
    act: () => click('details.wf-inspect-more > summary'),
  },
]
