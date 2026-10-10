// THE WORKFLOW'S AND THE RUN'S CHANGES TAB: VARIANT 5'S FILES x STEPS MATRIX
// (docs/design/diff-viewer.md §2 variant 5, owner decision 2026-10-08; lane
// DIFF2b).
//
// Files down the side, steps across the top, each cell that step's +/- on
// that file; the open file's hunks stacked and tagged by step in one viewer;
// a step filter; the step filter and the open file in the address. And the
// states, none of which reads as zero:
//
//   * `·` only where the step's file list is known AND complete;
//   * a step that changed nothing says so, with the hollow ring;
//   * not started, nothing written, still reading, not read, discarded over
//     the cap, past the list's cap: hatched, with the reason;
//   * a credential refusal in ANY attempt withholds the step: its patch is
//     never read, its column lists no file.
//
// MUTATIONS, one per block: treat a truncated `git.files` as complete (the
// `·` test goes red); read a step's patch before its attempts are checked, or
// check only the latest attempt (withheld); read every listed step's patch on
// open instead of on demand (the on-demand test); drop the cap in
// `useReadQueue` (the cap test); compose hunks in read order rather than step
// order (the stacking test); drop `RUN_PANE_QUERY` from paths.ts (routes).

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { ArtifactContent, GitSummary, Task, Workflow, WorkflowStep } from '../types'
import { attempt, issueRun, task as baseTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowUsage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const m = await import('../WorkflowChanges')
const { RunChangesTab, WorkflowChangesTab, cellOf, changesAtOf, columnFacts, composeFile, patchSections, withChangesAt, MAX_READS_IN_FLIGHT } = m
const { StepChanges } = await import('../RunSteps')
const { canonical, fromAddress } = await import('../App')
const { addressToPath, pathToAddress } = await import('../paths')

// ---------------------------------------------------------------------------
// Fixtures: three steps; `implement` and `fix` both change src/app/one.ts.
// ---------------------------------------------------------------------------

const PATCH = 'swarm-work.patch'

/** One file's section: `hunks` are [oldStart, removed, added]. */
function section(path: string, hunks: [number, string, string][], header: string[] = []): string {
  const out = [`diff --git a/${path} b/${path}`, ...header, `--- a/${path}`, `+++ b/${path}`]
  for (const [at, del, add] of hunks) out.push(`@@ -${at},1 +${at},1 @@`, `-${del}`, `+${add}`)
  return out.join('\n') + '\n'
}

const IMPLEMENT = section('src/app/one.ts', [[1, 'a', 'b'], [10, 'c', 'd']]) + section('src/app/two.ts', [[3, 'x', 'y']])
const FIX = section('src/app/one.ts', [[5, 'e', 'f']])

type FileRow = NonNullable<GitSummary['files']>[number]
const fileRow = (path: string, insertions: number, deletions: number): FileRow => ({ path, old_path: null, status: 'M', insertions, deletions, binary: false })

function stepTask(id: string, git: GitSummary | null, over: Partial<Task> = {}): Task {
  return baseTask({
    id,
    state: 'SUCCEEDED',
    workflow_id: 'wf_one',
    result_summary: git === null ? null : { git, artifacts: [{ name: PATCH, bytes: 100, uri: 'gs://b/p' }] },
    ...over,
  })
}

const IMPL_GIT: GitSummary = { base: 'abc', patch: PATCH, files: [fileRow('src/app/one.ts', 2, 2), fileRow('src/app/two.ts', 1, 1)] }
const FIX_GIT: GitSummary = { base: 'def', patch: PATCH, files: [fileRow('src/app/one.ts', 1, 1)] }
const ZERO_GIT: GitSummary = { base: 'abc', commits: [], dirty: [], files: [] }

function step(step_id: string, task_id: string | null): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id }
}

