// The workflow section's click-only states the fixture build never draws
// (issue 439): each screen mounted on hand-built props, so one click opens
// the state. Copied from the tests that build the same props: stepcard.tiles,
// checkpoint.reclaimed, diagram.view, qarungroup, workflow.names.surfaces,
// workflow.u2, workflow.pause and questions.card. The three that read the
// network (the QA run page, the questions card) stub `fetch` on the live
// build, and the diagram spies mermaid's `render` (jsdom has no layout);
// `restoreWorkflowStubs` undoes all of it after each state.

import type { ReactElement } from 'react'
import mermaid from 'mermaid'
import { vi } from 'vitest'

import type { CancelWorkflowResult, PauseWorkflowResult } from '../api'
import DiagramView from '../DiagramView'
import type { Result } from '../fetch'
import { absentCell, measuredCell } from '../measure'
import { RunFiles, type CheckpointListing } from '../RunFiles'
import { StepCard } from '../StepCard'
import type { CheckpointRecord, CheckpointsPage, Task, Workflow, WorkflowStep } from '../types'
import type { AttemptLoader, StepRowModel } from '../WorkflowViews'
import { CancelWorkflow, PauseWorkflow } from '../Workflows'
import { WorkflowFacts } from '../WorkflowFacts'
import { serve } from './repofixture'
import { at, attempt, task } from './runfixture'

// ------------------------------------------------------------------ stubs

const restores: (() => void)[] = []

/** Undo every stub a mount set: called after each state. */
export function restoreWorkflowStubs(): void {
  for (const undo of restores.splice(0).reverse()) undo()
}

/** Stub fetch on the live build (the page reads the API, not the fixtures). */
function stubLive(handler: Parameters<typeof serve>[0]): void {
  const realFetch = globalThis.fetch
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  restores.push(() => {
    globalThis.fetch = realFetch
    vi.unstubAllEnvs()
    vi.resetModules()
  })
}

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

// ---------------------------------------------------------------- StepCard

function wfStep(over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id: 'build', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, ...over }
}

const CAUSE = [
  'build failed: the agent exited with code 2',
  'step 1: read the pool table',
  'step 2: wrote the queue note',
  'step 3: the checkpoint upload was refused',
  'step 4: the agent stopped',
].join('\n')

function failedRow(): StepRowModel {
  return {
    step: wfStep(),
    taskId: 'tsk_fail',
    task: null,
    look: { tone: 'live', word: 'failed', title: 'failed', mark: 'failed', hue: 'bad' },
    times: { spans: [], mark: null, attempts: null, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: '' },
    ran: absentCell({ text: '—', note: '' }),
    attempts: measuredCell('3 attempts', ''),
    attemptsOver: 0,
    cost: absentCell({ text: '—', note: '' }),
    tokens: absentCell({ text: '—', note: '' }),
    costFrom: null,
    tokensFrom: null,
    result: null,
    pending: false,
    inputs: { declared: new Map(), stray: [], malformed: 0 },
    why: { kind: 'cause', text: 'build failed', full: CAUSE, warn: true },
    sort: { order: 0, stateRank: null, waitedMs: null, ranMs: null, attempts: null, costUsd: null, tokens: null },
  } as unknown as StepRowModel
}

const attemptsLoader: AttemptLoader = () =>
  Promise.resolve({
    status: 'ok',
    fetchedAt: 0,
    data: { attempts: [1, 2, 3].map((n) => attempt(n, { created_at: at(n), started_at: at(n), completed_at: at(n + 1), exit_code: 2 })) },
  } as Awaited<ReturnType<AttemptLoader>>)

