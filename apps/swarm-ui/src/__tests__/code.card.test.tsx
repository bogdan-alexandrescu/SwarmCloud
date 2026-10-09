// THE CODE CARD (agent-detail-2.html, pick A, A4).
//
// It opens with ONE sentence on what happened and what to do, then the
// base-pin line (`result_summary.git.base_pin`: pinned to a sha and where it
// came from, or not pinned and why -- and, while no worker writes it, "not
// recorded", never "not pinned"), then the per-file diff. FAILED
// `published_nothing` and PUBLISH_REFUSED get their own forms; a refused
// file is named and its content is never shown, so no diff is offered. The
// six causes of no pull request stay six, and an unrecognised reason is
// printed as the worker wrote it. The diff itself is the Changes tab
// (diff-viewer.md §2 variant 2): the card links to it, `Changes ›`, and reads
// no patch of its own.
//
// MUTATIONS, one per block: drop `AgCodeLead`; read an absent `base_pin` as
// not pinned; offer the diff on a credential refusal; read the refusal from
// the latest attempt only; fold the unrecognised reason into a known bucket.

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { AttemptRow, GitSummary, Task } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run, publishRefusals } = await import('../AgentDetail')

const PATCH = { name: 'change.diff', bytes: 9400, uri: 'gs://swarm-artifacts/tenants/eng/tasks/t/attempts/att_1/artifacts/change.diff' }

function run(t: Task, attempts: AttemptRow[] = [attempt(1)]): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function finished(git: GitSummary, over: Partial<Task> = {}): Task {
  return runTask({
    state: 'SUCCEEDED',
    repository_url: 'https://github.com/example/swarm',
    result_summary: { git, artifacts: [PATCH] },
    ...over,
  })
}

function code(): HTMLElement {
  const panel = document.querySelector<HTMLElement>('.run-output')
  expect(panel, 'no Code panel').not.toBeNull()
  return panel!
}

/** The first thing the card says, before every fact. */
function lead(): HTMLElement {
  const first = code().querySelector<HTMLElement>('.git-outcome > :first-child')
  expect(first, 'the Code card opens on nothing').not.toBeNull()
  return first!
}

beforeEach(() => {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
})

