// The work section's click-only states the fixture build never draws (issue
// 439): each screen mounted on hand-built props, so one click opens the
// state. Copied from the tests that build the same props: submit.picker (a
// catalogue with runners held back) and needsyou.collapse (a question the
// strip answers in place).

import type { ReactElement } from 'react'
import { vi } from 'vitest'

import { RunnerPicker, type ProviderKeys } from '../RunnerPicker'
import type { RunnerProfile } from '../types'
import { NeedsYou, type NeedItem } from '../work/NeedsYou'
import { PinnedDiagrams } from '../PinnedDiagrams'
import type { Conversation } from '../PlanThread'

const KEYS: ProviderKeys = { kind: 'read', registered: new Map([['anthropic', true]]) }

function profile(over: Partial<RunnerProfile> = {}): RunnerProfile {
  return {
    resource_class: 'standard',
    backend: 'CLOUD_RUN_JOB',
    provider: 'anthropic',
    units: 1,
    pools: ['global', 'tenant:eng', 'resource:standard'],
    admission: { units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [], counterfactual: [], complete: true, unread: [], uncapped: [] },
    ...over,
  }
}

/** Three runners, two switched off: more than half held back, so the list folds them under "Other runners". */
export function mountHeldRunners(): ReactElement {
  const profiles: Array<[string, RunnerProfile]> = [
    ['claude-code', profile()],
    ['codex', profile({ provider: 'openai', available: false, disabled_reason: 'switched off. Use claude-code.' })],
    ['gemini', profile({ provider: 'google', available: false, disabled_reason: 'switched off. Use claude-code.' })],
  ]
  return <RunnerPicker group="ps" label="runner" profiles={profiles} chosen="" keys={KEYS} onPick={() => undefined} />
}

const QUESTION: NeedItem = {
  kind: 'question',
  taskId: 'task_q',
  index: 0,
  title: 'design',
  question: 'Which schema?',
  options: [
    { label: 'v1', description: 'keep it' },
    { label: 'v2', description: 'move on' },
  ],
  recommended: 'v2',
}

/** The strip with one question an agent asked, answered in place. */
export function mountQuestion(): ReactElement {
  return <NeedsYou items={[QUESTION]} approvals={null} />
}

// -------------------------------------------------------------- await-input

const AT = '2026-10-18T10:00:00.000Z'
const INPUT_ROUTE = '/v1/workflows/wf_r/steps/review/input'
const realFetch = globalThis.fetch

/** Undo the mounts' fetch stubs and the live build: called after each state. */
export function restoreWorkStubs(): void {
  globalThis.fetch = realFetch
  document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
  vi.doUnmock('../api')
  vi.resetModules()
}

const json = (status: number, body: unknown): Promise<Response> =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }))

/** The API's refusal of a workflow whose contract Jev found unenforceable (contract.jev.test.tsx's REFUSAL). */
export const WORKFLOW_REFUSAL_MESSAGE = 'contract wc-example is strict, and Jev found requirements no worker could enforce: REQ-2, REQ-4'

/**
 * The workflow form with its submit sent live and refused with a 422 (the
 * fixture build creates every workflow), as contract.jev.test.tsx does; its
 * other reads stay the fixture build's.
 */
export async function mountWorkflowRefused(): Promise<ReactElement> {
  vi.resetModules()
  vi.doMock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), USE_FIXTURES: false }))
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = input instanceof Request ? input.url : input.toString()
    if ((init?.method ?? 'GET') === 'POST' && url.startsWith('/v1/workflows')) {
      return json(422, {
        code: 'validation_failed',
        message: WORKFLOW_REFUSAL_MESSAGE,
        detail: {
          reason: 'work_contract_not_enforceable',
          contract_id: 'wc-example',
          revision: 1,
          digest: 'ab'.repeat(32),
          unenforced: [],
          jev: {
            call_id: 'call-77777777',
            backend: 'internal-jev',
            requirements: [
              { id: 'REQ-2', class: 'unenforceable', reason: 'vague_outcome', p: 0.83 },
              { id: 'REQ-4', class: 'unenforceable', reason: 'depends_on_a_later_event', p: 0.71 },
            ],
          },
        },
      })
    }
    return realFetch(input, init)
  }
  const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')
  return <SubmitWorkflowScreen />
}