/** A failed step with a five-line cause. Clicks: `details.sc-more > summary` opens `details.sc-more[open]`; `.sc-err button[aria-expanded]` ("Show all") sets it true. */
export const mountStepCard = (): ReactElement => (
  <StepCard
    workflowId="wf_1"
    row={failedRow()}
    task={task({ id: 'tsk_fail', state: 'FAILED', started_at: at(1), completed_at: at(4), attempt_count: 3, max_attempts: 3, last_error: CAUSE })}
    taskState="FAILED"
    changes={null}
    now={Date.parse(at(10))}
    load={attemptsLoader}
    siblings={[
      { workflowId: 'wf_1', mark: 'failed', hue: 'bad', word: 'failed' },
      { workflowId: 'wf_0', mark: 'succeeded', hue: 'ok', word: 'succeeded' },
    ]}
    siblingIndex={0}
    onSibling={() => undefined}
    stop={null}
    retry={<button>Retry</button>}
    hasExtras
    extras={<section aria-label="Review verdict">Reviewed · approved</section>}
  />
)

// ---------------------------------------------------------------- RunFiles

const PREFIX = 'tenants/eng/tasks/tsk_files/attempts/att_1/checkpoints/ckpt-00001'

function record(): CheckpointRecord {
  return {
    checkpoint_id: 'ckpt-00001',
    attempt_id: 'att_1',
    attempt_known: true,
    attempt_created_at: at(1),
    attempt_completed_at: at(5),
    prefix: PREFIX,
    uri: `gs://swarm-artifacts-example-project/${PREFIX}/`,
    is_latest_pointer: true,
    objects: [
      { name: 'archive.tar.gz', key: `${PREFIX}/archive.tar.gz`, bytes: 4096, object_updated_at: at(4) },
      { name: 'manifest.json', key: `${PREFIX}/manifest.json`, bytes: 312, object_updated_at: at(4) },
    ],
    stored_bytes: 4408,
    manifest: 'present',
    manifest_detail: null,
    created_at: at(4),
    seq: 1,
    generation: 1,
    label: 'periodic',
    archive_bytes: 4096,
    archive_sha256: null,
    file_count: 4,
    resumable: true,
    resumable_detail: null,
  } as unknown as CheckpointRecord
}

function page(): CheckpointsPage {
  return {
    task_id: 'tsk_files',
    tenant_id: 'eng',
    prefix: PREFIX,
    checkpoints: [record()],
    count: 1,
    total_found: 1,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: `gs://swarm-artifacts-example-project/${PREFIX}/`, status: 'present', checkpoint_id: 'ckpt-00001' },
  } as unknown as CheckpointsPage
}

/**
 * One checkpoint with two objects. Click `button.copy[aria-expanded]` whose
 * text is "2 objects": `tr.rf-object` rows appear. Click the one whose text is
 * "files": `.ckb[role="region"]` (the checkpoint browser) appears.
 */
export const mountRunFiles = (): ReactElement => {
  const listing: CheckpointListing = { task: 'tsk_files', state: ok(page()), asked: [attempt(1, { checkpoints: ['ckpt-00001'] })] }
  return <RunFiles task={task({ id: 'tsk_files', state: 'SUCCEEDED' })} listing={listing} now={Date.parse(at(10))} />
}

// ----------------------------------------------------------------- Diagram

/** A source that parses, drawn with mermaid's `render` replaced (jsdom lays nothing out). Open: `details.dg-text > summary`. */
export function mountDiagram(): ReactElement {
  const spy = vi.spyOn(mermaid, 'render').mockImplementation(
    async (id: string) =>
      ({
        svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40"><rect width="100" height="40" fill="#fff"/><text x="4" y="20">Plan</text></svg>',
        diagramType: 'flowchart-v2',
        bindFunctions: undefined,
        id,
      }) as unknown as Awaited<ReturnType<typeof mermaid.render>>,
  )
  restores.push(() => spy.mockRestore())
  const source = ['flowchart LR', '  A[Plan] -->|approve| B[Build]', '  B --> C[Review]', '  C -->|changes| A', '  C --> D[Merge]'].join('\n')
  return <DiagramView source={source} title="Plan flow" theme="light" />
}

// ---------------------------------------------------------- QA dismissed

type Json = Record<string, unknown>
const GROUP = 'qrg_' + '5e'.repeat(8)
const TARGET = 'qat_' + '2c'.repeat(8)
const WF = 'wf_' + 'a1'.repeat(10)
const FOUND = [{ id: 'beta-reports', path: '/beta/reports', workflow_id: WF }]

