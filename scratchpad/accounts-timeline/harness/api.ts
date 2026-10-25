// REVIEW HARNESS ONLY. Scenario from localStorage `harness.s`: b2, b3, b6.
export * from '../../../apps/swarm-ui/src/api'
import { loadTasksInState as realTasks } from '../../../apps/swarm-ui/src/api'
import type { AccountsBoard } from '../../../apps/swarm-ui/src/api'
import type { PoolAccount } from '../../../apps/swarm-ui/src/accountsPool'

const S = (globalThis.localStorage?.getItem('harness.s') ?? 'b2') as 'b2' | 'b3' | 'b6'
const NOW = Date.parse(S === 'b3' ? '2026-10-24T14:04:00Z' : '2026-10-24T09:04:00Z')
const iso = (s: string) => new Date(Date.parse(s)).toISOString()
const W = (used: number, resets: string, reset = false) => ({ utilization: used, resets_at: iso(resets), reset })

function acct(label: string, over: Partial<PoolAccount>): PoolAccount {
  return {
    account_id: `eng:${label}`, owner_tenant: 'eng', label, provider: 'anthropic', state: 'AVAILABLE', reason: '',
    lend_to: [], assigned: 0, windows: {}, observed_at: new Date(NOW - 3 * 60_000).toISOString(), stale: false,
    unreadable_by: [], unreadable_now: [], last_assigned_at: new Date(NOW - 3_600_000).toISOString(),
    max_agents: 6, can_take_new_agent: true, blocked_by: [], next_eligible_at: null, organisation: null, ...over,
  } as PoolAccount
}
const wk = (used: number, resets: string) => ({ can_take_new_agent: false, blocked_by: [{ window: 'seven_day', used, floor: 0.05, resets_at: iso(resets) }], next_eligible_at: iso(resets) })
const fh = (used: number, resets: string) => ({ can_take_new_agent: false, blocked_by: [{ window: 'five_hour', used, floor: 0.15, resets_at: iso(resets) }], next_eligible_at: iso(resets) })

const B2: PoolAccount[] = [
  acct('ai-eng-2', { windows: { seven_day: W(0.86, '2026-10-25T18:59:00Z'), five_hour: W(0.4, '2026-10-24T08:39:00Z', true) } }),
  acct('ai-eng-3', { ...wk(0.97, '2026-10-27T10:59:00Z'), windows: { seven_day: W(0.97, '2026-10-27T10:59:00Z'), five_hour: W(0.2, '2026-10-24T03:20:00Z', true) } }),
  acct('aieng-claude1', { ...fh(0.99, '2026-10-24T10:10:00Z'), windows: { seven_day: W(0.39, '2026-10-30T14:00:00Z'), five_hour: W(0.99, '2026-10-24T10:10:00Z') } }),
  acct('devops-main', { windows: { seven_day: W(0.88, '2026-10-26T14:59:00Z'), five_hour: W(0.3, '2026-10-24T04:59:00Z', true) } }),
  acct('devops-team', { windows: { seven_day: W(0.86, '2026-10-28T12:00:00Z'), five_hour: W(0.3, '2026-10-24T04:20:00Z', true) } }),
  acct('saga-personal', { ...wk(0.98, '2026-10-26T07:59:00Z'), organisation: { name: 'Personal', plan: 'Max' }, windows: { seven_day: W(0.98, '2026-10-26T07:59:00Z'), five_hour: W(0.3, '2026-10-24T06:00:00Z', true) } }),
  acct('team', {
    can_take_new_agent: false, blocked_by: [{ duplicate_of: 'eng:saga-personal', reason: 'registered on the same organisation as eng:saga-personal' }],
    duplicate_of: 'eng:saga-personal', organisation: { name: 'Personal', plan: 'Max' }, lend_to: ['smoke', 'u-bogdan'],
    windows: { seven_day: W(0.98, '2026-10-26T08:00:00Z'), five_hour: W(0.3, '2026-10-24T05:59:00Z', true), harbor_lantern: W(0.1, '2026-10-15T00:00:00Z', true) },
  }),
]
const B3: PoolAccount[] = [
  acct('ai-eng-2', { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }], windows: { seven_day: W(0.9, '2026-10-25T18:59:00Z'), five_hour: W(0.4, '2026-10-24T13:20:00Z', true) } }),
  acct('ai-eng-3', { ...wk(0.97, '2026-10-27T10:59:00Z'), windows: { seven_day: W(0.97, '2026-10-27T10:59:00Z'), five_hour: W(0.2, '2026-10-24T13:20:00Z', true) } }),
  acct('aieng-claude1', { ...fh(0.91, '2026-10-24T15:10:00Z'), windows: { seven_day: W(0.52, '2026-10-30T14:00:00Z'), five_hour: W(0.91, '2026-10-24T15:10:00Z') } }),
  acct('devops-main', { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }], windows: { seven_day: W(0.92, '2026-10-26T14:59:00Z'), five_hour: W(0.38, '2026-10-24T17:10:00Z') } }),
  acct('devops-team', { assigned: 5, organisation: { name: 'Saga', plan: 'Team' }, lend_to: ['smoke'], windows: { seven_day: W(0.89, '2026-10-28T12:00:00Z'), five_hour: W(0.34, '2026-10-24T17:20:00Z') } }),
  acct('saga-personal', { ...wk(0.98, '2026-10-26T07:59:00Z'), stale: true, observed_at: new Date(NOW - 41 * 60_000).toISOString(), organisation: { name: 'Personal', plan: 'Max' }, windows: { seven_day: W(0.98, '2026-10-26T07:59:00Z'), five_hour: W(0.3, '2026-10-24T11:00:00Z', true) } }),
  acct('saga-team', { ...fh(0.95, '2026-10-24T15:00:00Z'), organisation: { name: 'Saga', plan: 'Team' }, lend_to: ['smoke', 'u-bogdan'], windows: { seven_day: W(0.37, '2026-10-29T10:40:00Z'), five_hour: W(0.95, '2026-10-24T15:00:00Z') } }),
]
const ACCOUNTS = S === 'b3' ? B3 : S === 'b6' ? [] : B2
const WAITING = S === 'b3' ? 1 : S === 'b6' ? 3 : 28
const LATER = S === 'b2' ? 32 : 0

export async function loadAccountsBoard() {
  await new Promise((r) => setTimeout(r, 50))
  const data: AccountsBoard = {
    page: { accounts: ACCOUNTS, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 },
    tenants: null, tenantsDetail: 'Admin group membership is required for this endpoint.', readAt: Date.now(), fixture: true,
  } as AccountsBoard
  return { status: 'ok' as const, fetchedAt: Date.now(), data }
}

export async function loadTasksInState(state: string, options?: { limit?: number; view?: 'summary' }) {
  if (state !== 'PARKED') return realTasks(state as never, options)
  const tasks = [
    ...Array.from({ length: WAITING }, (_, i) => ({ task_id: `task_h${i}`, state: 'PARKED', workflow_id: `wf_h${i % 4}`, waiting: { reason: 'PROVIDER_QUOTA_EXHAUSTED', summary: 'No pool account can take an agent now' } })),
    ...Array.from({ length: LATER }, (_, i) => ({ task_id: `task_l${i}`, state: 'PARKED', workflow_id: `wf_h${i % 4}`, waiting: { reason: 'DEPENDENCY_INCOMPLETE', summary: 'waits on an earlier step' } })),
  ]
  return { status: 'ok' as const, fetchedAt: Date.now(), data: { tasks, next_page_token: null } } as never
}
