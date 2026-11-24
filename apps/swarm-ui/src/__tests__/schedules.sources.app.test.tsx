// The sources view (?proposes=1) reached through the real App: the router must
// keep the query, and a click on Proposing must keep it too.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { serve } from './repofixture'
import { holdScheduleClock } from './scheduleclock'

const WAIT = { timeout: 4000 }

const row = (id: string, type: string) => ({
  schedule_id: id, name: `Schedule ${id}`, type, tier: 'R1', tenant_id: 'eng', params: {}, owner: 'person@example.com',
  created_by: 'person@example.com', scope: { mode: 'all' }, cron: '0 6 * * *', timezone: 'UTC', words: 'At 06:00',
  gate: { plan: 'approve' }, budget: {}, policy: { overlap: 'skip', catch_up: 'latest', dry_run: false }, state: 'enabled',
  state_word: 'Enabled', pause: null, last_firing: null, consecutive_failures: 0, pending_approvals: 0, revision: 1,
  created_at: '2026-11-01T00:00:00Z', next_run_at: null,
  spend_today: { day: '2026-11-24', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
})
const type = (name: string, proposes: boolean) => ({
  name, description: name, params_schema: {}, default_gate: {}, floor_gate: {}, min_interval_minutes: 60,
  budget_caps: null, scopes: ['repos'], available: true, disabled_reason: null, proposes,
})

beforeEach(() => holdScheduleClock())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('the sources view through the App', () => {
  it('keeps ?proposes=1 on the address and draws only the proposing sources', async () => {
    serve((m, url) => {
      const path = url.split('?')[0]
      if (m !== 'GET') return null
      if (path === '/v1/tenants/me') {
        return { status: 200, body: { tenant: { tenant_id: 'eng', kind: 'group' }, principal: { email: 'person@example.com', domain: 'example.com', groups: [], is_admin: false } } }
      }
      if (path === '/v1/schedules') return { status: 200, body: { schedules: [row('sch_a', 'sweep'), row('sch_b', 'cost')], tenant_id: 'eng' } }
      if (path === '/v1/schedule-types') return { status: 200, body: { types: [type('sweep', true), type('cost', false)] } }
      if (path === '/v1/repositories') return { status: 200, body: { repositories: [] } }
      if (path === '/v1/approvals') return { status: 200, body: { approvals: [] } }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    window.history.replaceState(null, '', '/automate/schedules?proposes=1')
    const { App } = await import('../App')
    render(<App />)
    // MUTATION: 'proposes' missing from keptKeys: the router strips the query and the full list is drawn.
    expect(await screen.findByRole('region', { name: 'Schedule sch_a' }, WAIT)).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Schedule sch_b' })).toBeNull()
    expect(window.location.search).toContain('proposes=1')
    fireEvent.click(within(await screen.findByRole('navigation', { name: 'Which schedules' }, WAIT)).getByRole('link', { name: 'Proposing' }))
    expect(window.location.search).toContain('proposes=1')
    expect(await screen.findByRole('region', { name: 'Schedule sch_a' }, WAIT)).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Schedule sch_b' })).toBeNull()
  })
})
