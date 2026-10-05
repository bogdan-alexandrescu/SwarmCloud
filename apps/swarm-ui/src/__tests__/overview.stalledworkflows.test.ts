/**
 * Overview "Needs a look": the reconciler's stalled workflows (#616).
 *
 * `GET /v1/workflows` carries `stalled_workflows`, the caller's rows from the
 * reconciler's latest workflow stall check (`swarm_api/stalls.py`). The
 * Workflows check turns them into a problem: the rows that still need a look,
 * worst first as the reconciler ranked them; a check whose result is not known
 * is a problem of its own, never a clear; and rows the reconciler repaired
 * are said in the note rather than raised.
 */
import { describe, expect, it } from 'vitest'

import { deriveChecks, type CheckInputs, type StalledWorkflows } from '../checks'
import type { Result } from '../fetch'
import type { WorkflowPage } from '../types'

const NOW = Date.parse('2026-10-05T12:00:00Z')
const empty = { status: 'empty', fetchedAt: NOW } as const

function row(workflow_id: string, kind: string, severity: string, extra: Record<string, unknown> = {}) {
  return {
    workflow_id,
    tenant_id: 'eng',
    step_id: 'review',
    task_id: `task-${workflow_id}`,
    kind,
    severity,
    age_seconds: 3000,
    reason: `${kind} reason`,
    repaired: false,
    repair: null,
    ...extra,
  }
}

function inputs(stalled: StalledWorkflows | undefined): CheckInputs {
  const page = {
    workflows: [],
    next_page_token: null,
    tenant_id: 'eng',
    ...(stalled === undefined ? {} : { stalled_workflows: stalled }),
  } as unknown as WorkflowPage
  const workflows: Result<WorkflowPage> = { status: 'ok', data: page, fetchedAt: NOW }
  return {
    capacity: empty, tasks: empty, leases: empty, providers: empty, accounts: empty, stats: empty,
    workflows,
  } as unknown as CheckInputs
}

function workflowsCheck(stalled: StalledWorkflows | undefined) {
  return deriveChecks(inputs(stalled), NOW).find((c) => c.label === 'Workflows')!
}

function report(rows: ReturnType<typeof row>[], over: Partial<StalledWorkflows> = {}): StalledWorkflows {
  return {
    count: rows.length,
    workflows: rows,
    truncated: false,
    scan_truncated: false,
    pass_at: '2026-10-05T11:59:00+00:00',
    check_error: null,
    ...over,
  } as StalledWorkflows
}

describe('the reconciler\'s stalled workflows on Overview', () => {
  it('raises the rows that need a look, naming the worst first', () => {
    const check = workflowsCheck(report([
      row('wf_stopped', 'no_progress', 'bad'),
      row('wf_slow', 'start_overdue', 'bad', { age_seconds: 900 }),
      row('wf_fixed', 'state_drift', 'note', { repaired: true, repair: 'stored state written as PARKED' }),
    ]))

    expect(check.status).toBe('found')
    if (check.status !== 'found') return
    const p = check.problems.find((x) => x.headline.includes('reconciler'))!
    expect(p.severity).toBe('bad')
    expect(p.n).toBe(2)
    expect(p.headline).toContain('wf_stopped')
    expect(p.detail).toContain('wf_slow')
    expect(p.detail).not.toContain('wf_fixed')
  })

  it('a check whose result is not known is a problem, not a clear', () => {
    const check = workflowsCheck(report([], { count: null, check_error: 'the check could not read: 503' }))

    expect(check.status).toBe('found')
    if (check.status !== 'found') return
    expect(check.problems[0]!.severity).toBe('warn')
    expect(check.problems[0]!.detail).toContain('503')
  })

  it('no stalled workflow stays clear, and says the reconciler looked', () => {
    const check = workflowsCheck(report([]))

    expect(check.status).toBe('clear')
    if (check.status !== 'clear') return
    expect(check.note).toContain('the reconciler found none stalled')
  })

  it('repaired rows are said in the note, not raised', () => {
    const check = workflowsCheck(report([
      row('wf_fixed', 'dependencies_met', 'note', { repaired: true, repair: 'promoted to READY' }),
    ]))

    expect(check.status).toBe('clear')
    if (check.status !== 'clear') return
    expect(check.note).toContain('the reconciler repaired 1')
  })

  it('an API without the field is said, not read as none', () => {
    const check = workflowsCheck(undefined)

    expect(check.status).toBe('clear')
    if (check.status !== 'clear') return
    expect(check.note).toContain('no reconciler stall check')
  })
})
