// WORK › RUNS, the issue sweeper (owner decisions 2026-10-08): a run the
// planner found NOT_READY shows its reason in the list, a run the sweep
// started says so in the By column, and an auto run held by the territory
// guard says why it waits. `IssueRun.to_api` serves `not_ready`, `hold`,
// `created_by: issue-sweep` and `on_behalf_of` (swarm_api/issueruns.py).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { issueRun } from './runfixture'
import { runCreatorWord, runStateWord, runWhyLine } from '../Runs'
import type { IssueRun } from '../types'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function serveRuns(runs: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const body = url.startsWith('/v1/runs')
      ? { runs, next_page_token: null, tenant_id: 'eng' }
      : { code: 'not_found', message: `no stub for ${url}` }
    return new Response(JSON.stringify(body), { status: url.startsWith('/v1/runs') ? 200 : 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mountList() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  return render(<RunsScreen view={null} go={vi.fn()} />)
}

afterEach(() => {
  vi.unstubAllEnvs()
})

const NOT_READY = {
  kind: 'blocked',
  reason: 'The sort path is rewritten by #612, which is still open.',
  needs: ['depends on #612'],
}

describe('the sweeper on the Runs list', () => {
  it('shows a NOT_READY run with its reason, and who started each run', async () => {
    serveRuns([
      issueRun({ id: 'run_nr', state: 'NOT_READY', terminal: true, plan: null, plan_digest: null,
        not_ready: NOT_READY, created_by: 'issue-sweep', on_behalf_of: 'alice@saga.xyz' }),
      issueRun({ id: 'run_person', state: 'FAILED', terminal: true, created_by: 'bob@saga.xyz' }),
    ])
    const { container } = await mountList()
    await screen.findByRole('heading', { name: 'Runs', level: 1 }, WAIT)
    const rows = await waitFor(() => {
      const r = [...container.querySelectorAll<HTMLElement>('.rn-row')]
      expect(r).toHaveLength(2)
      return r
    }, WAIT)
    expect(visible(rows[0]!)).toContain('Not ready')
    expect(visible(rows[0]!)).toContain('Not ready: The sort path is rewritten by #612')
    expect(visible(rows[0]!)).toContain('issue sweep, as alice@saga.xyz')
    expect(rows[0]!.querySelector('[data-sweep]')).not.toBeNull()
    // The control: a run a person started, and a state with no reason line.
    expect(visible(rows[1]!)).toContain('bob@saga.xyz')
    expect(visible(rows[1]!)).not.toContain('issue sweep')
    expect(rows[1]!.querySelector('.rn-why')).toBeNull()
  })
})

describe('the words', () => {
  const base = issueRun() as unknown as IssueRun

  it('says NOT_READY in sentence case', () => {
    expect(runStateWord('NOT_READY')).toBe('Not ready')
  })

  it('names the sweep, and a person by their address', () => {
    expect(runCreatorWord({ ...base, created_by: 'issue-sweep', on_behalf_of: null })).toBe('issue sweep')
    expect(runCreatorWord({ ...base, created_by: 'carol@saga.xyz' })).toBe('carol@saga.xyz')
    expect(runCreatorWord({ ...base, created_by: '' })).toBeNull()
  })

  it('says why a held auto run waits, and nothing for a run that is not held', () => {
    expect(runWhyLine({ ...base, state: 'PLANNED', hold: 'territory_overlap: run_a' }))
      .toBe('Waiting: territory_overlap: run_a')
    expect(runWhyLine({ ...base, state: 'PLANNED', hold: null })).toBeNull()
    expect(runWhyLine({ ...base, state: 'NOT_READY', not_ready: null })).toBe('Not ready: the planner gave no reason.')
  })
})
