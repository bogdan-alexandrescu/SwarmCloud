/**
 * A WORKFLOW WAITING FOR A PULL REQUEST SAYS SO (lane SPEED-D3).
 *
 * A workflow submitted with `after: {pr: N}` keeps every step PARKED until
 * pull request N of its repository merges. Its steps read PARKED on
 * DEPENDENCY_INCOMPLETE, which on its own says "a step it depends on has not
 * finished" -- and no step of it has run. The API serves
 * `workflow.after_pull_requests` with the sentence; the list row and the
 * workflow page's head print it as a tag.
 *
 * MUTATIONS: return the text for a `merged` gate; drop the tag from the row.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import type { Workflow } from '../types'

const { pullRequestWait, PullRequestWaitTag } = await import('../Workflows')

function workflow(after: Workflow['after_pull_requests']): Workflow {
  return {
    workflow_id: 'wf_gated',
    state: 'PARKED',
    tenant_id: 'eng',
    stored_state: 'QUEUED',
    state_source: 'derived',
    created_at: '2026-10-14T12:00:00Z',
    updated_at: '2026-10-14T12:00:00Z',
    submitted_by: 'alice@saga.xyz',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps: [],
    ...(after === undefined ? {} : { after_pull_requests: after }),
  }
}

describe('SPEED-D3: waiting for a pull request to merge', () => {
  it('reads the sentence while the gate waits, and when it closed unmerged', () => {
    const waiting = workflow({
      state: 'waiting', pull_requests: [41], pending: [41], merged: [], closed: null,
      repository: 'octo-org/widget-shop', text: 'waiting for PR #41 to merge',
    })
    expect(pullRequestWait(waiting)).toBe('waiting for PR #41 to merge')
    const closed = workflow({
      state: 'closed', pull_requests: [41], pending: [41], merged: [], closed: 41,
      repository: 'octo-org/widget-shop', text: 'PR #41 was closed without being merged',
    })
    expect(pullRequestWait(closed)).toBe('PR #41 was closed without being merged')
  })

  it('says nothing once the gate released, or for a workflow with none', () => {
    const merged = workflow({
      state: 'merged', pull_requests: [41], pending: [], merged: [41], closed: null,
      repository: 'octo-org/widget-shop', text: 'PR #41 merged',
    })
    expect(pullRequestWait(merged)).toBeNull()
    expect(pullRequestWait(workflow(undefined))).toBeNull()
  })

  it('draws the tag with the pull request named', () => {
    render(
      <PullRequestWaitTag
        workflow={workflow({
          state: 'waiting', pull_requests: [41, 42], pending: [42], merged: [41], closed: null,
          repository: 'octo-org/widget-shop', text: 'waiting for PR #42 to merge',
        })}
      />,
    )
    expect(screen.getByText('waiting for PR #42 to merge')).toBeTruthy()
  })

  it('draws nothing without a wait', () => {
    const { container } = render(<PullRequestWaitTag workflow={workflow(undefined)} />)
    expect(container.textContent).toBe('')
  })
})
