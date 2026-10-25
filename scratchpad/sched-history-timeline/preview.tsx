import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import '@fontsource-variable/inter-tight'
import '@fontsource/dm-mono/400.css'
import '@fontsource/dm-mono/500.css'
import '../../apps/swarm-ui/src/styles.css'
import { applyTheme } from '../../apps/swarm-ui/src/theme'
import { SchedulesScreen } from '../../apps/swarm-ui/src/Schedules'

const ago = (m: number) => new Date(Date.now() - m * 60_000).toISOString()
type J = Record<string, unknown>
const schedule: J = {
  schedule_id: 'sch_6ecbe1307511', tenant_id: 'eng', name: 'Weekday issue sweep', type: 'issue-sweep',
  scope: { mode: 'repos', repo_ids: ['repo_a'] }, cron: '0 9 * * 1-5', timezone: 'Europe/London',
  params: { max_new_per_firing: 3 }, gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
  budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 }, policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
  state: 'enabled', pause: null, owner: 'operator@example.com', created_by: 'operator@example.com', created_at: ago(20000),
  next_run_at: ago(-600), last_firing: null, consecutive_failures: 0, revision: 4, words: 'at 09:00, Monday through Friday', tier: 'R2',
  spend_today: { day: '2026-10-25', reported_usd: 41.2, unreported_attempts: 1, coverage: 'partial' }, pending_approvals: 1,
}
const D = 1440
function firing(n: number, at: number, over: J = {}): J {
  return { firing_id: `sch_6ecbe1307511:${n}`, schedule_id: 'sch_6ecbe1307511', type: 'issue-sweep', slot: ago(at), fired_at: ago(at),
    trigger: 'cron', state: 'done', work: [], skip: null, dry_run: null, approval_id: null, outcome: 'succeeded',
    cost: { reported_usd: 9.8, unreported_attempts: 0 }, ended_at: ago(at - 120), ...over }
}
const firings = [
  firing(12, 150, { state: 'created', outcome: null, ended_at: null, cost: null, work: [
    { kind: 'issue_run', id: 'run_8f2a' }, { kind: 'issue_run', id: 'run_51c0' }, { kind: 'issue_run', id: 'run_77b3' }, { kind: 'api_action', id: 'label:in-progress' }] }),
  firing(11, D + 150, { work: [{ kind: 'task', id: 'tsk_0d9e41aa' }], cost: { reported_usd: 0.6, unreported_attempts: 0 } }),
  firing(10, 2 * D + 150, { work: [], outcome: 'skipped', skip: { code: 'OVERLAP' }, cost: null }),
  firing(9, 3 * D + 150, { work: [], outcome: null, cost: null, skip: { code: 'NOTHING_STARTED', detail: { reason: 'no_room', bound: 'max_live_runs', own_live: { running: 8, limit: 8, bound: 'max_live_runs' } } } }),
  firing(8, 4 * D + 150, { work: [{ kind: 'workflow', id: 'wf_2e10c4' }], outcome: 'partial' }),
  firing(7, 5 * D + 150, { work: [{ kind: 'task', id: 'tsk_unread01' }] }),
  ...[6, 5, 4, 3, 2, 1].map((n) => firing(n, (13 - n) * D + 150, { work: [{ kind: 'task', id: `tsk_old0${n}` }] })),
]
function task(id: string, label: string, born: number, attempts: [number, number | null, number?][], over: J = {}): J {
  return { task_id: id, step_id: label, label, state: 'SUCCEEDED', created_at: ago(born), updated_at: ago(born - 60), completed_at: ago(attempts.length ? (attempts[attempts.length - 1]![1] ?? 0) : born),
    current_generation: attempts.length, park_reason: null, next_eligible_at: null,
    attempts: attempts.map(([s, e, code], i) => ({ attempt_id: `att_${id}_${i}`, task_id: id, generation: i + 1, created_at: ago(s), completed_at: e === null ? null : ago(e), exit_code: code ?? 0 })),
    attempts_truncated: false, attempts_unread: null, ...over }
}
const it = (kind: string, id: string, tasks: J[], over: J = {}): J => ({ kind, id, state: null, updated_at: null, tasks, unread: null, ...over })
const SPANS: Record<string, J[]> = {
  'sch_6ecbe1307511:12': [
    it('issue_run', 'run_8f2a', [task('tsk_plan8f2a', 'plan', 148, [[147, 140]]), task('tsk_fix8f2a', 'fix', 139, [[138, null]], { state: 'RUNNING', completed_at: null })], { state: 'RUNNING', updated_at: ago(139) }),
    it('issue_run', 'run_51c0', [task('tsk_plan51c0', 'plan', 148, [[146, 120]])], { state: 'PLANNED', updated_at: ago(119) }),
    it('issue_run', 'run_77b3', [task('tsk_plan77b3', 'plan', 148, [[147, 135]]), task('tsk_fix77b3', 'fix', 134, [[133, 110, 70], [108, 60, 1]], { state: 'FAILED', completed_at: ago(60) })], { state: 'FAILED', updated_at: ago(60) }),
    it('api_action', 'label:in-progress', [], { unread: 'an API action runs no attempt: it ended when it was recorded' }),
  ],
  'sch_6ecbe1307511:11': [it('task', 'tsk_0d9e41aa', [task('tsk_0d9e41aa', 'claude-code', D + 148, [[D + 146, D + 120, 1], [D + 115, D + 70]])])],
  'sch_6ecbe1307511:8': [it('workflow', 'wf_2e10c4', [
    task('tsk_wfbuild', 'build', 4 * D + 148, [[4 * D + 147, 4 * D + 100]]),
    task('tsk_wfreview', 'review', 4 * D + 99, [[4 * D + 98, 4 * D + 90]], { state: 'PARKED', completed_at: null, updated_at: ago(4 * D + 89), park_reason: 'QUOTA_EXHAUSTED' }),
  ], { state: 'RUNNING', updated_at: ago(4 * D + 89) })],
  'sch_6ecbe1307511:7': [it('task', 'tsk_unread01', [{ task_id: 'tsk_unread01', label: 'claude-code', attempts: null, attempts_unread: 'its attempts could not be read' }])],
}
for (const n of [6, 5, 4, 3, 2, 1]) {
  const born = (13 - n) * D + 148
  SPANS[`sch_6ecbe1307511:${n}`] = [it('task', `tsk_old0${n}`, [task(`tsk_old0${n}`, 'claude-code', born, [[born - 2, born - 30 - n * 5]])])]
}
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
globalThis.fetch = (async (input: RequestInfo | URL) => {
  const url = new URL(String(input), location.href)
  if (url.pathname === '/v1/schedules/sch_6ecbe1307511') return json(200, { schedule, firings })
  if (url.pathname === '/v1/schedules/sch_6ecbe1307511/spans') {
    const ids = url.searchParams.getAll('firing_id')
    await new Promise((r) => setTimeout(r, 150))
    return json(200, { firings: ids.filter((i) => SPANS[i]).map((i) => ({ firing_id: i, items: SPANS[i] })), not_found: ids.filter((i) => !SPANS[i]) })
  }
  if (url.pathname === '/v1/repositories') return json(200, { repositories: [{ repo_id: 'repo_a', owner: 'example-org', repo: 'example-api' }] })
  return json(404, { detail: 'Not Found' })
}) as typeof fetch
applyTheme('system')
function Page() {
  const [view, setView] = useState<string | null>('schedule=sch_6ecbe1307511')
  return <div style={{ padding: '16px 20px 140px', maxWidth: 1180 }}><SchedulesScreen view={view} go={(to) => { const q = to.indexOf('?'); if (to.startsWith('automate/schedules')) setView(q === -1 ? null : to.slice(q + 1)) }} /></div>
}
createRoot(document.getElementById('root')!).render(<Page />)
