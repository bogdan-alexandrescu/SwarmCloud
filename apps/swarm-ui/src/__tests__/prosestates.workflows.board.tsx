// The workflow board's click-only states the fixture build never draws (issue
// 439; the registry is prosestates.workflows.tsx): a stage too wide to draw
// whole folds to one band, and its toggle opens every step (built as
// qafix3.workflows.test.tsx builds it: thirteen scans fanned out from a
// plan); and a step whose questions were answered, its block collapsed
// until opened (as workflow.questions.test.tsx builds it).

import { useState, type ReactElement } from 'react'

import type { ZoomTier } from '../dag'
import type { Task, Workflow, WorkflowStep } from '../types'
import { WorkflowCard } from '../Workflows'
import { WorkflowQuestions } from '../WorkflowQuestions'
import { task } from './runfixture'

const T0 = Date.parse('2026-09-23T12:00:00.000Z')
const iso = (offsetMs: number): string => new Date(T0 + offsetMs).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

/** A plan, `n` scans after it and a report after them all: the scans' stage folds to a band. */
function wideStage(n: number): Workflow {
  const fan = Array.from({ length: n }, (_, i) => `scan-${i}`)
  const steps = [step('plan', []), ...fan.map((id) => step(id, ['plan'], { runner_profile: 'codex' })), step('report', fan)]
  return {
    workflow_id: 'wf_wide',
    state: 'RUNNING',
    tenant_id: 'u-owner',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts: { unstarted: steps.length },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-900_000),
    updated_at: iso(-60_000),
    submitted_by: 'alice@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
}

/** The card with real stores behind its stage expansion and its zoom, as the screen holds them. */
function Card({ workflow }: Readonly<{ workflow: Workflow }>): ReactElement {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [zoom, setZoom] = useState<'auto' | ZoomTier>('auto')
  return (
    <WorkflowCard
      workflow={workflow}
      taskById={new Map<string, Task>()}
      zoom={zoom}
      onZoom={(_id, next) => setZoom(next)}
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={() => {}}
    />
  )
}

/** Thirteen scans in one stage. Click `.wf-band > .wf-band-toggle`: the stage opens. */
export const mountWideStage = (): ReactElement => <Card workflow={wideStage(13)} />

/** A finished design step whose one question was answered. Click `.wq-answered`: its block opens. */
export function mountAnsweredQuestions(): ReactElement {
  const design = task({
    id: 't_design',
    state: 'SUCCEEDED',
    workflow_id: 'wf_asked',
    step_id: 'design',
    completed_at: iso(-3_600_000),
    result_summary: { questions: 1 },
    owner_answers: [{ question_index: 0, question: 'Which layout?', choice: 'Grid', text: null, by: 'alex@example.com', at: iso(-600_000), history: [] }],
  })
  const workflow = { ...wideStage(0), workflow_id: 'wf_asked', steps: [step('design', [], { task_id: 't_design' })] }
  return <WorkflowQuestions workflow={workflow} taskById={new Map([['t_design', design]])} stateOf={() => 'succeeded'} />
}