/** The QA run page, live, with one dismissed route. Click `.qr-dismissed .qr-linkish` ("Show"): `.qr-dismissed-list` appears (once the page has read; the page draws `.qr-dismissed` after its reads). */
export async function mountQaDismissed(): Promise<ReactElement> {
  const wf: Json = {
    workflow: { workflow_id: WF, state: 'SUCCEEDED', tenant_id: 'eng', created_at: '2026-10-23T21:12:01+00:00', steps: [{ step_id: 'report' }] },
    tasks: [{ id: `task_${WF}_report`, step_id: 'report', workflow_id: WF, state: 'SUCCEEDED', tenant_id: 'eng' }],
  }
  const target: Json = {
    qa_target: {
      id: TARGET, tenant_id: 'eng', name: 'Studio', revision: 4,
      screens: [{ id: 'home', path: '/' }],
      environments: [{
        name: 'staging', host: 'staging.example.test', environment: 'non_production',
        discovered: FOUND.map((f) => ({ ...f, first_seen: '2026-10-23T21:40:00+00:00' })),
        new_since_last_run: FOUND.map((f) => ({ ...f, first_seen: '2026-10-23T21:40:00+00:00' })),
      }],
      dismissed_routes: [{ id: 'old-print', path: '/print/preview', by: 'riley@example.com', at: '2026-10-22T09:40:00+00:00' }],
    },
  }
  const answers: Record<string, Json> = {
    [`/v1/qa-runs/${GROUP}`]: {
      qa_run: {
        group_id: GROUP, tenant_id: 'eng', target_id: TARGET, revision: 4, file: 'issues', browser_tool: null, schedule_id: null,
        by: 'vera@example.com', at: '2026-10-23T21:12:00+00:00',
        legs: [{ environment: 'staging', state: 'submitted', workflow_id: WF, browser_steps: ['capture-g1'], reason: null, workflow_state: 'SUCCEEDED' }],
      },
    },
    [`/v1/workflows/${WF}`]: wf,
    [`/v1/qa-targets/${TARGET}`]: target,
    '/v1/tenants/me': {
      tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
      principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
      environment: 'dev', environment_declared: true, role: { name: 'admin', source: 'override' },
    },
  }
  stubLive((m, url) => {
    const body = m === 'GET' ? answers[url.split('?')[0] ?? ''] : undefined
    return body === undefined ? null : { status: 200, body }
  })
  const { QaRunGroupScreen } = await import('../QaRunGroup')
  return <QaRunGroupScreen groupId={GROUP} go={() => undefined} />
}

// ----------------------------------------------------------- WorkflowFacts

const PURPOSE = Array.from({ length: 12 }, (_, i) => `Part ${i + 1} reads the pool table and notes the queue depth of pool ${i + 1} for the weekly capacity review.`).join(' ')

function workflowRow(over: Partial<Workflow> = {}): Workflow {
  const iso = (s: number) => at(s)
  return {
    workflow_id: 'wf_broker',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 2 },
    created_at: iso(1),
    updated_at: iso(2),
    submitted_by: 'dev@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps: [
      { step_id: 'plan', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 't_plan' },
      { step_id: 'core', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['plan'], input_from: {}, task_id: 't_core' },
    ],
    ...over,
  } as unknown as Workflow
}

/**
 * A long description. The card draws `.wff-more` only when the clamp cut text,
 * which jsdom cannot measure, so `scrollHeight` is stubbed taller than
 * `clientHeight` for `.wff-clip` until restored. Click `.wff-more`: it reads
 * `aria-expanded="true"`.
 */
export function mountWorkflowFacts(): ReactElement {
  const proto = HTMLElement.prototype
  const had = Object.getOwnPropertyDescriptor(proto, 'scrollHeight')
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('wff-clip') ? 200 : 0
    },
  })
  restores.push(() => {
    if (had) Object.defineProperty(proto, 'scrollHeight', had)
    else delete (proto as unknown as Record<string, unknown>).scrollHeight
  })
  const w = workflowRow({
    title: 'Capacity review',
    description: PURPOSE,
    description_source: 'submitted',
    started_from: 'console',
    repository_url: null,
  } as Partial<Workflow>)
  return <WorkflowFacts workflow={w} taskById={null} started={null} now={Date.parse(at(10))} />
}

