/**
 * VISUAL QA PASS 2, LANE QA2-FIX-23 (part of #2694): Submit's chooser and
 * Submit a workflow, on the fixture console.
 *
 *   runs2-10  "Start from a recent one" listed five agents over a footnote
 *             that said they were found among "the tenant's newest 3
 *             workflows, its newest 2 issue runs": each window now says
 *             whether its kind is listed, older than the five, or none of
 *             yours.
 *   runs2-31  A priority of 500 disabled "Submit this workflow" with no reason
 *             under it; the button now has a "Not ready: …" line, tied by
 *             aria-describedby, as the task form does.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { recentScope, recentSubmissions, SubmitChooser } from '../SubmitChooser'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'
import { task } from './runfixture'

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const ME = 'alice@example.com'
const lone = { submitted_by: ME, workflow_id: null, step_id: null, parent_task_id: null }

describe('runs2-10: the footnote says what became of each window it read', () => {
  it('says the workflows and issue runs held none of yours under a list of agents', () => {
    const items = recentSubmissions(ME, [
      task({ ...lone, id: 'task_a', created_at: '2026-10-04T12:00:00Z' }),
      task({ ...lone, id: 'task_b', created_at: '2026-10-04T11:00:00Z' }),
    ], [], [])
    const looked = [
      { kind: 'task' as const, read: 30, yours: 2 },
      { kind: 'workflow' as const, read: 3, yours: 0 },
      { kind: 'issue run' as const, read: 2, yours: 0 },
    ]
    // MUTATION: name every window after "Found among" again and this reads
    // "Found among your newest 30 agents, the tenant’s newest 3 workflows, its
    // newest 2 issue runs." under two agents.
    expect(recentScope(items, looked)).toBe(
      'Listed: 2 agents, newest first. Found among your newest 30 agents. None of yours among the tenant’s newest 3 workflows or its newest 2 issue runs.',
    )
    // A window whose yours are older than the five says so, and a run
    // window after it is the tenant's, not a dangling "its".
    // MUTATION: call every unlisted window "none of yours" and the first
    // reads "None of yours among the tenant’s newest 3 workflows or …".
    expect(recentScope(items, [looked[0]!, { ...looked[1]!, yours: 4 }, looked[2]!])).toBe(
      'Listed: 2 agents, newest first. Found among your newest 30 agents. Yours among the tenant’s newest 3 workflows are older than these. None of yours among the tenant’s newest 2 issue runs.',
    )
  })

  it('draws it so on the fixture console', async () => {
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(recent.querySelectorAll('.sb-recent-i').length).toBe(5), { timeout: 4000 })
    const chips = [...recent.querySelectorAll('.sb-recent-i .c-chip')].map(visible)
    expect(chips).toEqual(['agent', 'agent', 'agent', 'agent', 'agent'])
    // The fixture's workflows of yours are older than its five agents, and
    // none of its issue runs is yours. MUTATION: name every window after
    // "Found among" again and this reads "Found among your newest … agents,
    // the tenant’s newest … workflows, its newest … issue runs.".
    expect(visible(recent.querySelector('.sb-recent-scope'))).toMatch(
      /^Listed: 5 agents, newest first\. Found among your newest \d+ agents\. Yours among the tenant’s newest \d+ workflows? are older than these\. None of yours among the tenant’s newest \d+ issue runs?\.$/,
    )
  }, 20000)
})

async function readyForm() {
  const view = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, { timeout: 4000 })
  const select = view.container.querySelector<HTMLSelectElement>('.wfb-step select.sb-runner-select')!
  fireEvent.change(select, { target: { value: 'mock' } })
  return view
}

describe('runs2-31: a priority out of range says so under the button it disables', () => {
  it('reads "Not ready: priority must be −100 to 100." and ties it to the button', async () => {
    const { container } = await readyForm()
    const button = () => screen.getByRole('button', { name: 'Submit this workflow' })
    await waitFor(() => expect(visible(container.querySelector('.sbf-send h2'))).toBe('Ready to send'))
    // The control: a ready plan has no reason line and no description.
    expect(container.querySelector('#wf-send-why')).toBeNull()
    expect(button().getAttribute('aria-describedby')).toBeNull()
    fireEvent.change(container.querySelector<HTMLInputElement>('#wf-priority')!, { target: { value: '500' } })
    expect(button()).toHaveProperty('disabled', true)
    // MUTATION: drop the priority clause from `why` in SubmitWorkflow and
    // the button is disabled with nothing under it.
    const why = container.querySelector('#wf-send-why')
    expect(visible(why)).toBe('Not ready: priority must be −100 to 100.')
    expect(button().getAttribute('aria-describedby')).toBe('wf-send-why')
    // A priority that is not a whole number says that cause.
    fireEvent.change(container.querySelector<HTMLInputElement>('#wf-priority')!, { target: { value: '1.5' } })
    expect(visible(container.querySelector('#wf-send-why'))).toBe('Not ready: priority must be a whole number from −100 to 100.')
    // And back in range, the line is gone.
    fireEvent.change(container.querySelector<HTMLInputElement>('#wf-priority')!, { target: { value: '10' } })
    expect(container.querySelector('#wf-send-why')).toBeNull()
  }, 20000)
})
