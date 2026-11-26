// Shared example data for the plan thread and revision tests (lane U4
// PLAN-THREAD-UI, docs/design/plans-pipeline.md §2.3, §3.3-§3.5, §5). Every
// name, address and repository here is invented; nothing is token-shaped.

import { vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { serve } from './repofixture'

export const WAIT = { timeout: 4000 }

export type Json = Record<string, unknown>
export type Reply = { status: number; body: unknown } | null
export type Handler = (m: string, url: string, body: unknown) => Reply

export const at = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString()
export const RUN = 'run_4e1f0a0b0c0d41a2b3c4'
export const ITEM = `run:${RUN}`
export const D1 = 'sha256:' + '0f'.repeat(32)
export const D2 = 'sha256:' + 'ab12'.repeat(16)
export const D3 = 'sha256:' + 'cd34'.repeat(16)

export const PLAN = {
  summary: 'Serve each run lane’s overlaps on its plan read.',
  rationale: 'Plans are approved blind to the merge train.',
  estimate: '3 h',
  requirements: ['A run’s plan lists each train PR that shares a file', 'The read answers [] when none'],
  steps: [
    {
      step_id: 'overlaps-read',
      title: 'Serve plan.overlaps from the run read',
      prompt: 'Add overlap_refs() to plancontext.py and serve it from the run read.',
      files: ['apps/swarm-api/swarm_api/plancontext.py'],
      tests: ['tests/unit/control_plane/test_runs_overlap_ref.py'],
      estimate: '2 h',
    },
    {
      step_id: 'iam-binding',
      title: 'Grant the planner read on the train',
      prompt: 'Bind the planner’s account to the train bucket.',
      files: ['terraform/infra/iam.tf'],
      estimate: '1 h',
    },
  ],
}

/** Revision 1: the planner's first plan, before the hand edit made revision 2. */
export const PLAN_R1 = {
  ...PLAN,
  steps: [{ ...PLAN.steps[0]!, prompt: 'Add overlap_refs() to plancontext.py.' }, PLAN.steps[1]!],
}

/** Revision 3: the planner's answer to a request for changes. */
export const PLAN_R3 = {
  ...PLAN,
  summary: 'Serve overlaps on the plan read, cached per digest.',
  steps: [PLAN.steps[0]!, { ...PLAN.steps[1]!, files: ['terraform/infra/iam.tf', 'terraform/infra/buckets.tf'] }],
}

export const REV2 = { revision: 2, digest: D2, replaces: D1, author: { kind: 'person', by: 'ana@example.com' }, made_by: 'edit', answers: [], changed: [{ path: 'steps.0.prompt', op: 'replace' }], cost: null, at: at(-60) }
export const REV1 = { revision: 1, digest: D1, replaces: null, author: { kind: 'planner', by: 'planner' }, made_by: 'plan', answers: [], changed: [], cost: { usd: 2.4, task_id: 'task_planner1', model_class: 'opus', why_none: null }, at: at(-120) }
export const REV3 = { revision: 3, digest: D3, replaces: D2, author: { kind: 'planner', by: 'planner' }, made_by: 'revise', answers: ['pth_open1'], changed: [{ path: 'summary', op: 'replace' }], cost: { usd: 1.8, task_id: 'task_replan1', model_class: 'opus', why_none: null }, at: at(1) }

export function comment(over: Json = {}): Json {
  return {
    entry_id: 'pth_open1',
    kind: 'comment',
    anchor: { part: 'step', ref: 'iam-binding' },
    by: 'ben@example.com',
    text: 'Does the planner need write on the bucket too?',
    on_digest: D2,
    on_revision: 2,
    answered_by_revision: null,
    resolved: false,
    diagram: null,
    cost: null,
    at: at(-30),
    ...over,
  }
}

export function item(over: Json = {}): Json {
  return {
    approval_id: ITEM,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: RUN, issue: 'example-org/example-api#12' },
    digest: D2,
    summary: 'example-org/example-api#12: serve overlaps on the plan read',
    approvers: 'members',
    state: 'pending',
    requested_at: at(-60),
    expires_at: at(47 * 60),
    may_decide: { approve: true, reject: true, why: null, deciders: ['members'], edit: true, comment: true },
    ...over,
  }
}

export function runRead(over: Json = {}): Json {
  return {
    id: RUN,
    state: 'PLANNED',
    plan: PLAN,
    plan_digest: D2,
    plan_revision: 2,
    plan_revisions: [REV2, REV1],
    plan_history_complete: true,
    plan_thread: [
      { kind: 'event', event: 'planned', at: REV1.at, ref: null, revision: 1, digest: D1, by: 'planner', made_by: 'plan' },
      { kind: 'event', event: 'revised', at: REV2.at, ref: null, revision: 2, digest: D2, by: 'ana@example.com', made_by: 'edit' },
      comment(),
    ],
    thread_truncated: false,
    plan_replan: null,
    plan_replans: 0,
    may_decide: { approve: true, reject: true, why: null, deciders: ['members'], edit: true, comment: true },
    expected_cost_usd: { low: 3.1, high: 7.8, basis: '2 steps at their estimates', prices_at: at(0) },
    planner_documents: null,
    ...over,
  }
}

export const REVISION_PLANS: Record<number, { plan: Json; digest: string }> = { 1: { plan: PLAN_R1, digest: D1 }, 2: { plan: PLAN, digest: D2 }, 3: { plan: PLAN_R3, digest: D3 } }

export function routes(o: { item?: Json; run?: () => Json; extra?: Handler } = {}): Handler {
  return (m, url, body) => {
    const hit = o.extra?.(m, url, body) ?? null
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    const it = item(o.item ?? {})
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [it], tenant_id: 'eng' } }
    if (m === 'GET' && path === `/v1/approvals/${encodeURIComponent(ITEM)}`) return { status: 200, body: { approval: it, run: { id: RUN, state: 'PLANNED', plan: PLAN } } }
    if (m === 'GET' && path === `/v1/runs/${RUN}`) return { status: 200, body: { run: runRead(o.run?.() ?? {}) } }
    const rev = /^\/v1\/runs\/[^/]+\/plan-revisions\/(\d+)$/.exec(path)
    if (m === 'GET' && rev !== null) {
      const n = Number(rev[1])
      const r = REVISION_PLANS[n]
      return r === undefined ? null : { status: 200, body: { revision: { ...[REV3, REV2, REV1].find((x) => x.revision === n), plan: r.plan, digest: r.digest } } }
    }
    return null
  }
}

export async function mount(handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={`approval=${encodeURIComponent(ITEM)}`} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

export const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) => calls.filter((c) => c.method === method && c.url.split('?')[0] === path)
export const COMMENT = `/v1/runs/${RUN}/plan:comment`
export const REVISE = `/v1/runs/${RUN}/plan:revise`