/** The step's view, as swarm_api/awaitinput.py's `step_view` writes it (awaitinput.card.test.tsx). */
function stepView(answered: boolean): Record<string, unknown> {
  return {
    workflow_id: 'wf_r',
    step_id: 'review',
    task_id: 'task_review',
    status: answered ? 'answered' : 'waiting',
    spec_verified: true,
    prompt: 'Read draft.md and approve it.',
    form: 'verdict',
    artifact: 'review.json',
    max_files: 0,
    max_answer_bytes: 1_000_000,
    tenant: 'eng',
    may_answer: !answered,
    remind: [],
    opened_at: AT,
    expires_at: null,
    on_expire: null,
    shows: [],
    questions: null,
    answer: answered ? { answered_by: 'owner@example.com', answered_at: AT, via: 'console', bytes: 120, files: [] } : null,
  }
}

/** An await-input step waiting on a verdict; once an answer is posted, the step reads back answered. */
export async function mountAwaitInput(): Promise<ReactElement> {
  let answered = false
  const reply = (status: number, body: unknown): Promise<Response> =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }))
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = (input instanceof Request ? input.url : input.toString()).split('?')[0]
    if ((init?.method ?? 'GET') === 'POST') {
      answered = true
      return reply(200, { state: 'SUCCEEDED' })
    }
    return url === INPUT_ROUTE ? reply(200, stepView(answered)) : reply(404, { detail: 'Not Found' })
  }
  const { AwaitInputCard } = await import('../AwaitInputCard')
  return <AwaitInputCard workflowId="wf_r" stepId="review" />
}

/** A workspace request the Tenants page's "Being created" list draws (tenants.test.tsx's `ws`). */
const REQUESTED = {
  state: 'requested',
  kind: 'group',
  setup_url: '/setup#workspace',
  setup_command: '/sc:setup',
  workspace_id: 'w-group1',
  display_name: 'Platform team',
  principal: 'platform@example.com',
  requested_at: '2026-10-19T08:00:00Z',
  decision: null,
  held: null,
  limits: { max_active: 8, capacity_units: 8 },
  steps: {},
  failure: null,
}

/**
 * Admin › Tenants with one workspace request and the platform owner signed
 * in (the fixture build lists none): Approve answers with no message, so the
 * page says what it did in its own words. The roster stays the fixture build's.
 */
export async function mountTenantsRequested(): Promise<ReactElement> {
  // The platform admin's fixture build (proseapp.ts useBase sets the same cookie), read when api.ts loads.
  document.cookie = 'swarm_qa_persona=platform-admin; path=/'
  vi.resetModules()
  vi.doMock('../api', async (importOriginal) => {
    const real = await importOriginal<typeof import('../api')>()
    const ok = <T,>(data: T) => Promise.resolve({ status: 'ok' as const, data, fetchedAt: Date.now() })
    return {
      ...real,
      loadProfile: () =>
        ok({
          identity: { email: 'someone@example.com', display_name: null, display_name_suggested: null, domain: 'example.com', platform_role: 'owner', is_admin: true, is_admin_unresolved: false },
          workspaces: [],
          revision: 1,
          unavailable: [],
        }),
      loadAdminWorkspaces: () => ok([REQUESTED]),
      approveWorkspace: () => ok({ workspace: REQUESTED }),
    }
  })
  const { TenantsScreen } = await import('../Activity')
  return <TenantsScreen />
}

// ------------------------------------------------------------ diagrams off

const NO_THREAD: Conversation = {
  runId: 'run_d', thread: [], revisions: [], complete: true, replan: null, may: { comment: false, edit: false },
  mayDiagram: false, diagramsOn: false, current: null, pinned: null, tab: null, capped: null,
  addEntry: () => undefined, setReplan: () => undefined, setPinned: () => undefined, showTab: () => undefined, setCapped: () => undefined,
}

/** The plan page's diagrams strip for a run planned with diagrams off: its one-line chip, the reason behind its (i). */
export function mountDiagramsOff(): ReactElement {
  return <PinnedDiagrams plan={{}} run={{ plan_settings: { diagrams: false, from: 'workspace' } }} c={NO_THREAD} shownRevision={1} />
}

/** The strip with one pinned diagram; Open large draws it in a dialog (the diagram's source is drawn by the renderer, here a short flowchart). */
export function mountPinnedDiagram(): ReactElement {
  const plan = { diagrams: [{ diagram_id: 'd-one', kind: 'flow', title: 'Overlap read', source: 'flowchart TD\n  A --> B', explains: { part: 'plan' } }] }
  return <PinnedDiagrams plan={plan} run={{ plan_settings: { diagrams: true, from: 'workspace' } }} c={NO_THREAD} shownRevision={1} />
}
