// The work section's click-only states the fixture build never draws (issue
// 439): each screen mounted on hand-built props, so one click opens the
// state. Copied from the tests that build the same props: submit.picker (a
// catalogue with runners held back) and needsyou.collapse (a question the
// strip answers in place).

import type { ReactElement } from 'react'

import { RunnerPicker, type ProviderKeys } from '../RunnerPicker'
import type { RunnerProfile } from '../types'
import { NeedsYou, type NeedItem } from '../work/NeedsYou'

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
  } as RunnerProfile
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

/** Undo the await-input card's fetch stub: called after each state. */
export function restoreWorkStubs(): void {
  globalThis.fetch = realFetch
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
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).split('?')[0]
    const method = init?.method ?? 'GET'
    if (method === 'POST') answered = true
    const reply =
      method === 'POST' ? { status: 200, body: { state: 'SUCCEEDED' } } : url === INPUT_ROUTE ? { status: 200, body: stepView(answered) } : { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { 'content-type': 'application/json' } })
  }) as typeof fetch
  const { AwaitInputCard } = await import('../AwaitInputCard')
  return <AwaitInputCard workflowId="wf_r" stepId="review" />
}
