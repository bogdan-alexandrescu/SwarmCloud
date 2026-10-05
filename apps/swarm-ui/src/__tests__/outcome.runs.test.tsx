// ISSUE #646 (owner decision, 2026-10-05): an issue run whose build found the
// work already on main ends DONE with `outcome: already_on_main` rather than
// FAILED. The run page says so beside the state: a DONE run with that outcome
// reads "Already on main", and its state line says whether the issue was
// closed (every planned requirement met) or left open with the table.
//
// MUTATIONS: drop the label, or render it for every DONE run, or let the
// state line say "The workflow succeeded." for an already-on-main run -- each
// goes red here.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { RunOutcome, runOutcomeLabel, runStateLine } from '../Runs'
import type { IssueRun } from '../types'

function run(over: Record<string, unknown> = {}): IssueRun {
  return {
    id: 'run_646', tenant_id: 'eng', state: 'DONE', terminal: true,
    issue: { ref: 'saga-xyz/widgets#42', owner: 'saga-xyz', repo: 'widgets', number: 42, url: 'https://github.com/saga-xyz/widgets/issues/42' },
    plan_approval: 'auto', auto_merge: false, fix_rounds: 3, planner_task_id: 'tsk_p',
    plan: null, plan_digest: null, plan_revision: 1, plan_edited_by: null, workflow_id: 'wf_1',
    created_by: 'alice@saga.xyz', created_at: '2026-10-05T10:00:00Z', updated_at: '2026-10-05T11:00:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null,
    rejection_reason: null, error: null, history: [],
    ...over,
  } as unknown as IssueRun
}

describe('an issue run that found its work already on main', () => {
  it('labels the outcome', () => {
    render(<RunOutcome run={run({ outcome: 'already_on_main', requirements_met: true })} />)
    expect(screen.getByText('Already on main')).toBeTruthy()
    expect(runOutcomeLabel(run({ outcome: 'already_on_main' }))).toBe('Already on main')
  })

  it('shows no label for a run that opened a pull request', () => {
    const { container } = render(<RunOutcome run={run({ outcome: null, green_sha: 'a'.repeat(40) })} />)
    expect(container.textContent).toBe('')
    expect(runOutcomeLabel(run())).toBeNull()
  })

  it('says the issue was closed only when every requirement was met', () => {
    expect(runStateLine(run({ outcome: 'already_on_main', requirements_met: true })))
      .toMatch(/already on main.*closed/i)
    const open = runStateLine(run({ outcome: 'already_on_main', requirements_met: false }))
    expect(open).toMatch(/stays open/i)
    expect(open).not.toMatch(/workflow succeeded/i)
  })
})