describe('the card opens with one sentence, then the base pin, then the diff', () => {
  it('says the pull request opened and what to do, then where the base was pinned', () => {
    render(
      <Run
        run={run(
          finished({
            base: '9c4e0b2abcdef0',
            branch: 'swarm/task_x',
            published: true,
            publish_reason: 'opened',
            pull_request: { number: 512, url: 'https://github.com/example/swarm/pull/512', state: 'open', created: true },
            patch: 'change.diff',
            base_pin: { pinned: true, sha: '9c4e0b2abcdef0', from: 'plan' },
          }),
        )}
      />,
    )
    // A FINISHED agent's code card LEADS the tab, headed Outcome
    // (agent-details-v3.html A3): one heading over the result, still.
    expect(code().querySelector('.dt-card-head > b')?.textContent).toBe('Outcome')
    expect(code().dataset.lead).toBe('outcome')
    const first = lead()
    expect(first.textContent).toBe('Pull request #512 opened from swarm/task_x: review it on the forge.')
    const pin = code().querySelector('.ag-basepin')!
    expect(first.nextElementSibling).toBe(pin)
    expect(pin.textContent).toBe('Base pinned to 9c4e0b2abc, from plan')
    // Then the way to the diff: the Changes tab, which replaced `Show the diff`.
    expect(pin.nextElementSibling?.querySelector('a.dt-more')?.textContent).toBe('Changes ›')
  })

  it('links to the Changes tab, switches the split to it, and reads no patch here', () => {
    const changes = vi.fn()
    const links = { logs: vi.fn(), attempts: vi.fn(), artifacts: vi.fn(), changes }
    const t = finished({ base: 'abc', published: false, publish_reason: 'collect', strategy: 'collect', patch: 'change.diff' })
    render(<Run run={run(t)} links={links} />)
    const link = screen.getByRole('link', { name: 'Changes ›' })
    expect(link.getAttribute('href')).toBe(`#work/task/${encodeURIComponent(t.id)}/changes`)
    expect(link.parentElement?.textContent).toMatch(/change\.diff · 9[,.]?400 bytes/)
    fireEvent.click(link)
    expect(changes).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Show the diff' }), 'the in-card viewer is back').toBeNull()
    expect(api.loadArtifactContent).not.toHaveBeenCalled()
  })

  it('says the file count beside the link where the worker kept git.files', () => {
    const files = [
      { path: 'a.ts', old_path: null, status: 'M' as const, insertions: 1, deletions: 0, binary: false },
      { path: 'b.ts', old_path: null, status: 'A' as const, insertions: 2, deletions: 0, binary: false },
    ]
    render(<Run run={run(finished({ base: 'abc', published: false, publish_reason: 'collect', strategy: 'collect', patch: 'change.diff', files }))} />)
    expect(screen.getByRole('link', { name: 'Changes ›' }).parentElement?.textContent).toMatch(/2 files · change\.diff/)
  })

  it('says a base that was not pinned, with its reason', () => {
    render(<Run run={run(finished({ base: '9c4e0b2abcdef0', published: false, publish_reason: 'collect', strategy: 'collect', base_pin: { pinned: false, reason: 'the upstream step recorded no base' } }))} />)
    expect(code().querySelector('.ag-basepin')?.textContent).toBe('Base not pinned · the upstream step recorded no base · cloned at 9c4e0b2abc')
  })

  it('says a worker that records no pin did not record one -- never that it was not pinned', () => {
    render(<Run run={run(finished({ base: '9c4e0b2abcdef0', published: false, publish_reason: 'collect', strategy: 'collect' }))} />)
    const pin = code().querySelector('.ag-basepin')!
    expect(pin.textContent).toMatch(/pin not recorded · cloned at 9c4e0b2abc/)
    expect(pin.textContent).not.toMatch(/not pinned/)
    expect(pin.querySelector('.ctl-mark.is-absent')).not.toBeNull()
  })
})

describe('FAILED published_nothing', () => {
  it('is a failure with its own sentence, for a step meant to open a pull request', () => {
    render(
      <Run
        run={run(
          finished(
            { base: '9c4e0b2', published: false, publish_reason: 'published_nothing: no commits beyond the base' },
            { state: 'FAILED', end_cause: 'published_nothing' },
          ),
        )}
      />,
    )
    const first = lead()
    expect(first.textContent).toMatch(/Failed: nothing to publish/)
    expect(first.textContent).toMatch(/no commits beyond its base/)
    expect(first.querySelector('.ctl-empty.is-failed')).not.toBeNull()
  })

  it('is not read off a succeeded step that changed nothing', () => {
    render(<Run run={run(finished({ base: '9c4e0b2', published: false, publish_reason: 'the agent changed nothing in the repository' }))} />)
    expect(lead().textContent).toMatch(/the agent changed nothing/)
    expect(lead().textContent).not.toMatch(/Failed: nothing to publish/)
  })
})

