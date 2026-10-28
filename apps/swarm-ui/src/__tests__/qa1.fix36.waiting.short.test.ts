import { describe, expect, it } from 'vitest'
import { waitingShort } from '../waitingShort'

/**
 * QA1-FIX-36 (overview-agents-57; part of #2596): a step waiting on its
 * upstream steps said "upstream: 1 step" in Work › Agents › Waiting, which
 * names no step. The summary the API writes names them
 * ("Waiting on 1 upstream steps: scan-terraform (RUNNING)"), so the row says
 * "waiting on scan-terraform".
 */
describe('waitingShort names the upstream steps a step waits on', () => {
  it('names the one step the fixture console waits on', () => {
    // MUTATION: return `upstream: ${n} step(s)` again and this reads "upstream: 1 step".
    // The row `task_wf_report` (step "report") carries in src/api.ts's fixtures, verbatim.
    expect(
      waitingShort({ reason: 'DEPENDENCY_INCOMPLETE', summary: 'Waiting on 1 upstream steps: scan-terraform (RUNNING)' }),
    ).toBe('waiting on scan-terraform')
  })

  it('names two steps, and counts the rest past two', () => {
    // MUTATION: drop the "+N more" cut and the third name overflows the row.
    expect(
      waitingShort({ reason: 'DEPENDENCY_INCOMPLETE', summary: 'Waiting on 2 upstream steps: plan (RUNNING), scan (QUEUED)' }),
    ).toBe('waiting on plan, scan')
    expect(
      waitingShort({
        reason: 'DEPENDENCY_INCOMPLETE',
        summary: 'Waiting on 4 upstream steps: plan (RUNNING), scan (QUEUED), lint (state not read), build (QUEUED)',
      }),
    ).toBe('waiting on plan, scan +2 more')
  })

  it('keeps the count when the summary names no step', () => {
    // MUTATION: name from an empty list and this reads "waiting on ".
    expect(waitingShort({ reason: 'DEPENDENCY_INCOMPLETE', summary: 'Waiting on 1 upstream steps' })).toBe('upstream: 1 step')
    expect(waitingShort({ reason: 'DEPENDENCY_INCOMPLETE', summary: 'Waiting on 3 upstream steps' })).toBe('upstream: 3 steps')
  })
})