// ----------------------------------------------------------------- Cancel

const refusedStop = async (): Promise<Result<CancelWorkflowResult>> => ({
  status: 'error',
  error: { kind: 'server_error', httpStatus: 500, code: 'internal', message: 'workflow store unavailable' },
})

/** t_core is the step the `taskById` holds: RUNNING holds capacity, QUEUED does not. */
function steps(core: 'RUNNING' | 'QUEUED'): ReadonlyMap<string, Task> {
  return new Map<string, Task>([
    ['t_plan', task({ id: 't_plan', state: 'SUCCEEDED', workflow_id: 'wf_broker', step_id: 'plan' })],
    ['t_core', task({ id: 't_core', state: core, workflow_id: 'wf_broker', step_id: 'core', started_at: core === 'RUNNING' ? at(2) : null })],
  ])
}

/** No step runs, so the two-click confirm. Clicks: "Stop workflow", then "Yes, stop the workflow": `.wfp-confirm-err` appears (the refusal). */
export const mountCancelRefused = (): ReactElement => (
  <CancelWorkflow workflow={workflowRow()} taskById={steps('QUEUED')} onDone={() => undefined} cancel={refusedStop} />
)

/** A step runs, so the typed confirm. Click "Stop workflow": `[role="dialog"]` (TypedConfirm) appears. */
export const mountCancelTyped = (): ReactElement => (
  <CancelWorkflow workflow={workflowRow()} taskById={steps('RUNNING')} onDone={() => undefined} cancel={refusedStop} />
)

// ------------------------------------------------------------------ Pause

const refusedPause = async (): Promise<Result<PauseWorkflowResult>> => ({
  status: 'error',
  error: { kind: 'server_error', httpStatus: 500, code: 'internal', message: 'workflow store unavailable' },
})

/** The role gate reads the workspace role and leaves an unknown role enabled, so no stub. Click "Pause workflow": `.wfp-confirm-err` appears. */
export const mountPauseRefused = (): ReactElement => (
  <PauseWorkflow workflow={workflowRow()} onDone={() => undefined} pause={refusedPause} />
)

// -------------------------------------------------------------- Questions

const QID = 'task_e376006b452b4cd38df7'

/**
 * The questions card, live, with one unanswered question the caller may
 * answer. Clicks: `.qc-pick` whose text starts "per workflow", then the
 * "Answer" button: the POST answers with the saved answer and `.qc-saved`
 * appears.
 */
export async function mountQuestionsAnswered(): Promise<ReactElement> {
  const q = {
    question: 'Per task or per workflow?',
    context: 'The step writes one file today.',
    recommended: 'per task',
    options: [
      { label: 'per task', description: 'one file per task' },
      { label: 'per workflow', description: 'one file for the run' },
    ],
  }
  const base = { task_id: QID, count: 1, status: 'ok', attempt_id: 'att_1', redacted: false, redaction_count: 0, may_answer: true }
  const saved = { question_index: 0, question: q.question, choice: 'per workflow', text: null, by: 'owner@example.com', at: new Date().toISOString(), history: [] }
  stubLive((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'GET' && path === `/v1/tasks/${QID}/questions`) return { status: 200, body: { ...base, questions: [q] } }
    if (m === 'POST' && path === `/v1/tasks/${QID}/answers`) return { status: 200, body: { ...base, questions: [{ ...q, answer: saved }] } }
    return null
  })
  const { QuestionsCard } = await import('../QuestionsCard')
  const t = task({
    id: QID, state: 'SUCCEEDED', workflow_id: 'wf_studio', step_id: 'design', submitted_by: 'owner@example.com',
    result_summary: { artifacts: [{ name: 'questions.json' }], questions: 1 },
  })
  return <QuestionsCard task={t} />
}