describe('PUBLISH_REFUSED', () => {
  const credential = (file: string) => `the final tree adds a credential in ${file} (rule R1, line 3); remove it`
  const title = 'pr-title.txt refused: the title carries a task id; write a fact-style title'

  it('names each refused attempt and the file, withholds its content, and offers no diff -- while the task retries', () => {
    const attempts = [
      attempt(1, { attempt_id: 'att_1', error: credential('config/test.env'), exit_code: 1 }),
      attempt(2, { attempt_id: 'att_2', error: title, exit_code: 0 }),
    ]
    const t = runTask({
      state: 'READY',
      attempt_count: 2,
      max_attempts: 3,
      completed_at: null,
      last_error: title,
      result_summary: { git: { base: '9c4e0b2', patch: 'change.diff' }, artifacts: [PATCH] },
    })
    render(<Run run={run(t, attempts)} />)
    expect(code().querySelector('.dt-card-head > b')?.textContent).toBe('Code')
    const first = lead()
    expect(first.textContent).toMatch(/Publish refused 2 times\. Nothing was pushed\./)
    const items = [...first.querySelectorAll('.ag-refusal')].map((li) => li.textContent ?? '')
    expect(items).toHaveLength(2)
    expect(items[0]).toMatch(/Attempt 1 · gen 1 · a credential in the final tree/)
    expect(items[0]).toMatch(/config\/test\.env/)
    expect(items[0]).toMatch(/content is never shown/)
    expect(items[1]).toMatch(/Attempt 2 · gen 2 · an unusable pr-title\.txt/)
    expect(screen.queryByRole('link', { name: 'Changes ›' }), 'the refused file could be read through the diff').toBeNull()
  })

  it('withholds the diff on a FAILED task whose last attempt was refused for a credential', () => {
    const t = runTask({
      state: 'FAILED',
      attempt_count: 3,
      max_attempts: 3,
      last_error: credential('config/test.env'),
      result_summary: { git: { base: '9c4e0b2', patch: 'change.diff', published: false, publish_reason: 'refused' }, artifacts: [PATCH] },
    })
    render(<Run run={run(t, [attempt(1, { error: credential('config/test.env') })])} />)
    expect(lead().textContent).toMatch(/Publish refused\. Nothing was pushed\./)
    expect(code().querySelector('.ag-basepin'), 'the base pin follows the lead here too').not.toBeNull()
    expect(screen.queryByRole('link', { name: 'Changes ›' }), 'the refused file could be read through the diff').toBeNull()
  })

  it('reads every attempt, then the task’s own last error, then the end cause', () => {
    expect(publishRefusals(runTask({ last_error: credential('a.env') }), null)).toEqual([
      { kind: 'credential', file: 'a.env', attempt: 1, generation: 1 },
    ])
    expect(publishRefusals(runTask({ end_cause: 'publish_refused' }), [])).toHaveLength(1)
    expect(publishRefusals(runTask({ last_error: 'exit 1' }), [attempt(1)])).toEqual([])
  })
})

describe('the six ways to get no pull request stay six, and an unknown reason is printed as written', () => {
  const cases: { git: GitSummary; heading: RegExp }[] = [
    { git: { base: 'a', published: false, can_push: false, publish_reason: 'the token cannot push' }, heading: /token cannot write/ },
    { git: { base: 'a', published: false, publish_reason: 'the attempt parked before publishing' }, heading: /this attempt parked/ },
    { git: { base: 'a', published: false, publish_reason: 'the agent changed nothing in the repository' }, heading: /the agent changed nothing/ },
    { git: { base: 'a', published: false, publish_reason: 'the repository is not on a forge this worker knows' }, heading: /not a forge we can publish to/ },
    { git: { base: 'a', published: false, branch: 'swarm/x', publish_reason: '! [rejected] HEAD -> swarm/x (fetch first)' }, heading: /git's own message/ },
    { git: { base: 'a', published: true, branch: 'swarm/x', publish_reason: 'the pull request call failed: 502' }, heading: /pushed · no pull request · swarm\/x/ },
  ]

  it('gives each cause its own heading, first in the card', () => {
    const seen = new Set<string>()
    for (const c of cases) {
      const { unmount } = render(<Run run={run(finished(c.git))} />)
      const h = lead().querySelector('h3')?.textContent ?? ''
      expect(h).toMatch(c.heading)
      seen.add(h)
      unmount()
    }
    expect(seen.size).toBe(6)
  })

  it('prints a reason it does not know exactly as the worker wrote it', () => {
    const reason = 'publishing paused by the operator for the freeze'
    render(<Run run={run(finished({ base: 'a', published: false, publish_reason: reason }))} />)
    expect(lead().querySelector('.ctl-empty-foot')?.textContent).toBe(reason)
  })
})

describe('the split holds Stop in its header, so Details draws none', () => {
  it('draws the pane’s own stop control only when no header holds one', () => {
    const t = runTask({ state: 'RUNNING', completed_at: null })
    const { unmount } = render(<Run run={run(t)} reload={() => {}} />)
    expect(document.querySelector('.run-stop .run-stop-btn')).not.toBeNull()
    unmount()
    render(<Run run={run(t)} reload={() => {}} headed />)
    expect(document.querySelector('.run-stop .run-stop-btn')).toBeNull()
  })
})