function wf(steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: 'wf_one',
    state: 'SUCCEEDED',
    tenant_id: 'eng',
    stored_state: 'SUCCEEDED',
    created_at: '2026-10-08T10:00:00Z',
    updated_at: '2026-10-08T11:00:00Z',
    submitted_by: 'operator@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  } as Workflow
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function served(taskId: string, content: string, over: Partial<ArtifactContent> = {}): ArtifactContent {
  return {
    task_id: taskId,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    artifact: { name: PATCH, bytes: content.length, uri: null },
    status: 'ok',
    detail: null,
    key: 'k',
    uri: null,
    content,
    total_bytes: content.length,
    offset: 0,
    returned_bytes: content.length,
    next_offset: null,
    truncated: false,
    redacted: true,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
    ...over,
  }
}

const PATCHES: Record<string, string> = { t_impl: IMPLEMENT, t_fix: FIX }

/** The tab with an address of its own, as the workflow page keeps one. */
function Tab({ steps, tasks, start = { step: null, file: null }, onAt }: {
  steps: WorkflowStep[]
  tasks: Task[]
  start?: { step: string | null; file: string | null }
  onAt?: (at: { step: string | null; file: string | null }) => void
}) {
  const [at, setAt] = useState(start)
  return (
    <WorkflowChangesTab
      workflow={wf(steps)}
      taskById={new Map(tasks.map((t) => [t.id, t]))}
      at={at}
      onAt={(next) => {
        onAt?.(next)
        setAt(next)
      }}
    />
  )
}

const THREE = [step('implement', 't_impl'), step('review', 't_rev'), step('fix', 't_fix')]
const THREE_TASKS = [stepTask('t_impl', IMPL_GIT), stepTask('t_rev', ZERO_GIT), stepTask('t_fix', FIX_GIT)]

function table(): HTMLElement {
  return screen.getByRole('table', { name: 'Files by step' })
}

/** The cell of one file and one step. */
function cell(path: string, stepKey: string): HTMLElement {
  const heads = [...table().querySelectorAll('thead th')].map((th) => th.getAttribute('data-step'))
  const row = table().querySelector<HTMLElement>(`tbody tr[data-file="${path}"]`)
  expect(row, `no row for ${path}`).not.toBeNull()
  return row!.children[heads.indexOf(stepKey)] as HTMLElement
}

function head(stepKey: string): HTMLElement {
  return table().querySelector<HTMLElement>(`thead th[data-step="${stepKey}"]`)!
}

function sources(): string[] {
  const lines = screen.getByRole('region', { name: 'Diff lines' })
  return [...lines.querySelectorAll('[data-diff-row="source"]')].map((r) => r.getAttribute('data-step') ?? '')
}

function readsOf(taskId: string): number {
  return api.loadArtifactContent.mock.calls.filter((c) => c[0] === taskId).length
}

beforeEach(() => {
  vi.stubGlobal('innerWidth', 1280)
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
  api.loadArtifactContent.mockImplementation(async (id: string) => ok(served(id, PATCHES[id] ?? '')))
  api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------

describe('the pure half: sections, stacking, facts and cells', () => {
  it('cuts a patch into its files, keyed by path, as served', () => {
    const { sections, unparsed } = patchSections(IMPLEMENT)
    expect([...sections.keys()]).toEqual(['src/app/one.ts', 'src/app/two.ts'])
    expect(sections.get('src/app/one.ts')!.file.hunks).toHaveLength(2)
    expect(IMPLEMENT.startsWith(sections.get('src/app/one.ts')!.text)).toBe(true)
    expect(unparsed).toBe(0)
    // A section the parser refuses is counted, never guessed.
    expect(patchSections('diff --git a/x b/x\n@@ nonsense\n').unparsed).toBe(1)
  })

  it('stacks one file’s hunks in STEP order under one header, and names each hunk’s step', () => {
    const one = (t: string) => patchSections(t).sections.get('src/app/one.ts')!
    const { patch, stepOfHunk } = composeFile([
      { step: 'implement', section: one(IMPLEMENT) },
      { step: 'fix', section: one(FIX) },
    ])
    expect(stepOfHunk).toEqual(['implement', 'implement', 'fix'])
    expect(patch.match(/^diff --git /gm)).toHaveLength(1)
    expect(patch.match(/^@@ /gm)).toHaveLength(3)
    expect(patch.indexOf('-e')).toBeGreaterThan(patch.indexOf('-c'))
  })

  it('tells every state apart, and checks refusals before it lists a file', () => {
    const col = (task: Task | null, taskId: string | null = task?.id ?? null) => ({ key: 's', label: 's', taskId, task })
    expect(columnFacts(col(null, null), undefined, undefined).kind).toBe('not-started')
    expect(columnFacts(col(null, 't_gone'), undefined, undefined).kind).toBe('not-in-read')
    expect(columnFacts(col(stepTask('t', null, { state: 'RUNNING' })), undefined, undefined)).toEqual({ kind: 'unsummarised', done: false })
    expect(columnFacts(col(stepTask('t', null)), undefined, undefined)).toEqual({ kind: 'unsummarised', done: true })
    expect(columnFacts(col(stepTask('t', ZERO_GIT)), undefined, undefined).kind).toBe('zero')
    // A patch: nothing listed until the attempts say it is not withheld.
    expect(columnFacts(col(stepTask('t', IMPL_GIT)), undefined, undefined).kind).toBe('checking')
    const refused = [attempt(1, { error: 'the final tree adds a credential in config/test.env (rule R1, line 3)' }), attempt(2)]
    expect(columnFacts(col(stepTask('t', IMPL_GIT, { attempt_count: 2 })), refused, undefined)).toEqual({ kind: 'withheld', files: ['config/test.env'] })
    const listed = columnFacts(col(stepTask('t', IMPL_GIT)), [], undefined)
    expect(listed.kind === 'patch' && listed.listed && listed.files?.size).toBe(2)
    expect(columnFacts(col(stepTask('t', { base: 'abc', patch_omitted: true, patch_bytes: 9 })), [], undefined).kind).toBe('omitted')
  })

  it('draws `·` only where the list is complete; a capped list leaves the rest unknown', () => {
    const facts = (git: GitSummary) => columnFacts({ key: 's', label: 's', taskId: 't', task: stepTask('t', git) }, [], undefined)
    expect(cellOf(facts(IMPL_GIT), 'src/app/one.ts')).toEqual({ kind: 'count', count: { add: 2, del: 2, binary: false } })
    expect(cellOf(facts(IMPL_GIT), 'docs/x.md').kind).toBe('none')
    expect(cellOf(facts({ ...IMPL_GIT, files_truncated: true }), 'docs/x.md').kind).toBe('unknown')
    expect(cellOf(columnFacts({ key: 's', label: 's', taskId: 't', task: stepTask('t', ZERO_GIT) }, undefined, undefined), 'a').kind).toBe('none')
  })

  it('keeps the step filter and the open file in a query, and drops them when null', () => {
    expect(changesAtOf('wf=x&tab=changes&step=fix&file=src%2Fa.ts')).toEqual({ step: 'fix', file: 'src/a.ts' })
    expect(changesAtOf('wf=x&step=')).toEqual({ step: null, file: null })
    expect(withChangesAt('wf=x&tab=changes&file=old', { step: 'fix', file: null })).toBe('wf=x&tab=changes&step=fix')
  })
})

describe('the matrix: which step changed which file', () => {
  it('draws a row per file and a column per step, counted from git.files', async () => {
    render(<Tab steps={THREE} tasks={THREE_TASKS} />)
    await waitFor(() => expect(cell('src/app/one.ts', 'implement').textContent).toBe('+2 −2'))
    expect([...table().querySelectorAll('tbody tr')].map((r) => r.getAttribute('data-file'))).toEqual(['src/app/one.ts', 'src/app/two.ts'])
    expect(cell('src/app/one.ts', 'fix').textContent).toBe('+1 −1')
    expect(cell('src/app/two.ts', 'fix').textContent, 'a complete list’s absent file is a measured ·').toBe('·')
    expect(head('implement').textContent).toMatch(/2 files/)
  })

  it('says a step that changed nothing changed nothing, with the hollow ring', async () => {
    render(<Tab steps={THREE} tasks={THREE_TASKS} />)
    await waitFor(() => expect(head('review').textContent).toMatch(/changed nothing/))
    expect(head('review').querySelector('.ctl-mark.is-zero')).not.toBeNull()
    expect(cell('src/app/one.ts', 'review').textContent).toBe('·')
    expect(api.loadAttempts, 'a measured zero needs no read').not.toHaveBeenCalledWith('t_rev')
  })

  it('reads hunks on demand: only the steps that changed the open file', async () => {
    render(<Tab steps={THREE} tasks={THREE_TASKS} start={{ step: null, file: 'src/app/two.ts' }} />)
    await waitFor(() => expect(sources()).toEqual(['implement']))
    expect(readsOf('t_impl')).toBe(1)
    expect(readsOf('t_fix'), 'a step that did not change the open file was read').toBe(0)
    expect(readsOf('t_rev')).toBe(0)
  })

  it('stacks each step’s hunks for the open file, tagged by step, and writes the choice to the address', async () => {
    const onAt = vi.fn()
    render(<Tab steps={THREE} tasks={THREE_TASKS} onAt={onAt} start={{ step: null, file: 'src/app/two.ts' }} />)
    await waitFor(() => expect(sources()).toEqual(['implement']))
    fireEvent.click(within(table()).getByRole('button', { name: 'src/app/one.ts' }))
    expect(onAt).toHaveBeenLastCalledWith({ step: null, file: 'src/app/one.ts' })
    await waitFor(() => expect(sources()).toEqual(['implement', 'fix']))
    const meta = screen.getByRole('note', { name: 'Where these hunks come from' })
    expect(meta.textContent).toMatch(/which step wrote each line: not recorded/)
    expect(screen.getByRole('region', { name: 'src/app/one.ts, by step' }).textContent).toMatch(/changed by implement, fix/)
  })

  it('filters rows and hunks to one step, from a chip or from the address', async () => {
    const onAt = vi.fn()
    render(<Tab steps={THREE} tasks={THREE_TASKS} onAt={onAt} />)
    const chips = await screen.findByRole('group', { name: 'Steps' })
    fireEvent.click(within(chips).getByRole('button', { name: 'fix' }))
    expect(onAt).toHaveBeenLastCalledWith({ step: 'fix', file: 'src/app/one.ts' })
    await waitFor(() => expect([...table().querySelectorAll('tbody tr')].map((r) => r.getAttribute('data-file'))).toEqual(['src/app/one.ts']))
    await waitFor(() => expect(sources()).toEqual(['fix']))
    expect(within(chips).getByRole('button', { name: 'fix' }).getAttribute('aria-pressed')).toBe('true')
    expect(head('implement').classList.contains('is-dim')).toBe(true)
  })

  it('says a filtered step that changed nothing changed no files: a measured zero', async () => {
    render(<Tab steps={THREE} tasks={THREE_TASKS} start={{ step: 'review', file: null }} />)
    await waitFor(() => expect(screen.getByText(/review changed no files/)).toBeTruthy())
    expect(screen.queryByRole('region', { name: 'Diff lines' })).toBeNull()
  })

  it('reads a patch whose files the summary does not list, to find them', async () => {
    const tasks = [stepTask('t_impl', { base: 'abc', patch: PATCH }), stepTask('t_fix', FIX_GIT)]
    render(<Tab steps={[step('implement', 't_impl'), step('fix', 't_fix')]} tasks={tasks} start={{ step: null, file: 'src/app/one.ts' }} />)
    await waitFor(() => expect(cell('src/app/two.ts', 'implement').textContent).toBe('+1 −1'))
    expect(head('implement').textContent).toMatch(/2 files/)
  })

  it('links each step to its own agent’s Changes tab, and marks the integrator', async () => {
    const pr = { number: 553, url: 'https://github.com/example/swarm/pull/553', state: 'open', created: true }
    const integ = stepTask('t_fix', { ...FIX_GIT, role: 'integrator', pull_request: pr } as GitSummary)
    render(<Tab steps={THREE} tasks={[THREE_TASKS[0]!, THREE_TASKS[1]!, integ]} />)
    await waitFor(() => expect(head('fix').textContent).toMatch(/integrate · fix/))
    expect(within(head('implement')).getByRole('link', { name: 'Changes ›' }).getAttribute('href')).toMatch(/\/t_impl\/changes$/)
    // In the bar, and in the viewer's bar once the open file's hunks are drawn.
    expect(within(document.querySelector<HTMLElement>('.chg-mx-bar')!).getByRole('link', { name: 'PR #553 ↗' }).getAttribute('href')).toBe(pr.url)
    await waitFor(() => expect(document.querySelector<HTMLAnchorElement>('a.diff-pr')?.getAttribute('href')).toBe(pr.url))
  })
})

describe('the states: none of them reads as no changes', () => {
  it('withholds a step refused for a credential in ANY attempt: no file, no count, no read', async () => {
    api.loadAttempts.mockImplementation(async (id: string) =>
      ok({ attempts: id === 't_fix' ? [attempt(1, { error: 'the final tree adds a credential in config/test.env (rule R1, line 3)' }), attempt(2)] : [attempt(1)] }),
    )
    const tasks = [THREE_TASKS[0]!, THREE_TASKS[1]!, stepTask('t_fix', FIX_GIT, { attempt_count: 2 })]
    render(<Tab steps={THREE} tasks={tasks} start={{ step: null, file: 'src/app/one.ts' }} />)
    await waitFor(() => expect(head('fix').textContent).toMatch(/withheld/))
    expect(cell('src/app/one.ts', 'fix').textContent).toBe('withheld')
    await waitFor(() => expect(sources()).toEqual(['implement']))
    expect(readsOf('t_fix'), 'the refused patch was read').toBe(0)
  })

  it('reads no patch until the step’s attempts are known', async () => {
    api.loadAttempts.mockReturnValue(new Promise(() => {}))
    render(<Tab steps={THREE} tasks={THREE_TASKS} />)
    await waitFor(() => expect(api.loadAttempts).toHaveBeenCalled())
    expect(head('implement').querySelector('.ctl-mark.is-pending')).not.toBeNull()
    expect(api.loadArtifactContent).not.toHaveBeenCalled()
    expect(screen.getByText(/no file known yet/)).toBeTruthy()
  })

  it('hatches a step not started and one with nothing written yet, never `·`', async () => {
    const steps = [step('implement', 't_impl'), step('docs', null), step('fix', 't_fix')]
    const tasks = [THREE_TASKS[0]!, stepTask('t_fix', null, { state: 'RUNNING' })]
    render(<Tab steps={steps} tasks={tasks} />)
    await waitFor(() => expect(cell('src/app/one.ts', 'implement').textContent).toBe('+2 −2'))
    expect(cell('src/app/one.ts', 'docs').querySelector('.is-unknown')?.getAttribute('aria-label')).toMatch(/has not started/)
    expect(cell('src/app/one.ts', 'fix').querySelector('.is-unknown')?.getAttribute('aria-label')).toMatch(/written nothing yet/)
    expect(head('docs').textContent).toMatch(/not started/)
  })

  it('says a patch discarded over the cap cannot be drawn, while its counts still show', async () => {
    const tasks = [stepTask('t_impl', { base: 'abc', patch_omitted: true, patch_bytes: 18_402_113, files: [fileRow('src/app/one.ts', 5, 1)] })]
    render(<Tab steps={[step('implement', 't_impl')]} tasks={tasks} />)
    await waitFor(() => expect(cell('src/app/one.ts', 'implement').textContent).toBe('+5 −1'))
    expect(head('implement').querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(screen.getByRole('note', { name: 'Where these hunks come from' }).textContent).toMatch(/discarded over the size cap/)
    expect(api.loadArtifactContent).not.toHaveBeenCalled()
  })

  it('says a failed read is not read, and reads again on Retry', async () => {
    api.loadArtifactContent.mockResolvedValueOnce({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'unavailable' } })
    render(<Tab steps={[step('implement', 't_impl')]} tasks={[THREE_TASKS[0]!]} start={{ step: null, file: 'src/app/one.ts' }} />)
    const note = await screen.findByRole('note', { name: 'Where these hunks come from' })
    await waitFor(() => expect(note.textContent).toMatch(/not read: .*503/))
    fireEvent.click(within(note).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(sources()).toEqual(['implement']))
    expect(readsOf('t_impl')).toBe(2)
  })

  it(`never has more than ${MAX_READS_IN_FLIGHT} reads in flight`, async () => {
    api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
    const ids = ['a', 'b', 'c', 'd', 'e', 'f']
    render(<Tab steps={ids.map((s) => step(s, `t_${s}`))} tasks={ids.map((s) => stepTask(`t_${s}`, { base: 'abc', patch: PATCH }))} />)
    await waitFor(() => expect(api.loadArtifactContent).toHaveBeenCalledTimes(MAX_READS_IN_FLIGHT))
    await new Promise((r) => setTimeout(r, 20))
    expect(api.loadArtifactContent).toHaveBeenCalledTimes(MAX_READS_IN_FLIGHT)
  })
})

describe('an issue run’s Changes tab', () => {
  it('says a run with no workflow has no steps yet', () => {
    render(<RunChangesTab run={issueRun() as never} />)
    expect(screen.getByText(/no workflow yet/)).toBeTruthy()
    expect(api.loadWorkflow).not.toHaveBeenCalled()
  })

  it('draws its workflow’s steps, then each CI fix round’s, keyed apart', async () => {
    api.loadWorkflow.mockImplementation(async (id: string) =>
      id === 'wf_one'
        ? ok({ workflow: wf([step('implement', 't_impl')]), tasks: [THREE_TASKS[0]!] })
        : ok({ workflow: { ...wf([step('fix', 't_fix')]), workflow_id: id }, tasks: [stepTask('t_fix', FIX_GIT)] }),
    )
    const run = issueRun({ state: 'RUNNING', workflow_id: 'wf_one', ci_fix_workflows: ['wf_ci1'] })
    render(<RunChangesTab run={run as never} at={{ step: null, file: null }} onAt={() => {}} />)
    await waitFor(() => expect(cell('src/app/one.ts', '1:fix').textContent).toBe('+1 −1'))
    expect(head('1:fix').textContent).toMatch(/fix 1 · fix/)
    expect(cell('src/app/one.ts', 'implement').textContent).toBe('+2 −2')
  })

  it('links a step row to the tab filtered to that step', () => {
    const go = vi.fn()
    const row = { key: '0:ui', round: 0, stepId: 'ui', title: null, taskId: 't_ui', task: null, dependsOn: [], merge: null }
    render(<StepChanges runId="run_4c1e09d2" row={row} go={go} />)
    const a = screen.getByRole('link', { name: 'Changes ›' })
    expect(a.getAttribute('href')).toBe('/runs/run_4c1e09d2/changes?step=ui')
    fireEvent.click(a)
    expect(go).toHaveBeenCalledWith('work/runs?run=run_4c1e09d2&tab=changes&step=ui')
  })
})

describe('the addresses carry the step filter and the open file', () => {
  const roundTrip = (path: string, search = '') => {
    const p = pathToAddress(path, search)
    expect(p).not.toBeNull()
    return addressToPath(canonical(fromAddress(p!.address)))
  }

  it('on a run: `/runs/<id>/changes?step=&file=`, both ways', () => {
    const p = pathToAddress('/runs/run_4c1e09d2/changes', '?step=1%3Afix&file=src%2Fa.ts')!
    expect(changesAtOf(fromAddress(p.address).view)).toEqual({ step: '1:fix', file: 'src/a.ts' })
    expect(roundTrip('/runs/run_4c1e09d2/changes', '?step=fix&file=src%2Fa.ts')).toBe('/runs/run_4c1e09d2/changes?step=fix&file=src%2Fa.ts')
    // The run's own page takes none of it.
    expect(roundTrip('/runs/run_4c1e09d2', '?step=fix')).toBe('/runs/run_4c1e09d2')
  })

  it('on a workflow: `/workflows/<id>/changes?step=&file=`, both ways', () => {
    const p = pathToAddress('/workflows/wf_one/changes', '?step=fix&file=src%2Fa.ts')!
    expect(changesAtOf(fromAddress(p.address).view)).toEqual({ step: 'fix', file: 'src/a.ts' })
    expect(roundTrip('/workflows/wf_one/changes', '?step=fix&file=src%2Fa.ts')).toBe('/workflows/wf_one/changes?step=fix&file=src%2Fa.ts')
  })
})
